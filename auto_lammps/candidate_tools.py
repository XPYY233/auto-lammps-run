"""Deterministic plan tools. No engine execution, invented data or scientific defaults."""
import re
import shlex
import math
from pathlib import Path
from .manifest import sha256

VERSION = 3
MAX_CYCLES = 1000000
MAX_CYCLE_BLOCKS = 64
MAX_SAMPLE_TYPES = 64
_SAFE_ID = r'[A-Za-z][A-Za-z0-9_]{0,23}'
_RAW_CONTROL = {'label', 'next', 'jump', 'clear', 'include', 'shell', 'if'}
_CYCLE_STATE_RESETS = {'load_structure', 'reset_structure', 'delete_atoms',
                       'change_box', 'reset_timestep', 'displace_atoms'}

GUIDE = '''Adapter capabilities (version 3):
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


def workflow_tool_context():
    """Expose trusted operations proactively and pin their implementation."""
    return {'name': 'scientific_workflow_tools', 'version': VERSION,
            'source_sha256': sha256(Path(__file__).read_bytes()),
            'operations': ['capture', 'emit_table', 'begin_cycle', 'end_cycle',
                           'sample_swap_types'],
            'limits': {'cycle_blocks': MAX_CYCLE_BLOCKS, 'cycles_per_block': MAX_CYCLES},
            'cycle_nesting': False, 'native_control_owner': 'trusted_compiler',
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


def cycle_metadata(body, *, max_cycles=MAX_CYCLES):
    """Inspect virtual cycles, without executing or evaluating scientific expressions.

    Line numbers are one-based. Counts concern target calculation commands, including
    run 0, not submissions or authorization. Only this compiler owns native controls.
    """
    if not isinstance(body, str):
        raise ValueError('Cycle workflow must be text')
    if type(max_cycles) is not int or not 1 <= max_cycles <= 2147483647:
        raise ValueError('Invalid technical cycle bound')
    cycles, records, line_multipliers = [], [], {}
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
        if command in {'run', 'minimize'}:
            multiplier = active['count'] if active else 1
            calculation_commands += multiplier
            if active is not None:
                active['calculation_commands_per_cycle'] += 1
    if active is not None:
        raise ValueError('Unclosed scientific cycle '+active['id'])
    for number, content, words, cycle_id in records:
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
                sampling_seed=seed)


def _table_headers(name, tables, prefix):
    cols = tables[name]['columns']
    return [f'print "# columns: {" ".join(c["name"] for c in cols)}" file {prefix}{name}',
            f'print "# units: {" ".join(c["unit"] for c in cols)}" append {prefix}{name}']


def expand_tools(body, plan, prefix, *, lower_cycles=False):
    metadata = cycle_metadata(body)
    tables = {t['file']: t for t in (plan or {}).get('tables', [])}
    initialized, result = set(), []
    starts = {cycle['begin_line']: cycle for cycle in metadata['cycles']}
    ends = {cycle['end_line']: cycle for cycle in metadata['cycles']}
    samples = {sample['line']: sample for cycle in metadata['cycles'] for sample in cycle['samples']}
    lines = body.splitlines()
    for number, line in enumerate(lines, 1):
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
            if lower_cycles:
                result.extend([f'variable __alr_cycle_{cycle["id"]} loop {cycle["count"]}',
                               f'label __alr_label_{cycle["id"]}'])
            else:
                result.append(line)
        elif words[0] == 'end_cycle':
            cycle = ends[number]
            if lower_cycles:
                for sample in cycle['samples']:
                    result.extend([f'variable {name} delete' for name in sample['variables']])
                    result.append(f'variable __alr_draw_{sample["prefix"]} delete')
                result.extend([f'next __alr_cycle_{cycle["id"]}',
                               f'jump SELF __alr_label_{cycle["id"]}'])
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
