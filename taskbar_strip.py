"""任务栏文字条：把一个小窗口挂进 Windows 任务栏（托盘区左侧），显示手机图标、电量、功率和 CPU。

Windows 11 的任务栏内容是合成层画的，普通子窗口会被盖住，只有「分层子窗口」（WS_EX_LAYERED）
能显示在上面。这里用逐像素透明（UpdateLayeredWindow）：内容用 PIL 画好后整张贴上去，
背景保留 1/255 的不透明度——肉眼看不出来，但整块区域都能点（完全透明的像素点击会穿透）。
"""

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
import time
import tkinter as tk
import winreg
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.SetParent.restype = wintypes.HWND
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

GWL_STYLE = -16
WS_CHILD, WS_POPUP = 0x40000000, 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00080000, 0x00000080, 0x08000000
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
WM_MOUSEMOVE, WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE = 0x0200, 0x0202, 0x0205, 0x02A3
TME_LEAVE = 0x00000002
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x2, 0x0, 0x1
IDC_HAND = 32649

WIDTH_DIP = 128
COMPACT_WIDTH_DIP = 70  # 窄版：只显示电量 + 功率
APPS_POLL_SECONDS = 3  # 多久查一次任务栏应用图标排到了哪里
GAP_DIP = 6
GLYPH_PHONE, GLYPH_BOLT = "\uE8EA", "\uE945"
PHONE_W_DIP, PHONE_H_DIP = 12, 21  # 自绘手机外框尺寸
FONTS = Path(r"C:\Windows\Fonts")
CLASS_NAME = "iPhoneMonitorTaskbarStrip"

# 文字颜色跟任务栏主题走（任务栏跟随「Windows 模式」，不是应用模式）
THEMES = {
    "light": dict(fg=(27, 27, 27), muted=(92, 92, 92), good=(15, 123, 15), bad=(196, 43, 28), warn=(157, 93, 0),
                  hover=(0, 0, 0, 14)),
    "dark": dict(fg=(255, 255, 255), muted=(197, 197, 197), good=(108, 203, 95), bad=(255, 153, 164),
                 warn=(252, 225, 0), hover=(255, 255, 255, 20)),
}


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
                                       wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]


def taskbar_theme() -> str:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return "light" if winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] else "dark"
    except OSError:
        return "dark"


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _font(names: tuple[str, ...], px: int) -> ImageFont.FreeTypeFont:
    """按顺序找第一个可用字体；"文件名#序号" 表示 .ttc 里的第几个字体。"""
    for name in names:
        file, _, index = name.partition("#")
        try:
            return ImageFont.truetype(str(FONTS / file), px, index=int(index or 0))
        except OSError:
            continue
    return ImageFont.load_default()


class AppsEdgeWatcher(threading.Thread):
    """后台每隔几秒用 UI 自动化读一次任务栏应用图标的实际右边缘（屏幕像素）。

    Windows 11 的任务栏图标是 XAML 画的，旧的窗口结构（ReBarWindow32）位置不变，读不到图标排到了哪里。
    查一次约 50 ms，放在后台线程里不卡界面；读不到时 right 为 None，退回旧办法。
    """

    def __init__(self) -> None:
        super().__init__(name="taskbar-apps-edge", daemon=True)
        self.right: Optional[int] = None  # 应用图标最右边缘
        self.center_left: Optional[int] = None  # 中间那组（开始/搜索/任务视图/应用图标）最左边缘
        self.widgets_right: Optional[int] = None  # 左侧天气小组件右边缘；关掉小组件时为 None
        # 第一次读完（或确定读不了）后置位；在此之前文字条先不显示，免得启动时压在天气小组件上
        self.ready = threading.Event()

    def run(self) -> None:
        try:
            import sys

            # comtypes 第一次导入时会按 sys.coinit_flags 初始化「当前线程」的 COM；UI 自动化客户端推荐多线程模式
            if "comtypes" not in sys.modules:
                sys.coinit_flags = 0  # COINIT_MULTITHREADED
            import comtypes
            import comtypes.client

            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass  # 本线程已经初始化过（导入 comtypes 时），沿用即可
            comtypes.client.GetModule("UIAutomationCore.dll")  # 打包时已预先生成
            from comtypes.gen import UIAutomationClient as UIA

            uia = comtypes.CoCreateInstance(UIA.CUIAutomation._reg_clsid_, interface=UIA.IUIAutomation,
                                            clsctx=comtypes.CLSCTX_INPROC_SERVER)
            cond = uia.CreatePropertyCondition(UIA.UIA_ClassNamePropertyId, "Taskbar.TaskListButtonAutomationPeer")
            for aid in ("StartButton", "SearchButton", "TaskViewButton", "WidgetsButton"):
                cond = uia.CreateOrCondition(cond, uia.CreatePropertyCondition(UIA.UIA_AutomationIdPropertyId, aid))
        except Exception:  # noqa: BLE001
            log.warning("UI 自动化不可用，只能按旧方法判断任务栏空间", exc_info=True)
            self.ready.set()
            return
        while True:
            try:
                taskbar = user32.FindWindowW("Shell_TrayWnd", None)
                if taskbar:
                    found = uia.ElementFromHandle(taskbar).FindAll(UIA.TreeScope_Descendants, cond)
                    apps, center, widgets = [], [], None
                    for i in range(found.Length):
                        e = found.GetElement(i)
                        r = e.CurrentBoundingRectangle
                        if r.right <= r.left:
                            continue
                        aid = e.CurrentAutomationId
                        if aid == "WidgetsButton":
                            widgets = r.right
                            continue
                        center.append(r.left)
                        if aid not in ("StartButton", "SearchButton", "TaskViewButton"):
                            apps.append(r.right)
                    self.right = max(apps) if apps else None
                    self.center_left = min(center) if center else None
                    self.widgets_right = widgets
            except Exception as exc:  # noqa: BLE001 - 资源管理器重启等情况，下一轮再试
                log.debug("读取任务栏图标位置失败: %r", exc)
                self.right = self.center_left = self.widgets_right = None
            self.ready.set()
            time.sleep(APPS_POLL_SECONDS)


class TaskbarStrip:
    _class_registered = False
    _wndproc_ref: Optional[WNDPROC] = None
    _instances: dict[int, "TaskbarStrip"] = {}

    def __init__(
        self,
        root: tk.Tk,
        on_left_click: Callable[[], None],
        on_right_click: Callable[[int, int], None],
    ) -> None:
        self.root = root
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.dpi = root.winfo_fpixels("1i")
        self.scale = self.dpi / 96
        self.hwnd = 0
        self.taskbar = 0
        self.theme = ""
        self.size = (0, 0)
        self.placement: tuple = ()
        self.content: tuple = ()
        self.hover = False
        self.tracking = False
        self.fits = True  # 放不下（会压到应用图标）时为 False，界面据此改用托盘图标
        self.position = "right"  # 优先放的一侧：right=托盘旁，left=小组件旁；由界面按设置改
        self.side = "right"  # 实际放在哪一侧
        self.apps_watcher = AppsEdgeWatcher()
        self.apps_watcher.start()
        pt = lambda points: max(1, round(points * self.dpi / 72))  # noqa: E731
        self.f_icon = _font(("SegoeIcons.ttf", "segmdl2.ttf"), pt(13))
        self.f_bolt = _font(("SegoeIcons.ttf", "segmdl2.ttf"), pt(8))
        self._bolt_bbox: Optional[tuple[int, int, int, int]] = None
        self.f_pct = _font(("seguisb.ttf", "segoeuib.ttf", "segoeui.ttf"), pt(10))
        self.f_power = _font(("segoeui.ttf",), pt(8.5))
        self.f_line2 = _font(("msyh.ttc#1", "msyh.ttc", "segoeui.ttf"), pt(8))

    # ---- 生命周期 ----
    def ensure(self) -> None:
        """每次 tick 调用：任务栏重建（资源管理器重启）、主题变化、托盘变宽时自动修正。"""
        taskbar = user32.FindWindowW("Shell_TrayWnd", None)
        if not taskbar:
            self.destroy()
            return
        theme = taskbar_theme()
        if not (self.hwnd and user32.IsWindow(self.hwnd)) or taskbar != self.taskbar:
            self.destroy()
            self._create(taskbar)
        if theme != self.theme:
            self.theme = theme
            self.content = ()  # 换主题后重画
        self._place()

    def destroy(self) -> None:
        if self.hwnd:
            TaskbarStrip._instances.pop(self.hwnd, None)
            if user32.IsWindow(self.hwnd):
                user32.DestroyWindow(self.hwnd)
        self.hwnd = self.taskbar = 0
        self.placement = self.content = ()

    def _create(self, taskbar: int) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        if not TaskbarStrip._class_registered:
            TaskbarStrip._wndproc_ref = WNDPROC(TaskbarStrip._wndproc)  # 必须一直持有，否则回调被回收
            wc = WNDCLASSW(lpfnWndProc=TaskbarStrip._wndproc_ref, hInstance=hinst,
                           hCursor=user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND)), lpszClassName=CLASS_NAME)
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError()
            TaskbarStrip._class_registered = True
        hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, CLASS_NAME, "iPhone 状态条",
                                      WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError()
        user32.SetParent(hwnd, taskbar)
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, (style | WS_CHILD) & ~WS_POPUP)
        TaskbarStrip._instances[hwnd] = self
        self.hwnd, self.taskbar = hwnd, taskbar
        self.size = (0, 0)
        log.info("任务栏文字条已挂载")

    def _place(self) -> None:
        """按设置放在托盘旁（右）或小组件旁（左）。

        先试选定的一侧，放不下试另一侧；每侧先试完整版、再试窄版；都放不下就隐藏（由界面改用托盘图标）。
        """
        tray = user32.FindWindowExW(self.taskbar, None, "TrayNotifyWnd", None)
        apps = user32.FindWindowExW(self.taskbar, None, "ReBarWindow32", None)
        if not tray or not self.hwnd or not self.apps_watcher.ready.is_set():
            return  # 还没读到任务栏图标位置，先不显示
        tb = _rect(self.taskbar)
        tray_left = _rect(tray)[0]
        w = self.apps_watcher
        # Windows 11 上 ReBarWindow32 的位置是固定的，不反映实际图标；以 UI 自动化读到的真实边缘为准
        apps_right = max(_rect(apps)[2] if apps else tb[0], w.right or 0)
        center_left = w.center_left if w.center_left is not None else (_rect(apps)[0] if apps else tb[2])
        left_start = w.widgets_right if w.widgets_right is not None else tb[0]
        height, gap = tb[3] - tb[1], int(GAP_DIP * self.scale)

        def slot(side: str, width: int) -> Optional[int]:
            if side == "right":
                x = tray_left - width - gap
                return x if x >= apps_right + gap else None
            x = left_start + gap
            return x if x + width + gap <= center_left else None

        sides = (self.position, "left" if self.position == "right" else "right")
        choice = None
        for side in sides:
            for width in (int(WIDTH_DIP * self.scale), int(COMPACT_WIDTH_DIP * self.scale)):
                x = slot(side, width)
                if x is not None:
                    choice = (side, width, x)
                    break
            if choice:
                break
        fits = choice is not None
        placement = (choice, tb, fits)
        if placement == self.placement:
            return
        self.placement = placement
        self.fits = fits
        if not fits:
            user32.ShowWindow(self.hwnd, 0)
            log.info("任务栏两侧都放不下文字条（应用图标 %s–%s），暂时隐藏", center_left, apps_right)
            return
        self.side, width, x_screen = choice
        if (width, height) != self.size:
            self.size = (width, height)
            self.content = ()
        # 子窗口坐标相对任务栏左上角
        user32.SetWindowPos(self.hwnd, None, x_screen - tb[0], 0, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    # ---- 鼠标 ----
    @staticmethod
    def _wndproc(hwnd, msg, wparam, lparam):
        self = TaskbarStrip._instances.get(hwnd)
        if self is not None:
            try:
                if msg == WM_LBUTTONUP:
                    self._post(self.on_left_click)
                    return 0
                if msg == WM_RBUTTONUP:
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    self._post(lambda: self.on_right_click(pt.x, pt.y))
                    return 0
                if msg == WM_MOUSEMOVE and not self.tracking:
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self.tracking = True
                    self._set_hover(True)
                elif msg == WM_MOUSELEAVE:
                    self.tracking = False
                    self._set_hover(False)
            except Exception:  # noqa: BLE001 - 窗口过程里不能抛异常
                log.exception("任务栏文字条消息处理出错")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _post(self, fn: Callable[[], None]) -> None:
        """把回调交给 Tk 主线程执行。

        窗口过程是在 Tk 主循环分发消息时被调用的，此时 _tkinter 持有 Tcl 锁，
        在这里直接调 root.after 会死锁；从另一个线程调用则走 tkinter 的跨线程排队。
        """
        threading.Thread(target=lambda: self.root.after(0, fn), name="strip-click", daemon=True).start()

    def _set_hover(self, on: bool) -> None:
        if self.hover != on:
            self.hover = on
            if self.content:
                self._render(self.content)

    # ---- 绘制 ----
    def update(
        self,
        connected: bool,
        percent: Optional[int],
        charging: bool,
        external_power: bool,
        discharging: bool,
        low: bool,
        power_w: Optional[float],
        cpu_pct: Optional[float],
        mem_pct: Optional[float],
        show_stats: bool,
        warning: str = "",
        stats_note: str = "",
    ) -> None:
        if not self.hwnd or self.size == (0, 0):
            return
        content = (connected, percent, charging, external_power, discharging, low,
                   None if power_w is None else round(power_w, 1),
                   None if cpu_pct is None else round(cpu_pct),
                   None if mem_pct is None else round(mem_pct),
                   show_stats, warning, stats_note, self.theme, self.size)
        if content != self.content:
            self._render(content)  # 没变化就不重画

    def _render(self, content: tuple) -> None:
        self.content = content
        self._blit(self.compose(content, self.hover))

    def compose(self, content: tuple, hover: bool = False) -> Image.Image:
        """按内容画出整张图（RGBA）。布局：
        [手机图标(内含闪电)]  80%  0.9 W
                              11% · 内存 58%
        """
        (connected, percent, charging, external_power, discharging, low,
         power_w, cpu_pct, mem_pct, show_stats, warning, stats_note, theme, size) = content
        colors = THEMES[theme or "dark"]
        width, height = size
        s = self.scale
        # 背景 alpha=1：看不见，但整块都能接收鼠标
        img = Image.new("RGBA", size, (0, 0, 0, 1))
        draw = ImageDraw.Draw(img)
        if hover:
            draw.rounded_rectangle((int(2 * s), int(6 * s), width - int(2 * s), height - int(6 * s)),
                                   radius=int(4 * s), fill=colors["hover"])
        mid = height / 2
        x_text = int(30 * s)

        if not connected or percent is None:
            icon_color, bolt = colors["muted"], None
            pct_text, pct_color, power_text = "--", colors["muted"], ""
            line2, line2_color = "未连接", colors["muted"]
        else:
            # 电池在放电（插着线也可能放电）时图标整体变红；否则充电绿色闪电、插电暂停充电灰色闪电
            if discharging:
                icon_color = colors["bad"]
                bolt = colors["bad"] if external_power else None
            else:
                icon_color = colors["fg"]
                bolt = (colors["good"] if charging else colors["muted"]) if external_power else None
            pct_text = f"{percent}%"
            pct_color = colors["good"] if charging else (colors["bad"] if low else colors["fg"])
            power_text = f"{power_w:.1f} W" if power_w is not None else ""
            if warning:
                line2, line2_color = warning, colors["warn"]
            elif show_stats and stats_note and cpu_pct is None:
                line2, line2_color = stats_note, colors["muted"]  # 比如开发者模式没开
            elif show_stats:
                cpu = f"{cpu_pct:.0f}%" if cpu_pct is not None else "…"
                mem = f"{mem_pct:.0f}%" if mem_pct is not None else "…"
                line2, line2_color = f"{cpu} · 内存 {mem}", colors["muted"]
            else:
                line2, line2_color = "", colors["muted"]
            if width < round(WIDTH_DIP * s) - 2:
                # 窄版（任务栏图标多、空间紧时）：第一行电量，第二行功率；CPU/内存和长提示放不下
                line2 = power_text or ("⚠" if warning else "")
                line2_color = colors["bad"] if discharging and power_text else (
                    colors["warn"] if warning and not power_text else colors["muted"])
                power_text = ""

        # 手机图标：自己画圆角矩形外框（系统字形底部有条 Home 横线，不要）
        w_icon, h_icon = round(PHONE_W_DIP * s), round(PHONE_H_DIP * s)
        l, t = round(8 * s), round(mid - h_icon / 2)
        r, b = l + w_icon, t + h_icon
        stroke = max(1, round(1.4 * s))
        draw.rounded_rectangle((l, t, r, b), radius=round(w_icon * 0.26), outline=icon_color, width=stroke)
        if bolt:
            # 按实际画出来的笔画居中（字体度量会带上留白，按它居中看起来偏上）
            bx0, by0, bx1, by1 = self._bolt_ink_bbox()
            cx, cy = (l + r) / 2, (t + b) / 2
            draw.text((round(cx - (bx0 + bx1) / 2), round(cy - (by0 + by1) / 2)), GLYPH_BOLT, font=self.f_bolt,
                      fill=bolt, anchor="lt")

        # 第一行：电量 + 功率；没有第二行时整行垂直居中
        y1 = mid - int(8 * s) if line2 else mid
        draw.text((x_text, y1), pct_text, font=self.f_pct, fill=pct_color, anchor="lm")
        if power_text:
            right = draw.textbbox((x_text, y1), pct_text, font=self.f_pct, anchor="lm")[2]
            draw.text((right + int(6 * s), y1 + int(1 * s)), power_text, font=self.f_power,
                      fill=colors["bad"] if discharging else colors["muted"], anchor="lm")
        if line2:
            draw.text((x_text, mid + int(9 * s)), line2, font=self.f_line2, fill=line2_color, anchor="lm")
        return img

    def _bolt_ink_bbox(self) -> tuple[int, int, int, int]:
        if self._bolt_bbox is None:
            size = self.f_bolt.size * 2
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).text((0, 0), GLYPH_BOLT, font=self.f_bolt, fill=255, anchor="lt")
            # 只算明显的笔画像素，抗锯齿的淡边会把包围盒撑偏
            self._bolt_bbox = mask.point(lambda v: 255 if v > 128 else 0).getbbox() or (0, 0, 0, 0)
        return self._bolt_bbox

    def _blit(self, img: Image.Image) -> None:
        """把 RGBA 图像按预乘 alpha 贴到分层窗口上。"""
        width, height = img.size
        r, g, b, a = img.split()
        r, g, b = (ImageChops.multiply(ch, a) for ch in (r, g, b))  # 预乘 alpha
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # DIB 是 BGRA
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(mem_dc, bitmap)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(self.hwnd, screen_dc, None, ctypes.byref(SIZE(width, height)), mem_dc,
                                       ctypes.byref(POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem_dc, old)
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
