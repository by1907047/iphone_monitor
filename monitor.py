"""iPhone 状态采集：经由 usbmux（USB 线或 Wi-Fi 同步）读取电池、功耗、存储信息。

后台线程里跑一个 asyncio 循环，断线后自动重连；界面线程通过 snapshot()/history() 读取。
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.diagnostics import DiagnosticsService

from i18n import tr

log = logging.getLogger(__name__)

# 打包成 exe 后程序目录可能不可写（比如放在 Program Files），日志和调试数据统一放到 %APPDATA%
FROZEN = getattr(sys, "frozen", False)
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "iPhoneMonitor"
LOG_DIR = DATA_DIR if FROZEN else Path(__file__).parent
RAW_DUMP = LOG_DIR / "last_battery_raw.json"
DISK_REFRESH_SECONDS = 300
# 详情窗口关着（且图标不显示功率）时电池的轮询间隔下限
BACKGROUND_INTERVAL = 10.0
# sysmontap 只订阅界面用到的字段
SYSMON_PROCESS_ATTRS = ["pid", "name", "cpuUsage", "physFootprint"]
SYSMON_SYSTEM_ATTRS = [
    "physMemSize", "vmActiveCount", "vmWireCount", "vmCompressorPageCount",
    "vmFreeCount", "vmInactiveCount", "vmSpeculativeCount",
]
# 采样间隔；pymobiledevice3 11.33.1 起输出频率也跟着它（之前写死 1 ms）
SYSMON_INTERVAL_MS = 2000
ADAPTER_NAMES = {"pd charger": "USB-C PD", "usb host": "USB 主机", "usb charger": "USB 充电器"}


@dataclass(frozen=True)
class Snapshot:
    connected: bool = False
    status: str = tr("正在连接…")
    timestamp: float = 0.0

    name: str = ""
    model: str = ""
    ios: str = ""
    connection: str = ""  # "USB" / "Wi-Fi"

    percent: Optional[int] = None
    charging: Optional[bool] = None
    external_power: Optional[bool] = None
    fully_charged: Optional[bool] = None

    voltage_v: Optional[float] = None
    current_ma: Optional[int] = None  # 正 = 充入电池，负 = 电池放电
    battery_w: Optional[float] = None  # 同上符号
    system_load_w: Optional[float] = None  # 整机耗电（有遥测数据时为实测，否则放电时用电池功率估算）
    system_load_measured: bool = False
    power_in_w: Optional[float] = None  # 外部输入功率（需要设备提供遥测数据）
    input_voltage_v: Optional[float] = None  # 手机端实测的输入电压（线材压降会让它低于充电器标称）
    input_current_a: Optional[float] = None
    temperature_c: Optional[float] = None

    adapter: str = ""
    adapter_watts: Optional[float] = None
    adapter_voltage_v: Optional[float] = None  # 充电器协商的电压，如 5 V
    wireless: Optional[bool] = None

    cycle_count: Optional[int] = None
    design_mah: Optional[int] = None
    full_mah: Optional[int] = None
    remaining_mah: Optional[int] = None
    health_pct: Optional[float] = None

    disk_total: Optional[int] = None
    disk_free: Optional[int] = None


@dataclass(frozen=True)
class DevStats:
    """经开发者通道（隧道 + sysmontap）拿到的 CPU / 内存数据。"""

    active: bool = False
    status: str = ""
    timestamp: float = 0.0
    cpu_pct: Optional[float] = None  # 整机平均占用，0–100
    cpu_cores: Optional[int] = None
    mem_used: Optional[int] = None  # 字节
    mem_total: Optional[int] = None
    process_count: Optional[int] = None
    top: tuple[tuple[str, float, Optional[int]], ...] = ()  # (进程名, CPU% 按单核计, 内存字节)


def _signed64(value: Any) -> Optional[int]:
    """IORegistry 里的负电流有时以无符号 64 位整数返回。"""
    if value is None:
        return None
    value = int(value)
    if value >= 1 << 63:
        value -= 1 << 64
    return value


def _first(*values: Any) -> Any:
    for v in values:
        if v not in (None, 0, ""):
            return v
    return None


def parse_battery(raw: dict[str, Any]) -> dict[str, Any]:
    battery_data = raw.get("BatteryData") or {}
    telemetry = raw.get("PowerTelemetryData") or {}
    adapter = raw.get("AdapterDetails") or {}

    percent = raw.get("CurrentCapacity")
    max_capacity = raw.get("MaxCapacity")
    if percent is not None and max_capacity and max_capacity != 100:
        # 少数旧机型这两个字段是 mAh
        percent = round(percent * 100 / max_capacity)

    voltage_mv = _first(raw.get("Voltage"), raw.get("AppleRawBatteryVoltage"))
    # Amperage 是平滑后的电流，InstantAmperage 瞬时值跳动很大
    current_ma = _signed64(_first(raw.get("Amperage"), raw.get("InstantAmperage")))
    if current_ma is None and ("InstantAmperage" in raw or "Amperage" in raw):
        current_ma = 0

    battery_w = None
    if telemetry.get("BatteryPower") is not None:
        battery_w = _signed64(telemetry["BatteryPower"]) / 1000
    elif voltage_mv is not None and current_ma is not None:
        battery_w = voltage_mv * current_ma / 1_000_000

    system_load_w = None
    system_load_measured = False
    if telemetry.get("SystemLoad") is not None:
        system_load_w = telemetry["SystemLoad"] / 1000
        system_load_measured = True
    elif battery_w is not None and not raw.get("ExternalConnected") and battery_w < 0:
        system_load_w = -battery_w

    power_in_w = None
    if telemetry.get("SystemPowerIn") is not None:
        power_in_w = telemetry["SystemPowerIn"] / 1000
    input_voltage = telemetry.get("SystemVoltageIn") or None
    input_current = telemetry.get("SystemCurrentIn")

    temperature = _first(raw.get("Temperature"), raw.get("VirtualTemperature"))
    design = _first(raw.get("DesignCapacity"), battery_data.get("DesignCapacity"))
    full = _first(
        raw.get("NominalChargeCapacity"), battery_data.get("NominalChargeCapacity"), raw.get("AppleRawMaxCapacity")
    )
    health = round(full * 100 / design, 1) if design and full else None
    remaining = _first(raw.get("AppleRawCurrentCapacity"), battery_data.get("RemainingCapacity"))

    adapter_name = _first(adapter.get("Name"), adapter.get("Description"), adapter.get("Manufacturer")) or ""
    adapter_name = ADAPTER_NAMES.get(str(adapter_name).lower(), str(adapter_name))
    if adapter.get("AdapterVoltage") and adapter.get("Current"):
        adapter_name += f"（{adapter['AdapterVoltage'] / 1000:g}V/{adapter['Current'] / 1000:g}A）"
    wireless = adapter.get("IsWireless")
    if wireless is None and adapter_name:
        wireless = any(k in adapter_name.lower() for k in ("wireless", "magsafe", "qi"))

    return dict(
        percent=percent,
        charging=raw.get("IsCharging"),
        external_power=raw.get("ExternalConnected"),
        fully_charged=raw.get("FullyCharged"),
        voltage_v=voltage_mv / 1000 if voltage_mv else None,
        current_ma=current_ma,
        battery_w=battery_w,
        system_load_w=system_load_w,
        system_load_measured=system_load_measured,
        power_in_w=power_in_w,
        input_voltage_v=input_voltage / 1000 if input_voltage else None,
        input_current_a=input_current / 1000 if input_voltage and input_current is not None else None,
        temperature_c=temperature / 100 if temperature else None,
        adapter=adapter_name,
        adapter_watts=adapter.get("Watts"),
        adapter_voltage_v=adapter["AdapterVoltage"] / 1000 if adapter.get("AdapterVoltage") else None,
        wireless=wireless,
        cycle_count=_first(raw.get("CycleCount"), battery_data.get("CycleCount")),
        design_mah=design,
        full_mah=full,
        remaining_mah=remaining,
        health_pct=health,
    )


def friendly_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "usbmuxd" in lowered or "connectionrefused" in lowered or "10061" in lowered:
        return tr("未找到 Apple 设备服务（需安装 Apple Devices 或 iTunes）")
    if "nodevice" in lowered or "no device" in lowered or "device not found" in lowered:
        # 手机重启或开关个人热点后 USB 会重新枚举，Apple 服务有时认不回来，拔插一次就好
        return tr("未检测到 iPhone：请插线；已插线就拔插一次数据线")
    if "pair" in lowered or "trust" in lowered or "passwordprotected" in lowered:
        return tr("请解锁 iPhone 并点「信任此电脑」")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return tr("读取超时，正在重连…")
    return tr("连接中断，正在重连…")


def friendly_dev_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "developermode" in text or "developer mode" in text:
        return tr("需在 iPhone 上开启开发者模式")
    if "mount timeout" in text:
        return tr("挂载镜像超时：请解锁 iPhone，稍后自动重试")
    if "tss" in text or "github" in text:
        return tr("开发者镜像下载/签名失败，稍后重试")
    if "userspacetunnelunavailable" in text or "coredeviceproxy" in text:
        return tr("需用 USB 线连接（Wi-Fi 下不支持）")
    return tr("开发者通道断开，正在重连…")


class Monitor:
    def __init__(
        self, interval: float = 2.0, history_seconds: int = 600, dev_enabled: bool = True, usb_only: bool = True
    ) -> None:
        self.interval = interval
        self._dev_enabled = dev_enabled
        self.usb_only = usb_only  # True 时只连 USB，Wi-Fi 同步的设备一律忽略
        # 开发者通道按需开启：界面在看 CPU/内存时才连，没人看时关掉隧道省电脑 CPU
        self._dev_demand_until = 0.0
        # 电池按需快速轮询：窗口开着时用设置的间隔，否则放慢到 BACKGROUND_INTERVAL
        self._fast_until = 0.0
        # 后台线程的事件循环和事件；界面线程通过 _signal 唤醒，不再 0.2 秒轮询一次
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_ev: Optional[asyncio.Event] = None
        self._stop_ev: Optional[asyncio.Event] = None
        self._dev_ev: Optional[asyncio.Event] = None
        self._history_seconds = history_seconds
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._dev = DevStats(status=tr("等待连接…"))
        self._history: deque[tuple[float, Optional[float], Optional[float], Optional[int], Optional[float]]] = deque()
        self._dev_history: deque[tuple[float, Optional[float], Optional[float]]] = deque()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, name="iphone-monitor", daemon=True)

    # ---- 供界面线程调用 ----
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._signal("_stop_ev")

    def refresh_now(self) -> None:
        self._signal("_wake_ev")

    @property
    def dev_enabled(self) -> bool:
        return self._dev_enabled

    @dev_enabled.setter
    def dev_enabled(self, on: bool) -> None:
        self._dev_enabled = on
        self._signal("_dev_ev")

    def request_fast(self, linger: float = 5.0) -> None:
        """界面需要及时的电池数据时持续调用（窗口开着、图标显示功率）。"""
        was_fast = time.time() < self._fast_until
        self._fast_until = max(self._fast_until, time.time() + linger)
        if not was_fast:
            self._signal("_wake_ev")  # 刚打开窗口时立刻读一次

    def _signal(self, name: str) -> None:
        """从任意线程唤醒后台循环里的某个 asyncio.Event。"""
        loop, event = self._loop, getattr(self, name)
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # 循环已关闭

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def history(self) -> list[tuple[float, Optional[float], Optional[float], Optional[int], Optional[float]]]:
        """[(时间戳, 电池功率W, 整机耗电W, 电量%, 温度°C), ...]"""
        with self._lock:
            return list(self._history)

    def request_dev(self, linger: float = 60.0) -> None:
        """界面需要 CPU/内存数据时持续调用；最后一次调用 linger 秒后自动断开开发者通道。"""
        was_wanted = self._dev_wanted()
        self._dev_demand_until = max(self._dev_demand_until, time.time() + linger)
        if not was_wanted:
            self._signal("_dev_ev")

    def _dev_wanted(self) -> bool:
        return self.dev_enabled and time.time() < self._dev_demand_until

    def dev_stats(self) -> DevStats:
        with self._lock:
            return self._dev

    def dev_history(self) -> list[tuple[float, Optional[float], Optional[float]]]:
        """[(时间戳, CPU%, 内存%), ...]"""
        with self._lock:
            return list(self._dev_history)

    # ---- 后台线程 ----
    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _wait(self, seconds: float, *events: asyncio.Event) -> None:
        """等到超时、停止，或任一给定事件被触发（触发后清除）。"""
        waiters = [asyncio.ensure_future(e.wait()) for e in (self._stop_ev, *events)]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        for e in events:
            e.clear()

    async def _sleep(self, seconds: float, wakeable: bool = True) -> None:
        await self._wait(seconds, *([self._wake_ev] if wakeable else []))

    def _poll_interval(self) -> float:
        if time.time() < self._fast_until:
            return self.interval
        return max(self.interval, BACKGROUND_INTERVAL)

    def _publish(self, snap: Snapshot) -> None:
        with self._lock:
            self._snapshot = snap
            if snap.connected:
                self._history.append(
                    (snap.timestamp, snap.battery_w, snap.system_load_w, snap.percent, snap.temperature_c)
                )
                cutoff = snap.timestamp - self._history_seconds
                while self._history and self._history[0][0] < cutoff:
                    self._history.popleft()

    def _set_status(self, status: str) -> None:
        with self._lock:
            # 断线时保留最后一次的设备名，方便界面显示
            self._snapshot = replace(self._snapshot, connected=False, status=status, timestamp=time.time())

    async def _run(self) -> None:
        self._wake_ev, self._stop_ev, self._dev_ev = asyncio.Event(), asyncio.Event(), asyncio.Event()
        if self._stop.is_set():
            self._stop_ev.set()
        self._loop = asyncio.get_running_loop()
        await asyncio.gather(self._battery_loop(), self._dev_loop())

    async def _battery_loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 2.0
            except Exception as exc:  # noqa: BLE001 - 任何错误都转为重连
                log.warning("会话结束: %r", exc)
                self._set_status(friendly_error(exc))
                await self._sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    # ---- CPU / 内存（开发者通道） ----
    def _set_dev(self, **changes: Any) -> None:
        with self._lock:
            self._dev = replace(self._dev, **changes)

    async def _dev_loop(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            if not self.dev_enabled:
                self._set_dev(active=False, status=tr("已关闭"))
                await self._wait(60, self._dev_ev)
                continue
            if not self._dev_wanted():
                self._set_dev(active=False, status=tr("待机（打开详情窗口时读取）"))
                await self._wait(60, self._dev_ev)
                continue
            snap = self.snapshot()
            if not snap.connected:
                self._set_dev(active=False, status=tr("等待连接…"))
                await self._sleep(2, wakeable=False)
                continue
            if snap.connection == "Wi-Fi":
                self._set_dev(active=False, status=tr("需用 USB 线连接（Wi-Fi 下不支持）"))
                await self._sleep(5, wakeable=False)
                continue
            try:
                self._set_dev(status=tr("正在建立开发者通道…"))
                await self._dev_session()
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001 - 任何错误都转为重连
                log.warning("开发者通道结束: %r", exc)
                self._set_dev(active=False, status=friendly_dev_error(exc))
                await self._sleep(backoff, wakeable=False)
                backoff = min(backoff * 2, 60.0)

    async def _dev_session(self) -> None:
        # 导入较慢、占内存（纯 Python 网络栈等），只在需要时加载
        from pymobiledevice3.exceptions import AlreadyMountedError
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.mobile_image_mounter import auto_mount

        # remotepairing_fallback 会改用局域网 bonjour 配对，这里只允许走 USB
        async with UserspaceRsdTunnel(remotepairing_fallback=False) as rsd:
            try:
                if not await _ddi_installed(rsd):
                    # 手机锁屏时镜像服务不回应，会一直卡住，所以加超时并提示解锁
                    self._set_dev(status=tr("正在挂载开发者镜像（请保持手机解锁）…"))
                    try:
                        await asyncio.wait_for(auto_mount(rsd), 180)
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError("mount timeout: device locked?") from exc
                log.info("开发者镜像已挂载")
            except AlreadyMountedError:
                pass
            async with DvtProvider(rsd) as dvt:
                # 默认会要全部约 70 个进程字段，电脑端解码这份数据很耗 CPU；只要用到的几个
                async with DeviceInfo(dvt) as info:
                    proc_supported = set(await info.sysmon_process_attributes())
                    sys_supported = set(await info.sysmon_system_attributes())
                sys_attrs = [a for a in SYSMON_SYSTEM_ATTRS if a in sys_supported]
                # 试过「只有文字条时只订阅 pid、5 秒一次」的精简模式：手机整机 CPU 测不出差别（低于手机自身波动），
                # 所以只保留这一种模式
                await self._sample(dvt, [a for a in SYSMON_PROCESS_ATTRS if a in proc_supported], sys_attrs,
                                   SYSMON_INTERVAL_MS)

    async def _sample(self, dvt: Any, proc_attrs: list[str], sys_attrs: list[str], interval: int) -> None:
        """跑 sysmontap 采样，直到不再需要。"""
        from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap

        tap = Sysmontap(dvt, proc_attrs, sys_attrs, interval_ms=interval)
        proc_fields = list(tap.process_attributes_cls.__dataclass_fields__)
        sys_fields = list(tap.system_attributes_cls.__dataclass_fields__)
        async with tap as sysmon:
            self._set_dev(status=tr("正在读取…"))
            system: dict[str, Any] = {}
            cpu: Optional[dict[str, Any]] = None
            seen_cpu = False
            async for row in sysmon:
                if self._stop.is_set() or not self._dev_wanted():
                    return  # 退出后关闭隧道
                if not isinstance(row, dict):
                    continue  # 心跳等非数据帧
                if "System" in row:
                    system = dict(zip(sys_fields, row["System"]))
                if "SystemCPUUsage" in row:
                    # 第一帧的 CPU 数值总是 0 或 100，丢弃
                    if seen_cpu:
                        cpu = {**row["SystemCPUUsage"], "EnabledCPUs": row.get("EnabledCPUs"),
                               "CPUCount": row.get("CPUCount")}
                    seen_cpu = True
                if "Processes" in row and cpu is not None:
                    procs = [dict(zip(proc_fields, v)) for v in row["Processes"].values()]
                    self._publish_dev(parse_sysmon(system, cpu, procs))

    def _publish_dev(self, stats: DevStats) -> None:
        with self._lock:
            self._dev = stats
            mem_pct = stats.mem_used * 100 / stats.mem_total if stats.mem_used and stats.mem_total else None
            self._dev_history.append((stats.timestamp, stats.cpu_pct, mem_pct))
            cutoff = stats.timestamp - self._history_seconds
            while self._dev_history and self._dev_history[0][0] < cutoff:
                self._dev_history.popleft()

    # ---- 电池（lockdown） ----
    async def _session(self) -> None:
        usb_only = self.usb_only
        lockdown = await asyncio.wait_for(
            create_using_usbmux(autopair=True, pair_timeout=60, connection_type="USB" if usb_only else None), 90
        )
        async with lockdown:
            values = lockdown.all_values
            conn_type = lockdown.short_info.get("ConnectionType") or ""
            device = dict(
                name=values.get("DeviceName") or "iPhone",
                model=lockdown.display_name or values.get("ProductType") or "",
                ios=lockdown.product_version,
                connection="Wi-Fi" if conn_type == "Network" else (conn_type or "USB"),
            )
            log.info("已连接 %s", device)

            disk: dict[str, Any] = {}
            disk_at = 0.0
            dumped = False
            async with DiagnosticsService(lockdown=lockdown) as diag:
                while not self._stop.is_set():
                    if self.usb_only != usb_only:
                        return  # 连接方式改了，重新按新设置连接
                    raw = await asyncio.wait_for(diag.get_battery(), 15)
                    if not raw:
                        raise RuntimeError("设备没有返回电池数据")
                    if not dumped:
                        _dump_raw(raw)
                        dumped = True

                    now = time.time()
                    if now - disk_at > DISK_REFRESH_SECONDS:
                        try:
                            usage = await asyncio.wait_for(lockdown.get_value("com.apple.disk_usage"), 10) or {}
                            disk = dict(
                                disk_total=usage.get("TotalDiskCapacity"),
                                disk_free=usage.get("AmountDataAvailable"),
                            )
                        except Exception as exc:  # noqa: BLE001 - 存储信息拿不到不影响主功能
                            log.info("读取存储信息失败: %r", exc)
                        disk_at = now

                    self._publish(
                        Snapshot(connected=True, status=tr("已连接"), timestamp=now, **device, **disk, **parse_battery(raw))
                    )
                    await self._sleep(self._poll_interval())


async def _ddi_installed(rsd: Any) -> bool:
    """镜像已装好时跳过 auto_mount：它的预检查走 image mounter，锁屏时会卡住。"""
    from pymobiledevice3.services.cryptexd import CryptexdService

    try:
        # pymobiledevice3 没有公开的查询接口，用的是 auto_install_ddi 内部同一个方法
        return await asyncio.wait_for(CryptexdService(rsd)._installed_ddi(), 20) is not None
    except Exception as exc:  # noqa: BLE001 - 查不到就走正常挂载流程
        log.info("查询开发者镜像失败: %r", exc)
        return False


def parse_sysmon(system: dict[str, Any], cpu: dict[str, Any], procs: list[dict[str, Any]]) -> DevStats:
    # CPU_TotalLoad 是各核占用之和；EnabledCPUs 会随调度变化，按物理核数折算成整机百分比
    cores = cpu.get("CPUCount") or cpu.get("EnabledCPUs")
    total_load = cpu.get("CPU_TotalLoad")
    cpu_pct = None
    if total_load is not None and cores:
        cpu_pct = max(0.0, min(100.0, total_load / cores))

    page = system.get("vmPageSize") or 16384
    mem_total = system.get("physMemSize")
    if mem_total is not None and mem_total < 1 << 26:
        mem_total *= page  # iOS 上 physMemSize 是页数
    mem_used = None
    pages_used = [system.get(k) for k in ("vmActiveCount", "vmWireCount", "vmCompressorPageCount")]
    if all(v is not None for v in pages_used):
        mem_used = sum(pages_used) * page
    if mem_total is None:
        pages_all = [system.get(k) for k in ("vmFreeCount", "vmActiveCount", "vmInactiveCount", "vmWireCount",
                                              "vmSpeculativeCount", "vmCompressorPageCount")]
        if all(v is not None for v in pages_all):
            mem_total = sum(pages_all) * page

    top = sorted(procs, key=lambda p: p.get("cpuUsage") or 0, reverse=True)[:6]
    return DevStats(
        active=True,
        status=tr("已连接"),
        timestamp=time.time(),
        cpu_pct=cpu_pct,
        cpu_cores=cores,
        mem_used=mem_used,
        mem_total=mem_total,
        process_count=len(procs),
        top=tuple(
            (
                str(p.get("name") or p.get("pid")),
                float(p.get("cpuUsage") or 0),
                p.get("physFootprint") or p.get("memResidentSize"),
            )
            for p in top
        ),
    )


def _dump_raw(raw: dict[str, Any]) -> None:
    """保存一份原始数据，方便排查字段差异。"""
    try:
        RAW_DUMP.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass
