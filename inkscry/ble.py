"""EPD-nRF5 BLE 客户端（基于 bleak）。

用法:
    async with EPDClient() as epd:
        await epd.push_image(bitmap_bytes)   # INIT → WRITE_IMG... → REFRESH
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from . import config
from .protocol import CHAR_UUID, MODEL_INFO, EpdCmd, image_packets

log = logging.getLogger("inkscry.ble")

DEFAULT_MTU = 23            # BLE 最小 MTU；chunk = mtu - 2 = 20
MTU_WAIT_TIMEOUT = 3.0      # 等待设备上报 "mtu=..." 的秒数
INTERLEAVED_WRITES = 8      # 每 8 个免应答写后插入 1 个带应答写做流控


class EPDClient:
    def __init__(self, device_name: str | None = None, address: str | None = None,
                 interleaved: int = INTERLEAVED_WRITES):
        # 显式参数 > .env / 环境变量
        config.load_dotenv()
        self.device_name = device_name or config.device_name()
        self.address = address or config.device_address()
        self.interleaved = interleaved
        self.client: BleakClient | None = None
        self.mtu = DEFAULT_MTU
        self.rle_support = False
        self.model_id: int | None = None
        self._mtu_event = asyncio.Event()
        self.last_notify = ""

    # ------------------------------------------------------------ 连接

    async def __aenter__(self) -> "EPDClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    def _on_notify(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = bytes(data)
        # 订阅 notify 后固件先推一条 epd_config_t 二进制（model_id 在 offset 7）
        if raw and raw[0] not in (0x6D, 0x74):  # 非文本（'m'tu / 't'）
            if len(raw) >= 8:
                self.model_id = raw[7]
                info = MODEL_INFO.get(self.model_id)
                log.info("设备配置: model_id=0x%02X %s", self.model_id,
                         f"({info[0]}x{info[1]}, {'三色' if info[2] else '黑白'})"
                         if info else "(未知型号)")
            else:
                log.debug("忽略二进制通知: %s", raw.hex())
            return
        msg = raw.decode("utf-8", errors="replace")
        self.last_notify = msg
        log.info("⇓ %s", msg)
        if msg.startswith("mtu="):
            try:
                self.mtu = int(msg[4:].split()[0].split(",")[0])
            except ValueError:
                pass
            if "rle=1" in msg:
                self.rle_support = True
            self._mtu_event.set()

    async def _find_device(self) -> BLEDevice:
        """优先级：固定地址 > 名称（大小写不敏感）> EPD 服务 UUID。"""
        from .protocol import SERVICE_UUID
        target_name = (self.device_name or "").lower()
        found: BLEDevice | None = None
        uuid_candidate: BLEDevice | None = None

        def cb(dev: BLEDevice, adv) -> None:
            nonlocal found, uuid_candidate
            if self.address and dev.address == self.address:
                found = dev
            elif target_name and (dev.name or "").lower() == target_name:
                found = dev
            elif SERVICE_UUID in [u.lower() for u in adv.service_uuids]:
                uuid_candidate = uuid_candidate or dev

        async with BleakScanner(detection_callback=cb):
            for _ in range(150):  # 15s，命中即提前结束
                if found:
                    break
                await asyncio.sleep(0.1)
        device = found or uuid_candidate
        if device is None:
            hint = ("" if (self.address or self.device_name) else
                    "未配置 INKSCRY_DEVICE_NAME / INKSCRY_DEVICE_ADDRESS（见 .env.example）；")
            raise RuntimeError(
                f"找不到设备 {self.address or self.device_name or ''}，{hint}"
                "请确认墨水屏已开机、未被其它主机连接、且在蓝牙范围内。")
        return device

    async def connect(self) -> None:
        if self.address:
            # macOS 会直接按 CoreBluetooth UUID 找回外设，无需等待广播
            # （设备被占用/已连接时停止广播，扫描反而会失败）
            log.info("直连 %s (%s) ...", self.device_name or "设备", self.address)
            self.client = BleakClient(self.address, timeout=20)
        else:
            device = await self._find_device()
            log.info("连接 %s (%s) ...", device.name, device.address)
            self.client = BleakClient(device, timeout=20)
        await self.client.connect()
        await self.client.start_notify(CHAR_UUID, self._on_notify)
        # 实测：本固件在 INIT 之后才上报 "mtu=..."，故等待放在 push_image 里

    async def _wait_mtu(self) -> None:
        self._mtu_event.clear()
        try:
            await asyncio.wait_for(self._mtu_event.wait(), MTU_WAIT_TIMEOUT)
            log.info("设备 MTU=%d, RLE=%s", self.mtu, self.rle_support)
        except asyncio.TimeoutError:
            log.warning("未收到 MTU 上报，按 MTU=%d (无 RLE) 传输", self.mtu)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    # ------------------------------------------------------------ 指令

    async def write_cmd(self, cmd: int, payload: bytes = b"") -> None:
        assert self.client is not None
        await self.client.write_gatt_char(CHAR_UUID, bytes([cmd]) + payload,
                                          response=True)

    async def init(self) -> None:
        await self.write_cmd(EpdCmd.INIT)

    async def clear(self) -> None:
        await self.write_cmd(EpdCmd.CLEAR)

    async def refresh(self) -> None:
        await self.write_cmd(EpdCmd.REFRESH)

    async def sleep(self) -> None:
        await self.write_cmd(EpdCmd.SLEEP)

    # ------------------------------------------------------------ 图片

    async def send_image_data(self, data: bytes, plane: str = "bw") -> None:
        """分包写入一个 bitplane（不含 INIT/REFRESH）。"""
        assert self.client is not None
        chunk = self.mtu - 2
        use_rle = self.rle_support
        packets = image_packets(data, chunk, use_rle, plane)
        if use_rle:
            log.info("RLE[%s]: %d → %d 字节, %d 包", plane,
                     len(data), sum(len(p) - 2 for p in packets), len(packets))
        else:
            log.info("RAW[%s]: %d 字节, %d 包", plane, len(data), len(packets))
        for i, pkt in enumerate(packets):
            # 免应答突发 + 定期带应答写做流控（对应网页端 interleaved 机制）
            with_resp = (i % (self.interleaved + 1)) == self.interleaved
            await self.client.write_gatt_char(CHAR_UUID, pkt, response=with_resp)

    @property
    def is_tricolor(self) -> bool:
        info = MODEL_INFO.get(self.model_id or -1)
        return bool(info and info[2])

    async def push_image(self, data: bytes, red: bytes | None = None) -> None:
        """完整上屏流程：INIT → 等 MTU 上报 → 写黑白面 → (三色机写红面) → REFRESH。

        red: 红色面字节流（bit=0 为红像素）。三色机缺省补全白（无红），
             黑白机忽略此参数。
        """
        await self.init()
        await self._wait_mtu()
        await self.send_image_data(data, "bw")
        if self.is_tricolor:
            # 三色屏必须同时写红面，否则残留旧红面数据刷新出花屏。
            await self.send_image_data(red if red is not None
                                       else b"\xFF" * len(data), "red")
        await self.refresh()
