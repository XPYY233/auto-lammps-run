"""Fixed HPC helper for reference-only, pinned author-source text reads.

No author program is imported or executed. This module deliberately uses only
the standard library so that the trusted helper can be shipped independently.
"""
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys


VERSION = 1
MAX_FILES = 8
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 500_000
MAX_LINES = 400
MAX_TEXT_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 32 * 1024
_HASH = re.compile(r'[a-f0-9]{64}')
_PIN = re.compile(r'[a-f0-9]{40}')
_ID = re.compile(r'[a-f0-9]{32}')
_REPO = re.compile(r'[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}')


class AuthorSourceError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _relative_path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 500
            or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-' for c in value)
            or value.startswith('/') or any(p in ('', '.', '..', '.git') for p in value.split('/'))):
        raise AuthorSourceError('source_path_not_allowed')
    return value


def _absolute_root(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or not value.startswith('/') or value == '/'
            or str(PurePosixPath(value)) != value
            or any(p in ('', '.', '..') for p in value.split('/')[1:])
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise AuthorSourceError('trusted_HPC_root_required')
    return value


def validate_request(request):
    fields = {'version', 'purpose', 'reference_task_id', 'repository', 'commit',
              'remote_root', 'acquisition_id', 'receipt_sha256', 'inventory_sha256',
              'license_approval_sha256', 'selections'}
    if (not isinstance(request, dict) or set(request) != fields
            or type(request['version']) is not int or request['version'] != VERSION
            or request['purpose'] != 'author_reference'
            or not isinstance(request['reference_task_id'], str)
            or not _ID.fullmatch(request['reference_task_id'])
            or not isinstance(request['acquisition_id'], str) or not _ID.fullmatch(request['acquisition_id'])
            or not isinstance(request['repository'], str) or not _REPO.fullmatch(request['repository'])
            or any(part in {'.', '..'} for part in request['repository'].split('/'))
            or not isinstance(request['commit'], str) or not _PIN.fullmatch(request['commit'])):
        raise AuthorSourceError('reference_source_binding_required')
    _absolute_root(request['remote_root'])
    for field in ('receipt_sha256', 'inventory_sha256', 'license_approval_sha256'):
        if not isinstance(request[field], str) or not _HASH.fullmatch(request[field]):
            raise AuthorSourceError('reference_source_digest_required')
    selections = request['selections']
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
        raise AuthorSourceError('bounded_explicit_source_selection_required')
    seen = set()
    for selected in selections:
        if (not isinstance(selected, dict) or set(selected) != {'path', 'size', 'sha256', 'start_line', 'end_line'}
                or type(selected['size']) is not int or not 0 <= selected['size'] <= MAX_FILE_BYTES
                or not isinstance(selected['sha256'], str) or not _HASH.fullmatch(selected['sha256'])
                or type(selected['start_line']) is not int or type(selected['end_line']) is not int
                or not 1 <= selected['start_line'] <= selected['end_line'] <= 1_000_000
                or selected['end_line'] - selected['start_line'] + 1 > MAX_LINES):
            raise AuthorSourceError('invalid_source_text_selection')
        path = _relative_path(selected['path'])
        if path in seen:
            raise AuthorSourceError('duplicate_source_text_selection')
        seen.add(path)
    if len(canonical(request)) > MAX_REQUEST_BYTES:
        raise AuthorSourceError('source_read_request_limit')
    return request


def validate_receipt(receipt, request):
    """The acquisition service, not repository text, supplies the trusted pin."""
    validate_request(request)
    raw = canonical(receipt)
    if (not isinstance(receipt, dict) or len(raw) > MAX_RECEIPT_BYTES
            or digest(raw) != request['receipt_sha256']
            or receipt.get('schema_version') != 1 or receipt.get('id') != request['acquisition_id']
            or receipt.get('repository') != request['repository'] or receipt.get('commit') != request['commit']
            or receipt.get('location') != 'hpc' or receipt.get('state') not in {'finished', 'partial'}
            or receipt.get('source_complete') is not True
            or receipt.get('remote_directory') != request['remote_root'] + '/' + request['acquisition_id']
            or receipt.get('inventory_sha256') != request['inventory_sha256']
            or receipt.get('execution_authorized') is not False
            or receipt.get('scientific_validation') is not False):
        raise AuthorSourceError('pinned_acquisition_receipt_mismatch')
    inventory = receipt.get('files')
    if (not isinstance(inventory, list) or not 1 <= len(inventory) <= 128
            or digest(canonical(inventory)) != request['inventory_sha256']):
        raise AuthorSourceError('pinned_source_inventory_mismatch')
    files = {}
    for item in inventory:
        if (not isinstance(item, dict) or not isinstance(item.get('sha256'), str)
                or not _HASH.fullmatch(item['sha256']) or type(item.get('size')) is not int
                or not 0 <= item['size'] <= MAX_FILE_BYTES or item.get('git_mode') not in {'100644', '100755'}):
            raise AuthorSourceError('invalid_pinned_source_inventory')
        path = _relative_path(item.get('path'))
        if path in files:
            raise AuthorSourceError('duplicate_pinned_source_path')
        files[path] = item
    for selection in request['selections']:
        entry = files.get(selection['path'])
        if not entry or any(selection[key] != entry[key] for key in ('size', 'sha256')):
            raise AuthorSourceError('source_selection_outside_pinned_inventory')
    return files


def _directory(name, parent=None, *, private=False, traverse_only=False):
    access = getattr(os, 'O_PATH', os.O_RDONLY) if traverse_only else os.O_RDONLY
    descriptor = os.open(name, access | os.O_DIRECTORY | os.O_NOFOLLOW,
                         **({'dir_fd': parent} if parent is not None else {}))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (private and
                (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)):
            raise AuthorSourceError('private_owned_source_directory_required')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _root_descriptor(path):
    current = _directory('/', traverse_only=True)
    try:
        parts = PurePosixPath(path).parts[1:]
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            next_fd = _directory(part, current, private=last, traverse_only=not last)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _read_regular(parent, name, maximum, expected_size=None):
    # Opening relative to already pinned descriptors prevents a path being
    # swapped to an out-of-tree directory between validation and use.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > maximum or (expected_size is not None and before.st_size != expected_size)):
            raise AuthorSourceError('source_file_type_owner_or_size_mismatch')
        parts = []
        count = 0
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - count))
            if not block:
                break
            parts.append(block)
            count += len(block)
            if count > maximum:
                raise AuthorSourceError('source_file_size_limit')
        after = os.fstat(descriptor)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode')
        if any(getattr(before, field) != getattr(after, field) for field in fields) or count != before.st_size:
            raise AuthorSourceError('source_file_changed_during_read')
        return b''.join(parts)
    finally:
        os.close(descriptor)


def response_identity(request, state):
    return dict(version=VERSION, state=state, scope='reference_only', B_input=False,
                execution_authorized=False, physical_evaluation_performed=False,
                request_sha256=digest(canonical(request)), receipt_sha256=request['receipt_sha256'],
                inventory_sha256=request['inventory_sha256'], repository=request['repository'],
                commit=request['commit'], files=[])


def read_pinned_text(request):
    """Read verified text only in an HPC SSH process, never execute the source."""
    validate_request(request)
    if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
        raise AuthorSourceError('source_read_requires_HPC_SSH')
    root = _root_descriptor(request['remote_root'])
    folder = source = None
    try:
        folder = _directory(request['acquisition_id'], root, private=True)
        raw_receipt = _read_regular(folder, 'result.json', MAX_RECEIPT_BYTES)
        if digest(raw_receipt) != request['receipt_sha256']:
            raise AuthorSourceError('saved_acquisition_receipt_SHA256_mismatch')
        try:
            receipt = json.loads(raw_receipt)
        except (ValueError, UnicodeError) as exc:
            raise AuthorSourceError('invalid_saved_acquisition_receipt') from exc
        validate_receipt(receipt, request)
        source = _directory('source', folder, private=True)
        results = []
        total = 0
        for selection in request['selections']:
            parent = os.dup(source)
            try:
                components = selection['path'].split('/')
                for component in components[:-1]:
                    next_fd = _directory(component, parent, private=True)
                    os.close(parent)
                    parent = next_fd
                data = _read_regular(parent, components[-1], MAX_FILE_BYTES, selection['size'])
            finally:
                os.close(parent)
            if digest(data) != selection['sha256']:
                raise AuthorSourceError('source_file_SHA256_mismatch')
            try:
                text = data.decode('utf-8', errors='strict')
            except UnicodeError as exc:
                raise AuthorSourceError('source_file_not_UTF8_text') from exc
            if '\0' in text:
                raise AuthorSourceError('source_file_contains_NUL')
            lines = text.splitlines(keepends=True)
            if selection['start_line'] > max(1, len(lines)):
                raise AuthorSourceError('source_line_range_outside_file')
            returned = ''.join(lines[selection['start_line'] - 1:selection['end_line']])
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
        if folder is not None:
            os.close(folder)
        os.close(root)


def read_source_report(request):
    """Return fixed, actionable rejection codes without private tracebacks."""
    validate_request(request)
    try:
        return read_pinned_text(request)
    except (AuthorSourceError, OSError) as exc:
        report = response_identity(request, 'rejected')
        # AuthorSourceError messages are fixed helper codes; never interpolate
        # author text, a remote path or an OS exception's filename into them.
        report['rejection_reason'] = str(exc) if isinstance(exc, AuthorSourceError) else 'pinned_source_path_unavailable'
        return report


def main():
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise AuthorSourceError('source_read_request_limit')
    print(json.dumps(read_source_report(json.loads(raw)), ensure_ascii=True))


if __name__ == '__main__':
    main()
