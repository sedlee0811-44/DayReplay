"""拿频谱特征给每一段录音定性：是人在说话，还是稳态噪声（风扇/空调/轰鸣）。

webrtcvad 只回答"这一帧响不响"，回答不了"这是不是人声"。
风扇这种宽带稳态噪声能量足够、谐波连续，VAD 会一直判成语音，
于是段永远不关闭，一路撑到 30 秒上限 —— 这正是后来那批空文本的来历。

这里用三个便宜但有效的判据把它和真人说话分开：
    频谱平坦度  spectral flatness  —— 稳态噪声接近白噪声，flatness 高
    帧能量动态   energy dynamics   —— 说话有起伏，风扇几乎恒定
    谐波性       harmonicity       —— 浊音有基频，噪声没有
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
import vcfg  # noqa: E402


def feats(x: np.ndarray, sr: int) -> dict:
    """在 25ms/10ms 滑窗上算三个判据。"""
    win = int(sr * 0.025)
    hop = int(sr * 0.010)
    if len(x) < win * 2:
        return {}
    nfr = max(1, (len(x) - win) // hop)
    w = np.hanning(win)
    flats, ens, harms = [], [], []
    for i in range(nfr):
        seg = x[i * hop:i * hop + win] * w
        if len(seg) < win:
            break
        X = np.abs(np.fft.rfft(seg)) ** 2 + 1e-12
        # 频谱平坦度 = 几何均值 / 算术均值，白噪声趋近 1
        logm = np.exp(np.mean(np.log(X)))
        flats.append(logm / (np.mean(X) + 1e-12))
        ens.append(np.sqrt(np.mean(seg ** 2)))
        # 谐波性：自相关峰在 2-12ms（基频 80-500Hz）之间的高度
        ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
        if ac[0] <= 1e-12:
            harms.append(0.0)
            continue
        lo, hi = int(sr * 0.002), min(int(sr * 0.012), len(ac) - 1)
        harms.append(float(ac[lo:hi].max() / ac[0]) if hi > lo else 0.0)
    if not ens:
        return {}
    e = np.array(ens)
    # 能量动态：dB 域的标准差。说话 typically > 5dB，稳态噪声 < 3dB
    edb = 20 * np.log10(e / (e.max() + 1e-12) + 1e-12)
    voiced = e > (e.max() * 0.05)          # 只统计有声部分
    dyn = float(np.std(edb[voiced])) if voiced.sum() > 3 else 0.0
    return {
        "flat": float(np.median(flats)),
        "dyn": dyn,
        "harm": float(np.percentile(harms, 75)),
        "nfr": nfr,
    }


def readmono(p):
    x, sr = sf.read(str(p), dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x, sr


def main():
    conn = sqlite3.connect(vcfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        select s.id, s.wav_path, s.duration, coalesce(a.text,'') t
        from segment s left join asr a on a.segment_id=s.id
        order by s.id desc limit 240
    """).fetchall()
    conn.close()

    buckets = {"短(<8s)": [], "中(8-15s)": [], "长(>15s)": []}
    rows_out = []
    for r in rows:
        p = Path(r["wav_path"])
        if not p.exists():
            continue
        try:
            x, sr = readmono(p)
            f = feats(x, sr)
        except Exception:
            continue
        if not f:
            continue
        d = r["duration"]
        k = "短(<8s)" if d < 8 else ("中(8-15s)" if d <= 15 else "长(>15s)")
        buckets[k].append((bool(r["t"]), f, r))
        rows_out.append((r["id"], d, bool(r["t"]), f))

    print(f"{'时长档':<12}{'段数':>5}{'有字':>5}{'有字率':>8}   "
          f"{'平坦度中位':>10}{'动态dB中位':>11}{'谐波性中位':>11}")
    for k, v in buckets.items():
        if not v:
            continue
        import statistics as st
        ok = sum(1 for a, _, _ in v if a)
        print(f"{k:<12}{len(v):>5}{ok:>5}{ok/len(v)*100:>7.0f}%   "
              f"{st.median([f['flat'] for _,f,_ in v]):>10.3f}"
              f"{st.median([f['dyn'] for _,f,_ in v]):>11.2f}"
              f"{st.median([f['harm'] for _,f,_ in v]):>11.3f}")

    # 分"有字 vs 无字"看同一组统计
    import statistics as st
    ok = [f for _, _, f in [(0, 0, f) for _, d, a, f in rows_out if a]]
    no = [f for _, d, a, f in rows_out if not a]
    print(f"\n{'':<12}{'段数':>5}   平坦度   动态dB   谐波性")
    for name, g in (("有识别出字", ok), ("空/无效", no)):
        if not g:
            continue
        print(f"{name:<12}{len(g):>5}   "
              f"{st.median([x['flat'] for x in g]):>7.3f} "
              f"{st.median([x['dyn'] for x in g]):>7.2f} "
              f"{st.median([x['harm'] for x in g]):>7.3f}")

    # 按谐波性/平坦度提出候选阈值
    print("\n=== 尝试用特征筛掉无效段（目标：拿掉空段，保住有字段）===")
    for thr in (0.35, 0.40, 0.45, 0.50, 0.55):
        keep_ok = sum(1 for _, d, a, f in rows_out if a and f["harm"] >= thr)
        drop_no = sum(1 for _, d, a, f in rows_out if (not a) and f["harm"] < thr)
        tot_ok = sum(1 for _, d, a, f in rows_out if a)
        tot_no = sum(1 for _, d, a, f in rows_out if not a)
        print(f"  谐波性 >= {thr:.2f} → 留住有字段 {keep_ok}/{tot_ok} "
              f"({keep_ok/max(1,tot_ok)*100:.0f}%)，清掉空段 "
              f"{drop_no}/{tot_no} ({drop_no/max(1,tot_no)*100:.0f}%)")


if __name__ == "__main__":
    main()
