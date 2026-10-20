const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
function setup(){
 const calls=[],nodes=[],state={},parent={dataset:{task:'t'},append(x){this.child=x}};
 const node=(tag,text)=>{const n={tag,textContent:text||'',children:[],isConnected:true,dataset:{},setAttribute(k,v){this[k]=v},append(...x){this.children.push(...x)}};nodes.push(n);return n;};
 const c=vm.createContext({current:{id:'t'},node,$:()=>({hidden:false}),crypto:{randomUUID:()=> 'b'.repeat(32)},
  api:async(path,body)=>{calls.push({path,body});if(!body)return {parent_scope_id:'a'.repeat(32),task_revision:9};
   if(path.endsWith('/preview'))return {contract:{unit_count:body.unit_count},validation_sha256:'c'.repeat(64)};return {};},
  renderAuthorPlanPanel:async()=>{}});
 vm.runInContext(source.slice(source.indexOf('async function renderAuthorValidationPanel('),source.indexOf('async function renderAuthorExecutionPanel(')),c);
 return {c,calls,nodes,state,parent};
}
test('bounded A preview and save use explicit current intent without a science or HPC endpoint',async()=>{
 const s=setup();await s.c.renderAuthorValidationPanel('t',s.parent,s.state);
 assert.equal(s.calls.length,1);assert.equal(s.calls[0].body,undefined);
 const find=text=>s.nodes.find(n=>n.textContent===text);
 const count=s.nodes.find(n=>n['aria-label']==='作者 A 验证状态数');count.value='10';count.oninput();
 const note=s.nodes.find(n=>n.tag==='textarea');note.value='Ten states only; retain original P and B.';note.oninput();
 await find('预览作者 A 验证子集').onclick();
 assert.equal(s.calls[1].body.unit_count,10);
 await find('确认保存此验证子集（不提交计算）').onclick();
 assert.equal(s.calls[2].body.revision,9);assert.equal(s.calls[2].body.expected_validation_sha256,'c'.repeat(64));
 assert.ok(s.calls.every(x=>x.path.includes('/author-validation')));
});
test('changing preview inputs disables saving an earlier scope digest',async()=>{
 const s=setup();await s.c.renderAuthorValidationPanel('t',s.parent,s.state);
 const button=text=>s.nodes.find(n=>n.textContent===text);
 const count=s.nodes.find(n=>n['aria-label']==='作者 A 验证状态数');count.value='10';count.oninput();
 const note=s.nodes.find(n=>n.tag==='textarea');note.value='Explicit development validation.';note.oninput();
 await button('预览作者 A 验证子集').onclick();count.value='11';count.oninput();
 assert.equal(button('确认保存此验证子集（不提交计算）').disabled,true);
 await button('确认保存此验证子集（不提交计算）').onclick();assert.equal(s.calls.length,2);
});
