# -*- coding: utf-8 -*-
"""总账页里的自然语言搜索 UI（HTML/CSS/JS 三块，直接内联进页面）。

单独拆一个文件是因为这一大段是纯字符串：混在 vdigest.py 里会把里面的
逻辑冲散，而它的变更频率（调解析规则、调打分）和总账渲染完全不是一件事。

索引数据由 vsearch 生成，以 <script type="application/json" id="idx">
内联；这里只负责"人话 → 结构化查询 → 打分 → 渲染"。

日期解析覆盖：
  10月25日 / 10-25 / 10/25 / 25日 / 今天 / 昨天 / 前天 / 大前天 / 周五
关键词解析：去掉日期和停用词后，整串当一个短语（权重最高），
再拆 2-gram 兜底（"越南""南改""改革"都算弱命中，长词优先高亮）。

多语言：文案、停用词表、日期解析都随 vlang.LANG 切。中文页是默认，
英文页（DAYREPLAY_LANG=en）给不读中文的贡献者看 —— 停用词表不跟着换的话，
英文查询会被中文的"我/的/了"切碎，命中全乱。
"""
import json

import vlang

SEARCH_CSS = """
/* ===== 自然语言搜索 ===== */
.srch { margin: 0 0 calc(26px * var(--scale)); position: relative; }
.srch-box { display: flex; align-items: center; gap: calc(10px * var(--scale));
            background: #14171d; border: 1px solid #1e242e; border-radius: 12px;
            padding: calc(10px * var(--scale)) calc(14px * var(--scale));
            transition: border-color .2s, box-shadow .2s; }
.srch-box:focus-within { border-color: #2f4a6d; box-shadow: 0 0 0 3px rgba(47,74,109,.18); }
.srch-box svg { flex: 0 0 auto; opacity: .5; }
.srch-box input { flex: 1; min-width: 0; background: none; border: 0; outline: 0;
                  color: #e6e8eb; font: calc(14.5px * var(--scale))/1.6
                  "Microsoft YaHei","PingFang SC",system-ui,sans-serif; }
.srch-box input::placeholder { color: #4e5866; }
.srch-cnt { flex: 0 0 auto; font: calc(11.5px * var(--scale))/1
            "SFMono-Regular",Consolas,monospace; color: #5f6b7d; }
.srch-cnt b { color: #8fb7ff; font-weight: 600; }
.srch-read { font: calc(12px * var(--scale))/1.7 "SFMono-Regular",Consolas,monospace;
             color: #6b7787; margin: calc(10px * var(--scale)) 2px 0; }
.srch-read b { color: #9aa5b4; font-weight: 500; }
.srch-read .k { color: #8fb7ff; }
.srch-res { margin-top: calc(12px * var(--scale)); }
.srch-res:empty { display: none; }
.srch-sec { margin: 0 0 calc(20px * var(--scale)); }
.srch-sec > h4 { margin: 0 0 calc(9px * var(--scale)); font-size: calc(12px * var(--scale));
                 font-weight: 600; letter-spacing: .12em; color: #5f6b7d; }
.srch-hit { border: 1px solid #1c222c; border-radius: 11px; background: #12161c;
            padding: calc(11px * var(--scale)) calc(14px * var(--scale));
            margin-bottom: calc(9px * var(--scale)); }
.srch-hit .hd { display: flex; flex-wrap: wrap; align-items: baseline;
                gap: calc(9px * var(--scale)); margin-bottom: calc(5px * var(--scale));
                font: 600 calc(12px * var(--scale))/1.4 "SFMono-Regular",Consolas,monospace; }
.srch-hit .hd .t { color: var(--c, #8fb7ff); }
.srch-hit .hd .d { color: #5f6b7d; font-weight: 400; }
.srch-hit .hd .sp { color: #6ecf9b; }
.srch-hit .hd .lb { color: #d4a35e; }
.srch-hit .sn { font-size: calc(13.5px * var(--scale))/1.85; color: #b9c3d1;
                margin: 0 0 calc(8px * var(--scale)); word-break: break-word; }
.srch-hit .acts { display: flex; flex-wrap: wrap; gap: calc(7px * var(--scale));
                  font-size: calc(11.5px * var(--scale)); }
.srch-hit .acts a, .srch-hit .acts button { font-size: calc(11.5px * var(--scale));
                  color: #8fb7ff; text-decoration: none; border: 1px solid #263449;
                  background: #172031; padding: calc(2px * var(--scale)) calc(9px * var(--scale));
                  border-radius: 999px; cursor: pointer; font-family: inherit;
                  transition: all .18s; }
.srch-hit .acts a:hover, .srch-hit .acts button:hover { border-color: #4267a0;
                  color: #d5e4ff; background: #1c2740; }
.srch-hit .acts .go { color: #6b7787; border-color: #20262f; background: none; }
.srch-hit .acts .go:hover { color: #9aa5b4; border-color: #2b3442; background: #171b22; }
.srch-hit .sub { margin-top: calc(8px * var(--scale)); padding-top: calc(8px * var(--scale));
                 border-top: 1px dashed #1d232c; }
.srch-hit .sub .lab { font-size: calc(11px * var(--scale)); color: #556070;
                      letter-spacing: .1em; margin-bottom: calc(5px * var(--scale)); }
.srch-hit .sub .mini { font-size: calc(12px * var(--scale))/1.7; color: #8b96a6;
                       display: flex; flex-wrap: wrap; gap: calc(8px * var(--scale));
                       align-items: baseline; }
.srch-hit .sub .mini a { color: #8fb7ff; text-decoration: none; }
.srch-hit .sub .mini a:hover { text-decoration: underline; }
.srch-empty { color: #6b7787; font-size: calc(13px * var(--scale)); padding: 6px 2px; }
mark { background: rgba(143,183,255,.22); color: #dbe8ff; border-radius: 3px;
       padding: 0 2px; }
@media (max-width: 780px) { .srch-hit .hd { font-size: 11px; } }
"""

SEARCH_HTML = """
<div class="srch">
  <div class="srch-box">
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="6.8" cy="6.8" r="5.2" stroke="#8b96a6" stroke-width="1.6"/>
      <path d="M10.8 10.8L14.5 14.5" stroke="#8b96a6" stroke-width="1.6"
            stroke-linecap="round"/>
    </svg>
    <input id="q" type="text" autocomplete="off" spellcheck="false"
           placeholder="__SEARCH_PH__">
    <span class="srch-cnt" id="cnt"></span>
  </div>
  <div class="srch-read" id="read"></div>
  <div class="srch-res" id="res"></div>
</div>
"""
# placeholder 是文案不是代码，占位符替进去（.format 会误伤 CSS 里的花括号）
SEARCH_HTML = SEARCH_HTML.replace("__SEARCH_PH__", vlang.t("search_ph"))

# ------------------------------------------------------------------ 多语言
#
# 停用词表和日期解析必须跟着语言换：中文的"我/的/了"和英文的"the/of"作用
# 一样，都是从查询里剔掉的噪声。不换的话，英文查询会被中文停用词切不动，
# 整个短语当成一个词，命中精度直接归零。
_STOP_ZH = (
    '我 你 您 他 她 咱 们 自己 的 了 着 地 得 是 在 有 没有 没 无 什么 怎么 怎样 如何 '
    '哪 哪些 哪个 哪里 吗 呢 吧 啊 呀 嘛 啦 哦 噢 嗯 唉 帮 帮我 帮忙 给 给我 请问 '
    '我记 我记得 记得 想 想要 要 找 找找 找一下 查找 搜索 搜 搜一下 查 查查 查一下 '
    '看看 看 看下 看一下 当时 那时候 那时 时候 时间 好像 大概 可能 应该 事 事情 东西 '
    '做过 做了 干了 弄了 搞了 看过 听过 说过 用过 开过 找过 查过 过 把 被 让 向 从 '
    '到 和 跟 与 及 等 还 还是 就 都 也 再 最 更 很 太 一 下 个 些 么 那 这个 那些 '
    '这些 里 中 上 下'
)
_STOP_EN = (
    'i me my mine we us our ours you your yours he him his she her hers it its they '
    'them their this that these those am is are was were be been being have has had '
    'do does did doing will would shall should can could may might must '
    'a an the and or but if then else so of to in on at by for with about against '
    'what which who whom whose when where why how did do does done '
    'just now then today yesterday tomorrow '
    'please could would like want need find search look show tell '
    'remember recall time while during about around'
)

# JS 里要显示的文案。整句由 Python 侧给，JS 只管取 —— 免得英文版出现
# "理解："这种半吊子。
_UI = {
    "zh": {
        "date": "日期", "kw": "关键词", "got": "理解：",
        "none": "（没解析出日期或关键词）",
        "day": "当天", "open_day": "打开这天",
        "voice": "录音与文字稿", "sec": "秒",
        "rec": "录音", "tr": "文字稿",
        "no_text": "活动声，没有转写出文字",
        "more": "条，把关键词说得更具体些",
        "hits": "条命中",
    },
    "en": {
        "date": "Date", "kw": "Keywords", "got": "Understood:",
        "none": "(no date or keyword found)",
        "day": "That day", "open_day": "Open this day",
        "voice": "Recordings & transcripts", "sec": "s",
        "rec": "Audio", "tr": "Transcript",
        "no_text": "Sound event — no speech was transcribed",
        "more": "more — be more specific with your keywords",
        "hits": "hits",
    },
}

SEARCH_JS = r"""
(function () {
  var box = document.getElementById('q'), res = document.getElementById('res'),
      read = document.getElementById('read'), cnt = document.getElementById('cnt');
  var idx = {};
  try { idx = JSON.parse(document.getElementById('idx').textContent); } catch (e) {}
  if (!box || !idx || !idx.days) return;

  var DAYS = idx.days, VOICE = idx.voice || [], SCREENS = idx.screens || [];
  var base = new Date((DAYS[0] || {}).key + 'T00:00:00');
  DAYS.forEach(function (d) { if (d.key > base.toISOString().slice(0, 10)) base = new Date(d.key + 'T00:00:00'); });
  var Y = base.getFullYear(), M = base.getMonth() + 1;

  var STOP = __STOP__;
  var L = __UI__;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function rx(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  /* 削掉首尾"整段都是停用字"的部分（这里含单字 —— "得我"整个是停用字，
     该整条丢掉；而"上瘾"里的"上"在中间，动不到）。
     多字停用词已经在 keywords() 里当分隔符用掉了，这里兜的是单字。 */
  function trimEdges(w) {
    var a = 0, b = w.length;
    while (a < b && STOP.indexOf(w[a]) >= 0) a++;
    while (b > a && STOP.indexOf(w[b - 1]) >= 0) b--;
    return w.slice(a, b);
  }

  /* 把停用词当分隔符，从一串人话里切出候选词。
     多字停用词当刀（单字当刀会把"中文"切成"文"），按最长匹配切；
     空格也当刀，所以"abc  def"能切成两段。切出来的每一段再削首尾的
     停用字。这样"我记得我看过越南改革的事，帮我找找当时的录音"能切出
     "越南改革"这样的片段，而不是把整句当一个关键词显示出来。 */
  function keywords(s) {
    var delims = STOP.filter(function (x) { return x.length >= 2; }).concat([' ']);
    delims.sort(function (a, b) { return b.length - a.length; });   // 最长匹配优先
    function delimAt(p) {
      for (var k = 0; k < delims.length; k++) {
        if (s.substr(p, delims[k].length) === delims[k]) return delims[k];
      }
      return null;
    }
    var out = [], i = 0, guard = 0;
    while (i < s.length && guard++ < 400) {
      var hit = delimAt(i);
      if (hit) { i += hit.length; continue; }
      var j = i;
      while (j < s.length && !delimAt(j)) j++;
      var run = trimEdges(s.slice(i, j));
      if (run.length >= 2) out.push(run);
      i = j > i ? j : i + 1;
    }
    return out;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dkey(y, m, d) { return y + '-' + pad(m) + '-' + pad(d); }
  function count(hay, sub) {
    if (!sub) return 0;
    var n = 0, i = 0;
    while ((i = hay.indexOf(sub, i)) >= 0) { n++; i += sub.length; }
    return n;
  }

  /* ---- 人话 → 结构化查询 ---- */
  function parse(raw) {
    var s = ' ' + String(raw || '') + ' ', date = null, m, all;

    /* 英文分支先行：Oct 25 / October 25 / yesterday / Monday */
    if (L.months) {
      var MON = L.months, reMo, mo, mm, mi, k;
      reMo = new RegExp('\\b(' + MON.join('|') + ')\\.?\\s+(\\d{1,2})\\b', 'gi');
      mo = s.match(reMo);
      if (mo) {
        mm = reMo.exec(mo[mo.length - 1]);
        mi = -1;
        for (k = 0; k < MON.length; k++) {
          if (MON[k].toLowerCase() === mm[1].toLowerCase().replace('.', '')) { mi = k; break; }
        }
        if (mi >= 0) { date = dkey(Y, mi + 1, +mm[2]); s = s.replace(reMo, ' '); }
      }
      if (!date) {
        reMo = new RegExp('\\b(\\d{1,2})\\s+(' + MON.join('|') + ')\\b', 'gi');
        mo = s.match(reMo);
        if (mo) {
          mm = reMo.exec(mo[mo.length - 1]);
          mi = -1;
          for (k = 0; k < MON.length; k++) {
            if (MON[k].toLowerCase() === mm[2].toLowerCase()) { mi = k; break; }
          }
          if (mi >= 0) { date = dkey(Y, mi + 1, +mm[1]); s = s.replace(reMo, ' '); }
        }
      }
      if (!date) {
        var REL_EN = [['the day before yesterday', 2], ['yesterday', 1],
                      ['today', 0], ['tomorrow', -1]];
        for (var ri = 0; ri < REL_EN.length; ri++) {
          if (s.toLowerCase().indexOf(REL_EN[ri][0]) >= 0) {
            var dEn = new Date(base); dEn.setDate(dEn.getDate() - REL_EN[ri][1]);
            date = dkey(dEn.getFullYear(), dEn.getMonth() + 1, dEn.getDate());
            s = s.replace(new RegExp(REL_EN[ri][0], 'gi'), ' ');
            break;
          }
        }
      }
      if (!date && L.weekdays) {
        for (var wi = 0; wi < L.weekdays.length; wi++) {
          var reWk = new RegExp('\\b' + L.weekdays[wi] + '\\b', 'i');
          if (reWk.test(s)) {
            var wIdx = L.weekdays.indexOf(
              s.match(reWk)[0].charAt(0).toUpperCase() + s.match(reWk)[0].slice(1).toLowerCase());
            for (var kk = 0; kk < DAYS.length; kk++) {
              var ddw = new Date(DAYS[kk].key + 'T00:00:00');
              if (ddw.getDay() === wIdx) { date = DAYS[kk].key; break; }
            }
            s = s.replace(reWk, ' ');
            break;
          }
        }
      }
    }

    all = s.match(/(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?/g);
    if (all) { m = /(\d{1,2})\s*月\s*(\d{1,2})/.exec(all[all.length - 1]);
               date = dkey(Y, +m[1], +m[2]); }
    s = s.replace(/(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?/g, ' ');

    if (!date) {
      all = s.match(/(\d{1,2})\s*[-\/]\s*(\d{1,2})/g);
      if (all) { m = /(\d{1,2})\s*[-\/]\s*(\d{1,2})/.exec(all[all.length - 1]);
                 date = dkey(Y, +m[1], +m[2]); }
      s = s.replace(/(\d{1,2})\s*[-\/]\s*(\d{1,2})/g, ' ');
    }
    if (!date) {
      all = s.match(/[^\d](\d{1,2})\s*[日号]/g);
      if (all) { m = /(\d{1,2})/.exec(all[all.length - 1]);
                 date = dkey(Y, M, +m[1]); }
      s = s.replace(/[^\d]\d{1,2}\s*[日号]/g, ' ');
    }

    var REL = [['大前天', 3], ['前天', 2], ['昨天', 1], ['昨日', 1], ['今天', 0], ['今日', 0]];
    for (var i = 0; i < REL.length; i++) {
      if (s.indexOf(REL[i][0]) >= 0) {
        var d = new Date(base); d.setDate(d.getDate() - REL[i][1]);
        date = dkey(d.getFullYear(), d.getMonth() + 1, d.getDate());
        s = s.replace(REL[i][0], ' ');
        break;
      }
    }
    if (!date) {
      var WK = { '日': 0, '天': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6 };
      all = s.match(/周[一二三四五六日天]|星期[一二三四五六日天]/g);
      if (all) {
        var w = WK[all[all.length - 1].slice(-1)];
        for (var k = 0; k < DAYS.length; k++) {
          var dd = new Date(DAYS[k].key + 'T00:00:00');
          if (dd.getDay() === w) { date = DAYS[k].key; break; }
        }
      }
      s = s.replace(/周[一二三四五六日天]|星期[一二三四五六日天]/g, ' ');
    }

    /* 中英混排时按字符边界切开，否则"Ollama部署"会变成一个词，
       2-gram 全是"aO""ll"这种跨语言的噪声。 */
    s = s.replace(/([a-z0-9])([\u4e00-\u9fff])/gi, '$1 $2')
         .replace(/([\u4e00-\u9fff])([a-z0-9])/gi, '$1 $2');
    s = s.replace(/[，。！？、；：""''（）()【】\[\]…—~～\s,.?!;:|]+/g, ' ');
    var words = keywords(s);
    /* 短语取最长的那段（用于展示和高权重命中），2-gram 从所有段里出 */
    var phrase = words.slice().sort(function (a, b) { return b.length - a.length; })[0] || '';
    var terms = words.filter(function (w) { return w.length >= 2; });
    var grams = {};
    words.forEach(function (w) {
      for (var i2 = 0; i2 + 2 <= w.length; i2++) grams[w.substr(i2, 2)] = 1;
    });
    return { date: date, phrase: phrase, terms: terms, grams: grams };
  }

  /* ---- 打分 ---- */
  function score(hay, P) {
    var sc = 0, i;
    if (P.phrase && P.phrase.length >= 2) sc += 150 * Math.min(count(hay, P.phrase), 3);
    for (i = 0; i < P.terms.length; i++) sc += 40 * Math.min(count(hay, P.terms[i]), 3);
    var g = 0;
    for (var k in P.grams) if (hay.indexOf(k) >= 0) g++;
    sc += Math.min(g, 8) * 6;
    return sc;
  }
  function hl(t, P) {
    var e = esc(t), list = [];
    if (P.phrase && P.phrase.length >= 2) list.push(P.phrase);
    P.terms.forEach(function (x) { list.push(x); });
    for (var k in P.grams) list.push(k);
    list = list.filter(function (x) { return x && x.length >= 2; })
               .sort(function (a, b) { return b.length - a.length; });
    if (!list.length) return e;
    var re = new RegExp(list.map(function (x) { return rx(esc(x)); }).join('|'), 'g');
    return e.replace(re, function (m) { return '<mark>' + m + '</mark>'; });
  }
  function snippet(t, P, len) {
    t = String(t || '');
    if (!t) return '';
    var pos = -1, cands = [];
    if (P.phrase && P.phrase.length >= 2) cands.push(P.phrase);
    cands = cands.concat(P.terms);
    for (var i = 0; i < cands.length; i++) {
      var p = t.indexOf(cands[i]); if (p >= 0) { pos = p; break; }
    }
    if (pos < 0) for (var k in P.grams) { var p2 = t.indexOf(k); if (p2 >= 0) { pos = p2; break; } }
    if (pos < 0) return esc(t.slice(0, len)) + (t.length > len ? '…' : '');
    var a = Math.max(0, pos - Math.floor(len / 2));
    var b = Math.min(t.length, a + len);
    return (a > 0 ? '…' : '') + hl(t.slice(a, b), P) + (b < t.length ? '…' : '');
  }

  function openDay(key) {
    var li = document.getElementById('day-' + key);
    if (!li) return;
    var card = li.querySelector(':scope > .card');
    if (card && !li.classList.contains('open')) card.click();
    li.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function dayLabel(d) { return d.label + '　' + (d.week || ''); }

  /* ---- 主流程 ---- */
  var timer = null;
  function run() {
    var q = box.value.trim();
    if (!q) { res.innerHTML = ''; read.innerHTML = ''; cnt.textContent = ''; return; }
    var P = parse(q), i, html = '', nV = 0, nS = 0;

    var bits = [];
    if (P.date) {
      var dl = null;
      for (i = 0; i < DAYS.length; i++) if (DAYS[i].key === P.date) dl = DAYS[i];
      bits.push('<b>' + L.date + '</b> ' + (dl ? dayLabel(dl) : P.date));
    }
    if (P.phrase) bits.push('<span class="k">' + L.kw + '</span> 「' + esc(P.phrase) + '」');
    read.innerHTML = bits.length ? (L.got + bits.join('　·　')) : L.none;

    /* 当天 */
    var dayHits = [];
    for (i = 0; i < DAYS.length; i++) {
      var d = DAYS[i];
      var sc = score((d.title || '') + ' ' + (d.sum || ''), P);
      if (P.date) { if (d.key === P.date) sc += 400; else sc = -1; }
      if (sc > 0) dayHits.push({ d: d, sc: sc });
    }
    dayHits.sort(function (a, b) { return b.sc - a.sc; });
    if (dayHits.length) {
      html += '<div class="srch-sec"><h4>' + L.day + '</h4>';
      for (i = 0; i < Math.min(dayHits.length, 3); i++) {
        var dd = dayHits[i].d;
        html += '<div class="srch-hit"><div class="hd"><span class="t">' +
                esc(dayLabel(dd)) + '</span><span class="d">' + esc(dd.sub || '') +
                '</span></div><p class="sn">' + snippet(dd.sum, P, 150) +
                '</p><div class="acts"><button class="go" data-day="' + dd.key +
                '">' + L.open_day + '</button></div></div>';
      }
      html += '</div>';
    }

    /* 录音与文字稿。
       给了日期就展示那天的全部录音 —— "查查 25 日我做了什么"要的是流水，
       不是只给关键词命中的那几条；命中关键词的排前面就行。
       没给日期才要求必须命中，否则整本总账都倒出来没人看。 */
    var vh = [];
    for (i = 0; i < VOICE.length; i++) {
      var v = VOICE[i];
      if (P.date && v.day !== P.date) continue;
      var hay = (v.text || '') + ' ' + (v.spk || '') + ' ' + (v.labels || []).join(' ');
      var s2 = score(hay, P);
      if (!P.date && s2 <= 0) continue;
      vh.push({ v: v, sc: s2 });
    }
    vh.sort(function (a, b) { return b.sc - a.sc || b.v.t - a.v.t; });
    if (vh.length) {
      html += '<div class="srch-sec"><h4>' + L.voice + '</h4>';
      for (i = 0; i < Math.min(vh.length, 30); i++) {
        var it = vh[i].v;
        var meta = it.hm + '　' + it.dur.toFixed(1) + ' ' + L.sec;
        if (it.spk) meta += '　·　' + esc(it.spk);
        if (it.labels && it.labels.length) meta += '　·　' + esc(it.labels.join('、'));
        var acts = '';
        if (it.wav) acts += '<a href="' + esc(it.wav) + '">▶ ' + L.rec + '</a>';
        if (it.tr) acts += '<a href="' + esc(it.tr) + '">' + L.tr + '</a>';
        acts += '<button class="go" data-day="' + it.day + '">' + L.open_day + '</button>';
        html += '<div class="srch-hit"><div class="hd"><span class="t">' + meta +
                '</span></div><p class="sn">' +
                (it.text ? snippet(it.text, P, 130)
                         : '<span style="color:#7d8899">' + L.no_text + '</span>') +
                '</p><div class="acts">' + acts + '</div></div>';
      }
      if (vh.length > 30) html += '<div class="srch-empty">' + (vh.length - 30) + ' ' + L.more + '</div>';
      html += '</div>';
      nV = vh.length;
    }

    /* 屏幕上的内容（窗口标题 / OCR） */
    var sh = [];
    for (i = 0; i < SCREENS.length; i++) {
      var s3 = SCREENS[i];
      if (P.date && s3.day !== P.date) continue;
      var hay2 = (s3.titles || []).join(' ') + ' ' + (s3.ocr || '') + ' ' +
                 (s3.docs || []).join(' ') + ' ' + (s3.proc || '');
      var sc2 = score(hay2, P);
      if (!P.date && sc2 <= 0) continue;
      sh.push({ s: s3, sc: sc2 });
    }
    sh.sort(function (a, b) { return b.sc - a.sc; });
    if (sh.length) {
      html += '<div class="srch-sec"><h4>屏幕上的内容</h4>';
      for (i = 0; i < Math.min(sh.length, 12); i++) {
        var h = sh[i].s;
        var ttl = (h.titles || [])[0] || h.proc || '';
        var body2 = (h.titles || []).join(' / ');
        var ocr = (h.ocr || '').trim();
        if (ocr) body2 += (body2 ? '　·　' : '') + ocr;
        /* 当时附近的录音 —— 用户问的就是"帮我找找当时的录音" */
        var near = [];
        for (var j = 0; j < VOICE.length; j++) {
          var vv = VOICE[j];
          if (vv.t >= h.start - 900 && vv.t <= h.end + 900 && vv.wav) near.push(vv);
          if (near.length >= 4) break;
        }
        var sub = '';
        if (near.length) {
          sub = '<div class="sub"><div class="lab">同时段的录音</div>';
          for (var q2 = 0; q2 < near.length; q2++) {
            var nv = near[q2];
            sub += '<div class="mini"><span>' + esc(nv.hm) + '</span>' +
                   (nv.tr ? '<a href="' + esc(nv.tr) + '">文字稿</a>' : '') +
                   (nv.wav ? '<a href="' + esc(nv.wav) + '">▶ 录音</a>' : '') +
                   (nv.text ? '<span>' + snippet(nv.text, P, 60) + '</span>' : '') +
                   '</div>';
          }
          sub += '</div>';
        }
        html += '<div class="srch-hit"><div class="hd"><span class="t">' +
                esc(h.hm) + '–' + esc(h.hm2) + '</span><span class="d">' +
                esc(h.proc || '') + '　' + h.minutes.toFixed(0) + ' 分钟</span></div>' +
                '<p class="sn">' + (body2 ? hl(body2, P) : '（没有标题和文字）') + '</p>' +
                '<div class="acts"><button class="go" data-day="' + h.day +
                '">打开这天</button></div>' + sub + '</div>';
      }
      if (sh.length > 12) html += '<div class="srch-empty">还有 ' + (sh.length - 12) +
                                  ' 段屏幕记录</div>';
      html += '</div>';
      nS = sh.length;
    }

    res.innerHTML = html || '<div class="srch-empty">没找到对得上的记录 —— ' +
                            '换个说法试试（比如直接说日期，或换个关键词）</div>';
    var total = nV + nS + dayHits.length;
    cnt.innerHTML = total ? ('<b>' + total + '</b> 条') : '0 条';

    Array.prototype.forEach.call(res.querySelectorAll('button[data-day]'), function (b) {
      b.addEventListener('click', function () { openDay(b.getAttribute('data-day')); });
    });
  }

  var deb = null;
  box.addEventListener('input', function () {
    clearTimeout(deb); deb = setTimeout(run, 140);
  });
  box.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { clearTimeout(deb); run(); }
    if (e.key === 'Escape') { box.value = ''; run(); box.blur(); }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== box) {
      var t = document.activeElement;
      if (!t || (t.tagName !== 'INPUT' && t.tagName !== 'TEXTAREA')) {
        e.preventDefault(); box.focus(); box.select();
      }
    }
  });
})();
"""

# 把 Python 侧准备好的停用词表和文案注入 JS。
# 停用词是数组（JS 要 .indexOf 查），文案是对象。
_STOP_LIST = (_STOP_EN if vlang.IS_EN else _STOP_ZH).split()
_UI_OBJ = dict(_UI["en" if vlang.IS_EN else "zh"])
if vlang.IS_EN:
    _UI_OBJ["months"] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    _UI_OBJ["weekdays"] = ["Sunday", "Monday", "Tuesday", "Wednesday",
                           "Thursday", "Friday", "Saturday"]
else:
    _UI_OBJ["months"] = []
    _UI_OBJ["weekdays"] = []

SEARCH_JS = (SEARCH_JS
             .replace("__STOP__", json.dumps(_STOP_LIST, ensure_ascii=False))
             .replace("__UI__", json.dumps(_UI_OBJ, ensure_ascii=False)))
