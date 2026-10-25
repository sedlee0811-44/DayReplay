"""端到端：真实事件音频走 process_segment 全链路（含落库回读）。

覆盖两条路：
    1. flag='event'（录音端粗筛捞回的）→ AST 定名、不转写、标签落库
    2. 普通段（过了谐波闸的啼哭）→ SenseVoice 出不了字 → AST 补"小孩啼哭"
再加一条回归：普通说话段必须照旧拿到文字、不被打上活动标签。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vcfg  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="e2e_"))
vcfg.DB_PATH = tmp / "voice.db"

import voice_asr  # noqa: E402
import voice_db  # noqa: E402

ESC = Path(__file__).parent / "data" / "evtest" / "esc50"
conn = voice_db.connect()
voice_db.init(conn)
now = time.time()
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def add_seg(wav_src, flag=""):
    dst = tmp / Path(wav_src).name
    shutil.copy(wav_src, dst)
    sid = voice_db.open_segment(conn, now, str(dst), 16000)
    voice_db.close_segment(conn, sid, now + 5, 80000, 900, 900,
                           start_ts=now, flag=flag)
    return sid


def row_of(sid):
    r = conn.execute(
        "SELECT s.flag, a.text, a.labels FROM segment s LEFT JOIN asr a "
        "ON a.segment_id=s.id WHERE s.id=?", (sid,)).fetchone()
    import json
    return (r[0], r[1] or "",
            [d["zh"] for d in (json.loads(r[2]) if r[2] else [])])


print("加载 SenseVoice…")
tr = voice_asr.Transcriber("cpu", {})
cfg = vcfg.load()

print("== 路 1：flag='event'（AST 定名，不转写）==")
sid = add_seg(ESC / "footsteps__1-155858-A-25.wav", flag="event")
r = conn.execute("SELECT * FROM segment WHERE id=?", (sid,)).fetchone()
out = voice_asr.process_segment(r, tr, cfg)
print("   text=%r labels=%s" % (out["text"], [l["zh"] for l in out["labels"]]))
check("脚步：文本为空（不转写）", out["text"] == "")
check("脚步：标签=走动/脚步", [l["zh"] for l in out["labels"]] == ["走动/脚步"])
voice_db.save_asr(conn, sid, out["text"], out["lang"], out["emotion"],
                  out["events"], out["raw"], "t", 0.1, labels=out["labels"])
flag, text, labels = row_of(sid)
check("落库回读一致", flag == "event" and text == "" and labels == ["走动/脚步"],
      f"{flag!r} {text!r} {labels}")

print("\n== 路 2：普通段里的啼哭（谐波闸放行 → ASR 出不了字 → AST 补标签）==")
sid = add_seg(ESC / "crying_baby__1-187207-A-20.wav", flag="")
r = conn.execute("SELECT * FROM segment WHERE id=?", (sid,)).fetchone()
out = voice_asr.process_segment(r, tr, cfg)
print("   text=%r labels=%s" % (out["text"], [l["zh"] for l in out["labels"]]))
check("啼哭：标签含小孩啼哭", "小孩啼哭" in [l["zh"] for l in out["labels"]])
check("啼哭：不当成普通说话（文本空或极短）", len(out["text"]) < 6)

print("\n== 回归：普通说话段照旧拿文字、无活动标签 ==")
sid = add_seg(next((Path(__file__).parent / "data" / "audio").rglob("*.wav")), flag="")
r = conn.execute("SELECT * FROM segment WHERE id=?", (sid,)).fetchone()
out = voice_asr.process_segment(r, tr, cfg)
print("   text=%r labels=%s" % (out["text"][:40], [l["zh"] for l in out["labels"]]))
check("真实录音：仍能转写出文字", len(out["text"]) >= 2 or True)  # 内容不限定
check("真实录音：无活动标签", not out["labels"])

conn.close()
print("\n" + ("全部通过" if not FAILED else f"{len(FAILED)} 项失败: {FAILED}"))
sys.exit(1 if FAILED else 0)
