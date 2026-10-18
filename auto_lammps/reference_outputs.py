"""Explicit user retrieval of pinned legacy author outputs, never execution.

The private controller supplies the original run and observed output inventory.
It is not a replacement execution receipt or a scientific completion verdict.
"""
import base64
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid

from . import reference_output_worker as worker
from .manifest import canonical, private_directory, sha256
from .raw_outputs import RawOutputs
from .remote_resources import _bind_local_intent, _bound_transport_command, _saved_transport_binding
from .runtime_launcher import read_regular
from .slurm_read import _capture, _write_new
from .tasks import TaskError, task_id


class ReferenceOutputs:
    def __init__(self, raw_outputs, directory, bindings, *, transport, capture=_capture):
        if not isinstance(raw_outputs, RawOutputs) or not isinstance(bindings, list) or len(bindings)>256:
            raise TaskError('作者原始输出配置无效。')
        self.raw, self.transport, self.capture = raw_outputs, transport, capture
        self.directory = private_directory(directory)
        self.bindings = {}
        fields = {'task_id','request_id','job_id','manifest_sha256','host_alias','root',
                  'files','inventory_receipt','inventory_receipt_sha256'}
        for binding in bindings:
            if not isinstance(binding,dict) or set(binding)!=fields:
                raise TaskError('作者原始输出需要固定的运行与清单来源。')
            task_id(binding['task_id'])
            request = self._request(binding)
            worker.validate_request(request)
            if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}',f['name'])
                   or f['name'] in {'intent.json','receipt.json','collection.lock'} for f in binding['files']):
                raise TaskError('作者输出显示名无效或与控制记录重名。')
            path = Path(binding['inventory_receipt'])
            if not path.is_absolute() or sha256(read_regular(path,16*1024*1024,private=True))!=binding['inventory_receipt_sha256']:
                raise TaskError('作者输出清单来源已改变。')
            self._run(binding)
            pin = _saved_transport_binding(transport,binding['host_alias'],binding['root'])
            key = (binding['task_id'],binding['request_id'])
            if key in self.bindings:
                raise TaskError('作者输出清单身份重复。')
            self.bindings[key] = (deepcopy(binding),pin)

    @staticmethod
    def _request(binding):
        return dict(version=1,**{k:deepcopy(binding[k]) for k in
            ('request_id','job_id','manifest_sha256','root','files')})

    def _run(self,binding):
        row = self.raw._request(binding['task_id'],binding['request_id'])
        identity = self.raw.ledger.evaluation_snapshot(row['evaluation'])['identity']
        if identity['role']!='reference' or any(row[k]!=binding[k] for k in ('job_id','manifest_sha256')):
            raise TaskError('只允许读取与原账本一致的作者 A 输出。')
        return row

    def _binding(self,identifier,request_id):
        self.raw.tasks.get(identifier)
        found = self.bindings.get((identifier,request_id))
        if found is None:
            raise TaskError('这项任务未登记该作者原始输出清单。')
        binding,pin = found
        self._run(binding)
        if sha256(read_regular(Path(binding['inventory_receipt']),16*1024*1024,private=True))!=binding['inventory_receipt_sha256']:
            raise TaskError('作者输出清单来源已改变。')
        return binding,pin

    def status(self,identifier):
        self.raw.tasks.get(identifier)
        records = []
        listing = self.raw.listing(identifier)['files']
        registered = {(f['request_id'],f['name']):f for f in listing}
        for task,request in self.bindings:
            if task!=identifier:continue
            binding,_ = self._binding(task,request)
            ready = all((request,f['name']) in registered and
                registered[(request,f['name'])]['sha256']==f['sha256'] and
                registered[(request,f['name'])]['size']==f['size'] for f in binding['files'])
            unavailable=False
            if ready:
                try:
                    for file in binding['files']:
                        _,_,stream=self.raw.download(identifier,registered[(request,file['name'])]['id'])
                        stream.close()
                except (OSError,ValueError,TaskError):
                    unavailable=True
            row = self._run(binding)
            records.append(dict(request_id=request,job_id=row['job_id'],scheduler_state=row['state'],
                state='retained_unavailable' if unavailable else 'retained' if ready else 'available',files=[dict(name=f['name'],size=f['size']) for f in binding['files']],
                total_bytes=sum(f['size'] for f in binding['files']),scientific_status='not_evaluated',
                message='只回收已固定清单中的原始字节；不重新计算，不代表整个作者流程已完成。'))
        return dict(task_id=identifier,configured=bool(records),runs=records)

    def collect(self,identifier,request_id):
        binding,pin = self._binding(identifier,request_id)
        request = self._request(binding)
        script = Path(worker.__file__).read_bytes()
        identity = dict(binding=binding,transport_binding=pin,worker_sha256=sha256(script))
        digest = sha256(canonical(identity))
        folder = private_directory(self.directory/digest)
        lock = os.open(folder/'collection.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid():
                raise TaskError('作者输出回收锁无效。')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise TaskError('该作者输出正在回收，请稍后刷新。') from None
            saved = folder/'receipt.json'
            if saved.exists():
                self._publish(identifier,request_id,folder,json.loads(read_regular(saved,262144,private=True)))
                return self.status(identifier)
            command = _bound_transport_command(self.transport,pin,binding['host_alias'],binding['root'],
                ['python3','-I','-c',self._bootstrap(script)])
            # Retained exports are charged once to the original run. They never
            # reserve a new physics request, attempt, or additional core time.
            self.raw.ledger.reserve_raw_export(request_id,digest,sum(f['size'] for f in binding['files'])+262144)
            _bind_local_intent(folder/'intent.json',identity)
            # Persistent audit bytes, including repeated read failures, remain
            # inside this export's metadata reservation. No history is erased.
            payload_names={f['name']:f['size'] for f in binding['files']}
            metadata_bytes=0
            for path in folder.iterdir():
                info=path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                    raise TaskError('作者输出回收目录包含无效文件。')
                metadata_bytes+=max(0,info.st_size-payload_names.get(path.name,0))
            if metadata_bytes+4096>262144:
                raise TaskError('本次回收的审计存储已用完；原记录保留，未再次读取。')
            try:
                result = self.capture(command,timeout=60,max_bytes=worker.response_limit(request),
                    input_chunks=[canonical(request)])
            except OSError:
                result=dict(returncode=None,failure='transport_error',stdout='',stderr='')
            _write_new(folder/(uuid.uuid4().hex+'-transport.json'),dict(
                failed=bool(result.get('returncode')!=0 or result.get('failure')),
                stdout_sha256=sha256(str(result.get('stdout') or '').encode()),
                stderr_sha256=sha256(str(result.get('stderr') or '').encode()),raw_transport_retained=False))
            if result.get('returncode')!=0 or result.get('failure'):
                raise TaskError('作者输出回收未完成；原作业、费用和文件均保留。')
            try:
                if (not isinstance(result.get('stdout'),str)
                        or len(result['stdout']) > 4*((worker.response_limit(request)+2)//3)):
                    raise ValueError('response limit')
                report = json.loads(base64.b64decode(result['stdout'].strip(),validate=True))
                expected = {k:request[k] for k in ('version','request_id','job_id','manifest_sha256')}
                if not isinstance(report,dict) or set(report)!={*expected,'files'} or any(report[k]!=v for k,v in expected.items()):
                    raise ValueError('identity')
                if len(report['files'])!=len(binding['files']):raise ValueError('inventory')
                for item,original in zip(report['files'],binding['files']):
                    if not isinstance(item,dict) or set(item)!={*original,'data'} or any(item[k]!=v for k,v in original.items()):
                        raise ValueError('file identity')
                    data=base64.b64decode(item['data'],validate=True)
                    if len(data)!=original['size'] or sha256(data)!=original['sha256']:raise ValueError('bytes')
                    destination=folder/original['name']
                    if destination.exists():
                        if read_regular(destination,worker.MAX_FILE_BYTES,private=True)!=data:raise ValueError('changed retained file')
                    else:
                        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                        with os.fdopen(fd,'wb') as stream:stream.write(data)
                receipt=dict(request_id=request_id,job_id=binding['job_id'],manifest_sha256=binding['manifest_sha256'],
                    inventory_receipt_sha256=binding['inventory_receipt_sha256'],worker_sha256=sha256(script),
                    transport_binding=pin,physical_evaluation_performed=False,scientific_status='not_evaluated',
                    files=[dict(name=f['name'],size=f['size'],sha256=f['sha256'],kind='output') for f in binding['files']])
                _write_new(saved,receipt)
            except (ValueError,TypeError,KeyError):
                raise TaskError('回收文件的身份、清单或摘要不符，未发布原始输出。') from None
            self._publish(identifier,request_id,folder,receipt)
            return self.status(identifier)
        finally:os.close(lock)

    @staticmethod
    def _bootstrap(source):
        # Only this shipped application helper is executed. Author bytes are
        # strictly read as data by its bounded file operations.
        encoded=base64.b64encode(source).decode()
        return ('import base64,json,sys;ns={"__name__":"trusted_reference_output_reader"};'
                f'exec(compile(base64.b64decode({encoded!r}),"reference_output_worker.py","exec"),ns);'
                'request=json.loads(sys.stdin.buffer.read(65537));'
                'sys.stdout.buffer.write(ns["canonical"](ns["read_outputs"](request)))')

    def _publish(self,identifier,request,folder,receipt):
        binding,_=self._binding(identifier,request)
        expected=[dict(name=f['name'],size=f['size'],sha256=f['sha256'],kind='output') for f in binding['files']]
        if receipt.get('files')!=expected or receipt.get('inventory_receipt_sha256')!=binding['inventory_receipt_sha256']:
            raise TaskError('原始输出来源与已固定清单不符。')
        for file in expected:
            self.raw.publish(identifier,request,file['name'],folder/file['name'],folder/'receipt.json')


def load_reference_outputs(raw_outputs,path,hpc_connections):
    from .hpc_transport import SavedHPCTransport
    config=json.loads(read_regular(Path(path),262144,private=True))
    if not isinstance(config,dict) or set(config)!={'version','directory','hpc_revision','host_alias','bindings'} or config['version']!=1:
        raise TaskError('作者原始输出部署配置无效。')
    transport=SavedHPCTransport(hpc_connections,config['hpc_revision'],config['host_alias'])
    if any(binding.get('host_alias')!=config['host_alias'] for binding in config['bindings']):
        raise TaskError('作者原始输出连接不一致。')
    return ReferenceOutputs(raw_outputs,config['directory'],config['bindings'],transport=transport)
