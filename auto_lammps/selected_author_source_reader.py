"""Independent selected-set reader identity; old reader bytes stay unchanged."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

from . import selected_author_source_worker as worker
from . import reference_selected_resource_worker as selected
from . import author_source_worker as original
from .author_source_reader import (AuthorSourceReader, BOOTSTRAP as ORIGINAL_BOOTSTRAP,
    bootstrap_diagnostic, reader_tool_bundle)
from .author_resource_inventory_worker import _receipt
from .manifest import canonical, private_directory, sha256
from .remote_resources import (_bind_local_intent, _bound_transport_command,
    _saved_transport_binding, _transport_failure, RemoteResources)
from .runtime_launcher import read_regular
from .slurm_read import _capture, _write_new
from .tasks import TaskError

_POLICY = {'host_alias', 'remote_directory', 'reference_task_id', 'repository', 'commit',
    'license_approved', 'license_approval_sha256', 'selection_request_sha256', 'receipt_sha256'}
ADAPTER = 'pinned_selected_author_source_reader'
_OPERATION = dict(name='read_selected_pinned_text', arguments=['path', 'start_line', 'end_line'],
    maximum_files=original.MAX_FILES, maximum_lines_per_file=original.MAX_LINES,
    maximum_text_bytes=original.MAX_TEXT_BYTES, encoding='UTF-8', complete_lines_only=True)
_RULE = 'Only the exact selected set is complete; repository completeness and execution are not established.'
BOOTSTRAP = ORIGINAL_BOOTSTRAP.replace('from auto_lammps.author_source_worker import read_source_report',
    'from auto_lammps.selected_author_source_worker import read_source_report')


def selected_reader_tool_bundle():
    with zipfile.ZipFile(io.BytesIO(reader_tool_bundle())) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    for module in (worker, selected):
        contents['auto_lammps/'+Path(module.__file__).name] = Path(module.__file__).read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o100400 << 16
            archive.writestr(info, data)
    data = stream.getvalue()
    if len(data) > 1024**2:
        raise original.AuthorSourceError('source_reader_tool_bundle_limit')
    return data


def _bindings(selection_request, tree_inventory, original_receipt):
    selected.validate_selection_request(selection_request)
    inventory_request = selection_request['inventory_request']
    _receipt(original_receipt, inventory_request)
    RemoteResources.validate_tree_report(inventory_request, tree_inventory)
    selected.validate_selection_inventory(selection_request, tree_inventory)


class SelectedAuthorSourceReader:
    def __init__(self, audit_directory, policy, *, selection_request, tree_inventory,
                 original_receipt, transport, capture=_capture):
        if (not isinstance(policy, dict) or set(policy) != _POLICY
                or policy['license_approved'] is not True
                or not isinstance(policy['host_alias'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', policy['host_alias'])
                or transport is None):
            raise original.AuthorSourceError('trusted_selected_reference_policy_required')
        _bindings(selection_request, tree_inventory, original_receipt)
        source = selection_request['inventory_request']['acquisition_request']
        if (policy['selection_request_sha256'] != sha256(canonical(selection_request))
                or policy['remote_directory'] != source['directory']
                or any(policy[key] != source[key] for key in ('repository', 'commit'))):
            raise original.AuthorSourceError('selected_source_trusted_binding_changed')
        self.policy, self.selection = deepcopy(policy), deepcopy(selection_request)
        self.tree_inventory, self.original_receipt = deepcopy(tree_inventory), deepcopy(original_receipt)
        self.transport, self.capture = transport, capture
        self.transport_binding = _saved_transport_binding(transport, policy['host_alias'], policy['remote_directory'])
        self.root = private_directory(audit_directory)
        self._request([dict(path='README.md', size=0, sha256='0'*64, start_line=1, end_line=1)])

    def _request(self, selections, *, inventory_sha256='0'*64):
        request = dict(version=worker.VERSION, purpose='author_reference', protocol=worker.PROTOCOL,
            **{key: self.policy[key] for key in ('reference_task_id', 'repository', 'commit',
                'receipt_sha256', 'license_approval_sha256')},
            remote_root=self.policy['remote_directory'], acquisition_id=self.selection['id'],
            inventory_sha256=inventory_sha256,
            selection_request=deepcopy(self.selection), selections=deepcopy(selections),
            transport_binding_sha256=sha256(canonical(self.transport_binding)))
        return worker.validate_request(request)

    def selection_request(self, receipt, selections):
        if (not isinstance(selections, list) or not 1 <= len(selections) <= original.MAX_FILES
                or not isinstance(receipt, dict) or not isinstance(receipt.get('files'), list)):
            raise original.AuthorSourceError('bounded_explicit_source_selection_required')
        rows = []
        for choice in selections:
            if not isinstance(choice, dict) or set(choice) != {'path', 'start_line', 'end_line'}:
                raise original.AuthorSourceError('invalid_source_text_selection')
            found = [row for row in receipt['files'] if row.get('path') == choice['path']]
            if len(found) != 1:
                raise original.AuthorSourceError('source_selection_outside_pinned_inventory')
            rows.append(dict(choice, size=found[0]['size'], sha256=found[0]['sha256']))
        request = self._request(rows, inventory_sha256=receipt.get('inventory_sha256'))
        worker.validate_receipt(receipt, request)
        return request

    _selection = selection_request

    def capability(self, receipt, *, purpose):
        if purpose != 'author_reference':
            raise original.AuthorSourceError('author_source_forbidden_to_B')
        if not isinstance(receipt, dict) or not isinstance(receipt.get('files'), list) or not receipt['files']:
            raise original.AuthorSourceError('complete_selected_source_receipt_required')
        first = receipt['files'][0]
        request = self.selection_request(receipt, [dict(path=first.get('path'), start_line=1, end_line=1)])
        return dict(adapter=ADAPTER, adapter_version=worker.VERSION,
            source_sha256=sha256(Path(__file__).read_bytes()), worker_sha256=sha256(Path(worker.__file__).read_bytes()),
            selected_worker_sha256=sha256(Path(selected.__file__).read_bytes()),
            scope='reference_only', B_input=False, execution_authorized=False, physical_evaluation_performed=False,
            source_complete=False, selected_complete=True, repository=request['repository'], commit=request['commit'],
            reference_task_id=request['reference_task_id'],
            receipt_sha256=request['receipt_sha256'], inventory_sha256=request['inventory_sha256'],
            selection_request_sha256=sha256(canonical(self.selection)), selection_sha256=selected.selection_identity(self.selection),
            transport_binding_sha256=request['transport_binding_sha256'],
            license_approval_sha256=request['license_approval_sha256'],
            files=[{key: row[key] for key in ('path', 'size', 'sha256')} for row in receipt['files']],
            operation=deepcopy(_OPERATION), rule=_RULE)

    def retained(self, receipt, reads):
        return dict(capability=self.capability(receipt, purpose='author_reference'),
            acquisition_receipt=deepcopy(receipt), selection_request=deepcopy(self.selection),
            tree_inventory=deepcopy(self.tree_inventory), original_receipt=deepcopy(self.original_receipt),
            reads=deepcopy(reads))

    def read(self, receipt, selections, *, purpose):
        if purpose != 'author_reference':
            raise original.AuthorSourceError('author_source_forbidden_to_B')
        request = self.selection_request(receipt, selections)
        binding = dict(request_sha256=sha256(canonical(self.selection)),
            receipt_sha256=request['receipt_sha256'], transport_binding=deepcopy(self.transport_binding))
        folder = private_directory(self.root/'selected-source-transport-bindings')
        _bind_local_intent(folder/(self.selection['id']+'.json'), binding)
        bundle = selected_reader_tool_bundle()
        command = _bound_transport_command(self.transport, self.transport_binding, self.policy['host_alias'],
            self.policy['remote_directory'], ['python3', '-I', '-c', BOOTSTRAP])
        folder = private_directory(self.root/self.policy['reference_task_id']); observation = uuid.uuid4().hex
        _write_new(folder/(observation+'-intent.json'), dict(at=datetime.now(timezone.utc).isoformat(),
            request=request, request_sha256=sha256(canonical(request)), bundle_sha256=sha256(bundle),
            transport_binding=deepcopy(self.transport_binding)))
        envelope = canonical(dict(request=request, bundle=base64.b64encode(bundle).decode(), bundle_sha256=sha256(bundle)))
        try:
            result = self.capture(command, timeout=60, max_bytes=2*1024**2,
                input_chunks=[envelope[index:index+65536] for index in range(0, len(envelope), 65536)])
        except OSError as error:
            result = dict(returncode=None, failure=type(error).__name__, stdout='', stderr='')
        failure = _transport_failure(result); report = worker.response_identity(request, 'unknown')
        if result.get('returncode') == 0 and not failure:
            try:
                observed = json.loads(base64.b64decode(result['stdout'], validate=True))
                AuthorSourceReader._validate_response(observed, report, request); report = observed
            except (ValueError, KeyError, TypeError, UnicodeError):
                report['observation_failure'] = 'invalid_selected_source_read_receipt'
        else:
            diagnostic = bootstrap_diagnostic(result)
            if diagnostic:
                report['bootstrap_failure'] = diagnostic
            report['observation_failure'] = failure or 'selected_source_read_transport_failed'
        audit = deepcopy(report)
        for item in audit['files']:
            item.pop('text', None)
        audit.update(returned_text_persisted=False, transport_failure=failure,
            transport_returncode=result.get('returncode'), transport_binding=deepcopy(self.transport_binding))
        _write_new(folder/(observation+'-result.json'), audit)
        return report


def validated_inputs(value, identifier):
    from .author_reference_plan import _source
    expected = {'capability', 'acquisition_receipt', 'selection_request', 'tree_inventory', 'original_receipt', 'reads'}
    try:
        if not isinstance(value, dict) or set(value) != expected or not isinstance(value['reads'], list) or len(value['reads']) > 16:
            raise ValueError('selected source receipt fields')
        capability, receipt, selection = value['capability'], value['acquisition_receipt'], value['selection_request']
        _bindings(selection, value['tree_inventory'], value['original_receipt'])
        selected.validate_selected_report(selection, receipt)
        fixed = dict(adapter=ADAPTER, adapter_version=worker.VERSION,
            source_sha256=sha256(Path(__file__).read_bytes()), worker_sha256=sha256(Path(worker.__file__).read_bytes()),
            selected_worker_sha256=sha256(Path(selected.__file__).read_bytes()),
            scope='reference_only', B_input=False, execution_authorized=False, physical_evaluation_performed=False,
            source_complete=False, selected_complete=True, repository=receipt['repository'], commit=receipt['commit'],
            reference_task_id=identifier, operation=deepcopy(_OPERATION), rule=_RULE,
            receipt_sha256=sha256(canonical(receipt)), inventory_sha256=receipt['inventory_sha256'],
            selection_request_sha256=sha256(canonical(selection)), selection_sha256=selected.selection_identity(selection),
            files=[{key: row[key] for key in ('path', 'size', 'sha256')} for row in receipt['files']])
        if (not isinstance(capability, dict) or any(capability.get(key) != item or type(capability.get(key)) is not type(item)
                for key, item in fixed.items()) or receipt['state'] != 'selected_complete'):
            raise ValueError('selected capability changed')
        for key in ('transport_binding_sha256', 'license_approval_sha256'):
            if not isinstance(capability.get(key), str) or not original._HASH.fullmatch(capability[key]):
                raise ValueError('selected reference approval or transport missing')
        sources, proofs, seen, total = [], [], set(), 0
        for entry in value['reads']:
            if not isinstance(entry, dict) or set(entry) != {'request', 'response'}:
                raise ValueError('selected source request response')
            request, response = entry['request'], entry['response']
            worker.validate_receipt(receipt, request)
            if (request['reference_task_id'] != identifier or request['selection_request'] != selection
                    or any(request[key] != capability[key] for key in ('transport_binding_sha256', 'license_approval_sha256'))):
                raise ValueError('selected reference task or transport changed')
            AuthorSourceReader._validate_response(response, worker.response_identity(request, 'unknown'), request)
            if response['state'] != 'read':
                proofs.append(dict(request_sha256=response['request_sha256'], state=response['state'],
                    rejection_reason=response.get('rejection_reason')))
                continue
            for item in response['files']:
                identity = {key: item[key] for key in ('path', 'sha256', 'start_line', 'returned_end_line', 'text_sha256')}
                source_id = 'author-'+sha256(canonical(identity))[:32]
                if source_id in seen:
                    raise ValueError('duplicate selected source text')
                seen.add(source_id); total += len(item['text'].encode())
                if total > original.MAX_TEXT_BYTES:
                    raise ValueError('selected source text limit')
                if item['text'].strip():
                    sources.append(_source(source_id, 'author_source', item['path']+':'+str(item['start_line'])+'-'+str(item['returned_end_line']), item['text']))
                    proofs.append(dict(source_id=source_id, request_sha256=response['request_sha256'], state='read',
                        **{key: item[key] for key in ('path', 'sha256', 'size', 'start_line', 'end_line',
                            'returned_end_line', 'total_lines', 'truncated', 'text_sha256')}))
        return dict(capability=deepcopy(capability), sources=sources, files=deepcopy(capability['files']), reads=proofs)
    except (ValueError, TypeError, KeyError, IndexError):
        raise TaskError('所选作者原件、原获取树、许可或本任务读取回执未通过独立协议核验。') from None
