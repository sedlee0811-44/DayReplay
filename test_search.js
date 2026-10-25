/* 自然语言搜索的 JS 侧测试：从 vsearch_ui.py 里抽出真实的 SEARCH_JS，
 * 配 mock DOM + 合成索引跑，验日期解析、关键词打分、结果渲染。
 *
 * 合成索引里故意埋了"越南""改革"两个词（屏幕标题 + 一段录音文字），
 * 覆盖用户给的原话场景。
 */
const fs = require('fs');

// ---------- 虚拟时钟（SEARCH_JS 用 setTimeout 做输入防抖） ----------
let now = 1000;
const timers = [];
let tid = 1;
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...a) { if (!a.length) super(now); else super(...a); }
  static now() { return now; }
};
global.setTimeout = (fn, ms = 0) => { const id = tid++; timers.push({ at: now + ms, fn, id }); return id; };
global.clearTimeout = (id) => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > end) break;
    now = t.at; timers.shift(); t.fn();
  }
  now = end;
}

// ---------- mock DOM ----------
function mkEl() {
  return {
    _h: {}, value: '', innerHTML: '', textContent: '',
    style: {},
    addEventListener(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); },
    fire(ev, e) { (this._h[ev] || []).forEach(f => f(e || { preventDefault() {}, key: '' })); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    focus() {}, select() {}, blur() {},
    classList: { contains: () => false, add() {}, remove() {}, toggle() {} },
    scrollIntoView() {},
    getAttribute(k) { return this['_' + k]; },
    setAttribute(k, v) { this['_' + k] = v; },
  };
}
const box = mkEl(), res = mkEl(), read = mkEl(), cnt = mkEl(), idxEl = mkEl();
const byId = { q: box, res, read, cnt, idx: idxEl };
const dayEls = {};
global.document = {
  getElementById: (id) => (id in byId ? byId[id] : (dayEls[id] = dayEls[id] || mkEl())),
  addEventListener() {},
  querySelectorAll: () => [],
  querySelector: () => null,
};

// ---------- 合成索引 ----------
const INDEX = {
  generated: 1,
  days: [
    { key: '2026-10-25', label: '10 月 25 日', week: '周日', title: '10 月 25 日 周日',
      sub: '2 次开机', sum: '今天在改总账的搜索功能，顺便查了越南改革的资料。' },
    { key: '2026-10-24', label: '10 月 24 日', week: '周六', title: '10 月 24 日 周六',
      sub: '2 次开机', sum: '昨天在部署语音链路和活动声识别。' },
    { key: '2026-10-23', label: '10 月 23 日', week: '周五', title: '10 月 23 日 周五',
      sub: '1 次开机', sum: '前天在修截图裁切。' },
  ],
  voice: [
    { id: 11, t: 1761000000, day: '2026-10-25', hm: '08:18', dur: 3.2, spk: '我',
      text: '我在看越南改革的新闻，说他们要把省份合并。', labels: [],
      wav: 'ev/audio/a.wav', tr: 'ev/voice/t11.html' },
    { id: 12, t: 1761003600, day: '2026-10-25', hm: '08:26', dur: 2.0, spk: '其他',
      text: '窗帘打开了，风很大。', labels: [], wav: 'ev/audio/b.wav', tr: '' },
    { id: 13, t: 1761007200, day: '2026-10-25', hm: '08:46', dur: 4.0, spk: '',
      text: '', labels: ['敲击声'], wav: 'ev/audio/c.wav', tr: 'ev/voice/t13.html' },
    { id: 14, t: 1760900000, day: '2026-10-24', hm: '20:13', dur: 5.0, spk: '我',
      text: '昨天在装谐波闸门，风扇声总算挡掉了。', labels: [],
      wav: 'ev/audio/d.wav', tr: 'ev/voice/t14.html' },
  ],
  screens: [
    { day: '2026-10-25', hm: '08:10', hm2: '08:30', start: 1760999400, end: 1761000600,
      minutes: 20, proc: 'msedge.exe',
      titles: ['越南革新开放四十年 - 必应', 'msedge.exe'],
      ocr: '越南改革进入深水区 省份合并方案', docs: [] },
    { day: '2026-10-24', hm: '20:00', hm2: '20:30', start: 1760900000, end: 1760901800,
      minutes: 30, proc: 'WorkBuddy.exe', titles: ['vquality.py - WorkBuddy'],
      ocr: '谐波性 0.62', docs: [] },
  ],
};
idxEl.textContent = JSON.stringify(INDEX);

// ---------- 加载真实脚本 ----------
const py = fs.readFileSync('E:/AI/voice/vsearch_ui.py', 'utf8');
const m = py.match(/SEARCH_JS = r"""([\s\S]*?)"""/);
if (!m) { console.log('FAIL 没抽到 SEARCH_JS'); process.exit(1); }
eval(m[1]);

// ---------- 断言 ----------
let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra ? '  | ' + extra : '')); }
}
function ask(q) { box.value = q; box.fire('input'); advance(300); return res.innerHTML; }
const has = (h, s) => h.indexOf(s) >= 0;

console.log('\n== 1. 绝对日期 ==');
let h = ask('查查10月25日我做了什么？');
check('识别出 10 月 25 日', /2026-10-25|10 月 25 日/.test(read.innerHTML), read.innerHTML);
check('只返回 25 日的内容', !has(h, '昨天在装谐波闸门'), '混进了 24 日');
check('返回当天的摘要', has(h, '越南改革') || has(h, '搜索功能'), h.slice(0, 200));
check('当天那段录音有文字稿链接', has(h, 'ev/voice/t11.html'));
check('当天那段录音有播放链接', has(h, 'ev/audio/a.wav'));
check('有"打开这天"按钮', has(h, '打开这天'));

h = ask('10-24 我干了什么');
check('10-24 也认', /10 月 24 日|2026-10-24/.test(read.innerHTML), read.innerHTML);
check('返回 24 日内容', has(h, '昨天在装谐波闸门'));

h = ask('25日');
check('光写"25日"也认', /10 月 25 日|2026-10-25/.test(read.innerHTML), read.innerHTML);

console.log('\n== 2. 相对日期与星期 ==');
h = ask('昨天干了什么');
check('"昨天"→ 24 日', /10 月 24 日/.test(read.innerHTML), read.innerHTML);
check('昨天里是 24 日的录音', has(h, '昨天在装谐波闸门'));
h = ask('前天');
check('"前天"→ 23 日', /10 月 23 日/.test(read.innerHTML), read.innerHTML);
h = ask('周五做了什么');
check('"周五"→ 23 日', /10 月 23 日/.test(read.innerHTML), read.innerHTML);

console.log('\n== 3. 关键词（用户原话：越南改革） ==');
h = ask('我记得我看过越南改革的事，你帮我找找当时的录音');
check('理解行给出关键词', has(read.innerHTML, '越南改革'), read.innerHTML);
check('长句切出的关键词是干净的（不把整句当关键词）',
     has(read.innerHTML, '「越南改革」'), read.innerHTML);
check('命中屏幕标题/OCR', has(h, '屏幕上的内容') && has(h, '越南'));
check('同时段的录音被带出来', has(h, '同时段的录音') && has(h, 'ev/voice/t11.html'));
check('那段录音有文字稿入口', has(h, 'ev/voice/t11.html'));
check('高亮命中词', has(h, '<mark>'));
check('不相关日期的内容不出现', !has(h, '昨天在装谐波闸门'), '混进了 24 日');

h = ask('改革');
check('单词"改革"也命中', has(h, '越南') || has(h, '<mark>改革</mark>'), h.slice(0, 160));

console.log('\n== 4. 语音文字命中 ==');
h = ask('窗帘');
check('命中录音文字', /窗帘<\/mark>?打开了/.test(h), h.slice(0, 200));
check('该段没有文字稿时只给播放链接', has(h, 'ev/audio/b.wav') && !has(h, 'ev/voice/t12.html'));

console.log('\n== 5. 活动声标签 ==');
h = ask('敲击');
check('活动声标签能搜到', has(h, '敲击声'), h.slice(0, 200));
check('活动声说明没有文字', has(h, '活动声，没有转写出文字'));

console.log('\n== 6. 兜底与清空 ==');
h = ask('xyzzy 不存在的词');
check('搜不到时给提示', has(h, '没找到对得上的记录'), h.slice(0, 120));
check('计数显示 0', has(cnt.innerHTML, '0 条'), cnt.innerHTML);
h = ask('帮我找找');
check('没有关键词时提示没解析出', has(read.innerHTML, '没解析出'), read.innerHTML);
box.value = ''; box.fire('input'); advance(300);
check('清空后结果区为空', res.innerHTML === '' && cnt.textContent === '');

console.log('\n== 7. 计数 ==');
h = ask('10月25日');
check('有结果时计数非零', /<b>[1-9]/.test(cnt.innerHTML), cnt.innerHTML);

console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} 项通过，${fail} 项失败`);
process.exit(fail ? 1 : 0);
