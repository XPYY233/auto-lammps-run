"""Deterministic resource script for review; never invokes sbatch or an engine."""
from dataclasses import dataclass
import re
import shlex

from .manifest import sha256
from .slurm_read import identity
from .staging import absolute_path, BOOTSTRAP
from .submission import Submission


@dataclass(frozen=True)
class BatchEnvironment:
    partition: str
    account: str | None
    root_path: str
    python_path: str
    launcher_path: str
    launcher_sha256: str

    def __post_init__(self):
        for field in (self.partition, self.account):
            if field is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', field):
                raise ValueError('Invalid scheduler setting')
        if not self.partition:
            raise ValueError('A verified partition is required')
        for path in (self.root_path, self.python_path, self.launcher_path):
            absolute_path(path)
        if self.launcher_path.startswith(self.root_path + '/'):
            raise ValueError('Trusted launcher must be outside input storage')
        if not re.fullmatch(r'[a-f0-9]{64}', self.launcher_sha256):
            raise ValueError('Pin a reviewed launcher version')


@dataclass(frozen=True)
class BatchPlan:
    script: bytes
    sha256: str


def render_batch(submission: Submission, environment: BatchEnvironment):
    """One node, explicit requested CPU count, wall limit and whole-MiB RAM.

    This plan is not approval, a static science check, isolation or deployment.
    The named trusted launcher must enforce runtime isolation and output quota.
    Requested CPUs/RAM do not prove cluster allocation or cgroup enforcement.
    No launcher or actual scheduler submission is supplied by this module.
    """
    name, comment = identity(submission.request_id, submission.manifest_sha256)
    resources = submission.resources
    if resources.wall_seconds % 60:
        raise ValueError('Slurm rounds seconds upward; an exact whole-minute limit is required')
    if resources.memory_bytes % (1024 * 1024):
        raise ValueError('Memory must use exact whole MiB; implicit rounding is prohibited')
    days, remainder = divmod(resources.wall_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    walltime = f'{days}-{hours:02}:{minutes:02}:{seconds:02}'
    directory = environment.root_path + '/' + submission.request_id
    arguments = [environment.python_path, '-I', '-c', BOOTSTRAP, environment.launcher_path,
                 environment.launcher_sha256, '--request-id', submission.request_id,
                 '--manifest-sha256', submission.manifest_sha256]
    lines = ['#!/bin/sh', f'#SBATCH --job-name={name}', f'#SBATCH --comment={comment}',
             f'#SBATCH --partition={environment.partition}', '#SBATCH --nodes=1',
             f'#SBATCH --ntasks={resources.cores}', '#SBATCH --cpus-per-task=1',
             f'#SBATCH --time={walltime}', f'#SBATCH --mem={resources.memory_bytes // (1024 * 1024)}M',
             '#SBATCH --no-requeue', '#SBATCH --export=NIL',
             f'#SBATCH --chdir={directory}', f'#SBATCH --output={directory}/output/slurm-%j.stdout',
             f'#SBATCH --error={directory}/output/slurm-%j.stderr']
    if environment.account:
        lines.append(f'#SBATCH --account={environment.account}')
    lines.extend(['set -eu', 'umask 077', 'test -n "${SLURM_JOB_ID:-}" || exit 97',
                  'exec ' + shlex.join(arguments)])
    data = ('\n'.join(lines) + '\n').encode()
    return BatchPlan(data, sha256(data))
