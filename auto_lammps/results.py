"""Task-scoped, read-only browser projection; never fetches or analyzes data."""
import json
import math
import csv
import io
from pathlib import Path
import re
from datetime import datetime, timezone

from .candidate_jobs import CandidateHistory
from .manifest import canonical, sha256
from . import runtime_launcher as runtime

STATES={'prepared':'尚未提交','dispatching':'正在提交','unknown':'提交状态待核对','accepted':'已提交',
        'queued':'排队中','running':'计算中','cancelling':'正在取消','completed':'计算已结束',
        'failed':'计算失败','cancelled':'已取消','timeout':'计算超时','rejected':'提交被拒绝',
        'cancelled_before_dispatch':'提交前已取消','reconcile_required':'记录存在矛盾 · 待核对'}
EVENTS={'raw_export_reserved':'保留原始输出下载空间','reserved':'预留计算资源','dispatch_intent':'发起计算提交','scheduler_accepted':'收到作业编号',
        'scheduler_observed':'更新计算状态','accounting_final':'完成资源核算','dispatch_unknown':'提交状态待核对',
        'scheduler_rejected':'提交被拒绝','upload_intent':'开始传送输入','inputs_staged':'输入传送完成',
        'upload_failed':'输入传送未完成','output_fetch_started':'开始回收结果','output_fetch_finished':'保存回收记录',
        'analysis_reserved':'开始分析结果','analysis_saved':'保存分析记录','reconciliation_started':'核对调度记录',
        'reconciliation_finished':'保存调度核对','reconciliation_conflict':'调度记录存在矛盾',
        'scheduler_observation_conflict':'调度记录存在矛盾','cancel_intent':'请求取消',
        'cancelled_before_dispatch':'提交前已取消','following_registered':'开始自动跟进',
        'following_poll':'预留调度查询与记录空间','execution_authorized':'核对已有执行许可',
        'monitor_registered':'开启后台进度检查','monitor_poll':'检查超算进度',
        'monitor_result':'保存进度检查结果','monitor_transition':'计算或连接状态发生变化'}
FOLLOWING={'waiting':'等待计算进展','collecting':'自动回收结果','analyzing':'自动分析结果',
           'analyzed':'自动分析已完成','analysis_failed':'自动分析未完成',
           'diagnostics_saved':'失败计算的诊断已保存','attention':'自动跟进需要核对'}


def event_label(event):
    if event['kind']=='following_progress':
        return FOLLOWING.get(json.loads(event['payload'])['state'],'自动跟进状态待核对')
    return EVENTS.get(event['kind'],'保存运行记录')


class ResultUnavailable(ValueError):
    pass


def public_time(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if type(value) in (int,float) else value


def existing_private_directory(path):
    path=Path(path).expanduser().absolute()
    fd=runtime.directory(path,private=True)
    import os
    os.close(fd)
    if any((parent/'.git').exists() for parent in (path,*path.parents)):
        raise ValueError('Result storage must be outside Git')
    return path


def structural_projection(item, source):
    """Validate a bounded derived report, not recompute geometry in a GET."""
    from . import coordination_analysis as coordination
    proof=runtime.hash_value(item['derived_sha256'])
    if sha256(canonical({key:value for key,value in item.items() if key!='derived_sha256'}))!=proof:
        raise ResultUnavailable('Derived structural result changed')
    spec={key:source[key] for key in coordination.TABLE_FIELDS}
    coordination.validate_operation(item['parameters'],spec)
    if (item['method']!=coordination.METHOD or item['id']!=item['parameters']['id']
            or item['file']!=source['file'] or item['source']!=source
            or item['scientific_status']!='not_evaluated' or item['physics_simulation'] is not False):
        raise ResultUnavailable('Structural result differs from its declared source or method')
    columns=dict(directed=['type_i','type_j','directed_neighbor_count','N_i','N_j',
                          'p_j_given_i','c_j','alpha_ij'],
                 symmetric=['type_i','type_j','alpha_symmetric'],
                 aggregate=['type_i','type_j','mean','sample_std','min','max'])
    if item['columns']!=columns:
        raise ResultUnavailable('Unsupported structural derived columns')
    keys=sorted(map(int,source['elements']))
    pairs=dict(directed=[(i,j) for i in keys for j in keys],
               symmetric=[(i,j) for pos,i in enumerate(keys) for j in keys[pos:]])
    def rows(values,kind,width,nullable=()):
        if not isinstance(values,list) or len(values)!=len(pairs[kind]):
            raise ResultUnavailable('Incomplete structural element pairs')
        for row,pair in zip(values,pairs[kind]):
            if (not isinstance(row,list) or len(row)!=width or tuple(row[:2])!=pair
                    or any(type(v) is not int for v in row[:2])
                    or any(not (v is None and index in nullable) and
                           (type(v) not in (int,float) or not math.isfinite(v))
                           for index,v in enumerate(row))):
                raise ResultUnavailable('Invalid structural derived values')
        return values
    interval=item['parameters']['frames']
    selected=list(range(interval['first'],interval['last']+1,interval['stride']))
    frames=item['frames']
    if (type(item['source_frame_count']) is not int or item['source_frame_count']<=interval['last']
            or type(item['selected_frame_count']) is not int or item['selected_frame_count']!=len(selected) or not isinstance(frames,list)
            or len(frames)!=len(selected)):
        raise ResultUnavailable('Structural sampling differs from the frozen interval')
    safe_frames=[];previous_step=-1;particle_identity=None
    diagnostic_keys=('kth_distance_min','kth_distance_max','next_distance_min','next_distance_max',
                     'minimum_gap','zero_gap_atoms','reciprocal','coincident_fractional_tolerance')
    for frame,index in zip(frames,selected):
        identity=runtime.hash_value(frame['particle_ids_sha256'])
        if (type(frame['frame']) is not int or frame['frame']!=index
                or type(frame['timestep']) is not int or frame['timestep']<=previous_step
                or type(frame['atom_count']) is not int or frame['atom_count']!=sum(source['expected_counts'].values())
                or particle_identity not in (None,identity)):
            raise ResultUnavailable('Structural frame identity differs from frozen sampling')
        diagnostic={key:frame['neighbor_diagnostics'][key] for key in diagnostic_keys}
        if 'missing_reverse_edges' in frame['neighbor_diagnostics']:
            diagnostic['missing_reverse_edges']=frame['neighbor_diagnostics']['missing_reverse_edges']
            if type(diagnostic['missing_reverse_edges']) is not int or diagnostic['missing_reverse_edges']<0:
                raise ResultUnavailable('Invalid structural reciprocity count')
        if (type(diagnostic['reciprocal']) is not bool or type(diagnostic['zero_gap_atoms']) is not int
                or not 0<=diagnostic['zero_gap_atoms']<=frame['atom_count']
                or any(type(diagnostic[key]) not in (int,float) or not math.isfinite(diagnostic[key])
                       for key in diagnostic_keys if key not in ('reciprocal','zero_gap_atoms'))):
            raise ResultUnavailable('Invalid structural neighborhood diagnostics')
        safe_frames.append(dict(frame=index,timestep=frame['timestep'],atom_count=frame['atom_count'],
            particle_ids_sha256=identity,neighbor_diagnostics=diagnostic,
            directed=rows(frame['directed'],'directed',8),symmetric=rows(frame['symmetric'],'symmetric',3)))
        previous_step,particle_identity=frame['timestep'],identity
    aggregates={kind:rows(item['aggregates'][kind],kind,6,(3,)) for kind in pairs}
    chart=dict(kind='categorical_bars',quantity='Warren-Cowley alpha',unit='1',
               labels=[source['elements'][str(i)]+'–'+source['elements'][str(j)] for i,j in pairs['symmetric']],
               values=[row[2] for row in aggregates['symmetric']])
    if item['chart']!=chart or not isinstance(item['ovito_version'],str) or not re.fullmatch(r'[0-9.]+',item['ovito_version']):
        raise ResultUnavailable('Structural chart differs from the full derived table')
    return dict(id=item['id'],method=item['method'],file=item['file'],parameters=item['parameters'],
        source=source,ovito_version=item['ovito_version'],source_frame_count=item['source_frame_count'],
        selected_frame_count=len(selected),columns=columns,frames=safe_frames,aggregates=aggregates,
        chart=chart,derived_sha256=proof,scientific_status='not_evaluated',physics_simulation=False,
        formulas=dict(directed='alpha_ij = 1 - n_ij/(k*N_i*(N_j/N))',symmetric='(alpha_ij + alpha_ji)/2',
                      aggregation='equal weight per selected frame'),
        limitations=['最近邻定义不自动证明物理第一配位壳层。',
                    '帧间波动仅为描述统计，相关帧不等于独立重复实验。',
                    '数值处理不包含论文答案、科学通过阈值或事后改选采样区间。'])


SITE_COLUMNS={
    'differences': [('state','1'),('site','1'),('variant','1'),('host','1'),
        ('delta_E','eV'),('delta_V','angstrom^3'),('delta_H','eV'),('delta_N_0','1'),
        ('delta_N_1','1'),('baseline_source_line','1'),('variant_source_line','1')],
    'sites': [('state','1'),('site','1'),('host','1'),('model','1'),('temperature','K'),
        ('beta','1/eV'),('p_0','1'),('p_1','1'),('p_vacancy','1'),('vacancy_enthalpy','eV'),
        ('vacancy_volume','angstrom^3'),('baseline_source_line','1'),('vacancy_source_line','1')],
    'curves': [('grid','1'),('index','1'),('model','1'),('beta','1/eV'),('temperature','K'),
        ('mu_0','eV'),('mu_1','eV'),('partial_volume_0','angstrom^3'),
        ('partial_volume_1','angstrom^3'),('vacancy_fraction','1'),
        ('mean_vacancy_enthalpy','eV'),('mean_vacancy_volume','angstrom^3'),
        ('composition_residual','1'),('solver_iterations','1')]}
SITE_SUMMARY_FIELDS={'model','beta_eV_inverse','temperature_K','mu_0_eV','mu_1_eV',
    'partial_volume_0_A3','partial_volume_1_A3','vacancy_fraction',
    'mean_vacancy_enthalpy_eV','mean_vacancy_volume_A3','composition_residual','solver_iterations'}


def site_projection(item, source):
    """Check a saved complete-array proof; never rerun thermodynamics on GET."""
    from . import site_thermodynamics as site
    proof=runtime.hash_value(item['derived_sha256'])
    if sha256(canonical({k:v for k,v in item.items() if k!='derived_sha256'}))!=proof:
        raise ResultUnavailable('Derived site result changed')
    table={k:source[k] for k in ('file','format','columns')}
    operation=item['parameters'];site.validate_operation(operation,table)
    if (item['id']!=operation['id'] or item['method']!=site.METHOD
            or item['file']!=source['file'] or item['source']!=source
            or item['scientific_status']!='not_evaluated' or item['physics_simulation'] is not False
            or item['equations']!=site.EQUATIONS):
        raise ResultUnavailable('Site result differs from its frozen method or source')
    ns=len(site._domain(operation['states'],'state'));nl=len(site._domain(operation['sites'],'site'));n=ns*nl
    coverage=dict(states=ns,sites=nl,variants=3,rows=4*n,baseline_rows=n,complete=True)
    if (item['coverage']!=coverage or any(type(item['coverage'][k]) is not int
            for k in coverage if k!='complete') or item['coverage']['complete'] is not True):
        raise ResultUnavailable('Incomplete site-array coverage')
    summaries=item['temperature_summaries']
    expected=[(model,t) for model in operation['models'] for t in operation['temperatures_K']]
    if not isinstance(summaries,list) or len(summaries)!=len(expected):
        raise ResultUnavailable('Incomplete temperature summaries')
    for summary,(model,t) in zip(summaries,expected):
        if (not isinstance(summary,dict) or set(summary)!=SITE_SUMMARY_FIELDS
                or summary['model']!=model or any(type(v) not in (int,float) or not math.isfinite(v)
                    for k,v in summary.items() if k!='model')
                or not math.isclose(summary['temperature_K'],t,rel_tol=1e-12)
                or not math.isclose(summary['beta_eV_inverse'],1/(site.KB_EV_K*t),rel_tol=1e-12)
                or not 0<=summary['vacancy_fraction']<=1
                or type(summary['solver_iterations']) is not int
                or not 1<=summary['solver_iterations']<=operation['solver']['max_iterations']):
            raise ResultUnavailable('Invalid saved thermodynamic summary')
    receipts=item['derived_files']
    if not isinstance(receipts,list) or len(receipts)!=3:
        raise ResultUnavailable('Incomplete derived CSV inventory')
    expected_rows=dict(differences=3*n,sites=len(expected)*n,
        curves=(len(operation['temperatures_K'])+operation['beta_grid']['count'])*len(operation['models']))
    limits=dict(differences=8192+3*n*512,sites=8192+len(operation['temperatures_K'])*2*n*768,
        curves=8192+(len(operation['temperatures_K'])+operation['beta_grid']['count'])*2*768)
    for receipt,(kind,pairs) in zip(receipts,SITE_COLUMNS.items()):
        if (not isinstance(receipt,dict) or set(receipt)!={'name','size','sha256','rows','columns',
                'source_sha256','parameters_sha256'} or receipt['name']!=operation['id']+'-'+kind+'.csv'
                or type(receipt['size']) is not int or not 1<=receipt['size']<=limits[kind]
                or type(receipt['rows']) is not int or receipt['rows']!=expected_rows[kind]
                or receipt['columns']!=[dict(name=k,unit=u) for k,u in pairs]
                or receipt['source_sha256']!=source['sha256']
                or receipt['parameters_sha256']!=sha256(canonical(operation))):
            raise ResultUnavailable('Derived CSV differs from its complete frozen domain')
        runtime.hash_value(receipt['sha256'])
    # The free-text reservoir provenance remains in the saved private report.
    # The browser receives declared numeric choices and fixed method labels only.
    parameters={k:v for k,v in operation.items() if k!='reservoir_anchor'}
    parameters['reservoir_anchor']={k:v for k,v in operation['reservoir_anchor'].items() if k!='source'}
    return dict(id=item['id'],method=site.METHOD,file=item['file'],source=source,
        parameters=parameters,coverage=coverage,equations=site.EQUATIONS,
        temperature_summaries=summaries,derived_files=receipts,derived_sha256=proof,
        scientific_status='not_evaluated',physics_simulation=False,
        limitations=['独立无相互作用位点近似；弛豫焓代替含振动的自由能。',
            '化学势绝对基准来自预先选择的储库假设；成分只约束化学势差。',
            '全部状态与位点等权；未估计独立重复的不确定度或科学评分。',
            '收敛标记来自冻结的力与压力诊断；迭代数不证明最小化终止原因。'])


def site_source_preview(data, source):
    """Validate every source token within the site limits, retaining at most 128 rows."""
    from . import site_thermodynamics as site
    table={k:source[k] for k in ('file','format','columns')};site.validate_table(table)
    if len(data)>site.MAX_ARRAY_BYTES or not data.endswith(b'\n'):
        raise ResultUnavailable('Site source exceeds its byte bound or is truncated')
    count=data.count(b'\n')-2
    if count<1 or count*len(table['columns'])>site.MAX_ARRAY_CELLS:
        raise ResultUnavailable('Site source exceeds its full cell bound')
    selected={round(i*(count-1)/127) for i in range(min(128,count))} if count>128 else set(range(count))
    stream=io.BytesIO(data)
    expected=[('# columns: '+' '.join(c['name'] for c in table['columns'])).encode(),
              ('# units: '+' '.join(c['unit'] for c in table['columns'])).encode()]
    if [stream.readline().rstrip(b'\r\n'),stream.readline().rstrip(b'\r\n')]!=expected:
        raise ResultUnavailable('Site source headers changed')
    rows=[];lines=[]
    for index,raw in enumerate(stream):
        tokens=raw.split()
        if len(raw)>4098 or len(tokens)!=len(table['columns']) or any(
                not re.fullmatch(rb'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',t)
                for t in tokens):
            raise ResultUnavailable('Invalid site source row')
        values=[float(t) for t in tokens]
        if any(not math.isfinite(v) for v in values):raise ResultUnavailable('Nonfinite site source row')
        if index in selected:rows.append(values);lines.append(index+3)
    return dict(file=source['file'],format=site.FORMAT,sha256=source['sha256'],columns=source['columns'],
        total_rows=count,sampled=count>128,rows=rows,source_lines=lines)


def site_curve_preview(data, receipt, operation):
    """Read the saved curve CSV, not solve or interpolate new thermodynamic values."""
    columns=receipt['columns'];reader=csv.reader(io.StringIO(data.decode('ascii'),newline=''))
    if next(reader)!=[c['name'] for c in columns]:raise ResultUnavailable('Derived CSV header changed')
    total=receipt['rows'];rows=[]
    # Preserve each model/grid independently, including the first and last beta.
    per_grid={kind:len(operation['temperatures_K']) if kind=='temperature' else operation['beta_grid']['count']
              for kind in ('temperature','beta')}
    selected={kind:{round(i*(length-1)/127) for i in range(min(128,length))}
              if length>128 else range(length) for kind,length in per_grid.items()}
    seen={};count=0
    for raw in reader:
        if len(raw)!=len(columns) or raw[0] not in per_grid or raw[2] not in operation['models']:
            raise ResultUnavailable('Invalid saved curve row')
        values=[raw[0],int(raw[1]),raw[2],*[float(v) for v in raw[3:]]]
        key=(values[2],values[0]);index=seen.get(key,0);length=per_grid[values[0]]
        if values[1]!=index or index>=length or any(not math.isfinite(v) for v in values[3:]):
            raise ResultUnavailable('Incomplete saved curve domain')
        if index in selected[values[0]]:rows.append(values)
        seen[key]=index+1;count+=1
    if count!=total or any(seen.get((model,grid))!=length
            for model in operation['models'] for grid,length in per_grid.items()):
        raise ResultUnavailable('Incomplete saved curve CSV')
    return dict(name=receipt['name'],sha256=receipt['sha256'],columns=columns,total_rows=total,
        sampled=len(rows)<total,rows=rows,preview_policy='up_to_128_per_model_and_grid_including_endpoints')


class ResultsReader:
    def __init__(self, tasks, ledger, collections, reports):
        self.tasks,self.ledger=tasks,ledger
        self.preparations=CandidateHistory(tasks)
        self.collections=existing_private_directory(collections)
        self.reports=existing_private_directory(reports)

    def _report(self, request, event, events):
        saved=json.loads(event['payload'])
        identifier=runtime.hash_value(saved['analysis_id'])
        raw=runtime.read_regular(self.reports/(identifier+'.json'),65536,private=True)
        if sha256(raw)!=saved['evidence_sha256']:raise ResultUnavailable('Report changed')
        value=json.loads(raw);context=value['context'];report=value['report']
        if (context['analysis_id']!=identifier or context['request_id']!=request['id']
                or context['job_id']!=request['job_id'] or context['manifest_sha256']!=request['manifest_sha256']
                or context['storage_scope_sha256']!=sha256(str(self.reports).encode())):
            raise ResultUnavailable('Report belongs to another result')
        reservations=[json.loads(e['payload']) for e in events if e['kind']=='analysis_reserved']
        if context not in reservations:raise ResultUnavailable('No matching report reservation')
        collected=[json.loads(e['payload']) for e in events if e['kind']=='output_fetch_finished']
        matches=[e for e in collected if e['collected'] and e['evidence_sha256']==context['collection_sha256']]
        if len(matches)!=1 or not re.fullmatch(r'[a-f0-9]{32}',matches[0]['ticket']):
            raise ResultUnavailable('Missing source collection record')
        receipt=runtime.read_regular(self.collections/matches[0]['ticket']/'receipt.json',262144,private=True)
        if sha256(receipt)!=context['collection_sha256']:raise ResultUnavailable('Collection receipt changed')
        source=json.loads(receipt)
        if (source['state']!='collected' or any(source['context'][key]!=context[key] for key in
                ('request_id','job_id','manifest_sha256'))):raise ResultUnavailable('Wrong collection source')
        if request['state']!='completed' or not request['accounted']:
            raise ResultUnavailable('Scheduler evidence needs reconciliation')
        if report['scientific_status']!='not_evaluated':raise ResultUnavailable('Unsupported scientific verdict')
        if report['status']=='analysis_failed':
            return dict(id=identifier,status='analysis_failed',label='分析未完成',at=public_time(event['at']),
                        message='分析检查未通过，失败记录已保留。请核对分析计划、输出格式与计算记录。')
        if report['status']!='analyzed' or report['manifest_sha256']!=request['manifest_sha256']:
            raise ResultUnavailable('Invalid analysis identity')
        # Only numeric result fields and source labels reach the UI. No private
        # context, server paths, raw diagnostics, grants or reference records.
        results=[]
        for item in report['results']:
            values=item['values']
            if any(v is not None and (type(v) not in (int,float) or not math.isfinite(v)) for v in values.values()):
                raise ResultUnavailable('Invalid numeric result')
            results.append({key:item[key] for key in ('id','method','file','x','y','window','sample_count',
                                                       'source_line_ranges','values','value_units','units_origin')})
        inventory={item['path']:item for item in source['header']['files']}
        sources=[];structural_sources={};site_sources={}
        for item in report['sources']:
            name=item['file']
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name):
                raise ResultUnavailable('Invalid source label')
            collected_item=inventory.get('output/'+name)
            if not collected_item or any(item[key]!=collected_item[key] for key in ('sha256','size')):
                raise ResultUnavailable('Analysis source differs from collected receipt')
            if item.get('format')=='lammps_dump':
                from .coordination_analysis import TABLE_FIELDS, validate_table
                validate_table({key:item[key] for key in TABLE_FIELDS})
                public_source={key:item[key] for key in (*TABLE_FIELDS,'sha256','size')}
                structural_sources[name]=public_source
                sources.append(public_source)
            elif item.get('format')=='site_scan_array_v1':
                from .site_thermodynamics import validate_table,MAX_ARRAY_BYTES
                validate_table({key:item[key] for key in ('file','format','columns')})
                if type(item['size']) is not int or not 1<=item['size']<=MAX_ARRAY_BYTES:
                    raise ResultUnavailable('Invalid complete site source size')
                public_source={key:item[key] for key in ('file','format','columns','sha256','size')}
                site_sources[name]=public_source;sources.append(public_source)
            else:
                sources.append({key:item[key] for key in ('file','sha256','columns')})
        if len({s['file'] for s in sources})!=len(sources):
            raise ResultUnavailable('Duplicate analysis sources')
        structural_results=[]
        for item in report.get('structural_results',[]):
            if item['file'] not in structural_sources:
                raise ResultUnavailable('Structural result has no matching dump source')
            structural_results.append(structural_projection(item,structural_sources[item['file']]))
        if structural_sources and (report.get('adapter_version') not in (3,4)
                or {s['file'] for s in structural_results}!=set(structural_sources)):
            raise ResultUnavailable('Structural source lacks versioned analysis')
        site_results=[]
        for item in report.get('site_thermodynamic_results',[]):
            if item['file'] not in site_sources:
                raise ResultUnavailable('Site result has no matching complete source')
            if item['adapter_identity']!=report['adapter_identity']['site_thermodynamics']:
                raise ResultUnavailable('Site result differs from the saved adapter identity')
            site_results.append(site_projection(item,site_sources[item['file']]))
        if site_sources:
            from . import site_thermodynamics as site
            operations=[item['parameters'] for item in report['site_thermodynamic_results']]
            if (report.get('adapter_version')!=4 or {s['file'] for s in site_results}!=set(site_sources)
                    or context.get('storage_bytes')!=site.reservation_bytes(dict(operations=operations))
                    or sum(r['size'] for s in site_results for r in s['derived_files'])+65536>context['storage_bytes']
                    or len({s['id'] for s in site_results})!=len(site_results)):
                raise ResultUnavailable('Site result lacks its complete reserved version-four storage')
        projection=dict(id=identifier,status='analyzed',label='数值分析已完成',at=public_time(event['at']),
                    quantity=report['declared_quantity'],results=results,
                    sources=sources,
                    scientific_status='not_evaluated',
                    notes=['尚未核验科学结论；数值处理完成不等于研究目标已达成。',
                           ('原生标量表不含单位；单位来自预先声明，需核验物理定义和脚本中的单位处理。'
                            if any(r['units_origin']=='declared_only_not_present_in_scalar_header' for r in results)
                            else '结构分析参数或数值表头已核对，仍需核验物理定义和脚本中的单位处理。'),
                           '样本标准差描述数据波动，不代表独立重复实验的不确定度。'])
        if structural_results:projection.update(structural_results=structural_results,analysis_format='numeric_tables_v3')
        if site_results:projection.update(site_thermodynamic_results=site_results,analysis_format='numeric_tables_v4')
        return projection

    def task(self, identifier):
        self.tasks.get(identifier)
        candidate=self.preparations.get(identifier)
        base=dict(configured=True,evaluations=[],scientific_status='not_evaluated')
        if not candidate:
            return dict(base,message='尚无已准备的计算方案。结果会在计算与分析完成后出现在这里。')
        if sha256(self.tasks.export(identifier))!=candidate['condition_sha256']:
            raise ResultUnavailable('Prepared conditions changed')
        # Preparation of the next version must not hide past runs of this task.
        # Only immutable prepared events bound to the same frozen conditions qualify.
        digests={runtime.hash_value(e['payload']['snapshot_sha256']) for e in candidate['events']
                 if e['state']=='prepared' and e['payload'].get('snapshot_sha256')}
        groups=list({group['id']:group for digest in sorted(digests)
                     for group in self.ledger.product_results(digest)}.values())
        for group in groups:
            requests=[];ordinal=0
            for request in group['requests']:
                events=request['events'];reports=[]
                for event in events:
                    if event['kind']!='analysis_saved':continue
                    try:reports.append(self._report(request,event,events))
                    except (KeyError,ValueError,TypeError,AttributeError,OSError,runtime.ExecutionDenied):
                        reports.append(dict(status='unavailable',label='报告暂不可用',at=public_time(event['at']),
                                            message='报告或关联证据未通过核验，未展示数值。'))
                ordinal+=bool(request['dispatch_claimed'])
                kinds={e['kind'] for e in events}
                fetches=[json.loads(e['payload']) for e in events if e['kind']=='output_fetch_finished']
                collection=('结果已回收' if fetches[-1]['collected'] else '结果回收未完成') if fetches else None
                stage=('报告待核对' if any(r['status']=='unavailable' for r in reports) else
                       '分析未完成' if any(r['status']=='analysis_failed' for r in reports) else
                       '分析记录已保存' if reports else '分析待完成' if 'analysis_reserved' in kinds else
                       collection if collection else '结果回收待完成' if 'output_fetch_started' in kinds else
                       '结果待回收' if request['state']=='completed' and request['accounted'] else
                       STATES.get(request['state'],'状态待核对'))
                progress=[json.loads(e['payload']) for e in events if e['kind']=='following_progress']
                if progress and progress[-1]['state']=='attention':stage='自动跟进需要核对 · '+stage
                requests.append(dict(id=request['id'],job_id=request['job_id'],dispatch_ordinal=ordinal if request['dispatch_claimed'] else None,
                    state=request['state'],state_label=STATES.get(request['state'],'状态待核对'),stage=stage,
                    accounted=bool(request['accounted']),reports=reports,
                    history=[dict(at=public_time(e['at']),label=event_label(e)) for e in events]))
            pending=sum(r['state']=='prepared' and not r['dispatch_claimed'] for r in group['requests'])
            base['evaluations'].append(dict(id=group['id'],max_attempts=group['max_attempts'],
                dispatch_count=ordinal,pending_attempts=pending,used_attempts=ordinal+pending,requests=requests))
        return dict(base,message='计算状态、数值分析和科学核验分别记录。' if groups else '尚未提交计算，没有结果记录。')

    def summary(self, identifier):
        """The list and detail use the same task-bound execution evidence."""
        groups=self.task(identifier)['evaluations']
        runs=[r for g in groups for r in g['requests'] if r['dispatch_ordinal'] is not None]
        if not runs:return {}
        latest=max(runs,key=lambda r: r['history'][0]['at'])
        return dict(execution_state=latest['state'],execution_stage=latest['stage'],
                    job_id=latest['job_id'],submission_count=sum(g['dispatch_count'] for g in groups),
                    max_submissions=sum(g['max_attempts'] for g in groups),
                    scientific_status='not_evaluated')

    def _verified_sources(self, identifier, analysis_id):
        """Bind a second read to the task, immutable ledger event and collection proof."""
        verified=self.report(identifier,analysis_id)
        raw=runtime.read_regular(self.reports/(analysis_id+'.json'),65536,private=True)
        saved=json.loads(raw);context=saved['context'];report=saved['report']
        if saved['context']['analysis_id']!=verified['id']:raise ResultUnavailable('Report identity changed')
        # Recheck the full report after the read, so a concurrent replacement cannot
        # change the source declaration between binding and preview.
        self.report(identifier,analysis_id)
        events=self.ledger.events(context['request_id'])
        proofs=[json.loads(e['payload']) for e in events if e['kind']=='analysis_saved'
                and json.loads(e['payload']).get('analysis_id')==analysis_id]
        if len(proofs)!=1 or sha256(raw)!=proofs[0]['evidence_sha256']:
            raise ResultUnavailable('Report changed during preview')
        matches=[json.loads(e['payload']) for e in events if e['kind']=='output_fetch_finished'
                 and json.loads(e['payload']).get('evidence_sha256')==context['collection_sha256']]
        if len(matches)!=1:raise ResultUnavailable('Missing source receipt')
        ticket=matches[0]['ticket']
        if not re.fullmatch(r'[a-f0-9]{32}',ticket):raise ResultUnavailable('Invalid collection identity')
        receipt=runtime.read_regular(self.collections/ticket/'receipt.json',262144,private=True)
        if sha256(receipt)!=context['collection_sha256']:raise ResultUnavailable('Source receipt changed')
        inventory={i['path']:i for i in json.loads(receipt)['header']['files']}
        return verified,report,ticket,inventory

    def tables(self, identifier, analysis_id):
        """Bounded previews of verified source bytes, never new analysis or fetching."""
        verified,report,ticket,inventory=self._verified_sources(identifier,analysis_id)
        from .analysis import parse_table, MAX_TABLE_BYTES
        from .scalar_analysis import parse_scalar
        tables=[];total=0
        is_structural=verified.get('analysis_format') in ('numeric_tables_v3','numeric_tables_v4')
        if not 1<=len(report['sources'])<=(29 if is_structural else 16):
            raise ResultUnavailable('Preview source limit exceeded')
        public_sources={s['file']:s for s in verified['sources']}
        for source in report['sources']:
            name=source['file']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name):
                raise ResultUnavailable('Invalid source name')
            item=inventory.get('output/'+name)
            if not item or item['sha256']!=source['sha256'] or item['size']!=source['size']:
                raise ResultUnavailable('Source declaration differs from receipt')
            if source.get('format')=='lammps_dump':
                from .coordination_analysis import _source_digest
                _source_digest(self.collections/ticket/'payload'/'output'/name,source)
                continue
            if source.get('format')=='site_scan_array_v1':
                from .site_thermodynamics import MAX_ARRAY_BYTES
                data=runtime.read_regular(self.collections/ticket/'payload'/'output'/name,MAX_ARRAY_BYTES)
                if len(data)!=source['size'] or sha256(data)!=source['sha256']:
                    raise ResultUnavailable('Complete site source changed')
                tables.append(site_source_preview(data,public_sources[name]));del data
                continue
            total+=item['size']
            if item['size']>MAX_TABLE_BYTES or (not is_structural and total>MAX_TABLE_BYTES):
                raise ResultUnavailable('Preview byte limit exceeded')
            data=runtime.read_regular(self.collections/ticket/'payload'/'output'/name,item['size'])
            if sha256(data)!=source['sha256']:raise ResultUnavailable('Source bytes changed')
            spec={k:source[k] for k in ('file','columns')}
            if 'format' in source:spec.update({k:source[k] for k in ('format','headers','steps')})
            rows=parse_scalar(data,spec) if 'format' in spec else parse_table(data,spec)
            if len(rows)*len(spec['columns'])>500000:raise ResultUnavailable('Preview cell limit exceeded')
            # Even spacing includes both endpoints; the UI states that this is a
            # preview. Fit metrics still come from the original frozen analysis.
            indices=sorted({round(i*(len(rows)-1)/127) for i in range(min(128,len(rows)))}) if len(rows)>128 else range(len(rows))
            selected=[rows[i] for i in indices]
            tables.append(dict(file=name,sha256=source['sha256'],columns=public_sources[name]['columns'],
                               total_rows=len(rows),sampled=len(rows)>128,
                               rows=[values for _,values in selected],source_lines=[line for line,_ in selected]))
            del rows,data
        value=dict(analysis_id=analysis_id,tables=tables,scientific_status='not_evaluated')
        if verified.get('structural_results'):value['structural_results']=verified['structural_results']
        if verified.get('site_thermodynamic_results'):
            from . import site_thermodynamics as site
            previews=[]
            for item,original in zip(verified['site_thermodynamic_results'],report['site_thermodynamic_results']):
                receipt=next(r for r in item['derived_files'] if r['name'].endswith('-curves.csv'))
                raw=site.read_derived(self.reports/analysis_id,receipt)
                previews.append(dict(id=item['id'],curves=site_curve_preview(raw,receipt,original['parameters'])))
            value.update(site_thermodynamic_results=verified['site_thermodynamic_results'],site_previews=previews)
        return value

    def chart_data(self, identifier, analysis_id, name, x, y):
        """Return a bounded, complete two-column view of one verified numeric source.

        This is presentation data after the frozen report, never a new fit or
        scientific verdict. The source receipt and bytes are checked again.
        """
        from .analysis import MAX_TABLE_BYTES, parse_table
        verified,report,ticket,inventory=self._verified_sources(identifier,analysis_id)
        source=next((item for item in report['sources'] if item['file']==name),None)
        public=next((item for item in verified['sources'] if item['file']==name),None)
        if source is None or public is None or source.get('format') is not None:
            raise ResultUnavailable('Only declared numeric tables can be redrawn')
        columns=source['columns']
        names=[column['name'] for column in columns]
        if not isinstance(x,str) or not isinstance(y,str) or x==y or x not in names or y not in names:
            raise ResultUnavailable('Choose two distinct declared numeric columns')
        item=inventory.get('output/'+name)
        if (item is None or item['sha256']!=source['sha256'] or item['size']!=source['size']
                or item['size']>MAX_TABLE_BYTES):
            raise ResultUnavailable('Numeric source differs from collected receipt')
        data=runtime.read_regular(self.collections/ticket/'payload'/'output'/name,item['size'])
        if len(data)!=item['size'] or sha256(data)!=item['sha256']:
            raise ResultUnavailable('Numeric source bytes changed')
        rows=parse_table(data,{'file':name,'columns':columns})
        xi,yi=names.index(x),names.index(y)
        buffer=io.StringIO(newline='')
        writer=csv.writer(buffer,lineterminator='\n')
        writer.writerow(['source_line',f"{x} ({columns[xi]['unit']})",f"{y} ({columns[yi]['unit']})"])
        for line,values in rows:
            writer.writerow([line,repr(values[xi]),repr(values[yi])])
        raw=buffer.getvalue().encode('utf-8')
        if len(raw)>MAX_TABLE_BYTES:raise ResultUnavailable('Redrawn data exceeds storage limit')
        indices=sorted({round(i*(len(rows)-1)/127) for i in range(min(128,len(rows)))}) if len(rows)>128 else range(len(rows))
        return dict(analysis_id=analysis_id,file=name,source_sha256=source['sha256'],
            x=x,y=y,x_unit=columns[xi]['unit'],y_unit=columns[yi]['unit'],
            rows=len(rows),sampled=len(rows)>128,
            preview=[[rows[i][1][xi],rows[i][1][yi]] for i in indices],
            csv_sha256=sha256(raw),csv_size=len(raw),csv=raw)

    def derived(self, identifier, analysis_id, name):
        """Complete task-scoped CSV bytes from one saved, accounted receipt only."""
        from . import site_thermodynamics as site
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}-(differences|sites|curves)\.csv',name):
            raise ResultUnavailable('Unsupported derived download label')
        verified,report,ticket,inventory=self._verified_sources(identifier,analysis_id)
        matches=[(item,r) for item in verified.get('site_thermodynamic_results',[])
                 for r in item['derived_files'] if r['name']==name]
        if len(matches)!=1:raise ResultUnavailable('This task has no declared derived CSV')
        item,receipt=matches[0];source=item['source'];collected=inventory.get('output/'+source['file'])
        if not collected or any(source[k]!=collected[k] for k in ('sha256','size')):
            raise ResultUnavailable('Derived source differs from collection')
        raw=runtime.read_regular(self.collections/ticket/'payload'/'output'/source['file'],site.MAX_ARRAY_BYTES)
        if len(raw)!=source['size'] or sha256(raw)!=receipt['source_sha256']:
            raise ResultUnavailable('Derived source bytes changed')
        del raw
        return site.read_derived(self.reports/analysis_id,receipt)

    def report(self, identifier, analysis_id):
        runtime.hash_value(analysis_id)
        for group in self.task(identifier)['evaluations']:
            for request in group['requests']:
                for report in request['reports']:
                    if report.get('id')==analysis_id and report['status']=='analyzed':return report
        raise ResultUnavailable('This task has no verified report with that identity')
