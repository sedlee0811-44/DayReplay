"""声纹（说话人识别）——CAM++ 192 维嵌入 + 余弦判别。

实测判别力（2026-10-24 本机实测定标）：
    同一人跨次录音  0.69      不同真人之间  -0.08
    同一 SAPI 引擎的两个合成音之间 0.49-0.53（同引擎音色天然接近）
    真人档案质心一致性 0.78-0.81
故阈值取 spk_same=0.55 / spk_other=0.30：
    既能挡住合成的近似音，又不误杀真人的跨次匹配。

设计取舍：
- **嵌入一律落库**（768 字节/段）。这样重新登记或改阈值时只需重算标签，
  不必重跑模型——历史可以整体重判。
- 档案用"质心 + 一致性"表示，不用 SVM/聚类。人少（1-3 人）时质心最稳，
  且随时能看懂为什么这么判。
- **比质心，不比两两**。实测 Zira 与 Huihui 的单对相似度到过 0.57（已越阈值），
  但走质心只有 0.53，被正确挡下。用"任一样本超阈值即命中"会误标。
- 时长不足 spk_min_duration（现为 2.0 秒）不做判断：
  1.2-1.9 秒的嵌入在说话人维度上乱跳（实测 0.20-0.46），硬判全是误报。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

import numpy as np

import vcfg

log = logging.getLogger("speaker")

PROFILES_JSON = vcfg.SPEAKER_DIR / "profiles.json"
LABEL_OTHER = "其他"
LABEL_UNKNOWN = "?"


# ------------------------------------------------------------------ 编码器

class SpeakerEncoder:
    """CAM++ 说话人嵌入。按需加载，进程内复用。"""

    def __init__(self, device: str = "cpu"):
        self.device = device
        self._m = None
        self.fails = 0

    def _load(self):
        if self._m is not None:
            return self._m
        from funasr import AutoModel

        path = vcfg.model_dir("iic/speech_campplus_sv_zh-cn_16k-common")
        t0 = time.time()
        log.info("加载声纹模型 %s", path)
        self._m = AutoModel(model=path, device=self.device,
                            disable_update=True, disable_pbar=True, disable_log=True)
        log.info("声纹模型就绪，用时 %.1fs", time.time() - t0)
        return self._m

    def embed(self, wav: str | Path) -> np.ndarray | None:
        try:
            r = self._load().generate(input=str(wav))
            v = np.asarray(r[0]["spk_embedding"]).reshape(-1).astype(np.float32)
            n = float(np.linalg.norm(v))
            return v / n if n > 0 else None
        except Exception as e:
            self.fails += 1
            log.warning("声纹提取失败 %s: %s", Path(str(wav)).name, e)
            return None


def to_blob(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ------------------------------------------------------------------ 档案

def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "unnamed"


def load_profiles() -> dict[str, np.ndarray]:
    if not PROFILES_JSON.exists():
        return {}
    try:
        meta = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for name in meta.get("profiles", {}):
        p = vcfg.SPEAKER_DIR / f"{_safe(name)}.npy"
        if p.exists():
            out[name] = np.load(p).astype(np.float32)
    return out


def profile_meta() -> dict:
    if not PROFILES_JSON.exists():
        return {"profiles": {}}
    try:
        return json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {"profiles": {}}


def save_profile(name: str, centroid: np.ndarray, info: dict) -> None:
    vcfg.ensure_dirs()
    np.save(vcfg.SPEAKER_DIR / f"{_safe(name)}.npy", centroid.astype(np.float32))
    meta = profile_meta()
    meta.setdefault("profiles", {})[name] = info
    PROFILES_JSON.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _pcache["t"] = 0.0


def delete_profile(name: str) -> bool:
    meta = profile_meta()
    if name not in meta.get("profiles", {}):
        return False
    meta["profiles"].pop(name, None)
    PROFILES_JSON.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (vcfg.SPEAKER_DIR / f"{_safe(name)}.npy").unlink(missing_ok=True)
    _pcache["t"] = 0.0
    return True


_pcache: dict = {"t": 0.0, "p": {}}


def current_profiles(max_age: float = 30.0) -> dict[str, np.ndarray]:
    """带缓存的档案读取——常驻进程不必每次读盘，但又能在登记后自动生效。"""
    now = time.time()
    if now - _pcache["t"] > max_age:
        _pcache["p"] = load_profiles()
        _pcache["t"] = now
    return _pcache["p"]


def build_centroid(embs: list[np.ndarray]) -> tuple[np.ndarray, int, float]:
    """求质心，只剔掉真正的离群点。

    早期版本按 25 分位硬切，样本少时会无故丢好样本
    （实测 4 段一致性 0.97 还丢 1 段）。改成：只有明显低于中位数
    （差 0.20 以上）才当离群，否则全收。
    """
    M = np.stack([e / (np.linalg.norm(e) + 1e-9) for e in embs])
    c = M.mean(axis=0)
    c = c / (np.linalg.norm(c) + 1e-9)
    sims = M @ c
    keep = sims >= max(np.median(sims) - 0.20, 0.30)
    if keep.sum() < 2 or keep.sum() == len(sims):
        return c.astype(np.float32), int(keep.sum() or len(sims)), float(sims.mean())
    c = M[keep].mean(axis=0)
    c = c / (np.linalg.norm(c) + 1e-9)
    sims = M[keep] @ c
    return c.astype(np.float32), int(keep.sum()), float(sims.mean())


# ------------------------------------------------------------------ 判别

def classify(emb: np.ndarray, profiles: dict[str, np.ndarray],
             cfg: dict) -> tuple[str, float]:
    """返回 (标签, 分数)。标签为档案名 / '其他' / '?'。"""
    if not profiles:
        return LABEL_UNKNOWN, 0.0
    best_name, best = LABEL_UNKNOWN, -1.0
    for name, c in profiles.items():
        s = cosine(emb, c)
        if s > best:
            best_name, best = name, s
    if best >= cfg["spk_same"]:
        return best_name, best
    if best >= cfg["spk_other"]:
        return LABEL_OTHER, best
    return LABEL_UNKNOWN, best


# ------------------------------------------------------------------ 存储

DDL = """
CREATE TABLE IF NOT EXISTS speaker (
    segment_id INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    score      REAL,
    emb        BLOB,
    dim        INTEGER,
    created_at REAL NOT NULL
);
"""


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def save(conn: sqlite3.Connection, seg_id: int, label: str, score: float,
         emb: np.ndarray | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO speaker(segment_id,label,score,emb,dim,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (seg_id, label, score, to_blob(emb) if emb is not None else None,
         int(emb.shape[0]) if emb is not None else None, time.time()))
    conn.commit()


def relabel_all(conn: sqlite3.Connection, cfg: dict) -> dict[str, int]:
    """用当前档案重判所有已存嵌入（不重跑模型）。"""
    profiles = load_profiles()
    rows = list(conn.execute(
        "SELECT segment_id, emb FROM speaker WHERE emb IS NOT NULL"))
    counts: dict[str, int] = {}
    for r in rows:
        label, score = classify(from_blob(r["emb"]), profiles, cfg)
        conn.execute("UPDATE speaker SET label=?, score=? WHERE segment_id=?",
                     (label, score, r["segment_id"]))
        counts[label] = counts.get(label, 0) + 1
    conn.commit()
    return counts


# ------------------------------------------------------------- 登记取样

def _voiced_ratio(x: np.ndarray, sr: int, frame_ms: int = 30,
                  mode: int = 2) -> float:
    """一段 int16 音频里被判为语音的帧占比。用于筛掉停顿块。

    webrtcvad 只认 8/16/32/48k，其它采样率退回能量法（够用）。
    """
    frame = int(sr * frame_ms / 1000)
    if frame <= 0 or len(x) < frame:
        return 0.0
    n = len(x) // frame
    try:
        import webrtcvad
        vad = webrtcvad.Vad(mode)
        hit = sum(1 for i in range(n)
                  if vad.is_speech(x[i * frame:(i + 1) * frame].tobytes(), sr))
        return hit / float(n)
    except Exception:
        rms = np.sqrt(np.mean(x.astype(np.float32) ** 2))
        return 1.0 if rms >= 200 else 0.0


def embed_samples(path: str | Path, enc: "SpeakerEncoder", chunk_s: float = 3.0,
                  max_chunks: int = 8, min_voiced: float = 0.45) -> list[np.ndarray]:
    """把一段音频拆成若干 3 秒样本分别取嵌入。

    为什么需要：登记时如果用户连着说不换气，切句只切出 1-2 段，
    凑不够建质心所需的样本数。长段自己切块既解决这个问题，
    也让质心对"某一句话的偶然特征"更不敏感。

    为什么要加 min_voiced：盲切会把段内的停顿/换气块也切成样本。
    实测 #75（15.8s 切成 7 块）里有两块其实是停顿，与本人质心相似度
    只有 0.46/0.26，把簇内最低相似度硬拉到 0.331，差点触发
    "一致性偏低"告警。按语音占比预筛后，这类空块在取嵌入前就被丢掉。
    """
    import tempfile

    import soundfile as sf

    path = Path(path)
    try:
        x, sr = sf.read(str(path), dtype="int16")
    except Exception:
        return []
    if x.ndim > 1:
        x = x[:, 0]
    total = len(x) / float(sr or 16000)

    if total < chunk_s * 2:
        e = enc.embed(path)
        return [e] if e is not None else []

    per = int(sr * chunk_s)
    chunks: list[np.ndarray] = []
    for i in range(0, min(len(x) // per, max_chunks)):
        seg = x[i * per:(i + 1) * per]
        if len(seg) < sr * 1.0:      # 尾巴太短不要
            continue
        chunks.append(seg)

    # 先按语音占比筛一遍；筛完不够 3 块就退回全部，宁可留脏也别登记失败
    keep = [c for c in chunks if _voiced_ratio(c, sr) >= min_voiced]
    if len(keep) < 3:
        keep = chunks

    out: list[np.ndarray] = []
    with tempfile.TemporaryDirectory() as td:
        for i, seg in enumerate(keep):
            tmp = Path(td) / f"c{i}.wav"
            sf.write(str(tmp), seg, sr, subtype="PCM_16")
            e = enc.embed(tmp)
            if e is not None:
                out.append(e)
    if not out:
        e = enc.embed(path)
        if e is not None:
            out.append(e)
    return out
