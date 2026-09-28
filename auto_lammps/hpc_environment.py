"""Saved-HPC inspection: read what the cluster actually has, invent nothing.

The application previously only proved that SSH worked and then planned as if no
engine existed, which sent users into building an environment that was already
installed. This module runs a fixed, read-only command set over an existing saved
connection and stores the raw results with digests, so planning can cite reality.

Boundaries:
  * every command is a read-only query; there is no scheduler submission, no
    remote write, no file removal and no tool installation here;
  * results are evidence, never permission: an inventory that lists a usable
    engine does not authorise a run;
  * outputs are bounded per command, and a failing command is recorded as failed
    instead of being retried or guessed at.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from .manifest import canonical, sha256
from .tasks import TaskError

MODULE = re.compile(r'\b(lammps/[A-Za-z0-9_.\-]+)')
VERSION = re.compile(r'LAMMPS \(([^)]+)\)')
PACKAGE = re.compile(r'^\s*([A-Z][A-Z0-9\-]{2,})\s*:', re.M)
PAIR = re.compile(r'\b(meam/c|meam|snap|sw|eam/c|eam|tersoff|airebo|comb)\b')
FORBIDDEN = ('sbatch', 'srun', 'scancel', 'salloc', 'rm ', 'mv ', 'cp ', 'mkdir', 'touch',
             'pip install', 'conda install', 'make', 'cmake', 'tee')


@dataclass(frozen=True)
class InventoryCommand:
    """One bounded read-only query. ``remote`` never contains a write or a submission."""
    name: str
    remote: str
    timeout: int = 60
    max_bytes: int = 65536

    def __post_init__(self):
        lowered = self.remote.lower()
        for token in FORBIDDEN:
            if token.strip() and re.search(r'(^|[^a-z])' + re.escape(token.strip()) + r'([^a-z]|$)', lowered):
                raise TaskError(f'清点命令不得包含写操作或提交：{self.name} -> {token.strip()}')
        # 允许 `2>&1` 与丢弃输出到 /dev/null；禁止任何会写远端文件的重定向。
        for match in re.finditer(r'>>?\s*([^\s|;&]+)', self.remote):
            if match.group(1) != '/dev/null':
                raise TaskError(f'清点命令不得写入远端文件：{self.name} -> {match.group(0)}')
        if not 1 <= self.timeout <= 600 or not 1024 <= self.max_bytes <= 4 * 1024 * 1024:
            raise TaskError('清点命令的时间或长度上限无效')


def inventory_commands(work_directory: str = ''):
    """The fixed command set. Read-only by construction and checked on creation."""
    commands = [
        InventoryCommand('hostname', 'hostname; date -u +%Y-%m-%dT%H:%M:%SZ'),
        InventoryCommand('module_avail', 'module avail lammps 2>&1 | head -60'),
        InventoryCommand('module_spider', 'module spider lammps 2>&1 | head -60'),
        InventoryCommand('lmp_on_path', 'command -v lmp || echo "(no lmp on PATH)"'),
        InventoryCommand('python', 'command -v python3 || true; python3 -V 2>&1 | head -1'),
        InventoryCommand('scheduler', 'sinfo -s 2>&1 | head -12'),
        InventoryCommand('associations',
                         'sacctmgr -nP show assoc where user=$USER format=Account,Partition 2>&1 | head -20'),
    ]
    if work_directory:
        commands.append(InventoryCommand('storage', f'df -h {work_directory} 2>&1 | head -5'))
    return commands


def module_probe_command(module: str):
    """Load one module and read its version, packages and pair styles (still read-only)."""
    if not re.fullmatch(r'lammps/[A-Za-z0-9_.\-]{1,80}', module):
        raise TaskError('模块名无效')
    return InventoryCommand(f'engine:{module}',
                            f'module purge >/dev/null 2>&1; module load {module} >/dev/null 2>&1; '
                            f'(command -v lmp || true); (lmp -h 2>&1 | head -120)')


def run_commands(runner, commands):
    """Run each command once. A failure is recorded, never retried or filled in."""
    results = []
    for command in commands:
        try:
            completed = runner(command.remote, command.timeout, command.max_bytes)
            stdout = (completed.get('stdout') or '')[:command.max_bytes]
            results.append(dict(name=command.name, remote=command.remote,
                                returncode=int(completed.get('returncode', 255)),
                                stdout=stdout, stderr=(completed.get('stderr') or '')[:4096],
                                stdout_sha256=sha256(stdout.encode()), truncated=bool(completed.get('truncated'))))
        except Exception as error:  # a dead connection must be visible, not silent
            results.append(dict(name=command.name, remote=command.remote, returncode=255,
                                stdout='', stderr=f'{type(error).__name__}: {error}'[:4096],
                                stdout_sha256=sha256(b''), truncated=False))
    return results


def discover_modules(results):
    found = []
    for item in results:
        if item['name'] not in ('module_avail', 'module_spider'):
            continue
        for name in MODULE.findall(item['stdout']):
            if name not in found:
                found.append(name)
    return found[:8]


def parse_engine(item):
    """Read one ``lmp -h`` result. Missing facts stay missing; nothing is assumed."""
    text = item['stdout']
    version = VERSION.search(text)
    packages = sorted({match.group(1) for match in PACKAGE.finditer(text)})
    pairs = sorted({match.group(1) for match in PAIR.finditer(text)})
    command_path = ''
    for line in text.splitlines():
        if line.strip().startswith('/') and line.strip().endswith('lmp'):
            command_path = line.strip()
            break
    return dict(name=item['name'], returncode=item['returncode'], version=version.group(1) if version else None,
                binary=command_path or None, installed_packages=packages, pair_styles=pairs,
                meam_c_available='meam/c' in pairs, sha256=item['stdout_sha256'])


def summarize(results):
    """A compact view a planner can be shown: which engines exist and what they can do."""
    engines = [parse_engine(item) for item in results if item['name'].startswith('engine:')]
    modules = discover_modules(results)
    usable = [engine for engine in engines if engine['returncode'] == 0 and engine['version']]
    return dict(modules_available=modules,
                engines=engines,
                usable_engines=[dict(module=engine['name'].split(':', 1)[1], version=engine['version'],
                                     binary=engine['binary'], packages=engine['installed_packages'],
                                     meam_c=engine['meam_c_available']) for engine in usable],
                lammps_present=bool(usable),
                note=('清点只读取远端信息，不提交作业、不写远端、不安装任何软件。'
                      '若这里列出可用引擎，规划不得声称"没有 LAMMPS"，也不得提议构建新引擎。'))


class HPCEnvironment:
    """Store inventory reports as immutable private records beside the task store."""

    def __init__(self, tasks, connections):
        self.tasks = tasks
        self.connections = connections
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hpc_environment_reports ('
                       'id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL, at TEXT NOT NULL)')

    def run(self, revision, runner, *, work_directory=''):
        results = run_commands(runner, inventory_commands(work_directory))
        modules = discover_modules(results)
        results += run_commands(runner, [module_probe_command(name) for name in modules])
        document = dict(schema_version=1, at=datetime.now(timezone.utc).isoformat(), revision=revision,
                        commands=[dict(name=item['name'], remote=item['remote'], returncode=item['returncode'],
                                       stdout_sha256=item['stdout_sha256']) for item in results],
                        results=results, summary=summarize(results))
        encoded = canonical(document).decode()
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO hpc_environment_reports(revision,document,at) VALUES (?,?,?)',
                       (revision, encoded, document['at']))
        return document

    def latest(self, revision=None):
        query = 'SELECT revision,document,at FROM hpc_environment_reports'
        params = ()
        if revision is not None:
            query += ' WHERE revision=?'
            params = (revision,)
        query += ' ORDER BY id DESC LIMIT 1'
        with self.tasks.transaction() as db:
            row = db.execute(query, params).fetchone()
        if row is None:
            return None
        document = json.loads(row['document'])
        return document


def ssh_runner(connections, revision):
    """A runner bound to one exact saved connection revision."""
    argv = connections.ssh_arguments(revision)

    def run(remote, timeout, max_bytes):
        completed = subprocess.run(argv + [remote], capture_output=True, timeout=timeout)
        stdout = completed.stdout[:max_bytes]
        return dict(returncode=completed.returncode, stdout=stdout.decode('utf-8', 'replace'),
                    stderr=completed.stderr.decode('utf-8', 'replace')[:4096],
                    truncated=len(completed.stdout) > max_bytes)
    return run
