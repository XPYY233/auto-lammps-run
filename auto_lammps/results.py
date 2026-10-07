"""Task-scoped, read-only browser projection; never fetches or analyzes data."""
import json
import math
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
        sources=[];structural_sources={}
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
            else:
                sources.append({key:item[key] for key in ('file','sha256','columns')})
        if len({s['file'] for s in sources})!=len(sources):
            raise ResultUnavailable('Duplicate analysis sources')
        structural_results=[]
        for item in report.get('structural_results',[]):
            if item['file'] not in structural_sources:
                raise ResultUnavailable('Structural result has no matching dump source')
            structural_results.append(structural_projection(item,structural_sources[item['file']]))
        if structural_sources and (report.get('adapter_version')!=3
                or {s['file'] for s in structural_results}!=set(structural_sources)):
            raise ResultUnavailable('Structural source lacks versioned analysis')
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

    def tables(self, identifier, analysis_id):
        """Bounded previews of verified source bytes, never new analysis or fetching."""
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
        from .analysis import parse_table, MAX_TABLE_BYTES
        from .scalar_analysis import parse_scalar
        tables=[];total=0
        is_structural=verified.get('analysis_format')=='numeric_tables_v3'
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
        if is_structural:value['structural_results']=verified['structural_results']
        return value

    def report(self, identifier, analysis_id):
        runtime.hash_value(analysis_id)
        for group in self.task(identifier)['evaluations']:
            for request in group['requests']:
                for report in request['reports']:
                    if report.get('id')==analysis_id and report['status']=='analyzed':return report
        raise ResultUnavailable('This task has no verified report with that identity')
