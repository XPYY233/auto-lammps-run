"""Read pinned pre-service author selections; never manufacture acquisitions."""
import hashlib
import json
import os
import re
import sys

from .author_source_worker import (
    AuthorSourceError, MAX_FILES, MAX_FILE_BYTES, MAX_LINES, MAX_RECEIPT_BYTES,
    MAX_TEXT_BYTES, _absolute_root, _directory, _read_regular, _relative_path,
    _root_descriptor, canonical, digest,
)

VERSION = 1
SOURCE_FORMAT = 'legacy_selected_resource_v3'
MAX_INTENT_BYTES = 500_000
MAX_REQUEST_BYTES = 32 * 1024
_HASH = re.compile('[a-f0-9]{64}')
_GIT = re.compile('[a-f0-9]{40}')
_ID = re.compile('[a-f0-9]{32}')
BINDING_FIELDS = {
    'version', 'purpose', 'source_format', 'reference_task_id', 'repository', 'commit',
    'remote_root', 'receipt_sha256', 'receipt_document_sha256', 'intent_sha256',
    'selection_sha256', 'inventory_sha256', 'license_approval_sha256', 'transport_binding',
}
SELECTION_FIELDS = {'path', 'size', 'sha256', 'blob_sha1', 'git_mode', 'start_line', 'end_line'}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != BINDING_FIELDS | {'selections'}
            or type(request['version']) is not int or request['version'] != VERSION
            or request['source_format'] != SOURCE_FORMAT or request['purpose'] != 'author_reference'
            or not _matches(_ID, request['reference_task_id'])
            or not _matches(_GIT, request['commit'])
            or not isinstance(request['repository'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}', request['repository'])
            or any(p in {'.', '..'} for p in request['repository'].split('/'))):
        raise AuthorSourceError('legacy_reference_source_binding_required')
    _absolute_root(request['remote_root'])
    for name in ('receipt_sha256', 'receipt_document_sha256', 'intent_sha256',
                 'selection_sha256', 'inventory_sha256', 'license_approval_sha256'):
        if not _matches(_HASH, request[name]):
            raise AuthorSourceError('legacy_source_digest_required')
    transport = request['transport_binding']
    if (not isinstance(transport, dict) or set(transport) != {'identity_sha256', 'revision'}
            or not _matches(_HASH, transport['identity_sha256'])
            or type(transport['revision']) is not int or transport['revision'] < 1):
        raise AuthorSourceError('legacy_saved_transport_binding_required')
    selections = request['selections']
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
        raise AuthorSourceError('bounded_explicit_source_selection_required')
    seen = set()
    for row in selections:
        if (not isinstance(row, dict) or set(row) != SELECTION_FIELDS
                or type(row['size']) is not int or not 0 <= row['size'] <= MAX_FILE_BYTES
                or not _matches(_HASH, row['sha256']) or not _matches(_GIT, row['blob_sha1'])
                or row['git_mode'] not in ('100644', '100755')
                or type(row['start_line']) is not int or type(row['end_line']) is not int
                or not 1 <= row['start_line'] <= row['end_line'] <= 1_000_000
                or row['end_line'] - row['start_line'] + 1 > MAX_LINES):
            raise AuthorSourceError('invalid_legacy_source_selection')
        path = _relative_path(row['path'])
        if path in seen:
            raise AuthorSourceError('duplicate_source_text_selection')
        seen.add(path)
    if len(canonical(request)) > MAX_REQUEST_BYTES:
        raise AuthorSourceError('source_read_request_limit')
    return request


def validate_metadata(receipt, intent, request):
    """Check genuine legacy documents without changing their schema or identity."""
    validate_request(request)
    required = {'repository', 'commit', 'tree_sha1', 'selection_sha256', 'remote_directory',
                'files', 'bytes', 'request_count', 'whole_repository_downloaded',
                'selection_complete', 'source_executed', 'scientific_validation', 'cached_reuse'}
    if (not isinstance(receipt, dict) or not required <= set(receipt)
            or set(receipt) - required - {'potential_inspection', 'eam_header'}
            or len(canonical(receipt)) > MAX_RECEIPT_BYTES
            or digest(canonical(receipt)) != request['receipt_document_sha256']
            or receipt['repository'] != request['repository'] or receipt['commit'] != request['commit']
            or receipt['remote_directory'] != request['remote_root'] + '/source'
            or receipt['whole_repository_downloaded'] is not False
            or receipt['selection_complete'] is not True or receipt['source_executed'] is not False
            or receipt['scientific_validation'] is not False or type(receipt['cached_reuse']) is not bool
            or not _matches(_GIT, receipt['tree_sha1'])
            or receipt['selection_sha256'] != request['selection_sha256']
            or type(receipt['request_count']) is not int or receipt['request_count'] < 0):
        raise AuthorSourceError('pinned_legacy_receipt_mismatch')
    if (not isinstance(intent, dict) or set(intent) != {'plan', 'selection_sha256'}
            or len(canonical(intent)) > MAX_INTENT_BYTES
            or digest(canonical(intent)) != request['intent_sha256']
            or intent['selection_sha256'] != request['selection_sha256']):
        raise AuthorSourceError('pinned_legacy_staging_intent_mismatch')
    plan = intent['plan']
    if (not isinstance(plan, dict) or set(plan) != {'repository', 'commit', 'label', 'tree_sha1', 'selected'}
            or any(plan[k] != receipt[k] for k in ('repository', 'commit', 'tree_sha1'))
            or not isinstance(plan['label'], str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}', plan['label'])
            or digest(canonical(plan)) != request['selection_sha256']
            or not isinstance(plan['selected'], list) or not 1 <= len(plan['selected']) <= 128):
        raise AuthorSourceError('pinned_legacy_selection_mismatch')
    files = receipt['files']
    if (not isinstance(files, list) or len(files) != len(plan['selected'])
            or digest(canonical(files)) != request['inventory_sha256']):
        raise AuthorSourceError('pinned_legacy_inventory_mismatch')
    inventory, selected = {}, {}
    for row in files:
        if (not isinstance(row, dict) or set(row) != {'path', 'size', 'blob_sha1', 'sha256'}
                or type(row['size']) is not int or not 0 <= row['size'] <= MAX_FILE_BYTES
                or not _matches(_HASH, row['sha256']) or not _matches(_GIT, row['blob_sha1'])):
            raise AuthorSourceError('invalid_legacy_source_inventory')
        path = _relative_path(row['path'])
        if path in inventory:
            raise AuthorSourceError('duplicate_legacy_source_path')
        inventory[path] = row
    for row in plan['selected']:
        if (not isinstance(row, dict) or set(row) != {'path', 'mode', 'type', 'sha', 'size', 'url'}
                or row['mode'] not in ('100644', '100755') or row['type'] != 'blob'
                or not isinstance(row['url'], str) or not 1 <= len(row['url']) <= 2048):
            raise AuthorSourceError('invalid_legacy_git_selection')
        path = _relative_path(row['path'])
        original = inventory.get(path)
        if (path in selected or original is None or row['sha'] != original['blob_sha1']
                or type(row['size']) is not int or row['size'] != original['size']):
            raise AuthorSourceError('legacy_git_selection_inventory_mismatch')
        selected[path] = row
    if (type(receipt['bytes']) is not int or receipt['bytes'] != sum(row['size'] for row in files)
            or set(inventory) != set(selected)):
        raise AuthorSourceError('legacy_selection_completeness_mismatch')
    for row in request['selections']:
        original, git = inventory.get(row['path']), selected.get(row['path'])
        if (original is None or any(row[k] != original[k] for k in ('size', 'sha256', 'blob_sha1'))
                or row['git_mode'] != git['mode']):
            raise AuthorSourceError('source_selection_outside_pinned_inventory')
    return inventory


def response_identity(request, state):
    return dict(version=VERSION, state=state, source_format=SOURCE_FORMAT,
                scope='reference_only', B_input=False, execution_authorized=False,
                physical_evaluation_performed=False, whole_repository_downloaded=False,
                request_sha256=digest(canonical(request)),
                **{k: request[k] for k in BINDING_FIELDS - {'version', 'purpose', 'source_format'}}, files=[])


def read_pinned_text(request, intent):
    validate_request(request)
    if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
        raise AuthorSourceError('source_read_requires_HPC_SSH')
    root = _root_descriptor(request['remote_root'])
    source = None
    try:
        raw = _read_regular(root, 'selected-resource-receipt.json', MAX_RECEIPT_BYTES)
        if digest(raw) != request['receipt_sha256']:
            raise AuthorSourceError('saved_legacy_receipt_SHA256_mismatch')
        try:
            receipt = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AuthorSourceError('invalid_saved_legacy_receipt') from None
        validate_metadata(receipt, intent, request)
        source = _directory('source', root, private=True)
        results, total = [], 0
        for selection in request['selections']:
            parent = os.dup(source)
            try:
                parts = selection['path'].split('/')
                for part in parts[:-1]:
                    child = _directory(part, parent, private=True)
                    os.close(parent)
                    parent = child
                data = _read_regular(parent, parts[-1], MAX_FILE_BYTES, selection['size'])
            finally:
                os.close(parent)
            if digest(data) != selection['sha256']:
                raise AuthorSourceError('source_file_SHA256_mismatch')
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
            if blob != selection['blob_sha1']:
                raise AuthorSourceError('source_file_git_blob_mismatch')
            try:
                contents = data.decode('utf-8', errors='strict')
            except UnicodeError:
                raise AuthorSourceError('source_file_not_UTF8_text') from None
            if '\0' in contents:
                raise AuthorSourceError('source_file_contains_NUL')
            lines = contents.splitlines(keepends=True)
            if selection['start_line'] > max(1, len(lines)):
                raise AuthorSourceError('source_line_range_outside_file')
            returned = ''.join(lines[selection['start_line']-1:selection['end_line']])
            total += len(returned.encode('utf-8'))
            if total > MAX_TEXT_BYTES:
                raise AuthorSourceError('source_text_response_limit')
            results.append(dict(selection, text=returned, text_sha256=digest(returned.encode('utf-8')),
                                total_lines=len(lines), returned_end_line=min(selection['end_line'], len(lines)),
                                truncated=selection['start_line'] > 1 or selection['end_line'] < len(lines)))
        report = response_identity(request, 'read')
        report['files'] = results
        return report
    finally:
        if source is not None:
            os.close(source)
        os.close(root)


def read_source_report(request, intent):
    validate_request(request)
    try:
        return read_pinned_text(request, intent)
    except (AuthorSourceError, OSError) as error:
        report = response_identity(request, 'rejected')
        report['rejection_reason'] = str(error) if isinstance(error, AuthorSourceError) else 'pinned_source_path_unavailable'
        return report
