"""Inspect a pinned acquisition's cached Git metadata on HPC.

This is a reference-only diagnostic. It neither contacts GitHub nor opens any
author source/blob, and it cannot authorize acquisition or physical evaluation.
Only the original lock, intent, receipt and first two fixed API cache records
are read. Unsupported members are reported individually, not executed or hidden.
"""
import base64
from collections import Counter
import json
import os
from pathlib import Path
import re
import stat
import sys

from .manifest import canonical, sha256
from .potential_acquisition import AcquisitionError, safe_path
from .reference_resource_worker import (MAX_BLOB, MAX_RESPONSE, MAX_SOURCE,
                                        MAX_TRANSFER, validate_request)


STATUSES = frozenset({'supported_blob', 'oversized_blob', 'directory',
                      'nonregular_member', 'invalid_metadata', 'unsafe_path'})
LIMITS = dict(tree_entries=2000, source_files=128, source_bytes=MAX_SOURCE,
              blob_bytes=MAX_BLOB)
CACHE_LIMIT = 4 * ((MAX_RESPONSE + 2) // 3) + 4096
CACHE_KEYS = ('commit_request', 'commit_response', 'tree_request', 'tree_response')
MAX_REPORT = 2 * 1024**2


def validate_inventory_request(request):
    if (not isinstance(request, dict) or set(request) !=
            {'version', 'acquisition_request', 'receipt_sha256', 'tree_sha1'}
            or type(request['version']) is not int or request['version'] != 1
            or any(not isinstance(request[k], str) or not re.fullmatch('[a-f0-9]{'+str(n)+'}', request[k])
                   for k, n in (('receipt_sha256', 64), ('tree_sha1', 40)))):
        raise AcquisitionError('invalid_cached_tree_request')
    original = validate_request(request['acquisition_request'])
    if str(Path(original['directory'])) != original['directory']:
        raise AcquisitionError('invalid_cached_tree_directory')
    return request


def _directory(path):
    """Open an existing owned private directory without following ancestors."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path) == '/':
        raise AcquisitionError('invalid_cached_tree_directory')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            # Shared HPC ancestors can allow traversal without directory listing.
            # The actual private root still needs a readable descriptor.
            access = os.O_RDONLY if index == len(parts)-1 else getattr(os, 'O_PATH', os.O_RDONLY)
            child = os.open(part, access | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _private_directory(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_directory(descriptor):
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise AcquisitionError('cached_tree_private_owned_directory_required')


def _read(directory, name, limit):
    """Read a fixed basename, rejecting links, special files and concurrent edits."""
    if not re.fullmatch(r'[A-Za-z0-9.-]+', name):
        raise AcquisitionError('invalid_cached_tree_record_name')
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_mode & 0o077
                or not 0 <= before.st_size <= limit):
            raise AcquisitionError('unsafe_cached_tree_record')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(descriptor)
        fields = ('st_ino', 'st_size', 'st_mode', 'st_uid', 'st_nlink',
                  'st_mtime_ns', 'st_ctime_ns')
        if (len(data) != before.st_size or len(data) > limit
                or any(getattr(before, key) != getattr(after, key) for key in fields)):
            raise AcquisitionError('cached_tree_record_changed')
        return data
    finally:
        os.close(descriptor)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AcquisitionError('duplicate_cached_tree_json_key')
            result[key] = value
        return result

    def invalid_constant(value):
        raise AcquisitionError('invalid_cached_tree_json_constant')

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AcquisitionError('invalid_cached_tree_json') from exc


def _response(raw):
    envelope = _json(raw)
    if (not isinstance(envelope, dict) or set(envelope) != {'returncode', 'failure', 'stdout', 'stderr'}
            or type(envelope['returncode']) is not int or envelope['returncode'] != 0
            or envelope['failure'] != '' or envelope['stderr'] != ''
            or not isinstance(envelope['stdout'], str)):
        raise AcquisitionError('failed_cached_tree_response')
    try:
        data = base64.b64decode(envelope['stdout'], validate=True)
    except (ValueError, TypeError) as exc:
        raise AcquisitionError('invalid_cached_tree_response_base64') from exc
    if (len(data) > MAX_RESPONSE
            or base64.b64encode(data).decode('ascii') != envelope['stdout']):
        raise AcquisitionError('invalid_cached_tree_response_size_or_encoding')
    return _json(data), len(data)


def _row(entry, index):
    """Project untrusted metadata to bounded scalar diagnostics only."""
    if not isinstance(entry, dict):
        return dict(path=f'<invalid entry {index}>', type=None, mode=None, size=None, sha=None)
    path = entry.get('path')
    if not isinstance(path, str):
        path = f'<invalid path at entry {index}>'
    elif any(ord(character) < 32 or ord(character) > 126 for character in path):
        path = path.encode('unicode_escape').decode('ascii')
    if len(path) > 500:
        path = '<path truncated> ' + path[:483]
    row = dict(path=path)
    for key in ('type', 'mode', 'sha'):
        value = entry.get(key)
        row[key] = (value if isinstance(value, str) and len(value) <= 100
                    and all(32 <= ord(character) <= 126 for character in value) else None)
    size = entry.get('size')
    row['size'] = size if type(size) is int and size >= 0 else None
    # A missing directory size is normal. Invalid scalar sizes must not become
    # an apparently valid directory after projection.
    if (not isinstance(entry.get('path'), str)
            or ('size' in entry and size is not None and row['size'] is None)):
        row['type'] = None
    return row


def _status(row, seen):
    path = row['path']
    try:
        safe_path(path)
        if path in seen or '.git' in path.split('/'):
            raise AcquisitionError('unsafe_or_duplicate_source_path')
    except AcquisitionError:
        return 'invalid_metadata' if row['type'] is None else 'unsafe_path'
    if (row['type'] is None or row['mode'] is None or row['sha'] is None
            or not re.fullmatch('[a-f0-9]{40}', row['sha'])):
        return 'invalid_metadata'
    if row['type'] == 'tree' and row['mode'] == '040000':
        return 'directory' if row['size'] is None else 'invalid_metadata'
    if row['type'] == 'blob' and row['mode'] in {'100644', '100755'}:
        if type(row['size']) is not int:
            return 'invalid_metadata'
        return 'oversized_blob' if row['size'] > MAX_BLOB else 'supported_blob'
    if ((row['type'], row['mode']) in {('blob', '120000'), ('commit', '160000')}
            and (row['type'] != 'blob' or type(row['size']) is int)):
        return 'nonregular_member'
    return 'invalid_metadata'


def classify_entries(entries):
    if not isinstance(entries, list) or len(entries) > LIMITS['tree_entries']:
        raise AcquisitionError('incomplete_cached_source_tree')
    rows = []
    seen = set()
    for index, entry in enumerate(entries):
        row = _row(entry, index)
        row['status'] = _status(row, seen)
        seen.add(row['path'])
        rows.append(row)
    return rows


def inventory_summary(entries, cache_sha256):
    counts = {status: 0 for status in sorted(STATUSES)}
    counts.update(Counter(row['status'] for row in entries))
    total = sum(row['size'] for row in entries if row['status'] == 'supported_blob')
    blocked = len(entries) - counts['directory'] - counts['supported_blob']
    reasons = []
    if blocked:
        reasons.append('unsupported_members')
    if not counts['supported_blob']:
        reasons.append('no_supported_blobs')
    if counts['supported_blob'] > LIMITS['source_files']:
        reasons.append('source_file_count_limit')
    if total > LIMITS['source_bytes']:
        reasons.append('source_bytes_limit')
    return dict(entry_count=len(entries), counts=counts, supported_blob_bytes=total,
                blocking_entry_count=blocked, acquisition_limit_reasons=reasons,
                limits=dict(LIMITS), cache_sha256=cache_sha256,
                cached_tree_only=True, file_contents_read=False,
                source_complete=False, execution_ready=False)


def _receipt(value, request):
    original = request['acquisition_request']
    expected = dict(schema_version=1, id=original['id'], repository=original['repository'],
                    commit=original['commit'], location='hpc',
                    remote_directory=original['directory']+'/'+original['id'],
                    request_sha256=sha256(canonical(original)),
                    bundle_sha256=original['bundle_sha256'], tree_sha1=request['tree_sha1'],
                    scientific_validation=False, execution_authorized=False,
                    author_identity_verified=False)
    if (not isinstance(value, dict)
            or any(value.get(k) != v or type(value.get(k)) is not type(v) for k, v in expected.items())
            or sha256(canonical(value)) != request['receipt_sha256']
            or value.get('state') not in {'finished', 'partial'}
            or type(value.get('source_complete')) is not bool
            or type(value.get('request_count')) is not int or not 2 <= value['request_count'] <= 30
            or type(value.get('received_bytes')) is not int or not 0 <= value['received_bytes'] <= MAX_TRANSFER
            or not isinstance(value.get('files'), list) or len(value['files']) > 128
            or type(value.get('source_bytes')) is not int or not 0 <= value['source_bytes'] <= MAX_SOURCE
            or value.get('inventory_sha256') != sha256(canonical(value['files']))):
        raise AcquisitionError('original_cached_tree_receipt_mismatch')
    files = value['files']
    seen = set()
    for item in files:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or item['path'] in seen or '.git' in item['path'].split('/')
                or type(item.get('size')) is not int or not 0 <= item['size'] <= MAX_BLOB
                or item.get('git_mode') not in {'100644', '100755'}
                or any(not isinstance(item.get(k), str) or not re.fullmatch('[a-f0-9]{'+str(n)+'}', item[k])
                       for k, n in (('sha256', 64), ('blob_sha1', 40)))):
            raise AcquisitionError('original_cached_tree_file_inventory_invalid')
        safe_path(item['path'])
        seen.add(item['path'])
    if sum(item['size'] for item in files) != value['source_bytes']:
        raise AcquisitionError('original_cached_tree_file_inventory_invalid')
    return value


def inspect_cached_tree(request):
    validate_inventory_request(request)
    if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
        raise AcquisitionError('cached_tree_inspection_requires_HPC')
    original = request['acquisition_request']
    root = folder = None
    try:
        root = _directory(original['directory'])
        if _json(_read(root, original['id']+'.lock.json', 20000)) != original:
            raise AcquisitionError('original_cached_tree_lock_mismatch')
        folder = os.open(original['id'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        _private_directory(folder)
        if _json(_read(folder, 'intent.json', 20000)) != original:
            raise AcquisitionError('original_cached_tree_intent_mismatch')
        receipt = _receipt(_json(_read(folder, 'result.json', 500000)), request)
        records = {}
        hashes = {}
        for key, name, limit in (('commit_request', '01-request.json', 20000),
                                  ('commit_response', '01-response.json', CACHE_LIMIT),
                                  ('tree_request', '02-request.json', 20000),
                                  ('tree_response', '02-response.json', CACHE_LIMIT)):
            records[key] = _read(folder, name, min(limit, original['storage_bytes']))
            hashes[key] = sha256(records[key])
        prefix = 'repos/'+original['repository']+'/git/'
        if _json(records['commit_request']) != {'endpoint': prefix+'commits/'+original['commit']}:
            raise AcquisitionError('cached_commit_request_identity_mismatch')
        if _json(records['tree_request']) != {'endpoint': prefix+'trees/'+request['tree_sha1']+'?recursive=1'}:
            raise AcquisitionError('cached_tree_request_identity_mismatch')
        commit, commit_bytes = _response(records['commit_response'])
        tree, tree_bytes = _response(records['tree_response'])
        if (not isinstance(commit, dict) or commit.get('sha') != original['commit']
                or not isinstance(commit.get('tree'), dict)
                or commit['tree'].get('sha') != request['tree_sha1']):
            raise AcquisitionError('cached_commit_identity_mismatch')
        if (not isinstance(tree, dict) or tree.get('sha') != request['tree_sha1']
                or tree.get('truncated') is not False):
            raise AcquisitionError('incomplete_cached_source_tree')
        if receipt['received_bytes'] < commit_bytes + tree_bytes:
            raise AcquisitionError('cached_tree_transfer_accounting_mismatch')
        entries = classify_entries(tree.get('tree'))
        return dict(version=1, state='inspected', request_sha256=sha256(canonical(request)),
                    acquisition_id=original['id'], repository=original['repository'],
                    commit=original['commit'], receipt_sha256=request['receipt_sha256'],
                    tree_sha1=request['tree_sha1'], scope='reference_only', B_input=False,
                    execution_authorized=False, physical_evaluation_performed=False,
                    scientific_validation=False, entries=entries,
                    summary=inventory_summary(entries, hashes))
    except (OSError, UnicodeError) as exc:
        raise AcquisitionError('unsafe_or_unavailable_cached_tree_records') from exc
    finally:
        if folder is not None:
            os.close(folder)
        if root is not None:
            os.close(root)
