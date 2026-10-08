const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const section=source.slice(source.indexOf("const taskContents=$('#task-contents');"),source.indexOf("bind('ai-activity-refresh'"));

function setup(){
  const scrolled=[],elements=new Map(),buttons=[];
  const element=(name)=>({name,hidden:false,children:[],dataset:{},attributes:{},textContent:'',firstChild:{textContent:''},
    classList:{values:new Set(),toggle(key,on){if(on)this.values.add(key);else this.values.delete(key);return on;}},
    closest(selector){return selector==='[hidden]'&&this.hidden?this:null;},
    scrollIntoView(){scrolled.push(name);},
    setAttribute(key,value){this.attributes[key]=String(value);},
    querySelector(selector){return selector==='.task-contents-primary'?buttons[0]:null;},
    querySelectorAll(selector){return selector==='button[data-contents-target]'?buttons:[];},
    contains(button){return buttons.includes(button);},
  });
  const $=selector=>{if(!elements.has(selector))elements.set(selector,element(selector));return elements.get(selector);};
  function button(name,target,tab){const b=element(name);b.dataset.contentsTarget=target;if(tab)b.dataset.resultTab=tab;b.closest=()=>b;buttons.push(b);return b;}
  const overview=button('overview','#research-results','overview');overview.className='task-contents-primary';
  const targets=button('targets','#research-results','targets');
  const plots=button('plots','#research-results','plots');
  const history=button('history','#research-results','history');
  const progress=button('progress','#reference-progress');
  const plan=button('plan','#plan-review-panel');
  const discussion=button('discussion','.discussion-panel');
  $('#plan-review-panel').hidden=true;$('.discussion-panel').hidden=true;
  const tabs=['overview','targets','plots','history'].map(key=>{const tab=element('tab-'+key);tab.dataset.resultTab=key;return tab;});
  $('#result-tabs').children=tabs;
  let renders=0;
  const c=vm.createContext({$,current:{mode:'reproduction'},workspaceReport:{closeout:{}},normalResult:null,resultTab:'overview',renderWorkspaceResults(){renders++;}});
  vm.runInContext(section,c);
  return {c,$,buttons,overview,targets,plots,history,progress,plan,discussion,scrolled,tabs,renders:()=>renders};
}

test('saved reproduction results move before preparation and the local directory opens a selected plot',()=>{
  const s=setup();s.c.updateTaskContents();
  assert.equal(s.$('#task-view').classList.values.has('results-first'),true);
  assert.equal(s.$('#result-workspace-heading').textContent,'复现结果');
  assert.equal(s.targets.hidden,false);assert.equal(s.plots.hidden,false);
  assert.equal(s.plan.hidden,true);assert.equal(s.discussion.hidden,true);
  s.$('#task-contents').onclick({target:s.plots});
  assert.equal(s.c.resultTab,'plots');assert.equal(s.renders(),1);
  assert.equal(s.tabs[2].attributes['aria-selected'],'true');
  assert.deepEqual(s.scrolled,['#research-results']);
});

test('without saved results the directory keeps history but hides empty plot and paper targets',()=>{
  const s=setup();s.c.workspaceReport=null;s.$('#reference-progress').hidden=true;s.c.updateTaskContents();
  assert.equal(s.$('#task-view').classList.values.has('results-first'),false);
  assert.equal(s.plots.hidden,true);assert.equal(s.targets.hidden,true);
  assert.equal(s.history.hidden,false);assert.equal(s.progress.hidden,true);
  s.$('#task-contents').onclick({target:s.plots});
  assert.equal(s.renders(),0);assert.deepEqual(s.scrolled,[]);
});

test('a saved baseline comparison replaces stale preparation progress with a result entry',()=>{
  const s=setup();
  const c=vm.createContext({$:s.$,current:{id:'task',status:'conditions_frozen',mode:'reproduction'},
    workspaceReport:{closeout:{acceptance:{scope:'baseline'}}},normalResult:null,executionState:null,
    activityData:{task_id:'task',now:'条件已冻结，可以开始准备计算方案',steps:[]},candidateRecord:null,
    ordinaryExecutionJob:()=>null});
  vm.runInContext(source.slice(source.indexOf('function renderCurrentActivity(){'),source.indexOf('function scheduleActivityRefresh(')),c);
  c.renderCurrentActivity();
  assert.equal(s.$('#ai-current-title').textContent,'基准工况 P–A–B 结果已保存');
  assert.equal(s.$('#next-action').textContent,'查看基准复现结果');
  assert.match(s.$('#next-action-note').textContent,/全篇未覆盖范围/);
  s.$('#next-action').onclick();
  assert.deepEqual(s.scrolled,['#research-results']);
});

test('a newly saved draft does not claim its research conditions are already confirmed',()=>{
  const s=setup();
  const c=vm.createContext({$:s.$,current:{id:'task',status:'draft',mode:'research'},workspaceReport:null,
    normalResult:null,executionState:null,activityData:{task_id:'task',now:'已收到你的需求',steps:[]},
    candidateRecord:null,ordinaryExecutionJob:()=>null});
  vm.runInContext(source.slice(source.indexOf('function renderCurrentActivity(){'),source.indexOf('function scheduleActivityRefresh(')),c);
  c.renderNextAction=()=>{};
  c.renderCurrentActivity();
  assert.match(s.$('#ai-current-detail').textContent,/先整理并确认必要条件/);
  assert.doesNotMatch(s.$('#ai-current-detail').textContent,/已确认的研究条件/);
});
