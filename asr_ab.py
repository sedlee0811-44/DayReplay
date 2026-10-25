"""A/B：同一个模型，language=auto 与 language=zh 的差别。

为什么要这个脚本：
    用户全中文环境，最烦的是"明明在说中文，却被标成韩语/日语"。
    SenseVoice 的 language 参数可以在解码前就把语言钉死，理论上直接
    消灭这个问题。但"理论上"不算数 —— 拿今天真实的录音跑一遍才算。

用法：
    python asr_ab.py            # 默认取今天最长的 10 段
    python asr_ab.py 20         # 取 20 段
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("MODELSCOPE_CACHE", str(Path(__file__).resolve().parent / "models"))

import vcfg  # noqa: E402

TAG_RE = re.compile(r"<\|([^|]+)\|>")
LANG_TAGS = {"zh", "en", "yue", "ja", "ko", "nospeech"}
CJK = re.compile(r"[\u4e00-\u9fff]")


def clean(raw: str) -> str:
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        return rich_transcription_postprocess(raw)
    except Exception:
        return TAG_RE.sub("", raw).strip()


def tags(raw: str) -> tuple[str, str]:
    ts = TAG_RE.findall(raw or "")
    lang = next((t for t in ts if t in LANG_TAGS and t != "nospeech"), "")
    emo = next((t for t in ts if t in {"HAPPY", "SAD", "ANGRY", "NEUTRAL",
                                       "FEARFUL", "DISGUSTED", "SURPRISED"}), "")
    return lang, emo


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    base = vcfg.AUDIO_DIR / time.strftime("%Y-%m-%d")
    wavs = sorted(base.glob("*.wav"), key=lambda p: -p.stat().st_size)[:n]
    if not wavs:
        print("今天没有 wav")
        return 1

    from funasr import AutoModel
    t0 = time.time()
    model = AutoModel(model=vcfg.model_dir("iic/SenseVoiceSmall"), device="cpu",
                      disable_update=True, disable_pbar=True, disable_log=True)
    print(f"模型就绪 {time.time() - t0:.0f}s，{len(wavs)} 段\n")

    bad_auto = bad_zh = 0
    for w in wavs:
        r = {}
        for lang in ("auto", "zh"):
            try:
                out = model.generate(input=str(w), cache={}, language=lang,
                                     use_itn=True, batch_size_s=60)
                raw = out[0].get("text", "") if out else ""
            except Exception as e:
                raw = f"<ERR {e}>"
            lg, emo = tags(raw)
            r[lang] = (lg, emo, clean(raw))

        (la, ea, ta), (lz, ez, tz) = r["auto"], r["zh"]
        bad_auto += 0 if la == "zh" else 1
        bad_zh += 0 if lz == "zh" else 1
        mark = "  " if lz == "zh" else "!!"
        print(f"{mark}{w.name}  {w.stat().st_size / 32000:.1f}s")
        print(f"    auto [{la or '?':<3}] {ta}")
        print(f"    zh   [{lz or '?':<3}] {tz}")

    total = len(wavs)
    print(f"\nauto 判成非中文：{bad_auto}/{total}　　zh 判成非中文：{bad_zh}/{total}")
    print("注：中文文本里夹了英文字母不算错，这里看的是语言标签和错字。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
