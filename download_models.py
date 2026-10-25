"""Download AI models from HuggingFace on first run.

DayReplay's core installer (~1.5 GB) ships Python + code but NOT the models
(another ~1.3 GB). This script downloads them on demand:

  python download_models.py              # download all
  python download_models.py --check      # check what's missing
  python download_models.py --asr-only   # only speech-to-text model

Models (all from HuggingFace, free & open):
  1. SenseVoiceSmall    ~893 MB   — speech-to-text (zh + en)
  2. CAM++ speaker       ~48 MB   — voice fingerprint / who is speaking
  3. FSMN VAD            ~5 MB   — voice activity detection
  4. AST AudioSet       ~330 MB   — sound event classifier (527 classes)

Total: ~1.3 GB

Requirements (already in DayReplay's bundled Python):
  pip install huggingface_hub torch
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Project root: this script lives in code/ alongside vcfg.py
ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"

# HuggingFace 镜像。国内直连 huggingface.co 经常超时或极慢，
# 设 HF_ENDPOINT 到 hf-mirror.com 就走镜像（国内可达）。
# 也可以在 config.json 里写 "hf_endpoint"，或直接设环境变量。
if not os.environ.get("HF_ENDPOINT"):
    _cfg_path = ROOT / "config.json"
    try:
        import json as _json
        _cfg = _json.loads(_cfg_path.read_text(encoding="utf-8"))
        _ep = (_cfg.get("hf_endpoint") or "").strip()
        if _ep:
            os.environ["HF_ENDPOINT"] = _ep
            print(f"Using HuggingFace mirror: {_ep}")
    except Exception:
        pass

# Model definitions: (huggingface_repo_id, approximate_size_mb, description)
MODELS = [
    {
        "id": "FunAudioLLM/SenseVoiceSmall",
        "size_mb": 893,
        "desc": "Speech-to-text (Chinese + English)",
        "config_check": "config.yaml",
    },
    {
        "id": "iic/speech_campplus_sv_zh-cn_16k-common",
        "size_mb": 48,
        "desc": "Speaker recognition (voice fingerprint)",
        "config_check": "config.yaml",
    },
    {
        "id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "size_mb": 5,
        "desc": "Voice activity detection",
        "config_check": "config.yaml",
    },
    {
        "id": "MIT/ast-finetuned-audioset-10-10-0.4593",
        "size_mb": 330,
        "desc": "Sound event classifier (527 classes)",
        "config_check": "config.json",
    },
]


def check_model(model_id: str, config_check: str) -> bool:
    """Check if a model is already downloaded locally."""
    safe = model_id.replace("/", "--")
    # Check models/models/ path (our layout)
    snap = MODELS_DIR / "models" / safe / "snapshots"
    if snap.is_dir():
        for c in sorted(snap.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (c / config_check).exists():
                return True
    # Check models/hf/ path (alternative HF cache layout)
    hf_snap = MODELS_DIR / "hf" / "hub" / f"models--{safe}" / "snapshots"
    if hf_snap.is_dir():
        for c in sorted(hf_snap.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (c / config_check).exists():
                return True
    return False


def check_all() -> list[dict]:
    """Return list of models with their status."""
    result = []
    for m in MODELS:
        present = check_model(m["id"], m["config_check"])
        result.append({**m, "present": present})
    return result


def download_one(model_id: str, desc: str) -> bool:
    """Download a single model using huggingface_hub.

    Layout matters: vcfg.model_dir() only looks under
    models/models/<id-with-dashes>/snapshots/<something>/ and requires
    config.yaml or model.pt to be there. So we download straight into
    snapshots/master/ — hub 2.x's local_dir does exactly that.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  Installing huggingface_hub...")
        os.system(f'"{sys.executable}" -m pip install huggingface_hub -q')
        from huggingface_hub import snapshot_download

    target = MODELS_DIR / "models" / model_id.replace("/", "--") / "snapshots" / "master"
    print(f"  Downloading {model_id}...")
    print(f"  ({desc})")
    t0 = time.time()

    kwargs = dict(repo_id=model_id, local_dir=target)
    # hub 2.x 移除了 local_dir_use_symlinks，老版本才有这个参数
    import inspect
    if "local_dir_use_symlinks" in inspect.signature(snapshot_download).parameters:
        kwargs["local_dir_use_symlinks"] = False

    try:
        path = snapshot_download(**kwargs)
        elapsed = time.time() - t0
        size_mb = sum(f.stat().st_size for f in Path(path).rglob("*")
                       if f.is_file()) / 1048576
        print(f"  Done in {elapsed:.0f}s ({size_mb:.0f} MB)")
        return True
    except Exception as e:
        print(f"  Failed: {e}")
        # Alternative: let the model framework download it
        print("  Trying alternative download method...")
        try:
            if "SenseVoice" in model_id or "iic/" in model_id:
                from funasr import AutoModel
                _ = AutoModel(model=model_id, model_revision="v2.0.4")
                print("  Done (via funasr AutoModel)")
                return True
        except Exception as e2:
            print(f"  Alternative also failed: {e2}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Download DayReplay AI models")
    ap.add_argument("--check", action="store_true", help="Check which models are present")
    ap.add_argument("--asr-only", action="store_true", help="Only download ASR model")
    ap.add_argument("--json", action="store_true", help="Output status as JSON (for install.ps1)")
    args = ap.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    status = check_all()

    if args.json:
        print(json.dumps({"models": status, "all_present": all(m["present"] for m in status)}))
        return 0

    if args.check:
        print("Model status:")
        for m in status:
            icon = "OK" if m["present"] else "MISSING"
            print(f"  [{icon}] {m['id']}: {m['desc']} ({m['size_mb']} MB)")
        missing = [m for m in status if not m["present"]]
        if missing:
            total = sum(m["size_mb"] for m in missing)
            print(f"\nMissing: {len(missing)} model(s), ~{total:.0f} MB total")
            print(f"Run: python download_models.py")
        else:
            print("\nAll models installed.")
        return 0

    # Download
    to_download = [m for m in status if not m["present"]]
    if args.asr_only:
        to_download = [m for m in status if "SenseVoice" in m["id"] and not m["present"]]

    if not to_download:
        print("All models already installed.")
        return 0

    total_size = sum(m["size_mb"] for m in to_download)
    print(f"Downloading {len(to_download)} model(s), ~{total_size:.0f} MB total:")
    print()

    ok = 0
    fail = 0
    for m in to_download:
        print(f"[{to_download.index(m)+1}/{len(to_download)}] {m['desc']}")
        if download_one(m["id"], m["desc"]):
            ok += 1
        else:
            fail += 1
        print()

    print(f"Result: {ok} downloaded, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
