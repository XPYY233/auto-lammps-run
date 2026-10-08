const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const ID='a'.repeat(32),SHA='b'.repeat(64),OLD='c'.repeat(64);
function element(tag){return {tagName:tag.toUpperCase(),hidden:false,disabled:false,value:'',textContent:'',children:[],attributes:{},
  append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];},
  setAttribute(name,value){this.attributes[name]=String(value);}};}
function fixture(response={configured:true,charts:[]}){
 const elements=new Map(),calls=[],rendered=[];
 const $=selector=>{if(!elements.has(selector))elements.set(selector,element(selector));return elements.get(selector);};
 const c=vm.createContext({$,current:{id:ID,mode:'reproduction'},api:async path=>{calls.push(path);return response;},
  renderAICharts:(items,id,options)=>rendered.push({items,id,options}),updateTaskContents:()=>{},
  referenceChartSource:null,referenceChartRequest:null,referenceChartRead:0});
 vm.runInContext(source.slice(source.indexOf('async function refreshReferenceCharts('),source.indexOf("$('#refresh-reference').onclick=")),c);
 const report={task_id:ID,role:'reference',source_sha256:SHA,manifest_sha256:OLD,output_valid:false};
 return {c,$,calls,rendered,report};
}

test('A chart entry appears only for a verified reference report and labels partial coverage',async()=>{
 const s=fixture();await s.c.refreshReferenceCharts(null);
 assert.equal(s.$('#reference-ai-charts').hidden,true);assert.deepEqual(s.calls,[]);
 await s.c.refreshReferenceCharts(s.report);
 assert.equal(s.$('#reference-ai-charts').hidden,false);
 assert.match(s.$('#reference-ai-scope').textContent,/部分已核验数值/);
 assert.equal(s.c.referenceChartSource.sha,SHA);
 assert.deepEqual(s.calls,[`/api/tasks/${ID}/reference-charts`]);
 assert.equal(s.$('#reference-ai-submit').disabled,false);
});

test('A chart history hides an old or non-A chart even if a response marks it completed',async()=>{
 const good={state:'completed',chart:{role:'reference',source_identity:{task_id:ID,source_sha256:SHA,manifest_sha256:OLD}}};
 const stale={state:'completed',chart:{role:'reference',source_identity:{task_id:ID,source_sha256:OLD,manifest_sha256:OLD}}};
 const leaked={state:'completed',chart:{role:'agent',source_identity:{task_id:ID,source_sha256:SHA,manifest_sha256:OLD}}};
 const s=fixture({configured:true,charts:[good,stale,leaked]});
 await s.c.refreshReferenceCharts(s.report);
 assert.equal(s.rendered[0].items[0].state,'completed');
 assert.equal(s.rendered[0].items[1].state,'source_unavailable');
 assert.equal(s.rendered[0].items[2].state,'source_unavailable');
 assert.equal(s.rendered[0].options.channel,'reference-charts');
});

test('A chart request binds the verified source and reuses its id after an uncertain reply',async()=>{
 const s=fixture(),sent=[];let reads=0;
 s.c.api=async(path,data)=>{sent.push({path,data});throw new Error('connection lost');};
 s.c.action=async callback=>callback();s.c.refreshReferenceHistory=async()=>{reads++;};
 s.c.crypto={randomUUID:()=> 'd'.repeat(32)};
 s.c.referenceChartSource={task:ID,sha:SHA,manifest:OLD};
 s.$('#reference-ai-question').value='绘制作者 A 已有能量图';
 vm.runInContext(source.slice(source.indexOf("$('#reference-ai-refresh').onclick="),source.indexOf('setInterval(()=>{if(current')),s.c);
 await s.$('#reference-ai-submit').onclick();await s.$('#reference-ai-submit').onclick();
 assert.equal(reads,0);
 assert.equal(sent.length,2);
 assert.deepEqual(sent.map(call=>call.data.request_id),['d'.repeat(32),'d'.repeat(32)]);
 assert.equal(sent[0].data.source_sha256,SHA);
 assert.equal(sent[0].data.provider,'deepseek-official');
 assert.match(s.$('#reference-ai-status').textContent,/不会自动再次发送/);
});

test('A chart renderer uses A download route and retains origin in SVG metadata',()=>{
 const s=fixture(),svg=element('svg');
 s.c.document={createElement:element,createElementNS:(_,tag)=>element(tag)};
 s.c.node=(tag,value)=>{const item=element(tag);if(value!==undefined)item.textContent=value;return item;};
 s.c.numericalPlot=()=>svg;
 vm.runInContext(source.slice(source.indexOf('function renderAICharts('),source.indexOf('async function refreshDiscussion(')),s.c);
 const chart={role:'reference',analysis_id:'reference-a:'+OLD,file:'energy.csv',source_sha256:SHA,
  csv_sha256:OLD,x:'step',y:'A_energy',x_unit:'1',y_unit:'eV',rows:2,sampled:false,preview:[[0,1],[1,2]],
  output_status:'partial',output_valid:false,scientific_status:'not_evaluated'};
 s.c.renderAICharts([{id:'e'.repeat(32),question:'已有能量',state:'completed',chart}],ID,
  {box:s.$('#reference-ai-chart-history'),channel:'reference-charts',title:'作者 A'});
 const row=s.$('#reference-ai-chart-history').children[1];
 assert.equal(row.children[2].href,`/api/tasks/${ID}/reference-charts/${'e'.repeat(32)}/download`);
 assert.equal(JSON.parse(svg.children[0].textContent).role,'reference');
 assert.match(row.children.at(-1).textContent,/作者 A 部分数值/);
});
