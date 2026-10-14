const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const block=source.slice(source.indexOf('let paperRecommendationScope=null;'),source.indexOf('async function refreshPapers()'));
function setup(history=[],status){
 const elements=new Map(), calls=[],saved=new Map([['auto-lammps-paper-recommendation-scope','a'.repeat(32)]]);
 function node(tag,text='',className=''){
  const item={tag,textContent:text,className,children:[],isConnected:true,disabled:false,hidden:false,attributes:{},append(...v){this.children.push(...v);},replaceChildren(...v){this.children=v;},setAttribute(k,v){this.attributes[k]=v;}};
  Object.defineProperty(item,'id',{set(v){this._id=v;elements.set(v,this);},get(){return this._id;}});return item;
 }
 const view=node('section');view.id='papers-view';
 const sandbox={node,$:s=>elements.get(s.slice(1)),localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},crypto:{randomUUID:()=> 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'},
  paperOverviewCatalog:{result:{papers:[{selection:'selected',doi:'10.1234/already'},{selection:'candidate',doi:'10.1234/unselected'}]}},
  action:fn=>fn(),api:async(url,data)=>{calls.push({url,data});return {history,status};}};
 vm.createContext(sandbox);vm.runInContext(block,sandbox);
 const walk=item=>[item,...item.children.flatMap(child=>typeof child==='object'?walk(child):[])];
 return {sandbox,calls,saved,view,elements,all:()=>walk(view)};
}
test('opening recommendations reads history only and retains original scope',async()=>{
 const f=setup();await f.sandbox.appendPaperRecommendations();
 assert.equal(f.calls.length,1);assert.equal(f.calls[0].url,'/api/paper-recommendations/'+'a'.repeat(32));assert.equal(f.calls[0].data,undefined);
 assert.equal(f.sandbox.recommendationScope(),'a'.repeat(32));
});
test('explicit click sends only question and fixed exclusions, never source or P/B answers',async()=>{
 const f=setup();await f.sandbox.appendPaperRecommendations();
 f.elements.get('paper-recommendation-question').value='请推荐有完整资源的下一篇';
 await f.all().find(e=>e.tag==='button'&&e.textContent==='让应用 AI 推荐下一篇').onclick();
 const post=f.calls.find(c=>c.data);assert.ok(post);assert.equal(post.url,'/api/paper-recommendations');
 assert.equal(post.data.scope_id,'a'.repeat(32));assert.equal(post.data.request_id,'b'.repeat(32));
 assert.deepEqual(Array.from(post.data.exclude_dois),['10.1234/already']);
 assert.deepEqual(Object.keys(post.data).sort(),['exclude_dois','question','request_id','scope_id']);
});
test('unknown model request blocks new calls while history remains visible',async()=>{
 const f=setup([{state:'failed_or_unknown',question:'Original request'}]);await f.sandbox.appendPaperRecommendations();
 const run=f.all().find(e=>e.tag==='button'&&e.textContent==='让应用 AI 推荐下一篇');assert.equal(run.disabled,true);await run.onclick();
 assert.equal(f.calls.filter(c=>c.data).length,0);
});
test('supplier rejection is visible and does not dispatch a retry on display',async()=>{
 const f=setup([{state:'rejected',document:{message:'账户余额不足，请先处理 API 账户'}}]);await f.sandbox.appendPaperRecommendations();
 assert.ok(f.all().some(e=>e.textContent==='模型服务拒绝请求'));
 assert.ok(f.all().some(e=>e.textContent==='账户余额不足，请先处理 API 账户'));
 assert.equal(f.calls.filter(c=>c.data).length,0);
});
test('historical attempted call is not displayed as unsent and prevents replay',async()=>{
 const f=setup([{state:'attempted',original_state:'not_sent'}]);await f.sandbox.appendPaperRecommendations();
 assert.ok(f.all().some(e=>e.textContent==='模型请求已尝试，未取得有效答复'));
 const run=f.all().find(e=>e.tag==='button'&&e.textContent==='让应用 AI 推荐下一篇');assert.equal(run.disabled,true);await run.onclick();
 assert.equal(f.calls.filter(c=>c.data).length,0);
});
test('approved bounded recovery retains old attempts and enables only an explicit fresh request',async()=>{
 const records=[{state:'invalid_output'},{state:'source_unavailable'},{state:'attempted'}];
 const f=setup(records,{max_requests:6,used_requests:3,recovery_authorized:true,blocked_by_unresolved:false,can_request:true});
 await f.sandbox.appendPaperRecommendations();assert.equal(f.calls.filter(c=>c.data).length,0);
 const run=f.all().find(e=>e.tag==='button'&&e.textContent==='让应用 AI 推荐下一篇');assert.equal(run.disabled,false);
 assert.ok(f.all().some(e=>String(e.textContent).includes('3/6 次推荐')));await run.onclick();
 const post=f.calls.find(c=>c.data);assert.equal(post.data.scope_id,'a'.repeat(32));
});
test('new unresolved recovery blocks further calls despite authorized remaining allowance',async()=>{
 const f=setup([{state:'attempted'},{state:'unknown'}],{max_requests:6,recovery_authorized:true,blocked_by_unresolved:true,can_request:false});
 await f.sandbox.appendPaperRecommendations();const run=f.all().find(e=>e.tag==='button'&&e.textContent==='让应用 AI 推荐下一篇');assert.equal(run.disabled,true);await run.onclick();
 assert.equal(f.calls.filter(c=>c.data).length,0);
});
test('original three-request cap is not reset on page rerender',async()=>{
 const f=setup([{state:'invalid_output'},{state:'not_sent'},{state:'completed'}]);await f.sandbox.appendPaperRecommendations();await f.sandbox.appendPaperRecommendations();
 assert.equal(f.all().find(e=>e.tag==='button'&&e.textContent==='让应用 AI 推荐下一篇').disabled,true);
 assert.equal(f.calls.filter(c=>c.data).length,0);assert.equal(f.sandbox.recommendationScope(),'a'.repeat(32));
});
test('recommendation exposes full title, DOI and element/potential labels, with readiness limitation',async()=>{
 const resource={repository:'author/repo',commit:'1'.repeat(40),elements:['Fe','Al'],pair_styles:['meam'],license:'MIT'};
 const f=setup([{state:'completed',question:'Next paper',document:{candidate:{paper_title:'Full published title',doi:'10.1234/new',author_source:resource,potential:resource},reason:'Matched paired metadata'}}]);
 await f.sandbox.appendPaperRecommendations();const all=f.all();assert.ok(all.some(e=>e.textContent==='Full published title'));
 assert.equal(all.find(e=>e.tag==='a').href,'https://doi.org/10.1234/new');assert.ok(all.some(e=>String(e.textContent).includes('Fe · Al · meam')));
 assert.ok(all.some(e=>String(e.textContent).includes('尚未核验为可运行或科学复现成功')));
});
