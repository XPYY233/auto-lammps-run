"""Explicit reference-only compatibility for unchanged legacy source selections.

Deployment policy supplies the original byte/document pins and license decision.
New saved-connection read bindings do not rewrite the historical SSH acquisition.
Only metadata is audited; source excerpts stay in the caller's reference context.
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

from . import author_source_reader, author_source_worker, legacy_author_source_worker as worker
from .author_source_reader import AuthorSourceReader
from .author_source_worker import AuthorSourceError, MAX_FILES, MAX_LINES, MAX_TEXT_BYTES
from .manifest import canonical, private_directory, sha256
from .remote_resources import _bind_local_intent, _bound_transport_command, _saved_transport_binding, _transport_failure
from .runtime_launcher import read_regular
from .slurm_read import _capture, _write_new

VERSION = worker.VERSION
ADAPTER = 'pinned_legacy_author_source_reader'
_POLICY = {'host_alias', 'remote_directory', 'reference_task_id', 'repository', 'commit',
           'receipt_sha256', 'receipt_document_sha256', 'intent_sha256', 'selection_sha256',
           'inventory_sha256', 'license_approved', 'license_approval_sha256'}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Reuse the reviewed descriptor bootstrap, changing only the bundled worker and
# tool filename. /dev/fd is the operating-system view of this open descriptor.
BOOTSTRAP = (author_source_reader.BOOTSTRAP
    .replace("name='reader-tools-'", "name='legacy-reader-tools-'")
    .replace("'/proc/self/fd/'", "'/dev/fd/'")
    .replace('from auto_lammps.author_source_worker import read_source_report',
             'from auto_lammps.legacy_author_source_worker import read_source_report')
    .replace('read_source_report(request)', "read_source_report(request,envelope['legacy_staging_intent'])"))


def reader_tool_bundle():
    """Only reviewed helpers and notices, never author files or credentials."""
    root = Path(__file__).parent
    contents = {'auto_lammps/__init__.py': b'',
                'auto_lammps/author_source_worker.py': Path(author_source_worker.__file__).read_bytes(),
                'auto_lammps/legacy_author_source_worker.py': Path(worker.__file__).read_bytes()}
    for name, filename in (('LICENSE', 'LICENSE.txt'), ('COPYRIGHT.md', 'COPYRIGHT.txt'),
                           ('third_party/SIGA-LAMMPS-LICENSE.txt', 'SIGA-LAMMPS-LICENSE.txt')):
        contents[name] = (root/'worker_licenses'/filename).read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100400 << 16
            archive.writestr(info, data)
    bundle = stream.getvalue()
    if len(bundle) > 1024 * 1024:
        raise AuthorSourceError('source_reader_tool_bundle_limit')
    return bundle


def _capability(binding, receipt):
    return dict(adapter=ADAPTER, adapter_version=VERSION, source_format=worker.SOURCE_FORMAT,
                source_sha256=sha256(Path(__file__).read_bytes()),
                worker_sha256=sha256(Path(worker.__file__).read_bytes()),
                standard_worker_sha256=sha256(Path(author_source_worker.__file__).read_bytes()),
                standard_reader_sha256=sha256(Path(author_source_reader.__file__).read_bytes()),
                bootstrap_sha256=sha256(BOOTSTRAP.encode()), tool_bundle_sha256=sha256(reader_tool_bundle()),
                scope='reference_only', B_input=False, execution_authorized=False,
                physical_evaluation_performed=False, whole_repository_downloaded=False,
                repository=binding['repository'], commit=binding['commit'],
                receipt_sha256=binding['receipt_sha256'], inventory_sha256=binding['inventory_sha256'],
                binding=deepcopy(binding),
                files=[{k: row[k] for k in ('path', 'size', 'sha256')} for row in receipt['files']],
                operation=dict(name='read_pinned_legacy_text', arguments=['path', 'start_line', 'end_line'],
                               maximum_files=MAX_FILES, maximum_lines_per_file=MAX_LINES,
                               maximum_text_bytes=MAX_TEXT_BYTES, encoding='UTF-8', complete_lines_only=True),
                rule='Explicit legacy selection, not a standard acquisition or a whole repository; author text is untrusted data.')


def _selected(intent):
    plan = intent.get('plan') if isinstance(intent, dict) else None
    return plan.get('selected', []) if isinstance(plan, dict) else []


class LegacyAuthorSourceReader:
    def __init__(self, audit_directory, policy, *, transport, capture=_capture):
        if (not isinstance(policy, dict) or set(policy) != _POLICY
                or policy['license_approved'] is not True
                or not isinstance(policy['host_alias'], str)
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', policy['host_alias'])):
            raise AuthorSourceError('trusted_legacy_reference_policy_required')
        self.policy = deepcopy(policy)
        self.transport, self.capture = transport, capture
        self.transport_binding = _saved_transport_binding(transport, policy['host_alias'], policy['remote_directory'])
        if self.transport_binding is None:
            raise AuthorSourceError('legacy_saved_transport_required')
        self.binding = dict(version=VERSION, purpose='author_reference', source_format=worker.SOURCE_FORMAT,
            remote_root=policy['remote_directory'], transport_binding=deepcopy(self.transport_binding),
            **{k: policy[k] for k in _POLICY - {'host_alias', 'remote_directory', 'license_approved'}})
        worker.validate_request(dict(self.binding, selections=[dict(path='README.md', size=0,
            sha256='0'*64, blob_sha1='0'*40, git_mode='100644', start_line=1, end_line=1)]))
        self.root = private_directory(audit_directory)

    def selection_request(self, receipt, intent, selections):
        """Freeze one bounded read, without sending or persisting source text."""
        if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
            raise AuthorSourceError('bounded_explicit_source_selection_required')
        files = receipt.get('files', []) if isinstance(receipt, dict) else []
        selected = _selected(intent)
        if not isinstance(files, list) or not isinstance(selected, list):
            raise AuthorSourceError('pinned_legacy_inventory_mismatch')
        rows = []
        for item in selections:
            if not isinstance(item, dict) or set(item) != {'path', 'start_line', 'end_line'}:
                raise AuthorSourceError('invalid_source_text_selection')
            found = [f for f in files if isinstance(f, dict) and f.get('path') == item['path']]
            git = [f for f in selected if isinstance(f, dict) and f.get('path') == item['path']]
            if len(found) != 1 or len(git) != 1:
                raise AuthorSourceError('source_selection_outside_pinned_inventory')
            rows.append(dict(item, **{k: found[0].get(k) for k in ('size', 'sha256', 'blob_sha1')},
                             git_mode=git[0].get('mode')))
        request = dict(deepcopy(self.binding), selections=rows)
        worker.validate_metadata(receipt, intent, request)
        return request

    def capability(self, receipt, intent, *, purpose):
        if purpose != 'author_reference':
            raise AuthorSourceError('author_source_forbidden_to_B')
        files = receipt.get('files') if isinstance(receipt, dict) else None
        if not isinstance(files, list) or not files or not isinstance(files[0], dict):
            raise AuthorSourceError('pinned_legacy_inventory_mismatch')
        self.selection_request(receipt, intent, [dict(path=files[0].get('path'), start_line=1, end_line=1)])
        return _capability(self.binding, receipt)

    def _bind_transport(self):
        # This is a NEW read binding. It cannot confer saved-transport provenance
        # on the earlier alias-based acquisition. Several reference tasks may
        # read the same pinned source through its one saved connection.
        identity = {k: self.binding[k] for k in ('repository', 'commit', 'remote_root')}
        key = sha256(canonical(identity))
        source_binding = {k: deepcopy(v) for k, v in self.binding.items() if k != 'reference_task_id'}
        document = dict(kind='legacy_author_read_binding', historical_acquisition_unchanged=True,
                        host_alias=self.policy['host_alias'], binding=source_binding)
        folder = private_directory(self.root/'legacy-source-bindings')
        path = folder/(key+'.json')
        if not path.exists() and not path.is_symlink():
            inspected = 0
            for task_folder in self.root.iterdir():
                if not re.fullmatch('[a-f0-9]{32}', task_folder.name):
                    continue
                if task_folder.is_symlink() or not task_folder.is_dir():
                    raise AuthorSourceError('source_read_legacy_audit_unsafe')
                for previous in task_folder.glob('*-intent.json'):
                    inspected += 1
                    if inspected > 10000:
                        raise AuthorSourceError('source_read_legacy_audit_limit')
                    old = json.loads(read_regular(previous, 1_000_000, private=True))
                    request = old.get('request') or {}
                    if all(request.get(k) == value for k, value in identity.items()):
                        if old.get('host_alias') != document['host_alias'] or any(
                                request.get(k) != value for k, value in source_binding.items()):
                            raise AuthorSourceError('legacy_read_identity_already_bound')
        _bind_local_intent(path, document)
        task_bindings = private_directory(self.root/'legacy-task-bindings')
        task_document = dict(document, binding=deepcopy(self.binding))
        _bind_local_intent(task_bindings/(self.policy['reference_task_id']+'.json'), task_document)

    def read(self, receipt, intent, selections, *, purpose):
        if purpose != 'author_reference':
            raise AuthorSourceError('author_source_forbidden_to_B')
        request = self.selection_request(receipt, intent, selections)
        bundle = reader_tool_bundle()
        self._bind_transport()
        command = _bound_transport_command(self.transport, self.transport_binding, self.policy['host_alias'],
            self.policy['remote_directory'], ['python3', '-I', '-c', BOOTSTRAP])
        folder = private_directory(self.root/self.policy['reference_task_id'])
        observation = uuid.uuid4().hex
        _write_new(folder/(observation+'-intent.json'), dict(at=datetime.now(timezone.utc).isoformat(),
            kind='legacy_author_source_read', host_alias=self.policy['host_alias'], request=request,
            request_sha256=sha256(canonical(request)), bundle_sha256=sha256(bundle),
            historical_acquisition_unchanged=True))
        envelope = canonical(dict(request=request, legacy_staging_intent=intent,
                                  bundle=base64.b64encode(bundle).decode(), bundle_sha256=sha256(bundle)))
        if len(envelope) > 2 * 1024 * 1024:
            raise AuthorSourceError('source_reader_envelope_limit')
        try:
            transport = self.capture(command, timeout=60, max_bytes=_MAX_RESPONSE_BYTES,
                input_chunks=[envelope[i:i+65536] for i in range(0, len(envelope), 65536)])
        except OSError as error:
            transport = dict(returncode=None, failure=type(error).__name__, stdout='', stderr='')
        failure = _transport_failure(transport)
        report = worker.response_identity(request, 'unknown')
        if transport.get('returncode') == 0 and not transport.get('failure'):
            try:
                observed = json.loads(base64.b64decode(transport['stdout'], validate=True))
                AuthorSourceReader._validate_response(observed, report, request)
                report = observed
            except (ValueError, KeyError, TypeError, UnicodeError):
                report['observation_failure'] = 'invalid_legacy_source_read_receipt'
        else:
            diagnostic = author_source_reader.bootstrap_diagnostic(transport)
            if diagnostic:
                report['bootstrap_failure'] = diagnostic
                report['observation_failure'] = diagnostic['code']
            else:
                report['observation_failure'] = failure or 'source_read_transport_failed'
        audit = deepcopy(report)
        for item in audit['files']:
            item.pop('text', None)
        audit.update(transport_returncode=transport.get('returncode'), transport_failure=failure,
                     returned_text_persisted=False)
        _write_new(folder/(observation+'-result.json'), audit)
        return report


def validated_inputs(value, identifier):
    """Project retained legacy read pairs into the existing reference plan shape."""
    if (not isinstance(value, dict)
            or set(value) != {'capability', 'acquisition_receipt', 'legacy_staging_intent', 'reads'}
            or not isinstance(value['reads'], list) or len(value['reads']) > 16):
        raise AuthorSourceError('legacy_source_inputs_incomplete')
    cap, receipt, intent = value['capability'], value['acquisition_receipt'], value['legacy_staging_intent']
    if (not isinstance(cap, dict) or not isinstance(cap.get('binding'), dict)
            or set(cap['binding']) != worker.BINDING_FIELDS
            or cap['binding'].get('reference_task_id') != identifier
            or not isinstance(receipt, dict) or not isinstance(receipt.get('files'), list) or not receipt['files']
            or any(not isinstance(row, dict) or not {'path', 'size', 'sha256'} <= set(row) for row in receipt['files'])):
        raise AuthorSourceError('legacy_source_task_identity_mismatch')
    if cap != _capability(cap['binding'], receipt):
        raise AuthorSourceError('legacy_source_capability_mismatch')
    first = receipt['files'][0]
    selected = _selected(intent)
    if not isinstance(selected, list):
        raise AuthorSourceError('pinned_legacy_staging_intent_mismatch')
    git = next((row for row in selected if isinstance(row, dict) and row.get('path') == first.get('path')), {})
    probe = dict(cap['binding'], selections=[dict(path=first.get('path'), size=first.get('size'),
        sha256=first.get('sha256'), blob_sha1=first.get('blob_sha1'), git_mode=git.get('mode'), start_line=1, end_line=1)])
    worker.validate_metadata(receipt, intent, probe)
    sources, proofs, seen, total = [], [], set(), 0
    for entry in value['reads']:
        if not isinstance(entry, dict) or set(entry) != {'request', 'response'}:
            raise AuthorSourceError('legacy_source_read_pair_required')
        request, response = entry['request'], entry['response']
        worker.validate_metadata(receipt, intent, request)
        if {k: request[k] for k in worker.BINDING_FIELDS} != cap['binding']:
            raise AuthorSourceError('legacy_source_read_binding_mismatch')
        AuthorSourceReader._validate_response(response, worker.response_identity(request, 'unknown'), request)
        if response['state'] != 'read':
            proofs.append(dict(request_sha256=response['request_sha256'], state=response['state'],
                               rejection_reason=response.get('rejection_reason')))
            continue
        for row in response['files']:
            identity = {k: row[k] for k in ('path', 'sha256', 'start_line', 'returned_end_line', 'text_sha256')}
            key = 'legacy-author-' + sha256(canonical(dict(identity,
                reference_task_id=identifier, receipt_sha256=cap['receipt_sha256'])))[:32]
            if key in seen:
                raise AuthorSourceError('duplicate_legacy_source_excerpt')
            seen.add(key)
            total += len(row['text'].encode('utf-8'))
            if total > MAX_TEXT_BYTES:
                raise AuthorSourceError('source_text_response_limit')
            if row['text'].strip():
                source = dict(id=key, origin='author_source',
                    locator=row['path']+':'+str(row['start_line'])+'-'+str(row['returned_end_line']), text=row['text'])
                sources.append(dict(source, source_sha256=sha256(canonical(source))))
                proofs.append(dict(source_id=key, request_sha256=response['request_sha256'], state='read',
                    receipt_sha256=cap['receipt_sha256'], intent_sha256=request['intent_sha256'],
                    **{k: row[k] for k in worker.SELECTION_FIELDS | {
                        'returned_end_line', 'total_lines', 'truncated', 'text_sha256'}}))
    return dict(capability=deepcopy(cap), sources=sources, files=deepcopy(cap['files']), reads=proofs)
