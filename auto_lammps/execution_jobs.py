"""Durable operator execution intents using the existing accounted pipeline.

Deployment/evaluation bindings are controller-owned, never browser/model input.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

from .candidate_jobs import CandidateHistory
from .manifest import canonical, sha256
from .tasks import TaskError, StaleTask, task_id

ACTIVE={'queued','running','waiting'}
# 等待用户批准：工作线程仍需推进它（批准后自动放行），但它不属于“在飞行中”。
PENDING_APPROVAL={'awaiting_approval'}
WORKABLE=ACTIVE|PENDING_APPROVAL
# 记账类事件不参与状态判定（否则会把已完成/待处理的作业顶成未知状态）。
BOOKKEEPING={'config_rebased','request_replaced','recheck_failed'}
LABELS={'queued':'等待执行','running':'核验许可并推进计算','waiting':'自动跟进计算',
        'analyzed':'数值分析完成','analysis_failed':'分析未完成','diagnostics_saved':'计算未成功，诊断已保存',
        'attention':'执行需要核对','rejected':'提交被拒绝','config_rebased':'已按当前部署重新基线',
        'awaiting_approval':'等待你批准方案',
        'request_replaced':'已按当前方案重新预留请求','recheck_failed':'重新核对未通过',
        'stale_intent_cancelled':'旧意图已作废，按当前方案重新预留'}


class ExecutionJobs:
    def __init__(self, controller, bindings, *, enrollment=None):
        self.controller=controller;self.tasks=controller.tasks;self.ledger=controller.ledger
        self.bindings=dict(bindings);self.history=CandidateHistory(self.tasks)
        self.enrollment=enrollment
        if enrollment is not None and (enrollment.tasks.path!=self.tasks.path or enrollment.ledger.path!=self.ledger.path):
            raise ValueError('Enrollment must use the same task store and ledger')
        from .runtime_launcher import hash_value
        for key,value in self.bindings.items():task_id(key);hash_value(value)
        # Bind deployment semantics, not unrelated webpage edits or other tasks.
        config=dict(ledger=str(self.ledger.path.absolute()),snapshots=str(controller.snapshots),
            authorization=controller.authorization.policy_sha256,
            authorization_directory=str(controller.authorization.directory),
            environment=asdict(controller.environment),following=controller.following.config_sha256,
            stage=asdict(controller.staging.client.endpoint),submit=asdict(controller.submission.scheduler.endpoint),
            runtime_profile=str(controller.runtime_profile_path),
            sources={name:sha256((Path(__file__).parent/name).read_bytes())
                     for name in ('execution_jobs.py','execution.py','submission.py','staging.py','slurm_submit.py')})
        if enrollment is not None:config['enrollment']=enrollment.identity
        self.config_sha256=sha256(canonical(config))
        self.stop=threading.Event();self.wake=threading.Event();self.thread=None
        self._reprepare=set()
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution_jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), '
                       'revision INTEGER NOT NULL, evaluation TEXT NOT NULL, request_id TEXT NOT NULL, config_sha256 TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS execution_job_events (job_id TEXT NOT NULL REFERENCES execution_jobs(id), '
                       'seq INTEGER NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(job_id,seq))')
            for table in ('execution_jobs','execution_job_events'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable execution history'); END")

    def evaluation_for(self, identifier):
        explicit=self.bindings.get(identifier)
        registered=self.enrollment.get(identifier) if self.enrollment else None
        if explicit and registered and explicit!=registered['evaluation']:
            raise TaskError('任务执行身份冲突，原提交历史保留。')
        return explicit or (registered['evaluation'] if registered else None)

    def register_for_generation(self, identifier, revision):
        if identifier in self.bindings:
            return self.evaluation_for(identifier)
        if self.enrollment is not None:
            return self.enrollment.register(identifier,revision)
        return None

    def _event(self, db, job, state, reason=''):
        previous=db.execute('SELECT seq,state,reason FROM execution_job_events WHERE job_id=? ORDER BY seq DESC LIMIT 1',(job,)).fetchone()
        if previous and (previous['state'],previous['reason'])==(state,reason):return
        seq=previous['seq']+1 if previous else 1
        db.execute('INSERT INTO execution_job_events VALUES (?,?,?,?,?)',
                   (job,seq,datetime.now(timezone.utc).isoformat(),state,reason))

    def get(self, identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            row=db.execute('SELECT * FROM execution_jobs WHERE task_id=?',(identifier,)).fetchone()
            if row is None:return None
            events=[dict(r) for r in db.execute('SELECT * FROM execution_job_events WHERE job_id=? ORDER BY seq',(row['id'],))]
        significant=[e for e in events if e['state'] not in BOOKKEEPING] or events
        return dict(row)|{'events':events,'state':significant[-1]['state'],'reason':significant[-1]['reason']}

    def status(self, identifier):
        job=self.get(identifier)
        live=bool(self.thread and self.thread.is_alive())
        if job:
            evaluation=self.ledger.evaluation_snapshot(job['evaluation'])
            current_request=self.current_request_id(identifier)
            row=self.ledger.get(current_request)
            return dict(configured=True,worker_alive=live,can_start=False,job=dict(
                state=job['state'],label=LABELS.get(job['state'],job['state']),request_id=current_request,job_id=row['job_id'],
                scheduler_state=row['state'],accounted=bool(row['accounted']),
                dispatch_count=evaluation['dispatch_claims'],max_attempts=evaluation['max_attempts'],
                scientific_status='not_evaluated',reason=job['reason'],
                events=[dict(at=e['at'],label=LABELS.get(e['state'],e['state']),reason=e['reason']) for e in job['events']]))
        candidate=self.history.get(identifier)
        binding=self.evaluation_for(identifier)
        ready=binding is not None and candidate is not None and candidate['state']=='prepared'
        evaluation=self.ledger.evaluation_snapshot(binding) if binding else None
        return dict(configured=binding is not None or self.enrollment is not None,worker_alive=live,can_start=ready,job=None,
                    submissions=dict(count=evaluation['dispatch_claims'],maximum=evaluation['max_attempts']) if evaluation else None,
                    message='方案已准备，可开始计算。' if ready else '确认需求并生成方案后可开始计算。' if self.enrollment is not None and binding is None else '计算部署尚未绑定此任务。' if binding is None else '计算方案尚未准备完成。')

    def current_request_id(self, identifier):
        """作业行里的 request_id 不可改；被替换过时以最新 request_replaced 事件为准。"""
        job = self.get(identifier)
        if job is None:
            return None
        current = job['request_id']
        for event in job['events']:
            if event['state'] == 'request_replaced' and event['reason']:
                current = event['reason']
        return current

    def request_replaced(self, identifier):
        job = self.get(identifier)
        return bool(job) and any(e['state'] == 'request_replaced' for e in job['events'])

    def plan_scope(self, identifier):
        """当前已准备方案的摘要；没有已准备方案时返回 None。"""
        job = self.history.get(identifier)
        if job is None or job['state'] != 'prepared':
            return None
        digest = (job.get('result') or {}).get('snapshot_sha256')
        return 'plan:' + digest if isinstance(digest, str) and digest else None

    def approved_plan(self, identifier):
        scope = self.plan_scope(identifier)
        return bool(scope) and self.tasks.plan_approved(identifier, scope)

    def enqueue(self, identifier, revision):
        doc=self.tasks.get(identifier)
        if doc['revision']!=revision:raise StaleTask('任务已变化，请刷新后再开始。')
        job=self.get(identifier)
        if job:return self.status(identifier)
        # 第一道人工关卡：用户必须先看到方案并批准，才允许提交真实计算。
        if not self.approved_plan(identifier):
            raise TaskError('请先审阅并批准当前方案，再提交计算。方案一旦变化需要重新批准。')
        evaluation=self.evaluation_for(identifier)
        if evaluation is None:raise TaskError('此任务尚未接入已核验的计算部署。')
        # Only the controller can select evaluation and reserve the existing key.
        plan=self.controller.prepare(identifier,evaluation)
        with self.tasks.transaction() as db:
            latest=self.tasks._read(db,identifier)
            if latest['revision']!=revision:raise StaleTask('任务已变化，请刷新后再开始。')
            row=db.execute('SELECT id FROM execution_jobs WHERE task_id=?',(identifier,)).fetchone()
            if row is None:
                job=uuid.uuid4().hex
                db.execute('INSERT INTO execution_jobs VALUES (?,?,?,?,?,?)',(job,identifier,revision,
                    evaluation,plan['row']['id'],self.config_sha256))
                self._event(db,job,'queued')
        self.wake.set()
        return self.status(identifier)

    def recheck(self, identifier, revision):
        """用户显式要求重新核对一个"需要处理"的执行作业。

        追加事件（含必要的配置再基线），不改写任何历史；随后由工作线程重新核验并派发。
        """
        doc=self.tasks.get(identifier)
        if doc['revision']!=revision:raise StaleTask('任务已更新，请刷新后再试。')
        job=self.get(identifier)
        if job is None:raise TaskError('尚无执行记录，请先开始计算。')
        with self.history.lease(job['id']) as acquired:
            if not acquired:return self.status(identifier)
            job=self.get(identifier)
            if job['state'] in WORKABLE:return self.status(identifier)
            current_id=self.current_request_id(identifier)
            row=self.ledger.get(current_id)
            # Accepted/unknown submissions are followed as-is, never replaced.
            if not row['dispatch_claimed']:
                # Retain this exact evaluation's history and storage reservation.
                self.controller.cancel_stale_intent(current_id)
                plan=self.controller.prepare(identifier,job['evaluation'],allow_reprepare=True)
                new_id=plan['row']['id']
            else:
                new_id=current_id
            with self.tasks.transaction() as db:
                if new_id!=current_id:self._event(db,job['id'],'request_replaced',new_id)
                self._event(db,job['id'],'config_rebased',self.config_sha256)
                self._event(db,job['id'],'queued','user_recheck')
        self.wake.set()
        return self.status(identifier)

    def advance(self, identifier):
        job=self.get(identifier)
        if job is None or job['state'] not in WORKABLE:return self.status(identifier)
        # The OS lease, rather than a timestamp or persisted running flag, owns work.
        with self.history.lease(job['id']) as acquired:
            if not acquired:return self.status(identifier)
            job=self.get(identifier)
            if job['state'] not in WORKABLE:return self.status(identifier)
            if not self.approved_plan(identifier):
                # 方案变了或还没批准：停在"等待批准"，绝不派发。
                with self.tasks.transaction() as db:self._event(db,job['id'],'awaiting_approval','plan_not_approved')
                return self.status(identifier)
            baseline=job['config_sha256']
            for event in job['events']:
                if event['state']=='config_rebased' and event['reason']:
                    baseline=event['reason']
            if baseline!=self.config_sha256 or self.evaluation_for(identifier)!=job['evaluation']:
                with self.tasks.transaction() as db:self._event(db,job['id'],'attention','deployment_changed')
                return self.status(identifier)
            if job['state']=='queued':
                with self.tasks.transaction() as db:self._event(db,job['id'],'running')
            try:
                current_id=self.current_request_id(identifier)
                row=self.ledger.get(current_id)
                if row['evaluation']!=job['evaluation']:
                    raise ValueError('Execution evaluation changed')
                if row['dispatch_claimed']:
                    result=self.controller.following.advance(current_id)
                else:
                    allow=self.request_replaced(identifier)
                    plan=(self.controller.prepare(identifier,job['evaluation'],allow_reprepare=True)
                          if allow else self.controller.prepare(identifier,job['evaluation']))
                    if plan['row']['id']!=current_id:
                        raise ValueError('Execution identity changed before dispatch')
                    result=(self.controller.advance(identifier,job['evaluation'],allow_reprepare=True)
                            if allow else self.controller.advance(identifier,job['evaluation']))
                if result.get('request_id')!=current_id:raise ValueError('Execution identity changed')
                state=result['state']
                if state not in LABELS:raise ValueError('Unexpected execution state')
                # Only known adapter codes, never exception messages or remote text.
                reason=result.get('reason','')
                import re
                if not isinstance(reason,str) or not re.fullmatch(r'[a-z_]{0,80}',reason):reason='adapter_attention'
            except Exception as exc:
                # 只暴露异常类别（安全字符），否则"执行需要核对"无法定位问题；
                # 完整回溯只写日志（私有），不进入事件与页面。
                import sys as _sys, traceback as _tb
                _tb.print_exc(file=_sys.stderr)
                import re as _re
                kind=_re.sub(r'[^a-z]','',type(exc).__name__.lower())[:24] or 'error'
                state='attention';reason='execution_check_failed_'+kind
                # Class name only; private paths, grants and remote output stay private.
                if isinstance(exc,FileNotFoundError):reason='deployment_file_missing'
            with self.tasks.transaction() as db:self._event(db,job['id'],state,reason)
        return self.status(identifier)

    def _run(self):
        while not self.stop.is_set():
            with self.tasks.transaction() as db:
                tasks=[r['task_id'] for r in db.execute('SELECT task_id FROM execution_jobs ORDER BY rowid')]
            for identifier in tasks:
                if self.stop.is_set():break
                try:
                    self.advance(identifier)
                except Exception as error:
                    # 单个任务的问题绝不能杀死整个执行工作线程（此前正是这样：
                    # 一个任务绑定失效会让所有任务都不再被跟进）。
                    import sys as _sys, traceback as _tb
                    _tb.print_exc(file=_sys.stderr)
                    with self.tasks.transaction() as db:
                        job=self.get(identifier)
                        if job is not None:
                            self._event(db,job['id'],'attention',
                                        'worker_error_'+type(error).__name__.lower()[:24])
            self.wake.wait(5);self.wake.clear()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop.clear();self.thread=threading.Thread(target=self._run,name='research-execution',daemon=True);self.thread.start()

    def close(self):
        self.stop.set();self.wake.set()
        if self.thread:self.thread.join()


def load_execution_jobs(tasks, ledger, path):
    """Read private deployment; constructing the service never submits or queries."""
    from .analysis_v2 import VersionedAnalysisService
    from .batch_plan import BatchEnvironment
    from .execution import CandidateExecution, ExistingAuthorization
    from .following import FollowingService
    from .outputs import OutputCollector
    from .reconciliation import ReconciliationService
    from .runtime_launcher import read_regular
    from .slurm_read import SlurmReader
    from .slurm_submit import SlurmSubmitter
    from .staging import StageEndpoint, StageClient, StagingService
    from .submission import SubmissionService
    value=json.loads(read_regular(path,100000,private=True))
    required={'snapshots_directory','collections_directory','reports_directory','audit_directory',
              'stage_endpoint','submit_endpoint','collect_endpoint','environment','authorization',
              'runtime_profile_path','max_polls','interval_seconds','query_max_bytes','task_evaluations'}
    optional={'hpc_connection_revision','research_enrollment','authorization_mode'}
    if not required<=set(value) or set(value)-required-optional:raise ValueError('Invalid execution deployment fields')
    audit=Path(value['audit_directory'])
    stage=StageEndpoint(**value['stage_endpoint']);submit=StageEndpoint(**value['submit_endpoint']);collect=StageEndpoint(**value['collect_endpoint'])
    transport=None
    if 'hpc_connection_revision' in value:
        from .hpc_connections import HPCConnections
        from .hpc_transport import SavedHPCTransport
        transport=SavedHPCTransport(HPCConnections(tasks),value['hpc_connection_revision'],stage.host_alias)
        profile=transport.identity['profile']
        root=Path(profile['work_directory'])
        if any(root not in Path(e.root_path).parents for e in (stage,submit,collect)):
            raise ValueError('Request storage must be inside the saved HPC work directory')
        if (profile['partition']!=value['environment']['partition'] or
                (profile['account'] or None)!=value['environment']['account']):
            raise ValueError('Deployment partition/account differs from saved HPC settings')
    following=FollowingService(ledger,ReconciliationService(ledger,SlurmReader(collect.host_alias,audit/'queries',max_bytes=value['query_max_bytes'],transport=transport)),
        VersionedAnalysisService(OutputCollector(ledger,collect,value['collections_directory'],transport=transport),value['reports_directory']),
        value['snapshots_directory'],max_polls=value['max_polls'],interval_seconds=value['interval_seconds'])
    mode=value.get('authorization_mode','existing')
    if mode=='existing': authorization=ExistingAuthorization(**value['authorization'])
    elif mode=='automatic':
        from .authorization import AutomaticAuthorization
        authorization=AutomaticAuthorization(**value['authorization'],ledger=ledger,endpoint=submit,
            audit_directory=audit/'authorizations',transport=transport)
    else: raise ValueError('Unknown authorization mode')
    controller=CandidateExecution(tasks,ledger,value['snapshots_directory'],
        StagingService(ledger,StageClient(stage,audit/'uploads',transport=transport)),
        SubmissionService(ledger,SlurmSubmitter(ledger,submit,audit/'dispatch',transport=transport)),following,
        authorization,BatchEnvironment(**value['environment']),
        runtime_profile_path=value['runtime_profile_path'])
    enrollment=None
    if 'research_enrollment' in value:
        from .research_enrollment import ResearchEnrollment
        enrollment=ResearchEnrollment(tasks,ledger,**value['research_enrollment'])
    return ExecutionJobs(controller,value['task_evaluations'],enrollment=enrollment)
