"""Accounted output input identities, with synthetic byte-only transport."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from auto_lammps.author_reference_inputs import AuthorReferenceInputs,REMOTE_CODE,_remote_inputs,validate_reference_inputs
from auto_lammps.manifest import canonical,sha256
from auto_lammps.tasks import TaskError
import test_author_lane_deployment as fixture


class AuthorReferenceInputTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.AuthorLaneDeploymentTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.task=self.fx.task;self.root=self.fx.root;self.run=self.root/'old-run';self.run.mkdir(mode=0o700)
        (self.run/'samples').mkdir(mode=0o700)
        (self.run/'samples/one.restart').write_bytes(b'Synthetic old accounted input')
        (self.run/'samples/two.restart').write_bytes(b'Second synthetic old input')
        (self.run/'grant.key').write_bytes(b'not exported');(self.run/'metadata.json').write_text('{}')
        self.fx.transport.identity['profile']['work_directory']=str(self.root)
        self.binding=dict(task_id=self.task,request_id='a'*32,job_id='1234',manifest_sha256='b'*64,root=str(self.run),
            inventory_receipt_sha256='c'*64,host_alias='synthetic-hpc')
        self.row=dict(accounted=True,state='failed',job_id='1234',manifest_sha256='b'*64,charge_core_seconds=1000)
        self.outputs=SimpleNamespace(bindings={(self.task,'a'*32):None},transport=self.fx.transport,
            _binding=Mock(side_effect=lambda *_:(deepcopy(self.binding),{})),_run=Mock(side_effect=lambda _:deepcopy(self.row)))
        self.network=[];self.capture=Mock(side_effect=self.transfer)
        patcher=patch('auto_lammps.author_reference_inputs.SavedHPCTransport',fixture.OfflineTransport);patcher.start();self.addCleanup(patcher.stop)
        self.service=AuthorReferenceInputs(self.fx.workspace,self.outputs,audit_directory=self.root/'inputs-audit',capture=self.capture)

    def transfer(self,command,*,timeout,max_bytes,input_chunks):
        value=json.loads(b''.join(input_chunks));self.network.append(deepcopy(value))
        report=_remote_inputs(value['request'],action=value['action'],_test_host=True)
        return dict(returncode=0,failure=None,stdout=base64.b64encode(base64.b64encode(canonical(report))+b'\n').decode())

    def inspect(self):return self.service.inspect(self.task,'d'*32)

    def test_fixed_accounted_root_metadata_is_separate_from_author_source_and_preserves_charges(self):
        self.assertEqual(self.service.status(self.task)['history'],[]);self.capture.assert_not_called()
        old=deepcopy(self.row);result=self.inspect();self.assertEqual(result['state'],'metadata_ready',result)
        receipt=result['input_receipt'];self.assertEqual(receipt['kind'],'accounted_reference_output')
        self.assertEqual([row['path'] for row in receipt['files']],['samples/one.restart','samples/two.restart'])
        self.assertEqual(receipt['request_id'],'a'*32);self.assertEqual(receipt['job_id'],'1234')
        self.assertEqual(self.service.planner_inputs(self.task),[receipt]);self.assertEqual(self.row,old)
        self.assertEqual(self.inspect(),result);self.assertEqual(len(self.network),1)
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['scientific_validation'])

    def test_unaccounted_active_wrong_connection_and_changed_original_identity_reject(self):
        for key,value in [('accounted',False),('state','running')]:
            old=self.row[key];self.row[key]=value
            with self.assertRaises(TaskError):self.inspect()
            self.row[key]=old
        self.fx.transport.current=2
        with self.assertRaises(TaskError):self.inspect()
        self.capture.assert_not_called()

    def test_unknown_only_reconciles_original_request_without_rescan(self):
        def lost(*args,**kwargs):self.transfer(*args,**kwargs);raise OSError('SYNTHETIC PRIVATE')
        self.capture.side_effect=lost
        self.assertEqual(self.inspect()['state'],'unknown')
        with self.assertRaisesRegex(TaskError,'未知'):self.service.inspect(self.task,'e'*32)
        self.capture.side_effect=self.transfer
        (self.run/'samples/one.restart').write_bytes(b'Changed after inspection; staging must reject old SHA')
        result=self.service.reconcile(self.task,'d'*32);self.assertEqual(result['state'],'metadata_ready')
        self.assertEqual(self.network[-1]['action'],'reconcile')
        self.assertNotEqual(result['input_receipt']['files'][0]['sha256'],sha256((self.run/'samples/one.restart').read_bytes()))
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_absent_reconcile_and_origin_spoof_are_rejected_or_explicit(self):
        self.capture.side_effect=OSError('synthetic transport unavailable');self.assertEqual(self.inspect()['state'],'unknown')
        self.capture.side_effect=self.transfer
        self.assertEqual(self.service.reconcile(self.task,'d'*32)['state'],'not_started')
        self.assertFalse((self.root/'Auto-LAMMPS-Reference-Input-Audit').exists())
        saved=self.service._stored(self.task,'d'*32)[0]['context']
        self.assertEqual(self.inspect()['state'],'metadata_ready')
        self.assertEqual(self.service._stored(self.task,'d'*32)[0]['context'],saved)
        self.assertEqual(len(self.service.status(self.task)['history']),1)
        self.assertEqual([row['action'] for row in self.network],['reconcile','inspect'])
        receipt=dict(kind='author_source',task_id=self.task,request_id='a'*32,job_id='1',manifest_sha256='b'*64,
            receipt_sha256='c'*64,binding_sha256='d'*64,root=str(self.run),files=[])
        with self.assertRaises(TaskError):validate_reference_inputs([receipt],self.task)

    def test_search_only_ancestor_requires_no_directory_listing_but_final_root_remains_private(self):
        real_open=os.open;search_flag=1<<30;ancestor=self.root.parts[1];visited=[]
        def open_search(path,flags,*args,**kwargs):
            if kwargs.get('dir_fd') is not None and str(path)==ancestor and flags&os.O_DIRECTORY:
                visited.append(flags)
                if not flags&search_flag:raise PermissionError('synthetic search-only ancestor')
            return real_open(path,flags&~search_flag,*args,**kwargs)
        with patch.object(os,'O_PATH',search_flag,create=True),patch('os.open',side_effect=open_search):
            self.assertEqual(self.inspect()['state'],'metadata_ready')
        self.assertTrue(visited);self.assertTrue(all(flags&os.O_NOFOLLOW for flags in visited))
        self.run.chmod(0o755)
        self.capture.side_effect=self.transfer
        with self.assertRaisesRegex(ValueError,'private_reference_root'):
            context,_=self.service._context(self.task,'e'*32)
            _remote_inputs(context['request'],_test_host=True)

    def test_repaired_observer_preserves_original_intent_and_rejects_changed_run_binding(self):
        self.capture.side_effect=OSError('initial metadata transport failure')
        self.assertEqual(self.inspect()['state'],'unknown')
        original=self.service._stored(self.task,'d'*32)[0]['context']
        self.capture.side_effect=self.transfer
        repaired=REMOTE_CODE+'\n# synthetic metadata observer repair\n'
        with patch('auto_lammps.author_reference_inputs.REMOTE_CODE',repaired):
            absent=self.service.reconcile(self.task,'d'*32)
            self.assertEqual(absent['state'],'not_started')
            result=self.inspect();self.assertEqual(result['state'],'metadata_ready')
            event=result['events'][-1]
            self.assertEqual(event['request_worker_sha256'],original['request']['worker_sha256'])
            self.assertEqual(event['observer_worker_sha256'],sha256(repaired.encode()))
            self.assertNotEqual(event['observer_worker_sha256'],event['request_worker_sha256'])
            self.assertEqual(self.service.planner_inputs(self.task),[result['input_receipt']])
            self.binding['root']='/changed/accounted/root'
            with self.assertRaisesRegex(TaskError,'来源'):self.service.planner_inputs(self.task)
        self.assertEqual(self.service._stored(self.task,'d'*32)[0]['context'],original)

    def test_private_transport_diagnostics_are_retained_without_browser_exception_text(self):
        self.capture.side_effect=OSError('SYNTHETIC PRIVATE permission denied')
        result=self.inspect();self.assertEqual(result['state'],'unknown')
        self.assertNotIn('PRIVATE',json.dumps(result))
        capture=json.loads((self.service.audit/('d'*32+'-1-transport.json')).read_bytes())
        self.assertEqual(capture['exception_type'],'OSError')
        self.assertIn('permission denied',capture['exception_message'])
        self.assertEqual(result['events'][0]['transport_capture_sha256'],sha256(canonical(capture)))


if __name__=='__main__':unittest.main()
