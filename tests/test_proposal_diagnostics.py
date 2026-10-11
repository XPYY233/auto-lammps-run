"""Synthetic read-only proposal feedback through the real static validators."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from auto_lammps import agent_candidates, analysis_v2
from auto_lammps.proposal_diagnostics import collect_proposal_diagnostics


def proposal():
    return dict(summary='Synthetic static fixture, not a physical model', questions=[],
                structure=dict(crystal='bcc', elements=['Fe'], a_angstrom=3.0, repeat=[3, 3, 3],
                               orientation='cubic_axes', boundary=['p', 'p', 'p'], vacancies=[],
                               substitutions=[dict(site=0, element='Ni')],
                               type_elements=['Fe', 'Ni'], masses_amu=[56.0, 59.0]),
                potential_pin='a' * 64, workflow='run 0\nwrite_data /output/final.data',
                analysis=dict(quantity='Synthetic values', method='Frozen synthetic statistics',
                              files=['final.data'], plan=dict(
                                  tables=[dict(file='final.data', columns=[dict(name='step', unit='step'),
                                                                         dict(name='energy', unit='eV')])],
                                  operations=[dict(id='energy', method='last', file='final.data',
                                                   x='step', y='energy', window=[0, 1])])))


def structural_proposal(frames):
    value = proposal()
    tables, operations, writes = [], [], []
    for index, interval in enumerate(frames):
        name = f'geometry_{index}.dump'
        tables.append(dict(file=name, format='lammps_dump', length_unit='angstrom',
                           elements={'1': 'Fe', '2': 'Ni'}, expected_counts={'1': 53, '2': 1},
                           pbc=[True, True, True]))
        operations.append(dict(id=f'order_{index}', method='warren_cowley_first_shell', file=name,
                               neighbors=8, neighbor_selection='nearest_k', frames=interval,
                               aggregation='equal_frame_mean', pair_mode='directed_and_symmetric'))
        writes.append(f'write_dump all custom /output/{name} id type x y z')
    value['analysis'].update(files=[t['file'] for t in tables], plan=dict(tables=tables, operations=operations))
    value['workflow'] = '\n'.join(['run 0', *writes])
    return value


class ProposalDiagnosticsTests(unittest.TestCase):
    def diagnose(self, value, **kwargs):
        return collect_proposal_diagnostics(value, max_atoms=100000, packages=['MC'], **kwargs)

    def test_valid_proposal_keeps_whole_validator_as_authority(self):
        value = proposal()
        with patch.object(agent_candidates, 'validate_proposal', wraps=agent_candidates.validate_proposal) as validator:
            self.assertEqual(self.diagnose(value), [])
        validator.assert_called_once()
        self.assertIsNot(validator.call_args.args[0], value)

    def test_aggregate_all_frame_caps_and_mc_keyword_errors_in_one_pass(self):
        value = structural_proposal([dict(first=0, last=8, stride=1), dict(first=1, last=100, stride=1)])
        value['workflow'] = ('fix first all atom/swap 1 4 17 300 1 2 ke no\n'
                             'fix second all atom/swap 1 4 17 300 ${pair_i} ${pair_j} ke no\n' + value['workflow'])
        diagnostics = self.diagnose(value)
        self.assertEqual([(d['code'], d['path']) for d in diagnostics], [
            ('analysis.frames_limit', '/analysis/plan/operations/0/frames'),
            ('analysis.frames_limit', '/analysis/plan/operations/1/frames'),
            ('workflow.atom_swap_types_keyword', '/workflow/lines/0'),
            ('workflow.atom_swap_types_keyword', '/workflow/lines/1')])

    def test_eight_frames_per_operation_do_not_invent_an_aggregate_cap(self):
        value = structural_proposal([dict(first=0, last=7, stride=1), dict(first=10, last=24, stride=2)])
        self.assertEqual(self.diagnose(value), [])

    def test_frame_intervals_are_checked_even_if_another_operation_field_fails(self):
        value = structural_proposal([dict(first=0, last=8, stride=0), dict(first=True, last=7, stride=1)])
        value['analysis']['plan']['operations'][0]['neighbors'] = 0
        diagnostics = self.diagnose(value)
        self.assertEqual(sum(d['code'] == 'analysis.frames_interval' for d in diagnostics), 2)
        self.assertTrue(any(d['code'] == 'analysis.operation' and d['path'].endswith('/0') for d in diagnostics))

    def test_keyword_syntax_is_independent_of_engine_package_availability(self):
        value = proposal()
        value['workflow'] = 'fix exchange all atom/swap 1 4 17 300 1 2 ke no\n' + value['workflow']
        diagnostics = collect_proposal_diagnostics(value, max_atoms=100000, packages=[])
        self.assertIn('workflow.atom_swap_types_keyword', [d['code'] for d in diagnostics])
        self.assertTrue(any('requires MC' in d['message'] for d in diagnostics))

    def test_all_obviously_invalid_literal_type_pairs_are_localized(self):
        value = proposal()
        value['workflow'] = ('fix first all atom/swap 1 4 17 300 types 1 1 ke no\n'
                             'fix second all atom/swap 1 4 17 300 types -1 2 ke no\n'
                             'fix third all atom/swap 1 4 17 300 types 1\n' + value['workflow'])
        diagnostics = self.diagnose(value)
        self.assertEqual([d['path'] for d in diagnostics if d['code'] == 'workflow.atom_swap_types_pair'],
                         ['/workflow/lines/0', '/workflow/lines/1', '/workflow/lines/2'])

    def test_independent_initial_and_additional_structure_errors_are_aggregated(self):
        value = proposal()
        value['structure']['repeat'][0] = 0
        extra = proposal()['structure']
        extra['repeat'][1] = -1
        value['additional_structures'] = [dict(id='extra', structure=extra)]
        diagnostics = self.diagnose(value)
        self.assertEqual([d['path'] for d in diagnostics if d['code'] == 'structure.invalid'],
                         ['/structure', '/additional_structures/0/structure'])

    def test_missing_plan_and_bad_pin_do_not_mask_each_other(self):
        value = proposal()
        value['potential_pin'] = ''
        del value['analysis']['plan']
        diagnostics = self.diagnose(value, require_analysis_plan=True)
        self.assertEqual([d['code'] for d in diagnostics], ['potential.pin', 'analysis.plan_required'])

    def test_literal_types_with_sampler_variables_is_not_rejected_as_missing_keyword(self):
        value = proposal()
        value['workflow'] = ('begin_cycle observations 2\n'
                             'sample_swap_types pair 2 17\n'
                             'fix exchange all atom/swap 1 4 17 300 types ${pair_i} ${pair_j} ke no\n'
                             'run 1\nunfix exchange\nend_cycle observations\n'
                             'write_data /output/final.data')
        self.assertEqual(self.diagnose(value), [])

    def test_multiple_numeric_operations_reuse_original_plan_validator(self):
        value = proposal()
        plan = value['analysis']['plan']
        plan['operations'][0]['method'] = 'automatic_final'
        plan['operations'].append(dict(plan['operations'][0], id='second', method='invented_method'))
        with patch.object(analysis_v2, 'validate_plan', wraps=analysis_v2.validate_plan) as validator:
            diagnostics = self.diagnose(value)
        self.assertEqual([d['path'] for d in diagnostics if d['code'] == 'analysis.operation'],
                         ['/analysis/plan/operations/0', '/analysis/plan/operations/1'])
        self.assertGreaterEqual(validator.call_count, 3)

    def test_input_is_unchanged_even_when_authority_normalizes_output_names(self):
        value = proposal()
        value['analysis']['files'] = ['/output/final.data']
        before = json.dumps(value, sort_keys=True)
        self.assertEqual(self.diagnose(value), [])
        self.assertEqual(json.dumps(value, sort_keys=True), before)

    def test_original_whitespace_expansion_does_not_create_new_raw_length_limits(self):
        value = proposal()
        value['workflow'] = 'variable number equal 1 +' + ' ' * 100000 + '2\n' + value['workflow']
        before = deepcopy(value)
        agent_candidates.validate_proposal(deepcopy(value), max_atoms=100000, packages=['MC'])
        self.assertEqual(self.diagnose(value), [])
        self.assertEqual(value, before)

    def test_original_blank_lines_do_not_hide_later_mc_errors(self):
        value = proposal()
        value['workflow'] = '\n' * 2001 + ('fix first all atom/swap 1 4 17 300 1 2 ke no\n'
                                            'fix second all atom/swap 1 4 17 300 1 2 ke no\n') + value['workflow']
        diagnostics = self.diagnose(value)
        self.assertEqual([d['path'] for d in diagnostics if d['code'] == 'workflow.atom_swap_types_keyword'],
                         ['/workflow/lines/2001', '/workflow/lines/2002'])

    def test_independent_fields_and_unknown_commands_are_all_reported(self):
        value = proposal()
        value['summary'] = ''
        value['potential_pin'] = 'not-a-pin'
        value['workflow'] = 'invented_first 3\ninvented_second 4\n' + value['workflow']
        diagnostics = self.diagnose(value)
        self.assertIn(('proposal.text', '/summary'), [(d['code'], d['path']) for d in diagnostics])
        self.assertIn(('potential.pin', '/potential_pin'), [(d['code'], d['path']) for d in diagnostics])
        self.assertEqual(sum(d['code'] == 'workflow.command' for d in diagnostics), 2)

    def test_clarification_mode_does_not_require_runnable_fields(self):
        value = dict(summary='Synthetic clarification', questions=['Which synthetic condition?'],
                     structure=None, potential_pin=None, workflow=None, analysis=None)
        self.assertEqual(self.diagnose(value), [])
        value['workflow'] = 'run 0'
        self.assertEqual([d['code'] for d in self.diagnose(value)], ['proposal.clarification_mode'])

    def test_malformed_json_shapes_return_stable_records_without_python_errors(self):
        for value in (None, [], {}, dict(proposal(), analysis={'quantity': None, 'method': [],
                                                             'files': [[]], 'plan': {'tables': [False],
                                                                                     'operations': [7, {}]}})):
            with self.subTest(value=value):
                diagnostics = self.diagnose(value)
                self.assertTrue(diagnostics)
                for item in diagnostics:
                    self.assertEqual(set(item), {'code', 'path', 'message'})
                    self.assertTrue(all(isinstance(v, str) for v in item.values()))
                    self.assertNotIn('KeyError', item['message'])

    def test_diagnosis_has_no_network_accounting_or_execution_side_effects(self):
        value = proposal()
        before = deepcopy(value)
        with patch('subprocess.Popen', side_effect=AssertionError('No execution')), \
             patch('socket.socket', side_effect=AssertionError('No network')), \
             patch('sqlite3.connect', side_effect=AssertionError('No accounting')):
            self.assertEqual(self.diagnose(value), [])
        self.assertEqual(value, before)

    def test_no_scientific_pressure_conversion_or_last_frame_inference(self):
        value = proposal()
        value['analysis']['plan']['tables'][0]['columns'][1]['unit'] = 'GPa'
        value['analysis']['plan']['operations'][0]['window'] = [0, 100000]
        self.assertEqual(self.diagnose(value), [])


if __name__ == '__main__':
    unittest.main()
