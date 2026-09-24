"""Standalone Linux compute-node launcher. No setup, submission or approval issuer.

The administrator installs this reviewed file with runtime.json next to it.
A private controller signs request grants; neither key nor grants are mounted
inside the sandbox. Tests use synthetic data and never invoke a physics engine.
"""
import argparse
from contextlib import contextmanager, ExitStack
import ctypes
import errno
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import resource
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time


class ExecutionDenied(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ExecutionDenied('Invalid content identity')
    return value


def relative(value):
    if (not isinstance(value, str) or len(value) > 240 or not value or len(value.split('/')) > 8
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', x) for x in value.split('/'))):
        raise ExecutionDenied('Unsafe relative path')
    return value


def absolute(value):
    if (not isinstance(value, str) or not value.startswith('/') or len(value) > 1024
            or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts):
        raise ExecutionDenied('Unsafe absolute deployment path')
    return Path(value)


def directory(path, *, private=False):
    """Reject symlinks in every component, not merely the final name."""
    path = absolute(str(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            # Shared HPC ancestors may allow traversal but prohibit listing.
            # Keep the final descriptor readable for fsync and file operations.
            access = os.O_RDONLY if index == len(parts)-1 else getattr(os, 'O_PATH', os.O_RDONLY)
            child = os.open(part, access | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ExecutionDenied('Private directory ownership/permissions mismatch')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(path, limit, *, private=False):
    path = absolute(str(path))
    parent = directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit
                    or (private and (before.st_uid != os.getuid() or before.st_mode & 0o077))):
                raise ExecutionDenied('Unsafe or oversized file')
            with os.fdopen(fd, 'rb', closefd=False) as source:
                data = source.read(limit + 1)
            after = os.fstat(fd)
            if (len(data) != before.st_size or len(data) > limit
                    or (before.st_mtime_ns,before.st_ctime_ns) != (after.st_mtime_ns,after.st_ctime_ns)):
                raise ExecutionDenied('File changed while reading')
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def write_once(path, value):
    data = canonical(value)
    if len(data) > 16384:
        raise ExecutionDenied('Receipt is too large')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
    try:
        with os.fdopen(fd,'wb',closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    parent = directory(Path(path).parent)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def verify_grant(envelope, key, *, request_id, manifest_sha256, profile_sha256, now):
    if len(key) != 32 or set(envelope) != {'payload','hmac_sha256'}:
        raise ExecutionDenied('Invalid private grant')
    payload = envelope['payload']
    signature = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, hash_value(envelope['hmac_sha256'])):
        raise ExecutionDenied('Grant signature mismatch')
    if (payload.get('request_id') != request_id or payload.get('manifest_sha256') != manifest_sha256
            or payload.get('profile_sha256') != profile_sha256):
        raise ExecutionDenied('Grant belongs to another request, input or runtime')
    if type(payload.get('expires_at')) is not int or payload['expires_at'] <= now:
        raise ExecutionDenied('Execution grant expired')
    for name in ('approval_sha256','task_sha256','scoring_sha256','static_check_sha256'):
        hash_value(payload.get(name))
    if not isinstance(payload.get('reviewed_commit'),str) or not re.fullmatch(r'[a-f0-9]{40}',payload['reviewed_commit']):
        raise ExecutionDenied('A reviewed source commit is required')
    return payload


def duration(value):
    match = re.fullmatch(r'(?:(\d+)-)?(\d+):(\d{2}):(\d{2})',value)
    if not match or int(match[3]) >= 60 or int(match[4]) >= 60:
        raise ExecutionDenied('Unsupported scheduler duration')
    return (int(match[1] or 0)*24 + int(match[2]))*3600 + int(match[3])*60 + int(match[4])


def parse_allocation(text, *, request_id, manifest_sha256, job_id, uid, host, resources):
    fields = {}
    for part in text.strip().split():
        if '=' not in part:
            raise ExecutionDenied('Ambiguous scheduler record')
        key,value = part.split('=',1)
        if key in fields:
            raise ExecutionDenied('Duplicate scheduler field')
        fields[key]=value
    expected = {'JobId':job_id,'JobName':'al-'+request_id,'Comment':f'al:{manifest_sha256}:{request_id}',
                'JobState':'RUNNING','NumNodes':'1','NumCPUs':str(resources['cores']),
                'NodeList':host,'BatchHost':host,'Restarts':'0'}
    if any(fields.get(key) != value for key,value in expected.items()):
        raise ExecutionDenied('Scheduler allocation does not match the authorized request')
    owner = re.fullmatch(r'[^\s()]+\(([0-9]+)\)',fields.get('UserId',''))
    if owner is None or int(owner[1]) != uid:
        raise ExecutionDenied('Wrong allocation owner')
    if duration(fields.get('TimeLimit','')) != resources['wall_seconds']:
        raise ExecutionDenied('Scheduler time differs from reserved budget')
    elapsed = duration(fields.get('RunTime',''))
    if elapsed >= resources['wall_seconds']:
        raise ExecutionDenied('Allocation has no remaining run time')
    return elapsed


def cgroup_memory_limit(proc_cgroup='/proc/self/cgroup', mount='/sys/fs/cgroup'):
    """Read kernel hard limits, including v1's actual hierarchy semantics.

    Supported host layouts are a unified root or a memory controller at
    root/memory. No usage, soft-limit or environment value is enforcement.
    """
    lines = Path(proc_cgroup).read_text().splitlines()
    unified, legacy = [], []
    for line in lines:
        parts = line.split(':', 2)
        if len(parts) != 3 or not parts[0].isdecimal():
            raise ExecutionDenied('Invalid cgroup membership')
        if parts[:2] == ['0', '']:
            unified.append(parts[2])
        elif 'memory' in parts[1].split(','):
            if parts[0] == '0':
                raise ExecutionDenied('Invalid legacy memory controller')
            legacy.append(parts[2])
    if len(unified) > 1 or len(legacy) > 1 or not (unified or legacy):
        raise ExecutionDenied('A unique memory cgroup is required')
    # In a hybrid hierarchy, a controller attached to v1 is not active in v2.
    membership = (legacy or unified)[0]
    if (not membership.startswith('/') or membership.startswith('//') or '\x00' in membership
            or '..' in PurePosixPath(membership).parts):
        raise ExecutionDenied('Invalid cgroup membership path')
    mount = Path(mount)
    if legacy:
        current = mount / 'memory' / membership.lstrip('/')
        try:
            own = (current/'memory.limit_in_bytes').read_text().strip()
            # Kernel-reported effective limit respects memory.use_hierarchy.
            # Taking every parent's local limit would be incorrect on v1.
            rows = [line.split() for line in (current/'memory.stat').read_text().splitlines()]
            effective = [row for row in rows if row and row[0] == 'hierarchical_memory_limit']
        except OSError as exc:
            raise ExecutionDenied('Cannot read cgroup-v1 hard memory controls') from exc
        if (len(effective) != 1 or len(effective[0]) != 2
                or any(not re.fullmatch(r'[1-9][0-9]*', value) for value in (own, effective[0][1]))):
            raise ExecutionDenied('Invalid cgroup-v1 hard memory ceiling')
        limit = min(int(own), int(effective[0][1]))
        # v1's no-limit value is signed LONG_MAX rounded down to PAGE_SIZE.
        # The launcher supports Linux x86-64 only; the page size is read live.
        unlimited = ((1 << 63) - 1) // os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PAGE_SIZE')
        if limit >= unlimited:
            raise ExecutionDenied('No cgroup memory hard limit')
        return limit
    current = mount / membership.lstrip('/')
    limits = []
    while True:
        # The cgroup-v2 root does not expose memory.max. A non-root
        # allocation must still supply a finite ceiling along its ancestry.
        if current == mount and not (current/'memory.max').exists():
            break
        value = (current/'memory.max').read_text().strip()
        if value != 'max':
            if not re.fullmatch(r'[1-9][0-9]*',value):
                raise ExecutionDenied('Invalid cgroup memory ceiling')
            limits.append(int(value))
        if current == mount:
            break
        current = current.parent
    if not limits:
        raise ExecutionDenied('No cgroup memory hard limit')
    return min(limits)


def validate_outputs(outputs, *, storage_bytes, input_bytes):
    if not isinstance(outputs,list) or not 3 <= len(outputs) <= 32:
        raise ExecutionDenied('Declare a bounded set of output files')
    if len(set(outputs)) != len(outputs) or not {'stdout.txt','stderr.txt','log.lammps'} <= set(outputs):
        raise ExecutionDenied('Output names are incomplete or duplicated')
    for name in outputs:
        relative(name)
        if '/' in name:
            raise ExecutionDenied('Only flat declared outputs are supported')
    # Include two scheduler streams and reserve bounded controller receipts. This
    # is a logical byte bound; deployment still needs physical storage accounting.
    per_file = (storage_bytes - input_bytes - 262144) // (len(outputs) + 2)
    if per_file <= 0:
        raise ExecutionDenied('Insufficient reserved output space')
    return per_file


def volume_allocation(profile, *, storage_bytes, input_bytes):
    """Reserve the entire backing image, including its filesystem metadata.

    Scheduler/controller files remain outside the image. This is not a claim
    that the whole host filesystem or every project copy has a physical quota.
    """
    config = profile.get('output_volume')
    if config is None:
        return None
    fields = {'kind', 'max_image_bytes', 'mkfs_path', 'mkfs_sha256',
              'fuse2fs_path', 'fuse2fs_sha256', 'fusermount_path',
              'fusermount_sha256', 'helper_directory'}
    if (not isinstance(config, dict) or set(config) != fields or config['kind'] != 'ext2-fuse'
            or type(config['max_image_bytes']) is not int
            or not 8*1024*1024 <= config['max_image_bytes'] <= 256*1024**3
            or type(storage_bytes) is not int or type(input_bytes) is not int
            or storage_bytes <= 0 or input_bytes < 0):
        raise ExecutionDenied('Invalid output volume declaration')
    for prefix in ('mkfs', 'fuse2fs', 'fusermount'):
        absolute(config[prefix+'_path'])
        hash_value(config[prefix+'_sha256'])
    absolute(config['helper_directory'])
    # Leave space for two bounded controller/scheduler streams and receipts;
    # deployment must still enforce/account those external allocations.
    available = storage_bytes-input_bytes-262144
    image_bytes = min(config['max_image_bytes'], available-2*65536)
    image_bytes = image_bytes//4096*4096
    if image_bytes < 8*1024*1024:
        raise ExecutionDenied('Insufficient space for a bounded output image')
    return dict(kind='ext2-fuse', image_bytes=image_bytes)


def verify_volume_tools(profile):
    config = profile['output_volume']
    for prefix in ('mkfs', 'fuse2fs', 'fusermount'):
        path = absolute(config[prefix+'_path'])
        requests = absolute(profile['requests_root'])
        if path == requests or requests in path.parents:
            raise ExecutionDenied('Storage tools must be outside submitted files')
        if digest(read_regular(path, 64*1024*1024)) != hash_value(config[prefix+'_sha256']):
            raise ExecutionDenied('Storage tool version mismatch')
    helper = absolute(config['helper_directory'])
    requests = absolute(profile['requests_root'])
    if helper == requests or requests in helper.parents:
        raise ExecutionDenied('Storage helper directory must be outside requests')
    fd = directory(helper, private=True)
    try:
        # libfuse 2 falls back to PATH after its compiled-in fusermount path.
        if os.readlink('fusermount', dir_fd=fd) != config['fusermount_path']:
            raise ExecutionDenied('Unverified FUSE mount helper')
    finally:
        os.close(fd)
    for path in (Path('/bin/fusermount'), Path('/usr/bin/fusermount')):
        if path.exists() and path.resolve() != Path(config['fusermount_path']).resolve():
            raise ExecutionDenied('Unpinned default FUSE mount helper')
    return config


def verify_volume_mount(mount, image, image_bytes, *, readonly):
    """Check the actual mount, not just a successful helper exit status."""
    matches = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if len(fields) < 10 or '-' not in fields:
            continue
        # The volume backend rejects paths needing mountinfo escape decoding.
        if fields[4] != str(mount):
            continue
        separator = fields.index('-')
        matches.append(fields)
        if (fields[separator+1] != 'fuse.ext4' or fields[separator+2] != str(image)
                or ('ro' in fields[5].split(',')) != readonly
                or f'user_id={os.getuid()}' not in fields[separator+3].split(',')):
            raise ExecutionDenied('Output mount identity or mode mismatch')
    if len(matches) != 1:
        raise ExecutionDenied('Missing or ambiguous output mount')
    info = os.statvfs(mount)
    if not 0 < info.f_blocks*info.f_frsize <= image_bytes:
        raise ExecutionDenied('Output filesystem exceeds reserved image capacity')


@contextmanager
def mounted_output_volume(profile, case, allocation, *, readonly=False):
    """A fixed image survives daemon exit; collection remounts it read-only.

    No helper or image is writable or executable by the model. Only declared
    files inside this volume are subsequently bind-mounted into the engine.
    """
    config = verify_volume_tools(profile)
    image, mount = case/'output-volume.ext2', case/'output'
    capacity = allocation['image_bytes']
    if (type(capacity) is not int or not 8*1024*1024 <= capacity <= config['max_image_bytes']
            or capacity % 4096 or re.search(r'[\s\\]', str(case))):
        raise ExecutionDenied('Invalid frozen image capacity')
    if os.path.ismount(mount):
        raise ExecutionDenied('Output directory is already mounted; reconcile first')
    if not readonly:
        mount.mkdir(mode=0o700, exist_ok=False)
        descriptor = os.open(image, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        try:
            os.ftruncate(descriptor, capacity)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    fd = directory(mount, private=True)
    os.close(fd)
    if any(mount.iterdir()):
        raise ExecutionDenied('Output mountpoint must be empty')
    descriptor = os.open(image, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_mode & 0o077
                or before.st_size != capacity):
            raise ExecutionDenied('Invalid bounded output image')
    finally:
        os.close(descriptor)
    environment = {'PATH': config['helper_directory']+':/usr/bin:/bin', 'LC_ALL': 'C'}
    def helper_limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (capacity, capacity))
        resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
    if not readonly:
        result = subprocess.run([config['mkfs_path'], '-t', 'ext2', '-F', '-q', '-b', '4096', '-m', '0', str(image)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False, env=environment, preexec_fn=helper_limits)
        if result.returncode != 0:
            raise ExecutionDenied('Output image formatting failed')
    # fuse2fs consumes "ro" itself; -r also reaches libfuse so the kernel
    # mount is read-only, in addition to the backing-image descriptor.
    process = subprocess.Popen([config['fuse2fs_path'], '-f', *(['-r'] if readonly else []), '-o',
        ('ro' if readonly else 'rw')+',fakeroot', str(image), str(mount)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, env=environment, preexec_fn=helper_limits)
    try:
        deadline = time.monotonic()+5
        while not os.path.ismount(mount) and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        verify_volume_mount(mount, image, capacity, readonly=readonly)
        if process.poll() is not None:
            raise ExecutionDenied('Output filesystem process ended')
        if not readonly:
            os.chmod(mount, 0o700)
        yield mount
    finally:
        try:
            if os.path.ismount(mount):
                result = subprocess.run([config['fusermount_path'], '-u', str(mount)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=environment, timeout=5, check=False)
                if result.returncode != 0:
                    raise ExecutionDenied('Output filesystem unmount failed')
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        if os.path.ismount(mount):
            raise ExecutionDenied('Output mount remains; operator reconciliation required')
        if process.returncode != 0:
            raise ExecutionDenied('Output filesystem did not exit cleanly')
        after = image.stat(follow_symlinks=False)
        if (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, capacity):
            raise ExecutionDenied('Output image identity or size changed')
        if readonly and (after.st_mtime_ns, after.st_ctime_ns) != (before.st_mtime_ns, before.st_ctime_ns):
            raise ExecutionDenied('Read-only output image changed during collection')


def cpu_set(value):
    """Parse the kernel CPU-list format with bounded ranges and no duplicates."""
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise ExecutionDenied('Missing or excessive CPU set')
    result = set()
    for part in value.split(','):
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?', part):
            raise ExecutionDenied('Invalid CPU set')
        bounds = [int(x) for x in part.split('-')]
        first, last = bounds[0], bounds[-1]
        if not 0 <= first <= last <= 65535:
            raise ExecutionDenied('CPU set range exceeds bounds')
        added = set(range(first, last + 1))
        if result & added:
            raise ExecutionDenied('Overlapping CPU set ranges')
        result.update(added)
    return result


def cgroup_cpu_set(proc_cgroup='/proc/self/cgroup', mount='/sys/fs/cgroup'):
    """Read enforced CPU membership, not merely a changeable process affinity."""
    unified, legacy = [], []
    for line in Path(proc_cgroup).read_text().splitlines():
        fields = line.split(':', 2)
        if len(fields) != 3 or not fields[0].isdecimal():
            raise ExecutionDenied('Invalid CPU cgroup membership')
        if fields[:2] == ['0', '']:
            unified.append(fields[2])
        elif 'cpuset' in fields[1].split(','):
            if fields[0] == '0':
                raise ExecutionDenied('Invalid legacy CPU controller')
            legacy.append(fields[2])
    if len(unified) > 1 or len(legacy) > 1 or not (unified or legacy):
        raise ExecutionDenied('Missing or ambiguous CPU cgroup membership')
    membership = (legacy or unified)[0]
    absolute(membership)
    root = Path(mount)/'cpuset' if legacy else Path(mount)
    current = root/membership.lstrip('/')
    effective = current/('cpuset.effective_cpus' if legacy else 'cpuset.cpus.effective')
    if effective.exists():
        return cpu_set(effective.read_text().strip())
    if not legacy:
        raise ExecutionDenied('Missing effective CPU cgroup set')
    # Older v1 kernels lack effective_cpus. A child's CPUs are constrained by
    # every ancestor, so intersect the nonempty configured sets up to the root.
    effective_cpus = None
    while True:
        configured = (current/'cpuset.cpus').read_text().strip()
        if configured:
            cpus = cpu_set(configured)
            effective_cpus = cpus if effective_cpus is None else effective_cpus & cpus
        if current == root:
            break
        current = current.parent
    if not effective_cpus:
        raise ExecutionDenied('No enforced CPU cgroup set')
    return effective_cpus


def validate_deployment_paths(profile):
    paths=[absolute(profile[key]) for key in ('requests_root','control_root','runtime_tree')]
    for index,left in enumerate(paths):
        for right in paths[index+1:]:
            if left==right or left in right.parents or right in left.parents:
                raise ExecutionDenied('Request, control and runtime storage must be disjoint')


class ArgCompare(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


@contextmanager
def namespace_filter(library_path):
    """Use libseccomp to compile a native x86-64 filter, never hand-written BPF.

    The caller must verify the administrator's pinned library before loading it.
    Other ABIs are deliberately unsupported until separately tested.
    """
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise ExecutionDenied('The namespace filter requires Linux x86-64')
    library = ctypes.CDLL(str(library_path), use_errno=True)
    signatures = {
        'seccomp_init': ([ctypes.c_uint32], ctypes.c_void_p),
        'seccomp_release': ([ctypes.c_void_p], None),
        'seccomp_syscall_resolve_name': ([ctypes.c_char_p], ctypes.c_int),
        'seccomp_rule_add_array': ([ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                   ctypes.c_uint, ctypes.POINTER(ArgCompare)], ctypes.c_int),
        'seccomp_export_bpf': ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise ExecutionDenied('Cannot create syscall filter')
    try:
        # New user namespaces could restore capabilities and bypass file limits.
        # clone3 has a pointer argument: ENOSYS permits libc to fall back to clone,
        # whose flags can be checked. This is not a general syscall allowlist.
        for name, error, comparison in (
            (b'unshare', errno.EPERM, None), (b'setns', errno.EPERM, None),
            (b'clone3', errno.ENOSYS, None),
            (b'clone', errno.EPERM, ArgCompare(0, 7, 0x10000000, 0x10000000)),
        ):
            number = library.seccomp_syscall_resolve_name(name)
            if number < 0 or library.seccomp_rule_add_array(
                context, 0x50000 | error, number, int(comparison is not None),
                ctypes.byref(comparison) if comparison is not None else None,
            ) != 0:
                raise ExecutionDenied('Required namespace filter rule unavailable')
        yield library, context
    finally:
        library.seccomp_release(context)


@contextmanager
def seccomp_fd(profile):
    path = absolute(profile['seccomp_library_path'])
    if digest(read_regular(path, 64*1024*1024)) != hash_value(profile['seccomp_library_sha256']):
        raise ExecutionDenied('Unverified syscall filter library')
    with namespace_filter(path) as (library, context):
        fd = os.memfd_create('auto-lammps-syscall-filter', os.MFD_CLOEXEC)
        try:
            if library.seccomp_export_bpf(context, fd) != 0 or not 0 < os.fstat(fd).st_size <= 65536:
                raise ExecutionDenied('Cannot export bounded syscall filter')
            os.lseek(fd, 0, os.SEEK_SET)
            yield fd
        finally:
            os.close(fd)


def engine_environment(profile):
    """Administrator options, covered by the signed profile; never module env.

    Library lookup stays in inventoried, read-only runtime directories. Mount
    targets must be excluded because they hide the runtime tree's contents.
    MPI launch, when explicitly configured, uses a separate pinned declaration.
    """
    options = profile.get('runtime_options', {})
    if not isinstance(options, dict) or set(options) - {'library_directories', 'mpi_transport', 'mpi_launcher'}:
        raise ExecutionDenied('Unsupported engine runtime options')
    directories = options.get('library_directories', [])
    if not isinstance(directories, list) or len(directories) > 8:
        raise ExecutionDenied('Declare bounded runtime library directories')
    checked = []
    for name in directories:
        relative(name)
        if name.split('/')[0] in {'work', 'output', 'proc', 'dev', 'tmp'} or name in checked:
            raise ExecutionDenied('Library lookup must not use mutable or mounted directories')
        inventory = profile.get('runtime_files', {})
        if not isinstance(inventory, dict) or not any(
                isinstance(path, str) and str(PurePosixPath(path).parent) == name for path in inventory):
            raise ExecutionDenied('Library directory has no inventoried files')
        checked.append(name)
    transport = options.get('mpi_transport', 'none')
    if transport not in ('none', 'intel-shm'):
        raise ExecutionDenied('Unsupported single-process transport')
    environment = {'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
    if checked:
        environment['LD_LIBRARY_PATH'] = ':'.join('/' + name for name in checked)
    if transport == 'intel-shm':
        environment['I_MPI_FABRICS'] = 'shm'
    return environment


def mpi_profile(profile):
    """Only the inventoried Intel Hydra local-fork layout is currently supported."""
    engine_environment(profile)  # Validate the complete options, including lookup.
    options = profile.get('runtime_options', {})
    if 'mpi_launcher' not in options:
        return None
    mpi = options['mpi_launcher']
    fields = {'kind', 'launcher_relative', 'pmi_proxy_relative', 'bootstrap_proxy_relative', 'max_ranks'}
    if (not isinstance(mpi, dict) or set(mpi) != fields or mpi['kind'] != 'intel-hydra-fork'
            or type(mpi['max_ranks']) is not int or not 1 <= mpi['max_ranks'] <= 8
            or options.get('mpi_transport') != 'intel-shm'):
        raise ExecutionDenied('Invalid bounded local MPI declaration')
    inventory = profile.get('runtime_files', {})
    if not isinstance(inventory, dict):
        raise ExecutionDenied('MPI requires an explicit runtime inventory')
    parents = set()
    for field, basename in [('launcher_relative', 'mpiexec.hydra'),
                            ('pmi_proxy_relative', 'hydra_pmi_proxy'),
                            ('bootstrap_proxy_relative', 'hydra_bstrap_proxy')]:
        name = relative(mpi[field])
        path = PurePosixPath(name)
        if (path.name != basename or name.split('/')[0] in {'work', 'output', 'proc', 'dev', 'tmp'}
                or name not in inventory):
            raise ExecutionDenied('MPI binaries must be explicitly inventoried outside mounted paths')
        hash_value(inventory[name])
        parents.add(path.parent)
    if len(parents) != 1:
        raise ExecutionDenied('Hydra launcher and proxies must share their inventoried directory')
    return mpi


def validate_parallelism(profile, cores):
    mpi = mpi_profile(profile)
    if type(cores) is not int or not 1 <= cores <= (mpi['max_ranks'] if mpi else 1):
        raise ExecutionDenied('Requested ranks exceed the declared runtime capacity')
    return mpi


def sandbox_command(profile, *, case, outputs, entrypoint, filter_fd, cores=1, cpu_ids=None):
    tree = absolute(profile['runtime_tree'])
    bwrap = absolute(profile['bwrap_path'])
    engine = '/' + relative(profile['engine_relative'])
    relative(entrypoint)
    case = absolute(str(case))
    args = [str(bwrap),'--unshare-all','--unshare-user','--unshare-cgroup','--die-with-parent','--new-session',
            '--cap-drop','ALL','--seccomp',str(filter_fd),'--ro-bind',str(tree),'/',
            '--proc','/proc','--dev-bind','/dev/null','/dev/null',
            '--ro-bind','/dev/urandom','/dev/urandom','--ro-bind',str(case),'/work',
            '--ro-bind',str(case/'output'),'/output']
    for name in outputs:
        relative(name)
        if '/' in name:
            raise ExecutionDenied('Only flat output mounts are supported')
        args.extend(['--bind',str(case/'output'/name),'/output/'+name])
    environment = {'PATH': '/bin', 'HOME': '/nonexistent', 'LC_ALL': 'C', **engine_environment(profile)}
    mpi = validate_parallelism(profile, cores)
    command = [engine, '-in', '/work/'+entrypoint, '-log', '/output/log.lammps', '-screen', 'none']
    if mpi:
        if (not isinstance(cpu_ids, (set, list, tuple)) or len(cpu_ids) != cores
                or any(type(cpu) is not int or not 0 <= cpu <= 65535 for cpu in cpu_ids)
                or len(set(cpu_ids)) != cores):
            raise ExecutionDenied('MPI requires the exact verified CPU set')
        # Both temporary mounts are RAM-backed and charged to the verified job
        # memory cgroup. No host scratch, /dev/shm or network is exposed.
        args.extend(['--tmpfs', '/tmp', '--tmpfs', '/dev/shm'])
        environment.update(I_MPI_PIN='1', I_MPI_PIN_PROCESSOR_LIST=','.join(map(str, sorted(cpu_ids))),
                           I_MPI_PIN_RESPECT_CPUSET='1', I_MPI_HYDRA_IFACE='lo', I_MPI_TMPDIR='/tmp')
        command = ['/'+mpi['launcher_relative'], '-launcher', 'fork', '-hosts', '127.0.0.1',
                   '-localhost', '127.0.0.1', '-iface', 'lo', '-n', str(cores), *command]
    args.extend(['--chdir','/work'])
    for name, value in environment.items():
        args.extend(['--setenv', name, value])
    args.extend(['--remount-ro','/', '--', *command])
    return args


def verify_runtime_tree(profile):
    mpi_profile(profile)
    root = absolute(profile['runtime_tree'])
    fd = directory(root)
    os.close(fd)
    expected = profile['runtime_files']
    if not isinstance(expected,dict) or not 1 <= len(expected) <= 512:
        raise ExecutionDenied('Explicit runtime file inventory is required')
    seen = set()
    total = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(folder)/name).is_symlink():
                raise ExecutionDenied('Runtime directory symlinks are not allowed')
        for name in files:
            path = Path(folder)/name
            relative_path = path.relative_to(root).as_posix()
            relative(relative_path)
            if relative_path not in expected:
                raise ExecutionDenied('Undeclared runtime file')
            content = read_regular(path, 1024*1024*1024-total)
            total += len(content)
            if digest(content) != hash_value(expected[relative_path]):
                raise ExecutionDenied('Runtime file version mismatch')
            seen.add(relative_path)
    if seen != set(expected) or profile['engine_relative'] not in seen:
        raise ExecutionDenied('Runtime inventory incomplete')


def limit_child(memory_bytes, file_bytes):
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_FSIZE,(file_bytes,file_bytes))
    resource.setrlimit(resource.RLIMIT_AS,(memory_bytes,memory_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))


def execute(profile_path, request_id, manifest_sha256):
    if sys.platform != 'linux':
        raise ExecutionDenied('Only the approved Linux compute-node runtime may execute')
    if threading.active_count() != 1:
        raise ExecutionDenied('Use a dedicated single-threaded execution worker')
    if not re.fullmatch(r'[a-f0-9]{32}',request_id):
        raise ExecutionDenied('Invalid request')
    hash_value(manifest_sha256)
    job_id = os.environ.get('SLURM_JOB_ID','')
    if not re.fullmatch(r'[1-9][0-9]{0,19}',job_id):
        raise ExecutionDenied('A scheduler allocation is required')
    profile_data = read_regular(profile_path,1000000,private=True)
    profile = json.loads(profile_data)
    validate_deployment_paths(profile)
    case = absolute(profile['requests_root']) / request_id
    fd = directory(case,private=True)
    os.close(fd)
    control = absolute(profile['control_root'])
    fd = directory(control,private=True)
    os.close(fd)
    key = read_regular(control/'grant.key',32,private=True)
    grant = verify_grant(json.loads(read_regular(control/(request_id+'.json'),16384,private=True)),key,
                         request_id=request_id,manifest_sha256=manifest_sha256,
                         profile_sha256=digest(profile_data),now=time.time())
    manifest_data = read_regular(case/'manifest.json',1000000)
    if digest(manifest_data) != manifest_sha256:
        raise ExecutionDenied('Staged manifest changed')
    manifest = json.loads(manifest_data)
    resources = manifest['resources']
    if (set(resources) != {'cores','wall_seconds','memory_bytes','storage_bytes'}
            or any(type(value) is not int or value <= 0 for value in resources.values())):
        raise ExecutionDenied('Invalid frozen resource limits')
    if resources != grant['resources'] or manifest['provenance']['task_sha256'] != grant['task_sha256']:
        raise ExecutionDenied('Grant resources/task mismatch')
    validate_parallelism(profile, resources['cores'])
    cpus = cgroup_cpu_set()
    if len(cpus) != resources['cores'] or set(os.sched_getaffinity(0)) != cpus:
        raise ExecutionDenied('CPU cgroup and affinity must match the exact approved allocation')
    if cgroup_memory_limit() > resources['memory_bytes']:
        raise ExecutionDenied('Memory cgroup exceeds approved ceiling')
    for prefix in ('bwrap','scontrol'):
        if digest(read_regular(absolute(profile[prefix+'_path']),64*1024*1024)) != hash_value(profile[prefix+'_sha256']):
            raise ExecutionDenied('Unverified deployment executable')
    query_start = time.monotonic()
    query = subprocess.run([profile['scontrol_path'],'show','job',job_id,'--oneliner'],
                           capture_output=True,timeout=10,check=True,env={'LC_ALL':'C','PATH':'/usr/bin:/bin'})
    if len(query.stdout)+len(query.stderr)>65536:
        raise ExecutionDenied('Oversized allocation record')
    elapsed = parse_allocation(query.stdout.decode('utf-8'),request_id=request_id,manifest_sha256=manifest_sha256,
                               job_id=job_id,uid=os.getuid(),host=socket.gethostname(),resources=resources)
    verify_runtime_tree(profile)
    staged = json.loads(read_regular(case/'stage.json',16384))
    if staged.get('state') != 'staged' or staged.get('manifest_sha256') != manifest_sha256 or staged.get('request_id') != request_id:
        raise ExecutionDenied('Missing complete upload receipt')
    input_bytes = len(manifest_data)
    for item in manifest['files']:
        path = case/relative(item['path'])
        content = read_regular(path,item['size'])
        if len(content)!=item['size'] or digest(content)!=item['sha256']:
            raise ExecutionDenied('Staged input changed')
        input_bytes += len(content)
    # Batch script is present before Slurm starts the job. Account for it in
    # addition to bounded, potentially late scheduler/controller receipts.
    if (case/'job.sh').exists():
        input_bytes += len(read_regular(case/'job.sh',1000000))
    outputs = grant['outputs']
    per_file = validate_outputs(outputs,storage_bytes=resources['storage_bytes'],input_bytes=input_bytes)
    allocation = volume_allocation(profile, storage_bytes=resources['storage_bytes'], input_bytes=input_bytes)
    if allocation:
        per_file = allocation['image_bytes']
    with seccomp_fd(profile) as filter_descriptor, ExitStack() as output_stack:
        argv = sandbox_command(profile,case=case,outputs=outputs,entrypoint=manifest['entrypoint'],
                               filter_fd=filter_descriptor,cores=resources['cores'],cpu_ids=cpus)
        # Exclusive intent is also a restart/requeue gate. Never execute twice from
        # the same request, even if a previous process died before its final receipt.
        write_once(case/'execution-intent.json',dict(request_id=request_id,job_id=job_id,manifest_sha256=manifest_sha256,
            profile_sha256=digest(profile_data),grant_sha256=digest(canonical(grant)),argv_sha256=digest(canonical(argv)),
            outputs=outputs, ranks=resources['cores'], cpu_ids=sorted(cpus), output_storage=allocation,
            at=datetime.now(timezone.utc).isoformat()))
        output = case/'output'
        if allocation:
            output_stack.enter_context(mounted_output_volume(profile, case, allocation))
        else:
            output.mkdir(mode=0o700,exist_ok=False)
        for name in outputs:
            fd = os.open(output/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            os.close(fd)
        remaining = resources['wall_seconds'] - elapsed - (time.monotonic()-query_start)
        if remaining<=0:
            raise ExecutionDenied('Preflight exhausted remaining wall time')
        started = time.monotonic()
        with (output/'stdout.txt').open('wb') as stdout, (output/'stderr.txt').open('wb') as stderr:
            process = subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                close_fds=True,pass_fds=(filter_descriptor,),start_new_session=True,env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},
                preexec_fn=lambda: limit_child(resources['memory_bytes'],per_file))
            timed_out=False
            try:
                code=process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out=True
                os.killpg(process.pid,signal.SIGKILL)
                code=process.wait()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid,signal.SIGKILL)
                    process.wait()

    result=dict(request_id=request_id,job_id=job_id,returncode=code,timed_out=timed_out,
                elapsed_seconds=time.monotonic()-started,scientific_status='not_evaluated')
    write_once(case/'execution-result.json',result)
    return result


def collect(profile_path, request_id, manifest_sha256, job_id, root_path, stream):
    """Read-only framed export for the trusted controller after final accounting.

    Never executes inputs, invokes a scheduler, or reads the private grant key.
    The caller must establish terminal scheduler state; a launcher receipt alone
    does not prove that allocation accounting has finished.
    """
    if not re.fullmatch(r'[a-f0-9]{32}', request_id) or not re.fullmatch(r'[1-9][0-9]{0,19}', job_id):
        raise ExecutionDenied('Invalid collection identity')
    hash_value(manifest_sha256)
    profile = json.loads(read_regular(profile_path, 1000000, private=True))
    validate_deployment_paths(profile)
    if root_path != profile['requests_root']:
        raise ExecutionDenied('Wrong collection root')
    case = absolute(root_path)/request_id
    fd = directory(case, private=True)
    os.close(fd)
    manifest_data = read_regular(case/'manifest.json', 1000000)
    if digest(manifest_data) != manifest_sha256:
        raise ExecutionDenied('Collection belongs to another input')
    budget = json.loads(manifest_data)['resources']['storage_bytes']
    if type(budget) is not int or budget <= 0:
        raise ExecutionDenied('Invalid collection byte bound')
    controls = {}
    for name in ('execution-intent.json', 'execution-result.json', 'scheduler-result.json'):
        try:
            controls[name] = read_regular(case/name, 200000 if name == 'scheduler-result.json' else 16384)
        except FileNotFoundError:
            pass
    intent = json.loads(controls.get('execution-intent.json', b'null'))
    result = json.loads(controls.get('execution-result.json', b'null'))
    accepted = json.loads(controls.get('scheduler-result.json', b'null'))
    if intent is not None:
        if (intent.get('request_id') != request_id or intent.get('manifest_sha256') != manifest_sha256
                or intent.get('job_id') != job_id):
            raise ExecutionDenied('Wrong execution intent')
        outputs = intent.get('outputs')
        # This also rejects legacy intents without a declared output list.
        validate_outputs(outputs, storage_bytes=budget, input_bytes=0)
    else:
        if (not isinstance(accepted, dict) or accepted.get('state') != 'accepted'
                or accepted.get('request_id') != request_id or accepted.get('job_id') != job_id
                or accepted.get('manifest_sha256') != manifest_sha256):
            raise ExecutionDenied('No matching remote execution or acceptance identity')
        outputs = []
    if result is not None and (intent is None or result.get('request_id') != request_id
            or result.get('job_id') != job_id or type(result.get('returncode')) is not int
            or type(result.get('timed_out')) is not bool or result.get('scientific_status') != 'not_evaluated'):
        raise ExecutionDenied('Invalid execution result')
    header = dict(schema_version=1, request_id=request_id, manifest_sha256=manifest_sha256, job_id=job_id,
                  execution=dict(state='finished' if result else 'incomplete',
                                 returncode=result['returncode'] if result else None,
                                 timed_out=result['timed_out'] if result else None),
                  scientific_status='not_evaluated', missing_outputs=[], files=[])
    names = [name for name in ('execution-intent.json', 'execution-result.json') if name in controls]
    names += ['scheduler.stdout', 'scheduler.stderr'] + ['output/'+name for name in outputs]
    total = 0
    with ExitStack() as stack:
        allocation = intent.get('output_storage') if intent else None
        if allocation:
            if intent.get('profile_sha256') != digest(read_regular(profile_path, 1000000, private=True)):
                raise ExecutionDenied('Output volume profile changed since execution')
            manifest = json.loads(manifest_data)
            input_bytes = len(manifest_data)+sum(item['size'] for item in manifest['files'])
            if (case/'job.sh').exists():
                input_bytes += len(read_regular(case/'job.sh', 1000000))
            if allocation != volume_allocation(profile, storage_bytes=budget, input_bytes=input_bytes):
                raise ExecutionDenied('Output volume allocation does not match frozen resources')
            stack.enter_context(mounted_output_volume(profile, case, allocation, readonly=True))
        elif intent and profile.get('output_volume') is not None:
            raise ExecutionDenied('Missing output volume receipt')
        opened = []
        for name in names:
            path = case/name
            try:
                parent = directory(path.parent)
                try:
                    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                finally:
                    os.close(parent)
            except FileNotFoundError:
                if name.startswith('output/'):
                    header['missing_outputs'].append(name)
                elif name in controls:
                    raise ExecutionDenied('Execution receipt disappeared')
                continue
            handle = stack.enter_context(os.fdopen(fd, 'rb'))
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > budget-total):
                raise ExecutionDenied('Unsafe or oversized output')
            checksum = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = handle.read(min(remaining, 65536))
                if not block:
                    raise ExecutionDenied('Output truncated during collection')
                checksum.update(block)
                remaining -= len(block)
            after = os.fstat(fd)
            if handle.read(1) or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ExecutionDenied('Output changed during collection')
            if name in controls and checksum.hexdigest() != digest(controls[name]):
                raise ExecutionDenied('Execution receipt changed')
            total += before.st_size
            header['files'].append(dict(path=name, size=before.st_size, sha256=checksum.hexdigest()))
            opened.append((handle, before))
        data = canonical(header)
        if len(data) > 65536:
            raise ExecutionDenied('Collection header too large')
        stream.write(struct.pack('!I', len(data))+data)
        for (handle, before), item in zip(opened, header['files']):
            handle.seek(0)
            checksum = hashlib.sha256()
            remaining = item['size']
            while remaining:
                block = handle.read(min(remaining, 65536))
                if not block:
                    raise ExecutionDenied('Output truncated during transfer')
                checksum.update(block)
                stream.write(block)
                remaining -= len(block)
            after = os.fstat(handle.fileno())
            if handle.read(1) or checksum.hexdigest() != item['sha256'] or (
                    before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ExecutionDenied('Output changed during transfer')
        stream.flush()
    return header


def main():
    parser=argparse.ArgumentParser(description='Approved, single-node compute execution only.')
    parser.add_argument('--request-id',required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--collect', action='store_true', help='Read existing outputs only; never execute')
    parser.add_argument('--job-id')
    parser.add_argument('--root')
    args=parser.parse_args()
    if args.collect:
        if not args.job_id or not args.root:
            parser.error('Collection requires the accounted job identity and configured root')
        signal.alarm(60)
        try:
            collect(Path(__file__).resolve().parent/'runtime.json', args.request_id,
                    args.manifest_sha256, args.job_id, args.root, sys.stdout.buffer)
        except Exception as exc:
            print(json.dumps({'state':'collection_failed','error_type':type(exc).__name__}), file=sys.stderr)
            return 1
        finally:
            signal.alarm(0)
        return 0
    if args.job_id or args.root:
        parser.error('Collection arguments cannot be used for execution')
    try:
        result=execute(Path(__file__).resolve().parent/'runtime.json',args.request_id,args.manifest_sha256)
    except Exception as exc:
        print(json.dumps({'state':'execution_denied','error_type':type(exc).__name__,
                          'reason':str(exc) if isinstance(exc,ExecutionDenied) else 'Runtime setup or I/O failed'}))
        return 1
    print(json.dumps(result,sort_keys=True))
    return 0 if result['returncode']==0 and not result['timed_out'] else 1


if __name__=='__main__':
    raise SystemExit(main())
