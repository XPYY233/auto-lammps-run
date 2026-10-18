"""Reference-only text inspection of a separately pinned selected resource set.

This protocol never promotes selected completeness to whole-repository success.
Original acquisition/cache/selection receipts are checked before reading files.
"""
import hashlib
import json
import os
import re
import sys

from . import author_source_worker as original
from . import reference_selected_resource_worker as selected

VERSION = 1
PROTOCOL = 'selected_author_source_v1'
MAX_REQUEST_BYTES = 200_000
_CORE = {'version', 'purpose', 'reference_task_id', 'repository', 'commit',
         'remote_root', 'acquisition_id', 'receipt_sha256', 'inventory_sha256',
         'license_approval_sha256', 'selections'}
_EXTRA = {'protocol', 'selection_request', 'transport_binding_sha256'}
AuthorSourceError = original.AuthorSourceError


def validate_request(request):
    if not isinstance(request, dict) or set(request) != _CORE | _EXTRA or request['protocol'] != PROTOCOL:
        raise AuthorSourceError('selected_source_protocol_required')
    original.validate_request({key: request[key] for key in _CORE})
    selected.validate_selection_request(request['selection_request'])
    source = request['selection_request']['inventory_request']['acquisition_request']
    if (request['acquisition_id'] != request['selection_request']['id']
            or request['remote_root'] != source['directory']
            or any(request[key] != source[key] for key in ('repository', 'commit'))
            or not isinstance(request['transport_binding_sha256'], str)
            or not original._HASH.fullmatch(request['transport_binding_sha256'])
            or len(original.canonical(request)) > MAX_REQUEST_BYTES):
        raise AuthorSourceError('selected_source_identity_mismatch')
    return request


def validate_receipt(receipt, request):
    validate_request(request)
    selected.validate_selected_report(request['selection_request'], receipt)
    if (receipt['state'] != 'selected_complete' or receipt['selected_complete'] is not True
            or receipt['source_complete'] is not False
            or original.digest(original.canonical(receipt)) != request['receipt_sha256']
            or receipt['inventory_sha256'] != request['inventory_sha256']):
        raise AuthorSourceError('complete_selected_source_receipt_required')
    files = {row['path']: row for row in receipt['files']}
    for row in request['selections']:
        found = files.get(row['path'])
        if found is None or any(row[key] != found[key] for key in ('size', 'sha256')):
            raise AuthorSourceError('source_selection_outside_pinned_inventory')
    return files


def response_identity(request, state):
    return dict(original.response_identity(request, state), protocol=PROTOCOL,
        request_sha256=original.digest(original.canonical(request)),
        selection_sha256=selected.selection_identity(request['selection_request']),
        transport_binding_sha256=request['transport_binding_sha256'], source_complete=False)


def _saved_receipt(directory, selection):
    """Follow immutable same-operation receipts in explicit sequence, not time."""
    raw=original._read_regular(directory,'result.json',selected.MAX_REPORT)
    previous=selected.validate_selected_report(selection,json.loads(raw))
    resumes=sorted(name for name in os.listdir(directory)
        if re.fullmatch(r'resume-[0-9]{3}-intent\.json',name))
    if len(resumes)>selected.MAX_RESUMES:
        raise AuthorSourceError('selected_source_resume_chain_limit')
    for index,name in enumerate(resumes,1):
        if name!=f'resume-{index:03d}-intent.json':
            raise AuthorSourceError('selected_source_resume_chain_changed')
        intent=json.loads(original._read_regular(directory,name,2048))
        if (not isinstance(intent,dict) or set(intent)!={'request_sha256',
                'previous_receipt_sha256','previous_bundle_sha256'}
                or intent['request_sha256']!=original.digest(original.canonical(selection))
                or intent['previous_receipt_sha256']!=original.digest(original.canonical(previous))
                or not isinstance(intent['previous_bundle_sha256'],str)
                or not original._HASH.fullmatch(intent['previous_bundle_sha256'])
                or previous['state']!='partial'):
            raise AuthorSourceError('selected_source_resume_chain_changed')
        raw=original._read_regular(directory,name.replace('-intent.json','-result.json'),selected.MAX_REPORT)
        current=selected.validate_selected_report(selection,json.loads(raw))
        retained={row['path']:row for row in current['files']}
        if (any(retained.get(row['path'])!=row for row in previous['files'])
                or current['request_count']<previous['request_count']
                or current['received_bytes']<previous['received_bytes']
                or current.get('received_budget_bytes',current['received_bytes'])
                    <previous.get('received_budget_bytes',previous['received_bytes'])):
            raise AuthorSourceError('selected_source_resume_chain_changed')
        previous=current
    return raw


def read_pinned_text(request):
    validate_request(request)
    if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
        raise AuthorSourceError('source_read_requires_HPC_SSH')
    selection = request['selection_request']
    inventory = selected.inspect_cached_tree(selection['inventory_request'])
    selected.validate_selection_inventory(selection, inventory)
    root = original._root_descriptor(request['remote_root'])
    descriptors = []
    try:
        parent = root
        source_request = selection['inventory_request']['acquisition_request']
        for name in (source_request['id'], 'selected-resources', selection['id']):
            parent = original._directory(name, parent, private=True)
            descriptors.append(parent)
        intent = json.loads(original._read_regular(parent, 'intent.json', selected.MAX_REQUEST))
        if intent != selection:
            raise AuthorSourceError('selected_source_original_intent_changed')
        raw = _saved_receipt(parent, selection)
        if original.digest(raw) != request['receipt_sha256']:
            raise AuthorSourceError('selected_source_saved_receipt_changed')
        files = validate_receipt(json.loads(raw), request)
        source = original._directory('source', parent, private=True)
        descriptors.append(source)
        results, total = [], 0
        for row in request['selections']:
            current = os.dup(source)
            try:
                parts = row['path'].split('/')
                for name in parts[:-1]:
                    child = original._source_subdirectory(name, current)
                    os.close(current); current = child
                data = original._read_regular(current, parts[-1], original.MAX_FILE_BYTES, row['size'])
            finally:
                os.close(current)
            blob = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            if original.digest(data) != row['sha256'] or blob != files[row['path']]['blob_sha1']:
                raise AuthorSourceError('selected_source_file_integrity_changed')
            try:
                text = data.decode('utf-8', errors='strict')
            except UnicodeError:
                raise AuthorSourceError('source_file_not_UTF8_text') from None
            if '\0' in text:
                raise AuthorSourceError('source_file_contains_NUL')
            lines = text.splitlines(keepends=True)
            if row['start_line'] > max(1, len(lines)):
                raise AuthorSourceError('source_line_range_outside_file')
            returned = ''.join(lines[row['start_line']-1:row['end_line']])
            total += len(returned.encode('utf-8'))
            if total > original.MAX_TEXT_BYTES:
                raise AuthorSourceError('source_text_response_limit')
            results.append(dict(row, text=returned, text_sha256=original.digest(returned.encode()),
                total_lines=len(lines), returned_end_line=min(row['end_line'], len(lines)),
                truncated=row['start_line'] > 1 or row['end_line'] < len(lines)))
        result = response_identity(request, 'read'); result['files'] = results
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(root)


def read_source_report(request):
    validate_request(request)
    try:
        return read_pinned_text(request)
    except (ValueError, OSError) as error:
        result = response_identity(request, 'rejected')
        result['rejection_reason'] = (str(error) if isinstance(error, (AuthorSourceError, selected.AcquisitionError))
                                      else 'pinned_source_path_unavailable')
        return result
