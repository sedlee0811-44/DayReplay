"""人类活动声识别 —— 把"不是说话、但也不是噪声"的声响认出来。

为什么需要这个模块
------------------
谐波闸门（vquality.is_human）只回答一个问题："这里面有没有声带周期性振动"。
它把语音保住了，却也把一大类**有价值的声响**一并丢了：

    小孩啼哭    —— 其实有谐波，能过闸，但转写出来是乱码，等于没认
    挪动凳子    —— 吱嘎/刮擦，宽带非谐波，按噪声丢弃
    走动脚步    —— 脉冲串，非谐波，丢弃
    水龙头流水  —— 宽带嘶声，非谐波，丢弃

用户要的是"尽量"把这些认出来，不是全部。所以做成两级：

    第一级 · DSP 粗筛（录音进程里跑，纯 numpy，单段约 20ms）
        不谐波、但"有活动特征"的段不丢弃，打 flag='event' 落库。
        特征：脉冲数（脚步/敲击）、中频带调幅（刮擦的 stick-slip）、
        高频占比+能量动态（水流嘶声）。风扇/空调这类稳态噪声三个都不占。

    第二级 · AST 精筛（转写进程里跑，MIT/ast-finetuned-audioset，527 类）
        按 AudioSet 标签映射成中文类别。风扇、白噪声、静音这些明确不认；
        说话类交回 ASR 通路拿文本。

为什么第二级需要模型而不是继续堆规则：规则能分开"有水声特征"和"没水声
特征"，但分不清"水龙头"和"键盘打字"——后者同样是宽带瞬态。AudioSet 里
两类都有现成标签，让模型判比分我们拍阈值可靠。

⚠ 录音进程（实时回调）里只准用第一级：torch/transformers 一律懒加载，
放在模块顶层 import 会把 5~10 秒的导入时间塞进音频线程。
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np

log = logging.getLogger("vevents")

# 模型缓存放 E 盘（和 SenseVoice 一个思路：C 盘缓存会重复占几百 MB）
_HF_HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "hf")
os.environ.setdefault("HF_HOME", _HF_HOME)
# huggingface.co 在本机网络不可达，走镜像；不通就退化成"只有 DSP 粗筛"
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# ⚠ huggingface_hub 在 **import 时**就把缓存路径算进 constants 了。转写进程里
# funasr（SenseVoice）先 import 它，等 vevents 被懒加载时再设 os.environ 已经
# 晚了 —— 表现为"AST 明明已缓存，却报 Can't load feature extractor"，活动声
# 静默退化成只有粗筛（e2e 测试抓到的）。所以这里直接改常量兜底。
try:
    import huggingface_hub.constants as _hfc
    _hfc.HF_HOME = _HF_HOME
    _hfc.HF_HUB_CACHE = os.path.join(_HF_HOME, "hub")
    _hfc.ENDPOINT = os.environ.get("HF_ENDPOINT", _hfc.ENDPOINT)
except Exception:
    pass

# ---------------------------------------------------------------- 类别定义

BABY = "baby"          # 小孩相关
ACTIVITY = "activity"  # 人体活动（挪动、走动、碰撞）
WATER = "water"        # 水声
SPEECH = "speech"      # 说话（交回 ASR）
NOISE = "noise"        # 明确的环境噪声/无关声
OTHER = "other"        # 没映射上的，一律当无关

# AudioSet 英文标签 → (中文, 类别)。按名字匹配而不是按下标：换 checkpoint
# 时标签顺序可能变，名字不会。
AST_BY_NAME = {
    "Baby cry, infant cry": ("小孩啼哭", BABY),
    "Crying, sobbing": ("哭泣声", BABY),
    "Baby laughter": ("小孩笑声", BABY),
    "Children shouting": ("小孩叫喊", BABY),
    "Children playing": ("小孩玩耍", BABY),
    "Battle cry": ("喊叫声", ACTIVITY),
    "Walk, footsteps": ("走动/脚步", ACTIVITY),
    "Squeak": ("吱嘎摩擦声", ACTIVITY),          # 挪凳子最常见的一类
    "Scratch": ("刮擦声", ACTIVITY),
    "Rub": ("摩擦声", ACTIVITY),
    "Slam": ("碰撞声", ACTIVITY),
    "Knock": ("敲击声", ACTIVITY),
    "Tap": ("轻敲声", ACTIVITY),
    "Door": ("门响", ACTIVITY),
    "Sliding door": ("推拉门声", ACTIVITY),
    "Cupboard open or close": ("开合柜橱", ACTIVITY),
    "Drawer open or close": ("开合抽屉", ACTIVITY),
    "Doorbell": ("门铃声", ACTIVITY),
    "Power windows, electric windows": ("电动车窗声", ACTIVITY),
    "Water tap, faucet": ("水龙头流水", WATER),
    "Pour": ("倒水声", WATER),
    "Water": ("水流声", WATER),
    "Stream": ("流水声", WATER),
    "Waterfall": ("较大水声", WATER),
}
AST_SPEECH_NAMES = {
    "Speech", "Male speech, man speaking", "Female speech, woman speaking",
    "Child speech, kid speaking", "Hubbub, speech noise, speech babble",
}
# 命中这些且分数最高 → 这段不认。键盘放这里是因为它和活动声一样是宽带
# 瞬态，但用户要的是"人的活动"，打字不算。
AST_NOISE_NAMES = {
    "Mechanical fan", "Wind", "Wind noise (microphone)",
    "Traffic noise, roadway noise", "Silence", "Noise", "Environmental noise",
    "White noise", "Pink noise", "Static", "Hiss",
    "Typing", "Computer keyboard", "Clicking", "Clickety-clack",
    "Inside, small room", "Inside, large room or hall",
}

# SenseVoice 自带的 AED 事件 → 中文。它只有 10 类，且**不含**脚步/水声/
# 家具声，所以只能当补充，主力是 AST。
SENSEVOICE_ZH = {
    "Cry": "啼哭", "Laughter": "笑声", "Cough": "咳嗽", "Sneeze": "喷嚏",
    "Applause": "掌声", "BGM": "背景音乐", "Breath": "呼吸声",
    "VocalMusic": "人声演唱", "InstrumentalMusic": "器乐演奏",
}
# 老总账条目里存的是英文标签，读的时候照样映射
LEGACY_EVENT_ZH = dict(SENSEVOICE_ZH)

DEFAULT_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"

# DSP 粗筛默认阈值（config.json 的 event_screen 可覆盖）。
# 校准数据（72 条 ESC-50 真实样本 + 278 段真实录音，见 test_events.py）：
#   留下的：啼哭(谐波性高，走语音闸) 脚步 吱嘎 刮擦 敲门 倒水 冲水 水滴
#   毙掉的：风扇 洗衣机(无脉冲无高频) 键盘(交给 AST 判噪声，只费 CPU)
# 两条试过又删掉的规则，别再手贱加回来：
#   - "稀疏+亮+间隔短=键盘"否决：真实啼哭/吱嘎/倒水的脉冲间隔也是 0.21s，
#     和键盘完全重叠，误杀一片（ESC-50 上 1/3 的啼哭被它毙掉）
#   - "3~30Hz 调幅峰度=刮擦"：洗衣机峰度 57.9、手锯 40~82，同一个量级，
#     毫无区分度，只会把风雨声全放进来
SCREEN_DEFAULTS = {
    "onsets_min": 2,        # 至少这么多个脉冲（脚步/敲击/水滴）
    "rise_min": 15.0,       # 单个重击的上升沿下限（dB），单次巨响靠它认
    "hf_ratio_min": 0.15,   # 3kHz 以上能量占比（水流嘶声；风扇 <0.05）
    "hf_sustain_min": 0.40, # 高频路要求的持续度（水流是持续的，键盘是散的）
}


def screen_cfg(cfg: dict | None) -> dict:
    s = dict(SCREEN_DEFAULTS)
    s.update((cfg or {}).get("event_screen") or {})
    return s


# ---------------------------------------------------------------- DSP 粗筛

def _frames(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    n = 1 + (len(x) - win) // hop
    if n <= 0:
        return np.zeros((0, win), dtype=np.float32)
    shape = (n, win)
    strides = (x.strides[0] * hop, x.strides[0])
    return np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides)


def dsp_features(pcm_int16: np.ndarray, sr: int) -> dict:
    """一段音频的活动声特征画像。纯 numpy，单段（≤12s）约 20ms。

    返回的键：
        harm        谐波性（复用 vquality，>0.45 判说话）
        dyn         有声部分能量动态（dB）
        hf_ratio    3kHz 以上能量占总能量的比例
        onsets      合并后的脉冲个数（200ms 内的多个算一个）
        rise_max    最强脉冲的上升沿（dB），单次重击靠它认
        ioi_med     相邻脉冲的中位间隔（秒），键盘点击靠它和脚步分开
        sustain     RMS ≥ 峰值 25% 的帧占比（持续 vs 稀疏）
        centroid    平均谱质心（Hz）
        am_mid      3~30Hz 调幅峰度（峰/均值）。**诊断用，粗筛不用它** ——
                   实测洗衣机 57.9 vs 手锯 40~82，分不开（见 SCREEN_DEFAULTS）
        voiced_frac RMS ≥ 峰值 5% 的帧占比
        dur         时长（秒）
    """
    import vquality

    x = np.asarray(pcm_int16, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x / 32768.0
    n = len(x)
    dur = n / float(sr)
    out = {"dur": round(dur, 2), "harm": 0.0, "dyn": 0.0, "hf_ratio": 0.0,
           "onsets": 0, "rise_max": 0.0, "ioi_med": 0.0, "sustain": 0.0,
           "centroid": 0.0, "am_mid": 0.0, "voiced_frac": 0.0}
    if n < sr * 0.05:
        return out

    out["harm"] = round(vquality.harmonicity(x, sr), 4)
    out["dyn"] = round(vquality.energy_dynamics(x, sr), 3)

    win = int(sr * 0.025)
    hop = int(sr * 0.010)
    fr = _frames(x, win, hop)
    if fr.shape[0] < 4:
        return out
    w = np.hanning(win).astype(np.float32)
    e = np.sqrt((fr ** 2).mean(axis=1)) + 1e-12
    emax = float(e.max())
    out["voiced_frac"] = round(float((e > emax * 0.05).mean()), 3)
    out["sustain"] = round(float((e > emax * 0.25).mean()), 3)

    # ---- 脉冲：RMS 在 60ms 内比前 400ms 的局部最小值高 12dB 以上算一个起沿，
    #      200ms 内的多个起沿合并成一个（一次脚步可能有两三下）。
    #      为什么是 12dB 而不是 8dB：实测真实风扇/空调噪声的包络起伏只有
    #      3dB 上下，8dB 门槛会把噪声纹理数出上百个"脉冲"（278 段真实录音
    #      回归时噪声段 onsets 高达 125），粗筛就退化成了"全都留下"。
    prev = max(1, int(0.400 / 0.010))
    lo = np.array([e[max(0, i - prev):i + 1].min() for i in range(len(e))])
    rise = 20 * np.log10((e + 1e-12) / (lo + 1e-12))
    hit = np.where(rise >= 12.0)[0]
    merged: list[int] = []
    for i in hit:
        if merged and i - merged[-1] <= 20:      # 200ms
            continue
        merged.append(int(i))
    out["onsets"] = len(merged)
    if merged:
        out["rise_max"] = round(min(float(max(rise[i] for i in merged)), 60.0), 1)
        if len(merged) >= 2:
            iois = np.diff(merged) * 0.010
            out["ioi_med"] = round(float(np.median(iois)), 3)

    # ---- 频谱：平均幅度谱，算高频占比和质心
    X = np.abs(np.fft.rfft(fr * w, axis=1)).mean(axis=0)
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    tot = float((X ** 2).sum()) + 1e-12
    out["hf_ratio"] = round(float((X[freqs >= 3000.0] ** 2).sum() / tot), 4)
    out["centroid"] = round(float((X * freqs).sum() / (X.sum() + 1e-12)), 1)

    # ---- 调幅峰度：RMS 包络去掉 300ms 滑动均值后做 FFT，取 3~30Hz 带的
    #      **峰值 / 均值**。为什么不是功率占比：随机噪声的包络功率本身就铺满
    #      0~50Hz（10ms 一跳的包络，奈奎斯特只有 50Hz），占比对噪声也有 0.5+，
    #      毫无区分度。刮擦的 stick-slip 是在某个固定频率上的窄峰，峰度能到
    #      6 以上；稳态噪声接近 1。
    k = max(3, int(0.300 / 0.010))
    if len(e) > k * 3:
        ker = np.ones(k) / k
        slow = np.convolve(e, ker, mode="same")
        env = e - slow
        env = env - env.mean()
        E = np.abs(np.fft.rfft(env)) ** 2
        f = np.fft.rfftfreq(len(env), 0.010)
        band = E[(f >= 3.0) & (f <= 30.0)]
        if len(band):
            out["am_mid"] = round(float(band.max() / (band.mean() + 1e-12)), 2)
    return out


def screen_activity(f: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """DSP 粗筛：这段不谐波的音频，值不值得当"活动声"留下来精筛。

    三条"留"的路：
        1. 脉冲够多，或单个脉冲够重 —— 脚步、敲击、碰撞、水滴
        2. 够亮且够持续 —— 水流嘶声（风扇是暗的，说话谐波性高不来这）

    设计原则是**宁可错放，等 AST 毙**：粗筛只负责"别把明摆着的活动声
    提前扔掉"，精度由第二级管。键盘/鼠标/风雨会被这条路放进来，AST 对
    它们的判定很稳（键盘 3/3、鼠标 2/3 判噪声），多花的只是一次推理。
    反过来，任何"看着像噪声就先毙"的规则都会误杀真事件 —— 啼哭的脉冲
    间隔和键盘一模一样，刮擦的调幅和洗衣机一模一样（见 SCREEN_DEFAULTS
    上面记的校准数据）。
    """
    s = screen_cfg(cfg)
    if f.get("dur", 0) < 0.6:
        return False, "太短"
    if f.get("onsets", 0) >= s["onsets_min"]:
        return True, f"脉冲×{f['onsets']}"
    if (f.get("onsets", 0) >= 1 and f.get("rise_max", 0) >= s["rise_min"]):
        return True, f"重击{min(f['rise_max'], 60):.0f}dB"
    if (f.get("hf_ratio", 0) >= s["hf_ratio_min"]
            and f.get("sustain", 0) >= s["hf_sustain_min"]):
        return True, f"高频{f['hf_ratio']:.2f}且持续"
    return False, "稳态噪声"


# ---------------------------------------------------------------- AST 精筛

class EventClassifier:
    """AST AudioSet 527 类分类器。懒加载：不调用就不 import torch。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.model_id = self.cfg.get("event_model") or DEFAULT_MODEL
        # 标签下限。校准（72 条 ESC-50）：真实事件顶分 0.11~0.99，噪声里
        # 人类类别的最高泄漏只有 0.031（引擎→门响）。0.10 有 3 倍余量，
        # 再低就会开始收引擎/冲水这类模型没把握的猜测。
        self.min_score = float(self.cfg.get("event_min_score", 0.10))
        self._model = None
        self._fe = None
        self._map: dict[int, tuple[str, str]] = {}   # idx → (中文, 类别)

    # -- 生命周期 ----------------------------------------------------------
    def _ensure(self) -> bool:
        if self._model is not None:
            return True
        try:
            import torch
            from transformers import (AutoFeatureExtractor,
                                      AutoModelForAudioClassification)
        except Exception as e:                       # 缺依赖就当没有模型
            log.warning("AST 不可用（%s），活动声只剩 DSP 粗筛", e)
            return False
        try:
            t0 = __import__("time").time()
            self._fe = AutoFeatureExtractor.from_pretrained(self.model_id)
            model = AutoModelForAudioClassification.from_pretrained(self.model_id)
            model.eval()
            self._model = model
            # 英文标签名 → 下标。名字对不上（换了 checkpoint）就跳过该类，
            # 绝不会把 A 类的分数安到 B 类头上。
            name2idx = {}
            for i, l in model.config.id2label.items():
                name2idx[str(l).strip().lower()] = int(i)
            for en, (zh, cat) in AST_BY_NAME.items():
                i = name2idx.get(en.lower())
                if i is not None:
                    self._map[i] = (zh, cat)
                else:
                    log.info("AudioSet 里没有标签 %r，跳过", en)
            log.info("AST 就绪 %s（%d 个活动类）用时 %.1fs",
                     self.model_id, len(self._map), __import__("time").time() - t0)
            return True
        except Exception as e:
            log.warning("AST 加载失败（%s），活动声只剩 DSP 粗筛", e)
            return False

    @property
    def available(self) -> bool:
        return self._ensure()

    # -- 推理 --------------------------------------------------------------
    def classify_wav(self, wav) -> dict:
        """给一个 wav 打标签。返回 {category, labels, top, ok}。

        category: baby/activity/water/speech/noise/other
        labels:   [{"zh","score","category"}]，按分数降序，最多 3 条，
                  只收分数 ≥ min_score 的人类活动类
        top:      分数最高的已映射标签中文名（含噪声类），没有则空
        ok:       False 表示模型不可用/推理失败
        """
        if not self._ensure():
            return {"ok": False, "category": OTHER, "labels": [], "top": ""}
        try:
            import soundfile as sf
            import torch

            x, sr = sf.read(str(wav), dtype="float32", always_2d=False)
            if x.ndim > 1:
                x = x.mean(axis=1)
            if sr != 16000:                    # AST 按 16k 训练
                try:
                    import librosa
                    x = librosa.resample(x, orig_sr=sr, target_sr=16000)
                    sr = 16000
                except Exception:
                    x = x[::max(1, sr // 16000)]
                    sr = 16000
            inputs = self._fe(x, sampling_rate=sr, return_tensors="pt")
            with torch.no_grad():
                probs = torch.softmax(self._model(**inputs).logits, dim=-1)[0]
            order = torch.argsort(probs, descending=True)[:10]
            speech_map = self._speech_map()
            noise_map = self._noise_map()
            hits: list[tuple[str, str, float]] = []      # (中文, 类别, 分数)
            for i in order.tolist():
                p = float(probs[i])
                if p < 0.01:
                    continue
                if i in self._map:
                    zh, cat = self._map[i]
                elif i in speech_map:
                    zh, cat = "人声", SPEECH
                elif i in noise_map:
                    zh, cat = noise_map[i], NOISE
                else:
                    continue
                hits.append((zh, cat, p))
            if not hits:
                return {"ok": True, "category": OTHER, "labels": [], "top": ""}
            top_zh, top_cat, top_p = hits[0]
            labels = []
            seen: set[str] = set()
            for zh, cat, p in hits:
                if cat in (NOISE, SPEECH, OTHER) or zh in seen:
                    continue
                if p < self.min_score:
                    continue
                seen.add(zh)
                labels.append({"zh": zh, "score": round(p, 3), "category": cat})
                if len(labels) >= 3:
                    break
            return {"ok": True, "category": top_cat, "labels": labels,
                    "top": top_zh, "top_score": round(top_p, 3)}
        except Exception as e:
            log.warning("AST 推理失败 %s：%s", wav, e)
            return {"ok": False, "category": OTHER, "labels": [], "top": ""}

    def _speech_map(self) -> dict[int, tuple[str, str]]:
        if not hasattr(self, "_sp_cache"):
            name2idx = {str(l).strip().lower(): int(i)
                        for i, l in self._model.config.id2label.items()}
            self._sp_cache = {name2idx[n.lower()]: ("人声", SPEECH)
                              for n in AST_SPEECH_NAMES
                              if n.lower() in name2idx}
        return self._sp_cache

    def _noise_map(self) -> dict[int, str]:
        if not hasattr(self, "_nz_cache"):
            name2idx = {str(l).strip().lower(): int(i)
                        for i, l in self._model.config.id2label.items()}
            self._nz_cache = {name2idx[n.lower()]: _noise_zh(n)
                              for n in AST_NOISE_NAMES
                              if n.lower() in name2idx}
        return self._nz_cache


_NOISE_ZH = {
    "Mechanical fan": "风扇", "Wind": "风声", "Wind noise (microphone)": "麦克风风噪",
    "Traffic noise, roadway noise": "交通噪声", "Silence": "静音", "Noise": "噪声",
    "Environmental noise": "环境噪声", "White noise": "白噪声", "Pink noise": "粉噪声",
    "Static": "静电噪声", "Hiss": "嘶声",
    "Typing": "键盘打字", "Computer keyboard": "键盘", "Clicking": "点击声",
    "Clickety-clack": "咔哒声", "Inside, small room": "室内底噪",
    "Inside, large room or hall": "室内底噪",
}


def _noise_zh(name: str) -> str:
    return _NOISE_ZH.get(name, "噪声")


# ---------------------------------------------------------------- 标签合并

def sensevoice_zh(tags) -> list[dict]:
    """SenseVoice 的英文 AED 标签 → 中文标签列表。"""
    out = []
    for t in tags or []:
        zh = SENSEVOICE_ZH.get(t)
        if zh:
            out.append({"zh": zh, "score": 1.0, "category": "sensevoice",
                        "src": "sensevoice"})
    return out


def merge_labels(*groups) -> list[dict]:
    """把多来源标签按中文名去重合并，分数取最高，按分数降序。"""
    best: dict[str, dict] = {}
    for g in groups:
        for lb in g or []:
            zh = lb.get("zh")
            if not zh:
                continue
            if zh not in best or lb.get("score", 0) > best[zh].get("score", 0):
                best[zh] = lb
    return sorted(best.values(), key=lambda d: -d.get("score", 0))


def parse_labels(raw) -> list[str]:
    """读库里存的 labels 列（JSON），返回中文名列表。

    兼容三种历史格式：list[dict]（新）、list[str]（中文名字符串）、
    以及根本没有这一列的老行——老行的英文事件在 events 列，由调用方
    另外过一遍 LEGACY_EVENT_ZH。
    """
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    out: list[str] = []
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict):
            zh = d.get("zh")
        else:
            zh = d
        if zh and zh not in out:
            out.append(str(zh))
    return out
