const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function setup(job){
 const elements=new Map(),calls=[];
 const el=()=>({hidden:false,disabled:false,append(){},replaceChildren(){}});
 const c=vm.createContext({current:{id:'task',revision:7},schema:{automatic_workflow:{configured:true}},node:el,
 $:s=>{if(!elements.has(s))elements.set(s,el());return elements.get(s)},
 api:async(path,body)=>{calls.push({path,body});if(path.endsWith('/plan'))return {state:'prepared',approved:true,files:[]};if(path.endsWith('/execution')&&!body)return {job};if(path==='/api/tasks/task')return {id:'task',revision:7};return {};},
 action:fn=>fn(),afterChange:async()=>{},refreshCandidate:async()=>{}});
 vm.runInContext(source.slice(source.indexOf('async function refreshPlanReview(){'),source.indexOf('async function refreshCandidate()')),c);
 return {c,elements,calls};
}
test('approved fresh plan dispatches through execution without restarting generation',async()=>{const {c,elements,calls}=setup(null);await c.refreshPlanReview();await elements.get('#plan-approve').onclick();assert.deepEqual(calls.filter(x=>x.body).map(x=>x.path),['/api/tasks/task/execution']);});
test('failed preparation of execution resumes through the accounted recheck endpoint',async()=>{const {c,elements,calls}=setup({state:'attention'});await c.refreshPlanReview();await elements.get('#plan-approve').onclick();assert.deepEqual(calls.filter(x=>x.body).map(x=>x.path),['/api/tasks/task/execution/recheck']);});
test('known scheduler job or active dispatch cannot be submitted a second time',async()=>{for(const job of [{state:'waiting',job_id:'42'},{state:'running'}]){const {c,elements}=setup(job);await c.refreshPlanReview();assert.equal(elements.get('#plan-approve').disabled,true);}});
