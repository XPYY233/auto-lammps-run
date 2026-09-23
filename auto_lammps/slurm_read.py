"""Bounded, audited, read-only Slurm lookup. Never submits or cancels jobs.

Trusted controller API, not a runtime Agent tool or a security sandbox.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import base64
import os
import re
import selectors
import shlex
import subprocess
import time
import uuid

from .manifest import canonical, private_directory, sha256


# Adapted from SIGA-LAMMPS hpc/slurm.py at
# dd2e0e7f7a7381ff8286fd1152ae1ee4958e319b (MIT).
# Copyright (c) 2026 SIGA-LAMMPS contributors.
# See third_party/SIGA-LAMMPS-LICENSE.txt. Timeout remains distinct here.
_STATES = {
    'PENDING': 'queued', 'CONFIGURING': 'queued', 'RUNNING': 'running',
    'COMPLETING': 'running', 'SUSPENDED': 'running', 'COMPLETED': 'completed',
    'FAILED': 'failed', 'NODE_FAIL': 'failed', 'OUT_OF_MEMORY': 'failed',
    'BOOT_FAIL': 'failed', 'DEADLINE': 'failed', 'CANCELLED': 'cancelled',
    'TIMEOUT': 'timeout',
}
TERMINAL = {'completed', 'failed', 'cancelled', 'timeout'}


def normalise_state(raw):
    # Accept the documented CANCELLED by <uid> suffix, not truncated states.
    token = raw.strip().split()[0] if raw.strip() else ''
    if raw.strip() != token and not re.fullmatch(r'CANCELLED by [0-9]+', raw.strip()):
        return 'unknown'
    return _STATES.get(token, 'unknown')


def identity(request_id, manifest_sha256):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise ValueError('Invalid request identity')
    if not isinstance(manifest_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', manifest_sha256):
        raise ValueError('Invalid manifest identity')
    return 'al-' + request_id, f'al:{manifest_sha256}:{request_id}'


@dataclass(frozen=True)
class Observation:
    state: str
    job_id: str | None = None
    allocated_core_seconds: int | None = None
    reason: str = ''
    evidence_sha256: str = ''


def interpret(queue, accounting, request_id, manifest_sha256):
    """Only exact identities count. Missing/ambiguous data never releases a budget."""
    name, comment = identity(request_id, manifest_sha256)
    groups = []
    for source, text, width in [('queue', queue, 4), ('accounting', accounting, 8)]:
        rows = []
        if text and not text.endswith('\n'):
            return Observation('unknown', reason='truncated_response')
        for line in text.splitlines():
            fields = line.split('|')
            if len(fields) != width:
                return Observation('unknown', reason='malformed_response')
            job, state, job_name, job_comment = (x.strip() for x in fields[:4])
            # Ignore steps only when they belong to the named task. Root allocation
            # accounting already includes their elapsed time; never sum steps.
            if source == 'accounting' and re.fullmatch(r'[0-9]+\.[A-Za-z0-9_-]+', job):
                continue
            if job_name != name or job_comment != comment:
                return Observation('unknown', reason='identity_unverified')
            if not re.fullmatch(r'[1-9][0-9]*', job):
                return Observation('unknown', reason='unsupported_job_identity')
            mapped = normalise_state(state)
            if mapped == 'unknown':
                return Observation('unknown', job, reason='unsupported_state')
            cost = None
            if source == 'accounting':
                cores, elapsed, exit_code, restarts = (x.strip() for x in fields[4:])
                if not (cores.isascii() and cores.isdigit() and elapsed.isascii() and elapsed.isdigit()
                        and re.fullmatch(r'[0-9]+:[0-9]+', exit_code) and restarts == '0'):
                    return Observation('unknown', job, reason='accounting_incomplete_or_restarted')
                if mapped in TERMINAL:
                    if int(cores) == 0 and int(elapsed) != 0:
                        return Observation('unknown', job, reason='incomplete_allocation')
                    if mapped == 'completed' and (exit_code != '0:0' or int(cores) == 0):
                        return Observation('unknown', job, reason='inconsistent_completion')
                    cost = int(cores) * int(elapsed)
            rows.append(Observation(mapped, job, cost))
        if len(rows) > 1:
            return Observation('unknown', reason='duplicate_allocations_or_restarts')
        groups.append(rows[0] if rows else None)
    queued, recorded = groups
    if queued and recorded:
        if queued.job_id != recorded.job_id or queued.state in TERMINAL or recorded.state in TERMINAL:
            return Observation('unknown', reason='queue_accounting_conflict')
        return queued
    if queued:
        # Terminal queue states do not provide final accounting.
        return Observation('unknown', queued.job_id, reason='awaiting_accounting') if queued.state in TERMINAL else queued
    return recorded or Observation('unknown', reason='not_visible')


def _capture(argv, *, timeout, max_bytes):
    """Drain both streams under one byte/time bound; kill/reap on every failure."""
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                count = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        returncode, failure = None, 'timeout'
                        break
                    for key, _ in selector.select(min(remaining, .1)):
                        data = os.read(key.fileobj.fileno(), min(65536, max_bytes - count + 1))
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        output[key.data].extend(data)
                        count += len(data)
                    if count > max_bytes:
                        returncode, failure = None, 'output_limit'
                        break
                else:
                    try:
                        returncode = process.wait(timeout=max(.001, deadline - time.monotonic()))
                        failure = '' if returncode == 0 else 'command_failed'
                    except subprocess.TimeoutExpired:
                        returncode, failure = None, 'timeout'
        finally:
            # The OpenSSH child can have configured proxy descendants.
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    return dict(returncode=returncode, failure=failure,
                **{k: base64.b64encode(v).decode('ascii') for k, v in output.items()})


def _write_new(path, value):
    data = canonical(value)
    with path.open('xb') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return sha256(data)


class SlurmReader:
    """Fixed queries using an administrator-configured SSH alias and host keys.

    No arbitrary command input. Logs contain private cluster metadata and must
    not be published. An audit write error propagates rather than being ignored.
    """
    def __init__(self, host_alias, audit_directory, *, timeout=20, max_bytes=1_000_000):
        if not isinstance(host_alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', host_alias):
            raise ValueError('Invalid SSH alias')
        if not 0 < timeout <= 60 or type(max_bytes) is not int or not 1024 <= max_bytes <= 4_000_000:
            raise ValueError('Invalid query bounds')
        self.host_alias = host_alias
        self.audit_directory = private_directory(audit_directory)
        self.timeout, self.max_bytes = timeout, max_bytes

    def _query(self, arguments):
        path = self.audit_directory / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes',
                   '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no',
                   self.host_alias, shlex.join(['env', 'TZ=UTC', 'LC_ALL=C', *arguments])]
        intent = _write_new(path / 'intent.json', dict(argv=command, started_utc=datetime.now(timezone.utc).isoformat()))
        try:
            result = _capture(command, timeout=self.timeout, max_bytes=self.max_bytes)
        except OSError as exc:
            result = dict(returncode=None, failure=type(exc).__name__, stdout='', stderr='')
        result = {**result, 'intent_sha256': intent, 'finished_utc': datetime.now(timezone.utc).isoformat()}
        digest = _write_new(path / 'result.json', result)
        if result['failure']:
            return None, digest
        try:
            return base64.b64decode(result['stdout']).decode('utf-8', errors='strict'), digest
        except UnicodeDecodeError:
            return None, digest

    def lookup(self, request_id, manifest_sha256, *, since_utc):
        name, _ = identity(request_id, manifest_sha256)
        if not isinstance(since_utc, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', since_utc):
            raise ValueError('An explicit UTC query start is required')
        datetime.strptime(since_utc, '%Y-%m-%dT%H:%M:%S')
        queue, queue_proof = self._query(['squeue', '--local', '--me', '--noheader', f'--name={name}',
                                        '--format=%i|%T|%j|%k'])
        accounting, account_proof = self._query([
            'sacct', '--local', '--noheader', '--parsable2', '--duplicates', f'--name={name}',
            f'--starttime={since_utc}',
            '--format=JobIDRaw,State%40,JobName%80,Comment%160,AllocCPUS,ElapsedRaw,ExitCode,Restarts'])
        result = (interpret(queue, accounting, request_id, manifest_sha256)
                  if queue is not None and accounting is not None else Observation('unknown', reason='query_failed'))
        proof = dict(request_id=request_id, manifest_sha256=manifest_sha256, since_utc=since_utc,
                     queue=queue_proof, accounting=account_proof, observation=asdict(result))
        digest = _write_new(self.audit_directory / (uuid.uuid4().hex + '.json'), proof)
        return Observation(**{**asdict(result), 'evidence_sha256': digest})
