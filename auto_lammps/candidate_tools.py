"""Deterministic plan tools. No engine execution, invented data or scientific defaults."""
import re
import shlex
import math
import json
from pathlib import Path
from .manifest import sha256

VERSION = 5
MAX_CYCLES = 1000000
MAX_CYCLE_BLOCKS = 64
MAX_SAMPLE_TYPES = 64
MAX_SAVED_STATES = 256
MAX_SCAN_ATOMS = 100000
MAX_SCAN_EVALUATIONS = 500000
MAX_SCAN_TYPES = 16
SCAN_FORMAT = 'site_scan_array_v1'
_SAFE_ID = r'[A-Za-z][A-Za-z0-9_]{0,23}'
_RAW_CONTROL = {'label', 'next', 'jump', 'clear', 'include', 'shell', 'if'}
_CYCLE_STATE_RESETS = {'load_structure', 'reset_structure', 'delete_atoms',
                       'change_box', 'reset_timestep', 'displace_atoms'}

GUIDE = '''Adapter capabilities (version 5):
begin_cycle <safe_id> <positive_literal_count> / end_cycle <same_id> declares a
finite, non-nested scientific cycle. At most 64 sequential blocks, at most 1000000
iterations per block; these are technical limits, NOT additional resource authority.
Every run/minimize in the body is counted count times. Explicitly write each physical
stage, fix and unfix; the tool adds no MD integration, MC attempts, retry or defaults.
With cycles, run takes a nonnegative literal step count and optional pre/post yes/no;
no run every, upto or variable step count. Minimize in cycles has four literal finite
arguments and positive iteration/evaluation bounds; next() expressions are forbidden.
The adapter alone emits loop/label/next/jump; never write those native controls,
clear/include/shell/if or reserved __alr_ identifiers. Cycle bodies may not reload or
reset structures, delete atoms, change/reset boxes or timesteps, or overwrite files.
Declare computes and dumps before a cycle, clean them up after its end; their existing
sampling continues during its runs. No compute/uncompute/undump inside a cycle.
emit_table headers are placed once before the cycle, then each declared row appends.
sample_swap_types <safe_prefix> <literal_type_count> <positive_literal_seed> may
appear once per prefix, inside a cycle. It freezes ${prefix_i} and ${prefix_j} as two
distinct uniformly sampled type IDs from 1..type_count for that iteration; the declared
count must match ALL configured atom types. Use these exact variables as the MC pair.
The native fix grammar still requires the literal types keyword BEFORE those variables:
fix exchange all atom/swap 1 10 17311 450.0 types ${pair_i} ${pair_j} ke yes
Here pair is a prefix previously declared by sample_swap_types in this SAME cycle.
Numbers in this syntax example are illustrative, not task defaults; obtain all scientific
parameters from confirmed conditions, and use a unique sampler prefix for each cycle.
They are local to their cycle, must not be redefined/deleted manually and are deleted
by the adapter at its end. All samplers in one workflow must use the SAME seed; native
random()/normal() expressions cannot be mixed with this tool. LAMMPS equal-style RNG
is a shared stream initialized only once. Later cycles/clear do not establish an
independent new seed, and the tool does not promise independent temperature streams.
Selection uses two draws and skips the first ID algebraically, never rejection/retry.
Input must execute with the existing -in file launcher, not stdin: jump SELF needs a
rewindable input file. Old-engine semantics verified against official 28Mar2023 docs:
https://raw.githubusercontent.com/lammps/lammps/patch_28Mar2023/doc/src/variable.rst
https://raw.githubusercontent.com/lammps/lammps/patch_28Mar2023/doc/src/next.rst
https://raw.githubusercontent.com/lammps/lammps/patch_28Mar2023/doc/src/jump.rst
https://raw.githubusercontent.com/lammps/lammps/patch_28Mar2023/doc/src/clear.rst
emit_table <declared-basename> "<one numerical row with LAMMPS substitutions>" writes a
table declared in analysis.plan.tables. The adapter supplies its exact columns/units
headers once, then appends rows. Use this tool for labeled numeric tables instead of
manually writing headers. The payload must have one whitespace-separated scalar per
column, with no labels or units. Choose expressions from the physical calculation;
never put expected answers into the row. The adapter never computes physics locally.
capture <variable_name> <equal-style-expression> freezes a scalar evaluated at that
stage, before another minimization/deletion changes it. It compiles to variable name
equal $(expression). Formula whitespace is preserved; ordinary equal formulas are quoted, while immediate
$(...) expressions stay unquoted so substitution occurs;
the adapter never changes variable names, operators, values or stages. Refer to saved variables as v_name inside formulas and $(...),
or ${name} in output. Bare variable names inside $(...) are not valid thermo keywords.
Use fnorm/fmax thermo keywords for force diagnostics, not max(all,fx).
Conventional cubic cells contain bcc=2, fcc=4, diamond=8 atoms; count(all) gives the actual current atom count. Do not confuse cell count with atom count.
minimize changes atoms only; box relaxation requires an active fix box/relax, and
unfix before a later fixed-box stage. vmax if specified must be strictly positive. Do not use box/relax with vmax=0 for a fixed-box stage; omit the fix entirely. Check each requested condition separately.
Thermo keywords and stored energies must be current for the stage being recorded.
LAMMPS minimize etol ftol maxiter maxeval: etol is relative/dimensionless; ftol is
force units. Its criteria are OR, not AND. Do not interpret older model-suggested
unit/AND text as overriding a user's later explicit correction. Static review
checks implemented numerical parameters and retained convergence diagnostics;
actual convergence is judged from the completed outputs, never a pre-run claim.
Do not block merely because outputs must later be checked for fmax/pressure.
Potential literature metadata and original files are retained by the snapshot;
do not require a redundant LAMMPS-generated citation file when these exist.
Parser facts: $(pe) is evaluated IMMEDIATELY before the variable command executes.
Thus capture E pe -> variable E equal $(pe) stores a numeric constant, NOT a dynamic
reference to pe. It is different from variable E equal pe. Do not repair this correct
capture into a dynamic reference. emit_table lowers to print with exact headers.
load_structure uses clear, which does NOT delete input variables. Repeated index
variables cannot be reassigned by redefining them; use distinct names or delete first.
Use numeric literal IDs for static group/delete operations. Quote the entire equal
expression when it contains spaces. A conventional cubic lattice constant is lx/nx,
not the full supercell length lx. Retain both the pre-defect and post-defect initial
structures when requested. Defining a compute before minimization does NOT invoke it.
LAMMPS 28Mar2023 requires invoked_scalar == current timestep for c_ID reads between
runs. For custom pressure, define it before the calculation AND explicitly consume
c_ID in thermo_style custom at the final step before capturing $(c_ID). Merely
outputting press updates the default thermo pressure compute, not every custom one.
Alternatively capture the already evaluated thermo press from the unchanged state.
After atom/box edits or timestep resets, recalculate before recording diagnostics.
Preserve consistent virial/kinetic semantics; 0 K pressure requires zero kinetic energy.
Sources: https://docs.lammps.org/variable.html , https://docs.lammps.org/print.html ,
https://docs.lammps.org/fix_box_relax.html , https://docs.lammps.org/thermo_style.html .
https://docs.lammps.org/Commands_parse.html , https://docs.lammps.org/clear.html .
These rules are tool knowledge, not reference answers or proof of scientific success.
'''

GUIDE += '''
save_state <safe_id> <declared-dump-basename> <first_step> <stride> saves exactly one
complete atomic state per owning begin_cycle iteration. Declare it exactly once in
that cycle; the number of states is its frozen cycle count, at most 256. first_step
is a nonnegative literal and stride a positive literal. The compiler asserts that
each actual step matches first_step+(iteration-1)*stride before appending. It retains
IDs, types, coordinates, velocities, image flags and box at full floating precision.
It adds no run, minimization or MC attempt. Never reuse that file for another writer.
scan_sites <safe_id> '<JSON specification>' performs the FULL saved-state x 1..atom_count
site x variant Cartesian product, no sampling or retries. JSON must have exactly:
version:1, states:<save_state id>, atom_count:<positive integer>, units:"metal",
elements:<ordered configured type_elements>, variants:[{id:<nonnegative integer>,
type:<1-based atom type or null for vacancy>}], baseline_file:<declared dump basename>,
cache_file:<declared restart basename>, table_file:<declared array basename>,
baseline_relaxation:<relaxation object>, variant_relaxation:<relaxation object>.
Include each configured atom type exactly once; one optional null variant deletes the
site with compress no. Variant IDs are unique; baseline rows use variant=-1. host is
the numeric variant ID corresponding to that site's ORIGINAL baseline atom type.
Each relaxation object has exactly min_style (cg or sd), etol (dimensionless >=0),
ftol (force >=0), max_iterations, max_evaluations (positive integers), kinetic_energy:
"zero", box:null (fixed cell) or {mode:"iso",pressure:<finite bar>,vmax:<positive finite>},
convergence:{force_metric:"fnorm" or "fmax",force_tolerance:<finite nonnegative force>,
pressure_target:<finite bar>,pressure_tolerance:<finite nonnegative bar>}.
All scientific parameters and diagnostics criteria must be supplied explicitly.
The same state is independently relaxed ONCE to a complete baseline; all sites and
variants restore that exact baseline, including IDs/types/box. A compiler-owned
single restart cache is overwritten once per state, while baseline_file retains ALL
baselines. Each site emits a baseline row plus every declared variant, even unchanged
occupancy. No input variables, model loops, scripts, arbitrary read paths or native
set/read_dump/read_restart/write_restart commands are exposed by these tools.
The array table format is site_scan_array_v1. Ordered columns are state site variant
N E V host baseline converged fmax fnorm pressure iterations max_iterations
max_evaluations followed by n_<element> for ALL declared elements. Units are 1 for
IDs/counts/flags/iterations/bounds, eV for E, angstrom^3 for V, eV/angstrom for forces,
bar for pressure. State/site domains are 1-based and contiguous. max_evaluations is
a BOUND, never an observed force-evaluation count. Actual minimizer stopping reason
and evaluations remain in log.lammps, with compiler stage markers. converged is an
observed force-and-pressure diagnostic, not a claim of scientific success. Every
baseline and variant minimize is counted, with explicit upper force-evaluation
bounds. At most 500000 scan minimizations, 100000 sites, 16 element types; technical
limits grant no resources. The scientific scan is ONE workflow in ONE accounted HPC
submission and does not reset the two-submission allowance. Declare only four flat
outputs per scan/source: raw states dump, all-baselines dump, one last-state restart
cache, one full array table; no per-site files. The compiler owns native control.
These operations currently support atomic data and metal units; incompatible units,
noncontiguous IDs or conditions need clarification, never a reduced sample.
For nonuniform sampling, use run_schedule <state_id> <start_step> '<JSON step list>'
exactly once inside a bounded cycle, and save_state <same_id> <dump> steps '<same list>'
after it. The list contains 1..256 strictly increasing integer timesteps greater than
the explicit nonnegative start_step; the cycle count MUST equal its complete length.
Both lists must match exactly, including every timestep. This expresses logarithmic
or other explicit sampling without replacing it by a linear approximation. Example:
fix exchange all atom/swap 7 4 17311 450.0 types 1 2 ke no
begin_cycle observations 3
run_schedule sampled 0 '[14,35,105]'
save_state sampled states.dump steps '[14,35,105]'
end_cycle observations
unfix exchange
These are synthetic syntax examples, NOT scientific defaults. Obtain the complete
list, starting timestep and MC parameters from confirmed task conditions. Declare
the states.dump output and scan_sites specification with states:"sampled" explicitly.
Minimization leaves its actual timestep unknown before execution. Before a scheduled
stage, explicitly establish the starting timestep (for example reset_timestep before
creating its MC fix); never reset an active atom/swap fix. After scan_sites, the final
restored baseline has timestep equal to the complete saved-state count.
The scheduled cycle has exactly this one physical stage: no additional run/minimize,
state reset or hidden run 0. The compiler emits literal run differences (14,21,70 in
the example), checks the actual start and every saved step, and never evaluates a
model expression as a run length. Existing fixes remain active across run segments;
the compiler does not reset them or the timestep. Planned MC events use the actual
fix creation timestep: the first event is the next timestep, then every N steps
(LAMMPS 28Mar2023 atom/swap). Splitting a run preserves that event phase and does
not invent additional swap attempts; an explicitly recreated fix has a new phase.
Unknown creation timesteps cannot be assigned exact planned attempts. The scan
reads the SAME complete timestep list, with contiguous state IDs 1..list length.
The old first_step/stride save_state format remains available for uniform cycles.
Sources (standard engine protocols, not author code):
https://docs.lammps.org/read_dump.html , https://docs.lammps.org/write_dump.html ,
https://docs.lammps.org/read_restart.html , https://docs.lammps.org/write_restart.html ,
https://docs.lammps.org/set.html , https://docs.lammps.org/delete_atoms.html .
https://raw.githubusercontent.com/lammps/lammps/patch_28Mar2023/src/MC/fix_atom_swap.cpp .
'''


def workflow_tool_context():
    """Expose trusted operations proactively and pin their implementation."""
    return {'name': 'scientific_workflow_tools', 'version': VERSION,
            'source_sha256': sha256(Path(__file__).read_bytes()),
            'operations': ['capture', 'emit_table', 'begin_cycle', 'end_cycle',
                           'sample_swap_types', 'run_schedule', 'save_state', 'scan_sites'],
            'limits': {'cycle_blocks': MAX_CYCLE_BLOCKS, 'cycles_per_block': MAX_CYCLES,
                       'saved_states':MAX_SAVED_STATES,'scan_atoms':MAX_SCAN_ATOMS,
                       'scan_minimizations':MAX_SCAN_EVALUATIONS,'scan_types':MAX_SCAN_TYPES},
            'cycle_nesting': False, 'native_control_owner': 'trusted_compiler',
            'state_site_scan_scope':{'atom_style':'atomic','units':['metal'],
                                    'minimizers':['cg','sd'],'box_modes':['fixed','iso'],
                                    'full_cartesian_product':True,
                                    'current_thermodynamics':'independent_binary_sites_v1'},
            'explicit_sampling':{'operation':'run_schedule','save_state_form':'steps',
                'steps':'strictly_increasing_literal_integer_list','start_step':'explicit_literal_integer',
                'cycle_count':'complete_list_length','physical_stages_per_iteration':1,
                'compilation':'literal_run_segments','scan_steps':'same_complete_list'},
            'physical_evaluation_performed': False,
            'limits_grant_resources': False}


def workflow_tokens(line):
    """Account for immediate evaluation BEFORE LAMMPS splits into arguments.

    No expression evaluation here. Preserve the original formula and reject trailing
    tokens; quoting an immediate expression would prevent LAMMPS substitution.
    """
    matched=re.fullmatch(r'(variable\s+[A-Za-z][A-Za-z0-9_]*\s+equal)\s+(\$\(.+\))\s*(?:#.*)?',line.strip())
    if matched:
        expression=matched[2]
        depth=0
        for i,char in enumerate(expression[1:]):
            if char=='(':depth+=1
            elif char==')':depth-=1
            if depth==0 and i!=len(expression)-2:break
        else:
            if depth==0:return shlex.split(matched[1])+[expression]
    return shlex.split(line,comments=True,posix=True)


def _positive_literal(value, maximum, description, *, minimum=1):
    if (not re.fullmatch(r'[0-9]{1,10}', value)
            or not minimum <= int(value) <= maximum):
        raise ValueError(description+' must be a literal integer in '+str(minimum)+'..'+str(maximum))
    return int(value)


def _explicit_steps(value):
    try:
        steps=json.loads(value)
    except (ValueError,TypeError):
        raise ValueError('Scheduled timesteps require one literal JSON integer list') from None
    if not isinstance(steps,list) or not 1<=len(steps)<=MAX_SAVED_STATES:
        raise ValueError('Scheduled timesteps require 1..'+str(MAX_SAVED_STATES)+' complete steps')
    for step in steps:
        _literal_integer(step,2147483647,'Scheduled timestep',minimum=0)
    if any(second<=first for first,second in zip(steps,steps[1:])):
        raise ValueError('Scheduled timesteps must be strictly increasing without duplicates')
    return steps


def _run_schedule(words):
    if len(words)!=4 or not re.fullmatch(_SAFE_ID,words[1]):
        raise ValueError('run_schedule requires a safe state ID, literal starting step and quoted JSON timestep list')
    start=_positive_literal(words[2],2147483647,'Scheduled starting timestep',minimum=0)
    steps=_explicit_steps(words[3])
    if steps[0]<=start:
        raise ValueError('Every scheduled timestep must be greater than the starting timestep')
    intervals=list(zip([start,*steps[:-1]],steps))
    return dict(id=words[1],start_step=start,steps=steps,
                run_steps_per_iteration=[last-first for first,last in intervals],
                run_intervals=[list(pair) for pair in intervals],total_run_steps=steps[-1]-start)


def scheduled_swap_accounting(schedule, every_steps, attempts_per_event, *,
                              fix_created_step=None, recreate_per_segment=False):
    """Count planned MC work over absolute steps; never infer observed acceptance."""
    _literal_integer(every_steps,2147483647,'MC event interval')
    _literal_integer(attempts_per_event,2147483647,'MC attempts per event')
    # Revalidate metadata instead of trusting caller-created intervals or totals.
    if not isinstance(schedule,dict) or not {'id','start_step','steps','run_intervals',
            'run_steps_per_iteration','total_run_steps'}<=set(schedule):
        raise ValueError('Scheduled MC accounting requires complete literal schedule metadata')
    _literal_integer(schedule['start_step'],2147483647,'Scheduled starting timestep',minimum=0)
    if not isinstance(schedule['id'],str):
        raise ValueError('Scheduled MC accounting requires a safe state ID')
    validated=_run_schedule(['run_schedule',schedule['id'],str(schedule['start_step']),
                             json.dumps(schedule['steps'])])
    if (schedule.get('run_intervals')!=validated['run_intervals'] or
            schedule.get('run_steps_per_iteration')!=validated['run_steps_per_iteration'] or
            schedule.get('total_run_steps')!=validated['total_run_steps']):
        raise ValueError('Scheduled MC accounting requires consistent complete run intervals')
    if type(recreate_per_segment) is not bool or (recreate_per_segment and fix_created_step is not None):
        raise ValueError('MC accounting must distinguish a persistent fix from one explicitly recreated per segment')
    if not recreate_per_segment:
        _literal_integer(fix_created_step,2147483647,'MC fix creation timestep',minimum=0)
        if fix_created_step>validated['start_step']:
            raise ValueError('Persistent MC fix must exist before the scheduled run starts')
    events=[]
    for first,last in validated['run_intervals']:
        created=first if recreate_per_segment else fix_created_step
        # Fixed engine semantics: first exchange at creation+1; subsequent ones
        # are N steps apart, preserving the phase across literal run segments.
        def through(step):
            return max(0,1+(step-created-1)//every_steps)
        events.append(through(last)-through(first))
    attempts=[count*attempts_per_event for count in events]
    return dict(planned_events_per_segment=events,planned_events=sum(events),
                planned_attempts_per_segment=attempts,planned_attempts=sum(attempts))


def cycle_metadata(body, *, max_cycles=MAX_CYCLES):
    """Inspect virtual cycles, without executing or evaluating scientific expressions.

    Line numbers are one-based. Counts concern target calculation commands, including
    run 0, not submissions or authorization. Only this compiler owns native controls.
    """
    if not isinstance(body, str):
        raise ValueError('Cycle workflow must be text')
    if type(max_cycles) is not int or not 1 <= max_cycles <= 2147483647:
        raise ValueError('Invalid technical cycle bound')
    cycles, records, line_multipliers, schedules = [], [], {}, []
    active, ids, prefixes, sample_variables = None, set(), set(), {}
    seed = None
    calculation_commands = 0
    for number, line in enumerate(body.splitlines(), 1):
        try:
            words = workflow_tokens(line)
        except ValueError:
            raise ValueError('Unclosed tool argument') from None
        if not words:
            continue
        command = words[0]
        content = ' '.join(words)
        if re.search(r'__alr_', content, flags=re.I):
            raise ValueError('__alr_ names are reserved for the cycle adapter')
        if command in _RAW_CONTROL:
            raise ValueError('Unsupported workflow command: '+command+'; use begin_cycle/end_cycle for finite scientific cycles')
        if command == 'variable' and len(words) >= 3 and words[2] in {
                'loop', 'file', 'atomfile', 'universe', 'uloop', 'world', 'python', 'getenv'}:
            raise ValueError('Native variable control/external style is not allowed')
        records.append((number, content, words, active['id'] if active else None))
        line_multipliers[number] = active['count'] if active else 1
        if command == 'begin_cycle':
            if len(words) != 3 or not re.fullmatch(_SAFE_ID, words[1]):
                raise ValueError('begin_cycle requires a safe ID and a positive literal count')
            if active is not None:
                raise ValueError('Scientific cycles cannot be nested')
            if words[1] in ids or len(cycles) >= MAX_CYCLE_BLOCKS:
                raise ValueError('Scientific cycle IDs must be unique; at most '+str(MAX_CYCLE_BLOCKS)+' blocks')
            count = _positive_literal(words[2], max_cycles, 'Cycle count')
            ids.add(words[1])
            active = dict(id=words[1], count=count, begin_line=number, end_line=None,
                          calculation_commands_per_cycle=0, calculation_commands_total=0, samples=[])
            cycles.append(active)
        elif command == 'end_cycle':
            if len(words) != 2 or active is None or words[1] != active['id']:
                raise ValueError('end_cycle must close the matching active cycle')
            if not active['calculation_commands_per_cycle']:
                raise ValueError('Scientific cycle must contain an explicit run/minimize stage')
            active['end_line'] = number
            active['calculation_commands_total'] = active['count'] * active['calculation_commands_per_cycle']
            line_multipliers[number] = 1
            active = None
        elif command == 'run_schedule':
            if active is None:
                raise ValueError('run_schedule must belong to one bounded cycle')
            schedule=_run_schedule(words)
            if active.get('run_schedule') is not None or any(s['id']==schedule['id'] for s in schedules):
                raise ValueError('Scheduled state IDs must be unique, with one run_schedule per cycle')
            if active['count']!=len(schedule['steps']):
                raise ValueError('Scheduled cycle count must equal the complete timestep list length')
            schedule.update(line=number,cycle_id=active['id'])
            active['run_schedule']=schedule
            schedules.append(schedule)
        elif command == 'sample_swap_types':
            if len(words) != 4 or not re.fullmatch(_SAFE_ID, words[1]) or active is None:
                raise ValueError('sample_swap_types requires a safe prefix, type count and seed inside a cycle')
            if words[1] in prefixes:
                raise ValueError('Sampling prefixes must be unique in the workflow')
            types = _positive_literal(words[2], MAX_SAMPLE_TYPES, 'Sampling type count', minimum=2)
            declared_seed = _positive_literal(words[3], 2147483647, 'Sampling seed')
            if seed is not None and seed != declared_seed:
                raise ValueError('All sample_swap_types declarations share one RNG and must use the same seed')
            seed = declared_seed
            prefixes.add(words[1])
            names = [words[1]+'_i', words[1]+'_j']
            sample = dict(prefix=words[1], type_count=types, seed=seed, variables=names, line=number)
            active['samples'].append(sample)
            for name in names:
                sample_variables[name] = dict(cycle_id=active['id'], range=[1, types],
                                              seed=seed, declaration_line=number, prefix=words[1])
        elif active is not None:
            if command in _CYCLE_STATE_RESETS:
                raise ValueError('Scientific cycle cannot rebuild/reset its state: '+command)
            if command in {'compute', 'uncompute', 'undump'}:
                raise ValueError('Declare computes/dumps before cycles and clean up afterwards; lifecycle changes inside a cycle are not allowed')
            if command in {'dump', 'write_data', 'write_dump'} or 'file' in words:
                raise ValueError('Scientific cycle cannot reopen/overwrite output files; declare dumps before it or use emit_table/append')
            if command == 'variable' and len(words) >= 3 and words[2] == 'index':
                raise ValueError('Index variables survive iterations; use capture/equal or sampling tools inside cycles')
        if command in {'run', 'minimize', 'run_schedule'}:
            multiplier = active['count'] if active else 1
            calculation_commands += multiplier
            if active is not None:
                active['calculation_commands_per_cycle'] += 1
    if active is not None:
        raise ValueError('Unclosed scientific cycle '+active['id'])
    for number, content, words, cycle_id in records:
        if cycle_id is not None and words[0] in {'run','minimize'} and any(
                s['cycle_id']==cycle_id for s in schedules):
            raise ValueError('Scheduled cycles have exactly one run_schedule physical stage; no extra run/minimize')
        if cycles and re.search(r'\bnext\s*\(', content):
            raise ValueError('next() expressions can alter cycle control and are not allowed')
        if cycles and words[0] == 'run':
            if len(words) < 2:
                raise ValueError('Cycle workflows require an explicit literal run step count')
            _positive_literal(words[1], 2147483647, 'Run steps', minimum=0)
            options = words[2:]
            if (len(options) % 2 or any(options[i] not in {'pre', 'post'} or options[i+1] not in {'yes', 'no'}
                    for i in range(0, len(options), 2)) or len(set(options[::2])) != len(options[::2])):
                raise ValueError('Cycle run options are limited to pre/post yes/no; dynamic run every is not allowed')
        if cycle_id is not None and words[0] == 'minimize':
            if len(words) != 5:
                raise ValueError('Cycle minimize requires four literal finite arguments')
            try:
                tolerances = [float(value) for value in words[1:3]]
            except ValueError:
                tolerances = [math.nan]
            if any(not math.isfinite(value) or value < 0 for value in tolerances):
                raise ValueError('Cycle minimize tolerances must be finite nonnegative literals')
            _positive_literal(words[3], 2147483647, 'Minimize iteration bound')
            _positive_literal(words[4], 2147483647, 'Minimize evaluation bound')
        if sample_variables and re.search(r'\b(?:random|normal)\s*\(', content):
            raise ValueError('Native random()/normal() cannot be mixed with the shared sampling tool RNG')
        if words[0] in {'variable', 'capture'} and len(words) >= 2 and words[1] in sample_variables:
            raise ValueError('Sampled variables are immutable and managed by the adapter')
        referenced = set(re.findall(r'\$\{([A-Za-z][A-Za-z0-9_]*)\}|\bv_([A-Za-z][A-Za-z0-9_]*)', content))
        for pair in referenced:
            name = pair[0] or pair[1]
            sample = sample_variables.get(name)
            if sample and (cycle_id != sample['cycle_id'] or number <= sample['declaration_line']):
                raise ValueError('Sampled variable '+name+' may be used only after its declaration in its owning cycle')
            if (sample and words[0] == 'variable' and len(words) >= 4
                    and words[2] == 'equal' and not words[3].startswith('$(')):
                raise ValueError('A dynamic alias to sampled variables outlives its cycle; use capture to freeze it')
    return dict(version=VERSION, cycles=cycles, line_multipliers=line_multipliers,
                sample_variables=sample_variables, calculation_commands=calculation_commands,
                sampling_rng='shared_equal_style_stream' if sample_variables else None,
                sampling_seed=seed,run_schedules=schedules)


def _table_headers(name, tables, prefix):
    cols = tables[name]['columns']
    return [f'print "# columns: {" ".join(c["name"] for c in cols)}" file {prefix}{name}',
            f'print "# units: {" ".join(c["unit"] for c in cols)}" append {prefix}{name}']


def _literal_number(value, name, *, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(name+' must be an explicit finite numerical literal')
    return value


def _literal_integer(value, maximum, name, *, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name+' must be an explicit integer in '+str(minimum)+'..'+str(maximum))
    return value


def _basename(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value):
        raise ValueError('State/scan files require safe flat literal basenames')
    return value


def _relaxation(spec):
    fields={'min_style','etol','ftol','max_iterations','max_evaluations','kinetic_energy','box','convergence'}
    if (not isinstance(spec,dict) or set(spec)!=fields or not isinstance(spec['min_style'],str)
            or spec['min_style'] not in {'cg','sd'} or spec['kinetic_energy']!='zero'):
        raise ValueError('Scan relaxation requires explicit static minimizer, bounds, zero kinetic energy, box and convergence')
    for key in ('etol','ftol'):
        _literal_number(spec[key],key,minimum=0)
    for key in ('max_iterations','max_evaluations'):
        _literal_integer(spec[key],2147483647,key)
    box=spec['box']
    if box is not None:
        if not isinstance(box,dict) or set(box)!={'mode','pressure','vmax'} or box['mode']!='iso':
            raise ValueError('Scan box relaxation supports explicit iso pressure/vmax or null fixed cell')
        _literal_number(box['pressure'],'Box pressure')
        if _literal_number(box['vmax'],'Box vmax',minimum=0)==0:
            raise ValueError('Scan box vmax must be strictly positive')
    diagnostic=spec['convergence']
    if (not isinstance(diagnostic,dict) or set(diagnostic)!={'force_metric','force_tolerance','pressure_target','pressure_tolerance'}
            or not isinstance(diagnostic['force_metric'],str)
            or diagnostic['force_metric'] not in {'fnorm','fmax'}):
        raise ValueError('Scan convergence requires explicit force metric/tolerance and pressure target/tolerance')
    _literal_number(diagnostic['force_tolerance'],'Force diagnostic tolerance',minimum=0)
    _literal_number(diagnostic['pressure_target'],'Diagnostic pressure target')
    _literal_number(diagnostic['pressure_tolerance'],'Pressure diagnostic tolerance',minimum=0)


def scan_columns(elements):
    names=['state','site','variant','N','E','V','host','baseline','converged','fmax','fnorm',
           'pressure','iterations','max_iterations','max_evaluations']+['n_'+element for element in elements]
    units={ 'E':'eV','V':'angstrom^3','fmax':'eV/angstrom','fnorm':'eV/angstrom','pressure':'bar'}
    return [dict(name=name,unit=units.get(name,'1')) for name in names]


def _unique_json_object(pairs):
    value={}
    for name,item in pairs:
        if name in value:
            raise ValueError('Duplicate scan specification field')
        value[name]=item
    return value


def _scan_spec(words):
    if len(words)!=3 or not re.fullmatch(_SAFE_ID,words[1]):
        raise ValueError('scan_sites requires a safe ID and one quoted literal JSON specification')
    try:
        spec=json.loads(words[2],object_pairs_hook=_unique_json_object)
    except (ValueError,TypeError):
        raise ValueError('Invalid literal scan JSON specification') from None
    fields={'version','states','atom_count','units','elements','variants','baseline_file','cache_file','table_file',
            'baseline_relaxation','variant_relaxation'}
    if not isinstance(spec,dict) or set(spec)!=fields or type(spec['version']) is not int or spec['version']!=1 or spec['units']!='metal':
        raise ValueError('Scan specification requires the exact version-one atomic/metal contract')
    if not isinstance(spec['states'],str) or not re.fullmatch(_SAFE_ID,spec['states']):
        raise ValueError('Scan states must name a declared save_state operation')
    _literal_integer(spec['atom_count'],MAX_SCAN_ATOMS,'Scan atom count')
    elements=spec['elements']
    if (not isinstance(elements,list) or not 1<=len(elements)<=MAX_SCAN_TYPES
            or any(not isinstance(e,str) or not re.fullmatch('[A-Z][a-z]?',e) for e in elements)
            or len(set(elements))!=len(elements)):
        raise ValueError('Scan requires a unique ordered list of configured element types')
    variants=spec['variants']
    if not isinstance(variants,list) or not len(elements)<=len(variants)<=len(elements)+1:
        raise ValueError('Scan declares every occupied type once, and at most one vacancy')
    seen_ids=set();seen_types=set()
    for variant in variants:
        if not isinstance(variant,dict) or set(variant)!={'id','type'}:
            raise ValueError('Each scan variant requires its literal ID and type or null vacancy')
        _literal_integer(variant['id'],2147483647,'Variant ID',minimum=0)
        if variant['type'] is not None:
            _literal_integer(variant['type'],len(elements),'Variant atom type')
        if variant['id'] in seen_ids or variant['type'] in seen_types:
            raise ValueError('Scan variant IDs and atom types must be unique')
        seen_ids.add(variant['id']);seen_types.add(variant['type'])
    if seen_types-{None}!=set(range(1,len(elements)+1)):
        raise ValueError('Scan variants must cover ALL configured atom types without omission')
    files=[_basename(spec[key]) for key in ('baseline_file','cache_file','table_file')]
    if len(set(files))!=3:
        raise ValueError('Baseline, restart cache and array table require distinct files')
    _relaxation(spec['baseline_relaxation']);_relaxation(spec['variant_relaxation'])
    return spec


def state_scan_metadata(body, cycles=None, *, plan=None, atom_count=None, type_elements=None):
    """Freeze complete state/site domains and exact evaluation bounds, no physics."""
    cycles=cycles if cycles is not None else cycle_metadata(body)
    series={};scans=[];writers={};scan_ids=set()
    for number,line in enumerate(body.splitlines(),1):
        words=workflow_tokens(line)
        if not words:
            continue
        if words[0]=='save_state':
            owner=next((c for c in cycles['cycles'] if c['begin_line']<number<c['end_line']),None)
            if len(words)!=5 or not re.fullmatch(_SAFE_ID,words[1]) or owner is None:
                raise ValueError('save_state requires an ID, dump and literal sampling inside one bounded cycle')
            if words[1] in series or any(item['cycle_id']==owner['id'] for item in series.values()):
                raise ValueError('Save exactly one complete state per owning cycle; state IDs must be unique')
            _literal_integer(owner['count'],MAX_SAVED_STATES,'Saved state count')
            schedule=owner.get('run_schedule')
            if words[3]=='steps':
                steps=_explicit_steps(words[4])
                if (schedule is None or words[1]!=schedule['id'] or steps!=schedule['steps']
                        or number<=schedule['line'] or len(steps)!=owner['count']):
                    raise ValueError('save_state steps must follow the SAME complete run_schedule ID and timestep list')
                first,last=steps[0],steps[-1]
            else:
                if schedule is not None:
                    raise ValueError('Scheduled cycles require the SAME explicit save_state steps list')
                first=_positive_literal(words[3],2147483647,'First saved timestep',minimum=0)
                stride=_positive_literal(words[4],2147483647,'Saved timestep stride')
                last=first+(owner['count']-1)*stride
                if last>2147483647:
                    raise ValueError('Saved timestep domain exceeds the explicit bound')
            filename=_basename(words[2])
            if filename in writers:
                raise ValueError('State/scan outputs cannot have multiple writers')
            writers[filename]=number
            saved=dict(id=words[1],line=number,file=filename,cycle_id=owner['id'],
                count=owner['count'],first_step=first,last_step=last,
                fields=['id','type','x','y','z','vx','vy','vz','ix','iy','iz'],
                id_preservation='purge_yes_add_keep',full_precision=True)
            if schedule is not None:
                saved.update(sampling='explicit_steps',steps=list(steps),start_step=schedule['start_step'],
                    run_steps_per_iteration=list(schedule['run_steps_per_iteration']),
                    total_run_steps=schedule['total_run_steps'])
            else:
                run_steps=sum(int(tokens[1]) for text in body.splitlines()[owner['begin_line']:owner['end_line']-1]
                              if (tokens:=workflow_tokens(text)) and tokens[0]=='run')
                saved.update(stride=stride,run_steps_per_cycle=run_steps,total_run_steps=run_steps*owner['count'])
            series[words[1]]=saved
        elif words[0]=='scan_sites':
            if any(c['begin_line']<number<c['end_line'] for c in cycles['cycles']):
                raise ValueError('scan_sites owns its finite loops and cannot be placed inside a model cycle')
            if words[1:2] and words[1] in scan_ids:
                raise ValueError('Scan IDs must be unique')
            spec=_scan_spec(words)
            source=series.get(spec['states'])
            if source is None or number<=next(c['end_line'] for c in cycles['cycles'] if c['id']==source['cycle_id']):
                raise ValueError('Scan requires its entire saved-state cycle to be completed first')
            if atom_count is not None and spec['atom_count']!=atom_count:
                raise ValueError('Full scan atom count must match the complete frozen initial structure')
            if type_elements is not None and spec['elements']!=list(type_elements):
                raise ValueError('Scan element order must match the complete configured atom type mapping')
            evaluations=source['count']*(1+spec['atom_count']*len(spec['variants']))
            if evaluations>MAX_SCAN_EVALUATIONS:
                raise ValueError('Full scan exceeds the explicit technical minimization bound; never silently sample')
            for key in ('baseline_file','cache_file','table_file'):
                filename=spec[key]
                if filename in writers:
                    raise ValueError('State/scan outputs cannot have multiple writers')
                writers[filename]=number
            if plan is not None:
                tables=[t for t in plan.get('tables',[]) if t.get('file')==spec['table_file']]
                if len(tables)!=1 or tables[0].get('format')!=SCAN_FORMAT or tables[0].get('columns')!=scan_columns(spec['elements']):
                    raise ValueError('Scan array requires exact site_scan_array_v1 columns and units')
                for operation in plan.get('operations',[]):
                    if operation.get('file')!=spec['table_file'] or operation.get('method')!='site_thermodynamics_v1':
                        continue
                    expected_elements={str(v['id']):spec['elements'][v['type']-1]
                                       for v in spec['variants'] if v['type'] is not None}
                    vacancy=[v['id'] for v in spec['variants'] if v['type'] is None]
                    if (operation.get('states')!=dict(first=1,last=source['count'],stride=1)
                            or operation.get('sites')!=dict(first=1,last=spec['atom_count'],stride=1)
                            or operation.get('elements')!=expected_elements
                            or vacancy!=[operation.get('vacancy_variant')]):
                        raise ValueError('Scan analysis must cover the SAME complete state/site/variant domains without subsampling')
                    convergence=operation.get('convergence',{})
                    for relaxation in (spec['baseline_relaxation'],spec['variant_relaxation']):
                        diagnostic=relaxation['convergence']
                        exact={'force_metric':diagnostic['force_metric'],
                               'max_iterations':relaxation['max_iterations'],
                               'max_evaluations':relaxation['max_evaluations']}
                        if (any(convergence.get(key)!=value for key,value in exact.items())
                                or not all(type(value) in (float,int) and math.isfinite(value) for value in
                                           [operation.get('pressure_GPa'),convergence.get('force_max_eV_per_A'),
                                            convergence.get('pressure_tolerance_GPa')])
                                or not math.isclose(convergence['force_max_eV_per_A'],diagnostic['force_tolerance'],rel_tol=1e-12)
                                or not math.isclose(operation['pressure_GPa']*10000,diagnostic['pressure_target'],rel_tol=1e-12)
                                or not math.isclose(convergence['pressure_tolerance_GPa']*10000,
                                                    diagnostic['pressure_tolerance'],rel_tol=1e-12)):
                            raise ValueError('Scan and analysis must freeze the SAME force/pressure diagnostics and minimization bounds')
            scan_ids.add(words[1])
            scan=dict(id=words[1],line=number,specification=spec,state_count=source['count'],
                site_domain=[1,spec['atom_count']],state_domain=[1,source['count']],
                independent_restore=True,baseline_minimizations=source['count'],
                variant_minimizations=source['count']*spec['atom_count']*len(spec['variants']),
                calculation_commands=evaluations,rows=source['count']*spec['atom_count']*(1+len(spec['variants'])),
                maximum_force_evaluations=source['count']*spec['baseline_relaxation']['max_evaluations']+
                    source['count']*spec['atom_count']*len(spec['variants'])*spec['variant_relaxation']['max_evaluations'],
                retry_count=0,additional_submissions=0)
            if source.get('sampling')=='explicit_steps':
                scan['source_timesteps']=list(source['steps'])
            scans.append(scan)
    for schedule in cycles['run_schedules']:
        if schedule['id'] not in series or series[schedule['id']]['cycle_id']!=schedule['cycle_id']:
            raise ValueError('Every run_schedule requires its complete matching save_state steps operation')
    return dict(version=2 if cycles['run_schedules'] else 1,states=list(series.values()),scans=scans,
                calculation_commands=sum(s['calculation_commands'] for s in scans),
                output_files=list(writers),limits_grant_resources=False)


def _scan_relax_lines(name, spec, marker):
    lines=[f'print "AUTO_LAMMPS_SCAN {marker}"', 'velocity all set 0 0 0',
           f'min_style {spec["min_style"]}', f'thermo {spec["max_iterations"]}',
           'thermo_style custom step atoms pe vol press fmax fnorm', 'thermo_modify norm no',
           f'variable {name}_start equal $(step:%.0f)']
    if spec['box'] is not None:
        box=spec['box']
        lines.append(f'fix {name}_relax all box/relax iso {box["pressure"]} vmax {box["vmax"]}')
    lines.append(f'minimize {spec["etol"]} {spec["ftol"]} {spec["max_iterations"]} {spec["max_evaluations"]}')
    if spec['box'] is not None:
        lines.append(f'unfix {name}_relax')
    return lines


def _scan_values(name, relaxation, elements, *, saved=False):
    """Observed scalars are evaluated in the engine at the completed physical stage."""
    diagnostic=relaxation['convergence']
    values={'N':'count(all)','E':'pe','V':'vol','fmax':'fmax','fnorm':'fnorm','pressure':'press',
            'iterations':f'step-v_{name}_start',
            'converged':f'({diagnostic["force_metric"]}<={diagnostic["force_tolerance"]})&&'
                        f'(abs(press-({diagnostic["pressure_target"]}))<={diagnostic["pressure_tolerance"]})'}
    lines=[]
    for index,element in enumerate(elements,1):
        group=f'{name}_type{index}'
        lines.append(f'group {group} type {index}')
        values['n_'+element]=f'count({group})'
    if saved:
        for key,expression in values.items():
            lines.append(f'variable {name}_base_{key} equal $({expression}:%.17g)')
        result={key:'${'+name+'_base_'+key+'}' for key in values}
    else:
        result={key:'$('+expression+':%.17g)' for key,expression in values.items()}
    result['max_iterations']=str(relaxation['max_iterations'])
    result['max_evaluations']=str(relaxation['max_evaluations'])
    return lines,result


def _lower_scan(scan, source, tables, prefix, reload_header):
    spec=scan['specification'];name='__alr_scan_'+scan['id']
    state=name+'_state';site=name+'_site';host=name+'_host';step=name+'_step'
    baseline=spec['baseline_relaxation'];variant_relax=spec['variant_relaxation']
    def restore_cache():
        # Restart stores the complete baseline and IDs. EAM/MEAM/SNAP coefficients
        # are rebound explicitly, since engine restart formats need not retain them.
        return ['clear',*reload_header[:3],f'read_restart {prefix}{spec["cache_file"]}',*reload_header[5:]]
    def row(values, variant, baseline_flag):
        entries=dict(values,state='${'+state+'}',site='${'+site+'}',variant=str(variant),
                     host='${'+host+'}',baseline=str(baseline_flag))
        payload=' '.join(entries[c['name']] for c in scan_columns(spec['elements']))
        return f'print "{payload}" append {prefix}{spec["table_file"]}'
    result=_table_headers(spec['table_file'],tables,prefix)
    explicit=source.get('sampling')=='explicit_steps'
    if explicit:
        # next requires the same variable style in one synchronized operation.
        result.extend([f'variable {state} index '+ ' '.join(str(i) for i in range(1,source['count']+1)),
                       f'variable {step} index '+ ' '.join(map(str,source['steps'])),f'label {name}_states'])
    else:
        result.extend([f'variable {state} loop {source["count"]}',f'label {name}_states',
                       f'variable {step} equal $({source["first_step"]}+(v_{state}-1)*{source["stride"]}:%.0f)'])
    result.extend(['clear',*reload_header,
                   f'read_dump {prefix}{source["file"]} ${{{step}}} x y z vx vy vz ix iy iz box yes purge yes add keep',
                   f'if "$(count(all)) != {spec["atom_count"]}" then "quit 91"'])
    result.extend(_scan_relax_lines(name,baseline,f'{scan["id"]} state ${{{state}}} site 0 variant -1'))
    captured,base_values=_scan_values(name,baseline,spec['elements'],saved=True)
    result.extend(captured)
    result.extend(f'group {name}_type{index} delete' for index in range(1,len(spec['elements'])+1))
    # The all-baselines dump retains every state. The single restart is just an
    # exact engine-local cache for this state's full independent variant scan.
    result.extend([f'reset_timestep ${{{state}}}',
        f'write_dump all custom {prefix}{spec["baseline_file"]} id type x y z vx vy vz ix iy iz modify append yes sort id format float %.17g',
        f'write_restart {prefix}{spec["cache_file"]}',
        f'variable {site} loop {spec["atom_count"]}',f'label {name}_sites',*restore_cache(),
        f'group {name}_sitegroup id ${{{site}}}',
        f'if "$(count({name}_sitegroup)) != 1" then "quit 92"'])
    host_formula='+'.join(f'(type[v_{site}]=={v["type"]})*{v["id"]}' for v in spec['variants'] if v['type'] is not None)
    result.extend([f'variable {host} equal $({host_formula}:%.0f)',row(base_values,-1,1)])
    for variant in spec['variants']:
        result.extend(restore_cache())
        result.extend([f'group {name}_sitegroup id ${{{site}}}',
                       f'if "$(count({name}_sitegroup)) != 1" then "quit 92"'])
        if variant['type'] is None:
            result.append(f'delete_atoms group {name}_sitegroup compress no')
        else:
            result.append(f'set atom ${{{site}}} type {variant["type"]}')
        result.extend(_scan_relax_lines(name,variant_relax,f'{scan["id"]} state ${{{state}}} site ${{{site}}} variant {variant["id"]}'))
        observed,values=_scan_values(name,variant_relax,spec['elements'])
        result.extend(observed);result.append(row(values,variant['id'],0))
    result.extend([f'next {site}',f'jump SELF {name}_sites',
                   f'next {state}'+(' '+step if explicit else ''),f'jump SELF {name}_states'])
    result.extend(restore_cache())
    # Keep ordinary workflow variable namespace intact; trusted scratch names
    # cannot be supplied or reused by a model and are scoped to this scan ID.
    return result


def expand_tools(body, plan, prefix, *, lower_cycles=False, reload_header=None):
    metadata = cycle_metadata(body)
    state_scans=state_scan_metadata(body,metadata,plan=plan)
    tables = {t['file']: t for t in (plan or {}).get('tables', [])}
    initialized, result = set(), []
    starts = {cycle['begin_line']: cycle for cycle in metadata['cycles']}
    ends = {cycle['end_line']: cycle for cycle in metadata['cycles']}
    samples = {sample['line']: sample for cycle in metadata['cycles'] for sample in cycle['samples']}
    states={item['line']:item for item in state_scans['states']}
    scans={item['line']:item for item in state_scans['scans']}
    schedules={item['line']:item for item in metadata['run_schedules']}
    lines = body.splitlines()
    def entries():
        number=1
        while number<=len(lines):
            cycle=starts.get(number)
            if lower_cycles and cycle is not None and 'run_schedule' in cycle:
                # The bounded body is repeated lexically, never with a model run
                # expression. No clear/reset or extra calculation is inserted.
                yield number,lines[number-1],None
                for iteration in range(cycle['count']):
                    for owned in range(number+1,cycle['end_line']+1):
                        yield owned,lines[owned-1],iteration
                number=cycle['end_line']+1
            else:
                yield number,lines[number-1],None
                number+=1
    for number, line, iteration in entries():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError('Unclosed tool argument') from None
        if not words:
            result.append(line); continue
        if words[0] == 'begin_cycle':
            cycle = starts[number]
            # Header-only hoisting does not evaluate or modify physical row expressions.
            # A repeated file writer inside a native loop would otherwise erase history.
            for future in lines[number:cycle['end_line']-1]:
                writer = shlex.split(future, comments=True)
                if writer and writer[0] == 'emit_table':
                    if len(writer) != 3 or writer[1] not in tables or 'format' in tables[writer[1]]:
                        raise ValueError('emit_table requires a labeled plan table basename and a quoted row')
                    if writer[1] not in initialized:
                        result.extend(_table_headers(writer[1], tables, prefix))
                        initialized.add(writer[1])
            if lower_cycles and 'run_schedule' not in cycle:
                result.extend([f'variable __alr_cycle_{cycle["id"]} loop {cycle["count"]}',
                               f'label __alr_label_{cycle["id"]}'])
            elif not lower_cycles:
                result.append(line)
        elif words[0] == 'end_cycle':
            cycle = ends[number]
            if lower_cycles:
                for sample in cycle['samples']:
                    result.extend([f'variable {name} delete' for name in sample['variables']])
                    result.append(f'variable __alr_draw_{sample["prefix"]} delete')
                if 'run_schedule' not in cycle:
                    result.extend([f'next __alr_cycle_{cycle["id"]}',
                                   f'jump SELF __alr_label_{cycle["id"]}'])
            else:
                result.append(line)
        elif words[0]=='run_schedule':
            schedule=schedules[number]
            if lower_cycles:
                first,last=schedule['run_intervals'][iteration]
                result.extend([f'if "$(step) != {first}" then "quit 90"',f'run {last-first}'])
            else:
                result.append(line)
        elif words[0] == 'sample_swap_types':
            sample = samples[number]
            if lower_cycles:
                name, types, seed = sample['prefix'], sample['type_count'], sample['seed']
                raw = '__alr_draw_'+name
                result.extend([f'variable {name}_i index $(floor(random(1,{types+1},{seed})):%.0f)',
                               f'variable {raw} index $(floor(random(1,{types},{seed})):%.0f)',
                               f'variable {name}_j index $(v_{raw}+(v_{raw}>=v_{name}_i):%.0f)'])
            else:
                result.append(line)
        elif words[0]=='save_state':
            saved=states[number]
            if lower_cycles:
                if saved.get('sampling')=='explicit_steps':
                    expected=str(saved['steps'][iteration])
                else:
                    index='__alr_cycle_'+saved['cycle_id']
                    expected=f'{saved["first_step"]}+(v_{index}-1)*{saved["stride"]}'
                result.extend([f'if "$(step) != {expected}" then "quit 90"',
                    f'write_dump all custom {prefix}{saved["file"]} id type x y z vx vy vz ix iy iz modify append yes sort id format float %.17g'])
            else:
                result.append(line)
        elif words[0]=='scan_sites':
            scan=scans[number]
            if lower_cycles:
                if not isinstance(reload_header,list) or len(reload_header)<5:
                    raise ValueError('Trusted fixed structure/potential header required to compile the complete scan')
                if reload_header[0]!='units '+scan['specification']['units']:
                    raise ValueError('Scan units must match the frozen engine/potential units')
                source=next(s for s in state_scans['states'] if s['id']==scan['specification']['states'])
                result.extend(_lower_scan(scan,source,tables,prefix,reload_header))
                initialized.add(scan['specification']['table_file'])
            else:
                result.append(line)
        elif words[0] == 'capture':
            expression = ' '.join(words[2:])
            if (len(words) < 3 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', words[1])
                    or not re.fullmatch(r'[A-Za-z0-9_+*/().,\[\]<>!=: \-]+', expression)):
                raise ValueError('capture requires a safe name and scalar expression')
            immediate = f'$({expression})'
            result.append(f'variable {words[1]} equal {immediate}')
        elif (words[0] == 'variable' and len(words) > 4 and words[2] == 'equal'
                and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', words[1])
                and re.fullmatch(r'[A-Za-z0-9_+*/().,\[\]<>!=: \-]+', ' '.join(words[3:]))):
            # Lexical compilation only: preserve every operand/operator and its order.
            # Raw proposal + rendered script are both retained in the snapshot.
            result.append(f'variable {words[1]} equal "{" ".join(words[3:])}"')
        elif words[0] == 'emit_table':
            if len(words) != 3 or words[1] not in tables or 'format' in tables[words[1]]:
                raise ValueError('emit_table requires a labeled plan table basename and a quoted row')
            name, payload = words[1:]
            cols = tables[name]['columns']
            if any(c in payload for c in '\r\n"\\') or len(payload.split()) != len(cols):
                raise ValueError('emit_table row must contain exactly one whitespace-free scalar per column')
            if not re.fullmatch(r'[A-Za-z0-9_+*/().,\[\]<>!=:$%{} \-]+', payload):
                raise ValueError('emit_table row contains unsupported expression characters')
            if name not in initialized:
                result.extend(_table_headers(name, tables, prefix))
                initialized.add(name)
            result.append(f'print "{payload}" append {prefix}{name}')
        else:
            result.append(line)
    return '\n'.join(result)


def check_table_writers(body, plan, prefix):
    """Require labeled headers to match the frozen parser and reject overwrites.

    Native ave/time format has its own bounded adapter. Row values are not evaluated.
    """
    for table in plan['tables']:
        if table.get('format')==SCAN_FORMAT:
            scans=state_scan_metadata(body,plan=plan)['scans']
            if len([s for s in scans if s['specification']['table_file']==table['file']])!=1:
                raise ValueError('Complete scan arrays must be written by exactly one scan_sites tool')
            continue
        if 'format' in table:
            continue
        path = prefix + table['file']
        writes = []
        for line in body.splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens or path not in tokens:
                continue
            if tokens[0] != 'print' or len(tokens) < 4 or tokens[2] not in {'file','append'} or tokens[3] != path:
                raise ValueError('Use emit_table for labeled table '+table['file']+'; other writers are not verified')
            writes.append(tokens)
        expected = ['# columns: '+' '.join(c['name'] for c in table['columns']),
                    '# units: '+' '.join(c['unit'] for c in table['columns'])]
        if (len(writes) < 3 or [w[1] for w in writes[:2]] != expected
                or writes[0][2] != 'file' or any(w[2] != 'append' for w in writes[1:])):
            raise ValueError('Table '+table['file']+' lacks matching headers/rows or is overwritten; use emit_table')
