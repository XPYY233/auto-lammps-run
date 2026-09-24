"""Private, content-addressed input snapshots. No simulation or script execution."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile

from .ledger import Resources

ROLES = {'lammps_input', 'structure', 'potential', 'analysis_spec'}


class ManifestError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_provenance(provenance):
    keys = {'task_sha256', 'analysis_sha256', 'software_sha256'}
    if (not isinstance(provenance, dict) or set(provenance) != keys
            or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                   for value in provenance.values())):
        raise ManifestError('Frozen task, analysis and software hashes are required')


def relative_name(name):
    if (not isinstance(name, str) or len(name) > 240 or not name
            or len(name.split('/')) > 8
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', part) for part in name.split('/'))
            or str(PurePosixPath(name)) != name):
        raise ManifestError('Unsafe input path')
    if name.split('/')[0] in {'manifest.json', 'job.sh', 'receipt.json', 'allocation.json', 'stage.json', 'output',
                              'execution-intent.json', 'execution-result.json', 'scheduler.stdout', 'scheduler.stderr',
                              'scheduler-intent.json', 'scheduler-result.json'}:
        raise ManifestError('Reserved input name')
    return name


def private_directory(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ManifestError('Private directory cannot be a symlink')
    path = path.resolve()
    if any((p / '.git').exists() for p in [path, *path.parents]):
        raise ManifestError('Private storage must be outside Git working trees')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise ManifestError('Private directory must have owner-only permissions')
    return path


@contextmanager
def root_descriptor(root):
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ManifestError('Root directory cannot be safely opened') from exc
    try:
        yield fd
    finally:
        os.close(fd)


def read_file(root_fd, name, max_bytes):
    """Walk every component using no-follow descriptors, then read a regular file."""
    parts = name.split('/')
    directory = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                raise ManifestError('Input is not a bounded, unlinked regular file')
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                data = handle.read(max_bytes + 1)
            after = os.fstat(fd)
            if (len(data) > max_bytes or len(data) != before.st_size
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                       (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ManifestError('Input changed during snapshot')
            return data
        finally:
            os.close(fd)
    except OSError as exc:
        raise ManifestError('Input path cannot be safely opened') from exc
    finally:
        os.close(directory)


@dataclass(frozen=True)
class Snapshot:
    path: Path
    digest: str

    def verify(self):
        if not re.fullmatch(r'[a-f0-9]{64}', self.digest):
            raise ManifestError('Invalid snapshot digest')
        with root_descriptor(self.path) as root:
            encoded = read_file(root, 'manifest.json', 1_000_000)
            if sha256(encoded) != self.digest:
                raise ManifestError('Manifest hash mismatch')
            try:
                document = json.loads(encoded)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ManifestError('Invalid manifest JSON') from exc
            if (not isinstance(document, dict) or document.get('schema_version') != 1
                    or not isinstance(document.get('files'), list)
                    or not 1 <= len(document['files']) <= 128):
                raise ManifestError('Unsupported manifest')
            validate_provenance(document.get('provenance'))
            try:
                resources = Resources(**document['resources'])
            except (KeyError, TypeError, ValueError) as exc:
                raise ManifestError('Invalid manifest resources') from exc
            expected, total = {'manifest.json'}, len(encoded)
            if total > resources.storage_bytes:
                raise ManifestError('Manifest exceeds storage reservation')
            for record in document['files']:
                if not isinstance(record, dict) or set(record) != {'path', 'role', 'size', 'sha256'}:
                    raise ManifestError('Invalid file record')
                name = relative_name(record['path'])
                if (name in expected or not isinstance(record['role'], str) or record['role'] not in ROLES
                        or type(record['size']) is not int or record['size'] < 0):
                    raise ManifestError('Invalid file manifest')
                expected.add(name)
                if record['size'] > resources.storage_bytes - total:
                    raise ManifestError('Inputs exceed storage reservation')
                data = read_file(root, name, record['size'])
                if len(data) != record['size'] or sha256(data) != record['sha256']:
                    raise ManifestError('Input hash mismatch')
                total += len(data)
        observed = set()
        for directory, dirs, files in os.walk(self.path, followlinks=False):
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise ManifestError('Unexpected symlink directory')
            for name in files:
                observed.add((Path(directory) / name).relative_to(self.path).as_posix())
        if observed != expected:
            raise ManifestError('Snapshot contains missing or undeclared files')
        entry = document.get('entrypoint')
        scripts = [x['path'] for x in document['files'] if x['role'] == 'lammps_input']
        if scripts != [entry]:
            raise ManifestError('Exactly one declared LAMMPS input is required')
        return document


def freeze(source, store, *, files: dict, entrypoint: str, resources: Resources, provenance: dict):
    """Freeze named inputs and provenance; suggestions and scientific checks are separate.

    Files are never overwritten. Integrity is rechecked before later staging.
    Ownership permissions do not substitute for separate Agent/service identities.
    """
    entrypoint = relative_name(entrypoint)
    if not files or len(files) > 128 or files.get(entrypoint) != 'lammps_input':
        raise ManifestError('Missing entrypoint or excessive input count')
    if sum(role == 'lammps_input' for role in files.values()) != 1:
        raise ManifestError('Exactly one LAMMPS script is supported')
    for name, role in files.items():
        relative_name(name)
        if role not in ROLES:
            raise ManifestError('Unsupported input role')
    validate_provenance(provenance)
    store = private_directory(store)
    staging = Path(tempfile.mkdtemp(prefix='.freeze-', dir=store))
    try:
        records, total = [], 0
        with root_descriptor(source) as root:
            for name, role in sorted(files.items()):
                data = read_file(root, name, resources.storage_bytes - total)
                total += len(data)
                path = staging / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with path.open('xb') as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                path.chmod(0o400)
                records.append(dict(path=name, role=role, size=len(data), sha256=sha256(data)))
        document = dict(schema_version=1, files=records, entrypoint=entrypoint,
                        resources=asdict(resources), provenance=provenance)
        encoded = canonical(document)
        if len(encoded) > 1_000_000:
            raise ManifestError('Manifest exceeds size limit')
        if total + len(encoded) > resources.storage_bytes:
            raise ManifestError('Inputs and manifest exceed storage reservation')
        with (staging / 'manifest.json').open('xb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        (staging / 'manifest.json').chmod(0o400)
        digest = sha256(encoded)
        destination = store / digest
        try:
            lock = os.open(store / '.freeze.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            lock = os.open(store / '.freeze.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise ManifestError('Unsafe snapshot lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not destination.exists():
                os.rename(staging, destination)
            snapshot = Snapshot(destination, digest)
            snapshot.verify()
            with root_descriptor(store) as root:
                os.fsync(root)
            return snapshot
        finally:
            os.close(lock)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
