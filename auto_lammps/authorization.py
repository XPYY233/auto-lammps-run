"""Controller-owned automatic grants under an existing approved deployment policy.

No approval/review evidence is invented. Signing authorizes one bounded execution;
static input validation is not scientific correctness or formal isolation proof.
"""
import base64
from dataclasses import asdict
import hashlib
import hmac
import json
from pathlib import Path
import shlex
import time
import uuid

from . import runtime_launcher as runtime
from .agent_candidates import validate_proposal, RESERVED_OUTPUTS, render_candidate_script
from .analysis_v2 import plan_adapter
from .execution import ExistingAuthorization
from .hpc_transport import bind_request, transport_identity
from .ledger import Conflict
from .manifest import canonical, private_directory, read_file, root_descriptor, sha256
from .remote_submit import private_bytes_once
from .slurm_read import _capture, _write_new
from .staging import BOOTSTRAP


def candidate_check(snapshot, *, max_atoms):
    manifest=snapshot.verify()
    with root_descriptor(snapshot.path) as root:
        def load(name):
            record=next(item for item in manifest['files'] if item['path']==name)
            return read_file(root,name,record['size'])
        generation=json.loads(load('generation.json'));context=generation['input']
        analysis_raw=load('analysis.json');analysis=json.loads(analysis_raw)
        proposal=generation['proposal'];layout=context.get('output_layout','isolated')
        screen=validate_proposal(proposal,max_atoms=max_atoms,output_layout=layout,require_analysis_plan=True)
        if screen is None or screen!=generation['script_screen']:
            raise Conflict('Frozen candidate does not pass the current static screen')
        if (sha256(canonical(context))!=manifest['provenance']['task_sha256'] or
                sha256(analysis_raw)!=manifest['provenance']['analysis_sha256'] or
                analysis['proposal']!=proposal['analysis'] or analysis['implementation_status']=='not_implemented'):
            raise Conflict('Candidate provenance or supported analysis is incomplete')
        implementation,adapter=plan_adapter(proposal['analysis']['plan'])
        if (analysis['implementation_status']!=implementation or analysis['adapter_identity']!=adapter
                or analysis['outputs']!=sorted(RESERVED_OUTPUTS)+proposal['analysis']['files']
                or context['resources']!=manifest['resources']
                or generation['request_id']!=sha256(canonical(context))[:32]):
            raise Conflict('Frozen analysis, model identity or resources changed')
        expected=render_candidate_script(proposal,generation['potential_receipt']['units'],generation['potential_receipt']['commands'])
        if load(manifest['entrypoint'])!=expected:
            raise Conflict('Input differs from the frozen adapter and model workflow')
    return dict(schema_version=1,manifest_sha256=snapshot.digest,condition_sha256=context['condition_record_sha256'],
        output_layout=layout,screen=screen,analysis_sha256=sha256(analysis_raw),outputs=analysis['outputs'],
        validation='static_input_contract_only',scientific_status='not_evaluated')


class AutomaticAuthorization:
    def __init__(self, directory, *, ledger, endpoint, audit_directory, profile_sha256,
                 reviewed_commit, approval_sha256, scoring_sha256, software_sha256,
                 max_atoms, lifetime_seconds, transport=None):
        # Existing validator checks configured evidence identities; it does not
        # establish whether a maintainer actually approved/reviewed the release.
        base=ExistingAuthorization(directory,profile_sha256=profile_sha256,reviewed_commit=reviewed_commit,
            approval_sha256=approval_sha256,scoring_sha256=scoring_sha256,software_sha256=software_sha256,
            static_check_sha256='0'*64)
        if type(max_atoms) is not int or not 1<=max_atoms<=1000000:raise ValueError('Invalid geometry bound')
        if type(lifetime_seconds) is not int or not 60<=lifetime_seconds<=30*86400:raise ValueError('Explicit grant lifetime required')
        self.directory=base.directory;self.ledger=ledger;self.endpoint=endpoint;self.transport=transport
        self.audit_directory=private_directory(audit_directory)
        self.max_atoms=max_atoms;self.lifetime_seconds=lifetime_seconds;self.software_sha256=software_sha256
        self.pins={k:v for k,v in base.pins.items() if k!='static_check_sha256'}
        self.policy_sha256=sha256(canonical(dict(pins=self.pins,software=software_sha256,max_atoms=max_atoms,
            lifetime_seconds=lifetime_seconds,directory=str(self.directory),endpoint=asdict(endpoint),
            transport=transport_identity(self),sources={name:sha256(Path(__file__).with_name(name).read_bytes())
                for name in ('authorization.py','agent_candidates.py','remote_submit.py')})))

    def _validator(self,report):
        return ExistingAuthorization(self.directory,**self.pins,software_sha256=self.software_sha256,
                                     static_check_sha256=sha256(canonical(report)))

    def verify(self,submission,snapshot,batch):
        report=candidate_check(snapshot,max_atoms=self.max_atoms)
        envelope=json.loads(runtime.read_regular(self.directory/(submission.request_id+'.json'),16384,private=True))
        if envelope['payload'].get('authorization_policy_sha256')!=self.policy_sha256:
            raise Conflict('Execution grant belongs to another automatic policy')
        return self._validator(report).verify(submission,snapshot,batch)

    def ensure(self,submission,snapshot,batch):
        row=self.ledger.get(submission.request_id)
        if (row['state']!='prepared' or row['dispatch_claimed'] or row['manifest_sha256']!=snapshot.digest
                or json.loads(row['resources'])!=asdict(submission.resources)):
            raise Conflict('Only an existing undispatched reservation can receive authorization')
        evaluation=self.ledger.evaluation_snapshot(row['evaluation'])
        report=candidate_check(snapshot,max_atoms=self.max_atoms)
        if (evaluation['identity']['role']!='agent' or evaluation['max_attempts']!=2
                or evaluation['identity']['task']!=report['condition_sha256']):
            raise Conflict('Automatic grants require the registered research task identity')
        manifest=snapshot.verify()
        if manifest['provenance']['software_sha256']!=self.software_sha256:
            raise Conflict('Candidate software differs from approved deployment')
        bind_request(self.ledger,submission.request_id,self)
        identifier=submission.request_id;bundle_path=self.directory/(identifier+'.bundle.json')
        if not bundle_path.exists():
            payload=dict(request_id=identifier,manifest_sha256=snapshot.digest,**self.pins,
                static_check_sha256=sha256(canonical(report)),expires_at=int(time.time())+self.lifetime_seconds,
                task_sha256=manifest['provenance']['task_sha256'],resources=asdict(submission.resources),
                outputs=report['outputs'],batch_sha256=batch.sha256,authorization_policy_sha256=self.policy_sha256,
                scientific_status='not_evaluated')
            if report['output_layout']=='working_directory':payload.update(execution_scope='trusted_research',formal_isolation=False)
            key=runtime.read_regular(self.directory/'grant.key',32,private=True)
            envelope=dict(payload=payload,hmac_sha256=hmac.new(key,canonical(payload),hashlib.sha256).hexdigest())
            bundle=dict(envelope=envelope,batch_base64=base64.b64encode(batch.script).decode(),report=report)
            try:runtime.write_once(bundle_path,bundle)
            except FileExistsError:pass  # Another controller may have frozen the same request first.
        bundle=json.loads(runtime.read_regular(bundle_path,16384,private=True))
        payload=bundle['envelope']['payload']
        if (bundle['report']!=report or payload.get('authorization_policy_sha256')!=self.policy_sha256
                or base64.b64decode(bundle['batch_base64'],validate=True)!=batch.script):
            raise Conflict('Saved authorization policy or candidate changed; preserve prior grant')
        private_bytes_once(self.directory/(identifier+'.sh'),batch.script)
        private_bytes_once(self.directory/(identifier+'.json'),canonical(bundle['envelope']))
        grant_sha256=self.verify(submission,snapshot,batch)
        expected=dict(state='authorization_installed',request_id=identifier,manifest_sha256=snapshot.digest,
                      grant_sha256=grant_sha256,batch_sha256=batch.sha256,profile_sha256=self.pins['profile_sha256'])
        with self.ledger._transaction() as db:
            events=[dict(r) for r in db.execute("SELECT kind,at,payload FROM events WHERE request_id=? AND kind LIKE 'authorization_delivery_%' ORDER BY seq",(identifier,))]
            completed=[json.loads(e['payload']) for e in events if e['kind']=='authorization_delivery_finished']
            if completed:
                if completed[-1]['receipt']!=expected:raise Conflict('Delivered grant differs from this request')
                return True
            attempts=[e for e in events if e['kind']=='authorization_delivery_started']
            if attempts and time.time()-attempts[-1]['at']<60:return False
            if len(attempts)>=3:raise Conflict('Authorization delivery requires reconciliation after three attempts')
            ticket=uuid.uuid4().hex
            self.ledger._event(db,identifier,'authorization_delivery_started',dict(ticket=ticket,grant_sha256=grant_sha256,
                policy_sha256=self.policy_sha256))
        remote=[self.endpoint.python_path,'-I','-c',BOOTSTRAP,self.endpoint.helper_path,self.endpoint.helper_sha256,
                '--root',self.endpoint.root_path,'--request-id',identifier,'--manifest-sha256',snapshot.digest,'--install-authorization']
        command=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
                 '-o','ClearAllForwardings=yes','-o','ForwardAgent=no','-o','ForwardX11=no','-o','PermitLocalCommand=no',
                 self.endpoint.host_alias,shlex.join(remote)]
        if self.transport is not None:command=self.transport.command(self.endpoint.host_alias,remote)
        package=canonical({k:bundle[k] for k in ('envelope','batch_base64')})
        try:result=_capture(command,timeout=40,max_bytes=65536,input_chunks=[package])
        except (OSError,ValueError) as exc:result=dict(returncode=None,failure=type(exc).__name__,stdout='',stderr='')
        proof=_write_new(self.audit_directory/(ticket+'.json'),dict(result=result,request_id=identifier,
            package_sha256=sha256(package),grant_sha256=grant_sha256))
        try:
            receipt=json.loads(base64.b64decode(result['stdout'],validate=True))
            success=not result['failure'] and result['returncode']==0 and receipt==expected
        except (ValueError,TypeError):success=False
        with self.ledger._transaction() as db:
            self.ledger._event(db,identifier,'authorization_delivery_finished' if success else 'authorization_delivery_unconfirmed',
                dict(ticket=ticket,evidence_sha256=proof,**({'receipt':expected} if success else {})))
        return success
