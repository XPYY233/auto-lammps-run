"""HPC-only, fixed-tree selective acquisition; repository programs are never run.

The original full-acquisition receipt remains immutable. This separate protocol
can certify only its exact selected regular blobs, never the whole repository.
Unknown operations are queried, not resumed or silently recreated.
"""
import base64
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import io
import zipfile
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.request

from .author_resource_inventory_worker import (
    _directory, _json, _private_directory, _read, _receipt, inspect_cached_tree,
    validate_inventory_request)
from .manifest import canonical, sha256
from .potential_acquisition import AcquisitionError, safe_path
from .reference_resource_worker import (MAX_BLOB, MAX_RESPONSE, MAX_SOURCE,
    MAX_TRANSFER, NoRedirect, PublicGitHubReader)

VERSION = 1
MAX_FILES = 128
BATCH_FILES = 16
MAX_CALLS = 128
MAX_OPERATIONS = 8
MAX_RESUMES = 8
# Reviewed legacy reader: real urllib reads at most maximum_bytes+1.
_LEGACY_READER_SHA256 = 'c2eeae07851bcba8ea9b3daa40b8845debbe894a1caf92dbddb65a1a85e42eeb'
MAX_REPORT = 500_000
MAX_REQUEST = 150_000
FLAGS = dict(scope='reference_only', B_input=False, source_complete=False,
    scientific_validation=False, execution_authorized=False,
    physical_evaluation_performed=False, author_identity_verified=False)


def validate_selection_request(request):
    if (not isinstance(request, dict) or set(request) !=
            {'version', 'id', 'inventory_request', 'inventory_sha256', 'selected'}
            or type(request['version']) is not int or request['version'] != VERSION
            or any(not isinstance(request[k], str) or not re.fullmatch('[a-f0-9]{'+str(n)+'}', request[k])
                   for k, n in (('id', 32), ('inventory_sha256', 64)))):
        raise AcquisitionError('invalid_selected_resource_request')
    validate_inventory_request(request['inventory_request'])
    rows = request['selected']
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise AcquisitionError('selected_file_count_limit')
    seen = set(); total = 0
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'path', 'blob_sha1', 'size', 'git_mode'}
                or not isinstance(row['blob_sha1'], str) or not re.fullmatch('[a-f0-9]{40}', row['blob_sha1'])
                or type(row['size']) is not int or not 0 <= row['size'] <= MAX_BLOB
                or row['git_mode'] not in {'100644', '100755'}):
            raise AcquisitionError('invalid_selected_blob_identity')
        name = safe_path(row['path'])
        if ('.git' in name.split('/') or name in seen
                or any(name.startswith(old+'/') or old.startswith(name+'/') for old in seen)):
            raise AcquisitionError('unsafe_or_duplicate_selected_path')
        seen.add(name); total += row['size']
    if rows != sorted(rows, key=lambda row:row['path']):
        raise AcquisitionError('selected_paths_must_be_canonical')
    if total > MAX_SOURCE or len(canonical(request)) > MAX_REQUEST:
        raise AcquisitionError('selected_source_bytes_limit')
    return request


def selection_identity(request):
    validate_selection_request(request)
    return sha256(canonical({key:request[key] for key in ('inventory_request','inventory_sha256','selected')}))


def validate_selection_inventory(request, inventory):
    validate_selection_request(request)
    if sha256(canonical(inventory)) != request['inventory_sha256']:
        raise AcquisitionError('selected_tree_inventory_digest_mismatch')
    original = request['inventory_request']['acquisition_request']
    if (inventory.get('state') != 'inspected' or inventory.get('acquisition_id') != original['id']
            or inventory.get('repository') != original['repository'] or inventory.get('commit') != original['commit']
            or inventory.get('tree_sha1') != request['inventory_request']['tree_sha1']
            or inventory.get('receipt_sha256') != request['inventory_request']['receipt_sha256']):
        raise AcquisitionError('selected_tree_identity_mismatch')
    for selected in request['selected']:
        found = [row for row in inventory['entries'] if row['path'] == selected['path']]
        if (len(found) != 1 or found[0]['status'] != 'supported_blob'
                or any(found[0][key] != selected[value] for key,value in
                       (('sha','blob_sha1'),('size','size'),('mode','git_mode')))):
            raise AcquisitionError('selected_file_outside_verified_regular_tree')
    return request


def _report(request, state='unknown'):
    original = request['inventory_request']['acquisition_request']
    return dict(schema_version=1, kind='selected_author_resources', id=request['id'],
        acquisition_id=original['id'], repository=original['repository'], commit=original['commit'],
        tree_sha1=request['inventory_request']['tree_sha1'],
        original_receipt_sha256=request['inventory_request']['receipt_sha256'],
        tree_inventory_sha256=request['inventory_sha256'], selection_sha256=selection_identity(request),
        request_sha256=sha256(canonical(request)), location='hpc',
        remote_directory=original['directory']+'/'+original['id']+'/selected-resources/'+request['id'],
        state=state, selected_complete=False, files=[], missing=deepcopy(request['selected']), failures=[],
        source_bytes=0, request_count=0, received_bytes=0, received_budget_bytes=0,
        accounting_mode='unknown', transfer_accounting_complete=False,
        inventory_sha256=sha256(canonical([])), **FLAGS)


def validate_selected_report(request, report):
    expected = _report(request)
    mutable = {'state','selected_complete','files','missing','failures','source_bytes','request_count',
               'received_bytes','received_budget_bytes','accounting_mode',
               'transfer_accounting_complete','inventory_sha256'}
    legacy=set(expected)-{'received_budget_bytes','accounting_mode'}
    if (not isinstance(report, dict) or set(report) not in (set(expected),legacy)
            or len(canonical(report)) > MAX_REPORT
            or any(report[k] != expected[k] or type(report[k]) is not type(expected[k]) for k in set(expected)-mutable)
            or report['state'] not in {'selected_complete','partial','unknown'}
            or type(report['selected_complete']) is not bool
            or type(report['transfer_accounting_complete']) is not bool
            or type(report['request_count']) is not int or not 0 <= report['request_count'] <= MAX_CALLS
            or type(report['received_bytes']) is not int or not 0 <= report['received_bytes'] <= MAX_TRANSFER
            or type(report['source_bytes']) is not int or not 0 <= report['source_bytes'] <= MAX_SOURCE
            or not isinstance(report['files'], list) or len(report['files']) > MAX_FILES
            or not isinstance(report['failures'], list) or len(report['failures']) > 1):
        raise AcquisitionError('invalid_selected_resource_receipt')
    budget=report.get('received_budget_bytes',report['received_bytes'])
    mode=report.get('accounting_mode','exact' if report['transfer_accounting_complete'] else 'unknown')
    if (type(budget) is not int or not report['received_bytes']<=budget<=MAX_TRANSFER
            or mode not in {'exact','conservative_upper_bound','unknown'}
            or (mode=='exact' and (not report['transfer_accounting_complete'] or budget!=report['received_bytes']))
            or (mode=='conservative_upper_bound' and (report['transfer_accounting_complete'] or budget==report['received_bytes']))):
        raise AcquisitionError('invalid_selected_transfer_accounting')
    selected = {row['path']:row for row in request['selected']}; seen=set()
    for row in report['files']:
        if (not isinstance(row,dict) or set(row) != {'path','blob_sha1','size','git_mode','sha256'}
                or row.get('path') not in selected or row['path'] in seen
                or any(row[k] != selected[row['path']][k] for k in selected[row['path']])
                or not isinstance(row['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',row['sha256'])):
            raise AcquisitionError('selected_receipt_file_identity_mismatch')
        seen.add(row['path'])
    if (report['files'] != sorted(report['files'],key=lambda row:row['path'])
            or report['missing'] != [row for row in request['selected'] if row['path'] not in seen]
            or report['source_bytes'] != sum(row['size'] for row in report['files'])
            or report['inventory_sha256'] != sha256(canonical(report['files']))
            or report['selected_complete'] != (report['state']=='selected_complete')
            or (report['selected_complete'] and (report['missing'] or report['failures']))
            or (report['state']=='partial' and not report['failures'])):
        raise AcquisitionError('selected_receipt_completeness_mismatch')
    for failure in report['failures']:
        if (not isinstance(failure,dict) or set(failure) != {'path','reason'}
                or failure['path'] not in selected or not isinstance(failure['reason'],str)
                or not re.fullmatch('[A-Za-z0-9_]{1,100}',failure['reason'])):
            raise AcquisitionError('invalid_selected_failure')
    return report


def _mkdir(parent, name):
    try: os.mkdir(name,0o700,dir_fd=parent)
    except FileExistsError: pass
    descriptor=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    try: _private_directory(descriptor)
    except BaseException: os.close(descriptor); raise
    return descriptor


def _write(directory, name, value):
    data=canonical(value)
    descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=directory)
    with os.fdopen(descriptor,'wb') as output:
        os.fchmod(output.fileno(),0o400);output.write(data);output.flush();os.fsync(output.fileno())


@contextmanager
def _control(directory):
    descriptor=os.open('control.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
    try:
        info=os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077):
            raise AcquisitionError('unsafe_selected_control_lock')
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        yield
    finally: os.close(descriptor)


def _reservation(request):
    # Original bytes plus base64 API caches/receipts; reserve before any GET.
    return 32*1024**2 + 3*sum(row['size'] for row in request['selected']) + 8192*len(request['selected'])


def _register(parent, request, *, existing_bytes):
    name=selection_identity(request)+'.binding.json'
    with _control(parent):
        try:
            old=_json(_read(parent,name,MAX_REQUEST+1024))
        except FileNotFoundError: old=None
        if old is not None:
            if old != request: raise AcquisitionError('selected_set_already_bound_to_original_operation')
            return False
        bindings=[item for item in os.listdir(parent) if item.endswith('.binding.json')]
        if len(bindings)>=MAX_OPERATIONS: raise AcquisitionError('selected_operation_count_limit')
        used=0
        for item in bindings:
            if not re.fullmatch(r'[a-f0-9]{64}\.binding\.json',item):
                raise AcquisitionError('unsafe_selected_binding')
            old=_json(_read(parent,item,MAX_REQUEST+1024)); validate_selection_request(old)
            if item!=selection_identity(old)+'.binding.json':
                raise AcquisitionError('selected_binding_digest_mismatch')
            if old['id']==request['id']:
                raise AcquisitionError('selected_operation_identity_conflict')
            if old['inventory_request'] != request['inventory_request']:
                raise AcquisitionError('selected_original_acquisition_changed')
            used+=_reservation(old)
        original=request['inventory_request']['acquisition_request']
        if existing_bytes+used+_reservation(request)>original['storage_bytes']:
            raise AcquisitionError('selected_storage_reservation_limit')
        _write(parent,name,request)
        return True


def _source_file(directory, row, data=None):
    current=os.dup(directory)
    try:
        parts=row['path'].split('/')
        for part in parts[:-1]:
            if data is None:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
                try: _private_directory(child)
                except BaseException: os.close(child); raise
            else: child=_mkdir(current,part)
            os.close(current);current=child
        if data is not None:
            descriptor=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=current)
            with os.fdopen(descriptor,'wb') as output:
                os.fchmod(output.fileno(),0o400);output.write(data);output.flush();os.fsync(output.fileno())
        descriptor=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
        try:
            before=os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1
                    or stat.S_IMODE(before.st_mode)!=0o400 or before.st_size!=row['size']):
                raise AcquisitionError('selected_original_file_type_or_size_changed')
            with os.fdopen(os.dup(descriptor),'rb') as source: raw=source.read(row['size']+1)
            after=os.fstat(descriptor)
            if (len(raw)!=row['size'] or any(getattr(before,k)!=getattr(after,k) for k in
                    ('st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_nlink'))
                    or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=row['blob_sha1']
                    or (row.get('sha256') is not None and sha256(raw)!=row['sha256'])):
                raise AcquisitionError('selected_original_file_integrity_changed')
            return sha256(raw)
        finally: os.close(descriptor)
    finally: os.close(current)


def _saved(parent, request):
    try: folder=os.open(request['id'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    except FileNotFoundError: return _report(request)
    source=None
    try:
        _private_directory(folder)
        if _json(_read(folder,'intent.json',MAX_REQUEST)) != request:
            raise AcquisitionError('selected_operation_identity_conflict')
        resumes=sorted(name for name in os.listdir(folder) if re.fullmatch(r'resume-[0-9]{3}-intent\.json',name))
        if len(resumes)>MAX_RESUMES:raise AcquisitionError('selected_resume_limit')
        final='result.json' if not resumes else resumes[-1].replace('-intent.json','-result.json')
        try: report=validate_selected_report(request,_json(_read(folder,final,MAX_REPORT)))
        except FileNotFoundError:
            progress=[name for name in os.listdir(folder) if re.fullmatch(r'progress-[0-9]{3}\.json',name)]
            if len(progress)>MAX_FILES: raise AcquisitionError('selected_progress_limit')
            report=validate_selected_report(request,_json(_read(folder,sorted(progress)[-1],MAX_REPORT))) if progress else _report(request)
            report=deepcopy(report);report['state']='unknown';report['selected_complete']=False
            accounting=_fetch_accounting(folder)
            report.update(**accounting)
            validate_selected_report(request,report)
        if report['files']:
            source=os.open('source',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=folder);_private_directory(source)
            for row in report['files']: _source_file(source,row)
        return report
    finally:
        if source is not None: os.close(source)
        os.close(folder)


def blob_response_limit(size):
    """Base64 plus JSON-escaped LF/CRLF every 60 characters and metadata.

    GitHub's content string contains wrapping; JSON encodes LF as two bytes
    and CRLF as four. The fixed 8KiB remainder bounds all non-content fields.
    """
    if type(size) is not int or not 0<=size<=MAX_BLOB:raise AcquisitionError('invalid_selected_blob_size')
    encoded=4*((size+2)//3)
    return encoded+4*((encoded+59)//60)+8192


def _exists(folder,name):
    try:os.stat(name,dir_fd=folder,follow_symlinks=False);return True
    except FileNotFoundError:return False


def _fetch_accounting(folder):
    attempted=sorted(name for name in os.listdir(folder) if re.fullmatch(r'fetch-[0-9]{3}-intent\.json',name))
    if len(attempted)>MAX_CALLS:raise AcquisitionError('selected_request_limit')
    received=budget=0;complete=True;estimated=False
    for index,name in enumerate(attempted,1):
        if name!=f'fetch-{index:03d}-intent.json':raise AcquisitionError('selected_fetch_accounting_gap')
        intent=_json(_read(folder,name,2048))
        if (not isinstance(intent,dict) or set(intent)!={'endpoint','maximum_bytes'}
                or not isinstance(intent['endpoint'],str) or type(intent['maximum_bytes']) is not int
                or not 0<intent['maximum_bytes']<=MAX_RESPONSE):
            raise AcquisitionError('selected_fetch_accounting_invalid')
        try:outcome=_json(_read(folder,f'fetch-{index:03d}-result.json',1024))
        except FileNotFoundError:
            try:upper=_json(_read(folder,f'fetch-{index:03d}-upper-bound.json',1024))
            except FileNotFoundError:complete=False;continue
            if (not isinstance(upper,dict) or set(upper)!={'bytes_upper_bound','maximum_bytes',
                    'reason','legacy_reader_sha256','legacy_result_sha256'}
                    or upper['reason']!='legacy_selected_transfer_envelope'
                    or upper['legacy_reader_sha256']!=_LEGACY_READER_SHA256
                    or upper['maximum_bytes']!=intent['maximum_bytes']
                    or type(upper['bytes_upper_bound']) is not int
                    or upper['bytes_upper_bound']!=intent['maximum_bytes']+1
                    or upper['bytes_upper_bound']>MAX_RESPONSE
                    or upper['legacy_result_sha256']!=sha256(_read(folder,'result.json',MAX_REPORT))):
                raise AcquisitionError('selected_fetch_accounting_invalid')
            budget+=upper['bytes_upper_bound'];complete=False;estimated=True;continue
        if (not isinstance(outcome,dict) or set(outcome)!={'bytes','sha256'}
                or type(outcome['bytes']) is not int or not 0<=outcome['bytes']<=MAX_RESPONSE
                or not isinstance(outcome['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',outcome['sha256'])):
            raise AcquisitionError('selected_fetch_accounting_invalid')
        received+=outcome['bytes'];budget+=outcome['bytes']
    if budget>MAX_TRANSFER:raise AcquisitionError('selected_transfer_budget_exceeded')
    unresolved=any(not _exists(folder,f'fetch-{index:03d}-result.json')
                   and not _exists(folder,f'fetch-{index:03d}-upper-bound.json') for index in range(1,len(attempted)+1))
    mode='unknown' if unresolved else 'conservative_upper_bound' if estimated else 'exact'
    return dict(request_count=len(attempted),received_bytes=received,received_budget_bytes=budget,
                transfer_accounting_complete=complete,accounting_mode=mode)


def _operation_storage_bytes(folder):
    """Bound bytes in the original reservation, including immutable retries."""
    stack=[os.dup(folder)];total=count=0
    try:
        while stack:
            current=stack.pop()
            try:
                for name in os.listdir(current):
                    count+=1
                    if count>4096:raise AcquisitionError('selected_storage_inventory_limit')
                    info=os.stat(name,dir_fd=current,follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if len(stack)>=128:raise AcquisitionError('selected_storage_inventory_limit')
                        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
                        try:_private_directory(child)
                        except BaseException:os.close(child);raise
                        stack.append(child)
                    elif (stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                            and info.st_nlink==1 and not info.st_mode&0o077):total+=info.st_size
                    else:raise AcquisitionError('unsafe_selected_storage_member')
            finally:os.close(current)
        return total
    finally:
        for current in stack:os.close(current)


def _resume_accounting(root,folder,request,report,previous_bundle_sha256):
    accounting=_fetch_accounting(folder)
    if accounting['accounting_mode']!='unknown':return accounting
    # Only the reviewed legacy size-envelope failure ended in a known partial
    # result. A missing/uncertain transport outcome cannot use this exception.
    if (report['state']!='partial' or len(report['failures'])!=1
            or report['failures'][0]['reason']!='selected_transfer_budget_exceeded'
            or not isinstance(previous_bundle_sha256,str)
            or not re.fullmatch('[a-f0-9]{64}',previous_bundle_sha256)):
        raise AcquisitionError('selected_transfer_accounting_unknown')
    old_bytes=_read(folder,'result.json',MAX_REPORT)
    old=validate_selected_report(request,_json(old_bytes))
    if (old['state']!='partial' or old['failures']!=report['failures']
            or old['files']!=report['files'] or old['request_count']!=accounting['request_count']):
        raise AcquisitionError('selected_transfer_accounting_unknown')
    bundle=_read(root,'reader-tools-'+previous_bundle_sha256+'.zip',1024**2)
    if sha256(bundle)!=previous_bundle_sha256:raise AcquisitionError('selected_legacy_reader_unverified')
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        info=archive.getinfo('auto_lammps/reference_selected_resource_worker.py')
        if info.file_size>65536 or sha256(archive.read(info))!=_LEGACY_READER_SHA256:
            raise AcquisitionError('selected_legacy_reader_unverified')
    index=accounting['request_count']
    missing=[i for i in range(1,index+1) if not _exists(folder,f'fetch-{i:03d}-result.json')
             and not _exists(folder,f'fetch-{i:03d}-upper-bound.json')]
    if missing!=[index]:raise AcquisitionError('selected_transfer_accounting_unknown')
    intent=_json(_read(folder,f'fetch-{index:03d}-intent.json',2048))
    selected=next((row for row in request['selected'] if row['path']==old['failures'][0]['path']),None)
    endpoint='repos/'+request['inventory_request']['acquisition_request']['repository']+'/git/blobs/'
    if (selected is None or intent['endpoint']!=endpoint+selected['blob_sha1']
            or intent['maximum_bytes']!=min(MAX_RESPONSE,MAX_TRANSFER-accounting['received_bytes'],
                4*((selected['size']+2)//3)+8192)
            or intent['maximum_bytes']>=MAX_RESPONSE
            or accounting['received_budget_bytes']+intent['maximum_bytes']+1>MAX_TRANSFER):
        raise AcquisitionError('selected_transfer_budget_exceeded')
    _write(folder,f'fetch-{index:03d}-upper-bound.json',dict(
        bytes_upper_bound=intent['maximum_bytes']+1,maximum_bytes=intent['maximum_bytes'],
        reason='legacy_selected_transfer_envelope',legacy_reader_sha256=_LEGACY_READER_SHA256,
        legacy_result_sha256=sha256(old_bytes)))
    return _fetch_accounting(folder)


class _BoundedBlobTransport:
    def __init__(self,request,folder,*,transport=None,accounting=None):
        self.request,self.folder,self.transport=request,folder,transport
        accounting=accounting or {}
        self.calls=accounting.get('request_count',0);self.received=accounting.get('received_bytes',0)
        self.budget=accounting.get('received_budget_bytes',self.received)
        self.complete=accounting.get('transfer_accounting_complete',True)
        self.mode=accounting.get('accounting_mode','exact');self.cache={};self.failure=None
        self.sizes={row['blob_sha1']:row['size'] for row in request['selected']}

    def get(self,endpoint):
        try: return self._get(endpoint)
        except AcquisitionError as error:
            self.failure=str(error)
            raise

    def _get(self,endpoint):
        prefix='repos/'+self.request['inventory_request']['acquisition_request']['repository']+'/git/blobs/'
        if not endpoint.startswith(prefix) or endpoint[len(prefix):] not in self.sizes:
            raise AcquisitionError('blob_endpoint_outside_selected_repository')
        if endpoint in self.cache: return self.cache[endpoint]
        if self.calls>=MAX_CALLS: raise AcquisitionError('selected_request_limit')
        limit=blob_response_limit(self.sizes[endpoint[len(prefix):]])
        if limit>=MAX_RESPONSE or limit+1>MAX_TRANSFER-self.budget:
            raise AcquisitionError('selected_transfer_budget_exceeded')
        required=4*((limit+3)//3)+self.sizes[endpoint[len(prefix):]]+2*MAX_REPORT+8192
        if _operation_storage_bytes(self.folder)+required>_reservation(self.request):
            raise AcquisitionError('selected_storage_reservation_limit')
        self.calls+=1
        _write(self.folder,f'fetch-{self.calls:03d}-intent.json',dict(endpoint=endpoint,maximum_bytes=limit))
        try:
            if self.transport is None:
                req=urllib.request.Request('https://api.github.com/'+endpoint,headers={
                    'User-Agent':'Auto-LAMMPS-selected-reference','Accept':'application/vnd.github+json',
                    'X-GitHub-Api-Version':'2022-11-28'})
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:
                    if response.status!=200: raise AcquisitionError('selected_github_request_failed')
                    raw=response.read(limit+1)
                result=dict(returncode=0,failure='',stdout=base64.b64encode(raw).decode(),stderr='')
            else:
                result=self.transport.get(endpoint)
                if result.get('returncode')!=0 or result.get('failure') or result.get('stderr'):
                    raise AcquisitionError('selected_github_request_failed')
                raw=base64.b64decode(result['stdout'],validate=True)
            if len(raw)>limit+1:
                self.complete=False;self.mode='unknown'
                raise AcquisitionError('selected_transfer_budget_exceeded')
            self.received+=len(raw);self.budget+=len(raw)
            _write(self.folder,f'fetch-{self.calls:03d}-result.json',dict(bytes=len(raw),sha256=sha256(raw)))
            if len(raw)>limit:raise AcquisitionError('selected_transfer_budget_exceeded')
            result=dict(returncode=0,failure='',stdout=base64.b64encode(raw).decode(),stderr='')
            self.cache[endpoint]=result
            return result
        except (OSError,ValueError,KeyError,TypeError):
            if not isinstance(sys.exc_info()[1],AcquisitionError) or not (
                    self.calls and _exists(self.folder,f'fetch-{self.calls:03d}-result.json')):
                self.complete=False;self.mode='unknown'
            raise


def selected_resources(request, *, action, transport=None, previous_bundle_sha256=None):
    """Acquire once, or reconcile only; all original bytes remain on HPC."""
    validate_selection_request(request)
    if action not in {'acquire','reconcile','resume'}: raise AcquisitionError('invalid_selected_resource_action')
    if sys.platform!='linux' or not os.environ.get('SSH_CONNECTION'):
        raise AcquisitionError('selected_acquisition_requires_HPC')
    inventory=inspect_cached_tree(request['inventory_request'])
    validate_selection_inventory(request,inventory)
    original=request['inventory_request']['acquisition_request']
    root=original_fd=parent=folder=source=execution_lock=None
    try:
        root=_directory(original['directory'])
        original_fd=os.open(original['id'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root);_private_directory(original_fd)
        if action=='reconcile':
            try: parent=os.open('selected-resources',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=original_fd)
            except FileNotFoundError: return _report(request)
            _private_directory(parent)
            try: binding=_json(_read(parent,selection_identity(request)+'.binding.json',MAX_REQUEST+1024))
            except FileNotFoundError: return _report(request)
            if binding!=request: raise AcquisitionError('selected_set_already_bound_to_original_operation')
            return _saved(parent,request)
        parent=_mkdir(original_fd,'selected-resources')
        original_receipt=_receipt(_json(_read(original_fd,'result.json',500000)),request['inventory_request'])
        # Include the unchanged acquisition's originals and encoded API caches,
        # rather than treating selective acquisition as a fresh storage quota.
        existing_bytes=original_receipt['source_bytes']+2*original_receipt['received_bytes']+1024**2
        if action=='resume':
            try:saved_binding=_json(_read(parent,selection_identity(request)+'.binding.json',MAX_REQUEST+1024))
            except FileNotFoundError:raise AcquisitionError('selected_resume_requires_existing_operation') from None
            if saved_binding!=request:raise AcquisitionError('selected_set_already_bound_to_original_operation')
            registered=False
        else:
            registered=_register(parent,request,existing_bytes=existing_bytes)
            if not registered:return _saved(parent,request)
        folder=_mkdir(parent,request['id'])
        execution_lock=os.open('execution.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=folder)
        lock_info=os.fstat(execution_lock)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid!=os.getuid()
                or lock_info.st_nlink!=1 or lock_info.st_mode&0o077):
            raise AcquisitionError('unsafe_selected_control_lock')
        try:fcntl.flock(execution_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise AcquisitionError('selected_operation_busy') from None
        final='result.json';accounting=None
        if registered:
            _write(folder,'intent.json',request);report=_report(request)
        else:
            report=_saved(parent,request)
            if report['state']!='partial':return report
            accounting=_resume_accounting(root,folder,request,report,previous_bundle_sha256)
            resumptions=sorted(name for name in os.listdir(folder) if re.fullmatch(r'resume-[0-9]{3}-intent\.json',name))
            if len(resumptions)>=MAX_RESUMES:raise AcquisitionError('selected_resume_limit')
            prefix=f'resume-{len(resumptions)+1:03d}'
            _write(folder,prefix+'-intent.json',dict(request_sha256=sha256(canonical(request)),
                previous_receipt_sha256=sha256(canonical(report)),previous_bundle_sha256=previous_bundle_sha256))
            final=prefix+'-result.json';report=deepcopy(report);report.update(failures=[],state='unknown',selected_complete=False)
        source=_mkdir(folder,'source')
        bounded=_BoundedBlobTransport(request,folder,transport=transport,accounting=accounting)
        for start in range(0,len(request['selected']),BATCH_FILES):
            name=(f'batch-{start//BATCH_FILES:02d}' if final=='result.json' else f'{final[:-12]}-batch-{start//BATCH_FILES:02d}');batch=_mkdir(folder,name);os.close(batch)
            batch_path=Path(report['remote_directory'])/name
            reader=PublicGitHubReader(original['repository'],batch_path,transport=bounded)
            for row in request['selected'][start:start+BATCH_FILES]:
                if any(saved['path']==row['path'] for saved in report['files']):continue
                try:
                    response=reader.get('repos/'+original['repository']+'/git/blobs/'+row['blob_sha1'])
                    blob=_json(base64.b64decode(response['stdout'],validate=True))
                    if (not isinstance(blob,dict) or blob.get('sha')!=row['blob_sha1']
                            or blob.get('encoding')!='base64' or type(blob.get('size')) is not int
                            or blob['size']!=row['size'] or not isinstance(blob.get('content'),str)):
                        raise AcquisitionError('selected_blob_identity_mismatch')
                    data=base64.b64decode(''.join(blob['content'].split()),validate=True)
                    if len(data)!=row['size'] or hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()!=row['blob_sha1']:
                        raise AcquisitionError('selected_blob_integrity_mismatch')
                    checksum=_source_file(source,row,data)
                    report['files'].append(dict(row,sha256=checksum))
                    report.update(source_bytes=sum(item['size'] for item in report['files']),
                        request_count=bounded.calls,received_bytes=bounded.received,
                        received_budget_bytes=bounded.budget,accounting_mode=bounded.mode,
                        transfer_accounting_complete=bounded.complete,
                        inventory_sha256=sha256(canonical(report['files'])),
                        missing=[item for item in request['selected'] if item['path'] not in {entry['path'] for entry in report['files']}])
                    _write(folder,f'progress-{len(report["files"]):03d}.json',report)
                except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
                    reason=bounded.failure or (str(error) if isinstance(error,AcquisitionError) else type(error).__name__)
                    if not re.fullmatch('[A-Za-z0-9_]{1,100}',reason): reason='selected_blob_rejected'
                    report['failures']=[dict(path=row['path'],reason=reason)]
                    break
            if report['failures']: break
        report.update(state=('unknown' if report['failures'] and bounded.mode=='unknown'
            else 'partial' if report['failures'] else 'selected_complete'),
            selected_complete=not report['failures'],request_count=bounded.calls,received_bytes=bounded.received,
            received_budget_bytes=bounded.budget,accounting_mode=bounded.mode,
            transfer_accounting_complete=bounded.complete)
        validate_selected_report(request,report);_write(folder,final,report)
        return report
    finally:
        for descriptor in (execution_lock,source,folder,parent,original_fd,root):
            if descriptor is not None: os.close(descriptor)
