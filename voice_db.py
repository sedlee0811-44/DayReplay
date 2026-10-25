"""voice.db —— 语音段与转写结果。

⚠ 时间口径（2026-10-24 实测钉死）：
- 本库 start_ts / end_ts 一律 **真实 UTC Unix 秒，float**（time.time()），毫秒精度
- Windrecorder 的 video_text.videofile_time **不是** UTC Unix 秒！
  它是 timegm(localtime())，即"本地墙钟当成 UTC 编码"，比真实 UTC 多 28800 秒。
  对齐时必须走 voice_timeline.to_wind() 换算，否则整体错 8 小时。
  （本文件早前版本误记为"同源"，已更正。）

可靠性：
- WAL + 短事务；录到一半崩溃时 segment.state 仍是 'open'，
  下次启动由 voice_recorder.recover() 补收尾
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

import vcfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS segment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    REAL    NOT NULL,
    end_ts      REAL    NOT NULL,
    duration    REAL    NOT NULL,
    sample_rate INTEGER NOT NULL,
    samples     INTEGER NOT NULL,
    rms         REAL,
    peak        INTEGER,
    wav_path    TEXT    NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'open',
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_segment_start ON segment(start_ts);

CREATE TABLE IF NOT EXISTS asr (
    segment_id  INTEGER PRIMARY KEY,
    text        TEXT,
    lang        TEXT,
    emotion     TEXT,
    events      TEXT,
    raw         TEXT,
    model       TEXT,
    rtf         REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_asr_text ON asr(text);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS speaker (
    segment_id INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    score      REAL,
    emb        BLOB,
    dim        INTEGER,
    created_at REAL NOT NULL
);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else vcfg.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列。SCHEMA 里的 CREATE TABLE IF NOT EXISTS 对已存在的表无效，
    新加的列必须走 ALTER。两条都不能重复加（SQLite 会报 duplicate column）。
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(segment)")}
    if "flag" not in have:
        # flag='event'：谐波闸门没放行、但 DSP 粗筛认出"可能是人类活动声"
        # 的段。转写进程看到它就跳过 SenseVoice，直接上 AST 精筛。
        conn.execute("ALTER TABLE segment ADD COLUMN flag TEXT NOT NULL DEFAULT ''")
    have = {r[1] for r in conn.execute("PRAGMA table_info(asr)")}
    if "labels" not in have:
        # labels：合并 SenseVoice AED + AST 后的中文活动标签，
        # JSON list[{"zh","score","category","src"}]。空文本但有标签的段
        # 靠它进总账（voice_lines 以前见到空文本就跳过，活动声全丢）。
        conn.execute("ALTER TABLE asr ADD COLUMN labels TEXT")


# ---------------------------------------------------------------- segment

def open_segment(
    conn: sqlite3.Connection, start_ts: float, wav_path: str, sample_rate: int
) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO segment(start_ts,end_ts,duration,sample_rate,samples,"
        "wav_path,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (start_ts, start_ts, 0.0, sample_rate, 0, wav_path, "open", now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def touch_segment(
    conn: sqlite3.Connection, seg_id: int, end_ts: float, samples: int,
    rms: float, peak: int, start_ts: Optional[float] = None,
) -> None:
    """录音进行中的心跳落盘，保证崩溃后能知道录到哪了。"""
    st = start_ts if start_ts is not None else _start_of(conn, seg_id)
    conn.execute(
        "UPDATE segment SET start_ts=?, end_ts=?, duration=?, samples=?, rms=?, "
        "peak=?, updated_at=? WHERE id=?",
        (st, end_ts, end_ts - st, samples, rms, peak, time.time(), seg_id),
    )
    conn.commit()


def _start_of(conn: sqlite3.Connection, seg_id: int) -> float:
    row = conn.execute("SELECT start_ts FROM segment WHERE id=?", (seg_id,)).fetchone()
    return float(row["start_ts"]) if row else 0.0


def close_segment(
    conn: sqlite3.Connection, seg_id: int, end_ts: float, samples: int,
    rms: float, peak: int, start_ts: Optional[float] = None,
    flag: str = "",
) -> None:
    st = start_ts if start_ts is not None else _start_of(conn, seg_id)
    conn.execute(
        "UPDATE segment SET start_ts=?, end_ts=?, duration=?, samples=?, rms=?, "
        "peak=?, state='closed', flag=?, updated_at=? WHERE id=?",
        (st, end_ts, end_ts - st, samples, rms, peak, flag, time.time(), seg_id),
    )
    conn.commit()


def drop_segment(conn: sqlite3.Connection, seg_id: int) -> None:
    conn.execute("DELETE FROM segment WHERE id=?", (seg_id,))
    conn.commit()


def open_segments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM segment WHERE state='open'"))


# -------------------------------------------------------------------- asr

def save_asr(
    conn: sqlite3.Connection, seg_id: int, text: str, lang: str, emotion: str,
    events: Iterable[str], raw: str, model: str, rtf: float,
    labels: Iterable[dict] | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO asr(segment_id,text,lang,emotion,events,raw,"
        "model,rtf,labels,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (seg_id, text, lang, emotion, json.dumps(list(events), ensure_ascii=False),
         raw, model, rtf,
         json.dumps(list(labels or []), ensure_ascii=False), time.time()),
    )
    conn.commit()


def pending_asr(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT s.* FROM segment s LEFT JOIN asr a ON a.segment_id=s.id "
        "WHERE s.state='closed' AND a.segment_id IS NULL "
        "ORDER BY s.start_ts LIMIT ?", (limit,)))


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    conn.commit()


def get_meta(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default
