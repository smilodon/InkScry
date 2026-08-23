"""系统托盘常驻程序（Windows / Linux）：定时同步墨水屏，变化才刷。

功能与 macOS 的 inkscry-bar 对齐：托盘图标是 Pillow 渲染的「墨」字
（白字黑描边，同步中变灰、失败变红），菜单提供面板数据一览、
立即刷新、暂停/恢复、同步间隔、屏幕管理（查看当前画面/清屏）、
配置（编辑 .env/重载）。同步节奏同 `inkscry --watch`：
INKSCRY_SYNC_INTERVAL / INKSCRY_QUIET / INKSCRY_HEARTBEAT。

依赖 pystray：pip install '.[tray]'
入口：inkscry-tray 或 python -m inkscry.tray
（macOS 也能跑，但建议用原生体验更好的 inkscry-bar）
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw

from . import ble, cli, config, renderer

try:
    import pystray
except ImportError:
    pystray = None

log = logging.getLogger("inkscry.tray")

ICON_SIZE = 64
# 白字黑描边：浅色/深色任务栏都可见；同步中灰、失败红
STATE_COLORS = {"normal": (255, 255, 255), "busy": (150, 150, 150),
                "error": (230, 60, 60)}


def _make_icon(rgb: tuple[int, int, int]) -> Image.Image:
    """用 Pillow 把「墨」字渲染成托盘图标（透明底）。"""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = renderer._load_font(52, bold=True, cjk=True)
    left, top, right, bottom = d.textbbox((0, 0), "墨", font=font)
    d.text(((ICON_SIZE - (right - left)) / 2 - left,
            (ICON_SIZE - (bottom - top)) / 2 - top),
           "墨", font=font, fill=rgb + (255,),
           stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return img


def _open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)   # noqa: 仅 win32 存在
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class InkScryTray:
    def __init__(self) -> None:
        self.paused = False
        self.status = "启动中…"
        self._busy = threading.Lock()
        self._stopped = False
        self._wake = threading.Event()   # set() 立刻结束本轮等待
        self._interval = cli._env_interval()
        data = cli._load_last_sig()
        if isinstance(data, list):   # 兼容旧格式（纯面板列表）
            data = {"panels": data}
        self._last_sig: dict = data if isinstance(data, dict) else {}
        self.icons = {k: _make_icon(c) for k, c in STATE_COLORS.items()}
        self.icon = pystray.Icon("inkscry", self.icons["normal"], "InkScry",
                                 menu=pystray.Menu(self._menu_items))

    # ── 菜单（动态生成：每次展开/update_menu 时重新求值）────
    def _menu_items(self):
        item = pystray.MenuItem
        yield item(self.status, None, enabled=False)
        panels = self._last_sig.get("panels") or []
        if panels:
            yield pystray.Menu.SEPARATOR
            for p in panels:
                yield item(cli._panel_line(p), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield item("立即刷新", self.on_refresh)
        yield item("恢复自动同步" if self.paused else "暂停自动同步",
                   self.on_pause)
        yield pystray.Menu.SEPARATOR
        yield item("同步间隔", pystray.Menu(
            *[item(label, self._interval_setter(sec), radio=True,
                   checked=self._interval_checked(sec))
              for sec, label in cli.INTERVAL_CHOICES]))
        yield item("屏幕管理", pystray.Menu(
            item("查看当前画面", self.on_preview),
            item("清屏（并暂停同步）", self.on_clear)))
        yield item("配置", pystray.Menu(
            item("编辑 .env…", self.on_edit_env),
            item("重载配置", self.on_reload)))
        yield pystray.Menu.SEPARATOR
        yield item("退出", self.on_quit)

    def _interval_setter(self, sec: int):
        def do(_icon, _item):
            self._interval = sec
            self._wake.set()   # 立刻按新间隔同步一轮
        return do

    def _interval_checked(self, sec: int):
        return lambda _item: self._interval == sec

    # ── 菜单动作 ────────────────────────────────────────────
    def on_refresh(self, _icon, _item) -> None:
        self._spawn(self._sync_work, force=True)

    def on_pause(self, _icon, _item) -> None:
        self.paused = not self.paused
        if self.paused:
            self._set_status("已暂停自动同步")
        else:
            self._set_status("已恢复自动同步")
            self._wake.set()   # 恢复后立刻补一轮
        self.icon.update_menu()

    def on_preview(self, _icon, _item) -> None:
        if cli.PREVIEW_PNG.exists():
            _open_path(str(cli.PREVIEW_PNG))
        else:
            self._set_status(f"{time.strftime('%H:%M')} 尚无画面缓存，"
                             "请先「立即刷新」")

    def on_clear(self, _icon, _item) -> None:
        self._spawn(self._clear_work)

    def on_edit_env(self, _icon, _item) -> None:
        env = config.PROJECT_ROOT / ".env"
        if not env.exists():
            self._set_status(f"{time.strftime('%H:%M')} 未找到 .env"
                             "（先 cp .env.example .env）")
            return
        if sys.platform == "win32":   # .env 无关联程序，直接记事本
            subprocess.Popen(["notepad", str(env)])
        else:
            _open_path(str(env))

    def on_reload(self, _icon, _item) -> None:
        """清掉进程内 INKSCRY_* 后重读 .env，下一轮同步生效。"""
        for k in [k for k in os.environ if k.startswith("INKSCRY_")]:
            del os.environ[k]
        config.load_dotenv()
        self._interval = cli._env_interval()
        self._set_status(f"{time.strftime('%H:%M')} 配置已重载")
        self._wake.set()   # 按新配置立刻同步一轮

    def on_quit(self, icon, _item) -> None:
        self._stopped = True
        self._wake.set()
        icon.stop()

    # ── 同步调度（后台线程；_wake 可提前打断等待）───────────
    def _scheduler(self) -> None:
        while not self._stopped:
            if not self.paused:
                if cli._in_quiet(time.localtime().tm_hour):
                    self._set_status(f"{time.strftime('%H:%M')} "
                                     "静默时段，暂不同步")
                else:
                    self._spawn(self._sync_work, force=False)
            self._wake.wait(self._interval)
            self._wake.clear()

    def _spawn(self, work, **kwargs) -> None:
        if not self._busy.acquire(blocking=False):
            return   # 上一个 BLE 任务还没跑完
        self.icon.icon = self.icons["busy"]
        threading.Thread(target=work, kwargs=kwargs, daemon=True).start()

    def _sync_work(self, force: bool) -> None:
        stamp = time.strftime("%H:%M")
        try:
            msg, sig = asyncio.run(cli.sync_once(save=str(cli.PREVIEW_PNG),
                                                 force=force))
            if sig:
                self._last_sig = sig
            self._finish(f"{stamp} {msg}", error=False)
        except Exception as e:   # 设备不在场/断网：显示失败，下轮重试
            self._finish(f"{stamp} 同步失败: {e}", error=True)
        finally:
            self._busy.release()

    def _clear_work(self) -> None:
        stamp = time.strftime("%H:%M")
        try:
            asyncio.run(self._do_clear())
            # 屏已空，作废「屏上内容镜像」：恢复同步后第一轮必重推
            cli._last_sig_file().unlink(missing_ok=True)
            self._last_sig = {}
            self.paused = True
            self._finish(f"{stamp} 已清屏，自动同步已暂停", error=False)
        except Exception as e:
            self._finish(f"{stamp} 清屏失败: {e}", error=True)
        finally:
            self._busy.release()

    @staticmethod
    async def _do_clear() -> None:
        async with ble.EPDClient() as epd:
            await epd.clear()

    # ── 状态更新（pystray 的 update_menu/图标赋值可跨线程）──
    def _set_status(self, text: str) -> None:
        self.status = text
        self.icon.update_menu()

    def _finish(self, msg: str, error: bool) -> None:
        self.icon.icon = self.icons["error" if error else "normal"]
        self._set_status(msg)

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._scheduler, daemon=True).start()
        self.icon.run(setup=setup)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if pystray is None:
        print("缺少 pystray：pip install '.[tray]'", file=sys.stderr)
        return 1
    if sys.platform == "darwin":
        print("提示：macOS 建议用 inkscry-bar（原生菜单栏体验更好）",
              file=sys.stderr)
    config.load_dotenv()
    InkScryTray().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
