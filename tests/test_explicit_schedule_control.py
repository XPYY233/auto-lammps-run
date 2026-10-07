"""Offline control tracing only: no engine, force, energy, SSH, or model calls."""
import ast
from collections import Counter
from copy import deepcopy
from itertools import product
import operator
import re
import shlex
import unittest

from auto_lammps.candidate_tools import expand_tools
import test_state_site_scan_candidates as fixture


class ControlQuit(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Compiler guard quit {code}")


class ControlTrace:
    """A small input-control interpreter, never a scientific engine.

    Thermodynamic symbols are fixed dummy zeros. A minimize only records its
    control position and adds seven fake timesteps, so timestep changes during
    scans cannot accidentally stand in for saved-state selection.
    """
    _NO_OP = {'units', 'atom_style', 'atom_modify', 'boundary', 'pair_style',
              'pair_coeff', 'fix', 'unfix', 'velocity', 'min_style', 'thermo',
              'thermo_style', 'thermo_modify'}
    _BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
               ast.Mult: operator.mul, ast.Div: operator.truediv}
    _COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne,
                ast.Lt: operator.lt, ast.LtE: operator.le,
                ast.Gt: operator.gt, ast.GtE: operator.ge}

    def __init__(self, *, start_step=0, expected_steps=None):
        self.step = start_step
        self.variables = {}
        self.initial_atoms = {1: 1, 2: 2, 3: 1}
        self.atoms = deepcopy(self.initial_atoms)
        self.groups = {}
        self.dumps = {}
        self.restarts = {}
        self.runs = []
        self.dump_events = []
        self.read_events = []
        self.minimizations = []
        self.mutations = []
        self.rows = []
        self.clear_variables = []
        self.source_step = None
        self.marker = None
        self.skip_jump = False
        self.expected_steps = expected_steps

    def _value(self, name):
        if name not in self.variables:
            raise AssertionError(f"Undefined input variable: {name}")
        value = self.variables[name]
        if value['style'] == 'equal':
            return self._expression(value['formula'])
        return value['values'][value['position']]

    def _expression(self, expression):
        node = ast.parse(expression.replace('&&', ' and ').replace('||', ' or '),
                         mode='eval').body

        def evaluate(item):
            if isinstance(item, ast.Constant) and type(item.value) in (int, float):
                return item.value
            if isinstance(item, ast.Name):
                if item.id.startswith('v_'):
                    return float(self._value(item.id[2:]))
                if item.id == 'step':
                    return self.step
                if item.id in {'pe', 'vol', 'press', 'fmax', 'fnorm'}:
                    return 0  # Synthetic placeholders, never calculated physics.
            if isinstance(item, ast.BinOp) and type(item.op) in self._BINARY:
                return self._BINARY[type(item.op)](evaluate(item.left), evaluate(item.right))
            if isinstance(item, ast.UnaryOp) and isinstance(item.op, (ast.UAdd, ast.USub)):
                return evaluate(item.operand) * (-1 if isinstance(item.op, ast.USub) else 1)
            if isinstance(item, ast.Compare):
                left = evaluate(item.left)
                for operation, right_node in zip(item.ops, item.comparators):
                    right = evaluate(right_node)
                    if type(operation) not in self._COMPARE:
                        break
                    if not self._COMPARE[type(operation)](left, right):
                        return 0
                    left = right
                else:
                    return 1
            if isinstance(item, ast.BoolOp):
                values = [bool(evaluate(value)) for value in item.values]
                return int(all(values) if isinstance(item.op, ast.And) else any(values))
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and len(item.args) == 1:
                if item.func.id == 'abs':
                    return abs(evaluate(item.args[0]))
                if item.func.id == 'count' and isinstance(item.args[0], ast.Name):
                    group = item.args[0].id
                    return len(self.atoms) if group == 'all' else len(self.groups[group] & self.atoms.keys())
            if (isinstance(item, ast.Subscript) and isinstance(item.value, ast.Name)
                    and item.value.id == 'type'):
                return self.atoms[int(evaluate(item.slice))]
            raise AssertionError(f"Unsupported fake-control expression: {expression}")

        return evaluate(node)

    def _substitute(self, line):
        result = []
        index = 0
        while index < len(line):
            if line.startswith('${', index):
                end = line.index('}', index + 2)
                result.append(str(self._value(line[index + 2:end])))
                index = end + 1
            elif line.startswith('$(', index):
                end, depth = index + 2, 1
                while depth and end < len(line):
                    depth += (line[end] == '(') - (line[end] == ')')
                    end += 1
                if depth:
                    raise AssertionError('Unclosed immediate expression')
                expression = line[index + 2:end - 1]
                expression, separator, format_string = expression.rpartition(':')
                if not separator:
                    expression, format_string = format_string, '%.17g'
                result.append(format_string % self._expression(expression))
                index = end
            else:
                result.append(line[index])
                index += 1
        return ''.join(result)

    def _variable(self, words):
        name, style = words[1:3]
        if style == 'delete':
            self.variables.pop(name, None)
        elif style == 'equal':
            self.variables[name] = {'style': style, 'formula': ' '.join(words[3:])}
        elif style in {'index', 'loop'}:
            # Existing index/loop definitions are ignored, even after clear.
            if name not in self.variables:
                values = words[3:] if style == 'index' else list(range(1, int(words[3]) + 1))
                self.variables[name] = {'style': style, 'values': values, 'position': 0}
        else:
            raise AssertionError(f"Unsupported fake variable style: {style}")

    def run(self, script):
        lines = script.splitlines()
        labels = {words[1]: index for index, line in enumerate(lines)
                  if (words := shlex.split(line, comments=True)) and words[0] == 'label'}
        position = executed = 0
        while position < len(lines):
            executed += 1
            if executed > 20000:
                raise AssertionError('Unbounded fake-control loop')
            words = shlex.split(self._substitute(lines[position]), comments=True)
            position += 1
            if not words:
                continue
            command = words[0]
            if command == 'variable':
                self._variable(words)
            elif command == 'next':
                styles = {self.variables[name]['style'] for name in words[1:]}
                if len(styles) != 1 or not styles <= {'index', 'loop'}:
                    raise AssertionError('Mixed or unsupported next variable styles')
                for name in words[1:]:
                    variable = self.variables[name]
                    variable['position'] += 1
                    if variable['position'] == len(variable['values']):
                        del self.variables[name]
                        self.skip_jump = True
            elif command == 'jump':
                if self.skip_jump:
                    self.skip_jump = False
                else:
                    if words[1] != 'SELF':
                        raise AssertionError('Only compiler jump SELF is supported')
                    position = labels[words[2]] + 1
            elif command == 'label':
                pass
            elif command == 'clear':
                self.clear_variables.append(deepcopy(self.variables))
                self.atoms, self.groups, self.step = {}, {}, 0
            elif command == 'run':
                if not re.fullmatch(r'[0-9]+', words[1]):
                    raise AssertionError('Fake runs require literal steps')
                self.runs.append(int(words[1]))
                self.step += int(words[1])
            elif command == 'reset_timestep':
                self.step = int(words[1])
            elif command == 'read_data':
                self.atoms = deepcopy(self.initial_atoms)
            elif command == 'write_dump':
                filename = words[3]
                states = self.dumps.setdefault(filename, {})
                if self.step in states:
                    raise AssertionError('Duplicate saved timestep')
                states[self.step] = deepcopy(self.atoms)
                self.dump_events.append((filename, self.step))
            elif command == 'read_dump':
                filename, step = words[1], int(words[2])
                if words[-6:] != ['box', 'yes', 'purge', 'yes', 'add', 'keep']:
                    raise AssertionError('Incomplete saved-state restoration')
                if step not in self.dumps.get(filename, {}):
                    raise AssertionError('Read timestep absent from saved dump')
                self.atoms = deepcopy(self.dumps[filename][step])
                self.step = self.source_step = step
                self.read_events.append((filename, step))
            elif command == 'write_restart':
                self.restarts[words[1]] = (deepcopy(self.atoms), self.step, self.source_step)
            elif command == 'read_restart':
                self.atoms, self.step, self.source_step = deepcopy(self.restarts[words[1]])
            elif command == 'group':
                name, style = words[1:3]
                if style == 'delete':
                    self.groups.pop(name, None)
                else:
                    ids = ({int(words[3])} if style == 'id' else
                           {atom for atom, atom_type in self.atoms.items() if atom_type == int(words[3])})
                    self.groups.setdefault(name, set()).update(ids)
            elif command == 'set':
                self.mutations.append((self.source_step, deepcopy(self.atoms)))
                self.atoms[int(words[2])] = int(words[4])
            elif command == 'delete_atoms':
                if words[-2:] != ['compress', 'no']:
                    raise AssertionError('Vacancy must preserve remaining IDs')
                self.mutations.append((self.source_step, deepcopy(self.atoms)))
                for atom in self.groups[words[2]]:
                    self.atoms.pop(atom, None)
            elif command == 'minimize':
                self.minimizations.append((self.marker, self.source_step, deepcopy(self.atoms)))
                self.step += 7  # Fake minimizer iteration accounting only.
            elif command == 'print':
                if words[1].startswith('AUTO_LAMMPS_SCAN '):
                    marker = words[1].split()
                    self.marker = (int(marker[3]), int(marker[5]), int(marker[7]))
                    if (self.expected_steps is not None
                            and self.source_step != self.expected_steps[self.marker[0] - 1]):
                        raise AssertionError('Scan state/timestep are not synchronized')
                elif 'append' in words[2:] and not words[1].startswith('#'):
                    self.rows.append(words[1].split())
            elif command == 'if':
                if self._expression(words[1]):
                    action = shlex.split(words[3])
                    if action[0] != 'quit':
                        raise AssertionError('Unsupported fake guard action')
                    raise ControlQuit(int(action[1]))
            elif command not in self._NO_OP:
                raise AssertionError(f"Unsupported fake-control command: {command}")
        return self


class ExplicitScheduleControlTests(unittest.TestCase):
    STEPS = [10, 30, 90]

    def script(self):
        spec = fixture.specification()
        spec['variants'] = [{'id': 7, 'type': 1}, {'id': 11, 'type': 2}, {'id': 99, 'type': None}]
        body = fixture.workflow(spec, count=3).replace(
            'run 1000\nsave_state saved states.dump 1000 1000',
            "run_schedule saved 0 '[10,30,90]'\nsave_state saved states.dump steps '[10,30,90]'")
        return expand_tools('capture retained 17\n' + body, fixture.plan(spec), '/output/',
                            lower_cycles=True, reload_header=fixture.HEADER)

    def test_nonuniform_saved_and_loaded_steps_cover_every_state_site_variant(self):
        trace = ControlTrace(expected_steps=self.STEPS).run(self.script())
        self.assertEqual(trace.runs, [10, 20, 60])
        self.assertEqual([step for file, step in trace.dump_events if file.endswith('/states.dump')], self.STEPS)
        self.assertEqual([step for _, step in trace.read_events], self.STEPS)
        self.assertEqual([step for file, step in trace.dump_events if file.endswith('/baselines.dump')], [1, 2, 3])
        self.assertEqual(len(trace.minimizations), 30)
        self.assertEqual(Counter(marker for marker, _, _ in trace.minimizations),
                         Counter([(state, 0, -1) for state in range(1, 4)] +
                                 list(product(range(1, 4), range(1, 4), [7, 11, 99]))))
        self.assertEqual(Counter(tuple(map(int, row[:3])) for row in trace.rows),
                         Counter(product(range(1, 4), range(1, 4), [-1, 7, 11, 99])))
        self.assertEqual(len(trace.rows), 36)
        self.assertEqual(len(trace.mutations), 27)
        self.assertTrue(all(atoms == trace.initial_atoms for _, atoms in trace.mutations))
        self.assertTrue(all(len(atoms) == (2 if marker[2] == 99 else 3)
                            for marker, _, atoms in trace.minimizations))
        self.assertTrue(all(int(row[6]) == (11 if int(row[1]) == 2 else 7) for row in trace.rows))
        self.assertEqual(trace._value('retained'), 17)
        self.assertTrue(all('retained' in variables for variables in trace.clear_variables))
        self.assertFalse(any(name.endswith(('_state', '_step', '_site')) for name in trace.variables))

    def test_wrong_runtime_start_stops_before_any_run_or_save(self):
        trace = ControlTrace(start_step=1)
        with self.assertRaises(ControlQuit) as caught:
            trace.run(self.script())
        self.assertEqual(caught.exception.code, 90)
        self.assertEqual(trace.runs, [])
        self.assertEqual(trace.dump_events, [])

    def test_changed_runtime_step_stops_before_saving_an_incorrect_state(self):
        trace = ControlTrace()
        with self.assertRaises(ControlQuit) as caught:
            trace.run(self.script().replace('\nrun 10\n', '\nrun 11\n', 1))
        self.assertEqual(caught.exception.code, 90)
        self.assertEqual(trace.runs, [11])
        self.assertEqual(trace.dump_events, [])

    def test_next_rejects_mixed_index_and_loop_styles(self):
        script = self.script().replace('variable __alr_scan_complete_state index 1 2 3',
                                       'variable __alr_scan_complete_state loop 3')
        with self.assertRaisesRegex(AssertionError, 'Mixed.*next'):
            ControlTrace(expected_steps=self.STEPS).run(script)

    def test_missing_or_interrupted_synchronized_next_is_detected(self):
        next_line = 'next __alr_scan_complete_state __alr_scan_complete_step'
        replacements = ['next __alr_scan_complete_state', 'next __alr_scan_complete_step',
                        'next __alr_scan_complete_state\njump SELF __alr_scan_complete_states\n'
                        'next __alr_scan_complete_step']
        for replacement in replacements:
            with self.subTest(replacement=replacement), self.assertRaisesRegex(AssertionError, 'state/timestep'):
                ControlTrace(expected_steps=self.STEPS).run(self.script().replace(next_line, replacement))

    def test_clear_keeps_index_values_and_exhaustion_skips_one_jump(self):
        trace = ControlTrace().run('\n'.join([
            'variable value index 10 30 90', 'variable state index 1 2 3', 'label again',
            'clear', 'variable value index 999', 'print "${state} ${value} 0" append trace.dat',
            'next state value', 'jump SELF again', 'variable value index 700',
            'print "0 ${value} 0" append trace.dat']))
        self.assertEqual(trace.rows, [['1', '10', '0'], ['2', '30', '0'],
                                      ['3', '90', '0'], ['0', '700', '0']])
        self.assertNotIn('state', trace.variables)


if __name__ == '__main__':
    unittest.main()
