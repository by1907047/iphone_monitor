"""界面共用：配色、字体、系统信息、格式化。"""

import ctypes
import ctypes.wintypes
import winreg
from typing import Optional

from i18n import lang, tr
from monitor import Snapshot

LOW_BATTERY = 20
FONT_UI = "Segoe UI" if lang() == "en" else "Microsoft YaHei UI"
FONT_NUM = "Segoe UI Semibold"  # 大号数字


def windows_dark_mode() -> bool:
    """应用是否用深色模式（AppsUseLightTheme）。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def work_area() -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


class Palette:
    """接近 Windows 11 设置页的配色；深浅两套。"""

    def __init__(self, dark: bool) -> None:
        self.dark = dark
        if dark:
            self.bg, self.card, self.fg, self.muted, self.line = "#202020", "#2b2b2b", "#f2f2f2", "#a0a0a0", "#3a3a3a"
            self.track = "#3d3d3d"
            self.green, self.orange, self.red, self.blue = "#6ccb5f", "#ffb95e", "#ff99a4", "#60cdff"
            self.purple = "#c3a6ff"
            self.warn_bg, self.warn_fg = "#3a2d17", "#ffb95e"
            self.danger_bg, self.danger_fg = "#3b2022", "#ff99a4"
            self.info_bg, self.info_fg = "#2c2f33", "#c8c8c8"
            self.ok_bg, self.ok_fg = "#1f3322", "#6ccb5f"
        else:
            self.bg, self.card, self.fg, self.muted, self.line = "#f3f3f3", "#ffffff", "#1b1b1b", "#616161", "#e5e5e5"
            self.track = "#e6e6e6"
            self.green, self.orange, self.red, self.blue = "#0f7b0f", "#c25e00", "#c42b1c", "#005fb8"
            self.purple = "#8764b8"
            self.warn_bg, self.warn_fg = "#fff4ce", "#8a5100"
            self.danger_bg, self.danger_fg = "#fde7e9", "#a4262c"
            self.info_bg, self.info_fg = "#eef1f5", "#4a4a4a"
            self.ok_bg, self.ok_fg = "#dff6dd", "#0f7b0f"


def fmt(value: Optional[float], unit: str, digits: int = 1, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:{'+' if signed else ''}.{digits}f} {unit}"


def charge_state(s: Snapshot) -> str:
    if not s.connected:
        return s.status
    if s.fully_charged:
        return tr("已充满")
    if s.charging:
        return tr("充电中")
    if s.external_power:
        return tr("已接电源 · 暂停充电")
    return tr("使用电池")


def format_minutes(minutes: float) -> str:
    if minutes < 1:
        return tr("不到 1 分钟")
    if minutes < 60:
        return tr("{m} 分钟", m=f"{minutes:.0f}")
    return tr("{h} 小时 {m} 分钟", h=int(minutes // 60), m=int(minutes % 60))


def adapter_label(adapter: str) -> str:
    """充电器类型，去掉「（5V/3A）」这类括号；「USB 主机」等中文名按当前语言显示。"""
    return tr(adapter.split("（")[0])


def power_source(s: Snapshot) -> str:
    if not s.external_power:
        return tr("未接电源")
    kind = tr("无线 / MagSafe") if s.wireless else tr("有线")
    parts = [kind]
    if s.adapter:
        parts.append(adapter_label(s.adapter))
    if s.adapter_watts:
        parts.append(f"{s.adapter_watts} W")
    return " · ".join(parts)
