"""Reference controller: ship trusted tools, return metadata, keep source on HPC."""
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import shlex
import uuid
import zipfile

from .manifest import canonical, private_directory, sha256
from .potential_acquisition import AcquisitionError, safe_path
from .reference_resource_worker import validate_request
from .slurm_read import _capture, _write_new

MODULES = ('__init__','reference_resource_worker','engine_source_worker','potential_acquisition',
           'potentials','source_discovery','papers','tasks','manifest','ledger','slurm_read')
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


class RemoteResources:
    def __init__(self,directory,policy,*,capture=_capture):
        if (not isinstance(policy,dict) or set(policy)!={'host_alias','remote_directory','operation_id','storage_bytes'}
                or not isinstance(policy['host_alias'],str)
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',policy['host_alias'])):
            raise AcquisitionError('explicit_HPC_resource_policy_required')
        self.root=private_directory(directory);self.policy=policy;self.capture=capture

    def acquire(self,repository,commit):
        bundle=tool_bundle();policy=self.policy
        request=validate_request(dict(id=policy['operation_id'],repository=repository,commit=commit,
            directory=policy['remote_directory'],storage_bytes=policy['storage_bytes'],bundle_sha256=sha256(bundle)))
        intent=dict(host_alias=policy['host_alias'],request=request)
        folder=self.root/request['id'];folder.mkdir(mode=0o700,exist_ok=True)
        try:
            _write_new(folder/'intent.json',intent)
        except FileExistsError:
            if (folder/'intent.json').is_symlink() or json.loads((folder/'intent.json').read_text())!=intent:
                raise AcquisitionError('preparation_identity_already_bound')
        observation=uuid.uuid4().hex
        _write_new(folder/(observation+'-start.json'),dict(at=datetime.now(timezone.utc).isoformat()))
        data=canonical(dict(request=request,bundle=base64.b64encode(bundle).decode()))
        command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
                 '-o','ClearAllForwardings=yes','-o','PermitLocalCommand=no','-o','RemoteCommand=none','-T',
                 policy['host_alias'],shlex.join(['python3','-c',BOOTSTRAP])]
        try:
            result=self.capture(command,timeout=240,max_bytes=750000,
                input_chunks=[data[i:i+65536] for i in range(0,len(data),65536)])
        except OSError as exc:
            result=dict(returncode=None,failure=type(exc).__name__,stdout='',stderr='')
        _write_new(folder/(observation+'-transport.json'),result)
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
            report['observation_failure']=result.get('failure') or 'transport_failed'
        _write_new(folder/(observation+'-result.json'),report)
        return report
