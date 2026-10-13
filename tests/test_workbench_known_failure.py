"""Synthetic returned-length recovery; no real PDFs, providers or physics."""
from copy import deepcopy
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskStore, TaskError
from auto_lammps.workbench_adapter import WorkbenchScientificAdapter
from auto_lammps.workbench_bridge import WorkbenchExtractionBridge, WorkbenchModelClient, WorkbenchPaperBinding, WorkbenchRecoveryBinding
from auto_lammps.workbench_known_failure import known_failure_context, create_known_failure_child, KnownFailureError


def native_available():
    try:
        return importlib.util.find_spec('auto_research.evidence.literature_known_length_recovery') is not None
    except ModuleNotFoundError:
        return False


class SyntheticSealer:
    def seal(self, plaintext, *, associated_data):
        return bytes.fromhex(sha256(associated_data + plaintext)) + plaintext
    def open(self, ciphertext, *, associated_data):
        body = ciphertext[32:]
        if ciphertext[:32] != bytes.fromhex(sha256(associated_data + body)):
            raise ValueError('synthetic authentication failure')
        return body


class PublicKnownFailurePreflightTests(unittest.TestCase):
    """These public CI refusal checks require no optional native package."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.task = self.tasks.create('Synthetic public refusal', 'No physics', 'reproduction')
        self.papers = PaperStore(self.tasks)
        paper = self.papers.add('Synthetic unknown article', '10.1234/unknown', 'Scope', 'Test')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        self.binding = WorkbenchPaperBinding(self.paper['id'], 7, 'a'*64)
        self.native_reads = []
        self.runtime = SimpleNamespace(session_id='synthetic-public-session',
            database=SimpleNamespace(get_paper=lambda identifier: {'title': self.paper['title'], 'doi': self.paper['doi'],
                'pdf_sha256': 'a'*64, 'pdf_path': 'synthetic-only.pdf'}),
            native_source_reader=lambda identifier: 'a'*64,
            native_recovery_reader=lambda token: self.native_reads.append(token))
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        self.calls = ModelCalls(self.root/'calls.sqlite', DeepSeekConfig('synthetic-model', max_output_tokens=65536),max_requests=8)
        self.client = DeepSeekClient(self.calls,transport=lambda *args:self.fail('paid transport must not occur'),
            key_reader=lambda:self.fail('credential must not be read'))
        self.original_id = 'b'*32
        intent = self.bridge.extraction_identity(self.task['id'],self.original_id,self.binding,self.client,
            connection_revision=1,credential_generation=1)
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (self.original_id,self.task['id'],self.paper['id'],sha256(canonical(intent)),canonical(intent).decode()))

    def context(self, task=None, binding=None):
        return known_failure_context(self.bridge,task or self.task['id'],self.original_id,
            binding or self.binding,self.client,connection_revision=1,credential_generation=1,now=100)

    def test_missing_receipt_is_unknown_and_never_reaches_native(self):
        with self.assertRaises(KnownFailureError):self.context()
        self.assertEqual(self.calls.history(),[])
        self.assertEqual(self.native_reads,[])

    def test_current_source_change_blocks_before_native_or_credentials(self):
        self.runtime.native_source_reader=lambda identifier:'f'*64
        with self.assertRaises(KnownFailureError):self.context()
        self.assertEqual(self.calls.history(),[])
        self.assertEqual(self.native_reads,[])

    def test_other_task_cannot_use_reference_paper_history(self):
        other=self.tasks.create('Another synthetic task','No physics','reproduction')
        with self.assertRaises(KnownFailureError):self.context(task=other['id'])
        self.assertEqual(self.native_reads,[])

    def test_browser_binding_or_independent_role_is_not_a_trusted_source(self):
        forged=SimpleNamespace(paper_id=self.paper['id'],workbench_paper_id=7,pdf_sha256='a'*64,role='independent')
        with self.assertRaises(KnownFailureError):self.context(binding=forged)
        self.assertEqual(self.native_reads,[])

    def test_effective_model_policy_change_rejects_stale_client(self):
        amended=ModelCalls(self.calls.path,self.calls.config,max_requests=8)
        amended.amend_limits(replace(amended.config,max_output_tokens=32000),reason_sha256='e'*64)
        with self.assertRaises(KnownFailureError):self.context()
        self.assertEqual(self.calls.history(),[])
        self.assertEqual(self.native_reads,[])


@unittest.skipUnless(native_available(), 'optional native known-length dependency is unavailable')
class WorkbenchKnownFailureTests(unittest.TestCase):
    def setUp(self):
        from auto_research.evidence.literature_checkpoint_runtime import LiteratureCheckpointRuntime
        from auto_research.evidence.literature_task_checkpoint_service import LiteratureTaskCheckpointService
        from auto_research.evidence.literature_task_checkpoint_store import SealedSQLiteLiteratureCheckpointStore
        from auto_research.evidence.literature_snapshot_blob import SealedImmutablePDFBlobStore
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.task = self.tasks.create('Synthetic reference recovery', 'No physics', 'reproduction')
        self.papers = PaperStore(self.tasks)
        paper = self.papers.add('Synthetic returned length article', '10.1234/known-length', 'Scope', 'Test')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        self.source_bytes = b'Synthetic immutable source fixture bytes'
        self.source_sha = sha256(self.source_bytes)
        self.binding = WorkbenchPaperBinding(self.paper['id'], 7, self.source_sha)
        self.session = 'synthetic-stable-native-session'
        self.original_id, self.child_id = 'a'*32, 'b'*32
        self.token = 'synthetic_parent_' + 'p'*40
        self.store = SealedSQLiteLiteratureCheckpointStore(data_root=self.root/'checkpoints', sealer=SyntheticSealer())
        self.service = LiteratureTaskCheckpointService(store=self.store, clock=lambda: 100)
        self.native_runtime = LiteratureCheckpointRuntime(self.service)
        self.blobs = SealedImmutablePDFBlobStore(data_root=self.root/'snapshots', sealer=SyntheticSealer())
        self.outgoing = []; self.plan_count = self.create_count = 0
        self.calls = ModelCalls(self.root/'calls.sqlite', DeepSeekConfig('synthetic-model',
            max_output_tokens=65536, max_input_bytes=262144), max_requests=512)
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda: 'synthetic-only-key')
        self.capabilities = {'planner': {'id': 'existing_literature_stage_planner', 'version': 'v3'},
            'executor': {'id': 'literature_extraction_executor', 'version': 'v1'},
            'runtime_tasks': ['analysis', 'extraction'], 'model_stages': ['initial_focus'],
            'operations': {key: 'synthetic_native.' + key for key in ('assemble','execute','project','plan_next','finalize')},
            'modules': {'synthetic_native': 'e'*64}, 'output_limits': {'extraction_records_per_response': 64},
            'source_partition': {'chunk_pages': 1, 'max_pages': 64, 'policy': 'whole_line_source_fragments_v1',
                'max_lines': 32, 'max_chars': 1500, 'max_fragments': 63}}
        self.runtime = SimpleNamespace(session_id=self.session,
            database=SimpleNamespace(get_paper=lambda identifier: {'title': self.paper['title'], 'doi': self.paper['doi'],
                'pdf_sha256': self.source_sha, 'pdf_path': 'synthetic-only.pdf'}),
            native_source_reader=lambda identifier: self.source_sha,
            native_capability_reader=lambda: deepcopy(self.capabilities),
            native_recovery_reader=self.read_native,
            native_known_failure_planner=self.plan_native,
            native_known_failure_creator=self.create_native)
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        self.make_original()
        self.original_model_history = self.calls.history()
        self.original_checkpoint_bytes = self.encode_checkpoint(self.service.load('literature_' + sha256(self.token.encode())))

    def transport(self, body, key, timeout):
        self.outgoing.append(json.loads(body))
        if len(self.outgoing) == 39:
            return 200, canonical({'choices': [{'finish_reason': 'length', 'message': {'role': 'assistant', 'content': '{"data":['}}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 16000, 'total_tokens': 16004}})
        return 200, canonical({'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': json.dumps({'data': [], 'findings': [], 'pending_tasks': []})}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})

    @staticmethod
    def encode_checkpoint(checkpoint):
        from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
        return encode_checkpoint(checkpoint)

    def read_native(self, token):
        from auto_research.evidence.literature_checkpoint_runtime import decode_execution_state
        from auto_research.evidence.literature_job_persistence import decode_job_private_state
        checkpoint = self.service.load('literature_' + sha256(token.encode()))
        execution = decode_execution_state(checkpoint.private_payload)
        return {'checkpoint': checkpoint, 'execution': execution, 'job': decode_job_private_state(execution.job_state)}

    def native_result(self, checkpoint):
        from auto_research.evidence.literature_extraction_job import FrozenModelCall
        value = self.read_native_from(checkpoint)
        index = len(value['execution'].completed_results)
        call = value['job'].stage['calls'][index]
        return {**value, 'replacement_index': index,
            'replacement_call': FrozenModelCall.create(call_id=call['call_id'], task=call['task'], messages=call['messages'],
                options=call['options'], max_tokens=call['max_tokens'])}

    def read_native_from(self, checkpoint):
        from auto_research.evidence.literature_checkpoint_runtime import decode_execution_state
        from auto_research.evidence.literature_job_persistence import decode_job_private_state
        execution = decode_execution_state(checkpoint.private_payload)
        return {'checkpoint': checkpoint, 'execution': execution, 'job': decode_job_private_state(execution.job_state)}

    def plan_native(self, token, **options):
        from auto_research.evidence.literature_known_length_recovery import KnownLengthProof, plan_known_length_continuation
        self.plan_count += 1
        options['proof'] = KnownLengthProof(**options.pop('proof_kwargs'))
        verify = options['verify_proof']; options['verify_proof'] = lambda proof: verify(asdict(proof))
        plan = plan_known_length_continuation(self.read_native(token)['checkpoint'], **options)
        return {**self.read_native_from(plan.checkpoint), 'replacement_index': plan.replacement_index,
            'replacement_call': plan.replacement_call}

    def create_native(self, token, **options):
        from auto_research.evidence.literature_known_length_recovery import KnownLengthProof
        self.create_count += 1
        options['proof'] = KnownLengthProof(**options.pop('proof_kwargs'))
        verify = options['verify_proof']; options['verify_proof'] = lambda proof: verify(asdict(proof))
        child = self.native_runtime.create_known_length_continuation('literature_' + sha256(token.encode()),
            snapshot_blobs=self.blobs, **options)
        return self.native_result(child)

    def make_original(self, fragments=52):
        from auto_research.evidence.literature_checkpoint_runtime import LiteratureCheckpointCall
        from auto_research.evidence.literature_extraction_budget import task_budget_for_page_blocks
        from auto_research.evidence.literature_extraction_job import FrozenPDFContext, FrozenModelCall, FrozenExtractionStage, _Job
        from auto_research.evidence.literature_job_persistence import _digest, encode_job_private_state
        from auto_research.evidence.literature_task_checkpoint import LiteratureTaskManifest, LiteratureTaskCheckpointError
        texts = [f'Synthetic fixture row {i:03d} contains complete value {i}.125\n' for i in range(fragments)]
        pages = [{'page': 1, 'text': ''.join(texts)}]
        snapshot = FrozenPDFContext(self.source_sha, tuple(pages), 1,
            _digest({'pdf_sha256': self.source_sha, 'page_count': 1, 'pages': pages}))
        calls = tuple(FrozenModelCall.create(call_id=f'{branch}-chunk-{i+1}-focus-1', task='extraction',
            messages=[{'role': 'system', 'content': 'Extract original source as JSON.'},
                {'role': 'user', 'content': 'Full synthetic context: '+pages[0]['text']+'\nFragment: '+texts[i]}],
            max_tokens=16000, options={'thinking': False, 'temperature': 0.1})
            for branch in ('a','b') for i in range(fragments))
        self.stage = FrozenExtractionStage.create('initial_focus', calls, snapshot.content_fingerprint, 90)
        job = _Job(self.token, sha256(b'synthetic separate job HMAC'), 7,
            {'title': self.paper['title'], 'doi': self.paper['doi']},
            self.blobs.put(self.source_bytes, expected_sha256=self.source_sha), snapshot, {'synthetic': True},
            tuple(({'page': 1, 'text': text},) for text in texts), ('synthetic focus',), '', self.stage, 90, 1000)
        budget = task_budget_for_page_blocks(fragments)
        manifest = LiteratureTaskManifest('literature_' + sha256(self.token.encode()), sha256(self.session.encode()),
            'deepseek', 1, 1, (('analysis', self.client.model), ('extraction', self.client.model)),
            'literature_extraction_executor', 'v1', snapshot.content_fingerprint, budget.max_calls, budget.max_tokens, 90, 1000)
        checkpoint = self.native_runtime.start(manifest=manifest, job_state=encode_job_private_state(job),
            stage='initial_focus', stage_fingerprint=self.stage.stage_fingerprint)
        checkpoint = self.native_runtime.recover(manifest.task_id, owner_id='synthetic-parent-owner')
        intent = self.bridge.extraction_identity(self.task['id'], self.original_id, self.binding, self.client,
            connection_revision=1, credential_generation=1)
        adapter = WorkbenchModelClient(self.client, task=self.task['id'], paper=self.paper['id'],
            source_sha256=self.source_sha, request_id=self.original_id)
        adapter.adapter = WorkbenchScientificAdapter({**adapter.identity, 'title': self.paper['title'],
            'doi': self.paper['doi'], 'workbench_paper_id': 7}, capability_reader=self.runtime.native_capability_reader,
            stage_reader=lambda: {'paper_id': 7, 'paper': dict(job.paper), 'pdf_sha256': self.source_sha,
                'stage': self.read_native_from(checkpoint)['job'].stage, 'runtime_task': lambda value: value})
        with self.assertRaises(LiteratureTaskCheckpointError):
            self.native_runtime.execute_stage(checkpoint, owner_id='synthetic-parent-owner',
                stage_fingerprint=self.stage.stage_fingerprint,
                calls=tuple(LiteratureCheckpointCall('initial_focus', 'extraction', c.call_digest, c.max_tokens) for c in calls),
                invoke=lambda index: adapter.request_json(calls[index].messages, task='extraction', max_tokens=16000,
                    thinking=False, temperature=0.1))
        self.model_ids = list(adapter.requests)
        failure = {**intent, 'state': 'failed', 'error_code': 'workbench_model_response_invalid',
            'model_request_ids': self.model_ids}
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (self.original_id, self.task['id'], self.paper['id'], sha256(canonical(intent)), canonical(intent).decode()))
            db.execute('INSERT INTO workbench_extraction_receipts VALUES (?,?)', (self.original_id, canonical(failure).decode()))
        self.bridge._save_continuation(self.original_id, intent, self.binding, self.client, self.token, adapter.adapter)

    def context(self, **kwargs):
        return known_failure_context(self.bridge, self.task['id'], self.original_id, self.binding, self.client,
            connection_revision=kwargs.pop('connection_revision',1), credential_generation=kwargs.pop('credential_generation',1),
            now=kwargs.pop('now',100), **kwargs)

    def create(self, identifier=None):
        return create_known_failure_child(self.bridge, self.task['id'], self.original_id, self.binding, self.client,
            continuation_request_id=identifier or self.child_id, connection_revision=1, credential_generation=1, now=100)

    def test_read_only_context_verifies_38_results_and_new_budget(self):
        context = self.context()
        self.assertEqual(len(context.successful_prefix), 38)
        self.assertEqual(context.model_request_ids, tuple(self.model_ids))
        self.assertEqual(context.recovery_metadata['max_calls'],425)
        self.assertEqual(context.recovery_metadata['max_tokens'],3828000)
        self.assertEqual(context.proof_kwargs['model_request_id'],self.model_ids[-1])
        self.assertEqual(self.create_count,0)
        self.assertEqual(len(self.store.list_task_ids()),1)
        self.assertEqual(self.calls.history(),self.original_model_history)
        self.assertEqual(self.encode_checkpoint(self.read_native(self.token)['checkpoint']),self.original_checkpoint_bytes)

    def test_application_action_creates_once_and_keeps_parent_and_charges(self):
        child = self.create()
        self.assertNotEqual(child['job_token'],self.token)
        self.assertEqual((child['prefix_count'],child['reused_model_calls'],child['inherited_failed_model_calls']),(38,38,1))
        self.assertEqual((child['max_calls'],child['max_tokens']),(425,3828000))
        self.assertEqual(len(self.store.list_task_ids()),2)
        self.assertEqual(self.create(),child)
        with self.assertRaises(KnownFailureError):self.create('c'*32)
        self.assertEqual(self.calls.history(),self.original_model_history)
        self.assertEqual(len(self.outgoing),39)
        self.assertEqual(self.encode_checkpoint(self.read_native(self.token)['checkpoint']),self.original_checkpoint_bytes)

    def test_terminal_failure_and_unknown_counterexamples_never_create(self):
        lookup = self.calls.lookup
        for field,value in {'state':'unknown','http_status':500,'finish_reason':'stop',
                'error':'model_transport_unknown','response_sha256':'','request_sha256':'f'*64,
                'transport_attempts':2,'max_output_tokens':32000,'usage':None}.items():
            def altered(identifier):
                row=deepcopy(lookup(identifier))
                if identifier==self.model_ids[-1]:row['receipt'][field]=value
                return row
            with self.subTest(field=field),patch.object(self.calls,'lookup',side_effect=altered):
                with self.assertRaises(KnownFailureError):self.context()
        self.assertEqual(self.create_count,0)
        self.assertEqual(len(self.outgoing),39)

    def test_success_hash_call_options_and_adapter_counterexamples(self):
        lookup=self.calls.lookup
        cases=[('structured_output',{'data':[{'forged':True}]}),('output_sha256','f'*64),
            ('task','analysis'),('role','independent'),('requested_model','foreign-model')]
        for field,value in cases:
            def changed(identifier):
                row=deepcopy(lookup(identifier))
                if identifier==self.model_ids[0]:row['receipt'][field]=value
                return row
            with self.subTest(field=field),patch.object(self.calls,'lookup',side_effect=changed):
                with self.assertRaises(KnownFailureError):self.context()
        for field in ('native_call_digest','stage_fingerprint','adapter_source_sha256',
                'original_messages_sha256','request_options_sha256','adapted_messages_sha256'):
            def changed(identifier):
                row=deepcopy(lookup(identifier))
                if identifier==self.model_ids[-1]:row['receipt']['scientific_adapter'][field]='f'*64
                return row
            with self.subTest(field=field),patch.object(self.calls,'lookup',side_effect=changed):
                with self.assertRaises(KnownFailureError):self.context()

    def test_source_connection_and_session_changes_fail(self):
        for settings in ({'connection_revision':2},{'credential_generation':2},{'now':1000}):
            with self.assertRaises(KnownFailureError):self.context(**settings)
        with patch.object(self.runtime,'native_source_reader',return_value='f'*64):
            with self.assertRaises(KnownFailureError):self.context()
        with patch.object(self.runtime,'session_id','different-session'):
            with self.assertRaises(KnownFailureError):self.context()

    def test_other_unknown_request_for_pdf_blocks_recovery(self):
        intent=self.bridge.extraction_identity(self.task['id'],'d'*32,self.binding,self.client,
            connection_revision=1,credential_generation=1,force_rescan=True)
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                ('d'*32,self.task['id'],self.paper['id'],sha256(canonical(intent)),canonical(intent).decode()))
        with self.assertRaises(KnownFailureError):self.context()

    def test_reserved_same_action_can_create_but_different_action_cannot(self):
        intent=self.bridge.extraction_identity(self.task['id'],self.child_id,self.binding,self.client,
            connection_revision=1,credential_generation=1,resume_of=self.original_id)
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (self.child_id,self.task['id'],self.paper['id'],sha256(canonical(intent)),canonical(intent).decode()))
            db.execute('INSERT INTO workbench_extraction_recoveries VALUES (?,?,?)',
                (self.child_id,self.original_id,'{}'))
        with self.assertRaises(KnownFailureError):self.context()
        self.assertEqual(self.create()['continuation_request_id'],self.child_id)
        with self.assertRaises(KnownFailureError):self.create('c'*32)

    def test_current_native_change_needs_exact_trusted_registration(self):
        old=sha256(canonical(self.capabilities))
        self.capabilities['modules']['synthetic_native']='f'*64
        with self.assertRaises(KnownFailureError):self.context()
        with self.tasks.transaction() as db:
            intent,failure=self.bridge._request(db,self.original_id)
        self.bridge.recoveries[self.original_id]=WorkbenchRecoveryBinding(self.original_id,self.token,
            sha256(canonical(intent)),sha256(canonical(failure)),**self.bridge._model_binding(self.client),
            prior_native_capability_sha256=old,native_capability_sha256=sha256(canonical(self.capabilities)))
        self.assertEqual(self.context().recovery_metadata['prefix_count'],38)

    def test_creator_rechecks_source_during_native_proof_verification(self):
        creator=self.runtime.native_known_failure_creator
        def changed(token,**options):
            self.runtime.native_source_reader=lambda identifier:'f'*64
            return creator(token,**options)
        with patch.object(self.runtime,'native_known_failure_creator',side_effect=changed):
            with self.assertRaises(KnownFailureError):self.create()
        self.assertEqual(len(self.store.list_task_ids()),1)
        self.assertEqual(len(self.outgoing),39)

    def test_unknown_existing_child_is_not_recreated_or_overwritten(self):
        child=self.create()
        current=self.read_native(child['job_token'])['checkpoint']
        # An actual started replacement, followed by transport uncertainty.
        from auto_research.evidence.literature_checkpoint_runtime import LiteratureCheckpointCall
        from auto_research.evidence.literature_task_checkpoint import LiteratureTaskCheckpointError
        current=self.native_runtime.recover(current.manifest.task_id,owner_id='synthetic-child-owner')
        job=self.read_native(child['job_token'])['job']
        with self.assertRaises(LiteratureTaskCheckpointError):
            self.native_runtime.execute_stage(current,owner_id='synthetic-child-owner',
                stage_fingerprint=job.stage['stage_fingerprint'],
                calls=tuple(LiteratureCheckpointCall('initial_focus','extraction',c['call_digest'],c['max_tokens']) for c in job.stage['calls']),
                invoke=lambda index:(_ for _ in ()).throw(TimeoutError('synthetic unknown')))
        before=self.encode_checkpoint(self.read_native(child['job_token'])['checkpoint'])
        with self.assertRaises(KnownFailureError):self.create()
        self.assertEqual(self.encode_checkpoint(self.read_native(child['job_token'])['checkpoint']),before)
        self.assertEqual(len(self.store.list_task_ids()),2)


if __name__=='__main__':unittest.main()
