"""多语言：中文（默认）/ 英文。

为什么需要这个模块
------------------
总账页面（reports/总账.html）的渲染管线只有一套，事实来源是总账 md。
要让同一个管线既能出中文页、又能出英文页（给 GitHub 展示、给不读中文的
贡献者看），有两条路：

    A. 复制一份渲染代码，改成英文      → 两份代码必然漂移（档案 #53 同款病）
    B. 渲染代码里所有文案走一个查表函数 → 改文案不动结构，结构永远唯一

选 B。语言由环境变量 DAYREPLAY_LANG 决定：

    DAYREPLAY_LANG=en python vdigest.py     # 出英文页
    DAYREPLAY_LANG=zh python vdigest.py     # 出中文页（默认，不设也行）

新增文案的规矩：先在 T 里加一条中英对照，再在渲染处用 t("key") 取。
别在渲染处直接写死中文 —— 那样英文页就会漏一处。
"""
from __future__ import annotations

import os

LANG = (os.environ.get("DAYREPLAY_LANG") or "zh").strip().lower()
IS_EN = LANG.startswith("en")

# key -> (中文, English)
T: dict[str, tuple[str, str]] = {
    # ---- 事件行的小标签 ----
    "screen":      ("屏幕", "Screen"),
    "file_log":    ("电脑日志", "File Log"),
    "voice":       ("语音", "Voice"),
    "details":     ("细节", "Details"),
    "openable":    ("可点开", "Open"),
    "orig":        ("原图", "Original"),

    # ---- 会话/节点 ----
    "session":     ("本次开机", "Session"),
    "sessions":    ("次开机", "sessions"),
    "events":      ("个节点", "events"),
    "min":         ("分钟", "min"),
    "hour":        ("小时", "h"),
    "expand":      ("点开看当天明细", "Expand this day"),
    "collapse":    ("收起明细", "Collapse"),
    "no_records":  ("这天没有可用记录", "No usable records for this day"),

    # ---- 页面 ----
    "page_title":  ("总账", "DayReplay Timeline"),
    "page_h1":     ("总　账", "T I M E L I N E"),
    "page_intro":  (
        "一根 S 形的轴，走到屏幕边上就折回下一行，新行的第一个点落在上一行最后一个点的正"
        "下方 —— 屏幕越宽，一行放得下的天就越多。轴上一天一个点，先看这天干了什么；点一"
        "下才长出当天的明细，再点收起。明细里的录音、截图、文档都能直接点开。",
        "One S-shaped axis: it folds to the next row at the screen edge, and each new row's "
        "first dot sits directly below the previous row's last dot — the wider the screen, "
        "the more days fit in a row. Each day is one dot: see what happened, click to grow "
        "the detail, click again to fold. Every recording, screenshot and document in the "
        "detail opens with one click.",
    ),
    "recording":   ("正在记录", "Recording"),
    "last_update": ("最后更新", "Last update"),
    "auto_refresh": ("分钟自动刷新，不用手动按 F5", "min auto-refresh, no need to press F5"),
    "build":       ("构建", "build"),
    "sessions_sub": ("次开机", "sessions"),
    "details_link": ("整场细节：", "Full session details:"),
    "want_details": ("想看细节：", "Want details:"),
    "search_ph":   (
        "大白话直接搜：10 月 25 日我做了什么 · 越南改革 · 录音里提到…",
        "Search in plain words: what did I do on Oct 25 · project review · something in a recording…",
    ),
    "reading":     ("正在读", "Reading"),
    "matches":     ("条命中", "matches"),
    "results_for": ("关于", "results for"),
    "no_hit":      ("没找到。换个说法再试试。", "Nothing found. Try different words."),
}

# 星期：中文一二三四五六日 → 英文
_WEEK_EN = {
    "一": "Monday", "二": "Tuesday", "三": "Wednesday", "四": "Thursday",
    "五": "Friday", "六": "Saturday", "日": "Sunday",
}
_WEEK_ZH = {
    "Monday": "周一", "Tuesday": "周二", "Wednesday": "周三", "Thursday": "周四",
    "Friday": "周五", "Saturday": "周六", "Sunday": "周日",
}
# 月份缩写：1 → Jan
_MONTH_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def t(key: str) -> str:
    """按当前语言取文案。key 忘了登记就抛错 —— 静默返回中文更糟。"""
    if key not in T:
        raise KeyError(f"vlang.T 里没有 '{key}' 这条文案")
    return T[key][1] if IS_EN else T[key][0]


def month_label(mo: int, dd: int) -> str:
    """'10 月 25 日' / 'Oct 25'。"""
    if IS_EN:
        return f"{_MONTH_EN[mo - 1]} {dd}"
    return f"{mo} 月 {dd} 日"


def week_label(wk: str) -> str:
    """'周日' / 'Sunday'。wk 可以是中文的'日'也可以是英文的'Sunday'。"""
    if IS_EN:
        return _WEEK_EN.get(wk, wk)
    return "周" + _WEEK_ZH.get(wk, wk)


def dur_text(h: int, m: int) -> str:
    """'12 小时 34 分' / '12h 34m'。"""
    if IS_EN:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{h} 小时 {m} 分"


def n_text(n: int, key: str) -> str:
    """'3 次开机' / '3 sessions'。英文用复数。"""
    zh, en = T[key]
    if not IS_EN:
        return f"{n} {zh}"
    if n == 1:
        return f"{n} {en.rstrip('s')}"
    return f"{n} {en}"


def hint_text(now: str, every_min: int) -> str:
    """页面顶部那条"正在记录"提示。

    整句走一个函数：中文和英文的语序不同，拆成词条拼起来会变成
    "Every 5 min auto-refresh" 这种半吊子句子。
    """
    if IS_EN:
        return (f"● {T['recording'][1]}　{T['last_update'][1]} {now}　"
                f"Auto-refreshes every {every_min} min, no need to press F5")
    return (f"● {T['recording'][0]}　{T['last_update'][0]} {now}　"
            f"每 {every_min} {T['auto_refresh'][0]}")
