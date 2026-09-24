"""ASE geometry and serialization only. No calculator, relaxation or dynamics."""
from collections import Counter
from dataclasses import dataclass
from io import StringIO
import math

from .manifest import canonical, sha256
from .potentials import ELEMENTS

ASE_VERSION = '3.29.0'
CELL_ATOMS = {'fcc': 4, 'bcc': 2, 'diamond': 8, 'rocksalt': 8, 'zincblende': 8}
SPEC_FIELDS = {'crystal', 'elements', 'a_angstrom', 'repeat', 'orientation', 'boundary',
               'vacancies', 'substitutions', 'type_elements', 'masses_amu'}


class StructureError(ValueError):
    pass


def geometry_runtime():
    try:
        import ase
        import numpy
    except ImportError:
        raise StructureError('Install the geometry optional dependency before preparing structures') from None
    if ase.__version__ != ASE_VERSION:
        raise StructureError('ASE version differs from the pinned geometry implementation')
    return {'ase_version': ase.__version__, 'numpy_version': numpy.__version__}


def _positive(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def validate_structure(spec, *, max_atoms=100000):
    if type(max_atoms) is not int or not 1 <= max_atoms <= 1000000:
        raise StructureError('Invalid geometry atom limit')
    if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS:
        raise StructureError('All geometry fields must be explicit')
    if not isinstance(spec['crystal'], str) or spec['crystal'] not in CELL_ATOMS:
        raise StructureError('Unsupported crystal builder')
    if spec['orientation'] != 'cubic_axes':
        raise StructureError('This builder requires conventional cubic [100], [010], [001] axes')
    elements = spec['elements']
    count = 2 if spec['crystal'] in {'rocksalt', 'zincblende'} else 1
    if (not isinstance(elements, list) or len(elements) != count
            or any(not isinstance(x, str) or x not in ELEMENTS for x in elements)):
        raise StructureError('Invalid crystal species')
    if not _positive(spec['a_angstrom']) or spec['a_angstrom'] > 1000:
        raise StructureError('Explicit finite lattice constant in angstrom is required')
    repeat = spec['repeat']
    if not isinstance(repeat, list) or len(repeat) != 3 or any(type(x) is not int or not 1 <= x <= 1000 for x in repeat):
        raise StructureError('Three positive integer cell repetitions are required')
    total = CELL_ATOMS[spec['crystal']] * math.prod(repeat)
    if total > max_atoms:
        raise StructureError('Geometry exceeds the service atom limit')
    boundary = spec['boundary']
    if not isinstance(boundary, list) or len(boundary) != 3 or any(x not in ('p', 'f') for x in boundary):
        raise StructureError('Three explicit periodic or fixed boundaries are required')
    vacancies = spec['vacancies']
    if (not isinstance(vacancies, list) or len(vacancies) >= total
            or any(type(x) is not int or not 0 <= x < total for x in vacancies)
            or len(set(vacancies)) != len(vacancies)):
        raise StructureError('Vacancies use unique zero-based original site indices; retain at least one atom')
    substitutions = spec['substitutions']
    if not isinstance(substitutions, list) or len(substitutions) > total:
        raise StructureError('Invalid substitution list')
    sites = set(vacancies)
    for item in substitutions:
        if (not isinstance(item, dict) or set(item) != {'site', 'element'}
                or type(item['site']) is not int or not 0 <= item['site'] < total or item['site'] in sites
                or not isinstance(item['element'], str) or item['element'] not in ELEMENTS):
            raise StructureError('Substitution sites must be unique and cannot also be vacant')
        sites.add(item['site'])
    types, masses = spec['type_elements'], spec['masses_amu']
    if (not isinstance(types, list) or not 1 <= len(types) <= 118
            or any(not isinstance(x, str) or x not in ELEMENTS for x in types)
            or len(set(types)) != len(types) or not isinstance(masses, list) or len(masses) != len(types)
            or any(not _positive(x) or x > 1000 for x in masses)):
        raise StructureError('Provide unique ordered types and explicit positive masses')
    return total


@dataclass(frozen=True)
class Geometry:
    data: bytes
    receipt: dict


def build_structure(spec, *, units, max_atoms=100000):
    total = validate_structure(spec, max_atoms=max_atoms)
    if units not in ('metal', 'real'):
        raise StructureError('Unsupported LAMMPS unit system')
    runtime = geometry_runtime()
    from ase.build import bulk
    from ase.io.lammpsdata import write_lammps_data
    # All lattice, composition, replication and mass choices are supplied by
    # the plan; ASE elemental reference-state defaults are not consulted.
    atoms = bulk(''.join(spec['elements']), crystalstructure=spec['crystal'],
                 a=spec['a_angstrom'], cubic=True).repeat(spec['repeat'])
    if len(atoms) != total or atoms.calc is not None:
        raise StructureError('Unexpected geometry builder result')
    for change in spec['substitutions']:
        atoms[change['site']].symbol = change['element']
    del atoms[spec['vacancies']]
    symbols = atoms.get_chemical_symbols()
    if set(symbols) != set(spec['type_elements']):
        raise StructureError('Atom types must match the surviving structure species exactly')
    masses = dict(zip(spec['type_elements'], spec['masses_amu']))
    atoms.set_masses([masses[symbol] for symbol in symbols])
    atoms.set_pbc([x == 'p' for x in spec['boundary']])
    output = StringIO()
    write_lammps_data(output, atoms, specorder=spec['type_elements'], units=units,
                      atom_style='atomic', masses=True, velocities=False, reduce_cell=False)
    data = output.getvalue().encode('ascii')
    receipt = {'schema_version': 1, 'builder': 'ase.bulk.conventional_cubic',
               **runtime,
               'specification_sha256': sha256(canonical(spec)), 'data_sha256': sha256(data),
               'atom_count': len(atoms), 'original_site_count': total,
               'composition': dict(Counter(symbols)), 'cell_angstrom': atoms.cell.tolist(),
               'type_elements': list(spec['type_elements']), 'masses_amu': list(spec['masses_amu']),
               'boundary': list(spec['boundary']), 'units': units, 'atom_style': 'atomic',
               'site_index_convention': 'zero_based_before_edits',
               'physical_evaluation_performed': False, 'scientifically_verified': False}
    return Geometry(data, receipt)
