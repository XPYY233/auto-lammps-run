"""Current byte identities of outputs from an already accounted reference run.

The existing ReferenceOutputs service fixes the run/root. Browser callers choose
only task/request identifiers. This is metadata inspection, never scientific
validation, restart selection, a new evaluation, or repository-source discovery.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import re

from .hpc_transport import SavedHPCTransport
from .manifest import canonical, private_directory, sha256
from .runtime_launcher import absolute, hash_value, relative
from .slurm_read import _capture, _write_new
from .tasks import TaskError, task_id


FLAGS = dict(scope='reference_only', B_input=False, execution_authorized=False,
    physical_evaluation_performed=False, scientific_validation=False, calls_model=False, submits_HPC=False)


def validate_reference_inputs(rows, identifier):
    if not isinstance(rows, list) or len(rows) > 4: raise TaskError('旧参考输入回执须独立、有界。')
    seen = set()
    for row in rows:
        fields = {'kind','task_id','request_id','job_id','manifest_sha256','receipt_sha256','binding_sha256','root','files'}
        if (not isinstance(row,dict) or set(row) != fields or row['kind'] != 'accounted_reference_output'
                or row['task_id'] != identifier or not isinstance(row['job_id'],str)
                or not re.fullmatch('[1-9][0-9]{0,19}',row['job_id'])):
            raise TaskError('旧输入必须保留独立记账作者作业身份，不能伪装成作者仓库原件。')
        task_id(row['request_id']); absolute(row['root'])
        for key in ('manifest_sha256','receipt_sha256','binding_sha256'): hash_value(row[key])
        if row['binding_sha256'] in seen: raise TaskError('旧参考输入回执不能重复。')
        seen.add(row['binding_sha256']); paths=set()
        if not isinstance(row['files'],list) or not 1 <= len(row['files']) <= 512:
            raise TaskError('旧参考输入需要实际核验的完整有界文件清单。')
        for item in row['files']:
            if (not isinstance(item,dict) or set(item) != {'path','size','sha256'} or item['path'] in paths
                    or type(item['size']) is not int or item['size'] < 0):
                raise TaskError('旧参考输入文件身份无效。')
            relative(item['path']);hash_value(item['sha256']);paths.add(item['path'])
    return deepcopy(rows)


def _remote_inputs(request, *, action='inspect', _test_host=False):
    import hashlib,json,os,re,stat,sys
    from pathlib import Path,PurePosixPath
    def encoded(value):return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    digest=lambda value:hashlib.sha256(value).hexdigest()
    def directory(path):
        if not isinstance(path,str) or not path.startswith('/') or str(PurePosixPath(path)) != path or '..' in PurePosixPath(path).parts:
            raise ValueError('invalid_reference_root')
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            parts=PurePosixPath(path).parts[1:]
            for index,part in enumerate(parts):
                # HPC ancestors may grant search without directory-list access.
                # Only the final private root needs a readable directory fd.
                access=os.O_RDONLY if index==len(parts)-1 else getattr(os,'O_PATH',os.O_RDONLY)
                child=os.open(part,access|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
            info=os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise ValueError('private_reference_root_required')
            return fd
        except Exception:os.close(fd);raise
    if not _test_host and (sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION')):raise ValueError('saved_HPC_SSH_required')
    fields={'version','purpose','id','task_id','request_id','job_id','manifest_sha256','root','binding_sha256','audit_root','worker_sha256','submission_source'}
    if (set(request) != fields or request['version'] != 1 or request['purpose'] != 'accounted_reference_input_metadata'
            or action not in {'inspect','reconcile'}):raise ValueError('invalid_reference_input_request')
    for key in ('id','task_id','request_id'):
        if not re.fullmatch('[a-f0-9]{32}',request[key]):raise ValueError('invalid_reference_identity')
    parent=Path(request['audit_root']);root=Path(request['root']);folder=parent/request['id']
    fixed=dict(version=1,purpose=request['purpose'],id=request['id'],task_id=request['task_id'],
        request_sha256=digest(encoded(request)),binding_sha256=request['binding_sha256'],worker_sha256=request['worker_sha256'],**{
            'scope':'reference_only','B_input':False,'execution_authorized':False,'physical_evaluation_performed':False,'scientific_validation':False})
    if folder.exists():
        fd=directory(str(folder));os.close(fd)
        receipt=folder/'receipt.json'
        if not receipt.exists():return dict(fixed,state='unknown',files=[],omitted=[],submission_environment=None)
        fd=os.open(receipt,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>1024**2 or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
                raise ValueError('invalid_reference_input_receipt')
            saved=json.loads(stream.read(1024**2+1))
        if any(saved.get(key)!=value for key,value in fixed.items()):raise ValueError('reference_input_receipt_changed')
        return saved
    if action=='reconcile':return dict(fixed,state='not_started',files=[],omitted=[],submission_environment=None)
    # The audit namespace is server-derived from the saved HPC work directory.
    parent.mkdir(mode=0o700,parents=True,exist_ok=True);fd=directory(str(parent));os.close(fd)
    folder.mkdir(mode=0o700);root_fd=directory(str(root));files=[];omitted=[];entries=0
    suffixes={'.restart','.dump','.dat','.data','.txt','.csv','.xyz','.lammpstrj'}
    def visit(fd,prefix,depth):
        nonlocal entries
        if depth>12:raise ValueError('reference_inventory_depth_limit')
        for name in sorted(os.listdir(fd)):
            entries+=1
            if entries>4096:raise ValueError('reference_inventory_entry_limit')
            if not re.fullmatch('[A-Za-z0-9_.-]+',name) or name.startswith('.'):continue
            path=prefix+name;info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:visit(child,path+'/',depth+1)
                finally:os.close(child)
            elif stat.S_ISREG(info.st_mode) and Path(name).suffix.lower() in suffixes:
                if info.st_size>256*1024**2:
                    omitted.append(dict(path=path,reason='metadata_hash_byte_limit'));continue
                if info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o022:raise ValueError('unsafe_reference_input_file')
                if len(files)>=512:raise ValueError('reference_inventory_file_limit')
                source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                try:
                    before=os.fstat(source);checksum=hashlib.sha256()
                    while True:
                        block=os.read(source,65536)
                        if not block:break
                        checksum.update(block)
                    after=os.fstat(source)
                    if any(getattr(before,key)!=getattr(after,key) for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode')):
                        raise ValueError('reference_input_changed_while_hashing')
                    files.append(dict(path=path,size=after.st_size,sha256=checksum.hexdigest()))
                finally:os.close(source)
    try:visit(root_fd,'',0)
    finally:os.close(root_fd)
    submission=None;source=request['submission_source']
    if source is not None:
        def pinned_file(path,expected=None,limit=64*1024**2):
            absolute=Path(path)
            if not absolute.is_absolute() or '..' in absolute.parts:raise ValueError('invalid_scheduler_source')
            fd=os.open(absolute,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode&0o022 or info.st_size>limit:raise ValueError('unsafe_scheduler_source')
                data=stream.read(limit+1)
            if expected is not None and digest(data)!=expected:raise ValueError('scheduler_source_changed')
            return data
        pinned_file(source['helper_path'],source['helper_sha256'],1000000)
        config=json.loads(pinned_file(source['config_path'],limit=16384))
        if config['runtime_path']!=source['runtime_path'] or config['runtime_sha256']!=source['runtime_sha256']:
            raise ValueError('scheduler_runtime_identity_changed')
        pinned_file(config['sbatch_path'],config['sbatch_sha256'])
        submission=dict(sbatch_path=config['sbatch_path'],sbatch_sha256=config['sbatch_sha256'],
                        original_config_sha256=digest(encoded(config)))
    report=dict(fixed,state='metadata_ready' if files else 'incomplete',files=files,omitted=omitted,submission_environment=submission)
    fd=os.open(folder/'receipt.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400)
    with os.fdopen(fd,'wb') as stream:stream.write(encoded(report));stream.flush();os.fsync(stream.fileno())
    return report


REMOTE_CODE=inspect.getsource(_remote_inputs)+'''
import base64,json,sys
value=json.loads(sys.stdin.buffer.read(1048577))
if set(value)!={'request','action'}:raise ValueError('invalid_reference_input_envelope')
print(base64.b64encode(json.dumps(_remote_inputs(value['request'],action=value['action']),ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).decode())
'''


class AuthorReferenceInputs:
    def __init__(self,workspace,reference_outputs,*,audit_directory,controller=None,capture=_capture):
        self.workspace,self.tasks,self.outputs=workspace,workspace.tasks,reference_outputs
        self.audit=private_directory(audit_directory);self.capture=capture
        self.controller=controller
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_reference_input_intents (id TEXT PRIMARY KEY,task_id TEXT NOT NULL,document TEXT NOT NULL,sha256 TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS author_reference_input_events (id TEXT NOT NULL,sequence INTEGER NOT NULL,document TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            for table in ('author_reference_input_intents','author_reference_input_events'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,"immutable accounted reference inputs"); END')

    def _context(self,identifier,request):
        self.tasks.get(task_id(identifier));task_id(request)
        if self.outputs is None:raise TaskError('现有作者原始输出服务尚未连接，不能指定任意旧输出路径。')
        bindings=[key for key in self.outputs.bindings if key[0]==identifier]
        if len(bindings)!=1:raise TaskError('需要唯一既有作者输出绑定；不能猜测作业或目录。')
        binding,_=self.outputs._binding(*bindings[0]);row=self.outputs._run(binding)
        if not row['accounted'] or row['state'] not in {'completed','failed','timeout','cancelled'}:
            raise TaskError('旧参考输入必须来自已记账且已结束的原作业。')
        transport=self.outputs.transport
        if not isinstance(transport,SavedHPCTransport) or transport._identity()!=transport.identity:
            raise TaskError('旧参考输入必须绑定保存的项目连接。')
        original,paper,receipt,scope=self.workspace._metadata(identifier)
        author_transport=original['reader'].transport
        strip=lambda value:{key:item for key,item in value.items() if key!='host_alias'}
        if strip(author_transport.identity)!=strip(transport.identity):raise TaskError('旧参考输出与作者源码连接不一致。')
        proof=dict(task_id=identifier,request_id=binding['request_id'],job_id=row['job_id'],manifest_sha256=row['manifest_sha256'],
            root=binding['root'],inventory_receipt_sha256=binding['inventory_receipt_sha256'],
            transport_identity_sha256=sha256(canonical(transport.identity)),ledger_state=row['state'],accounted=True,
            charge_core_seconds=row['charge_core_seconds'])
        remote=dict(version=1,purpose='accounted_reference_input_metadata',id=request,task_id=identifier,
            request_id=binding['request_id'],job_id=row['job_id'],manifest_sha256=row['manifest_sha256'],root=binding['root'],
            binding_sha256=sha256(canonical(proof)),worker_sha256=sha256(REMOTE_CODE.encode()),
            audit_root=str(Path(transport.identity['profile']['work_directory'])/'Auto-LAMMPS-Reference-Input-Audit'))
        submission=None
        if self.controller is not None:
            endpoint=self.controller.submission.scheduler.endpoint;runtime_endpoint=self.controller.following.analysis.collector.endpoint
            submission=dict(helper_path=endpoint.helper_path,helper_sha256=endpoint.helper_sha256,
                config_path=str(Path(endpoint.helper_path).parent/'submission.json'),
                runtime_path=runtime_endpoint.helper_path,runtime_sha256=runtime_endpoint.helper_sha256)
        remote['submission_source']=submission
        return dict(proof=proof,request=remote,host_alias=binding['host_alias']),transport

    def _stored(self,identifier,request=None):
        self.tasks.get(task_id(identifier))
        with self.tasks.transaction() as db:
            rows=db.execute('SELECT * FROM author_reference_input_intents WHERE '+('id=?' if request else 'task_id=?')+' ORDER BY rowid',(task_id(request) if request else identifier,)).fetchall()
            result=[]
            for row in rows:
                context=json.loads(row['document'])
                if row['task_id']!=identifier or sha256(canonical(context))!=row['sha256']:raise TaskError('旧参考输入身份或摘要不一致。')
                events=[]
                for event in db.execute('SELECT * FROM author_reference_input_events WHERE id=? ORDER BY sequence',(row['id'],)):
                    value=json.loads(event['document'])
                    if sha256(canonical(value))!=event['sha256']:raise TaskError('旧参考输入回执摘要不一致。')
                    events.append(value)
                result.append(dict(context=context,events=events))
        return result

    def _summary(self,row):
        report=row['events'][-1]['report'] if row['events'] else None;request=row['context']['request']
        receipt=None
        if report and report['state']=='metadata_ready':
            receipt=dict(kind='accounted_reference_output',task_id=request['task_id'],request_id=request['request_id'],
                job_id=request['job_id'],manifest_sha256=request['manifest_sha256'],root=request['root'],
                receipt_sha256=sha256(canonical(report)),binding_sha256=sha256(canonical(dict(proof=row['context']['proof'],report=report))),
                files=deepcopy(report['files']))
            validate_reference_inputs([receipt],request['task_id'])
        return dict(**FLAGS,task_id=request['task_id'],request_id=request['id'],state=report['state'] if report else 'unknown',
            report=deepcopy(report),input_receipt=receipt,events=deepcopy(row['events']))

    def get(self,identifier,request):
        rows=self._stored(identifier,request);return self._summary(rows[0]) if rows else None

    def status(self,identifier):
        history=[self._summary(row) for row in self._stored(identifier)]
        return dict(**FLAGS,task_id=identifier,history=history,latest=history[-1] if history else None,reads_remote=False)

    def planner_inputs(self,identifier):
        rows=self._stored(identifier)
        if not rows:return []
        row=rows[-1];current,_=self._context(identifier,row['context']['request']['id'])
        if not self._same_binding(current,row['context']):raise TaskError('原参考输入来源或记账身份已变化；不能沿用旧回执。')
        value=self._summary(row)
        if value['state']!='metadata_ready':raise TaskError('原参考输入元数据尚未核验，未知请求须核对原请求。')
        return [value['input_receipt']]

    def inspect(self,identifier,request):
        existing=self.get(identifier,request)
        if existing:
            if existing['state']!='not_started':return existing
            row=self._stored(identifier,request)[0]
            current,transport=self._context(identifier,request)
            if not self._same_binding(current,row['context']):raise TaskError('旧输入来源或保存连接已变化，未迁移原请求。')
            # Only a verified absent request can resume; unknown never resends.
            return self._observe(identifier,request,'inspect',transport)
        context,transport=self._context(identifier,request)
        if any(self._summary(row)['state']=='unknown' for row in self._stored(identifier)):
            raise TaskError('旧参考输入仍有未知核验，请核对原请求，不能换号重发。')
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO author_reference_input_intents VALUES(?,?,?,?)',(request,identifier,canonical(context).decode(),sha256(canonical(context))))
        return self._observe(identifier,request,'inspect',transport)

    def reconcile(self,identifier,request):
        rows=self._stored(identifier,request)
        if not rows:raise TaskError('请核对原参考输入请求。')
        row=rows[0]
        if self._summary(row)['state']!='unknown':return self._summary(row)
        current,transport=self._context(identifier,request)
        if not self._same_binding(current,row['context']):raise TaskError('旧输入来源或保存连接已变化，未迁移原请求。')
        return self._observe(identifier,request,'reconcile',transport)

    @staticmethod
    def _same_binding(current,stored):
        # A repaired metadata observer can recover the immutable original intent.
        # Source/run/connection bindings remain exact; observer bytes are recorded
        # separately on every event rather than rewriting the request's worker.
        value=deepcopy(current)
        value['request']['worker_sha256']=stored['request']['worker_sha256']
        return value==stored

    def _observe(self,identifier,request,action,transport):
        context=self._stored(identifier,request)[0]['context'];remote=context['request']
        command=transport.command(context['host_alias'],['python3','-I','-S','-c',REMOTE_CODE])
        try:result=self.capture(command,timeout=60,max_bytes=2*1024**2,input_chunks=[canonical(dict(request=remote,action=action))])
        except (OSError,ValueError) as error:
            result=dict(returncode=None,failure='reference_input_transport_unverified',stdout='',
                exception_type=type(error).__name__,exception_message=str(error))
        fixed=dict(version=1,purpose=remote['purpose'],id=request,task_id=identifier,request_sha256=sha256(canonical(remote)),
            binding_sha256=remote['binding_sha256'],worker_sha256=remote['worker_sha256'],**{key:FLAGS[key] for key in
                ('scope','B_input','execution_authorized','physical_evaluation_performed','scientific_validation')})
        report=None
        if result.get('returncode')==0 and not result.get('failure'):
            try:
                value=json.loads(base64.b64decode(base64.b64decode(result['stdout'],validate=True).strip(),validate=True))
                if (set(value)==set(fixed)|{'state','files','omitted','submission_environment'} and value['state'] in {'metadata_ready','incomplete','unknown','not_started'}
                        and all(value.get(key)==item and type(value.get(key)) is type(item) for key,item in fixed.items())
                        and isinstance(value['files'],list) and len(value['files'])<=512 and isinstance(value['omitted'],list)):
                    # Validate every received file before accepting a transport receipt.
                    synthetic=dict(kind='accounted_reference_output',task_id=identifier,request_id=remote['request_id'],job_id=remote['job_id'],
                        manifest_sha256=remote['manifest_sha256'],receipt_sha256='0'*64,binding_sha256='0'*64,root=remote['root'],files=value['files'])
                    if value['state']=='metadata_ready':validate_reference_inputs([synthetic],identifier)
                    elif value['files']:raise ValueError('unexpected_partial_reference_files')
                    scheduler=value['submission_environment']
                    if scheduler is not None:
                        if set(scheduler)!={'sbatch_path','sbatch_sha256','original_config_sha256'} or remote['submission_source'] is None:
                            raise ValueError('unexpected_scheduler_metadata')
                        absolute(scheduler['sbatch_path']);hash_value(scheduler['sbatch_sha256']);hash_value(scheduler['original_config_sha256'])
                    elif remote['submission_source'] is not None and value['state']=='metadata_ready':
                        raise ValueError('missing_scheduler_metadata')
                    report=value
            except (ValueError,TypeError,KeyError):pass
        if report is None:report=dict(fixed,state='unknown',files=[],omitted=[],submission_environment=None)
        event=dict(at=datetime.now(timezone.utc).isoformat(),action=action,report=report,
                   transport={key:result.get(key) for key in ('returncode','failure')},
                   request_worker_sha256=remote['worker_sha256'],observer_worker_sha256=sha256(REMOTE_CODE.encode()),
                   transport_capture_sha256=sha256(canonical(result)))
        with self.tasks.transaction() as db:
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM author_reference_input_events WHERE id=?',(request,)).fetchone()[0]
            db.execute('INSERT INTO author_reference_input_events VALUES(?,?,?,?)',(request,sequence,canonical(event).decode(),sha256(canonical(event))))
        _write_new(self.audit/(request+'-'+str(sequence)+'.json'),event)
        # Keep private stderr for real failures; browser summaries stay sanitized.
        _write_new(self.audit/(request+'-'+str(sequence)+'-transport.json'),result)
        return self.get(identifier,request)
