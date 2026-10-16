const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const TASK='a'.repeat(32),OTHER='b'.repeat(32),REQUEST='c'.repeat(32);
function production(from,to){
 const start=source.indexOf(from),end=source.indexOf(to,start+from.length);
 assert.ok(start>=0&&end>start,`Missing production section: ${from}`);
 return source.slice(start,end);
}
const code=[
 production('function paperWorkflowCurrent(', 'async function refreshPaperWorkflow('),
 production('function paperWorkflowAction(', 'function renderAuthorEvidence('),
 production('const authorPlanStates=', 'const outputAnalysisStates=')
].join('\n');

function element(tag,text=''){
 return {tag,textContent:text??'',children:[],dataset:{},attributes:{},hidden:false,disabled:false,value:'',
  append(...items){for(const item of items){this.children.push(item);item.parentNode=this;}},
  replaceChildren(...items){this.children=[];this.textContent='';this.append(...items);},
  setAttribute(key,value){this.attributes[key]=String(value);},
  scrollIntoView(){this.scrolled=true;}};
}
const all=root=>[root,...root.children.flatMap(all)];
const text=root=>all(root).map(item=>item.textContent).join('\n');
const find=(root,predicate)=>all(root).find(predicate);
const plain=value=>JSON.parse(JSON.stringify(value));
const available=(overrides={})=>({task_id:TASK,can_prepare:true,used_rounds:0,max_rounds:3,history:[],...overrides});
const saved=(overrides={})=>({request_id:REQUEST,round:1,max_rounds:3,state:'preparation_paused',revision_note:'Original revision\nwith its exact wording',recoverable:true,events:[],receipt:null,report:null,...overrides});

// Only HTTP timing and the DOM surface are synthetic; request and rendering
// decisions execute the application functions above without copying their logic.
function setup(){
 const root=element('main'),anchor=element('section'),taskView=element('section'),calls=[];
 anchor.id='author-reference';root.append(anchor);let uuidCalls=0;
 const c=vm.createContext({current:{id:TASK,revision:7,mode:'reproduction'},node:element,
  $:selector=>selector==='#task-view'?taskView:find(root,item=>item.id===selector.slice(1)),
  api:(path,body)=>new Promise((resolve,reject)=>calls.push({path,body,resolve,reject})),
  action:fn=>fn(),researchContent:value=>element('div',value),
  refreshPaperWorkflow:Object.assign(async()=>{throw new Error('Unexpected workflow mutation');},{generation:1}),
  crypto:{randomUUID:()=>{uuidCalls++;return 'dddddddd-dddd-dddd-dddd-dddddddddddd';}}});
 vm.runInContext(code,c);
 return {c,root,anchor,taskView,calls,panel:()=>find(root,item=>item.id==='author-plan-panel'),uuidCalls:()=>uuidCalls};
}
async function open(s,data=available(),id=TASK){
 const opening=s.c.renderAuthorPlanPanel(id);
 s.calls.at(-1).resolve(data);await opening;return s.panel();
}
const button=(s,label)=>find(s.panel(),item=>item.tag==='button'&&item.textContent===label);
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
async function finishPost(s,posting,data){
 const call=s.calls.at(-1);assert.notEqual(call.body,undefined);call.resolve({state:'preparation_paused'});
 await flush();const refresh=s.calls.at(-1);assert.equal(refresh.body,undefined);refresh.resolve(data);await posting;
}

test('the paper author entry and history refresh perform reads without a scientific action',async()=>{
 const s=setup();
 const row=s.c.paperWorkflowAction({id:'prepare_A_plan',label:'Prepare author workflow',enabled:true,method:'GET',endpoint:`/api/tasks/${TASK}/author-plan`},{});
 assert.equal(s.calls.length,0);
 const opening=find(row,item=>item.tag==='button').onclick();
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].path,`/api/tasks/${TASK}/author-plan`);assert.equal(s.calls[0].body,undefined);
 s.calls[0].resolve(available());await opening;
 const refreshing=button(s,'刷新作者方案记录').onclick();s.calls[1].resolve(available());await refreshing;
 assert.equal(s.calls.length,2);assert.ok(s.calls.every(call=>call.path===`/api/tasks/${TASK}/author-plan`&&call.body===undefined));
 assert.equal(s.uuidCalls(),0);assert.match(text(s.panel()),/此步骤不会提交计算/);
});

test('an author entry with another task endpoint or a mutation method is not actionable',()=>{
 const s=setup();
 for(const bad of [{method:'GET',endpoint:`/api/tasks/${OTHER}/author-plan`},{method:'POST',endpoint:`/api/tasks/${TASK}/author-plan`}]){
  const row=s.c.paperWorkflowAction({id:'prepare_A_plan',label:'Prepare',enabled:true,...bad},{});
  assert.equal(find(row,item=>item.tag==='button'),undefined);
 }
 assert.equal(s.calls.length,0);
});

test('explicit preparation posts only its request identity and revision note and blocks double clicks',async()=>{
 const s=setup();await open(s);
 const note=find(s.panel(),item=>item.tag==='textarea');note.value='Retain the original entry and show missing evidence.';note.oninput();
 const prepare=button(s,'让应用 AI 准备作者运行方案');const posting=prepare.onclick();await prepare.onclick();
 assert.equal(s.calls.length,2);assert.equal(s.calls[1].path,`/api/tasks/${TASK}/author-plan`);
 assert.deepEqual(plain(s.calls[1].body),{request_id:'d'.repeat(32),revision_note:note.value});
 await finishPost(s,posting,available({can_prepare:false,used_rounds:1,history:[saved({request_id:'d'.repeat(32),recoverable:false,state:'unknown'})]}));
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);
 assert.equal(button(s,'正在准备作者方案…').disabled,true);
 assert.equal(button(s,'恢复原作者方案请求'),undefined);
});

test('recovery uses the saved request and exact saved note rather than the edited revision box',async()=>{
 const s=setup(),record=saved();await open(s,available({used_rounds:1,history:[record]}));
 const note=find(s.panel(),item=>item.tag==='textarea');note.value='A different new paid revision';note.oninput();
 const posting=button(s,'恢复原作者方案请求').onclick();
 assert.deepEqual(plain(s.calls[1].body),{request_id:REQUEST,revision_note:record.revision_note});
 assert.equal(s.uuidCalls(),0);
 await finishPost(s,posting,available({used_rounds:1,history:[saved({state:'prepared',recoverable:false,receipt:{message:'Saved original response',usage:{total_tokens:23}}})]}));
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);assert.match(text(s.panel()),/模型用量 23/);
});

test('the full retained model proposal remains inspectable even when its third draft failed',async()=>{
 const proposal={summary:'A retained invalid synthetic draft',invocations:[{id:'entry',argv:['original-entry','unchanged-argument']}],unaccepted_detail:{full_text:'Synthetic text '.repeat(1600)+'END_OF_FULL_REPLY'}};
 const s=setup();await open(s,available({can_prepare:false,used_rounds:3,history:[saved({round:3,state:'failed',recoverable:false,proposal,receipt:{message:'Draft validation failed',usage:{total_tokens:57}}})]}));
 const raw=find(s.panel(),item=>item.tag==='details'&&text(item).includes('查看 AI 原始完整答复'));
 assert.ok(raw);assert.equal(find(raw,item=>item.tag==='pre').textContent,JSON.stringify(proposal,null,2));
 assert.match(text(s.panel()),/第 3\/3 版/);assert.equal(button(s,'基于上一版修订作者方案').disabled,true);
 assert.equal(s.calls.length,1);
});

test('a late author history response cannot overwrite the current different task',async()=>{
 const s=setup(),old=s.c.renderAuthorPlanPanel(TASK);
 s.c.current.id=OTHER;const current=s.c.renderAuthorPlanPanel(OTHER);
 s.calls[1].resolve(available({task_id:OTHER,history:[saved({report:{summary:'CURRENT_TASK_ONLY'}})]}));await current;
 const before=text(s.panel());
 s.calls[0].resolve(available({history:[saved({report:{summary:'STALE_AUTHOR_REPLY'}})]}));await old;
 assert.equal(text(s.panel()),before);assert.match(before,/CURRENT_TASK_ONLY/);assert.doesNotMatch(before,/STALE_AUTHOR_REPLY/);
 assert.equal(s.panel().dataset.task,OTHER);
});

test('a completed old task POST neither refreshes nor publishes over a newly opened task',async()=>{
 const s=setup();await open(s);const posting=button(s,'让应用 AI 准备作者运行方案').onclick();
 s.c.current.id=OTHER;const current=s.c.renderAuthorPlanPanel(OTHER);
 s.calls[2].resolve(available({task_id:OTHER,disabled_reason:'CURRENT_TASK_STATUS'}));await current;
 const before=text(s.panel());s.calls[1].resolve({report:{summary:'OLD_POST_RESULT'}});await posting;
 assert.equal(s.calls.length,3);assert.equal(text(s.panel()),before);assert.match(before,/CURRENT_TASK_STATUS/);
 assert.equal(s.panel().dataset.task,OTHER);
});

test('an older same-task history read cannot replace a newer completed read',async()=>{
 const s=setup(),old=s.c.renderAuthorPlanPanel(TASK),newer=s.c.renderAuthorPlanPanel(TASK);
 s.calls[1].resolve(available({history:[saved({report:{summary:'NEWER_SAVED_HISTORY'}})]}));await newer;
 const before=text(s.panel());s.calls[0].resolve(available({history:[saved({report:{summary:'OLDER_SAVED_HISTORY'}})]}));await old;
 assert.equal(text(s.panel()),before);assert.match(before,/NEWER_SAVED_HISTORY/);assert.doesNotMatch(before,/OLDER_SAVED_HISTORY/);
});
