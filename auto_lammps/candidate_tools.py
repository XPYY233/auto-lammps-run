"""Deterministic plan tools. No engine execution, invented data or scientific defaults."""
import re
import shlex

VERSION = 2
GUIDE = '''Adapter capabilities (version 2):
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
Parser facts: $(pe) is evaluated IMMEDIATELY before the variable command executes.
Thus capture E pe -> variable E equal $(pe) stores a numeric constant, NOT a dynamic
reference to pe. It is different from variable E equal pe. Do not repair this correct
capture into a dynamic reference. emit_table lowers to print with exact headers.
load_structure uses clear, which does NOT delete input variables. Repeated index
variables cannot be reassigned by redefining them; use distinct names or delete first.
Use numeric literal IDs for static group/delete operations. Quote the entire equal
expression when it contains spaces. A conventional cubic lattice constant is lx/nx,
not the full supercell length lx. Retain both the pre-defect and post-defect initial
structures when requested. Define computes before minimization and include their
values in thermo output so they are current; do not create a new pressure compute
after minimization and read it without initialization. Use thermo pressure keywords
already evaluated in that stage when possible.
Sources: https://docs.lammps.org/variable.html , https://docs.lammps.org/print.html ,
https://docs.lammps.org/fix_box_relax.html , https://docs.lammps.org/thermo_style.html .
https://docs.lammps.org/Commands_parse.html , https://docs.lammps.org/clear.html .
These rules are tool knowledge, not reference answers or proof of scientific success.
'''


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


def expand_tools(body, plan, prefix):
    tables = {t['file']: t for t in (plan or {}).get('tables', [])}
    initialized, result = set(), []
    for line in body.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError('Unclosed tool argument') from None
        if not words:
            result.append(line); continue
        if words[0] == 'capture':
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
                result.extend([f'print "# columns: {" ".join(c["name"] for c in cols)}" file {prefix}{name}',
                    f'print "# units: {" ".join(c["unit"] for c in cols)}" append {prefix}{name}'])
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
