"""Bounded source views and retained-byte access to existing analysis adapters.

This is an orchestration bridge, not another scientific implementation. Native
plans use analysis_v2's unchanged validator, scalar/legacy parsers and arithmetic,
and the fixed isolated OVITO worker. Previews never enter those computations.
"""
from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile

from . import analysis, analysis_v2, coordination_analysis as coordination
from . import postprocess_tables as tables, scalar_analysis as scalar
from .manifest import canonical, sha256

VERSION = 1
MODE = 'mixed_native_v1'
REPRESENTATION = 'bounded_source_preview_v1'
MAX_FILES = 32
MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_PREVIEW_BYTES = 6144
MAX_PREVIEW_LINES = 12
MAX_OUTPUT_BYTES = tables.MAX_OUTPUT_BYTES
METHODS = {'summary', 'last', 'linear_fit', coordination.METHOD}
RAW_FORMATS = {'whitespace', 'csv'}
AnalysisError = analysis.AnalysisError


class NativeExecutionError(RuntimeError):
    """A fixed worker/runtime failed; replay the paid proposal, not the model."""


def describe(data):
    """Scan every physical line; expose only bounded, labeled source excerpts."""
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise AnalysisError('retained_source_byte_limit')
    try:
        content = data.decode('ascii')
    except UnicodeDecodeError:
        raise AnalysisError('retained_source_ascii_required') from None
    if any(ord(c) < 32 and c not in '\r\n\t' or ord(c) == 127 for c in content):
        raise AnalysisError('retained_source_control_character')
    lines = list(io.StringIO(content, newline=''))
    if len(lines) > MAX_ROWS or any(len(line) > tables.MAX_LINE_BYTES for line in lines):
        raise AnalysisError('retained_source_row_or_line_limit')
    head = 10 if data.startswith(b'ITEM: TIMESTEP') else 6
    selected = sorted(set(range(min(head, len(lines)))) | set(range(max(0, len(lines)-2), len(lines))))
    excerpts = []
    remaining = MAX_PREVIEW_BYTES
    for index in selected:
        if not remaining:
            break
        value = lines[index][:remaining]
        excerpts.append(dict(line=index+1, text=value, complete_line=len(value)==len(lines[index])))
        remaining -= len(value)
    numeric_rows = 0
    frame_count = 0
    timesteps = []
    atom_headers = []
    type_count_sets = []
    atom_columns = None
    type_counts = None
    counted_frames = 0
    counts_complete = True
    def finish_atoms():
        nonlocal counted_frames,counts_complete,atom_columns,type_counts
        if atom_columns is not None:
            counted_frames += 1
            if type_counts is None:
                counts_complete = False
            elif type_counts not in type_count_sets:
                if len(type_count_sets) < 16:type_count_sets.append(dict(type_counts))
                else:counts_complete = False
        atom_columns = None
        type_counts = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        tokens = stripped.replace(',', ' ').split()
        if tokens and all(tables._NUMBER.fullmatch(token) for token in tokens):
            numeric_rows += 1
        if stripped.startswith('ITEM:'):
            finish_atoms()
        if stripped == 'ITEM: TIMESTEP':
            frame_count += 1
            if index+1 < len(lines) and lines[index+1].strip().isdigit():
                step = int(lines[index+1].strip())
                if len(timesteps) < 2:
                    timesteps.append(step)
                else:
                    timesteps[-1] = step
        if stripped.startswith('ITEM: ATOMS '):
            header = stripped.removeprefix('ITEM: ATOMS ').split()
            if header not in atom_headers:
                if len(atom_headers) >= 16 or len(header) > 32:
                    raise AnalysisError('retained_source_dump_header_limit')
                atom_headers.append(header)
            atom_columns = header
            type_counts = {} if 'type' in header else None
        elif atom_columns is not None:
            position = atom_columns.index('type') if 'type' in atom_columns else None
            if (type_counts is not None and position is not None and len(tokens)==len(atom_columns)
                    and tokens[position].isdigit() and 1 <= int(tokens[position]) <= 9999):
                kind = tokens[position]
                type_counts[kind] = type_counts.get(kind,0)+1
            else:type_counts = None
    finish_atoms()
    # Newlines in a source quote remain original. The gap marker is never itself
    # source evidence: callers additionally check every output quote in raw bytes.
    parts = []
    previous = None
    for item in excerpts:
        if previous is not None and item['line'] != previous+1:
            parts.append('\n[OMITTED SOURCE LINES: PREVIEW ONLY]\n')
        parts.append(item['text'])
        previous = item['line']
    inventory = dict(size=len(data), sha256=sha256(data), line_count=len(lines),
        numeric_line_count=numeric_rows, format_hint='empty' if not data else 'lammps_dump' if frame_count else 'numeric_text',
        preview=dict(only_preview=True, complete_source_shown=len(excerpts)==len(lines)
            and all(item['complete_line'] for item in excerpts), lines=excerpts),
        final_line_terminated=data.endswith(b'\n'))
    if not data:inventory['empty']=True
    if frame_count:
        inventory.update(frame_count=frame_count, first_timestep=timesteps[0] if timesteps else None,
            last_timestep=timesteps[-1] if timesteps else None, atom_column_sets=atom_headers,
            observed_type_count_sets=type_count_sets,
            type_count_sets_cover_all_frames=counts_complete and counted_frames==frame_count)
    return inventory, ''.join(parts)


def check_files(files):
    if type(files) is not dict or not 1 <= len(files) <= MAX_FILES:
        raise AnalysisError('retained_source_file_limit')
    total = 0
    rows = 0
    inventory = {}
    for file, data in files.items():
        if type(file) is not str or not tables._FILE.fullmatch(file):
            raise AnalysisError('retained_source_flat_identity_required')
        total += len(data) if type(data) is bytes else MAX_BYTES+1
        if total > MAX_BYTES:
            raise AnalysisError('retained_source_byte_limit')
        item, _ = describe(data)
        rows += item['line_count']
        if rows > MAX_ROWS:
            raise AnalysisError('retained_source_aggregate_row_limit')
        inventory[file] = item
    return inventory


def is_mixed(plan):
    return isinstance(plan, dict) and plan.get('mode') == MODE


def _raw_declaration(table, files):
    """Bind exact bytes and reuse the existing raw-table declaration validator."""
    bound = deepcopy(table)
    if set(bound) == {'id','file','format','header_lines','columns'}:
        bound['sha256'] = sha256(files[bound['file']])
    tables._fields(bound, {'id','file','sha256','format','header_lines','columns'})
    tables._columns(bound['columns'])
    if bound['sha256'] != sha256(files[bound['file']]):
        raise AnalysisError('retained_mixed_raw_sha256_mismatch')
    # Validation only, not an executed statistic or an added scientific action.
    operation = dict(id='SourceCheck' if bound['id']=='SourceValidation' else 'SourceValidation',
        method='summary',table=bound['id'],column=bound['columns'][0]['name'])
    tables.validate_plan(dict(version=tables.VERSION,tables=[bound],operations=[operation]),
        {bound['file']:files[bound['file']]})
    return bound


def bind_source_digests(plan, files):
    """The service binds SHA values; model-supplied different SHAs are rejected."""
    for index, table in enumerate(plan['plan']['tables']):
        if isinstance(table,dict) and table.get('format') in RAW_FORMATS:
            if not isinstance(table.get('file'),str) or table['file'] not in files:
                raise AnalysisError('retained_mixed_raw_source_not_declared')
            plan['plan']['tables'][index] = _raw_declaration(table,files)
    return plan


def _validate_native_declarations(native, files):
    if not isinstance(native,dict) or not isinstance(native.get('tables'),list):
        analysis_v2.validate_plan(native,files)
    projected = deepcopy(native)
    for index, table in enumerate(projected.get('tables',[])):
        if isinstance(table,dict) and table.get('format') in RAW_FORMATS:
            if not isinstance(table.get('file'),str) or table['file'] not in files:
                raise AnalysisError('retained_mixed_raw_source_not_declared')
            bound = _raw_declaration(table,files)
            # Raw units (including unknown/compound declarations) have already
            # passed their mature validator. analysis_v2 owns source counts,
            # names, methods, x/y and window validation, not the raw parser's
            # wider unit vocabulary. This dimensionless validation placeholder
            # never enters parsing, arithmetic, the retained plan or a receipt.
            projected['tables'][index] = dict(file=bound['file'],
                columns=[dict(name=c['name'],unit='1') for c in bound['columns']])
    analysis_v2.validate_plan(projected,files)


def _output_columns(operation):
    if operation['method'] == coordination.METHOD:
        return [dict(name=name, unit='step' if name=='timestep' else '1') for name in
            ('frame', 'timestep', 'type_i', 'type_j', 'directed_neighbor_count',
             'N_i', 'N_j', 'p_j_given_i', 'c_j', 'alpha_ij', 'alpha_symmetric')]
    # sample_std is retained in the native report; it may be null for one sample
    # and is therefore not exposed as a finite-numeric plotting column.
    unit = operation['_y_unit']
    xu = operation['_x_unit']
    units = ({'value':unit, 'x':xu} if operation['method']=='last' else
        {'mean':unit, 'min':unit, 'max':unit} if operation['method']=='summary' else
        {'slope':unit if xu=='1' else unit+'/'+xu, 'intercept':unit,
         'rmse':unit, 'r_squared':'1'})
    # Constant-y fits can have null r_squared too; it remains in the native report.
    units.pop('r_squared', None)
    return [dict(name='sample_count', unit='1')]+[dict(name=key, unit=value) for key,value in units.items()]


def output_columns(plan):
    declarations = {table['file']:table for table in plan['plan']['tables']}
    result = {}
    for index, original in enumerate(plan['plan']['operations'], 1):
        operation = dict(original)
        if operation['method'] != coordination.METHOD:
            units = {c['name']:c['unit'] for c in declarations[operation['file']]['columns']}
            operation.update(_y_unit=units[operation['y']], _x_unit=units[operation['x']])
        result['result'+str(index)] = _output_columns(operation)
    return result


def validate_plan(plan, files):
    if not is_mixed(plan) or set(plan) != {'mode','plan','figures','method_evidence'}:
        raise AnalysisError('retained_mixed_explicit_mode_and_fields_required')
    check_files(files)
    native = plan['plan']
    _validate_native_declarations(native, files)
    if {table['file'] for table in native['tables']} != set(files):
        raise AnalysisError('retained_mixed_every_source_must_be_declared')
    if any(operation['method'] not in METHODS for operation in native['operations']):
        raise AnalysisError('retained_mixed_method_not_exposed')
    # The bridge is post-hoc, user-requested analysis. No fake frozen manifest or
    # scheduler success is manufactured to call analyze_collected.
    for table in native['tables']:
        if table.get('format') not in (None, scalar.FORMAT, coordination.FORMAT, *RAW_FORMATS):
            raise AnalysisError('retained_mixed_parser_not_exposed')
        if not any(operation['file']==table['file'] for operation in native['operations']):
            raise AnalysisError('retained_mixed_source_operation_required')
    outputs = output_columns(plan)
    figures = plan['figures']
    if type(figures) is not list or len(figures) > 16:
        raise AnalysisError('retained_mixed_figure_limit')
    seen = set()
    for figure in figures:
        tables._fields(figure, {'id','method','table','x','y'})
        name = tables._name(figure['id'])
        columns = {c['name'] for c in outputs.get(figure['table'], [])}
        if (name in seen or figure['method'] not in {'line','scatter'}
                or figure['x']==figure['y'] or figure['x'] not in columns or figure['y'] not in columns):
            raise AnalysisError('retained_mixed_invalid_figure_selection')
        seen.add(name)
    targets = {table['file'] for table in native['tables']} | {op['id'] for op in native['operations']}
    if len(targets) != len(native['tables'])+len(native['operations']):
        raise AnalysisError('retained_mixed_method_target_collision')
    evidence = plan['method_evidence']
    if type(evidence) is not list or len(evidence) != len(targets):
        raise AnalysisError('retained_mixed_parser_and_method_evidence_required')
    seen = set()
    for item in evidence:
        tables._fields(item, {'target','citations'})
        refs = item['citations']
        if (item['target'] not in targets or item['target'] in seen or type(refs) is not list
                or not 1 <= len(refs) <= 16 or any(type(ref) is not str for ref in refs)
                or len(set(refs)) != len(refs)):
            raise AnalysisError('retained_mixed_parser_and_method_evidence_required')
        seen.add(item['target'])
    if len(canonical(plan)) > tables.MAX_PLAN_BYTES:
        raise AnalysisError('retained_mixed_plan_limit')
    return plan


def column_units(plan, files):
    validate_plan(plan, files)
    expected = {}
    for table in plan['plan']['tables']:
        if table.get('format') == coordination.FORMAT:
            meta, _ = describe(files[table['file']])
            if len(meta.get('atom_column_sets', [])) != 1:
                raise AnalysisError('retained_mixed_consistent_atom_columns_required')
            for name in meta['atom_column_sets'][0]:
                if not tables._FILE.fullmatch(name):
                    raise AnalysisError('retained_mixed_atom_column_identity')
                unit = ('angstrom' if name in {'x','y','z','xu','yu','zu'} else
                        '1' if name in {'id','type','xs','ys','zs','xsu','ysu','zsu','ix','iy','iz'} else 'unknown')
                expected[table['file'],name] = unit
        else:
            expected.update({(table['file'],c['name']):c['unit'] for c in table['columns']})
    for name, columns in output_columns(plan).items():
        expected.update({(name,c['name']):c['unit'] for c in columns})
    return expected


def adapter_identity():
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
        mixed_analysis=analysis_v2.adapter_identity(), table_tools=tables.adapter_identity())


def display_results(report):
    """Project native summaries onto the existing output panel's field names.

    The native report is retained unchanged. Values, units and exact selections
    are copied from completed arithmetic; no statistic or meaning is inferred.
    Legacy postprocess results already satisfy that display contract.
    """
    results = deepcopy(report['results'])
    if report.get('mode') == MODE:
        for result in results:
            if result['method'] == 'summary':
                result.update(table=result['file'],column=result['y'],
                    value_unit=result['value_units']['mean'])
                result['values']['count'] = result['sample_count']
    return results


def plan_schema():
    """Machine-readable forms of the existing native validator contracts."""
    def obj(properties):
        return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
    def array(items,minimum=1,maximum=32):
        return dict(type='array',items=items,minItems=minimum,maxItems=maximum)
    name=dict(type='string',pattern='^'+tables._IDENTIFIER_PATTERN+'$')
    file=dict(type='string',pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    unit=dict(type='string',enum=sorted(analysis.UNITS))
    column=obj(dict(name=name,unit=unit))
    numeric=obj(dict(file=file,columns=array(column,2,16)))
    raw_schema=deepcopy(tables.tool_context()['plan_schema']['properties']['tables']['items'])
    del raw_schema['properties']['sha256'];raw_schema['required'].remove('sha256')
    raw_schema['properties']['columns']['minItems']=analysis.MIN_COLUMNS
    scalar_column=obj(dict(name=name,unit=unit,source=dict(type='string',pattern=r'^[A-Za-z_][A-Za-z0-9_\[\]]{0,127}$')))
    scalar_table=obj(dict(file=file,format=dict(const=scalar.FORMAT),
        headers=array(dict(type='string',pattern='^# ',maxLength=4096),2,2),
        columns=array(scalar_column,2,16),steps=obj(dict(
            first=dict(type='integer',minimum=0,maximum=2**63-1),
            last=dict(type='integer',minimum=0,maximum=2**63-1),
            stride=dict(type='integer',minimum=1,maximum=2**63-1)))))
    element_map=dict(type='object',minProperties=2,maxProperties=coordination.MAX_ELEMENTS,
        propertyNames=dict(pattern=r'^[1-9][0-9]{0,3}$'),
        additionalProperties=dict(type='string',pattern=r'^[A-Z][a-z]?$'))
    count_map=dict(type='object',minProperties=2,maxProperties=coordination.MAX_ELEMENTS,
        propertyNames=dict(pattern=r'^[1-9][0-9]{0,3}$'),
        additionalProperties=dict(type='integer',minimum=1,maximum=coordination.MAX_ATOMS))
    trajectory=obj(dict(file=file,format=dict(const=coordination.FORMAT),
        length_unit=dict(const='angstrom'),elements=element_map,expected_counts=count_map,
        pbc=dict(const=[True,True,True])))
    number=dict(type='number')
    numeric_operation=obj(dict(id=name,method=dict(enum=['summary','last','linear_fit']),
        file=file,x=name,y=name,window=array(number,2,2)))
    structural=dict(id=name,method=dict(const=coordination.METHOD),file=file,
        neighbors=dict(type='integer',minimum=1,maximum=coordination.MAX_NEIGHBORS),
        neighbor_selection=dict(const='nearest_k'),
        frames=obj(dict(first=dict(type='integer',minimum=0,maximum=2**31-1),
            last=dict(type='integer',minimum=0,maximum=2**31-1),
            stride=dict(type='integer',minimum=1,maximum=2**31-1))),
        aggregation=dict(const='equal_frame_mean'),pair_mode=dict(const='directed_and_symmetric'))
    bounded=deepcopy(structural)
    bounded.update(neighbor_selection=dict(const='bounded_shell'),
        max_distance=dict(type='number',exclusiveMinimum=0),shell_gap=dict(type='number',exclusiveMinimum=0))
    native=obj(dict(tables=array(dict(oneOf=[numeric,scalar_table,trajectory,raw_schema]),1,analysis_v2.MAX_V3_SOURCES),
        operations=array(dict(oneOf=[numeric_operation,obj(structural),obj(bounded)]),1,analysis.MAX_OPERATIONS)))
    plot=obj(dict(id=name,method=dict(enum=['line','scatter']),table=dict(type='string',pattern=r'^result[1-9][0-9]?$'),x=name,y=name))
    evidence=obj(dict(target=file,citations=array(dict(type='string',minLength=1,maxLength=80),1,16)))
    return obj(dict(mode=dict(const=MODE),plan=native,figures=array(plot,0,16),method_evidence=array(evidence,2,61)))


def tool_context():
    """Existing parser/operator contracts, with a small explicit mode wrapper."""
    return dict(mode=MODE, adapter_identity=adapter_identity(),plan_schema=plan_schema(),
        wrapper_fields=dict(mode=MODE, plan='analysis_v2 {tables,operations}; numeric sources may additionally use the existing explicit postprocess_tables whitespace/csv declaration',
            figures='list of existing {id,method:line|scatter,table:resultN,x,y} selections; [] renders no plots',
            method_evidence='one {target,citations:[citation ids]} per source file and operation id'),
        numeric_table_forms=[dict(file='source1', columns=[dict(name='step',unit='step'),dict(name='value',unit='1')]),
            dict(id='raw_samples',file='source1',format='whitespace',header_lines=0,
                columns=[dict(name='step',unit='step'),dict(name='value',unit='unknown')]),
            dict(file='source1',format=scalar.FORMAT,headers=['# Synthetic scalar data','# TimeStep v_value'],
                columns=[dict(name='step',unit='step',source='TimeStep'),dict(name='value',unit='1',source='v_value')],
                steps=dict(first=0,last=2,stride=1))],
        parser_selection=dict(labeled='No format field: exact # columns: / # units: headers must actually exist.',
            raw='format whitespace or csv: full existing {id,file,format,header_lines,columns}; header_lines=0 explicitly selects headerless numeric input. Do not add or invent headers. Service binds sha256; omit sha256 in model output.',
            scalar='format lammps_ave_time_scalar: both real headers, source labels and exact steps are required.'),
        minimal_headerless_example=dict(mode=MODE,
            plan=dict(tables=[dict(id='raw_samples',file='source1',format='whitespace',header_lines=0,
                columns=[dict(name='step',unit='step'),dict(name='value',unit='unknown')])],
                operations=[dict(id='description',method='summary',file='source1',x='step',y='value',window=[0,2])]),
            figures=[],method_evidence=[dict(target='source1',citations=['raw_method']),dict(target='description',citations=['raw_method'])]),
        numeric_operations=dict(methods=['summary','last','linear_fit'],
            fields=['id','method','file','x','y','window'], window='explicit inclusive [low,high]; no automatic selection'),
        structural_contract=coordination.GUIDE,
        output_tables=dict(naming='resultN, N is the one-based operation position; each complete table is exported as CSV',
            numeric='sample_count plus last:value,x; summary:mean,min,max; linear_fit:slope,intercept,rmse. Null-capable sample_std/r_squared stay in full native JSON.',
            structural=[c['name'] for c in _output_columns(dict(method=coordination.METHOD))],
            structure_rows='every selected frame and every directed element pair; symmetric value is copied from the existing worker result, never recomputed',
            evidence='column_evidence covers every raw column (table=source ID) and every resultN column'),
        limits=dict(source_files=MAX_FILES,input_bytes=MAX_BYTES,aggregate_physical_lines=MAX_ROWS,
            native_mixed_sources=analysis_v2.MAX_V3_SOURCES,numeric_sources=analysis_v2.MAX_TABLES,
            structural_sources=analysis_v2.MAX_TABLES,numeric_rows_per_file=analysis.MAX_ROWS,
            numeric_bytes_per_file=analysis.MAX_TABLE_BYTES,
            raw_table_bytes_per_file=tables.MAX_INPUT_BYTES,raw_table_rows_per_file=tables.MAX_ROWS,
            raw_table_cells_per_file=tables.MAX_CELLS,
            numeric_cells_per_file=500000,operations=analysis.MAX_OPERATIONS,figures=16),
        contract=['Previews are for method/column selection only. Full retained source bytes are parsed for all calculations.',
            'Every input must have an explicit parser and operation; no silently skipped input, automatic frame/window, unit inference or source reduction.',
            'Empty files remain in the source inventory and verified bytes with their SHA and zero lines. Current ready plans do not accept empty numeric/trajectory tables or provide an empty-source exclusion: return needs_evidence rather than omit an empty source or invent samples.',
            'The plan is selected after collection and is not a pre-execution frozen analysis or scientific acceptance.',
            'Numeric labeled tables require exact # columns: and # units: headers. Native scalar tables require both exact headers and complete integer cadence.',
            'Headerless inputs must explicitly choose format whitespace/csv and header_lines=0. This reuses postprocess_tables.parse_table unchanged, including SHA, finite-cell and physical-line checks. Header selection never rewrites source bytes.',
            'Raw columns retain the original declared units (including unknown); numeric unit meanings are not inferred from values or replaced by validation placeholders.',
            'Source parser/mapping and every operation require authenticated method_evidence. Quotations establish provenance, not scientific correctness.',
            'Fixed OVITO worker, one source at a time. No generated code, energy, force, minimization, dynamics, HPC submission or network tool.'])


def _csv_table(identifier, columns, rows, references, operation):
    text = io.StringIO(newline='')
    writer = csv.writer(text, lineterminator='\n')
    writer.writerow(column['name'] for column in columns)
    for row in rows:
        for value in row:analysis._finite(value)
        writer.writerow(str(value) if type(value) is int else format(value, '.17g') for value in row)
    content = text.getvalue()
    return dict(id=identifier,columns=columns,rows=rows,csv=content,csv_sha256=sha256(content.encode('ascii')),
        references=references,source_tables=[operation['file']],operation=operation,
        units_origin='declared_not_inferred')


def execute_plan(plan, files):
    plan = deepcopy(plan)
    files = dict(files)
    bind_source_digests(plan,files)
    validate_plan(plan, files)
    inventory = check_files(files)
    declarations = {table['file']:table for table in plan['plan']['tables']}
    columns = output_columns(plan)
    sources, derived, numeric, structural, selections = [], [], [], [], []
    results = {}
    total_numeric_rows = 0
    # One source is parsed at a time, matching the existing mixed adapter. Input
    # bytes remain the caller's verified originals, never the model's excerpts.
    with tempfile.TemporaryDirectory(prefix='auto-lammps-analysis-') as directory:
        for file, table in declarations.items():
            data = files[file]
            identity = inventory[file]
            source = dict(file=file,sha256=identity['sha256'],size=len(data),
                line_count=identity['line_count'],parser=deepcopy(table),complete_source_authenticated=True)
            sources.append(source)
            operations = [(index,op) for index,op in enumerate(plan['plan']['operations'],1) if op['file']==file]
            if table.get('format') == coordination.FORMAT:
                path = Path(directory)/file
                path.write_bytes(data)
                for index, operation in operations:
                    try:
                        result = coordination.analyze_isolated(path,table,operation,identity)
                    except AnalysisError as error:
                        if str(error).startswith(('Structural tool ','Structural worker ',
                                'Application OVITO ','OVITO could not')):
                            raise NativeExecutionError('retained_native_worker_unavailable') from error
                        raise
                    structural.append(result)
                    values = []
                    for frame in result['frames']:
                        symmetric = {(int(row[0]),int(row[1])):row[2] for row in frame['symmetric']}
                        for row in frame['directed']:
                            pair = tuple(sorted((int(row[0]),int(row[1]))))
                            values.append([frame['frame'],frame['timestep']]+row+[symmetric[pair]])
                    selection = dict(operation_id=operation['id'],file=file,source_sha256=identity['sha256'],
                        full_source_line_count=identity['line_count'],source_frame_count=result['source_frame_count'],
                        selected_frame_count=result['selected_frame_count'],
                        frames=[dict(frame=f['frame'],timestep=f['timestep'],atom_count=f['atom_count']) for f in result['frames']],
                        parameters=operation,worker_receipt=result.get('worker_receipt'))
                    references = [selection]
                    results[index] = _csv_table('result'+str(index),columns['result'+str(index)],values,references,operation)
                    selections.append(selection)
                path.unlink()
            else:
                if table.get('format') in RAW_FORMATS:
                    parsed = tables.parse_table(data,table)
                    rows = list(zip(parsed['line_numbers'],parsed['rows']))
                    source.update(parser_adapter_identity=tables.adapter_identity(),
                        source_line_ranges=[[parsed['line_numbers'][0],parsed['line_numbers'][-1]]])
                    del parsed
                elif 'format' in table:rows = scalar.parse_scalar(data,table)
                else:
                    try:rows = analysis.parse_table(data,table)
                    except AnalysisError as error:
                        if str(error)=='Table labels or units differ from the frozen plan':
                            raise AnalysisError('retained_mixed_labeled_headers_mismatch: no-format declarations require actual # columns: and # units: headers; for headerless numeric bytes explicitly select format whitespace with header_lines=0, without rewriting data') from error
                        raise
                total_numeric_rows += len(rows)
                if total_numeric_rows > MAX_ROWS:
                    raise AnalysisError('retained_mixed_aggregate_row_limit')
                source.update(row_count=len(rows),source_line_extent=[rows[0][0],rows[-1][0]],
                    source_lines_sha256=sha256(canonical([number for number,_ in rows])))
                for index, operation in operations:
                    result = analysis.calculate(rows,table,operation)
                    if table.get('format') in RAW_FORMATS:
                        result['units_origin']='declared_only_not_verified_in_raw_table'
                    elif 'format' in table:
                        result['units_origin']='declared_only_not_present_in_scalar_header'
                    numeric.append(result)
                    selection = dict(operation_id=operation['id'],file=file,source_sha256=identity['sha256'],
                        full_source_line_count=identity['line_count'],parsed_row_count=len(rows),
                        sample_count=result['sample_count'],source_line_ranges=result['source_line_ranges'],parameters=operation)
                    if table.get('format') in RAW_FORMATS:
                        selection.update(parser=deepcopy(table),parser_adapter_identity=tables.adapter_identity())
                    out_columns = columns['result'+str(index)]
                    values = [[result['sample_count'] if c['name']=='sample_count' else result['values'][c['name']]
                        for c in out_columns]]
                    results[index] = _csv_table('result'+str(index),out_columns,values,[selection],operation)
                    selections.append(selection)
                del rows
    positions = {op['id']:i for i,op in enumerate(plan['plan']['operations'])}
    for collection in (numeric,structural,selections):
        collection.sort(key=lambda item:positions[item.get('operation_id',item.get('id'))])
    derived = [results[index] for index in sorted(results)]
    # Present only explicitly selected plots to the unchanged renderer. Complete
    # native reports and output CSVs remain in the separately retained report.
    figure_report = dict(version=1,scientific_status='not_evaluated',
        plan=dict(version=1,tables=[],operations=plan['figures']),
        sources=[],derived_tables=derived,results=[tables._result(op,{t['id']:t for t in derived}) for op in plan['figures']])
    figure_report['plan_sha256']=sha256(canonical(figure_report['plan']))
    figure_report['analysis_plan_sha256']=sha256(canonical(plan))
    report = dict(version=1,mode=MODE,scientific_status='not_evaluated',adapter_identity=adapter_identity(),
        plan=plan,plan_sha256=sha256(canonical(plan)),sources=sources,derived_tables=derived,
        results=numeric,structural_results=structural,operation_sources=selections,
        parsed_numeric_row_count=total_numeric_rows,figure_report=figure_report,
        limitations=['Selected after collection; this is not the frozen simulation analysis or scientific acceptance.',
            'Unit and element meanings require separate workflow/method evidence; matching quotes do not prove them.',
            'Frame scatter and sample standard deviation are descriptive, not independent replicate uncertainty.',
            'Previews were not used for numeric or structural calculations.'])
    if len(canonical(report)) > MAX_OUTPUT_BYTES:
        raise AnalysisError('retained_mixed_output_limit')
    return report
