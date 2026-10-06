const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
// Execute the production refresh coordinator, with deferred HTTP responses and a
// minimal rendered-node surface. The tests control network timing, not the state logic.
function setup(){
 const elements=new Map();
 const element=()=>({children:[],hidden:false,textContent:'',className:'',append(...xs){this.children.push(...xs)},replaceChildren(...xs){this.children=xs},setAttribute(){},getAttribute(){}});
 const requests=[];
 const context=vm.createContext({Map,Date,Promise,schema:{model_calls_enabled:true},current:{id:'one',status:'draft',updated_at:Date.now(),fields:{}},workspaceGeneration:0,workspaceState:{task:'one',phase:'ready',updated:new Date(0)},workspaceReport:{old:true},rawResult:{task:'one',files:[]},normalResult:{evaluations:[]},executionState:{old:true},resultTab:'overview',renders:[],targetRenders:[],requests,
 $:selector=>{if(!elements.has(selector))elements.set(selector,element());return elements.get(selector)},node:()=>element(),
 api:path=>new Promise((resolve,reject)=>requests.push({path,resolve,reject})),
 renderWorkspaceResults(){context.renders.push({phase:context.workspaceState.phase,report:context.workspaceReport})},renderTargetPlanning(){context.targetRenders.push(context.workspaceReport)},renderRefreshStatus(){},renderFlow(){},addInfo(){},renderRawFiles(){},currentRawFiles(){return []},renderExecutionControls(){},refreshPlanReview:async()=>{},refreshActivity:async()=>{},refreshDiscussion:async()=>{},document:{createTextNode:x=>x}});
 const code=source.slice(source.indexOf('async function refreshWorkspace(){'),source.indexOf('function renderExecutionControls(){'));
 vm.runInContext(code,context);
 return context;
}
function settle(c,offset=0){const values=[{report:null},{job:null},{files:[]},{evaluations:[]}];c.requests.slice(offset,offset+4).forEach((r,i)=>r.resolve(values[i]));}
test('slow refresh keeps the last verified results until all four reads finish',async()=>{const c=setup(),old=c.workspaceReport;const pending=c.refreshWorkspace();assert.equal(c.workspaceReport,old);assert.equal(c.workspaceState.phase,'loading');c.requests[0].resolve({report:null});await Promise.resolve();assert.equal(c.workspaceReport,old);c.requests[1].resolve({job:null});c.requests[2].resolve({files:[]});c.requests[3].resolve({evaluations:[]});await pending;assert.equal(c.workspaceState.phase,'ready');assert.equal(c.workspaceReport,null);assert.equal(c.targetRenders.length,1);});
test('failed refresh preserves evidence and marks it stale instead of empty',async()=>{const c=setup(),old=c.workspaceReport;const pending=c.refreshWorkspace();c.requests[0].reject(new Error('offline'));settle(c);await pending;assert.equal(c.workspaceReport,old);assert.equal(c.workspaceState.phase,'error');assert.equal(c.workspaceState.updated.getTime(),0);});
test('late response cannot publish into a different task',async()=>{const c=setup(),old=c.workspaceReport;const pending=c.refreshWorkspace();c.current.id='two';settle(c);await pending;assert.equal(c.workspaceReport,old);assert.equal(c.renders.length,1);assert.equal(c.targetRenders.length,0);});
test('older same-task refresh cannot overwrite newer completed evidence',async()=>{const c=setup();const first=c.refreshWorkspace(),second=c.refreshWorkspace();settle(c,4);await second;const updated=c.workspaceState.updated;settle(c,0);await first;assert.equal(c.workspaceState.updated,updated);assert.equal(c.workspaceReport,null);assert.equal(c.renders.length,4);assert.equal(c.targetRenders.length,1);});
test('initial failed load stays unknown and never claims no calculation',async()=>{const c=setup();c.workspaceState.updated=null;c.workspaceReport=null;const pending=c.refreshWorkspace();c.requests[2].reject(new Error('timeout'));settle(c);await pending;assert.equal(c.workspaceState.phase,'error');assert.equal(c.workspaceState.updated,null);});
function resources(paper){const c=vm.createContext({paper});const code=source.slice(source.indexOf('function potentialResourceRows('),source.indexOf('function renderResourceTable('));vm.runInContext(code+'; output=potentialBundleRows(paper);',c);return JSON.parse(JSON.stringify(c.output));}
const potentialPaper={title:'Synthetic paper',doi:'synthetic',potential_acquisition:{commit:'fixed',files:[{path:'library.meam',sha256:'a',source_url:'https://example.org/library'},{path:'alloy.meam',sha256:'b',source_url:'https://example.org/parameters'}],bindings:[{elements:['Ni','Cr'],pair_style:'meam',files:{library:'library.meam',parameters:'alloy.meam'},static_status:'checked'}]}};
test('paired potential files become one model without losing file provenance',()=>{const rows=resources(potentialPaper);assert.equal(rows.length,1);assert.equal(rows[0].name,'Ni–Cr · MEAM');assert.equal(rows[0].files.length,2);assert.deepEqual(rows[0].files.map(f=>f.hash),['a','b']);assert.match(rows[0].note,/不代表/);});
test('missing model parameters are shown as missing, not ready',()=>{const paper=JSON.parse(JSON.stringify(potentialPaper));paper.potential_acquisition.files.pop();const rows=resources(paper);assert.match(rows[0].state,/缺少 alloy.meam/);assert.equal(rows[0].files.length,1);});

function executionPanel(state){
 const info=[],elements=new Map();
 const element=(tag,text,className)=>({tag,textContent:text||'',className,children:[],append(...xs){this.children.push(...xs)},replaceChildren(...xs){this.children=xs}});
 const c=vm.createContext({workspaceReport:null,executionState:state,
  requestStates:{queued:'排队中',running:'计算中'},node:element,
  $:s=>{if(!elements.has(s))elements.set(s,element());return elements.get(s);},
  addInfo:(key,value)=>info.push([key,value])});
 vm.runInContext(source.slice(source.indexOf('function renderExecutionControls(){'),source.indexOf('let hpcState=')),c);
 c.renderExecutionControls();info.panel=elements.get('#execution-flow');return info;
}
test('preparation shows no HPC submission using the existing counted evaluation',()=>{
 const info=executionPanel({job:null,submissions:{count:0,maximum:2},automatic_workflow:{worker_alive:true,workflow:{state:'preparing',label:'正在准备'}}});
 assert.ok(info.some(([k,v])=>k==='HPC 作业号'&&v==='尚未提交'));
 assert.ok(info.some(([k,v])=>k==='提交次数'&&v==='0 / 2'));
});
test('scheduler receipt is shown separately from preparation and unknown dispatch',()=>{
 const state={worker_alive:true,job:{state:'waiting',label:'跟进中',dispatch_count:1,max_attempts:2,job_id:'123456',scheduler_state:'queued',events:[]}};
 let info=executionPanel(state);assert.ok(info.some(([k,v])=>k==='HPC 作业号'&&v==='123456'));
 assert.ok(info.some(([k,v])=>k==='调度状态'&&v==='排队中'));
 state.job.job_id=null;info=executionPanel(state);
 assert.ok(info.some(([k,v])=>k==='HPC 作业号'&&v==='提交结果待核对'));
});
test('finished numerical analysis completes all execution stages without scientific approval',()=>{
 const info=executionPanel({worker_alive:true,job:{state:'analyzed',label:'数值分析完成',dispatch_count:1,max_attempts:2,job_id:'123456',scheduler_state:'completed',accounted:true,events:[]}});
 assert.ok(info.panel.children[1].children.every(x=>x.className==='done'));
 assert.ok(info.some(([k,v])=>k==='科学结论'&&v==='尚未核验'));
});
test('failed analysis does not mark collection and analysis complete',()=>{
 const info=executionPanel({worker_alive:true,job:{state:'analysis_failed',label:'分析失败',dispatch_count:1,max_attempts:2,job_id:'123456',scheduler_state:'completed',events:[]}});
 assert.equal(info.panel.children[1].children[3].className,'current');
});
