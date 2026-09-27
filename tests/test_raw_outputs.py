import json
import os
import unittest
from auto_lammps.raw_outputs import RawOutputs
from auto_lammps.manifest import canonical,sha256
from auto_lammps.tasks import TaskError
import test_operator_workspace as fixtures

class RawOutputTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.WorkspaceTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.raw=RawOutputs(self.fx.tasks,self.fx.papers)
        self.data=b'# Original LAMMPS output\r\n0 0\r\n100 2\r\n'
        self.path=self.fx.root/'stress.dat';self.path.write_bytes(self.data);self.path.chmod(0o400)
        row=self.fx.ledger.get(self.fx.req['id']);self.proof=self.fx.root/'transfer.json'
        self.value=dict(request_id=row['id'],job_id=row['job_id'],manifest_sha256=row['manifest_sha256'],files=[dict(name='stress.dat',size=len(self.data),sha256=sha256(self.data),kind='output')])
        self.proof.write_bytes(canonical(self.value));self.proof.chmod(0o600)

    def publish(self):return self.raw.publish(self.fx.task['id'],self.fx.req['id'],'stress.dat',self.path,self.proof)

    def test_original_bytes_download_metadata_and_no_submission(self):
        before=self.fx.ledger.evaluation_snapshot(self.fx.evaluation)['dispatch_claims']
        identifier=self.publish();self.assertEqual(identifier,self.publish())
        url=f"/api/tasks/{self.fx.task['id']}/raw-files"
        r=self.fx.client.get(url);self.assertEqual(r.status_code,200,r.text);self.assertNotIn(str(self.fx.root),r.text)
        response=self.fx.client.get(url+'/'+identifier)
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.content,self.data)
        self.assertEqual(response.headers['content-length'],str(len(self.data)))
        self.assertIn('attachment',response.headers['content-disposition'])
        self.assertEqual(before,self.fx.ledger.evaluation_snapshot(self.fx.evaluation)['dispatch_claims'])

    def test_different_task_and_changed_file_fail_closed(self):
        identifier=self.publish();other=self.fx.tasks.create('Other','Other task','research')
        self.assertEqual(self.fx.client.get(f"/api/tasks/{other['id']}/raw-files/{identifier}").status_code,409)
        self.path.chmod(0o600);self.path.write_bytes(self.data.replace(b'100 2',b'100 9'))
        self.assertEqual(self.fx.client.get(f"/api/tasks/{self.fx.task['id']}/raw-files/{identifier}").status_code,409)

    def test_reference_inputs_and_changed_receipt_rejected(self):
        self.value['files'][0]['kind']='input';self.proof.write_bytes(canonical(self.value))
        with self.assertRaises(TaskError):self.publish()
        self.value['files'][0]['kind']='output';self.proof.write_bytes(canonical(self.value));identifier=self.publish()
        self.value['job_id']='999';self.proof.write_bytes(canonical(self.value))
        with self.assertRaises(TaskError):self.raw.download(self.fx.task['id'],identifier)

    def test_links_and_non_accounted_state_rejected(self):
        original=self.path.with_name('original.dat');self.path.rename(original);self.path.symlink_to(original)
        with self.assertRaises(OSError):self.publish()
        self.path.unlink();original.rename(self.path)
        with self.fx.ledger._transaction() as db:db.execute("UPDATE requests SET accounted=0 WHERE id=?",(self.fx.req['id'],))
        with self.assertRaises(TaskError):self.publish()

    def test_export_storage_retained_once_without_resetting_attempts(self):
        ledger=self.fx.ledger;request=self.fx.req['id'];before=ledger.get(request)
        proof='e'*64
        ledger.reserve_raw_export(request,proof,200)
        ledger.reserve_raw_export(request,proof,200)
        after=ledger.get(request)
        self.assertEqual(after['charge_storage_bytes'],before['charge_storage_bytes']+200)
        self.assertEqual(after['dispatch_claimed'],before['dispatch_claimed'])
        self.assertEqual(after['actual_core_seconds'],before['actual_core_seconds'])
        from auto_lammps.ledger import Conflict,LimitExceeded
        with self.assertRaises(Conflict):ledger.reserve_raw_export(request,proof,201)
        with self.assertRaises(LimitExceeded):ledger.reserve_raw_export(request,'f'*64,1000000)
