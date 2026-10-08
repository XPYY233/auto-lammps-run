const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');
const TASK='a'.repeat(32);
function setup(overrides={}){
 const elements=new Map(),clicks=[],scrolls=[];
 const c=vm.createContext({
  current:{id:TASK,status:'draft',mode:'research',fields:{material:{selected:null,confirmed:false}}},
  candidateRecord:null,executionState:null,activityData:null,
  schema:{model_calls_enabled:true},ordinaryExecutionJob:()=>null,proposalRoundsExhausted:job=>job.proposal_rounds?.remaining===0,
  $:selector=>{if(!elements.has(selector))elements.set(selector,{hidden:false,disabled:false,textContent:'',open:false,
    click(){clicks.push(selector);},scrollIntoView(){scrolls.push(selector);}});return elements.get(selector);},
  ...overrides,
 });
 vm.runInContext(source.slice(source.indexOf('function renderNextAction(){'),source.indexOf('function scheduleActivityRefresh(')),c);
 return {c,clicks,scrolls};
}
test('draft next action invokes the ordinary condition control only after a click',()=>{
 const {c,clicks}=setup();c.renderNextAction();
 assert.equal(c.$('#next-action').textContent,'让应用 AI 整理需求');assert.deepEqual(clicks,[]);
 c.$('#next-action').onclick();assert.deepEqual(clicks,['#generate-conditions']);
});
test('an unconfirmed suggestion opens condition review without confirming or freezing it',()=>{
 const {c,clicks,scrolls}=setup({current:{id:TASK,status:'draft',mode:'research',fields:{material:{selected:'choice',confirmed:false}}}});
 c.renderNextAction();assert.match(c.$('#next-action').textContent,/核对 AI 建议/);
 c.$('#next-action').onclick();assert.equal(c.$('#advanced-task').open,true);
 assert.deepEqual(scrolls,['#advanced-task']);assert.deepEqual(clicks,[]);
 assert.equal(c.current.fields.material.confirmed,false);
});
test('complete conditions route to the explicit freeze control without locking automatically',()=>{
 const {c,clicks,scrolls}=setup({current:{id:TASK,status:'draft',mode:'research',fields:{material:{selected:'choice',confirmed:true}}}});
 c.renderNextAction();c.$('#next-action').onclick();
 assert.deepEqual(scrolls,['#freeze']);assert.deepEqual(clicks,[]);assert.equal(c.current.status,'draft');
});
test('a prepared candidate exposes review and never invokes an HPC approval',()=>{
 const {c,clicks,scrolls}=setup({candidateRecord:{state:'prepared'}});
 c.current.status='conditions_frozen';c.renderNextAction();c.$('#next-action').onclick();
 assert.deepEqual(scrolls,['#plan-review-panel']);assert.deepEqual(clicks,[]);
});
test('exhausted preparation routes to the retained error rather than generating a fourth round',()=>{
 const {c,clicks,scrolls}=setup({candidateRecord:{state:'failed',proposal_rounds:{remaining:0}}});
 c.current.status='conditions_frozen';c.renderNextAction();assert.match(c.$('#next-action-note').textContent,/三轮方案机会已用完/);
 c.$('#next-action').onclick();assert.deepEqual(scrolls,['#candidate-attention']);assert.deepEqual(clicks,[]);
});
test('active condition preparation waits and unrelated progress cannot become the previous step',()=>{
 const {c,clicks}=setup({activityData:{task_id:TASK,condition_preparation:{state:'repairing'},steps:[]}});
 c.renderNextAction();assert.equal(c.$('#next-action').hidden,true);assert.match(c.$('#next-action-note').textContent,/正在整理研究条件/);
 c.activityData={task_id:'b'.repeat(32),steps:[{title:'old stage'},{title:'old latest'}]};c.renderNextAction();
 assert.doesNotMatch(c.$('#previous-step').textContent,/old stage/);assert.deepEqual(clicks,[]);
});
test('shortcut navigation refuses unavailable result discussion without issuing requests',()=>{
 const elements=new Map(),scrolls=[];
 const nav={contains:()=>true},button={dataset:{taskTarget:'.discussion-panel'},closest:()=>button};
 const target={closest:()=>({hidden:true}),scrollIntoView:()=>scrolls.push('discussion')},note={textContent:''};
 elements.set('#task-quick-links',nav);elements.set('.discussion-panel',target);elements.set('#task-quick-note',note);
 const c=vm.createContext({$:selector=>elements.get(selector)});
 vm.runInContext(source.slice(source.indexOf("const taskQuickLinks=$('#task-quick-links');"),source.indexOf("bind('ai-activity-refresh'")),c);
 nav.onclick({target:button});assert.match(note.textContent,/已有可用结果后/);assert.deepEqual(scrolls,[]);
});
