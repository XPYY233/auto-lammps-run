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
 const context=vm.createContext({Map,Date,Promise,current:{id:'one',status:'draft',updated_at:Date.now(),fields:{}},workspaceGeneration:0,workspaceState:{task:'one',phase:'ready',updated:new Date(0)},workspaceReport:{old:true},rawResult:{task:'one',files:[]},normalResult:{evaluations:[]},executionState:{old:true},resultTab:'overview',renders:[],targetRenders:[],requests,
 $:selector=>{if(!elements.has(selector))elements.set(selector,element());return elements.get(selector)},node:()=>element(),
 api:path=>new Promise((resolve,reject)=>requests.push({path,resolve,reject})),
 renderWorkspaceResults(){context.renders.push({phase:context.workspaceState.phase,report:context.workspaceReport})},renderTargetPlanning(){context.targetRenders.push(context.workspaceReport)},renderRefreshStatus(){},renderFlow(){},addInfo(){},renderRawFiles(){},currentRawFiles(){return []},renderExecutionControls(){},refreshDiscussion:async()=>{},document:{createTextNode:x=>x}});
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
