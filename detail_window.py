"""详情窗口。

布局（从上到下）：
  名称 + 连接状态
  电量大字 + 电池条（标出充电上限）+ 状态 / 预计时间 / 电源
  提示条（充电诊断、热点接管网络；正常时不显示）
  四个数字卡：整机耗电 · 外部供电 · 电池 · CPU/内存
  近 10 分钟曲线（功率 / CPU·内存）或近 7 天每天放电量（电池损耗），三者切换
  电源 | 电池与存储 两栏明细
  占用最高的进程
  更新时间 · 电池历史 · 刷新
"""

import ctypes
import datetime
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from i18n import tr
from insights import ChargeInsight, cycle_wh, device_key, load_sessions, load_wear
from laptop_battery import LaptopBattery, LaptopStatus
import battery_report
from monitor import DevStats, Monitor, Snapshot
from ui_common import (
    FONT_NUM, FONT_UI, LOW_BATTERY, Palette, charge_state, fmt, format_minutes, power_source, windows_dark_mode,
    work_area,
)

CONTENT_DIP = 440  # 内容区宽度
TOP_N = 3
HISTORY_SPAN = 600.0
WEAR_DAYS = 7
MJ_PER_WH = 3.6e6
SLOW_REFRESH_SECONDS = 60  # 放电统计、上次充电要读文件，不用每次刷新都读
OWN_PROCESSES = ("DTServiceHub", "sysmond")  # 采样服务本身

user32 = ctypes.windll.user32
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


class DetailWindow:
    def __init__(self, root: tk.Tk, monitor: Monitor, settings: dict,
                 on_usb_only: Optional[Callable[[bool], None]] = None,
                 charge_limit: Callable[[], int] = lambda: 100,
                 dock_side: Callable[[], str] = lambda: "right",
                 laptop: Optional[LaptopBattery] = None) -> None:
        self.root, self.monitor, self.settings = root, monitor, settings
        self.laptop = laptop if laptop and laptop.present else None
        self.page_name = "phone"
        self.lp_chart_mode = "power"
        self._lp_slow_at = 0.0
        self._slow_at = 0.0
        self.charge_limit = charge_limit
        self.dock_side = dock_side  # 窗口贴哪个角：跟着任务栏文字条在左边还是右边
        self.insight = ChargeInsight()  # 由 App 每次拿到新数据后更新
        self.p = p = Palette(windows_dark_mode())
        self.scale = sc = root.winfo_fpixels("1i") / 96
        self.visible = False
        self.chart_mode = "power"
        self.last_snapshot = Snapshot()
        self.last_dev = DevStats()
        self.width = int(CONTENT_DIP * sc)
        self.pad = int(12 * sc)

        root.title(tr("iPhone 状态"))  # 版本号由 App 追加
        root.configure(bg=p.bg)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind("<Escape>", lambda _e: self.hide())
        self._docked_size = (0, 0)
        root.bind("<Configure>", self._on_configure, add="+")
        root.attributes("-topmost", bool(settings.get("topmost")))
        icon = Path(__file__).with_name("icon.ico")
        if icon.exists():
            root.iconbitmap(default=str(icon))  # default=… 让历史窗口等子窗口也用这个图标

        if self.laptop:
            self._build_switch()
        self.page = self.phone_page = tk.Frame(root, bg=p.bg)
        self.phone_page.pack(fill="x")
        self._build_header()
        self._build_hero()
        self.banner_frame = tk.Frame(self.page, bg=p.bg)
        self.banner_frame.pack(fill="x", padx=self.pad)
        # Tk 的容器在最后一个子控件被移走后不会收缩；留一个 0 高的占位，提示条消失后窗口才能缩回去
        tk.Frame(self.banner_frame, height=0, bg=p.bg).pack(fill="x")
        self.diag_banner = self._banner()
        self.tether_banner = self._banner()
        self._build_tiles()
        self._build_chart()
        self._build_details()
        self._build_processes()
        self._build_footer()
        self.laptop_page = None
        if self.laptop:
            self.page = self.laptop_page = tk.Frame(root, bg=p.bg)
            self._build_laptop_page()
            self.page = self.phone_page
        root.withdraw()

    # ================================================================ 构建
    def d(self, dip: float) -> int:
        return int(dip * self.scale)

    def _label(self, parent, text="", size=10, color=None, bold=False, font=FONT_UI, bg=None, **kw) -> tk.Label:
        return tk.Label(parent, text=text, font=(font, size, "bold") if bold else (font, size),
                        fg=color or self.p.fg, bg=bg or parent["bg"], **kw)

    def _card(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=self.p.card, highlightthickness=1, highlightbackground=self.p.line)

    def _build_header(self) -> None:
        p = self.p
        header = tk.Frame(self.page, bg=p.bg)
        header.pack(fill="x", padx=self.pad, pady=(self.pad, 0))
        left = tk.Frame(header, bg=p.bg)
        left.pack(side="left")
        self.name_label = self._label(left, size=12, bold=True, anchor="w")
        self.name_label.pack(anchor="w")
        self.sub_label = self._label(left, size=8, color=p.muted, anchor="w")
        self.sub_label.pack(anchor="w")
        self.status_pill = self._label(header, size=8, padx=self.d(8), pady=self.d(2))
        self.status_pill.pack(side="right", anchor="n", pady=(self.d(3), 0))

    def _build_hero(self) -> None:
        p = self.p
        hero = tk.Frame(self.page, bg=p.bg)
        hero.pack(fill="x", padx=self.pad, pady=(self.d(4), self.d(2)))
        self.pct_label = self._label(hero, "--", size=24, font=FONT_NUM)
        self.pct_label.pack(side="left", anchor="w")
        right = tk.Frame(hero, bg=p.bg)
        right.pack(side="left", fill="x", expand=True, padx=(self.d(12), 0), pady=(self.d(6), 0))
        self.bar_w, self.bar_h = self.width - self.d(112), self.d(7)
        self.battery_bar = tk.Canvas(right, width=self.bar_w, height=self.bar_h + self.d(6), bg=p.bg,
                                     highlightthickness=0)
        self.battery_bar.pack(anchor="w")
        self.state_label = self._label(right, size=9, anchor="w")
        self.state_label.pack(anchor="w", pady=(self.d(2), 0))
        self.detail_label = self._label(right, size=8, color=p.muted, anchor="w")
        self.detail_label.pack(anchor="w")

    def _banner(self) -> tk.Label:
        return tk.Label(self.banner_frame, font=(FONT_UI, 8), anchor="w", justify="left",
                        padx=self.d(8), pady=self.d(4), wraplength=self.width - self.d(20))

    def _show_banner(self, banner: tk.Label, text: str, kind: str) -> None:
        if not text:
            banner.pack_forget()
            return
        bg, fg = {"warn": (self.p.warn_bg, self.p.warn_fg), "danger": (self.p.danger_bg, self.p.danger_fg),
                  "info": (self.p.info_bg, self.p.info_fg)}[kind]
        icon = "ⓘ " if kind == "info" else "⚠ "
        banner.config(text=icon + text, bg=bg, fg=fg)
        banner.pack(fill="x", pady=(self.d(4), 0))

    def _build_tiles(self) -> None:
        self.tiles = self._tile_row((("load", tr("整机耗电")), ("input", tr("外部供电")),
                                     ("battery", tr("电池")), ("stats", tr("CPU · 内存"))))

    def _tile_row(self, specs) -> dict[str, tuple[tk.Label, tk.Label]]:
        p = self.p
        half = self.d(3)
        row = tk.Frame(self.page, bg=p.bg)
        row.pack(fill="x", padx=self.pad - half, pady=(self.d(8), 0))
        tiles: dict[str, tuple[tk.Label, tk.Label]] = {}
        for col, (key, title) in enumerate(specs):
            card = self._card(row)
            card.grid(row=0, column=col, sticky="nsew", padx=half)
            row.grid_columnconfigure(col, weight=1, uniform="tiles")
            self._label(card, title, size=8, color=p.muted, anchor="w", width=1).pack(fill="x", padx=self.d(8),
                                                                             pady=(self.d(5), 0))
            value = self._label(card, "—", size=11, font=FONT_NUM, anchor="w", width=1)
            value.pack(fill="x", padx=self.d(8))
            sub = self._label(card, "", size=7, color=p.muted, anchor="w", width=1)  # width=1：文字长了截断，不把整行撑宽
            sub.pack(fill="x", padx=self.d(8), pady=(0, self.d(5)))
            tiles[key] = (value, sub)
        return tiles

    def _build_chart(self) -> None:
        p = self.p
        head = tk.Frame(self.page, bg=p.bg)
        head.pack(fill="x", padx=self.pad, pady=(self.d(8), self.d(3)))
        self.chart_title = self._label(head, tr("近 10 分钟"), size=9, bold=True)
        self.chart_title.pack(side="left")
        self.mode_buttons: dict[str, tk.Label] = {}
        for mode, text in (("power", tr("功率")), ("stats", tr("CPU / 内存")), ("wear", tr("电池损耗"))):
            btn = self._label(head, text, size=8, padx=self.d(6), pady=0, cursor="hand2")
            btn.pack(side="left", padx=(self.d(6) if mode == "power" else self.d(2), 0))
            btn.bind("<Button-1>", lambda _e, m=mode: self.set_chart_mode(m))
            self.mode_buttons[mode] = btn
        self.legend = tk.Frame(head, bg=p.bg)
        self.legend.pack(side="right")
        self.chart_w, self.chart_h = self.width, self.d(78)
        self.chart = tk.Canvas(self.page, width=self.chart_w, height=self.chart_h, bg=p.card,
                               highlightthickness=1, highlightbackground=p.line)
        self.chart.pack(padx=self.pad)
        self._style_mode_buttons()

    def _build_details(self) -> None:
        p = self.p
        card = self._card(self.page)
        card.configure(pady=self.d(4))
        card.pack(fill="x", padx=self.pad, pady=(self.d(8), 0))
        self.details: dict[str, tk.Label] = {}
        self.detail_rows: dict[str, tuple[tk.Label, tk.Label]] = {}
        # 每行左右各一项：左边电源相关，右边电池与存储
        rows = ((("source", tr("充电器")), ("health", tr("健康度"))),
                (("supply", tr("电源电压")), ("capacity", tr("容量"))),
                (("line", tr("线路电阻")), ("disk", tr("存储"))),
                (("bt", tr("声音转电脑")), ("last", tr("上次充电"))),
                (("temp", tr("温度")),))  # iOS 27 不提供温度，这一行多数时候是隐藏的
        wrap = self.width // 2 - self.d(64)
        for r, pair in enumerate(rows):
            pady = self.d(1)
            for c, (key, name) in enumerate(pair):
                k = self._label(card, name, size=8, color=p.muted, anchor="w")
                v = self._label(card, "—", size=8, anchor="w", justify="left", wraplength=wrap)
                k.grid(row=r, column=c * 2, sticky="nw", padx=(self.d(8) if c == 0 else self.d(6), self.d(6)),
                       pady=pady)
                v.grid(row=r, column=c * 2 + 1, sticky="nw", padx=(0, self.d(8) if c == 1 else 0), pady=pady)
                self.details[key] = v
                self.detail_rows[key] = (k, v)
        card.grid_columnconfigure(1, weight=1, uniform="dv")
        card.grid_columnconfigure(3, weight=1, uniform="dv")

    def _build_processes(self) -> None:
        p = self.p
        head = tk.Frame(self.page, bg=p.bg)
        head.pack(fill="x", padx=self.pad, pady=(self.d(8), self.d(3)))
        self._label(head, tr("占用最高的进程"), size=9, bold=True).pack(side="left")
        self.proc_note = self._label(head, "", size=8, color=p.muted)
        self.proc_note.pack(side="right")
        card = self._card(self.page)
        card.pack(fill="x", padx=self.pad)
        self.proc_rows: list[tuple[tk.Label, tk.Canvas, tk.Label, tk.Label]] = []
        self.proc_bar_w = self.d(60)
        for i in range(TOP_N):
            pady = (self.d(4) if i == 0 else 0, self.d(4) if i == TOP_N - 1 else 0)
            name = self._label(card, "", size=8, anchor="w")
            name.grid(row=i, column=0, sticky="w", padx=(self.d(8), 0), pady=pady)
            bar = tk.Canvas(card, width=self.proc_bar_w, height=self.d(5), bg=p.card, highlightthickness=0)
            bar.grid(row=i, column=1, padx=self.d(8), pady=pady)
            cpu = self._label(card, "", size=8, anchor="e", width=6)
            cpu.grid(row=i, column=2, sticky="e", pady=pady)
            mem = self._label(card, "", size=8, color=p.muted, anchor="e", width=8)
            mem.grid(row=i, column=3, sticky="e", padx=(0, self.d(8)), pady=pady)
            self.proc_rows.append((name, bar, cpu, mem))
        card.grid_columnconfigure(0, weight=1)
        self.proc_empty = self._label(card, "", size=8, color=p.muted, anchor="w")

    def _build_footer(self) -> None:
        p = self.p
        self.footer = footer = tk.Frame(self.root, bg=p.bg)
        footer.pack(fill="x", padx=self.pad, pady=(self.d(8), self.pad))
        self.updated_label = self._label(footer, "", size=8, color=p.muted)
        self.updated_label.pack(side="left")
        for text, command in ((tr("刷新"), self.monitor.refresh_now),):
            btn = tk.Label(footer, text=text, font=(FONT_UI, 8), bg=p.card, fg=p.fg, padx=self.d(8),
                           pady=self.d(2), cursor="hand2", highlightthickness=1, highlightbackground=p.line)
            btn.pack(side="right", padx=(self.d(6), 0))
            btn.bind("<Button-1>", lambda _e, c=command: c())
            btn.bind("<Enter>", lambda _e, b=btn: b.config(bg=p.line))
            btn.bind("<Leave>", lambda _e, b=btn: b.config(bg=p.card))

    # ================================================================ 显示 / 隐藏
    def show(self) -> None:
        if self.laptop and not self.monitor.snapshot().connected:
            self.set_page("laptop")
        self.update(self.monitor.snapshot())
        self.update_dev(self.monitor.dev_stats())
        # 先透明显示（可见边框要显示后才量得到），贴好位置再显现，避免在旧位置闪一下
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.update_idletasks()
        self._dock()
        self.root.attributes("-alpha", 1.0)
        self.root.lift()
        self.root.focus_force()
        self.visible = True

    def _dock(self) -> None:
        """让窗口可见边框贴到屏幕左下角或右下角（跟着文字条那一侧）：侧边贴屏幕边缘、下边贴任务栏。

        Windows 10/11 窗口四周有一圈看不见的拖拽边框，GetWindowRect 会把它算进去；
        DWM 的「扩展边框」才是肉眼看到的边，所以按它来算偏移。
        """
        hwnd = int(self.root.wm_frame(), 16)
        outer, visible = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(outer))
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(visible), ctypes.sizeof(visible)) != 0:
            visible = outer
        left, top, right, bottom = work_area()
        if self.dock_side() == "left":
            x = outer.left + (left - visible.left)
        else:
            x = outer.left + (right - visible.right)
        y = max(top, outer.top + (bottom - visible.bottom))
        user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self._docked_size = (self.root.winfo_width(), self.root.winfo_height())

    def _on_configure(self, event: tk.Event) -> None:
        """内容变化导致窗口变高/变宽时重新贴边（Tk 改尺寸时左上角不动，下边会伸到任务栏后面）。"""
        if event.widget is not self.root or not self.visible:
            return
        if (event.width, event.height) != self._docked_size:
            self.root.after_idle(self._dock)

    def hide(self) -> None:
        self.root.withdraw()
        self.visible = False

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def set_topmost(self, on: bool) -> None:
        self.root.attributes("-topmost", on)

    def sync_usb_only(self, on: bool) -> None:
        """「仅 USB」开关已移到右键菜单，这里保留接口以兼容。"""

    def set_tether(self, ip: Optional[str]) -> None:
        text = tr("电脑正通过 USB 使用手机个人热点（{ip}），单位内网可能打不开。"
                  "关闭 iPhone「设置 → 个人热点 → 允许其他人加入」即可。", ip=ip) if ip else ""
        self._show_banner(self.tether_banner, text, "danger")

    def set_bt(self, text: str, connected: bool) -> None:
        self.details["bt"].config(text=text, fg=self.p.green if connected else self.p.fg)

    def set_chart_mode(self, mode: str) -> None:
        self.chart_mode = mode
        self.chart_title.config(text=tr("近 7 天") if mode == "wear" else tr("近 10 分钟"))
        self._style_mode_buttons()
        self.draw_chart()

    def _style_mode_buttons(self) -> None:
        p = self.p
        for mode, btn in self.mode_buttons.items():
            active = mode == self.chart_mode
            btn.config(bg=p.card if active else p.bg, fg=p.fg if active else p.muted,
                       highlightthickness=1 if active else 0, highlightbackground=p.line)
        for child in self.legend.winfo_children():
            child.destroy()
        legend = {"power": ((tr("整机耗电", "legend"), p.orange), (tr("电池"), p.green)),
                  "stats": (("CPU", p.blue), (tr("内存"), p.purple)),
                  "wear": ((tr("插电时"), p.orange), (tr("拔线时"), p.blue))}[self.chart_mode]
        for name, color in legend:
            self._label(self.legend, f"● {name}", size=8, color=color).pack(side="left", padx=(self.d(8), 0))

    # ================================================================ 数据
    def update(self, s: Snapshot) -> None:
        self.last_snapshot = s
        p, ins = self.p, self.insight
        self.name_label.config(text=s.name or "iPhone")
        self.sub_label.config(text=" · ".join(x for x in (s.model, f"iOS {s.ios}" if s.ios else "") if x) or " ")
        if s.connected:
            self.status_pill.config(text=tr("● 已连接 · {conn}", conn=s.connection), bg=p.ok_bg, fg=p.ok_fg)
        else:
            self.status_pill.config(text="● " + (s.status or tr("未连接"))[:30], bg=p.warn_bg, fg=p.warn_fg)

        # 电量 + 电池条
        if s.connected and s.percent is not None:
            color = p.green if (s.charging or s.fully_charged) else (
                p.red if s.percent <= LOW_BATTERY and not s.external_power else p.fg)
            self.pct_label.config(text=f"{s.percent}%", fg=color)
        else:
            self.pct_label.config(text="--", fg=p.muted)
        self._draw_battery_bar(s)
        state = (ins.state or charge_state(s)) if s.connected else (s.status or tr("未连接"))
        self.state_label.config(text=state)
        parts = []
        if ins.eta_minutes is not None:
            parts.append(tr("约 {time}到 {pct}%", time=format_minutes(ins.eta_minutes), pct=ins.eta_target))
        if s.connected:
            parts.append(power_source(s))
        self.detail_label.config(text=" · ".join(parts))

        # 提示条：只在有情况时出现
        kind = "warn" if ins.level == "warn" else "info"
        self._show_banner(self.diag_banner, ins.diagnosis if s.connected else "", kind)

        # 数字卡
        self._tile("load", fmt(s.system_load_w, "W"),
                   tr("手机此刻用电") if s.system_load_measured else (
                       tr("按放电估算") if s.system_load_w is not None else ""))
        if s.connected and s.external_power:
            sub = (f"{s.input_voltage_v:.2f} V × {s.input_current_a:.2f} A"
                   if s.input_voltage_v and s.input_current_a is not None else "")
            self._tile("input", fmt(s.power_in_w, "W"), sub)
        else:
            self._tile("input", "—", tr("未接电源") if s.connected else "")
        if s.battery_w is None or not s.connected:
            self._tile("battery", "—", "")
        elif abs(s.battery_w) < 0.05:
            self._tile("battery", "0.0 W", tr("不充不放"))
        elif s.battery_w > 0:
            self._tile("battery", fmt(s.battery_w, "W", signed=True), tr("充入"), p.green)
        else:
            self._tile("battery", fmt(s.battery_w, "W", signed=True), tr("放电"), p.red)

        # 明细：电源
        dt = self.details
        dt["source"].config(text=power_source(s) if s.connected else "—")
        est = ins.supply
        if est:
            low = est.source_v < 4.75
            dt["supply"].config(text=tr("约 {v} V，偏低" if low else "约 {v} V，正常", v=f"{est.source_v:.2f}"),
                                fg=p.orange if low else p.fg)
            high = est.line_ohm >= 0.2
            dt["line"].config(text=tr("约 {r} Ω，偏大" if high else "约 {r} Ω，正常", r=f"{est.line_ohm:.2f}"),
                              fg=p.orange if high else p.fg)
        elif s.connected and s.external_power and s.input_voltage_v:
            dt["supply"].config(text=tr("手机端 {v} V", v=f"{s.input_voltage_v:.2f}"), fg=p.fg)
            dt["line"].config(text=tr("收集数据中…"), fg=p.muted)
        else:
            dt["supply"].config(text="—", fg=p.fg)
            dt["line"].config(text="—", fg=p.fg)

        # 明细：电池与存储
        health = "—"
        if s.health_pct is not None:
            health = f"{s.health_pct:.1f}%" + (tr(" · 循环 {n} 次", n=s.cycle_count) if s.cycle_count else "")
        dt["health"].config(text=health)
        cap = "—"
        if s.remaining_mah and s.full_mah:
            cap = f"{s.remaining_mah} / {s.full_mah} mAh"
        dt["capacity"].config(text=cap)
        self._set_row_visible("temp", s.temperature_c is not None)  # iOS 27 不提供温度，没有就不占一行
        if s.temperature_c is not None:
            dt["temp"].config(text=fmt(s.temperature_c, "°C"))
        disk = "—"
        if s.disk_total:
            free = tr("剩 {free} / ", free=f"{s.disk_free / 1e9:.0f}") if s.disk_free is not None else ""
            disk = f"{free}{s.disk_total / 1e9:.0f} GB"
        dt["disk"].config(text=disk)

        if s.timestamp and self.page_name == "phone":
            self.updated_label.config(text=tr("更新于 {t}", t=time.strftime("%H:%M:%S", time.localtime(s.timestamp))))
        slow_due = time.time() - self._slow_at >= SLOW_REFRESH_SECONDS
        if slow_due:
            self._slow_at = time.time()
            self._update_last_charge(s)
        if self.chart_mode == "power" or (self.chart_mode == "wear" and slow_due):
            self.draw_chart()

    def _update_last_charge(self, s: Snapshot) -> None:
        sessions = load_sessions(device_key(s) if s.connected else None, 1)
        if not sessions:
            self.details["last"].config(text="—")
            return
        r = sessions[0]
        minutes = (r["end"] - r["start"]) / 60
        duration = tr("{m} 分钟", m=f"{minutes:.0f}") if minutes < 60 else tr("{h} 小时", h=f"{minutes / 60:.1f}")
        day = time.strftime("%m-%d", time.localtime(r["start"]))
        self.details["last"].config(text=f"{r['start_pct']}→{r['end_pct']}% · {duration} · {day}")

    def update_dev(self, dv: DevStats) -> None:
        self.last_dev = dv
        p = self.p
        if dv.active and dv.cpu_pct is not None:
            mem_pct = dv.mem_used * 100 / dv.mem_total if dv.mem_used and dv.mem_total else None
            value = f"{dv.cpu_pct:.0f}% · {mem_pct:.0f}%" if mem_pct is not None else f"{dv.cpu_pct:.0f}%"
            sub = (f"{dv.mem_used / 2**30:.1f} / {dv.mem_total / 2**30:.1f} GB" if mem_pct is not None else "")
            self._tile("stats", value, sub, p.red if dv.cpu_pct >= 80 else None)
        else:
            self._tile("stats", "—", dv.status[:24] if dv.status else "")

        rows = list(dv.top)[:TOP_N] if dv.active else []
        if rows:
            self.proc_empty.grid_forget()
            self.proc_note.config(text=tr("共 {n} 个进程", n=dv.process_count) if dv.process_count else "")
        else:
            self.proc_note.config(text="")
            self.proc_empty.config(text=dv.status or tr("打开后读取"))
            self.proc_empty.grid(row=0, column=0, columnspan=4, sticky="w", padx=self.d(8), pady=self.d(5))
        top_cpu = max([r[1] for r in rows] + [1.0])
        for i, (name_l, bar, cpu_l, mem_l) in enumerate(self.proc_rows):
            bar.delete("all")
            if i >= len(rows):
                for w in (name_l, cpu_l, mem_l):
                    w.config(text="")
                continue
            name, cpu, mem = rows[i]
            own = name in OWN_PROCESSES
            name_l.config(text=f"{name[:18]}" + (tr("（监测）") if own else ""), fg=p.muted if own else p.fg)
            bar.create_rectangle(0, 0, self.proc_bar_w, self.d(5), fill=p.track, width=0)
            bar.create_rectangle(0, 0, max(2, self.proc_bar_w * min(cpu, top_cpu) / top_cpu), self.d(5),
                                 fill=p.muted if own else p.blue, width=0)
            cpu_l.config(text=f"{cpu:.1f}%")
            mem_l.config(text=f"{mem / 2**20:.0f} MB" if mem else "—")
        if self.chart_mode == "stats":
            self.draw_chart()

    def _tile(self, key: str, value: str, sub: str, color: Optional[str] = None) -> None:
        self._set_tile(self.tiles, key, value, sub, color)

    def _set_tile(self, tiles: dict, key: str, value: str, sub: str, color: Optional[str] = None) -> None:
        v, s = tiles[key]
        v.config(text=value, fg=color or self.p.fg)
        s.config(text=sub or " ")

    def _set_row_visible(self, key: str, visible: bool) -> None:
        for w in self.detail_rows[key]:
            if visible:
                w.grid()
            else:
                w.grid_remove()

    def _draw_battery_bar(self, s: Snapshot) -> None:
        c, p = self.battery_bar, self.p
        c.delete("all")
        w, h, top = self.bar_w, self.bar_h, self.d(3)
        c.create_rectangle(0, top, w, top + h, fill=p.track, width=0)
        if s.connected and s.percent is not None:
            color = p.green if s.charging else (p.red if s.battery_w is not None and s.battery_w < -0.1 else p.blue)
            c.create_rectangle(0, top, w * s.percent / 100, top + h, fill=color, width=0)
        limit = self.charge_limit()
        if limit < 100:  # 充电上限刻度
            x = w * limit / 100
            c.create_line(x, 0, x, top + h + self.d(3), fill=p.fg, width=max(1, self.d(1.5)))

    # ================================================================ 曲线
    def draw_chart(self) -> None:
        p = self.p
        if self.chart_mode == "wear":
            self._draw_wear()
        elif self.chart_mode == "power":
            pts = self.monitor.history()
            self._draw_series([(t[0], t[2]) for t in pts], [(t[0], t[1]) for t in pts], (p.orange, p.green))
        else:
            pts = self.monitor.dev_history()
            self._draw_series([(t[0], t[1]) for t in pts], [(t[0], t[2]) for t in pts], (p.blue, p.purple),
                              fixed=(0.0, 100.0), digits=0, unit="%")

    def _draw_wear(self) -> None:
        """近 7 天每天的放电量，插电时 / 拔线时叠起来；单位是电池容量的百分比，方便和手机的「电池用量」对照。"""
        s = self.last_snapshot
        rows = dict(load_wear(device_key(s) if s.connected else None, WEAR_DAYS))
        design = s.design_mah or next((d["design_mah"] for d in rows.values() if d.get("design_mah")), None)
        self._draw_wear_bars(self.chart, rows, cycle_wh(design), tr("还没有数据。连着手机时会自动统计。"))

    def _draw_wear_bars(self, c: tk.Canvas, rows: dict, per_cycle: Optional[float], empty_text: str) -> None:
        p = self.p
        c.delete("all")
        w, h = self.chart_w, self.chart_h
        unit = 100 / per_cycle if per_cycle else 1.0  # Wh → 电池百分比；没有设计容量时直接显示 Wh
        today = datetime.date.today()
        dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(WEAR_DAYS - 1, -1, -1)]
        bars = [(rows[d]["plugged_mj"] / MJ_PER_WH * unit, rows[d]["unplugged_mj"] / MJ_PER_WH * unit)
                if d in rows else None for d in dates]
        small = (FONT_UI, 8)
        logged = [b for b in bars if b]
        if not any(sum(b) > 0 for b in logged):
            c.create_text(w / 2, h / 2, text=empty_text, fill=p.muted, font=small)
            return

        def fmt_v(v: float) -> str:
            if not per_cycle:
                return f"{v:.1f} Wh"
            return f"{v:.0f}%" if v >= 9.5 else f"{v:.1f}%"  # 小于 10% 带一位小数，刚开始统计时不至于全是 0%

        total = sum(sum(b) for b in logged)
        c.create_text(self.d(8), self.d(9), anchor="w", fill=p.muted, font=small,
                      text=tr("平均每天放电 {v} · 插电时占 {pct}%", v=fmt_v(total / len(logged)),
                              pct=f"{sum(b[0] for b in logged) * 100 / total:.0f}"))
        top, bottom = self.d(32), h - self.d(14)
        scale = (bottom - top) / max(max(sum(b) for b in logged), 10 if per_cycle else 1.5)
        slot = (w - self.d(16)) / WEAR_DAYS
        bar_w = slot * 0.46
        for i, (date, b) in enumerate(zip(dates, bars)):
            x = self.d(8) + slot * i + (slot - bar_w) / 2
            label = tr("今天") if i == WEAR_DAYS - 1 else date[5:]
            c.create_text(x + bar_w / 2, h - self.d(7), text=label, fill=p.muted, font=small)
            if not b:
                c.create_line(x, bottom, x + bar_w, bottom, fill=p.line)
                continue
            y_pl = bottom - b[0] * scale
            y_un = y_pl - b[1] * scale
            c.create_rectangle(x, y_pl, x + bar_w, bottom, fill=p.orange, width=0)
            c.create_rectangle(x, y_un, x + bar_w, y_pl, fill=p.blue, width=0)
            c.create_text(x + bar_w / 2, y_un - self.d(2), anchor="s", text=fmt_v(sum(b)), fill=p.fg, font=small)

    def _draw_series(self, first, second, colors, fixed=None, digits=1, unit="W", canvas=None) -> None:
        p, c = self.p, canvas or self.chart
        c.delete("all")
        w, h = self.chart_w, self.chart_h
        pad_l, pad_r, pad_t, pad_b = self.d(34), self.d(8), self.d(7), self.d(7)
        now = time.time()
        values = [v for series in (first, second) for _, v in series if v is not None]
        if not values:
            c.create_text(w / 2, h / 2, text=tr("暂无数据"), fill=p.muted, font=(FONT_UI, 9))
            return
        if fixed:
            lo, hi = fixed
            grid = (hi, (lo + hi) / 2, lo)
        else:
            lo, hi = min(0.0, min(values)), max(0.5, max(values))
            span = (hi - lo) * 1.15 or 1.0
            lo -= (hi - lo) * 0.05
            hi = lo + span
            grid = (hi, 0.0, lo) if lo < -(hi - lo) * 0.15 else (hi, 0.0)

        def xy(t, v):
            return (pad_l + (w - pad_l - pad_r) * (1 - (now - t) / HISTORY_SPAN),
                    pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo)))

        small = ("Segoe UI", 8)
        for v in grid:
            _, y = xy(now, v)
            c.create_line(pad_l, y, w - pad_r, y, fill=p.line, dash=(2, 3) if v == 0 and not fixed else ())
            c.create_text(pad_l - self.d(4), y, text=f"{v:.{digits}f}{'' if unit == 'W' else unit}", anchor="e",
                          fill=p.muted, font=small)
        width = max(1, self.d(2))
        for series, color in ((first, colors[0]), (second, colors[1])):
            coords: list[float] = []
            for t, v in series:
                if v is None or now - t > HISTORY_SPAN:
                    if len(coords) >= 4:
                        c.create_line(*coords, fill=color, width=width, smooth=True)
                    coords = []
                    continue
                coords.extend(xy(t, v))
            if len(coords) >= 4:
                c.create_line(*coords, fill=color, width=width, smooth=True)

    # ================================================================ 本机电池
    def _build_switch(self) -> None:
        """窗口顶部的「iPhone / 本机电池」切换。"""
        p = self.p
        bar = tk.Frame(self.root, bg=p.bg)
        bar.pack(fill="x", padx=self.pad, pady=(self.d(8), 0))
        self.page_buttons: dict[str, tk.Label] = {}
        for name, text in (("phone", "iPhone"), ("laptop", tr("本机电池"))):
            btn = self._label(bar, text, size=9, padx=self.d(10), pady=self.d(1), cursor="hand2")
            btn.pack(side="left", padx=(0, self.d(4)))
            btn.bind("<Button-1>", lambda _e, n=name: self.set_page(n))
            self.page_buttons[name] = btn
        self._style_page_buttons()

    def _style_page_buttons(self) -> None:
        p = self.p
        for name, btn in getattr(self, "page_buttons", {}).items():
            active = name == self.page_name
            btn.config(bg=p.card if active else p.bg, fg=p.fg if active else p.muted,
                       highlightthickness=1 if active else 0, highlightbackground=p.line)

    def set_page(self, name: str) -> None:
        if name == self.page_name or not self.laptop_page:
            return
        old, new = (self.phone_page, self.laptop_page) if name == "laptop" else (self.laptop_page, self.phone_page)
        old.pack_forget()
        new.pack(fill="x", before=self.footer)
        self.page_name = name
        self._style_page_buttons()
        if name == "laptop":
            self.update_laptop(self.laptop.read())
        else:
            self.update(self.monitor.snapshot())

    def _build_laptop_page(self) -> None:
        p, lp = self.p, {}
        self.lp = lp
        header = tk.Frame(self.page, bg=p.bg)
        header.pack(fill="x", padx=self.pad, pady=(self.pad, 0))
        left = tk.Frame(header, bg=p.bg)
        left.pack(side="left")
        self._label(left, tr("本机电池"), size=12, bold=True, anchor="w").pack(anchor="w")
        lp["sub"] = self._label(left, " ", size=8, color=p.muted, anchor="w")
        lp["sub"].pack(anchor="w")
        lp["pill"] = self._label(header, size=8, padx=self.d(8), pady=self.d(2))
        lp["pill"].pack(side="right", anchor="n", pady=(self.d(3), 0))

        hero = tk.Frame(self.page, bg=p.bg)
        hero.pack(fill="x", padx=self.pad, pady=(self.d(4), self.d(2)))
        lp["pct"] = self._label(hero, "--", size=24, font=FONT_NUM)
        lp["pct"].pack(side="left", anchor="w")
        right = tk.Frame(hero, bg=p.bg)
        right.pack(side="left", fill="x", expand=True, padx=(self.d(12), 0), pady=(self.d(6), 0))
        lp["bar"] = tk.Canvas(right, width=self.bar_w, height=self.bar_h + self.d(6), bg=p.bg, highlightthickness=0)
        lp["bar"].pack(anchor="w")
        lp["state"] = self._label(right, size=9, anchor="w")
        lp["state"].pack(anchor="w", pady=(self.d(2), 0))
        lp["detail"] = self._label(right, size=8, color=p.muted, anchor="w")
        lp["detail"].pack(anchor="w")

        self.lp_tiles = self._tile_row((("system", tr("整机耗电", "laptop")), ("rate", tr("电池功率")),
                                        ("health", tr("健康度")), ("cycles", tr("循环次数"))))

        head = tk.Frame(self.page, bg=p.bg)
        head.pack(fill="x", padx=self.pad, pady=(self.d(8), self.d(3)))
        lp["chart_title"] = self._label(head, tr("近 10 分钟"), size=9, bold=True)
        lp["chart_title"].pack(side="left")
        self.lp_mode_buttons: dict[str, tk.Label] = {}
        for mode, text in (("power", tr("功率")), ("wear", tr("电池损耗")), ("long", tr("长期"))):
            btn = self._label(head, text, size=8, padx=self.d(6), pady=0, cursor="hand2")
            btn.pack(side="left", padx=(self.d(6) if mode == "power" else self.d(2), 0))
            btn.bind("<Button-1>", lambda _e, m=mode: self.set_laptop_chart_mode(m))
            self.lp_mode_buttons[mode] = btn
        lp["legend"] = tk.Frame(head, bg=p.bg)
        lp["legend"].pack(side="right")
        lp["chart"] = tk.Canvas(self.page, width=self.chart_w, height=self.chart_h, bg=p.card,
                                highlightthickness=1, highlightbackground=p.line)
        lp["chart"].pack(padx=self.pad)
        self._style_laptop_mode_buttons()

        card = self._card(self.page)
        card.configure(pady=self.d(4))
        card.pack(fill="x", padx=self.pad, pady=(self.d(8), 0))
        self.lp_details: dict[str, tk.Label] = {}
        rows = ((("full", tr("满电容量")), ("design", tr("设计容量"))),
                (("cap", tr("当前容量")), ("source", tr("电源"))),
                (("voltage", tr("电压")), ("package", tr("处理器功耗"))))
        for r, pair in enumerate(rows):
            for c, (key, name) in enumerate(pair):
                self._label(card, name, size=8, color=p.muted, anchor="w").grid(
                    row=r, column=c * 2, sticky="nw", padx=(self.d(8) if c == 0 else self.d(6), self.d(6)), pady=self.d(1))
                v = self._label(card, "—", size=8, anchor="w")
                v.grid(row=r, column=c * 2 + 1, sticky="nw", padx=(0, self.d(8) if c == 1 else 0), pady=self.d(1))
                self.lp_details[key] = v
        card.grid_columnconfigure(1, weight=1, uniform="lv")
        card.grid_columnconfigure(3, weight=1, uniform="lv")

    def set_laptop_chart_mode(self, mode: str) -> None:
        self.lp_chart_mode = mode
        self.lp["chart_title"].config(text={"wear": tr("近 7 天"), "long": tr("每月")}.get(mode, tr("近 10 分钟")))
        self._style_laptop_mode_buttons()
        self._draw_laptop_chart(self.laptop.status)

    def _style_laptop_mode_buttons(self) -> None:
        p = self.p
        for mode, btn in self.lp_mode_buttons.items():
            active = mode == self.lp_chart_mode
            btn.config(bg=p.card if active else p.bg, fg=p.fg if active else p.muted,
                       highlightthickness=1 if active else 0, highlightbackground=p.line)
        legend = self.lp["legend"]
        for child in legend.winfo_children():
            child.destroy()
        items = {"power": ((tr("整机耗电", "legend"), p.orange), (tr("电池"), p.green)),
                 "wear": ((tr("插电时"), p.orange), (tr("拔线时"), p.blue)),
                 "long": ((tr("健康度"), p.blue), (tr("用电池（次循环）"), p.orange))}[self.lp_chart_mode]
        for name, color in items:
            self._label(legend, f"● {name}", size=8, color=color).pack(side="left", padx=(self.d(8), 0))

    def _draw_laptop_chart(self, s: LaptopStatus) -> None:
        if self.lp_chart_mode == "power":
            hist = list(self.laptop.history)
            self._draw_series([(t, sw) for t, _r, sw in hist], [(t, r) for t, r, _sw in hist],
                              (self.p.orange, self.p.green), canvas=self.lp["chart"])
            return
        if self.lp_chart_mode == "long":
            self._draw_long_term(s)
            return
        rows = dict(load_wear(s.key, WEAR_DAYS)) if s.present else {}
        design = s.design_mwh or next((d.get("design_mwh") for d in rows.values() if d.get("design_mwh")), None)
        self._draw_wear_bars(self.lp["chart"], rows, design / 1000 if design else None,
                             tr("还没有数据。程序运行时会自动统计。"))

    def _draw_long_term(self, s: LaptopStatus) -> None:
        """每月：健康度折线 + 当月用电池放掉的电（折合循环次数）柱子。数据来自 Windows 电池报告。"""
        p, c = self.p, self.lp["chart"]
        c.delete("all")
        w, h = self.chart_w, self.chart_h
        small = (FONT_UI, 8)
        months = [m for m in battery_report.monthly(s.key) if m.get("health")]
        if len(months) < 2:
            c.create_text(w / 2, h / 2, text=tr("还没有长期记录。程序每天会从 Windows 电池报告导入一次。"),
                          fill=p.muted, font=small)
            return
        first, last = months[0], months[-1]
        c.create_text(self.d(8), self.d(9), anchor="w", fill=p.muted, font=small,
                      text=tr("健康度 {h0}% → {h1}% · 循环 {c0} → {c1} 次（{since} 起）",
                              h0=f"{first['health']:.1f}", h1=f"{last['health']:.1f}", c0=first["cycles"],
                              c1=last["cycles"], since=first["month"]))
        top, bottom, left, right = self.d(24), h - self.d(14), self.d(8), w - self.d(8)
        # 连续的月份轴：Windows 没记录的月份留空，折线在那里断开，不把前后两段连成一条
        by_month = {m["month"]: m for m in months}
        y, mo = map(int, first["month"].split("-"))
        axis = []
        while f"{y:04d}-{mo:02d}" <= last["month"]:
            axis.append(f"{y:04d}-{mo:02d}")
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        slot = (right - left) / len(axis)
        hi_used = max([m["battery_wh"] / m["design_wh"] for m in months] + [1.0])
        healths = [m["health"] for m in months]
        lo_h, hi_h = min(healths) - 1, max(healths) + 1
        segment: list[float] = []
        for i, month in enumerate(axis):
            x = left + slot * i
            m = by_month.get(month)
            if i in (0, len(axis) - 1) or month.endswith("-01"):
                c.create_text(x + slot / 2, h - self.d(7), text=month[2:].replace("-", "."), fill=p.muted, font=small)
            if m is None:
                if len(segment) >= 4:
                    c.create_line(*segment, fill=p.blue, width=max(1, self.d(2)))
                segment = []
                continue
            bar_h = (bottom - top) * m["battery_wh"] / m["design_wh"] / hi_used
            c.create_rectangle(x + slot * 0.2, bottom - bar_h, x + slot * 0.8, bottom, fill=p.orange, width=0)
            segment.extend((x + slot / 2, bottom - (bottom - top) * (m["health"] - lo_h) / (hi_h - lo_h)))
        if len(segment) >= 4:
            c.create_line(*segment, fill=p.blue, width=max(1, self.d(2)))

    def update_laptop(self, s: LaptopStatus) -> None:
        if not self.laptop_page or self.page_name != "laptop" or not s.present:
            return
        p, lp = self.p, self.lp
        lp["sub"].config(text=" · ".join(x for x in (
            f"{s.maker} {s.name}".strip(),
            tr("设计 {wh} Wh", wh=f"{s.design_mwh / 1000:.1f}") if s.design_mwh else "") if x) or " ")
        draining = s.rate_w is not None and s.rate_w < -0.3
        if s.online:
            lp["pill"].config(text=tr("● 已接电源"), bg=p.ok_bg, fg=p.ok_fg)
        else:
            lp["pill"].config(text=tr("● 使用电池"), bg=p.info_bg, fg=p.info_fg)
        low = s.percent is not None and s.percent <= LOW_BATTERY and not s.online
        lp["pct"].config(text=f"{s.percent:.0f}%" if s.percent is not None else "--",
                         fg=p.green if s.charging else (p.red if low else p.fg))
        c = lp["bar"]
        c.delete("all")
        top = self.d(3)
        c.create_rectangle(0, top, self.bar_w, top + self.bar_h, fill=p.track, width=0)
        if s.percent is not None:
            color = p.green if s.charging else (p.red if draining and s.online else p.blue)
            c.create_rectangle(0, top, self.bar_w * s.percent / 100, top + self.bar_h, fill=color, width=0)
        if s.charging:
            state = tr("充电中")
        elif s.online and draining:
            state = tr("插着电源仍在耗电池")
        elif s.online:
            state = tr("已接电源 · 未充电")
        else:
            state = tr("使用电池")
        lp["state"].config(text=state, fg=p.orange if s.online and draining else p.fg)
        minutes = self.laptop.minutes_left(s)
        detail = ""
        if minutes is not None and minutes < 24 * 60:
            detail = (tr("约 {time}充满", time=format_minutes(minutes)) if s.rate_w > 0
                      else tr("剩余约 {time}", time=format_minutes(minutes)))
        lp["detail"].config(text=detail or " ")

        tiles = self.lp_tiles
        if s.system_w is not None and s.system_measured:
            self._set_tile(tiles, "system", fmt(s.system_w, "W"), tr("按电池放电实测"))
        elif s.system_w is not None:
            self._set_tile(tiles, "system", "≈ " + fmt(s.system_w, "W"),
                           tr("估算：处理器 {w} W + 其他", w=f"{s.package_w:.1f}"))
        elif s.package_w is not None:
            self._set_tile(tiles, "system", fmt(s.package_w, "W"), tr("仅处理器；用电池时可学会估整机"))
        else:
            self._set_tile(tiles, "system", "—", "")
        if s.rate_w is None:
            self._set_tile(tiles, "rate", "—", "")
        elif abs(s.rate_w) < 0.05:
            self._set_tile(tiles, "rate", "0.0 W", tr("不充不放"))
        elif s.rate_w > 0:
            self._set_tile(tiles, "rate", fmt(s.rate_w, "W", signed=True), tr("充入"), p.green)
        else:
            self._set_tile(tiles, "rate", fmt(s.rate_w, "W", signed=True), tr("放电"), p.red)
        health = s.health_pct
        self._set_tile(tiles, "health", f"{health:.1f}%" if health else "—",
                       tr("{full} / {design} Wh", full=f"{s.full_mwh / 1000:.1f}",
                          design=f"{s.design_mwh / 1000:.1f}") if health else "")
        self._set_tile(tiles, "cycles", str(s.cycles) if s.cycles else "—", "")

        dt = self.lp_details
        dt["full"].config(text=f"{s.full_mwh / 1000:.1f} Wh" if s.full_mwh else "—")
        dt["design"].config(text=f"{s.design_mwh / 1000:.1f} Wh" if s.design_mwh else "—")
        dt["cap"].config(text=f"{s.capacity_mwh / 1000:.1f} Wh" if s.capacity_mwh is not None else "—")
        dt["source"].config(text=tr("已接电源") if s.online else tr("电池"))
        dt["voltage"].config(text=fmt(s.voltage_v, "V", digits=2))
        dt["package"].config(text=fmt(s.package_w, "W") if s.package_w is not None else "—")
        self.updated_label.config(text=tr("更新于 {t}", t=time.strftime("%H:%M:%S", time.localtime(s.timestamp))))
        slow_due = time.time() - self._lp_slow_at >= SLOW_REFRESH_SECONDS
        if self.lp_chart_mode == "power" or slow_due:
            if slow_due:
                self._lp_slow_at = time.time()
            self._draw_laptop_chart(s)
