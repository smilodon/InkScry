"""多供应商额度/余额查询。

订阅制（5h/1w 窗口）：Codex / Claude / Kimi / 智谱 GLM / MiniMax /
Mirasim（桌面客户端本机接口）；
余额制（预付费或自建中转）：DeepSeek / New API / Sub2API。
凭据来源三种，可混用（同一家以 .env 优先）：
    Codex  ← ~/.codex/auth.json；Claude ← Keychain / ~/.claude/.credentials.json
    当前供应商 ← ~/.claude/settings.json 的 ANTHROPIC_BASE_URL
    Mirasim ← 本机 ~/.mirasim 存在即自动识别（回环接口，无需 token）
    .env   ← INKSCRY_{X}_TOKEN（自建中转必须另配 INKSCRY_{X}_BASE）
带本地缓存：hook 触发刷屏是高频事件，网络查询默认 5 分钟一次；
网络失败时回退到过期的缓存数据，最差情况只是额度数字旧一点。
"""

from __future__ import annotations

import base64
import concurrent.futures
import gzip
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger("inkscry.quota")

AUTH_FILE = Path.home() / ".codex" / "auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "inkscry"
CACHE_FILE = CACHE_DIR / "codex_quota.json"
CACHE_TTL = 300  # 秒


@dataclass
class QuotaWindow:
    used_pct: float          # 已用百分比 0-100
    reset: datetime | None   # 重置时间

    @property
    def remaining_pct(self) -> float:
        return max(0.0, 100.0 - self.used_pct)

    def short_reset(self) -> str:
        if self.reset is None:
            return "?"
        now = datetime.now().astimezone()
        if self.reset.date() == now.date():
            return self.reset.strftime("%H:%M")
        return self.reset.strftime("%m-%d %H:%M")


@dataclass
class CodexQuota:
    five_h: QuotaWindow | None
    one_w: QuotaWindow | None
    fetched_at: float
    stale: bool = False      # True = 网络失败回退的过期缓存
    balance: str = ""        # 余额模式（预付费供应商如 DeepSeek）：如 "¥12.34"
    available: bool = True   # False = 余额不足
    bar_pct: float | None = None   # 余额模式占比条：DeepSeek=充值占比，
                                   # NewAPI=剩余占比；None=不画条
    detail: str = ""         # 余额模式备注行：如 "赠 ¥1.00" / "已用 $2.50"
    extra: QuotaWindow | None = None   # 第三窗口（如 Mirasim 7d_fable）
    extra_label: str = ""              # 第三窗口的档位标签（如 "F周"）


# ---------------------------------------------------------------- 认证

def load_auth() -> tuple[str, str | None]:
    """Codex 凭据：.env 的 INKSCRY_CODEX_TOKEN 优先，否则读 auth.json
    （路径可用 INKSCRY_CODEX_AUTH 覆盖，缺省 ~/.codex/auth.json）。"""
    config.load_dotenv()
    token = os.environ.get("INKSCRY_CODEX_TOKEN")
    if token:
        return token, os.environ.get("INKSCRY_CODEX_ACCOUNT_ID")
    auth_file = Path(os.environ.get("INKSCRY_CODEX_AUTH") or AUTH_FILE).expanduser()
    auth = json.loads(auth_file.read_text(encoding="utf-8"))
    tokens = auth.get("tokens", {})
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("没有找到 Codex access token，请先运行 codex login")
    return access_token, tokens.get("account_id")


def query_usage() -> dict:
    access_token, account_id = load_auth()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "inkscry-quota/1.0",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


# ---------------------------------------------------------------- 解析

def _find_windows(value, results=None) -> list[dict]:
    """递归寻找所有带 used_percent 的额度窗口。"""
    if results is None:
        results = []
    if isinstance(value, dict):
        if "used_percent" in value:
            results.append(value)
        for child in value.values():
            _find_windows(child, results)
    elif isinstance(value, list):
        for child in value:
            _find_windows(child, results)
    return results


def _window_seconds(window: dict) -> float | None:
    if window.get("limit_window_seconds") is not None:
        return float(window["limit_window_seconds"])
    if window.get("window_seconds") is not None:
        return float(window["window_seconds"])
    if window.get("window_minutes") is not None:
        return float(window["window_minutes"]) * 60
    return None


def _window_reset(window: dict) -> datetime | None:
    ts = window.get("reset_at", window.get("resets_at"))
    if ts is not None:
        try:
            return datetime.fromtimestamp(float(ts)).astimezone()
        except (TypeError, ValueError, OSError):
            return None
    secs = window.get("reset_after_seconds")
    if secs is not None:
        try:
            return datetime.fromtimestamp(time.time() + float(secs)).astimezone()
        except (TypeError, ValueError, OSError):
            return None
    return None


def _select_window(windows: list[dict], target_seconds: float) -> dict | None:
    """选窗口时长最接近目标的窗口；偏差超过 2 倍视为不存在该档。

    （有的账号只暴露周窗口，不能把周窗口当成 5h 窗口显示）
    """
    candidates = []
    for w in windows:
        secs = _window_seconds(w)
        if secs is not None and 0.5 <= secs / target_seconds <= 2.0:
            candidates.append((abs(secs - target_seconds), w))
    # key 只比偏差：两个窗口时长相同时（接口会返回主/副同长窗口），
    # 裸 min 会退化成 dict < dict 直接 TypeError
    return min(candidates, key=lambda c: c[0])[1] if candidates else None


def parse_usage(data: dict) -> CodexQuota:
    windows = _find_windows(data)

    def make(target: float) -> QuotaWindow | None:
        w = _select_window(windows, target)
        if w is None:
            return None
        return QuotaWindow(used_pct=float(w.get("used_percent", 0)),
                           reset=_window_reset(w))

    return CodexQuota(five_h=make(5 * 3600),
                      one_w=make(7 * 24 * 3600),
                      fetched_at=time.time())


# ---------------------------------------------------------------- 缓存入口

def _to_json(q: CodexQuota) -> dict:
    def win(w: QuotaWindow | None):
        return None if w is None else {
            "used_pct": w.used_pct,
            "reset": w.reset.timestamp() if w.reset else None,
        }
    return {"five_h": win(q.five_h), "one_w": win(q.one_w),
            "fetched_at": q.fetched_at,
            "balance": q.balance, "available": q.available,
            "bar_pct": q.bar_pct, "detail": q.detail,
            "extra": win(q.extra), "extra_label": q.extra_label}


def _from_json(d: dict, stale: bool) -> CodexQuota:
    def win(w):
        if w is None:
            return None
        reset = w.get("reset")
        return QuotaWindow(
            used_pct=w["used_pct"],
            reset=datetime.fromtimestamp(reset).astimezone() if reset else None)
    return CodexQuota(five_h=win(d.get("five_h")), one_w=win(d.get("one_w")),
                      fetched_at=d.get("fetched_at", 0), stale=stale,
                      balance=d.get("balance", ""),
                      available=d.get("available", True),
                      bar_pct=d.get("bar_pct"),
                      detail=d.get("detail", ""),
                      extra=win(d.get("extra")),
                      extra_label=d.get("extra_label", ""))


def get_quota(cache_ttl: int = CACHE_TTL) -> CodexQuota | None:
    """取额度：缓存新鲜直接返回；过期则联网刷新；失败回退过期缓存。"""
    cached: CodexQuota | None = None
    try:
        cached = _from_json(json.loads(CACHE_FILE.read_text("utf-8")), stale=True)
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    if cached and time.time() - cached.fetched_at < cache_ttl:
        cached.stale = False
        return cached
    try:
        quota = parse_usage(query_usage())
    except (OSError, urllib.error.URLError, RuntimeError, KeyError,
            json.JSONDecodeError):
        return cached  # 无缓存时返回 None
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(_to_json(quota)), "utf-8")
    except OSError:
        pass
    return quota


# ---------------------------------------------------------------- Coding Plan 供应商
# 路由与接口对齐 cc-switch coding_plan.rs（Kimi / 智谱 GLM / MiniMax）

PROVIDER_LABELS = {"claude": "CLAUDE", "kimi": "KIMI", "zhipu": "GLM",
                   "minimax": "MINIMAX", "deepseek": "DEEPSEEK",
                   "newapi": "NEWAPI", "sub2api": "SUB2API",
                   "mirasim": "MIRASIM"}
# .env 变量名后缀（INKSCRY_{X}_TOKEN / INKSCRY_{X}_BASE），兼定义面板顺序
ENV_KEYS = {"claude": "CLAUDE", "kimi": "KIMI", "zhipu": "GLM",
            "minimax": "MINIMAX", "deepseek": "DEEPSEEK",
            "newapi": "NEWAPI", "sub2api": "SUB2API"}
# 自建中转（newapi/sub2api）没有默认域名，必须在 .env 里配 BASE
DEFAULT_BASES = {
    "claude": "https://api.anthropic.com",
    "kimi": "https://api.kimi.com/coding",
    "zhipu": "https://open.bigmodel.cn",     # 国际版填 https://api.z.ai
    "minimax": "https://api.minimaxi.com",   # 国际版填 https://api.minimax.io
    "deepseek": "https://api.deepseek.com",
}
# INKSCRY_PANEL_ORDER 里写面板标题；ZHIPU 视为 GLM 的别名
_ORDER_ALIASES = {"ZHIPU": "GLM"}


def panel_order_rank() -> dict[str, int]:
    """解析 INKSCRY_PANEL_ORDER（逗号分隔的面板标题，如 KIMI,GLM,CODEX）。

    返回 {标题: 序号}，未配置返回空 dict。列出的面板排前面；
    未列出的排后面并保持默认顺序（依赖 list.sort 的稳定性）。
    """
    config.load_dotenv()
    raw = os.environ.get("INKSCRY_PANEL_ORDER", "")
    rank: dict[str, int] = {}
    for part in raw.split(","):
        name = part.strip().upper()
        name = _ORDER_ALIASES.get(name, name)
        if name and name not in rank:
            rank[name] = len(rank)
    return rank


def load_coding_auth() -> tuple[str, str, str] | None:
    """从 ~/.claude/settings.json 识别订阅制 Coding Plan 供应商。

    返回 (provider_id, base_url, token)；未配置或不识别返回 None。
    """
    try:
        env = json.loads(CLAUDE_SETTINGS.read_text("utf-8")).get("env", {})
    except (OSError, json.JSONDecodeError):
        return None
    base = (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if not token or not base:
        return None
    lower = base.lower()
    if "kimi.com/coding" in lower:
        return "kimi", base, token
    if "bigmodel.cn" in lower or "api.z.ai" in lower:
        return "zhipu", base, token
    if "minimaxi.com" in lower or "minimax.io" in lower:
        return "minimax", base, token
    if "deepseek.com" in lower:
        return "deepseek", base, token
    return None


def _parse_reset(value) -> datetime | None:
    """resetTime 兼容 epoch 秒/毫秒与 ISO 字符串。"""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts).astimezone()
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError):
        pass
    return None


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":  # 服务端可能无视 Accept-Encoding 强制 gzip（如 CDN）
        raw = gzip.decompress(raw)
    return json.loads(raw)


def query_kimi_usage(base: str, token: str) -> CodexQuota:
    """Kimi: GET {base}/v1/usages。limits[0].detail→5h，usage→1w，
    均为 {limit, remaining, resetTime}。"""
    body = _get_json(f"{base}/v1/usages",
                     {"Authorization": f"Bearer {token}",
                      "Accept": "application/json"})

    def win(d: dict) -> QuotaWindow:
        limit = float(d.get("limit") or 1)
        remaining = float(d.get("remaining") or 0)
        used_pct = max(0.0, limit - remaining) / limit * 100
        return QuotaWindow(used_pct=used_pct, reset=_parse_reset(d.get("resetTime")))

    five_h = one_w = None
    limits = body.get("limits")
    if isinstance(limits, list) and limits:
        detail = limits[0].get("detail")
        if isinstance(detail, dict):
            five_h = win(detail)
    usage = body.get("usage")
    if isinstance(usage, dict):
        one_w = win(usage)
    return CodexQuota(five_h=five_h, one_w=one_w, fetched_at=time.time())


def query_zhipu_usage(base: str, token: str) -> CodexQuota:
    """智谱: GET /api/monitor/usage/quota/limit（注意不加 Bearer 前缀）。

    data.limits[] 取 TOKENS_LIMIT/CREDIT_LIMIT 条目，percentage 即已用百分比；
    unit=3 → 5h 窗口，unit=6 → 周窗口；unit 缺失走重置时间启发式兜底。
    """
    quota_base = ("https://open.bigmodel.cn" if "bigmodel.cn" in base.lower()
                  else "https://api.z.ai")
    body = _get_json(f"{quota_base}/api/monitor/usage/quota/limit",
                     {"Authorization": token,
                      "Content-Type": "application/json",
                      "Accept-Language": "en-US,en"})
    if body.get("success") is False:
        raise KeyError(str(body.get("msg", "unknown error")))

    five = weekly = None
    unclassified: list[QuotaWindow] = []
    for item in (body.get("data") or {}).get("limits") or []:
        if str(item.get("type", "")).upper() not in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            continue
        w = QuotaWindow(used_pct=float(item.get("percentage") or 0),
                        reset=_parse_reset(item.get("nextResetTime")))
        unit = item.get("unit")
        if unit == 3 and five is None:
            five = w
        elif unit == 6 and weekly is None:
            weekly = w
        else:
            unclassified.append(w)
    # 兜底：无重置时间者优先归 5h，其余按重置时间升序补空位
    unclassified.sort(key=lambda w: (w.reset is not None,
                                     w.reset.timestamp() if w.reset else 0))
    for w in unclassified:
        if five is None:
            five = w
        elif weekly is None:
            weekly = w
    return CodexQuota(five_h=five, one_w=weekly, fetched_at=time.time())


def query_minimax_usage(base: str, token: str) -> CodexQuota:
    """MiniMax: GET /v1/api/openplatform/coding_plan/remains（国内/国际双域名）。

    model_remains[] 取 model_name=="general"；接口给剩余百分比需反转；
    周窗口仅 current_weekly_status==1 时有效（3 表示套餐无周限额）。
    """
    domain = ("api.minimaxi.com" if "minimaxi.com" in base.lower()
              else "api.minimax.io")
    body = _get_json(f"https://{domain}/v1/api/openplatform/coding_plan/remains",
                     {"Authorization": f"Bearer {token}",
                      "Content-Type": "application/json"})
    base_resp = body.get("base_resp") or {}
    if base_resp.get("status_code", 0) != 0:
        raise KeyError(str(base_resp.get("status_msg", "unknown error")))

    item = next((i for i in body.get("model_remains") or []
                 if i.get("model_name") == "general"), None)
    if item is None:
        return CodexQuota(five_h=None, one_w=None, fetched_at=time.time())
    five = weekly = None
    pct = item.get("current_interval_remaining_percent")
    if pct is not None:
        five = QuotaWindow(used_pct=100.0 - float(pct),
                           reset=_parse_reset(item.get("end_time")))
    if item.get("current_weekly_status") == 1:
        pct = item.get("current_weekly_remaining_percent")
        if pct is not None:
            weekly = QuotaWindow(used_pct=100.0 - float(pct),
                                 reset=_parse_reset(item.get("weekly_end_time")))
    return CodexQuota(five_h=five, one_w=weekly, fetched_at=time.time())


def query_deepseek_usage(base: str, token: str) -> CodexQuota:
    """DeepSeek: GET /user/balance（预付费余额，无订阅窗口 → 余额模式）。

    balance_infos[] 按币种给 total/granted/topped_up（字符串）：
    占比条 = 充值占比（赠送部分会过期）；备注行显示赠送余额。
    is_available=false 表示余额不足。对齐 cc-switch balance.rs。
    """
    body = _get_json("https://api.deepseek.com/user/balance",
                     {"Authorization": f"Bearer {token}",
                      "Accept": "application/json"})
    sym = {"CNY": "¥", "USD": "$"}
    parts, grants = [], []
    bar_pct = None
    for info in body.get("balance_infos") or []:
        total = info.get("total_balance")
        if total is None:
            continue
        cur = str(info.get("currency", ""))
        s = sym.get(cur, cur + " ")
        total_f = float(total)
        parts.append(f"{s}{total_f:.2f}")
        granted = info.get("granted_balance")
        if granted is not None and float(granted) > 0:
            grants.append(f"赠{s}{float(granted):.2f}")
        topped = info.get("topped_up_balance")
        if topped is not None and total_f > 0:
            bar_pct = float(topped) / total_f * 100
    if not parts:
        raise KeyError("balance_infos 为空")
    return CodexQuota(five_h=None, one_w=None, fetched_at=time.time(),
                      balance=" ".join(parts),
                      available=bool(body.get("is_available", True)),
                      bar_pct=bar_pct, detail=" ".join(grants))


def query_claude_usage(base: str, token: str) -> CodexQuota:
    """Claude 官方订阅: GET api.anthropic.com/api/oauth/usage（OAuth token）。

    five_hour/seven_day 各为 {utilization=已用%, resets_at=ISO}，
    需带 anthropic-beta: oauth-2025-04-20 头。对齐 cc-switch subscription.rs。
    """
    body = _get_json("https://api.anthropic.com/api/oauth/usage",
                     {"Authorization": f"Bearer {token}",
                      "anthropic-beta": "oauth-2025-04-20",
                      "Accept": "application/json"})

    def win(d) -> QuotaWindow | None:
        if not isinstance(d, dict) or d.get("utilization") is None:
            return None
        return QuotaWindow(used_pct=float(d["utilization"]),
                           reset=_parse_reset(d.get("resets_at")))

    return CodexQuota(five_h=win(body.get("five_hour")),
                      one_w=win(body.get("seven_day")),
                      fetched_at=time.time())


def query_newapi_usage(base: str, token: str) -> CodexQuota:
    """New API 中转站: GET /api/user/self（对齐 cc-switch New API 模板）。

    认证 = 控制台「系统访问令牌」+ New-Api-User 头（用户 ID，由调用方
    注入的 INKSCRY_NEWAPI_USER_ID）；data.quota 为剩余配额，
    500000 units = $1（One API 约定）→ 余额模式。
    """
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    uid = os.environ.get("INKSCRY_NEWAPI_USER_ID")
    if uid:
        headers["New-Api-User"] = uid
    body = _get_json(f"{base}/api/user/self", headers)
    if not body.get("success"):
        raise KeyError(str(body.get("message", "unknown error")))
    data = body.get("data") or {}
    remaining = float(data.get("quota") or 0) / 500000
    used = float(data.get("used_quota") or 0) / 500000
    bar_pct = (remaining / (remaining + used) * 100
               if remaining + used > 0 else None)
    return CodexQuota(five_h=None, one_w=None, fetched_at=time.time(),
                      balance=f"${remaining:.2f}",
                      available=remaining > 0,
                      bar_pct=bar_pct,
                      detail=f"已用 ${used:.2f}" if used > 0 else "")


def query_sub2api_usage(base: str, token: str) -> CodexQuota:
    """Sub2API 中转站: GET /v1/usage（sk- API key，对齐 cc-switch）。

    响应含 balance（USD 余额）→ 余额模式；daily_usage 最近一天的
    花费进备注行。sk- key 长期有效，比面板 JWT 方案省心。
    """
    body = _get_json(f"{base}/v1/usage",
                     {"Authorization": f"Bearer {token}",
                      "Accept": "application/json"})
    if body.get("balance") is None:
        raise KeyError("响应缺少 balance 字段")
    bal = float(body["balance"])
    detail = ""
    daily = body.get("daily_usage") or []
    if daily:
        last = max(daily, key=lambda d: d.get("date") or "")
        cost = last.get("actual_cost", last.get("cost"))
        if cost is not None:
            detail = f"{(last.get('date') or '?')[5:]} ${float(cost):.2f}"
    return CodexQuota(five_h=None, one_w=None, fetched_at=time.time(),
                      balance=f"${bal:.2f}",
                      available=bool(body.get("isValid", True)) and bal > 0,
                      detail=detail)


def _mirasim_bases() -> list[str]:
    """枚举本机 Mirasim 进程的回环监听端口（服务端口随启动漂移）。

    对齐 mirasim-quota-widget 的 probe 思路：按平台列出进程的
    127.0.0.1 LISTEN 端口，交给调用方逐个探测 /api/health。
    """
    cmds = {
        "darwin": ["lsof", "-a", "-iTCP", "-sTCP:LISTEN", "-nP",
                   "-c", "Mirasim"],
        "linux": ["sh", "-c", "ss -ltnpH 2>/dev/null | grep -i mirasim"],
        "win32": ["powershell", "-NoProfile", "-Command",
                  "$p=(Get-Process Mirasim -ErrorAction SilentlyContinue).Id;"
                  "if($p){Get-NetTCPConnection -State Listen | Where-Object"
                  " { $p -contains $_.OwningProcess -and $_.LocalAddress"
                  " -eq '127.0.0.1' } | ForEach-Object LocalPort}"],
    }
    try:
        out = subprocess.run(cmds.get(sys.platform, cmds["linux"]),
                             capture_output=True, text=True,
                             timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ports = re.findall(r"127\.0\.0\.1:(\d{2,5})", out)
    if not ports:   # win32 输出为每行一个纯端口号
        ports = re.findall(r"^\s*(\d{2,5})\s*$", out, re.M)
    return [f"http://127.0.0.1:{p}" for p in dict.fromkeys(ports)]


# Mirasim 0.0.225 起逐步封死本机 HTTP 接口，0.0.227 起 /v1/limits 彻底
# 下线（hub 变成带令牌的 shell 自动化 API，只有窗口操控 op）。目前仅存的
# 数据通道是渲染层 WebSocket（ws://{服务端口}/ws）：连上后服务端主动推
# relay.usage（5h/7d/7d_fable 三窗口的 usedPercent/resetAt）与 usage
# （各 agent 窗口）消息。0.0.276 起回环访问也要鉴权：服务端每次启动
# 生成随机 token 写到 ~/.mirasim/run/local-{端口}.token（0600，退出即删），
# HTTP 用 ?token= / Authorization: Bearer，WS 握手用 ?token=（Mirasim 自家
# 客户端就是这么连的）；/api/health 免鉴权，探测端口不受影响。
# 服务端不再在建连时主动发快照（只有 sessions），relay/usage 按约 60s
# 的节拍广播；但客户端发一条 {"type":"ready"}（Mirasim 自家客户端的开场
# 握手）服务端会立刻回全量快照（init + relay + usage + …），冷路径因此
# 仍能秒级拿到数据。以下是一个只读极简 WS 客户端。

_MIRASIM_WS_WAIT = 10.0   # 等 relay.usage 的最长秒数（ready 后快照 <1s 即到）
_MIRASIM_WS_HELLO = json.dumps({"type": "ready"})   # 开场握手，换全量快照
_mirasim_ws_base: str | None = None   # 进程内记住探测成功的服务端口


def _mirasim_server_base(base: str) -> str:
    """定位 Mirasim 服务端口：/api/health 报 name=mirasim 的才是
    （shell 端口回 401，其他端口不回此 JSON）。"""
    global _mirasim_ws_base
    if base and base != "auto":
        return base.rstrip("/")
    candidates = ([_mirasim_ws_base] if _mirasim_ws_base else []) \
        + [b for b in _mirasim_bases() if b != _mirasim_ws_base]
    for b in candidates:
        try:
            data = _get_json(f"{b}/api/health", {"Accept": "application/json"})
        except Exception:   # 连接拒绝 / 非 JSON（SPA 兜底页）
            continue
        if isinstance(data, dict) and data.get("name") == "mirasim":
            _mirasim_ws_base = b
            return b
    _mirasim_ws_base = None
    raise OSError("未探测到 Mirasim 本机服务端口（客户端未运行？）")


def _mirasim_local_token(http_base: str) -> str | None:
    """本机访问 token：INKSCRY_MIRASIM_TOKEN 显式指定，否则按服务端口读
    ~/.mirasim/run/local-{端口}.token。token 随服务端每次启动轮换，
    每次建连前都要重读，不能缓存。"""
    explicit = os.environ.get("INKSCRY_MIRASIM_TOKEN", "").strip()
    if explicit:
        return explicit
    port = urllib.parse.urlparse(http_base).port
    if not port:
        return None
    try:
        tok = (Path.home() / ".mirasim" / "run"
               / f"local-{port}.token").read_text("utf-8").strip()
    except OSError:
        return None
    return tok or None


def _mirasim_ws_url(http_base: str) -> str:
    """http://127.0.0.1:{端口} → ws://127.0.0.1:{端口}/ws[?token=…]。"""
    url = http_base.replace("http", "ws", 1) + "/ws"
    tok = _mirasim_local_token(http_base)
    return f"{url}?token={urllib.parse.quote(tok, safe='')}" if tok else url


def _ws_texts(url: str, deadline: float, hello: str | None = None):
    """极简只读 WebSocket 客户端（stdlib 零依赖）：握手后逐帧 yield 文本
    消息，直到 deadline。ping 自动回 pong（客户端帧按 RFC 带掩码）。
    hello 非空则握手成功后先发这一条文本帧（如 Mirasim 的 ready）。"""
    u = urllib.parse.urlparse(url)
    sock = socket.create_connection((u.hostname, u.port or 80), timeout=5)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        target = (u.path or '/') + (f'?{u.query}' if u.query else '')
        sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("WS 握手被对端断开")
            buf += chunk
        head, _, buf = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("WS 握手被拒绝: " +
                          head.split(b"\r\n", 1)[0].decode("utf-8", "replace"))
        sock.settimeout(1.0)

        def fill(n: int) -> bytes:
            nonlocal buf
            while len(buf) < n:
                if time.time() > deadline:
                    raise TimeoutError
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise OSError("WS 连接被对端关闭")
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out

        def send_frame(opcode: int, payload: bytes = b"") -> None:
            mask = os.urandom(4)
            n = len(payload)
            if n < 126:
                header = bytes([0x80 | opcode, 0x80 | n])
            elif n < 65536:
                header = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
            else:
                header = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            sock.sendall(header + mask + masked)

        if hello:
            send_frame(0x1, hello.encode("utf-8"))

        frag = bytearray()
        while time.time() < deadline:
            h = fill(2)
            fin, opcode = h[0] & 0x80, h[0] & 0x0F
            ln = h[1] & 0x7F
            if ln == 126:
                ln = int.from_bytes(fill(2), "big")
            elif ln == 127:
                ln = int.from_bytes(fill(8), "big")
            mask = fill(4) if h[1] & 0x80 else None
            payload = fill(ln) if ln else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:        # ping → pong
                send_frame(0xA, payload)
            elif opcode == 0x8:      # close
                send_frame(0x8)
                return
            elif opcode in (0x1, 0x0):   # text / continuation
                frag += payload
                if fin:
                    yield bytes(frag).decode("utf-8", "replace")
                    frag.clear()
            # 二进制等其余帧忽略
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _mirasim_relay_usage(base: str) -> dict:
    """从渲染层 WS 抓 relay.usage 推送（5h/7d/7d_fable 的 usedPercent
    与 resetAt，服务端实时刷新）。"""
    ws_url = _mirasim_ws_url(_mirasim_server_base(base))
    deadline = time.time() + _MIRASIM_WS_WAIT
    gen = _ws_texts(ws_url, deadline, hello=_MIRASIM_WS_HELLO)
    try:
        for text in gen:
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            usage = (msg.get("relay") or {}).get("usage") \
                if isinstance(msg, dict) else None
            if isinstance(usage, dict) and usage.get("ok") \
                    and usage.get("windows"):
                return usage
    except (OSError, TimeoutError):
        pass
    finally:
        gen.close()
    raise OSError("Mirasim WS 已连接但未等到额度推送（版本又不兼容？）")


def _windows_to_quota(windows: list) -> CodexQuota:
    """relay.usage.windows[] → CodexQuota：5h/7d 主窗 + 7d_fable 第三档。
    usedPercent 直接是百分比（服务端给到一位小数），resetAt 为 ISO 时间。"""
    wins: dict[str, QuotaWindow] = {}
    for w in windows:
        try:
            used = float(w["usedPercent"])
        except (TypeError, ValueError, KeyError):
            continue
        wins[str(w.get("label", ""))] = QuotaWindow(
            used_pct=used, reset=_parse_reset(w.get("resetAt")))
    fable = wins.get("7d_fable")
    return CodexQuota(five_h=wins.get("5h"), one_w=wins.get("7d"),
                      fetched_at=time.time(),
                      extra=fable, extra_label="F周" if fable else "")


def query_mirasim_usage(base: str, token: str) -> CodexQuota:
    """Mirasim 桌面客户端：旁听 ws://127.0.0.1:{服务端口}/ws 的
    relay.usage 推送（0.0.227 起 /v1/limits 已下线，WS 是仅存通道；
    0.0.276 起握手须带 ~/.mirasim/run 下的本机 token）。

    推送制通道与轮询不合拍（relay 数据 ≤60s 一跳，冷连接可能等不到），
    常驻进程应改用 start_mirasim_listener() 长连落缓存；本函数作为
    无常驻进程时的冷路径兜底（等 _MIRASIM_WS_WAIT 秒，等不到抛错走
    过期缓存）。

    同一面板显示同源三窗口：5h、7d 总窗，账号带 7d_fable 档位
    子额度（全用 Fable 时先撞墙）时以第三档「F周」并列展示；
    无此窗口的账号自动只有两档。
    """
    return _windows_to_quota(_mirasim_relay_usage(base)["windows"])


# ── 常驻 WS 监听（菜单栏/托盘启动；hook 等短进程共享其写出的缓存）──────
_mirasim_listener_started = False


def start_mirasim_listener() -> None:
    """常驻进程调用一次：后台线程长连 Mirasim 渲染层 WS，把推送实时
    写进额度缓存文件（对 menubar/tray/hook 全生效）：
      - relay.usage → mirasim 缓存（5h/7d/7d_fable 三窗口）
      - usage(agent=codex) → codex 缓存（Mirasim MITM 从 Codex 真实
        响应头捕获的窗口；新鲜时同步轮免查 chatgpt.com 慢接口）
    未安装 Mirasim 时不启动；客户端未运行时低频重试。"""
    global _mirasim_listener_started
    if _mirasim_listener_started:
        return
    if not (os.environ.get("INKSCRY_MIRASIM_BASE")
            or (Path.home() / ".mirasim").exists()):
        return
    _mirasim_listener_started = True
    threading.Thread(target=_mirasim_listen_loop, daemon=True,
                     name="mirasim-ws").start()


def _quota_sig(q: CodexQuota) -> tuple:
    """防重播签名：三窗口的百分比与重置时间（不含 fetched_at）。"""
    return tuple(sorted(
        (k, round(v.used_pct, 3), v.reset) for k, v in
        (("5h", q.five_h), ("7d", q.one_w), ("x", q.extra)) if v))


def _cache_write_atomic(path: Path, q: CodexQuota) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_to_json(q)), "utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _codex_usage_to_quota(entry: dict) -> CodexQuota | None:
    """WS usage 消息里 agent=codex 的条目 → CodexQuota。

    fetched_at 取 capturedAt（数据被捕获的时刻）而非落地时刻：
    该数据随 Codex 真实调用而更新，闲置时自动老化，缓存过期后
    同步轮回退官方接口直查，不会因旁听通道停在旧数字上。
    gpt-reserve 备用池不上屏（0% 占用，且宽面板会多吃一个槽位）。
    """
    if not entry.get("ok") or not entry.get("windows"):
        return None
    five = week = None
    for w in entry["windows"]:
        if not isinstance(w, dict) or w.get("detail") == "gpt-reserve":
            continue
        try:
            used = float(w["usedPercent"])
        except (TypeError, ValueError):
            continue
        win = QuotaWindow(used_pct=used, reset=_parse_reset(w.get("resetAt")))
        if w.get("label") == "5h":
            five = win
        elif w.get("label") == "7d":
            week = win
    if five is None and week is None:
        return None
    captured = _parse_reset(entry.get("capturedAt"))
    return CodexQuota(five_h=five, one_w=week,
                      fetched_at=captured.timestamp() if captured
                      else time.time())


def _mirasim_listen_loop() -> None:
    warned: str | None = None   # 同一条握手拒绝原因只告警一次
    while True:
        try:
            base = _mirasim_server_base("auto")
        except OSError:
            time.sleep(60)   # 客户端未运行：低频重试
            continue
        try:
            _mirasim_listen_once(base)
        except Exception as e:
            if "握手被拒绝" in str(e):
                # 401 等：token 文件缺失/过期或版本又改鉴权。每 5s 重试
                # 只会刷爆对方 server.log，改 60s 退避
                if str(e) != warned:
                    log.warning("Mirasim WS %s（本机 token 缺失或不匹配？"
                                "60s 后重试）", e)
                    warned = str(e)
                time.sleep(60)
                continue
            log.debug("Mirasim WS 断开，5s 后重连: %s", e)
            time.sleep(5)


def _mirasim_listen_once(base: str) -> None:
    ws_url = _mirasim_ws_url(base)   # 每次建连重读 token（随服务端轮换）
    last_mirasim: tuple | None = None
    for text in _ws_texts(ws_url, time.time() + 7 * 86400,
                          hello=_MIRASIM_WS_HELLO):
        try:
            msg = json.loads(text)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        relay_usage = (msg.get("relay") or {}).get("usage")
        if isinstance(relay_usage, dict) and relay_usage.get("ok") \
                and relay_usage.get("windows"):
            q = _windows_to_quota(relay_usage["windows"])
            sig = _quota_sig(q)
            if sig != last_mirasim:
                _cache_write_atomic(CACHE_DIR / "mirasim_quota.json", q)
                last_mirasim = sig
                log.debug("Mirasim 额度推送已落缓存")
        if msg.get("type") == "usage":
            for entry in msg.get("usage") or []:
                if not isinstance(entry, dict) or entry.get("agent") != "codex":
                    continue
                q = _codex_usage_to_quota(entry)
                if q is None:
                    continue
                try:   # 缓存里已有更新鲜的（如刚直查的）就不回滚
                    old_at = float(json.loads(
                        CACHE_FILE.read_text())["fetched_at"])
                except (OSError, ValueError, KeyError, TypeError):
                    old_at = 0.0
                if q.fetched_at > old_at:
                    _cache_write_atomic(CACHE_FILE, q)
                    log.debug("Codex 窗口捕获已落缓存（capturedAt 口径）")


_QUERY_FNS = {
    "claude": query_claude_usage,
    "kimi": query_kimi_usage,
    "zhipu": query_zhipu_usage,
    "minimax": query_minimax_usage,
    "deepseek": query_deepseek_usage,
    "newapi": query_newapi_usage,
    "sub2api": query_sub2api_usage,
    "mirasim": query_mirasim_usage,
}


def _base_pid(key: str) -> str:
    """编号实例（newapi_2）→ 基础供应商 id（newapi）。"""
    return key.rsplit("_", 1)[0] if "_" in key else key


def _load_claude_token() -> str | None:
    """Claude Code 官方 OAuth accessToken。

    来源优先级同 cc-switch subscription.rs：macOS Keychain
    （service "Claude Code-credentials"）→ ~/.claude/.credentials.json；
    JSON key 兼容 claudeAiOauth / claude.ai_oauth 两种。
    """
    raw = None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s",
                 "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                raw = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if raw is None:
        try:
            raw = (Path.home() / ".claude" / ".credentials.json").read_text("utf-8")
        except OSError:
            return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    entry = parsed.get("claudeAiOauth") or parsed.get("claude.ai_oauth") or {}
    return entry.get("accessToken") or None


def load_coding_providers() -> list[tuple[str, str, str, str]]:
    """全部已配置的供应商 [(key, base, token, 面板标签)]，按 ENV_KEYS 顺序。

    来源合并：Claude 官方 OAuth 凭据（Keychain/凭据文件）+
    ~/.claude/settings.json 自动识别的当前供应商 + .env 里
    INKSCRY_{X}_TOKEN 显式配置的；同一家时 .env 优先。
    .env 配置的供应商（TOKEN/BASE）均支持逗号列表多实例，按位置配对
    （如 KIMI_TOKEN=t1,t2 → KIMI/KIMI2 两面板；有默认域名的家 BASE
    可省略，全部实例用默认域名；自建中转必须配 BASE）。
    """
    config.load_dotenv()
    providers: dict[str, tuple[str, str, str]] = {}
    active = load_coding_auth()
    if active:
        pid, base, token = active
        providers[pid] = (base, token, PROVIDER_LABELS[pid])
    claude_token = _load_claude_token()
    if claude_token:
        providers["claude"] = (DEFAULT_BASES["claude"], claude_token,
                               PROVIDER_LABELS["claude"])
    for pid, name in ENV_KEYS.items():
        token = os.environ.get(f"INKSCRY_{name}_TOKEN")
        if not token:
            continue
        # 所有供应商支持逗号列表多实例，按位置配对：第 i 个 TOKEN 配
        # 第 i 个 BASE（有默认域名的家，BASE 不配时全部实例用默认域名）；
        # LABELS 同样按位置自定义面板名，空位回退自动命名
        tokens = [t.strip() for t in token.split(",") if t.strip()]
        raw_base = os.environ.get(f"INKSCRY_{name}_BASE") or ""
        bases = [b.strip().rstrip("/") for b in raw_base.split(",") if b.strip()]
        labels = [l.strip()
                  for l in (os.environ.get(f"INKSCRY_{name}_LABELS")
                            or "").split(",")]
        default = DEFAULT_BASES.get(pid, "")
        for i, tok in enumerate(tokens):
            base = (bases[i] if i < len(bases)
                    else (bases[-1] if bases else default))
            if not base:
                break   # 自建中转无默认域名，必须配 BASE
            key = pid if i == 0 else f"{pid}_{i + 1}"
            label = (labels[i] if i < len(labels) and labels[i]
                     else (name if i == 0 else f"{name}{i + 1}"))
            providers[key] = (base, tok, label)
    # Mirasim 桌面客户端（本机回环接口，无需 token）：装了就自动识别；
    # INKSCRY_MIRASIM_BASE 可显式固定 hub 地址（缺省 "auto" 由查询阶段
    # 探测漂移端口）；INKSCRY_MIRASIM_LABELS 自定义面板名
    mira_base = (os.environ.get("INKSCRY_MIRASIM_BASE") or "").rstrip("/")
    if mira_base or (Path.home() / ".mirasim").exists():
        label = next((l.strip() for l in
                      (os.environ.get("INKSCRY_MIRASIM_LABELS") or "").split(",")
                      if l.strip()), PROVIDER_LABELS["mirasim"])
        providers.setdefault("mirasim", (mira_base or "auto", "", label))
    return [(key, *providers[key]) for key in providers]


# newapi 的 User-ID 通过 env 传递（见 _provider_quota），并发查询时加锁
_NEWAPI_LOCK = threading.Lock()


def _newapi_uid(key: str) -> str | None:
    """New-Api-User 头值：INKSCRY_NEWAPI_USER_ID 同样支持逗号列表，
    第 i 个值对应第 i 个实例；没配够按最后一个（同站多账号常见同 ID）。"""
    parts = [p.strip() for p in os.environ.get("INKSCRY_NEWAPI_USER_ID",
                                               "").split(",")]
    if not any(parts):
        return None
    if key == "newapi":
        return parts[0] or None
    try:
        idx = int(key.rsplit("_", 1)[1]) - 1
    except (IndexError, ValueError):
        return None
    return (parts[idx] if idx < len(parts) and parts[idx] else parts[-1]) or None


def _provider_quota(key: str, base: str, token: str,
                    cache_ttl: int) -> CodexQuota | None:
    """单个供应商实例的额度，缓存策略与 Codex 相同（按实例分文件）。"""
    cache_file = CACHE_DIR / f"{key}_quota.json"
    cached: CodexQuota | None = None
    try:
        cached = _from_json(json.loads(cache_file.read_text("utf-8")), stale=True)
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    if cached and time.time() - cached.fetched_at < cache_ttl:
        cached.stale = False
        return cached
    pid = _base_pid(key)
    try:
        if pid == "newapi":
            # 按实例注入 New-Api-User（query_newapi_usage 读 env）：
            # env 是进程共享的，并发查询多实例时必须与查询原子化
            with _NEWAPI_LOCK:
                os.environ["INKSCRY_NEWAPI_USER_ID"] = _newapi_uid(key) or ""
                quota = _QUERY_FNS[pid](base, token)
        else:
            quota = _QUERY_FNS[pid](base, token)
    except (OSError, urllib.error.URLError, KeyError, ValueError,
            json.JSONDecodeError):
        return cached
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(_to_json(quota)), "utf-8")
    except OSError:
        pass
    return quota


def get_coding_quotas(cache_ttl: int = CACHE_TTL) -> list[tuple[str, CodexQuota]]:
    """所有已配置 Coding Plan 供应商的额度 [(面板标签, 额度)]。

    各家并发查询：接口互不相干，串行会把一轮耗时拉成各家之和
    （某家挂起还要白等 15s 超时），并发后 ≈ 最慢一家。
    """
    providers = load_coding_providers()
    if not providers:
        return []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(providers))) as pool:
        quotas = list(pool.map(
            lambda p: _provider_quota(p[0], p[1], p[2], cache_ttl), providers))
    return [(label, q) for (*_, label), q in zip(providers, quotas) if q]
