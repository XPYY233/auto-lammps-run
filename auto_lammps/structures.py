"""ASE geometry and serialization only. No calculator, relaxation or dynamics."""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from io import StringIO
from hashlib import sha256 as hash_bytes
import math

from .manifest import canonical, sha256
from .potentials import ELEMENTS

ASE_VERSION = '3.29.0'
CELL_ATOMS = {'fcc': 4, 'bcc': 2, 'diamond': 8, 'rocksalt': 8, 'zincblende': 8}
SPEC_FIELDS = {'crystal', 'elements', 'a_angstrom', 'repeat', 'orientation', 'boundary',
               'vacancies', 'substitutions', 'type_elements', 'masses_amu'}
EXPLICIT_FIELDS = (SPEC_FIELDS - {'elements', 'a_angstrom'}) | {
    'cell_angstrom', 'site_elements', 'scaled_positions'}
ALLOY_CRYSTALS = ('fcc', 'bcc')
COMPOSITION_SEED_MAX = 4294967295
MAX_COMPOSITION_LAYERS = 32
GEOMETRY_TOOL_VERSION = 2


class StructureError(ValueError):
    pass


def geometry_tool_context(max_atoms):
    """Automatically supplied to planning, repair and review, not model-selected."""
    return {'adapter': 'ase_geometry', 'version': GEOMETRY_TOOL_VERSION,
            'ase_version': ASE_VERSION, 'max_atoms': max_atoms,
            'cubic': {'basis_counts': dict(CELL_ATOMS), 'required_fields': sorted(SPEC_FIELDS),
                      'orientation': 'cubic_axes'},
            'explicit_cell': {'required_fields': sorted(EXPLICIT_FIELDS),
                              'orientation': 'provided_axes', 'frame': 'restricted_triclinic'},
            'composition': {'crystals': list(ALLOY_CRYSTALS),
                            'random_counts': {'required_fields': ['mode', 'counts', 'seed'],
                                              'seed_range': [0, COMPOSITION_SEED_MAX],
                                              'algorithm': 'sha256_rank_v1',
                                              'counts': 'positive integers in type_elements order; sum is original sites'},
                            'fractional_layers': {'required_fields': ['mode', 'axis', 'breaks', 'elements'],
                                                  'axes': [0, 1, 2], 'max_layers': MAX_COMPOSITION_LAYERS,
                                                  'cell': 'final replicated cell', 'interval': '[lower,upper)'}},
            'edit_order': ['composition', 'substitutions', 'vacancies'],
            'site_indices': 'zero-based original sites; x,y,z,basis replication order',
            'missing_scientific_parameters': 'ask a specific clarification; never guess or change scope',
            'physical_evaluation': 'HPC only; this geometry adapter performs none',
            'scientific_success': 'not established by geometry preparation'}


def geometry_runtime():
    try:
        import ase
        import numpy
    except ImportError:
        raise StructureError('Install the geometry optional dependency before preparing structures') from None
    if ase.__version__ != ASE_VERSION:
        raise StructureError('ASE version differs from the pinned geometry implementation')
    return {'ase_version': ase.__version__, 'numpy_version': numpy.__version__}


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _positive(value):
    return _finite(value) and value > 0


def _explicit_cell(spec, max_basis_atoms):
    if spec['orientation'] != 'provided_axes':
        raise StructureError('Explicit cells require provided_axes; no implicit rotation')
    cell = spec['cell_angstrom']
    if (not isinstance(cell, list) or len(cell) != 3
            or any(not isinstance(row, list) or len(row) != 3
                   or any(not _finite(x) or abs(x) > 1000 for x in row) for row in cell)):
        raise StructureError('Provide three finite cell vectors in angstrom')
    # LAMMPS restricted-triclinic form preserves the supplied Cartesian frame.
    # ASE can rotate general cells, but silently rotating only the geometry
    # would also require transforming the agent's directional workflow.
    if (any(cell[i][j] != 0 for i, j in ((0, 1), (0, 2), (1, 2)))
            or any(cell[i][i] <= 0 for i in range(3))):
        raise StructureError('Cell vectors must have restricted-triclinic form with positive diagonal')
    volume = math.prod(cell[i][i] for i in range(3))
    length_product = math.prod(math.hypot(*row) for row in cell)
    if volume <= 1e-12 or volume / length_product <= 1e-10:
        raise StructureError('Cell is numerically degenerate')
    elements, positions = spec['site_elements'], spec['scaled_positions']
    if (not isinstance(elements, list) or not 1 <= len(elements) <= max_basis_atoms
            or not isinstance(positions, list) or len(positions) != len(elements)):
        raise StructureError('Provide one species and fractional position per bounded basis site')
    if any(not isinstance(x, str) or x not in ELEMENTS for x in elements):
        raise StructureError('Invalid basis species')
    seen = set()
    for position in positions:
        if (not isinstance(position, list) or len(position) != 3
                or any(not _finite(x) or not 0 <= x < 1 for x in position)):
            raise StructureError('Fractional coordinates must be finite and in [0,1); no implicit wrapping')
        point = tuple(position)
        if point in seen:
            raise StructureError('Duplicate basis positions; do not silently overlap atoms')
        seen.add(point)
    return len(elements)


def _validate_assignment(spec, total):
    """Composition is explicit task data, never inferred from elemental defaults."""
    assignment = spec.get('assignment')
    if assignment is None:
        if 'assignment' in spec:
            raise StructureError('Omit assignment or supply its complete specification')
        return
    if not isinstance(assignment, dict) or spec['crystal'] not in ALLOY_CRYSTALS:
        raise StructureError('Composition assignment requires a conventional fcc or bcc geometry')
    if assignment.get('mode') == 'random_counts':
        if set(assignment) != {'mode', 'counts', 'seed'}:
            raise StructureError('Random composition requires explicit counts and seed')
        counts, seed = assignment['counts'], assignment['seed']
        if (not isinstance(counts, list) or len(counts) != len(spec['type_elements'])
                or any(type(x) is not int or x <= 0 for x in counts) or sum(counts) != total):
            raise StructureError('Positive integer counts must follow type_elements and sum to all original sites')
        if type(seed) is not int or not 0 <= seed <= COMPOSITION_SEED_MAX:
            raise StructureError('An explicit composition seed in [0,4294967295] is required')
    elif assignment.get('mode') == 'fractional_layers':
        if set(assignment) != {'mode', 'axis', 'breaks', 'elements'}:
            raise StructureError('Layers require an explicit fractional axis, breaks and species')
        axis, breaks, elements = assignment['axis'], assignment['breaks'], assignment['elements']
        if type(axis) is not int or axis not in (0, 1, 2):
            raise StructureError('Layer axis must be 0, 1 or 2 in the final replicated cell')
        if (not isinstance(breaks, list) or not 3 <= len(breaks) <= MAX_COMPOSITION_LAYERS+1
                or any(not _finite(x) or not 0 <= x <= 1 for x in breaks)
                or breaks[0] != 0 or breaks[-1] != 1
                or any(a >= b for a, b in zip(breaks, breaks[1:]))):
            raise StructureError('Strictly increasing layer breaks must span [0,1] without gaps')
        if (not isinstance(elements, list) or len(elements) != len(breaks)-1
                or any(not isinstance(x, str) or x not in spec['type_elements'] for x in elements)
                or set(elements) != set(spec['type_elements'])):
            raise StructureError('Provide one declared species per layer, covering all types')
    else:
        raise StructureError('Unsupported composition assignment; no guessed fractions or ordering')


def _assign_composition(atoms, spec):
    assignment = spec.get('assignment')
    if assignment is None:
        return None
    total = len(atoms)
    if assignment['mode'] == 'random_counts':
        # Stable across Python/NumPy versions. Hash collisions use original site order.
        prefix = b'auto-lammps/alloy-sha256-rank-v1\0' + assignment['seed'].to_bytes(4, 'big')
        order = sorted(range(total), key=lambda i: (hash_bytes(prefix+i.to_bytes(8, 'big')).digest(), i))
        symbols = [None]*total
        start = 0
        for element, count in zip(spec['type_elements'], assignment['counts']):
            for site in order[start:start+count]:
                symbols[site] = element
            start += count
        algorithm = 'sha256_rank_v1'
    else:
        positions = atoms.get_scaled_positions(wrap=False)[:, assignment['axis']]
        symbols = []
        for position in positions:
            if not 0 <= position < 1:
                raise StructureError('Layer site is outside the explicitly supplied fractional cell')
            layer = next(i for i, upper in enumerate(assignment['breaks'][1:]) if position < upper)
            symbols.append(assignment['elements'][layer])
        algorithm = 'fractional_half_open_layers_v1'
    atoms.set_chemical_symbols(symbols)
    return {'specification': deepcopy(assignment), 'algorithm': algorithm,
            'original_site_composition': dict(Counter(symbols)),
            'order': 'assignment_before_substitutions_before_vacancies',
            'coordinate_changes_performed': False, 'physical_evaluation_performed': False}


def validate_structure(spec, *, max_atoms=100000):
    if type(max_atoms) is not int or not 1 <= max_atoms <= 1000000:
        raise StructureError('Invalid geometry atom limit')
    if not isinstance(spec, dict):
        raise StructureError('All geometry fields must be explicit')
    explicit = spec.get('crystal') == 'explicit_cell'
    fields = EXPLICIT_FIELDS if explicit else SPEC_FIELDS
    if set(spec) not in (fields, fields | {'assignment'}):
        raise StructureError('All geometry fields must be explicit')
    if not isinstance(spec['crystal'], str) or spec['crystal'] not in {*CELL_ATOMS, 'explicit_cell'}:
        raise StructureError('Unsupported crystal builder')
    repeat = spec['repeat']
    if not isinstance(repeat, list) or len(repeat) != 3 or any(type(x) is not int or not 1 <= x <= 1000 for x in repeat):
        raise StructureError('Three positive integer cell repetitions are required')
    replicas = math.prod(repeat)
    if explicit:
        basis_count = _explicit_cell(spec, max_atoms // replicas)
    else:
        if spec['orientation'] != 'cubic_axes':
            raise StructureError('This builder requires conventional cubic [100], [010], [001] axes')
        elements = spec['elements']
        count = 2 if spec['crystal'] in {'rocksalt', 'zincblende'} else 1
        if (not isinstance(elements, list) or len(elements) != count
                or any(not isinstance(x, str) or x not in ELEMENTS for x in elements)):
            raise StructureError('Invalid crystal species')
        if not _positive(spec['a_angstrom']) or spec['a_angstrom'] > 1000:
            raise StructureError('Explicit finite lattice constant in angstrom is required')
        basis_count = CELL_ATOMS[spec['crystal']]
    total = basis_count * replicas
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
    _validate_assignment(spec, total)
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
    from ase import Atoms
    from ase.build import bulk
    from ase.io.lammpsdata import write_lammps_data
    # All lattice, composition, replication and mass choices are supplied by
    # the plan; ASE elemental reference-state defaults are not consulted.
    if spec['crystal'] == 'explicit_cell':
        atoms = Atoms(symbols=spec['site_elements'], scaled_positions=spec['scaled_positions'],
                      cell=spec['cell_angstrom'], pbc=[x == 'p' for x in spec['boundary']]).repeat(spec['repeat'])
        builder = 'ase.Atoms.explicit_cell'
    else:
        atoms = bulk(''.join(spec['elements']), crystalstructure=spec['crystal'],
                     a=spec['a_angstrom'], cubic=True).repeat(spec['repeat'])
        builder = 'ase.bulk.conventional_cubic'
    if len(atoms) != total or atoms.calc is not None:
        raise StructureError('Unexpected geometry builder result')
    assignment_receipt = _assign_composition(atoms, spec)
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
    # ASE's default skew threshold can omit a small but explicitly supplied
    # tilt. Preserve every nonzero tilt, including finite-strain perturbations.
    force_skew = spec['crystal'] == 'explicit_cell' and any(
        spec['cell_angstrom'][i][j] != 0 for i, j in ((1, 0), (2, 0), (2, 1)))
    write_lammps_data(output, atoms, specorder=spec['type_elements'], units=units,
                      atom_style='atomic', masses=True, velocities=False, reduce_cell=False,
                      force_skew=force_skew)
    data = output.getvalue().encode('ascii')
    receipt = {'schema_version': 1, 'builder': builder,
               **runtime,
               'specification_sha256': sha256(canonical(spec)), 'data_sha256': sha256(data),
               'atom_count': len(atoms), 'original_site_count': total,
               'composition': dict(Counter(symbols)), 'cell_angstrom': atoms.cell.tolist(),
               'type_elements': list(spec['type_elements']), 'masses_amu': list(spec['masses_amu']),
               'boundary': list(spec['boundary']), 'units': units, 'atom_style': 'atomic',
               'site_index_convention': 'zero_based_before_edits',
               'physical_evaluation_performed': False, 'scientifically_verified': False}
    if spec['crystal'] == 'explicit_cell':
        receipt.update(coordinate_frame='provided_restricted_triclinic',
                       automatic_rotation_performed=False, automatic_wrapping_performed=False,
                       basis_site_count=len(spec['site_elements']),
                       replication_order='x_outer_y_middle_z_inner_basis_innermost')
    if assignment_receipt is not None:
        receipt.update(schema_version=2, composition_assignment=assignment_receipt)
    return Geometry(data, receipt)
