"""Trusted candidate-to-execution bridge. Consumes authorization; never issues it."""
from dataclasses import asdict
import json
import time

from . import runtime_launcher as runtime
from .agent_candidates import CandidateError, research_inputs
from .batch_plan import render_batch
from .candidate_jobs import CandidateHistory
from .ledger import Conflict, Resources
from .manifest import Snapshot, canonical, private_directory, read_file, root_descriptor, sha256
from .submission import Submission


class ExistingAuthorization:
    """Private controller mirror of an already issued grant and approved batch.

    Pinned identities come from trusted deployment configuration. This verifies
    the binding/signature, not whether scientific or independent review occurred.
    The remote submission/runtime helpers still perform their own grant checks.
    """
    def __init__(self, directory, *, profile_sha256, reviewed_commit, approval_sha256,
                 scoring_sha256, static_check_sha256, software_sha256):
        self.directory=private_directory(directory)
        self.pins=dict(profile_sha256=profile_sha256,reviewed_commit=reviewed_commit,
                       approval_sha256=approval_sha256,scoring_sha256=scoring_sha256,
                       static_check_sha256=static_check_sha256)
        for key,value in self.pins.items():
            if key!='reviewed_commit':runtime.hash_value(value)
        import re
        if not isinstance(reviewed_commit,str) or not re.fullmatch(r'[a-f0-9]{40}',reviewed_commit):
            raise ValueError('Pin the actually reviewed source commit')
        self.software_sha256=runtime.hash_value(software_sha256)
        self.policy_sha256=sha256(canonical(dict(self.pins,software_sha256=self.software_sha256)))

    def verify(self, submission, snapshot, batch):
        manifest=snapshot.verify()
        if snapshot.digest!=submission.manifest_sha256:raise Conflict('Wrong authorized snapshot')
        envelope=json.loads(runtime.read_regular(self.directory/(submission.request_id+'.json'),16384,private=True))
        key=runtime.read_regular(self.directory/'grant.key',32,private=True)
        grant=runtime.verify_grant(envelope,key,request_id=submission.request_id,manifest_sha256=snapshot.digest,
                                   profile_sha256=self.pins['profile_sha256'],now=time.time())
        if any(grant.get(name)!=value for name,value in self.pins.items()):
            raise Conflict('Grant differs from pinned authorization evidence')
        if (grant.get('resources')!=asdict(submission.resources) or manifest['resources']!=grant['resources'] or
                grant.get('task_sha256')!=manifest['provenance']['task_sha256'] or
                manifest['provenance']['software_sha256']!=self.software_sha256):
            raise Conflict('Grant task, resources or software mismatch')
        with root_descriptor(snapshot.path) as root:
            record=next(item for item in manifest['files'] if item['path']=='analysis.json')
            raw=read_file(root,'analysis.json',record['size'])
        if sha256(raw)!=manifest['provenance']['analysis_sha256']:raise Conflict('Wrong frozen analysis')
        outputs=json.loads(raw)['outputs']
        if not isinstance(grant.get('outputs'),list) or sorted(grant['outputs'])!=sorted(outputs):
            raise Conflict('Grant outputs differ from frozen plan')
        script=runtime.read_regular(self.directory/(submission.request_id+'.sh'),1000000,private=True)
        if script!=batch.script or grant.get('batch_sha256')!=batch.sha256:
            raise Conflict('Resource script is not the authorized batch')
        # Same payload digest recorded by the compute-node execution intent.
        return sha256(canonical(grant))


class CandidateExecution:
    def __init__(self, tasks, ledger, snapshots, staging, submission, following, authorization, environment,
                 *, runtime_profile_path=None):
        self.tasks,self.ledger=tasks,ledger
        self.history=CandidateHistory(tasks)
        self.snapshots=private_directory(snapshots)
        self.staging,self.submission,self.following=staging,submission,following
        self.authorization,self.environment=authorization,environment
        self.runtime_profile_path=runtime_profile_path
        if any(service.ledger.path!=ledger.path for service in (staging,submission,following)):
            raise ValueError('Execution stages must share the same ledger')
        endpoints=(staging.client.endpoint,submission.scheduler.endpoint,following.analysis.collector.endpoint)
        if len({(e.host_alias,e.root_path,e.python_path) for e in endpoints})!=1:
            raise ValueError('Execution stages must use the same approved deployment')
        collector=endpoints[2]
        if (environment.root_path!=collector.root_path or environment.python_path!=collector.python_path or
                environment.launcher_path!=collector.helper_path or environment.launcher_sha256!=collector.helper_sha256 or
                following.snapshots!=self.snapshots):
            raise ValueError('Batch, snapshots and result collection must use the same deployment')

    def prepare(self, task_id, evaluation):
        """Reserve once and render a reviewable plan; no model/network/physics call."""
        task=self.tasks.get(task_id)
        inputs=research_inputs(self.tasks,task_id,task['revision'])
        job=self.history.get(task_id)
        if not job or job['state']!='prepared' or job['condition_sha256']!=inputs['condition_record_sha256']:
            raise CandidateError('No matching prepared candidate for the frozen task')
        digest=runtime.hash_value(job['result']['snapshot_sha256'])
        snapshot=Snapshot(self.snapshots/digest,digest);manifest=snapshot.verify()
        with root_descriptor(snapshot.path) as root:
            record=next(item for item in manifest['files'] if item['path']=='generation.json')
            generation=json.loads(read_file(root,'generation.json',record['size']))
        context=generation['input']
        if (context['condition_record_sha256']!=inputs['condition_record_sha256'] or
                sha256(canonical(context))!=manifest['provenance']['task_sha256'] or
                generation['request_id']!=job['result']['request_id']):
            raise Conflict('Generation does not belong to these frozen conditions')
        identity=self.ledger.evaluation_snapshot(evaluation)['identity']
        if identity['role']!='agent' or identity['task']!=inputs['condition_record_sha256']:
            raise Conflict('Evaluation must pre-register this research task as agent work')
        resources=Resources(**manifest['resources'])
        if resources.wall_seconds%60 or resources.memory_bytes%(1024*1024):
            raise CandidateError('Candidate resources require whole-minute time and whole-MiB memory')
        profile={}
        if self.runtime_profile_path is not None:
            raw=runtime.read_regular(self.runtime_profile_path,1000000,private=True)
            if sha256(raw)!=self.authorization.pins['profile_sha256']:
                raise Conflict('Runtime capacity differs from the pinned deployment profile')
            profile=json.loads(raw)
        runtime.validate_parallelism(profile,resources.cores)
        # The key is owned by the controller; callers cannot rename an attempt.
        key='candidate_'+job['id']
        row=self.ledger.reserve(evaluation,key,digest,resources)
        request=Submission(row['id'],digest,resources)
        return dict(row=row,key=key,snapshot=snapshot,submission=request,batch=render_batch(request,self.environment))

    def advance(self, task_id, evaluation):
        plan=self.prepare(task_id,evaluation);row=plan['row'];request_id=row['id']
        if row['dispatch_claimed']:
            if row['state']=='rejected':return dict(request_id=request_id,state='rejected',scientific_status='not_evaluated')
            # An expired grant cannot authorize a new dispatch, but must not
            # prevent recovering an already dispatched request.
            return self.following.advance(request_id)
        if row['state']!='prepared':
            return dict(request_id=request_id,state='attention',reason='request_not_prepared',scientific_status='not_evaluated')
        self.authorization.verify(plan['submission'],plan['snapshot'],plan['batch'])
        result=self.staging.stage(request_id,plan['snapshot'])
        if result['upload_state']!='staged':
            return dict(request_id=request_id,state='attention',reason='upload_unresolved',scientific_status='not_evaluated')
        # Recheck expiry and exact bytes after the potentially slow upload.
        grant_sha256=self.authorization.verify(plan['submission'],plan['snapshot'],plan['batch'])
        self.ledger.bind_execution_authorization(request_id,grant_sha256,plan['batch'].sha256,self.authorization.policy_sha256)
        row=self.submission.submit(evaluation,plan['key'],plan['snapshot'].digest,plan['submission'].resources)
        if row['state']=='rejected':return dict(request_id=request_id,state='rejected',scientific_status='not_evaluated')
        return self.following.advance(request_id)

    def run(self, task_id, evaluation, *, stop=None, notify=None):
        if stop is not None and stop.is_set():return dict(state='stopped',scientific_status='not_evaluated')
        first=self.advance(task_id,evaluation)
        if notify:notify(first)
        if first['state']!='waiting':return first
        return self.following.run(first['request_id'],stop=stop,notify=notify)
