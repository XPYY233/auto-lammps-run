const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('function appendWorkbenchTargetInventory('),source.indexOf('function paperWorkflowAction('));
const ID='a'.repeat(32),REQUEST='b'.repeat(32);
const GENERATE='让应用 AI 整理完整 P 为图表目标';
function fixture(overrides={}){
 const calls=[],root=node('section');
 function node(tag,textContent='',className=''){
  return {tag,textContent,className,children:[],disabled:false,isConnected:true,
   append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}};
 }
 const record={task_id:ID,published_P_ready:true,history:[],status:{blocked:false,used_rounds:0,maximum_rounds:3,remaining_rounds:3},source:{state:'unavailable'},...overrides};
 const c={node,current:{id:ID,revision:7},crypto:{randomUUID:()=> 'cccccccc-cccc-cccc-cccc-cccccccccccc'},
  notice(){},action:fn=>fn(),afterChange:async()=>{},renderReferenceTargetAnalysis:async()=>{},
  api:async(path,data)=>{calls.push({path,data});return data&&path.endsWith('/import')?{id:ID,revision:8}:record;}};
 vm.createContext(c);vm.runInContext(code,c);c.appendWorkbenchTargetInventory(root,ID,()=>true);
 const all=()=>{function walk(item){return [item,...item.children.flatMap(walk)];}return walk(root);};
 const load=()=>all().find(x=>x.textContent==='查看 P 目标整理与已有记录').onclick();
 return {c,calls,root,all,load};
}
test('opening the P inventory panel never creates a paid request',async()=>{
 const f=fixture();assert.equal(f.calls.length,0);await f.load();
 assert.equal(f.calls.length,1);assert.equal(f.calls[0].data,undefined);
});
test('incomplete P unknown calls and the three round cap block generation',async()=>{
 for(const value of [{published_P_ready:false},{status:{blocked:true,used_rounds:1,maximum_rounds:3,remaining_rounds:2}},
   {status:{blocked:false,used_rounds:3,maximum_rounds:3,remaining_rounds:0}}]){
  const f=fixture(value);await f.load();const button=f.all().find(x=>x.textContent===GENERATE);
  assert.equal(button.disabled,true);await button.onclick();assert.equal(f.calls.filter(x=>x.data).length,0);
 }
});
test('a normal generation click sends only its request ID and never science data',async()=>{
 const f=fixture();await f.load();await f.all().find(x=>x.textContent===GENERATE).onclick();
 const sent=f.calls.find(x=>x.data);assert.equal(sent.path,`/api/tasks/${ID}/workbench/targets`);
 assert.deepEqual(Object.keys(sent.data),['request_id']);assert.equal(sent.data.request_id,'c'.repeat(32));
});
test('a complete inventory is reused and importing it is a separate explicit action',async()=>{
 const report={input_sha256:'d'.repeat(64),inventory:{coverage_note:'Complete native P',targets:[{id:'one',label:'Fig. 1',conditions:'Fixed original conditions',outputs:'Original data',availability:'new_calculation'}]},priorities:{one:1},limitations:[]};
 const f=fixture({source:{state:'completed',request_id:REQUEST,report}});await f.load();
 assert.ok(f.all().some(x=>x.textContent==='Fig. 1'));assert.equal(f.all().find(x=>x.textContent===GENERATE).disabled,true);
 assert.equal(f.calls.filter(x=>x.data).length,0);
 await f.all().find(x=>x.textContent==='使用这份 P 目标清单，继续选择范围').onclick();
 const sent=f.calls.find(x=>x.data);assert.equal(sent.path,`/api/tasks/${ID}/workbench/targets/${REQUEST}/import`);
 assert.deepEqual(Object.keys(sent.data),['revision']);assert.equal(sent.data.revision,7);
});
test('a stale P inventory is not offered for import',async()=>{
 const f=fixture({source:{state:'stale',request_id:REQUEST,report:null}});await f.load();
 assert.equal(f.all().some(x=>x.textContent==='使用这份 P 目标清单，继续选择范围'),false);
 assert.ok(f.all().some(x=>String(x.textContent).includes('原 P 已变化')));
});
test('delayed P target history cannot populate another task',async()=>{
 const f=fixture();let resolve;f.c.api=()=>new Promise(done=>resolve=done);const waiting=f.load();
 f.c.current={id:'e'.repeat(32),revision:1};resolve({task_id:ID});await waiting;
 assert.equal(f.all().some(x=>x.textContent===GENERATE),false);
});
