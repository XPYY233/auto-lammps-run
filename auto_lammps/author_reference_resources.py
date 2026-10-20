"""User-triggered reference acquisition through the existing HPC-only tool.

An existing licensed paper/source binding fixes the repository and commit.
The browser supplies only an operation identity, never paths or credentials.
Acquisition is distinct from a model proposal and never submits physics.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import PurePosixPath
import re
import uuid

from .manifest import canonical, private_directory, sha256
from .remote_resources import RemoteResources, tool_bundle
from .reference_resource_worker import validate_request
from .hpc_transport import SavedHPCTransport
from .tasks import TaskError, task_id
from .runtime_launcher import ExecutionDenied


FLAGS = dict(scope='reference_only', B_input=False, execution_authorized=False,
             physical_evaluation_performed=False, scientific_validation=False)


class AuthorReferenceResources:
    def __init__(self, workspace, *, acquirer_factory=RemoteResources):
        self.workspace = workspace
        self.tasks = workspace.tasks
        self.factory = acquirer_factory
        self.audit = private_directory(self.tasks.path.parent/'author-resource-acquisitions')
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_resource_requests ('
                       'id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), '
                       'document TEXT NOT NULL, sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS author_resource_events ('
                       'id TEXT NOT NULL REFERENCES author_resource_requests(id), '
                       'sequence INTEGER NOT NULL, document TEXT NOT NULL, sha256 TEXT NOT NULL, '
                       'PRIMARY KEY(id,sequence))')
            db.execute('CREATE TABLE IF NOT EXISTS author_resource_inspections ('
                       'id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES author_resource_requests(id), '
                       'task_id TEXT NOT NULL REFERENCES tasks(id), document TEXT NOT NULL, '
                       'sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            for table in ('author_resource_requests', 'author_resource_events', 'author_resource_inspections'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} '
                               f'BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'
                               '"immutable author resource history"); END')

    def _binding(self, identifier):
        if identifier not in self.workspace.bindings and self.workspace.initial is not None:
            return self.workspace.initial.resource_binding(identifier)
        binding, paper, receipt, _ = self.workspace._metadata(identifier, require_scope=False)
        transport = getattr(binding['reader'], 'transport', None)
        if not isinstance(transport, SavedHPCTransport):
            raise TaskError('请先连接作者来源已固定的 HPC 版本；未借用其他连接。')
        root = str(PurePosixPath(transport.identity['profile']['work_directory'])/
                   'Auto-LAMMPS-Resources'/'author-sources')
        context = dict(version=1, **FLAGS, task_id=identifier, paper_id=paper['id'],
            repository=receipt['repository'], commit=receipt['commit'],
            original_receipt_sha256=sha256(canonical(receipt)),
            license_approval_sha256=binding['reader'].policy['license_approval_sha256'],
            transport_identity_sha256=sha256(canonical(transport.identity)),
            transport_revision=transport.revision, host_alias=transport.host_alias,
            remote_directory=root, storage_bytes=512*1024**2,
            tool_bundle_sha256=sha256(tool_bundle()))
        return context, transport

    def _rows(self, identifier):
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT * FROM author_resource_requests WHERE task_id=? '
                              'ORDER BY rowid', (identifier,)).fetchall()
            result = []
            for row in rows:
                context = json.loads(row['document'])
                if context.get('task_id')!=identifier or sha256(canonical(context)) != row['sha256']:
                    raise TaskError('作者资料原请求摘要不一致；未重新获取。')
                events = []
                for item in db.execute('SELECT * FROM author_resource_events WHERE id=? '
                                       'ORDER BY sequence', (row['id'],)):
                    document = json.loads(item['document'])
                    if sha256(canonical(document)) != item['sha256']:
                        raise TaskError('作者资料历史摘要不一致；保留原记录。')
                    events.append(document)
                inspections=[]
                for item in db.execute('SELECT * FROM author_resource_inspections WHERE request_id=? '
                                       'ORDER BY rowid',(row['id'],)):
                    document=json.loads(item['document'])
                    if (item['task_id']!=identifier or sha256(canonical(document))!=item['sha256']
                            or any(document.get(key)!=value or type(document.get(key)) is not type(value)
                                   for key,value in dict(version=1,**FLAGS).items())
                            or document.get('task_id')!=identifier or document.get('request_id')!=row['id']
                            or document.get('inspection_id')!=item['id']
                            or document.get('source_context')!=context
                            or not re.fullmatch('[a-f0-9]{64}',str(document.get('inspector_bundle_sha256','')))):
                        raise TaskError('作者资料清单核对记录摘要或原身份不一致；保留原获取记录。')
                    request=self._inspection_request(context,row['id'],document.get('acquisition_receipt'))
                    if (document.get('request')!=request
                            or not any(event.get('report')==document.get('acquisition_receipt') for event in events)):
                        raise TaskError('作者资料清单核对请求摘要不一致；未重取原件。')
                    self._inspection_report(request,document.get('report'))
                    inspections.append(document)
                result.append(dict(id=row['id'], context=context, events=events, inspections=inspections))
        return result

    def _event(self, request, state, *, report=None, message=''):
        document = dict(at=datetime.now(timezone.utc).isoformat(), state=state,
                        report=report, message=message)
        data = canonical(document)
        with self.tasks.transaction() as db:
            sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM '
                                  'author_resource_events WHERE id=?', (request,)).fetchone()[0]
            db.execute('INSERT INTO author_resource_events VALUES(?,?,?,?)',
                       (request, sequence, data.decode(), sha256(data)))

    @staticmethod
    def _report(context, request, report):
        """Validate the returned metadata independently of the browser view."""
        expected_request = dict(id=request, repository=context['repository'], commit=context['commit'],
            directory=context['remote_directory'], storage_bytes=context['storage_bytes'],
            bundle_sha256=context['tool_bundle_sha256'])
        validate_request(expected_request)
        if (not isinstance(report, dict) or len(canonical(report)) > 500000
                or any(report.get(k) != v for k,v in dict(schema_version=1, id=request,
                    repository=context['repository'], commit=context['commit'], location='hpc',
                    request_sha256=sha256(canonical(expected_request)),
                    bundle_sha256=context['tool_bundle_sha256'], scientific_validation=False,
                    execution_authorized=False, author_identity_verified=False).items())
                or report.get('state') not in {'finished', 'partial', 'unknown'}):
            raise TaskError('作者资料返回与原仓库、版本或工具身份不符。')
        files = report.get('files')
        if not isinstance(files,list) or len(files)>128:
            raise TaskError('作者资料文件清单无效。')
        from .potential_acquisition import safe_path
        seen = set()
        for row in files:
            name = safe_path(row['path'])
            if (name in seen or type(row.get('size')) is not int or row['size']<0
                    or row.get('git_mode') not in {'100644','100755'}
                    or not re.fullmatch('[a-f0-9]{64}',str(row.get('sha256','')))
                    or not re.fullmatch('[a-f0-9]{40}',str(row.get('blob_sha1','')))):
                raise TaskError('作者资料文件身份或清单重复，未接入。')
            seen.add(name)
        if report['state']!='unknown':
            if (report.get('remote_directory') != context['remote_directory']+'/'+request
                    or report.get('inventory_sha256') != sha256(canonical(files))
                    or type(report.get('source_complete')) is not bool):
                raise TaskError('作者资料远端位置或完整清单摘要不符。')
            if sum(row['size'] for row in files)>64*1024**2 or (report['source_complete'] and not files):
                raise TaskError('作者原件范围为空或超出获取工具容量。')
        elif report.get('source_complete') is not False or files:
            raise TaskError('未知获取不能冒称已有完整原件。')
        return deepcopy(report)

    @staticmethod
    def _summary(row):
        latest = row['events'][-1] if row['events'] else dict(state='unknown',report=None,message='')
        report = latest.get('report') or {}
        inspections=[dict(inspection_id=item['inspection_id'],at=item['at'],state=item['report']['state'],
                          message=item['message'],report=deepcopy(item['report']))
                     for item in row.get('inspections',[])]
        return dict(request_id=row['id'], state=latest['state'], message=latest.get('message',''),
            repository=row['context']['repository'], commit=row['context']['commit'],
            files=[{k:f[k] for k in ('path','size','sha256')} for f in report.get('files',[])],
            source_complete=report.get('source_complete') is True,
            potential_state=(report.get('potential_acquisition') or {}).get('state'),
            failures=deepcopy(report.get('failures',[])),
            recoverable=latest['state'] in {'started','unknown'},
            can_inspect_tree=(latest['state'] in {'finished','partial'}
                              and bool(re.fullmatch('[a-f0-9]{40}',str(report.get('tree_sha1',''))))),
            inspection=inspections[-1] if inspections else None,inspections=inspections)

    def status(self, identifier):
        self.tasks.get(identifier)
        rows=self._rows(identifier)
        history = [self._summary(row) for row in rows]
        enabled,reason = True,''
        try:
            context,_=self._binding(identifier)
            if identifier not in self.workspace.bindings and self.workspace.initial is not None:
                raise TaskError('请先核对固定仓库清单与许可证，再由应用 AI 选择本次范围所需原件。')
            for row,summary in zip(rows,history):
                summary['can_inspect_tree'] &= self._same_binding(row['context'],context)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied):
            enabled,reason=False,'固定作者来源、许可或保存的 HPC 连接尚未通过核验。'
            for summary in history:summary['can_inspect_tree']=False
        latest = history[-1] if history else None
        return dict(version=1, **FLAGS, task_id=identifier, enabled=enabled,
                    disabled_reason=reason, latest=latest, history=history,
                    calls_model=False, submits_HPC=False)

    @staticmethod
    def _same_binding(previous,current):
        # The old acquisition tool identity is immutable. A new diagnostic
        # bundle may inspect that old cache without acquiring the repository.
        return ({key:value for key,value in previous.items() if key!='tool_bundle_sha256'}
                == {key:value for key,value in current.items() if key!='tool_bundle_sha256'})

    @classmethod
    def _inspection_request(cls,context,request,receipt):
        receipt=cls._report(context,request,receipt)
        if (receipt['state'] not in {'finished','partial'}
                or not re.fullmatch('[a-f0-9]{40}',str(receipt.get('tree_sha1','')))):
            raise TaskError('原获取尚无已核验的固定树记录；请先核对原请求。')
        original=dict(id=request,repository=context['repository'],commit=context['commit'],
                      directory=context['remote_directory'],storage_bytes=context['storage_bytes'],
                      bundle_sha256=context['tool_bundle_sha256'])
        return dict(version=1,acquisition_request=original,
                    receipt_sha256=sha256(canonical(receipt)),tree_sha1=receipt['tree_sha1'])

    @staticmethod
    def _inspection_report(request,report):
        if isinstance(report,dict) and report.get('state')=='inspected':
            return RemoteResources.validate_tree_report(request,report)
        expected=dict(version=1,state='unknown',**FLAGS,
            request_sha256=sha256(canonical(request)),acquisition_id=request['acquisition_request']['id'],
            repository=request['acquisition_request']['repository'],commit=request['acquisition_request']['commit'],
            receipt_sha256=request['receipt_sha256'],tree_sha1=request['tree_sha1'],entries=[],summary=None)
        if (not isinstance(report,dict) or set(report) not in (set(expected),set(expected)|{'observation_failure'})
                or any(report.get(key)!=value or type(report.get(key)) is not type(value)
                       for key,value in expected.items())
                or ('observation_failure' in report and not re.fullmatch('[A-Za-z0-9_-]{1,100}',
                                                                        str(report['observation_failure'])))):
            raise TaskError('作者资料清单核对返回身份或边界不符；未改变原获取状态。')
        return report

    def inspect_tree(self,identifier,request):
        """Inspect one original cache identity; never acquire or read source."""
        task_id(identifier);task_id(request)
        with self._lease(identifier):
            with self.tasks.transaction() as db:
                owner=db.execute('SELECT task_id FROM author_resource_requests WHERE id=?',(request,)).fetchone()
            if owner is None or owner[0]!=identifier:
                raise TaskError('没有属于本任务的原获取标识；未借用其他任务或新建获取。')
            context,transport=self._binding(identifier)
            saved=next(row for row in self._rows(identifier) if row['id']==request)
            if not self._same_binding(saved['context'],context):
                raise TaskError('原获取的来源、许可或保存连接变化；不能迁移原清单核对。')
            latest=saved['events'][-1] if saved['events'] else {}
            receipt=latest.get('report')
            inspection_request=self._inspection_request(saved['context'],request,receipt)
            for previous in reversed(saved['inspections']):
                if previous['request']==inspection_request and previous['report']['state']=='inspected':
                    return self.status(identifier)
            if len(saved['inspections'])>=32:
                raise TaskError('原请求清单核对记录已达容量；所有原记录保留。')
            policy={key:saved['context'][key] for key in ('host_alias','remote_directory','storage_bytes')}
            policy['operation_id']=request
            report=dict(version=1,state='unknown',**FLAGS,
                request_sha256=sha256(canonical(inspection_request)),acquisition_id=request,
                repository=context['repository'],commit=context['commit'],
                receipt_sha256=inspection_request['receipt_sha256'],tree_sha1=receipt['tree_sha1'],
                entries=[],summary=None)
            try:
                observed=self.factory(self.audit,policy,transport=transport).inspect_tree(receipt)
                self._inspection_report(inspection_request,observed)
                # Revalidate the saved connection after the read as well.
                after,_=self._binding(identifier)
                if not self._same_binding(saved['context'],after):
                    raise TaskError('原获取固定连接变化。')
                report=observed
            except (ValueError,KeyError,TypeError,OSError,ExecutionDenied):
                report['observation_failure']='cached_tree_inspection_unverified'
            document=dict(version=1,**FLAGS,inspection_id=uuid.uuid4().hex,request_id=request,
                task_id=identifier,source_context=deepcopy(saved['context']),request=inspection_request,
                acquisition_receipt=deepcopy(receipt),inspector_bundle_sha256=context['tool_bundle_sha256'],
                report=deepcopy(report),at=datetime.now(timezone.utc).isoformat(),
                message=('已核对原缓存成员元数据；未重新下载、读取源码或确认可执行。'
                         if report['state']=='inspected' else '缓存清单尚未核对；原获取状态和请求保留。'))
            data=canonical(document)
            with self.tasks.transaction() as db:
                db.execute('INSERT INTO author_resource_inspections VALUES(?,?,?,?,?,?)',
                    (document['inspection_id'],request,identifier,data.decode(),sha256(data),document['at']))
            return self.status(identifier)

    @contextmanager
    def _lease(self, identifier):
        descriptor=os.open(self.workspace.locks/(identifier+'.lock'),os.O_CREAT|os.O_NOFOLLOW|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise TaskError('本任务正在处理作者原件或方案；未重复获取。') from None
            yield
        finally:os.close(descriptor)

    def acquire(self, identifier, request):
        task_id(identifier);task_id(request)
        if identifier not in self.workspace.bindings and self.workspace.initial is not None:
            raise TaskError('首次资料接入只允许固定清单核验及所选原件获取，不默认下载整仓。')
        with self._lease(identifier):
            with self.tasks.transaction() as db:
                owner=db.execute('SELECT task_id FROM author_resource_requests WHERE id=?',(request,)).fetchone()
            if owner is not None and owner[0]!=identifier:
                raise TaskError('获取标识属于另一任务；未借用其资料。')
            context,transport=self._binding(identifier)
            rows=self._rows(identifier)
            saved=next((row for row in rows if row['id']==request),None)
            if saved and saved['context']!=context:
                raise TaskError('原获取请求已绑定旧来源/连接/工具；不能重写或换身份重发。')
            for row in rows:
                summary=self._summary(row)
                if row['id']!=request and summary['recoverable']:
                    raise TaskError('已有作者资料请求待核对；请恢复原请求，不创建另一份。')
                if summary['source_complete'] and row['context']==context:
                    return self.status(identifier)
            if saved is None:
                data=canonical(context)
                with self.tasks.transaction() as db:
                    if db.execute('SELECT 1 FROM author_resource_requests WHERE id=?',(request,)).fetchone():
                        raise TaskError('获取标识属于另一任务；未借用其资料。')
                    if len(rows)>=32:raise TaskError('作者资料历史容量已用完；原记录保留。')
                    db.execute('INSERT INTO author_resource_requests VALUES(?,?,?,?,?)',
                        (request,identifier,data.decode(),sha256(data),datetime.now(timezone.utc).isoformat()))
                self._event(request,'started')
            elif saved['events'] and not self._summary(saved)['recoverable']:
                return self.status(identifier)
            policy={k:context[k] for k in ('host_alias','remote_directory','storage_bytes')}
            policy['operation_id']=request
            try:
                report=self.factory(self.audit,policy,transport=transport).acquire(context['repository'],context['commit'])
                report=self._report(context,request,report)
                _,_,original,_=self.workspace._metadata(identifier,require_scope=False)
                files={row['path']:row for row in report['files']}
                for previous in original['files']:
                    updated=files.get(previous['path'])
                    if ((report['source_complete'] and updated is None) or
                        (updated is not None and any(updated[k]!=previous[k] for k in ('size','sha256')))):
                        raise TaskError('补充来源遗漏或改变了原作者文件摘要；未接入原方案。')
            except (ValueError,KeyError,TypeError,OSError,ExecutionDenied):
                self._event(request,'unknown',message='资料获取回执未核对；保留原请求，不能另建重发。')
                return self.status(identifier)
            self._event(request,report['state'],report=report,
                message='作者原件保存在配置的 HPC；取件不等于环境、运行或科学验证。')
            return self.status(identifier)

    def completed(self, identifier):
        context,_=self._binding(identifier)
        for row in reversed(self._rows(identifier)):
            if row['context']!=context or not row['events']:continue
            event=row['events'][-1]
            report=event.get('report')
            if report and report.get('source_complete') is True and event['state'] in {'finished','partial'}:
                return self._report(row['context'],row['id'],report),row['context']
        return None
