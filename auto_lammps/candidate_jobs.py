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
                               generate_research_candidate, research_inputs, output_prefix)
from .deepseek import ModelError
from .manifest import ManifestError, Snapshot, canonical, private_directory, read_file, root_descriptor, sha256
from .potentials import PotentialError
from .structures import StructureError, geometry_runtime
from .tasks import TaskError, task_id

BOOKKEEPING = {'config_rebased', 'clarification_answered', 'model_proposal', 'proposal_request'}
ACTIVE = {'diagnosing_failure','reusing_plan', 'running', 'model_requested', 'checking_plan', 'repairing_plan', 'preparing_files'}
LABELS = {'proposal_request':'方案生成轮次已登记', 'model_proposal':'方案版本已保存', 'reusing_plan':'沿用上一版方案并重新检查', 'queued': '等待准备', 'running': '核对准备条件', 'model_requested': '生成计算方案',
          'diagnosing_failure':'AI 正在读取失败日志并诊断原因',
          'checking_plan':'核对需求与方案', 'repairing_plan':'自动修正方案', 'preparing_files': '准备结构与输入文件', 'prepared': '方案已准备 · 待核验',
          'clarification': '需要补充条件', 'failed': '准备未完成', 'interrupted': '准备中断 · 待核对',
          'configuration_changed': '配置已变化 · 待核对', 'clarification_answered': '已收到补充答复', 'config_rebased': '已按当前配置重新基线'}
ERRORS = {'model_budget_exhausted': '模型额度已用完，没有自动重试。',
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
    return {'limit':MAX_PROPOSAL_ROUNDS, 'used':len(ids),
            'remaining':0 if unknown else max(0,MAX_PROPOSAL_ROUNDS-len(ids)),
            'historical_count_unknown':unknown}


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
                'proposal_rounds':proposal_rounds(events),
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
                        {'request_id':request_id,'kind':kind,'round':budget['used']+1,'limit':MAX_PROPOSAL_ROUNDS})
            return {**budget,'used':budget['used']+1,'remaining':budget['remaining']-1}

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
    def __init__(self, tasks, client, adapter, *, resources, snapshots, max_atoms=100000, output_layout='isolated', review_plan=False):
        output_prefix(output_layout)
        self.output_layout = output_layout
        self.review_plan = review_plan
        self.failure_context_provider = None
        self.tasks, self.client, self.adapter = tasks, client, adapter
        self.resources, self.max_atoms = resources, max_atoms
        self.snapshots = private_directory(snapshots)
        self.history = CandidateHistory(tasks)
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

    def run(self, identifier):
        job = self.history.get(identifier)
        with self.history.lease(job['id']) as acquired:
            if not acquired:
                return
            with self.tasks.transaction() as db:
                state = db.execute('SELECT state FROM candidate_events WHERE job_id=? ORDER BY sequence DESC LIMIT 1', (job['id'],)).fetchone()[0]
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
                        {**link, 'condition_sha256':job['condition_sha256']})
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
                failure=(self.failure_context_provider(identifier) if self.failure_context_provider else None)
                result = generate_research_candidate(self.client, self.tasks, identifier, revision, self.adapter,
                            resources=self.resources, store=self.snapshots, max_atoms=self.max_atoms, on_stage=stage,
                            output_layout=self.output_layout, answers=answers, guidance=guidance, review_plan=self.review_plan,
                            previous_proposal=self.previous_proposal(identifier), on_proposal=proposal_saved,
                            before_proposal_request=lambda key,kind:self.history.reserve_proposal(identifier,key,kind),
                            proposal_round_budget=self.history.get(identifier)['proposal_rounds'],
                            failure_context=failure)
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
                  'proposal_rounds':job['proposal_rounds'],
                  'execution_authorized': result.get('execution_authorized', False), 'files': []}
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
