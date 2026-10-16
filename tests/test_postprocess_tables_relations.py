"""Generic synthetic aggregation/key matching; no paper, API or simulation."""
import copy
import csv
import io
import math
import statistics
import unittest
from unittest.mock import patch

from auto_lammps import postprocess_tables as tool
from auto_lammps.manifest import canonical, sha256


class PostprocessRelationTests(unittest.TestCase):
    def setUp(self):
        self.files = {}
        self.plan = dict(version=1, tables=[], operations=[])
        self.add_table('observations', b'# explicit header\n2 1 10\n1 1 2\n2 2 14\n1 2 6\n',
                       [('site', '1'), ('configuration', '1'), ('value', 'eV')], header=1)
        self.aggregate()

    def add_table(self, name, data, columns, *, header=0):
        file = name + '.dat'
        self.files[file] = data
        table = dict(id=name, file=file, sha256=sha256(data), format='whitespace', header_lines=header,
                     columns=[dict(name=column, unit=unit) for column, unit in columns])
        self.plan['tables'].append(table)
        return table

    def aggregate(self, *, keys=None, columns=None, source='observations', name='grouped'):
        operation = dict(id=name, method='aggregate', table=source,
                         group_by=['site'] if keys is None else keys,
                         columns=columns or [dict(name='average', column='value', reducer='mean', unit='eV')])
        self.plan['operations'] = [operation]
        return operation

    def join(self):
        self.add_table('metadata', b'1 20\n2 40\n', [('identifier', '1'), ('reference', 'eV')])
        operation = dict(id='matched', method='join', left='grouped', right='metadata',
                         on=[dict(left='site', right='identifier')], columns=[
                             dict(name='site', side='left', column='site', unit='1'),
                             dict(name='observed', side='left', column='average', unit='eV'),
                             dict(name='reference', side='right', column='reference', unit='eV')])
        self.plan['operations'].append(operation)
        return operation

    def replace_data(self, table, data, *, header=0):
        declaration = next(item for item in self.plan['tables'] if item['id'] == table)
        self.files[declaration['file']] = data
        declaration['sha256'] = sha256(data)
        declaration['header_lines'] = header

    def execute(self):
        return tool.execute_plan(self.plan, self.files)

    def reject(self, code):
        with self.assertRaisesRegex(tool.PostprocessError, '^' + code + '$'):
            self.execute()

    def test_grouping_preserves_first_seen_keys_every_member_and_original_bytes(self):
        plan, files = copy.deepcopy(self.plan), dict(self.files)
        report = self.execute()
        table = report['derived_tables'][0]
        self.assertEqual(table['rows'], [[2., 12.], [1., 4.]])
        self.assertEqual(table['columns'], [dict(name='site', unit='1'), dict(name='average', unit='eV')])
        self.assertEqual(table['operation'], plan['operations'][0])
        self.assertEqual(table['row_sources'], [[dict(table='observations', rows=[1, 3])],
                                              [dict(table='observations', rows=[2, 4])]])
        self.assertEqual(table['source_row_numbering'], 'one_based_table_row')
        self.assertEqual(table['source_tables'], ['observations'])
        for reference in table['references']:
            self.assertEqual(reference['source_lines'], [2, 3, 4, 5])
            self.assertEqual(reference['input_sha256'], plan['tables'][0]['sha256'])
        self.assertEqual(''.join(report['sources'][0]['raw_lines']).encode('ascii'), files['observations.dat'])
        self.assertEqual(self.plan, plan)
        self.assertEqual(self.files, files)
        self.assertEqual(table['csv_sha256'], sha256(table['csv'].encode('ascii')))
        self.assertEqual(list(csv.reader(io.StringIO(table['csv']))),
                         [['site', 'average'], ['2', '12'], ['1', '4']])
        self.assertEqual(report['scientific_status'], 'not_evaluated')
        self.assertEqual(report['plan_sha256'], sha256(canonical(plan)))

    def test_all_reducers_have_explicit_sample_statistics_and_units(self):
        reducers = ['count', 'mean', 'sum', 'std', 'min', 'max', 'median']
        self.aggregate(columns=[dict(name='reduced_' + method, column='value', reducer=method,
                                     unit='1' if method == 'count' else 'eV') for method in reducers])
        table = self.execute()['derived_tables'][0]
        for row, values in zip(table['rows'], ([10., 14.], [2., 6.])):
            self.assertEqual(row[1:], [2, statistics.fmean(values), math.fsum(values),
                                      statistics.stdev(values), min(values), max(values), statistics.median(values)])
        self.assertEqual(table['columns'][1]['unit'], '1')
        self.assertTrue(all(column['unit'] == 'eV' for column in table['columns'][2:]))

    def test_no_group_keys_means_all_rows_once_and_sum_is_stable(self):
        self.aggregate(keys=[], columns=[dict(name='total', column='value', reducer='sum', unit='eV')])
        self.replace_data('observations', b'1 1 1e16\n2 1 1\n3 1 -1e16\n')
        table = self.execute()['derived_tables'][0]
        self.assertEqual(table['rows'], [[1.]])
        self.assertEqual(table['row_sources'], [[dict(table='observations', rows=[1, 2, 3])]])

    def test_composite_group_keys_are_not_merged_by_one_key(self):
        self.aggregate(keys=['site', 'configuration'])
        table = self.execute()['derived_tables'][0]
        self.assertEqual(table['rows'], [[2., 1., 10.], [1., 1., 2.], [2., 2., 14.], [1., 2., 6.]])

    def test_aggregate_join_then_derive_summary_and_plot_are_reusable(self):
        self.join()
        self.plan['operations'] += [dict(id='differences', method='derive', alignment='row_index', columns=[
            dict(name='difference', unit='eV', expression=dict(op='subtract',
                 left=dict(table='matched', column='observed'), right=dict(table='matched', column='reference')))]),
            dict(id='description', method='summary', table='differences', column='difference'),
            dict(id='chart', method='scatter', table='matched', x='site', y='observed')]
        report = self.execute()
        joined, derived = report['derived_tables'][1:]
        self.assertEqual(joined['rows'], [[2., 12., 40.], [1., 4., 20.]])
        self.assertEqual(joined['row_sources'], [
            [dict(table='grouped', rows=[1]), dict(table='metadata', rows=[2])],
            [dict(table='grouped', rows=[2]), dict(table='metadata', rows=[1])]])
        group_refs = [ref for ref in joined['references'] if ref['table'] == 'grouped']
        self.assertEqual({ref['column'] for ref in group_refs}, {'site', 'average'})
        self.assertTrue(all(ref['derived_csv_sha256'] == report['derived_tables'][0]['csv_sha256']
                            for ref in group_refs))
        self.assertEqual(derived['rows'], [[-28.], [-16.]])
        self.assertTrue(all(ref['derivation_id'] == 'matched' for ref in derived['references']))
        self.assertEqual(report['results'][0]['values']['mean'], -22.)
        self.assertEqual(report['results'][1]['points'], [[2., 12.], [1., 4.]])
        units = tool.column_units(self.plan, self.files)
        expected = {(table['id'], column['name']): column['unit']
                    for table in report['sources'] + report['derived_tables'] for column in table['columns']}
        self.assertEqual(units, expected)
        self.assertEqual(tool.TABLE_METHODS, {'derive', 'aggregate', 'join'})

    def test_selected_relational_row_and_reaggregation_keep_full_dag(self):
        self.plan['operations'] += [dict(id='selected', method='derive', alignment='row_index', columns=[
            dict(name='value', unit='eV', expression=dict(table='grouped', column='average', row=2))]),
            dict(id='all_groups', method='aggregate', table='grouped', group_by=[], columns=[
                dict(name='average', column='average', reducer='mean', unit='eV')])]
        report = self.execute()
        self.assertEqual(report['derived_tables'][1]['rows'], [[4.]])
        self.assertEqual(report['derived_tables'][1]['references'][0]['row'], 2)
        self.assertEqual(report['derived_tables'][2]['rows'], [[8.]])
        self.assertEqual(report['derived_tables'][2]['row_sources'], [[dict(table='grouped', rows=[1, 2])]])

    def test_sample_std_rejects_singleton_group_instead_of_fabricating_zero(self):
        self.aggregate(keys=['site', 'configuration'], columns=[dict(name='spread', column='value',
                                                                   reducer='std', unit='eV')])
        self.reject('postprocess_insufficient_group_samples')

    def test_nonfinite_cells_and_overflow_are_rejected(self):
        self.replace_data('observations', b'1 1 nan\n')
        self.reject('postprocess_nonnumeric_cell')
        self.aggregate(keys=[], columns=[dict(name='total', column='value', reducer='sum', unit='eV')])
        self.replace_data('observations', b'1 1 1.7e308\n1 1 1.7e308\n')
        self.reject('postprocess_undefined_statistics')

    def test_empty_input_or_empty_internal_group_is_rejected(self):
        self.replace_data('observations', b'# header\n', header=1)
        self.reject('postprocess_empty_or_oversized_table_line')
        with self.assertRaisesRegex(tool.PostprocessError, '^postprocess_empty_group_or_table$'):
            tool._reduce([], 'count')

    def test_aggregate_unit_change_count_unit_and_duplicate_keys_rejected(self):
        operation = self.plan['operations'][0]
        operation['columns'][0]['unit'] = 'J'
        self.reject('postprocess_unit_mismatch')
        operation['columns'][0]['reducer'] = 'count'
        operation['columns'][0]['unit'] = 'eV'
        self.reject('postprocess_unit_mismatch')
        operation['columns'][0]['unit'] = '1'
        operation['group_by'] = ['site', 'site']
        self.reject('postprocess_duplicate_group_key')
        operation['group_by'] = ['site']
        operation['columns'][0]['name'] = 'site'
        self.reject('postprocess_invalid_or_duplicate_column')

    def test_unknown_units_are_retained_without_inference(self):
        self.plan['tables'][0]['columns'][2]['unit'] = 'unknown'
        self.plan['operations'][0]['columns'][0]['unit'] = 'unknown'
        table = self.execute()['derived_tables'][0]
        self.assertEqual(table['columns'][1]['unit'], 'unknown')
        self.assertEqual(table['units_origin'], 'declared_not_inferred')

    def test_duplicate_keys_on_either_join_side_are_rejected(self):
        self.join()
        self.replace_data('metadata', b'1 20\n1 40\n')
        self.reject('postprocess_join_key_not_unique')
        self.replace_data('metadata', b'1 20\n2 40\n')
        self.plan['operations'][1]['left'] = 'observations'
        self.plan['operations'][1]['columns'][1]['column'] = 'value'
        self.reject('postprocess_join_key_not_unique')

    def test_missing_pair_on_either_side_never_drops_rows(self):
        self.join()
        for data in (b'1 20\n', b'1 20\n2 40\n3 60\n', b'1 20\n3 60\n'):
            with self.subTest(data=data):
                self.replace_data('metadata', data)
                self.reject('postprocess_join_missing_pair')

    def test_composite_join_uses_all_explicit_keys(self):
        self.add_table('other', b'1 2 60\n2 1 100\n1 1 20\n2 2 140\n',
                       [('site_key', '1'), ('configuration_key', '1'), ('reference', 'eV')])
        self.plan['operations'] = [dict(id='matched', method='join', left='observations', right='other',
            on=[dict(left='site', right='site_key'), dict(left='configuration', right='configuration_key')],
            columns=[dict(name='reference', side='right', column='reference', unit='eV')])]
        self.assertEqual(self.execute()['derived_tables'][0]['rows'], [[100.], [20.], [140.], [60.]])

    def test_matching_is_exact_without_numeric_tolerance_or_row_fallback(self):
        self.join()
        self.replace_data('metadata', b'1 20\n2.000000000000001 40\n')
        self.reject('postprocess_join_missing_pair')

    def test_raw_key_precision_loss_cannot_make_distinct_identifiers_match(self):
        self.join()
        self.replace_data('observations', b'9007199254740993 1 10\n9007199254740993 2 14\n')
        self.replace_data('metadata', b'9007199254740992 40\n')
        self.reject('postprocess_key_precision_loss')
        self.replace_data('observations', b'2 1 10\n2 2 14\n')
        self.replace_data('metadata', b'2.00000000000000001 40\n')
        self.reject('postprocess_key_precision_loss')

    def test_csv_keys_accept_equivalent_decimal_notation_and_preserve_order(self):
        declaration = self.plan['tables'][0]
        declaration['format'] = 'csv'
        self.replace_data('observations', b'"0.1",1,10\n.100,2,14\n1e3,1,2\n1000.0,2,6\n')
        self.join()
        self.replace_data('metadata', b'1000 20\n1e-1 40\n')
        joined = self.execute()['derived_tables'][1]
        self.assertEqual(joined['rows'], [[.1, 12., 40.], [1000., 4., 20.]])

    def test_join_key_and_projected_units_cannot_be_changed(self):
        operation = self.join()
        self.plan['tables'][1]['columns'][0]['unit'] = 'K'
        self.reject('postprocess_unit_mismatch')
        self.plan['tables'][1]['columns'][0]['unit'] = '1'
        operation['columns'][1]['unit'] = 'eV/atom'
        self.reject('postprocess_unit_mismatch')

    def test_invalid_join_keys_columns_and_unknown_fields_rejected(self):
        operation = self.join()
        original = copy.deepcopy(operation)
        cases = [('on', [], 'postprocess_explicit_join_keys_required'),
                 ('on', [dict(left='site', right='identifier')] * 2, 'postprocess_duplicate_join_key'),
                 ('on', [dict(left='missing', right='identifier')], 'postprocess_undeclared_column'),
                 ('columns', [dict(name='bad', side='both', column='reference', unit='eV')],
                  'postprocess_invalid_join_side'),
                 ('columns', [dict(name='bad', side='right', column='missing', unit='eV')],
                  'postprocess_undeclared_column')]
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                operation.clear()
                operation.update(copy.deepcopy(original))
                operation[field] = value
                self.reject(code)
        operation.clear()
        operation.update(original)
        operation['fallback'] = 'row_index'
        self.reject('postprocess_unknown_or_missing_fields')

    def test_forward_reference_duplicate_id_and_unsupported_reducer_rejected(self):
        self.plan['operations'][0]['table'] = 'not_yet_defined'
        self.reject('postprocess_undeclared_table')
        self.plan['operations'][0]['table'] = 'observations'
        self.plan['operations'][0]['columns'][0]['reducer'] = 'fit'
        self.reject('postprocess_unsupported_reducer')
        self.plan['operations'][0]['columns'][0]['reducer'] = 'mean'
        self.plan['operations'][0]['id'] = 'observations'
        self.reject('postprocess_duplicate_operation')

    def test_raw_hash_and_all_capacity_limits_remain_enforced(self):
        self.files['observations.dat'] += b'2 3 16\n'
        self.reject('postprocess_raw_sha256_mismatch')
        self.files['observations.dat'] = self.files['observations.dat'][:-7]
        self.join()
        for limit, value, code in [('MAX_TABLES', 3, 'postprocess_table_limit'),
                                   ('MAX_COLUMNS', 2, 'postprocess_column_limit'),
                                   ('MAX_EVALUATED_CELLS', 1, 'postprocess_arithmetic_work_limit'),
                                   ('MAX_CELLS', 20, 'postprocess_derived_cell_limit'),
                                   ('MAX_OUTPUT_BYTES', 1, 'postprocess_output_limit')]:
            with self.subTest(limit=limit), patch.object(tool, limit, value):
                self.reject(code)

    def test_relational_column_limit_includes_preserved_group_keys(self):
        self.aggregate(columns=[dict(name='n' + str(i), column='value', reducer='count', unit='1')
                                for i in range(tool.MAX_COLUMNS)])
        self.reject('postprocess_column_limit')

    def test_column_discovery_rejects_invalid_plan_and_returns_independent_values(self):
        units = tool.column_units(self.plan, self.files)
        units[('grouped', 'average')] = 'fabricated'
        self.assertEqual(tool.column_units(self.plan, self.files)[('grouped', 'average')], 'eV')
        self.plan['operations'][0]['columns'][0]['unit'] = 'K'
        with self.assertRaisesRegex(tool.PostprocessError, '^postprocess_unit_mismatch$'):
            tool.column_units(self.plan, self.files)

    def test_tool_schema_exposes_exact_new_operations_and_preserves_version_one(self):
        context = tool.tool_context()
        variants = context['plan_schema']['properties']['operations']['items']['oneOf']
        by_method = {item['properties']['method'].get('const'): item for item in variants
                     if 'const' in item['properties']['method']}
        self.assertEqual(set(by_method), {'derive', 'aggregate', 'join', 'summary', 'histogram'})
        aggregate, join = by_method['aggregate'], by_method['join']
        self.assertEqual(aggregate['properties']['group_by']['minItems'], 0)
        self.assertEqual(aggregate['properties']['columns']['items']['properties']['reducer']['enum'],
                         ['count', 'mean', 'sum', 'std', 'min', 'max', 'median'])
        self.assertEqual(set(aggregate['required']), {'id', 'method', 'table', 'group_by', 'columns'})
        self.assertEqual(set(join['required']), {'id', 'method', 'left', 'right', 'on', 'columns'})
        self.assertFalse(join['additionalProperties'])
        self.assertEqual(context['version'], 1)
        self.assertEqual(context['limits']['columns'], 16)


if __name__ == '__main__':
    unittest.main()
