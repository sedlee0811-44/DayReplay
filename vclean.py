"""磁盘清理 —— 只删"中间产物"，不删"结论"。

分级保留策略（为什么这么分）：

    语音转写 / 声纹 / 总账   → **永久保留**（几十 KB 量级，是结论）
    OCR 文字（Windrecorder） → **永久保留**（在它的库里，几 MB）
    每小时快照小图           → 永久（一天 5 MB，比原图省 500 倍）
    原始录音 wav             → 7 天（一份转写重跑成本低，原件没必要留）
    Windrecorder 截图缓存    → **6 小时**（最大的黑洞：单张 4K PNG 19~20 MB，
                               合成视频 + OCR 之后就纯属垃圾，实测一天 2.7 GB）
    Windrecorder 录像        → 30 天（能回放画面，比截图有用）

三道安全阀（缺一个都可能删掉不该删的）：

    1. **OCR 已入库才删** —— 删之前查 Windrecorder 的 `video_text`，确认
       这个时间点的截图已经被 OCR 过、文字已经落了库。否则不删。
       （文字没落库的时候删图 = 这段屏幕活动永久消失）
    2. **超过 2 天无条件删** —— 万一 OCR 管线坏了，"文字没入库"会成为
       永不删除的理由，磁盘又会被塞满。所以给个硬兜底。
    3. **最近 2 小时不动** —— 可能正在被录制进程写。

    另外：删之前先调 vsnap 补做每小时快照，保证删掉的原图里
    已经挑出了代表帧。顺序不能反。

安全设计：
    - **默认 dry-run**，只打印不删；确认无误再加 --apply
    - 只删文件，绝不动 voice.db（删了音频，转写和声纹仍然完整）
    - 删除前统计总量，删完再统计一次

用法：
    python vclean.py               # 试运行，看看会删什么
    python vclean.py --apply       # 真删
    python vclean.py --apply --audio-days 3 --shot-days 0.25
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import shutil
import sqlite3
import time
from pathlib import Path

import vcfg

_WIND = Path((vcfg.load().get("windrecorder_dir") or "").strip()
             or "E:/AI/Windrecorder")
SHOT_DIR = _WIND / "cache_screenshot"
VIDEO_DIR = _WIND / "userdata/videos"
WIND_DB_DIR = _WIND / "userdata/db"
LOG = vcfg.LOG_DIR / "clean.log"

# 最近这么多小时内产生的文件一律不动（可能正在被写）
RECENT_GUARD_H = 2.0

# 超过这个天数无条件删，不管 OCR 有没有入库（防止 OCR 坏掉后磁盘无限涨）
FORCE_DAYS = 2.0


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def _mb(n: int) -> str:
    return f"{n/1024/1024:.1f} MB" if n < 1024**3 else f"{n/1024**3:.2f} GB"


def _old_enough(p: Path, cutoff: float, guard: float) -> bool:
    """按 mtime 判断，同时保证不碰刚刚写过的文件。"""
    try:
        m = p.stat().st_mtime
    except OSError:
        return False
    return m < cutoff and (time.time() - m) > guard * 3600


# ------------------------------------------------------- 安全阀：OCR 已入库？

def _ocr_stamps() -> list[float]:
    """Windrecorder 库里所有"有文字"的时刻（已换算成真 UTC），升序。

    videofile_time 用的是 timegm(localtime(ts))，比真 UTC 多 28800 秒。
    """
    out: list[float] = []
    if not WIND_DB_DIR.is_dir():
        return out
    for db in sorted(WIND_DB_DIR.glob("default_*_wind.db")):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for (t,) in conn.execute(
                    "SELECT videofile_time FROM video_text "
                    "WHERE ocr_text IS NOT NULL AND length(ocr_text) > 10"):
                out.append(float(t) - 28800)
            conn.close()
        except Exception:
            continue
    out.sort()
    return out


def _dir_time(name: str) -> float | None:
    """会话目录名就是时间戳，如 2026-10-24_15-16-50。"""
    try:
        return time.mktime(time.strptime(name[:19], "%Y-%m-%d_%H-%M-%S"))
    except Exception:
        return None


def _covered(ts: float, stamps: list[float], window: float = 600.0) -> bool:
    """这个时刻附近（正负 10 分钟）有没有被 OCR 过。"""
    if not stamps:
        return False
    i = bisect.bisect_left(stamps, ts - window)
    return i < len(stamps) and stamps[i] <= ts + window


def _snap_hours() -> set[str]:
    """已经有代表图的小时，形如 {"2026-10-24 15", ...}。"""
    p = vcfg.DATA / "snapshots" / "index.json"
    if not p.exists():
        return set()
    try:
        import json
        return set(json.loads(p.read_text(encoding="utf-8")).keys())
    except Exception:
        return set()


def _sweep_dir(root: Path, cutoff: float, guard: float,
               dry: bool, label: str,
               stamps: list[float] | None = None,
               force_before: float | None = None,
               snap_hours: set[str] | None = None) -> tuple[int, int]:
    """按目录整体清理（截图缓存的每个子目录是一次截图会话）。

    两把锁 + 一个兜底：
        stamps      给了就要求"该时刻 OCR 已入库"才删（文字不能丢）
        snap_hours  给了就要求"该小时已有代表图"才删（画面不能全丢）
        force_before 超过这个时间无条件删（防止上面两把锁把磁盘锁死）
    """
    if not root.is_dir():
        return 0, 0
    freed = count = 0
    held_ocr = held_snap = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not _old_enough(d, cutoff, guard):
            continue
        ts = _dir_time(d.name)
        forced = force_before is not None and ts is not None and ts < force_before
        if not forced:
            if stamps is not None and (ts is None or not _covered(ts, stamps)):
                held_ocr += 1
                continue
            if snap_hours is not None:
                h = time.strftime("%Y-%m-%d %H", time.localtime(ts))
                if h not in snap_hours:
                    held_snap += 1
                    continue
        sz = _size(d)
        freed += sz
        count += 1
        _log(f"  [{label}] {'将删除' if dry else '删除'} {d.name}  {_mb(sz)}")
        if not dry:
            shutil.rmtree(d, ignore_errors=True)
    if held_ocr:
        _log(f"  [{label}] 另有 {held_ocr} 个目录因为 OCR 还没入库，先留着")
    if held_snap:
        _log(f"  [{label}] 另有 {held_snap} 个目录因为所属小时还没存代表图，先留着")
    return freed, count


def _sweep_files(root: Path, cutoff: float, guard: float,
                 dry: bool, label: str) -> tuple[int, int]:
    """按文件清理（录音）。删空目录但保留目录结构。"""
    if not root.is_dir():
        return 0, 0
    freed = count = 0
    for f in root.rglob("*.wav"):
        if not _old_enough(f, cutoff, guard):
            continue
        sz = _size(f)
        freed += sz
        count += 1
        if count <= 20 or count % 50 == 0:
            _log(f"  [{label}] {'将删除' if dry else '删除'} {f.relative_to(root)}  {_mb(sz)}")
        if not dry:
            try:
                f.unlink()
            except OSError:
                pass
    if not dry:
        for d in sorted(root.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    return freed, count


def _snapshot_first(days: float, dry: bool) -> None:
    """删之前先把每小时的代表图挑出来，顺序不能反。"""
    try:
        import vsnap
        r = vsnap.run_once(days=max(days, 1.0), dry=dry)
        _log(f"  快照：{r['hours']} 个小时里有 {r['made']} 个新存下"
             f"（{r['bytes']/1024/1024:.1f} MB），已有 {r['skipped']} 个")
    except Exception as e:
        _log(f"  快照失败（不影响清理）：{e}")


def _free_gb(p: Path) -> float:
    """所在磁盘的剩余空间（GB）。"""
    try:
        return shutil.disk_usage(str(p)).free / (1024 ** 3)
    except Exception:
        return 9999.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认只看）")
    ap.add_argument("--audio-days", type=float, default=0.0,
                    help="录音保留天数；0=永久（默认）。注意：文字永远永久，这里只管 wav")
    ap.add_argument("--audio-free-gb", type=float, default=50.0,
                    help="仅当磁盘剩余低于这么多 GB 时才清老录音（默认 50）；0=永不因空间清")
    ap.add_argument("--shot-days", type=float, default=0.25,
                    help="截图缓存保留天数（默认 6 小时；OCR 文字已在库里，原图没用）")
    ap.add_argument("--video-days", type=float, default=30.0)
    ap.add_argument("--guard-hours", type=float, default=RECENT_GUARD_H)
    ap.add_argument("--force-days", type=float, default=FORCE_DAYS,
                    help="超过这么多天无条件删，不管 OCR 有没有入库")
    a = ap.parse_args()

    dry = not a.apply
    guard = a.guard_hours
    now = time.time()
    t_shot = now - a.shot_days * 86400
    t_video = now - a.video_days * 86400
    t_force = now - a.force_days * 86400

    # 录音默认永久保留。
    # 2026-10-24 用户要求："有人说话的声音就保存下来，识别出来的文字永久保留。"
    # 文字（voice.db 里的 asr.text）本来就永久，没人删；这里管的是 wav 音频。
    # 之前 --audio-days 默认 7 天，而 vsnap 每 6 小时跑一次 `vclean.py --apply`
    # 不带参数 —— 等于录完 7 天就自动消失，用户完全不知道。已改默认 0＝永久。
    # 唯一例外：磁盘真的快满了（剩余 < audio_free_gb）才按 90 天前的先清，
    # 这是防止把盘撑爆的兜底，不是日常策略。
    t_audio = now - a.audio_days * 86400
    sweep_audio = True
    if a.audio_days <= 0:
        free = _free_gb(vcfg.AUDIO_DIR)
        if a.audio_free_gb > 0 and free < a.audio_free_gb:
            t_audio = now - 90 * 86400
            sweep_audio = True
            _log(f"！磁盘只剩 {free:.0f} GB，低于警戒线 {a.audio_free_gb:g} GB，"
                 f"开始清理 90 天前的录音")
        else:
            sweep_audio = False

    _log("=" * 56)
    audio_desc = f">{a.audio_days:g}天" if sweep_audio else "永久保留"
    _log(f"清理开始（{'试运行，不删任何东西' if dry else '实际删除'}）"
         f"　音频{audio_desc}　截图>{a.shot_days:g}天　录像>{a.video_days:g}天")

    before_a = _size(vcfg.AUDIO_DIR)
    before_s = _size(SHOT_DIR)
    before_v = _size(VIDEO_DIR)

    # 1. 先把代表图挑出来（要删的时间范围都要覆盖到）
    _snapshot_first(a.shot_days, dry)

    # 2. 查两把锁的条件
    stamps = _ocr_stamps()
    snaps = _snap_hours()
    _log(f"  Windrecorder 已 OCR 的记录 {len(stamps)} 条　"
         f"已有代表图的小时 {len(snaps)} 个")

    # 3. 删
    if sweep_audio:
        f1, c1 = _sweep_files(vcfg.AUDIO_DIR, t_audio, guard, dry, "录音")
    else:
        f1 = c1 = 0
        _log(f"  录音：{_mb(before_a)}，永久保留，本次不动")
    f2, c2 = _sweep_dir(SHOT_DIR, t_shot, guard, dry, "截图",
                        stamps=stamps, force_before=t_force, snap_hours=snaps)
    f3, c3 = _sweep_dir(VIDEO_DIR, t_video, guard, dry, "录像")

    total = f1 + f2 + f3
    _log(f"合计：录音 {c1} 个 / 截图 {c2} 个会话 / 录像 {c3} 个，"
         f"{'可回收' if dry else '已回收'} {_mb(total)}")
    if not dry:
        _log(f"占用变化：录音 {_mb(before_a)}→{_mb(_size(vcfg.AUDIO_DIR))}　"
             f"截图 {_mb(before_s)}→{_mb(_size(SHOT_DIR))}　"
             f"录像 {_mb(before_v)}→{_mb(_size(VIDEO_DIR))}")
    else:
        _log("（试运行结束。确认无误后加 --apply 才会真删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
