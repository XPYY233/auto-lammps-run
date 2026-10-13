const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('async function renderReferenceTargetAnalysis('),source.indexOf('function renderPaperEvidence('));
const ID='a'.repeat(32),SHA='b'.repeat(64),BUTTON='让应用 AI 核定全部 P 目标与共享计算依赖';
function fixture(history=[]){
 const calls=[];
 function node(tag,value='',className=''){return {tag,textContent:value,className,children:[],isConnected:true,disabled:false,append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}};}
 const panel=node('section');
 const c={node,researchContent:v=>node('div',v),current:{id:ID},renderPaperEvidence:{generation:1},crypto:{randomUUID:()=> 'cccccccc-cccc-cccc-cccc-cccccccccccc'},api:async(path,data)=>{calls.push({path,data});return {task_id:ID,history};}};
 vm.createContext(c);vm.runInContext(code,c);
 const all=()=>{function walk(item){return [item,...item.children.flatMap(v=>walk(v))];}return walk(panel);};
 return {c,panel,calls,all,render:()=>c.renderReferenceTargetAnalysis(panel,{task_id:ID},ID+':'+SHA)};
}
test('opening P reads target history without generating, selecting or submitting',async()=>{
 const f=fixture();await f.render();assert.equal(f.calls.length,1);assert.equal(f.calls[0].data,undefined);
 assert.equal(f.calls[0].path,`/api/tasks/${ID}/reference-target-analysis`);
});
test('normal click requests reference analysis using only task route and request ID',async()=>{
 const f=fixture();await f.render();await f.all().find(x=>x.textContent===BUTTON).onclick();
 const post=f.calls.find(x=>x.data);assert.equal(post.path,`/api/tasks/${ID}/reference-target-analysis`);
 assert.deepEqual(Object.keys(post.data),['request_id']);assert.equal(post.data.request_id,'c'.repeat(32));
});
test('prepared dispatching and uncertain requests block another paid analysis',async()=>{
 for(const state of ['prepared','dispatching','unknown']){const f=fixture([{request_id:'d'.repeat(32),state}]);await f.render();const run=f.all().find(x=>x.textContent===BUTTON);assert.equal(run.disabled,true);await run.onclick();assert.equal(f.calls.filter(x=>x.data).length,0);}
});
test('three suggestions preserve cap on rerender and do not reset failures',async()=>{
 const f=fixture(Array.from({length:3},()=>({state:'invalid_output',request_id:'d'.repeat(32)})));await f.render();await f.render();
 assert.ok(f.all().filter(x=>x.textContent===BUTTON).every(x=>x.disabled));assert.equal(f.calls.filter(x=>x.data).length,0);
});
test('delayed history cannot populate another task',async()=>{
 const f=fixture();let resolve;f.c.api=()=>new Promise(r=>resolve=r);const pending=f.render();f.c.current.id='e'.repeat(32);resolve({task_id:ID,history:[]});await pending;
 assert.equal(f.all().some(x=>x.textContent===BUTTON),false);
});
test('full suggested targets and shared dependencies remain visible with exact citations',async()=>{
 const report={inventory:{targets:[{id:'one',label:'Table 1'},{id:'two',label:'Fig. 3'}]},targets:[{target_id:'one',priority:1,data_class:'raw_lammps',reason:'原文计算',dependency_ids:['shared'],missing:[],citations:[{source_locator:'PDF page 2',quote:'Exact source'}]},{target_id:'two',priority:2,data_class:'postprocessing',reason:'共用原始输出',dependency_ids:['shared'],missing:['方法待核验'],citations:[]}],dependencies:[{id:'shared',label:'共用计算',description:'一次输出支持两项目标',data_class:'raw_lammps',requires:[],missing:[],citations:[]}],limitations:['保留全部目标，尚未选定范围']};
 const f=fixture([{state:'completed',request_id:'d'.repeat(32),report}]);await f.render();const texts=f.all().map(x=>x.textContent);
 assert.ok(texts.some(x=>String(x).includes('Table 1')));assert.ok(texts.some(x=>String(x).includes('Fig. 3')));assert.ok(texts.includes('Exact source'));assert.ok(texts.includes('shared · 共用计算'));
 assert.equal(f.calls.filter(x=>x.data).length,0);
});
