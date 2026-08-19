"""EPD-nRF5 BLE 协议常量与封包逻辑。

协议参考: github.com/tsl0922/EPD-nRF5 网页客户端 (html/js/main.js, rle.js)
"""

from __future__ import annotations

# GATT
SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"
CHAR_UUID = "62750002-d828-918d-fb46-b6c11c675aec"  # Write + Notify
VERSION_UUID = "62750003-d828-918d-fb46-b6c11c675aec"


class EpdCmd:
    SET_PINS = 0x00
    INIT = 0x01
    CLEAR = 0x02
    SEND_CMD = 0x03
    SEND_DATA = 0x04
    REFRESH = 0x05
    SLEEP = 0x06
    SET_TIME = 0x20
    WRITE_IMG = 0x30
    SET_CONFIG = 0x90
    SYS_RESET = 0x91
    SYS_SLEEP = 0x92
    CFG_ERASE = 0x99


# ---------------------------------------------------------------- RLE 压缩
# 与固件端解码器严格一致的 PackBits 变体:
#   重复段: [0x80 | (run_len - 3), byte]        run_len ∈ [3, 130]
#   字面段: [literal_len - 1, ...bytes]          literal_len ∈ [1, 128]

def rle_compress(data: bytes, max_literal: int = 128) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and run < 130 and data[i + run] == data[i]:
            run += 1
        if run >= 3:
            out.append(0x80 | (run - 3))
            out.append(data[i])
            i += run
        else:
            start = i
            lit = 0
            while i < n and lit < max_literal:
                if i + 2 < n and data[i] == data[i + 1] == data[i + 2]:
                    break
                lit += 1
                i += 1
            if lit == 0:
                out.append(0x00)
                out.append(data[i])
                i += 1
            else:
                out.append(lit - 1)
                out += data[start:start + lit]
    return bytes(out)


def rle_compress_mtu(data: bytes, max_chunk: int) -> list[bytes]:
    """RLE 压缩并按 BLE 包大小切分，保证不拆散任何一条编码。"""
    max_lit = min(max_chunk - 1, 128)
    comp = rle_compress(data, max_lit)
    chunks: list[bytes] = []
    i, start = 0, 0
    while i < len(comp):
        ctrl = comp[i]
        code_len = 2 if ctrl & 0x80 else ctrl + 2
        if i - start + code_len > max_chunk and i > start:
            chunks.append(comp[start:i])
            start = i
        i += code_len
    if i > start:
        chunks.append(comp[start:])
    return chunks


# ---------------------------------------------------------------- 封包

# 机型表：model_id → (宽, 高, 是否三色)
# 来源：EPD-nRF5 EPD/UC81xx.c、EPD/SSD16xx.c 与网页端 index.html 驱动列表
MODEL_INFO = {
    0x01: (400, 300, False),   # 4.2" 黑白 UC8176
    0x02: (400, 300, True),    # 4.2" 三色 SSD1619
    0x03: (400, 300, True),    # 4.2" 三色 UC8176
    0x04: (400, 300, False),   # 4.2" 黑白 SSD1619
    0x05: (400, 300, True),    # 4.2" 四色 JD79668
    0x06: (800, 480, False),   # 7.5" 黑白 UC8179
    0x07: (800, 480, True),    # 7.5" 三色 UC8179
    0x08: (640, 384, False),   # 7.5"低分 黑白 UC8159
    0x09: (640, 384, True),    # 7.5"低分 三色 UC8159
    0x0A: (880, 528, False),   # 7.5"HD 黑白 SSD1677
    0x0B: (880, 528, True),    # 7.5"HD 三色 SSD1677
    0x0C: (800, 480, True),    # 7.5" 四色 JD79665
    0x0D: (648, 480, True),    # 5.83" 四色 JD79665
    0x0E: (600, 448, True),    # 5.83"低分 三色 UC8159
    0x0F: (600, 448, False),   # 5.83"低分 黑白 UC8159
    0x10: (648, 480, True),    # 5.83" 三色 UC8179
    0x11: (648, 480, False),   # 5.83" 黑白 UC8179
}


def image_packets(data: bytes, chunk_size: int, use_rle: bool,
                  plane: str = "bw") -> list[bytes]:
    """把 1-bit 显存数据切成 WRITE_IMG 包序列: [0x30, header, ...chunk]。

    老固件 header（无 RLE，半字节语义: 高=0 起始 / 低=F 黑白面）:
        黑白面 首包 0x0F / 后续 0xFF；红色面 首包 0x00 / 后续 0xF0
    新固件 header（bit 语义: bit0=红面, bit1=首包, bit2=RLE）:
        黑白面 首包 0x06/后续 0x04；红色面 首包 0x07/后续 0x05
    """
    if use_rle:
        red = 0x01 if plane == "red" else 0x00
        chunks = rle_compress_mtu(data, chunk_size)
        return [
            bytes([EpdCmd.WRITE_IMG,
                   0x04 | red | (0x02 if i == 0 else 0x00)]) + c
            for i, c in enumerate(chunks)
        ]
    chunks = [data[o:o + chunk_size] for o in range(0, len(data), chunk_size)]
    first, cont = (0x00, 0xF0) if plane == "red" else (0x0F, 0xFF)
    return [
        bytes([EpdCmd.WRITE_IMG, first if i == 0 else cont]) + c
        for i, c in enumerate(chunks)
    ]
