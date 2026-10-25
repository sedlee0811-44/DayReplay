# -*- coding: utf-8 -*-
"""总账自然语言搜索的 Python 侧测试：索引、文字稿、链接有效性。

浏览器二进制在沙箱里不可用，所以 JS 解析/打分另有一份 Node 测试
（test_search.js）；这里只验"数据和文件都对"。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import vdigest
import vsearch

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}" + (f"  | {extra}" if extra else ""))


print("== 1. 索引内联进页面 ==")
p = vdigest.write_ledger_html(live=True, every=300)
html = Path(p).read_text(encoding="utf-8")
m = re.search(r'<script type="application/json" id="idx">(.*?)</script>',
              html, re.S)
check("页面里有 idx 数据块", bool(m))
idx = json.loads(m.group(1).replace("<\\/", "</")) if m else {}
check("索引有 days/voice/screens",
      bool(idx.get("days")) and "voice" in idx and "screens" in idx,
      f"days={len(idx.get('days', []))}")
check("索引里的天和页面锚点对得上",
      all(f'id="day-{d["key"]}"' in html for d in idx.get("days", [])))
check("索引里的语音段都带时间和日期",
      all(v.get("day") and v.get("hm") and v.get("dur") is not None
          for v in idx.get("voice", [])))

print("\n== 2. 录音与文字稿链接都指向真实文件 ==")
miss_wav = [v for v in idx.get("voice", []) if v.get("wav")
            and not (REPORTS / v["wav"]).is_file()]
miss_tr = [v for v in idx.get("voice", []) if v.get("tr")
           and not (REPORTS / v["tr"]).is_file()]
check("所有录音链接都落得到文件", not miss_wav, f"{len(miss_wav)} 个缺失")
check("所有文字稿链接都落得到文件", not miss_tr, f"{len(miss_tr)} 个缺失")

n_tr = sum(1 for v in idx.get("voice", []) if v.get("tr"))
n_txt = sum(1 for v in idx.get("voice", []) if (v.get("text") or "").strip())
n_lbl = sum(1 for v in idx.get("voice", []) if v.get("labels"))
check("有文字或有活动标签的段都生成了文字稿", n_tr >= n_txt + n_lbl,
      f"文字稿 {n_tr} / 有文字 {n_txt} / 有标签 {n_lbl}")

print("\n== 3. 文字稿页内容 ==")
sample = next((v for v in idx.get("voice", [])
               if v.get("tr") and (v.get("text") or "").strip()), None)
if sample:
    tp = REPORTS / sample["tr"]
    t = tp.read_text(encoding="utf-8")
    check("标题带日期和时刻", sample["day"] in t and sample["hm"] in t)
    check("有播放器", "<audio" in t and "controls" in t)
    check("播放器指向真实音频",
          bool(re.search(r'src="([^"]+\.wav)"', t))
          and (tp.parent / re.search(r'src="([^"]+\.wav)"', t).group(1)).is_file())
    check("有返回总账的链接", 'href="../../总账.html"' in t)
    body = re.search(r'<div class="art">(.*?)</div>', t, re.S)
    check("正文就是识别出的文字", bool(body) and sample["text"][:8] in body.group(1),
          (body.group(1)[:40] if body else "无正文"))
else:
    check("找到带文字的样本段", False, "索引里没有带文字的段")

ev_sample = next((v for v in idx.get("voice", [])
                  if v.get("tr") and not (v.get("text") or "").strip()
                  and v.get("labels")), None)
if ev_sample:
    t = (REPORTS / ev_sample["tr"]).read_text(encoding="utf-8")
    check("活动声文字稿说明了没有文字", "没有转写出文字" in t)
    check("活动声文字稿带上了中文标签", ev_sample["labels"][0] in t)
else:
    print("  note 没有纯活动声样本，跳过两条")

print("\n== 4. 明细里的文字稿入口 ==")
links = re.findall(r'href="(ev/voice/t\d+\.html)"[^>]*>([^<]+)</a>', html)
check("明细里有「文字稿」链接", any("文字稿" in lab for _, lab in links),
      f"共 {len(links)} 条")
check("明细链接同时有「录音」链接", "录音" in html)
bad = [u for u, _ in links if not (REPORTS / u).is_file()]
check("明细里的文字稿链接全部有效", not bad, f"{len(bad)} 个缺失")

print("\n== 5. 屏幕活动段进索引（「我看过越南改革」靠它命中） ==")
scr = idx.get("screens", [])
check("屏幕段带 proc/标题/OCR",
      scr and all("proc" in s and "titles" in s and "ocr" in s for s in scr),
      f"{len(scr)} 段")
check("屏幕段带起止时间", all(s.get("start") and s.get("end") for s in scr))

print("\n== 6. JSON 内联安全 ==")
check("索引里没有裸 </script>", "</script>" not in (m.group(1) if m else ""))
check("中文没被转义成 \\u", "\\u" not in (m.group(1) if m else ""))

print(f"\n{'全部通过' if not fail else '有失败'}：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
