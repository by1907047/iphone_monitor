"""电池历史窗口：每日健康度趋势 + 最近的充电记录。"""

import time
import tkinter as tk
from typing import Optional

from i18n import tr
from insights import ChargeInsight, device_key, load_health, load_sessions
from monitor import Snapshot
from ui_common import FONT_UI, adapter_label

FONT = FONT_UI
MAX_SESSIONS = 10
COLUMNS = [(tr("时间"), 11), (tr("电量"), 9), (tr("时长"), 8), (tr("充入", "column"), 7), (tr("平均/峰值"), 11),
           (tr("电源"), 14)]


class HistoryWindow:
    def __init__(self, root: tk.Tk, palette, scale: float) -> None:
        self.root, self.p, self.scale = root, palette, scale
        self.win: Optional[tk.Toplevel] = None

    def show(self, s: Snapshot, insight: ChargeInsight) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        device = device_key(s) if s.connected else None
        self._fill(device, s)
        # 详情窗口是置顶的话，历史窗口也要置顶，否则会被它挡住
        try:
            self.win.attributes("-topmost", bool(self.root.attributes("-topmost")))
        except tk.TclError:
            pass
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _build(self) -> None:
        p, sc = self.p, self.scale
        win = tk.Toplevel(self.root)
        win.title(tr("电池历史"))
        win.configure(bg=p.bg)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        win.bind("<Escape>", lambda _e: win.withdraw())
        pad = int(12 * sc)

        tk.Label(win, text=tr("电池健康趋势"), font=(FONT, 10, "bold"), bg=p.bg, fg=p.fg, anchor="w").pack(
            fill="x", padx=pad, pady=(pad, 0))
        self.health_summary = tk.Label(win, font=(FONT, 8), bg=p.bg, fg=p.muted, anchor="w", justify="left")
        self.health_summary.pack(fill="x", padx=pad)
        self.chart_w, self.chart_h = int(440 * sc), int(90 * sc)
        self.chart = tk.Canvas(win, width=self.chart_w, height=self.chart_h, bg=p.card,
                               highlightthickness=1, highlightbackground=p.line)
        self.chart.pack(padx=pad, pady=(int(6 * sc), 0))

        tk.Label(win, text=tr("充电记录（最近 {n} 次）", n=MAX_SESSIONS), font=(FONT, 10, "bold"), bg=p.bg, fg=p.fg,
                 anchor="w").pack(fill="x", padx=pad, pady=(int(10 * sc), int(3 * sc)))
        self.table = tk.Frame(win, bg=p.card, highlightthickness=1, highlightbackground=p.line)
        self.table.pack(fill="x", padx=pad, pady=(0, pad))
        self.win = win

    def _fill(self, device: Optional[str], s: Snapshot) -> None:
        self._fill_health(load_health(device), s)
        self._fill_sessions(load_sessions(device, MAX_SESSIONS))

    def _fill_health(self, rows: list[dict], s: Snapshot) -> None:
        p, c, sc = self.p, self.chart, self.scale
        c.delete("all")
        points = []
        for r in rows:
            try:
                points.append((r["date"], float(r["health_pct"]), r.get("cycle_count") or ""))
            except (KeyError, ValueError):
                continue
        if points:
            first, last = points[0], points[-1]
            text = tr("已记录 {n} 天：{d0} {h0}% → {d1} {h1}%", n=len(points), d0=first[0], h0=f"{first[1]:.1f}",
                      d1=last[0], h1=f"{last[1]:.1f}")
            if first[2] and last[2]:
                text += tr("，循环 {c0} → {c1} 次", c0=first[2], c1=last[2])
            text += tr("（新电池超过 100% 属正常）")
        else:
            text = tr("还没有记录。手机连着时每天自动记一次。")
        self.health_summary.config(text=text, wraplength=self.chart_w)

        if len(points) < 2:
            c.create_text(self.chart_w / 2, self.chart_h / 2, text=tr("记满 2 天后显示趋势"), fill=p.muted, font=(FONT, 9))
            return
        values = [v for _, v, _ in points]
        lo, hi = min(values) - 0.5, max(values) + 0.5
        pad_l, pad_r, pad_t, pad_b = int(44 * sc), int(12 * sc), int(10 * sc), int(22 * sc)
        n = len(points)

        def xy(i: int, v: float) -> tuple[float, float]:
            x = pad_l + (self.chart_w - pad_l - pad_r) * (i / (n - 1))
            y = pad_t + (self.chart_h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))
            return x, y

        small = ("Segoe UI", 8)
        for v in (hi, (hi + lo) / 2, lo):
            _, y = xy(0, v)
            c.create_line(pad_l, y, self.chart_w - pad_r, y, fill=p.line)
            c.create_text(pad_l - 4, y, text=f"{v:.1f}%", anchor="e", fill=p.muted, font=small)
        coords = [coord for i, (_, v, _) in enumerate(points) for coord in xy(i, v)]
        c.create_line(*coords, fill=p.green, width=max(1, int(2 * sc)))
        for i in (0, n - 1):
            x, _ = xy(i, values[i])
            c.create_text(x, self.chart_h - pad_b / 2, text=points[i][0][5:], fill=p.muted, font=small,
                          anchor="w" if i == 0 else "e")

    def _fill_sessions(self, sessions: list[dict]) -> None:
        p, sc = self.p, self.scale
        for child in self.table.winfo_children():
            child.destroy()
        small = (FONT, 8)
        for col, (title, width) in enumerate(COLUMNS):
            tk.Label(self.table, text=title, font=small, bg=p.card, fg=p.muted, width=width, anchor="w").grid(
                row=0, column=col, sticky="w", padx=(int(8 * sc) if col == 0 else 0, 0), pady=(int(4 * sc), 0))
        if not sessions:
            tk.Label(self.table, text=tr("还没有充电记录。插着线充一次电后会出现在这里。"), font=small, bg=p.card,
                     fg=p.muted, anchor="w").grid(row=1, column=0, columnspan=len(COLUMNS), sticky="w",
                                                   padx=int(8 * sc), pady=int(6 * sc))
            return
        for row, r in enumerate(sessions, start=1):
            minutes = (r["end"] - r["start"]) / 60
            duration = tr("{m} 分钟", m=f"{minutes:.0f}") if minutes < 60 else tr("{h} 小时", h=f"{minutes / 60:.1f}")
            pct = f"{r['start_pct']}→{r['end_pct']}%"
            if r.get("partial"):
                pct = "…" + pct  # 程序启动前就插着线，开头没记到
            power = "—"
            if r.get("avg_w"):
                power = f"{r['avg_w']:.1f} / {r['peak_w']:.1f} W"
            # 「USB-C PD（5V/3A）」在表格里太长，只留类型
            source = tr("无线") if r.get("wireless") else adapter_label(r.get("adapter") or "有线")
            if r.get("adapter_w"):
                source += f" {r['adapter_w']} W"
            values = [time.strftime("%m-%d %H:%M", time.localtime(r["start"])), pct, duration,
                      f"{r['energy_wh']:.1f} Wh", power, source]
            for col, (value, (_, width)) in enumerate(zip(values, COLUMNS)):
                tk.Label(self.table, text=value, font=small, bg=p.card, fg=p.fg, width=width, anchor="w").grid(
                    row=row, column=col, sticky="w", padx=(int(8 * sc) if col == 0 else 0, 0),
                    pady=(0, int(4 * sc) if row == len(sessions) else 0))
