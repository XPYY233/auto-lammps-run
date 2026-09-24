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

from .agent_candidates import CandidateError, generate_research_candidate, research_inputs
from .deepseek import ModelError
from .manifest import ManifestError, Snapshot, canonical, private_directory, read_file, root_descriptor, sha256
from .potentials import PotentialError
from .structures import StructureError, geometry_runtime
from .tasks import TaskError, task_id

ACTIVE = {'running', 'model_requested', 'preparing_files'}
LABELS = {'queued': '等待准备', 'running': '核对准备条件', 'model_requested': '生成计算方案',
          'preparing_files': '准备结构与输入文件', 'prepared': '方案已准备 · 待核验',
          'clarification': '需要补充条件', 'failed': '准备未完成', 'interrupted': '准备中断 · 待核对',
          'configuration_changed': '配置已变化 · 待核对'}
ERRORS = {'model_budget_exhausted': '模型额度已用完，没有自动重试。',
          'model_key_missing_or_invalid': '模型密钥尚未配置，请联系管理员。',
          'request_already_reserved': '已有模型请求记录，需要核对，未重复调用。',
          'model_transport_unknown': '调用状态未确认，保留记录且不自动重试。',
          'model_generation_failed': '模型未返回完整有效方案，原有记录已保留。',
          'candidate_validation_failed': '方案或资源检查未通过，原始模型回答已保留。',
          'preparation_failed': '文件准备未完成，记录已保留，请核对服务状态。'}


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
            event['label'] = LABELS[event['state']]
        return {**job, 'state': events[-1]['state'], 'label': events[-1]['label'],
                'updated_at': events[-1]['at'], 'result': events[-1]['payload'], 'events': events,
                'execution_authorized': False}

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
                        latest = db.execute('SELECT state FROM candidate_events WHERE job_id=? ORDER BY sequence DESC LIMIT 1', (job['id'],)).fetchone()[0]
                        if latest in ACTIVE:
                            self._event(db, job['id'], 'interrupted', {'message': '后台准备进程已结束；模型请求和已有文件需核对，不会自动重发。'})
            job = self.get(identifier)
        return job


class CandidateService:
    def __init__(self, tasks, client, adapter, *, resources, snapshots, max_atoms=100000):
        self.tasks, self.client, self.adapter = tasks, client, adapter
        self.resources, self.max_atoms = resources, max_atoms
        self.snapshots = private_directory(snapshots)
        self.history = CandidateHistory(tasks)
        config = {'resources': asdict(resources), 'max_atoms': max_atoms, 'model': asdict(client.calls.config),
                  'model_ledger': str(client.calls.path.resolve()), 'catalog': str(adapter.catalog.directory),
                  'pins': sorted(adapter.allowed_pins), 'software': adapter.software_sha256,
                  'potential_compatibility': adapter.compatibility_policy(),
                  'packages': sorted(adapter.packages), 'snapshots': str(self.snapshots),
                  'geometry': geometry_runtime(), 'sources': {name: sha256((Path(__file__).parent / name).read_bytes())
                    for name in ('candidate_jobs.py', 'agent_candidates.py', 'structures.py', 'potentials.py')}}
        self.config_sha256 = sha256(canonical(config))
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='candidate-preparation')

    def availability(self):
        if self.client.calls.status()['remaining_requests'] <= 0:
            return {'enabled': False, 'reason': '模型尚无可用调用额度。'}
        if self.adapter.compatible_models():
            return {'enabled': True, 'reason': ''}
        return {'enabled': False, 'reason': '尚无已配置且通过静态兼容检查的势函数。'}

    def enqueue(self, identifier, revision):
        existing = self.history.reconcile(identifier)
        if existing:
            return existing
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

    def start(self):
        with self.tasks.transaction() as db:
            identifiers = [row[0] for row in db.execute('SELECT task_id FROM candidate_jobs')]
        for identifier in identifiers:
            job = self.history.reconcile(identifier)
            if job['state'] == 'queued':
                self.pool.submit(self.run, identifier)

    def close(self, *, wait=False):
        self.pool.shutdown(wait=wait, cancel_futures=True)

    def run(self, identifier):
        job = self.history.get(identifier)
        with self.history.lease(job['id']) as acquired:
            if not acquired:
                return
            with self.tasks.transaction() as db:
                state = db.execute('SELECT state FROM candidate_events WHERE job_id=? ORDER BY sequence DESC LIMIT 1', (job['id'],)).fetchone()[0]
                if state != 'queued':
                    return
                if job['config_sha256'] != self.config_sha256:
                    self.history._event(db, job['id'], 'configuration_changed', {'message': '服务配置或代码版本已变化；没有重新调用模型。'})
                    return
                self.history._event(db, job['id'], 'running')
            def stage(state):
                with self.tasks.transaction() as db:
                    self.history._event(db, job['id'], state)
            try:
                result = generate_research_candidate(self.client, self.tasks, identifier, job['revision'], self.adapter,
                            resources=self.resources, store=self.snapshots, max_atoms=self.max_atoms, on_stage=stage)
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
                elif isinstance(error, (CandidateError, PotentialError, StructureError, TaskError, ManifestError)):
                    code = 'candidate_validation_failed'
                state, payload = 'failed', {'error': code, 'message': ERRORS[code]}
            with self.tasks.transaction() as db:
                self.history._event(db, job['id'], state, payload)

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
