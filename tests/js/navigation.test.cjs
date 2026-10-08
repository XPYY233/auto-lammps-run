const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function setup(hash='#home'){
 const c=vm.createContext({location:{hash},visits:[],writes:[],listeners:{},errors:[],document:{querySelectorAll:()=>[]}});
 c.history={pushState:(_,__,h)=>{c.writes.push(['push',h]);c.location.hash=h;},replaceState:(_,__,h)=>{c.writes.push(['replace',h]);c.location.hash=h;}};
 c.window={addEventListener:(name,fn)=>c.listeners[name]=fn};
 c.notice=(m)=>c.errors.push(m);
 vm.runInContext('let busy=false,pendingRoute=null,renderedRoute=null;',c);
 vm.runInContext(source.slice(source.indexOf('async function action('),source.indexOf('async function listTasks(')),c);
 for(const [name,route] of Object.entries({showHome:'#home',renderNew:'#new',showTasks:'#tasks',showPapers:'#papers',showResources:'#resources',showHelp:'#help'}))c[name]=()=>{c.visits.push(route);c.recordRoute(route);};
 c.openTask=async id=>{c.visits.push('#'+id);c.recordRoute('#'+id);};
 return c;
}
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
test('navigation adds history once and native back/forward restores without new entries',async()=>{
 const c=setup();c.restoreRoute();await flush();c.requestRoute('#tasks');await flush();c.requestRoute('#resources');await flush();
 assert.deepEqual(c.writes,[['push','#tasks'],['push','#resources']]);
 c.location.hash='#tasks';c.listeners.popstate();await flush();
 c.location.hash='#resources';c.listeners.popstate();await flush();
 assert.deepEqual(c.visits,['#home','#tasks','#resources','#tasks','#resources']);assert.equal(c.writes.length,2);
});
test('latest navigation survives busy work and an old response cannot replace its address',async()=>{
 const c=setup();c.restoreRoute();await flush();let release;
 const hold=c.action(()=>new Promise(r=>release=r));c.requestRoute('#tasks');c.requestRoute('#help');c.recordRoute('#old');
 assert.equal(c.location.hash,'#help');assert.deepEqual(c.visits,['#home']);release();await hold;await flush();
 assert.deepEqual(c.visits,['#home','#help']);assert.equal(c.location.hash,'#help');
});
test('direct task hash and unknown hash recover without dispatch actions',async()=>{
 const id='a'.repeat(32),c=setup('#'+id);c.restoreRoute();await flush();assert.deepEqual(c.visits,['#'+id]);
 c.location.hash='#invalid';c.listeners.hashchange();await flush();assert.equal(c.visits.at(-1),'#home');assert.equal(c.location.hash,'#home');
});
test('queued route is restored after a failed action, including the previously displayed page',async()=>{
 const c=setup();c.restoreRoute();await flush();let reject;
 const hold=c.action(()=>new Promise((_,r)=>reject=r));c.requestRoute('#help');c.requestRoute('#home');reject(new Error('request failed'));
 await hold;await flush();assert.equal(c.visits.at(-1),'#home');assert.equal(c.visits.length,2);assert.deepEqual(c.errors,['request failed']);
});
test('stale task-open response cannot publish task content after a new navigation request',async()=>{
 const c=setup(),elements=new Map();let resolve;c.api=()=>new Promise(r=>resolve=r);
 c.$=selector=>{if(!elements.has(selector))elements.set(selector,element(selector));return elements.get(selector);};
 c.$('#ai-current-title').textContent='Old task status';
 vm.runInContext('let current=null;',c);
 vm.runInContext(source.slice(source.indexOf('async function openTask('),source.indexOf('function showNew(')),c);
 const hold=c.action(()=>c.openTask('a'.repeat(32)));await flush();
 assert.equal(elements.get('#task-view').hidden,true,'old task must disappear before the new GET returns');
 assert.equal(elements.get('#task-loading').hidden,false);
 assert.equal(elements.get('#ai-current-title').textContent,'正在读取当前任务进度…');
 c.requestRoute('#help');resolve({id:'a'.repeat(32)});await hold;await flush();
 assert.equal(vm.runInContext('current',c),null);assert.equal(c.visits.at(-1),'#help');assert.equal(c.location.hash,'#help');
});

function element(tag){
 return {tagName:tag,children:[],textContent:'',hidden:false,disabled:false,value:'',dataset:{},attributes:{},
  append(...items){this.children.push(...items);},
  replaceChildren(...items){this.children=[...items];this.textContent='';},
  setAttribute(name,value){this.attributes[name]=String(value);},getAttribute(name){return this.attributes[name]??null;}};
}
function textContent(element){return [element.textContent,...element.children.map(child=>typeof child==='object'?textContent(child):child)].join(' ');}
const OLD='a'.repeat(32),NEXT='b'.repeat(32);
function setupCandidateNavigation(){
 const elements=new Map(),calls=[],renders=[];
 const c=vm.createContext({
  document:{createElement:element},window:{scrollTo(){}},pendingRoute:null,
  current:{id:OLD,title:'Old task',status:'conditions_frozen',mode:'research',revision:1},
  candidateState:'clarification',candidateTask:OLD,candidateRecord:{id:'old-record',task_id:OLD,state:'clarification',result:{questions:['old question']}},
  candidateAnswers:[{answer:'old answer'}],candidateOutcome:'old outcome',
  schema:{automatic_workflow:{configured:false},candidate_preparation:{enabled:true}},
  normalResult:null,workspaceReport:null,rawResult:null,executionState:null,referenceProgress:null,activityData:null,
  initialGeometryCatalog:null,initialGeometryCatalogTask:null,initialGeometryLoading:false,initialGeometryRead:0,
  workspaceGeneration:0,workspaceState:null,candidateStatuses:{},requestStates:{},
  $:selector=>{if(!elements.has(selector))elements.set(selector,element(selector));return elements.get(selector);},
  api:async(path,data)=>{calls.push({path,data});return {candidate:null};},
  recordRoute(){},listTasks:async()=>{},renderHistory:async()=>{},refreshGuidance:async()=>{},refreshPlanReview:async()=>{},
  refreshActivity:async()=>{},refreshResults:async()=>{},refreshReferenceHistory:async()=>{},refreshWorkspace:async()=>{},refreshDiscussion:async()=>{},
  renderWorkspaceResults(){},renderCandidateClarification(){},ordinaryExecutionJob:()=>null,
  candidateStatus:job=>({label:job.state}),candidateStageChip:job=>{const el=element('span');el.textContent=job.state;return el;},
  proposalRoundsExhausted:()=>false,proposalRoundLabel:()=>'',number:String,duration:String,
 });
 vm.runInContext(source.slice(source.indexOf('function node(tag, value, className) {'),source.indexOf('function notice(')),c);
 vm.runInContext(source.slice(source.indexOf('function renderCurrentActivity(){'),source.indexOf('function scheduleActivityRefresh(')),c);
 vm.runInContext(source.slice(source.indexOf('function clearCandidateView('),source.indexOf("$('#prepare-candidate').onclick=")),c);
 vm.runInContext(source.slice(source.indexOf('async function openTask('),source.indexOf('function showNew(')),c);
 c.render=()=>{renders.push({state:c.candidateState,task:c.candidateTask,record:c.candidateRecord,answers:c.candidateAnswers.length,outcome:c.candidateOutcome});c.renderCurrentActivity();};
 for(const selector of ['#candidate-attention','#candidate-clarification','#candidate-summary','#candidate-downloads','#candidate-stage']){
  const old=element('span');old.textContent='old task content';c.$(selector).append(old);
 }
 c.$('#candidate-answers').value='old answer';
 for(const selector of ['#candidate-note','#candidate-authorization','#candidate-outcome'])c.$(selector).textContent='old task content';
 return {c,elements,calls,renders};
}
function assertCandidateCleared({c,elements}){
 assert.equal(c.candidateState,null);assert.equal(c.candidateTask,null);assert.equal(c.candidateRecord,null);
 assert.equal(c.candidateAnswers.length,0);assert.equal(c.candidateOutcome,'');
 assert.equal(elements.get('#candidate-attention').hidden,true);assert.equal(elements.get('#candidate-answers-block').hidden,true);
 assert.equal(elements.get('#candidate-answers').value,'');
 for(const selector of ['#candidate-attention','#candidate-clarification','#candidate-summary','#candidate-downloads','#candidate-stage'])assert.equal(elements.get(selector).children.length,0,selector);
 for(const selector of ['#candidate-note','#candidate-authorization','#candidate-outcome'])assert.equal(elements.get(selector).textContent,'',selector);
 assert.equal(elements.get('#prepare-candidate').disabled,true);
}
test('opening a draft task clears previous questions and outcome before its first render, without requesting a candidate',async()=>{
 const state=setupCandidateNavigation(),{c,calls,renders,elements}=state;
 c.api=async(path,data)=>{calls.push({path,data});return {id:NEXT,title:'New task',status:'draft',mode:'research',revision:1};};
 await c.openTask(NEXT);
 assertCandidateCleared(state);
 assert.deepEqual(renders,[{state:null,task:null,record:null,answers:0,outcome:''}]);
 assert.deepEqual(calls,[{path:'/api/tasks/'+NEXT,data:undefined}]);
 assert.doesNotMatch(elements.get('#ai-current-title').textContent,/clarification|old/);
});
test('opening a frozen task clears old content while its candidate read is pending',async()=>{
 const state=setupCandidateNavigation(),{c,calls}=state;let resolve;
 c.api=(path,data)=>{calls.push({path,data});return path.endsWith('/candidate')?new Promise(r=>resolve=r):Promise.resolve({id:NEXT,status:'conditions_frozen',mode:'research',revision:1});};
 const pending=c.openTask(NEXT);await flush();assertCandidateCleared(state);
 resolve({candidate:{id:'new-record',task_id:NEXT,state:'prepared',revision:1,result:{summary:'new task only'}}});await pending;
 assert.equal(c.candidateRecord.id,'new-record');assert.equal(c.candidateTask,NEXT);
 assert.equal(calls.every(call=>call.data===undefined),true,'navigation only reads saved state');
});
test('refreshing draft conditions removes stale candidate UI without a model or candidate request',async()=>{
 const state=setupCandidateNavigation();state.c.current.status='draft';await state.c.refreshCandidate();
 assertCandidateCleared(state);assert.equal(state.calls.length,0);
});
test('a candidate response from a departed task cannot restore its attention panel',async()=>{
 const state=setupCandidateNavigation(),{c}=state;let resolve;c.api=()=>new Promise(r=>resolve=r);
 const pending=c.refreshCandidate();c.current={id:NEXT,status:'draft'};await c.refreshCandidate();
 resolve({candidate:{id:'old-late-record',task_id:OLD,state:'clarification'}});await pending;
 assertCandidateCleared(state);
});
test('returning to the same task cannot revive a candidate read from its previous visit',async()=>{
 const state=setupCandidateNavigation(),{c}=state,pending=[];
 c.api=()=>new Promise(resolve=>pending.push(resolve));
 const oldRead=c.refreshCandidate();c.current={id:NEXT,status:'draft'};await c.refreshCandidate();
 c.current={id:OLD,status:'conditions_frozen',mode:'research'};const newRead=c.refreshCandidate();
 pending[1]({candidate:{id:'current-record',task_id:OLD,state:'prepared',result:{}}});await newRead;
 pending[0]({candidate:{id:'old-late-record',task_id:OLD,state:'clarification'}});await oldRead;
 assert.equal(c.candidateRecord.id,'current-record');assert.equal(state.elements.get('#candidate-attention').hidden,true);
});
test('a slower read of the same task cannot replace its newest candidate record',async()=>{
 const state=setupCandidateNavigation(),{c}=state,pending=[];c.api=()=>new Promise(resolve=>pending.push(resolve));
 const first=c.refreshCandidate(),second=c.refreshCandidate();
 pending[1]({candidate:{id:'latest-record',task_id:OLD,state:'prepared',result:{}}});await second;
 pending[0]({candidate:{id:'earlier-record',task_id:OLD,state:'clarification'}});await first;
 assert.equal(c.candidateRecord.id,'latest-record');
});

function setupTaskList(tasks){
 const elements=new Map(),c=vm.createContext({
  document:{createElement:element},current:null,taskCache:tasks,taskFilter:'all',
  $:selector=>{if(!elements.has(selector))elements.set(selector,element(selector));return elements.get(selector);},
  api:async()=>({tasks}),statsFor(){},requestRoute(){},removalButton:label=>element(label),notice(){},
 });
 vm.runInContext(source.slice(source.indexOf('function node(tag, value, className) {'),source.indexOf('function notice(')),c);
 vm.runInContext(source.slice(source.indexOf('function taskFinished(t)'),source.indexOf('function statsFor(')),c);
 vm.runInContext(source.slice(source.indexOf('function taskPhaseLabels('),source.indexOf('async function showTasks(')),c);
 vm.runInContext(source.slice(source.indexOf('async function listTasks('),source.indexOf('async function openTask(')),c);
 return {c,elements};
}
test('task table and sidebar distinguish completed A from failed B preparation and failed A from B clarification',async()=>{
 const tasks=[
  {id:OLD,title:'Complete A',mode:'research',status:'conditions_frozen',reference_state:'completed',reference_stage:'作者参考 A · 计算结束 · 科学结果待核验',preparation_state:'failed',reference_job_id:'123',reference_submission_count:2,submission_count:0},
  {id:NEXT,title:'Failed A',mode:'reproduction',status:'conditions_frozen',reference_state:'failed',reference_stage:'作者参考 A · 计算失败',preparation_state:'clarification',reference_submission_count:1,submission_count:0},
 ];
 const {c,elements}=setupTaskList(tasks);await c.listTasks();c.taskCards();
 for(const container of ['#task-list','#task-cards']){
  const text=textContent(elements.get(container));
  assert.match(text,/作者参考 A · 计算结束 · 科学结果待核验/);assert.match(text,/B 方案 · 方案准备失败/);
  assert.match(text,/作者参考 A · 计算失败/);assert.match(text,/B 方案 · 需要补充条件/);
  assert.doesNotMatch(text,/作者参考：作者参考|B 尚未提交|复现成功/);
 }
 assert.match(textContent(elements.get('#task-cards')),/A 提交：2 次（含失败）/);
 assert.match(textContent(elements.get('#task-cards')),/B 提交：0 \/ 2/);
});
test('reference-only metadata does not leak A failure into B, and ordinary research stays unlabelled',async()=>{
 const tasks=[{id:OLD,title:'Reference only',status:'draft',mode:'research',reference_state:'failed'},
  {id:NEXT,title:'Ordinary research',status:'conditions_frozen',mode:'research',preparation_state:'failed'}];
 const {c,elements}=setupTaskList(tasks);await c.listTasks();c.taskCards();
 const labels=c.taskPhaseLabels(tasks[0]);assert.equal(labels[0].label,'作者参考 A · 计算失败');assert.equal(labels[1].label,'B 方案 · 待准备');
 assert.equal(c.taskPhaseLabels(tasks[1]).length,1);assert.equal(c.taskPhaseLabels(tasks[1])[0].label,'方案准备失败');
 const ordinaryRow=elements.get('#task-list').children[1];assert.doesNotMatch(textContent(ordinaryRow),/作者参考|B 方案|B 计算/);
});
test('unknown submission status and absent job IDs are presented as missing receipts, not proof of no submission',async()=>{
 const tasks=[{id:OLD,title:'Unknown B',mode:'research',status:'conditions_frozen',reference_state:'completed',execution_state:'unknown'},
  {id:NEXT,title:'No receipt',mode:'research',status:'draft'}];
 const {c,elements}=setupTaskList(tasks);await c.listTasks();c.taskCards();const text=textContent(elements.get('#task-cards'));
 assert.match(text,/B 计算 · 提交状态待核对/);assert.match(text,/B 暂无作业回执/);assert.match(text,/暂无作业回执/);
 assert.doesNotMatch(text,/尚未提交|未提交|尚未派发/);
});
test('a reference with retained submissions and no receipt cannot inherit an old not-submitted stage label',async()=>{
 const tasks=[{id:OLD,title:'Retained A intent',mode:'research',status:'conditions_frozen',reference_state:'prepared',
  reference_stage:'作者参考 A · 尚未提交',reference_submission_count:2,reference_job_id:null,preparation_state:'clarification'}];
 const {c,elements}=setupTaskList(tasks);await c.listTasks();c.taskCards();
 for(const container of ['#task-list','#task-cards']){
  const text=textContent(elements.get(container));assert.match(text,/作者参考 A · 暂无作业回执/);assert.doesNotMatch(text,/尚未提交|未提交/);
 }
 assert.match(textContent(elements.get('#task-cards')),/A 提交：2 次（含失败）/);
});

function setupAfterChange(){
 const state=setupCandidateNavigation(),{c,renders}=state,refreshes=[],notices=[];
 c.targetPreviewGeneration=0;
 vm.runInContext(source.slice(source.indexOf('function limitations(report)'),source.indexOf('function reportDownloads(')),c);
 vm.runInContext(source.slice(source.indexOf('function evidenceSourceHeading('),source.indexOf('function closeoutFigure(')),c);
 vm.runInContext(source.slice(source.indexOf('function renderPaperEvidence('),source.indexOf('function targetReadinessRequest(')),c);
 vm.runInContext(source.slice(source.indexOf('function renderTargetPlanning()')),c);
 vm.runInContext(source.slice(source.indexOf('async function afterChange('),source.indexOf("$('#close-dialog').onclick=")),c);
 c.render=()=>{renders.push(c.current);c.$('#task-view').hidden=false;c.renderTargetPlanning();};
 for(const name of ['listTasks','renderHistory','refreshCandidate','refreshResults','refreshReferenceHistory','refreshWorkspace'])c[name]=async()=>{refreshes.push(name);};
 c.notice=message=>notices.push(message);
 return {...state,refreshes,notices};
}
function paperFixture(taskId=OLD){
 return {task_id:taskId,title:'Synthetic paper title',doi:'10.0000/synthetic-fixture',source_note:'Synthetic source receipt',limitations:[],
  figures:[{name:'fixture.png',label:'Synthetic figure 1',caption:'Synthetic caption, page 2'}],
  views:[{id:'figure1',title:'Synthetic view',description:'Synthetic plotted quantity',figures:[{name:'fixture.png'}],tables:[]}]};
}
function findElement(root,predicate){
 if(predicate(root))return root;
 for(const child of root.children||[]){if(typeof child!=='object')continue;const found=findElement(child,predicate);if(found)return found;}
}
test('after saving, a task GET rejoins human-only evidence and renders the actual P figure and source',async()=>{
 const {c,calls,renders,refreshes,notices}=setupAfterChange(),paper=paperFixture();
 const mutation={id:OLD,status:'draft',mode:'research',revision:2},enriched={...mutation,paper_evidence:paper};
 c.current={...mutation,revision:1,paper_evidence:paper};c.render();renders.length=0;
 // The save endpoint returns ordinary task inputs, without human-only P evidence.
 c.current=mutation;c.api=async(path,data)=>{calls.push({path,data});return enriched;};
 await c.afterChange('Saved task');
 assert.deepEqual(calls,[{path:'/api/tasks/'+OLD,data:undefined}]);assert.equal(c.current,enriched);
 assert.equal(renders.length,1);assert.equal(renders[0],enriched);
 const box=c.$('#target-planning'),text=textContent(box);assert.equal(box.hidden,false);
 assert.match(text,/文献工作台 · 论文结果 P/);assert.match(text,/Synthetic paper title/);
 assert.match(text,/10\.0000\/synthetic-fixture/);assert.match(text,/Synthetic caption, page 2/);
 const image=findElement(box,item=>item.tagName==='img');assert.equal(image.alt,'Synthetic figure 1');
 assert.equal(image.src,`/api/tasks/${OLD}/paper-evidence/files/fixture.png`);
 assert.equal(findElement(box,item=>item.tagName==='a'&&item.href===image.src).children[0],image);
 assert.deepEqual(refreshes,['listTasks','renderHistory','refreshCandidate','refreshResults','refreshReferenceHistory','refreshWorkspace']);
 assert.deepEqual(notices,['Saved task']);
});
test('a late task GET after saving cannot replace another task or its P evidence',async()=>{
 const {c,calls,renders,refreshes,notices}=setupAfterChange();let resolve;
 c.current={id:OLD,status:'draft',mode:'research',revision:2};
 c.api=(path,data)=>{calls.push({path,data});return new Promise(r=>resolve=r);};
 const pending=c.afterChange('Saved old task');assert.equal(calls.length,1);
 const next={id:NEXT,status:'draft',mode:'research',revision:3,paper_evidence:paperFixture(NEXT)};
 c.current=next;const marker=element('p');marker.textContent='New task evidence stays visible';c.$('#target-planning').replaceChildren(marker);
 resolve({id:OLD,status:'draft',mode:'research',revision:2,paper_evidence:paperFixture()});await pending;
 assert.equal(c.current,next);assert.equal(c.$('#target-planning').children[0],marker);
 assert.deepEqual(renders,[]);assert.deepEqual(refreshes,[]);assert.deepEqual(notices,[]);
 assert.deepEqual(calls,[{path:'/api/tasks/'+OLD,data:undefined}]);
});
test('closing the task view while its saved-task GET is pending prevents every repaint and follow-up refresh',async()=>{
 const {c,calls,renders,refreshes,notices}=setupAfterChange();let resolve;
 const mutation={id:OLD,status:'draft',mode:'research',revision:2};c.current=mutation;
 c.api=(path,data)=>{calls.push({path,data});return new Promise(r=>resolve=r);};
 const pending=c.afterChange('Saved task');assert.equal(calls.length,1);
 c.$('#task-view').hidden=true;const marker=element('p');marker.textContent='Closed page';c.$('#target-planning').replaceChildren(marker);
 resolve({...mutation,paper_evidence:paperFixture()});await pending;
 assert.equal(c.current,mutation);assert.equal(c.$('#task-view').hidden,true);assert.equal(c.$('#target-planning').children[0],marker);
 assert.deepEqual(renders,[]);assert.deepEqual(refreshes,[]);assert.deepEqual(notices,[]);
});

function setupTaskCreation(){
 const state=setupAfterChange(),{c}=state,routes=[],modes=[];
 let submitted;
 c.current=null;c.resultTab='overview';c.schema.model_calls_enabled=false;c.$('#task-view').hidden=true;
 c.FormData=function(form){return new Map(Object.entries(form.values));};
 c.action=work=>{submitted=work();return submitted;};c.recordRoute=route=>routes.push(route);c.setMode=mode=>modes.push(mode);
 c.generateConditions=()=>assert.fail('creating a task with model calls disabled must not request a model');
 vm.runInContext(source.slice(source.indexOf("$('#create-form').onsubmit="),source.indexOf("$('#condition-form').onsubmit=")),c);
 const form={values:{title:'Synthetic new task',prompt:'Synthetic research request',mode:'research'},resetCount:0,reset(){this.resetCount++;}};
 return {...state,routes,modes,form,submit(){c.$('#create-form').onsubmit({target:form,preventDefault(){}});return submitted;}};
}
test('the actual create handler opens its saved task from a hidden view and rejoins P without a model call',async()=>{
 const state=setupTaskCreation(),{c,calls,renders,routes,modes,form}=state;
 const mutation={id:NEXT,title:form.values.title,prompt:form.values.prompt,mode:'research',status:'draft',revision:1};
 const enriched={...mutation,paper_evidence:paperFixture(NEXT)};
 c.api=async(path,data)=>{calls.push({path,data});return path==='/api/tasks'?mutation:enriched;};
 await state.submit();
 assert.equal(c.current,enriched);assert.equal(c.$('#task-view').hidden,false);assert.equal(renders.length,1);
 assert.equal(calls.length,2);assert.equal(calls[0].path,'/api/tasks');
 assert.equal(calls[0].data.prompt,form.values.prompt);assert.equal(calls[0].data.mode,'research');
 assert.deepEqual(calls[1],{path:'/api/tasks/'+NEXT,data:undefined});
 assert.deepEqual(routes,['#'+NEXT]);assert.deepEqual(modes,['research']);assert.equal(form.resetCount,1);
 assert.match(textContent(c.$('#target-planning')),/文献工作台 · 论文结果 P/);
 assert.equal(findElement(c.$('#target-planning'),item=>item.tagName==='img').src,`/api/tasks/${NEXT}/paper-evidence/files/fixture.png`);
});
test('a queued navigation still prevents the actual create handler from repainting after its late task GET',async()=>{
 const state=setupTaskCreation(),{c,calls,renders,refreshes,notices,form}=state;let resolve;
 const mutation={id:NEXT,title:form.values.title,prompt:form.values.prompt,mode:'research',status:'draft',revision:1};
 c.api=(path,data)=>{calls.push({path,data});return path==='/api/tasks'?Promise.resolve(mutation):new Promise(r=>resolve=r);};
 const pending=state.submit();await flush();assert.equal(calls.length,2);
 c.pendingRoute='#help';const marker=element('p');marker.textContent='Next page';c.$('#target-planning').replaceChildren(marker);
 resolve({...mutation,paper_evidence:paperFixture(NEXT)});await pending;
 assert.equal(c.current,mutation);assert.equal(c.$('#task-view').hidden,true);assert.equal(c.$('#target-planning').children[0],marker);
 assert.deepEqual(renders,[]);assert.deepEqual(refreshes,[]);assert.deepEqual(notices,[]);
});

function setupTaskTagWorkspace(fields){
 const state=setupCandidateNavigation(),{c,calls}=state;
 c.current={id:OLD,status:'draft',mode:'research',updated_at:'2026-01-01T00:00:00Z',fields,prompt:'Complete research request'};
 c.workspaceState={task:OLD,phase:'ready',updated:null};c.resultTab='overview';
 for(const name of ['renderRefreshStatus','renderTargetPlanning','renderFlow','addInfo','renderRawFiles','renderExecutionControls','renderReferenceProgress'])c[name]=()=>{};
 c.currentRawFiles=()=>[];
 const replies={
  'reference-result':{report:null},execution:{job:null},'raw-files':{files:[]},
  results:{evaluations:[]},'reference-progress':{task_id:OLD,entries:[]},
 };
 c.api=async(path,data)=>{calls.push({path,data});return replies[path.split('/').at(-1)];};
 vm.runInContext(source.slice(source.indexOf('function researchText('),source.indexOf('function researchContent(')),c);
 vm.runInContext(source.slice(source.indexOf('function taskTagSummary('),source.indexOf('function renderExecutionControls(){')),c);
 return state;
}
function selectedCondition(value,unit=''){
 return {selected:'selected',candidates:[{id:'selected',value,unit},{id:'not-selected',value:'Unselected evidence must stay out of tags',unit:''}]};
}
test('workspace tags bound long duplicated condition excerpts without changing the complete evidence or request',async()=>{
 const excerpt='Synthetic research protocol\n  with full methods, conditions and source evidence. '.repeat(100);
 const fields={material:selectedCondition(excerpt),temperature:selectedCondition(excerpt)},original=JSON.stringify(fields);
 const {c,calls}=setupTaskTagWorkspace(fields);await c.refreshWorkspace();
 const box=c.$('#task-tags'),tags=box.children.filter(item=>item.className==='tag');assert.equal(tags.length,2);
 for(const tag of tags){assert.ok(Array.from(tag.textContent).length<=48);assert.match(tag.textContent,/…$/);assert.doesNotMatch(tag.textContent,/\n|\r|\t|Unselected evidence/);}
 assert.match(textContent(box),/完整内容见“查看你的完整研究需求”和“研究条件”/);
 assert.equal(JSON.stringify(c.current.fields),original);assert.equal(c.current.fields.material.candidates[0].value,excerpt);
 assert.equal(c.current.prompt,'Complete research request');assert.equal(calls.length,5);assert.ok(calls.every(call=>call.data===undefined));
});
test('short condition tags keep readable values and units, while missing selections clear stale tags',async()=>{
 const fields={material:selectedCondition('  Synthetic\n phase  '),temperature:selectedCondition('310','K')};
 const {c}=setupTaskTagWorkspace(fields);await c.refreshWorkspace();
 assert.deepEqual(c.$('#task-tags').children.map(item=>item.textContent),['Synthetic phase','310 K']);
 fields.material.selected=null;fields.temperature.selected='absent';await c.refreshWorkspace();
 assert.equal(c.$('#task-tags').children.length,0);
 assert.equal(fields.material.candidates[0].value,'  Synthetic\n phase  ');
});
test('condition tag limits include a long unit and preserve Unicode characters at the truncation boundary',async()=>{
 const fields={material:selectedCondition('🧪'.repeat(100)),temperature:selectedCondition('310','K\n Synthetic unit qualifier '.repeat(100))};
 const original=JSON.stringify(fields),{c}=setupTaskTagWorkspace(fields);await c.refreshWorkspace();
 const tags=c.$('#task-tags').children.filter(item=>item.className==='tag');assert.equal(tags.length,2);
 for(const tag of tags){assert.ok(Array.from(tag.textContent).length<=48);assert.match(tag.textContent,/…$/);}
 assert.ok(Array.from(tags[0].textContent).every(character=>character==='🧪'||character==='…'));
 assert.match(tags[1].textContent,/^310 K Synthetic unit qualifier/);assert.doesNotMatch(tags[1].textContent,/\n/);
 assert.equal(JSON.stringify(c.current.fields),original);
});

function selectablePaperFixture(taskId=OLD,manifest='a'.repeat(64)){
 const report=paperFixture(taskId);report.manifest_sha256=manifest;
 report.figures.push({name:'second.png',label:'Synthetic second figure',caption:'Synthetic second caption'});
 report.views.push({id:'second',title:'Synthetic second view',description:'Another synthetic quantity',figures:[{name:'second.png'}],tables:[]});
 return report;
}
function redrawPaper(state,report){
 state.c.current={id:report.task_id,mode:'research',status:'draft',paper_evidence:report};state.c.render();
 const box=state.c.$('#target-planning');
 return {tabs:findElement(box,item=>item.className==='evidence-view-tabs'),area:findElement(box,item=>item.className==='paper-evidence-content')};
}
function assertSelectedPaper(view,id,filename,taskId=OLD){
 assert.equal(view.tabs.children.find(tab=>tab.getAttribute('aria-selected')==='true').dataset.view,id);
 assert.equal(view.tabs.children.filter(tab=>tab.getAttribute('aria-selected')==='true').length,1);
 assert.equal(findElement(view.area,item=>item.tagName==='img').src,`/api/tasks/${taskId}/paper-evidence/files/${filename}`);
}
test('paper refresh keeps the selected figure for the same task and manifest, including reordered tabs',()=>{
 const state=setupAfterChange(),report=selectablePaperFixture();let view=redrawPaper(state,report);
 view.tabs.children[1].onclick();assertSelectedPaper(view,'second','second.png');
 view=redrawPaper(state,structuredClone(report));assertSelectedPaper(view,'second','second.png');
 const reordered=structuredClone(report);reordered.views.reverse();view=redrawPaper(state,reordered);
 assertSelectedPaper(view,'second','second.png');assert.equal(state.calls.length,0);
});
test('another task with the same manifest and view IDs does not inherit the previous paper tab',()=>{
 const state=setupAfterChange();let view=redrawPaper(state,selectablePaperFixture());view.tabs.children[1].onclick();
 view=redrawPaper(state,selectablePaperFixture(NEXT));assertSelectedPaper(view,'figure1','fixture.png',NEXT);
});
test('a changed evidence manifest resets the paper tab even when its view IDs remain the same',()=>{
 const state=setupAfterChange();let view=redrawPaper(state,selectablePaperFixture());view.tabs.children[1].onclick();
 view=redrawPaper(state,selectablePaperFixture(OLD,'b'.repeat(64)));assertSelectedPaper(view,'figure1','fixture.png');
});
test('removed or empty paper views cannot keep a nonexistent selected tab',()=>{
 const state=setupAfterChange(),report=selectablePaperFixture();let view=redrawPaper(state,report);view.tabs.children[1].onclick();
 const reduced=structuredClone(report);reduced.views.pop();view=redrawPaper(state,reduced);assertSelectedPaper(view,'figure1','fixture.png');
 const empty=structuredClone(report);empty.views=[];view=redrawPaper(state,empty);
 assert.equal(view.tabs.children.length,0);assert.equal(findElement(view.area,item=>item.tagName==='img'),undefined);
 view=redrawPaper(state,report);assertSelectedPaper(view,'figure1','fixture.png');
});
test('paper tab persistence requires an actual manifest identity and does not guess from its title or views',()=>{
 for(const manifest of [undefined,'not-a-digest']){
  const state=setupAfterChange(),report=selectablePaperFixture();report.manifest_sha256=manifest;
  let view=redrawPaper(state,report);view.tabs.children[1].onclick();assertSelectedPaper(view,'second','second.png');
  view=redrawPaper(state,structuredClone(report));assertSelectedPaper(view,'figure1','fixture.png');
 }
});
test('a removed tab button cannot change the latest paper selection on the next refresh',()=>{
 const state=setupAfterChange(),report=selectablePaperFixture();const old=redrawPaper(state,report),oldFirst=old.tabs.children[0];
 let view=redrawPaper(state,structuredClone(report));view.tabs.children[1].onclick();assertSelectedPaper(view,'second','second.png');
 oldFirst.onclick();assertSelectedPaper(view,'second','second.png');
 view=redrawPaper(state,structuredClone(report));assertSelectedPaper(view,'second','second.png');
});
