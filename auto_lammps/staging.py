"""Reserved, fixed-protocol upload to a pinned trusted file receiver. No submission."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import base64
import json
from pathlib import PurePosixPath
import re
import shlex
import struct
import uuid

from .ledger import Conflict, Ledger, Resources
from .manifest import Snapshot, private_directory, read_file, root_descriptor, sha256
from .slurm_read import _capture, _write_new, identity
from .submission import Submission

# This bootstrap executes only an administrator-installed helper with a pinned
# digest, never bytes from the uploaded input stream. -I ignores Python env paths.
BOOTSTRAP = '''import os,sys,stat,hashlib
path,expected=sys.argv[1:3]
fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
info=os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>1000000 or info.st_mode&0o022: sys.exit(91)
with os.fdopen(fd,'rb') as source: data=source.read(1000001)
if hashlib.sha256(data).hexdigest()!=expected: sys.exit(92)
sys.argv=[path,*sys.argv[3:]]
exec(compile(data,path,'exec'),{'__name__':'__main__','__file__':path})
'''


class UploadUncertain(RuntimeError):
    pass


def absolute_path(value):
    if (not isinstance(value, str) or not value.startswith('/') or len(value) > 1024
            or str(PurePosixPath(value)) != value
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', x) or x in {'.', '..'} for x in value.split('/')[1:])):
        raise ValueError('An explicit safe absolute deployment path is required')
    return value


@dataclass(frozen=True)
class StageEndpoint:
    host_alias: str
    python_path: str
    helper_path: str
    helper_sha256: str
    root_path: str

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', self.host_alias):
            raise ValueError('Invalid SSH alias')
        for value in (self.python_path, self.helper_path, self.root_path):
            absolute_path(value)
        if self.helper_path.startswith(self.root_path + '/'):
            raise ValueError('Trusted helper must be outside writable request root')
        if not re.fullmatch(r'[a-f0-9]{64}', self.helper_sha256):
            raise ValueError('Helper version hash is required')


def upload_chunks(snapshot):
    """Yield bounded binary frames, rechecking all bytes against the frozen hashes."""
    document = snapshot.verify()
    with root_descriptor(snapshot.path) as root:
        manifest = read_file(root, 'manifest.json', 1_000_000)
        if sha256(manifest) != snapshot.digest:
            raise ValueError('Manifest changed before upload')
        yield struct.pack('!I', len(manifest))
        for offset in range(0, len(manifest), 65536):
            yield manifest[offset:offset+65536]
        for item in document['files']:
            data = read_file(root, item['path'], item['size'])
            if sha256(data) != item['sha256'] or len(data) != item['size']:
                raise ValueError('Input changed before upload')
            for offset in range(0, len(data), 65536):
                yield data[offset:offset+65536]


class StageClient:
    def __init__(self, endpoint: StageEndpoint, audit_directory, *, timeout=60):
        if not 0 < timeout <= 60:
            raise ValueError('Upload timeout must be bounded')
        self.endpoint = endpoint
        self.audit_directory = private_directory(audit_directory)
        self.timeout = timeout

    def upload(self, submission: Submission, snapshot: Snapshot):
        identity(submission.request_id, submission.manifest_sha256)
        manifest = snapshot.verify()
        if snapshot.digest != submission.manifest_sha256 or manifest['resources'] != asdict(submission.resources):
            raise Conflict('Snapshot differs from reserved request')
        endpoint = self.endpoint
        remote = [endpoint.python_path, '-I', '-c', BOOTSTRAP, endpoint.helper_path, endpoint.helper_sha256,
                  '--root', endpoint.root_path, '--request-id', submission.request_id,
                  '--manifest-sha256', submission.manifest_sha256]
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
                   '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no', endpoint.host_alias, shlex.join(remote)]
        trace = self.audit_directory / uuid.uuid4().hex
        trace.mkdir(mode=0o700)
        intent = _write_new(trace / 'intent.json', dict(argv=command, request_id=submission.request_id,
                            manifest_sha256=snapshot.digest, started_utc=datetime.now(timezone.utc).isoformat()))
        try:
            result = _capture(command, timeout=self.timeout, max_bytes=65536, input_chunks=upload_chunks(snapshot))
        except (OSError, ValueError) as exc:
            result = dict(returncode=None, failure=type(exc).__name__, stdout='', stderr='')
        result = {**result, 'intent_sha256': intent, 'finished_utc': datetime.now(timezone.utc).isoformat()}
        proof = _write_new(trace / 'result.json', result)
        if result['failure']:
            raise UploadUncertain('Upload was not confirmed; retain reservation and inspect receipts')
        try:
            receipt = json.loads(base64.b64decode(result['stdout']).decode('utf-8'))
            expected = dict(schema_version=1, state='staged', request_id=submission.request_id,
                            manifest_sha256=snapshot.digest, storage_bytes=submission.resources.storage_bytes,
                            input_bytes=sum(item['size'] for item in manifest['files']))
            if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
                raise ValueError('Mismatched upload receipt')
            if not re.fullmatch(r'[a-f0-9]{64}', receipt.get('policy_sha256', '')):
                raise ValueError('Missing remote policy evidence')
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise UploadUncertain('Invalid upload receipt; inspect rather than retry') from exc
        return proof


class StagingService:
    def __init__(self, ledger: Ledger, client: StageClient):
        self.ledger, self.client = ledger, client

    def stage(self, request_id: str, snapshot: Snapshot):
        row = self.ledger.get(request_id)
        document = snapshot.verify()
        if row['manifest_sha256'] != snapshot.digest or json.loads(row['resources']) != document['resources']:
            raise Conflict('Frozen inputs do not match the reserved request')
        if self.ledger.begin_staging(request_id):
            try:
                proof = self.client.upload(Submission(request_id, snapshot.digest, Resources(**document['resources'])), snapshot)
            except Exception as exc:
                self.ledger.staging_result(request_id, error_type=type(exc).__name__)
                raise
            self.ledger.staging_result(request_id, evidence_sha256=proof)
        events = self.ledger.events(request_id)
        complete = next((event for event in events if event['kind'] == 'inputs_staged'), None)
        return dict(request_id=request_id, upload_state='staged' if complete else 'upload_unresolved')
