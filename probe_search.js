/* 真实数据探针：拿页面里的 SEARCH_JS 问几个真问题，看解析和理解对不对。
 * 用法：node probe_search.js（需要 reports/总账.html 已生成）
 *
 * 和 test_search.js 的区别：那个用合成索引做断言，这个用线上真实数据，
 * 输出是人看的，不是断言 —— 改了解析规则后跑一遍，肉眼确认没跑偏。
 *
 * 注意：tail 用数组拼接，避免模板字面量把正则里的 \/ 吃掉。
 */
const fs = require('fs');
const src = fs.readFileSync('E:/AI/voice/test_search.js', 'utf8');
const head = src.slice(0, src.indexOf('// ---------- 合成索引'));
const py = fs.readFileSync('E:/AI/voice/vsearch_ui.py', 'utf8');
let js = py.slice(py.indexOf('SEARCH_JS = r"""') + 'SEARCH_JS = r"""'.length);
js = js.slice(0, js.indexOf('"""\n'));

const idxJson = fs.readFileSync('E:/AI/voice/reports/总账.html', 'utf8')
  .match(/<script type="application\/json" id="idx">([\s\S]*?)<\/script>/)[1]
  .replace(/<\\\//g, '</');

const tail = [
  'const realIdx = JSON.parse(idxEl.textContent);',
  "console.log('真实索引：days ' + realIdx.days.length + ' / voice ' + realIdx.voice.length +",
  "            ' / screens ' + realIdx.screens.length);",
  'function n_of(h, s) { return h.split(s).length - 1; }',
  'function ask(q) { box.value = q; box.fire("input"); advance(300); return res.innerHTML; }',
  'function show(q) {',
  '  const h = ask(q);',
  '  console.log("");',
  '  console.log("问：" + q);',
  "  console.log('  理解：' + read.innerHTML.replace(/<[^>]+>/g, '').replace(/^理解：/, ''));",
  "  console.log('  结果卡 ' + n_of(h, 'srch-hit') + ' 张 · 文字稿链接 ' + n_of(h, 'ev/voice/t') +",
  "              ' · 录音链接 ' + n_of(h, '.wav') + ' · 高亮 ' + n_of(h, '<mark>'));",
  '  const i = h.indexOf(\'<p class="sn">\');',
  "  if (i >= 0) console.log('  首条摘要：' + h.slice(i + 14, i + 90).replace(/<[^>]+>/g, ''));",
  '}',
  "['查查10月25日我做了什么？', '25日', '昨天',",
  " '我记得我看过越南改革的事，你帮我找找当时的录音', '改革', '窗帘', '敲击',",
  " 'Ollama', '帮我找找'].forEach(show);",
].join('\n');

eval(head + 'idxEl.textContent = ' + JSON.stringify(idxJson) + ';\n' + js + '\n' + tail);
