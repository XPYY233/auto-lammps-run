"""Operator paper register and append-only history, never a score publisher."""
from datetime import datetime, timezone
import json
import re
import sqlite3
import uuid
from urllib.parse import quote

from .ledger import LedgerError
from .manifest import canonical
from .tasks import StaleTask, TaskError, task_id, text

STATUSES = {'pending': '待复现', 'in_progress': '复现中', 'reproduced': '已复现'}


def doi_text(value):
    value=text(value, 300).lower()
    value=re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)', '', value)
    if not re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x1f]+',value):
        raise TaskError('请填写有效 DOI')
    return value


class PaperStore:
    def __init__(self, tasks, *, ledger=None):
        self.tasks, self.ledger = tasks, ledger
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, doi TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS paper_revisions (paper_id TEXT NOT NULL REFERENCES papers(id), '
                       'revision INTEGER NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL, document TEXT NOT NULL, '
                       'PRIMARY KEY(paper_id,revision))')
            db.execute('CREATE TABLE IF NOT EXISTS paper_tasks (paper_id TEXT NOT NULL REFERENCES papers(id), '
                       'task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), PRIMARY KEY(paper_id,task_id))')
            db.execute('CREATE TABLE IF NOT EXISTS paper_evaluations (paper_id TEXT NOT NULL REFERENCES papers(id), '
                       'task_id TEXT NOT NULL REFERENCES tasks(id), evaluation TEXT NOT NULL UNIQUE, task_sha256 TEXT NOT NULL, '
                       'PRIMARY KEY(paper_id,evaluation))')
            for table in ('paper_revisions','paper_tasks','paper_evaluations'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable paper history'); END")

    def _read(self, db, identifier):
        row=db.execute('SELECT r.document FROM paper_revisions r JOIN papers p ON p.id=r.paper_id '
                       'AND p.revision=r.revision WHERE p.id=?',(task_id(identifier),)).fetchone()
        if row is None: raise KeyError('文献不存在')
        return json.loads(row['document'])

    def _write(self, db, doc, event):
        doc['revision']+=1
        doc['updated_at']=datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO paper_revisions VALUES (?,?,?,?,?)',
                   (doc['id'],doc['revision'],event,doc['updated_at'],canonical(doc).decode()))
        db.execute('UPDATE papers SET revision=? WHERE id=?',(doc['revision'],doc['id']))
        return doc

    def _edit(self, db, identifier, revision):
        doc=self._read(db,identifier)
        if type(revision) is not int or revision!=doc['revision']:
            raise StaleTask('文献记录已更新，请刷新后再操作')
        return doc

    def add(self, title, doi, scope, note):
        doc=dict(id=uuid.uuid4().hex,revision=0,title=text(title,500),doi=doi_text(doi),
                 scope=text(scope,4000),note=text(note,4000),selection='candidate')
        with self.tasks.transaction() as db:
            if db.execute('SELECT 1 FROM papers WHERE doi=?',(doc['doi'],)).fetchone():
                raise TaskError('该 DOI 已在清单中；不能重建文献来隐藏历史')
            db.execute('INSERT INTO papers VALUES (?,?,0)',(doc['id'],doc['doi']))
            self._write(db,doc,'candidate_added')
        return self.get(doc['id'])

    def select(self, identifier, revision):
        with self.tasks.transaction() as db:
            doc=self._edit(db,identifier,revision)
            if doc['selection']=='selected': raise TaskError('文献已选入计划')
            doc['selection']='selected'
            self._write(db,doc,'paper_selected')
        return self.get(identifier)

    def link_task(self, identifier, revision, identifier_task):
        with self.tasks.transaction() as db:
            doc=self._edit(db,identifier,revision)
            task=self.tasks._read(db,identifier_task)
            if task['mode']!='reproduction': raise TaskError('只能关联文献复现任务')
            if db.execute('SELECT 1 FROM paper_tasks WHERE task_id=?',(identifier_task,)).fetchone():
                raise TaskError('该任务已有关联，不能转移或重复记录')
            db.execute('INSERT INTO paper_tasks VALUES (?,?)',(identifier,identifier_task))
            self._write(db,doc,'task_linked:'+identifier_task)
        return self.get(identifier)

    def bind_evaluation(self, identifier, identifier_task, evaluation, expected_task_sha256):
        """Trusted controller only: no browser route. Does not create/reset quotas."""
        if self.ledger is None: raise TaskError('未配置受信提交账本')
        snapshot=self.ledger.evaluation_snapshot(evaluation)
        if snapshot['identity']['task']!=expected_task_sha256:
            raise TaskError('评测任务身份不一致')
        with self.tasks.transaction() as db:
            doc=self._read(db,identifier)
            task=self.tasks._read(db,identifier_task)
            if doc['selection']!='selected' or task['status']!='conditions_frozen':
                raise TaskError('必须先选定论文并冻结关联任务条件')
            if not db.execute('SELECT 1 FROM paper_tasks WHERE paper_id=? AND task_id=?',(identifier,identifier_task)).fetchone():
                raise TaskError('任务未关联此论文')
            if db.execute('SELECT 1 FROM paper_evaluations WHERE evaluation=?',(evaluation,)).fetchone():
                raise TaskError('该评测已关联；历史不能重置或转移')
            db.execute('INSERT INTO paper_evaluations VALUES (?,?,?,?)',
                       (identifier,identifier_task,evaluation,expected_task_sha256))
            self._write(db,doc,'evaluation_linked:'+evaluation)

    def get(self, identifier):
        with self.tasks.transaction() as db:
            doc=self._read(db,identifier)
            task_ids=[r[0] for r in db.execute('SELECT task_id FROM paper_tasks WHERE paper_id=?',(identifier,))]
            bindings=[dict(r) for r in db.execute('SELECT * FROM paper_evaluations WHERE paper_id=?',(identifier,))]
            history=[dict(r) for r in db.execute('SELECT revision,event,at FROM paper_revisions WHERE paper_id=? ORDER BY revision',(identifier,))]
        tasks=[self.tasks.get(t) for t in task_ids]
        evaluations=[]
        for binding in bindings:
            try:
                if self.ledger is None: raise TaskError('未配置提交账本')
                snapshot=self.ledger.evaluation_snapshot(binding['evaluation'])
                if snapshot['identity']['task']!=binding['task_sha256']: raise TaskError('评测身份不一致')
                evaluations.append(snapshot | {'task_id':binding['task_id'],'available':True})
            except (LedgerError,TaskError,sqlite3.Error,OSError):
                evaluations.append(dict(id=binding['evaluation'],task_id=binding['task_id'],available=False))
        unavailable=any(not e['available'] for e in evaluations)
        started=any(e.get('dispatch_claims',0)>0 for e in evaluations)
        exhausted=any(e.get('remaining_attempts')==0 and all(r['state'] in
                      {'rejected','failed','cancelled','cancelled_before_dispatch','timeout','completed'}
                      for r in e.get('requests',[])) for e in evaluations)
        # Scheduler COMPLETED and an operator's confirmation cannot publish a
        # scientific success. The independent scoring publication gate is not
        # implemented yet, so this version never emits 'reproduced'.
        status='in_progress' if started or unavailable else 'pending'
        if unavailable: stage='提交记录暂不可读，不能据此认定未提交或重置额度'
        elif exhausted: stage='存在额度已用尽的评测，已停止重试；尚无独立科学核验结论'
        elif started: stage='已存在记账派发；逐次结果见历史。尚无独立科学核验结论'
        elif bindings: stage='已关联正式账本，尚未记账派发'
        elif tasks: stage='正在准备任务条件；尚未关联正式评测'
        else: stage='尚未建立计算任务'
        return doc | dict(doi_url='https://doi.org/'+quote(doc['doi'],safe='/'),status=status,stage=stage,
                          history=history,evaluations=evaluations,
                          tasks=[dict(id=t['id'],title=t['title'],status=t['status'],revision=t['revision'],
                                      history=self.tasks.history(t['id'])) for t in tasks],
                          score_publication_available=False,execution_authorized=False)

    def list(self):
        with self.tasks.transaction() as db:
            ids=[r[0] for r in db.execute('SELECT id FROM papers ORDER BY rowid DESC')]
        papers=[self.get(i) for i in ids]
        return dict(papers=papers,counts={status:sum(p['selection']=='selected' and p['status']==status for p in papers)
                                         for status in STATUSES},candidate_count=sum(p['selection']=='candidate' for p in papers),
                    statuses=STATUSES,score_publication_available=False)
