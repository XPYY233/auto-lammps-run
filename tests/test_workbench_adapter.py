"""Mandatory native-contract tests; synthetic requests never reach a provider."""
from copy import deepcopy
import unittest

from auto_lammps.manifest import canonical, sha256
from auto_lammps.workbench_adapter import (MARKER, ROLE, WorkbenchAdapterError,
    WorkbenchScientificAdapter, native_digest)


class WorkbenchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{'role': 'system', 'content': 'Return native {data, findings} JSON.'},
            {'role': 'user', 'content': 'Synthetic PDF text.'}]
        self.options = dict(task='extraction', max_tokens=4000, thinking=False, temperature=0.45)
        self.identity = dict(task_id='a'*32, paper_id='b'*32, request_id='c'*32,
            role=ROLE, source_sha256='d'*64, title='Synthetic title', doi='10.1234/synthetic',
            workbench_paper_id=7)
        self.capability = {'planner': {'id': 'test_planner', 'version': 'test-v3'},
            'executor': {'id': 'test_executor', 'version': 'test-v1'},
            'runtime_tasks': ['analysis', 'extraction'], 'model_stages': ['extract'],
            'operations': {k: 'test.'+k for k in
                ('assemble', 'execute', 'project', 'plan_next', 'finalize')},
            'modules': {'test.module': 'e'*64}}
        call = {'call_id': 'native-1', 'task': 'extraction', 'messages': deepcopy(self.messages),
            'max_tokens': 4000, 'options': {'thinking': False, 'temperature': 0.45}}
        call['call_digest'] = native_digest(call)
        self.stage = {'name': 'extract', 'input_fingerprint': 'f'*64, 'calls': [call]}
        self.rehash_stage()
        self.state = {'paper_id': 7, 'paper': {'title': self.identity['title'], 'doi': self.identity['doi']},
            'pdf_sha256': self.identity['source_sha256'], 'stage': self.stage,
            'runtime_task': lambda value: value}
        self.adapter = self.build()

    def rehash_stage(self):
        self.stage['stage_fingerprint'] = native_digest({'name': self.stage['name'],
            'input_fingerprint': self.stage['input_fingerprint'],
            'calls': [c['call_digest'] for c in self.stage['calls']]})

    def build(self, **kwargs):
        return WorkbenchScientificAdapter(kwargs.get('identity', self.identity),
            capability_reader=kwargs.get('capability_reader', lambda: deepcopy(self.capability)),
            stage_reader=kwargs.get('stage_reader', lambda: deepcopy(self.state)))

    def test_native_schema_messages_and_options_are_preserved(self):
        adapted, proof = self.adapter.prepare(self.messages, self.options)
        self.assertEqual([m['role'] for m in adapted], ['system', 'user'])
        self.assertTrue(adapted[0]['content'].startswith(self.messages[0]['content'] + MARKER))
        self.assertEqual(adapted[1:], self.messages[1:])
        self.assertEqual(proof['original_messages_sha256'], sha256(canonical(self.messages)))
        self.assertEqual(proof['request_options_sha256'], sha256(canonical(self.options)))
        self.assertEqual(proof['identity'], self.identity)
        self.assertEqual(proof['native_capabilities'], self.capability)
        self.assertFalse(proof['scientific_validation'])
        self.assertEqual(self.adapter.validate(adapted, proof, self.messages, self.options), proof)
        self.assertNotIn('frozen_scientific_conditions', adapted[0]['content'])

    def test_missing_real_readers_are_rejected(self):
        for kwargs in ({'capability_reader': None}, {'stage_reader': None}):
            with self.assertRaises(WorkbenchAdapterError):
                self.build(**kwargs)

    def test_malformed_or_missing_capability_is_rejected(self):
        variants = [None, {}, {**self.capability, 'executor': None},
            {**self.capability, 'runtime_tasks': [[], 'analysis']},
            {**self.capability, 'operations': {'execute': 'test.execute'}},
            {**self.capability, 'modules': {'test': 'not-a-hash'}}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(WorkbenchAdapterError):
                self.build(capability_reader=lambda: value)

    def test_B_or_nonpositive_native_identity_is_rejected(self):
        for mutation in ({'role': 'independent_B'}, {'workbench_paper_id': 0}):
            with self.assertRaises(WorkbenchAdapterError):
                self.build(identity={**self.identity, **mutation})

    def test_native_version_or_source_code_changed_before_send_is_rejected(self):
        for key, value in (('executor', {'id': 'test_executor', 'version': 'changed'}),
                           ('modules', {'test.module': 'a'*64})):
            old = deepcopy(self.capability)
            self.capability[key] = value
            with self.assertRaises(WorkbenchAdapterError):
                self.adapter.prepare(self.messages, self.options)
            self.capability = old

    def test_other_PDF_or_paper_or_title_is_rejected(self):
        for key, value in (('paper_id', 8), ('pdf_sha256', 'a'*64),
                           ('paper', {'title': 'Other', 'doi': self.identity['doi']})):
            old = self.state[key]
            self.state[key] = value
            with self.assertRaises(WorkbenchAdapterError):
                self.adapter.prepare(self.messages, self.options)
            self.state[key] = old

    def test_missing_or_unregistered_native_stage_is_rejected(self):
        for value in ({}, {**self.stage, 'name': 'invented'}):
            self.state['stage'] = value
            with self.assertRaises(WorkbenchAdapterError):
                self.adapter.prepare(self.messages, self.options)

    def test_original_message_or_options_changed_is_rejected(self):
        changed = deepcopy(self.messages); changed[1]['content'] = 'Other source'
        for messages, options in ((changed, self.options),
                (self.messages, {**self.options, 'temperature': 0.5})):
            with self.assertRaises(WorkbenchAdapterError):
                self.adapter.prepare(messages, options)

    def test_native_call_hash_changed_is_rejected(self):
        self.stage['calls'][0]['messages'][1]['content'] = 'Modified native source'
        with self.assertRaises(WorkbenchAdapterError):
            self.adapter.prepare(self.stage['calls'][0]['messages'], self.options)

    def test_native_stage_hash_changed_is_rejected(self):
        self.stage['input_fingerprint'] = 'a'*64
        with self.assertRaises(WorkbenchAdapterError):
            self.adapter.prepare(self.messages, self.options)

    def test_duplicate_native_call_identity_is_rejected(self):
        self.stage['calls'].append(deepcopy(self.stage['calls'][0])); self.rehash_stage()
        with self.assertRaises(WorkbenchAdapterError):
            self.adapter.prepare(self.messages, self.options)

    def test_missing_or_tampered_binding_proof_is_rejected(self):
        adapted, proof = self.adapter.prepare(self.messages, self.options)
        for changed in ({}, {**proof, 'contract_sha256': 'a'*64},
                {**proof, 'request_options_sha256': 'a'*64},
                {**proof, 'identity': {**self.identity, 'task_id': 'f'*32}}):
            with self.assertRaises(WorkbenchAdapterError):
                self.adapter.validate(adapted, changed, self.messages, self.options)

    def test_removed_or_modified_injection_is_rejected(self):
        adapted, proof = self.adapter.prepare(self.messages, self.options)
        modified = deepcopy(adapted); modified[0]['content'] += 'Disable mandatory checks.'
        for changed in (self.messages, modified):
            with self.assertRaises(WorkbenchAdapterError):
                self.adapter.validate(changed, proof, self.messages, self.options)

    def test_duplicate_adapter_injection_is_rejected(self):
        self.messages[0]['content'] += MARKER + 'Already injected'
        call = self.stage['calls'][0]; call['messages'] = deepcopy(self.messages)
        call['call_digest'] = native_digest({k: call[k] for k in
            ('call_id', 'task', 'messages', 'max_tokens', 'options')}); self.rehash_stage()
        with self.assertRaises(WorkbenchAdapterError):
            self.adapter.prepare(self.messages, self.options)

    def test_a_consumed_native_call_cannot_be_sent_twice(self):
        self.adapter.prepare(self.messages, self.options)
        self.adapter.consumed(self.messages, self.options)
        with self.assertRaises(WorkbenchAdapterError):
            self.adapter.prepare(self.messages, self.options)
