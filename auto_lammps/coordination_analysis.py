"""Frozen Warren-Cowley statistics from collected trajectories, using OVITO.

This adapter reads geometry only. It never evaluates a potential, starts a
simulation, chooses a sampling window, or compares with reference answers.
Derived tables stay in the already accounted analysis report.
"""
from importlib import metadata
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import sys

from .analysis import AnalysisError, _name, _finite
from .analysis_runtime import REQUIRED
from .manifest import canonical, sha256

VERSION = 1
FORMAT = 'lammps_dump'
METHOD = 'warren_cowley_first_shell'
MAX_FRAMES, MAX_ELEMENTS, MAX_ATOMS, MAX_NEIGHBORS = 8, 6, 250000, 32
MAX_TRAJECTORY_BYTES = 1024 * 1024 * 1024
WORKER_TIMEOUT_SECONDS, WORKER_OUTPUT_BYTES = 180, 60000
WORKER_BOOTSTRAP = ('import sys;sys.path.insert(0,sys.argv[1]);'
                    'from auto_lammps.coordination_analysis import _worker_main;'
                    'raise SystemExit(_worker_main())')
TABLE_FIELDS = {'file', 'format', 'length_unit', 'elements', 'expected_counts', 'pbc'}
OPERATION_FIELDS = {'id', 'method', 'file', 'neighbors', 'neighbor_selection',
                    'frames', 'aggregation', 'pair_mode'}
GUIDE = '''Structural analysis adapter:
Use a plan.tables declaration {file,format:"lammps_dump",length_unit:"angstrom",
elements:{"1":"Fe","2":"Ni"},expected_counts:{"1":<integer>,"2":<integer>},
pbc:[true,true,true]} for an actual LAMMPS custom dump containing id, type and
positions. These are trajectory source declarations, not labeled numeric tables.
Use an operation {id,method:"warren_cowley_first_shell",file,neighbors:<integer>,
neighbor_selection:"nearest_k",frames:{first:<zero-based frame>,last:<frame>,
stride:<positive integer>},aggregation:"equal_frame_mean",
pair_mode:"directed_and_symmetric"}. Freeze the exact source frame window; do not
substitute a final frame or time average when different sampling was requested.
nearest_k selects exactly the k closest unique particles using periodic geometry;
the kth and next-neighbor distances and minimum gap are retained as diagnostics,
not used to silently change k. For a physically bounded first shell, explicitly
select neighbor_selection:"bounded_shell" and additionally declare positive
max_distance and shell_gap in angstrom. Only bounded_shell enforces those bounds.
The adapter requires unique particle IDs, exact declared type counts, finite
coordinates, three-dimensional periodic cells, and unique neighbors.
It fails explicitly rather than dropping particles, types, or failed frames.
Nearest-k adjacency need not be reciprocal: this is retained as a diagnostic,
and does not prevent calculating the explicitly directed definitions and their
explicit symmetric mean. No missing reverse edge is silently removed.
Directed alpha_ij = 1 - n_ij / (k*N_i*c_j), c_j=N_j/N; n_ij counts directed
neighbor occurrences. Both directions remain visible. Symmetric alpha_ij is
explicitly (alpha_ij+alpha_ji)/2; same-type terms retain their directed value.
Means weight the selected frames equally; scatter is descriptive, not independent
replicate uncertainty. Limits: 2-6 elements, 1-32 neighbors, 1-8 selected frames per
operation, 250000 particles, 1 GiB per trajectory. Structural results include full
derived tables, chart data, source SHA, frames/timesteps, parameters and tool
versions. No paper answers or scientific pass/fail are supplied by this tool.
Sources: https://www.ovito.org/manual/python/modules/ovito_data.html#ovito.data.NearestNeighborFinder
https://www.ovito.org/manual/python/modules/ovito_io.html#ovito.io.import_file
'''


def adapter_identity():
    try:
        version = metadata.version('ovito')
    except metadata.PackageNotFoundError:
        version = None
    try:
        numpy_version = metadata.version('numpy')
    except metadata.PackageNotFoundError:
        numpy_version = None
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                ovito=dict(expected=REQUIRED['ovito'], installed=version), numpy_version=numpy_version)


def validate_table(table):
    if not isinstance(table, dict) or set(table) != TABLE_FIELDS or table.get('format') != FORMAT:
        raise AnalysisError('Declare an explicit LAMMPS dump structural source')
    if (not isinstance(table['file'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', table['file'])):
        raise AnalysisError('Structural source requires a flat declared output basename')
    if table['length_unit'] != 'angstrom' or table['pbc'] != [True, True, True] or any(
            type(value) is not bool for value in table['pbc']):
        raise AnalysisError('Warren-Cowley analysis requires angstrom and three explicit periodic boundaries')
    elements, counts = table['elements'], table['expected_counts']
    if (not isinstance(elements, dict) or not 2 <= len(elements) <= MAX_ELEMENTS
            or any(not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{0,3}', key)
                   or not isinstance(value, str) or not re.fullmatch(r'[A-Z][a-z]?', value)
                   for key, value in elements.items())
            or len(set(elements.values())) != len(elements)):
        raise AnalysisError('Declare two to six distinct elements and their exact positive particle type IDs')
    if (not isinstance(counts, dict) or set(counts) != set(elements)
            or any(type(value) is not int or value <= 0 for value in counts.values())
            or sum(counts.values()) > MAX_ATOMS):
        raise AnalysisError('Declare positive exact counts for every structural particle type')
    return table


def validate_operation(operation, table):
    validate_table(table)
    if not isinstance(operation, dict):
        raise AnalysisError('Explicit structural operation is required')
    selection = operation.get('neighbor_selection')
    fields = OPERATION_FIELDS | ({'max_distance', 'shell_gap'} if selection == 'bounded_shell' else set())
    if set(operation) != fields or operation.get('method') != METHOD or operation.get('file') != table['file']:
        raise AnalysisError('Structural operation fields or source differ from the declared Warren-Cowley method')
    _name(operation['id'])
    if selection not in {'nearest_k', 'bounded_shell'}:
        raise AnalysisError('Choose nearest_k or explicitly bounded_shell neighbor selection')
    if (type(operation['neighbors']) is not int or not 1 <= operation['neighbors'] <= MAX_NEIGHBORS
            or operation['neighbors'] >= sum(table['expected_counts'].values())):
        raise AnalysisError('Declare one to thirty-two unique neighbors, fewer than the total particles')
    if selection == 'bounded_shell':
        for key in ('max_distance', 'shell_gap'):
            value = operation[key]
            if _finite(value) <= 0:
                raise AnalysisError('Bounded-shell distance and separation must be explicit positive angstrom values')
    frames = operation['frames']
    if (not isinstance(frames, dict) or set(frames) != {'first', 'last', 'stride'}
            or any(type(value) is not int or not 0 <= value <= 2**31 - 1 for value in frames.values())
            or frames['stride'] == 0 or frames['first'] > frames['last']
            or (frames['last'] - frames['first']) % frames['stride']
            or (frames['last'] - frames['first']) // frames['stride'] + 1 > MAX_FRAMES):
        raise AnalysisError('Freeze an inclusive frame interval with one to eight exact selected frames')
    if operation['aggregation'] != 'equal_frame_mean' or operation['pair_mode'] != 'directed_and_symmetric':
        raise AnalysisError('Declare equal-frame means and retain both directed and explicitly symmetric pairs')
    return operation


def _source_digest(path, source):
    """Stream a retained regular file; never import network URLs or source copies."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != source['size']
                or info.st_size > MAX_TRAJECTORY_BYTES or info.st_size <= 0):
            raise AnalysisError('Structural source is not the declared bounded regular collected file')
        if not handle.read(32).startswith(b'ITEM: TIMESTEP\n'):
            raise AnalysisError('Structural source must be an explicit text LAMMPS dump')
        handle.seek(0)
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != source['sha256']:
            raise AnalysisError('Structural source changed after output collection')
    return digest


def _frame_statistics(data, table, operation, frame):
    import numpy as np
    from ovito.data import NearestNeighborFinder
    particles, cell = data.particles, data.cell
    count = sum(table['expected_counts'].values())
    if particles is None or particles.count != count or cell is None or tuple(cell.pbc) != (True, True, True):
        raise AnalysisError('Structural frame has wrong atom count or lacks the declared periodic cell')
    try:
        types = np.asarray(particles['Particle Type'])
        ids = np.asarray(particles['Particle Identifier'])
        positions = np.asarray(particles.positions)
        matrix = np.asarray(cell)[:, :3]
        origin = np.asarray(cell)[:, 3]
    except (KeyError, ValueError, TypeError) as exc:
        raise AnalysisError('Structural dump requires particle id, type, positions and cell') from exc
    if (types.shape != (count,) or ids.shape != (count,) or positions.shape != (count, 3)
            or not np.issubdtype(types.dtype, np.integer) or not np.issubdtype(ids.dtype, np.integer)
            or np.any(ids <= 0) or len(np.unique(ids)) != count
            or not np.isfinite(positions).all() or not np.isfinite(matrix).all()
            or not np.isfinite(origin).all() or not math.isfinite(float(np.linalg.det(matrix)))
            or float(np.linalg.det(matrix)) <= 0):
        raise AnalysisError('Structural frame has nonfinite geometry, invalid identifiers or singular cell')
    actual = {str(int(kind)): int(np.count_nonzero(types == kind)) for kind in np.unique(types)}
    if actual != table['expected_counts']:
        raise AnalysisError('Structural particle types or composition differ from the frozen declaration')
    # OVITO nearest-neighbor find() skips exactly coincident sites. Detect them
    # explicitly, including periodic images, instead of silently omitting atoms.
    reduced = np.linalg.solve(matrix, (positions - origin).T).T
    wrapped = np.mod(np.round(reduced, 12), 1.0)
    if len(np.unique(wrapped, axis=0)) != count:
        raise AnalysisError('Structural frame contains coincident periodic sites (fractional tolerance 1e-12)')
    k = operation['neighbors']
    indices, vectors = NearestNeighborFinder(k + 1, data).find_all()
    indices, vectors = np.asarray(indices), np.asarray(vectors)
    if indices.shape != (count, k + 1) or vectors.shape != (count, k + 1, 3):
        raise AnalysisError('Neighbor search did not return the declared complete neighborhoods')
    distances = np.linalg.norm(vectors, axis=2)
    if not np.isfinite(distances).all() or np.any(distances <= 0) or np.any(indices < 0) or np.any(indices >= count):
        raise AnalysisError('Neighbor search contains missing, coincident or nonfinite neighbors')
    order = np.argsort(distances, axis=1, kind='stable')
    distances = np.take_along_axis(distances, order, axis=1)
    indices = np.take_along_axis(indices, order, axis=1)
    selected = indices[:, :k]
    if any(len(set(map(int, row))) != k or center in row for center, row in enumerate(selected)):
        raise AnalysisError('First-shell neighbors contain a repeated particle or its own periodic image')
    kth, next_distance = distances[:, k - 1], distances[:, k]
    gaps = next_distance - kth
    if operation['neighbor_selection'] == 'bounded_shell' and (
            np.any(kth > operation['max_distance']) or np.any(gaps < operation['shell_gap'])):
        raise AnalysisError('A particle neighborhood violates the frozen bounded-shell distance or gap')
    # Encode both orientations as bounded integer pairs; avoid millions of
    # Python tuple objects for a large collected geometry.
    centers = np.arange(count,dtype=np.int64)[:,None]
    edges = (centers*count+selected).ravel()
    reverse = (selected*count+centers).ravel()
    missing_reverse = int(np.count_nonzero(~np.isin(reverse,edges,assume_unique=True)))
    keys = sorted(table['elements'], key=int)
    occurrences = {(i, j): 0 for i in keys for j in keys}
    for center, row in enumerate(selected):
        i = str(int(types[center]))
        for other in row:
            occurrences[i, str(int(types[other]))] += 1
    directed = []
    for i in keys:
        for j in keys:
            ni, nj = actual[i], actual[j]
            probability = occurrences[i, j] / (k * ni)
            cj = nj / count
            directed.append([int(i), int(j), occurrences[i, j], ni, nj, probability, cj, 1 - probability / cj])
    alphas = {(row[0], row[1]): row[-1] for row in directed}
    symmetric = [[int(i), int(j), (alphas[int(i), int(j)] + alphas[int(j), int(i)]) / 2]
                 for index, i in enumerate(keys) for j in keys[index:]]
    step = data.attributes.get('Timestep')
    if type(step) is not int or not 0 <= step <= 2**63 - 1:
        raise AnalysisError('Structural source frame lacks an exact nonnegative LAMMPS timestep')
    return dict(frame=frame, timestep=step, atom_count=count,
                particle_ids_sha256=sha256(canonical(sorted(map(int, ids)))),
                neighbor_diagnostics=dict(kth_distance_min=float(kth.min()), kth_distance_max=float(kth.max()),
                    next_distance_min=float(next_distance.min()), next_distance_max=float(next_distance.max()),
                    minimum_gap=float(gaps.min()), zero_gap_atoms=int(np.count_nonzero(gaps == 0)),
                    reciprocal=missing_reverse == 0, missing_reverse_edges=missing_reverse,
                    coincident_fractional_tolerance=1e-12),
                directed=directed, symmetric=symmetric)


def analyze_trajectory(path, table, operation, source):
    validate_operation(operation, table)
    identity = adapter_identity()
    if identity['ovito']['installed'] != identity['ovito']['expected']:
        raise AnalysisError('Application OVITO runtime is missing or differs from its pinned version')
    path = Path(path)
    _source_digest(path, source)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('OVITO_THREAD_COUNT', '2')
    try:
        import ovito
        from ovito.io import import_file
        pipeline = import_file(str(path), input_format='lammps/dump')
        interval = operation['frames']
        if interval['last'] >= pipeline.source.num_frames:
            raise AnalysisError('Frozen source frame interval is not present in the collected trajectory')
        frames = []
        previous_step, previous_ids = None, None
        for frame in range(interval['first'], interval['last'] + 1, interval['stride']):
            value = _frame_statistics(pipeline.compute(frame), table, operation, frame)
            if previous_step is not None and value['timestep'] <= previous_step:
                raise AnalysisError('Selected trajectory timesteps repeat or reverse; sampling is ambiguous')
            if previous_ids is not None and value['particle_ids_sha256'] != previous_ids:
                raise AnalysisError('Selected frames do not contain the same particle identities')
            previous_step, previous_ids = value['timestep'], value['particle_ids_sha256']
            frames.append(value)
    except AnalysisError:
        raise
    except (ImportError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        raise AnalysisError('OVITO could not read or analyze the declared collected trajectory') from exc
    _source_digest(path, source)
    aggregates = {}
    for kind in ('directed', 'symmetric'):
        rows = []
        for index, row in enumerate(frames[0][kind]):
            samples = [value[kind][index][-1] for value in frames]
            rows.append([row[0], row[1], statistics.fmean(samples),
                         statistics.stdev(samples) if len(samples) > 1 else None,
                         min(samples), max(samples)])
        aggregates[kind] = rows
    result = dict(id=operation['id'], method=METHOD, file=table['file'], parameters=operation,
        source=dict(file=table['file'], sha256=source['sha256'], size=source['size'], format=FORMAT,
                    length_unit=table['length_unit'], elements=table['elements'],
                    expected_counts=table['expected_counts'], pbc=table['pbc']),
        adapter_identity=identity, ovito_version=ovito.version_string,
        source_frame_count=pipeline.source.num_frames, selected_frame_count=len(frames),
        formulas=dict(directed='alpha_ij = 1 - n_ij/(k*N_i*(N_j/N))',
                      symmetric='(alpha_ij + alpha_ji)/2', aggregation='equal weight per selected frame'),
        columns=dict(directed=['type_i', 'type_j', 'directed_neighbor_count', 'N_i', 'N_j',
                               'p_j_given_i', 'c_j', 'alpha_ij'],
                     symmetric=['type_i', 'type_j', 'alpha_symmetric'],
                     aggregate=['type_i', 'type_j', 'mean', 'sample_std', 'min', 'max']),
        frames=frames, aggregates=aggregates,
        chart=dict(kind='categorical_bars', quantity='Warren-Cowley alpha', unit='1',
                   labels=[table['elements'][str(row[0])]+'–'+table['elements'][str(row[1])]
                           for row in aggregates['symmetric']],
                   values=[row[2] for row in aggregates['symmetric']]),
        scientific_status='not_evaluated', physics_simulation=False,
        limitations=['The declared nearest-k neighborhood is not automatically proven to be a physical first shell.',
                     'Frame scatter is descriptive; correlated frames are not independent replicates.',
                     'Nonreciprocal nearest-k edges are retained; the symmetric mean does not assert a reciprocal physical shell.',
                     'No paper reference answers, scientific acceptance thresholds or automatic sampling windows are used.'])
    if len(canonical(result)) > 55000:
        raise AnalysisError('Derived structural table exceeds the accounted compact report limit')
    result['derived_sha256'] = sha256(canonical(result))
    return result


def analyze_isolated(path, table, operation, source):
    """Trusted fixed-code worker; native failures cannot kill the application.

    Reuses the existing bounded stream/process capture, including timeout and
    descendant cleanup. No generated Python, shell, network retrieval or files.
    """
    from .slurm_read import _capture
    validate_operation(operation,table)
    source=dict(size=source['size'],sha256=source['sha256'])
    _source_digest(Path(path),source)
    request=dict(path=str(Path(path).absolute()),table=table,operation=operation,
                 source=source,adapter_identity=adapter_identity())
    encoded=canonical(request)
    if len(encoded)>65536:
        raise AnalysisError('Structural worker request exceeds its bounded input')
    argv=[sys.executable,'-I','-X','faulthandler','-c',WORKER_BOOTSTRAP,
          str(Path(__file__).resolve().parent.parent)]
    captured=_capture(argv,timeout=WORKER_TIMEOUT_SECONDS,max_bytes=WORKER_OUTPUT_BYTES,input_chunks=[encoded])
    if captured['failure'] or captured['returncode'] != 0:
        reason={'timeout':'Structural tool exceeded its fixed execution timeout',
                'output_limit':'Structural tool exceeded its bounded report output',
                'command_failed':'Structural tool process failed; no partial statistics were accepted'}
        raise AnalysisError(reason.get(captured['failure'],'Structural tool execution failed'))
    try:
        raw=base64.b64decode(captured['stdout'],validate=True)
        value=json.loads(raw)
        if value['status']=='analysis_failed':
            raise AnalysisError(value['reason'])
        result=value['result']
        proof=result.get('derived_sha256')
        content={key:item for key,item in result.items() if key!='derived_sha256'}
        if (value['status']!='analyzed' or proof!=sha256(canonical(content))
                or result['adapter_identity']!=request['adapter_identity']
                or result['id']!=operation['id'] or result['parameters']!=operation
                or result['source']['sha256']!=source['sha256'] or result['source']['size']!=source['size']
                or result['scientific_status']!='not_evaluated' or result['physics_simulation'] is not False):
            raise AnalysisError('Structural worker returned an unbound or invalid derived result')
    except AnalysisError:
        raise
    except (ValueError,KeyError,TypeError) as exc:
        raise AnalysisError('Structural worker returned an invalid bounded result') from exc
    _source_digest(Path(path),source)
    return result


def _worker_main():
    """Entry callable only from the application fixed bootstrap."""
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:
            raise AnalysisError('Structural worker input exceeds the bounded request')
        request=json.loads(raw)
        if (not isinstance(request,dict)
                or set(request)!={'path','table','operation','source','adapter_identity'}
                or not isinstance(request['path'],str) or len(request['path'])>4096
                or not Path(request['path']).is_absolute()
                or request['adapter_identity']!=adapter_identity()):
            raise AnalysisError('Structural worker source or runtime differs from the frozen request')
        result=analyze_trajectory(Path(request['path']),request['table'],request['operation'],request['source'])
        result.pop('derived_sha256')
        result['worker_receipt']=dict(mode='fixed_source_subprocess',timeout_seconds=WORKER_TIMEOUT_SECONDS,
                                     output_limit_bytes=WORKER_OUTPUT_BYTES)
        result['derived_sha256']=sha256(canonical(result))
        value=dict(status='analyzed',result=result)
    except (ValueError,KeyError,TypeError,OSError,ImportError) as exc:
        value=dict(status='analysis_failed',reason=str(exc) if isinstance(exc,AnalysisError)
                   else 'Structural worker could not verify or analyze the declared collected source')
    encoded=canonical(value)
    if len(encoded)>WORKER_OUTPUT_BYTES:
        encoded=canonical(dict(status='analysis_failed',reason='Derived structural result exceeds the accounted worker limit'))
    sys.stdout.buffer.write(encoded)
    return 0
