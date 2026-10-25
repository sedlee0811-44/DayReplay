"""总账实时刷新 —— 让网页上的内容停在此刻，而不是停在上一次关机。

为什么单独做一个进程，而不是塞进 vdigest：
    vdigest 是一次性工具：跑完就退出。要"实时"就得有个到点自动重跑的
    节拍器。这个节拍器必须满足三个条件，所以只能单独活一个进程：
      1. 能被 data/STOP 一句话停掉（和录音链路同一个停止约定）
      2. 单实例（用户点两下别跑出两套来回抢总账）
      3. 自己绝不算任何东西 —— 只负责叫 vdigest，崩了也伤不到录音

为什么间隔是 5 分钟：
    采集端（截屏每小时一张、录音逐段、转写逐句）本来就是实时的，
    这里补的只是"汇总"的延迟。5 分钟够用：人不会盯着秒看，而更频繁
    地重算只会让风扇一直响 —— 每次都要读写磁盘、问一次本地模型。

为什么第一轮要等 3 分钟：
    开机后 90 秒 boot_tasks 正在补写上一次开机的总账（最多几百秒）。
    两边同时写同一份文件没意义，等它收工再来。反正 vdigest 自己有锁，
    真撞上也只会跳过一轮，不会写坏。

用法：
    pythonw vlive.py               # 开机自启，静默
    python  vlive.py --once        # 立刻刷新一轮，看效果
    python  vlive.py --every 600   # 改成 10 分钟一轮
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time

import vcfg

LOG = vcfg.LOG_DIR / "live.log"
STOP = vcfg.DATA / "STOP"
LOCK = vcfg.DATA / "live.lock"
PY = sys.executable
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if sys.stdout is not None:
        print(line, flush=True)


def grab_lock():
    """单实例，两道保险。

    第一道是内核级命名互斥（vcfg.acquire_mutex）：能挡住"同时按下两次"这种
    竞态 —— 两边都在对方写盘之前读到旧状态，于是都判定没人跑。
    2026-10-25 凌晨就是这么跑出两套完整链路的。
    第二道是原来的文件锁，跟着进程走，被杀掉自动释放，不会留死锁。
    """
    if vcfg.acquire_mutex("vlive") is None:
        return None
    import msvcrt
    try:
        f = open(LOCK, "a+b")
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        return None


def run_once(every: int, timeout: float = 420.0) -> int:
    """叫一次 vdigest --live。返回退出码，日志里留尾巴。"""
    cmd = [str(PY), str(vcfg.ROOT / "vdigest.py"), "--live", "--every", str(every)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(vcfg.ROOT), capture_output=True,
                           timeout=timeout, env=env,
                           creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        tail = [ln for ln in (out + err).strip().splitlines() if ln.strip()]
        for ln in tail[-6:]:
            log("    " + ln)
        log(f"    退出码 {p.returncode}　耗时 {time.time() - t:.0f}s")
        return p.returncode
    except subprocess.TimeoutExpired:
        log(f"    超时（>{timeout:.0f}s），放弃这一轮")
        return -1
    except Exception as e:
        log(f"    起不来：{e}")
        return -2


def sleep_until(seconds: float) -> bool:
    """边睡边看 STOP。返回 False 表示该收工了。"""
    end = time.time() + seconds
    while time.time() < end:
        if STOP.exists():
            return False
        time.sleep(min(5.0, max(0.2, end - time.time())))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=300, help="多少秒刷新一轮")
    ap.add_argument("--delay", type=float, default=180.0, help="首轮前先等多少秒")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    a = ap.parse_args()

    vcfg.ensure_dirs()
    if STOP.exists():
        # 和录音守护进程同一秒启动时，STOP 可能还没被它清掉。等一下再判，
        # 否则会误以为自己该收工，刚开完机就退出，页面又停在上一次关机。
        for _ in range(30):
            if not STOP.exists():
                break
            time.sleep(1)
        if STOP.exists():
            return 0

    if a.once:
        return 0 if run_once(a.every) == 0 else 1

    lock = grab_lock()
    if not lock:
        log("已经有一个实时刷新在跑了，这个不再启动")
        return 0

    log("=" * 46)
    log(f"实时刷新启动　每 {a.every} 秒一轮")

    if not sleep_until(a.delay):
        log("收到停止信号")
        return 0

    while True:
        if STOP.exists():
            log("收到停止信号，退出")
            break
        run_once(a.every)
        if not sleep_until(a.every):
            log("收到停止信号，退出")
            break

    log("实时刷新结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
