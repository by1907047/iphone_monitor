"""详情窗口。

布局（从上到下）：
  名称 + 连接状态
  电量大字 + 电池条（标出充电上限）+ 状态 / 预计时间 / 电源
  提示条（充电诊断、热点接管网络；正常时不显示）
  四个数字卡：整机耗电 · 外部供电 · 电池 · CPU/内存
  近 10 分钟曲线（功率 / CPU·内存 切换）
  电源 | 电池与存储 两栏明细
  占用最高的进程
  更新时间 · 电池历史 · 刷新
"""

import ctypes
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from i18n import tr
from insights import ChargeInsight
from monitor import DevStats, Monitor, Snapshot
from ui_common import (
    FONT_NUM, FONT_UI, LOW_BATTERY, Palette, charge_state, fmt, format_minutes, power_source, windows_dark_mode,
    work_area,
)

CONTENT_DIP = 440  # 内容区宽度
TOP_N = 3
HISTORY_SPAN = 600.0
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
                 on_history: Optional[Callable[[], None]] = None,
                 charge_limit: Callable[[], int] = lambda: 100,
                 dock_side: Callable[[], str] = lambda: "right") -> None:
        self.root, self.monitor, self.settings = root, monitor, settings
        self.on_history = on_history
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

        self._build_header()
        self._build_hero()
        self.banner_frame = tk.Frame(root, bg=p.bg)
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
        header = tk.Frame(self.root, bg=p.bg)
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
        hero = tk.Frame(self.root, bg=p.bg)
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
        p = self.p
        half = self.d(3)
        row = tk.Frame(self.root, bg=p.bg)
        row.pack(fill="x", padx=self.pad - half, pady=(self.d(8), 0))
        self.tiles: dict[str, tuple[tk.Label, tk.Label]] = {}
        for col, (key, title) in enumerate((("load", tr("整机耗电")), ("input", tr("外部供电")),
                                            ("battery", tr("电池")), ("stats", tr("CPU · 内存")))):
            card = self._card(row)
            card.grid(row=0, column=col, sticky="nsew", padx=half)
            row.grid_columnconfigure(col, weight=1, uniform="tiles")
            self._label(card, title, size=8, color=p.muted, anchor="w").pack(fill="x", padx=self.d(8),
                                                                             pady=(self.d(5), 0))
            value = self._label(card, "—", size=11, font=FONT_NUM, anchor="w")
            value.pack(fill="x", padx=self.d(8))
            sub = self._label(card, "", size=7, color=p.muted, anchor="w")
            sub.pack(fill="x", padx=self.d(8), pady=(0, self.d(5)))
            self.tiles[key] = (value, sub)

    def _build_chart(self) -> None:
        p = self.p
        head = tk.Frame(self.root, bg=p.bg)
        head.pack(fill="x", padx=self.pad, pady=(self.d(8), self.d(3)))
        self._label(head, tr("近 10 分钟"), size=9, bold=True).pack(side="left")
        self.mode_buttons: dict[str, tk.Label] = {}
        for mode, text in (("power", tr("功率")), ("stats", tr("CPU / 内存"))):
            btn = self._label(head, text, size=8, padx=self.d(6), pady=0, cursor="hand2")
            btn.pack(side="left", padx=(self.d(6) if mode == "power" else self.d(2), 0))
            btn.bind("<Button-1>", lambda _e, m=mode: self.set_chart_mode(m))
            self.mode_buttons[mode] = btn
        self.legend = tk.Frame(head, bg=p.bg)
        self.legend.pack(side="right")
        self.chart_w, self.chart_h = self.width, self.d(78)
        self.chart = tk.Canvas(self.root, width=self.chart_w, height=self.chart_h, bg=p.card,
                               highlightthickness=1, highlightbackground=p.line)
        self.chart.pack(padx=self.pad)
        self._style_mode_buttons()

    def _build_details(self) -> None:
        p = self.p
        card = self._card(self.root)
        card.pack(fill="x", padx=self.pad, pady=(self.d(8), 0))
        self.details: dict[str, tk.Label] = {}
        self.detail_rows: dict[str, tuple[tk.Label, tk.Label]] = {}
        # 每行左右各一项：左边电源相关，右边电池与存储
        rows = ((("source", tr("充电器")), ("health", tr("健康度"))),
                (("supply", tr("电源电压")), ("capacity", tr("容量"))),
                (("line", tr("线路电阻")), ("disk", tr("存储"))),
                (("bt", tr("声音转电脑")), ("temp", tr("温度"))))
        wrap = self.width // 2 - self.d(64)
        for r, pair in enumerate(rows):
            pady = (self.d(5) if r == 0 else self.d(1), self.d(5) if r == len(rows) - 1 else self.d(1))
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
        head = tk.Frame(self.root, bg=p.bg)
        head.pack(fill="x", padx=self.pad, pady=(self.d(8), self.d(3)))
        self._label(head, tr("占用最高的进程"), size=9, bold=True).pack(side="left")
        self.proc_note = self._label(head, "", size=8, color=p.muted)
        self.proc_note.pack(side="right")
        card = self._card(self.root)
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
        footer = tk.Frame(self.root, bg=p.bg)
        footer.pack(fill="x", padx=self.pad, pady=(self.d(8), self.pad))
        self.updated_label = self._label(footer, "", size=8, color=p.muted)
        self.updated_label.pack(side="left")
        for text, command in ((tr("刷新"), self.monitor.refresh_now),
                              (tr("电池历史"), lambda: self.on_history and self.on_history())):
            btn = tk.Label(footer, text=text, font=(FONT_UI, 8), bg=p.card, fg=p.fg, padx=self.d(8),
                           pady=self.d(2), cursor="hand2", highlightthickness=1, highlightbackground=p.line)
            btn.pack(side="right", padx=(self.d(6), 0))
            btn.bind("<Button-1>", lambda _e, c=command: c())
            btn.bind("<Enter>", lambda _e, b=btn: b.config(bg=p.line))
            btn.bind("<Leave>", lambda _e, b=btn: b.config(bg=p.card))

    # ================================================================ 显示 / 隐藏
    def show(self) -> None:
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
        legend = (((tr("整机耗电"), p.orange), (tr("电池"), p.green)) if self.chart_mode == "power"
                  else (("CPU", p.blue), (tr("内存"), p.purple)))
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

        if s.timestamp:
            self.updated_label.config(text=tr("更新于 {t}", t=time.strftime("%H:%M:%S", time.localtime(s.timestamp))))
        if self.chart_mode == "power":
            self.draw_chart()

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
        v, s = self.tiles[key]
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
        if self.chart_mode == "power":
            pts = self.monitor.history()
            self._draw_series([(t[0], t[2]) for t in pts], [(t[0], t[1]) for t in pts], (p.orange, p.green))
        else:
            pts = self.monitor.dev_history()
            self._draw_series([(t[0], t[1]) for t in pts], [(t[0], t[2]) for t in pts], (p.blue, p.purple),
                              fixed=(0.0, 100.0), digits=0, unit="%")

    def _draw_series(self, first, second, colors, fixed=None, digits=1, unit="W") -> None:
        p, c = self.p, self.chart
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
