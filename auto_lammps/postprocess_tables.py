"""Bounded, declarative numeric postprocessing of already verified raw bytes.

No path is read, expression text evaluated, physical model run, or scientific
success inferred. The calling service authenticates raw provenance and scalar
evidence; this tool preserves both while checking formats and arithmetic.
"""
import csv
from decimal import Decimal
import io
import math
import operator
from pathlib import Path
import re
import statistics
import sys

from . import analysis
from .manifest import canonical, sha256


VERSION = 1
MAX_TABLES, MAX_OPERATIONS, MAX_COLUMNS = 16, 32, 16
MAX_ROWS, MAX_CELLS = 100000, 500000
MAX_INPUT_BYTES, MAX_OUTPUT_BYTES = 16 * 1024 * 1024, 32 * 1024 * 1024
MAX_PLAN_BYTES, MAX_LINE_BYTES = 128 * 1024, 4096
MAX_AST_NODES, MAX_AST_DEPTH, MAX_EVALUATED_CELLS = 1024, 12, 8_000_000
MAX_BINS = 1024
_IDENTIFIER_PATTERN = r'[A-Za-z][A-Za-z0-9_]{0,63}'
_FILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
_HASH = re.compile(r'[a-f0-9]{64}')
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
_UNIT = re.compile(r'[A-Za-z0-9_/.^*()+% -]{1,64}')
_ARITHMETIC = {'add': operator.add, 'subtract': operator.sub,
               'multiply': operator.mul, 'divide': operator.truediv}
TABLE_METHODS = frozenset({'derive', 'aggregate', 'join'})
_REDUCERS = ('count', 'mean', 'sum', 'std', 'min', 'max', 'median')


class PostprocessError(ValueError):
    """A fixed rejection code, without private source text or paths."""


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise PostprocessError('postprocess_unknown_or_missing_fields')


def _name(value):
    if type(value) is not str or not re.fullmatch(_IDENTIFIER_PATTERN, value):
        raise PostprocessError('postprocess_invalid_identifier')
    try:
        return analysis._name(value)
    except analysis.AnalysisError:
        raise PostprocessError('postprocess_invalid_identifier') from None


def _finite(value):
    try:
        return float(analysis._finite(value))
    except analysis.AnalysisError:
        raise PostprocessError('postprocess_nonfinite_number') from None


def _columns(columns, *, derived=False):
    if type(columns) is not list or not 1 <= len(columns) <= MAX_COLUMNS:
        raise PostprocessError('postprocess_column_limit')
    names = set()
    for column in columns:
        _fields(column, {'name', 'unit', 'expression'} if derived else {'name', 'unit'})
        name = _name(column['name'])
        unit = column['unit']
        if name in names or type(unit) is not str or not _UNIT.fullmatch(unit) or not unit.strip():
            raise PostprocessError('postprocess_invalid_or_duplicate_column')
        names.add(name)
    return names


def _expression(expression, tables, budget, depth=0):
    if type(expression) is not dict or depth > MAX_AST_DEPTH:
        raise PostprocessError('postprocess_invalid_expression')
    budget[0] += 1
    if budget[0] > MAX_AST_NODES:
        raise PostprocessError('postprocess_expression_limit')
    if set(expression) in ({'table', 'column'}, {'table', 'column', 'row'}):
        table = expression['table']
        column = expression['column']
        if type(table) is not str or table not in tables or type(column) is not str or column not in tables[table]:
            raise PostprocessError('postprocess_undeclared_column')
        if 'row' in expression and (type(expression['row']) is not int or not 1 <= expression['row'] <= MAX_ROWS):
            raise PostprocessError('postprocess_invalid_scalar_row')
        return {table}
    if set(expression) == {'literal', 'evidence'}:
        _finite(expression['literal'])
        evidence = expression['evidence']
        if (type(evidence) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', evidence)):
            raise PostprocessError('postprocess_literal_evidence_required')
        return set()
    _fields(expression, {'op', 'left', 'right'})
    if type(expression['op']) is not str or expression['op'] not in _ARITHMETIC:
        raise PostprocessError('postprocess_unsupported_expression')
    return (_expression(expression['left'], tables, budget, depth + 1)
            | _expression(expression['right'], tables, budget, depth + 1))


def _declared_column(tables, table, column):
    if type(table) is not str or table not in tables:
        raise PostprocessError('postprocess_undeclared_table')
    if type(column) is not str or column not in tables[table]:
        raise PostprocessError('postprocess_undeclared_column')
    return tables[table][column]


def _aggregate_columns(operation, tables):
    table = operation['table']
    if type(table) is not str or table not in tables:
        raise PostprocessError('postprocess_undeclared_table')
    keys = operation['group_by']
    if type(keys) is not list or len(keys) > MAX_COLUMNS:
        raise PostprocessError('postprocess_invalid_group_keys')
    output = {}
    for key in keys:
        unit = _declared_column(tables, table, key)
        if key in output:
            raise PostprocessError('postprocess_duplicate_group_key')
        output[key] = unit
    columns = operation['columns']
    if type(columns) is not list or not 1 <= len(columns) <= MAX_COLUMNS:
        raise PostprocessError('postprocess_column_limit')
    for column in columns:
        _fields(column, {'name', 'column', 'reducer', 'unit'})
        _columns([dict(name=column['name'], unit=column['unit'])])
        unit = _declared_column(tables, table, column['column'])
        if type(column['reducer']) is not str or column['reducer'] not in _REDUCERS:
            raise PostprocessError('postprocess_unsupported_reducer')
        if column['unit'] != ('1' if column['reducer'] == 'count' else unit):
            raise PostprocessError('postprocess_unit_mismatch')
        if column['name'] in output:
            raise PostprocessError('postprocess_invalid_or_duplicate_column')
        output[column['name']] = column['unit']
    if len(output) > MAX_COLUMNS:
        raise PostprocessError('postprocess_column_limit')
    return output


def _join_columns(operation, tables):
    left, right = operation['left'], operation['right']
    for table in (left, right):
        if type(table) is not str or table not in tables:
            raise PostprocessError('postprocess_undeclared_table')
    keys = operation['on']
    if type(keys) is not list or not 1 <= len(keys) <= MAX_COLUMNS:
        raise PostprocessError('postprocess_explicit_join_keys_required')
    seen_left, seen_right = set(), set()
    for key in keys:
        _fields(key, {'left', 'right'})
        left_unit = _declared_column(tables, left, key['left'])
        right_unit = _declared_column(tables, right, key['right'])
        if key['left'] in seen_left or key['right'] in seen_right:
            raise PostprocessError('postprocess_duplicate_join_key')
        seen_left.add(key['left'])
        seen_right.add(key['right'])
        if left_unit != right_unit:
            raise PostprocessError('postprocess_unit_mismatch')
    columns = operation['columns']
    if type(columns) is not list or not 1 <= len(columns) <= MAX_COLUMNS:
        raise PostprocessError('postprocess_column_limit')
    output = {}
    for column in columns:
        _fields(column, {'name', 'side', 'column', 'unit'})
        _columns([dict(name=column['name'], unit=column['unit'])])
        if type(column['side']) is not str or column['side'] not in ('left', 'right'):
            raise PostprocessError('postprocess_invalid_join_side')
        unit = _declared_column(tables, operation[column['side']], column['column'])
        if unit != column['unit']:
            raise PostprocessError('postprocess_unit_mismatch')
        if column['name'] in output:
            raise PostprocessError('postprocess_invalid_or_duplicate_column')
        output[column['name']] = unit
    return output


def _validate_plan(plan, files):
    _fields(plan, {'version', 'tables', 'operations'})
    if type(plan['version']) is not int or plan['version'] != VERSION:
        raise PostprocessError('postprocess_unsupported_version')
    if type(files) is not dict or not 1 <= len(files) <= MAX_TABLES:
        raise PostprocessError('postprocess_input_limit')
    total = 0
    for name, data in files.items():
        if type(name) is not str or not _FILE.fullmatch(name) or type(data) is not bytes:
            raise PostprocessError('postprocess_invalid_raw_input')
        total += len(data)
    if total > MAX_INPUT_BYTES:
        raise PostprocessError('postprocess_input_limit')
    if type(plan['tables']) is not list or not 1 <= len(plan['tables']) <= MAX_TABLES:
        raise PostprocessError('postprocess_table_limit')
    tables, used_files = {}, set()
    for table in plan['tables']:
        _fields(table, {'id', 'file', 'sha256', 'format', 'header_lines', 'columns'})
        name, file = _name(table['id']), table['file']
        if name in tables or type(file) is not str or file not in files or file in used_files:
            raise PostprocessError('postprocess_invalid_or_duplicate_table')
        if (type(table['sha256']) is not str or not _HASH.fullmatch(table['sha256'])
                or type(table['format']) is not str or table['format'] not in {'whitespace', 'csv'}
                or type(table['header_lines']) is not int or not 0 <= table['header_lines'] <= 1024):
            raise PostprocessError('postprocess_invalid_parser_declaration')
        _columns(table['columns'])
        tables[name] = {column['name']: column['unit'] for column in table['columns']}
        used_files.add(file)
    if used_files != set(files):
        raise PostprocessError('postprocess_undeclared_input')
    operations = plan['operations']
    if type(operations) is not list or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise PostprocessError('postprocess_operation_limit')
    ids, budget = set(tables), [0]
    for operation in operations:
        if type(operation) is not dict:
            raise PostprocessError('postprocess_unknown_or_missing_fields')
        method = operation.get('method')
        if method == 'derive':
            _fields(operation, {'id', 'method', 'alignment', 'columns'})
        elif method == 'aggregate':
            _fields(operation, {'id', 'method', 'table', 'group_by', 'columns'})
        elif method == 'join':
            _fields(operation, {'id', 'method', 'left', 'right', 'on', 'columns'})
        elif method == 'summary':
            _fields(operation, {'id', 'method', 'table', 'column'})
        elif method == 'histogram':
            _fields(operation, {'id', 'method', 'table', 'column', 'bins', 'range'})
        elif method in ('line', 'scatter'):
            _fields(operation, {'id', 'method', 'table', 'x', 'y'})
        else:
            raise PostprocessError('postprocess_unsupported_method')
        name = _name(operation['id'])
        if name in ids:
            raise PostprocessError('postprocess_duplicate_operation')
        ids.add(name)
        if method == 'derive':
            _columns(operation['columns'], derived=True)
            if operation['alignment'] != 'row_index':
                raise PostprocessError('postprocess_explicit_alignment_required')
            sources = set()
            for column in operation['columns']:
                sources |= _expression(column['expression'], tables, budget)
            if not sources:
                raise PostprocessError('postprocess_numeric_source_required')
            columns = {column['name']: column['unit'] for column in operation['columns']}
        elif method == 'aggregate':
            columns = _aggregate_columns(operation, tables)
        elif method == 'join':
            columns = _join_columns(operation, tables)
        else:
            table = operation['table']
            if type(table) is not str or table not in tables:
                raise PostprocessError('postprocess_undeclared_table')
            for key in (('x', 'y') if method in ('line', 'scatter') else ('column',)):
                if type(operation[key]) is not str or operation[key] not in tables[table]:
                    raise PostprocessError('postprocess_undeclared_column')
            if method == 'histogram':
                bounds = operation['range']
                if (type(operation['bins']) is not int or not 1 <= operation['bins'] <= MAX_BINS
                        or type(bounds) is not list or len(bounds) != 2):
                    raise PostprocessError('postprocess_explicit_histogram_bounds_required')
                low, high = _finite(bounds[0]), _finite(bounds[1])
                if low >= high or not math.isfinite(high - low):
                    raise PostprocessError('postprocess_invalid_histogram_range')
        if method in TABLE_METHODS:
            if len(tables) >= MAX_TABLES:
                raise PostprocessError('postprocess_table_limit')
            tables[name] = columns
    if len(canonical(plan)) > MAX_PLAN_BYTES:
        raise PostprocessError('postprocess_plan_limit')
    return tables


def validate_plan(plan, files):
    """Check exact JSON fields; files are flat logical names mapped to bytes."""
    _validate_plan(plan, files)
    return plan


def column_units(plan, files):
    """Return {(table_id, column_name): declared_unit} for every numeric table.

    This uses the same ordered schema validation as execution, including new
    table operations. It does not parse data or attest physical unit correctness;
    execute_plan separately checks the supplied bytes against their declared SHA.
    """
    return {(table, column): unit for table, columns in _validate_plan(plan, files).items()
            for column, unit in columns.items()}


def parse_table(data, table):
    """Parse declared physical lines, keeping every original line and value."""
    _fields(table, {'id', 'file', 'sha256', 'format', 'header_lines', 'columns'})
    _name(table['id'])
    _columns(table['columns'])
    if (type(table['file']) is not str or not _FILE.fullmatch(table['file'])
            or type(table['sha256']) is not str or not _HASH.fullmatch(table['sha256'])
            or type(table['format']) is not str or table['format'] not in {'whitespace', 'csv'}
            or type(table['header_lines']) is not int or not 0 <= table['header_lines'] <= 1024):
        raise PostprocessError('postprocess_invalid_parser_declaration')
    if type(data) is not bytes or len(data) > MAX_INPUT_BYTES:
        raise PostprocessError('postprocess_input_limit')
    if sha256(data) != table['sha256']:
        raise PostprocessError('postprocess_raw_sha256_mismatch')
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError:
        raise PostprocessError('postprocess_ascii_table_required') from None
    # Only CR/LF are line separators. Other control characters are rejected.
    if any(ord(c) < 32 and c not in '\t\r\n' or ord(c) == 127 for c in text):
        raise PostprocessError('postprocess_invalid_table_character')
    lines = list(io.StringIO(text, newline=''))
    header = table['header_lines']
    if len(lines) <= header or any(len(line) > MAX_LINE_BYTES for line in lines):
        raise PostprocessError('postprocess_empty_or_oversized_table_line')
    rows, line_numbers = [], []
    for number, line in enumerate(lines[header:], header + 1):
        if len(rows) >= MAX_ROWS or (len(rows) + 1) * len(table['columns']) > MAX_CELLS:
            raise PostprocessError('postprocess_row_or_cell_limit')
        try:
            tokens = line.split() if table['format'] == 'whitespace' else next(csv.reader([line], strict=True))
        except (csv.Error, StopIteration):
            raise PostprocessError('postprocess_malformed_numeric_row') from None
        if len(tokens) != len(table['columns']):
            raise PostprocessError('postprocess_malformed_numeric_row')
        values = []
        for token in tokens:
            token = token.strip()
            if not _NUMBER.fullmatch(token):
                raise PostprocessError('postprocess_nonnumeric_cell')
            try:
                values.append(_finite(float(token)))
            except (ValueError, OverflowError):
                raise PostprocessError('postprocess_nonfinite_number') from None
        rows.append(values)
        line_numbers.append(number)
    return dict(id=table['id'], file=table['file'], sha256=sha256(data), size=len(data),
                format=table['format'], header_lines=header, columns=table['columns'],
                rows=rows, line_numbers=line_numbers, raw_lines=lines,
                units_origin='declared_not_inferred')


def _column(table, name):
    index = next(i for i, column in enumerate(table['columns']) if column['name'] == name)
    return [row[index] for row in table['rows']]


def _references(expression):
    if 'table' in expression:
        return {(expression['table'], expression['column'], expression.get('row'))}
    if 'literal' in expression:
        return set()
    return _references(expression['left']) | _references(expression['right'])


def _evaluate(expression, tables, count, budget):
    budget[0] += count
    if budget[0] > MAX_EVALUATED_CELLS:
        raise PostprocessError('postprocess_arithmetic_work_limit')
    if 'table' in expression:
        values = _column(tables[expression['table']], expression['column'])
        if 'row' in expression:
            if expression['row'] > len(values):
                raise PostprocessError('postprocess_scalar_row_outside_table')
            return [values[expression['row'] - 1]] * count
        return values
    if 'literal' in expression:
        return [float(expression['literal'])] * count
    left = _evaluate(expression['left'], tables, count, budget)
    right = _evaluate(expression['right'], tables, count, budget)
    if len(left) != count or len(right) != count:
        raise PostprocessError('postprocess_row_alignment_mismatch')
    function = _ARITHMETIC[expression['op']]
    try:
        return [_finite(function(left[i], right[i])) for i in range(count)]
    except (ZeroDivisionError, OverflowError):
        raise PostprocessError('postprocess_undefined_arithmetic') from None


def _derive(operation, tables, budget):
    references = set()
    for column in operation['columns']:
        references |= _references(column['expression'])
    lengths = {len(tables[name]['rows']) for name, _, row in references if row is None}
    if len(lengths) > 1:
        raise PostprocessError('postprocess_row_alignment_mismatch')
    count = next(iter(lengths)) if lengths else 1
    if count * len(operation['columns']) > MAX_CELLS:
        raise PostprocessError('postprocess_derived_cell_limit')
    columns = [_evaluate(column['expression'], tables, count, budget) for column in operation['columns']]
    rows = [[column[i] for column in columns] for i in range(count)]
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(column['name'] for column in operation['columns'])
    for row in rows:
        writer.writerow(format(value, '.17g') for value in row)
    text = output.getvalue()
    provenance = []
    for name, column, row in sorted(references, key=lambda value: (value[0], value[1], value[2] or 0)):
        source = tables[name]
        entry = dict(table=name, column=column, row=row,
                     selection='scalar' if row is not None else 'column')
        if 'line_numbers' in source:
            entry['input_sha256'] = source['sha256']
            entry['source_lines'] = [source['line_numbers'][row - 1]] if row else source['line_numbers']
        else:
            # The retained derivation DAG plus this exact row selection resolves
            # every value to original source lines without inventing a join.
            entry['derivation_id'] = name
            entry['derived_csv_sha256'] = source['csv_sha256']
        provenance.append(entry)
    return dict(id=operation['id'], method='derive', columns=operation['columns'], rows=rows,
                alignment=operation['alignment'], source_tables=sorted({item[0] for item in references}),
                references=provenance,
                units_origin='declared_not_inferred', csv=text, csv_sha256=sha256(text.encode('ascii')))


def _summary(values):
    try:
        result = dict(count=len(values), mean=statistics.fmean(values),
                      sample_std=statistics.stdev(values) if len(values) > 1 else None,
                      min=min(values), max=max(values), median=statistics.median(values))
        for value in result.values():
            if value is not None:
                _finite(value)
        return result
    except (OverflowError, statistics.StatisticsError, ZeroDivisionError):
        raise PostprocessError('postprocess_undefined_statistics') from None


def _table_record(operation, tables, columns, rows, references, row_sources, alignment):
    """Retain an explicit relational operation and its complete source-row map."""
    if not rows:
        raise PostprocessError('postprocess_empty_group_or_table')
    if len(rows) > MAX_ROWS or len(rows) * len(columns) > MAX_CELLS:
        raise PostprocessError('postprocess_derived_cell_limit')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(column['name'] for column in columns)
    for row in rows:
        writer.writerow(format(_finite(value), '.17g') for value in row)
    text, provenance = output.getvalue(), []
    for name, column in sorted(references):
        source = tables[name]
        entry = dict(table=name, column=column, row=None, selection='column')
        if 'line_numbers' in source:
            entry.update(input_sha256=source['sha256'], source_lines=source['line_numbers'])
        else:
            entry.update(derivation_id=name, derived_csv_sha256=source['csv_sha256'])
        provenance.append(entry)
    return dict(id=operation['id'], method=operation['method'], columns=columns, rows=rows,
                operation=operation, alignment=alignment,
                source_tables=sorted({name for name, _ in references}), references=provenance,
                row_sources=row_sources, source_row_numbering='one_based_table_row',
                units_origin='declared_not_inferred', csv=text, csv_sha256=sha256(text.encode('ascii')))


def _consume_work(budget, cells):
    budget[0] += cells
    if budget[0] > MAX_EVALUATED_CELLS:
        raise PostprocessError('postprocess_arithmetic_work_limit')


def _key_rows(table, columns):
    positions = {column['name']: index for index, column in enumerate(table['columns'])}
    indices = [positions[name] for name in columns]
    for index, row in enumerate(table['rows'], 1):
        if indices and 'line_numbers' in table:
            # Numeric tables use binary floats. A raw identifier that changes
            # even its shortest decimal value on conversion must not silently
            # match a different original identifier after that conversion.
            line = table['raw_lines'][table['line_numbers'][index - 1] - 1]
            tokens = line.split() if table['format'] == 'whitespace' else next(csv.reader([line], strict=True))
            if any(Decimal(tokens[position].strip()) != Decimal(str(row[position])) for position in indices):
                raise PostprocessError('postprocess_key_precision_loss')
        yield index, tuple(row[position] for position in indices)


def _reduce(values, reducer):
    if not values:
        raise PostprocessError('postprocess_empty_group_or_table')
    if reducer == 'count':
        return len(values)
    if reducer == 'sum':
        try:
            return _finite(math.fsum(values))
        except (OverflowError, ValueError):
            raise PostprocessError('postprocess_undefined_statistics') from None
    value = _summary(values)['sample_std' if reducer == 'std' else reducer]
    if value is None:
        raise PostprocessError('postprocess_insufficient_group_samples')
    return _finite(value)


def _aggregate(operation, tables, budget):
    source = tables[operation['table']]
    positions = {column['name']: index for index, column in enumerate(source['columns'])}
    keys, declarations = operation['group_by'], operation['columns']
    _consume_work(budget, len(source['rows']) * (len(keys) + len(declarations)))
    groups = {}
    for index, key in _key_rows(source, keys):
        groups.setdefault(key, []).append(index)
    columns = [dict(name=name, unit=source['columns'][positions[name]]['unit']) for name in keys]
    columns += [dict(name=column['name'], unit=column['unit']) for column in declarations]
    if len(groups) * len(columns) > MAX_CELLS:
        raise PostprocessError('postprocess_derived_cell_limit')
    rows, row_sources = [], []
    for key, members in groups.items():
        values = [_reduce([source['rows'][row - 1][positions[column['column']]] for row in members],
                          column['reducer']) for column in declarations]
        rows.append(list(key) + values)
        row_sources.append([dict(table=source['id'], rows=members)])
    references = {(source['id'], name) for name in keys}
    references |= {(source['id'], column['column']) for column in declarations}
    return _table_record(operation, tables, columns, rows, references, row_sources,
                         'explicit_group_keys_first_seen_order')


def _join(operation, tables, budget):
    left, right = tables[operation['left']], tables[operation['right']]
    positions = {side: {column['name']: index for index, column in enumerate(source['columns'])}
                 for side, source in (('left', left), ('right', right))}
    _consume_work(budget, (len(left['rows']) + len(right['rows'])) * len(operation['on'])
                  + len(left['rows']) * len(operation['columns']))
    indices = {}
    for side, source in (('left', left), ('right', right)):
        mapping = {}
        for index, key in _key_rows(source, [pair[side] for pair in operation['on']]):
            if key in mapping:
                raise PostprocessError('postprocess_join_key_not_unique')
            mapping[key] = index
        indices[side] = mapping
    if indices['left'].keys() != indices['right'].keys():
        raise PostprocessError('postprocess_join_missing_pair')
    columns = [dict(name=column['name'], unit=column['unit']) for column in operation['columns']]
    if len(left['rows']) * len(columns) > MAX_CELLS:
        raise PostprocessError('postprocess_derived_cell_limit')
    rows, row_sources = [], []
    for key, left_row in indices['left'].items():
        selected = dict(left=left_row, right=indices['right'][key])
        rows.append([tables[operation[column['side']]]['rows'][selected[column['side']] - 1]
                     [positions[column['side']][column['column']]] for column in operation['columns']])
        row_sources.append([dict(table=operation[side], rows=[selected[side]]) for side in ('left', 'right')])
    references = {(operation[side], pair[side]) for pair in operation['on'] for side in ('left', 'right')}
    references |= {(operation[column['side']], column['column']) for column in operation['columns']}
    return _table_record(operation, tables, columns, rows, references, row_sources,
                         'explicit_one_to_one_keys_left_order')


def _histogram(values, operation):
    try:
        import numpy as np
    except ImportError:
        raise PostprocessError('postprocess_numpy_required') from None
    low, high = map(float, operation['range'])
    try:
        with np.errstate(all='raise'):
            counts, edges = np.histogram(values, bins=operation['bins'], range=(low, high))
        counts, edges = [int(value) for value in counts], [float(value) for value in edges]
        if any(not math.isfinite(value) for value in edges) or any(a >= b for a, b in zip(edges, edges[1:])):
            raise PostprocessError('postprocess_undefined_histogram')
    except (ValueError, OverflowError, FloatingPointError, IndexError):
        raise PostprocessError('postprocess_undefined_histogram') from None
    below = sum(value < low for value in values)
    above = sum(value > high for value in values)
    if sum(counts) + below + above != len(values):
        raise PostprocessError('postprocess_histogram_count_mismatch')
    return dict(counts=counts, edges=edges, bins=operation['bins'], range=operation['range'],
                below_range=below, above_range=above, in_range=sum(counts), total=len(values),
                implementation='numpy.histogram', numpy_version=np.__version__,
                boundary_rule='left_closed_right_open_except_final_right_closed')


def _result(operation, tables):
    table = tables[operation['table']]
    method = operation['method']
    result = dict(operation, sample_count=len(table['rows']),
                  row_selection=dict(start=0, stop=len(table['rows']), step=1),
                  units_origin='declared_not_inferred')
    units = {column['name']: column['unit'] for column in table['columns']}
    if method in ('line', 'scatter'):
        x, y = _column(table, operation['x']), _column(table, operation['y'])
        result['points'] = [[x[i], y[i]] for i in range(len(x))]
        result['value_units'] = dict(x=units[operation['x']], y=units[operation['y']])
    else:
        values = _column(table, operation['column'])
        result['values'] = _summary(values) if method == 'summary' else _histogram(values, operation)
        result['value_unit'] = units[operation['column']]
    return result


def adapter_identity():
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                analysis_source_sha256=analysis.adapter_identity()['source_sha256'],
                python=sys.version, implementation=sys.implementation.name)


def tool_context():
    """The implementation's own strict schema for the active Adapter prompt."""
    def obj(properties):
        return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)

    def array(item, minimum=1, maximum=MAX_COLUMNS):
        return dict(type='array', items=item, minItems=minimum, maxItems=maximum)

    name = dict(type='string', pattern='^' + _IDENTIFIER_PATTERN + '$')
    number = dict(type='number', minimum=-sys.float_info.max, maximum=sys.float_info.max)
    column = obj(dict(name=name, unit=dict(type='string', pattern='^' + _UNIT.pattern + '$')))
    expression = {'$ref': '#/$defs/expression'}
    ref = obj(dict(table=name, column=name))
    scalar = obj(dict(table=name, column=name, row=dict(type='integer', minimum=1, maximum=MAX_ROWS)))
    literal = obj(dict(literal=number, evidence=dict(type='string', pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$')))
    arithmetic = obj(dict(op=dict(enum=list(_ARITHMETIC)), left=expression, right=expression))
    derived_column = obj(dict(name=name, unit=column['properties']['unit'], expression=expression))
    table = obj(dict(id=name, file=dict(type='string', pattern='^' + _FILE.pattern + '$'),
                     sha256=dict(type='string', pattern='^' + _HASH.pattern + '$'),
                     format=dict(enum=['whitespace', 'csv']),
                     header_lines=dict(type='integer', minimum=0, maximum=1024), columns=array(column)))
    derive = obj(dict(id=name, method=dict(const='derive'), alignment=dict(const='row_index'),
                      columns=array(derived_column)))
    aggregate_column = obj(dict(name=name, column=name, reducer=dict(enum=list(_REDUCERS)),
                                unit=column['properties']['unit']))
    aggregate = obj(dict(id=name, method=dict(const='aggregate'), table=name,
                         group_by=array(name, 0), columns=array(aggregate_column)))
    join_key = obj(dict(left=name, right=name))
    join_column = obj(dict(name=name, side=dict(enum=['left', 'right']), column=name,
                          unit=column['properties']['unit']))
    join = obj(dict(id=name, method=dict(const='join'), left=name, right=name,
                   on=array(join_key), columns=array(join_column)))
    summary = obj(dict(id=name, method=dict(const='summary'), table=name, column=name))
    histogram = obj(dict(id=name, method=dict(const='histogram'), table=name, column=name,
                         bins=dict(type='integer', minimum=1, maximum=MAX_BINS),
                         range=array(number, 2, 2)))
    plot = obj(dict(id=name, method=dict(enum=['line', 'scatter']), table=name, x=name, y=name))
    schema = obj(dict(version=dict(type='integer', const=VERSION),
                      tables=array(table, 1, MAX_TABLES),
                      operations=array(dict(oneOf=[derive, aggregate, join, summary, histogram, plot]), 1, MAX_OPERATIONS)))
    schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    schema['$defs'] = dict(expression=dict(oneOf=[ref, scalar, literal, arithmetic]))
    example = dict(version=VERSION, tables=[dict(id='samples', file='source1', sha256=sha256(b'1\n2\n'),
                   format='whitespace', header_lines=0, columns=[dict(name='value', unit='1')])],
                   operations=[dict(id='description', method='summary', table='samples', column='value')])
    return dict(version=VERSION, adapter_identity=adapter_identity(), plan_schema=schema,
                limits=dict(tables=MAX_TABLES, operations=MAX_OPERATIONS, columns=MAX_COLUMNS,
                            rows=MAX_ROWS, cells=MAX_CELLS, input_bytes=MAX_INPUT_BYTES,
                            output_bytes=MAX_OUTPUT_BYTES, plan_bytes=MAX_PLAN_BYTES,
                            line_bytes=MAX_LINE_BYTES, ast_nodes=MAX_AST_NODES,
                            ast_depth=MAX_AST_DEPTH, evaluated_cells=MAX_EVALUATED_CELLS,
                            histogram_bins=MAX_BINS),
                contract=[
                    'files maps safe flat source IDs to verified bytes; all supplied files must be declared exactly once.',
                    'Tables and operations are ordered; a derivation may reference only existing tables.',
                    'The sixteen-table cap includes raw and derived tables. Raw rows and all numeric cells are aggregate limits.',
                    'header_lines counts physical lines exactly; no blank rows, comments or semantic headers are skipped or inferred.',
                    'Only ASCII whitespace or comma-separated finite numeric cells are parsed. Quoted multiline CSV records are rejected.',
                    'Unit strings are explicit declarations, including compound units; no physical dimensions, meanings or conversions are verified.',
                    'Literal evidence IDs must be authenticated by the calling service; this tool only checks their bounded syntax.',
                    'Full columns require equal row counts. Only an explicit one-based row selector broadcasts an observed scalar.',
                    'Arithmetic is elementwise only. No code, expression strings, interpolation, fit or physical evaluation is executed.',
                    'aggregate creates a reusable table: explicit group_by keys (or [] for all rows), first-seen group order, and named count/mean/sum/std/min/max/median columns. std is sample standard deviation and requires at least two rows per group.',
                    'aggregate count has unit 1; other reducers retain the source unit exactly. Group keys retain their source names and units. No rows are silently omitted.',
                    'join creates a reusable table using explicit numeric keys with exactly equal declared units. Both sides must have unique keys and complete pairs; no row-index, tolerance, inner-join or Cartesian fallback is permitted. Output follows left-table order.',
                    'join output columns explicitly select a side and source column with an unchanged declared unit. Composite keys are supported; grouping and matching use exact finite numeric equality, not inferred physical state equivalence.',
                    'Raw grouping/join keys must retain their numeric value in the shortest decimal float representation; keys that lose decimal precision during parsing are rejected rather than matched after rounding. Derived keys use their explicitly computed float values.',
                    'Histograms require integer bins and an increasing finite range; all outside-range samples are counted separately.',
                    'All source values, original lines, source line numbers, expressions, references and complete derived CSV are retained.',
                    'Derived references form a retained DAG; relational tables retain their exact operation plus one-based source-row lists for every output row, including all group members and both join sides.'
                ],
                minimal_example=example, synthetic_example_files_ascii={'source1': '1\n2\n'},
                scalar_ast_example=dict(table='samples', column='value', row=1),
                return_fields=dict(version='integer', scientific_status='not_evaluated', adapter_identity='tool and analysis SHA',
                                   plan='exact validated plan', plan_sha256='canonical plan SHA',
                                   sources='raw table records: SHA, size, parser, columns, rows, line_numbers, raw_lines',
                                   derived_tables='derive/aggregate/join records: columns, rows, alignment, references, CSV text and SHA; derive columns include AST; aggregate/join include operation and row_sources (one-based source table rows)',
                                   results='summary/histogram or full ordered line/scatter points with units and row selection',
                                   limitations='explicit scientific interpretation limits'))


def execute_plan(plan, files):
    """Return complete JSON-ready source records, derived CSV and plot data."""
    validate_plan(plan, files)
    # JSON round trip prevents later edits to caller metadata changing provenance.
    import json
    plan = json.loads(canonical(plan))
    files = dict(files)
    validate_plan(plan, files)
    tables, sources, derived, results = {}, [], [], []
    rows = cells = 0
    for declaration in plan['tables']:
        data = files[declaration['file']]
        if sha256(data) != declaration['sha256']:
            raise PostprocessError('postprocess_raw_sha256_mismatch')
        parsed = parse_table(data, declaration)
        rows += len(parsed['rows'])
        cells += len(parsed['rows']) * len(parsed['columns'])
        if rows > MAX_ROWS or cells > MAX_CELLS:
            raise PostprocessError('postprocess_aggregate_row_or_cell_limit')
        tables[parsed['id']] = parsed
        sources.append(parsed)
    report = dict(version=VERSION, scientific_status='not_evaluated', adapter_identity=adapter_identity(),
                  plan=plan, plan_sha256=sha256(canonical(plan)), sources=sources,
                  derived_tables=derived, results=results,
                  limitations=['Column meanings, units and literal evidence are declared by the caller.',
                               'Row-index alignment does not establish matching physical states.',
                               'Descriptive statistics do not estimate independent replicate uncertainty.'])
    if len(canonical(report)) > MAX_OUTPUT_BYTES:
        raise PostprocessError('postprocess_output_limit')
    budget = [0]
    for operation in plan['operations']:
        if operation['method'] in TABLE_METHODS:
            function = {'derive': _derive, 'aggregate': _aggregate, 'join': _join}[operation['method']]
            table = function(operation, tables, budget)
            cells += len(table['rows']) * len(table['columns'])
            if cells > MAX_CELLS:
                raise PostprocessError('postprocess_derived_cell_limit')
            tables[table['id']] = table
            derived.append(table)
        else:
            results.append(_result(operation, tables))
        if len(canonical(report)) > MAX_OUTPUT_BYTES:
            raise PostprocessError('postprocess_output_limit')
    return report
