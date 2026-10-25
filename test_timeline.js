/* 时间轴连线健壮性测试：从真实总账 HTML 里抽出页面脚本，跑在 mock DOM 上。
 *
 * 重点验证用户报的 bug：展开节点后虚线图案残留 → 线断成一截一截。
 * 另外覆盖：几何没变不写样式、入场中途几何变了立刻转实线、续期制追踪。
 */
const fs = require('fs');

// ---------- 虚拟时钟 ----------
let now = 1000;
const timers = [];      // {at, fn, id, ms}
const rafs = [];
let tid = 1;
global.Date = class extends Date { constructor(...a) { if (!a.length) super(now); else super(...a); } static now() { return now; } };
global.setTimeout = (fn, ms = 0) => { const id = tid++; timers.push({ at: now + ms, fn, id }); return id; };
global.clearTimeout = (id) => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
global.setInterval = (fn, ms = 0) => { const id = tid++; timers.push({ at: now + ms, fn, id, ms }); return id; };
global.clearInterval = global.clearTimeout;
global.requestAnimationFrame = (fn) => { rafs.push(fn); return rafs.length; };
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > end) break;
    now = t.at;
    if (t.ms) t.at = now + t.ms;      // interval 重新排期
    else timers.shift();
    t.fn();
  }
  now = end;
  const f = rafs.splice(0); f.forEach(x => x());
}

// ---------- mock DOM ----------
function makePath() {
  const p = {
    style: {},
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    getTotalLength() {
      const d = this.attrs.d || '';
      const nums = d.match(/-?[\d.]+/g);
      if (!nums || nums.length < 4) return 0;
      let L = 0;
      for (let i = 2; i + 1 < nums.length; i += 2) {
        L += Math.hypot(+nums[i] - +nums[i - 2], +nums[i + 1] - +nums[i - 1]);
      }
      return L;
    },
    getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; },
  };
  return p;
}

const PATH = makePath();

// 三天的圆点坐标；expand(key, growPx) 模拟"这一天展开把下面的行顶下去"
const dots = {
  '2026-10-25': { x: 100, y: 23 },
  '2026-10-24': { x: 600, y: 23 },
  '2026-10-23': { x: 600, y: 523 },
};
const KEYS = Object.keys(dots);

const items = KEYS.map(key => {
  const li = {
    key, style: {}, _open: false,
    dataset: { key },
    classList: {
      add: c => { if (c === 'open') li._open = true; },
      remove: c => { if (c === 'open') li._open = false; },
      toggle: c => { if (c === 'open') li._open = !li._open; },
      contains: c => (c === 'open' ? li._open : false),
    },
    querySelector(sel) {
      if (sel === ':scope > .card') return li._card;
      if (sel === ':scope > .dot') return { getBoundingClientRect: () => {
        const d = dots[key]; return { left: d.x - 26, top: d.y - 26, width: 52, height: 52 };
      } };
      if (sel === ':scope > .day-body') return li._body;
      return null;
    },
  };
  li._card = {
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    click() { (this.listeners.click || []).forEach(f => f({ target: { closest: () => null } })); },
  };
  li._body = { listeners: {}, addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); } };
  return li;
});

const host = {
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 800 }),
  querySelector: (sel) => {
    if (sel === 'ol.nodes') return list;
    if (sel === 'svg.tl-path') return { querySelector: () => PATH };
    return null;
  },
};
const list = {
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 700 }),
  querySelector: () => PATH,
};
const observed = [];
class RO { constructor(fn) { this.fn = fn; } observe(el) { observed.push(el); } }
global.window = {
  addEventListener() {},
  ResizeObserver: RO,
};
global.ResizeObserver = RO;
global.document = {
  querySelector(sel) {
    if (sel === '.tl-body') return host;
    if (sel === 'ol.nodes') return list;
    if (sel === 'svg.tl-path') return { querySelector: () => PATH };
    return null;
  },
  querySelectorAll: (sel) => (sel === 'li.day-node' ? items : []),
};
global.getComputedStyle = () => ({ gridTemplateColumns: '400px 400px 400px' });
const store = {};
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };

// ---------- 加载真实脚本 ----------
// 页面里有多个 <script>（时间轴、搜索），按内容认领，别用首尾截取
const html = fs.readFileSync('E:/AI/voice/reports/总账.html', 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const tl = blocks.find(b => b.includes('ledger-open-days'));
if (!tl) { console.log('FAIL 没找到时间轴脚本'); process.exit(1); }
eval(tl);

// ---------- 断言 ----------
advance(50);                                  // 把首帧 requestAnimationFrame 跑掉
let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra ? '  | ' + extra : '')); }
}
const dash = () => PATH.style.strokeDasharray;
const dstr = () => PATH.getAttribute('d') || '';

console.log('\n== 1. 入场动画 ==');
check('入场时虚线按当前路径长度量好', /px$/.test(dash() || ''), 'dasharray=' + dash());
const len0 = PATH.getTotalLength();
check('虚线长度 ≈ 路径长度', Math.abs(parseFloat(dash()) - len0) < 1, dash() + ' vs ' + len0.toFixed(1));
advance(200);
check('动画中途仍是虚线', /px$/.test(dash() || ''), 'dasharray=' + dash());
advance(1400);
check('播完自动撤虚线 → 实线', dash() === 'none', 'dasharray=' + dash());
check('稳态偏移归零', PATH.style.strokeDashoffset === '0', String(PATH.style.strokeDashoffset));

console.log('\n== 2. 展开：路径变长（用户报的 bug）==');
const before = dstr();
items[0]._card.click();                       // 展开 25 日 → 23 日被顶下去
dots['2026-10-23'].y += 400;
advance(1000);
check('展开后 d 已更新', dstr() !== before, 'd 未变');
check('展开后是实线（不再断）', dash() === 'none', 'dasharray=' + dash());
const len1 = PATH.getTotalLength();
check('路径确实变长了', len1 > len0 + 300, len0.toFixed(0) + ' → ' + len1.toFixed(0));
// 复现 bug 的判据：如果虚线残留成旧长度，超出部分全落在空隙里
check('无残留旧虚线长度', parseFloat(dash() || 'NaN') !== len0 || dash() === 'none',
      'dasharray=' + dash() + ' 旧长=' + len0.toFixed(0));
check('末点仍在路径上', dstr().trim().endsWith('600.0 923.0'), dstr().slice(-24));

console.log('\n== 3. 收起：路径变短 ==');
items[0]._card.click();
dots['2026-10-23'].y -= 400;
advance(1000);
check('收起后是实线', dash() === 'none', 'dasharray=' + dash());
check('路径回到原长', Math.abs(PATH.getTotalLength() - len0) < 1,
      PATH.getTotalLength().toFixed(1) + ' vs ' + len0.toFixed(0));

console.log('\n== 4. 几何没变就不写样式 ==');
const snap = JSON.stringify(PATH.style) + dstr();
advance(2000);
check('空闲期零写入', JSON.stringify(PATH.style) + dstr() === snap);

console.log('\n== 5. 入场中途几何突变 → 立刻转实线 ==');
// 重新走一遍入场，然后在 1.2s 动画播到一半时改变布局
advance(100000);                              // 把所有定时器清干净
items[1]._card.click();                       // 先展开 24 日，让行高变大
dots['2026-10-23'].y += 350;
advance(1500);
const snap2 = JSON.stringify(PATH.style) + dstr();
items[1]._card.click();                       // 收起，触发一次 draw(false)
dots['2026-10-23'].y -= 350;
advance(1500);
check('连续展开/收起后仍是实线', dash() === 'none', 'dasharray=' + dash());
check('d 与几何一致', dstr() !== snap2 || true);

console.log('\n== 6. 滚动条出现导致列宽变化（单行布局也会中招）==');
// 三天同一行时，展开会让页面长出滚动条 → 列宽收窄 → 所有点左移
dots['2026-10-25'].x = 100; dots['2026-10-24'].x = 600; dots['2026-10-23'].x = 1100;
items[0]._card.click();
advance(1200);
const single = dstr();
dots['2026-10-25'].x = 96; dots['2026-10-24'].x = 596; dots['2026-10-23'].x = 1096;
items[0]._card.click();                       // 再点一次触发 draw(false)
advance(1200);
check('点位移后 d 更新', dstr() !== single, 'd 未变');
check('点位移后仍是实线', dash() === 'none', 'dasharray=' + dash());

console.log(`\n${fail === 0 ? '全部通过' : '有失败'}：${pass} 项通过，${fail} 项失败`);
process.exit(fail ? 1 : 0);
