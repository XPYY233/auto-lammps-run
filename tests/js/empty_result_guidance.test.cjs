const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const guidance=source.slice(source.indexOf('function emptyResultGuidance(){'),source.indexOf('function renderWorkspaceResults(){'));

function forTask({job=null,candidate=null,mode='research',status='conditions_frozen',exhausted=false}={}){
 const context=vm.createContext({
  executionState:job?{job}:null,ordinaryExecutionJob:()=>null,
  candidateRecord:candidate,current:{mode,status},proposalRoundsExhausted:()=>exhausted,
 });
 vm.runInContext(guidance,context);
 return Array.from(context.emptyResultGuidance());
}

test('exhausted plan points to its saved failure instead of model settings or another attempt',()=>{
 const [title,detail,label,target]=forTask({candidate:{state:'failed'},exhausted:true});
 assert.match(title,/方案机会已用完/);
 assert.match(detail,/不会自动追加调用/);
 assert.equal(label,'查看未完成原因');
 assert.equal(target,'#candidate-attention');
});

test('running calculation points to live progress without claiming results',()=>{
 const [title,detail,,target]=forTask({job:{state:'running'}});
 assert.match(title,/正在进行/);
 assert.match(detail,/不会重新提交计算/);
 assert.equal(target,'#execution-flow');
});

test('confirmed conditions lead to AI plan preparation before any result exists',()=>{
 const [title,detail,,target]=forTask();
 assert.match(title,/尚未计算/);
 assert.match(detail,/应用 AI 准备计算方案/);
 assert.equal(target,'#candidate-panel');
});

test('paper reproduction without a result directs the user to its P–A–B record',()=>{
 const [title,,,target]=forTask({mode:'reproduction'});
 assert.match(title,/复现结果尚未生成/);
 assert.equal(target,'#reference-progress');
});
