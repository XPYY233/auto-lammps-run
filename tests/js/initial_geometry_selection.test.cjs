const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const functions=source.slice(source.indexOf('function geometryLabel('),source.indexOf('function render()'));
const nodes=source.slice(source.indexOf('function node(tag, value, className) {'),source.indexOf('function notice('));
const TASK='a'.repeat(32), PIN='b'.repeat(64), CATALOG='c'.repeat(64);
const entry={pin:PIN,sha256:'d'.repeat(64),summary:{type_elements:['Ni','Cu'],atom_count:2,size:180,units:'metal',composition:{Ni:1,Cu:1},boundary:['p','p','p']}};
const view={configured:true,catalog_sha256:CATALOG,entries:[entry],reason:''};
const clone=value=>JSON.parse(JSON.stringify(value));
function setup({selected=false,frozen=false}={}){
  const elements=new Map(),calls=[],notices=[],pending=[];
  function element(tag){return {tagName:tag,children:[],value:'',id:'',disabled:false,
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];},
    setAttribute(name,value){this[name]=value;},
    before(item){elements.set('#'+item.id,item);}};}
  elements.set('#execution-flow',element('div'));
  const context=vm.createContext({
    current:{id:TASK,revision:9,status:frozen?'conditions_frozen':'conditions_draft',
      ...(selected?{initial_geometry:{catalog_sha256:CATALOG,entry:clone(entry)}}:{})},
    initialGeometryCatalog:null,initialGeometryCatalogTask:null,initialGeometryRead:0,initialGeometryLoading:false,
    document:{createElement:element,getElementById:id=>elements.get('#'+id)},
    $:selector=>elements.get(selector),
    api:(path,data)=>{calls.push({path,data:clone(data??null)});return new Promise((resolve,reject)=>pending.push({resolve,reject}));},
    notice:(message,error)=>notices.push({message,error}),
    action:async work=>{try{await work();}catch(error){context.notice(error.message,true);}},
    afterChange:async message=>{context.renderInitialGeometry();context.notice(message,false);},
  });
  vm.runInContext(nodes+functions,context);
  const panel=()=>elements.get('#initial-geometry-panel');
  return {context,elements,calls,notices,pending,panel};
}
function collect(root,predicate,found=[]){if(predicate(root))found.push(root);for(const child of root?.children||[])if(child&&typeof child==='object')collect(child,predicate,found);return found;}
const text=root=>collect(root,()=>true).map(item=>item.textContent||'').join('\n');

test('ordinary page reads catalog explicitly then posts only the fixed selector',async()=>{
  const c=setup();c.context.renderInitialGeometry();
  assert.equal(c.calls.length,0);
  assert.match(text(c.panel()),/初始结构文件（可选）/);
  const read=collect(c.panel(),item=>item.tagName==='button'&&item.textContent==='读取可用结构')[0];
  const reading=read.onclick();assert.deepEqual(c.calls,[{path:'/api/geometry-catalog',data:null}]);
  c.pending.shift().resolve(clone(view));await reading;
  const form=collect(c.panel(),item=>item.tagName==='form')[0],select=collect(form,item=>item.tagName==='select')[0];
  const save=collect(form,item=>item.type==='submit')[0];
  assert.equal(select.value,'');assert.equal(save.disabled,true);
  select.value=PIN;select.onchange();assert.equal(save.disabled,false);
  assert.match(text(form),/1 → Ni，2 → Cu/);
  let prevented=false;form.onsubmit({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.deepEqual(c.calls[1],{path:`/api/tasks/${TASK}/initial-geometry`,data:{revision:9,catalog_sha256:CATALOG,pin:PIN}});
  c.pending.shift().resolve({...clone(c.context.current),revision:10,initial_geometry:{catalog_sha256:CATALOG,entry:clone(entry)}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(c.context.current.revision,10);assert.equal(c.context.current.initial_geometry.entry.pin,PIN);
  assert.match(text(c.panel()),/已选择的固定结构/);
});

test('frozen selection remains visible without catalog reads or edit controls',()=>{
  const c=setup({selected:true,frozen:true});c.context.renderInitialGeometry();
  assert.match(text(c.panel()),/已随研究条件冻结的结构/);
  assert.match(text(c.panel()),new RegExp(PIN));
  assert.equal(collect(c.panel(),item=>item.tagName==='button'||item.tagName==='select'||item.tagName==='form').length,0);
  assert.equal(c.calls.length,0);
});

test('unconfigured and empty catalog show their honest reason without a form',async()=>{
  for(const result of [{configured:false,entries:[],reason:'未配置固定目录'}, {...view,entries:[],reason:'目录为空'}]){
    const c=setup();const reading=c.context.readInitialGeometryCatalog();c.pending.shift().resolve(clone(result));await reading;
    assert.match(text(c.panel()),new RegExp(result.reason));
    assert.equal(collect(c.panel(),item=>item.tagName==='form').length,0);
    assert.equal(c.calls.length,1);
    assert.match(text(c.panel()),/应用准备结构/);
  }
});

test('catalog response from a departed task cannot populate another task selector',async()=>{
  const c=setup();const reading=c.context.readInitialGeometryCatalog();
  c.context.current={id:'e'.repeat(32),revision:1,status:'conditions_draft'};
  c.context.initialGeometryRead++;c.context.initialGeometryLoading=false;
  c.context.renderInitialGeometry();c.pending.shift().resolve(clone(view));await reading;
  assert.equal(c.context.initialGeometryCatalog,null);
  assert.equal(collect(c.panel(),item=>item.tagName==='form').length,0);
});

test('failed read preserves saved selection and does not retain a selectable old view',async()=>{
  const c=setup({selected:true});c.context.initialGeometryCatalog=clone(view);c.context.initialGeometryCatalogTask=TASK;
  const reading=c.context.readInitialGeometryCatalog();c.pending.shift().reject(new Error('目录暂未通过核对'));await reading;
  assert.equal(c.context.current.initial_geometry.entry.pin,PIN);
  assert.equal(c.context.initialGeometryCatalog,null);
  assert.equal(collect(c.panel(),item=>item.tagName==='form').length,0);
  assert.equal(c.notices[0].error,true);
});

test('clear selection uses current revision and no resource or workflow payload',async()=>{
  const c=setup({selected:true});c.context.renderInitialGeometry();
  const clear=collect(c.panel(),item=>item.textContent==='清除文件选择')[0];
  const clearing=clear.onclick();assert.deepEqual(c.calls,[{path:`/api/tasks/${TASK}/initial-geometry/clear`,data:{revision:9}}]);
  c.pending.shift().resolve({id:TASK,revision:10,status:'conditions_draft'});await clearing;
  assert.equal(c.context.current.initial_geometry,undefined);
  assert.match(c.notices[0].message,/旧版本仍保留/);
});

test('geometry selection and invalidation history explain saved events in ordinary language',async()=>{
  const c=setup();c.context.schema={fields:{material:'材料与成分'}};
  c.elements.set('#history-list',c.context.node('ol'));
  vm.runInContext(source.slice(source.indexOf('async function renderHistory() {'),source.indexOf('async function afterChange(')),c.context);
  const reading=c.context.renderHistory();
  c.pending.shift().resolve({events:[
    {event:'initial_geometry_selected',revision:2,at:'2026-01-01T00:00:00Z'},
    {event:'conditions_generated:initial_geometry_invalidated',revision:3,at:'2026-01-01T00:01:00Z'},
    {event:'candidate_added:material:initial_geometry_invalidated',revision:4,at:'2026-01-01T00:02:00Z'},
  ]});await reading;
  const rendered=text(c.elements.get('#history-list'));
  assert.match(rendered,/选择固定初始结构/);assert.match(rendered,/材料与成分/);
  assert.match(rendered,/原结构选择已撤销，需重新核对/);
  assert.doesNotMatch(rendered,/initial_geometry/);
});
