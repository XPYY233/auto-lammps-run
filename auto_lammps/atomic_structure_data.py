"""Read-only checks for pinned, atomic-style LAMMPS geometry bytes.

This module has no file, network, engine or calculator interface. The caller
retains the original bytes on HPC and binds the returned summary to its resource
pin. Parsing establishes a bounded input contract, never execution permission,
physical stability or scientific success. Units, boundaries and the type-to-
element mapping must be supplied explicitly; none is inferred from the file.
"""
import hashlib
import io
import json
import math
import re


PARSER_VERSION = 1
MAX_ATOMS = 1_000_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_LINE_LENGTH = 65536
ENGINE_CONTENT_LIMIT = 254
ELEMENTS = frozenset(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn '
    'Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd '
    'In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu '
    'Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu '
    'Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split())
NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
INTEGER = re.compile(r'[+-]?[0-9]+\Z')
UNSIGNED = re.compile(r'[0-9]+\Z')
TITLE_COMMANDS = frozenset(('units atom_style boundary read_data read_restart '
    'create_box create_atoms pair_style pair_coeff run minimize include jump '
    'shell python variable clear write_data write_restart').split())


class AtomicStructureDataError(ValueError):
    pass


def _number(token):
    if not NUMBER.fullmatch(token):
        raise AtomicStructureDataError('Expected a finite numeric geometry value')
    value = float(token)
    if not math.isfinite(value):
        raise AtomicStructureDataError('Expected a finite numeric geometry value')
    return value


def _integer(token):
    if not INTEGER.fullmatch(token):
        raise AtomicStructureDataError('Expected an integer identifier or image flag')
    try:
        return int(token)
    except ValueError:
        raise AtomicStructureDataError('Integer token exceeds the parsing bound') from None


def _hash_record(digest, record):
    # Streaming, original row order; the full coordinate table is never returned.
    digest.update(json.dumps(record, separators=(',', ':'), allow_nan=False).encode('ascii'))
    digest.update(b'\n')


def _cell(header):
    required = {'atoms', 'atom types', 'xlo xhi', 'ylo yhi', 'zlo zhi'}
    if not required <= header.keys():
        raise AtomicStructureDataError('Atomic data requires counts and all three explicit box bounds')
    bounds = [header[key] for key in ('xlo xhi', 'ylo yhi', 'zlo zhi')]
    lengths = [hi - lo for lo, hi in bounds]
    if any(not math.isfinite(length) or length <= 0 for length in lengths):
        raise AtomicStructureDataError('Box lengths must be finite and positive')
    xy, xz, yz = header.get('xy xz yz', [0.0, 0.0, 0.0])
    lx, ly, lz = lengths
    cell = [[lx, 0.0, 0.0], [xy, ly, 0.0], [xz, yz, lz]]
    volume = math.prod(lengths)
    norms = [math.hypot(*row) for row in cell]
    # Match the existing geometry adapter's numerical-degeneracy contract.
    # Large finite tilts are permitted; LAMMPS does not require half-box tilts.
    ratio = math.prod(length / norm for length, norm in zip(lengths, norms))
    if (not math.isfinite(volume) or volume <= 1e-12
            or any(not math.isfinite(norm) for norm in norms) or ratio <= 1e-10):
        raise AtomicStructureDataError('Box or restricted-triclinic tilt is numerically degenerate')
    return cell, [lo for lo, _ in bounds], [xy, xz, yz]


def _check_position(position, images, cell, origin, boundary):
    lx, ly, lz = cell[0][0], cell[1][1], cell[2][2]
    xy, xz, yz = cell[1][0], cell[2][0], cell[2][1]
    dx, dy, dz = [coordinate - start for coordinate, start in zip(position, origin)]
    sz = dz / lz
    sy = (dy - yz * sz) / ly
    sx = (dx - xy * sy - xz * sz) / lx
    fractions = [sx, sy, sz]
    if any(not math.isfinite(value) for value in fractions):
        raise AtomicStructureDataError('Coordinates cannot be represented in the declared box')
    for axis, mode in enumerate(boundary):
        if mode == 'f' and not 0 <= fractions[axis] < 1:
            raise AtomicStructureDataError('Atom lies outside a fixed box direction')
        if mode == 'f' and images and images[axis] != 0:
            raise AtomicStructureDataError('A fixed direction cannot retain a nonzero image flag')
    return any(mode == 'p' and not 0 <= value < 1 for mode, value in zip(boundary, fractions))


def inspect_atomic_data(data, *, units, boundary, type_elements, max_atoms=100000,
                        max_bytes=DEFAULT_MAX_BYTES):
    """Return a small JSON-compatible summary without modifying or writing data.

    Version 1 permits orthogonal/restricted-triclinic atomic data, ``Atoms`` rows
    with five columns or uniformly eight columns (three integer image flags),
    and an optional complete ``Masses`` section. The ID set must be exactly
    1..N; row order is preserved. Topology, velocities, coefficients, general
    triclinic headers and other sections are deliberately unsupported.

    Section headers require the ignored separator line mandated by read_data;
    records are contiguous physical lines, without blank/comment-only records.
    Version 1 conservatively limits effective content to 254 characters for
    compatibility with fixed-line read_data implementations. Long trailing
    comments are retained in the raw-byte identity. Image flags use the
    SMALLBIG-safe range [-512,511]; larger lines or ABI-specific image values
    are unsupported by this parser version. Line endings are LF or CRLF,
    not standalone CR.

    Missing masses remain None. The caller must resolve that absence explicitly
    before preparing a runnable input; atomic masses never identify elements.
    max_bytes is a caller-controlled parsing/storage bound, not a run budget.
    """
    if units not in ('metal', 'real'):
        raise AtomicStructureDataError('Explicit metal or real units are required')
    if (not isinstance(boundary, (list, tuple)) or len(boundary) != 3
            or any(value not in ('p', 'f') for value in boundary)):
        raise AtomicStructureDataError('Three explicit periodic or fixed boundaries are required')
    if (not isinstance(type_elements, (list, tuple)) or not 1 <= len(type_elements) <= 118
            or any(not isinstance(value, str) or value not in ELEMENTS for value in type_elements)
            or len(set(type_elements)) != len(type_elements)):
        raise AtomicStructureDataError('An explicit unique ordered type-to-element mapping is required')
    if type(max_atoms) is not int or not 1 <= max_atoms <= MAX_ATOMS:
        raise AtomicStructureDataError('Invalid geometry atom limit')
    if type(max_bytes) is not int or not 1 <= max_bytes <= 512 * 1024 * 1024:
        raise AtomicStructureDataError('Invalid geometry byte limit')
    if not isinstance(data, bytes) or not data or len(data) > max_bytes:
        raise AtomicStructureDataError('Atomic data must be nonempty bounded bytes')
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError:
        raise AtomicStructureDataError('Version 1 accepts ASCII atomic data only') from None
    if any(ord(char) < 32 and char not in '\r\n\t' for char in text) or '\x7f' in text:
        raise AtomicStructureDataError('Atomic data contains unsupported control characters')
    if re.search(r'\r(?!\n)', text):
        raise AtomicStructureDataError('Atomic data requires LF or CRLF physical line endings')
    lines = io.StringIO(text, newline=None)
    # LAMMPS always consumes the first physical line as a title.
    title = lines.readline()
    if len(title) > MAX_LINE_LENGTH:
        raise AtomicStructureDataError('Atomic data line exceeds the parsing bound')
    title_words = title.split('#', 1)[0].split()
    if title_words and title_words[0] in TITLE_COMMANDS:
        raise AtomicStructureDataError('A LAMMPS script is not an atomic data file')
    header, sections, section = {}, set(), None
    counts, masses = [0] * len(type_elements), {}
    identifiers, rows, columns = None, 0, None
    coordinates = hashlib.sha256()
    ids_digest = hashlib.sha256()
    cell, origin, tilt = None, None, None
    separator_pending, records_remaining = False, 0
    periodic_remapping = False
    for line_number, raw in enumerate(lines, 2):
        if len(raw) > MAX_LINE_LENGTH:
            raise AtomicStructureDataError('Atomic data line exceeds the parsing bound')
        body, _, comment = raw.partition('#')
        # Conservative compatibility with MAXLINE=256/fgets_trunc engines.
        # Truncating only trailing whitespace/comments does not change a record.
        if len(body.rstrip()) > ENGINE_CONTENT_LIMIT:
            raise AtomicStructureDataError('Effective content above 254 characters is unsupported by parser version 1')
        body = body.strip()
        if separator_pending:
            if body:
                raise AtomicStructureDataError('A section header requires a blank or comment-only separator line')
            separator_pending = False
            continue
        if not body:
            if records_remaining:
                raise AtomicStructureDataError('Atomic section records must occupy contiguous physical lines')
            continue
        if records_remaining and '#' in raw and not raw[raw.index('#') - 1].isspace():
            raise AtomicStructureDataError('Atomic record comments must be separated from numeric tokens by whitespace')
        if body in {'Masses', 'Atoms'}:
            if records_remaining:
                raise AtomicStructureDataError('A section ended before its declared record count')
            if body in sections:
                raise AtomicStructureDataError('Duplicate atomic data section')
            if body == 'Atoms' and comment.strip() not in ('', 'atomic'):
                raise AtomicStructureDataError('Only the atomic Atoms style is supported')
            if section is None:
                cell, origin, tilt = _cell(header)
                if header['atom types'] != len(type_elements):
                    raise AtomicStructureDataError('Header atom types differ from the explicit element mapping')
                identifiers = bytearray(header['atoms'] + 1)
            sections.add(body)
            section = body
            separator_pending = True
            records_remaining = header['atom types'] if body == 'Masses' else header['atoms']
            continue
        tokens = body.split()
        if section is None:
            if len(tokens) == 2 and tokens[1] == 'atoms':
                if not UNSIGNED.fullmatch(tokens[0]):
                    raise AtomicStructureDataError('Header counts require unsigned integers')
                key, value = 'atoms', _integer(tokens[0])
                if not 1 <= value <= max_atoms:
                    raise AtomicStructureDataError('Declared atom count exceeds the geometry limit or is empty')
            elif len(tokens) == 3 and tokens[1:] == ['atom', 'types']:
                if not UNSIGNED.fullmatch(tokens[0]):
                    raise AtomicStructureDataError('Header counts require unsigned integers')
                key, value = 'atom types', _integer(tokens[0])
                if not 1 <= value <= 118:
                    raise AtomicStructureDataError('Invalid declared atom type count')
            elif len(tokens) == 4 and tokens[2:] in (['xlo', 'xhi'], ['ylo', 'yhi'], ['zlo', 'zhi']):
                key, value = ' '.join(tokens[2:]), [_number(token) for token in tokens[:2]]
            elif len(tokens) == 6 and tokens[3:] == ['xy', 'xz', 'yz']:
                key, value = 'xy xz yz', [_number(token) for token in tokens[:3]]
            else:
                raise AtomicStructureDataError('Unsupported atomic header or section at line '+str(line_number))
            if key in header:
                raise AtomicStructureDataError('Duplicate atomic data header field')
            header[key] = value
        elif records_remaining == 0:
            raise AtomicStructureDataError('Undeclared extra record or unsupported atomic section')
        elif section == 'Masses':
            if len(tokens) != 2:
                raise AtomicStructureDataError('Masses requires exactly a type ID and mass; unsupported section or row')
            kind, mass = _integer(tokens[0]), _number(tokens[1])
            if not 1 <= kind <= len(type_elements) or kind in masses or mass <= 0:
                raise AtomicStructureDataError('Masses requires unique declared types and positive finite masses')
            masses[kind] = mass
            records_remaining -= 1
        else:
            if len(tokens) not in (5, 8):
                raise AtomicStructureDataError('Atoms requires five atomic columns or eight including image flags')
            if columns is not None and len(tokens) != columns:
                raise AtomicStructureDataError('Atoms must use one consistent image-flag column layout')
            columns = len(tokens)
            identifier, kind = _integer(tokens[0]), _integer(tokens[1])
            if not 1 <= identifier <= header['atoms'] or identifiers[identifier]:
                raise AtomicStructureDataError('Atom IDs must be unique and form the continuous set 1..N')
            if not 1 <= kind <= len(type_elements):
                raise AtomicStructureDataError('Atom type is outside the explicit mapping')
            position = [_number(token) for token in tokens[2:5]]
            images = [_integer(token) for token in tokens[5:]]
            if any(not -512 <= flag <= 511 for flag in images):
                raise AtomicStructureDataError('Image flags outside [-512,511] are unsupported by parser version 1')
            periodic_remapping |= _check_position(position, images, cell, origin, boundary)
            identifiers[identifier] = 1
            rows += 1
            records_remaining -= 1
            counts[kind - 1] += 1
            _hash_record(coordinates, [identifier, kind, *position, *images])
            _hash_record(ids_digest, identifier)
    if separator_pending or records_remaining or 'Atoms' not in sections or rows != header['atoms']:
        raise AtomicStructureDataError('Atoms section must contain exactly the declared continuous 1..N ID set')
    if 'Masses' in sections and len(masses) != len(type_elements):
        raise AtomicStructureDataError('A present Masses section must cover every declared type')
    return {'schema_version': 1, 'parser': 'lammps_atomic_data', 'parser_version': PARSER_VERSION,
            'data_sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
            'atom_style': 'atomic', 'units': units, 'boundary': list(boundary),
            'type_elements': list(type_elements), 'atom_count': rows, 'type_count': len(type_elements),
            'type_counts': {str(index + 1): count for index, count in enumerate(counts)},
            'composition': {element: count for element, count in zip(type_elements, counts) if count},
            'cell_angstrom': cell, 'origin_angstrom': origin, 'tilt_angstrom': tilt,
            'masses_amu': [masses[kind] for kind in range(1, len(type_elements) + 1)] if masses else None,
            'mass_source': 'file' if masses else 'not_in_file',
            'coordinate_content_sha256': coordinates.hexdigest(), 'particle_id_order_sha256': ids_digest.hexdigest(),
            'id_policy': 'continuous_1_to_N_preserve_input_order', 'atom_record_columns': columns,
            'image_flag_range': [-512, 511],
            'periodic_remapping_may_occur': periodic_remapping, 'fixed_boundary_geometry_verified': True,
            'original_bytes_modified': False, 'elements_inferred': False,
            'physical_evaluation_performed': False, 'scientifically_verified': False}
