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
            for table in ('author_resource_requests', 'author_resource_events'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} '
                               f'BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'
                               '"immutable author resource history"); END')

    def _binding(self, identifier):
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
                result.append(dict(id=row['id'], context=context, events=events))
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
        return dict(request_id=row['id'], state=latest['state'], message=latest.get('message',''),
            repository=row['context']['repository'], commit=row['context']['commit'],
            files=[{k:f[k] for k in ('path','size','sha256')} for f in report.get('files',[])],
            source_complete=report.get('source_complete') is True,
            potential_state=(report.get('potential_acquisition') or {}).get('state'),
            failures=deepcopy(report.get('failures',[])),
            recoverable=latest['state'] in {'started','unknown'})

    def status(self, identifier):
        self.tasks.get(identifier)
        history = [self._summary(row) for row in self._rows(identifier)]
        enabled,reason = True,''
        try:self._binding(identifier)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied):
            enabled,reason=False,'固定作者来源、许可或保存的 HPC 连接尚未通过核验。'
        latest = history[-1] if history else None
        return dict(version=1, **FLAGS, task_id=identifier, enabled=enabled,
                    disabled_reason=reason, latest=latest, history=history,
                    calls_model=False, submits_HPC=False)

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
