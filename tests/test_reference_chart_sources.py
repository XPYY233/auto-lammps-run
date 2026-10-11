"""A-only chart provider through the existing real synthetic evidence fixture."""
from copy import deepcopy
import csv
import io
import json
import unittest
from unittest.mock import patch

from auto_lammps.manifest import sha256
from auto_lammps.reference_chart_sources import ReferenceChartSources
from auto_lammps.results import ResultUnavailable
import test_reference_evidence as evidence_fixture


class ReferenceChartSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = evidence_fixture.ReferenceEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for column in self.fixture.document['views'][0]['tables'][0]['columns']:
            column['source_role'] = 'reference'
        self.fixture.save()
        self.sources = ReferenceChartSources(self.fixture.views)

    def selection(self):
        return self.sources.options(self.fixture.task['id'])[0]

    def chart(self, selection=None, **kwargs):
        option = selection or self.selection()
        args = dict(identifier=self.fixture.task['id'], analysis_id=option['analysis_id'],
                    file=option['file'], x='cycle', y='A_energy')
        args.update(kwargs)
        return self.sources.chart_data(**args)

    def test_options_and_full_export_use_verified_numeric_A_columns_only(self):
        option = self.selection()
        self.assertTrue(self.sources.owns(option['analysis_id']))
        self.assertEqual(option['analysis_id'], 'reference-a:' + self.fixture.views.get(self.fixture.task['id'])['manifest_sha256'])
        self.assertEqual(option['columns'], [dict(name='cycle', unit='1'), dict(name='A_energy', unit='eV/atom')])
        self.assertEqual((option['rows'], option['examples']), (3, [['0', '1'], ['1', '']]))
        result = self.chart(option)
        self.assertEqual(result['rows'], 3)
        self.assertEqual(result['preview'], [[0., 1.], [2., 3.]])
        self.assertEqual(result['preview_table_rows'], [1, 3])
        self.assertEqual(result['missing_pair_count'], 1)
        self.assertEqual(result['csv'], b'cycle,A_energy\n0,1\n1,\n2,3\n')
        self.assertEqual(result['source_sha256'], sha256(self.fixture.contents['data.csv']))
        self.assertEqual(result['csv_sha256'], sha256(result['csv']))
        self.assertEqual(result['csv_size'], len(result['csv']))
        self.assertFalse(result['sampled'])
        encoded = json.dumps({key: value for key, value in result.items() if key != 'csv'}) + result['csv'].decode()
        for marker in ('P_ANSWER_CANARY', 'P_RECEIPT_CANARY', 'B_FROZEN_PRIVATE_CANARY', 'PRIVATE_', 'P_energy'):
            self.assertNotIn(marker, encoded)

    def test_no_text_column_is_promoted_by_numeric_looking_content(self):
        declaration = self.fixture.document['views'][0]['tables'][0]
        declaration['columns'][1]['kind'] = 'text'
        self.fixture.save()
        self.assertEqual(self.sources.options(self.fixture.task['id']), [])
        with self.assertRaises(ResultUnavailable):
            self.chart(dict(analysis_id='reference-a:' + 'a' * 64, file='data.csv'))

    def test_mixed_raw_P_and_B_canaries_never_enter_selected_exports(self):
        fixture = self.fixture
        fixture.contents['data.csv'] = (b'cycle,A_energy,P_energy,B_energy\n'
                                        b'0,1,P_ANSWER_CANARY,B_ANSWER_CANARY\n'
                                        b'1,2,P_ANSWER_CANARY,B_ANSWER_CANARY\n')
        fixture.save()
        result = self.chart()
        self.assertEqual(result['csv'], b'cycle,A_energy\n0,1\n1,2\n')
        self.assertNotIn('P_ANSWER_CANARY', result['csv'].decode())
        self.assertNotIn('B_ANSWER_CANARY', result['csv'].decode())
        for key in ('P_energy', 'B_energy'):
            fixture.document['views'][0]['tables'][0]['columns'][1]['key'] = key
            fixture.save()
            with self.subTest(key=key), self.assertRaises(ResultUnavailable):
                self.sources.options(fixture.task['id'])

    def test_unaccounted_source_cannot_supply_any_chart_options(self):
        fixture = self.fixture
        record = fixture.ledger.get(fixture.request['id'])
        with patch.object(fixture.ledger, 'get', return_value=dict(record, accounted=False)), self.assertRaises(ResultUnavailable):
            self.sources.options(fixture.task['id'])

    def test_missing_explicit_source_role_does_not_infer_reference_from_defaults(self):
        del self.fixture.document['views'][0]['tables'][0]['columns'][1]['source_role']
        self.fixture.save()
        self.assertEqual(self.sources.options(self.fixture.task['id']), [])

    def test_forbidden_P_B_columns_and_roles_fail_closed(self):
        original = deepcopy(self.fixture.document)
        for key, role in [('P_energy', 'reference'), ('B_energy', 'reference'), ('A_energy', 'agent')]:
            with self.subTest(key=key, role=role):
                self.fixture.document = deepcopy(original)
                self.fixture.document['views'][0]['tables'][0]['columns'][1].update(key=key, source_role=role)
                self.fixture.save()
                with self.assertRaises(ResultUnavailable):
                    self.sources.options(self.fixture.task['id'])

    def test_stale_manifest_or_artifact_is_not_reused(self):
        option = self.selection()
        self.fixture.document['views'][0]['tables'][0]['columns'][1]['label'] = 'Changed A label'
        self.fixture.save()
        with self.assertRaises(ResultUnavailable):
            self.chart(option)
        option = self.selection()
        (self.fixture.folder / 'data.csv').write_bytes(b'cycle,A_energy,P_energy\n0,999,P\n')
        with self.assertRaises(ResultUnavailable):
            self.chart(option)

    def test_manifest_read_race_is_bound_to_the_first_verified_digest(self):
        original = self.fixture.views._load
        def replaced(identifier):
            loaded = original(identifier)
            self.fixture.document['views'][0]['tables'][0]['columns'][1]['kind'] = 'text'
            self.fixture.write_manifest()
            return loaded
        with patch.object(self.fixture.views, '_load', side_effect=replaced), self.assertRaises(ResultUnavailable):
            self.sources.options(self.fixture.task['id'])

    def test_wrong_task_and_unowned_or_stale_namespace_are_rejected(self):
        option = self.selection()
        other = self.fixture.tasks.create('Other synthetic task', 'Permitted synthetic request', 'research')
        self.assertEqual(self.sources.options(other['id']), [])
        with self.assertRaises(ResultUnavailable):
            self.chart(option, identifier=other['id'])
        for analysis_id in ('a' * 64, 'reference-a:' + 'f' * 64, 'reference-a:../private', None):
            with self.subTest(analysis_id=analysis_id), self.assertRaises(ResultUnavailable):
                self.chart(option, analysis_id=analysis_id)
        self.assertFalse(self.sources.owns('agent:' + 'a' * 64))

    def test_partial_A_keeps_failure_coverage_and_scientific_limits(self):
        fixture = self.fixture
        row = fixture.ledger.reserve(fixture.evaluation, 'partial-author-A', evidence_fixture.H2, evidence_fixture.RESOURCE)
        fixture.ledger.begin_dispatch(row['id'])
        fixture.ledger.accepted(row['id'], '124', {'synthetic': True})
        fixture.ledger.observe(row['id'], '124', 'failed', {'synthetic': True})
        fixture.ledger.account(row['id'], 15, evidence_fixture.H1)
        fixture.document.update(request_id=row['id'], job_id='124', output_status='partial', output_valid=False)
        fixture.document['coverage'] = [dict(label='states', required=50, available=1, unit='state')]
        fixture.save(state='failed')
        result = self.chart()
        self.assertEqual((result['role'], result['output_status'], result['scheduler_state']), ('reference', 'partial', 'failed'))
        self.assertFalse(result['output_valid'])
        self.assertEqual(result['scientific_status'], 'not_evaluated')
        self.assertEqual(result['coverage'][0]['available'], 1)
        self.assertTrue(result['limitations'])

    def test_preview_is_bounded_and_exports_full_string_precision_and_missing_rows(self):
        fixture = self.fixture
        fixture.contents['data.csv'] = ('cycle,A_energy,P_energy\n' + ''.join(
            f'{index},{"" if index == 129 else "1.234567890123456789"},P_ANSWER_CANARY\n' for index in range(300))).encode()
        fixture.save()
        result = self.chart()
        self.assertTrue(result['sampled'])
        self.assertEqual(len(result['preview']), 128)
        self.assertEqual((result['preview_table_rows'][0], result['preview_table_rows'][-1]), (1, 300))
        self.assertEqual(result['missing_pair_count'], 1)
        rows = list(csv.reader(io.StringIO(result['csv'].decode())))
        self.assertEqual(len(rows), 301)
        self.assertEqual(rows[130], ['129', ''])
        self.assertEqual(rows[1][1], '1.234567890123456789')

    def test_all_missing_pairs_do_not_become_a_successful_empty_chart(self):
        self.fixture.contents['data.csv'] = b'cycle,A_energy,P_energy\n0,,P\n1,,P\n'
        self.fixture.save()
        with self.assertRaises(ResultUnavailable):
            self.chart()
        self.assertEqual(self.fixture.views.download(self.fixture.task['id'], 'data.csv'),
                         self.fixture.contents['data.csv'])

    def test_conflicting_kind_declarations_for_same_source_are_not_guessed(self):
        view = deepcopy(self.fixture.document['views'][0])
        view['id'] = 'same-source-other-kind'
        view['tables'][0]['columns'][1]['kind'] = 'text'
        self.fixture.document['views'].append(view)
        self.fixture.save()
        with self.assertRaises(ResultUnavailable):
            self.sources.options(self.fixture.task['id'])

    def test_read_only_adapter_does_not_borrow_model_context_or_raw_download(self):
        fixture = self.fixture
        before_task = fixture.tasks.get(fixture.task['id'])
        before_ledger = fixture.ledger.events(fixture.request['id'])
        with patch.object(fixture.views, 'download', side_effect=AssertionError('No mixed CSV download')), \
             patch.object(fixture.views, 'assistant_context', side_effect=AssertionError('No model context')), \
             patch('subprocess.Popen', side_effect=AssertionError('No engine')), \
             patch('socket.socket', side_effect=AssertionError('No model or scheduler')):
            self.chart()
        self.assertEqual(fixture.tasks.get(fixture.task['id']), before_task)
        self.assertEqual(fixture.ledger.events(fixture.request['id']), before_ledger)


if __name__ == '__main__':
    unittest.main()
