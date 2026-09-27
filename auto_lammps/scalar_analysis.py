"""Read declared LAMMPS ave/time scalar output without rewriting its bytes.

A separate adapter keeps already frozen numeric_tables_v1 identities unchanged.
Units are declarations, never inferred from LAMMPS variable names. No engine or
model-generated code is executed here.
"""
from pathlib import Path
import re
import sys

from . import analysis
from .manifest import canonical, sha256

VERSION = 1
FORMAT = 'lammps_ave_time_scalar'
AnalysisError = analysis.AnalysisError


def adapter_identity():
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                arithmetic=analysis.adapter_identity(), python=sys.version)


def validate_table(table):
    required = {'file', 'format', 'headers', 'columns', 'steps'}
    if not isinstance(table, dict) or set(table) != required or table['format'] != FORMAT:
        raise AnalysisError('Declare a LAMMPS scalar table format')
    columns = table['columns']
    if not isinstance(columns, list) or not 2 <= len(columns) <= 16:
        raise AnalysisError('Declare the timestep and one to fifteen scalar columns')
    for c in columns:
        if not isinstance(c, dict) or set(c) != {'name', 'unit', 'source'}:
            raise AnalysisError('Each column needs a name, unit and source label')
        if not isinstance(c['source'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\[\]]{0,127}', c['source']):
            raise AnalysisError('Invalid scalar source label')
    if len({c['source'] for c in columns}) != len(columns):
        raise AnalysisError('Scalar source labels must be distinct')
    if columns[0]['source'] != 'TimeStep' or columns[0]['unit'] != 'step':
        raise AnalysisError('The first scalar column must be TimeStep in step units')
    headers = table['headers']
    if (not isinstance(headers, list) or len(headers) != 2
            or any(not isinstance(h, str) or not h.startswith('# ') or len(h) > 4096
                   or any(ord(c) < 32 or ord(c) > 126 for c in h) for h in headers)
            or headers[1].split() != ['#'] + [c['source'] for c in columns]):
        raise AnalysisError('Declare both scalar headers and their exact column mapping')
    steps = table['steps']
    if (not isinstance(steps, dict) or set(steps) != {'first', 'last', 'stride'}
            or any(type(v) is not int or v < 0 or v > 2**63-1 for v in steps.values())
            or steps['stride'] == 0 or steps['first'] > steps['last']
            or (steps['last']-steps['first']) % steps['stride']):
        raise AnalysisError('Declare an integer first, last and positive stride')
    count = (steps['last']-steps['first']) // steps['stride'] + 1
    if count > analysis.MAX_ROWS or count*len(columns) > 500000:
        raise AnalysisError('Declared scalar table exceeds row or cell limits')
    # Existing plan validation owns identifier, units and output-name policy.
    numeric = numeric_table(table)
    operation = dict(id='validate', method='last', file=table['file'],
                     x=columns[0]['name'], y=columns[1]['name'], window=[0, 0])
    analysis.validate_plan(dict(tables=[numeric], operations=[operation]), [table['file']])
    return table


def numeric_table(table):
    return dict(file=table['file'], columns=[dict(name=c['name'], unit=c['unit']) for c in table['columns']])


def parse_scalar(data, table):
    """Return original one-based source lines and values, with integer steps."""
    validate_table(table)
    if not isinstance(data, bytes) or len(data) > analysis.MAX_TABLE_BYTES:
        raise AnalysisError('Scalar table exceeds byte limit or is not bytes')
    if not data.endswith(b'\n'):
        raise AnalysisError('Scalar table has an incomplete final line')
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError as exc:
        raise AnalysisError('Scalar table must be ASCII') from exc
    headers = 0
    rows = []
    expected = table['steps']['first']
    for line_no, line in enumerate(text.split('\n'), 1):
        if line.endswith('\r'):
            line = line[:-1]
        if len(line) > 4096 or any(ord(c) < 32 and c != '\t' for c in line):
            raise AnalysisError('Invalid scalar line')
        line = line.strip()
        if not line:
            continue
        if headers < 2:
            if line != table['headers'][headers]:
                raise AnalysisError('Scalar headers differ from declared format')
            headers += 1
            continue
        tokens = line.split()
        if len(tokens) != len(table['columns']) or not re.fullmatch(r'\d+', tokens[0]):
            raise AnalysisError('Expected a scalar row with an integer timestep')
        step = int(tokens[0])
        if step != expected or step > table['steps']['last']:
            raise AnalysisError('Missing, duplicate, reversed or unexpected timestep')
        try:
            values = [step] + [float(t) for t in tokens[1:]]
        except ValueError as exc:
            raise AnalysisError('Nonnumeric scalar output') from exc
        for v in values:
            analysis._finite(v)
        rows.append((line_no, values))
        expected += table['steps']['stride']
    if headers != 2 or not rows or rows[-1][1][0] != table['steps']['last']:
        raise AnalysisError('Scalar table is missing headers or declared samples')
    return rows


def analyze_scalar(data, table, operations):
    """Pure reader/arithmetic adapter; caller owns receipt and plan authority.

    This does not certify scheduler completion, scientific agreement, or that a
    supplied plan was frozen before running. Preserve that distinction in callers.
    """
    validate_table(table)
    numeric = numeric_table(table)
    analysis.validate_plan(dict(tables=[numeric], operations=operations), [table['file']])
    rows = parse_scalar(data, table)
    results = []
    for op in operations:
        value = analysis.calculate(rows, numeric, op)
        value['units_origin'] = 'declared_only_not_present_in_scalar_header'
        results.append(value)
    return dict(schema_version=1, status='analyzed', scientific_status='not_evaluated',
                execution_status='not_checked', plan_status='caller_supplied',
                adapter_identity=adapter_identity(),
                plan_sha256=sha256(canonical(dict(table=table, operations=operations))),
                source=dict(file=table['file'], sha256=sha256(data), size=len(data),
                            format=FORMAT, headers=table['headers'], columns=table['columns'],
                            steps=table['steps'], sample_count=len(rows)),
                results=results,
                limitations=['Units and physical definitions require separate workflow evidence.',
                             'Declared cadence detects missing samples, not scientific validity.',
                             'No unit, stress sign, strain or volume conversion is inferred.'])
