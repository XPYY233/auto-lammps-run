"""Read-only A numeric sources for the shared chart provider contract.

Only selected columns of controller-bound ReferenceEvidenceViews are exposed.
The adapter performs no model request, physics, scientific analysis or export
write, and never parses an unfiltered downloadable mixed-source CSV.
"""
import csv
import io
import json
import math
import re

from . import runtime_launcher as runtime
from .manifest import canonical, sha256
from .reference_evidence import _NON_A_COLUMN
from .results import ResultUnavailable


_NAMESPACE = 'reference-a:'
_IDENTITY = ('task_id', 'paper_id', 'evaluation_id', 'request_id', 'job_id',
             'manifest_sha256', 'source_sha256')
MAX_PREVIEW_PAIRS = 128
MAX_CATEGORY_VALUES = 128


class ReferenceChartSources:
    """A-only source_provider; partial A remains explicitly partial."""

    def __init__(self, views):
        self.views = views

    def owns(self, analysis_id):
        return isinstance(analysis_id, str) and bool(re.fullmatch(r'reference-a:[a-f0-9]{64}', analysis_id))

    def _load(self, identifier):
        try:
            report, _, tables = self.views._load(identifier)
            if report is None:
                return None, []
            folder = self.views.directory / identifier / 'reference-evidence'
            raw = runtime.read_regular(folder / 'manifest.json', 262144, private=True)
            if sha256(raw) != report['manifest_sha256']:
                raise ResultUnavailable('A chart manifest changed during source selection')
            document = json.loads(raw)
            selections = {}
            for view in document['views']:
                for declaration in view.get('tables', []):
                    name = declaration['name']
                    if declaration['role'] != 'reference':
                        raise ResultUnavailable('A chart source must have the reference role')
                    columns = [column for column in declaration['columns']
                               if column.get('kind') in {'number', 'text'}
                               and column.get('source_role') == 'reference'
                               and not _NON_A_COLUMN.search(column['key'])]
                    if name in selections and canonical(selections[name]) != canonical(columns):
                        raise ResultUnavailable('Conflicting A numeric column declarations')
                    selections[name] = columns
            selected = []
            for table in tables:
                declared = selections.get(table['name'], [])
                numeric = [column for column in declared if column['kind'] == 'number']
                categorical = [column for column in declared if column['kind'] == 'text']
                if len(numeric) < 2:
                    continue
                indexes = {column['key']: index for index, column in enumerate(table['columns'])}
                if any(column['key'] not in indexes for column in declared) or table['role'] != 'reference':
                    raise ResultUnavailable('A numeric selection differs from its verified table')
                categories = []
                for column in categorical:
                    values = sorted({row[indexes[column['key']]] for row in table['rows']
                                     if row[indexes[column['key']]] != ''})
                    if len(values) > MAX_CATEGORY_VALUES:
                        raise ResultUnavailable('A category menu exceeds its bound; no values were omitted')
                    if values:
                        categories.append(dict(name=column['key'], values=values))
                selected.append(dict(name=table['name'], sha256=table['sha256'],
                    columns=[dict(name=column['key'], unit=column['unit']) for column in numeric],
                    rows=[[row[indexes[column['key']]] for column in numeric] for row in table['rows']],
                    filter_categories=categories,
                    category_rows=[{category['name']: row[indexes[category['name']]] for category in categories}
                                   for row in table['rows']],
                    total_rows=table['total_rows']))
            # Revalidate ledger and every artifact after the separate manifest
            # read. Neither a replacement nor a stale source can supply a result.
            again, _, _ = self.views._load(identifier)
            if again is None or canonical({key: again[key] for key in _IDENTITY}) != canonical(
                    {key: report[key] for key in _IDENTITY}):
                raise ResultUnavailable('A chart identity changed during source selection')
            return report, selected
        except (ValueError, KeyError, TypeError, OSError, runtime.ExecutionDenied):
            raise ResultUnavailable('Author A chart sources did not pass their task and source checks') from None

    @staticmethod
    def _metadata(report):
        return dict(role='reference', output_status=report['output_status'], output_valid=report['output_valid'],
                    scheduler_state=report['scheduler_state'], coverage=report['coverage'],
                    scientific_status='not_evaluated', limitations=report['limitations'],
                    source_identity={key: report[key] for key in _IDENTITY})

    def options(self, identifier):
        report, tables = self._load(identifier)
        if report is None:
            return []
        analysis_id = _NAMESPACE + report['manifest_sha256']
        return [dict(analysis_id=analysis_id, file=table['name'], source_sha256=table['sha256'],
                     columns=table['columns'], rows=table['total_rows'], examples=table['rows'][:2],
                     filter_categories=table['filter_categories'],
                     **self._metadata(report)) for table in tables]

    def chart_data(self, identifier, analysis_id, file, x, y, *, filters=None):
        if not self.owns(analysis_id) or not all(isinstance(item, str) for item in (file, x, y)):
            raise ResultUnavailable('Select a declared author A numeric chart source')
        report, tables = self._load(identifier)
        if report is None or analysis_id != _NAMESPACE + report['manifest_sha256']:
            raise ResultUnavailable('Author A chart source is unavailable or has changed')
        table = next((table for table in tables if table['name'] == file), None)
        if table is None:
            raise ResultUnavailable('No declared author A numeric table')
        columns = {column['name']: (index, column['unit']) for index, column in enumerate(table['columns'])}
        if x == y or x not in columns or y not in columns:
            raise ResultUnavailable('Choose two distinct declared author A numeric columns')
        selected_filters = []
        if filters is not None:
            if not isinstance(filters, list) or not 1 <= len(filters) <= 4:
                raise ResultUnavailable('Select bounded declared A category filters')
            allowed = {category['name']: category['values'] for category in table['filter_categories']}
            used = set()
            for item in filters:
                if (not isinstance(item, dict) or set(item) != {'column', 'value'}
                        or not isinstance(item['column'], str) or not isinstance(item['value'], str)
                        or item['column'] in used or item['column'] not in allowed
                        or item['value'] not in allowed[item['column']]):
                    raise ResultUnavailable('A category filter differs from its verified menu')
                used.add(item['column'])
                selected_filters.append(dict(column=item['column'], value=item['value']))
        x_index, x_unit = columns[x]
        y_index, y_unit = columns[y]
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow([x, y])
        pairs, positions, missing, matched = [], [], 0, 0
        for table_row, row in enumerate(table['rows'], 1):
            category_row = table['category_rows'][table_row - 1]
            if any(category_row[item['column']] != item['value'] for item in selected_filters):
                continue
            matched += 1
            values = [row[x_index], row[y_index]]
            writer.writerow(values)
            if any(value == '' for value in values):
                missing += 1
                continue
            try:
                pair = [float(value) for value in values]
            except (ValueError, OverflowError):
                raise ResultUnavailable('Author A chart pair is not finite numeric data') from None
            if not all(math.isfinite(value) for value in pair):
                raise ResultUnavailable('Author A chart pair is not finite numeric data')
            pairs.append(pair)
            positions.append(table_row)
        if not pairs:
            raise ResultUnavailable('The selected author A columns have no finite paired data')
        indices = (sorted({round(index * (len(pairs) - 1) / (MAX_PREVIEW_PAIRS - 1))
                           for index in range(MAX_PREVIEW_PAIRS)}) if len(pairs) > MAX_PREVIEW_PAIRS
                   else range(len(pairs)))
        data = stream.getvalue().encode('utf-8')
        selection = (dict(filters=selected_filters, source_total_rows=table['total_rows'],
                          filtered_out_rows=table['total_rows'] - matched,
                          filter_policy='exact_declared_category_equality_AND') if selected_filters else {})
        return dict(analysis_id=analysis_id, file=file, source_sha256=table['sha256'], x=x, y=y,
                    x_unit=x_unit, y_unit=y_unit, rows=matched, sampled=len(pairs) > MAX_PREVIEW_PAIRS,
                    preview=[pairs[index] for index in indices], preview_table_rows=[positions[index] for index in indices],
                    missing_pair_count=missing, preview_policy='up_to_128_finite_pairs_including_endpoints',
                    table_row_policy='one_based_data_row_in_verified_selected_table',
                    csv_sha256=sha256(data), csv_size=len(data), csv=data, **selection, **self._metadata(report))
