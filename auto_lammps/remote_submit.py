"""Standalone, grant-bound sbatch handler. Install with private submission.json.

Loads a digest-pinned runtime helper from administrator storage. No grants are
issued here. A durable exclusive intent prevents repeating an uncertain submit.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import types


def bootstrap_read(path, limit, *, private=False):
    # Configuration and installed code are administrator owned, never upload data.
    path=Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError('Deployment path must be absolute and contain no symlinks')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>limit
                or info.st_mode&0o022 or (private and (info.st_uid!=os.getuid() or info.st_mode&0o077))):
            raise ValueError('Unsafe deployment file')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            data=stream.read(limit+1)
        if len(data)!=info.st_size:
            raise ValueError('Deployment file changed')
        return data
    finally:
        os.close(fd)


def capture_sbatch(argv, cwd, *, timeout=20, limit=65536):
    output={'stdout':bytearray(),'stderr':bytearray()}
    with subprocess.Popen(argv,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          close_fds=True,start_new_session=True,env={'LC_ALL':'C','PATH':'/usr/bin:/bin'}) as process:
        end=time.monotonic()+timeout
        failure=''
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ,'stdout')
                selector.register(process.stderr,selectors.EVENT_READ,'stderr')
                while selector.get_map():
                    if time.monotonic()>=end:
                        failure='timeout'; break
                    for key,_ in selector.select(min(.1,max(0,end-time.monotonic()))):
                        room=limit-sum(len(value) for value in output.values())
                        block=os.read(key.fileobj.fileno(),min(65536,max(1,room+1)))
                        if not block: selector.unregister(key.fileobj)
                        else: output[key.data].extend(block)
                    if sum(len(value) for value in output.values())>limit:
                        failure='output_limit'; break
            if not failure:
                try: process.wait(timeout=max(.001,end-time.monotonic()))
                except subprocess.TimeoutExpired: failure='timeout'
        finally:
            # A scheduler helper can exit while descendants retain its pipes.
            if failure or process.poll() is None:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
        return dict(returncode=process.returncode,failure=failure,
                    **{key:base64.b64encode(value).decode() for key,value in output.items()})


def submit(config_path, request_id, manifest_sha256, root_path):
    if sys.platform!='linux':
        raise ValueError('Scheduler submission requires the approved Linux service')
    if not re.fullmatch(r'[a-f0-9]{32}',request_id) or not re.fullmatch(r'[a-f0-9]{64}',manifest_sha256):
        raise ValueError('Invalid request identity')
    config_data=bootstrap_read(config_path,16384,private=True)
    config=json.loads(config_data)
    source=bootstrap_read(config['runtime_path'],1000000)
    if hashlib.sha256(source).hexdigest()!=config['runtime_sha256']:
        raise ValueError('Installed runtime helper version mismatch')
    runtime=types.ModuleType('approved_runtime_helper')
    exec(compile(source,config['runtime_path'],'exec'),runtime.__dict__)
    profile_path=Path(config['runtime_path']).parent/'runtime.json'
    profile_data=runtime.read_regular(profile_path,1000000,private=True)
    profile=json.loads(profile_data)
    runtime.validate_deployment_paths(profile)
    if root_path!=profile['requests_root']:
        raise ValueError('Wrong deployment root')
    case=runtime.absolute(root_path)/request_id
    fd=runtime.directory(case,private=True); os.close(fd)
    control=runtime.absolute(profile['control_root'])
    fd=runtime.directory(control,private=True); os.close(fd)
    key=runtime.read_regular(control/'grant.key',32,private=True)
    envelope=json.loads(runtime.read_regular(control/(request_id+'.json'),16384,private=True))
    grant=runtime.verify_grant(envelope,key,request_id=request_id,manifest_sha256=manifest_sha256,
                              profile_sha256=runtime.digest(profile_data),now=time.time())
    manifest_data=runtime.read_regular(case/'manifest.json',1000000)
    if runtime.digest(manifest_data)!=manifest_sha256:
        raise ValueError('Staged manifest differs from signed grant')
    manifest=json.loads(manifest_data)
    if manifest['resources']!=grant['resources'] or manifest['provenance']['task_sha256']!=grant['task_sha256']:
        raise ValueError('Grant task/resource mismatch')
    stage=json.loads(runtime.read_regular(case/'stage.json',16384))
    if stage.get('state')!='staged' or stage.get('request_id')!=request_id or stage.get('manifest_sha256')!=manifest_sha256:
        raise ValueError('Incomplete upload')
    for item in manifest['files']:
        content=runtime.read_regular(case/runtime.relative(item['path']),item['size'])
        if runtime.digest(content)!=item['sha256'] or len(content)!=item['size']:
            raise ValueError('Staged file changed')
    script=runtime.read_regular(control/(request_id+'.sh'),1000000,private=True)
    if runtime.digest(script)!=runtime.hash_value(grant['batch_sha256']):
        raise ValueError('Batch script has not been approved for this request')
    binary=runtime.absolute(config['sbatch_path'])
    if runtime.digest(runtime.read_regular(binary,64*1024*1024))!=runtime.hash_value(config['sbatch_sha256']):
        raise ValueError('Unverified scheduler executable')
    result_path=case/'scheduler-result.json'
    intent_path=case/'scheduler-intent.json'
    if intent_path.exists():
        if not result_path.exists():
            return dict(state='unknown',request_id=request_id,manifest_sha256=manifest_sha256)
        raw=runtime.read_regular(result_path,200000)
        saved=json.loads(raw)
        return dict(state=saved['state'],job_id=saved.get('job_id'),request_id=request_id,
                    manifest_sha256=manifest_sha256,evidence_sha256=runtime.digest(raw))
    # Script comes from private controller storage, never from an Agent upload.
    script_path=case/'job.sh'
    try:
        fd=os.open(script_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400)
    except FileExistsError:
        if runtime.read_regular(script_path,1000000)!=script:
            raise ValueError('Existing batch script differs; inspect rather than overwrite')
    else:
        with os.fdopen(fd,'wb') as output:
            output.write(script); output.flush(); os.fsync(output.fileno())
    argv=[str(binary),'--parsable','--export=NIL',str(script_path)]
    try:
        runtime.write_once(intent_path,dict(request_id=request_id,manifest_sha256=manifest_sha256,
            batch_sha256=runtime.digest(script),argv_sha256=runtime.digest(runtime.canonical(argv)),
            config_sha256=runtime.digest(config_data),at=time.time()))
    except FileExistsError:
        return dict(state='unknown',request_id=request_id,manifest_sha256=manifest_sha256)
    try:
        captured=capture_sbatch(argv,case)
    except OSError as exc:
        captured=dict(returncode=None,failure=type(exc).__name__,stdout='',stderr='')
    state,job='unknown',None
    if not captured['failure'] and captured['returncode']==0:
        value=base64.b64decode(captured['stdout']).decode('ascii',errors='replace').strip()
        if re.fullmatch(r'[1-9][0-9]{0,19}',value):
            state,job='accepted',value
    # Nonzero exit/connection ambiguity is recorded, not silently called rejection.
    receipt=dict(state=state,job_id=job,request_id=request_id,manifest_sha256=manifest_sha256,
                 raw=captured,at=time.time())
    # Larger than ordinary controller receipts, but bounded by capture_sbatch.
    data=runtime.canonical(receipt)
    fd=os.open(result_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400)
    with os.fdopen(fd,'wb') as output:
        output.write(data); output.flush(); os.fsync(output.fileno())
    directory=runtime.directory(case)
    try: os.fsync(directory)
    finally: os.close(directory)
    return dict(state=state,job_id=job,request_id=request_id,manifest_sha256=manifest_sha256,evidence_sha256=runtime.digest(data))


def main():
    parser=argparse.ArgumentParser(description='Submit one approved existing request, once.')
    parser.add_argument('--request-id',required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--root',required=True)
    args=parser.parse_args()
    try:
        result=submit(Path(__file__).resolve().parent/'submission.json',args.request_id,args.manifest_sha256,args.root)
    except Exception as exc:
        print(json.dumps(dict(state='unknown',error_type=type(exc).__name__)))
        return 1
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
