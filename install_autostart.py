"""开机自启注册 / 卸载。

为什么不用启动文件夹里的快捷方式：
    建 .lnk 需要 WScript.Shell 这个 COM 组件，本机安全策略把它拦了。
    改用注册表的 Run 键 —— 这是 Windows 官方的自启位置，任务管理器的
    "启动"标签页里能直接看到、能直接禁用，比快捷方式还透明。

装了什么：
    VoiceRecorder  后台录音 + 转写 + 声纹（pythonw，无窗口）
    VoiceBootTasks 登录 90 秒后补写上一次开机的总账 + 清理磁盘
    VoiceLive      每 5 分钟刷新一次总账网页，让内容停在当下
    Ollama         本地大模型（不装这条，总账就只能用程序拼的干巴句子）

**WorkBuddy 不用我们管**：它自己就往 Run 键写了一条 `WorkBuddy.WorkBuddy`，
已经会开机自启。这里再写一条会变成启动两个实例，所以刻意不碰它。

为什么 pythonw 而不是 python：
    python 会弹一个黑窗口，用户会以为是病毒；pythonw 完全无窗口。

为什么 Ollama 要单独加：
    它安装时问过"是否开机启动"，当时没勾。结果就是：每次冷启动，总账
    生成的时候模型还没起来，只好退回程序拼的段落。这台机器又不是常开，
    等于永远用不上模型。必须补上。

用法：
    python install_autostart.py            # 装
    python install_autostart.py --list     # 看现在装了什么
    python install_autostart.py --remove   # 卸
"""
from __future__ import annotations

import argparse
import winreg
from pathlib import Path

import vcfg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 安装包的布局是 <根>\python\pythonw.exe（便携 Python 直接放在根目录），
# 开发机还是 .venv 布局。两个都认，安装包优先。
PYW = vcfg.ROOT / "python" / "pythonw.exe"
if not PYW.exists():
    PYW = vcfg.ROOT / ".venv" / "Scripts" / "pythonw.exe"
WORKBUDDY = Path.home() / "AppData/Local/Programs/WorkBuddy/WorkBuddy.exe"
OLLAMA_APP = Path.home() / "AppData/Local/Programs/Ollama/ollama app.exe"


def _wind_root() -> Path:
    """屏幕记录（Windrecorder）装在哪。

    开发机老位置是 E:\AI\Windrecorder；整包搬到别的电脑后，如果那边也装
    了 Windrecorder 但在别的盘，就在 config.json 里写 "windrecorder_dir"。
    指不到地方就当没有屏幕记录 —— 语音链路本来就不依赖它。
    """
    d = (vcfg.load().get("windrecorder_dir") or "").strip()
    return Path(d) if d else Path(r"E:\AI\Windrecorder")


WR_PYW = _wind_root() / ".venv" / "Scripts" / "pythonw.exe"
WR_LAUNCH = _wind_root() / "scripts" / "launch_app.py"

# 屏幕记录。2026-10-24 审查发现：它从来没有自启项，一重启就整条屏幕链路
# 归零 —— 截图、OCR、屏幕活动全没了，而语音侧完全不受影响，安静得像没事
# 一样，用户根本不会发现屏幕记录断了。必须补上。
# 用 --background 直接进后台：不打印"正在加载"、不等托盘、不弹窗口。
WR_PYW = Path(r"E:\AI\Windrecorder\.venv\Scripts\pythonw.exe")
WR_LAUNCH = Path(r"E:\AI\Windrecorder\scripts\launch_app.py")

ENTRIES = {
    "VoiceRecorder": f'"{PYW}" "{vcfg.ROOT / "voice_daemon.py"}"',
    "VoiceBootTasks": f'"{PYW}" "{vcfg.ROOT / "boot_tasks.py"}"',
    # 每 5 分钟把"当前这场开机"重算一遍，让总账网页停在当下。
    # 没有它，页面永远滞后到上一次关机 —— 正在用的这场一行都不会有。
    "VoiceLive": f'"{PYW}" "{vcfg.ROOT / "vlive.py"}"',
}

# 只在找得到的时候才装，别为了它们让整个安装失败
OPTIONAL = {
    "Ollama": (OLLAMA_APP, f'"{OLLAMA_APP}"'),
    "Windrecorder": (
        WR_PYW,
        f'"{WR_PYW}" -u "{WR_LAUNCH}" --background "{vcfg.DATA / "wind_ready"}"',
    ),
}

_OPTIONAL_NOTE = {
    "Ollama": "本地大模型，不加这条总结会退化成干巴句子",
    "Windrecorder": "屏幕记录，不加这条重启后就没有屏幕数据了",
}

# 以前误加的重复项，装的时候顺手清掉
LEGACY = ("WorkBuddy",)


def _has_workbuddy_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            n = winreg.QueryInfoKey(k)[1]
            for i in range(n):
                name, val, _ = winreg.EnumValue(k, i)
                if "workbuddy" in name.lower() and "workbuddy.exe" in val.lower():
                    return True
    except OSError:
        pass
    return False


def install() -> int:
    missing = [str(p) for p in (PYW,) if not p.exists()]
    if missing:
        print("✗ 找不到这些文件，先别装：")
        for m in missing:
            print("   ", m)
        return 1
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        for name in LEGACY:
            try:
                winreg.DeleteValue(k, name)
                print(f"- 清掉了重复的旧项 {name}")
            except FileNotFoundError:
                pass
        for name, val in ENTRIES.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            print(f"✓ {name}")
            print(f"    {val}")
        for name, (path, val) in OPTIONAL.items():
            if path.exists():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
                print(f"✓ {name}（{_OPTIONAL_NOTE.get(name, '')}）")
            else:
                print(f"- 跳过 {name}：没找到 {path}")

    if _has_workbuddy_autostart():
        print("\n✓ WorkBuddy 本来就有自启项，不用我们管")
    else:
        print("\n⚠ 没找到 WorkBuddy 的自启项。它通常自己会写，"
              "如果开机没自动起来，检查 WorkBuddy 设置里的开机启动开关。")
    print("下次登录就会自动跑起来。")
    print("想确认：任务管理器 → 启动应用，应该能看到这几项。")
    return 0


def remove() -> int:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        for name in list(ENTRIES) + list(OPTIONAL):
            try:
                winreg.DeleteValue(k, name)
                print(f"✓ 已删除 {name}")
            except FileNotFoundError:
                print(f"- {name} 本来就没有")
    return 0


def show() -> int:
    mine = set(ENTRIES) | set(OPTIONAL)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_QUERY_VALUE) as k:
        n = winreg.QueryInfoKey(k)[1]
        found = False
        for i in range(n):
            name, val, _ = winreg.EnumValue(k, i)
            mark = "←本项目" if name in mine else ""
            print(f"{name:<22} {val}  {mark}")
            found = found or name in mine
        if not found:
            print("（本项目的自启项还没装）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        return show()
    if a.remove:
        return remove()
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
