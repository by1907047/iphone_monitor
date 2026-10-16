"""充电分析：充电上限、预计充满时间、插着线掉电 / 慢充诊断、每日电池健康记录、充电记录。

只依赖 monitor.Snapshot，界面线程每拿到一份新数据调用一次 Insights.feed()。
历史数据很小：健康度每天一行（CSV），充电每次一行（JSONL），都在 %APPDATA%\\iPhoneMonitor 下。
"""

import csv
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from monitor import Snapshot

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("APPDATA", Path(__file__).parent)) / "iPhoneMonitor"
HEALTH_FILE = DATA_DIR / "battery_health.csv"
SESSIONS_FILE = DATA_DIR / "charge_sessions.jsonl"
HEALTH_FIELDS = ["date", "device", "health_pct", "cycle_count", "full_mah", "design_mah"]

CHARGE_LIMITS = (80, 85, 90, 95, 100)  # 与 iPhone「设置 → 电池 → 充电」一致；100 表示不限
DRAIN_W = 0.1  # 电池功率低于 -0.1 W 算放电
SLOW_CHARGE_W = 2.5  # 充入电池的功率低于它算慢充（离目标还远时）
LOW_PORT_W = 7.5  # Apple：有线 ≤ 7.5 W 可能充得慢；电脑 USB 口通常就在这个档
SUPPLY_MARGIN_W = 0.5  # 输入比耗电少这么多以上，才算供电不够
VOLTAGE_SAG_V = 0.4  # 手机端电压比充电器标称低这么多，说明线材/接口压降大
HIGH_LOAD_CPU = 50
DRAIN_ALERT_AFTER = 120  # 持续这么久才提醒，避免瞬时波动
SLOW_ALERT_AFTER = 300
SESSION_MIN_SECONDS = 120
SESSION_GAP_SECONDS = 120  # 断开超过这么久就结束本次充电
MAX_INTEGRATION_DT = 60  # 两次采样间隔太大就不累计能量（中间可能睡眠或断开）
ETA_EMA_SECONDS = 90  # 充电电流的平滑时间常数
SUPPLY_MIN_SPREAD_A = 0.5  # 电流范围至少这么大，才能把「电源电压」和「线路电阻」分开
SUPPLY_MAX_SAMPLES = 200
HIGH_LINE_OHM = 0.2  # 线路电阻超过它就算偏大（正常的线 + 接口一般在 0.1 Ω 左右）
LOW_SOURCE_V = 4.75  # 估算出的电源空载电压低于它，说明电源本身电压偏低（5 V 档）


@dataclass(frozen=True)
class SupplyEstimate:
    source_v: float  # 电流为零时的电压 ≈ 电源（电脑口/充电器）实际输出电压
    line_ohm: float  # 线材 + 接口（+ 电源内部）的总电阻
    samples: int


class SupplyEstimator:
    """收集手机端「输入电压-输入电流」读数做线性拟合：V = V0 − I × R。

    电流小时电压正常、电流大时电压掉得多 → 线路电阻大；电流再小电压也上不去 → 电源本身电压低。
    """

    def __init__(self) -> None:
        self.samples: list[tuple[float, float]] = []
        self.key: tuple = ()

    def feed(self, s: Snapshot) -> Optional[SupplyEstimate]:
        key = (s.adapter, s.adapter_voltage_v, bool(s.wireless))
        if key != self.key:  # 换了充电器/线，旧数据作废
            self.key, self.samples = key, []
        if (s.external_power and not s.wireless and s.input_voltage_v and s.input_current_a is not None
                and s.input_current_a > 0.02):
            point = (round(s.input_current_a, 3), round(s.input_voltage_v, 3))
            if not self.samples or self.samples[-1] != point:  # 遥测大约 20 秒才变一次，去重
                self.samples.append(point)
                del self.samples[:-SUPPLY_MAX_SAMPLES]
        return self.estimate()

    def estimate(self) -> Optional[SupplyEstimate]:
        if len(self.samples) < 2:
            return None
        currents = [i for i, _ in self.samples]
        if max(currents) - min(currents) < SUPPLY_MIN_SPREAD_A:
            return None
        n = len(self.samples)
        mean_i = sum(currents) / n
        mean_v = sum(v for _, v in self.samples) / n
        var_i = sum((i - mean_i) ** 2 for i in currents)
        slope = sum((i - mean_i) * (v - mean_v) for i, v in self.samples) / var_i
        return SupplyEstimate(source_v=mean_v - slope * mean_i, line_ohm=max(0.0, -slope), samples=n)


@dataclass(frozen=True)
class ChargeInsight:
    state: str = ""  # 充电中 / 已到上限 80% / 已充满 / 插着线在掉电 / 使用电池 …
    eta_minutes: Optional[float] = None
    eta_target: Optional[int] = None
    diagnosis: str = ""  # 诊断说明，空表示正常
    level: str = "ok"  # ok / info / warn
    alert: str = ""  # 需要弹通知的类别：drain / slow；空表示不需要
    supply: Optional[SupplyEstimate] = None


def device_key(s: Snapshot) -> str:
    return f"{s.name}|{s.model}"


class Insights:
    def __init__(self, charge_limit: Callable[[], int]) -> None:
        self.charge_limit = charge_limit
        self.ema_ma: Optional[float] = None
        self.last_ts = 0.0
        self.drain_since: Optional[float] = None
        self.slow_since: Optional[float] = None
        self.session: Optional[dict] = None
        self.last_seen = 0.0
        self.first_feed = True
        self.supply = SupplyEstimator()
        self.health_logged: dict[str, str] = self._read_last_health_dates()

    # ---- 主入口 ----
    def feed(self, s: Snapshot, tether: bool = False, cpu_pct: Optional[float] = None) -> ChargeInsight:
        now = s.timestamp or time.time()
        if not s.connected or s.percent is None:
            if self.session and now - self.last_seen > SESSION_GAP_SECONDS:
                self._end_session("disconnected")
            return ChargeInsight(state=s.status)
        dt = now - self.last_ts if self.last_ts else 0.0
        self.last_ts = self.last_seen = now

        self.supply.feed(s)
        self._update_current(s, dt)
        self._track_session(s, dt, now)
        self._log_health(s)
        self.first_feed = False
        return self._analyse(s, now, tether, cpu_pct)

    def close(self) -> None:
        if self.session:
            self._end_session("app_exit")

    # ---- 状态、预计时间、诊断 ----
    def at_limit(self, s: Snapshot) -> bool:
        limit = self.charge_limit()
        return (limit < 100 and bool(s.external_power) and not s.charging
                and s.percent is not None and s.percent >= limit - 1)

    def _analyse(self, s: Snapshot, now: float, tether: bool, cpu_pct: Optional[float]) -> ChargeInsight:
        limit = self.charge_limit()
        target = limit if limit < 100 else 100
        at_limit = self.at_limit(s)
        draining = s.battery_w is not None and s.battery_w < -DRAIN_W

        if not s.external_power:
            state = "使用电池"
        elif s.fully_charged or s.percent >= 100:
            state = "已充满"
        elif at_limit:
            state = f"已到上限 {limit}%"
        elif s.charging:
            state = "充电中"
        else:
            state = "已接电源 · 暂停充电"

        eta = None
        if s.charging and s.percent < target and self.ema_ma and self.ema_ma > 50 and s.full_mah:
            eta = (target - s.percent) / 100 * s.full_mah / self.ema_ma * 60
            if eta > 24 * 60:
                eta = None

        diagnosis, level, alert = "", "ok", ""
        if s.external_power and draining:
            self.slow_since = None
            short_supply = (s.power_in_w is not None and s.system_load_w is not None
                            and s.power_in_w + SUPPLY_MARGIN_W < s.system_load_w)
            if at_limit and not short_supply:
                # 到上限后 iPhone 会暂时改用电池供电、掉一点再补，属正常
                diagnosis, level = "到上限，暂由电池供电（正常）", "info"
                self.drain_since = None
            else:
                self.drain_since = self.drain_since or now
                reasons = self._reasons(s, tether, cpu_pct)
                diagnosis = "插电仍掉电" + (f"：{' · '.join(reasons)}" if reasons else "")
                level = "warn"
                if now - self.drain_since >= DRAIN_ALERT_AFTER:
                    alert = "drain"
        else:
            self.drain_since = None
            slow = (s.charging and s.battery_w is not None and 0 <= s.battery_w < SLOW_CHARGE_W
                    and s.percent < target - 5)
            if slow:
                self.slow_since = self.slow_since or now
                reasons = self._reasons(s, tether, cpu_pct)
                diagnosis = f"充电慢 {s.battery_w:.1f} W" + (f"：{' · '.join(reasons)}" if reasons else "")
                level = "warn"
                if now - self.slow_since >= SLOW_ALERT_AFTER:
                    alert = "slow"
            else:
                self.slow_since = None
        return ChargeInsight(state=state, eta_minutes=eta, eta_target=target if eta else None,
                             diagnosis=diagnosis, level=level, alert=alert, supply=self.supply.estimate())

    def _reasons(self, s: Snapshot, tether: bool, cpu_pct: Optional[float]) -> list[str]:
        """简短原因，按重要程度排，最多两条。"""
        reasons = []
        if s.power_in_w is not None and s.system_load_w is not None and s.power_in_w < s.system_load_w:
            reasons.append(f"供电 {s.power_in_w:.1f} W < 耗电 {s.system_load_w:.1f} W")
        est = self.supply.estimate()
        if est and est.source_v < LOW_SOURCE_V:
            reasons.append(f"电源电压偏低（约 {est.source_v:.2f} V）")
        elif est and est.line_ohm >= HIGH_LINE_OHM:
            reasons.append(f"线路电阻大（约 {est.line_ohm:.2f} Ω）")
        elif (s.input_voltage_v and s.adapter_voltage_v and not s.wireless
                and s.adapter_voltage_v - s.input_voltage_v >= VOLTAGE_SAG_V):
            reasons.append(f"线材压降（仅 {s.input_voltage_v:.2f} V）")
        if s.adapter_watts and s.adapter_watts <= LOW_PORT_W and not s.wireless:
            reasons.append(f"电源仅 {s.adapter_watts} W")
        if tether:
            reasons.append("开着热点")
        if cpu_pct is not None and cpu_pct >= HIGH_LOAD_CPU:
            reasons.append(f"负载高 {cpu_pct:.0f}%")
        return reasons[:2]

    def _update_current(self, s: Snapshot, dt: float) -> None:
        """充电电流做指数平滑，预计时间才不会跳来跳去。"""
        if not s.charging or s.current_ma is None or s.current_ma <= 0:
            self.ema_ma = None
            return
        if self.ema_ma is None or dt <= 0 or dt > MAX_INTEGRATION_DT:
            self.ema_ma = float(s.current_ma)
        else:
            alpha = min(1.0, dt / ETA_EMA_SECONDS)
            self.ema_ma += alpha * (s.current_ma - self.ema_ma)

    # ---- 充电记录 ----
    def _track_session(self, s: Snapshot, dt: float, now: float) -> None:
        if s.external_power and self.session is None:
            self.session = dict(
                device=device_key(s), start=now, start_pct=s.percent, end_pct=s.percent, max_pct=s.percent,
                energy_wh=0.0, charge_seconds=0.0, charge_wh_for_avg=0.0, peak_w=0.0,
                adapter=s.adapter, adapter_w=s.adapter_watts, wireless=bool(s.wireless),
                partial=self.first_feed,  # 程序启动时已经插着线，开头没记到
                reached_limit=False,
            )
        elif not s.external_power and self.session is not None:
            self._end_session("unplugged")
            return
        if self.session is None:
            return
        sess = self.session
        sess["end"], sess["end_pct"] = now, s.percent
        sess["max_pct"] = max(sess["max_pct"], s.percent)
        if self.at_limit(s):
            sess["reached_limit"] = True
        if s.battery_w is not None and s.battery_w > 0.05 and 0 < dt <= MAX_INTEGRATION_DT:
            sess["energy_wh"] += s.battery_w * dt / 3600
            sess["charge_seconds"] += dt
            sess["peak_w"] = max(sess["peak_w"], s.battery_w)

    def _end_session(self, reason: str) -> None:
        sess, self.session = self.session, None
        if not sess or "end" not in sess:
            return
        duration = sess["end"] - sess["start"]
        if duration < SESSION_MIN_SECONDS and sess["max_pct"] - sess["start_pct"] < 1:
            return
        record = dict(
            device=sess["device"], start=round(sess["start"]), end=round(sess["end"]),
            start_pct=sess["start_pct"], end_pct=sess["end_pct"], max_pct=sess["max_pct"],
            energy_wh=round(sess["energy_wh"], 2),
            avg_w=round(sess["energy_wh"] * 3600 / sess["charge_seconds"], 2) if sess["charge_seconds"] else None,
            peak_w=round(sess["peak_w"], 2), adapter=sess["adapter"], adapter_w=sess["adapter_w"],
            wireless=sess["wireless"], partial=sess["partial"], reached_limit=sess["reached_limit"],
            end_reason=reason,
        )
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with SESSIONS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.info("记录了一次充电：%s%% → %s%%，%.2f Wh", record["start_pct"], record["end_pct"], record["energy_wh"])
        except OSError as exc:
            log.warning("写充电记录失败: %r", exc)

    # ---- 每日健康记录 ----
    def _read_last_health_dates(self) -> dict[str, str]:
        last: dict[str, str] = {}
        for row in load_health():
            last[row["device"]] = row["date"]
        return last

    def _log_health(self, s: Snapshot) -> None:
        if s.health_pct is None:
            return
        today, key = time.strftime("%Y-%m-%d"), device_key(s)
        if self.health_logged.get(key) == today:
            return
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            new_file = not HEALTH_FILE.exists()
            with HEALTH_FILE.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=HEALTH_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(dict(date=today, device=key, health_pct=s.health_pct, cycle_count=s.cycle_count,
                                     full_mah=s.full_mah, design_mah=s.design_mah))
            self.health_logged[key] = today
        except OSError as exc:
            log.warning("写电池健康记录失败: %r", exc)


# ---- 读取历史（给历史窗口用） ----
def load_health(device: Optional[str] = None) -> list[dict]:
    try:
        with HEALTH_FILE.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return [r for r in rows if device is None or r.get("device") == device]


def load_sessions(device: Optional[str] = None, limit: int = 30) -> list[dict]:
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if device is None or r.get("device") == device:
            records.append(r)
    return records[-limit:][::-1]  # 最新的在前
