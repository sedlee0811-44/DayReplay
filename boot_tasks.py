"""开机收尾 —— 每次登录后在后台自动干两件事，无窗口、无感。

为什么要等一会儿：
    刚登录时一堆程序在抢磁盘（安全软件、输入法、Steam、各种自更新），
    这时候去扫描十万个文件会互相拖死。等 90 秒，等它们消停。

为什么总结的是"上一次"：
    摘要里要写"几点关机"。关机的时候程序已经没了，写不出来。
    所以只能下次开机补 —— 那时上一次的关机时间已经在事件日志里了。

为什么要检查"未总结"：
    万一这次开机没跑成（比如断电），下次开机还会重试那次会话，
    不会漏掉，也不会重复写两遍。

用法：
    pythonw boot_tasks.py                 # 正常（开机自启走这个）
    python boot_tasks.py --delay 0        # 立刻跑一遍，看效果
    python boot_tasks.py --no-clean       # 只总结不清理
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

import vcfg

LOG = vcfg.LOG_DIR / "boot.log"
PY = Path(sys.executable)


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    # pythonw 下没有 stdout，只有从控制台跑时才打
    if sys.stdout is not None:
        print(line, flush=True)


def wait_ollama(max_wait: float = 60.0) -> bool:
    """等本地大模型醒过来。

    为什么必须等：登录 90 秒时 Ollama 那个托盘进程可能刚起来、模型还没
    加载进显存。这时候去要段落会拿到空，只能退回程序拼的干巴句子 ——
    用户第二天看到的就不是"一段话"而是"本次开机共 3 小时，主要在 Edge
    里操作"。等于白折腾。

    等不到就自己拉一把（Ollama 自带单实例，重复启动只是把窗口叫出来）。
    """
    try:
        import vdigest
    except Exception as e:
        log(f"  vdigest 导入失败，跳过模型等待：{e}")
        return False

    def ok() -> bool:
        try:
            return vdigest.ollama_up()
        except Exception:
            return False

    if ok():
        log("  本地模型已就绪")
        return True

    end = time.time() + max_wait
    while time.time() < end:
        if (vcfg.DATA / "STOP").exists():
            return False
        time.sleep(5)
        if ok():
            log("  本地模型就绪（等了 %.0f 秒）" % (time.time() - (end - max_wait)))
            return True

    # 还没醒 —— 自己拉起来，再等一轮
    app = Path.home() / "AppData/Local/Programs/Ollama/ollama app.exe"
    if app.exists():
        log("  模型没起来，尝试启动 Ollama")
        try:
            subprocess.Popen([str(app)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log(f"  启动失败：{e}")
            return False
        end = time.time() + 90
        while time.time() < end:
            time.sleep(5)
            if ok():
                log("  拉起后模型就绪")
                return True
    log("  模型始终没起来，这次用程序拼的句子")
    return False


def run(script: str, *args: str, timeout: float = 600.0) -> int:
    """跑一个子脚本，输出并进日志。"""
    cmd = [str(PY), str(vcfg.ROOT / script), *args]
    log(f"  → {' '.join([script, *args])}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        p = subprocess.run(cmd, cwd=str(vcfg.ROOT), capture_output=True,
                           timeout=timeout, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        for ln in (out + err).strip().splitlines()[-40:]:
            log("    " + ln)
        log(f"    退出码 {p.returncode}")
        return p.returncode
    except subprocess.TimeoutExpired:
        log(f"    超时（>{timeout:.0f}s），跳过")
        return -1
    except Exception as e:
        log(f"    起不来：{e}")
        return -2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=90.0, help="先等多少秒")
    ap.add_argument("--no-clean", action="store_true")
    ap.add_argument("--no-digest", action="store_true")
    a = ap.parse_args()

    if a.delay > 0:
        time.sleep(a.delay)

    log("=" * 50)
    log("开机收尾开始")

    # ---- 1. 补写上一次开机的总账 ----
    if not a.no_digest:
        try:
            import vsession as vss
            pending = vss.unresolved()
        except Exception as e:
            log(f"  会话查询失败：{e}")
            pending = None
        if pending:
            log(f"  有未总结的会话：{vss.describe(pending)}")
            # 先确保模型能用。用不上就明确传 --no-llm，省得 vdigest
            # 内部再等一次超时。
            model_ok = wait_ollama()
            args = ["--prev"] if model_ok else ["--prev", "--no-llm"]
            run("vdigest.py", *args, timeout=900)
        else:
            log("  没有待总结的会话（上次已写过，或没记录到数据）")

    # ---- 2. 清磁盘 ----
    if not a.no_clean:
        run("vclean.py", "--apply", timeout=1800)

    log("开机收尾结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
