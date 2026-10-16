"""Reference controller: ship trusted tools, return metadata, keep source on HPC."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import uuid
import zipfile

from .manifest import canonical, private_directory, sha256
from .potential_acquisition import AcquisitionError, safe_path
from .reference_resource_worker import validate_request
from .runtime_launcher import ExecutionDenied, read_regular
from .slurm_read import _capture, _write_new

MODULES = ('__init__','reference_resource_worker','engine_source_worker','potential_acquisition',
           'potentials','source_discovery','papers','tasks','manifest','ledger','slurm_read',
           'author_resource_inventory_worker')
BOOTSTRAP = '''import sys,os,json,base64,hashlib,stat
from pathlib import Path
if sys.platform!='linux' or not os.environ.get('SSH_CONNECTION'):raise ValueError('HPC SSH required')
raw=sys.stdin.buffer.read(2097153)
if len(raw)>2097152:raise ValueError('Request limit')
envelope=json.loads(raw);request=envelope['request'];data=base64.b64decode(envelope['bundle'],validate=True)
digest=hashlib.sha256(data).hexdigest()
if digest!=request['bundle_sha256']:raise ValueError('Bundle identity mismatch')
root=Path(request['directory'])
if not root.is_absolute() or '..' in root.parts:raise ValueError('Invalid directory')
for p in [root,*root.parents]:
 if p.is_symlink():raise ValueError('Symlink directory')
root.mkdir(parents=True,mode=0o700,exist_ok=True)
if root.stat().st_uid!=os.getuid() or stat.S_IMODE(root.stat().st_mode)&0o077:raise ValueError('Private directory required')
bundle=root/('tools-'+digest+'.zip')
try:
 with bundle.open('xb') as f:
  os.fchmod(f.fileno(),0o400);f.write(data);f.flush();os.fsync(f.fileno())
except FileExistsError:
 if bundle.is_symlink() or bundle.stat().st_size!=len(data) or hashlib.sha256(bundle.read_bytes()).hexdigest()!=digest:raise ValueError('Saved bundle mismatch')
sys.path.insert(0,str(bundle))
from auto_lammps.reference_resource_worker import acquire
print(json.dumps(acquire(request)))
'''

INVENTORY_BOOTSTRAP = '''import sys,os,json,base64,hashlib,stat,re
from pathlib import Path
if sys.platform!='linux' or not os.environ.get('SSH_CONNECTION'):raise ValueError('HPC SSH required')
raw=sys.stdin.buffer.read(2097153)
if len(raw)>2097152:raise ValueError('Envelope limit')
envelope=json.loads(raw)
if not isinstance(envelope,dict) or set(envelope)!={'request','bundle','bundle_sha256'}:raise ValueError('Invalid envelope')
request=envelope['request'];data=base64.b64decode(envelope['bundle'],validate=True)
digest=hashlib.sha256(data).hexdigest()
if len(data)>1048576 or digest!=envelope['bundle_sha256']:raise ValueError('Tool bundle mismatch')
if not isinstance(request,dict) or set(request)!={'version','acquisition_request','receipt_sha256','tree_sha1'} or type(request['version']) is not int or request['version']!=1:raise ValueError('Invalid request')
original=request['acquisition_request']
if not isinstance(original,dict) or set(original)!={'id','repository','commit','directory','bundle_sha256','storage_bytes'}:raise ValueError('Invalid original request')
for key,n in [('id',32),('commit',40),('bundle_sha256',64)]:
 if not isinstance(original[key],str) or not re.fullmatch('[a-f0-9]{'+str(n)+'}',original[key]):raise ValueError('Invalid original identity')
for key,n in [('receipt_sha256',64),('tree_sha1',40)]:
 if not isinstance(request[key],str) or not re.fullmatch('[a-f0-9]{'+str(n)+'}',request[key]):raise ValueError('Invalid cache identity')
if not isinstance(original['directory'],str) or len(original['directory'])>2048 or type(original['storage_bytes']) is not int or not 536870912<=original['storage_bytes']<=17179869184:raise ValueError('Invalid original bounds')
root=Path(original['directory'])
if not root.is_absolute() or '..' in root.parts or str(root)=='/' or str(root)!=request['acquisition_request']['directory']:raise ValueError('Invalid directory')
directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
parts=root.parts[1:]
for index,part in enumerate(parts):
 access=os.O_RDONLY if index==len(parts)-1 else getattr(os,'O_PATH',os.O_RDONLY)
 child=os.open(part,access|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
 os.close(directory);directory=child
info=os.fstat(directory)
if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise ValueError('Private directory required')
def cached(directory,name,maximum):
 descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
 try:
  info=os.fstat(descriptor)
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>maximum:raise ValueError('Unsafe cached metadata')
  with os.fdopen(descriptor,'rb',closefd=False) as f:value=f.read(maximum+1)
  after=os.fstat(descriptor)
  if len(value)!=info.st_size or (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('Changed cached metadata')
  return json.loads(value)
 finally:os.close(descriptor)
if cached(directory,original['id']+'.lock.json',20000)!=original:raise ValueError('Original lock mismatch')
folder=os.open(original['id'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
info=os.fstat(folder)
if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise ValueError('Private acquisition directory required')
if cached(folder,'intent.json',20000)!=original:raise ValueError('Original intent mismatch')
receipt=cached(folder,'result.json',500000)
encoded=json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
if hashlib.sha256(encoded).hexdigest()!=request['receipt_sha256']:raise ValueError('Original receipt mismatch')
os.close(folder)
name='tools-'+digest+'.zip'
try:
 descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=directory)
 with os.fdopen(descriptor,'wb') as f:
  os.fchmod(f.fileno(),0o400);f.write(data);f.flush();os.fsync(f.fileno())
except FileExistsError:
 pass
descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
info=os.fstat(descriptor)
if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o400 or info.st_size!=len(data):raise ValueError('Saved bundle mismatch')
with os.fdopen(descriptor,'rb',closefd=False) as f:saved=f.read(1048577)
if saved!=data:raise ValueError('Saved bundle mismatch')
# Import through the verified open descriptor, avoiding a path replacement race.
sys.path.insert(0,'/proc/self/fd/'+str(descriptor))
from auto_lammps.author_resource_inventory_worker import inspect_cached_tree
print(json.dumps(inspect_cached_tree(request)))
os.close(descriptor);os.close(directory)
'''


def tool_bundle():
    root=Path(__file__).parent
    contents={f'auto_lammps/{name}.py':(root/(name+'.py')).read_bytes() for name in MODULES}
    for name, filename in (('LICENSE','LICENSE.txt'),('COPYRIGHT.md','COPYRIGHT.txt'),
                           ('third_party/SIGA-LAMMPS-LICENSE.txt','SIGA-LAMMPS-LICENSE.txt')):
        contents[name]=(root/'worker_licenses'/filename).read_bytes()
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(contents.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100400<<16
            z.writestr(info,data)
    value=stream.getvalue()
    if len(value)>1024**2:
        raise AcquisitionError('tool_bundle_size_limit')
    return value


def _saved_transport_binding(transport, host_alias, remote_directory):
    """Return an opaque pin only; saved credentials/profile stay outside audit."""
    if transport is None:
        return None
    from .hpc_transport import SavedHPCTransport
    if not isinstance(transport, SavedHPCTransport):
        raise AcquisitionError('saved_HPC_transport_required')
    identity = transport.identity
    if identity.get('host_alias') != host_alias:
        raise AcquisitionError('saved_HPC_transport_alias_mismatch')
    if not isinstance(remote_directory, str):
        raise AcquisitionError('resource_root_outside_saved_HPC_work_directory')
    resource_root = PurePosixPath(remote_directory)
    work_root = PurePosixPath(identity['profile']['work_directory'])
    # The caller supplies an independent resource root. Never infer or reuse
    # staging storage, and never put resource files outside the saved work area.
    if (not resource_root.is_absolute() or '..' in resource_root.parts
            or str(resource_root) != remote_directory or work_root not in resource_root.parents):
        raise AcquisitionError('resource_root_outside_saved_HPC_work_directory')
    return dict(identity_sha256=sha256(canonical(identity)), revision=identity['revision'])


def _bound_transport_command(transport, binding, host_alias, remote_directory, remote_argv):
    if _saved_transport_binding(transport, host_alias, remote_directory) != binding:
        raise AcquisitionError('saved_HPC_transport_identity_changed')
    if transport is not None:
        try:
            command = transport.command(host_alias, remote_argv)
        except Exception:
            raise AcquisitionError('saved_HPC_transport_command_rejected') from None
        if _saved_transport_binding(transport, host_alias, remote_directory) != binding:
            raise AcquisitionError('saved_HPC_transport_identity_changed')
        return command
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no', '-o', 'RemoteCommand=none', '-T',
            host_alias, shlex.join(remote_argv)]


def _bind_local_intent(path, intent):
    """Exclusive local identity binding; legacy intents cannot adopt a pin."""
    try:
        _write_new(path, intent)
    except FileExistsError:
        try:
            previous = json.loads(read_regular(path, 1000000, private=True))
        except (OSError, ValueError, TypeError, ExecutionDenied):
            raise AcquisitionError('preparation_identity_already_bound') from None
        if previous != intent:
            raise AcquisitionError('preparation_identity_already_bound')


def _transport_failure(result):
    value = result.get('failure') or ''
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{0,100}', value) else 'transport_failed'


class RemoteResources:
    def __init__(self,directory,policy,*,capture=_capture,transport=None):
        if (not isinstance(policy,dict) or set(policy)!={'host_alias','remote_directory','operation_id','storage_bytes'}
                or not isinstance(policy['host_alias'],str)
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',policy['host_alias'])):
            raise AcquisitionError('explicit_HPC_resource_policy_required')
        self.root=private_directory(directory);self.policy=deepcopy(policy);self.capture=capture
        self.transport=transport
        self.transport_binding=_saved_transport_binding(transport,policy['host_alias'],policy['remote_directory'])

    def acquire(self,repository,commit):
        bundle=tool_bundle();policy=self.policy
        request=validate_request(dict(id=policy['operation_id'],repository=repository,commit=commit,
            directory=policy['remote_directory'],storage_bytes=policy['storage_bytes'],bundle_sha256=sha256(bundle)))
        intent=dict(host_alias=policy['host_alias'],request=request)
        if self.transport_binding is not None:
            intent['transport_binding']=deepcopy(self.transport_binding)
        folder=private_directory(self.root/request['id'])
        _bind_local_intent(folder/'intent.json',intent)
        command=_bound_transport_command(self.transport,self.transport_binding,policy['host_alias'],
            policy['remote_directory'],['python3','-c',BOOTSTRAP])
        observation=uuid.uuid4().hex
        _write_new(folder/(observation+'-start.json'),dict(at=datetime.now(timezone.utc).isoformat()))
        data=canonical(dict(request=request,bundle=base64.b64encode(bundle).decode()))
        try:
            result=self.capture(command,timeout=240,max_bytes=750000,
                input_chunks=[data[i:i+65536] for i in range(0,len(data),65536)])
        except OSError as exc:
            result=dict(returncode=None,failure=type(exc).__name__,stdout='',stderr='')
        failure=_transport_failure(result)
        summary=dict(returncode=result.get('returncode'),failure=failure,raw_output_persisted=False)
        for name in ('stdout','stderr'):
            raw=str(result.get(name) or '').encode('utf-8')
            summary[name+'_sha256']=sha256(raw);summary[name+'_bytes']=len(raw)
        if self.transport_binding is not None:
            summary['transport_binding']=deepcopy(self.transport_binding)
        _write_new(folder/(observation+'-transport.json'),summary)
        report=dict(schema_version=1,id=request['id'],repository=repository,commit=commit,location='hpc',
            request_sha256=sha256(canonical(request)),bundle_sha256=request['bundle_sha256'],
            state='unknown',files=[],failures=[],source_complete=False,
            scientific_validation=False,execution_authorized=False,author_identity_verified=False)
        if result.get('returncode')==0 and not result.get('failure'):
            try:
                observed=json.loads(base64.b64decode(result['stdout'],validate=True))
                fixed=('schema_version','id','repository','commit','location','request_sha256','bundle_sha256',
                       'scientific_validation','execution_authorized','author_identity_verified')
                if (not isinstance(observed,dict) or len(canonical(observed))>500000
                        or any(observed.get(k)!=report[k] for k in fixed)
                        or observed.get('state') not in {'finished','partial','unknown'}
                        or type(observed.get('source_complete')) is not bool
                        or not isinstance(observed.get('files'),list) or len(observed['files'])>128):
                    raise ValueError('Invalid remote resource receipt')
                for f in observed['files']:
                    safe_path(f['path'])
                    if not re.fullmatch('[a-f0-9]{64}',f['sha256']) or type(f['size']) is not int or f['size']<0:
                        raise ValueError('Invalid file identity')
                if observed['state']!='unknown' and sha256(canonical(observed['files']))!=observed.get('inventory_sha256'):
                    raise ValueError('Inventory identity mismatch')
                report=observed
            except (ValueError,KeyError,TypeError):
                report['observation_failure']='invalid_remote_receipt'
        else:
            report['observation_failure']=failure or 'transport_failed'
        _write_new(folder/(observation+'-result.json'),report)
        return report

    def inspect_tree(self,receipt):
        """Inspect the original cached tree, without another GitHub download."""
        if (not isinstance(receipt,dict) or receipt.get('id')!=self.policy['operation_id']
                or receipt.get('state') not in {'finished','partial'}
                or receipt.get('location')!='hpc'
                or receipt.get('remote_directory')!=self.policy['remote_directory']+'/'+self.policy['operation_id']
                or not re.fullmatch('[a-f0-9]{40}',str(receipt.get('tree_sha1','')))):
            raise AcquisitionError('known_pinned_acquisition_tree_required')
        try:
            original=validate_request(dict(id=receipt['id'],repository=receipt['repository'],commit=receipt['commit'],
                directory=self.policy['remote_directory'],storage_bytes=self.policy['storage_bytes'],
                bundle_sha256=receipt['bundle_sha256']))
        except (KeyError,TypeError,ValueError):
            raise AcquisitionError('original_acquisition_identity_mismatch') from None
        if receipt.get('request_sha256')!=sha256(canonical(original)):
            raise AcquisitionError('original_acquisition_identity_mismatch')
        intent=dict(host_alias=self.policy['host_alias'],request=original)
        if self.transport_binding is not None:
            intent['transport_binding']=deepcopy(self.transport_binding)
        try:
            saved_intent=json.loads(read_regular(self.root/receipt['id']/'intent.json',1000000,private=True))
        except (OSError,ValueError,TypeError,ExecutionDenied):
            raise AcquisitionError('original_acquisition_intent_unavailable') from None
        if saved_intent!=intent:
            raise AcquisitionError('original_acquisition_intent_mismatch')
        request=dict(version=1,acquisition_request=original,receipt_sha256=sha256(canonical(receipt)),tree_sha1=receipt['tree_sha1'])
        bundle=tool_bundle()
        folder=private_directory(self.root/receipt['id']/'tree-inspections')
        identity=uuid.uuid4().hex
        _write_new(folder/(identity+'-intent.json'),dict(request=request,bundle_sha256=sha256(bundle),
            transport_binding=deepcopy(self.transport_binding)))
        command=_bound_transport_command(self.transport,self.transport_binding,self.policy['host_alias'],
            self.policy['remote_directory'],['python3','-c',INVENTORY_BOOTSTRAP])
        data=canonical(dict(request=request,bundle=base64.b64encode(bundle).decode(),bundle_sha256=sha256(bundle)))
        try:result=self.capture(command,timeout=60,max_bytes=2*1024**2,
            input_chunks=[data[i:i+65536] for i in range(0,len(data),65536)])
        except OSError as exc:
            result=dict(returncode=None,failure=type(exc).__name__,stdout='',stderr='')
        failure=_transport_failure(result)
        summary=dict(returncode=result.get('returncode'),failure=failure,raw_output_persisted=False)
        for key in ('stdout','stderr'):
            raw=str(result.get(key) or '').encode()
            summary[key+'_sha256']=sha256(raw);summary[key+'_bytes']=len(raw)
        _write_new(folder/(identity+'-transport.json'),summary)
        report=dict(version=1,state='unknown',request_sha256=sha256(canonical(request)),
            acquisition_id=receipt['id'],repository=receipt['repository'],commit=receipt['commit'],
            receipt_sha256=request['receipt_sha256'],tree_sha1=request['tree_sha1'],
            scope='reference_only',B_input=False,execution_authorized=False,
            physical_evaluation_performed=False,scientific_validation=False,entries=[],summary=None)
        if result.get('returncode')==0 and not failure:
            try:
                observed=json.loads(base64.b64decode(result['stdout'],validate=True))
                self.validate_tree_report(request,observed)
                report=observed
            except (ValueError,KeyError,TypeError):
                report['observation_failure']='invalid_cached_tree_report'
        else:report['observation_failure']=failure or 'transport_failed'
        _write_new(folder/(identity+'-result.json'),report)
        return report

    @staticmethod
    def validate_tree_report(request,report):
        from .author_resource_inventory_worker import (CACHE_KEYS,MAX_REPORT,STATUSES,
            classify_entries,inventory_summary,validate_inventory_request)
        validate_inventory_request(request)
        expected=dict(version=1,state='inspected',scope='reference_only',B_input=False,
            execution_authorized=False,physical_evaluation_performed=False,scientific_validation=False,
            request_sha256=sha256(canonical(request)),acquisition_id=request['acquisition_request']['id'],
            repository=request['acquisition_request']['repository'],commit=request['acquisition_request']['commit'],
            receipt_sha256=request['receipt_sha256'],tree_sha1=request['tree_sha1'])
        if (not isinstance(report,dict) or set(report)!=set(expected)|{'entries','summary'}
                or any(report.get(k)!=v or type(report.get(k)) is not type(v) for k,v in expected.items())
                or len(canonical(report))>MAX_REPORT):
            raise AcquisitionError('cached_tree_report_identity_mismatch')
        entries=report['entries'];details=report['summary']
        if (not isinstance(entries,list) or len(entries)>2000 or not isinstance(details,dict)
                or type(details.get('entry_count')) is not int or details['entry_count']!=len(entries)
                or details.get('cached_tree_only') is not True
                or details.get('file_contents_read') is not False or details.get('source_complete') is not False
                or details.get('execution_ready') is not False):
            raise AcquisitionError('cached_tree_report_boundaries_invalid')
        for row in entries:
            if (not isinstance(row,dict) or set(row)!={'path','type','mode','size','sha','status'}
                    or not isinstance(row['path'],str) or len(row['path'])>500
                    or not isinstance(row['status'],str) or row['status'] not in STATUSES
                    or (row['size'] is not None and (type(row['size']) is not int or row['size']<0))
                    or any(row[key] is not None and (not isinstance(row[key],str) or len(row[key])>100)
                           for key in ('type','mode','sha'))):
                raise AcquisitionError('cached_tree_member_metadata_invalid')
        if classify_entries(entries)!=entries:
            raise AcquisitionError('cached_tree_member_classification_mismatch')
        hashes=details.get('cache_sha256')
        if (not isinstance(hashes,dict) or set(hashes)!=set(CACHE_KEYS)
                or any(not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value)
                       for value in hashes.values())):
            raise AcquisitionError('cached_tree_cache_digest_invalid')
        if canonical(details)!=canonical(inventory_summary(entries,hashes)):
            raise AcquisitionError('cached_tree_member_counts_mismatch')
        return report
