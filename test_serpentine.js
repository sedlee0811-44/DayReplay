/* 从渲染好的总账 HTML 里抽出真实的 place() 逻辑，模拟各种列数/天数，
   验证蛇形换行：折行时新行第一个格子必须落在上一行最后一个格子正下方。 */
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, 'reports', '总账.html'), 'utf8');

// 抽出 place 函数源码（花括号配对）
const start = html.indexOf('function place()');
if (start < 0) { console.error('FAIL: place() not found in HTML'); process.exit(1); }
let i = html.indexOf('{', start), depth = 0, end = -1;
for (let k = i; k < html.length; k++) {
  if (html[k] === '{') depth++;
  else if (html[k] === '}') { depth--; if (depth === 0) { end = k + 1; break; } }
}
const placeSrc = html.slice(start, end);

function layout(c, n) {
  const items = Array.from({ length: n }, () => ({ style: {} }));
  const list = {};
  const cols = () => c;
  const factory = new Function('list', 'items', 'cols', placeSrc + '\nreturn place;');
  factory(list, items, cols)();
  return items.map((d, idx) => ({
    idx,
    row: parseInt(d.style.gridRow, 10),
    col: parseInt(d.style.gridColumn, 10),
  }));
}

let fail = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  ok   ' + name); }
  else { console.log('  FAIL ' + name + '  ' + detail); fail++; }
}

// 1) 用户当前场景：2 列、3 天（25,24,23）—— 23 必须落在 24 正下方
{
  const L = layout(2, 3);
  check('c=2 n=3: 25 在 (1,1)', L[0].row === 1 && L[0].col === 1, JSON.stringify(L[0]));
  check('c=2 n=3: 24 在 (1,2)', L[1].row === 1 && L[1].col === 2, JSON.stringify(L[1]));
  check('c=2 n=3: 23 在 (2,2) 即 24 正下方', L[2].row === 2 && L[2].col === 2, JSON.stringify(L[2]));
}
// 2) 用户假设场景：2 列、5 天 —— 22 在 23 左边，21 在 22 正下方
{
  const L = layout(2, 5);
  check('c=2 n=5: 23 在 (2,2)', L[2].row === 2 && L[2].col === 2, JSON.stringify(L[2]));
  check('c=2 n=5: 22 在 (2,1) 23 左边', L[3].row === 2 && L[3].col === 1, JSON.stringify(L[3]));
  check('c=2 n=5: 21 在 (3,1) 22 正下方', L[4].row === 3 && L[4].col === 1, JSON.stringify(L[4]));
}
// 3) 3 列、5 天：22 落在 23 正下方，21 在 22 左边
{
  const L = layout(3, 5);
  check('c=3 n=5: 23 在 (1,3)', L[2].row === 1 && L[2].col === 3, JSON.stringify(L[2]));
  check('c=3 n=5: 22 在 (2,3) 23 正下方', L[3].row === 2 && L[3].col === 3, JSON.stringify(L[3]));
  check('c=3 n=5: 21 在 (2,2) 22 左边', L[4].row === 2 && L[4].col === 2, JSON.stringify(L[4]));
}
// 4) 4 列、5 天：21 落在 22 正下方（最右列折返）
{
  const L = layout(4, 5);
  check('c=4 n=5: 22 在 (1,4)', L[3].row === 1 && L[3].col === 4, JSON.stringify(L[3]));
  check('c=4 n=5: 21 在 (2,4) 22 正下方', L[4].row === 2 && L[4].col === 4, JSON.stringify(L[4]));
}
// 5) 1 列：纯竖轴
{
  const L = layout(1, 3);
  check('c=1 n=3: 全部同列、行号递增',
    L.every((d, k) => d.col === 1 && d.row === k + 1), JSON.stringify(L));
}
// 6) 通用不变量：相邻两天只能同行相邻列，或同列下一行 —— 保证轴线只横只竖
for (const c of [1, 2, 3, 4, 5, 6]) {
  for (const n of [1, 2, 3, 5, 7, 12]) {
    const L = layout(c, n);
    let ok = true, bad = '';
    for (let k = 1; k < L.length; k++) {
      const dr = L[k].row - L[k - 1].row, dc = L[k].col - L[k - 1].col;
      const good = (dr === 0 && Math.abs(dc) === 1) || (dr === 1 && dc === 0);
      if (!good) { ok = false; bad = `${k - 1}->${k} dr=${dr} dc=${dc}`; break; }
    }
    check(`路径不变量 c=${c} n=${n}`, ok, bad);
  }
}
// 7) 占位不重格
for (const c of [2, 3, 5]) {
  const L = layout(c, 7);
  const keys = new Set(L.map(d => d.row + ':' + d.col));
  check(`无重格 c=${c} n=7`, keys.size === L.length, JSON.stringify(L));
}

console.log(fail ? `\n${fail} 项失败` : '\n全部通过');
process.exit(fail ? 1 : 0);
