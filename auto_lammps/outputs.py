"""Accounted, streamed output retrieval. Downloads data; never executes it."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import struct
import subprocess
import time

from .ledger import Conflict, JOB_TERMINAL, Ledger
from .manifest import private_directory
from . import runtime_launcher as runtime
from .slurm_read import _write_new, identity
from .staging import BOOTSTRAP, StageEndpoint

HEADER_LIMIT = 65536
CONTROL_NAMES = {'execution-intent.json', 'execution-result.json', 'scheduler.stdout', 'scheduler.stderr'}


def output_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'output/[A-Za-z0-9][A-Za-z0-9_.-]{0,239}', name):
        raise ValueError('Invalid output path')
    return name


def validate_header(header, context):
    if not isinstance(header, dict) or set(header) != {'schema_version', 'request_id', 'manifest_sha256',
            'job_id', 'execution', 'scientific_status', 'missing_outputs', 'files'}:
        raise ValueError('Unsupported collection header')
    if header['schema_version'] != 1 or any(header[key] != context[key] for key in
            ('request_id', 'manifest_sha256', 'job_id')) or header['scientific_status'] != 'not_evaluated':
        raise ValueError('Collection identity or scientific status mismatch')
    execution = header['execution']
    if not isinstance(execution, dict) or set(execution) != {'state', 'returncode', 'timed_out'}:
        raise ValueError('Invalid execution summary')
    if execution['state'] == 'finished':
        if type(execution['returncode']) is not int or type(execution['timed_out']) is not bool:
            raise ValueError('Invalid finished execution')
    elif execution != {'state':'incomplete', 'returncode':None, 'timed_out':None}:
        raise ValueError('Invalid incomplete execution')
    records, missing = header['files'], header['missing_outputs']
    if not isinstance(records, list) or len(records) > 36 or not isinstance(missing, list) or len(missing) > 32:
        raise ValueError('Invalid collection inventory')
    names, total = set(), 0
    for item in records:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
            raise ValueError('Invalid output record')
        name = item['path']
        if not isinstance(name, str) or (name not in CONTROL_NAMES and output_name(name) != name):
            raise ValueError('Unsupported collection file')
        if name in names or type(item['size']) is not int or item['size'] < 0:
            raise ValueError('Duplicated or invalid output')
        runtime.hash_value(item['sha256'])
        if name.startswith('execution-') and item['size'] > 16384:
            raise ValueError('Oversized execution receipt')
        names.add(name)
        total += item['size']
    if total > context['payload_bytes']:
        raise ValueError('Collection exceeds reserved payload')
    missing_names = [output_name(name) for name in missing]
    if len(set(missing_names)) != len(missing_names) or set(missing_names) & names:
        raise ValueError('Inconsistent missing outputs')
    if execution['state'] == 'finished' and not {'execution-intent.json','execution-result.json'} <= names:
        raise ValueError('Finished execution requires its receipts')
    if 'execution-intent.json' not in names and (missing or any(n.startswith('output/') for n in names)):
        raise ValueError('Outputs require a declared execution intent')
    return header


def verify_payload(folder, header, context):
    validate_header(header, context)
    expected = {item['path'] for item in header['files']}
    observed = set()
    for parent, dirs, names in os.walk(folder, followlinks=False):
        if any((Path(parent)/name).is_symlink() for name in dirs):
            raise ValueError('Linked output directory')
        observed.update((Path(parent)/name).relative_to(folder).as_posix() for name in names)
    if observed != expected:
        raise ValueError('Local output inventory changed')
    for item in header['files']:
        path = folder/item['path']
        parent = runtime.directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != item['size']:
                raise ValueError('Local output file changed')
            checksum, remaining = hashlib.sha256(), item['size']
            while remaining:
                block = handle.read(min(remaining, 65536))
                if not block:
                    raise ValueError('Truncated local output')
                checksum.update(block)
                remaining -= len(block)
            after = os.fstat(handle.fileno())
            if handle.read(1) or checksum.hexdigest() != item['sha256'] or (
                    before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Local output hash or stability mismatch')
    if 'execution-intent.json' in expected:
        intent = json.loads(runtime.read_regular(folder/'execution-intent.json', 16384))
        if any(intent.get(k) != context[k] for k in ('request_id','manifest_sha256','job_id')):
            raise ValueError('Execution receipt identity mismatch')
        outputs = intent.get('outputs')
        if not isinstance(outputs, list) or not 3 <= len(outputs) <= 32:
            raise ValueError('Missing declared outputs')
        declared = [output_name('output/'+name) for name in outputs]
        if len(set(declared)) != len(declared) or not {'output/stdout.txt','output/stderr.txt','output/log.lammps'} <= set(declared):
            raise ValueError('Invalid declared outputs')
        if set(declared) != {name for name in expected if name.startswith('output/')} | set(header['missing_outputs']):
            raise ValueError('Collected files differ from declared outputs')
    if 'execution-result.json' in expected:
        result = json.loads(runtime.read_regular(folder/'execution-result.json', 16384))
        if (any(result.get(k) != context[k] for k in ('request_id','job_id'))
                or result.get('scientific_status') != 'not_evaluated'
                or header['execution'] != dict(state='finished', returncode=result.get('returncode'), timed_out=result.get('timed_out'))):
            raise ValueError('Execution summary differs from its receipt')


class Receiver:
    """Incremental framing and hash checks; never buffers a full output file."""
    def __init__(self, folder, context):
        self.folder, self.context = folder, context
        folder.mkdir(mode=0o700)
        (folder/'output').mkdir(mode=0o700)
        self.pending, self.header_size, self.header = bytearray(), None, None
        self.index, self.handle = 0, None

    def feed(self, block):
        if not block or len(block) > 65536:
            raise ValueError('Invalid transport chunk')
        self.pending.extend(block)
        if self.header_size is None:
            if len(self.pending) < 4:
                return
            self.header_size = struct.unpack('!I', self.pending[:4])[0]
            del self.pending[:4]
            if not 0 < self.header_size <= HEADER_LIMIT:
                raise ValueError('Oversized collection header')
        if self.header is None:
            if len(self.pending) < self.header_size:
                return
            self.header = validate_header(json.loads(self.pending[:self.header_size]), self.context)
            del self.pending[:self.header_size]
        while self.index < len(self.header['files']):
            item = self.header['files'][self.index]
            if self.handle is None:
                fd = os.open(self.folder/item['path'], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
                self.handle = os.fdopen(fd, 'wb')
                self.remaining, self.checksum = item['size'], hashlib.sha256()
            take = min(self.remaining, len(self.pending))
            if take:
                chunk = self.pending[:take]
                self.handle.write(chunk)
                self.checksum.update(chunk)
                del self.pending[:take]
                self.remaining -= take
            if self.remaining:
                return
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
            self.handle = None
            if self.checksum.hexdigest() != item['sha256']:
                raise ValueError('Downloaded output hash mismatch')
            self.index += 1
        if self.pending:
            raise ValueError('Trailing undeclared output bytes')

    def finish(self):
        if self.header is None or self.index != len(self.header['files']) or self.pending:
            raise ValueError('Incomplete output transfer')
        verify_payload(self.folder, self.header, self.context)
        for folder in (self.folder/'output', self.folder):
            fd = runtime.directory(folder)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return self.header

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def transfer(argv, receiver, *, timeout, max_bytes):
    """Drain both pipes, stream payload to disk, and kill/reap on every failure."""
    stderr, received, checksum = bytearray(), 0, hashlib.sha256()
    deadline, failure = time.monotonic()+timeout, ''
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Output transfer timed out')
                    for key,_ in selector.select(min(.1, remaining)):
                        room = max_bytes-received if key.data == 'stdout' else 65536-len(stderr)
                        block = os.read(key.fileobj.fileno(), min(65536, room+1))
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == 'stderr':
                            stderr.extend(block)
                            if len(stderr) > 65536:
                                raise ValueError('Collection diagnostics exceed limit')
                        else:
                            received += len(block)
                            checksum.update(block)
                            if received > max_bytes:
                                raise ValueError('Output transport exceeds reservation')
                            receiver.feed(block)
            code = process.wait(timeout=max(.001, deadline-time.monotonic()))
            if code != 0:
                failure = 'remote_failed'
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            failure = type(exc).__name__
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    return dict(returncode=process.returncode, failure=failure, stdout_bytes=received,
                stdout_sha256=checksum.hexdigest(), stderr=base64.b64encode(stderr).decode())


class OutputCollector:
    def __init__(self, ledger, endpoint, directory, *, timeout=60):
        if not 0 < timeout <= 60:
            raise ValueError('Collection timeout must be bounded')
        self.ledger, self.endpoint = ledger, endpoint
        self.directory, self.timeout = private_directory(directory), timeout

    def fetch(self, request_id):
        row = self.ledger.get(request_id)
        identity(request_id, row['manifest_sha256'])
        lock = os.open(self.directory/'.collection.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise ValueError('Unsafe collection lock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict('Another collection is running') from exc
            row = self.ledger.get(request_id)
            if row['state'] not in JOB_TERMINAL or not row['accounted']:
                raise Conflict('Reconcile and account for the job before collecting outputs')
            for event in reversed(self.ledger.events(request_id)):
                saved = json.loads(event['payload'])
                if event['kind'] != 'output_fetch_finished' or not saved['collected']:
                    continue
                path = self.directory/saved['ticket']
                raw = runtime.read_regular(path/'receipt.json', 262144, private=True)
                if runtime.digest(raw) != saved['evidence_sha256']:
                    raise ValueError('Collected receipt changed')
                receipt = json.loads(raw)
                context = receipt['context']
                if (context['request_id'] != request_id or context['job_id'] != row['job_id']
                        or context['manifest_sha256'] != row['manifest_sha256'] or context['scheduler_state'] != row['state']):
                    raise Conflict('Collected outputs no longer match scheduler evidence')
                verify_payload(path/'payload', receipt['header'], context)
                return dict(receipt, directory=str(path))
            context = self.ledger.begin_output_fetch(request_id)
            path = self.directory/context['ticket']
            path.mkdir(mode=0o700)
            endpoint = self.endpoint
            remote = [endpoint.python_path, '-I', '-c', BOOTSTRAP, endpoint.helper_path, endpoint.helper_sha256,
                      '--collect', '--root', endpoint.root_path, '--request-id', request_id,
                      '--manifest-sha256', context['manifest_sha256'], '--job-id', context['job_id']]
            argv = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
                    '-o','ClearAllForwardings=yes','-o','ForwardAgent=no','-o','ForwardX11=no',
                    '-o','PermitLocalCommand=no',endpoint.host_alias,shlex.join(remote)]
            intent_sha256 = _write_new(path/'intent.json', dict(context=context, argv=argv))
            receiver, header, captured, error = None, None, None, ''
            try:
                receiver = Receiver(path/'payload', context)
                captured = transfer(argv, receiver, timeout=self.timeout, max_bytes=context['payload_bytes']+HEADER_LIMIT+4)
                if captured['failure']:
                    raise ValueError('Collection transport failed')
                header = receiver.finish()
            except (ValueError, OSError, KeyError, TypeError, runtime.ExecutionDenied) as exc:
                error = type(exc).__name__
            finally:
                if receiver is not None:
                    receiver.close()
            receipt = dict(context=context, state='collected' if not error else 'collection_failed',
                           intent_sha256=intent_sha256, transport=captured, header=header, error_type=error,
                           scientific_status='not_evaluated')
            proof = _write_new(path/'receipt.json', receipt)
            self.ledger.finish_output_fetch(request_id, context['ticket'], collected=not bool(error), evidence_sha256=proof)
            return dict(receipt, directory=str(path))
        finally:
            os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Collect outputs of one accounted job; never submit or execute.')
    for name in ('ledger','ssh-alias','python-path','runtime-helper','helper-sha256','requests-root','directory','request-id'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(argv)
    ledger_path = Path(args.ledger)
    if not ledger_path.is_file() or ledger_path.is_symlink():
        parser.error('An existing private ledger is required')
    endpoint = StageEndpoint(args.ssh_alias,args.python_path,args.runtime_helper,args.helper_sha256,args.requests_root)
    result = OutputCollector(Ledger(ledger_path), endpoint, args.directory).fetch(args.request_id)
    print(json.dumps({'state':result['state'], 'directory':result['directory'], 'scientific_status':'not_evaluated'}))
    return 0 if result['state'] == 'collected' else 1


if __name__ == '__main__':
    raise SystemExit(main())
