"""Synthetic bootstrap traversal, fixed diagnostics, and actual local pipes."""
import base64
import json
import os
import shlex
import stat
import sys
import unittest

from auto_lammps.author_source_reader import bootstrap_diagnostic
from auto_lammps.slurm_read import _capture
import test_author_source_reader as standard_fixture
import test_legacy_author_source_reader as legacy_fixture


class AuthorSourceBootstrapTests(unittest.TestCase):
    def fixtures(self):
        for kind, factory in (('standard', standard_fixture.AuthorSourceReaderTests),
                              ('legacy', legacy_fixture.LegacyAuthorSourceReaderTests)):
            fx = factory()
            fx.setUp()
            self.addCleanup(fx.doCleanups)
            yield kind, fx

    def read(self, kind, fx, capture, selections=None):
        reader = fx.service(capture=capture)
        chosen = selections or [dict(path='README.md', start_line=1, end_line=2)]
        if kind == 'legacy':
            return fx.read(service=reader, selections=chosen)
        return reader.read(fx.receipt, chosen, purpose='author_reference')

    def nested_source(self, kind, fx):
        previous = next(name for name in fx.texts if name.startswith('workflow/'))
        data = fx.texts.pop(previous)
        nested = fx.source/'workflow/nested'
        nested.mkdir(mode=0o755)
        name = 'workflow/nested/'+previous.split('/')[-1]
        (fx.source/previous).rename(fx.source/name)
        fx.texts[name] = data
        if kind == 'legacy':
            fx.refresh()
        else:
            fx._bind_receipt()
        (fx.source/'workflow').chmod(0o755)
        nested.chmod(0o755)
        return nested, name, [dict(path=name, start_line=1, end_line=20)]

    def local_capture(self, *, prefix='', transform=None, results=None):
        def capture(command, **options):
            remote = shlex.split(command[-1])
            self.assertEqual(remote[:3], ['python3', '-I', '-c'])
            chunks = options['input_chunks']
            if transform is not None:
                options['input_chunks'] = [transform(b''.join(chunks))]
            gate = 'import sys,os;sys.platform="linux";os.environ["SSH_CONNECTION"]="synthetic";\n'
            result = _capture([sys.executable, '-I', '-c', gate+prefix+remote[3]], **options)
            if results is not None:
                results.append(result)
            return result
        return capture

    def test_bootstrap_and_worker_traverse_ancestors_without_requesting_directory_reads(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                # The real child process emulates an execute-only shared
                # ancestor. The same guard is reached during bootstrap AND
                # during the imported worker's independent descriptor walk.
                prefix = '''
_native_open=os.open
_path_flag=1073741824
os.O_PATH=_path_flag
def _guarded_open(path,flags,*args,**kwargs):
 if str(path)==ANCESTOR_NAME and flags&os.O_DIRECTORY and not flags&_path_flag:
  raise PermissionError('PRIVATE_ANCESTOR_CANARY')
 if str(path) in FINAL_NAMES and flags&os.O_DIRECTORY and flags&_path_flag:
  raise RuntimeError('PRIVATE_FINAL_MUST_BE_READABLE_CANARY')
 return _native_open(path,flags&~_path_flag,*args,**kwargs)
os.open=_guarded_open
'''.replace('ANCESTOR_NAME', repr(fx.root.name)).replace(
                    'FINAL_NAMES', repr((fx.remote.name, 'source', 'workflow')))
                results = []
                report = self.read(kind, fx, self.local_capture(prefix=prefix, results=results))
                self.assertEqual(report['state'], 'read', report)
                self.assertEqual(results[0]['returncode'], 0)
                self.assertEqual(results[0]['failure'], '')
                self.assertEqual(base64.b64decode(results[0]['stderr']), b'')
                self.assertTrue(report['files'][0]['text'])

    def test_without_O_PATH_both_walks_keep_the_readonly_fallback(self):
        prefix = '''
if hasattr(os,'O_PATH'):delattr(os,'O_PATH')
_native_open=os.open
def _readonly_directory(path,flags,*args,**kwargs):
 if flags&os.O_DIRECTORY and flags&(os.O_WRONLY|os.O_RDWR):
  raise RuntimeError('PRIVATE_WRITE_ACCESS_CANARY')
 return _native_open(path,flags,*args,**kwargs)
os.open=_readonly_directory
'''
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                report = self.read(kind, fx, self.local_capture(prefix=prefix))
                self.assertEqual(report['state'], 'read', report)

    def test_owned_755_nested_directories_inside_private_source_are_read_without_changes(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                nested, name, chosen = self.nested_source(kind, fx)
                before = {path: (path.stat().st_mode, path.read_bytes() if path.is_file() else None)
                          for path in fx.source.rglob('*')}
                results = []
                report = self.read(kind, fx, self.local_capture(results=results), chosen)
                self.assertEqual(results[0]['returncode'], 0)
                self.assertEqual(report['state'], 'read', report)
                self.assertEqual(report['files'][0]['text'].encode(), fx.texts[name])
                self.assertFalse(report['files'][0]['truncated'])
                self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(fx.source.stat().st_mode), 0o700)
                for path, expected in before.items():
                    self.assertEqual((path.stat().st_mode, path.read_bytes() if path.is_file() else None), expected)

    def test_owned_group_writable_inside_private_source_reads_but_world_write_is_rejected(self):
        for kind, fx in self.fixtures():
            nested, _, chosen = self.nested_source(kind, fx)
            for mode in (0o775, 0o777):
                with self.subTest(reader=kind, mode=oct(mode)):
                    nested.chmod(mode)
                    report = self.read(kind, fx, self.local_capture(), chosen)
                    self.assertEqual(report['state'], 'read' if mode==0o775 else 'rejected')
                    if mode==0o775:
                        self.assertEqual(report['files'][0]['text'].encode(), fx.texts[chosen[0]['path']])
                    else:
                        self.assertEqual(report['rejection_reason'], 'source_subdirectory_world_writable')
                        self.assertEqual(report['files'], [])
                    self.assertEqual(stat.S_IMODE(fx.source.stat().st_mode),0o700)
                    self.assertEqual(stat.S_IMODE(nested.stat().st_mode), mode)

    def test_unowned_nested_directory_is_rejected_without_changing_ownership(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                nested, _, chosen = self.nested_source(kind, fx)
                original = nested.stat()
                prefix = '''
_native_fstat=os.fstat
def _different_owner(fd):
 info=_native_fstat(fd)
 if (info.st_dev,info.st_ino)==TARGET_INODE:
  fields=list(info);fields[4]=info.st_uid+1
  return os.stat_result(fields)
 return info
os.fstat=_different_owner
'''.replace('TARGET_INODE', repr((original.st_dev, original.st_ino)))
                report = self.read(kind, fx, self.local_capture(prefix=prefix), chosen)
                self.assertEqual(report['state'], 'rejected')
                self.assertEqual(report['rejection_reason'], 'source_subdirectory_owner_mismatch')
                self.assertEqual(nested.stat().st_uid, original.st_uid)

    def test_nested_symlinks_remain_rejected(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                nested, _, chosen = self.nested_source(kind, fx)
                moved = nested.with_name('original-nested')
                nested.rename(moved)
                nested.symlink_to(moved, target_is_directory=True)
                report = self.read(kind, fx, self.local_capture(), chosen)
                self.assertEqual(report['state'], 'rejected')
                self.assertEqual(report['files'], [])
                self.assertTrue(nested.is_symlink())

    def test_source_root_and_final_file_still_require_private_permissions(self):
        for kind, fx in self.fixtures():
            nested, name, chosen = self.nested_source(kind, fx)
            with self.subTest(reader=kind, object='source root'):
                fx.source.chmod(0o755)
                report = self.read(kind, fx, self.local_capture(), chosen)
                self.assertEqual(report['state'], 'rejected')
                self.assertEqual(report['rejection_reason'], 'private_owned_source_directory_required')
                self.assertEqual(stat.S_IMODE(fx.source.stat().st_mode), 0o755)
            fx.source.chmod(0o700)
            with self.subTest(reader=kind, object='final file'):
                (fx.source/name).chmod(0o644)
                report = self.read(kind, fx, self.local_capture(), chosen)
                self.assertEqual(report['state'], 'rejected')
                self.assertEqual(report['rejection_reason'], 'source_file_type_owner_or_size_mismatch')
                self.assertEqual(report['files'], [])
                self.assertEqual(stat.S_IMODE((fx.source/name).stat().st_mode), 0o644)
                self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o755)

    def test_actual_process_root_open_failure_has_finite_safe_diagnostic_and_no_traceback(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                prefix = '''
_native_open=os.open
def _denied(path,flags,*args,**kwargs):
 if str(path)==ANCESTOR_NAME and flags&os.O_DIRECTORY:
  raise PermissionError('PRIVATE_PATH_SOURCE_CREDENTIAL_CANARY')
 return _native_open(path,flags,*args,**kwargs)
os.open=_denied
'''.replace('ANCESTOR_NAME', repr(fx.root.name))
                results = []
                report = self.read(kind, fx, self.local_capture(prefix=prefix, results=results))
                self.assertEqual(report['state'], 'unknown')
                self.assertEqual(report['files'], [])
                self.assertEqual(report['bootstrap_failure'], dict(stage='root_open', exception_type='PermissionError',
                    code='source_bootstrap_root_open_PermissionError'))
                self.assertEqual(report['observation_failure'], 'source_bootstrap_root_open_PermissionError')
                self.assertEqual(results[0]['returncode'], 1)
                self.assertEqual(results[0]['failure'], 'command_failed')
                self.assertEqual(base64.b64decode(results[0]['stdout']), b'')
                self.assertEqual(base64.b64decode(results[0]['stderr']),
                    b'ALAMMPS_SOURCE_BOOTSTRAP_V1|root_open|PermissionError\n')
                for path in (fx.root/'audit').rglob('*.json'):
                    value = path.read_text()
                    self.assertNotIn('PRIVATE_PATH_SOURCE_CREDENTIAL_CANARY', value)
                    self.assertNotIn('Traceback', value)
                    self.assertNotIn('stderr', json.loads(value))
                audit = json.loads(next((fx.root/'audit').rglob('*-result.json')).read_bytes())
                self.assertEqual(audit['bootstrap_failure'], report['bootstrap_failure'])

    def test_final_root_permissions_are_not_relaxed(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                fx.remote.chmod(0o755)
                report = self.read(kind, fx, self.local_capture())
                self.assertEqual(report['state'], 'unknown')
                self.assertEqual(report['bootstrap_failure']['stage'], 'root_permissions')
                self.assertEqual(report['bootstrap_failure']['exception_type'], 'ValueError')
                self.assertEqual(report['files'], [])

    def test_envelope_decode_failure_also_uses_the_same_safe_process_protocol(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                results = []
                report = self.read(kind, fx, self.local_capture(
                    transform=lambda raw:b'{PRIVATE_INVALID_ENVELOPE_CANARY', results=results))
                self.assertEqual(report['state'], 'unknown')
                self.assertEqual(report['bootstrap_failure']['stage'], 'envelope_decode')
                self.assertEqual(report['bootstrap_failure']['exception_type'], 'JSONDecodeError')
                self.assertNotIn('PRIVATE_INVALID_ENVELOPE_CANARY', base64.b64decode(results[0]['stderr']).decode())

    def test_nonallowlisted_exception_names_are_reduced_to_Exception(self):
        for kind, fx in self.fixtures():
            with self.subTest(reader=kind):
                prefix = '''
_native_open=os.open
class PRIVATE_EXCEPTION_NAME_CANARY(Exception):pass
def _denied(path,flags,*args,**kwargs):
 if str(path)==ANCESTOR_NAME and flags&os.O_DIRECTORY:
  raise PRIVATE_EXCEPTION_NAME_CANARY('PRIVATE_MESSAGE_CANARY')
 return _native_open(path,flags,*args,**kwargs)
os.open=_denied
'''.replace('ANCESTOR_NAME', repr(fx.root.name))
                results = []
                report = self.read(kind, fx, self.local_capture(prefix=prefix, results=results))
                self.assertEqual(report['bootstrap_failure']['exception_type'], 'Exception')
                self.assertNotIn('CANARY', base64.b64decode(results[0]['stderr']).decode())

    def test_arbitrary_stderr_and_extended_markers_never_become_audit_diagnostics(self):
        values = (b'PRIVATE_SOURCE_CANARY',
            b'ALAMMPS_SOURCE_BOOTSTRAP_V1|root_open|PermissionError|PRIVATE_PATH_CANARY\n',
            b'ALAMMPS_SOURCE_BOOTSTRAP_V1|PRIVATE_STAGE_CANARY|PermissionError\n',
            b'ALAMMPS_SOURCE_BOOTSTRAP_V1|root_open|PRIVATE_EXCEPTION_CANARY\n',
            b'ALAMMPS_SOURCE_BOOTSTRAP_V1|root_open|PermissionError\n\n',
            b'ALAMMPS_SOURCE_BOOTSTRAP_V1|root_open|PermissionError\nPRIVATE_PATH_CANARY',
            b'x'*1000)
        for raw in values:
            transport = dict(returncode=1, failure='command_failed', stdout='',
                             stderr=base64.b64encode(raw).decode())
            self.assertIsNone(bootstrap_diagnostic(transport))
            for kind, fx in self.fixtures():
                with self.subTest(reader=kind, raw=raw[:80]):
                    report = self.read(kind, fx, lambda *args, **kwargs:transport)
                    self.assertEqual(report['observation_failure'], 'command_failed')
                    self.assertNotIn('bootstrap_failure', report)
                    for path in (fx.root/'audit').rglob('*.json'):
                        self.assertNotIn('CANARY', path.read_text())


if __name__ == '__main__':
    unittest.main()
