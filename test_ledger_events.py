"""活动声进总账的集成测试：假库 → voice_lines → 兜底文案/节点简报。

不跑模型，只验数据从 asr.labels 一路流到渲染层没断：
    1. 活动声条目要出现（以前空文本被 continue 掉，一个都不显示）
    2. 同类活动间隔够近要合并（走动几步并一条，n 计数）
    3. 纯噪声段（无文字无标签）不许出现
    4. 老行的英文事件标签要映射成中文（Cry→啼哭）
    5. 兜底文案和节点简报要带上活动声
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vcfg  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="evdb_"))
vcfg.DB_PATH = tmp / "voice.db"

import voice_db  # noqa: E402
import vcollect  # noqa: E402
import vdigest  # noqa: E402

conn = voice_db.connect()
voice_db.init(conn)
now = time.time()
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def seg(t, dur, flag=""):
    sid = voice_db.open_segment(conn, t, str(tmp / f"s{t}.wav"), 16000)
    voice_db.close_segment(conn, sid, t + dur, int(16000 * dur), 800, 800,
                           start_ts=t, flag=flag)
    return sid


s1 = seg(now - 600, 5)                      # 普通说话
voice_db.save_asr(conn, s1, "今天把总账修好了", "zh", "NEUTRAL", [],
                  "<|zh|><|NEUTRAL|><|Speech|>今天把总账修好了", "t", 0.1,
                  labels=[])
s2 = seg(now - 500, 6, flag="event")        # 小孩啼哭
voice_db.save_asr(conn, s2, "", "", "", [], "", "ast", 1.0,
                  labels=[{"zh": "小孩啼哭", "score": 0.44, "category": "baby"}])
s3 = seg(now - 400, 4, flag="event")        # 走动（两条，间隔 30s 应合并）
voice_db.save_asr(conn, s3, "", "", "", [], "", "ast", 1.0,
                  labels=[{"zh": "走动/脚步", "score": 0.48, "category": "activity"}])
s4 = seg(now - 370, 4, flag="event")
voice_db.save_asr(conn, s4, "", "", "", [], "", "ast", 1.0,
                  labels=[{"zh": "走动/脚步", "score": 0.50, "category": "activity"}])
s5 = seg(now - 300, 5, flag="event")        # AST 判噪声：无标签，不许出现
voice_db.save_asr(conn, s5, "", "", "", [], "", "ast", 1.0, labels=[])
s6 = seg(now - 200, 5)                      # 老行：只有英文事件，无 labels 列内容
conn.execute("UPDATE asr SET labels=NULL WHERE segment_id=?", (s6,))
voice_db.save_asr(conn, s6, "", "", "", ["Cry"], "<|Cry|>", "SenseVoice", 0.5)
conn.commit()

print("== voice_lines ==")
lines = vcollect.voice_lines(now - 1000, now + 100)
kinds = [l.get("kind") or "talk" for l in lines]
print("  ", [(k, l.get("labels") or (l.get("text") or "")[:12], l["n"]) for k, l in zip(kinds, lines)])
check("活动声条目出现", kinds.count("event") >= 2, str(kinds))
check("说话条目还在", "talk" in kinds)
check("走动两条合并成一条 n=2",
      any(l.get("kind") == "event" and l.get("n") == 2
          and l["labels"] == ["走动/脚步"] for l in lines))
check("啼哭条目带标签", any(l.get("labels") == ["小孩啼哭"] for l in lines))
check("噪声段不出现", not any(
    l.get("kind") == "event" and not l.get("labels") for l in lines))
check("老行 Cry 映射成啼哭", any(
    l.get("labels") == ["啼哭"] for l in lines), str([l.get("labels") for l in lines]))
check("条目按时间排序", [l["start"] for l in lines] == sorted(l["start"] for l in lines))

print("\n== 渲染层 ==")
node = {"voice": lines, "docs": [], "files": [], "idle": [], "app": "code.exe",
        "kind": "work", "start": now - 1000, "end": now, "shots": [],
        "voice_links": [], "shot_links": [], "doc_links": [],
        "minutes": 15.0, "art_paths": [], "apps": [], "titles": [],
        "ocr": ""}
fb = vdigest._fallback_node(node)
print("   兜底 voice 字段:", fb["voice"])
check("兜底文案含活动声", "活动声" in fb["voice"] and "啼哭" in fb["voice"])
check("兜底文案仍报说话", "录到" in fb["voice"] and "段" in fb["voice"])
brief = vdigest._node_brief(node)
print("   节点简报:", brief.replace("\n", " ⏎ "))
check("简报含活动声标签", "活动声" in brief and "啼哭" in brief)
check("简报含走动", "走动" in brief)
seg_txt = vdigest.auto_paragraph({"screens": [], "voice": lines, "t0": now - 1000,
                                  "t1": now, "artifacts": []})
print("   会话摘要:", seg_txt)
check("摘要分别计数", "1 段说话" in seg_txt and "3 段活动声" in seg_txt, seg_txt)

conn.close()
print("\n" + ("全部通过" if not FAILED else f"{len(FAILED)} 项失败: {FAILED}"))
sys.exit(1 if FAILED else 0)
