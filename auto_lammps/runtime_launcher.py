"""Standalone Linux compute-node launcher. No setup, submission or approval issuer.

The administrator installs this reviewed file with runtime.json next to it.
A private controller signs request grants; neither key nor grants are mounted
inside the sandbox. Tests use synthetic data and never invoke a physics engine.
"""
import argparse
from contextlib import contextmanager
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
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
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
    """Require an inherited cgroup-v2 hard memory ceiling, not an env assertion."""
    lines = Path(proc_cgroup).read_text().splitlines()
    memberships = [line[3:] for line in lines if line.startswith('0::')]
    if len(memberships) != 1 or not memberships[0].startswith('/') or '..' in PurePosixPath(memberships[0]).parts:
        raise ExecutionDenied('A cgroup-v2 allocation is required')
    mount = Path(mount)
    current = mount / memberships[0].lstrip('/')
    limits = []
    while True:
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


def sandbox_command(profile, *, case, outputs, entrypoint, filter_fd):
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
    args.extend(['--chdir','/work','--setenv','PATH','/bin','--setenv','HOME','/nonexistent',
                 '--setenv','LC_ALL','C','--remount-ro','/', '--',engine,
                 '-in','/work/'+entrypoint,'-log','/output/log.lammps','-screen','none'])
    return args


def verify_runtime_tree(profile):
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
    if resources['cores'] != 1:
        raise ExecutionDenied('Current entry supports a single process; MPI is not enabled')
    if len(os.sched_getaffinity(0)) > resources['cores']:
        raise ExecutionDenied('CPU affinity exceeds approved allocation')
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
    with seccomp_fd(profile) as filter_descriptor:
        argv = sandbox_command(profile,case=case,outputs=outputs,entrypoint=manifest['entrypoint'],filter_fd=filter_descriptor)
        # Exclusive intent is also a restart/requeue gate. Never execute twice from
        # the same request, even if a previous process died before its final receipt.
        write_once(case/'execution-intent.json',dict(request_id=request_id,job_id=job_id,manifest_sha256=manifest_sha256,
            profile_sha256=digest(profile_data),grant_sha256=digest(canonical(grant)),argv_sha256=digest(canonical(argv)),
            at=datetime.now(timezone.utc).isoformat()))
        output = case/'output'
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


def main():
    parser=argparse.ArgumentParser(description='Approved, single-process compute-node execution only.')
    parser.add_argument('--request-id',required=True)
    parser.add_argument('--manifest-sha256',required=True)
    args=parser.parse_args()
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
