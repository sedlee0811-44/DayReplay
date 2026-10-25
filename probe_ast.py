"""探针：确认 AST AudioSet 模型能通过 hf-mirror 下载并在本机推理。"""
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", r"E:\AI\voice\models\hf")

import numpy as np

t0 = time.time()
from transformers import AutoModelForAudioClassification, AutoFeatureExtractor

mid = "MIT/ast-finetuned-audioset-10-10-0.4593"
fe = AutoFeatureExtractor.from_pretrained(mid)
model = AutoModelForAudioClassification.from_pretrained(mid)
model.eval()
print(f"loaded in {time.time()-t0:.1f}s")

labels = model.config.id2label
print("n_labels:", len(labels))
want = ["cry", "footstep", "walk", "water", "tap", "faucet", "squeak", "slam",
        "knock", "pour", "stream", "speech", "child", "baby", "door",
        "cupboard", "drawer", "furniture", "scratch", "rub", "wind", "fan",
        "silence", "noise", "inside, small", "water tap"]
hits = {i: l for i, l in labels.items()
        if any(w in l.lower() for w in want)}
for i, l in sorted(hits.items()):
    print(f"  [{i}] {l}")

# 合成一段"水声"（高通噪声）试推
sr = 16000
rng = np.random.RandomState(0)
x = rng.randn(sr * 4).astype(np.float32) * 0.05
# 简单一阶高通留下高频嘶嘶声
x = np.diff(x, prepend=x[:1])
inputs = fe(x, sampling_rate=sr, return_tensors="pt")
import torch
with torch.no_grad():
    out = model(**inputs)
probs = torch.softmax(out.logits, dim=-1)[0]
top = torch.topk(probs, 5)
print("\n合成高频噪声 top5:")
for p, i in zip(top.values.tolist(), top.indices.tolist()):
    print(f"  {p:.3f}  {labels[i]}")
print(f"\ninference {time.time()-t0:.1f}s total")
