"""macOS 菜单栏常驻程序：定时同步墨水屏，变化才刷。

菜单栏常年一个「墨」字（同步中「墨…」、失败「墨!」）；下拉菜单
显示屏上各面板数据与上次同步结果，并提供管理项：

    立即刷新            跳过防抖与比对，必推
    暂停/恢复自动同步
    同步间隔 ▸          5/15/30/60 分钟（运行期生效；默认值在 .env）
    屏幕管理 ▸          查看当前画面 / 清屏（并暂停同步）
    配置 ▸              编辑 .env… / 重载配置（改面板顺序、标签后点它）

同步节奏与 `inkscry --watch` 一致：INKSCRY_SYNC_INTERVAL /
INKSCRY_QUIET / INKSCRY_HEARTBEAT。

依赖 rumps（仅 macOS）：pip install '.[menubar]'
入口：inkscry-bar 或 python -m inkscry.menubar
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

from . import ble, cli, config, quota

try:
    import rumps
except ImportError:  # 非 macOS 或未装可选依赖
    rumps = None

log = logging.getLogger("inkscry.bar")

TITLE = "墨"
TITLE_BUSY = "墨…"
TITLE_ERROR = "墨!"


class InkScryBar(rumps.App if rumps else object):
    def __init__(self) -> None:
        super().__init__(TITLE, quit_button=None)
        quota.CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # 常驻菜单项只建一次，跨 _rebuild 复用（保住回调与勾选态）
        self.refresh_item = rumps.MenuItem("立即刷新", callback=self.on_refresh)
        self.pause_item = rumps.MenuItem("暂停自动同步", callback=self.on_pause)
        self.interval_items = {
            sec: rumps.MenuItem(label,
                                callback=lambda _i, s=sec: self._set_interval(s))
            for sec, label in cli.INTERVAL_CHOICES}
        self.interval_menu = rumps.MenuItem("同步间隔")
        self.interval_menu.update(list(self.interval_items.values()))
        self.screen_menu = rumps.MenuItem("屏幕管理")
        self.screen_menu.update([
            rumps.MenuItem("查看当前画面", callback=self.on_preview),
            rumps.MenuItem("清屏（并暂停同步）", callback=self.on_clear),
        ])
        self.config_menu = rumps.MenuItem("配置")
        self.config_menu.update([
            rumps.MenuItem("编辑 .env…", callback=self.on_edit_env),
            rumps.MenuItem("重载配置", callback=self.on_reload),
        ])

        self.paused = False
        self._busy = threading.Lock()
        # (消息, 签名|None, 出错?, 附加动作|None)，工作线程写、主线程消费
        self._pending: tuple[str, dict | None, bool, str | None] | None = None
        # 启动即从「屏上内容镜像」恢复面板行，不必等第一轮同步
        data = cli._load_last_sig()
        if isinstance(data, list):   # 兼容旧格式（纯面板列表）
            data = {"panels": data}
        self._last_sig: dict = data if isinstance(data, dict) else {}
        self._rebuild("启动中…")

        self._sync_timer: rumps.Timer | None = None
        self._set_interval(cli._env_interval())   # 建定时器；NSTimer 启动即触发首轮
        rumps.Timer(self._apply_pending, 1).start()

    # ── 菜单动作（主线程）─────────────────────────────────
    def on_tick(self, _timer) -> None:
        if self.paused:
            return
        if cli._in_quiet(time.localtime().tm_hour):
            self._rebuild(f"{time.strftime('%H:%M')} 静默时段，暂不同步")
            return
        self._spawn(self._sync_work, force=False)

    def on_refresh(self, _item) -> None:
        self._spawn(self._sync_work, force=True)

    def on_pause(self, item) -> None:
        self.paused = not self.paused
        item.title = "恢复自动同步" if self.paused else "暂停自动同步"
        if self.paused:
            self._rebuild("已暂停自动同步")
        else:
            self._rebuild("已恢复自动同步")
            self.on_tick(None)

    def _set_interval(self, seconds: int) -> None:
        for sec, item in self.interval_items.items():
            item.state = 1 if sec == seconds else 0
        if self._sync_timer is not None:
            self._sync_timer.stop()
        self._sync_timer = rumps.Timer(self.on_tick, seconds)
        self._sync_timer.start()   # 启动即触发一轮，再按新间隔重复

    def on_preview(self, _item) -> None:
        if cli.PREVIEW_PNG.exists():
            subprocess.Popen(["open", str(cli.PREVIEW_PNG)])
        else:
            self._rebuild(f"{time.strftime('%H:%M')} 尚无画面缓存，"
                          "请先「立即刷新」")

    def on_clear(self, _item) -> None:
        self._spawn(self._clear_work)

    def on_edit_env(self, _item) -> None:
        env = config.PROJECT_ROOT / ".env"
        if not env.exists():
            self._rebuild(f"{time.strftime('%H:%M')} 未找到 .env"
                          "（先 cp .env.example .env）")
            return
        subprocess.Popen(["open", "-t", str(env)])

    def on_reload(self, _item) -> None:
        """清掉进程内 INKSCRY_* 后重读 .env（loader 是 setdefault 语义），
        面板顺序/标签/token 等改动随下一轮同步生效。"""
        for k in [k for k in os.environ if k.startswith("INKSCRY_")]:
            del os.environ[k]
        config.load_dotenv()
        self._rebuild(f"{time.strftime('%H:%M')} 配置已重载")
        self._set_interval(cli._env_interval())   # 顺带按新配置重建定时器并同步一轮

    def _spawn(self, work, **kwargs) -> None:
        if not self._busy.acquire(blocking=False):
            return   # 上一个 BLE 任务还没跑完
        self.title = TITLE_BUSY
        threading.Thread(target=work, kwargs=kwargs, daemon=True).start()

    # ── 工作线程（BLE + 网络，可能十几秒）──────────────────
    def _sync_work(self, force: bool) -> None:
        stamp = time.strftime("%H:%M")
        try:
            msg, sig = asyncio.run(cli.sync_once(save=str(cli.PREVIEW_PNG),
                                                 force=force))
            self._pending = (f"{stamp} {msg}", sig or None, False, None)
        except Exception as e:   # 设备不在场/断网：显示失败，下轮重试
            self._pending = (f"{stamp} 同步失败: {e}", None, True, None)
        finally:
            self._busy.release()

    def _clear_work(self) -> None:
        stamp = time.strftime("%H:%M")
        try:
            asyncio.run(self._do_clear())
            # 屏已空，作废「屏上内容镜像」：恢复同步后第一轮必重推
            cli._last_sig_file().unlink(missing_ok=True)
            self._pending = (f"{stamp} 已清屏，自动同步已暂停", None, False,
                             "cleared")
        except Exception as e:
            self._pending = (f"{stamp} 清屏失败: {e}", None, True, None)
        finally:
            self._busy.release()

    @staticmethod
    async def _do_clear() -> None:
        async with ble.EPDClient() as epd:
            await epd.clear()

    # ── AppKit 要求 UI 只在主线程改：1s 定时器消费 _pending ──
    def _apply_pending(self, _timer) -> None:
        if self._pending is None:
            return
        msg, sig, error, extra = self._pending
        self._pending = None
        if sig:
            self._last_sig = sig
        if extra == "cleared":
            self.paused = True
            self.pause_item.title = "恢复自动同步"
            self._last_sig = {}
        self._rebuild(msg)
        self.title = TITLE_ERROR if error else TITLE

    def _rebuild(self, status: str) -> None:
        items: list = [rumps.MenuItem(status)]
        panels = self._last_sig.get("panels") if self._last_sig else None
        if panels:
            items.append(None)
            for p in panels:
                items.append(rumps.MenuItem(cli._panel_line(p)))
        items += [None, self.refresh_item, self.pause_item,
                  None, self.interval_menu, self.screen_menu, self.config_menu,
                  None, rumps.MenuItem("退出", callback=rumps.quit_application)]
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
