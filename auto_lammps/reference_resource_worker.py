"""HPC-side author resource acquisition. Static reads only; never executes source."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import urllib.request

from .engine_source_worker import private_directory
from .manifest import canonical, sha256
from .potential_acquisition import MeamAcquisition, safe_path, AcquisitionError
from .source_discovery import repository_name
from .slurm_read import _write_new

MAX_SOURCE = 64*1024**2
MAX_BLOB = 16*1024**2
MAX_RESPONSE = 24*1024**2
MAX_TRANSFER = 128*1024**2


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AcquisitionError('github_redirect_rejected')


class PublicGitHubReader:
    """Unauthenticated fixed-repository API; bounded cache and remote receipts."""
    def __init__(self, repository, directory, *, transport=None):
        self.repository = repository_name(repository)
        self.root = directory
        self.cache = {}; self.calls = 0; self.received = 0
        self.transport = transport

    def get(self, endpoint):
        if not re.fullmatch(re.escape('repos/'+self.repository+'/git/')+
                r'(commits/[a-f0-9]{40}|trees/[a-f0-9]{40}\?recursive=1|blobs/[a-f0-9]{40})', endpoint):
            raise AcquisitionError('endpoint_outside_pinned_repository')
        if endpoint in self.cache:
            return self.cache[endpoint]
        if self.calls >= 30:
            raise AcquisitionError('request_limit')
        self.calls += 1
        _write_new(self.root/f'{self.calls:02d}-request.json',dict(endpoint=endpoint))
        try:
            if self.transport is not None:
                result = self.transport.get(endpoint)
                raw = base64.b64decode(result['stdout'], validate=True)
            else:
                if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
                    raise AcquisitionError('real_download_requires_HPC')
                req = urllib.request.Request('https://api.github.com/'+endpoint,
                    headers={'User-Agent':'Auto-LAMMPS-reference-resources',
                             'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28'})
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:
                    if response.status != 200:
                        raise AcquisitionError('github_download_failed')
                    raw = response.read(min(MAX_RESPONSE, MAX_TRANSFER-self.received)+1)
                result = dict(returncode=0,failure='',stdout=base64.b64encode(raw).decode(),stderr='')
            if len(raw) > MAX_RESPONSE or self.received+len(raw) > MAX_TRANSFER:
                raise AcquisitionError('response_budget_exceeded')
            self.received += len(raw)
            _write_new(self.root/f'{self.calls:02d}-response.json',result)
            self.cache[endpoint] = result
            return result
        except (OSError, ValueError, KeyError) as exc:
            _write_new(self.root/f'{self.calls:02d}-failure.json',dict(error_type=type(exc).__name__))
            raise AcquisitionError('github_transport_failed') from exc


def validate_request(request):
    keys = {'id','repository','commit','directory','bundle_sha256','storage_bytes'}
    if not isinstance(request,dict) or set(request) != keys:
        raise AcquisitionError('invalid_remote_resource_request')
    repository_name(request['repository'])
    for key,n in (('id',32),('commit',40),('bundle_sha256',64)):
        if not isinstance(request[key],str) or not re.fullmatch('[a-f0-9]{'+str(n)+'}',request[key]):
            raise AcquisitionError('invalid_request_identity')
    path=request['directory']
    if (not isinstance(path,str) or not Path(path).is_absolute() or '..' in Path(path).parts
            or len(path)>2048 or any(ord(c)<32 for c in path)
            or type(request['storage_bytes']) is not int or not 512*1024**2 <= request['storage_bytes'] <= 16*1024**3):
        raise AcquisitionError('invalid_resource_directory_or_budget')
    return request


def acquire(request, *, transport=None):
    validate_request(request)
    if transport is None and (sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION')):
        raise AcquisitionError('remote_HPC_session_required')
    root=private_directory(request['directory'])
    request_hash=sha256(canonical(request))
    identifier=request['id']; folder=root/identifier
    report=dict(schema_version=1,id=identifier,repository=request['repository'],commit=request['commit'],
        location='hpc',remote_directory=str(folder),state='unknown',request_sha256=request_hash,
        bundle_sha256=request['bundle_sha256'],files=[],failures=[],source_complete=False,
        scientific_validation=False,execution_authorized=False,author_identity_verified=False)
    lock=root/(identifier+'.lock.json')
    try:
        _write_new(lock,request)
    except FileExistsError:
        if lock.is_symlink() or lock.stat().st_size>20000 or json.loads(lock.read_text())!=request:
            raise AcquisitionError('operation_identity_conflict')
        result=folder/'result.json'
        if not result.exists():
            return report
        if result.is_symlink() or result.stat().st_size>500000:
            raise AcquisitionError('invalid_saved_result')
        saved=json.loads(result.read_text())
        if saved.get('request_sha256')!=request_hash:
            raise AcquisitionError('saved_result_identity_conflict')
        return saved
    folder.mkdir(mode=0o700)
    report.update(at=datetime.now(timezone.utc).isoformat(),hostname=socket.gethostname())
    _write_new(folder/'intent.json',request)
    source=folder/'source';source.mkdir(mode=0o700)
    reader=PublicGitHubReader(request['repository'],folder,transport=transport)
    total=0

    def get(endpoint):
        response=reader.get(endpoint)
        if response.get('returncode')!=0 or response.get('failure'):
            raise AcquisitionError('github_request_failed')
        return json.loads(base64.b64decode(response['stdout'],validate=True))

    try:
        repo=request['repository'];commit=request['commit']
        obj=get(f'repos/{repo}/git/commits/{commit}')
        if obj.get('sha')!=commit or not re.fullmatch('[a-f0-9]{40}',obj.get('tree',{}).get('sha','')):
            raise AcquisitionError('commit_identity_mismatch')
        tree_sha=obj['tree']['sha']
        tree=get(f'repos/{repo}/git/trees/{tree_sha}?recursive=1')
        if (tree.get('sha')!=tree_sha or tree.get('truncated') is not False
                or not isinstance(tree.get('tree'),list) or len(tree['tree'])>2000):
            raise AcquisitionError('incomplete_source_tree')
        report['tree_sha1']=tree_sha
        entries=[];seen=set()
        for entry in tree['tree']:
            path=safe_path(entry['path'])
            if path in seen or '.git' in Path(path).parts:
                raise AcquisitionError('unsafe_or_duplicate_source_path')
            seen.add(path)
            if entry.get('type')=='tree' and entry.get('mode')=='040000':
                continue
            if (entry.get('type')!='blob' or entry.get('mode') not in {'100644','100755'}
                    or type(entry.get('size')) is not int or not 0<=entry['size']<=MAX_BLOB
                    or not re.fullmatch('[a-f0-9]{40}',entry.get('sha',''))):
                raise AcquisitionError('unsupported_source_member')
            entries.append(entry)
        if not 1<=len(entries)<=128 or sum(e['size'] for e in entries)>MAX_SOURCE:
            raise AcquisitionError('source_budget_exceeded')
        for entry in entries:
            data=get(f"repos/{repo}/git/blobs/{entry['sha']}")
            if data.get('sha')!=entry['sha'] or data.get('encoding')!='base64':
                raise AcquisitionError('source_blob_identity_mismatch')
            content=base64.b64decode(''.join(data['content'].split()),validate=True)
            actual=hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
            if actual!=entry['sha'] or len(content)!=entry['size'] or data.get('size')!=len(content):
                raise AcquisitionError('source_blob_integrity_mismatch')
            dest=source/entry['path'];dest.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            with dest.open('xb') as output:
                os.fchmod(output.fileno(),0o400);output.write(content);output.flush();os.fsync(output.fileno())
            total+=len(content)
            report['files'].append(dict(path=entry['path'],size=len(content),sha256=sha256(content),
                                       blob_sha1=actual,git_mode=entry['mode']))
        report['source_complete']=True
        # Reuse the existing static parser and model inspector. The cache avoids
        # retrieving the same commit/tree/blob twice; all bytes stay on HPC.
        potentials=MeamAcquisition(folder/'potentials',reader=reader).acquire(repo,commit)
        potentials.update(location='hpc',remote_directory=str(folder/'potentials'/potentials['id']))
        report['potential_acquisition']=potentials
        if potentials['state']!='finished':
            report['failures'].append(dict(reason='potential_acquisition_incomplete'))
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        report['failures'].append(dict(reason=str(exc) if isinstance(exc,AcquisitionError) else type(exc).__name__))
    report.update(state='partial' if report['failures'] else 'finished',source_bytes=total,
                  request_count=reader.calls,received_bytes=reader.received)
    report['inventory_sha256']=sha256(canonical(report['files']))
    _write_new(folder/'result.json',report)
    return report
