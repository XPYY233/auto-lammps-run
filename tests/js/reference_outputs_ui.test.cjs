const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('async function renderAuthorRawOutputs('),source.indexOf('async function refreshAuthorDiscussion('));
function setup(api){
 const node=(tag,text)=>({tag,textContent:text||'',children:[],isConnected:true,append(...items){this.children.push(...items)},setAttribute(){},remove(){this.isConnected=false;}});
 const root=node('section');const context=vm.createContext({node,current:{id:'task'},renderAuthorEvidence:{generation:1},api,fileSize:String,$:()=>({scrollIntoView(){}}),refreshWorkspace:async()=>{}});
 vm.runInContext(code,context);return {root,context,all:function all(n=root){return [n,...n.children.flatMap(child=>all(child))]}};
}
const available={task_id:'task',configured:true,runs:[{request_id:'saved-request',job_id:'123',total_bytes:12,files:[{name:'raw-values.txt',size:12}],state:'available'}]};
test('raw retrieval sends only the fixed request and ignores double click while busy',async()=>{
 let finish,posts=0;const pending=new Promise(resolve=>finish=resolve);
 const r=setup(async(url,body)=>{if(!body)return available;posts++;assert.equal(url,'/api/tasks/task/reference-outputs');assert.deepEqual(JSON.parse(JSON.stringify(body)),{request_id:'saved-request'});return pending;});
 await r.context.renderAuthorRawOutputs(r.root,'task',1);
 const button=r.all().find(n=>n.tag==='button');const first=button.onclick();await button.onclick();
 assert.equal(posts,1);assert.equal(button.disabled,true);finish(available);await first;assert.equal(button.disabled,false);
});
test('delayed data from an old task never creates retrieval controls in the new task',async()=>{
 let finish;const pending=new Promise(resolve=>finish=resolve);const r=setup(()=>pending);
 const call=r.context.renderAuthorRawOutputs(r.root,'task',1);r.context.current.id='another';finish(available);await call;
 assert.equal(r.all().filter(n=>n.tag==='button').length,0);
});
test('retained raw output directs to download without another collection action',async()=>{
 const retained=JSON.parse(JSON.stringify(available));retained.runs[0].state='retained';
 const r=setup(async()=>retained);await r.context.renderAuthorRawOutputs(r.root,'task',1);
 const text=r.all().map(n=>n.textContent).join('\n');assert.match(text,/原始输出已回收/);assert.match(text,/分析尚未因此完成/);
 assert.equal(r.all().filter(n=>n.tag==='button'&&n.textContent==='回收作者 A 原始输出').length,0);
});
