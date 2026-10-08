'use strict';
const $ = (selector) => document.querySelector(selector);
const origins = {user:'用户明确指定',paper:'论文提供',code:'作者代码提供',proposed:'建议 · 待用户确认'};
const statuses = {missing:'缺失',unselected:'待选择',conflict:'有矛盾',pending:'待确认',confirmed:'已确认'};
let schema, current = null, editing = null, resolving = null, busy = false;
let literaturePreview = null;
let pendingRoute=null, renderedRoute=null;
let paperFilter='all';
let taskCache=[], workspaceReport=null, resultTab='overview', modelPreference=null, normalResult=null, rawResult=null, executionState=null;
let workspaceGeneration=0, workspaceState={task:null,phase:'loading',updated:null};
let referenceProgress=null;
let initialGeometryCatalog=null, initialGeometryCatalogTask=null, initialGeometryRead=0, initialGeometryLoading=false;
let candidateState=null, candidateTask=null, candidatePolling=false;
let candidateRecord=null, candidateAnswers=[], candidateOutcome='';
const methodNames = {lammps_direct:'LAMMPS 直接结果',lammps_postprocessed:'LAMMPS 结果经后处理',other:'其他方法',unclear:'来源不明确'};
const literatureColumns = {material:'材料',conditions:'条件',conditions_text:'条件说明'};
function node(tag, value, className) {
  const item = document.createElement(tag);
  if (value !== undefined) item.textContent = value;
  if (className) item.className = className;
  return item;
}
function notice(value, error = false) {
  const dialog = document.querySelector('dialog[open] .dialog-error');
  if (error && dialog) { dialog.textContent=value; dialog.hidden=false; return; }
  $('#message').textContent = value;
  $('#message').className = error ? 'error' : '';
  $('#message').hidden = false;
}
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Task-Review':'1'},body:JSON.stringify(data)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '输入格式不正确，请检查字段。');
  return result;
}
async function action(work) {
  // A dropped click must be visible: before, a user action during startup or while
  // another action ran was discarded with no feedback at all.
  if (busy) { notice('正在处理上一步，请稍候再试。'); return; }
  busy = true;
  document.querySelectorAll('.dialog-error').forEach(item=>{item.hidden=true;});
  try { await work(); } catch (error) { notice(error.message, true); }
  finally { busy = false; drainRoute(); }
}
function normalizeRoute(hash){
 return /^#[a-f0-9]{32}$/.test(hash)||['#home','#new','#tasks','#papers','#resources','#help'].includes(hash)?hash:'#home';
}
function recordRoute(hash){
 // A newer browser destination wins over a response already in flight.
 if(pendingRoute!==null)return;
 if(location.hash!==hash){
  if(renderedRoute===null)history.replaceState(null,'',hash);
  else history.pushState(null,'',hash);
 }
 renderedRoute=hash;
}
async function renderRoute(hash){
 if(/^#[a-f0-9]{32}$/.test(hash))return openTask(hash.slice(1));
 const pages={'#home':showHome,'#new':renderNew,'#tasks':showTasks,'#papers':showPapers,'#resources':showResources,'#help':showHelp};
 return pages[normalizeRoute(hash)]();
}
function drainRoute(){
 if(busy||pendingRoute===null)return;
 const target=pendingRoute;pendingRoute=null;
 action(()=>renderRoute(target));
}
function requestRoute(hash){
 const target=normalizeRoute(hash);
 // Write navigation intent now; render serially after an existing action.
 if(location.hash!==target)history.pushState(null,'',target);
 pendingRoute=target;drainRoute();
}
function restoreRoute(){pendingRoute=normalizeRoute(location.hash);drainRoute();}
window.addEventListener('hashchange',restoreRoute);
window.addEventListener('popstate',restoreRoute);

async function listTasks() {
  const result = await api('/api/tasks');
  taskCache=result.tasks;
  $('#task-count').textContent = result.tasks.length;
  $('#task-list').replaceChildren();
  if (!result.tasks.length) $('#task-list').append(node('p','还没有任务。','subtle'));
  for (const task of result.tasks.filter(t=>t.title.toLowerCase().includes(($('#task-search').value||'').toLowerCase()))) {
    const button = node('button',task.title, current?.id === task.id ? 'active' : '');
    for(const phase of taskPhaseLabels(task)) button.append(node('small',phase.label));
    button.onclick = () => requestRoute('#'+task.id);
    $('#task-list').append(button);
  }
}
async function openTask(id) {
  const generation=openTask.generation=(openTask.generation||0)+1;
  // Remove the previous task before the new response arrives. Background reads
  // use current.id to reject a response from the task we just left.
  current=null;
  $('#task-view').hidden=true;
  $('#task-loading').hidden=false;
  $('#task-loading').textContent='正在打开任务并读取最新记录…';
  $('#ai-current-title').textContent='正在读取当前任务进度…';
  $('#ai-current-detail').textContent='';$('#ai-current-meta').textContent='';
  $('#previous-step').textContent='正在读取上一条记录…';
  $('#next-action').hidden=true;$('#next-action-note').textContent='';
  $('#task-quick-note').textContent='';
  $('#plan-review-panel').hidden=true;
  $('#ai-activity').replaceChildren();$('#discussion-history').replaceChildren();
  const discussionSubmit=$('#discussion-form button[type=submit]');
  if(discussionSubmit)discussionSubmit.disabled=true;
  let task;
  try { task=await api('/api/tasks/'+id); }
  catch(error) {
    if(generation===openTask.generation)$('#task-loading').textContent='任务暂时无法打开，请稍后重试或返回“我的任务”。';
    throw error;
  }
  if(pendingRoute!==null||generation!==openTask.generation)return;
  current=task;
  clearCandidateView();
  initialGeometryCatalog=null;initialGeometryCatalogTask=null;initialGeometryLoading=false;initialGeometryRead++;
  $('#advanced-task').open=false;
  normalResult=null;workspaceReport=null;rawResult=null;executionState=null;referenceProgress=null;
  $('#reference-progress').replaceChildren();$('#reference-progress').hidden=true;
  activityData=null;
  workspaceGeneration++;workspaceState={task:id,phase:'loading',updated:null};
  for(const selector of ['#task-files','#task-information','#task-resources','#execution-flow'])$(selector).replaceChildren();
  renderWorkspaceResults();
  recordRoute('#'+id);
  render();
  $('#task-view').hidden=false;
  $('#task-loading').hidden=true;
  $('#results-content').replaceChildren();
  $('#results-status').textContent='正在读取记录…';
  window.scrollTo({top:0});
  const reads=[
    ['任务列表',listTasks],['修订记录',renderHistory],['方案准备',refreshCandidate],
    ['补充要求',refreshGuidance],['方案审核',refreshPlanReview],['AI 进度',refreshActivity],
    ['计算历史',refreshResults],['文献记录',refreshReferenceHistory],
    ['数据与图表',refreshWorkspace],['结果讨论',refreshDiscussion],
  ];
  const failures=[];
  for(const [label,read] of reads){
    if(current?.id!==id||pendingRoute!==null||generation!==openTask.generation)return;
    try{await read();}catch(error){failures.push(label);}
  }
  if(current?.id===id&&pendingRoute===null&&generation===openTask.generation&&failures.length)
    notice('部分记录暂未读取：'+failures.join('、')+'。已显示可读取的内容；点击任务顶部“刷新状态”重试。',true);
}
function showNew(){requestRoute('#new');}
async function renderNew() {
  current = null;
  recordRoute('#new');
  hideViews('welcome');
  selectNavigation('new');
  $('#welcome').hidden = false;
  $('#papers-view').hidden = true;
  $('#task-view').hidden = true;
  $('#message').hidden = true;
  await listTasks();
  $('#create-form textarea').focus();
  window.scrollTo({top:0});
}
function conditionStatus(field) {
  if (!field.candidates.length) return 'missing';
  if (!field.selected) return field.candidates.length > 1 ? 'conflict' : 'unselected';
  return field.confirmed ? 'confirmed' : 'pending';
}
function geometryLabel(entry) {
  const summary=entry.summary;
  return `${summary.type_elements.join('–')} · ${summary.atom_count} 原子 · ${summary.units} · ${entry.pin.slice(0,12)}`;
}
function geometryMetadata(entry, catalog) {
  const box=node('div',undefined,'source-context'), summary=entry.summary;
  box.append(node('p',`${summary.atom_count} 个原子 · ${summary.size} 字节 · ${summary.units} 单位制`));
  box.append(node('p','原子类型顺序：'+summary.type_elements.map((element,index)=>`${index+1} → ${element}`).join('，')));
  box.append(node('p','组成：'+Object.entries(summary.composition).map(([element,count])=>`${element} ${count}`).join('，')+' · 边界：'+summary.boundary.join(' ')));
  const identity=node('details');identity.append(node('summary','查看固定结构身份'),
    node('p','结构标识：'+entry.pin),node('p','文件摘要：'+entry.sha256),node('p','目录摘要：'+catalog));
  box.append(identity);
  return box;
}
async function readInitialGeometryCatalog() {
  if(!current || current.status==='conditions_frozen')return;
  const id=current.id, request=++initialGeometryRead;
  initialGeometryLoading=true;renderInitialGeometry();
  try {
    const view=await api('/api/geometry-catalog');
    if(current?.id!==id || request!==initialGeometryRead)return;
    initialGeometryCatalog=view;initialGeometryCatalogTask=id;
  } catch(error) {
    if(current?.id!==id || request!==initialGeometryRead)return;
    initialGeometryCatalog=null;initialGeometryCatalogTask=null;
    notice(error.message,true);
  } finally {
    if(current?.id===id && request===initialGeometryRead){initialGeometryLoading=false;renderInitialGeometry();}
  }
}
function renderInitialGeometry() {
  const anchor=$('#conditions');
  if(!anchor || !current)return;
  let panel=document.getElementById('initial-geometry-panel');
  if(!panel){panel=node('section',undefined,'panel');panel.id='initial-geometry-panel';panel.setAttribute('aria-label','选择固定初始结构');anchor.before(panel);}
  const frozen=current.status==='conditions_frozen', selected=current.initial_geometry;
  panel.replaceChildren(node('h2','初始结构文件（可选）'));
  if(selected){panel.append(node('p',frozen?'已随研究条件冻结的结构':'已选择的固定结构'),geometryMetadata(selected.entry,selected.catalog_sha256));}
  else panel.append(node('p',frozen?'此版本未选择固定结构文件。':'已有结构文件时，可从受信资源目录选择。也可按已确认条件由应用准备结构。','subtle'));
  panel.append(node('p','此处仅核对文件格式、原子数量与固定身份；物理稳定性和科学适用性仍需核验。','form-note'));
  if(frozen)return;
  if(selected)panel.append(node('p','更改材料、结构、尺寸、边界、单位或初始化条件后，需要重新选择结构。','form-note'));
  const actions=node('div',undefined,'actions'), read=node('button',initialGeometryLoading?'正在读取结构目录…':'读取可用结构','quiet');
  read.type='button';read.disabled=initialGeometryLoading;read.onclick=()=>action(readInitialGeometryCatalog);actions.append(read);
  if(selected){
    const clear=node('button','清除文件选择','quiet');clear.type='button';
    clear.onclick=()=>action(async()=>{current=await api(`/api/tasks/${current.id}/initial-geometry/clear`,{revision:current.revision});await afterChange('结构选择已清除，旧版本仍保留。');});
    actions.append(clear);
  }
  panel.append(actions);
  if(initialGeometryLoading)panel.append(node('p','只读取已核对的结构摘要，不导入坐标或发起计算。','form-note'));
  const catalog=initialGeometryCatalogTask===current.id?initialGeometryCatalog:null;
  if(!catalog || initialGeometryLoading)return;
  if(!catalog.configured || !catalog.entries.length){panel.append(node('p',catalog.reason,'subtle'));return;}
  const form=node('form'), label=node('label','选择初始结构'), choices=node('select'), preview=node('div');
  choices.id='initial-geometry-choice';label.setAttribute('for',choices.id);
  const placeholder=node('option','请选择一份结构');placeholder.value='';choices.append(placeholder);
  for(const entry of catalog.entries){const option=node('option',geometryLabel(entry));option.value=entry.pin;choices.append(option);}
  choices.value=selected?.catalog_sha256===catalog.catalog_sha256 && catalog.entries.some(entry=>entry.pin===selected.entry.pin)?selected.entry.pin:'';
  const save=node('button','采用所选结构','primary');save.type='submit';
  const update=()=>{
    const entry=catalog.entries.find(item=>item.pin===choices.value);
    preview.replaceChildren();save.disabled=!entry;
    if(entry)preview.append(geometryMetadata(entry,catalog.catalog_sha256));
  };
  choices.onchange=update;label.append(choices);form.append(label,preview,save);update();
  form.onsubmit=event=>{event.preventDefault();action(async()=>{
    if(!catalog.entries.some(entry=>entry.pin===choices.value))return;
    current=await api(`/api/tasks/${current.id}/initial-geometry`,{revision:current.revision,catalog_sha256:catalog.catalog_sha256,pin:choices.value});
    await afterChange('固定初始结构已保存；条件确认、方案审批与计算次数分别保留。');
  });};
  panel.append(form);
}
function render() {
  hideViews('task-view');
  selectNavigation('tasks');
  $('#welcome').hidden = true;
  $('#task-view').hidden = false;
  $('#papers-view').hidden = true;
  $('#task-title').textContent = String(current.title||'');
  $('#task-prompt').replaceChildren(researchContent(current.prompt));
  // 全局提示条是上一次动作留下的，打开任务时先清掉，避免用户以为那是当前状态。
  const banner = $('#message');
  if (banner) { banner.hidden = true; banner.textContent = ''; banner.className = ''; }
  const frozen = current.status === 'conditions_frozen';
  renderInitialGeometry();
  $('#candidate-panel').hidden = !frozen;
  $('#prepare-candidate').disabled = true;
  $('#candidate-stage').textContent='正在读取准备记录…';
  $('#import-literature').hidden = frozen;
  $('#import-literature').textContent = current.mode==='reproduction' ? '导入文献证据' : '从文献导入条件';
  renderReference();
  renderTargetPlanning();
  $('#task-status').textContent = frozen ? '条件已冻结' : '条件草稿';
  $('#task-meta').textContent = `${current.mode === 'reproduction' ? '文献复现测试' : '科研计算'} · 更新于 ${new Date(current.updated_at).toLocaleString('zh-CN')}`;
  const relevant = Object.entries(current.fields).filter(([key])=>key !== 'reference' || current.mode === 'reproduction');
  const all = relevant.map(([,field])=>field);
  $('#confirmed-count').textContent = `${all.filter(f=>f.confirmed).length} / ${all.length}`;
  $('#conflict-count').textContent = all.filter(f=>conditionStatus(f)==='conflict').length;
  $('#generate-conditions').hidden = frozen;
  $('#generate-conditions').disabled = !schema.model_calls_enabled;
  $('#confirm-all-conditions').hidden = frozen;
  $('#confirm-all-conditions').disabled = !all.some(f=>!f.confirmed) || all.some(f=>!f.selected);
  $('#complete-conditions').hidden = frozen;
  $('#refine-conditions').hidden = frozen;
  $('#refine-conditions').disabled = !schema.model_calls_enabled;
  $('#complete-conditions').disabled = !schema.model_calls_enabled;
  $('#generation-note').textContent = frozen ? '条件已冻结。' : schema.model_calls_enabled ? '根据原始需求整理条件，保留引用和缺项，不自动确认。' : '模型整理尚未启用或额度已用完。需求和已有条件已保存。';
  $('#generation-questions').replaceChildren();
  const batches = Object.values(current.generated_batches || {}).sort((a,b)=>a.revision-b.revision);
  if (batches.length) {
    const questions = batches[batches.length-1].questions.filter(q=>conditionStatus(current.fields[q.field]) !== 'confirmed');
    if (questions.length) {
      const details=node('details');
      details.append(node('summary',`查看 AI 整理的 ${questions.length} 项待明确事项`));
      const list=node('ul');
      for (const q of questions) list.append(node('li',q.question));
      details.append(list);$('#generation-questions').append(details);
    }
  }
  $('#conditions').replaceChildren();
  let index = 0;
  for (const [key,label] of Object.entries(schema.fields)) {
    if (key === 'reference' && current.mode === 'research') continue;
    const field = current.fields[key], status = conditionStatus(field);
    const row = node('article',undefined,'condition');
    row.id = 'condition-'+key;
    const head = node('div');
    const heading = node('div',undefined,'field-heading');
    const title = node('span',undefined,'field-name');
    title.append(node('span',String(++index).padStart(2,'0'),'field-number'),document.createTextNode(label));
    heading.append(title,node('span',statuses[status],'badge '+status)); head.append(heading);
    if (!frozen) {
      const actions = node('div',undefined,'field-actions');
      const edit = node('button',field.candidates.length ? '补充 / 纠正' : '填写条件','quiet');
      edit.setAttribute('aria-label',(field.candidates.length ? '补充' : '填写')+label);
      edit.onclick = () => {
        editing=key; $('#condition-form').reset(); $('#condition-form .dialog-error').hidden=true; $('#condition-title').textContent=label;
        $('#condition-dialog').showModal(); $('#condition-form textarea').focus();
      };
      actions.append(edit);
      if (field.selected && !field.confirmed) {
        const confirm = node('button','确认此项','quiet');
        confirm.setAttribute('aria-label','确认'+label);
        confirm.onclick = () => action(async () => {
          current = await api(`/api/tasks/${current.id}/confirm`,{revision:current.revision,fields:[key]});
          await afterChange('已确认：'+label,key);
        });
        actions.append(confirm);
      }
      head.append(actions);
    }
    const content = node('div');
    const activePolicy = key==='resources' ? schema.task_resource_policy : null;
    if (activePolicy) content.append(node('p',activePolicy.description,'selected'),node('small','当前生效 · 用户已批准；旧条件记录保留在下方历史中。'));
    const history=node('details');history.append(node('summary','查看旧建议与选择记录'));
    if (!field.candidates.length) content.append(node('p','尚未填写，也没有自动采用默认值。','missing-text'));
    for (const choice of field.candidates) {
      const card = node('div',undefined,'candidate'+(choice.id===field.selected?' selected':''));
      const value = (choice.applicability === 'not_applicable' ? '不适用：' : '') + choice.value + (choice.unit ? ' '+choice.unit : '');
      const selected = choice.id===field.selected;
      card.append(node('p',value),node('small',(selected&&field.confirmed?'当前已确认':origins[choice.origin])+(choice.source_locator ? ' · '+choice.source_locator : '')));
      if (choice.generated_evidence) {
        const details=node('details');
        details.append(node('summary','查看整理依据'),node('p',choice.generated_evidence.quote),
                       node('small','引用已与所给原文核对；科学含义仍需确认。'));
        card.append(details);
      }
      if (choice.literature_source) {
        const source = choice.literature_source;
        const snapshot = current.literature_sources?.[source.source_sha256];
        const details = node('details');
        details.append(node('summary','查看导入原文与分类依据'));
        details.append(node('p',methodNames[source.method_class]+' · 操作者声明，尚未验证'));
        details.append(node('p','分类依据：'+source.classification_basis));
        if (snapshot) renderSource(details,snapshot.row);
        card.append(details);
      }
      if (!frozen && choice.id !== field.selected) {
        const use = node('button','采用这一项','quiet');
        use.setAttribute('aria-label','采用'+label+'：'+choice.value.slice(0,80));
        use.onclick = () => {
          resolving = {field:key,candidate_id:choice.id}; $('#resolve-form').reset(); $('#resolve-form .dialog-error').hidden=true; $('#resolve-dialog').showModal();
        };
        card.append(use);
      }
      (activePolicy || (frozen && !selected) ? history : content).append(card);
    }
    if (history.children.length>1) content.append(history);
    if (field.resolution) (activePolicy?history:content).append(node('p','选择依据：'+field.resolution,'resolution'));
    row.append(head,content); $('#conditions').append(row);
  }
  $('#freeze').hidden = frozen;
  $('#freeze').disabled = !!current.issues.length || current.mode==='reproduction';
  $('#export').hidden = !frozen;
  $('#export').href = `/api/tasks/${current.id}/export`;
  $('#package-exports').hidden = !frozen;
  $('#export-execution').href = `/api/tasks/${current.id}/packages/execution`;
  $('#export-reference').href = `/api/tasks/${current.id}/packages/reference`;
  $('#freeze-heading').textContent = frozen ? '这份条件已锁定，修订证据已保留。' : '保存这份确定的研究条件';
  $('#freeze-note').textContent = frozen ? '可导出本人的条件审阅记录。它不是主 Agent 的隔离任务包，也不授权执行计算。' : `还有 ${current.issues.length} 项需要处理。冻结后不可覆盖；不会自动生成脚本或提交计算。`;
}
async function renderHistory() {
  const id=current.id;
  const {events,preparation_events=[],lifecycle_events=[]} = await api(`/api/tasks/${id}/history`);
  if(current?.id!==id) return;
  const labels = {created:'建立任务',candidate_added:'补充条件证据',condition_selected:'选择条件',user_confirmed:'确认条件',targets_selected:'选择复现目标',target_inventory_assessed:'整理图表工况与资源',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件',reference_evidence_generated:'整理文献证据',initial_geometry_selected:'选择固定初始结构',initial_geometry_cleared:'清除初始结构选择'};
  $('#history-list').replaceChildren();
  for(const event of lifecycle_events)$('#history-list').append(node('li',new Date(event.at).toLocaleString('zh-CN')+' · '+(event.action==='finish'?'用户确认任务结束':'用户删除列表记录')));
  for (const item of events) {
    const [kind,...parts] = item.event.split(':');
    const fields=parts.filter(part=>part!=='initial_geometry_invalidated').join(',');
    const details = (fields ? ' · '+fields.split(',').map(key=>schema.fields[key]||key).join('、') : '')+(item.event.includes(':initial_geometry_invalidated')?' · 原结构选择已撤销，需重新核对':'');
    $('#history-list').append(node('li',`版本 ${item.revision} · ${labels[kind] || kind}${details} · ${new Date(item.at).toLocaleString('zh-CN')}`));
  }
  for(const item of preparation_events) {
    $('#history-list').append(node('li',`方案准备 · ${item.label} · ${new Date(item.at).toLocaleString('zh-CN')}`));
  }
}
async function afterChange(message, field, openCreatedTask=false) {
  const id=current?.id;if(!id)return;
  // Mutation replies contain the task record only. Rejoin human-only evidence
  // through GET, never persist it into frozen inputs or model context.
  try{const document=await api(`/api/tasks/${id}`);if(current?.id!==id||pendingRoute!==null||($('#task-view').hidden&&!openCreatedTask))return;current=document;}
  catch(error){notice(message+'；任务已保存，页面证据暂未刷新：'+error.message);return;}
  render(); await listTasks(); await renderHistory(); await refreshCandidate(); await refreshResults(); await refreshReferenceHistory(); await refreshWorkspace(); notice(message);
  if (field) document.getElementById('condition-'+field).scrollIntoView({block:'nearest'});
}
$('#close-dialog').onclick=()=>$('#condition-dialog').close();
$('#cancel-resolve').onclick=()=>$('#resolve-dialog').close();
$('#create-form').onsubmit=(event)=>{
  event.preventDefault();
  action(async()=>{
    const form=new FormData(event.target);
    const data=Object.fromEntries(form);
    const supplements=[['extra-material','材料体系'],['extra-temperature','温度'],['extra-loading','压力 / 加载'],['extra-properties','目标性质'],['extra-potential','势函数'],['extra-resources','已有资源']].filter(([id])=>$('#'+id).value.trim()).map(([id,label])=>label+'：'+$('#'+id).value.trim());
    if(supplements.length)data.prompt+='\n\n补充条件：\n'+supplements.join('\n');
    data.title=data.title.trim() || data.prompt.trim().slice(0,36);
    current=await api('/api/tasks',data);
    initialGeometryCatalog=null;initialGeometryCatalogTask=null;initialGeometryLoading=false;initialGeometryRead++;
    normalResult=null;workspaceReport=null;resultTab='overview';
    recordRoute('#'+current.id);
    await afterChange('研究需求已保存。可在这里继续查看进度和结果。',undefined,true);
    event.target.reset();setMode('research');
    if (schema.model_calls_enabled) await generateConditions();
  });
};
$('#condition-form').onsubmit=(event)=>{
  event.preventDefault();
  action(async()=>{
    const data=Object.fromEntries(new FormData(event.target));
    current=await api(`/api/tasks/${current.id}/conditions/${editing}`,{...data,revision:current.revision,evidence_role:'input'});
    $('#condition-dialog').close(); await afterChange('条件证据已保存；请确认当前选择。',editing);
  });
};
$('#resolve-form').onsubmit=(event)=>{
  event.preventDefault();
  action(async()=>{
    current=await api(`/api/tasks/${current.id}/conditions/${resolving.field}/select`,{revision:current.revision,candidate_id:resolving.candidate_id,reason:new FormData(event.target).get('reason')});
    $('#resolve-dialog').close(); await afterChange('选择理由已记录，原有证据仍保留。',resolving.field);
  });
};
$('#freeze').onclick=()=>action(async()=>{
  current=await api(`/api/tasks/${current.id}/freeze`,{revision:current.revision});
  await afterChange('条件已冻结。尚未进行科学检查，也没有提交计算。');
});
async function refreshModelStatus() {
  schema=await api('/api/schema');
  $('#model-state').textContent=schema.model_calls_enabled ? '模型条件整理已启用；尚不提交作业。' : '科研自动执行尚未启用。模型连接与结果讨论可在设置中配置。';
  $('#model-create-note').textContent=schema.model_calls_enabled ? '保存后将使用已配置模型整理需求，保留原文依据。' : '需求会先保存。通用自动计算尚在验证中，当前不自动提交作业。';
  if(schema.reference_generation?.configured && !schema.model_calls_enabled) $('#model-state').textContent='文献整理服务已配置；研究需求模型尚未启用或额度已用完。不提交作业。';
}
function renderReference() {
  const isReference=current.mode==='reproduction';
  $('#reference-panel').hidden=!isReference;
  const content=$('#reference-results');content.replaceChildren();
  if(!isReference) return;
  const config=schema.reference_generation || {configured:false};
  const batches=Object.values(current.reference_batches||{}).sort((a,b)=>a.revision-b.revision);
  $('#reference-status').textContent=batches.length ? `已保存 ${batches.length} 批证据草稿；条件见下方，论文结果见此处。` :
    config.configured ? '尚无证据草稿。导入文献工作台的证据后可自动整理。' : '文献自动整理尚未配置。已有记录会保留。';
  for(const [index,batch] of batches.entries()) {
    const card=node('article',undefined,'analysis-report');
    card.append(node('h3',`第 ${index+1} 批 · 论文报告结果`));
    if(!batch.reported_results.length) card.append(node('p','本批没有提取到有原文支持的结果。'));
    for(const result of batch.reported_results) {
      const item=node('div',undefined,'result-source');
      item.append(node('h4',`${result.quantity}：${result.value}${result.unit?' '+result.unit:''}`),
        node('p',`${methodNames[result.method_class]||'来源不明确'} · 模型分类，尚未核验`,'subtle'));
      const source=node('details');source.append(node('summary','查看论文出处'),node('p',result.source_locator),node('p',result.quote));
      if(result.method_evidence) source.append(node('strong','方法依据'),node('p',result.method_evidence.source_locator),node('p',result.method_evidence.quote));
      item.append(source);card.append(item);
    }
    if(batch.questions.length) {
      card.append(node('h4','待明确事项'));
      const list=node('ul');for(const q of batch.questions) list.append(node('li',q.question));card.append(list);
    }
    content.append(card);
  }
}
async function refreshReferenceHistory() {
  if(!current || current.mode!=='reproduction') return;
  const id=current.id, result=await api(`/api/tasks/${id}/reference-evidence`);
  if(current?.id!==id) return;
  $('#reference-history').replaceChildren();
  if(!result.requests.length) $('#reference-history').append(node('li','尚无文献整理请求。'));
  for(const r of result.requests) {
    const row=node('li',`${new Date(r.at).toLocaleString('zh-CN')} · ${r.label}`);
    if(r.state==='completed' && current.status!=='conditions_frozen') {
      const recover=node('button','恢复已返回草稿','quiet');
      recover.onclick=()=>action(async()=>{
        current=await api(`/api/tasks/${id}/reference-evidence/${r.request_id}/recover`,{revision:current.revision});
        await afterChange('已恢复保存的模型响应，没有发出新请求。');
      });
      row.append(recover);
    }
    $('#reference-history').append(row);
  }
}
$('#refresh-reference').onclick=()=>action(async()=>{
  const id=current.id;current=await api('/api/tasks/'+id);
  await refreshModelStatus();await afterChange('文献记录已刷新。');
});
async function confirmAllConditions() {
  const id=current.id;
  const relevant=Object.entries(current.fields).filter(([key])=>key!=='reference'||current.mode==='reproduction');
  if(relevant.some(([,f])=>!f.selected)){notice('请先补齐缺项并解决矛盾。',true);return;}
  const fields=relevant.filter(([,f])=>!f.confirmed).map(([key])=>key);
  if(!fields.length)return;
  const updated=await api(`/api/tasks/${id}/confirm`,{revision:current.revision,fields});
  if(current?.id!==id)return;
  current=updated;await afterChange('已确认当前选择的全部研究条件；尚未提交计算。');
}
$('#confirm-all-conditions').onclick=()=>action(confirmAllConditions);
async function completeConditions(refine=false) {
  const id=current.id, revision=current.revision;
  $('#complete-conditions').disabled=true;
  notice('正在为缺失条件提出可执行的默认建议；建议需你逐项确认，不会自动确认。');
  try {
    await api(`/api/tasks/${id}/complete-conditions`,{revision,refine});
    const updated=await api(`/api/tasks/${id}`);
    if(current?.id!==id)return;
    current=updated;
    await afterChange('已补充待确认的条件建议。请核对来源标注为“模型建议”的条目。');
  } finally {
    await refreshModelStatus();
    $('#complete-conditions').disabled=false;
  }
}
async function generateConditions() {
  const id=current.id, revision=current.revision;
  $('#generate-conditions').disabled=true;
  notice('正在整理需求中的条件，尚未提交计算。');
  try {
    // Reconcile the saved request before selecting an explicit retry token.
    // An interrupted POST resumes the active identity on the server.
    const activity=await api(`/api/tasks/${id}/ai-activity`);
    if(current?.id!==id)return;
    if(activity.task_id!==id)throw Error('条件整理记录不属于当前任务，请重新核对。');
    const condition=activity.condition_preparation;
    const payload={revision};
    if(condition?.state==='failed'){
      if(!/^[a-f0-9]{32}$/.test(condition.request_id))throw Error('失败请求身份缺失，请先核对已有记录。');
      payload.retry_of=condition.request_id;
    }
    const updated=await api(`/api/tasks/${id}/generate-conditions`,payload);
    if(current?.id!==id)return;
    current=updated;
    await afterChange('条件草稿已整理。请核对摘要中的条件与待明确事项。');
  } finally {
    await refreshModelStatus();
    if(current?.id===id){render();await refreshActivity();}
  }
}
$('#generate-conditions').onclick=()=>action(generateConditions);
$('#complete-conditions').onclick=()=>action(()=>completeConditions(false));
$('#refine-conditions').onclick=()=>action(()=>completeConditions(true));
const resultMetrics={mean:'均值',sample_std:'样本标准差',min:'最小值',max:'最大值',value:'末行值',x:'末行横坐标',slope:'斜率',intercept:'截距',rmse:'残差均方根',r_squared:'R²'};
const analysisMethods={summary:'区间统计',last:'区间末行',linear_fit:'线性拟合'};
function resultReport(report,taskId) {
  const box=node('article',undefined,'analysis-report');
  box.append(node('h4',report.label));
  if(report.status!=='analyzed') {box.append(node('p',report.message));return box;}
  box.append(node('p',report.quantity));
  for(const item of report.results) {
    box.append(node('h5',`${analysisMethods[item.method]||item.method} · ${item.y}`),
      node('p',`${item.x} 区间：${item.window.join(' 至 ')} · ${item.sample_count} 行数据`,'subtle'));
    const table=node('table',undefined,'result-table'),head=node('thead'),headRow=node('tr'),body=node('tbody');
    for(const title of ['指标','数值','单位']) headRow.append(node('th',title));
    head.append(headRow);table.append(head,body);
    for(const [key,value] of Object.entries(item.values)) {
      const row=node('tr');
      row.append(node('th',resultMetrics[key]||key),node('td',value===null?'未估计':String(value)),node('td',item.value_units[key]||'—'));
      body.append(row);
    }
    box.append(table);
    const source=node('details',undefined,'result-source');source.append(node('summary','数据来源与选取范围'));
    const file=report.sources.find(s=>s.file===item.file);
    source.append(node('p',`文件：${item.file} · 源行：${item.source_line_ranges.map(([a,b])=>a===b?String(a):`${a}–${b}`).join('、')}`));
    if(file) source.append(node('p',file.columns.map(c=>`${c.name} [${c.unit}]`).join(' · ')),node('p','SHA-256：'+file.sha256,'source-hash'));
    box.append(source);
  }
  for(const result of report.site_thermodynamic_results||[])box.append(siteDetails(result,taskId,report.id,{data:true}));
  for(const note of report.notes) box.append(node('p',note,'form-note'));
  const link=node('a','下载数值分析报告 ↓','quiet');
  link.href=`/api/tasks/${taskId}/results/${report.id}/download`;box.append(link);
  return box;
}
async function refreshResults() {
  if(!current || $('#task-view').hidden) return;
  const id=current.id;
  try {
    const result=await api(`/api/tasks/${id}/results`);
    if(current?.id!==id || $('#task-view').hidden) return;
    normalResult=result;
    const content=$('#results-content');content.replaceChildren();
    $('#results-status').textContent=result.message+' 最近读取：'+new Date().toLocaleTimeString('zh-CN');
    for(const [index,evaluation] of result.evaluations.entries()) {
      const group=node('section',undefined,'result-evaluation');
      group.append(node('h3',`评测 ${index+1} · 已提交 ${evaluation.dispatch_count} / ${evaluation.max_attempts} 次`),
        node('p',`额度占用 ${evaluation.used_attempts} 次 · 其中 ${evaluation.pending_attempts} 次待提交；失败及提交状态不明的派发仍计数。`,'subtle'));
      for(const request of evaluation.requests) {
        const card=node('div',undefined,'request-record');
        card.append(node('h4',(request.dispatch_ordinal?`第 ${request.dispatch_ordinal} 次提交`:'尚未提交')+' · '+request.state_label),
          node('p',request.stage+' · '+(request.accounted?'资源已核算':'资源待核算')));
        for(const report of request.reports) card.append(resultReport(report,id));
        const history=node('details',undefined,'result-history');history.append(node('summary','运行历史'));
        const events=node('ol');
        for(const event of request.history) events.append(node('li',`${new Date(event.at*1000).toLocaleString('zh-CN')} · ${event.label}`));
        history.append(events,node('p','运行记录：'+request.id,'source-hash'));card.append(history);group.append(card);
      }
      content.append(group);
    }
  } catch(error) {
    if(current?.id!==id || $('#task-view').hidden) return;
    normalResult=null;
    $('#results-content').replaceChildren();
    $('#results-status').textContent='结果暂不可读。未展示旧结果，请稍后刷新或联系管理员。';
  }
}
$('#refresh-results').onclick=()=>action(refreshResults);
$('#jump-results').onclick=event=>{event.preventDefault();$('#results-panel').scrollIntoView({block:'start'});};
// 候选作业状态文案：排队 / 需要澄清 / 已完成 / 失败分开显示，准备完成不等于科学验证或执行授权。
const candidateStatuses = {
  queued:{label:'排队中 · 等待准备',tone:'pending'},
  running:{label:'进行中 · 核对准备条件',tone:'pending'},
  model_requested:{label:'进行中 · 已请求模型',tone:'pending'},
  reusing_plan:{label:'进行中 · 沿用上一版方案',tone:'pending'},
  checking_plan:{label:'进行中 · 核对需求与方案',tone:'pending'},
  repairing_plan:{label:'进行中 · 自动修正方案',tone:'pending'},
  preparing_files:{label:'进行中 · 准备结构与输入文件',tone:'pending'},
  clarification:{label:'需要澄清 · 等待补充条件',tone:'attention'},
  prepared:{label:'方案已准备 · 待科学核验',tone:'confirmed'},
  failed:{label:'失败 · 准备未完成',tone:'failed'},
  interrupted:{label:'中断 · 待核对',tone:'attention'},
  configuration_changed:{label:'配置已变化 · 待核对',tone:'attention'},
};
const candidateErrorLabels = {
  plan_iteration_limit:'首版在内三轮已用完，不能追加生成',
  model_budget_exhausted:'模型额度已用完，未自动重试',
  model_key_missing_or_invalid:'模型密钥尚未配置',
  request_already_reserved:'已有模型请求记录，需要核对',
  model_transport_unknown:'调用状态未确认，未自动重试',
  model_generation_failed:'模型未返回完整有效方案',
  candidate_validation_failed:'方案或资源检查未通过',
  preparation_failed:'文件准备未完成',
};
// 澄清问题到条件字段的确定性关键词映射：只做预选，用户可改选；页面不替用户猜填数值。
const clarificationFieldRules = [
  [/随机|种子|seed/i,'initialization'],
  [/列名|单位|输出|文件|格式/,'outputs'],
  [/加载|应变|拉伸|平衡|采样|步数|时长/,'stages'],
  [/超胞|重复数|原子数|晶向|盒|尺寸/,'size'],
  [/计算方法|计算方式|拟合|cij|弹性常数|平均|方法/,'analysis'],
  [/温度/,'temperature'],[/压力/,'pressure'],[/边界/,'boundary'],
  [/势函数|势|potential/i,'potential'],[/核|内存|资源/,'resources'],
];
function clarificationFieldFor(question) {
  const fields=schema?.fields||{};
  for(const [pattern,field] of clarificationFieldRules) if(pattern.test(question)&&fields[field]) return field;
  return '';
}
function candidateFieldSelect(selected,label) {
  const select=node('select');select.setAttribute('aria-label',label);
  select.append(new Option('请选择条件字段',''));
  for(const [key,text] of Object.entries(schema?.fields||{})) select.append(new Option(text,key));
  select.value=selected||'';return select;
}
function candidateStatus(job) {
  return candidateStatuses[job.state]||{label:job.label||'状态待核对',tone:'attention'};
}
function candidateStageChip(job) {
  const status=candidateStatus(job);return node('span',status.label,'badge '+status.tone);
}
function clarificationDrafts() {
  return candidateAnswers.map(entry=>({question:entry.question,value:entry.answer?.value?.trim?.()||'',
    unit:entry.unit?.value?.trim?.()||'',field:entry.field?.value||''}));
}
function clarificationAnswersText() {
  return clarificationDrafts().filter(draft=>draft.value).map((draft,index)=>
    `${index+1}. ${draft.question}\n   答复：${draft.value}${draft.unit?' '+draft.unit:''}`+
    `${draft.field&&schema?.fields?.[draft.field]?'（条件字段：'+schema.fields[draft.field]+'）':''}`).join('\n\n');
}
function renderCandidateClarification(job) {
  const box=$('#candidate-clarification');box.replaceChildren();candidateAnswers=[];
  if(!job||job.state!=='clarification') return;
  const questions=job.result?.questions||[],frozen=current.status==='conditions_frozen';
  box.append(node('h3','需要补充的条件'),
    node('p',`准备方案时模型要求补充 ${questions.length} 项信息；补齐前不会生成结构、势函数调用或输入脚本，也不会提交计算。`,'plot-caption'));
  if(job.result?.request_id) box.append(node('p','模型请求记录：'+job.result.request_id,'source-hash'));
  if(!questions.length) {box.append(node('p','服务端标记为需要澄清，但没有返回具体问题，请核对准备记录。','form-note'));return;}
  if(frozen) {
    box.append(node('p','原研究条件已固定。请在“方案准备需要补充条件”区逐条答复；答复追加到本任务的方案历史，仍共用首版在内三轮上限，不会改写冻结条件。','form-note'));
    const go=node('button','打开逐条问题与答复','primary');go.type='button';
    go.onclick=()=>{
      $('#advanced-task').open=true;
      $('#candidate-attention').scrollIntoView({behavior:'smooth',block:'start'});
      $('#candidate-attention').querySelector('textarea')?.focus();
    };
    box.append(go);
    return;
  }
  const form=node('form',undefined,'clarification-form');form.id='clarification-form';
  const cell=(text,control)=>{const label=node('label',undefined,'clarification-cell');label.append(node('span',text),control);return label;};
  for(const [index,rawQuestion] of questions.entries()) {
    const question=questionText(rawQuestion);
    const answer=node('textarea');answer.rows=3;answer.maxLength=4000;answer.className='clarification-answer';
    answer.setAttribute('aria-label',`问题 ${index+1} 的答复`);
    const unit=node('input');unit.maxLength=80;unit.className='clarification-unit';unit.placeholder='可留空';
    unit.setAttribute('aria-label',`问题 ${index+1} 答复的单位`);
    const field=candidateFieldSelect(clarificationFieldFor(question),`问题 ${index+1} 记录到哪个条件字段`);
    field.className='clarification-field';
    const card=node('article',undefined,'clarification-question');
    card.append(node('h4',`问题 ${index+1}`),node('p',question,'clarification-text'),
      cell('答复内容',answer),cell('单位',unit),cell('记录到条件字段',field));
    form.append(card);candidateAnswers.push({question,answer,unit,field});
  }
  box.append(form);
  box.append(node('p','答复会以“用户明确指定”写入所选条件字段（复用既有条件证据接口）。写入后需在条件区逐项选择、确认并重新冻结，才能再次准备方案。','form-note'));
  const actions=node('div',undefined,'actions');
  const save=node('button','记录澄清答复','primary');
  save.type='button';save.id='save-clarification';
  save.onclick=()=>action(saveClarificationAnswers);
  const copy=node('button','复制澄清答复','quiet');copy.type='button';copy.id='copy-clarification';
  copy.onclick=()=>action(copyClarificationAnswers);
  actions.append(save,copy);box.append(actions,
    node('p','尚未写入任何条件。','form-note'));
}
async function copyClarificationAnswers() {
  const text=clarificationAnswersText();
  if(!text) {notice('尚未填写答复，没有可复制的内容。',true);return;}
  try {await navigator.clipboard.writeText(text);notice('澄清答复已复制。要在本任务继续，请点击“记录澄清答复”。');}
  catch(error) {notice('无法自动写入剪贴板，请在页面中手动选择答复文本。',true);}
}
async function saveClarificationAnswers() {
  if(!current) return;
  if(current.status==='conditions_frozen') {notice('已冻结版本不可修改：答复未写入条件，也没有重新调用模型。',true);return;}
  const drafts=clarificationDrafts().filter(draft=>draft.value);
  if(!drafts.length) {notice('请至少填写一条答复。',true);return;}
  const fields=schema?.fields||{};
  if(drafts.some(draft=>!draft.field||!fields[draft.field])) {notice('每条答复都要选择要记录的条件字段。',true);return;}
  const id=current.id;let document=current;
  for(const [index,draft] of drafts.entries()) {
    // 复用既有条件证据端点，不新建提交或记账路径；revision 链式推进。
    document=await api(`/api/tasks/${id}/conditions/${draft.field}`,{revision:document.revision,value:draft.value,
      unit:draft.unit,origin:'user',source_locator:`候选方案澄清问题 ${index+1}`,applicability:'required',evidence_role:'input'});
  }
  if(current?.id!==id) return;
  current=document;candidateOutcome='';
  await renderHistory();
  notice(`已按条件证据记录 ${drafts.length} 条答复。请在条件区选择并确认后重新冻结，再准备方案。`);
}
// 服务端一个任务只保留一条准备记录且入队幂等：重新触发后必须按响应如实回报，不假定发生新调用。
function retriggerOutcome(before,after) {
  if(!after) return '请求已交给自动流程；正在等待后台的准备记录。';
  const name=`方案记录 ${String(after.id).slice(0,8)} · 登记版本 ${after.revision}`;
  if(!before||before.id!==after.id) return `已登记新的准备记录（${name}），后台会读取该记录。`;
  if((after.events||[]).length>(before.events||[]).length || after.state!==before.state)
    return `准备记录已更新（${name}）：${candidateStatus(after).label}。调用情况以活动记录为准。`;
  if(before.created_at===after.created_at&&before.revision===after.revision)
    return `服务端返回同一条准备记录（${name}）：一个任务只保留一条准备记录，尚未读到新的准备事件；调用情况以活动记录为准，原历史保留。`;
  return `准备记录已更新（${name}）：${candidateStatus(after).label}。`;
}


async function refreshGuidance() {
  const list=$('#guidance-list'); if(!list) return;
  const id=current?.id; if(!id){list.replaceChildren();return;}
  let data;
  try { data=await api(`/api/tasks/${id}/guidance`); }
  catch(error){ list.replaceChildren(node('li','无法读取引导：'+error.message,'subtle')); return; }
  if(current?.id!==id) return;
  const paused=Boolean(data.paused);
  const button=$('#task-pause');
  if(button){ button.textContent=paused?'继续任务':'暂停任务';
              button.className=paused?'primary':'quiet'; }
  const noteBox=$('#guidance-note'); if(noteBox) noteBox.disabled=false;
  list.replaceChildren();
  const items=data.guidance||[];
  if(!items.length){ list.append(node('li','还没有中途引导。','subtle')); return; }
  for(const item of items){
    list.append(node('li',`#${item.sequence} ${String(item.at||'').replace('T',' ').slice(0,19)} — ${item.note}`));
  }
}

async function sendGuidance() {
  const note=$('#guidance-note').value.trim();
  if(!note){ notice('请先写下引导内容。', true); return; }
  const id=current.id;
  await api(`/api/tasks/${id}/guidance`,{revision:current.revision,note});
  const updated=await api(`/api/tasks/${id}`);
  if(current?.id!==id)return;
  current=updated;
  $('#guidance-note').value='';
  await afterChange('引导已记录；应用内 AI 的后续判断会遵守它。');
  await refreshGuidance();
}

async function togglePause() {
  const paused=($('#task-pause').textContent||'').includes('暂停');
  const id=current.id;
  const result=await api(`/api/tasks/${id}/pause`,{revision:current.revision,paused});
  const updated=await api(`/api/tasks/${id}`);
  if(current?.id!==id)return;
  current=updated;
  await afterChange(result.paused?'任务已暂停：新的准备与派发会等待你的继续。':'任务已继续。');
  await refreshGuidance();
}


function planFilesText(review){
  return (review?.files||[]).map(file=>`${file.name}  ${(file.size/1024).toFixed(1)} KiB  ${String(file.sha256||'').slice(0,12)}`).join('\n');
}

let activityTimer=null;
let activityData=null;
let activityGeneration=0;

function researchText(value){
  return String(value||'').replace(/\\[()[\]]/g,'').replace(/\\times/g,'×')
    .replace(/\\(?:mathrm|text)\{([^{}]*)\}/g,'$1')
    .replace(/\^\{([^{}]*)\}/g,'^($1)').replace(/_\{([^{}]*)\}/g,'_($1)');
}

function researchContent(value,cls=''){
  const source=String(value??'');
  const body=typeof renderResearchMarkdown==='function'?renderResearchMarkdown(source):node('div',source,'markdown-body markdown-fallback');
  if(cls)body.className=[body.className||'',cls].join(' ').trim();
  return body;
}

function ordinaryExecutionJob(){
  if(typeof normalResult==='undefined')return null;
  const groups=normalResult?.evaluations||[],runs=groups.flatMap(g=>g.requests.filter(r=>r.dispatch_ordinal!==null).map(r=>({r,g})));
  if(!runs.length)return null;
  const {r,g}=runs.sort((a,b)=>a.r.history[0].at.localeCompare(b.r.history[0].at)).at(-1);
  const analyzed=r.reports.some(x=>x.status==='analyzed');
  return {state:analyzed?'analyzed':r.state,label:analyzed?'数值处理完成 · 科学结论待核验':r.state_label,
    job_id:r.job_id,scheduler_state:r.state,accounted:r.accounted,dispatch_count:groups.reduce((n,x)=>n+x.dispatch_count,0),
    max_attempts:groups.reduce((n,x)=>n+x.max_attempts,0),events:r.history};
}
function proposalRoundsExhausted(record){
  const budget=record?.proposal_rounds;
  return Boolean(budget)&&(budget.historical_count_unknown||budget.remaining<=0);
}
function proposalRoundLabel(record){
  const budget=record?.proposal_rounds;
  if(!budget)return '首版在内最多三轮';
  if(budget.historical_count_unknown)return '历史方案轮次待核验 · 禁止追加';
  return `方案生成 ${budget.used} / ${budget.limit} 轮（含首版） · 剩余 ${budget.remaining} 轮`;
}
function renderCurrentActivity(){
  const title=$('#ai-current-title'),detail=$('#ai-current-detail'),meta=$('#ai-current-meta');
  if(!title)return;
  const generate=$('#generate-conditions');
  if(generate)generate.textContent='根据需求整理条件';
  const taskActivity=activityData?.task_id===current?.id?activityData:null;
  const job=executionState?.job||ordinaryExecutionJob();
  const preparing=candidateRecord&&['queued','running','model_requested','reusing_plan','checking_plan','repairing_plan','preparing_files'].includes(candidateRecord.state);
  if(preparing){
    title.textContent=candidateStatus(candidateRecord).label;
    detail.textContent='应用正在处理当前方案；之前的方案、计算结果和失败记录仍可查看。';
    const seconds=Math.max(0,(Date.now()-Date.parse(candidateRecord.updated_at))/1000);
    meta.textContent='当前阶段开始于 '+new Date(candidateRecord.updated_at).toLocaleTimeString('zh-CN')+' · 已持续 '+duration(seconds)+' · 后台继续，无需保持页面打开';
  }else if(candidateRecord&&['clarification','failed','interrupted','configuration_changed'].includes(candidateRecord.state)){
    title.textContent=candidateStatus(candidateRecord).label;
    detail.textContent=candidateRecord.result?.detail||candidateRecord.result?.message||'请查看页面上方的待处理问题；已有方案和计算历史保留。';
    meta.textContent='当前准备未完成，不会自动追加计算提交。';
  }else if(job){
    const state=job.scheduler_state;
    title.textContent=job.job_id?('作业 '+job.job_id+' · '+(requestStates[state]||job.label)):job.label;
    detail.textContent=job.job_id&&['queued','running'].includes(state)?'方案已批准并提交。应用后台正在跟进，结束后回收输出并进行分析。':
      (job.state==='analyzed'?'已保存分析结果，请查看下方结果；科学结论仍需核验。':job.state==='awaiting_approval'?'请审阅下方方案，确认后提交计算。':job.state==='attention'?'当前流程需要处理，请查看下方执行说明。':job.label);
    meta.textContent='提交次数 '+job.dispatch_count+' / '+job.max_attempts+' · '+(executionState?.worker_alive?'后台服务在线':'后台服务未运行')+' · 调度完成与科学核验分别记录';
  }else if(current?.status!=='conditions_frozen'&&taskActivity?.condition_preparation){
    const condition=taskActivity.condition_preparation;
    title.textContent=condition.label||'正在核对需求中的条件';
    detail.textContent=condition.detail||'模型答复和来源核对分开记录；未通过核对的条件不会写入方案。';
    meta.textContent='条件整理已记录 '+(condition.call_count||0)+' 次模型调用 · '+(condition.state==='failed'?'尚未生成方案或提交计算；失败和费用保留。':'条件确认后才能准备方案。');
    if(generate)generate.textContent=condition.recovery_required&&!condition.reconstructed?'恢复已返回的条件（不新增调用）':'根据需求整理条件';
  }else{
    title.textContent=taskActivity?.now||'等待准备计算方案';
    detail.textContent=candidateRecord?.state==='prepared'?'方案已准备，等待你的确认。':'应用按已确认的研究需求组织方案。';
    meta.textContent='进度来自应用记录；刷新页面不会重新生成方案或提交计算。';
  }
  renderNextAction();
}

function renderNextAction(){
  const button=$('#next-action'),note=$('#next-action-note'),previous=$('#previous-step');
  if(!button||!current)return;
  const steps=activityData?.task_id===current.id?(activityData.steps||[]):[];
  const prior=steps.length>1?steps[steps.length-2]:null;
  previous.textContent=prior?`上一条记录：${prior.title||'阶段记录'} · ${new Date(prior.at).toLocaleString('zh-CN')}`:'还没有可核对的上一条阶段记录。';
  button.hidden=true;button.disabled=false;button.onclick=null;note.textContent='';
  const jump=(label,selector,explain,expand)=>{
    const target=$(selector);
    if(!target||target.hidden){note.textContent=explain||'正在读取下一步入口…';return;}
    button.textContent=label;button.hidden=false;note.textContent=explain||'';
    button.onclick=()=>{if(expand)$(expand).open=true;target.scrollIntoView({block:'start'});};
  };
  const job=executionState?.job||ordinaryExecutionJob();
  if(job){
    if(['queued','running','waiting','dispatching','collecting','analyzing'].includes(job.state)){
      note.textContent=`应用正在后台跟进${job.job_id?'作业 '+job.job_id:'提交'}；可关闭页面，稍后再来查看。`;
      jump('查看计算进度','#execution-flow',note.textContent);return;
    }
    if(job.state==='analyzed'){
      jump('查看数据与图表','#research-results','结果已保存；科学结论仍待核验。');return;
    }
    if(job.state==='completed'){
      jump('查看回收与分析进度','#execution-flow','计算已结束；输出与分析尚待核验。');return;
    }
    if(job.state==='attention'||job.can_retry){
      jump('查看问题与恢复操作','#execution-flow','应用保留了本次作业和费用记录；请先查看原因。');return;
    }
    if(job.state==='awaiting_approval'){
      jump('查看并确认计算方案','#plan-review-panel','请先核对方案，再决定是否提交计算。');return;
    }
    if(['analysis_failed','diagnostics_saved','failed','timeout','rejected'].includes(job.state)){
      jump('查看失败原因与记录','#execution-flow','本次执行未完成；已有提交与费用记录保留。');return;
    }
    if(['unknown','reconcile_required'].includes(job.state)){
      jump('核对提交状态','#execution-flow','提交或计算回执待核对；不会自动再次提交。');return;
    }
    jump('查看计算记录','#execution-flow','已有执行记录；请先核对其状态与可用操作。');return;
  }
  if(candidateRecord){
    if(['queued','running','model_requested','reusing_plan','checking_plan','repairing_plan','preparing_files'].includes(candidateRecord.state)){
      note.textContent='应用内 AI 正在准备方案，后台会继续处理。';return;
    }
    if(candidateRecord.state==='clarification'){
      jump('回答 AI 的问题','#candidate-attention','答复会进入下一轮方案准备；剩余轮次见准备记录。');return;
    }
    if(['failed','interrupted','configuration_changed'].includes(candidateRecord.state)){
      jump('查看未完成原因','#candidate-attention',proposalRoundsExhausted(candidateRecord)?'三轮方案机会已用完；已有记录保留，不会自动追加调用。':'先查看失败原因，再决定如何继续。');return;
    }
    if(candidateRecord.state==='prepared'){
      jump('查看并确认计算方案','#plan-review-panel','请先核对方案，再决定是否提交计算。');return;
    }
  }
  if(current.mode==='reproduction'&&current.status==='conditions_frozen'){
    jump('查看 P–A–B 进度','#reference-progress','作者参考 A 与独立方案 B 分开记录。');return;
  }
  if(current.status==='conditions_frozen'){
    const prepare=$('#prepare-candidate');
    if(prepare&&!prepare.hidden&&!prepare.disabled){
      button.textContent='让应用 AI 准备方案';button.hidden=false;
      button.onclick=()=>prepare.click();note.textContent='准备方案不会提交 HPC 计算。';
    }else note.textContent='正在读取准备服务状态；可在下方查看具体原因。';
    return;
  }
  const fields=Object.entries(current.fields||{}).filter(([key])=>key!=='reference'||current.mode==='reproduction').map(([,field])=>field);
  const missing=fields.filter(field=>!field.selected).length;
  const unconfirmed=fields.filter(field=>field.selected&&!field.confirmed).length;
  const condition=activityData?.task_id===current.id?activityData.condition_preparation:null;
  if(['generating','validating','repairing','awaiting_import'].includes(condition?.state)){
    note.textContent='应用内 AI 正在整理研究条件；完成后会显示需要你核对的内容。';return;
  }
  if(!missing&&!unconfirmed){
    const freeze=$('#freeze');
    if(freeze&&!freeze.disabled){
      button.textContent='确认条件并进入方案准备';button.hidden=false;
      button.onclick=()=>{ $('#advanced-task').open=true;$('#freeze').scrollIntoView({block:'center'});};
      note.textContent='请核对条件锁定说明；锁定后不能覆盖。';
    }else jump('查看尚待处理的条件','#advanced-task','应用保留缺项或矛盾的原因；请查看条件摘要。','#advanced-task');
    return;
  }
  if(!missing&&unconfirmed){
    jump('核对 AI 建议并确认','#advanced-task',`${unconfirmed} 项条件需要你确认；应用不会替你作科学决定。`,'#advanced-task');return;
  }
  const complete=$('#complete-conditions');
  if(Object.keys(current.generated_batches||{}).length&&complete&&!complete.disabled){
    button.textContent='让应用 AI 补齐未指定条件';button.hidden=false;button.onclick=()=>complete.click();
    note.textContent=`还有 ${missing} 项未确定；AI 会提出有来源的建议，供你审核。`;
    return;
  }
  if(Object.keys(current.generated_batches||{}).length&&!schema.model_calls_enabled){
    jump('查看条件与 AI 状态','#advanced-task',`${missing} 项仍未确定；请查看模型额度或失败记录。`,'#advanced-task');return;
  }
  const generate=$('#generate-conditions');
  if(generate&&!generate.disabled){
    button.textContent='让应用 AI 整理需求';button.hidden=false;button.onclick=()=>generate.click();
    note.textContent='应用会保留你的原文和条件来源。';
  }else{button.textContent='配置应用内 AI';button.hidden=false;button.onclick=()=>$('#open-model').click();note.textContent='模型尚未就绪或当前任务额度已用完，请查看设置与工作记录。';}
}

function scheduleActivityRefresh(){
  if(activityTimer){clearTimeout(activityTimer);activityTimer=null;}
  activityTimer=setTimeout(()=>{activityTimer=null;refreshActivity();},6000);
}

async function refreshActivity(){
  const box=$('#ai-activity'); if(!box) return;
  const id=current?.id;
  const generation=++activityGeneration;
  if(!id){box.replaceChildren();if(activityTimer){clearTimeout(activityTimer);activityTimer=null;}return;}
  let data;
  try { data=await api(`/api/tasks/${id}/ai-activity`); }
  catch(error){
    if(current?.id!==id||generation!==activityGeneration)return;
    box.replaceChildren(node('p','暂时读不到进度：'+error.message,'subtle'));
    scheduleActivityRefresh();
    return;
  }
  if(current?.id!==id||generation!==activityGeneration) return;
  activityData=data;
  renderCurrentActivity();
  const note=$('#ai-activity-note');
  if(note) note.textContent=(data.now?('当前：'+data.now+'　'):'')+(data.note||'');
  const steps=data.steps||[];
  box.replaceChildren();
  scheduleActivityRefresh();
  if(!steps.length){ box.append(node('p','还没有进度记录。','subtle')); return; }
  const list=node('ol',undefined,'timeline');
  steps.forEach((step,index)=>{
    const latest=index===steps.length-1;
    const item=node('li',undefined,'timeline-item'+(latest?' current':'')+(step.state==='attention'?' attention':''));
    item.append(node('span',undefined,'timeline-dot'));
    const body=node('div',undefined,'timeline-body');
    const head=node('div',undefined,'timeline-head');
    head.append(node('strong',step.title||''));
    if(latest) head.append(node('span','现在','badge pending'));
    head.append(node('small',new Date(step.at).toLocaleString('zh-CN')));
    body.append(head);
    if(step.detail) body.append(node('p',step.detail,'subtle'));
    item.append(body);
    list.append(item);
  });
  box.append(list);
  // The current stage remains live even when the detailed event list is collapsed.
  // These are read-only local records, independent of mutation serialization.
  box.scrollTop=box.scrollHeight;
}

function renderPlanSummary(review){
  const box=$('#plan-summary');
  const opened=new Set([...box.children].filter(x=>x.open).map(x=>x.dataset?.key));box.replaceChildren();
  const detailsFor=(key,label,cls)=>{const d=node('details',undefined,cls);d.dataset.key=key;d.open=opened.has(key);d.append(node('summary',label));return d;};
  const plan=review.workspace?.current;
  if(!plan){if(review.summary)box.append(researchContent(review.summary));return;}
  const quantity=current.fields?.quantity;const target=quantity?.candidates?.find(c=>c.id===quantity.selected)?.value;
  box.append(researchContent('研究目标：'+(target||plan.analysis?.quantity||current.title||''),'plan-objective'));
  const cards=node('div',undefined,'plan-facts');
  const fact=(title,text)=>{const card=node('div',undefined,'plan-fact');card.append(node('span',title),node('p',text));cards.append(card);};
  const structures=[plan.structure,...plan.additional_structures.map(item=>item.structure)];
  const names={bcc:'体心立方',fcc:'面心立方',sc:'简单立方',hcp:'密排六方'};
  const g=plan.structure;
  fact('材料与结构',`${(g.elements||[]).join(' / ')} · ${names[g.crystal]||g.crystal||'自定义结构'} · ${structures.map(x=>(x.repeat||[]).join('×')).filter(Boolean).join('、')||'尺寸见结构文件'}`);
  fact('边界与初始晶格',(g.boundary||[]).map(x=>x==='p'?'周期':x==='f'?'固定':x).join(' / ')+(g.a_angstrom?` · 初始晶格常数 ${g.a_angstrom} Å`:''));
  const potential=plan.potential||{};
  const selected=current.fields?.potential;const source=selected?.candidates?.find(c=>c.id===selected.selected)?.value;
  fact('势函数',source||`${(potential.elements||[]).join(' / ')} · ${potential.style||'类型见脚本'} · ${potential.units||''}`);
  const r=plan.resources||{};
  fact('计算资源',`${r.cores} CPU 核 · ${r.memory_bytes/1024**3} GiB · 单作业最长 ${r.wall_seconds/3600} 小时`);
  box.append(cards);
  const steps=node('div',undefined,'plan-calculation');steps.append(node('h3','计算安排'));
  const workflow=plan.workflow||'';const list=node('ol');
  list.append(node('li',`准备 ${structures.length} 种初始结构；尺寸和成分沿用已确认条件。`));
  if(/box\/relax/.test(workflow))list.append(node('li','完美体系：进行晶胞与原子联合弛豫。'));
  if(/delete_atoms/.test(workflow))list.append(node('li','缺陷体系：删除指定原子，并保存删除记录。'));
  if(/minimize/.test(workflow))list.append(node('li',`进行能量最小化（脚本含 ${workflow.split('\n').filter(x=>/^minimize\s/.test(x)).length} 个最小化阶段）；记录实际收敛情况。`));
  if(/\brun\s/.test(workflow))list.append(node('li','执行模拟阶段，并按方案保存过程数据。'));
  const columnNames={E_form_eV:'空位形成能（eV）',inv_N:'原子数倒数（1/N）'};
  for(const op of plan.analysis?.plan?.operations||[])list.append(node('li',`${({linear_fit:'线性拟合',maximum:'最大值',mean:'均值',minimum:'最小值'}[op.method]||op.method)}：${columnNames[op.y]||op.y||''}${op.x?' 对 '+(columnNames[op.x]||op.x):''}；数据来自 ${op.file}。`));
  steps.append(list);box.append(steps);
  const files=plan.analysis?.files||[];
  const outputs=detailsFor('outputs',`查看预期科研产物（${files.length} 个文件）与数据字段`,'plan-output-list');
  for(const table of plan.analysis?.plan?.tables||[])outputs.append(node('p',`${table.file}：${table.columns.map(c=>c.name+' ['+c.unit+']').join('、')}`));
  outputs.append(node('p',files.join('、')));box.append(outputs);
  const check=plan.automatic_check||review.automatic_check;
  if(check){const details=detailsFor('checks','查看应用的需求与步骤核对结果','plan-checks');
    for(const item of check.coverage||[])details.append(researchContent(item.requirement+'：'+item.evidence));box.append(details);}
  const recovery=plan.failure_recovery||review.failure_recovery;
  if(recovery){const details=detailsFor('recovery','查看应用内 AI 的失败诊断与本次修正','plan-checks');
    details.append(researchContent(recovery.summary),researchContent('原因：'+recovery.cause),researchContent('修改：'+recovery.repair));
    for(const text of recovery.evidence||[])details.append(node('pre',text,'plan-code'));box.append(details);}
  const explanation=detailsFor('explanation','查看模型完整方案说明','plan-checks');explanation.append(researchContent(plan.summary));box.append(explanation);
}

function renderPlanVersions(review){
  const versions=review.workspace?.versions||[],box=$('#plan-version-list');
  const opened=new Set([...box.children].filter(x=>x.open).map(x=>x.dataset?.version));box.replaceChildren();
  $('#plan-versions').hidden=!versions.length;
  $('#plan-versions-label').textContent=`查看历次方案、修改原因与步骤差异（${versions.length} 版）`;
  for(const version of [...versions].reverse()){
    const item=node('details',undefined,'plan-version-item');
    item.dataset.version=String(version.number);item.open=opened.has(item.dataset.version);
    const changes=version.available?(version.number===1?'首版方案':version.changes.length?version.changes.join('、'):'科研设置与步骤未变化；重新封装并核验'):version.reason;
    item.append(node('summary',`第 ${version.number} 版 · ${changes}`));
    item.append(node('p',new Date(version.at).toLocaleString('zh-CN'),'subtle'),researchContent('修改依据：'+version.reason));
    if(version.workflow_diff)item.append(node('pre',version.workflow_diff,'plan-code'));
    box.append(item);
  }
}

async function refreshPlanReview(){
  const panel=$('#plan-review-panel');if(!panel)return;
  const id=current?.id;if(!id){panel.hidden=true;return;}
  let review;
  try{review=await api(`/api/tasks/${id}/plan`);}
  catch(error){$('#plan-status').textContent='暂时无法读取方案，已展示的方案未更新。请刷新核对。';$('#plan-approve').disabled=true;$('#plan-revise').disabled=true;return;}
  if(current?.id!==id)return;
  const plan=review.workspace?.current;
  const prepared=review.state==='prepared'&&(!review.workspace||(plan&&!plan.historical));
  panel.hidden=!prepared&&!plan;$('#open-current-plan').disabled=panel.hidden;if(panel.hidden){renderNextAction();return;}
  $('#plan-version').textContent=(plan?`第 ${plan.version} 版${plan.historical?' · 上一份已准备方案':''}`:'已准备')+' · '+proposalRoundLabel(review);
  $('#plan-status').textContent=prepared?(review.approved?'当前方案已批准。':'方案已准备，等待你确认。'):'应用正在处理下一版；下方保留最近一版方案供查看，暂不能批准。';
  renderPlanSummary(review);renderPlanVersions(review);
  const files=$('#plan-files');const opened=new Set([...files.children].filter(x=>x.open).map(x=>x.dataset?.file));files.replaceChildren();
  for(const file of (plan?.files||review.files||[])){
    const block=node('details',undefined,'plan-file');
    block.dataset.file=file.name;block.open=opened.has(file.name);
    const labels={'in.lammps':'LAMMPS 计算脚本','analysis.json':'数据分析定义','structure.data':'初始原子结构'};
    block.append(node('summary',`${labels[file.name]||'其他尺寸初始结构'} · ${file.name} · ${(file.size/1024).toFixed(1)} KiB`));
    if(file.content)block.append(node('pre',file.content.slice(0,20000),'plan-code'));
    block.append(node('p','文件校验值：'+file.sha256,'form-note'));files.append(block);
  }
  let execution=executionState||{},executionReadable=true;
  // A refresh of the plan always reads the real dispatch receipt; approval never regenerates a plan.
  try{execution=await api(`/api/tasks/${id}/execution`);}catch(error){execution={};executionReadable=false;}
  if(current?.id!==id)return;
  const job=execution.job||{},dispatched=Boolean(job.job_id);
  const active=['queued','running','waiting','dispatching'].includes(job.state);
  const unresolved=['unknown','reconcile_required'].includes(job.scheduler_state);
  const blocked=!executionReadable||execution.configured===false;
  const retryAvailable=job.scheduler_state==='failed'&&job.accounted&&job.dispatch_count<job.max_attempts;
  const approve=$('#plan-approve');approve.disabled=!prepared||active||blocked||(dispatched&&!retryAvailable);
  approve.textContent=retryAvailable?`使用第 ${job.dispatch_count+1} 次机会提交 HPC`:dispatched?'已提交 · 作业 '+job.job_id:active?'正在提交，请等待回执':unresolved?'核对已有提交回执':review.approved?'提交已批准方案':'批准并提交 HPC';
  $('#plan-note').textContent=blocked?'执行回执暂不可读取或执行服务未配置；已暂停批准与修改，请刷新核对。':dispatched?'作业 '+job.job_id+' · '+(requestStates[job.scheduler_state]||job.label)+'。状态与提交次数见上方；本按钮不会重复提交。':
    !prepared?'等待当前方案修改与核对结束；旧版批准不能用于新版。':active?'应用正在传送输入或等待调度回执，请勿重复提交。':review.approved?'提交将使用这一份已批准方案，不会重新生成。':'确认后，应用将提交这一份方案到 HPC；修改后需要重新确认。';
  approve.onclick=()=>action(async()=>{
    if(approve.disabled)return;
    approve.disabled=true;
    if(!review.approved)await api(`/api/tasks/${id}/plan/approve`,{revision:current.revision,note:'页面批准'});
    current=await api(`/api/tasks/${id}`);
    const endpoint=retryAvailable?'execution/retry':job.state==='attention'?'execution/recheck':'execution';
    try{await api(`/api/tasks/${id}/${endpoint}`,{revision:current.revision});await afterChange('已提交申请；请查看真实 HPC 作业回执。');}
    catch(error){await afterChange('提交未通过：'+error.message);}
    await refreshPlanReview();await refreshCandidate();
  });
  const revise=$('#plan-revise'),input=$('#plan-revision-note');
  const limitReached=proposalRoundsExhausted(review);
  revise.disabled=!prepared||active||unresolved||blocked||limitReached;input.disabled=active||unresolved||blocked||limitReached;
  $('#plan-revision-help').textContent=limitReached?'三轮方案准备机会已用完或历史轮次无法核验；不能追加生成。已有完整方案仍可查看并批准提交。':active?'本次计算已经提交，运行中的方案保持固定。计算结束后可讨论结果；不会修改正在运行的计算。':'写下具体修改意见；首版在内最多三轮，应用内 AI 将修改当前方案，保留旧版和记录，新版需要重新确认。';
  revise.onclick=()=>action(async()=>{
    if(revise.disabled)return;
    const note=input.value.trim();if(!note){notice('请在方案旁的修改意见框填写要求。',true);input.focus();return;}
    revise.disabled=true;
    await api(`/api/tasks/${id}/plan/revise`,{revision:current.revision,note});
    if(current?.id!==id)return;
    current=await api(`/api/tasks/${id}`);input.value='';
    await afterChange('修改意见已交给应用内 AI；完成后会显示新版与差异，需要重新确认。');
    await refreshPlanReview();await refreshCandidate();await refreshActivity();
  });
  renderNextAction();
}

function clearCandidateView() {
  // Invalidate earlier reads, including a response from a previous visit to the same task.
  refreshCandidate.generation=(refreshCandidate.generation||0)+1;
  candidateState=null;candidateTask=null;candidateRecord=null;candidateAnswers=[];candidateOutcome='';
  for(const selector of ['#candidate-attention','#candidate-clarification','#candidate-summary','#candidate-downloads','#candidate-stage']) {
    const element=$(selector);if(element) element.replaceChildren();
  }
  for(const selector of ['#candidate-attention','#candidate-answers-block']) {
    const element=$(selector);if(element) element.hidden=true;
  }
  const answers=$('#candidate-answers');if(answers) answers.value='';
  for(const selector of ['#candidate-note','#candidate-authorization','#candidate-outcome']) {
    const element=$(selector);if(element) element.textContent='';
  }
  const prepare=$('#prepare-candidate');if(prepare) prepare.disabled=true;
}
async function refreshCandidate() {
  if(!current || current.status!=='conditions_frozen') {
    clearCandidateView();
    if(current) renderCurrentActivity();
    return;
  }
  const id=current.id;
  if(candidateTask!==id) clearCandidateView();
  const generation=refreshCandidate.generation=(refreshCandidate.generation||0)+1;
  const {candidate,downloads_enabled}=await api(`/api/tasks/${id}/candidate`);
  if(current?.id!==id || current.status!=='conditions_frozen' || $('#task-view').hidden || generation!==refreshCandidate.generation) return;
  candidateState=candidate?.state || null; candidateTask=id; candidateRecord=candidate||null;
  renderCurrentActivity();
  const automatic=schema.automatic_workflow?.configured;
  const available=(automatic?schema.automatic_workflow:schema.candidate_preparation) || {enabled:false,reason:'方案准备服务尚未配置。'};
  const retriggerable=Boolean(candidate)&&['clarification','failed','interrupted','configuration_changed'].includes(candidate.state);
  if(retriggerable) $('#advanced-task').open=true; // 需要用户处理时直接展开，不把澄清问题藏在折叠区
  const answersBlock=$('#candidate-answers-block');
  if(answersBlock) answersBlock.hidden=!(candidate && candidate.state==='clarification');
  const attention=$('#candidate-attention');attention.hidden=!retriggerable;attention.replaceChildren();
  if(retriggerable) {
    attention.append(node('strong',candidate.state==='clarification'?'方案准备需要补充条件：':
      candidate.state==='failed'?'方案准备未完成，需要处理：':'方案准备需要核对：'),
      node('span',`${candidateStatus(candidate).label} · 记录 ${String(candidate.id).slice(0,8)}`));
    const detail=String(candidate?.result?.detail || candidate?.result?.message || '').trim();
    if(detail) attention.append(node('p',detail,'attention-detail'));
    const questions=candidate?.result?.questions || [];
    if(questions.length){
      // 逐条显示问题、原因、建议和答复位置；所有答复共用首版在内三轮上限。
      const form=node('div',undefined,'clarify-form');
      questions.forEach((question,index)=>{
        const item=node('div',undefined,'clarify-item');
        item.append(node('p',`${index+1}. ${questionText(question)}`,'clarify-question'));
        if(question&&typeof question==='object'&&question.why)item.append(node('p','为什么问：'+question.why,'subtle'));
        const answer=node('textarea');answer.rows=2;answer.dataset.questionIndex=String(index);
        answer.placeholder='在此回答这一条；留空表示跳过';
        if(question&&typeof question==='object'&&question.suggestion){
          const line=node('p','建议：'+question.suggestion,'subtle');
          const use=node('button','采用建议','quiet');use.type='button';
          use.onclick=()=>{answer.value=question.suggestion;};
          line.append(' ',use);
          item.append(line);
        }
        item.append(answer);
        form.append(item);
      });
      attention.append(form);
      const submit=node('button','提交答复并继续生成方案','primary');submit.type='button';submit.disabled=proposalRoundsExhausted(candidate);
      submit.onclick=()=>action(async()=>{
        const inputs=[...document.querySelectorAll('#candidate-attention textarea[data-question-index]')];
        const values=inputs.map(input=>input.value);
        const answers=buildClarificationAnswers(questions,values);
        if(!answers.trim()){notice('请至少回答一条，或直接使用下方的引导输入框。',true);return;}
        submit.disabled=true;
        await api(`/api/tasks/${current.id}/candidate`,{revision:current.revision,answers});
        await afterChange('已提交答复；应用内 AI 会带着你的答复继续组织方案。');
        await refreshCandidate();
      });
      attention.append(submit);
    }
    const jump=node('button','查看需要处理的内容 ↓','quiet');jump.type='button';
    jump.onclick=()=>{$('#advanced-task').open=true;$('#candidate-panel').scrollIntoView({block:'start'});};
    attention.append(jump);
  }
  $('#prepare-candidate').textContent=candidate?'重新准备计算方案':'生成计算方案';
  $('#prepare-candidate').hidden=current.mode!=='research'||(Boolean(candidate)&&!retriggerable);
  $('#prepare-candidate').disabled=!available.enabled||proposalRoundsExhausted(candidate);
  $('#candidate-stage').replaceChildren();
  if(candidate) {
    $('#candidate-stage').append(candidateStageChip(candidate));
    $('#candidate-stage').append(node('span',proposalRoundLabel(candidate),'subtle'));
    if(!candidateStatuses[candidate.state]) $('#candidate-stage').append(node('span','服务端状态：'+(candidate.label||candidate.state)));
  } else $('#candidate-stage').textContent='尚未准备方案';
  $('#candidate-authorization').textContent=candidate ?
    `服务端记录：execution_authorized = ${candidate.execution_authorized===true}；方案准备不代表科学验证，也不构成执行授权。` : '';
  $('#candidate-note').textContent=candidate ? `更新于 ${new Date(candidate.updated_at).toLocaleString('zh-CN')} · 记录编号 ${String(candidate.id).slice(0,8)} · 条件版本 ${candidate.revision}` :
    current.mode==='reproduction' ? '文献测试任务仍需核对输入发布与访问隔离，暂不生成方案。' :
    available.enabled ? '根据已确认条件生成结构、势函数调用与计算输入；可关闭页面，稍后查看进度。' : available.reason;
  const summary=$('#candidate-summary'); summary.replaceChildren(); $('#candidate-downloads').replaceChildren();
  $('#candidate-outcome').textContent=candidateOutcome||'';
  if(automatic && schema.task_resource_policy) summary.append(node('p',schema.task_resource_policy.description));
  if(!candidate && automatic){const r=schema.automatic_workflow.resources;summary.append(node('p',`按已确认条件生成并核对方案；你批准后才提交 HPC 和分析结果。计算资源：${r.cores} 核 · ${number(r.memory_bytes/1024**3,1)} GiB · 单次最长 ${number(r.wall_seconds/3600,2)} 小时 · 最多 ${schema.automatic_workflow.max_submissions} 次提交。`));}
  if(!candidate) {renderCandidateClarification(null);renderNextAction();return;}
  const result=candidate.result||{};
  if(result.summary) summary.append(node('p',result.summary));
  if(candidate.state==='failed') {
    const code=result.error||'';
    summary.append(node('p',(candidateErrorLabels[code]||result.message||'准备未完成')+'（'+ (code||'未提供错误码') +'）','clarification-error'),
      node('p','失败记录与已有模型回答会保留，不会自动重试；重新准备前请核对条件与服务配置。','form-note'));
  }
  if(result.message && candidate.state!=='failed') summary.append(node('p',result.message,'subtle'));
  if(result.geometry) {
    const g=result.geometry;
    summary.append(node('p',`${g.atom_count} 个原子 · ${Object.entries(g.composition).map(([element,n])=>element+' '+n).join('，')} · 仅完成几何准备`));
  }
  if(result.analysis) summary.append(node('p','拟分析：'+result.analysis.quantity+'。'+result.analysis.method));
  renderCandidateClarification(candidate);
  if(candidate.state==='prepared' && downloads_enabled) {
    for(const [name,label] of [['in.lammps','计算输入'],['structure.data','初始结构'],['analysis.json','分析说明'],['generation.json','准备记录']]) {
      const link=node('a',label+' ↓','quiet');link.href=`/api/tasks/${id}/candidate/files/${name}`;
      $('#candidate-downloads').append(link);
    }
    $('#candidate-downloads').append(node('p','文件来自已准备的方案快照；下载不代表已提交计算，也不代表科学验证通过。','form-note'));
  }
  renderNextAction();
}
$('#prepare-candidate').onclick=()=>action(async()=>{
  const id=current.id, before=candidateTask===id?candidateRecord:null;
  $('#prepare-candidate').disabled=true;
  const automatic=schema.automatic_workflow?.configured;
  $('#candidate-outcome').textContent='正在提交重新准备请求…';
  const inputs=[...document.querySelectorAll('#candidate-attention textarea[data-question-index]')];
  const perQuestion=inputs.length?buildClarificationAnswers(candidateRecord?.result?.questions||[],inputs.map(i=>i.value)):'';
  const answers=(perQuestion||$('#candidate-answers')?.value||'').trim();
  try {
    if(answers) await api(`/api/tasks/${id}/candidate`,{revision:current.revision,answers});
    else await api(`/api/tasks/${id}/${automatic?'workflow':'candidate'}`,{revision:current.revision});
  }
  finally { await refreshModelStatus(); await refreshCandidate(); await renderHistory(); }
  if(current?.id!==id) return;
  candidateOutcome=retriggerOutcome(before,candidateRecord);
  $('#candidate-outcome').textContent=candidateOutcome;
  await refreshWorkspace();
  notice(candidateOutcome);
});
setInterval(async()=>{
  if(candidatePolling || busy || !current || current.id!==candidateTask || $('#task-view').hidden ||
     !['queued','running','model_requested','reusing_plan','checking_plan','repairing_plan','preparing_files',null].includes(candidateState)) return;
  candidatePolling=true;
  try {await refreshCandidate();await renderHistory();await refreshModelStatus();await refreshPlanReview();await refreshActivity();}
  catch(error) {notice('暂时无法读取准备进度；不会重新发起模型请求。',true);}
  finally {candidatePolling=false;}
},3000);
function renderSource(container,row) {
  const labels={article_title:'论文',doi:'DOI',source_locator:'原文位置',source_page:'页码',source_excerpt:'原文',caption:'图表注',source_context:'来源上下文',value_text:'结果值（不导入）',unit:'结果单位（不导入）',finding_text:'研究发现（不导入）',method:'方法',methods_text:'方法说明',...literatureColumns};
  for (const [key,label] of Object.entries(labels)) if (row[key]) {
    container.append(node('strong',label),node('p',row[key]));
  }
}
function resetLiteraturePreview() {
  literaturePreview=null; $('#literature-preview').hidden=true;
  $('#literature-input-role').checked=false;
}
$('#import-literature').onclick=()=>{
  $('#literature-form').reset(); resetLiteraturePreview();
  const config=schema.reference_generation || {configured:false};
  $('#reference-generation-controls').hidden=current.mode!=='reproduction';
  $('#generate-reference').disabled=!config.configured;
  $('#reference-generation-note').textContent=!config.configured ? '文献自动整理尚未配置。' :
    config.model_status.remaining_requests ? '自动整理原文中的条件、结果和方法出处，无需逐项选择字段。不会启动计算。' :
    '模型额度已用完；仅能恢复同一来源之前已返回的草稿，不发送新请求。';
  $('#literature-form .dialog-error').hidden=true; $('#literature-dialog').showModal();
};
$('#generate-reference').onclick=()=>action(async()=>{
  const id=current.id, revision=current.revision, content=$('#literature-csv').value;
  $('#generate-reference').disabled=true;
  $('#reference-generation-note').textContent='正在整理文献证据。原文与请求记录会保存；不会提交计算。';
  try {
    current=await api(`/api/tasks/${id}/reference-evidence`,{revision,csv_texts:[content]});
    $('#literature-dialog').close();resetLiteraturePreview();
    await afterChange('文献证据已整理。输入条件与论文结果分别保存，尚未进行科学核验。');
  } finally {
    await refreshModelStatus();await refreshReferenceHistory();
    $('#generate-reference').disabled=!schema.reference_generation?.configured;
    $('#reference-generation-note').textContent=schema.reference_generation?.model_status?.remaining_requests ?
      '可整理新来源；重复提交同一来源将读取已有记录。' : '没有新的模型额度；已有完成回执仍可恢复。';
  }
});
$('#close-literature').onclick=()=>$('#literature-dialog').close();
$('#literature-csv').oninput=resetLiteraturePreview;
$('#literature-file').onchange=()=>action(async()=>{
  resetLiteraturePreview();
  $('#literature-csv').value='';
  const file=$('#literature-file').files[0];
  if (!file) return;
  if (file.size>65536) throw new Error('单条文献导出文件必须小于等于 64 KiB');
  $('#literature-csv').value=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(await file.arrayBuffer());
});
$('#preview-literature').onclick=()=>action(async()=>{
  resetLiteraturePreview();
  const content=$('#literature-csv').value;
  const result=await api('/api/literature/preview',{csv_text:content});
  if (content!==$('#literature-csv').value) throw new Error('文本已改变，请重新预览');
  literaturePreview={...result,csv_text:content};
  $('#literature-context').replaceChildren(); renderSource($('#literature-context'),result.row);
  $('#literature-column').replaceChildren(new Option('请选择',''));
  for (const key of Object.keys(result.available_columns)) $('#literature-column').append(new Option(literatureColumns[key],key));
  $('#literature-field').replaceChildren(new Option('请先选择来源列',''));
  $('#literature-method').replaceChildren(new Option('请选择',''));
  for (const [key,label] of Object.entries(result.method_classes)) $('#literature-method').append(new Option(label,key));
  $('#literature-basis').value=''; $('#literature-value').textContent='';
  $('#literature-preview').hidden=false;
  if (!Object.keys(result.available_columns).length) notice('这条证据没有可导入的材料或条件列；结果值不会转换为输入。',true);
});
$('#literature-column').onchange=()=>{
  const column=$('#literature-column').value;
  $('#literature-field').replaceChildren(new Option('请选择',''));
  for (const [key,label] of Object.entries(literaturePreview?.available_columns[column]||{})) $('#literature-field').append(new Option(label,key));
  $('#literature-value').textContent=literaturePreview?.row[column]||'';
  $('#literature-input-role').checked=false;
};
$('#literature-field').onchange=()=>{ $('#literature-input-role').checked=false; };
$('#literature-form').onsubmit=(event)=>{
  event.preventDefault();
  action(async()=>{
    if (!literaturePreview || literaturePreview.csv_text!==$('#literature-csv').value) throw new Error('请先预览当前来源');
    current=await api(`/api/tasks/${current.id}/literature`,{
      revision:current.revision,csv_text:literaturePreview.csv_text,source_sha256:literaturePreview.source_sha256,
      column:$('#literature-column').value,field:$('#literature-field').value,
      evidence_role:$('#literature-input-role').checked?'input':'unclear',method_class:$('#literature-method').value,
      classification_basis:$('#literature-basis').value});
    const field=$('#literature-field').value;
    $('#literature-dialog').close(); resetLiteraturePreview();
    await afterChange('文献条件已保存，原文与分类依据已保留；请核对冲突并确认。',field);
  });
};
action(async()=>{
  await refreshModelStatus();
  if(pendingRoute===null)await renderRoute(normalizeRoute(location.hash));
});

const requestStates={reserved:'已预留，未派发',dispatching:'派发中',unknown:'提交结果不明，先对账',accepted:'调度器已接受',queued:'排队中',pending:'排队中',running:'运行中',completed:'计算已结束，尚未科学核验',failed:'计算失败',cancelled:'已取消',timeout:'已超时',rejected:'提交被拒',cancel_requested:'已请求取消',cancelled_before_dispatch:'派发前取消'};
const paperEvents={bibliography_corrected:'核正论文题目与出版信息',candidate_added:'登记候选文献',paper_selected:'选入复现计划',task_linked:'关联条件任务',evaluation_linked:'关联不可重置的评测账本'};
const roleNames={agent:'主 Agent 正式评测',reference:'作者参考运行',development:'开发验证',analysis:'分析作业'};
async function showPapers() {
  hideViews('papers-view');selectNavigation('papers');
  current=null; recordRoute('#papers');
  $('#welcome').hidden=true; $('#task-view').hidden=true; $('#papers-view').hidden=false;
  await refreshPapers(); window.scrollTo({top:0});
}
async function refreshPapers() {
  const expanded=new Set([...document.querySelectorAll('[data-paper-history][open]')].map(d=>d.dataset.paperHistory));
  const expandedSources=new Set([...document.querySelectorAll('[data-paper-source][open]')].map(d=>d.dataset.paperSource));
  const result=await api('/api/papers');
  const availableTasks=(await api('/api/tasks')).tasks.filter(t=>t.mode==='reproduction');
  $('#paper-filters').replaceChildren();
  const total=Object.values(result.counts).reduce((sum,n)=>sum+n,0);
  for (const [key,label] of Object.entries({all:'全部已选',...result.statuses})) {
    const b=node('button',`${label} ${key==='all'?total:result.counts[key]}`,paperFilter===key?'primary':'quiet');
    b.setAttribute('aria-pressed',String(paperFilter===key));
    b.onclick=()=>action(async()=>{paperFilter=key;await refreshPapers();});
    $('#paper-filters').append(b);
  }
  $('#paper-list').replaceChildren(); $('#candidate-list').replaceChildren();
  const selected=result.papers.filter(p=>(p.title+' '+p.doi).toLowerCase().includes($('#paper-search').value.toLowerCase())&&p.selection==='selected'&&(paperFilter==='all'||p.status===paperFilter));
  if (!selected.length) $('#paper-list').append(node('p',paperFilter==='reproduced'?'尚无通过独立科学核验的复现记录。':'此分类还没有选定文章。','paper-empty'));
  for (const p of selected) $('#paper-list').append(paperCard(p,result.statuses,availableTasks));
  for (const p of result.papers.filter(p=>p.selection==='candidate')) $('#candidate-list').append(paperCard(p,result.statuses,availableTasks));
  $('#candidate-heading').textContent=`候选文章 · 尚未选定 ${result.candidate_count}`;
  for (const d of document.querySelectorAll('[data-paper-history]')) d.open=expanded.has(d.dataset.paperHistory);
  for (const d of document.querySelectorAll('[data-paper-source]')) d.open=expandedSources.has(d.dataset.paperSource);
  $('#paper-updated').textContent='最近读取：'+new Date().toLocaleString('zh-CN')+' · 仅在此页面每 15 秒读取记录';
}
function paperCard(p,statuses,availableTasks) {
  const card=node('article',undefined,'paper-card');
  const top=node('div',undefined,'paper-card-top');
  const link=node('a',p.title); link.href=p.doi_url; link.target='_blank'; link.rel='noopener noreferrer';
  top.append(link,node('span',p.selection==='candidate'?'候选 · 尚未选定':statuses[p.status],'badge '+(p.status==='reproduced'?'confirmed':'pending')));
  card.append(top,node('p','DOI '+p.doi,'subtle'),node('p','拟复现范围：'+p.scope),node('p',p.note,'paper-note'),node('p',p.stage,'subtle'));
  if(p.tasks.length){const go=node('button','查看结果与进度 →','primary');go.onclick=()=>requestRoute('#'+p.tasks[0].id);card.append(go);}
  if (p.source_discovery) {
    const sources=node('details'); sources.dataset.paperSource=p.id; sources.append(node('summary','论文源码检索'));
    sources.append(node('p',p.source_discovery.state==='partial'?'检索有未完成项，不能据此判定没有源码。':'已完成本轮有限检索，未覆盖全部仓库。','subtle'));
    for (const c of p.source_discovery.candidates) {
      const source=node('a',c.repository); source.href=c.url; source.target='_blank'; source.rel='noopener noreferrer';
      sources.append(source,node('p',(c.association==='doi_and_title'?'README 题目与 DOI 相符':'关联尚未确认')+' · 尚未验证作者身份或复现结果'),node('small','源码版本 '+c.commit+' · 许可 '+(c.license_spdx||'未声明')));
    }
    if (!p.source_discovery.candidates.length) sources.append(node('p','本轮未取得可核查的仓库信息。'));
    card.append(sources);
  }
  if (p.potential_acquisition) {
    const r=p.potential_acquisition;
    const resources=node('section',undefined,'paper-note'); resources.append(node('h4','势函数准备'));
    resources.append(node('p',`已获取 ${r.files.length} 个势函数文件 · 找到 ${r.bindings.length} 组调用配置`));
    for (const f of r.files) {
      const link=node('a',f.path); link.href=f.source_url; link.target='_blank'; link.rel='noopener noreferrer';
      resources.append(link,node('small',` · ${f.size} 字节 · SHA256 ${f.sha256}`));
      resources.append(node('br'));
    }
    for (const b of r.bindings) {
      resources.append(node('p',`${b.input_path} · MEAM · 元素索引 ${b.elements.join('、')} · 原子类型 ${b.type_elements.join('、')}`));
      resources.append(node('p',b.static_status==='checked'?'文件格式已检查，科学适用性尚未验证。':'文件检查发现待处理项，原文件已保留。'));
      if (b.inspection?.warnings?.length) {
        const notes=node('div'); notes.append(node('p',`保留 ${b.inspection.warnings.length} 项参数提示：`));
        for (const warning of b.inspection.warnings) {
          if (warning.startsWith('zero_atomic_number:')) notes.append(node('p',`${warning.split(':')[1]} 的库文件原子序号为 0，原值保留。`));
        }
        for (const item of b.inspection.reassignments||[]) notes.append(node('p',`${item.parameter} 在第 ${item.line} 行再次赋值：${item.previous} → ${item.value}。文件顺序保持不变。`));
        resources.append(notes);
      }
    }
    resources.append(node('p',r.license_status==='text_found_scope_unverified'?'已保存许可文本，适用范围待核实。':'所检查的目录未发现许可文件，未授权重新分发。'));
    resources.append(node('p',p.evaluations.some(e=>e.available&&e.dispatch_claims)?'实际运行与环境证据见关联任务。':'实际计算验证见关联任务。'));
    if (r.state==='partial') resources.append(node('p','本轮获取有未完成项，失败记录已保留。'));
    card.append(resources);
  }
  if (p.reference_resources) {
    const r=p.reference_resources;
    const resources=node('section',undefined,'paper-note'); resources.append(node('h4','超算参考资料'));
    resources.append(node('p',r.state==='unknown'?'准备状态待核实，暂不重复启动。':r.source_complete?`作者仓库的 ${r.files.length} 个文件已在超算获取并校验。`:'本轮参考资料未完整获取，已保留失败记录。'));
    resources.append(node('p',p.evaluations.some(e=>e.available&&e.dispatch_claims)?'作者原始文件已保留；实际计算进展见关联任务。':'作者原始文件已保留；尚未关联运行记录。'));
    card.append(resources);
  }
  if (p.engine_preparation && !p.evaluations.some(e=>e.available&&e.dispatch_claims)) {
    const r=p.engine_preparation;
    const environment=node('section',undefined,'paper-note'); environment.append(node('h4','计算环境准备'));
    environment.append(node('p',`LAMMPS ${r.requirements.release} · MEAM · ${r.requirements.cores} 核方案`));
    environment.append(node('p',r.state==='source_ready'?'官方源码已在超算获取并校验，等待在记账的计算资源内构建。':r.state==='unknown'?'超算准备状态待核实，暂不重复启动。':'本轮源码准备失败，下载及检查记录已保留。'));
    environment.append(node('p','尚未完成引擎构建或计算节点验证。'));
    card.append(environment);
  }
  if (p.selection==='candidate') {
    const select=node('button','加入复现计划','quiet');
    select.setAttribute('aria-label','选定文献：'+p.title);
    select.onclick=()=>action(async()=>{await api(`/api/papers/${p.id}/select`,{revision:p.revision});await refreshPapers();notice('已加入复现计划。任务条件与评分仍需冻结；没有提交计算。');});
    card.append(select);
  }
  const details=node('details',undefined,'paper-history'); details.dataset.paperHistory=p.id; details.append(node('summary','历史记录与提交次数'));
  const timeline=node('ol');
  for (const e of p.history) timeline.append(node('li',`${new Date(e.at).toLocaleString('zh-CN')} · ${e.event.startsWith('source_search_completed:')?'检索论文源码':e.event.startsWith('potential_acquired:')?'准备势函数资源':e.event.startsWith('reference_resources_prepared:')?({'finished':'超算参考资料准备完成','partial':'超算参考资料准备有未完成项','unknown':'超算参考资料准备待核实'}[e.event.split(':').at(-1)]||'准备超算参考资料'):e.event.startsWith('engine_source_prepared:')?({'failed':'计算环境准备失败','source_ready':'超算源码准备完成','unknown':'计算环境准备待核实'}[e.event.split(':').at(-1)]||'准备计算环境'):paperEvents[e.event.split(':')[0]]||e.event} · 文献版本 ${e.revision}`));
  details.append(timeline);
  for (const task of p.tasks) {
    const button=node('button','打开研究任务：'+task.title,'quiet'); button.onclick=()=>requestRoute('#'+task.id); details.append(button);
    const revisions=node('ol');
    for (const e of task.history) {
      const [kind,...parts]=e.event.split(':');
      const field=parts.filter(part=>part!=='initial_geometry_invalidated').join(',');
      const label={created:'建立任务',candidate_added:'补充条件',condition_selected:'选择条件',user_confirmed:'确认条件',targets_selected:'选择复现目标',target_inventory_assessed:'整理图表工况与资源',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件',reference_evidence_generated:'整理文献证据',initial_geometry_selected:'选择固定初始结构',initial_geometry_cleared:'清除初始结构选择'}[kind]||kind;
      revisions.append(node('li',`${new Date(e.at).toLocaleString('zh-CN')} · 条件版本 ${e.revision} · ${label}${field?' · '+field.split(',').map(k=>schema.fields[k]||k).join('、'):''}${e.event.includes(':initial_geometry_invalidated')?' · 原结构选择已撤销，需重新核对':''}`));
    }
    details.append(revisions);
  }
  if (!p.evaluations.length) details.append(node('p','尚未关联正式评测，暂无提交次数记录。默认上限为每次独立评测 2 次；候选筛选和条件修订不算计算提交。','subtle'));
  for (const evaluation of p.evaluations) {
    if (!evaluation.available) { details.append(node('p','账本记录暂不可读；次数未知，不能重新提交。','error')); continue; }
    details.append(node('h3',roleNames[evaluation.identity.role]||evaluation.identity.role));
    details.append(node('p',evaluation.max_attempts===null
      ? `参考开发已获准继续至跑通 · 已记账派发 ${evaluation.dispatch_claims} 次 · 全部失败与费用保留`
      : `额度已占用 ${evaluation.reserved_attempts} / ${evaluation.max_attempts} · 已记账派发 ${evaluation.dispatch_claims} · 剩余 ${evaluation.remaining_attempts}`));
    details.append(node('small','评测标识：'+evaluation.id));
    for (const [i,request] of evaluation.requests.entries()) {
      const requestBox=node('div',undefined,'request-record');
      requestBox.append(node('p',`第 ${i+1} 次 · ${requestStates[request.state]||request.state}`),node('small','记录 '+request.id+(request.job_id?' · 作业 '+request.job_id:' · 尚无确认作业号')));
      requestBox.append(node('p',request.accounted?`已核算 ${request.actual_core_seconds} 核秒`:`核时尚未最终对账，当前预留 ${request.charge_core_seconds} 核秒`));
      const events=node('ol'); for (const e of request.events) events.append(node('li',`${new Date(e.at*1000).toLocaleString('zh-CN')} · ${e.kind}`));
      requestBox.append(events); details.append(requestBox);
    }
  }
  const choices=availableTasks.filter(t=>!p.tasks.some(x=>x.id===t.id));
  if (choices.length) {
    const form=node('form',undefined,'paper-task-link'),label=node('label','关联已有复现任务'),select=node('select');
    select.required=true;select.setAttribute('aria-label','为'+p.title+'关联任务');select.append(new Option('请选择任务',''));
    for (const t of choices) select.append(new Option(t.title,t.id));
    const submit=node('button','关联任务','quiet');submit.type='submit';label.append(select);form.append(label,submit);
    form.onsubmit=e=>{e.preventDefault();action(async()=>{await api(`/api/papers/${p.id}/tasks`,{revision:p.revision,task_id:select.value});await refreshPapers();});};
    details.append(form);
  }
  const technical=node('details',undefined,'paper-resources');technical.append(node('summary','资源来源与准备记录'));
  for(const section of Array.from(card.children).filter(e=>e.tagName==='SECTION'||e.dataset.paperSource))technical.append(section);
  if(technical.children.length>1)card.append(technical);
  card.append(details); return card;
}
setInterval(()=>{if(location.hash==='#papers'&&!busy&&!document.querySelector('dialog[open]')&&!$('#papers-view').contains(document.activeElement))action(refreshPapers);},15000);

// Researcher workspace. Views read existing evidence; no controls dispatch jobs.
function hideViews(show){$('#go-home').setAttribute('aria-current',show==='home-view'?'page':'false');$('.search-box').hidden=false;$('#task-loading').hidden=true;for(const id of ['home-view','welcome','tasks-view','papers-view','resources-view','help-view','task-view']) $('#'+id).hidden=id!==show;}
function selectNavigation(name){
  for(const [id,key] of Object.entries({'top-new':'new','top-tasks':'tasks','top-papers':'papers','top-resources':'resources','top-help':'help'})){
    $('#'+id).classList.toggle('selected',name===key);
    if(name===key)$('#'+id).setAttribute('aria-current','page');else $('#'+id).removeAttribute('aria-current');
  }
}
function setMode(mode){
  $('#create-form [name=mode]').value=mode;
  $('#mode-research').classList.toggle('selected',mode==='research');$('#mode-reproduction').classList.toggle('selected',mode==='reproduction');
  $('#mode-research').setAttribute('aria-pressed',String(mode==='research'));$('#mode-reproduction').setAttribute('aria-pressed',String(mode==='reproduction'));
  $('#reproduction-hint').hidden=mode!=='reproduction';
}
function taskPhaseLabels(task){
 const independent={...task,reference_state:null};
 const hasReference=Boolean(task.reference_stage||task.reference_state||task.reference_available!==undefined);
 if(!hasReference)return [{state:taskState(task),label:taskStateLabel(task)}];
 const referenceLabels={prepared:'暂无作业回执',running:'运行中',queued:'排队中',accepted:'已提交',completed:'计算结束 · 科学结果待核验',failed:'计算失败',timeout:'超时',unknown:'提交结果待核对',reconcile_required:'记录暂不可读，保留已有提交'};
 const referenceLabel=referenceLabels[task.reference_state];
 return [
  {state:task.reference_state||'unknown',label:referenceLabel?'作者参考 A · '+referenceLabel:task.reference_stage||'作者参考 A · 状态待核对'},
  {state:taskState(independent),label:(task.execution_state?'B 计算 · ':'B 方案 · ')+taskStateLabel(independent)},
 ];
}
function taskNextLabel(task){
 const state=taskState(task);
 if(['running','queued','accepted','dispatching','preparing','understanding'].includes(state))return '查看进度';
 if(['completed','validated'].includes(state))return '查看结果';
 if(['failed','timeout','preparation_failed','condition_failed','condition_attention','clarification','reconcile_required','unknown'].includes(state))return '查看问题';
 if(state==='prepared')return '确认方案';
 if(state==='draft')return task.condition_preparation_state==='imported'
  ? Number(task.outstanding)>0?'让应用 AI 补齐条件':'核对并确认条件'
  :'整理需求';
 if(state==='frozen')return task.mode==='reproduction'?'查看 P–A–B 进度':'准备方案';
 return '查看任务';
}
function taskMatchesFilter(task,filter){
 const state=taskState(task);
 if(filter==='all')return true;
 if(filter==='attention')return ['draft','frozen','prepared','clarification','condition_attention','condition_failed','preparation_failed','failed','timeout','reconcile_required','unknown','completed'].includes(state);
 if(filter==='draft')return ['draft','frozen','prepared','preparing','understanding','clarification','condition_attention'].includes(state);
 if(filter==='failed')return ['failed','timeout','preparation_failed','condition_failed'].includes(state);
 if(filter==='finished')return taskFinished(task);
 return state===filter;
}
function taskRecordActionBlocker(task){
 const active=new Set(['reserved','dispatching','accepted','queued','pending','running','waiting','cancel_requested']);
 const uncertain=new Set(['unknown','reconcile_required','attention','interrupted','uncertain','needs_reconciliation']);
 const states=[task.execution_state,task.reference_state,task.preparation_state,task.condition_preparation_state];
 if(states.some(state=>uncertain.has(state)))return '运行记录待核对，暂不能结束或删除。';
 if(states.some(state=>active.has(state))||['generating','validating','repairing','model_requested','reusing_plan','checking_plan','repairing_plan','preparing_files'].includes(task.preparation_state)||['generating','validating','repairing'].includes(task.condition_preparation_state))return '任务仍在准备或计算，暂不能结束或删除。';
 return '';
}
function taskCards(){
 const box=$('#task-cards');box.replaceChildren();statsFor(taskCache,$('#task-stats'));
 const filters=$('#task-filters');filters.replaceChildren();for(const [key,label] of Object.entries({all:'全部任务',attention:'待我处理',running:'运行中',queued:'排队中',completed:'计算结束',validated:'验收通过',finished:'已确认结束',failed:'失败',draft:'待准备'})){const b=node('button',label,taskFilter===key?'primary':'quiet');b.onclick=()=>{taskFilter=key;taskCards();};filters.append(b);}
 const tasks=taskCache.filter(t=>t.title.toLowerCase().includes(($('#task-search').value||'').toLowerCase())&&taskMatchesFilter(t,taskFilter));
 if(!tasks.length){box.append(emptyState('此分类暂无任务','新建一项研究，或调整搜索条件。'));return;}
 const table=node('table',undefined,'research-table'),head=node('thead'),hr=node('tr'),body=node('tbody');for(const label of ['任务','当前阶段','下一步','作业 / 提交次数','最近更新','操作'])hr.append(node('th',label));head.append(hr);table.append(head,body);
 for(const t of tasks){
 const tr=node('tr'),info=node('td'),title=node('strong',t.title);
 info.append(title,node('small',t.mode==='reproduction'?'文献验证':'科研计算'));
 const progress=node('td'),phases=taskPhaseLabels(t);
 for(const phase of phases){
  const line=node('div');
  line.append(node('span',phase.label,'badge '+(['validated','finished','prepared'].includes(phase.state)?'accepted':['running','preparing','queued','accepted'].includes(phase.state)?'pending':['failed','timeout','preparation_failed'].includes(phase.state)?'failed':'')));
  progress.append(line);
 }
 if(taskState(t)==='validated'&&taskFinished(t))progress.append(node('small','已确认结束'));
 const next=node('td'),nextButton=node('button',taskNextLabel(t)+' →','task-next-link');nextButton.onclick=()=>requestRoute('#'+t.id);next.append(nextButton);
 const counts=node('td');
 if(phases.length>1){
  counts.append(node('strong',t.reference_job_id?'A 作业 '+t.reference_job_id:'A 作业号待核对'),node('small',`A 提交：${t.reference_submission_count??'待核对'} 次（含失败）`),node('small',t.job_id?'B 作业 '+t.job_id:'B 暂无作业回执'));
 }else counts.append(node('span',t.job_id?'作业 '+t.job_id:'暂无作业回执'));
 if(t.submission_count!==undefined)counts.append(node('small',(phases.length>1||t.mode==='reproduction'?'B 提交：':'提交：')+t.submission_count+' / '+(t.max_submissions||2)));
 const changed=node('td',t.updated_at?new Date(t.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'),actions=node('td'),go=node('button','查看详情','quiet');go.onclick=()=>requestRoute('#'+t.id);actions.append(go);
 const manage=async operation=>{await api(`/api/tasks/${t.id}/lifecycle`,{revision:t.revision,lifecycle_revision:t.lifecycle_revision||0,action:operation});await listTasks();taskCards();notice(operation==='delete'?'任务已从列表删除；计算账本和费用记录保留。':'已记录你确认任务结束；科学验收结论保持独立。');};
 const lifecycleBlocker=taskRecordActionBlocker(t);
 if(lifecycleBlocker)actions.append(node('small',lifecycleBlocker,'form-note'));
 else{
  if(!taskFinished(t))actions.append(removalButton('确认任务结束','再次点击确认结束',()=>manage('finish')));
  actions.append(removalButton('删除任务记录','确认从列表删除',()=>manage('delete')));
 }
 tr.append(info,progress,next,counts,changed,actions);body.append(tr);}box.append(table);

}
function markTaskListFresh(){
 const note=$('#tasks-last-sync');
 if(note)note.textContent='状态更新于 '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})+' · 页面停留时会自动更新';
}
async function showTasks(){current=null;hideViews('tasks-view');selectNavigation('tasks');recordRoute('#tasks');await listTasks();taskCards();markTaskListFresh();}
let taskSummaryRefreshing=false;
async function refreshVisibleTaskSummaries(force=false){
 const tasksVisible=!$('#tasks-view').hidden,homeVisible=!$('#home-view').hidden;
 if((!tasksVisible&&!homeVisible)||taskSummaryRefreshing||busy||document.hidden||document.querySelector('dialog[open]'))return;
 if(!force && (tasksVisible?$('#tasks-view'):$('#home-view')).contains(document.activeElement))return;
 taskSummaryRefreshing=true;
 try{
  await listTasks();
  if(tasksVisible&&!$('#tasks-view').hidden){taskCards();markTaskListFresh();}
  if(homeVisible&&!$('#home-view').hidden){
   statsFor(taskCache,$('#home-stats'));
   const computing=$('#home-status dd');
   if(computing)computing.textContent=taskCache.some(t=>taskState(t)==='running')?'有任务正在运行':'以任务记录为准';
  }
 }catch(error){
  if(tasksVisible&&!$('#tasks-view').hidden)$('#tasks-last-sync').textContent='状态暂未更新：'+error.message+'。点击“刷新状态”重试；已有列表保留。';
 }finally{taskSummaryRefreshing=false;}
}
$('#tasks-refresh').onclick=()=>refreshVisibleTaskSummaries(true);
setInterval(()=>refreshVisibleTaskSummaries(),30000);
let resourceFilter='all',resourceRows=[];
function potentialResourceRows(p){
 const report=p.potential_acquisition||{},files=report.files||[];
 return files.map(f=>{
  const bindings=(report.bindings||[]).filter(b=>Object.values(b.files||{}).includes(f.path));
  const unique=values=>[...new Set(values.filter(Boolean))];
  const elements=unique(bindings.flatMap(b=>b.elements||[]));
  const styles=unique(bindings.map(b=>b.pair_style));
  const roles=unique(bindings.flatMap(b=>Object.entries(b.files||{}).filter(([,path])=>path===f.path).map(([role])=>role)));
  const roleNames={library:'元素基础参数库',parameters:'合金相互作用参数'};
  const family=styles.length?styles.map(x=>x.toUpperCase()).join(' / '):'类型待核验';
  const purpose=roles.length?roles.map(x=>roleNames[x]||x).join(' / '):'文件用途待核验';
  const companions=unique(bindings.flatMap(b=>Object.values(b.files||{}))).filter(path=>path!==f.path);
  const names={Nb:'铌',Ti:'钛',Zr:'锆',Mo:'钼',V:'钒',Fe:'铁',Cu:'铜',Al:'铝',W:'钨',Ta:'钽',Ni:'镍',Co:'钴',Cr:'铬',Si:'硅',C:'碳',O:'氧',H:'氢'};
  const details=[];
  if(bindings.length)details.push('元素依据：固定来源中调用配置的所选元素；不代表已验证所有合金组成。');
  else details.push('尚无与此文件对应的调用配置，不能仅凭文件名推断元素或类型。');
  for(const mapping of unique(bindings.map(b=>(b.type_elements||[]).join(' → '))))details.push('原子类型顺序：'+mapping);
  for(const units of unique(bindings.map(b=>b.units)))details.push('LAMMPS 单位制：'+units);
  for(const pack of unique(bindings.map(b=>b.required_package)))details.push('所需引擎组件：'+pack);
  const warnings=unique(bindings.flatMap(b=>b.inspection?.warnings||[])),blockers=unique(bindings.flatMap(b=>b.inspection?.blockers||[]));
  if(warnings.length)details.push('静态检查提示：'+warnings.join('；'));
  if(blockers.length)details.push('待解决项：'+blockers.join('；'));
  const license=report.license_status;
  details.push('许可：'+(license==='not_declared_in_scanned_tree'?'已扫描来源未声明许可证':license||'待核验'));
  details.push('已保存来源与摘要；当前条目不代表科学适用性已验证。');
  return {kind:'potential',name:`${elements.join('–')||'元素待核验'} · ${family} ${purpose}`,filename:f.path,
   type:family,elements:elements.map(e=>names[e]?`${e}（${names[e]}）`:e).join(' · ')||'元素待核验',
   tags:[purpose,companions.length?'需配套使用':'配套关系待核验'],companion:companions.join('、'),
   version:report.commit||f.version||'',paper:p.title,doi:p.doi,url:f.source_url,hash:f.sha256,
   state:blockers.length?'存在待解决项':bindings.length&&bindings.every(b=>b.static_status==='checked')?'已获取 · 静态检查记录已保存':'已获取 · 元数据待核验',
   note:details.join('\n')};
 });
}
function potentialBundleRows(p){
 const files=potentialResourceRows(p),used=new Set(),groups=new Map();
 for(const binding of p.potential_acquisition?.bindings||[]){
  const paths=[...new Set(Object.values(binding.files||{}))].sort();if(!paths.length)continue;
  const key=JSON.stringify([paths,binding.elements,binding.pair_style]);if(groups.has(key))continue;
  const present=files.filter(f=>paths.includes(f.filename));if(!present.length)continue;
  const first=present[0],missing=paths.filter(path=>!present.some(f=>f.filename===path));
  present.forEach(f=>used.add(f.filename));
  groups.set(key,{...first,name:`${(binding.elements||[]).join('–')||'元素待核验'} · ${(binding.pair_style||'类型待核验').toUpperCase()}`,
   filename:'',companion:paths.join('、'),bundle:true,files:present,tags:[`${present.length} 个配套文件`,missing.length?'存在缺项':'配套文件已取得'],
   state:missing.length?'缺少 '+missing.join('、'):first.state,
   note:'元素覆盖不代表所有温度、成分或研究性质适用。\n'+first.note});
 }
 return [...groups.values(),...files.filter(f=>!used.has(f.filename))];
}
let discoveryLibraryNote='',resourceTab='potential',resourceFacets={elements:'',style:'',source:'',role:'',process:''},resourceStats=[];
const RESOURCE_TABS=[
 ['potential','势函数库','按元素体系与势函数类型命名的势函数资源；论文题名与 DOI 保留在来源一列。'],
 ['paper','论文案例','论文配套源码与可复现任务；只展示题名与 DOI 已匹配的关联，关联证据不等于作者身份核验。'],
 ['structure','结构模板','可复用结构模板尚未接入：需先取得结构文件并核验来源与许可。'],
 ['script','脚本片段','脚本片段尚未接入：不展示未经验证的脚本，避免把作者解法或示例当作可复用资产。'],
 ['dataset','数据集','数据集尚未接入：需先完成来源、许可与摘要核验。'],
 ['tools','工具与软件','引擎与后处理工具的官方入口；实际版本与能力以任务环境记录为准。'],
];
const RESOURCE_COLUMNS={
 potential:['资源名称','元素体系','势函数类型','来源','验证状态','依据与文件'],
 paper:['资源名称','研究过程','论文与 DOI','来源','验证状态','依据与文件'],
 tools:['资源名称','类型','来源','说明'],
};
function resourceTabMeta(key){return RESOURCE_TABS.find(t=>t[0]===key)||RESOURCE_TABS[0];}
function resourceFacetGroups(){
 if(resourceTab==='potential')return [['elements','元素体系'],['style','势函数类型'],['source','数据来源'],['role','仓库角色']];
 if(resourceTab==='paper')return [['process','研究过程'],['source','数据来源'],['role','仓库角色']];
 return [];
}
function resourceFacetOf(r,group){
 if(group==='elements'){const n=(r.elementList||[]).length;return n===0?'元素待核验':n===1?'单元素':n===2?'二元':n===3?'三元':'多元（≥4）';}
 if(group==='style'){const s=(r.styles||[])[0];return s?s.toUpperCase():'类型待核验';}
 if(group==='process'){const p=(r.process||[])[0];return p||'过程待核';}
 if(group==='source'){return ({github:'文献仓库',nist_ipr:'NIST IPR',openkim:'OpenKIM'})[r.sourceType]||'其他来源';}
 if(group==='role'){return ({author_source:'作者源码',validation_tests:'验证测试',example_suite:'示例集',artifact:'论文 artifact',potential_library:'势函数库'})[r.repositoryRole]||'未标注';}
 return '';
}
function resourceTabRows(){
 const byTab={potential:r=>r.kind==='potential',paper:r=>r.kind==='paper',tools:r=>r.kind==='tools'};
 const keep=byTab[resourceTab];return keep?resourceRows.filter(keep):[];
}
function resourceTabCounts(){
 const counts={potential:0,paper:0,tools:0};
 for(const r of resourceRows){if(counts[r.kind]!==undefined)counts[r.kind]+=1;}
 return counts;
}
function renderResourceTabs(){
 const box=$('#resource-tabs'),counts=resourceTabCounts();box.replaceChildren();
 for(const [key,label] of RESOURCE_TABS){
  const button=node('button');button.type='button';button.dataset.resourceTab=key;
  button.append(node('span',label));
  if(counts[key]!==undefined)button.append(node('span',String(counts[key]),'resource-tab-count'));
  button.className=key===resourceTab?'selected':'';
  button.onclick=()=>{if(resourceTab===key)return;resourceTab=key;resourceFacets={elements:'',style:'',source:'',role:'',process:''};renderResourceTabs();renderResourceTable();};
  box.append(button);
 }
}
function renderResourceStats(){
 const box=$('#resource-stats');box.replaceChildren();
 for(const [label,value,note] of resourceStats){const card=node('div',undefined,'resource-stat');card.append(node('strong',value),node('span',label));if(note)card.append(node('small',note));box.append(card);}
}
function renderResourceFacets(rows){
 const box=$('#resource-facets');box.replaceChildren();
 const groups=resourceFacetGroups();
 if(!groups.length){box.append(node('p','该页签没有可筛选的维度。','form-note'));return;}
 for(const [key,title] of groups){
  const counts=new Map();for(const r of rows){const v=resourceFacetOf(r,key);counts.set(v,(counts.get(v)||0)+1);}
  const sec=node('section',undefined,'resource-facet');sec.append(node('h3',title));const ul=node('ul');
  const allButton=node('button','全部（'+rows.length+'）');allButton.type='button';allButton.className='facet-value'+(resourceFacets[key]?'':' selected');
  allButton.onclick=()=>{resourceFacets[key]='';renderResourceTable();};
  const all=node('li');all.append(allButton);ul.append(all);
  for(const [value,count] of [...counts.entries()].sort((a,b)=>b[1]-a[1]||String(a[0]).localeCompare(String(b[0]),'zh'))){
   const button=node('button',value+'（'+count+'）');button.type='button';button.className='facet-value'+(resourceFacets[key]===value?' selected':'');
   button.onclick=()=>{resourceFacets[key]=resourceFacets[key]===value?'':value;renderResourceTable();};
   const li=node('li');li.append(button);ul.append(li);
  }
  sec.append(ul);box.append(sec);
 }
}
function resourceDetail(r){
 const d=node('details');d.append(node('summary','查看依据'));
 if(r.paper)d.append(node('p',r.paper));
 if(r.doi){const a=node('a','DOI '+r.doi);a.href='https://doi.org/'+r.doi;a.target='_blank';a.rel='noopener noreferrer';d.append(a);}
 if(r.version)d.append(node('p','固定版本：'+r.version));
 for(const line of (r.note||'').split('\n'))if(line)d.append(node('p',line));
 if(r.files){d.append(node('h4','配套文件与来源'));for(const f of r.files){d.append(node('p',f.filename,'mono'));if(f.hash)d.append(node('code','SHA-256 '+f.hash));const link=node('a','查看该文件来源');link.href=f.url;link.target='_blank';link.rel='noopener noreferrer';d.append(link);}}
 else if(r.hash)d.append(node('code','SHA-256 '+r.hash));
 if(r.url){const a=node('a','原始来源 ↗');a.href=r.url;a.target='_blank';a.rel='noopener noreferrer';d.append(a);}
 return d;
}
function renderResourceTable(){
 const box=$('#resource-cards');box.replaceChildren();
 const meta=resourceTabMeta(resourceTab),tabRows=resourceTabRows();
 const query=$('#resource-search').value.trim().toLowerCase();
 renderResourceStats();renderResourceFacets(tabRows);
 const rows=tabRows.filter(r=>Object.entries(resourceFacets).every(([k,v])=>!v||resourceFacetOf(r,k)===v)
   &&[r.name,r.filename,r.type,r.paper,r.doi,r.elements,r.companion,...(r.tags||[])].join(' ').toLowerCase().includes(query));
 const notice=node('div',undefined,'resource-tab-notice');
 notice.append(node('strong',meta[1]),node('p',meta[2]));
 $('#resource-count').textContent=rows.length
   ? `当前页签 ${rows.length} 条（筛选前 ${tabRows.length} 条；资源目录合计 ${resourceRows.filter(r=>r.discovery).length} 条发现记录 + ${resourceRows.filter(r=>!r.discovery).length} 条已登记资源）`
   : `当前页签 0 条（筛选前 ${tabRows.length} 条）`;
 box.append(notice);
 if(discoveryLibraryNote)box.append(node('p',discoveryLibraryNote,'form-note'));
 if(!rows.length){
  box.append(tabRows.length
    ? emptyState('没有匹配的资源','调整关键词或左侧筛选。')
    : emptyState('该页签尚未接入真实条目',meta[2]));
  return;
 }
 const columns=RESOURCE_COLUMNS[resourceTab]||RESOURCE_COLUMNS.potential;
 const table=node('table',undefined,'research-table'),head=node('thead'),hr=node('tr'),body=node('tbody');
 for(const label of columns)hr.append(node('th',label));head.append(hr);table.append(head,body);
 for(const r of rows){
  const tr=node('tr');tr.dataset.resourceKind=r.kind;tr.dataset.resourceState=r.state;
  const name=node('td');name.append(node('strong',r.name));
  if(r.filename)name.append(node('small','原始文件：'+r.filename,'mono'));
  if(r.tags?.length){const tags=node('div',undefined,'resource-tags');for(const tag of r.tags)tags.append(node('span',tag,'badge'));name.append(tags);}
  if(r.companion&&!r.bundle)name.append(node('small','配套文件：'+r.companion));
  const source=node('td',(r.sourceType?({github:'文献仓库',nist_ipr:'NIST IPR',openkim:'OpenKIM'}[r.sourceType]||r.sourceType):'已登记资源')
    +(r.repositoryRole&&r.repositoryRole!=='author_source'?' · '+r.repositoryRole:''));
  const state=node('td');state.append(node('span',r.state,(r.state||'').includes('候选')?'badge':'badge warn'));
  if(resourceTab==='tools'){tr.append(name,node('td',r.type),source,resourceDetail(r));}
  else{
   const second=resourceTab==='potential'?node('td',r.elements||'—'):node('td',(r.process||[]).join('、')||'过程待核');
   const third=resourceTab==='potential'?(r.styles||[]).length?r.styles.join(' / ').toUpperCase():'—':(r.paper||'论文待核');
   tr.append(name,second,node('td',third),source,state,resourceDetail(r));
  }
  body.append(tr);
 }
 box.append(table);
}
async function showResources(){
 current=null;hideViews('resources-view');selectNavigation('resources');recordRoute('#resources');
 const result=await api('/api/papers');resourceRows=[];resourceStats=[];let discovered;
 try{discovered=await api('/api/resource-discoveries');}catch(error){discoveryLibraryNote='发现清单暂时无法读取，已登记的资源仍保留。';}
 renderResourceTabs();
 for(const p of result.papers){
  resourceRows.push(...potentialBundleRows(p));
  for(const c of (p.source_discovery?.candidates||[]).filter(c=>c.association==='doi_and_title'))resourceRows.push({kind:'paper',name:c.repository,type:'论文源码',elements:'',elementList:[],styles:[],version:c.commit?.slice(0,12),paper:p.title,doi:p.doi,url:c.url,state:'题目与 DOI 相符',note:'关联证据不等于作者身份核验。源码仅供参考端验证，不提供给独立评测生成者。'});
 }
 if(discovered?.configured){
  const dRows=discovered.row_count??discovered.entries.length,dPot=discovered.entries.filter(d=>d.kind==='potential').length,dSrc=discovered.entries.filter(d=>d.kind==='author_source').length;
  resourceStats=[['资源行',String(dRows),`势函数 ${dPot} · 论文源码 ${dSrc}`],
   ['去重资源包',String(discovered.bundle_count??'—'),'拆行不增加论文数'],
   ['仓库（含镜像）',String(discovered.repository_count),'含外部注册表来源'],
   ['论文 DOI',String(discovered.paper_doi_count??'—'),`已核验关联 ${discovered.verified_doi_count??'—'}`]];
  discoveryLibraryNote=`发现目录：${dRows} 条资源行 · 去重资源包 ${discovered.bundle_count??'未提供'} · 仓库 ${discovered.repository_count}（含镜像）· 论文 DOI ${discovered.paper_doi_count??'未提供'}（已核验关联 ${discovered.verified_doi_count??'未提供'}）· 更新时间 ${discovered.source.generated} · 来源版本 ${discovered.source.sha256.slice(0,12)}。候选分级与关联来自资源发现记录，尚未完成应用内运行验证。`;
  const roleType={author_source:'论文源码（发现候选）',validation_tests:'验证测试（发现候选）',example_suite:'示例集（发现候选）',artifact:'论文 artifact（发现候选）',potential_library:'势函数库（发现候选）'};
  const doiNote={verified_association:'题名与 DOI 关联已核验',candidate_unverified:'DOI 为候选关联，待核验',artifact_or_dataset:'所附为 artifact/数据集 DOI，不代表论文',absent:'缺论文 DOI'};
  for(const d of discovered.entries){
   const isPotential=d.kind==='potential';
   const elementDetail=isPotential?(d.elements.length?(d.element_label||d.elements.join(' · '))+(d.elements_complete?'':'（证据被截断，未构成完整映射）')+'（发现方标注，待核验）':'元素与类型映射待核验'):((d.process||[]).join('、')||'过程待核');
   resourceRows.push({kind:isPotential?'potential':'paper',discovery:true,hasPotential:isPotential,discoveryKind:d.kind,
    name:d.title,type:roleType[d.repository_role]||roleType.author_source,elements:elementDetail,
    elementList:d.elements||[],styles:d.pair_styles||[],sourceType:d.source_type,repositoryRole:d.repository_role,
    version:d.source_type==='github'?d.commit:d.source_type+' '+d.commit,paper:d.paper_title,doi:d.doi,url:d.url,
    state:d.state==='conflict'?'关联冲突':d.state==='missing_resources'?'资源有缺项':(isPotential&&d.potential_basis==='built_in_analytic'?'内建解析势 · 待核验':'候选 · 待核验'),
    tags:[...(d.pair_styles||[]),d.source_tier.replace('tier_','发现方分级 ').toUpperCase(),d.doi_state==='artifact_or_dataset'?'artifact DOI':''].filter(Boolean),
    note:[d.repository,d.description,'来源类型：'+d.source_type+' · 仓库角色：'+d.repository_role,'关联依据（发现方）：'+d.evidence,'论文关联：'+d.paper_title+'（'+(doiNote[d.doi_state]||d.doi_state)+'）','许可标注：'+d.license+(d.license_state?'（'+d.license_state+'）':''),'LAMMPS入口：'+(d.input_files.join('、')||'未登记'),'势函数文件：'+(d.potential_files.join('、')||'未登记'),isPotential&&d.potential_library_elements.length?'势库元素顺序：'+d.potential_library_elements.join('、'):'',isPotential&&d.type_order.length?'原子类型顺序：'+d.type_order.join(' → '):'',isPotential?'原子类型顺序、势库元素顺序与体系元素集合分别记录，未合并推断':'',d.mirrors.length?'镜像：'+d.mirrors.join('、'):'','缺项：'+d.gaps.join('；'),'没有纳入独立 B 的答案或源码检索。'].filter(Boolean).join('\n')});
  }
 }else if(discovered)discoveryLibraryNote='发现目录尚未配置；下方仅为已登记资源。';
 for(const [name,url,note] of [['LAMMPS','https://docs.lammps.org/','模拟引擎；实际版本及能力以任务环境记录为准。'],['OVITO','https://www.ovito.org/','结构与轨迹分析工具；网页交互尚待接入。']])resourceRows.push({kind:'tools',name,type:'工具文档',url,note,state:'官方文档',elementList:[],styles:[]});
 renderResourceTabs();
 renderResourceTable();
}
$('#resource-search').oninput=renderResourceTable;
$('#resource-refresh').onclick=()=>action(showResources);

function showHelp(){current=null;hideViews('help-view');selectNavigation('help');recordRoute('#help');renderHelp();}
function emptyState(title,message){const box=node('div',undefined,'empty-result');box.append(node('span','◇','empty-icon'),node('h3',title),node('p',message));return box;}
function number(v,digits=4){return Number.isFinite(v)?v.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:0}):'—';}
function fileSize(bytes){if(bytes<1024)return number(bytes,0)+' B';if(bytes<1024**2)return number(bytes/1024,2)+' KiB';if(bytes<1024**3)return number(bytes/1024**2,2)+' MiB';return number(bytes/1024**3,2)+' GiB';}
function currentRawFiles(){return rawResult?.task===current?.id?rawResult.files:[];}
function duration(seconds){const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=Math.floor(seconds%60);return `${h} 小时 ${m} 分 ${s} 秒`;}
function addInfo(label,value){$('#task-information').append(node('dt',label),node('dd',value));}
function acceptanceLabel(acceptance){
  const status=acceptance?.status;
  return {accepted_by_user:'基准工况 · 用户验收',accepted:'基准工况 · 已验收',pending:'验收待确认',rejected:'验收未通过'}[status]||('验收状态：'+(status||'未记录'));
}
function scientificLabel(closeout){
  return `科学状态：${closeout?.scientific_status||'未记录'}${closeout?.formal_blind?' · 已盲测':' · 未盲测（formal_blind 非真）'}`;
}
function bState(report){if(report.closeout)return acceptanceLabel(report.closeout.acceptance)+' · '+scientificLabel(report.closeout);return report.agent_progress?.stage || '记录暂不可核验';}
function bCount(report){return report.agent_progress?.available ? `${report.agent_progress.dispatch_claims} / 2` : '暂不可核验';}
function renderFlow(report){
 let box=$('#execution-flow');box.replaceChildren();if(report){const details=node('details',undefined,'reference-flow');details.append(node('summary','查看作者参考 A 的执行阶段'));box.append(details);box=details;}const head=node('div',undefined,'flow-heading');head.append(node('h2',report?'作者参考 A · 执行流程':'任务执行流程'),node('span',report?'完整运行 '+duration(report.runtime.elapsed_seconds):'等待模型与执行服务就绪'));box.append(head);
 const labels=['需求理解','结构准备','势函数','计算脚本','提交 HPC','运行结束','结果分析'];const steps=node('ol',undefined,'flow-steps');
 for(let i=0;i<labels.length;i++){const done=!!report&&report.stages[i]?.state==='completed';const li=node('li',undefined,done?'done':i===0?'current':'');li.append(node('span',done?'✓':String(i+1)),document.createTextNode(labels[i]));steps.append(li);}box.append(steps,node('p',report?`参考 A 已结束并完成诊断分析；B：${bState(report)}。${report.closeout?acceptanceLabel(report.closeout.acceptance)+'；该记录只覆盖上述范围，未覆盖工况不作为已复现。':'科学结论仍待核验。'}`:'需求已保存。缺项会集中说明；当前不会自动提交计算。','flow-note'));
}
function metricTable(metrics,compact=false){
 const table=node('table',undefined,'result-table'),head=node('thead'),tr=node('tr'),body=node('tbody');
 for(const label of compact?['性质 / GPa','P','A','|P−A|']:['性质 / 方法','P','A','abs(P−A)','相对差'])tr.append(node('th',label));head.append(tr);table.append(head,body);
 for(const m of metrics){const row=node('tr'),name=node('td',m.label,'metric-name');if(!compact)name.append(node('small',m.method));row.append(name,node('td',number(m.paper),'number'),node('td',number(m.reference),'number'),node('td',number(m.absolute_difference),'number'));if(!compact)row.append(node('td',m.relative_difference_percent===null?'未定义':number(m.relative_difference_percent,3)+'%','number'));body.append(row);}return table;
}
function curvePlot(report){
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 450 290');svg.setAttribute('class','plot-svg');svg.setAttribute('role','img');svg.setAttribute('aria-label','实际计算应力应变曲线：'+report.curves.map(c=>c.label).join('、'));
 const el=(tag,attrs,text)=>{const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;svg.append(e);return e;};
 const xmax=report.xmax||.5, curves=report.curves.map(c=>({...c,points:c.points.filter(p=>p[0]<=xmax)})),ys=curves.flatMap(c=>c.points.map(p=>p[1])),max=Math.ceil(Math.max(...ys,1)/2)*2,low=Math.min(0,Math.floor(Math.min(...ys))),left=52,right=430,top=18,bottom=244;
 const sx=x=>left+x/xmax*(right-left),sy=y=>bottom-(y-low)/(max-low)*(bottom-top);
 for(let i=0;i<=5;i++){const x=i*xmax/5;el('line',{x1:sx(x),y1:top,x2:sx(x),y2:bottom,stroke:'#edf1f7'});el('text',{x:sx(x),y:bottom+18,'text-anchor':'middle',fill:'#8c9ab1','font-size':10},number(x,3));}
 for(let i=0;i<=4;i++){const y=low+(max-low)*i/4;el('line',{x1:left,y1:sy(y),x2:right,y2:sy(y),stroke:'#e6edf7'});el('text',{x:left-10,y:sy(y)+4,'text-anchor':'end',fill:'#8c9ab1','font-size':10},number(y,1));}
 el('text',{x:240,y:284,'text-anchor':'middle',fill:'#677f9f','font-size':11},'应变');el('text',{x:15,y:140,transform:'rotate(-90 15 140)','text-anchor':'middle',fill:'#677f9f','font-size':11},'应力 (GPa)');
 curves.forEach((c,i)=>el('polyline',{points:c.points.map(p=>`${sx(p[0])},${sy(p[1])}`).join(' '),fill:'none',stroke:i?'#a21caf':'#185adb','stroke-dasharray':i?'7 4':'none','stroke-width':2,'stroke-linejoin':'round'}));return svg;
}
function limitations(report){const d=node('details',undefined,'result-limits');d.append(node('summary','查看结果限制与分析依据'));const ul=node('ul');for(const text of report.limitations)ul.append(node('li',text));d.append(ul);return d;}
function reportDownloads(report,box){
 if(report.closeout){const accepted=node('details',undefined,'download-group');accepted.open=false;accepted.append(node('summary','论文与计算结果 · '+report.closeout.files.length+' 个文件'));for(const f of report.closeout.files){const row=node('div',undefined,'file-row'),a=node('a',readableResultLabel(f.label)+' ↓');a.href=`/api/tasks/${current.id}/closeout/files/${f.name}`;row.append(a,node('small',fileSize(f.size)));accepted.append(row);}box.append(accepted);}
 const group=node('details',undefined,'download-group');group.open=true;group.append(node('summary','作者参考 A · 分析与报告'));box.append(group);
 for(const f of report.files.filter(f=>f.name!=='analysis.json')){const row=node('div',undefined,'file-row'),a=node('a',readableResultLabel(f.label)+' ↓');a.href=`/api/tasks/${current.id}/reference-result/files/${f.name}`;row.append(a,node('small',fileSize(f.size)));group.append(row);}
}
function closeoutTable(report){
 const table=node('table',undefined,'result-table'),head=node('thead'),tr=node('tr'),body=node('tbody');
 for(const label of ['指标 / GPa','论文结果 P','作者源码运行 A','Agent 独立生成 B','论文与作者之差 |P−A|','作者与Agent之差 |A−B|','论文与Agent之差 |P−B|'])tr.append(node('th',label));head.append(tr);table.append(head,body);
 for(const m of report.metrics){const row=node('tr'),name=node('td',m.label);name.append(node('small',m.method));row.append(name);for(const k of ['P','A','B','absolute_PA','absolute_AB','absolute_PB'])row.append(node('td',number(m[k],5),'number'));body.append(row);}return table;
}
function evidenceSourceHeading(role){
 const sources={paper:['P','论文结果'],reference:['A','作者源码运行'],agent:['B','Agent 独立生成']};
 const value=sources[role];if(!value)return null;
 const heading=node('div',undefined,'figure-source');heading.append(node('span',value[0],'figure-source-letter'),node('strong',value[1]));return heading;
}
function closeoutFigure(report,figure,role){
 role=role||report.views?.flatMap(v=>v.figures).find(f=>f.name===figure.name)?.role;
 const panel=node('figure',undefined,'paper-figure evidence-figure '+(role||'')),link=node('a');
 const heading=evidenceSourceHeading(role);if(heading)panel.append(heading);
 panel.append(node('h4',readableResultLabel(figure.label)));link.href=`/api/tasks/${current.id}/closeout/files/${figure.name}`;link.target='_blank';link.rel='noopener';const img=node('img');img.src=link.href;img.alt=readableResultLabel(figure.label);img.loading='lazy';link.append(img);panel.append(link,node('figcaption',readableResultLabel(figure.caption)));return panel;
}
function closeoutCoverage(report,expanded=false){
 const panel=node('details',undefined,'coverage-panel');panel.open=expanded;panel.append(node('summary',`查看全篇 ${report.coverage.length} 项目标的工况、缺项与分析依据`));const table=node('table',undefined,'research-table'),body=node('tbody');for(const item of report.coverage){const row=node('tr'),target=node('td');target.append(node('strong',item.target),node('small',item.content));const evidence=node('td');evidence.append(node('strong',item.status),node('p',item.evidence),node('small',item.additional_work));row.append(target,evidence);body.append(row);}table.append(body);panel.append(table);return panel;
}

function readableResultLabel(value){
 return value.replace(/^A · /,'作者源码运行（A） · ').replace(/^B · /,'Agent 独立生成（B） · ');
}
function evidenceLegend(){
 const box=node('section',undefined,'evidence-legend');box.setAttribute('aria-label','三类结果的含义');
 for(const [role,letter,title,description] of [
  ['paper','P','论文结果','原论文中的图、表和报告值。'],
  ['reference','A','作者源码运行','使用作者公开的原始代码与工作流程，实际运行所得。'],
  ['agent','B','Agent 独立生成','Agent 根据允许的研究条件独立编写流程，实际运行所得。']]){
   const card=node('article',undefined,'evidence-source '+role);card.append(node('span',letter,'source-letter'),node('strong',title),node('p',description));box.append(card);
 }
 return box;
}
let evidenceViewChoice=null;
function evidenceDataTable(table,fileRoute='closeout'){
 const panel=node('section',undefined,'evidence-data '+table.role),roles={paper:'论文结果（P）',reference:'作者源码运行（A）',agent:'Agent 独立生成（B）'};
 panel.append(evidenceSourceHeading(table.role),node('h4',roles[table.role]+' · '+table.label));
 const scroll=node('div',undefined,'comparison-scroll'),grid=node('table',undefined,'result-table'),head=node('thead'),row=node('tr'),body=node('tbody');
 for(const column of table.columns)row.append(node('th',column.label));head.append(row);grid.append(head,body);
 for(const values of table.rows){const tr=node('tr');for(const value of values)tr.append(node('td',value,'number'));body.append(tr);}scroll.append(grid);panel.append(scroll);
 const download=node('a','下载完整数据表 ↓');download.href=`/api/tasks/${current.id}/${fileRoute}/files/${table.name}`;
 panel.append(node('p',table.truncated?`共 ${table.total_rows} 行，预览前 6 行与末 6 行；完整数据可下载。`:`共 ${table.total_rows} 行。`,'plot-caption'),download);return panel;
}
function renderEvidenceGallery(reference,box){
 const report=reference.closeout;
 const views=report.views?.length?report.views:[{id:'baseline',title:'应力–应变',description:'已完成基准工况的曲线与数值比较。',stress_curve:true,figures:[],tables:[],metric_labels:report.metrics.map(m=>m.label)}];
 const toolbar=node('div',undefined,'evidence-tabs');toolbar.setAttribute('role','tablist');toolbar.setAttribute('aria-label','图表主题');
 const area=node('section',undefined,'evidence-view');box.append(toolbar,area);
 if(!views.some(v=>v.id===evidenceViewChoice))evidenceViewChoice=views[0].id;
 const draw=view=>{
  evidenceViewChoice=view.id;area.replaceChildren(node('h3',view.title),node('p',view.description,'plot-caption'));
  for(const button of toolbar.children)button.setAttribute('aria-selected',String(button.dataset.view===view.id));
  const grid=node('div',undefined,'closeout-figures');
  for(const image of view.figures.filter(f=>f.role==='paper')){const figure=report.figures.find(f=>f.name===image.name);grid.append(closeoutFigure(report,figure,image.role));}
  if(view.stress_curve){const computed=node('article',undefined,'result-widget');computed.append(node('h4','作者源码运行与 Agent 独立生成'),curvePlot(report),node('p','深蓝实线：作者源码运行（A）；紫红虚线：Agent 独立生成（B）。初始体积归一化的平均应力。','plot-caption'));grid.append(computed);}
  for(const image of view.figures.filter(f=>f.role!=='paper')){const figure=report.figures.find(f=>f.name===image.name);grid.append(closeoutFigure(report,figure,image.role));}
  const computedImages=view.figures.filter(f=>f.role==='reference'||f.role==='agent');
  const identical=computedImages.some(a=>a.role==='reference'&&computedImages.some(b=>b.role==='agent'&&a.sha256&&a.sha256===b.sha256));
  if(identical)area.append(node('p','这组 A、B 图片文件完全一致，按两份来源分别保留。颜色仅标识来源，不表示结果有差异；图片相同本身不作为复现通过依据。','evidence-equality'));
  area.append(grid);
  if(view.metric_labels.length){const scroll=node('div',undefined,'comparison-scroll');scroll.append(closeoutTable({...report,metrics:report.metrics.filter(m=>view.metric_labels.includes(m.label))}));area.append(scroll);}
  if(view.tables.length){const tables=node('details',undefined,'evidence-tables');tables.open=true;tables.append(node('summary','查看数值表与下载'));for(const table of view.tables)tables.append(evidenceDataTable(table));area.append(tables);}
 };
 for(const view of views){const button=node('button',view.title);button.dataset.view=view.id;button.setAttribute('role','tab');button.onclick=()=>draw(view);toolbar.append(button);}
 draw(views.find(v=>v.id===evidenceViewChoice));
}
let closeoutPlotChoice='ab';
function renderCloseout(reference,box){
 const r=reference.closeout,a=r.acceptance||{};box.append(evidenceLegend());const banner=node('section',undefined,'acceptance-note');banner.append(node('span',acceptanceLabel(a),'badge'),node('strong',scientificLabel(r)),node('p',a.scope||'验收范围未记录'),node('small',`${a.date||'日期未记录'} · 来源：${a.source||'未记录'}；验收只覆盖上述范围，未覆盖的工况不作为已复现；拟合口径等诊断差异见结果限制。`));box.append(banner);
 if(resultTab==='plots'){
  const label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择图表');const options={ab:'A–B 应力–应变',elastic05:'A–B 弹性区间 0–0.05',elastic06:'A–B 弹性区间 0–0.06',author:'作者 A 原有诊断视图'};r.figures.forEach((f,i)=>options['paper'+i]=f.label);for(const [k,v] of Object.entries(options))select.append(new Option(v,k));select.value=Object.hasOwn(options,closeoutPlotChoice)?closeoutPlotChoice:'ab';label.append(select);box.append(label);const area=node('div',undefined,'chart-area');box.append(area);const draw=()=>{area.replaceChildren();if(select.value.startsWith('paper'))area.append(closeoutFigure(r,r.figures[Number(select.value.slice(5))]));else if(select.value==='author')renderPlotGallery(reference,area);else area.append(curvePlot({...r,xmax:select.value==='elastic05'?.05:select.value==='elastic06'?.06:.5}),node('p','深蓝实线：作者源码运行（A）；紫红虚线：Agent 独立生成（B）。论文（P）的完整曲线未数字化，见论文原图。','plot-caption'));};select.onchange=()=>{closeoutPlotChoice=select.value;draw();};draw();
 }else if(resultTab==='overview'){
  renderEvidenceGallery(reference,box);
 }
 if(!['plots','overview'].includes(resultTab)){const scroll=node('div',undefined,'comparison-scroll');scroll.append(closeoutTable(r));box.append(scroll,node('p','P 为论文表格值；两种模量窗口分别保留。数值比较不代替整条曲线或整篇论文验证。','plot-caption'));}
 if(resultTab==='report'){box.append(node('h3',reference.title));const doi=node('a','DOI '+reference.doi);doi.href='https://doi.org/'+reference.doi;doi.target='_blank';doi.rel='noopener noreferrer';box.append(doi,node('p',`A 本次 ${number(reference.runtime.core_hours,4)} 核时；B 本次 ${number(r.core_hours,4)} 核时。A 历次失败另见历史，未从总账删除。`));}
 box.append(limitations({limitations:[...reference.limitations,...r.limitations]}),closeoutCoverage(r));
}
function taskTimeline(){const list=node('ol',undefined,'task-timeline');for(const item of $('#history-list').children)list.append(item.cloneNode(true));return list;}
function renderWorkspaceResults(){
 const box=$('#research-results');box.replaceChildren();const r=workspaceReport;
 const phase=workspaceState.task===current?.id?workspaceState.phase:'loading';
 if(phase==='loading'&&!workspaceState.updated){box.append(emptyState('正在读取计算记录','正在核对结果与文件，请稍候。'));return;}
 if(phase==='error'&&!workspaceState.updated){box.append(emptyState('暂时无法核对结果','请刷新重试；这不表示计算失败或没有结果。'));return;}
 if(phase==='loading'||phase==='error')box.append(node('p',phase==='loading'?'正在更新；下方保留上次核验的结果。':'更新未完成；下方为上次核验的结果，请稍后刷新。','refresh-note'));

 if(resultTab==='history'&&!r){box.append(node('h3','任务历史'),taskTimeline());if(!normalResult?.evaluations?.length&&!currentRawFiles().length){box.append(node('p','尚无计算提交记录。','plot-caption'));return;}}
 if(!r && normalResult?.evaluations?.length){renderOrdinaryResults(box);return;}
 if(!r&&currentRawFiles().length){
  box.append(emptyState('原始输出已回收','可从右侧下载原始数据和日志。分析报告尚未生成或未接入；文件回收不代表科学验证通过。'));
  const jobs=new Map(currentRawFiles().map(f=>[f.request_id,f]));
  for(const f of jobs.values())box.append(node('p',`作业 ${f.job_id} · ${requestStates[f.state]||f.state}`));
  return;
 }
 if(!r){const empty=emptyState('还没有计算结果',current.status==='conditions_frozen'?'研究条件已保存。方案与计算记录会在服务就绪后显示。':'研究需求已保存。方案、计算与分析就绪后，真实结果会显示在这里。');const button=node('button','查看模型设置','quiet');button.onclick=()=>action(openModel);empty.append(button);box.append(empty);return;}
 if(resultTab==='targets'){box.append(node('h2','论文图表与计算结果'));if(r.closeout){box.append(evidenceLegend());renderEvidenceGallery(r,box);box.append(closeoutCoverage(r.closeout));}else box.append(node('p','全文图表审计尚未接入。'));return;}
 if(r.closeout && ['overview','data','plots','report'].includes(resultTab)){renderCloseout(r,box);return;}
 if(resultTab==='overview'){
  const grid=node('div',undefined,'result-grid'),values=node('article',undefined,'result-widget'),plot=node('article',undefined,'result-widget');values.append(node('h3','P–A 关键结果 · GPa'),metricTable(r.metrics.slice(0,3),true),node('p','模量两行分别使用应变 0–0.05、0–0.06，尚未冻结唯一论文评分口径。','plot-caption'));plot.append(node('h3','作者参考 A · 应力–应变曲线'),node('p','当前图为 A 的实际输出；P 的完整曲线与 B 的比较不在此图中。','plot-caption'),curvePlot(r));const legend=node('div',undefined,'plot-legend');for(const c of r.curves)legend.append(node('span',c.label));plot.append(legend);grid.append(plot,values);box.append(grid);const summary=node('div',undefined,'research-summary');summary.append(node('h3','结果说明'),node('p',r.summary));box.append(summary,limitations(r));
 }else if(resultTab==='data'){
  box.append(node('h3','论文 P 与作者参考 A 的数值比较'));const scroll=node('div',undefined,'comparison-scroll');scroll.append(metricTable(r.metrics));box.append(scroll,node('p','单位均为 GPa；相对差 = abs(P−A) / abs(P)。本表为参考 A 的诊断结果。','plot-caption'),limitations(r));
 }else if(resultTab==='plots'){
  renderPlotGallery(r,box);
 }else if(resultTab==='structure'||resultTab==='trajectory'){
  const structures=r.closeout?.figures.filter(f=>f.kind==='structure')||[];if(structures.length){box.append(node('p','由已回收轨迹生成的 OVITO 结构快照。颜色为局部结构分类，不是元素颜色；自适应 CNA 是追加诊断。','plot-caption'));for(const figure of structures)box.append(closeoutFigure(r.closeout,figure));if(resultTab==='trajectory')box.append(node('p','当前展示代表帧；连续动画与交互旋转尚未接入，可下载原始轨迹。','plot-caption'));}else box.append(emptyState('结构分析尚未发布','原始结构与轨迹可在文件下载中获取。'));
 }else if(resultTab==='report'){
  const article=node('article',undefined,'report-text');article.append(node('h3','作者参考 A · 诊断报告'),node('p',r.title,'paper-title'));const link=node('a','DOI '+r.doi);link.href='https://doi.org/'+r.doi;link.target='_blank';link.rel='noopener noreferrer';article.append(link,node('p',r.scope),node('p',r.summary),limitations(r));const linkReport=node('a','下载完整分析报告 ↓','quiet');linkReport.href=`/api/tasks/${current.id}/reference-result/files/report.md`;article.append(linkReport);box.append(article);
 }else{
  box.append(node('h3','真实提交记录'));const list=node('ol',undefined,'timeline');for(const [i,q] of r.evaluation.requests.entries()){const li=node('li');li.append(node('strong',`A 第 ${i+1} 次 · ${requestStates[q.state]||q.state}`),node('small',`作业 ${q.job_id||'未确认'} · ${q.accounted?number(q.actual_core_seconds/3600,4)+' 核时':'尚待核算'}`));const events=node('details');events.append(node('summary','展开本次记录'));const history=node('ol');for(const e of q.events)history.append(node('li',`${new Date(e.at*1000).toLocaleString('zh-CN')} · ${{reserved:'预留资源',dispatch_intent:'发起提交',scheduler_accepted:'调度器接受',scheduler_observed:'核对计算状态',accounting_final:'核算真实用量'}[e.kind]||'保存运行记录'}`));events.append(history);li.append(events);list.append(li);}box.append(list,node('p',`B 已用 ${bCount(r)} 次；${bState(r)}。作者参考 A 的失败不从历史移除。`,'plot-caption'));for(const ev of r.agent_progress?.evaluations||[])for(const [i,q] of ev.requests.entries()){const row=node('article',undefined,'request-record');row.append(node('strong',`B 第 ${i+1} 次 · ${requestStates[q.state]||q.state}`),node('p','作业 '+(q.job_id||'待确认')),node('small',q.accounted?number(q.actual_core_seconds/3600,4)+' 核时':'运行费用尚未最终核算'));if(q.monitoring){const m=q.monitoring;row.append(node('p','后台检查：'+new Date(m.last_checked*1000).toLocaleString('zh-CN')+(m.reason?' · 连接或记录待核对，保留上次状态':' · 本次调度记录已核验')));if(!q.accounted)row.append(node('small',Date.now()/1000>m.next_due+60?'更新已延迟，等待后台恢复':'下次计划检查：'+new Date(m.next_due*1000).toLocaleTimeString('zh-CN')));}box.append(row);}box.append(node('h3','任务与条件历史'),taskTimeline());
 }
}
function renderReferenceProgress(){
 const box=$('#reference-progress');box.replaceChildren();
 const entries=referenceProgress?.task_id===current?.id?referenceProgress.entries:[];
 box.hidden=!entries?.length;if(box.hidden)return;
 box.append(node('h2','作者源码参考计算（A）'),node('p','A 直接运行作者工作流程；B 由应用内 AI 根据研究条件独立生成。两者的作业、次数和结果分别记录。','subtle'));
 for(const item of entries){
  const paper=item.paper,evaluation=item.evaluation;
  const title=node('a',paper.title,'reference-paper-title');title.href=paper.doi_url;title.target='_blank';title.rel='noopener noreferrer';
  box.append(title,node('small','DOI '+paper.doi),node('p',paper.scope,'subtle'));
  if(!evaluation.available){box.append(node('p','参考账本暂不可读；已有提交保留，不能据此认定未提交。','error'));addInfo('A 状态','提交记录待核对');continue;}
  const count=`${evaluation.dispatch_claims} 次（含失败）`;
  addInfo('A 提交次数',count);
  box.append(node('p','实际提交：'+count+' · '+(evaluation.max_attempts===null?'参考开发许可：继续至跑通':`本参考评测上限 ${evaluation.max_attempts} 次`),'reference-count'));
  for(const [i,q] of evaluation.requests.entries()){
   const row=node('article',undefined,'request-record'),latest=i===evaluation.requests.length-1;
   row.append(node('strong',`A 第 ${i+1} 次 · ${requestStates[q.state]||'状态待核对'}`),node('p',q.job_id?'HPC 作业号：'+q.job_id:'尚无确认的作业号'));
   if(q.resources)row.append(node('small',`${q.resources.cores} CPU 核 · ${fileSize(q.resources.memory_bytes)} 内存 · 作业最长 ${number(q.resources.wall_seconds/3600,1)} 小时`));
   row.append(node('p',q.accounted?`实际用量：${number(q.actual_core_seconds/3600,4)} CPU 核时`:'实际费用尚未最终核算；资源预留与最终用量分别保留。'));
   const observed=q.events.filter(e=>['scheduler_observed','accounting_final','monitor_result'].includes(e.kind)).at(-1);
   if(observed){row.append(node('small','最近核对：'+new Date(observed.at*1000).toLocaleString('zh-CN')));if(!q.accounted&&Date.now()/1000-observed.at>1800)row.append(node('small','显示上次已核验状态；核对已超过 30 分钟，不能据此判断作业失败。'));}
   if(q.monitoring?.reason)row.append(node('p','最近检查未能确认新状态，保留上次调度记录。','form-note'));
   const detail=node('details');detail.append(node('summary','查看本次提交、状态核对与费用历史'));
   const history=node('ol',undefined,'timeline');for(const e of q.events)history.append(node('li',new Date(e.at*1000).toLocaleString('zh-CN')+' · '+({reserved:'预留资源',dispatch_intent:'发起提交',scheduler_accepted:'收到作业号',scheduler_observed:'核对调度状态',accounting_final:'核算实际费用',monitor_result:'保存后台核对',dispatch_unknown:'提交结果待核对'}[e.kind]||'保存运行记录')));detail.append(history);row.append(detail);box.append(row);
   if(latest){addInfo('A 作业号',q.job_id||'尚未确认');addInfo('A 状态',requestStates[q.state]||'待核对');}
  }
 }
 box.append(node('p','调度完成后还需核验输出和论文对比；此处不代表科学复现通过。','plot-caption'));
}
function taskTagSummary(value,unit=''){
 const text=(researchText(value)+(unit?' '+researchText(unit):'')).replace(/\s+/g,' ').trim();
 const characters=Array.from(text);
 return characters.length>48?characters.slice(0,47).join('')+'…':text;
}
async function refreshWorkspace(){
 if(!current)return;
 const id=current.id, generation=++workspaceGeneration;
 if(workspaceState.task!==id)workspaceState={task:id,phase:'loading',updated:null};
 workspaceState.phase='loading';renderWorkspaceResults();renderRefreshStatus();
 const replies=await Promise.allSettled([
  api(`/api/tasks/${id}/reference-result`),api(`/api/tasks/${id}/execution`),
  api(`/api/tasks/${id}/raw-files`),api(`/api/tasks/${id}/results`),
  api(`/api/tasks/${id}/reference-progress`)
 ]);
 if(current?.id!==id||generation!==workspaceGeneration)return;
 if(replies.some(r=>r.status==='rejected')){
  workspaceState.phase='error';renderRefreshStatus();renderWorkspaceResults();return;
 }
 const [reference,execution,raw,results,progress]=replies.map(r=>r.value);
 workspaceReport=reference.report;executionState=execution;rawResult={task:id,files:raw.files};normalResult=results;
 referenceProgress=progress;
 workspaceState={task:id,phase:'ready',updated:new Date()};renderRefreshStatus();renderTargetPlanning();
 const r=workspaceReport;
 $('.discussion-panel').hidden=!r&&!normalResult?.evaluations?.some(e=>e.requests.some(q=>q.reports?.length));
 $('#task-tags').replaceChildren();
 if(r){$('#task-status').textContent='A 已结束 · B '+bState(r);$('#task-meta').textContent='文献复现验证 · '+r.scope;for(const label of (r.closeout?['作者原始流程','P–A–B 比较已保存',acceptanceLabel(r.closeout.acceptance)]:['作者原始流程','P–A 诊断已保存','科学结论待核验']))$('#task-tags').append(node('span',label,'tag'));}
 else{
  let shortened=false;
  for(const key of ['material','temperature']){
   const f=current.fields[key],c=f?.candidates.find(c=>c.id===f.selected);
   if(!c)continue;
   const label=taskTagSummary(c.value,c.unit);if(!label)continue;
   $('#task-tags').append(node('span',label,'tag'));shortened=shortened||label.endsWith('…');
  }
  if(shortened)$('#task-tags').append(node('small','完整内容见“查看你的完整研究需求”和“研究条件”。','subtle'));
 }
 renderFlow(r);$('#task-information').replaceChildren();$('#task-files').replaceChildren();$('#task-resources').replaceChildren();
 addInfo('状态',r?'A 已结束，B '+bState(r):current.status==='conditions_frozen'?'条件已保存':'需求已保存');addInfo('记录更新',new Date(current.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}));
 if(r){addInfo('A 运行时长',duration(r.runtime.elapsed_seconds));addInfo('A 使用核数',String(r.runtime.cores));addInfo('A 提交次数',`${r.evaluation.dispatch_claims} 次（含失败）`);addInfo('B 提交次数',bCount(r));addInfo('A 本次核时',number(r.runtime.core_hours,3));addInfo('B 资源额度','用户未设上限');addInfo('A 作业号',r.job_id);reportDownloads(r,$('#task-files'));const a=node('a','论文与 DOI ↗','resource-link');a.href='https://doi.org/'+r.doi;a.target='_blank';a.rel='noopener noreferrer';a.append(node('small',r.title));$('#task-resources').append(a);}
 else{$('#task-files').append(node('p','计算及分析产生的文件会保存在这里。','subtle'));addInfo('提交次数','尚无已核验记录');}
 for(const [label,url] of [['LAMMPS 使用文档','https://docs.lammps.org/'],['OVITO 分析工具','https://www.ovito.org/']]){const a=node('a',label+' ↗','resource-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';$('#task-resources').append(a);}
 $('#task-model').textContent=r?(r.evaluation?.attempt_scope==='week_one_reference_development'?'作者参考 A · 第一周人工辅助验证记录':'作者参考 A · 来源见任务信息'):(schema?.model_calls_enabled?'应用模型已连接 · 生成与用量见活动记录':'模型状态见设置');
 const tabs=$('#result-tabs');tabs.replaceChildren();for(const [key,label] of Object.entries({overview:'结果总览',targets:'论文目标',data:'关键数据',plots:'可视化图表',structure:'原子结构',trajectory:'轨迹动画',report:'分析报告',history:'历史记录'})){const b=node('button',label);b.setAttribute('role','tab');b.setAttribute('aria-selected',String(key===resultTab));b.onclick=()=>{resultTab=key;for(const x of tabs.children)x.setAttribute('aria-selected',String(x===b));renderWorkspaceResults();};tabs.append(b);}renderWorkspaceResults();
 renderRawFiles(raw);
 if(current?.id!==id)return;
 if(!r&&currentRawFiles().length&&!normalResult?.evaluations?.length){
  $('#task-status').textContent='原始输出已回收';
  $('#task-information').replaceChildren();addInfo('结果状态','原始输出已回收');addInfo('分析报告','尚未生成或未接入');
  addInfo('提交次数','完整提交记录尚未接入');
  for(const f of new Map(currentRawFiles().map(f=>[f.request_id,f])).values())addInfo('作业 '+f.job_id,requestStates[f.state]||f.state);
  $('#execution-flow').replaceChildren(node('h2','计算记录'),node('p','原始输出已回收，可下载查看。完整执行阶段及分析报告尚未接入。','flow-note'));
 }
 renderExecutionControls();renderReferenceProgress();renderWorkspaceResults();
 await refreshPlanReview();await refreshActivity();
 if(!$('.discussion-panel').hidden)await refreshDiscussion();
}
function renderExecutionControls(){
 if(workspaceReport||!executionState)return;
 const box=$('#execution-flow'),job=executionState.job||(typeof ordinaryExecutionJob==='function'?ordinaryExecutionJob():null);
 if(job){
  box.replaceChildren(node('h2','执行进度'));
  const progress=node('ol',undefined,'execution-stages');
  const index=job.state==='analyzed'?4:['collecting','analyzing','analysis_failed'].includes(job.state)?3:job.job_id?2:1;
  ['需求已确认','方案确认','HPC 计算','回收与分析'].forEach((label,n)=>{
    const item=node('li',undefined,n<index?'done':n===index?'current':'pending');
    item.append(node('span',n<index?'✓':String(n+1)),node('strong',label));progress.append(item);
  });box.append(progress);
  const receipt=node('div',undefined,'execution-receipt');
  receipt.append(node('strong',job.job_id?'HPC 作业 '+job.job_id:'等待 HPC 接收回执'),
    node('span',job.job_id?(requestStates[job.scheduler_state]||job.label):job.label),
    node('span',`已提交 ${job.dispatch_count} / ${job.max_attempts} 次`));box.append(receipt);
  $('#task-status').textContent=job.label;
  $('#task-information').replaceChildren();addInfo('执行状态',job.label);addInfo('提交次数',`${job.dispatch_count} / ${job.max_attempts}`);
  addInfo((typeof referenceProgress!=='undefined'&&referenceProgress?.entries?.length)?'B HPC 作业号':'HPC 作业号',job.job_id||(job.dispatch_count?'提交结果待核对':'尚未提交'));
  if(job.job_id)addInfo('调度状态',requestStates[job.scheduler_state]||'待核对');
  addInfo('资源核算',job.accounted?'已核算':'待核算');addInfo('科学结论','尚未核验');
  if(!executionState.worker_alive&&['queued','running','waiting'].includes(job.state))box.append(node('p','后台当前未运行；恢复服务后继续已有请求，不会重复提交。','form-note'));
  if(job.state==='attention')box.append(node('p',job.reason==='deployment_file_missing'?'执行所需的部署文件尚未就绪，记录已保留。':job.reason==='deployment_changed'?'执行配置发生变化，需要核对后恢复。':'执行检查未通过，记录已保留；不会自动重提计算。','form-note'));
  if(job.can_retry){
   const retry=node('button',`使用第 ${job.dispatch_count+1} 次机会提交 HPC（最多 ${job.max_attempts} 次）`,'primary');
   retry.onclick=()=>action(async()=>{retry.disabled=true;await api(`/api/tasks/${current.id}/execution/retry`,{revision:current.revision});await refreshWorkspace();await refreshPlanReview();});
   box.append(node('p','沿用当前已批准方案；保留首次失败及费用，不重新生成方案。','form-note'),retry);
  }else if(job.state==='attention'){
   const resume=node('button',job.job_id?'恢复状态跟进':'重新核对并继续提交','primary');
   resume.onclick=()=>action(async()=>{resume.disabled=true;await api(`/api/tasks/${current.id}/execution/recheck`,{revision:current.revision});await refreshWorkspace();await refreshPlanReview();});
   box.append(resume);
  }
  const details=node('details');details.append(node('summary','查看提交、调度、回收与分析的完整记录'));const list=node('ol');for(const e of job.events)list.append(node('li',new Date(e.at).toLocaleString('zh-CN')+' · '+e.label));details.append(list);box.append(details);
 }else{
  $('#task-information').replaceChildren();
  addInfo((typeof referenceProgress!=='undefined'&&referenceProgress?.entries?.length)?'B HPC 作业号':'HPC 作业号',executionState.submissions?.count?'已有提交，回执待核对':'尚未提交');
  if(executionState.submissions)addInfo('提交次数',`${executionState.submissions.count} / ${executionState.submissions.maximum}`);
  const flow=executionState.automatic_workflow,step=flow?.workflow;
  if(step){box.replaceChildren(node('h2','任务执行流程'),node('p',step.label,'flow-note'));$('#task-status').textContent=step.label;
   addInfo('当前阶段',step.label);
   if(step.state==='attention')box.append(node('p','请查看准备记录中的问题；已有调用和提交历史保留，未自动重试。','form-note'));
   else if(!flow.worker_alive)box.append(node('p','后台当前未运行；服务恢复后继续已有流程。','form-note'));
   return;
  }
  if(executionState.can_start){
   box.replaceChildren(node('h2','计算方案已准备'),node('p','请确认下方方案并使用“批准并提交 HPC”。应用会按这一份方案提交、跟进和处理结果。','flow-note'));
   $('#task-status').textContent='方案已准备';$('#task-information').replaceChildren();addInfo('执行状态','等待开始');
   if(executionState.submissions)addInfo('提交次数',`${executionState.submissions.count} / ${executionState.submissions.maximum}`);
  }
  box.append(node('p',executionState.message||'计算服务状态待核对。','form-note'));
 }
}
let hpcState=null,hpcEditingId=null;
function editHPC(entry){
 hpcEditingId=entry?.id||null;const p=entry?.profile||{};
 for(const [id,key] of Object.entries({'hpc-label':'label','hpc-host':'host','hpc-user':'username','hpc-work':'work_directory','hpc-partition':'partition','hpc-account':'account'}))$('#'+id).value=p[key]||'';
 $('#hpc-port').value=p.port||22;$('#hpc-auth').value=p.authentication||'agent';clearHPCSecrets();
 $('#hpc-key-label').hidden=$('#hpc-auth').value!=='private_key';$('#check-hpc').disabled=!entry;
 $('#hpc-editor-title').textContent=entry?'编辑：'+p.label:'添加 HPC 连接';
 $('#hpc-status').textContent=entry?'已有认证可留空保留；更换地址或用户名时需要重新提供认证。':'填写新连接，保存不会提交计算。';
}
function removalButton(label,confirmed,run){
 const button=node('button',label,'quiet connection-remove');button.type='button';let armed=false;
 button.onclick=()=>{if(!armed){armed=true;button.textContent=confirmed;return;}action(run);};return button;
}
function renderHPCConnections(){
 const box=$('#saved-hpc-list');box.replaceChildren();
 const entries=hpcState.connections||[];
 if(!entries.length)box.append(node('p','尚未保存 HPC 连接。','form-note'));
 for(const entry of entries){
  const card=node('article',undefined,'connection-card'),p=entry.profile;
  card.append(node('strong',p.label),node('span',entry.archived?'已移除':entry.active?'首选连接':'已保存','connection-badge'));
  card.append(node('p',`${p.username}@${p.host}:${p.port} · ${p.partition||'未指定分区'}`,'connection-detail'));
  card.append(node('p',entry.archived?'不再用于新任务；旧任务认证与历史保留。':`版本 ${entry.revision} · ${entry.last_check?(entry.last_check.connected?'最近 SSH 检查成功':'最近 SSH 检查未成功'):'尚未检查 SSH'} · 保存不等于可提交计算`,'form-note'));
  const actions=node('div',undefined,'actions');
  const manage=async operation=>{hpcState=await api('/api/hpc-connection/manage',{connection_id:entry.id,operation,management_revision:hpcState.management_revision});renderHPCConnections();editHPC(hpcState.connections.find(x=>x.id===hpcState.active_id));};
  if(entry.archived){const restore=node('button','恢复连接','quiet');restore.type='button';restore.onclick=()=>action(()=>manage('restore'));actions.append(restore);}
  else{
   const edit=node('button','编辑','quiet');edit.type='button';edit.onclick=()=>editHPC(entry);actions.append(edit);
   if(!entry.active){const use=node('button','设为首选','quiet');use.type='button';use.onclick=()=>action(()=>manage('select'));actions.append(use);}
   actions.append(removalButton('移出列表','确认移除（保留旧任务）',()=>manage('archive')));
  }
  card.append(actions);box.append(card);
 }
}
async function openHPC(){hpcState=await api('/api/hpc-connection');renderHPCConnections();editHPC(hpcState.connections?.find(x=>x.active));$('#hpc-dialog .dialog-error').hidden=true;$('#hpc-dialog').showModal();}
$('#new-hpc').onclick=()=>{editHPC(null);$('#hpc-label').focus();};
function clearHPCSecrets(){for(const id of ['hpc-key','hpc-known','hpc-cert'])$('#'+id).value='';}
$('#open-hpc').onclick=()=>action(async()=>{$('#model-key').value='';$('#model-dialog').close();await openHPC();});
$('#switch-model').onclick=()=>action(async()=>{clearHPCSecrets();$('#hpc-dialog').close();await openModel();});
$('#close-hpc').onclick=()=>{clearHPCSecrets();$('#hpc-dialog').close();};
$('#hpc-dialog').addEventListener('close',clearHPCSecrets);
$('#hpc-auth').onchange=()=>{$('#hpc-key-label').hidden=$('#hpc-auth').value!=='private_key';};
$('#hpc-form').onsubmit=e=>{e.preventDefault();action(async()=>{const payload={revision:hpcState.revision,management_revision:hpcState.management_revision,connection_id:hpcEditingId,as_new:!hpcEditingId,label:$('#hpc-label').value.trim(),host:$('#hpc-host').value.trim(),port:Number($('#hpc-port').value),username:$('#hpc-user').value.trim(),work_directory:$('#hpc-work').value.trim(),partition:$('#hpc-partition').value.trim(),account:$('#hpc-account').value.trim(),authentication:$('#hpc-auth').value,private_key:$('#hpc-key').value||null,known_hosts:$('#hpc-known').value||null,certificate:$('#hpc-cert').value||null};try{hpcState=await api('/api/hpc-connection',payload);}finally{clearHPCSecrets();}renderHPCConnections();editHPC(hpcState.connections.find(x=>x.active));$('#hpc-status').textContent='已保存。旧任务继续使用原连接；运行服务绑定不会随设置变更。';});};
$('#check-hpc').onclick=()=>action(async()=>{const b=$('#check-hpc');b.disabled=true;$('#hpc-status').textContent='正在检查已保存的 SSH 连接…';try{const result=await api('/api/hpc-connection/check',{revision:hpcState.connections.find(x=>x.id===hpcEditingId).revision});hpcState=await api('/api/hpc-connection');renderHPCConnections();$('#hpc-status').textContent=result.message;}finally{b.disabled=false;}});
function renderRefreshStatus(){
 const status=$('#workspace-refresh-status');
 status.textContent=workspaceState.phase==='loading'?'正在核对结果与文件…':workspaceState.phase==='error'?'连接暂不可用；未更新结果与文件。':'结果与文件已核对';
 if(workspaceState.updated)status.textContent+=' · 上次核验 '+workspaceState.updated.toLocaleTimeString('zh-CN');
 status.className='workspace-freshness '+workspaceState.phase;
 $('#task-files').setAttribute('aria-busy',String(workspaceState.phase==='loading'));
}
function rawFileCategory(file){
 const name=file.name.toLowerCase();
 if(/lammpstrj|\.dump$|trajectory/.test(name))return '轨迹';
 if(/\.data$|^data[.-]|\.xyz$|\.vasp$/.test(name))return '结构';
 if(/log|stdout|stderr|\.out$|\.err$/.test(name))return '运行日志';
 return '原始数据';
}
function renderRawFiles(result){
 const box=$('#task-files');
 if(!result.files.length){if(!workspaceReport)box.append(node('p',result.message,'form-note'));return;}
 const groups=new Map();
 for(const f of result.files){const key=f.request_id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(f);}
 for(const files of groups.values()){
  const f=files[0],reference=workspaceReport?.evaluation?.requests?.some(q=>q.id===f.request_id),independent=workspaceReport?.agent_progress?.evaluations?.some(e=>e.requests.some(q=>q.id===f.request_id));
  const group=node('details',undefined,'download-group');
  group.append(node('summary',`${reference?'作者参考 A':independent?'独立计算 B':'计算输出'} · 作业 ${f.job_id} · ${files.length} 个文件`));
  for(const kind of ['原始数据','结构','轨迹','运行日志']){
   const selected=files.filter(f=>rawFileCategory(f)===kind);if(!selected.length)continue;
   const section=node('div',undefined,'download-category');section.append(node('h4',kind));
   for(const file of selected){const row=node('div',undefined,'file-row'),a=node('a',file.name);a.href=`/api/tasks/${current.id}/raw-files/${file.id}`;row.append(a,node('small',fileSize(file.size)));section.append(row);}group.append(section);
  }
  box.append(group);
 }
}
let connectionState=null;
async function openModel(){modelPreference=await api('/api/model-preference');connectionState=await api('/api/model-connections');$('#provider-choice').value=modelPreference.provider;$('#model-name').value=connectionState.connections[modelPreference.provider]?.model||modelPreference.model;$('#model-key').value='';$('#model-dialog .dialog-error').hidden=true;refreshConnectionLabel();renderModelConnections();$('#model-dialog').showModal();}
function selectModelEditor(provider){
 $('#provider-choice').value=provider;$('#model-key').value='';$('#available-models').replaceChildren();
 $('#model-name').value=connectionState?.connections[provider]?.model||'';
 $('#model-editor-title').textContent=(connectionState?.connections[provider]?.configured?'编辑：':'添加：')+(connectionState?.connections[provider]?.label||provider);
 refreshConnectionLabel();
}
async function removeModelConnection(provider){
 connectionState=await api('/api/model-connections',{provider,model:'',remove:true});
 if(modelPreference.provider===provider)modelPreference=await api('/api/model-preference',{provider,model:'',revision:modelPreference.revision});
 selectModelEditor(provider);renderModelConnections();if(current)await refreshDiscussion();
}
function renderModelConnections(){
 const box=$('#saved-model-list');box.replaceChildren();
 const entries=Object.entries(connectionState?.connections||{}).filter(([,c])=>c.configured);
 if(!entries.length)box.append(node('p','尚未保存 API。选择模型商并填写密钥后即可添加。','form-note'));
 for(const [provider,c] of entries){
  const card=node('article',undefined,'connection-card'),preferred=Boolean(c.model)&&modelPreference.provider===provider&&modelPreference.model===c.model;
  card.append(node('strong',c.label),node('span',preferred?'首选模型':'已保存','connection-badge'),node('p',c.model||'尚未选择模型 ID','connection-detail'),node('p',c.endpoint+' · 密钥已保存，不回显','form-note'));
  const actions=node('div',undefined,'actions'),edit=node('button','编辑模型 / 更换密钥','quiet');edit.type='button';edit.onclick=()=>{selectModelEditor(provider);$('#model-name').focus();};actions.append(edit);
  if(!preferred&&c.model){const use=node('button','设为首选','quiet');use.type='button';use.onclick=()=>action(async()=>{modelPreference=await api('/api/model-preference',{provider,model:c.model,revision:modelPreference.revision});selectModelEditor(provider);renderModelConnections();});actions.append(use);}
  actions.append(removalButton('移除连接','确认移除 API 密钥',()=>removeModelConnection(provider)));card.append(actions);box.append(card);
 }
}
$('#new-model').onclick=()=>{const provider=Object.entries(connectionState.connections).find(([,c])=>!c.configured)?.[0];if(provider)selectModelEditor(provider);$('#model-editor-title').textContent=provider?'添加模型 API':'各模型商已有连接；可编辑或更换密钥';$('#provider-choice').focus();};
function refreshConnectionLabel(){const c=connectionState?.connections[$('#provider-choice').value];$('#connection-status').textContent=c?.configured?'已保存密钥，留空可保留；连接可用性以实际响应为准。':'尚未保存该模型商的密钥。';$('#fetch-models').disabled=!c?.configured||!c?.model_listing;$('#remove-key').disabled=!c?.configured;if(c&&!c.model_listing)$('#connection-status').textContent+=' 此模型商请手动填写模型 ID。';}
$('#provider-choice').onchange=()=>selectModelEditor($('#provider-choice').value);
$('#model-form').onsubmit=e=>{e.preventDefault();action(async()=>{const provider=$('#provider-choice').value,model=$('#model-name').value.trim(),key=$('#model-key').value;try{connectionState=await api('/api/model-connections',{provider,model,api_key:key||null});}finally{$('#model-key').value='';}modelPreference=await api('/api/model-preference',{provider,model,revision:modelPreference.revision});$('#open-model').title='模型与计算连接 · '+modelPreference.providers[provider];refreshConnectionLabel();renderModelConnections();if(current)await refreshDiscussion();notice('模型连接已保存。现在可在有结果的任务中提问；保存本身不调用模型。');});};
$('#fetch-models').onclick=()=>action(async()=>{const result=await api('/api/model-connections/models',{provider:$('#provider-choice').value});$('#available-models').replaceChildren(...result.models.map(id=>new Option(id,id)));$('#connection-status').textContent=`已取得 ${result.models.length} 个模型 ID${result.has_more?'（目录还有后续页，可手动输入）':''}。点击模型输入框选择。`});
$('#remove-key').onclick=()=>{const provider=$('#provider-choice').value;const card=[...$('#saved-model-list').children].find(x=>x.querySelector('strong')?.textContent===connectionState.connections[provider].label);card?.querySelector('.connection-remove')?.focus();$('#connection-status').textContent='请在上方已保存连接中点击“移除连接”并确认。';};
$('#close-model').onclick=()=>{$('#model-key').value='';$('#model-dialog').close();};for(const id of ['open-model','home-model','rail-model','discussion-model'])$('#'+id).onclick=()=>action(openModel);
for(const id of ['top-tasks','back-tasks','home-tasks'])$('#'+id).onclick=()=>requestRoute('#tasks');
for(const id of ['top-new','tasks-new'])$('#'+id).onclick=showNew;
$('#top-papers').onclick=()=>requestRoute('#papers');$('#pick-paper').onclick=()=>requestRoute('#papers');
for(const id of ['top-resources'])$('#'+id).onclick=()=>requestRoute('#resources');
for(const id of ['top-help','home-help'])$('#'+id).onclick=()=>requestRoute('#help');
$('#mode-research').onclick=()=>setMode('research');$('#mode-reproduction').onclick=()=>setMode('reproduction');
$('#task-search').oninput=()=>{action(async()=>{await listTasks();if(!$('#tasks-view').hidden)taskCards();});};

// 展开时自动加载活动明细（收起时不请求）。
const activityPanel=document.getElementById('ai-activity-panel');
if(activityPanel) activityPanel.ontoggle=()=>{if(activityPanel.open)refreshActivity();};
checkForUpdate();setInterval(checkForUpdate,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkForUpdate();});
bind('open-ai-records',()=>{$('#ai-activity-panel').open=true;$('#ai-activity-panel').scrollIntoView({block:'start',behavior:'smooth'});refreshActivity();});
bind('open-current-plan',()=>{$('#plan-review-panel').scrollIntoView({block:'start',behavior:'smooth'});});
const taskQuickLinks=$('#task-quick-links');
if(taskQuickLinks)taskQuickLinks.onclick=event=>{
  const button=event.target.closest('button[data-task-target]');
  if(!button||!taskQuickLinks.contains(button))return;
  const target=$(button.dataset.taskTarget),note=$('#task-quick-note');
  if(!target||target.closest('[hidden]')){
    note.textContent=button.dataset.taskTarget==='#plan-review-panel'?'方案尚未准备好；先按上方的下一步继续。':
      button.dataset.taskTarget==='.discussion-panel'?'已有可用结果后，才能围绕数据向应用内 AI 提问。':'这一步尚无记录；请查看上方当前进展。';
    return;
  }
  if(button.dataset.taskExpand)$(button.dataset.taskExpand).open=true;
  note.textContent='';target.scrollIntoView({block:'start',behavior:'smooth'});
};
bind('ai-activity-refresh',event=>{if(event&&event.preventDefault)event.preventDefault();if(event&&event.stopPropagation)event.stopPropagation();refreshActivity();});bind('guidance-send',()=>action(sendGuidance));
bind('plan-refresh',()=>action(refreshPlanReview));bind('task-pause',()=>action(togglePause));
$("#refresh-workspace").onclick=()=>action(async()=>{if(current)await openTask(current.id);});

// Shared navigation follows the approved home; all counts come from saved evidence.
let taskFilter='all', selectedPlot='full', discussionRequest=null;
function taskFinished(t){return Boolean(t.user_finished)||Boolean((t.lifecycle_events||[]).some(e=>e&&e.action==='finish'));}

// 统一绑定：元素不存在时安静跳过，避免"少一个元素就整页停止渲染"。
// 版本自检：我改完并部署后，页面会自动刷新，用户不需要手动清缓存或刷新。
let assetsSignature=null;
async function checkForUpdate(){
  try{
    const data=await api('/api/schema');
    const now=JSON.stringify(data.assets||{});
    if(assetsSignature===null){assetsSignature=now;return;}
    if(now!==assetsSignature){assetsSignature=now;notice('检测到界面已更新，正在自动刷新…');setTimeout(()=>location.reload(),600);}
  }catch(error){/* 网络抖动时忽略，不影响使用 */}
}

function bind(id,handler){const el=document.getElementById(id);if(el)el.onclick=handler;return Boolean(el);}

function questionText(question){return typeof question==='string'?question:String(question?.question||'');}

function buildClarificationAnswers(questions,values){
  const lines=[];
  (questions||[]).forEach((question,index)=>{
    const answer=String((values||[])[index]??'').trim();
    if(!answer) return;
    lines.push(`Q${index+1}: ${questionText(question)}`);
    lines.push(`A${index+1}: ${answer}`);
  });
  return lines.join('\n');
}

function taskState(t){
  if(t.scoped_acceptance?.status==='accepted_by_user') return 'validated';
  if(taskFinished(t)) return 'finished';
  if(t.execution_state) return t.execution_state;
  // 方案准备状态必须参与：否则冻结后无论准备中/失败/需要补充条件都显示“待准备”。
  const prep=t.preparation_state;
  if(prep==='prepared') return 'prepared';
  if(prep==='clarification') return 'clarification';
  if(prep==='failed') return 'preparation_failed';
  if(['queued','running','model_requested','reusing_plan','checking_plan','repairing_plan','preparing_files'].includes(prep)) return 'preparing';
  if(t.status!=='conditions_frozen'){
    if(t.condition_preparation_state==='failed')return 'condition_failed';
    if(['needs_reconciliation','uncertain'].includes(t.condition_preparation_state))return 'condition_attention';
    if(['prepared','generating','validating','repairing','awaiting_import'].includes(t.condition_preparation_state))return 'understanding';
  }
  // A 的参考作业只在 A 栏显示，不能覆盖独立 B 的下一步和筛选。
  return t.status==='conditions_frozen'?'frozen':'draft';
}
function taskStateLabel(t){
 const state=taskState(t);
 if(state==='draft'&&t.condition_preparation_state==='imported')return Number(t.outstanding)>0?'条件已整理 · 待补齐':'条件待确认';
 return ({validated:'验收通过 · 基准工况',finished:'已确认结束',running:'运行中',queued:'排队中',accepted:'已提交',completed:'计算结束 · 待核验',reconcile_required:'计算记录待核对',unknown:'提交状态待核对',dispatching:'正在提交',failed:'失败',timeout:'超时',draft:'待准备',frozen:'条件已冻结 · 待准备方案',prepared:'方案已准备 · 待执行',preparing:'方案准备中',clarification:'需要补充条件',preparation_failed:'方案准备失败',condition_failed:'需求条件整理失败',condition_attention:'条件整理记录待核对',understanding:'正在整理需求条件'})[state]||'状态待核对';
}
function statsFor(tasks, box){box.replaceChildren();for(const [label,value] of [['全部任务',tasks.length],['运行中',tasks.filter(t=>taskState(t)==='running').length],['排队中',tasks.filter(t=>['queued','accepted'].includes(taskState(t))).length],['计算结束',tasks.filter(t=>taskState(t)==='completed').length],['验收通过',tasks.filter(t=>taskState(t)==='validated').length],['已确认结束',tasks.filter(t=>taskFinished(t)).length]]){const c=node('div',undefined,'stat'),icon=node('span',undefined,'stat-icon');icon.append(uiIcon(label==='运行中'?'play':label==='排队中'?'clock':label==='计算结束'?'check':'tasks'));c.append(icon,node('small',label),node('strong',String(value)));box.append(c);}}
async function showHome(){current=null;hideViews('home-view');selectNavigation('');recordRoute('#home');await listTasks();statsFor(taskCache,$('#home-stats'));const papers=await api('/api/papers');const selected=papers.papers.filter(p=>p.selection==='selected');const box=$('#home-cases-content');box.replaceChildren();for(const p of selected.slice(0,2)){const c=node('article',undefined,'compact-case');c.append(node('span','文献复现验证','badge pending'),node('h3',p.title),node('small','DOI '+p.doi),node('p',p.stage));const go=node('button','查看进度','quiet');go.onclick=()=>requestRoute(p.tasks.length?'#'+p.tasks[0].id:'#papers');c.append(go);box.append(c);}if(!selected.length)box.append(node('p','尚未选定验证案例。','subtle'));const status=$('#home-status');status.replaceChildren();for(const [k,v] of [['计算状态',taskCache.some(t=>taskState(t)==='running')?'有任务正在运行':'以任务记录为准'],['案例清单',selected.length+' 篇已选'],['模型连接','点击模型设置查看'],['存储空间','未连接实时用量查询']])status.append(node('dt',k),node('dd',v));const dl=$('#home-downloads');dl.replaceChildren();const finished=taskCache.filter(t=>t.reference_stage||taskState(t)==='completed');for(const t of finished.slice(0,3)){const b=node('button',t.title,'download-task');b.onclick=()=>requestRoute('#'+t.id);b.append(node('small','打开结果与下载文件'));dl.append(b);}if(!finished.length)dl.append(node('p','结果文件会随任务保存在这里。','subtle'));}
$('#go-home').onclick=e=>{e.preventDefault();requestRoute('#home');};
$('#home-start').onclick=()=>{const prompt=$('#home-prompt').value;showNew();$('#create-form [name=prompt]').value=prompt;};
$('#home-guide').onclick=()=>{$('#home-start').click();};$('#home-cases').onclick=()=>requestRoute('#papers');
$('#paper-search').oninput=()=>action(refreshPapers);
for(const [title,text] of [['合金拉伸响应','研究 300 K 下 NbTiZrMoV 合金的单轴拉伸响应。请整理需要确认的初始结构、势函数和加载条件，输出应力–应变曲线与缺陷分析。'],['晶体弹性性质','计算 SiC 晶体的弹性性质。请先确认晶型、温度和势函数，给出弹性常数与分析图表。'],['点缺陷形成能','研究 W 晶体中的空位形成能。请明确参考体系、边界条件及弛豫方案，保留结构与能量来源。']]){const b=node('button',undefined,'example-task');b.append(node('strong',title),node('small',text));b.onclick=()=>{$('#create-form [name=prompt]').value=text;$('#create-form [name=prompt]').focus();};$('#task-examples').append(b);}
const helpItems=[['快速开始','点击“新建任务”，描述研究问题。补充信息可留空；任务会保存，尚未确定的科学条件需要进一步明确。当前通用自动执行尚未开放。'],['任务设置与输入','至少描述材料体系、温度或工况以及希望得到的性质。界面不会把未填写的条件当作已经确认。'],['势函数与材料资源','资源库保留势函数来源、版本和许可。元素相同不代表势函数适用，正式计算仍需任务相关的科学核验。'],['计算与 HPC','在顶部“设置 → 计算连接”中填写自己的登录地址、端口、用户名与认证资料。保存不提交作业，SSH 检查不代表计算环境已验证。目标物理计算只在授权超算执行。查看任务详情中的提交次数、作业号和历史。刷新页面不会重新提交计算。'],['结果分析与可视化','结果页可切换数据、图表、结构、轨迹和报告。多图使用已有真实数据；未接入的视图会明确标出。结果后的问题发给你配置的模型，不触发新模拟。'],['模型与 API 密钥','模型设置中选择厂商和具体模型 ID，输入 API 密钥。保存不调用模型。可读取 DeepSeek、Claude、GPT 的模型目录；GLM 当前按官方文档手动填写 ID。'],['失败与恢复','状态不明时先核对原作业，不能再次点击产生新计算。B 最多两次提交，失败和费用完整保留。'],['文献验证与历史','P 是论文结果；A 运行作者原始代码；B 独立生成。作者参考完成与科学复现成功分别记录。完整题目、DOI 和失败记录保存在案例中心。']];
function renderHelp(){const query=$('#help-search').value.trim().toLowerCase(),box=$('#help-articles');box.replaceChildren();for(const [title,body] of helpItems.filter(r=>r.join(' ').toLowerCase().includes(query))){const d=node('details',undefined,'panel help-article');d.open=title==='快速开始';d.append(node('summary',title),node('p',body));box.append(d);}if(!box.children.length)box.append(emptyState('未找到相关说明','试试“模型”“计算”或“结果”等关键词。'));}
$('#help-search').oninput=renderHelp;
function renderPlotGallery(report,box){
 const options={full:'完整拉伸曲线',virial:'固定体积位力应力',pressure:'瞬时压力口径',elastic05:'弹性区间 0–0.05',elastic06:'弹性区间 0–0.06'};
 const controls=node('div',undefined,'chart-toolbar'),label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择图表');for(const [key,value] of Object.entries(options))select.append(new Option(value,key));select.value=selectedPlot;label.append(select);controls.append(label);box.append(controls);const area=node('div',undefined,'chart-area');box.append(area);
 const draw=()=>{selectedPlot=select.value;area.replaceChildren();let curves=report.curves,xmax=.5;if(selectedPlot==='virial')curves=curves.slice(0,1);if(selectedPlot==='pressure')curves=curves.slice(1);if(selectedPlot.startsWith('elastic')){xmax=selectedPlot==='elastic05'?.05:.06;curves=curves.slice(0,1);}area.append(node('h3',options[selectedPlot]),curvePlot({...report,curves,xmax}));const legend=node('div',undefined,'plot-legend');for(const c of curves)legend.append(node('span',c.label));area.append(legend,node('p','同一真实基准工况的不同视图，不代表复现了多个工况或整篇论文。','plot-caption'));};select.onchange=draw;draw();
}
async function refreshDiscussion(){if(!current)return;const id=current.id;const result=await api(`/api/tasks/${id}/discussion`);if(current?.id!==id)return;const box=$('#discussion-history');box.replaceChildren();for(const m of result.messages){const row=node('article',undefined,'discussion-message');row.append(researchContent(m.question,'discussion-question'),researchContent(m.answer||'请求状态待核对，未重复发送。'),node('small',m.provider+' / '+m.model+' · '+new Date(m.at).toLocaleString('zh-CN')));box.append(row);}$('#discussion-status').textContent=result.enabled?'可围绕已有数据提问；发送后会保留问题、答复与模型用量。助手只解读已有结果，不能提交新的计算或执行任意分析代码。':'请先在“设置 → 模型 API”中保存当前所选模型的密钥和模型 ID；保存后即可在这里提问。';const discussionSubmit=$('#discussion-form button[type=submit]');if(discussionSubmit)discussionSubmit.disabled=!result.enabled;}
$('#discussion-open-plots').onclick=()=>{
 if(!current)return;
 resultTab='plots';
 for(const tab of $('#result-tabs').children)tab.setAttribute('aria-selected',String(tab.textContent==='可视化图表'));
 renderWorkspaceResults();$('#research-results').scrollIntoView({block:'start'});
};
$('#discussion-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(!current)return;const question=$('#discussion-prompt').value.trim();const pref=await api('/api/model-preference');if(!discussionRequest||discussionRequest.question!==question||discussionRequest.task!==current.id)discussionRequest={id:crypto.randomUUID().replaceAll('-',''),question,task:current.id};$('#discussion-status').textContent='正在分析已有结果…';try{const reply=await api(`/api/tasks/${current.id}/discussion`,{request_id:discussionRequest.id,provider:pref.provider,question});if(reply.state==='completed'){$('#discussion-prompt').value='';discussionRequest=null;}await refreshDiscussion();}catch(error){$('#discussion-status').textContent=error.message;throw error;}});};
setInterval(()=>{if(current&&!busy&&!document.querySelector('dialog[open]')&&!$('#discussion-prompt').value)action(async()=>{await refreshResults();await refreshWorkspace();});},30000);

function uiIcon(name){
 const paths={new:'M12 5v14M5 12h14',tasks:'M9 5H5v16h14V5h-4M9 3h6v4H9zM8 12h8M8 16h5',cases:'M3 5h7l2 2 2-2h7v15h-7l-2 2-2-2H3zM12 7v15',resources:'M4 6c0-4 16-4 16 0s-16 4-16 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0',help:'M9 8a3 3 0 0 1 6 0c0 3-3 2-3 5M12 17h.01',play:'M8 4l12 8-12 8z',clock:'M12 7v5l3 2',check:'M6 12l4 4 8-8'};
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('class','ui-icon');svg.setAttribute('aria-hidden','true');
 if(['clock','check','help'].includes(name)){const circle=document.createElementNS(ns,'circle');circle.setAttribute('cx','12');circle.setAttribute('cy','12');circle.setAttribute('r','10');svg.append(circle);}
 const path=document.createElementNS(ns,'path');path.setAttribute('d',paths[name]||paths.tasks);svg.append(path);return svg;
}
for(const key of ['new','tasks','papers','resources','help'])$('#top-'+key).prepend(uiIcon(key==='papers'?'cases':key));
$('#task-search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();action(showTasks);}});

for(const button of document.querySelectorAll('[data-resource-tab]'))button.prepend(uiIcon(button.dataset.resourceTab==='paper'?'cases':button.dataset.resourceTab==='tools'?'help':'resources'));

let targetPreviewGeneration=0;
function renderPaperEvidence(box,report){
 if(!report||report.task_id!==current?.id)return;
 const identity=typeof report.manifest_sha256==='string'&&/^[a-f0-9]{64}$/.test(report.manifest_sha256)?report.task_id+':'+report.manifest_sha256:null;
 const choice=renderPaperEvidence.selection;
 const generation=renderPaperEvidence.generation=(renderPaperEvidence.generation||0)+1;
 const panel=node('section',undefined,'paper-workbench');
 panel.append(node('h2','文献工作台 · 论文结果 P'),node('h3',report.title));
 const doi=node('a','DOI '+report.doi);doi.href='https://doi.org/'+report.doi;doi.target='_blank';doi.rel='noopener noreferrer';panel.append(doi,node('p',report.source_note,'subtle'));
 panel.append(node('p','P 是论文原图与提取数值；A 是作者源码运行；B 是应用内 AI 独立计算。提取完成不等于计算或科学复现通过。','form-note'));
 const tabs=node('div',undefined,'evidence-view-tabs'),area=node('section',undefined,'paper-evidence-content');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','切换论文图表');panel.append(tabs,area);
 const draw=view=>{
  if(current?.id!==report.task_id||generation!==renderPaperEvidence.generation)return;
  renderPaperEvidence.selection=identity?{identity,view_id:view.id}:null;
  area.replaceChildren();for(const b of tabs.children)b.setAttribute('aria-selected',String(b.dataset.view===view.id));
  area.append(node('h3',view.title),node('p',view.description));
  const images=node('div',undefined,'evidence-grid');
  for(const item of view.figures){const figure=report.figures.find(f=>f.name===item.name);if(!figure)continue;
   const card=node('figure',undefined,'paper-figure evidence-figure paper');card.append(evidenceSourceHeading('paper'),node('h4',figure.label));
   const link=node('a');link.href=`/api/tasks/${report.task_id}/paper-evidence/files/${figure.name}`;link.target='_blank';link.rel='noopener';
   const img=node('img');img.src=link.href;img.alt=figure.label;img.loading='lazy';link.append(img);card.append(link,node('figcaption',figure.caption));images.append(card);
  }area.append(images);
  const tables=node('details',undefined,'evidence-tables');tables.append(node('summary','查看提取数值表、来源字段与下载'));
  for(const table of view.tables)tables.append(evidenceDataTable(table,'paper-evidence'));if(view.tables.length)area.append(tables);
 };
 for(const view of report.views){const b=node('button',view.title);b.dataset.view=view.id;b.setAttribute('role','tab');b.onclick=()=>draw(view);tabs.append(b);}
 if(report.views.length)draw((identity&&choice?.identity===identity&&report.views.find(view=>view.id===choice.view_id))||report.views[0]);
 else renderPaperEvidence.selection=null;
 const source=report.target_inventory;
 if(source){const list=node('details',undefined,'coverage-panel');list.append(node('summary',`查看 ${source.targets.length} 项图表的复现优先级、工况与缺项`));
  list.append(node('p',source.coverage_note));
  const names={retained_data:'已有输出可分析',new_calculation:'需要计算',missing_resources:'资源缺项',not_simulation:'非 LAMMPS 目标',unresolved:'方法待核对'};
  for(const row of [...source.targets].sort((a,b)=>(report.priorities[a.id]||3)-(report.priorities[b.id]||3))){const item=node('article',undefined,'target-group');item.append(node('strong',`优先级 ${report.priorities[row.id]||3} · ${row.label} · ${names[row.availability]}`),node('p',row.conditions),node('small',row.locator+' · '+row.outputs),node('p',row.limitations));list.append(item);}panel.append(list);
 }
 panel.append(limitations(report));box.append(panel);
}
function targetReadinessRequest(isCurrent, onState, onError) {
  let sequence=0;
  return async (id,payload)=>{
    const request=++sequence;
    try {
      const state=await api(`/api/tasks/${id}/targets/preview`,payload);
      if(request===sequence&&isCurrent())onState(state);
    }catch(error){if(request===sequence&&isCurrent())onError(error);}
  };
}
function renderTargetPlanning() {
  const generation=++targetPreviewGeneration;
  const box=$('#target-planning');box.replaceChildren();box.hidden=current.mode!=='reproduction'&&!current.paper_evidence&&!current.paper_evidence_error;
  if(box.hidden)return;
  if(current.paper_evidence_error)box.append(node('p',current.paper_evidence_error,'error'));
  const paper=current.paper_evidence;renderPaperEvidence(box,paper);
  if(current.mode!=='reproduction'){
    box.append(node('p','这是普通研究 B 的运行页面；上方论文证据仅供人类对照，不进入 AI 方案生成。作者 A 与本页 B 分别记录，固定条件和原提交历史保留。','form-note'));return;
  }
  box.append(node('h2','先选复现目标'),node('p','逐项查看论文图表、工况与所需数据；同一工况的多个图表共用计算。'));
  const historical=current.status!=='conditions_frozen'&&(paper?.target_import_allowed===false||
    (typeof referenceProgress!=='undefined'&&referenceProgress?.task_id===current.id&&referenceProgress.entries.some(e=>
      !e.evaluation.available||e.evaluation.reserved_attempts>0||e.evaluation.dispatch_claims>0)));
  const source=current.target_inventory, frozen=current.status==='conditions_frozen'||historical;
  if(historical){const note='此任务已有作者计算记录或记录待核对。图表清单仅供事后查看，不能补写为事前目标；原作业与验收历史保留。';box.append(node('p',note,'form-note'));$('#freeze').disabled=true;$('#freeze-note').textContent=note;}
  if(!source){
    const audit=workspaceReport?.closeout;
    if(audit){
      box.replaceChildren(node('h2','论文目标与全篇审计'),node('p','先查看论文原图、计算曲线和数值表，再按需展开全篇 '+audit.coverage.length+' 项目标的详细依据。'));
      const show=node('button','查看论文与计算图表','primary');
      show.onclick=()=>{resultTab='targets';for(const tab of $('#result-tabs').children)tab.setAttribute('aria-selected',String(tab.textContent==='论文目标'));renderWorkspaceResults();$('#research-results').scrollIntoView({block:'start'});};
      box.append(show,node('p','这是对已完成基准的补充审计；原 PAB 验收、提交次数和历史不变。','form-note'));
    }else if(paper){
      box.append(node('p',frozen?'P 已提取并显示；此历史任务没有事前图表计划，不能追写为事前冻结。':'P 已提取并显示。请查看工况、资源与缺项，再保存目标范围；未确定的比较标准仍需确认。','form-note'));
      if(!frozen&&paper.target_import_allowed===true&&paper.target_inventory){const load=node('button','读取工作台图表清单并选择目标','primary');load.onclick=()=>action(async()=>{current=await api(`/api/tasks/${current.id}/paper-evidence/import-targets`,{revision:current.revision});await afterChange('已读取登记的工作台清单；选择范围不会提交计算。');});box.append(load);}
    }else box.append(node('p',frozen?'此历史任务没有事前图表计划；原有验收与运行记录保留。':'尚未接入文献工作台图表产物；这不表示正文不存在或不可复现。请先完成资源匹配与 P 提取接线。'));
    return;
  }
  box.append(node('h3',source.paper.title),node('p','DOI: '+source.paper.doi),node('p',source.coverage_note,'subtle'));
  const names={retained_data:'已有数据可分析',new_calculation:'需要新增计算',missing_resources:'缺少资源',not_simulation:'资料核对项目',unresolved:'方法或工况待核对'};
  const selected=new Set(current.target_selection?.selected_ids||[]),checks=[];
  const overview=node('section',undefined,'target-readiness');overview.setAttribute('aria-live','polite');box.append(overview);
  for(const row of source.targets){
    const item=node('details',undefined,'target-option'),head=node('summary');
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=selected.has(row.id);checkbox.disabled=frozen;checkbox.setAttribute('aria-label','选择 '+row.label);checkbox.onclick=e=>e.stopPropagation();
    head.append(checkbox,document.createTextNode(row.label+' · '+names[row.availability]));item.append(head);
    for(const [label,value] of [['出处',row.locator],['工况',row.conditions],['同工况分组',row.condition_group],['资源',row.resources],['输出',row.outputs],['采样',row.sampling],['分析',row.analysis],['比较标准',row.criterion||'尚未确定，不能冻结'],['限制',row.limitations]])item.append(node('p',label+'：'+value));
    box.append(item);checks.push([row.id,checkbox]);
  }
  const label=node('label','本次范围与未选目标说明'),reason=document.createElement('textarea');reason.rows=2;reason.maxLength=2000;reason.value=current.target_selection?.exclusion_reason||'';reason.disabled=frozen;label.append(reason);box.append(label);
  const id=current.id,revision=current.revision;
  const preview=targetReadinessRequest(()=>current?.id===id&&current.revision===revision&&generation===targetPreviewGeneration,state=>{
    overview.replaceChildren(node('h3',`已选 ${state.selected_count} / ${state.total_count} 项 · ${state.groups.length} 个工况组`));
    for(const group of state.groups)overview.append(node('p',group.labels.join('、')+'：'+group.conditions.join('；'),'target-group'));
    if(state.blockers.length){const gaps=node('ul');for(const gap of state.blockers)gaps.append(node('li',gap.message));overview.append(node('strong','开始前还需处理'),gaps);}
    const message=historical?'这是已有计算后的清单查看，不能补作事前冻结。':frozen?'范围已经固定，原始记录保留。':state.can_freeze?'范围已保存，条件和比较标准齐全，可以冻结。':!state.selection_saved?'当前选择尚未保存；保存后继续核对上述缺项。':'范围已保存，请先处理上述缺项。';
    overview.append(node('p',message,'form-note'));
    if(!frozen){$('#freeze').disabled=!state.can_freeze;$('#freeze-note').textContent=message+' 冻结不会提交计算，也不表示科学通过。';}
  },error=>{overview.replaceChildren(node('p','暂未取得目标检查结果：'+error.message));if(!frozen)$('#freeze').disabled=true;});
  const refresh=()=>{if(!frozen)$('#freeze').disabled=true;overview.replaceChildren(node('p','正在核对所选目标…'));void preview(id,{revision,selected_ids:checks.filter(([,c])=>c.checked).map(([key])=>key),exclusion_reason:reason.value});};
  for(const [,checkbox] of checks)checkbox.onchange=refresh;
  reason.oninput=refresh;refresh();
  if(!frozen){const save=node('button','保存目标选择','primary');save.onclick=()=>action(async()=>{current=await api(`/api/tasks/${current.id}/targets`,{revision:current.revision,selected_ids:checks.filter(([,c])=>c.checked).map(([id])=>id),exclusion_reason:reason.value});await afterChange('目标范围已保存；比较标准与条件齐全后才能冻结。');});box.append(save);}
  box.append(node('p','B 每轮评测最多提交两次；保存或选择不提交计算。缺项或未定比较标准不能冻结。','form-note'));
}
