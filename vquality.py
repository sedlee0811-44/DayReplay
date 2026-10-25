"""录音质量的判定与增强。挂在录音进程（裁决）和转写进程（增强）两头。

为什么需要这个模块
------------------
离线回放真实的 240 段录音得到一张很干脆的表：

    段长        有识别出字的比例
    < 8 秒      69%
    8~15 秒     31%
    > 15 秒     12%

差这么多不是模型偏心短段。真实因果是反过来的 **—— webrtcvad 只回答
"这一帧响不响"，回答不了"这是不是人说话"。风扇、空调、主机轰鸣这类
宽带稳态噪声能量足够、在时间上连续，VAD 会一直判成语音，段就永远
不满足 silence_close_ms 的关闭条件，一路撑到 max_segment_s 被硬切开。
换句话说：**越长的段，越有可能压根不是人在说话。**

所以要补两个东西：
    1. 一个便宜的"这是不是人声"的判据 —— 谐波性（见 harmonicity）
    2. 一个不要把长段直接塞给模型的收尾策略 —— 见下面的 max_segment_s

判据的选择也是量出来的，不是拍的。同一批数据上试过三个：
    频谱平坦度    几乎无效（本案落对人声和噪声的 flatness 都很低）
    能量动态      区分度弱（6.24 vs 5.15 dB）
    谐波性        有效（0.657 vs 0.387），阈值 0.45 能清掉 91% 的无效段
                  同时保住 87% 的有效段

为什么谐波性管用：汉语里绝大部分内容是浊音，声带周期性振动会在
2~12ms 的滞后区间上顶起一个自相关峰；风扇噪声是宽带随机的，自相关
除了 0 点以外一路平坦。这个差别不依赖音量大小，因此很适合做二次裁决。

离线实测这条线上（asr_probe.py / audio_probe.py 两个脚本留着，随时复现）：
    2.8 秒的真人说话 → "你跟你妈妈说明了，明天要做饭。"（准确）
    30 秒的噪声段   → 怎么切怎么增强都是 "。"（因为里面本来就没话）
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("vquality")

WIN_MS = 25.0
HOP_MS = 10.0
# 基频 80~500Hz 对应的滞后区间
LAG_LO_MS = 2.0
LAG_HI_MS = 12.0


def _frame(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    """把一维信号切成重叠帧，返回 [帧数, 帧长]。"""
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    n = 1 + (len(x) - win) // hop
    shape = (n, win)
    strides = (x.strides[0] * hop, x.strides[0])
    return np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides)


def harmonicity(x: np.ndarray, sr: int) -> float:
    """整段的谐波性得分，0~1。越高越像有人在说话。

    做法：逐帧算自相关，取基频滞后区间（2~12ms）上的峰值高度，
    再对有声帧取 75 分位。取高分位而不是中位数，是因为一句话里
    总有几帧是清音/过渡，中位数会被它们拉低。
    """
    if len(x) < sr * 0.05:
        return 0.0
    win = int(sr * WIN_MS / 1000)
    hop = int(sr * HOP_MS / 1000)
    lo = max(1, int(sr * LAG_LO_MS / 1000))
    hi = min(int(sr * LAG_HI_MS / 1000), win - 1)
    if hi <= lo:
        return 0.0

    fr = _frame(np.asarray(x, dtype=np.float32), win, hop)
    w = np.hanning(win).astype(np.float32)
    X = np.fft.rfft(fr * w, n=2 * win, axis=1)
    # 自相关 = |X|^2 的逆变换；除以能量归一化到 +-1
    ac = np.fft.irfft(np.abs(X) ** 2, axis=1)[:, :win]
    e0 = ac[:, 0:1] + 1e-12
    peak = ac[:, lo:hi].max(axis=1)
    score = np.clip(peak / e0[:, 0], 0.0, 1.0)

    # 只统计有声部分：能量低于峰值 5% 的帧是间隙，它们没有谐波可言
    energy = np.sqrt((fr ** 2).mean(axis=1)) + 1e-12
    voiced = energy > energy.max() * 0.05
    if voiced.sum() < 3:
        return 0.0
    return float(np.percentile(score[voiced], 75))


def energy_dynamics(x: np.ndarray, sr: int) -> float:
    """有声部分的能量起伏（dB 域标准差）。说话一般 > 5dB，稳态噪声偏小。"""
    win = int(sr * WIN_MS / 1000)
    hop = int(sr * HOP_MS / 1000)
    if len(x) < win * 2:
        return 0.0
    fr = _frame(np.asarray(x, dtype=np.float32), win, hop)
    e = np.sqrt((fr ** 2).mean(axis=1)) + 1e-12
    voiced = e > e.max() * 0.05
    if voiced.sum() < 3:
        return 0.0
    edb = 20 * np.log10(e[voiced] / e[voiced].max() + 1e-12)
    return float(np.std(edb))


def snapshot(pcm_int16: np.ndarray, sr: int) -> dict:
    """一段音频的质量画像。裁决和日志都用它。"""
    x = np.asarray(pcm_int16, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x / 32768.0
    return {
        "harm": round(harmonicity(x, sr), 4),
        "dyn": round(energy_dynamics(x, sr), 3),
        "dur": round(len(x) / float(sr), 2),
    }


def is_human(pcm_int16: np.ndarray, sr: int, min_harm: float) -> tuple[bool, dict]:
    """最后这道闸回答一个问题：这段里像不像有人在说话。

    注意它**不看音量**。机箱风扇能录到 RMS 1500+，比远处说话还响，
    所以"够不够响"这条判据在这里只会帮倒忙 —— 只会把噪声放行。
    """
    q = snapshot(pcm_int16, sr)
    return q["harm"] >= min_harm, q


# ---------------------------------------------------------------- 前端增强

def trim_edges(x: np.ndarray, sr: int, top_db: float = 30.0,
               keep_s: float = 0.06) -> np.ndarray:
    """掐掉首尾的静音尾巴。

    两头各留 60ms 免得切掉起字和收字的辅音。长段前后常常吊着几百毫秒
    纯噪声，它们会稀释下面归一化的增益。
    """
    n = max(1, int(sr * 0.02))
    if len(x) < n * 4:
        return x
    m = len(x) // n
    e = np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)
    thr = e.max() * 10 ** (-top_db / 20.0)
    idx = np.where(e >= thr)[0]
    if not len(idx):
        return x
    pad = int(keep_s / 0.02)
    a = max(0, (idx[0] - pad) * n)
    b = min(len(x), (idx[-1] + 1 + pad) * n)
    return x[a:b] if b - a > n else x


def highpass(x: np.ndarray, sr: int, cutoff: float = 60.0) -> np.ndarray:
    """去掉低频轰鸣与直流漂移 —— USB 摄像头麦的常见毛病。

    对 ASR 的帮助有限（SenseVoice 内部有自己的前端），但戴上耳机复核时
    声音干净得多；代价是一次 sosfilt，可以忽略。
    """
    try:
        from scipy.signal import butter, sosfilt
    except Exception:
        return x
    sos = butter(4, cutoff, "highpass", fs=sr, output="sos")
    return sosfilt(sos, x).astype(np.float32)


def normalize(x: np.ndarray, target_dbfs: float = -20.0,
              ceiling_dbfs: float = -3.0) -> np.ndarray:
    """整段增益到目标电平，同时保证不削顶。

    摄像头麦是远场拾音，录进来的电平比模型的训练分布低一截。
    归一化把它拉回 -20dBFS 这个常见基准，等价于"把音量拧到合适"，
    不改变任何语义内容 —— 所以它对已经清楚的短段几乎无影响，
    只对偏轻的段有实打实的帮助。
    """
    r = float(np.sqrt(np.mean(x ** 2)) + 1e-12)
    if r <= 0:
        return x
    g = 10 ** (target_dbfs / 20.0) / r
    peak = float(np.max(np.abs(x)) + 1e-12)
    ceil = 10 ** (ceiling_dbfs / 20.0)
    if peak * g > ceil:          # 别因为整体很轻就把某一下戳Б到爆音
        g = ceil / peak
    return np.clip(x * g, -1.0, 1.0).astype(np.float32)


def enhance(pcm_int16: np.ndarray, sr: int, target_dbfs: float = -20.0,
            do_hp: bool = True) -> np.ndarray:
    """送进模型之前的三件套：掐静音 → 高通 → 归一化。

    返回 float32，幅值 +-1。离线对照（asr_probe.py）里这一套对清晰的
    短段是中性的（"说明书" "你跟你妈妈说明吧"，语义没变），对偏轻的
    段有微弱改善；它的真正作用是让同一套参数在不同响度下都成立。
    """
    x = np.asarray(pcm_int16, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x / 32768.0
    x = trim_edges(x, sr)
    if do_hp:
        x = highpass(x, sr)
    return normalize(x, target_dbfs)
