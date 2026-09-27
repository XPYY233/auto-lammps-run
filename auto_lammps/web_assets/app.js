'use strict';
const $ = (selector) => document.querySelector(selector);
const origins = {user:'用户明确指定',paper:'论文提供',code:'作者代码提供',proposed:'建议 · 待用户确认'};
const statuses = {missing:'缺失',unselected:'待选择',conflict:'有矛盾',pending:'待确认',confirmed:'已确认'};
let schema, current = null, editing = null, resolving = null, busy = false;
let literaturePreview = null;
let paperFilter='all';
let taskCache=[], workspaceReport=null, resultTab='overview', modelPreference=null, normalResult=null;
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
  normalResult=null;workspaceReport=null;
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
}
function showNew() {
  if (busy) return;
  $('#show-papers').removeAttribute('aria-current');
  current = null;
  history.replaceState(null, '', location.pathname);
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
  $('#show-papers').removeAttribute('aria-current');
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
  $('#freeze').disabled = !!current.issues.length;
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
  const labels = {created:'建立任务',candidate_added:'补充条件证据',condition_selected:'选择条件',user_confirmed:'确认条件',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件',reference_evidence_generated:'整理文献证据'};
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
$('#new-task').onclick=showNew;
$('#show-papers').onclick=()=>action(showPapers);
$('#close-dialog').onclick=()=>$('#condition-dialog').close();
$('#cancel-resolve').onclick=()=>$('#resolve-dialog').close();
$('#create-form').onsubmit=(event)=>{
  event.preventDefault();
  action(async()=>{
    const form=new FormData(event.target);
    const data=Object.fromEntries(form);
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
  $('#model-state').textContent=schema.model_calls_enabled ? '模型条件整理已启用；尚不提交作业。' : '模型尚未启用或额度已用完；不提交作业。';
  $('#model-create-note').textContent=schema.model_calls_enabled ? '保存后将使用已配置模型整理需求，保留原文依据。' : '需求会先保存。模型尚未连接或额度已用完，当前不提交作业。';
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
  const available=schema.candidate_preparation || {enabled:false,reason:'方案准备服务尚未配置。'};
  $('#prepare-candidate').hidden=!!candidate || current.mode!=='research';
  $('#prepare-candidate').disabled=!available.enabled;
  $('#candidate-stage').textContent=candidate?.label || '尚未准备方案';
  $('#candidate-note').textContent=candidate ? '更新于 '+new Date(candidate.updated_at).toLocaleString('zh-CN') :
    current.mode==='reproduction' ? '文献测试任务仍需核对输入发布与访问隔离，暂不生成方案。' :
    available.enabled ? '根据已确认条件生成结构、势函数调用与计算输入；可关闭页面，稍后查看进度。' : available.reason;
  const summary=$('#candidate-summary'); summary.replaceChildren(); $('#candidate-downloads').replaceChildren();
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
  try { await api(`/api/tasks/${id}/candidate`,{revision:current.revision}); }
  finally { await refreshModelStatus(); await refreshCandidate(); await renderHistory(); }
  notice('方案准备已记录，可以稍后返回查看进度。');
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
  else {await listTasks();selectNavigation('new');}
});

const requestStates={reserved:'已预留，未派发',dispatching:'派发中',unknown:'提交结果不明，先对账',accepted:'调度器已接受',pending:'排队中',running:'运行中',completed:'计算已结束，尚未科学核验',failed:'计算失败',cancelled:'已取消',timeout:'已超时',rejected:'提交被拒',cancel_requested:'已请求取消',cancelled_before_dispatch:'派发前取消'};
const paperEvents={bibliography_corrected:'核正论文题目与出版信息',candidate_added:'登记候选文献',paper_selected:'选入复现计划',task_linked:'关联条件任务',evaluation_linked:'关联不可重置的评测账本'};
const roleNames={agent:'主 Agent 正式评测',reference:'作者参考运行',development:'开发验证',analysis:'分析作业'};
async function showPapers() {
  hideViews('papers-view');selectNavigation('papers');
  $('#show-papers').setAttribute('aria-current','page');
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
  const selected=result.papers.filter(p=>p.selection==='selected'&&(paperFilter==='all'||p.status===paperFilter));
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
      const label={created:'建立任务',candidate_added:'补充条件',condition_selected:'选择条件',user_confirmed:'确认条件',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件',reference_evidence_generated:'整理文献证据'}[kind]||kind;
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
function hideViews(show){for(const id of ['welcome','tasks-view','papers-view','resources-view','help-view','task-view']) $('#'+id).hidden=id!==show;}
function selectNavigation(name){
  for(const [id,key] of Object.entries({'top-new':'new','top-tasks':'tasks','top-papers':'papers','top-resources':'resources','top-help':'help','show-tasks':'tasks','show-papers':'papers','show-resources':'resources','show-help':'help'})){
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
  const box=$('#task-cards');box.replaceChildren();
  const tasks=taskCache.filter(t=>t.title.toLowerCase().includes(($('#task-search').value||'').toLowerCase()));
  if(!tasks.length){box.append(emptyState('暂无任务','新建一项研究，或调整搜索词。'));return;}
  for(const t of tasks){const card=node('article',undefined,'task-card');card.append(node('span',t.mode==='reproduction'?'文献复现验证':'科研计算','badge pending'),node('h2',t.title),node('p',t.reference_stage || (t.status==='conditions_frozen'?'研究条件已保存，可继续查看方案与结果。':'需求与已有证据已保存。进度以任务内记录为准。')));const go=node('button','打开任务 →','text-button');go.onclick=()=>action(()=>openTask(t.id));card.append(go);box.append(card);}
}
async function showTasks(){current=null;hideViews('tasks-view');selectNavigation('tasks');history.replaceState(null,'','#tasks');await listTasks();taskCards();}
async function showResources(){
  current=null;hideViews('resources-view');selectNavigation('resources');history.replaceState(null,'','#resources');
  const result=await api('/api/papers'),box=$('#resource-cards');box.replaceChildren();
  for(const p of result.papers){if(!p.potential_acquisition&&!p.source_discovery)continue;const card=node('article',undefined,'task-card');card.append(node('span','有来源记录','badge pending'),node('h2',p.title));const doi=node('a','DOI '+p.doi);doi.href=p.doi_url;doi.target='_blank';doi.rel='noopener noreferrer';card.append(doi);for(const f of p.potential_acquisition?.files||[]){const a=node('a',f.path,'resource-link');a.href=f.source_url;a.target='_blank';a.rel='noopener noreferrer';a.append(node('small',`${f.size} 字节 · 已保存来源与文件摘要`));card.append(a);}card.append(node('p','科学适用性与许可按任务核验，不能仅按元素名称选用。'));box.append(card);}
  if(!box.children.length)box.append(emptyState('暂无已登记资源','势函数与论文来源会随研究保存到这里。'));
}
function showHelp(){current=null;hideViews('help-view');selectNavigation('help');history.replaceState(null,'','#help');}
function emptyState(title,message){const box=node('div',undefined,'empty-result');box.append(node('span','◇','empty-icon'),node('h3',title),node('p',message));return box;}
function number(v,digits=4){return Number.isFinite(v)?v.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:0}):'—';}
function duration(seconds){const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=Math.floor(seconds%60);return `${h} 小时 ${m} 分 ${s} 秒`;}
function addInfo(label,value){$('#task-information').append(node('dt',label),node('dd',value));}
function bState(report){return report.agent_progress?.stage || '记录暂不可核验';}
function bCount(report){return report.agent_progress?.available ? `${report.agent_progress.dispatch_claims} / 2` : '暂不可核验';}
function renderFlow(report){
 const box=$('#execution-flow');box.replaceChildren();const head=node('div',undefined,'flow-heading');head.append(node('h2',report?'作者参考 A · 执行流程':'任务执行流程'),node('span',report?'完整运行 '+duration(report.runtime.elapsed_seconds):'等待模型与执行服务就绪'));box.append(head);
 const labels=['需求理解','结构准备','势函数','计算脚本','提交 HPC','运行结束','结果分析'];const steps=node('ol',undefined,'flow-steps');
 for(let i=0;i<labels.length;i++){const done=!!report&&report.stages[i]?.state==='completed';const li=node('li',undefined,done?'done':i===0?'current':'');li.append(node('span',done?'✓':String(i+1)),document.createTextNode(labels[i]));steps.append(li);}box.append(steps,node('p',report?`参考 A 已结束并完成诊断分析；B：${bState(report)}。科学结论仍待核验。`:'需求已保存。缺项会集中说明；当前不会自动提交计算。','flow-note'));
}
function metricTable(metrics,compact=false){
 const table=node('table',undefined,'result-table'),head=node('thead'),tr=node('tr'),body=node('tbody');
 for(const label of compact?['性质','P','A','差值']:['性质 / 方法','P','A','abs(P−A)','相对差'])tr.append(node('th',label));head.append(tr);table.append(head,body);
 for(const m of metrics){const row=node('tr'),name=node('td',m.label,'metric-name');if(!compact)name.append(node('small',m.method));row.append(name,node('td',number(m.paper),'number'),node('td',number(m.reference),'number'),node('td',number(m.absolute_difference),'number'));if(!compact)row.append(node('td',m.relative_difference_percent===null?'未定义':number(m.relative_difference_percent,3)+'%','number'));body.append(row);}return table;
}
function curvePlot(report){
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 450 290');svg.setAttribute('class','plot-svg');svg.setAttribute('role','img');svg.setAttribute('aria-label','真实 A 应力应变曲线，两种应力口径分别显示');
 const el=(tag,attrs,text)=>{const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;svg.append(e);return e;};
 const curves=report.curves,ys=curves.flatMap(c=>c.points.map(p=>p[1])),max=Math.ceil(Math.max(...ys,1)/2)*2,low=Math.min(0,Math.floor(Math.min(...ys))),left=52,right=430,top=18,bottom=244;
 const sx=x=>left+x/.5*(right-left),sy=y=>bottom-(y-low)/(max-low)*(bottom-top);
 for(let i=0;i<=5;i++){const x=i/10;el('line',{x1:sx(x),y1:top,x2:sx(x),y2:bottom,stroke:'#edf1f7'});el('text',{x:sx(x),y:bottom+18,'text-anchor':'middle',fill:'#8c9ab1','font-size':10},x.toFixed(1));}
 for(let i=0;i<=4;i++){const y=low+(max-low)*i/4;el('line',{x1:left,y1:sy(y),x2:right,y2:sy(y),stroke:'#e6edf7'});el('text',{x:left-10,y:sy(y)+4,'text-anchor':'end',fill:'#8c9ab1','font-size':10},number(y,1));}
 el('text',{x:240,y:284,'text-anchor':'middle',fill:'#677f9f','font-size':11},'应变');el('text',{x:15,y:140,transform:'rotate(-90 15 140)','text-anchor':'middle',fill:'#677f9f','font-size':11},'应力 (GPa)');
 curves.forEach((c,i)=>el('polyline',{points:c.points.map(p=>`${sx(p[0])},${sy(p[1])}`).join(' '),fill:'none',stroke:i?'#a5b2c6':'#2870ed','stroke-width':i?1.25:2,'stroke-linejoin':'round'}));return svg;
}
function limitations(report){const d=node('details',undefined,'result-limits');d.append(node('summary','查看结果限制与分析依据'));const ul=node('ul');for(const text of report.limitations)ul.append(node('li',text));d.append(ul);return d;}
function reportDownloads(report,box){
 for(const f of report.files.filter(f=>f.name!=='analysis.json')){const row=node('div',undefined,'file-row'),a=node('a',f.label+' ↓');a.href=`/api/tasks/${current.id}/reference-result/files/${f.name}`;row.append(a,node('small',number(f.size/1024,1)+' KB'));box.append(row);}
}
function taskTimeline(){const list=node('ol',undefined,'task-timeline');for(const item of $('#history-list').children)list.append(item.cloneNode(true));return list;}
function renderWorkspaceResults(){
 const box=$('#research-results');box.replaceChildren();const r=workspaceReport;
 if(resultTab==='history'&&!r){box.append(node('h3','任务历史'),taskTimeline());if(!normalResult?.evaluations?.length){box.append(node('p','尚无计算提交记录。','plot-caption'));return;}}
 if(!r && normalResult?.evaluations?.length){for(const group of normalResult.evaluations){box.append(node('h3',`计算记录 · ${group.dispatch_count} / ${group.max_attempts} 次提交`));for(const request of group.requests){box.append(node('p',request.stage));for(const report of request.reports)box.append(resultReport(report,current.id));}}return;}
 if(!r){const empty=emptyState('还没有计算结果',current.status==='conditions_frozen'?'研究条件已保存。方案与计算记录会在服务就绪后显示。':'研究需求已保存。模型连接后将整理条件、准备方案，计算完成后在这里查看结果。');const button=node('button','查看模型设置','quiet');button.onclick=()=>action(openModel);empty.append(button);box.append(empty);return;}
 if(resultTab==='overview'){
  const grid=node('div',undefined,'result-grid'),values=node('article',undefined,'result-widget'),plot=node('article',undefined,'result-widget');values.append(node('h3','P–A 关键结果 · GPa'),metricTable(r.metrics.slice(0,3),true),node('p','模量两行分别使用应变 0–0.05、0–0.06，尚未冻结唯一论文评分口径。','plot-caption'));plot.append(node('h3','应力–应变曲线'),curvePlot(r));const legend=node('div',undefined,'plot-legend');for(const c of r.curves)legend.append(node('span',c.label));plot.append(legend);grid.append(values,plot);box.append(grid);const summary=node('div',undefined,'research-summary');summary.append(node('h3','结果说明'),node('p',r.summary));box.append(summary,limitations(r));
 }else if(resultTab==='data'){
  box.append(node('h3','论文 P 与作者参考 A 的数值比较'));const scroll=node('div',undefined,'comparison-scroll');scroll.append(metricTable(r.metrics));box.append(scroll,node('p','单位均为 GPa；相对差 = abs(P−A) / abs(P)。本表为参考 A 的诊断结果。','plot-caption'),limitations(r));
 }else if(resultTab==='plots'){
  box.append(node('h3','作者参考 A · 完整拉伸曲线'),curvePlot(r));const legend=node('div',undefined,'plot-legend');for(const c of r.curves)legend.append(node('span',c.label));box.append(legend,node('p','曲线来自本次真实输出。P 当前只有表格指标，未伪造论文曲线。','plot-caption'));
 }else if(resultTab==='structure'||resultTab==='trajectory'){
  box.append(emptyState(resultTab==='structure'?'原子结构视图待接入':'轨迹播放待接入','真实结构与轨迹已保留在超算。OVITO 分析和网页交互尚未接入，本页不以示意图替代计算结果。'));
 }else if(resultTab==='report'){
  const article=node('article',undefined,'report-text');article.append(node('h3','作者参考 A · 诊断报告'),node('p',r.title,'paper-title'));const link=node('a','DOI '+r.doi);link.href='https://doi.org/'+r.doi;link.target='_blank';link.rel='noopener noreferrer';article.append(link,node('p',r.scope),node('p',r.summary),limitations(r));const linkReport=node('a','下载完整分析报告 ↓','quiet');linkReport.href=`/api/tasks/${current.id}/reference-result/files/report.md`;article.append(linkReport);box.append(article);
 }else{
  box.append(node('h3','真实提交记录'));const list=node('ol',undefined,'timeline');for(const [i,q] of r.evaluation.requests.entries()){const li=node('li');li.append(node('strong',`A 第 ${i+1} 次 · ${requestStates[q.state]||q.state}`),node('small',`作业 ${q.job_id||'未确认'} · ${q.accounted?number(q.actual_core_seconds/3600,4)+' 核时':'尚待核算'}`));const events=node('details');events.append(node('summary','展开本次记录'));const history=node('ol');for(const e of q.events)history.append(node('li',`${new Date(e.at*1000).toLocaleString('zh-CN')} · ${{reserved:'预留资源',dispatch_intent:'发起提交',scheduler_accepted:'调度器接受',scheduler_observed:'核对计算状态',accounting_final:'核算真实用量'}[e.kind]||'保存运行记录'}`));events.append(history);li.append(events);list.append(li);}box.append(list,node('p',`B 已用 ${bCount(r)} 次；${bState(r)}。作者参考 A 的失败不从历史移除。`,'plot-caption'),node('h3','任务与条件历史'),taskTimeline());
 }
}
async function refreshWorkspace(){
 if(!current)return;const id=current.id;workspaceReport=null;renderFlow(null);renderWorkspaceResults();
 try{const result=await api(`/api/tasks/${id}/reference-result`);if(current?.id!==id)return;workspaceReport=result.report;}catch(error){if(current?.id===id)notice('参考报告暂不可核验，未显示旧数值。',true);}
 if(current?.id!==id)return;const r=workspaceReport;
 $('#task-tags').replaceChildren();
 if(r){$('#task-status').textContent='A 已结束 · B '+bState(r);$('#task-meta').textContent='文献复现验证 · '+r.scope;for(const label of ['作者原始流程','P–A 诊断已保存','科学结论待核验'])$('#task-tags').append(node('span',label,'tag'));}
 else{for(const key of ['material','temperature','potential']){const f=current.fields[key],c=f?.candidates.find(c=>c.id===f.selected);if(c)$('#task-tags').append(node('span',c.value+(c.unit?' '+c.unit:''),'tag'));}}
 renderFlow(r);$('#task-information').replaceChildren();$('#task-files').replaceChildren();$('#task-resources').replaceChildren();
 addInfo('状态',r?'A 已结束，B '+bState(r):current.status==='conditions_frozen'?'条件已保存':'需求已保存');addInfo('记录更新',new Date(current.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}));
 if(r){addInfo('A 运行时长',duration(r.runtime.elapsed_seconds));addInfo('A 使用核数',String(r.runtime.cores));addInfo('A 提交次数',`${r.evaluation.dispatch_claims} 次（含失败）`);addInfo('B 提交次数',bCount(r));addInfo('A 本次核时',number(r.runtime.core_hours,3));addInfo('B 总核时上限',number(r.runtime.B_max_core_hours,3));addInfo('A 作业号',r.job_id);reportDownloads(r,$('#task-files'));const a=node('a','论文与 DOI ↗','resource-link');a.href='https://doi.org/'+r.doi;a.target='_blank';a.rel='noopener noreferrer';a.append(node('small',r.title));$('#task-resources').append(a);}
 else{$('#task-files').append(node('p','计算及分析产生的文件会保存在这里。','subtle'));addInfo('提交次数','尚无已核验记录');}
 for(const [label,url] of [['LAMMPS 使用文档','https://docs.lammps.org/'],['OVITO 分析工具','https://www.ovito.org/']]){const a=node('a',label+' ↗','resource-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';$('#task-resources').append(a);}
 $('#task-model').textContent=r?'第一周 · Codex 辅助验证':'应用模型尚未启用';
 const tabs=$('#result-tabs');tabs.replaceChildren();for(const [key,label] of Object.entries({overview:'结果总览',data:'关键数据',plots:'可视化图表',structure:'原子结构',trajectory:'轨迹动画',report:'分析报告',history:'历史记录'})){const b=node('button',label);b.setAttribute('role','tab');b.setAttribute('aria-selected',String(key===resultTab));b.onclick=()=>{resultTab=key;for(const x of tabs.children)x.setAttribute('aria-selected',String(x===b));renderWorkspaceResults();};tabs.append(b);}renderWorkspaceResults();
}
async function openModel(){modelPreference=await api('/api/model-preference');$('#provider-choice').value=modelPreference.provider;$('#model-name').value=modelPreference.model;$('#model-dialog .dialog-error').hidden=true;$('#model-dialog').showModal();}
$('#model-form').onsubmit=e=>{e.preventDefault();action(async()=>{modelPreference=await api('/api/model-preference',{provider:$('#provider-choice').value,model:$('#model-name').value.trim(),revision:modelPreference.revision});$('#model-dialog').close();$('#open-model').textContent=modelPreference.providers[modelPreference.provider]+' · 未连接';notice('模型偏好已保存；未发起 API 调用，也未更改已有任务。');});};
$('#close-model').onclick=()=>$('#model-dialog').close();for(const id of ['open-model','sidebar-model','rail-model'])$('#'+id).onclick=()=>action(openModel);
for(const id of ['show-tasks','top-tasks','back-tasks'])$('#'+id).onclick=()=>action(showTasks);
for(const id of ['top-new','tasks-new'])$('#'+id).onclick=showNew;
$('#top-papers').onclick=()=>action(showPapers);$('#pick-paper').onclick=()=>action(showPapers);
for(const id of ['top-resources','show-resources'])$('#'+id).onclick=()=>action(showResources);
for(const id of ['top-help','show-help'])$('#'+id).onclick=showHelp;
$('#mode-research').onclick=()=>setMode('research');$('#mode-reproduction').onclick=()=>setMode('reproduction');
$('#task-search').oninput=()=>{action(async()=>{await listTasks();if(!$('#tasks-view').hidden)taskCards();});};

$("#refresh-workspace").onclick=()=>action(async()=>{await refreshResults();await refreshWorkspace();notice("已读取最新记录，没有提交计算。");});
