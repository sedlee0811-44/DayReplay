"""会话（一次开机）边界识别。

为什么以"会话"而不是"天"为单位：
    这台机器的用法是"为了某个任务开机，做完就关"，不是每天定点开机。
    按天定时生成摘要，很可能永远不触发。会话边界必须由系统事件决定。

数据源：Windows 系统事件日志
    6005  事件日志服务已启动  → 开机（最可靠的启动标记）
    6006  事件日志服务已停止  → 关机
    1074  用户或程序发起的关机 → 比 6006 早几秒，含发起进程
    41/6008 非正常关机

为什么用 subprocess 调 PowerShell 而不是 win32evtlog：
    本机没有 pywin32，也不想为了读日志装一个 C 扩展。
    PowerShell 输出的 JSON 全是 ASCII 时间戳，不受控制台代码页影响。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

import vcfg

CACHE = vcfg.DATA / "sessions.json"
CACHE_TTL = 600.0          # 10 分钟
CREATE_NO_WINDOW = 0x08000000

_PS = (
    "Get-WinEvent -FilterHashtable @{LogName='System';Id=@(6005,6006,1074,41,6008)} "
    "-MaxEvents 400 -ErrorAction SilentlyContinue | "
    "Select-Object Id,@{n='T';e={$_.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss')}} | "
    "ConvertTo-Json -Compress"
)


def _query_events() -> list[dict]:
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
            capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW,
        )
        raw = p.stdout.decode("utf-8", "ignore").strip()
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        try:
            ts = time.mktime(time.strptime(d["T"], "%Y-%m-%dT%H:%M:%S"))
            out.append({"id": int(d["Id"]), "ts": ts})
        except Exception:
            continue
    out.sort(key=lambda x: x["ts"])
    return out


def build_sessions(events: list[dict]) -> list[dict]:
    """把事件流还原成 [(开机, 关机)] 序列。"""
    sessions: list[dict] = []
    cur: Optional[dict] = None
    for e in events:
        i = e["id"]
        if i == 6005:
            # 上一条没等到 6006 就又开机了 → 上次是非正常关机
            if cur and cur["shutdown"] is None:
                cur["clean"] = False
                sessions.append(cur)
            cur = {"boot": e["ts"], "shutdown": None, "clean": True, "reason": ""}
        elif i == 6006:
            if cur:
                cur["shutdown"] = e["ts"]
                sessions.append(cur)
                cur = None
        elif i == 1074:
            if cur:
                cur["reason"] = "用户关机"
        elif i in (41, 6008):
            if cur:
                cur["clean"] = False
                cur["reason"] = cur["reason"] or "异常关机"
    if cur:
        sessions.append(cur)
    # 重启会在一分钟内产生 6006 + 6005 对，落地成 0 小时"会话"，是噪声不是任务
    sessions = [s for s in sessions
                if s["shutdown"] is None or (s["shutdown"] - s["boot"]) >= 180]
    return sessions


def sessions(force: bool = False) -> list[dict]:
    """带缓存的会话列表。最后一条的 shutdown 为 None 表示当前正在用。"""
    if not force and CACHE.exists():
        try:
            d = json.loads(CACHE.read_text(encoding="utf-8"))
            if time.time() - d.get("fetched", 0) < CACHE_TTL:
                return d["sessions"]
        except Exception:
            pass
    got = build_sessions(_query_events())
    try:
        vcfg.ensure_dirs()
        CACHE.write_text(json.dumps(
            {"fetched": time.time(), "sessions": got},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return got


def current() -> Optional[dict]:
    """当前这次开机。机器已关就是 None。"""
    ss = sessions()
    if ss and ss[-1]["shutdown"] is None:
        return ss[-1]
    return None


def containing(ts: float) -> Optional[dict]:
    for s in sessions():
        end = s["shutdown"] if s["shutdown"] is not None else time.time()
        if s["boot"] <= ts <= end:
            return s
    return None


def unresolved(now: Optional[float] = None,
               max_age_days: float = 3.0) -> Optional[dict]:
    """上一次已经结束、但还没生成**最终**摘要的会话。

    `max_age_days` 是必须的。事件日志里存着几个月的历史会话，如果只按
    "没总结过"来找，每次开机都会翻出一个远古会话补一条 —— 总账里会
    慢慢堆满"某月某日某时开机，动过几个文件"这种没人看、也没有屏幕和
    语音数据支撑的废条目。

    用户要的是"这次开机干了什么"，所以只补最近几天的。更早的直接跳过，
    当作翻篇了。

    2026-10-24 缺陷修复：会话还在进行时也会写一份摘要（总账里显示
    "现在 结束"），此时打的"已总结"标记是半成品 —— shutdown 记录为 null。
    关机后必须重出一份完整的，否则从 18:03 到关机之间的内容永远丢失。
    所以：标记存在但 shutdown 是 null、而会话现在已关机 → 视为未总结。
    """
    now = now or time.time()
    done = _digested_boots()
    stale = _stale_boots()          # 标记是半成品（shutdown=null）的 boot
    cutoff = now - max_age_days * 86400
    for s in reversed(sessions()):
        if s["shutdown"] is None:
            continue                # 还在用，不用补
        if s["boot"] in done and s["boot"] not in stale:
            continue                # 已有完整总结
        if s["boot"] < cutoff:
            continue
        return s
    return None


_DIGEST_INDEX = vcfg.ROOT / "reports" / "index.json"


def _digested_boots() -> set[float]:
    i = _load_index()
    return {round(float(x["boot"]), 0) for x in i}


def _stale_boots() -> set[float]:
    """标记是半成品的 boot：`shutdown` 记录为 null，说明是会话进行中写的。

    会话结束后必须重出完整总结，否则后半段内容永远丢在总账外面。
    """
    out = set()
    for x in _load_index():
        if x.get("shutdown") is None:
            out.add(round(float(x["boot"]), 0))
    return out


def _load_index() -> list[dict]:
    if not _DIGEST_INDEX.exists():
        return []
    try:
        return json.loads(_DIGEST_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def mark_digested(sess: dict, path: Path, headline: str = "") -> None:
    idx = _load_index()
    idx = [x for x in idx if round(float(x["boot"]), 0) != round(sess["boot"], 0)]
    idx.append({
        "boot": sess["boot"],
        "shutdown": sess["shutdown"],
        "file": path.name,
        "headline": headline,
        "made_at": time.time(),
    })
    idx.sort(key=lambda x: x["boot"])
    _DIGEST_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _DIGEST_INDEX.write_text(
        json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


def fmt(ts: Optional[float]) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def describe(s: dict) -> str:
    end = s["shutdown"] if s["shutdown"] is not None else time.time()
    h = (end - s["boot"]) / 3600.0
    tail = "进行中" if s["shutdown"] is None else ("关机" if s["clean"] else "非正常关机")
    return f"{fmt(s['boot'])} → {fmt(s['shutdown'])}　{h:.1f} 小时　{tail}"


if __name__ == "__main__":
    ss = sessions(force=True)
    print(f"共 {len(ss)} 个会话")
    for s in ss[-8:]:
        print("  ", describe(s), s.get("reason", ""))
    c = current()
    print("\n当前会话:", describe(c) if c else "（机器已关）")
