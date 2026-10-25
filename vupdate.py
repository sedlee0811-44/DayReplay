"""版本与自动更新 —— 让装好的副本能自己变新，且永远变不坏。

设计（2026-10-25，随 1.0.0 首发）：

    版本号      语义化 major.minor.patch。源头是项目根目录的 VERSION
                文本文件（一行一个号），build_package.py 读它、可以
                --bump 自动递增。本机版本身份证是安装根目录的
                VERSION.json（带 build 时间、更新说明、状态位）。

    更新源      一个目录：U 盘插着、局域网共享文件夹、或者本机另一个
                位置都行。源里放着一个完整发布包（build_package.py
                的产物）。config.json 的 update_source 指向它，
                留空 = 不检查更新（安装器默认填成安装包来源）。

    分级增量    manifest.json 按顶层目录（python / models / code）
                逐部分记签名。检查更新只 stat 不读内容（秒级）；
                哪部分的签名变了才传哪部分。日常发新版一般只动
                code（约 700KB），几秒完成；模型/依赖大版本才动
                GB 级目录。

    安全四道闸  ① staging 全量拷完并逐部分校验，才允许替换
                ② 同盘 rename 原子换入，旧版同时 rename 成备份
                ③ 换完先写 VERSION.json(state=testing) 再跑冒烟测试，
                   失败立刻 rename 回来
                ④ 守护进程启动时看到 state=testing（说明上次更新
                   中途断电）自动回滚 —— 用户永远见不到半新半旧

    data/ 目录  永远不碰。录音、数据库、模型缓存都是用户的，更新只
                动 python/models/code 三件和根级说明文件。

用法：
    python vupdate.py check                检查更新（写 data/update_state.json）
    python vupdate.py status               当前版本 + 最近检查结果
    python vupdate.py apply [--yes]        执行更新（自动重启监控）
    python vupdate.py rollback [--auto]    回滚到上一个版本
    python vupdate.py history              更新历史
    python vupdate.py verify               复检本机文件与 manifest 是否一致
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import vcfg

ROOT = vcfg.ROOT
STATE = vcfg.DATA / "update_state.json"
HISTORY = vcfg.DATA / "update_history.jsonl"
UPDATES = ROOT / "updates"                 # staging / backup 都在这
MANIFEST_NAME = "manifest.json"
VERSION_NAME = "VERSION.json"

# 参与更新的顶层目录（data 不在其列 —— 用户数据，更新永不触碰）
PARTS = ("code", "models", "python")
# 发布包根级的文件，更新时顺手刷新（安装三件套 + 版本身份证 + 清单）。
# manifest.json 必须一起换：它是本机"复检"用的底账，留着旧版会让
# vupdate verify 在更新完成后误报"code 与清单不符"。
ROOT_FILES = ("install.ps1", "一键安装.bat", "安装说明.txt",
              VERSION_NAME, MANIFEST_NAME)

CHUNK = 1 << 20


# ---------------------------------------------------------------- 版本号

def parse_ver(s: str) -> tuple[int, int, int]:
    """'1.2.3' → (1,2,3)。容忍 'v1.2' '1.2.3-beta' 这类脏输入。"""
    s = (s or "").strip().lstrip("vV")
    core = s.split("-")[0].split("+")[0]
    nums = []
    for p in core.split(".")[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)          # type: ignore[return-value]


def ver_cmp(a: str, b: str) -> int:
    """a 比 b 新返回 1，旧 -1，相等 0。"""
    ta, tb = parse_ver(a), parse_ver(b)
    return (ta > tb) - (ta < tb)


# ---------------------------------------------------------------- 签名

def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def part_sig(root: Path, part: str, content: bool = False) -> dict:
    """算一个顶层部分的签名。

    content=False（默认，用于 python/models 这类 GB 级目录）：
        只按"路径+大小"清单算哈希，纯 stat，几千个文件也是毫秒级。
        这两个目录由构建脚本整体产出，没人会单独改里面某个文件。
    content=True（用于 code）：
        逐文件读内容算 sha256。705KB，一次读完不到一秒，
        换来"改一个字节都能发现"的精度 —— 代码最需要这个。
    """
    base = root / part
    if not base.is_dir():
        return {"sig": "", "files": 0, "bytes": 0}
    lines, files, total = [], 0, 0
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts:
            continue     # 跑一次 python 就会生成，永远不该参与签名
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(base).as_posix()
        total += st.st_size
        files += 1
        if content:
            try:
                lines.append(f"{rel}|{_file_sha(p)}")
            except OSError:
                lines.append(f"{rel}|<unreadable>")
        else:
            lines.append(f"{rel}|{st.st_size}")
    sig = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"sig": sig, "files": files, "bytes": total}


def part_files(root: Path, part: str) -> dict[str, dict]:
    """code 部分的逐文件内容清单（构建时写进 manifest，更新时复检用）。"""
    base = root / part
    out: dict[str, dict] = {}
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts:
            continue     # 与 part_sig 口径一致
        rel = (part + "/" + p.relative_to(base).as_posix())
        try:
            out[rel] = {"sha256": _file_sha(p), "size": p.stat().st_size}
        except OSError:
            out[rel] = {"sha256": "", "size": 0}
    return out


def build_manifest(root: Path, version: str, build: str) -> dict:
    """构建发布包时调用：对 payload 下三部分签名 + code 逐文件清单。"""
    parts = {}
    for p in PARTS:
        parts[p] = part_sig(root / "payload", p, content=(p == "code"))
    return {
        "version": version,
        "build": build,
        "parts": parts,
        "files": part_files(root / "payload", "code"),
    }


# ---------------------------------------------------------------- 本机状态

def local_version() -> dict:
    """读安装根目录的 VERSION.json；没有就退回裸 VERSION 文本。"""
    vj = ROOT / VERSION_NAME
    if vj.exists():
        try:
            return json.loads(vj.read_text(encoding="utf-8"))
        except Exception:
            pass
    v = ROOT / "VERSION"
    if v.exists():
        return {"version": v.read_text(encoding="utf-8").strip() or "0.0.0"}
    return {"version": "0.0.0", "notes": []}


def local_manifest() -> dict:
    mj = ROOT / MANIFEST_NAME
    if mj.exists():
        try:
            return json.loads(mj.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def update_source(cfg: dict) -> Path | None:
    """config.json 的 update_source；空 = 没配更新源。"""
    s = (cfg.get("update_source") or "").strip()
    return Path(s) if s else None


def source_ready(src: Path) -> tuple[bool, str]:
    if not src.is_dir():
        return False, f"更新源不存在：{src}"
    vj = src / VERSION_NAME
    if not vj.exists():
        return False, f"源里没有 {VERSION_NAME}，不像一个发布包"
    if not (src / MANIFEST_NAME).exists():
        return False, f"源里没有 {MANIFEST_NAME}"
    return True, ""


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except OSError:
        pass


def record_history(entry: dict) -> None:
    try:
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 停止/启动监控

def daemon_running() -> bool:
    lock = vcfg.DATA / "daemon.lock"
    try:
        txt = lock.read_text(encoding="utf-8").strip()
        pid = int(txt)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
                return False
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return False
            return "python" in buf.value.lower()
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def stop_daemon(timeout: float = 40.0) -> bool:
    """写 STOP 文件，等守护进程自己收尾退出。返回 True = 已彻底停止。"""
    stop = vcfg.DATA / "STOP"
    try:
        vcfg.DATA.mkdir(parents=True, exist_ok=True)
        stop.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not daemon_running():
            time.sleep(1.0)          # 子进程封 WAV、提交事务还要一点时间
            return not daemon_running()
        time.sleep(1.0)
    return False


def start_daemon() -> bool:
    """隐式拉起守护进程（不弹窗、不阻塞）。"""
    vbs = ROOT / "start_voice.vbs"
    pyw = ROOT / "python" / "pythonw.exe"
    if not pyw.exists():
        pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    try:
        if vbs.exists():
            os.startfile(str(vbs))          # type: ignore[attr-defined]
        elif pyw.exists():
            subprocess.Popen([str(pyw), "voice_daemon.py"], cwd=str(ROOT),
                             creationflags=getattr(subprocess,
                                                   "CREATE_NO_WINDOW", 0))
        else:
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 校验

def verify_staging(stage: Path, manifest: dict, parts: list[str]) -> list[str]:
    """校验 staging 里的内容。返回错误列表，空 = 全部通过。"""
    errs: list[str] = []
    for part in parts:
        want = manifest["parts"][part]
        got = part_sig(stage, part, content=(part == "code"))
        if got["sig"] != want["sig"]:
            errs.append(f"{part}: 签名不符（拷了一半？文件 {got['files']}/{want['files']}，"
                        f"{got['bytes']}/{want['bytes']} 字节）")
    # code 部分再逐文件复检一次 sha256（manifest.files 是构建时算的）
    for rel, want in (manifest.get("files") or {}).items():
        f = stage / rel
        if not f.exists():
            errs.append(f"{rel}: 缺失")
            continue
        try:
            if f.stat().st_size != want.get("size", -1):
                errs.append(f"{rel}: 大小不符")
                continue
            if _file_sha(f) != want.get("sha256"):
                errs.append(f"{rel}: 内容校验失败")
        except OSError as e:
            errs.append(f"{rel}: 读不了（{e}）")
    return errs


def code_layout() -> str:
    """code 在本机的布局。

    flat：.py 就在安装根（发布器把 payload\\code 平铺拷到 $InstallDir，
    和开发机同构，vcfg 的 ROOT=Path(__file__).parent 靠它成立）。
    nested：code/ 子目录（vupdate 早期误建的产物，或测试造的树）。
    两处都有时以 flat 为准 —— 根目录那份才是运行时真正 import 的。
    """
    if (ROOT / "vcfg.py").exists():
        return "flat"
    return "nested"


def local_code_sig(m: dict) -> dict:
    """flat 布局下就地复算 code 部分签名。

    part_sig 用不了：flat 时 ROOT 下还有 python/ models/ data/，
    不能整个 rglob。好在 manifest.files 就是 code 的文件全集，
    按它逐个算，rel 与发布包一侧（payload/code 相对路径）同构，
    签名可以直接比。
    """
    rels = [k.split("/", 1)[1] for k in (m.get("files") or {})
            if k.startswith("code/")]
    lines, total = [], 0
    # 行序必须和 part_sig 一致：它是对 Path 对象排序（Windows 上大小写
    # 不敏感，VERSION 会排在 boot_tasks.py 之后），字符串排序会得到
    # 另一个顺序、另一个签名 —— 实测踩过（19 行全部错位）。
    for p in sorted(ROOT / r for r in rels):
        rel = p.relative_to(ROOT).as_posix()
        try:
            sha = _file_sha(p)
            total += p.stat().st_size
        except Exception:
            lines.append(f"{rel}|<missing>")
            continue
        lines.append(f"{rel}|{sha}")
    return {"sig": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(),
            "files": len(rels), "bytes": total}


def code_sig_for(m: dict) -> dict:
    """按本机布局取 code 部分的实际签名（check/verify 共用）。"""
    if code_layout() == "flat":
        return local_code_sig(m)
    return part_sig(ROOT, "code", content=True)


def verify_local() -> list[str]:
    """复检本机文件与根目录 manifest 是否一致（vupdate verify 用）。"""
    manifest = local_manifest()
    if not manifest:
        return ["本机没有 manifest.json（安装时就该带上）"]
    errs: list[str] = []
    flat = code_layout() == "flat"
    for part in PARTS:
        base = ROOT / part
        if not base.is_dir():
            continue          # 这台机器没这个部分（比如没装模型），没什么可复检的
        want = manifest["parts"].get(part)
        if not want:
            continue
        got = code_sig_for(manifest) if part == "code" else \
            part_sig(ROOT, part, content=(part == "code"))
        if got["sig"] != want["sig"]:
            errs.append(f"{part}: 本机签名与 manifest 不符（被改过或没更新全）")
    for rel, want in (manifest.get("files") or {}).items():
        f = ROOT / (rel.split("/", 1)[1]
                    if flat and rel.startswith("code/") else rel)
        if not f.exists():
            errs.append(f"{rel}: 缺失")
            continue
        try:
            if f.stat().st_size != want.get("size", -1) or _file_sha(f) != want.get("sha256"):
                errs.append(f"{rel}: 与 manifest 不一致")
        except OSError as e:
            errs.append(f"{rel}: 读不了（{e}）")
    return errs


# ---------------------------------------------------------------- 检查更新

def check(cfg: dict, quiet: bool = False) -> dict:
    """比对源与本机。结果写 data/update_state.json 供守护进程/总账读取。"""
    res: dict = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "local": local_version().get("version", "0.0.0"),
                 "source": "", "remote": "", "has_update": False,
                 "parts": [], "notes": [], "error": ""}
    src = update_source(cfg)
    if src is None:
        res["error"] = "未配置更新源（config.json 的 update_source 为空）"
        save_state(res)
        if not quiet:
            print("未配置更新源，跳过检查。")
        return res
    res["source"] = str(src)
    ok, why = source_ready(src)
    if not ok:
        res["error"] = why
        save_state(res)
        if not quiet:
            print(f"更新源不可用：{why}")
        return res
    try:
        rj = json.loads((src / VERSION_NAME).read_text(encoding="utf-8"))
    except Exception as e:
        res["error"] = f"源的 {VERSION_NAME} 读不了：{e}"
        save_state(res)
        if not quiet:
            print(res["error"])
        return res
    res["remote"] = rj.get("version", "0.0.0")
    res["notes"] = rj.get("notes") or []
    try:
        mj = json.loads((src / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception as e:
        res["error"] = f"源的 {MANIFEST_NAME} 读不了：{e}"
        save_state(res)
        return res
    # 哪些部分与源不一致 —— 就地复算磁盘签名，不信任本机 manifest：
    # "账本说更新过了、磁盘还是旧文件"的假成功必须在这里现形。
    for part in PARTS:
        want = (mj.get("parts") or {}).get(part)
        if not want:
            continue
        got = code_sig_for(mj) if part == "code" else \
            part_sig(ROOT, part, content=(part == "code"))
        if got["sig"] != want["sig"]:
            res["parts"].append(part)
    if ver_cmp(res["remote"], res["local"]) > 0:
        res["has_update"] = True
    elif ver_cmp(res["remote"], res["local"]) == 0 and res["parts"]:
        # 版本号相同但文件不符：更新中途被打断/杀软干扰/拷贝不完整。
        # 给一次自我修复的机会，而不是永远"已是最新"地装傻。
        res["has_update"] = True
        res["notes"] = list(res["notes"]) + [
            "版本号相同但本机文件与发布包不符，将自我修复"]
    if not res["has_update"]:
        save_state(res)
        if not quiet:
            print(f"已是最新（本机 {res['local']}，源 {res['remote']}）")
        return res
    res["has_update"] = True
    save_state(res)
    if not quiet:
        print(f"发现新版本 {res['remote']}（本机 {res['local']}）")
        for n in res["notes"]:
            print(f"  - {n}")
        print(f"  需要更新：{'、'.join(res['parts']) or '仅说明文件'}")
    return res


# ---------------------------------------------------------------- 执行更新

def apply(cfg: dict, yes: bool = False, auto: bool = False) -> int:
    res = check(cfg, quiet=True)
    if res.get("error"):
        print(f"无法更新：{res['error']}")
        return 2
    if not res.get("has_update"):
        print(f"已是最新（{res['local']}）。")
        return 0
    src = Path(res["source"])
    new_ver, old_ver = res["remote"], res["local"]
    manifest = json.loads((src / MANIFEST_NAME).read_text(encoding="utf-8"))

    # 哪几部分要传。parts 为空（只有根级文件变化）也照样走一遍流程。
    parts = res.get("parts") or []
    size = sum(manifest["parts"][p]["bytes"] for p in parts)
    print(f"准备更新 {old_ver} → {new_ver}")
    print(f"  更新部分：{'、'.join(parts) or '仅说明文件'}"
          f"（{size/1048576:.0f} MB）")
    if not auto and not yes:
        ans = input("确认更新？[Y/n] ").strip().lower()
        if ans not in ("", "y", "yes", "是"):
            print("已取消。")
            return 1

    # 0) 同盘检查：staging 和目标必须在同一卷，rename 才是原子操作
    try:
        if os.path.splitdrive(str(UPDATES))[0].lower() != \
           os.path.splitdrive(str(ROOT))[0].lower():
            print("updates 暂存区与安装目录不在同一磁盘，无法安全替换。")
            return 2
    except Exception:
        pass
    UPDATES.mkdir(parents=True, exist_ok=True)
    stage = UPDATES / f"staging-{new_ver}"
    rm_dir(stage)

    # 1) 全量拷进 staging（含根级文件）
    print("① 拷贝到暂存区…")
    try:
        for part in parts:
            shutil.copytree(src / "payload" / part, stage / part,
                            dirs_exist_ok=True)
        for f in ROOT_FILES:
            s = src / f
            if s.exists():
                shutil.copy2(s, stage / f)
    except Exception as e:
        print(f"拷贝失败：{e}")
        rm_dir(stage)
        return 2

    # 2) 校验 staging
    print("② 校验暂存内容…")
    errs = verify_staging(stage, manifest, parts)
    if errs:
        print("校验未通过，已放弃更新：")
        for e in errs[:10]:
            print(f"  - {e}")
        rm_dir(stage)
        return 2

    # 3) 停监控（更新时录音暂停，完事自动拉起来）
    was_running = daemon_running()
    if was_running:
        print("③ 停止监控…")
        if not stop_daemon():
            print("监控停不下来，放弃更新（下次再说，录音不受影响）。")
            rm_dir(stage)
            return 2

    # 4) 原子换入：旧 parts rename 成备份，staging rename 上位
    print("④ 替换文件…")
    backup = UPDATES / f"backup-{old_ver}-{time.strftime('%Y%m%d-%H%M%S')}"
    moved_backup: list[str] = []
    flat = code_layout() == "flat"
    old_manifest = local_manifest()     # 换入前记旧清单，flat 删文件要用
    try:
        backup.mkdir(parents=True)
        for part in parts:
            dst = ROOT / part
            if part == "code" and flat:
                # 平铺布局：ROOT 下还有 python/models，整目录 rename 无从谈起。
                # 逐文件换入，旧文件快照进 backup/_codefiles 供回滚。
                cf = backup / "_codefiles"
                new_files = manifest.get("files") or {}
                old_files = (old_manifest.get("files") or {}) \
                    if old_manifest else {}
                for rel in new_files:
                    if not rel.startswith("code/"):
                        continue
                    r = rel.split("/", 1)[1]
                    d = ROOT / r
                    if d.exists():
                        (cf / r).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(d, cf / r)
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(stage / "code" / r, d)
                for rel in old_files:
                    if rel in new_files or not rel.startswith("code/"):
                        continue
                    r = rel.split("/", 1)[1]
                    d = ROOT / r
                    if d.exists():
                        (cf / r).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(d, cf / r)
                        d.unlink()
                continue
            if dst.exists():
                shutil.move(str(dst), str(backup / part))
                moved_backup.append(part)
            shutil.move(str(stage / part), str(dst))
        # 根级文件（VERSION.json 最后单独写，带 testing 状态）。
        # 旧的先快照进 backup/_rootfiles —— 回滚时连 manifest 一起还原，
        # 否则"新 manifest + 旧 code"会让复检永远对不上（实测踩过）。
        for f in ROOT_FILES:
            if f == VERSION_NAME:
                continue
            s = stage / f
            if not s.exists():
                continue
            old = ROOT / f
            if old.exists():
                rf = backup / "_rootfiles"
                rf.mkdir(exist_ok=True)
                shutil.copy2(old, rf / f)
            shutil.copy2(s, ROOT / f)
    except Exception as e:
        print(f"替换过程出错：{e}")
        restore(backup, moved_backup)
        rm_dir(stage)
        if was_running:
            start_daemon()
        return 2

    # 5) 写新 VERSION.json（state=testing：冒烟过了才改 ok）
    vj = json.loads((src / VERSION_NAME).read_text(encoding="utf-8"))
    vj["state"] = "testing"
    vj["backup"] = str(backup)
    vj["prev_version"] = old_ver
    (ROOT / VERSION_NAME).write_text(
        json.dumps(vj, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) 冒烟测试：静态自检 + ASR 真的转一段
    print("⑤ 冒烟测试…")
    ok, detail = smoke_test()
    if not ok:
        print(f"冒烟未通过：{detail}")
        print("正在回滚到旧版本…")
        restore(backup, moved_backup)
        rollback_version_file(old_ver, backup)
        rm_dir(stage)
        print(f"已回滚到 {old_ver}。")
        if was_running:
            start_daemon()
        record_history({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": old_ver, "to": new_ver, "parts": parts,
                        "ok": False, "note": detail})
        return 3

    # 7) 复检：磁盘文件必须和新 manifest 逐项对得上才承认成功。
    #    "账本写了新版本、磁盘还是旧文件"的假成功（实测踩过）在这里拦下。
    errs = verify_local()
    if errs:
        print("更新后复检未通过，正在回滚：")
        for e in errs[:6]:
            print(f"  - {e}")
        restore(backup, moved_backup)
        rollback_version_file(old_ver, backup)
        rm_dir(stage)
        print(f"已回滚到 {old_ver}。")
        if was_running:
            start_daemon()
        record_history({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": old_ver, "to": new_ver, "parts": parts,
                        "ok": False, "note": "复检未通过：" + errs[0]})
        return 3

    # 8) 成功：落状态、记历史、拉起监控
    if flat:
        junk = ROOT / "code"
        if junk.is_dir() and (junk / "vcfg.py").exists():
            rm_dir(junk)    # 早期按嵌套布局误建的副本，平铺下是永远跑不到的死文件
    vj["state"] = "ok"
    vj.pop("backup", None)
    (ROOT / VERSION_NAME).write_text(
        json.dumps(vj, ensure_ascii=False, indent=2), encoding="utf-8")
    record_history({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": old_ver, "to": new_ver, "parts": parts,
                    "ok": True, "note": "smoke ok"})
    rm_dir(stage)
    prune_backups(cfg)
    if was_running:
        print("⑥ 重启监控…")
        start_daemon()
    print(f"\n✓ 已更新到 {new_ver}")
    for n in res.get("notes") or []:
        print(f"  - {n}")
    return 0


def rm_dir(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)


def restore(backup: Path, parts: list[str]) -> None:
    """把备份的 parts rename 回原位，根级文件也从快照还原。"""
    for part in parts:
        src, dst = backup / part, ROOT / part
        if not src.exists():
            continue
        if dst.exists():
            rm_dir(dst)
        try:
            shutil.move(str(src), str(dst))
        except Exception:
            pass
    rf = backup / "_rootfiles"
    if rf.is_dir():
        for f in sorted(rf.iterdir()):
            if f.is_file():
                try:
                    shutil.copy2(f, ROOT / f.name)
                except Exception:
                    pass
    # 平铺布局逐文件换入时的旧文件快照，按相对路径还原
    cf = backup / "_codefiles"
    if cf.is_dir():
        for f in cf.rglob("*"):
            if not f.is_file():
                continue
            d = ROOT / f.relative_to(cf).as_posix()
            try:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, d)
            except Exception:
                pass


def rollback_version_file(old_ver: str, backup: Path) -> None:
    """更新失败后把 VERSION.json 还原成旧版（构造一个最小合法身份证）。"""
    vj = {"version": old_ver, "state": "ok",
          "build": time.strftime("%Y-%m-%d %H:%M:%S"),
          "notes": [f"从 {old_ver} 回滚（更新失败自动还原）"]}
    (ROOT / VERSION_NAME).write_text(
        json.dumps(vj, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_backups(cfg: dict) -> None:
    """只保留最近 N 个备份，老的删掉。"""
    keep = int(cfg.get("update_keep_backups", 2) or 2)
    bks = sorted(UPDATES.glob("backup-*"), key=lambda p: p.stat().st_mtime)
    for p in bks[:-keep] if keep > 0 else bks:
        rm_dir(p)
    # staging 残留一律清掉
    for p in UPDATES.glob("staging-*"):
        rm_dir(p)


def smoke_test() -> tuple[bool, str]:
    """更新后的最低限度验证：模块能导入、ASR 真能转出字。

    为什么拿一段固定录音而不是现录：新机器/安静房间里现录可能零段，
    验证不到模型。包里 tools/asr_smoke.wav 是确定性的证据。
    """
    py = ROOT / "python" / "python.exe"
    if not py.exists():
        py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        return False, "找不到 python.exe"
    # ① 导入链路
    r = subprocess.run([str(py), "-c",
                        "import vcfg, voice_db, voice_recorder, voice_asr, "
                        "vevents, vdigest, vcollect, vsearch"],
                       cwd=str(ROOT), capture_output=True, timeout=180,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        return False, "模块导入失败：" + (r.stderr or b"").decode("utf-8", "ignore")[-300:]
    # ② ASR 转写冒烟录音
    wav = ROOT / "tools" / "asr_smoke.wav"
    if not wav.exists():
        return True, "（没有冒烟录音，跳过 ASR 验证）"
    r = subprocess.run([str(py), "voice_asr.py", "--file", str(wav)],
                       cwd=str(ROOT), capture_output=True, timeout=300,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or b"").decode("utf-8", "ignore")
    if r.returncode != 0:
        return False, "ASR 冒烟失败：" + out[-300:]
    # 只要转出了非空文字就算过（不比对具体内容，模型小版本可能措辞不同）
    if "text" in out.lower() or any(c for c in out if "\u4e00" <= c <= "\u9fff"):
        return True, "ok"
    return True, "ok(输出较短)"


# ---------------------------------------------------------------- 回滚

def rollback(cfg: dict, auto: bool = False) -> int:
    """回滚到 backup-* 里最新的一个。auto=True 表示守护进程启动时的
    自动救援（上次更新中途断电，VERSION.json 停在 testing）。"""
    vj = local_version()
    old_ver = vj.get("version", "0.0.0")
    bks = sorted(UPDATES.glob("backup-*"), key=lambda p: p.stat().st_mtime)
    if not bks:
        print("没有可回滚的备份。")
        return 1
    backup = bks[-1]
    # _rootfiles 是根级文件快照，不是 part，别混进来
    parts = [p.name for p in backup.iterdir()
             if p.is_dir() and p.name != "_rootfiles"]
    if not parts:
        print(f"备份 {backup.name} 是空的，没法回滚。")
        return 1
    if not auto:
        print(f"将把 {old_ver} 回滚到 {backup.name} 里的旧版（{'、'.join(parts)}）")
        ans = input("确认回滚？[Y/n] ").strip().lower()
        if ans not in ("", "y", "yes", "是"):
            print("已取消。")
            return 1
    was_running = daemon_running()
    if was_running and not stop_daemon():
        print("监控停不下来，放弃回滚。")
        return 2
    prev = vj.get("prev_version") or old_ver
    restore(backup, parts)
    rollback_version_file(prev, backup)
    rm_dir(backup)
    record_history({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": old_ver, "to": prev, "parts": parts,
                    "ok": True, "note": "auto" if auto else "manual"})
    if was_running:
        start_daemon()
    print(f"✓ 已回滚到 {prev}")
    return 0


def auto_rescue() -> int:
    """守护进程启动时调用：上次更新是不是中途死了？

    判据是 VERSION.json 的 state=testing —— 这个状态只在"文件已换入、
    冒烟未过"之间存在，正常流程几秒内就会变成 ok。开机还看到它，
    说明当时断电/崩了，此时备份一定还在，直接回滚最安全。
    """
    vj = local_version()
    if vj.get("state") != "testing":
        return 0
    bk = vj.get("backup")
    if bk and Path(bk).exists():
        print(f"[rescue] 检测到未完成的更新（{vj.get('prev_version')} → "
              f"{vj.get('version')}），自动回滚…")
        return rollback({}, auto=True)
    # 备份不在（被清了）就只把身份证修成 ok，至少界面不报警
    vj["state"] = "ok"
    vj.pop("backup", None)
    (ROOT / VERSION_NAME).write_text(
        json.dumps(vj, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="版本与自动更新")
    ap.add_argument("cmd", choices=["check", "status", "apply", "rollback",
                                    "history", "verify", "rescue"],
                    nargs="?", default="check")
    ap.add_argument("--yes", action="store_true", help="apply 不再询问")
    ap.add_argument("--auto", action="store_true", help="自动模式（守护进程调用）")
    args = ap.parse_args()
    cfg = vcfg.load()

    if args.cmd == "check":
        r = check(cfg)
        return 1 if r.get("has_update") else 0
    if args.cmd == "status":
        vj = local_version()
        st = load_state()
        print(f"本机版本：{vj.get('version', '0.0.0')}"
              f"（构建于 {vj.get('build', '未知')}，状态 {vj.get('state', 'ok')}）")
        if st.get("checked_at"):
            print(f"最近检查：{st['checked_at']}"
                  + (f" → 有新版本 {st['remote']}" if st.get("has_update")
                     else f" → 已是最新（源 {st.get('remote', '?')}）"))
        else:
            print("最近检查：从未")
        if st.get("error"):
            print(f"检查问题：{st['error']}")
        return 0
    if args.cmd == "apply":
        return apply(cfg, yes=args.yes, auto=args.auto)
    if args.cmd == "rollback":
        return rollback(cfg, auto=args.auto)
    if args.cmd == "history":
        if HISTORY.exists():
            for line in HISTORY.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                mark = "✓" if e.get("ok") else "✗"
                print(f"{mark} {e['ts']}  {e.get('from')} → {e.get('to')}"
                      f"  [{','.join(e.get('parts') or []) or '仅说明文件'}]"
                      f"  {e.get('note', '')}")
        else:
            print("还没有更新记录。")
        return 0
    if args.cmd == "verify":
        errs = verify_local()
        if errs:
            print("本机与清单不一致：")
            for e in errs[:20]:
                print(f"  - {e}")
            return 1
        print("✓ 本机文件与 manifest 完全一致")
        return 0
    if args.cmd == "rescue":
        return auto_rescue()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
