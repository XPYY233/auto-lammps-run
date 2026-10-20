'use strict';
const $ = (selector) => document.querySelector(selector);
const origins = {user:'用户明确指定',paper:'论文提供',code:'作者代码提供',proposed:'建议 · 待用户确认'};
const statuses = {missing:'缺失',unselected:'待选择',conflict:'有矛盾',pending:'待确认',confirmed:'已确认'};
let schema, current = null, editing = null, resolving = null, busy = false;
let literaturePreview = null;
let pendingRoute=null, renderedRoute=null;
let paperFilter='all';
let paperOverviewCatalog=null;
let taskCache=[], workspaceReport=null, resultTab='overview', modelPreference=null, normalResult=null, rawResult=null, executionState=null;
let workspaceGeneration=0, workspaceState={task:null,phase:'loading',updated:null};
let scientificReviewState={task:null,phase:'idle',reviews:[],error:''};
let scientificReviewGeneration=0;
let referenceProgress=null;
let referenceChartSource=null,referenceChartRequest=null,referenceChartRead=0;
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
  if (!response.ok) {const error=new Error(typeof result.detail === 'string' ? result.detail : '输入格式不正确，请检查字段。');error.requestRecorded=result.request_recorded;throw error;}
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
function clearTaskView(id) {
  clearTaskView.taskId=id;
  clearTaskView.generation=(clearTaskView.generation||0)+1;
  for(const refresh of [renderHistory,refreshGuidance,refreshPlanReview,refreshResults,refreshReferenceHistory,refreshDiscussion]){
    refresh.generation=(refresh.generation||0)+1;
  }
  clearCandidateView();
  if(typeof clearPaperWorkflowView==='function')clearPaperWorkflowView(id);
  literaturePreview=null;editing=null;resolving=null;
  initialGeometryCatalog=null;initialGeometryCatalogTask=null;initialGeometryLoading=false;initialGeometryRead++;
  normalResult=null;workspaceReport=null;rawResult=null;executionState=null;referenceProgress=null;
  referenceChartSource=null;referenceChartRequest=null;referenceChartRead++;
  scientificReviewGeneration++;scientificReviewState={task:id,phase:'idle',reviews:[],error:''};
  activityData=null;activityGeneration++;
  if(activityTimer){clearTimeout(activityTimer);activityTimer=null;}
  discussionRequest=null;resultTab='overview';selectedPlot='full';evidenceViewChoice=null;closeoutPlotChoice='ab';
  workspaceGeneration++;workspaceState={task:id,phase:'loading',updated:null};
  targetPreviewGeneration++;
  if(typeof renderPaperEvidence==='function')renderPaperEvidence.generation=(renderPaperEvidence.generation||0)+1;
  for(const selector of ['#task-title','#task-prompt','#task-tags','#task-status','#task-meta','#task-model',
    '#task-files','#task-information','#task-resources','#execution-flow','#conditions','#generation-questions','#history-list',
    '#reference-progress','#reference-results','#reference-history','#reference-status','#target-planning','#initial-geometry-panel',
    '#results-content','#results-status','#research-results','#result-tabs','#workspace-refresh-status',
    '#plan-summary','#plan-files','#plan-version-list','#plan-version','#plan-status','#plan-note','#plan-revision-help','#plan-versions-label',
    '#guidance-list','#ai-activity','#ai-activity-note','#ai-current-title','#ai-current-detail','#ai-current-meta',
    '#discussion-history','#discussion-charts','#discussion-status','#previous-step','#next-action-note','#task-quick-note',
    '#reference-ai-chart-history','#reference-ai-status','#reference-ai-scope','#scientific-review']){
    const element=$(selector);if(element)element.replaceChildren();
  }
  for(const selector of ['#reference-progress','#reference-panel','#plan-review-panel','#plan-versions','.discussion-panel','#candidate-panel']){
    const element=$(selector);if(element)element.hidden=true;
  }
  for(const selector of ['#discussion-prompt','#guidance-note','#plan-revision-note']){
    const element=$(selector);if(element)element.value='';
  }
  for(const selector of ['#plan-approve','#plan-revise','#plan-recheck','#open-current-plan','#plan-revision-note',
    '#discussion-form button[type=submit]','#guidance-note','#guidance-send','#task-pause']){
    const element=$(selector);if(element)element.disabled=true;
  }
  for(const selector of ['#plan-approve','#plan-revise','#plan-recheck','#next-action']){
    const element=$(selector);if(element)element.onclick=null;
  }
  const pause=$('#task-pause');if(pause){pause.textContent='暂停任务';pause.className='quiet';}
  for(const selector of ['#condition-dialog','#resolve-dialog','#literature-dialog']){
    const dialog=$(selector);if(dialog?.open)dialog.close();
  }
  const next=$('#next-action');if(next){next.hidden=true;next.disabled=true;}
  const activity=$('#ai-current-title');if(activity)activity.textContent='正在读取当前任务进度…';
  const chartPanel=$('#reference-ai-charts');if(chartPanel)chartPanel.hidden=true;
  const reviewPanel=$('#scientific-review');if(reviewPanel)reviewPanel.hidden=true;
  const taskView=$('#task-view');if(taskView?.classList)taskView.classList.remove('results-first','accepted-results-first');
  renderWorkspaceResults();
}
async function openTask(id) {
  const generation=openTask.generation=(openTask.generation||0)+1;
  const changing=current?.id!==id;
  if(changing){
    current=null;clearTaskView(id);$('#task-view').hidden=true;
    $('#task-loading').hidden=false;$('#task-loading').textContent='正在打开任务并读取最新记录…';
    $('#previous-step').textContent='正在读取上一条记录…';
  }
  let task;
  try{task=await api('/api/tasks/'+id);}
  catch(error){
    if(changing&&pendingRoute===null&&generation===openTask.generation)$('#task-loading').textContent='任务暂时无法打开，请稍后重试或返回“我的任务”。';
    throw error;
  }
  if(pendingRoute!==null||generation!==openTask.generation)return;
  current=task;
  if(changing)$('#advanced-task').open=false;
  recordRoute('#'+id);
  render();
  $('#task-view').hidden=false;
  $('#task-loading').hidden=true;
  if(changing)$('#results-status').textContent='正在读取记录…';
  window.scrollTo({top:0});
  for(const [refresh,label] of [[listTasks,'任务列表'],[renderHistory,'修订记录'],[refreshCandidate,'方案准备'],
    [refreshGuidance,'补充要求'],[refreshPlanReview,'计算方案'],[refreshActivity,'当前进展'],
    [refreshResults,'计算结果'],[refreshReferenceHistory,'文献记录'],[refreshWorkspace,'研究结果'],[refreshDiscussion,'结果问答']]){
    if(current!==task||pendingRoute!==null||generation!==openTask.generation||$('#task-view').hidden)return;
    try{await refresh();}catch(error){if(current===task&&generation===openTask.generation)notice(label+'暂时无法读取，请刷新状态：'+error.message,true);}
  }
  if(current===task&&generation===openTask.generation&&typeof updateTaskContents==='function')updateTaskContents();
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
  if(typeof sessionStorage!=='undefined'){
    try{if(sessionStorage.getItem('auto-lammps-new-mode')==='reproduction')setMode('reproduction');}catch{}
  }
  ($('#create-form').hidden?$('#paper-pdf-file'):$('#create-form textarea')).focus();
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
  if(clearTaskView.taskId!==current.id){clearTaskView(current.id);$('#advanced-task').open=false;}
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
  if(typeof renderPaperWorkflow==='function')renderPaperWorkflow();
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
  if(!current)return;
  const id=current.id,generation=renderHistory.generation=(renderHistory.generation||0)+1;
  const {events,preparation_events=[],lifecycle_events=[]} = await api(`/api/tasks/${id}/history`);
  if(current?.id!==id||$('#task-view').hidden||generation!==renderHistory.generation) return;
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
  if(!current) return;
  const id=current.id,generation=refreshReferenceHistory.generation=(refreshReferenceHistory.generation||0)+1;
  const result=await api(`/api/tasks/${id}/reference-evidence`);
  if(current?.id!==id||$('#task-view').hidden||generation!==refreshReferenceHistory.generation) return;
  if(current.mode==='reproduction'){
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
  await refreshReferenceCharts(result.report||null);
}
async function refreshReferenceCharts(report){
  const panel=$('#reference-ai-charts'),id=current?.id,read=++referenceChartRead;
  if(!panel)return;
  const valid=Boolean(id)&&report?.task_id===id&&report?.role==='reference'&&
    /^[a-f0-9]{64}$/.test(report.source_sha256||'')&&/^[a-f0-9]{64}$/.test(report.manifest_sha256||'');
  if(!valid){panel.hidden=true;referenceChartSource=null;referenceChartRequest=null;$('#reference-ai-chart-history').replaceChildren();if(typeof updateTaskContents==='function')updateTaskContents();return;}
  if(referenceChartSource?.task!==id||referenceChartSource.sha!==report.source_sha256||referenceChartSource.manifest!==report.manifest_sha256){
    referenceChartRequest=null;$('#reference-ai-question').value='';
  }
  referenceChartSource={task:id,sha:report.source_sha256,manifest:report.manifest_sha256};panel.hidden=false;
  $('#reference-ai-scope').textContent=report.output_valid===true?
    '只从作者 A 已核验的数值表选图；已有输出完整，图表仍不代表科学复现通过。':
    '作者 A 目前仅有部分已核验数值；图表只覆盖这些数据，缺失曲线与工况不会补造。';
  $('#reference-ai-status').textContent='正在读取已有图表记录…';
  try{
    const response=await api(`/api/tasks/${id}/reference-charts`);
    if(current?.id!==id||read!==referenceChartRead||referenceChartSource?.sha!==report.source_sha256||referenceChartSource?.manifest!==report.manifest_sha256)return;
    const items=(response.charts||[]).map(item=>item.state==='completed'&&(
      item.chart?.role!=='reference'||item.chart?.source_identity?.task_id!==id||
      item.chart?.source_identity?.source_sha256!==report.source_sha256||
      item.chart?.source_identity?.manifest_sha256!==report.manifest_sha256)?{...item,state:'source_unavailable',chart:null}:item);
    renderAICharts(items,id,{box:$('#reference-ai-chart-history'),channel:'reference-charts',title:'作者 A · 已保存的数据图'});
    $('#reference-ai-submit').disabled=!response.configured;
    $('#reference-ai-status').textContent=response.configured?
      '描述想看的图，应用内 AI 只选择已有作者 A 数值列；不会运行新计算或判定科学通过。':
      '作者 A 选图服务尚未接入；已有数据和报告仍可查看。';
  }catch(error){
    if(current?.id!==id||read!==referenceChartRead)return;
    $('#reference-ai-submit').disabled=true;
    $('#reference-ai-status').textContent='图表记录暂不可读取；请刷新后查看，已有请求不会自动重发。';
  }
  if(typeof updateTaskContents==='function')updateTaskContents();
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
  const id=current.id,generation=refreshResults.generation=(refreshResults.generation||0)+1;
  try {
    const result=await api(`/api/tasks/${id}/results`);
    if(current?.id!==id || $('#task-view').hidden||generation!==refreshResults.generation) return;
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
    if(current?.id!==id || $('#task-view').hidden||generation!==refreshResults.generation) return;
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
  const generation=refreshGuidance.generation=(refreshGuidance.generation||0)+1;
  let data;
  try { data=await api(`/api/tasks/${id}/guidance`); }
  catch(error){ if(current?.id!==id||$('#task-view').hidden||generation!==refreshGuidance.generation)return;list.replaceChildren(node('li','无法读取引导：'+error.message,'subtle')); return; }
  if(current?.id!==id||$('#task-view').hidden||generation!==refreshGuidance.generation) return;
  const paused=Boolean(data.paused);
  const button=$('#task-pause');
  if(button){ button.textContent=paused?'继续任务':'暂停任务';
              button.className=paused?'primary':'quiet';button.disabled=false; }
  const send=$('#guidance-send');if(send)send.disabled=false;
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
      (job.state==='analyzed'?'数值结果已保存。打开下方核验卡片，查看已核对的依据与尚缺的科学判据；也可让应用内 AI 解释数据。':job.state==='awaiting_approval'?'请审阅下方方案，确认后提交计算。':job.state==='attention'?'当前流程需要处理，请查看下方执行说明。':job.label);
    meta.textContent='提交次数 '+job.dispatch_count+' / '+job.max_attempts+' · '+(executionState?.worker_alive?'后台服务在线':'后台服务未运行')+' · 调度完成与科学核验分别记录';
  }else if(typeof workspaceReport!=='undefined'&&workspaceReport?.closeout){
    title.textContent='基准工况 P–A–B 结果已保存';
    detail.textContent='查看论文 P、作者 A 与独立计算 B 的真实对比；全篇其他工况和正式盲测状态以结果证据为准。';
    meta.textContent='已有计算与分析记录可直接查看；不需要为这份结果重新准备方案。';
  }else if(current?.status!=='conditions_frozen'&&taskActivity?.condition_preparation){
    const condition=taskActivity.condition_preparation;
    title.textContent=condition.label||'正在核对需求中的条件';
    detail.textContent=condition.detail||'模型答复和来源核对分开记录；未通过核对的条件不会写入方案。';
    meta.textContent='条件整理已记录 '+(condition.call_count||0)+' 次模型调用 · '+(condition.state==='failed'?'尚未生成方案或提交计算；失败和费用保留。':'条件确认后才能准备方案。');
    if(generate)generate.textContent=condition.recovery_required&&!condition.reconstructed?'恢复已返回的条件（不新增调用）':'根据需求整理条件';
  }else{
    title.textContent=taskActivity?.now||'等待准备计算方案';
    detail.textContent=candidateRecord?.state==='prepared'?'方案已准备，等待你的确认。':current?.status==='conditions_frozen'?'应用按已确认的研究条件组织方案。':'需求已保存；先整理并确认必要条件，应用才会准备计算方案。';
    meta.textContent='进度来自应用记录；刷新页面不会重新生成方案或提交计算。';
  }
  renderNextAction();
}

function askAIAboutResults(sendImmediately=false){
  const panel=$('.discussion-panel'),input=$('#discussion-prompt'),note=$('#task-quick-note');
  if(!current||!panel||panel.hidden){if(note)note.textContent='已有可用结果后，才能核对结果与研究目标。';return;}
  const freshQuestion=!input.value.trim();
  if(freshQuestion)input.value='请依据本任务已冻结的研究条件和已经核验的计算结果，逐项核对：计算方法是否符合研究目标；关键数值、单位、收敛与异常是否有证据；目前能确认什么、还缺什么。每项引用具体数据或说明证据缺失。不要把调度结束或数值处理完成说成科学验收通过，也不要发起新计算。';
  panel.scrollIntoView({block:'start',behavior:'smooth'});input.focus();
  const form=$('#discussion-form'),enabled=!$('#discussion-form button[type=submit]').disabled;
  $('#discussion-status').textContent=!enabled?'先从“设置”连接应用内模型，然后回到这里提问；已保存的结果核验记录仍可查看。':'请检查问题后点击“询问结果”。AI 会解释已有证据；正式科学验收状态不会因此改变。';
  if(note)note.textContent='已打开结果核对问题，请检查后发送。';
  if(sendImmediately&&freshQuestion&&enabled){form.requestSubmit();return;}
}
function prepareResultReview(){
  const review=$('#scientific-review'),note=$('#task-quick-note');
  if(review&&!review.hidden){review.scrollIntoView({block:'start',behavior:'smooth'});if(note)note.textContent='已打开结果核验。需要解释时，可在核验卡片中继续向 AI 提问。';return;}
  askAIAboutResults();
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
      if(!$('.discussion-panel')?.hidden){
        button.textContent='核对结果与研究目标';button.hidden=false;button.onclick=prepareResultReview;
        note.textContent='应用核对已保存的数据与来源；缺少的科学判据会明确显示。';return;
      }
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
      jump('查看方案准备进度','#candidate-panel','应用内 AI 正在准备方案，后台会继续处理。','#advanced-task');return;
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
  if(typeof workspaceReport!=='undefined'&&workspaceReport?.closeout){
    jump('查看基准复现结果','#research-results','P–A–B 基准对比已保存；全篇未覆盖范围见结果限制。');return;
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
    jump('查看条件整理进度','#ai-activity-panel','应用内 AI 正在整理研究条件；完成后会显示需要你核对的内容。','#ai-activity-panel');return;
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
  for(const table of plan.analysis?.plan?.tables||[]){
    const fields=Array.isArray(table.columns)?table.columns.map(c=>c.name+' ['+c.unit+']').join('、'):
      table.format==='lammps_dump'?`LAMMPS 原子轨迹 · 长度单位 ${table.length_unit||'见分析定义'} · 原子类型 ${Object.entries(table.elements||{}).sort(([a],[b])=>Number(a)-Number(b)).map(([type,element])=>`${type} → ${element}`).join(' / ')}`:
      `结构化数据 · 格式 ${table.format||'见分析定义'}`;
    outputs.append(node('p',`${table.file}：${fields}`));
  }
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
  const generation=refreshPlanReview.generation=(refreshPlanReview.generation||0)+1;
  let review;
  try{review=await api(`/api/tasks/${id}/plan`);}
  catch(error){if(current?.id!==id||$('#task-view').hidden||generation!==refreshPlanReview.generation)return;$('#plan-status').textContent='暂时无法读取方案，已展示的方案未更新。请刷新核对。';$('#plan-approve').disabled=true;$('#plan-revise').disabled=true;$('#plan-recheck').disabled=true;return;}
  if(current?.id!==id||$('#task-view').hidden||generation!==refreshPlanReview.generation)return;
  const plan=review.workspace?.current;
  const prepared=review.state==='prepared'&&(!review.workspace||(plan&&!plan.historical));
  const sameDraft=review.same_plan_review||review.workspace?.same_plan_review||{};
  const recheck=$('#plan-recheck');recheck.hidden=!sameDraft.can_recheck;recheck.disabled=!sameDraft.can_recheck;
  panel.hidden=!prepared&&!plan&&!sameDraft.proposal_request_id;$('#open-current-plan').disabled=panel.hidden;if(panel.hidden){renderNextAction();return;}
  $('#plan-version').textContent=(plan?`第 ${plan.version} 版${plan.historical?' · 上一份已准备方案':''}`:'已准备')+' · '+proposalRoundLabel(review);
  if(!prepared&&!plan)$('#plan-version').textContent='已保存原稿 · '+proposalRoundLabel(review);
  $('#plan-status').textContent=prepared?(review.approved?'当前方案已批准。':'方案已准备，等待你确认。'):!plan?
    `${sameDraft.reason||'原稿正在重新审核。'} 原方案和费用保留；重审不生成新稿、不提交计算。`:
    '应用正在处理下一版；下方保留最近一版方案供查看，暂不能批准。';
  renderPlanSummary(review);renderPlanVersions(review);
  const files=$('#plan-files');const opened=new Set([...files.children].filter(x=>x.open).map(x=>x.dataset?.file));files.replaceChildren();
  for(const file of (plan?.files||review.files||[])){
    const block=node('details',undefined,'plan-file');
    block.dataset.file=file.name;block.open=opened.has(file.name);
    const labels={'in.lammps':'LAMMPS 计算脚本','analysis.json':'数据分析定义','structure.data':'初始原子结构'};
    block.append(node('summary',`${labels[file.name]||'其他尺寸初始结构'} · ${file.name} · ${(file.size/1024).toFixed(1)} KiB`));
    if(file.content)block.append(node('pre',file.content,'plan-code'));
    block.append(node('p','文件校验值：'+file.sha256,'form-note'));files.append(block);
  }
  let execution=executionState||{},executionReadable=true;
  // A refresh of the plan always reads the real dispatch receipt; approval never regenerates a plan.
  try{execution=await api(`/api/tasks/${id}/execution`);}catch(error){execution={};executionReadable=false;}
  if(current?.id!==id||$('#task-view').hidden||generation!==refreshPlanReview.generation)return;
  const job=execution.job||{},dispatched=Boolean(job.job_id);
  const active=['queued','running','waiting','dispatching'].includes(job.state);
  const unresolved=['unknown','reconcile_required'].includes(job.scheduler_state);
  const blocked=!executionReadable||execution.configured===false;
  renderFormalBRetestBudget(id,panel,execution.formal_b_retest);
  recheck.disabled=!sameDraft.can_recheck||active||unresolved||blocked;
  recheck.onclick=()=>action(async()=>{
    if(recheck.disabled)return;
    recheck.disabled=true;
    const used=sameDraft.review_rounds?.used||0;
    if(!recheck.request||recheck.request.task!==id||recheck.request.proposal!==sameDraft.proposal_sha256||recheck.request.used!==used)
      recheck.request={id:crypto.randomUUID().replaceAll('-',''),task:id,proposal:sameDraft.proposal_sha256,used};
    await api(`/api/tasks/${id}/plan/recheck`,{revision:current.revision,request_id:recheck.request.id});
    if(current?.id!==id)return;
    await afterChange('原稿已交给应用内 AI 重新审核；不会生成新稿或提交 HPC。');
    await refreshPlanReview();await refreshCandidate();await refreshActivity();
  });
  const retryAvailable=Boolean(job.can_retry||job.can_retry_after_approval);
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

function renderFormalBRetestBudget(id,parent,data){
 let panel=$('#formal-b-retest-budget');
 if(!panel){panel=node('section',undefined,'result-widget');panel.id='formal-b-retest-budget';parent.append(panel);}
 panel.replaceChildren();panel.dataset.task=id;panel.hidden=!data?.can_add_supplement&&!data?.grant;
 if(panel.hidden)return;
 const alive=()=>current?.id===id&&panel.dataset.task===id&&!$('#task-view').hidden;
 panel.append(node('h4','第二次 B 开发复测额度'),node('p','原评测失败、取消与超支继续保留；追加额度只用于同一任务的第二次提交，不清零历史，不自动调用模型或提交计算。','form-note'));
 if(data.grant){
  panel.append(node('p',`已记录追加 ${(data.grant.additional_core_seconds/3600).toFixed(2)} CPU 核时；当前剩余 ${((data.resource_limit?.remaining_core_seconds||0)/3600).toFixed(2)} CPU 核时。`,'form-note'));return;
 }
 const hours=node('input');hours.type='number';hours.min='0.01';hours.step='0.01';hours.setAttribute('aria-label','第二次开发复测新增 CPU 核时');
 const note=node('textarea');note.setAttribute('aria-label','补充核时授权说明');note.placeholder='记录你明确批准的额度与开发复测用途；原失败和费用继续保留。';
 const confirm=node('button','记录补充核时授权（不提交计算）','quiet'),feedback=node('p','','form-note');feedback.setAttribute('role','status');
 let pending=false;
 confirm.onclick=async()=>{
  if(!alive()||pending)return;
  const amount=Number(hours.value),reason=note.value.trim(),seconds=Math.round(amount*3600);
  if(!Number.isFinite(amount)||amount<=0||!Number.isSafeInteger(seconds)||!seconds||!reason){feedback.textContent='请填写明确的正数核时和授权说明。';return;}
  pending=true;confirm.disabled=true;
  try{await api(`/api/tasks/${id}/execution/development-supplement`,{revision:current.revision,parent_request_id:data.parent_request_id,additional_core_seconds:seconds,note:reason});if(alive())await refreshPlanReview();}
  catch(error){if(alive())feedback.textContent=error.message;}
  finally{pending=false;if(alive())confirm.disabled=false;}
 };
 panel.append(hours,note,confirm,feedback);
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
  const previousQuestions=candidateRecord?.result?.questions||[];
  const drafts=candidateTask===id&&candidateRecord?.id===candidate?.id&&candidateRecord?.state==='clarification'&&candidate?.state==='clarification'?new Map(
    [...document.querySelectorAll('#candidate-attention textarea[data-question-index]')].map(input=>{
      const index=Number(input.dataset.questionIndex);return [index,{question:questionText(previousQuestions[index]),value:input.value}];
    })):new Map();
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
        const draft=drafts.get(index);if(draft?.question===questionText(question))answer.value=draft.value;
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
  if(!candidate) {renderCandidateClarification(null);renderNextAction();renderWorkspaceResults();return;}
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
  renderWorkspaceResults();
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
  await appendPaperRecommendations();
}

let paperRecommendationScope=null;
function recommendationScope(){
 if(paperRecommendationScope)return paperRecommendationScope;
 try{const saved=localStorage.getItem('auto-lammps-paper-recommendation-scope');if(/^[a-f0-9]{32}$/.test(saved||''))paperRecommendationScope=saved;}catch{}
 if(!paperRecommendationScope){paperRecommendationScope=crypto.randomUUID().replaceAll('-','');try{localStorage.setItem('auto-lammps-paper-recommendation-scope',paperRecommendationScope);}catch{}}
 return paperRecommendationScope;
}
async function appendPaperRecommendations(){
 const view=$('#papers-view');if(!view||view.hidden)return;
 let panel=$('#paper-recommendations');
 if(!panel){panel=node('details');panel.id='paper-recommendations';panel.append(node('summary','让应用 AI 从成套资源中推荐论文 · 查看来源、势函数与缺项'));view.append(panel);}
 const scope=recommendationScope(),body=node('div');panel.replaceChildren(node('summary','让应用 AI 从成套资源中推荐论文 · 查看来源、势函数与缺项'),body);
 const alive=()=>!view.hidden&&panel.isConnected&&recommendationScope()===scope;
 body.append(node('p','优先读取资源库中的作者工作流与势函数配对。推荐仅说明目录证据；选定后仍需核验全文、提取 P 并确定计算目标，不会提交 HPC。','form-note'));
 let records=[],status={};
 try{const response=await api('/api/paper-recommendations/'+scope);records=response.history||[];status=response.status||{};}catch(error){if(alive())body.append(node('p','推荐记录暂不可读取；先刷新记录，未发送新请求。','form-note'));return;}
 if(!alive())return;
 const labels={completed:'应用 AI 已推荐候选',unavailable:'现有目录没有符合条件的成套候选',not_sent:'模型请求未发送',rejected:'模型服务拒绝请求',attempted:'模型请求已尝试，未取得有效答复',invalid_output:'模型推荐未通过来源核验',source_unavailable:'推荐来源版本已变化',connection_changed:'模型连接已变化',failed_or_unknown:'模型请求状态待核对',unknown:'模型请求状态待核对'};
 for(const record of records){
  const item=node('article');item.append(node('h3',labels[record.state]||record.state),node('p',record.question||''));
  const result=record.document||{};if(result.message)item.append(node('p',result.message));
  const candidate=result.candidate;
  if(candidate&&record.state==='completed'){
   item.append(node('h4',candidate.paper_title),node('p',result.reason||''));
   const doi=node('a','DOI '+candidate.doi);doi.href='https://doi.org/'+candidate.doi;doi.target='_blank';doi.rel='noopener noreferrer';item.append(doi);
   for(const [kind,label] of [['author_source','作者 LAMMPS 工作流'],['potential','配套势函数']]){const resource=candidate[kind]||{};item.append(node('p',label+'：'+resource.repository+' @ '+String(resource.commit||'').slice(0,12)),node('p',(resource.elements||[]).join(' · ')+' · '+(resource.pair_styles||[]).join(' / ')+' · 许可 '+(resource.license||'待核验')));}
   item.append(node('p','这是资源目录推荐，尚未核验为可运行或科学复现成功。请通过“导入我的论文”继续全文与 P 提取。','form-note'));
  }
  if(record.usage?.total_tokens)item.append(node('p','已记录模型用量：'+record.usage.total_tokens+' tokens','form-note'));
  body.append(item);
 }
 const limit=Number.isInteger(status.max_requests)&&status.max_requests>=3&&status.max_requests<=6?status.max_requests:3;
 const unresolved=typeof status.blocked_by_unresolved==='boolean'?status.blocked_by_unresolved:records.some(record=>['unknown','failed_or_unknown','attempted'].includes(record.state));
 const field=node('textarea');field.id='paper-recommendation-question';field.maxLength=1000;field.setAttribute('aria-label','论文推荐要求');field.value=records.at(-1)?.question||'请推荐下一篇用于完整 P–A–B 验证的论文。优先作者 LAMMPS 工作流、势函数、元素映射和许可已配对、原文条件与源码无已知冲突的候选；明确仍待核验的正文及资源缺项。';
 const excluded=(paperOverviewCatalog?.result?.papers||[]).filter(p=>p.selection==='selected').map(p=>p.doi).filter(Boolean);
 body.append(node('label','论文推荐要求'),field,node('p','本次默认排除已选论文：'+(excluded.join('、')||'无')+`。已使用 ${records.length}/${limit} 次推荐；未知请求不自动重发。`,'form-note'));
 if(status.recovery_authorized)body.append(node('p','已批准本筛选任务最多三次开发恢复；原失败、费用和未获答复记录保留，B 的限制保持。','form-note'));
 const run=node('button','让应用 AI 推荐下一篇','primary');run.disabled=unresolved||records.length>=limit||status.can_request===false;body.append(run);
 if(unresolved)body.append(node('p','请先核对已有模型回执，不能换编号重复发送。','form-note'));
 run.onclick=()=>action(async()=>{if(!alive()||run.disabled)return;run.disabled=true;await api('/api/paper-recommendations',{scope_id:scope,request_id:crypto.randomUUID().replaceAll('-',''),question:field.value,exclude_dois:excluded});if(alive())await appendPaperRecommendations();});
 const refresh=node('button','刷新推荐与来源记录','quiet');refresh.onclick=()=>action(appendPaperRecommendations);body.append(refresh);
}
async function refreshPapers() {
  const result=await api('/api/papers');
  const availableTasks=(await api('/api/tasks')).tasks.filter(t=>t.mode==='reproduction');
  paperOverviewCatalog={result,availableTasks};renderPaperOverview();
}
function renderPaperOverview(){
  if(!paperOverviewCatalog)return;
  const {result,availableTasks}=paperOverviewCatalog;
  const expanded=new Set([...document.querySelectorAll('[data-paper-history][open]')].map(d=>d.dataset.paperHistory));
  const expandedSources=new Set([...document.querySelectorAll('[data-paper-source][open]')].map(d=>d.dataset.paperSource));
  $('#paper-filters').replaceChildren();
  const total=Object.values(result.counts).reduce((sum,n)=>sum+n,0);
  for (const [key,label] of Object.entries({all:'全部已选',...result.statuses})) {
    const b=node('button',`${label} ${key==='all'?total:result.counts[key]}`,paperFilter===key?'primary':'quiet');
    b.setAttribute('aria-pressed',String(paperFilter===key));
    b.onclick=()=>{paperFilter=key;renderPaperOverview();};
    $('#paper-filters').append(b);
  }
  $('#paper-list').replaceChildren(); $('#candidate-list').replaceChildren();
  const matches=p=>(p.title+' '+p.doi).toLowerCase().includes($('#paper-search').value.trim().toLowerCase());
  const selected=result.papers.filter(p=>matches(p)&&p.selection==='selected'&&(paperFilter==='all'||p.status===paperFilter))
    .sort((a,b)=>({reproduced:0,in_progress:1,pending:2}[a.status]??3)-({reproduced:0,in_progress:1,pending:2}[b.status]??3));
  if (!selected.length) $('#paper-list').append(node('p',paperFilter==='reproduced'?'尚无通过独立科学核验的复现记录。':'此分类还没有选定文章。','paper-empty'));
  for (const p of selected) $('#paper-list').append(paperCard(p,result.statuses,availableTasks));
  const candidates=result.papers.filter(p=>matches(p)&&p.selection==='candidate');
  for (const p of candidates) $('#candidate-list').append(paperCard(p,result.statuses,availableTasks));
  $('#candidate-heading').textContent=`候选文章 · ${candidates.length} 篇`;
  if($('#paper-search').value.trim()&&candidates.length)$('#candidate-section').open=true;
  for (const d of document.querySelectorAll('[data-paper-history]')) d.open=expanded.has(d.dataset.paperHistory);
  for (const d of document.querySelectorAll('[data-paper-source]')) d.open=expandedSources.has(d.dataset.paperSource);
  $('#paper-updated').textContent='已选 '+selected.length+' 篇 · 更新于 '+new Date().toLocaleTimeString('zh-CN');
}
function paperCard(p,statuses,availableTasks) {
  const card=node('article',undefined,'paper-card');
  const top=node('div',undefined,'paper-card-top');
  const link=node('a',p.title); link.href=p.doi_url; link.target='_blank'; link.rel='noopener noreferrer';
  top.append(link,node('span',p.selection==='candidate'?'候选':p.reproduction_accepted?.length?'已复现 · 验收范围':statuses[p.status],'badge '+(p.status==='reproduced'?'confirmed':'pending')));
  card.append(top,node('p','DOI '+p.doi,'subtle'));
  if(p.reproduction_accepted?.length)card.append(node('p',p.reproduction_accepted.map(item=>item.scope).filter(Boolean).join('；'),'paper-accepted-scope'));
  else card.append(node('p',p.tasks.length?`${p.tasks.length} 项关联研究`:'尚未创建研究任务','subtle'));
  const description=node('details',undefined,'paper-description');description.append(node('summary','复现范围与说明'),node('p',p.scope),node('p',p.note),node('p',p.stage));
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
  card.append(description);
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
  if(typeof sessionStorage!=='undefined'){try{sessionStorage.setItem('auto-lammps-new-mode',mode);}catch{}}
  $('#create-form [name=mode]').value=mode;
  $('#mode-research').classList.toggle('selected',mode==='research');$('#mode-reproduction').classList.toggle('selected',mode==='reproduction');
  $('#mode-research').setAttribute('aria-pressed',String(mode==='research'));$('#mode-reproduction').setAttribute('aria-pressed',String(mode==='reproduction'));
  $('#reproduction-hint').hidden=mode!=='reproduction';
  $('#create-form').hidden=mode==='reproduction';
  $('#reproduction-hint').hidden=true;
  $('#paper-intake-panel').hidden=mode!=='reproduction';
  if(mode==='reproduction')refreshPaperIntake().catch(error=>paperIntakeError(error.message));
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
 if(state==='completed')return task.mode==='research'?'核对结果':'查看结果';
 if(state==='validated')return '查看结果';
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
 const filters=$('#task-filters');filters.replaceChildren();for(const [key,label] of Object.entries({all:'全部任务',attention:'待我处理',running:'运行中',queued:'排队中',completed:'计算结束',validated:'验收通过',finished:'已确认结束',failed:'失败',draft:'待准备'})){const b=node('button',label,taskFilter===key?'primary':'quiet');b.setAttribute('aria-pressed',String(taskFilter===key));b.onclick=()=>{taskFilter=key;taskCards();};filters.append(b);}
 const query=($('#tasks-query')?.value||$('#task-search').value||'').trim().toLowerCase(),sort=$('#tasks-sort')?.value||'recent';
 const tasks=taskCache.filter(t=>t.title.toLowerCase().includes(query)&&taskMatchesFilter(t,taskFilter));
 tasks.sort((a,b)=>sort==='name'?a.title.localeCompare(b.title,'zh-CN'):((Date.parse(a.updated_at)||0)-(Date.parse(b.updated_at)||0))*(sort==='oldest'?1:-1));
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
 const lifecycleBlocker=taskRecordActionBlocker(t),menu=node('details',undefined,'task-manage');menu.append(node('summary','管理'));
 if(lifecycleBlocker)menu.append(node('small',lifecycleBlocker,'form-note'));
 else{
  if(!taskFinished(t))menu.append(removalButton('确认任务结束','再次点击确认结束',()=>manage('finish')));
  menu.append(removalButton('删除任务记录','确认从列表删除',()=>manage('delete')));
 }
 actions.append(menu);
 [info,progress,next,counts,changed,actions].forEach((cell,i)=>cell.dataset.label=['任务','当前阶段','下一步','作业 / 提交次数','最近更新','操作'][i]);
 tr.append(info,progress,next,counts,changed,actions);body.append(tr);}box.append(table);

}
function markTaskListFresh(){
 const note=$('#tasks-last-sync');
 if(note)note.textContent='状态更新于 '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})+' · 页面停留时会自动更新';
}
async function showTasks(){current=null;hideViews('tasks-view');$('.search-box').hidden=true;$('#tasks-query').value=$('#task-search').value;selectNavigation('tasks');recordRoute('#tasks');await listTasks();taskCards();markTaskListFresh();}
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
function resourceElementLabel(elements){
 const names={Nb:'铌',Ti:'钛',Zr:'锆',Mo:'钼',V:'钒',Fe:'铁',Cu:'铜',Al:'铝',W:'钨',Ta:'钽',Ni:'镍',Co:'钴',Cr:'铬',Si:'硅',C:'碳',O:'氧',H:'氢'};
 return elements.map(e=>names[e]?`${e}（${names[e]}）`:e).join(' · ')||'元素待核验';
}
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
   type:family,elements:resourceElementLabel(elements),elementList:elements,styles,
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
let discoveryLibraryNote='',resourceTab='potential',resourceFacets={elements:'',style:'',source:'',role:'',process:''},resourceStats=[],resourcePage=1;
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
  button.setAttribute('aria-pressed',String(key===resourceTab));
  button.onclick=()=>{if(resourceTab===key)return;resourceTab=key;resourcePage=1;resourceFacets={elements:'',style:'',source:'',role:'',process:''};renderResourceTabs();renderResourceTable();};
  box.append(button);
 }
}
function renderResourceStats(){
 const box=$('#resource-stats');box.replaceChildren();
 for(const [label,value,note] of resourceStats){const card=node('div',undefined,'resource-stat');card.append(node('strong',value),node('span',label));if(note)card.append(node('small',note));box.append(card);}
 const source=$('#resource-provenance-note');if(source)source.textContent=discoveryLibraryNote;
}
function renderResourceFacets(rows){
 const box=$('#resource-facets');box.replaceChildren();
 const groups=resourceFacetGroups();
 if(!groups.length){box.append(node('p','该页签没有可筛选的维度。','form-note'));return;}
 const reset=node('button','清除筛选','text-button');reset.type='button';reset.disabled=!Object.values(resourceFacets).some(Boolean);
 reset.onclick=()=>{resourceFacets={elements:'',style:'',source:'',role:'',process:''};resourcePage=1;renderResourceTable();};box.append(reset);
 for(const [key,title] of groups){
  const counts=new Map();for(const r of rows){const v=resourceFacetOf(r,key);counts.set(v,(counts.get(v)||0)+1);}
  const secondary=['source','role'].includes(key),sec=node(secondary?'details':'section',undefined,'resource-facet');sec.append(node(secondary?'summary':'h3',title));sec.open=Boolean(resourceFacets[key]);const ul=node('ul');
  const allButton=node('button','全部（'+rows.length+'）');allButton.type='button';allButton.className='facet-value'+(resourceFacets[key]?'':' selected');
  allButton.onclick=()=>{resourceFacets[key]='';resourcePage=1;renderResourceTable();};
  const all=node('li');all.append(allButton);ul.append(all);
  for(const [value,count] of [...counts.entries()].sort((a,b)=>b[1]-a[1]||String(a[0]).localeCompare(String(b[0]),'zh'))){
   const button=node('button',value+'（'+count+'）');button.type='button';button.className='facet-value'+(resourceFacets[key]===value?' selected':'');
   button.onclick=()=>{resourceFacets[key]=resourceFacets[key]===value?'':value;resourcePage=1;renderResourceTable();};
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
 $('#resource-count').textContent=rows.length
   ? `${meta[1]} · ${rows.length} 项${rows.length!==tabRows.length?'（全部 '+tabRows.length+' 项）':''}`
   : `${meta[1]} · 0 项`;
 if(!rows.length){
  box.append(tabRows.length
    ? emptyState('没有匹配的资源','调整关键词或左侧筛选。')
    : emptyState('该页签尚未接入真实条目',meta[2]));
  return;
 }
 const columns=RESOURCE_COLUMNS[resourceTab]||RESOURCE_COLUMNS.potential;
 const table=node('table',undefined,'research-table'),head=node('thead'),hr=node('tr'),body=node('tbody');
 for(const label of columns)hr.append(node('th',label));head.append(hr);table.append(head,body);
 const pageCount=Math.max(1,Math.ceil(rows.length/25));resourcePage=Math.min(resourcePage,pageCount);
 for(const r of rows.slice((resourcePage-1)*25,resourcePage*25)){
  const tr=node('tr');tr.dataset.resourceKind=r.kind;tr.dataset.resourceState=r.state;
  const name=node('td');name.append(node('strong',r.name));
  if(r.filename)name.append(node('small','原始文件：'+r.filename,'mono'));
  if(r.tags?.length){const tags=node('div',undefined,'resource-tags');for(const tag of r.tags)tags.append(node('span',tag,'badge'));name.append(tags);}
  if(r.companion&&!r.bundle)name.append(node('small','配套文件：'+r.companion));
  const source=node('td',(r.sourceType?({github:'文献仓库',nist_ipr:'NIST IPR',openkim:'OpenKIM'}[r.sourceType]||r.sourceType):'已登记资源')
    +(r.repositoryRole&&r.repositoryRole!=='author_source'?' · '+resourceFacetOf(r,'role'):''));
  const state=node('td');state.append(node('span',r.state,(r.state||'').includes('候选')?'badge':'badge warn'));
  const evidence=node('td');evidence.append(resourceDetail(r));
  if(resourceTab==='tools'){tr.append(name,node('td',r.type),source,evidence);}
  else{
   const second=resourceTab==='potential'?node('td',r.elements||'—'):node('td',(r.process||[]).join('、')||'过程待核');
   const third=resourceTab==='potential'?(r.styles||[]).length?r.styles.join(' / ').toUpperCase():'—':(r.paper||'论文待核');
   tr.append(name,second,node('td',third),source,state,evidence);
  }
  body.append(tr);
 }
 box.append(table);
 const pages=node('nav',undefined,'resource-pagination');pages.setAttribute('aria-label','资源分页');
 const previous=node('button','上一页','quiet'),next=node('button','下一页','quiet');previous.disabled=resourcePage===1;next.disabled=resourcePage===pageCount;
 previous.onclick=()=>{resourcePage--;renderResourceTable();};next.onclick=()=>{resourcePage++;renderResourceTable();$('#resource-cards').scrollIntoView({block:'start'});};
 pages.append(node('span',`第 ${resourcePage} / ${pageCount} 页 · 每页 25 项`),previous,next);box.append(pages);
}
async function showResources(){
 current=null;hideViews('resources-view');selectNavigation('resources');recordRoute('#resources');
 const result=await api('/api/papers');resourceRows=[];resourceStats=[];resourcePage=1;let discovered;
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
   const elementDetail=isPotential?resourceElementLabel(d.elements)+(d.elements.length&&!d.elements_complete?'（映射不完整）':''):((d.process||[]).join('、')||'过程待核');
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
$('#resource-search').oninput=()=>{resourcePage=1;renderResourceTable();};
$('#resource-refresh').onclick=()=>action(showResources);

function showHelp(){current=null;hideViews('help-view');selectNavigation('help');recordRoute('#help');renderHelp();}
function emptyState(title,message){const box=node('div',undefined,'empty-result');box.append(node('span','◇','empty-icon'),node('h3',title),node('p',message));return box;}
function number(v,digits=4){return Number.isFinite(v)?v.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:0}):'—';}
function fileSize(bytes){if(bytes<1024)return number(bytes,0)+' B';if(bytes<1024**2)return number(bytes/1024,2)+' KiB';if(bytes<1024**3)return number(bytes/1024**2,2)+' MiB';return number(bytes/1024**3,2)+' GiB';}
function currentRawFiles(){return rawResult?.task===current?.id?rawResult.files:[];}
function duration(seconds){const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=Math.floor(seconds%60);return `${h} 小时 ${m} 分 ${s} 秒`;}
function addInfo(label,value){
 const box=$('#task-information');
 if(workspaceReport?.closeout?.acceptance?.status==='accepted_by_user'&&[...box.querySelectorAll('dt')].some(item=>item.textContent===label&&item.nextElementSibling?.textContent===String(value)))return;
 box.append(node('dt',label),node('dd',value));
}
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
 let box=$('#execution-flow');box.replaceChildren();if(report){const details=node('details',undefined,'reference-flow');details.append(node('summary','查看作者参考 A 的执行阶段'));box.append(details);box=details;}const head=node('div',undefined,'flow-heading');head.append(node('h2',report?'作者参考 A · 执行流程':'任务执行流程'),node('span',report?'完整运行 '+duration(report.runtime.elapsed_seconds):current?.status==='conditions_frozen'?'条件已冻结 · 准备方案':'需求已保存 · 核对研究条件'));box.append(head);
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
 const r=reference.closeout,a=r.acceptance||{},accepted=a.status==='accepted_by_user';
 const context=node(accepted?'details':'section',undefined,accepted?'baseline-context':'');
 if(accepted)context.append(node('summary',acceptanceLabel(a)+' · 查看范围与方法'));
 context.append(evidenceLegend());
 const banner=node('section',undefined,'acceptance-note');banner.append(node('span',acceptanceLabel(a),'badge'),node('strong',scientificLabel(r)),node('p',a.scope||'验收范围未记录'),node('small',`${a.date||'日期未记录'} · 来源：${a.source||'未记录'}；验收只覆盖上述范围，未覆盖的工况不作为已复现；拟合口径等诊断差异见结果限制。`));context.append(banner);box.append(context);
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
function emptyResultGuidance(){
 const job=executionState?.job||ordinaryExecutionJob();
 if(job){
  if(['queued','running','waiting','dispatching','collecting','analyzing'].includes(job.state))return ['计算与分析正在进行','应用正在后台跟进；刷新或关闭页面都不会重新提交计算。','查看计算进度','#execution-flow'];
  if(job.state==='awaiting_approval')return ['计算方案等待确认','方案尚未提交计算。请先核对方案与执行范围。','查看计算方案','#plan-review-panel'];
  if(job.state==='completed')return ['计算已结束，结果仍待核对','应用还在核对输出与分析记录。调度结束不等于已有可用结果。','查看回收与分析进度','#execution-flow'];
  if(job.state==='unknown'||job.state==='reconcile_required')return ['提交状态待核对','现有回执尚不能确认计算是否完成；应用不会据此再次提交。','查看提交记录','#execution-flow'];
  return ['本次计算尚无可用结果','请先核对作业状态、失败原因和已有提交记录。','查看计算问题','#execution-flow'];
 }
 if(candidateRecord){
  if(['queued','running','model_requested','reusing_plan','checking_plan','repairing_plan','preparing_files'].includes(candidateRecord.state))return ['应用 AI 正在准备方案','方案完成并确认后才能提交计算；这里不会显示尚未产生的结果。','查看准备进度','#candidate-panel'];
  if(candidateRecord.state==='clarification')return ['方案需要补充条件','应用 AI 留下了待回答的问题；完成条件前还没有计算结果。','回答方案问题','#candidate-attention'];
  if(['failed','interrupted','configuration_changed'].includes(candidateRecord.state))return proposalRoundsExhausted(candidateRecord)
   ? ['方案机会已用完，尚无计算结果','三轮方案机会已用完或历史轮次无法核验；已有记录保留，不会自动追加调用。','查看未完成原因','#candidate-attention']
   : ['计算方案尚未准备好','请查看失败原因与已有记录；目前没有计算结果。','查看未完成原因','#candidate-attention'];
  if(candidateRecord.state==='prepared')return ['计算方案等待确认','方案已准备，但尚未提交计算。请先核对方案与执行范围。','查看计算方案','#plan-review-panel'];
 }
 if(current.mode==='reproduction')return ['复现结果尚未生成','请查看 P–A–B 进度；作者参考运行、独立计算和科学核验分别记录。','查看 P–A–B 进度','#reference-progress'];
 if(current.status==='conditions_frozen')return ['研究条件已确认，尚未计算','下一步由应用 AI 准备计算方案；方案确认后才会提交计算。','查看方案准备','#candidate-panel'];
 return ['先完成研究条件','需求已保存。确认材料、势函数与计算条件后，应用才能准备方案。','核对研究条件','#advanced-task'];
}
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
 if(!r){
  const [title,detail,label,target]=emptyResultGuidance(),empty=emptyState(title,detail),button=node('button',label,'quiet');
  button.type='button';button.onclick=()=>{
   const destination=$(target);
   if(!destination||destination.closest('[hidden]')||destination.hidden){$('#task-quick-note').textContent='这一步尚无可查看的记录；请查看上方当前进展。';$('#ai-current-title').scrollIntoView({block:'start'});return;}
   if(destination.closest('#advanced-task'))$('#advanced-task').open=true;
   destination.scrollIntoView({block:'start',behavior:'smooth'});
  };
  empty.append(button);box.append(empty);return;
 }
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
function renderScientificReviews(){
 const box=$('#scientific-review'),state=scientificReviewState;
 box.replaceChildren();box.hidden=state.phase==='idle';
 if(box.hidden)return;
 const heading=node('div',undefined,'scientific-review-heading');
 heading.append(node('div',undefined,'scientific-review-title'));
 heading.firstChild.append(node('span','结果核验','workspace-kicker'),node('h2','这些结果能说明什么？'));
 box.append(heading);
 if(state.phase==='loading'){box.append(node('p','正在核对已保存的计算、分析和原始数据…','form-note'));return;}
 if(state.error)box.append(node('p',state.error,'scientific-review-warning'));
 if(!state.reviews.length){box.append(node('p','尚无可用的结果核验记录。请查看分析报告与计算历史。','form-note'));return;}
 const latest=[...state.reviews].sort((a,b)=>b.at.localeCompare(a.at))[0];
 if(latest.state==='source_unavailable'){
  box.append(node('p','原始来源或报告已经变化，旧核验记录仍保留；旧数值暂不展示。','scientific-review-warning'));
 }else{
  box.append(node('p',latest.summary,'scientific-review-summary'));
  const checks=node('ul',undefined,'scientific-review-checks');
  for(const check of latest.checks){const item=node('li');item.append(node('span',check.state==='verified'?'✓':'!','scientific-review-mark '+check.state),node('span',check.label));checks.append(item);}box.append(checks);
  const e=latest.evidence;
  box.append(node('p',`依据：作业 ${e.job_id}；${e.source_tables.length} 份数值来源；${e.numerical_results.length} 项数值分析；${e.structural_result_count} 项结构分析；${e.site_result_count} 项位点分析。`,'form-note'));
  box.append(node('p',latest.next_step,'form-note'));
 }
 const actions=node('div',undefined,'scientific-review-actions');
 if(latest.state!=='source_unavailable'){
  const download=node('a','下载核验记录 ↓','quiet');download.href=`/api/tasks/${current.id}/scientific-reviews/${latest.id}/download`;actions.append(download);
 }
 if(!$('.discussion-panel').hidden){const ask=node('button','让应用内 AI 解释证据','quiet');ask.type='button';ask.onclick=()=>askAIAboutResults(true);actions.append(ask);}
 box.append(actions);
 if(state.reviews.length>1){const history=node('details',undefined,'scientific-review-history');history.append(node('summary',`查看其他 ${state.reviews.length-1} 份核验记录`));for(const item of state.reviews.filter(item=>item.id!==latest.id)){const row=node('p',`${new Date(item.at).toLocaleString('zh-CN')} · ${item.state==='source_unavailable'?'来源已变化':'科学结论未判定'}`);if(item.state!=='source_unavailable'){const link=node('a','下载 ↓');link.href=`/api/tasks/${current.id}/scientific-reviews/${item.id}/download`;row.append(' ',link);}history.append(row);}box.append(history);}
}
async function refreshScientificReviews(id,results){
 const generation=++scientificReviewGeneration;
 const ids=[...new Set((results?.evaluations||[]).flatMap(group=>group.requests.flatMap(request=>request.reports.filter(report=>report.status==='analyzed').map(report=>report.id))))];
 if(!ids.length&&!results?.evaluations?.length){scientificReviewState={task:id,phase:'idle',reviews:[],error:''};renderScientificReviews();return;}
 const previous=scientificReviewState.task===id?scientificReviewState:null;
 scientificReviewState=previous?.reviews.length?{...previous,phase:'refreshing'}:{task:id,phase:'loading',reviews:[],error:''};
 if(!previous?.reviews.length)renderScientificReviews();
 try{
  const response=await api(`/api/tasks/${id}/scientific-reviews`);
  if(current?.id!==id||generation!==scientificReviewGeneration)return;
  if(!response.configured)throw new Error('结果核验服务尚未配置。');
  const reviews=response.reviews,errors=[];
  for(const analysis_id of ids){
   if(reviews.some(item=>item.analysis_id===analysis_id))continue;
   try{reviews.push(await api(`/api/tasks/${id}/scientific-reviews`,{analysis_id}));}
   catch(error){errors.push(error.message);}
   if(current?.id!==id||generation!==scientificReviewGeneration)return;
  }
  const next={task:id,phase:reviews.length||errors.length?'ready':'idle',reviews,error:errors.length?'有结果尚未通过来源核对：'+errors.join('；'):''};
  const changed=!previous||previous.phase==='idle'||previous.error!==next.error||JSON.stringify(previous.reviews)!==JSON.stringify(next.reviews);
  scientificReviewState=next;
  if(changed)renderScientificReviews();
 }catch(error){if(current?.id!==id||generation!==scientificReviewGeneration)return;scientificReviewState={task:id,phase:'error',reviews:[],error:'结果核验暂不可用：'+error.message};}
 if(scientificReviewState.phase==='error')renderScientificReviews();
}
async function refreshWorkspace(){
 if(!current)return;
 if(typeof refreshPaperWorkflow==='function')void refreshPaperWorkflow();
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
 refreshScientificReviews(id,results);
 referenceProgress=progress;
 workspaceState={task:id,phase:'ready',updated:new Date()};renderRefreshStatus();renderTargetPlanning();
 const r=workspaceReport;
 $('.discussion-panel').hidden=!r&&!normalResult?.evaluations?.some(e=>e.requests.some(q=>q.reports?.length));
 $('#task-tags').replaceChildren();
 if(r){$('#task-status').textContent=r.closeout?.acceptance?.status==='accepted_by_user'?'A/B 计算已结束 · 基准工况已验收':'A 已结束 · B '+bState(r);$('#task-meta').textContent='文献复现验证 · '+r.scope;for(const label of (r.closeout?['作者原始流程','P–A–B 比较已保存',acceptanceLabel(r.closeout.acceptance)]:['作者原始流程','P–A 诊断已保存','科学结论待核验']))$('#task-tags').append(node('span',label,'tag'));}
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
 const tabs=$('#result-tabs');tabs.replaceChildren();
 const hasResult=Boolean(r||normalResult?.evaluations?.some(e=>e.requests?.some(q=>q.reports?.length)));
 if((!hasResult&&!['overview','history'].includes(resultTab))||(resultTab==='targets'&&!r?.closeout))resultTab='overview';
 for(const [key,label] of Object.entries({overview:'结果总览',targets:'论文目标',data:'关键数据',plots:'可视化图表',structure:'原子结构',trajectory:'轨迹动画',report:'分析报告',history:'历史记录'})){
  if((!hasResult&&!['overview','history'].includes(key))||(key==='targets'&&!r?.closeout))continue;
  const b=node('button',label);b.setAttribute('data-result-tab',key);b.setAttribute('role','tab');b.setAttribute('aria-selected',String(key===resultTab));b.onclick=()=>{resultTab=key;for(const x of tabs.children)x.setAttribute('aria-selected',String(x===b));renderWorkspaceResults();if(typeof updateTaskContents==='function')updateTaskContents();};tabs.append(b);
 }
 renderWorkspaceResults();
 renderRawFiles(raw);
 if(typeof renderOutputAnalysisPanel==='function')void renderOutputAnalysisPanel(id);
 if(current?.id!==id)return;
 if(!r&&currentRawFiles().length&&!normalResult?.evaluations?.length){
  $('#task-status').textContent='原始输出已回收';
  $('#task-information').replaceChildren();addInfo('结果状态','原始输出已回收');addInfo('分析报告','尚未生成或未接入');
  addInfo('提交次数','完整提交记录尚未接入');
  for(const f of new Map(currentRawFiles().map(f=>[f.request_id,f])).values())addInfo('作业 '+f.job_id,requestStates[f.state]||f.state);
  $('#execution-flow').replaceChildren(node('h2','计算记录'),node('p','原始输出已回收，可下载查看。完整执行阶段及分析报告尚未接入。','flow-note'));
 }
 renderExecutionControls();renderReferenceProgress();renderWorkspaceResults();
 if(typeof updateTaskContents==='function')updateTaskContents();
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
let paperIntakeRecord=null,paperUpload=null,paperIntakeRead=0;
function paperIntakeError(message){const box=$('#paper-intake-error');box.textContent=message;box.hidden=!message;}
function renderPaperIntake(record){
 paperIntakeRecord=record;paperIntakeError('');
 const form=$('#paper-identity-form'),start=$('#paper-import-start');form.hidden=!record;start.hidden=true;
 const summary=$('#paper-intake-summary');summary.replaceChildren();summary.hidden=!record?.title;
 if(record?.title){summary.append(node('strong',record.title));const link=node('a','DOI '+record.doi);link.href='https://doi.org/'+encodeURI(record.doi);link.target='_blank';link.rel='noopener';summary.append(link);const detail=node('details');detail.append(node('summary','查看已保存的复现需求'),node('p',record.scope));summary.append(detail);}
 if(!record){$('#paper-intake-status').textContent='选择 PDF 后保存；关闭页面也会保留已保存的导入。';return;}
 const fixed=record.state!=='uploaded';
 $('#paper-import-title').value=record.title||record.suggested_title||'';
 $('#paper-import-doi').value=record.doi||(record.suggested_dois||[])[0]||'';
 $('#paper-import-scope').value=record.scope||'先提取全文中的图、表和 LAMMPS 计算条件，再选择本次复现范围。';
 for(const id of ['paper-import-title','paper-import-doi','paper-import-scope'])$('#'+id).disabled=fixed;
 $('#paper-import-confirm').textContent=record.state==='prepared'?'继续完成登记':'核对并登记论文';
 form.hidden=['bound','task_created'].includes(record.state);
 $('#paper-intake-status').textContent=({uploaded:'PDF 已保存 · 待核对论文身份',prepared:'PDF 与论文身份已保存 · 继续完成登记',bound:'论文已登记 · 可以创建复现任务',task_created:'复现任务已保存 · 可以继续提取 P'}[record.state]||'导入状态待核对')+' · '+record.pages+' 页 · '+(record.size_bytes/1048576).toFixed(2)+' MiB';
 if(['bound','task_created'].includes(record.state)){start.hidden=false;start.textContent=record.task_id?'打开复现任务 →':'创建复现任务，提取 P →';}
}
async function refreshPaperIntake(){
 const read=++paperIntakeRead,active=paperIntakeRecord?.id;
 const data=await api('/api/paper-imports');if(read!==paperIntakeRead)return;
 $('#paper-pdf-file').disabled=!data.configured;$('#paper-import-upload').disabled=!data.configured||!paperUpload;
 if(!data.configured){$('#paper-intake-status').textContent=data.reason;$('#paper-identity-form').hidden=true;$('#paper-import-start').hidden=true;return;}
 const history=$('#paper-intake-history');history.replaceChildren();
 for(const saved of data.imports){const row=node('div',undefined,'intake-history-row'),button=node('button',saved.title||saved.suggested_title||'PDF · '+saved.pages+' 页','quiet');button.type='button';button.onclick=()=>{paperIntakeRead++;renderPaperIntake(saved);};row.append(button,node('small',{uploaded:'待核对',prepared:'待完成登记',bound:'已登记',task_created:'已有任务'}[saved.state]||'待核对'));history.append(row);}
 if(!data.imports.length)history.append(node('p','尚无已保存的导入。','subtle'));
 if(active&&paperIntakeRecord?.id===active){const saved=data.imports.find(r=>r.id===active);if(saved)renderPaperIntake(saved);}
 else if(!paperIntakeRecord&&!paperUpload)renderPaperIntake(data.imports[0]||null);
}
function paperIntakeWork(work){
 return action(async()=>{paperIntakeError('');for(const id of ['paper-import-upload','paper-import-confirm','paper-import-start','paper-pdf-file'])$('#'+id).disabled=true;
  try{await work();}catch(error){paperIntakeError(error.message);throw error;}
  finally{for(const id of ['paper-import-confirm','paper-import-start','paper-pdf-file'])$('#'+id).disabled=false;$('#paper-import-upload').disabled=!paperUpload;}
 });
}
$('#paper-pdf-file').onchange=()=>{
 const file=$('#paper-pdf-file').files[0];if(!file)return;
 if(file.size>20*1048576||!file.size){paperIntakeError('请选择不超过 20 MiB 的 PDF。');paperUpload=null;$('#paper-import-upload').disabled=true;return;}
 paperIntakeRead++;paperUpload={id:crypto.randomUUID().replaceAll('-',''),file};renderPaperIntake(null);$('#paper-import-upload').disabled=false;$('#paper-intake-status').textContent='已选择 '+file.name+' · 点击保存 PDF';
};
$('#paper-import-upload').onclick=()=>paperIntakeWork(async()=>{
 if(!paperUpload)return;const pending=paperUpload;
 $('#paper-intake-status').textContent='正在保存并读取 PDF…';
 const response=await fetch('/api/paper-imports/'+pending.id+'/pdf',{method:'POST',headers:{'Content-Type':'application/pdf','X-Task-Review':'1'},body:pending.file});
 const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'PDF 未能保存，请继续同一导入。');
 paperUpload=null;renderPaperIntake(result);await refreshPaperIntake();
});
$('#paper-identity-form').onsubmit=event=>{event.preventDefault();paperIntakeWork(async()=>{
 if(!paperIntakeRecord)return;$('#paper-intake-status').textContent='正在核对题目、DOI 与 PDF 来源…';
 const saved=await api('/api/paper-imports/'+paperIntakeRecord.id+'/confirm',{title:$('#paper-import-title').value,doi:$('#paper-import-doi').value,scope:$('#paper-import-scope').value});
 renderPaperIntake(saved);await refreshPaperIntake();
});};
$('#paper-import-start').onclick=()=>paperIntakeWork(async()=>{
 if(!paperIntakeRecord)return;
 if(paperIntakeRecord.task_id){requestRoute('#'+paperIntakeRecord.task_id);return;}
 const task=await api('/api/paper-imports/'+paperIntakeRecord.id+'/task',{});
 renderPaperIntake({...paperIntakeRecord,state:'task_created',task_id:task.id});
 notice('论文与任务已保存。下一步由应用 AI 提取 P。');requestRoute('#'+task.id);
});
$('#paper-intake-existing').onclick=()=>requestRoute('#papers');
$('#papers-import-pdf').onclick=()=>{showNew();setMode('reproduction');};
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
  const review=event.target.closest('button[data-task-action="review-results"]');
  if(review&&taskQuickLinks.contains(review)){prepareResultReview();return;}
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
const taskContents=$('#task-contents');
function updateTaskContents(){
  if(!current||!taskContents)return;
  const hasReport=Boolean(workspaceReport||normalResult?.evaluations?.some(e=>e.requests?.some(q=>q.reports?.length)));
  const hasRaw=typeof rawResult!=='undefined'&&rawResult?.task===current.id&&Boolean(rawResult.files?.length);
  $('#task-view').classList.toggle('results-first',hasReport);
  const accepted=workspaceReport?.closeout?.acceptance?.status==='accepted_by_user';
  $('#task-view').classList.toggle('accepted-results-first',accepted);
  $('#result-workspace-heading').textContent=current.mode==='reproduction'?'复现结果':'计算结果';
  taskContents.querySelector('.task-contents-primary').firstChild.textContent=(current.mode==='reproduction'?'复现结果':'计算结果')+(hasReport?' ':hasRaw?' · 原始输出 ':' · 尚未生成 ');
  $('#result-priority-note').textContent=accepted?'':hasReport?'以下内容来自已保存的计算与分析记录；科学结论以核验状态为准。':hasRaw?'原始输出已回收，可下载核对；分析结果尚未生成。':'尚无可展示的计算结果；可以查看当前进展与计算记录。';
  for(const button of taskContents.querySelectorAll('button[data-contents-target]')){
    const target=$(button.dataset.contentsTarget),tab=button.dataset.resultTab;
    button.hidden=Boolean((tab&&!['overview','history'].includes(tab)&&!hasReport)||(tab==='targets'&&!workspaceReport?.closeout)||
      target?.closest('[hidden]')||(button.dataset.contentsTarget==='#reference-progress'&&target?.hidden)||
      (button.dataset.contentsTarget==='#execution-flow'&&!target?.children.length)||
      (button.dataset.contentsTarget==='#plan-review-panel'&&target?.hidden));
    if(tab)button.setAttribute('aria-current',tab===resultTab?'location':'false');
  }
}
if(taskContents)taskContents.onclick=event=>{
  const button=event.target.closest('button[data-contents-target]');
  if(!button||!taskContents.contains(button)||button.hidden)return;
  const target=$(button.dataset.contentsTarget);
  if(!target||target.closest('[hidden]'))return;
  if(button.dataset.contentsExpand)$(button.dataset.contentsExpand).open=true;
  if(button.dataset.resultTab){
    resultTab=button.dataset.resultTab;
    for(const tab of $('#result-tabs').children)tab.setAttribute('aria-selected',String(tab.dataset.resultTab===resultTab));
    renderWorkspaceResults();
  }
  updateTaskContents();
  target.scrollIntoView({block:'start',behavior:'smooth'});
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
$('#paper-search').oninput=renderPaperOverview;
for(const [title,text] of [['合金拉伸响应','研究 300 K 下 NbTiZrMoV 合金的单轴拉伸响应。请整理需要确认的初始结构、势函数和加载条件，输出应力–应变曲线与缺陷分析。'],['晶体弹性性质','计算 SiC 晶体的弹性性质。请先确认晶型、温度和势函数，给出弹性常数与分析图表。'],['点缺陷形成能','研究 W 晶体中的空位形成能。请明确参考体系、边界条件及弛豫方案，保留结构与能量来源。']]){const b=node('button',undefined,'example-task');b.append(node('strong',title),node('small',text));b.onclick=()=>{$('#create-form [name=prompt]').value=text;$('#create-form [name=prompt]').focus();};$('#task-examples').append(b);}
const helpItems=[
 ['快速开始','在新建任务中写下材料、工况和研究目标。应用 AI 整理条件后，你可在任务页核对方案并批准计算。结果会保存在同一任务中。','new','新建研究任务','第一次 创建 需求 批准'],
 ['需求应该怎么写？','例如：研究 300 K 下 W 晶体的空位形成能，并给出结构、数值和收敛图。明确的条件直接写；未确定的实施细节交给应用 AI 提出方案。','new','填写研究需求','任务设置 自然语言 输入'],
 ['如何配置应用 AI？','打开模型设置，选择模型商、具体模型 ID，保存自己的 API 密钥。已有连接可以编辑或设为首选。保存后，任务使用该连接调用应用 AI。','model','打开模型设置','DeepSeek 模型 API 密钥 连接'],
 ['如何连接计算平台？','在计算连接中填写平台地址、账户与认证资料，保存后检查连接。每次作业的编号、进度与提交次数保存在任务详情中。','hpc','打开计算连接','HPC SSH 超算 登录'],
 ['在哪里找势函数与来源？','资源库可按元素和势函数类型筛选。展开“查看依据”，查看配套文件、版本、论文和许可；有缺项或冲突的资源会保留提示。','resources','打开资源库','材料 资源 势函数'],
 ['如何看图和下载数据？','打开任务的结果区，切换图表、关键数据或报告；文件下载区提供已保存文件。点击图像可查看原尺寸，继续分析区可向应用 AI 提问。','tasks','选择已有任务','结果 可视化 图表 下载 CSV 报告 轨迹'],
 ['上一步失败后怎么继续？','先查看任务的当前进展和失败记录。页面会给出可用的恢复操作；提交状态不明时先核对原作业，保留原请求和费用。','tasks','查看任务状态','失败 错误 恢复 重试 断点 暂停'],
 ['论文 P、作者 A、独立 B 是什么？','P 是论文图表与报告值；A 来自作者原代码；B 由应用 AI 根据允许条件独立计算。案例中心保存各自进度与已接受的复现范围。','papers','打开案例中心','文献 复现 验证 历史 PAB'],
];
function helpAction(key){if(key==='model')return action(openModel);if(key==='hpc')return action(openHPC);requestRoute({new:'#new',tasks:'#tasks',resources:'#resources',papers:'#papers'}[key]||'#help');}
function renderHelp(){const query=$('#help-search').value.trim().toLowerCase(),box=$('#help-articles');box.replaceChildren();for(const [title,body,key,label] of helpItems.filter(r=>r.join(' ').toLowerCase().includes(query))){const d=node('details',undefined,'panel help-article');d.open=Boolean(query)||title==='快速开始';d.append(node('summary',title),node('p',body));const go=node('button',label,'text-button');go.type='button';go.onclick=()=>helpAction(key);d.append(go);box.append(d);}if(!box.children.length)box.append(emptyState('未找到相关说明','试试“模型”“下载”或“计算”等关键词。'));}
$('#help-search').oninput=renderHelp;
$('#help-view').onclick=event=>{const button=event.target.closest('[data-help-action]');if(button)helpAction(button.dataset.helpAction);};
$('#new-task-help').onclick=()=>{$('#help-search').value='输入';requestRoute('#help');};
$('#tasks-query').oninput=()=>{$('#task-search').value=$('#tasks-query').value;taskCards();};
$('#tasks-sort').onchange=taskCards;
function renderPlotGallery(report,box){
 const options={full:'完整拉伸曲线',virial:'固定体积位力应力',pressure:'瞬时压力口径',elastic05:'弹性区间 0–0.05',elastic06:'弹性区间 0–0.06'};
 const controls=node('div',undefined,'chart-toolbar'),label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择图表');for(const [key,value] of Object.entries(options))select.append(new Option(value,key));select.value=selectedPlot;label.append(select);controls.append(label);box.append(controls);const area=node('div',undefined,'chart-area');box.append(area);
 const draw=()=>{selectedPlot=select.value;area.replaceChildren();let curves=report.curves,xmax=.5;if(selectedPlot==='virial')curves=curves.slice(0,1);if(selectedPlot==='pressure')curves=curves.slice(1);if(selectedPlot.startsWith('elastic')){xmax=selectedPlot==='elastic05'?.05:.06;curves=curves.slice(0,1);}area.append(node('h3',options[selectedPlot]),curvePlot({...report,curves,xmax}));const legend=node('div',undefined,'plot-legend');for(const c of curves)legend.append(node('span',c.label));area.append(legend,node('p','同一真实基准工况的不同视图，不代表复现了多个工况或整篇论文。','plot-caption'));};select.onchange=draw;draw();
}
function renderAICharts(items,taskId,{box=$('#discussion-charts'),channel='charts',title='AI 绘图请求与结果'}={}){
 box.replaceChildren();if(!items.length)return;
 box.append(node('h3',title));
 for(const item of items){
  const row=node('article',undefined,'result-widget');row.append(node('h4',item.question));
  const chart=item.chart;
  if(item.state!=='completed'||!chart){row.append(node('p',item.state==='source_unavailable'?'来源数据暂未通过核验，图与下载已隐藏。':chart?.message||'这次选图未完成；请求记录已保留，没有自动重发。','form-note'));box.append(row);continue;}
  const table={columns:[{name:chart.x,unit:chart.x_unit},{name:chart.y,unit:chart.y_unit}],rows:chart.preview};
  const xs=chart.preview.map(values=>values[0]);
  const svg=numericalPlot(table,{method:'raw_preview',x:chart.x,y:chart.y,window:[Math.min(...xs),Math.max(...xs)],values:{}});
  const metadata=document.createElementNS('http://www.w3.org/2000/svg','metadata');
  metadata.textContent=JSON.stringify({task_id:taskId,role:chart.role||'agent',analysis_id:chart.analysis_id,source_file:chart.file,
    source_sha256:chart.source_sha256,data_sha256:chart.csv_sha256,x:chart.x,y:chart.y,
    total_rows:chart.rows,plotted_rows:chart.preview.length,sampled:chart.sampled,
    filters:chart.filters||[],source_total_rows:chart.source_total_rows||chart.rows,
    output_status:chart.output_status||null,scientific_status:chart.scientific_status||null});svg.append(metadata);
  const csv=node('a','下载完整数据 CSV','quiet');csv.href=`/api/tasks/${encodeURIComponent(taskId)}/${channel}/${encodeURIComponent(item.id)}/download`;
  const svgLink=node('a','下载图表 SVG','quiet');svgLink.download='auto-lammps-ai-chart.svg';svgLink.href='#';
  svgLink.onclick=()=>{svgLink.href='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(svg));};
  const filterNote=chart.filters?.length?`筛选 ${chart.filters.map(item=>`${item.column} = ${item.value}`).join(' 且 ')}；原表 ${chart.source_total_rows} 行，匹配 ${chart.rows} 行。`:'';
  const rowsNote=chart.missing_pair_count?`保留 ${chart.missing_pair_count} 行缺失配对，不补值。`:'';
  row.append(svg,csv,svgLink,node('p',`${chart.file} · 来源 SHA-256：${chart.source_sha256} · ${filterNote}${chart.rows} 行完整数据；图中${chart.sampled?'等间隔预览 '+chart.preview.length+' 行':'显示全部有效配对'}。${rowsNote}横轴 ${chart.x} (${chart.x_unit})，纵轴 ${chart.y} (${chart.y_unit})。${channel==='reference-charts'?(chart.output_valid===true?'作者 A 已有数值；':'作者 A 部分数值；'):'仅重绘已有结果，'}科学结论尚待核验。`,'plot-caption'));
  box.append(row);
 }
}
async function refreshDiscussion(){
 if(!current)return;const id=current.id,generation=refreshDiscussion.generation=(refreshDiscussion.generation||0)+1;
 const result=await api(`/api/tasks/${id}/discussion`);if(current?.id!==id||generation!==refreshDiscussion.generation||$('#task-view').hidden)return;
 const box=$('#discussion-history');box.replaceChildren();
 for(const m of result.messages){const row=node('article',undefined,'discussion-message');row.append(researchContent(m.question,'discussion-question'),researchContent(m.answer||'请求状态待核对，未重复发送。'),node('small',m.provider+' / '+m.model+' · '+new Date(m.at).toLocaleString('zh-CN')));box.append(row);}
 $('#discussion-status').textContent=result.enabled?'可围绕已有数据提问；问题、答复与模型用量会保留。需要绘图时点“让 AI 生成数据图”。':'请先在“设置 → 模型 API”中保存当前所选模型的密钥和模型 ID；保存后即可在这里提问。';
 if(result.enabled&&result.provider!=='deepseek-official')$('#discussion-status').textContent+=' 自动选图使用 DeepSeek；请在模型设置中切换到已连接的 DeepSeek。';
 const discussionSubmit=$('#discussion-form button[type=submit]');if(discussionSubmit)discussionSubmit.disabled=!result.enabled;
 const chartButton=$('#discussion-ai-chart');chartButton.disabled=!result.enabled||result.provider!=='deepseek-official';
 try{const charts=await api(`/api/tasks/${id}/charts`);if(current?.id===id&&generation===refreshDiscussion.generation&&!$('#task-view').hidden)renderAICharts(charts.charts||[],id);}
 catch{if(current?.id===id&&generation===refreshDiscussion.generation&&!$('#task-view').hidden)$('#discussion-charts').replaceChildren(node('p','已保存的数据图暂不可读取，请稍后刷新。','form-note'));}
}
$('#discussion-open-plots').onclick=()=>{
 if(!current)return;
 resultTab='plots';
 for(const tab of $('#result-tabs').children)tab.setAttribute('aria-selected',String(tab.textContent==='可视化图表'));
 renderWorkspaceResults();$('#research-results').scrollIntoView({block:'start'});
};
$('#discussion-review-results').onclick=prepareResultReview;
$('#discussion-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(!current)return;const question=$('#discussion-prompt').value.trim();const pref=await api('/api/model-preference');if(!discussionRequest||discussionRequest.question!==question||discussionRequest.task!==current.id)discussionRequest={id:crypto.randomUUID().replaceAll('-',''),question,task:current.id};$('#discussion-status').textContent='正在分析已有结果…';try{const reply=await api(`/api/tasks/${current.id}/discussion`,{request_id:discussionRequest.id,provider:pref.provider,question});if(reply.state==='completed'){$('#discussion-prompt').value='';discussionRequest=null;}await refreshDiscussion();}catch(error){$('#discussion-status').textContent=error.message;throw error;}});};
let chartRequest=null;
$('#discussion-ai-chart').onclick=()=>action(async()=>{
 if(!current)return;const id=current.id,question=$('#discussion-prompt').value.trim();
 if(!question||question.length>1000){$('#discussion-status').textContent='请用 1000 字以内描述要绘制的图。';return;}
 if(!chartRequest||chartRequest.task!==id||chartRequest.question!==question)chartRequest={id:crypto.randomUUID().replaceAll('-',''),task:id,question};
 $('#discussion-status').textContent='应用内 AI 正在从已核验数据选择图表…';
 try{const result=await api(`/api/tasks/${id}/charts`,{request_id:chartRequest.id,provider:'deepseek-official',question});
  if(current?.id!==id)return;
  if(result.state==='completed'){$('#discussion-prompt').value='';chartRequest=null;}
  await refreshDiscussion();
  $('#discussion-status').textContent=result.state==='completed'?'数据图已生成，完整 CSV 和 SVG 可在上方下载。':result.chart?.message||'这次选图未完成；请求记录已保留。';
 }catch(error){if(current?.id===id)$('#discussion-status').textContent=error.message;throw error;}
});
$('#reference-ai-refresh').onclick=()=>action(refreshReferenceHistory);
$('#reference-ai-submit').onclick=()=>action(async()=>{
 const source=referenceChartSource,id=current?.id,question=$('#reference-ai-question').value.trim();
 if(!source||source.task!==id||$('#reference-ai-charts').hidden)return;
 if(!question||question.length>1000){$('#reference-ai-status').textContent='请用 1000 字以内描述想看的作者 A 数据图。';return;}
 if(!referenceChartRequest||referenceChartRequest.task!==id||referenceChartRequest.sha!==source.sha||referenceChartRequest.manifest!==source.manifest||referenceChartRequest.question!==question)
   referenceChartRequest={id:crypto.randomUUID().replaceAll('-',''),task:id,sha:source.sha,manifest:source.manifest,question};
 const request=referenceChartRequest;
 $('#reference-ai-submit').disabled=true;$('#reference-ai-status').textContent='应用内 AI 正在从作者 A 已核验的数值表选图…';
 try{
   const result=await api(`/api/tasks/${id}/reference-charts`,{request_id:request.id,provider:'deepseek-official',question,source_sha256:source.sha});
   if(current?.id!==id||referenceChartSource?.sha!==source.sha||referenceChartSource?.manifest!==source.manifest)return;
   if(result.state==='completed'){$('#reference-ai-question').value='';referenceChartRequest=null;}
   await refreshReferenceHistory();
   $('#reference-ai-status').textContent=result.state==='completed'?'作者 A 数据图已保存，可下载完整 CSV 和 SVG；科学状态仍待核验。':
     '这次选图未完成，记录已保留。同一问题不会自动再次发送；可修改问题后新建请求。';
 }catch(error){
   if(current?.id===id&&referenceChartSource?.sha===source.sha&&referenceChartSource?.manifest===source.manifest)$('#reference-ai-status').textContent='请求结果待核对；请刷新记录，应用不会自动再次发送。';
 }finally{if(current?.id===id&&referenceChartSource?.sha===source.sha&&referenceChartSource?.manifest===source.manifest)$('#reference-ai-submit').disabled=false;}
});
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

// These human reference views are never part of the candidate or B discussion.
function paperWorkflowState(){
 return paperWorkflowState.value||(paperWorkflowState.value={task_id:null,workflow:null,report:null,discussion:null,request:null,draft:'',identity:null,error:null});
}
function clearPaperWorkflowView(id){
 refreshPaperWorkflow.generation=(refreshPaperWorkflow.generation||0)+1;
 refreshAuthorDiscussion.generation=(refreshAuthorDiscussion.generation||0)+1;
 renderAuthorEvidence.generation=(renderAuthorEvidence.generation||0)+1;
 paperWorkflowState.value={task_id:id,workflow:null,report:null,discussion:null,request:null,draft:'',identity:null,error:null};
 for(const selector of ['#paper-workflow','#author-reference']){const box=$(selector);if(box){box.replaceChildren();box.hidden=true;}}
}
function paperWorkflowPanels(){
 const anchor=$('#target-planning'),main=anchor?.parentNode;if(!main)return null;
 let workflow=$('#paper-workflow'),reference=$('#author-reference');
 if(!workflow){workflow=node('section',undefined,'panel paper-workflow');workflow.id='paper-workflow';workflow.setAttribute('aria-label','论文复现流程');main.insertBefore(workflow,$('.ai-current')||anchor);}
 if(!reference){reference=node('section',undefined,'panel author-reference paper-workbench');reference.id='author-reference';reference.setAttribute('aria-label','作者参考 A 的独立结果与分析');main.insertBefore(reference,anchor.nextSibling);}
 return {workflow,reference};
}
function authorEvidenceIdentity(report){
 return report?.task_id===current?.id&&report.role==='reference'&&/^[a-f0-9]{64}$/.test(report.source_sha256||'')&&/^[a-f0-9]{64}$/.test(report.manifest_sha256||'')?
  report.task_id+':'+report.source_sha256+':'+report.manifest_sha256:null;
}
function paperWorkflowCurrent(id,generation){return current?.id===id&&!$('#task-view').hidden&&generation===refreshPaperWorkflow.generation;}
async function refreshPaperWorkflow(){
 if(!current||$('#task-view').hidden)return;
 const id=current.id,generation=refreshPaperWorkflow.generation=(refreshPaperWorkflow.generation||0)+1;
 const replies=await Promise.allSettled([api(`/api/tasks/${id}/paper-workflow`),api(`/api/tasks/${id}/reference-evidence`)]);
 if(!paperWorkflowCurrent(id,generation))return;
 const state=paperWorkflowState();if(state.task_id!==id)clearPaperWorkflowView(id);
 const active=paperWorkflowState();active.task_id=id;
 if(replies[0].status==='fulfilled'&&replies[0].value.task_id===id)active.workflow=replies[0].value;
 if(replies[1].status==='fulfilled'){
  const report=replies[1].value.report||null,identity=authorEvidenceIdentity(report);
  if(identity!==active.identity){active.discussion=null;active.request=null;active.draft='';}
  active.report=identity?report:null;active.identity=identity;
 }
 active.error=replies.some(reply=>reply.status==='rejected')?'参考视图暂未刷新，保留上次已读取内容；这不表示计算失败。':null;
 renderPaperWorkflow();
 if(active.report)await refreshAuthorDiscussion(id,active.identity);
}
function renderPaperWorkflow(){
 const panels=paperWorkflowPanels();if(!panels||!current)return;
 const state=paperWorkflowState(),status=state.task_id===current.id?state.workflow:null;
 const workflowActive=status?status.applicable===true:current.mode==='reproduction';
 const associated=workflowActive||Boolean(current.paper_evidence||current.paper_evidence_error)||Boolean(authorEvidenceIdentity(state.report));
 panels.workflow.hidden=!associated;panels.reference.hidden=!associated;
 if(!associated){panels.workflow.replaceChildren();panels.reference.replaceChildren();return;}
 const box=panels.workflow;box.replaceChildren(node('h2',workflowActive?'论文复现 · P–A–B':'论文与作者参考 · 只读对照'));
 const definitions=node('div',undefined,'paper-role-definitions');
 for(const [letter,title,note] of [['P','论文原文','原论文的图、表与报告值'],['A','作者原代码','作者原流程实际运行所得'],['B','应用 AI 独立计算','仅从允许条件独立生成并计算']]){const item=node('article');item.append(node('strong',letter+' · '+title),node('p',note));definitions.append(item);}box.append(definitions);
 appendWorkbenchEntry(box);
 if(!workflowActive){
  box.append(node('p','此普通科研任务保留已有 P/A 人类对照；无需补做论文资源、目标选择或冻结。原计算与验收历史保留。','form-note'));
  if(state.error)box.append(node('p',state.error,'workspace-freshness error'));
  const refresh=node('button','刷新参考记录','quiet');refresh.onclick=()=>action(refreshPaperWorkflow);box.append(refresh);
  renderAuthorEvidence(panels.reference,state.report);return;
 }
 const stages=[['resources','资源检查'],['paper','P 提取与优先级目标'],['reference','作者 A'],['independent','无答案 B'],['comparison','结果对比']];
 const phases={resource_check:0,resource_review:0,evidence_ready:1,target_selection:1,reference_active:2,reference_results:2,independent_preparation:3,comparison:4};
 const list=node('ol',undefined,'paper-workflow-stages');list.setAttribute('aria-label','复现阶段');
 for(const [index,[key,label]] of stages.entries()){const row=node('li',undefined,index===phases[status?.state]?'current':'');row.dataset.stage=key;row.append(node('span',String(index+1)),node('strong',label));if(index===phases[status?.state])row.setAttribute('aria-current','step');list.append(row);}box.append(list);
 if(!status){box.append(node('p','正在读取复现流程；尚未核对的步骤不显示完成。','form-note'));}
 else{
  if(status.paper?.title)box.append(node('h3',status.paper.title));
  if(status.paper?.doi){const doi=node('a','DOI '+status.paper.doi);doi.href='https://doi.org/'+status.paper.doi;doi.target='_blank';doi.rel='noopener noreferrer';box.append(doi);}
  const resources=status.resources||{};
  box.append(node('p',resources.source_ready?'作者来源与资源登记已匹配；科学适用性仍以核验记录为准。':resources.catalog_checked?'已核对登记资源，仍有资源或作者流程缺项。':'尚未完成资源匹配。'));
  const missing=node('ul',undefined,'workflow-gaps');for(const gap of resources.missing||[])missing.append(node('li',typeof gap==='string'?gap:gap.message||gap.label));if(missing.children.length)box.append(missing);
  if(resources.bundles?.length){const bundles=node('details');bundles.append(node('summary','查看已匹配的作者来源与配套资源'));for(const bundle of resources.bundles){bundles.append(node('h4',bundle.repository||bundle.id),node('p',(bundle.kinds||[]).join('、')+' · '+(bundle.entries||[]).length+' 条登记资源'),node('p','版本 '+String(bundle.commit||'待核对').slice(0,12)+' · '+(bundle.association_verified?'论文对应已核对':'论文对应待核对'),'form-note'));}box.append(bundles);}
  if(resources.github?.search_exhaustive===false)box.append(node('p','公开来源检索只列已有发现，不能证明所有作者资源均已找到。','form-note'));
  const priorities=status.evidence?.priorities||[];
  if(priorities.length){const details=node('details');details.append(node('summary','查看论文目标优先级'));for(const item of priorities)details.append(node('p',item.label+' · 优先级 '+item.priority));box.append(details);}
  if(status.title)box.append(node('p','当前阶段：'+status.title,'form-note'));
  if(status.notice)box.append(node('p',status.notice,'form-note'));
  for(const error of status.errors||[])box.append(node('p',error.message,'form-note'));
  const actions=node('div',undefined,'paper-workflow-actions');for(const item of status.actions||[])actions.append(paperWorkflowAction(item,status));box.append(actions);
  if(status.b_draft?.disabled_reason)box.append(node('p','B：'+status.b_draft.disabled_reason,'form-note'));
 }
 if(state.error)box.append(node('p',state.error,'workspace-freshness error'));
 const refresh=node('button','刷新复现记录','quiet');refresh.onclick=()=>action(refreshPaperWorkflow);box.append(refresh);
 renderAuthorEvidence(panels.reference,state.report);
}
function appendWorkbenchEntry(box){
 const id=current.id,state=paperWorkflowState();
 if(state.workbenchEntry?.taskId===id){box.append(state.workbenchEntry);return;}
 const entry=node('details',undefined,'literature-workbench');entry.id='workbench-entry';
 entry.taskId=id;state.workbenchEntry=entry;
 entry.append(node('summary','P · 从论文全文提取数据、原图与表格（应用 AI）'));
 const content=node('div');entry.append(content);box.append(entry);
 const active=()=>current?.id===id&&!$('#task-view').hidden&&paperWorkflowState()===state&&entry.isConnected;
 let pending=false,pendingRequest=null,latestRecord=null,loadGeneration=0,sourceChanged=false;
 const draw=record=>{
  if(!active())return;
  if(latestRecord&&latestRecord.source_sha256!==record.source_sha256)sourceChanged=true;
  latestRecord=record;content.replaceChildren();
  content.append(node('p','复用文献工作台的提取、自动核验与保存；使用本应用已配置的模型。论文结果仅供人类参考，不进入独立 B。','form-note'));
  const completedContinuations=(record.completed_unknown_reissues||[]).filter(item=>
   item.task_id===id&&item.source_sha256===record.source_sha256&&/^[a-f0-9]{64}$/.test(item.source_sha256||'')&&
   /^[a-f0-9]{32}$/.test(item.unknown_request_id||'')&&/^[a-f0-9]{32}$/.test(item.superseded_by||'')&&item.unknown_request_id!==item.superseded_by&&
   item.data_continuation_completed===true&&item.unknown_charge_unsettled===true&&item.repair_visuals_only===true&&item.automatic_retry===false&&
   (record.messages||[]).some(message=>message.request_id===item.superseded_by&&message.source_sha256===record.source_sha256&&['completed','completed_with_limitations'].includes(message.state)));
  const continuedUnknowns=new Set(completedContinuations.map(item=>item.unknown_request_id));
  if(continuedUnknowns.size)content.append(node('p','已批准的断点续接已保存同源 P；旧未知调用及未结费用仍保留。现在仅可单独修复图表，不重新扫描全文。','form-note'));
  if(sourceChanged)content.append(node('p','论文全文来源版本已变化；原请求与费用保留，未自动重新提取。','form-note'));
  for(const message of record.messages||[]){
   const state={completed:'已提取并保存',completed_with_limitations:'已保存，仍有明确缺项',no_evidence_published:'未得到可发布证据',failed:'提取未完成',failed_or_unknown:'请求未完成或状态待核对',unknown:'正在提取或等待回执'}[message.state]||message.state;
   content.append(node('p',state+' · '+(message.at||'')+' · 请求 '+message.request_id));
   if(message.error)content.append(node('p',message.error,'workspace-freshness error'));
   const counts=message.counts;if(counts)content.append(node('p','新增数据 '+counts.published_item_count+' · 已有数据 '+counts.existing_item_count+' · 图 '+counts.figure_candidate_count+' · 表 '+counts.table_candidate_count));
  }
  for(const job of record.jobs||[]){
   if(job.legacy)continue;
   content.append(node('p',(job.label||job.state)+' · 请求 '+job.request_id,'form-note'));
  }
  if(record.disabled_reason)content.append(node('p',record.disabled_reason,'form-note'));
  const terminal=['completed','completed_with_limitations','no_evidence_published','failed'];
  if(!pending&&pendingRequest&&(record.messages||[]).some(message=>message.request_id===pendingRequest.request_id&&terminal.includes(message.state)))pendingRequest=null;
  const activeJobs=(record.jobs||[]).filter(job=>['queued','dispatching'].includes(job.state));
  const uncertain=Boolean(pendingRequest&&!pending)||(record.messages||[]).some(message=>!continuedUnknowns.has(message.request_id)&&['unknown','failed_or_unknown'].includes(message.state))||(record.jobs||[]).some(job=>['queued','dispatching'].includes(job.state)||!continuedUnknowns.has(job.request_id)&&['interrupted','unknown','failed_or_unknown'].includes(job.state));
  const saved=(record.messages||[]).some(message=>['completed','completed_with_limitations','no_evidence_published'].includes(message.state)||message.model_request_ids?.length>0);
  const runButton=(label,options={},style='quiet',endpoint='extract',confirmed=()=>true)=>{
   const run=node('button',label,style);run.disabled=pending;content.append(run);
   run.onclick=()=>action(async()=>{
    if(!active()||pending||pendingRequest||latestRecord!==record||!confirmed())return;
    const request={request_id:crypto.randomUUID().replaceAll('-',''),source_sha256:record.source_sha256};
    pending=true;run.disabled=true;pendingRequest={...request,...options};draw(record);
    try{const reply=await api(`/api/tasks/${id}/workbench/${endpoint}`,pendingRequest);if(terminal.includes(reply.state)||['queued','dispatching'].includes(reply.state))pendingRequest=null;}
    finally{pending=false;if(active())await entry.loadWorkbench();}
   });
   return run;
  };
  if(record.enabled&&!uncertain){
   const resumable=continuedUnknowns.size?[]:(record.recoveries||[]).filter(item=>item.enabled===true&&/^[a-f0-9]{32}$/.test(item.resume_of));
   if(resumable.length){
    for(const recovery of resumable){
     content.append(node('p',`可复用原请求已成功的 ${recovery.reused_model_calls} 次答复；后续尚未完成的核验会调用模型并记账。原失败与费用保留。`,'form-note'));
     if(recovery.reason)content.append(node('p',recovery.reason,'form-note'));
     runButton('从已保存的阶段继续核验',{resume_of:recovery.resume_of},'primary');
    }
   }
   else if(!saved&&!continuedUnknowns.size)runButton('让应用 AI 提取并核验论文',{},'primary');
   else{
    content.append(node('p','重新扫描或修复图表会新增模型调用并记账；已有证据、请求和费用保留。','form-note'));
    if(!continuedUnknowns.size)runButton('重新扫描全文（新增模型调用）',{force_rescan:true});
    if((record.messages||[]).some(message=>['completed_with_limitations','no_evidence_published'].includes(message.state)||message.visual_evidence_ready===false))runButton('修复图表提取（新增模型调用）',{repair_visuals:true});
   }
  }
  if(record.enabled&&!sourceChanged&&!pending&&!pendingRequest&&!activeJobs.length){
   for(const option of record.unknown_reissues||[]){
    if(option.enabled!==true||!/^[a-f0-9]{32}$/.test(option.unknown_reissue_of)||!Number.isInteger(option.reused_model_calls)||option.reused_model_calls<1)continue;
    content.append(node('p',`核对供应商后，可仅重发这处断点并复用 ${option.reused_model_calls} 次成功答复。旧未知请求及费用永久保留；不重扫全文，不提交 HPC，每个旧未知请求仅允许一次显式恢复。`,'form-note'));
    if(option.reason)content.append(node('p',option.reason,'form-note'));
    const label=node('label'),check=node('input');check.type='checkbox';
    const confirmation='我已核对供应商记录，批准仅重发此断点，并保留旧未知调用及费用';
    check.setAttribute('aria-label',confirmation);label.append(check,node('span',confirmation));content.append(label);
    const run=runButton('仅重发已核对的断点（保留成功答复）',{
     unknown_reissue_of:option.unknown_reissue_of,supplier_checked:true,retain_unknown_charge:true
    },'primary','reissue-unknown',()=>check.checked===true);
    run.disabled=true;check.onchange=()=>{run.disabled=pending||check.checked!==true;};
   }
  }
  if(pending){const progress=node('p',pendingRequest?.resume_of||pendingRequest?.unknown_reissue_of?'应用 AI 正从保存阶段继续核验，复用已保存的成功答复，新增调用仍会记账。可以关闭浏览器后再查看记录；不会提交 HPC。':'应用 AI 正在提取并自动核验全文。可以关闭浏览器后再查看记录；不会提交 HPC。','form-note');progress.setAttribute('role','status');content.append(progress);}
  if(activeJobs.length)content.append(node('p','文献工作台正在后台提取；可以离开本页或关闭浏览器，稍后查看已有请求的进度。','form-note'));
  else if(uncertain)content.append(node('p','已有请求尚未得到终态；先核对原回执，不能换编号自动重提。','form-note'));
  const refresh=node('button','查看提取进度与已保存数据','quiet');refresh.onclick=()=>action(entry.loadWorkbench);content.append(refresh);
  const evidence=record.evidence;if(!evidence)return;
  content.append(node('h3',evidence.title),node('p','DOI '+evidence.doi+' · 论文原文结果 P'));
  appendWorkbenchTargetInventory(content,id,active);
  if(evidence.items?.length){
   const table=node('table',undefined,'reference-data-table');
   const head=node('tr');for(const text of ['数值','单位','含义','来源'])head.append(node('th',text));table.append(head);
   for(const item of evidence.items){const row=node('tr');
    for(const value of [item.value_text,item.unit,item.meaning,'第 '+(item.source_page||'?')+' 页 · '+(item.source_locator||'')])row.append(node('td',value||'—'));
    const download=node('a','下载 CSV');download.href=`/api/tasks/${id}/workbench/export/item/${item.entity_uid}?source_sha256=${record.source_sha256}`;
    const cell=node('td');cell.append(download);row.append(cell);table.append(row);
   }content.append(table);
  }
  const figures=node('div',undefined,'evidence-grid');
  for(const visual of evidence.visuals||[]){
   const figure=node('figure',undefined,'evidence-figure paper');figure.append(node('h4',visual.label||visual.display_name||'论文原图'));
   if(visual.image_sha256){const image=node('img');image.src=`/api/tasks/${id}/workbench/images/${visual.entity_uid}?source_sha256=${record.source_sha256}`;image.alt=visual.caption||visual.label||'论文原图';image.loading='lazy';figure.append(image);}
   figure.append(node('figcaption',visual.caption||'原文图注未提取'));
   const download=node('a','下载原文提取数据');download.href=`/api/tasks/${id}/workbench/export/${visual.asset_type}/${visual.entity_uid}?source_sha256=${record.source_sha256}`;figure.append(download);figures.append(figure);
  }content.append(figures);
 };
 entry.loadWorkbench=async()=>{if(!active())return;const generation=++loadGeneration,record=await api(`/api/tasks/${id}/workbench`);if(active()&&generation===loadGeneration&&record.task_id===id){draw(record);if(entry.poll)clearTimeout(entry.poll);if((record.jobs||[]).some(job=>['queued','dispatching'].includes(job.state)))entry.poll=setTimeout(()=>{if(active()&&!pending)entry.loadWorkbench().catch(()=>{if(active())content.append(node('p','进度暂未刷新；后台提取和已有记录保留，请稍后刷新。','form-note'));});},6000);}};
 const load=node('button','打开文献提取与已有证据','quiet');load.onclick=()=>action(entry.loadWorkbench);content.append(load);
}
function appendWorkbenchTargetInventory(container,id,parentActive){
 const panel=node('details',undefined,'reference-target-analysis');container.append(panel);
 panel.append(node('summary','整理 P 的图表目标、优先级与共享计算依赖'));
 const area=node('div');panel.append(area);
 const alive=()=>current?.id===id&&parentActive()&&panel.isConnected;
 let pending=false,readGeneration=0;
 async function refresh(){
  const generation=++readGeneration;
  const data=await api(`/api/tasks/${id}/workbench/targets`);
  if(!alive()||generation!==readGeneration||data.task_id!==id)return;
  area.replaceChildren(node('p','应用 AI 根据已发布 P 和完整原文整理目标；不会替你选择范围、冻结任务或提交计算。','form-note'));
  if(data.disabled_reason)area.append(node('p',data.disabled_reason,'form-note'));
  const saved=data.source,report=saved?.state==='completed'?saved.report:null;
  if(report){
   area.append(node('h4','完整目标清单 · AI 建议，尚未选定本次范围'),node('p',report.inventory.coverage_note));
   const table=node('table',undefined,'reference-data-table'),head=node('tr');
   for(const label of ['优先级','原文目标','工况','所需数据与可用性'])head.append(node('th',label));table.append(head);
   for(const target of report.inventory.targets){const row=node('tr');
    for(const value of [report.priorities[target.id],target.label,target.conditions,target.outputs+' · '+target.availability])row.append(node('td',String(value??'—')));table.append(row);
   }area.append(table);
   for(const note of report.limitations||[])area.append(node('p',note,'form-note'));
   const use=node('button','使用这份 P 目标清单，继续选择范围','quiet');area.append(use);
   use.onclick=()=>action(async()=>{if(!alive()||use.disabled)return;use.disabled=true;
    const updated=await api(`/api/tasks/${id}/workbench/targets/${saved.request_id}/import`,{revision:current.revision});
    if(!alive())return;current=updated;await afterChange('P 目标清单已读取；请核对范围与条件，尚未提交计算。');
   });
  }else if(saved?.state==='stale')area.append(node('p','原 P 已变化；旧库存与费用仍保留，不能混用新旧来源。','error'));
  await renderReferenceTargetAnalysis(area,{task_id:id},id+':'+(report?.input_sha256||'retained-history'),{historyOnly:!report});
  if(!alive())return;
  const history=node('details');history.append(node('summary',`查看 ${data.history.length} 次目标整理、失败原因与模型用量`));
  for(const item of data.history){const row=node('article');
   row.append(node('p','请求 '+item.request_id+' · '+item.state));
   if(item.receipt?.message)row.append(node('p',item.receipt.message));
   if(Number.isInteger(item.usage?.total_tokens))row.append(node('small','已记录模型用量 '+item.usage.total_tokens));history.append(row);
  }area.append(history);
  const recovery=data.status.development_recovery||{},refining=recovery.authorized===true;
  const run=node('button',refining?'让应用 AI 细分子图与表列（开发恢复）':'让应用 AI 整理完整 P 为图表目标','primary');
  run.disabled=pending||(Boolean(report)&&(!refining||saved.request_id!==recovery.parent_inventory_request))||!data.published_P_ready||data.status.blocked||data.status.remaining_rounds===0||
    (refining&&recovery.remaining_requests===0);
  if(refining)area.append(node('p',`同任务开发恢复已使用 ${recovery.used_requests}/${recovery.maximum_additional_requests} 次；目标细分与依赖核定共用额度，原记录及费用保留。`,'form-note'));
  const note=node('p',`已使用 ${data.status.used_rounds}/${data.status.maximum_rounds} 轮；保留完整前稿、错误和费用。`,'form-note');
  run.onclick=async()=>{if(!alive()||run.disabled)return;pending=true;run.disabled=true;note.textContent='应用 AI 正在整理完整 P；可以离开页面，返回后查看同一请求记录。';
   try{await api(`/api/tasks/${id}/workbench/targets`,{request_id:crypto.randomUUID().replaceAll('-','')});}
   catch(error){if(alive())note.textContent='原目标整理请求需要核对：'+error.message;}
   finally{pending=false;if(alive())await refresh();}
  };
  const reload=node('button','刷新图表目标整理记录','quiet');reload.onclick=()=>refresh().catch(error=>{if(alive())notice(error.message,true);});
  area.append(run,reload,note);
 }
 const load=node('button','查看 P 目标整理与已有记录','quiet');load.onclick=()=>refresh().catch(error=>{if(alive())notice(error.message,true);});area.append(load);
}
function paperWorkflowAction(item,status){
 const row=node('div',undefined,'workflow-action'),id=current.id,generation=refreshPaperWorkflow.generation;
 const expected={check_resources:['POST','paper-workflow/resources'],search_github:['POST','paper-workflow/search'],view_P:['GET','paper-evidence'],extract_P:['GET','workbench'],import_targets:['POST','paper-evidence/import-targets'],select_targets:['POST','targets'],freeze_targets:['POST','freeze'],view_A:['GET','reference-result'],prepare_A_plan:['GET','author-plan'],approve_A:['GET','author-execution'],analyze_A:['GET','output-analyses'],preview_B_draft:['GET','paper-workflow/b-draft'],compare:['GET','reference-result']}[item.id];
 const enabled=item.enabled===true&&expected&&item.method===expected[0]&&item.endpoint===`/api/tasks/${id}/${expected[1]}`;
 row.append(node('strong',item.label));
 if(!enabled){row.append(node('p',item.disabled_reason||'此步骤尚未接入完整的安全执行服务。','form-note'));return row;}
 const button=node('button',item.label,'quiet');button.onclick=()=>action(async()=>{
  if(!paperWorkflowCurrent(id,generation))return;
  if(item.id==='extract_P'){const entry=$('#workbench-entry');if(entry){entry.open=true;entry.scrollIntoView({block:'start'});await entry.loadWorkbench();}return;}
  if(['view_P','select_targets'].includes(item.id)){$('#target-planning').scrollIntoView({block:'start'});return;}
  if(item.id==='view_A'){$('#author-reference').scrollIntoView({block:'start'});return;}
  if(item.id==='prepare_A_plan'||item.id==='approve_A'){await renderAuthorPlanPanel(id);$('#author-plan-panel')?.scrollIntoView({block:'start'});return;}
  if(item.id==='analyze_A'){await renderOutputAnalysisPanel(id);$('#output-analysis-panel')?.scrollIntoView({block:'start'});return;}
  if(item.id==='compare'){resultTab='targets';renderWorkspaceResults();$('#research-results').scrollIntoView({block:'start'});return;}
  if(item.id==='preview_B_draft'){
   const draft=await api(item.endpoint);if(!paperWorkflowCurrent(id,generation))return;
   const preview=node('details',undefined,'independent-draft');preview.open=true;preview.append(node('summary','允许条件草稿 · 尚未交给 B'));
   if(draft.task_id!==id||draft.role!=='human_only_unreleased_condition_draft'||draft.released!==false)return;
   preview.append(researchContent(draft.draft?.task_text||'允许条件草稿已保存。'),node('p',draft.disabled_reason||'尚未获准交给独立计算。','form-note'));
   row.append(preview);return;
  }
  const result=await api(item.endpoint,{revision:current.revision});if(!paperWorkflowCurrent(id,generation))return;
  if(['import_targets','freeze_targets'].includes(item.id)){current=result;await afterChange(item.id==='freeze_targets'?'复现范围与条件已冻结；尚未提交计算或通过科学核验。':'论文目标清单已读取；请选择范围。尚未提交计算。');}
  else await refreshPaperWorkflow();
 });row.append(button);return row;
}
function renderAuthorEvidence(box,report){
 const state=paperWorkflowState(),identity=authorEvidenceIdentity(report),generation=renderAuthorEvidence.generation=(renderAuthorEvidence.generation||0)+1;
 const focused=identity&&state.identity===identity&&state.input&&document.activeElement===state.input?{start:state.input.selectionStart,end:state.input.selectionEnd}:null;
 const authorPanel=$('#author-plan-panel'),retained=authorPanel?.dataset.task===current?.id?authorPanel:null;
 box.replaceChildren(node('h2','作者参考 A · 原代码运行结果'),...(retained?[retained]:[]));
 if(current?.id)void renderAuthorRawOutputs(box,current.id,generation);
 if(!identity){box.append(node('p','尚无可核验的作者 A 图表报告。作业状态与已有失败记录见作者参考历史；没有报告不表示未提交。','form-note'));return;}
 box.append(node('h3',report.title),node('p',report.scope||'', 'subtle'));
 box.append(node('p',report.output_status==='complete'&&report.output_valid===true?'登记输出已完整回收；科学复现尚未核验。':'部分输出或输出待核对；不能标为完整复现。','reference-output-status'));
 box.append(node('p',report.summary||''));
 const ids=node('dl',undefined,'reference-identities');for(const [label,value] of [['请求',report.request_id],['HPC 作业',report.job_id]])ids.append(node('dt',label),node('dd',value||'记录待核对'));box.append(ids);
 const coverage=node('ul',undefined,'reference-coverage');for(const item of report.coverage||[])coverage.append(node('li',item.label+'：'+item.available+' / '+item.required+(item.unit?' '+item.unit:'')));box.append(coverage);
 for(const limitation of report.limitations||[])box.append(node('p',limitation,'form-note'));
 const methods=node('details',undefined,'reference-methods');methods.append(node('summary','查看分析方法、单位与来源版本'));
 for(const method of report.methods||[])methods.append(node('h4',method.label),node('p',method.description+(method.unit?' · 单位 '+method.unit:'')));
 methods.append(node('p','作者输出来源 '+report.source_sha256.slice(0,12)+' · 报告版本 '+report.manifest_sha256.slice(0,12),'form-note'));
 if(report.accounted===true&&Number.isFinite(report.actual_core_hours))methods.append(node('p','已核算 '+report.actual_core_hours+' 核时'));
 else methods.append(node('p','计算资源用量以作者参考账本的最终核算为准。','form-note'));box.append(methods);
 const tabs=node('div',undefined,'evidence-view-tabs'),area=node('section',undefined,'paper-evidence-content');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','切换作者 A 图表');box.append(tabs,area);
 const choice=renderAuthorEvidence.selection,views=report.views||[];
 const draw=view=>{
  if(current?.id!==report.task_id||generation!==renderAuthorEvidence.generation||paperWorkflowState().identity!==identity)return;
  renderAuthorEvidence.selection={identity,view_id:view.id};area.replaceChildren(node('h3',view.title),node('p',view.description||''));
  for(const button of tabs.children)button.setAttribute('aria-selected',String(button.dataset.view===view.id));
  const grid=node('div',undefined,'evidence-grid');
  for(const item of view.figures||[]){const figure=(report.figures||[]).find(figure=>figure.name===item.name);if(!figure||item.role!=='reference')continue;
   const card=node('figure',undefined,'evidence-figure reference');card.append(evidenceSourceHeading('reference'),node('h4',figure.label));
   const link=node('a');link.href=`/api/tasks/${report.task_id}/reference-evidence/files/${encodeURIComponent(figure.name)}?source_sha256=${report.source_sha256}`;link.target='_blank';link.rel='noopener';
   const img=node('img');img.src=link.href;img.alt=figure.label;img.loading='lazy';link.append(img);card.append(link,node('figcaption',figure.caption||''));grid.append(card);
  }area.append(grid);
  for(const table of view.tables||[])if(table.role==='reference'){
   const data={...table,columns:table.columns.map(column=>({...column,label:column.label+(column.unit?' ('+column.unit+')':'')}))};
   const tablePanel=evidenceDataTable(data,'reference-evidence');const download=Array.from(tablePanel.children).find(child=>child.tagName==='A');
   if(download)download.href+=`?source_sha256=${report.source_sha256}`;area.append(tablePanel);
  }
 };
 for(const view of views){const button=node('button',view.title);button.dataset.view=view.id;button.setAttribute('role','tab');button.onclick=()=>draw(view);tabs.append(button);}
 if(views.length)draw((choice?.identity===identity&&views.find(view=>view.id===choice.view_id))||views[0]);else area.append(node('p','已有输出尚未形成图表，请先查看下方真实文件。'));
 const downloads=node('details',undefined,'reference-downloads');downloads.append(node('summary','下载作者 A 原始结果与报告'));
 for(const file of report.files||[]){const row=node('div'),link=node('a',file.label||file.name);link.href=`/api/tasks/${report.task_id}/reference-evidence/files/${encodeURIComponent(file.name)}?source_sha256=${report.source_sha256}`;link.target='_blank';link.rel='noopener';row.append(link);if(Number.isInteger(file.size)&&file.size>=0)row.append(node('small',file.size+' 字节'));downloads.append(row);}box.append(downloads);
 renderAuthorDiscussion(box,report,identity);
 if(focused){state.input.focus({preventScroll:true});state.input.setSelectionRange(focused.start,focused.end);}
}
async function renderAuthorRawOutputs(box,id,generation){
 const panel=node('section',undefined,'reference-raw-outputs');box.append(panel);
 const alive=()=>current?.id===id&&generation===renderAuthorEvidence.generation&&panel.isConnected;
 const pending=renderAuthorRawOutputs.pending||(renderAuthorRawOutputs.pending=new Set());
 try{
  const data=await api(`/api/tasks/${id}/reference-outputs`);if(!alive())return;
  if(data.task_id!==id||!data.configured){panel.remove();return;}
  panel.append(node('h3','作者 A 原始输出'),node('p','回收已核对清单中的原始字节，供下载和后续分析；不提交计算，也不把部分输出标成整篇复现。','form-note'));
  for(const run of data.runs||[]){
   const row=node('article'),key=id+':'+run.request_id;
   row.append(node('p','作业 '+run.job_id+' · '+run.files.length+' 个文件 · '+fileSize(run.total_bytes)));
   const detail=node('details');detail.append(node('summary','查看本次可回收的原始文件清单'));
   for(const file of run.files)detail.append(node('p',file.name+' · '+fileSize(file.size)));row.append(detail);
   if(run.state==='retained_unavailable'){
    row.append(node('p','原始文件已登记，但本地字节缺失或发生变化；下载已停止，原来源和记录保留，请先恢复原文件。','form-note'));
   }else if(run.state==='retained'){
    row.append(node('p','原始输出已回收并保留；分析尚未因此完成。','form-note'));
    const show=node('button','查看原始文件下载');show.type='button';show.onclick=()=>$('#task-files')?.scrollIntoView({behavior:'smooth',block:'start'});row.append(show);
    const analyze=node('button','用应用 AI 分析这些原始数据');analyze.type='button';analyze.onclick=()=>$('#output-analysis-panel')?.scrollIntoView({behavior:'smooth',block:'start'});row.append(analyze);
   }else{
    const collect=node('button',pending.has(key)?'正在回收作者 A 输出…':'回收作者 A 原始输出');collect.type='button';collect.disabled=pending.has(key);
    const status=node('p',undefined,'form-note');status.setAttribute('role','status');
    collect.onclick=async()=>{
     if(pending.has(key))return;pending.add(key);collect.disabled=true;collect.textContent='正在回收作者 A 输出…';
     try{
      await api(`/api/tasks/${id}/reference-outputs`,{request_id:run.request_id});
      if(!alive())return;status.textContent='原始字节回收完成，正在刷新下载列表。';await refreshWorkspace();
     }catch(error){if(alive())status.textContent=error.message;}
     finally{pending.delete(key);if(alive()){collect.disabled=false;collect.textContent='回收作者 A 原始输出';}}
    };row.append(collect,status);
   }panel.append(row);
  }
 }catch(error){if(alive())panel.append(node('p','作者原始输出入口暂不可核验：'+error.message,'form-note'));}
}
const authorPlanStates=new Map();
async function renderAuthorPlanPanel(id){
 const anchor=$('#author-reference');if(!anchor||current?.id!==id)return;
 let panel=$('#author-plan-panel');if(!panel){panel=node('section');panel.id='author-plan-panel';anchor.append(panel);}
 if(panel.dataset.task!==id){panel.replaceChildren();panel.dataset.task=id;}
 const state=authorPlanStates.get(id)||{note:'',pending:null};authorPlanStates.set(id,state);
 const sequence=state.sequence=(state.sequence||0)+1;
 const alive=()=>current?.id===id&&panel.dataset.task===id&&!$('#task-view').hidden;
 try{
  const data=await api(`/api/tasks/${id}/author-plan`);if(!alive()||state.sequence!==sequence)return;
  if(state.pending){const saved=data.history.find(row=>row.request_id===state.pending);if(saved&&(saved.receipt||saved.recoverable))state.pending=null;}
  panel.replaceChildren(node('h3','作者运行方案：原文件、目标覆盖与缺项'),node('p','应用 AI 依据已保存 P 范围和固定作者文件准备运行方案。此步骤不会提交计算；原作者文件不改写，未核验的环境或调用参数会明确列出。','form-note'));
  const note=node('textarea');note.setAttribute('aria-label','作者方案修订要求');note.rows=3;note.value=state.note;note.placeholder='如需修订，说明对上一版的要求；完整上一版和具体检查反馈会一起保留。';note.oninput=()=>{state.note=note.value;};
  const status=node('p',state.error||data.disabled_reason||`已用 ${data.used_rounds}/${data.max_rounds} 版；准备不等于批准或运行。`,'form-note');
  async function send(request,revision){
   if(state.pending||!alive())return;state.pending=request;status.textContent='应用 AI 正在核对原作者调用；保留同一请求，请勿重复发送。';
   try{await api(`/api/tasks/${id}/author-plan`,{request_id:request,revision_note:revision});state.error='';}
   catch(error){if(error.requestRecorded===false)state.pending=null;state.error=error.message;}
   finally{if(alive())await renderAuthorPlanPanel(id);}
  }
  const button=node('button',state.pending?'正在准备作者方案…':data.used_rounds?'基于上一版修订作者方案':'让应用 AI 准备作者运行方案','quiet');button.disabled=Boolean(state.pending)||!data.can_prepare;
  button.onclick=()=>send(crypto.randomUUID().replaceAll('-',''),note.value);
  const refresh=node('button','刷新作者方案记录','quiet');refresh.onclick=()=>renderAuthorPlanPanel(id);panel.append(note,button,refresh,status);
  const resources=node('button','补齐作者原件：获取到 HPC，不消耗方案轮次','quiet');resources.onclick=()=>{state.resourceOpen=true;return renderAuthorResourcePanel(id,panel,state);};panel.append(resources);
  for(const item of data.history){
   const article=node('article',undefined,'result-widget');article.append(node('h4',`作者运行方案 · 第 ${item.round}/${item.max_rounds} 版`),node('p',item.receipt?.message||item.events.at(-1)?.document?.message||item.state,'form-note'));
   if(item.recoverable){const resume=node('button','恢复原作者方案请求','quiet');resume.disabled=Boolean(state.pending);resume.onclick=()=>send(item.request_id,item.revision_note||'');article.append(resume);}
   if(item.report){article.append(researchContent(item.report.summary));
    for(const question of item.report.questions||[])article.append(node('p',question));
    for(const invocation of item.report.invocations||[]){const details=node('details');details.append(node('summary','查看原入口、参数、所需文件与输出：'+invocation.id));const pre=node('pre');pre.textContent=JSON.stringify(invocation,null,2);details.append(pre);article.append(details);}
    const details=node('details');details.append(node('summary','查看完整目标覆盖、来源和分析缺项'));const pre=node('pre');pre.textContent=JSON.stringify(item.report,null,2);details.append(pre);article.append(details);
   }
   if(item.proposal){const raw=node('details');raw.append(node('summary','查看 AI 原始完整答复（保留未通过的方案）'));const pre=node('pre');pre.textContent=JSON.stringify(item.proposal,null,2);raw.append(pre);article.append(raw);}
   article.append(node('p',`状态 ${item.state} · 请求 ${item.request_id} · 模型用量 ${item.receipt?.usage?.total_tokens??'待核对'}；未批准或提交计算。`,'form-note'));panel.append(article);
  }
  if(data.execution)await renderAuthorExecutionPanel(id,panel,state,data.execution);
  if(state.resourceOpen)void renderAuthorResourcePanel(id,panel,state);
 }catch(error){if(alive())panel.replaceChildren(node('p','作者运行方案暂不可核验：'+error.message,'form-note'));}
}
async function renderAuthorExecutionPanel(id,parent,state,data){
 if(current?.id!==id||parent.dataset.task!==id)return;
 const panel=node('section',undefined,'result-widget');panel.id='author-execution-panel';parent.append(panel);
 const sequence=state.executionSequence=(state.executionSequence||0)+1;
 const alive=()=>current?.id===id&&parent.dataset.task===id&&state.executionSequence===sequence&&!$('#task-view').hidden;
 panel.append(node('h4','作者 A：准备提交包 → 批准 → 提交 HPC → 跟进与回收'));
 const status=node('p',state.executionError||data.disabled_reason||'批准只绑定当前原调用提交包；提交后显示真实作业号。','form-note');panel.append(status);
 async function action(name,body){
  if(state.executionPending||!alive())return;state.executionPending=name;status.textContent='正在处理作者 A '+name+'；保留当前请求，请勿重复点击。';
  try{await api(`/api/tasks/${id}/author-execution/${name}`,body);state.executionError='';}
  catch(error){state.executionError=error.message;}
  finally{state.executionPending=null;if(alive())await renderAuthorPlanPanel(id);}
 }
 const prepare=node('button','核对并准备作者 A 提交包','quiet');prepare.disabled=Boolean(state.executionPending)||!data.can_prepare;prepare.onclick=()=>action('prepare',{});panel.append(prepare);
 const latest=data.latest;
 if(latest){
  const payload=latest.report,resources=payload.resources;
  panel.append(node('p',`当前包 ${latest.package_sha256.slice(0,12)} · ${payload.invocations.length} 个作者原调用 · ${payload.files.length} 个原件 · ${resources.cores} 核 CPU · 最长 ${(resources.wall_seconds/3600).toFixed(2)} 小时。`,'form-note'));
  const detail=node('details');detail.append(node('summary','查看将执行的完整原调用、输入、输出、资源与版本'));const pre=node('pre');pre.textContent=JSON.stringify(payload,null,2);detail.append(pre);panel.append(detail);
  const approve=node('button',latest.approved?'这份原调用提交包已批准':'批准这份原调用提交包','quiet');approve.disabled=Boolean(state.executionPending)||!data.can_approve;approve.onclick=()=>action('approve',{revision:data.task_revision,package_sha256:latest.package_sha256,note:'用户在作者 A 普通页面批准当前完整原调用提交包。'});panel.append(approve);
  const submit=node('button','将已批准方案提交 HPC','quiet');submit.disabled=Boolean(state.executionPending)||!data.can_submit;submit.onclick=()=>action('submit',{package_sha256:latest.package_sha256});panel.append(submit);
 }
 const requests=(data.history||[]).flatMap(item=>item.requests||[]);
 for(const request of requests)panel.append(node('p',`作者 A · 作业 ${request.job_id||'尚未取得作业号'} · 状态 ${request.state} · 已核算 CPU 核时 ${request.actual_core_seconds==null?'待核算':(request.actual_core_seconds/3600).toFixed(3)} · 请求 ${request.id}`,'form-note'));
 if(requests.some(item=>item.dispatch_claimed&&item.state!=='rejected')){const follow=node('button','核对原作业状态并回收结果','quiet');follow.disabled=Boolean(state.executionPending);follow.onclick=()=>action('follow',{});panel.append(follow);}
}
async function renderAuthorResourcePanel(id,parent,state){
 if(current?.id!==id||parent.dataset.task!==id)return;
 let panel=$('#author-resource-panel');if(!panel){panel=node('section',undefined,'result-widget');panel.id='author-resource-panel';parent.append(panel);}
 panel.dataset.task=id;
 const sequence=state.resourceSequence=(state.resourceSequence||0)+1;
 const alive=()=>current?.id===id&&parent.dataset.task===id&&panel.dataset.task===id&&!$('#task-view').hidden&&state.resourceSequence===sequence;
 try{
  const data=await api(`/api/tasks/${id}/author-resources`);if(!alive())return;
  panel.replaceChildren(node('h4','补齐固定版本作者资料'),node('p','复用当前论文已批准的仓库、版本和 HPC 连接，将原件保存到超算。不调用模型、不提交计算，原方案、范围和轮次保留。','form-note'));
  if(data.initial){await renderInitialAuthorResources(id,parent,panel,state,data.initial,alive);return;}
  const latest=data.latest;
  if(latest){
   panel.append(node('p',`${latest.repository} · ${latest.commit} · 已获取 ${latest.files.length} 个文件；原件${latest.source_complete?'完整':'尚未完整'}。`,'form-note'));
   if(latest.failures?.length){const detail=node('details');detail.append(node('summary','查看资料缺项与获取诊断'));const pre=node('pre');pre.textContent=JSON.stringify(latest.failures,null,2);detail.append(pre);panel.append(detail);}
   const detail=node('details');detail.append(node('summary','查看已获取文件、大小与 SHA-256'));for(const row of latest.files||[])detail.append(node('p',`${row.path} · ${row.size} B · ${row.sha256}`));panel.append(detail);
   if(latest.inspection){
    const inspection=latest.inspection,report=inspection.report;
    panel.append(node('p',inspection.message||'原获取清单核对记录已保留；未重新下载。','form-note'));
    if(report?.state==='inspected'){
     const counts=report.summary?.counts||{};panel.append(node('p',`缓存清单共 ${report.entries.length} 项；普通文件 ${counts.supported_blob||0} 项，目录 ${counts.directory||0} 项。仅核对元数据，尚未确认源码完整或运行就绪。`,'form-note'));
     const labels={supported_blob:'普通文件（仅元数据支持）',oversized_blob:'超过单文件读取上限',directory:'目录',nonregular_member:'链接或子模块等非普通成员',invalid_metadata:'元数据无效',unsafe_path:'路径不符合安全规则'};
     const members=node('details');members.append(node('summary','查看原获取清单的具体成员与限制'));
     for(const row of report.entries||[])members.append(node('p',`${row.path} · ${labels[row.status]||row.status} · ${row.size==null?'大小未提供':row.size+' B'} · 类型 ${row.type??'未提供'} · 模式 ${row.mode??'未提供'}`));
     const raw=node('pre');raw.textContent=JSON.stringify(report,null,2);const evidence=node('details');evidence.append(node('summary','查看清单核对出处与摘要'),raw);members.append(evidence);panel.append(members);
     const select=node('button','查看应用 AI 原件选择与获取','quiet');
     select.onclick=async()=>{if(alive())await renderAuthorResourceSelections(id,panel,state);};panel.append(select);
    }else panel.append(node('p','原缓存清单尚未核对成功；原获取状态保留，可使用同一原请求再次核对。','form-note'));
   }
  }
  const feedback=node('p',state.resourceError||data.disabled_reason||state.resourceRead?.message||(state.resourceInspectionPending?'正在核对原缓存清单，不重新下载。':state.resourcePending?'正在获取原件，保留原请求，请勿重复点击。':'获取原件后，可单独读取补充文本，再修订同一作者方案。'),'form-note');feedback.setAttribute('role','status');panel.append(feedback);
  if(latest?.can_inspect_tree){
   const inspect=node('button',state.resourceInspectionPending?'正在核对原获取清单…':'核对原有获取记录具体缺项（不重新下载）','quiet');inspect.disabled=Boolean(state.resourceInspectionPending)||Boolean(state.resourcePending)||Boolean(state.sourceReadPending)||!data.enabled;
   inspect.onclick=async()=>{
    if(!alive()||state.resourceInspectionPending||state.resourcePending||state.sourceReadPending)return;
    state.resourceInspectionPending=latest.request_id;inspect.disabled=true;feedback.textContent='正在通过原保存连接核对缓存成员；不重新下载、不读取源码、不调用模型。';
    try{await api(`/api/tasks/${id}/author-resources/inspect-tree`,{request_id:latest.request_id});state.resourceError='';}
    catch(error){state.resourceError=error.message;}
    finally{state.resourceInspectionPending=null;if(alive())await renderAuthorResourcePanel(id,parent,state);}
   };panel.append(inspect);
  }
  const acquire=node('button',state.resourcePending?'正在获取原件…':latest?.recoverable?'核对原资料获取请求':'获取固定作者仓库到 HPC','quiet');acquire.disabled=Boolean(state.resourcePending)||Boolean(state.resourceInspectionPending)||Boolean(state.sourceReadPending)||!data.enabled||Boolean(latest?.source_complete);
  acquire.onclick=async()=>{
   if(!alive()||state.resourcePending||state.resourceInspectionPending||state.sourceReadPending)return;
   const request=latest?.recoverable?latest.request_id:crypto.randomUUID().replaceAll('-','');state.resourcePending=request;acquire.disabled=true;feedback.textContent='正在通过保存的 HPC 连接获取原件；不会消耗方案轮次。';
   try{await api(`/api/tasks/${id}/author-resources`,{request_id:request});state.resourceError='';}
   catch(error){state.resourceError=error.message;}
   finally{state.resourcePending=null;if(alive())await renderAuthorResourcePanel(id,parent,state);}
  };panel.append(acquire);
  if(latest?.source_complete){
   const read=node('button',state.sourceReadPending?'正在读取补充原件…':'读取补充作者原件（不调用模型）','quiet');read.disabled=Boolean(state.sourceReadPending)||Boolean(state.resourcePending)||Boolean(state.resourceInspectionPending)||!data.enabled;
   read.onclick=async()=>{
    if(!alive()||state.sourceReadPending||state.resourcePending||state.resourceInspectionPending)return;
    state.sourceReadPending=true;read.disabled=true;feedback.textContent='正在核验并读取补充原件，保留原文件与读取证明。';
    try{state.resourceRead=await api(`/api/tasks/${id}/author-resources/read`,{});state.resourceError='';}
    catch(error){state.resourceError=error.message;}
    finally{state.sourceReadPending=false;if(alive())await renderAuthorResourcePanel(id,parent,state);}
   };panel.append(read);
  }
  if(state.resourceRead){
   const read=state.resourceRead;panel.append(node('p',`已读 ${read.read_files}/${read.files} 个文件，保留 ${read.source_fragments} 个来源片段；未调用模型或新增计算。`,'form-note'));
   for(const [label,names] of [['未读文件（未冒称已检查）',read.unread_files],['截断文件（仅保留有界片段）',read.truncated_files]])if(names?.length){const detail=node('details');detail.append(node('summary',label));for(const name of names)detail.append(node('p',name));panel.append(detail);}
  }
 }catch(error){if(alive())panel.replaceChildren(node('p','作者资料入口暂不可核验：'+error.message,'form-note'));}
}
async function renderInitialAuthorResources(id,parent,panel,state,data,alive){
 if(!alive())return;
 panel.replaceChildren(node('h4','接入本次复现范围的作者原件'),node('p','先核对已登记仓库的固定版本和许可证，再让应用 AI 根据已保存的 P 范围选择原件。这里只读取资料，不执行作者代码、不批准计算。','form-note'));
 const latest=data.latest,pending=Boolean(state.bootstrapPending);
 const feedback=node('p',state.bootstrapError||data.disabled_reason||'','form-note');feedback.setAttribute('role','status');
 const action=async(suffix,body)=>{
  if(!alive()||state.bootstrapPending)return;
  state.bootstrapPending=body.request_id;state.bootstrapError='';
  feedback.textContent='正在核对本任务资料记录，请勿重复点击。';
  for(const button of panel.querySelectorAll?.('button')||[])button.disabled=true;
  try{await api(`/api/tasks/${id}/author-resources/bootstrap${suffix}`,body);}
  catch(error){state.bootstrapError=error.message;}
  finally{state.bootstrapPending=null;if(alive())await renderAuthorResourcePanel(id,parent,state);}
 };
 if(latest){
  panel.append(node('p',`${latest.repository} · 固定版本 ${latest.commit}`,'form-note'));
  panel.append(node('p',`清单状态 ${latest.state} · ${latest.tree_entries||0} 项；这不是源码已获取或计算就绪。`,'form-note'));
  for(const file of latest.license_evidence?.files||[]){
   const detail=node('details');detail.append(node('summary',`查看许可证原文：${file.path}`));
   const pre=node('pre');pre.textContent=file.text;detail.append(pre,node('p',`SHA-256 ${file.sha256}`,'form-note'));panel.append(detail);
  }
 }
 panel.append(feedback);
 if(latest?.recoverable){
  const reconcile=node('button',pending?'正在核对原请求…':'核对原清单请求（不重新获取）','quiet');
  reconcile.disabled=pending||!data.enabled;reconcile.onclick=()=>action('/reconcile',{request_id:latest.request_id});panel.append(reconcile);
 }else if(latest?.state!=='metadata_ready'){
  const inspect=node('button',pending?'正在核对清单…':'核对固定仓库清单与许可证','quiet');inspect.disabled=pending||!data.enabled;
  inspect.onclick=()=>action('',{request_id:crypto.randomUUID().replaceAll('-','')});panel.append(inspect);
 }
 if(latest?.state==='metadata_ready'&&!data.reading_approved){
  const files=latest.license_evidence?.files||[];
  if(!files.length)panel.append(node('p','未取得许可证原文，不能确认阅读用途或获取作者源码。请保留此候选的缺项。','form-note'));
  const label=node('label'),check=node('input');check.type='checkbox';check.setAttribute('aria-label','确认已查看许可证，仅认可本项目读取作者原件');
  check.disabled=pending||!files.length||!data.receipt_sha256;
  label.append(check,node('span','已查看许可证，仅认可本项目读取作者原件；不批准执行或重新分发。'));panel.append(label);
  const approve=node('button','确认阅读用途','quiet');approve.disabled=true;
  check.onchange=()=>{approve.disabled=!check.checked||pending||check.disabled;};
  approve.onclick=()=>{if(!check.checked||check.disabled)return;return action('/read-approval',{request_id:latest.request_id,receipt_sha256:data.receipt_sha256,note:'已查看界面展示的固定版本许可证原文，仅确认本项目阅读原件用途；不批准执行或重新分发。'});};panel.append(approve);
 }
 if(data.reading_approved){
  panel.append(node('p','已记录本项目阅读用途确认。下一步由应用 AI 选择文件，随后在此获取和读取原件。','form-note'));
  await renderAuthorResourceSelections(id,panel,state);
 }
 for(const missing of data.missing||[])panel.append(node('p',missing,'form-note'));
}
async function renderAuthorResourceSelections(id,parent,state){
 if(current?.id!==id||parent.dataset.task!==id)return;
 let panel=$('#author-resource-selections');
 if(!panel){panel=node('section',undefined,'result-widget');panel.id='author-resource-selections';parent.append(panel);}
 panel.dataset.task=id;
 const sequence=state.selectionSequence=(state.selectionSequence||0)+1;
 const alive=()=>current?.id===id&&parent.dataset.task===id&&panel.dataset.task===id&&!$('#task-view').hidden&&state.selectionSequence===sequence;
 try{
  const data=await api(`/api/tasks/${id}/author-resources/selections`);if(!alive())return;
  panel.replaceChildren(node('h4','应用 AI 选择缺少的作者原件'),node('p','AI 根据原 P 范围、作者方案缺项和已核验的固定 Git 树选择具体文件；保留已有原件，不默认下载整仓。选取与取件不代表科学验证，不提交计算。','form-note'));
  panel.append(node('p',`原件选择 ${data.used_rounds}/${data.max_rounds} 版；作者运行方案轮次另行保留。`,'form-note'));
  for(const row of data.history||[]){
   const detail=node('details');detail.append(node('summary',`第 ${row.round} 版 · ${row.state} · 查看文件、理由与缺项`));
   detail.append(node('p',row.receipt?.message||''));
   if(row.report){detail.append(node('p',row.report.summary));for(const item of row.report.selections||[])detail.append(node('p',`${item.path} · ${item.size} B · ${item.reason}`));
    for(const item of [...(row.report.missing||[]),...(row.report.limitations||[])])detail.append(node('p',item,'form-note'));}
   const pre=node('pre');pre.textContent=JSON.stringify(row,null,2);const record=node('details');record.append(node('summary','查看完整选择、版本与模型用量记录'),pre);detail.append(record);panel.append(detail);
  }
  const feedback=node('p',state.selectionError||data.disabled_reason||data.latest?.receipt?.message||'尚未调用模型。','form-note');feedback.setAttribute('role','status');panel.append(feedback);
  const revise=node('textarea');revise.setAttribute('aria-label','原件选择修订要求');revise.placeholder='可选：指出上一版具体需要修正的地方；应用会携带完整前稿。';revise.value=state.selectionNote||'';revise.oninput=()=>state.selectionNote=revise.value;panel.append(revise);
  const pending=data.latest?.recoverable?data.latest:null;
  const prepare=node('button',state.selectionPending?'AI 正在选择原件…':pending?'核对原件选择原请求':'让应用 AI 选择需补齐的原件','quiet');
  prepare.disabled=Boolean(state.selectionPending)||(!pending&&!data.can_prepare);
  prepare.onclick=async()=>{
   if(!alive()||state.selectionPending)return;
   const request=pending?.request_id||state.selectionRequest||crypto.randomUUID().replaceAll('-','');
   state.selectionPending=request;state.selectionRequest=request;prepare.disabled=true;feedback.textContent='应用 AI 正在使用固定树与完整前稿选择原件，实际调用与费用会保存。';
   try{const result=await api(`/api/tasks/${id}/author-resources/selections`,{request_id:request,revision_note:pending?(pending.revision_note||''):(state.selectionNote||'')});state.selectionError='';if(result.receipt)state.selectionRequest=null;}
   catch(error){state.selectionError=error.message;}
   finally{state.selectionPending=null;if(alive())await renderAuthorResourceSelections(id,parent,state);}
  };panel.append(prepare);
  const refresh=node('button','刷新原件选择记录','quiet');refresh.onclick=async()=>{if(alive())await renderAuthorResourceSelections(id,parent,state);};panel.append(refresh);
  const acquired=data.acquisitions?.latest;
  for(const item of data.acquisitions?.history||[]){
   const detail=node('details');detail.append(node('summary',`所选原件获取 · ${item.state} · ${item.report?.files?.length||0} 个文件`));
   detail.append(node('p',item.message||''),node('p','完整性仅限本次所选文件，不代表整仓完整、运行就绪或科学通过。','form-note'));
   for(const file of item.report?.files||[])detail.append(node('p',`${file.path} · ${file.size} B · SHA-256 ${file.sha256}`));
   for(const error of item.report?.failures||[])detail.append(node('p',error.reason,'form-note'));panel.append(detail);
  }
  const selected=['selected','needs_clarification'].includes(data.latest?.state)&&data.latest?.report?.selections?.length>0?data.latest:null;
  if(selected?.state==='needs_clarification')panel.append(node('p','已有具体文件可以取件；其他缺项仍待澄清，取件不会批准运行或改变科学状态。','form-note'));
  if(selected||acquired?.recoverable||acquired?.resumable){
   const original=acquired?.recoverable||acquired?.resumable;
   const fetch=node('button',state.selectedFetchPending?'正在获取所选原件…':acquired?.recoverable?'只核对原所选原件获取请求':acquired?.resumable?'继续原请求未完成的原件':'把 AI 所选原件获取到 HPC','quiet');
   fetch.disabled=Boolean(state.selectionPending)||Boolean(state.selectedFetchPending)||(!original&&data.acquisitions?.history?.some(item=>item.selection_request_id===selected?.request_id));
   if(acquired?.resume_reason)panel.append(node('p',acquired.resume_reason,'form-note'));
   fetch.onclick=async()=>{
    if(!alive()||state.selectedFetchPending)return;
    const operation=original?acquired.operation_id:state.selectedFetchRequest||crypto.randomUUID().replaceAll('-','');
    state.selectedFetchPending=operation;state.selectedFetchRequest=operation;fetch.disabled=true;
    feedback.textContent='正在通过原保存连接获取所选原件；不执行源码或提交计算，状态未知时只核对原请求。';
    try{await api(`/api/tasks/${id}/author-resources/selected-files`,{request_id:operation,selection_request_id:original?acquired.selection_request_id:selected.request_id});state.selectionError='';state.selectedFetchRequest=null;}
    catch(error){state.selectionError=error.message;}
    finally{state.selectedFetchPending=null;if(alive())await renderAuthorResourceSelections(id,parent,state);}
   };panel.append(fetch);
  }
  if(acquired?.selected_complete){
   const read=node('button',state.selectedReadPending?'正在读取所选补充原件…':'读取所选补充原件（不调用模型）','quiet');read.disabled=Boolean(state.selectedReadPending)||Boolean(state.selectedFetchPending)||Boolean(state.selectionPending);
   read.onclick=async()=>{
    if(!alive()||state.selectedReadPending)return;
    state.selectedReadPending=true;read.disabled=true;feedback.textContent='正在核对原件字节与读取证明；只读文本，保留二进制/大文件未读标记，不消耗方案轮次。';
    try{state.resourceRead=await api(`/api/tasks/${id}/author-resources/read`,{});state.selectionError='';}
    catch(error){state.selectionError=error.message;}
    finally{state.selectedReadPending=false;if(alive())await renderAuthorResourceSelections(id,parent,state);}
   };panel.append(read);
  }
  if(state.resourceRead){const value=state.resourceRead;panel.append(node('p',`已读取 ${value.read_files}/${value.files} 个原件，保存 ${value.source_fragments} 个来源片段。`,'form-note'));
   for(const [label,names] of [['未读文件',value.unread_files],['截断文件',value.truncated_files]])if(names?.length)panel.append(node('p',`${label}：${names.join('、')}`,'form-note'));
  }
 }catch(error){if(alive())panel.replaceChildren(node('p','原件选择尚不可用：'+error.message,'form-note'));}
}
const outputAnalysisStates=new Map();
async function renderOutputAnalysisPanel(id){
 const anchor=$('#research-results');if(!anchor?.parentElement||current?.id!==id)return;
 let panel=$('#output-analysis-panel');
 if(!panel){panel=node('section',undefined,'output-analysis-panel');panel.id='output-analysis-panel';anchor.parentElement.append(panel);}
 if(panel.dataset.task!==id){panel.replaceChildren();panel.dataset.task=id;}
 const alive=()=>current?.id===id&&panel.dataset.task===id&&!$('#task-view').hidden;
 const state=outputAnalysisStates.get(id)||{draft:'',pending:null,view:null};outputAnalysisStates.set(id,state);
 const sequence=state.sequence=(state.sequence||0)+1;
 try{
  const data=await api(`/api/tasks/${id}/output-analyses`);if(!alive()||state.sequence!==sequence)return;
  panel.hidden=!(data.runs||[]).length;if(panel.hidden)return;
  if(state.pending){const saved=(data.history||[]).find(item=>item.request_id===state.pending);if(saved&&(!['prepared','dispatching'].includes(saved.state)||saved.recoverable))state.pending=null;}
  // Preserve an actively edited question while refreshing only on new evidence.
  const fingerprint=JSON.stringify([data.enabled,data.runs,data.history,state.pending]);
  if(state.view===fingerprint)return;state.view=fingerprint;
  panel.replaceChildren(node('h3','分析已回收原始数据'),node('p','应用 AI 根据本次原始数据与可核验的方法提出分析方案，由工具生成数值表和图。不会新增物理计算，也不会把部分输出当作完整复现。','form-note'));
  const select=node('select');select.setAttribute('aria-label','选择原始数据作业');
  for(const run of data.runs)select.append(new Option(`${run.role==='reference'?'作者 A':'科研计算'} · 作业 ${run.job_id} · ${run.files.length} 个文件`,run.request_id));
  if(data.runs.some(run=>run.request_id===state.source))select.value=state.source;
  state.source=select.value;select.onchange=()=>{state.source=select.value;};
  const label=node('label','希望如何处理这些数据？'),input=node('textarea');input.rows=3;input.maxLength=4000;input.value=state.draft;input.setAttribute('aria-label','原始数据分析要求');
  input.placeholder='例如：核对已有数据的单位和含义，计算可支持的统计量，并绘制完整分布；保留缺项与局限。';input.oninput=()=>{state.draft=input.value;};label.append(input);
  const status=node('p',state.pending?'应用 AI 正在提出并执行受限分析；离开后返回可查看原请求。':state.lastError||'','form-note');status.setAttribute('role','status');
  const run=node('button',state.pending?'正在分析原始数据…':'让应用 AI 分析原始数据','primary');run.type='button';run.disabled=Boolean(state.pending)||!data.enabled;
  run.onclick=async()=>{
   if(!alive()||state.pending||!input.value.trim())return;
   const request=crypto.randomUUID().replaceAll('-','');state.pending=request;state.draft=input.value;run.disabled=true;status.textContent='应用 AI 正在读取本次完整原始数据和允许的方法出处；请勿重复发送。';
   try{await api(`/api/tasks/${id}/output-analyses`,{request_id:request,source_request_id:select.value,question:input.value});state.lastError='';}
   catch(error){if(error.requestRecorded===false)state.pending=null;state.lastError='原请求需要核对：'+error.message;if(alive())status.textContent=state.lastError;}
   finally{if(alive()){state.view=null;await renderOutputAnalysisPanel(id);}}
  };
  const refresh=node('button','刷新原始数据分析记录','quiet');refresh.type='button';refresh.onclick=()=>{state.view=null;void renderOutputAnalysisPanel(id);};
  panel.append(select,label,run,refresh,status);
  if(!data.enabled)panel.append(node('p','请先在模型设置中连接本项目运行模型；已有文件仍可下载。','form-note'));
  for(const item of data.history||[]){
   const article=node('article',undefined,'result-widget');
   article.append(node('h4',`${item.role==='reference'?'作者 A':'科研计算'} · 原始数据分析 · 第 ${item.round}/${item.maximum_rounds} 版`),node('p',item.question),node('p',item.message||item.state,'form-note'));
   if(item.recoverable||item.state==='analysis_execution_failed'){
    const resume=node('button',item.state==='analysis_execution_failed'?'恢复原分析执行（不重新调用模型）':'核对并恢复原分析请求','quiet');resume.type='button';resume.disabled=Boolean(state.pending);
    resume.onclick=async()=>{if(state.pending||!alive())return;state.pending=item.request_id;resume.disabled=true;
     try{await api(`/api/tasks/${id}/output-analyses`,{request_id:item.request_id,source_request_id:item.source_request_id,question:item.question});state.lastError='';}
     catch(error){state.lastError=error.message;if(error.requestRecorded===false)state.pending=null;}
     finally{if(alive()){state.view=null;await renderOutputAnalysisPanel(id);}}
    };article.append(resume);
   }
   if(item.result){
    const result=item.result;
    article.append(node('p','以下数值和图来自已执行的数据处理；科学复现仍待原文比较。列含义与单位由方案声明，逐字引用核验不等于物理单位已验证。','form-note'));
    for(const figure of result.figures?.figures||[]){
     const name=figure.name||figure.file;
     if(!name||!result.files.some(file=>file.name===name))continue;
     const img=node('img');img.src=`/api/tasks/${id}/output-analyses/${item.request_id}/files/${encodeURIComponent(name)}`;img.alt=`${item.role==='reference'?'作者 A':'科研计算'} 作业 ${item.job_id} · ${figure.operation_id||figure.operation||name}`;img.style.maxWidth='100%';article.append(img);
    }
    const summaries=result.numeric_results.filter(op=>op.method==='summary');
    if(summaries.length){const table=node('table',undefined,'result-table'),head=node('tr');for(const title of ['数据','单位','样本数','均值','最小值','最大值'])head.append(node('th',title));table.append(head);
     for(const op of summaries){const row=node('tr');for(const value of [op.table+'.'+op.column,op.value_unit,op.values.count,op.values.mean,op.values.min,op.values.max])row.append(node('td',typeof value==='number'?String(Number(value.toPrecision(8))):String(value)));table.append(row);}article.append(table);}
    const downloads=node('details');downloads.append(node('summary','下载完整派生数据、图与分析来源记录'));
    for(const file of result.files){const a=node('a',file.name);a.href=`/api/tasks/${id}/output-analyses/${item.request_id}/files/${encodeURIComponent(file.name)}?download=true`;a.download=file.name;downloads.append(a,node('br'));}article.append(downloads);
   }
   const details=node('details');details.append(node('summary','查看完整分析方案、逐字出处、局限和模型用量'));
   if(item.proposal){details.append(researchContent(item.proposal.summary||''));for(const limitation of item.proposal.limitations||[])details.append(node('p',limitation));const pre=node('pre');pre.textContent=JSON.stringify(item.proposal,null,2);details.append(pre);}
   details.append(node('p',`请求 ${item.request_id} · ${item.state} · 已记录模型用量 ${item.model_receipt?.usage?.total_tokens??'待核对'}`,'form-note'));article.append(details);panel.append(article);
  }
 }catch(error){if(alive()){panel.hidden=false;panel.replaceChildren(node('p','原始数据分析入口暂不可核验：'+error.message,'form-note'));}}
}
async function refreshAuthorDiscussion(id,identity){
 const generation=refreshAuthorDiscussion.generation=(refreshAuthorDiscussion.generation||0)+1;
 try{
  const result=await api(`/api/tasks/${id}/reference-discussion`);
  if(current?.id!==id||$('#task-view').hidden||generation!==refreshAuthorDiscussion.generation||paperWorkflowState().identity!==identity)return;
  const state=paperWorkflowState();if(result.task_id!==id||result.source_sha256!==state.report?.source_sha256)return;
  if(state.request){const returned=(result.messages||[]).find(message=>message.id===state.request.id);if(returned&&['completed','not_sent'].includes(returned.state)){state.request=null;if(returned.state==='completed')state.draft='';}}
  if(!state.request){const unresolved=(result.messages||[]).find(message=>['unknown','failed_or_unknown'].includes(message.state));if(unresolved)state.request={id:unresolved.id,question:unresolved.question,task:id,source_sha256:state.report.source_sha256};}
  state.discussion=result;const box=$('#author-reference');if(box)renderAuthorEvidence(box,state.report);
 }catch(error){
  if(current?.id!==id||$('#task-view').hidden||generation!==refreshAuthorDiscussion.generation||paperWorkflowState().identity!==identity)return;
  const state=paperWorkflowState();state.discussion={...state.discussion,enabled:false,disabled_reason:'作者 A 分析历史暂未刷新，保留已收到的答复；不能据此重发问题。',messages:state.discussion?.messages||[]};
  const box=$('#author-reference');if(box)renderAuthorEvidence(box,paperWorkflowState().report);
 }
}
function renderAuthorDiscussion(box,report,identity){
 const state=paperWorkflowState(),history=state.discussion,section=node('section',undefined,'reference-discussion');
 section.append(node('h3','作者 A 的 AI 分析与问答'),node('p','仅解释此版本作者 A 的已有数据。此处独立记账与保留历史，不向 B 的方案或讨论传送参考答案，不提交计算。','form-note'));
 const messages=node('div',undefined,'reference-discussion-history');
 for(const message of history?.messages||[]){if(message.source_sha256&&message.source_sha256!==report.source_sha256)continue;const row=node('article',undefined,'discussion-message'),status=({completed:'分析答复已返回，科学主张仍需数据支持',unknown:'请求状态待核对',failed_or_unknown:'请求未完成或状态不明，未重发',not_sent:'请求未发送'})[message.state]||'请求状态待核对';row.append(researchContent(message.question,'discussion-question'),researchContent(message.answer||'请求状态待核对，未重复发送。'),node('p',status,'form-note'),node('small',(message.provider||'')+' / '+(message.model||'')+(message.at?' · '+new Date(message.at).toLocaleString('zh-CN'):'')));if(message.id)row.append(node('small','分析请求 '+message.id));if(Number.isInteger(message.usage?.total_tokens)&&message.usage.total_tokens>=0)row.append(node('small','已记录模型用量：'+message.usage.total_tokens));messages.append(row);}section.append(messages);
 const form=node('form'),label=node('label','围绕作者 A 结果提问'),input=node('textarea');input.rows=3;input.maxLength=4000;input.value=state.draft;input.placeholder='例如：解释已有曲线的变化，并列出目前输出的缺项。';label.append(input);
 state.input=input;
 const sending=state.request?.phase==='sending';
 const button=node('button',sending?'作者 A 分析请求正在进行':state.request?'请求已保留，需先核对状态':'发送作者 A 分析问题','primary');button.type='submit';button.disabled=history?.enabled!==true||Boolean(state.request);
 const note=node('p',sending?'已保留分析身份，正在等待模型解释已有作者 A 数据。':state.request?'问题请求已保留，状态不明时不重复发送。':history?.enabled===true?'发送会使用已配置的模型并保留独立分析回执。':history?.disabled_reason||'正在读取作者 A 分析配置与历史。','form-note');
 input.oninput=()=>{if(paperWorkflowState()===state&&state.identity===identity)state.draft=input.value;};
 form.append(label,button,note);form.onsubmit=event=>{event.preventDefault();return action(async()=>{
  if(current?.id!==report.task_id||$('#task-view').hidden||paperWorkflowState()!==state||state.identity!==identity||state.discussion?.enabled!==true||state.request)return;
  const question=input.value.trim();if(!question)return;
  const preference=await api('/api/model-preference');if(current?.id!==report.task_id||paperWorkflowState()!==state||state.identity!==identity||$('#task-view').hidden)return;
  const request={id:crypto.randomUUID().replaceAll('-',''),question,task:report.task_id,source_sha256:report.source_sha256,phase:'sending'};state.request=request;button.disabled=true;note.textContent='正在分析此版本的作者 A 数据…';
  try{
   const reply=await api(`/api/tasks/${report.task_id}/reference-discussion`,{request_id:request.id,provider:preference.provider,question,source_sha256:request.source_sha256});
   if(current?.id!==report.task_id||paperWorkflowState()!==state||state.identity!==identity||$('#task-view').hidden)return;
   if(reply.id===request.id){state.discussion={...state.discussion,messages:[...(state.discussion?.messages||[]).filter(message=>message.id!==reply.id),{...reply,source_sha256:request.source_sha256}]};}
   if(reply.state==='completed'){state.request=null;state.draft='';}else request.phase='needs_reconciliation';
   await refreshAuthorDiscussion(report.task_id,identity);
  }catch(error){if(current?.id===report.task_id&&paperWorkflowState()===state&&state.identity===identity&&!$('#task-view').hidden){request.phase='needs_reconciliation';const panel=$('#author-reference');if(panel)renderAuthorEvidence(panel,state.report);}}
 });};section.append(form);box.append(section);
}

let targetPreviewGeneration=0;
function referenceLammpsTargetIds(answer){
 const dependencies=new Map(answer.dependencies.map(row=>[row.id,row]));
 const inventory=new Map((answer.inventory?.targets||[]).map(row=>[row.id,row]));
 const usesLammps=(id,seen=new Set())=>{if(seen.has(id))return false;seen.add(id);const row=dependencies.get(id);
  return Boolean(row&&(row.data_class==='raw_lammps'||row.requires.some(parent=>usesLammps(parent,seen))));};
 return answer.targets.filter(row=>inventory.get(row.target_id)?.availability!=='not_simulation'&&
   (row.data_class==='raw_lammps'||row.dependency_ids.some(id=>usesLammps(id)))).map(row=>row.target_id);
}
async function renderReferenceScopeSelection(box,id,record,answer,review,alive){
 const form=node('section',undefined,'reference-scope-selection');box.append(form);
 form.append(node('h4','选择这次要复现的论文目标'),node('p','按应用 AI 的原文核定选择目标，共享依赖只记录一次。保存范围不会提交 A，也不会改写已运行的历史。','form-note'));
 const key=id+':'+record.request_id+':'+(review?.review_id||'original')+':'+record.context_sha256;
 const states=renderReferenceScopeSelection.states||(renderReferenceScopeSelection.states=new Map());
 let state=states.get(key);if(!state){state={selected:new Set(),reason:'',preview:null,pending:null};states.set(key,state);}
 const notice=node('p','','form-note'),saved=node('div'),choices=node('details'),controls=new Map();
 choices.append(node('summary',`打开选择 ${answer.targets.length} 个图表目标（逐项勾选）`));
 const labels=new Map(answer.inventory.targets.map(row=>[row.id,row.label]));
 const invalidate=()=>{state.preview=null;previewBox.replaceChildren();approve.disabled=true;preview.disabled=state.selected.size===0||Boolean(state.pending);};
 for(const row of answer.targets){const label=node('label'),input=node('input');input.type='checkbox';input.checked=state.selected.has(row.target_id);input.disabled=Boolean(state.pending);
  input.setAttribute('aria-label',labels.get(row.target_id)||row.target_id);label.append(input,node('span',labels.get(row.target_id)||row.target_id));choices.append(label);controls.set(row.target_id,input);
  input.onchange=()=>{if(input.checked)state.selected.add(row.target_id);else state.selected.delete(row.target_id);invalidate();};}
 const choose=node('button','选择作者 LAMMPS 数据及其派生图表');choose.disabled=Boolean(state.pending);
 choose.onclick=()=>{state.selected=new Set(referenceLammpsTargetIds(answer));for(const [target,input] of controls)input.checked=state.selected.has(target);
  state.reason='依据应用 AI 的全文核定，只选择作者 LAMMPS 数据及依赖这些数据的图表；其他方法和引用资料保留在 P 清单中。';reason.value=state.reason;invalidate();};
 const reasonLabel=node('label','未选目标的理由（只选部分目标时填写）'),reason=node('textarea');reason.value=state.reason;reason.disabled=Boolean(state.pending);reason.maxLength=2000;
 reason.setAttribute('aria-label','未选论文目标的理由');reason.oninput=()=>{state.reason=reason.value;invalidate();};reasonLabel.append(reason);
 const preview=node('button','查看所选范围与共享计算'),previewBox=node('div'),approve=node('button',state.pending?'核对并保存同一范围':'确认保存这份范围');
 preview.disabled=state.selected.size===0||Boolean(state.pending);approve.disabled=!state.preview&&!state.pending;
 const body=()=>({target_request_id:record.request_id,selected_ids:[...state.selected],exclusion_reason:state.reason,...(review?{target_review_id:review.review_id}:{})});
 const showPreview=value=>{previewBox.replaceChildren();const scope=value.scope;
  previewBox.append(node('p',`选择 ${scope.selected_ids.length} 项目标 · 共用 ${scope.dependencies.length} 组依赖（含上游）`),node('p',scope.kind==='continuation_scope'?'这是已有运行后的追加范围，原运行和失败记录保留，不补作事前冻结。':'这是运行前的新范围，仍需作者原流程方案与运行批准。','form-note'));
  for(const dependency of scope.dependencies)previewBox.append(node('p',dependency.id+' · '+dependency.label));
  for(const blocker of scope.blockers)previewBox.append(node('p',blocker.id+' · '+(blocker.missing.join('；')||'方法尚未核定'),'form-note'));
  previewBox.append(node('small','范围摘要 '+value.scope_sha256));};
 preview.onclick=async()=>{if(!alive()||preview.disabled)return;preview.disabled=true;notice.textContent='正在核对已选目标与来源，不调用模型或提交计算…';
  const selection=body();try{const value=await api(`/api/tasks/${id}/reference-scopes/preview`,selection);if(!alive())return;
   if(JSON.stringify(selection)!==JSON.stringify(body())){notice.textContent='预览期间选择已改变，请按当前选择重新核对。';return;}
   state.preview={value,body:selection};showPreview(value);approve.disabled=false;notice.textContent='已生成完整范围预览。确认只保存这份范围；缺项仍须处理。';}
  catch(error){if(alive()){state.preview=null;approve.disabled=true;notice.textContent='范围尚未核对：'+error.message;}}finally{if(alive())preview.disabled=false;}};
 approve.onclick=async()=>{if(!alive()||approve.disabled)return;approve.disabled=true;
  if(!state.pending){state.pending={...state.preview.body,revision:current.revision,scope_id:crypto.randomUUID().replaceAll('-',''),expected_scope_sha256:state.preview.value.scope_sha256};}
  for(const input of controls.values())input.disabled=true;choose.disabled=true;reason.disabled=true;preview.disabled=true;
  notice.textContent='正在保存原范围请求；不会派发 A 或 B…';
  try{const value=await api(`/api/tasks/${id}/reference-scopes`,state.pending);if(!alive())return;
   state.pending=null;state.preview=null;notice.textContent=`范围已保存：${value.scope.selected_ids.length} 项目标、${value.scope.dependencies.length} 组共享依赖。作者运行仍待方案与批准。`;
   saved.replaceChildren(node('small','范围记录 '+value.id),node('p',value.scope.kind==='continuation_scope'?'已有运行的追加范围；原任务、失败、费用保留。':'已记录运行前范围。'));
   for(const input of controls.values())input.disabled=false;choose.disabled=false;reason.disabled=false;preview.disabled=false;}
  catch(error){if(alive()){notice.textContent='保存回执需核对：'+error.message+'；再点击只核对/发送同一范围身份，不建新身份。';approve.textContent='核对并保存同一范围';approve.disabled=false;}}
 };
 form.append(choose,choices,reasonLabel,preview,previewBox,approve,notice,saved);
 if(state.preview)showPreview(state.preview.value);
 try{const history=await api(`/api/tasks/${id}/reference-scopes`);if(alive()&&history.latest){const last=history.latest;
  saved.append(node('p',`最近保存范围：${last.scope.selected_ids.length} 项目标 · ${last.scope.dependencies.length} 组依赖 · ${last.state==='recorded'?'来源可核对':'原来源暂不可核对，保留历史'}`),node('small','范围记录 '+last.id));}}
 catch(error){if(alive())saved.append(node('p','已有范围记录暂不可读取：'+error.message,'form-note'));}
}
async function renderReferenceTargetAnalysis(panel,report,identity,{historyOnly=false}={}){
 const section=node('section',undefined,'reference-target-analysis');panel.append(section);
 const id=report.task_id,generation=renderPaperEvidence.generation,alive=()=>current?.id===id&&section.isConnected&&
   renderPaperEvidence.generation===generation;
 section.append(node('h3','应用 AI 核定论文目标与共享计算依赖'),node('p','根据完整原文和全部已提取 P 目标，区分 LAMMPS 计算、后处理、解析模型与引用数据，并列出可共用的计算。建议不会修改原任务、替你选目标或提交 HPC；不进入 B 的生成上下文。','form-note'));
 const content=node('div');section.append(content);
 async function refresh(){
  content.replaceChildren(node('p','正在读取已有目标核定记录…','form-note'));
  let result;
  try{result=await api(`/api/tasks/${id}/reference-target-analysis`);}
  catch(error){if(alive())content.replaceChildren(node('p','目标核定记录暂不可读取：'+error.message,'error'));return;}
  if(!alive()||result.task_id!==id)return;
  const records=result.history||[],development=result.development_recovery||{};content.replaceChildren();
  if(development.authorized)content.append(node('p',`同任务开发恢复已使用 ${development.used_requests}/${development.maximum_additional_requests} 次；与 P 子图/表列细分共用，原三稿及费用保留，不算三轮内成功。`,'form-note'));
  const pending=records.some(r=>r.blocking!==false&&['prepared','dispatching','reserved','calling','unknown'].includes(r.state));
  const latest=records.at(-1),review=latest?.reviews?.filter(r=>r.state==='completed').at(-1),answer=latest?.state==='completed'?latest.report:review?.report;
  if(answer){
   if(latest?.original_source_preserved)content.append(node('p','本次恢复严格使用原请求的 P 快照、原目标和原文；后续修复或新增的图表未自动加入这份建议。','form-note'));
   if(review)content.append(node('p','这份已保存的 AI 答复经通用格式规范化后通过复核；原失败与费用保留，没有新模型调用，科学范围仍待确认。','form-note'));
   const names={raw_lammps:'LAMMPS 原始计算',postprocessing:'计算数据后处理',analytical:'解析模型',cited:'引用资料',unclear:'方法待核定'};
   const labels=new Map((answer.inventory?.targets||[]).map(row=>[row.id,row.label]));
   content.append(node('p',`已核对全部 ${answer.targets.length} 项目标 · ${answer.dependencies.length} 组共享依赖；这是有出处的模型建议，仍需确认科学范围。`,'form-note'));
   for(const row of [...answer.targets].sort((a,b)=>a.priority-b.priority)){
    const item=node('details',undefined,'target-option');item.append(node('summary',`优先级 ${row.priority} · ${labels.get(row.target_id)||row.target_id} · ${names[row.data_class]}`),researchContent(row.reason));
    item.append(node('p','共享依赖：'+(row.dependency_ids.join('、')||'无额外计算依赖')));
    for(const gap of row.missing)item.append(node('p','待核验：'+gap,'form-note'));
    for(const ref of row.citations)item.append(node('p',ref.source_locator),node('blockquote',ref.quote));content.append(item);
   }
   const groups=node('details');groups.append(node('summary','查看共享计算、上游依赖与原文依据'));
   for(const group of answer.dependencies){const item=node('article');item.append(node('h4',group.id+' · '+group.label),researchContent(group.description),node('p','方法：'+names[group.data_class]+' · 上游：'+(group.requires.join('、')||'无')));
    for(const gap of group.missing)item.append(node('p','待核验：'+gap,'form-note'));
    for(const ref of group.citations)item.append(node('p',ref.source_locator),node('blockquote',ref.quote));groups.append(item);}content.append(groups);
   for(const limitation of answer.limitations)content.append(node('p',limitation,'form-note'));
   if(current?.mode==='reproduction')await renderReferenceScopeSelection(content,id,latest,answer,review,alive);
   if(!alive())return;
  }
  const history=node('details');history.append(node('summary',`查看 ${records.length} 次目标核定记录、失败原因、用量与未知请求恢复`));
  for(const record of records){const item=node('article');const label=({completed:'完整建议已返回',source_unavailable:'原建议保留，来源已变化',invalid_output:'建议未通过检查',failed:'模型调用未完成',unavailable:'出处或契约尚未齐备',paused:'来源或配置变化，已暂停',prepared:'已登记请求，正在核定',dispatching:'应用 AI 正在核定',reserved:'已登记请求，正在核定',calling:'应用 AI 正在核定',unknown:'请求状态待核对'})[record.state]||'状态待核对';item.append(node('p',(record.at?new Date(record.at).toLocaleString('zh-CN')+' · ':'')+label),node('small','请求 '+record.request_id));
   if(record.receipt?.message)item.append(node('p',record.receipt.message));
   if(Number.isInteger(record.usage?.total_tokens))item.append(node('small','已记录模型用量 '+record.usage.total_tokens));
   for(const savedReview of record.reviews||[]){item.append(node('p',savedReview.state==='completed'?'已保存答复格式复核通过':'已保存答复复核未通过'),node('p',savedReview.message||''));}
   if(record.state==='invalid_output'&&!(record.reviews||[]).length){
    const check=node('button','重新核验这份已保存答复（不调用模型）');check.onclick=async()=>{if(!alive()||check.disabled)return;check.disabled=true;
     try{await api(`/api/tasks/${id}/reference-target-analysis/${record.request_id}/review`,{});if(alive())await refresh();}
     catch(error){if(alive()){item.append(node('p','原答复复核未完成：'+error.message,'error'));check.disabled=false;}}};item.append(check);
   }
   if(record===latest&&record.original_source_preserved&&['invalid_output','unavailable'].includes(record.state)&&
      record.receipt?.model_receipt?.state==='completed'&&records.length<3){
    const revise=node('button','基于完整前稿修订原核定（消耗下一轮）');
    item.append(node('p','保留原 P、完整前稿和具体错误，让应用 AI 修订；不会重新提取 P 或清空三轮记录。','form-note'));
    const revisionRequest=crypto.randomUUID().replaceAll('-','');
    revise.onclick=async()=>{if(!alive()||revise.disabled)return;revise.disabled=true;
     try{await api(`/api/tasks/${id}/reference-target-analysis/${record.request_id}/revise-original`,{request_id:revisionRequest});if(alive())await refresh();}
     catch(error){if(alive()){item.append(node('p','原来源修订需查看记录：'+error.message,'error'));await refresh();}}};item.append(revise);
   }
   const recovery=(result.unknown_reissues||[]).find(row=>row.unknown_reissue_of===record.request_id);
   if(recovery){
    item.append(node('p','恢复只重发原消息和原 P 来源，保留原未知调用与未确定费用，并计入本任务三次核定上限。新调用仍未知时不会自动再发。','form-note'));
    if(recovery.reason)item.append(node('p',recovery.reason,'form-note'));
    if(recovery.enabled){
     const supplier=node('input'),retain=node('input');supplier.type='checkbox';retain.type='checkbox';
     const supplierLabel=node('label'),retainLabel=node('label');
     supplierLabel.append(supplier,node('span','我已核对这笔供应商记录，允许恢复原请求'));
     retainLabel.append(retain,node('span','保留原未知费用，并接受这一次新增模型调用'));
     const restore=node('button','按原内容恢复这次目标核定（一次）');restore.disabled=true;
     const enable=()=>{restore.disabled=!(supplier.checked&&retain.checked);};supplier.onchange=enable;retain.onchange=enable;
     const request=recovery.request_id||crypto.randomUUID().replaceAll('-','');
     restore.onclick=async()=>{if(!alive()||restore.disabled)return;restore.disabled=true;
      try{await api(`/api/tasks/${id}/reference-target-analysis/unknown-reissue`,{request_id:request,
       unknown_reissue_of:record.request_id,supplier_checked:supplier.checked,retain_unknown_charge:retain.checked});if(alive())await refresh();}
      catch(error){if(alive()){item.append(node('p','原请求恢复需核对记录：'+error.message,'error'));await refresh();}}};
     item.append(supplierLabel,retainLabel,restore);
    }
   }
   history.append(item);}content.append(history);
  const exhausted=development.authorized?development.remaining_requests===0:records.length>=3;
  const run=node('button',development.authorized?'让应用 AI 修订细分目标与依赖（开发恢复）':'让应用 AI 核定全部 P 目标与共享计算依赖','primary');run.disabled=historyOnly||pending||exhausted;
  const note=node('p',historyOnly?'当前 P 库存尚未就绪；仍可查看原核定历史，并在核对供应商后恢复原请求。':pending?'原请求仍待核对，不新发或重建。':exhausted?'本任务已批准的建议次数已用完，原产物与费用保留。':`已使用 ${records.length} 次目标建议；仅明确点击后调用已配置模型。`,'form-note');
  const reload=node('button','刷新目标核定记录');reload.onclick=refresh;
  run.onclick=async()=>{if(!alive()||run.disabled)return;run.disabled=true;note.textContent='应用 AI 正在核定完整 P；可以查看其他任务，回来刷新原请求。';
   try{await api(`/api/tasks/${id}/reference-target-analysis`,{request_id:crypto.randomUUID().replaceAll('-','')});if(alive())await refresh();}
   catch(error){if(alive()){note.textContent='核定请求需查看原记录：'+error.message;await refresh();}}
  };content.append(run,reload,note);
 }
 await refresh();
}
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
 if(identity&&source)void renderReferenceTargetAnalysis(panel,report,identity);
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
