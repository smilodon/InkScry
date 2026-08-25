"""仪表盘渲染：状态字典 → 400x300 1-bit 位图字节流。

布局：
    顶部告警栏（仅 WAITING/ERROR 时出现，红底白字；平时不占空间）
    中部额度面板区（≤3 分列 / >3 上下分行；无面板时显示最近工具调用）
    底部状态栏（三色屏红底白字：数据过期警示 / 事件状态 + 刷新时间戳）

字体优先级：项目 fonts/ 目录（可放像素字体 .ttf/.otf）→ 系统等宽字体 → PIL 内置。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 400, 300
BYTES_PER_ROW = WIDTH // 8
TITLE_FONT_SIZE = 16        # 面板标题字号
CENTER_CONTENT = False      # 面板内容水平居中（试过不如左对齐，保留开关）

STATUS_LABELS = {
    "running": "运行中",
    "waiting": "等待确认",
    "error": "出错",
    "done": "完成",
    "idle": "空闲",
}

# 需要反色告警的状态
ALERT_STATUSES = {"waiting", "error"}

_FONT_DIRS = [Path(__file__).resolve().parent.parent / "fonts"]
# (路径, ttc face 序号)
_SYSTEM_FONTS = [
    ("/System/Library/Fonts/Menlo.ttc", 0),                       # macOS
    ("/System/Library/Fonts/Monaco.ttf", 0),                      # macOS
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),   # Linux
    ("C:/Windows/Fonts/consola.ttf", 0),                          # Windows
]
_SYSTEM_FONTS_BOLD = [
    ("/System/Library/Fonts/Menlo.ttc", 1),                            # Menlo-Bold
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
    ("C:/Windows/Fonts/consolab.ttf", 0),
]
# 中文字体（状态栏/中文面板标题用；等宽字体无 CJK 字形）
_SYSTEM_FONTS_CJK = [
    ("/System/Library/Fonts/PingFang.ttc", 3),             # macOS 苹方 SC Regular
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),     # macOS 冬青黑体 W3
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),       # macOS 华文黑体
    ("C:/Windows/Fonts/msyh.ttc", 0),                      # Windows 微软雅黑
    ("C:/Windows/Fonts/simhei.ttf", 0),                    # Windows 黑体
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),   # Linux
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
]
# CJK 粗体面（中文面板标题用，与英文标题 Bold 拉齐粗细）
_SYSTEM_FONTS_CJK_BOLD = [
    ("/System/Library/Fonts/PingFang.ttc", 5),             # 苹方 SC Semibold
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),     # 冬青黑体 W6
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),       # 华文黑体（本身偏粗）
    ("C:/Windows/Fonts/msyhbd.ttc", 0),                    # 微软雅黑 Bold
    ("C:/Windows/Fonts/simhei.ttf", 0),                    # 黑体（本身偏粗）
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
]
_font_cache: dict[tuple[int, bool, bool], ImageFont.ImageFont] = {}


def _load_font(size: int, bold: bool = False,
               cjk: bool = False) -> ImageFont.ImageFont:
    key = (size, bold, cjk)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    if not cjk:   # 项目 fonts/ 目录一般是拉丁像素字体，仅非 CJK 时优先
        for d in _FONT_DIRS:
            if d.is_dir():
                files = sorted(d.glob("*.ttf")) + sorted(d.glob("*.otf"))
                if bold:
                    files = [f for f in files if "bold" in f.name.lower()]
                for f in files:
                    try:
                        font = ImageFont.truetype(str(f), size)
                        break
                    except OSError:
                        continue
                if font:
                    break
    if font is None:
        if cjk and bold:
            table = _SYSTEM_FONTS_CJK_BOLD
        elif cjk:
            table = _SYSTEM_FONTS_CJK
        else:
            table = _SYSTEM_FONTS_BOLD if bold else _SYSTEM_FONTS
        for path, idx in table:
            if Path(path).exists():
                try:
                    font = ImageFont.truetype(path, size, index=idx)
                    break
                except OSError:
                    continue
    if font is None:
        # 缺失时逐级降级（无 CJK 字体的系统中文会成方块，但不至于崩溃）
        font = _load_font(size) if (bold or cjk) else ImageFont.load_default(size)
    _font_cache[key] = font
    return font


@dataclass
class RenderedImage:
    """双平面渲染结果：black 为黑白面（1=白 0=黑），red 为红色面（1=红像素）。"""
    black: Image.Image
    red: Image.Image

    def black_bytes(self) -> bytes:
        return to_bitmap_bytes(self.black)

    def red_bytes(self) -> bytes:
        # 红面极性：bit=0 红。直接按位取反字节流
        # （不能用 ImageChops.invert：mode "1" 内部 0/1 标度，反相得 254/255 会失真）
        return bytes(b ^ 0xFF for b in to_bitmap_bytes(self.red))

    def preview(self) -> Image.Image:
        """合成 RGB 预览图（用于本地保存查看）。"""
        rgb = Image.new("RGB", self.black.size, "white")
        # mode "1" 转 "L" 后黑面黑像素=0/红面红像素=255，不能用 ImageChops.invert
        mask_black = self.black.convert("L").point(lambda p: 255 - p)
        rgb.paste("black", mask=mask_black)
        rgb.paste("#D02020", mask=self.red.convert("L"))
        return rgb


@dataclass
class QuotaPanel:
    """一个额度面板（CODEX/KIMI/GLM/…），pct 为剩余百分比。

    balance 非空时进入余额模式（预付费供应商）：忽略 5h/1w，
    显示一栏余额大数字。
    """
    label: str
    five_pct: float | None = None          # 5h 窗口剩余 %
    five_reset: str = ""                   # 紧凑重置时间，如 "15:00"
    week_pct: float | None = None          # 1w 窗口剩余 %
    week_reset: str = ""
    balance: str = ""                      # 余额模式：如 "¥12.34"
    alert: bool = False                    # 余额不足 → 红色告警
    stale: bool = False                    # 数据为断网回退的过期缓存
    bar_pct: float | None = None           # 余额占比条（充值/剩余占比），None 不画
    detail: str = ""                       # 余额备注行：如 "赠 ¥1.00"
    extra_label: str = ""                  # 第三档位（如 Mirasim 的 F周）
    extra_pct: float | None = None
    extra_reset: str = ""


@dataclass
class DashboardState:
    status: str = "idle"                    # running/waiting/error/done/idle
    model: str = ""
    tool_lines: list[str] = field(default_factory=list)   # 最近工具调用
    message: str = ""                       # 挂起/错误提示
    quota_panels: list[QuotaPanel] = field(default_factory=list)
    time_str: str = ""                      # 缺省取当前时间


def _fit(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """按像素宽度截断，超出加省略号。"""
    while len(text) > 3 and d.textlength(text, font=font) > max_w:
        text = text[:-3].rstrip() + "…"
    return text


def _panel_content_h(p: QuotaPanel) -> int:
    """面板内容高度：统一按满配计（标题 26 + 块含底行 44）。用于垂直居中。

    缺失的重置行/备注行保留占位空间，相邻面板无论内容多少都以同一
    高度居中——标题、大数字、进度条跨面板严格对齐（否则少一行的
    面板会整体下沉，如 GLM 窗口未启动时无重置时间）。
    """
    return 70


def _fmt_pct(pct: float, mini: bool = False) -> str:
    """剩余百分比文本：按数据源真实精度显示——小数位非零才带一位小数
    （Codex/Kimi 等上游是整数百分点粒度，挂「.0」是假精度；100 同理
    收敛整数）；mini 档宽度装不下小数，一律整数。"""
    txt = f"{pct:.1f}"
    if mini or txt.endswith(".0"):
        return f"{pct:.0f}%"
    return txt + "%"


def _draw_quota_block(d: ImageDraw.ImageDraw, x0: int, w: int, by: int,
                      label: str, pct: float | None, reset: str,
                      f_sm, tier_w: int | None = None) -> int:
    """单个额度档位（标签 + 大数字 + 进度条 + 重置时间），返回下一块的 y。

    宽度三档：≥150 宽松（26px 数字 + rst 前缀）；<150 紧凑（20px）；
    <80 迷你（16px，每行 3 格的横排子块用）。tier_w 单独指定取档宽度
    （进度条等几何仍按 w），用于独占全宽时和相邻面板字号对齐。
    """
    wide = (tier_w or w) >= 150
    mini = (tier_w or w) < 80
    f_pct = _load_font(26 if wide else (16 if mini else 20))
    f_rst = f_sm if wide else _load_font(10 if mini else 12)
    # 中文标签（5时/1周）走 CJK 字体；等宽字体无 CJK 字形
    cjk_lbl = any(ord(c) > 127 for c in label)
    f_lbl = _load_font(10 if mini else 16, cjk=cjk_lbl)
    # pct_dx 需容纳标签（CJK16=27px / CJK10=17px）并留间隙
    pct_dx, pct_dy = (30, -7) if wide else ((20, -2) if mini else (30, -4))
    # 「标签 + 百分比」作为组合整体居中；进度条保持通栏作视觉锚点
    shift = 0
    if CENTER_CONTENT:
        pct_txt = "--" if pct is None else _fmt_pct(pct, mini)
        group_w = pct_dx + d.textlength(pct_txt, font=f_pct)
        shift = max(0, int((w - group_w) / 2))
    d.text((x0 + shift, by + (2 if mini else 0)), label, font=f_lbl, fill=0)
    if pct is None:
        d.text((x0 + shift + pct_dx, by + pct_dy), "--", font=f_pct, fill=0)
        return by + 44
    d.text((x0 + shift + pct_dx, by + pct_dy), _fmt_pct(pct, mini),
           font=f_pct, fill=0)
    pbar_y = by + (24 if wide else (16 if mini else 20))
    d.rectangle([x0, pbar_y, x0 + w - 6, pbar_y + 8], outline=0)
    fill_w = int((w - 8) * max(0.0, min(100.0, pct)) / 100)
    if fill_w > 0:
        d.rectangle([x0 + 1, pbar_y + 1, x0 + 1 + fill_w, pbar_y + 7], fill=0)
    if reset:
        # mini 档装不下「MM-DD HH:MM」：跨天重置只显示日期部分
        txt = reset.split(" ")[0] if mini and " " in reset else reset
        if wide:
            txt = f"rst {reset}"
        txt = _fit(d, txt, f_rst, w - 4)
        rx = x0
        if CENTER_CONTENT:
            rx = x0 + max(0, int((w - d.textlength(txt, font=f_rst)) / 2))
        d.text((rx, pbar_y + 12), txt, font=f_rst, fill=0)
    return pbar_y + (32 if reset else 24)


def _draw_quota_panel(d: ImageDraw.ImageDraw, dr: ImageDraw.ImageDraw,
                      x0: int, w: int, y0: int, p: QuotaPanel,
                      f_sm, horizontal: bool = False) -> None:
    """额度面板：标题 + 档位块（或余额大数字）。

    horizontal=True 时档位在面板内左右横排（上下分行布局用），
    否则纵向堆叠。标题双面都画：三色屏红色优先盖过黑色 → 显示红字，
    黑白屏忽略红面 → 仍有黑字。
    """
    title = p.label or "QUOTA"
    # 自定义面板名可能含中文：等宽字体无 CJK 字形，切中文字体粗体面
    f_ttl = (_load_font(TITLE_FONT_SIZE, bold=True, cjk=True)
             if any(ord(c) > 127 for c in title)
             else _load_font(TITLE_FONT_SIZE, bold=True))
    tx = x0
    if CENTER_CONTENT:
        tx = x0 + max(0, int((w - d.textlength(title, font=f_ttl)) / 2))
    d.text((tx, y0), title, font=f_ttl, fill=0)
    dr.text((tx, y0), title, font=f_ttl, fill=1)
    d.line([(x0, y0 + TITLE_FONT_SIZE + 3), (x0 + w - 4, y0 + TITLE_FONT_SIZE + 3)], fill=0)
    dr.line([(x0, y0 + TITLE_FONT_SIZE + 3), (x0 + w - 4, y0 + TITLE_FONT_SIZE + 3)], fill=1)
    by = y0 + 26
    if p.stale:
        # 数据过期：旧数字有误导性（如昨天的「100%」），不再展示，
        # 面板亮红色「数据过期」占位（三色屏红字 / 黑白屏黑字），
        # 底栏同时列名指认
        f_exp = _load_font(24, bold=True, cjk=True)
        d.text((x0, by + 6), "数据过期", font=f_exp, fill=0)
        dr.text((x0, by + 6), "数据过期", font=f_exp, fill=1)
        return
    if p.balance:
        # 余额模式与档位块同构三段式：余额+金额行 / 占比条(可选) / 备注行(可选)
        pct_dx = 34   # 容纳「余额」CJK16（27px）并留间隙
        for size in (20, 16):
            f_bal = _load_font(size)
            if d.textlength(p.balance, font=f_bal) <= w - pct_dx - 6:
                break
        ax, ay = x0 + pct_dx, by + (-4 if size == 20 else -2)
        if CENTER_CONTENT:
            group_w = pct_dx + d.textlength(p.balance, font=f_bal)
            ax = x0 + max(0, int((w - group_w) / 2)) + pct_dx
        d.text((ax - pct_dx, by), "余额", font=_load_font(16, cjk=True), fill=0)
        d.text((ax, ay), p.balance, font=f_bal, fill=0)
        if p.alert:
            dr.text((ax, ay), p.balance, font=f_bal, fill=1)
        pbar_y = by + 20
        if p.bar_pct is not None:
            d.rectangle([x0, pbar_y, x0 + w - 6, pbar_y + 8], outline=0)
            fill_w = int((w - 8) * max(0.0, min(100.0, p.bar_pct)) / 100)
            if fill_w > 0:
                d.rectangle([x0 + 1, pbar_y + 1, x0 + 1 + fill_w,
                             pbar_y + 7], fill=0)
        cap = "LOW BALANCE" if p.alert else p.detail
        if cap:
            # 备注可能含中文（如"08-12 用 $3.19"）：含非 ASCII 切中文字体
            f_rst = _load_font(12, cjk=any(ord(c) > 127 for c in cap))
            txt = _fit(d, cap, f_rst, w - 4)
            d.text((x0, pbar_y + 12), txt, font=f_rst, fill=0)
            if p.alert:
                dr.text((x0, pbar_y + 12), txt, font=f_rst, fill=1)
        return
    blocks = [("5时", p.five_pct, p.five_reset),
              ("1周", p.week_pct, p.week_reset)]
    if p.extra_pct is not None:   # 第三档位（如 F周）：三块横排落 mini 档
        blocks.append((p.extra_label or "?", p.extra_pct, p.extra_reset))
    # 缺数据的档位不占位，剩下的档位独占全宽（全缺时保留 -- 占位）
    shown = [b for b in blocks if b[1] is not None] or blocks
    if horizontal:
        bw = (w - 12 * (len(shown) - 1)) // len(shown)
        # 单档独占全宽时字号仍按双档格宽取档，与相邻双档面板一致
        tier_w = (w - 12) // 2 if len(shown) == 1 else None
        for i, (label, pct, reset) in enumerate(shown):
            _draw_quota_block(d, x0 + i * (bw + 12), bw, by, label, pct,
                              reset, f_sm, tier_w=tier_w)
    else:
        for label, pct, reset in shown:
            by = _draw_quota_block(d, x0, w, by, label, pct, reset, f_sm)


def render(state: DashboardState, width: int = WIDTH, height: int = HEIGHT) -> RenderedImage:
    img = Image.new("1", (width, height), 1)   # 白底（黑白面）
    red = Image.new("1", (width, height), 0)   # 无红（红色面）
    d = ImageDraw.Draw(img)
    dr = ImageDraw.Draw(red)
    f_sm, f_md, f_lg = _load_font(14), _load_font(16), _load_font(20)

    alert = state.status.lower() in ALERT_STATUSES
    status_text = STATUS_LABELS.get(state.status.lower(), state.status.upper())
    time_str = state.time_str or datetime.datetime.now().strftime("%H:%M")

    # ── 顶部告警栏：仅 WAITING/ERROR 时出现（红底白字），平时不占空间 ──
    if alert:
        bar_h = 34
        f_alert = _load_font(20, cjk=True)
        dr.rectangle([0, 0, width, bar_h], fill=1)     # 红底
        d.text((8, 6), f"! {status_text}", font=f_alert, fill=1)   # 白字：黑面留空
        dr.text((8, 6), f"! {status_text}", font=f_alert, fill=0)  #      红面镂空
        mid_top_base = bar_h + 10
    else:
        mid_top_base = 8

    # ── 中部：额度面板（≤2 分列，≥3 网格）──────
    foot_h = 26
    line_h = 20
    mid_top = mid_top_base
    if state.message:
        mid_bottom = height - foot_h - line_h - 8   # 给消息条留位
    else:
        mid_bottom = height - foot_h - 6

    panels = state.quota_panels[:6]
    half = width // 2
    # 垂直居中的可视格底：末行/分列取底栏顶边，否则内容视觉上浮
    cell_floor = height - foot_h if not state.message else mid_bottom + 4
    if len(panels) == 1:
        # 单面板占左半（档位横排一行，垂直居中），右半画 "+" 占位
        p = panels[0]
        y0 = mid_top + max(0, (cell_floor - mid_top
                               - _panel_content_h(p)) // 2)
        _draw_quota_panel(d, dr, 10, half - 18, y0, p, f_sm,
                          horizontal=True)
        d.line([(half, mid_top), (half, mid_bottom)], fill=0)
        f_plus = _load_font(26)
        pw = d.textlength("+", font=f_plus)
        cx = half + int((half - pw) / 2)
        cy = mid_top + (cell_floor - mid_top - 26) // 2
        d.text((cx, cy), "+", font=f_plus, fill=0)
    elif len(panels) == 2:
        col_w = width // len(panels)
        for i, p in enumerate(panels):
            y0 = mid_top + max(0, (cell_floor - mid_top
                                   - _panel_content_h(p)) // 2)
            _draw_quota_panel(d, dr, i * col_w + 8, col_w - 14, y0,
                              p, f_sm, horizontal=True)
        for i in range(1, len(panels)):
            d.line([(i * col_w, mid_top), (i * col_w, mid_bottom)], fill=0)
    elif panels:
        # 3 个及以上：2 列 × ⌈n/2⌉ 行网格（奇数个时末槽画 "+"），
        # 面板内 5h/1w 左右横排；面板内容在行内垂直居中
        cols = 2
        rows = (len(panels) + 1) // 2
        col_w = width // cols
        row_h = (mid_bottom - mid_top) // rows
        for i, p in enumerate(panels):
            r, c = divmod(i, cols)
            # 按实际内容高度在行内垂直居中；末行格底取底栏顶边，
            # 否则末行内容视觉上浮
            ch = _panel_content_h(p)
            cell_top = mid_top + r * row_h
            cell_bottom = (mid_top + (r + 1) * row_h if r < rows - 1
                           else cell_floor)
            y0 = cell_top + max(0, (cell_bottom - cell_top - ch) // 2)
            _draw_quota_panel(d, dr, c * col_w + 8, col_w - 14,
                              y0, p, f_sm, horizontal=True)
        d.line([(col_w, mid_top), (col_w, mid_bottom)], fill=0)   # 列分隔竖线
        for r in range(1, rows):                                  # 行分隔横线
            d.line([(0, mid_top + r * row_h),
                    (width, mid_top + r * row_h)], fill=0)
        # 奇数个面板时末槽画 "+" 占位（提示还能再加一个面板）
        if len(panels) % 2 == 1:
            f_plus = _load_font(26)
            pw = d.textlength("+", font=f_plus)
            cx = col_w + int((col_w - pw) / 2)
            cell_top = mid_top + (rows - 1) * row_h
            cy = cell_top + (cell_floor - cell_top - 26) // 2
            d.text((cx, cy), "+", font=f_plus, fill=0)

    # 工具行：仅无面板时占满全宽显示（单面板右半与网格末槽均为 "+" 占位）
    if not panels:
        y = mid_top + 4
        for line in state.tool_lines[:6]:
            if y + line_h > mid_bottom:
                break
            while len(line) > 3 and d.textlength(line, font=f_md) > width - 20:
                line = line[:-3] + "…"
            d.text((10, y), line, font=f_md, fill=0)
            y += line_h

    if state.message:
        msg = state.message
        max_w = width - 20
        while msg and d.textlength(msg, font=f_md) > max_w:
            msg = msg[:-2]
        if msg != state.message:
            msg = msg.rstrip() + "…" if msg else ""
        if msg:
            msg_y = mid_bottom + 4
            d.rectangle([6, msg_y - 3, 14 + d.textlength(msg, font=f_md),
                         msg_y + line_h - 1], fill=0)
            d.text((10, msg_y), msg, font=f_md, fill=1)

    # ── 底部状态栏（三色屏红底白字，黑白屏黑底白字；时钟右角 = 刷新时间戳）──
    # 左侧优先级：过期数据警示 > 事件状态。文字画法同顶部告警栏：
    # 黑面白字 + 红面镂空，三色屏红色盖过黑底显示红条
    d.rectangle([0, height - foot_h, width, height], fill=0)
    dr.rectangle([0, height - foot_h, width, height], fill=1)
    tw = int(d.textlength(time_str, font=f_sm))
    d.text((width - tw - 8, height - foot_h + 5), time_str, font=f_sm, fill=1)
    dr.text((width - tw - 8, height - foot_h + 5), time_str, font=f_sm, fill=0)
    foot_y = height - foot_h + 4
    f_foot = _load_font(14, cjk=True)
    stale = [p.label for p in panels if p.stale]
    txt = status_text
    if stale:
        txt = _fit(d, "数据过期: " + " ".join(stale), f_foot, width - 26 - tw)
    d.text((8, foot_y), txt, font=f_foot, fill=1)
    dr.text((8, foot_y), txt, font=f_foot, fill=0)

    return RenderedImage(img, red)


def to_bitmap_bytes(img: Image.Image) -> bytes:
    """PIL 1-bit 图 → 固件显存字节流（行主序，MSB 先，白=1/黑=0，与 PIL 一致）。"""
    if img.mode != "1":
        img = img.convert("1")
    if img.width % 8 != 0:
        raise ValueError(f"宽度必须是 8 的倍数，当前 {img.width}")
    return img.tobytes()


def render_bytes(state: DashboardState) -> bytes:
    """仅黑白面字节流（兼容旧用法/测试）。"""
    return render(state).black_bytes()


def demo_state() -> DashboardState:
    return DashboardState(
        status="waiting",
        model="Claude Fable 5",
        tool_lines=[
            "> git status",
            '> execute_bash(command="npm run build")',
            "> edit_file(renderer.py)",
        ],
        message="[Pending] Waiting for user confirmation...",
        quota_panels=[
            QuotaPanel("CODEX", 87.0, "15:00", 92.0, "08-24 09:00"),
            QuotaPanel("KIMI", 90.0, "16:03", 34.0, "08-20 16:03"),
            QuotaPanel("GLM", 62.0, "17:30", 78.0, "08-22 10:00"),
        ],
    )


if __name__ == "__main__":
    import tempfile

    # 调试产物放系统临时目录：不依赖运行目录，也避免写进项目（Dropbox 同步）
    out = Path(tempfile.gettempdir()) / "inkscry_demo.bmp"
    render(demo_state()).preview().save(out)
    print(f"已生成 {out}")
