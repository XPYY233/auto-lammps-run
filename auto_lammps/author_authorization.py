"""Explicit user-approved author grants through the existing delivery service.

The original author package is reference-only. This module does not create an
agent candidate, modify an author input, synthesize a user approval or submit a
job. Scientific validity and formal isolation are not established by these static
metadata checks. Signing keys are existing controller configuration only.
"""
import base64
from dataclasses import asdict
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import time

from . import runtime_launcher as runtime
from .author_execution import _deployment, _resources, _source_files
from .authorization import AutomaticAuthorization
from .execution import ExistingAuthorization
from .hpc_transport import bind_request, transport_identity
from .ledger import Conflict
from .manifest import canonical, private_directory, sha256, validate_provenance
from .remote_submit import private_bytes_once
from .tasks import task_id as validate_task_id


ENVELOPE_LIMIT = 1024**2
BUNDLE_LIMIT = 13*1024**2//10


class AuthorAuthorization(AutomaticAuthorization):
    """Reference-specific checks; immutable delivery is the shared implementation."""
    def __init__(self, directory, *, tasks, task_id, ledger, endpoint, audit_directory,
                 profile_sha256, reviewed_commit, approval_sha256, scoring_sha256,
                 software_sha256, lifetime_seconds, transport=None):
        validate_task_id(task_id); tasks.get(task_id)
        base=ExistingAuthorization(directory,profile_sha256=profile_sha256,reviewed_commit=reviewed_commit,
            approval_sha256=approval_sha256,scoring_sha256=scoring_sha256,software_sha256=software_sha256,
            static_check_sha256='0'*64)
        if type(lifetime_seconds) is not int or not 60<=lifetime_seconds<=30*86400:
            raise ValueError('Explicit author grant lifetime required')
        self.tasks,self.task_id,self.ledger,self.endpoint=tasks,task_id,ledger,endpoint
        self.directory,self.transport=base.directory,transport
        self.audit_directory=private_directory(audit_directory)
        self.pins={key:value for key,value in base.pins.items() if key!='static_check_sha256'}
        self.software_sha256=software_sha256;self.lifetime_seconds=lifetime_seconds
        self.policy_sha256=sha256(canonical(dict(schema_version=1,role='author_reference',
            pins=self.pins,software_sha256=software_sha256,lifetime_seconds=lifetime_seconds,
            task_id=task_id,task_store=str(tasks.path.absolute()),ledger=str(ledger.path.absolute()),
            directory=str(self.directory),endpoint=asdict(endpoint),transport=transport_identity(self),
            sources={name:sha256(Path(__file__).with_name(name).read_bytes()) for name in
                ('author_authorization.py','author_execution.py','authorization.py','remote_submit.py','runtime_launcher.py')})))

    def _approval(self, digest):
        scope='author:'+digest
        if not self.tasks.plan_approved(self.task_id,scope):
            raise Conflict('The user has not approved this exact original author package')
        rows=[row for row in self.tasks.approvals(self.task_id) if row['scope']==scope]
        if not rows:
            raise Conflict('Original author approval receipt is unavailable')
        # This is a digest OF the first real append-only user approval row. It is
        # never a newly invented approval based only on a package or condition.
        receipt=dict(task_id=self.task_id,**rows[0])
        return receipt,sha256(canonical(receipt))

    def _check(self,submission,snapshot,batch,*,undispatched=False):
        row=self.ledger.get(submission.request_id)
        if undispatched and (row['state']!='prepared' or row['dispatch_claimed']):
            raise Conflict('Only an existing undispatched reference reservation can receive a grant')
        if (row['manifest_sha256']!=snapshot.digest or submission.manifest_sha256!=snapshot.digest
                or json.loads(row['resources'])!=asdict(submission.resources)):
            raise Conflict('Original author reservation, snapshot or resource identity changed')
        package=snapshot.verify()
        raw=runtime.read_regular(snapshot.path/'author-package.json',1_000_000,private=True)
        if sha256(raw)!=snapshot.digest or canonical(package)!=raw:
            raise Conflict('Original author package bytes differ from the frozen snapshot')
        expected_keys={'schema_version','kind','role','scope','B_input','original_files_rewritten',
            'plan_sha256','source_binding_sha256','reference_scope','resources','provenance',
            'deployment','files','invocations','outputs'}
        if (not isinstance(package,dict) or set(package)!=expected_keys
                or type(package['schema_version']) is not int or package['schema_version']!=1
                or package['kind']!='original_author_execution' or package['role']!='author_reference'
                or package['scope']!='reference_only' or package['B_input'] is not False
                or package['original_files_rewritten'] is not False):
            raise Conflict('An original reference package cannot be an agent candidate')
        resources=_resources(package['resources']);validate_provenance(package['provenance'])
        if resources!=asdict(submission.resources) or package['provenance']['software_sha256']!=self.software_sha256:
            raise Conflict('Original author resources or software differ from the approved deployment')
        evaluation=self.ledger.evaluation_snapshot(row['evaluation'])
        identity=evaluation['identity']
        if (identity['role']!='reference' or identity['task']!=package['provenance']['task_sha256']
                or identity['system']!=self.software_sha256):
            raise Conflict('Author grants require the original registered reference identity and software')
        task=self.tasks.get(self.task_id)
        if task['mode']!='reproduction':
            raise Conflict('Original author grants belong only to a paper reference task')
        if undispatched:
            life=self.tasks.lifecycle(self.task_id)
            if self.tasks.paused(self.task_id) or life['deleted'] or life['user_finished']:
                raise Conflict('The paper reference task is paused or finished')
        deployment=package['deployment'];roots,registry=_deployment(deployment,resources)
        if (deployment['profile_sha256']!=self.pins['profile_sha256']
                or deployment['approval_sha256']!=self.pins['approval_sha256']):
            raise Conflict('Author deployment differs from the pinned runtime or deployment approval')
        request_root=PurePosixPath(self.endpoint.root_path)
        for path in roots.values():
            source=PurePosixPath(path)
            if source==request_root or source in request_root.parents or request_root in source.parents:
                raise Conflict('Original author source must remain outside request storage')
        files=_source_files(package['files'])
        if any(row['binding_sha256'] not in roots for row in files.values()):
            raise Conflict('Original author file source is not registered')
        scope=package['reference_scope']
        if (not isinstance(scope,dict) or set(scope)!={'id','receipt_sha256','scope_sha256','scope'}
                or scope['scope_sha256']!=sha256(canonical(scope['scope']))):
            raise Conflict('Original author P-scope binding changed')
        runtime.hash_value(scope['receipt_sha256']);runtime.hash_value(package['plan_sha256'])
        runtime.hash_value(package['source_binding_sha256'])
        dependencies=scope['scope']['dependency_ids']
        if (not isinstance(dependencies,list) or not dependencies or len(set(dependencies))!=len(dependencies)):
            raise Conflict('Original author scope must retain its selected dependencies')
        calls=package['invocations']
        if not isinstance(calls,list) or not 1<=len(calls)<=128:
            raise Conflict('A bounded complete original author invocation list is required')
        seen,signatures,outputs=set(),set(),{}
        for call in calls:
            if not isinstance(call,dict) or set(call)!={'id','entrypoint','argv','working_directory',
                    'environment_id','executable_id','executable_sha256','dependency_ids','required_files','output_files'}:
                raise Conflict('Original author invocation fields changed')
            if call['id'] in seen:raise Conflict('Repeated original author action identity')
            seen.add(call['id'])
            environment=registry.get(call['environment_id'])
            program=next((row for row in environment['executables'] if row['id']==call['executable_id']),None) if environment else None
            if program is None or program['sha256']!=call['executable_sha256'] or call['entrypoint'] not in program['entrypoints']:
                raise Conflict('Original author entrypoint or executable is not in the fixed deployment')
            runtime.validate_author_argv(program['kind'],call['argv'],working_directory=call['working_directory'],
                entrypoint=call['entrypoint']['path'])
            required=call['required_files']
            if (not isinstance(required,list) or not required or call['entrypoint'] not in required
                    or len({row['path'] for row in required})!=len(required)
                    or any(set(row)!={'path','sha256'} or row['path'] not in files
                        or files[row['path']]['sha256']!=row['sha256'] for row in required)):
                raise Conflict('Original author required input bytes are incomplete')
            if (not isinstance(call['dependency_ids'],list) or not call['dependency_ids']
                    or len(set(call['dependency_ids']))!=len(call['dependency_ids'])
                    or set(call['dependency_ids'])-set(dependencies)):
                raise Conflict('An original action cannot add computations outside the selected P scope')
            signature=sha256(canonical(dict(executable=program['path'],argv=call['argv'],cwd=call['working_directory'])))
            if signature in signatures:raise Conflict('Duplicate original physical actions must share one evaluation')
            signatures.add(signature)
            for output in call['output_files']:
                if not isinstance(output,dict) or set(output)!={'path','kind'}:
                    raise Conflict('Original author output declaration changed')
                runtime.relative(output['path'])
                if output['kind'] not in {'numeric_table','trajectory','log','other'}:
                    raise Conflict('Unsupported original output kind')
                if output['path'] in outputs and outputs[output['path']]!=output:
                    raise Conflict('Conflicting original output declarations')
                outputs[output['path']]=output
        if sorted(outputs.values(),key=lambda row:row['path'])!=package['outputs']:
            raise Conflict('Original author collection list differs from its fixed invocations')
        declared=list(dict.fromkeys(['stdout.txt','stderr.txt',*[row['path'] for row in package['outputs']]]))
        runtime.validate_author_outputs(declared,package,input_bytes=len(raw)+sum(row['size'] for row in files.values()))
        if not isinstance(batch.script,bytes) or sha256(batch.script)!=batch.sha256:
            raise Conflict('Original author batch bytes changed')
        approval,approval_sha=self._approval(snapshot.digest)
        report=dict(schema_version=1,role='author_reference',task_id=self.task_id,evaluation=row['evaluation'],
            manifest_sha256=snapshot.digest,plan_sha256=package['plan_sha256'],
            scope_sha256=scope['scope_sha256'],source_binding_sha256=package['source_binding_sha256'],
            input_inventory_sha256=sha256(canonical(package['files'])),output_inventory_sha256=sha256(canonical(package['outputs'])),
            invocation_list_sha256=sha256(canonical(calls)),invocation_count=len(calls),
            resources_sha256=sha256(canonical(resources)),user_plan_approval=approval,
            user_plan_approval_sha256=approval_sha,validation='original_author_metadata_binding_only',
            physical_evaluation_performed=False,scientific_status='not_evaluated',formal_isolation=False)
        return package,report,declared

    def _verify_envelope(self,envelope,submission,snapshot,batch,package,report,outputs):
        key=runtime.read_regular(self.directory/'grant.key',32,private=True)
        payload=runtime.verify_grant(envelope,key,request_id=submission.request_id,manifest_sha256=snapshot.digest,
            profile_sha256=self.pins['profile_sha256'],now=time.time())
        expected=dict(**self.pins,role='author_reference',author_package_sha256=snapshot.digest,
            author_task_id=self.task_id,reference_evaluation=report['evaluation'],
            task_sha256=package['provenance']['task_sha256'],resources=asdict(submission.resources),
            outputs=outputs,batch_sha256=batch.sha256,authorization_policy_sha256=self.policy_sha256,
            static_check_sha256=sha256(canonical(report)),user_plan_approval_sha256=report['user_plan_approval_sha256'],
            execution_scope='trusted_research',formal_isolation=False,scientific_status='not_evaluated')
        if any(payload.get(key)!=value for key,value in expected.items()):
            raise Conflict('Author grant differs from its policy, approval, original package or resources')
        return sha256(canonical(payload))

    def verify(self,submission,snapshot,batch):
        package,report,outputs=self._check(submission,snapshot,batch)
        envelope=json.loads(runtime.read_regular(self.directory/(submission.request_id+'.json'),ENVELOPE_LIMIT,private=True))
        checksum=self._verify_envelope(envelope,submission,snapshot,batch,package,report,outputs)
        script=runtime.read_regular(self.directory/(submission.request_id+'.sh'),1_000_000,private=True)
        if script!=batch.script:raise Conflict('The local author batch differs from the issued grant')
        return checksum

    def ensure(self,submission,snapshot,batch):
        package,report,outputs=self._check(submission,snapshot,batch,undispatched=True)
        bind_request(self.ledger,submission.request_id,self)
        bundle_path=self.directory/(submission.request_id+'.bundle.json')
        if not bundle_path.exists():
            payload=dict(request_id=submission.request_id,manifest_sha256=snapshot.digest,**self.pins,
                role='author_reference',author_package_sha256=snapshot.digest,author_task_id=self.task_id,
                reference_evaluation=report['evaluation'],static_check_sha256=sha256(canonical(report)),
                user_plan_approval_sha256=report['user_plan_approval_sha256'],expires_at=int(time.time())+self.lifetime_seconds,
                task_sha256=package['provenance']['task_sha256'],resources=asdict(submission.resources),outputs=outputs,
                batch_sha256=batch.sha256,authorization_policy_sha256=self.policy_sha256,
                execution_scope='trusted_research',formal_isolation=False,scientific_status='not_evaluated')
            key=runtime.read_regular(self.directory/'grant.key',32,private=True)
            envelope=dict(payload=payload,hmac_sha256=hmac.new(key,canonical(payload),hashlib.sha256).hexdigest())
            if len(canonical(envelope))>ENVELOPE_LIMIT:raise Conflict('Author grant exceeds its bounded metadata capacity')
            bundle=dict(envelope=envelope,batch_base64=base64.b64encode(batch.script).decode(),report=report)
            if len(canonical(bundle))>BUNDLE_LIMIT:raise Conflict('Author authorization bundle exceeds its bounded metadata capacity')
            try:runtime.write_once(bundle_path,bundle,max_bytes=BUNDLE_LIMIT)
            except FileExistsError:pass
        bundle=json.loads(runtime.read_regular(bundle_path,BUNDLE_LIMIT,private=True))
        if (not isinstance(bundle,dict) or set(bundle)!={'envelope','batch_base64','report'}
                or bundle['report']!=report or base64.b64decode(bundle['batch_base64'],validate=True)!=batch.script
                or len(canonical(bundle['envelope']))>ENVELOPE_LIMIT):
            raise Conflict('Saved author bundle, first user approval or original batch changed')
        self._verify_envelope(bundle['envelope'],submission,snapshot,batch,package,report,outputs)
        private_bytes_once(self.directory/(submission.request_id+'.sh'),batch.script)
        private_bytes_once(self.directory/(submission.request_id+'.json'),canonical(bundle['envelope']))
        checksum=self.verify(submission,snapshot,batch)
        return self._deliver(submission,snapshot,batch,bundle,checksum,author_reference=True)
