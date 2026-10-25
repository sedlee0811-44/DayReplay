"""SenseVoice-Small 转写工作进程。

一次推理同时拿到三样东西，这是选它而不是 Whisper 的全部理由：
    1. 转写文本（中文显著强于 Whisper）
    2. 情绪标签（SER）：NEUTRAL / HAPPY / SAD / ANGRY / FEARFUL / DISGUSTED / SURPRISED
    3. 声学事件（AED）：Laughter / Cry / Cough / Applause / BGM / Sneeze / Breath

关键坑：FunASR 的 rich_transcription_postprocess 会把 <|HAPPY|> <|Laughter|>
这些标签**擦掉**，只留干净文本。所以必须在后处理之前解析原始输出，
否则情绪和事件全丢。

用法：
    python voice_asr.py                 # 常驻，轮询待转写段
    python voice_asr.py --once          # 处理完现有队列就退出
    python voice_asr.py --file x.wav    # 单文件测试（不写库）
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

import vcfg

# 模型缓存统一放 E 盘（C 盘缓存会重复占 940MB）
os.environ.setdefault("MODELSCOPE_CACHE", str(vcfg.ROOT / "models"))

import voice_db  # noqa: E402
import voice_speaker as vs  # noqa: E402

log = logging.getLogger("asr")

MODEL_ID = "iic/SenseVoiceSmall"


def resolve_model_dir(model_id: str) -> str:
    """保留旧名，实际逻辑在 vcfg.model_dir（见那里的注释）。"""
    return vcfg.model_dir(model_id)

LANG_TAGS = {"zh", "en", "yue", "ja", "ko", "nospeech"}
EMO_TAGS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED",
            "SURPRISED", "EMO_UNKNOWN"}
EVENT_TAGS = {"Speech", "BGM", "Applause", "Laughter", "Cry", "Sneeze",
              "Breath", "Cough", "VocalMusic", "InstrumentalMusic"}
IGNORE_TAGS = {"withitn", "woitn", "EMO_UNKNOWN"}

TAG_RE = re.compile(r"<\|([^|]+)\|>")

# 这台机器上只会有普通话。韩文/日文假名一旦出现，一定是模型在噪声上瞎猜
# （实测见过 ".그."、"그도."、"我从买行네"）。它们不但没意义，还会被总账
# 当成"听到的内容"写进去。所以直接抹掉，抹完不剩字就判为噪声。
NON_ZH_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af\u31f0-\u31ff]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def scrub(text: str, lang: str, forced: str = "") -> tuple[str, str]:
    """去掉非中文残字。返回 (清洗后的文本, 修正后的语种)。

    判噪声的标准是"还剩几个实在的字"，不是"模型说了什么"：
    只剩标点、只剩一两个字、或者抹掉假名后什么都不剩 —— 都当没听到。

    `forced` 是本次解码指定的语言（比如 "zh"）。指定了就以它为准，不再
    采信模型自己打的标签 —— 同一个模型既说"这是中文"又说"这是韩语"，
    只会让人对着日志犯嘀咕。
    """
    t = NON_ZH_RE.sub(" ", text or "").strip()
    core = re.sub(r"[\s\W_]+", "", t)
    if len(core) < 2:
        return "", ""
    if not CJK_RE.search(t) and len(core) < 4:
        return "", ""
    return t, (forced or lang or "zh")


def parse_rich(raw: str) -> dict:
    """从 SenseVoice 原始输出里拆出 语言 / 情绪 / 事件。"""
    tags = TAG_RE.findall(raw or "")
    lang = next((t for t in tags if t in LANG_TAGS and t != "nospeech"), "")
    emotion = next((t for t in tags if t in EMO_TAGS and t != "EMO_UNKNOWN"), "")
    events = [t for t in tags if t in EVENT_TAGS]
    return {"lang": lang, "emotion": emotion, "events": events, "tags": tags}


class Transcriber:
    def __init__(self, device: str = "cpu", cfg: dict | None = None):
        from funasr import AutoModel

        self.cfg = cfg or {}
        t0 = time.time()
        path = resolve_model_dir(MODEL_ID)
        log.info("加载模型 %s ...", path)
        self.model = AutoModel(
            model=path,
            device=device,
            disable_update=True,
            disable_pbar=True,
            disable_log=True,
        )
        log.info("模型就绪，用时 %.1fs", time.time() - t0)

    def _input(self, wav):
        """决定喂给模型的东西：默认做一遍前端处理，不行就退回原文件。

        为什么要在这一步加工而不是相信原盘：USB 摄像头麦是远场拾音，
        录到的电平比模型训练分布低一截，段首尾还常常吊着几百毫秒纯噪声。
        归一化 + 掐静音把这些对齐到模型的舒适区。

        为什么要用 try 兜底但**不能直接静默**：这里一旦悄悄退回原文件，
        "增强没生效"就会和"增强没帮忙"混成一回事，永远查不清。
        所以退回时要把原因记进日志（见本文件头部踩过的坑）。
        """
        if not self.cfg.get("asr_preprocess", True):
            return str(wav)
        try:
            import soundfile as sf

            import vquality

            x, sr = sf.read(str(wav), dtype="int16", always_2d=False)
            if x.ndim > 1:
                x = x.mean(axis=1).astype(np.int16)
            y = vquality.enhance(
                x, sr, float(self.cfg.get("asr_target_dbfs", -20.0)))
            return y.astype(np.float32)
        except Exception as e:
            log.warning("前端处理失败，退回原始音频 %s：%s", wav, e)
            return str(wav)

    def run(self, wav: str | Path, language: str = "auto") -> dict:
        t0 = time.time()
        src = self._input(wav)
        res = self.model.generate(
            input=src,
            cache={},
            language=language,
            use_itn=True,
            batch_size_s=int(self.cfg.get("asr_batch_size_s", 30)),
        )
        dt = time.time() - t0
        raw = res[0].get("text", "") if res else ""
        info = parse_rich(raw)
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
            clean = rich_transcription_postprocess(raw)
        except Exception:
            clean = TAG_RE.sub("", raw).strip()
        info["raw"] = raw
        info["text"] = clean
        info["elapsed"] = dt
        return info


# ---------------------------------------------------------------- 活动声

_CLF: "vevents.EventClassifier | None" = None


def get_classifier(cfg: dict) -> "vevents.EventClassifier":
    """AST 分类器全局单例。懒加载：纯说话的日子一次都不 import torch。"""
    global _CLF
    if _CLF is None:
        import vevents
        _CLF = vevents.EventClassifier(cfg)
    return _CLF


def process_segment(r, tr: "Transcriber", cfg: dict) -> dict:
    """处理一个语音段：转写 + 活动声标签。返回要落库的字段。

    两条路：
        flag='event'（录音端粗筛捞回来的活动声候选）
            → AST 先判。判人声才转写（可能是含话的声响）；判活动/小孩/水声
              直接定名，**不转写**——SenseVoice 在吱嘎流水上只会瞎猜；判噪声
              就静默处理掉，只留痕迹不出声。
        普通段（过了谐波闸门）
            → 照旧 SenseVoice 转写。但文本为空、或带了非语音事件标签
              （啼哭/笑/咳/背景音乐…）时，再上 AST 补中文活动标签。
              啼哭走这条：SenseVoice 只输出 <|Cry|>，能给出"小孩啼哭"和
              置信度的只有 AST。有整句文字的普通说话不上 AST，省 CPU。
    """
    import vevents

    wav = Path(r["wav_path"])
    lang_cfg = cfg.get("asr_language", "zh")
    try:
        flag = (r["flag"] or "") if "flag" in r.keys() else ""
    except Exception:
        flag = ""

    text, lang, emotion, events, raw = "", "", "", [], ""
    labels: list[dict] = []
    t0 = time.time()

    if flag == "event":
        ev = get_classifier(cfg).classify_wav(wav)
        if ev["ok"] and ev["category"] == vevents.SPEECH:
            info = tr.run(wav, language=lang_cfg)
            text, lang = scrub(info["text"], info["lang"],
                               "" if lang_cfg == "auto" else lang_cfg)
            emotion, events, raw = info["emotion"], info["events"], info["raw"]
            labels = vevents.merge_labels(vevents.sensevoice_zh(events),
                                          ev["labels"])
        elif ev["category"] in (vevents.BABY, vevents.ACTIVITY, vevents.WATER):
            labels = ev["labels"]           # 定名即可，不转写
        # else：噪声/无关 —— text/labels 留空，只留"已处理"的痕迹
    else:
        info = tr.run(wav, language=lang_cfg)
        text, lang = scrub(info["text"], info["lang"],
                           "" if lang_cfg == "auto" else lang_cfg)
        emotion, events, raw = info["emotion"], info["events"], info["raw"]
        aed = vevents.sensevoice_zh(events)
        # "背景音乐"是伴奏，有整句文字时不用 AST；其余事件标签都值得深挖。
        # 短文本（<6 字）也上 AST：啼哭时 SenseVoice 经常吐出"啊啊啊"这种
        # 两三个字的碎音，SCRUB 拦不住，只有 AST 能认出"小孩啼哭"
        # （实测 SenseVoice 的 <|Cry|> 在 ESC-50 啼哭上 0/3 触发，指望不上）。
        need_ast = (not text) or len(text) < 6 or any(
            lb["zh"] != "背景音乐" for lb in aed)
        if need_ast:
            ev = get_classifier(cfg).classify_wav(wav)
            labels = vevents.merge_labels(aed, ev["labels"])
            # AST 认出了真事件，SenseVoice 那张"背景音乐"多半是误触发
            # （实测啼哭段被判 BGM）。有真事件时不带它。
            if ev["labels"]:
                labels = [lb for lb in labels
                          if not (lb.get("src") == "sensevoice"
                                  and lb["zh"] == "背景音乐")]
        else:
            labels = aed
    return {"text": text, "lang": lang, "emotion": emotion, "events": events,
            "raw": raw, "labels": labels, "flag": flag,
            "elapsed": time.time() - t0}


def worker(conn, tr: Transcriber, enc, cfg: dict, once: bool = False):
    stop_file = vcfg.DATA / "STOP"
    idle = 0
    while True:
        if stop_file.exists():
            log.info("检测到 STOP 文件，退出")
            return
        rows = voice_db.pending_asr(conn, limit=20)
        if not rows:
            if once:
                log.info("队列已清空，退出")
                return
            idle += 1
            if idle % 40 == 1:
                log.debug("空闲，等待新语音段…")
            time.sleep(3.0)
            continue
        idle = 0
        for r in rows:
            wav = Path(r["wav_path"])
            if not wav.exists():
                log.warning("段 #%d 音频缺失，跳过：%s", r["id"], wav)
                voice_db.save_asr(conn, r["id"], "", "", "", [], "[missing]", "n/a", 0.0)
                continue
            try:
                out = process_segment(r, tr, cfg)
            except Exception as e:
                log.error("段 #%d 处理失败: %s", r["id"], e)
                continue
            dur = float(r["duration"] or 0.0)
            rtf = out["elapsed"] / dur if dur > 0 else 0.0
            voice_db.save_asr(
                conn, r["id"], out["text"], out["lang"], out["emotion"],
                out["events"], out["raw"], MODEL_ID, rtf,
                labels=out["labels"])

            # ---- 声纹：短段不判，避免误报；活动声段没有可辨的声纹 ----
            if out["flag"] == "event" or dur < cfg["spk_min_duration"]:
                vs.save(conn, r["id"], vs.LABEL_UNKNOWN, 0.0, None)
                who, wscore = vs.LABEL_UNKNOWN, 0.0
            else:
                emb = enc.embed(wav)
                if emb is None:
                    vs.save(conn, r["id"], vs.LABEL_UNKNOWN, 0.0, None)
                    who, wscore = vs.LABEL_UNKNOWN, 0.0
                else:
                    who, wscore = vs.classify(emb, vs.current_profiles(), cfg)
                    vs.save(conn, r["id"], who, wscore, emb)

            zh = "、".join(l["zh"] for l in out["labels"])
            log.info("#%d %.1fs%s [%s/%s|%s %.2f] %s",
                     r["id"], dur, " 活动声" if out["flag"] else "",
                     out["lang"] or "?", out["emotion"] or "-",
                     who, wscore,
                     (out["text"] or "")[:50] or (f"〔{zh}〕" if zh else "（无声纹内容）"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--device", default="cpu")
    a = ap.parse_args()

    vcfg.ensure_dirs()
    vcfg.setup_logging("asr.log")
    cfg = vcfg.load()

    tr = Transcriber(a.device, cfg)
    if a.file:
        lang_cfg = cfg.get("asr_language", "zh")
        info = tr.run(a.file, language=lang_cfg)
        info["text"], info["lang"] = scrub(
            info["text"], info["lang"], "" if lang_cfg == "auto" else lang_cfg)
        print("\n语言 :", info["lang"])
        print("情绪 :", info["emotion"])
        print("事件 :", info["events"])
        print("文本 :", info["text"])
        print("原始 :", info["raw"][:300])
        print(f"耗时 : {info['elapsed']:.2f}s")
        enc = vs.SpeakerEncoder(a.device)
        e = enc.embed(a.file)
        if e is not None:
            who, sc = vs.classify(e, vs.current_profiles(), cfg)
            print(f"声纹 : {who} ({sc:.3f})")
        return

    conn = voice_db.connect()
    voice_db.init(conn)
    vs.init_table(conn)
    enc = vs.SpeakerEncoder(a.device)
    worker(conn, tr, enc, cfg, once=a.once)
    conn.close()


if __name__ == "__main__":
    main()
