"""Selective reference acquisition, synthetic cache/HTTP only; no real network."""
import base64
from copy import deepcopy
from contextlib import contextmanager,redirect_stdout,redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from auto_lammps import reference_selected_resource_worker as worker
from auto_lammps.hpc_transport import SavedHPCTransport
from auto_lammps.manifest import canonical,sha256
from auto_lammps.potential_acquisition import AcquisitionError
from auto_lammps.reference_resource_worker import acquire,MAX_BLOB
from auto_lammps.reference_selected_resources import ReferenceSelectedResources,BOOTSTRAP,selected_tool_bundle
from test_author_resource_inventory import MetadataReader,entry
from test_potential_acquisition import TREE
from test_source_discovery import NAME,COMMIT,response


def blob_hash(data): return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


class SavedTransport(SavedHPCTransport):
    def __init__(self,root):
        self.identity=dict(revision=1,host_alias='offline-hpc',profile=dict(work_directory=str(root)))
    def _identity(self): return self.identity
    def command(self,host,remote):
        if host!=self.identity['host_alias']: raise ValueError('wrong host')
        return ['ssh','offline-only',shlex.join(remote)]


class Blobs:
    def __init__(self,contents): self.contents=contents;self.calls=[];self.change=None
    def get(self,endpoint):
        self.calls.append(endpoint)
        identity=endpoint.rsplit('/',1)[-1];data=self.contents[identity]
        value=dict(sha=identity,encoding='base64',size=len(data),content=base64.b64encode(data).decode())
        if self.change is not None: self.change(value,len(self.calls))
        return response(value)


class SelectedResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.hpc=self.root/'hpc';self.hpc.mkdir(mode=0o700)
        self.original=dict(id='a'*32,repository=NAME,commit=COMMIT,directory=str(self.hpc),
            bundle_sha256='b'*64,storage_bytes=512*1024**2)
        self.data=getattr(self,'synthetic_contents',None) or {f'code/group/file-{index:03d}.txt':f'synthetic original {index}\n'.encode() for index in range(40)}
        self.entries=[entry(path,size=len(data),identity=blob_hash(data)) for path,data in self.data.items()]
        self.entries.extend([entry('large-unselected.log',size=MAX_BLOB+1),entry('linked',mode='120000'),
            entry('code',kind='tree',mode='040000',size=None)])
        self.old=acquire(self.original,transport=MetadataReader(self.entries))
        self.assertFalse(self.old['source_complete']);self.assertEqual(self.old['state'],'partial')
        self.original_bytes=(Path(self.old['remote_directory'])/'result.json').read_bytes()
        self.inventory_request=dict(version=1,acquisition_request=deepcopy(self.original),
            receipt_sha256=sha256(canonical(self.old)),tree_sha1=TREE)
        self.blobs=Blobs({blob_hash(data):data for data in self.data.values()})
        self.transport=SavedTransport(self.root);self.actions=[]
        with self.hpc_session(): self.inventory=worker.inspect_cached_tree(self.inventory_request)
        self.service=ReferenceSelectedResources(self.root/'audit',host_alias='offline-hpc',
            acquisition_request=self.original,acquisition_receipt=self.old,inventory=self.inventory,
            transport=self.transport,capture=self.capture)

    @contextmanager
    def hpc_session(self):
        with patch('auto_lammps.reference_selected_resource_worker.sys.platform','linux'), \
                patch.dict(os.environ,{'SSH_CONNECTION':'synthetic offline'}): yield

    def choices(self,count=1):
        return [dict(path=path,blob_sha1=blob_hash(data),size=len(data),git_mode='100644')
                for path,data in list(self.data.items())[:count]]

    def request(self,count=1,identifier='c'*32): return self.service.selection_request(identifier,self.choices(count))

    def run_worker(self,request=None,action='acquire',**kwargs):
        with self.hpc_session():
            return worker.selected_resources(request or self.request(),action=action,transport=self.blobs,**kwargs)

    def capture(self,command,**options):
        envelope=json.loads(b''.join(options['input_chunks']));self.actions.append(envelope['action'])
        self.assertEqual(command[0],'ssh');self.assertIn('-I',command[-1]);self.assertEqual(options['max_bytes'],worker.MAX_REPORT)
        bundle=base64.b64decode(envelope['bundle']);self.assertEqual(sha256(bundle),envelope['bundle_sha256'])
        report=self.run_worker(envelope['request'],envelope['action'],
            previous_bundle_sha256=envelope.get('previous_bundle_sha256'))
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(report)).decode(),stderr='')

    def test_exact_selected_blobs_preserve_nested_paths_and_leave_old_failure_unchanged(self):
        with patch('urllib.request.build_opener',side_effect=AssertionError('no real network')), \
                patch('subprocess.Popen',side_effect=AssertionError('no code execution')):
            result=self.service.acquire('c'*32,self.choices(3))
        self.assertEqual(result['state'],'selected_complete');self.assertTrue(result['selected_complete'])
        self.assertFalse(result['source_complete']);self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['request_count'],3);self.assertEqual(len(self.blobs.calls),3)
        folder=Path(result['remote_directory'])/'source'
        for row in result['files']:
            path=folder/row['path'];self.assertEqual(path.read_bytes(),self.data[row['path']])
            self.assertEqual(path.stat().st_mode&0o777,0o400)
            self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
        self.assertEqual((Path(self.old['remote_directory'])/'result.json').read_bytes(),self.original_bytes)
        self.assertEqual(self.actions,['acquire'])

    def test_batches_share_exact_global_call_and_transfer_accounting(self):
        result=self.service.acquire('c'*32,self.choices(40))
        self.assertEqual(result['request_count'],40);self.assertEqual(len(self.blobs.calls),40)
        self.assertEqual(len(list(Path(result['remote_directory']).glob('batch-*'))),3)
        sizes=[json.loads(path.read_bytes())['bytes'] for path in Path(result['remote_directory']).glob('fetch-*-result.json')]
        self.assertEqual(result['received_bytes'],sum(sizes));self.assertLessEqual(sum(sizes),worker.MAX_TRANSFER)

    def test_global_request_cap_does_not_reset_at_the_second_batch(self):
        with patch.object(worker,'MAX_CALLS',17): result=self.run_worker(self.request(40))
        self.assertEqual(result['state'],'partial');self.assertEqual(result['request_count'],17)
        self.assertEqual(len(self.blobs.calls),17);self.assertEqual(len(result['files']),17)
        self.assertEqual(result['failures'][0]['reason'],'selected_request_limit')

    def test_global_transfer_cap_does_not_reset_at_batch_boundary(self):
        # The fake small JSON has a known 250-byte bound; isolate cumulative
        # accounting from the production 8KiB metadata headroom.
        with patch.object(worker,'MAX_TRANSFER',3500),patch.object(worker,'blob_response_limit',return_value=250):
            result=self.run_worker(self.request(40))
        self.assertEqual(result['state'],'partial');self.assertLessEqual(result['received_bytes'],3500)
        self.assertLess(len(result['files']),40);self.assertGreater(result['request_count'],1)

    def test_original_request_counter_is_not_reset_by_partial_resume(self):
        self.blobs.change=lambda value,count:value.update(sha='f'*40) if count==2 else None
        first=self.service.acquire('c'*32,self.choices(3));before=len(self.blobs.calls)
        self.assertEqual(first['request_count'],2)
        self.blobs.change=None
        with patch.object(worker,'MAX_CALLS',2):final=self.service.resume('c'*32)
        self.assertEqual(final['state'],'partial')
        self.assertEqual(final['request_count'],2)
        self.assertEqual(final['failures'][0]['reason'],'selected_request_limit')
        self.assertEqual(final['files'],first['files'])
        self.assertEqual(len(self.blobs.calls),before)

    def test_small_remaining_transfer_budget_is_refused_before_new_GET(self):
        with patch.object(worker,'MAX_TRANSFER',3500):result=self.run_worker(self.request(40))
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['request_count'],0)
        self.assertEqual(self.blobs.calls,[])

    def test_known_partial_resumes_only_missing_files_in_original_operation(self):
        self.blobs.change=lambda value,count:value.update(sha='f'*40) if count==2 else None
        first=self.service.acquire('c'*32,self.choices(3))
        self.assertEqual(first['state'],'partial');self.assertEqual(len(first['files']),1)
        remote=Path(first['remote_directory']);old=(remote/'result.json').read_bytes()
        retained=(remote/'source'/first['files'][0]['path']).read_bytes()
        self.blobs.change=None
        final=self.service.resume('c'*32)
        self.assertEqual(final['state'],'selected_complete',final)
        self.assertEqual(final['request_count'],4)
        self.assertEqual(len(self.blobs.calls),4)
        self.assertEqual(final['files'][0],first['files'][0])
        self.assertEqual((remote/'result.json').read_bytes(),old)
        self.assertEqual((remote/'source'/first['files'][0]['path']).read_bytes(),retained)
        self.assertEqual(self.actions,['acquire','resume'])
        self.assertEqual(self.service.resume('c'*32),final)
        self.assertEqual(len(self.blobs.calls),4)

    def test_original_storage_reservation_refuses_missing_file_retry_without_new_GET(self):
        self.blobs.change=lambda value,count:value.update(sha='f'*40) if count==2 else None
        first=self.service.acquire('c'*32,self.choices(3))
        self.assertEqual(first['state'],'partial');before=len(self.blobs.calls)
        with patch.object(worker,'_operation_storage_bytes',return_value=worker._reservation(self.request(3))):
            final=self.service.resume('c'*32)
        self.assertEqual(final['state'],'partial')
        self.assertEqual(final['failures'][0]['reason'],'selected_storage_reservation_limit')
        self.assertEqual(len(self.blobs.calls),before)
        self.assertEqual(final['files'],first['files'])

    def test_same_operation_and_reconcile_never_download_again(self):
        first=self.service.acquire('c'*32,self.choices(3));count=len(self.blobs.calls)
        self.assertEqual(self.service.acquire('c'*32,self.choices(3)),first)
        self.assertEqual(self.service.reconcile('c'*32),first)
        self.assertEqual(len(self.blobs.calls),count);self.assertEqual(self.actions,['acquire','reconcile'])

    def test_same_selected_set_cannot_be_redownloaded_under_a_new_operation_id(self):
        self.run_worker();count=len(self.blobs.calls)
        with self.assertRaisesRegex(AcquisitionError,'already_bound'):
            self.run_worker(self.request(identifier='d'*32))
        self.assertEqual(len(self.blobs.calls),count)
        self.service.acquire('c'*32,self.choices())
        with self.assertRaisesRegex(AcquisitionError,'already_bound'):
            self.service.acquire('e'*32,self.choices())
        self.assertEqual(len(self.blobs.calls),count)

    def test_unknown_transport_is_not_resent_and_can_reconcile_original_remote_result(self):
        capture=self.service.capture
        def lost(command,**options):
            capture(command,**options)
            return dict(returncode=None,failure='timeout',stdout='',stderr='')
        self.service.capture=lost
        unknown=self.service.acquire('c'*32,self.choices(2));self.assertEqual(unknown['state'],'unknown')
        count=len(self.blobs.calls)
        self.assertEqual(self.service.acquire('c'*32,self.choices(2))['state'],'unknown')
        self.assertEqual(self.service.resume('c'*32)['state'],'unknown')
        self.service.capture=capture
        self.assertEqual(self.service.reconcile('c'*32)['state'],'selected_complete')
        self.assertEqual(len(self.blobs.calls),count)

    def test_interrupted_worker_reconciliation_retains_attempt_count_without_new_GET(self):
        self.blobs.change=lambda value,count:(_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): self.run_worker()
        self.blobs.change=None
        result=self.run_worker(action='reconcile')
        self.assertEqual(result['state'],'unknown');self.assertEqual(result['request_count'],1)
        self.assertFalse(result['transfer_accounting_complete']);self.assertEqual(len(self.blobs.calls),1)
        self.assertEqual(self.run_worker()['state'],'unknown');self.assertEqual(len(self.blobs.calls),1)

    def test_bad_second_blob_keeps_first_original_and_partial_receipt(self):
        self.blobs.change=lambda value,count:value.update(sha='f'*40) if count==2 else None
        result=self.run_worker(self.request(3));self.assertEqual(result['state'],'partial')
        self.assertEqual(len(result['files']),1);self.assertEqual(len(result['missing']),2)
        self.assertEqual(result['failures'][0]['reason'],'selected_blob_identity_mismatch')
        self.assertFalse(result['selected_complete']);self.assertEqual(self.run_worker(self.request(3)),result)

    def test_wrong_base64_content_and_git_hash_are_rejected_without_saving_bad_original(self):
        for mutation in ('base64','hash','size','encoding'):
            with self.subTest(mutation=mutation):
                request=self.request(identifier=(str(1+len(self.blobs.calls))*32))
                # Distinct path identities are required, never a same-set retry.
                selected=self.choices(4)[len(self.blobs.calls):len(self.blobs.calls)+1]
                request=self.service.selection_request(request['id'],selected)
                def change(value,count):
                    if mutation=='base64': value['content']='!bad'
                    elif mutation=='hash': value['content']=base64.b64encode(b'x'*value['size']).decode()
                    elif mutation=='size': value['size']+=1
                    else: value['encoding']='utf-8'
                self.blobs.change=change
                report=self.run_worker(request)
                self.assertEqual(report['state'],'partial');self.assertEqual(report['files'],[])

    def test_nonregular_unsafe_absent_and_changed_model_selections_reject_before_dispatch(self):
        for change in ('link','unsafe','absent','sha','mode','size','extra','duplicate'):
            choices=self.choices()
            if change=='link': choices=[dict(path='linked',blob_sha1='f'*40,size=20,git_mode='120000')]
            elif change=='unsafe': choices[0]['path']='../outside'
            elif change=='absent': choices[0]['path']='not-listed.py'
            elif change=='sha': choices[0]['blob_sha1']='f'*40
            elif change=='mode': choices[0]['git_mode']='100755'
            elif change=='size': choices[0]['size']+=1
            elif change=='extra': choices[0]['remote_directory']='/elsewhere'
            else: choices*=2
            with self.subTest(change=change),self.assertRaises(AcquisitionError): self.service.acquire('c'*32,choices)
        self.assertEqual(self.actions,[]);self.assertEqual(self.blobs.calls,[])

    def test_cached_tree_changes_cannot_be_authorized_by_client_supplied_entries(self):
        folder=Path(self.old['remote_directory']);path=folder/'02-response.json'
        value=json.loads(path.read_bytes());tree=json.loads(base64.b64decode(value['stdout']))
        tree['tree'][0]['sha']='f'*40;value['stdout']=base64.b64encode(canonical(tree)).decode()
        path.chmod(0o600);path.write_bytes(canonical(value))
        with self.assertRaisesRegex(AcquisitionError,'inventory_digest'):
            self.run_worker()
        self.assertEqual(self.blobs.calls,[])

    def test_original_request_and_receipt_identities_are_not_replaceable(self):
        for field in ('commit','repository','id','directory'):
            request=self.request();original=request['inventory_request']['acquisition_request']
            original[field]={'commit':'f'*40,'repository':'Other/repo','id':'d'*32,'directory':str(self.root/'other')}[field]
            with self.subTest(field=field),self.assertRaises((ValueError,OSError)):
                self.run_worker(request)
        self.assertEqual(self.blobs.calls,[])

    def test_saved_source_symlink_permissions_and_changed_bytes_rejected_on_reconcile(self):
        result=self.run_worker();path=Path(result['remote_directory'])/'source'/self.choices()[0]['path']
        old=path.read_bytes();path.chmod(0o600);path.write_bytes(b'x'*len(old));path.chmod(0o400)
        with self.assertRaisesRegex(AcquisitionError,'integrity'): self.run_worker(action='reconcile')
        path.unlink();target=self.root/'target';target.write_bytes(old);path.symlink_to(target)
        with self.assertRaises((ValueError,OSError)): self.run_worker(action='reconcile')
        path.unlink();path.write_bytes(old);path.chmod(0o644)
        with self.assertRaisesRegex(AcquisitionError,'type_or_size'): self.run_worker(action='reconcile')

    def test_nonprivate_remote_parent_is_rejected_before_download(self):
        self.hpc.chmod(0o755)
        with self.assertRaises(AcquisitionError): self.run_worker()
        self.assertEqual(self.blobs.calls,[])

    def test_forged_remote_receipt_cannot_turn_partial_or_other_request_into_success(self):
        request=self.request();report=worker._report(request)
        for field,value in [('selected_complete',True),('source_complete',True),('request_count',129),
                ('request_sha256','f'*64),('missing',[])]:
            changed=deepcopy(report);changed[field]=value
            with self.subTest(field=field),self.assertRaises(AcquisitionError):worker.validate_selected_report(request,changed)

    def test_tool_bundle_contains_only_reviewed_code_and_keeps_originals_out(self):
        bundle=selected_tool_bundle()
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn('auto_lammps/reference_selected_resource_worker.py',archive.namelist())
            self.assertFalse(any(name.endswith('.lmp') for name in archive.namelist()))
        self.assertIn('selected_resources(request,action=',BOOTSTRAP)
        self.assertNotIn('response=read_source_report(request)',BOOTSTRAP)

    def test_exact_count_and_byte_boundaries_are_enforced_before_any_request(self):
        request=self.request();row=request['selected'][0]
        request['selected']=[dict(row,path=f'file-{index:03d}') for index in range(128)]
        worker.validate_selection_request(request)
        request['selected'].append(dict(row,path='file-128'))
        with self.assertRaisesRegex(AcquisitionError,'file_count'):worker.validate_selection_request(request)
        request['selected']=[dict(row,path=f'file-{index:03d}',size=MAX_BLOB) for index in range(4)]
        worker.validate_selection_request(request)
        request['selected'].append(dict(row,path='file-004',size=1))
        with self.assertRaisesRegex(AcquisitionError,'source_bytes'):worker.validate_selection_request(request)
        request['selected']=[dict(row,size=MAX_BLOB+1)]
        with self.assertRaises(AcquisitionError):worker.validate_selection_request(request)
        self.assertEqual(self.blobs.calls,[])

    def test_persistent_original_storage_and_operation_limits_are_not_reset_by_new_ids(self):
        with patch.object(worker,'MAX_OPERATIONS',1):
            self.run_worker()
            request=self.service.selection_request('d'*32,self.choices(2)[1:])
            with self.assertRaisesRegex(AcquisitionError,'operation_count'):
                self.run_worker(request)
        self.assertEqual(len(self.blobs.calls),1)

    def test_storage_reservation_counts_previous_selection_before_more_downloads(self):
        with patch.object(worker,'_reservation',return_value=300*1024**2):
            self.run_worker()
            request=self.service.selection_request('d'*32,self.choices(2)[1:])
            with self.assertRaisesRegex(AcquisitionError,'storage_reservation'):
                self.run_worker(request)
        self.assertEqual(len(self.blobs.calls),1)

    def test_local_machine_cannot_run_acquisition_even_with_fake_HTTP_transport(self):
        with patch.object(worker.sys,'platform','darwin'):
            with self.assertRaisesRegex(AcquisitionError,'requires_HPC'):
                worker.selected_resources(self.request(),action='acquire',transport=self.blobs)
        self.assertEqual(self.blobs.calls,[])

    def test_reviewed_bootstrap_reconcile_has_no_GitHub_or_source_execution(self):
        bundle=selected_tool_bundle()
        envelope=canonical(dict(action='reconcile',request=self.request(),
            bundle=base64.b64encode(bundle).decode(),bundle_sha256=sha256(bundle)))
        output,errors=io.StringIO(),io.StringIO();original_path=list(sys.path)
        try:
            with self.hpc_session(),patch('sys.stdin',SimpleNamespace(buffer=io.BytesIO(envelope))), \
                    redirect_stdout(output),redirect_stderr(errors), \
                    patch('urllib.request.build_opener',side_effect=AssertionError('no network')):
                exec(compile(BOOTSTRAP,'<reviewed-selected-bootstrap>','exec'),{})
        finally:sys.path[:]=original_path
        self.assertEqual(errors.getvalue(),'')
        report=json.loads(output.getvalue());self.assertEqual(report['state'],'unknown')
        self.assertEqual(report['files'],[]);self.assertEqual(self.blobs.calls,[])


class LargeBlobResumeTests(unittest.TestCase):
    def setUp(self):
        self.fx=SelectedResourceTests()
        self.fx.synthetic_contents={f'code/group/file-{i:03d}.txt':
            (b'x'*1000000 if i==19 else f'synthetic original {i}\n'.encode()) for i in range(40)}
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def test_large_wrapped_base64_and_multiple_batches_use_bounded_correct_envelope(self):
        def wrap(value,count):
            content=value['content'];value['content']='\n'.join(content[i:i+60] for i in range(0,len(content),60))+'\n'
        self.fx.blobs.change=wrap
        result=self.fx.run_worker(self.fx.request(40))
        self.assertEqual(result['state'],'selected_complete',result.get('failures'))
        self.assertEqual(len(result['files']),40);self.assertEqual(result['request_count'],40)
        self.assertEqual(result['accounting_mode'],'exact')
        self.assertLessEqual(result['received_budget_bytes'],worker.MAX_TRANSFER)
        self.assertEqual(len(list(Path(result['remote_directory']).glob('batch-*'))),3)

    def test_legacy_known_partial_resumes_with_conservative_cost_and_immutable_prefix(self):
        request=self.fx.request(40)
        with patch.object(worker,'MAX_CALLS',19):first=self.fx.run_worker(request)
        folder=Path(first['remote_directory']);old=deepcopy(first)
        old.pop('received_budget_bytes');old.pop('accounting_mode')
        selected=request['selected'][19]
        maximum=4*((selected['size']+2)//3)+8192
        old.update(request_count=20,transfer_accounting_complete=False,
            failures=[dict(path=selected['path'],reason='selected_transfer_budget_exceeded')])
        (folder/'result.json').unlink();(folder/'result.json').write_bytes(canonical(old));(folder/'result.json').chmod(0o400)
        original=(folder/'result.json').read_bytes()
        (folder/'fetch-020-intent.json').write_bytes(canonical(dict(
            endpoint='repos/'+self.fx.original['repository']+'/git/blobs/'+selected['blob_sha1'],maximum_bytes=maximum)))
        (folder/'fetch-020-intent.json').chmod(0o400)
        retained={r['path']:(folder/'source'/r['path']).read_bytes() for r in old['files']}
        legacy=b'reviewed synthetic urllib response.read(limit+1) hard bound'
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('auto_lammps/reference_selected_resource_worker.py',legacy)
        bundle=buffer.getvalue();bundle_sha=sha256(bundle)
        target=self.fx.hpc/('reader-tools-'+bundle_sha+'.zip');target.write_bytes(bundle);target.chmod(0o400)
        self.fx.blobs.calls.append('synthetic previous failed bounded GET')
        def wrap(value,count):
            content=value['content'];value['content']='\n'.join(content[i:i+60] for i in range(0,len(content),60))+'\n'
        self.fx.blobs.change=wrap
        with patch.object(worker,'_LEGACY_READER_SHA256',sha256(legacy)):
            # Unknown exact old bytes are conservatively charged, never reset.
            cap=old['received_bytes']+maximum+1+100
            before_calls=len(self.fx.blobs.calls)
            with patch.object(worker,'MAX_TRANSFER',cap):
                exhausted=self.fx.run_worker(request,action='resume',previous_bundle_sha256=bundle_sha)
            self.assertEqual(exhausted['state'],'partial')
            self.assertEqual(exhausted['request_count'],20)
            self.assertEqual(len(self.fx.blobs.calls),before_calls)
            self.assertEqual(exhausted['accounting_mode'],'conservative_upper_bound')
            self.assertLessEqual(exhausted['received_budget_bytes'],cap)
            result=self.fx.run_worker(request,action='resume',previous_bundle_sha256=bundle_sha)
            self.assertEqual(result['state'],'selected_complete',result.get('failures'))
            self.assertEqual(result['request_count'],41)
            self.assertEqual(len(self.fx.blobs.calls),41)
            self.assertFalse(result['transfer_accounting_complete'])
            self.assertEqual(result['accounting_mode'],'conservative_upper_bound')
            self.assertEqual(result['received_budget_bytes']-result['received_bytes'],maximum+1)
            self.assertEqual(result,self.fx.run_worker(request,action='reconcile'))
        self.assertEqual((folder/'result.json').read_bytes(),original)
        for path,data in retained.items():self.assertEqual((folder/'source'/path).read_bytes(),data)
        self.assertEqual(len(result['files']),40)


if __name__=='__main__': unittest.main()
