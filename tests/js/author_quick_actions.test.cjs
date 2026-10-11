const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');

function quickButton(action,target){
 return {dataset:{taskAction:action,taskTarget:target},closest(selector){
  if(selector.includes('extract-paper'))return action==='extract-paper'?this:null;
  if(selector.includes('review-results'))return action==='review-results'?this:null;
  if(selector.includes('analyze-reference'))return action==='analyze-reference'?this:null;
  return selector==='button[data-task-target]'&&target?this:null;
 }};
}
function setup(){
 const elements=new Map(),quick={contains:()=>true},note={textContent:''};
 let extracts=0;
 const input={value:'',focusCount:0,inputCount:0,focus(){this.focusCount++;},dispatchEvent(){this.inputCount++;}};
 const panel={closest:()=>null,querySelector:()=>input,scrollCount:0,scrollIntoView(){this.scrollCount++;}};
 const files={details:[{open:false},{open:false}],scrollCount:0,querySelectorAll(){return this.details;},scrollIntoView(){this.scrollCount++;},closest:()=>null};
 elements.set('#task-quick-links',quick);elements.set('#task-quick-note',note);
 elements.set('#author-reference .reference-discussion',panel);elements.set('#task-files',files);
 const c=vm.createContext({$:selector=>elements.get(selector),Event,paperWorkflowState:()=>({}),prepareResultReview(){throw Error('wrong action');},openPaperExtraction(){extracts++;}});
 vm.runInContext(source.slice(source.indexOf('const taskQuickLinks='),source.indexOf('const taskContents=')),c);
 return {quick,note,input,panel,files,extracts:()=>extracts};
}
test('paper P shortcut opens the existing workbench path',()=>{
 const {quick,extracts}=setup();quick.onclick({target:quickButton('extract-paper')});
 assert.equal(extracts(),1);
});
test('paper P entry loads saved workbench state without starting extraction',()=>{
 let reads=0,scrolls=0,actions=0;
 const entry={taskId:'task',isConnected:true,open:false,scrollIntoView(){scrolls++;},loadWorkbench(){reads++;}};
 const c=vm.createContext({current:{id:'task'},$:selector=>selector==='#workbench-entry'?entry:null,
   action(callback){actions++;callback();}});
 vm.runInContext(source.slice(source.indexOf('function openPaperExtraction(){'),source.indexOf('function paperWorkflowAction(')),c);
 c.openPaperExtraction();
 assert.equal(entry.open,true);assert.equal(scrolls,1);assert.equal(reads,1);assert.equal(actions,1);
 c.current={id:'another-task'};c.openPaperExtraction();assert.equal(reads,1);
});
test('author A analysis shortcut opens a reviewable question without sending it',()=>{
 const {quick,note,input,panel}=setup();
 quick.onclick({target:quickButton('analyze-reference')});
 assert.match(input.value,/作者 A 已核验的输出/);assert.equal(input.inputCount,1);
 assert.equal(input.focusCount,1);assert.equal(panel.scrollCount,1);
 assert.match(note.textContent,/不会自动使用模型额度/);
 input.value='已有用户问题';quick.onclick({target:quickButton('analyze-reference')});
 assert.equal(input.value,'已有用户问题');assert.equal(input.inputCount,1);
});
test('download shortcut exposes saved file links instead of leaving groups closed',()=>{
 const {quick,files,note}=setup();
 quick.onclick({target:quickButton('', '#task-files')});
 assert.deepEqual(files.details.map(group=>group.open),[true,true]);
 assert.equal(files.scrollCount,1);assert.equal(note.textContent,'');
});

test('author A shortcuts require a source-bound report for the current task',()=>{
 const id='a'.repeat(32),sha='b'.repeat(64),manifest='c'.repeat(64);
 const buttons={'#quick-author-analyze':{hidden:true},'#quick-author-files':{hidden:true}};
 let report={task_id:id,role:'reference',source_sha256:sha,manifest_sha256:manifest,files:[{name:'data.csv'}]},updates=0;
 const c=vm.createContext({$:selector=>selector==='#author-reference'?{hidden:false}:buttons[selector],
  paperWorkflowState:()=>({task_id:id,report,identity:id+':'+sha+':'+manifest}),
  updateTaskContents:()=>updates++});
 vm.runInContext(`let current={id:'${id}'};`,c);
 vm.runInContext(source.slice(source.indexOf('function authorEvidenceIdentity('),source.indexOf('function paperWorkflowCurrent(')),c);
 c.updateAuthorQuickActions();
 assert.equal(buttons['#quick-author-analyze'].hidden,false);assert.equal(buttons['#quick-author-files'].hidden,false);
 report=null;c.updateAuthorQuickActions();
 assert.equal(buttons['#quick-author-analyze'].hidden,true);assert.equal(buttons['#quick-author-files'].hidden,true);
 assert.equal(updates,2);
});
