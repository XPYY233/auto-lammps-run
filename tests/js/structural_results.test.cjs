const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/results-view.js','utf8');
function result(){return {id:'ordering',file:'positions.dump',method:'warren_cowley_first_shell',source:{sha256:'a'.repeat(64),elements:{1:'Fe',2:'Ni'}},ovito_version:'3.16.1',selected_frame_count:1,source_frame_count:300,parameters:{neighbors:8,neighbor_selection:'nearest_k'},frames:[{frame:299,timestep:60000,atom_count:54,directed:[[1,1,0,27,27,0,.5,1],[1,2,216,27,27,1,.5,-1],[2,1,216,27,27,1,.5,-1],[2,2,0,27,27,0,.5,1]]}],aggregates:{symmetric:[[1,1,1,null,1,1],[1,2,-1,null,-1,-1],[2,2,1,null,1,1]],directed:[[1,1,1,null,1,1],[1,2,-1,null,-1,-1],[2,1,-1,null,-1,-1],[2,2,1,null,1,1]]},chart:{labels:['Fe–Fe','Fe–Ni','Ni–Ni'],values:[1,-1,1]},limitations:['最近邻定义不自动证明物理第一壳层。']};}
function setup(){
 const el=(tag,text,cls)=>({tag,textContent:text||'',className:cls||'',attrs:{},children:[],value:'',append(...values){this.children.push(...values)},replaceChildren(...values){this.children=values},setAttribute(k,v){this.attrs[k]=String(v)}});
 const c=vm.createContext({document:{createElementNS:(ns,tag)=>el(tag)},node:el,Option:function(text,value){return {...el('option',text),value}},analysisMethods:{summary:'区间统计'},current:{id:'task'},workspaceState:{updated:1},renderWorkspaceResults(){},normalResult:{evaluations:[]},emptyState:(a,b)=>el('p',a+b),resultReport:()=>el('article','数值报告'),api:async()=>({tables:[],structural_results:[result()]})});
 vm.runInContext(source,c);return {c,el};
}
function flat(node){return [node.textContent||'',...node.children.map(flat)].join(' ')}
function find(node,tag){return [node,...node.children.flatMap(child=>find(child,tag))].filter(item=>item.tag===tag)}
test('structural bars expose actual pair labels, signs, sampling and values accessibly',()=>{
 const {c}=setup(),value=result(),before=JSON.stringify(value),svg=c.structuralPlot(value);
 assert.equal(find(svg,'rect').length,3);assert.match(svg.attrs['aria-label'],/Fe–Ni -1/);
 assert.match(flat(svg),/Warren–Cowley α/);assert.match(flat(svg),/1 个指定帧/);
 assert.equal(JSON.stringify(value),before);
});
test('derived table preserves symbols, both directions and exact adjacency counts',()=>{
 const {c}=setup(),value=result(),detail=c.structuralDetails(value,{plot:true,data:true}),text=flat(detail);
 assert.match(text,/299（步 60000）/);assert.match(text,/α 平均值/);assert.match(text,/每帧原始邻接计数/);
 assert.match(text,/216/);assert.match(text,/Fe/);assert.match(text,/Ni/);assert.match(text,/OVITO 3.16.1/);
 assert.match(text,/不是独立重复/);assert.match(text,/右侧下载/);assert.equal(find(detail,'svg').length,1);
});
test('ordinary structural-only result offers a real chart instead of crashing on no scalar operations',async()=>{
 const {c,el}=setup(),report={id:'a',status:'analyzed',quantity:'short range order',results:[],structural_results:[result()]};
 c.normalResult={evaluations:[{requests:[{job_id:'42',reports:[report]}]}]};c.resultTab='plots';
 const box=el('div');c.renderOrdinaryResults(box);await new Promise(resolve=>setImmediate(resolve));
 const next=el('div');c.renderOrdinaryResults(next);
 assert.equal(find(next,'select').length,1);assert.equal(find(next,'svg').length,1);
 assert.match(flat(next),/短程有序/);assert.match(flat(next),/作业 42/);
});
test('ordinary mixed selector switches scalar and structural charts from one verified receipt',async()=>{
 const {c,el}=setup(),op={method:'summary',file:'values.dat',x:'step',y:'energy',window:[0,2],sample_count:3,values:{}};
 c.normalResult={evaluations:[{requests:[{job_id:'42',reports:[{id:'b',status:'analyzed',quantity:'energy and order',results:[op],structural_results:[result()]}]}]}]};c.resultTab='plots';
 c.api=async()=>({tables:[{file:'values.dat',columns:[{name:'step',unit:'step'},{name:'energy',unit:'eV'}],rows:[[0,1],[1,2],[2,3]],total_rows:3,sampled:false}],structural_results:[result()]});
 c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));const box=el('div');c.renderOrdinaryResults(box);
 const select=find(box,'select')[0];assert.equal(select.children.length,3);select.value='structural:0';select.onchange();
 assert.match(flat(box),/短程有序 · ordering/);assert.equal(find(box,'rect').length,3);
});
test('verified numeric source without a frozen plot can be drawn in the app from its actual preview',async()=>{
 const {c,el}=setup();c.normalResult={evaluations:[{requests:[{job_id:'42',reports:[{id:'plot',status:'analyzed',quantity:'energy',results:[]}]}]}]};c.resultTab='plots';
 c.api=async()=>({tables:[{file:'values.dat',columns:[{name:'step',unit:'step'},{name:'energy',unit:'eV'},{name:'phase',unit:'1'}],
  rows:[[0,1,'alpha'],[1,2,'beta'],[2,3,'gamma']],total_rows:300,sampled:true}]});
 c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));const box=el('div');c.renderOrdinaryResults(box);
 assert.equal(find(box,'svg').length,1);
 assert.match(find(box,'svg')[0].attrs['aria-label'],/真实数据预览/);
 assert.doesNotMatch(find(box,'svg')[0].attrs['aria-label'],/拟合/);
 assert.match(flat(box),/自选图表 · values.dat/);
 assert.match(flat(box),/等间隔抽样的 3 行预览/);
 assert.match(flat(box),/下载当前图表 SVG/);
 assert.equal(find(box,'a')[0].download,'auto-lammps-result-plot.svg');
 assert.match(flat(box),/下载完整绘图数据 CSV/);
 assert.match(find(box,'a')[1].href,/\/api\/tasks\/task\/results\/plot\/chart-data\?file=values.dat&x=step&y=energy/);
 assert.match(flat(find(box,'metadata')[0]),/"source_file":"values.dat"/);
 assert.match(flat(find(box,'metadata')[0]),/"sampled":true/);
 const axes=find(box,'select').slice(1);assert.equal(axes.length,2);
 assert.equal(axes[0].children.length,2,'categorical phase is not offered as a numeric axis');
 axes[1].value=axes[0].value;axes[1].onchange();
 assert.equal(find(box,'svg').length,0);assert.match(flat(box),/请选择不同的横轴和纵轴/);
});
test('background redraw keeps a custom chart and its axes within the same task only',async()=>{
 const {c,el}=setup(),op={method:'summary',file:'values.dat',x:'step',y:'energy',window:[0,2],sample_count:3,values:{}};
 c.normalResult={evaluations:[{requests:[{job_id:'42',reports:[{id:'same',status:'analyzed',quantity:'energy',results:[op]}]}]}]};c.resultTab='overview';
 c.api=async()=>({tables:[{file:'values.dat',sha256:'a'.repeat(64),columns:[{name:'step',unit:'step'},{name:'energy',unit:'eV'},{name:'force',unit:'eV/A'}],rows:[[0,1,3],[1,2,4],[2,3,5]],total_rows:3,sampled:false}]});
 c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));
 const first=el('div');c.renderOrdinaryResults(first);const plot=find(first,'select')[0];plot.value='custom:0';plot.onchange();
 const axes=find(first,'select').slice(1);axes[0].value='1';axes[0].onchange();axes[1].value='2';axes[1].onchange();
 const refreshed=el('div');c.renderOrdinaryResults(refreshed);
 assert.equal(find(refreshed,'select')[0].value,'custom:0');
 assert.deepEqual(find(refreshed,'select').slice(1).map(select=>select.value),['1','2']);
 c.current={id:'another'};c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));
 const other=el('div');c.renderOrdinaryResults(other);
 assert.equal(find(other,'select')[0].value,'numeric:0');
});
test('unverified source never presents structural chart data',async()=>{
 const {c,el}=setup();c.normalResult={evaluations:[{requests:[{job_id:'42',reports:[{id:'c',status:'analyzed',quantity:'order',results:[],structural_results:[result()]}]}]}]};c.resultTab='plots';c.api=async()=>{throw Error('source changed')};
 c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));const box=el('div');c.renderOrdinaryResults(box);
 assert.equal(find(box,'svg').length,0);assert.match(flat(box),/来源核验未通过/);
});
