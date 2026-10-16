"""界面语言：源码里写简体中文，tr() 按当前语言换成英文。

语言在程序启动时确定（读设置里的 language；auto 时跟随 Windows 显示语言），中途切换会重启程序，
所以各处在生成文字时直接调用 tr() 即可，不需要事后刷新。
"""

import ctypes
import json
import os
from pathlib import Path

LANGUAGES = (("auto", "跟随系统"), ("zh", "简体中文"), ("en", "English"))
SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "iPhoneMonitor" / "settings.json"
LANG_CHINESE = 0x04  # PRIMARYLANGID

_lang = "zh"


def system_language() -> str:
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError):
        return "zh"
    return "zh" if langid & 0x3FF == LANG_CHINESE else "en"


def set_language(code: str) -> None:
    global _lang
    _lang = code if code in ("zh", "en") else system_language()


def lang() -> str:
    return _lang


def tr(text: str, context: str = "", **kw) -> str:
    """context 区分同一个中文在不同地方的译法，比如「充入」在数字卡里是状态，在表格里是列名。"""
    if _lang == "en":
        text = EN.get(f"{context}|{text}", None) or EN.get(text, text)
    return text.format(**kw) if kw else text


def _load() -> None:
    try:
        code = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("language", "auto")
    except (OSError, ValueError, AttributeError):
        code = "auto"
    set_language(code)


EN = {
    # ---- 通用 / 状态
    "iPhone 状态": "iPhone Monitor",
    "iPhone 状态：正在连接…": "iPhone Monitor: connecting…",
    "跟随系统": "System default",
    "正在连接…": "Connecting…",
    "等待连接…": "Waiting for connection…",
    "已连接": "Connected",
    "未连接": "Not connected",
    "已关闭": "Off",
    "已充满": "Fully charged",
    "充电中": "Charging",
    "已接电源 · 暂停充电": "Plugged in · not charging",
    "使用电池": "On battery",
    "已到上限 {limit}%": "At limit {limit}%",
    "未接电源": "Not plugged in",
    "无线 / MagSafe": "Wireless / MagSafe",
    "有线": "Wired",
    "无线": "Wireless",
    "USB 主机": "USB host",
    "USB 充电器": "USB charger",
    "不到 1 分钟": "under 1 min",
    "{m} 分钟": "{m} min",
    "{h} 小时 {m} 分钟": "{h} h {m} min",
    "{h} 小时": "{h} h",
    # ---- 连接错误（monitor）
    "未找到 Apple 设备服务（需安装 Apple Devices 或 iTunes）":
        "Apple device service not found (install Apple Devices or iTunes)",
    "未检测到 iPhone：请插线；已插线就拔插一次数据线":
        "No iPhone found: plug it in, or replug the cable once",
    "请解锁 iPhone 并点「信任此电脑」": "Unlock the iPhone and tap “Trust This Computer”",
    "读取超时，正在重连…": "Timed out, reconnecting…",
    "连接中断，正在重连…": "Connection lost, reconnecting…",
    "需在 iPhone 上开启开发者模式": "Turn on Developer Mode on the iPhone",
    "挂载镜像超时：请解锁 iPhone，稍后自动重试": "Disk image mount timed out: unlock the iPhone, retrying soon",
    "开发者镜像下载/签名失败，稍后重试": "Developer disk image download/signing failed, retrying soon",
    "需用 USB 线连接（Wi-Fi 下不支持）": "Needs a USB cable (not supported over Wi-Fi)",
    "开发者通道断开，正在重连…": "Developer channel lost, reconnecting…",
    "待机（打开详情窗口时读取）": "Idle (reads when the window is open)",
    "正在建立开发者通道…": "Opening developer channel…",
    "正在挂载开发者镜像（请保持手机解锁）…": "Mounting developer disk image (keep the iPhone unlocked)…",
    "正在读取…": "Reading…",
    # ---- 文字条 / 托盘提示
    "需开发者模式": "Dev Mode needed",
    "解锁手机读取": "Unlock to read",
    "需 USB 连接": "USB needed",
    "热点接管了电脑网络": "Hotspot in use",
    "{cpu} · 内存 {mem}": "{cpu} · Mem {mem}",
    "⚠ 手机热点正在接管电脑网络": "⚠ iPhone hotspot is carrying this PC's traffic",
    "{name}：{status}": "{name}: {status}",
    "电池 {w}": "Battery {w}",
    "整机耗电 {w}": "Phone load {w}",
    " · 内存 {pct}%": " · Mem {pct}%",
    "连接：{conn}": "Connection: {conn}",
    # ---- 菜单
    "显示详情": "Show details",
    "立即刷新": "Refresh now",
    "图标显示": "Tray icon shows",
    "电量 %": "Battery %",
    "电池功率 W": "Battery power W",
    "温度 °C": "Temperature °C",
    "任务栏文字条": "Taskbar strip",
    "文字条显示 CPU / 内存": "Strip shows CPU / memory",
    "文字条位置": "Strip position",
    "靠右（托盘旁）": "Right (next to tray)",
    "靠左（小组件旁）": "Left (next to widgets)",
    "显示托盘图标": "Show tray icon",
    "CPU / 内存监测": "CPU / memory monitoring",
    "仅通过 USB 连接": "USB only",
    "充电上限（与手机设置一致）": "Charge limit (match the iPhone setting)",
    "不限（100%）": "No limit (100%)",
    "电池历史…": "Battery history…",
    "手机声音转到电脑（蓝牙）": "Play iPhone audio on this PC (Bluetooth)",
    "刷新间隔": "Refresh interval",
    "{n} 秒": "{n} s",
    "详情窗口置顶": "Keep details window on top",
    "开机自动启动": "Start with Windows",
    "退出": "Exit",
    "显示 / 隐藏详情": "Show / hide details",
    "第二行显示 CPU / 内存": "Second line: CPU / memory",
    "隐藏任务栏文字条（改用托盘图标）": "Hide taskbar strip (use tray icon)",
    # ---- 通知
    "电量只剩 {pct}%，记得充电": "Only {pct}% left. Time to charge.",
    "iPhone 电量低": "iPhone battery low",
    "电池温度 {t}°C，建议暂停重度使用或移开充电器": "Battery at {t}°C. Ease off heavy use or unplug the charger.",
    "iPhone 温度偏高": "iPhone running hot",
    "电池已充满": "Battery fully charged",
    "电脑正通过 USB 使用手机热点（{ip}），单位内网可能打不开。关闭 iPhone 的「个人热点」即可。":
        "This PC is using the iPhone hotspot over USB ({ip}), so intranet sites may not open. "
        "Turn off Personal Hotspot on the iPhone.",
    "iPhone 热点接管了电脑网络": "iPhone hotspot took over the network",
    "iPhone 插电仍掉电": "iPhone draining while plugged in",
    "iPhone 充电慢": "iPhone charging slowly",
    "换一根短而好的线，或用 20 W 以上充电头。": "Try a short, good cable or a 20 W+ charger.",
    "用 20 W 以上充电头会快很多。": "A 20 W+ charger is much faster.",
    "手机声音会从电脑播放（{name}）。没声音的话，在 iPhone 控制中心的音频输出里选这台电脑。":
        "iPhone audio now plays on this PC ({name}). No sound? Pick this PC as the audio output "
        "in the iPhone's Control Center.",
    "手机声音已转到电脑": "iPhone audio is on this PC",
    "已关闭（右键菜单可开启）": "Off (turn on from the right-click menu)",
    # ---- 充电分析
    "到上限，暂由电池供电（正常）": "At limit, running on battery for now (normal)",
    "插电仍掉电": "Draining while plugged in",
    "插电仍掉电：{reasons}": "Draining while plugged in: {reasons}",
    "充电慢 {w} W": "Charging slowly at {w} W",
    "充电慢 {w} W：{reasons}": "Charging slowly at {w} W: {reasons}",
    "供电 {inp} W < 耗电 {load} W": "supply {inp} W < load {load} W",
    "电源电压偏低（约 {v} V）": "low source voltage (~{v} V)",
    "线路电阻大（约 {r} Ω）": "high cable resistance (~{r} Ω)",
    "线材压降（仅 {v} V）": "cable voltage drop (only {v} V)",
    "电源仅 {w} W": "power source only {w} W",
    "开着热点": "hotspot on",
    "负载高 {pct}%": "high load {pct}%",
    # ---- 详情窗口
    "整机耗电": "Phone load",
    "外部供电": "Power in",
    "电池": "Battery",
    "CPU · 内存": "CPU · Memory",
    "近 10 分钟": "Last 10 minutes",
    "功率": "Power",
    "CPU / 内存": "CPU / Memory",
    "内存": "Memory",
    "充电器": "Charger",
    "健康度": "Health",
    "电源电压": "Source voltage",
    "容量": "Capacity",
    "线路电阻": "Cable resistance",
    "存储": "Storage",
    "声音转电脑": "Audio to PC",
    "温度": "Temperature",
    "占用最高的进程": "Top processes",
    "刷新": "Refresh",
    "电池历史": "Battery history",
    "电脑正通过 USB 使用手机个人热点（{ip}），单位内网可能打不开。关闭 iPhone「设置 → 个人热点 → 允许其他人加入」即可。":
        "This PC is using the iPhone's Personal Hotspot over USB ({ip}), so intranet sites may not open. "
        "Turn off Settings → Personal Hotspot → Allow Others to Join on the iPhone.",
    "● 已连接 · {conn}": "● Connected · {conn}",
    "约 {time}到 {pct}%": "{time} to {pct}%",
    "手机此刻用电": "measured now",
    "按放电估算": "estimated from discharge",
    "不充不放": "idle",
    "充入": "charging",
    "放电": "discharging",
    "约 {v} V，偏低": "~{v} V, low",
    "约 {v} V，正常": "~{v} V, normal",
    "约 {r} Ω，偏大": "~{r} Ω, high",
    "约 {r} Ω，正常": "~{r} Ω, normal",
    "手机端 {v} V": "{v} V at the phone",
    "收集数据中…": "Collecting data…",
    " · 循环 {n} 次": " · {n} cycles",
    "剩 {free} / ": "{free} free / ",
    "更新于 {t}": "Updated {t}",
    "共 {n} 个进程": "{n} processes",
    "打开后读取": "Reads when open",
    "（监测）": " (monitor)",
    "暂无数据": "No data yet",
    # ---- 历史窗口
    "电池健康趋势": "Battery health",
    "充电记录（最近 {n} 次）": "Charging sessions (last {n})",
    "时间": "Start",
    "电量": "Level",
    "时长": "Duration",
    "平均/峰值": "Avg / peak",
    "column|充入": "Energy",
    "电源": "Source",
    "已记录 {n} 天：{d0} {h0}% → {d1} {h1}%": "{n} days logged: {d0} {h0}% → {d1} {h1}%",
    "，循环 {c0} → {c1} 次": ", cycles {c0} → {c1}",
    "（新电池超过 100% 属正常）": " (over 100% is normal for a new battery)",
    "还没有记录。手机连着时每天自动记一次。": "Nothing logged yet. One entry is saved per day while the iPhone is connected.",
    "记满 2 天后显示趋势": "The trend appears after 2 days",
    "还没有充电记录。插着线充一次电后会出现在这里。": "No sessions yet. Charge once over a cable and it shows up here.",
    # ---- 蓝牙音频
    "不可用（系统不支持）": "Unavailable (not supported by Windows)",
    "连接已断开，稍后重连": "Disconnected, reconnecting soon",
    "未找到已配对的手机（先在系统里配对蓝牙）": "No paired phone (pair it in Windows Bluetooth settings first)",
    "正在连接 {name}…": "Connecting to {name}…",
    "已连接 {name}": "Connected to {name}",
    "手机不在附近或蓝牙未开": "Phone not nearby or Bluetooth off",
    "被系统拒绝": "Denied by Windows",
    "连接失败": "Connection failed",
    "{reason}，稍后重试": "{reason}, retrying soon",
    "连接失败，稍后重试": "Connection failed, retrying soon",
}

_load()
