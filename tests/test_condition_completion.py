"""Completion proposals are confirmable defaults, never extracted facts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from auto_lammps.condition_generation import complete_condition_draft, completion_messages, validate_completion
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from test_deepseek import response

ALL_FIELDS = [key for key in FIELDS if key != 'reference']
PROPOSALS = dict(proposals=[dict(field=key, value='合成值 '+key, unit='', basis='合成依据：领域惯例')
                            for key in ALL_FIELDS])


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.doc=self.store.create('合成补全测试','研究铜在 300 K 下的弹性常数。','research')
        self.calls=ModelCalls(Path(self.tmp.name)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=4)
        self.transport=Mock(return_value=(200, response(PROPOSALS)))
        self.client=DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda:'synthetic-key')

    def complete(self, payload=PROPOSALS, request_id='b'*32):
        self.transport.return_value=(200, response(payload))
        return complete_condition_draft(self.client, self.store, self.doc['id'], self.store.get(self.doc['id'])['revision'], request_id)

    def test_missing_fields_drive_the_request_and_proposals_are_labelled(self):
        doc=self.complete()
        field=doc and self.store.get(self.doc['id'])['fields']['units']
        self.assertEqual(field['candidates'][0]['origin'], 'proposed')
        self.assertIn('合成依据', field['candidates'][0]['source_locator'])
        self.assertFalse(field['confirmed'])
        self.assertEqual(len(self.calls.history()), 1)

    def test_existing_candidates_are_never_overwritten(self):
        self.store.add_candidate(self.doc['id'], self.doc['revision'], 'units',
                                 dict(value='metal', unit='', origin='user', source_locator='用户原文',
                                      applicability='required', evidence_role='input'))
        payload=dict(proposals=[item for item in PROPOSALS['proposals'] if item['field']!='units'])
        self.complete(payload)
        field=self.store.get(self.doc['id'])['fields']['units']
        self.assertEqual(len(field['candidates']), 1)
        self.assertEqual(field['candidates'][0]['origin'], 'user')
        import json
        body=json.loads(self.transport.call_args[0][0])
        user=json.loads(body['messages'][1]['content'])
        self.assertNotIn('units', user['missing_fields'])

    def test_a_field_answered_during_the_call_is_skipped_not_lost(self):
        def answer_units_while_running(body, key, timeout):
            # 模拟用户在模型运行期间自己补上了 units
            doc=self.store.get(self.doc['id'])
            self.store.add_candidate(self.doc['id'], doc['revision'], 'units',
                                     dict(value='metal', unit='', origin='user', source_locator='用户原文',
                                          applicability='required', evidence_role='input'))
            return 200, response(PROPOSALS)
        self.transport.side_effect=answer_units_while_running
        result=complete_condition_draft(self.client, self.store, self.doc['id'],
                                        self.store.get(self.doc['id'])['revision'], 'd'*32)
        self.assertIn('units', result['skipped_fields'])
        self.assertNotIn('units', result['proposed_fields'])
        self.assertEqual(len(self.store.get(self.doc['id'])['fields']['units']['candidates']), 1)

    def test_no_missing_field_is_refused_without_calling_the_model(self):
        for key in ALL_FIELDS:
            self.store.add_candidate(self.doc['id'], self.store.get(self.doc['id'])['revision'], key,
                                     dict(value='x', unit='', origin='user', source_locator='用户原文',
                                          applicability='required', evidence_role='input'))
        with self.assertRaises(TaskError):
            complete_condition_draft(self.client, self.store, self.doc['id'],
                                     self.store.get(self.doc['id'])['revision'], 'c'*32)
        self.assertEqual(self.calls.history(), [])

    def test_completion_then_confirmation_makes_freeze_reachable(self):
        self.complete()
        doc=self.store.get(self.doc['id'])
        selected={key: doc['fields'][key]['selected'] for key in ALL_FIELDS}
        missing_selection=[key for key,value in selected.items() if value is None]
        for key in missing_selection:
            candidate_id=doc['fields'][key]['candidates'][0]['id']
            self.store.select(self.doc['id'], self.store.get(self.doc['id'])['revision'], key, candidate_id, '验收：选择模型建议')
        doc=self.store.get(self.doc['id'])
        self.store.confirm(self.doc['id'], doc['revision'], ALL_FIELDS)
        doc=self.store.get(self.doc['id'])
        self.assertEqual(__import__('auto_lammps.tasks', fromlist=['issues']).issues(doc), [])
        frozen=self.store.freeze(self.doc['id'], doc['revision'])
        self.assertEqual(frozen['status'], 'conditions_frozen')
        self.assertEqual(self.store.get(self.doc['id'])['issues'], [])

    def test_invalid_shapes_are_refused(self):
        for payload in (dict(), dict(proposals=[dict(field='units', value='metal')]),
                        dict(proposals=[dict(field='unknown', value='x', unit='', basis='b')]),
                        dict(proposals=[dict(field='units', value='x', unit='', basis='')]),
                        dict(proposals=[dict(field='units', value='x', unit='', basis='b'),
                                        dict(field='units', value='y', unit='', basis='b')])):
            with self.assertRaises(TaskError):
                validate_completion(['units'], payload)

    def test_message_contract_forbids_claiming_the_source(self):
        messages=completion_messages(ALL_FIELDS, [], 'research')
        system=messages[0]['content']
        self.assertIn('不是从原文抽取的事实', system)
        self.assertIn('basis', system)
        with self.assertRaises(TaskError):
            completion_messages([], [], 'research')
        with self.assertRaises(TaskError):
            completion_messages(['not-a-field'], [], 'research')


if __name__ == '__main__':
    unittest.main()
