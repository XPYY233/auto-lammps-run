// 资源库页签与筛选的回归测试：切换页签必须移动选中态、切换列、清空旧内容。
// 用法：node resource_rerender_test.js <app.js 路径>
const fs = require('fs');
const { JSDOM } = require('jsdom');

const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('let discoveryLibraryNote=');
const end = source.indexOf('async function showResources(){');
if (start < 0 || end < 0) { console.error('无法定位资源库段落'); process.exit(2); }
const resourceSection = source.slice(start, end);

const html = `<!doctype html><html><body>
  <div id="resource-tabs"></div><div id="resource-stats"></div><aside id="resource-facets"></aside>
  <input id="resource-search" value=""><button id="resource-refresh">刷新</button>
  <p id="resource-count"></p><div id="resource-cards"></div></body></html>`;
const dom = new JSDOM(html, { runScripts: 'outside-only' });
const { window } = dom; const { document } = window;

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}
function emptyState(title, message) {
  const box = node('div', undefined, 'empty-result');
  box.append(node('h3', title), node('p', message)); return box;
}
const rows = JSON.stringify([
  {kind:'potential',discovery:true,name:'Sn · MEAM',elements:'Sn（锡）',elementList:['Sn'],styles:['meam'],sourceType:'github',repositoryRole:'author_source',state:'候选 · 待核验',tags:['meam'],note:'来源：github',process:['单轴拉伸']},
  {kind:'potential',discovery:true,name:'Si · SW',elements:'Si（硅）',elementList:['Si'],styles:['sw'],sourceType:'nist_ipr',repositoryRole:'potential_library',state:'候选 · 待核验',tags:['sw'],note:'来源：nist_ipr',process:['能量最小化']},
  {kind:'paper',discovery:true,name:'Cantor 合金 · 能量最小化',elementList:[],styles:[],sourceType:'github',repositoryRole:'author_source',paper:'Data-driven design of Cantor-type FeNiCoCrMn alloys',doi:'10.1039/x',state:'候选 · 待核验',tags:['tier_a'],note:'来源：github',process:['能量最小化']},
  {kind:'tools',discovery:false,name:'LAMMPS',type:'工具文档',state:'官方文档',note:'官方文档'},
]);
window.eval(`
  var $ = sel => document.querySelector(sel);
  var node = ${node.toString()};
  var emptyState = ${emptyState.toString()};
  var action = fn => fn;
  var resourceRows = ${rows};
  ${resourceSection}
  window.__render = renderResourceTable;
  window.__tabs = renderResourceTabs;
  window.__setFacet = (k,v) => { resourceFacets[k]=v; };
`);

const cards = document.querySelector('#resource-cards');
const tabsBox = document.querySelector('#resource-tabs');
const problems = [];
const selected = () => [...tabsBox.querySelectorAll('button')].filter(b=>b.classList.contains('selected')).map(b=>b.dataset.resourceTab);
const headers = () => [...cards.querySelectorAll('thead th')].map(t=>t.textContent);
const bodyRows = () => cards.querySelectorAll('tbody tr').length;
const clickTab = key => tabsBox.querySelector(`[data-resource-tab="${key}"]`).dispatchEvent(new window.MouseEvent('click',{bubbles:true}));

window.__tabs(); window.__render();
if (tabsBox.querySelectorAll('button').length !== 6) problems.push('页签数量不是 6');
if (selected().join() !== 'potential') problems.push('初始选中态不是势函数库：'+selected());
if (cards.querySelectorAll('table').length !== 1) problems.push('首次渲染表格数不为 1');
if (bodyRows() !== 2) problems.push('势函数页签应有 2 行，实际 '+bodyRows());
if (!headers().includes('势函数类型')) problems.push('势函数页签缺少「势函数类型」列');

clickTab('paper');
if (selected().join() !== 'paper') problems.push('点击论文案例后选中态没移动：'+selected());
if (!headers().includes('论文与 DOI')) problems.push('论文页签未切换列：'+headers().join('/'));
if (bodyRows() !== 1) problems.push('论文页签应有 1 行，实际 '+bodyRows());
if (cards.querySelectorAll('table').length !== 1) problems.push('切换页签后表格叠加：'+cards.querySelectorAll('table').length);

clickTab('dataset');
if (selected().join() !== 'dataset') problems.push('点击数据集后选中态没移动：'+selected());
if (cards.querySelectorAll('table').length !== 0) problems.push('无数据页签残留旧表格');
if (!cards.textContent.includes('尚未接入')) problems.push('无数据页签未给出说明');

clickTab('potential');
window.__setFacet('style','SW'); window.__render();
if (bodyRows() !== 1) problems.push('筛选 SW 后应有 1 行，实际 '+bodyRows());
if (cards.querySelectorAll('table').length !== 1) problems.push('筛选后表格叠加：'+cards.querySelectorAll('table').length);

clickTab('tools');
if (bodyRows() !== 1) problems.push('工具页签应有 1 行，实际 '+bodyRows());
if (cards.querySelectorAll('table').length !== 1) problems.push('工具页签表格叠加');

if (problems.length) { console.log('FAIL:\n  - '+problems.join('\n  - ')); process.exit(1); }
console.log('PASS: 6 页签 / 选中态随点击移动 / 每页签列与行数正确 / 无数据页签有说明且无残留 / 筛选与切换均不叠加');
