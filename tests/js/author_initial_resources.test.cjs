const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const start=source.indexOf('async function renderAuthorResourcePanel(');
const stop=source.indexOf('const outputAnalysisStates=',start);
const TASK='a'.repeat(32),REQUEST='b'.repeat(32),RECEIPT='c'.repeat(64);
function element(tag,text=''){
 return {tag,textContent:text??'',children:[],dataset:{},attributes:{},hidden:false,disabled:false,
  append(...items){for(const item of items){this.children.push(item);item.parentNode=this;}},
  replaceChildren(...items){this.children=[];this.textContent='';this.append(...items);},
  setAttribute(key,value){this.attributes[key]=String(value);},
  querySelectorAll(tag){return all(this).filter(item=>item!==this&&item.tag===tag);}};
}
const all=root=>[root,...root.children.flatMap(all)];
const find=(root,predicate)=>all(root).find(predicate);
const text=root=>all(root).map(item=>item.textContent).join('\n');
const plain=value=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function setup(){
 const root=element('main'),parent=element('section'),taskView=element('section'),state={},calls=[];
 parent.dataset.task=TASK;root.append(parent);
 const context=vm.createContext({current:{id:TASK},node:element,
  $:selector=>selector==='#task-view'?taskView:find(root,item=>item.id===selector.slice(1)),
  api:(path,body)=>new Promise((resolve,reject)=>calls.push({path,body,resolve,reject})),
  crypto:{randomUUID:()=>REQUEST}});
 vm.runInContext(source.slice(start,stop),context);
 return {context,root,parent,state,calls,panel:()=>find(root,item=>item.id==='author-resource-panel')};
}
function status(latest=null){return {enabled:false,latest:null,initial:{enabled:true,latest,
 reading_approved:false,receipt_sha256:latest?.state==='metadata_ready'?RECEIPT:null,missing:[]}};}
function metadata(){return {request_id:REQUEST,state:'metadata_ready',repository:'owner/fixed',commit:'d'.repeat(40),
 tree_entries:42,recoverable:false,license_evidence:{files:[{path:'LICENSE',sha256:'e'.repeat(64),text:'<literal license>\nResearch reading only.'}]}};}
async function open(s,data){const loading=s.context.renderAuthorResourcePanel(TASK,s.parent,s.state);s.calls.at(-1).resolve(data);await loading;}
const button=(s,label)=>find(s.panel(),item=>item.tag==='button'&&item.textContent===label);

test('first ordinary resource view has explicit tree inspection and never downloads the whole repository',async()=>{
 const s=setup();await open(s,status());
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].body,undefined);
 assert.equal(button(s,'获取固定作者仓库到 HPC'),undefined);
 const inspect=button(s,'核对固定仓库清单与许可证'),pending=inspect.onclick();await inspect.onclick();
 assert.equal(s.calls.length,2);assert.equal(s.calls[1].path,`/api/tasks/${TASK}/author-resources/bootstrap`);
 assert.deepEqual(plain(s.calls[1].body),{request_id:REQUEST});
 s.calls[1].resolve({});await flush();s.calls.at(-1).resolve(status(metadata()));await pending;
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);
 assert.match(text(s.panel()),/不是源码已获取或计算就绪/);
});

test('literal license and reading-only confirmation bind the displayed receipt, without execution approval',async()=>{
 const s=setup();await open(s,status(metadata()));
 const pre=find(s.panel(),item=>item.tag==='pre');assert.equal(pre.textContent,'<literal license>\nResearch reading only.');
 const approve=button(s,'确认阅读用途'),check=find(s.panel(),item=>item.tag==='input');
 assert.equal(approve.disabled,true);await approve.onclick();assert.equal(s.calls.length,1);
 check.checked=true;check.onchange();assert.equal(approve.disabled,false);
 const pending=approve.onclick();assert.equal(s.calls.at(-1).path,`/api/tasks/${TASK}/author-resources/bootstrap/read-approval`);
 assert.equal(s.calls.at(-1).body.receipt_sha256,RECEIPT);
 assert.equal(s.calls.at(-1).body.request_id,REQUEST);
 assert.match(s.calls.at(-1).body.note,/不批准执行/);
 assert.equal('execution_authorized' in s.calls.at(-1).body,false);
 s.calls.at(-1).resolve({});await flush();s.calls.at(-1).resolve(status(metadata()));await pending;
});

test('missing literal license or receipt cannot enable the reading confirmation',async()=>{
 for(const which of ['license','receipt']){
  const s=setup(),data=status(metadata());
  if(which==='license')data.initial.latest.license_evidence.files=[];else data.initial.receipt_sha256=null;
  await open(s,data);const check=find(s.panel(),item=>item.tag==='input');
  assert.equal(check.disabled,true);check.checked=true;check.onchange();assert.equal(button(s,'确认阅读用途').disabled,true);
  await button(s,'确认阅读用途').onclick();assert.equal(s.calls.length,1);
 }
});

test('unknown initial request only reconciles its original identity, never starts new metadata retrieval',async()=>{
 const s=setup(),latest={...metadata(),state:'unknown',recoverable:true};await open(s,status(latest));
 assert.equal(button(s,'核对固定仓库清单与许可证'),undefined);
 const pending=button(s,'核对原清单请求（不重新获取）').onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:REQUEST});
 assert.equal(s.calls.at(-1).path,`/api/tasks/${TASK}/author-resources/bootstrap/reconcile`);
 s.calls.at(-1).resolve({});await flush();s.calls.at(-1).resolve(status(latest));await pending;
});

test('old-task mutation response cannot refresh the newly selected task',async()=>{
 const s=setup();await open(s,status());const pending=button(s,'核对固定仓库清单与许可证').onclick();
 s.context.current.id='f'.repeat(32);s.parent.dataset.task=s.context.current.id;
 const count=s.calls.length;s.calls.at(-1).resolve({});await pending;assert.equal(s.calls.length,count);
});

test('reading confirmation exposes existing bounded application AI selection rather than starting a paid call on GET',async()=>{
 const s=setup(),data=status(metadata());data.initial.reading_approved=true;
 const pending=s.context.renderAuthorResourcePanel(TASK,s.parent,s.state);s.calls.at(-1).resolve(data);await flush();
 assert.equal(s.calls.length,2);assert.equal(s.calls.at(-1).path,`/api/tasks/${TASK}/author-resources/selections`);
 assert.equal(s.calls.at(-1).body,undefined);
 s.calls.at(-1).resolve({used_rounds:0,max_rounds:3,remaining_rounds:3,can_prepare:true,history:[],acquisitions:{history:[]}});await pending;
 assert.match(text(s.panel()),/原件选择 0\/3 版/);assert.ok(button(s,'让应用 AI 选择需补齐的原件'));
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,0);
});
