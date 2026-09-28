import csv
import json
from pathlib import Path
import tempfile
import unittest

from auto_lammps.analysis_runtime import versions
from auto_lammps.retained_analysis import analyze, digest


@unittest.skipUnless(all(p['ready'] for p in versions().values()), 'analysis extra not installed')
class RetainedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'bcc.dump'
        points = [(3*(x+b), 3*(y+b), 3*(z+b))
                  for x in range(4) for y in range(4) for z in range(4) for b in (0, .5)]
        text = 'ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n128\nITEM: BOX BOUNDS pp pp pp\n0 12\n0 12\n0 12\nITEM: ATOMS id type x y z\n'
        self.source.write_text(text + ''.join(f'{i} 1 {x} {y} {z}\n' for i, (x,y,z) in enumerate(points, 1)))
        self.plan = dict(source=dict(path=str(self.source), sha256=digest(self.source)),
                         kind='cna', frames=[0], elements={'1': 'Fe'}, length_unit='angstrom')

    def test_bcc_and_receipt(self):
        output = self.root / 'analysis'
        report = analyze(self.plan, output)
        with (output / 'analysis.csv').open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(int(row['BCC']), 128)
        self.assertEqual(float(row['cell_x_strain']), 0)
        self.assertIsNone(report['scientific_pass'])
        self.assertFalse(report['physics_simulation'])
        for artifact in report['files']:
            self.assertEqual(digest(output / artifact['name']), artifact['sha256'])
        with self.assertRaises(FileExistsError):
            analyze(self.plan, output)

    def test_rdf_is_single_snapshot(self):
        self.plan.update(kind='rdf_snapshot', cutoff=4., bins=40)
        output = self.root / 'rdf'
        report = analyze(self.plan, output)
        with (output / 'analysis.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 40)
        self.assertIn('Fe-Fe', rows[0])
        self.assertGreater(max(float(r['Fe-Fe']) for r in rows), 0)
        self.assertEqual(report['frames'], [0])

    def test_source_tamper_rejected_before_output(self):
        self.source.write_text(self.source.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'differs'):
            analyze(self.plan, self.root / 'output')
        self.assertFalse((self.root / 'output').exists())

    def test_missing_frame_rejected(self):
        self.plan['frames'] = [1]
        with self.assertRaisesRegex(ValueError, 'frames'):
            analyze(self.plan, self.root / 'output')

    def test_rdf_rejects_insufficient_periodic_cell(self):
        self.plan.update(kind='rdf_snapshot', cutoff=6., bins=40)
        with self.assertRaisesRegex(ValueError, 'half'):
            analyze(self.plan, self.root / 'output')
        self.assertFalse((self.root / 'output' / 'receipt.json').exists())

    def test_unknown_element_rejected(self):
        self.plan['elements'] = {'2': 'Fe'}
        with self.assertRaisesRegex(ValueError, 'Unmapped'):
            analyze(self.plan, self.root / 'output')


if __name__ == '__main__':
    unittest.main()
