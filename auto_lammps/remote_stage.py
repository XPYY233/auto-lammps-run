"""Standalone trusted staging helper; no engine, shell, archive or scheduler execution.

Install this exact reviewed file outside the writable request root. The SSH client
pins its SHA-256. Root policy and filesystem ownership are administrator managed.
Wire format: 4-byte network-order manifest length, manifest JSON, listed local file
bytes. Schema 2 external structures come only from the fixed trusted HPC catalog.
"""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import struct
import sys

HEADER_LIMIT = 1_000_000
RECEIPT_ALLOWANCE = 8192
ROOT_ALLOWANCE = 65536
ROLES = {'lammps_input', 'structure', 'potential', 'analysis_spec'}
GEOMETRY_SUMMARY_KEYS = {'schema_version', 'parser', 'parser_version', 'parser_sha256',
    'data_sha256', 'size', 'atom_style', 'units', 'boundary', 'type_elements',
    'atom_count', 'type_count', 'type_counts', 'composition', 'cell_angstrom',
    'origin_angstrom', 'tilt_angstrom', 'masses_amu', 'mass_source',
    'coordinate_content_sha256', 'particle_id_order_sha256', 'id_policy',
    'atom_record_columns', 'original_bytes_modified', 'elements_inferred',
    'physical_evaluation_performed', 'scientifically_verified', 'image_flag_range',
    'periodic_remapping_may_occur', 'fixed_boundary_geometry_verified'}
GEOMETRY_ELEMENTS = frozenset(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn '
    'Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd '
    'In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu '
    'Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu '
    'Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split())
RESERVED = {'manifest.json', 'allocation.json', 'stage.json', 'receipt.json', 'job.sh', 'output',
            'execution-intent.json', 'execution-result.json', 'scheduler.stdout', 'scheduler.stderr',
            'scheduler-intent.json', 'scheduler-result.json', 'output-volume.ext2'}


class StageError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise StageError('Invalid content digest')
    return value


def safe_name(value):
    if (not isinstance(value, str) or not value or len(value) > 240
            or len(value.split('/')) > 8
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', x) for x in value.split('/'))
            or value.split('/')[0] in RESERVED or str(PurePosixPath(value)) != value):
        raise StageError('Unsafe input path')
    return value


def read_exact(stream, size):
    parts, remaining = [], size
    while remaining:
        block = stream.read(min(remaining, 65536))
        if not block:
            raise StageError('Truncated upload')
        parts.append(block)
        remaining -= len(block)
    return b''.join(parts)


def regular_read(directory, name, limit, *, private=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or (private and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            raise StageError('Invalid control file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        if (len(data) > limit or len(data) != info.st_size
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) !=
                   (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise StageError('Control file changed during read')
        return data
    finally:
        os.close(fd)


def write_new(directory, name, data):
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)


@contextmanager
def directory_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def approved_root(path):
    if not isinstance(path, str) or not path.startswith('/') or str(PurePosixPath(path)) != path:
        raise StageError('An absolute canonical staging root is required')
    parts = path.split('/')[1:]
    if not parts or any(part in {'', '.', '..'} for part in parts):
        raise StageError('Unsafe root')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            access = os.O_RDONLY if index == len(parts)-1 else getattr(os, 'O_PATH', os.O_RDONLY)
            child = os.open(part, access | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            try:
                os.stat('.git', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StageError('Staging root must be outside Git working trees')
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StageError('Staging root must be owned by service identity and private')
        yield fd
    finally:
        os.close(fd)


def validate_manifest(data, expected):
    if digest(data) != hash_value(expected):
        raise StageError('Manifest digest mismatch')
    try:
        value = json.loads(data)
        if (set(value) != {'schema_version', 'files', 'entrypoint', 'resources', 'provenance'}
                or type(value['schema_version']) is not int or value['schema_version'] not in {1, 2}):
            raise StageError('Unsupported manifest')
        resources = value['resources']
        if set(resources) != {'cores', 'wall_seconds', 'memory_bytes', 'storage_bytes'}:
            raise StageError('Invalid resources')
        if any(type(x) is not int or x <= 0 for x in resources.values()):
            raise StageError('Invalid resource bounds')
        if set(value['provenance']) != {'task_sha256', 'analysis_sha256', 'software_sha256'}:
            raise StageError('Missing provenance')
        for item in value['provenance'].values():
            hash_value(item)
        files = value['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 128:
            raise StageError('Invalid file count')
        names, entries = set(), []
        total = len(data) + RECEIPT_ALLOWANCE
        for item in files:
            external = isinstance(item, dict) and 'external_source' in item
            if (not isinstance(item, dict)
                    or set(item) != {'path', 'role', 'size', 'sha256'} | ({'external_source'} if external else set())):
                raise StageError('Invalid file record')
            name = safe_name(item['path'])
            if name in names or any(name.startswith(old + '/') or old.startswith(name + '/') for old in names):
                raise StageError('Duplicate or overlapping path')
            names.add(name)
            if not isinstance(item['role'], str) or item['role'] not in ROLES:
                raise StageError('Invalid input role')
            if type(item['size']) is not int or item['size'] < 0:
                raise StageError('Invalid file size')
            total += item['size']
            hash_value(item['sha256'])
            if external:
                source = item['external_source']
                potential = isinstance(source, dict) and source.get('kind') == 'potential'
                if (value['schema_version'] != 2 or not isinstance(source, dict)
                        or item['role'] != ('potential' if potential else 'structure')
                        or set(source) != ({'kind', 'catalog_sha256', 'pin', 'role'} if potential else {'catalog_sha256', 'pin'})
                        or (potential and source['role'] not in {'library', 'parameters', 'coefficients', 'model', 'license'})):
                    raise StageError('Invalid external resource source')
                for key in ('catalog_sha256', 'pin'):
                    hash_value(source[key])
            if item['role'] == 'lammps_input':
                entries.append(name)
        if entries != [value['entrypoint']] or total > resources['storage_bytes']:
            raise StageError('Invalid entrypoint or insufficient storage including receipts')
        return value
    except (TypeError, KeyError, AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageError('Malformed manifest') from exc


def finite_number(value):
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def validate_geometry_summary(summary):
    """Standalone counterpart of the registered atomic metadata contract.

    No free-text or coordinate field is accepted, including inside numeric arrays.
    The controller additionally applies the task's approved atom-count policy.
    """
    if not isinstance(summary, dict) or set(summary) != GEOMETRY_SUMMARY_KEYS:
        raise StageError('Unsupported geometry metadata')
    constants = dict(schema_version=1, parser='lammps_atomic_data', parser_version=1,
        atom_style='atomic', id_policy='continuous_1_to_N_preserve_input_order', image_flag_range=[-512,511],
        original_bytes_modified=False, elements_inferred=False, physical_evaluation_performed=False,
        scientifically_verified=False, fixed_boundary_geometry_verified=True)
    if any(type(summary[name]) is not type(value) or summary[name] != value for name,value in constants.items()):
        raise StageError('Geometry summary changes the fixed input contract')
    for name in ('parser_sha256', 'data_sha256', 'coordinate_content_sha256', 'particle_id_order_sha256'):
        hash_value(summary[name])
    if (summary['units'] not in ('metal','real') or type(summary['atom_record_columns']) is not int
            or summary['atom_record_columns'] not in (5,8) or type(summary['periodic_remapping_may_occur']) is not bool
            or type(summary['size']) is not int or not 1 <= summary['size'] <= 64*1024*1024
            or type(summary['atom_count']) is not int or not 1 <= summary['atom_count'] <= 1000000):
        raise StageError('Invalid geometry units, size or count')
    boundary, elements = summary['boundary'], summary['type_elements']
    if (not isinstance(boundary, list) or len(boundary) != 3 or any(item not in ('p','f') for item in boundary)
            or not isinstance(elements, list) or not 1 <= len(elements) <= 118
            or any(not isinstance(item, str) or item not in GEOMETRY_ELEMENTS for item in elements)
            or len(set(elements)) != len(elements) or type(summary['type_count']) is not int
            or summary['type_count'] != len(elements)):
        raise StageError('Invalid geometry boundary or element mapping')
    counts = summary['type_counts']
    if (not isinstance(counts, dict) or set(counts) != {str(i+1) for i in range(len(elements))}
            or any(type(item) is not int or item < 0 for item in counts.values())
            or sum(counts.values()) != summary['atom_count']):
        raise StageError('Geometry type counts do not match atoms')
    composition = {element:counts[str(i+1)] for i,element in enumerate(elements) if counts[str(i+1)]}
    if (not isinstance(summary['composition'], dict) or summary['composition'] != composition
            or any(type(item) is not int for item in summary['composition'].values())):
        raise StageError('Geometry element composition does not match type counts')
    cell, origin, tilt = summary['cell_angstrom'], summary['origin_angstrom'], summary['tilt_angstrom']
    if (not isinstance(cell, list) or len(cell) != 3
            or any(not isinstance(row, list) or len(row) != 3 or not all(finite_number(item) for item in row) for row in cell)
            or not isinstance(origin, list) or len(origin) != 3 or not all(finite_number(item) for item in origin)
            or not isinstance(tilt, list) or len(tilt) != 3 or not all(finite_number(item) for item in tilt)
            or any(cell[i][i] <= 0 for i in range(3))
            or any(cell[i][j] != 0 for i,j in ((0,1),(0,2),(1,2)))
            or tilt != [cell[1][0],cell[2][0],cell[2][1]]):
        raise StageError('Invalid geometry cell metadata')
    volume = math.prod(float(cell[i][i]) for i in range(3)); norms = [math.hypot(*row) for row in cell]
    if (not math.isfinite(volume) or volume <= 1e-12 or not all(math.isfinite(item) for item in norms)
            or math.prod(cell[i][i]/norms[i] for i in range(3)) <= 1e-10):
        raise StageError('Numerically degenerate geometry cell')
    masses = summary['masses_amu']
    if masses is None:
        if summary['mass_source'] != 'not_in_file':
            raise StageError('Absent geometry masses must be explicitly reported')
    elif (not isinstance(masses, list) or len(masses) != len(elements)
            or any(not finite_number(item) or item <= 0 for item in masses) or summary['mass_source'] != 'file'):
        raise StageError('Invalid geometry masses metadata')
    return summary


def validate_geometry_catalog(raw):
    """Fixed metadata only. Geometry parsing/registration occurs before this step."""
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'entries'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not isinstance(value['entries'], list) or len(value['entries']) > 128):
            raise StageError('Invalid geometry catalog')
        pins, paths = set(), set()
        for item in value['entries']:
            if not isinstance(item, dict) or set(item) != {'pin', 'path', 'size', 'sha256', 'summary'}:
                raise StageError('Invalid geometry catalog entry')
            path = safe_name(item['path'])
            if (path.split('/')[0] in {'catalog.json', 'versions'} or path in paths
                    or any(path.startswith(old + '/') or old.startswith(path + '/') for old in paths)
                    or type(item['size']) is not int or item['size'] < 0):
                raise StageError('Invalid geometry source identity')
            paths.add(path)
            hash_value(item['sha256']); hash_value(item['pin'])
            summary = validate_geometry_summary(item['summary'])
            if summary['size'] != item['size'] or summary['data_sha256'] != item['sha256']:
                raise StageError('Geometry bytes differ from fixed metadata')
            pin = digest(encoded({name: item[name] for name in ('size', 'sha256', 'summary')}))
            if item['pin'] != pin or pin in pins:
                raise StageError('Geometry pin differs from fixed metadata')
            pins.add(pin)
        return value
    except (TypeError, KeyError, AttributeError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, StageError):
            raise
        raise StageError('Malformed geometry catalog') from exc


def catalog_version_bytes(directory, checksum):
    hash_value(checksum)
    try:
        with directory_at(directory, 'versions') as versions:
            info = os.fstat(versions)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise StageError('Geometry versions directory is not owner-private')
            raw = regular_read(versions, checksum+'.json', HEADER_LIMIT, private=True)
            if digest(raw) != checksum:
                raise StageError('Fixed geometry catalog version changed')
            return raw
    except FileNotFoundError as exc:
        raise StageError('Fixed geometry catalog version is unavailable') from exc


def geometry_catalog_path(_geometry_catalog):
    return Path(_geometry_catalog) if _geometry_catalog is not None else Path(__file__).resolve().parent/'geometry-catalog'


@contextmanager
def geometry_catalog(*, _geometry_catalog=None, _catalog_sha256=None):
    # Internal injection supports inert file tests; no CLI/model source selection.
    path = geometry_catalog_path(_geometry_catalog)
    with approved_root(str(path)) as directory:
        if _catalog_sha256 is None:
            raw = regular_read(directory, 'catalog.json', HEADER_LIMIT, private=True)
            checksum = digest(raw)
            if catalog_version_bytes(directory, checksum) != raw:
                raise StageError('Current geometry catalog differs from its fixed version')
        else:
            checksum = hash_value(_catalog_sha256)
            raw = catalog_version_bytes(directory, checksum)
        yield directory, checksum, validate_geometry_catalog(raw)


def inspect_geometry_catalog(*, _geometry_catalog=None):
    """Read fixed allowed metadata only; do not export source paths or byte content."""
    with geometry_catalog(_geometry_catalog=_geometry_catalog) as (_, checksum, catalog):
        return dict(schema_version=1, catalog_sha256=checksum,
                    entries=[{name: item[name] for name in ('pin', 'size', 'sha256', 'summary')}
                             for item in catalog['entries']])


def validate_potential_catalog(raw):
    """Registered original files only; no scientific compatibility inference."""
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'entries'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['entries'], list) or len(value['entries']) > 128):
        raise StageError('Invalid HPC potential catalog')
    pins, paths = set(), set()
    roles = {'meam': {'library', 'parameters', 'license'},
             'eam/alloy': {'model', 'license'}, 'snap': {'coefficients', 'parameters', 'license'}}
    for entry in value['entries']:
        if not isinstance(entry, dict) or set(entry) != {'pin', 'record', 'paths'}:
            raise StageError('Invalid potential entry')
        pin = hash_value(entry['pin']); record = entry['record']
        if (pin in pins or not isinstance(record, dict)
                or set(record) != {'schema_version', 'status', 'metadata', 'files', 'inspection'}
                or type(record['schema_version']) is not int or record['schema_version'] != 1
                or not isinstance(record['metadata'], dict) or not isinstance(record['files'], dict)
                or record['status'] != 'collected'
                or digest(encoded(record)) != pin):
            raise StageError('Registered potential record changed')
        pins.add(pin)
        fmt = record['metadata'].get('format')
        if (not isinstance(fmt, str) or fmt not in roles or not isinstance(entry['paths'], dict)
                or set(record['files']) != roles[fmt] or set(entry['paths']) != roles[fmt]):
            raise StageError('Incomplete potential and license bundle')
        for role, item in record['files'].items():
            if (not isinstance(item, dict) or set(item) != {'name', 'size', 'sha256'}
                    or type(item['size']) is not int or not 1 <= item['size'] <= 16 * 1024 * 1024):
                raise StageError('Invalid potential source file')
            hash_value(item['sha256'])
            if (not isinstance(item['name'], str) or '/' in item['name']
                    or entry['paths'][role] != pin + '/' + item['name']):
                raise StageError('Potential source path differs from its registered role')
            path = safe_name(entry['paths'][role])
            if path in paths:
                raise StageError('Overlapping potential source')
            paths.add(path)
    return value


def potential_catalog_path(_potential_catalog):
    return Path(_potential_catalog) if _potential_catalog is not None else Path(__file__).resolve().parent/'potential-catalog'


@contextmanager
def potential_catalog(*, _potential_catalog=None, _catalog_sha256=None):
    path = potential_catalog_path(_potential_catalog)
    with approved_root(str(path)) as directory:
        if _catalog_sha256 is None:
            raw = regular_read(directory, 'catalog.json', HEADER_LIMIT, private=True)
            checksum = digest(raw)
            if catalog_version_bytes(directory, checksum) != raw:
                raise StageError('Current potential catalog differs from its fixed version')
        else:
            checksum = hash_value(_catalog_sha256)
            raw = catalog_version_bytes(directory, checksum)
        yield directory, checksum, validate_potential_catalog(raw)


def inspect_potential_catalog(*, _potential_catalog=None):
    with potential_catalog(_potential_catalog=_potential_catalog) as (_, checksum, catalog):
        return dict(schema_version=1, catalog_sha256=checksum,
            entries=[{key: item[key] for key in ('pin', 'record')} for item in catalog['entries']])


def copy_geometry_source(catalog_directory, item, output_fd):
    folder = os.dup(catalog_directory)
    try:
        parts = safe_name(item['path']).split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder); folder = child
            info = os.fstat(folder)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise StageError('Geometry source directory is not owner-private')
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            before = os.fstat(source_fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size != item['size']):
                raise StageError('Invalid geometry source file')
            with os.fdopen(source_fd, 'rb', closefd=False) as source, os.fdopen(output_fd, 'wb', closefd=False) as output:
                remaining, checksum = item['size'], hashlib.sha256()
                while remaining:
                    block = source.read(min(remaining, 65536))
                    if not block:
                        raise StageError('Geometry source truncated during copy')
                    checksum.update(block); output.write(block); remaining -= len(block)
                after = os.fstat(source_fd)
                if (source.read(1) or checksum.hexdigest() != item['sha256']
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                           (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise StageError('Geometry source changed during copy')
                output.flush(); os.fsync(output_fd)
        finally:
            os.close(source_fd)
    finally:
        os.close(folder)


def receive(root_path, request_id, manifest_sha256, stream, *, _geometry_catalog=None, _potential_catalog=None):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise StageError('Invalid request identity')
    size = struct.unpack('!I', read_exact(stream, 4))[0]
    if not 0 < size <= HEADER_LIMIT:
        raise StageError('Manifest size exceeds limit')
    manifest_bytes = read_exact(stream, size)
    manifest = validate_manifest(manifest_bytes, manifest_sha256)
    allocation = manifest['resources']['storage_bytes']
    with approved_root(root_path) as root, ExitStack() as catalogs:
        external_files = [item for item in manifest['files'] if 'external_source' in item and item['role']=='structure']
        external_potentials = [item for item in manifest['files'] if 'external_source' in item and item['role']=='potential']
        catalog_directory, catalog_sha256, selected = None, None, {}
        potential_directory, potential_sha256, potential_selected = None, None, {}
        if external_files:
            source_root, request_root = geometry_catalog_path(_geometry_catalog), Path(root_path)
            if source_root == request_root or source_root in request_root.parents or request_root in source_root.parents:
                raise StageError('Geometry catalog must be outside the request root')
            pinned = external_files[0]['external_source']['catalog_sha256']
            catalog_directory, catalog_sha256, catalog = catalogs.enter_context(geometry_catalog(
                _geometry_catalog=_geometry_catalog, _catalog_sha256=pinned))
            inventory = {item['pin']: item for item in catalog['entries']}
            for item in external_files:
                source = item['external_source']; entry = inventory.get(source['pin'])
                if (source['catalog_sha256'] != catalog_sha256 or entry is None
                        or entry['size'] != item['size'] or entry['sha256'] != item['sha256']):
                    raise StageError('External structure differs from the fixed HPC catalog')
                selected[item['path']] = entry
        if external_potentials:
            source_root, request_root = potential_catalog_path(_potential_catalog), Path(root_path)
            if source_root == request_root or source_root in request_root.parents or request_root in source_root.parents:
                raise StageError('Potential catalog must be outside the request root')
            pinned = external_potentials[0]['external_source']['catalog_sha256']
            potential_directory, potential_sha256, catalog = catalogs.enter_context(potential_catalog(
                _potential_catalog=_potential_catalog, _catalog_sha256=pinned))
            inventory = {item['pin']: item for item in catalog['entries']}
            names = dict(library='library.meam', coefficients='model.snapcoeff', parameters=None,
                         model='model.eam.alloy', license='LICENSE.txt')
            requested = {}
            for item in external_potentials:
                source = item['external_source']; entry = inventory.get(source['pin'])
                if source['catalog_sha256'] != potential_sha256 or entry is None:
                    raise StageError('External potential differs from the fixed HPC catalog')
                role = source['role']; rec = entry['record']['files'].get(role)
                if rec is None or rec['size'] != item['size'] or rec['sha256'] != item['sha256']:
                    raise StageError('External potential file differs from its registered role')
                name = names[role] if role != 'parameters' else ('model.meam' if entry['record']['metadata']['format']=='meam' else 'model.snapparam')
                if item['path'] != 'potentials/'+source['pin']+'/'+name:
                    raise StageError('External potential destination is not its fixed binding name')
                requested.setdefault(source['pin'], set()).add(role)
                potential_selected[item['path']] = dict(path=entry['paths'][role], size=rec['size'], sha256=rec['sha256'])
            for pin, role_set in requested.items():
                if role_set != set(inventory[pin]['record']['files']):
                    raise StageError('All potential roles and license must be staged together')
        # Exclusive creation avoids racing O_CREAT/O_NOFOLLOW path resolution on
        # some filesystems. Existing locks are opened without creation semantics.
        try:
            lock = os.open('.stage.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root)
            os.fsync(root)
        except FileExistsError:
            lock = os.open('.stage.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise StageError('Unsafe staging lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            policy_bytes = regular_read(root, 'policy.json', 4096)
            policy = json.loads(policy_bytes)
            if (set(policy) != {'schema_version', 'max_total_bytes', 'approval_sha256'} or policy['schema_version'] != 1
                    or type(policy['max_total_bytes']) is not int or policy['max_total_bytes'] <= ROOT_ALLOWANCE):
                raise StageError('Invalid private storage policy')
            hash_value(policy['approval_sha256'])
            used = ROOT_ALLOWANCE
            for name in os.listdir(root):
                if name in {'.stage.lock', 'policy.json'}:
                    continue
                if not re.fullmatch(r'[a-f0-9]{32}', name):
                    raise StageError('Unexpected object in service root')
                if name == request_id:
                    raise StageError('Request directory already exists; inspect rather than overwrite')
                with directory_at(root, name) as old:
                    saved = json.loads(regular_read(old, 'allocation.json', 4096))
                    charge = saved['storage_bytes']
                    if type(charge) is not int or charge <= 0 or saved['request_id'] != name:
                        raise StageError('Invalid previous allocation; reconciliation required')
                    used += charge
            if used + allocation > policy['max_total_bytes']:
                raise StageError('Remote aggregate storage reservation exceeded')
            os.mkdir(request_id, mode=0o700, dir_fd=root)
            os.fsync(root)
            with directory_at(root, request_id) as target:
                # A crash before allocation is durable leaves a directory that
                # fails closed during subsequent aggregate accounting.
                write_new(target, 'allocation.json', encoded(dict(request_id=request_id,
                    storage_bytes=allocation, manifest_sha256=manifest_sha256)))
                write_new(target, 'manifest.json', manifest_bytes)
                for item in manifest['files']:
                    folder = os.dup(target)
                    try:
                        parts = item['path'].split('/')
                        for part in parts[:-1]:
                            try:
                                os.mkdir(part, mode=0o700, dir_fd=folder)
                                os.fsync(folder)
                            except FileExistsError:
                                pass
                            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                            os.close(folder)
                            folder = child
                        fd = os.open(parts[-1], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                     0o400, dir_fd=folder)
                        try:
                            if 'external_source' in item:
                                if item['role']=='potential':
                                    copy_geometry_source(potential_directory, potential_selected[item['path']], fd)
                                else:
                                    copy_geometry_source(catalog_directory, selected[item['path']], fd)
                            else:
                                remaining, checksum = item['size'], hashlib.sha256()
                                with os.fdopen(fd, 'wb', closefd=False) as output:
                                    while remaining:
                                        block = read_exact(stream, min(remaining, 65536))
                                        output.write(block)
                                        checksum.update(block)
                                        remaining -= len(block)
                                    output.flush()
                                    os.fsync(fd)
                                if checksum.hexdigest() != item['sha256']:
                                    raise StageError('Uploaded file digest mismatch')
                        finally:
                            os.close(fd)
                        os.fsync(folder)
                    finally:
                        os.close(folder)
                if stream.read(1):
                    raise StageError('Trailing undeclared upload content')
                if catalog_directory is not None:
                    catalog_version_bytes(catalog_directory, catalog_sha256)
                if potential_directory is not None:
                    catalog_version_bytes(potential_directory, potential_sha256)
                receipt = dict(schema_version=1, state='staged', request_id=request_id,
                               manifest_sha256=manifest_sha256, input_bytes=sum(x['size'] for x in manifest['files']),
                               storage_bytes=allocation, policy_sha256=digest(policy_bytes))
                write_new(target, 'stage.json', encoded(receipt))
                return receipt
        finally:
            os.close(lock)


def main():
    parser = argparse.ArgumentParser(description='Receive declared regular files only; never execute them.')
    parser.add_argument('--root')
    parser.add_argument('--request-id')
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--list-geometry', action='store_true', help='Read fixed geometry metadata only; never stage or execute')
    parser.add_argument('--list-potentials', action='store_true', help='Read fixed potential metadata only; never stage or execute')
    args = parser.parse_args()
    if args.list_geometry or args.list_potentials:
        if args.list_geometry and args.list_potentials:
            parser.error('Choose one fixed metadata inventory')
        if any(value is not None for value in (args.root, args.request_id, args.manifest_sha256)):
            parser.error('Metadata inspection cannot be combined with request arguments')
    elif any(value is None for value in (args.root, args.request_id, args.manifest_sha256)):
        parser.error('--root, --request-id and --manifest-sha256 are required for staging')
    # A stalled client cannot hold the service lock indefinitely. Termination
    # leaves partial files/reservations for inspection; never implies rejection.
    signal.alarm(60)
    try:
        result = (inspect_geometry_catalog() if args.list_geometry else inspect_potential_catalog() if args.list_potentials else
                  receive(args.root, args.request_id, args.manifest_sha256, sys.stdin.buffer))
    except (StageError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'state': 'stage_failed', 'error_type': type(exc).__name__}))
        return 1
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
