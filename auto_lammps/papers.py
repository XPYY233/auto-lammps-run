"""Operator paper register and append-only history, never a score publisher."""
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import uuid
from urllib.parse import quote

from .ledger import ACTIVE, TERMINAL, LedgerError
from .manifest import canonical
from .tasks import StaleTask, TaskError, task_id, text

STATUSES = {'pending': '待复现', 'in_progress': '复现中', 'reproduced': '已复现'}
REFERENCE_RESOURCE_FIELDS = ('cores', 'wall_seconds', 'memory_bytes', 'storage_bytes')


def _reference_time(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


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

    def bind_reference_evaluation(self, identifier, identifier_task, evaluation):
        """Trusted controller: A may precede B condition freeze; never permits agent bindings."""
        snapshot = self._reference_snapshot(evaluation)
        with self.tasks.transaction() as db:
            doc=self._read(db,identifier)
            task=self.tasks._read(db,identifier_task)
            if doc['selection']!='selected' or task['mode'] not in {'research', 'reproduction'}:
                raise TaskError('需要已选论文和已关联任务')
            if not db.execute('SELECT 1 FROM paper_tasks WHERE paper_id=? AND task_id=?',(identifier,identifier_task)).fetchone():
                raise TaskError('参考任务未关联此论文')
            self._bind_reference(db, doc, identifier_task, evaluation, snapshot['identity']['task'])

    def _reference_snapshot(self, evaluation):
        if self.ledger is None:
            raise TaskError('未配置参考账本')
        if not isinstance(evaluation, str) or not re.fullmatch('[a-f0-9]{64}', evaluation):
            raise TaskError('参考评测标识无效')
        snapshot = self.ledger.evaluation_snapshot(evaluation)
        identity = snapshot.get('identity') if isinstance(snapshot, dict) else None
        if (not isinstance(identity, dict) or snapshot.get('id') != evaluation
                or identity.get('role') != 'reference'):
            raise TaskError('这里只能关联作者参考 A')
        if not isinstance(identity.get('task'), str) or not re.fullmatch('[a-f0-9]{64}', identity['task']):
            raise TaskError('参考任务摘要无效')
        return snapshot

    def _bind_reference(self, db, doc, identifier_task, evaluation, expected_task_sha256):
        previous = db.execute('SELECT * FROM paper_evaluations WHERE evaluation=?', (evaluation,)).fetchone()
        if previous:
            if (previous['paper_id'] == doc['id'] and previous['task_id'] == identifier_task
                    and previous['task_sha256'] == expected_task_sha256):
                if not db.execute('SELECT 1 FROM paper_revisions WHERE paper_id=? AND event=?',
                                  (doc['id'], 'reference_evaluation_linked:'+evaluation)).fetchone():
                    self._write(db, doc, 'reference_evaluation_linked:'+evaluation)
                return
            raise TaskError('参考运行已有关联，身份与历史不可转移')
        db.execute('INSERT INTO paper_evaluations VALUES (?,?,?,?)',
                   (doc['id'], identifier_task, evaluation, expected_task_sha256))
        self._write(db, doc, 'reference_evaluation_linked:'+evaluation)

    def link_reference_task(self, identifier, identifier_task, evaluation):
        """Trusted controller-only linkage of an existing A to its user-visible task.

        This does not authorize a job, freeze B, alter a request or validate the
        scientific match. Browser link_task deliberately remains reproduction-only.
        Both append-only relationships and their history commit atomically.
        """
        snapshot = self._reference_snapshot(evaluation)
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            task = self.tasks._read(db, identifier_task)
            if doc['selection'] != 'selected' or task['mode'] not in {'research', 'reproduction'}:
                raise TaskError('需要已选论文和研究或复现任务')
            previous = db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?',
                                  (identifier_task,)).fetchone()
            if previous and previous['paper_id'] != identifier:
                raise TaskError('该任务已关联其他论文，不可转移')
            if previous is None:
                db.execute('INSERT INTO paper_tasks VALUES (?,?)', (identifier, identifier_task))
                self._write(db, doc, 'task_linked:'+identifier_task)
            self._bind_reference(db, doc, identifier_task, evaluation, snapshot['identity']['task'])
        return self.reference_progress(identifier_task)

    def _reference_request(self, evaluation, request):
        """Project snapshot and immutable resources without raw request payloads."""
        if (not isinstance(request, dict) or not isinstance(request.get('id'), str)
                or not re.fullmatch('[a-f0-9]{32}', request['id'])
                or request.get('state') not in {*ACTIVE, *TERMINAL}):
            raise TaskError('参考请求状态不可核对')
        original = self.ledger.get(request['id'])
        if (not isinstance(original, dict) or original.get('evaluation') != evaluation
                or original.get('id') != request['id']):
            raise TaskError('参考请求身份不一致')
        resources = json.loads(original['resources'])
        if (not isinstance(resources, dict) or set(resources) != set(REFERENCE_RESOURCE_FIELDS)
                or any(type(resources[k]) is not int or resources[k] <= 0 for k in REFERENCE_RESOURCE_FIELDS)):
            raise TaskError('参考资源记录不可核对')
        result = {key: request[key] for key in ('id', 'job_id', 'state', 'dispatch_claimed',
                                               'charge_core_seconds', 'actual_core_seconds', 'accounted')}
        if (type(result['charge_core_seconds']) is not int or result['charge_core_seconds'] < 0
                or result['dispatch_claimed'] not in (0, 1) or result['accounted'] not in (0, 1)
                or (result['actual_core_seconds'] is not None and
                    (type(result['actual_core_seconds']) is not int or result['actual_core_seconds'] < 0))
                or (result['job_id'] is not None and
                    (not isinstance(result['job_id'], str) or not re.fullmatch('[1-9][0-9]{0,19}', result['job_id'])))):
            raise TaskError('参考费用或作业号不可核对')
        result['resources'] = {key: resources[key] for key in REFERENCE_RESOURCE_FIELDS}
        storage = original.get('charge_storage_bytes')
        if type(storage) is not int or storage < 0:
            raise TaskError('参考存储费用不可核对')
        result['charge_storage_bytes'] = storage
        events = request.get('events')
        if not isinstance(events, list):
            raise TaskError('参考历史不可核对')
        result['events'] = []
        for event in events:
            if (not isinstance(event, dict) or not isinstance(event.get('kind'), str)
                    or not re.fullmatch('[a-z_]{1,100}', event['kind'])
                    or not _reference_time(event.get('at'))):
                raise TaskError('参考历史不可核对')
            result['events'].append({key: event[key] for key in ('kind', 'at')})
        monitor = request.get('monitoring')
        result['monitoring'] = None
        if monitor is not None:
            if (not isinstance(monitor, dict) or any(not _reference_time(monitor.get(k))
                    for k in ('last_checked', 'next_due'))
                    or type(monitor.get('failures')) is not int or monitor['failures'] < 0
                    or not isinstance(monitor.get('reason'), str)
                    or not re.fullmatch('[a-z_]{0,80}', monitor['reason'])):
                raise TaskError('参考跟进记录不可核对')
            result['monitoring'] = {key: monitor[key] for key in ('last_checked', 'next_due', 'reason', 'failures')}
        return result

    def reference_progress(self, identifier_task):
        """Read known A bindings even before a report; no model or scheduler I/O."""
        with self.tasks.transaction() as db:
            self.tasks._read(db, identifier_task)
            rows = db.execute('SELECT e.*,r.document FROM paper_evaluations e JOIN papers p ON p.id=e.paper_id '
                    'JOIN paper_revisions r ON r.paper_id=p.id AND r.revision=p.revision '
                    'WHERE e.task_id=? AND EXISTS (SELECT 1 FROM paper_revisions h WHERE h.paper_id=e.paper_id '
                    "AND h.event=('reference_evaluation_linked:' || e.evaluation)) ORDER BY e.rowid",
                    (identifier_task,)).fetchall()
        entries = []
        for binding in rows:
            doc = json.loads(binding['document'])
            entry = dict(paper={key: doc[key] for key in ('id', 'title', 'doi', 'scope')},
                         evaluation=dict(id=binding['evaluation'], available=False))
            entry['paper']['doi_url'] = 'https://doi.org/'+quote(doc['doi'], safe='/')
            try:
                snapshot = self._reference_snapshot(binding['evaluation'])
                if snapshot['identity']['task'] != binding['task_sha256']:
                    raise TaskError('参考评测任务摘要改变')
                visible = {key: snapshot[key] for key in ('id', 'max_attempts', 'remaining_attempts',
                                                          'dispatch_claims', 'reserved_attempts')}
                if (any(type(visible[key]) is not int or visible[key] < 0
                        for key in ('dispatch_claims', 'reserved_attempts'))
                        or any(visible[key] is not None and (type(visible[key]) is not int or visible[key] < 0)
                               for key in ('max_attempts', 'remaining_attempts'))
                        or not isinstance(snapshot.get('requests'), list)):
                    raise TaskError('参考次数记录不可核对')
                visible['requests'] = [self._reference_request(binding['evaluation'], request)
                                       for request in snapshot['requests']]
                entry['evaluation'] = dict(visible, available=True)
            except (LedgerError, TaskError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                entry['evaluation']['reason'] = 'reference_record_unavailable'
            entries.append(entry)
        return dict(schema_version=1, task_id=identifier_task,
                    available=all(item['evaluation']['available'] for item in entries), entries=entries,
                    scientific_validation='not_performed', execution_authorized=False)

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

    def record_source_search(self, identifier, report):
        """Trusted reference controller only; never accept browser-written results."""
        if (not isinstance(report, dict) or report.get('schema_version') != 1
                or report.get('state') not in {'finished', 'partial'}
                or report.get('scientific_validation') is not False
                or report.get('author_identity_verified') is not False
                or len(canonical(report)) > 60000):
            raise TaskError('源码检索记录不完整')
        search_id = task_id(report.get('id'))
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            if report.get('doi') != doc['doi'] or report.get('title') != doc['title']:
                raise TaskError('源码检索与论文身份不一致')
            if db.execute('SELECT 1 FROM paper_revisions WHERE paper_id=? AND event=?',
                          (identifier, 'source_search_completed:'+search_id)).fetchone():
                return self._read(db, identifier)
            doc['source_discovery'] = report
            self._write(db, doc, 'source_search_completed:'+search_id)
        return self.get(identifier)

    def record_potential_acquisition(self, identifier, report):
        """Reference controller records collection; grants no use or score approval."""
        if (not isinstance(report, dict) or report.get('schema_version') != 1
                or report.get('state') not in {'finished', 'partial'}
                or any(report.get(k) is not False for k in
                       ('scientific_validation', 'engine_verified', 'catalog_admitted', 'execution_authorized'))
                or not isinstance(report.get('files'), list) or not isinstance(report.get('bindings'), list)
                or len(canonical(report)) > 150000):
            raise TaskError('势函数获取记录不完整')
        acquisition_id = task_id(report.get('id'))
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            candidates = doc.get('source_discovery', {}).get('candidates', [])
            if not any(c.get('association') == 'doi_and_title' and
                       c.get('repository') == report.get('repository') and
                       c.get('commit') == report.get('commit') for c in candidates):
                raise TaskError('势函数来源与已核对的论文仓库不一致')
            if db.execute('SELECT 1 FROM paper_revisions WHERE paper_id=? AND event=?',
                          (identifier, 'potential_acquired:'+acquisition_id)).fetchone():
                return self._read(db, identifier)
            doc['potential_acquisition'] = report
            self._write(db, doc, 'potential_acquired:'+acquisition_id)
        return self.get(identifier)

    def record_reference_resources(self, identifier, report):
        """Remote source identities for the reference side; no scientific pass."""
        if (not isinstance(report, dict) or report.get('schema_version') != 1
                or report.get('state') not in {'finished', 'partial', 'unknown'}
                or report.get('location') != 'hpc'
                or any(report.get(k) is not False for k in
                       ('scientific_validation', 'execution_authorized', 'author_identity_verified'))
                or len(canonical(report)) > 500000):
            raise TaskError('超算参考资料记录不完整')
        acquisition_id = task_id(report.get('id'))
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            if not any(c.get('association') == 'doi_and_title' and
                       c.get('repository') == report.get('repository') and
                       c.get('commit') == report.get('commit')
                       for c in doc.get('source_discovery', {}).get('candidates', [])):
                raise TaskError('超算参考资料与已核对的论文仓库不一致')
            event = 'reference_resources_prepared:'+acquisition_id+':'+report['state']
            if db.execute('SELECT 1 FROM paper_revisions WHERE paper_id=? AND event=?', (identifier, event)).fetchone():
                return self._read(db, identifier)
            doc['reference_resources'] = report
            self._write(db, doc, event)
        return self.get(identifier)

    def record_engine_preparation(self, identifier, report):
        """Source preparation status only, no engine deployment or execution grant."""
        from .manifest import sha256
        if (not isinstance(report, dict) or report.get('schema_version') != 1
                or report.get('state') not in {'source_ready', 'failed', 'unknown'}
                or report.get('location') != 'hpc'
                or any(report.get(k) is not False for k in
                       ('built', 'scientific_validation', 'environment_verified', 'execution_authorized'))
                or len(canonical(report)) > 20000):
            raise TaskError('计算环境准备记录不完整')
        preparation_id = task_id(report.get('id'))
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            model_report = doc.get('potential_acquisition')
            if not model_report or report.get('requirements', {}).get('model_report_sha256') != sha256(canonical(model_report)):
                raise TaskError('计算环境要求与当前势函数记录不一致')
            event = 'engine_source_prepared:'+preparation_id+':'+report['state']
            if db.execute('SELECT 1 FROM paper_revisions WHERE paper_id=? AND event=?', (identifier, event)).fetchone():
                return self._read(db, identifier)
            doc['engine_preparation'] = report
            self._write(db, doc, event)
        return self.get(identifier)

    def list(self, transform=None):
        """The caller may adjust each document (for example with a user-accepted scope);
        the tab counts are computed from the adjusted documents."""
        with self.tasks.transaction() as db:
            ids=[r[0] for r in db.execute('SELECT id FROM papers ORDER BY rowid DESC')]
        papers=[self.get(i) for i in ids]
        if transform is not None:
            papers=[transform(document) for document in papers]
        return dict(papers=papers,counts={status:sum(p['selection']=='selected' and p['status']==status for p in papers)
                                         for status in STATUSES},candidate_count=sum(p['selection']=='candidate' for p in papers),
                    statuses=STATUSES,score_publication_available=False)
