"""Private pinned resources; explicit binding conversions never alter the catalog."""
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile
from urllib.parse import urlsplit

from .manifest import canonical, private_directory, read_file, root_descriptor, sha256

MAX_FILE = 16 * 1024 * 1024
SNAP_DIAGONAL_RULE = 'remove-diagonalstyle-3-v1'
SNAP_DIAGONAL_SOURCE = ('https://github.com/lammps/lammps/blob/'
                        'd71abe6102c44577442ba7f03b7378a83166b9fd/doc/src/pair_snap.rst')
SNAP_LEGACY_DEFAULTS_SOURCE = ('https://github.com/lammps/lammps/blob/'
                              'b47e49223377d4ff6779e712bae54bbddc3596cf/src/SNAP/pair_snap.cpp')
ELEMENTS = set(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn '
                'Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd '
                'In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu '
                'Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu '
                'Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split())


class PotentialError(ValueError):
    pass


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise PotentialError('A full resource SHA-256 is required')
    return value


def _name(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value)
            or value == 'record.json'):
        raise PotentialError('Model filenames must be plain safe basenames')
    return value


def _text(value, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise PotentialError('Missing or excessive resource metadata')
    return value


def _metadata(document):
    fields = {'name', 'format', 'elements', 'units', 'source', 'license',
              'applicability', 'usage_evidence', 'interaction'}
    if not isinstance(document, dict) or set(document) != fields:
        raise PotentialError('Unsupported potential metadata fields')
    for key in ('name', 'license', 'applicability', 'usage_evidence'):
        _text(document[key])
    if document['format'] not in {'snap', 'meam'} or document['units'] not in {'metal', 'real'}:
        raise PotentialError('Unsupported model format or units')
    if document['format'] == 'meam' and document['units'] != 'metal':
        raise PotentialError('MEAM binding currently requires source-verified metal units')
    if document['interaction'] not in {'standalone', 'hybrid', 'unresolved'}:
        raise PotentialError('Declare standalone, hybrid or unresolved interaction')
    elements = document['elements']
    if (not isinstance(elements, list) or not 1 <= len(elements) <= 118
            or any(not isinstance(x, str) or x not in ELEMENTS for x in elements)
            or len(elements) != len(set(elements))):
        raise PotentialError('Invalid element inventory')
    source = document['source']
    if not isinstance(source, dict) or set(source) != {'url', 'revision', 'locator'}:
        raise PotentialError('Source URL, immutable revision and locator are required')
    for value in source.values():
        _text(value)
    url = urlsplit(source['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise PotentialError('Source must be an HTTPS provenance URL without credentials or query')
    if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', source['revision']):
        raise PotentialError('Pin a Git commit or source archive SHA-256')
    # Round trip also detaches the caller's mutable dictionary.
    return json.loads(canonical(document))


def _lines(data):
    try:
        return [part.split() for line in data.decode('utf-8').splitlines()
                if (part := line.split('#', 1)[0].strip())]
    except UnicodeDecodeError as exc:
        raise PotentialError('SNAP files must be UTF-8 text') from exc


def _number(token):
    try:
        value = float(token)
    except (ValueError, OverflowError) as exc:
        raise PotentialError('Invalid SNAP numeric value') from exc
    if not math.isfinite(value):
        raise PotentialError('Non-finite SNAP value')
    return value


def inspect_snap(coefficients, parameters, elements):
    """Bounded format checks; no claim of engine compatibility or scientific fitness."""
    rows = _lines(coefficients)
    if not rows or len(rows[0]) != 2 or any(not x.isdecimal() for x in rows[0]):
        raise PotentialError('Invalid SNAP coefficient header')
    nelem, ncoeff = map(int, rows[0])
    if not 1 <= nelem <= 118 or not 1 <= ncoeff <= 100000 or len(rows) != 1 + nelem * (ncoeff + 1):
        raise PotentialError('SNAP coefficient count does not match its header')
    names = []
    for index in range(nelem):
        start = 1 + index * (ncoeff + 1)
        header = rows[start]
        if len(header) != 3 or header[0] not in ELEMENTS or _number(header[1]) <= 0:
            raise PotentialError('Invalid SNAP element header')
        _number(header[2])
        names.append(header[0])
        for row in rows[start + 1:start + 1 + ncoeff]:
            if len(row) != 1:
                raise PotentialError('Expected one SNAP coefficient per line')
            _number(row[0])
    if names != elements or len(set(names)) != nelem:
        raise PotentialError('Declared elements must match coefficient file order exactly')
    params = {}
    for row in _lines(parameters):
        if len(row) < 2 or row[0] in params:
            raise PotentialError('Missing or duplicate SNAP parameter')
        params[row[0]] = row[1:]
    if not {'rcutfac', 'twojmax'} <= params.keys():
        raise PotentialError('Missing required SNAP parameters')
    scalar = {'rcutfac', 'twojmax', 'rfac0', 'rmin0', 'switchflag', 'bzeroflag',
              'quadraticflag', 'chemflag', 'bnormflag', 'wselfallflag', 'switchinnerflag',
              'chunksize', 'parallelthresh', 'diagonalstyle'}
    blockers = []
    for key, values in params.items():
        if key not in scalar | {'sinner', 'dinner'}:
            blockers.append('unsupported_parameter:' + key)
            continue
        if (key in scalar and len(values) != 1) or any(not math.isfinite(_number(x)) for x in values):
            raise PotentialError('Invalid SNAP parameter cardinality')
    for key in ('twojmax', 'chunksize', 'parallelthresh', 'diagonalstyle'):
        if key in params and (not params[key][0].isdecimal() or int(params[key][0]) > 100000):
            raise PotentialError('Invalid SNAP integer parameter')
    for key in ('switchflag', 'bzeroflag', 'quadraticflag', 'chemflag', 'bnormflag',
                'wselfallflag', 'switchinnerflag'):
        if key in params and params[key] not in [['0'], ['1']]:
            raise PotentialError('Invalid SNAP flag')
    if _number(params['rcutfac'][0]) <= 0:
        raise PotentialError('SNAP cutoff factor must be positive')
    if params.get('chemflag', ['0']) == ['0'] and params.get('diagonalstyle', ['3']) == ['3']:
        # Descriptor cardinality from the documented angular-index formulas.
        angular_limit = int(params['twojmax'][0])
        m = angular_limit // 2 + 1
        count = (m * (m + 1) * (m + 2) // 3 if angular_limit % 2
                 else m * (m + 1) * (2 * m + 1) // 6)
        expected = 1 + count
        if params.get('quadraticflag') == ['1']:
            expected += count * (count + 1) // 2
        if ncoeff != expected:
            raise PotentialError('SNAP coefficient count disagrees with descriptor parameters')
    if 'diagonalstyle' in params:
        blockers.append('legacy_diagonalstyle_requires_version_review')
    if params.get('chemflag') == ['1']:
        blockers.append('chemical_snap_not_supported')
    if params.get('switchinnerflag') == ['1'] or {'sinner', 'dinner'} & params.keys():
        blockers.append('inner_switching_not_supported')
    return {'elements': names, 'coefficient_count': ncoeff, 'parameters': params,
            'blockers': sorted(blockers)}


def inspect_meam(library, parameters, elements, *, version=2):
    """Conservative C++ MEAM format screen, not physical validation.

    Metadata elements fixes the parameter index order; library row order and
    LAMMPS atom type order are independent. Original bytes are never rewritten.
    """
    if version not in {1, 2}:
        raise PotentialError('Unsupported MEAM inspection version')
    warnings = []
    if len(elements) > 8:
        raise PotentialError('MEAM binding supports at most eight selected elements')
    try:
        library_text = library.decode('ascii')
        parameter_text = parameters.decode('ascii')
        tokens = shlex.split(library_text, comments=True, posix=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PotentialError('MEAM files require ASCII text and balanced quotes') from exc
    if not tokens or len(tokens) % 19 or len(tokens) > 19 * 2048:
        raise PotentialError('MEAM library requires bounded 19-field entries')
    lattices = {'fcc', 'bcc', 'hcp', 'dim', 'dia', 'b1', 'c11', 'l12',
                'b2', 'ch4', 'lin', 'zig', 'tri', 'sc'}
    number_pattern = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'

    def number(value):
        if not re.fullmatch(number_pattern, value):
            raise PotentialError('Invalid MEAM numeric value')
        result = float(value)
        if not math.isfinite(result):
            raise PotentialError('Non-finite MEAM numeric value')
        return result

    selected, blockers = {}, set()
    for offset in range(0, len(tokens), 19):
        row = tokens[offset:offset + 19]
        if any(not re.fullmatch('[A-Za-z0-9_-]+', x) for x in row[:2]):
            raise PotentialError('Invalid MEAM library label')
        values = [number(x) for x in row[2:]]
        if row[0] not in elements:
            continue
        if row[0] in selected:
            blockers.add('duplicate_selected_library_element:' + row[0])
            continue
        if row[1] not in lattices:
            blockers.add('unsupported_library_lattice:' + row[1])
        if (values[0] <= 0 or not values[1].is_integer() or not (1 if version == 1 else 0) <= values[1] <= 118
                or values[2] <= 0 or values[8] <= 0 or values[15] <= 0):
            raise PotentialError('Invalid MEAM library coordination, atomic number, mass, length or density')
        if version == 2 and values[1] == 0:
            warnings.append('zero_atomic_number:' + row[0])
        if values[11] != 1:
            blockers.add('library_t0_must_be_one:' + row[0])
        if values[16] not in {0, 1, 3, 4, -5}:
            blockers.add('unsupported_library_ibar:' + row[0])
        selected[row[0]] = {'entry': offset // 19 + 1, 'fields': row}
    if set(selected) != set(elements):
        raise PotentialError('Selected MEAM element missing from library')
    arities = {key: 0 for key in ('rc', 'delr', 'gsmooth_factor', 'augt1', 'ialloy',
                                 'mixture_ref_t', 'erose_form', 'emb_lin_neg', 'bkgd_dyn')}
    arities.update(rho0=1, Ec=2, delta=2, alpha=2, re=2, Cmax=3, Cmin=3,
                   lattce=2, nn2=2, attrac=2, repuls=2, zbl=2, theta=2)
    flags = {key: {0, 1} for key in ('augt1', 'mixture_ref_t', 'emb_lin_neg', 'bkgd_dyn', 'nn2', 'zbl')}
    flags.update(ialloy={0, 1, 2}, erose_form={0, 1, 2})
    assignments = {}
    assignment_history = []
    for line_number, raw in enumerate(parameter_text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_]*)\s*(?:\(\s*([0-9]+(?:\s*,\s*[0-9]+){0,2})\s*\))?\s*=\s*(.+)', line)
        if not match:
            raise PotentialError('Invalid MEAM parameter assignment')
        key, indices, value = match.groups()
        indices = [int(x.strip()) for x in indices.split(',')] if indices else []
        if any(not 1 <= index <= len(elements) for index in indices):
            raise PotentialError('MEAM parameter index outside selected library order')
        identity = key + ('(' + ','.join(map(str, indices)) + ')' if indices else '')
        if identity in assignments:
            if version == 1:
                raise PotentialError('Duplicate MEAM parameter assignment')
            assignment_history.append({'parameter': identity, 'line': line_number,
                                       'previous': assignments[identity], 'value': value})
            warnings.append('parameter_reassigned:' + identity)
        if key not in arities:
            blockers.add('unsupported_parameter:' + key)
        elif len(indices) != arities[key]:
            raise PotentialError('Invalid MEAM parameter index count')
        if key == 'lattce':
            try:
                parsed = shlex.split(value)
            except ValueError as exc:
                raise PotentialError('Invalid MEAM lattice quote') from exc
            if len(parsed) != 1 or parsed[0] not in lattices:
                raise PotentialError('Unsupported MEAM reference lattice')
        else:
            numeric = number(value)
            if key in flags and numeric not in flags[key]:
                raise PotentialError('Invalid MEAM flag')
            if key in {'rc', 'delr', 'rho0', 're', 'gsmooth_factor'} and numeric <= 0:
                raise PotentialError('MEAM length or density parameter must be positive')
        assignments[identity] = value
        if len(assignments) + len(assignment_history) > 2048:
            raise PotentialError('Too many MEAM parameter assignments')
    if not assignments:
        raise PotentialError('An explicit nonempty MEAM parameter file is required')
    result = {'screen': 'meam_static_v' + str(version), 'elements': list(elements),
              'library_entries': selected, 'parameters': assignments, 'blockers': sorted(blockers)}
    if version == 2:
        result.update(warnings=sorted(set(warnings)), reassignments=assignment_history)
    return result


def _roles(metadata):
    return {'library' if metadata['format'] == 'meam' else 'coefficients', 'parameters', 'license'}


def _inspect(content, metadata, *, meam_version=2):
    if metadata['format'] == 'meam':
        return inspect_meam(content['library'], content['parameters'], metadata['elements'], version=meam_version)
    return inspect_snap(content['coefficients'], content['parameters'], metadata['elements'])


class PotentialCatalog:
    """Administrator imports only; a catalog pin is not permission to use it in a test."""

    def __init__(self, directory):
        self.directory = private_directory(directory)

    def import_model(self, source, *, metadata, files):
        metadata = _metadata(metadata)
        if not isinstance(files, dict) or set(files) != _roles(metadata):
            raise PotentialError('Exactly the format-specific model, parameters and license files are required')
        names = [_name(x) for x in files.values()]
        if len(set(names)) != 3:
            raise PotentialError('Model resource roles must use distinct files')
        with root_descriptor(source) as root:
            content = {role: read_file(root, name, MAX_FILE) for role, name in files.items()}
        if not content['license'].strip():
            raise PotentialError('License text must be retained')
        diagnostics = _inspect(content, metadata)
        records = {role: {'name': files[role], 'size': len(data), 'sha256': sha256(data)}
                   for role, data in content.items()}
        record = {'schema_version': 1, 'status': 'collected', 'metadata': metadata,
                  'files': records, 'inspection': diagnostics}
        encoded = canonical(record)
        if len(encoded) > 100000:
            raise PotentialError('Potential record exceeds metadata limit')
        pin = sha256(encoded)
        stage = Path(tempfile.mkdtemp(prefix='.import-', dir=self.directory))
        try:
            for name, data in [(files[role], data) for role, data in content.items()] + [('record.json', encoded)]:
                with (stage / name).open('xb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                (stage / name).chmod(0o400)
            with root_descriptor(stage) as root:
                os.fsync(root)
            lock = os.open(self.directory / '.import.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PotentialError('Unsafe catalog lock')
                fcntl.flock(lock, fcntl.LOCK_EX)
                destination = self.directory / pin
                if not destination.exists():
                    os.rename(stage, destination)
                self.read(pin)
                with root_descriptor(self.directory) as root:
                    os.fsync(root)
            finally:
                os.close(lock)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return pin

    def read(self, pin):
        path = self.directory / _digest(pin)
        with root_descriptor(path) as root:
            encoded = read_file(root, 'record.json', 100000)
            if sha256(encoded) != pin:
                raise PotentialError('Potential record hash mismatch')
            record = json.loads(encoded)
            if record.get('schema_version') != 1 or record.get('status') != 'collected':
                raise PotentialError('Invalid potential record')
            metadata = _metadata(record['metadata'])
            if set(record.get('files', {})) != _roles(metadata):
                raise PotentialError('Invalid potential file roles')
            content, expected = {}, {'record.json'}
            for role, item in record['files'].items():
                name = _name(item['name'])
                if name in expected or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE:
                    raise PotentialError('Invalid potential file record')
                expected.add(name)
                data = read_file(root, name, item['size'])
                if len(data) != item['size'] or sha256(data) != item['sha256']:
                    raise PotentialError('Potential file hash mismatch')
                content[role] = data
            if set(os.listdir(root)) != expected:
                raise PotentialError('Undeclared files in potential resource')
        legacy = metadata['format'] == 'meam' and record['inspection'].get('screen') == 'meam_static_v1'
        if _inspect(content, metadata, meam_version=1 if legacy else 2) != record['inspection']:
            raise PotentialError('Potential inspection mismatch')
        return record, content

    def list_models(self):
        return [{'pin': name, 'record': self.read(name)[0]}
                for name in sorted(os.listdir(self.directory)) if re.fullmatch(r'[a-f0-9]{64}', name)]


@dataclass(frozen=True)
class PotentialBinding:
    pin: str
    files: dict
    commands: tuple
    receipt: dict


class PotentialAdapter:
    """Service-owned environment declaration and resource allowlist, not model inputs.

    Produces data for case preparation. No subprocess, network, evaluation or submission.
    A formal test still requires OS-enforced separation and audited allowed resources.
    """

    def __init__(self, catalog, *, allowed_pins, software_sha256, packages, legacy_snap_pins=()):
        self.catalog = catalog
        self.allowed_pins = frozenset(_digest(x) for x in allowed_pins)
        self.software_sha256 = _digest(software_sha256)
        self.packages = frozenset(packages)
        if not isinstance(legacy_snap_pins, (list, tuple, set, frozenset)):
            raise PotentialError('Legacy SNAP policy must be a collection of resource pins')
        self.legacy_snap_pins = frozenset(_digest(x) for x in legacy_snap_pins)
        if not self.legacy_snap_pins <= self.allowed_pins:
            raise PotentialError('Legacy SNAP policy must be a subset of allowed resources')

    def compatibility_policy(self):
        return {'rule': SNAP_DIAGONAL_RULE, 'pins': sorted(self.legacy_snap_pins),
                'software_sha256': self.software_sha256, 'meam_inspection_version': 2}

    def _parameters(self, pin, record, content):
        inspection = record['inspection']
        original = content['parameters']
        if pin not in self.legacy_snap_pins:
            return original, inspection['blockers'], None
        params = inspection['parameters']
        # This narrowly reviewed rule is not a general old-to-new translator.
        required = {'rcutfac', 'twojmax', 'rfac0', 'rmin0', 'diagonalstyle',
                    'quadraticflag', 'bzeroflag'}
        if (not required <= params.keys() or params.keys() - required - {'switchflag'}
                or params.get('diagonalstyle') != ['3']
                or inspection['blockers'] != ['legacy_diagonalstyle_requires_version_review']):
            raise PotentialError('Resource does not match the reviewed legacy SNAP rule')
        lines = original.splitlines(keepends=True)
        removed = [i for i, line in enumerate(lines)
                   if line.split(b'#', 1)[0].split()[:1] == [b'diagonalstyle']]
        if len(removed) != 1:
            raise PotentialError('Expected exactly one legacy parameter line')
        converted = b''.join(line for i, line in enumerate(lines) if i != removed[0])
        checked = inspect_snap(content['coefficients'], converted, record['metadata']['elements'])
        if checked['parameters'] != {k: v for k, v in params.items() if k != 'diagonalstyle'}:
            raise PotentialError('Unexpected parameter change during SNAP conversion')
        receipt = {'rule': SNAP_DIAGONAL_RULE, 'basis': SNAP_DIAGONAL_SOURCE,
                   'legacy_defaults_basis': SNAP_LEGACY_DEFAULTS_SOURCE,
                   'unwritten_defaults_requiring_environment_review':
                       {} if 'switchflag' in params else {'switchflag': 1},
                   'original_parameter_sha256': sha256(original),
                   'bound_parameter_sha256': sha256(converted),
                   'removed_line': removed[0] + 1, 'removed_parameter': {'diagonalstyle': '3'},
                   'numerical_equivalence_verified': False}
        return converted, checked['blockers'], receipt

    def compatible_models(self, *, units=None):
        """The same resource checks serve availability, generation and binding."""
        models = []
        for pin in sorted(self.allowed_pins):
            record, _ = self.catalog.read(pin)
            meta = record['metadata']
            if units is not None and meta['units'] != units:
                continue
            try:
                self.resolve_potential(pin, type_elements=meta['elements'], units=meta['units'])
            except PotentialError:
                continue
            models.append({'pin': pin, 'format': meta['format'], 'elements': meta['elements'],
                           'units': meta['units'], 'applicability': meta['applicability'],
                           'warnings': record['inspection'].get('warnings', [])})
        return models

    def resolve_potential(self, pin, *, type_elements, units):
        if _digest(pin) not in self.allowed_pins:
            raise PotentialError('Potential is not in this task resource allowlist')
        record, content = self.catalog.read(pin)
        meta = record['metadata']
        if (not isinstance(type_elements, list) or not 1 <= len(type_elements) <= 118
                or any(not isinstance(x, str) or x not in meta['elements'] for x in type_elements)):
            raise PotentialError('Map every atom type explicitly to a model element')
        if units != meta['units']:
            raise PotentialError('Task and potential units differ; no automatic conversion')
        if meta['interaction'] != 'standalone':
            raise PotentialError('Hybrid or unresolved interactions need another adapter')
        if meta['format'] == 'meam':
            if pin in self.legacy_snap_pins:
                raise PotentialError('MEAM cannot use a legacy SNAP conversion policy')
            parameters, blockers, conversion = content['parameters'], record['inspection']['blockers'], None
        else:
            parameters, blockers, conversion = self._parameters(pin, record, content)
        if blockers:
            raise PotentialError('Potential compatibility blocked: ' + ', '.join(blockers))
        package = 'MEAM' if meta['format'] == 'meam' else 'ML-SNAP'
        if package not in self.packages:
            raise PotentialError('Declared software environment lacks ' + package)
        # Fixed names prevent metadata or upstream filenames becoming LAMMPS syntax.
        prefix = 'potentials/' + pin
        if meta['format'] == 'meam':
            files = {prefix + '/library.meam': content['library'],
                     prefix + '/model.meam': parameters, prefix + '/LICENSE.txt': content['license']}
            commands = ('pair_style meam', f'pair_coeff * * {prefix}/library.meam '
                        + ' '.join(meta['elements']) + f' {prefix}/model.meam ' + ' '.join(type_elements))
        else:
            files = {prefix + '/model.snapcoeff': content['coefficients'],
                     prefix + '/model.snapparam': parameters,
                     prefix + '/LICENSE.txt': content['license']}
            commands = ('pair_style snap',
                        f'pair_coeff * * {prefix}/model.snapcoeff {prefix}/model.snapparam ' + ' '.join(type_elements))
        receipt = {'schema_version': 1, 'potential_sha256': pin, 'software_sha256': self.software_sha256,
                   'atom_type_elements': list(type_elements), 'units': units,
                   'files': {name: sha256(data) for name, data in files.items()},
                   'commands': list(commands), 'checks': 'static_resource_binding',
                   'execution_authorized': False, 'environment_verified': False,
                   'scientifically_verified': False}
        if conversion is not None:
            receipt['compatibility_conversion'] = conversion
        if meta['format'] == 'meam':
            receipt['library_index_elements'] = list(meta['elements'])
            receipt['potential_warnings'] = record['inspection'].get('warnings', [])
        return PotentialBinding(pin, files, commands, receipt)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Private administrator potential catalog; no simulation')
    parser.add_argument('--store', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    ingest = commands.add_parser('import')
    ingest.add_argument('--source', required=True)
    ingest.add_argument('--metadata', required=True, help='JSON with metadata and files objects')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('pin')
    commands.add_parser('list')
    args = parser.parse_args()
    catalog = PotentialCatalog(args.store)
    if args.action == 'import':
        config_path = Path(args.metadata)
        with root_descriptor(config_path.parent) as root:
            config = json.loads(read_file(root, config_path.name, 100000))
        print(catalog.import_model(args.source, metadata=config['metadata'], files=config['files']))
    elif args.action == 'inspect':
        print(json.dumps(catalog.read(args.pin)[0], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(catalog.list_models(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
