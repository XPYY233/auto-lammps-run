'use strict';
const $ = (selector) => document.querySelector(selector);
const origins = {user:'用户明确指定',paper:'论文提供',code:'作者代码提供',proposed:'建议 · 待用户确认'};
const statuses = {missing:'缺失',unselected:'待选择',conflict:'有矛盾',pending:'待确认',confirmed:'已确认'};
let schema, current = null, editing = null, resolving = null, busy = false;
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
    button.append(node('small',task.status === 'conditions_frozen' ? '条件已冻结 · 未提交' : `${task.outstanding} 项待处理`));
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
  current = null;
  history.replaceState(null, '', location.pathname);
  $('#welcome').hidden = false;
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
  $('#welcome').hidden = true;
  $('#task-view').hidden = false;
  $('#task-title').textContent = current.title;
  $('#task-prompt').textContent = current.prompt;
  const frozen = current.status === 'conditions_frozen';
  $('#task-status').textContent = frozen ? '条件已冻结' : '条件草稿';
  $('#task-meta').textContent = `${current.mode === 'reproduction' ? '文献复现' : '开放研究'} · 版本 ${current.revision} · 更新于 ${new Date(current.updated_at).toLocaleString('zh-CN')}`;
  const all = Object.values(current.fields);
  $('#confirmed-count').textContent = `${all.filter(f=>f.confirmed).length} / ${all.length}`;
  $('#conflict-count').textContent = all.filter(f=>conditionStatus(f)==='conflict').length;
  $('#conditions').replaceChildren();
  let index = 0;
  for (const [key,label] of Object.entries(schema.fields)) {
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
  $('#freeze-heading').textContent = frozen ? '这份条件已锁定，修订证据已保留。' : '保存这份确定的研究条件';
  $('#freeze-note').textContent = frozen ? '可导出本人的条件审阅记录。它不是主 Agent 的隔离任务包，也不授权执行计算。' : `还有 ${current.issues.length} 项需要处理。冻结后不可覆盖；不会自动生成脚本或提交计算。`;
}
async function renderHistory() {
  const {events} = await api(`/api/tasks/${current.id}/history`);
  const labels = {created:'建立任务',candidate_added:'补充条件证据',condition_selected:'选择条件',user_confirmed:'确认条件',conditions_frozen:'冻结条件'};
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
action(async()=>{
  schema=await api('/api/schema');
  if (/^#[a-f0-9]{32}$/.test(location.hash)) await openTask(location.hash.slice(1));
  else await listTasks();
});
