"""Ordinary, reference-only author execution through the accounted services.

The trusted binding provider supplies source-validated recipes and deployment
metadata. Browser calls select only a task/revision/package hash. This controller
does not generate scientific code, invent a grant or use agent candidate schemas.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import struct
import uuid

from . import runtime_launcher as runtime
from .author_execution import compile_author_package
from .batch_plan import BatchPlan, render_batch
from .hpc_transport import bind_request, transport_identity
from .ledger import Conflict, JOB_TERMINAL, LimitExceeded, Resources
from .manifest import canonical, private_directory, relative_name, sha256
from .outputs import CONTROL_NAMES, transfer
from .remote_submit import private_bytes_once
from .slurm_read import _capture, _write_new, identity
from .slurm_submit import SlurmSubmitter, SubmitUncertain
from .staging import BOOTSTRAP, StageClient, UploadUncertain
from .submission import SchedulerReceipt, Submission
from .tasks import TaskError, task_id


HEADER_LIMIT = 1024**2
RECEIPT_LIMIT = 2*1024**2
FLAGS = dict(role='author_reference', scope='reference_only', B_input=False,
             scientific_status='not_evaluated')


def _command(adapter, remote):
    endpoint = adapter.endpoint
    if adapter.transport is not None:
        return adapter.transport.command(endpoint.host_alias, remote)
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
            '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no', endpoint.host_alias, shlex.join(remote)]


@dataclass(frozen=True)
class AuthorSnapshot:
    path: Path
    digest: str

    def verify(self):
        runtime.hash_value(self.digest)
        raw = runtime.read_regular(self.path/'author-package.json', 1_000_000, private=True)
        package = json.loads(raw)
        if (sha256(raw) != self.digest or canonical(package) != raw
                or package.get('role') != 'author_reference'
                or package.get('kind') != 'original_author_execution'
                or package.get('scope') != 'reference_only' or package.get('B_input') is not False
                or package.get('original_files_rewritten') is not False):
            raise Conflict('Author snapshot identity or reference role changed')
        Resources(**package['resources'])
        return package


def render_author_batch(submission, environment):
    """Reuse the reviewed CPU-only resource script with the reference launcher."""
    batch = render_batch(submission, environment)
    arguments = [environment.python_path, '-I', '-c', BOOTSTRAP, environment.launcher_path,
        environment.launcher_sha256, '--request-id', submission.request_id,
        '--manifest-sha256', submission.manifest_sha256]
    suffix = ('exec '+shlex.join(arguments)+'\n').encode()
    if not batch.script.endswith(suffix):
        raise Conflict('Unexpected base batch launcher')
    raw = batch.script[:-len(suffix)]+('exec '+shlex.join(arguments+['--author-reference'])+'\n').encode()
    return BatchPlan(raw, sha256(raw))


class AuthorStageClient(StageClient):
    def __init__(self, *args, capture=_capture, **kwargs):
        super().__init__(*args, **kwargs)
        self.capture = capture

    def upload(self, submission, snapshot):
        identity(submission.request_id, submission.manifest_sha256)
        package = snapshot.verify()
        if snapshot.digest != submission.manifest_sha256 or package['resources'] != asdict(submission.resources):
            raise Conflict('Author package differs from its accounted reservation')
        endpoint = self.endpoint
        remote = [endpoint.python_path, '-I', '-c', BOOTSTRAP, endpoint.helper_path,
                  endpoint.helper_sha256, '--author-reference', '--root', endpoint.root_path,
                  '--request-id', submission.request_id, '--manifest-sha256', snapshot.digest]
        trace = private_directory(self.audit_directory/uuid.uuid4().hex)
        _write_new(trace/'intent.json', dict(role='author_reference', request_id=submission.request_id,
            manifest_sha256=snapshot.digest, metadata_only=True, argv_sha256=sha256(canonical(remote))))
        raw = canonical(package)
        def chunks():
            yield struct.pack('!I', len(raw))
            for offset in range(0, len(raw), 65536):
                yield raw[offset:offset+65536]
        try:
            result = self.capture(_command(self, remote), timeout=self.timeout,
                                  max_bytes=65536, input_chunks=chunks())
        except (OSError, ValueError) as error:
            result = dict(returncode=None, failure=type(error).__name__, stdout='', stderr='')
        proof = _write_new(trace/'result.json', result)
        try:
            receipt = json.loads(base64.b64decode(result['stdout'], validate=True))
            expected = dict(schema_version=1, state='staged', role='author_reference',
                request_id=submission.request_id, manifest_sha256=snapshot.digest,
                package_sha256=snapshot.digest, profile_sha256=package['deployment']['profile_sha256'],
                storage_bytes=submission.resources.storage_bytes,
                input_bytes=sum(row['size'] for row in package['files']),
                original_files_rewritten=False, physical_evaluation_performed=False, execution_authorized=False)
            if (result['returncode'] != 0 or result['failure'] or not isinstance(receipt, dict)
                    or any(receipt.get(key) != value or type(receipt.get(key)) is not type(value)
                           for key, value in expected.items())):
                raise ValueError('Wrong author staging receipt')
            runtime.hash_value(receipt.get('policy_sha256'))
            runtime.hash_value(receipt.get('command_plan_sha256'))
        except (ValueError, KeyError, TypeError) as error:
            raise UploadUncertain('Author original copy unconfirmed; reconcile before another stage') from error
        return proof


class AuthorSlurmSubmitter(SlurmSubmitter):
    """Same one-shot Slurm adapter, explicitly selecting the reference handler."""
    def __init__(self, *args, capture=_capture, **kwargs):
        super().__init__(*args, **kwargs)
        self.capture = capture

    def submit(self, submission):
        row = self._reserved(submission, {'dispatching'})
        if not row['dispatch_claimed']:
            raise Conflict('A durable reference dispatch intent is required')
        endpoint = self.endpoint
        remote = [endpoint.python_path, '-I', '-c', BOOTSTRAP, endpoint.helper_path,
                  endpoint.helper_sha256, '--author-reference', '--root', endpoint.root_path,
                  '--request-id', submission.request_id, '--manifest-sha256', submission.manifest_sha256]
        trace = self.audit_directory/submission.request_id
        trace.mkdir(mode=0o700, exist_ok=False)
        _write_new(trace/'intent.json', dict(request_id=submission.request_id,
            manifest_sha256=submission.manifest_sha256, role='author_reference',
            argv_sha256=sha256(canonical(remote)), at=datetime.now(timezone.utc).isoformat()))
        try:
            result = self.capture(_command(self, remote), timeout=40, max_bytes=65536)
        except (OSError, ValueError) as error:
            result = dict(returncode=None, failure=type(error).__name__, stdout='', stderr='')
        proof = _write_new(trace/'result.json', result)
        try:
            receipt = json.loads(base64.b64decode(result['stdout'], validate=True))
            if (result['returncode'] != 0 or result['failure'] or not isinstance(receipt, dict)
                    or any(receipt.get(key) != value for key, value in dict(state='accepted',
                        request_id=submission.request_id, manifest_sha256=submission.manifest_sha256).items())
                    or not isinstance(receipt.get('job_id'), str)
                    or not re.fullmatch('[1-9][0-9]{0,19}', receipt['job_id'])):
                raise ValueError('No matching reference scheduler receipt')
            runtime.hash_value(receipt.get('evidence_sha256'))
        except (ValueError, KeyError, TypeError) as error:
            raise SubmitUncertain('Author scheduler acceptance unknown; reconcile the original request') from error
        return SchedulerReceipt(receipt['job_id'], proof)


def _output_name(value):
    if not isinstance(value, str) or not value.startswith('output/'):
        raise ValueError('Author output path must be inside output/')
    relative_name(value[7:])
    return value


def validate_author_header(header, context, *, package=None):
    keys = {'schema_version', 'request_id', 'manifest_sha256', 'job_id', 'execution',
            'scientific_status', 'missing_outputs', 'files'}
    if (not isinstance(header, dict) or set(header) != keys
            or type(header['schema_version']) is not int or header['schema_version'] != 1
            or any(header[key] != context[key] for key in ('request_id', 'manifest_sha256', 'job_id'))
            or header['scientific_status'] != 'not_evaluated'):
        raise ValueError('Author collection identity mismatch')
    execution = header['execution']
    if not isinstance(execution, dict) or set(execution) != {'state', 'returncode', 'timed_out'}:
        raise ValueError('Invalid author execution summary')
    if execution.get('state') == 'finished':
        if type(execution['returncode']) is not int or type(execution['timed_out']) is not bool:
            raise ValueError('Invalid finished author summary')
    elif execution != dict(state='incomplete', returncode=None, timed_out=None):
        raise ValueError('Invalid incomplete author summary')
    records, missing = header['files'], header['missing_outputs']
    if (not isinstance(records, list) or len(records) > 4100
            or not isinstance(missing, list) or len(missing) > 4096):
        raise ValueError('Author collection inventory exceeds its bound')
    names, total = set(), 0
    for row in records:
        if not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256'}:
            raise ValueError('Invalid author output identity')
        name = row['path']
        if not isinstance(name, str) or (name not in CONTROL_NAMES and _output_name(name) != name):
            raise ValueError('Invalid author collection path')
        if (name in names or any(name.startswith(old+'/') or old.startswith(name+'/') for old in names)
                or type(row['size']) is not int or row['size'] < 0):
            raise ValueError('Overlapping or invalid author collection files')
        runtime.hash_value(row['sha256'])
        if name.startswith('execution-') and row['size'] > HEADER_LIMIT:
            raise ValueError('Oversized author execution receipt')
        names.add(name); total += row['size']
    missing_names = [_output_name(name) for name in missing]
    if len(set(missing_names)) != len(missing_names) or set(missing_names) & names or total > context['payload_bytes']:
        raise ValueError('Inconsistent author collection inventory or budget')
    if execution['state'] == 'finished' and not {'execution-intent.json', 'execution-result.json'} <= names:
        raise ValueError('Finished author execution requires original receipts')
    if 'execution-intent.json' not in names and (missing or any(name.startswith('output/') for name in names)):
        raise ValueError('Author output has no execution intent')
    if package is not None and 'execution-intent.json' in names:
        expected = {'output/stdout.txt', 'output/stderr.txt'} | {'output/'+row['path'] for row in package['outputs']}
        if expected != {name for name in names if name.startswith('output/')} | set(missing_names):
            raise ValueError('Author outputs differ from the approved package')
    return header


def verify_author_payload(folder, header, context, *, package=None):
    validate_author_header(header, context, package=package)
    expected = {row['path'] for row in header['files']}
    observed = set()
    for parent, folders, names in os.walk(folder, followlinks=False):
        if any((Path(parent)/name).is_symlink() for name in folders):
            raise ValueError('Linked author output directory')
        observed.update((Path(parent)/name).relative_to(folder).as_posix() for name in names)
    if observed != expected:
        raise ValueError('Author output inventory changed')
    for row in header['files']:
        path = folder/row['path']; parent = runtime.directory(path.parent, private=True)
        try:
            descriptor = os.open(path.name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno()); checksum = hashlib.sha256(); count = 0
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size != row['size']):
                raise ValueError('Unsafe author output file')
            for block in iter(lambda: stream.read(65536), b''):
                count += len(block)
                if count > row['size']:
                    raise ValueError('Author output grew while reading')
                checksum.update(block)
            after = os.fstat(stream.fileno())
            if (count != row['size'] or checksum.hexdigest() != row['sha256']
                    or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError('Author output bytes changed')
    if 'execution-intent.json' in expected:
        intent = json.loads(runtime.read_regular(folder/'execution-intent.json', HEADER_LIMIT, private=True))
        if (any(intent.get(key) != context[key] for key in ('request_id', 'manifest_sha256', 'job_id'))
                or intent.get('role') != 'author_reference'
                or intent.get('author_package_sha256') != context['manifest_sha256']):
            raise ValueError('Author execution receipt has the wrong role or identity')
        outputs = intent.get('outputs')
        if not isinstance(outputs, list) or not 2 <= len(outputs) <= 4096:
            raise ValueError('Missing declared author outputs')
        declared = [_output_name('output/'+name) for name in outputs]
        if (len(set(declared)) != len(declared) or not {'output/stdout.txt', 'output/stderr.txt'} <= set(declared)
                or set(declared) != {name for name in expected if name.startswith('output/')} | set(header['missing_outputs'])):
            raise ValueError('Author collection differs from its original declared outputs')
    if 'execution-result.json' in expected:
        result = json.loads(runtime.read_regular(folder/'execution-result.json', HEADER_LIMIT, private=True))
        if (any(result.get(key) != context[key] for key in ('request_id', 'job_id'))
                or result.get('scientific_status') != 'not_evaluated'
                or header['execution'] != dict(state='finished', returncode=result.get('returncode'), timed_out=result.get('timed_out'))):
            raise ValueError('Author result summary differs from its receipt')


class AuthorReceiver:
    """The existing framed output protocol, with bounded nested author names."""
    def __init__(self, folder, context, package):
        self.folder, self.context, self.package = folder, context, package
        folder.mkdir(mode=0o700); (folder/'output').mkdir(mode=0o700)
        self.pending = bytearray(); self.header_size = self.header = self.handle = None
        self.index = 0

    def _open_output(self, name):
        # mkdir(parents=True) uses default permissions for intermediate parents.
        # Walk descriptors instead: every new level stays private/no-follow.
        directory = runtime.directory(self.folder, private=True)
        try:
            parts = name.split('/')
            for part in parts[:-1]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory); os.fsync(directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=directory)
                info = os.fstat(child)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    os.close(child)
                    raise ValueError('Author collection parents must remain private')
                os.close(directory); directory = child
            descriptor = os.open(parts[-1], os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o400, dir_fd=directory)
            os.fsync(directory)
            return descriptor
        finally:
            os.close(directory)

    def feed(self, block):
        if not block or len(block) > 65536:
            raise ValueError('Invalid author transport chunk')
        self.pending.extend(block)
        if self.header_size is None:
            if len(self.pending) < 4:
                return
            self.header_size = struct.unpack('!I', self.pending[:4])[0]; del self.pending[:4]
            if not 0 < self.header_size <= HEADER_LIMIT:
                raise ValueError('Oversized author collection header')
        if self.header is None:
            if len(self.pending) < self.header_size:
                return
            self.header = validate_author_header(json.loads(self.pending[:self.header_size]), self.context, package=self.package)
            del self.pending[:self.header_size]
        while self.index < len(self.header['files']):
            row = self.header['files'][self.index]
            if self.handle is None:
                descriptor = self._open_output(row['path'])
                self.handle = os.fdopen(descriptor, 'wb'); self.remaining = row['size']; self.checksum = hashlib.sha256()
            take = min(self.remaining, len(self.pending))
            if take:
                chunk = self.pending[:take]; self.handle.write(chunk); self.checksum.update(chunk)
                del self.pending[:take]; self.remaining -= take
            if self.remaining:
                return
            self.handle.flush(); os.fsync(self.handle.fileno()); self.handle.close(); self.handle = None
            if self.checksum.hexdigest() != row['sha256']:
                raise ValueError('Author output transfer digest mismatch')
            self.index += 1
        if self.pending:
            raise ValueError('Undeclared author transport bytes')

    def finish(self):
        if self.header is None or self.index != len(self.header['files']) or self.pending:
            raise ValueError('Incomplete author output transfer')
        verify_author_payload(self.folder, self.header, self.context, package=self.package)
        return self.header

    def close(self):
        if self.handle is not None:
            self.handle.close(); self.handle = None


class AuthorOutputCollector:
    def __init__(self, ledger, endpoint, directory, *, snapshots, transport=None, timeout=60, transfer_call=transfer):
        if not 0 < timeout <= 60:
            raise ValueError('Bound author collection timeout')
        self.ledger, self.endpoint, self.transport = ledger, endpoint, transport
        self.directory = private_directory(directory); self.snapshots = private_directory(snapshots)
        self.timeout, self.transfer = timeout, transfer_call

    def fetch(self, request_id):
        bind_request(self.ledger, request_id, self)
        row = self.ledger.get(request_id)
        if (self.ledger.evaluation_snapshot(row['evaluation'])['identity']['role'] != 'reference'
                or row['state'] not in JOB_TERMINAL or not row['accounted']):
            raise Conflict('Only an accounted author allocation can be collected')
        package = AuthorSnapshot(self.snapshots/row['manifest_sha256'], row['manifest_sha256']).verify()
        with _lease(self.directory/'.author-collection.lock'):
            for event in reversed(self.ledger.events(request_id)):
                if event['kind'] != 'output_fetch_finished':
                    continue
                saved = json.loads(event['payload'])
                if not saved['collected']:
                    continue
                path = self.directory/saved['ticket']
                raw = runtime.read_regular(path/'receipt.json', RECEIPT_LIMIT, private=True)
                if sha256(raw) != saved['evidence_sha256']:
                    raise Conflict('Saved author collection receipt changed')
                receipt = json.loads(raw); context = receipt['context']
                if (receipt.get('role') != 'author_reference'
                        or any(context[key] != value for key, value in dict(request_id=request_id,
                            job_id=row['job_id'], manifest_sha256=row['manifest_sha256'], scheduler_state=row['state']).items())):
                    raise Conflict('Author collection belongs to another allocation')
                verify_author_payload(path/'payload', receipt['header'], context, package=package)
                return dict(receipt, directory=str(path))
            context = self.ledger.begin_output_fetch(request_id)
            self.ledger.reserve_raw_export(request_id, sha256(canonical(dict(author_collection_metadata=context['ticket']))), RECEIPT_LIMIT)
            path = private_directory(self.directory/context['ticket']); endpoint = self.endpoint
            remote = [endpoint.python_path, '-I', '-c', BOOTSTRAP, endpoint.helper_path,
                endpoint.helper_sha256, '--author-reference', '--collect', '--root', endpoint.root_path,
                '--request-id', request_id, '--manifest-sha256', row['manifest_sha256'], '--job-id', row['job_id']]
            proof = _write_new(path/'intent.json', dict(context=context, role='author_reference', argv_sha256=sha256(canonical(remote))))
            receiver = None; header = captured = None; error = ''
            try:
                receiver = AuthorReceiver(path/'payload', context, package)
                captured = self.transfer(_command(self, remote), receiver, timeout=self.timeout,
                    max_bytes=context['payload_bytes']+HEADER_LIMIT+4)
                if captured['failure']:
                    raise ValueError('Author collection transport unconfirmed')
                header = receiver.finish()
            except (ValueError, KeyError, TypeError, OSError, runtime.ExecutionDenied) as exc:
                error = type(exc).__name__
            finally:
                if receiver is not None:
                    receiver.close()
            receipt = dict(role='author_reference', context=context, state='collection_failed' if error else 'collected',
                intent_sha256=proof, transport=captured, header=header, error_type=error, scientific_status='not_evaluated')
            digest = _write_new(path/'receipt.json', receipt)
            self.ledger.finish_output_fetch(request_id, context['ticket'], collected=not bool(error), evidence_sha256=digest)
            return dict(receipt, directory=str(path))


@contextmanager
def _lease(path):
    descriptor = os.open(path, os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise Conflict('Unsafe author controller lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise Conflict('This author operation is already running') from None
        yield
    finally:
        os.close(descriptor)


class AuthorFollowingService:
    def __init__(self, ledger, reconciliation, collector, *, max_polls=240, interval_seconds=3600):
        self.ledger, self.reconciliation, self.collector = ledger, reconciliation, collector
        if (ledger.path != reconciliation.ledger.path or ledger.path != collector.ledger.path
                or transport_identity(reconciliation.reader) != transport_identity(collector)
                or getattr(reconciliation.reader, 'host_alias', collector.endpoint.host_alias) != collector.endpoint.host_alias):
            raise Conflict('Reference following must share the accounted transport')
        self.max_polls, self.interval_seconds = max_polls, interval_seconds
        self.poll_storage_bytes = 3*(getattr(reconciliation.reader, 'max_bytes', 65536)+1)+16384
        self.config_sha256 = sha256(canonical(dict(role='author_reference',
            endpoint=asdict(collector.endpoint), transport=transport_identity(collector),
            directory=str(collector.directory), max_polls=max_polls, interval_seconds=interval_seconds)))

    def advance(self, request_id):
        row = self.ledger.get(request_id)
        if self.ledger.evaluation_snapshot(row['evaluation'])['identity']['role'] != 'reference':
            raise Conflict('Agent work cannot enter author following')
        with _lease(self.ledger.path.with_name(self.ledger.path.name+'.author-follow.lock')):
            bind_request(self.ledger, request_id, self.reconciliation.reader)
            self.ledger.register_following(request_id, self.config_sha256, max_polls=self.max_polls,
                interval_seconds=self.interval_seconds, poll_storage_bytes=self.poll_storage_bytes)
            if row['state'] == 'reconcile_required':
                return dict(request_id=request_id, state='attention', reason='scheduler_conflict', **FLAGS)
            if row['state'] not in JOB_TERMINAL or not row['accounted']:
                try:
                    if not self.ledger.claim_following_poll(request_id):
                        return dict(request_id=request_id, state='waiting', reason='poll_not_due', **FLAGS)
                    row = self.reconciliation.refresh(request_id)
                except (ValueError, OSError, RuntimeError) as error:
                    return dict(request_id=request_id, state='attention' if isinstance(error, LimitExceeded) else 'waiting',
                        reason='poll_or_storage_limit' if isinstance(error, LimitExceeded) else 'scheduler_read_unconfirmed', **FLAGS)
            if row['state'] not in JOB_TERMINAL or not row['accounted']:
                return dict(request_id=request_id, state='waiting', scheduler_state=row['state'], job_id=row['job_id'], **FLAGS)
            events = self.ledger.events(request_id)
            starts = [event for event in events if event['kind']=='output_fetch_started']
            finishes = [json.loads(event['payload']) for event in events if event['kind']=='output_fetch_finished']
            if starts and not any(event['collected'] for event in finishes):
                # A supervisor restart is not permission for unbounded copies.
                # A direct user collection action may recover the retained error.
                return dict(request_id=request_id, state='attention', reason='collection_recovery_required', **FLAGS)
            collected = self.collector.fetch(request_id)
            return dict(request_id=request_id, job_id=row['job_id'], scheduler_state=row['state'],
                state=collected['state'], ready_for_analysis=collected['state']=='collected', **FLAGS)


class AuthorDispatch:
    """Metadata prepare -> explicit approval -> accounted native dispatch -> follow."""
    def __init__(self, tasks, papers, ledger, *, binding_provider, runtime_provider, snapshots):
        if papers.ledger is None or papers.ledger.path != ledger.path:
            raise Conflict('Paper reference and execution must share the original ledger')
        self.tasks, self.papers, self.ledger = tasks, papers, ledger
        self.binding_provider, self.runtime_provider = binding_provider, runtime_provider
        self.snapshots = private_directory(snapshots)
        self.locks = private_directory(tasks.path.parent/'author-dispatch-locks')
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_dispatch_packages ('
                'task_id TEXT NOT NULL, package_sha256 TEXT NOT NULL, document TEXT NOT NULL, '
                'sha256 TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(task_id,package_sha256))')
            for action in ('UPDATE', 'DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_author_dispatch_packages_{action} '
                    f'BEFORE {action} ON author_dispatch_packages BEGIN SELECT RAISE(ABORT,"immutable author dispatch"); END')

    def _binding(self, identifier):
        task_id(identifier); task = self.tasks.get(identifier)
        if (task['mode'] != 'reproduction' or self.tasks.paused(identifier)
                or self.tasks.lifecycle(identifier)['deleted'] or self.tasks.lifecycle(identifier)['user_finished']):
            raise TaskError('作者 A 只属于活动的论文参考任务。')
        binding = deepcopy(self.binding_provider(identifier))
        required = {'report', 'evidence', 'source_files', 'deployment', 'resources', 'provenance',
                    'evaluation', 'task_revision', 'transport_identity'}
        if not isinstance(binding, dict) or set(binding) != required or binding['task_revision'] != task['revision']:
            raise TaskError('作者 A 可信部署字段或任务版本尚未齐备。')
        if isinstance(binding['resources'], Resources):
            binding['resources'] = asdict(binding['resources'])
        package = compile_author_package(binding['report'], evidence=binding['evidence'],
            source_files=binding['source_files'], deployment=binding['deployment'],
            resources=binding['resources'], provenance=binding['provenance'])
        evaluation = self.ledger.evaluation_snapshot(binding['evaluation'])
        if (evaluation['identity']['role'] != 'reference'
                or evaluation['identity']['task'] != binding['provenance']['task_sha256']):
            raise TaskError('作者 A 必须使用已登记的原参考评测身份。')
        with self.tasks.transaction() as db:
            papers = [row[0] for row in db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?', (identifier,))]
        if len(papers) != 1:
            raise TaskError('作者 A 必须唯一关联本论文。')
        return binding, package, dict(task_id=identifier, paper_id=papers[0], evaluation=binding['evaluation'],
            task_revision=task['revision'], transport_identity=binding['transport_identity'],
            binding=binding, package=package)

    def _stored(self, identifier):
        with self.tasks.transaction() as db:
            rows = list(db.execute('SELECT * FROM author_dispatch_packages WHERE task_id=? ORDER BY rowid', (identifier,)))
        result = []
        for row in rows:
            value = json.loads(row['document'])
            if sha256(canonical(value)) != row['sha256'] or value['package']['package_sha256'] != row['package_sha256']:
                raise Conflict('Saved author execution identity changed')
            result.append(value)
        return result

    def status(self, identifier):
        task=self.tasks.get(identifier); history = self._stored(identifier)
        current = None; reason = ''
        try:
            _, package, current = self._binding(identifier)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            reason = str(error)
        rows = []
        for saved in history:
            digest = saved['package']['package_sha256']
            evaluation = self.ledger.evaluation_snapshot(saved['evaluation'])
            requests = []
            for item in evaluation['requests']:
                original = self.ledger.get(item['id'])
                if original['manifest_sha256'] == digest:
                    requests.append({key: original[key] for key in ('id', 'state', 'job_id', 'dispatch_claimed',
                        'accounted', 'actual_core_seconds', 'charge_core_seconds')})
            rows.append(dict(package_sha256=digest, approved=self.tasks.plan_approved(identifier, 'author:'+digest),
                current=current == saved, evaluation=saved['evaluation'], requests=requests,
                report=saved['package']['package'], **FLAGS))
        latest = rows[-1] if rows else None
        return dict(task_id=identifier, task_revision=task['revision'], history=rows, latest=latest, configured=current is not None,
            disabled_reason=reason, can_prepare=current is not None,
            can_approve=bool(latest and latest['current'] and not latest['approved']),
            can_submit=bool(latest and latest['current'] and latest['approved']
                and not any(item['dispatch_claimed'] for item in latest['requests'])), **FLAGS)

    def prepare(self, identifier):
        with _lease(self.locks/(identifier+'.lock')):
            _, package, context = self._binding(identifier)
            digest = package['package_sha256']; path = private_directory(self.snapshots/digest)
            private_bytes_once(path/'author-package.json', canonical(package['package']))
            AuthorSnapshot(path, digest).verify()
            data = canonical(context)
            with self.tasks.transaction() as db:
                previous = db.execute('SELECT document FROM author_dispatch_packages WHERE task_id=? AND package_sha256=?',
                    (identifier, digest)).fetchone()
                if previous is not None and previous[0] != data.decode():
                    raise Conflict('An author package is already bound to another evaluation or transport')
                if previous is None:
                    db.execute('INSERT INTO author_dispatch_packages VALUES(?,?,?,?,?)',
                        (identifier, digest, data.decode(), sha256(data), datetime.now(timezone.utc).isoformat()))
        return self.status(identifier)

    def _current(self, identifier, digest):
        runtime.hash_value(digest)
        binding, package, context = self._binding(identifier)
        saved = next((row for row in self._stored(identifier) if row['package']['package_sha256'] == digest), None)
        if saved is None or context != saved or package['package_sha256'] != digest:
            raise TaskError('作者方案、部署或范围已改变，请重新查看当前完整方案。')
        snapshot = AuthorSnapshot(self.snapshots/digest, digest)
        if snapshot.verify() != package['package']:
            raise Conflict('Author package bytes changed')
        return binding, package, context, snapshot

    def approve(self, identifier, revision, package_sha256, *, note=''):
        with _lease(self.locks/(identifier+'.lock')):
            _, _, context, _ = self._current(identifier, package_sha256)
            if type(revision) is not int or context['task_revision'] != revision:
                raise TaskError('任务版本已改变，请刷新后批准。')
            scope = 'author:'+package_sha256
            if not self.tasks.plan_approved(identifier, scope):
                self.tasks.approve_plan(identifier, revision, scope=scope, note=note)
        return self.status(identifier)

    def _runtime(self, binding, package):
        services = self.runtime_provider(binding, package)
        if not isinstance(services, dict) or set(services) != {'staging', 'submission', 'authorization', 'following', 'environment'}:
            raise TaskError('作者 A 的提交、回收与授权服务未完整配置。')
        staging, submission, authorization, following = (services[key] for key in ('staging', 'submission', 'authorization', 'following'))
        if any(service.ledger.path != self.ledger.path for service in (staging, submission, authorization, following)):
            raise Conflict('Author operations must use one original ledger')
        adapters = (staging.client, submission.scheduler, authorization, following.collector, following.reconciliation.reader)
        if any(transport_identity(adapter) != binding['transport_identity'] for adapter in adapters):
            raise Conflict('Author operations cannot switch the pinned HPC connection')
        endpoints = (staging.client.endpoint, submission.scheduler.endpoint, following.collector.endpoint)
        if len({(item.host_alias, item.root_path, item.python_path) for item in endpoints}) != 1:
            raise Conflict('Author stages cannot mix deployments')
        env = services['environment']; endpoint = following.collector.endpoint
        if (env.root_path != endpoint.root_path or env.python_path != endpoint.python_path
                or env.launcher_path != endpoint.helper_path or env.launcher_sha256 != endpoint.helper_sha256):
            raise Conflict('Author batch and collection must use the same runtime')
        if following.collector.snapshots != self.snapshots:
            raise Conflict('Author following must use the prepared package store')
        return services

    def dispatch(self, identifier, package_sha256):
        with _lease(self.locks/(identifier+'.lock')):
            binding, package, context, snapshot = self._current(identifier, package_sha256)
            if not self.tasks.plan_approved(identifier, 'author:'+package_sha256):
                raise TaskError('请先批准这一版作者 A 完整原调用方案。')
            services = self._runtime(binding, package)
            self.papers.bind_reference_evaluation(context['paper_id'], identifier, context['evaluation'])
            key = 'author_'+identifier+'_'+package_sha256
            resources = Resources(**package['package']['resources'])
            row = self.ledger.reserve(context['evaluation'], key, package_sha256, resources)
            if row['dispatch_claimed']:
                return self.status(identifier)
            if row['state'] != 'prepared':
                raise Conflict('Original author reservation needs reconciliation, not another submission')
            request = Submission(row['id'], package_sha256, resources)
            batch = render_author_batch(request, services['environment'])
            bind_request(self.ledger, row['id'], services['staging'].client)
            authorization = services['authorization']
            if not authorization.ensure(request, snapshot, batch):
                return dict(self.status(identifier), waiting_reason='authorization_delivery_unconfirmed')
            authorization.verify(request, snapshot, batch)
            staged = services['staging'].stage(row['id'], snapshot)
            if staged['upload_state'] != 'staged':
                return dict(self.status(identifier), waiting_reason='author_staging_unconfirmed')
            self._current(identifier,package_sha256)
            grant = authorization.verify(request, snapshot, batch)
            self.ledger.bind_execution_authorization(row['id'], grant, batch.sha256, authorization.policy_sha256)
            services['submission'].submit(context['evaluation'], key, package_sha256, resources)
        return self.status(identifier)

    def follow(self, identifier):
        task_id(identifier)
        with _lease(self.locks/(identifier+'.lock')):
            history = self._stored(identifier)
            if not history:
                raise TaskError('尚无作者 A 可跟进。')
            latest = None; matched = []
            for saved in reversed(history):
                requests = self.ledger.evaluation_snapshot(saved['evaluation'])['requests']
                found = [row for row in requests if self.ledger.get(row['id'])['manifest_sha256'] == saved['package']['package_sha256']
                         and row['dispatch_claimed'] and row['state'] != 'rejected']
                if found:
                    latest, matched = saved, found
                    break
            if latest is None:
                raise TaskError('作者 A 尚未派发；跟进不会代为提交。')
            # Following an existing allocation must survive a newer proposal or
            # changed scientific preparation. It keeps its original package and
            # connection; no new grant, source read or reservation is issued.
            binding, package = latest['binding'], latest['package']
            AuthorSnapshot(self.snapshots/package['package_sha256'], package['package_sha256']).verify()
            services = self._runtime(binding, package)
            result = services['following'].advance(matched[-1]['id'])
        return dict(self.status(identifier), following=result)
