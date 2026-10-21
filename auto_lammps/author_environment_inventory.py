"""Explicit, reference-side metadata checks of an existing saved HPC environment.

No engine, author script or scientific Python package is executed. Paths come
only from a trusted server provider; ordinary callers supply task/request IDs.
Receipts identify installed metadata, never approve an interpreter, entrypoint,
child-process policy, scientific applicability or a physical calculation.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path, PurePosixPath
import re

from .hpc_transport import SavedHPCTransport
from .manifest import canonical, private_directory, sha256
from . import runtime_launcher as runtime
from .slurm_read import _capture, _write_new
from .tasks import TaskError, task_id, text


VERSION = 1
PURPOSE = 'existing_author_environment_metadata_only'
MAX_REPORT = 1024 * 1024
FLAGS = dict(scope='reference_only', B_input=False, execution_authorized=False,
    physical_evaluation_performed=False, scientific_validation=False,
    execution_ready=False, child_process_policy_verified=False, author_entrypoints_reviewed=False)
_PROVIDER = {'profile_path', 'profile_sha256', 'connection_revision', 'host_alias',
             'python_candidates', 'required_distributions'}
_POLICY_MISSING = ['作者驱动的同一计算分配子进程策略尚须独立审核。',
                   '作者原始入口与执行部署尚须独立登记和批准。']


def _remote_inventory(request, *, action='inspect', _test_capture=None):
    """Standalone stdlib worker; the optional capture is for synthetic tests only."""
    import hashlib
    import json
    import os
    from pathlib import Path, PurePosixPath
    import re
    import stat
    import subprocess
    import sys
    from datetime import datetime, timezone

    def encoded(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    digest=lambda value:hashlib.sha256(value).hexdigest()
    def absolute(value):
        if (not isinstance(value,str) or not value.startswith('/') or value=='/' or len(value)>2048
                or str(PurePosixPath(value))!=value or '..' in PurePosixPath(value).parts
                or any(ord(c)<32 or ord(c)==127 for c in value)):
            raise ValueError('invalid_metadata_path')
        return value
    def checksum(value):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
            raise ValueError('invalid_metadata_digest')
    fields={'version','purpose','id','binding_sha256','remote_directory','native_files',
            'python_candidates','required_distributions','worker_sha256'}
    if (not isinstance(request,dict) or set(request)!=fields or request['version']!=1
            or request['purpose']!='existing_author_environment_metadata_only'
            or not isinstance(request['id'],str) or not re.fullmatch('[a-f0-9]{32}',request['id'])
            or action not in {'inspect','reconcile'}):
        raise ValueError('invalid_environment_request')
    for key in ('binding_sha256','worker_sha256'):checksum(request[key])
    absolute(request['remote_directory'])
    if (not isinstance(request['native_files'],dict) or not 4<=len(request['native_files'])<=128
            or not isinstance(request['python_candidates'],list) or not 1<=len(request['python_candidates'])<=8
            or not isinstance(request['required_distributions'],list) or len(request['required_distributions'])>32):
        raise ValueError('environment_metadata_capacity')
    for path,expected in request['native_files'].items():absolute(path);checksum(expected)
    identifiers=set()
    for candidate in request['python_candidates']:
        if (not isinstance(candidate,dict) or set(candidate)!={'id','label','path'}
                or not isinstance(candidate['id'],str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',candidate['id'])
                or candidate['id'] in identifiers or not isinstance(candidate['label'],str)
                or not 1<=len(candidate['label'])<=500):raise ValueError('invalid_python_candidate')
        identifiers.add(candidate['id']);absolute(candidate['path'])
    if (len(set(request['required_distributions']))!=len(request['required_distributions'])
            or any(not isinstance(name,str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,99}',name)
                   for name in request['required_distributions'])):
        raise ValueError('invalid_distribution_names')
    if _test_capture is None and (sys.platform!='linux' or not os.environ.get('SSH_CONNECTION')):
        raise ValueError('HPC_metadata_session_required')
    flags=dict(scope='reference_only',B_input=False,execution_authorized=False,
        physical_evaluation_performed=False,scientific_validation=False,execution_ready=False,
        child_process_policy_verified=False,author_entrypoints_reviewed=False)
    report=dict(version=1,purpose=request['purpose'],**flags,id=request['id'],location='hpc',
        request_sha256=digest(encoded(request)),binding_sha256=request['binding_sha256'],
        worker_sha256=request['worker_sha256'],state='unknown',native_files=[],python_candidates=[],missing=[])
    root=Path(request['remote_directory']);folder=root/request['id']
    def read(path,limit):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>limit
                    or before.st_mode&0o077):raise ValueError('unsafe_environment_receipt')
            with os.fdopen(fd,'rb',closefd=False) as stream:data=stream.read(limit+1)
            if len(data)!=before.st_size:raise ValueError('changed_environment_receipt')
            return data
        finally:os.close(fd)
    def once(path,value):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
    if action=='reconcile' and not folder.exists():
        report['state']='not_started';return report
    if folder.exists():
        if folder.is_symlink() or json.loads(read(folder/'intent.json',1024**2))!=request:
            raise ValueError('environment_request_identity_conflict')
        return json.loads(read(folder/'receipt.json',1024**2)) if (folder/'receipt.json').exists() else report
    if action=='reconcile':return report
    # Only this purpose-specific audit directory is writable; never a source,
    # request, installation or package directory supplied by a model/browser.
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode&0o077:
        raise ValueError('unsafe_environment_audit_directory')
    try:folder.mkdir(mode=0o700)
    except FileExistsError:raise ValueError('environment_request_identity_conflict') from None
    once(folder/'intent.json',request)
    def file_hash(path,limit):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>limit:
                raise ValueError('unsupported_environment_file')
            value=hashlib.sha256();prefix=b''
            with os.fdopen(fd,'rb',closefd=False) as stream:
                while True:
                    block=stream.read(1024**2)
                    if not block:break
                    if not prefix:prefix=block[:4]
                    value.update(block)
            after=os.fstat(fd)
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('changed_environment_file')
            return value.hexdigest(),before.st_size,prefix
        finally:os.close(fd)
    total=0
    for path,expected in sorted(request['native_files'].items()):
        row=dict(path=path,expected_sha256=expected,sha256=None,size=None,status='unverified')
        try:
            actual,size,_=file_hash(path,1024**3);total+=size
            if total>2*1024**3:raise ValueError('environment_metadata_byte_limit')
            row.update(sha256=actual,size=size,status='verified' if actual==expected else 'changed')
        except (OSError,ValueError):pass
        report['native_files'].append(row)
    # -I -S prevents user/site startup hooks. Read distribution metadata without
    # importing numpy, lammps, an author module or any scientific extension.
    metadata_code='''import json,sys,sysconfig,re
from pathlib import Path
from importlib import metadata
value=json.loads(sys.argv[1]);roots=[]
paths=sysconfig.get_paths();prefix=Path(value['candidate']).parent.parent
if (prefix/'pyvenv.cfg').is_file():
 paths=sysconfig.get_paths(vars={'base':str(prefix),'platbase':str(prefix)})
for key in ('purelib','platlib'):
 if paths[key] not in roots:roots.append(paths[key])
rows=[]
for name in value['names']:
 found=list(metadata.Distribution.discover(name=name,path=roots))
 versions=[item.version for item in found[:2]]
 state='installed' if len(found)==1 and isinstance(versions[0],str) and 0<len(versions[0])<=200 else 'missing_metadata' if not found else 'ambiguous_metadata'
 rows.append({'name':name,'state':state,'version':versions[0] if state=='installed' else None})
print(json.dumps({'version':list(sys.version_info[:3]),'executable':sys.executable,'distributions':rows},allow_nan=False))
'''
    def run(argv,index):
        if _test_capture is not None:return _test_capture(argv)
        import resource
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE,(128*1024,128*1024))
            resource.setrlimit(resource.RLIMIT_CPU,(5,5))
            resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        out_fd=os.open(folder/('python-'+str(index)+'.stdout'),os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        err_fd=os.open(folder/('python-'+str(index)+'.stderr'),os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(out_fd,'wb') as out, os.fdopen(err_fd,'wb') as err:
            process=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=out,stderr=err,cwd='/',
                env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},timeout=10,check=False,preexec_fn=limits)
        if process.returncode!=0:raise ValueError('python_metadata_process_failed')
        return read(folder/('python-'+str(index)+'.stdout'),128*1024)
    native_ok=all(row['status']=='verified' for row in report['native_files'])
    for index,candidate in enumerate(request['python_candidates']):
        row=dict(**candidate,real_path=None,sha256=None,size=None,version=None,distributions=[],
                 status='unverified',code='native_profile_unverified')
        if native_ok:
            try:
                real=str(Path(candidate['path']).resolve(strict=True));absolute(real)
                actual,size,prefix=file_hash(real,256*1024**2)
                if prefix!=b'\x7fELF':raise ValueError('python_not_native_executable')
                argv=[real,'-I','-S','-c',metadata_code,json.dumps(dict(candidate=candidate['path'],names=request['required_distributions']))]
                value=json.loads(run(argv,index))
                if (not isinstance(value,dict) or set(value)!={'version','executable','distributions'}
                        or not isinstance(value['version'],list) or len(value['version'])!=3
                        or any(type(item)is not int or item<0 for item in value['version']) or value['version'][0]!=3
                        or str(Path(value['executable']).resolve(strict=True))!=real
                        or str(Path(candidate['path']).resolve(strict=True))!=real
                        or file_hash(real,256*1024**2)[:2]!=(actual,size)):
                    raise ValueError('invalid_python_metadata')
                distributions=value['distributions']
                if (not isinstance(distributions,list) or [item.get('name') for item in distributions]!=request['required_distributions']
                        or any(not isinstance(item,dict) or set(item)!={'name','state','version'}
                            or item['state'] not in {'installed','missing_metadata','ambiguous_metadata'}
                            or (item['state']=='installed' and (not isinstance(item['version'],str)
                                or not 1<=len(item['version'])<=200 or any(ord(c)<32 for c in item['version'])))
                            or (item['state']!='installed' and item['version'] is not None) for item in distributions)):
                    raise ValueError('invalid_distribution_metadata')
                row.update(real_path=real,sha256=actual,size=size,version=value['version'],distributions=distributions,
                    status='verified' if all(item['state']=='installed' for item in distributions) else 'dependencies_unverified',code=None)
            except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
                row['code']='python_metadata_unverified'
        report['python_candidates'].append(row)
    report['state']='metadata_ready' if native_ok and any(row['status']=='verified' for row in report['python_candidates']) else 'incomplete'
    if not native_ok:report['missing'].append('原固定运行环境文件未全部通过当前SHA核对。')
    if not any(row['status']=='verified' for row in report['python_candidates']):
        report['missing'].append('尚无同时具备固定解释器身份和全部所需依赖元数据的Python候选。')
    report['missing']+=['作者驱动的同一计算分配子进程策略尚须独立审核。','作者原始入口与执行部署尚须独立登记和批准。']
    report['at']=datetime.now(timezone.utc).isoformat()
    once(folder/'receipt.json',report)
    return report


REMOTE_CODE = inspect.getsource(_remote_inventory) + '''
import base64,json,sys
value=json.loads(sys.stdin.buffer.read(1048577))
if set(value)!={'request','action'}:raise ValueError('invalid_environment_envelope')
print(base64.b64encode(json.dumps(_remote_inventory(value['request'],action=value['action']),ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).decode())
'''


def _validate_report(request, report):
    fixed=dict(version=VERSION,purpose=PURPOSE,**FLAGS,id=request['id'],location='hpc',
        request_sha256=sha256(canonical(request)),binding_sha256=request['binding_sha256'],worker_sha256=request['worker_sha256'])
    if (not isinstance(report,dict) or len(canonical(report))>MAX_REPORT
            or any(report.get(key)!=value or type(report.get(key))is not type(value) for key,value in fixed.items())
            or set(report)!=(set(fixed)|{'state','native_files','python_candidates','missing'}|({'at'} if 'at' in report else set()))
            or report.get('state') not in {'unknown','not_started','metadata_ready','incomplete'}
            or not isinstance(report.get('native_files'),list) or not isinstance(report.get('python_candidates'),list)
            or not isinstance(report.get('missing'),list)):
        raise TaskError('已有环境元数据回执身份或字段未核验。')
    if report['state'] in {'unknown','not_started'}:
        if report['native_files'] or report['python_candidates']:raise TaskError('未开始环境检查不能携带成功内容。')
        return deepcopy(report)
    if len(report['native_files'])!=len(request['native_files']) or len(report['python_candidates'])!=len(request['python_candidates']):
        raise TaskError('已有环境元数据清单不能缺件或截断。')
    for row,(path,expected) in zip(report['native_files'],sorted(request['native_files'].items())):
        if (not isinstance(row,dict) or set(row)!={'path','expected_sha256','sha256','size','status'}
                or row['path']!=path or row['expected_sha256']!=expected
                or row['status'] not in {'unverified','changed','verified'}
                or (row['status']=='verified' and row['sha256']!=expected)
                or (row['sha256'] is not None and (not isinstance(row['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',row['sha256'])))
                or (row['size'] is not None and (type(row['size'])is not int or not 0<=row['size']<=1024**3))):
            raise TaskError('固定运行文件元数据发生变化。')
    for row,candidate in zip(report['python_candidates'],request['python_candidates']):
        if (not isinstance(row,dict) or set(row)!={'id','label','path','real_path','sha256','size','version','distributions','status','code'}
                or any(row.get(key)!=value for key,value in candidate.items())
                or row['status'] not in {'unverified','verified','dependencies_unverified'}
                or not isinstance(row['distributions'],list)):
            raise TaskError('Python候选元数据与固定菜单不一致。')
        if row['status']!='unverified':
            runtime.absolute(row['real_path']);runtime.hash_value(row['sha256'])
            if (type(row['size'])is not int or not 0<=row['size']<=256*1024**2
                    or not isinstance(row['version'],list) or len(row['version'])!=3 or row['version'][0]!=3
                    or any(type(v)is not int or v<0 for v in row['version'])
                    or [item.get('name') for item in row['distributions']]!=request['required_distributions']):
                raise TaskError('Python解释器或依赖证明不完整。')
            for item in row['distributions']:
                if (not isinstance(item,dict) or set(item)!={'name','state','version'}
                        or item['state'] not in {'installed','missing_metadata','ambiguous_metadata'}
                        or (item['state']=='installed' and (not isinstance(item['version'],str) or not 1<=len(item['version'])<=200
                            or any(ord(c)<32 for c in item['version'])))
                        or (item['state']!='installed' and item['version'] is not None)):
                    raise TaskError('已安装依赖元数据无效。')
            if (row['status']=='verified')!=all(item['state']=='installed' for item in row['distributions']):
                raise TaskError('缺失依赖不能标成已核验。')
    ready=all(row['status']=='verified' for row in report['native_files']) and any(row['status']=='verified' for row in report['python_candidates'])
    if (report['state']=='metadata_ready')!=ready or any(item not in report['missing'] for item in _POLICY_MISSING):
        raise TaskError('环境元数据不得冒充作者执行就绪或忽略审批缺项。')
    return deepcopy(report)


class AuthorEnvironmentInventory:
    """Project-persistent metadata receipts; construction/history never transmit."""
    def __init__(self,tasks,hpc,*,profile_provider,audit_directory,capture=_capture):
        if not callable(profile_provider):raise TaskError('需要服务端已固定环境来源。')
        self.tasks,self.hpc,self.profile_provider,self.capture=tasks,hpc,profile_provider,capture
        self.audit=private_directory(audit_directory)
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_environment_intents ('
                'id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),binding_sha256 TEXT NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS author_environment_events ('
                'id TEXT NOT NULL REFERENCES author_environment_intents(id),sequence INTEGER NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            for table in ('author_environment_intents','author_environment_events'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} '
                               'BEGIN SELECT RAISE(ABORT,"immutable author environment inventory"); END')

    def _context(self,identifier):
        task=self.tasks.get(task_id(identifier));life=self.tasks.lifecycle(identifier)
        if self.tasks.paused(identifier) or life['deleted'] or life['user_finished']:
            raise TaskError('任务已暂停或结束，环境历史仍保留。')
        raw=self.profile_provider(identifier)
        if not isinstance(raw,dict) or set(raw) not in (_PROVIDER,_PROVIDER|{'python_dependency_requirements'},
                _PROVIDER|{'python_dependency_requirements','python_dependency_missing'}):raise TaskError('已有环境来源只接受服务端固定字段。')
        path=Path(raw['profile_path'])
        if not path.is_absolute():raise TaskError('环境profile须为固定私有元数据文件。')
        runtime.hash_value(raw['profile_sha256'])
        data=runtime.read_regular(path,1024**2,private=True)
        if sha256(data)!=raw['profile_sha256']:raise TaskError('环境profile来源摘要已变化。')
        profile=json.loads(data);runtime.validate_deployment_paths(profile)
        if runtime.execution_mode(profile)!='native_slurm':raise TaskError('仅复用已固定native Slurm环境。')
        native=runtime.deployment_parallelism(profile,1)
        candidates=raw['python_candidates'];names=raw['required_distributions']
        if not isinstance(candidates,list) or not 1<=len(candidates)<=8 or not isinstance(names,list) or len(names)>32:
            raise TaskError('已有解释器及依赖目录须明确且有界。')
        seen=set();normalized=[]
        for row in candidates:
            if (not isinstance(row,dict) or set(row)!={'id','label','path'} or not isinstance(row['id'],str)
                    or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',row['id']) or row['id'] in seen):
                raise TaskError('已有解释器只接受固定编号、说明和路径。')
            runtime.absolute(row['path']);text(row['label'],500);seen.add(row['id'])
        for name in names:
            if not isinstance(name,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',name):
                raise TaskError('依赖名称须为固定distribution名称，不能是模块表达式或安装命令。')
            value=re.sub('[-_.]+','-',name).lower()
            if value in normalized:raise TaskError('所需依赖不能重复。')
            normalized.append(value)
        requirements=raw.get('python_dependency_requirements')
        if requirements is not None:
            from .author_python_dependencies import validate_requirements
            requirements=validate_requirements(requirements,identifier)
            if normalized!=requirements['required_distributions']:
                raise TaskError('元数据查询清单必须完整保留当前原Python文本的显式依赖。')
        transport=SavedHPCTransport(self.hpc,raw['connection_revision'],raw['host_alias'])
        binding=dict(version=VERSION,purpose=PURPOSE,profile_sha256=sha256(data),profile_path=str(path),
            native_files=deepcopy(native['files']),python_candidates=deepcopy(candidates),required_distributions=normalized,
            connection_revision=raw['connection_revision'],host_alias=raw['host_alias'],
            transport_identity_sha256=sha256(canonical(transport.identity)),worker_sha256=sha256(REMOTE_CODE.encode()))
        if requirements is not None:binding['python_dependency_requirements']=requirements
        if raw.get('python_dependency_missing'):
            missing=raw['python_dependency_missing']
            if not isinstance(missing,list) or len(missing)>8 or any(not isinstance(item,str) or not 1<=len(item)<=2000 for item in missing):
                raise TaskError('Python源码依赖缺项说明须明确且有界。')
            binding['python_dependency_missing']=deepcopy(missing)
        return binding,transport

    def _stored(self,identifier,request=None):
        self.tasks.get(task_id(identifier))
        with self.tasks.transaction() as db:
            rows=db.execute('SELECT * FROM author_environment_intents WHERE '+('id=?' if request else 'task_id=?')+' ORDER BY rowid',
                            (task_id(request) if request else identifier,)).fetchall()
            result=[]
            for row in rows:
                context=json.loads(row['document'])
                if row['task_id']!=identifier:raise TaskError('环境检查请求属于其他任务。')
                if sha256(canonical(context))!=row['sha256'] or context['task_id']!=identifier or context['request']['id']!=row['id']:
                    raise TaskError('环境检查意图摘要不一致。')
                events=[]
                for item in db.execute('SELECT * FROM author_environment_events WHERE id=? ORDER BY sequence',(row['id'],)):
                    event=json.loads(item['document'])
                    if sha256(canonical(event))!=item['sha256']:raise TaskError('环境检查历史摘要不一致。')
                    if event['report'] is not None:_validate_report(context['request'],event['report'])
                    events.append(event)
                result.append(dict(context=context,events=events,context_sha256=row['sha256'],at=row['at']))
        return result

    @staticmethod
    def _summary(row):
        latest=row['events'][-1] if row['events'] else None
        report=latest['report'] if latest else None
        result=dict(version=VERSION,**FLAGS,task_id=row['context']['task_id'],request_id=row['context']['request']['id'],
            state=report['state'] if report else 'unknown',at=row['at'],report=deepcopy(report),
            report_sha256=sha256(canonical(report)) if report is not None else None,
            context_sha256=row['context_sha256'],events=deepcopy(row['events']),
            missing=deepcopy(report['missing']) if report else ['原环境检查回执待核对，未重复执行。'])
        requirements=row['context']['binding'].get('python_dependency_requirements')
        if requirements is not None:
            from .author_python_dependencies import project_capabilities
            result['capabilities']=project_capabilities(requirements,report)
        if row['context']['binding'].get('python_dependency_missing'):
            result['python_dependency_missing']=deepcopy(row['context']['binding']['python_dependency_missing'])
        return result

    def get(self,identifier,request):
        rows=self._stored(identifier,request)
        return self._summary(rows[0]) if rows else None

    def history(self,identifier):
        return [self._summary(row) for row in self._stored(identifier)]

    def status(self,identifier):
        history=self.history(identifier)
        return dict(version=VERSION,**FLAGS,task_id=identifier,history=history,latest=history[-1] if history else None,
            calls_model=False,checks_on_GET=False,submits_HPC=False)

    def _event(self,request,report,transport):
        value=dict(at=datetime.now(timezone.utc).isoformat(),report=deepcopy(report),transport=deepcopy(transport))
        with self.tasks.transaction() as db:
            number=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM author_environment_events WHERE id=?',(request,)).fetchone()[0]
            db.execute('INSERT INTO author_environment_events VALUES(?,?,?,?)',(request,number,canonical(value).decode(),sha256(canonical(value))))

    def inspect(self,identifier,request):
        task_id(identifier);task_id(request)
        rows=self._stored(identifier,request)
        if rows:return self._summary(rows[0])
        binding,transport=self._context(identifier)
        directory=str(PurePosixPath(transport.identity['profile']['work_directory'])/'Auto-LAMMPS-Environment-Audit')
        remote=dict(version=VERSION,purpose=PURPOSE,id=request,binding_sha256=sha256(canonical(binding)),remote_directory=directory,
            native_files=binding['native_files'],python_candidates=binding['python_candidates'],
            required_distributions=binding['required_distributions'],worker_sha256=binding['worker_sha256'])
        context=dict(version=VERSION,**FLAGS,task_id=identifier,binding=binding,request=remote)
        with self.tasks.transaction() as db:
            if db.execute('SELECT 1 FROM author_environment_intents WHERE id=?',(request,)).fetchone():
                raced=True
            else:
                for row in db.execute('SELECT i.id FROM author_environment_intents i WHERE i.binding_sha256=?',(remote['binding_sha256'],)):
                    event=db.execute('SELECT document FROM author_environment_events WHERE id=? ORDER BY sequence DESC LIMIT 1',(row['id'],)).fetchone()
                    if event is None or (json.loads(event['document']).get('report') or {}).get('state')=='unknown':
                        raise TaskError('同一固定环境仍有未知检查，须先核对原请求，不能换号重查。')
                db.execute('INSERT INTO author_environment_intents VALUES(?,?,?,?,?,?)',
                    (request,identifier,remote['binding_sha256'],canonical(context).decode(),sha256(canonical(context)),datetime.now(timezone.utc).isoformat()))
                raced=False
        if raced:return self.get(identifier,request)
        return self._observe(identifier,request,'inspect',transport)

    def reconcile(self,identifier,request):
        rows=self._stored(identifier,request)
        if not rows:raise TaskError('请使用原环境检查标识核对。')
        if rows[0]['events'] and self._summary(rows[0])['state']!='unknown':return self._summary(rows[0])
        return self._observe(identifier,request,'reconcile')

    def _observe(self,identifier,request,action,transport=None):
        row=self._stored(identifier,request)[0];context=row['context']
        current,checked=self._context(identifier)
        if current!=context['binding']:raise TaskError('环境来源或保存连接已变化；原请求保留，未迁移或重发。')
        transport=transport or checked
        envelope=canonical(dict(request=context['request'],action=action))
        command=transport.command(current['host_alias'],['python3','-I','-S','-c',REMOTE_CODE])
        trace=private_directory(self.audit/(request+'-'+action+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
        _write_new(trace/'intent.json',dict(request_sha256=sha256(canonical(context['request'])),action=action,command_sha256=sha256(canonical(command))))
        try:result=self.capture(command,timeout=60,max_bytes=MAX_REPORT*2,input_chunks=[envelope])
        except (OSError,ValueError):result=dict(returncode=None,failure='metadata_transport_unverified',stdout='',stderr='')
        _write_new(trace/'transport.json',result)
        report=None
        if result.get('returncode')==0 and not result.get('failure'):
            try:
                payload=base64.b64decode(result['stdout'],validate=True).strip()
                report=_validate_report(context['request'],json.loads(base64.b64decode(payload,validate=True)))
            except (ValueError,KeyError,TypeError):pass
        summary={key:result.get(key) for key in ('returncode','failure')}
        summary.update(action=action,report_verified=report is not None)
        if report is None:
            report=dict(version=VERSION,purpose=PURPOSE,**FLAGS,id=request,location='hpc',
                request_sha256=sha256(canonical(context['request'])),binding_sha256=context['request']['binding_sha256'],
                worker_sha256=context['request']['worker_sha256'],state='unknown',native_files=[],python_candidates=[],
                missing=['已有环境元数据回执未核对；原请求保留，未重发检查。'])
        self._event(request,report,summary)
        return self.get(identifier,request)

    def planner_environments(self,identifier,request):
        """A non-authorizing menu, rechecked against the unchanged saved source."""
        row=self._stored(identifier,request)
        if not row:raise TaskError('环境检查回执尚不存在。')
        row=row[0];binding,_=self._context(identifier)
        if binding!=row['context']['binding']:raise TaskError('环境检查来源已变化；旧历史保留，不作为当前菜单。')
        value=self._summary(row);report=value['report'];environments=[]
        capabilities=value.get('capabilities')
        base_ready=bool(capabilities and capabilities['base_native_ready'])
        if report is not None and (report['state']=='metadata_ready' or base_ready):
            for candidate in report['python_candidates']:
                if candidate['status']!='verified' and not (base_ready and candidate['status']=='dependencies_unverified'):continue
                identity=sha256(canonical(dict(profile=binding['profile_sha256'],native_files=report['native_files'],python=candidate)))
                environments.append(dict(id='metadata-python-'+identity[:16],label=candidate['label'],sha256=identity,
                    executables=[dict(id=candidate['id'],label=candidate['label'],sha256=candidate['sha256'],kind='python')]))
            # Existing native programs share the very same pinned CPU profile.
            # Projection is metadata only; author entry/child policies stay false.
            raw=runtime.read_regular(binding['profile_path'],1024**2,private=True)
            if sha256(raw)!=binding['profile_sha256']:raise TaskError('已有部署文件已变化。')
            native=json.loads(raw)['native_slurm']
            verified={item['path']:item['sha256'] for item in report['native_files'] if item['status']=='verified'}
            for environment in environments:
                for field,kind,label in [('engine_path','lammps','已有CPU LAMMPS引擎'),('shell_path','shell','已有作者脚本解释器')]:
                    path=native[field]
                    if path not in verified or verified[path]!=binding['native_files'].get(path):
                        raise TaskError('已有程序未通过当前原件摘要核对。')
                    environment['executables'].append(dict(id='deployment_'+kind,label=label,sha256=verified[path],kind=kind))
        missing=(deepcopy(_POLICY_MISSING) if base_ready else deepcopy(value['missing']))
        result=dict(version=VERSION,**FLAGS,task_id=identifier,request_id=request,report_sha256=value['report_sha256'],
            metadata_status=value['state'],environments=environments,missing=missing,
            environment_evidence=deepcopy(report),calls_model=False,reads_remote=False,
            note='仅证明已有解释器及distribution元数据；不能替代作者入口、子进程、部署或科学适用性审核。')
        if capabilities is not None:result.update(capabilities=capabilities,raw_missing=deepcopy(value['missing']))
        if value.get('python_dependency_missing'):result['python_dependency_missing']=value['python_dependency_missing']
        return result

    def capabilities(self,identifier,request=None):
        """Current source/profile/transport-bound metadata; no remote action."""
        rows=self._stored(identifier,request)
        if not rows:raise TaskError('环境检查回执尚不存在。')
        row=rows[-1];binding,_=self._context(identifier)
        if binding!=row['context']['binding']:raise TaskError('依赖来源、环境profile或保存连接已变化；原回执保留。')
        return self._summary(row).get('capabilities')
