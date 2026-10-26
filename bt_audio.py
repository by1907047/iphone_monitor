"""手机声音转到电脑：把已配对的 iPhone 当作蓝牙音源，声音从电脑当前的输出设备（比如你的耳机）播放。

用的是 Windows 10 2004+ 自带的 AudioPlaybackConnection（A2DP Sink），不需要第三方应用。
后台线程跑一个 asyncio 循环：开着时连不上就每隔一段时间重试，断开后自动重连。
建立连接的两步各有超时：手机半在线时系统可能一直不回应，不设超时就会卡住、再也不重试
（以前只有打开系统的声音设置、让它重新枚举设备，卡住的请求才会走下去）。
"""

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from i18n import tr

log = logging.getLogger(__name__)

FAILURE_LOG_SECONDS = 600  # 一直连不上时，每隔这么久记一次进展
RETRY_MIN, RETRY_MAX = 10.0, 30.0  # 连不上时的重试间隔（秒），逐步拉长
STEP_TIMEOUT = 20.0  # start / open 各自最多等这么久


class BluetoothAudio:
    def __init__(self, preferred_name: Callable[[], str], enabled: bool = False) -> None:
        self.preferred_name = preferred_name  # 优先连这个名字的设备（手机名，如「My iPhone」）
        self._enabled = enabled
        self.status = tr("已关闭")
        self.device_name = ""
        self._conn = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._stop = False
        self._force = False  # 手动「重新连接」：断开现有连接并马上重连
        self._last_failure = ""
        self._failures = 0
        self._failure_logged_at = 0.0
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

    def reconnect(self) -> None:
        """右键菜单「重新连接手机声音」：不管现在什么状态，断开后立刻重连。"""
        self._force = True
        self._signal()

    def kick(self) -> None:
        """手机刚通过 USB 连上电脑：人就在旁边，没连上的话马上重试一次，不等退避时间。"""
        self._signal()

    @property
    def connected(self) -> bool:
        return self._conn is not None  # 只在连接打开后才赋值，断开时清空

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
            self.status = tr("不可用（系统不支持）")

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
            if self._force:
                self._force = False
                self._close()
                retry = RETRY_MIN
                log.info("蓝牙音频：手动重新连接")
            if not self._enabled:
                self._close()
                self.status = tr("已关闭")
                await self._wait(3600)
                continue
            if self._conn is not None:
                if self._conn.state == AudioPlaybackConnectionState.OPENED:
                    await self._wait(30)  # 已连接：状态变化会立刻唤醒，这里只是兜底
                    continue
                # 真的断开了（手机走远、关了蓝牙）：先等一会儿再重连，避免来回抖
                self._close()
                self.status = tr("连接已断开，稍后重连")
                await self._wait(RETRY_MIN)
                continue
            try:
                devices = list(await DeviceInformation.find_all_async_aqs_filter(
                    AudioPlaybackConnection.get_device_selector()))
                if not devices:
                    self.status = tr("未找到已配对的手机（先在系统里配对蓝牙）")
                    await self._wait(retry)
                    retry = min(retry * 2, RETRY_MAX)
                    continue
                name = self.preferred_name() or ""
                devices.sort(key=lambda d: 0 if name and name in d.name else 1)
                device = devices[0]
                self.status = tr("正在连接 {name}…", name=device.name)
                conn = AudioPlaybackConnection.try_create_from_id(device.id)
                if conn is None:
                    raise RuntimeError("无法创建连接")
                conn.add_state_changed(lambda _sender, _args: self._signal())
                try:
                    await asyncio.wait_for(_await(conn.start_async()), STEP_TIMEOUT)
                    result = await asyncio.wait_for(_await(conn.open_async()), STEP_TIMEOUT)
                except asyncio.TimeoutError:
                    conn.close()
                    self._failed("timeout", tr("连接超时"))
                    await self._wait(retry)
                    retry = min(retry * 2, RETRY_MAX)
                    continue
                if result.status == AudioPlaybackConnectionOpenResultStatus.SUCCESS:
                    # open 返回成功时状态还是 CLOSED，约 0.3 秒后才变成 OPENED；等它变过来再判断
                    for _ in range(50):
                        if conn.state == AudioPlaybackConnectionState.OPENED:
                            break
                        await asyncio.sleep(0.1)
                    self._conn, self.device_name = conn, device.name
                    self.status = tr("已连接 {name}", name=device.name)
                    if self._failures:
                        log.info("蓝牙音频已连接：%s（之前失败 %d 次）", device.name, self._failures)
                    else:
                        log.info("蓝牙音频已连接：%s", device.name)
                    retry, self._last_failure, self._failures = RETRY_MIN, "", 0
                    continue
                conn.close()
                reason = {
                    AudioPlaybackConnectionOpenResultStatus.REQUEST_TIMED_OUT: tr("手机不在附近或蓝牙未开"),
                    AudioPlaybackConnectionOpenResultStatus.DENIED_BY_SYSTEM: tr("被系统拒绝"),
                }.get(result.status, tr("连接失败"))
                self._failed(f"open status {result.status.name}", reason)
            except Exception as exc:  # noqa: BLE001 - 任何错误都稍后重试
                self._failed(repr(exc), tr("连接失败"))
            await self._wait(retry)
            retry = min(retry * 2, RETRY_MAX)
        self._close()

    def _failed(self, code: str, reason: str) -> None:
        """连接失败：更新状态；原因变了才写日志，免得每 30 秒刷一行。"""
        self.status = tr("{reason}，稍后重试", reason=reason)
        self._failures += 1
        now = time.time()
        if code != self._last_failure:
            log.info("蓝牙音频连接失败：%s", code)
            self._last_failure, self._failure_logged_at = code, now
        elif now - self._failure_logged_at >= FAILURE_LOG_SECONDS:
            log.info("蓝牙音频仍未连上：已重试 %d 次，最近原因 %s", self._failures, code)
            self._failure_logged_at = now

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("蓝牙音频已断开")
            self._conn = None
            self.device_name = ""


async def _await(operation):
    return await operation
