"""Synthetic native scalar output: arithmetic, provenance and failure cases."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from auto_lammps.analysis import AnalysisError
from auto_lammps.scalar_analysis import analyze_scalar, parse_scalar, validate_table
from auto_lammps.manifest import sha256

TABLE = dict(file='curve.dat', format='lammps_ave_time_scalar',
             headers=['# Time-averaged data for fix stress', '# TimeStep v_strain v_stress'],
             columns=[dict(name='step', unit='step', source='TimeStep'),
                      dict(name='strain', unit='1', source='v_strain'),
                      dict(name='stress', unit='GPa', source='v_stress')],
             steps=dict(first=100, last=300, stride=100))
DATA = b'# Time-averaged data for fix stress\n# TimeStep v_strain v_stress\n100 0 1\n200 1 3\n300 2 5\n'
OPS = [dict(id=m, method=m, file='curve.dat', x='strain', y='stress', window=[0, 2])
       for m in ('summary', 'last', 'linear_fit')]


class ScalarAnalysisTests(unittest.TestCase):
    def test_known_arithmetic_and_original_provenance(self):
        report = analyze_scalar(DATA, TABLE, OPS)
        self.assertEqual(report['source']['sha256'], sha256(DATA))
        self.assertEqual(report['source']['columns'], TABLE['columns'])
        summary, last, fit = report['results']
        self.assertEqual(summary['values'], dict(mean=3.0, sample_std=2.0, min=1.0, max=5.0))
        self.assertEqual(last['values'], dict(value=5.0, x=2.0))
        self.assertEqual(fit['values'], dict(slope=2.0, intercept=1.0, rmse=0.0, r_squared=1.0))
        self.assertEqual(fit['source_line_ranges'], [[3, 5]])
        self.assertEqual(report['scientific_status'], 'not_evaluated')
        self.assertEqual(report['execution_status'], 'not_checked')
        self.assertEqual(report['plan_status'], 'caller_supplied')
        self.assertEqual(fit['units_origin'], 'declared_only_not_present_in_scalar_header')

    def test_crlf_and_blank_lines_keep_source_positions(self):
        data = b'\n' + DATA.replace(b'200 1 3\n', b'\n200 1 3\n').replace(b'\n', b'\r\n')
        report = analyze_scalar(data, TABLE, OPS)
        self.assertEqual(report['results'][0]['source_line_ranges'], [[4, 4], [6, 7]])
        self.assertEqual(report['source']['sha256'], sha256(data))
        self.assertEqual(report['results'][2]['values']['slope'], 2)

    def test_truncated_rows_and_missing_complete_lines_rejected(self):
        for data in [DATA[:-1], DATA.rsplit(b'300', 1)[0], DATA.replace(b'200 1 3\n', b''),
                     DATA.replace(b'300 2 5\n', b'300 2\n'), b'', DATA.split(b'100')[0]]:
            with self.subTest(data=data), self.assertRaises(AnalysisError):
                parse_scalar(data, TABLE)

    def test_wrong_stage_column_order_restart_and_vector_blocks_rejected(self):
        variants = [DATA.replace(b'fix stress', b'fix heat'),
                    DATA.replace(b'# TimeStep v_strain v_stress', b'# TimeStep v_stress v_strain'),
                    DATA + DATA, DATA.replace(b'200 1 3', b'100 1 3'),
                    DATA.replace(b'200 1 3', b'250 1 3'),
                    DATA.replace(b'100 0 1', b'100 2\n1 0 1\n2 1 3'),
                    DATA.replace(b'100 0 1', b'1e2 0 1')]
        for data in variants:
            with self.subTest(data=data), self.assertRaises(AnalysisError):
                parse_scalar(data, TABLE)

    def test_nonfinite_errors_and_unexpected_comments_rejected(self):
        for replacement in [b'nan', b'inf', b'1e999', b'ERROR', b'1 2', b'\xff', b'1\x001']:
            with self.subTest(replacement=replacement), self.assertRaises(AnalysisError):
                parse_scalar(DATA.replace(b'200 1 3', b'200 1 '+replacement), TABLE)
        with self.assertRaises(AnalysisError):
            parse_scalar(DATA+b'# end\n', TABLE)

    def test_integer_timestep_precision_retained(self):
        table = deepcopy(TABLE)
        first = 2**53
        table['steps'] = dict(first=first, last=first+2, stride=1)
        data = ('\n'.join(table['headers'])+'\n'+''.join(f'{first+i} {i} {i*2+1}\n' for i in range(3))).encode()
        rows = parse_scalar(data, table)
        self.assertEqual([r[1][0] for r in rows], [first, first+1, first+2])
        self.assertTrue(all(type(r[1][0]) is int for r in rows))

    def test_invalid_declarations_and_inferred_units_rejected(self):
        variants = []
        for field, value in [('format','vector'), ('steps',dict(first=100,last=301,stride=100)),
                             ('steps',dict(first=True,last=300,stride=100)),
                             ('steps',dict(first=100,last=300,stride=0)),
                             ('headers',['# any', '# TimeStep v_stress v_strain'])]:
            table=deepcopy(TABLE);table[field]=value;variants.append(table)
        for column, field, value in [(0,'unit','1'), (1,'unit','guess'), (1,'name','../x'),
                                     (1,'source','v_stress'), (1,'source','v_x;exec')]:
            table=deepcopy(TABLE);table['columns'][column][field]=value;variants.append(table)
        for table in variants:
            with self.subTest(table=table), self.assertRaises(AnalysisError):validate_table(table)

    def test_limits_and_no_answer_or_automatic_conversion(self):
        with patch('auto_lammps.analysis.MAX_TABLE_BYTES', 8), self.assertRaises(AnalysisError):
            parse_scalar(DATA, TABLE)
        with patch('auto_lammps.analysis.MAX_ROWS', 2), self.assertRaises(AnalysisError):
            parse_scalar(DATA, TABLE)
        table=deepcopy(TABLE);table['columns'][2]['unit']='bar'
        report=analyze_scalar(DATA,table,OPS)
        self.assertEqual(report['results'][2]['values']['slope'], 2)
        self.assertEqual(report['results'][2]['value_units']['slope'], 'bar')
        self.assertNotIn('score', report)

    def test_window_and_source_are_frozen_in_report_identity(self):
        a=analyze_scalar(DATA,TABLE,OPS)
        ops=deepcopy(OPS);ops[0]['window']=[1,2]
        b=analyze_scalar(DATA,TABLE,ops)
        self.assertNotEqual(a['plan_sha256'],b['plan_sha256'])
        self.assertEqual(b['results'][0]['source_line_ranges'],[[4,5]])
        self.assertEqual(b['results'][0]['sample_count'],2)
