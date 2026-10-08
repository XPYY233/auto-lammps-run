"""Frozen independent-site thermodynamics of complete relaxed substitution arrays.

This is statistics of supplied, collected E/V/N arrays, never a physics engine.
Absolute chemical potentials need an explicitly selected reservoir anchor. The
Euler enthalpy anchor is a declared approximation, not an inferred unique answer.
No target answers, executable model code, sampling or fit-window selection occur.
"""
import csv
import hashlib
import io
import math
import os
from importlib import metadata
from pathlib import Path
import re

from .analysis import AnalysisError, _finite, _name
from .manifest import canonical, private_directory, sha256
from . import runtime_launcher as runtime

VERSION = 1
FORMAT = 'site_scan_array_v1'
METHOD = 'site_thermodynamics_v1'
EQUATIONS_VERSION = 'independent_binary_sites_v1'
MAX_ARRAY_BYTES = 64 * 1024 * 1024
MAX_ARRAY_CELLS = 8_000_000
MAX_STATE_SITES = 250_000
MAX_DERIVED_BYTES = 1024 * 1024 * 1024
# Exact SI definitions; eV/angstrom^3 = 160.2176634 GPa.
KB_EV_K = 1.380649e-23 / 1.602176634e-19
GPA_A3_EV = 1e9 * 1e-30 / 1.602176634e-19
BASE_COLUMNS = [('state', '1'), ('site', '1'), ('variant', '1'), ('N', '1'),
                ('E', 'eV'), ('V', 'angstrom^3'), ('host', '1'), ('baseline', '1'),
                ('converged', '1'), ('fmax', 'eV/angstrom'), ('fnorm', 'eV/angstrom'),
                ('pressure', 'bar'), ('iterations', '1'), ('max_iterations', '1'),
                ('max_evaluations', '1')]
SOURCES = [
    'https://ocw.mit.edu/courses/8-333-statistical-mechanics-i-statistical-mechanics-of-particles-fall-2013/89259a8fd183acfaec06922c88a6024b_MIT8_333F13_Lec14.pdf',
    'https://numpy.org/doc/stable/reference/generated/numpy.logaddexp.html',
    'https://docs.lammps.org/thermo_style.html',
    'https://www.bipm.org/en/measurement-units/si-defining-constants',
]
EQUATIONS = dict(
    enthalpy='H=E+p*V; p[GPa]*V[angstrom^3]*GPA_A3_EV gives eV',
    local_difference='D_alpha=H_alpha-H_baseline; W_alpha=V_alpha-V_baseline',
    reservoir='mu_0=a-x_1*delta; mu_1=a+x_0*delta; delta=mu_1-mu_0',
    grand_cost='g_alpha=D_alpha-sum_k(delta_N_alpha,k*mu_k)',
    occupation='p_i,alpha=exp(-beta*g_i,alpha)/sum_gamma(exp(-beta*g_i,gamma))',
    composition='sum_i(p_i,1)/sum_i(p_i,0+p_i,1)=x_1; equal state/site weight',
    two_state='delta solved from species 0/1; each actual host competes only with vacancy',
    three_state='delta solved with species 0/1/vacancy all competing',
    defect_enthalpy='h_v,i=H_v,i-H_baseline,i+mu_host(i)',
    defect_volume='v_v,i=V_v,i-V_baseline,i+d(mu_host(i))/dp; fixed composition and beta',
    anchor='a=mean_state(H_baseline/N) for baseline_euler_enthalpy_v1, otherwise explicit a',
    anchor_volume='a_prime=mean_state(V_baseline/N), otherwise explicit partial volume',
    temperature='beta=1/(k_B*T); k_B=1.380649e-23/1.602176634e-19 eV/K',
)
GUIDE = '''Complete site-array thermodynamics adapter v1 (no physics execution):
Declare table {file,format:"site_scan_array_v1",columns:[{name,unit},...]}. Exact
columns are state site variant N E V host baseline converged fmax fnorm pressure
iterations max_iterations max_evaluations n_<element0> n_<element1>. Units are
1 1 1 1 eV angstrom^3 1 1 1 eV/angstrom eV/angstrom bar 1 1 1 1 1.
Baseline variant=-1/baseline=1 records the same independently relaxed state
baseline at every site; other variants have baseline=0. host is that site's
actual element variant ID. Each species/vacancy variant restores the same state
baseline independently, not the preceding mutation. Preserve all states/sites.
Operation fields: id,method:"site_thermodynamics_v1",file,equations_version:
"independent_binary_sites_v1",states:{first,last,stride},sites:{first,last,stride},
elements:{"<numeric species variant>":"<element>"},expected_counts:{"<variant>":
<positive count>},vacancy_variant:<nonnegative distinct ID>,pressure_GPa,
convergence:{force_metric:"fmax" or "fnorm",force_max_eV_per_A,
pressure_tolerance_GPa,max_iterations,max_evaluations},baseline_tolerance:
{energy_eV,volume_A3},reservoir_anchor:{method:"baseline_euler_enthalpy_v1",
source:<permitted scientific justification>} OR {method:"explicit_reference_v1",
chemical_potential_eV,partial_volume_A3,source:<permitted scientific justification>},
temperatures_K:[positive values],beta_grid:{first:<positive eV^-1>,last,count},
solver:{method:"safeguarded_newton_bisection_v1",chemical_potential_bounds_eV:
[lower,upper],composition_tolerance,chemical_potential_tolerance_eV,max_iterations},
models:[one or both of "two_state_host_vacancy","three_state_competing_species"],aggregation:
"equal_state_site_weight". Every numerical parameter and anchor choice must be
explicit in the proposal; the adapter inserts no hidden defaults. Preserve all
supplied scientific conditions. The model may propose unspecified solver bounds,
tolerances and an anchor approximation within the user's research intent, with
its rationale, assumptions and limitations declared in the proposal summary and
the exact values implemented in this operation. These are proposed choices,
never extracted paper facts, observed results or independently verified values.
The composition constrains mu_1-mu_0 only. An absolute reservoir anchor is
indispensable. The Euler enthalpy anchor explicitly neglects vibrational terms
and anchors weighted mu to the relaxed baseline enthalpy per atom; it is an
approximation, not an independently determined free energy. Explicit reference
values must have permitted provenance; do not invent explicit reference chemical
potentials or partial volumes. Missing or conflicting required scientific intent,
unsupported methods or unavailable required reference evidence need clarification;
unspecified numerical implementation parameters need not all be user-filled.
H=E+pV. Local grand costs are H_variant-H_baseline-sum(delta_N*mu).
Independent-site probabilities use stable logsumexp. The two-state vacancy model
uses binary 0/1 composition-constrained chemical potentials then host/vacancy
occupation; the three-state model solves occupied composition with 0/1/vacancy
competition. Both freeze bounds before seeing data. Finite pressure derivatives
of chemical potentials are obtained analytically from the same composition
constraint; defect volume adds the host partial volume. No interactions between
defects, vibrational entropy, confidence intervals or scientific acceptance are
inferred. Complete derived CSVs include differences, per-site temperature values,
and every beta/temperature curve row. JSON retains bounded summaries/receipts.
Complete arrays: at most 250000 state-sites, 32 columns, 64MiB, 8000000 cells;
at most 64 temperatures and 10000 beta points. These bounds never authorize HPC.
General mathematical basis: MIT Statistical Mechanics I Lecture 14, IV.71-77
(independent two-level partition), IV.85-93 (pressure/enthalpy), IV.99-104 (grand
canonical probabilities). Binary species/site specialization and Euler enthalpy
anchor are explicit adapter assumptions, not claims that these notes determine
an alloy's chemical potentials. NumPy logaddexp, LAMMPS thermo_style and exact
BIPM SI defining constants are recorded in identity. No paper-specific equations,
elements, system size, state count, temperatures, beta extent or target answers
are supplied by this general method.
'''


def adapter_identity():
    try:
        numpy_version = metadata.version('numpy')
    except metadata.PackageNotFoundError:
        numpy_version = None
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                numpy_version=numpy_version, equations_version=EQUATIONS_VERSION,
                equations_sha256=sha256(canonical(EQUATIONS)), sources=SOURCES,
                k_B_eV_K=KB_EV_K, GPa_A3_eV=GPA_A3_EV)


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise AnalysisError('Application NumPy runtime is required for complete site arrays') from exc
    return np


def validate_table(table):
    if (not isinstance(table, dict) or set(table) != {'file', 'format', 'columns'}
            or table.get('format') != FORMAT or not isinstance(table.get('file'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', table['file'])):
        raise AnalysisError('Declare a complete site_scan_array_v1 source')
    columns = table['columns']
    if not isinstance(columns, list) or not 17 <= len(columns) <= 32:
        raise AnalysisError('Site array requires complete scalar and binary composition columns')
    for column in columns:
        if not isinstance(column, dict) or set(column) != {'name', 'unit'}:
            raise AnalysisError('Declare exact site-array columns and units')
        _name(column['name'])
    if [(c['name'], c['unit']) for c in columns[:15]] != BASE_COLUMNS:
        raise AnalysisError('Site-array metadata, units or column order differ from v1')
    if len(columns) != 17 or any(not re.fullmatch(r'n_[A-Z][a-z]?', c['name'])
                                or c['unit'] != '1' for c in columns[15:]):
        raise AnalysisError('Binary site array requires two exact n_Element count columns')
    if len({c['name'] for c in columns}) != len(columns):
        raise AnalysisError('Duplicate site-array columns')
    return table


def _domain(value, label):
    if (not isinstance(value, dict) or set(value) != {'first', 'last', 'stride'}
            or any(type(v) is not int or not 0 <= v <= 10**7 for v in value.values())
            or value['stride'] < 1 or value['last'] < value['first']
            or (value['last'] - value['first']) % value['stride']):
        raise AnalysisError('Declare a complete integer ' + label + ' domain')
    return range(value['first'], value['last'] + 1, value['stride'])


def _positive(value, label, zero=False):
    _finite(value)
    if value < 0 or (not zero and value == 0):
        raise AnalysisError('Declare positive ' + label)
    return value


OP_FIELDS = {'id', 'method', 'file', 'equations_version', 'states', 'sites', 'elements',
             'expected_counts', 'vacancy_variant', 'pressure_GPa', 'convergence',
             'baseline_tolerance', 'reservoir_anchor', 'temperatures_K', 'beta_grid',
             'solver', 'models', 'aggregation'}


def validate_operation(operation, table):
    validate_table(table)
    if (not isinstance(operation, dict) or set(operation) != OP_FIELDS
            or operation['method'] != METHOD or operation['file'] != table['file']
            or operation['equations_version'] != EQUATIONS_VERSION):
        raise AnalysisError('Freeze the explicit versioned site thermodynamics equations and parameters')
    _name(operation['id'])
    states, sites = _domain(operation['states'], 'state'), _domain(operation['sites'], 'site')
    if len(states) * len(sites) > MAX_STATE_SITES:
        raise AnalysisError('Complete site-array extent exceeds the supported limit')
    elements, counts = operation['elements'], operation['expected_counts']
    if (not isinstance(elements, dict) or len(elements) != 2
            or any(not isinstance(k, str) or not re.fullmatch(r'0|[1-9][0-9]{0,5}', k)
                   or not isinstance(v, str) or not re.fullmatch(r'[A-Z][a-z]?', v)
                   for k, v in elements.items()) or len(set(elements.values())) != 2
            or not isinstance(counts, dict) or set(counts) != set(elements)
            or any(type(v) is not int or v <= 0 for v in counts.values())
            or sum(counts.values()) != len(sites)):
        raise AnalysisError('Freeze binary elements and exact counts covering all sites')
    if {c['name'] for c in table['columns'][15:]} != {'n_' + v for v in elements.values()}:
        raise AnalysisError('Site-array element count columns differ from the frozen element mapping')
    vacancy = operation['vacancy_variant']
    if type(vacancy) is not int or not 0 <= vacancy <= 10**6 or str(vacancy) in elements:
        raise AnalysisError('Declare a distinct vacancy variant ID')
    _finite(operation['pressure_GPa'])
    convergence = operation['convergence']
    if (not isinstance(convergence, dict) or set(convergence) != {'force_metric', 'force_max_eV_per_A',
            'pressure_tolerance_GPa', 'max_iterations', 'max_evaluations'}
            or convergence['force_metric'] not in {'fmax', 'fnorm'}
            or any(type(convergence[k]) is not int or not 1 <= convergence[k] <= 10**9
                   for k in ('max_iterations', 'max_evaluations'))):
        raise AnalysisError('Freeze force/pressure convergence evidence and minimization bounds')
    _positive(convergence['force_max_eV_per_A'], 'force tolerance')
    _positive(convergence['pressure_tolerance_GPa'], 'pressure tolerance', zero=True)
    tolerance = operation['baseline_tolerance']
    if not isinstance(tolerance, dict) or set(tolerance) != {'energy_eV', 'volume_A3'}:
        raise AnalysisError('Freeze baseline repeat tolerances')
    for value in tolerance.values():
        _positive(value, 'baseline tolerance', zero=True)
    anchor = operation['reservoir_anchor']
    if not isinstance(anchor, dict) or anchor.get('method') not in {
            'baseline_euler_enthalpy_v1', 'explicit_reference_v1'}:
        raise AnalysisError('An explicit absolute reservoir anchor is required')
    fields = {'method', 'source'} | ({'chemical_potential_eV', 'partial_volume_A3'}
                                     if anchor['method'] == 'explicit_reference_v1' else set())
    if (set(anchor) != fields or not isinstance(anchor['source'], str)
            or not 1 <= len(anchor['source']) <= 2048 or any(ord(c) < 32 for c in anchor['source'])):
        raise AnalysisError('Reservoir anchor requires an explicit permitted scientific justification')
    if anchor['method'] == 'explicit_reference_v1':
        _finite(anchor['chemical_potential_eV']); _finite(anchor['partial_volume_A3'])
    temperatures = operation['temperatures_K']
    if not isinstance(temperatures, list) or not 1 <= len(temperatures) <= 64:
        raise AnalysisError('Declare one to sixty-four exact temperatures')
    for t in temperatures:
        _positive(t, 'temperature')
    if len(set(temperatures)) != len(temperatures):
        raise AnalysisError('Duplicate temperatures')
    grid = operation['beta_grid']
    if (not isinstance(grid, dict) or set(grid) != {'first', 'last', 'count'}
            or type(grid['count']) is not int or not 2 <= grid['count'] <= 10000):
        raise AnalysisError('Declare a complete two to ten-thousand point beta grid')
    _positive(grid['first'], 'beta'); _positive(grid['last'], 'beta')
    if grid['first'] >= grid['last']:
        raise AnalysisError('Beta grid must increase')
    solver = operation['solver']
    if (not isinstance(solver, dict) or set(solver) != {'method', 'chemical_potential_bounds_eV',
            'composition_tolerance', 'chemical_potential_tolerance_eV', 'max_iterations'}
            or solver['method'] != 'safeguarded_newton_bisection_v1'
            or type(solver['max_iterations']) is not int or not 1 <= solver['max_iterations'] <= 256):
        raise AnalysisError('Freeze a bounded root solver and explicit tolerances')
    bounds = solver['chemical_potential_bounds_eV']
    if (not isinstance(bounds, list) or len(bounds) != 2
            or _finite(bounds[0]) >= _finite(bounds[1])):
        raise AnalysisError('Freeze increasing chemical potential difference bounds')
    _positive(solver['composition_tolerance'], 'composition tolerance')
    _positive(solver['chemical_potential_tolerance_eV'], 'chemical potential tolerance')
    if solver['composition_tolerance'] >= 1:
        raise AnalysisError('Composition tolerance must be less than one')
    models = operation['models']
    if (not isinstance(models, list) or not 1 <= len(models) <= 2
            or any(m not in {'two_state_host_vacancy', 'three_state_competing_species'} for m in models)
            or len(set(models)) != len(models)
            or operation['aggregation'] != 'equal_state_site_weight'):
        raise AnalysisError('Declare independent-site models and equal state/site weighting')
    if reservation_bytes(dict(operations=[operation])) > MAX_DERIVED_BYTES:
        raise AnalysisError('Complete derived tables exceed the supported storage limit')
    return operation


def reservation_bytes(plan):
    """Conservative ASCII CSV bound; caller reserves this before any writing."""
    total = 65536
    for op in plan['operations']:
        if op.get('method') != METHOD:
            continue
        n = len(_domain(op['states'], 'state')) * len(_domain(op['sites'], 'site'))
        t, b = len(op['temperatures_K']), op['beta_grid']['count']
        total += 3*8192 + 3*n*512 + t*2*n*768 + (t+b)*2*768
    return total


def parse_array(data, table):
    """Parse every row and preserve exact one-based original source line numbers."""
    validate_table(table)
    if not isinstance(data, bytes) or len(data) > MAX_ARRAY_BYTES or not data.endswith(b'\n'):
        raise AnalysisError('Complete site array exceeds byte limit or has a truncated final line')
    try:
        content = data.decode('ascii')
    except UnicodeDecodeError as exc:
        raise AnalysisError('Site array must be ASCII') from exc
    lines = content.splitlines()
    expected = ['# columns: ' + ' '.join(c['name'] for c in table['columns']),
                '# units: ' + ' '.join(c['unit'] for c in table['columns'])]
    if lines[:2] != expected:
        raise AnalysisError('Site-array headers or units differ from the frozen plan')
    count = len(lines) - 2
    if count < 1 or count * len(table['columns']) > MAX_ARRAY_CELLS:
        raise AnalysisError('Complete site array exceeds cell limit or is empty')
    np = _numpy()
    # Reject comments, ragged rows and partial numeric tokens before fromstring.
    for line in lines[2:]:
        tokens = line.split()
        if len(line) > 4096 or len(tokens) != len(table['columns']) or any(
                not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', t)
                for t in tokens):
            raise AnalysisError('Malformed or nonnumeric site-array row')
    values = np.fromstring('\n'.join(lines[2:]), dtype=np.float64, sep=' ')
    values = values.reshape(count, len(table['columns']))
    if not np.isfinite(values).all():
        raise AnalysisError('Site arrays require finite values')
    return values, np.arange(3, count + 3, dtype=np.int64)


def validate_array(values, lines, table, operation):
    """No duplicates, omissions, extrapolated sites or inconsistent compositions."""
    validate_operation(operation, table)
    np = _numpy()
    names = [c['name'] for c in table['columns']]
    column = {k: names.index(k) for k in names}
    states, sites = list(_domain(operation['states'], 'state')), list(_domain(operation['sites'], 'site'))
    species = sorted(map(int, operation['elements']))
    variants = [-1] + species + [operation['vacancy_variant']]
    n, width = len(states)*len(sites), len(variants)
    if values.shape != (n*width, len(names)) or len(lines) != n*width:
        raise AnalysisError('Site array is missing the complete state/site/variant domain')
    integers = ['state', 'site', 'variant', 'N', 'host', 'baseline', 'converged', 'iterations',
                'max_iterations', 'max_evaluations'] + names[15:]
    for key in integers:
        a = values[:, column[key]]
        if np.any(a != np.floor(a)) or np.any(np.abs(a) > 2**53-1):
            raise AnalysisError('Site-array identities, counts and bounds must be exact integers')
    # Map exact declared domain members without guessing rows from their order.
    def indexed(key, domain):
        array = values[:, column[key]].astype(np.int64)
        index = np.searchsorted(domain, array)
        if np.any(index >= len(domain)) or np.any(np.asarray(domain)[np.minimum(index, len(domain)-1)] != array):
            raise AnalysisError('Unexpected site-array ' + key + ' identity')
        return index
    si, li = indexed('state', states), indexed('site', sites)
    sorted_variants = sorted(variants)
    vi = indexed('variant', sorted_variants)
    keys = (si*len(sites)+li)*width+vi
    order = np.argsort(keys)
    if not np.array_equal(keys[order], np.arange(n*width)):
        raise AnalysisError('Duplicate or missing state/site/variant tuple')
    array, source_lines = values[order].reshape(n, width, len(names)), lines[order].reshape(n, width)
    pos = {v: sorted_variants.index(v) for v in variants}
    base = array[:, pos[-1]]
    if np.any(base[:, column['baseline']] != 1) or np.any(array[:, [pos[v] for v in variants[1:]], column['baseline']] != 0):
        raise AnalysisError('Each site requires one explicit baseline and each independent variant')
    if np.any(array[:, :, column['converged']] != 1):
        raise AnalysisError('Unconverged site or baseline cannot enter thermodynamics')
    conv = operation['convergence']
    if (np.any(array[:, :, column[conv['force_metric']]] > conv['force_max_eV_per_A'])
            or np.any(array[:, :, column['fmax']] < 0) or np.any(array[:, :, column['fnorm']] < 0)
            or np.any(np.abs(array[:, :, column['pressure']]*1e-4-operation['pressure_GPa'])
                      > conv['pressure_tolerance_GPa'])
            or np.any(array[:, :, column['max_iterations']] != conv['max_iterations'])
            or np.any(array[:, :, column['max_evaluations']] != conv['max_evaluations'])
            or np.any(array[:, :, column['iterations']] < 0)
            or np.any(array[:, :, column['iterations']] > conv['max_iterations'])):
        raise AnalysisError('Declared convergence flag conflicts with frozen force/pressure or iteration evidence')
    if np.any(array[:, :, column['V']] <= 0):
        raise AnalysisError('Relaxed cell volume must be positive')
    hosts = base[:, column['host']].astype(np.int64)
    if not np.isin(hosts, species).all() or np.any(array[:, :, column['host']] != hosts[:, None]):
        raise AnalysisError('Host identity must remain the original site element for all variants')
    for state_index in range(len(states)):
        part = base[state_index*len(sites):(state_index+1)*len(sites)]
        tol = operation['baseline_tolerance']
        if (np.ptp(part[:, column['E']]) > tol['energy_eV']
                or np.ptp(part[:, column['V']]) > tol['volume_A3']):
            raise AnalysisError('Site variants do not share the same relaxed state baseline')
        for variant in species:
            if np.count_nonzero(hosts[state_index*len(sites):(state_index+1)*len(sites)] == variant) != operation['expected_counts'][str(variant)]:
                raise AnalysisError('Complete host identities differ from the frozen state composition')
    counts_columns = [column['n_'+operation['elements'][str(v)]] for v in species]
    baseline_counts = np.array([operation['expected_counts'][str(v)] for v in species])
    if np.any(base[:, counts_columns] != baseline_counts) or np.any(base[:, column['N']] != len(sites)):
        raise AnalysisError('Baseline N or composition differs from all occupied lattice sites')
    host_index = np.searchsorted(species, hosts)
    delta_counts = np.empty((n, 3, 2), dtype=np.float64)
    for j, variant in enumerate(species + [operation['vacancy_variant']]):
        row = array[:, pos[variant]]
        expected = np.broadcast_to(baseline_counts, (n, 2)).copy()
        expected[np.arange(n), host_index] -= 1
        if variant in species:
            expected[:, species.index(variant)] += 1
        if np.any(row[:, counts_columns] != expected) or np.any(row[:, column['N']] != expected.sum(axis=1)):
            raise AnalysisError('Variant N/composition is inconsistent with replacing the original site once')
        delta_counts[:, j] = expected-baseline_counts
    selected = array[:, [pos[v] for v in species + [operation['vacancy_variant']]]]
    de = selected[:, :, column['E']]-base[:, None, column['E']]
    dv = selected[:, :, column['V']]-base[:, None, column['V']]
    # Unchanged host variant must reproduce its own independent baseline within
    # explicit frozen tolerances, rather than silently renormalizing bad output.
    if (np.any(np.abs(de[np.arange(n), host_index]) > operation['baseline_tolerance']['energy_eV'])
            or np.any(np.abs(dv[np.arange(n), host_index]) > operation['baseline_tolerance']['volume_A3'])):
        raise AnalysisError('Independently relaxed host variant does not reproduce the frozen baseline')
    enthalpy = base[:, column['E']]+operation['pressure_GPa']*GPA_A3_EV*base[:, column['V']]
    return dict(de=de, dv=dv, dh=de+operation['pressure_GPa']*GPA_A3_EV*dv,
                delta_counts=delta_counts, host_index=host_index, source_lines=source_lines,
                variant_lines=source_lines[:, [pos[v] for v in species + [operation['vacancy_variant']]]],
                baseline_lines=source_lines[:, pos[-1]], species=species,
                state_ids=np.repeat(states, len(sites)), site_ids=np.tile(sites, len(states)),
                anchor_enthalpy=float(np.mean(enthalpy/len(sites))),
                anchor_volume=float(np.mean(base[:, column['V']]/len(sites))),
                coverage=dict(states=len(states), sites=len(sites), variants=3,
                              rows=n*width, baseline_rows=n, complete=True))


def probabilities(costs, beta):
    np = _numpy()
    array = np.asarray(costs, dtype=np.float64)
    # A site's common energy is arbitrary. Remove it before beta multiplication
    # to preserve probabilities under large shifts and reduce cancellation.
    logits = -beta * (array-np.min(array, axis=1)[:, None])
    if not np.isfinite(logits).all():
        raise AnalysisError('Grand-canonical costs overflow at the frozen beta')
    logs = logits-np.logaddexp.reduce(logits, axis=1)[:, None]
    return np.exp(logs), logs


def _constraint(costs, q, beta, target, delta, volume_cost=None):
    np = _numpy()
    p, logs = probabilities(costs-delta*q, beta)
    # Log total occupancies avoid accepting a false root when all occupied
    # probabilities underflow in a vacancy-dominated reservoir.
    l0 = np.logaddexp.reduce(logs[:, 0]); l1 = np.logaddexp.reduce(logs[:, 1])
    w0, w1 = np.exp(logs[:, 0]-l0), np.exp(logs[:, 1]-l1)
    f = float(l1-l0-math.log(target/(1-target)))
    mean_q = p@q
    derivative = float(beta*(q[1]-q[0]-(w1@mean_q-w0@mean_q)))
    ratio = float(math.exp(-float(np.logaddexp(0., -(l1-l0)))))
    volume_derivative = None
    if volume_cost is not None:
        mean_v = np.sum(p*volume_cost, axis=1)
        # derivative dF/dp (pressure expressed in eV/angstrom^3).
        volume_derivative = float(-beta*(w1@(volume_cost[:, 1]-mean_v)
                                         -w0@(volume_cost[:, 0]-mean_v)))
    return f, derivative, ratio, p, volume_derivative


def solve_composition(costs, q, beta, target, solver, initial=None, volume_cost=None):
    """Safeguarded Newton with a finite bracket; never expand frozen bounds."""
    low, high = solver['chemical_potential_bounds_eV']
    delta = initial if initial is not None and low < initial < high else (low+high)/2
    first = _constraint(costs, q, beta, target, delta, volume_cost)
    bracketed = False
    for iteration in range(1, solver['max_iterations']+1):
        f, derivative, ratio, p, fp = first if iteration == 1 else _constraint(costs, q, beta, target, delta, volume_cost)
        if abs(ratio-target) <= solver['composition_tolerance'] and abs(f) <= solver['composition_tolerance']/max(target*(1-target), 1e-15):
            if volume_cost is not None and (not math.isfinite(derivative) or derivative <= 0):
                raise AnalysisError('Composition pressure derivative is singular at the frozen root')
            return dict(delta=delta, delta_volume=(-fp/derivative if fp is not None else None),
                        probabilities=p, residual=ratio-target, iterations=iteration)
        if not bracketed:
            flo = _constraint(costs, q, beta, target, low)[0]
            fhi = _constraint(costs, q, beta, target, high)[0]
            if flo > 0 or fhi < 0:
                raise AnalysisError('Composition root is outside frozen chemical potential bounds')
            bracketed = True
        if f < 0:
            low = delta
        else:
            high = delta
        if high-low <= solver['chemical_potential_tolerance_eV']:
            raise AnalysisError('Bounded composition solver exhausted precision before residual tolerance')
        proposal = delta-f/derivative if math.isfinite(derivative) and derivative > 0 else math.nan
        delta = proposal if math.isfinite(proposal) and low < proposal < high else (low+high)/2
    raise AnalysisError('Composition solver did not converge within the frozen iteration bound')


def evaluate(arrays, operation, beta, model, initial=None):
    np = _numpy()
    species = arrays['species']
    counts = operation['expected_counts']
    target = counts[str(species[1])]/sum(counts.values())
    anchor = operation['reservoir_anchor']
    a = arrays['anchor_enthalpy'] if anchor['method'] == 'baseline_euler_enthalpy_v1' else anchor['chemical_potential_eV']
    av = arrays['anchor_volume'] if anchor['method'] == 'baseline_euler_enthalpy_v1' else anchor['partial_volume_A3']
    coefficients = np.array([-target, 1-target])
    dc = arrays['delta_counts']
    q = dc@coefficients
    base_costs = arrays['dh']-dc.sum(axis=2)*a
    volume_costs = arrays['dv']-dc.sum(axis=2)*av
    # q differs by the host-specific common term. Subtract it for the scalar
    # constraint's derivative; probabilities are invariant to common site shifts.
    expected_q = np.array([-target, 1-target, 0.])
    width = 2 if model == 'two_state_host_vacancy' else 3
    shift = q[:, :width]-expected_q[:width]
    if not np.allclose(shift, shift[:, :1], rtol=0, atol=1e-14):
        raise AnalysisError('Variant stoichiometry is incompatible with the frozen binary model')
    solution = solve_composition(base_costs[:, :width], expected_q[:width], beta, target,
        operation['solver'], initial=initial, volume_cost=volume_costs[:, :width])
    mu = a+coefficients*solution['delta']
    mu_volume = av+coefficients*solution['delta_volume']
    costs = arrays['dh']-np.sum(dc*mu, axis=2)
    if model == 'two_state_host_vacancy':
        hosts = arrays['host_index']
        hcost = costs[np.arange(len(hosts)), hosts]
        pair, _ = probabilities(np.column_stack((hcost, costs[:, 2])), beta)
        p = np.zeros_like(costs)
        p[np.arange(len(hosts)), hosts] = pair[:, 0]; p[:, 2] = pair[:, 1]
    else:
        p, _ = probabilities(costs, beta)
    hv = arrays['dh'][:, 2]+mu[arrays['host_index']]
    vv = arrays['dv'][:, 2]+mu_volume[arrays['host_index']]
    summary = dict(model=model, beta_eV_inverse=float(beta), temperature_K=1/(KB_EV_K*beta),
        mu_0_eV=float(mu[0]), mu_1_eV=float(mu[1]),
        partial_volume_0_A3=float(mu_volume[0]), partial_volume_1_A3=float(mu_volume[1]),
        vacancy_fraction=float(np.mean(p[:, 2])),
        mean_vacancy_enthalpy_eV=float(np.mean(hv)), mean_vacancy_volume_A3=float(np.mean(vv)),
        composition_residual=solution['residual'], solver_iterations=solution['iterations'])
    if any(not math.isfinite(v) for v in summary.values() if isinstance(v, (int, float))):
        raise AnalysisError('Nonfinite derived thermodynamic statistic')
    return summary, p, hv, vv, solution['delta']


class _CSV:
    """Exclusive, bounded, content-addressed publication in an accounted folder."""
    def __init__(self, destination, name, columns, limit):
        self.path = destination/name
        self.columns, self.limit, self.rows, self.bytes = columns, limit, 0, 0
        # Create a fresh temporary name; final deterministic name is only
        # published after complete numerical success and content hashing.
        import tempfile
        fd, name = tempfile.mkstemp(prefix='.derived-', dir=destination)
        self.temporary = Path(name)
        self.stream = os.fdopen(fd, 'wb')
        self.checksum = hashlib.sha256()
        self._write([c['name'] for c in columns])

    def _write(self, row):
        text = io.StringIO(newline='')
        csv.writer(text, lineterminator='\n').writerow(row)
        raw = text.getvalue().encode('ascii')
        if self.bytes+len(raw) > self.limit:
            raise AnalysisError('Complete derived table exceeds its reserved byte bound')
        self.stream.write(raw); self.checksum.update(raw); self.bytes += len(raw)

    def write(self, row):
        if len(row) != len(self.columns):
            raise AnalysisError('Derived table differs from its declared columns')
        for value in row:
            if type(value) is float and not math.isfinite(value):
                raise AnalysisError('Nonfinite derived table value')
        self._write(row); self.rows += 1

    def publish(self, source_sha256, parameters_sha256):
        self.stream.flush(); os.fsync(self.stream.fileno()); self.stream.close()
        receipt = dict(name=self.path.name, size=self.bytes, sha256=self.checksum.hexdigest(),
                       rows=self.rows, columns=self.columns, source_sha256=source_sha256,
                       parameters_sha256=parameters_sha256)
        try:
            os.link(self.temporary, self.path)
        except FileExistsError:
            # A process may have published before the bounded report was saved.
            # Resume only byte-identical content; never replace prior artifacts.
            existing = runtime.read_regular(self.path, self.limit, private=True)
            if len(existing) != self.bytes or sha256(existing) != receipt['sha256']:
                raise AnalysisError('Existing derived table differs from the frozen calculation')
        self.temporary.unlink()
        return receipt

    def abort(self):
        if not self.stream.closed:
            self.stream.close()
        self.temporary.unlink(missing_ok=True)


def _columns(pairs):
    return [dict(name=n, unit=u) for n, u in pairs]


def analyze(data, table, operation, source, destination):
    """Calculate full arrays and publish bounded CSVs under the service reservation."""
    validate_operation(operation, table)
    if source.get('sha256') != sha256(data) or source.get('size') != len(data):
        raise AnalysisError('Site-array source differs from its collection receipt')
    values, lines = parse_array(data, table)
    arrays = validate_array(values, lines, table, operation)
    del values, lines
    destination = private_directory(destination)
    np = _numpy()
    local_n = len(arrays['host_index']); temps = operation['temperatures_K']; grid = operation['beta_grid']
    columns = dict(
        differences=_columns([('state', '1'), ('site', '1'), ('variant', '1'), ('host', '1'),
            ('delta_E', 'eV'), ('delta_V', 'angstrom^3'), ('delta_H', 'eV'),
            ('delta_N_0', '1'), ('delta_N_1', '1'), ('baseline_source_line', '1'), ('variant_source_line', '1')]),
        sites=_columns([('state', '1'), ('site', '1'), ('host', '1'), ('model', '1'),
            ('temperature', 'K'), ('beta', '1/eV'), ('p_0', '1'), ('p_1', '1'), ('p_vacancy', '1'),
            ('vacancy_enthalpy', 'eV'), ('vacancy_volume', 'angstrom^3'),
            ('baseline_source_line', '1'), ('vacancy_source_line', '1')]),
        curves=_columns([('grid', '1'), ('index', '1'), ('model', '1'), ('beta', '1/eV'),
            ('temperature', 'K'), ('mu_0', 'eV'), ('mu_1', 'eV'), ('partial_volume_0', 'angstrom^3'),
            ('partial_volume_1', 'angstrom^3'), ('vacancy_fraction', '1'),
            ('mean_vacancy_enthalpy', 'eV'), ('mean_vacancy_volume', 'angstrom^3'),
            ('composition_residual', '1'), ('solver_iterations', '1')]))
    limits = dict(differences=8192+3*local_n*512, sites=8192+len(temps)*2*local_n*768,
                  curves=8192+(len(temps)+grid['count'])*2*768)
    writers = {k: _CSV(destination, operation['id']+'-'+k+'.csv', columns[k], limits[k]) for k in columns}
    summaries, receipts = [], []
    try:
        variants = arrays['species']+[operation['vacancy_variant']]
        for i in range(local_n):
            host = arrays['species'][arrays['host_index'][i]]
            for j, variant in enumerate(variants):
                writers['differences'].write([int(arrays['state_ids'][i]), int(arrays['site_ids'][i]), variant, host,
                    *map(float, (arrays['de'][i, j], arrays['dv'][i, j], arrays['dh'][i, j])),
                    *map(int, arrays['delta_counts'][i, j]), int(arrays['baseline_lines'][i]),
                    int(arrays['variant_lines'][i, j])])
        for model in operation['models']:
            initial = None
            for kind, betas in [('temperature', [1/(KB_EV_K*t) for t in temps]),
                                ('beta', np.linspace(grid['first'], grid['last'], grid['count']))]:
                for index, beta in enumerate(betas):
                    summary, p, hv, vv, initial = evaluate(arrays, operation, float(beta), model, initial)
                    writers['curves'].write([kind, index, model, summary['beta_eV_inverse'], summary['temperature_K'],
                        summary['mu_0_eV'], summary['mu_1_eV'], summary['partial_volume_0_A3'],
                        summary['partial_volume_1_A3'], summary['vacancy_fraction'],
                        summary['mean_vacancy_enthalpy_eV'], summary['mean_vacancy_volume_A3'],
                        summary['composition_residual'], summary['solver_iterations']])
                    if kind == 'temperature':
                        summaries.append(summary)
                        for i in range(local_n):
                            writers['sites'].write([int(arrays['state_ids'][i]), int(arrays['site_ids'][i]),
                                arrays['species'][arrays['host_index'][i]], model, temps[index], float(beta),
                                *map(float, p[i]), float(hv[i]), float(vv[i]), int(arrays['baseline_lines'][i]),
                                int(arrays['variant_lines'][i, 2])])
        parameters_sha256 = sha256(canonical(operation))
        receipts = [writers[k].publish(source['sha256'], parameters_sha256) for k in columns]
    finally:
        for writer in writers.values():
            writer.abort()
    result = dict(id=operation['id'], method=METHOD, file=table['file'], parameters=operation,
        source=source, adapter_identity=adapter_identity(), coverage=arrays['coverage'], equations=EQUATIONS,
        temperature_summaries=summaries, derived_files=receipts, scientific_status='not_evaluated',
        physics_simulation=False, assumptions=[
            'Independent noninteracting sites; relaxed enthalpies substitute for vibrational free energies.',
            'The explicitly selected reservoir anchor is an assumption; composition alone fixes only a difference.',
            'Equal weight of all states/sites; no independent-replicate uncertainty or scientific score is inferred.',
            'converged is the frozen force/pressure diagnostic; minimizer stopping reason is not inferred from iteration count.',
            'max_evaluations is a minimizer bound, not an observed evaluation count; original logs remain the evidence.'])
    result['derived_sha256'] = sha256(canonical(result))
    return result


def read_derived(destination, receipt):
    """Read only one explicitly declared, immutable, bounded CSV download."""
    if (not isinstance(receipt, dict) or set(receipt) != {'name', 'size', 'sha256', 'rows',
            'columns', 'source_sha256', 'parameters_sha256'}
            or not isinstance(receipt['name'], str)
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}-(differences|sites|curves)\.csv', receipt['name'])
            or type(receipt['size']) is not int or not 0 <= receipt['size'] <= MAX_DERIVED_BYTES
            or type(receipt['rows']) is not int or receipt['rows'] < 1):
        raise AnalysisError('Invalid derived table receipt')
    for key in ('sha256', 'source_sha256', 'parameters_sha256'):
        runtime.hash_value(receipt[key])
    data = runtime.read_regular(Path(destination)/receipt['name'], receipt['size'], private=True)
    if len(data) != receipt['size'] or sha256(data) != receipt['sha256']:
        raise AnalysisError('Derived table changed after analysis publication')
    return data
