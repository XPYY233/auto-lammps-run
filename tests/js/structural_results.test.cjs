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
 c.api=async()=>({tables:[{file:'values.dat',columns:[{name:'step',unit:'step'},{name:'energy',unit:'eV'}],rows:[[0,1],[1,2],[2,3]],sampled:false}],structural_results:[result()]});
 c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));const box=el('div');c.renderOrdinaryResults(box);
 const select=find(box,'select')[0];assert.equal(select.children.length,2);select.value='structural:0';select.onchange();
 assert.match(flat(box),/短程有序 · ordering/);assert.equal(find(box,'rect').length,3);
});
test('unverified source never presents structural chart data',async()=>{
 const {c,el}=setup();c.normalResult={evaluations:[{requests:[{job_id:'42',reports:[{id:'c',status:'analyzed',quantity:'order',results:[],structural_results:[result()]}]}]}]};c.resultTab='plots';c.api=async()=>{throw Error('source changed')};
 c.renderOrdinaryResults(el('div'));await new Promise(resolve=>setImmediate(resolve));const box=el('div');c.renderOrdinaryResults(box);
 assert.equal(find(box,'svg').length,0);assert.match(flat(box),/来源核验未通过/);
});
