"""冷启动自检 —— 在关机之前，先把"明天开机能不能起来"验掉。

为什么要有这个：
    这套东西全靠注册表 Run 键自启，一旦某个路径写错、某个 venv 被删、
    某个模块改了名，**明天开机会静静地什么都不发生**，而用户只会看到
    "总账没有更新"。这种失败最难受：不报错、不弹窗、无从察觉。

    所以关机器之前，把自己当成"刚登录的 Windows"验一遍。

三层检查：

    第一层 静态（不碰任何进程）
        注册表项在不在、指向的 exe 在不在、脚本在不在、模型在不在

    第二层 环境（不碰任何进程）
        cwd 换成 C:\\Windows\\System32、环境变量精简之后，
        脚本还能不能 import、能不能找到自己的数据目录

    第三层 冷启动（真的杀掉再拉起来）
        写 STOP → 等全部退出 → 用资源管理器按 Run 键的方式拉起 →
        数进程 → 再拉一次验证单实例锁

用法：
    python selftest.py             # 只做第一、二层（快，安全）
    python selftest.py --cold      # 加做第三层（会中断录音约 60 秒）
    python selftest.py --cold --digest   # 顺便验证总账能不能真生成
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

import vcfg

PY = vcfg.ROOT / ".venv" / "Scripts" / "python.exe"
PYW = vcfg.ROOT / ".venv" / "Scripts" / "pythonw.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STOP = vcfg.DATA / "STOP"
LOCK = vcfg.DATA / "daemon.lock"

OK, BAD, WARN = "✓", "✗", "!"
results: list[tuple[str, str, str]] = []      # (级别, 项目, 说明)


def rec(level: str, item: str, note: str = "") -> None:
    results.append((level, item, note))
    print(f"  {level} {item}" + (f"　{note}" if note else ""), flush=True)


def num_pythonw() -> int:
    """数一下 pythonw 进程。tasklist 的中文输出不碰，只取 csv 里的行数。"""
    try:
        p = subprocess.run(["tasklist", "/fi", "imagename eq pythonw.exe", "/fo", "csv"],
                           capture_output=True, timeout=20)
        txt = (p.stdout or b"").decode("gbk", "ignore")
        # 第一行是表头 "Image Name","PID",...
        return max(0, len([x for x in txt.splitlines() if x.strip().startswith('"')]) - 1)
    except Exception:
        return -1


def read_run_key() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                n, v, _ = winreg.EnumValue(k, i)
                out[n] = v
    except OSError:
        pass
    return out


def _exe_of(cmdline: str) -> Path | None:
    """从 Run 键的值里抠出可执行文件路径（可能是带引号的）。"""
    s = cmdline.strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        return Path(s[1:end]) if end > 0 else None
    first = s.split(" ")[0]
    return Path(first) if first else None


# ---------------------------------------------------------------- 第一层

def layer_static() -> None:
    print("\n【第一层】静态检查 —— 注册表、文件、模型")
    run = read_run_key()

    need = {"VoiceRecorder": "录音守护", "VoiceBootTasks": "开机收尾", "Ollama": "本地大模型"}
    for name, desc in need.items():
        if name not in run:
            if name == "Ollama":
                rec(WARN, f"自启项 {name}", f"{desc} 没注册（总账会退化成机器拼的句子）")
            else:
                rec(BAD, f"自启项 {name}", f"{desc} 缺失！明天开机不会启动")
            continue
        exe = _exe_of(run[name])
        if exe and exe.exists():
            rec(OK, f"自启项 {name}", f"{desc} → {exe.name}")
        else:
            rec(BAD, f"自启项 {name}", f"指向的文件不存在：{exe}")

    # Run 键里剩下的参数（脚本路径）也要在
    for name in ("VoiceRecorder", "VoiceBootTasks"):
        for part in run.get(name, "").split('"'):
            if part.strip().lower().endswith(".py"):
                p = Path(part.strip())
                rec(OK if p.exists() else BAD, f"{name} 的脚本", str(p.name))

    for p, what in [(PY, "venv python"), (PYW, "venv pythonw"),
                    (vcfg.ROOT / "start_voice.vbs", "启动脚本"),
                    (vcfg.ROOT / "stop_voice.vbs", "停止脚本"),
                    (vcfg.ROOT / "boot_tasks.py", "开机收尾"),
                    (vcfg.ROOT / "vclean.py", "清理器"),
                    (vcfg.ROOT / "vsnap.py", "快照进程"),
                    (vcfg.ROOT / "vocr.py", "OCR 清洗"),
                    (vcfg.ROOT / "vdigest.py", "总账生成")]:
        rec(OK if p.exists() else BAD, what, "" if p.exists() else f"缺 {p}")

    # 模型（目录结构是 models/models/<owner>--<name>/snapshots/master/）
    # 权重后缀不统一：SenseVoice 是 model.pt，CAM++ 是 campplus_cn_common.bin，
    # 所以不能只认 .pt，否则会误报。
    WEIGHT_EXT = ("*.pt", "*.bin", "*.onnx", "*.safetensors")
    for mid, what in [("iic--SenseVoiceSmall", "SenseVoice 转写"),
                      ("iic--speech_campplus_sv_zh-cn_16k-common", "CAM++ 声纹")]:
        d = vcfg.ROOT / "models" / "models" / mid
        weights = [f for pat in WEIGHT_EXT for f in d.rglob(pat)] if d.is_dir() else []
        rec(OK if weights else BAD, f"模型 {what}",
            (f"{weights[0].name} {weights[0].stat().st_size/1024/1024:.0f} MB"
             if weights else "缺权重文件，ASR 会去联网下载然后卡死"))

    # 声纹档案
    prof = vcfg.DATA / "speaker" / "profiles.json"
    rec(OK if prof.exists() else WARN, "声纹档案",
        "" if prof.exists() else "没有档案，所有人都会显示成「?」")


# ---------------------------------------------------------------- 第二层

def layer_env() -> None:
    """模拟"刚登录"的干净环境：cwd 在系统目录，环境变量只剩系统那几个。"""
    print("\n【第二层】环境检查 —— 换到 C:\\Windows\\System32 再跑一遍")

    probe = ("import sys; sys.path.insert(0, r'%s');"
             "import vcfg, vocr, vsnap, vclean, vdigest, boot_tasks, install_autostart;"
             "print(vcfg.ROOT); print(vcfg.DATA); print(len(vcfg.load()))" % vcfg.ROOT)
    env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
           "SystemDrive": os.environ.get("SystemDrive", "C:"),
           "USERPROFILE": os.environ.get("USERPROFILE", ""),
           "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
           "APPDATA": os.environ.get("APPDATA", ""),
           "TEMP": os.environ.get("TEMP", r"C:\Windows\Temp"),
           "PATH": os.environ.get("PATH", "")}
    try:
        p = subprocess.run([str(PY), "-c", probe], cwd=r"C:\Windows\System32",
                           capture_output=True, timeout=120, env=env)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        if p.returncode == 0 and str(vcfg.ROOT) in out:
            rec(OK, "干净环境导入全部模块")
            lines = out.splitlines()
            if len(lines) >= 3:
                rec(OK, "数据目录自解析", lines[1])
                rec(OK, "配置项数量", lines[2] + " 项")
        else:
            rec(BAD, "干净环境导入失败", (err or out).splitlines()[-1][:90])
    except Exception as e:
        rec(BAD, "干净环境导入异常", str(e)[:90])

    # Ollama
    try:
        import vdigest
        up = vdigest.ollama_up()
        rec(OK if up else WARN, "Ollama 可用",
            "" if up else "没起来或模型不在，总账会用机器拼的句子")
        if up:
            t0 = time.time()
            para = vdigest.ask_para("【时间】今天 09:00 开机 → 12:00（3 小时）\n"
                                    "【用过的软件】Word 120分钟\n"
                                    "【屏幕上出现过的文档】施工方案")
            dt_s = time.time() - t0
            if para:
                rec(OK, "模型能出段落", f"{len(para)} 字 / {dt_s:.1f} 秒")
            else:
                rec(BAD, "模型出不了段落", "ask_para 返回空")
    except Exception as e:
        rec(BAD, "模型调用异常", str(e)[:90])

    # Windrecorder 库
    dbs = list(Path("E:/AI/Windrecorder/userdata/db").glob("default_*_wind.db"))
    rec(OK if dbs else WARN, "Windrecorder 库",
        f"{len(dbs)} 个" if dbs else "没有库，屏幕侧取不到数据")


# ---------------------------------------------------------------- 第三层

def _wait(fn, want: bool, timeout: float, step: float = 2.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if (v == want) if isinstance(want, bool) else (v == want):
            return True
        time.sleep(step)
    return False


def layer_cold(digest: bool) -> None:
    print("\n【第三层】冷启动 —— 真的杀掉再按开机的方式拉起来")

    base = num_pythonw()
    rec(OK if base >= 0 else BAD, "当前进程数", f"{base} 个")
    if base < 0:
        return

    # 1. 优雅停止
    print("  … 写 STOP，等全部退出（最多 40 秒）", flush=True)
    STOP.parent.mkdir(parents=True, exist_ok=True)
    try:
        STOP.write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        rec(BAD, "写不了 STOP 文件", str(e))
        return
    gone = _wait(num_pythonw, 0, 40)
    if gone:
        rec(OK, "优雅停止", "全部进程已退出")
    else:
        rec(BAD, "停不干净", f"还剩 {num_pythonw()} 个")
        return

    # 2. 清 STOP，模拟干净的开机状态
    try:
        STOP.unlink()
    except OSError:
        pass
    rec(OK if not STOP.exists() else BAD, "STOP 已清理")

    # 3. 按"开机"的方式拉起 —— 走资源管理器，进程才不会被工具回收
    vbs = vcfg.ROOT / "start_voice.vbs"
    print("  … 用资源管理器拉起 start_voice.vbs", flush=True)
    subprocess.run(["explorer.exe", str(vbs)], timeout=30)

    def up() -> int:
        return num_pythonw()

    end = time.time() + 70
    n = 0
    while time.time() < end:
        n = up()
        if n >= 8:
            break
        time.sleep(2)
    if n >= 8:
        rec(OK, "冷启动成功", f"{n} 个 pythonw 起来了")
    else:
        rec(BAD, "冷启动失败", f"只起来了 {n} 个，应该有 8 个")
        return

    # 4. 锁文件
    if LOCK.exists():
        rec(OK, "单实例锁", LOCK.read_text(encoding="utf-8").strip())
    else:
        rec(BAD, "单实例锁没写", "说明守护进程没走到加锁那一步")

    # 5. 再拉一次，验证不会录出双份
    print("  … 再拉一次，验证单实例锁", flush=True)
    subprocess.run(["explorer.exe", str(vbs)], timeout=30)
    time.sleep(12)
    n2 = num_pythonw()
    if n2 == n:
        rec(OK, "重复启动被挡住", f"仍是 {n2} 个")
    else:
        rec(BAD, "重复启动产生了额外进程", f"{n} → {n2} 个")

    # 6. 录音真在写
    logf = vcfg.LOG_DIR / "recorder.out.log"
    if logf.exists():
        age = time.time() - logf.stat().st_mtime
        rec(OK if age < 300 else WARN, "录音日志在更新",
            f"{age/60:.0f} 分钟前写过")
    # 7. 快照进程
    snaplog = vcfg.LOG_DIR / "snap.log"
    rec(OK if snaplog.exists() else WARN, "快照进程日志",
        "" if snaplog.exists() else "还没写过（正常，5 分钟内会出现）")

    # 8. 总账能不能真生成
    if digest:
        print("  … 验证总账能生成（不写总账，只看过程）", flush=True)
        p = subprocess.run([str(PY), "vdigest.py", "--llm", "--no-ledger"],
                           cwd=str(vcfg.ROOT), capture_output=True, timeout=300)
        out = (p.stdout or b"").decode("utf-8", "replace")
        if "模型写了" in out:
            line = [x for x in out.splitlines() if "模型写了" in x][0]
            rec(OK, "总账生成链路", line.strip())
        else:
            rec(WARN, "总账生成链路", "没看到模型输出，退回程序拼的句子")


# ---------------------------------------------------------------- 汇总

def summary() -> int:
    bad = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    print("\n" + "=" * 56)
    print(f"检查 {len(results)} 项：通过 {len(results)-len(bad)-len(warn)}，"
          f"警告 {len(warn)}，失败 {len(bad)}")
    if bad:
        print("\n必须修好，否则明天开机会失效：")
        for _, item, note in bad:
            print(f"  ✗ {item}　{note}")
    if warn:
        print("\n能用，但会打折：")
        for _, item, note in warn:
            print(f"  ! {item}　{note}")
    if not bad:
        print("\n明天开机会自动：启动录音 → 90 秒后补写上次总账 → 清磁盘")
    rep = vcfg.LOG_DIR / "selftest.log"
    try:
        rep.parent.mkdir(parents=True, exist_ok=True)
        with rep.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            for lv, item, note in results:
                f.write(f"{lv} {item}  {note}\n")
            f.write(f"失败 {len(bad)} / 警告 {len(warn)}\n")
    except Exception:
        pass
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold", action="store_true", help="真杀进程做冷启动测试")
    ap.add_argument("--digest", action="store_true", help="顺便验证总账生成")
    a = ap.parse_args()

    print("=" * 56)
    print("冷启动自检　" + time.strftime("%Y-%m-%d %H:%M"))
    print("=" * 56)
    layer_static()
    layer_env()
    if a.cold:
        layer_cold(a.digest)
    else:
        print("\n（没加 --cold，跳过真杀进程那一步）")
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
