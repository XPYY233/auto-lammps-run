"""First pinned tree/license inspection, before any author workspace exists.

The controller binds an existing P scope, catalog association and saved HPC.
The static remote worker fetches only commit/tree metadata and license blobs;
it never fetches scientific source, approves use or evaluates a physical model.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

from .manifest import canonical, sha256
from .potential_acquisition import AcquisitionError, safe_path
from .reference_resource_worker import PublicGitHubReader, validate_request
from .slurm_read import _write_new


VERSION = 1
PURPOSE = 'pinned_tree_and_license_only'
MAX_REPORT = 2 * 1024**2
MAX_LICENSE_FILES = 8
MAX_LICENSE_BYTES = 256 * 1024
MAX_LICENSE_TOTAL = 512 * 1024
FLAGS = dict(scope='reference_only', B_input=False, execution_authorized=False,
    physical_evaluation_performed=False, scientific_validation=False, source_complete=False)
_LICENSE_NAMES = {'license','license.txt','license.md','license.rst','copying','copying.txt','copying.md'}


def _validate_bootstrap_request(request):
    fields={'version','purpose','acquisition_request','binding_sha256'}
    if (not isinstance(request,dict) or set(request)!=fields or type(request['version']) is not int
            or request['version']!=VERSION or request['purpose']!=PURPOSE
            or not isinstance(request['binding_sha256'],str)
            or not re.fullmatch('[a-f0-9]{64}',request['binding_sha256'])):
        raise AcquisitionError('invalid_author_bootstrap_request')
    validate_request(request['acquisition_request'])
    return request


def _once(path,value):
    try:_write_new(path,value)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_size>MAX_REPORT or json.loads(path.read_bytes())!=value:
            raise AcquisitionError('author_bootstrap_identity_conflict') from None


def _unknown(request):
    original=request['acquisition_request']
    return dict(version=VERSION,purpose=PURPOSE,**FLAGS,id=original['id'],state='unknown',
        repository=original['repository'],commit=original['commit'],location='hpc',
        request_sha256=sha256(canonical(request)),binding_sha256=request['binding_sha256'],
        acquisition_request=deepcopy(original),acquisition_receipt=None,tree_request=None,tree_report=None,
        license_evidence=dict(status='unavailable',files=[],use_approved=False,read_approved=False,
            redistribution_approved=False,execution_approved=False),code='receipt_not_available')


def bootstrap_pinned_tree(request,*,action='inspect',transport=None):
    """HPC-only static worker. Reconcile reads its receipt, never GitHub again."""
    from .engine_source_worker import private_directory
    from .author_resource_inventory_worker import inspect_cached_tree
    _validate_bootstrap_request(request)
    if action not in {'inspect','reconcile'}:raise AcquisitionError('invalid_author_bootstrap_action')
    if transport is None and (sys.platform!='linux' or not os.environ.get('SSH_CONNECTION')):
        raise AcquisitionError('author_bootstrap_requires_HPC')
    original=request['acquisition_request'];root=Path(original['directory']);folder=root/original['id']
    outcome=folder/'bootstrap-result.json';intent=folder/'bootstrap-intent.json'
    if action=='reconcile':
        # Do not create a missing directory, intent, HTTP request or result.
        if not intent.exists():return _unknown(request)
        from .runtime_launcher import read_regular
        if json.loads(read_regular(intent,20000,private=True))!=request:
            raise AcquisitionError('author_bootstrap_identity_conflict')
        return json.loads(read_regular(outcome,MAX_REPORT,private=True)) if outcome.exists() else _unknown(request)
    root=private_directory(str(root))
    if folder.exists():
        if not intent.exists():raise AcquisitionError('author_bootstrap_identity_conflict')
        from .runtime_launcher import read_regular
        if json.loads(read_regular(intent,20000,private=True))!=request:
            raise AcquisitionError('author_bootstrap_identity_conflict')
        return json.loads(read_regular(outcome,MAX_REPORT,private=True)) if outcome.exists() else _unknown(request)
    folder.mkdir(mode=0o700)
    _once(intent,request);_once(root/(original['id']+'.lock.json'),original)
    _once(folder/'intent.json',original)
    (folder/'source').mkdir(mode=0o700)  # Empty: compatible metadata, no author source fetched.
    (folder/'licenses').mkdir(mode=0o700)
    report=_unknown(request);reader=PublicGitHubReader(original['repository'],folder,transport=transport)
    def get(endpoint):
        response=reader.get(endpoint)
        if response.get('returncode')!=0 or response.get('failure'):raise AcquisitionError('github_request_failed')
        return json.loads(base64.b64decode(response['stdout'],validate=True))
    try:
        prefix='repos/'+original['repository']+'/git/'
        commit=get(prefix+'commits/'+original['commit'])
        tree_sha=commit.get('tree',{}).get('sha')
        if commit.get('sha')!=original['commit'] or not isinstance(tree_sha,str) or not re.fullmatch('[a-f0-9]{40}',tree_sha):
            raise AcquisitionError('commit_identity_mismatch')
        tree=get(prefix+'trees/'+tree_sha+'?recursive=1')
        if (tree.get('sha')!=tree_sha or tree.get('truncated') is not False
                or not isinstance(tree.get('tree'),list) or not 1<=len(tree['tree'])<=2000):
            raise AcquisitionError('incomplete_source_tree')
        from .author_resource_inventory_worker import classify_entries
        entries=classify_entries(tree['tree'])
        licenses=[row for row in entries if row['status']=='supported_blob'
            and PurePosixPath(row['path']).name.lower() in _LICENSE_NAMES]
        if len(licenses)>MAX_LICENSE_FILES:raise AcquisitionError('license_file_budget_exceeded')
        if any(row['size']>MAX_LICENSE_BYTES for row in licenses) or sum(row['size'] for row in licenses)>MAX_LICENSE_TOTAL:
            raise AcquisitionError('license_byte_budget_exceeded')
        files=[]
        for row in licenses:
            blob=get(prefix+'blobs/'+row['sha'])
            if blob.get('sha')!=row['sha'] or blob.get('encoding')!='base64' or blob.get('size')!=row['size']:
                raise AcquisitionError('license_blob_identity_mismatch')
            raw=base64.b64decode(''.join(blob.get('content','').split()),validate=True)
            if (len(raw)!=row['size'] or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=row['sha']):
                raise AcquisitionError('license_blob_integrity_mismatch')
            contents=raw.decode('utf-8')
            if '\0' in contents:raise AcquisitionError('license_text_encoding_invalid')
            digest=sha256(raw);dest=folder/'licenses'/(digest+'.txt')
            if not dest.exists():
                with dest.open('xb') as handle:
                    os.fchmod(handle.fileno(),0o400);handle.write(raw);handle.flush();os.fsync(handle.fileno())
            files.append(dict(path=safe_path(row['path']),size=len(raw),sha256=digest,blob_sha1=row['sha'],text=contents))
        # The compatibility receipt describes an intentionally incomplete source
        # acquisition, not a successful repository download or source approval.
        receipt=dict(schema_version=1,id=original['id'],repository=original['repository'],commit=original['commit'],
            location='hpc',remote_directory=str(folder),state='partial',request_sha256=sha256(canonical(original)),
            bundle_sha256=original['bundle_sha256'],files=[],failures=[dict(reason='metadata_only_no_author_source_requested')],
            inventory_sha256=sha256(canonical([])),
            source_complete=False,scientific_validation=False,execution_authorized=False,author_identity_verified=False,
            tree_sha1=tree_sha,source_bytes=0,request_count=reader.calls,received_bytes=reader.received,
            purpose=PURPOSE,bootstrap_request_sha256=sha256(canonical(request)))
        _once(folder/'result.json',receipt)
        tree_request=dict(version=1,acquisition_request=deepcopy(original),
            receipt_sha256=sha256(canonical(receipt)),tree_sha1=tree_sha)
        tree_report=inspect_cached_tree(tree_request)
        report.update(state='metadata_ready',code='',acquisition_receipt=receipt,tree_request=tree_request,
            tree_report=tree_report,license_evidence=dict(status='text_found_scope_unverified' if files else 'not_found',
                files=files,use_approved=False,read_approved=False,redistribution_approved=False,execution_approved=False))
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        report.update(state='failed',code=str(error) if isinstance(error,AcquisitionError) else 'metadata_validation_failed')
    report.update(request_count=reader.calls,received_bytes=reader.received)
    if len(canonical(report))>MAX_REPORT:
        report={**_unknown(request),'state':'failed','code':'metadata_report_budget_exceeded',
            'request_count':reader.calls,'received_bytes':reader.received}
    _once(outcome,report)
    return report


def bootstrap_tool_bundle():
    from .remote_resources import tool_bundle
    original=tool_bundle();stream=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source,zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as target:
        files={name:source.read(name) for name in source.namelist()}
        files['auto_lammps/author_resource_bootstrap.py']=Path(__file__).read_bytes()
        # read_regular is used only to read a previously persisted result. Keep
        # its existing vetted implementation and transitive imports in bundle.
        files['auto_lammps/runtime_launcher.py']=Path(__file__).with_name('runtime_launcher.py').read_bytes()
        for name,raw in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100400<<16;target.writestr(info,raw)
    data=stream.getvalue()
    if len(data)>1024**2:raise AcquisitionError('author_bootstrap_bundle_budget_exceeded')
    return data


def validate_bootstrap_report(request,report):
    from .author_resource_inventory_worker import _receipt
    from .remote_resources import RemoteResources
    _validate_bootstrap_request(request);expected=_unknown(request)
    fixed=('version','purpose','scope','B_input','execution_authorized','physical_evaluation_performed',
        'scientific_validation','source_complete','id','repository','commit','location','request_sha256',
        'binding_sha256','acquisition_request')
    base=set(expected)
    if (not isinstance(report,dict) or set(report) not in (base,base|{'request_count','received_bytes'})
            or len(canonical(report))>MAX_REPORT
            or any(type(report.get(key)) is not type(expected[key]) or report.get(key)!=expected[key] for key in fixed)
            or report.get('state') not in {'metadata_ready','failed','unknown'}):
        raise AcquisitionError('author_bootstrap_receipt_mismatch')
    if 'request_count' in report and (type(report['request_count']) is not int
            or not 0<=report['request_count']<=2+MAX_LICENSE_FILES
            or type(report['received_bytes']) is not int or not 0<=report['received_bytes']<=128*1024**2):
        raise AcquisitionError('author_bootstrap_transfer_budget_invalid')
    license=report.get('license_evidence')
    if (not isinstance(license,dict) or set(license)!={'status','files','use_approved','read_approved',
            'redistribution_approved','execution_approved'}
            or any(license.get(key) is not False for key in ('use_approved','read_approved',
                'redistribution_approved','execution_approved'))):
        raise AcquisitionError('author_bootstrap_cannot_approve_license')
    if report['state']!='metadata_ready':
        if (report.get('acquisition_receipt') is not None or report.get('tree_request') is not None
                or report.get('tree_report') is not None or license['files'] or license['status']!='unavailable'):
            raise AcquisitionError('author_bootstrap_unverified_source_claim')
        return deepcopy(report)
    receipt=report['acquisition_receipt'];tree_request=report['tree_request'];tree_report=report['tree_report']
    if ('request_count' not in report or tree_request.get('acquisition_request')!=request['acquisition_request']
            or receipt.get('purpose')!=PURPOSE or receipt.get('bootstrap_request_sha256')!=sha256(canonical(request))
            or receipt.get('files')!=[] or receipt.get('source_bytes')!=0 or receipt.get('source_complete') is not False
            or any(report[key]!=receipt.get(key) for key in ('request_count','received_bytes'))):
        raise AcquisitionError('author_bootstrap_is_not_source_acquisition')
    _receipt(receipt,tree_request);RemoteResources.validate_tree_report(tree_request,tree_report)
    rows=license['files']
    if (not isinstance(rows,list) or len(rows)>MAX_LICENSE_FILES
            or any(not isinstance(r,dict) or set(r)!={'path','size','sha256','blob_sha1','text'}
                   or not isinstance(r['path'],str) or type(r['size']) is not int for r in rows)
            or len({r['path'] for r in rows})!=len(rows)
            or sum(r['size'] for r in rows)>MAX_LICENSE_TOTAL
            or license['status']!=('text_found_scope_unverified' if rows else 'not_found')):
        raise AcquisitionError('author_bootstrap_license_evidence_invalid')
    by_path={row['path']:row for row in tree_report['entries']}
    for row in rows:
        if (set(row)!={'path','size','sha256','blob_sha1','text'} or not isinstance(row['text'],str)
                or '\0' in row['text'] or type(row['size']) is not int or not 0<=row['size']<=MAX_LICENSE_BYTES):
            raise AcquisitionError('author_bootstrap_license_evidence_invalid')
        member=by_path.get(safe_path(row['path'])) or {};raw=row['text'].encode('utf-8')
        if (PurePosixPath(row['path']).name.lower() not in _LICENSE_NAMES
                or member.get('status')!='supported_blob' or member.get('sha')!=row['blob_sha1']
                or member.get('size')!=row['size'] or len(raw)!=row['size'] or sha256(raw)!=row['sha256']
                or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=row['blob_sha1']):
            raise AcquisitionError('author_bootstrap_license_evidence_invalid')
    return deepcopy(report)


class AuthorResourceBootstrap:
    def __init__(self,tasks,papers,scopes,discovery_library,hpc,*,capture=None):
        from .manifest import private_directory
        from .slurm_read import _capture
        from .tasks import TaskError
        if papers.tasks.path!=tasks.path or scopes.tasks.path!=tasks.path or hpc.tasks.path!=tasks.path:
            raise TaskError('初始作者资料必须共用本项目的任务、范围和保存的HPC连接。')
        self.tasks,self.papers,self.scopes,self.library,self.hpc=tasks,papers,scopes,discovery_library,hpc
        self.capture=capture or _capture
        self.audit=private_directory(tasks.path.parent/'author-resource-bootstrap')
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_bootstrap_intents ('
                'id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),document TEXT NOT NULL,'
                'sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS author_bootstrap_events ('
                'id TEXT NOT NULL REFERENCES author_bootstrap_intents(id),sequence INTEGER NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            for table in ('author_bootstrap_intents','author_bootstrap_events'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} '
                        'BEGIN SELECT RAISE(ABORT,"immutable author bootstrap history"); END')

    def _context(self,identifier):
        from .hpc_transport import SavedHPCTransport
        from .reference_resource_association import validate_catalog_association
        from .tasks import TaskError,task_id
        task=self.tasks.get(task_id(identifier))
        if task['mode']!='reproduction' or self.tasks.paused(identifier) or any(self.tasks.lifecycle(identifier)[key]
                for key in ('user_finished','deleted')):
            raise TaskError('请使用未暂停或结束的论文复现任务核对初始资料。')
        with self.tasks.transaction() as db:
            ids=[row[0] for row in db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?',(identifier,))]
        if len(ids)!=1:raise TaskError('初始资料需要本任务唯一关联论文。')
        paper=self.papers.get(ids[0])
        association=validate_catalog_association(paper,paper.get('resource_catalog_association'),self.library)
        scope=self.scopes.get(identifier)
        if not scope or scope.get('state')!='recorded' or scope['scope']['provenance']['paper_id']!=paper['id']:
            raise TaskError('请先保存并核对来自P的目标和共享依赖范围。')
        saved=self.hpc.status()
        if not saved['configured'] or saved['active_revision'] is None:
            raise TaskError('请先选择本项目已保存的HPC连接。')
        revision=saved['active_revision'];alias='saved-hpc-'+str(revision)
        transport=SavedHPCTransport(self.hpc,revision,alias)
        directory=str(PurePosixPath(saved['profile']['work_directory'])/'Auto-LAMMPS-Resources'/'author-bootstrap')
        value=dict(version=1,**FLAGS,task_id=identifier,paper_id=paper['id'],
            paper={key:paper[key] for key in ('title','doi')},reference_scope=deepcopy(scope),
            catalog_association=association,connection_revision=revision,host_alias=alias,
            transport_identity_sha256=sha256(canonical(transport.identity)),remote_directory=directory)
        return value,transport

    def _stored(self,identifier,request_id=None):
        from .tasks import TaskError,task_id
        self.tasks.get(task_id(identifier))
        with self.tasks.transaction() as db:
            rows=db.execute('SELECT * FROM author_bootstrap_intents WHERE '+('id=?' if request_id else 'task_id=?')+
                ' ORDER BY rowid',(task_id(request_id) if request_id else identifier,)).fetchall()
            result=[]
            for row in rows:
                context=json.loads(row['document'])
                _validate_bootstrap_request(context['request'])
                if (row['task_id']!=identifier or context['binding']['task_id']!=identifier
                        or context['request']['acquisition_request']['id']!=row['id']
                        or context['request']['binding_sha256']!=sha256(canonical(context['binding']))
                        or sha256(canonical(context))!=row['sha256']):
                    raise TaskError('初始作者资料请求属于其他任务或摘要不符。')
                events=[]
                for event in db.execute('SELECT * FROM author_bootstrap_events WHERE id=? ORDER BY sequence',(row['id'],)):
                    value=json.loads(event['document'])
                    if sha256(canonical(value))!=event['sha256']:raise TaskError('初始作者资料追加回执摘要不符。')
                    if value.get('report') is not None:validate_bootstrap_report(context['request'],value['report'])
                    events.append(value)
                result.append(dict(request_id=row['id'],context=context,events=events))
        return result[0] if request_id and result else None if request_id else result

    def _event(self,request_id,report=None,transport=None):
        value=dict(at=datetime.now(timezone.utc).isoformat(),report=deepcopy(report),transport=transport)
        with self.tasks.transaction() as db:
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM author_bootstrap_events WHERE id=?',(request_id,)).fetchone()[0]
            db.execute('INSERT INTO author_bootstrap_events VALUES (?,?,?,?)',
                (request_id,sequence,canonical(value).decode(),sha256(canonical(value))))

    @staticmethod
    def _summary(row):
        report=(row['events'][-1].get('report') if row['events'] else None) or _unknown(row['context']['request'])
        return dict(request_id=row['request_id'],state=report['state'],code=report['code'],
            repository=report['repository'],commit=report['commit'],purpose=PURPOSE,
            files=[],source_complete=False,tree_entries=len((report.get('tree_report') or {}).get('entries',[])),
            license_evidence=deepcopy(report['license_evidence']),recoverable=report['state']=='unknown',
            reading_approved=False,use_approved=False,execution_authorized=False)

    def status(self,identifier):
        from .runtime_launcher import ExecutionDenied
        rows=self._stored(identifier);enabled=True;reason=''
        try:self._context(identifier)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            enabled=False;reason=str(error) if isinstance(error,ValueError) else '固定目录、P范围或保存的HPC连接尚未核验。'
        history=[self._summary(row) for row in rows]
        return dict(version=1,**FLAGS,task_id=identifier,enabled=enabled,disabled_reason=reason,
            history=history,latest=history[-1] if history else None,calls_model=False,submits_HPC=False,
            requires_author_workspace=False,read_approval_required=True)

    def inspect(self,identifier,request_id):
        from .tasks import TaskError,task_id
        from .reference_targets import ReferenceTargetAnalysis
        task_id(request_id)
        # Reuse the existing cross-process task lock without creating a model
        # service, changing proposal rounds or claiming physics submissions.
        lock=type('BootstrapLock',(),dict(locks=self.audit))()
        with ReferenceTargetAnalysis._lease(lock,identifier) as acquired:
            if not acquired:raise TaskError('本任务正在核对初始资料，未重复请求。')
            current,transport=self._context(identifier);saved=self._stored(identifier,request_id)
            if saved:
                if saved['context']['binding']!=current:raise TaskError('原请求绑定的P范围、来源或HPC已改变；未迁移。')
                return self._summary(saved)
            rows=self._stored(identifier)
            if any(self._summary(row)['recoverable'] for row in rows):
                raise TaskError('初始作者资料有未知请求，请先核对原请求；未换编号重发。')
            for row in reversed(rows):
                if row['context']['binding']==current and self._summary(row)['state']=='metadata_ready':return self._summary(row)
            if len(rows)>=32:raise TaskError('初始作者资料请求容量已用完；旧记录保留。')
            bundle=bootstrap_tool_bundle();association=current['catalog_association']
            original=dict(id=request_id,repository=association['repository'],commit=association['commit'],
                directory=current['remote_directory'],bundle_sha256=sha256(bundle),storage_bytes=512*1024**2)
            request=dict(version=VERSION,purpose=PURPOSE,acquisition_request=original,binding_sha256=sha256(canonical(current)))
            context=dict(binding=current,request=request)
            with self.tasks.transaction() as db:
                ReferenceTargetAnalysis._task(db,identifier,active=True)
                db.execute('INSERT INTO author_bootstrap_intents VALUES (?,?,?,?,?)',
                    (request_id,identifier,canonical(context).decode(),sha256(canonical(context)),datetime.now(timezone.utc).isoformat()))
            self._event(request_id)
            return self._observe(identifier,request_id,context,transport,'inspect')

    def reconcile(self,identifier,request_id):
        from .tasks import TaskError
        row=self._stored(identifier,request_id)
        if row is None:raise TaskError('没有属于本任务的原初始资料请求。')
        if self._summary(row)['state']!='unknown':return self._summary(row)
        if len(row['events'])>=32:raise TaskError('原初始资料核对次数已达上限；保留未知记录，未重新取件。')
        current,transport=self._context(identifier)
        if current!=row['context']['binding']:raise TaskError('原初始资料绑定已变化；不能迁移或另建获取。')
        return self._observe(identifier,request_id,row['context'],transport,'reconcile')

    def _observe(self,identifier,request_id,context,transport,action):
        from .remote_resources import BOOTSTRAP
        # The wrapper installs only this reviewed static helper. The action is
        # a server-chosen verb; no repository code or caller shell is executed.
        script=BOOTSTRAP.replace("request=envelope['request'];data=", "request=envelope['request'];original=request['acquisition_request'];data=")
        script=script.replace("request['bundle_sha256']", "original['bundle_sha256']").replace("request['directory']", "original['directory']")
        script=script.replace("if digest!=original['bundle_sha256']:","if digest!=envelope['bundle_sha256']:")
        script=script.replace('from auto_lammps.reference_resource_worker import acquire',
            'from auto_lammps.author_resource_bootstrap import bootstrap_pinned_tree')
        script=script.replace('acquire(request)',"bootstrap_pinned_tree(request,action=envelope['action'])")
        bundle=bootstrap_tool_bundle();request=context['request'];report=_unknown(request)
        # Inspect uses its exact original helper. A current read-only helper may
        # reconcile that immutable intent/result without issuing another GET.
        if action=='inspect' and sha256(bundle)!=request['acquisition_request']['bundle_sha256']:
            from .tasks import TaskError
            raise TaskError('原初始资料工具版本已改变；未重发或迁移原请求。')
        data=canonical(dict(request=request,bundle=base64.b64encode(bundle).decode(),
            bundle_sha256=sha256(bundle),action=action))
        command=transport.command(context['binding']['host_alias'],['python3','-c',script])
        try:result=self.capture(command,timeout=240,max_bytes=MAX_REPORT,
            input_chunks=[data[i:i+65536] for i in range(0,len(data),65536)])
        except OSError as error:result=dict(returncode=None,failure=type(error).__name__,stdout='',stderr='')
        summary={**{key:result.get(key) for key in ('returncode','failure')},
            'reader_bundle_sha256':sha256(bundle),'action':action}
        for key in ('stdout','stderr'):
            raw=str(result.get(key) or '').encode();summary[key+'_sha256']=sha256(raw);summary[key+'_bytes']=len(raw)
        if result.get('returncode')==0 and not result.get('failure'):
            try:report=validate_bootstrap_report(request,json.loads(base64.b64decode(result['stdout'],validate=True)))
            except (ValueError,KeyError,TypeError):report['code']='invalid_bootstrap_receipt'
        else:report['code']='bootstrap_transport_unverified'
        self._event(request_id,report,summary)
        return self._summary(self._stored(identifier,request_id))

    def metadata_for_selection(self,identifier,request_id=None):
        from .tasks import TaskError
        current,_=self._context(identifier);rows=self._stored(identifier)
        row=(next((row for row in rows if row['request_id']==request_id),None) if request_id else
             next((row for row in reversed(rows) if row['context']['binding']==current
                   and self._summary(row)['state']=='metadata_ready'),None))
        if row is None or row['context']['binding']!=current or self._summary(row)['state']!='metadata_ready':
            raise TaskError('当前P范围、固定树与许可原件尚未形成匹配的完整初始回执。')
        request=row['context']['request'];report=validate_bootstrap_report(request,row['events'][-1]['report'])
        return dict(version=1,**FLAGS,task_id=identifier,paper_id=current['paper_id'],paper=deepcopy(current['paper']),
            reference_scope=deepcopy(current['reference_scope']),catalog_association=deepcopy(current['catalog_association']),
            bootstrap_request_id=row['request_id'],bootstrap_request=deepcopy(request),bootstrap_receipt=report,
            bootstrap_receipt_sha256=sha256(canonical(report)),acquisition_request=deepcopy(report['acquisition_request']),
            acquisition_receipt=deepcopy(report['acquisition_receipt']),tree_request=deepcopy(report['tree_request']),
            tree_report=deepcopy(report['tree_report']),license_evidence=deepcopy(report['license_evidence']),
            existing_files=[],binding=dict(connection_revision=current['connection_revision'],host_alias=current['host_alias'],
                transport_identity_sha256=current['transport_identity_sha256'],repository=report['repository'],commit=report['commit']),
            read_approval_required=True)
