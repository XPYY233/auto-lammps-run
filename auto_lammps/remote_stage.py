"""Standalone trusted staging helper; no engine, shell, archive or scheduler execution.

Install this exact reviewed file outside the writable request root. The SSH client
pins its SHA-256. Root policy and filesystem ownership are administrator managed.
Wire format: 4-byte network-order manifest length, manifest JSON, listed file bytes.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import signal
import stat
import struct
import sys

HEADER_LIMIT = 1_000_000
RECEIPT_ALLOWANCE = 8192
ROOT_ALLOWANCE = 65536
ROLES = {'lammps_input', 'structure', 'potential', 'analysis_spec'}
RESERVED = {'manifest.json', 'allocation.json', 'stage.json', 'receipt.json', 'job.sh', 'output'}


class StageError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise StageError('Invalid content digest')
    return value


def safe_name(value):
    if (not isinstance(value, str) or not value or len(value) > 240
            or len(value.split('/')) > 8
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', x) for x in value.split('/'))
            or value.split('/')[0] in RESERVED or str(PurePosixPath(value)) != value):
        raise StageError('Unsafe input path')
    return value


def read_exact(stream, size):
    parts, remaining = [], size
    while remaining:
        block = stream.read(min(remaining, 65536))
        if not block:
            raise StageError('Truncated upload')
        parts.append(block)
        remaining -= len(block)
    return b''.join(parts)


def regular_read(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise StageError('Invalid control file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise StageError('Oversized control file')
        return data
    finally:
        os.close(fd)


def write_new(directory, name, data):
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)


@contextmanager
def directory_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def approved_root(path):
    if not isinstance(path, str) or not path.startswith('/') or str(PurePosixPath(path)) != path:
        raise StageError('An absolute canonical staging root is required')
    parts = path.split('/')[1:]
    if not parts or any(part in {'', '.', '..'} for part in parts):
        raise StageError('Unsafe root')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            try:
                os.stat('.git', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StageError('Staging root must be outside Git working trees')
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StageError('Staging root must be owned by service identity and private')
        yield fd
    finally:
        os.close(fd)


def validate_manifest(data, expected):
    if digest(data) != hash_value(expected):
        raise StageError('Manifest digest mismatch')
    try:
        value = json.loads(data)
        if (set(value) != {'schema_version', 'files', 'entrypoint', 'resources', 'provenance'}
                or value['schema_version'] != 1):
            raise StageError('Unsupported manifest')
        resources = value['resources']
        if set(resources) != {'cores', 'wall_seconds', 'memory_bytes', 'storage_bytes'}:
            raise StageError('Invalid resources')
        if any(type(x) is not int or x <= 0 for x in resources.values()):
            raise StageError('Invalid resource bounds')
        if set(value['provenance']) != {'task_sha256', 'analysis_sha256', 'software_sha256'}:
            raise StageError('Missing provenance')
        for item in value['provenance'].values():
            hash_value(item)
        files = value['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 128:
            raise StageError('Invalid file count')
        names, entries = set(), []
        total = len(data) + RECEIPT_ALLOWANCE
        for item in files:
            if set(item) != {'path', 'role', 'size', 'sha256'}:
                raise StageError('Invalid file record')
            name = safe_name(item['path'])
            if name in names or any(name.startswith(old + '/') or old.startswith(name + '/') for old in names):
                raise StageError('Duplicate or overlapping path')
            names.add(name)
            if not isinstance(item['role'], str) or item['role'] not in ROLES:
                raise StageError('Invalid input role')
            if type(item['size']) is not int or item['size'] < 0:
                raise StageError('Invalid file size')
            total += item['size']
            hash_value(item['sha256'])
            if item['role'] == 'lammps_input':
                entries.append(name)
        if entries != [value['entrypoint']] or total > resources['storage_bytes']:
            raise StageError('Invalid entrypoint or insufficient storage including receipts')
        return value
    except (TypeError, KeyError, AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageError('Malformed manifest') from exc


def receive(root_path, request_id, manifest_sha256, stream):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise StageError('Invalid request identity')
    size = struct.unpack('!I', read_exact(stream, 4))[0]
    if not 0 < size <= HEADER_LIMIT:
        raise StageError('Manifest size exceeds limit')
    manifest_bytes = read_exact(stream, size)
    manifest = validate_manifest(manifest_bytes, manifest_sha256)
    allocation = manifest['resources']['storage_bytes']
    with approved_root(root_path) as root:
        # Exclusive creation avoids racing O_CREAT/O_NOFOLLOW path resolution on
        # some filesystems. Existing locks are opened without creation semantics.
        try:
            lock = os.open('.stage.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root)
            os.fsync(root)
        except FileExistsError:
            lock = os.open('.stage.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise StageError('Unsafe staging lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            policy_bytes = regular_read(root, 'policy.json', 4096)
            policy = json.loads(policy_bytes)
            if (set(policy) != {'schema_version', 'max_total_bytes', 'approval_sha256'} or policy['schema_version'] != 1
                    or type(policy['max_total_bytes']) is not int or policy['max_total_bytes'] <= ROOT_ALLOWANCE):
                raise StageError('Invalid private storage policy')
            hash_value(policy['approval_sha256'])
            used = ROOT_ALLOWANCE
            for name in os.listdir(root):
                if name in {'.stage.lock', 'policy.json'}:
                    continue
                if not re.fullmatch(r'[a-f0-9]{32}', name):
                    raise StageError('Unexpected object in service root')
                if name == request_id:
                    raise StageError('Request directory already exists; inspect rather than overwrite')
                with directory_at(root, name) as old:
                    saved = json.loads(regular_read(old, 'allocation.json', 4096))
                    charge = saved['storage_bytes']
                    if type(charge) is not int or charge <= 0 or saved['request_id'] != name:
                        raise StageError('Invalid previous allocation; reconciliation required')
                    used += charge
            if used + allocation > policy['max_total_bytes']:
                raise StageError('Remote aggregate storage reservation exceeded')
            os.mkdir(request_id, mode=0o700, dir_fd=root)
            os.fsync(root)
            with directory_at(root, request_id) as target:
                # A crash before allocation is durable leaves a directory that
                # fails closed during subsequent aggregate accounting.
                write_new(target, 'allocation.json', encoded(dict(request_id=request_id,
                    storage_bytes=allocation, manifest_sha256=manifest_sha256)))
                write_new(target, 'manifest.json', manifest_bytes)
                for item in manifest['files']:
                    folder = os.dup(target)
                    try:
                        parts = item['path'].split('/')
                        for part in parts[:-1]:
                            try:
                                os.mkdir(part, mode=0o700, dir_fd=folder)
                                os.fsync(folder)
                            except FileExistsError:
                                pass
                            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                            os.close(folder)
                            folder = child
                        fd = os.open(parts[-1], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                     0o400, dir_fd=folder)
                        try:
                            remaining, checksum = item['size'], hashlib.sha256()
                            with os.fdopen(fd, 'wb', closefd=False) as output:
                                while remaining:
                                    block = read_exact(stream, min(remaining, 65536))
                                    output.write(block)
                                    checksum.update(block)
                                    remaining -= len(block)
                                output.flush()
                                os.fsync(fd)
                            if checksum.hexdigest() != item['sha256']:
                                raise StageError('Uploaded file digest mismatch')
                        finally:
                            os.close(fd)
                        os.fsync(folder)
                    finally:
                        os.close(folder)
                if stream.read(1):
                    raise StageError('Trailing undeclared upload content')
                receipt = dict(schema_version=1, state='staged', request_id=request_id,
                               manifest_sha256=manifest_sha256, input_bytes=sum(x['size'] for x in manifest['files']),
                               storage_bytes=allocation, policy_sha256=digest(policy_bytes))
                write_new(target, 'stage.json', encoded(receipt))
                return receipt
        finally:
            os.close(lock)


def main():
    parser = argparse.ArgumentParser(description='Receive declared regular files only; never execute them.')
    parser.add_argument('--root', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    # A stalled client cannot hold the service lock indefinitely. Termination
    # leaves partial files/reservations for inspection; never implies rejection.
    signal.alarm(60)
    try:
        result = receive(args.root, args.request_id, args.manifest_sha256, sys.stdin.buffer)
    except (StageError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'state': 'stage_failed', 'error_type': type(exc).__name__}))
        return 1
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
