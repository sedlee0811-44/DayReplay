"""离线对照实验：长段到底是怎么被识别崩的，以及怎么救。

取一批真实录音，分别用不同的前端处理跑 SenseVoice，比较：
    原文 vs 归一化(峰值) vs 归一化(RMS) vs 切短后的子段
目的只有一个 —— 用数据回答"该改哪一环"，不靠猜。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
import vcfg  # noqa: E402

os.environ.setdefault("MODELSCOPE_CACHE", str(vcfg.ROOT / "models"))

OUT = Path("_tmp_asr_test")
OUT.mkdir(exist_ok=True)


def load_wav(p):
    x, sr = sf.read(str(p), dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x, sr


def save_wav(x, sr, name):
    p = OUT / name
    sf.write(str(p), np.clip(x, -1, 1), sr, subtype="PCM_16")
    return p


def norm_rms(x, target_dbfs=-20.0):
    """整体增益到目标电平。摄像头麦录远场时电平偏低。"""
    r = float(np.sqrt(np.mean(x ** 2)) + 1e-9)
    g = 10 ** (target_dbfs / 20) / r
    return np.clip(x * g, -1, 1), g


def highpass(x, sr, cutoff=60.0):
    """去掉低频轰鸣和直流漂移 —— 摄像头麦常见毛病。"""
    from scipy.signal import butter, sosfilt
    sos = butter(4, cutoff, "highpass", fs=sr, output="sos")
    return sosfilt(sos, x).astype(np.float32)


def trim_silence(x, sr, top_db=30, frame=0.02, keep=0.08):
    """掐掉首尾的静音尾巴。长段前后往往有几百毫秒纯噪声。"""
    n = int(sr * frame)
    if len(x) < n * 2:
        return x
    m = len(x) // n
    e = np.array([np.sqrt(np.mean(x[i * n:(i + 1) * n] ** 2) + 1e-9)
                  for i in range(m)])
    thr = e.max() * 10 ** (-top_db / 20)
    idx = np.where(e >= thr)[0]
    if not len(idx):
        return x
    a = max(0, int((idx[0] - keep / frame) * n))
    b = min(len(x), int((idx[-1] + 1 + keep / frame) * n))
    return x[a:b]


TAG_RE = re.compile(r"<\|([^|]+)\|>")


def main():
    conn = sqlite3.connect(vcfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    # 各取若干：长段（崩）与短段（准）
    long_rows = conn.execute("""
        select s.id, s.wav_path, s.duration, coalesce(a.text,'') t
        from segment s left join asr a on a.segment_id=s.id
        where s.duration > 15 order by s.id desc limit 3
    """).fetchall()
    short_rows = conn.execute("""
        select s.id, s.wav_path, s.duration, coalesce(a.text,'') t
        from segment s left join asr a on a.segment_id=s.id
        where s.duration between 1.5 and 8 and trim(coalesce(a.text,''))<>''
        order by s.id desc limit 3
    """).fetchall()
    conn.close()

    from funasr import AutoModel
    t0 = time.time()
    print("加载模型…", flush=True)
    model = AutoModel(model=vcfg.model_dir(vcfg.load().get("asr_model_id")),
                      device="cpu", disable_update=True, disable_pbar=True,
                      disable_log=True)
    print(f"就绪 {time.time()-t0:.1f}s\n", flush=True)

    def asr(p, **kw):
        r = model.generate(input=str(p), cache={}, language="zh",
                           use_itn=True, **kw)
        return (r[0].get("text", "") if r else "")

    def show(label, raw, extra=""):
        try:
            from funasr.utils.postprocess_utils import (
                rich_transcription_postprocess)
            clean = rich_transcription_postprocess(raw)
        except Exception:
            clean = TAG_RE.sub("", raw).strip()
        print(f"    {label:<22} {clean[:78]}")
        return clean

    for tag, rows in (("长段 >15s", long_rows), ("短段 1.5-8s", short_rows)):
        print(f"===== {tag} =====")
        for r in rows:
            p = Path(r["wav_path"])
            if not p.exists():
                print("  缺失", p)
                continue
            x, sr = load_wav(p)
            print(f"\n  #{r['id']}  {r['duration']:.1f}s  sr={sr}  库里存={r['t'][:40]!r}")
            show("原始", asr(p))

            # 1) 掐首尾静音
            t = trim_silence(x, sr)
            if len(t) < len(x):
                show("掐首尾静音", asr(save_wav(t, sr, "a_trim.wav")))

            # 2) 掐 + 电平归一化
            n1, g = norm_rms(t, -20.0)
            show(f"掐+归一化(g={g:.1f})", asr(save_wav(n1, sr, "b_norm.wav")))

            # 3) 掐 + 高通 + 归一化
            n2, _ = norm_rms(highpass(t, sr), -20.0)
            show("掐+高通+归一化", asr(save_wav(n2, sr, "c_hp.wav")))

            # 4) 切成 <=10s 的子段分别识别
            win = int(sr * 10)
            pieces = []
            for i in range(0, len(t), win):
                pieces.append(t[i:i + win])
            if len(pieces) > 1:
                outs = []
                for k, pc in enumerate(pieces):
                    if len(pc) < sr * 0.3:
                        continue
                    o = asr(save_wav(pc, sr, f"d_{k}.wav"))
                    outs.append(TAG_RE.sub("", o).strip())
                joined = " ".join(o for o in outs if o)
                print(f"    {'切成<=10s子段':<22} {joined[:78]}")
        print()


if __name__ == "__main__":
    main()
