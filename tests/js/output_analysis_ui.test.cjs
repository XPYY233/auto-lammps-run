const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('const outputAnalysisStates='),source.indexOf('async function refreshAuthorDiscussion('));
function setup(api){
 function node(tag,text){return {tag,textContent:text||'',children:[],dataset:{},style:{},value:'',hidden:false,
  append(...items){for(const item of items){this.children.push(item);item.parentElement=this;if(this.tag==='select'&&!this.value)this.value=item.value;}},
  replaceChildren(...items){this.children=[];this.append(...items)},setAttribute(name,value){this[name]=value}};}
 const root=node('section'),anchor=node('div'),taskView=node('section');anchor.id='research-results';root.append(anchor);
 function all(n=root){return [n,...n.children.flatMap(child=>all(child))]}
 const context=vm.createContext({node,api,current:{id:'task'},crypto:{randomUUID:()=> 'a'.repeat(32)},
  $:selector=>selector==='#task-view'?taskView:all().find(el=>el.id===selector.slice(1)),
  Option:function(label,value){return {tag:'option',textContent:label,value,children:[]}},researchContent:s=>node('div',s)});
 vm.runInContext(code,context);return {context,root,all};
}
const available={enabled:true,task_id:'task',runs:[{request_id:'source-request',job_id:'123',role:'reference',files:[{name:'raw.txt',size:4}],total_bytes:4}],history:[]};
test('opening raw analysis is read only; explicit click sends identifiers and question only',async()=>{
 let data=structuredClone(available),posts=[];
 const r=setup(async(url,body)=>{if(!body)return data;posts.push({url,body});data.history=[{request_id:'a'.repeat(32),state:'unknown',role:'reference',round:1,maximum_rounds:3,question:body.question,message:'unknown'}];return data.history[0]});
 await r.context.renderOutputAnalysisPanel('task');assert.equal(posts.length,0);
 const input=r.all().find(el=>el.tag==='textarea');input.value='核对已有数值并画分布';input.oninput();
 await r.all().find(el=>el.tag==='button'&&el.textContent==='让应用 AI 分析原始数据').onclick();
 assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/tasks/task/output-analyses');
 assert.deepEqual(JSON.parse(JSON.stringify(posts[0].body)),{request_id:'a'.repeat(32),source_request_id:'source-request',question:'核对已有数值并画分布'});
 assert.match(r.all().map(n=>n.textContent).join('\n'),/unknown/);
});
test('double click while the same request is in flight never dispatches a second model action',async()=>{
 let finish,posts=0;const waiting=new Promise(resolve=>finish=resolve);
 const r=setup(async(url,body)=>{if(!body)return available;posts++;return waiting});
 await r.context.renderOutputAnalysisPanel('task');const input=r.all().find(el=>el.tag==='textarea');input.value='分析';
 const run=r.all().find(el=>el.tag==='button'&&el.textContent==='让应用 AI 分析原始数据');
 const pending=run.onclick();await run.onclick();assert.equal(posts,1);finish({state:'unknown'});await pending;
 assert.equal(r.context.outputAnalysisStates,undefined); // lexical private state is not a window API
 assert.equal(r.all().find(el=>el.tag==='button'&&/正在分析/.test(el.textContent)).disabled,true);
});
test('a delayed old task read cannot add controls to another task',async()=>{
 let finish;const r=setup(()=>new Promise(resolve=>finish=resolve));
 const pending=r.context.renderOutputAnalysisPanel('task');r.context.current.id='other';finish(available);await pending;
 assert.equal(r.all().filter(el=>el.tag==='button').length,0);
});
test('known unsaved preflight rejection clears the local pending control without auto retry',async()=>{
 let posts=0;const r=setup(async(url,body)=>{if(!body)return available;posts++;const error=new Error('not sent');error.requestRecorded=false;throw error;});
 await r.context.renderOutputAnalysisPanel('task');r.all().find(el=>el.tag==='textarea').value='分析';
 await r.all().find(el=>el.tag==='button'&&el.textContent==='让应用 AI 分析原始数据').onclick();
 assert.equal(posts,1);assert.equal(r.all().find(el=>el.tag==='button'&&el.textContent==='让应用 AI 分析原始数据').disabled,false);
});
test('completed numerical results expose real figures and full downloads without scientific pass',async()=>{
 const data=structuredClone(available);data.history=[{request_id:'b'.repeat(32),role:'reference',job_id:'123',round:1,maximum_rounds:3,state:'completed',question:'histogram',message:'已保存分析，科学未核验',
  proposal:{summary:'已声明方法',limitations:['部分输出']},result:{figures:{figures:[{name:'plot-hist.png',operation_id:'hist'}]},numeric_results:[{method:'summary',table:'raw',column:'v',value_unit:'1',values:{count:2,mean:2,min:1,max:3}}],files:[{name:'plot-hist.png'},{name:'numeric-report.json'}]}}];
 const r=setup(async()=>data);await r.context.renderOutputAnalysisPanel('task');
 const img=r.all().find(el=>el.tag==='img');assert.match(img.src,/output-analyses\/b+\/files\/plot-hist.png$/);
 assert.match(img.alt,/作者 A 作业 123/);assert.ok(r.all().some(el=>el.tag==='a'&&el.textContent==='numeric-report.json'));
 const download=r.all().find(el=>el.tag==='a'&&el.textContent==='numeric-report.json');
 assert.equal(download.download,'numeric-report.json');assert.match(download.href,/\?download=true$/);
 assert.match(r.all().map(n=>n.textContent).join('\n'),/科学复现仍待/);
});
test('technical recovery reuses the original request and question instead of creating a paid revision',async()=>{
 const data=structuredClone(available),posts=[];
 data.history=[{request_id:'b'.repeat(32),source_request_id:'source-request',job_id:'123',role:'reference',round:1,maximum_rounds:3,
  state:'analysis_execution_failed',recoverable:true,question:'原问题',message:'原答复保留'}];
 const r=setup(async(url,body)=>{if(!body)return data;posts.push(body);data.history[0].recoverable=false;data.history[0].state='completed';return data.history[0]});
 await r.context.renderOutputAnalysisPanel('task');
 await r.all().find(el=>el.tag==='button'&&el.textContent==='恢复原分析执行（不重新调用模型）').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(posts)),[{request_id:'b'.repeat(32),source_request_id:'source-request',question:'原问题'}]);
});
