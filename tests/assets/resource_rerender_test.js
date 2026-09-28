// 资源库重渲染回归测试：点击筛选后不得出现表格叠加或计数与内容不一致。
// 用法：node resource_rerender_test.js <app.js 路径>
const fs = require('fs');
const { JSDOM } = require('jsdom');

const appPath = process.argv[2];
const source = fs.readFileSync(appPath, 'utf8');
const start = source.indexOf('let discoveryLibraryNote=');
const end = source.indexOf('function showHelp(){');
if (start < 0 || end < 0) { console.error('无法定位资源库段落'); process.exit(2); }
const resourceSection = source.slice(start, end);

const html = `<!doctype html><html><body>
  <div id="resource-tabs"><button data-resource-tab="potential">势函数库</button><button data-resource-tab="paper">论文案例</button>
  <button data-resource-tab="structure">结构模板</button><button data-resource-tab="script">脚本片段</button>
  <button data-resource-tab="dataset">数据集</button><button data-resource-tab="tools">工具与软件</button></div>
  <div id="resource-stats"></div><aside id="resource-facets"></aside>
  <input id="resource-search" value=""><button id="resource-refresh">刷新资源目录</button><p id="resource-count"></p><div id="resource-cards"></div>
</body></html>`;

const dom = new JSDOM(html, { runScripts: 'outside-only' });
const { window } = dom;
const { document } = window;

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}
function emptyState(title, message) {
  const box = node('div', undefined, 'empty-result');
  box.append(node('h3', title), node('p', message));
  return box;
}
window.eval(`
  var $ = sel => document.querySelector(sel);
  var node = ${node.toString()};
  var emptyState = ${emptyState.toString()};
  var action = fn => fn;
  var resourceRows = [
    {kind:'potential',discovery:true,name:'Sn · MEAM',type:'势函数（发现候选）',elements:'Sn（锡）',elementList:['Sn'],styles:['meam'],sourceType:'github',repositoryRole:'author_source',version:'abc1234',paper:'论文 A',doi:'10.1/a',url:'https://example.invalid/a',state:'候选 · 待核验',tags:['meam'],note:'来源：github'},
    {kind:'potential',discovery:true,name:'Nb—Ti—Zr—Mo—V · MEAM',type:'势函数（发现候选）',elements:'Nb（铌）',elementList:['Nb','Ti','Zr','Mo','V'],styles:['meam'],sourceType:'github',repositoryRole:'author_source',version:'def5678',paper:'论文 B',doi:'10.1/b',url:'https://example.invalid/b',state:'候选 · 待核验',tags:['meam'],note:'来源：github'},
    {kind:'potential',discovery:true,name:'Si · SW',type:'势函数（发现候选）',elements:'Si（硅）',elementList:['Si'],styles:['sw'],sourceType:'nist_ipr',repositoryRole:'potential_library',version:'nist_ipr xyz',paper:'论文 C',doi:'10.1/c',url:'https://example.invalid/c',state:'候选 · 待核验',tags:['sw'],note:'来源：nist_ipr'}
  ];
  ${resourceSection}
  discoveryLibraryNote = '发现目录：3 条资源行';
  window.__render = renderResourceTable;
  window.__setFacet = (k,v) => { resourceFacets[k]=v; };
  window.__setTab = v => { resourceTab = v; };
  window.__facetValues = () => [...document.querySelectorAll('#resource-facets .facet-value')].map(b=>b.textContent);
  window.__rows = resourceRows;
`);

const cards = document.querySelector('#resource-cards');
const count = document.querySelector('#resource-count');
const problems = [];

window.__render();
const tablesAfterFirst = cards.querySelectorAll('table').length;
if (tablesAfterFirst !== 1) problems.push(`首次渲染应只有 1 个表格，实际 ${tablesAfterFirst}`);

// 模拟用户点击左侧筛选标签（这正是出问题的操作）
const facets = window.__facetValues();
if (!facets.length) problems.push('左侧筛选没有渲染出任何可选值');
window.__setFacet('style', 'MEAM');
window.__render();
const tablesAfterClick = cards.querySelectorAll('table').length;
if (tablesAfterClick !== 1) problems.push(`点击筛选后表格叠加：${tablesAfterClick} 个（应为 1）`);
const meamRows = cards.querySelectorAll('tbody tr').length;
if (meamRows !== 2) problems.push(`筛选 MEAM 后应有 2 行，实际 ${meamRows}`);
if (!/2 条/.test(count.textContent)) problems.push(`计数未随筛选更新：${count.textContent}`);

// 再点一次其它筛选，确认不会越积越多
window.__setFacet('style', 'SW');
window.__render();
const tablesFinal = cards.querySelectorAll('table').length;
if (tablesFinal !== 1) problems.push(`第二次切换筛选后表格叠加：${tablesFinal} 个（应为 1）`);
if (cards.querySelectorAll('tbody tr').length !== 1) problems.push('筛选 SW 后应只有 1 行');

// 切到无数据页签，确认显示空状态而不是残留旧表格
window.__setTab('dataset'); window.__render();
if (cards.querySelectorAll('table').length !== 0) problems.push('切到无数据页签后仍残留旧表格');
if (!cards.textContent.includes('尚未接入')) problems.push('无数据页签未给出说明');

if (problems.length) { console.log('FAIL:\n  - ' + problems.join('\n  - ')); process.exit(1); }
console.log(`PASS: 首次 1 表 / 点击筛选后 1 表 / 筛选行数正确 / 计数更新 / 空页签无残留（筛选项 ${facets.length} 个）`);
