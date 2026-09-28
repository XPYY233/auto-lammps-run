"""就绪门与深度引用核验的离线测试（不联网）。"""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_resource_ready import deep_check, gate, load

CATALOG = dict(generated='2026-01-01', tier_a=[
    dict(repo='synthetic/ready', commit='a'*40, title='就绪资源包', doi='10.1/ready',
         pair_style=['sw'], potentials=['Si.sw'], inputs=['lammps/in.shear'], elements=['Si'],
         license='MIT', closure=dict(state='built_in_script')),
    dict(repo='synthetic/no-entry', commit='b'*40, title='无入口', doi='10.1/x',
         pair_style=['eam'], potentials=['Cu.eam'], inputs=[], elements=['Cu'], license='MIT',
         closure=dict(state='read_data_resolved')),
    dict(repo='synthetic/missing-structure', commit='c'*40, title='缺结构', doi='10.1/y',
         pair_style=['eam'], potentials=['Cu.eam'], inputs=['in.run'], elements=['Cu'], license='MIT',
         closure=dict(state='read_data_missing', read_data_missing=['cell.data'])),
    dict(repo='synthetic/no-license', commit='d'*40, title='缺许可', doi='10.1/z',
         pair_style=['eam'], potentials=['Cu.eam'], inputs=['in.run'], elements=['Cu'], license='未声明',
         closure=dict(state='read_data_resolved')),
    dict(repo='synthetic/registry', commit='', title='注册表记录', doi='10.1/r',
         source=dict(type='nist_ipr', locator='x', version='v1'), pair_style=['meam'],
         potentials=['x.meam'], inputs=[], elements=['Cu'], license='未声明',
         closure=dict(state='not_applicable_registry_record')),
], tier_b=[], tier_c=[])

SCREENING = dict(bundles={
    'synthetic/ready': dict(decl=[dict(line='pair_coeff * * Si.sw Si')]),
    'synthetic/no-license': dict(decl=[dict(line='pair_coeff * * library.meam Cu Cu.eam Cu')]),
})


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.catalog=self.root/'catalog.json'
        self.catalog.write_text(json.dumps(CATALOG, ensure_ascii=False))

    def rows(self, screening=None):
        rows, bundles = load(self.catalog, screening)
        return {row['repo']: gate(tier, row, bundles) for tier, row in rows}

    def test_statuses_without_screening_are_conservative(self):
        statuses=self.rows()
        self.assertEqual(statuses['synthetic/ready'][0], 'ready_candidate')
        self.assertIn('未做声明级势函数核验', ' '.join(statuses['synthetic/ready'][1]))
        self.assertEqual(statuses['synthetic/no-entry'][0], 'blocked_no_entry')
        self.assertEqual(statuses['synthetic/missing-structure'][0], 'blocked_missing_structure')
        self.assertEqual(statuses['synthetic/no-license'][0], 'blocked_license')
        self.assertEqual(statuses['synthetic/registry'][0], 'registry_metadata_only')

    def test_declared_potential_missing_blocks_the_bundle(self):
        rows, bundles = load(self.catalog, None)
        row = dict(next(row for tier, row in rows if row['repo'] == 'synthetic/ready'))
        row['potentials'] = []
        self.assertEqual(gate('tier_a', row, bundles)[0], 'ready_candidate')  # 无 screening 时不断言
        screening=self.root/'screening.json'
        screening.write_text(json.dumps(SCREENING, ensure_ascii=False))
        rows, bundles = load(self.catalog, screening)
        row = dict(next(row for tier, row in rows if row['repo'] == 'synthetic/ready'))
        row['potentials'] = []
        status, reasons = gate('tier_a', row, bundles)
        self.assertEqual(status, 'blocked_missing_potential')
        self.assertIn('Si.sw', reasons[0])

    def test_deep_check_resolves_relative_paths_and_flags_staging(self):
        tree = ['lammps/in.shear', 'lammps/Si.sw',
                'PSO/Demo_results/0/GSFE1/GSFE.in', 'PSO/Demo_results/0/library.meam',
                'LAMMPS_Input/cluster_shear.in', 'Potential/library.meam']
        files = {
            'lammps/in.shear': 'pair_coeff * * Si.sw Si\n',
            'PSO/Demo_results/0/GSFE1/GSFE.in': 'pair_coeff * * ../library.meam Cu Ni\n',
            'LAMMPS_Input/cluster_shear.in': 'read_data cell.lmp\npair_coeff * * library.meam Mg\n',
        }
        fetch_tree=lambda repo: tree
        fetch_file=lambda repo, path: files.get(path, '')
        same=deep_check('synthetic/ready', ['lammps/in.shear'], fetch_tree=fetch_tree, fetch_file=fetch_file)
        self.assertEqual(same['findings'], [])
        self.assertEqual(same['references']['Si.sw'], 'lammps/Si.sw')
        parent=deep_check('synthetic/ready', ['PSO/Demo_results/0/GSFE1/GSFE.in'], fetch_tree=fetch_tree, fetch_file=fetch_file)
        self.assertEqual(parent['findings'], [])
        self.assertEqual(parent['references']['library.meam'], 'PSO/Demo_results/0/library.meam')
        staging=deep_check('synthetic/ready', ['LAMMPS_Input/cluster_shear.in'], fetch_tree=fetch_tree, fetch_file=fetch_file)
        self.assertTrue(any('必须先与入口暂存到同一运行目录' in item for item in staging['findings']))
        self.assertTrue(any('引用文件在仓库中不存在' in item for item in staging['findings']))


if __name__ == '__main__':
    unittest.main()
