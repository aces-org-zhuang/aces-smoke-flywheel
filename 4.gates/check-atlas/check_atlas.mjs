// 资产全景页的点击验证：每一条资产是不是真的能点开、点开的是不是原文。
//
// 为什么要有这个检查：页面上"看着有 617 条端点"和"每一条都能点开看到规格原文"是两件事。
// 前者靠肉眼就能糊过去，后者必须真的点。这里用 jsdom 跑页面里的真实脚本，模拟点击，断言展开后的 DOM。
//
// 依赖：jsdom（只在验证时需要，不在交付产物的运行路径上）
//   npm install jsdom
// 用法：
//   node 4.gates/check-atlas/check_atlas.mjs [页面路径]
// 退出码：全部断言通过 0，否则 1。
import { fileURLToPath } from 'url';

// 验证 docs/ssot-atlas.html：所有资产行真的能点开，且展开内容是原文而不是占位。
// 用 jsdom 跑页面里的真实脚本，模拟点击，断言展开后的 DOM 文本。
import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';

const FILE = process.argv[2]
  || fileURLToPath(new URL('../../docs/ssot-atlas.html', import.meta.url));
const html = fs.readFileSync(FILE, 'utf8');

const problems = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => problems.push('jsdomError: ' + e.message));
vc.on('error', (...a) => problems.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(w) { w.scrollTo = () => {}; },
});
const { window } = dom;
const doc = window.document;

const out = [];
const ok = (name, cond, extra = '') => {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!cond) problems.push(name + ' ' + extra);
};
const txt = (el) => (el ? el.textContent.replace(/\s+/g, ' ') : '');
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
const row = (v, kind, x) =>
  [...doc.querySelectorAll(`#${v} tr.row[data-k="${kind}"]`)].find((r) => r.dataset.x === x);

// ---------- 0. 脚本本身没报错，版本切换已生效
ok('页面脚本执行无异常', problems.length === 0, problems.join(' | ').slice(0, 200));
ok('body 已标记 js（标签页生效）', doc.body.classList.contains('js'));
const active = doc.querySelector('.ver.active');
ok('默认停在 V3 已提交原件', active && active.id === 'V3', active ? active.id : 'none');

// ---------- 1. 内嵌数据可解析，且覆盖全量资产
const data = JSON.parse(doc.getElementById('atlas-data').textContent);
const PATHS = data.spec.paths;
const SCH = data.spec.schemas;
ok('内嵌规范可用', Object.keys(PATHS).length === 292, `paths=${Object.keys(PATHS).length}`);
ok('内嵌 schema 可用', Object.keys(SCH).length === 423, `schemas=${Object.keys(SCH).length}`);
ok('声明源码可用', Object.keys(data.sources).length === 216, `sources=${Object.keys(data.sources).length}`);

// 每一行端点都必须能落到规范里的真实 path，否则点开是空壳
let epRows = 0, epBad = 0;
for (const v of ['V1', 'V2', 'V3']) {
  for (const r of doc.querySelectorAll(`#${v} tr.row[data-k="ep"]`)) {
    epRows++;
    const p = r.dataset.x.slice(r.dataset.x.indexOf(' ') + 1);
    if (!PATHS[p]) { epBad++; if (epBad <= 3) out.push('  未落地的端点: ' + v + ' ' + r.dataset.x); }
  }
}
ok('端点行全部能落到规范 path', epBad === 0, `rows=${epRows} bad=${epBad}`);

let schRows = 0, schBad = 0;
for (const v of ['V1', 'V2', 'V3']) {
  for (const r of doc.querySelectorAll(`#${v} tr.row[data-k="sch"]`)) {
    schRows++;
    if (!SCH[r.dataset.x]) schBad++;
  }
}
ok('schema 行全部能在规范中找到', schBad === 0, `rows=${schRows} bad=${schBad}`);

// ---------- 2. 端点：点开是 OpenAPI 原文
const ep = row('V3', 'ep', 'GET /api/part/');
ok('端点行存在', !!ep);
click(ep);
let drow = ep.nextElementSibling;
ok('端点行点开出现详情', drow && drow.classList.contains('drow'));
let dt = txt(drow);
ok('端点详情含 operationId', dt.includes('part_list'));
ok('端点详情含参数表与真实参数名', dt.includes('参数') && dt.includes('IPN'));
ok('端点详情含响应段', dt.includes('响应'));
ok('端点详情含原始 JSON', dt.includes('规格原文'));
ok('端点详情默认不带折叠外的空壳', dt.length > 400, `len=${dt.length}`);
click(ep);
ok('再次点击收起详情', !(ep.nextElementSibling && ep.nextElementSibling.classList.contains('drow')));

// 一个带请求体的写操作
const ep2 = row('V3', 'ep', 'POST /api/part/');
if (ep2) {
  click(ep2);
  const t2 = txt(ep2.nextElementSibling);
  ok('POST 端点展开含请求体', t2.includes('请求体'), t2.slice(0, 80));
  click(ep2);
}

// ---------- 3. schema：点开是逐字段定义
const sch = row('V3', 'sch', 'Part');
ok('schema 行存在', !!sch);
click(sch);
const st = txt(sch.nextElementSibling);
ok('schema 详情含字段表', st.includes('字段') && st.includes('类型'));
ok('schema 详情含真实字段名', /IPN|description|component/.test(st), st.slice(0, 120));
ok('schema 详情含规格原文', st.includes('规格原文'));
click(sch);

// ---------- 4. 消费方声明：点开是前端源码行
const con = row('V3', 'con', 'user_list');
ok('声明行存在', !!con);
click(con);
const ct = txt(con.nextElementSibling);
ok('声明详情含源码文件与行号', ct.includes('ApiEndpoints.tsx'), ct.slice(0, 90));
ok('声明详情含真实源码行', ct.includes("user_list = 'user/'"), ct.slice(0, 160));
click(con);

// ---------- 5. 契约矩阵：命中项可下钻到 operationId
const mat = row('V3', 'mat', 'address_list');
ok('命中行存在', !!mat);
click(mat);
const mt = txt(mat.nextElementSibling);
ok('命中详情列出 operationId', mt.includes('company_address_list'), mt.slice(0, 120));
click(mat);

// ---------- 6. 依赖边：点开是 import 语句
const dep = row('V3', 'dep', 'order');
ok('依赖行存在', !!dep);
click(dep);
const dt2 = txt(dep.nextElementSibling);
ok('依赖详情含 import 语句与文件', /\.py/.test(dt2) && /import|from/.test(dt2), dt2.slice(0, 140));
click(dep);

// ---------- 7. 鉴权方案：点开是完整定义
const sec = row('V3', 'sec', 'tokenAuth');
ok('鉴权方案行存在', !!sec);
if (sec) {
  click(sec);
  const sect = txt(sec.nextElementSibling);
  ok('鉴权详情含方案定义', sect.includes('tokenAuth') && sect.includes('{'), sect.slice(0, 120));
  click(sec);
}

// ---------- 8. 资产间跳转：详情里的 schema 引用能开弹层
// 不预设某条路径（参数名不一定是 {pk}），而是扫一遍端点到第一个带 $ref 链接的
let jumpRow = null, jumpLink = null;
for (const r of doc.querySelectorAll('#V3 tr.row[data-k="ep"]')) {
  click(r);
  const link = r.nextElementSibling && r.nextElementSibling.querySelector('a[data-k="sch"]');
  if (link) { jumpRow = r; jumpLink = link; break; }
  click(r);
}
ok('存在带 schema 引用的端点详情', !!jumpLink, jumpRow ? jumpRow.dataset.x : '整版找不到任何 $ref 链接');
if (jumpLink) {
  const name = jumpLink.dataset.x;
  click(jumpLink);
  const wrap = doc.querySelector('.modalwrap');
  ok('点击引用后弹层打开', wrap.classList.contains('on'), name);
  ok('弹层内容是目标 schema', txt(wrap).includes(name) && txt(wrap).includes('字段'), name);
  const back = wrap.querySelector('[data-act="back"]');
  click(wrap.querySelector('[data-act="close"]'));
  ok('弹层可关闭', !wrap.classList.contains('on'));
  if (jumpRow) click(jumpRow);
}

// ---------- 9. 版本切换：V1 是全未命中版
click(doc.querySelector('button.tab[data-v="V1"]'));
ok('切到 V1 生效', doc.querySelector('.ver.active').id === 'V1');
const v1Rows = doc.querySelectorAll('#V1 tr.row[data-k="con"][data-st="命中"]').length;
ok('V1 命中数为 0（该版本确实全未命中）', v1Rows === 0, `hit_rows=${v1Rows}`);
click(doc.querySelector('button.tab[data-v="V3"]'));
ok('切回 V3 生效', doc.querySelector('.ver.active').id === 'V3');

// ---------- 10. 筛选
const q = doc.getElementById('q');
q.value = 'bom';
q.dispatchEvent(new window.Event('input', { bubbles: true }));
const cnt = doc.getElementById('cnt').textContent;
ok('筛选有计数反馈', /筛选\s*\d+\s*\/\s*\d+/.test(cnt), cnt);
const hidden = doc.querySelectorAll('#V3 tr.row.hide').length;
ok('筛选确实隐藏了不匹配行', hidden > 0, `hidden=${hidden}`);
q.value = '';
q.dispatchEvent(new window.Event('input', { bubbles: true }));

// ---------- 11. 分组展开按钮
const btn = doc.querySelector('[data-act="toggle-all"]');
click(btn);
const opened = doc.querySelectorAll('#V3 details[open]').length;
ok('展开分组按钮生效', opened > 0, `open_details=${opened}`);
click(btn);

// ---------- 汇总
console.log(out.join('\n'));
console.log('\n---- 汇总 ----');
console.log('断言 ' + out.length + ' 条，失败 ' + out.filter((l) => l.startsWith('FAIL')).length + ' 条');
if (problems.length) {
  console.log('问题：\n' + problems.slice(0, 10).join('\n'));
  process.exitCode = 1;
}
