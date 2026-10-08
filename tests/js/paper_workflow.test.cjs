const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const TASK='a'.repeat(32),OTHER='b'.repeat(32),SHA='c'.repeat(64),MANIFEST='d'.repeat(64);
function element(tag,text='',className=''){
 return {tag,tagName:String(tag).toUpperCase(),children:[],textContent:text??'',className,hidden:false,disabled:false,value:'',dataset:{},attributes:{},parentNode:null,
  append(...items){for(const item of items){this.children.push(item);if(item&&typeof item==='object')item.parentNode=this;}},
  replaceChildren(...items){for(const item of this.children)if(item&&typeof item==='object')item.parentNode=null;this.children=[];this.textContent='';this.append(...items);},
  insertBefore(item,before){const index=this.children.indexOf(before);if(index<0)this.append(item);else{this.children.splice(index,0,item);item.parentNode=this;}},
  setAttribute(key,value){this.attributes[key]=String(value);},getAttribute(key){return this.attributes[key];},
  focus(options){this.focusOptions=options;},setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;},
  scrollIntoView(){this.scrolled=true;},get isConnected(){return Boolean(this.parentNode);},get nextSibling(){return this.parentNode?.children[this.parentNode.children.indexOf(this)+1]||null;}};
}
const text=node=>[node.textContent,...node.children.map(child=>typeof child==='object'?text(child):child)].join(' ');
const descendants=node=>[node,...node.children.filter(child=>typeof child==='object').flatMap(descendants)];
const find=(root,predicate)=>descendants(root).find(predicate);
const workflow=(task=TASK)=>({schema_version:1,task_id:task,applicable:true,state:'reference_results',paper:{title:'Synthetic reproduction paper',doi:'10.1000/example'},resources:{catalog_checked:true,source_ready:false,missing:['Original workflow recipe has not been verified'],github:{search_exhaustive:false}},evidence:{priorities:[{id:'plot',label:'Synthetic target',priority:'first'}]},actions:[
 {id:'approve_A',label:'批准作者 A',enabled:false,disabled_reason:'作者流程配方尚未核定',method:null,endpoint:null},
 {id:'start_B',label:'启动独立 B',enabled:false,disabled_reason:'尚未获准向 B 交接允许条件',method:null,endpoint:null},
 {id:'view_A',label:'查看作者 A',enabled:true,method:'GET',endpoint:`/api/tasks/${task}/reference-result`}],b_draft:{disabled_reason:'草稿仅供人类检查'}});
const report=(task=TASK)=>({task_id:task,role:'reference',title:'Synthetic author calculation',scope:'Declared scope',source_sha256:SHA,manifest_sha256:MANIFEST,output_status:'partial',output_valid:false,scientific_status:'not_evaluated',request_id:'request-1',job_id:'123456',summary:'One actual state was recovered.',limitations:['Second state is missing.'],coverage:[{label:'States',available:1,required:2,unit:'states'}],files:[{name:'states.tsv',label:'Actual states',size:12}],figures:[{name:'first.svg',label:'First state',caption:'Actual curve'},{name:'second.svg',label:'Second view',caption:'Same evidence'}],views:[{id:'first',title:'First actual view',figures:[{name:'first.svg',role:'reference'}],tables:[]},{id:'second',title:'Second actual view',figures:[{name:'second.svg',role:'reference'}],tables:[{name:'states.tsv',role:'reference',label:'Actual states',columns:[{label:'Energy',unit:'eV'}],rows:[[-1]],total_rows:1,truncated:false}]}]});
function setup(){
 const main=element('main'),taskView=element('section'),target=element('section'),ai=element('section'),calls=[],pending=[];
 target.id='target-planning';main.append(ai,target);const constants=new Map([['#target-planning',target],['#task-view',taskView],['.ai-current',ai],['#research-results',element('section')]]);
 const c=vm.createContext({Date,Promise,crypto:{randomUUID:()=> '1'.repeat(32)},current:{id:TASK,revision:1,mode:'reproduction'},
  $:selector=>constants.get(selector)||descendants(main).find(node=>node.id===selector.slice(1))||null,node:element,
  researchContent:(value,className)=>element('div',value,className),evidenceSourceHeading:role=>element('span',role),
  action:fn=>fn(),notice(){},afterChange:async()=>{},renderWorkspaceResults(){},resultTab:'overview',
  api:(path,data)=>{calls.push({path,data});return new Promise((resolve,reject)=>pending.push({path,data,resolve,reject}));},
  document:{createElement:tag=>element(tag)}});
 vm.runInContext(source.slice(source.indexOf('function paperWorkflowState(){'),source.indexOf('let targetPreviewGeneration=0;')),c);
 vm.runInContext(source.slice(source.indexOf("function evidenceDataTable(table,fileRoute='closeout'){"),source.indexOf('function renderEvidenceGallery(')),c);
 c.clearPaperWorkflowView(TASK);c.renderPaperWorkflow();
 return {c,main,taskView,calls,pending,box:()=>c.$('#paper-workflow'),reference:()=>c.$('#author-reference')};
}
function publish(s,w=workflow(),r=report(),history={task_id:TASK,source_sha256:SHA,enabled:true,messages:[]}){
 const state=s.c.paperWorkflowState();state.workflow=w;state.report=r;state.identity=s.c.authorEvidenceIdentity(r);state.discussion=history;s.c.renderPaperWorkflow();return state;
}
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
test('workbench extraction is a genuine user action with a bound P source and no B inputs',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);
 const entry=s.c.$('#workbench-entry'),load=find(entry,n=>n.tag==='button');
 const read=load.onclick();assert.equal(s.calls[0].path,`/api/tasks/${TASK}/workbench`);
 s.pending[0].resolve({task_id:TASK,enabled:true,source_sha256:SHA,messages:[],evidence:{title:'Paper',doi:'10.1000/example',items:[],visuals:[]}});await read;
 const run=find(entry,n=>n.tag==='button'&&n.textContent==='让应用 AI 提取并核验论文');assert.ok(run);
 const request=run.onclick();await flush();assert.equal(s.calls[1].path,`/api/tasks/${TASK}/workbench/extract`);
 assert.deepEqual(JSON.parse(JSON.stringify(s.calls[1].data)),{request_id:'e'.repeat(32),source_sha256:SHA});
 assert.match(text(entry),/正在提取并自动核验/);assert.equal(run.disabled,true);
 s.pending[1].resolve({state:'completed'});await flush();
 s.pending[2].resolve({task_id:TASK,enabled:true,source_sha256:SHA,messages:[{state:'completed',request_id:'e'.repeat(32)}],evidence:{title:'Paper',doi:'10.1000/example',items:[],visuals:[]}});await request;
 assert.match(text(entry),/已提取并保存/);assert.equal(find(entry,n=>n.textContent==='让应用 AI 提取并核验论文'),undefined);
 assert.equal(s.calls.some(c=>/candidates|dispatch|approve|freeze/.test(c.path)),false);
});
test('unknown workbench request stays visible and cannot expose a new paid extraction',async()=>{
 const s=setup();publish(s);const entry=s.c.$('#workbench-entry');const read=find(entry,n=>n.tag==='button').onclick();
 s.pending[0].resolve({task_id:TASK,enabled:true,source_sha256:SHA,messages:[{state:'failed_or_unknown',request_id:'e'.repeat(32)}],evidence:null});await read;
 assert.match(text(entry),/不能换编号自动重提/);
 assert.equal(find(entry,n=>n.textContent==='让应用 AI 提取并核验论文'),undefined);
 assert.equal(s.calls.length,1);
});
const workbenchRecord=(options={})=>({task_id:TASK,enabled:true,source_sha256:SHA,messages:[],evidence:null,...options});
async function readWorkbench(s,record=workbenchRecord()){
 const entry=s.c.$('#workbench-entry'),before=s.pending.length,read=entry.loadWorkbench();s.pending[before].resolve(record);await read;return entry;
}
test('same-task refresh preserves a loaded workbench and its pending source read',async()=>{
 const s=setup();publish(s);const entry=s.c.$('#workbench-entry');entry.open=true;
 const read=entry.loadWorkbench();s.c.refreshPaperWorkflow.generation++;s.c.renderPaperWorkflow();
 assert.equal(s.c.$('#workbench-entry'),entry);assert.equal(entry.open,true);
 s.pending[0].resolve(workbenchRecord());await read;
 assert.ok(find(entry,n=>n.textContent==='让应用 AI 提取并核验论文'));
 s.c.refreshPaperWorkflow.generation++;s.c.renderPaperWorkflow();
 assert.equal(s.c.$('#workbench-entry'),entry);assert.match(text(entry),/复用文献工作台/);assert.equal(s.calls.length,1);
});
test('same-task workflow refresh preserves pending extraction and displays its completed receipt',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const entry=await readWorkbench(s);
 const run=find(entry,n=>n.textContent==='让应用 AI 提取并核验论文'),request=run.onclick();await flush();
 s.c.refreshPaperWorkflow.generation++;s.c.renderPaperWorkflow();
 assert.equal(s.c.$('#workbench-entry'),entry);assert.match(text(entry),/正在提取并自动核验/);
 assert.equal(find(entry,n=>n.textContent==='让应用 AI 提取并核验论文').disabled,true);
 await run.onclick();assert.equal(s.calls.length,2);
 s.pending[1].resolve({state:'completed'});await flush();assert.equal(s.calls[2].path,`/api/tasks/${TASK}/workbench`);
 s.pending[2].resolve(workbenchRecord({messages:[{state:'completed',request_id:'e'.repeat(32)}]}));await request;
 assert.match(text(entry),/已提取并保存/);assert.doesNotMatch(text(entry),/正在提取并自动核验/);
 assert.equal(s.calls.filter(call=>call.data).length,1);
});
test('workbench extraction cannot revive an old view after A to B to A navigation',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const old=await readWorkbench(s);
 const button=find(old,n=>n.textContent==='让应用 AI 提取并核验论文'),request=button.onclick();await flush();
 s.c.current={id:OTHER,mode:'research'};s.c.clearPaperWorkflowView(OTHER);s.c.renderPaperWorkflow();
 s.c.current={id:TASK,mode:'reproduction'};s.c.clearPaperWorkflowView(TASK);publish(s);const renewed=s.c.$('#workbench-entry');
 assert.notEqual(renewed,old);s.pending[1].resolve({state:'completed'});await request;await button.onclick();
 assert.equal(s.calls.length,2);assert.doesNotMatch(text(renewed),/已提取并保存|正在提取并自动核验/);
});
test('late workbench reads and retired buttons cannot reuse an old PDF source',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const entry=await readWorkbench(s),oldButton=find(entry,n=>n.textContent==='让应用 AI 提取并核验论文');
 const oldRead=entry.loadWorkbench(),newRead=entry.loadWorkbench(),newSHA='f'.repeat(64);
 s.pending[2].resolve(workbenchRecord({source_sha256:newSHA}));await newRead;
 s.pending[1].resolve(workbenchRecord());await oldRead;await oldButton.onclick();
 assert.match(text(entry),/全文来源版本已变化/);assert.equal(s.calls.length,3);assert.equal(s.calls.filter(call=>call.data).length,0);
 const request=find(entry,n=>n.textContent==='让应用 AI 提取并核验论文').onclick();await flush();
 assert.equal(s.calls[3].data.source_sha256,newSHA);assert.equal(s.calls[3].data.force_rescan,undefined);
 s.pending[3].resolve({state:'completed'});await flush();s.pending[4].resolve(workbenchRecord({source_sha256:newSHA,messages:[{state:'completed',request_id:'e'.repeat(32)}]}));await request;
});
test('an empty saved extraction exposes an explicit accounted full rescan',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const entry=await readWorkbench(s,workbenchRecord({messages:[{state:'no_evidence_published',request_id:'old'}]}));
 assert.match(text(entry),/新增模型调用并记账/);assert.equal(s.calls.filter(call=>call.data).length,0);
 const request=find(entry,n=>n.textContent==='重新扫描全文（新增模型调用）').onclick();await flush();
 assert.deepEqual(JSON.parse(JSON.stringify(s.calls[1].data)),{request_id:'e'.repeat(32),source_sha256:SHA,force_rescan:true});
 s.pending[1].resolve({state:'completed'});await flush();s.pending[2].resolve(workbenchRecord({messages:[{state:'completed',request_id:'e'.repeat(32)}]}));await request;
});
test('a limited saved extraction exposes a separate accounted visual repair',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const entry=await readWorkbench(s,workbenchRecord({messages:[{state:'completed_with_limitations',request_id:'old'}]}));
 assert.match(text(entry),/已有证据、请求和费用保留/);assert.equal(s.calls.filter(call=>call.data).length,0);
 const request=find(entry,n=>n.textContent==='修复图表提取（新增模型调用）').onclick();await flush();
 assert.deepEqual(JSON.parse(JSON.stringify(s.calls[1].data)),{request_id:'e'.repeat(32),source_sha256:SHA,repair_visuals:true});
 s.pending[1].resolve({state:'completed'});await flush();s.pending[2].resolve(workbenchRecord({messages:[{state:'completed',request_id:'e'.repeat(32)}]}));await request;
});
test('a completed extraction with unavailable visuals still exposes explicit repair',async()=>{
 const s=setup();publish(s);const entry=await readWorkbench(s,workbenchRecord({messages:[{state:'completed',request_id:'old',visual_evidence_ready:false}]}));
 assert.ok(find(entry,n=>n.textContent==='修复图表提取（新增模型调用）'));
 assert.equal(s.calls.filter(call=>call.data).length,0);
});
test('a known failure with an accounted model request requires an explicit full rescan',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const entry=await readWorkbench(s,workbenchRecord({messages:[{state:'failed',request_id:'old',model_request_ids:['accounted-native-call']}]}));
 assert.equal(find(entry,n=>n.textContent==='让应用 AI 提取并核验论文'),undefined);
 const request=find(entry,n=>n.textContent==='重新扫描全文（新增模型调用）').onclick();await flush();
 assert.deepEqual(JSON.parse(JSON.stringify(s.calls[1].data)),{request_id:'e'.repeat(32),source_sha256:SHA,force_rescan:true});
 s.pending[1].resolve({state:'completed'});await flush();s.pending[2].resolve(workbenchRecord({messages:[{state:'completed',request_id:'e'.repeat(32)}]}));await request;
});
test('an unknown request blocks both rescan and visual repair even with older saved evidence',async()=>{
 const s=setup();publish(s);const entry=await readWorkbench(s,workbenchRecord({messages:[{state:'completed_with_limitations',request_id:'old'},{state:'unknown',request_id:'uncertain'}]}));
 assert.equal(find(entry,n=>/新增模型调用/.test(n.textContent)&&n.tag==='button'),undefined);
 assert.match(text(entry),/不能换编号自动重提/);assert.equal(s.calls.length,1);
});
test('lost POST and history replies retain the original identity and prevent another paid request',async()=>{
 const s=setup();publish(s);s.c.uid=()=> 'e'.repeat(32);const entry=await readWorkbench(s);
 const request=find(entry,n=>n.textContent==='让应用 AI 提取并核验论文').onclick();await flush();
 s.pending[1].reject(new Error('synthetic lost POST'));await flush();s.pending[2].reject(new Error('synthetic lost history'));await assert.rejects(request);
 s.c.refreshPaperWorkflow.generation++;s.c.renderPaperWorkflow();await readWorkbench(s);
 assert.match(text(entry),/不能换编号自动重提/);assert.equal(find(entry,n=>n.textContent==='让应用 AI 提取并核验论文'),undefined);
 assert.equal(s.calls.filter(call=>call.data).length,1);
});
test('paper flow shows P A B definitions and five stages without inventing execution actions',()=>{
 const s=setup();publish(s);const content=text(s.box());assert.match(content,/P · 论文原文/);assert.match(content,/A · 作者原代码/);assert.match(content,/B · 应用 AI 独立计算/);
 assert.equal(find(s.box(),node=>node.className==='paper-workflow-stages').children.length,5);
 assert.match(content,/作者流程配方尚未核定/);assert.match(content,/尚未获准向 B 交接/);
 assert.equal(descendants(s.box()).filter(node=>node.tag==='button'&&node.textContent==='批准作者 A').length,0);
 assert.equal(descendants(s.box()).filter(node=>node.tag==='button'&&node.textContent==='启动独立 B').length,0);assert.equal(s.calls.length,0);
});
test('ordinary research has no paper requirements or author analysis controls',()=>{
 const s=setup();s.c.current.mode='research';publish(s,{task_id:TASK,applicable:false},null);assert.equal(s.box().hidden,true);assert.equal(s.reference().hidden,true);assert.equal(s.reference().children.length,0);
});
test('ordinary research with real A evidence keeps a read-only comparison without reproduction gates',()=>{
 const s=setup();s.c.current.mode='research';publish(s,{task_id:TASK,applicable:false},report());assert.equal(s.box().hidden,false);assert.equal(s.reference().hidden,false);assert.match(text(s.box()),/只读对照/);assert.match(text(s.reference()),/123456/);
 assert.equal(find(s.box(),node=>node.className==='paper-workflow-stages'),undefined);assert.equal(find(s.box(),node=>node.className==='paper-workflow-actions'),undefined);assert.doesNotMatch(text(s.box()),/尚未完成资源匹配/);
});
test('A partial output keeps missing coverage, actual request, job and genuine download',()=>{
 const s=setup();publish(s);const content=text(s.reference());assert.match(content,/部分输出/);assert.match(content,/1 \/ 2 states/);assert.match(content,/Second state is missing/);assert.match(content,/request-1/);assert.match(content,/123456/);assert.doesNotMatch(content,/复现成功/);
 const download=find(s.reference(),node=>node.tag==='a'&&node.textContent==='Actual states');assert.equal(download.href,`/api/tasks/${TASK}/reference-evidence/files/states.tsv?source_sha256=${SHA}`);
 assert.equal(find(s.reference(),node=>node.tag==='img').src,`/api/tasks/${TASK}/reference-evidence/files/first.svg?source_sha256=${SHA}`);
});
test('same A source refresh retains selected view and question; new source clears question and view',()=>{
 const s=setup(),state=publish(s);find(s.reference(),node=>node.tag==='button'&&node.dataset.view==='second').onclick();const input=find(s.reference(),node=>node.tag==='textarea');input.value='Explain the actual second view';input.oninput();s.c.renderPaperWorkflow();
 assert.equal(find(s.reference(),node=>node.tag==='button'&&node.getAttribute('aria-selected')==='true').dataset.view,'second');assert.equal(find(s.reference(),node=>node.tag==='textarea').value,'Explain the actual second view');assert.match(text(s.reference()),/Energy \(eV\)/);
 assert.equal(find(s.reference(),node=>node.tag==='a'&&node.textContent==='下载完整数据表 ↓').href,`/api/tasks/${TASK}/reference-evidence/files/states.tsv?source_sha256=${SHA}`);
 const previousButton=find(s.reference(),node=>node.tag==='button'&&node.dataset.view==='second');state.report={...report(),source_sha256:'e'.repeat(64)};state.identity=s.c.authorEvidenceIdentity(state.report);state.draft='';s.c.renderPaperWorkflow();previousButton.onclick();
 assert.equal(find(s.reference(),node=>node.tag==='button'&&node.getAttribute('aria-selected')==='true').dataset.view,'first');assert.equal(find(s.reference(),node=>node.tag==='textarea').value,'');
});
test('a report with mismatched task, source or role cannot offer author files or analysis',()=>{
 for(const invalid of [{...report(),task_id:OTHER},{...report(),source_sha256:'invalid'},{...report(),role:'agent'}]){const s=setup();publish(s,workflow(),invalid);assert.match(text(s.reference()),/尚无可核验/);assert.equal(find(s.reference(),node=>node.tag==='form'),undefined);assert.equal(find(s.reference(),node=>node.tag==='a'),undefined);}
});
test('only explicitly enabled same-task workflow routes expose an operation',()=>{
 const s=setup(),w=workflow();w.actions=[{id:'check_resources',label:'检查资源',enabled:true,method:'POST',endpoint:'/api/tasks/'+OTHER+'/paper-workflow/resources'},{id:'check_resources',label:'错误动词',enabled:true,method:'GET',endpoint:'/api/tasks/'+TASK+'/paper-workflow/resources'},{id:'unknown',label:'任意动作',enabled:true,method:'POST',endpoint:'/arbitrary'}];publish(s,w);
 const operations=find(s.box(),node=>node.className==='paper-workflow-actions');
 assert.equal(descendants(operations).filter(node=>node.tag==='button').length,0);assert.equal(s.calls.length,0);
});
test('late workflow reads do not replace a new task and closing task does not redraw',async()=>{
 for(const leave of ['switch','close']){const s=setup(),read=s.c.refreshPaperWorkflow();if(leave==='switch'){s.c.current={id:OTHER,mode:'reproduction'};s.c.clearPaperWorkflowView(OTHER);}else s.taskView.hidden=true;
  s.pending[0].resolve(workflow());s.pending[1].resolve({report:report()});await read;assert.equal(s.c.paperWorkflowState().report,null);assert.doesNotMatch(text(s.reference()),/123456/);assert.equal(s.pending.length,2);}
});
test('A to B to A rejects the first A generation despite matching task id',async()=>{
 const s=setup(),old=s.c.refreshPaperWorkflow();s.c.current={id:OTHER,mode:'reproduction'};s.c.clearPaperWorkflowView(OTHER);s.c.current={id:TASK,mode:'reproduction'};s.c.clearPaperWorkflowView(TASK);
 s.pending[0].resolve(workflow());s.pending[1].resolve({report:report()});await old;assert.equal(s.c.paperWorkflowState().report,null);assert.equal(s.pending.length,2);
});
test('changed source while author history is pending cannot inject old answers',async()=>{
 const s=setup(),state=publish(s),read=s.c.refreshAuthorDiscussion(TASK,state.identity);state.report={...report(),source_sha256:'e'.repeat(64)};state.identity=s.c.authorEvidenceIdentity(state.report);state.discussion=null;s.c.renderPaperWorkflow();
 s.pending[0].resolve({task_id:TASK,source_sha256:SHA,enabled:true,messages:[{question:'old',answer:'old answer',source_sha256:SHA}]});await read;assert.equal(state.discussion,null);assert.doesNotMatch(text(s.reference()),/old answer/);
});
test('author question goes only to independent reference discussion with source identity',async()=>{
 const s=setup(),state=publish(s),input=find(s.reference(),node=>node.tag==='textarea');input.value='Explain actual A curve';input.oninput();const submit=find(s.reference(),node=>node.tag==='form').onsubmit({preventDefault(){}});await flush();
 assert.equal(s.pending[0].path,'/api/model-preference');s.pending[0].resolve({provider:'deepseek-official'});await flush();const posted=s.pending[1];assert.equal(posted.path,`/api/tasks/${TASK}/reference-discussion`);assert.equal(posted.data.source_sha256,SHA);assert.equal(posted.data.question,'Explain actual A curve');
 posted.resolve({state:'completed'});await flush();s.pending[2].resolve({task_id:TASK,source_sha256:SHA,enabled:true,messages:[{question:posted.data.question,answer:'Independent author answer',source_sha256:SHA}]});await submit;
 assert.equal(state.request,null);assert.equal(state.draft,'');assert.match(text(s.reference()),/Independent author answer/);assert.ok(s.calls.every(call=>!call.path.endsWith('/discussion')));
});
test('uncertain author POST retains request identity and prevents another paid call',async()=>{
 const s=setup(),state=publish(s),input=find(s.reference(),node=>node.tag==='textarea');input.value='Explain A';input.oninput();const form=find(s.reference(),node=>node.tag==='form'),submit=form.onsubmit({preventDefault(){}});await flush();s.pending[0].resolve({provider:'deepseek-official'});await flush();s.pending[1].reject(new Error('offline after send'));await submit;assert.ok(state.request);assert.equal(state.request.source_sha256,SHA);
 await form.onsubmit({preventDefault(){}});assert.equal(s.pending.length,2);s.c.renderPaperWorkflow();assert.equal(find(s.reference(),node=>node.type==='submit').disabled,true);assert.match(text(s.reference()),/不会|不重复/);
});
test('same-task source version refresh clears old question through production reads',async()=>{
 const s=setup(),state=publish(s);state.draft='Old version question';const read=s.c.refreshPaperWorkflow();s.pending[0].resolve(workflow());s.pending[1].resolve({report:{...report(),source_sha256:'e'.repeat(64)}});await flush();
 assert.equal(state.draft,'');assert.equal(state.report.source_sha256,'e'.repeat(64));assert.equal(s.pending[2].path,`/api/tasks/${TASK}/reference-discussion`);
 s.pending[2].resolve({task_id:TASK,source_sha256:'e'.repeat(64),enabled:false,messages:[],disabled_reason:'Analysis connection is unavailable'});await read;assert.match(text(s.reference()),/Analysis connection is unavailable/);assert.doesNotMatch(text(s.reference()),/Old version question/);
});
test('reference read failures retain prior evidence and do not claim scientific failure',async()=>{
 const s=setup(),state=publish(s),before=state.report,read=s.c.refreshPaperWorkflow();s.pending[0].reject(new Error('offline'));s.pending[1].reject(new Error('offline'));await flush();s.pending[2].resolve({task_id:TASK,source_sha256:SHA,enabled:false,messages:[]});await read;
 assert.equal(state.report,before);assert.match(text(s.box()),/保留上次已读取内容/);assert.match(text(s.reference()),/123456/);assert.doesNotMatch(text(s.box()),/计算失败$/);
});
test('unknown author history is blocked after reload and completed read reconciles original identity',async()=>{
 const s=setup(),state=publish(s),first=s.c.refreshAuthorDiscussion(TASK,state.identity);s.pending[0].resolve({task_id:TASK,source_sha256:SHA,enabled:true,messages:[{id:'saved-analysis',state:'unknown',question:'Existing question'}]});await first;
 assert.equal(state.request.id,'saved-analysis');assert.equal(find(s.reference(),node=>node.type==='submit').disabled,true);assert.match(text(s.reference()),/请求状态待核对/);
 const second=s.c.refreshAuthorDiscussion(TASK,state.identity);s.pending[1].resolve({task_id:TASK,source_sha256:SHA,enabled:true,messages:[{id:'saved-analysis',state:'completed',question:'Existing question',answer:'Recovered answer',usage:{total_tokens:24}}]});await second;
 assert.equal(state.request,null);assert.equal(find(s.reference(),node=>node.type==='submit').disabled,false);assert.match(text(s.reference()),/Recovered answer/);assert.match(text(s.reference()),/已记录模型用量：24/);assert.ok(s.calls.every(call=>call.data===undefined));
});
test('resource action uses the exact real endpoint and current revision, without a simulation request',async()=>{
 const s=setup(),w=workflow();w.actions=[{id:'check_resources',label:'核对登记资源',enabled:true,method:'POST',endpoint:`/api/tasks/${TASK}/paper-workflow/resources`}];publish(s,w);const run=find(s.box(),node=>node.tag==='button'&&node.textContent==='核对登记资源').onclick();await flush();
 assert.equal(s.pending[0].path,`/api/tasks/${TASK}/paper-workflow/resources`);assert.equal(s.pending[0].data.revision,1);s.pending[0].resolve(workflow());await flush();s.pending[1].resolve(workflow());s.pending[2].resolve({report:report()});await flush();s.pending[3].resolve({task_id:TASK,source_sha256:SHA,enabled:true,messages:[]});await run;
 assert.ok(s.calls.every(call=>!/(submit|prepare|candidate|generate)/.test(call.path)));
});
test('same-source A refresh preserves the active question cursor without scrolling',()=>{
 const s=setup();publish(s);const input=find(s.reference(),node=>node.tag==='textarea');input.value='Explain actual results';input.oninput();input.selectionStart=3;input.selectionEnd=8;s.c.document.activeElement=input;s.c.renderPaperWorkflow();
 const renewed=find(s.reference(),node=>node.tag==='textarea');assert.notEqual(renewed,input);assert.equal(renewed.value,'Explain actual results');assert.equal(renewed.selectionStart,3);assert.equal(renewed.selectionEnd,8);assert.equal(renewed.focusOptions.preventScroll,true);
});
test('B condition draft preview remains human-only and never creates or submits a candidate',async()=>{
 const s=setup(),w=workflow();w.actions=[{id:'preview_B_draft',label:'查看 B 条件草稿',enabled:true,method:'GET',endpoint:`/api/tasks/${TASK}/paper-workflow/b-draft`}];publish(s,w);const run=find(s.box(),node=>node.tag==='button'&&node.textContent==='查看 B 条件草稿').onclick();await flush();s.pending[0].resolve({task_id:TASK,role:'human_only_unreleased_condition_draft',released:false,draft:{task_text:'Synthetic allowed conditions'},disabled_reason:'Not released'});await run;
 assert.match(text(s.box()),/Synthetic allowed conditions/);assert.match(text(s.box()),/尚未交给 B/);assert.match(text(s.box()),/Not released/);assert.equal(s.calls.length,1);assert.equal(s.calls[0].data,undefined);
});
test('late model preference read cannot send an old question after A to B to A navigation',async()=>{
 const s=setup();publish(s);const input=find(s.reference(),node=>node.tag==='textarea');input.value='Old A question';input.oninput();const run=find(s.reference(),node=>node.tag==='form').onsubmit({preventDefault(){}});await flush();s.c.current={id:OTHER,mode:'research'};s.c.clearPaperWorkflowView(OTHER);s.c.current={id:TASK,mode:'reproduction'};s.c.clearPaperWorkflowView(TASK);publish(s);
 s.pending[0].resolve({provider:'deepseek-official'});await run;assert.equal(s.calls.length,1);assert.equal(s.c.paperWorkflowState().request,null);assert.equal(s.c.paperWorkflowState().draft,'');
});
test('completed author POST answer stays visible if the following history read fails',async()=>{
 const s=setup();publish(s);const input=find(s.reference(),node=>node.tag==='textarea');input.value='Explain A';input.oninput();const run=find(s.reference(),node=>node.tag==='form').onsubmit({preventDefault(){}});await flush();s.pending[0].resolve({provider:'deepseek-official'});await flush();const posted=s.pending[1];posted.resolve({id:posted.data.request_id,state:'completed',question:posted.data.question,answer:'Actual received synthetic reply'});await flush();s.pending[2].reject(new Error('offline reading saved history'));await run;
 assert.match(text(s.reference()),/Actual received synthetic reply/);assert.match(text(s.reference()),/保留已收到的答复/);assert.equal(s.c.paperWorkflowState().request,null);assert.equal(find(s.reference(),node=>node.type==='submit').disabled,true);assert.equal(s.calls.length,3);
});
test('enabled freeze uses the existing revision endpoint; disabled historical freeze has no button',async()=>{
 const s=setup(),w=workflow();w.actions=[{id:'freeze_targets',label:'确认范围',enabled:true,method:'POST',endpoint:`/api/tasks/${TASK}/freeze`}];publish(s,w);const run=find(s.box(),node=>node.tag==='button'&&node.textContent==='确认范围').onclick();await flush();assert.equal(s.pending[0].path,`/api/tasks/${TASK}/freeze`);assert.equal(s.pending[0].data.revision,1);s.pending[0].resolve({...s.c.current,status:'conditions_frozen',revision:2});await run;assert.equal(s.c.current.revision,2);assert.equal(s.calls.length,1);
 w.actions[0].enabled=false;w.actions[0].disabled_reason='已有运行，不能补作事前冻结';publish(s,w);assert.equal(find(s.box(),node=>node.tag==='button'&&node.textContent==='确认范围'),undefined);assert.match(text(s.box()),/不能补作事前冻结/);
});
