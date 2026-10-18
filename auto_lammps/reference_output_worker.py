"""Standalone, read-only helper for pinned reference-output bytes.

The trusted caller binds the request to a terminal, accounted author-reference
job. This helper neither establishes scheduler state nor executes author files.
Only the standard library is needed when its source is sent to an HPC process.
"""
import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys


VERSION = 1
MAX_FILES = 32
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
_ID = re.compile(r'[a-f0-9]{32}')
_HASH = re.compile(r'[a-f0-9]{64}')
_JOB = re.compile(r'[0-9]{1,32}')
# Relative paths are passed to dir-fd operations, never interpolated into a
# shell. Retain spaces and '=' used by original LAMMPS output directories.
_PART = re.compile(r'[A-Za-z0-9_.= \-]{1,255}')
_NAME = re.compile(r'[A-Za-z0-9_.-]{1,160}')


class ReferenceOutputError(ValueError):
    """A rejection whose message is always a fixed, non-private code."""


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('ascii')


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _root(value):
    if (type(value) is not str or not 1 <= len(value) <= 2048
            or not value.startswith('/') or str(PurePosixPath(value)) != value
            or any(part in ('', '.', '..') for part in value.split('/')[1:])
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ReferenceOutputError('reference_output_root_invalid')
    return value


def _path(value):
    if (type(value) is not str or not 1 <= len(value) <= 1024
            or any(part in ('.', '..') or not _matches(_PART, part)
                   for part in value.split('/'))):
        raise ReferenceOutputError('reference_output_path_invalid')
    return value


def validate_request(request):
    """Validate a bounded v1 selection; sizes and digests are trusted pins."""
    if (type(request) is not dict
            or set(request) != {'version', 'request_id', 'job_id', 'manifest_sha256', 'root', 'files'}
            or type(request['version']) is not int or request['version'] != VERSION
            or not _matches(_ID, request['request_id'])
            or not _matches(_JOB, request['job_id'])
            or not _matches(_HASH, request['manifest_sha256'])):
        raise ReferenceOutputError('reference_output_request_invalid')
    _root(request['root'])
    files = request['files']
    if type(files) is not list or not 1 <= len(files) <= MAX_FILES:
        raise ReferenceOutputError('reference_output_selection_limit')
    paths, names, total = set(), set(), 0
    for item in files:
        if (type(item) is not dict or set(item) != {'path', 'name', 'size', 'sha256'}
                or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE_BYTES
                or not _matches(_HASH, item['sha256'])
                or not _matches(_NAME, item['name']) or item['name'] in ('.', '..')):
            raise ReferenceOutputError('reference_output_file_invalid')
        path = _path(item['path'])
        if path in paths or item['name'] in names:
            raise ReferenceOutputError('reference_output_duplicate_selection')
        paths.add(path)
        names.add(item['name'])
        total += item['size']
    if total > MAX_TOTAL_BYTES or len(canonical(request)) > MAX_REQUEST_BYTES:
        raise ReferenceOutputError('reference_output_request_limit')
    return request



def response_limit(request):
    """Bound actual SSH stdout plus stderr for the pinned byte selection.

    Each file is base64 inside one canonical JSON response. The local capture
    utility encodes its return value again only after enforcing this bound.
    """
    validate_request(request)
    empty = {key: request[key] for key in ('version','request_id','job_id','manifest_sha256')}
    empty['files'] = [dict(item,data='') for item in request['files']]
    encoded = sum(4*((item['size']+2)//3) for item in request['files'])
    return len(canonical(empty))+encoded+65536


def _directory(name, parent=None, *, owned=False):
    # Linux O_PATH permits traversal of shared execute-only HPC ancestors.
    access = getattr(os, 'O_PATH', os.O_RDONLY)
    descriptor = os.open(name, access | os.O_DIRECTORY | os.O_NOFOLLOW,
                         **({'dir_fd': parent} if parent is not None else {}))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (owned and info.st_uid != os.getuid()):
            raise ReferenceOutputError('reference_output_directory_invalid')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _root_descriptor(path):
    descriptor = _directory('/')
    try:
        parts = PurePosixPath(path).parts[1:]
        for index, part in enumerate(parts):
            child = _directory(part, descriptor, owned=index == len(parts) - 1)
            os.close(descriptor)
            descriptor = child
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ReferenceOutputError('reference_output_directory_invalid')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stamp(info):
    return tuple(getattr(info, field) for field in (
        'st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
        'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _read_pass(descriptor, size, *, keep):
    checksum = hashlib.sha256()
    blocks, count = [], 0
    while True:
        block = os.read(descriptor, min(65536, size + 1 - count))
        if not block:
            break
        count += len(block)
        if count > size:
            raise ReferenceOutputError('reference_output_size_mismatch')
        checksum.update(block)
        if keep:
            blocks.append(block)
    if count != size:
        raise ReferenceOutputError('reference_output_size_mismatch')
    return b''.join(blocks), checksum.hexdigest()


def _read_file(parent, item):
    descriptor = os.open(item['path'].split('/')[-1],
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_size != item['size']):
            raise ReferenceOutputError('reference_output_file_type_owner_or_size_mismatch')
        data, checksum = _read_pass(descriptor, item['size'], keep=True)
        if _stamp(before) != _stamp(os.fstat(descriptor)):
            raise ReferenceOutputError('reference_output_changed_during_read')
        if checksum != item['sha256']:
            raise ReferenceOutputError('reference_output_sha256_mismatch')
        # Re-read the same inode to check its digest again before returning bytes.
        os.lseek(descriptor, 0, os.SEEK_SET)
        _, final_checksum = _read_pass(descriptor, item['size'], keep=False)
        if (_stamp(before) != _stamp(os.fstat(descriptor))
                or final_checksum != checksum):
            raise ReferenceOutputError('reference_output_changed_during_read')
        return data
    finally:
        os.close(descriptor)


def read_outputs(request):
    """Return pinned bytes only; never import, execute, or alter selected files."""
    # Snapshot primitive validated metadata so concurrent caller edits cannot
    # change the selection after validation.
    validate_request(request)
    try:
        request = json.loads(canonical(request))
    except (TypeError, ValueError, RuntimeError):
        raise ReferenceOutputError('reference_output_request_invalid') from None
    validate_request(request)
    result = {key: request[key] for key in ('version', 'request_id', 'job_id', 'manifest_sha256')}
    result['files'] = []
    try:
        root = _root_descriptor(request['root'])
        try:
            for item in request['files']:
                parent = os.dup(root)
                try:
                    for part in item['path'].split('/')[:-1]:
                        child = _directory(part, parent, owned=True)
                        os.close(parent)
                        parent = child
                    data = _read_file(parent, item)
                finally:
                    os.close(parent)
                result['files'].append(dict(item, data=base64.b64encode(data).decode('ascii')))
        finally:
            os.close(root)
    except OSError:
        raise ReferenceOutputError('reference_output_unavailable') from None
    return result


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReferenceOutputError('reference_output_request_invalid')
        value[key] = item
    return value


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ReferenceOutputError('reference_output_request_limit')
        try:
            request = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ReferenceOutputError('reference_output_request_invalid') from None
        response = base64.b64encode(canonical(read_outputs(request)))
        sys.stdout.buffer.write(response + b'\n')
        return 0
    except ReferenceOutputError as exc:
        sys.stderr.write(canonical({'error_type': 'ReferenceOutputError', 'message': str(exc)}).decode('ascii') + '\n')
    except Exception:
        sys.stderr.write('{"error_type":"ReferenceOutputError","message":"reference_output_failed"}\n')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
