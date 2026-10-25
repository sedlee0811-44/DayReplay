"""证据采集 —— 把一个会话里的四类原始材料抽成结构化的"事实"。

四类材料：
    file_changes()   会话期间被创建/修改的文件（路径、大小、新旧）
    screen_activity()  按应用聚合的屏幕活动（含标题里的文档名）
    voice_lines()    合并后的说话内容（带说话人）
    print_events()   打印记录（需系统日志已启用）

设计原则：
    **只取事实，不做判断**。判断留给 vdigest 和模型。
    这里输出的每一项都必须能在原始数据里指回去，不允许推测。

性能约束（实测）：
    全盘 os.walk + os.stat 扫 10 万文件要 119 秒，太慢。
    改用 os.scandir + entry.stat()（省一次系统调用），加扩展名白名单、
    目录剪枝和 90 秒时间预算，超时就截断并标记 truncated。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import vcfg
import voice_timeline as vt

CREATE_NO_WINDOW = 0x08000000

# 只认"人做出来的东西"，日志/缓存/临时文件一律不进来
KEEP_EXT = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf", ".txt", ".md",
    ".csv", ".dwg", ".dxf", ".mpp", ".vsdx", ".xmind", ".rtf", ".wps",
    ".jpg", ".jpeg", ".png", ".psd", ".mp4", ".mkv", ".avi", ".mov",
    ".zip", ".rar", ".7z", ".py", ".html", ".htm", ".json", ".bat", ".ps1",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "AppData", "Application Data",
    "$RECYCLE.BIN", "System Volume Information", ".venv", "venv", "env",
    "site-packages", "360RecycleBin", "Recovery", "Windows", "Program Files",
    "Program Files (x86)", "ProgramData", "found.000", "$WinREAgent",
    "models", "cache_screenshot", "cache", "logs", "temp", "Temp", "tmp",
    "Microsoft", "Packages", "Local", "Roaming", "LocalLow",
    ".workbuddy", ".cache", ".npm", ".ollama", "Dist-packages",
}

# 这些是采集系统自己的机器数据（库、日志、音频），不该出现在"我做了什么"里
EXCLUDE_PREFIX = (str(vcfg.DATA), str(Path("E:/AI/Windrecorder/userdata")),
                  str(Path("E:/AI/Windrecorder/cache")))

DOC_RE = re.compile(
    r"([\w\u4e00-\u9fff\-—（）()【】\[\]＋+＆&·]+\.(?:docx?|xlsx?|pptx?|pdf|"
    r"txt|md|csv|dwg|dxf|mpp|vsdx|xmind|rtf|wps))", re.IGNORECASE)


# --------------------------------------------------------------- 文件活动

def _roots() -> list[Path]:
    home = Path.home()
    return [home / "Desktop", home / "Documents", home / "Downloads",
            home / "Pictures", home / "Videos", Path("D:/"), Path("E:/")]


def file_changes(t0: float, t1: float, budget: float = 90.0) -> dict:
    """会话期间动过的文件。返回 {items:[...], truncated:bool, scanned:int}"""
    deadline = time.time() + budget
    items: list[dict] = []
    scanned = 0
    truncated = False

    for root in _roots():
        if truncated or not root.exists():
            continue
        base_depth = len(root.parts)
        stack = [str(root)]
        while stack:
            if time.time() > deadline:
                truncated = True
                break
            dp = stack.pop()
            if dp.startswith(EXCLUDE_PREFIX):
                continue
            depth = len(Path(dp).parts) - base_depth
            try:
                with os.scandir(dp) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if depth < 4 and e.name not in SKIP_DIRS \
                                        and not e.name.startswith("$"):
                                    stack.append(e.path)
                                continue
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext not in KEEP_EXT:
                                continue
                            scanned += 1
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        # 用 max 兜住：某些程序改文件不改 mtime，但 ctime 会动
                        touched = max(st.st_mtime, st.st_ctime)
                        if t0 <= touched <= t1:
                            items.append({
                                "path": e.path,
                                "name": e.name,
                                "ext": ext,
                                "size": st.st_size,
                                "mtime": st.st_mtime,
                                "is_new": st.st_ctime >= t0,
                            })
            except OSError:
                continue

    # 同一目录下连续产生的一堆碎文件（如导出的图片序列）合并成一条
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items, "truncated": truncated, "scanned": scanned}


def group_files(items: list[dict], max_show: int = 20) -> list[dict]:
    """按目录聚类，碎文件量大的目录折叠成"目录 + N 个文件"。"""
    by_dir: dict[str, list[dict]] = {}
    for it in items:
        by_dir.setdefault(os.path.dirname(it["path"]), []).append(it)
    out = []
    for d, fs in by_dir.items():
        fs.sort(key=lambda x: x["mtime"], reverse=True)
        if len(fs) > 6:
            out.append({"dir": d, "collapsed": True, "count": len(fs),
                        "sample": [f["name"] for f in fs[:3]],
                        "last": fs[0]["mtime"]})
        else:
            for f in fs:
                out.append({"dir": d, "collapsed": False, "path": f["path"],
                            "name": f["name"], "ext": f["ext"],
                            "size": f["size"], "is_new": f["is_new"],
                            "last": f["mtime"], "peek": peek_file(f["path"])})
    out.sort(key=lambda x: x["last"], reverse=True)
    return out[:max_show]


# --------------------------------------------------------------- 文档内容

def peek_file(path: str, limit: int = 220) -> str:
    """抽取文档的正文片段，用来回答"这份文档里写了什么"。

    不装 python-docx：docx/xlsx 本质是 zip，直接读里面的 XML 就够拿正文。
    失败一律返回空串，不让它影响主流程。
    """
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in (".md", ".txt", ".csv", ".py", ".json", ".html", ".htm",
                   ".bat", ".ps1", ".rtf"):
            raw = p.read_text(encoding="utf-8", errors="ignore")
            if ext in (".html", ".htm"):
                raw = re.sub(r"<[^>]+>", " ", raw)
            raw = re.sub(r"\s+", " ", raw).strip()
            return raw[:limit]

        if ext in (".docx", ".doc"):
            import zipfile
            with zipfile.ZipFile(p) as z:
                try:
                    xml = z.read("word/document.xml").decode("utf-8", "ignore")
                except KeyError:
                    return ""
            txt = re.sub(r"<w:p[ >]", "\n<w:p ", xml)
            txt = re.sub(r"<[^>]+>", "", txt)
            txt = re.sub(r"[ \t]+", " ", txt)
            return re.sub(r"\s+", " ", txt).strip()[:limit]

        if ext in (".pptx", ".ppt"):
            import zipfile
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist()
                         if re.match(r"ppt/slides/slide\d+\.xml$", n)]
                names.sort(key=lambda n: int(re.findall(r"\d+", n)[-1]))
                buf = []
                for n in names[:6]:
                    xml = z.read(n).decode("utf-8", "ignore")
                    t = re.sub(r"<[^>]+>", " ", xml)
                    t = re.sub(r"\s+", " ", t).strip()
                    if t:
                        buf.append(t)
            return " | ".join(buf)[:limit]

        if ext in (".xlsx", ".xls"):
            import zipfile
            with zipfile.ZipFile(p) as z:
                try:
                    xml = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
                except KeyError:
                    return ""
            t = re.sub(r"<[^>]+>", " ", xml)
            return re.sub(r"\s+", " ", t).strip()[:limit]
    except Exception:
        return ""
    return ""


# --------------------------------------------------------------- 屏幕活动

def _wind_db(t0: float) -> Path | None:
    return vt.wind_db_for(t0)


def screen_activity(t0: float, t1: float, gap_break: float = 900.0) -> list[dict]:
    """按"应用 + 时间连续"聚合成活动段。

    Windrecorder 的帧很稀（约 2 分钟一帧），不要指望秒级。
    这一层的用途是回答"这段时间主要在用什么、在弄哪个文件"。
    """
    db = _wind_db(t0)
    if not db or not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT videofile_time, win_title, ocr_text FROM video_text "
            "WHERE videofile_time BETWEEN ? AND ? ORDER BY videofile_time",
            (vt.to_wind(t0), vt.to_wind(t1))).fetchall()
        conn.close()
    except Exception:
        return []
    if not rows:
        return []

    segs: list[dict] = []
    cur: dict | None = None
    for wt, title, ocr in rows:
        ts = vt.from_wind(wt)
        proc = vt.app_of(title or "")
        if cur and (proc != cur["proc"] or ts - cur["last"] > gap_break):
            segs.append(cur)
            cur = None
        if cur is None:
            cur = {"proc": proc, "start": ts, "last": ts, "frames": 0,
                   "titles": [], "ocr": "", "docs": []}
        cur["frames"] += 1
        cur["last"] = ts
        if title and title not in cur["titles"]:
            cur["titles"].append(title)
        for m in DOC_RE.findall((title or "") + " " + (ocr or "")):
            if m not in cur["docs"]:
                cur["docs"].append(m)
        if ocr:
            cur["ocr"] = ocr
    if cur:
        segs.append(cur)

    for s in segs:
        s["end"] = min(t1, s["last"] + 60)
        s["minutes"] = round((s["end"] - s["start"]) / 60.0, 1)
        s["ocr"] = _clean_ocr(s["ocr"])
        s["titles"] = s["titles"][:3]
        s["docs"] = s["docs"][:5]
    return [s for s in segs if s["minutes"] >= 0.5]


def _clean_ocr(s: str, limit: int = 260) -> str:
    """OCR 清洗统一走 vocr。

    以前这里有一份自己的清洗逻辑，和 vocr 会慢慢长歪（改一处忘一处），
    所以收敛到一处。vocr.clean 会顺手做繁简、误识对、UI 噪声过滤。
    """
    try:
        import vocr
        return vocr.clean(s, limit=limit)
    except Exception:
        # 兜底：vocr 出问题时至少别崩，退回最小清洗
        if not s:
            return ""
        lines = [re.sub(r"\s+", " ", x).strip() for x in s.split("\n")]
        return " / ".join(x for x in lines if len(x) >= 2)[:limit]


# --------------------------------------------------------------- 语音

def voice_lines(t0: float, t1: float, merge_gap: float = 90.0) -> list[dict]:
    """把同一人连着说的话并成一条，避免一句话一条。

    2026-10-25 起多一类条目：**活动声**（kind='event'）——没有转写文字、
    但有中文活动标签的段（小孩啼哭、走动、水龙头流水…）。以前这里见到
    空文本就 continue，活动声段全被跳过，总账上一个字都不显示。
    同类活动间隔够近也合并（连着走动的几步并成一条，n 计段数）。
    """
    conn = sqlite3.connect(str(vcfg.DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.id, s.start_ts, s.end_ts, s.duration, s.wav_path, a.text, a.emotion, "
        "a.events, a.labels, sp.label AS spk FROM segment s "
        "LEFT JOIN asr a ON a.segment_id=s.id "
        "LEFT JOIN speaker sp ON sp.segment_id=s.id "
        "WHERE s.state='closed' AND s.end_ts>=? AND s.start_ts<=? "
        "ORDER BY s.start_ts", (t0, t1)).fetchall()
    conn.close()

    import vevents

    out: list[dict] = []
    cur: dict | None = None        # 说话条目游标
    ev: dict | None = None         # 活动声条目游标
    for r in rows:
        txt = (r["text"] or "").strip()
        labels = vevents.parse_labels(r["labels"] if "labels" in r.keys() else None)
        if not labels and r["events"]:
            # 老行没有 labels 列，英文事件标签现映射（Cry→啼哭 等）
            try:
                old = json.loads(r["events"])
            except Exception:
                old = []
            labels = [vevents.LEGACY_EVENT_ZH[t] for t in old
                      if t in vevents.LEGACY_EVENT_ZH]
        if txt:
            key = r["spk"] or "?"
            seg = {"id": r["id"], "start": r["start_ts"], "end": r["end_ts"],
                   "dur": float(r["duration"] or 0.0), "text": txt,
                   "wav": r["wav_path"], "labels": labels,
                   "emotion": r["emotion"] or ""}
            if cur and cur["spk"] == key and r["start_ts"] - cur["end"] <= merge_gap:
                cur["end"] = r["end_ts"]
                cur["text"] += txt
                cur["n"] += 1
                cur["segs"].append(seg)
                if r["emotion"]:
                    cur["emotions"].add(r["emotion"])
            else:
                if cur:
                    out.append(cur)
                cur = {"spk": key, "start": r["start_ts"], "end": r["end_ts"],
                       "text": txt, "n": 1, "wav": r["wav_path"],
                       "segs": [seg],
                       "emotions": {r["emotion"]} if r["emotion"] else set()}
        elif labels:
            seg = {"id": r["id"], "start": r["start_ts"], "end": r["end_ts"],
                   "dur": float(r["duration"] or 0.0), "text": "",
                   "wav": r["wav_path"], "labels": labels,
                   "emotion": r["emotion"] or ""}
            if (ev and ev["labels"] == labels
                    and r["start_ts"] - ev["end"] <= merge_gap):
                ev["end"] = r["end_ts"]
                ev["n"] += 1
                ev["segs"].append(seg)
            else:
                if ev:
                    out.append(ev)
                ev = {"spk": "活动声", "kind": "event", "labels": labels,
                      "start": r["start_ts"], "end": r["end_ts"], "text": "",
                      "n": 1, "wav": r["wav_path"], "segs": [seg],
                      "emotions": set()}
        # 既没文字也没标签：纯噪声段，不出现
    if cur:
        out.append(cur)
    if ev:
        out.append(ev)
    out.sort(key=lambda o: o["start"])
    for o in out:
        o["emotions"] = sorted(e for e in o["emotions"] if e)
    return out


# --------------------------------------------------------------- 离座 / 走神

# 短视频、外放媒体常出现的词。只用来"提示可能是",不单独据此下结论。
MEDIA_HINTS = ("抖音", "快手", "头条", "小红书", "哔哩", "B站", "西瓜",
               "下一个", "老铁", "点赞", "关注", "直播", "带货",
               "歌曲", "音乐", "节目", "段子", "搞笑", "影视", "解说")


def _voiced_segments(t0: float, t1: float) -> list[dict]:
    """时间窗内所有有声段。

    刻意**不过滤 text 为空**：抖音的背景音乐、纯音乐、外语片，
    转写经常是空的，但它们恰恰是"在用手机放东西"最主要的证据。
    只查有文字的那些会把证据先扔掉。
    """
    try:
        conn = sqlite3.connect(str(vcfg.DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT s.id, s.start_ts, s.end_ts, s.duration, s.wav_path, "
            "a.text, a.events, sp.label AS spk FROM segment s "
            "LEFT JOIN asr a ON a.segment_id=s.id "
            "LEFT JOIN speaker sp ON sp.segment_id=s.id "
            "WHERE s.state='closed' AND s.end_ts>=? AND s.start_ts<=? "
            "ORDER BY s.start_ts", (t0, t1)).fetchall()
        conn.close()
    except Exception:
        return []
    return [dict(r) for r in rows]


def idle_blocks(t0: float, t1: float, screens: list[dict] | None = None,
                min_gap: float = 600.0, min_voice: float = 40.0,
                min_cover: float = 600.0) -> list[dict]:
    """找"人离开电脑、但屋里有声音"的时段 —— 也就是走神刷手机。

    为什么要做这个：
        等一个长任务的时候人会离开电脑去刷手机，这段时间的声音对
        "这次开机干了什么"没有信息量。但完全删掉又不诚实 —— 那段时间
        确实发生过。所以既不细写、也不假装没有：一句话带过。

    判断依据（按可靠度从高到低）：

        1. 这段时间电脑屏幕**完全没有变化**     ← 必要条件
           Windrecorder 只在画面变了才落新帧，所以"帧与帧之间的空档"
           就是"没人动电脑"的时间。
        2. 这段时间里有声音，且累计够长
        3. 旁证：声纹不像本人 / 出现 BGM / 转写里有短视频的词

    分层结论（**关键：只有 1+2 时不下判断**）：

        1+2 满足          → kind="voice"，只说"电脑没动、有声音"
        1+2+3 满足        → kind="media"，才写"像是在放视频/刷手机"

    宁可说"没记录清楚"，也不要把"家人在客厅说话"说成"你在刷抖音"。
    """
    if screens is None:
        screens = screen_activity(t0, t1)

    # 数据不足就不判：屏幕活动覆盖不到 10 分钟，说明录屏刚起或断了，
    # 这时候的"空档"是没数据，不是没人动电脑。
    cover = sum(s["end"] - s["start"] for s in screens) if screens else 0.0
    if cover < min_cover:
        return []

    # 屏幕静止区间 = 相邻活动段之间的空档（含首尾）。
    # 先按时间排序，避免 screen_activity 因应用切换而乱序。
    segs = sorted(screens, key=lambda s: s["start"])
    gaps: list[tuple[float, float]] = []
    prev_end = t0
    for s in segs:
        if s["start"] - prev_end >= min_gap:
            gaps.append((prev_end, s["start"]))
        prev_end = max(prev_end, s["end"])
    if t1 - prev_end >= min_gap:
        gaps.append((prev_end, t1))
    if not gaps:
        return []

    voices = _voiced_segments(t0, t1)
    out: list[dict] = []
    for g0, g1 in gaps:
        span = g1 - g0
        inside = [v for v in voices
                  if v["start_ts"] < g1 and v["end_ts"] > g0]
        if not inside:
            continue
        # 重叠时长（段可能跨出空档边界）
        voiced = sum(max(0.0, min(v["end_ts"], g1) - max(v["start_ts"], g0))
                     for v in inside)
        if voiced < min_voice:
            continue

        n_self = sum(1 for v in inside if (v["spk"] or "") not in ("", "?", "其他"))
        non_self = 1.0 - n_self / len(inside)
        bgm = any("BGM" in (v["events"] or "") for v in inside)
        text = "".join(v["text"] or "" for v in inside)
        hints = [h for h in MEDIA_HINTS if h in text]
        density = voiced / span if span else 0.0
        spks: dict[str, int] = {}
        for v in inside:
            k = v["spk"] or "?"
            spks[k] = spks.get(k, 0) + 1

        # 判"在放媒体"必须有一个**媒体专有**的特征。
        #
        # 曾经把"声纹不是本人"也算一条，结果家里人在客厅聊天会被判成
        # "你在刷抖音" —— 因为说话本来就不是本人。这个错必须避免：
        # 把"不是自己"当成"在放视频"，等于把家里人当成了手机。
        #
        #   强证据：BGM 声学事件 / 转写里出现短视频的词
        #   弱证据：持续高密度、且完全不是本人（连续几十分钟没人应答地"说"，
        #           更像在放而不是在聊）
        strong = bgm or bool(hints)
        weak = non_self >= 0.9 and density >= 0.45
        media = strong or weak
        out.append({
            "start": g0, "end": g1, "minutes": span / 60.0,
            "voiced": voiced, "density": round(density, 2),
            "non_self": round(non_self, 2), "bgm": bgm, "hints": hints[:3],
            "spks": spks, "segments": len(inside),
            "kind": "media" if media else "voice",
            "wav": next((v["wav_path"] for v in inside if v["wav_path"]), None),
        })
    return out


def _dur(mins: float) -> str:
    return f"{mins:.0f} 分钟" if mins < 90 else f"{mins/60:.1f} 小时"


def drift_sentence(blocks: list[dict]) -> str:
    """把离座时段压成"一句话"，给总账用。

    刻意写得像人随口提一句，不像报表 —— 用户要的就是这个分寸：
    如实记下"那会儿我不在电脑前"，但不描述具体看了什么。

    三种说法，取决于证据到什么程度：
        有 BGM / 短视频词   → "像是在刷手机"
        声纹是别的具体的人 → "像是屋里另一个人在说话"
        什么都判不出        → "有声音，分不清是什么"
    """
    if not blocks:
        return ""
    total = sum(b["minutes"] for b in blocks)
    media = [b for b in blocks if b["kind"] == "media"]
    b0 = blocks[0]
    span = time.strftime("%H:%M", time.localtime(b0["start"]))

    if media:
        bad = [h for b in media for h in b["hints"]]
        app = next((h for h in bad
                    if h in ("抖音", "快手", "小红书", "哔哩", "西瓜")), None)
        where = f"像是在{app}上刷" if app else "声音像是手机或电视在放节目"
        return (f"中间有大约 {_dur(total)}（从 {span} 起）电脑一直没动过，"
                f"{where}——猜是在用手机打发等待时间，这段只记一句，不展开。")

    # 没见过媒体特征。看看声纹有没有明确的人
    _LABEL = {"其他": "屋里的另一个人", "TTS": "一段合成语音"}
    people = {k for b in blocks for k in b["spks"] if k not in ("?", "", "我")}
    if people:
        who = "、".join(sorted(_LABEL.get(p, p) for p in people)[:2])
        return (f"中间有大约 {_dur(total)}（从 {span} 起）电脑一直没动过，"
                f"声纹显示是{who}在说话，没记具体内容。")

    return (f"中间累计约 {_dur(total)}（从 {span} 起）电脑没有动过，但有声音，"
            f"分不清是家里人在说话还是在放东西，因此没有细记。")


# --------------------------------------------------------------- 打印

_PS_PRINT = (
    "Get-WinEvent -LogName 'Microsoft-Windows-PrintService/Operational' "
    "-MaxEvents 60 -ErrorAction SilentlyContinue | "
    "Where-Object {$_.Id -eq 307} | "
    "Select-Object @{n='T';e={$_.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss')}},"
    "@{n='M';e={$_.Message}} | ConvertTo-Json -Compress"
)


def print_events(t0: float, t1: float) -> dict:
    """打印记录。日志默认是关的，未启用时返回 enabled=False。"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-WinEvent -ListLog 'Microsoft-Windows-PrintService/Operational')"
             ".IsEnabled"],
            capture_output=True, timeout=25, creationflags=CREATE_NO_WINDOW)
        enabled = p.stdout.decode("utf-8", "ignore").strip().lower() == "true"
    except Exception:
        return {"enabled": False, "items": []}
    if not enabled:
        return {"enabled": False, "items": []}

    items = []
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_PRINT],
            capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW)
        raw = p.stdout.decode("utf-8", "ignore").strip()
        if raw:
            import json as _j
            data = _j.loads(raw)
            if isinstance(data, dict):
                data = [data]
            for d in data:
                ts = time.mktime(time.strptime(d["T"], "%Y-%m-%dT%H:%M:%S"))
                if t0 <= ts <= t1:
                    items.append({"ts": ts, "msg": (d.get("M") or "")[:300]})
    except Exception:
        pass
    return {"enabled": True, "items": items}


if __name__ == "__main__":
    import sys
    t1 = time.time()
    t0 = t1 - float(sys.argv[1] if len(sys.argv) > 1 else 4) * 3600
    print(f"区间 {vt._fmt(t0)} – {vt._fmt(t1)}\n")

    t = time.time()
    fc = file_changes(t0, t1)
    print(f"[文件] 命中 {len(fc['items'])} 个，扫描 {fc['scanned']}，"
          f"截断={fc['truncated']}，耗时 {time.time()-t:.1f}s")
    for g in group_files(fc["items"], 12)[:12]:
        if g["collapsed"]:
            print(f"    {vt._fmt(g['last'])}  [{g['count']} 个文件] {g['dir']}")
        else:
            print(f"    {vt._fmt(g['last'])}  {'新建' if g['is_new'] else '修改'}  "
                  f"{g['size']/1024:7.1f}KB  {g['path']}")

    print("\n[屏幕]")
    for s in screen_activity(t0, t1):
        print(f"    {vt._fmt(s['start'])}–{vt._fmt(s['end'])} {s['minutes']:5.1f}min "
              f"{s['proc']:<20} 帧{s['frames']:>3}  文档={s['docs'][:2]}")
        if s["ocr"]:
            print(f"        OCR: {s['ocr'][:90]}")

    print("\n[语音]")
    for v in voice_lines(t0, t1):
        print(f"    {vt._fmt(v['start'])} {v['spk']:<6} {v['n']}段 "
              f"{'/'.join(v['emotions']):<12} {v['text'][:70]}")

    pe = print_events(t0, t1)
    print(f"\n[打印] 日志启用={pe['enabled']} 命中={len(pe['items'])}")
