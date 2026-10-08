const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const functionSource=source.slice(source.indexOf('function renderAICharts('),source.indexOf('async function refreshDiscussion(){'));
function setup(){
 const el=(tag,text='',cls='')=>({tag,textContent:text||'',className:cls,children:[],attrs:{},append(...parts){this.children.push(...parts)},replaceChildren(...parts){this.children=parts},setAttribute(key,value){this.attrs[key]=value}});
 const box=el('div');
 const context=vm.createContext({$:selector=>selector==='#discussion-charts'?box:null,node:el,
  document:{createElementNS:(namespace,tag)=>el(tag)},numericalPlot:()=>el('svg'),Math,JSON,encodeURIComponent});
 vm.runInContext(functionSource,context);return {context,box};
}
function find(node,tag){return [node,...node.children.flatMap(child=>find(child,tag))].filter(item=>item.tag===tag)}
function text(node){return [node.textContent,...node.children.map(text)].join(' ')}
test('saved AI chart shows verified axes, sampling and full-data download',()=>{
 const {context,box}=setup(),chart={analysis_id:'a',file:'curve.dat',source_sha256:'f'.repeat(64),
  csv_sha256:'e'.repeat(64),x:'strain',y:'stress',x_unit:'1',y_unit:'GPa',rows:500,sampled:true,preview:[[0,0],[.5,2]]};
 context.renderAICharts([{id:'b'.repeat(32),question:'画应力应变图',state:'completed',chart}],'task');
 assert.equal(find(box,'svg').length,1);
 assert.match(text(box),/500 行完整数据/);assert.match(text(box),/GPa/);
 assert.equal(find(box,'a')[0].href,'/api/tasks/task/charts/'+ 'b'.repeat(32)+'/download');
 assert.match(find(box,'metadata')[0].textContent,/"sampled":true/);
});
test('changed source hides AI chart and its downloads',()=>{
 const {context,box}=setup();
 context.renderAICharts([{id:'c'.repeat(32),question:'画图',state:'source_unavailable',chart:null}],'task');
 assert.equal(find(box,'svg').length,0);assert.equal(find(box,'a').length,0);
 assert.match(text(box),/来源数据暂未通过核验/);
});
