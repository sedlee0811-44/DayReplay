"""语音链路守护进程：守住 recorder 与 asr 两个子进程。

为什么要守护而不是注册两个任务：
    - 麦克风被拔、驱动重启、模型 OOM 都会让子进程死掉，
      守护进程能自动拉起，用户永远不用管
    - 只有一个入口，停止也只需写一个 STOP 文件

停止约定：
    写 data/STOP 文件 → 两个子进程各自收尾（封 WAV、提交事务）后退出。
    之所以用文件而不是信号：用 pythonw 跑时没有控制台，
    GenerateConsoleCtrlEvent 送不进去，SIGTERM 又被 TerminateProcess 短路。

用法：
    pythonw voice_daemon.py       # 开机自启（无窗口）
    python  voice_daemon.py       # 前台调试
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import vcfg
import vupdate

log = logging.getLogger("daemon")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

CHILDREN = [
    ("recorder", ["voice_recorder.py"]),
    ("asr", ["voice_asr.py"]),
    # 每小时存一张屏幕小图。放在守护进程底下是因为它必须常驻：
    # 截图原图 6 小时后就删了，错过了就再也挑不出代表帧。
    ("snap", ["vsnap.py", "--loop"]),
]

LOCK = vcfg.DATA / "daemon.lock"
# 进程退出时由内核自动释放，别手动 close
MUTEX = vcfg.acquire_mutex("voice_daemon")


def _pid_alive(pid: int) -> bool:
    """pid 还活着，而且那个进程确实是 python。

    为什么要查进程名：Windows 会复用 PID。只比对 PID，重启后可能撞上
    一个无关的进程，于是守护进程以为"已经有人在跑"而不启动，录音就断了。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
                return False                     # 259 = STILL_ACTIVE
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                # 查不到名字就当作"不是我们的"：宁可多开一次（会被下面的
                # 麦克风占用挡一下），也不要因为 PID 复用而误判成"已经在跑"
                # 从而让录音静默断掉 —— 后者用户完全无从察觉。
                return False
            return "python" in buf.value.lower()
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def already_running() -> bool:
    """两道都通过才算没人跑。

    第一道是内核级命名互斥（vcfg.acquire_mutex），它抹掉了"两人同时启动"
    这个竞态窗口 —— 2026-10-25 双开事故就出在那儿：两边的锁文件检查都在
    对方写盘之前读到了旧内容，于是都判定没人跑。
    第二道是原来的锁文件 + pid，互为备份：理论上 mutex 不会失效，但万一
    哪台机器上 ctypes 不可用，还有文件这道不至于裸奔。
    """
    if MUTEX is None:
        return True          # 互斥没抢到 → 有人比我先到
    return not _lock_stale()  # 抢到了，再看文件是不是也说自己可以跑


def _lock_stale() -> bool:
    """锁文件的心跳凉了吗 ——  mutex 的第二道备份。

    凉了（>300s 没 touch、pid 对不上 python）就说明没有别人的守护进程在跑。
    """
    try:
        age = time.time() - LOCK.stat().st_mtime
        pid = int((LOCK.read_text(encoding="utf-8") or "0").strip())
    except Exception:
        return True
    if age > 300 or pid <= 0:
        return True
    return not _pid_alive(pid)


class Child:
    def __init__(self, name: str, args: list[str]):
        self.name = name
        self.args = args
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.rapid = 0
        self.logf = None

    def start(self):
        out = vcfg.LOG_DIR / f"{self.name}.out.log"
        self.logf = open(out, "a", encoding="utf-8", buffering=1)
        self.logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start =====\n")
        # 子进程的 stdout 被重定向到 utf-8 文件，但它默认按控制台代码页(GBK)
        # 编码，中文日志落盘就成了乱码。强制它也用 utf-8。
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", *self.args],
            cwd=str(vcfg.ROOT),
            stdout=self.logf, stderr=subprocess.STDOUT,
            env=env,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        self.started_at = time.time()
        log.info("启动 %s (pid=%d)", self.name, self.proc.pid)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace: float = 15.0):
        if not self.alive():
            return
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            log.warning("%s 未在 %.0fs 内退出，强制结束", self.name, grace)
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _updater_thread() -> None:
    """后台检查更新。绝不阻塞主循环，也绝不在本进程内执行更新。

    为什么不放在主循环里：检查更新要 stat 几千个文件（GB 级目录），
    跑在主循环里会让心跳迟滞；自动更新更是要停进程、换文件、跑冒烟，
    动辄几十秒。这些都放到独立子进程里去干，本线程只负责"发现"。

    自动更新的让位逻辑：派生一个独立的 vupdate apply 进程，然后写
    STOP 让自己退场。新代码由 vupdate 更新完成后重新拉起 —— 旧进程
    必须先彻底退出，否则单实例互斥锁还攥在旧代码手里，新实例起不来。
    """
    cfg = vcfg.load()
    if not (cfg.get("update_source") or "").strip():
        return
    time.sleep(60)                       # 开机一分钟后再查，别和启动抢资源
    interval = max(1, int(cfg.get("update_check_hours", 24) or 24)) * 3600
    while True:
        try:
            r = vupdate.check(cfg, quiet=True)
            if r.get("has_update"):
                if (cfg.get("auto_update") or "check") == "auto":
                    log.info("发现新版本 %s，转入自动更新后让位", r["remote"])
                    pyw = vcfg.ROOT / "python" / "pythonw.exe"
                    if not pyw.exists():
                        pyw = vcfg.ROOT / ".venv" / "Scripts" / "pythonw.exe"
                    try:
                        subprocess.Popen(
                            [str(pyw), "vupdate.py", "apply", "--auto"],
                            cwd=str(vcfg.ROOT),
                            creationflags=CREATE_NO_WINDOW)
                    except Exception as e:
                        log.error("派发更新进程失败：%s", e)
                        return
                    # 写 STOP 让主循环收尾退出；vupdate 会等锁释放再动手
                    try:
                        (vcfg.DATA / "STOP").write_text(
                            str(time.time()), encoding="utf-8")
                    except OSError:
                        pass
                    return
                log.info("发现新版本 %s（双击桌面「检查更新」安装）",
                         r["remote"])
        except Exception as e:
            log.debug("更新检查失败：%s", e)
        time.sleep(interval)


def main():
    vcfg.ensure_dirs()
    vcfg.setup_logging("daemon.log", level=logging.INFO)

    # 先救援再查锁：上次更新中途断电的话，VERSION.json 会停在
    # state=testing，此时文件系统可能半新半旧。必须赶在启动录音链路
    # 之前回滚干净。放在写锁文件之前，回滚里的 stop_daemon 才不会
    # 把我们自己的心跳当成"别人在跑"而死等。
    try:
        vupdate.auto_rescue()
    except Exception as e:
        log.warning("更新救援检查失败：%s", e)

    # 单实例保护：开机自启 + 用户手动双击，两条路都可能触发。
    # 没有这道锁，两个守护进程会各自开一路麦克风，录出双份入库。
    if already_running():
        log.info("已有守护进程在跑（%s），本次不再启动", LOCK)
        return
    try:
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as e:
        log.warning("锁文件写不了：%s", e)

    stop_file = vcfg.DATA / "STOP"
    if stop_file.exists():
        stop_file.unlink()
        log.info("清除了上次遗留的 STOP 文件")

    children = [Child(n, a) for n, a in CHILDREN]
    for c in children:
        c.start()

    # 后台更新检查（守护线程，主循环退出时随之而逝）
    import threading
    threading.Thread(target=_updater_thread, name="updater",
                     daemon=True).start()

    stopping = {"flag": False}

    def _sig(signum, _frame):
        log.info("收到信号 %s", signum)
        stopping["flag"] = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        s = getattr(signal, name, None)
        if s is not None:
            try:
                signal.signal(s, _sig)
            except (OSError, ValueError):
                pass

    try:
        while not stopping["flag"]:
            if stop_file.exists():
                log.info("检测到 STOP 文件，开始收尾")
                break
            try:
                LOCK.touch()          # 心跳：告诉后来的实例"我还活着"
            except OSError:
                pass
            for c in children:
                if c.alive():
                    continue
                code = c.proc.returncode if c.proc else None
                # 刚起就死 = 启动失败，退避；稳定运行过则立即重启
                if time.time() - c.started_at < 20:
                    c.rapid += 1
                else:
                    c.rapid = 0
                delay = min(60, 2 ** c.rapid) if c.rapid else 1
                log.error("%s 退出 (code=%s)，%.0fs 后重启（第 %d 次快速失败）",
                          c.name, code, delay, c.rapid)
                time.sleep(delay)
                if stopping["flag"] or stop_file.exists():
                    break
                c.start()
            time.sleep(2.0)
    finally:
        # 让子进程自己收尾：写 STOP 文件
        stop_file.write_text(str(time.time()), encoding="utf-8")
        log.info("等待子进程收尾…")
        for c in children:
            c.stop(15.0)
        for c in children:
            if c.logf:
                c.logf.close()
        try:
            LOCK.unlink()
        except OSError:
            pass
        log.info("全部退出")


if __name__ == "__main__":
    main()
