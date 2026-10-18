const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const code=source.slice(source.indexOf('async function renderReferenceTargetAnalysis('),source.indexOf('function renderPaperEvidence('));
const ID='a'.repeat(32),SHA='b'.repeat(64),BUTTON='让应用 AI 核定全部 P 目标与共享计算依赖';
function fixture(history=[],unknown_reissues=[]){
 const calls=[];
 function node(tag,value='',className=''){return {tag,textContent:value,className,children:[],isConnected:true,disabled:false,append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}};}
 const panel=node('section');
 const c={node,researchContent:v=>node('div',v),current:{id:ID},renderPaperEvidence:{generation:1},crypto:{randomUUID:()=> 'cccccccc-cccc-cccc-cccc-cccccccccccc'},api:async(path,data)=>{calls.push({path,data});return {task_id:ID,history,unknown_reissues};}};
 vm.createContext(c);vm.runInContext(code,c);
 const all=()=>{function walk(item){return [item,...item.children.flatMap(v=>walk(v))];}return walk(panel);};
 return {c,panel,calls,all,render:(options)=>c.renderReferenceTargetAnalysis(panel,{task_id:ID},ID+':'+SHA,options)};
}
test('opening P reads target history without generating, selecting or submitting',async()=>{
 const f=fixture();await f.render();assert.equal(f.calls.length,1);assert.equal(f.calls[0].data,undefined);
 assert.equal(f.calls[0].path,`/api/tasks/${ID}/reference-target-analysis`);
});
test('normal click requests reference analysis using only task route and request ID',async()=>{
 const f=fixture();await f.render();await f.all().find(x=>x.textContent===BUTTON).onclick();
 const post=f.calls.find(x=>x.data);assert.equal(post.path,`/api/tasks/${ID}/reference-target-analysis`);
 assert.deepEqual(Object.keys(post.data),['request_id']);assert.equal(post.data.request_id,'c'.repeat(32));
});
test('prepared dispatching and uncertain requests block another paid analysis',async()=>{
 for(const state of ['prepared','dispatching','unknown']){const f=fixture([{request_id:'d'.repeat(32),state}]);await f.render();const run=f.all().find(x=>x.textContent===BUTTON);assert.equal(run.disabled,true);await run.onclick();assert.equal(f.calls.filter(x=>x.data).length,0);}
});
test('three suggestions preserve cap on rerender and do not reset failures',async()=>{
 const f=fixture(Array.from({length:3},()=>({state:'invalid_output',request_id:'d'.repeat(32)})));await f.render();await f.render();
 assert.ok(f.all().filter(x=>x.textContent===BUTTON).every(x=>x.disabled));assert.equal(f.calls.filter(x=>x.data).length,0);
});
test('reviewing a saved answer sends no new generation and preserves its parent identity',async()=>{
 const parent='d'.repeat(32),f=fixture([{state:'invalid_output',request_id:parent}]);await f.render();
 assert.equal(f.calls.filter(x=>x.data).length,0);
 await f.all().find(x=>x.textContent==='重新核验这份已保存答复（不调用模型）').onclick();
 const post=f.calls.find(x=>x.data);assert.equal(post.path,`/api/tasks/${ID}/reference-target-analysis/${parent}/review`);
 assert.deepEqual(Object.keys(post.data),[]);
});
test('delayed history cannot populate another task',async()=>{
 const f=fixture();let resolve;f.c.api=()=>new Promise(r=>resolve=r);const pending=f.render();f.c.current.id='e'.repeat(32);resolve({task_id:ID,history:[]});await pending;
 assert.equal(f.all().some(x=>x.textContent===BUTTON),false);
});
test('an appended failed review is not offered as a fresh zero-call repair again',async()=>{
 const f=fixture([{state:'invalid_output',request_id:'d'.repeat(32),reviews:[{state:'unavailable',message:'引用仍不匹配'}]}]);await f.render();
 assert.equal(f.all().some(x=>x.textContent==='重新核验这份已保存答复（不调用模型）'),false);
 assert.equal(f.calls.filter(x=>x.data).length,0);
});
test('full suggested targets and shared dependencies remain visible with exact citations',async()=>{
 const report={inventory:{targets:[{id:'one',label:'Table 1'},{id:'two',label:'Fig. 3'}]},targets:[{target_id:'one',priority:1,data_class:'raw_lammps',reason:'原文计算',dependency_ids:['shared'],missing:[],citations:[{source_locator:'PDF page 2',quote:'Exact source'}]},{target_id:'two',priority:2,data_class:'postprocessing',reason:'共用原始输出',dependency_ids:['shared'],missing:['方法待核验'],citations:[]}],dependencies:[{id:'shared',label:'共用计算',description:'一次输出支持两项目标',data_class:'raw_lammps',requires:[],missing:[],citations:[]}],limitations:['保留全部目标，尚未选定范围']};
 const f=fixture([{state:'completed',request_id:'d'.repeat(32),report}]);await f.render();const texts=f.all().map(x=>x.textContent);
 assert.ok(texts.some(x=>String(x).includes('Table 1')));assert.ok(texts.some(x=>String(x).includes('Fig. 3')));assert.ok(texts.includes('Exact source'));assert.ok(texts.includes('shared · 共用计算'));
 assert.equal(f.calls.filter(x=>x.data).length,0);
});
test('unknown recovery requires both explicit acknowledgments and sends only the original parent identity',async()=>{
 const parent='d'.repeat(32),f=fixture([{state:'unknown',request_id:parent}],
  [{unknown_reissue_of:parent,enabled:true}]);await f.render();
 const restore=f.all().find(x=>x.textContent==='按原内容恢复这次目标核定（一次）');
 assert.equal(restore.disabled,true);await restore.onclick();assert.equal(f.calls.filter(x=>x.data).length,0);
 const inputs=f.all().filter(x=>x.tag==='input');assert.equal(inputs.length,2);
 inputs[0].checked=true;inputs[0].onchange();assert.equal(restore.disabled,true);
 inputs[1].checked=true;inputs[1].onchange();assert.equal(restore.disabled,false);
 await restore.onclick();const post=f.calls.find(x=>x.data);
 assert.equal(post.path,`/api/tasks/${ID}/reference-target-analysis/unknown-reissue`);
 assert.deepEqual(JSON.parse(JSON.stringify(post.data)),{request_id:'c'.repeat(32),unknown_reissue_of:parent,
  supplier_checked:true,retain_unknown_charge:true});
 assert.equal(post.data.messages,undefined);assert.equal(post.data.sources,undefined);
});
test('disabled recovery never exposes a paid action and resolved unknown preserves history without blocking forever',async()=>{
 const parent='d'.repeat(32),f=fixture([{state:'unknown',request_id:parent,blocking:false}],
  [{unknown_reissue_of:parent,enabled:false,reason:'原请求已有一次恢复'}]);await f.render();
 assert.equal(f.all().some(x=>x.textContent==='按原内容恢复这次目标核定（一次）'),false);
 assert.equal(f.all().find(x=>x.textContent===BUTTON).disabled,false);
 assert.equal(f.calls.filter(x=>x.data).length,0);
});
test('stale inventory preserves original recovery controls but prohibits a fresh target generation',async()=>{
 const parent='d'.repeat(32),f=fixture([{state:'unknown',request_id:parent}],
  [{unknown_reissue_of:parent,enabled:true}]);await f.render({historyOnly:true});
 assert.equal(f.all().find(x=>x.textContent===BUTTON).disabled,true);
 assert.ok(f.all().some(x=>x.textContent==='按原内容恢复这次目标核定（一次）'));
 assert.equal(f.calls.filter(x=>x.data).length,0);
});
test('known failure can revise its full original proposal once within the same three-round cap',async()=>{
 const parent='d'.repeat(32),record={state:'unavailable',request_id:parent,original_source_preserved:true,
  receipt:{model_receipt:{state:'completed'},message:'Exact citation mismatch'}};
 const f=fixture([{state:'unknown',request_id:'e'.repeat(32)},record]);await f.render({historyOnly:true});
 const revise=f.all().find(x=>x.textContent==='基于完整前稿修订原核定（消耗下一轮）');assert.ok(revise);
 await revise.onclick();const post=f.calls.find(x=>x.data);
 assert.equal(post.path,`/api/tasks/${ID}/reference-target-analysis/${parent}/revise-original`);
 assert.deepEqual(Object.keys(post.data),['request_id']);
 const capped=fixture([{state:'unknown'},record,{...record,request_id:'f'.repeat(32)}]);await capped.render({historyOnly:true});
 assert.equal(capped.all().some(x=>x.textContent==='基于完整前稿修订原核定（消耗下一轮）'),false);
});
