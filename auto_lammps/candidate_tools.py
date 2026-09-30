"""Deterministic plan tools. No engine execution, invented data or scientific defaults."""
import re
import shlex

VERSION = 1
GUIDE = '''Adapter capabilities (version 1):
emit_table <declared-basename> "<one numerical row with LAMMPS substitutions>" writes a
table declared in analysis.plan.tables. The adapter supplies its exact columns/units
headers once, then appends rows. Use this tool for labeled numeric tables instead of
manually writing headers. The payload must have one whitespace-separated scalar per
column, with no labels or units. Choose expressions from the physical calculation;
never put expected answers into the row. The adapter never computes physics locally.
capture <variable_name> <equal-style-expression> freezes a scalar evaluated at that
stage, before another minimization/deletion changes it. It compiles to variable name
equal $(expression). Refer to saved variables as v_name inside formulas and $(...),
or ${name} in output. Bare variable names inside $(...) are not valid thermo keywords.
Use fnorm/fmax thermo keywords for force diagnostics, not max(all,fx).
minimize changes atoms only; box relaxation requires an active fix box/relax, and
unfix before a later fixed-box stage. Check each requested condition separately.
Thermo keywords and stored energies must be current for the stage being recorded.
Sources: https://docs.lammps.org/variable.html , https://docs.lammps.org/print.html ,
https://docs.lammps.org/fix_box_relax.html , https://docs.lammps.org/thermo_style.html .
These rules are tool knowledge, not reference answers or proof of scientific success.
'''


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
            if (len(words) != 3 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', words[1])
                    or not re.fullmatch(r'[A-Za-z0-9_+*/().,\[\]<>!=:\-]+', words[2])):
                raise ValueError('capture requires a safe name and one scalar expression without spaces')
            result.append(f'variable {words[1]} equal $({words[2]})')
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
