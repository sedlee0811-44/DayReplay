# -*- coding: utf-8 -*-
"""总账里的自然语言搜索，以及"录音文字稿"页。

用户要的三件事：
  1. 在总账页面里直接用大白话搜 —— "查查 10 月 25 日我做了什么"、
     "我记得我看过越南改革的事，帮我找找当时的录音"；
  2. 搜到的录音要点得开（wav 硬链进 reports/ev，file:// 和预览窗口都通）；
  3. 每条录音还要有个"文字内容版"，点开就是一篇能读的文章。

为什么索引直接嵌在页面里，而不是放个 json 让前端 fetch：
总账是双击打开的（file://），Chrome 在 file:// 下 fetch 本地文件会被
CORS 拦掉。所以索引必须内联，整个页面保持自包含。

日期解析和打分全部在前端做规则匹配，不调模型 —— 页面要"双击就能用"，
不能依赖 Ollama 起没起。规则覆盖：绝对日期（10月25日/10-25/25日）、
相对日期（今天/昨天/前天）、关键词（含 2-gram 兜底）。
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import vcfg

REPORT_DIR = vcfg.ROOT / "reports"
TR_DIR = REPORT_DIR / "ev" / "voice"        # 文字稿落点

# 索引里正文的最大长度。全量太胖（页面要内联），但截太短又搜不到后半句。
# 完整文字永远在文字稿页里，这里只承担"能被搜到"的职责。
TEXT_CAP = 400
MAX_ITEMS = 900                            # 语音段上限，防页面无限长大
MAX_SCREENS = 600                          # 屏幕段上限
SEARCH_DAYS = 21                           # 索引只覆盖最近这么多天


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ------------------------------------------------------------------ 取数据

def voice_segments(t0: float, t1: float) -> list[dict]:
    """逐段取录音（不是合并后的"一句话"）。

    合并只在展示时做（连着的话并成一条读着顺）；搜索和文字稿必须落到
    段上 —— 一段一个 wav、一段一段文字，用户点开哪段就是哪段。
    """
    if not vcfg.DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{vcfg.DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT s.id, s.start_ts, s.end_ts, s.duration, s.wav_path, "
            "a.text, a.emotion, a.events, a.labels, sp.label AS spk "
            "FROM segment s "
            "LEFT JOIN asr a ON a.segment_id=s.id "
            "LEFT JOIN speaker sp ON sp.segment_id=s.id "
            "WHERE s.state='closed' AND s.end_ts>=? AND s.start_ts<=? "
            "ORDER BY s.start_ts DESC LIMIT ?",
            (t0, t1, MAX_ITEMS)).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    import vevents

    out: list[dict] = []
    for r in rows:
        labels = vevents.parse_labels(r["labels"] if "labels" in r.keys() else None)
        if not labels and r["events"]:
            try:
                old = json.loads(r["events"])
            except Exception:
                old = []
            labels = [vevents.LEGACY_EVENT_ZH[t] for t in old
                      if t in vevents.LEGACY_EVENT_ZH]
        txt = (r["text"] or "").strip()
        if not txt and not labels:
            continue
        out.append({
            "id": r["id"],
            "t": r["start_ts"],
            "day": _day(r["start_ts"]),
            "hm": _hm(r["start_ts"]),
            "dur": round(float(r["duration"] or 0.0), 1),
            # "?" 是声纹没认出来时的占位，别当成名字显示
            "spk": "" if (r["spk"] or "") in ("", "?", "unknown") else r["spk"],
            "text": txt[:TEXT_CAP],
            "full": len(txt) > TEXT_CAP,
            "emotion": r["emotion"] or "",
            "labels": labels,
            "wav": r["wav_path"] or "",
        })
    return out


# ------------------------------------------------------------------ 文字稿

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0e1013;color:#c9d2de;
     font:15px/1.9 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     padding:46px 22px 90px}
.wrap{max-width:760px;margin:0 auto}
a.back{color:#8fb7ff;text-decoration:none;font-size:13px;letter-spacing:.03em}
a.back:hover{text-decoration:underline}
h1{font-size:22px;color:#eef2f8;margin:18px 0 6px;line-height:1.45;font-weight:600}
.meta{font:12px/1.7 "SFMono-Regular",Consolas,monospace;color:#6b7787;
      letter-spacing:.04em;margin:0 0 22px}
.meta b{color:#9aa5b4;font-weight:500}
.meta .tag{color:#d4a35e}
audio{width:100%;margin:0 0 30px;filter:invert(.92) hue-rotate(180deg)}
.art{border-left:2px solid #232a35;padding-left:20px}
.art p{margin:0 0 1.1em;color:#d6dde8}
.art p:first-child{margin-top:0}
.note{color:#7d8899;font-size:13.5px;background:#14181e;border:1px solid #1e242e;
      border-radius:10px;padding:14px 16px;margin:0 0 22px}
.note b{color:#d4a35e;font-weight:600}
.mark{background:rgba(143,183,255,.22);color:#dbe8ff;border-radius:3px;padding:0 2px}
"""


def transcript_rel(seg_id: int) -> str:
    """文字稿在 reports 下的相对地址（和 _url 的约定一致）。"""
    return (Path("ev") / "voice" / f"t{seg_id}.html").as_posix()


def write_transcript(seg: dict, wav_url: str) -> str:
    """给一段录音写一篇"文字内容版"，返回相对总账的链接。

    wav_url 是调用方已经用 _url() 解析好的地址（相对 reports/），这里再
    折算成相对文字稿自己所在目录的地址。
    """
    dst = TR_DIR / f"t{seg['id']}.html"
    TR_DIR.mkdir(parents=True, exist_ok=True)

    audio_src = os.path.relpath(
        (REPORT_DIR / wav_url).as_posix(), dst.parent.as_posix()).replace("\\", "/")
    back = os.path.relpath((REPORT_DIR / "总账.html").as_posix(),
                           dst.parent.as_posix()).replace("\\", "/")

    txt = (seg.get("text") or "").strip()
    labels = seg.get("labels") or []
    meta = [f"<b>时长</b> {seg.get('dur', 0):.1f} 秒"]
    if seg.get("spk"):
        meta.append(f"<b>说话人</b> {_esc(seg['spk'])}")
    if seg.get("emotion"):
        meta.append(f"<b>情绪</b> {_esc(seg['emotion'])}")
    if labels:
        meta.append('<span class="tag">' +
                    "、".join(_esc(x) for x in labels) + "</span>")

    if txt:
        paras = [p.strip() for p in re.split(r"(?<=[。！？；!?;])\s+|\n+", txt) if p.strip()]
        if not paras:
            paras = [txt]
        body = '<div class="art">' + "".join(f"<p>{_esc(p)}</p>" for p in paras) + "</div>"
    else:
        body = ('<div class="note">这段没有转写出文字 —— 它是一段<b>活动声</b>'
                "（没有成句的人话，只有声音本身）。上面可以直接放来听。</div>")

    title = f"录音文字稿 · {seg.get('day','')} {seg.get('hm','')}"
    page = (f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>"
            f"<div class=\"wrap\"><a class=\"back\" href=\"{_esc(back)}\">← 返回总账</a>"
            f"<h1>{_esc(title)}</h1>"
            f"<p class=\"meta\">{'　·　'.join(meta)}</p>"
            f"<audio controls preload=\"none\" src=\"{_esc(audio_src)}\"></audio>"
            f"{body}</div></body></html>")

    # 已经写过、内容也没变就不再落盘（实时模式每 5 分钟跑一轮）
    try:
        if dst.exists() and dst.read_text(encoding="utf-8") == page:
            return transcript_rel(seg["id"])
    except Exception:
        pass
    _atomic(dst, page)
    return transcript_rel(seg["id"])


def _atomic(dst: Path, text: str) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dst)


# ------------------------------------------------------------------ 索引

def build_index(days: list[dict], voice: list[dict], screens: list[dict]) -> dict:
    """组装内联到页面里的检索索引。

    days/screens 由调用方给（都已经是可 JSON 化的 dict）；voice 是逐段
    的原始记录，wav 地址和文字稿地址在这里补全。
    """
    return {"generated": int(time.time()), "days": days,
            "voice": voice, "screens": screens}


def index_json(obj: dict) -> str:
    """JSON 内联进 <script> 时要防 </script> 提前闭合。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/")
