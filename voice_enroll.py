"""声纹登记与重判。

两种登记方式：
    python voice_enroll.py --name 我 --seconds 30
        现场说 30 秒，自动取这段时间的语音段建档案（推荐）

    python voice_enroll.py --name 我 --from-segments 12,15,18
        指定已有段（回听某天录音后挑干净的）

其他：
    python voice_enroll.py --list                  # 看现有档案
    python voice_enroll.py --relabel               # 用当前档案重判全部历史
    python voice_enroll.py --delete 我
    python voice_enroll.py --explain --from-segments ...   # 只看相似度矩阵，不建档
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np

import vcfg
import voice_db
import voice_speaker as vs


def _fmt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def collect_by_time(conn, t0: float, t1: float, cfg: dict) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.start_ts, s.duration, s.wav_path, a.text "
        "FROM segment s LEFT JOIN asr a ON a.segment_id = s.id "
        "WHERE s.state='closed' AND s.start_ts >= ? AND s.start_ts <= ? "
        "AND s.duration >= ? ORDER BY s.start_ts",
        (t0, t1, cfg["spk_min_duration"])).fetchall()
    return [dict(r) for r in rows]


def collect_by_ids(conn, ids: list[int]) -> list[dict]:
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT s.id, s.start_ts, s.duration, s.wav_path, a.text "
        f"FROM segment s LEFT JOIN asr a ON a.segment_id = s.id "
        f"WHERE s.id IN ({q}) ORDER BY s.id", ids).fetchall()
    return [dict(r) for r in rows]


def do_enroll(tr_rows: list[dict], name: str, enc: vs.SpeakerEncoder,
              cfg: dict, explain_only: bool) -> int:
    if not tr_rows:
        print("\n这段时间没有抓到语音段。请靠近麦克风、正常音量再说一次。")
        return 1
    total = sum(float(r["duration"] or 0) for r in tr_rows)
    if total < 6.0:
        print(f"\n总共只录到 {total:.1f} 秒语音，太少。请多说几句（至少 10 秒）。")
        return 1

    print(f"\n共取到 {len(tr_rows)} 段，开始提取声纹…")
    embs, used = [], []
    for r in tr_rows:
        wav = Path(r["wav_path"])
        if not wav.exists():
            print(f"  #{r['id']} 音频缺失，跳过")
            continue
        # 长段自动切块，避免"连着说不换气"时凑不够样本
        parts = vs.embed_samples(
            wav, enc,
            chunk_s=cfg.get("spk_chunk_s", 3.0),
            min_voiced=cfg.get("spk_chunk_min_voiced", 0.45))
        if not parts:
            print(f"  #{r['id']} 提取失败，跳过")
            continue
        for e in parts:
            embs.append(e)
            used.append(r)
        if len(parts) > 1:
            print(f"  #{r['id']} {r['duration']:.1f}s → 切出 {len(parts)} 个样本")

    if len(embs) < 3:
        print(f"\n有效样本只有 {len(embs)} 个，不足以建档。请多说几句再试。")
        return 1

    # 两两相似度矩阵，用来发现"混进了别人的话"
    M = np.stack(embs)
    S = M @ M.T
    iu = np.triu_indices(len(embs), k=1)
    pair = S[iu] if len(iu[0]) else np.array([1.0])
    print(f"\n两两相似度：最小 {pair.min():.3f}  中位 {np.median(pair):.3f}  最大 {pair.max():.3f}")

    centroid, n_keep, consistency = vs.build_centroid(embs)

    print(f"\n{'':4}{'时间':<10}{'时长':>6}  {'与本档案相似度':>12}  文本")
    for r, e in zip(used, embs):
        sim = vs.cosine(e, centroid)
        mark = "" if sim >= cfg["spk_same"] else "  ← 偏低"
        print(f"  #{r['id']:<3}{_fmt(r['start_ts']):<10}{r['duration']:5.1f}s"
              f"{sim:12.3f}  {(r['text'] or '')[:34]}{mark}")

    print(f"\n质心一致性 = {consistency:.3f}（保留 {n_keep}/{len(embs)} 段）")
    if consistency < 0.55:
        print("⚠ 一致性偏低，可能混入了不同人的话。建议换更干净的几段重试。")

    if explain_only:
        print("\n（--explain 模式，未建档）")
        return 0

    vs.save_profile(name, centroid, {
        "model": "campplus_sv_zh-cn_16k",
        "dim": int(centroid.shape[0]),
        "n": n_keep,
        "consistency": round(consistency, 4),
        "created_at": time.time(),
        "sample_segments": [r["id"] for r in used],
    })
    print(f"\n✓ 已登记声纹档案「{name}」")
    return 0


def do_live(conn, name: str, seconds: float, enc, cfg, explain_only: bool) -> int:
    print(f"\n请用平时的语速和音量，自然地说 {seconds:.0f} 秒话。")
    print("  （随便说什么都行：念一段施工方案、说说今天干了什么、吐槽天气）")
    print("  说满之后会自动截取，不用按键。\n")
    t0 = time.time()
    bar = 30
    while True:
        left = seconds - (time.time() - t0)
        if left <= 0:
            break
        done = int(bar * (1 - left / seconds))
        sys.stdout.write(f"\r  [{'█'*done}{'·'*(bar-done)}] {left:4.1f}s ")
        sys.stdout.flush()
        time.sleep(0.2)
    print("\r" + " " * 70 + "\r", end="")
    t1 = time.time()

    rows = collect_by_time(conn, t0 - 1, t1 + 1, cfg)
    print(f"这段时间切出 {len(rows)} 段语音：")
    for r in rows:
        print(f"  #{r['id']} {_fmt(r['start_ts'])} {r['duration']:5.1f}s  "
              f"{(r['text'] or '（待转写）')[:40]}")
    return do_enroll(rows, name, enc, cfg, explain_only)


def do_list(conn):
    meta = vs.profile_meta().get("profiles", {})
    if not meta:
        print("还没有任何声纹档案。用 --name 我 --seconds 30 登记。")
        return
    print(f"共 {len(meta)} 个档案：")
    for name, info in meta.items():
        n = conn.execute(
            "SELECT COUNT(*) FROM speaker WHERE label=?", (name,)).fetchone()[0]
        print(f"  「{name}」 维度={info.get('dim')}  样本={info.get('n')}  "
              f"一致性={info.get('consistency')}  已判给它的段数={n}")
        print(f"      登记于 {dt.datetime.fromtimestamp(info.get('created_at', 0)):%Y-%m-%d %H:%M}")


def do_backfill(conn, enc, cfg, relabel_after: bool = True) -> None:
    """给还没有声纹记录的段补算嵌入。

    嵌入一旦落库，以后重新登记/改阈值只要 relabel，不必重跑模型。
    """
    rows = list(conn.execute(
        "SELECT s.id, s.wav_path, s.duration FROM segment s "
        "LEFT JOIN speaker sp ON sp.segment_id = s.id "
        "WHERE s.state='closed' AND sp.segment_id IS NULL ORDER BY s.id"))
    if not rows:
        print("没有需要补算的段。")
        return
    print(f"补算 {len(rows)} 段…")
    profiles = vs.current_profiles()
    done = short = fail = 0
    for r in rows:
        if (r["duration"] or 0) < cfg["spk_min_duration"]:
            vs.save(conn, r["id"], vs.LABEL_UNKNOWN, 0.0, None)
            short += 1
            continue
        wav = Path(r["wav_path"])
        if not wav.exists():
            vs.save(conn, r["id"], vs.LABEL_UNKNOWN, 0.0, None)
            fail += 1
            continue
        e = enc.embed(wav)
        if e is None:
            vs.save(conn, r["id"], vs.LABEL_UNKNOWN, 0.0, None)
            fail += 1
            continue
        label, score = vs.classify(e, profiles, cfg)
        vs.save(conn, r["id"], label, score, e)
        done += 1
    print(f"  成功 {done} / 时长不足 {short} / 失败 {fail}")
    if relabel_after and profiles:
        counts = vs.relabel_all(conn, cfg)
        print("  重判：", "  ".join(f"{k}={v}" for k, v in counts.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="档案名，如 我 / 妻子")
    ap.add_argument("--seconds", type=float, default=30.0, help="现场说多久")
    ap.add_argument("--from-segments", help="逗号分隔的段 id")
    ap.add_argument("--explain", action="store_true", help="只看分析，不建档")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--relabel", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="给历史段补算声纹嵌入")
    ap.add_argument("--delete")
    ap.add_argument("--device", default="cpu")
    a = ap.parse_args()

    vcfg.ensure_dirs()
    cfg = vcfg.load()
    conn = voice_db.connect()
    voice_db.init(conn)
    vs.init_table(conn)

    if a.list:
        do_list(conn)
        return
    if a.delete:
        print("已删除" if vs.delete_profile(a.delete) else "没有这个档案")
        return
    if a.relabel:
        counts = vs.relabel_all(conn, cfg)
        print("重判完成：", "  ".join(f"{k}={v}" for k, v in counts.items()) or "（无数据）")
        return
    if a.backfill:
        do_backfill(conn, vs.SpeakerEncoder(a.device), cfg)
        return

    if not a.name:
        ap.error("需要 --name（除非用 --list / --relabel / --backfill / --delete）")

    if a.from_segments:
        ids = [int(x) for x in a.from_segments.replace(" ", "").split(",") if x]
        rows = collect_by_ids(conn, ids)
        rc = do_enroll(rows, a.name, vs.SpeakerEncoder(a.device), cfg, a.explain)
    else:
        rc = do_live(conn, a.name, a.seconds, vs.SpeakerEncoder(a.device), cfg, a.explain)

    if rc == 0 and not a.explain:
        counts = vs.relabel_all(conn, cfg)
        print("历史重判：", "  ".join(f"{k}={v}" for k, v in counts.items()) or "（无数据）")
    sys.exit(rc)


if __name__ == "__main__":
    main()
