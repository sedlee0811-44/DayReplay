"""给 Segmenter 的两处新逻辑做单元验证：能量谷落刀 + 谐波裁决。

这两处都是"写错了不会崩、只会悄悄变坏"的类型 —— 切错位置丢半个字，
阈值不对要么漏录要么刷噪声段。所以必须有值焊缝。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import vcfg  # noqa: E402
import vquality  # noqa: E402


class FakeVAD:
    """故意把每一帧都判成语音 —— 模拟最糟的情况：风扇从不停。"""

    kind = "fake"

    def is_speech(self, frame) -> bool:
        return True


class FakeDB:
    """替掉 voice_db，只记录被调用的动作。"""

    def __init__(self):
        self.opened, self.closed, self.dropped = [], [], []

    def open_segment(self, conn, start_ts, wav_path, sr):
        self.opened.append((start_ts, wav_path, sr))
        return len(self.opened)

    def close_segment(self, conn, seg_id, end_ts, n, rms, peak, start_ts=None,
                      flag=None):
        # flag 是 2026-10-25 活动声识别加的（'event' 或 None），桩要跟上。
        # 'event' = 谐波闸门没放行、但 DSP 粗筛认为"有活动特征"的段 ——
        # 它进库是为了后面给 AST 定名用，不是当语音。
        self.closed.append((seg_id, n, rms, peak, flag))

    def drop_segment(self, conn, seg_id):
        self.dropped.append(seg_id)


def build_segmenter(tmp: Path, monkeypatch_db=False):
    import voice_recorder as vr

    vr.AUDIO_DIR = tmp          # 重定向落盘位置
    cfg = dict(vcfg.load())
    cfg["max_segment_s"] = 1.2          # 短一点，测试跑得快
    cfg["min_segment_s"] = 0.2
    cfg["min_voiced_ratio"] = 0.0       # 关掉旧门，只考新逻辑
    cfg["loud_voiced_ratio"] = 0.0
    cfg["loud_rms"] = 0.0
    cfg["harm_min_s"] = 0.3
    cfg["split_search_ms"] = 400
    seg = vr.Segmenter(cfg, None, FakeVAD())
    return seg, cfg


def tone(sr, secs, f0, amp=0.4):
    """一段有谐波的声音 —— 模拟人说话。"""
    t = np.arange(int(sr * secs)) / sr
    x = sum(amp / (k + 1) * np.sin(2 * np.pi * f0 * (k + 1) * t) for k in range(6))
    return (x * 32767 / max(1e-9, np.abs(x).max())).astype(np.int16)


def noise(sr, secs, amp=0.3):
    """宽带白噪声 —— 模拟风扇/空调，没有基频结构。"""
    rng = np.random.default_rng(7)
    x = rng.standard_normal(int(sr * secs))
    return (x * amp * 32767 / max(1e-9, np.abs(x).max())).astype(np.int16)


def main():
    ok = True

    def chk(desc, cond, detail=""):
        nonlocal ok
        print(f"  {'OK  ' if cond else 'FAIL'} {desc}" + (f"  {detail}" if detail else ""))
        ok = ok and cond

    sr = 16000
    print("=== 谐波性判据本身 ===")
    h_tone = vquality.harmonicity(tone(sr, 2.0, 160).astype(np.float32) / 32768, sr)
    h_noise = vquality.harmonicity(noise(sr, 2.0).astype(np.float32) / 32768, sr)
    print(f"    合成浊音 harm={h_tone:.3f}   白噪声 harm={h_noise:.3f}")
    chk("浊音谐波性显著高于噪声", h_tone > h_noise + 0.15)
    chk("阈值 0.45 能把两者分开", h_tone >= 0.45 > h_noise)

    print("\n=== 能量谷落刀 ===")
    # 造一段："响 0.6s + 静 0.2s + 响 0.6s"，让 max_segment 落在中间的静音处
    x = np.concatenate([tone(sr, 0.6, 160), np.zeros(int(sr * 0.2), np.int16),
                        tone(sr, 0.6, 160)])
    tmp = Path(tempfile.mkdtemp())
    try:
        seg, cfg = build_segmenter(tmp)
        frame_n = int(sr * cfg["vad_frame_ms"] / 1000)
        seg.cfg["max_segment_s"] = 1.0
        seg.max_frames = int(1.0 * 1000 / cfg["vad_frame_ms"])
        # 直接构造 frames
        seg.frames = [x[i:i + frame_n] for i in range(0, len(x) - frame_n, frame_n)]
        seg.frames = seg.frames[:seg.max_frames]   # 就像 feed() 刚到上限那一刻
        n_before = len(seg.frames)
        seg.pcm = tmp / "t.pcm"
        seg.fh = open(seg.pcm, "wb", buffering=0)
        seg.fh.write(np.concatenate(seg.frames).astype(np.int16).tobytes())
        seg.sample_count = sum(len(f) for f in seg.frames)
        seg.seg_id = 1

        cut = seg._cut_at_valley()
        # 应该落在后半段的开头（那儿是静音）
        kept_len = sum(len(f) for f in seg.frames)
        print(f"    原 {n_before} 帧，保留 {len(seg.frames)} 帧，砍下 {len(cut)} 帧")
        chk("确实发生了切割", len(cut) > 0)
        chk("保留+砍下 = 原长", len(seg.frames) + len(cut) == n_before)

        # 落刀点应当接近中间那段静音（第 20~32 帧）
        cut_at = len(seg.frames)
        lo, hi = 20, 32
        chk("刀口落在预先埋的静音区里", lo <= cut_at <= hi, f"实际 {cut_at}")

        # 磁盘上的字节数必须和内存里的帧数一致，否则 wav 比记录长
        seg.fh.close()
        on_disk = seg.pcm.stat().st_size
        chk("PCM 已同步截断", on_disk == kept_len * 2, f"{on_disk} vs {kept_len*2}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 端到端：喂一段持续噪声，应当被判为'不像人声'整段丢弃 ===")
    tmp = Path(tempfile.mkdtemp())
    try:
        seg, cfg = build_segmenter(tmp)
        seg.cfg["min_harmonicity"] = 0.45
        fake_db = FakeDB()
        import voice_recorder as vr
        vr.voice_db = fake_db
        x = noise(sr, 3.0)
        # 直接走内部 _begin / _write / _end，避开声卡
        seg.sr = sr
        seg.active = False
        frame_n = int(sr * cfg["vad_frame_ms"] / 1000)
        base = __import__('time').time()
        for i in range(0, len(x) - frame_n, frame_n):
            seg.feed(x[i:i + frame_n], base + i / sr)
            if seg.sample_count >= sr * cfg["max_segment_s"]:
                break
        seg.flush()
        print(f"    opened={len(fake_db.opened)} closed={len(fake_db.closed)} "
              f"dropped={len(fake_db.dropped)}")
        # 2026-10-25 起，谐波闸门没放行的段不再直接丢，先过 DSP 活动声粗筛：
        # 白噪声也有 onsets，会被粗筛当"活动声"留下（flag='event'）交给 AST
        # 定名 —— 这是设计行为。所以"一个都没入库"这个旧断言要改成：
        # **没有任何一段被当语音收下**（close 时 flag 必须非空）。
        as_speech = [c for c in fake_db.closed if not c[4]]
        chk("噪声段没有被当语音收下", not as_speech,
            f"被当语音收下 {len(as_speech)} 段")
        chk("丢弃或转活动声至少占一样",
            len(fake_db.dropped) > 0 or len(fake_db.closed) > 0)

        print("\n=== 端到端：喂一段有谐波的声音，应当被留下 ===")
        seg2, cfg2 = build_segmenter(tmp)
        seg2.cfg["min_harmonicity"] = 0.45
        fake_db2 = FakeDB()
        vr.voice_db = fake_db2
        y = tone(sr, 3.0, 170)
        base = __import__('time').time()
        for i in range(0, len(y) - frame_n, frame_n):
            seg2.feed(y[i:i + frame_n], base + i / sr)
            if seg2.sample_count >= sr * cfg2["max_segment_s"]:
                break
        seg2.flush()
        print(f"    opened={len(fake_db2.opened)} closed={len(fake_db2.closed)} "
              f"dropped={len(fake_db2.dropped)}")
        chk("有谐波的声音被保留入库", len(fake_db2.closed) > 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'✓ 全部通过' if ok else '✗ 有未通过项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
