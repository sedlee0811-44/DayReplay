"""vupdate 版本与更新链路的单元/流程测试。

用临时目录搭一个"迷你安装树 + 迷你发布包"，把 vupdate 的路径常量
整体重定向过去，跑完整的 check → apply → 回滚 → 救援 → 复检流程。
守护进程探测和冒烟测试用替身换掉，测的是更新逻辑本身。

    python test_update.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import vcfg
import vupdate

FAILS: list[str] = []


def chk(name: str, cond: bool, note: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f"　{note}" if note else ""))
    if not cond:
        FAILS.append(name)


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def make_src(root: Path, version: str, extra_code: str = "") -> Path:
    """造一个发布包（VERSION.json + manifest.json + payload/code）。"""
    src = root / f"src-{version}"
    code = src / "payload" / "code"
    write(code / "vcfg.py", f"# version {version}{extra_code}\n")
    write(code / "app.py", f"print('{version}')\n")
    # 大写文件名：part_sig 按 Windows Path 排序（大小写不敏感），
    # 字符串排序会得到不同行序 —— 专防这个坑（2026-10-25 实测踩过）
    write(code / "NOTES.md", f"notes {version}\n")
    (src / "VERSION.json").write_text(json.dumps({
        "version": version, "build": "2026-10-25 10:00:00",
        "state": "ok", "notes": [f"说明 {version}", "第二条说明"],
    }, ensure_ascii=False), encoding="utf-8")
    # manifest 直接用 vupdate 自己的算法生成（构建侧同算法，档案 #48 的教训）
    m = vupdate.build_manifest(src, version, "2026-10-25 10:00:00")
    (src / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return src


def make_inst(root: Path, version: str, flat: bool = False) -> Path:
    """造一个已安装的树（code/VERSION/VERSION.json/manifest.json/config.json）。

    manifest 按"安装树布局"手工拼（part_sig + part_files 直接指向
    inst/code）：发布包里的 manifest 是 payload/ 布局算的，安装时原样
    拷过来 —— 两侧内容一致所以签名一致，这正是分级更新的比较基础。
    flat=True 造平铺布局（.py 在安装根，真实安装器就是这样铺的）。
    """
    inst = root / (f"inst-{version}" + ("-flat" if flat else ""))
    code = inst / "code"
    if flat:
        write(inst / "vcfg.py", f"# version {version}\n")
        write(inst / "app.py", f"print('{version}')\n")
        write(inst / "NOTES.md", f"notes {version}\n")
        files = {}
        for r in ("vcfg.py", "app.py", "NOTES.md"):
            p = inst / r
            files[f"code/{r}"] = {"sha256": vupdate._file_sha(p),
                                  "size": p.stat().st_size}
        m = {"version": version, "parts": {}, "files": files}
        m["parts"]["code"] = vupdate.local_code_sig(m)
    else:
        write(code / "vcfg.py", f"# version {version}\n")
        write(code / "app.py", f"print('{version}')\n")
        write(code / "NOTES.md", f"notes {version}\n")
        m = {"version": version,
             "parts": {"code": vupdate.part_sig(inst, "code", content=True)},
             "files": vupdate.part_files(inst, "code")}
    (inst / "VERSION.json").write_text(json.dumps({
        "version": version, "state": "ok",
    }, ensure_ascii=False), encoding="utf-8")
    (inst / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False), encoding="utf-8")
    write(inst / "config.json", json.dumps(
        {"update_source": "", "auto_update": "check"}, ensure_ascii=False))
    return inst


def redirect(inst: Path, src: Path) -> None:
    """把 vupdate/vcfg 的路径常量指向测试树。"""
    vcfg.ROOT = inst
    vcfg.DATA = inst / "data"
    (inst / "data").mkdir(parents=True, exist_ok=True)
    vupdate.ROOT = inst
    vupdate.STATE = inst / "data" / "update_state.json"
    vupdate.HISTORY = inst / "data" / "update_history.jsonl"
    vupdate.UPDATES = inst / "updates"
    # 守护进程不在跑；冒烟测试可二选一
    vupdate.daemon_running = lambda: False
    vupdate.start_daemon = lambda: True
    vupdate.stop_daemon = lambda timeout=40.0: True


def set_smoke(ok: bool) -> None:
    vupdate.smoke_test = (lambda: (True, "ok")) if ok else \
        (lambda: (False, "故意失败"))


def cfg_of(inst: Path) -> dict:
    return json.loads((inst / "config.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 用例

def t_ver() -> None:
    print("== 版本号比较 ==")
    chk("1.0.1 > 1.0.0", vupdate.ver_cmp("1.0.1", "1.0.0") > 0)
    chk("1.1.0 > 1.0.9", vupdate.ver_cmp("1.1.0", "1.0.9") > 0)
    chk("2.0.0 > 1.9.9", vupdate.ver_cmp("2.0.0", "1.9.9") > 0)
    chk("相等 = 0", vupdate.ver_cmp("1.0.0", "1.0.0") == 0)
    chk("旧版 < 新版", vupdate.ver_cmp("1.0.0", "1.0.1") < 0)
    chk("容忍 v 前缀", vupdate.ver_cmp("v1.2.0", "1.1.9") > 0)
    chk("容忍两位", vupdate.ver_cmp("1.2", "1.1.9") > 0)
    chk("容忍后缀", vupdate.ver_cmp("1.2.0-beta", "1.1.9") > 0)


def t_check() -> None:
    print("== 检查更新 ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        inst = make_inst(tmp, "1.0.0")
        src = make_src(tmp, "1.1.0")
        redirect(inst, src)
        cfg = cfg_of(inst)
        cfg["update_source"] = str(src)
        r = vupdate.check(cfg, quiet=True)
        chk("发现 1.1.0", r["has_update"] and r["remote"] == "1.1.0")
        chk("识别出 code 要更新", "code" in r["parts"], str(r["parts"]))
        chk("带上更新说明", r["notes"] == ["说明 1.1.0", "第二条说明"])

        # 源同版本 → 不更新
        src0 = make_src(tmp, "1.0.0")
        cfg["update_source"] = str(src0)
        r = vupdate.check(cfg, quiet=True)
        chk("同版本不提示更新", not r["has_update"])

        # 同版本但文件被改坏 → 走自我修复（2026-10-25 假成功事故的回归用例）
        write(inst / "code" / "vcfg.py", "# corrupted\n")
        r = vupdate.check(cfg, quiet=True)
        chk("同版本文件损坏仍提示修复",
            r["has_update"] and r["remote"] == "1.0.0",
            f"has_update={r['has_update']} remote={r['remote']}")
        chk("修复标记 code", "code" in r["parts"], str(r["parts"]))
        set_smoke(True)
        rc = vupdate.apply(cfg, yes=True)
        chk("自我修复返回 0", rc == 0, f"exit={rc}")
        chk("文件被修回原样",
            (inst / "code" / "vcfg.py").read_text(encoding="utf-8")
            == "# version 1.0.0\n")
        h = (inst / "data" / "update_history.jsonl").read_text(
            encoding="utf-8")
        chk("修复记入历史", '"ok": true' in h, h.strip()[-120:])

        # 源更旧 → 不更新（不许降级）
        srcd = make_src(tmp, "0.9.0")
        cfg["update_source"] = str(srcd)
        r = vupdate.check(cfg, quiet=True)
        chk("更旧的源不提示", not r["has_update"])

        # 没源 → 安静返回
        cfg["update_source"] = ""
        r = vupdate.check(cfg, quiet=True)
        chk("没配更新源不炸", r["error"].startswith("未配置"))

        # 源不是发布包
        cfg["update_source"] = str(tmp / "nonexist")
        r = vupdate.check(cfg, quiet=True)
        chk("源不存在有错误信息", "不存在" in r["error"])

        # 已装新版 + 源旧 → has_update False
        redirect(make_inst(tmp, "2.0.0"), src)
        cfg = cfg_of(tmp / "inst-2.0.0")
        cfg["update_source"] = str(src)
        r = vupdate.check(cfg, quiet=True)
        chk("本机更新时不降级", not r["has_update"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_flat() -> None:
    print("== 平铺布局（真实安装器的铺法） ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        set_smoke(True)
        inst = make_inst(tmp, "1.0.0", flat=True)
        src = make_src(tmp, "1.1.0")
        redirect(inst, src)
        cfg = cfg_of(inst)
        cfg["update_source"] = str(src)
        r = vupdate.check(cfg, quiet=True)
        chk("平铺也认出要更新 code", r["has_update"] and "code" in r["parts"],
            str(r["parts"]))
        # 源里多一个文件、少一个旧文件，验证逐文件换入 + 删除
        write(src / "payload" / "code" / "newmod.py", "# new\n")
        (src / "payload" / "code" / "app.py").unlink()
        m = vupdate.build_manifest(src, "1.1.0", "b")
        (src / "manifest.json").write_text(
            json.dumps(m, ensure_ascii=False), encoding="utf-8")
        rc = vupdate.apply(cfg, yes=True)
        chk("平铺更新返回 0", rc == 0, f"exit={rc}")
        chk("新文件落到安装根",
            (inst / "newmod.py").exists())
        chk("根目录文件被替换",
            (inst / "vcfg.py").read_text(encoding="utf-8")
            == "# version 1.1.0\n")
        chk("新版没有的旧文件被删", not (inst / "app.py").exists())
        chk("复检通过", vupdate.verify_local() == [],
            str(vupdate.verify_local()[:2]))
        chk("没有嵌套 code/ 残骸", not (inst / "code" / "vcfg.py").exists())
        chk("VERSION.json 是 1.1.0",
            vupdate.local_version()["version"] == "1.1.0")

        # 平铺自我修复：同版本改坏一个文件 → check 报、apply 修
        src0 = make_src(tmp, "1.1.0", extra_code="x")   # 内容不同的同版本源
        (src0 / "payload" / "code" / "newmod.py").write_text(
            "# new\n", encoding="utf-8")
        (src0 / "manifest.json").write_text(json.dumps(
            vupdate.build_manifest(src0, "1.1.0", "b"),
            ensure_ascii=False), encoding="utf-8")
        # inst 现在内容和 src 一致（刚更新过）——先对齐再改坏
        write(inst / "newmod.py", "# new\n")
        (inst / "vcfg.py").write_text("# corrupted\n", encoding="utf-8")
        # 用与 inst 当前内容一致的源（重造 manifest 对齐 vcfg/app 删除态）
        src0 = make_src(tmp, "1.1.0")
        (src0 / "payload" / "code" / "newmod.py").write_text(
            "# new\n", encoding="utf-8")
        (src0 / "payload" / "code" / "app.py").unlink()
        (src0 / "manifest.json").write_text(json.dumps(
            vupdate.build_manifest(src0, "1.1.0", "b"),
            ensure_ascii=False), encoding="utf-8")
        cfg["update_source"] = str(src0)
        r = vupdate.check(cfg, quiet=True)
        chk("平铺同版本坏文件被识别",
            r["has_update"] and "code" in r["parts"], str(r["parts"]))
        rc = vupdate.apply(cfg, yes=True)
        chk("平铺自我修复返回 0", rc == 0, f"exit={rc}")
        chk("文件被修回",
            (inst / "vcfg.py").read_text(encoding="utf-8")
            == "# version 1.1.0\n")
        chk("修复后复检通过", vupdate.verify_local() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_apply() -> None:
    print("== 执行更新（成功路径） ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        inst = make_inst(tmp, "1.0.0")
        src = make_src(tmp, "1.1.0")
        redirect(inst, src)
        cfg = cfg_of(inst)
        cfg["update_source"] = str(src)
        set_smoke(True)
        rc = vupdate.apply(cfg, yes=True)
        chk("返回 0", rc == 0)
        chk("VERSION.json 是 1.1.0", vupdate.local_version()["version"] == "1.1.0")
        chk("state=ok", vupdate.local_version().get("state") == "ok")
        chk("code 文件已换", "# version 1.1.0 新功能" in
            (inst / "code" / "vcfg.py").read_text(encoding="utf-8")
            or "# version 1.1.0" in (inst / "code" / "vcfg.py").read_text(encoding="utf-8"))
        chk("app.py 已换", "print('1.1.0')" in
            (inst / "code" / "app.py").read_text(encoding="utf-8"))
        chk("manifest 已换", json.loads(
            (inst / "manifest.json").read_text(encoding="utf-8"))["version"] == "1.1.0")
        chk("备份已留", len(list(vupdate.UPDATES.glob("backup-*"))) == 1)
        chk("staging 已清", not list(vupdate.UPDATES.glob("staging-*")))
        chk("历史有记录", '"ok": true' in
            (inst / "data" / "update_history.jsonl").read_text(encoding="utf-8")
            and "1.1.0" in (inst / "data" / "update_history.jsonl").read_text(encoding="utf-8"))
        chk("复检一致", vupdate.verify_local() == [])

        # 幂等：再 apply 一次应报告已是最新
        rc = vupdate.apply(cfg, yes=True)
        chk("重复 apply 返回 0 且不重复替换", rc == 0 and
            len(list(vupdate.UPDATES.glob("backup-*"))) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_rollback() -> None:
    print("== 冒烟失败自动回滚 ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        inst = make_inst(tmp, "1.0.0")
        src = make_src(tmp, "1.1.0")
        redirect(inst, src)
        cfg = cfg_of(inst)
        cfg["update_source"] = str(src)
        set_smoke(False)          # 冒烟必败
        rc = vupdate.apply(cfg, yes=True)
        chk("返回 3", rc == 3)
        chk("VERSION.json 回到 1.0.0", vupdate.local_version()["version"] == "1.0.0")
        chk("state 回到 ok", vupdate.local_version().get("state") == "ok")
        chk("code 已还原", "# version 1.0.0" in
            (inst / "code" / "vcfg.py").read_text(encoding="utf-8"))
        chk("manifest 也还原", json.loads(
            (inst / "manifest.json").read_text(encoding="utf-8"))["version"] == "1.0.0")
        chk("历史记录失败", '"ok": false' in
            (inst / "data" / "update_history.jsonl").read_text(encoding="utf-8")
            or '"ok":false' in (inst / "data" / "update_history.jsonl").read_text(encoding="utf-8"))
        chk("复检一致", vupdate.verify_local() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_manual_rollback() -> None:
    print("== 手动回滚 ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        inst = make_inst(tmp, "1.0.0")
        src = make_src(tmp, "1.1.0")
        redirect(inst, src)
        cfg = cfg_of(inst)
        cfg["update_source"] = str(src)
        set_smoke(True)
        vupdate.apply(cfg, yes=True)
        chk("已是 1.1.0", vupdate.local_version()["version"] == "1.1.0")
        # 模拟用户输入 y
        import builtins
        old_input = builtins.input
        builtins.input = lambda *a: "y"
        try:
            rc = vupdate.rollback(cfg)
        finally:
            builtins.input = old_input
        chk("回滚返回 0", rc == 0)
        chk("回到 1.0.0", vupdate.local_version()["version"] == "1.0.0")
        chk("code 回到 1.0.0", "# version 1.0.0" in
            (inst / "code" / "vcfg.py").read_text(encoding="utf-8"))
        chk("备份已消费", not list(vupdate.UPDATES.glob("backup-*")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_rescue() -> None:
    print("== 断电救援（state=testing） ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        inst = make_inst(tmp, "1.0.0")
        src = make_src(tmp, "1.1.0")
        redirect(inst, src)
        cfg = cfg_of(inst)
        cfg["update_source"] = str(src)
        set_smoke(True)
        # 手动模拟"换完文件、写了 testing、还没跑冒烟就断电"的中间态
        shutil.copytree(src / "payload" / "code", inst / "code_new", dirs_exist_ok=True)
        shutil.rmtree(inst / "code")
        shutil.move(str(inst / "code_new"), str(inst / "code"))
        backup = vupdate.UPDATES / "backup-1.0.0-20261025-100000"
        backup.mkdir(parents=True)
        make_inst(tmp, "1.0.0")   # 另造一棵，只为拿旧 code
        shutil.copytree(tmp / "inst-1.0.0" / "code", backup / "code",
                        dirs_exist_ok=True)
        (inst / "VERSION.json").write_text(json.dumps({
            "version": "1.1.0", "state": "testing",
            "backup": str(backup), "prev_version": "1.0.0",
        }), encoding="utf-8")
        rc = vupdate.auto_rescue()
        chk("rescue 返回 0", rc == 0)
        chk("已回滚到 1.0.0", vupdate.local_version()["version"] == "1.0.0")
        chk("state 修复", vupdate.local_version().get("state") == "ok")
        chk("code 是旧版", "# version 1.0.0" in
            (inst / "code" / "vcfg.py").read_text(encoding="utf-8"))
        chk("复检一致", vupdate.verify_local() == [])

        # testing 但备份没了：至少把身份证修 ok，不报警
        (inst / "VERSION.json").write_text(json.dumps({
            "version": "1.1.0", "state": "testing",
            "backup": str(inst / "updates" / "gone"), "prev_version": "1.0.0",
        }), encoding="utf-8")
        vupdate.auto_rescue()
        chk("备份丢失也修好状态", vupdate.local_version().get("state") == "ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_verify_and_prune() -> None:
    print("== 复检与备份轮换 ==")
    tmp = Path(tempfile.mkdtemp(prefix="vgtest-"))
    try:
        inst = make_inst(tmp, "1.0.0")
        redirect(inst, make_src(tmp, "1.0.0"))
        chk("干净树复检通过", vupdate.verify_local() == [])
        # 改一个字节 → code 签名与逐文件校验都应报出来
        (inst / "code" / "app.py").write_text("print('hacked')\n",
                                              encoding="utf-8")
        errs = vupdate.verify_local()
        chk("改文件被抓到", any("app.py" in e for e in errs), str(errs[:2]))
        # 删文件
        (inst / "code" / "vcfg.py").unlink()
        errs = vupdate.verify_local()
        chk("缺文件被抓到", any("vcfg.py" in e for e in errs))

        # 备份轮换：只留最近 2 个
        for i in range(4):
            d = vupdate.UPDATES / f"backup-1.0.{i}-20261025-10000{i}"
            (d / "code").mkdir(parents=True)
            write(d / "code" / "x.txt", str(i))
            time.sleep(0.02)
        vupdate.prune_backups({"update_keep_backups": 2})
        left = sorted(p.name for p in vupdate.UPDATES.glob("backup-*"))
        chk("只留 2 个备份", len(left) == 2, str(left))
        chk("留的是最新的两个", "backup-1.0.3" in left[-1] and
            "backup-1.0.2" in left[0], str(left))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    t_ver()
    t_check()
    t_apply()
    t_flat()
    t_rollback()
    t_manual_rollback()
    t_rescue()
    t_verify_and_prune()
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
