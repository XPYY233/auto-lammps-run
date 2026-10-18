const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const start=source.indexOf('async function renderAuthorResourceSelections(');
const stop=source.indexOf('const outputAnalysisStates=',start);
assert.ok(start>=0&&stop>start);
const code=source.slice(start,stop),TASK='a'.repeat(32),OTHER='b'.repeat(32);
const REQUEST='7'.repeat(32),NEXT='8'.repeat(32),OPERATION='c'.repeat(32);
const SELECT='让应用 AI 选择需补齐的原件',FETCH='把 AI 所选原件获取到 HPC';
function element(tag,text=''){
 return {tag,textContent:text??'',children:[],dataset:{},attributes:{},hidden:false,disabled:false,value:'',
  append(...items){for(const item of items){this.children.push(item);item.parentNode=this;}},
  replaceChildren(...items){this.children=[];this.textContent='';this.append(...items);},
  setAttribute(key,value){this.attributes[key]=String(value);}};
}
const all=root=>[root,...root.children.flatMap(all)];
const text=root=>all(root).map(item=>item.textContent).join('\n');
const find=(root,predicate)=>all(root).find(predicate);
const plain=value=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function selected(overrides={}){
 return {request_id:REQUEST,round:1,state:'selected',recoverable:false,revision_note:'',
  receipt:{message:'Synthetic suggestion; no execution.'},report:{summary:'SYNTHETIC_SELECTION',
   selections:[{path:'scripts/helper.py',size:200,reason:'Synthetic missing companion.'}],
   missing:['Original environment remains unverified.'],limitations:['Not scientific validation.']},...overrides};
}
function status(overrides={}){
 return {task_id:TASK,used_rounds:0,max_rounds:3,remaining_rounds:3,can_prepare:true,
  pending_reconciliation:false,disabled_reason:'',history:[],latest:null,
  acquisitions:{history:[],latest:null},...overrides};
}
function setup(){
 const root=element('main'),parent=element('section'),taskView=element('section'),state={},calls=[];
 let uuidCalls=0;parent.dataset.task=TASK;root.append(parent);
 const context=vm.createContext({current:{id:TASK},node:element,
  $:selector=>selector==='#task-view'?taskView:find(root,item=>item.id===selector.slice(1)),
  api:(path,body)=>new Promise((resolve,reject)=>calls.push({path,body,resolve,reject})),
  crypto:{randomUUID:()=>{uuidCalls++;return 'dddddddd-dddd-dddd-dddd-dddddddddddd';}}});
 vm.runInContext(code,context);
 return {context,root,parent,taskView,state,calls,uuids:()=>uuidCalls,
  panel:()=>find(root,item=>item.id==='author-resource-selections')};
}
async function open(s,data=status(),id=TASK,state=s.state){
 const pending=s.context.renderAuthorResourceSelections(id,s.parent,state);
 s.calls.at(-1).resolve(data);await pending;
}
const button=(s,label)=>find(s.panel(),item=>item.tag==='button'&&item.textContent===label);
async function finish(s,pending,postResponse,nextStatus){
 s.calls.at(-1).resolve(postResponse);await flush();
 assert.equal(s.calls.at(-1).body,undefined);
 s.calls.at(-1).resolve(nextStatus);await pending;
}

test('ordinary opening reads only status, displays independent rounds and does not choose or fetch',async()=>{
 const s=setup();await open(s);
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].body,undefined);
 assert.equal(s.calls[0].path,`/api/tasks/${TASK}/author-resources/selections`);
 assert.match(text(s.panel()),/原件选择 0\/3 版/);assert.match(text(s.panel()),/作者运行方案轮次另行保留/);
 assert.match(text(s.panel()),/不代表科学验证/);assert.equal(s.uuids(),0);
 assert.equal(button(s,FETCH),undefined);
});

test('selection submits only request ID and note once, without paths, repo or licensing inputs',async()=>{
 const s=setup();await open(s);
 const area=find(s.panel(),item=>item.tag==='textarea');area.value='Preserve the complete original scope.';area.oninput();
 const prepare=button(s,SELECT),pending=prepare.onclick();await prepare.onclick();
 assert.equal(s.calls.length,2);assert.equal(prepare.disabled,true);assert.equal(s.uuids(),1);
 assert.equal(s.calls[1].path,`/api/tasks/${TASK}/author-resources/selections`);
 assert.deepEqual(plain(s.calls[1].body),{request_id:'d'.repeat(32),revision_note:area.value});
 const row=selected({request_id:'d'.repeat(32),revision_note:area.value});
 await finish(s,pending,row,status({used_rounds:1,remaining_rounds:2,history:[row],latest:row}));
 assert.equal(s.state.selectionRequest,null);assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);
 assert.ok(s.calls.every(call=>!call.path.endsWith('/author-plan')));
});

test('complete prior selection records remain visible and three selection rounds disable a new call',async()=>{
 const s=setup(),rows=[1,2,3].map(round=>selected({round,request_id:String(round).repeat(32),
  state:'invalid_output',report:null,revision:{complete_previous_answer:{full:'FULL_PRIOR_REPLY_'+round}}}));
 await open(s,status({used_rounds:3,remaining_rounds:0,can_prepare:false,history:rows,latest:rows.at(-1)}));
 assert.equal(button(s,SELECT).disabled,true);assert.equal(button(s,FETCH),undefined);
 assert.match(text(s.panel()),/原件选择 3\/3 版/);
 for(const row of rows)assert.ok(find(s.panel(),item=>item.tag==='pre'&&item.textContent.includes(row.revision.complete_previous_answer.full)));
 assert.equal(s.calls.length,1);assert.equal(s.uuids(),0);
});

test('a known unknown model call does not expose another paid call or a fetch',async()=>{
 const s=setup(),row=selected({state:'unknown',report:null,receipt:{message:'Original accounted request is unknown.'}});
 await open(s,status({used_rounds:1,history:[row],latest:row,can_prepare:false,pending_reconciliation:true}));
 assert.equal(button(s,SELECT).disabled,true);assert.equal(button(s,FETCH),undefined);
 assert.match(text(s.panel()),/Original accounted request is unknown/);assert.equal(s.calls.length,1);
});

test('original empty note recovery ignores newly typed note and never invents a new request ID',async()=>{
 const s=setup(),row=selected({state:'preparation_paused',report:null,receipt:null,recoverable:true,revision_note:''});
 s.state.selectionNote='New text must not overwrite the frozen empty note.';
 await open(s,status({used_rounds:1,history:[row],latest:row,can_prepare:false,pending_reconciliation:true}));
 const pending=button(s,'核对原件选择原请求').onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:REQUEST,revision_note:''});assert.equal(s.uuids(),0);
 const restored=selected();await finish(s,pending,restored,status({used_rounds:1,history:[restored],latest:restored}));
});

test('recoverable selection with a frozen nonempty note keeps its exact original ID and note',async()=>{
 const s=setup(),row=selected({state:'preparation_paused',report:null,receipt:null,recoverable:true,
  revision_note:'Frozen exact original note.'});s.state.selectionNote='Different current edit.';
 await open(s,status({used_rounds:1,history:[row],latest:row,can_prepare:false,pending_reconciliation:true}));
 const pending=button(s,'核对原件选择原请求').onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:REQUEST,revision_note:row.revision_note});
 const restored=selected();await finish(s,pending,restored,status({used_rounds:1,history:[restored],latest:restored}));
 assert.equal(s.uuids(),0);
});

test('fetch sends only saved selected request and new operation IDs, never developer chosen paths',async()=>{
 const s=setup(),row=selected();await open(s,status({used_rounds:1,history:[row],latest:row}));
 const fetch=button(s,FETCH),pending=fetch.onclick();await fetch.onclick();
 assert.equal(fetch.disabled,true);assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);
 assert.equal(s.calls.at(-1).path,`/api/tasks/${TASK}/author-resources/selected-files`);
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:'d'.repeat(32),selection_request_id:REQUEST});
 const acquisition={operation_id:'d'.repeat(32),selection_request_id:REQUEST,state:'selected_complete',
  recoverable:false,report:{files:[{path:'scripts/helper.py',size:200,sha256:'f'.repeat(64)}]}};
 await finish(s,pending,{latest:acquisition},status({used_rounds:1,history:[row],latest:row,
  acquisitions:{history:[acquisition],latest:acquisition}}));
 assert.equal(button(s,FETCH).disabled,true);assert.match(text(s.panel()),/不代表整仓完整、运行就绪或科学通过/);
 assert.match(text(s.panel()),/SHA-256/);assert.equal(s.state.selectedFetchRequest,null);
});

test('unknown earlier fetch keeps its own matching operation and selection even if latest AI selection differs',async()=>{
 const s=setup(),earlier=selected(),latest=selected({request_id:NEXT,round:2});
 const unknown={operation_id:OPERATION,selection_request_id:REQUEST,state:'unknown',recoverable:true,report:{}};
 await open(s,status({used_rounds:2,history:[earlier,latest],latest,
  acquisitions:{history:[unknown],latest:unknown}}));
 const pending=button(s,'只核对原所选原件获取请求').onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:OPERATION,selection_request_id:REQUEST});
 assert.equal(s.uuids(),0);
 const done={...unknown,state:'selected_complete',recoverable:false};
 await finish(s,pending,{latest:done},status({used_rounds:2,history:[earlier,latest],latest,
  acquisitions:{history:[done],latest:done}}));
 assert.equal(button(s,FETCH).disabled,false);
});

test('switching tasks while a selection reply arrives cannot replace or refresh the new page',async()=>{
 const s=setup();await open(s);const pending=button(s,SELECT).onclick();
 const oldPost=s.calls.at(-1);s.context.current.id=OTHER;s.parent.dataset.task=OTHER;
 const row=selected({report:{summary:'CURRENT_OTHER_TASK',selections:[],missing:[],limitations:[]}});
 await open(s,status({task_id:OTHER,history:[row],latest:row}),OTHER,{});
 const content=text(s.panel()),count=s.calls.length;
 oldPost.resolve(selected());await pending;
 assert.equal(s.calls.length,count);assert.equal(text(s.panel()),content);
 assert.match(content,/CURRENT_OTHER_TASK/);assert.doesNotMatch(content,/SYNTHETIC_SELECTION/);
});

test('switching tasks while an old fetch returns cannot publish old files into the new page',async()=>{
 const s=setup(),row=selected();await open(s,status({used_rounds:1,history:[row],latest:row}));
 const pending=button(s,FETCH).onclick(),oldPost=s.calls.at(-1);
 s.context.current.id=OTHER;s.parent.dataset.task=OTHER;
 await open(s,status({task_id:OTHER,disabled_reason:'CURRENT_TASK_ONLY',can_prepare:false}),OTHER,{});
 const content=text(s.panel()),count=s.calls.length;
 oldPost.resolve({latest:{state:'selected_complete',report:{files:[{path:'OLD_TASK_FILE'}]}}});await pending;
 assert.equal(s.calls.length,count);assert.equal(text(s.panel()),content);
 assert.match(content,/CURRENT_TASK_ONLY/);assert.doesNotMatch(content,/OLD_TASK_FILE/);
});

test('an older same-task GET cannot overwrite newer selection status',async()=>{
 const s=setup(),older=s.context.renderAuthorResourceSelections(TASK,s.parent,s.state);
 const newer=s.context.renderAuthorResourceSelections(TASK,s.parent,s.state);
 const row=selected();s.calls[1].resolve(status({used_rounds:1,history:[row],latest:row}));await newer;
 const content=text(s.panel());s.calls[0].resolve(status({disabled_reason:'STALE_STATUS'}));await older;
 assert.equal(text(s.panel()),content);assert.doesNotMatch(text(s.panel()),/STALE_STATUS/);
});

test('hidden or detached task controls cannot start selection or acquisition',async()=>{
 const s=setup(),row=selected();await open(s,status({used_rounds:1,history:[row],latest:row}));
 s.taskView.hidden=true;await button(s,SELECT).onclick();await button(s,FETCH).onclick();
 s.taskView.hidden=false;s.parent.dataset.task=OTHER;
 await button(s,SELECT).onclick();await button(s,FETCH).onclick();
 assert.equal(s.calls.length,1);assert.equal(s.uuids(),0);
});

test('validated nonempty clarified selection offers fetching while keeping all unresolved needs',async()=>{
 const s=setup(),row=selected({state:'needs_clarification'});
 await open(s,status({used_rounds:1,history:[row],latest:row}));
 assert.ok(button(s,FETCH));assert.match(text(s.panel()),/其他缺项仍待澄清/);
 assert.match(text(s.panel()),/Original environment remains unverified/);
 const pending=button(s,FETCH).onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:'d'.repeat(32),selection_request_id:REQUEST});
 await finish(s,pending,{latest:null},status({used_rounds:1,history:[row],latest:row}));
});
test('empty clarified and invalid nonempty selections never offer fetching',async()=>{
 for(const row of [selected({state:'needs_clarification',report:{selections:[],missing:['Still unresolved.']}}),selected({state:'invalid_output'})]){
  const s=setup();await open(s,status({used_rounds:1,history:[row],latest:row}));assert.equal(button(s,FETCH),undefined);
 }
});

test('known partial fetch continues missing files under the original operation without another selection',async()=>{
 const s=setup(),row=selected(),partial={operation_id:OPERATION,selection_request_id:REQUEST,
  state:'partial',recoverable:false,resumable:true,resume_reason:'Original files and cumulative budgets are retained.',
  report:{files:[{path:'scripts/helper.py',size:200,sha256:'f'.repeat(64)}],failures:[]}};
 await open(s,status({used_rounds:1,history:[row],latest:row,acquisitions:{history:[partial],latest:partial}}));
 const pending=button(s,'继续原请求未完成的原件').onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:OPERATION,selection_request_id:REQUEST});
 assert.equal(s.calls.at(-1).path,`/api/tasks/${TASK}/author-resources/selected-files`);assert.equal(s.uuids(),0);
 const done={...partial,state:'selected_complete',resumable:false,selected_complete:true};
 await finish(s,pending,{latest:done},status({used_rounds:1,history:[row],latest:row,acquisitions:{history:[partial,done],latest:done}}));
 assert.equal(button(s,'继续原请求未完成的原件'),undefined);
});
test('exhausted partial does not create another fetch with a new identity',async()=>{
 const s=setup(),row=selected(),partial={operation_id:OPERATION,selection_request_id:REQUEST,
 state:'partial',recoverable:false,resumable:false,resume_reason:'Original cumulative transfer budget is exhausted.',report:{}};
 await open(s,status({used_rounds:1,history:[row],latest:row,acquisitions:{history:[partial],latest:partial}}));
 assert.equal(button(s,'继续原请求未完成的原件'),undefined);assert.equal(button(s,FETCH).disabled,true);
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,0);assert.equal(s.uuids(),0);
});
