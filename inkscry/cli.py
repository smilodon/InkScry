"""InkScry CLI 入口：接收 Claude Code Hook 事件 → 渲染 → 推送墨水屏。

用法示例:
    cli.py --event notification          # 从 stdin 读取 hook JSON
    cli.py --demo --save demo.bmp        # 仅渲染示例图，不发蓝牙
    cli.py --demo                        # 渲染示例图并推送到墨水屏
    cli.py --clear / --sleep             # 清屏 / 休眠
    cli.py --print-hooks                 # 输出 settings.json hooks 配置
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

from . import ble, monitor, quota, renderer

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


async def run(args: argparse.Namespace) -> int:
    if args.clear or args.sleep:
        async with ble.EPDClient(address=args.address) as epd:
            if args.clear:
                await epd.clear()
                log.info("清屏指令已发送")
            else:
                await epd.sleep()
                log.info("休眠指令已发送")
        return 0

    if args.demo:
        state = renderer.demo_state()
    else:
        hook = read_hook_stdin()
        state = build_state(args.event, args.message or "", hook,
                            with_quota=not args.no_quota)

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
    ap.add_argument("--clear", action="store_true", help="清屏")
    ap.add_argument("--sleep", action="store_true", help="屏幕休眠")
    ap.add_argument("--print-hooks", action="store_true",
                    help="打印 ~/.claude/settings.json 的 hooks 配置片段")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

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
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # BLE 失败不应阻塞 Claude 主流程
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
