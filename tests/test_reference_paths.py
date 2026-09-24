"""Original synthetic text only. No physics, author code execution or model calls."""
import tempfile
from pathlib import Path
import unittest

from auto_lammps.ledger import Resources
from auto_lammps.manifest import freeze, sha256
from auto_lammps.reference_paths import ReferencePathError, adapt_reference_scripts


class ReferencePathTests(unittest.TestCase):
    def setUp(self):
        self.scripts = {
            'main.in': b'# synthetic workflow\nread_data state.dat\ninclude relax.in\n'
                       b'write_data state.dat\ninclude sample.in\ninclude sample.in\n'
                       b'print "file is a word, # not a comment" file result.txt screen no\n',
            'relax.in': b'min_style cg\nminimize 0 1e-8 10 20\n',
            'sample.in': b'clear\nread_data state.dat\nif "${axis} == 1" then &\n'
                         b' "change_box all x scale 1.001 remap"\nminimize 0 1e-8 10 20\n',
        }
        self.outputs = {'state.dat': 'state.dat', 'result.txt': 'measurements.txt'}

    def adapt(self, **changes):
        kwargs = dict(hashes={n: sha256(b) for n, b in self.scripts.items()},
                      entrypoint='main.in', output_paths=self.outputs, input_files=['state.dat'])
        kwargs.update(changes)
        return adapt_reference_scripts(self.scripts, **kwargs)

    def test_initial_input_and_repeated_state_read_have_distinct_paths(self):
        result = self.adapt()
        text = result.script.decode()
        self.assertIn('read_data /work/state.dat', text)
        self.assertEqual(text.count('read_data /output/state.dat'), 2)
        self.assertIn('write_data /output/state.dat', text)
        self.assertIn('file /output/measurements.txt screen no', text)
        self.assertNotIn('include ', text)
        self.assertEqual(result.originals, self.scripts)
        self.assertEqual(len(result.receipt['expansions']), 3)
        self.assertEqual(text.count('minimize 0 1e-8 10 20'), 3)
        self.assertFalse(result.receipt['execution_authorized'])
        self.assertEqual(result.receipt['scientific_equivalence'], 'not_verified')
        for origin in result.receipt['origins']:
            self.assertIn(origin['source'], self.scripts)
        self.assertEqual(sum(o['line_count'] for o in result.receipt['origins']), text.count('\n'))
        self.assertEqual(result, self.adapt())

    def test_compiled_script_enters_existing_frozen_input_contract(self):
        result = self.adapt()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'inputs'; source.mkdir()
            (source / 'input.in').write_bytes(result.script)
            (source / 'state.dat').write_text('synthetic structure bytes, not an engine input')
            snapshot = freeze(source, root / 'snapshots',
                files={'input.in': 'lammps_input', 'state.dat': 'structure'}, entrypoint='input.in',
                resources=Resources(cores=1, wall_seconds=60, memory_bytes=1048576, storage_bytes=1048576),
                provenance={k: 'a' * 64 for k in ('task_sha256', 'analysis_sha256', 'software_sha256')})
            self.assertEqual(snapshot.verify()['entrypoint'], 'input.in')
            self.assertEqual((snapshot.path / 'input.in').read_bytes(), result.script)

    def test_hash_change_and_extra_source_rejected(self):
        hashes = {n: sha256(b) for n, b in self.scripts.items()}
        self.scripts['relax.in'] += b'# changed\n'
        with self.assertRaises(ReferencePathError): self.adapt(hashes=hashes)
        self.scripts['unused.in'] = b'clear\n'
        with self.assertRaises(ReferencePathError): self.adapt()

    def test_control_characters_and_expansion_byte_limit(self):
        for control in (b'\r', b'\v', b'\f', b'\x00', b'\x7f'):
            self.scripts['sample.in'] = b'clear' + control + b'\n'
            with self.assertRaises(ReferencePathError): self.adapt()
        self.scripts['sample.in'] = b'#' + b'x' * 999_990 + b'\n'
        self.scripts['main.in'] = b'include sample.in\n' * 3
        with self.assertRaisesRegex(ReferencePathError, 'exceeds bounds'): self.adapt()

    def test_cycles_missing_dynamic_and_excessive_includes_rejected(self):
        for body in (b'include main.in\n', b'include absent.in\n', b'include ${path}\n',
                     b'include relax.in\n' * 257):
            with self.subTest(body=body[:40]):
                self.scripts['sample.in'] = body
                with self.assertRaises(ReferencePathError): self.adapt()

    def test_unknown_outputs_and_state_before_write_rejected(self):
        with self.assertRaises(ReferencePathError): self.adapt(input_files=[])
        self.outputs['missing.txt'] = 'missing.txt'
        with self.assertRaises(ReferencePathError): self.adapt()
        del self.outputs['missing.txt']
        del self.outputs['state.dat']
        with self.assertRaises(ReferencePathError): self.adapt()

    def test_path_aliases_traversal_reserved_and_dynamic_rejected(self):
        for outputs in ({'state.dat': 'same', 'result.txt': 'same'},
                        {'state.dat': '../escape'}, {'state.dat': 'sub/file'},
                        {'state.dat': 'log.lammps'}, {'${state}': 'state.dat'}):
            with self.subTest(outputs=outputs):
                with self.assertRaises(ReferencePathError): self.adapt(output_paths=outputs)

    def test_control_flow_cannot_hide_file_operations_or_retries(self):
        for body in (b'if "1" then "write_data state.dat"\n',
                     b'if "1" then "include relax.in"\n',
                     b'if "1" then "run 10"\n', b'jump main.in\n',
                     b'run 10 every 1 "print hello"\n', b'variable x python helper\n',
                     b'fix x all print 1 hello file surprise\n', b'log surprise\n'):
            with self.subTest(body=body):
                self.scripts['sample.in'] = body
                with self.assertRaises(ReferencePathError): self.adapt()

    def test_byte_preservation_except_paths_including_quotes_and_comments(self):
        self.scripts = {'main.in': b'print  "# file fake"  append \'old.txt\' # old.txt stays\n'}
        self.outputs = {'old.txt': 'new.txt'}
        result = self.adapt(input_files=[])
        self.assertEqual(result.script, b'print  "# file fake"  append /output/new.txt # old.txt stays\n')
        self.assertEqual(result.receipt['changes'][0]['before'], 'old.txt')

    def test_final_newline_and_origin_boundaries(self):
        self.scripts = {'main.in': b'include fragment.in\nprint "ok" file old.txt',
                        'fragment.in': b'clear'}
        self.outputs = {'old.txt': 'new.txt'}
        result = self.adapt(input_files=[])
        self.assertEqual(result.script, b'clear\nprint "ok" file /output/new.txt\n')
        self.assertEqual([o['output_first_line'] for o in result.receipt['origins']], [1, 2])
        self.assertEqual(sum(c['kind'] == 'terminate_line' for c in result.receipt['changes']), 2)

    def test_restart_state_and_print_option_validation(self):
        self.scripts = {'main.in': b'write_restart state.bin\nclear\nread_restart state.bin\n'}
        self.outputs = {'state.bin': 'checkpoint.bin'}
        self.assertEqual(self.adapt(input_files=[]).script.count(b'/output/checkpoint.bin'), 2)
        for raw in (b'print "x" file old.txt append old.txt\n', b'print "x" file\n',
                    b'write_data state.bin &\n nocoeff\n', b'print "unclosed\n', b'clear\n&'):
            self.scripts['main.in'] = raw
            with self.assertRaises(ReferencePathError): self.adapt()

    def test_multiline_condition_is_preserved_and_else_checked(self):
        body = (b'if "${dir} == 1" then &\n "variable n equal 2" else &\n'
                b' "change_box all x scale 1.0 remap"\n')
        self.scripts['sample.in'] = body + b'read_data state.dat\n'
        self.assertIn(body, self.adapt().script)
        self.scripts['sample.in'] = body.replace(b'change_box all x scale 1.0 remap', b'shell echo hello')
        with self.assertRaises(ReferencePathError): self.adapt()


if __name__ == '__main__':
    unittest.main()
