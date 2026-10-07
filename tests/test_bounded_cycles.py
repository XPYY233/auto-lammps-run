"""Bounded compiler checks only: no engine, API, target data or simulation."""
import collections
import unittest

from auto_lammps.candidate_tools import cycle_metadata, expand_tools, check_table_writers


PLAN = {'tables': [{'file': 'energy.dat', 'columns': [
    {'name': 'step', 'unit': '1'}, {'name': 'energy', 'unit': 'eV'}]}]}


def body(name='sample', count=60000, types=3, seed=24680):
    return '\n'.join([
        f'begin_cycle {name} {count}',
        f'sample_swap_types {name} {types} {seed}',
        f'fix mc all atom/swap 1 10 34567 500 types ${{{name}_i}} ${{{name}_j}} ke no',
        'run 1', 'unfix mc',
        'fix dynamics all npt temp 500 500 0.1 iso 0 0 1',
        'run 2', 'unfix dynamics',
        'emit_table energy.dat "$(step) $(pe)"',
        f'end_cycle {name}'])


class BoundedCycleTests(unittest.TestCase):
    def test_metadata_counts_every_physical_command_not_submission(self):
        metadata = cycle_metadata('run 0\n'+body(count=60000)+'\nminimize 0 1e-8 100 1000')
        self.assertEqual(metadata['calculation_commands'], 120002)
        cycle = metadata['cycles'][0]
        self.assertEqual((cycle['count'], cycle['calculation_commands_per_cycle'],
                          cycle['calculation_commands_total']), (60000, 2, 120000))
        self.assertEqual(metadata['line_multipliers'][5], 60000)
        self.assertEqual(metadata['sample_variables']['sample_i']['range'], [1, 3])
        self.assertEqual(metadata['sample_variables']['sample_j']['cycle_id'], 'sample')
        self.assertEqual(metadata['sampling_rng'], 'shared_equal_style_stream')
        self.assertEqual(metadata['sampling_seed'], 24680)

    def test_default_retains_virtual_markers_for_the_parent_screen(self):
        expanded = expand_tools(body(), PLAN, '/output/')
        self.assertIn('begin_cycle sample 60000', expanded)
        self.assertIn('sample_swap_types sample 3 24680', expanded)
        self.assertIn('end_cycle sample', expanded)
        self.assertNotIn('jump ', expanded)
        self.assertEqual(cycle_metadata(expanded)['calculation_commands'], 120000)
        check_table_writers(expanded, PLAN, '/output/')

    def test_only_compiler_creates_closed_loop_and_frozen_samples(self):
        expanded = expand_tools(body(count=4), PLAN, '', lower_cycles=True)
        lines = expanded.splitlines()
        self.assertNotIn('begin_cycle', expanded)
        self.assertNotIn('end_cycle', expanded)
        self.assertNotIn('sample_swap_types', expanded)
        self.assertIn('variable __alr_cycle_sample loop 4', lines)
        self.assertIn('label __alr_label_sample', lines)
        self.assertIn('variable sample_i index $(floor(random(1,4,24680)):%.0f)', lines)
        self.assertIn('variable sample_j index $(v___alr_draw_sample+(v___alr_draw_sample>=v_sample_i):%.0f)', lines)
        self.assertEqual(lines[-2:], ['next __alr_cycle_sample', 'jump SELF __alr_label_sample'])
        self.assertLess(lines.index('variable sample_i delete'), lines.index('next __alr_cycle_sample'))
        self.assertLess(lines.index('variable sample_j delete'), lines.index('next __alr_cycle_sample'))
        self.assertIn('variable __alr_draw_sample delete', lines)

    def test_six_temperatures_are_sequential_finite_cycles(self):
        raw = '\n'.join(body(name=f't{temperature}', count=20).replace(' 500 ', f' {temperature} ')
                        for temperature in (300, 400, 500, 600, 700, 800))
        metadata = cycle_metadata(raw)
        expanded = expand_tools(raw, PLAN, '/output/', lower_cycles=True)
        self.assertEqual(len(metadata['cycles']), 6)
        self.assertEqual(metadata['calculation_commands'], 6*20*2)
        self.assertEqual(expanded.count(' loop 20'), 6)
        self.assertEqual(expanded.count('\nnext __alr_cycle_'), 6)
        self.assertEqual(expanded.count('\njump SELF __alr_label_'), 6)
        self.assertEqual(expanded.count('# columns:'), 1)
        self.assertEqual(expanded.count('# units:'), 1)
        self.assertEqual(expanded.count(' append /output/energy.dat'), 7)

    def test_headers_are_before_the_first_cycle_not_erased_per_iteration(self):
        expanded = expand_tools('run 0\n'+body(count=8), PLAN, '', lower_cycles=True)
        lines = expanded.splitlines()
        header = lines.index('print "# columns: step energy" file energy.dat')
        begin = lines.index('variable __alr_cycle_sample loop 8')
        self.assertLess(header, begin)
        self.assertEqual(sum(' file energy.dat' in line for line in lines), 1)
        check_table_writers(expanded, PLAN, '')

    def test_prior_table_initialization_is_reused(self):
        expanded = expand_tools('emit_table energy.dat "0 $(pe)"\n'+body(), PLAN, '', lower_cycles=True)
        self.assertEqual(expanded.count('# columns:'), 1)
        self.assertEqual(expanded.count('# units:'), 1)
        self.assertEqual(expanded.count('print "0 $(pe)" append energy.dat'), 1)

    def test_all_ordered_type_pairs_have_the_same_discrete_preimage(self):
        # Check the compiler's two-draw mapping, not a stochastic engine trace.
        for types in (2, 3, 5, 64):
            pairs = collections.Counter((first, second+(second >= first))
                                        for first in range(1, types+1)
                                        for second in range(1, types))
            self.assertEqual(len(pairs), types*(types-1))
            self.assertEqual(set(pairs.values()), {1})
            self.assertTrue(all(1 <= i <= types and 1 <= j <= types and i != j for i, j in pairs))

    def test_immediate_capture_formula_is_preserved_in_a_cycle(self):
        raw = 'begin_cycle energy 2\nrun 0\ncapture saved pe - v_offset\nend_cycle energy'
        expanded = expand_tools(raw, None, '', lower_cycles=True)
        self.assertIn('variable saved equal $(pe - v_offset)', expanded)
        self.assertEqual(cycle_metadata(raw)['calculation_commands'], 2)

    def test_literal_count_bound_is_technical_and_configurable(self):
        self.assertEqual(cycle_metadata(body(count=2), max_cycles=2)['cycles'][0]['count'], 2)
        for count in ('0', '-1', '1e3', '${count}', '1000001', '2147483648'):
            with self.subTest(count=count), self.assertRaises(ValueError):
                cycle_metadata(body().replace('60000', count, 1))
        for limit in (True, 0, -1, '2', 2147483648):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                cycle_metadata(body(count=1), max_cycles=limit)

    def test_matching_closure_uniqueness_and_non_nesting(self):
        invalid = [
            'begin_cycle one 2\nrun 1',
            'end_cycle one',
            'begin_cycle one 2\nrun 1\nend_cycle two',
            'begin_cycle one 2\nbegin_cycle two 2\nrun 1\nend_cycle two\nend_cycle one',
            'begin_cycle one 2\nrun 1\nend_cycle one\nbegin_cycle one 2\nrun 1\nend_cycle one',
            'begin_cycle one 2\nprint "not physics"\nend_cycle one',
            'begin_cycle ../one 2\nrun 1\nend_cycle ../one',
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                expand_tools(raw, None, '', lower_cycles=True)

    def test_native_controls_and_reserved_names_never_lower(self):
        for raw in ('label loop', 'next other', 'jump SELF loop', 'clear', 'include other.lmp',
                    'shell echo hacked', 'if "1" then "run 0"', 'variable user loop 100',
                    'variable source file values', 'variable __alr_cycle_sample equal 0',
                    'print "${__alr_cycle_sample}"', 'capture __alr_draw_sample 1'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                expand_tools(raw+'\n'+body(count=1), PLAN, '', lower_cycles=True)

    def test_cycle_cannot_rebuild_or_overwrite_the_physical_history(self):
        for command in ('load_structure another', 'reset_structure initial', 'delete_atoms group vacancy compress no',
                        'change_box all x scale 1.1', 'reset_timestep 0', 'displace_atoms all move 1 0 0',
                        'dump d all atom 1 dump.dat', 'write_data initial.data',
                        'write_dump all atom dump.dat', 'print "row" file energy.dat',
                        'fix a all ave/time 1 1 1 c_pe file energy.dat', 'variable forever index 1'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                cycle_metadata('begin_cycle stable 2\n'+command+'\nrun 1\nend_cycle stable')

    def test_sampling_rejects_seed_or_rng_semantics_which_do_not_hold(self):
        invalid = [body()+'\n'+body('second', seed=24681),
                   'variable rand equal random(1,2,99)\n'+body(),
                   body().replace('run 1', 'capture rand normal(0,1,99)\nrun 1'),
                   'sample_swap_types pair 3 99\nrun 0',
                   body().replace('sample_swap_types sample 3 24680', 'sample_swap_types sample 1 24680'),
                   body().replace('sample_swap_types sample 3 24680', 'sample_swap_types sample 65 24680'),
                   body().replace('sample_swap_types sample 3 24680', 'sample_swap_types sample 3 0'),
                   body().replace('sample_swap_types sample 3 24680', 'sample_swap_types sample 3 ${seed}'),
                   body()+'\n'+body(),
                   body().replace('run 1', 'sample_swap_types sample 3 24680\nrun 1')]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                cycle_metadata(raw)

    def test_sample_variables_cannot_escape_be_redefined_or_exist_early(self):
        for raw in (body()+'\nprint "${sample_i}"',
                    'variable sample_i equal 1\n'+body(),
                    body().replace('run 1', 'variable sample_j delete\nrun 1'),
                    body().replace('run 1', 'capture sample_i 2\nrun 1'),
                    body().replace('sample_swap_types sample 3 24680', 'print "${sample_i}"\nsample_swap_types sample 3 24680'),
                    body()+'\n'+body('another').replace('run 1', 'capture wrong v_sample_j\nrun 1')):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                expand_tools(raw, PLAN, '', lower_cycles=True)

    def test_comments_do_not_supply_counts_or_instructions(self):
        raw = '# jump SELF bogus\n'+body(count=1)+' # closing comment\n# variable __alr_hack index 0'
        self.assertEqual(cycle_metadata(raw)['calculation_commands'], 2)
        self.assertIn('jump SELF __alr_label_sample', expand_tools(raw, PLAN, '', lower_cycles=True))

    def test_cycle_step_counts_cannot_dispatch_hidden_commands(self):
        for command in ('run ${steps}', 'run -1', 'run 1 every 1 "jump SELF other"',
                        'run 1 upto', 'run 1 start 0 stop 100', 'run 1 pre yes pre no',
                        'minimize 0 1e-8 100 ${limit}', 'minimize NaN 1e-8 100 1000',
                        'minimize 0 1e-8 0 1000', 'capture escaped next(counter)',
                        'variable alias equal v_sample_i'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                cycle_metadata(body().replace('run 1', command))
        valid = body().replace('run 1', 'run 1 pre no post yes').replace('run 2', 'minimize 0 1e-8 100 1000')
        self.assertEqual(cycle_metadata(valid)['calculation_commands'], 120000)
        valid = body().replace('run 1', 'capture alias v_sample_i\nrun 1')
        self.assertIn('variable alias equal $(v_sample_i)', expand_tools(valid, PLAN, '', lower_cycles=True))

    def test_compute_and_dump_lifecycle_cannot_break_on_a_second_iteration(self):
        for command in ('compute pressure all pressure thermo_temp', 'uncompute pressure', 'undump trajectory'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                cycle_metadata(body().replace('run 1', command+'\nrun 1'))
        raw = ('compute pressure all pressure thermo_temp\n'
               'dump trajectory all custom 3 trajectory.dat id type x y z\n'+body(count=2)+
               '\nuncompute pressure\nundump trajectory')
        metadata = cycle_metadata(raw)
        self.assertEqual(metadata['calculation_commands'], 4)
        expanded = expand_tools(raw, PLAN, '', lower_cycles=True)
        self.assertLess(expanded.index('compute pressure'), expanded.index('label __alr_label_'))
        self.assertGreater(expanded.index('uncompute pressure'), expanded.index('jump SELF __alr_label_'))
        self.assertGreater(expanded.index('undump trajectory'), expanded.index('jump SELF __alr_label_'))

    def test_technical_block_limit_remains_finite(self):
        raw = '\n'.join(f'begin_cycle c{i} 1\nrun 0\nend_cycle c{i}' for i in range(64))
        self.assertEqual(len(cycle_metadata(raw)['cycles']), 64)
        with self.assertRaises(ValueError):
            cycle_metadata(raw+'\nbegin_cycle more 1\nrun 0\nend_cycle more')


if __name__ == '__main__':
    unittest.main()
