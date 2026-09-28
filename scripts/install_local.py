"""Install an isolated, non-editable local application and verify its tools.

Requires Python 3.11+ and network access on first installation. Existing paths are
never overwritten; keep the prior runtime for rollback and select a new directory.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description='Install Auto-LAMMPS with its analysis tools')
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--install-system-dependencies', action='store_true',
                        help='Install declared Debian/Ubuntu graphics runtime using apt (requires admin rights)')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or later is required')
    root = Path(__file__).resolve().parents[1]
    runtime = args.runtime.expanduser().absolute()
    if runtime.exists() or runtime.is_symlink():
        parser.error('Runtime already exists; choose a new directory to preserve rollback')
    if runtime.resolve().is_relative_to(root):
        parser.error('Keep installed runtime and private data outside the source checkout')
    if args.install_system_dependencies:
        if sys.platform != 'linux':
            parser.error('System dependency installation is only supported on Debian/Ubuntu')
        release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                       if '=' in line)
        if release.get('ID', '').strip('\"') not in {'debian', 'ubuntu'}:
            parser.error('This Linux distribution requires its native graphics library installer')
        prefix = [] if os.geteuid() == 0 else ['sudo']
        subprocess.run(prefix + ['apt-get', 'update'], check=True)
        subprocess.run(prefix + ['apt-get', 'install', '-y', '--no-install-recommends',
                                 'libgl1', 'libegl1', 'libopengl0', 'libxkbcommon0',
                                 'libdbus-1-3'], check=True)
    os.umask(0o077)
    runtime.mkdir(parents=True, mode=0o700)
    env = dict(os.environ)
    # Neither installation nor acceptance may inherit developer import overrides.
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env['PYTHONNOUSERSITE'] = '1'
    if sys.platform == 'linux':
        details = subprocess.run(['dpkg-query', '-W', 'libgl1', 'libegl1', 'libopengl0',
                                  'libxkbcommon0', 'libdbus-1-3'],
                                 capture_output=True, text=True, check=False) if args.install_system_dependencies else None
        if details is not None:
            (runtime / 'system-dependencies.txt').write_text(details.stdout, encoding='utf-8')
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(runtime)
    python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-I', '-m', 'pip', 'install',
                    '--report', str(runtime / 'install-receipt.json'),
                    str(root) + '[web,geometry,analysis]'], check=True, env=env, cwd=runtime)
    subprocess.run([str(python), '-I', '-m', 'pip', 'check'], check=True, env=env, cwd=runtime)
    resolved = subprocess.check_output([str(python), '-I', '-m', 'pip', 'list', '--format=json'],
                                       env=env, cwd=runtime, text=True)
    (runtime / 'installed-packages.json').write_text(resolved, encoding='utf-8')
    check = subprocess.run([str(python), '-I', '-X', 'faulthandler', '-m', 'auto_lammps.analysis_runtime', '--smoke'],
                           env=env, cwd=runtime, capture_output=True, text=True, timeout=120)
    (runtime / 'analysis-check.json').write_text(check.stdout, encoding='utf-8')
    (runtime / 'analysis-check.stderr.txt').write_text(check.stderr, encoding='utf-8')
    (runtime / 'analysis-process.json').write_text(json.dumps({'returncode': check.returncode}), encoding='utf-8')
    if check.returncode:
        print('Analysis verification failed. Installation retained for diagnosis; not ready.', file=sys.stderr)
        return 1
    report = json.loads(check.stdout)
    if not report.get('ready'):
        return 1
    print('Application and analysis tools installed and checked.')
    print('Start with this interpreter: ' + str(python))
    print('Module: -m auto_lammps.web --data-directory YOUR_PRIVATE_DATA_DIRECTORY --port 8785')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
