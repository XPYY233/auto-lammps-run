const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function render(progress){
 const elements=new Map(),info=[];
 const node=(tag,text,className)=>({tag,textContent:text||'',className,children:[],hidden:false,append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items}});
 const context=vm.createContext({referenceProgress:progress,current:{id:'task'},Date,
  $:selector=>{if(!elements.has(selector))elements.set(selector,node());return elements.get(selector)},node,
  requestStates:{running:'运行中',failed:'计算失败',completed:'计算已结束，尚未科学核验'},
  number:(value)=>String(value),fileSize:value=>String(value),addInfo:(label,value)=>info.push([label,value])});
 vm.runInContext(source.slice(source.indexOf('function renderReferenceProgress(){'),source.indexOf('async function refreshWorkspace(){')),context);
 context.renderReferenceProgress();
 const flatten=n=>[n.textContent,...n.children.map(flatten)].join('\n');
 return {text:flatten(elements.get('#reference-progress')),info,hidden:elements.get('#reference-progress').hidden};
}
const request=(id,job,state,accounted=false)=>({id,job_id:job,state,accounted,actual_core_seconds:accounted?3600:null,resources:{cores:32,memory_bytes:8589934592,wall_seconds:604800},events:[{kind:'scheduler_observed',at:Date.now()/1000}],monitoring:null});
const progress=()=>({task_id:'task',entries:[{paper:{title:'Synthetic scientific paper',doi:'10.1000/example',doi_url:'https://doi.org/10.1000/example',scope:'Full declared reference scope'},evaluation:{available:true,dispatch_claims:2,max_attempts:3,requests:[request('first','12345','failed',true),request('second','12346','running')]}}]});
test('running reference shows both real jobs and failure without a completed report',()=>{
 const r=render(progress());assert.equal(r.hidden,false);assert.match(r.text,/12345/);assert.match(r.text,/12346/);assert.match(r.text,/A 第 1 次 · 计算失败/);assert.match(r.text,/A 第 2 次 · 运行中/);assert.ok(r.info.some(([key,value])=>key==='A 作业号'&&value==='12346'));assert.match(r.text,/尚未最终核算/);assert.match(r.text,/不代表科学复现通过/);
 assert.ok(r.info.every(([key])=>key.startsWith('A ')));
});
test('reference status cannot leak from a different current task',()=>{
 const p=progress();p.task_id='other';const r=render(p);assert.equal(r.hidden,true);assert.equal(r.info.length,0);assert.doesNotMatch(r.text,/12346/);
});
test('unreadable reference retains its association and does not claim unsubmitted',()=>{
 const p=progress();p.entries[0].evaluation={available:false,id:'saved-evaluation'};const r=render(p);assert.match(r.text,/已有提交保留/);assert.doesNotMatch(r.text,/尚无确认的作业号/);assert.ok(r.info.some(([key,value])=>key==='A 状态'&&value==='提交记录待核对'));
});
test('ended reference is still scientifically unevaluated',()=>{
 const p=progress();p.entries[0].evaluation.requests=[request('second','12346','completed',true)];const r=render(p);assert.match(r.text,/尚未科学核验/);assert.match(r.text,/实际用量/);assert.doesNotMatch(r.text,/验收通过/);
});
