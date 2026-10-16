const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const start=source.indexOf('async function renderAuthorExecutionPanel(');
const end=source.indexOf('async function renderAuthorResourcePanel(',start);
assert.ok(start>=0&&end>start);
const TASK='a'.repeat(32),OTHER='b'.repeat(32),PACKAGE='c'.repeat(64);
function element(tag,text=''){
 return {tag,textContent:text??'',children:[],dataset:{},disabled:false,hidden:false,
  append(...rows){this.children.push(...rows);}};
}
const all=root=>[root,...root.children.flatMap(all)];
const plain=value=>JSON.parse(JSON.stringify(value));
function setup(){
 const parent=element('section');parent.dataset.task=TASK;
 const calls=[],refreshes=[],state={};
 const c=vm.createContext({current:{id:TASK},node:element,$:()=>({hidden:false}),
  api:(path,body)=>new Promise((resolve,reject)=>calls.push({path,body,resolve,reject})),
  renderAuthorPlanPanel:async id=>{refreshes.push(id);}});
 vm.runInContext(source.slice(start,end),c);
 return {c,parent,calls,refreshes,state};
}
const data=(overrides={})=>({task_id:TASK,task_revision:9,can_prepare:true,can_approve:true,can_submit:false,history:[],
 latest:{package_sha256:PACKAGE,approved:false,requests:[],report:{resources:{cores:32,wall_seconds:3600},files:[],invocations:[{}],outputs:[]}},...overrides});
const button=(s,text)=>all(s.parent).find(item=>item.tag==='button'&&item.textContent===text);

test('opening the author execution panel never mutates or generates a new model proposal',async()=>{
 const s=setup();await s.c.renderAuthorExecutionPanel(TASK,s.parent,s.state,data());
 assert.equal(s.calls.length,0);assert.equal(button(s,'将已批准方案提交 HPC').disabled,true);
 assert.ok(all(s.parent).some(item=>item.tag==='summary'&&item.textContent.includes('完整原调用')));
});

test('approval and real HPC submission are distinct endpoints bound to the current package',async()=>{
 const s=setup();await s.c.renderAuthorExecutionPanel(TASK,s.parent,s.state,data());
 const approval=button(s,'批准这份原调用提交包').onclick();
 await button(s,'批准这份原调用提交包').onclick();
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].path,`/api/tasks/${TASK}/author-execution/approve`);
 assert.equal(s.calls[0].body.package_sha256,PACKAGE);assert.equal(s.calls[0].body.revision,9);
 s.calls[0].resolve({});await approval;
 const second=setup();await second.c.renderAuthorExecutionPanel(TASK,second.parent,second.state,data({can_approve:false,can_submit:true}));
 const submitting=button(second,'将已批准方案提交 HPC').onclick();
 assert.equal(second.calls[0].path,`/api/tasks/${TASK}/author-execution/submit`);
 assert.deepEqual(plain(second.calls[0].body),{package_sha256:PACKAGE});
 assert.ok(!second.calls[0].path.endsWith('/author-plan'));
 second.calls[0].resolve({});await submitting;
 assert.deepEqual(second.refreshes,[TASK]);
});

test('a completed author submission cannot repaint a different task and following never submits',async()=>{
 const s=setup();const record={id:'d'.repeat(32),job_id:'321',state:'running',dispatch_claimed:1,actual_core_seconds:null};
 await s.c.renderAuthorExecutionPanel(TASK,s.parent,s.state,data({history:[{requests:[record]}]}));
 assert.ok(all(s.parent).some(item=>item.textContent.includes('作业 321')));
 const following=button(s,'核对原作业状态并回收结果').onclick();
 assert.equal(s.calls[0].path,`/api/tasks/${TASK}/author-execution/follow`);assert.deepEqual(plain(s.calls[0].body),{});
 vm.runInContext(`current={id:'${OTHER}'}`,s.c);s.calls[0].resolve({});await following;
 assert.equal(s.refreshes.length,0);
});
