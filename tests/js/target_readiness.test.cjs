const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function setup(){const pending=[],states=[],errors=[];let current=true;const c=vm.createContext({api:()=>new Promise((resolve,reject)=>pending.push({resolve,reject})),isCurrent:()=>current,onState:s=>states.push(s),onError:e=>errors.push(e)});vm.runInContext(source.slice(source.indexOf('function targetReadinessRequest('),source.indexOf('function renderTargetPlanning(')),c);return{run:c.targetReadinessRequest(c.isCurrent,c.onState,c.onError),pending,states,errors,leave:()=>current=false};}
test('older preview cannot enable freeze after selection changes',async()=>{const c=setup(),first=c.run('task',{}),second=c.run('task',{});c.pending[1].resolve({can_freeze:false});await second;c.pending[0].resolve({can_freeze:true});await first;assert.deepEqual(c.states,[{can_freeze:false}]);});
test('preview from departed task cannot change new task controls',async()=>{const c=setup(),first=c.run('task',{});c.leave();c.pending[0].resolve({can_freeze:true});await first;assert.equal(c.states.length,0);});
test('only current preview error is displayed',async()=>{const c=setup(),first=c.run('task',{}),second=c.run('task',{});c.pending[0].reject(new Error('old'));await first;c.pending[1].reject(new Error('current'));await second;assert.equal(c.errors.length,1);assert.equal(c.errors[0].message,'current');});
test('verified scoped acceptance and manual finish remain distinct from scheduler completion',()=>{const c=vm.createContext({});vm.runInContext(source.slice(source.indexOf('function taskFinished(t)'),source.indexOf('function statsFor(')),c);assert.equal(c.taskState({execution_state:'completed'}),'completed');assert.equal(c.taskStateLabel({user_finished:true}),'已确认结束');assert.equal(c.taskStateLabel({execution_state:'completed',scoped_acceptance:{status:'accepted_by_user'}}),'验收通过 · 基准工况');});

test('manual finish is read from the lifecycle events the API actually returns',()=>{const c=vm.createContext({});vm.runInContext(source.slice(source.indexOf('function taskFinished(t)'),source.indexOf('function statsFor(')),c);const finished={status:'conditions_frozen',lifecycle_revision:1,lifecycle_events:[{sequence:1,action:'finish'}]};assert.equal(c.taskFinished(finished),true);assert.equal(c.taskStateLabel(finished),'已确认结束');assert.equal(c.taskFinished({lifecycle_events:[]}),false);assert.equal(c.taskStateLabel({execution_state:'completed',lifecycle_events:[{action:'finish'}],scoped_acceptance:{status:'accepted_by_user'}}),'验收通过 · 基准工况');});

test('the task list reflects preparation progress, not only execution',()=>{const c=vm.createContext({});vm.runInContext(source.slice(source.indexOf('function taskFinished(t)'),source.indexOf('function statsFor(')),c);
assert.equal(c.taskStateLabel({status:'conditions_frozen'}),'条件已冻结 · 待准备方案');
assert.equal(c.taskStateLabel({status:'conditions_frozen',preparation_state:'queued'}),'方案准备中');
assert.equal(c.taskStateLabel({status:'conditions_frozen',preparation_state:'prepared'}),'方案已准备 · 待执行');
assert.equal(c.taskStateLabel({status:'conditions_frozen',preparation_state:'clarification'}),'需要补充条件');
assert.equal(c.taskStateLabel({status:'conditions_frozen',preparation_state:'failed'}),'方案准备失败');
assert.equal(c.taskStateLabel({status:'draft'}),'待准备');
assert.equal(c.taskStateLabel({status:'conditions_frozen',execution_state:'running',preparation_state:'prepared'}),'运行中');});
