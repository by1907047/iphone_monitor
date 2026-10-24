"""本机（笔记本）电池：直接向 Windows 电池驱动查询（IOCTL_BATTERY_*），不用 WMI，也不要管理员权限。

一次查询不到 1 毫秒，能拿到设计容量、满电容量、循环次数、当前容量、电压和充放电功率。
台式机没有电池时 present 为 False，界面就不显示「本机电池」。

整机功率：用电池供电时就是电池放电功率（实测）。插着电源时 Windows 没有通用的输入功率接口
（这台电脑的 ACPI Power Meter 读数恒为 0），所以用处理器封装功耗（Intel RAPL，Windows 的 Energy Meter
性能计数器）加上「其余部分」估算；「其余部分」= 用电池时的放电功率 − 处理器功耗，拔电时持续学习并保存。
"""

import ctypes
import ctypes.wintypes as w
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

HISTORY_SECONDS = 600
PACKAGE_COUNTER = "\\Energy Meter(RAPL_Package0_PKG)\\Power"  # 毫瓦
REST_FILE = Path(os.environ.get("APPDATA", Path.home())) / "iPhoneMonitor" / "laptop_power.json"
REST_ALPHA = 0.1  # 「其余部分」功耗的平滑系数
REST_MIN_W, REST_MAX_W = 0.5, 40.0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


GUID_DEVCLASS_BATTERY = _GUID(0x72631E54, 0x78A4, 0x11D0,
                              (ctypes.c_ubyte * 8)(0xBC, 0xF7, 0x00, 0xAA, 0x00, 0xB7, 0xB3, 0x2A))
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
IOCTL_BATTERY_QUERY_TAG = 0x294040
IOCTL_BATTERY_QUERY_INFORMATION = 0x294044
IOCTL_BATTERY_QUERY_STATUS = 0x29404C
LEVEL_INFORMATION, LEVEL_DEVICE_NAME, LEVEL_MANUFACTURER = 0, 4, 6
POWER_ON_LINE, DISCHARGING, CHARGING = 0x1, 0x2, 0x4
UNKNOWN_RATE = -0x80000000  # BATTERY_UNKNOWN_RATE


class _DeviceInterfaceData(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", w.DWORD), ("Reserved", ctypes.c_void_p)]


class _QueryInformation(ctypes.Structure):
    _fields_ = [("BatteryTag", w.ULONG), ("InformationLevel", ctypes.c_int), ("AtRate", ctypes.c_long)]


class _Information(ctypes.Structure):
    _fields_ = [("Capabilities", w.ULONG), ("Technology", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte * 3),
                ("Chemistry", ctypes.c_ubyte * 4), ("DesignedCapacity", w.ULONG), ("FullChargedCapacity", w.ULONG),
                ("DefaultAlert1", w.ULONG), ("DefaultAlert2", w.ULONG), ("CriticalBias", w.ULONG),
                ("CycleCount", w.ULONG)]


class _WaitStatus(ctypes.Structure):
    _fields_ = [("BatteryTag", w.ULONG), ("Timeout", w.ULONG), ("PowerState", w.ULONG), ("LowCapacity", w.ULONG),
                ("HighCapacity", w.ULONG)]


class _Status(ctypes.Structure):
    _fields_ = [("PowerState", w.ULONG), ("Capacity", w.ULONG), ("Voltage", w.ULONG), ("Rate", ctypes.c_long)]


_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), w.LPCWSTR, w.HWND, w.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), w.DWORD,
                                                  ctypes.POINTER(_DeviceInterfaceData)]
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DeviceInterfaceData),
                                                       ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
                                                       ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_kernel32.CreateFileW.restype = w.HANDLE
_kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
_kernel32.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD,
                                      ctypes.POINTER(w.DWORD), ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [w.HANDLE]
INVALID_HANDLE = w.HANDLE(-1).value


@dataclass(frozen=True)
class LaptopStatus:
    present: bool = False
    timestamp: float = 0.0
    name: str = ""  # 电池型号，如 L23N4PG1
    maker: str = ""
    percent: Optional[float] = None
    capacity_mwh: Optional[int] = None
    full_mwh: Optional[int] = None
    design_mwh: Optional[int] = None
    voltage_v: Optional[float] = None
    rate_w: Optional[float] = None  # 正 = 充入电池，负 = 电池放电
    package_w: Optional[float] = None  # 处理器封装功耗（CPU + 核显）
    system_w: Optional[float] = None  # 整机耗电
    system_measured: bool = False  # True：用电池时按放电实测；False：插电时估算（处理器 + 其余部分）
    online: bool = False  # 接着电源
    charging: bool = False
    cycles: Optional[int] = None

    @property
    def health_pct(self) -> Optional[float]:
        return self.full_mwh * 100 / self.design_mwh if self.full_mwh and self.design_mwh else None

    @property
    def key(self) -> str:
        return f"laptop|{self.maker} {self.name}".strip()


def _battery_paths() -> list[str]:
    hdev = _setupapi.SetupDiGetClassDevsW(ctypes.byref(GUID_DEVCLASS_BATTERY), None, None,
                                          DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE:
        return []
    paths: list[str] = []
    try:
        for i in range(8):
            did = _DeviceInterfaceData(cbSize=ctypes.sizeof(_DeviceInterfaceData))
            if not _setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(GUID_DEVCLASS_BATTERY), i,
                                                         ctypes.byref(did)):
                break
            need = w.DWORD()
            _setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(max(need.value, 8))
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize：x64 为 8，x86 为 6
            ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


class _PdhValue(ctypes.Structure):
    _fields_ = [("CStatus", w.DWORD), ("doubleValue", ctypes.c_double)]  # PDH_FMT_COUNTERVALUE（x64 下 8 字节对齐）


class PackagePower:
    """读 Windows「Energy Meter」性能计数器里的 RAPL 封装功耗；没有这个计数器（AMD 老机型等）时 available 为 False。"""

    def __init__(self) -> None:
        self._pdh = ctypes.WinDLL("pdh")
        self._query, self._counter = w.HANDLE(), w.HANDLE()
        self.available = (self._pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)) == 0
                          and self._pdh.PdhAddEnglishCounterW(self._query, PACKAGE_COUNTER, None,
                                                              ctypes.byref(self._counter)) == 0)
        self._value: Optional[float] = None
        self._collected_at = time.monotonic()
        if self.available:
            self._pdh.PdhCollectQueryData(self._query)

    def read(self) -> Optional[float]:
        """两次采样之间的平均功率；间隔太短（< 0.5 秒）算出来不准，沿用上一次的值。"""
        if not self.available:
            return None
        now = time.monotonic()
        if now - self._collected_at < 0.5:
            return self._value
        self._collected_at = now
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return None
        value = _PdhValue()
        if self._pdh.PdhGetFormattedCounterValue(self._counter, 0x200, None, ctypes.byref(value)) != 0:  # DOUBLE
            return None
        self._value = value.doubleValue / 1000 if value.doubleValue > 0 else None
        return self._value


class LaptopBattery:
    """只读第一块系统电池（绝大多数笔记本只有一块）。读取在界面线程里做，耗时不到 1 毫秒。"""

    def __init__(self) -> None:
        self._handle = None
        self._tag = 0
        self._static: dict = {}
        self.history: deque[tuple[float, Optional[float], Optional[float]]] = deque()  # (时间, 电池, 整机)
        self.status = LaptopStatus()
        self.present = bool(_battery_paths())
        self.package = PackagePower() if self.present else None
        self.rest_w = self._load_rest()

    @staticmethod
    def _load_rest() -> Optional[float]:
        try:
            return float(json.loads(REST_FILE.read_text(encoding="utf-8"))["rest_w"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _learn_rest(self, sample_w: float) -> None:
        """用电池时：整机 − 处理器 = 屏幕、硬盘、网卡等其余部分；平滑后保存，插电时拿来估算整机。"""
        sample_w = max(REST_MIN_W, min(REST_MAX_W, sample_w))
        self.rest_w = sample_w if self.rest_w is None else self.rest_w + REST_ALPHA * (sample_w - self.rest_w)
        try:
            REST_FILE.parent.mkdir(parents=True, exist_ok=True)
            REST_FILE.write_text(json.dumps({"rest_w": round(self.rest_w, 3), "updated": time.time()}), encoding="utf-8")
        except OSError:
            pass

    def _ioctl(self, code: int, inp, out) -> bool:
        n = w.DWORD()
        return bool(_kernel32.DeviceIoControl(self._handle, code, ctypes.byref(inp), ctypes.sizeof(inp),
                                              ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(n), None))

    def _open(self) -> bool:
        paths = _battery_paths()
        if not paths:
            return False
        h = _kernel32.CreateFileW(paths[0], 0xC0000000, 3, None, 3, 0x80, None)  # 读写、共享、OPEN_EXISTING
        if not h or h == INVALID_HANDLE:
            return False
        self._handle = h
        tag, wait = w.ULONG(), w.ULONG(0)
        if not self._ioctl(IOCTL_BATTERY_QUERY_TAG, wait, tag) or not tag.value:
            self.close()
            return False
        self._tag = tag.value
        info = _Information()
        q = _QueryInformation(BatteryTag=self._tag, InformationLevel=LEVEL_INFORMATION)
        self._static = {}
        if self._ioctl(IOCTL_BATTERY_QUERY_INFORMATION, q, info):
            self._static.update(design=info.DesignedCapacity or None, full=info.FullChargedCapacity or None,
                                cycles=info.CycleCount or None)
        for level, field in ((LEVEL_DEVICE_NAME, "name"), (LEVEL_MANUFACTURER, "maker")):
            q.InformationLevel = level
            buf = (ctypes.c_wchar * 128)()
            n = w.DWORD()
            if _kernel32.DeviceIoControl(self._handle, IOCTL_BATTERY_QUERY_INFORMATION, ctypes.byref(q),
                                         ctypes.sizeof(q), buf, ctypes.sizeof(buf), ctypes.byref(n), None):
                self._static[field] = buf.value.strip()
        return True

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
        self._handle = None

    def read(self) -> LaptopStatus:
        if not self.present:
            return self.status
        if self._handle is None and not self._open():
            return self.status
        st = _Status()
        if not self._ioctl(IOCTL_BATTERY_QUERY_STATUS, _WaitStatus(BatteryTag=self._tag), st):
            self.close()  # 电池被拔出或驱动重载，下次重新打开
            return self.status
        now = time.time()
        rate = None if st.Rate == UNKNOWN_RATE else st.Rate / 1000
        online = bool(st.PowerState & POWER_ON_LINE)
        package = self.package.read() if self.package else None
        system, measured = None, False
        if not online and rate is not None and rate < 0:
            system, measured = -rate, True
            if package is not None:
                self._learn_rest(system - package)
        elif package is not None and self.rest_w is not None:
            system = package + self.rest_w
        full = self._static.get("full")
        self.status = LaptopStatus(
            present=True, timestamp=now, name=self._static.get("name", ""), maker=self._static.get("maker", ""),
            percent=min(100.0, st.Capacity * 100 / full) if full else None, capacity_mwh=st.Capacity,
            full_mwh=full, design_mwh=self._static.get("design"), voltage_v=st.Voltage / 1000 if st.Voltage else None,
            rate_w=rate, online=online, charging=bool(st.PowerState & CHARGING),
            cycles=self._static.get("cycles"), package_w=package, system_w=system, system_measured=measured)
        self.history.append((now, rate, system))
        while self.history and self.history[0][0] < now - HISTORY_SECONDS:
            self.history.popleft()
        return self.status

    def minutes_left(self, s: LaptopStatus) -> Optional[float]:
        """充电时到充满、放电时到用完的分钟数（按当前功率估算）。"""
        if not s.rate_w or s.capacity_mwh is None or not s.full_mwh:
            return None
        if s.rate_w > 0.3:
            return (s.full_mwh - s.capacity_mwh) / 1000 / s.rate_w * 60
        if s.rate_w < -0.3:
            return s.capacity_mwh / 1000 / -s.rate_w * 60
        return None
