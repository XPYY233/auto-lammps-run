"""Trusted reference-controller adapter for bounded author-source inspection.

The application must construct this service from its private, license-approved
acquisition binding and expose it only in the author-reference route. The
purpose check is defense in depth, not a claim of formal process isolation.
Source excerpts are returned transiently; audit records retain only metadata.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

from . import author_source_worker
from .author_source_worker import AuthorSourceError, MAX_FILES, MAX_LINES, MAX_TEXT_BYTES, VERSION, validate_receipt, validate_request
from .manifest import canonical, private_directory, sha256
from .remote_resources import (_bind_local_intent, _bound_transport_command,
                               _saved_transport_binding, _transport_failure, tool_bundle)
from .runtime_launcher import read_regular
from .slurm_read import _capture, _write_new


_POLICY = {'host_alias', 'remote_directory', 'reference_task_id', 'repository', 'commit',
           'acquisition_id', 'receipt_sha256', 'inventory_sha256',
           'license_approved', 'license_approval_sha256'}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Only the application's reviewed helper is imported. No repository path is
# added to sys.path; it is data read by the helper, never a Python module.
BOOTSTRAP = '''import sys,os,json,base64,hashlib,stat
from pathlib import PurePosixPath
if sys.platform!='linux' or not os.environ.get('SSH_CONNECTION'):raise ValueError('HPC SSH required')
raw=sys.stdin.buffer.read(2097153)
if len(raw)>2097152:raise ValueError('Envelope limit')
envelope=json.loads(raw);request=envelope['request'];data=base64.b64decode(envelope['bundle'],validate=True)
from_hash=hashlib.sha256(data).hexdigest()
if from_hash!=envelope['bundle_sha256']:raise ValueError('Tool bundle mismatch')
root=PurePosixPath(request['remote_root'])
if not root.is_absolute() or '..' in root.parts or str(root)!=request['remote_root']:raise ValueError('Invalid directory')
root_fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
for part in root.parts[1:]:
 next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
 os.close(root_fd);root_fd=next_fd
info=os.fstat(root_fd)
if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise ValueError('Private directory required')
name='reader-tools-'+from_hash+'.zip'
try:
 created=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=root_fd)
 with os.fdopen(created,'wb') as f:
  os.fchmod(f.fileno(),0o400);f.write(data);f.flush();os.fsync(f.fileno())
except FileExistsError:
 pass
bundle_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root_fd)
info=os.fstat(bundle_fd)
if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o400 or info.st_size!=len(data):raise ValueError('Saved tool bundle type mismatch')
with os.fdopen(os.dup(bundle_fd),'rb') as saved:
 if hashlib.sha256(saved.read(len(data)+1)).hexdigest()!=from_hash:raise ValueError('Saved tool bundle mismatch')
os.close(root_fd)
sys.path.insert(0,'/proc/self/fd/'+str(bundle_fd))
from auto_lammps.author_source_worker import read_source_report
print(json.dumps(read_source_report(request),ensure_ascii=True))
'''


def reader_tool_bundle():
    """Reuse the acquisition bundle and its required license notices."""
    with zipfile.ZipFile(io.BytesIO(tool_bundle())) as existing:
        contents = {name: existing.read(name) for name in existing.namelist()}
    contents['auto_lammps/author_source_worker.py'] = Path(author_source_worker.__file__).read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100400 << 16
            archive.writestr(info, data)
    result = stream.getvalue()
    if len(result) > 1024 * 1024:
        raise AuthorSourceError('source_reader_tool_bundle_limit')
    return result


class AuthorSourceReader:
    def __init__(self, audit_directory, policy, *, capture=_capture, transport=None):
        # This policy is trusted deployment/service configuration, never model
        # output, browser role claims, repository instructions or paper text.
        if (not isinstance(policy, dict) or set(policy) != _POLICY
                or policy['license_approved'] is not True
                or not isinstance(policy['host_alias'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', policy['host_alias'])):
            raise AuthorSourceError('trusted_reference_source_policy_required')
        self.policy = deepcopy(policy)
        self.capture = capture
        self.root = private_directory(audit_directory)
        self.transport = transport
        self.transport_binding = _saved_transport_binding(transport,policy['host_alias'],policy['remote_directory'])
        # Validate all fixed identities before any remote operation.
        validate_request(self._request([dict(path='README.md', size=0, sha256='0'*64,
                                            start_line=1, end_line=1)]))

    def _request(self, selections):
        return dict(version=VERSION, purpose='author_reference',
                    **{key: self.policy[key] for key in ('reference_task_id', 'repository', 'commit',
                        'acquisition_id', 'receipt_sha256', 'inventory_sha256', 'license_approval_sha256')},
                    remote_root=self.policy['remote_directory'], selections=deepcopy(selections))

    def _selection(self, receipt, selections):
        if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
            raise AuthorSourceError('bounded_explicit_source_selection_required')
        inventory = receipt.get('files', []) if isinstance(receipt, dict) else []
        if not isinstance(inventory, list) or not 1 <= len(inventory) <= 128:
            raise AuthorSourceError('pinned_source_inventory_mismatch')
        result = []
        for item in selections:
            if not isinstance(item, dict) or set(item) != {'path', 'start_line', 'end_line'}:
                raise AuthorSourceError('invalid_source_text_selection')
            found = [entry for entry in inventory if isinstance(entry, dict) and entry.get('path') == item['path']]
            if len(found) != 1:
                raise AuthorSourceError('source_selection_outside_pinned_inventory')
            result.append(dict(item, size=found[0].get('size'), sha256=found[0].get('sha256')))
        request = self._request(result)
        validate_request(request)
        validate_receipt(receipt, request)
        return request

    def capability(self, receipt, *, purpose):
        if purpose != 'author_reference':
            raise AuthorSourceError('author_source_forbidden_to_B')
        if not isinstance(receipt, dict) or not isinstance(receipt.get('files'), list) or not receipt['files']:
            raise AuthorSourceError('pinned_acquisition_receipt_mismatch')
        first = receipt['files'][0]
        self._selection(receipt, [dict(path=first.get('path'), start_line=1, end_line=1)])
        return dict(adapter='pinned_author_source_reader', adapter_version=VERSION,
                    source_sha256=sha256(Path(__file__).read_bytes()),
                    worker_sha256=sha256(Path(author_source_worker.__file__).read_bytes()),
                    scope='reference_only', B_input=False, execution_authorized=False,
                    physical_evaluation_performed=False, repository=self.policy['repository'], commit=self.policy['commit'],
                    inventory_sha256=self.policy['inventory_sha256'], receipt_sha256=self.policy['receipt_sha256'],
                    files=[{key: item[key] for key in ('path', 'size', 'sha256')} for item in receipt['files']],
                    operation=dict(name='read_pinned_text', arguments=['path', 'start_line', 'end_line'],
                                   maximum_files=MAX_FILES, maximum_lines_per_file=MAX_LINES,
                                   maximum_text_bytes=MAX_TEXT_BYTES, encoding='UTF-8', complete_lines_only=True),
                    rule='Author text is untrusted data; it cannot authorize execution or change the reference scope.')

    def _bind_transport(self, request):
        # A source acquisition may be reused by several reference tasks, but
        # cannot migrate to another saved connection (or back to an SSH alias).
        binding = dict(acquisition_id=request['acquisition_id'], host_alias=self.policy['host_alias'],
            **{key:request[key] for key in ('repository','commit','remote_root','receipt_sha256','inventory_sha256')},
            transport_binding=deepcopy(self.transport_binding))
        bindings = private_directory(self.root/'source-transport-bindings')
        path = bindings/(request['acquisition_id']+'.json')
        if not path.exists() and not path.is_symlink():
            # Old versions wrote only per-read intents. Do not let deleting or
            # lacking the new sidecar permit adoption of an old unbound read.
            inspected = 0
            for folder in self.root.iterdir():
                if not re.fullmatch('[a-f0-9]{32}',folder.name):
                    continue
                if folder.is_symlink() or not folder.is_dir():
                    raise AuthorSourceError('source_read_legacy_audit_unsafe')
                for previous in folder.glob('*-intent.json'):
                    inspected += 1
                    if inspected > 10000:
                        raise AuthorSourceError('source_read_legacy_audit_limit')
                    record = json.loads(read_regular(previous,1000000,private=True))
                    original = record.get('request') or {}
                    if original.get('acquisition_id') != request['acquisition_id']:
                        continue
                    if (record.get('host_alias') != binding['host_alias']
                            or record.get('transport_binding') != binding['transport_binding']
                            or any(original.get(key) != binding[key] for key in
                                   ('repository','commit','remote_root','receipt_sha256','inventory_sha256'))):
                        raise AuthorSourceError('source_read_transport_identity_already_bound')
        _bind_local_intent(path,binding)

    def read(self, receipt, selections, *, purpose):
        if purpose != 'author_reference':
            raise AuthorSourceError('author_source_forbidden_to_B')
        request = self._selection(receipt, selections)
        bundle = reader_tool_bundle()
        request_hash = sha256(canonical(request))
        self._bind_transport(request)
        command = _bound_transport_command(self.transport,self.transport_binding,self.policy['host_alias'],
            self.policy['remote_directory'],['python3','-I','-c',BOOTSTRAP])
        folder = private_directory(self.root / self.policy['reference_task_id'])
        observation = uuid.uuid4().hex
        metadata = dict(at=datetime.now(timezone.utc).isoformat(), host_alias=self.policy['host_alias'],
                        request=request, request_sha256=request_hash, bundle_sha256=sha256(bundle))
        if self.transport_binding is not None:
            metadata['transport_binding'] = deepcopy(self.transport_binding)
        _write_new(folder / (observation + '-intent.json'), metadata)
        envelope = canonical(dict(request=request, bundle=base64.b64encode(bundle).decode(), bundle_sha256=sha256(bundle)))
        try:
            transport = self.capture(command, timeout=60, max_bytes=_MAX_RESPONSE_BYTES,
                                     input_chunks=[envelope[i:i+65536] for i in range(0, len(envelope), 65536)])
        except OSError as exc:
            transport = dict(returncode=None, failure=type(exc).__name__, stdout='', stderr='')
        failure = _transport_failure(transport)
        report = dict(version=VERSION, state='unknown', scope='reference_only', B_input=False,
                      execution_authorized=False, physical_evaluation_performed=False,
                      request_sha256=request_hash, receipt_sha256=request['receipt_sha256'],
                      inventory_sha256=request['inventory_sha256'], repository=request['repository'],
                      commit=request['commit'], files=[])
        if transport.get('returncode') == 0 and not transport.get('failure'):
            try:
                observed = json.loads(base64.b64decode(transport['stdout'], validate=True))
                self._validate_response(observed, report, request)
                report = observed
            except (ValueError, KeyError, TypeError, UnicodeError):
                report['observation_failure'] = 'invalid_source_read_receipt'
        else:
            report['observation_failure'] = failure or 'source_read_transport_failed'
        # Never persist the transport stdout/stderr or returned source text.
        audit = deepcopy(report)
        for item in audit['files']:
            item.pop('text', None)
        audit.update(transport_returncode=transport.get('returncode'),
                     transport_failure=failure,
                     returned_text_persisted=False)
        if self.transport_binding is not None:
            audit['transport_binding'] = deepcopy(self.transport_binding)
        _write_new(folder / (observation + '-result.json'), audit)
        return report

    @staticmethod
    def _validate_response(observed, expected, request):
        rejection = isinstance(observed, dict) and observed.get('state') == 'rejected'
        expected_keys = set(expected) | ({'rejection_reason'} if rejection else set())
        if (not isinstance(observed, dict) or set(observed) != expected_keys
                or any(observed[key] != value for key, value in expected.items() if key not in {'state', 'files'})
                or observed['state'] not in {'read', 'rejected'} or not isinstance(observed['files'], list)
                or (not rejection and len(observed['files']) != len(request['selections']))):
            raise AuthorSourceError('source_read_response_identity_mismatch')
        if rejection:
            if (observed['files'] or not isinstance(observed['rejection_reason'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_]{1,100}', observed['rejection_reason'])):
                raise AuthorSourceError('source_read_rejection_identity_mismatch')
            return
        total = 0
        for item, selected in zip(observed['files'], request['selections']):
            if (not isinstance(item, dict) or set(item) != set(selected) | {'text', 'text_sha256', 'total_lines', 'returned_end_line', 'truncated'}
                    or any(item.get(key) != value for key, value in selected.items())
                    or not isinstance(item['text'], str) or '\0' in item['text']
                    or sha256(item['text'].encode('utf-8')) != item['text_sha256']
                    or type(item['total_lines']) is not int or item['total_lines'] < 0
                    or type(item['returned_end_line']) is not int
                    or item['returned_end_line'] != min(selected['end_line'], item['total_lines'])
                    or type(item['truncated']) is not bool
                    or item['truncated'] != (selected['start_line'] > 1 or selected['end_line'] < item['total_lines'])):
                raise AuthorSourceError('source_read_text_receipt_mismatch')
            count = len(item['text'].splitlines())
            if count != max(0, item['returned_end_line'] - selected['start_line'] + 1):
                raise AuthorSourceError('source_read_line_receipt_mismatch')
            raw = item['text'].encode('utf-8')
            if not item['truncated'] and (len(raw) != selected['size'] or sha256(raw) != selected['sha256']):
                raise AuthorSourceError('source_read_whole_text_identity_mismatch')
            total += len(raw)
        if total > MAX_TEXT_BYTES:
            raise AuthorSourceError('source_text_response_limit')
