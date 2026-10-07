const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function setup(job,review={state:'prepared',approved:true,files:[]}){
 const elements=new Map(),calls=[];
 const el=(tag,text)=>({hidden:false,disabled:false,dataset:{},textContent:text||'',value:'',children:[],append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},focus(){}});
 const c=vm.createContext({current:{id:'task',revision:7},executionState:null,requestStates:{running:'运行中'},schema:{automatic_workflow:{configured:true}},node:el,
 $:s=>{if(!elements.has(s))elements.set(s,el());return elements.get(s)},
 api:async(path,body)=>{calls.push({path,body});if(path.endsWith('/plan'))return review;if(path.endsWith('/execution')&&!body)return {job};if(path==='/api/tasks/task')return {id:'task',revision:7};return {};},
 notice:()=>{},action:fn=>fn(),afterChange:async()=>{},refreshCandidate:async()=>{},refreshActivity:async()=>{}});
 vm.runInContext(source.slice(source.indexOf('function researchText('),source.indexOf('function renderCurrentActivity(')),c);
 vm.runInContext(source.slice(source.indexOf('function renderPlanSummary('),source.indexOf('async function refreshCandidate()')),c);
 return {c,elements,calls};
}
test('approved fresh plan dispatches through execution without restarting generation',async()=>{const {c,elements,calls}=setup(null);await c.refreshPlanReview();await elements.get('#plan-approve').onclick();assert.deepEqual(calls.filter(x=>x.body).map(x=>x.path),['/api/tasks/task/execution']);});
test('three-round limit disables rewrite but keeps a complete plan approvable',async()=>{
 const review={state:'prepared',approved:true,files:[],proposal_rounds:{limit:3,used:3,remaining:0,historical_count_unknown:false}};
 const {c,elements,calls}=setup(null,review);await c.refreshPlanReview();
 assert.match(elements.get('#plan-version').textContent,/3 \/ 3/);
 assert.equal(elements.get('#plan-revise').disabled,true);
 assert.equal(elements.get('#plan-revision-note').disabled,true);
 assert.match(elements.get('#plan-revision-help').textContent,/不能追加.*可查看并批准/);
 assert.equal(elements.get('#plan-approve').disabled,false);
 await elements.get('#plan-revise').onclick();assert.equal(calls.some(x=>x.body),false);
 await elements.get('#plan-approve').onclick();
 assert.deepEqual(calls.filter(x=>x.body).map(x=>x.path),['/api/tasks/task/execution']);
});
test('failed preparation of execution resumes through the accounted recheck endpoint',async()=>{const {c,elements,calls}=setup({state:'attention'});await c.refreshPlanReview();await elements.get('#plan-approve').onclick();assert.deepEqual(calls.filter(x=>x.body).map(x=>x.path),['/api/tasks/task/execution/recheck']);});
test('known scheduler job or active dispatch cannot be submitted a second time',async()=>{for(const job of [{state:'waiting',job_id:'42'},{state:'running'}]){const {c,elements}=setup(job);await c.refreshPlanReview();assert.equal(elements.get('#plan-approve').disabled,true);}});
test('running job receipt replaces stale submit instruction and locks plan editing',async()=>{
 const {c,elements,calls}=setup({state:'waiting',scheduler_state:'running',job_id:'42'});await c.refreshPlanReview();
 assert.match(elements.get('#plan-note').textContent,/42.*运行中/);
 assert.doesNotMatch(elements.get('#plan-note').textContent,/点.*提交/);
 assert.equal(elements.get('#plan-revise').disabled,true);
 await elements.get('#plan-approve').onclick();assert.equal(calls.some(x=>x.body),false);
});
test('revision reads the input beside the plan and uses the existing iteration endpoint',async()=>{
 const {c,elements,calls}=setup(null);await c.refreshPlanReview();
 elements.get('#plan-revision-note').value='Retain all sizes';
 await elements.get('#plan-revise').onclick();
 const writes=calls.filter(x=>x.body);assert.equal(writes.length,1);
 assert.equal(writes[0].path,'/api/tasks/task/plan/revise');assert.equal(writes[0].body.note,'Retain all sizes');
});
test('previous verified plan stays visible during iteration but cannot be approved',async()=>{
 const review={state:'checking_plan',approved:false,workspace:{versions:[],current:{version:2,historical:true,structure:{},additional_structures:[],resources:{},analysis:{},potential:{},files:[]}}};
 const {c,elements}=setup(null,review);await c.refreshPlanReview();
 assert.equal(elements.get('#plan-review-panel').hidden,false);assert.equal(elements.get('#plan-approve').disabled,true);
 assert.match(elements.get('#plan-version').textContent,/上一份/);
});
test('scientific text is readable without changing the stored request',()=>{
 const {c}=setup(null);assert.equal(c.researchText('\\(T=0\\) K; 6\\times6'), 'T=0 K; 6×6');
});
test('uncertain dispatch keeps plan revision locked instead of creating a new plan',async()=>{
 const {c,elements}=setup({state:'attention',scheduler_state:'unknown',dispatch_count:1});
 await c.refreshPlanReview();assert.equal(elements.get('#plan-revise').disabled,true);
});
test('failed execution read cannot leave an active approval button behind',async()=>{
 const {c,elements}=setup(null);const api=c.api;c.api=async(path,body)=>{if(path.endsWith('/execution'))throw Error('offline');return api(path,body)};
 await c.refreshPlanReview();assert.equal(elements.get('#plan-approve').disabled,true);
});
test('refresh preserves expanded version differences and script previews',async()=>{
 const review={state:'prepared',approved:true,files:[{name:'in.lammps',size:7,sha256:'fixed',content:'run 0'}],workspace:{versions:[{number:1,at:'2026-01-01',available:true,changes:[],reason:'first'}],current:{version:1,historical:false,structure:{},additional_structures:[],resources:{},analysis:{},potential:{},files:[{name:'in.lammps',size:7,sha256:'fixed',content:'run 0'}]}}};
 const {c,elements}=setup(null,review);await c.refreshPlanReview();
 elements.get('#plan-files').children[0].open=true;elements.get('#plan-version-list').children[0].open=true;
 await c.refreshPlanReview();assert.equal(elements.get('#plan-files').children[0].open,true);assert.equal(elements.get('#plan-version-list').children[0].open,true);
});
