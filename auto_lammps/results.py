"""Task-scoped, read-only browser projection; never fetches or analyzes data."""
import json
import math
from pathlib import Path
import re

from .candidate_jobs import CandidateHistory
from .manifest import sha256
from . import runtime_launcher as runtime

STATES={'prepared':'尚未提交','dispatching':'正在提交','unknown':'提交状态待核对','accepted':'已提交',
        'queued':'排队中','running':'计算中','cancelling':'正在取消','completed':'计算已结束',
        'failed':'计算失败','cancelled':'已取消','timeout':'计算超时','rejected':'提交被拒绝',
        'cancelled_before_dispatch':'提交前已取消','reconcile_required':'记录存在矛盾 · 待核对'}
EVENTS={'reserved':'预留计算资源','dispatch_intent':'发起计算提交','scheduler_accepted':'收到作业编号',
        'scheduler_observed':'更新计算状态','accounting_final':'完成资源核算','dispatch_unknown':'提交状态待核对',
        'scheduler_rejected':'提交被拒绝','upload_intent':'开始传送输入','inputs_staged':'输入传送完成',
        'upload_failed':'输入传送未完成','output_fetch_started':'开始回收结果','output_fetch_finished':'保存回收记录',
        'analysis_reserved':'开始分析结果','analysis_saved':'保存分析记录','reconciliation_started':'核对调度记录',
        'reconciliation_finished':'保存调度核对','reconciliation_conflict':'调度记录存在矛盾',
        'scheduler_observation_conflict':'调度记录存在矛盾','cancel_intent':'请求取消',
        'cancelled_before_dispatch':'提交前已取消','following_registered':'开始自动跟进',
        'following_poll':'预留调度查询与记录空间'}
FOLLOWING={'waiting':'等待计算进展','collecting':'自动回收结果','analyzing':'自动分析结果',
           'analyzed':'自动分析已完成','analysis_failed':'自动分析未完成',
           'diagnostics_saved':'失败计算的诊断已保存','attention':'自动跟进需要核对'}


def event_label(event):
    if event['kind']=='following_progress':
        return FOLLOWING.get(json.loads(event['payload'])['state'],'自动跟进状态待核对')
    return EVENTS.get(event['kind'],'保存运行记录')


class ResultUnavailable(ValueError):
    pass


def existing_private_directory(path):
    path=Path(path).expanduser().absolute()
    fd=runtime.directory(path,private=True)
    import os
    os.close(fd)
    if any((parent/'.git').exists() for parent in (path,*path.parents)):
        raise ValueError('Result storage must be outside Git')
    return path


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
            return dict(id=identifier,status='analysis_failed',label='分析未完成',at=event['at'],
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
                                                       'source_line_ranges','values','value_units')})
        return dict(id=identifier,status='analyzed',label='数值分析已完成',at=event['at'],
                    quantity=report['declared_quantity'],results=results,
                    sources=[{key:item[key] for key in ('file','sha256','columns')} for item in report['sources']],
                    scientific_status='not_evaluated',
                    notes=['尚未核验科学结论；数值处理完成不等于研究目标已达成。',
                           '单位已与表头核对，仍需核验物理定义和脚本中的单位处理。',
                           '样本标准差描述数据波动，不代表独立重复实验的不确定度。'])

    def task(self, identifier):
        self.tasks.get(identifier)
        candidate=self.preparations.get(identifier)
        base=dict(configured=True,evaluations=[],scientific_status='not_evaluated')
        if not candidate or candidate['state']!='prepared':
            return dict(base,message='尚无已准备的计算方案。结果会在计算与分析完成后出现在这里。')
        if sha256(self.tasks.export(identifier))!=candidate['condition_sha256']:
            raise ResultUnavailable('Prepared conditions changed')
        digest=runtime.hash_value(candidate['result']['snapshot_sha256'])
        groups=self.ledger.product_results(digest)
        for group in groups:
            requests=[];ordinal=0
            for request in group['requests']:
                events=request['events'];reports=[]
                for event in events:
                    if event['kind']!='analysis_saved':continue
                    try:reports.append(self._report(request,event,events))
                    except (KeyError,ValueError,TypeError,AttributeError,OSError,runtime.ExecutionDenied):
                        reports.append(dict(status='unavailable',label='报告暂不可用',at=event['at'],
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
                requests.append(dict(id=request['id'],dispatch_ordinal=ordinal if request['dispatch_claimed'] else None,
                    state=request['state'],state_label=STATES.get(request['state'],'状态待核对'),stage=stage,
                    accounted=bool(request['accounted']),reports=reports,
                    history=[dict(at=e['at'],label=event_label(e)) for e in events]))
            pending=sum(r['state']=='prepared' and not r['dispatch_claimed'] for r in group['requests'])
            base['evaluations'].append(dict(id=group['id'],max_attempts=group['max_attempts'],
                dispatch_count=ordinal,pending_attempts=pending,used_attempts=ordinal+pending,requests=requests))
        return dict(base,message='计算状态、数值分析和科学核验分别记录。' if groups else '尚未提交计算，没有结果记录。')

    def report(self, identifier, analysis_id):
        runtime.hash_value(analysis_id)
        for group in self.task(identifier)['evaluations']:
            for request in group['requests']:
                for report in request['reports']:
                    if report.get('id')==analysis_id and report['status']=='analyzed':return report
        raise ResultUnavailable('This task has no verified report with that identity')
