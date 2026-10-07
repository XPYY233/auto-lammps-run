"""Independent predeclared conditions reuse one immutable initial geometry."""
import json
import unittest
from unittest.mock import patch

from auto_lammps.agent_candidates import CandidateError, validate_body
from auto_lammps.authorization import candidate_check
import test_agent_candidates as fixture


class InitialResetScreenTests(unittest.TestCase):
    def test_each_condition_is_counted_without_duplicating_geometry(self):
        body='run 0\n'+('reset_structure initial\nrun 2\n'*8)+'print "0 -1" file /output/result.dat'
        screen=validate_body(body,['result.dat'],structures={'initial':4},type_count=1)
        self.assertEqual(screen['calculation_commands'],9)
        self.assertFalse(screen['execution_authorized'])

    def test_reload_restores_atom_ids_after_a_declared_vacancy(self):
        body=('group defect id 4\ndelete_atoms group defect compress no\nrun 0\n'
              'reset_structure initial\ngroup newdefect id 4\n'
              'delete_atoms group newdefect compress no\nrun 0\n'
              'print "0 -1" file /output/result.dat')
        validate_body(body,['result.dat'],structures={'initial':4},type_count=1)

    def test_reset_does_not_erase_surviving_input_variables(self):
        body='variable n index 1\nrun 0\nreset_structure initial\nvariable n index 2\nprint "0 -1" file /output/result.dat'
        with self.assertRaisesRegex(CandidateError,'survives'):
            validate_body(body,['result.dat'],structures={'initial':4},type_count=1)

    def test_only_exact_initial_reset_is_a_tool(self):
        for line in ('reset_structure other','reset_structure initial extra','reset_structure',
                     'reset_structure ../structure.data','read_data structure.data','clear'):
            with self.subTest(line=line),self.assertRaises(CandidateError):
                validate_body('run 0\n'+line+'\nprint "0 -1" file /output/result.dat',
                              ['result.dat'],structures={'initial':4},type_count=1)


class InitialResetCandidateTests(unittest.TestCase):
    setUp=fixture.AgentCandidateTests.setUp
    generate=fixture.AgentCandidateTests.generate

    def test_generation_and_grant_recheck_reload_identical_frozen_inputs(self):
        lines=['run 0','print "# columns: x y" file /output/final.data',
               'print "# units: 1 eV" append /output/final.data','print "0 -1" append /output/final.data']
        for index in range(1,9):
            lines.extend(['reset_structure initial','run 0',f'print "{index} -1" append /output/final.data'])
        self.value['workflow']='\n'.join(lines)
        with patch('subprocess.Popen',side_effect=AssertionError('no engine or network')):
            result=self.generate()
            check=candidate_check(result['snapshot'],max_atoms=100000)
        script=(result['snapshot'].path/'in.lammps').read_text()
        self.assertEqual(script.count('read_data structure.data'),9)
        self.assertEqual(script.count('\nclear\n'),8)
        self.assertNotIn('reset_structure',script)
        record=json.loads((result['snapshot'].path/'generation.json').read_text())
        self.assertEqual(check['screen'],record['script_screen'])
        self.assertEqual(check['screen']['calculation_commands'],9)
        self.assertFalse(record['execution_authorized'])
