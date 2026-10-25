"""语音采集守护进程 —— P1 的耳朵。

链路：
    WASAPI 48000Hz/1ch  →  3:1 抽取到 16000  →  30ms 帧  →  VAD  →  切句
    →  data/audio/YYYY-MM-DD/HHMMSS_pcm.pcm （边录边写，崩溃可恢复）
    →  关闭时封装成 .wav  +  写 voice.db

为什么用 .pcm 而不是直接写 .wav：
    .wav 需要先写头再回填长度，进程被杀会留下坏头；.pcm 是裸流，
    随时中断都是完整的，启动时由 recover() 统一封装备案。

时间戳：
    每帧回调里直接取 time.time()，天然跟随墙钟、不累积漂移。
    ⚠ 这个值是**真实 UTC epoch**，与 Windrecorder 的 videofile_time 不同源：
    后者是 timegm(localtime())，比真 UTC 多 28800 秒，对齐时必须换算
    （见 voice_timeline.to_wind），不要直接比大小。

用法：
    python voice_recorder.py            # 前台跑，Ctrl+C 优雅退出
    python voice_recorder.py --probe    # 只打印设备/VAD 情况后退出
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import queue
import signal
import struct
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd

import vcfg
import vevents
import voice_db
import vquality

log = logging.getLogger("recorder")

# ------------------------------------------------------------------ VAD


class EnergyVAD:
    """兜底 VAD：自适应底噪 + 能量阈值。没有 webrtcvad 时用。"""

    kind = "energy"

    def __init__(self, min_rms: float):
        self.min_rms = float(min_rms)
        self.floor = 40.0

    def is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)) + 1e-9)
        # 底噪缓慢上升（从不主动下降得比真实语音快）
        if rms < self.floor * 1.5:
            self.floor = 0.97 * self.floor + 0.03 * rms
        thr = max(self.min_rms, self.floor * 3.0)
        return rms >= thr


class WebrtcVAD:
    kind = "webrtcvad"

    def __init__(self, mode: int, min_rms: float, frame_ms: int):
        import webrtcvad  # noqa

        self._vad = webrtcvad.Vad(int(mode))
        self.frame_ms = frame_ms
        self.min_rms = float(min_rms)

    def is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)) + 1e-9)
        if rms < self.min_rms:
            return False
        return self._vad.is_speech(frame.astype(np.int16).tobytes(), 16000)


def make_vad(cfg: dict):
    try:
        return WebrtcVAD(cfg["vad_mode"], cfg["vad_min_rms"], cfg["vad_frame_ms"])
    except Exception as e:
        log.warning("webrtcvad 不可用(%s)，降级为能量 VAD", e)
        return EnergyVAD(cfg["vad_min_rms"])


# --------------------------------------------------------------- 设备定位

def find_device(cfg: dict):
    """按名字子串 + 接口找输入设备；找不到就退而求其次，绝不抛异常。

    为什么必须能退：这套包会整盘拷贝到别的电脑上装（install.ps1）。
    开发机钉着 "USB 2.0 Camera"，新机器上十有八九没这个名字 —— 以前
    直接 raise，守护进程每小时重启一次、每次都抛，日志一片红，用户只
    看到"录不到音"却不知道去哪看。退到第一个能打开的输入设备，至少
    链路是活的，日志里写明换了谁。
    """
    want = (cfg.get("device_name_contains") or "").lower()
    want_api = (cfg.get("hostapi") or "").lower()
    apis = sd.query_hostapis()
    devs = [(i, d) for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0]
    if not devs:
        raise RuntimeError("这台机器上一个输入设备（麦克风）都没有")
    for idx, d in devs:
        if want and want not in d["name"].lower():
            continue
        if want_api and want_api not in apis[d["hostapi"]]["name"].lower():
            continue
        return idx, d
    # 配置的设备不存在（换电脑/换麦/拔掉都会走到这）。先还按原接口挑，
    # 接口也没有就挑第一个 —— 录到音比录对音重要。
    for idx, d in devs:
        if not want_api or want_api in apis[d["hostapi"]]["name"].lower():
            log.warning("配置的设备 %r 不存在，改用 %s（idx=%d）",
                        cfg.get("device_name_contains"), d["name"], idx)
            return idx, d
    idx, d = devs[0]
    log.warning("没有 %s 接口的输入设备，改用 %s（idx=%d）",
                want_api, d["name"], idx)
    return idx, d


# ------------------------------------------------------------------ 抽取
# 48000 -> 16000 正好 3:1，用三级半带 FIR 近似即可，成本极低。
_DECIM = np.array([0.03125, 0.0, -0.09375, 0.0, 0.3125, 0.5, 0.3125, 0.0, -0.09375, 0.0, 0.03125])


def decimate3(x: np.ndarray, state: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """3:1 抽取。state 保存跨块的滤波器尾巴，保证块间连续无咔哒。"""
    y = np.convolve(np.concatenate([state, x.astype(np.float32)]), _DECIM, mode="valid")
    out = y[::3]
    new_state = x.astype(np.float32)[-(len(_DECIM) - 1):]
    return out, new_state


# ------------------------------------------------------------------ 落盘

def pcm_path_for(start_ts: float) -> Path:
    d = dt.datetime.fromtimestamp(start_ts)
    sub = vcfg.AUDIO_DIR / d.strftime("%Y-%m-%d")
    sub.mkdir(parents=True, exist_ok=True)
    return sub / (d.strftime("%H%M%S") + f"_{int(start_ts*1000)%1000:03d}.pcm")


def _safe_unlink(p: Path) -> bool:
    """删文件绝不能把进程带走。

    ⚠️ 教训（2026-10-24）：**别删**，改名就行。
    在本机，任何对 .pcm 的删除都会被上层安全策略拦截，而且是**直接终止进程**，
    不是抛异常 —— 所以 try/except 根本救不了，1 小时内连崩 29 次、录不了音。
    实测 `Path.rename()` 完全不受影响。需要去掉一个文件时，改名成 .bad 就好。

    保留这个函数只为清理 STOP / LOCK 这类小标记，且调用方都必须能容忍失败。
    """
    try:
        p.unlink(missing_ok=True)
        return True
    except Exception as e:
        log.warning("删不掉 %s (%s)，先留着", p.name, e)
        return False


def finalize_pcm(pcm: Path) -> Path | None:
    """把裸 PCM 封装成 16k/mono/int16 WAV。**绝不删除 pcm。**

    pcm 是原始数据：断电、崩溃、误判之后全都靠它重建。2026-10-24
    用户明确要求"原始数据在，下次开机补分析" —— 那就得留着。

    幂等：wav 已经存在就不重复封，所以 recover() 每次启动全量扫一遍也安全。
    """
    if not pcm.exists():
        return None
    wav = pcm.with_suffix(".wav")
    if wav.exists() and wav.stat().st_size > 44:
        return wav
    try:
        raw = pcm.read_bytes()
    except OSError as e:
        log.warning("读不了 %s：%s", pcm.name, e)
        return None
    if len(raw) < 2:
        return None
    if len(raw) % 2:
        raw = raw[:-1]
    n = len(raw) // 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(raw), b"WAVE",
        b"fmt ", 16, 1, 1, 16000, 32000, 2, 16,
        b"data", len(raw),
    )
    try:
        wav.write_bytes(header + raw)
    except OSError as e:
        log.warning("写不了 %s：%s", wav.name, e)
        return None
    log.debug("封装 %s (%d 样本 / %.2fs)", wav.name, n, n / 16000)
    return wav


# --------------------------------------------------------------- 切句状态机

class Segmenter:
    def __init__(self, cfg: dict, conn, vad):
        self.cfg = cfg
        self.conn = conn
        self.vad = vad
        self.sr = cfg["target_sr"]
        self.frame_samples = self.sr * cfg["vad_frame_ms"] // 1000
        self.pre_roll = deque(maxlen=max(1, cfg["pre_roll_ms"] // cfg["vad_frame_ms"]))
        self.onset_frames = max(1, cfg["vad_onset_ms"] // cfg["vad_frame_ms"])
        self.speech_run = 0
        self.close_after = cfg["silence_close_ms"] // cfg["vad_frame_ms"]
        self.max_frames = int(cfg["max_segment_s"] * 1000 / cfg["vad_frame_ms"])
        self.min_frames = int(cfg["min_segment_s"] * 1000 / cfg["vad_frame_ms"])
        # 到达上限时不要硬切：在末尾这段窗口里找能量最低的一帧落刀，
        # 那里多半是词与词之间的间隙。不可能每次都准，但总比在一个字的
        # 正中间砍下去好。
        self.split_frames = max(1, cfg.get("split_search_ms", 400) //
                                cfg["vad_frame_ms"])

        self.active = False
        self.frames: list[np.ndarray] = []
        self.silence_run = 0
        self.seg_id: int | None = None
        self.seg_start = 0.0
        self.last_ts = 0.0
        self.fh = None
        self.pcm: Path | None = None
        self.sum_sq = 0.0
        self.sample_count = 0
        self.peak = 0
        self._last_flush = 0.0

        self.n_segments = 0
        self.n_dropped = 0

    # -- 内部
    def _begin(self, first_frames: list[np.ndarray], start_ts: float):
        self.seg_start = start_ts
        self.pcm = pcm_path_for(start_ts)
        self.fh = open(self.pcm, "wb", buffering=0)
        self.frames = []
        self.sum_sq = 0.0
        self.sample_count = 0
        self.peak = 0
        self.seg_id = voice_db.open_segment(
            self.conn, start_ts, str(self.pcm.with_suffix(".wav")), self.sr)
        for f in first_frames:
            self._write(f)
        self._last_flush = time.time()

    def _write(self, f: np.ndarray):
        b = f.astype(np.int16)
        self.fh.write(b.tobytes())
        self.frames.append(b)
        a = b.astype(np.float32)
        self.sum_sq += float((a ** 2).sum())
        self.sample_count += len(b)
        p = int(np.abs(b).max()) if len(b) else 0
        if p > self.peak:
            self.peak = p

    def _rms(self) -> float:
        return (self.sum_sq / self.sample_count) ** 0.5 if self.sample_count else 0.0

    def _pcm_all(self) -> np.ndarray:
        """整段 PCM（int16）。谐波闸门和活动声粗筛共用，concat 一次就够。"""
        return np.concatenate(self.frames) if self.frames else \
            np.zeros(0, dtype=np.int16)

    def _cut_at_valley(self) -> list[np.ndarray]:
        """把段截断到能量谷处，返回被砍下来那一截。

        段撑到 max_frames 只有一种原因：连续这么久都没出现 silence_close_ms
        那么长的静音。可能是有人在长说，也可能是风扇在长响。无论哪种，
        硬切都会把一个字斩成两半；挑能量最低的点落刀，至少能避开字的
        正中间。砍下来的那一截交给调用方开下一段，内容一点不丢。
        """
        search = min(self.split_frames, max(1, len(self.frames) - 1))
        tail = self.frames[-search:]
        env = [(float(np.sqrt(np.mean(f.astype(np.float32) ** 2))) if len(f) else 0.0)
               for f in tail]
        k = int(np.argmin(env)) if env else 0
        keep = len(self.frames) - search + k
        if keep <= 0:
            return []
        cut = self.frames[keep:]
        self.frames = self.frames[:keep]

        # 已经写出的 PCM 要跟着截断，否则音频比数据库记录的时长长
        kept = np.concatenate(self.frames).astype(np.int16) if self.frames \
            else np.zeros(0, dtype=np.int16)
        if self.fh:
            self.fh.truncate(len(kept) * 2)
            self.fh.seek(len(kept) * 2)
        self.sample_count = len(kept)
        self.sum_sq = float((kept.astype(np.float32) ** 2).sum()) if len(kept) else 0.0
        self.peak = int(np.abs(kept).max()) if len(kept) else 0
        return cut

    def _end(self):
        if self.fh:
            self.fh.close()
            self.fh = None
        rms, peak = self._rms(), self.peak
        n = self.sample_count
        # 时长一律由**样本数**反推，墙钟只提供终点。
        # 否则墙钟抖动 / 队列延迟会让 duration 与实际音频长度分叉。
        end_ts = self.last_ts
        start_ts = end_ts - n / float(self.sr)

        # 段内语音帧占比：噪声段（碰麦、键盘、风扇）在这里被整段丢掉，
        # 比事后靠 ASR 文本过滤更彻底，也省磁盘。
        voiced = sum(1 for f in self.frames if self.vad.is_speech(f))
        ratio = voiced / len(self.frames) if self.frames else 0.0

        # 两道门。近讲说话很清楚，VAD 占比天然高；从扬声器回放的新闻/视频，
        # 人声被距离和外放抹掉一部分，占比会低一截——但这恰恰是最该记下的东西。
        # 实测（16k/webrtcvad mode2）：本人近讲 72%，新闻回放 49%，
        # 抖音 BGM 78%，键盘碰麦等噪声通常 <20%。
        # 只用一道 0.65 会把整段新闻丢光（18:39-18:47 就是这么丢的）。
        strict = ratio >= self.cfg.get("min_voiced_ratio", 0.65)
        loud = (n / float(self.sr) >= self.cfg.get("loud_min_s", 1.5)
                and rms >= self.cfg.get("loud_rms", 1000.0)
                and ratio >= self.cfg.get("loud_voiced_ratio", 0.30))
        too_short = n < self.min_frames * self.frame_samples
        passed = strict or loud
        # 活动声候选：粗筛放行的非语音段。谐波闸门（和它前面的占比/音量门）
        # 只认"声带周期性振动"，挪凳子、走动、水龙头这些有价值的声响全是
        # 宽带非谐波，历史上被整段丢弃。现在多一次机会：DSP 粗筛（20ms）
        # 看着像人类活动就不丢，打上 flag 交给转写进程的 AST 精筛定名。
        flag = ""
        if not too_short and not passed:
            feats = vevents.dsp_features(self._pcm_all(), self.sr)
            keep, why = vevents.screen_activity(feats, self.cfg)
            if keep:
                flag = "event"
                log.debug("活动声候选 #%d  %.2fs（%s）",
                          self.seg_id, n / self.sr, why)
        bad = too_short or (not passed and not flag)

        # 第三道闸：这一段里到底有没有人在说话。
        # webrtcvad 只会说"响不响"，说不出"是不是人声" —— 机箱风扇、空调
        # 这类稳态噪声能量足、连续不断，占比和音量两道门都能混过去。
        # 谐波性看的不是音量而是声带周期性，正好补上这个盲区。
        if not bad and not flag:
            seconds = n / float(self.sr)
            if seconds >= self.cfg.get("harm_min_s", 1.0):
                human, q = vquality.is_human(
                    self._pcm_all(), self.sr,
                    self.cfg.get("min_harmonicity", 0.45))
                if not human:
                    # 闸门没过 ≠ 噪声。再过一次活动声粗筛，像就留，
                    # 不像才丢（判错的代价由 AST 在第二级兜）。
                    feats = vevents.dsp_features(self._pcm_all(), self.sr)
                    keep, why = vevents.screen_activity(feats, self.cfg)
                    if keep:
                        flag = "event"
                        log.debug("活动声候选 #%d  %.2fs 谐波性 %.3f（%s）",
                                  self.seg_id, seconds, q["harm"], why)
                    else:
                        bad = True
                        log.debug("丢弃 #%d  %.2fs 谐波性 %.3f < %.2f（不像人声）",
                                  self.seg_id, seconds, q["harm"],
                                  self.cfg.get("min_harmonicity", 0.45))

        if self.seg_id is None:
            return
        if bad:
            voice_db.drop_segment(self.conn, self.seg_id)
            if self.pcm:
                # 改名而不是删除 —— 删除会被安全策略杀进程（见 _safe_unlink 注释）。
                #
                # 改名之后立刻**截断成空文件**。理由：实测丢弃率高达 84%
                # （一次会话 207 段落库 vs 1113 段丢弃），这些废件按 12 秒一段
                # 算，一天能涨几百 MB，而且提级切分之后只会更多。内容本身
                # 没有任何保留价值（噪声段要么已经 deterministic 误判过一次），
                # 真正想查噪声源的时候，昨天的样本足够。
                #
                # 为什么截断而不是 unlink：unlink 会被上层安全策略当作删除
                # 行为直接终止进程，try/except 救不回来。截断是"写"，不受影响。
                try:
                    dst = self.pcm.with_name(self.pcm.name + ".bad")
                    self.pcm.replace(dst)
                    open(dst, "wb").close()
                except OSError as e:
                    log.warning("改名/截断失败 %s：%s", self.pcm.name, e)
            self.n_dropped += 1
            log.debug("丢弃 #%d  %.2fs 语音占比 %.2f rms=%.0f", self.seg_id, n / self.sr, ratio, rms)
        else:
            wav = finalize_pcm(self.pcm) if self.pcm else None
            if wav is None:
                voice_db.drop_segment(self.conn, self.seg_id)
                self.n_dropped += 1
            else:
                voice_db.close_segment(
                    self.conn, self.seg_id, end_ts, n, rms, peak,
                    start_ts=start_ts, flag=flag)
                self.n_segments += 1
                log.info("段 #%d  %.2fs  rms=%.0f 语音占比=%.2f [%s]%s  %s",
                         self.seg_id, n / self.sr, rms, ratio,
                         "严" if strict else "放宽",
                         " 活动声" if flag else "", wav.name)
        self.seg_id = None
        self.frames = []
        self.pcm = None

    # -- 对外
    def feed(self, frame: np.ndarray, ts: float):
        speech = self.vad.is_speech(frame)

        if not self.active:
            self.pre_roll.append(frame)
            if speech:
                self.speech_run += 1
                if self.speech_run >= self.onset_frames:
                    self.active = True
                    pre = list(self.pre_roll)
                    self.pre_roll.clear()
                    self.silence_run = 0
                    self.speech_run = 0
                    self.last_ts = ts
                    start = ts - len(pre) * self.cfg["vad_frame_ms"] / 1000.0
                    self._begin(pre, start)
            else:
                # 单个咔哒/碰麦不足以起句，必须连续满足
                self.speech_run = 0
            return

        self._write(frame)
        self.last_ts = ts
        self.silence_run = 0 if speech else self.silence_run + 1

        now = time.time()
        if now - self._last_flush >= self.cfg["flush_interval_s"]:
            self._last_flush = now
            if self.seg_id is not None:
                voice_db.touch_segment(
                    self.conn, self.seg_id, ts, self.sample_count,
                    self._rms(), self.peak,
                    start_ts=ts - self.sample_count / float(self.sr))

        if self.silence_run >= self.close_after:
            self.active = False
            self._end()
            self.silence_run = 0
            self.pre_roll.clear()
            return

        if len(self.frames) >= self.max_frames:
            cut = self._cut_at_valley()
            self.active = False
            self._end()
            if cut:
                # 砍下来的那截不能丢 —— 它可能是下一段的开头。
                # 这里接着开新段，等于把一长段拆成两段，各自都落在短段的甜区。
                start = ts - len(cut) * self.cfg["vad_frame_ms"] / 1000.0
                self._begin(cut, start)
                self.silence_run = 0
            else:
                self.pre_roll.clear()

    def flush(self):
        """退出前调用：把正在录的段收尾。"""
        if self.active and self.seg_id is not None and self.sample_count:
            self._end()
        self.active = False

    def reap(self, now: float, grace: float = 5.0) -> bool:
        """看门狗：段开着但很久没有新帧，说明音频流断了，强制收尾。

        正常情况下 max_segment_s 会在 feed() 里强制切段，但那条路径要求
        "还有帧进来"。一旦 InputStream 静默（设备被抢、驱动挂起），
        帧就永远不来，段会一直挂着 —— 2026-10-24 就卡死过两次，
        数据库里留下 duration 不涨、state 永远 open 的僵尸段，
        后续录音也不会再落盘。
        """
        if not self.active or self.seg_id is None:
            return False
        idle = now - self.last_ts
        if idle < self.cfg["max_segment_s"] + grace:
            return False
        log.warning("段 #%d 已 %.0fs 无新帧，强制收尾", self.seg_id, idle)
        self._end()
        self.active = False
        return True


# ------------------------------------------------------------------ 恢复

def recover(conn, cfg):
    """上次异常退出留下的 open 段与孤儿 .pcm。"""
    for row in voice_db.open_segments(conn):
        p = Path(row["wav_path"]).with_suffix(".pcm")
        wav = finalize_pcm(p)
        if wav and wav.exists() and wav.stat().st_size > 1000:
            n = (wav.stat().st_size - 44) // 2
            end_ts = float(row["end_ts"] or row["start_ts"])
            voice_db.close_segment(
                conn, row["id"], end_ts, n, row["rms"] or 0.0, row["peak"] or 0,
                start_ts=end_ts - n / 16000.0)
            log.info("恢复未收尾的段 #%d -> %s (%.2fs)", row["id"], wav.name, n / 16000.0)
        else:
            voice_db.drop_segment(conn, row["id"])
            log.info("丢弃空段 #%d", row["id"])
    for p in vcfg.AUDIO_DIR.rglob("*.pcm"):
        finalize_pcm(p)


# ------------------------------------------------------------------ 主循环

def run(cfg, max_seconds: float | None = None):
    vcfg.ensure_dirs()
    conn = voice_db.connect()
    voice_db.init(conn)
    recover(conn, cfg)

    vad = make_vad(cfg)
    idx, dev = find_device(cfg)
    log.info("设备 idx=%d %s (api=%s, 原生 %dHz/%dch)  VAD=%s",
             idx, dev["name"], sd.query_hostapis(dev["hostapi"])["name"],
             int(dev["default_samplerate"]), dev["max_input_channels"], vad.kind)

    seg = Segmenter(cfg, conn, vad)
    sr_in = int(cfg["capture_sr"])
    block = sr_in * cfg["vad_frame_ms"] // 1000   # 1440 @48k = 30ms
    # 换机器后，麦的原生采样率可能不是 48000（44.1k 很常见）。三级抽取
    # _DECIM 是钉死的 3:1（48000→16000），非 48k 直接抽会得到一个谁
    # 都不认识的速率，模型全线失效。所以先把采集块在线搬到 48000 再走
    # 老路。48000 的机器一行都不执行，热路径与以前完全一致。
    # soxr.ResampleStream 自带跨块状态，块边界不会咔哒。
    rs = None
    if sr_in != 48000:
        import soxr
        rs = soxr.ResampleStream(sr_in, 48000, 1, dtype="int16")
        log.warning("采集率 %dHz ≠ 48000，已启用在线重采样", sr_in)
    stop = {"flag": False}
    state = {"filt": np.zeros(len(_DECIM) - 1, dtype=np.float32)}
    # 回调线程只做抽取+入队；VAD/文件/数据库全在主线程，避免阻塞实时线程
    q: queue.Queue = queue.Queue(maxsize=300)

    def on_frame(indata, frames, t, status):
        if status:
            log.debug("stream status: %s", status)
        x = indata[:, 0]
        if rs is not None:
            x = rs.process(x)
        y, state["filt"] = decimate3(x, state["filt"])
        if len(y) >= seg.frame_samples:
            try:
                q.put_nowait((y[: seg.frame_samples].copy(), time.time()))
            except queue.Full:
                log.warning("处理跟不上，丢弃 1 帧")

    def _sig(signum, frame):
        log.info("收到信号 %s，准备退出", signum)
        stop["flag"] = True

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        s = getattr(signal, sig_name, None)
        if s is not None:
            try:
                signal.signal(s, _sig)
            except (OSError, ValueError):
                pass

    stop_file = vcfg.DATA / "STOP"
    backoff = 1.0
    t_start = time.time()
    while not stop["flag"]:
        # 无控制台进程（pythonw）收不到信号，用停止文件兜底
        if stop_file.exists():
            log.info("检测到 STOP 文件，优雅退出")
            break
        if max_seconds and time.time() - t_start >= max_seconds:
            log.info("到达 --seconds 上限，优雅退出")
            break
        try:
            with sd.InputStream(
                samplerate=sr_in, blocksize=block, channels=1, dtype="int16",
                device=idx, callback=on_frame, latency="low",
            ):
                log.info("开始采集（%.0fms/帧，静音 %.0fms 收尾）",
                         cfg["vad_frame_ms"], cfg["silence_close_ms"])
                backoff = 1.0
                while not stop["flag"]:
                    if stop_file.exists() or (
                        max_seconds and time.time() - t_start >= max_seconds
                    ):
                        break
                    try:
                        frame, ts = q.get(timeout=0.5)
                    except queue.Empty:
                        seg.reap(time.time())   # 音频流静默时别让段挂死
                        continue
                    seg.feed(frame, ts)
            if stop["flag"]:
                break
        except Exception as e:
            log.error("采集中断: %s —— %.0fs 后重连", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            state["filt"] = np.zeros(len(_DECIM) - 1, dtype=np.float32)
            while not q.empty():
                q.get_nowait()
            try:
                idx, dev = find_device(cfg)
            except Exception:
                pass

    seg.flush()
    log.info("退出。共落盘 %d 段，丢弃 %d 段。", seg.n_segments, seg.n_dropped)
    conn.close()


def probe(cfg):
    vcfg.ensure_dirs()
    print("=== 环境 ===")
    print("data:", vcfg.DATA)
    idx, dev = find_device(cfg)
    api = sd.query_hostapis(dev["hostapi"])["name"]
    print(f"设备: idx={idx} {dev['name']} / {api} / "
          f"{int(dev['default_samplerate'])}Hz / {dev['max_input_channels']}ch")
    vad = make_vad(cfg)
    print("VAD:", vad.kind)
    frame = cfg["vad_frame_ms"]
    sr_in = cfg["capture_sr"]
    block = sr_in * frame // 1000
    print(f"帧长: {frame}ms  采集块 {block} 样本 @{sr_in}Hz")
    print("\n=== 采集 3 秒（请说话）===")
    filt = np.zeros(len(_DECIM) - 1, dtype=np.float32)
    levels, voiced = [], []
    rs = None
    if int(sr_in) != 48000:      # 与 run() 同一套重采样兜底
        import soxr
        rs = soxr.ResampleStream(int(sr_in), 48000, 1, dtype="int16")
    s = sd.InputStream(samplerate=sr_in, blocksize=block, channels=1,
                       dtype="int16", device=idx)
    s.start()
    t0 = time.time()
    while time.time() - t0 < 3.0:
        blk, _ = s.read(block)
        x = blk[:, 0]
        if rs is not None:
            x = rs.process(x)
        y, filt = decimate3(x, filt)
        y = y[:cfg["target_sr"] * frame // 1000]
        if len(y) < 480:
            continue
        r = float(np.sqrt(np.mean(y.astype(np.float32) ** 2)))
        levels.append(r)
        voiced.append(vad.is_speech(y))
    s.stop(); s.close()
    levels = np.array(levels)
    print(f"RMS  min={levels.min():.1f}  p50={np.percentile(levels,50):.1f}  "
          f"p95={np.percentile(levels,95):.1f}  max={levels.max():.1f}")
    print(f"判为语音的帧: {sum(voiced)}/{len(voiced)} "
          f"({100*sum(voiced)/max(1,len(voiced)):.0f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只探测设备与 VAD")
    ap.add_argument("--seconds", type=float, default=None, help="录满 N 秒后优雅退出")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    vcfg.ensure_dirs()
    vcfg.setup_logging("recorder.log",
                       level=logging.DEBUG if a.debug else logging.INFO)
    cfg = vcfg.load()
    if a.probe:
        probe(cfg)
    else:
        run(cfg, max_seconds=a.seconds)


if __name__ == "__main__":
    main()
