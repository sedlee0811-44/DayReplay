"""模拟"某些包没装"，验证精简安装包会不会炸。

为什么要这么测
--------------
直接删 site-packages 里的目录去试，有两个坏处：一是删错了恢复起来
麻烦，二是"import 成功"只说明顶层不依赖，跑到一半才 import 深层模块
的情况照样会炸。

这里用 meta_path 拦截器：装一个假的 finder，遇到黑名单里的包名就抛
ImportError。效果和"根本没装这个包"完全一样，但不动磁盘。

用法：
    python tools/probe_trim.py
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from pathlib import Path

# 这些包在精简版安装包里不打算带。逐个验证"少了它会不会炸"。
BLACKLIST = [
    "numba", "llvmlite", "pyarrow", "sympy",
    "onnxruntime", "sklearn", "jieba", "modelscope", "networkx",
]


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in BLACKLIST:
            raise ImportError(f"[probe] blocked: {fullname}")
        return None


def main() -> int:
    # 项目模块在仓库根目录，脚本在 tools/ —— 不把根目录加进 sys.path，
    # 后面那批 import 全会 ModuleNotFoundError，测的就不是精简而是路径。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.meta_path.insert(0, _Blocker())

    checks = [
        ("numpy", "import numpy"),
        ("torch", "import torch"),
        ("transformers", "import transformers"),
        ("funasr", "import funasr"),
        ("sounddevice", "import sounddevice"),
        ("librosa", "import librosa"),
        ("scipy", "import scipy"),
        # 项目自己的模块
        ("vcfg", "import vcfg"),
        ("voice_db", "import voice_db"),
        ("voice_recorder", "import voice_recorder"),
        ("voice_asr", "import voice_asr"),
        ("vevents", "import vevents"),
        ("vcollect", "import vcollect"),
        ("vdigest", "import vdigest"),
        ("vsearch", "import vsearch"),
        ("vsearch_ui", "import vsearch_ui"),
        ("vsession", "import voice_timeline"),
        ("vupdate", "import vupdate"),
        ("vlang", "import vlang"),
        # 真正会拉起模型的路径
        ("funasr.AutoModel", "from funasr import AutoModel"),
    ]

    bad = []
    for name, code in checks:
        try:
            exec(code, {})
            print(f"  OK   {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            bad.append(name)

    print()
    if bad:
        print(f"BLOCKED BY: {', '.join(bad)}")
        print("→ 这些包不能排除，或要用别的方式满足它")
        return 1
    print("ALL PASS — 黑名单里的包全都可以排除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
