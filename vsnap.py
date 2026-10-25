"""每小时留一张小图 —— 把 2.7 GB/天 的截图缓存换成 5 MB/天。

为什么要这么做：
    Windrecorder 的 cache_screenshot 里放的是 4K 原图（单张 19~20 MB）和
    它的裁切版（11 MB）。这些图的唯一用途是"合成视频"和"做一次 OCR"。
    合成完、OCR 完就纯属垃圾 —— 但实测一天能堆 2.7 GB，一个月 80 GB。

    而**文字已经进了 Windrecorder 的库**（video_text.ocr_text，实测 95 条
    全都有），所以分析"当时在干什么"根本不需要这些图。

    但完全不存也有损失：过两天想回忆"那会儿屏幕上是什么样"，光有文字
    不够。所以降到每小时一张、每张压到 1600px 宽 —— 一天 24 张约 5 MB，
    比原来省 500 倍。

挑哪一张：
    挑这一小时里 **OCR 汉字最多**的那一帧。字最多 = 屏幕上信息最满 =
    最可能看出在干什么。纯看视频、玩游戏的时段字很少，就退化成挑
    时间靠中间的那张。

存哪：
    data/snapshots/2026-10-24/14-00.jpg
    已经存过的小时记在 data/snapshots/index.json，重启不会重复做。

用法：
    python vsnap.py                # 补做今天所有还没存的小时
    python vsnap.py --days 3       # 补最近三天
    python vsnap.py --once         # 只做一次就退出（调试用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import vcfg

# 屏幕记录装在哪（可换）：config.json 的 "windrecorder_dir"，留空回退
# 开发机老位置。指不到就一小时一张都挑不出来，安静地不干活，不报错。
WIND_ROOT = Path((vcfg.load().get("windrecorder_dir") or "").strip()
                 or "E:/AI/Windrecorder")
SHOT_DIR = WIND_ROOT / "cache_screenshot"
WIND_DB_DIR = WIND_ROOT / "userdata/db"
SNAP_DIR = vcfg.DATA / "snapshots"
INDEX = SNAP_DIR / "index.json"

MAX_WIDTH = 2400
MIN_HEIGHT = 640        # 这台机器是双屏 3840x1080，只按宽度缩会糊到看不清
JPEG_Q = 72
SCAN_EVERY = 300          # 5 分钟扫一次
CLEAN_EVERY = 6 * 3600    # 每 6 小时顺带清一次盘（见下）

log = None


def _init_log():
    global log
    import logging
    vcfg.setup_logging("snap.log", level=logging.INFO)
    log = logging.getLogger("snap")
    return log


def _say(msg: str) -> None:
    if log:
        log.info(msg)
    else:
        print(msg, flush=True)


# ------------------------------------------------------------------ 索引

def load_index() -> dict:
    if INDEX.exists():
        try:
            return json.loads(INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_index(idx: dict) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=0),
                   encoding="utf-8")
    tmp.replace(INDEX)


# ------------------------------------------------------------------ 数据

def _shot_time(name: str) -> float | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[_-](\d{2})-(\d{2})-(\d{2})", name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(
            "-".join(m.groups()[:3]) + " " + ":".join(m.groups()[3:]), 
            "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _candidates(since: float) -> dict[str, list[dict]]:
    """按小时分组，列出这一小时所有还有原图、且有 OCR 文字的帧。

    返回 {"2026-10-24 14": [{"png":Path, "ts":float, "score":int}, ...]}
    """
    groups: dict[str, list[dict]] = {}
    for db in sorted(WIND_DB_DIR.glob("default_*_wind.db")):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT videofile_time, ocr_text, picturefile_name FROM video_text "
                "WHERE picturefile_name IS NOT NULL AND picturefile_name<>''"
            ).fetchall()
            conn.close()
        except Exception:
            continue
        for r in rows:
            real_ts = r["videofile_time"] - 28800        # 换回真 UTC
            if real_ts < since:
                continue
            p = WIND_ROOT / r["picturefile_name"].replace("\\", "/")
            if not p.exists():
                continue
            # 只统计汉字数当信息量
            txt = r["ocr_text"] or ""
            score = len(re.findall(r"[\u4e00-\u9fff]", txt))
            key = dt.datetime.fromtimestamp(real_ts).strftime("%Y-%m-%d %H")
            groups.setdefault(key, []).append(
                {"png": p, "ts": real_ts, "score": score})
    return groups


def _pick(items: list[dict], hour_key: str) -> dict:
    """这一小时挑哪张：先看信息量，并列时取最靠近整点过 30 分的那张。"""
    h = dt.datetime.strptime(hour_key, "%Y-%m-%d %H")
    mid = h.timestamp() + 1800
    return max(items, key=lambda x: (x["score"], -abs(x["ts"] - mid)))


def _save(png: Path, out: Path) -> bool:
    """压成 JPEG。

    缩放规则：先按最大宽度算，如果那样高度不够（双屏截图特别扁），
    再按最小高度反算。两个约束取更保守的那个，保证字还看得见。
    """
    try:
        from PIL import Image
        with Image.open(png) as im:
            im = im.convert("RGB")
            sw = MAX_WIDTH / im.width
            sh = MIN_HEIGHT / im.height
            scale = min(sw, sh) if sh > sw else min(sw, 1.0)
            # 高度不够就按高度走，但不能超过最大宽度
            if im.height * scale < MIN_HEIGHT:
                scale = min(MIN_HEIGHT / im.height, MAX_WIDTH / im.width)
            if scale < 1.0:
                size = (round(im.width * scale), round(im.height * scale))
                im = im.resize(size, Image.LANCZOS)
            out.parent.mkdir(parents=True, exist_ok=True)
            im.save(out, "JPEG", quality=JPEG_Q, optimize=True,
                    progressive=True)
        return out.exists() and out.stat().st_size > 1024
    except Exception as e:
        _say(f"  压缩失败 {png.name}: {e}")
        return False


# ------------------------------------------------------------------ 主流程

def run_once(days: float = 1.0, dry: bool = False) -> dict:
    """补做最近几天还没存过的小时。返回统计。"""
    since = time.time() - days * 86400
    idx = load_index()
    groups = _candidates(since)
    made = skipped = failed = 0
    saved_bytes = 0

    for hour_key in sorted(groups):
        items = groups[hour_key]
        if not items:
            continue
        if hour_key in idx:
            skipped += 1
            continue
        best = _pick(items, hour_key)
        d = dt.datetime.strptime(hour_key, "%Y-%m-%d %H")
        out = SNAP_DIR / d.strftime("%Y-%m-%d") / d.strftime("%H-00.jpg")
        if dry:
            _say(f"  [试运行] 会存 {out.name}（来自 {best['png'].name}，"
                 f"{best['score']} 字）")
            made += 1
            continue
        if _save(best["png"], out):
            size = out.stat().st_size
            saved_bytes += size
            idx[hour_key] = {"file": str(out), "from": best["png"].name,
                             "score": best["score"], "bytes": size,
                             "at": time.time()}
            made += 1
            _say(f"  存下 {d:%m-%d %H} 点：{size/1024:.0f} KB"
                 f"（{best['score']} 字，来自 {best['png'].parent.name}）")
        else:
            failed += 1

    if made and not dry:
        save_index(idx)
    return {"made": made, "skipped": skipped, "failed": failed,
            "hours": len(groups), "bytes": saved_bytes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--once", action="store_true", help="只做一次（调试）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--loop", action="store_true", help="常驻，每 5 分钟一次")
    a = ap.parse_args()

    if not a.loop:
        _init_log()
        r = run_once(a.days, a.dry)
        _say(f"小时 {r['hours']} 个 → 新存 {r['made']} 张，跳过 {r['skipped']}，"
             f"失败 {r['failed']}，共 {r['bytes']/1024/1024:.1f} MB")
        return 0

    _init_log()
    _say("快照进程启动（每 %d 秒扫一次）" % SCAN_EVERY)
    # 为什么清理要挂在这里：
    #   boot_tasks 只在登录时清一次。如果这台机器连着开一星期，
    #   6 小时的截图保留期就永远不会再生效，缓存会涨到十几 GB。
    #   这个进程本来就是常驻的，顺手每 6 小时清一次，不用再加调度器。
    last_clean = 0.0
    while True:
        stop = vcfg.DATA / "STOP"
        if stop.exists():
            _say("检测到 STOP，退出")
            return 0
        try:
            r = run_once(days=2.0)
            if r["made"]:
                _say(f"新存 {r['made']} 张，{r['bytes']/1024/1024:.1f} MB")
        except Exception as e:
            _say(f"本轮出错：{e}")

        if time.time() - last_clean > CLEAN_EVERY:
            last_clean = time.time()
            try:
                _say("顺带清理一次磁盘")
                # 刻意用 python.exe 而不是 sys.executable：
                # 本进程是 pythonw 起的，它的 stdout 在重定向下不可靠，
                # 拿不到 vclean 的输出就等于瞎清。加 CREATE_NO_WINDOW 一样没窗口。
                py = vcfg.ROOT / ".venv" / "Scripts" / "python.exe"
                p = subprocess.run(
                    [str(py), str(vcfg.ROOT / "vclean.py"), "--apply"],
                    cwd=str(vcfg.ROOT), capture_output=True, timeout=1800,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                tail = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
                for ln in tail[-3:]:
                    _say("  " + ln.split("  ", 1)[-1])
            except Exception as e:
                _say(f"清理失败：{e}")

        for _ in range(SCAN_EVERY):
            if (vcfg.DATA / "STOP").exists():
                _say("检测到 STOP，退出")
                return 0
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
