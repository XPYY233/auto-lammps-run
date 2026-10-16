"""Synthetic format/arithmetic checks; no papers, model, SSH or simulation."""
import copy
import csv
import io
import math
import statistics
import unittest
from unittest.mock import patch

from auto_lammps import postprocess_tables as tool
from auto_lammps.manifest import canonical, sha256


def column(table='samples', name='value', row=None):
    result = dict(table=table, column=name)
    if row is not None:
        result['row'] = row
    return result


def binary(op, left, right):
    return dict(op=op, left=left, right=right)


class PostprocessTableTests(unittest.TestCase):
    def setUp(self):
        self.files = {}
        self.plan = dict(version=1, tables=[], operations=[])
        self.add_table('samples', b'# heading\r\n 1 \r\n2\r\n3', header_lines=1)
        self.plan['operations'].append(dict(id='summary1', method='summary', table='samples', column='value'))

    def add_table(self, name, data, *, header_lines=0, format='whitespace', columns=None):
        file = name + '.dat'
        self.files[file] = data
        declaration = dict(id=name, file=file, sha256=sha256(data), format=format,
                           header_lines=header_lines, columns=columns or [dict(name='value', unit='1')])
        self.plan['tables'].append(declaration)
        return declaration

    def replace_data(self, data, table=0):
        declaration = self.plan['tables'][table]
        self.files[declaration['file']] = data
        declaration['sha256'] = sha256(data)

    def derive(self, expression, name='derived', columns=None):
        operation = dict(id=name, method='derive', alignment='row_index',
                         columns=columns or [dict(name='result', unit='1', expression=expression)])
        self.plan['operations'] = [operation]
        return operation

    def execute(self, plan=None, files=None):
        return tool.execute_plan(self.plan if plan is None else plan, self.files if files is None else files)

    def reject(self, plan=None, files=None):
        with self.assertRaises(tool.PostprocessError):
            self.execute(plan, files)

    def test_one_column_raw_lines_source_numbers_and_summary(self):
        before = copy.deepcopy(self.plan)
        raw = dict(self.files)
        report = self.execute()
        source = report['sources'][0]
        self.assertEqual(source['rows'], [[1.0], [2.0], [3.0]])
        self.assertEqual(source['line_numbers'], [2, 3, 4])
        self.assertEqual(source['raw_lines'], ['# heading\r\n', ' 1 \r\n', '2\r\n', '3'])
        self.assertEqual(''.join(source['raw_lines']).encode('ascii'), raw['samples.dat'])
        self.assertEqual(source['sha256'], sha256(raw['samples.dat']))
        self.assertEqual(report['results'][0]['values'],
                         dict(count=3, mean=2.0, sample_std=1.0, min=1.0, max=3.0, median=2.0))
        self.assertEqual(report['results'][0]['row_selection'], dict(start=0, stop=3, step=1))
        self.assertEqual(report['scientific_status'], 'not_evaluated')
        self.assertEqual(report['plan_sha256'], sha256(canonical(self.plan)))
        self.assertEqual(self.plan, before)
        self.assertEqual(self.files, raw)
        self.assertEqual(report['adapter_identity'], tool.adapter_identity())

    def test_csv_uses_explicit_header_count_and_all_declared_columns(self):
        self.plan['tables'] = []
        self.files = {}
        self.add_table('samples', b'ignored,header\n"1", 2 \n3,4\n', format='csv', header_lines=1,
                       columns=[dict(name='x', unit='ps'), dict(name='y', unit='eV/atom')])
        self.plan['operations'] = [dict(id='plot', method='line', table='samples', x='x', y='y')]
        result = self.execute()['results'][0]
        self.assertEqual(result['points'], [[1., 2.], [3., 4.]])
        self.assertEqual(result['value_units'], dict(x='ps', y='eV/atom'))
        self.assertEqual(result['units_origin'], 'declared_not_inferred')

    def test_all_four_structured_operations_and_complete_csv(self):
        expressions = [binary('add', column(), dict(literal=2, evidence='metadata:two')),
                       binary('subtract', column(), dict(literal=2, evidence='metadata:two')),
                       binary('multiply', column(), dict(literal=2, evidence='metadata:two')),
                       binary('divide', column(), dict(literal=2, evidence='metadata:two'))]
        columns = [dict(name='value' + str(i), unit='1', expression=expression) for i, expression in enumerate(expressions)]
        self.derive(None, columns=columns)
        report = self.execute()
        derived = report['derived_tables'][0]
        self.assertEqual(derived['rows'], [[3., -1., 2., .5], [4., 0., 4., 1.], [5., 1., 6., 1.5]])
        csv_rows = list(csv.reader(io.StringIO(derived['csv'])))
        self.assertEqual(csv_rows[0], [item['name'] for item in columns])
        self.assertEqual([[float(value) for value in row] for row in csv_rows[1:]], derived['rows'])
        self.assertEqual(sha256(derived['csv'].encode('ascii')), derived['csv_sha256'])
        self.assertEqual(derived['references'][0]['source_lines'], [2, 3, 4])
        self.assertEqual(derived['columns'][0]['expression']['right']['evidence'], 'metadata:two')

    def test_scalar_source_row_is_explicit_and_preserves_precise_line(self):
        self.add_table('baseline', b'# one\n# two\n7\n', header_lines=2)
        self.replace_data(b'10\n12\n14\n')
        self.plan['tables'][0]['header_lines'] = 0
        self.derive(binary('subtract', column(), column('baseline', row=1)))
        table = self.execute()['derived_tables'][0]
        self.assertEqual(table['rows'], [[3.], [5.], [7.]])
        scalar = next(item for item in table['references'] if item['table'] == 'baseline')
        self.assertEqual(scalar['row'], 1)
        self.assertEqual(scalar['source_lines'], [3])
        self.assertEqual(scalar['selection'], 'scalar')
        self.assertEqual(scalar['input_sha256'], self.plan['tables'][1]['sha256'])

    def test_single_row_full_column_is_not_implicitly_broadcast(self):
        self.add_table('baseline', b'7\n')
        self.derive(binary('subtract', column(), column('baseline')))
        self.reject()

    def test_scalar_only_selection_has_one_row_and_selected_original_line(self):
        self.derive(binary('multiply', column(row=2), dict(literal=2, evidence='trusted2')))
        table = self.execute()['derived_tables'][0]
        self.assertEqual(table['rows'], [[4.]])
        self.assertEqual(table['references'][0]['source_lines'], [3])

    def test_nested_derivation_and_selected_derived_row_keep_dag(self):
        first = self.derive(binary('add', column(), dict(literal=1, evidence='trusted1')), name='first')
        self.plan['operations'] = [first,
            dict(id='second', method='derive', alignment='row_index', columns=[dict(name='difference', unit='1',
                 expression=binary('subtract', column('first', 'result'), column('first', 'result', row=1)))]),
            dict(id='description', method='summary', table='second', column='difference')]
        report = self.execute()
        self.assertEqual(report['derived_tables'][1]['rows'], [[0.], [1.], [2.]])
        scalar = next(item for item in report['derived_tables'][1]['references'] if item['selection'] == 'scalar')
        self.assertEqual(scalar['derivation_id'], 'first')
        self.assertEqual(scalar['derived_csv_sha256'], report['derived_tables'][0]['csv_sha256'])
        self.assertEqual(report['results'][0]['values']['mean'], 1.)

    def test_cross_table_row_alignment_is_full_and_explicit(self):
        self.add_table('other', b'4\n5\n6\n')
        operation = self.derive(binary('add', column(), column('other')))
        self.assertEqual(self.execute()['derived_tables'][0]['rows'], [[5.], [7.], [9.]])
        operation['alignment'] = 'truncate'
        self.reject()
        operation['alignment'] = 'row_index'
        self.replace_data(b'4\n5\n', table=1)
        self.reject()

    def test_line_and_scatter_preserve_all_points_and_original_order(self):
        self.plan['tables'][0]['columns'].append(dict(name='other', unit='K'))
        self.replace_data(b'# header\n3 9\n1 8\n2 7\n')
        self.plan['operations'] = [dict(id=method, method=method, table='samples', x='value', y='other')
                                   for method in ('line', 'scatter')]
        report = self.execute()
        for result in report['results']:
            self.assertEqual(result['points'], [[3., 9.], [1., 8.], [2., 7.]])
            self.assertEqual(result['sample_count'], 3)

    def test_histogram_counts_boundaries_and_outside_samples_without_clipping(self):
        self.replace_data(b'# header\n-1\n0\n.5\n1\n1.5\n2\n3\n')
        self.plan['operations'] = [dict(id='hist', method='histogram', table='samples', column='value', bins=2, range=[0, 2])]
        report = self.execute()
        result = report['results'][0]['values']
        self.assertEqual(result['counts'], [2, 3])
        self.assertEqual(result['edges'], [0., 1., 2.])
        self.assertEqual((result['below_range'], result['above_range'], result['in_range'], result['total']), (1, 1, 5, 7))
        self.assertEqual(result['implementation'], 'numpy.histogram')
        self.assertEqual(len(report['sources'][0]['rows']), 7)

    def test_statistics_matches_standard_library_and_singleton_std_is_none(self):
        self.replace_data(b'# header\n.1\n.2\n.3\n.4\n')
        values = [.1, .2, .3, .4]
        summary = self.execute()['results'][0]['values']
        self.assertEqual(summary['mean'], statistics.fmean(values))
        self.assertEqual(summary['sample_std'], statistics.stdev(values))
        self.assertEqual(summary['median'], statistics.median(values))
        self.replace_data(b'# header\n7\n')
        self.assertIsNone(self.execute()['results'][0]['values']['sample_std'])

    def test_unknown_fields_and_unsupported_plan_or_operation_types(self):
        variants = [None, [], dict(self.plan, extra=True), dict(self.plan, version=True),
                    dict(self.plan, version=2), dict(self.plan, operations=[])]
        for plan in variants:
            with self.subTest(plan=plan), self.assertRaises(tool.PostprocessError):
                tool.execute_plan(plan, self.files)
        for location in ('table', 'column', 'operation'):
            plan = copy.deepcopy(self.plan)
            target = plan['tables'][0] if location == 'table' else plan['tables'][0]['columns'][0] if location == 'column' else plan['operations'][0]
            target['unknown'] = True
            self.reject(plan)
        for method in ('exec', 'eval', 'interpolate', [], None):
            plan = copy.deepcopy(self.plan)
            plan['operations'][0]['method'] = method
            self.reject(plan)

    def test_safe_flat_file_ids_and_input_bytes_only(self):
        for name in ('../secret', '/secret', 'directory/file', 'a b', '.hidden', 'a' * 161):
            files = {name: next(iter(self.files.values()))}
            plan = copy.deepcopy(self.plan)
            plan['tables'][0]['file'] = name
            self.reject(plan, files)
        for value in ('1\n', bytearray(b'1\n'), memoryview(b'1\n'), None):
            self.reject(files={'samples.dat': value})
        self.reject(files=dict(self.files, undeclared=b'1\n'))

    def test_sha_pin_mismatch_is_rejected(self):
        self.files['samples.dat'] = b'# heading\n1\n2\n4\n'
        self.reject()

    def test_declared_format_and_header_are_never_guessed(self):
        for field, values in (('header_lines', [-1, True, '1', 1025]),
                              ('format', ['auto', 'tsv', None])):
            for value in values:
                plan = copy.deepcopy(self.plan)
                plan['tables'][0][field] = value
                self.reject(plan)
        self.plan['tables'][0]['header_lines'] = 0
        self.reject()

    def test_malformed_blank_and_nonfinite_rows_are_rejected(self):
        for data in (b'# h\n', b'# h\n\n1\n', b'# h\n1 2\n', b'# h\n# comment\n1\n',
                     b'# h\nnan\n', b'# h\ninf\n', b'# h\n1e999\n', b'# h\n1_000\n',
                     b'# h\nword\n', b'# h\n\xff\n', b'# h\n1\x00\n', b'# h\n1\v2\n'):
            self.replace_data(data)
            with self.subTest(data=data):
                self.reject()

    def test_csv_multiline_bad_quotes_and_missing_values_are_rejected(self):
        self.plan['tables'][0]['format'] = 'csv'
        for data in (b'# h\n"1\n2"\n', b'# h\n"1"bad\n', b'# h\n,\n', b'# h\n1,2\n'):
            self.replace_data(data)
            self.reject()

    def test_expression_text_unknown_fields_and_unverified_literal_shape_rejected(self):
        bad = ['__import__("os").system("false")', dict(literal=1),
               dict(literal=True, evidence='trusted'), dict(literal=math.nan, evidence='trusted'),
               dict(literal=1, evidence=''), dict(literal=1, evidence='../file'),
               dict(table='samples', column='value', extra=1),
               dict(table='missing', column='value'), dict(table='samples', column='missing'),
               dict(op='power', left=column(), right=column())]
        for expression in bad:
            self.derive(expression)
            self.reject()
        self.derive(dict(literal=1, evidence='trusted'))
        self.reject()

    def test_scalar_rows_are_one_based_bounded_and_exact_integers(self):
        for row in (0, -1, 4, True, 1.0, '1', tool.MAX_ROWS + 1):
            self.derive(column(row=row))
            self.reject()

    def test_zero_division_and_nonfinite_arithmetic_are_rejected(self):
        self.derive(binary('divide', column(), dict(literal=0, evidence='zero')))
        self.reject()
        self.derive(binary('multiply', column(), dict(literal=1e308, evidence='large')))
        self.reject()

    def test_forward_self_and_duplicate_table_operation_or_column_references_rejected(self):
        self.derive(column('derived', 'result'))
        self.reject()
        self.plan['operations'] = [dict(id='samples', method='summary', table='samples', column='value')]
        self.reject()
        self.plan['operations'][0]['id'] = 'summary1'
        self.plan['operations'].append(copy.deepcopy(self.plan['operations'][0]))
        self.reject()
        self.plan['operations'].pop()
        self.plan['tables'][0]['columns'].append(dict(name='value', unit='1'))
        self.reject()

    def test_unit_declarations_are_preserved_but_controls_and_empty_units_rejected(self):
        for unit in ('', ' ', 'eV\n', '../unit\x00', None):
            plan = copy.deepcopy(self.plan)
            plan['tables'][0]['columns'][0]['unit'] = unit
            self.reject(plan)
        self.plan['tables'][0]['columns'][0]['unit'] = 'declared_unit/atom'
        self.assertEqual(self.execute()['results'][0]['value_unit'], 'declared_unit/atom')

    def test_histogram_requires_explicit_finite_ordered_bounds_and_bins(self):
        operation = dict(id='hist', method='histogram', table='samples', column='value', bins=2, range=[0, 3])
        for field, values in (('bins', [0, True, 1.5, 'auto', tool.MAX_BINS + 1]),
                              ('range', [None, [], [1], [1, 1], [2, 1], [0, math.inf], [-1e308, 1e308]])):
            for value in values:
                altered = dict(operation)
                altered[field] = value
                self.plan['operations'] = [altered]
                self.reject()

    def test_row_cell_input_and_output_limits(self):
        with patch.object(tool, 'MAX_ROWS', 2):
            self.reject()
        with patch.object(tool, 'MAX_CELLS', 2):
            self.reject()
        with patch.object(tool, 'MAX_INPUT_BYTES', 3):
            self.reject()
        self.derive(column())
        with patch.object(tool, 'MAX_CELLS', 5):
            self.reject()
        baseline = self.execute()
        exact = len(canonical(baseline))
        with patch.object(tool, 'MAX_OUTPUT_BYTES', exact):
            self.assertEqual(self.execute(), baseline)
        with patch.object(tool, 'MAX_OUTPUT_BYTES', exact - 1):
            self.reject()

    def test_aggregate_rows_table_operation_and_arithmetic_work_limits(self):
        self.add_table('other', b'4\n5\n6\n')
        with patch.object(tool, 'MAX_ROWS', 5):
            self.reject()
        with patch.object(tool, 'MAX_TABLES', 1):
            self.reject()
        self.plan['tables'].pop()
        self.files.pop('other.dat')
        self.plan['operations'] *= tool.MAX_OPERATIONS + 1
        self.reject()
        self.derive(binary('add', column(), dict(literal=1, evidence='trusted')))
        with patch.object(tool, 'MAX_EVALUATED_CELLS', 8):
            self.reject()
        with patch.object(tool, 'MAX_AST_NODES', 2):
            self.reject()
        with patch.object(tool, 'MAX_AST_DEPTH', 0):
            self.reject()

    def test_public_parser_is_strict_and_supports_one_column_without_existing_two_header_contract(self):
        declaration = dict(id='one', file='raw', sha256=sha256(b'7\n'), format='whitespace',
                           header_lines=0, columns=[dict(name='value', unit='1')])
        parsed = tool.parse_table(b'7\n', declaration)
        self.assertEqual(parsed['rows'], [[7.]])
        self.assertEqual(parsed['line_numbers'], [1])
        with self.assertRaises(tool.PostprocessError):
            tool.parse_table(b'7\n', dict(declaration, unknown=True))

    def test_context_exports_same_limits_sha_schema_example_and_return_contract(self):
        context = tool.tool_context()
        self.assertEqual(context['version'], tool.VERSION)
        self.assertEqual(context['adapter_identity'], tool.adapter_identity())
        self.assertEqual(context['limits']['cells'], tool.MAX_CELLS)
        self.assertEqual(context['plan_schema']['additionalProperties'], False)
        self.assertEqual(len(context['plan_schema']['$defs']['expression']['oneOf']), 4)
        self.assertEqual(context['scalar_ast_example'], dict(table='samples', column='value', row=1))
        report = tool.execute_plan(context['minimal_example'],
                                  {name: data.encode('ascii') for name, data in context['synthetic_example_files_ascii'].items()})
        self.assertEqual(report['results'][0]['values']['mean'], 1.5)
        self.assertIn('derived_tables', context['return_fields'])
        canonical(context)


if __name__ == '__main__':
    unittest.main()
