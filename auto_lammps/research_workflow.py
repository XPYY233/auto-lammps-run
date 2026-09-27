"""An explicit durable start continues existing research services, not a second pipeline."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid

from .agent_candidates import research_inputs
from .authorization import AutomaticAuthorization
from .manifest import canonical, sha256
from .tasks import TaskError, StaleTask

ACTIVE = {'queued', 'preparing'}
LABELS = {'queued': '等待自动准备', 'preparing': '正在自动准备计算方案',
          'handed_off': '已进入自动计算流程', 'attention': '需要处理后继续'}


class ResearchWorkflow:
    def __init__(self, candidates, execution):
        self.candidates, self.execution = candidates, execution
        self.tasks = candidates.tasks
        if self.tasks.path != execution.tasks.path or candidates.snapshots != execution.controller.snapshots:
            raise ValueError('Automatic workflow must share tasks and candidate snapshots')
        self.identity = sha256(canonical(dict(candidate=candidates.config_sha256,
            execution=execution.config_sha256, source=sha256(Path(__file__).read_bytes()))))
        self.stop = threading.Event(); self.wake = threading.Event(); self.thread = None
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS research_workflows ('
                'id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), '
                'revision INTEGER NOT NULL, condition_sha256 TEXT NOT NULL, service_sha256 TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS research_workflow_events ('
                'workflow_id TEXT NOT NULL REFERENCES research_workflows(id), seq INTEGER NOT NULL, '
                'at TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(workflow_id,seq))')
            for table in ('research_workflows', 'research_workflow_events'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable workflow history'); END")

    def availability(self):
        available = self.candidates.availability()
        configured = isinstance(self.execution.controller.authorization, AutomaticAuthorization)
        return dict(configured=configured, enabled=configured and available['enabled'],
                    reason=available['reason'] if configured else '自动计算部署尚未就绪，可先准备方案。',
                    resources=asdict(self.candidates.resources), max_submissions=2)

    def _event(self, db, identifier, state, reason=''):
        old = db.execute('SELECT seq,state,reason FROM research_workflow_events WHERE workflow_id=? '
                         'ORDER BY seq DESC LIMIT 1', (identifier,)).fetchone()
        if old and (old['state'], old['reason']) == (state, reason): return
        db.execute('INSERT INTO research_workflow_events VALUES (?,?,?,?,?)',
            (identifier, old['seq']+1 if old else 1, datetime.now(timezone.utc).isoformat(), state, reason))

    def get(self, identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            row = db.execute('SELECT * FROM research_workflows WHERE task_id=?', (identifier,)).fetchone()
            if row is None: return None
            events = [dict(e) for e in db.execute('SELECT * FROM research_workflow_events WHERE workflow_id=? ORDER BY seq', (row['id'],))]
        return dict(row) | dict(events=events, state=events[-1]['state'], reason=events[-1]['reason'])

    def status(self, identifier):
        row = self.get(identifier)
        return dict(worker_alive=bool(self.thread and self.thread.is_alive()), workflow=None if row is None else
            dict(state=row['state'], label=LABELS[row['state']], reason=row['reason'],
                 events=[dict(at=e['at'], label=LABELS[e['state']], reason=e['reason']) for e in row['events']]))

    def enqueue(self, identifier, revision):
        inputs = research_inputs(self.tasks, identifier, revision)
        if self.get(identifier): return self.status(identifier)
        available = self.availability()
        if not available['enabled']: raise TaskError(available['reason'])
        if self.execution.evaluation_for(identifier) is None and self.execution.enrollment is None:
            raise TaskError('此任务尚未接入已核验的计算部署。')
        with self.tasks.transaction() as db:
            if self.tasks._read(db, identifier)['revision'] != revision: raise StaleTask('任务已变化，请刷新后再开始。')
            if db.execute('SELECT id FROM research_workflows WHERE task_id=?', (identifier,)).fetchone() is None:
                key = uuid.uuid4().hex
                db.execute('INSERT INTO research_workflows VALUES (?,?,?,?,?)',
                    (key, identifier, revision, inputs['condition_record_sha256'], self.identity))
                self._event(db, key, 'queued')
        self.wake.set()
        return self.status(identifier)

    def advance(self, identifier):
        row = self.get(identifier)
        if row is None or row['state'] not in ACTIVE: return self.status(identifier)
        with self.candidates.history.lease(row['id']) as acquired:
            if not acquired: return self.status(identifier)
            row = self.get(identifier)
            if row['state'] not in ACTIVE: return self.status(identifier)
            try:
                inputs = research_inputs(self.tasks, identifier, row['revision'])
                if row['service_sha256'] != self.identity or inputs['condition_record_sha256'] != row['condition_sha256']:
                    state, reason = 'attention', 'configuration_changed'
                else:
                    self.execution.register_for_generation(identifier, row['revision'])
                    candidate = self.candidates.history.reconcile(identifier)
                    if candidate is None:
                        candidate = self.candidates.enqueue(identifier, row['revision'])
                    if candidate['config_sha256'] != self.candidates.config_sha256:
                        state, reason = 'attention', 'configuration_changed'
                    elif candidate['state'] == 'prepared':
                        self.execution.enqueue(identifier, row['revision'])
                        state, reason = 'handed_off', ''
                    elif candidate['state'] in {'queued', 'running', 'model_requested', 'preparing_files'}:
                        state, reason = 'preparing', ''
                    else:
                        state, reason = 'attention', 'candidate_'+candidate['state']
            except Exception:
                state, reason = 'attention', 'workflow_check_failed'
            with self.tasks.transaction() as db: self._event(db, row['id'], state, reason)
        return self.status(identifier)

    def _run(self):
        while not self.stop.is_set():
            with self.tasks.transaction() as db:
                identifiers = [r[0] for r in db.execute('SELECT task_id FROM research_workflows ORDER BY rowid')]
            for identifier in identifiers:
                if self.stop.is_set(): break
                self.advance(identifier)
            self.wake.wait(2); self.wake.clear()

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop.clear(); self.thread = threading.Thread(target=self._run, name='research-workflow', daemon=True); self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread: self.thread.join()
