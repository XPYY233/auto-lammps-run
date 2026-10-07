const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');

test('a rejected source quote remains the current visible stage after reopening',()=>{
 const elements=new Map(),c=vm.createContext({current:{status:'draft'},candidateRecord:null,executionState:null,
  ordinaryExecutionJob:()=>null,activityData:{now:'已收到需求',condition_preparation:{state:'failed',label:'原文依据核对未通过',detail:'条件未导入',call_count:2}},
  $:key=>{if(!elements.has(key))elements.set(key,{});return elements.get(key)}});
 vm.runInContext(source.slice(source.indexOf('function renderCurrentActivity()'),source.indexOf('async function refreshActivity()')),c);
 c.renderCurrentActivity();
 assert.equal(elements.get('#ai-current-title').textContent,'原文依据核对未通过');
 assert.equal(elements.get('#ai-current-detail').textContent,'条件未导入');
 assert.match(elements.get('#ai-current-meta').textContent,/2 次模型调用/);
 assert.match(elements.get('#ai-current-meta').textContent,/尚未生成方案或提交计算/);
});

test('saved condition failure is shown in task list but never overrides actual HPC state',()=>{
 const c=vm.createContext({taskFinished:()=>false});
 vm.runInContext(source.slice(source.indexOf('function taskState(t)'),source.indexOf('function statsFor(')),c);
 assert.equal(c.taskStateLabel({status:'draft',condition_preparation_state:'failed'}),'需求条件整理失败');
 assert.equal(c.taskStateLabel({status:'draft',condition_preparation_state:'repairing'}),'正在整理需求条件');
 assert.equal(c.taskStateLabel({status:'draft',condition_preparation_state:'needs_reconciliation'}),'条件整理记录待核对');
 assert.equal(c.taskStateLabel({status:'conditions_frozen',execution_state:'running',condition_preparation_state:'failed'}),'运行中');
});

test('refreshing or opening progress is read only and does not occupy the mutation lock',async()=>{
 const clicks={};let reads=0,mutations=0;
 const panel={open:true},c=vm.createContext({document:{getElementById:()=>panel},bind:(id,fn)=>clicks[id]=fn,
  refreshActivity:()=>{reads++},action:()=>{mutations++}});
 vm.runInContext(source.split('\n').find(line=>line.startsWith('if(activityPanel)')),vm.createContext({...c,activityPanel:panel}));
 panel.ontoggle();
 vm.runInContext(source.split('\n').find(line=>line.startsWith("bind('ai-activity-refresh',")),c);
 clicks['ai-activity-refresh']({preventDefault(){},stopPropagation(){}});
 assert.equal(reads,2);assert.equal(mutations,0);
 assert.doesNotMatch(source.slice(source.indexOf('async function refreshActivity()'),source.indexOf('function renderPlanSummary(')),/action\(refreshActivity\)/);
});

test('an older progress response cannot replace the newly opened task',async()=>{
 const pending=[];let renders=0;
 const box={replaceChildren(){},append(){}};
 const c=vm.createContext({current:{id:'old'},activityGeneration:0,activityTimer:null,activityData:null,
  $:key=>key==='#ai-activity'?box:null,api:()=>new Promise(resolve=>pending.push(resolve)),
  node:()=>({}),renderCurrentActivity:()=>renders++,setTimeout:()=>1,clearTimeout(){}});
 vm.runInContext(source.slice(source.indexOf('function scheduleActivityRefresh()'),source.indexOf('function renderPlanSummary(')),c);
 const first=c.refreshActivity();c.current={id:'new'};const second=c.refreshActivity();
 pending[1]({now:'new response',steps:[]});await second;
 pending[0]({now:'old response',steps:[]});await first;
 assert.equal(c.activityData.now,'new response');assert.equal(renders,1);
});

test('a temporary progress read failure schedules another read without a mutation',async()=>{
 let scheduled=0;
 const box={replaceChildren(){}};
 const c=vm.createContext({current:{id:'same'},activityGeneration:0,activityTimer:null,
  $:key=>key==='#ai-activity'?box:null,api:async()=>{throw Error('temporary read')},node:()=>({}),
  setTimeout:()=>{scheduled++;return 1},clearTimeout(){}});
 vm.runInContext(source.slice(source.indexOf('function scheduleActivityRefresh()'),source.indexOf('function renderPlanSummary(')),c);
 await c.refreshActivity();assert.equal(scheduled,1);
});

test('switching to a new draft never promises a free recovery from another task',()=>{
 const elements=new Map(),c=vm.createContext({current:{id:'old',status:'draft'},candidateRecord:null,executionState:null,
  ordinaryExecutionJob:()=>null,activityData:{task_id:'old',condition_preparation:{state:'awaiting_import',label:'待恢复',recovery_required:true,call_count:1}},
  $:key=>{if(!elements.has(key))elements.set(key,{});return elements.get(key)}});
 vm.runInContext(source.slice(source.indexOf('function renderCurrentActivity()'),source.indexOf('function scheduleActivityRefresh()')),c);
 c.renderCurrentActivity();assert.match(elements.get('#generate-conditions').textContent,/不新增调用/);
 c.current={id:'new',status:'draft'};c.renderCurrentActivity();
 assert.equal(elements.get('#generate-conditions').textContent,'根据需求整理条件');
 assert.equal(elements.get('#ai-current-title').textContent,'等待准备计算方案');
 c.activityData={task_id:'new',steps:[]};c.renderCurrentActivity();
 assert.equal(elements.get('#generate-conditions').textContent,'根据需求整理条件');
});

function conditionEntry(api){
 const c=vm.createContext({current:{id:'task',revision:1},$:()=>({}),notice(){},api,
  afterChange:async()=>{},refreshModelStatus:async()=>{},render(){},refreshActivity:async()=>{}});
 vm.runInContext(source.slice(source.indexOf('async function generateConditions()'),
  source.indexOf("$('#generate-conditions').onclick=")),c);
 return c;
}

test('an explicit condition retry names the latest failed request after a read-only reconciliation',async()=>{
 const requests=[],failed='a'.repeat(32);
 const c=conditionEntry(async(path,payload)=>{
  requests.push({path,payload});
  if(path.endsWith('/ai-activity'))return {task_id:'task',condition_preparation:{state:'failed',request_id:failed}};
  return {id:'task',revision:2};
 });
 await c.generateConditions();
 assert.equal(requests.length,2);
 assert.equal(requests[0].path,'/api/tasks/task/ai-activity');
 assert.equal(requests[0].payload,undefined);
 assert.equal(requests[1].path,'/api/tasks/task/generate-conditions');
 assert.deepEqual(JSON.parse(JSON.stringify(requests[1].payload)),{revision:1,retry_of:failed});
 assert.equal(c.current.revision,2);
});

test('active or completed-receipt condition requests use recovery without selecting a retry',async()=>{
 for(const state of ['generating','awaiting_import']){
  let payload;
  const c=conditionEntry(async(path,data)=>{
   if(path.endsWith('/ai-activity'))return {task_id:'task',condition_preparation:{state,request_id:'b'.repeat(32)}};
   payload=data;return {id:'task',revision:2};
  });
  await c.generateConditions();
  assert.deepEqual(JSON.parse(JSON.stringify(payload)),{revision:1});
 }
});

test('an interrupted condition POST is not automatically resent with a fresh identity',async()=>{
 let posts=0;
 const c=conditionEntry(async(path)=>{
  if(path.endsWith('/ai-activity'))return {task_id:'task',condition_preparation:{state:'failed',request_id:'a'.repeat(32)}};
  posts++;throw Error('synthetic network disconnect');
 });
 await assert.rejects(c.generateConditions(),/synthetic network disconnect/);
 assert.equal(posts,1);assert.equal(c.current.revision,1);
});

test('unavailable or mismatched condition progress cannot dispatch a model request',async()=>{
 for(const progress of [null,{task_id:'other'},
  {task_id:'task',condition_preparation:{state:'failed'}}]){
  let posts=0;
  const c=conditionEntry(async(path)=>{
   if(path.endsWith('/ai-activity')){
    if(progress===null)throw Error('synthetic progress unavailable');
    return progress;
   }
   posts++;return {};
  });
  await assert.rejects(c.generateConditions());
  assert.equal(posts,0);
 }
});

test('switching tasks while reconciling condition history cannot submit the old task',async()=>{
 let release,posts=0;
 const c=conditionEntry(async(path)=>{
  if(path.endsWith('/ai-activity'))return new Promise(resolve=>{release=resolve});
  posts++;return {};
 });
 const pending=c.generateConditions();c.current={id:'new',revision:1};
 release({task_id:'task',condition_preparation:{state:'failed',request_id:'a'.repeat(32)}});
 await pending;
 assert.equal(posts,0);assert.equal(c.current.id,'new');
});
