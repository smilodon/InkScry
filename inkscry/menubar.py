"""macOS 菜单栏常驻程序：定时同步墨水屏，变化才刷。

菜单栏常年一个「墨」字（同步中「墨…」、失败「墨!」）；下拉菜单
显示屏上各面板数据与上次同步结果，可「立即刷新」（跳过防抖与
比对必推）或暂停自动同步。同步节奏与 `inkscry --watch` 一致：
INKSCRY_SYNC_INTERVAL / INKSCRY_QUIET / INKSCRY_HEARTBEAT。

依赖 rumps（仅 macOS）：pip install '.[menubar]'
入口：inkscry-bar 或 python -m inkscry.menubar
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time

from . import cli, config, quota

try:
    import rumps
except ImportError:  # 非 macOS 或未装可选依赖
    rumps = None

log = logging.getLogger("inkscry.bar")

TITLE = "墨"
TITLE_BUSY = "墨…"
TITLE_ERROR = "墨!"


def _panel_line(p: dict) -> str:
    """把一条屏显签名（cli._state_signature 的元素）排成菜单行。"""
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


class InkScryBar(rumps.App if rumps else object):
    def __init__(self) -> None:
        super().__init__(TITLE, quit_button=None)
        self.refresh_item = rumps.MenuItem("立即刷新", callback=self.on_refresh)
        self.pause_item = rumps.MenuItem("暂停自动同步", callback=self.on_pause)
        self.paused = False
        self._busy = threading.Lock()
        self._pending: tuple[str, list[dict] | None, bool] | None = None
        # 启动即从「屏上内容镜像」恢复面板行，不必等第一轮同步
        try:
            self._last_sig = json.loads(
                (quota.CACHE_DIR / "last_pushed_state.json").read_text())
        except (OSError, ValueError):
            self._last_sig = []
        self._rebuild("启动中…")
        try:
            interval = max(60, int(os.environ.get("INKSCRY_SYNC_INTERVAL",
                                                  "900")))
        except ValueError:
            interval = 900
        # NSTimer 启动即触发首轮，之后按间隔重复——无需手动先调一次
        rumps.Timer(self.on_tick, interval).start()
        rumps.Timer(self._apply_pending, 1).start()

    # ── 菜单动作（主线程）─────────────────────────────────
    def on_tick(self, _timer) -> None:
        if self.paused:
            return
        if cli._in_quiet(time.localtime().tm_hour):
            self._rebuild(f"{time.strftime('%H:%M')} 静默时段，暂不同步")
            return
        self._spawn(force=False)

    def on_refresh(self, _item) -> None:
        self._spawn(force=True)

    def on_pause(self, item) -> None:
        self.paused = not self.paused
        item.title = "恢复自动同步" if self.paused else "暂停自动同步"
        if self.paused:
            self._rebuild("已暂停自动同步")

    def _spawn(self, force: bool) -> None:
        if not self._busy.acquire(blocking=False):
            return   # 上一轮还没跑完
        self.title = TITLE_BUSY
        threading.Thread(target=self._work, args=(force,),
                         daemon=True).start()

    # ── 同步工作线程（BLE + 网络，可能十几秒）──────────────
    def _work(self, force: bool) -> None:
        stamp = time.strftime("%H:%M")
        try:
            msg, sig = asyncio.run(cli.sync_once(force=force))
            self._pending = (f"{stamp} {msg}", sig or None, False)
        except Exception as e:   # 设备不在场/断网：显示失败，下轮重试
            self._pending = (f"{stamp} 同步失败: {e}", None, True)
        finally:
            self._busy.release()

    # ── AppKit 要求 UI 只在主线程改：1s 定时器消费 _pending ──
    def _apply_pending(self, _timer) -> None:
        if self._pending is None:
            return
        msg, sig, error = self._pending
        self._pending = None
        if sig:
            self._last_sig = sig
        self._rebuild(msg)
        self.title = TITLE_ERROR if error else TITLE

    def _rebuild(self, status: str) -> None:
        items: list = [rumps.MenuItem(status)]
        if self._last_sig:
            items.append(None)
            for p in self._last_sig:
                items.append(rumps.MenuItem(_panel_line(p)))
        items += [None, self.refresh_item, self.pause_item, None,
                  rumps.MenuItem("退出", callback=rumps.quit_application)]
        self.menu.clear()
        self.menu.update(items)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if sys.platform != "darwin":
        print("菜单栏程序仅支持 macOS；其他平台请用 inkscry --watch",
              file=sys.stderr)
        return 1
    if rumps is None:
        print("缺少 rumps：pip install '.[menubar]'", file=sys.stderr)
        return 1
    config.load_dotenv()
    InkScryBar().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
