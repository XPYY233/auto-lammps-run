"""Reference-side text adaptation, not an engine, scientific check or Agent tool.

The caller retains originals and receipt privately, freezes the returned script
with its declared assets, and obtains ordinary reference execution authorization.
"""
from dataclasses import dataclass
import re

from .manifest import canonical, relative_name, sha256


class ReferencePathError(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceAdaptation:
    script: bytes
    originals: dict
    receipt: dict


# Deliberately a small literal-token reader, not a general LAMMPS parser.
TOKEN = re.compile(r'''"[^"\n]*"|'[^'\n]*'|[^\s"'#]+''')
PASS = {'clear', 'units', 'atom_style', 'boundary', 'box', 'lattice', 'region',
        'create_box', 'create_atoms', 'mass', 'pair_style', 'pair_coeff', 'neighbor',
        'neigh_modify', 'min_style', 'min_modify', 'minimize', 'thermo', 'thermo_style',
        'thermo_modify', 'unfix', 'reset_timestep', 'change_box', 'displace_atoms',
        'group', 'velocity', 'timestep'}
FIXES = {'box/relax', 'nve', 'nvt', 'npt', 'setforce', 'momentum'}
RESERVED = {'stdout.txt', 'stderr.txt', 'log.lammps'}


def _tokens(line):
    result, pos = [], 0
    while pos < len(line):
        if line[pos].isspace():
            pos += 1
            continue
        if line[pos] == '#':
            break
        match = TOKEN.match(line, pos)
        if match is None:
            raise ReferencePathError('Unsupported or unclosed quotation')
        raw = match.group()
        if match.end() < len(line) and not line[match.end()].isspace() and line[match.end()] != '#':
            raise ReferencePathError('Quoted tokens must be separated')
        result.append((raw[1:-1] if raw[0] in "\"'" else raw, match.start(), match.end()))
        pos = match.end()
    return result


def _literal(value):
    try:
        return relative_name(value)
    except ValueError as exc:
        raise ReferencePathError('Only declared literal relative filenames are supported') from exc


def _conditional(tokens):
    """Only geometry/variable branches; never conditional file or engine commands."""
    words = [t[0] for t in tokens]
    if len(words) < 4 or words[2] != 'then':
        raise ReferencePathError('Unsupported conditional')
    commands = []
    i = 3
    while i < len(words):
        if words[i] == 'elif':
            if i + 2 >= len(words) or words[i + 2] != 'then':
                raise ReferencePathError('Unsupported elif')
            i += 3
        elif words[i] == 'else':
            i += 1
        else:
            nested = _tokens(words[i])
            if not nested or nested[0][0] not in {'variable', 'change_box'}:
                raise ReferencePathError('Conditional file access or execution is unsupported')
            _ordinary(nested)
            commands.append(words[i])
            i += 1
    if not commands:
        raise ReferencePathError('Empty conditional')


def _ordinary(tokens):
    words = [t[0] for t in tokens]
    command = words[0]
    if command == 'variable':
        if len(words) < 3 or words[2] not in {'equal', 'index', 'string', 'delete'}:
            raise ReferencePathError('Unsupported variable style')
    elif command == 'fix':
        if len(words) < 4 or words[3] not in FIXES:
            raise ReferencePathError('Unsupported reference fix style')
    elif command == 'run':
        if 'every' in words:
            raise ReferencePathError('Dynamic run commands are unsupported')
    elif command not in PASS:
        raise ReferencePathError('Unsupported reference command: ' + command[:40])


def adapt_reference_scripts(scripts, *, hashes, entrypoint, output_paths, input_files=()):
    """Inline literal includes and route explicit file operands to mounted paths.

    ``scripts`` contains rendered bytes, never Python templates to execute.
    ``output_paths`` maps each original result/state filename to a flat output
    name. Reading a mapped state before its first unconditional write requires a
    declared original input. Other file-writing commands fail rather than being
    guessed. Command screening here is NOT a sandbox or scientific verifier.
    """
    if not isinstance(scripts, dict) or not 1 <= len(scripts) <= 64 or set(scripts) != set(hashes):
        raise ReferencePathError('Exact source hashes are required')
    originals = dict(scripts)
    for name, data in originals.items():
        _literal(name)
        if (not isinstance(data, bytes) or len(data) > 1_000_000 or sha256(data) != hashes[name]
                or not data.isascii() or any(c < 32 and c not in (9, 10) for c in data)
                or b'\x7f' in data
                or b'\\' in data or b'"""' in data or b"'''" in data):
            raise ReferencePathError('Changed or unsupported reference source')
    if sum(map(len, originals.values())) > 2_000_000 or entrypoint not in originals:
        raise ReferencePathError('Missing entrypoint or excessive source size')
    if not isinstance(output_paths, dict) or not 1 <= len(output_paths) <= 29:
        raise ReferencePathError('Declare bounded reference outputs')
    for old, new in output_paths.items():
        _literal(old)
        _literal(new)
        if '/' in new or new in RESERVED or len(new) > 80:
            raise ReferencePathError('Only flat nonreserved output names are supported')
    if len(set(output_paths.values())) != len(output_paths):
        raise ReferencePathError('Output aliases may not collapse distinct files')
    inputs = set(input_files)
    for name in inputs:
        _literal(name)
    emitted, origins, changes, expansions = [], [], [], []
    written, used, read_inputs = set(), set(), set()
    size, line_count = 0, 0

    def emit(raw, source, first, last):
        nonlocal size, line_count
        # A final missing newline must not merge two included commands.
        value = raw if raw.endswith('\n') else raw + '\n'
        if value != raw:
            changes.append(dict(source=source, line=last, kind='terminate_line'))
        size += len(value)
        if size > 2_000_000 or len(emitted) >= 20000:
            raise ReferencePathError('Expanded reference exceeds bounds')
        emitted.append(value)
        origins.append(dict(source=source, first_line=first, last_line=last,
                            output_first_line=1 + line_count,
                            line_count=value.count('\n')))
        line_count += value.count('\n')

    def visit(name, stack):
        if name not in originals or name in stack or len(stack) >= 16:
            raise ReferencePathError('Missing, recursive or excessively nested include')
        used.add(name)
        lines = originals[name].decode('ascii').splitlines(keepends=True)
        i = 0
        while i < len(lines):
            first = i + 1
            raw = lines[i]
            logical = raw.rstrip('\n')
            while logical.rstrip().endswith('&'):
                i += 1
                if i == len(lines):
                    raise ReferencePathError('Dangling continuation')
                raw += lines[i]
                logical = logical.rstrip()[:-1] + lines[i].rstrip('\n')
            tokens = _tokens(logical)
            i += 1
            if not tokens:
                emit(raw, name, first, i)
                continue
            words = [t[0] for t in tokens]
            command = words[0]
            if command == 'include':
                if len(tokens) != 2:
                    raise ReferencePathError('Only literal includes are supported')
                child = _literal(words[1])
                expansions.append(dict(source=name, line=first, included=child))
                if len(expansions) > 256:
                    raise ReferencePathError('Too many include expansions')
                visit(child, (*stack, name))
                continue
            index = None
            access = None
            if command in {'read_data', 'read_restart', 'write_data', 'write_restart'}:
                if len(tokens) != 2:
                    raise ReferencePathError('File command options need separate adaptation support')
                index, access = 1, ('read' if command.startswith('read') else 'write')
            elif command == 'print':
                if len(tokens) < 2:
                    raise ReferencePathError('Missing print text')
                # Parse only options after the quoted text, not words in its message.
                k = 2
                while k < len(tokens):
                    if words[k] in {'file', 'append'} and index is None and k + 1 < len(tokens):
                        index, access = k + 1, 'write'
                    elif words[k] == 'screen' and k + 1 < len(tokens) and words[k + 1] in {'yes', 'no'}:
                        pass
                    else:
                        raise ReferencePathError('Unsupported print options')
                    k += 2
            elif command == 'if':
                _conditional(tokens)
            else:
                _ordinary(tokens)
            if index is not None:
                if i != first:
                    raise ReferencePathError('File operands must be on a single physical line')
                old = _literal(words[index])
                if access == 'write':
                    if old not in output_paths:
                        raise ReferencePathError('Undeclared output file')
                    new = '/output/' + output_paths[old]
                    written.add(old)
                elif old in written:
                    new = '/output/' + output_paths[old]
                elif old in inputs:
                    new = '/work/' + old
                    read_inputs.add(old)
                else:
                    raise ReferencePathError('State read before write or undeclared initial input')
                _, start, end = tokens[index]
                raw = raw[:start] + new + raw[end:]
                changes.append(dict(source=name, line=first, kind='file_operand', access=access,
                                    before=old, after=new, command=command))
            emit(raw, name, first, i)

    visit(entrypoint, ())
    if used != set(originals) or written != set(output_paths):
        raise ReferencePathError('Unused script or output declaration')
    script = ''.join(emitted).encode('ascii')
    receipt = dict(schema_version=1, adapter='literal_reference_paths_v1',
                   source_sha256={n: sha256(b) for n, b in sorted(originals.items())},
                   entrypoint=entrypoint, script_sha256=sha256(script),
                   output_paths=dict(output_paths), read_inputs=sorted(read_inputs),
                   expansions=expansions, changes=changes, origins=origins,
                   scientific_equivalence='not_verified', execution_authorized=False,
                   author_invocation_verified=False)
    # Ensure the receipt is serializable without implicit objects or nonfinite values.
    canonical(receipt)
    return ReferenceAdaptation(script, originals, receipt)
