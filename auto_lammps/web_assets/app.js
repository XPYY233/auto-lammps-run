'use strict';
const $ = (selector) => document.querySelector(selector);
const origins = {user:'用户明确指定',paper:'论文提供',code:'作者代码提供',proposed:'建议 · 待用户确认'};
const statuses = {missing:'缺失',unselected:'待选择',conflict:'有矛盾',pending:'待确认',confirmed:'已确认'};
let schema, current = null, editing = null, resolving = null, busy = false;
let literaturePreview = null;
let paperFilter='all';
let taskCache=[], workspaceReport=null, resultTab='overview', modelPreference=null, normalResult=null, rawResult=null, executionState=null;
let workspaceGeneration=0, workspaceState={task:null,phase:'loading',updated:null};
let candidateState=null, candidateTask=null, candidatePolling=false;
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
  if (busy) return;
  busy = true;
  document.querySelectorAll('.dialog-error').forEach(item=>{item.hidden=true;});
  try { await work(); } catch (error) { notice(error.message, true); }
  finally { busy = false; }
}
async function listTasks() {
  const result = await api('/api/tasks');
  taskCache=result.tasks;
  $('#task-count').textContent = result.tasks.length;
  $('#task-list').replaceChildren();
  if (!result.tasks.length) $('#task-list').append(node('p','还没有任务。','subtle'));
  for (const task of result.tasks.filter(t=>t.title.toLowerCase().includes(($('#task-search').value||'').toLowerCase()))) {
    const button = node('button',task.title, current?.id === task.id ? 'active' : '');
    button.append(node('small',task.reference_stage || (task.status === 'conditions_frozen' ? '条件已保存 · 查看历史' : '需求已保存 · 待整理')));
    button.onclick = () => action(() => openTask(task.id));
    $('#task-list').append(button);
  }
}
async function openTask(id) {
  current = await api('/api/tasks/'+id);
  $('#advanced-task').open=false;
  normalResult=null;workspaceReport=null;rawResult=null;executionState=null;
  workspaceGeneration++;workspaceState={task:id,phase:'loading',updated:null};
  for(const selector of ['#task-files','#task-information','#task-resources','#execution-flow'])$(selector).replaceChildren();
  renderWorkspaceResults();
  history.replaceState(null, '', '#'+id);
  render();
  $('#results-content').replaceChildren();
  $('#results-status').textContent='正在读取记录…';
  window.scrollTo({top:0});
  await listTasks();
  await renderHistory();
  await refreshCandidate();
  await refreshResults();
  await refreshReferenceHistory();
  await refreshWorkspace();
  await refreshDiscussion();
}
function showNew() {
  if (busy) return;
  current = null;
  history.replaceState(null, '', '#new');
  hideViews('welcome');
  selectNavigation('new');
  $('#welcome').hidden = false;
  $('#papers-view').hidden = true;
  $('#task-view').hidden = true;
  $('#message').hidden = true;
  action(listTasks);
  $('#create-form textarea').focus();
  window.scrollTo({top:0});
}
function conditionStatus(field) {
  if (!field.candidates.length) return 'missing';
  if (!field.selected) return field.candidates.length > 1 ? 'conflict' : 'unselected';
  return field.confirmed ? 'confirmed' : 'pending';
}
function render() {
  hideViews('task-view');
  selectNavigation('tasks');
  $('#welcome').hidden = true;
  $('#task-view').hidden = false;
  $('#papers-view').hidden = true;
  $('#task-title').textContent = current.title;
  $('#task-prompt').textContent = current.prompt;
  const frozen = current.status === 'conditions_frozen';
  $('#candidate-panel').hidden = !frozen;
  $('#prepare-candidate').disabled = true;
  $('#candidate-stage').textContent='正在读取准备记录…';
  $('#import-literature').hidden = frozen;
  $('#import-literature').textContent = current.mode==='reproduction' ? '导入文献证据' : '从文献导入条件';
  renderReference();
  renderTargetPlanning();
  $('#task-status').textContent = frozen ? '条件已冻结' : '条件草稿';
  $('#task-meta').textContent = `${current.mode === 'reproduction' ? '文献复现测试' : '科研计算'} · 版本 ${current.revision} · 更新于 ${new Date(current.updated_at).toLocaleString('zh-CN')}`;
  const relevant = Object.entries(current.fields).filter(([key])=>key !== 'reference' || current.mode === 'reproduction');
  const all = relevant.map(([,field])=>field);
  $('#confirmed-count').textContent = `${all.filter(f=>f.confirmed).length} / ${all.length}`;
  $('#conflict-count').textContent = all.filter(f=>conditionStatus(f)==='conflict').length;
  $('#generate-conditions').hidden = frozen;
  $('#generate-conditions').disabled = !schema.model_calls_enabled;
  $('#generation-note').textContent = frozen ? '条件已冻结。' : schema.model_calls_enabled ? '根据原始需求整理条件，保留引用和缺项，不自动确认。' : '模型整理尚未启用或额度已用完。需求和已有条件已保存。';
  $('#generation-questions').replaceChildren();
  const batches = Object.values(current.generated_batches || {}).sort((a,b)=>a.revision-b.revision);
  if (batches.length) {
    const questions = batches[batches.length-1].questions.filter(q=>conditionStatus(current.fields[q.field]) !== 'confirmed');
    if (questions.length) {
      $('#generation-questions').append(node('p','最近整理时发现的待明确事项：'));
      const list=node('ul');
      for (const q of questions) list.append(node('li',q.question));
      $('#generation-questions').append(list);
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
    if (!field.candidates.length) content.append(node('p','尚未填写，也没有自动采用默认值。','missing-text'));
    for (const choice of field.candidates) {
      const card = node('div',undefined,'candidate'+(choice.id===field.selected?' selected':''));
      const value = (choice.applicability === 'not_applicable' ? '不适用：' : '') + choice.value + (choice.unit ? ' '+choice.unit : '');
      card.append(node('p',value),node('small',origins[choice.origin]+(choice.source_locator ? ' · '+choice.source_locator : '')));
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
      content.append(card);
    }
    if (field.resolution) content.append(node('p','选择依据：'+field.resolution,'resolution'));
    row.append(head,content); $('#conditions').append(row);
  }
  $('#freeze').hidden = frozen;
  $('#freeze').disabled = !!current.issues.length || (current.mode==='reproduction' && !current.target_selection);
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
  const {events,preparation_events=[]} = await api(`/api/tasks/${id}/history`);
  if(current?.id!==id) return;
  const labels = {created:'建立任务',candidate_added:'补充条件证据',condition_selected:'选择条件',user_confirmed:'确认条件',targets_selected:'选择复现目标',target_inventory_assessed:'整理图表工况与资源',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件',reference_evidence_generated:'整理文献证据'};
  $('#history-list').replaceChildren();
  for (const item of events) {
    const [kind,fields] = item.event.split(':');
    const details = fields ? ' · '+fields.split(',').map(key=>schema.fields[key]||key).join('、') : '';
    $('#history-list').append(node('li',`版本 ${item.revision} · ${labels[kind] || kind}${details} · ${new Date(item.at).toLocaleString('zh-CN')}`));
  }
  for(const item of preparation_events) {
    $('#history-list').append(node('li',`方案准备 · ${item.label} · ${new Date(item.at).toLocaleString('zh-CN')}`));
  }
}
async function afterChange(message, field) {
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
    normalResult=null;workspaceReport=null;resultTab='overview';
    history.replaceState(null,'','#'+current.id);
    await afterChange('研究需求已保存。可在这里继续查看进度和结果。');
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
async function generateConditions() {
  const id=current.id, revision=current.revision;
  $('#generate-conditions').disabled=true;
  notice('正在整理需求中的条件，尚未提交计算。');
  try {
    current=await api(`/api/tasks/${id}/generate-conditions`,{revision});
    await afterChange('条件草稿已整理。请核对摘要中的条件与待明确事项。');
  } finally {
    await refreshModelStatus();
    render();
  }
}
$('#generate-conditions').onclick=()=>action(generateConditions);
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
async function refreshCandidate() {
  if(!current || current.status!=='conditions_frozen') return;
  const id=current.id;
  const {candidate,downloads_enabled}=await api(`/api/tasks/${id}/candidate`);
  if(current?.id!==id || $('#task-view').hidden) return;
  candidateState=candidate?.state || null; candidateTask=id;
  const automatic=schema.automatic_workflow?.configured;
  const available=(automatic?schema.automatic_workflow:schema.candidate_preparation) || {enabled:false,reason:'方案准备服务尚未配置。'};
  $('#prepare-candidate').textContent=automatic?'开始自动计算':'准备计算方案';
  $('#prepare-candidate').hidden=!!candidate || current.mode!=='research';
  $('#prepare-candidate').disabled=!available.enabled;
  $('#candidate-stage').textContent=candidate?.label || '尚未准备方案';
  $('#candidate-note').textContent=candidate ? '更新于 '+new Date(candidate.updated_at).toLocaleString('zh-CN') :
    current.mode==='reproduction' ? '文献测试任务仍需核对输入发布与访问隔离，暂不生成方案。' :
    available.enabled ? '根据已确认条件生成结构、势函数调用与计算输入；可关闭页面，稍后查看进度。' : available.reason;
  const summary=$('#candidate-summary'); summary.replaceChildren(); $('#candidate-downloads').replaceChildren();
  if(!candidate && automatic){const r=schema.automatic_workflow.resources;summary.append(node('p',`按已确认条件自动准备、提交与分析。计算资源：${r.cores} 核 · ${number(r.memory_bytes/1024**3,1)} GiB · 单次最长 ${number(r.wall_seconds/3600,2)} 小时 · 最多 ${schema.automatic_workflow.max_submissions} 次提交。`));}
  if(!candidate) return;
  const result=candidate.result;
  if(result.summary) summary.append(node('p',result.summary));
  if(result.message) summary.append(node('p',result.message,'subtle'));
  if(result.questions?.length) {
    const list=node('ul');for(const question of result.questions) list.append(node('li',question));summary.append(list);
  }
  if(result.geometry) {
    const g=result.geometry;
    summary.append(node('p',`${g.atom_count} 个原子 · ${Object.entries(g.composition).map(([element,n])=>element+' '+n).join('，')} · 仅完成几何准备`));
  }
  if(result.analysis) summary.append(node('p','拟分析：'+result.analysis.quantity+'。'+result.analysis.method));
  if(candidate.state==='prepared' && downloads_enabled) {
    for(const [name,label] of [['in.lammps','计算输入'],['structure.data','初始结构'],['analysis.json','分析说明'],['generation.json','准备记录']]) {
      const link=node('a',label+' ↓','quiet');link.href=`/api/tasks/${id}/candidate/files/${name}`;
      $('#candidate-downloads').append(link);
    }
  }
}
$('#prepare-candidate').onclick=()=>action(async()=>{
  const id=current.id;
  $('#prepare-candidate').disabled=true;
  const automatic=schema.automatic_workflow?.configured;
  try { await api(`/api/tasks/${id}/${automatic?'workflow':'candidate'}`,{revision:current.revision}); }
  finally { await refreshModelStatus(); await refreshCandidate(); await renderHistory(); }
  await refreshWorkspace();
  notice(automatic?'已开始自动准备与计算，可以关闭页面，稍后查看结果。':'方案准备已记录，可以稍后返回查看进度。');
});
setInterval(async()=>{
  if(candidatePolling || busy || !current || current.id!==candidateTask || $('#task-view').hidden ||
     !['queued','running','model_requested','preparing_files'].includes(candidateState)) return;
  candidatePolling=true;
  try {await refreshCandidate();await renderHistory();await refreshModelStatus();}
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
  if (location.hash==='#papers') { await listTasks(); await showPapers(); }
  else if (/^#[a-f0-9]{32}$/.test(location.hash)) await openTask(location.hash.slice(1));
  else if(location.hash==='#tasks') await showTasks();
  else if(location.hash==='#resources') await showResources();
  else if(location.hash==='#help') showHelp();
  else if(location.hash==='#new'){await listTasks();hideViews('welcome');selectNavigation('new');}
  else await showHome();
});

const requestStates={reserved:'已预留，未派发',dispatching:'派发中',unknown:'提交结果不明，先对账',accepted:'调度器已接受',pending:'排队中',running:'运行中',completed:'计算已结束，尚未科学核验',failed:'计算失败',cancelled:'已取消',timeout:'已超时',rejected:'提交被拒',cancel_requested:'已请求取消',cancelled_before_dispatch:'派发前取消'};
const paperEvents={bibliography_corrected:'核正论文题目与出版信息',candidate_added:'登记候选文献',paper_selected:'选入复现计划',task_linked:'关联条件任务',evaluation_linked:'关联不可重置的评测账本'};
const roleNames={agent:'主 Agent 正式评测',reference:'作者参考运行',development:'开发验证',analysis:'分析作业'};
async function showPapers() {
  hideViews('papers-view');selectNavigation('papers');
  current=null; history.replaceState(null,'','#papers');
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
  if(p.tasks.length){const go=node('button','查看结果与进度 →','primary');go.onclick=()=>action(()=>openTask(p.tasks[0].id));card.append(go);}
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
    const button=node('button','打开研究任务：'+task.title,'quiet'); button.onclick=()=>action(()=>openTask(task.id)); details.append(button);
    const revisions=node('ol');
    for (const e of task.history) {
      const [kind,field]=e.event.split(':');
      const label={created:'建立任务',candidate_added:'补充条件',condition_selected:'选择条件',user_confirmed:'确认条件',targets_selected:'选择复现目标',target_inventory_assessed:'整理图表工况与资源',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件',reference_evidence_generated:'整理文献证据'}[kind]||kind;
      revisions.append(node('li',`${new Date(e.at).toLocaleString('zh-CN')} · 条件版本 ${e.revision} · ${label}${field?' · '+field.split(',').map(k=>schema.fields[k]||k).join('、'):''}`));
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
function hideViews(show){$('#go-home').setAttribute('aria-current',show==='home-view'?'page':'false');$('.search-box').hidden=false;for(const id of ['home-view','welcome','tasks-view','papers-view','resources-view','help-view','task-view']) $('#'+id).hidden=id!==show;}
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
function taskCards(){
 const box=$('#task-cards');box.replaceChildren();statsFor(taskCache,$('#task-stats'));
 const filters=$('#task-filters');filters.replaceChildren();for(const [key,label] of Object.entries({all:'全部任务',running:'运行中',queued:'排队中',completed:'计算结束',failed:'失败',draft:'待准备'})){const b=node('button',label,taskFilter===key?'primary':'quiet');b.onclick=()=>{taskFilter=key;taskCards();};filters.append(b);}
 const tasks=taskCache.filter(t=>t.title.toLowerCase().includes(($('#task-search').value||'').toLowerCase())&&(taskFilter==='all'||taskState(t)===taskFilter));
 if(!tasks.length){box.append(emptyState('此分类暂无任务','新建一项研究，或调整搜索条件。'));return;}
 const table=node('table',undefined,'research-table'),head=node('thead'),hr=node('tr'),body=node('tbody');for(const label of ['任务','当前阶段','作业 / 提交次数','最近更新','操作'])hr.append(node('th',label));head.append(hr);table.append(head,body);
 for(const t of tasks){const tr=node('tr'),info=node('td'),title=node('strong',t.title);info.append(title,node('small',t.mode==='reproduction'?'文献验证':'科研计算'));const progress=node('td');progress.append(node('span',taskStateLabel(t),'badge '+(taskState(t)==='running'?'pending':'')));if(t.reference_stage)progress.append(node('small','作者参考：'+t.reference_stage));const counts=node('td',t.job_id?'作业 '+t.job_id:'尚未派发');if(t.submission_count!==undefined)counts.append(node('small',(t.mode==='reproduction'?'B 提交：':'提交：')+t.submission_count+' / 2'));const changed=node('td',t.updated_at?new Date(t.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'),actions=node('td'),go=node('button','查看详情','quiet');go.onclick=()=>action(()=>openTask(t.id));actions.append(go);tr.append(info,progress,counts,changed,actions);body.append(tr);}box.append(table);

}
async function showTasks(){current=null;hideViews('tasks-view');selectNavigation('tasks');history.replaceState(null,'','#tasks');await listTasks();taskCards();}
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
let discoveryLibraryNote='';
function renderResourceTable(){
 const box=$('#resource-cards');box.replaceChildren();const query=$('#resource-search').value.trim().toLowerCase();const rows=resourceRows.filter(r=>(resourceFilter==='all'||r.kind===resourceFilter||(resourceFilter==='potential'&&r.discovery&&r.hasPotential))&&[r.name,r.filename,r.type,r.paper,r.doi,r.elements,r.companion,...(r.tags||[])].join(' ').toLowerCase().includes(query));$('#resource-count').textContent=`${rows.filter(r=>r.bundle).length} 套势函数 · ${rows.filter(r=>r.kind==='potential').reduce((n,r)=>n+(r.files?.length||1),0)} 个势函数文件 · ${rows.filter(r=>r.kind==='paper'&&!r.discovery).length} 项已有源码关联 · ${rows.filter(r=>r.discovery).length} 套发现候选 · ${rows.filter(r=>r.kind==='tools').length} 项工具文档`;
 if(discoveryLibraryNote)box.append(node('p',discoveryLibraryNote,'form-note'));
 if(!rows.length){box.append(emptyState('没有匹配的资源','调整关键词或资源分类。'));return;}
 const table=node('table',undefined,'research-table'),head=node('thead'),hr=node('tr'),body=node('tbody');for(const label of ['资源','类型 / 元素','版本','验证状态','来源与详情'])hr.append(node('th',label));head.append(hr);table.append(head,body);
 for(const r of rows){const tr=node('tr'),name=node('td');tr.dataset.resourceKind=r.kind;name.append(node('strong',r.name));if(r.filename)name.append(node('small','原始文件：'+r.filename,'mono'));if(r.tags){const tags=node('div',undefined,'resource-tags');for(const tag of r.tags)tags.append(node('span',tag,'badge'));name.append(tags);}if(r.companion&&!r.bundle)name.append(node('small','配套文件：'+r.companion));const type=node('td',r.type);if(r.elements)type.append(node('small',r.elements));const version=node('td',r.version?r.version.slice(0,12):'未登记','mono'),state=node('td',r.state),detail=node('td'),d=node('details');d.append(node('summary','查看依据'));if(r.paper)d.append(node('p',r.paper));if(r.doi){const a=node('a','DOI '+r.doi);a.href='https://doi.org/'+r.doi;a.target='_blank';a.rel='noopener noreferrer';d.append(a);}if(r.version)d.append(node('p','固定版本：'+r.version));for(const line of r.note.split('\n'))d.append(node('p',line));if(r.files){d.append(node('h4','配套文件与来源'));for(const f of r.files){d.append(node('p',f.filename,'mono'));if(f.hash)d.append(node('code','SHA-256 '+f.hash));const link=node('a','查看该文件来源');link.href=f.url;link.target='_blank';link.rel='noopener noreferrer';d.append(link);}}else if(r.hash)d.append(node('code','SHA-256 '+r.hash));const a=node('a','原始来源 ↗');a.href=r.url;a.target='_blank';a.rel='noopener noreferrer';d.append(a);detail.append(d);tr.append(name,type,version,state,detail);body.append(tr);}box.append(table);
}
async function showResources(){
 current=null;hideViews('resources-view');selectNavigation('resources');history.replaceState(null,'','#resources');
 const result=await api('/api/papers');resourceRows=[];let discovered;try{discovered=await api('/api/resource-discoveries');}catch(error){discoveryLibraryNote='发现清单暂时无法读取，已登记的资源仍保留。';}
 for(const button of document.querySelectorAll('[data-resource]')){button.classList.toggle('selected',button.dataset.resource===resourceFilter);button.onclick=()=>{resourceFilter=button.dataset.resource;for(const b of document.querySelectorAll('[data-resource]'))b.classList.toggle('selected',b===button);renderResourceTable();};}
 for(const p of result.papers){
  resourceRows.push(...potentialBundleRows(p));
  for(const c of (p.source_discovery?.candidates||[]).filter(c=>c.association==='doi_and_title'))resourceRows.push({kind:'paper',name:c.repository,type:'论文源码',elements:'',version:c.commit?.slice(0,12),paper:p.title,doi:p.doi,url:c.url,state:'题目与 DOI 相符',note:'关联证据不等于作者身份核验。源码仅供参考端验证，不提供给独立评测生成者。'});
 }
 if(discovered?.configured){
  discoveryLibraryNote=`发现目录：${discovered.entries.length} 条主记录 · ${discovered.repository_count} 个仓库（含镜像） · 交接时间 ${discovered.source.generated} · 来源版本 ${discovered.source.sha256.slice(0,12)}。候选分级来自发现方，尚未获得控制端运行验证。`;
  for(const d of discovered.entries)resourceRows.push({kind:'paper',discovery:true,hasPotential:d.potential_files.length>0||d.pair_styles.length>0,name:d.title,type:'源码 + 势函数候选',elements:d.elements.length?d.elements.join(' · ')+'（发现方标注，待核验）':'元素与类型映射待核验',version:d.commit,paper:d.title,doi:d.doi,url:d.url,state:d.state==='conflict'?'关联冲突':d.state==='missing_resources'?'资源有缺项':'候选 · 待核验',tags:[...d.pair_styles,d.source_tier.replace('tier_','发现方分级 ').toUpperCase()],note:[d.repository,d.description,'关联依据（发现方）：'+d.evidence,'许可标注：'+d.license,'LAMMPS入口：'+(d.input_files.join('、')||'未登记'),'势函数文件：'+(d.potential_files.join('、')||'未登记'),'缺项：'+d.gaps.join('；'),d.mirrors.length?'镜像：'+d.mirrors.join('、'):'','没有纳入独立 B 的答案或源码检索。'].filter(Boolean).join('\n')});
 }else if(discovered)discoveryLibraryNote='发现目录尚未配置；下方仅为已登记资源。';
 for(const [name,url,note] of [['LAMMPS','https://docs.lammps.org/','模拟引擎；实际版本及能力以任务环境记录为准。'],['OVITO','https://www.ovito.org/','结构与轨迹分析工具；网页交互尚待接入。']])resourceRows.push({kind:'tools',name,type:'工具文档',url,note,state:'官方文档'});
 renderResourceTable();
}
$('#resource-search').oninput=renderResourceTable;
$('#resource-refresh').onclick=()=>action(showResources);

function showHelp(){current=null;hideViews('help-view');selectNavigation('help');history.replaceState(null,'','#help');renderHelp();}
function emptyState(title,message){const box=node('div',undefined,'empty-result');box.append(node('span','◇','empty-icon'),node('h3',title),node('p',message));return box;}
function number(v,digits=4){return Number.isFinite(v)?v.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:0}):'—';}
function fileSize(bytes){if(bytes<1024)return number(bytes,0)+' B';if(bytes<1024**2)return number(bytes/1024,2)+' KiB';if(bytes<1024**3)return number(bytes/1024**2,2)+' MiB';return number(bytes/1024**3,2)+' GiB';}
function currentRawFiles(){return rawResult?.task===current?.id?rawResult.files:[];}
function duration(seconds){const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=Math.floor(seconds%60);return `${h} 小时 ${m} 分 ${s} 秒`;}
function addInfo(label,value){$('#task-information').append(node('dt',label),node('dd',value));}
function bState(report){if(report.closeout)return '已完成比较 · 基准已验收';return report.agent_progress?.stage || '记录暂不可核验';}
function bCount(report){return report.agent_progress?.available ? `${report.agent_progress.dispatch_claims} / 2` : '暂不可核验';}
function renderFlow(report){
 let box=$('#execution-flow');box.replaceChildren();if(report){const details=node('details',undefined,'reference-flow');details.append(node('summary','查看作者参考 A 的执行阶段'));box.append(details);box=details;}const head=node('div',undefined,'flow-heading');head.append(node('h2',report?'作者参考 A · 执行流程':'任务执行流程'),node('span',report?'完整运行 '+duration(report.runtime.elapsed_seconds):'等待模型与执行服务就绪'));box.append(head);
 const labels=['需求理解','结构准备','势函数','计算脚本','提交 HPC','运行结束','结果分析'];const steps=node('ol',undefined,'flow-steps');
 for(let i=0;i<labels.length;i++){const done=!!report&&report.stages[i]?.state==='completed';const li=node('li',undefined,done?'done':i===0?'current':'');li.append(node('span',done?'✓':String(i+1)),document.createTextNode(labels[i]));steps.append(li);}box.append(steps,node('p',report?`参考 A 已结束并完成诊断分析；B：${bState(report)}。${report.closeout?'用户已确认基准工况验收，其他工况尚未覆盖。':'科学结论仍待核验。'}`:'需求已保存。缺项会集中说明；当前不会自动提交计算。','flow-note'));
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
 curves.forEach((c,i)=>el('polyline',{points:c.points.map(p=>`${sx(p[0])},${sy(p[1])}`).join(' '),fill:'none',stroke:i?'#a96815':'#087f8c','stroke-width':i?1.25:2,'stroke-linejoin':'round'}));return svg;
}
function limitations(report){const d=node('details',undefined,'result-limits');d.append(node('summary','查看结果限制与分析依据'));const ul=node('ul');for(const text of report.limitations)ul.append(node('li',text));d.append(ul);return d;}
function reportDownloads(report,box){
 if(report.closeout){const accepted=node('details',undefined,'download-group');accepted.open=true;accepted.append(node('summary','P–A–B 对照与验收'));for(const f of report.closeout.files){const row=node('div',undefined,'file-row'),a=node('a',f.label+' ↓');a.href=`/api/tasks/${current.id}/closeout/files/${f.name}`;row.append(a,node('small',fileSize(f.size)));accepted.append(row);}box.append(accepted);}
 const group=node('details',undefined,'download-group');group.open=true;group.append(node('summary','作者参考 A · 分析与报告'));box.append(group);
 for(const f of report.files.filter(f=>f.name!=='analysis.json')){const row=node('div',undefined,'file-row'),a=node('a',f.label+' ↓');a.href=`/api/tasks/${current.id}/reference-result/files/${f.name}`;row.append(a,node('small',fileSize(f.size)));group.append(row);}
}
function closeoutTable(report){
 const table=node('table',undefined,'result-table'),head=node('thead'),tr=node('tr'),body=node('tbody');
 for(const label of ['指标 / GPa','P','A','B','|P−A|','|A−B|','|P−B|'])tr.append(node('th',label));head.append(tr);table.append(head,body);
 for(const m of report.metrics){const row=node('tr'),name=node('td',m.label);name.append(node('small',m.method));row.append(name);for(const k of ['P','A','B','absolute_PA','absolute_AB','absolute_PB'])row.append(node('td',number(m[k],5),'number'));body.append(row);}return table;
}
function closeoutFigure(report,figure){
 const panel=node('figure',undefined,'paper-figure'),link=node('a');link.href=`/api/tasks/${current.id}/closeout/files/${figure.name}`;link.target='_blank';link.rel='noopener';const img=node('img');img.src=link.href;img.alt=figure.label;img.loading='lazy';link.append(img);panel.append(link,node('figcaption',figure.caption));return panel;
}
function closeoutCoverage(report){
 const panel=node('details',undefined,'coverage-panel');panel.append(node('summary',`论文目标覆盖 · ${report.coverage.length} 组图表`));const table=node('table',undefined,'research-table'),body=node('tbody');for(const item of report.coverage){const row=node('tr'),target=node('td');target.append(node('strong',item.target),node('small',item.content));const evidence=node('td');evidence.append(node('strong',item.status),node('p',item.evidence),node('small',item.additional_work));row.append(target,evidence);body.append(row);}table.append(body);panel.append(table);return panel;
}
let closeoutPlotChoice='ab';
function renderCloseout(reference,box){
 const r=reference.closeout,a=r.acceptance,banner=node('section',undefined,'acceptance-note');banner.append(node('span','基准复现成功 · 用户验收','badge'),node('strong','第一周已完成'),node('p',a.scope),node('small',`${a.date} · 用户确认；保留科学诊断与原始提交记录。`));box.append(banner);
 if(resultTab==='plots'){
  const label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择图表');const options={ab:'A–B 应力–应变',elastic05:'A–B 弹性区间 0–0.05',elastic06:'A–B 弹性区间 0–0.06',author:'作者 A 原有诊断视图'};r.figures.forEach((f,i)=>options['paper'+i]=f.label);for(const [k,v] of Object.entries(options))select.append(new Option(v,k));select.value=Object.hasOwn(options,closeoutPlotChoice)?closeoutPlotChoice:'ab';label.append(select);box.append(label);const area=node('div',undefined,'chart-area');box.append(area);const draw=()=>{area.replaceChildren();if(select.value.startsWith('paper'))area.append(closeoutFigure(r,r.figures[Number(select.value.slice(5))]));else if(select.value==='author')renderPlotGallery(reference,area);else area.append(curvePlot({...r,xmax:select.value==='elastic05'?.05:select.value==='elastic06'?.06:.5}),node('p','青绿：作者参考 A；琥珀：独立生成 B。P 完整曲线未数字化，见论文原图。','plot-caption'));};select.onchange=()=>{closeoutPlotChoice=select.value;draw();};draw();
 }else if(resultTab==='overview'){
  const grid=node('div',undefined,'closeout-figures'),computed=node('article',undefined,'result-widget');computed.append(node('h3','A–B 实际应力–应变'),curvePlot(r),node('p','青绿：A；琥珀：B。初始体积归一化的平均应力。','plot-caption'));grid.append(computed);if(r.figures[0])grid.append(closeoutFigure(r,r.figures[0]));box.append(grid);
 }
 if(resultTab!=='plots'){const scroll=node('div',undefined,'comparison-scroll');scroll.append(closeoutTable(r));box.append(scroll,node('p','P 为论文表格值；两种模量窗口分别保留。数值比较不代替整条曲线或整篇论文验证。','plot-caption'));}
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
 if(!r && normalResult?.evaluations?.length){for(const group of normalResult.evaluations){box.append(node('h3',`计算记录 · ${group.dispatch_count} / ${group.max_attempts} 次提交`));for(const request of group.requests){box.append(node('p',request.stage));for(const report of request.reports)box.append(resultReport(report,current.id));}}return;}
 if(!r&&currentRawFiles().length){
  box.append(emptyState('原始输出已回收','可从右侧下载原始数据和日志。分析报告尚未生成或未接入；文件回收不代表科学验证通过。'));
  const jobs=new Map(currentRawFiles().map(f=>[f.request_id,f]));
  for(const f of jobs.values())box.append(node('p',`作业 ${f.job_id} · ${requestStates[f.state]||f.state}`));
  return;
 }
 if(!r){const empty=emptyState('还没有计算结果',current.status==='conditions_frozen'?'研究条件已保存。方案与计算记录会在服务就绪后显示。':'研究需求已保存。方案、计算与分析就绪后，真实结果会显示在这里。');const button=node('button','查看模型设置','quiet');button.onclick=()=>action(openModel);empty.append(button);box.append(empty);return;}
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
async function refreshWorkspace(){
 if(!current)return;
 const id=current.id, generation=++workspaceGeneration;
 if(workspaceState.task!==id)workspaceState={task:id,phase:'loading',updated:null};
 workspaceState.phase='loading';renderWorkspaceResults();renderRefreshStatus();
 const replies=await Promise.allSettled([
  api(`/api/tasks/${id}/reference-result`),api(`/api/tasks/${id}/execution`),
  api(`/api/tasks/${id}/raw-files`),api(`/api/tasks/${id}/results`)
 ]);
 if(current?.id!==id||generation!==workspaceGeneration)return;
 if(replies.some(r=>r.status==='rejected')){
  workspaceState.phase='error';renderRefreshStatus();renderWorkspaceResults();return;
 }
 const [reference,execution,raw,results]=replies.map(r=>r.value);
 workspaceReport=reference.report;executionState=execution;rawResult={task:id,files:raw.files};normalResult=results;
 workspaceState={task:id,phase:'ready',updated:new Date()};renderRefreshStatus();
 const r=workspaceReport;
 $('.discussion-panel').hidden=!r&&!normalResult?.evaluations?.some(e=>e.requests.some(q=>q.reports?.length));
 $('#task-tags').replaceChildren();
 if(r){$('#task-status').textContent='A 已结束 · B '+bState(r);$('#task-meta').textContent='文献复现验证 · '+r.scope;for(const label of (r.closeout?['作者原始流程','P–A–B 比较已保存','基准已验收 · 用户确认']:['作者原始流程','P–A 诊断已保存','科学结论待核验']))$('#task-tags').append(node('span',label,'tag'));}
 else{for(const key of ['material','temperature','potential']){const f=current.fields[key],c=f?.candidates.find(c=>c.id===f.selected);if(c)$('#task-tags').append(node('span',c.value+(c.unit?' '+c.unit:''),'tag'));}}
 renderFlow(r);$('#task-information').replaceChildren();$('#task-files').replaceChildren();$('#task-resources').replaceChildren();
 addInfo('状态',r?'A 已结束，B '+bState(r):current.status==='conditions_frozen'?'条件已保存':'需求已保存');addInfo('记录更新',new Date(current.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}));
 if(r){addInfo('A 运行时长',duration(r.runtime.elapsed_seconds));addInfo('A 使用核数',String(r.runtime.cores));addInfo('A 提交次数',`${r.evaluation.dispatch_claims} 次（含失败）`);addInfo('B 提交次数',bCount(r));addInfo('A 本次核时',number(r.runtime.core_hours,3));addInfo('B 资源额度','用户未设上限');addInfo('A 作业号',r.job_id);reportDownloads(r,$('#task-files'));const a=node('a','论文与 DOI ↗','resource-link');a.href='https://doi.org/'+r.doi;a.target='_blank';a.rel='noopener noreferrer';a.append(node('small',r.title));$('#task-resources').append(a);}
 else{$('#task-files').append(node('p','计算及分析产生的文件会保存在这里。','subtle'));addInfo('提交次数','尚无已核验记录');}
 for(const [label,url] of [['LAMMPS 使用文档','https://docs.lammps.org/'],['OVITO 分析工具','https://www.ovito.org/']]){const a=node('a',label+' ↗','resource-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';$('#task-resources').append(a);}
 $('#task-model').textContent=r?'第一周 · Codex 辅助验证':'应用模型尚未启用';
 const tabs=$('#result-tabs');tabs.replaceChildren();for(const [key,label] of Object.entries({overview:'结果总览',data:'关键数据',plots:'可视化图表',structure:'原子结构',trajectory:'轨迹动画',report:'分析报告',history:'历史记录'})){const b=node('button',label);b.setAttribute('role','tab');b.setAttribute('aria-selected',String(key===resultTab));b.onclick=()=>{resultTab=key;for(const x of tabs.children)x.setAttribute('aria-selected',String(x===b));renderWorkspaceResults();};tabs.append(b);}renderWorkspaceResults();
 renderRawFiles(raw);
 if(current?.id!==id)return;
 if(!r&&currentRawFiles().length&&!normalResult?.evaluations?.length){
  $('#task-status').textContent='原始输出已回收';
  $('#task-information').replaceChildren();addInfo('结果状态','原始输出已回收');addInfo('分析报告','尚未生成或未接入');
  addInfo('提交次数','完整提交记录尚未接入');
  for(const f of new Map(currentRawFiles().map(f=>[f.request_id,f])).values())addInfo('作业 '+f.job_id,requestStates[f.state]||f.state);
  $('#execution-flow').replaceChildren(node('h2','计算记录'),node('p','原始输出已回收，可下载查看。完整执行阶段及分析报告尚未接入。','flow-note'));
 }
 renderExecutionControls();renderWorkspaceResults();
 if(!$('.discussion-panel').hidden)await refreshDiscussion();
}
function renderExecutionControls(){
 if(workspaceReport||!executionState)return;
 const box=$('#execution-flow'),job=executionState.job;
 if(job){
  box.replaceChildren(node('h2','任务执行流程'),node('p',job.label,'flow-note'));
  $('#task-status').textContent=job.label;
  $('#task-information').replaceChildren();addInfo('执行状态',job.label);addInfo('提交次数',`${job.dispatch_count} / ${job.max_attempts}`);
  if(job.job_id)addInfo('作业号',job.job_id);
  addInfo('资源核算',job.accounted?'已核算':'待核算');addInfo('科学结论','尚未核验');
  if(!executionState.worker_alive&&['queued','running','waiting'].includes(job.state))box.append(node('p','后台当前未运行；恢复服务后继续已有请求，不会重复提交。','form-note'));
  if(job.state==='attention')box.append(node('p',job.reason==='deployment_file_missing'?'执行所需的部署文件尚未就绪，记录已保留。':job.reason==='deployment_changed'?'执行配置发生变化，需要核对后恢复。':'执行检查未通过，记录已保留；不会自动重提计算。','form-note'));
  const details=node('details');details.append(node('summary','执行历史'));const list=node('ol');for(const e of job.events)list.append(node('li',new Date(e.at).toLocaleString('zh-CN')+' · '+e.label));details.append(list);box.append(details);
 }else{
  const flow=executionState.automatic_workflow,step=flow?.workflow;
  if(step){box.replaceChildren(node('h2','任务执行流程'),node('p',step.label,'flow-note'));$('#task-status').textContent=step.label;
   if(step.state==='attention')box.append(node('p','请查看准备记录中的问题；已有调用和提交历史保留，未自动重试。','form-note'));
   else if(!flow.worker_alive)box.append(node('p','后台当前未运行；服务恢复后继续已有流程。','form-note'));
   return;
  }
  if(executionState.can_start){
   box.replaceChildren(node('h2','计算方案已准备'),node('p','开始后将核对执行许可，自动提交、跟进和处理结果。','flow-note'));
   $('#task-status').textContent='方案已准备';$('#task-information').replaceChildren();addInfo('执行状态','等待开始');
   if(executionState.submissions)addInfo('提交次数',`${executionState.submissions.count} / ${executionState.submissions.maximum}`);
  }
  box.append(node('p',executionState.message||'计算服务状态待核对。','form-note'));
  if(executionState.can_start){const button=node('button','开始计算','primary');button.onclick=()=>action(async()=>{const id=current.id;button.disabled=true;await api(`/api/tasks/${id}/execution`,{revision:current.revision});if(current?.id===id){await refreshResults();await refreshWorkspace();}});box.append(button);}
 }
}
let hpcState=null;
async function openHPC(){hpcState=await api('/api/hpc-connection');const p=hpcState.profile||{};for(const [id,key] of Object.entries({'hpc-label':'label','hpc-host':'host','hpc-user':'username','hpc-work':'work_directory','hpc-partition':'partition','hpc-account':'account'}))$('#'+id).value=p[key]||'';$('#hpc-port').value=p.port||22;$('#hpc-auth').value=p.authentication||'agent';clearHPCSecrets();$('#hpc-key-label').hidden=$('#hpc-auth').value!=='private_key';$('#hpc-dialog .dialog-error').hidden=true;$('#check-hpc').disabled=!hpcState.configured;$('#hpc-status').textContent=hpcState.configured?'连接已保存；SSH 检查仅验证登录，计算环境与自动提交另行验证。':'填写你自己的计算连接。应用不预设其他用户的超算地址。';$('#hpc-dialog').showModal();}
function clearHPCSecrets(){for(const id of ['hpc-key','hpc-known','hpc-cert'])$('#'+id).value='';}
$('#open-hpc').onclick=()=>action(async()=>{$('#model-key').value='';$('#model-dialog').close();await openHPC();});
$('#switch-model').onclick=()=>action(async()=>{clearHPCSecrets();$('#hpc-dialog').close();await openModel();});
$('#close-hpc').onclick=()=>{clearHPCSecrets();$('#hpc-dialog').close();};
$('#hpc-dialog').addEventListener('close',clearHPCSecrets);
$('#hpc-auth').onchange=()=>{$('#hpc-key-label').hidden=$('#hpc-auth').value!=='private_key';};
$('#hpc-form').onsubmit=e=>{e.preventDefault();action(async()=>{const payload={revision:hpcState.revision,label:$('#hpc-label').value.trim(),host:$('#hpc-host').value.trim(),port:Number($('#hpc-port').value),username:$('#hpc-user').value.trim(),work_directory:$('#hpc-work').value.trim(),partition:$('#hpc-partition').value.trim(),account:$('#hpc-account').value.trim(),authentication:$('#hpc-auth').value,private_key:$('#hpc-key').value||null,known_hosts:$('#hpc-known').value||null,certificate:$('#hpc-cert').value||null};try{hpcState=await api('/api/hpc-connection',payload);}finally{clearHPCSecrets();}$('#check-hpc').disabled=false;$('#hpc-status').textContent='已保存第 '+hpcState.revision+' 版连接。运行中的作业仍使用原连接；通用自动提交尚未开放。';});};
$('#check-hpc').onclick=()=>action(async()=>{const b=$('#check-hpc');b.disabled=true;$('#hpc-status').textContent='正在检查已保存的 SSH 连接…';try{const result=await api('/api/hpc-connection/check',{revision:hpcState.revision});$('#hpc-status').textContent=result.message;}finally{b.disabled=false;}});
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
async function openModel(){modelPreference=await api('/api/model-preference');connectionState=await api('/api/model-connections');$('#provider-choice').value=modelPreference.provider;$('#model-name').value=modelPreference.model;$('#model-key').value='';$('#model-dialog .dialog-error').hidden=true;refreshConnectionLabel();$('#model-dialog').showModal();}
function refreshConnectionLabel(){const c=connectionState?.connections[$('#provider-choice').value];$('#connection-status').textContent=c?.configured?'已保存密钥，留空可保留；连接可用性以实际响应为准。':'尚未保存该模型商的密钥。';$('#fetch-models').disabled=!c?.configured||!c?.model_listing;$('#remove-key').disabled=!c?.configured;if(c&&!c.model_listing)$('#connection-status').textContent+=' 此模型商请手动填写模型 ID。';}
$('#provider-choice').onchange=()=>{$('#model-key').value='';$('#available-models').replaceChildren();$('#model-name').value=connectionState?.connections[$('#provider-choice').value]?.model||'';refreshConnectionLabel();};
$('#model-form').onsubmit=e=>{e.preventDefault();action(async()=>{const provider=$('#provider-choice').value,model=$('#model-name').value.trim(),key=$('#model-key').value;try{connectionState=await api('/api/model-connections',{provider,model,api_key:key||null});}finally{$('#model-key').value='';}modelPreference=await api('/api/model-preference',{provider,model,revision:modelPreference.revision});$('#open-model').title='模型与计算连接 · '+modelPreference.providers[provider];refreshConnectionLabel();notice('模型连接已保存。可读取模型目录，或在结果页发送分析问题。保存本身不调用模型。');});};
$('#fetch-models').onclick=()=>action(async()=>{const result=await api('/api/model-connections/models',{provider:$('#provider-choice').value});$('#available-models').replaceChildren(...result.models.map(id=>new Option(id,id)));$('#connection-status').textContent=`已取得 ${result.models.length} 个模型 ID${result.has_more?'（目录还有后续页，可手动输入）':''}。点击模型输入框选择。`});
$('#remove-key').onclick=()=>action(async()=>{connectionState=await api('/api/model-connections',{provider:$('#provider-choice').value,model:'',remove:true});$('#model-key').value='';refreshConnectionLabel();});
$('#close-model').onclick=()=>{$('#model-key').value='';$('#model-dialog').close();};for(const id of ['open-model','home-model','rail-model','discussion-model'])$('#'+id).onclick=()=>action(openModel);
for(const id of ['top-tasks','back-tasks','home-tasks'])$('#'+id).onclick=()=>action(showTasks);
for(const id of ['top-new','tasks-new'])$('#'+id).onclick=showNew;
$('#top-papers').onclick=()=>action(showPapers);$('#pick-paper').onclick=()=>action(showPapers);
for(const id of ['top-resources'])$('#'+id).onclick=()=>action(showResources);
for(const id of ['top-help','home-help'])$('#'+id).onclick=showHelp;
$('#mode-research').onclick=()=>setMode('research');$('#mode-reproduction').onclick=()=>setMode('reproduction');
$('#task-search').oninput=()=>{action(async()=>{await listTasks();if(!$('#tasks-view').hidden)taskCards();});};

$("#refresh-workspace").onclick=()=>action(async()=>{await refreshResults();await refreshWorkspace();notice("已读取最新记录，没有提交计算。");});

// Shared navigation follows the approved home; all counts come from saved evidence.
let taskFilter='all', selectedPlot='full', discussionRequest=null;
function taskState(t){return t.execution_state||'draft';}
function taskStateLabel(t){return ({running:'运行中',queued:'排队中',accepted:'已提交',completed:'计算结束 · 待核验',failed:'失败',timeout:'超时',draft:'待准备'})[taskState(t)]||'状态待核对';}
function statsFor(tasks, box){box.replaceChildren();for(const [label,value] of [['全部任务',tasks.length],['运行中',tasks.filter(t=>taskState(t)==='running').length],['排队中',tasks.filter(t=>['queued','accepted'].includes(taskState(t))).length],['计算结束',tasks.filter(t=>taskState(t)==='completed').length]]){const c=node('div',undefined,'stat'),icon=node('span',undefined,'stat-icon');icon.append(uiIcon(label==='运行中'?'play':label==='排队中'?'clock':label==='计算结束'?'check':'tasks'));c.append(icon,node('small',label),node('strong',String(value)));box.append(c);}}
async function showHome(){current=null;hideViews('home-view');selectNavigation('');history.replaceState(null,'','#home');await listTasks();statsFor(taskCache,$('#home-stats'));const papers=await api('/api/papers');const selected=papers.papers.filter(p=>p.selection==='selected');const box=$('#home-cases-content');box.replaceChildren();for(const p of selected.slice(0,2)){const c=node('article',undefined,'compact-case');c.append(node('span','文献复现验证','badge pending'),node('h3',p.title),node('small','DOI '+p.doi),node('p',p.stage));const go=node('button','查看进度','quiet');go.onclick=()=>action(()=>p.tasks.length?openTask(p.tasks[0].id):showPapers());c.append(go);box.append(c);}if(!selected.length)box.append(node('p','尚未选定验证案例。','subtle'));const status=$('#home-status');status.replaceChildren();for(const [k,v] of [['计算状态',taskCache.some(t=>taskState(t)==='running')?'有任务正在运行':'以任务记录为准'],['案例清单',selected.length+' 篇已选'],['模型连接','点击模型设置查看'],['存储空间','未连接实时用量查询']])status.append(node('dt',k),node('dd',v));const dl=$('#home-downloads');dl.replaceChildren();const finished=taskCache.filter(t=>t.reference_stage||taskState(t)==='completed');for(const t of finished.slice(0,3)){const b=node('button',t.title,'download-task');b.onclick=()=>action(()=>openTask(t.id));b.append(node('small','打开结果与下载文件'));dl.append(b);}if(!finished.length)dl.append(node('p','结果文件会随任务保存在这里。','subtle'));}
$('#go-home').onclick=e=>{e.preventDefault();action(showHome);};
$('#home-start').onclick=()=>{const prompt=$('#home-prompt').value;showNew();$('#create-form [name=prompt]').value=prompt;};
$('#home-guide').onclick=()=>{$('#home-start').click();};$('#home-cases').onclick=()=>action(showPapers);
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
async function refreshDiscussion(){if(!current)return;const id=current.id;const result=await api(`/api/tasks/${id}/discussion`);if(current?.id!==id)return;const box=$('#discussion-history');box.replaceChildren();for(const m of result.messages){const row=node('article',undefined,'discussion-message');row.append(node('strong',m.question),node('p',m.answer||'请求状态待核对，未重复发送。'),node('small',m.provider+' / '+m.model+' · '+new Date(m.at).toLocaleString('zh-CN')));box.append(row);}$('#discussion-status').textContent=result.enabled?'请先保存模型连接。发送后会保留问题、答复和模型用量；当前助手可解读结果，不能执行新的计算或任意分析代码。':'结果助手尚未启用。可先下载数据或配置模型。';}
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

for(const button of document.querySelectorAll("[data-resource]"))button.prepend(uiIcon(button.dataset.resource==='paper'?'cases':button.dataset.resource==='tools'?'help':'resources'));

function renderTargetPlanning() {
  const box=$('#target-planning');box.replaceChildren();box.hidden=current.mode!=='reproduction';
  if(box.hidden)return;
  box.append(node('h2','先选复现目标'),node('p','逐项查看论文图表、工况与所需数据；同一工况的多个图表共用计算。'));
  const source=current.target_inventory, frozen=current.status==='conditions_frozen';
  if(!source){box.append(node('p',frozen?'此历史任务没有事前图表计划；原有验收与运行记录保留。':'文献图表清单尚未整理完成，暂不能冻结复现任务。'));return;}
  box.append(node('h3',source.paper.title),node('p','DOI: '+source.paper.doi),node('p',source.coverage_note,'subtle'));
  const names={retained_data:'已有数据可分析',new_calculation:'需要新增计算',missing_resources:'缺少资源',not_simulation:'资料核对项目',unresolved:'方法或工况待核对'};
  const selected=new Set(current.target_selection?.selected_ids||[]),checks=[];
  for(const row of source.targets){
    const item=node('details',undefined,'target-option'),head=node('summary');
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=selected.has(row.id);checkbox.disabled=frozen;checkbox.setAttribute('aria-label','选择 '+row.label);checkbox.onclick=e=>e.stopPropagation();
    head.append(checkbox,document.createTextNode(row.label+' · '+names[row.availability]));item.append(head);
    for(const [label,value] of [['出处',row.locator],['工况',row.conditions],['同工况分组',row.condition_group],['资源',row.resources],['输出',row.outputs],['采样',row.sampling],['分析',row.analysis],['比较标准',row.criterion||'尚未确定，不能冻结'],['限制',row.limitations]])item.append(node('p',label+'：'+value));
    box.append(item);checks.push([row.id,checkbox]);
  }
  const label=node('label','本次范围与未选目标说明'),reason=document.createElement('textarea');reason.rows=2;reason.maxLength=2000;reason.value=current.target_selection?.exclusion_reason||'';reason.disabled=frozen;label.append(reason);box.append(label);
  if(!frozen){const save=node('button','保存目标选择','primary');save.onclick=()=>action(async()=>{current=await api(`/api/tasks/${current.id}/targets`,{revision:current.revision,selected_ids:checks.filter(([,c])=>c.checked).map(([id])=>id),exclusion_reason:reason.value});await afterChange('目标范围已保存；比较标准与条件齐全后才能冻结。');});box.append(save);}
  box.append(node('p','B 每轮评测最多提交两次；保存或选择不提交计算。缺项或未定比较标准不能冻结。','form-note'));
}
