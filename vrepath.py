"""历史数据换位助手 —— 整包搬到新目录后，修掉里面写死的旧根路径。

为什么需要它：
    voice.db 的 segment.wav_path、总账 md 里固化的 file:/// 链接，写的
    都是旧机器的绝对路径（E:\\AI\\voice\\data\\audio\\...）。原样拷到新
    电脑，这些链接全都指向一个不存在的盘符 —— 文字稿还能看，录音点不开。
    本脚本只做一件事：把 data/ 与 reports/ 下所有内容里的旧根路径换成
    新根路径。数据库按列做字符串替换，文本文件按字节替换（编码无关），
    音频/图片等二进制一律跳过。只换路径，不动任何内容。

用法：
    python vrepath.py --old E:\\AI\\voice --new D:\\VoiceGuard
    python vrepath.py --old E:\\AI\\voice --new D:\\VoiceGuard --root .
    # --root 默认当前目录（要求 data/ 与 reports/ 都在它下面）

安装器（install.ps1）在"带入历史数据"时会自动调用，平时不用手动跑。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 二进制/大文件：跳过（音频、图片、模型、压缩包）
SKIP_EXT = {".db", ".db-wal", ".db-shm", ".wav", ".pcm", ".jpg", ".jpeg",
            ".png", ".webp", ".bin", ".pt", ".onnx", ".mvn", ".zip",
            ".bpe", ".model", ".bad"}
# 只处理这些目录下的文件
SCAN_DIRS = ("data", "reports")
# ORIGIN_ROOT.txt 记的是"这套数据原来在哪"，是迁移的源头信息，
# 必须留着原值 —— 改了它，下次再搬家就找不到真正的旧根了。
SKIP_NAMES = {"ORIGIN_ROOT.txt"}


def _pairs(old: str, new: str) -> list[tuple[bytes, bytes]]:
    o_bs = old.replace("/", "\\").rstrip("\\")
    n_bs = new.replace("/", "\\").rstrip("\\")
    o_fs = old.replace("\\", "/").rstrip("/")
    n_fs = new.replace("\\", "/").rstrip("/")
    pairs = [(o_bs.encode("utf-8"), n_bs.encode("utf-8")),
             (o_fs.encode("utf-8"), n_fs.encode("utf-8"))]
    return pairs


def fix_db(db: Path, pairs: list[tuple[bytes, bytes]]) -> int:
    """按表按列做 replace。只动 TEXT 列；返回受影响的行数。"""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    total = 0
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            for c in cols:
                expr = f'"{c}"'
                # ⚠ SQLite 坑：`SET x = f1(x), x = f2(x)` 这种给同一列多个
                # 赋值，第二个表达式看到的是**原值**，会把第一个的结果整个
                # 盖回原样 —— 曾经这么写，replace 报 416 行成功，数据一个都
                # 没变（changes() 按"处理过的行"计数，不比对新旧值）。
                # 多个替换必须套在一个表达式里，层层传递。
                inner = expr
                params = []
                for ob, nb in pairs:
                    inner = f"replace({inner}, ?, ?)"
                    params += [ob.decode("utf-8"), nb.decode("utf-8")]
                where = " OR ".join(f"{expr} LIKE ?" for _, _ in pairs)
                like_params = ["%" + ob.decode("utf-8") + "%" for ob, _ in pairs]
                cur = conn.execute(
                    f'UPDATE "{t}" SET {expr} = {inner} WHERE {where}',
                    params + like_params)
                if cur.rowcount and cur.rowcount > 0:
                    total += cur.rowcount
                    print(f"  {t}.{c}: {cur.rowcount} 行")
        conn.commit()
    finally:
        conn.close()
    return total


def verify(db: Path, pairs: list[tuple[bytes, bytes]]) -> int:
    """换连接重读，数还有多少行带着旧路径。

    为什么必须有这一步：SQLite 的 changes() 按"处理过的行"计数，不比对
    新旧值 —— 曾经 UPDATE 报 416 行成功，其实一个都没改（见代码档案
    坑 #48）。迁移脚本说"改完了"不算数，重读才算。
    """
    conn = sqlite3.connect(str(db))
    left = 0
    try:
        for t in [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]:
            for r in conn.execute(f'PRAGMA table_info("{t}")'):
                c = r[1]
                for ob, _ in pairs:
                    n = conn.execute(
                        f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" LIKE ?',
                        ("%" + ob.decode("utf-8") + "%",)).fetchone()[0]
                    left += n
                    if n:
                        print(f"  ⚠ {t}.{c} 还有 {n} 行带着旧路径")
    finally:
        conn.close()
    return left


def fix_file(p: Path, pairs: list[tuple[bytes, bytes]]) -> bool:
    try:
        raw = p.read_bytes()
    except Exception:
        return False
    new = raw
    for ob, nb in pairs:
        if ob in new:
            new = new.replace(ob, nb)
    if new != raw:
        try:
            p.write_bytes(new)
            return True
        except Exception:
            return False
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="旧根路径，如 E:\\AI\\voice")
    ap.add_argument("--new", required=True, help="新根路径，如 D:\\VoiceGuard")
    ap.add_argument("--root", default=".", help="安装根目录（默认当前目录）")
    a = ap.parse_args()

    old, new = a.old.rstrip("\\/"), a.new.rstrip("\\/")
    if not old or not new:
        print("✗ --old / --new 不能为空")
        return 1
    if old == new:
        print("新旧路径相同，无需处理。")
        return 0
    pairs = _pairs(old, new)
    root = Path(a.root)

    db = root / "data" / "voice.db"
    if db.exists():
        print(f"数据库 {db}")
        n = fix_db(db, pairs)
        print(f"  合计 {n} 行")
        # WAL 里可能还躺着没并进去的页，checkpoint 一下让替换完整落盘
        try:
            conn = sqlite3.connect(str(db))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            pass
        left = verify(db, pairs)
        if left:
            print(f"✗ 复检发现 {left} 处旧路径没改干净，请把上面的信息发给开发的人")
            return 2
        print("  复检通过：数据库里已无旧路径")
    else:
        print("（没有 data/voice.db，跳过）")

    nfile = 0
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() in SKIP_EXT:
                continue
            if p.name == "voice.db":
                continue
            if fix_file(p, pairs):
                nfile += 1
                print(f"  {p.relative_to(root)}")
    print(f"\n✓ 完成：{nfile} 个文本文件 + 数据库已从 {old} 改到 {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
