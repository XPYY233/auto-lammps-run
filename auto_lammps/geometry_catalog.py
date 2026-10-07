"""Fixed HPC geometry inputs and small, verified controller-side metadata.

Registration belongs to the trusted HPC resource service. It never executes an
engine, reads author workflows, or downloads simulation bytes to the controller.
The ordinary application selects an immutable pin, not a client-supplied path.
"""
import base64
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import uuid

from . import atomic_structure_data as atomic
from .manifest import canonical, private_directory, read_file, root_descriptor, sha256
from .slurm_read import _capture, _write_new
from .staging import BOOTSTRAP

CATALOG_LIMIT = 1_000_000
ENTRY_LIMIT = 128
H = re.compile(r'[a-f0-9]{64}\Z')
SUMMARY_FIELDS = {
    'schema_version', 'parser', 'parser_version', 'parser_sha256', 'data_sha256',
    'size', 'atom_style', 'units', 'boundary', 'type_elements', 'atom_count',
    'type_count', 'type_counts', 'composition', 'cell_angstrom', 'origin_angstrom',
    'tilt_angstrom', 'masses_amu', 'mass_source', 'coordinate_content_sha256',
    'particle_id_order_sha256', 'id_policy', 'atom_record_columns',
    'image_flag_range', 'periodic_remapping_may_occur', 'fixed_boundary_geometry_verified',
    'original_bytes_modified', 'elements_inferred', 'physical_evaluation_performed',
    'scientifically_verified',
}


class GeometryCatalogError(ValueError):
    pass


def _hash(value):
    if not isinstance(value, str) or not H.fullmatch(value):
        raise GeometryCatalogError('Invalid geometry digest')
    return value


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_summary(value, *, max_atoms=100000):
    if not isinstance(value, dict) or set(value) != SUMMARY_FIELDS:
        raise GeometryCatalogError('Unsupported geometry metadata; coordinates and free text are not permitted')
    constants = dict(schema_version=1, parser='lammps_atomic_data', parser_version=1,
                     atom_style='atomic', id_policy='continuous_1_to_N_preserve_input_order',
                     image_flag_range=[-512, 511], original_bytes_modified=False,
                     elements_inferred=False, physical_evaluation_performed=False,
                     scientifically_verified=False, fixed_boundary_geometry_verified=True)
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in constants.items()):
        raise GeometryCatalogError('Geometry metadata changes the verified input contract')
    for key in ('parser_sha256', 'data_sha256', 'coordinate_content_sha256', 'particle_id_order_sha256'):
        _hash(value[key])
    if (value['units'] not in ('metal', 'real') or value['atom_record_columns'] not in (5, 8)
            or type(value['atom_record_columns']) is not int
            or type(value['periodic_remapping_may_occur']) is not bool
            or type(value['size']) is not int or not 1 <= value['size'] <= atomic.DEFAULT_MAX_BYTES
            or type(max_atoms) is not int or not 1 <= max_atoms <= atomic.MAX_ATOMS
            or type(value['atom_count']) is not int or not 1 <= value['atom_count'] <= max_atoms):
        raise GeometryCatalogError('Geometry units, size or count are unsupported')
    boundary = value['boundary']
    elements = value['type_elements']
    if (not isinstance(boundary, list) or len(boundary) != 3 or any(x not in ('p', 'f') for x in boundary)
            or not isinstance(elements, list) or not 1 <= len(elements) <= 118
            or any(not isinstance(x, str) or x not in atomic.ELEMENTS for x in elements)
            or len(set(elements)) != len(elements)
            or type(value['type_count']) is not int or value['type_count'] != len(elements)):
        raise GeometryCatalogError('Explicit boundary and ordered element mapping are required')
    counts = value['type_counts']
    if (not isinstance(counts, dict) or set(counts) != {str(i+1) for i in range(len(elements))}
            or any(type(n) is not int or n < 0 for n in counts.values())
            or sum(counts.values()) != value['atom_count']):
        raise GeometryCatalogError('Geometry composition counts do not match atoms')
    composition = {el: counts[str(i+1)] for i, el in enumerate(elements) if counts[str(i+1)]}
    if (not isinstance(value['composition'], dict)
            or any(type(n) is not int or n <= 0 for n in value['composition'].values())
            or value['composition'] != composition):
        raise GeometryCatalogError('Element composition differs from the explicit type mapping')
    cell = value['cell_angstrom']
    origin = value['origin_angstrom']
    tilt = value['tilt_angstrom']
    if (not isinstance(cell, list) or len(cell) != 3
            or any(not isinstance(row, list) or len(row) != 3 or not all(_finite(x) for x in row) for row in cell)
            or not isinstance(origin, list) or len(origin) != 3 or not all(_finite(x) for x in origin)
            or not isinstance(tilt, list) or len(tilt) != 3 or not all(_finite(x) for x in tilt)
            or any(cell[i][i] <= 0 for i in range(3))
            or any(cell[i][j] != 0 for i, j in ((0, 1), (0, 2), (1, 2)))
            or tilt != [cell[1][0], cell[2][0], cell[2][1]]):
        raise GeometryCatalogError('Invalid explicit geometry cell')
    volume = math.prod(float(cell[i][i]) for i in range(3))
    norms = [math.hypot(*row) for row in cell]
    if (not math.isfinite(volume) or volume <= 1e-12 or not all(math.isfinite(n) for n in norms)
            or math.prod(cell[i][i]/norms[i] for i in range(3)) <= 1e-10):
        raise GeometryCatalogError('Numerically degenerate geometry cell')
    masses = value['masses_amu']
    if masses is None:
        if value['mass_source'] != 'not_in_file':
            raise GeometryCatalogError('Absent masses must be explicitly reported')
    elif (not isinstance(masses, list) or len(masses) != len(elements)
          or any(not _finite(x) or x <= 0 for x in masses) or value['mass_source'] != 'file'):
        raise GeometryCatalogError('Invalid explicit data-file masses')
    return value


def validate_entry(entry, *, max_atoms=100000):
    if not isinstance(entry, dict) or set(entry) != {'pin', 'size', 'sha256', 'summary'}:
        raise GeometryCatalogError('Only immutable geometry metadata may enter the application')
    _hash(entry['pin']); _hash(entry['sha256'])
    summary = validate_summary(entry['summary'], max_atoms=max_atoms)
    if (entry['size'] != summary['size'] or type(entry['size']) is not int
            or entry['sha256'] != summary['data_sha256']
            or entry['pin'] != sha256(canonical({k: entry[k] for k in ('size', 'sha256', 'summary')}))):
        raise GeometryCatalogError('Geometry pin, bytes and metadata do not agree')
    return entry


def validate_view(view, *, max_atoms=100000):
    if (not isinstance(view, dict) or set(view) != {'schema_version', 'catalog_sha256', 'entries'}
            or type(view['schema_version']) is not int or view['schema_version'] != 1
            or not isinstance(view['entries'], list) or len(view['entries']) > ENTRY_LIMIT):
        raise GeometryCatalogError('Invalid fixed geometry catalog view')
    _hash(view['catalog_sha256'])
    seen = set()
    for entry in view['entries']:
        validate_entry(entry, max_atoms=max_atoms)
        if entry['pin'] in seen:
            raise GeometryCatalogError('Repeated geometry pin')
        seen.add(entry['pin'])
    return view


@contextmanager
def _locked(directory):
    with root_descriptor(directory) as root:
        try:
            fd = os.open('.register.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root)
        except FileExistsError:
            fd = os.open('.register.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise GeometryCatalogError('Unsafe geometry catalog lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield root
        finally:
            os.close(fd)


def register_atomic_geometry(source_root, source_name, directory, *, units, boundary, type_elements,
                             max_atoms=100000, max_bytes=atomic.DEFAULT_MAX_BYTES):
    """HPC-admin resource operation, never a model/browser arbitrary-file API.

    Keep original simulation bytes on configured HPC. Each catalog version and
    data object is immutable; adding a resource preserves previously frozen pins.
    Local callers may use synthetic geometry fixtures for offline checks only.
    """
    from .manifest import relative_name
    relative_name(source_name)
    directory = private_directory(directory)
    with root_descriptor(source_root) as root:
        data = read_file(root, source_name, max_bytes)
    summary = atomic.inspect_atomic_data(data, units=units, boundary=boundary,
        type_elements=type_elements, max_atoms=max_atoms, max_bytes=max_bytes)
    summary['parser_sha256'] = sha256(Path(atomic.__file__).read_bytes())
    public = dict(size=len(data), sha256=sha256(data), summary=summary)
    entry = dict(pin=sha256(canonical(public)), **public)
    validate_entry(entry, max_atoms=max_atoms)
    stored = {**entry, 'path': 'data/' + entry['sha256'] + '.data'}
    with _locked(directory) as root:
        current = None
        try:
            current = read_file(root, 'catalog.json', CATALOG_LIMIT)
        except ValueError:
            # Distinguish absence from an unsafe object without following links.
            try:
                os.stat('catalog.json', dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise
        entries = []
        if current is not None:
            old = json.loads(current)
            if not isinstance(old, dict) or set(old) != {'schema_version', 'entries'} or old['schema_version'] != 1:
                raise GeometryCatalogError('Existing geometry catalog is invalid')
            entries = old['entries']
            validate_view(dict(schema_version=1, catalog_sha256=sha256(current),
                entries=[{k: item[k] for k in ('pin', 'size', 'sha256', 'summary')} for item in entries]), max_atoms=max_atoms)
            for item in entries:
                if set(item) != {'pin', 'path', 'size', 'sha256', 'summary'} or item['path'] != 'data/'+item['sha256']+'.data':
                    raise GeometryCatalogError('Existing geometry source path is invalid')
                saved = read_file(root, item['path'], item['size'])
                if len(saved) != item['size'] or sha256(saved) != item['sha256']:
                    raise GeometryCatalogError('Existing geometry bytes changed; do not overwrite the catalog')
        duplicate = next((item for item in entries if item['pin'] == stored['pin']), None)
        if duplicate and duplicate != stored:
            raise GeometryCatalogError('Existing geometry pin changed')
        if duplicate is None:
            if len(entries) >= ENTRY_LIMIT:
                raise GeometryCatalogError('Geometry catalog has reached its explicit entry limit')
            entries = [*entries, stored]
        for name in ('data', 'versions'):
            path = directory/name
            private_directory(path)
        _immutable(directory/stored['path'], data)
        if current is not None:
            _immutable(directory/'versions'/ (sha256(current)+'.json'), current)
        encoded = canonical(dict(schema_version=1, entries=entries))
        if len(encoded) > CATALOG_LIMIT:
            raise GeometryCatalogError('Geometry catalog metadata exceeds its bound')
        digest = sha256(encoded)
        _immutable(directory/'versions'/(digest+'.json'), encoded)
        if current != encoded:
            fd, temporary = tempfile.mkstemp(prefix='.catalog-', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(encoded); output.flush(); os.fsync(output.fileno())
                os.chmod(temporary, 0o400)
                os.replace(temporary, directory/'catalog.json'); os.fsync(root)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
    return {**entry, 'catalog_sha256': digest}


def _immutable(path, data):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        with root_descriptor(path.parent) as root:
            if read_file(root, path.name, len(data)) != data:
                raise GeometryCatalogError('Immutable geometry object differs')
        return
    try:
        with os.fdopen(fd, 'wb', closefd=False) as output:
            output.write(data); output.flush(); os.fsync(fd)
    finally:
        os.close(fd)
    with root_descriptor(path.parent) as root:
        os.fsync(root)


class GeometryCatalogClient:
    """Reuse the configured pinned staging helper, SSH identity and audit trail."""
    def __init__(self, stage_client, *, max_atoms=100000):
        self.client = stage_client
        self.max_atoms = max_atoms

    def list(self):
        e = self.client.endpoint
        remote = [e.python_path, '-I', '-c', BOOTSTRAP, e.helper_path, e.helper_sha256, '--list-geometry']
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
                   '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no', e.host_alias, shlex.join(remote)]
        if self.client.transport is not None:
            command = self.client.transport.command(e.host_alias, remote)
        trace = self.client.audit_directory/uuid.uuid4().hex
        trace.mkdir(mode=0o700)
        _write_new(trace/'intent.json', dict(kind='geometry_metadata_read', helper_sha256=e.helper_sha256))
        try:
            result = _capture(command, timeout=self.client.timeout, max_bytes=CATALOG_LIMIT)
        except (OSError, ValueError) as error:
            result = dict(returncode=None, failure=type(error).__name__, stdout='', stderr='')
        _write_new(trace/'result.json', result)
        if result.get('failure') or result.get('returncode') != 0:
            raise GeometryCatalogError('Configured HPC geometry metadata could not be verified')
        try:
            view = json.loads(base64.b64decode(result['stdout'], validate=True))
            return validate_view(view, max_atoms=self.max_atoms)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise GeometryCatalogError('HPC geometry response is invalid; no resource selected') from error
