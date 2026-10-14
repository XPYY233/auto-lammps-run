const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('function referenceLammpsTargetIds('),source.indexOf('async function renderReferenceTargetAnalysis('));
const ID='a'.repeat(32),REQ='b'.repeat(32),SHA='c'.repeat(64);
const answer={inventory:{targets:[{id:'raw',label:'Table 1'},{id:'derived',label:'Fig. 2'},{id:'analytic',label:'Fig. 3'},{id:'cited',label:'Cited value'}]},
 targets:[{target_id:'raw',data_class:'raw_lammps',dependency_ids:['raw']},{target_id:'derived',data_class:'postprocessing',dependency_ids:['stats']},{target_id:'analytic',data_class:'analytical',dependency_ids:['stats']},{target_id:'cited',data_class:'cited',dependency_ids:[]}],
 dependencies:[{id:'raw',label:'Shared raw calculation',data_class:'raw_lammps',requires:[]},{id:'stats',label:'Shared statistics',data_class:'postprocessing',requires:['raw']}]};
function fixture(){
 const calls=[];let saves=0;
 function node(tag,value='',className=''){return {tag,textContent:value,className,children:[],isConnected:true,disabled:false,append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;},setAttribute(k,v){this[k]=v;}};}
 const box=node('div');
 const c={node,current:{id:ID,revision:7},crypto:{randomUUID:()=> `${++saves}`.padStart(32,'d')},api:async(path,data)=>{calls.push({path,data:data?JSON.parse(JSON.stringify(data)):undefined});
  if(path.endsWith('/preview'))return {scope:{selected_ids:data.selected_ids,dependencies:answer.dependencies,blockers:[],kind:'continuation_scope'},scope_sha256:SHA};
  if(data)return {id:data.scope_id,scope:{selected_ids:data.selected_ids,dependencies:answer.dependencies,kind:'continuation_scope'},state:'recorded'};
  return {task_id:ID,history:[],latest:null};}};
 vm.createContext(c);vm.runInContext(code,c);
 const all=()=>{const walk=x=>[x,...x.children.flatMap(walk)];return walk(box);};
 const button=text=>all().find(x=>x.tag==='button'&&x.textContent===text);
 const render=()=>c.renderReferenceScopeSelection(box,ID,{request_id:REQ,context_sha256:SHA},answer,null,()=>c.current.id===ID);
 return {c,box,calls,all,button,render};
}
test('opening selection is read only and does not choose targets or grant A/B',async()=>{
 const f=fixture();await f.render();assert.equal(f.calls.length,1);assert.equal(f.calls[0].data,undefined);
 assert.ok(f.all().filter(x=>x.tag==='input').every(x=>!x.checked));
 assert.ok(f.button('查看所选范围与共享计算').disabled);assert.ok(f.button('确认保存这份范围').disabled);
});
test('LAMMPS preset includes raw and all downstream dependencies but not independent cited data',async()=>{
 const f=fixture();await f.render();f.button('选择作者 LAMMPS 数据及其派生图表').onclick();
 assert.deepEqual(f.all().filter(x=>x.tag==='input'&&x.checked).map(x=>x['aria-label']),['Table 1','Fig. 2','Fig. 3']);
 assert.equal(f.calls.filter(x=>x.data).length,0);
 await f.button('查看所选范围与共享计算').onclick();const preview=f.calls.find(x=>x.path.endsWith('/preview'));
 assert.deepEqual(preview.data.selected_ids,['raw','derived','analytic']);assert.ok(preview.data.exclusion_reason.length);
 assert.deepEqual(Object.keys(preview.data),['target_request_id','selected_ids','exclusion_reason']);
 assert.ok(!f.button('确认保存这份范围').disabled);assert.equal(f.calls.filter(x=>x.path.includes('execution')).length,0);
});
test('only reviewed scope digest can be saved and edits require another preview',async()=>{
 const f=fixture();await f.render();f.button('选择作者 LAMMPS 数据及其派生图表').onclick();await f.button('查看所选范围与共享计算').onclick();
 const input=f.all().find(x=>x.tag==='input');input.checked=false;input.onchange();assert.ok(f.button('确认保存这份范围').disabled);
 await f.button('查看所选范围与共享计算').onclick();await f.button('确认保存这份范围').onclick();
 const saved=f.calls.find(x=>x.data?.scope_id);assert.equal(saved.data.expected_scope_sha256,SHA);assert.equal(saved.data.revision,7);
 assert.equal(saved.data.target_request_id,REQ);assert.ok(!('script' in saved.data));
 assert.ok(f.all().some(x=>String(x.textContent).includes('范围已保存')));
});
test('preview returning after selection edit cannot approve the stale selection',async()=>{
 const f=fixture();await f.render();f.button('选择作者 LAMMPS 数据及其派生图表').onclick();let resolve;const api=f.c.api;
 f.c.api=(path,data)=>path.endsWith('/preview')?new Promise(r=>resolve=r):api(path,data);
 const pending=f.button('查看所选范围与共享计算').onclick();const input=f.all().find(x=>x.tag==='input');input.checked=false;input.onchange();
 resolve({scope:{selected_ids:['raw'],dependencies:[],blockers:[],kind:'continuation_scope'},scope_sha256:SHA});await pending;
 assert.ok(f.button('确认保存这份范围').disabled);
});
test('uncertain save retries only the same immutable scope request',async()=>{
 const f=fixture();await f.render();f.button('选择作者 LAMMPS 数据及其派生图表').onclick();await f.button('查看所选范围与共享计算').onclick();const api=f.c.api;let first;
 f.c.api=async(path,data)=>{if(data?.scope_id&&!first){first=JSON.parse(JSON.stringify(data));throw Error('receipt unavailable');}return api(path,data);};
 await f.button('确认保存这份范围').onclick();assert.ok(f.all().filter(x=>x.tag==='input').every(x=>x.disabled));
 await f.button('核对并保存同一范围').onclick();assert.deepEqual(f.calls.find(x=>x.data?.scope_id).data,first);
});
