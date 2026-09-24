"""Installed stage/compute helper for approved engine builds, never target physics."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import types


def read_bootstrap(path,limit,*,private=False,allow_hardlinks=False):
    path=Path(path)
    if not path.is_absolute() or path!=path.resolve():raise ValueError('Unsafe installed path')
    with path.open('rb') as handle:
        info=os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or (info.st_nlink!=1 and not allow_hardlinks) or info.st_size>limit or info.st_mode&0o022
                or private and (info.st_uid!=os.getuid() or info.st_mode&0o077)):
            raise ValueError('Unsafe installed file')
        data=handle.read(limit+1)
    if len(data)!=info.st_size:raise ValueError('Installed file changed')
    return data


def load_profile(path):
    raw=read_bootstrap(path,100000,private=True);profile=json.loads(raw)
    loaded=[]
    for name in ('runtime','source_helper'):
        data=read_bootstrap(profile[name+'_path'],1000000)
        if hashlib.sha256(data).hexdigest()!=profile[name+'_sha256']:raise ValueError('Helper version mismatch')
        module=types.ModuleType('approved_'+name)
        exec(compile(data,profile[name+'_path'],'exec'),module.__dict__);loaded.append(module)
    runtime,source=loaded
    runtime.validate_deployment_paths(profile)
    return raw,profile,runtime,source


def validate_documents(runtime,source,manifest,spec):
    resources=manifest.get('resources',{})
    if (set(resources)!={'cores','wall_seconds','memory_bytes','storage_bytes'}
            or any(type(v) is not int or v<=0 for v in resources.values())
            or resources['cores']>8 or resources['wall_seconds']>1800 or resources['wall_seconds']%60
            or resources['memory_bytes']>8*1024**3 or resources['memory_bytes']%(1024**2)
            or not 16*1024**2<=resources['storage_bytes']<=16*1024**3):
        raise ValueError('Invalid build resource limits')
    keys={'kind','source_directory','source_result_sha256','source_inventory_sha256','source_commit','requirements','tools'}
    if not isinstance(spec,dict) or set(spec)!=keys or spec['kind']!='engine_build':
        raise ValueError('Invalid build specification')
    source.validate_requirements(spec['requirements'])
    if resources['cores']!=spec['requirements']['cores']:raise ValueError('Build core mismatch')
    runtime.absolute(spec['source_directory'])
    for name in ('source_result_sha256','source_inventory_sha256'):runtime.hash_value(spec[name])
    if not re.fullmatch('[a-f0-9]{40}',spec['source_commit']):raise ValueError('Source commit required')
    if not isinstance(spec['tools'],dict) or set(spec['tools'])!={'cmake','compiler','mpi_compiler','make'}:
        raise ValueError('Invalid build tools')
    for item in spec['tools'].values():
        if not isinstance(item,dict) or set(item)!={'path','sha256'}:raise ValueError('Invalid pinned tool')
        runtime.absolute(item['path']);runtime.hash_value(item['sha256'])
    encoded=runtime.canonical(spec);digest=runtime.digest(encoded)
    expected=dict(schema_version=1,kind='engine_build',resources=resources,
                  provenance={'task_sha256':digest},files=[dict(path='build.json',role='engine_build_spec',size=len(encoded),sha256=digest)])
    if manifest!=expected:raise ValueError('Build manifest does not match specification')
    return resources


def stage(profile_path,request_id,manifest_sha256,root_path,stream):
    if sys.platform!='linux':raise ValueError('HPC staging requires Linux')
    raw,profile,runtime,source=load_profile(profile_path)
    if not re.fullmatch('[a-f0-9]{32}',request_id):raise ValueError('Invalid request identity')
    runtime.hash_value(manifest_sha256)
    if root_path!=profile['requests_root']:raise ValueError('Wrong build staging root')
    header=stream.read(4)
    if len(header)!=4:raise ValueError('Incomplete manifest header')
    size=struct.unpack('!I',header)[0]
    if not 1<=size<=100000:raise ValueError('Manifest limit')
    data=stream.read(size)
    if runtime.digest(data)!=manifest_sha256:raise ValueError('Build manifest hash mismatch')
    manifest=json.loads(data)
    if len(manifest.get('files',[]))!=1:raise ValueError('One build specification required')
    n=manifest['files'][0].get('size')
    if type(n) is not int or not 1<=n<=100000:raise ValueError('Specification limit')
    encoded=stream.read(n)
    if len(encoded)!=n or stream.read(1):raise ValueError('Incomplete or trailing build data')
    spec=json.loads(encoded);resources=validate_documents(runtime,source,manifest,spec)
    if runtime.digest(encoded)!=manifest['files'][0]['sha256']:raise ValueError('Build spec hash mismatch')
    allocation=runtime.volume_allocation(profile,storage_bytes=resources['storage_bytes'],input_bytes=size+n)
    if allocation is None:raise ValueError('A bounded build output volume is required')
    case=Path(root_path)/request_id
    case.mkdir(mode=0o700,exist_ok=False)
    for name,content in (('manifest.json',data),('build.json',encoded)):
        with (case/name).open('xb') as output:
            os.fchmod(output.fileno(),0o400);output.write(content);output.flush();os.fsync(output.fileno())
    result=dict(schema_version=1,state='staged',request_id=request_id,manifest_sha256=manifest_sha256,
                storage_bytes=resources['storage_bytes'],input_bytes=n,policy_sha256=runtime.digest(raw))
    runtime.write_once(case/'stage.json',result)
    return result


def verify_source(runtime,source,spec):
    root=runtime.absolute(spec['source_directory'])
    data=runtime.read_regular(root/'result.json',20000)
    if runtime.digest(data)!=spec['source_result_sha256']:raise ValueError('Source preparation changed')
    report=json.loads(data)
    if (report.get('state')!='source_ready' or report.get('commit')!=spec['source_commit']
            or report.get('requirements')!=spec['requirements']):raise ValueError('Source preparation mismatch')
    inventory_data=runtime.read_regular(root/'inventory.json',16*1024**2)
    if runtime.digest(inventory_data)!=spec['source_inventory_sha256']:raise ValueError('Source inventory changed')
    inventory=json.loads(inventory_data);seen=set()
    if not isinstance(inventory.get('files'),list) or not 1<=len(inventory['files'])<=50000:
        raise ValueError('Invalid source inventory')
    for item in inventory['files']:
        path=item['path']
        if (not isinstance(path,str) or len(path)>4096 or not path or
                PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                or str(PurePosixPath(path))!=path or '\\' in path or '\x00' in path):
            raise ValueError('Unsafe source inventory path')
        if path in seen:raise ValueError('Duplicate source inventory path')
        seen.add(path)
        if type(item['size']) is not int or not 0<=item['size']<=32*1024**2:raise ValueError('Source file limit')
        content=runtime.read_regular(root/'source'/path,item['size'])
        if len(content)!=item['size'] or runtime.digest(content)!=item['sha256']:raise ValueError('Source file changed')
    paths=list((root/'source').rglob('*'))
    if any(p.is_symlink() for p in paths):raise ValueError('Unexpected source symlink')
    actual={str(p.relative_to(root/'source')) for p in paths if not p.is_dir()}
    if actual!=seen:raise ValueError('Unexpected source files')
    for item in spec['tools'].values():
        # System compiler aliases may be hard links; retain the original driver path.
        if runtime.digest(read_bootstrap(item['path'],64*1024**2,allow_hardlinks=True))!=item['sha256']:
            raise ValueError('Build tool changed')
    return root/'source'


def compile_commands(commands,output,*,seconds,memory_bytes,file_bytes,runtime):
    """Only fixed CMake configure/build argv, with output inside the bounded volume."""
    deadline=time.monotonic()+seconds;results=[]
    for name in ('home','tmp'):(output/name).mkdir(mode=0o700)
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','HOME':str(output/'home'),'TMPDIR':str(output/'tmp')}
    for name,argv in commands:
        with (output/(name+'.log')).open('xb') as log:
            process=subprocess.Popen(argv,cwd=output,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                env=environment,close_fds=True,start_new_session=True,
                preexec_fn=lambda:runtime.limit_child(memory_bytes,file_bytes))
            timed_out=False
            try:
                code=process.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out=True;os.killpg(process.pid,signal.SIGKILL);code=process.wait()
            finally:
                # Do not leave children writing after a failed or completed tool.
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
        results.append(dict(stage=name,returncode=code,timed_out=timed_out))
        if code!=0 or timed_out:break
    return results


def execute(profile_path,request_id,manifest_sha256):
    if sys.platform!='linux':raise ValueError('Builds run on HPC compute nodes only')
    raw,profile,runtime,source=load_profile(profile_path)
    if not re.fullmatch('[a-f0-9]{32}',request_id):raise ValueError('Invalid request identity')
    runtime.hash_value(manifest_sha256)
    job=os.environ.get('SLURM_JOB_ID','')
    if not re.fullmatch('[1-9][0-9]{0,19}',job):raise ValueError('An accounted Slurm allocation is required')
    case=Path(profile['requests_root'])/request_id;control=Path(profile['control_root'])
    manifest_data=runtime.read_regular(case/'manifest.json',100000)
    if runtime.digest(manifest_data)!=manifest_sha256:raise ValueError('Build manifest changed')
    manifest=json.loads(manifest_data);spec_data=runtime.read_regular(case/'build.json',100000);spec=json.loads(spec_data)
    resources=validate_documents(runtime,source,manifest,spec)
    key=runtime.read_regular(control/'grant.key',32,private=True)
    grant=runtime.verify_grant(json.loads(runtime.read_regular(control/(request_id+'.json'),16384,private=True)),key,
        request_id=request_id,manifest_sha256=manifest_sha256,profile_sha256=runtime.digest(raw),now=time.time())
    if (grant.get('purpose')!='engine_build' or grant.get('resources')!=resources
            or grant.get('task_sha256')!=manifest['provenance']['task_sha256']
            or grant.get('scoring_sha256')!=runtime.digest(runtime.canonical(spec['requirements']))
            or grant.get('static_check_sha256')!=spec['source_inventory_sha256']):raise ValueError('Grant is not for this build')
    batch=runtime.read_regular(case/'job.sh',1000000)
    if runtime.digest(batch)!=grant.get('batch_sha256'):raise ValueError('Batch changed')
    staged=json.loads(runtime.read_regular(case/'stage.json',16384))
    if staged.get('state')!='staged' or staged.get('manifest_sha256')!=manifest_sha256:raise ValueError('Build not staged')
    cpus=runtime.cgroup_cpu_set()
    if len(cpus)!=resources['cores'] or set(os.sched_getaffinity(0))!=cpus:raise ValueError('CPU allocation differs')
    if runtime.cgroup_memory_limit()>resources['memory_bytes']:raise ValueError('Memory limit not enforced')
    if runtime.digest(runtime.read_regular(profile['scontrol_path'],64*1024**2))!=profile['scontrol_sha256']:
        raise ValueError('Scheduler tool changed')
    started=time.monotonic()
    query=subprocess.run([profile['scontrol_path'],'show','job',job,'--oneliner'],capture_output=True,timeout=10,check=True,
                         env={'LC_ALL':'C','PATH':'/usr/bin:/bin'})
    if len(query.stdout)+len(query.stderr)>65536:raise ValueError('Allocation record limit')
    elapsed=runtime.parse_allocation(query.stdout.decode(),request_id=request_id,manifest_sha256=manifest_sha256,
        job_id=job,uid=os.getuid(),host=socket.gethostname(),resources=resources)
    source_directory=verify_source(runtime,source,spec)
    if Path(profile['requests_root']) in source_directory.parents:raise ValueError('Source must be outside writable requests')
    allocation=runtime.volume_allocation(profile,storage_bytes=resources['storage_bytes'],
        input_bytes=len(manifest_data)+len(spec_data)+len(batch))
    if allocation is None:raise ValueError('A bounded build volume is required')
    plan=source.cmake_plan(spec['requirements'],source=str(source_directory),build=str(case/'output/build'),
        cmake=spec['tools']['cmake']['path'],compiler=spec['tools']['compiler']['path'],mpi_compiler=spec['tools']['mpi_compiler']['path'])
    plan['configure'] += ['-G','Unix Makefiles','-D','CMAKE_MAKE_PROGRAM='+spec['tools']['make']['path'], '-D','BUILD_TESTING=OFF']
    runtime.write_once(case/'execution-intent.json',dict(kind='engine_build',request_id=request_id,job_id=job,
        manifest_sha256=manifest_sha256,source_inventory_sha256=spec['source_inventory_sha256'],output_storage=allocation,
        argv_sha256=runtime.digest(runtime.canonical(plan)),at=datetime.now(timezone.utc).isoformat()))
    result=dict(kind='engine_build',request_id=request_id,job_id=job,manifest_sha256=manifest_sha256,
                built=False,environment_verified=False,scientific_validation=False)
    try:
        with runtime.mounted_output_volume(profile,case,allocation) as output:
            remaining=resources['wall_seconds']-elapsed-(time.monotonic()-started)
            if remaining<=0:raise ValueError('Build preflight exhausted wall time')
            result['commands']=compile_commands([('configure',plan['configure']),('build',plan['build'])],output,
                seconds=remaining,memory_bytes=resources['memory_bytes'],file_bytes=allocation['image_bytes'],runtime=runtime)
            result['built']=len(result['commands'])==2 and all(r['returncode']==0 and not r['timed_out'] for r in result['commands'])
            if result['built']:
                binary=runtime.read_regular(output/'build/lmp',256*1024**2)
                if not binary.startswith(b'\x7fELF'):raise ValueError('Build output is not an ELF executable')
                result.update(engine_sha256=runtime.digest(binary),engine_bytes=len(binary),artifact='build/lmp')
    except (OSError,ValueError,runtime.ExecutionDenied) as exc:
        result.update(built=False,failure_type=type(exc).__name__)
    runtime.write_once(case/'execution-result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description='Approved engine build stage/compute worker')
    parser.add_argument('--request-id',required=True);parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--root')
    args=parser.parse_args();profile=Path(__file__).resolve().parent/'runtime.json'
    if args.root:result=stage(profile,args.request_id,args.manifest_sha256,args.root,sys.stdin.buffer)
    else:result=execute(profile,args.request_id,args.manifest_sha256)
    print(json.dumps(result))
    return 0 if args.root or result['built'] else 1


if __name__=='__main__':raise SystemExit(main())
