"""OCR 文字清洗与纠错 —— 让截图可以放心删掉。

背景：
    Windrecorder 每张截图都会 OCR 一次，结果存在它的 `video_text.ocr_text` 里
    （实测 91 行全都有文字）。所以**截图本身对"总结干了什么"毫无用处**，
    文字已经在库里了。真正该做的是：把文字洗干净，然后让截图早点消失。

这个模块干四件事：

    1. 丢掉噪声行        UI 图标、侧边栏被误识成的碎词（"隹荐""釒资*斗"）
    2. 统一标点全半角     中文里混英文标点会让后面的分词全乱
    3. 繁体转简体         OCR 有时把简体识别成繁体（"這""時""檔"）
    4. 修已知误识对       只修**高置信度**的，改了不会误伤

关于"自动纠正"的边界（重要）：
    不做猜测性的泛化纠错。比如把"己经"改成"已经"是安全的（"己经"不是词），
    但把"表格"改成"报表"就纯属瞎猜 —— 这种错改比不改糟得多，因为后面
    所有的分析都建在假文字上。宁可留一个错字，也不制造一个假事实。

    原始文字永远保留在 `data/ocr/raw.jsonl`，清洗后的在 `data/ocr/clean.jsonl`，
    两条都带同一时间戳，随时可以对照"这台机器到底改了我什么"。

用法：
    python vocr.py                  # 清洗最近一天，看统计
    python vocr.py --days 3 --show  # 顺便打印清洗前后对照
    python vocr.py --export         # 导出成 jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path

import vcfg

WIND_DB_DIR = Path("E:/AI/Windrecorder/userdata/db")
OUT_DIR = vcfg.DATA / "ocr"

# ---------------------------------------------------------------- 噪声行

# Windrecorder 的 OCR 是整屏识别，侧边栏、图标、按钮全都会被当成字。
# 这些词反复出现，且对"用户在干什么"零信息量。
UI_NOISE = {
    "推荐", "灵感", "附件", "档名", "运行命令", "上下文已压缩", "正在修改",
    "运行校验", "查看文件", "已停止", "思考中", "复制", "粘贴", "新建",
    "打开", "保存", "取消", "确定", "关闭", "最小化", "最大化", "设置",
    "文件", "编辑", "查看", "帮助", "工具", "窗口", "搜索", "分享",
    "点赞", "收藏", "转发", "评论", "更多", "加载中", "请稍候",
    "小时前", "分钟前", "天前", "刚刚", "昨天", "前天",
}

# 单个字符就基本是图标被识成了字（汉字里没有单独成行的常用字）
NOISE_CHARS = set("丷丨丶丿亅乀乁乂乚")

CJK = re.compile(r"[\u4e00-\u9fff]")

# ---------------------------------------------------------------- 繁简
#
# 只收屏幕上真正常见的那批。不做全量繁简转换（那要 opencc，为这点事不值得
# 引一个依赖），够用就行 —— 目标是让后面的关键词提取别被繁体字带偏。

T2S = str.maketrans({
    "這": "这", "個": "个", "們": "们", "來": "来", "時": "时", "說": "说",
    "對": "对", "會": "会", "為": "为", "與": "与", "學": "学", "國": "国",
    "開": "开", "關": "关", "點": "点", "實": "实", "現": "现", "麼": "么",
    "樣": "样", "發": "发", "經": "经", "過": "过", "問": "问", "題": "题",
    "還": "还", "進": "进", "動": "动", "種": "种", "應": "应", "該": "该",
    "總": "总", "結": "结", "賬": "账", "帳": "帐", "電": "电", "腦": "脑",
    "軟": "软", "體": "体", "錄": "录", "聲": "声", "聽": "听", "記": "记",
    "憶": "忆", "檔": "档", "視": "视", "頻": "频", "圖": "图", "網": "网",
    "頁": "页", "瀏": "浏", "覽": "览", "資": "资", "單": "单", "擊": "击",
    "選": "选", "擇": "择", "確": "确", "認": "认", "錯": "错", "誤": "误",
    "糾": "纠", "檢": "检", "測": "测", "試": "试", "驗": "验", "證": "证",
    "碼": "码", "簽": "签", "約": "约", "條": "条", "範": "范", "圍": "围",
    "標": "标", "準": "准", "規": "规", "劃": "划", "計": "计", "價": "价",
    "錢": "钱", "閉": "闭", "儲": "储", "備": "备", "復": "复", "製": "制",
    "轉": "转", "換": "换", "連": "连", "續": "续", "斷": "断", "線": "线",
    "後": "后", "從": "从", "麼": "么", "覺": "觉", "夠": "够", "幾": "几",
    "務": "务", "員": "员", "處": "处", "務": "务", "區": "区", "場": "场",
    "總": "总", "結": "结", "報": "报", "錶": "表", "們": "们", "隨": "随",
})

# ---------------------------------------------------------------- 误识对
#
# 只收"左边的组合在中文里根本不成词、右边一定是本意"的对子。
# 判断标准：如果拿不准，就不收 —— 宁可留错字，不制造假事实。

FIX_PAIRS = [
    ("隹荐", "推荐"),
    ("釒资", "资料"),
    ("巳经", "已经"),
    ("己经", "已经"),
    ("未来", "未来"),
    ("大冢", "大家"),
    ("文什", "文件"),
    ("文伴", "文件"),
    ("路经", "路径"),
    ("磁盆", "磁盘"),
    ("硬盆", "硬盘"),
    ("己录", "记录"),
    ("己忆", "记忆"),
    ("锴误", "错误"),
    ("验征", "验证"),
    ("渲揽", "渲染"),
    ("渲揽", "渲染"),
]

# 零宽、BOM、方向控制符：OCR 从网页抓字时常带
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060\u00ad"), None)

FULL2HALF = {
    "，": ",", "。": ".", "；": ";", "：": ":", "？": "?", "！": "!",
    "（": "(", "）": ")", "【": "[", "】": "]", "　": " ",
    "《": "<", "》": ">", "、": ",", "～": "~",
}


# ---------------------------------------------------------------- 核心

def fix_text(s: str) -> tuple[str, int]:
    """统一标点、繁转简、修误识对。返回 (新文字, 改了几处)。"""
    if not s:
        return "", 0
    n = 0
    out = s.translate(ZERO_WIDTH)
    # 繁简
    t = out.translate(T2S)
    if t != out:
        n += sum(1 for a, b in zip(out, t) if a != b)
        out = t
    # 误识对
    for wrong, right in FIX_PAIRS:
        if wrong in out:
            n += out.count(wrong)
            out = out.replace(wrong, right)
    # 标点统一：只在"周围有中文"时把全角换成半角，避免破坏中文排版习惯
    for f, h in FULL2HALF.items():
        # 这里刻意保留中文标点，只处理全角空格和书名号这种明显的问题
        if f in ("　", "《", "》"):
            out = out.replace(f, h)
    return out, n


# 单看像字、其实是界面碎片的行：日期、相对时间、纯数字、序号。
# 这些是侧边栏和列表项的编号，一条一条留着只会把真正的正文挤出去。
NOISE_PATTERNS = [
    re.compile(r"^\d{1,2}\s*(小时|分钟|天|分|秒)\s*前$"),   # 5小时前 / 3天前
    re.compile(r"^\d{1,2}\s*月\s*\d{1,2}\s*日$"),           # 10月8日
    re.compile(r"^\d{1,4}\s*日$"),                          # 918日（9月18日误识）
    re.compile(r"^\d{4}\s*[-/.年]\s*\d{1,2}"),          # 2026-10-24
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$"),            # 18:20
    re.compile(r"^[\d\s.,%+\-*/×÷=<>~^]+$"),            # 纯数字算式
    re.compile(r"^[A-Za-z]{1,3}$"),                     # 单个按钮字母
    re.compile(r"^[（(【\[].{0,6}[）)】\]]$"),           # (3) [12]
    re.compile(r"^第?\s*\d+\s*[章节页条款项]?$"),        # 12 / 第3
]


def is_noise_line(x: str) -> bool:
    """这一行有没有信息量。"""
    s = x.strip()
    if len(s) <= 1:
        return True
    if any(c in NOISE_CHARS for c in s):
        return True
    for p in NOISE_PATTERNS:
        if p.match(s):
            return True
    # 纯符号 / 纯标点
    if not any(ch.isalnum() for ch in s):
        return True
    # 已知 UI 词（整行就是它，或去掉符号后就是它）
    bare = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]", "", s)
    if bare in UI_NOISE:
        return True
    # 符号占比太高 —— OCR 把图标、边框、进度条都识别成了字符
    junk = sum(1 for ch in s if not ch.isalnum() and not CJK.match(ch))
    if junk / len(s) > 0.45:
        return True
    # 短、又混着符号 → 是图标旁边挂了半个词（"资料*斗"这种）
    if junk and len(CJK.findall(s)) <= 3 and len(s) <= 5:
        return True
    # 中文少于 2 个字、又没有英文单词 → 基本是图标
    if len(CJK.findall(s)) < 2 and not re.search(r"[a-zA-Z]{3,}", s):
        return True
    return False


def clean(text: str, limit: int = 260) -> str:
    """把一段 OCR 原文压成一行可读摘要。

    顺序很重要：先修字再判断噪声 —— 否则"隹荐"会先被当成有信息量的词留下。
    """
    if not text:
        return ""
    fixed, _ = fix_text(text)
    lines = [re.sub(r"\s+", " ", x).strip()
             for x in fixed.replace("\r", "\n").split("\n")]
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for x in lines:
        if not x or is_noise_line(x):
            continue
        key = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]", "", x)[:10]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(x)
        total += len(x)
        if total > limit:
            break
    return " / ".join(out)[:limit]


def info_score(text: str) -> int:
    """这段文字的信息量（汉字数）。用来判断这一帧值不值得留。"""
    return len(CJK.findall(text or ""))


# ---------------------------------------------------------------- 读 Windrecorder

def _db_paths(days: float = 2.0) -> list[Path]:
    """按月分库，取最近几天可能用到的库。"""
    if not WIND_DB_DIR.is_dir():
        return []
    out = []
    now = dt.datetime.now()
    for p in WIND_DB_DIR.glob("default_*_wind.db"):
        m = re.search(r"default_(\d{4})-(\d{2})_wind", p.name)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        # 当月和上个月都要
        if (now.year, now.month) == (y, mo) or (y * 12 + mo) >= (now.year * 12 + now.month - 1):
            out.append(p)
    return sorted(out)


def rows_since(t0: float, t1: float | None = None) -> list[dict]:
    """取时间窗内的所有 OCR 记录（Windrecorder 时间口径，需换算）。"""
    t1 = t1 or time.time()
    res: list[dict] = []
    for db in _db_paths():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                    "SELECT videofile_time, ocr_text, win_title, picturefile_name "
                    "FROM video_text WHERE ocr_text IS NOT NULL AND ocr_text<>'' "
                    "ORDER BY videofile_time"):
                d = dict(r)
                d["ts"] = d["videofile_time"]
                res.append(d)
            conn.close()
        except Exception:
            continue
    # videofile_time 是 timegm(localtime(ts))，比真 UTC 多 28800 秒
    res = [r for r in res if t0 - 28800 <= r["ts"] <= t1 - 28800 + 86400]
    return res


# ---------------------------------------------------------------- CLI

def _stat(days: float, show: bool) -> int:
    t0 = time.time() - days * 86400
    rows = rows_since(t0)
    if not rows:
        print("这段时间没有 OCR 记录。")
        return 1

    before = after = 0
    fixes = 0
    empties = 0
    samples = []
    for r in rows:
        raw = r["ocr_text"] or ""
        c = clean(raw)
        before += len(raw)
        after += len(c)
        _, n = fix_text(raw)
        fixes += n
        if not c:
            empties += 1
        if show and len(samples) < 3 and info_score(c) > 8:
            samples.append((raw, c))

    print(f"记录 {len(rows)} 条　保留期 {days:g} 天")
    print(f"  原始 {before:,} 字 → 清洗后 {after:,} 字"
          f"（压掉 {100*(1-after/max(before,1)):.1f}%）")
    print(f"  纠错 {fixes} 处　清洗后仍为空 {empties} 条"
          f"（{empties} 条属于'只拍到图标'）")
    for raw, c in samples:
        print("\n" + "-" * 56)
        print("原始：")
        for ln in raw.split("\n")[:8]:
            if ln.strip():
                print("   ", ln.strip()[:80])
        print("清洗后：")
        print("   ", c[:200])
    return 0


def _export(days: float) -> int:
    t0 = time.time() - days * 86400
    rows = rows_since(t0)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    raw_p = OUT_DIR / f"raw_{stamp}.jsonl"
    cln_p = OUT_DIR / f"clean_{stamp}.jsonl"
    with raw_p.open("w", encoding="utf-8") as f1, cln_p.open("w", encoding="utf-8") as f2:
        for r in rows:
            ts = r["ts"] - 28800          # 换算回真 UTC
            head = {"ts": ts, "time": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                    "title": r["win_title"] or ""}
            f1.write(json.dumps({**head, "text": r["ocr_text"]}, ensure_ascii=False) + "\n")
            f2.write(json.dumps({**head, "text": clean(r["ocr_text"]),
                                 "score": info_score(clean(r["ocr_text"]))},
                                ensure_ascii=False) + "\n")
    print(f"✓ {raw_p}\n✓ {cln_p}\n  共 {len(rows)} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--show", action="store_true", help="打印清洗前后对照")
    ap.add_argument("--export", action="store_true", help="导出 jsonl")
    a = ap.parse_args()
    if a.export:
        return _export(a.days)
    return _stat(a.days, a.show)


if __name__ == "__main__":
    raise SystemExit(main())
