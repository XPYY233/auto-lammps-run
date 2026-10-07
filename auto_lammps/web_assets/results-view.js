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
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('class','plot-svg');svg.setAttribute('viewBox','0 0 620 355');svg.setAttribute('role','img');svg.setAttribute('aria-label',`${operation.y} 随 ${operation.x} 变化，${operation.method==='saved_curve'?'已保存统计值':'真实数据与冻结拟合'}`);
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
  add('text',{x:82,y:27,fill:'#245dc8','font-size':12},operation.method==='saved_curve'?'● 保存的统计值':'● 原始数据');if(fit)add('text',{x:215,y:27,fill:'#952459','font-size':12},'– – 冻结区间拟合（含外推）');
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
const siteModelLabels={two_state_host_vacancy:'原宿主–空位两态',three_state_competing_species:'元素–元素–空位三态'};
const siteQuantityLabels={vacancy_fraction:'空位比例',mean_vacancy_enthalpy:'平均空位焓',mean_vacancy_volume:'平均空位体积',mu_0:'元素 0 化学势',mu_1:'元素 1 化学势',partial_volume_0:'元素 0 偏体积',partial_volume_1:'元素 1 偏体积'};
function siteDownloads(result,taskId,analysisId){
  const box=node('div',undefined,'result-downloads');
  for(const receipt of result.derived_files){const link=node('a',`下载完整 ${receipt.name} · ${receipt.rows} 行 ↓`,'quiet');link.href=`/api/tasks/${encodeURIComponent(taskId)}/results/${encodeURIComponent(analysisId)}/derived/${encodeURIComponent(receipt.name)}`;box.append(link,node('p','SHA-256：'+receipt.sha256,'source-hash'));}
  return box;
}
function siteDetails(result,taskId,analysisId,{data=false,preview}={}){
  const box=node('article',undefined,'result-widget'),c=result.coverage,p=result.parameters;
  box.append(node('h4',`逐位点统计热力学 · ${result.id}`),node('p',`${c.states} 个状态 × ${c.sites} 个位点 × ${c.variants} 个变体；${c.baseline_rows} 行独立基线，共 ${c.rows} 行完整原始数据。`,'plot-caption'),
    node('p',`${Object.entries(p.elements).map(([kind,symbol])=>`${kind}=${symbol}`).join('，')}；储库基准：${p.reservoir_anchor.method}；全部状态与位点等权。科学结论尚待核验。`,'form-note'));
  if(data){const fields=[['model','模型','1'],['temperature_K','温度','K'],['beta_eV_inverse','β','1/eV'],['mu_0_eV','元素 0 化学势','eV'],['mu_1_eV','元素 1 化学势','eV'],['vacancy_fraction','空位比例','1'],['mean_vacancy_enthalpy_eV','平均空位焓','eV'],['mean_vacancy_volume_A3','平均空位体积','angstrom^3']];
    box.append(node('h5','指定温度的全部统计值'),sourceTable({columns:fields.map(([,name,unit])=>({name,unit})),rows:result.temperature_summaries.map(s=>fields.map(([key])=>key==='model'?(siteModelLabels[s[key]]||s[key]):s[key]))}));
    if(preview){const detail=node('details');detail.append(node('summary','查看保存曲线的数值预览'),sourceTable(preview),node('p',`${preview.total_rows} 行完整曲线；${preview.sampled?'每个模型与网格最多预览 128 行，保留端点':'展示全部曲线值'}。完整曲线见 CSV 下载。`,'plot-caption'));box.append(detail);}
  }
  box.append(siteDownloads(result,taskId,analysisId));
  const detail=node('details',undefined,'result-source');detail.append(node('summary','查看统计假设、来源与完整范围'),node('p',`原始数组：${result.file}；SHA-256：${result.source.sha256}`,'source-hash'),node('p',`状态 ${p.states.first}–${p.states.last}，步距 ${p.states.stride}；位点 ${p.sites.first}–${p.sites.last}，步距 ${p.sites.stride}；β 网格 ${p.beta_grid.first}–${p.beta_grid.last}，${p.beta_grid.count} 点。`));
  for(const note of result.limitations)detail.append(node('p',note,'form-note'));box.append(detail);return box;
}
function siteCurvePlot(result,preview){
  if(!preview)return node('p','保存曲线未通过来源核验，未展示图表。','form-note');
  const box=node('article',undefined,'result-widget'),names=preview.columns.map(c=>c.name),label=node('label','选择热力学量'),select=node('select');select.setAttribute('aria-label','选择逐位点热力学量');
  for(const c of preview.columns)if(Object.hasOwn(siteQuantityLabels,c.name))select.append(new Option(`${siteQuantityLabels[c.name]} (${c.unit})`,c.name));
  select.value='vacancy_fraction';label.append(select);box.append(label);
  const gridLabel=node('label','选择保存网格'),grid=node('select');grid.setAttribute('aria-label','选择保存热力学网格');grid.append(new Option('完整 β 网格预览','beta'),new Option('指定温度','temperature'));grid.value='beta';gridLabel.append(grid);box.append(gridLabel);
  const area=node('div',undefined,'chart-area');box.append(area);
  const draw=()=>{area.replaceChildren();const y=select.value,x=grid.value==='beta'?'beta':'temperature',xi=names.indexOf(x),yi=names.indexOf(y);
    for(const model of result.parameters.models){const rows=preview.rows.filter(r=>r[0]===grid.value&&r[2]===model).map(r=>[r[xi],r[yi]]).sort((a,b)=>a[0]-b[0]);
      if(!rows.length)continue;const table={columns:[{name:x,unit:preview.columns[xi].unit},{name:y,unit:preview.columns[yi].unit}],rows};
      area.append(node('h5',siteModelLabels[model]||model),numericalPlot(table,{method:'saved_curve',x,y,window:[rows[0][0],rows.at(-1)[0]],values:{}}));}
    area.append(node('p',`曲线来自已保存的完整 CSV；${preview.sampled?'图中是每个模型/网格最多 128 点的预览':'展示该网格全部保存点'}。连线帮助阅读，不代表新增计算；完整 ${preview.total_rows} 行可下载。`,'plot-caption'));};select.onchange=draw;grid.onchange=draw;draw();return box;
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
    if(resultTab==='report'){box.append(resultReport(report,current.id));if(!(report.structural_results||[]).length&&!(report.site_thermodynamic_results||[]).length)continue;}
    const area=node('div',undefined,'numeric-result-area');box.append(area);
    const key=current.id+':'+report.id+':'+workspaceState.updated;
    let cached=sourceTableCache.get(key);
    if(!cached){cached={state:'loading'};sourceTableCache.set(key,cached);const task=current.id;
      api(`/api/tasks/${task}/results/${report.id}/tables`).then(value=>{cached.state='ready';cached.value=value;if(current?.id===task)renderWorkspaceResults();}).catch(()=>{cached.state='error';if(current?.id===task)renderWorkspaceResults();});}
    if(cached.state!=='ready'){area.append(node('p',cached.state==='error'?'数据来源核验未通过，未展示图表；可查看报告与历史。':'正在核对原始数据…','form-note'));continue;}
    const tables=cached.value.tables;
    const structures=cached.value.structural_results||[];
    const sites=cached.value.site_thermodynamic_results||[],sitePreviews=cached.value.site_previews||[];
    if(resultTab==='report'){for(const result of structures)area.append(structuralDetails(result,{plot:true,data:true}));for(const result of sites)area.append(siteCurvePlot(result,sitePreviews.find(p=>p.id===result.id)?.curves));continue;}
    if(resultTab==='data'||resultTab==='overview')for(const table of tables){area.append(node('h4',`原始数值 · ${table.file}`),sourceTable(table),node('p',`${table.total_rows} 行；${table.sampled?'等间隔预览 128 行，包含首尾；完整数据在右侧下载':'展示全部数据'}。单位来自冻结分析计划。`,'plot-caption'));}
    if(resultTab==='data'||resultTab==='overview')for(const result of structures)area.append(structuralDetails(result,{data:true}));
    if(resultTab==='data'||resultTab==='overview')for(const result of sites)area.append(siteDetails(result,current.id,report.id,{data:true,preview:sitePreviews.find(p=>p.id===result.id)?.curves}));
    if(resultTab==='plots'||resultTab==='overview'){
      const options=[...report.results.map((op,i)=>({value:`numeric:${i}`,label:`${op.y} 对 ${op.x} · ${analysisMethods[op.method]||op.method}`,op})),...structures.map((result,i)=>({value:`structural:${i}`,label:`短程有序 · ${result.id}`,result})),...sites.map((site,i)=>({value:`site:${i}`,label:`逐位点统计热力学 · ${site.id}`,site}))];
      if(!options.length){area.append(node('p','当前报告没有可核验的图表。'));continue;}
      const label=node('label','选择图表'),select=node('select');select.setAttribute('aria-label','选择计算结果图表');options.forEach(option=>select.append(new Option(option.label,option.value)));label.append(select);area.append(label);const chart=node('article',undefined,'result-widget');area.append(chart);
      const draw=()=>{const selected=options.find(option=>option.value===select.value)||options[0];chart.replaceChildren();if(selected.site){chart.append(siteCurvePlot(selected.site,sitePreviews.find(p=>p.id===selected.site.id)?.curves),siteDownloads(selected.site,current.id,report.id));return;}if(selected.result){chart.append(structuralDetails(selected.result,{plot:true}));return;}const op=selected.op,table=tables.find(t=>t.file===op.file);if(!table){chart.append(node('p','此图的数据未通过来源核验。'));return;}chart.append(numericalPlot(table,op),node('p',`${op.sample_count} 行参与原有分析；图中${table.sampled?'数据为预览采样':'展示原始点'}。选取区间：${op.window.join(' 至 ')}。`,'plot-caption'));if(op.method==='linear_fit')chart.append(node('p',`\\(y=${numberText(op.values.slope)}x+${numberText(op.values.intercept)}\\)；\\(R^2=${numberText(op.values.r_squared)}\\)。外推值不是新增计算。`));};select.onchange=draw;draw();
    }
  }
}
