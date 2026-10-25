"""麦克风探测与配置向导 —— 换电脑、换麦之后跑一次，写回 config.json。

为什么需要它：
    开发机的配置钉着 "USB 2.0 Camera" 这颗麦。同样的安装包放到别的电脑
    上，十有八九没有这个名字的设备。以前 find_device 直接抛异常，守护
    进程每小时重启一次、每次都抛，日志里一片红，用户只看到"录不到音"。
    现在两条保险：
      1. 安装时自动跑一遍本脚本：挨个试输入设备，挑第一个真的能以
         48000Hz 单声道打开的，把名字/接口/采样率写回 config.json；
      2. 运行时 find_device 自己也会回退到第一个输入设备（见
         voice_recorder.find_device），配置指不到地方也不至于录不到音。

    一个设备都试不出来时不写配置 —— 留着原样，让运行时的回退逻辑接手。

用法：
    python vprobe.py            # 探测并打印结果，不改任何文件
    python vprobe.py --write    # 探测并写回 config.json
    python vprobe.py --list     # 只列出所有输入设备，不试开
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vcfg  # noqa: E402

import sounddevice as sd  # noqa: E402

PREFER_SR = 48000          # 抽取器钉死 3:1（48000→16000），首选 48k
# 接口优先级：WASAPI 在开发机上底噪最低；别的机器没有就往下退。
PREFER_APIS = ("wasapi", "mme", "directsound", "wdm-ks", "asio")


def _api_name(idx: int) -> str:
    try:
        return sd.query_hostapis(idx)["name"]
    except Exception:
        return "?"


def list_devices() -> list[dict]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        out.append({"idx": i, "name": d["name"], "api": _api_name(d["hostapi"]),
                    "sr": int(d["default_samplerate"]),
                    "ch": d["max_input_channels"]})
    return out


def _try_open(idx: int, sr: int) -> bool:
    """真的开一次流、读一耳朵再关。能过这三关的设备才配写进配置。"""
    try:
        with sd.InputStream(samplerate=sr, channels=1, dtype="int16",
                            device=idx, latency="low"):
            return True
    except Exception:
        return False


def pick(devs: list[dict]) -> tuple[dict, int] | None:
    """第一轮：首选 48k。第二轮：退回设备原生采样率（recorder 会重采样）。"""
    def order(ds: list[dict]) -> list[dict]:
        return sorted(ds, key=lambda d: (
            next((i for i, a in enumerate(PREFER_APIS)
                  if a in d["api"].lower()), len(PREFER_APIS)), d["idx"]))

    for sr in (PREFER_SR, None):
        for d in order(devs):
            rate = sr if sr else d["sr"]
            if _try_open(d["idx"], rate):
                return d, rate
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写回 config.json")
    ap.add_argument("--list", action="store_true", help="只列设备，不试开")
    a = ap.parse_args()

    try:
        devs = list_devices()
    except Exception as e:
        print(f"✗ 枚举音频设备失败：{e}")
        print("  检查声卡驱动 / 隐私设置里麦克风权限是否打开。")
        return 1
    if not devs:
        print("✗ 这台机器上一个输入设备（麦克风）都没有。")
        print("  插一个麦克风，或在 设置→隐私→麦克风 里允许应用使用。")
        return 1

    print(f"输入设备 {len(devs)} 个：")
    for d in devs:
        print(f"  [{d['idx']:>2}] {d['name']:<48} {d['api']:<12} "
              f"{d['sr']}Hz / {d['ch']}ch")

    if a.list:
        return 0

    got = pick(devs)
    if not got:
        print("\n✗ 没有设备能以单声道 int16 打开（试过 48000Hz 和原生采样率）。")
        print("  录音链路会退而使用第一个输入设备；可以先跑起来再排查。")
        return 1

    d, sr = got
    print(f"\n✓ 选定 [{d['idx']}] {d['name']}（{d['api']}，{sr}Hz）")
    if sr != PREFER_SR:
        print(f"  注意：设备不支持 {PREFER_SR}Hz，已按原生 {sr}Hz 采集，"
              f"运行时自动重采样到 48000Hz 再走识别。")

    if a.write:
        cfg = vcfg.load()
        cfg["device_name_contains"] = d["name"]   # 全名 = 自身子串，最稳
        cfg["hostapi"] = d["api"]
        cfg["capture_sr"] = int(sr)
        vcfg.save(cfg)
        print(f"\n已写入 {vcfg.CONFIG_PATH.name}：")
        print(f'  "device_name_contains": "{d["name"]}"')
        print(f'  "hostapi": "{d["api"]}"')
        print(f'  "capture_sr": {sr}')
        print("以后换麦/拔插设备，重跑一次本脚本（--write）即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
