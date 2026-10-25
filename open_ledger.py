"""打开总账网页。给桌面快捷方式「打开总账.bat」用。

为什么单独一个 py 而不是让 .bat 直接 start 总账.html：
    .bat 按 OEM 代码页读字节，UTF-8 存的"总账.html"四个字在中文
    Windows 上会变成乱码，文件找不到；在英文 Windows 上更糟。
    Python 按 UTF-8 读自己的源文件，路径怎么都不会错。

还没有总账时（全新安装、当天还没说话）：顺手刷一次再打开。
刷的过程可能几秒到几十秒，用无窗口方式跑，不弹黑屏。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import webbrowser
from pathlib import Path

root = Path(__file__).resolve().parent
LEDGER = root / "reports" / "总账.html"
CREATE_NO_WINDOW = 0x08000000


def refresh() -> None:
    py = root / "python" / "python.exe"
    if not py.exists():                      # 开发机布局
        py = root / ".venv" / "Scripts" / "python.exe"
    subprocess.run([str(py), str(root / "vdigest.py"), "--no-llm", "--live"],
                   cwd=str(root), creationflags=CREATE_NO_WINDOW)


def main() -> int:
    if not LEDGER.exists():
        refresh()
    if LEDGER.exists():
        webbrowser.open(LEDGER.as_uri())
        return 0
    ctypes.windll.user32.MessageBoxW(
        0,
        "总账还没有生成。\n\n对着麦克风说几句话，或等 5 分钟 —— "
        "后台的 vlive 会自动刷新，然后再点本快捷方式。",
        "语音总账", 0x40)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
