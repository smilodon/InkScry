"""Claude Code 会话日志解析（M2）。

轮询 ~/.claude/projects/ 下最新的 .jsonl 会话文件，提取：
    最近工具调用 / 模型 / Token 用量 / 费用估算 / 会话时长
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 每百万 token 价格 (USD)：input / output / cache_write / cache_read
PRICING = {
    "opus":   (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku":  (1.0, 5.0, 1.25, 0.10),
}
DEFAULT_PRICING = PRICING["sonnet"]


@dataclass
class SessionInfo:
    model: str = ""
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    total_tokens: int = 0
    duration_min: float | None = None
    file: Path | None = None


def find_latest_session(projects_dir: Path = PROJECTS_DIR) -> Path | None:
    files = sorted(projects_dir.glob("*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _pricing_for(model: str) -> tuple[float, float, float, float]:
    m = model.lower()
    for key, prices in PRICING.items():
        if key in m:
            return prices
    return DEFAULT_PRICING


def _summarize_tool(name: str, tool_input: dict) -> str:
    for key in ("command", "file_path", "pattern", "path", "url", "query"):
        if key in tool_input:
            val = str(tool_input[key]).replace("\n", " ")
            return f"> {name}({_shorten(val, 40)})"
    return f"> {name}"


def _shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_session(path: Path, tail_lines: int = 400) -> SessionInfo:
    info = SessionInfo(file=path)
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    try:
        with open(path, "rb") as f:
            lines = f.readlines()[-tail_lines:]
    except OSError:
        return info

    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = _parse_ts(entry.get("timestamp", ""))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        msg = entry.get("message") or {}
        if not isinstance(msg, dict):
            continue
        if msg.get("model"):
            info.model = msg["model"]

        usage = msg.get("usage")
        if isinstance(usage, dict):
            prices = _pricing_for(info.model)
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cw = usage.get("cache_creation_input_tokens", 0)
            cr = usage.get("cache_read_input_tokens", 0)
            info.input_tokens += inp
            info.output_tokens += out
            info.cache_write_tokens += cw
            info.cache_read_tokens += cr
            info.cost_usd += (inp * prices[0] + out * prices[1]
                              + cw * prices[2] + cr * prices[3]) / 1_000_000

        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    info.tool_calls.append(
                        _summarize_tool(block.get("name", "?"),
                                        block.get("input") or {}))

    info.total_tokens = (info.input_tokens + info.output_tokens
                         + info.cache_write_tokens + info.cache_read_tokens)
    if first_ts and last_ts and last_ts > first_ts:
        info.duration_min = (last_ts - first_ts).total_seconds() / 60
    return info


def latest_session_info() -> SessionInfo | None:
    path = find_latest_session()
    return parse_session(path) if path else None
