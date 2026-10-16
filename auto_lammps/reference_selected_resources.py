"""Trusted selective reference-resource adapter; original bytes stay on HPC.

The application injects a saved acquisition and its verified cached tree. Model
output may select exact listed blob identities, but cannot set a repository,
version, URL, local path, remote root or command. This adapter never runs source.
"""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

from . import reference_selected_resource_worker as worker
from .author_resource_inventory_worker import _receipt
from .author_source_reader import BOOTSTRAP as _READER_BOOTSTRAP
from .manifest import canonical, private_directory, sha256
from .potential_acquisition import AcquisitionError
from .remote_resources import (_bind_local_intent, _bound_transport_command,
    _saved_transport_binding, _transport_failure, RemoteResources, tool_bundle)
from .runtime_launcher import read_regular
from .slurm_read import _capture, _write_new


BOOTSTRAP = (_READER_BOOTSTRAP
    .replace("root=PurePosixPath(request['remote_root'])", "remote_root=request['inventory_request']['acquisition_request']['directory'];root=PurePosixPath(remote_root)")
    .replace("str(root)!=request['remote_root']", "str(root)!=remote_root")
    .replace('from auto_lammps.author_source_worker import read_source_report',
             'from auto_lammps.reference_selected_resource_worker import selected_resources')
    .replace('response=read_source_report(request)', "response=selected_resources(request,action=envelope['action'])"))


def selected_tool_bundle():
    """Reuse existing reviewed metadata tooling and add only this static worker."""
    original=tool_bundle();stream=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as output:
        contents={name:source.read(name) for name in source.namelist()}
        contents['auto_lammps/reference_selected_resource_worker.py']=Path(worker.__file__).read_bytes()
        for name,data in sorted(contents.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100400<<16;output.writestr(info,data)
    result=stream.getvalue()
    if len(result)>1024**2: raise AcquisitionError('selected_tool_bundle_size_limit')
    return result


class ReferenceSelectedResources:
    def __init__(self,audit_directory,*,host_alias,acquisition_request,acquisition_receipt,
                 inventory,transport,capture=_capture):
        if not isinstance(host_alias,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',host_alias):
            raise AcquisitionError('explicit_saved_HPC_selection_policy_required')
        original=deepcopy(acquisition_request)
        self.inventory_request=dict(version=1,acquisition_request=original,
            receipt_sha256=sha256(canonical(acquisition_receipt)),tree_sha1=inventory.get('tree_sha1'))
        _receipt(acquisition_receipt,self.inventory_request)
        RemoteResources.validate_tree_report(self.inventory_request,inventory)
        self.inventory=deepcopy(inventory);self.host_alias=host_alias;self.transport=transport;self.capture=capture
        self.remote_directory=original['directory']
        self.transport_binding=_saved_transport_binding(transport,host_alias,self.remote_directory)
        if self.transport_binding is None: raise AcquisitionError('selected_saved_HPC_transport_required')
        self.root=private_directory(audit_directory)

    def selection_request(self,operation_id,selections):
        """Pure admission of exact application-AI selections from the pinned tree."""
        if not isinstance(selections,list): raise AcquisitionError('bounded_explicit_selected_files_required')
        if any(not isinstance(row,dict) or not isinstance(row.get('path'),str) for row in selections):
            raise AcquisitionError('invalid_selected_blob_identity')
        request=dict(version=1,id=operation_id,inventory_request=deepcopy(self.inventory_request),
            inventory_sha256=sha256(canonical(self.inventory)),selected=sorted(deepcopy(selections),key=lambda row:row['path']))
        worker.validate_selection_inventory(request,self.inventory)
        return request

    def _intent(self,request):
        return dict(host_alias=self.host_alias,request=request,transport_binding=deepcopy(self.transport_binding),
                    bundle_sha256=sha256(selected_tool_bundle()))

    def _saved(self,operation_id):
        if not isinstance(operation_id,str) or not re.fullmatch('[a-f0-9]{32}',operation_id):
            raise AcquisitionError('invalid_selected_operation_identity')
        folder=self.root/operation_id
        intent=json.loads(read_regular(folder/'intent.json',worker.MAX_REQUEST+2048,private=True))
        request=intent.get('request')
        worker.validate_selection_inventory(request,self.inventory)
        # A helper update can reconcile the original immutable request without
        # retroactively changing which helper was used for its first dispatch.
        expected=self._intent(request)
        if (not isinstance(intent,dict) or set(intent)!=set(expected)
                or any(intent.get(k)!=expected[k] for k in ('host_alias','request','transport_binding'))
                or not isinstance(intent.get('bundle_sha256'),str)
                or not re.fullmatch('[a-f0-9]{64}',intent['bundle_sha256'])
                or request['id']!=operation_id or request['inventory_request']!=self.inventory_request):
            raise AcquisitionError('selected_operation_identity_changed')
        return folder,request

    def status(self,operation_id):
        folder,request=self._saved(operation_id)
        records=sorted(folder.glob('*-result.json'))
        if len(records)>32: raise AcquisitionError('selected_observation_limit')
        if not records: return worker._report(request)
        # Sequence timestamps are immutable local observation ordering, not a
        # new resource request or a claim that an unknown download is failed.
        latest=max(records,key=lambda path:path.name)
        return worker.validate_selected_report(request,json.loads(read_regular(latest,worker.MAX_REPORT,private=True)))

    def acquire(self,operation_id,selections):
        request=self.selection_request(operation_id,selections);folder=private_directory(self.root/operation_id)
        intent=self._intent(request)
        _bind_local_intent(folder/'intent.json',intent)
        _bind_local_intent(self.root/(worker.selection_identity(request)+'.binding.json'),
                           dict(id=operation_id,request_sha256=sha256(canonical(request))))
        marker=folder/'dispatch.json'
        if marker.exists() or marker.is_symlink():
            if json.loads(read_regular(marker,1024,private=True)) != dict(request_sha256=sha256(canonical(request))):
                raise AcquisitionError('selected_dispatch_identity_conflict')
            return self.status(operation_id)
        try: _write_new(marker,dict(request_sha256=sha256(canonical(request))))
        except FileExistsError: return self.status(operation_id)
        return self._observe(request,folder,'acquire')

    def reconcile(self,operation_id):
        """Read the original remote operation only. Never restart its downloads."""
        folder,request=self._saved(operation_id)
        return self._observe(request,folder,'reconcile')

    def _observe(self,request,folder,action):
        observations=list(folder.glob('*-start.json'))
        if len(observations)>=32: raise AcquisitionError('selected_observation_limit')
        # Monotonic lexical sequence permits stable local status after restart.
        sequence=len(observations)+1;identity=f'{sequence:03d}-{uuid.uuid4().hex}'
        bundle=selected_tool_bundle()
        _write_new(folder/(identity+'-start.json'),dict(action=action,request_sha256=sha256(canonical(request)),
            bundle_sha256=sha256(bundle)))
        command=_bound_transport_command(self.transport,self.transport_binding,self.host_alias,
            self.remote_directory,['python3','-I','-c',BOOTSTRAP])
        data=canonical(dict(action=action,request=request,bundle=base64.b64encode(bundle).decode(),bundle_sha256=sha256(bundle)))
        try:
            result=self.capture(command,timeout=2700 if action=='acquire' else 60,max_bytes=worker.MAX_REPORT,
                input_chunks=[data[index:index+65536] for index in range(0,len(data),65536)])
        except OSError as error:
            result=dict(returncode=None,failure=type(error).__name__,stdout='',stderr='')
        failure=_transport_failure(result)
        summary=dict(action=action,returncode=result.get('returncode'),failure=failure,raw_output_persisted=False)
        for key in ('stdout','stderr'):
            raw=str(result.get(key) or '').encode();summary[key+'_sha256']=sha256(raw);summary[key+'_bytes']=len(raw)
        _write_new(folder/(identity+'-transport.json'),summary)
        report=worker._report(request)
        if result.get('returncode')==0 and not failure:
            try:
                observed=json.loads(base64.b64decode(result['stdout'],validate=True))
                report=worker.validate_selected_report(request,observed)
            except (ValueError,KeyError,TypeError,RecursionError): pass
        _write_new(folder/(identity+'-result.json'),report)
        return report
