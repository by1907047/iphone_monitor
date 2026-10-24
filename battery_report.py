"""Windows 电池报告（powercfg /batteryreport）：补上程序没运行时的笔记本电池历史。

Windows 自己会记：
- 最近约 7 天，每次接电 / 拔电 / 睡眠 / 唤醒时的剩余容量和这段时间放了多少电（RecentUsage）；
- 从装系统起按天、按周汇总的满电容量、循环次数、用电池的时长和电量（History）。

程序每天导入一次：最近几天的放电按天补进「电池损耗」（比程序自己的记录完整，因为不需要程序开着），
长期记录合并保存到 laptop_history.json——Windows 会删掉太旧的记录，存一份就不会丢。
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

HISTORY_FILE = Path(os.environ.get("APPDATA", Path.home())) / "iPhoneMonitor" / "laptop_history.json"
CREATE_NO_WINDOW = 0x08000000
MWH_TO_MJ = 3600  # 1 mWh = 3600 mW·s


def _seconds(duration: Optional[str]) -> float:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", duration or "PT0S")
    return sum(float(v or 0) * k for v, k in zip(m.groups(), (86400, 3600, 60, 1))) if m else 0.0


def run_report(timeout: float = 60) -> Optional[ET.Element]:
    """生成并读取 XML 版电池报告；台式机或命令失败时返回 None。约 1–2 秒，放在后台线程里调用。"""
    out = Path(tempfile.gettempdir()) / "iphonemonitor-battery-report.xml"
    try:
        subprocess.run(["powercfg", "/batteryreport", "/xml", "/output", str(out)], capture_output=True,
                       timeout=timeout, creationflags=CREATE_NO_WINDOW, check=True)
        return ET.parse(out).getroot()
    except (OSError, subprocess.SubprocessError, ET.ParseError) as exc:
        log.info("读取 Windows 电池报告失败: %r", exc)
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def parse(root: ET.Element) -> Optional[dict]:
    ns = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else ""
    tag = (lambda name: f"{{{ns}}}{name}") if ns else (lambda name: name)
    battery = next(root.iter(tag("Battery")), None)
    if battery is None:
        return None
    field = lambda name: (battery.findtext(tag(name)) or "").strip()  # noqa: E731
    key = f"laptop|{field('Manufacturer')} {field('Id')}".strip()

    days: dict[str, dict] = {}
    for e in root.iter(tag("UsageEntry")):
        a = e.attrib
        discharge = int(a.get("Discharge") or 0)  # 这一段时间放掉的电（mWh），负数表示充入
        if not discharge:
            continue
        day = days.setdefault(a["LocalTimestamp"][:10], dict(plugged_mj=0.0, unplugged_mj=0.0, charge_mj=0.0))
        if discharge < 0:
            day["charge_mj"] += -discharge * MWH_TO_MJ
        else:
            day["plugged_mj" if a.get("Ac") == "1" else "unplugged_mj"] += discharge * MWH_TO_MJ

    periods = {}
    for e in root.iter(tag("HistoryEntry")):
        a = e.attrib
        periods[a["LocalStartDate"][:10]] = dict(
            end=a["LocalEndDate"][:10], full_mwh=int(a.get("FullChargeCapacity") or 0),
            design_mwh=int(a.get("DesignCapacity") or 0), cycles=int(a.get("CycleCount") or 0),
            battery_wh=(int(a.get("ActiveDcEnergy") or 0) + int(a.get("CsDcEnergy") or 0)) / 1000,
            battery_h=(_seconds(a.get("ActiveDcTime")) + _seconds(a.get("CsDcTime"))) / 3600,
            ac_h=_seconds(a.get("ActiveAcTime")) / 3600)
    return dict(key=key, days=days, periods=periods)


def save_periods(key: str, periods: dict) -> None:
    """长期记录按起始日期合并保存：Windows 删掉的旧记录，这里仍然留着。"""
    data = load_history_file()
    stored = data.setdefault(key, {})
    stored.update(periods)
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("写笔记本电池长期记录失败: %r", exc)


def load_history_file() -> dict:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def monthly(key: str) -> list[dict]:
    """按月汇总：月末的健康度和循环次数，当月用电池放掉的电（折合循环次数）。"""
    months: dict[str, dict] = {}
    for start, p in sorted(load_history_file().get(key, {}).items()):
        m = months.setdefault(start[:7], dict(month=start[:7], battery_wh=0.0, battery_h=0.0))
        m["battery_wh"] += p["battery_wh"]
        m["battery_h"] += p["battery_h"]
        if p["design_mwh"]:
            m["health"] = p["full_mwh"] * 100 / p["design_mwh"]
            m["design_wh"] = p["design_mwh"] / 1000
        m["cycles"] = p["cycles"]
    return list(months.values())
