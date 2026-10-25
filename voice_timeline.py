"""语音 × 屏幕 时间对齐与合并时间线。

⚠ 时间口径（2026-10-24 实测钉死，别凭直觉改）：
    voice.db  start_ts / end_ts        = 真实 UTC Unix 秒（time.time()）
    Windrecorder  video_text.videofile_time
        = timegm(localtime())，即**本地墙钟当成 UTC 编码**，比真实 UTC 多出
          28800 秒（UTC+8）。实测：本地 16:48:39 的记录存为 1792860519，
          该数值按 UTC 解释正好是 16:48:39。
    因此对齐前必须 to_wind() 换算，否则整体错 8 小时。
    另：该表**没有** datetime_str_record 字段。

Windrecorder 按月分库：userdata/db/default_YYYY-MM_wind.db。
这里以只读挂载，绝不写对方的库。
"""
from __future__ import annotations

import calendar
import datetime as dt
import glob
import sqlite3
import time
from pathlib import Path
from typing import Optional

import vcfg
import voice_db

# 屏幕记录库的位置。默认是开发机老位置；整包搬到别的电脑后，在
# config.json 里写 "windrecorder_dir" 指过去。没有屏幕记录不影响语音。
WIND_DB_DIR = Path((vcfg.load().get("windrecorder_dir") or "").strip()
                   or r"E:\AI\Windrecorder") / "userdata" / "db"


def to_wind(ts: float) -> float:
    """真实 UTC epoch → Windrecorder 的 videofile_time 口径。"""
    return calendar.timegm(time.localtime(ts))


def from_wind(wt: float) -> float:
    """Windrecorder 口径 → 真实 UTC epoch（反算，用于校验/展示）。"""
    return time.mktime(time.gmtime(wt))


def wind_db_for(ts: float) -> Optional[Path]:
    m = dt.datetime.fromtimestamp(to_wind(ts)).strftime("%Y-%m")
    p = WIND_DB_DIR / f"default_{m}_wind.db"
    if p.exists():
        return p
    cands = sorted(glob.glob(str(WIND_DB_DIR / "*_wind.db")))
    return Path(cands[-1]) if cands else None


class WindReader:
    """只读挂载 Windrecorder 的库。对外一律用真实 UTC 秒，内部自动换算。"""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.path = path
        uri = f"file:{path.as_posix()}?mode=ro"
        try:
            conn.execute("ATTACH DATABASE ? AS wind", (uri,))
        except sqlite3.OperationalError:
            conn.execute("ATTACH DATABASE ? AS wind", (str(path),))
        self.conn = conn
        self.has_table = bool(conn.execute(
            "SELECT 1 FROM wind.sqlite_master WHERE type='table' "
            "AND name='video_text'").fetchone())

    def frames_between(self, t0: float, t1: float, limit: int = 200):
        if not self.has_table:
            return []
        return list(self.conn.execute(
            "SELECT videofile_time, win_title, ocr_text FROM wind.video_text "
            "WHERE videofile_time BETWEEN ? AND ? ORDER BY videofile_time LIMIT ?",
            (to_wind(t0), to_wind(t1), limit)))

    def titles_between(self, t0: float, t1: float) -> list[str]:
        if not self.has_table:
            return []
        rows = self.conn.execute(
            "SELECT win_title, COUNT(*) c FROM wind.video_text "
            "WHERE videofile_time BETWEEN ? AND ? AND win_title IS NOT NULL "
            "GROUP BY win_title ORDER BY c DESC",
            (to_wind(t0), to_wind(t1))).fetchall()
        return [r[0] for r in rows if r[0]]

    def screens_during(self, t0: float, t1: float, max_age: float = 1800.0):
        """返回这段时间内的屏幕状态序列。

        行语义：一行 = 从 videofile_time 起、到下一行为止的屏幕状态，
        因此**必须带上 t0 之前的最后一行**（状态延续），
        只做区间求交会大面积落空（实测帧间隔约 2 分钟）。
        返回 [(真实UTC秒, win_title, ocr_text, 距t0的秒数), ...]
        """
        if not self.has_table:
            return []
        out = []
        base = self.conn.execute(
            "SELECT videofile_time, win_title, ocr_text FROM wind.video_text "
            "WHERE videofile_time <= ? AND videofile_time >= ? "
            "ORDER BY videofile_time DESC LIMIT 1",
            (to_wind(t0), to_wind(t0 - max_age))).fetchone()
        if base:
            out.append((from_wind(base[0]), base[1], base[2], t0 - from_wind(base[0])))
        for r in self.conn.execute(
            "SELECT videofile_time, win_title, ocr_text FROM wind.video_text "
            "WHERE videofile_time > ? AND videofile_time <= ? "
            "ORDER BY videofile_time", (to_wind(t0), to_wind(t1))):
            out.append((from_wind(r[0]), r[1], r[2], 0.0))
        return out

    def nearest(self, ts: float, window: float = 60.0):
        if not self.has_table:
            return None
        return self.conn.execute(
            "SELECT videofile_time, win_title FROM wind.video_text "
            "WHERE videofile_time BETWEEN ? AND ? "
            "ORDER BY ABS(videofile_time - ?) LIMIT 1",
            (to_wind(ts - window), to_wind(ts + window), to_wind(ts))).fetchone()


def app_of(title: str) -> str:
    """win_title 格式为 '窗口标题 | 进程名'，取进程名做粗归类。"""
    if not title:
        return "?"
    parts = title.split("|")
    return parts[-1].strip() if len(parts) > 1 else title.strip()


def merged(conn: sqlite3.Connection, since: float, until: float,
           wind: Optional[WindReader]) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.start_ts, s.end_ts, s.duration, s.rms, "
        "       a.text, a.emotion, a.events, a.lang, sp.label AS spk, sp.score AS spk_score "
        "FROM segment s LEFT JOIN asr a ON a.segment_id = s.id "
        "LEFT JOIN speaker sp ON sp.segment_id = s.id "
        "WHERE s.state='closed' AND s.end_ts >= ? AND s.start_ts <= ? "
        "ORDER BY s.start_ts", (since, until)).fetchall()

    out = []
    for r in rows:
        apps: list[str] = []
        stale = 0.0   # 屏幕状态比这一句早了多少秒才被记录（越大越不可靠）
        if wind:
            for _, title, _ocr, age in wind.screens_during(r["start_ts"], r["end_ts"]):
                a = app_of(title)
                if a and a != "?" and a not in apps:
                    apps.append(a)
                    stale = max(stale, age)
        out.append({
            "id": r["id"],
            "start": r["start_ts"],
            "end": r["end_ts"],
            "dur": r["duration"],
            "text": r["text"],
            "emotion": r["emotion"],
            "events": r["events"],
            "speaker": r["spk"],
            "spk_score": r["spk_score"],
            "apps": apps,
            "stale": stale,
        })
    return out


def _fmt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def print_timeline(items: list[dict], show_apps: bool = True) -> None:
    if not items:
        print("（该时段没有语音段）")
        return
    for it in items:
        head = f"{_fmt(it['start'])}–{_fmt(it['end'])}  {it['dur']:5.1f}s"
        tags = []
        if it.get("speaker"):
            s = it["speaker"]
            if s in ("其他", "?"):
                tags.append(s)
            else:
                tags.append(f"说话人:{s}")
        if it["emotion"]:
            tags.append(it["emotion"])
        if it["events"]:
            tags.append(it["events"].strip("[]").replace('"', ""))
        if tags:
            head += "  [" + " ".join(tags) + "]"
        print(head)
        if show_apps and it["apps"]:
            note = ""
            if it.get("stale", 0) > 90:
                note = f"（状态取自 {it['stale']/60:.0f} 分钟前的画面）"
            print("      同时屏幕:", " / ".join(it["apps"][:4]), note)
        txt = it["text"]
        print("      " + ("（未转写）" if txt is None else (txt or "（静音/无内容）")))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="打印合并时间线")
    ap.add_argument("--date", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--days", type=float, default=None, help="最近 N 天")
    a = ap.parse_args()

    if a.days:
        until = dt.datetime.now().timestamp()
        since = until - a.days * 86400
    else:
        d = dt.datetime.strptime(a.date, "%Y-%m-%d") if a.date else dt.datetime.now()
        since = dt.datetime(d.year, d.month, d.day).timestamp()
        until = since + 86400

    conn = voice_db.connect()
    voice_db.init(conn)
    wp = wind_db_for(since)
    wind = WindReader(conn, wp) if wp and wp.exists() else None
    fmt = "%Y-%m-%d %H:%M"
    t0s = dt.datetime.fromtimestamp(since).strftime(fmt)
    t1s = dt.datetime.fromtimestamp(until).strftime(fmt)
    print(f"时段 {t0s} – {t1s}", end="")
    print(f"   屏幕库: {wp.name if wp else '未找到'}")
    print("-" * 70)
    print_timeline(merged(conn, since, until, wind))
