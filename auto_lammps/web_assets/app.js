'use strict';
const $ = (selector) => document.querySelector(selector);
const origins = {user:'用户明确指定',paper:'论文提供',code:'作者代码提供',proposed:'建议 · 待用户确认'};
const statuses = {missing:'缺失',unselected:'待选择',conflict:'有矛盾',pending:'待确认',confirmed:'已确认'};
let schema, current = null, editing = null, resolving = null, busy = false;
let literaturePreview = null;
let paperFilter='all';
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
  $('#task-count').textContent = result.tasks.length;
  $('#task-list').replaceChildren();
  if (!result.tasks.length) $('#task-list').append(node('p','还没有任务。','subtle'));
  for (const task of result.tasks) {
    const button = node('button',task.title, current?.id === task.id ? 'active' : '');
    button.append(node('small',task.status === 'conditions_frozen' ? '条件已冻结 · 查看历史' : `${task.outstanding} 项待处理`));
    button.onclick = () => action(() => openTask(task.id));
    $('#task-list').append(button);
  }
}
async function openTask(id) {
  current = await api('/api/tasks/'+id);
  history.replaceState(null, '', '#'+id);
  render();
  window.scrollTo({top:0});
  await listTasks();
  await renderHistory();
}
function showNew() {
  if (busy) return;
  $('#show-papers').removeAttribute('aria-current');
  current = null;
  history.replaceState(null, '', location.pathname);
  $('#welcome').hidden = false;
  $('#papers-view').hidden = true;
  $('#task-view').hidden = true;
  $('#message').hidden = true;
  action(listTasks);
  $('#create-form input').focus();
  window.scrollTo({top:0});
}
function conditionStatus(field) {
  if (!field.candidates.length) return 'missing';
  if (!field.selected) return field.candidates.length > 1 ? 'conflict' : 'unselected';
  return field.confirmed ? 'confirmed' : 'pending';
}
function render() {
  $('#show-papers').removeAttribute('aria-current');
  $('#welcome').hidden = true;
  $('#task-view').hidden = false;
  $('#papers-view').hidden = true;
  $('#task-title').textContent = current.title;
  $('#task-prompt').textContent = current.prompt;
  const frozen = current.status === 'conditions_frozen';
  $('#import-literature').hidden = frozen;
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
  const {events} = await api(`/api/tasks/${current.id}/history`);
  const labels = {created:'建立任务',candidate_added:'补充条件证据',condition_selected:'选择条件',user_confirmed:'确认条件',conditions_frozen:'冻结条件',literature_imported:'导入文献条件',conditions_generated:'模型整理条件'};
  $('#history-list').replaceChildren();
  for (const item of events) {
    const [kind,fields] = item.event.split(':');
    const details = fields ? ' · '+fields.split(',').map(key=>schema.fields[key]||key).join('、') : '';
    $('#history-list').append(node('li',`版本 ${item.revision} · ${labels[kind] || kind}${details} · ${new Date(item.at).toLocaleString('zh-CN')}`));
  }
}
async function afterChange(message, field) {
  render(); await listTasks(); await renderHistory(); notice(message);
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
    current=await api('/api/tasks',Object.fromEntries(form));
    history.replaceState(null,'','#'+current.id);
    await afterChange('任务已保存。已整理的标签行仍需确认；没有推断其他条件。');
    event.target.reset();
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
  $('#model-create-note').textContent=schema.model_calls_enabled ? '保存后将使用 DeepSeek 整理这段需求。仅发送原始描述，不自动提交计算。' : '模型尚未启用或额度已用完。需求会先保存；明确标注的条件可按原文整理。';
}
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
  $('#literature-form .dialog-error').hidden=true; $('#literature-dialog').showModal();
};
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
  else await listTasks();
});

const requestStates={reserved:'已预留，未派发',dispatching:'派发中',unknown:'提交结果不明，先对账',accepted:'调度器已接受',pending:'排队中',running:'运行中',completed:'计算已结束，尚未科学核验',failed:'计算失败',cancelled:'已取消',timeout:'已超时',rejected:'提交被拒',cancel_requested:'已请求取消',cancelled_before_dispatch:'派发前取消'};
const paperEvents={candidate_added:'登记候选文献',paper_selected:'选入复现计划',task_linked:'关联条件任务',evaluation_linked:'关联不可重置的评测账本'};
const roleNames={agent:'主 Agent 正式评测',reference:'作者参考运行',development:'开发验证',analysis:'分析作业'};
async function showPapers() {
  $('#show-papers').setAttribute('aria-current','page');
  current=null; history.replaceState(null,'','#papers');
  $('#welcome').hidden=true; $('#task-view').hidden=true; $('#papers-view').hidden=false;
  await refreshPapers(); window.scrollTo({top:0});
}
async function refreshPapers() {
  const expanded=new Set([...document.querySelectorAll('[data-paper-history][open]')].map(d=>d.dataset.paperHistory));
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
  $('#paper-updated').textContent='最近读取：'+new Date().toLocaleString('zh-CN')+' · 仅在此页面每 15 秒读取记录';
}
function paperCard(p,statuses,availableTasks) {
  const card=node('article',undefined,'paper-card');
  const top=node('div',undefined,'paper-card-top');
  const link=node('a',p.title); link.href=p.doi_url; link.target='_blank'; link.rel='noopener noreferrer';
  top.append(link,node('span',p.selection==='candidate'?'候选 · 尚未选定':statuses[p.status],'badge '+(p.status==='reproduced'?'confirmed':'pending')));
  card.append(top,node('p','DOI '+p.doi,'subtle'),node('p','拟复现范围：'+p.scope),node('p',p.note,'paper-note'),node('p',p.stage,'subtle'));
  if (p.selection==='candidate') {
    const select=node('button','加入复现计划','quiet');
    select.setAttribute('aria-label','选定文献：'+p.title);
    select.onclick=()=>action(async()=>{await api(`/api/papers/${p.id}/select`,{revision:p.revision});await refreshPapers();notice('已加入复现计划。任务条件与评分仍需冻结；没有提交计算。');});
    card.append(select);
  }
  const details=node('details',undefined,'paper-history'); details.dataset.paperHistory=p.id; details.append(node('summary','历史记录与提交次数'));
  const timeline=node('ol');
  for (const e of p.history) timeline.append(node('li',`${new Date(e.at).toLocaleString('zh-CN')} · ${paperEvents[e.event.split(':')[0]]||e.event} · 文献版本 ${e.revision}`));
  details.append(timeline);
  for (const task of p.tasks) {
    const button=node('button','查看条件：'+task.title,'quiet'); button.onclick=()=>action(()=>openTask(task.id)); details.append(button);
    const revisions=node('ol');
    for (const e of task.history) {
      const [kind,field]=e.event.split(':');
      const label={created:'建立任务',candidate_added:'补充条件',condition_selected:'选择条件',user_confirmed:'确认条件',conditions_frozen:'冻结条件',literature_imported:'导入文献条件'}[kind]||kind;
      revisions.append(node('li',`${new Date(e.at).toLocaleString('zh-CN')} · 条件版本 ${e.revision} · ${label}${field?' · '+field.split(',').map(k=>schema.fields[k]||k).join('、'):''}`));
    }
    details.append(revisions);
  }
  if (!p.evaluations.length) details.append(node('p','尚未关联正式评测，暂无提交次数记录。默认上限为每次独立评测 2 次；候选筛选和条件修订不算计算提交。','subtle'));
  for (const evaluation of p.evaluations) {
    if (!evaluation.available) { details.append(node('p','账本记录暂不可读；次数未知，不能重新提交。','error')); continue; }
    details.append(node('h3',roleNames[evaluation.identity.role]||evaluation.identity.role));
    details.append(node('p',`额度已占用 ${evaluation.reserved_attempts} / ${evaluation.max_attempts} · 已记账派发 ${evaluation.dispatch_claims} · 剩余 ${evaluation.remaining_attempts}`));
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
  card.append(details); return card;
}
setInterval(()=>{if(location.hash==='#papers'&&!busy&&!document.querySelector('dialog[open]')&&!$('#papers-view').contains(document.activeElement))action(refreshPapers);},15000);
