"""iPhone 状态监测：任务栏托盘图标 + 详情小窗。

左键单击托盘图标打开详情，右键菜单可切换图标显示内容、刷新间隔、开机自启。
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import pystray
from PIL import Image, ImageDraw, ImageFont

from i18n import LANGUAGES, tr
from monitor import FROZEN, LOG_DIR, DevStats, Monitor, Snapshot
from insights import CHARGE_LIMITS, ChargeInsight, Insights
from laptop_battery import LaptopBattery, LaptopStatus
import battery_report
from bt_audio import BluetoothAudio
from detail_window import DetailWindow
from taskbar_strip import TaskbarStrip
from ui_common import charge_state, enable_per_monitor_dpi, fmt, format_minutes, laptop_state, taskbar_dpi

APP_NAME = "iPhoneMonitor"
__version__ = "1.4.1"
HERE = Path(__file__).resolve().parent
SETTINGS_FILE = Path(os.environ.get("APPDATA", HERE)) / APP_NAME / "settings.json"
DEFAULT_SETTINGS = {
    "icon_mode": "percent", "interval": 2.0, "topmost": True, "dev_stats": True, "usb_only": True,
    "taskbar_strip": True,  # 任务栏里显示「图标 + 电量 + 功率/CPU」文字条
    "strip_cpu": True,  # 文字条第二行显示 CPU 和内存（会让开发者通道常开）
    "tray_icon": False,  # 有文字条时默认不显示托盘图标
    "strip_position": "right",  # 文字条优先放的一侧：right=托盘旁，left=任务栏左侧小组件旁
    "strip_laptop": True,  # 没插手机时文字条改显示本机电池（台式机没有电池时照旧收起）
    "bt_audio": False,  # 手机声音通过蓝牙转到电脑播放
    "charge_limit": 100,  # 与 iPhone「设置 → 电池 → 充电」里的上限保持一致；100 表示不限
    "language": "auto",  # auto（跟随 Windows 显示语言）/ zh / en
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

LOW_BATTERY = 20
HOT_TEMPERATURE = 40.0
DISCHARGE_THRESHOLD_W = 0.1  # 电池功率低于 -0.1 W 才算放电，避免 0 附近抖动

log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------- 基础设施

def setup_logging() -> None:
    # pythonw 下没有控制台，避免第三方库写 stdout 时报错
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "monitor.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


SHOW_EVENT = f"Local\\{APP_NAME}.Show"  # 再次启动时用它通知已在运行的实例打开详情窗口


RESTART_ARG = "--restart"  # 切换语言、显示缩放变化后重启：新进程等旧进程退出再接手
SHOW_ARG = "--show"  # 重启后直接打开详情窗口
RESTART_WAIT_SECONDS = 15
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_mutex: Optional[int] = None


def single_instance() -> bool:
    global _mutex
    handle = _kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        _kernel32.CloseHandle(handle)  # 不关的话旧进程退出后互斥量也不会消失
        return False
    _mutex = handle
    return True


def wait_single_instance(seconds: float) -> bool:
    deadline = time.time() + seconds
    while not single_instance():
        if time.time() > deadline:
            return False
        time.sleep(0.2)
    return True


def restart_command(show: bool) -> list[str]:
    args = [RESTART_ARG] + ([SHOW_ARG] if show else [])
    if FROZEN:
        return [sys.executable, *args]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else sys.executable), str(HERE / "app.py"), *args]


def signal_running_instance() -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.restype = ctypes.c_void_p
    handle = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if handle:
        kernel32.SetEvent(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("保存设置失败: %r", exc)


def autostart_command() -> str:
    if FROZEN:  # 打包后的 exe 直接启动自己
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{HERE / "app.py"}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass




STRIP_HIDE_AFTER_SECONDS = 5
STRIP_POSITIONS = (("right", "靠右（托盘旁）"), ("left", "靠左（小组件旁）"))
# 语言菜单两种语言都写上，选错了语言也能找回来
LANGUAGE_MENU = "语言 / Language"
LANGUAGE_LABEL = {"auto": "跟随系统 / System default"}
TICK_VISIBLE_MS = 500
TICK_IDLE_MS = 1000
LAPTOP_FAST_SECONDS, LAPTOP_IDLE_SECONDS = 2, 20
LAPTOP_STRIP_SECONDS = 5  # 文字条在显示本机电池时  # 本机电池：页面开着时 2 秒读一次，否则 20 秒（够统计损耗和提醒）
LAPTOP_DRAIN_W = 1.0  # 插着电源时电池放电超过这么多才算「插电仍掉电」
LAPTOP_DRAIN_ALERT_SECONDS = 120
REPORT_FIRST_DELAY, REPORT_CHECK_SECONDS = 30, 3600  # Windows 电池报告：启动后 30 秒导入，之后每小时看日期变没变
DPI_SETTLE_SECONDS = 3
TETHER_CHECK_SECONDS = 15
# iPhone 开着个人热点时，USB 网卡会拿到 172.20.10.x 并可能抢走默认路由。
# 只在外网流量真的走这块网卡时才算「接管」：调高了网卡跃点数的电脑上热点在但不生效，不报警。
# 直接调 iphlpapi，每次约 0.2 ms；之前用 PowerShell 每次要 3.5 秒 CPU。
ROUTE_PROBE = "1.1.1.1"
MIB_IF_ROW2_SIZE = 1352
MIB_IF_ROW2_DESCRIPTION = 542  # NET_LUID(8) + InterfaceIndex(4) + GUID(16) + Alias WCHAR[257]
APPLE_TETHER_ADAPTER = "Apple Mobile Device Ethernet"


def iphone_tethering_ip() -> Optional[str]:
    """外网流量正通过 iPhone 的 USB 网卡（个人热点）出去时返回电脑分到的 IP，否则返回 None。"""
    try:
        iphlpapi = ctypes.windll.iphlpapi
        index = ctypes.c_ulong()
        dest = struct.unpack("<I", socket.inet_aton(ROUTE_PROBE))[0]
        if iphlpapi.GetBestInterface(dest, ctypes.byref(index)) != 0:
            return None
        row = ctypes.create_string_buffer(MIB_IF_ROW2_SIZE + 64)
        struct.pack_into("<I", row, 8, index.value)
        if iphlpapi.GetIfEntry2(row) != 0:
            return None
        if not ctypes.wstring_at(ctypes.addressof(row) + MIB_IF_ROW2_DESCRIPTION).startswith(APPLE_TETHER_ADAPTER):
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ROUTE_PROBE, 53))  # UDP connect 不发包，只让系统选出口地址
            return s.getsockname()[0]
    except OSError:
        return None




# ---------------------------------------------------------------- 格式化









def strip_stats_note(d: DevStats) -> str:
    """CPU / 内存读不到时，文字条第二行显示的简短原因。"""
    if d.active:
        return ""
    if d.status == tr("需在 iPhone 上开启开发者模式"):
        return tr("需开发者模式")
    if d.status in (tr("挂载镜像超时：请解锁 iPhone，稍后自动重试"), tr("正在挂载开发者镜像（请保持手机解锁）…")):
        return tr("解锁手机读取")
    if d.status == tr("需用 USB 线连接（Wi-Fi 下不支持）"):
        return tr("需 USB 连接")
    return ""


def tooltip(s: Snapshot, d: DevStats, tether_ip: Optional[str] = None, limit: Optional[int] = 127) -> str:
    """托盘图标的提示最多 127 个字符；文字条的悬停提示不限长度（limit=None）。"""
    text = _tooltip(s, d)
    if tether_ip:
        text = tr("⚠ 手机热点正在接管电脑网络") + "\n" + text
    return text[:limit]


def laptop_tooltip(s: LaptopStatus, minutes: Optional[float]) -> str:
    lines = [f"{tr('本机电池')} · {s.percent:.0f}% · {laptop_state(s)}" if s.percent is not None
             else tr("本机电池")]
    if s.rate_w is not None:
        lines.append(tr("电池 {w}", w=fmt(s.rate_w, "W", signed=True)))
    if s.system_w is not None:
        lines.append(tr("整机耗电 {w}", "laptop", w=("" if s.system_measured else "≈ ") + fmt(s.system_w, "W")))
    if minutes is not None and minutes < 24 * 60 and s.rate_w:
        lines.append(tr("约 {time}充满", time=format_minutes(minutes)) if s.rate_w > 0
                     else tr("剩余约 {time}", time=format_minutes(minutes)))
    return "\n".join(lines)


def laptop_strip_note(s: LaptopStatus, minutes: Optional[float]) -> str:
    """文字条第二行：用电池时显示还能用多久（时:分），否则显示电源状态。"""
    if s.charging:
        return tr("充电中")
    if s.online:
        return tr("插电仍耗电") if s.rate_w is not None and s.rate_w < -0.3 else tr("已接电源")
    if minutes is not None and minutes < 24 * 60:
        return tr("剩余 {time}", time=f"{int(minutes // 60)}:{int(minutes % 60):02d}")
    return tr("使用电池")


def _tooltip(s: Snapshot, d: DevStats) -> str:
    if not s.connected:
        name = s.name or "iPhone"
        return tr("{name}：{status}", name=name, status=s.status)
    battery = tr("电池 {w}", w=fmt(s.battery_w, "W", signed=True))
    if s.temperature_c is not None:
        battery += f" · {fmt(s.temperature_c, '°C')}"
    lines = [f"{s.name} · {s.percent}% · {charge_state(s)}", battery]
    if s.system_load_w is not None:
        lines.append(tr("整机耗电 {w}", w=fmt(s.system_load_w, "W")))
    if d.active and d.cpu_pct is not None:
        mem = tr(" · 内存 {pct}%", pct=f"{d.mem_used * 100 / d.mem_total:.0f}") if d.mem_used and d.mem_total else ""
        lines.append(f"CPU {d.cpu_pct:.0f}%{mem}")
    lines.append(tr("连接：{conn}", conn=s.connection))
    return "\n".join(lines)


# ---------------------------------------------------------------- 托盘图标

def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_icon(s: Snapshot, mode: str, d: Optional[DevStats] = None) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    showing_percent = False

    if not s.connected:
        bg, text = (120, 120, 120), "--"
    elif mode == "cpu" and d is not None and d.active and d.cpu_pct is not None:
        bg = (214, 69, 69) if d.cpu_pct >= 80 else (110, 90, 190)
        text = f"{d.cpu_pct:.0f}"
    elif mode == "power" and s.battery_w is not None:
        bg = (46, 158, 91) if s.battery_w >= 0 else (224, 130, 40)
        w = abs(s.battery_w)
        text = f"{w:.1f}" if w < 10 else f"{w:.0f}"
    elif mode == "temp" and s.temperature_c is not None:
        hot = s.temperature_c >= HOT_TEMPERATURE
        bg = (214, 69, 69) if hot else (90, 110, 140)
        text = f"{s.temperature_c:.0f}"
    else:
        pct = s.percent if s.percent is not None else 0
        if s.charging or s.fully_charged:
            bg = (46, 158, 91)
        elif pct <= LOW_BATTERY:
            bg = (214, 69, 69)
        else:
            bg = (58, 123, 213)
        text = str(pct) if s.percent is not None else "?"
        showing_percent = True

    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=14, fill=bg)

    font_size = 52
    while font_size > 16:
        font = _font(font_size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= size - 6 and bottom - top <= size - 10:
            break
        font_size -= 2
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = (size - (right - left)) / 2 - left
    y = (size - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=(255, 255, 255))

    # 充电时底部加一条白色细线作为标记
    if showing_percent and s.external_power:
        draw.rounded_rectangle((16, size - 7, size - 16, size - 3), radius=2, fill=(255, 255, 255))
    return img


# ---------------------------------------------------------------- 主程序

class App:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.monitor = Monitor(
            interval=float(self.settings["interval"]),
            dev_enabled=bool(self.settings["dev_stats"]),
            usb_only=bool(self.settings["usb_only"]),
        )
        self.root = tk.Tk()
        # 界面按任务栏所在屏幕的缩放来画（Tk 的字号是磅，换算比例跟着 DPI）；变了就重启重新布局
        self.dpi = taskbar_dpi()
        self.root.tk.call("tk", "scaling", self.dpi / 72)
        self.dpi_changed_since: Optional[float] = None
        log.info("任务栏所在屏幕 %s DPI（缩放 %d%%）", self.dpi, round(self.dpi / 96 * 100))
        # pythonw 下没有 stderr，Tk 回调里的异常默认会被吞掉，记到日志里
        self.root.report_callback_exception = lambda *exc: log.error("界面回调出错", exc_info=exc)
        self.laptop = LaptopBattery()
        self.laptop_read_at = 0.0
        self.laptop_status: Optional[LaptopStatus] = None
        self.strip_device = "phone"  # 文字条当前显示的是手机还是本机电池
        self.laptop_drain_since: Optional[float] = None
        self.laptop_alerted = False
        self.report_check_at = time.time() + REPORT_FIRST_DELAY
        self.window = DetailWindow(self.root, self.monitor, self.settings, on_usb_only=self.set_usb_only,
                                   charge_limit=lambda: int(self.settings["charge_limit"]),
                                   dock_side=self.window_side, laptop=self.laptop)
        self.insights = Insights(lambda: int(self.settings["charge_limit"]))
        self.bt = BluetoothAudio(lambda: self.monitor.snapshot().name, enabled=bool(self.settings["bt_audio"]))
        self.bt_was_connected = False
        self.usb_was_connected = False
        self.insight_alerted: set[str] = set()
        self.root.title(f"{tr('iPhone 状态')} · v{__version__}")
        self.strip = TaskbarStrip(self.root, on_left_click=self.window.toggle, on_right_click=self.show_strip_menu)
        self.strip_menu = self.build_strip_menu()
        # 详情窗口上右键也弹同一个菜单（托盘图标和文字条都不在时的入口）
        self.root.bind("<Button-3>", lambda e: self.show_strip_menu(e.x_root, e.y_root))
        self.disconnected_since: Optional[float] = None
        self.last_side = "right"
        self.temp_tray_until = 0.0
        self.tray_ready = False
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = ctypes.c_void_p
        self.show_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT)  # 自动复位
        self.tasks: "queue.Queue[callable]" = queue.Queue()
        self.last_ts = -1.0
        self.last_dev: Optional[DevStats] = None
        self.tether_ip: Optional[str] = None
        self.shown_tether: Optional[str] = None
        self.stop_event = threading.Event()
        self.last_key: tuple = ()
        self.alerted_low = False
        self.alerted_hot = False
        self.alerted_full = False

        self.icon = pystray.Icon(
            APP_NAME, render_icon(Snapshot(), self.settings["icon_mode"]), tr("iPhone 状态：正在连接…"), menu=self.build_menu()
        )

    def ui(self, fn) -> None:
        """托盘回调在 pystray 线程里执行，界面操作统一转到 Tk 主线程。"""
        self.tasks.put(fn)
        # tick 间隔调长后，点托盘图标要立刻响应：请 Tk 主线程马上处理（Tcl 为多线程版本时可跨线程调用）
        try:
            self.root.after(0, self.drain_tasks)
        except (RuntimeError, tk.TclError):
            pass  # 下一次 tick 会处理

    def build_menu(self) -> pystray.Menu:
        item = pystray.MenuItem

        def mode_item(label: str, mode: str):
            enabled = True
            if mode == "temp":
                enabled = lambda _i: self.monitor.snapshot().temperature_c is not None  # noqa: E731
            elif mode == "cpu":
                enabled = lambda _i: bool(self.settings["dev_stats"])  # noqa: E731
            return item(
                label, lambda: self.set_mode(mode), checked=lambda _i: self.settings["icon_mode"] == mode,
                radio=True, enabled=enabled,
            )

        def interval_item(label: str, seconds: float):
            return item(
                label, lambda: self.set_interval(seconds),
                checked=lambda _i: float(self.settings["interval"]) == seconds, radio=True,
            )

        return pystray.Menu(
            item(tr("显示详情"), lambda: self.ui(self.window.toggle), default=True),
            item(tr("立即刷新"), lambda: self.monitor.refresh_now()),
            pystray.Menu.SEPARATOR,
            item(tr("图标显示"), pystray.Menu(
                mode_item(tr("电量 %"), "percent"), mode_item(tr("电池功率 W"), "power"),
                mode_item("CPU %", "cpu"), mode_item(tr("温度 °C"), "temp"),
            )),
            item(tr("任务栏文字条"), lambda: self.set_taskbar_strip(not self.settings["taskbar_strip"]),
                 checked=lambda _i: bool(self.settings["taskbar_strip"])),
            item(tr("文字条显示 CPU / 内存"), lambda: self.set_strip_cpu(not self.settings["strip_cpu"]),
                 checked=lambda _i: bool(self.settings["strip_cpu"]),
                 enabled=lambda _i: bool(self.settings["taskbar_strip"])),
            item(tr("没插手机时显示本机电池"),
                 lambda: self.set_strip_laptop(not self.settings.get("strip_laptop", True)),
                 checked=lambda _i: bool(self.settings.get("strip_laptop", True)),
                 enabled=lambda _i: bool(self.settings["taskbar_strip"]) and self.laptop.present),
            item(tr("文字条位置"), pystray.Menu(*[
                item(tr(label), (lambda v=value: lambda: self.set_strip_position(v))(),
                     checked=(lambda v=value: lambda _i: self.settings.get("strip_position", "right") == v)(),
                     radio=True)
                for value, label in STRIP_POSITIONS
            ]), enabled=lambda _i: bool(self.settings["taskbar_strip"])),
            item(tr("显示托盘图标"), lambda: self.set_tray_icon(not self.settings["tray_icon"]),
                 checked=lambda _i: bool(self.settings["tray_icon"]),
                 enabled=lambda _i: bool(self.settings["taskbar_strip"])),
            item(tr("CPU / 内存监测"), lambda: self.toggle_dev(), checked=lambda _i: bool(self.settings["dev_stats"])),
            item(
                tr("仅通过 USB 连接"), lambda: self.set_usb_only(not self.settings["usb_only"]),
                checked=lambda _i: bool(self.settings["usb_only"]),
            ),
            item(tr("充电上限（与手机设置一致）"), pystray.Menu(*[
                item(tr("不限（100%）") if v == 100 else f"{v}%", (lambda v=v: lambda: self.set_charge_limit(v))(),
                     checked=(lambda v=v: lambda _i: int(self.settings["charge_limit"]) == v)(), radio=True)
                for v in CHARGE_LIMITS
            ])),
            item(tr("手机声音转到电脑（蓝牙）"), lambda: self.set_bt_audio(not self.settings["bt_audio"]),
                 checked=lambda _i: bool(self.settings["bt_audio"])),
            item(tr("重新连接手机声音"), lambda: self.bt.reconnect(),
                 enabled=lambda _i: bool(self.settings["bt_audio"])),
            item(tr("刷新间隔"), pystray.Menu(*[interval_item(tr("{n} 秒", n=n), float(n)) for n in (1, 2, 5, 10)])),
            item(LANGUAGE_MENU, pystray.Menu(*[
                item(LANGUAGE_LABEL.get(code, label), (lambda c=code: lambda: self.set_language(c))(),
                     checked=(lambda c=code: lambda _i: self.settings.get("language", "auto") == c)(), radio=True)
                for code, label in LANGUAGES
            ])),
            item(tr("详情窗口置顶"), lambda: self.toggle_topmost(), checked=lambda _i: bool(self.settings["topmost"])),
            item(tr("开机自动启动"), lambda: self.toggle_autostart(), checked=lambda _i: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item(tr("退出"), lambda: self.ui(self.quit)),
        )

    def set_mode(self, mode: str) -> None:
        self.settings["icon_mode"] = mode
        save_settings(self.settings)
        self.last_key = ()  # 强制重绘图标

    def set_interval(self, seconds: float) -> None:
        self.settings["interval"] = seconds
        self.monitor.interval = seconds
        save_settings(self.settings)
        self.monitor.refresh_now()

    def toggle_topmost(self) -> None:
        self.settings["topmost"] = not self.settings["topmost"]
        save_settings(self.settings)
        self.ui(lambda: self.window.set_topmost(bool(self.settings["topmost"])))

    def toggle_dev(self) -> None:
        self.settings["dev_stats"] = not self.settings["dev_stats"]
        self.monitor.dev_enabled = bool(self.settings["dev_stats"])
        save_settings(self.settings)
        self.last_key = ()

    def set_taskbar_strip(self, on: bool) -> None:
        self.settings["taskbar_strip"] = on
        save_settings(self.settings)
        if not on:
            self.ui(self.strip.destroy)  # 显示时由 tick 里的 ensure() 挂载
        self.icon.update_menu()

    def set_strip_cpu(self, on: bool) -> None:
        self.settings["strip_cpu"] = on
        save_settings(self.settings)
        self.icon.update_menu()

    def set_strip_laptop(self, on: bool) -> None:
        self.settings["strip_laptop"] = on
        save_settings(self.settings)
        self.icon.update_menu()

    def window_side(self) -> str:
        """详情窗口贴哪个角：文字条正显示在左边就贴左下角，否则贴右下角。"""
        strip_shown = self.settings["taskbar_strip"] and self.strip.hwnd and self.strip.fits
        return self.strip.side if strip_shown else "right"

    def set_strip_position(self, position: str) -> None:
        self.settings["strip_position"] = position
        save_settings(self.settings)
        self.icon.update_menu()

    def set_tray_icon(self, on: bool) -> None:
        self.settings["tray_icon"] = on
        save_settings(self.settings)
        self.icon.update_menu()

    def tray_wanted(self) -> bool:
        """托盘图标默认不显示；文字条关掉时自动显示，保证程序总有入口。"""
        strip_hidden = bool(self.strip.hwnd) and not self.strip.fits  # 任务栏图标太多，文字条没地方放
        return bool(self.settings["tray_icon"]) or not self.settings["taskbar_strip"] or strip_hidden

    def sync_tray(self) -> None:
        want = self.tray_wanted() or time.time() < self.temp_tray_until
        if self.icon.visible != want:
            self.icon.visible = want

    def tray_setup(self, icon: pystray.Icon) -> None:
        """pystray 就绪后调用（默认会直接显示图标，这里按设置决定）。"""
        icon.visible = self.tray_wanted()
        self.tray_ready = True

    def notify(self, message: str, title: str) -> None:
        """托盘图标隐藏时临时显示 10 秒来弹通知（Windows 通知必须挂在托盘图标上）。"""
        self.temp_tray_until = time.time() + 10
        if not self.icon.visible:
            self.icon.visible = True
        self.icon.notify(message, title)

    def build_strip_menu(self) -> tk.Menu:
        """文字条/详情窗口的右键菜单：包含原托盘菜单里的全部设置。"""
        menu = tk.Menu(self.root, tearoff=0)
        self.menu_vars = {k: tk.BooleanVar() for k in ("strip_cpu", "strip_laptop", "dev_stats", "usb_only",
                                                         "topmost", "autostart", "tray_icon", "bt_audio")}
        self.menu_interval = tk.DoubleVar()
        v = self.menu_vars
        menu.add_command(label=tr("显示 / 隐藏详情"), command=self.window.toggle)
        menu.add_checkbutton(label=tr("手机声音转到电脑（蓝牙）"), variable=self.menu_vars["bt_audio"],
                             command=lambda: self.set_bt_audio(self.menu_vars["bt_audio"].get()))
        menu.add_command(label=tr("重新连接手机声音"), command=self.bt.reconnect)
        menu.add_separator()
        menu.add_checkbutton(label=tr("第二行显示 CPU / 内存"), variable=v["strip_cpu"],
                             command=lambda: self.set_strip_cpu(v["strip_cpu"].get()))
        menu.add_checkbutton(label=tr("没插手机时显示本机电池"), variable=v["strip_laptop"],
                             command=lambda: self.set_strip_laptop(v["strip_laptop"].get()))
        menu.add_checkbutton(label=tr("CPU / 内存监测"), variable=v["dev_stats"], command=self.toggle_dev)
        menu.add_checkbutton(label=tr("仅通过 USB 连接"), variable=v["usb_only"],
                             command=lambda: self.set_usb_only(v["usb_only"].get()))
        interval = tk.Menu(menu, tearoff=0)
        for seconds in (1.0, 2.0, 5.0, 10.0):
            interval.add_radiobutton(label=tr("{n} 秒", n=int(seconds)), value=seconds, variable=self.menu_interval,
                                     command=lambda s=seconds: self.set_interval(s))
        menu.add_cascade(label=tr("刷新间隔"), menu=interval)
        limits = tk.Menu(menu, tearoff=0)
        self.menu_limit = tk.IntVar()
        for v_limit in CHARGE_LIMITS:
            limits.add_radiobutton(label=tr("不限（100%）") if v_limit == 100 else f"{v_limit}%", value=v_limit,
                                   variable=self.menu_limit, command=lambda x=v_limit: self.set_charge_limit(x))
        menu.add_cascade(label=tr("充电上限（与手机设置一致）"), menu=limits)
        positions = tk.Menu(menu, tearoff=0)
        self.menu_position = tk.StringVar()
        for value, label in STRIP_POSITIONS:
            positions.add_radiobutton(label=tr(label), value=value, variable=self.menu_position,
                                      command=lambda v=value: self.set_strip_position(v))
        menu.add_cascade(label=tr("文字条位置"), menu=positions)
        languages = tk.Menu(menu, tearoff=0)
        self.menu_language = tk.StringVar()
        for code, label in LANGUAGES:
            languages.add_radiobutton(label=LANGUAGE_LABEL.get(code, label), value=code, variable=self.menu_language,
                                      command=lambda c=code: self.set_language(c))
        menu.add_cascade(label=LANGUAGE_MENU, menu=languages)
        menu.add_checkbutton(label=tr("详情窗口置顶"), variable=v["topmost"], command=self.toggle_topmost)
        menu.add_checkbutton(label=tr("开机自动启动"), variable=v["autostart"], command=self.toggle_autostart)
        menu.add_checkbutton(label=tr("显示托盘图标"), variable=v["tray_icon"],
                             command=lambda: self.set_tray_icon(v["tray_icon"].get()))
        menu.add_separator()
        menu.add_command(label=tr("隐藏任务栏文字条（改用托盘图标）"), command=lambda: self.set_taskbar_strip(False))
        menu.add_command(label=tr("退出"), command=self.quit)
        return menu

    def show_strip_menu(self, x: int, y: int) -> None:
        # 设置可能在别处（托盘菜单、详情窗口）改过，弹出前同步勾选状态
        for key, var in self.menu_vars.items():
            var.set(autostart_enabled() if key == "autostart" else bool(self.settings.get(key, True)))
        self.menu_interval.set(float(self.settings["interval"]))
        self.menu_limit.set(int(self.settings["charge_limit"]))
        self.menu_position.set(self.settings.get("strip_position", "right"))
        self.menu_language.set(self.settings.get("language", "auto"))
        try:
            self.strip_menu.tk_popup(x, y)
        finally:
            self.strip_menu.grab_release()

    def update_strip(self, s: Snapshot, d: DevStats) -> None:
        if not self.settings["taskbar_strip"]:
            return
        # 没插手机时改显示本机电池（台式机没有电池、或关了这个选项就整块收起）；
        # 断开 5 秒后再切换，避免重连时一闪一闪
        if not s.connected:
            if self.disconnected_since is None:
                self.disconnected_since = time.time()
            if time.time() - self.disconnected_since >= STRIP_HIDE_AFTER_SECONDS or not self.strip.hwnd:
                if self.settings.get("strip_laptop", True) and self.laptop_status is not None:
                    self.strip_device = "laptop"
                    self.update_strip_laptop(self.laptop_status, s, d)
                else:
                    self.strip_device = "phone"
                    self.strip.destroy()
                return
        else:
            self.disconnected_since = None
        self.strip_device = "phone"
        self.strip.position = self.settings.get("strip_position", "right")
        self.strip.ensure()
        power = s.system_load_w if s.system_load_w is not None else (abs(s.battery_w) if s.battery_w is not None else None)
        mem_pct = d.mem_used * 100 / d.mem_total if d.active and d.mem_used and d.mem_total else None
        self.strip.update(
            connected=s.connected,
            percent=s.percent,
            charging=bool(s.charging),
            external_power=bool(s.external_power),
            # 插着线也可能在耗电池（比如电脑 USB 口供电不够、或停在充电上限时），这时图标变红
            discharging=s.battery_w is not None and s.battery_w < -DISCHARGE_THRESHOLD_W,
            low=s.percent is not None and s.percent <= LOW_BATTERY and not s.external_power,
            power_w=power,
            cpu_pct=d.cpu_pct if d.active else None,
            mem_pct=mem_pct,
            show_stats=bool(self.settings["strip_cpu"]) and bool(self.settings["dev_stats"]),
            warning=tr("热点接管了电脑网络") if self.tether_ip else "",
            stats_note=strip_stats_note(d),
        )
        tip = tooltip(s, d, self.tether_ip, limit=None)
        if self.laptop_status is not None and self.laptop_status.percent is not None:
            ls = self.laptop_status
            tip += "\n" + f"{tr('本机电池')} · {ls.percent:.0f}% · {laptop_state(ls)}"
        self.strip.set_tooltip(tip)

    def update_strip_laptop(self, ls: LaptopStatus, s: Snapshot, d: DevStats) -> None:
        self.strip.position = self.settings.get("strip_position", "right")
        self.strip.ensure()
        minutes = self.laptop.minutes_left(ls)
        power = ls.system_w if ls.system_w is not None else (abs(ls.rate_w) if ls.rate_w is not None else None)
        self.strip.update(
            connected=ls.percent is not None,
            percent=None if ls.percent is None else round(ls.percent),
            charging=bool(ls.charging),
            external_power=bool(ls.online),
            discharging=ls.rate_w is not None and ls.rate_w < -0.3,
            low=ls.percent is not None and ls.percent <= LOW_BATTERY and not ls.online,
            power_w=power,
            cpu_pct=None,
            mem_pct=None,
            show_stats=False,
            device="laptop",
            note=laptop_strip_note(ls, minutes),
        )
        # 悬停时也带上手机的状态（连接中 / 未连接），一眼知道为什么没显示手机
        self.strip.set_tooltip(laptop_tooltip(ls, minutes) + "\n" + tooltip(s, d, limit=None))

    def set_usb_only(self, on: bool) -> None:
        """面板勾选框和托盘菜单共用；可能在 pystray 线程里调用，界面同步交给 Tk 线程。"""
        self.settings["usb_only"] = on
        self.monitor.usb_only = on
        save_settings(self.settings)
        self.monitor.refresh_now()
        self.ui(lambda: self.window.sync_usb_only(on))
        self.icon.update_menu()

    def toggle_autostart(self) -> None:
        try:
            set_autostart(not autostart_enabled())
        except OSError as exc:
            log.warning("设置开机启动失败: %r", exc)
        self.icon.update_menu()

    def check_alerts(self, s: Snapshot) -> None:
        if not s.connected:
            return
        if s.percent is not None:
            if s.percent <= LOW_BATTERY and not s.external_power:
                if not self.alerted_low:
                    self.notify(tr("电量只剩 {pct}%，记得充电", pct=s.percent), tr("iPhone 电量低"))
                    self.alerted_low = True
            elif s.percent > LOW_BATTERY + 5 or s.external_power:
                self.alerted_low = False
        if s.temperature_c is not None:
            if s.temperature_c >= HOT_TEMPERATURE and not self.alerted_hot:
                self.notify(tr("电池温度 {t}°C，建议暂停重度使用或移开充电器", t=f"{s.temperature_c:.1f}"),
                            tr("iPhone 温度偏高"))
                self.alerted_hot = True
            elif s.temperature_c < HOT_TEMPERATURE - 3:
                self.alerted_hot = False
        if s.fully_charged and s.external_power:
            if not self.alerted_full:
                self.notify(tr("电池已充满"), "iPhone")
                self.alerted_full = True
        elif not s.external_power:
            self.alerted_full = False

    def watch_tethering(self) -> None:
        """后台线程：定期检查 iPhone 个人热点是否通过 USB 接管了电脑网络。"""
        while not self.stop_event.is_set():
            self.tether_ip = iphone_tethering_ip()
            self.stop_event.wait(TETHER_CHECK_SECONDS)

    def check_tethering(self) -> None:
        ip = self.tether_ip
        if ip == self.shown_tether:
            return
        if ip:
            log.warning("iPhone 个人热点正在通过 USB 接管电脑网络: %s", ip)
            self.notify(
                tr("电脑正通过 USB 使用手机热点（{ip}），单位内网可能打不开。关闭 iPhone 的「个人热点」即可。", ip=ip),
                tr("iPhone 热点接管了电脑网络"),
            )
        self.window.set_tether(ip)
        self.shown_tether = ip
        self.last_ts = -1.0  # 刷新提示文字

    def poll_laptop(self) -> None:
        """本机电池：统计损耗、插电仍掉电提醒，详情窗口开在「本机电池」页时刷新界面。"""
        if not self.laptop.present:
            return
        fast = self.window.visible and self.window.page_name == "laptop"
        if fast:
            interval = LAPTOP_FAST_SECONDS
        elif self.strip_device == "laptop" and self.strip.hwnd:
            interval = LAPTOP_STRIP_SECONDS
        else:
            interval = LAPTOP_IDLE_SECONDS
        if time.time() - self.laptop_read_at < interval:
            return
        self.laptop_read_at = time.time()
        s = self.laptop.read()
        if not s.present:
            return
        self.laptop_status = s
        self.insights.wear.feed_laptop(s)
        self.check_laptop_drain(s)
        if fast:
            self.window.update_laptop(s)

    def maybe_import_report(self) -> None:
        if not self.laptop.present or time.time() < self.report_check_at:
            return
        self.report_check_at = time.time() + REPORT_CHECK_SECONDS
        today = time.strftime("%Y-%m-%d")
        if self.settings.get("battery_report_date") == today:
            return

        def work() -> None:
            root = battery_report.run_report()
            data = battery_report.parse(root) if root is not None else None
            if data:
                battery_report.save_periods(data["key"], data["periods"])
                self.ui(lambda: self.apply_report(data, today))

        threading.Thread(target=work, name="battery-report", daemon=True).start()

    def apply_report(self, data: dict, today: str) -> None:
        self.insights.wear.import_laptop_days(data["key"], data["days"])
        self.settings["battery_report_date"] = today
        save_settings(self.settings)
        log.info("已导入 Windows 电池报告：%d 天放电记录、%d 段长期记录", len(data["days"]), len(data["periods"]))

    def check_laptop_drain(self, s: LaptopStatus) -> None:
        draining = s.online and s.rate_w is not None and s.rate_w < -LAPTOP_DRAIN_W
        if not draining:
            self.laptop_drain_since, self.laptop_alerted = None, False
            return
        self.laptop_drain_since = self.laptop_drain_since or time.time()
        if not self.laptop_alerted and time.time() - self.laptop_drain_since >= LAPTOP_DRAIN_ALERT_SECONDS:
            self.notify(tr("电脑插着电源，电池仍在以 {w} W 放电。充电器功率可能不够，或者接在了扩展坞、低功率接口上。",
                           w=f"{-s.rate_w:.1f}"), tr("电脑插电仍掉电"))
            self.laptop_alerted = True

    def check_charge_alerts(self, ins: ChargeInsight) -> None:
        """插着线掉电 / 慢充持续一段时间后各提醒一次；情况消失后才会再次提醒。"""
        if ins.alert and ins.alert not in self.insight_alerted:
            drain = ins.alert == "drain"
            title = tr("iPhone 插电仍掉电") if drain else tr("iPhone 充电慢")
            hint = tr("换一根短而好的线，或用 20 W 以上充电头。") if drain else tr("用 20 W 以上充电头会快很多。")
            self.notify(ins.diagnosis + "\n" + hint, title)
            self.insight_alerted.add(ins.alert)
        elif not ins.diagnosis:
            self.insight_alerted.clear()

    def set_bt_audio(self, on: bool) -> None:
        self.settings["bt_audio"] = on
        save_settings(self.settings)
        self.bt.set_enabled(on)
        self.icon.update_menu()

    def check_bt_audio(self) -> None:
        usb = self.monitor.snapshot().connected
        if usb and not self.usb_was_connected and self.bt.enabled and not self.bt.connected:
            self.bt.kick()  # 手机刚插上 USB，人就在电脑旁：蓝牙音频没连上的话马上重试
        self.usb_was_connected = usb
        connected = self.bt.connected
        if connected and not self.bt_was_connected:
            self.notify(tr("手机声音会从电脑播放（{name}）。没声音的话，在 iPhone 控制中心的音频输出里选这台电脑。",
                           name=self.bt.device_name), tr("手机声音已转到电脑"))
        self.bt_was_connected = connected
        if self.window.visible:
            self.window.set_bt(self.bt.status if self.bt.enabled else tr("已关闭（右键菜单可开启）"), connected)

    def set_charge_limit(self, limit: int) -> None:
        self.settings["charge_limit"] = limit
        save_settings(self.settings)
        self.icon.update_menu()

    def set_language(self, code: str) -> None:
        """界面文字在启动时确定，换语言就重启程序（可能在 pystray 线程里调用）。"""
        if code == self.settings.get("language", "auto"):
            return
        self.settings["language"] = code
        save_settings(self.settings)
        self.ui(lambda: self.restart("切换语言", show=True))

    def restart(self, reason: str, show: bool) -> None:
        log.info("%s，重启程序", reason)
        try:
            subprocess.Popen(restart_command(show), close_fds=True)
        except OSError as exc:
            log.warning("重启失败: %r", exc)
            return
        self.quit()

    def drain_tasks(self) -> None:
        while True:
            try:
                self.tasks.get_nowait()()
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001
                log.exception("界面任务出错")

    def check_dpi(self) -> bool:
        """显示缩放或任务栏所在屏幕变了（接显示器、改缩放比例），稳定 3 秒后重启按新尺寸布局。"""
        dpi = taskbar_dpi()
        if dpi == self.dpi:
            self.dpi_changed_since = None
            return False
        self.dpi_changed_since = self.dpi_changed_since or time.time()
        if time.time() - self.dpi_changed_since < DPI_SETTLE_SECONDS:
            return False
        self.restart(f"显示缩放变了（{self.dpi} → {dpi} DPI）", show=self.window.visible)
        return True

    def tick(self) -> None:
        if self.check_dpi():
            return  # 正在退出
        self.drain_tasks()
        self.check_tethering()
        self.check_bt_audio()
        self.poll_laptop()
        self.maybe_import_report()
        if self.tray_ready:
            self.sync_tray()
        if self.show_event and ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(self.show_event), 0) == 0:
            self.window.show()  # 又运行了一次 start.bat 或 exe
        mode = self.settings["icon_mode"]
        strip_on = bool(self.settings["taskbar_strip"])
        if self.window.visible or mode == "cpu" or (strip_on and self.settings["strip_cpu"]):
            self.monitor.request_dev()
        # 电池功率遥测约 20 秒才更新一次：只有文字条时按后台间隔（10 秒）读就够，详情窗口开着才 2 秒一读
        if self.window.visible or mode == "power":
            self.monitor.request_fast()

        s = self.monitor.snapshot()
        d = self.monitor.dev_stats()
        key = (s.connected, s.percent, s.charging, s.external_power, s.fully_charged, mode,
               round(s.battery_w or 0, 1) if mode == "power" else None,
               round(s.temperature_c or 0) if mode == "temp" else None,
               (d.active, round(d.cpu_pct or 0)) if mode == "cpu" else None)
        if key != self.last_key:
            self.icon.icon = render_icon(s, mode, d)
            self.last_key = key
        if s.timestamp != self.last_ts:
            self.window.insight = self.insights.feed(s, tether=bool(self.tether_ip),
                                                     cpu_pct=d.cpu_pct if d.active else None)
            self.check_charge_alerts(self.window.insight)
            self.icon.title = tooltip(s, d, self.tether_ip)
            self.check_alerts(s)
            if self.window.visible:
                self.window.update(s)
            self.last_ts = s.timestamp
        if d is not self.last_dev:
            if self.window.visible:
                self.window.update_dev(d)
            self.last_dev = d
        try:
            self.update_strip(s, d)
        except Exception:  # noqa: BLE001 - 任务栏结构变化等问题不能拖垮整个程序
            log.exception("任务栏文字条更新失败")
        side = self.window_side()
        if side != self.last_side:
            self.last_side = side
            if self.window.visible:
                self.window._dock()  # 文字条换了一侧，详情窗口跟着换到对应的角
        # 数据最快 1 秒一更，窗口开着时 0.5 秒刷新足够；关着时 1 秒，少唤醒
        self.root.after(TICK_VISIBLE_MS if self.window.visible else TICK_IDLE_MS, self.tick)

    def run(self, show_window: bool = False) -> None:
        self.monitor.start()
        threading.Thread(target=self.watch_tethering, name="tether-watch", daemon=True).start()
        self.icon.run_detached(setup=self.tray_setup)
        self.root.after(TICK_IDLE_MS, self.tick)
        if show_window:
            self.root.after(TICK_IDLE_MS, self.window.show)
        self.root.mainloop()

    def quit(self) -> None:
        self.stop_event.set()
        self.insights.close()  # 正在充电的话把这次记录写下来
        self.bt.stop()
        self.strip.destroy()
        self.monitor.stop()
        self.icon.stop()
        self.root.destroy()


def main() -> None:
    enable_per_monitor_dpi()
    setup_logging()
    log.info("iPhone Monitor %s 启动（%s）", __version__, "exe" if FROZEN else "源码")
    restarted = RESTART_ARG in sys.argv
    if not (wait_single_instance(RESTART_WAIT_SECONDS) if restarted else single_instance()):
        log.info("已有实例在运行，通知它打开详情窗口")
        signal_running_instance()
        return
    App().run(show_window=SHOW_ARG in sys.argv)  # 换完语言直接把详情窗口打开，让人看到效果


if __name__ == "__main__":
    main()
