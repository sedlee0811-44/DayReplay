"""语音链路公共配置。

设备事实（2026-10-24 实测，勿凭记忆改）：
- 唯一可用采集端点：麦克风 (USB 2.0 Camera)，单声道
- WASAPI shared 模式只接受 48000Hz / 1ch，其它采样率报 Invalid sample rate
- PortAudio 索引：1=MME 8=DirectSound 18=WASAPI 27=WDM-KS(打不开)
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
AUDIO_DIR = DATA / "audio"
DB_PATH = DATA / "voice.db"
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = DATA / "logs"
SPEAKER_DIR = DATA / "speaker"

DEFAULTS = {
    # 设备：按名字子串匹配，避免索引变动
    "device_name_contains": "USB 2.0 Camera",
    "hostapi": "WASAPI",
    "capture_sr": 48000,
    "target_sr": 16000,

    # VAD：默认 webrtcvad，缺失时自动降级为自适应能量 VAD
    "vad_frame_ms": 30,
    "vad_mode": 2,            # webrtcvad 激进程度 0..3
    "vad_min_rms": 200,       # 能量下限：实测这颗 USB 麦底噪 RMS≈90-110，200 能压掉

    # 切段
    "vad_onset_ms": 90,       # 连续这么久的语音才认定为起句，压掉瞬时咔哒声
    "pre_roll_ms": 300,       # 起始前留一点，避免切掉首字
    "silence_close_ms": 720,  # 连续静音多久判定一句结束
    # 单段上限。以前是 30，2026-10-25 改成 12，理由是离线回放的数据很干脆：
    #   段长 <8s → 有识别出字 69%；8~15s → 31%；>15s → 12%
    # 倒不是模型偏心短段，而是**越长的段越可能压根不是人在说话** —— 风扇、
    # 家电这类稳态噪声让 VAD 一直判"有声"，段永远不满足关闭条件，只能被
    # 硬切；这种段塞进模型，出来就一个"。"或一个"我"。
    # 12 秒既能装下正常的一句话，又给 ASR 留了甜区；到了上限还会自动在
    # 能量谷处落刀（见 voice_recorder.Segmenter._cut_at_valley），不会
    # 把字拦腰斩断。
    "max_segment_s": 12,
    "min_segment_s": 0.6,
    # 段结束时再看一眼"像不像人说话"，不像就整段丢，不进 ASR 也不入库。
    # 判据是谐波性阈值，实测小题见 vquality.is_human 的注释。
    # 阈值 0.45 的效果（200 段真实录音回放）：清掉 93% 的无效段，
    # 同时留住 88% 的有效段；单段判定约 25ms，放在热路径上无感。
    "min_harmonicity": 0.45,
    # 太短的段谐波统计不可靠（样本不够），低于这个时长直接放行不裁决。
    "harm_min_s": 1.0,
    # 段长到上限时，在末尾这么长的窗口里找能量谷作为刀口。
    "split_search_ms": 400,
    # 段内语音帧占比低于此值即判为噪声丢弃。
    # 离线实测（21 段，webrtcvad mode=2）：真语音 0.74-0.85，噪声 0.12-0.52，
    # 中间留 0.22 空隙，取 0.65 能一段不误杀地清掉全部噪声。
    "min_voiced_ratio": 0.65,
    # 上面那道 0.65 只对"近讲"成立。从扬声器回放的新闻/视频/音乐，人声要穿过
    # 距离和外放，VAD 占比会掉到 0.4-0.5 —— 但那是"我在听什么"的核心证据，
    # 不能因为不够清楚就整段丢掉（2026-10-24 18:39-18:47 的越南新闻就是这么丢光的：
    # 实测占比 0.49，卡在 0.65 下面，7 分钟音频全被删，一个字没留下）。
    # 所以再开一道"够响够长就放行"的门。实测数据：
    #   本人近讲 0.72 / 抖音 BGM 0.78 / 新闻回放 0.49 / 键盘碰麦噪声 <0.20
    "loud_voiced_ratio": 0.30,   # 放宽门的占比下限
    "loud_rms": 1000.0,          # 放宽门的音量下限（int16 尺度；底噪约 90-110）
    "loud_min_s": 1.5,           # 放宽门要求的最短时长，挡住瞬时响声

    # 模型（留空则自动在 models/ 下找；绝不联网）
    "asr_model_id": "iic/SenseVoiceSmall",
    # 强制中文解码。这台机器上全是普通话，让模型自己猜语种只会猜错 ——
    # 实测 8 段真实录音：language="auto" 有 2 段被判成非中文（输出 ".그." "그도."），
    # 钉成 "zh" 后 0 段。文字内容几乎不变，但那种"韩文乱码"没了。
    # 极少数情况要改回自动（比如录外语素材）就把它设成 "auto"。
    "asr_language": "zh",
    "spk_model_id": "iic/speech_campplus_sv_zh-cn_16k-common",

    # 声纹判别
    # 实测：真人同人跨次 0.69 / 不同人 -0.08；同一台机器的两个 SAPI 合成音
    # 之间却有 0.49-0.53（同引擎，音色天然接近）。故阈值取 0.55：
    # 既能分开合成的近似音，又不误杀真人的跨次匹配。
    "spk_same": 0.55,         # ≥ 此值判为已登记的人
    "spk_other": 0.30,        # ≥ 此值判为"别人"，低于则"无法判断"
    # 实测：1.2-1.9s 的段落声纹极不稳（修复前那批噪声段全挤在这个区间，
    # 相似度在 0.20-0.46 之间乱跳）。CAM++ 也是按 2s 以上训练的，
    # 故提到 2.0s：短句拿不到说话人标签，但不会给出错标签。
    "spk_min_duration": 2.0,

    # 登记取样：长段切块时的块长与最低语音占比
    # 加语音占比门槛是因为盲切会把停顿块也当成样本（实测 #75 的 7 块里
    # 有 2 块是停顿，相似度 0.46/0.26，把簇内最低相似度拉到 0.331）。
    "spk_chunk_s": 3.0,
    "spk_chunk_min_voiced": 0.45,

    # 总账叙事用的本地大模型。换模型只改这里，不用动代码。
    # 2026-10-24 起建议用 qwen3.5:9b —— 全中文环境，本土模型措辞更地道；
    # 10GB 显存也放得下（约 6.6GB，比原来的 gemma4-12b 7.8GB 还小）。
    "digest_model": "gemma4-12b-unc:latest",

    # 落盘
    "flush_interval_s": 1.0,  # 语音段写入磁盘的刷新间隔（掉电最多丢这么久）

    # 保留期。⚠️ 这个键已经不起作用了 —— 真正的开关在 vclean.py 的
    # `--audio-days`（默认 0 = 永久）。当初这里是 60，但 vclean 用的是自己的
    # 默认值 7 天，两处对不上，导致"录完 7 天自动消失"而用户毫不知情。
    # 2026-10-24 用户明确要求：有人说话的音频、识别出的文字，都要永久保留。
    #   音频 31.8 KB/秒 → 一天 2 小时人声约 230 MB → 一年约 84 GB（E 盘剩 3.1T，够 37 年）
    #   文字 11.6 字节/秒 → 一年约 30 MB（比音频小 2800 倍，永久存完全无压力）
    # 留这个键只为兼容老配置，别在这里改策略。
    "audio_keep_days": 0,

    # 送进模型之前先做一遍前端处理：掐首尾静音 → 高通去低频轰鸣 → 归一化。
    # 摄像头麦是远场拾音，录制电平比模型训练分布低一截，归一化把音量拧
    # 回 -20dBFS 这个常见基准。离线对照里它对清晰的短段是中性的，对偏轻
    # 的段有实质帮助（详见 vquality.enhance 的注释）。
    "asr_preprocess": True,
    "asr_target_dbfs": -20.0,
    # 动态批处理的时长上限。60 太大：一段长音频会被整块喂进去，批处理越
    # 大越容易触发非自回归模型的解码退化。30 更稳，对短段没有区别。
    "asr_batch_size_s": 30,

    # ---- 屏幕记录（Windrecorder，可选组件）装在哪 ----
    # 留空 = E:\AI\Windrecorder（开发机的老位置）。整个安装包搬到别的电脑、
    # Windrecorder 也装在别处时，只改这一个键，不用动代码。
    # 语音链路本身完全不依赖它：这里指不到地方，就当"没有屏幕数据"，
    # 录音、转写、活动声、总账照常跑，只是屏幕侧的内容缺席。
    "windrecorder_dir": "",

    # ---- 人类活动声识别（vevents.py，2026-10-25 新增）----
    # 两级。第一级在录音进程里跑 DSP 粗筛（纯 numpy，20ms/段）：谐波闸门
    # 没放行但"有活动特征"的段不丢弃，打 flag='event' 落库。第二级在转写
    # 进程里跑 AST（AudioSet 527 类）定中文名：小孩啼哭/走动/水龙头流水/
    # 吱嘎摩擦/敲击碰撞……风扇键盘判噪声不认。
    # 模型留空 = 只用第一级粗筛，粗筛放行的段不再定名（也不会显示）。
    "event_model": "MIT/ast-finetuned-audioset-10-10-0.4593",
    # 标签置信度下限。校准（72 条 ESC-50 真实样本）：真实事件顶分
    # 0.11~0.99，噪声里人类类别的最高泄漏 0.031（引擎→门响）。0.10 有 3
    # 倍余量；再低就开始收模型没把握的猜测。
    "event_min_score": 0.10,
    # 粗筛子阈值（onsets_min / rise_min / hf_ratio_min / hf_sustain_min）
    # 不写也有默认值，见 vevents.SCREEN_DEFAULTS —— 那里的注释记了校准数据。

    # ---- 版本与自动更新（vupdate.py，2026-10-25 新增，1.0.0 首发自带）----
    # 更新源：一个放着"完整发布包"的目录（U盘、局域网共享、本机别处）。
    # 安装器默认填成安装包来源；之后想换成局域网共享，只改这一处。
    # 留空 = 不检查更新。
    "update_source": "",
    # off = 不自动检查；check = 定期检查、有新版本只提示不动手（默认）；
    # auto = 检查到新版就自动装上（仍带四道安全闸，失败自动回滚）。
    "auto_update": "check",
    "update_check_hours": 24,     # 自动检查间隔（小时）
    "update_keep_backups": 2,     # 保留最近几个旧版本备份，多的自动删
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_dirs() -> None:
    for d in (DATA, AUDIO_DIR, LOG_DIR, SPEAKER_DIR):
        d.mkdir(parents=True, exist_ok=True)


def acquire_mutex(name: str):
    """拿一个进程间的排他锁。拿到返回一个句柄，拿不到返回 None。

    为什么要有它 —— 2026-10-25 凌晨的真实事故：语音链路整整跑了两套，
    两套 recorder 同时开 WASAPI 麦克风，两套 asr 同时写库，两套 daemon
    互相看不到对方。画面没法看 —— 更糟的是它安静得像一切正常，
    只有识别结果悄悄变差。

    原来的守护进程用的是 "锁文件 + 心跳 + 查 pid" 三件套，看着够严，
    实际挡不住两人同时启动：两边都在对方写文件之前读到了旧内容，
    于是都判定"没人跑"，双双继续。这种竞态在开机自启和手动双击撞上
    时必现，而且只在那一刻必现，之后再也复现不出来。

    内核级的命名互斥没有这个窗口：谁拿到谁跑，第二个进去立刻返回。
    句柄必须一直攥在手里（不能 close），进程一退内核自动释放，
    所以崩溃也不会留下死锁。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt

        # 必须 use_last_error=True：普通 windll 调用之后，ctypes 不会替我们
        # 留下 GetLastError 的值，于是 ERROR_ALREADY_EXISTS 永远看不到，
        # 第二个实例照样觉得自己抢到了 —— 第一次写就是这么错的。
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = wt.HANDLE
        k.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
        ERROR_ALREADY_EXISTS = 183

        # Global 优先：Local\ 是按 Windows 会话隔离的，从计划任务起的进程
        # 和从命令行起的进程可能不在同一个会话里，各拿各的锁，等于没锁 ——
        # 2026-10-25 就是这么双开的。Global\ 跨会话，才是我们要的那个。
        # 但 Global\ 需要 SeCreateGlobalPrivilege，拿不到时会返回句柄 0，
        # 所以必须准备 Local\ 兜底：宁可回到会话内互斥，也不能因为建不出
        # 锁就让所有实例全部退出。
        for ns in ("Global\\", "Local\\"):
            handle = k.CreateMutexW(None, True, ns + name)
            err = ctypes.get_last_error()
            if not handle:
                continue
            if err == ERROR_ALREADY_EXISTS:
                ctypes.windll.kernel32.CloseHandle(handle)
                return None          # 别人已经拿着了
            return handle
        return None
    except Exception:
        return None


def model_dir(model_id: str) -> str:
    """把 ModelScope 的 model id 换成本地目录。

    为什么必须这么做：把 model id 直接交给 funasr，它每次启动都会联网向
    modelscope 解析最新版本（日志里的 'download models from model hub: ms'），
    网络一慢就卡死两分钟以上。本项目要求纯本地，所以只认本地路径。
    """
    safe = model_id.replace("/", "--")
    for base in (ROOT / "models", Path.home() / ".cache" / "modelscope"):
        snap = base / "models" / safe / "snapshots"
        if not snap.is_dir():
            continue
        for c in sorted(snap.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if (c / "config.yaml").exists() or (c / "model.pt").exists():
                return str(c)
    return model_id   # 实在没有，退回在线解析


def setup_logging(logfile: str, level: int = 20) -> None:
    """pythonw 下 sys.stdout / sys.stderr 都是 None，
    必须只在有控制台时才挂 StreamHandler，否则每条日志都会触发异常。"""
    import logging
    import sys

    handlers: list[logging.Handler] = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    ensure_dirs()
    handlers.append(logging.FileHandler(LOG_DIR / logfile, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )
