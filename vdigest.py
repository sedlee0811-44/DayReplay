"""会话摘要生成 —— 把一次开机期间的原始材料压成"干货"。

定位：
    产物不是流水账，是**任务账**：这次开机为了什么事、动了哪些文件、
    文件里写了什么、几点开的机几点关的机。

流程：
    1. vsession 取会话边界（开机/关机时间）
    2. vcollect 取四类事实（文件/屏幕/语音/打印）
    3. 本地 gemma4-12b 归纳成任务列表（只准引用给定编号，不准编造）
    4. 渲染 Markdown，落盘到 reports/，并登记索引

防幻觉：
    文件名、时间、大小全部由渲染器用真实数据填，模型只负责"归组 + 说人话"。
    模型给的任务若引用了不存在的编号，直接丢弃该引用，不补、不猜。

用法：
    python vdigest.py                  # 当前这次开机
    python vdigest.py --prev           # 上一次已结束但还没总结的会话
    python vdigest.py --live           # 实时：总结当前这场到此刻，网页自动刷新
    python vdigest.py --no-llm         # 不调模型，只出结构化事实
    python vdigest.py --boot 2026-10-24T08:42
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import vcfg
import vcollect as vc
import vcrop
import vsession as vss

OLLAMA = "http://127.0.0.1:11434"
# 换模型不用改代码：config.json 里写 "digest_model": "qwen3.5:9b" 即可。
# 为什么要能换：这台机器只处理中文，而 gemma 是英文优先的模型 —— 中文能写，
# 但常有翻译腔（"运行对话环境程序""处理产品相关文档"这种半生不熟的措辞）。
# 本土中文模型（Qwen 系）在同一段事实上的表达明显更顺。
MODEL = vcfg.load().get("digest_model") or "gemma4-12b-unc:latest"
REPORT_DIR = vcfg.ROOT / "reports"


# ------------------------------------------------------------------ 事实

def build_facts(sess: dict, scan_budget: float = 90.0,
                live: bool = False) -> dict:
    t0 = sess["boot"]
    t1 = sess["shutdown"] if sess["shutdown"] is not None else time.time()
    # 跨天要按自然天切开。一夜没关机是最常见的情况：开机在 24 日晚上，
    # 跑到 25 日早上还在同一场里。以前整场都挂在开机那天，于是"今天"
    # 永远不出现新节点，25 日的活全记在 24 日账上。
    # 现在 live 模式把窗口起点抬到当天 0 点，今天的內容单独成一条；
    # 真实开机时刻记在 carry 里，标题照样显示它是哪次开机的延续。
    carry = None
    if live and t0 < _day_start(t1):
        carry = t0
        t0 = _day_start(t1)

    print(f"  采集会话 {vss.describe(sess)}"
          + (f"（跨天，只取 {vss.fmt(t0)} 起）" if carry else ""), flush=True)
    fc = vc.file_changes(t0, t1, budget=scan_budget)
    arts = vc.group_files(fc["items"], max_show=18)
    print(f"    文件 {len(fc['items'])} 个（扫描 {fc['scanned']}，"
          f"{'已截断' if fc['truncated'] else '完整'}）", flush=True)
    scr = vc.screen_activity(t0, t1)
    print(f"    屏幕 {len(scr)} 段", flush=True)
    voc = vc.voice_lines(t0, t1)
    print(f"    语音 {len(voc)} 条", flush=True)
    pe = vc.print_events(t0, t1)
    print(f"    打印 {len(pe['items'])} 条（日志启用={pe['enabled']}）", flush=True)

    # 离座/走神时段：电脑屏幕不动、但屋里有声音。
    # 这类时间对"干了什么"没信息量，但不该假装不存在，所以单独拎出来，
    # 后面只压成一句话。
    idle = vc.idle_blocks(t0, t1, screens=scr)
    if idle:
        print(f"    离座 {len(idle)} 段（"
              f"{sum(b['minutes'] for b in idle):.0f} 分钟，"
              f"其中判为放媒体的 {sum(1 for b in idle if b['kind'] == 'media')} 段）",
              flush=True)
    # 给落在离座时段里的说话打个标记，后面渲染时不再逐条列出
    for v in voc:
        v["idle"] = any(b["start"] <= v["start"] <= b["end"] for b in idle)

    return {"session": sess, "t0": t0, "t1": t1, "carry": carry,
            "artifacts": arts, "screens": scr, "voice": voc, "print": pe,
            "idle": idle,
            "drift": vc.drift_sentence(idle),
            "scanned": fc["scanned"], "truncated": fc["truncated"]}


def _brief(facts: dict) -> str:
    """给模型看的极简版：只保留编号和关键字段，控制 token。"""
    lines = []
    carry = facts.get("carry")
    span = f"{vss.fmt(facts['t0'])} → {vss.fmt(facts['t1'])}"
    if carry is not None:
        span += f"（这是 {vss.fmt(carry)} 那次开机的延续，今天从 0 点算起）"
    lines.append("【会话】%s（%.1f 小时）" % (span, (facts["t1"] - facts["t0"]) / 3600))
    lines.append("")
    lines.append("【产出/动过的文件】编号 | 时间 | 新建或修改 | 大小 | 路径 | 内容片段")
    for i, a in enumerate(facts["artifacts"]):
        if a["collapsed"]:
            lines.append(f"{i} | {vss.fmt(a['last'])} | 目录 | {a['count']}个文件 | "
                         f"{a['dir']} | 例：{'、'.join(a['sample'])}")
        else:
            lines.append(f"{i} | {vss.fmt(a['last'])} | "
                         f"{'新建' if a['is_new'] else '修改'} | "
                         f"{a['size']/1024:.0f}KB | {a['path']} | "
                         f"{(a.get('peek') or '')[:110]}")
    lines.append("")
    lines.append("【屏幕活动】编号 | 起–止 | 分钟 | 应用 | 出现过的文档名 | 屏幕文字摘要")
    for i, s in enumerate(facts["screens"]):
        lines.append(f"{i} | {vss.fmt(s['start'])}–{vss.fmt(s['end'])} | "
                     f"{s['minutes']:.0f}min | {s['proc']} | "
                     f"{'、'.join(s['docs']) or '—'} | {s['ocr'][:110]}")
    lines.append("")
    lines.append("【说过的话】")
    for v in facts["voice"]:
        lines.append(f"- {vss.fmt(v['start'])} [{v['spk']}] {v['text'][:120]}")
    if not facts["voice"]:
        lines.append("- （无）")
    lines.append("")
    lines.append("【打印】" + ("无记录" if not facts["print"]["items"]
                              else f"{len(facts['print']['items'])} 条"))
    return "\n".join(lines)


# ------------------------------------------------------------------ 模型

SYS = """你是一个日记整理助手。用户会给你一次开机期间观察到的事实清单，你要把它归纳成"这次开机做了什么"。

铁律：
1. 只能使用清单里出现的信息。绝对不许编造文件名、时间、路径、内容。
2. 清单里没有的，就不要写。某段时间没有记录，就说"没有记录"，不要推测。
3. 文件、屏幕、语音三类证据要互相印证后再归到同一个任务里。
4. 任务按时间顺序，最多 6 个。合并同一件事的多个文件。
5. 每个任务的 what 写 1-2 句，说清楚"在做什么、做到什么程度"，不要罗列文件。
6. artifacts 和 screens 只能填清单里的编号（整数），不相关的不要硬塞。
7. 全程用简体中文，语气平实，不抒情、不评价用户。

只输出 JSON，不要任何解释文字：
{"headline":"一句话概括这次开机做了什么","tasks":[{"title":"任务名","what":"1-2句说明","artifacts":[0,3],"screens":[1]}]}"""


def _post_chat(payload: dict, timeout: float) -> dict:
    """发请求。老版本 Ollama 不认 think 参数会返回 400，这时去掉再发一次。"""
    body = json.dumps(payload).encode("utf-8")
    hdr = {"Content-Type": "application/json"}

    def go(p):
        req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(p).encode("utf-8"),
                                     headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        return go(payload)
    except urllib.error.HTTPError as e:
        if e.code in (400, 422) and "think" in payload:
            p2 = {k: v for k, v in payload.items() if k != "think"}
            return go(p2)
        raise


def ask_llm(brief: str, timeout: float = 300.0) -> dict | None:
    payload = {
        "model": MODEL,
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": 4096},
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": brief}],
    }
    t0 = time.time()
    try:
        d = _post_chat(payload, timeout)
        txt = (d.get("message") or {}).get("content", "")
        print(f"    模型返回 {len(txt)} 字，用时 {time.time()-t0:.1f}s", flush=True)
        return json.loads(txt)
    except Exception as e:
        print(f"    模型不可用：{e}", flush=True)
        return None


# ------------------------------------------------------------------ 快路径：只写那一段话
#
# 之前 420 秒超时的根因不是模型慢（实测 34 tok/s），是**输出结构太重**：
# 要求 JSON + 6 个任务 + 每个任务多字段，还要 format:"json" 逐个 token
# 做语法约束。现在交付物本身就是"一段话"，所以直接让模型写一段 200 字
# 的散文，输出 250 token 左右 → 7~10 秒就能出。

SYS_PARA = """你在替用户写开机日记。给你一次开机期间机器观察到的事实，你写一段话告诉用户这次开机干了什么。

读者是用户本人。他要知道的是"我今天做了什么事"，不是"开了哪些软件"。

铁律：
1. 第一句必须点明主线：这段时间主要在推进哪一件事。靠事实里的阶段标题、
   文档名、屏幕上出现的内容来判断，不要靠软件名。**不许**用"某软件用了多少
   分钟""还打开了某某软件"这种开头——软件是工具，不是内容。
2. 主线只能从"经过的几个阶段"和动过的文档里来。**录音里听到的东西是背景音**，
   不是用户在做的事——绝不许把听到的节目、视频、别人说的话当成他这天的工作主题。
3. 接着补充次要的事，或者主线卡在哪、做成了什么。
4. 有声音就单独用一句交代：在放什么节目、谁在说话、大致在聊什么。
   **不要逐字引用转写原话**——语音识别会有错字，引出来像故障（"我我残""俄我残隽"）。
   事实里凡是给了原文的句子才可以稍稍复述大意；标成"有声音，但没听出能辨认的话"
   的部分，**一个字都不许引用**，也不许凭录音猜他在干什么。
4b. 如果对听到的内容没把握，宁可写成"屋里有声音"，也不要写具体的话。
   写错一句话，比漏写一句话让人更难接受。
5. 只能写事实里有的内容。不许编造文件名、时间、软件名、网址、节目名。
6. 某段时间没记录就直接说没记录，不要猜。
7. 绝对不许出现目录名、磁盘路径、文件扩展名、英文下划线的代码名。
8. 不要评论你收到的材料本身。禁止"清单""事实""材料""记录里显示"这类词。
9. 不要分点、不要标题、不要 Markdown，就是一整段话。
10. 开头直接进入正题，不要"好的""根据……"。
11. 2~4 句，150 字以内。**要像一个人连着说下来的**，句子之间要有承接，
    不许把几个短语硬拼在一起。
12. 简体中文。"""


def ollama_up(timeout: float = 2.0) -> bool:
    """几百毫秒就能判断，别为这个卡住开机流程。"""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        names = [m.get("name", "") for m in d.get("models", [])]
        return any(MODEL.split(":")[0] in n for n in names)
    except Exception:
        return False


def _human_name(p) -> str | None:
    """只保留"人起的名字"。`config_src`、`2026-10-24_0842.brief.txt` 一律不给模型。

    判断标准很土但管用：含中文的、或者形如"季度报告""方案A"这种首字母大写的，
    才算人能叫得出口的名字。纯小写英文 + 下划线/横杠的基本是代码和配置。
    """
    stem = Path(p).stem
    if not stem:
        return None
    if any("\u4e00" <= ch <= "\u9fff" for ch in stem):
        return stem
    if len(stem) <= 20 and stem[:1].isupper() and "_" not in stem and "-" not in stem:
        return stem
    return None


def _clean_voice_line(t: str | None) -> str | None:
    """挑能进正文的转写：汉字占绝对多数、没有韩文/假名乱码。

    实测噪声样本：`.그.`、`我从买行네`、`我我残`。这些进了段落会变成
    "有人说过'我我残'"这种读起来像故障的句子。
    """
    t = (t or "").strip(" .。,，、·-—_")
    if len(t) < 6:
        return None
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    foreign = sum(1 for ch in t if "\uac00" <= ch <= "\ud7af"
                  or "\u3040" <= ch <= "\u30ff")
    if cjk < 5 or foreign or cjk / len(t) < 0.7:
        return None
    return t[:44]


# 三个及以上连续相同的字。解码崩掉的典型征兆 —— 实测就有 "我我残"。
_RUN_RE = re.compile(r"(.)\1{2,}")


def _voice_trustable(v: dict, max_s: float = 12.0) -> bool:
    """这句转写值不值得被原样引述。

    问的不是"它对不对" —— 没有参考答案时谁也判不了对错。问的是
    "在什么条件下它容易出错"，这个问题离线数据答得很干脆：

        段长 < 8 秒   → 有识别出字 69%
        8 ~ 15 秒     → 31%
        > 15 秒       → 12%

    因为越长的段越可能压根不是人在说话（详见 voice_recorder 的注释），
    所以超过 max_s 的一律不引原文，只报"有人在说话/在放东西"。
    再叠两条显形的故障特征：连续重复字、英文占比失控。

    为什么不干脆用 MORE-less rule 判"读不通顺就丢"：试过，
    会把"代理软件点击连接""明天几十分"这类没有虚词但完全正常的短句
    一起误杀。宁可少引，不可错杀。
    """
    dur = float(v.get("end") or 0) - float(v.get("start") or 0)
    if dur > max_s:
        return False
    t = (v.get("text") or "").strip(" .。,，、·-—_")
    if len(t) < 6:
        return False
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    if cjk < 5 or cjk / len(t) < 0.7:
        return False
    if _RUN_RE.search(t):
        return False
    latin = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    if latin and latin / len(t) > 0.4:
        return False
    return True


def _brief_para(facts: dict, stages: list[str] | None = None) -> str:
    """写段落用的浓缩事实，控制在 1000 字以内。

    写段落不需要路径、不需要目录名、不需要每个文件 —— 那些给了模型它就会
    照抄，抄出来用户看不懂。只给"什么时间、在哪个软件、碰了哪些叫得出名字
    的文档、说了什么"。

    stages 是时间轴已经切好的阶段标题。这是整份材料里信息密度最高的一项：
    它已经把"一堆零散动作"归成了"几件正经事"，段落的主线就该从它来。
    老版本不给这个，模型只能数软件用时，于是写出"某软件用了大半时间"。
    """
    sess, t0, t1 = facts["session"], facts["t0"], facts["t1"]
    hh = (t1 - t0) / 3600.0
    # 结束状态必须说实话：会话还在进行时，旧版这里照样写"正常关机"，
    # 于是实时模式下模型会在段尾加一句"最后按常规流程关机了" —— 人还坐在
    # 电脑前，页面却说他关机了。这种错误比少写一句严重得多。
    if sess["shutdown"] is None:
        # 别写"不要写关机"——提示词里出现"关机"两个字，模型反而会回一句
        # "尚未关机"。只给正向的"还在用"就够了。
        tail = "仍在进行中"
    else:
        tail = "正常关机" if sess.get("clean", True) else "非正常关机"
    L = [f"【时间】{vss.fmt(t0)} 开机 → "
         f"{vss.fmt(t1) if sess['shutdown'] else '现在'}（{hh:.1f} 小时，{tail}）"]

    if stages:
        L.append("【经过的几个阶段】" + " → ".join(stages[:9]))
        L.append("　　（这段话的主线就从这几个阶段里来，别去数软件用了多少分钟）")

    # 屏幕：按应用聚合，只留出现最多的前几个
    by_app: dict[str, float] = {}
    docs: list[str] = []
    for s in facts["screens"]:
        p = s["proc"] or "?"
        by_app[p] = by_app.get(p, 0.0) + s["minutes"]
        for dname in s["docs"]:
            n = _human_name(dname)
            if n and n not in docs:
                docs.append(n)
    if by_app:
        top = sorted(by_app.items(), key=lambda kv: -kv[1])[:5]
        L.append("【用过的软件】" + "、".join(
            f"{p} {m:.0f}分钟" for p, m in top))
    if docs:
        L.append("【屏幕上出现过的文档】" + "、".join(docs[:8]))

    # 文件：只报人叫得出名字的那几个，外加总量
    named: list[str] = []
    for a in facts["artifacts"]:
        if a["collapsed"]:
            continue
        n = _human_name(a["name"])
        if n and n not in named:
            named.append(n)
    n_plain = len(facts["artifacts"])
    if named:
        L.append("【这次动过的文档】" + "、".join(named[:8]))
    elif n_plain:
        L.append(f"【动过 {n_plain} 个文件】都是程序自己的配置文件，没动用户文档")

    # 语音：离座时段的不逐条列，只报一句
    talk = [v for v in facts["voice"] if not v.get("idle") and v.get("text")]
    if talk:
        # 只引"当时接管得比较清楚"的那几句；其余统一降级成"有声音"。
        # 否则模型会把噪声段的乱码当成用户说过的话写进去 —— 实测就有
        # "音响里有人在说'俄我残隽'"这种让人一头雾水的句子。
        bits = []
        for v in talk:
            line = _clean_voice_line(v.get("text"))
            if line and _voice_trustable(v):
                bits.append(f"{vss.fmt(v['start'])} {v['spk']}说「{line}」")
            if len(bits) >= 5:
                break
        if bits:
            L.append(f"【录到 {len(talk)} 段说话】" + "；".join(bits))
        else:
            L.append(f"【录到 {len(talk)} 段声音】有声音，但没听出能辨认的话。"
                     "这种情况只写一句「当时屋里有声音」，不要猜在说什么，"
                     "也不要把任何转写原文写进正文")
    elif not facts.get("idle"):
        L.append("【说话】这次没录到说话")

    # 活动声：没有文字、但有中文标签的段（小孩啼哭/走动/水龙头流水…）。
    # 这是 AST 声学事件模型认出来的，按听到的如实写，没列出的别猜。
    evs = [v for v in facts["voice"] if v.get("kind") == "event"]
    if evs:
        c: dict[str, int] = {}
        for v in evs:
            for lb in v["labels"]:
                c[lb] = c.get(lb, 0) + v.get("n", 1)
        L.append("【活动声】" + "、".join(f"{k} {n} 次" for k, n in
                                        sorted(c.items(), key=lambda x: -x[1])))
        L.append("　　这些是录音认出的人体活动声响，按上面的次数如实写，"
                 "每个标签都可以点开听原音；没列出来的声音不要猜。")

    if facts.get("idle"):
        L.append("【离座时段】" + facts["drift"])
        L.append("　　这类时间只用一句话带过，不要描述具体看了什么节目、不要列时间点、"
                 "不要分析情绪。但**必须点明那会儿是在用手机/放视频这类**，"
                 "不能简化成「离开了座位」——那句话的意义就是记下「我走神了」。")

    if facts["print"]["items"]:
        L.append(f"【打印】{len(facts['print']['items'])} 次")

    # 数据缺口要说清楚，否则模型会自作聪明编一段"在思考"出来
    if not facts["screens"] and not facts["voice"]:
        L.append("【重要】这次开机完全没有屏幕和语音数据，只有文件变动可以看。"
                 "写的时候要说明这段时间没记录下来，不要猜用户在干什么。")
    return "\n".join(L)


def ask_para(brief: str, timeout: float = 90.0) -> str | None:
    """让本地模型写那一段话。失败返回 None，绝不编。"""
    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,          # 关键：gemma4 是思考型模型，不关掉 content 会是空的
        "options": {
            "temperature": 0.3,
            "num_ctx": 3072,
            "num_predict": 400,      # 硬顶，防止模型跑飞
        },
        "messages": [{"role": "system", "content": SYS_PARA},
                     {"role": "user", "content": brief}],
    }
    t0 = time.time()
    try:
        d = _post_chat(payload, timeout)
        txt = ((d.get("message") or {}).get("content") or "").strip()
        dt_s = time.time() - t0
        tok = d.get("eval_count") or 0
        speed = f"{tok/dt_s:.0f} tok/s" if dt_s > 0 and tok else "—"
        print(f"    模型写了 {len(txt)} 字，用时 {dt_s:.1f}s（{speed}）", flush=True)
        txt = _tidy_para(txt)
        return txt or None
    except Exception as e:
        print(f"    本地模型没写成：{e}", flush=True)
        return None


def _tidy_para(txt: str) -> str:
    """模型爱加的壳子统一剥掉：引号、markdown 强调、"好的以下是"。"""
    t = txt.strip()
    for _ in range(3):
        if len(t) > 1 and t[0] in "\"“'「" and t[-1] in "\"”'」":
            t = t[1:-1].strip()
        for pre in ("好的，", "好的。", "以下是", "根据事实清单，",
                    "根据清单，", "这段开机期间，"):
            if t.startswith(pre):
                t = t[len(pre):].strip()
    t = t.replace("**", "").replace("##", "").replace("\n", "")
    if len(t) > 400:                      # 200 字的目标，400 已经是跑飞
        cut = t[:400].rfind("。")
        t = t[:cut + 1] if cut > 50 else t[:400]
    return t.strip()


def fallback_tasks(facts: dict) -> dict:
    """模型不可用时的确定性归组：按目录聚类。"""
    by_dir: dict[str, list[int]] = {}
    for i, a in enumerate(facts["artifacts"]):
        if not a["collapsed"]:
            by_dir.setdefault(a["dir"], []).append(i)
    tasks = []
    for d, idx in list(by_dir.items())[:6]:
        names = [facts["artifacts"][i]["name"] for i in idx[:4]]
        tasks.append({"title": Path(d).name or d, "what": "动过：" + "、".join(names),
                      "artifacts": idx, "screens": []})
    return {"headline": "本次开机动了 %d 个文件" % len(facts["artifacts"]),
            "tasks": tasks}


def sanitize(res: dict, facts: dict) -> dict:
    """把模型给的编号卡到合法范围，非法引用直接丢掉。"""
    na, ns = len(facts["artifacts"]), len(facts["screens"])
    out = {"headline": str(res.get("headline") or "")[:120], "tasks": []}
    for t in (res.get("tasks") or [])[:6]:
        if not isinstance(t, dict):
            continue
        arts = [i for i in (t.get("artifacts") or [])
                if isinstance(i, int) and 0 <= i < na]
        scr = [i for i in (t.get("screens") or [])
               if isinstance(i, int) and 0 <= i < ns]
        out["tasks"].append({
            "title": str(t.get("title") or "")[:40],
            "what": str(t.get("what") or "")[:300],
            "artifacts": arts, "screens": scr})
    return out


# ------------------------------------------------------------------ 渲染

def _span(facts: dict, task: dict) -> str:
    ts = []
    for i in task["artifacts"]:
        ts.append(facts["artifacts"][i]["last"])
    for i in task["screens"]:
        ts += [facts["screens"][i]["start"], facts["screens"][i]["end"]]
    if not ts:
        return "（时间不明）"
    return f"{vss.fmt(min(ts))} – {vss.fmt(max(ts))}"


def render(facts: dict, res: dict) -> str:
    sess, t0, t1 = facts["session"], facts["t0"], facts["t1"]
    d = dt.datetime.fromtimestamp(t0)
    week = "一二三四五六日"[d.weekday()]
    end_txt = "尚未关机" if sess["shutdown"] is None else "关机"
    hours = (t1 - t0) / 3600.0
    end_str = ("现在" if sess["shutdown"] is None
               else dt.datetime.fromtimestamp(t1).strftime("%H:%M"))

    L: list[str] = []
    L.append(f"# {d:%Y年%m月%d日}（周{week}）")
    L.append("")
    L.append(f"**{d:%H:%M} 开机 → {end_str}（{end_txt}）　共 {hours:.1f} 小时**")
    if not sess.get("clean", True):
        L.append(f"> ⚠ 非正常关机（{sess.get('reason') or '未记录原因'}）")
    L.append("")

    if res.get("headline"):
        L.append(f"## 一句话")
        L.append(res["headline"])
        L.append("")

    L.append("## 做了什么")
    L.append("")
    if not res.get("tasks"):
        L.append("（这次没有提取到可归纳的任务）")
        L.append("")
    for n, t in enumerate(res["tasks"], 1):
        L.append(f"### {n}. {_span(facts, t)}　{t['title']}")
        if t["what"]:
            L.append(t["what"])
        arts = [facts["artifacts"][i] for i in t["artifacts"]]
        if arts:
            L.append("")
            L.append("**产出/动过的文件**")
            for a in arts:
                if a["collapsed"]:
                    L.append(f"- `{a['dir']}`（{a['count']} 个文件，例："
                             f"{'、'.join(a['sample'])}）")
                else:
                    flag = "新建" if a["is_new"] else "修改"
                    L.append(f"- `{a['path']}`　{flag}　{a['size']/1024:.1f}KB")
                    pk = (a.get("peek") or "").strip()
                    if pk:
                        L.append(f"  > {pk[:150]}")
        scr = [facts["screens"][i] for i in t["screens"]]
        if scr:
            L.append("")
            L.append("**当时在用什么**")
            for s in scr:
                doc = f"，涉及 {'、'.join(s['docs'])}" if s["docs"] else ""
                L.append(f"- {vss.fmt(s['start'])}–{vss.fmt(s['end'])}　"
                         f"{s['proc']}　{s['minutes']:.0f} 分钟{doc}")
        # 时间窗内的语音
        lo = min([facts["artifacts"][i]["last"] for i in t["artifacts"]] or [0]) or None
        hi = max([facts["artifacts"][i]["last"] for i in t["artifacts"]] or [0]) or None
        for i in t["screens"]:
            s = facts["screens"][i]
            lo = s["start"] if lo is None else min(lo, s["start"])
            hi = s["end"] if hi is None else max(hi, s["end"])
        said = [v for v in facts["voice"]
                if lo and hi and v["start"] >= lo - 600 and v["start"] <= hi + 600
                and not v.get("idle") and v.get("text")]
        if said:
            L.append("")
            L.append("**说过**")
            for v in said[:4]:
                L.append(f"- {vss.fmt(v['start'])}　[{v['spk']}]　{v['text'][:90]}")
        L.append("")

    # ---- 覆盖情况 ----
    L.append("## 记录覆盖")
    L.append("")
    ev = []
    if facts["screens"]:
        ev.append(facts["screens"][0]["start"])
        ev.append(max(s["end"] for s in facts["screens"]))
    if facts["voice"]:
        ev.append(facts["voice"][0]["start"])
    if ev:
        L.append(f"- 有记录：{vss.fmt(min(ev))} – {vss.fmt(max(ev))}")
        gap = (min(ev) - t0) / 60.0
        if gap > 20:
            L.append(f"- 空白：{vss.fmt(t0)} – {vss.fmt(min(ev))}"
                     f"（{gap/60:.1f} 小时没有屏幕或语音数据）")
    else:
        L.append("- 这次没有任何屏幕或语音数据")
    if facts["print"]["enabled"]:
        n = len(facts["print"]["items"])
        L.append(f"- 打印：{n} 条" if n else "- 打印：无")
    else:
        L.append("- 打印：系统打印日志未启用，取不到")
    if facts["truncated"]:
        L.append("- ⚠ 文件扫描超时被截断，可能有遗漏")
    L.append("")
    L.append("---")
    L.append(f"<sub>数据来源：系统事件日志 · voice.db · Windrecorder · 文件系统　"
             f"|　扫描 {facts['scanned']} 个文件　|　"
             f"生成于 {dt.datetime.now():%Y-%m-%d %H:%M}</sub>")
    return "\n".join(L)


# ------------------------------------------------------------------ 主流程

def pick_session(a) -> dict | None:
    if a.boot:
        want = time.mktime(time.strptime(a.boot, "%Y-%m-%dT%H:%M"))
        for s in vss.sessions():
            if abs(s["boot"] - want) < 120:
                return s
        return None
    if a.prev:
        return vss.unresolved()
    cur = vss.current()
    if cur:
        return cur
    return vss.sessions()[-1] if vss.sessions() else None


# ------------------------------------------------------------------ 总账

def _shots(t0: float, t1: float, n: int = 1) -> list[Path]:
    """从每小时快照里挑时间最接近的几张。

    注意：**不能去 cache_screenshot 找**。那是 Windrecorder 的临时缓存，
    6 小时后就会被 vclean 删掉 —— 链接会变成死链，点进去 404。
    真正能长期打开的只有 data/snapshots 里的小图。
    """
    base = vcfg.DATA / "snapshots"
    if not base.is_dir():
        return []
    best: list[tuple[float, Path]] = []
    mid = (t0 + t1) / 2.0
    for p in base.rglob("*.jpg"):
        try:
            # 目录名是日期，文件名是小时：2026-10-24/17-00.jpg
            ts = time.mktime(time.strptime(p.parent.name + " " + p.stem[:2] + ":00:00",
                                           "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if t0 - 3600 <= ts <= t1 + 3600:
            best.append((abs(ts - mid), p))
    best.sort()
    return [p for _, p in best[:n]]


def _links(facts: dict, detail: Path | None) -> list[tuple[str, str]]:
    """整场级别的链接。录音和截图已经各自挂到所属时段了，这里只留完整记录。"""
    out: list[tuple[str, str]] = []
    if detail and detail.exists():
        out.append(("完整记录", _url(detail)))
    return out


EV_DIR = REPORT_DIR / "ev"


def _ev_slot(q: Path) -> Path:
    """证据在 reports/ev 下的落点。"""
    suf = q.suffix.lower()
    if suf in (".wav", ".pcm", ".mp3", ".m4a", ".flac"):
        cat = "audio"
    elif suf in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        cat = "shot"
    else:
        cat = "doc"
    # 截图的名字是 "22-00.jpg"，隔天就撞名，所以按日期分层；录音本身带时间戳。
    day = ""
    if cat in ("audio", "shot"):
        parent = q.parent.name
        day = parent if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parent) else ""
    base = q.stem if cat != "doc" else f"{q.stem}-{q.parent.name}"
    return EV_DIR / cat / day / (base + (".html" if cat == "doc" and suf in (".md", ".markdown") else suf))


def _hardlink(src: Path, dst: Path) -> None:
    """把证据链到 reports/ev 下。

    为什么不在原地引用：预览窗口那个静态服务会把 URL 解析成真实路径，
    `..` 和目录联接（junction）一律 403 —— 只有**真实躺在 reports 下面的文件**
    才给。硬链接在 NTFS 上就是同一份数据多一个名字，不占额外空间，删链接
    也不影响原文件，正合适。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst.exists():
            a, b = src.stat(), dst.stat()
            if a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime):
                return                    # 已经链过，内容也没变
            dst.unlink()
        os.link(src, dst)
    except OSError:                       # 跨分区等情况，退回复制
        shutil.copy2(src, dst)


def _url(p) -> str:
    """证据链接 —— 指向 reports/ev 下的**真实文件**，写成相对路径。

    踩过的三个坑，一个都不能少：
    1. 绝对 `file:///E:/…` 在预览窗口（http）里会被浏览器静默拦掉，看着像链接坏了。
    2. `../data/…` 这类带 `..` 的相对路径，预览服务的静态服务会 403。
    3. 目录联接（junction）同样 403 —— 它会先解析成真实路径再看在不在根下。

    所以最终做法：把每个证据硬链接进 reports/ev，链接就是 `ev/audio/…` 这种
    不带 `..` 的相对路径。双击打开（file://）和预览窗口（http）两边都通。
    """
    q = Path(p)
    try:
        q = q.resolve()
    except Exception:
        pass
    if not q.is_file():
        return "file:///" + str(q).replace("\\", "/").lstrip("/")

    dst = _ev_slot(q)
    try:
        if dst.suffix.lower() == ".html" and q.suffix.lower() in (".md", ".markdown"):
            _write_md_page(q, dst)
        else:
            _hardlink(q, dst)
        rel = dst.relative_to(REPORT_DIR).as_posix()
        # 截图额外裁一份去掉空白桌面的。原图留在旁边，胶囊里给"原图"入口，
        # 裁错了也能回头核对 —— 裁切永远不删原图。
        if _is_shot(dst):
            crop = _crop_shot(dst)
            if crop is not None:
                rel = crop.relative_to(REPORT_DIR).as_posix()
        return rel
    except Exception:
        return "file:///" + str(q).replace("\\", "/").lstrip("/")


def _is_shot(p: Path) -> bool:
    return (p.parent.name == "shot"
            and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))


def _crop_shot(dst: Path) -> Path | None:
    """把截图里大片空白桌面裁掉，返回裁好的副本；裁不出就返回 None。

    判据是灰度双峰度（见 vcrop 模块头说明）。只在产物比原图旧时才重裁 ——
    实时模式每 5 分钟跑一次，不缓存的话每轮都把所有历史截图重算一遍。
    """
    crop = dst.with_name(dst.stem + ".crop.jpg")
    try:
        if crop.exists() and crop.stat().st_mtime >= dst.stat().st_mtime:
            return crop if crop.stat().st_size else None
        did, _why = vcrop.crop_file(dst, crop)
        return crop if (did and crop.exists() and crop.stat().st_size) else None
    except Exception as e:
        log.warning("裁截图失败 %s：%s", dst.name, e)
        return None


def _write_md_page(src: Path, dst: Path) -> None:
    """md 转成能直接在浏览器里读的 html。

    预览服务按扩展名放行，`.md` 一律 404 —— 手册、代码档案、说明全是 md，
    不转就永远是死链。转换只求"读得下去"，不求还原原始排版。
    """
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    body = _md_to_html(src.read_text(encoding="utf-8", errors="replace"))
    page = ("<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{_esc(src.stem)}</title><style>"
            "body{background:#0e1013;color:#c9d2de;font:15px/1.85 -apple-system,"
            "'Segoe UI','Microsoft YaHei',sans-serif;max-width:860px;margin:0 auto;"
            "padding:48px 28px 80px}h1,h2,h3,h4{color:#e8eef7;line-height:1.4;"
            "margin:1.8em 0 .6em}h1{font-size:24px;border-bottom:1px solid #232a34;"
            "padding-bottom:14px}h2{font-size:19px}h3{font-size:16px}"
            "code{background:#1a1f27;color:#8fb7ff;padding:2px 6px;border-radius:4px;"
            "font-size:13px}pre{background:#14181e;border:1px solid #222933;"
            "border-radius:8px;padding:14px 16px;overflow:auto}pre code{background:none;"
            "padding:0;color:#b9c4d2;font-size:12.5px;line-height:1.7}"
            "table{border-collapse:collapse;width:100%;margin:1em 0;font-size:14px}"
            "th,td{border:1px solid #262e39;padding:8px 12px;text-align:left}"
            "th{background:#171c23;color:#dfe6f0}a{color:#8fb7ff}"
            "blockquote{border-left:3px solid #2b3a52;margin:1em 0;padding:.2em 0 .2em 16px;"
            "color:#8b96a6}li{margin:.35em 0}"
            "</style><body>" + body + "</body></html>")
    _atomic_write(dst, page)


# ---- 极简 markdown → html（只覆盖文档里真实出现的语法）----
_MD_INLINE_RULES = (
    (re.compile(r"`([^`]+)`"),
     r'<code>\1</code>'),
    (re.compile(r"\*\*([^*]+)\*\*"),
     r'<strong>\1</strong>'),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"),
     r'<em>\1</em>'),
    (re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)"),
     r'<a href="\2">\1</a>'),
)


def _esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def _md_inline_html(t: str) -> str:
    """行内标记。先转义再把标记换回来，避免把 html 当文本渲染。"""
    placeholders: list[str] = []

    def hold(m: re.Match) -> str:
        placeholders.append(m.group(1))
        return f"\x00{len(placeholders) - 1}\x00"

    t = re.sub(r"`([^`]+)`", hold, t)
    t = _esc(t)
    for pat, rep in _MD_INLINE_RULES[1:]:
        t = pat.sub(rep, t)
    for i, code in enumerate(placeholders):
        t = t.replace(f"\x00{i}\x00", f"<code>{_esc(code)}</code>")
    return t


def _md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + _md_inline_html(" ".join(para)) + "</p>")
            para.clear()

    while i < n:
        ln = lines[i]
        s = ln.strip()

        if s.startswith("```"):                      # 代码块
            flush()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>" + _esc("\n".join(buf)) + "</code></pre>")
            continue

        if re.fullmatch(r"\|?[\s:|-]+\|[\s:|-]*", s) and "-" in s and "|" in s:
            i += 1                                    # 表格分隔行，跳过
            continue

        if s.startswith("|") and s.endswith("|"):     # 表格
            flush()
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cur = lines[i].strip()
                if not re.fullmatch(r"\|?[\s:|-]+\|[\s:|-]*", cur):
                    cells = [c.strip() for c in cur.strip("|").split("|")]
                    rows.append(cells)
                i += 1
            if rows:
                head = "".join(f"<th>{_md_inline_html(c)}</th>" for c in rows[0])
                body = "".join("<tr>" + "".join(
                    f"<td>{_md_inline_html(c)}</td>" for c in r) + "</tr>"
                    for r in rows[1:])
                out.append(f"<table><thead><tr>{head}</tr></thead>"
                           f"<tbody>{body}</tbody></table>")
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            lv = min(len(m.group(1)), 4)
            out.append(f"<h{lv}>{_md_inline_html(m.group(2))}</h{lv}>")
            i += 1
            continue

        if s.startswith(">"):
            flush()
            out.append("<blockquote>" + _md_inline_html(s.lstrip("> ")) + "</blockquote>")
            i += 1
            continue

        if re.match(r"^([-*+]|\d+\.)\s+", s):
            flush()
            tag = "ol" if re.match(r"^\d+\.", s) else "ul"
            items = []
            while i < n and re.match(r"^([-*+]|\d+\.)\s+", lines[i].strip()):
                items.append(re.sub(r"^([-*+]|\d+\.)\s+", "", lines[i].strip()))
                i += 1
            out.append(f"<{tag}>" + "".join(
                f"<li>{_md_inline_html(x)}</li>" for x in items) + f"</{tag}>")
            continue

        if not s:
            flush()
            i += 1
            continue

        if s.startswith("---"):
            flush()
            out.append("<hr>")
            i += 1
            continue

        para.append(s)
        i += 1
    flush()
    return "\n".join(out)


# 证据要跟着时段走。用户的原话：21:26 的录音属于 21:16–21:30 那一段，
# 就该挂在那一节底下，而不是整场堆在末尾 —— 堆在末尾等于没有归属。
def _node_voice_links(voice: list[dict], limit: int = 3) -> list[tuple[str, str]]:
    """这一段里的录音。有转写的、时间长的优先，最后按时间排回去。

    2026-10-25 起，有转写的段额外给一条**"文字稿"**入口 —— 用户原话：
    "找个链接叫录音识别出的文字内容版，我可以点击，直接看录音的文章内容"。
    文字稿文件由检索索引那一轮统一生成（覆盖同一时段的全部段），这里只算
    地址不写文件，免得同一段被写两遍。
    """
    import vsearch

    vs = [v for v in voice if v.get("wav")]
    if not vs:
        return []
    vs.sort(key=lambda v: ((v.get("text") or "").strip() != "",
                           v["end"] - v["start"]), reverse=True)
    picked = sorted(vs[:limit], key=lambda v: v["start"])
    out: list[tuple[str, str]] = []
    for v in picked:
        out.append((f"{_fmt_hm(v['start'])} 录音", _url(v["wav"])))
        segs = [s for s in (v.get("segs") or []) if (s.get("text") or "").strip()]
        # 内容多的优先（一条合并条目可能跨好几段），挑两段够读了
        segs.sort(key=lambda s: len(s["text"]), reverse=True)
        for s in sorted(segs[:2], key=lambda s: s["start"]):
            out.append((f"{_fmt_hm(s['start'])} 文字稿",
                        _ensure_transcript(s, v)))
    return out


def _ensure_transcript(seg: dict, line: dict) -> str:
    """给这一段写文字稿（已写过且内容没变就复用），返回链接。

    单独走这一步而不是全靠检索索引那一轮：索引只覆盖最近若干天，而总账
    里的老条目也挂着"文字稿"链接 —— 链接点过去必须有东西，不能 404。
    """
    import vsearch

    rec = {"id": seg["id"], "day": time.strftime("%Y-%m-%d",
                                                 time.localtime(seg["start"])),
           "hm": _fmt_hm(seg["start"]), "dur": seg.get("dur", 0.0),
           "spk": line.get("spk") if line.get("spk") not in (None, "?") else "",
           "text": seg.get("text") or "", "emotion": seg.get("emotion") or "",
           "labels": seg.get("labels") or line.get("labels") or []}
    return vsearch.write_transcript(rec, _url(seg["wav"]) if seg.get("wav") else "")


def _node_shots(t0: float, t1: float, limit: int = 2) -> list[tuple[str, str]]:
    """这一段覆盖到的整点截图。

    只挂真正落在段内的。贪心挂"最接近的那张"会把 20 点的图挂到 22 点的
    节点上，点开一看牛头不对马嘴。
    """
    base = vcfg.DATA / "snapshots"
    if not base.is_dir():
        return []
    out: list[tuple[str, str]] = []
    d = dt.datetime.fromtimestamp(t0).date()
    end = dt.datetime.fromtimestamp(t1).date()
    while d <= end and len(out) < limit:
        folder = base / f"{d:%Y-%m-%d}"
        if folder.is_dir():
            for p in sorted(folder.glob("*.jpg")):
                hh = p.stem[:2]
                if not hh.isdigit():
                    continue
                try:
                    ts = time.mktime(time.strptime(f"{d:%Y-%m-%d} {hh}:00:00",
                                                   "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if t0 <= ts <= t1:
                    out.append((f"{int(hh)} 点屏幕", _url(p)))
                    if len(out) >= limit:
                        break
        d += dt.timedelta(days=1)
    return out


# 段落里已经提到离座/走神时，就不再补一句，免得多出一段重复的话。
_DRIFT_WORDS = ("手机", "抖音", "快手", "刷", "打发", "走神", "离座",
                "放节目", "放东西", "放视频", "没动过", "不在电脑前")


def _said_drift(para: str) -> bool:
    return any(w in para for w in _DRIFT_WORDS)


# ------------------------------------------------------------------- 时间轴
#
# 用户要的不是"一次开机一段话"，而是竖向时间轴：每个时间节点一个标题，
# 标题下面分三块 —— 屏幕在干什么、电脑上存了什么、耳里听到什么。
#
# 切节点的原则是"按人干活的分段"，不是按小时切：
#   1. 屏幕活动段本身就是"同一软件连续在用"，天然是一段
#   2. 短于 8 分钟的小段（切个资源管理器、弹个窗）并到邻居，不单独成节
#   3. 没有屏幕记录但还在开机的空档，单独成节并如实说明"没记录"

GAP_MERGE = 8 * 60.0      # 小于 8 分钟的小段并到邻居
MIN_NODE = 20 * 60.0      # 空档超过 20 分钟才单独成节

SYS_NODE = """你在替用户写一条"开机时间轴"里的节点。给你一段时间里机器观察到的事实，你写一个标题和三行说明。

铁律：
1. 只能写事实里有的内容。不许编造软件名、文件名、网址、节目名。
2. 某一块没有数据就直说没记录，绝对不要猜。
3. 绝对不许出现目录名、磁盘路径、文件扩展名、英文下划线的代码名。
4. 不要评论你收到的材料本身。禁止"清单""事实""材料""记录里显示"这类词。
5. 不要说"好的""根据……"，直接写。
6. 严格按下面四行格式输出，一行一个字段，不要多余的话、不要空行：

标题：不超过 14 个字，说清这段时间在干什么（"检查 Qwen 模型下载进度"好过"操作软件"）
屏幕：2~3 句。必须说清"在哪个软件里、屏幕上是哪份文档或哪类内容"。
      **禁止这几种开头**："软件正在运行""窗口保持在工作状态""界面显示……
      等文字信息""界面显示方案与指令"。这类句子一个信息都没有，等于没写。
      要写具体的东西：在看哪份文档、哪个页面、哪段代码、哪个报错。
日志：2~3 句。这段时间存了或改了哪些文档、改了什么。
      确实没动文件就直说"这段没存也没改文件"，然后说当时在干什么；
      不要用"系统仅完成了启动流程""未对现有文件进行编辑操作"这种没有信息量的句子凑字数。
语音：2~3 句。听到的是什么——在放什么节目/音乐、谁在说话、大致在聊什么。
      **不要逐字引用转写原文**：语音识别有错字，引出来是"俄我残隽"这种东西。
      说了几段就写几段，别写"十几段"这种没数过的数。
      听不出内容就写"当时有声音，听不出在说什么"，不要编，也不要猜。
      事实里把某几句标成了"有声音但没听出能辨认的话"时，那几句一个字都不许引。

7. 每块 40~80 字，写具体一点，不要一句话打发。
8. 简体中文。语气平实，像同事随口说的。"""


def _fmt_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def timeline_nodes(facts: dict) -> list[dict]:
    """把一次开机切成若干"有意义的时段"。返回按时间升序的节点列表。"""
    t0, t1 = facts["t0"], facts["t1"]
    scr = sorted(facts["screens"], key=lambda s: s["start"])

    nodes: list[dict] = []
    if scr:
        # 1) 长段留作主干，短段先攒起来
        main = [s for s in scr if s["minutes"] >= GAP_MERGE / 60.0]
        minor = [s for s in scr if s["minutes"] < GAP_MERGE / 60.0]
        if not main:                       # 全是小段 → 整体并成一段
            main = [{"proc": scr[0]["proc"], "start": scr[0]["start"],
                     "end": scr[-1]["end"],
                     "minutes": sum(s["minutes"] for s in scr),
                     "titles": [t for s in scr for t in s["titles"]][:3],
                     "docs": [d for s in scr for d in s["docs"]][:5],
                     "ocr": max((s["ocr"] for s in scr), key=len, default="")}]
            minor = []

        # 2) 小段并入时间上最近的干段
        for s in minor:
            best, best_gap = None, None
            for m in main:
                gap = min(abs(s["start"] - m["end"]), abs(m["start"] - s["end"]))
                if best_gap is None or gap < best_gap:
                    best, best_gap = m, gap
            if best is None:
                continue
            best["start"] = min(best["start"], s["start"])
            best["end"] = max(best["end"], s["end"])
            best["minutes"] += s["minutes"]
            for d in s["docs"]:
                if d not in best["docs"]:
                    best["docs"].append(d)

        for m in main:
            nodes.append({"start": m["start"], "end": m["end"],
                          "minutes": round((m["end"] - m["start"]) / 60.0, 1),
                          "kind": "work", "app": m["proc"] or "?",
                          "titles": list(m["titles"])[:3],
                          "docs": list(m["docs"])[:5],
                          "ocr": m.get("ocr") or ""})

        # 3) 开机到第一个屏幕段之间，以及段与段之间的长空档，各成节点
        edges = [(t0, nodes[0]["start"] if nodes else t1)]
        for a, b in zip(nodes, nodes[1:]):
            edges.append((a["end"], b["start"]))
        if nodes:
            edges.append((nodes[-1]["end"], t1))
        for a, b in edges:
            if b - a >= MIN_NODE:
                nodes.append({"start": a, "end": b,
                              "minutes": round((b - a) / 60.0, 1),
                              "kind": "gap", "app": "", "titles": [],
                              "docs": [], "ocr": ""})
    else:
        nodes.append({"start": t0, "end": t1,
                      "minutes": round((t1 - t0) / 60.0, 1),
                      "kind": "gap", "app": "", "titles": [], "docs": [],
                      "ocr": ""})

    nodes.sort(key=lambda n: n["start"])

    # 3.5) 掐掉交叠。屏幕段本身可能首尾重叠（截图落盘晚于切段），
    # 不掐就会在时间轴上出现 20:49–21:16 紧接 21:14–21:30 这种自相矛盾的读数。
    for i in range(1, len(nodes)):
        if nodes[i]["start"] < nodes[i - 1]["end"]:
            nodes[i]["start"] = nodes[i - 1]["end"]
            if nodes[i]["start"] >= nodes[i]["end"]:      # 被掐没了，留半分钟
                nodes[i]["start"] = nodes[i]["end"] - 30
            nodes[i]["minutes"] = round(
                (nodes[i]["end"] - nodes[i]["start"]) / 60.0, 1)

    # 3.6) 尾巴太短就并进前一段。实时模式下最后一段永远在长，切出一个十分钟
    # 的碎段，模型手里没多少料，写出来必是"软件正在运行"这种空话。
    if len(nodes) >= 2 and nodes[-1]["minutes"] < 15 \
            and nodes[-1]["kind"] == nodes[-2]["kind"]:
        tail, prev = nodes[-1], nodes[-2]
        prev["end"] = tail["end"]
        prev["minutes"] = round((prev["end"] - prev["start"]) / 60.0, 1)
        for d in tail.get("docs") or []:
            if d not in prev["docs"]:
                prev["docs"].append(d)
        nodes.pop()

    # 4) 挂上文件变动、录音、离座标记
    arts = facts["artifacts"]
    real = {Path(a["name"]).stem for a in arts if not a["collapsed"]}
    for n in nodes:
        n["docs"] = _canon_docs(n["docs"], real)
        n["files"] = []
        n["art_paths"] = []
        for a in arts:
            if a["collapsed"]:
                continue
            if n["start"] <= a["last"] <= n["end"]:
                nm = _human_name(a["name"])
                if nm and nm not in n["files"]:
                    n["files"].append(nm)
                    n["art_paths"].append((nm, a["path"]))
        n["voice"] = [v for v in facts["voice"]
                      if n["start"] <= v["start"] <= n["end"]]
        n["idle"] = [b for b in (facts.get("idle") or [])
                     if n["start"] <= b["start"] <= n["end"]]
        n["voice_links"] = _node_voice_links(n["voice"])
        n["shot_links"] = _node_shots(n["start"], n["end"])
        n["doc_links"] = [(nm, _url(p)) for nm, p in n["art_paths"][:4]]
    return nodes


def _voice_kind(v: dict) -> str:
    """把一条录音归成"谁在说/在放什么"，供模型和兜底文案共用。

    措辞要经得起被模型原样抄进正文：写成"分不清是谁"，模型就会在叙事里
    来一句"音响里一直放着声音分不清是谁在说话"。所以用中性的标签说法。
    """
    if v.get("kind") == "event":
        return "活动声"
    spk = v.get("spk") or "?"
    if spk == "我":
        return "自己说话"
    if spk == "其他":
        return "别人说话"
    return "说话人未识别"


def _voice_sample(voice: list[dict]) -> str | None:
    """挑一句能当例子的转写。太短、夹着外文乱码的一律不要。

    实测噪声样本长这样：`.그.`、`我从买行네`、`嗯少人所以说跟这种天了`。
    SenseVoice 拿不准语言时会蹦出韩文/日文假名，这种句子放进交付物只会让人
    一头雾水，所以要求"汉字占绝对多数"才留下。
    """
    for v in voice:
        t = (v.get("text") or "").strip(" .。,，、·-—_")
        if len(t) < 6:
            continue
        cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
        foreign = sum(1 for ch in t if "\uac00" <= ch <= "\ud7af"      # 韩文
                      or "\u3040" <= ch <= "\u30ff")                   # 假名
        if cjk < 5 or foreign or cjk / len(t) < 0.7:
            continue
        return t[:30]
    return None


# 屏幕上"认到的文件名"经常是命令行或路径的残片。
# 这些一律不进正文 —— 用户看到 `python -m venv` 只会觉得乱。
_BAD_DOC_HINTS = ("install", "pip", "python", "cmd.exe", "http",
                  "www.", "\\user", "c:/", "d:/", "e:/")


def _doc_name(d: str) -> str | None:
    stem = Path(d).stem
    if not stem or len(stem) > 20:
        return None
    low = stem.lower()
    if any(b in low for b in _BAD_DOC_HINTS):
        return None
    return _human_name(stem)


def _canon_docs(docs: list[str], real: set[str]) -> list[str]:
    """屏幕 OCR 认出来的文件名要和磁盘上真实存在的对一遍。

    实测问题：OCR 把"使用手册"读成"创建用手册""已读取用手册"，这种名字
    直接写进交付物很难看。做法是拿它跟这次真正动过的文件名比相似度，
    够像就用真名，差太远就整个丢掉 —— 宁可少写一个，不写错的。
    """
    out: list[str] = []
    for d in docs:
        nm = _doc_name(d)
        if not nm:
            continue
        if nm in real:
            pick = nm
        else:
            best, score = None, 0.0
            for r in real:
                s = difflib.SequenceMatcher(None, nm, r).ratio()
                if s > score:
                    best, score = r, s
            if score >= 0.6 and best:
                pick = best
            elif len(nm) <= 8:
                pick = nm          # 短的中文名大概率是对的
            else:
                continue
        if pick not in out:
            out.append(pick)
    return out


def _node_brief(n: dict) -> str:
    """一个节点给模型的浓缩事实。"""
    L = [f"【时段】{_fmt_hm(n['start'])}–{_fmt_hm(n['end'])}"
         f"（{n['minutes']:.0f} 分钟）"]
    if n["kind"] == "gap":
        L.append("【屏幕】这段时间没有屏幕记录")
    else:
        L.append(f"【软件】{n['app']}")
        if n["titles"]:
            L.append("【窗口标题】" + "；".join(t[:40] for t in n["titles"]))
        if n["ocr"]:
            L.append("【屏幕上的字】" + n["ocr"][:260])
        else:
            L.append("【屏幕上的字】没认出来")
    docs = n["docs"]
    if docs:
        L.append("【屏幕里出现的文档名】" + "、".join(docs[:4]))
    if n["files"]:
        L.append("【动过的文档】" + "、".join(n["files"][:6]))
    else:
        L.append("【动过的文档】没有存或改的文档")
    if n["voice"]:
        bits = []
        for v in n["voice"][:5]:
            t = (v.get("text") or "").strip(" .。,，")
            if v.get("kind") == "event":
                bits.append(f"{_fmt_hm(v['start'])} 活动声"
                            f"（{'、'.join(v['labels'])}）")
            elif len(t) >= 4:
                bits.append(f"{_fmt_hm(v['start'])} {_voice_kind(v)}「{t[:40]}」")
            else:
                bits.append(f"{_fmt_hm(v['start'])} {_voice_kind(v)}（音响里在放东西，没转出字）")
        L.append(f"【录音 {len(n['voice'])} 段】" + "；".join(bits))
    else:
        L.append("【录音】这段没听到人声（录音在跑，只是没判出说话）")
    if n["idle"]:
        L.append("【离座】屏幕长时间没变化，期间有声音，判断为离开电脑/走神")
    return "\n".join(L)


def _parse_node(txt: str) -> dict | None:
    """把模型那四行拆成字段。缺哪个就留空，让兜底文案接手。"""
    out: dict[str, str] = {}
    keymap = {"标题": "title", "屏幕": "screen", "日志": "log", "语音": "voice"}
    for ln in (txt or "").splitlines():
        s = ln.strip().lstrip("-*〇· ").strip()
        for k, f in keymap.items():
            if s.startswith(k) and s[len(k):len(k) + 1] in ("：", ":"):
                out[f] = s[len(k) + 1:].strip()
                break
    if not out.get("title") and not out.get("screen"):
        return None
    return out


def ask_node(brief: str, timeout: float = 120.0) -> dict | None:
    """让本地模型写一个节点。失败返回 None。"""
    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_ctx": 3072, "num_predict": 480},
        "messages": [{"role": "system", "content": SYS_NODE},
                     {"role": "user", "content": brief}],
    }
    try:
        d = _post_chat(payload, timeout)
        txt = ((d.get("message") or {}).get("content") or "").strip()
        return _parse_node(txt)
    except Exception as e:
        print(f"    节点模型失败：{e}", flush=True)
        return None


def _fallback_node(n: dict) -> dict:
    """模型没写出来时的确定性文案。宁可干巴，不许编。"""
    app = (n["app"] or "").replace(".exe", "")
    if n["kind"] == "gap":
        title = "没有屏幕记录的一段"
        screen = "这段电脑开着，但没留下屏幕记录，看不到当时在做什么。"
    else:
        docs = n["docs"]                 # 已经在 timeline_nodes 里校准过
        title = f"{app} 里干活" if app else "在电脑前"
        screen = f"一直在用 {app}。" if app else "有屏幕活动，但认不出具体软件。"
        if docs:
            screen += "屏幕上出现过「" + "」「".join(docs[:2]) + "」。"
            title = f"处理「{docs[0]}」"    # 有文档名就别退回到"某某里干活"
        elif n.get("files"):
            title = f"改了{n['files'][0]}"

    log = ("存了或改了 " + "、".join(n["files"][:4]) + "。") if n["files"] \
        else "这段时间没有存或改文档。"

    evs = [v for v in n["voice"] if v.get("kind") == "event"]
    talks = [v for v in n["voice"] if not v.get("kind") == "event"]
    voice = ""
    if talks:
        kinds: dict[str, int] = {}
        for v in talks:
            k = _voice_kind(v)
            kinds[k] = kinds.get(k, 0) + 1
        voice = "录到 " + "、".join(f"{k} {c} 段" for k, c in kinds.items())
        sample = _voice_sample(talks)
        if sample:
            voice += f"；其中一句是「{sample}」"
        voice += "。"
    if evs:
        c: dict[str, int] = {}
        for v in evs:
            for lb in v["labels"]:
                c[lb] = c.get(lb, 0) + v.get("n", 1)
        voice += ("另" if talks else "") + "录到活动声：" + \
            "、".join(f"{k} {n} 次" for k, n in c.items()) + "。"
    if not talks and not evs:
        voice = "这段没听到人声。"
    if n["idle"]:
        voice += " 屏幕长时间没动，判断那会儿离开电脑了。"
    return {"title": title, "screen": screen, "log": log, "voice": voice}


_LAZY = {"无", "无。", "没有", "没有。", "无记录", "没有记录",
         "无内容", "没记录", "未知", "暂无", "空"}


def _polish_node(txt: str | None, fallback: str) -> str:
    """模型偷懒写"无。"时，换成完整的句子 —— 交付物里不该出现一个"无"字。"""
    t = (txt or "").strip().strip('"“”「」')
    if not t or len(t) <= 5 and t.rstrip("。.") in _LAZY:
        return fallback
    if len(t) > 160:                      # 跑飞了就截到最后一个句号
        cut = t[:160].rfind("。")
        t = t[:cut + 1] if cut > 40 else t[:160]
    return t


# 模型描述"没听到人声"时喜欢写成"没有录音数据""录音没有声音"。事实上录音
# 一直在跑，只是这几段判成噪声没送转写 —— 说"没有录音数据"是错的，得纠正。
_NO_VOICE_RE = re.compile(
    r"没有录[音到]|没录[音到]|未录[音到]|没有录音数据|未检测到|没有检测到"
    r"|没有声音|没声音|无语音|没有语音|未听到|没有听到")
_NO_VOICE_FIX = "这段时间没听到人声，只有环境噪声。"


def _norm_voice(txt: str | None, fallback: str) -> str:
    t = _polish_node(txt, fallback)
    if len(t) <= 34 and _NO_VOICE_RE.search(t):
        return _NO_VOICE_FIX
    return t


# ------------------------------------------------- 节点文案缓存（实时模式用）
#
# 为什么要有这层缓存：
#   实时模式每 5 分钟重算一次。一次开机到晚上有八九个节点，全量调模型是
#   9×6 秒 ≈ 1 分钟，机器一直在响风扇，用户会以为中病毒了。而已经过去的
#   节点（比如上午那一段）事实再也不变，没有重写的理由 —— 只有"当前这一
#   段"还在长。所以缓存按 开机时间+起止秒 做键，冻结的节点直接复用。
#
# 故意只缓存模型真的写出来的东西：模型没起来时兜底的干巴句子不进缓存，
# 否则一次掉线就把那段话永久钉死了。

NODE_CACHE = vcfg.DATA / "node_cache.json"


def _node_key(boot: float, n: dict) -> str:
    return f"{int(boot)}|{int(n['start'])}|{int(n['end'])}"


def load_node_cache(boot: float) -> dict:
    """只认当前这次开机的缓存。换一次开机就是另一套节点，旧键永远对不上。"""
    try:
        d = json.loads(NODE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if int(d.get("boot", 0)) != int(boot):
        return {}
    nodes = d.get("nodes")
    return nodes if isinstance(nodes, dict) else {}


def save_node_cache(boot: float, nodes: dict) -> None:
    try:
        NODE_CACHE.write_text(json.dumps(
            {"boot": int(boot), "nodes": nodes}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass


def build_timeline(facts: dict, use_model: bool = True,
                   max_nodes: int = 9, cache: dict | None = None) -> list[dict]:
    """切节点并把每个节点的三块文案备好。"""
    nodes = timeline_nodes(facts)
    # 节点太多时，把最长的几个留着，其余按时间就近并入 —— 总账不该变成流水账
    if len(nodes) > max_nodes:
        nodes.sort(key=lambda n: -n["minutes"])
        keep = sorted(nodes[:max_nodes], key=lambda n: n["start"])
        dropped = nodes[max_nodes:]
        for d in dropped:
            near = min(keep, key=lambda k: min(
                abs(d["start"] - k["end"]), abs(k["start"] - d["end"])))
            near["start"] = min(near["start"], d["start"])
            near["end"] = max(near["end"], d["end"])
        nodes = keep

    boot = facts["t0"]
    prev_title = ""
    for n in nodes:
        key = _node_key(boot, n)
        got = (cache or {}).get(key)
        reused = got is not None
        if not reused:
            got = ask_node(_node_brief(n)) if use_model else None
            if got and cache is not None:
                cache[key] = got          # 只存模型真写出来的
        fb = _fallback_node(n)
        title = (got or {}).get("title") or ""
        title = title.strip().strip("。.：:").strip()
        # 模型爱写长标题（"在 WorkBuddy 中处理文档并检查配置"）。以前超过 18 字
        # 就整个丢掉退回兜底的"WorkBuddy 里干活"，结果一条时间轴三个节点同名。
        # 现在只做收缩，不轻易丢：在标点或连词处断开。
        if len(title) > 20:
            cut = title[:20]
            for sep in ("，", "、", "；", " ", "并", "和", "与", "及"):
                i = cut.rfind(sep)
                if i >= 7:
                    cut = cut[:i]
                    break
            title = cut.strip()
        if not title:
            title = fb["title"]
        if not title or len(title) < 4:      # "WorkBuddy" 单独一个词不叫标题
            title = fb["title"]
        # 纯英文的标题基本都是把软件名当了标题（"WorkBuddy"）。这不是概括，
        # 交付物上写着一个软件名等于什么都没写。
        if not re.search(r"[\u4e00-\u9fff]", title):
            title = fb["title"]
        # 相邻两段同标题（"WorkBuddy 里干活" 连着出现两次）读起来像复制粘贴，
        # 后一段标"（续）"，一眼能看出是同一件事的延续。
        if title and title == prev_title:
            title = title + "（续）"
        prev_title = title
        n["title"] = title
        n["screen_txt"] = _polish_node((got or {}).get("screen"), fb["screen"])
        n["log_txt"] = _polish_node((got or {}).get("log"), fb["log"])
        n["voice_txt"] = _norm_voice((got or {}).get("voice"), fb["voice"])
        n["reused"] = reused
    return nodes


def render_nodes(nodes: list[dict], newest_first: bool = True) -> str:
    """把节点渲染成 md 的 ### 小节。HTML 那边按同样结构解析。

    倒序：离现在最近的排最前面。打开总账第一眼看到的应该是"刚才在干什么"，
    而不是从头往下翻十二个小时。
    """
    seq = list(reversed(nodes)) if newest_first else list(nodes)
    out: list[str] = []
    for n in seq:
        span = (_fmt_hm(n["start"]) if n["minutes"] < 2
                else f"{_fmt_hm(n['start'])}–{_fmt_hm(n['end'])}")
        out.append(f"### {span} · {n['title']}")
        out.append(f"**屏幕**　{n['screen_txt']}")
        out.append(f"**电脑日志**　{n['log_txt']}")
        out.append(f"**语音**　{n['voice_txt']}")
        ev = ((n.get("voice_links") or []) + (n.get("shot_links") or [])
              + (n.get("doc_links") or []))
        if ev:
            out.append("**细节**　" + " · ".join(f"[{a}]({b})" for a, b in ev))
        out.append("")
    return "\n".join(out)


def _day_start(ts: float) -> float:
    """ts 所在自然天的 0 点（本地时区）。"""
    d = dt.datetime.fromtimestamp(ts)
    return dt.datetime(d.year, d.month, d.day).timestamp()


def _render_ledger_day(s: dict, t0: float) -> str:
    """md 里那条 `## 年月日 周X …` 的日期部分。

    单独拆出来是因为 live 跨天时 t0 已被抬到当天 0 点，而真实开机时刻
    要照实写 —— 用户看到"25 日 00:00 起"再加一个括注，才知道这不是
    一次新开机，而是昨晚那场的延续。
    """
    d = dt.datetime.fromtimestamp(t0)
    week = "一二三四五六日"[d.weekday()]
    return d, week


def render_ledger(facts: dict, paragraph: str, detail: Path | None,
                  nodes: list[dict] | None = None) -> str:
    sess, t0, t1 = facts["session"], facts["t0"], facts["t1"]
    carry = facts.get("carry")
    d, week = _render_ledger_day(sess, t0)
    hh = int((t1 - t0) // 3600)
    mm = int(((t1 - t0) % 3600) // 60)
    # 还在用的时候别写"现在 结束" —— 人坐在电脑前，页面上却写着结束，很刺眼。
    end_str = ("进行中" if sess["shutdown"] is None
               else dt.datetime.fromtimestamp(t1).strftime("%H:%M") + " 结束")
    # 跨天延续的那条，开机时刻写"00:00 起"并括注真实开机时间。
    # 不能写成"20:48 开机"——日期已经是 25 日了，再写 20:48 会被读成
    # "25 日晚上 8 点开机的"，跟事实相反。
    if carry is not None:
        cd = dt.datetime.fromtimestamp(carry)
        boot_str = (f"00:00 起（接 {cd:%m 月 %d 日 %H:%M} 的开机）")
    else:
        boot_str = f"{dt.datetime.fromtimestamp(sess['boot']):%H:%M} 开机"

    body = paragraph.strip()
    # 走神那段一定要留一句话。模型写了就不重复，没写就由程序补上 ——
    # 这是"如实记录历史"的底线，不能因为模型今天话少就丢了。
    drift = (facts.get("drift") or "").strip()
    if drift and not _said_drift(body):
        body = body + "\n\n" + drift

    L = [f"## {d:%Y年%m月%d日} 周{week}　{boot_str} · {end_str} · "
         f"{hh} 小时 {mm} 分", "", body, ""]
    if nodes:
        L.append(render_nodes(nodes))
    links = _links(facts, detail)
    if links:
        L.append("整场细节：" + " · ".join(f"[{a}]({b})" for a, b in links))
    L += ["", "---", ""]
    return "\n".join(L)


LEDGER = REPORT_DIR / "总账.md"
_BOOT_RE = re.compile(r"^## (\d{4})年(\d{2})月(\d{2})日 .*?(\d{2}):(\d{2}) "
                      r"(?:开机|起)", re.M)


def _boot_key(entry: str) -> str | None:
    """从一条总账里抠出"哪次开机"，如 2026102408:42。

    同一次开机会被写两次：进行中写一次（"至今仍在用"），关机后下次开机
    再写一次（"18:40 结束"）。没有这个 key，总账里同一天会出现两条，
    一条说"还在用"、一条说"已结束"，看着像 bug。
    """
    m = _BOOT_RE.search(entry)
    return "".join(m.groups()) if m else None


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件，再整体替换。

    直接 write_text 是"先清空、再写"，这几十毫秒里谁读文件，谁就会拿到
    一份被截断的页面 —— 实测用户正好赶上过：标题和"最后更新 22:16:34"
    都在，下面的时间轴却只剩第一条，后面一小时凭空消失。浏览器照这份
    残页渲染完就不动了，要等下一次 meta refresh（5 分钟）才恢复。

    os.replace 在同一分区上是原子的：读的人要么看到旧的整份，要么看到
    新的整份，没有中间态。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _seal_stale(block: str, now: float) -> str:
    """把昨天及更早、还写着"进行中"的条目封存成已结束。

    跨天切开之后，昨天那条最后一次被写是在它自己的 23:5x，标题里留着
    "进行中"。不封的话，总账上会永远挂着一个昨天还在跑的会话。
    封存只改标题那一行：结束时刻用当天 24:00（内容确实持续到了零点），
    时长按开机时刻重算。正文一个字不动 —— 那是当时的事实。
    """
    m = _DAY_RE.match(block.lstrip().split("\n", 1)[0])
    if not m or "进行中" not in block:
        return block
    y, mo, dd = (int(x) for x in m.groups()[:3])
    try:
        that = dt.datetime(y, mo, dd)
    except ValueError:
        return block
    today = dt.datetime.fromtimestamp(now)
    if (that.year, that.month, that.day) >= (today.year, today.month, today.day):
        return block
    bm = re.search(r"(\d{1,2}):(\d{2})\s*(?:开机|起)", block)
    if not bm:
        return block
    boot_min = int(bm.group(1)) * 60 + int(bm.group(2))
    total = 24 * 60 - boot_min
    h, mi = divmod(max(total, 0), 60)
    return re.sub(r"·\s*进行中\s*·\s*\d+\s*小时\s*\d+\s*分",
                  f"· 24:00 结束 · {h} 小时 {mi} 分", block, count=1)


def append_ledger(entry: str) -> bool:
    """把新的一段插到最前面（最新在上）。同一次开机的旧条目会被替换。

    返回 True 表示是替换（而不是新增）—— 调用方可以据此少打一行提示。
    """
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    head = "# 总账\n\n"
    old = ""
    if LEDGER.exists():
        txt = LEDGER.read_text(encoding="utf-8")
        old = txt[len(head):] if txt.startswith(head) else txt

    key = _boot_key(entry)
    now = time.time()
    blocks = [b.strip() for b in old.split("\n---") if b.strip()]
    kept: list[str] = []
    replaced = False
    for b in blocks:
        k = _boot_key(b)
        if key and k == key:
            replaced = True
            continue            # 丢掉同一次开机的旧版本
        kept.append(_seal_stale(b, now))

    body = entry.strip()
    if kept:
        body += "\n" + "\n---\n".join("\n" + b for b in kept) + "\n"
    _atomic_write(LEDGER, head + body)
    return replaced


# ------------------------------------------------------------------ HTML 版
#
# .md 双击打不开（Windows 不认），而用户要的是"点进去就能看"。
# 所以每次写完 md 顺手转一份同内容的 html，双击浏览器直接读。

def _md_inline(s: str) -> str:
    import html as _h
    import re
    s = _h.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>总账</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
{refresh_meta}
<style>:root {{ color-scheme: dark; --scale: 1; --cols: 1; --rail: 44px; --lead: 46px; }}
* {{ box-sizing: border-box; }}
html {{ background: #101216; }}
body {{ margin: 0;
       padding: calc(30px * var(--scale)) clamp(14px, 2vw, 40px) calc(70px * var(--scale));
       background: #101216; color: #e6e8eb;
       font: calc(15px * var(--scale))/1.8 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;
       -webkit-font-smoothing: antialiased; }}
main {{ max-width: min(98vw, 3600px); margin: 0 auto; }}
h1 {{ font-size: calc(13px * var(--scale)); letter-spacing: .42em; color: #5c6472;
      font-weight: 500; margin: 0 0 calc(14px * var(--scale)); }}
.hint {{ font-size: calc(12.5px * var(--scale)); color: #5f6a7c;
         margin: calc(-6px * var(--scale)) 0 calc(22px * var(--scale)); }}
.hint b {{ color: #6ecf9b; font-weight: 600; }}
.tip {{ font-size: calc(12px * var(--scale)); color: #4e5866;
        margin: 0 0 calc(18px * var(--scale)); }}
/* 版本条：常驻显示"我是谁"，发现新版本时变醒目 */
.verbar {{ display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline;
           font-size: calc(12px * var(--scale)); color: #4e5866;
           margin: 0 0 calc(16px * var(--scale)); }}
.verbar .ver-new {{ color: #e8b04b; }}
.verbar .ver-new b {{ font-weight: 600; }}
.verbar .ver-note {{ color: #6a7484; }}

/* ===== S 形时间轴 =====
   节点排成网格；轴线走 "左侧轨道 + 顶部通道" 的 L 形走廊：
   同一行里从左往右连到第 N 列，然后顺着这一列往下落到下一行，
   下一行再从右往左 —— 折返回来就是 S 形。屏幕越宽列越多，
   一行能放下的天数就越多，不再是一根线拉到黑。 */
.tl {{ margin: 0; }}
.tl-body {{ position: relative; }}
svg.tl-path {{ position: absolute; left: 0; top: 0; width: 100%; height: 100%;
               pointer-events: none; overflow: visible; z-index: 0; }}
svg.tl-path path {{ fill: none; stroke: #39465a; stroke-width: 2;
                    stroke-linejoin: round; stroke-linecap: round; }}

ol.nodes {{ position: relative; z-index: 1; list-style: none; margin: 0; padding: 0;
            display: grid; grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
            column-gap: calc(22px * var(--scale)); row-gap: 0;
            align-items: start; }}
li.node {{ position: relative; }}

/* 天节点：左边留出一条走廊给轴线走，圆点骑在走廊中线上 */
li.day-node {{ padding-left: var(--rail); }}
li.day-node > .dot {{ position: absolute; left: calc(var(--rail) / 2); top: calc(var(--lead) / 2);
                      transform: translate(-50%, -50%);
                      width: calc(52px * var(--scale)); height: calc(52px * var(--scale));
                      border-radius: 50%; display: grid; place-items: center;
                      background: radial-gradient(circle at 34% 26%,
                                  rgba(255,255,255,.30), transparent 62%), var(--c);
                      color: #fff;
                      font: 600 calc(12px * var(--scale))/1 "SFMono-Regular",Consolas,monospace;
                      box-shadow: 0 0 0 calc(4px * var(--scale)) #101216,
                                  0 8px 22px -8px var(--c),
                                  inset 0 1px 0 rgba(255,255,255,.24);
                      transition: box-shadow .3s; }}
li.day-node > .dot b {{ font-size: calc(19px * var(--scale)); font-weight: 600;
                        letter-spacing: 0; line-height: 1; }}
li.day-node:hover > .dot,
li.day-node.open > .dot {{ box-shadow: 0 0 0 calc(4px * var(--scale)) #101216,
                           0 0 0 calc(9px * var(--scale)) var(--br),
                           0 10px 26px -8px var(--c),
                           inset 0 1px 0 rgba(255,255,255,.30); }}

li.day-node > .card {{ margin-top: var(--lead);
                       border: 1px solid #1e242e; border-radius: 14px;
                       background: #14171d; cursor: pointer; user-select: none;
                       padding: calc(16px * var(--scale)) calc(18px * var(--scale))
                                calc(14px * var(--scale));
                       transition: border-color .25s, background .25s, box-shadow .25s; }}
li.day-node:hover > .card {{ border-color: #2a3342; background: #161a21; }}
li.day-node.open > .card {{ border-color: var(--br); background: #15191f; }}

.eyebrow {{ font: calc(11.5px * var(--scale))/1 "SFMono-Regular",Consolas,monospace;
            color: #6b7787; letter-spacing: .05em;
            margin-bottom: calc(8px * var(--scale)); }}
li.day-node > .card > h3 {{ font-size: calc(17px * var(--scale)); font-weight: 600;
                            color: #eef2f8; margin: 0 0 calc(7px * var(--scale));
                            line-height: 1.45; letter-spacing: .01em; }}
.day-sum {{ margin: 0; font-size: calc(13.5px * var(--scale)); line-height: 1.8;
            color: #97a2b2; }}
.day-more {{ display: inline-flex; align-items: center; gap: calc(7px * var(--scale));
             margin-top: calc(11px * var(--scale));
             font-size: calc(11.5px * var(--scale)); color: #5f6b7d;
             letter-spacing: .04em; transition: color .25s; }}
li.day-node > .card:hover .day-more {{ color: #8b98aa; }}
li.day-node.open .day-more {{ color: #8fb7ff; }}
.day-more .mc {{ display: none; }}
li.day-node.open .day-more .mo {{ display: none; }}
li.day-node.open .day-more .mc {{ display: inline; }}
.caret {{ width: calc(7px * var(--scale)); height: calc(7px * var(--scale));
          border-right: 1.6px solid currentColor; border-bottom: 1.6px solid currentColor;
          transform: rotate(45deg); margin-top: calc(-3px * var(--scale));
          transition: transform .3s; flex: 0 0 auto; }}
li.day-node.open .caret {{ transform: rotate(-135deg); margin-top: calc(2px * var(--scale)); }}

/* 展开：0fr → 1fr 才真的能动，不用给 max-height 猜一个值 */
.day-body {{ display: grid; grid-template-rows: 0fr; opacity: 0;
             transition: grid-template-rows .5s cubic-bezier(.22,1,.36,1),
                         opacity .3s ease; }}
li.day-node.open .day-body {{ grid-template-rows: 1fr; opacity: 1; }}
.day-inner {{ overflow: hidden; min-height: 0; }}
.day-pad {{ margin-top: calc(13px * var(--scale));
            padding-top: calc(13px * var(--scale));
            border-top: 1px dashed #232a35; }}

.sess-mark {{ font: calc(11.5px * var(--scale))/1.6 "SFMono-Regular",Consolas,monospace;
              color: #5c6674; letter-spacing: .06em;
              margin: calc(12px * var(--scale)) 0 calc(4px * var(--scale)); }}
.lede {{ margin: 0 0 calc(12px * var(--scale)); font-size: calc(13.5px * var(--scale));
         line-height: 1.9; color: #9aa5b4; max-width: 96ch; }}
.sess-links {{ margin: calc(12px * var(--scale)) 0 0; font-size: calc(12px * var(--scale));
               color: #6b7280; }}
.sess-links a {{ color: #7f8b9d; text-decoration: none;
                 border-bottom: 1px dashed #33405a; padding-bottom: 1px; }}
.sess-links a:hover {{ color: #a8c7ff; }}

/* ===== 展开后的明细：横向铺开的卡片网格 =====
   宽屏上一行能并排放 3~5 张，不再是一条一条往下摞。 */
ol.ev-grid {{ list-style: none; margin: calc(14px * var(--scale)) 0 0; padding: 0;
              display: grid;
              grid-template-columns: repeat(auto-fill,
                                     minmax(calc(300px * var(--scale)), 1fr));
              gap: calc(12px * var(--scale)); }}
li.ev {{ position: relative; border: 1px solid #1c222c; border-radius: 11px;
         background: #12161c;
         padding: calc(12px * var(--scale)) calc(14px * var(--scale)) calc(13px * var(--scale));
         transition: border-color .22s, background .22s, transform .22s; }}
li.ev:hover {{ border-color: #2a3342; background: #161b22; transform: translateY(-2px); }}
.ev-head {{ display: flex; align-items: baseline; flex-wrap: wrap;
            gap: calc(9px * var(--scale)); margin-bottom: calc(7px * var(--scale));
            font: 600 calc(12px * var(--scale))/1.3 "SFMono-Regular",Consolas,monospace; }}
.ev-time {{ color: var(--c); letter-spacing: .04em; }}
.ev-dur {{ color: #5f6b7d; font-weight: 400; }}
li.ev > h4 {{ font-size: calc(14.5px * var(--scale)); font-weight: 600;
              color: #e6ebf2; margin: 0 0 calc(7px * var(--scale)); line-height: 1.55; }}

.row {{ display: grid; grid-template-columns: calc(58px * var(--scale)) 1fr;
        gap: 0 calc(10px * var(--scale)); margin-bottom: calc(6px * var(--scale)); }}
.row .k {{ font-size: calc(11.5px * var(--scale)); font-weight: 600; letter-spacing: .04em;
           color: #6ea8fe; padding-top: calc(3px * var(--scale)); white-space: nowrap; }}
.row.v2 .k {{ color: #6ecf9b; }}
.row.v3 .k {{ color: #d4a35e; }}
.row .v {{ margin: 0; font-size: calc(13.5px * var(--scale)); line-height: 1.85;
           color: #c3cad5; }}

.ev-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: calc(6px * var(--scale));
           margin-top: calc(9px * var(--scale)); padding-top: calc(9px * var(--scale));
           border-top: 1px dashed #1d232c; }}
.ev-row .evk {{ font-size: calc(11px * var(--scale)); color: #556070; letter-spacing: .1em; }}
.ev-row a {{ font-size: calc(11.5px * var(--scale)); color: #8fb7ff; text-decoration: none;
             border: 1px solid #263449; background: #172031;
             padding: calc(2px * var(--scale)) calc(8px * var(--scale));
             border-radius: 999px; transition: all .2s; }}
.ev-row a:hover {{ border-color: #4267a0; color: #d5e4ff; background: #1c2740;
                   transform: translateY(-1px); }}
/* "原图"是复核入口，不该跟正文胶囊抢眼 */
.ev-row a.ev-orig {{ color: #5f6b7d; border-color: #20262f; background: none;
                     font-size: calc(10.5px * var(--scale));
                     padding: calc(1px * var(--scale)) calc(6px * var(--scale)); }}
.ev-row a.ev-orig:hover {{ color: #8b98aa; border-color: #2b3442;
                           background: #171b22; }}

strong {{ color: #fff; font-weight: 600; }}
code {{ background: #1e2126; padding: 1px 5px; border-radius: 3px; font-size: .88em; }}

/* ===== 动画 ===== */
@keyframes card-in {{ from {{ opacity: 0; transform: translateY(10px); }}
                      to   {{ opacity: 1; transform: none; }} }}
@keyframes dot-in  {{ from {{ opacity: 0; transform: translate(-50%,-50%) scale(.3); }}
                      to   {{ opacity: 1; transform: translate(-50%,-50%) scale(1); }} }}
@keyframes ev-in   {{ from {{ opacity: 0; transform: translateY(8px) scale(.985); }}
                      to   {{ opacity: 1; transform: none; }} }}

li.day-node > .card {{ animation: card-in .45s cubic-bezier(.22,1,.36,1) backwards;
                       animation-delay: calc(var(--i, 0) * 70ms + 40ms); }}
li.day-node > .dot {{ animation: dot-in .5s cubic-bezier(.34,1.56,.64,1) backwards;
                      animation-delay: calc(var(--i, 0) * 70ms + 20ms); }}
li.day-node.open li.ev {{ animation: ev-in .34s cubic-bezier(.22,1,.36,1) backwards;
                          animation-delay: calc(var(--i, 0) * 45ms + 30ms); }}

/* ===== 断档换列：列数随视口宽度变，轴跟着折成几段 ===== */
@media (min-width: 860px)  {{ :root {{ --cols: 2; }} }}
@media (min-width: 1400px) {{ :root {{ --cols: 3; }} }}
@media (min-width: 2100px) {{ :root {{ --cols: 4; }} }}
@media (min-width: 3000px) {{ :root {{ --cols: 5; }} }}
/* 高分辨率上字号温和放大（列变多以后不需要放太狠） */
@media (min-width: 1920px) and (max-width: 2559px) {{ :root {{ --scale: 1.15; }} }}
@media (min-width: 2560px) {{ :root {{ --scale: 1.28; }} }}

@media (max-width: 780px) {{
  body {{ padding: 24px 12px 60px; }}
  li.day-node {{ padding-left: 34px; }}
  li.day-node > .dot {{ left: 17px; }}
  ol.ev-grid {{ grid-template-columns: 1fr; }}
  .row {{ grid-template-columns: calc(52px * var(--scale)) 1fr; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .day-body, .caret, li.day-node > .dot, li.day-node > .card, li.ev {{ transition: none; }}
  li.day-node > .card, li.day-node > .dot, li.day-node.open li.ev {{ animation: none; }}
  svg.tl-path path {{ transition: none !important; stroke-dasharray: none !important;
                      stroke-dashoffset: 0 !important; }}
}}
</style></head><body><main>
<h1>总　账</h1>
{ver_banner}
{hint}
<p class="tip">一根 S 形的轴，走到屏幕边上就折回下一行，新行的第一个点落在上一行最后一个点的正下方 —— 屏幕越宽，一行放得下的天就越多。轴上一天一个点，先看这天干了什么；点一下才长出当天的明细，再点收起。明细里的录音、截图、文档都能直接点开。</p>
{body}
<script>
(function () {{
  var KEY = 'ledger-open-days';
  var opened = [];
  try {{ opened = JSON.parse(localStorage.getItem(KEY) || '[]'); }} catch (e) {{ opened = []; }}

  var host = document.querySelector('.tl-body');
  var list = host && host.querySelector('ol.nodes');
  var svg = host && host.querySelector('svg.tl-path');
  var path = svg && svg.querySelector('path');
  var items = [].slice.call(document.querySelectorAll('li.day-node'));

  var lastD = '';      // 上次画出去的 d，几何没变就什么都不碰
  var intro = null;    // 入场动画的收尾定时器

  items.forEach(function (d) {{
    if (opened.indexOf(d.dataset.key) >= 0) d.classList.add('open');
    var card = d.querySelector(':scope > .card');
    if (!card) return;
    card.addEventListener('click', function (e) {{
      if (e.target.closest('a')) return;
      d.classList.toggle('open');
      save();
      track(900);
    }});
  }});

  function save() {{
    var arr = items.filter(function (x) {{ return x.classList.contains('open'); }})
                   .map(function (x) {{ return x.dataset.key; }});
    try {{ localStorage.setItem(KEY, JSON.stringify(arr)); }} catch (e) {{}}
  }}

  /* 实际列数以浏览器算出来的为准，不去猜断点 */
  function cols() {{
    if (!list) return 1;
    var t = getComputedStyle(list).gridTemplateColumns || '';
    var n = t.split(' ').filter(function (s) {{ return s && s !== 'none'; }}).length;
    return n > 0 ? n : 1;
  }}

  /* 蛇形落位：第 0 行左→右，第 1 行右→左，第 2 行再左→右 ……
     折行的规矩是"新行第一个格子落在上一行最后一个格子的正下方"。
     不能靠 order 让浏览器自动排 —— 自动排永远从左往右填，奇数行会被
     塞到左边缘（23 日跑到 25 日正下方，轴线还得斜着拐过去）。
     必须按蛇形坐标显式写 grid-row / grid-column；落完位，轴线按 DOM
     顺序连圆点，自然就是一条只横只竖的 S。 */
  function place() {{
    if (!list || !items.length) return;
    var c = cols();
    items.forEach(function (d, i) {{
      var r = Math.floor(i / c);
      var j = i % c;
      if (r % 2 === 1) j = c - 1 - j;
      d.style.gridRow = String(r + 1);
      d.style.gridColumn = String(j + 1);
    }});
  }}

  /* 撤掉虚线、回到实线。虚线只在入场时用一下，用完必须撤：
     stroke-dasharray 是按当时的路径长度量好的，而路径长度会随布局变
     （展开一天会把下面的行整个顶下去，页面长出滚动条时列宽也会收窄）。
     SVG 的虚线图案是"实线 L、空隙 L"循环 —— 路径一变长，多出来那截正好
     落进空隙里，线就断成一截一截，最后一个节点看着像没连上。
     所以稳态必须是实线，一点侥幸都不能留。 */
  function solid() {{
    if (!path) return;
    if (intro) {{ clearTimeout(intro); intro = null; }}
    path.style.transition = 'none';
    path.style.strokeDasharray = 'none';
    path.style.strokeDashoffset = '0';
    void path.getBoundingClientRect();     // 让上面的改动立刻生效，别被合成器拖一拍
    path.style.transition = '';
  }}

  function draw(first) {{
    if (!host || !list || !path) return;
    place();
    var hb = host.getBoundingClientRect();
    var pts = [];
    items.forEach(function (d) {{
      var dot = d.querySelector(':scope > .dot');
      if (!dot) return;
      var r = dot.getBoundingClientRect();
      pts.push([r.left + r.width / 2 - hb.left, r.top + r.height / 2 - hb.top]);
    }});
    var d = pts.length
      ? 'M' + pts.map(function (p) {{
          return p[0].toFixed(1) + ' ' + p[1].toFixed(1);
        }}).join(' L ')
      : '';
    if (!first && d === lastD) return;      // 几何没动，一个样式都别写
    lastD = d;
    path.setAttribute('d', d);
    if (!d) return;

    if (first) {{
      // 入场：量当前长度，用虚线偏移从满长收到 0，画出一条"长出来"的线
      var len = 0;
      try {{ len = path.getTotalLength(); }} catch (e) {{}}
      if (!len) {{ solid(); return; }}
      path.style.transition = 'none';
      path.style.strokeDasharray = len + 'px';
      path.style.strokeDashoffset = len + 'px';
      void path.getBoundingClientRect();
      requestAnimationFrame(function () {{
        path.style.transition = 'stroke-dashoffset 1.2s cubic-bezier(.22,1,.36,1)';
        path.style.strokeDashoffset = '0';
      }});
      if (intro) clearTimeout(intro);
      intro = setTimeout(solid, 1400);      // 播完就收，此后线永远是实的
      return;
    }}

    // 非入场重画：几何刚变过（展开、收起、滚动条、视口），路径长度十有八九
    // 也变了。此时虚线图案必然对不上，直接撤掉换实线。
    if (intro) {{ solid(); return; }}       // 入场还没播完几何就变了：别硬撑
    path.style.strokeDasharray = 'none';
  }}

  /* 展开/收起期间行高一直在变，轴得跟着刷。不是定时刷固定时长就完事 ——
     采用续期制：这一帧几何还在变，就把截止时间往后推；稳住了才停。
     另外配 ResizeObserver 兜底，凡是能让布局变动的事情（内容长高、
     滚动条出现、字体到位）都能追上，不再依赖"猜一个足够长的时长"。 */
  var timer = null, until = 0;
  function track(ms) {{
    until = Date.now() + ms;
    if (timer) return;
    timer = setInterval(function () {{
      var before = lastD;
      draw(false);
      if (lastD !== before) until = Date.now() + 600;
      if (Date.now() > until) {{ clearInterval(timer); timer = null; }}
    }}, 60);
  }}

  if (window.ResizeObserver) {{
    var ro = new ResizeObserver(function () {{ draw(false); }});
    ro.observe(list);
    items.forEach(function (d) {{
      var b = d.querySelector(':scope > .day-body');
      if (b) ro.observe(b);            // 展开的那天自己长高也要知道
    }});
  }} else {{
    items.forEach(function (d) {{
      var b = d.querySelector(':scope > .day-body');
      if (!b) return;
      b.addEventListener('transitionend', function (e) {{
        if (e.propertyName === 'grid-template-rows') draw(false);
      }});
    }});
  }}

  var rt = null;
  window.addEventListener('resize', function () {{
    clearTimeout(rt);
    rt = setTimeout(function () {{ draw(false); }}, 120);
  }});
  window.addEventListener('load', function () {{ draw(false); }});

  requestAnimationFrame(function () {{ draw(true); }});
  track(1200);
}})();
</script>
</main></body></html>
"""

# 总账 md 的几种行，解析时靠它们区分
_DAY_RE = re.compile(r"^##\s*(\d{4})年(\d{2})月(\d{2})日\s*周([一二三四五六日])\s*(.*)$")
_DUR_RE = re.compile(r"(\d+)\s*小时\s*(\d+)\s*分")
_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[–—\-~]\s*(\d{1,2}):(\d{2})$")
_CHIP_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_ROW_CLS = {"屏幕": "", "电脑日志": "v2", "语音": "v3"}
_LINK_PREFIX = ("整场细节：", "想看细节：")


def _parse_ledger(md_text: str) -> list[dict]:
    """把总账 md 拆成"一次开机"的对象列表。日期相同的后面合成一天。"""
    out: list[dict] = []
    cur: dict | None = None
    for raw in md_text.split("\n"):
        t = raw.rstrip()
        if not t or t.startswith("# "):
            continue
        m = _DAY_RE.match(t)
        if m:
            y, mo, dd, wk, rest = m.groups()
            cur = {"date": f"{y}-{mo}-{dd}",
                   "label": f"{int(mo)} 月 {int(dd)} 日",
                   "week": "周" + wk, "rest": rest.strip(),
                   "lede": [], "nodes": [], "links": []}
            out.append(cur)
            continue
        if cur is None:
            continue
        if t == "---":
            cur = None
            continue
        if t.startswith("### "):
            head = t[4:]
            span, _, title = head.partition(" · ")
            if not title:                 # 老条目没有 "·"，整行当标题
                span, title = "", head
            cur["nodes"].append({"span": span.strip(),
                                 "title": title.strip(), "rows": []})
            continue
        if t.startswith("**") and "　" in t and cur["nodes"]:
            cur["nodes"][-1]["rows"].append(t)
            continue
        for pre in _LINK_PREFIX:
            if t.startswith(pre):
                cur["links"].append(t[len(pre):].strip())
                break
        else:
            cur["lede"].append(t)
    return out


def _span_minutes(span: str) -> float:
    m = _RANGE_RE.match(span or "")
    if not m:
        return 0.0
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return float(b - a) if b >= a else float(b + 24 * 60 - a)


def _shrink(t: str, limit: int = 50) -> str:
    """压到 limit 字以内，尽量在标点处收尾，不硬切词。"""
    t = (t or "").strip().strip("。！？；，")
    if len(t) <= limit:
        return t
    head = t[:limit]
    for sep in ("；", "，", "、", "：", " "):
        i = head.rfind(sep)
        if i >= limit * 0.6:
            return head[:i].rstrip("，、；： ") + "…"
    return head[:limit - 1] + "…"


def _day_summary(sessions: list[dict]) -> str:
    """卡片上那行"这天干了啥"：当天最新一场总述的第一句，压到 50 字以内。

    以前是把最长的几个节点标题拼起来，出来的是
    "查看 README 并静默运行录音、排查网络请求被拒…" —— 那是流水账碎片。
    模型写的那段话，第一句本来就是这天的主线（"今天主要在推进……"），
    缩它比拼标题准得多，也不会有省略号堆在中间。
    """
    for s in sessions:                    # 列表里最新的那场在前
        for para in s.get("lede") or []:
            first = re.split(r"(?<=[。！？])", para.strip())[0].strip()
            if len(first) < 10:           # "这次没录到什么。" 不算摘要
                continue
            return _shrink(first, 50)

    for s in sessions:                    # 加时间轴之前的老条目没有段落
        for n in s.get("nodes") or []:
            if n.get("title"):
                return _shrink(n["title"], 50)
    return "这天没有可用记录"


def _chips(val: str) -> str:
    """把 `[21:26 录音](file:///…) · [22 点屏幕](…)` 变成一排小胶囊。

    截图胶囊链的是裁掉空白桌面的那一版；旁边补一个"原图"小链，裁错了
    也能回头核对。裁不出来的图（内容铺满全屏）只有原图一个链。
    """
    out: list[str] = []
    for part in re.split(r"\s*·\s*", val.strip()):
        if not part:
            continue
        m = _CHIP_RE.fullmatch(part.strip())
        if not m:
            out.append(f'<span class="evk">{_md_inline(part)}</span>')
            continue
        label, href = m.group(1), m.group(2)
        out.append(f'<a href="{href}" target="_blank">'
                   f'{_md_inline(label)}</a>')
        if href.endswith(".crop.jpg"):
            orig = href[:-len(".crop.jpg")] + ".jpg"
            if (REPORT_DIR / orig).exists():
                out.append(f'<a class="ev-orig" href="{orig}" '
                           f'target="_blank">原图</a>')
    return "".join(out)


def _render_row(line: str) -> str:
    mark, _, val = line.partition("　")
    key = mark.strip("*").strip()
    if key == "细节":
        return (f'<div class="ev-row"><span class="evk">可点开</span>'
                f'{_chips(val)}</div>')
    cls = _ROW_CLS.get(key, "")
    return (f'<div class="row {cls}"><span class="k">{key}</span>'
            f'<p class="v">{_md_inline(val)}</p></div>')


_NODE_COLORS = ("#c8792c", "#7f9d2b", "#2f9e63", "#2b8d94", "#a83b45", "#3d6fc4")


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _split_head(rest: str) -> tuple[str, str]:
    """把 "20:48 开机 · 进行中 · 2 小时 19 分" 拆成菱形里的时刻 + 下面的小字。

    菱形里放不下整行字，只放时刻；"开机/结束/多久"这些放到菱形上方的小字里。
    认不出时刻就退回"本次开机"，整行照抄。
    """
    rest = (rest or "").strip()
    parts = [p.strip() for p in rest.split("·") if p.strip()]
    if not parts:
        return "本次开机", ""
    m = re.match(r"^(\d{1,2}:\d{2})\s*(.*)$", parts[0])
    if not m:
        return "本次开机", rest
    badge = m.group(1)
    cap = " · ".join(([m.group(2)] if m.group(2) else []) + parts[1:])
    return badge, cap


def _split_span(span: str) -> tuple[str, str]:
    """'21:16–21:30' → ('21:16', '→ 21:30 · 14 分钟')。

    圆点里放开始时刻（短、能对齐），结束时刻和时长放到圆点下方的引线说明里。
    """
    span = (span or "").strip()
    m = _RANGE_RE.match(span)
    if not m:
        return span[:5], ""
    a = f"{int(m.group(1)):02d}:{m.group(2)}"
    b = f"{int(m.group(3)):02d}:{m.group(4)}"
    mins = int(_span_minutes(span))
    tail = f"→ {b}" + (f" · {mins} 分钟" if mins >= 1 else "")
    return a, tail


def _render_mini(n: dict, i: int, color: str) -> str:
    """一天里的一条事件：一张独立的卡片。

    宽屏上一次开机往往有五六条事件，以前是把它们摞成一根竖排的小轴，
    半边屏幕是空的；现在改成自动填充的卡片网格，有几列取决于卡片要看多宽，
    屏幕越宽一行放得越多。
    """
    clock, tail = _split_span(n["span"])
    out = [f'<li class="ev" style="--c:{color};--i:{i}">']
    if clock or tail:
        out.append('<div class="ev-head">')
        if clock:
            out.append(f'<span class="ev-time">{clock}</span>')
        if tail:
            out.append(f'<span class="ev-dur">{tail}</span>')
        out.append('</div>')
    out.append(f'<h4>{_md_inline(n["title"])}</h4>')
    for row in n["rows"]:
        out.append(_render_row(row))
    out.append('</li>')
    return "".join(out)


def _render_session(s: dict, color: str = "", show_mark: bool = False) -> str:
    """一次开机的展开内容：开场小字 + 那段总述 + 事件卡片网格 + 整场入口。

    不再画第二根轴 —— 这一天本来就挂在外面那根 S 形主轴上，明细是它的
    内部内容，横着铺开比再套一根轴清楚得多。
    """
    out = []
    if show_mark:
        badge, cap = _split_head(s["rest"])
        bits = s["rest"].strip() or badge
        out.append(f'<div class="sess-mark">{_md_inline(bits)}</div>')
    for para in s["lede"]:
        out.append(f'<p class="lede">{_md_inline(para)}</p>')
    if s["nodes"]:
        out.append('<ol class="ev-grid">')
        for i, n in enumerate(s["nodes"]):
            out.append(_render_mini(n, i, color))
        out.append('</ol>')
    if s["links"]:
        out.append(f'<p class="sess-links">' + " · ".join(
            _md_inline(x) for x in s["links"]) + '</p>')
    return "".join(out)


def _render_day(date_key: str, sessions: list[dict], index: int = 0) -> str:
    """一天 = S 形主轴上的一个点。默认只有它，点开才长出当天明细。

    最早的排法是"一天一张可折叠的卡，卡里再放一根事件轴"，轴套在里面，
    一眼上去分不清哪层是哪层。第二版把轴提到最外层，但那是一根竖轴 ——
    在宽屏上整页拉成一条细长的竖带，左右两侧全是黑边。这一版改成 S 形：
    节点排成网格，轴线顺着行之间的通道来回折返，列数由屏幕宽度决定。
    """
    total = 0
    for s in sessions:
        m = _DUR_RE.search(s["rest"])
        if m:
            total += int(m.group(1)) * 60 + int(m.group(2))
    H, M = divmod(total, 60)
    n_nodes = sum(len(s["nodes"]) for s in sessions)
    meta: list[str] = []
    if len(sessions) > 1:
        meta.append(f"{len(sessions)} 次开机")
    if n_nodes:
        meta.append(f"{n_nodes} 个节点")
    if H or M:
        meta.append(f"{H} 小时 {M} 分")
    # 跨天延续的那条要在卡面上就说清楚，否则"25 日 00:00 起"看着像
    # 凌晨开过一次机，而事实是昨晚 20:48 那场一直没关。
    carry = next((re.search(r"（接[^）]*）", s["rest"]) for s in sessions
                  if "（接" in s["rest"]), None)
    if carry:
        meta.insert(0, carry.group(0))

    color = _NODE_COLORS[index % len(_NODE_COLORS)]
    day_no = str(int(date_key[-2:]))
    head = sessions[0]
    # 圆点是 <li> 的直接子元素，不放在卡片里 —— 轴线走的是卡片左边的走廊
    # 和卡片上方的通道，圆点压着走廊中线，一眼看出它挂在哪一段轴上。
    out = [f'<li class="node day-node" id="day-{date_key}" data-key="{date_key}" '
           f'style="--c:{color};--br:{_rgba(color, .30)};--i:{index}">',
           f'<span class="dot"><b>{day_no}</b></span>',
           '<div class="card">',
           f'<div class="eyebrow">{" · ".join(meta)}</div>',
           f'<h3>{head["label"]}　{head["week"]}</h3>',
           f'<p class="day-sum">{_md_inline(_day_summary(sessions))}</p>',
           '<span class="day-more"><i class="caret"></i>'
           '<span class="mo">点开看当天明细</span>'
           '<span class="mc">收起明细</span></span>',
           '</div>',
           '<div class="day-body"><div class="day-inner"><div class="day-pad">']
    multi = len(sessions) > 1
    for s in sessions:
        out.append(_render_session(s, color, show_mark=multi))
    out += ['</div></div></div>', '</li>']
    return "".join(out)


def _rebased(md_text: str) -> str:
    """把 md 里写死的 `file:///E:/…` 全部改写成相对路径再渲染。

    改成相对路径之前生成的条目，链接已经固化成绝对路径了。只要总账是从
    预览窗口（http）打开的，那些链接一个都点不动 —— 用户看到的正是这个。
    渲染时统一改写，不用重跑模型，老条目也跟着一起修好。
    """
    def sub(m: re.Match) -> str:
        return _url(m.group(1).replace("/", "\\"))

    return re.sub(r"file:///([^)\s\"'·]+)", sub, md_text)


def _crop_links(md_text: str) -> str:
    """把 md 里的截图链接换成裁掉空白桌面的版本。

    老条目的链接在写 md 那会儿就固化了，改 `_url` 只对新条目生效。
    在渲染前统一换一遍，历史的截图也一起受益 —— 裁切产物按需生成，
    已经裁过且原图没变的直接复用（`_crop_shot` 里判 mtime）。
    裁不出来的（内容铺满全屏的图）保持原样。
    """
    def sub(m: re.Match) -> str:
        rel = m.group(2)
        if "/shot/" not in rel or not rel.lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp")):
            return m.group(0)
        p = REPORT_DIR / rel
        if not p.is_file():
            return m.group(0)
        crop = _crop_shot(p)
        if crop is None:
            return m.group(0)
        return f"[{m.group(1)}]({crop.relative_to(REPORT_DIR).as_posix()})"

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", sub, md_text)


def _version_banner() -> str:
    """页面顶部的版本条：常驻"我是谁"，发现新版本时提示去更新。

    数据来自两份文件：安装根目录的 VERSION.json（我是谁）和
    data/update_state.json（守护进程/检查更新写下的最近一次检查结果）。
    任何一份不在都安静返回空 —— 开发机上跑时没有 VERSION.json，
    不该因此坏了页面。
    """
    vj = vcfg.ROOT / "VERSION.json"
    if not vj.exists():
        return ""
    try:
        me = json.loads(vj.read_text(encoding="utf-8"))
    except Exception:
        return ""
    ver = me.get("version", "?")
    build = (me.get("build") or "")[:10]
    parts = [f'<span>VoiceGuard {_esc(ver)}'
             + (f' · {_esc(build)} 构建' if build else '') + '</span>']
    try:
        st = json.loads((vcfg.DATA / "update_state.json")
                        .read_text(encoding="utf-8"))
    except Exception:
        st = {}
    if st.get("has_update") and st.get("remote"):
        parts.append(
            f'<span class="ver-new">↗ 有新版本 <b>{_esc(st["remote"])}</b>，'
            f'双击桌面「检查更新」安装</span>')
        for n in (st.get("notes") or [])[:2]:
            parts.append(f'<span class="ver-note">{_esc(str(n))}</span>')
    return '<div class="verbar">' + "".join(parts) + "</div>"


def _search_block(days: list[tuple[str, list[dict]]]) -> str:
    """页面顶部的自然语言搜索：索引 +  UI。

    索引内联进页面（file:// 下 fetch 本地 json 会被 CORS 拦掉），内容取
    总账覆盖的那些天：逐段录音（带 wav 链接和文字稿链接）+ 屏幕活动段
    （窗口标题/OCR，"我看过越南改革"这类就靠它命中）。

    文字稿在这一轮统一生成 —— 一天里所有有文字或有活动标签的段都写一篇，
    所以明细里那些"文字稿"链接点过去一定有东西。
    """
    import vsearch
    import vsearch_ui
    import vcollect

    # 总账覆盖的时间范围：这些天的 0 点到次日 0 点。md 里没有精确的开机
    # 时刻（只有 "08:42 开机" 这样的字），而检索要的是"这些天全部的段"，
    # 按天取整最稳妥，也不会漏掉跨天那场。
    def _day_span(key: str) -> tuple[float, float]:
        d = dt.datetime.strptime(key, "%Y-%m-%d")
        return d.timestamp(), (d + dt.timedelta(days=1)).timestamp()

    # 索引只取最近 SEARCH_DAYS 天 —— 页面要内联这份索引，而总账是追加式
    # 的，不设上限的话几个月后整页会变成几 MB。老天的"文字稿"链接不受
    # 影响：那是 _ensure_transcript 按段保证的，与索引覆盖范围无关。
    idx_days = sorted(days, key=lambda kv: kv[0], reverse=True)[:vsearch.SEARCH_DAYS]
    spans = [_day_span(k) for k, _ in idx_days]
    t0, t1 = min(a for a, _ in spans), max(b for _, b in spans)

    day_idx: list[dict] = []
    for key, ss in reversed(idx_days):
        head = ss[0]
        sub = " · ".join(
            x for x in [f"{len(ss)} 次开机" if len(ss) > 1 else "",
                        head["rest"].split("·")[0].strip()] if x)
        day_idx.append({"key": key, "label": head["label"], "week": head["week"],
                        "title": f"{head['label']} {head['week']}",
                        "sub": sub, "sum": _day_summary(ss)})

    voice_idx: list[dict] = []
    for seg in vsearch.voice_segments(t0, t1):
        wav = _url(seg["wav"]) if seg["wav"] else ""
        tr = vsearch.write_transcript(seg, wav) if (seg["text"] or seg["labels"]) else ""
        voice_idx.append({"id": seg["id"], "t": seg["t"], "day": seg["day"],
                          "hm": seg["hm"], "dur": seg["dur"], "spk": seg["spk"],
                          "text": seg["text"], "labels": seg["labels"],
                          "wav": wav, "tr": tr})

    screen_idx: list[dict] = []
    for s in vcollect.screen_activity(t0, t1)[-vsearch.MAX_SCREENS:]:
        screen_idx.append({
            "day": time.strftime("%Y-%m-%d", time.localtime(s["start"])),
            "hm": time.strftime("%H:%M", time.localtime(s["start"])),
            "hm2": time.strftime("%H:%M", time.localtime(s["end"])),
            "start": s["start"], "end": s["end"],
            "minutes": s["minutes"], "proc": s["proc"],
            "titles": s["titles"], "ocr": s["ocr"], "docs": s["docs"],
        })

    idx = vsearch.build_index(day_idx, voice_idx, screen_idx)
    return (f"<style>{vsearch_ui.SEARCH_CSS}</style>"
            + vsearch_ui.SEARCH_HTML
            + '<script type="application/json" id="idx">'
            + vsearch.index_json(idx) + "</script>"
            + "<script>" + vsearch_ui.SEARCH_JS + "</script>")


def ledger_to_html(md_text: str, refresh_meta: str = "",
                   hint: str = "") -> str:
    """把总账 md 转成能双击打开的 html。

    结构：一天一张卡（默认收起）→ 点开 → 当天几次开机 → 每次开机的
    时间轴（最近的节点在最前）→ 每个节点自带可点开的录音/截图/文档。

    md 仍然是唯一的事实来源，这里只负责把它摆成能点的样子。
    """
    md_text = _rebased(md_text)
    md_text = _crop_links(md_text)
    sessions = _parse_ledger(md_text)
    banner = _version_banner()
    if not sessions:
        return _PAGE.format(body="", refresh_meta=refresh_meta, hint=hint,
                            ver_banner=banner)
    # 同一天可能开机关机好几次，合成一张卡；新的一天在最上面
    days: list[tuple[str, list[dict]]] = []
    for s in sessions:
        if days and days[-1][0] == s["date"]:
            days[-1][1].append(s)
        else:
            days.append((s["date"], [s]))
    # 整页一根 S 形主轴：所有天都挂在这根轴上，走到屏幕边上折回下一行
    body = _search_block(days)      # 搜索框放最上面：一进页面先能搜
    body += ('<div class="tl"><div class="tl-body">'
             '<svg class="tl-path" aria-hidden="true"><path d=""></path></svg>'
             '<ol class="nodes">'
             + "\n".join(_render_day(k, v, i) for i, (k, v) in enumerate(days))
             + '</ol></div></div>')
    return _PAGE.format(body=body, refresh_meta=refresh_meta, hint=hint,
                        ver_banner=banner)

def write_ledger_html(live: bool = False, every: int = 300) -> Path | None:
    """写网页版。live=True 时让页面自己刷 —— 用户不会手动按 F5。

    刷新用 <meta refresh> 而不是 JS 定时器：file:// 下 JS 重新载入会拿
    浏览器缓存，meta refresh 是整页重新读文件，最稳。多刷一次也不花钱，
    因为内容本来就由后台每 5 分钟重写一次。
    """
    if not LEDGER.exists():
        return None
    EV_DIR.mkdir(parents=True, exist_ok=True)   # 证据链接都落在 reports/ev 下
    p = LEDGER.with_suffix(".html")
    meta = hint = ""
    if live:
        meta = f'<meta http-equiv="refresh" content="{max(60, int(every))}">'
        hint = ('<p class="hint"><b>● 正在记录</b>　最后更新 '
                f'{time.strftime("%H:%M:%S")}　每 {max(60, int(every)) // 60} '
                '分钟自动刷新，不用手动按 F5</p>')
    _atomic_write(p, ledger_to_html(LEDGER.read_text(encoding="utf-8"),
                                    refresh_meta=meta, hint=hint))
    return p


def auto_paragraph(facts: dict) -> str:
    """没人写稿时的兜底段落。

    刻意**不提任何文件名**：机器归组出来的目录名对人毫无意义，
    宁可用"主要在哪些软件里操作"这种粗粒度描述。
    """
    apps: list[str] = []
    mins = 0.0
    for s in facts["screens"]:
        mins += s["minutes"]
        p = s["proc"]
        if p and p != "?" and p not in apps:
            apps.append(p)
    n_talk = len([v for v in facts["voice"] if v.get("text")])
    n_ev = len([v for v in facts["voice"] if v.get("kind") == "event"])
    hh = int((facts["t1"] - facts["t0"]) // 3600)
    mm = int(((facts["t1"] - facts["t0"]) % 3600) // 60)
    seg = f"这次开机共 {hh} 小时 {mm} 分"
    if apps:
        seg += f"，主要在 {'、'.join(apps[:3])} 里操作，屏幕活动约 {mins/60:.1f} 小时"
    if n_talk:
        seg += f"，录到 {n_talk} 段说话"
    if n_ev:
        seg += f"，另认出 {n_ev} 段活动声（走动/水声/小孩啼哭这类）"
    return seg + "。"


# ------------------------------------------------------------------ 实时模式
#
# 为什么需要它：
#   原来的总账只在"下一次开机后 90 秒"为上一次会话生成一次。也就是说页面
#   永远滞后到"上一次关机"为止 —— 正在用的这场，一行都不会有。用户 21:26
#   在电脑前干活，打开网页却看到 20:47 就结束了，自然会问"这不是实时的吗"。
#   采集端（截屏/录音/转写）一直是实时的，滞后只发生在汇总这一层。
#
# 做法：
#   每 5 分钟把"当前这场开机"重算一次，替换总账里同一次开机的那条，并让
#   网页自己刷新。已冻结的节点走缓存，只有正在长的最后一段会重新问模型。

DIGEST_LOCK = vcfg.DATA / "digest.lock"
LIVE_PARA = vcfg.DATA / "live_para.json"
_lock = None


def grab_lock(wait: float = 0.0) -> bool:
    """同一份总账可能同时被"开机补写"和"实时刷新"改。排队，别互相覆盖。

    用 msvcrt 文件锁而不是"存在即锁"的标记文件：进程被强杀时系统会自动
    释放锁；标记文件留在盘上会把后续所有总结永久堵死。
    """
    global _lock
    import msvcrt

    end = time.time() + wait
    while True:
        try:
            f = open(DIGEST_LOCK, "a+b")
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            _lock = f
            return True
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            if time.time() >= end:
                return False
            time.sleep(2)


def _live_para_read(sess: dict, max_age: float = 1800.0) -> str | None:
    """半小时内写过的那段话直接沿用。

    那段叙述是"这次开机整体在干什么"，不是逐分钟的流水。为了它每 5 分钟
    让 12B 模型重新写一遍，纯属浪费 —— 而且每次措辞都不一样，人看着像在
    抖。超过半小时才重写一次，内容才有实质变化。
    """
    try:
        d = json.loads(LIVE_PARA.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(d.get("boot", 0)) != int(sess["boot"]):
        return None
    if time.time() - float(d.get("made_at") or 0) > max_age:
        return None
    return (d.get("para") or "").strip() or None


def _live_para_write(sess: dict, para: str) -> None:
    try:
        LIVE_PARA.write_text(json.dumps(
            {"boot": int(sess["boot"]), "made_at": time.time(), "para": para},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", action="store_true", help="上一次已结束未总结的会话")
    ap.add_argument("--boot", help="指定开机时间，如 2026-10-24T08:42")
    ap.add_argument("--live", action="store_true",
                    help="实时模式：把当前这场开机总结到此刻，并让网页自动刷新")
    ap.add_argument("--llm", dest="llm", action="store_true", default=None,
                    help="强制用本地模型写那段话（默认自动：Ollama 活着就用）")
    ap.add_argument("--no-llm", dest="llm", action="store_false",
                    help="不调模型，用程序拼出来的那段话")
    ap.add_argument("--llm-tasks", action="store_true",
                    help="额外让模型归组任务生成完整记录（慢，几分钟）")
    ap.add_argument("--brief", action="store_true",
                    help="只产出事实清单给外部模型写稿，不生成叙述")
    ap.add_argument("--paragraph-file",
                    help="用这个文件里的文字作为总账里那一段话")
    ap.add_argument("--no-ledger", action="store_true", help="不追加到总账")
    ap.add_argument("--print-facts", action="store_true", help="打印给模型的输入")
    ap.add_argument("--every", type=int, default=300,
                    help="实时模式下网页自动刷新的间隔（秒），默认 300")
    ap.add_argument("--scan-budget", type=float, default=None,
                    help="扫文件的时间上限（秒）。实时模式默认 25，正式模式 90")
    a = ap.parse_args()

    vcfg.ensure_dirs()
    # 实时刷新是后台常跑，抢不到锁就跳过这一轮，绝不排队堵住；
    # 正式的补写可以等 —— 它要是被跳过，那次开机就再也补不上。
    if not grab_lock(0 if a.live else 180):
        print("另一个总结正在写总账，这次跳过。")
        return 0

    sess = pick_session(a)
    if not sess:
        print("找不到可总结的会话。")
        return 1

    budget = (a.scan_budget if a.scan_budget is not None
              else (25.0 if a.live else 90.0))
    facts = build_facts(sess, scan_budget=budget, live=a.live)
    if not facts["artifacts"] and not facts["screens"] and not facts["voice"]:
        print("这次会话没有任何证据，跳过。")
        return 1

    brief = _brief(facts)

    if a.brief:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{dt.datetime.fromtimestamp(sess['boot']):%Y-%m-%d_%H%M}"
        bp = REPORT_DIR / f"{name}.brief.txt"
        bp.write_text(brief, encoding="utf-8")
        print(f"\n✓ 事实清单已写入 {bp}（{len(brief)} 字）\n")
        print(brief)
        return 0

    if a.print_facts:
        print("\n" + "=" * 60)
        print(brief)
        print("=" * 60 + "\n")

    # 叙述默认由本地模型写；写不成才退回程序拼的段落。
    # （旧版是"默认不写"，因为走 JSON 任务清单要 400 秒以上；
    #   现在只让它写一段 200 字散文，实测 10 秒内出。）
    res = ask_llm(brief) if a.llm_tasks else None
    real_tasks = bool(res and res.get("tasks"))
    if not real_tasks:
        res = fallback_tasks(facts)
    res = sanitize(res, facts)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{dt.datetime.fromtimestamp(sess['boot']):%Y-%m-%d_%H%M}"

    # 只有真的归纳出任务，才写"完整记录"；否则把事实清单当详情，
    # 不拿一堆目录名去冒充一份记录。
    if real_tasks:
        path = REPORT_DIR / f"{stem}.md"
        path.write_text(render(facts, res), encoding="utf-8")
    else:
        path = REPORT_DIR / f"{stem}.brief.txt"
        # 如果同一次开机已经有一份人写/精修过的 .md，详情就指向它 ——
        # 事实清单是给人核对用的，不该盖过一份真正的记录。
        md = REPORT_DIR / f"{stem}.md"
        if md.exists():
            path = md

    # ---- 时间轴：每次干活切一段，每段标题 + 屏幕/日志/语音三行 ----
    # 先切段再写段落：阶段标题是整份材料里信息密度最高的一项，段落的主线
    # 应该从"这段时间推进了哪几件事"里来，而不是从"哪个软件用时最长"里来。
    use_model = a.llm if a.llm is not None else ollama_up()
    print(f"  切时间轴（{'本地模型' if use_model else '程序兜底'}）…", flush=True)
    cache = load_node_cache(sess["boot"]) if a.live else None
    nodes = build_timeline(facts, use_model=use_model, cache=cache)
    if a.live and cache is not None:
        save_node_cache(sess["boot"], cache)
    froze = sum(1 for n in nodes if n.get("reused"))
    print(f"    {len(nodes)} 个节点"
          + (f"（{froze} 个沿用上次，只重算最后一段）" if a.live else "") + "：",
          flush=True)
    for n in nodes:
        print(f"    {_fmt_hm(n['start'])}–{_fmt_hm(n['end'])}  {n['title']}",
              flush=True)

    # ---- 总账：一段话 + 链接（这才是给人看的东西）----
    para = None
    if a.paragraph_file:
        para = Path(a.paragraph_file).read_text(encoding="utf-8").strip()
        print(f"  用外部写好的段落（{len(para)} 字）", flush=True)

    if not para and a.live:
        para = _live_para_read(sess)
        if para:
            print(f"  沿用半小时内写过的那段话（{len(para)} 字）", flush=True)

    if not para:
        if use_model:
            print("  让本地模型写那段话…", flush=True)
            para = ask_para(_brief_para(facts, [n["title"] for n in nodes]))
            if para and a.live:
                _live_para_write(sess, para)
        else:
            print("  本地模型没在跑，用程序拼的那段话", flush=True)
    if not para:
        para = auto_paragraph(facts)
        print("  （未使用模型叙述）", flush=True)

    entry = render_ledger(facts, para, path, nodes=nodes)
    if not a.no_ledger:
        replaced = append_ledger(entry)
        print(f"\n✓ {'已替换同一次开机的旧条目' if replaced else '已追加'}　{LEDGER}")
        hp = write_ledger_html(live=a.live, every=a.every)
        if hp:
            print(f"✓ 网页版 {hp}")
    # 只给"已关机"的会话打已总结标记；正在用的这次关机后还要重出一份
    if sess["shutdown"] is not None:
        vss.mark_digested(sess, path, "")
    print(f"✓ 详情 {path}\n")
    print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
