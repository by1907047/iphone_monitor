"""手机声音转到电脑：把已配对的 iPhone 当作蓝牙音源，声音从电脑当前的输出设备（比如你的耳机）播放。

用的是 Windows 10 2004+ 自带的 AudioPlaybackConnection（A2DP Sink），不需要第三方应用。
后台线程跑一个 asyncio 循环：开着时连不上就每隔一段时间重试，断开后自动重连。
"""

import asyncio
import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

RETRY_MIN, RETRY_MAX = 10.0, 60.0  # 连不上时的重试间隔（秒），逐步拉长


class BluetoothAudio:
    def __init__(self, preferred_name: Callable[[], str], enabled: bool = False) -> None:
        self.preferred_name = preferred_name  # 优先连这个名字的设备（手机名，如「My iPhone」）
        self._enabled = enabled
        self.status = "已关闭"
        self.device_name = ""
        self._conn = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._stop = False
        threading.Thread(target=self._thread_main, name="bt-audio", daemon=True).start()

    # ---- 界面线程接口 ----
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        self._signal()

    def stop(self) -> None:
        self._stop = True
        self._signal()

    @property
    def connected(self) -> bool:
        return self._conn is not None and self.status.startswith("已连接")

    def _signal(self) -> None:
        if self._loop is not None and self._wake is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    # ---- 后台线程 ----
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:  # noqa: BLE001
            log.exception("蓝牙音频线程退出")
            self.status = "不可用（系统不支持）"

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _run(self) -> None:
        # WinRT 在导入时初始化当前线程的 COM 公寓，所以放在后台线程里导入
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.media.audio import (
            AudioPlaybackConnection,
            AudioPlaybackConnectionOpenResultStatus,
            AudioPlaybackConnectionState,
        )

        self._loop, self._wake = asyncio.get_running_loop(), asyncio.Event()
        retry = RETRY_MIN
        while not self._stop:
            if not self._enabled:
                self._close()
                self.status = "已关闭"
                await self._wait(3600)
                continue
            if self._conn is not None:
                if self._conn.state == AudioPlaybackConnectionState.OPENED:
                    await self._wait(30)  # 已连接：状态变化会立刻唤醒，这里只是兜底
                    continue
                # 真的断开了（手机走远、关了蓝牙）：先等一会儿再重连，避免来回抖
                self._close()
                self.status = "连接已断开，稍后重连"
                await self._wait(RETRY_MIN)
                continue
            try:
                devices = list(await DeviceInformation.find_all_async_aqs_filter(
                    AudioPlaybackConnection.get_device_selector()))
                if not devices:
                    self.status = "未找到已配对的手机（先在系统里配对蓝牙）"
                    await self._wait(retry)
                    retry = min(retry * 2, RETRY_MAX)
                    continue
                name = self.preferred_name() or ""
                devices.sort(key=lambda d: 0 if name and name in d.name else 1)
                device = devices[0]
                self.status = f"正在连接 {device.name}…"
                conn = AudioPlaybackConnection.try_create_from_id(device.id)
                if conn is None:
                    raise RuntimeError("无法创建连接")
                conn.add_state_changed(lambda _sender, _args: self._signal())
                await conn.start_async()
                result = await conn.open_async()
                if result.status == AudioPlaybackConnectionOpenResultStatus.SUCCESS:
                    # open 返回成功时状态还是 CLOSED，约 0.3 秒后才变成 OPENED；等它变过来再判断
                    for _ in range(50):
                        if conn.state == AudioPlaybackConnectionState.OPENED:
                            break
                        await asyncio.sleep(0.1)
                    self._conn, self.device_name = conn, device.name
                    self.status = f"已连接 {device.name}"
                    log.info("蓝牙音频已连接：%s", device.name)
                    retry = RETRY_MIN
                    continue
                conn.close()
                reason = {
                    AudioPlaybackConnectionOpenResultStatus.REQUEST_TIMED_OUT: "手机不在附近或蓝牙未开",
                    AudioPlaybackConnectionOpenResultStatus.DENIED_BY_SYSTEM: "被系统拒绝",
                }.get(result.status, "连接失败")
                self.status = f"{reason}，稍后重试"
            except Exception as exc:  # noqa: BLE001 - 任何错误都稍后重试
                log.info("蓝牙音频连接失败: %r", exc)
                self.status = "连接失败，稍后重试"
            await self._wait(retry)
            retry = min(retry * 2, RETRY_MAX)
        self._close()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("蓝牙音频已断开")
            self._conn = None
            self.device_name = ""
