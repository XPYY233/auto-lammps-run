"""Register confirmed research work before generation under an existing policy.

This is accounting enrollment, not a signed execution grant or scientific review.
"""
from datetime import datetime, timezone
import re
from pathlib import Path

from .agent_candidates import research_inputs
from .candidate_jobs import CandidateHistory
from .manifest import canonical, sha256
from .runtime_launcher import hash_value
from .tasks import TaskError, StaleTask


# Stable v1 enrollment protocol; implementation fixes must not create new evaluations.
ENROLLMENT_PROTOCOL_SHA256 = '93a0a63b88a8703116b40fa2a9aaf978adf3efc39182417c52bbb635b29846a8'


class ResearchEnrollment:
    def __init__(self,tasks,ledger,*,campaign,system_sha256,policy_sha256):
        if not isinstance(campaign,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',campaign):
            raise ValueError('Invalid enrolled campaign')
        hash_value(system_sha256);hash_value(policy_sha256)
        self.tasks,self.ledger=tasks,ledger
        self.identity=dict(campaign=campaign,system_sha256=system_sha256,policy_sha256=policy_sha256,
                           role='agent',repetition=0,max_attempts=2,source_sha256=ENROLLMENT_PROTOCOL_SHA256)
        self.digest=sha256(canonical(self.identity));self.history=CandidateHistory(tasks)
        self._check_policy()
        from .resource_limits import TASK_CPU_CORE_SECONDS, APPROVAL_SHA256
        self.ledger.approve_task_resource_limit(campaign, TASK_CPU_CORE_SECONDS,
                                               approval_sha256=APPROVAL_SHA256)
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS research_execution_bindings ('
                       'task_id TEXT PRIMARY KEY REFERENCES tasks(id), revision INTEGER NOT NULL, '
                       'condition_sha256 TEXT NOT NULL, evaluation TEXT NOT NULL, scope_sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_research_execution_bindings_{action} "
                           f"BEFORE {action} ON research_execution_bindings "
                           "BEGIN SELECT RAISE(ABORT, 'immutable research enrollment'); END")

    def _check_policy(self):
        with self.ledger._transaction() as db:
            policy=self.ledger._policy(db,self.identity['campaign'])
        if sha256(canonical(policy))!=self.identity['policy_sha256']:
            raise TaskError('计算授权配置已变化，需要核对；原提交历史保留。')

    def get(self,identifier):
        self._check_policy()
        with self.tasks.transaction() as db:
            self.tasks._read(db,identifier)
            row=db.execute('SELECT * FROM research_execution_bindings WHERE task_id=?',(identifier,)).fetchone()
        if row is None:return None
        if not self._matches_policy_scope(row):
            raise TaskError('此任务已经绑定其他计算授权，不能重新登记。')
        return dict(row)

    def _matches_policy_scope(self,row):
        """Approved policy amendments preserve original enrollment, never reset it."""
        if row['scope_sha256']==self.digest:return True
        with self.ledger._transaction() as db:
            policies=[r[0] for r in db.execute('SELECT policy FROM campaigns WHERE id=?',
                (self.identity['campaign'],))]
            policies += [r[0] for r in db.execute('SELECT policy FROM campaign_policy_revisions WHERE campaign=?',
                (self.identity['campaign'],))]
        import json
        return any(sha256(canonical({**self.identity,'policy_sha256':sha256(canonical(json.loads(p)))}))
                   ==row['scope_sha256'] for p in policies)

    def register(self,identifier,revision):
        inputs=research_inputs(self.tasks,identifier,revision)
        self._check_policy()
        with self.tasks.transaction() as db:
            task=self.tasks._read(db,identifier)
            if task['revision']!=revision:raise StaleTask('任务已变化，请刷新后再生成。')
            row=db.execute('SELECT * FROM research_execution_bindings WHERE task_id=?',(identifier,)).fetchone()
            if row:
                if (not self._matches_policy_scope(row) or
                        row['condition_sha256']!=inputs['condition_record_sha256']):
                    raise TaskError('任务与原计算登记不一致，不能重置提交次数。')
                return row['evaluation']
            if db.execute('SELECT 1 FROM candidate_jobs WHERE task_id=?',(identifier,)).fetchone():
                raise TaskError('该方案在自动登记启用前已生成，请核对已有执行身份。')
            evaluation=self.ledger.register_evaluation(self.identity['campaign'],
                task_sha256=inputs['condition_record_sha256'],repetition=0,role='agent',
                system_sha256=self.identity['system_sha256'],max_attempts=2)
            # An interruption between ledger registration and this insert is safe:
            # register_evaluation derives the same identity on the next request.
            db.execute('INSERT INTO research_execution_bindings VALUES (?,?,?,?,?,?)',
                (identifier,revision,inputs['condition_record_sha256'],evaluation,self.digest,
                 datetime.now(timezone.utc).isoformat()))
        return evaluation
