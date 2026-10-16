const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const start=source.indexOf('async function renderAuthorResourcePanel(');
const stop=source.indexOf('const outputAnalysisStates=',start);
assert.ok(start>=0&&stop>start);
const code=source.slice(start,stop),TASK='a'.repeat(32),OTHER='b'.repeat(32),REQUEST='c'.repeat(32);
const LABEL='核对原有获取记录具体缺项（不重新下载）';
function element(tag,text=''){
 return {tag,textContent:text??'',children:[],dataset:{},attributes:{},hidden:false,disabled:false,
  append(...items){for(const item of items){this.children.push(item);item.parentNode=this;}},
  replaceChildren(...items){this.children=[];this.textContent='';this.append(...items);},
  setAttribute(key,value){this.attributes[key]=String(value);}};
}
const all=root=>[root,...root.children.flatMap(all)];
const text=root=>all(root).map(item=>item.textContent).join('\n');
const find=(root,predicate)=>all(root).find(predicate);
const plain=value=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function status(overrides={}){
 const latest={request_id:REQUEST,state:'partial',repository:'example/fixed',commit:'f'.repeat(40),
  files:[],source_complete:false,recoverable:false,can_inspect_tree:true,
  failures:[{reason:'unsupported_source_member'}],inspection:null,...overrides};
 return {task_id:TASK,enabled:true,latest,history:[latest]};
}
function inspected(){
 return status({inspection:{inspection_id:'d'.repeat(32),state:'inspected',
  message:'已核对原缓存成员元数据；未重新下载、读取源码或确认可执行。',report:{
   state:'inspected',scope:'reference_only',B_input:false,execution_authorized:false,
   physical_evaluation_performed:false,summary:{counts:{supported_blob:1,directory:1}},entries:[
    {path:'data/too-large.dat',status:'oversized_blob',type:'blob',mode:'100644',size:20000000},
    {path:'scripts',status:'directory',type:'tree',mode:'040000',size:null},
    {path:'linked',status:'nonregular_member',type:'blob',mode:'120000',size:12},
    {path:'<untrusted>.py',status:'supported_blob',type:'blob',mode:'100644',size:17}]}}});
}
function setup(){
 const root=element('main'),parent=element('section'),taskView=element('section'),state={},calls=[];
 parent.dataset.task=TASK;root.append(parent);
 const context=vm.createContext({current:{id:TASK},node:element,
  $:selector=>selector==='#task-view'?taskView:find(root,item=>item.id===selector.slice(1)),
  api:(path,body)=>new Promise((resolve,reject)=>calls.push({path,body,resolve,reject})),
  crypto:{randomUUID:()=>{throw new Error('Inspection cannot invent an acquisition identity');}}});
 vm.runInContext(code,context);
 return {context,root,parent,state,calls,panel:()=>find(root,item=>item.id==='author-resource-panel')};
}
async function open(s,data=status(),id=TASK){
 const loading=s.context.renderAuthorResourcePanel(id,s.parent,s.state);
 s.calls.at(-1).resolve(data);await loading;
}
const button=(s,label)=>find(s.panel(),item=>item.tag==='button'&&item.textContent===label);

test('opening cached inventory is local status and its explicit button sends only the original identity once',async()=>{
 const s=setup();await open(s);
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].body,undefined);
 assert.equal(s.calls[0].path,`/api/tasks/${TASK}/author-resources`);
 const inspect=button(s,LABEL),checking=inspect.onclick();await inspect.onclick();
 await button(s,'获取固定作者仓库到 HPC').onclick();
 assert.equal(s.calls.length,2);assert.equal(inspect.disabled,true);
 assert.equal(s.calls[1].path,`/api/tasks/${TASK}/author-resources/inspect-tree`);
 assert.deepEqual(plain(s.calls[1].body),{request_id:REQUEST});
 s.calls[1].resolve(inspected());await flush();s.calls.at(-1).resolve(inspected());await checking;
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);
 assert.ok(s.calls.every(call=>!call.path.includes('author-plan')));
});

test('specific member limits and provenance are visible without declaring acquired source or executable readiness',async()=>{
 const s=setup();await open(s,inspected());
 const content=text(s.panel());
 assert.match(content,/data\/too-large\.dat/);assert.match(content,/超过单文件读取上限/);
 assert.match(content,/链接或子模块等非普通成员/);assert.match(content,/原件尚未完整/);
 assert.match(content,/尚未确认源码完整或运行就绪/);assert.match(content,/<untrusted>\.py/);
 assert.ok(find(s.panel(),item=>item.tag==='pre'&&item.textContent.includes('"B_input": false')));
 assert.equal(button(s,'读取补充作者原件（不调用模型）'),undefined);
 assert.equal(s.calls.length,1);
});

test('unknown inspection offers the same original record while preserving original partial retrieval diagnostics',async()=>{
 const s=setup();await open(s,status({inspection:{state:'unknown',message:'缓存清单尚未核对',report:{state:'unknown'}}}));
 assert.match(text(s.panel()),/原缓存清单尚未核对成功/);
 assert.match(text(s.panel()),/unsupported_source_member/);
 const checking=button(s,LABEL).onclick();
 assert.deepEqual(plain(s.calls.at(-1).body),{request_id:REQUEST});
 s.calls.at(-1).resolve(status());await flush();s.calls.at(-1).resolve(status());await checking;
 assert.equal(s.calls.filter(call=>call.body!==undefined).length,1);
});

test('a completed old task inspection cannot refresh or publish over a different current task',async()=>{
 const s=setup();await open(s);const checking=button(s,LABEL).onclick();
 s.context.current.id=OTHER;s.parent.dataset.task=OTHER;
 await open(s,status({repository:'CURRENT_TASK_ONLY'}),OTHER);
 const content=text(s.panel()),count=s.calls.length;
 s.calls[1].resolve(inspected());await checking;
 assert.equal(s.calls.length,count);assert.equal(text(s.panel()),content);
 assert.match(content,/CURRENT_TASK_ONLY/);assert.doesNotMatch(content,/too-large/);
});

test('an older same-task status read cannot replace a newer cached inspection view',async()=>{
 const s=setup(),older=s.context.renderAuthorResourcePanel(TASK,s.parent,s.state);
 const newer=s.context.renderAuthorResourcePanel(TASK,s.parent,s.state);
 s.calls[1].resolve(inspected());await newer;const content=text(s.panel());
 s.calls[0].resolve(status({repository:'STALE_SOURCE_ONLY'}));await older;
 assert.equal(text(s.panel()),content);assert.doesNotMatch(text(s.panel()),/STALE_SOURCE_ONLY/);
});

test('unknown acquisition has no cached-tree button and disabled saved bindings cannot enable inspection',async()=>{
 const s=setup();await open(s,status({state:'unknown',recoverable:true,can_inspect_tree:false}));
 assert.equal(button(s,LABEL),undefined);assert.ok(button(s,'核对原资料获取请求'));
 const disabled=status();disabled.enabled=false;await open(s,disabled);
 assert.equal(button(s,LABEL).disabled,true);assert.equal(s.calls.filter(call=>call.body!==undefined).length,0);
});
