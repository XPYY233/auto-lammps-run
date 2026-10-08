const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('auto_lammps/web_assets/app.js','utf8');

// 只抽取候选方案区段的生产代码，用最小 DOM 面代替浏览器：测状态文案、请求契约与幂等回报，
// 不测像素。依赖仅 node:test / node:vm / node:assert，无需安装 jsdom。
const panelCode=source.slice(source.indexOf('const candidateStatuses = {'),source.indexOf("$('#prepare-candidate').onclick="));
const labelCode=source.slice(source.indexOf('function acceptanceLabel('),source.indexOf('function renderFlow('));
const nodeCode=source.slice(source.indexOf('function node(tag, value, className) {'),source.indexOf('function notice('));
// 逐条问答的纯函数位于面板切片之外，必须一并注入沙箱，否则渲染路径会因未定义而中断。
const clarifyCode=source.slice(source.indexOf('function questionText('),source.indexOf('function taskState(t)'));
const TASK='a'.repeat(32);

function element(tag){
  return {tagName:tag,children:[],className:'',textContent:'',hidden:false,disabled:false,value:'',
    dataset:{},type:'',id:'',placeholder:'',
    append(...items){this.children.push(...items);},
    replaceChildren(...items){this.children=[...items];},
    setAttribute(){},getAttribute(){return null;}};
}
function setupPanel({status='conditions_frozen',automatic=false,candidatePreparation={enabled:true,reason:''}}={}){
  const elements=new Map(),calls=[],notices=[],documents=[];
  const context=vm.createContext({
    console,
    document:{createElement:tag=>element(tag)},
    Option:function Option(text,value){const option=element('option');option.textContent=text;option.value=value;return option;},
    navigator:{clipboard:{writeText:async()=>{}}},
    schema:{fields:{initialization:'初始化与随机种子',size:'尺寸与晶向',stages:'平衡与生产阶段',
      analysis:'分析方法',outputs:'输出文件与内容',temperature:'温度',material:'材料与成分'},
      automatic_workflow:{configured:automatic},candidate_preparation:candidatePreparation},
    current:{id:TASK,status,revision:21,mode:'research',fields:{}},
    executionState:null,
    candidateState:null,candidateTask:null,candidateRecord:null,candidateAnswers:[],candidateOutcome:'',
    $:selector=>{if(!elements.has(selector))elements.set(selector,element(selector));return elements.get(selector);},
    api:(path,data)=>{calls.push({path,data});return Promise.resolve(documents.length?documents.shift():{id:TASK,revision:22});},
    notice:(message,error)=>{notices.push({message,error:Boolean(error)});},
    renderHistory:async()=>{},refreshModelStatus:async()=>{},refreshWorkspace:async()=>{},renderWorkspaceResults:()=>{},
    number:(value,digits=4)=>String(value),
    duration:seconds=>String(seconds),
    action:async work=>work(),
  });
  vm.runInContext(nodeCode,context);
  vm.runInContext(clarifyCode,context);
  vm.runInContext(panelCode,context);
  return {context,elements,calls,notices,documents};
}
const clarificationJob={
  id:'94fbde3310f24223a5d523fb32a0657d',task_id:TASK,revision:21,state:'clarification',
  label:'需要补充条件',created_at:'2026-09-28T08:34:32.951471+00:00',updated_at:'2026-09-28T08:34:36.137781+00:00',
  execution_authorized:false,
  result:{request_id:'ed7bb1ce06858c653765cc0d4e195f3e',summary:'需要补充条件后才能生成方案。',
    questions:['请给出生成随机固溶体占位的固定随机种子。',
      '请给出立方超胞沿三个轴的重复数或期望原子数。',
      '请说明应力–应变曲线的加载与平衡方案：应变范围与每步采样时长。',
      '请说明弹性常数 Cij 的计算方法与拟合区间。',
      '请确认输出文件格式与所需列名和单位。']},
};

test('四类候选状态文案互相区分，且不把方案准备写成已验证或可执行',async()=>{
  const {context}=setupPanel();
  const read=state=>vm.runInContext(`candidateStatus({state:${JSON.stringify(state)}}).label`,context);
  assert.match(read('queued'),/排队/);
  assert.match(read('clarification'),/需要澄清/);
  assert.match(read('prepared'),/待科学核验/);
  assert.match(read('failed'),/失败/);
  assert.equal(read('unknown_state'),'状态待核对');
  assert.equal(new Set([read('queued'),read('model_requested'),read('clarification'),read('prepared'),read('failed')]).size,5);
  for(const state of ['queued','running','model_requested','preparing_files','clarification','prepared','failed','interrupted','configuration_changed']){
    assert.doesNotMatch(read(state),/已验证|可执行|复现成功/);
  }
});

test('澄清问题按确定性关键词预选条件字段，未命中时留空而不猜',async()=>{
  const {context}=setupPanel();
  const map=question=>vm.runInContext('clarificationFieldFor('+JSON.stringify(question)+')',context);
  for(const [question,field] of [
    ['请给出生成随机固溶体占位的固定随机种子。','initialization'],
    ['请给出立方超胞沿三个轴的重复数或期望原子数。','size'],
    ['请说明应力–应变曲线的加载与平衡方案：应变范围与每步采样时长。','stages'],
    ['请说明弹性常数 Cij 的计算方法与拟合区间。','analysis'],
    ['请确认输出文件格式与所需列名和单位。','outputs'],
  ]) assert.equal(map(question),field,question);
  assert.equal(map('请说明该体系的可接受性。'),'');
});

function collect(root,predicate,found=[]){
  if(predicate(root))found.push(root);
  for(const child of root.children||[]) if(child&&typeof child==='object'&&child.children) collect(child,predicate,found);
  return found;
}
test('冻结任务只引导至正常候选答复入口，不显示重复条件编辑或后台交接',async()=>{
  const {context,calls,notices,elements}=setupPanel({status:'conditions_frozen'});
  const job={...clarificationJob,result:{...clarificationJob.result,
    questions:[{question:'请补充采样方案',why:'需要完整范围'}]}};
  vm.runInContext('renderCandidateClarification('+JSON.stringify(job)+')',context);
  assert.equal(vm.runInContext('candidateAnswers.length',context),0);
  await vm.runInContext('saveClarificationAnswers()',context);
  assert.deepEqual(calls,[]);
  assert.match(notices.at(-1).message,/已冻结版本不可修改/);
  const box=elements.get('#candidate-clarification');
  assert.equal(collect(box,item=>item.id==='save-clarification').length,0);
  assert.equal(collect(box,item=>item.tagName==='textarea').length,0);
  assert.equal(collect(box,item=>item.textContent==='打开逐条问题与答复').length,1);
  const text=collect(box,item=>item.textContent).map(item=>item.textContent).join('\n');
  assert.match(text,/答复追加到本任务的方案历史/);
  assert.match(text,/三轮上限/);
  assert.doesNotMatch(text,/\[object Object\]|请把答复交给控制端|冻结版本不可修改/);
});

test('未冻结条件编辑表单可读显示结构化问题',()=>{
  const {context,elements}=setupPanel({status:'draft'});
  const job={...clarificationJob,result:{...clarificationJob.result,
    questions:[{question:'请给出初始化种子',why:'复查'}]}};
  vm.runInContext('renderCandidateClarification('+JSON.stringify(job)+')',context);
  const text=collect(elements.get('#candidate-clarification'),item=>item.textContent)
    .map(item=>item.textContent).join('\n');
  assert.match(text,/请给出初始化种子/);
  assert.doesNotMatch(text,/\[object Object\]/);
});

test('可编辑任务按既有条件证据端点写入答复并链式推进版本',async()=>{
  const {context,calls,documents}=setupPanel({status:'draft'});
  vm.runInContext('renderCandidateClarification('+JSON.stringify(clarificationJob)+')',context);
  vm.runInContext("candidateAnswers[0].answer.value='20260928'",context);
  vm.runInContext("candidateAnswers[0].unit.value=''",context);
  vm.runInContext("candidateAnswers[2].answer.value='0.5% 应变，逐步弛豫'",context);
  vm.runInContext("candidateAnswers[2].unit.value='%'",context);
  documents.push({id:TASK,revision:22},{id:TASK,revision:23});
  await vm.runInContext('saveClarificationAnswers()',context);
  assert.equal(calls.length,2);
  assert.equal(calls[0].path,`/api/tasks/${TASK}/conditions/initialization`);
  assert.equal(calls[1].path,`/api/tasks/${TASK}/conditions/stages`);
  assert.deepEqual(calls.map(call=>call.data.revision),[21,22]);
  for(const call of calls){
    // 与 AddCondition(extra='forbid') 的字段集合逐项一致，不能多也不能少。
    assert.deepEqual(Object.keys(call.data).sort(),
      ['applicability','evidence_role','origin','revision','source_locator','unit','value']);
    assert.equal(call.data.origin,'user');
    assert.equal(call.data.applicability,'required');
    assert.equal(call.data.evidence_role,'input');
    assert.match(call.data.source_locator,/候选方案澄清问题 \d/);
  }
  assert.equal(calls[1].data.unit,'%');
});

test('答复缺少目标字段时拒绝写入并说明原因',async()=>{
  const {context,calls,notices}=setupPanel({status:'draft'});
  vm.runInContext('renderCandidateClarification('+JSON.stringify(clarificationJob)+')',context);
  vm.runInContext("candidateAnswers[0].answer.value='20260928'",context);
  vm.runInContext("candidateAnswers[0].field.value=''",context);
  await vm.runInContext('saveClarificationAnswers()',context);
  assert.deepEqual(calls,[]);
  assert.match(notices.at(-1).message,/选择要记录的条件字段/);
});

test('重新触发按幂等响应如实回报：同一条记录不等于新调用',async()=>{
  const {context}=setupPanel();
  const outcome=(before,after)=>vm.runInContext(`retriggerOutcome(${JSON.stringify(before)},${JSON.stringify(after)})`,context);
  const same=outcome(clarificationJob,{...clarificationJob});
  assert.match(same,/同一条准备记录/);
  assert.match(same,/尚未读到新的准备事件/);
  assert.doesNotMatch(same,/未再次调用模型|控制端先开放/);
  assert.match(outcome(clarificationJob,{...clarificationJob,state:'model_requested'}),/准备记录已更新/);
  assert.doesNotMatch(same,/已登记新的准备记录|已开始/);
  const fresh=outcome(clarificationJob,{...clarificationJob,id:'b'.repeat(32),created_at:'later'});
  assert.match(fresh,/已登记新的准备记录/);
  assert.match(outcome(clarificationJob,null),/等待后台的准备记录/);
  const moved=outcome(clarificationJob,{...clarificationJob,revision:22});
  assert.match(moved,/准备记录已更新/);
});

test('候选面板如实显示 execution_authorized 与澄清入口，并保留下载',async()=>{
  const {context,elements,calls}=setupPanel();
  context.api=async path=>{calls.push({path});return path.endsWith('/candidate')
    ? {candidate:clarificationJob,downloads_enabled:true}
    : {id:TASK,revision:21};};
  await vm.runInContext('refreshCandidate()',context);
  assert.match(elements.get('#candidate-authorization').textContent,/execution_authorized = false/);
  assert.match(elements.get('#candidate-authorization').textContent,/不构成执行授权/);
  assert.match(elements.get('#candidate-stage').children[0].textContent,/需要澄清/);
  assert.equal(elements.get('#prepare-candidate').hidden,false);
  assert.equal(elements.get('#prepare-candidate').textContent,'重新准备计算方案');
  assert.equal(elements.get('#candidate-clarification').children.length>0,true);
  assert.match(elements.get('#candidate-note').textContent,/条件版本 21/);
  assert.equal(elements.get('#candidate-downloads').children.length,0,'未准备完成时不应提供方案文件下载');
});

test('需要澄清时在页面顶部直接暴露入口并展开详情区，完成后不再提示',async()=>{
  const {context,elements,calls}=setupPanel();
  let job=clarificationJob;
  context.api=async path=>{calls.push({path});return path.endsWith('/candidate')
    ? {candidate:job,downloads_enabled:true} : {id:TASK,revision:21};};
  await vm.runInContext('refreshCandidate()',context);
  const attention=elements.get('#candidate-attention');
  assert.equal(attention.hidden,false);
  assert.match(attention.children.map(child=>child.textContent).join(' '),/方案准备需要补充条件/);
  assert.match(attention.children.map(child=>child.textContent).join(' '),/需要澄清/);
  assert.equal(elements.get('#advanced-task').open,true);
  job={...clarificationJob,state:'prepared',result:{summary:'方案已准备。',questions:[],snapshot_sha256:'f'.repeat(64)}};
  await vm.runInContext('refreshCandidate()',context);
  assert.equal(elements.get('#candidate-attention').hidden,true);
  assert.equal(elements.get('#candidate-clarification').children.length,0);
  assert.equal(elements.get('#candidate-downloads').children.length,5,'准备完成后提供 4 个文件与来源说明');
});

test('澄清答复文本包含问题、答复与目标字段，供交接复制',async()=>{
  const {context}=setupPanel({status:'draft'});
  vm.runInContext('renderCandidateClarification('+JSON.stringify(clarificationJob)+')',context);
  vm.runInContext("candidateAnswers[0].answer.value='20260928'",context);
  vm.runInContext("candidateAnswers[4].answer.value='stress,strain'",context);
  vm.runInContext("candidateAnswers[4].unit.value='GPa'",context);
  const text=vm.runInContext('clarificationAnswersText()',context);
  assert.match(text,/1\. 请给出生成随机固溶体占位的固定随机种子。/);
  assert.match(text,/答复：20260928/);
  assert.match(text,/答复：stress,strain GPa/);
  assert.match(text,/条件字段：输出文件与内容/);
});

test('结果区验收与科学状态由真实字段派生，不硬编码复现成功',async()=>{
  const context=vm.createContext({});
  vm.runInContext(labelCode,context);
  assert.equal(context.acceptanceLabel({status:'accepted_by_user'}),'基准工况 · 用户验收');
  assert.equal(context.acceptanceLabel({status:'pending'}),'验收待确认');
  assert.equal(context.acceptanceLabel(undefined),'验收状态：未记录');
  assert.equal(context.scientificLabel({scientific_status:'diagnostic',formal_blind:false}),
    '科学状态：diagnostic · 未盲测（formal_blind 非真）');
  const state=context.bState({closeout:{acceptance:{status:'accepted_by_user'},scientific_status:'diagnostic',formal_blind:false}});
  assert.match(state,/基准工况 · 用户验收/);
  assert.match(state,/diagnostic/);
  assert.doesNotMatch(state,/复现成功|已验证/);
  assert.equal(context.bState({agent_progress:{stage:'计算结束，待核验'}}),'计算结束，待核验');
});

test('已删除的硬编码结论和反向的助手文案不再出现',async()=>{
  assert.ok(!source.includes('基准复现成功'),'不得硬编码“基准复现成功”');
  assert.ok(!source.includes("'第一周已完成'"),'不得硬编码“第一周已完成”');
  assert.ok(!source.includes('基准已验收 · 用户确认'),'不得硬编码基准验收结论');
  assert.ok(source.includes("result.enabled?'可围绕已有数据提问"),'结果助手启用时应提示可提问');
  assert.ok(source.includes('请先在“设置 → 模型 API”中保存当前所选模型的密钥和模型 ID'),'未配置时应给出应用内可执行的下一步');
});

test('逐条问答按问题组装答复，跳过空回答，并支持结构化问题',()=>{
  const context=vm.createContext({});
  const code=source.slice(source.indexOf('function questionText('),source.indexOf('function taskState(t)'));
  vm.runInContext(code,context);
  assert.equal(context.questionText('纯文本问题'),'纯文本问题');
  assert.equal(context.questionText({question:'结构化问题'}),'结构化问题');
  assert.equal(context.buildClarificationAnswers(['q1',{question:'q2'}],['a1','   ']),'Q1: q1\nA1: a1');
  assert.equal(context.buildClarificationAnswers([{question:'q'}],['x']),'Q1: q\nA1: x');
  assert.equal(context.buildClarificationAnswers(['q'],[]),'');
});
