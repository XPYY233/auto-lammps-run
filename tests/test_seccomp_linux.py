"""Linux kernel checks using synthetic system calls only; no simulation engine."""
import platform
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64',
                     'Linux x86-64 filter test runs in the public Linux CI')
class SeccompLinuxTests(unittest.TestCase):
    def test_compiled_filter_blocks_namespace_calls_in_disposable_process(self):
        code = r'''
import ctypes, ctypes.util, errno, os, tempfile
from pathlib import Path
from auto_lammps.runtime_launcher import namespace_filter
name = ctypes.util.find_library('seccomp')
assert name, 'Linux validation requires the installed libseccomp runtime'
with namespace_filter(name) as (library, context):
    with tempfile.TemporaryFile() as output:
        assert library.seccomp_export_bpf(context, output.fileno()) == 0
        assert 0 < os.fstat(output.fileno()).st_size <= 65536
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    assert library.seccomp_load(context) == 0
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    for call, arguments, expected in (
        (b'unshare', [ctypes.c_ulong(0x10000000)], errno.EPERM),
        (b'setns', [ctypes.c_int(-1), ctypes.c_int(0)], errno.EPERM),
        (b'clone3', [ctypes.c_void_p(), ctypes.c_size_t(0)], errno.ENOSYS),
        (b'clone', [ctypes.c_ulong(0x10000000 | 17), ctypes.c_void_p(),
                    ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_ulong(0)], errno.EPERM),
    ):
        number = library.seccomp_syscall_resolve_name(call)
        ctypes.set_errno(0)
        result = libc.syscall(ctypes.c_long(number), *arguments)
        if call == b'clone' and result == 0: os._exit(19)
        if call == b'clone' and result > 0: os.waitpid(result, 0)
        assert result == -1 and ctypes.get_errno() == expected, (call, result, ctypes.get_errno())
# Normal child creation must still work: no process limit is falsely claimed.
assert __import__('subprocess').run(['/bin/true'], check=False).returncode == 0
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
