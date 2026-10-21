"""Durable preparation intents and append-only events; no scheduler access."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from .agent_candidates import (CandidateError, PlanIterationLimit, MAX_PROPOSAL_ROUNDS,
                               generate_research_candidate, research_inputs, output_prefix,
                               validate_execution_budget)
from .deepseek import ModelError
from .ledger import Resources
from .manifest import ManifestError, Snapshot, canonical, private_directory, read_file, root_descriptor, sha256
from .potentials import PotentialError
from .plan_review import normalized_review_issues
from .structures import StructureError, geometry_runtime
from .tasks import TaskError, task_id

MAX_REVIEW_REQUESTS = 3
BOOKKEEPING = {'candidate_review_request', 'candidate_model_review', 'review_recovery_requested',
               'review_request', 'model_review', 'config_rebased', 'clarification_answered',
               'model_proposal', 'proposal_request', 'development_recovery_authorized',
               'review_unknown_disposition','saved_plan_selected'}
ACTIVE = {'diagnosing_failure','reusing_plan', 'running', 'model_requested', 'checking_plan', 'repairing_plan', 'preparing_files'}
LABELS = {'proposal_request':'方案生成轮次已登记', 'model_proposal':'方案版本已保存', 'reusing_plan':'沿用上一版方案并重新检查', 'queued': '等待准备', 'running': '核对准备条件', 'model_requested': '生成计算方案',
          'review_recovery_requested':'同一原稿重审已登记', 'review_request':'AI 原稿审核请求已记账',
          'model_review':'AI 原稿审核结果已保存', 'candidate_review_request':'方案审核请求已登记',
          'candidate_model_review':'方案审核结果已保存',
          'diagnosing_failure':'AI 正在读取失败日志并诊断原因',
          'checking_plan':'核对需求与方案', 'repairing_plan':'自动修正方案', 'preparing_files': '准备结构与输入文件', 'prepared': '方案已准备 · 待核验',
          'clarification': '需要补充条件', 'failed': '准备未完成', 'interrupted': '准备中断 · 待核对',
          'configuration_changed': '配置已变化 · 待核对', 'clarification_answered': '已收到补充答复', 'config_rebased': '已按当前配置重新基线'}
ERRORS = {'model_budget_exhausted': '模型额度已用完，没有自动重试。',
          'review_iteration_limit': '已达到同稿重审三次上限，保留原稿、审核和费用。',
          'plan_iteration_limit': '已达到首版在内三轮方案上限，不能继续追加。已有方案和失败历史保留。',
          'model_key_missing_or_invalid': '模型密钥尚未配置，请联系管理员。',
          'request_already_reserved': '已有模型请求记录，需要核对，未重复调用。',
          'model_transport_unknown': '调用状态未确认，保留记录且不自动重试。',
          'model_generation_failed': '模型未返回完整有效方案，原有记录已保留。',
          'candidate_validation_failed': '方案或资源检查未通过，原始模型回答已保留。',
          'preparation_failed': '文件准备未完成，记录已保留，请核对服务状态。'}


def proposal_rounds(events):
    """Count immutable request identities, including legacy saved proposals."""
    ids = {event['payload']['request_id'] for event in events
           if event['state'] in {'proposal_request', 'model_proposal'}
           and event['payload'].get('request_id')}
    unknown = not ids and any(event['state']=='prepared' for event in events)
    grants = [e['payload'] for e in events if e['state'] == 'development_recovery_authorized']
    limit = MAX_PROPOSAL_ROUNDS + (grants[0]['additional_rounds'] if grants else 0)
    result = {'limit':limit, 'used':len(ids),
              'remaining':0 if unknown else max(0,limit-len(ids)),
              'historical_count_unknown':unknown}
    if grants:
        result.update(original_limit=MAX_PROPOSAL_ROUNDS,
                      development_recovery={'authorization_id':grants[0]['authorization_id'],
                          'limit':grants[0]['additional_rounds'],
                          'used':max(0,len(ids)-MAX_PROPOSAL_ROUNDS)},
                      within_original_limit=False)
    return result


def review_rounds(events):
    """Every recovery review call, including report repair, spends a persistent slot."""
    ids={e['payload']['request_id'] for e in events
         if e['state']=='review_request' and e['payload'].get('request_id')}
    return {'limit':MAX_REVIEW_REQUESTS,'used':len(ids),
            'remaining':max(0,MAX_REVIEW_REQUESTS-len(ids))}


class CandidateHistory:
    def __init__(self, tasks):
        self.tasks = tasks
        self.locks = private_directory(tasks.path.parent / 'candidate-locks')
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS candidate_jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE '
                       'REFERENCES tasks(id), revision INTEGER NOT NULL, condition_sha256 TEXT NOT NULL, '
                       'config_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS candidate_events (job_id TEXT NOT NULL REFERENCES candidate_jobs(id), '
                       'sequence INTEGER NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, '
                       'PRIMARY KEY(job_id,sequence))')
            for table in ('candidate_jobs', 'candidate_events'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable candidate history'); END")

    def _event(self, db, job, state, payload=None):
        sequence = db.execute('SELECT count(*) FROM candidate_events WHERE job_id=?', (job,)).fetchone()[0] + 1
        db.execute('INSERT INTO candidate_events VALUES (?,?,?,?,?)',
                   (job, sequence, datetime.now(timezone.utc).isoformat(), state, canonical(payload or {}).decode()))

    def get(self, identifier):
        with self.tasks.transaction() as db:
            self.tasks._read(db, identifier)
            row = db.execute('SELECT * FROM candidate_jobs WHERE task_id=?', (identifier,)).fetchone()
            if row is None:
                return None
            job = dict(row)
            events = [dict(row) for row in db.execute('SELECT * FROM candidate_events WHERE job_id=? ORDER BY sequence', (job['id'],))]
        for event in events:
            event['payload'] = json.loads(event['payload'])
            event['label'] = LABELS.get(event['state'], event['state'])
        # 记账类事件（重新基线、答复登记）不改变准备状态，否则会把已准备的方案顶回"需要处理"。
        significant = [e for e in events if e['state'] not in BOOKKEEPING] or events
        return {**job, 'state': significant[-1]['state'], 'label': significant[-1]['label'],
                'updated_at': significant[-1]['at'], 'result': significant[-1]['payload'], 'events': events,
                'proposal_rounds':proposal_rounds(events), 'review_rounds':review_rounds(events),
                'execution_authorized': False}

    def reserve_proposal(self, identifier, request_id, kind):
        """Reserve before a model call; restart/rebaseline cannot reset the cap."""
        with self.tasks.transaction() as db:
            row=db.execute('SELECT id FROM candidate_jobs WHERE task_id=?',(identifier,)).fetchone()
            if row is None:
                raise CandidateError('Proposal request needs a saved preparation identity')
            events=[{'state':r['state'],'payload':json.loads(r['payload'])} for r in
                    db.execute('SELECT state,payload FROM candidate_events WHERE job_id=? ORDER BY sequence',(row['id'],))]
            existing={e['payload'].get('request_id') for e in events
                      if e['state'] in {'proposal_request','model_proposal'}}
            budget=proposal_rounds(events)
            if request_id in existing:
                return budget
            if budget['remaining']<=0:
                raise PlanIterationLimit('方案已达到首版在内三轮上限，或历史轮次无法核验；不得继续追加。')
            self._event(db,row['id'],'proposal_request',
                        {'request_id':request_id,'kind':kind,'round':budget['used']+1,'limit':budget['limit'],
                         **({'development_recovery':True} if budget.get('development_recovery') else {})})
            return {**budget,'used':budget['used']+1,'remaining':budget['remaining']-1}

    def reserve_review(self, identifier, request_id, intent):
        """Reserve the unchanged-proposal review independently of generation rounds."""
        with self.tasks.transaction() as db:
            row=db.execute('SELECT id FROM candidate_jobs WHERE task_id=?',(identifier,)).fetchone()
            if row is None: raise CandidateError('Review requires an existing preparation identity')
            events=[{'state':r['state'],'payload':json.loads(r['payload'])} for r in
                    db.execute('SELECT state,payload FROM candidate_events WHERE job_id=? ORDER BY sequence',(row['id'],))]
            linked=[e['payload'] for e in events if e['state']=='review_recovery_requested'
                    and e['payload'].get('request_id')==intent['request_id']]
            if linked!=[intent]: raise CandidateError('Review intent changed or was not saved')
            prior=[e['payload'] for e in events if e['state']=='review_request'
                   and e['payload'].get('request_id')==request_id]
            if prior:
                if prior[0]['intent_id']!=intent['request_id']:
                    raise CandidateError('Review request belongs to another intent')
                return review_rounds(events)
            budget=review_rounds(events)
            if budget['remaining']<=0: raise ModelError('review_iteration_limit')
            self._event(db,row['id'],'review_request',
                {'request_id':request_id,'intent_id':intent['request_id'],
                 'proposal_sha256':intent['proposal_sha256'],'round':budget['used']+1})
            return {**budget,'used':budget['used']+1,'remaining':budget['remaining']-1}

    def authorize_development_recovery(self, grant):
        """Append an administrator's explicit amendment; never enqueue or erase history.

        No browser/model can submit this grant. It binds the original three spent
        request identities and frozen conditions, and survives removal from config.
        """
        fields = {'task_id', 'condition_sha256', 'original_request_ids',
                  'authorization_id', 'approved_at', 'additional_rounds'}
        if not isinstance(grant,dict) or set(grant) != fields:
            raise CandidateError('Invalid development recovery authorization')
        identifier=task_id(grant['task_id']); task_id(grant['authorization_id'])
        requests=grant['original_request_ids']
        if (not isinstance(requests,list) or len(requests)!=MAX_PROPOSAL_ROUNDS
                or len(set(requests))!=MAX_PROPOSAL_ROUNDS
                or requests!=sorted(requests)):
            raise CandidateError('Recovery must bind all original proposal requests')
        for key in requests: task_id(key)
        if (type(grant['additional_rounds']) is not int or not 1<=grant['additional_rounds']<=3
                or not isinstance(grant['condition_sha256'],str)
                or len(grant['condition_sha256'])!=64
                or any(c not in '0123456789abcdef' for c in grant['condition_sha256'])):
            raise CandidateError('Invalid bounded recovery scope')
        try:
            approved_at=datetime.fromisoformat(grant['approved_at'])
            if approved_at.tzinfo is None: raise ValueError()
        except (ValueError,TypeError):
            raise CandidateError('Recovery authorization needs a dated human approval') from None
        with self.tasks.transaction() as db:
            row=db.execute('SELECT * FROM candidate_jobs WHERE task_id=?',(identifier,)).fetchone()
            if row is None or row['condition_sha256']!=grant['condition_sha256']:
                raise CandidateError('Recovery does not match the original frozen task')
            events=[{'state':r['state'],'payload':json.loads(r['payload'])} for r in
                    db.execute('SELECT state,payload FROM candidate_events WHERE job_id=? ORDER BY sequence',(row['id'],))]
            existing=[e['payload'] for e in events if e['state']=='development_recovery_authorized']
            if existing:
                if existing!=[grant]:
                    raise CandidateError('Recovery already authorized; cannot extend or replace it')
                return proposal_rounds(events)
            ids={e['payload'].get('request_id') for e in events
                 if e['state'] in {'proposal_request','model_proposal'}}
            significant=[e for e in events if e['state'] not in BOOKKEEPING]
            saved={e['payload'].get('request_id') for e in events if e['state']=='model_proposal'
                   and e['payload'].get('condition_sha256')==row['condition_sha256']
                   and isinstance(e['payload'].get('proposal_sha256'),str)
                   and len(e['payload']['proposal_sha256'])==64
                   and all(c in '0123456789abcdef' for c in e['payload']['proposal_sha256'])}
            if (ids!=set(requests) or saved!=ids or not significant
                    or significant[-1]['state']!='failed'
                    or significant[-1]['payload'].get('error')!='plan_iteration_limit'):
                raise CandidateError('Recovery requires the three recorded failed proposal rounds')
            self._event(db,row['id'],'development_recovery_authorized',grant)
            return proposal_rounds(events+[{'state':'development_recovery_authorized','payload':grant}])

    @contextmanager
    def lease(self, job):
        path = self.locks / (task_id(job) + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise CandidateError('Unsafe candidate worker lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def reconcile(self, identifier):
        job = self.get(identifier)
        if job and job['state'] in ACTIVE:
            with self.lease(job['id']) as acquired:
                if acquired:
                    with self.tasks.transaction() as db:
                        latest = db.execute('SELECT state FROM candidate_events WHERE job_id=? AND state NOT IN ('+
                                            ','.join('?' for _ in BOOKKEEPING)+') ORDER BY sequence DESC LIMIT 1',
                                            (job['id'],*sorted(BOOKKEEPING))).fetchone()[0]
                        if latest in ACTIVE:
                            self._event(db, job['id'], 'interrupted', {'message': '后台准备进程已结束；模型请求和已有文件需核对，不会自动重发。'})
            job = self.get(identifier)
        return job


class CandidateService:
    def __init__(self, tasks, client, adapter, *, resources, snapshots, max_atoms=100000, output_layout='isolated', review_plan=False, recovery_authorizations=(), resources_provider=None):
        output_prefix(output_layout)
        self.output_layout = output_layout
        self.review_plan = review_plan
        self.failure_context_provider = None
        self.resources_provider = resources_provider
        self.tasks, self.client, self.adapter = tasks, client, adapter
        self.resources, self.max_atoms = resources, max_atoms
        self.snapshots = private_directory(snapshots)
        self.history = CandidateHistory(tasks)
        for grant in recovery_authorizations:
            self.history.authorize_development_recovery(grant)
        config = {'review_plan':review_plan, 'resources': asdict(resources), 'max_atoms': max_atoms, 'model': asdict(client.calls.config),
                  'requested_model': getattr(client,'model',client.calls.config.model), 'thinking':getattr(client,'thinking',False), 'model_ledger': str(client.calls.path.resolve()), 'catalog': str(adapter.catalog.directory),
                  'pins': sorted(adapter.allowed_pins), 'software': adapter.software_sha256,
                  'potential_compatibility': adapter.compatibility_policy(),
                  'packages': sorted(adapter.packages), 'snapshots': str(self.snapshots),
                  'geometry': geometry_runtime(), 'sources': {name: sha256((Path(__file__).parent / name).read_bytes())
                    for name in ('candidate_jobs.py', 'agent_candidates.py', 'candidate_tools.py', 'plan_review.py',
                                 'failure_recovery.py', 'scientific_adapters.py', 'analysis.py', 'analysis_v2.py',
                                 'site_thermodynamics.py', 'structures.py', 'potentials.py', 'remote_potentials.py')}}
        if output_layout != 'isolated': config['output_layout'] = output_layout
        self.config_sha256 = sha256(canonical(config))
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='candidate-preparation')

    def availability(self):
        if self.client.calls.status()['remaining_requests'] <= 0:
            return {'enabled': False, 'reason': '模型尚无可用调用额度。'}
        if self.adapter.compatible_models():
            return {'enabled': True, 'reason': ''}
        return {'enabled': False, 'reason': '尚无已配置且通过静态兼容检查的势函数。'}

    def _new_execution_inputs(self, identifier):
        """Resolve one task's trusted current budget, never mutate deployment defaults."""
        supplied = self.resources_provider(identifier,self.resources) if self.resources_provider else None
        if supplied is None:
            return self.resources,None
        if (not isinstance(supplied,dict)
                or set(supplied) != {'remaining_core_seconds','approval_sha256','gpu_allowed'}
                or supplied['gpu_allowed'] is not False
                or type(supplied['remaining_core_seconds']) is not int
                or supplied['remaining_core_seconds'] <= 0):
            raise CandidateError('Approved task CPU resource allowance is unavailable or invalid')
        wall = min(self.resources.wall_seconds,
                   supplied['remaining_core_seconds'] // (self.resources.cores * 60) * 60)
        wall = wall // 60 * 60
        if wall < 60:
            raise CandidateError('Approved CPU allowance cannot reserve one minute at the fixed core count; no model request sent')
        resources = Resources(self.resources.cores,wall,self.resources.memory_bytes,self.resources.storage_bytes)
        budget = dict(remaining_core_seconds=supplied['remaining_core_seconds'],
            reserved_core_seconds=resources.core_seconds,cores=resources.cores,
            wall_seconds=resources.wall_seconds,gpu_allowed=False,approval_sha256=supplied['approval_sha256'])
        return resources,validate_execution_budget(resources,budget)

    def _proposal_execution_inputs(self, job, proposal_request_id):
        """Recheck exactly the saved draft's resources, without consulting new limits."""
        link = next((event['payload'] for event in reversed(job['events'])
            if event['state'] == 'model_proposal'
            and event['payload'].get('request_id') == proposal_request_id),{})
        if 'resources' not in link:
            if link.get('resources_sha256') not in (None,sha256(canonical(asdict(self.resources)))):
                raise CandidateError('原稿绑定的计算资源已变化，无法从当前部署还原旧资源。')
            return self.resources,None
        try:
            resources = Resources(**link['resources'])
            if link.get('resources_sha256') != sha256(canonical(asdict(resources))):
                raise CandidateError('Saved proposal resources failed integrity verification')
            budget = validate_execution_budget(resources,link.get('execution_budget'))
            if link.get('execution_budget_sha256') != (sha256(canonical(budget)) if budget is not None else None):
                raise CandidateError('Saved proposal execution budget failed integrity verification')
            return resources,budget
        except (TypeError,ValueError) as error:
            raise CandidateError('Saved proposal resources are invalid') from error

    def enqueue(self, identifier, revision, answers=None):
        existing = self.history.reconcile(identifier)
        if existing:
            if not answers:
                return existing
            # 澄清闭环：用户的答复作为**追加事件**进入同一个作业，随后重新入队，
            # 触发一次新的、可记账的模型调用。冻结条件与既有事件、费用都不改写。
            if existing['state'] not in ('clarification', 'failed', 'interrupted', 'configuration_changed', 'prepared'):
                raise CandidateError('当前状态不需要补充答复；请先查看已有准备记录。')
            self.ensure_proposal_round(identifier)
            available = self.availability()
            if not available['enabled']:
                raise CandidateError(available['reason'])
            with self.tasks.transaction() as db:
                if existing['config_sha256'] != self.config_sha256:
                    # 作业行不可改（immutable triggers），因此以**追加事件**记录重新基线，
                    # run 检查时以最新一次再基线为准。
                    self.history._event(db, existing['id'], 'config_rebased',
                                        {'from': existing['config_sha256'], 'to': self.config_sha256})
                self.history._event(db, existing['id'], 'clarification_answered',
                                    {'answers': str(answers)[:4000]})
                self.history._event(db, existing['id'], 'queued')
            self.pool.submit(self.run, identifier)
            return self.history.get(identifier)
        inputs = research_inputs(self.tasks, identifier, revision)
        available = self.availability()
        with self.tasks.transaction() as db:
            row = db.execute('SELECT id FROM candidate_jobs WHERE task_id=?', (identifier,)).fetchone()
            if row is None:
                if not available['enabled']:
                    raise CandidateError(available['reason'])
                job = uuid.uuid4().hex
                db.execute('INSERT INTO candidate_jobs VALUES (?,?,?,?,?,?)',
                           (job, identifier, revision, inputs['condition_record_sha256'], self.config_sha256,
                            datetime.now(timezone.utc).isoformat()))
                self.history._event(db, job, 'queued')
        self.pool.submit(self.run, identifier)
        return self.history.get(identifier)

    def _review_source(self, job, original_request_id):
        """Verify the historical full draft immediately preceding one review intent."""
        events = [e for e in job['events'] if e['state'] in {'review_request','candidate_review_request'}
                  and e['payload'].get('request_id') == original_request_id]
        if not events:
            raise CandidateError('原审核请求不属于本任务。')
        event = events[-1]
        prefix = {**job,'events':[e for e in job['events'] if e['sequence'] <= event['sequence']]}
        links = [e['payload'] for e in prefix['events'] if e['state'] == 'model_proposal']
        if not links:
            raise CandidateError('原审核未关联可核验的完整模型方案。')
        link = links[-1]
        found = self.client.calls.lookup(link['request_id'])
        receipt = (found or {}).get('receipt') or {}
        value = receipt.get('structured_output')
        original = self.client.calls.lookup(original_request_id)
        if (link.get('condition_sha256') != job['condition_sha256'] or not original
                or receipt.get('state') != 'completed' or not isinstance(value,dict)
                or receipt.get('output_sha256') != link.get('proposal_sha256')
                or sha256(canonical(value)) != link['proposal_sha256']
                or event['payload'].get('proposal_sha256',link['proposal_sha256']) != link['proposal_sha256']):
            raise CandidateError('原审核或完整方案回执未通过核验。')
        resources,budget = self._proposal_execution_inputs(prefix,link['request_id'])
        return dict(original_request_id=original_request_id,
            original_request_sha256=original['request_sha256'],original_event_sequence=event['sequence'],
            proposal_request_id=link['request_id'],proposal_sha256=link['proposal_sha256'],
            condition_sha256=job['condition_sha256'],resources_sha256=sha256(canonical(asdict(resources))),
            execution_budget_sha256=sha256(canonical(budget)) if budget is not None else None)

    def _checked_supplier_dispositions(self, job):
        """A disposition excludes only its original unknown review, never its child."""
        checked = {}
        for event in job['events']:
            if event['state'] != 'review_unknown_disposition':
                continue
            saved = event['payload']
            required = {'version','request_id','revision','note','supplier_checked','retain_unknown_charge',
                'original_request_id','original_request_sha256','original_event_sequence',
                'proposal_request_id','proposal_sha256','condition_sha256','resources_sha256',
                'execution_budget_sha256','disposition_sha256'}
            if (not isinstance(saved,dict) or set(saved) != required or saved['version'] != 1
                    or saved['supplier_checked'] is not True or saved['retain_unknown_charge'] is not True
                    or type(saved['original_event_sequence']) is not int
                    or saved['original_event_sequence'] >= event['sequence']
                    or type(saved['revision']) is not int or saved['revision'] < 0
                    or not isinstance(saved['note'],str) or not saved['note'].strip() or len(saved['note']) > 2000
                    or saved['disposition_sha256'] != sha256(canonical({k:v for k,v in saved.items()
                                                                       if k != 'disposition_sha256'}))):
                raise CandidateError('供应商核对记录未通过核验。')
            task_id(saved['request_id']); task_id(saved['original_request_id'])
            if saved['request_id'] == saved['original_request_id'] or saved['original_request_id'] in checked:
                raise CandidateError('同一未知审核只能登记一次明确恢复。')
            prefix = {**job,'events':[e for e in job['events'] if e['sequence'] <= saved['original_event_sequence']]}
            source = self._review_source(prefix,saved['original_request_id'])
            if any(saved[k] != value for k,value in source.items()):
                raise CandidateError('供应商核对记录的方案、资源或原请求身份已变化。')
            intents = [e['payload'] for e in job['events'] if e['state'] == 'review_recovery_requested'
                       and e['payload'].get('request_id') == saved['request_id']]
            if any(any(intent.get(k) != saved[k] for k in
                       ('revision','proposal_request_id','proposal_sha256','condition_sha256','resources_sha256'))
                   or intent.get('execution_budget_sha256') != saved['execution_budget_sha256'] for intent in intents):
                raise CandidateError('新审核意图与供应商核对记录不一致。')
            checked[saved['original_request_id']] = saved
        return checked

    def _review_unresolved(self, job, *, ignore_review_ids=()):
        disposed = self._checked_supplier_dispositions(job)
        for event in job['events']:
            if event['state'] not in {'review_request','candidate_review_request','proposal_request'}: continue
            key = event['payload']['request_id']
            if event['state'] != 'proposal_request' and (key in disposed or key in ignore_review_ids):
                continue
            prior=self.client.calls.lookup(event['payload']['request_id'])
            if prior and (not prior.get('receipt') or prior['receipt'].get('state')=='unknown'):
                return True
        return False

    def supplier_check_recoveries(self, identifier):
        """Safe local action menu; opening it never checks credentials or sends."""
        job = self.history.get(identifier)
        if not job:
            return []
        disposed = self._checked_supplier_dispositions(job)
        reviews = [e for e in job['events'] if e['state'] in {'review_request','candidate_review_request'}]
        if not reviews:
            return []
        original_id = reviews[-1]['payload']['request_id']
        children = {e['payload']['request_id'] for e in reviews if e['state'] == 'review_request'
                    and any(e['payload'].get('intent_id') == saved['request_id'] for saved in disposed.values())}
        result = []
        for key in dict.fromkeys([*disposed,original_id]):
            found = self.client.calls.lookup(key)
            if key not in disposed and (not found or (found.get('receipt') or {}).get('state') not in (None,'unknown')):
                continue
            try:
                source = self._review_source(job,key) if key not in disposed else disposed[key]
                item = {k:source[k] for k in ('original_request_id','original_request_sha256',
                                             'proposal_request_id','proposal_sha256')}
                saved = disposed.get(key)
                previous = self.previous_proposal(identifier)
                same = previous is not None and previous['request_id'] == source['proposal_request_id']
                can = (not saved and key not in children and same and key == original_id and self.review_plan
                    and job['state'] not in ACTIVE | {'queued','prepared'}
                    and job['review_rounds']['remaining'] > 0
                    and not self._review_unresolved(job,ignore_review_ids=(key,)))
                item.update(can_recover=bool(can),supplier_checked=bool(saved),retains_unknown_charge=True,
                    recovery_request_id=saved['request_id'] if saved else None,
                    reason=('此未知审核已登记一次恢复；原记录及未知费用保留。' if saved else
                            '核对供应商后，可放弃旧未知结果，并用当前 Adapter 重新审核同一完整稿。' if can else
                            '原方案、资源、剩余轮次或其他未知请求尚不允许恢复。'))
                result.append(item)
            except (CandidateError,ModelError,TaskError,ValueError,KeyError,TypeError):
                result.append(dict(original_request_id=key,can_recover=False,supplier_checked=False,
                    retains_unknown_charge=True,reason='原审核与完整方案关联尚未通过核验。'))
        return result

    def enqueue_review_after_supplier_check(self, identifier, revision, *, request_id, original_request_id, note):
        """Discard one explicitly checked unknown outcome; start a new same-draft review.

        This is not a replay of the original body. Its new intent uses the current
        mandatory Adapter and shares the existing review allowance and draft.
        """
        request_id = task_id(request_id); original_request_id = task_id(original_request_id)
        if (request_id == original_request_id or not isinstance(note,str) or not note.strip()
                or len(note) > 2000 or '\x00' in note):
            raise CandidateError('须提供不同的新审核编号和明确的供应商核对说明。')
        job = self.history.reconcile(identifier)
        if job is None:
            raise CandidateError('没有可恢复的原审核。')
        with self.history.lease(job['id']) as acquired:
            if not acquired:
                return self.history.get(identifier)
            job = self.history.get(identifier)
            disposed = self._checked_supplier_dispositions(job)
            saved = disposed.get(original_request_id)
            if saved:
                if (saved['request_id'] != request_id or saved['revision'] != revision or saved['note'] != note):
                    raise CandidateError('不能替换已登记的供应商核对恢复。')
                if (any(e['state'] == 'review_recovery_requested' and e['payload'].get('request_id') == request_id
                        for e in job['events']) and self._review_unresolved(job)):
                    return job  # A repeated UI action never resends an unknown child.
            else:
                menu = self.supplier_check_recoveries(identifier)
                option = next((item for item in menu if item['original_request_id'] == original_request_id),None)
                if not option or not option['can_recover']:
                    raise CandidateError('原未知审核不符合一次恢复条件；不能忽略其他未知请求或更换方案。')
                task = self.tasks.get(identifier)
                if task['revision'] != revision:
                    raise CandidateError('任务版本已变化，请核对当前完整方案。')
                if any(e['state'] == 'review_recovery_requested' and e['payload'].get('request_id') == request_id
                       for e in job['events']) or self.client.calls.lookup(request_id):
                    raise CandidateError('新审核编号已被使用。')
                source = self._review_source(job,original_request_id)
                saved = dict(version=1,request_id=request_id,revision=revision,note=note,
                    supplier_checked=True,retain_unknown_charge=True,**source)
                saved['disposition_sha256'] = sha256(canonical(saved))
                with self.tasks.transaction() as db:
                    self.history._event(db,job['id'],'review_unknown_disposition',saved)
        return self.enqueue_review(identifier,revision,request_id=request_id)

    def review_availability(self, identifier):
        """Read-only capability for the ordinary plan page, with raw proposal identity."""
        job=self.history.get(identifier)
        result={'can_recheck':False,'reason':'尚无可重审的模型方案。','proposal_sha256':None,
                'proposal_request_id':None,'review_rounds':review_rounds([]),'supplier_check_recoveries':[]}
        if not job: return result
        result['state']=job['state']
        result['review_rounds']=job['review_rounds']
        result['supplier_check_recoveries']=self.supplier_check_recoveries(identifier)
        try:
            proposal=self.previous_proposal(identifier)
            if proposal is None: return result
            result.update(proposal_sha256=proposal['receipt']['output_sha256'],
                          proposal_request_id=proposal['request_id'])
            self._proposal_execution_inputs(job,proposal['request_id'])
            if not self.review_plan:
                result['reason']='当前服务未启用 AI 方案审查。'
            elif job['state'] in ACTIVE or job['state']=='queued':
                result['reason']='同一任务正在准备或审查，请等待现有记录。'
            elif job['state']=='prepared':
                result['reason']='当前方案已准备，请查看并批准。'
            elif self._review_unresolved(job):
                result['reason']='已有审核请求状态未知，须先对账，不能发起新审核。'
            elif job['review_rounds']['remaining']<=0:
                result['reason']=ERRORS['review_iteration_limit']
            else:
                result.update(can_recheck=True,reason='只重新审查原稿；不会生成新方案或提交计算。')
        except (CandidateError,ModelError,ManifestError,KeyError,TypeError) as error:
            result['reason']='原稿或关联回执未通过核验：'+str(error)[:300]
        return result

    def enqueue_review(self, identifier, revision, *, request_id):
        """Explicit user action: recheck the exact retained draft, never regenerate it."""
        request_id=task_id(request_id)
        job=self.history.reconcile(identifier)
        if job is None: raise CandidateError('No saved proposal to review')
        with self.history.lease(job['id']) as acquired:
            if not acquired: return self.history.get(identifier)
            job=self.history.get(identifier)
            intents=[e['payload'] for e in job['events'] if e['state']=='review_recovery_requested'
                     and e['payload'].get('request_id')==request_id]
            if intents:
                intent=intents[0]
                if intent['revision']!=revision: raise CandidateError('Review request identity cannot change revision')
                # A crash after a completed receipt can finish the exact same review without sending again.
                if job['state']!='interrupted': return job
                if self._review_unresolved(job): raise CandidateError('Unknown review request needs reconciliation before recovery')
                if intent['config_sha256']!=self.config_sha256:
                    raise CandidateError('Saved review configuration changed; retain the original review intent')
            else:
                pending = [saved for saved in self._checked_supplier_dispositions(job).values()
                    if not any(e['state'] == 'review_recovery_requested'
                        and e['payload'].get('request_id') == saved['request_id'] for e in job['events'])]
                if pending and any(saved['request_id'] != request_id for saved in pending):
                    raise CandidateError('已核对的原审核只允许登记的同一新审核意图，不能更换编号。')
                capability=self.review_availability(identifier)
                if not capability['can_recheck']: raise CandidateError(capability['reason'])
                inputs=research_inputs(self.tasks,identifier,revision)
                if inputs['condition_record_sha256']!=job['condition_sha256']:
                    raise CandidateError('Review must retain the original frozen conditions')
                if self._review_unresolved(job): raise CandidateError('Unknown review request needs reconciliation first')
                available=self.availability()
                if not available['enabled']: raise CandidateError(available['reason'])
                guidance=[item['note'] for item in self.tasks.guidance(identifier)]
                resources,execution_budget = self._proposal_execution_inputs(job,capability['proposal_request_id'])
                intent={'request_id':request_id,'revision':revision,
                        'proposal_request_id':capability['proposal_request_id'],
                        'proposal_sha256':capability['proposal_sha256'],
                        'condition_sha256':job['condition_sha256'],'config_sha256':self.config_sha256,
                        'resources_sha256':sha256(canonical(asdict(resources))),
                        'model_config_sha256':sha256(canonical(asdict(self.client.calls.config))),
                        'requested_model':getattr(self.client,'model',self.client.calls.config.model),
                        'thinking':getattr(self.client,'thinking',False),
                        'guidance_sha256':sha256(canonical(guidance))}
                if execution_budget is not None:
                    intent['execution_budget_sha256'] = sha256(canonical(execution_budget))
            baseline=self.effective_config_sha256(identifier)
            with self.tasks.transaction() as db:
                if not intents:
                    self.history._event(db,job['id'],'review_recovery_requested',intent)
                # This explicit review action binds the upgraded review contract/configuration.
                self.history._event(db,job['id'],'config_rebased',
                    {'from':baseline,'to':self.config_sha256})
                self.history._event(db,job['id'],'queued',{'review_recovery':intent})
        self.pool.submit(self.run,identifier)
        return self.history.get(identifier)

    def ensure_proposal_round(self, identifier):
        job=self.history.get(identifier)
        if job and job['proposal_rounds']['remaining']<=0:
            raise PlanIterationLimit('方案已达到首版在内三轮上限，或历史轮次无法核验；已有完整方案仍可查看和批准，不能追加生成。')

    def rebaseline(self, identifier):
        """显式重启时把作业的有效基线更新到当前配置（追加事件，作业行不可改）。"""
        job = self.history.get(identifier)
        if job is None:
            return None
        baseline = self.effective_config_sha256(identifier)
        if baseline == self.config_sha256:
            return self.config_sha256
        # 注意：事务内不得再调用会自行开事务的方法（否则 BEGIN IMMEDIATE 嵌套 → database is locked）。
        with self.tasks.transaction() as db:
            self.history._event(db, job['id'], 'config_rebased',
                                {'from': baseline, 'to': self.config_sha256})
        return self.config_sha256

    def effective_config_sha256(self, identifier):
        """作业行不可改，因此以最新一次'重新基线'事件为准；没有则用行内值。"""
        job = self.history.get(identifier)
        if job is None:
            return None
        baseline = job['config_sha256']
        with self.tasks.transaction() as db:
            row = db.execute("SELECT payload FROM candidate_events WHERE job_id=? AND state='config_rebased' "
                             "ORDER BY sequence DESC LIMIT 1", (job['id'],)).fetchone()
        if row:
            baseline = (json.loads(row['payload']) or {}).get('to', baseline)
        return baseline

    def start(self):
        with self.tasks.transaction() as db:
            identifiers = [row[0] for row in db.execute('SELECT task_id FROM candidate_jobs')]
        for identifier in identifiers:
            job = self.history.reconcile(identifier)
            if job['state'] == 'queued':
                self.pool.submit(self.run, identifier)

    def close(self, *, wait=False):
        self.pool.shutdown(wait=wait, cancel_futures=True)

    def previous_proposal(self, identifier):
        """Read only a receipt linked to this task's immutable preparation history."""
        job=self.history.get(identifier)
        for event in reversed(job['events']):
            if event['state']=='prepared' and event['payload'].get('snapshot_sha256'):
                # Upgrade path: legacy prepared snapshots already bind task inputs and receipts.
                digest=event['payload']['snapshot_sha256']
                snapshot=Snapshot(self.snapshots/digest,digest)
                snapshot.verify()
                with root_descriptor(snapshot.path) as root:
                    generation=json.loads(read_file(root,'generation.json',2000000))
                if generation['input'].get('condition_record_sha256')!=job['condition_sha256']:
                    continue
                proposal_digest=sha256(canonical(generation['proposal']))
                matches={r['request_sha256'] for r in generation['model_receipts']
                         if r.get('output_sha256')==proposal_digest and r.get('state')=='completed'}
                for prior in self.client.calls.history():
                    receipt=prior.get('receipt') or {}
                    if (prior['request_sha256'] in matches and receipt.get('state')=='completed'
                            and receipt.get('output_sha256')==proposal_digest
                            and sha256(canonical(receipt.get('structured_output')))==proposal_digest):
                        return {'value':receipt['structured_output'],'request_id':prior['request_id'],
                                'receipt':{k:v for k,v in receipt.items() if k!='structured_output'}}
                raise CandidateError('Legacy snapshot model receipt could not be verified')
            if event['state']!='model_proposal': continue
            link=event['payload']
            if link.get('condition_sha256')!=job['condition_sha256']: continue
            found=self.client.calls.lookup(link['request_id'])
            receipt=(found or {}).get('receipt') or {}
            value=receipt.get('structured_output')
            if (receipt.get('state')!='completed' or not isinstance(value,dict)
                    or receipt.get('output_sha256')!=link.get('proposal_sha256')
                    or sha256(canonical(value))!=link['proposal_sha256']):
                raise CandidateError('Saved proposal receipt failed integrity verification')
            if value.get('workflow') is None: return None
            return {'value':value,'request_id':link['request_id'],
                    'receipt':{k:v for k,v in receipt.items() if k!='structured_output'}}
        return None

    def latest_review_feedback(self, identifier, previous=None):
        """Return only the latest verified rejection of this task's exact draft.

        The short failed-event detail is presentation text, not repair evidence.
        Use the complete, validated review event and its immutable model receipt;
        never fall back past a newer unknown, invalid report or different draft.
        """
        job = self.history.get(identifier)
        if job is None:
            return None
        if self._review_unresolved(job):
            raise CandidateError('已有模型请求状态未知，不能将旧审核用于新稿。')
        previous = previous if previous is not None else self.previous_proposal(identifier)
        if previous is None:
            return None
        requests = [e for e in job['events']
                    if e['state'] in {'review_request','candidate_review_request'}]
        if not requests:
            return None
        requested = requests[-1]
        links = [e for e in job['events'] if e['state'] == 'model_proposal'
                 and e['sequence'] < requested['sequence']]
        if not links:
            return None
        source = links[-1]['payload']
        if (source.get('request_id') != previous['request_id']
                or source.get('proposal_sha256') != previous['receipt']['output_sha256']
                or source.get('condition_sha256') != job['condition_sha256']):
            return None
        key = requested['payload']['request_id']
        if requested['payload'].get('proposal_sha256',source['proposal_sha256']) != source['proposal_sha256']:
            raise CandidateError('Latest review does not match its recorded complete proposal')
        checks = [e for e in job['events']
                  if e['state'] in {'model_review','candidate_model_review'}
                  and e['sequence'] > requested['sequence']
                  and e['payload'].get('request_id') == key]
        if not checks:
            return None
        checked = checks[-1]
        saved = checked['payload']
        if saved.get('status') == 'invalid_report':
            return None
        found = self.client.calls.lookup(key)
        receipt = (found or {}).get('receipt') or {}
        raw_report = receipt.get('structured_output')
        report = {k:saved.get(k) for k in ('issues','coverage','summary')}
        saved_receipt = saved.get('receipt') or {}
        if (not found or receipt.get('state') != 'completed'
                or receipt.get('request_sha256') != found['request_sha256']
                or not isinstance(raw_report,dict) or set(raw_report) != {'issues','coverage','summary'}
                or receipt.get('output_sha256') != sha256(canonical(raw_report))
                or saved_receipt != {k:v for k,v in receipt.items() if k != 'structured_output'}
                or report != {**raw_report,'issues':normalized_review_issues(raw_report['issues'])}
                or saved.get('proposal_sha256') != source['proposal_sha256']
                or not isinstance(saved.get('artifact_sha256'),dict)
                or set(saved['artifact_sha256']) != {'rendered_script','resource_metadata','geometry_checks','analysis_plan'}
                or any(not isinstance(value,str) or len(value) != 64
                       or any(c not in '0123456789abcdef' for c in value)
                       for value in saved['artifact_sha256'].values())):
            raise CandidateError('Latest complete review feedback failed receipt or proposal verification')
        if not report['issues']:
            return None
        return dict(version=1,task_id=identifier,condition_sha256=job['condition_sha256'],
            proposal_request_id=previous['request_id'],proposal_sha256=source['proposal_sha256'],
            review_request_id=key,review_request_sha256=found['request_sha256'],
            review_output_sha256=receipt['output_sha256'],review_event_sequence=checked['sequence'],
            artifact_sha256=saved['artifact_sha256'],receipt=saved_receipt,report=report,
            raw_issues=raw_report['issues'] if raw_report['issues'] != report['issues'] else None)

    def run(self, identifier):
        job = self.history.get(identifier)
        with self.history.lease(job['id']) as acquired:
            if not acquired:
                return
            with self.tasks.transaction() as db:
                queued = db.execute('SELECT state,payload FROM candidate_events WHERE job_id=? ORDER BY sequence DESC LIMIT 1', (job['id'],)).fetchone()
                state = queued['state']
                review_intent=json.loads(queued['payload']).get('review_recovery')
                if state != 'queued':
                    return
                baseline = job['config_sha256']
                rebased = db.execute("SELECT payload FROM candidate_events WHERE job_id=? AND state='config_rebased' "
                                     "ORDER BY sequence DESC LIMIT 1", (job['id'],)).fetchone()
                if rebased:
                    baseline = (json.loads(rebased['payload']) or {}).get('to', baseline)
                if baseline != self.config_sha256:
                    self.history._event(db, job['id'], 'configuration_changed',
                                        {'message': '服务配置或代码版本已变化；没有重新调用模型。',
                                         'detail': '如确认要按当前版本重跑，请在页面上再次点击准备并（如有需要）补充答复。'})
                    return
                self.history._event(db, job['id'], 'running')
            def stage(state):
                with self.tasks.transaction() as db:
                    self.history._event(db, job['id'], state)
            def proposal_saved(link):
                with self.tasks.transaction() as db:
                    self.history._event(db, job['id'], 'model_proposal',
                        {**link, 'condition_sha256':job['condition_sha256'],
                         'config_sha256':self.config_sha256,
                         'resources':asdict(resources),
                         'resources_sha256':sha256(canonical(asdict(resources))),
                         **({'execution_budget':execution_budget,
                             'execution_budget_sha256':sha256(canonical(execution_budget))}
                            if execution_budget is not None else {})})
            answers = None
            with self.tasks.transaction() as db:
                row = db.execute("SELECT payload FROM candidate_events WHERE job_id=? AND state='clarification_answered' "
                                 "ORDER BY sequence DESC LIMIT 1", (job['id'],)).fetchone()
                if row:
                    answers = (json.loads(row['payload']) or {}).get('answers')
            guidance = [item['note'] for item in self.tasks.guidance(identifier)]
            try:
                revision=self.tasks.get(identifier)['revision']
                inputs=research_inputs(self.tasks,identifier,revision)
                if inputs['condition_record_sha256']!=job['condition_sha256']:
                    raise CandidateError('Frozen research conditions changed; preserve the original preparation identity')
                previous=self.previous_proposal(identifier)
                review_feedback = self.latest_review_feedback(identifier,previous) if not review_intent else None
                resources,execution_budget = (self._proposal_execution_inputs(job,previous['request_id'])
                    if review_intent and previous is not None else self._new_execution_inputs(identifier))
                if review_intent:
                    if (previous is None or previous['request_id']!=review_intent['proposal_request_id']
                            or previous['receipt']['output_sha256']!=review_intent['proposal_sha256']
                            or inputs['condition_record_sha256']!=review_intent['condition_sha256']
                            or self.config_sha256!=review_intent['config_sha256']
                            or sha256(canonical(asdict(resources)))!=review_intent['resources_sha256']
                            or (sha256(canonical(execution_budget)) if execution_budget is not None else None)
                                != review_intent.get('execution_budget_sha256')
                            or sha256(canonical(asdict(self.client.calls.config)))!=review_intent['model_config_sha256']
                            or getattr(self.client,'model',self.client.calls.config.model)!=review_intent['requested_model']
                            or getattr(self.client,'thinking',False)!=review_intent['thinking']
                            or revision!=review_intent['revision']
                            or sha256(canonical(guidance))!=review_intent['guidance_sha256']):
                        raise CandidateError('Same-plan review inputs or configuration changed; no request sent')
                failure=(self.failure_context_provider(identifier) if self.failure_context_provider and not review_intent else None)
                def review_requested(key):
                    if review_intent:
                        self.history.reserve_review(identifier,key,review_intent)
                    else:
                        with self.tasks.transaction() as db:
                            self.history._event(db,job['id'],'candidate_review_request',{'request_id':key})
                def review_saved(check):
                    with self.tasks.transaction() as db:
                        self.history._event(db,job['id'],'model_review' if review_intent else 'candidate_model_review',
                            {**check,**({'intent_id':review_intent['request_id']} if review_intent else {})})
                result = generate_research_candidate(self.client, self.tasks, identifier, revision, self.adapter,
                            resources=resources, store=self.snapshots, max_atoms=self.max_atoms, on_stage=stage,
                            output_layout=self.output_layout, answers=answers, guidance=guidance, review_plan=self.review_plan,
                            previous_proposal=previous, on_proposal=None if review_intent else proposal_saved,
                            before_proposal_request=lambda key,kind:self.history.reserve_proposal(identifier,key,kind),
                            proposal_round_budget=self.history.get(identifier)['proposal_rounds'],
                            failure_context=failure, review_recovery=review_intent,
                            before_review_request=review_requested,
                            on_review=review_saved,execution_budget=execution_budget,
                            review_feedback=review_feedback)
                if self.tasks.get(identifier)['revision']!=revision:
                    raise CandidateError('Task guidance changed during preparation; preserve this answer and review the new instructions')
                if result['status'] == 'clarification_required':
                    state, payload = 'clarification', {'summary': result['proposal']['summary'],
                        'questions': result['proposal']['questions'], 'request_id': result['request_id']}
                else:
                    snapshot = result['snapshot']
                    snapshot.verify()
                    generation = result['generation']
                    state, payload = 'prepared', {'summary': generation['proposal']['summary'], 'questions': [],
                        'snapshot_sha256': snapshot.digest, 'request_id': result['request_id'],
                        'geometry': generation['geometry_receipt'], 'analysis': generation['proposal']['analysis']}
            except Exception as error:
                code = 'preparation_failed'
                if isinstance(error, ModelError):
                    code = str(error) if str(error) in ERRORS else 'model_generation_failed'
                elif isinstance(error, PlanIterationLimit):
                    code = 'plan_iteration_limit'
                elif isinstance(error, (CandidateError, PotentialError, StructureError, TaskError, ManifestError)):
                    code = 'candidate_validation_failed'
                # 具体原因必须可见：只说"检查未通过"用户无法定位。
                state, payload = 'failed', {'error': code, 'message': ERRORS[code],
                                            'detail': str(error)[:400]}
            with self.tasks.transaction() as db:
                self.history._event(db, job['id'], state, payload)

    def saved_plan_options(self, identifier):
        """Only actual prepared snapshots of this frozen task can be selected."""
        job=self.history.get(identifier)
        if job is None:return []
        options=[];seen=set()
        for event in job['events']:
            digest=event['payload'].get('snapshot_sha256')
            if event['state']!='prepared' or not digest or digest in seen:continue
            seen.add(digest)
            try:
                snapshot=Snapshot(self.snapshots/digest,digest);snapshot.verify()
                with root_descriptor(snapshot.path) as root:
                    generation=json.loads(read_file(root,'generation.json',2000000))
                if (generation['input'].get('condition_record_sha256')!=job['condition_sha256']
                        or generation.get('saved_plan_selection')):continue
                proposal_sha=sha256(canonical(generation['proposal']))
                links=[e['payload'] for e in job['events'] if e['state']=='model_proposal'
                       and e['payload'].get('proposal_sha256')==proposal_sha]
                if not links:continue
                key=links[-1]['request_id'];found=self.client.calls.lookup(key)
                receipt=(found or {}).get('receipt') or {}
                if (receipt.get('state')!='completed' or receipt.get('output_sha256')!=proposal_sha
                        or sha256(canonical(receipt.get('structured_output')))!=proposal_sha):continue
                round_number=next((e['payload'].get('round') for e in job['events']
                    if e['state']=='proposal_request' and e['payload'].get('request_id')==key),None)
                previous=dict(value=receipt['structured_output'],request_id=key,
                              receipt={k:v for k,v in receipt.items() if k!='structured_output'})
                options.append(dict(snapshot_sha256=digest,proposal_sha256=proposal_sha,
                                    proposal_request_id=key,proposal_round=round_number,
                                    review_feedback=self.latest_review_feedback(identifier,previous)))
            except (OSError,ValueError,KeyError,TypeError):continue
        return options

    def select_saved_plan(self,identifier,revision,*,snapshot_sha256,request_id,accept_unresolved_review):
        """Explicit development retest selection; rebind resources, never regenerate science."""
        request_id=task_id(request_id)
        if accept_unresolved_review is not True:raise CandidateError('请明确确认保留未解决科研审核问题后选用原稿。')
        job=self.history.get(identifier)
        if job is None:raise CandidateError('没有已准备的历史方案。')
        with self.history.lease(job['id']) as acquired:
            if not acquired:raise CandidateError('当前方案仍在处理中，请等待原操作结束。')
            job=self.history.get(identifier)
            if job['state'] in ACTIVE or job['state']=='queued':raise CandidateError('当前准备仍在进行，不能切换原稿。')
            if self._review_unresolved(job):raise CandidateError('存在未知审核回执，请先核对供应商；不能借选稿绕过。')
            inputs=research_inputs(self.tasks,identifier,revision)
            if inputs['condition_record_sha256']!=job['condition_sha256']:raise CandidateError('历史方案的冻结条件已变化。')
            existing=[e['payload'] for e in job['events'] if e['state']=='saved_plan_selected' and e['payload'].get('selection_request_id')==request_id]
            if existing:
                if existing[0]['source_snapshot_sha256']!=snapshot_sha256:raise CandidateError('选用请求编号不可换稿。')
                return job
            option=next((o for o in self.saved_plan_options(identifier) if o['snapshot_sha256']==snapshot_sha256),None)
            if option is None:raise CandidateError('只能选用本任务真实已准备且来源核验通过的方案。')
            found=self.client.calls.lookup(option['proposal_request_id']);receipt=found['receipt']
            previous=dict(value=receipt['structured_output'],request_id=option['proposal_request_id'],
                          receipt={k:v for k,v in receipt.items() if k!='structured_output'})
            feedback=self.latest_review_feedback(identifier,previous)
            resources,budget=self._new_execution_inputs(identifier)
            if budget is None:raise CandidateError('原稿开发复测需要本任务已批准的实际资源上限。')
            selection=dict(mode='development_retest_with_unresolved_scientific_review',
                selection_request_id=request_id,source_snapshot_sha256=snapshot_sha256,
                proposal_request_id=option['proposal_request_id'],proposal_sha256=option['proposal_sha256'],
                proposal_round=option['proposal_round'],accept_unresolved_review=True,
                scientific_review_status='unresolved',review_feedback=feedback)
            from .agent_candidates import generate_candidate_draft
            result=generate_candidate_draft(self.client,self.adapter,**inputs,resources=resources,
                execution_budget=budget,store=self.snapshots,max_atoms=self.max_atoms,output_layout=self.output_layout,
                previous_proposal=previous,proposal_round_budget=job['proposal_rounds'],require_analysis_plan=True,
                review_plan=False,saved_plan_selection=selection,
                guidance=[item['note'] for item in self.tasks.guidance(identifier)])
            source=Snapshot(self.snapshots/snapshot_sha256,snapshot_sha256).verify()
            current=result['snapshot'].verify()
            old_script=next(item['sha256'] for item in source['files'] if item['path']=='in.lammps')
            new_script=next(item['sha256'] for item in current['files'] if item['path']=='in.lammps')
            if old_script!=new_script:raise CandidateError('选用原稿不能改变任何科学脚本字节。')
            from .authorization import candidate_check
            candidate_check(result['snapshot'],max_atoms=self.max_atoms)
            if self.tasks.get(identifier)['revision']!=revision:raise CandidateError('选用期间任务已变化。')
            generation=result['generation']
            with self.tasks.transaction() as db:
                self.history._event(db,job['id'],'config_rebased',{'from':job['config_sha256'],'to':self.config_sha256})
                self.history._event(db,job['id'],'saved_plan_selected',selection)
                self.history._event(db,job['id'],'prepared',dict(summary=generation['proposal']['summary'],questions=[],
                    snapshot_sha256=result['snapshot'].digest,request_id=result['request_id'],
                    geometry=generation['geometry_receipt'],analysis=generation['proposal']['analysis'],
                    saved_plan_selection=selection))
            return self.history.get(identifier)

    def plan_review(self, identifier):
        """用户审核方案所需的内容：脚本、结构、分析定义与文件摘要（不含模型私密回答）。"""
        public = ('in.lammps', 'structure.data', 'analysis.json', 'generation.json')
        job = self.history.get(identifier)
        if job is None:
            return {'state': None, 'files': []}
        result = job.get('result') or {}
        review = {'state': job['state'], 'label': job.get('label'), 'revision': job['revision'],
                  'summary': result.get('summary'), 'geometry': result.get('geometry'),
                  'analysis': result.get('analysis'), 'questions': result.get('questions') or [],
                  'detail': result.get('detail'), 'snapshot_sha256': result.get('snapshot_sha256'),
                  'proposal_rounds':job['proposal_rounds'], 'review_rounds':job['review_rounds'],
                  'same_plan_review':self.review_availability(identifier),
                  'review_history':[e for e in job['events'] if e['state'] in
                                    {'candidate_review_request','candidate_model_review',
                                     'review_recovery_requested','review_request','model_review'}],
                  'execution_authorized': result.get('execution_authorized', False), 'files': []}
        review['saved_plan_options']=self.saved_plan_options(identifier)
        review['saved_plan_selection']=result.get('saved_plan_selection')
        try:
            review['latest_review_feedback'] = self.latest_review_feedback(identifier)
        except (CandidateError,ModelError,TaskError,ManifestError,KeyError,TypeError,ValueError):
            # Retained plans remain readable; an unverified report is never
            # presented as feedback or supplied to the next model request.
            review['latest_review_feedback'] = None
        if result.get('saved_plan_selection'):
            review['latest_review_feedback']=result['saved_plan_selection'].get('review_feedback')
        if job['state'] != 'prepared' or not result.get('snapshot_sha256'):
            return review
        digest = result['snapshot_sha256']
        snapshot = Snapshot(self.snapshots / digest, digest)
        record = snapshot.verify()
        for item in record['files']:
            if item['path'] not in public:
                continue
            entry = {'name': item['path'], 'sha256': item['sha256'], 'size': item['size']}
            if item['size'] <= 200000:
                try:
                    entry['content'] = self.file(identifier, item['path']).decode('utf-8', 'replace')
                except (OSError, ValueError):
                    entry['content'] = None
            review['files'].append(entry)
            if item['path']=='generation.json' and entry.get('content'):
                checks=json.loads(entry['content']).get('plan_reviews',[])
                recovery=json.loads(entry['content']).get('failure_recovery')
                if recovery:review['failure_recovery']={k:recovery[k] for k in
                    ('summary','evidence','cause','repair','proposed_lesson','validation_status')}
                if checks:
                    review['automatic_check']={k:checks[-1][k] for k in ('issues','coverage','summary')}
        return review

    def file(self, identifier, name):
        if name not in {'in.lammps', 'structure.data', 'analysis.json', 'generation.json'}:
            raise KeyError('Candidate file not available')
        job = self.history.get(identifier)
        if job is None or job['state'] != 'prepared':
            raise CandidateError('方案文件尚未准备完成。')
        digest = job['result']['snapshot_sha256']
        snapshot = Snapshot(self.snapshots / digest, digest)
        record = snapshot.verify()
        size = next(item['size'] for item in record['files'] if item['path'] == name)
        with root_descriptor(snapshot.path) as root:
            return read_file(root, name, size)
