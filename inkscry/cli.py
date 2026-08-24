"""InkScry CLI 入口：接收 Claude Code Hook 事件 → 渲染 → 推送墨水屏。

用法示例:
    cli.py --event notification          # 从 stdin 读取 hook JSON
    cli.py --demo --save demo.bmp        # 仅渲染示例图，不发蓝牙
    cli.py --demo                        # 渲染示例图并推送到墨水屏
    cli.py --clear / --sleep             # 清屏 / 休眠
    cli.py --print-hooks                 # 输出 settings.json hooks 配置
    cli.py --sync                        # 定时同步单次：额度有变化才刷屏
    cli.py --watch                       # 常驻定时同步程序
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from . import ble, config, monitor, quota, renderer

log = logging.getLogger("inkscry")

EVENT_STATUS = {
    "notification": "waiting",
    "permission": "waiting",
    "stop": "done",
    "session-end": "idle",
    "error": "error",
    "test": "running",
}


def read_hook_stdin() -> dict:
    """Hook 触发时 Claude Code 会把事件 JSON 写到 stdin。"""
    if sys.stdin.isatty():
        return {}
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def _quota_panel(label: str, q: quota.CodexQuota) -> renderer.QuotaPanel:
    p = renderer.QuotaPanel(label=label, stale=q.stale)
    if q.balance:   # 预付费供应商（DeepSeek 等）→ 余额模式
        p.balance = q.balance
        p.alert = not q.available
        p.bar_pct = q.bar_pct
        p.detail = q.detail
        return p
    if q.five_h:
        p.five_pct = q.five_h.remaining_pct
        # 无重置时间（如窗口未启动）不渲染 "rst ?" 行
        if q.five_h.reset:
            p.five_reset = q.five_h.short_reset()
    if q.one_w:
        p.week_pct = q.one_w.remaining_pct
        if q.one_w.reset:
            p.week_reset = q.one_w.short_reset()
    return p


def build_state(event: str, message: str, hook: dict,
                with_quota: bool = True) -> renderer.DashboardState:
    status = EVENT_STATUS.get(event, "idle")
    message = message or hook.get("message", "")

    info = None
    transcript = hook.get("transcript_path")
    if transcript and Path(transcript).exists():
        info = monitor.parse_session(Path(transcript))
    else:
        try:
            info = monitor.latest_session_info()
        except OSError:
            info = None

    state = renderer.DashboardState(status=status, message=message)
    if info:
        state.model = info.model.split("[")[0].strip() or state.model
        state.tool_lines = info.tool_calls[-4:]

    if with_quota:
        q = quota.get_quota()
        if q:
            state.quota_panels.append(_quota_panel("CODEX", q))
        for label, cq in quota.get_coding_quotas():
            state.quota_panels.append(_quota_panel(label, cq))
        rank = quota.panel_order_rank()
        if rank:
            state.quota_panels.sort(key=lambda p: rank.get(p.label, len(rank)))
    return state


# 防抖：这些事件关乎用户响应，不受最小间隔限制；其余事件（stop 等）限频
_PRIORITY_EVENTS = {"notification", "permission", "error", "test"}


def _throttled(event: str) -> int:
    """距上次推送不足 INKSCRY_PUSH_INTERVAL 秒（默认 60）时返回剩余秒数，否则 0。"""
    if event in _PRIORITY_EVENTS:
        return 0
    try:
        interval = int(os.environ.get("INKSCRY_PUSH_INTERVAL", "60"))
    except ValueError:
        interval = 60
    try:
        last = float((quota.CACHE_DIR / "last_push").read_text().strip())
    except (OSError, ValueError):
        return 0
    return max(0, int(interval - (time.time() - last)))


# ── 定时同步（--sync 单次 / --watch 常驻）──────────────────────
# 查额度是电脑侧 HTTP（免费），刷屏是设备侧 BLE + 全刷（三色屏必闪
# 十几秒、毫安级耗电）：高频查、低频刷——屏显数据有变化才值得推送。


def _infer_status() -> str:
    """无 hook 场景从最新会话日志 mtime 推断状态（10 分钟内活跃 → running）。"""
    try:
        latest = monitor.find_latest_session()
        if latest and time.time() - latest.stat().st_mtime < 600:
            return "running"
    except OSError:
        pass
    return "idle"


def _state_signature(state: renderer.DashboardState) -> dict:
    """屏显签名：只收会画到屏上的实质内容，并按屏显精度取整。

    banner 是顶部红色告警横幅（等待确认/出错），必须参与比对——
    否则横幅上屏后一次「数据没变」的跳过会把过期告警永远留在屏上。
    底栏时间戳、状态字不参与——只有实质内容变化才触发全刷，
    状态字的更新搭数据变化的便车。
    """
    return {
        "banner": state.status if state.status in ("waiting", "error") else "",
        # 与 renderer 的 6 面板上限一致：签名只收真正画上屏的面板，
        # 否则被裁掉的面板数据一变就会触发一次画面毫无变化的全刷
        "panels": [{
            "label": p.label,
            "five": None if p.five_pct is None else f"{p.five_pct:.0f}",
            "five_reset": p.five_reset,
            "week": None if p.week_pct is None else f"{p.week_pct:.0f}",
            "week_reset": p.week_reset,
            "balance": p.balance,
            "alert": p.alert,
            "stale": p.stale,
            "bar": None if p.bar_pct is None else f"{p.bar_pct:.0f}",
            "detail": p.detail,
        } for p in state.quota_panels[:6]],
    }


def _last_sig_file() -> Path:
    return quota.CACHE_DIR / "last_pushed_state.json"


def _load_last_sig() -> dict | None:
    """读「屏上内容镜像」：上一次成功推送时的屏显签名。"""
    try:
        return json.loads(_last_sig_file().read_text())
    except (OSError, ValueError):
        return None


def _save_last_sig(sig: dict) -> None:
    quota.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _last_sig_file().write_text(json.dumps(sig, ensure_ascii=False))


# ── 菜单栏（macOS）/ 托盘（Windows、Linux）共用的展示工具 ──────

INTERVAL_CHOICES = [(300, "5 分钟"), (900, "15 分钟"),
                    (1800, "30 分钟"), (3600, "1 小时")]

# 每次推送同时落一张预览图，「查看当前画面」直接打开它
PREVIEW_PNG = quota.CACHE_DIR / "last_push_preview.png"


def _env_interval() -> int:
    try:
        return max(60, int(os.environ.get("INKSCRY_SYNC_INTERVAL", "900")))
    except ValueError:
        return 900


def _panel_line(p: dict) -> str:
    """把一条屏显签名面板（_state_signature 的 panels 元素）排成菜单行。"""
    parts = []
    if p.get("balance"):
        parts.append(p["balance"])
        if p.get("detail"):
            parts.append(p["detail"])
    else:
        if p.get("five") is not None:
            parts.append(f"5时 {p['five']}%")
        if p.get("week") is not None:
            parts.append(f"1周 {p['week']}%")
    body = " · ".join(parts) or "--"
    warn = "⚠ " if p.get("stale") or p.get("alert") else ""
    return f"{warn}{p['label']}  {body}"


def _heartbeat_due() -> bool:
    """INKSCRY_HEARTBEAT（小时，默认 0 关闭）：超时未推则强制刷一次，
    让右下角时间戳保持可信（区分「数据没变」和「同步挂了」）。"""
    try:
        hours = float(os.environ.get("INKSCRY_HEARTBEAT", "0"))
    except ValueError:
        hours = 0.0
    if hours <= 0:
        return False
    try:
        last = float((quota.CACHE_DIR / "last_push").read_text().strip())
    except (OSError, ValueError):
        return True
    return time.time() - last > hours * 3600


def _in_quiet(hour: int) -> bool:
    """INKSCRY_QUIET=23-8 静默时段判断（含起点不含终点，支持跨零点）。"""
    spec = os.environ.get("INKSCRY_QUIET", "")
    if "-" not in spec:
        return False
    try:
        start, end = (int(x) for x in spec.split("-", 1))
    except ValueError:
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


async def sync_once(address: str | None = None, save: str | None = None,
                    no_ble: bool = False, force: bool = False
                    ) -> tuple[str, dict]:
    """同步一轮：查额度 → 与上次已推画面比对 → 有变化才连 BLE 推送。

    force（菜单栏「立即刷新」）跳过防抖与比对，必推。
    返回 (一句话结果, 当前屏显签名)，供菜单栏程序展示。
    """
    if not force:
        skip = _throttled("sync")
        if skip:
            msg = f"{skip}s 内已有推送，本轮让路"
            log.info("同步：%s", msg)
            return msg, {}
    state = build_state("sync", "", {})
    state.status = _infer_status()
    sig = _state_signature(state)
    changed = sig != _load_last_sig()
    if not force and not changed and not _heartbeat_due():
        msg = "数据无变化，未刷屏"
        log.info("同步：%s", msg)
        return msg, sig
    result = renderer.render(state)
    if save:
        result.preview().save(save)
    reason = ("手动刷新" if force
              else "数据有变化" if changed else "心跳强制刷新")
    if no_ble:
        msg = f"检测到{reason}（--no-ble 不推送不记录）"
        log.info("同步：%s", msg)
        return msg, sig
    async with ble.EPDClient(address=address) as epd:
        await epd.push_image(result.black_bytes(), result.red_bytes())
    quota.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (quota.CACHE_DIR / "last_push").write_text(str(time.time()))
    _save_last_sig(sig)
    msg = f"{reason}，已推送"
    log.info("同步：%s", msg)
    return msg, sig


async def watch(args: argparse.Namespace) -> int:
    """常驻程序：周期执行 sync_once，异常不退出（设备不在场下轮重试）。"""
    interval = _env_interval()
    quiet = os.environ.get("INKSCRY_QUIET", "")
    log.info("常驻同步启动：每 %ds 检查一次%s", interval,
             f"，静默时段 {quiet} 点" if quiet else "")
    while True:
        if _in_quiet(time.localtime().tm_hour):
            await asyncio.sleep(300)
            continue
        try:
            await sync_once(address=args.address, save=args.save,
                            no_ble=args.no_ble)
        except Exception as e:   # BLE/网络失败不退出常驻循环
            log.warning("本轮同步失败（下轮重试）: %s", e)
        await asyncio.sleep(interval)


async def run(args: argparse.Namespace) -> int:
    if args.clear or args.sleep:
        async with ble.EPDClient(address=args.address) as epd:
            if args.clear:
                await epd.clear()
                # 屏已空，作废屏上内容镜像：下次比对必判定有变化
                _last_sig_file().unlink(missing_ok=True)
                log.info("清屏指令已发送")
            else:
                await epd.sleep()
                log.info("休眠指令已发送")
        return 0

    # 防抖提前：跳过就不必查额度/渲染（--no-ble 调试与 --demo 不受限）
    if not args.demo and not args.no_ble:
        skip = _throttled(args.event)
        if skip:
            log.info("防抖：距上次推送还剩 %ds，跳过 %s（不影响钩子主流程）",
                     skip, args.event)
            return 0

    if args.demo:
        state = renderer.demo_state()
    else:
        hook = read_hook_stdin()
        state = build_state(args.event, args.message or "", hook,
                            with_quota=not args.no_quota)
        # 普通事件（Stop 等）：数据与告警横幅都和屏上一致就不值得一次
        # 全刷（比对口径与 --sync 相同）；等待确认/出错类必推不比
        if args.event not in _PRIORITY_EVENTS and not args.no_ble:
            if _state_signature(state) == _load_last_sig():
                log.info("屏显数据无变化，跳过 %s 刷屏", args.event)
                return 0

    result = renderer.render(state)
    if args.save:
        result.preview().save(args.save)
        log.info("预览图已保存到 %s", args.save)
    if args.no_ble:
        if not args.save:
            # 缺省预览放系统临时目录，不依赖运行目录、不写进项目
            out = Path(tempfile.gettempdir()) / "inkscry_preview.bmp"
            result.preview().save(out)
            print(f"已生成 {out}（--no-ble 模式）")
        return 0

    async with ble.EPDClient(address=args.address) as epd:
        await epd.push_image(result.black_bytes(), result.red_bytes())
    quota.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (quota.CACHE_DIR / "last_push").write_text(str(time.time()))
    _save_last_sig(_state_signature(state))   # 镜像跟随每一次成功推送
    log.info("推送完成")
    return 0


# Claude Code hook 名 → inkscry 事件名
HOOK_EVENTS = {"Notification": "notification", "Stop": "stop"}


def _hook_command(event: str) -> str:
    """生成 hook 命令：后台执行、静默输出、不阻塞 Claude 主流程。

    Windows 按 cmd 语法（start /b 等价 POSIX 结尾 &；路径双引号）；
    其余按 POSIX 语法（路径单引号，兼容含空格路径）。
    """
    root = Path(__file__).resolve().parent.parent
    if sys.platform == "win32":
        return (f'cd /d "{root}" && start "" /b "{sys.executable}" '
                f"-m inkscry.cli --event {event} >NUL 2>&1")
    return (f"cd '{root}' && '{sys.executable}' "
            f"-m inkscry.cli --event {event} >/dev/null 2>&1 &")


def main() -> int:
    ap = argparse.ArgumentParser(prog="inkscry", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", default="test",
                    choices=list(EVENT_STATUS), help="hook 事件类型")
    ap.add_argument("--message", default="", help="覆盖提示文本")
    ap.add_argument("--address", default=None,
                    help="BLE 地址（缺省读 .env，再缺省按名称扫描）")
    ap.add_argument("--demo", action="store_true", help="使用内置演示数据")
    ap.add_argument("--save", metavar="BMP", help="把渲染结果保存为 BMP")
    ap.add_argument("--no-ble", action="store_true", help="只渲染，不推送")
    ap.add_argument("--no-quota", action="store_true", help="跳过额度查询")
    ap.add_argument("--quota", action="store_true", help="仅打印额度（不刷屏）")
    ap.add_argument("--sync", action="store_true",
                    help="定时同步单次：额度有变化才推送（配合系统定时器）")
    ap.add_argument("--watch", action="store_true",
                    help="常驻定时同步：周期查额度，变化才刷屏")
    ap.add_argument("--clear", action="store_true", help="清屏")
    ap.add_argument("--sleep", action="store_true", help="屏幕休眠")
    ap.add_argument("--print-hooks", action="store_true",
                    help="打印 ~/.claude/settings.json 的 hooks 配置片段")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    # 提前加载 .env：防抖/同步间隔等配置在首次额度查询前就会被读取
    config.load_dotenv()

    if args.print_hooks:
        cfg = {"hooks": {
            name: [{"matcher": "",
                    "hooks": [{"type": "command",
                               "command": _hook_command(event)}]}]
            for name, event in HOOK_EVENTS.items()}}
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        print("\n将以上 \"hooks\" 内容合并进 ~/.claude/settings.json 即可。",
              file=sys.stderr)
        if sys.platform == "win32":
            print("（已按 cmd 语法生成；若 Claude Code 跑在 WSL，"
                  "请在 WSL 里重新执行本命令）", file=sys.stderr)
        return 0

    try:
        if args.quota:
            def show(name: str, q) -> None:
                print(f"[{name}]" + ("（过期缓存）" if q.stale else ""))
                if q.balance:
                    warn = "" if q.available else "  ⚠ 余额不足"
                    print(f"  余额: {q.balance}{warn}")
                    return
                for label, w in (("5h", q.five_h), ("1w", q.one_w)):
                    if w:
                        print(f"  {label}: 剩余 {w.remaining_pct:.1f}%  "
                              f"已用 {w.used_pct:.1f}%  重置 {w.short_reset()}")
                    else:
                        print(f"  {label}: 未找到")

            entries: list[tuple[str, quota.CodexQuota]] = []
            q = quota.get_quota(cache_ttl=0)  # 强制联网刷新
            if q:
                entries.append(("CODEX", q))
            entries += quota.get_coding_quotas(cache_ttl=0)
            rank = quota.panel_order_rank()
            if rank:
                entries.sort(key=lambda e: rank.get(e[0], len(rank)))
            for label, cq in entries:
                show(label, cq)
            if not entries:
                print("未取到额度数据（检查 codex login / 供应商配置 / 网络）")
                return 1
            return 0
        if args.watch:
            return asyncio.run(watch(args))
        if args.sync:
            asyncio.run(sync_once(address=args.address, save=args.save,
                                  no_ble=args.no_ble))
            return 0
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # BLE 失败不应阻塞 Claude 主流程
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
