"""HPC inventory: read-only by construction, failures recorded instead of guessed."""
import json
from pathlib import Path
import tempfile
import unittest

from auto_lammps.hpc_environment import (HPCEnvironment, InventoryCommand, discover_modules,
                                         inventory_commands, module_probe_command, parse_engine,
                                         run_commands, summarize)
from auto_lammps.tasks import TaskError, TaskStore

MODULE_AVAIL = """
------------------------ /dssg/opt/modulefiles ------------------------
lammps/20230328-intel-2021.4.0-omp   lammps/20240829.1-intel-2021.4.0
lammps/20231121-intel-2021.4.0-kim   lammps/20250722-intel-2021.4.0
"""

# 真实 help 格式（取自集群实际输出）：版本行第二种写法 + 空格分隔的包清单。
LMP_HELP = """load_rc=0
/dssg/opt/icelake/linux-centos8-icelake/contribute/lammps/lammps-28Mar2023/bin/lmp

Large-scale Atomic/Molecular Massively Parallel Simulator - 28 Mar 2023

Usage example: lmp -var t 300 -echo screen -in in.alloy

Installed packages:

AMOEBA ASPHERE BOCS BODY BPM BROWNIAN CG-DNA CLASS2 COLLOID COLVARS
COMPRESS CORESHELL DIELECTRIC DIPOLE DPD-BASIC DRUDE EFF ELECTRODE
GRANULAR INTEL INTERLAYER KSPACE MANYBODY MC MEAM MISC ML-IAP ML-POD ML-SNAP
MOLECULE OPENMP OPT PERI PHONON PLUGIN QEQ REAXFF REPLICA RIGID SHOCK SPH
SPIN SRD TALLY VORONOI

List of individual styles:
"""


class InventoryCommandTests(unittest.TestCase):
    def test_command_set_is_read_only(self):
        commands = inventory_commands('/dssg/home/acct/x')
        self.assertTrue(commands)
        for command in commands:
            lowered = command.remote.lower()
            for token in ('sbatch', 'srun', 'scancel', 'rm ', 'pip install', 'conda install'):
                self.assertNotIn(token, lowered, command.remote)

    def test_write_or_submission_is_refused(self):
        for remote in ('sbatch job.sh', 'srun lmp -in in.lmp', 'rm -rf /tmp/x', 'module load a > out'):
            with self.assertRaises(TaskError):
                InventoryCommand('bad', remote)

    def test_module_probe_loads_one_module_and_asks_for_help(self):
        command = module_probe_command('lammps/20230328-intel-2021.4.0-omp')
        self.assertIn('module load lammps/20230328-intel-2021.4.0-omp', command.remote)
        self.assertIn('lmp -h', command.remote)
        with self.assertRaises(TaskError):
            module_probe_command('lammps/x; sbatch job.sh')


class ParsingTests(unittest.TestCase):
    def results(self):
        return run_commands(lambda remote, timeout, max_bytes: dict(
            returncode=0, stdout=MODULE_AVAIL if 'module avail' in remote else LMP_HELP,
            stderr='', truncated=False), inventory_commands('/dssg/home/x'))

    def test_modules_and_engines_are_read_from_output(self):
        results = self.results()
        modules = discover_modules(results)
        self.assertIn('lammps/20230328-intel-2021.4.0-omp', modules)
        self.assertIn('lammps/20250722-intel-2021.4.0', modules)

    def test_engine_help_yields_version_packages_and_pair_styles(self):
        item = dict(name='engine:x', returncode=0, stdout=LMP_HELP, stdout_sha256='a' * 64)
        engine = parse_engine(item)
        self.assertEqual(engine['version'], '28 Mar 2023')
        self.assertEqual(engine['binary'], '/dssg/opt/icelake/linux-centos8-icelake/contribute/lammps/lammps-28Mar2023/bin/lmp')
        self.assertIn('MEAM', engine['installed_packages'])
        self.assertTrue(engine['meam_package'])
        self.assertIn('MANYBODY', engine['installed_packages'])

    def test_summary_states_lammps_is_present_and_forbids_a_build_claim(self):
        summary = summarize(self.results() + [dict(name='engine:lammps/20230328-intel-2021.4.0-omp',
                                                   returncode=0, stdout=LMP_HELP, stderr='',
                                                   stdout_sha256='b' * 64, truncated=False, remote='')])
        self.assertTrue(summary['lammps_present'])
        self.assertIn('不得声称', summary['note'])
        self.assertTrue(summary['usable_engines'])

    def test_a_failed_command_is_recorded_not_hidden(self):
        def broken(remote, timeout, max_bytes):
            raise TimeoutError('no answer')
        results = run_commands(broken, [InventoryCommand('x', 'hostname')])
        self.assertEqual(results[0]['returncode'], 255)
        self.assertIn('TimeoutError', results[0]['stderr'])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=TaskStore(Path(self.temp.name)/'tasks.sqlite')

        class Connections:
            @staticmethod
            def ssh_arguments(revision): return ['ssh', 'alias']
        self.environment=HPCEnvironment(self.store, Connections())

    def test_report_is_stored_and_read_back_with_digests(self):
        runner=lambda remote, timeout, max_bytes: dict(
            returncode=0, stdout=MODULE_AVAIL if 'module avail' in remote else LMP_HELP,
            stderr='', truncated=False)
        document=self.environment.run(1, runner, work_directory='/dssg/home/x')
        self.assertTrue(document['summary']['lammps_present'] or document['summary']['modules_available'])
        for command in document['commands']:
            self.assertEqual(len(command['stdout_sha256']), 64)
        latest=self.environment.latest(1)
        self.assertEqual(latest['at'], document['at'])
        self.assertEqual(latest['summary']['modules_available'], document['summary']['modules_available'])

    def test_nothing_is_stored_before_a_run(self):
        self.assertIsNone(self.environment.latest(1))


if __name__ == '__main__':
    unittest.main()
