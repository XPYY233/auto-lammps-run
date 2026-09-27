"""Operator-only reference evidence and model preferences; no execution or API calls."""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from .manifest import sha256
from .results import existing_private_directory, ResultUnavailable
from . import runtime_launcher as runtime
from .tasks import TaskError, task_id, text

PROVIDERS = {'deepseek-official':'DeepSeek', 'glm':'GLM', 'anthropic':'Claude', 'openai':'GPT'}


class ModelPreferences:
    """Preferences are not a model policy, credential store or spending permission."""
    def __init__(self, tasks):
        self.tasks=tasks
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operator_model_preferences '
                       '(revision INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, at TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_model_preferences_{action} "
                           f"BEFORE {action} ON operator_model_preferences BEGIN SELECT RAISE(ABORT, 'immutable preferences'); END")

    def get(self):
        with self.tasks.transaction() as db:
            row=db.execute('SELECT * FROM operator_model_preferences ORDER BY revision DESC LIMIT 1').fetchone()
        return (dict(row) if row else dict(revision=0,provider='deepseek-official',model='',at=None)) | {
            'providers':PROVIDERS, 'activates_runtime':False}

    def save(self, provider, model, revision):
        if provider not in PROVIDERS or not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{0,100}',model):
            raise TaskError('请选择模型商，模型名称只能包含字母、数字及 . _ : / -')
        with self.tasks.transaction() as db:
            latest=db.execute('SELECT MAX(revision) FROM operator_model_preferences').fetchone()[0] or 0
            if type(revision) is not int or revision!=latest:
                from .tasks import StaleTask
                raise StaleTask('模型偏好已更新，请刷新后保存')
            db.execute('INSERT INTO operator_model_preferences VALUES (?,?,?,?)',
                       (latest+1,provider,model,datetime.now(timezone.utc).isoformat()))
        return self.get()


class ReferenceViews:
    """Private controller-produced report, joined to exact existing reference ledger.

    Deliberately separate from model input, normal results and scientific pass publication.
    No browser endpoint can register reports or bind evaluations.
    """
    def __init__(self, directory, papers):
        self.directory=existing_private_directory(directory)
        self.papers=papers
        if papers.ledger is None:raise ValueError('Reference views require the existing ledger')

    def _load(self, identifier):
        task_id(identifier)
        self.papers.tasks.get(identifier)
        folder=self.directory/identifier
        if not folder.exists():return None,None
        existing_private_directory(folder)
        raw=runtime.read_regular(folder/'report.json',262144,private=True)
        report=json.loads(raw)
        if report.get('version')!=1 or report.get('task_id')!=identifier or report.get('scientific_status')!='diagnostic':
            raise ResultUnavailable('Invalid reference report identity or verdict')
        paper=self.papers.get(report['paper_id'])
        if report['title']!=paper['title'] or report['doi']!=paper['doi'] or identifier not in [t['id'] for t in paper['tasks']]:
            raise ResultUnavailable('Reference report belongs to another paper')
        matches=[e for e in paper['evaluations'] if e['id']==report['evaluation'] and e.get('available')]
        if len(matches)!=1 or matches[0]['identity']['role']!='reference':raise ResultUnavailable('No reference binding')
        request=self.papers.ledger.get(report['request_id'])
        if (request['evaluation']!=report['evaluation'] or request['state']!='completed' or not request['accounted']
                or request['job_id']!=report['job_id'] or request['manifest_sha256']!=report['manifest_sha256']):
            raise ResultUnavailable('Reference is not completed and accounted')
        files=report['files']
        if not isinstance(files,list) or not 1<=len(files)<=12:raise ResultUnavailable('Invalid reference files')
        names=set();total=0
        for item in files:
            name=item['name']
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}',name) or name in names or Path(name).suffix not in {'.json','.csv','.md','.png','.pdf'}:
                raise ResultUnavailable('Invalid reference filename')
            names.add(name)
            data=runtime.read_regular(folder/name,5*1024*1024,private=True)
            total+=len(data)
            if total>12*1024*1024 or sha256(data)!=item['sha256'] or len(data)!=item['size']:
                raise ResultUnavailable('Reference artifact changed')
        # A hashed diagnostic receipt is mandatory, not just browser-facing numbers.
        if report['analysis_file'] not in names:raise ResultUnavailable('Missing diagnostic evidence')
        evidence=json.loads(runtime.read_regular(folder/report['analysis_file'],262144,private=True))
        if evidence['runtime']['job_id']!=request['job_id'] or evidence['runtime']['core_hours']*3600!=request['actual_core_seconds']:
            # Account for representation rounding, not changed usage.
            if evidence['runtime']['job_id']!=request['job_id'] or not math.isclose(evidence['runtime']['core_hours']*3600,request['actual_core_seconds'],abs_tol=1e-6):
                raise ResultUnavailable('Diagnostic accounting differs')
        if report.get('paper_evidence_file') not in names:raise ResultUnavailable('Missing paper evidence')
        paper_evidence=json.loads(runtime.read_regular(folder/report['paper_evidence_file'],262144,private=True))
        if paper_evidence.get('doi')!=paper['doi']:raise ResultUnavailable('Wrong paper evidence')
        targets=paper_evidence['primary_stage_reference_proposal']
        metrics=[]
        for metric in report['metrics']:
            if len(metrics)>=32:raise ResultUnavailable('Too many metrics')
            pv,av=metric['paper'],metric['reference']
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (pv,av)):
                raise ResultUnavailable('Nonfinite comparison')
            if metric.get('paper_key') not in {'tensile_strength_GPa','elastic_modulus_GPa'} or pv!=targets[metric['paper_key']]:
                raise ResultUnavailable('Comparison differs from retained paper evidence')
            channel=evidence[metric['channel']]
            actual=(channel['peak']['values']['max'] if metric['operation']=='peak' else
                    channel['fits'][metric['window_end']]['values']['slope'])
            if av!=actual:raise ResultUnavailable('Comparison differs from diagnostic result')
            metrics.append({k:metric[k] for k in ('label','paper','reference','unit','method','channel','operation')} |
                           {'absolute_difference':abs(pv-av),'relative_difference_percent':100*abs(pv-av)/abs(pv) if pv else None})
        curves=[]
        for curve in report['curves']:
            values=evidence[curve['channel']]['curve']
            if not isinstance(values,list) or not 1<=len(values)<=10000 or any(len(r)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in r) for r in values):
                raise ResultUnavailable('Invalid curve')
            curves.append({'label':text(curve['label'],200),'points':values})
        public={k:report[k] for k in ('task_id','paper_id','title','doi','scope','summary','limitations','at','job_id','scientific_status','stages')}
        bound=[e for e in paper['evaluations'] if e['task_id']==identifier]
        agents=[e for e in bound if e.get('available') and e['identity']['role']=='agent']
        available=all(e.get('available') for e in bound)
        claims=sum(e['dispatch_claims'] for e in agents) if available else None
        requests=[r for e in agents for r in e.get('requests',[])]
        stage=next(({'running':'正在计算','queued':'排队中','accepted':'调度已接受',
                     'completed':'计算结束，待核验','failed':'计算失败','unknown':'提交状态待核对',
                     'timeout':'计算超时'}.get(r['state'],'已有计算记录') for r in reversed(requests)),
                   '已建立评测，尚未提交' if agents else '策略待确认，尚未提交')
        agent_progress=dict(available=available,dispatch_claims=claims,evaluations=agents,
                            stage=stage if available else '记录暂不可核验')
        public.update(metrics=metrics,curves=curves,files=[{k:f[k] for k in ('name','label','size')} for f in files],
                      runtime={k:evidence['runtime'][k] for k in ('elapsed_seconds','cores','hours','core_hours','B_max_core_hours','prior_failure_core_seconds')},
                      evaluation=matches[0],agent_progress=agent_progress,report_sha256=sha256(raw))
        return public,folder

    def get(self, identifier):
        return self._load(identifier)[0]

    def download(self, identifier, name):
        report,folder=self._load(identifier)
        if report is None or name not in [f['name'] for f in report['files']]:raise KeyError('No declared artifact')
        # Re-read and verify against the manifest before returning bytes, never FileResponse paths.
        raw=runtime.read_regular(folder/'report.json',262144,private=True)
        if sha256(raw)!=report['report_sha256']:raise ResultUnavailable('Report changed during download')
        manifest=json.loads(raw)
        item=next(f for f in manifest['files'] if f['name']==name)
        data=runtime.read_regular(folder/name,5*1024*1024,private=True)
        if sha256(data)!=item['sha256']:raise ResultUnavailable('Artifact changed')
        return data
