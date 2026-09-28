"""Typed, accounted engine builds. No LAMMPS input is fabricated or executed."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import time

from . import runtime_launcher as runtime
from .batch_plan import render_batch
from .engine_source_worker import validate_requirements
from .ledger import Conflict, Resources
from .manifest import canonical, private_directory, read_file, root_descriptor, sha256
from .slurm_read import _write_new
from .staging import absolute_path
from .submission import Submission


def validate_spec(spec):
    keys={'kind','source_directory','source_result_sha256','source_inventory_sha256','source_commit','requirements','tools'}
    if not isinstance(spec,dict) or set(spec)!=keys or spec['kind']!='engine_build':
        raise ValueError('A typed engine build specification is required')
    absolute_path(spec['source_directory'])
    for name in ('source_result_sha256','source_inventory_sha256'):
        runtime.hash_value(spec[name])
    if not isinstance(spec['source_commit'],str) or not re.fullmatch('[a-f0-9]{40}',spec['source_commit']):
        raise ValueError('Pinned source commit required')
    validate_requirements(spec['requirements'])
    if not isinstance(spec['tools'],dict) or set(spec['tools'])!={'cmake','compiler','mpi_compiler','make'}:
        raise ValueError('Explicit build toolchain required')
    for item in spec['tools'].values():
        if not isinstance(item,dict) or set(item)!={'path','sha256'}:
            raise ValueError('Pinned tool identity required')
        runtime.absolute(item['path']);runtime.hash_value(item['sha256'])
    return json.loads(canonical(spec))


def build_manifest(spec,resources):
    spec=validate_spec(spec)
    if (resources.cores!=spec['requirements']['cores'] or resources.wall_seconds%60
            or resources.memory_bytes%(1024**2) or resources.cores>8 or resources.wall_seconds>1800
            or resources.memory_bytes>8*1024**3 or not 16*1024**2<=resources.storage_bytes<=16*1024**3):
        raise ValueError('Build resources must match bounded requirements and exact Slurm units')
    data=canonical(spec)
    return dict(schema_version=1,kind='engine_build',resources=asdict(resources),
                provenance={'task_sha256':sha256(data)},
                files=[dict(path='build.json',role='engine_build_spec',size=len(data),sha256=sha256(data))])


@dataclass(frozen=True)
class BuildSnapshot:
    path: Path
    digest: str

    def verify(self):
        runtime.hash_value(self.digest)
        with root_descriptor(self.path) as root:
            manifest=read_file(root,'manifest.json',100000)
            spec=read_file(root,'build.json',100000)
        if sha256(manifest)!=self.digest:
            raise ValueError('Build manifest changed')
        document=json.loads(manifest)
        expected=build_manifest(json.loads(spec),Resources(**document['resources']))
        if expected!=document or sha256(spec)!=document['files'][0]['sha256']:
            raise ValueError('Build specification changed')
        if {p.name for p in self.path.iterdir()}!={'manifest.json','build.json'}:
            raise ValueError('Unexpected build snapshot files')
        return document


def freeze_build(directory,spec,resources):
    root=private_directory(directory);manifest=build_manifest(spec,resources)
    digest=sha256(canonical(manifest));folder=root/digest
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        snapshot=BuildSnapshot(folder,digest);snapshot.verify();return snapshot
    _write_new(folder/'build.json',spec);_write_new(folder/'manifest.json',manifest)
    snapshot=BuildSnapshot(folder,digest);snapshot.verify();return snapshot


class BuildAuthorization:
    """Verify an existing reviewed grant; never creates or approves one."""
    def __init__(self,directory,*,profile_sha256,reviewed_commit,approval_sha256):
        self.directory=private_directory(directory)
        runtime.hash_value(profile_sha256);runtime.hash_value(approval_sha256)
        if not re.fullmatch('[a-f0-9]{40}',reviewed_commit):raise ValueError('Reviewed code commit required')
        self.pins=dict(profile_sha256=profile_sha256,reviewed_commit=reviewed_commit,approval_sha256=approval_sha256)
        self.policy_sha256=sha256(canonical(self.pins))

    def verify(self,submission,snapshot,batch):
        manifest=snapshot.verify()
        spec=json.loads((snapshot.path/'build.json').read_text())
        key=runtime.read_regular(self.directory/'grant.key',32,private=True)
        envelope=json.loads(runtime.read_regular(self.directory/(submission.request_id+'.json'),16384,private=True))
        grant=runtime.verify_grant(envelope,key,request_id=submission.request_id,manifest_sha256=snapshot.digest,
            profile_sha256=self.pins['profile_sha256'],now=time.time())
        if (any(grant.get(k)!=v for k,v in self.pins.items()) or grant.get('purpose')!='engine_build'
                or grant.get('resources')!=asdict(submission.resources) or manifest['resources']!=grant['resources']
                or grant.get('task_sha256')!=manifest['provenance']['task_sha256']
                or grant.get('scoring_sha256')!=sha256(canonical(spec['requirements']))
                or grant.get('static_check_sha256')!=spec['source_inventory_sha256']
                or grant.get('batch_sha256')!=batch.sha256 or submission.manifest_sha256!=snapshot.digest):
            raise Conflict('Build grant differs from reviewed plan')
        if runtime.read_regular(self.directory/(submission.request_id+'.sh'),1000000,private=True)!=batch.script:
            raise Conflict('Build batch differs from approved bytes')
        return sha256(canonical(grant))


class EngineBuildExecution:
    def __init__(self,ledger,snapshots,staging,submission,authorization,environment):
        self.ledger=ledger;self.snapshots=private_directory(snapshots)
        self.staging,self.submission,self.authorization,self.environment=staging,submission,authorization,environment
        if staging.ledger.path!=ledger.path or submission.ledger.path!=ledger.path:
            raise ValueError('Build stages must share the campaign ledger')
        endpoints=(staging.client.endpoint,submission.scheduler.endpoint)
        if len({(e.host_alias,e.python_path,e.root_path) for e in endpoints})!=1:
            raise ValueError('Build stages must share one HPC deployment')
        if environment.root_path!=endpoints[0].root_path or environment.python_path!=endpoints[0].python_path:
            raise ValueError('Build batch must use the same HPC deployment')
        if (environment.launcher_path!=endpoints[0].helper_path or
                environment.launcher_sha256!=endpoints[0].helper_sha256):
            raise ValueError('Build stage and compute worker must be the same reviewed helper')

    def prepare(self,evaluation,spec,resources):
        snapshot=freeze_build(self.snapshots,spec,resources)
        task=sha256(canonical(spec));identity=self.ledger.evaluation_snapshot(evaluation)['identity']
        if identity['role']!='development' or identity['task']!=task:
            raise Conflict('Engine builds require a registered development task')
        key='build_'+snapshot.digest
        row=self.ledger.reserve(evaluation,key,snapshot.digest,resources)
        submission=Submission(row['id'],snapshot.digest,resources)
        return dict(snapshot=snapshot,row=row,key=key,submission=submission,batch=render_batch(submission,self.environment))

    def advance(self,evaluation,spec,resources):
        plan=self.prepare(evaluation,spec,resources);row=plan['row'];request=plan['submission']
        if row['dispatch_claimed'] or row['state']!='prepared':
            return row  # Existing reconciliation service observes it; never resubmit.
        self.authorization.verify(request,plan['snapshot'],plan['batch'])
        staged=self.staging.stage(request.request_id,plan['snapshot'])
        if staged['upload_state']!='staged':return self.ledger.get(request.request_id)
        proof=self.authorization.verify(request,plan['snapshot'],plan['batch'])
        self.ledger.bind_execution_authorization(request.request_id,proof,plan['batch'].sha256,self.authorization.policy_sha256)
        return self.submission.submit(evaluation,plan['key'],request.manifest_sha256,resources)
