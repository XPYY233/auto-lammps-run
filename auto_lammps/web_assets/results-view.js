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
  for(const values of table.rows){const row=node('tr');for(const v of values)row.append(node('td',typeof v==='string'?v:numberText(v)));body.append(row);}
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
function structuralPlot(result){
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
  svg.setAttribute('class','plot-svg');svg.setAttribute('viewBox','0 0 620 380');
  svg.setAttribute('role','img');svg.setAttribute('aria-label',`Warren–Cowley 短程有序参数：${result.chart.labels.map((label,i)=>label+' '+numberText(result.chart.values[i])).join('；')}`);
  const add=(tag,attrs,text)=>{const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;svg.append(el);return el;};
  let low=Math.min(0,...result.chart.values),high=Math.max(0,...result.chart.values);
  if(low===high){low-=.5;high+=.5;}
  const margin=(high-low)*.1;low-=margin;high+=margin;
  const Y=value=>285-(value-low)/(high-low)*230,step=500/result.chart.labels.length;
  for(let i=0;i<=4;i++){const y=low+(high-low)*i/4;add('line',{x1:82,x2:582,y1:Y(y),y2:Y(y),stroke:'#e4e9f1'});add('text',{x:72,y:Y(y)+4,'text-anchor':'end',fill:'#566479','font-size':11},numberText(y));}
  add('line',{x1:82,x2:582,y1:Y(0),y2:Y(0),stroke:'#536276','stroke-width':1.5});
  result.chart.labels.forEach((label,i)=>{const value=result.chart.values[i],x=82+step*(i+.5);add('rect',{x:x-step*.32,y:Math.min(Y(0),Y(value)),width:step*.64,height:Math.max(1,Math.abs(Y(value)-Y(0))),fill:'#245dc8'});add('text',{x,y:310,transform:`rotate(35 ${x} 310)`,'text-anchor':'start',fill:'#20304b','font-size':11},label);});
  add('text',{x:18,y:170,transform:'rotate(-90 18 170)','text-anchor':'middle',fill:'#20304b','font-size':13},'Warren–Cowley α (无量纲)');
  add('text',{x:82,y:27,fill:'#20304b','font-size':12},`元素对 · ${result.selected_frame_count} 个指定帧等权平均`);
  return svg;
}
function structuralTable(result,kind,frame){
  const symbols=result.source.elements,aggregate=frame===undefined;
  const names=aggregate?['元素 i','元素 j','α 平均值','样本标准差','最小值','最大值']:
    kind==='directed'?['中心元素 i','邻居元素 j','邻接计数','Nᵢ','Nⱼ','条件概率','组分 cⱼ','αᵢⱼ']:['元素 i','元素 j','对称 α'];
  const rows=(aggregate?result.aggregates[kind]:frame[kind]).map(row=>[symbols[String(row[0])],symbols[String(row[1])],...row.slice(2)]);
  return sourceTable({columns:names.map((name,i)=>({name,unit:i<2?'元素':!aggregate&&kind==='directed'&&i>=2&&i<=4?'个':'1'})),rows});
}
function structuralDetails(result,{plot=false,data=false}={}){
  const article=node('article',undefined,'result-widget');article.append(node('h4',`短程有序 · ${result.id}`),
    node('p',`${result.parameters.neighbors} 个最近邻；${result.selected_frame_count} / ${result.source_frame_count} 帧。选中帧：${result.frames.map(f=>`${f.frame}（步 ${f.timestep}）`).join('、')}。`,'plot-caption'));
  article.append(node('p','α 描述元素邻接偏离随机组分的程度。对称值为两个方向 αᵢⱼ 与 αⱼᵢ 的平均；保留同元素项。科学结论仍需核验。','form-note'));
  if(plot)article.append(structuralPlot(result));
  if(data){article.append(node('h5','元素对 · 等权帧平均（对称值）'),structuralTable(result,'symmetric'),
      node('p','标准差来自选定帧的波动；单帧无法估计标准差，显示为 —。这不是独立重复的不确定度。','plot-caption'));
    const directed=node('details');directed.append(node('summary','查看有方向的元素对平均值与每帧原始邻接计数'),
      structuralTable(result,'directed'));
    for(const frame of result.frames)directed.append(node('h5',`帧 ${frame.frame} · 步 ${frame.timestep} · ${frame.atom_count} 原子`),structuralTable(result,'directed',frame));
    article.append(directed);
  }
  const source=node('details',undefined,'result-source');source.append(node('summary','查看采样方法、原始轨迹与分析依据'),
    node('p',`轨迹：${result.file}；SHA-256：${result.source.sha256}`,'source-hash'),
    node('p',`OVITO ${result.ovito_version}；${result.parameters.neighbor_selection==='nearest_k'?'固定最近邻数（不自动证明物理第一壳层）':`有界壳层：最大距离 ${result.parameters.max_distance} Å，最小间隙 ${result.parameters.shell_gap} Å`}。三维周期边界；元素映射 ${Object.entries(result.source.elements).map(([kind,symbol])=>`${kind}=${symbol}`).join('，')}。`),
    node('p','αᵢⱼ = 1 − nᵢⱼ / [k Nᵢ (Nⱼ/N)]；选定帧等权平均。派生值与原始轨迹摘要绑定；原始轨迹可在右侧下载。'));
  for(const frame of result.frames){const diagnostic=frame.neighbor_diagnostics;if(diagnostic)source.append(node('p',`帧 ${frame.frame}：第 k 邻居距离 ${numberText(diagnostic.kth_distance_min)}–${numberText(diagnostic.kth_distance_max)} Å；最小相邻距离差 ${numberText(diagnostic.minimum_gap)} Å；${diagnostic.reciprocal?'邻接关系互反':`有 ${diagnostic.missing_reverse_edges} 条非互反邻接，两个方向的结果均已保留`}。`,'plot-caption'));}
  for(const note of result.limitations)source.append(node('p',note,'form-note'));article.append(source);return article;
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
    if(resultTab==='report'){box.append(resultReport(report,current.id));if(!(report.structural_results||[]).length)continue;}
    const area=node('div',undefined,'numeric-result-area');box.append(area);
    const key=current.id+':'+report.id+':'+workspaceState.updated;
    let cached=sourceTableCache.get(key);
    if(!cached){cached={state:'loading'};sourceTableCache.set(key,cached);const task=current.id;
      api(`/api/tasks/${task}/results/${report.id}/tables`).then(value=>{cached.state='ready';cached.value=value;if(current?.id===task)renderWorkspaceResults();}).catch(()=>{cached.state='error';if(current?.id===task)renderWorkspaceResults();});}
    if(cached.state!=='ready'){area.append(node('p',cached.state==='error'?'数据来源核验未通过，未展示图表；可查看报告与历史。':'正在核对原始数据…','form-note'));continue;}
    const tables=cached.value.tables;
    const structures=cached.value.structural_results||[];
    if(resultTab==='report'){for(const result of structures)area.append(structuralDetails(result,{plot:true,data:true}));continue;}
    if(resultTab==='data'||resultTab==='overview')for(const table of tables){area.append(node('h4',`原始数值 · ${table.file}`),sourceTable(table),node('p',`${table.total_rows} 行；${table.sampled?'等间隔预览 128 行，包含首尾；完整数据在右侧下载':'展示全部数据'}。单位来自冻结分析计划。`,'plot-caption'));}
    if(resultTab==='data'||resultTab==='overview')for(const result of structures)area.append(structuralDetails(result,{data:true}));
    if(resultTab==='plots'||resultTab==='overview'){
      const options=[...report.results.map((op,i)=>({value:`numeric:${i}`,label:`${op.y} 对 ${op.x} · ${analysisMethods[op.method]||op.method}`,op})),...structures.map((result,i)=>({value:`structural:${i}`,label:`短程有序 · ${result.id}`,result}))];
      if(!options.length){area.append(node('p','当前报告没有可核验的图表。'));continue;}
      const label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择计算结果图表');options.forEach(option=>select.append(new Option(option.label,option.value)));label.append(select);area.append(label);const chart=node('article',undefined,'result-widget');area.append(chart);
      const draw=()=>{const selected=options.find(option=>option.value===select.value)||options[0];chart.replaceChildren();if(selected.result){chart.append(structuralDetails(selected.result,{plot:true}));return;}const op=selected.op,table=tables.find(t=>t.file===op.file);if(!table){chart.append(node('p','此图的数据未通过来源核验。'));return;}chart.append(numericalPlot(table,op),node('p',`${op.sample_count} 行参与原有分析；图中${table.sampled?'数据为预览采样':'展示原始点'}。选取区间：${op.window.join(' 至 ')}。`,'plot-caption'));if(op.method==='linear_fit')chart.append(node('p',`\\(y=${numberText(op.values.slope)}x+${numberText(op.values.intercept)}\\)；\\(R^2=${numberText(op.values.r_squared)}\\)。外推值不是新增计算。`));};select.onchange=draw;draw();
    }
  }
}
