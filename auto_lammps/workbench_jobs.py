"""Durable background dispatch of the existing native literature workbench.

Only a demonstrably undispatched queue may start after a restart. The original
bridge retains every model call, failure and continuation; this service never
reconstructs scientific outputs or retries an unknown extraction.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat

from .manifest import canonical, private_directory, sha256
from .tasks import TaskError, task_id
from .workbench_adapter import WorkbenchScientificAdapter
from .workbench_bridge import _digest


VERSION=1
ACTIVE={'queued','dispatching'}
BLOCKING=ACTIVE|{'interrupted','unknown','failed_or_unknown'}
LABELS={'queued':'文献提取已排队','dispatching':'文献工作台正在提取',
    'paused':'来源或配置变化 · 提取暂停','interrupted':'提取进程已结束 · 待核对',
    'failed':'文献提取未完成','failed_or_unknown':'模型状态未知 · 待核对',
    'unknown':'原提取状态未知 · 待核对','completed':'文献提取已保存',
    'completed_with_limitations':'文献提取已保存 · 存在限制',
    'no_evidence_published':'提取结束 · 尚无已发布证据'}


class WorkbenchExtractionJobs:
    def __init__(self, bridge, context_provider, *, max_workers=2):
        if not callable(context_provider) or type(max_workers) is not int or not 1<=max_workers<=4:
            raise TaskError('文献后台服务配置无效。')
        self.bridge,self.tasks=bridge,bridge.papers.tasks
        self.context_provider=context_provider
        self.max_workers=max_workers
        self.locks=private_directory(self.tasks.path.parent/'workbench-job-locks')
        self.pool=ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix='workbench-extraction')
        self.closed=False
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workbench_jobs (id TEXT PRIMARY KEY, '
                'task_id TEXT NOT NULL REFERENCES tasks(id), source_sha256 TEXT NOT NULL, '
                'context_sha256 TEXT NOT NULL, document TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workbench_job_events ('
                'job_id TEXT NOT NULL REFERENCES workbench_jobs(id), sequence INTEGER NOT NULL, '
                'at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id,sequence))')
            for table in ('workbench_jobs','workbench_job_events'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} '
                        "BEGIN SELECT RAISE(ABORT, 'immutable workbench job history'); END")

    @staticmethod
    def _task(db,identifier):
        task_id(identifier)
        if not db.execute('SELECT 1 FROM tasks WHERE id=?',(identifier,)).fetchone():
            raise TaskError('没有这项任务。')

    @staticmethod
    def _event(db,request_id,state,payload=None):
        sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM workbench_job_events WHERE job_id=?',
            (request_id,)).fetchone()[0]
        db.execute('INSERT INTO workbench_job_events VALUES (?,?,?,?,?)',
            (request_id,sequence,datetime.now(timezone.utc).isoformat(),state,canonical(payload or {}).decode()))

    def _row(self,identifier,request_id):
        with self.tasks.transaction() as db:
            self._task(db,identifier)
            row=db.execute('SELECT * FROM workbench_jobs WHERE id=?',(request_id,)).fetchone()
            if row is None:return None
            if row['task_id']!=identifier:raise TaskError('提取请求属于其他任务。')
            events=[dict(r) for r in db.execute('SELECT * FROM workbench_job_events WHERE job_id=? ORDER BY sequence',
                (request_id,))]
        document=json.loads(row['document'])
        if (sha256(canonical(document))!=row['context_sha256']
                or document['intent']['request_id']!=request_id
                or document['intent']['task_id']!=identifier
                or document['intent']['source_sha256']!=row['source_sha256'] or not events):
            raise TaskError('文献排队身份或记录摘要不一致。')
        for event in events:
            event['payload']=json.loads(event['payload'])
            event['label']=LABELS.get(event['state'],event['state'])
        return dict(row),document,events

    def _receipt(self,identifier,request_id):
        return next((item for item in self.bridge.history(identifier) if item['request_id']==request_id),None)

    def get(self,identifier,request_id=None):
        """Credential-free status, including existing synchronous bridge history."""
        if request_id is None:return self.history(identifier)
        task_id(request_id)
        row=self._row(identifier,request_id)
        receipt=self._receipt(identifier,request_id)
        if row is None:
            if receipt is None:return None
            return {'request_id':request_id,'task_id':identifier,'state':receipt['state'],
                'label':LABELS.get(receipt['state'],receipt['state']),'events':[],
                'receipt':receipt,'legacy':True,'source_sha256':receipt['source_sha256'],
                'automatic_retry':False,'execution_authorized':False}
        saved,document,events=row
        state=events[-1]['state']
        return {'request_id':request_id,'task_id':identifier,'state':state,'label':LABELS.get(state,state),
            'events':events,'receipt':receipt,'legacy':False,'at':saved['at'],
            'updated_at':events[-1]['at'],'source_sha256':saved['source_sha256'],
            'paper_id':document['intent']['paper_id'],'context_sha256':saved['context_sha256'],
            'automatic_retry':False,'execution_authorized':False}

    def history(self,identifier):
        with self.tasks.transaction() as db:
            self._task(db,identifier)
            ids=[r[0] for r in db.execute('SELECT id FROM workbench_jobs WHERE task_id=? ORDER BY at,id',
                (identifier,))]
        ids.extend(item['request_id'] for item in self.bridge.history(identifier) if item['request_id'] not in ids)
        return [self.get(identifier,request_id) for request_id in ids]

    def _context(self,identifier,request_id,options):
        supplied=self.context_provider(identifier)
        if not isinstance(supplied,dict) or supplied.get('enabled') is not True:
            raise TaskError('文献提取授权或连接已停用。')
        binding,client=supplied['binding'],supplied['client']
        if binding.pdf_sha256!=options['source_sha256']:
            raise TaskError('论文来源已变化；请刷新后再提取。')
        kwargs={key:options[key] for key in ('force_rescan','repair_visuals','resume_of')}
        kwargs.update(connection_revision=supplied['connection_revision'],
                      credential_generation=supplied['credential_generation'])
        intent=self.bridge.extraction_identity(identifier,request_id,binding,client,**kwargs)
        with self.tasks.transaction() as db:
            self._task(db,identifier)
            ended=db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('delete','finish')",
                (identifier,)).fetchone()
            control=db.execute('SELECT action FROM task_control WHERE task_id=? ORDER BY sequence DESC LIMIT 1',
                (identifier,)).fetchone()
        if ended or (control and control[0]=='pause'):
            raise TaskError('任务已暂停或结束，不能继续文献提取。')
        source_reader=self.bridge.runtime.native_source_reader
        if not callable(source_reader):
            raise TaskError('原 PDF 字节核验能力尚未连接，未排队或收费。')
        if source_reader(binding.workbench_paper_id)!=binding.pdf_sha256:
            raise TaskError('原 PDF 字节已变化，未继续提取。')
        adapter=WorkbenchScientificAdapter({key:intent[key] for key in
            ('task_id','paper_id','request_id','role','source_sha256','title','doi')}
            |{'workbench_paper_id':binding.workbench_paper_id},
            capability_reader=self.bridge.runtime.native_capability_reader,stage_reader=lambda:None)
        with client.calls.transaction() as db:
            _,effective_policy=client.calls._latest_policy(db)
        frozen={'version':VERSION,'intent':intent,'workbench_paper_id':binding.workbench_paper_id,
            **self.bridge._model_binding(client),'max_requests':client.calls.status()['max_requests'],
            'model_authorization_sha256':sha256(effective_policy.encode()),
            'native_capability_sha256':sha256(canonical(adapter.installed)),
            'native_modules_sha256':sha256(canonical(adapter.installed['modules'])),
            'session_sha256':sha256(self.bridge.runtime.session_id.encode()),
            'controller_sha256':sha256(Path(__file__).read_bytes()),
            'bridge_sha256':sha256(Path(__file__).with_name('workbench_bridge.py').read_bytes()),
            'max_workers':self.max_workers}
        return frozen,binding,client,kwargs

    @staticmethod
    def _same_request(intent,identifier,request_id,options):
        expected={'task_id':identifier,'request_id':request_id,**options}
        if any(intent.get(key)!=value for key,value in expected.items()):
            raise TaskError('提取请求编号已用于其他来源或操作。')

    @staticmethod
    def _admit_source(db,source_sha256,options,*,exclude_request=None):
        """Both histories are checked under the queue's atomic write transaction."""
        for row in db.execute('SELECT i.document intent,r.document receipt FROM workbench_extraction_intents i '
                'LEFT JOIN workbench_extraction_receipts r ON r.id=i.id'):
            intent=json.loads(row['intent'])
            if intent['source_sha256']!=source_sha256:continue
            receipt=json.loads(row['receipt']) if row['receipt'] else None
            if receipt is None or receipt['state'] in {'unknown','failed_or_unknown'}:
                raise TaskError('同一 PDF 原提取状态未知；须先核对，不能再次排队或收费。')
            if (not options['resume_of'] and not options['force_rescan'] and not options['repair_visuals']
                    and (receipt['state'] in {'completed','completed_with_limitations','no_evidence_published'}
                         or receipt.get('model_request_ids'))):
                raise TaskError('同一 PDF 已有提取记录；请查看已有证据或使用原恢复入口。')
        for row in db.execute('SELECT j.id,(SELECT e.state FROM workbench_job_events e WHERE e.job_id=j.id '
                'ORDER BY e.sequence DESC LIMIT 1) state FROM workbench_jobs j WHERE j.source_sha256=?',
                (source_sha256,)):
            if row['id']==exclude_request:continue
            if row['state'] in BLOCKING:
                raise TaskError('同一 PDF 已排队、正在提取或状态待核对；不能换请求编号重复排队。')

    def enqueue(self,identifier,request_id,*,source_sha256,force_rescan=False,repair_visuals=False,resume_of=None):
        task_id(request_id);_digest(source_sha256)
        options=dict(source_sha256=source_sha256,force_rescan=force_rescan,
                     repair_visuals=repair_visuals,resume_of=resume_of)
        row=self._row(identifier,request_id)
        if row:
            self._same_request(row[1]['intent'],identifier,request_id,options)
            return self.get(identifier,request_id)
        legacy=self._receipt(identifier,request_id)
        if legacy:
            self._same_request(legacy,identifier,request_id,options)
            return self.get(identifier,request_id)
        if self.closed:raise TaskError('文献后台服务已关闭，未新增排队。')
        try:
            frozen,binding,client,kwargs=self._context(identifier,request_id,options)
        except Exception:
            raise TaskError('论文来源、模型配置或提取授权未通过核验，未排队或收费。') from None
        if resume_of is not None:
            self.bridge._recovery_context(identifier,resume_of,binding,client,
                connection_revision=kwargs['connection_revision'],credential_generation=kwargs['credential_generation'])
        with self.tasks.transaction() as db:
            prior=db.execute('SELECT document FROM workbench_jobs WHERE id=?',(request_id,)).fetchone()
            if prior:
                self._same_request(json.loads(prior[0])['intent'],identifier,request_id,options)
            else:
                self._admit_source(db,source_sha256,options)
                now=datetime.now(timezone.utc).isoformat()
                db.execute('INSERT INTO workbench_jobs VALUES (?,?,?,?,?,?)',
                    (request_id,identifier,source_sha256,sha256(canonical(frozen)),canonical(frozen).decode(),now))
                self._event(db,request_id,'queued')
        self.pool.submit(self.run,identifier,request_id)
        return self.get(identifier,request_id)

    @contextmanager
    def _lease(self,request_id):
        fd=os.open(self.locks/(task_id(request_id)+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077:
                raise TaskError('文献后台锁身份无效。')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:yield False
            else:yield True
        finally:os.close(fd)

    def run(self,identifier,request_id):
        with self._lease(request_id) as acquired:
            if not acquired:return
            row=self._row(identifier,request_id)
            if row is None or row[2][-1]['state']!='queued':return
            saved,frozen,events=row
            intent=frozen['intent']
            options={key:intent.get(key) for key in ('source_sha256','force_rescan','repair_visuals','resume_of')}
            prior=self._receipt(identifier,request_id)
            if prior is not None:
                state=(prior['state'] if prior['state']!='unknown' and all(
                    prior.get(key)==value for key,value in intent.items()) else 'interrupted')
                with self.tasks.transaction() as db:self._event(db,request_id,state,
                    {'message':'原工作台已登记此请求；须按原记录核对或续接，未自动重复派发。'})
                return
            try:
                current,binding,client,kwargs=self._context(identifier,request_id,options)
                if canonical(current)!=canonical(frozen):raise TaskError('context changed')
            except Exception:
                with self.tasks.transaction() as db:self._event(db,request_id,'paused',
                    {'message':'论文来源、模型配置、原生能力或授权已变化，未派发模型请求。'})
                return
            with self.tasks.transaction() as db:
                try:self._admit_source(db,options['source_sha256'],options,exclude_request=request_id)
                except TaskError:
                    self._event(db,request_id,'paused',
                        {'message':'同一 PDF 的原提取记录已变化；须先核对，未重复派发模型请求。'})
                    return
                self._event(db,request_id,'dispatching')
            changed=False
            def guard():
                nonlocal changed
                try:
                    actual,*_=self._context(identifier,request_id,options)
                    if canonical(actual)!=canonical(frozen):raise TaskError('context changed')
                except Exception:
                    changed=True
                    raise TaskError('文献提取上下文已变化，未继续发送。') from None
            try:
                receipt=self.bridge.extract(identifier,request_id,binding,client,**kwargs,before_model_request=guard)
                try:guard()
                except TaskError:pass
                state='paused' if changed else receipt['state']
                payload={'message':'上下文变化，后续调用已暂停；原调用、失败与原生续接记录保留。'} if changed else {}
            except Exception:
                receipt=self._receipt(identifier,request_id)
                state='paused' if changed else (receipt['state'] if receipt and receipt['state']!='unknown' else 'interrupted')
                payload={'message':'提取未完成；保留原记录，须核对来源或按原工作台规则续接。'}
            with self.tasks.transaction() as db:self._event(db,request_id,state,payload)

    def reconcile(self,identifier,request_id):
        """A real OS lease, never the existence of a lock file, establishes liveness."""
        job=self.get(identifier,request_id)
        if job and not job['legacy'] and job['state']=='dispatching':
            with self._lease(request_id) as acquired:
                if acquired:
                    with self.tasks.transaction() as db:
                        latest=db.execute('SELECT state FROM workbench_job_events WHERE job_id=? '
                            'ORDER BY sequence DESC LIMIT 1',(request_id,)).fetchone()[0]
                        if latest=='dispatching':
                            state='interrupted'
                            if job['receipt'] and job['receipt']['state']!='unknown':
                                intent=json.loads(db.execute('SELECT document FROM workbench_jobs WHERE id=?',
                                    (request_id,)).fetchone()[0])['intent']
                                if all(job['receipt'].get(key)==value for key,value in intent.items()):
                                    state=job['receipt']['state']
                            self._event(db,request_id,state,
                                {'message':'后台执行进程已结束；原请求和原生续接需核对，未自动重发。'})
            job=self.get(identifier,request_id)
        return job

    def start(self):
        with self.tasks.transaction() as db:
            rows=[tuple(row) for row in db.execute('SELECT task_id,id FROM workbench_jobs ORDER BY at,id')]
        for identifier,request_id in rows:
            job=self.reconcile(identifier,request_id)
            if job['state']=='queued':self.pool.submit(self.run,identifier,request_id)

    def close(self,*,wait=False):
        self.closed=True
        self.pool.shutdown(wait=wait,cancel_futures=True)
