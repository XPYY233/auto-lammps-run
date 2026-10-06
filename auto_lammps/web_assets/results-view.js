/* Ordinary research results: only task-bound, verified output previews. */
const sourceTableCache=new Map();
const numberText=value=>Number.isFinite(value)?Number(value.toPrecision(8)).toString():'—';
function equationText(text){
  if(/\\\(|\$/.test(text))return text;
  const start=text.search(/\b[A-Za-z]+[_^](?:\{[^}]+\}|[A-Za-z0-9]+)\s*(?:\([^)]*\))?\s*=/);
  return start<0?text:text.slice(0,start)+'\\('+text.slice(start)+'\\)';
}
function sourceTable(table){
  const wrap=node('div',undefined,'comparison-scroll'),grid=node('table',undefined,'result-table'),head=node('tr'),body=node('tbody');
  for(const c of table.columns)head.append(node('th',`${c.name} (${c.unit})`));
  const thead=node('thead');thead.append(head);grid.append(thead,body);
  for(const values of table.rows){const row=node('tr');for(const v of values)row.append(node('td',numberText(v)));body.append(row);}
  wrap.append(grid);return wrap;
}
function numericalPlot(table,operation){
  const names=table.columns.map(c=>c.name),xi=names.indexOf(operation.x),yi=names.indexOf(operation.y);
  const points=table.rows.filter(r=>r[xi]>=operation.window[0]&&r[xi]<=operation.window[1]).map(r=>[r[xi],r[yi]]);
  if(!points.length)return node('p','预览中没有此区间的数据。完整选取范围见分析报告。');
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('class','plot-svg');svg.setAttribute('viewBox','0 0 620 355');svg.setAttribute('role','img');svg.setAttribute('aria-label',`${operation.y} 随 ${operation.x} 变化，真实数据与冻结拟合`);
  const add=(tag,attrs,text)=>{const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;svg.append(el);return el;};
  let xmin=Math.min(...points.map(p=>p[0])),xmax=Math.max(...points.map(p=>p[0])),ymin=Math.min(...points.map(p=>p[1])),ymax=Math.max(...points.map(p=>p[1]));
  const fit=operation.method==='linear_fit'&&Number.isFinite(operation.values.slope)&&Number.isFinite(operation.values.intercept);
  if(fit){ymin=Math.min(ymin,operation.values.intercept);ymax=Math.max(ymax,operation.values.intercept);xmin=Math.min(xmin,0);xmax=Math.max(xmax,0);}
  if(xmin===xmax){xmin-=.5;xmax+=.5;}if(ymin===ymax){ymin-=.5;ymax+=.5;}
  const dy=(ymax-ymin)*.12;ymin-=dy;ymax+=dy;
  const X=x=>82+(x-xmin)/(xmax-xmin)*500,Y=y=>285-(y-ymin)/(ymax-ymin)*230;
  for(let i=0;i<=4;i++){const x=xmin+(xmax-xmin)*i/4,y=ymin+(ymax-ymin)*i/4;add('line',{x1:X(x),x2:X(x),y1:55,y2:285,stroke:'#e4e9f1'});add('line',{x1:82,x2:582,y1:Y(y),y2:Y(y),stroke:'#e4e9f1'});add('text',{x:X(x),y:307,'text-anchor':'middle',fill:'#566479','font-size':11},numberText(x));add('text',{x:72,y:Y(y)+4,'text-anchor':'end',fill:'#566479','font-size':11},numberText(y));}
  const xu=table.columns[xi].unit,yu=table.columns[yi].unit;
  add('text',{x:332,y:339,'text-anchor':'middle',fill:'#20304b','font-size':13},`${operation.x} (${xu})`);
  add('text',{x:18,y:170,transform:'rotate(-90 18 170)','text-anchor':'middle',fill:'#20304b','font-size':13},`${operation.y} (${yu})`);
  if(fit)add('line',{x1:X(xmin),y1:Y(operation.values.slope*xmin+operation.values.intercept),x2:X(xmax),y2:Y(operation.values.slope*xmax+operation.values.intercept),stroke:'#952459','stroke-width':2,'stroke-dasharray':'7 5'});
  if(!fit)add('polyline',{points:points.map(p=>`${X(p[0])},${Y(p[1])}`).join(' '),fill:'none',stroke:'#245dc8','stroke-width':2});
  for(const p of points)add('circle',{cx:X(p[0]),cy:Y(p[1]),r:4,fill:'#245dc8',stroke:'white','stroke-width':1});
  add('text',{x:82,y:27,fill:'#245dc8','font-size':12},'● 原始数据');if(fit)add('text',{x:215,y:27,fill:'#952459','font-size':12},'– – 冻结区间拟合（含外推）');
  return svg;
}
function renderOrdinaryResults(box){
  const valid=[];
  for(const group of normalResult.evaluations)for(const request of group.requests)for(const report of request.reports)if(report.status==='analyzed')valid.push({request,report});
  if(resultTab==='history'){
    for(const group of normalResult.evaluations){box.append(node('h3',`提交历史 · ${group.dispatch_count} / ${group.max_attempts}`));for(const r of group.requests){box.append(node('h4',r.job_id?`作业 ${r.job_id}`:r.state_label),node('p',r.stage));const ol=node('ol',undefined,'task-timeline');for(const e of r.history){const li=node('li');li.append(node('strong',e.label),node('small',new Date(e.at).toLocaleString('zh-CN')));ol.append(li);}box.append(ol);}}return;
  }
  if(['structure','trajectory','targets'].includes(resultTab)){box.append(emptyState('此视图尚无可显示的产物','可下载原始输出；当前数值报告未提供此类型的可视化。'));return;}
  if(!valid.length){box.append(emptyState('尚无可核验的数值结果','计算、回收与分析状态分别保存；请查看历史记录与右侧原始下载。'));return;}
  for(const {request,report} of valid){
    box.append(node('h3',`${equationText(report.quantity)} · 作业 ${request.job_id}`),node('p','数值处理完成 · 科学结论尚待核验','badge pending'));
    if(resultTab==='report'){box.append(resultReport(report,current.id));continue;}
    const area=node('div',undefined,'numeric-result-area');box.append(area);
    const key=current.id+':'+report.id+':'+workspaceState.updated;
    let cached=sourceTableCache.get(key);
    if(!cached){cached={state:'loading'};sourceTableCache.set(key,cached);const task=current.id;
      api(`/api/tasks/${task}/results/${report.id}/tables`).then(value=>{cached.state='ready';cached.value=value;if(current?.id===task)renderWorkspaceResults();}).catch(()=>{cached.state='error';if(current?.id===task)renderWorkspaceResults();});}
    if(cached.state!=='ready'){area.append(node('p',cached.state==='error'?'数据来源核验未通过，未展示图表；可查看报告与历史。':'正在核对原始数据…','form-note'));continue;}
    const tables=cached.value.tables;
    if(resultTab==='data'||resultTab==='overview')for(const table of tables){area.append(node('h4',`原始数值 · ${table.file}`),sourceTable(table),node('p',`${table.total_rows} 行；${table.sampled?'等间隔预览 128 行，包含首尾；完整数据在右侧下载':'展示全部数据'}。单位来自冻结分析计划。`,'plot-caption'));}
    if(resultTab==='plots'||resultTab==='overview'){
      const label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择计算结果图表');report.results.forEach((op,i)=>select.append(new Option(`${op.y} 对 ${op.x} · ${analysisMethods[op.method]||op.method}`,String(i))));label.append(select);area.append(label);const chart=node('article',undefined,'result-widget');area.append(chart);
      const draw=()=>{const op=report.results[Number(select.value)],table=tables.find(t=>t.file===op.file);chart.replaceChildren();if(!table){chart.append(node('p','此图的数据未通过来源核验。'));return;}chart.append(numericalPlot(table,op),node('p',`${op.sample_count} 行参与原有分析；图中${table.sampled?'数据为预览采样':'展示原始点'}。选取区间：${op.window.join(' 至 ')}。`,'plot-caption'));if(op.method==='linear_fit')chart.append(node('p',`\\(y=${numberText(op.values.slope)}x+${numberText(op.values.intercept)}\\)；\\(R^2=${numberText(op.values.r_squared)}\\)。外推值不是新增计算。`));};select.onchange=draw;draw();
    }
  }
}
