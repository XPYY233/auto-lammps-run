"""Fresh ordinary author-resource metadata without model, SSH or physics."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import zipfile

from auto_lammps.author_resource_bootstrap import (AuthorResourceBootstrap,bootstrap_pinned_tree,
    bootstrap_tool_bundle,validate_bootstrap_report,PURPOSE,MAX_LICENSE_BYTES)
from auto_lammps.hpc_connections import HPCConnections
from auto_lammps.manifest import canonical,sha256
from auto_lammps.potential_acquisition import AcquisitionError
from auto_lammps.reference_selected_resources import ReferenceSelectedResources
from auto_lammps.tasks import TaskError
from test_hpc_connections import PROFILE
from test_potential_acquisition import Reader,git_blob
import test_reference_resource_association as catalog_fixture


class AuthorResourceBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.f=catalog_fixture.ReferenceResourceAssociationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.tasks,self.papers=self.f.tasks,self.f.papers
        self.task=self.tasks.create('Synthetic bootstrap','Synthetic request','reproduction')
        paper=self.papers.select(self.f.paper['id'],self.f.paper['revision'])
        self.paper=self.papers.link_task(paper['id'],paper['revision'],self.task['id'])
        self.f.associate()
        self.scope=dict(id='9'*32,state='recorded',scope_sha256='a'*64,
            scope=dict(provenance=dict(paper_id=self.paper['id'])))
        self.scopes=SimpleNamespace(tasks=self.tasks,get=Mock(side_effect=lambda _:deepcopy(self.scope)))
        self.hpc=HPCConnections(self.tasks,probe=Mock(side_effect=AssertionError('No SSH probe')))
        self.profile={**PROFILE,'work_directory':str(self.f.root/'remote')}
        self.hpc.save(self.profile,0,known_hosts='Synthetic-known-host')
        self.repository=Reader();self.repository.files['LICENSE']=b'Synthetic license\r\nAll rights preserved.\n'
        self.actions=[];self.transform=None;self.fail_capture=False;self.after_worker_unknown=False
        self.capture=Mock(side_effect=self.synthetic_capture)
        self.service=AuthorResourceBootstrap(self.tasks,self.papers,self.scopes,self.f.library,self.hpc,capture=self.capture)
        self.request='7'*32

    def synthetic_capture(self,argv,**options):
        self.assertEqual(argv[0],'ssh');self.assertIn(self.profile['host'],argv)
        self.assertIn('StrictHostKeyChecking=yes',argv)
        envelope=json.loads(b''.join(options['input_chunks']));self.actions.append(envelope['action'])
        self.assertEqual(sha256(base64.b64decode(envelope['bundle'])),envelope['bundle_sha256'])
        if self.fail_capture:return dict(returncode=None,failure='Synthetic-timeout',stdout='',stderr='')
        with patch('sys.platform','linux'),patch.dict(os.environ,{'SSH_CONNECTION':'synthetic-only'}):
            report=bootstrap_pinned_tree(envelope['request'],action=envelope['action'],transport=self.repository)
        if self.after_worker_unknown:return dict(returncode=None,failure='Synthetic-disconnected',stdout='',stderr='')
        if self.transform:report=self.transform(deepcopy(report))
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(report)).decode(),stderr='')

    def inspect(self,request=None):return self.service.inspect(self.task['id'],request or self.request)

    def test_no_workspace_first_inspection_fetches_tree_license_only_and_retains_actual_pins(self):
        before=self.tasks.get(self.task['id']);status=self.service.status(self.task['id'])
        self.assertTrue(status['enabled']);self.assertFalse(status['requires_author_workspace'])
        self.assertEqual(status['history'],[]);self.capture.assert_not_called()
        result=self.inspect();self.assertEqual(result['state'],'metadata_ready',result)
        self.assertEqual(result['files'],[]);self.assertFalse(result['source_complete'])
        self.assertFalse(result['use_approved']);self.assertFalse(result['reading_approved'])
        self.assertEqual(len(self.repository.calls),3)
        blob_calls=[call for call in self.repository.calls if '/blobs/' in call]
        self.assertEqual(len(blob_calls),1);self.assertTrue(blob_calls[0].endswith(git_blob(self.repository.files['LICENSE'])))
        metadata=self.service.metadata_for_selection(self.task['id'])
        self.assertEqual(metadata['reference_scope'],self.scope)
        self.assertEqual(metadata['acquisition_receipt']['files'],[])
        self.assertEqual(metadata['acquisition_receipt']['source_bytes'],0)
        self.assertFalse(metadata['acquisition_receipt']['source_complete'])
        self.assertEqual(metadata['bootstrap_receipt']['purpose'],PURPOSE)
        licence,=metadata['license_evidence']['files']
        self.assertEqual(licence['text'].encode(),self.repository.files['LICENSE'])
        self.assertEqual(licence['sha256'],sha256(self.repository.files['LICENSE']))
        self.assertTrue(metadata['read_approval_required'])
        original=metadata['acquisition_request'];folder=Path(original['directory'])/self.request
        self.assertEqual(list((folder/'source').iterdir()),[])
        self.assertEqual((folder/'licenses'/(licence['sha256']+'.txt')).read_bytes(),self.repository.files['LICENSE'])
        self.assertEqual(self.tasks.get(self.task['id']),before)
        # Admit the compatibility envelope through the existing tool without
        # invoking acquire; metadata readiness never equals source readiness.
        _,transport=self.service._context(self.task['id'])
        tool=ReferenceSelectedResources(self.f.root/'selected-audit',host_alias=metadata['binding']['host_alias'],
            acquisition_request=original,acquisition_receipt=metadata['acquisition_receipt'],
            inventory=metadata['tree_report'],transport=transport,capture=Mock(side_effect=AssertionError('No fetch')))
        self.assertEqual(tool.inventory,metadata['tree_report'])

    def test_same_request_and_new_id_for_same_fixed_context_reuse_without_extra_download(self):
        first=self.inspect();requests=len(self.repository.calls)
        self.assertEqual(self.inspect(),first)
        self.assertEqual(self.inspect('8'*32),first)
        self.assertEqual(len(self.repository.calls),requests)
        self.assertEqual(len(self.service.status(self.task['id'])['history']),1)
        restart=AuthorResourceBootstrap(self.tasks,self.papers,self.scopes,self.f.library,self.hpc,
            capture=Mock(side_effect=AssertionError('No restart fetch')))
        self.assertEqual(restart.inspect(self.task['id'],self.request),first)

    def test_unknown_blocks_new_ids_and_reconciliation_does_not_restart_http(self):
        self.after_worker_unknown=True
        first=self.inspect();self.assertEqual(first['state'],'unknown')
        used=len(self.repository.calls);self.after_worker_unknown=False
        with self.assertRaises(TaskError):self.inspect('8'*32)
        self.assertEqual(self.inspect()['state'],'unknown')
        self.assertEqual(len(self.repository.calls),used)
        recovered=self.service.reconcile(self.task['id'],self.request)
        self.assertEqual(recovered['state'],'metadata_ready',recovered)
        self.assertEqual(self.actions,['inspect','reconcile'])
        self.assertEqual(len(self.repository.calls),used)
        history=self.service._stored(self.task['id'],self.request)['events']
        self.assertEqual(history[-2]['report']['state'],'unknown')
        self.assertEqual(history[-1]['report']['state'],'metadata_ready')

    def test_missing_remote_receipt_reconciliation_creates_no_remote_request(self):
        self.fail_capture=True;first=self.inspect();self.assertEqual(first['state'],'unknown')
        self.fail_capture=False
        result=self.service.reconcile(self.task['id'],self.request)
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(self.repository.calls,[])
        row=self.service._stored(self.task['id'],self.request)
        original=row['context']['request']['acquisition_request']
        self.assertFalse((Path(original['directory'])/self.request).exists())

    def test_p_scope_catalog_and_saved_hpc_identity_changes_cannot_migrate_old_request(self):
        self.inspect();used=len(self.repository.calls)
        self.scope['scope_sha256']='b'*64
        with self.assertRaises(TaskError):self.service.metadata_for_selection(self.task['id'],self.request)
        with self.assertRaises(TaskError):self.inspect()
        self.scope['scope_sha256']='a'*64
        self.hpc.save({**self.profile,'label':'Changed saved revision'},1)
        with self.assertRaises(TaskError):self.inspect()
        self.assertEqual(len(self.repository.calls),used)

    def test_unmatched_catalog_or_missing_scope_or_research_task_cannot_fetch(self):
        self.scope['state']='source_unavailable'
        with self.assertRaises(TaskError):self.inspect()
        self.scope['state']='recorded';self.f.document['tier_a'][0]['commit']='d'*40;self.f.save_catalog()
        with self.assertRaises(TaskError):self.inspect()
        research=self.tasks.create('Synthetic research','No paper','research')
        with self.assertRaises(TaskError):self.service.inspect(research['id'],self.request)
        self.capture.assert_not_called()

    def test_absent_license_keeps_unapproved_missing_and_no_scientific_source_download(self):
        del self.repository.files['LICENSE']
        result=self.inspect();self.assertEqual(result['state'],'metadata_ready')
        self.assertEqual(result['license_evidence']['status'],'not_found')
        self.assertEqual(len(self.repository.calls),2)
        self.assertTrue(self.service.metadata_for_selection(self.task['id'])['read_approval_required'])

    def test_incomplete_tree_and_oversized_license_are_known_failed_without_scientific_blob_fetch(self):
        self.repository.truncated=True
        result=self.inspect();self.assertEqual(result['state'],'failed')
        self.assertEqual(result['code'],'incomplete_source_tree')
        self.assertEqual(len(self.repository.calls),2)
        self.repository.truncated=False;self.repository.files['LICENSE']=b'x'*(MAX_LICENSE_BYTES+1)
        result=self.inspect('8'*32);self.assertEqual(result['state'],'failed')
        self.assertEqual(result['code'],'license_byte_budget_exceeded')
        self.assertFalse(any('/blobs/' in path for path in self.repository.calls))

    def test_forged_license_approval_tree_or_bytes_are_rejected_without_publishing(self):
        result=self.inspect();self.assertEqual(result['state'],'metadata_ready')
        row=self.service._stored(self.task['id'],self.request);request=row['context']['request'];original=row['events'][-1]['report']
        for mutate in (lambda r:r['license_evidence'].update(use_approved=True),
                lambda r:r['license_evidence']['files'][0].update(text='Changed literal notice.'),
                lambda r:r['acquisition_receipt'].update(source_complete=True),
                lambda r:r['tree_report'].update(tree_sha1='0'*40)):
            changed=deepcopy(original);mutate(changed)
            with self.assertRaises(AcquisitionError):validate_bootstrap_report(request,changed)

    def test_append_only_history_and_cross_task_request_rejected(self):
        self.inspect()
        for table in ('author_bootstrap_intents','author_bootstrap_events'):
            for action in ('DELETE','UPDATE'):
                with self.subTest(table=table,action=action),self.assertRaises(sqlite3.IntegrityError):
                    with self.tasks.transaction() as db:db.execute('DELETE FROM '+table if action=='DELETE' else
                        'UPDATE '+table+' SET document=document')
        other=self.tasks.create('Synthetic other','Other','reproduction')
        with self.assertRaises(TaskError):self.service._stored(other['id'],self.request)

    def test_built_bundle_imports_in_isolation_and_uses_fixed_reviewed_static_worker(self):
        data=bootstrap_tool_bundle();path=self.f.root/'bootstrap-tools.zip';path.write_bytes(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn('auto_lammps/author_resource_bootstrap.py',archive.namelist())
            self.assertEqual(archive.read('auto_lammps/author_resource_bootstrap.py'),
                Path(sys.modules['auto_lammps.author_resource_bootstrap'].__file__).read_bytes())
        code='import sys;sys.path.insert(0,sys.argv[1]);from auto_lammps.author_resource_bootstrap import bootstrap_pinned_tree;print(bootstrap_pinned_tree.__module__)'
        check=subprocess.run([sys.executable,'-I','-S','-c',code,str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(check.returncode,0,check.stderr)
        self.assertEqual(check.stdout.strip(),'auto_lammps.author_resource_bootstrap')

    def test_actual_remote_wrapper_runs_bundle_with_only_synthetic_http_and_no_import_fallback(self):
        self.inspect();envelope=json.loads(b''.join(self.capture.call_args.kwargs['input_chunks']))
        # Use an independent temp operation only inside this synthetic worker;
        # no application task, model request or actual remote call is created.
        envelope['request']['acquisition_request']['id']='b'*32
        script=shlex.split(self.capture.call_args.args[0][-1])[2]
        responses={}
        from test_source_discovery import NAME,COMMIT
        from test_potential_acquisition import TREE
        prefix='https://api.github.com/repos/'+NAME+'/git/'
        responses[prefix+'commits/'+COMMIT]=dict(sha=COMMIT,tree=dict(sha=TREE))
        responses[prefix+'trees/'+TREE+'?recursive=1']=dict(sha=TREE,truncated=False,tree=[
            dict(path=name,sha=git_blob(raw),size=len(raw),type='blob',mode='100644')
            for name,raw in self.repository.files.items()])
        raw=self.repository.files['LICENSE'];digest=git_blob(raw)
        responses[prefix+'blobs/'+digest]=dict(sha=digest,size=len(raw),encoding='base64',
            content=base64.b64encode(raw).decode())
        harness='''import sys,os,json,urllib.request,socket
sys.platform='linux';os.environ['SSH_CONNECTION']='synthetic-isolated-worker'
responses=json.loads(sys.argv[1])
class Reply:
 status=200
 def __init__(self,value):self.raw=json.dumps(value).encode()
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def read(self,maximum):return self.raw[:maximum]
class Opener:
 def open(self,request,timeout):return Reply(responses[request.full_url])
urllib.request.build_opener=lambda *args:Opener()
def forbidden(*args,**kwargs):raise AssertionError('External network forbidden')
socket.socket=forbidden
exec(sys.argv[2],{'__name__':'synthetic_bootstrap_wrapper'})
'''
        check=subprocess.run([sys.executable,'-I','-S','-c',harness,json.dumps(responses),script],
            input=canonical(envelope),capture_output=True,timeout=20)
        self.assertEqual(check.returncode,0,check.stderr.decode())
        report=json.loads(check.stdout)
        self.assertEqual(report['state'],'metadata_ready',report)
        self.assertEqual(report['request_count'],3)
        self.assertEqual(report['acquisition_receipt']['files'],[])
        self.assertEqual(report['license_evidence']['files'][0]['text'].encode(),raw)


if __name__=='__main__':unittest.main()
