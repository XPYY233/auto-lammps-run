"""Synthetic legacy A output retrieval through the actual user HTTP service."""
import base64
from dataclasses import replace
import json
import shlex
import sys
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient
from auto_lammps.hpc_connections import HPCConnections
from auto_lammps.hpc_transport import SavedHPCTransport
from auto_lammps.manifest import canonical, sha256
from auto_lammps.raw_outputs import RawOutputs
from auto_lammps.reference_outputs import ReferenceOutputs
from auto_lammps.reference_output_worker import read_outputs
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
import test_operator_workspace as fixture
from test_web import ORIGIN, HEADERS


class ReferenceOutputsTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.WorkspaceTests()
        with patch.object(fixture,'POLICY',replace(fixture.POLICY,total_storage_bytes=128*1024*1024)):
            self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root=self.fx.root
        self.remote=self.root/'remote';self.remote.mkdir(mode=0o700)
        self.data=b'# Original synthetic output\r\n1.5\r\n2.5\r\n'
        (self.remote/'values.txt').write_bytes(self.data)
        inventory=self.root/'observed-output-inventory.json'
        inventory.write_bytes(canonical(dict(path='values.txt',size=len(self.data),sha256=sha256(self.data))))
        inventory.chmod(0o600)
        self.binding=dict(task_id=self.fx.task['id'],request_id=self.fx.req['id'],job_id='123',
            manifest_sha256=self.fx.req['manifest_sha256'],root=str(self.remote),host_alias='synthetic-hpc',
            inventory_receipt=str(inventory),inventory_receipt_sha256=sha256(inventory.read_bytes()),
            files=[dict(path='values.txt',name='author-values.txt',size=len(self.data),sha256=sha256(self.data))])
        hpc=HPCConnections(self.fx.tasks)
        hpc.save(dict(label='Synthetic',host='saved.example.edu',port=2233,username='researcher',
            work_directory=str(self.root),partition='compute',account='',authentication='agent'),0)
        self.transport=SavedHPCTransport(hpc,1,'synthetic-hpc')
        self.raw=RawOutputs(self.fx.tasks,self.fx.papers)
        self.captured=[];self.capture_limits=[]
        self.service=ReferenceOutputs(self.raw,self.root/'retrieved',[self.binding],
            transport=self.transport,capture=self.capture)

    def capture(self,command,**kwargs):
        self.captured.append(command);self.capture_limits.append(kwargs['max_bytes'])
        request=json.loads(b''.join(kwargs['input_chunks']))
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(read_outputs(request))).decode(),stderr='')

    def test_http_retrieves_original_bytes_and_reuses_without_new_computation(self):
        before=self.fx.ledger.get(self.fx.req['id'])
        client=TestClient(create_app(self.fx.tasks,papers=self.fx.papers,
            reference_outputs_service=self.service),base_url=ORIGIN)
        self.addCleanup(client.close)
        url=f"/api/tasks/{self.fx.task['id']}/reference-outputs"
        initial=client.get(url);self.assertEqual(initial.status_code,200,initial.text)
        self.assertNotIn(str(self.root),initial.text)
        self.assertEqual(initial.json()['runs'][0]['state'],'available')
        for _ in range(2):
            result=client.post(url,json={'request_id':self.fx.req['id']},headers=HEADERS)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['runs'][0]['state'],'retained')
        self.assertEqual(len(self.captured),1)
        self.assertIn('saved.example.edu',self.captured[0])
        raw=client.get(f"/api/tasks/{self.fx.task['id']}/raw-files").json()['files']
        self.assertEqual(len(raw),1)
        downloaded=client.get(f"/api/tasks/{self.fx.task['id']}/raw-files/{raw[0]['id']}")
        self.assertEqual(downloaded.content,self.data)
        after=self.fx.ledger.get(self.fx.req['id'])
        self.assertEqual(after['dispatch_claimed'],before['dispatch_claimed'])
        self.assertEqual(after['actual_core_seconds'],before['actual_core_seconds'])
        self.assertEqual(after['state'],before['state'])
        self.assertEqual(after['charge_storage_bytes'],before['charge_storage_bytes']+len(self.data)+262144)

    def test_full_eighteen_original_outputs_are_retrieved_with_exact_bytes_and_one_charge(self):
        sizes=[1662110]*9+[768975]*8
        sizes.append(22000941-sum(sizes))
        pins=[]
        for index,size in enumerate(sizes):
            temperature=350+(index%9)*100
            path=(f'temperatures/T={temperature}K/Potential Energy vs step T_{temperature}.txt'
                  if index<9 else f'temperatures/T={temperature}K/dump.mc.60000')
            file=self.remote/path;file.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            data=(f'Synthetic original byte record {index}\n'.encode()+b'x'*size)[:size]
            file.write_bytes(data)
            pins.append(dict(path=path,name=f'T{temperature}-'+('energy.txt' if index<9 else 'terminal-dump.dat'),
                size=size,sha256=sha256(data)))
        inventory=self.root/'observed-output-inventory.json'
        inventory.write_bytes(canonical(dict(files=pins)))
        binding={**self.binding,'files':pins,'inventory_receipt_sha256':sha256(inventory.read_bytes())}
        service=ReferenceOutputs(self.raw,self.root/'retrieved-large',[binding],
            transport=self.transport,capture=self.capture)
        before=self.fx.ledger.get(self.fx.req['id'])
        first=service.collect(self.fx.task['id'],self.fx.req['id'])
        second=service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(first,second)
        self.assertEqual(first['runs'][0]['state'],'retained')
        self.assertEqual(first['runs'][0]['total_bytes'],22000941)
        self.assertEqual(len(self.captured),1)
        self.assertEqual(len(self.raw.listing(self.fx.task['id'])['files']),18)
        self.assertLess(self.capture_limits[0],2*22000941)
        by_name={item['name']:item for item in pins}
        for saved in self.raw.listing(self.fx.task['id'])['files']:
            original=by_name[saved['name']]
            _,_,stream=self.raw.download(self.fx.task['id'],saved['id'])
            with stream:content=stream.read()
            self.assertEqual(len(content),original['size'])
            self.assertEqual(sha256(content),original['sha256'])
        after=self.fx.ledger.get(self.fx.req['id'])
        self.assertEqual(after['charge_storage_bytes'],before['charge_storage_bytes']+22000941+262144)
        for key in ('actual_core_seconds','dispatch_claimed','job_id','state','manifest_sha256'):
            self.assertEqual(after[key],before[key])

    def test_failed_A_data_remains_failed_and_scientifically_unverified(self):
        with self.fx.ledger._transaction() as db:
            db.execute("UPDATE requests SET state='failed' WHERE id=?",(self.fx.req['id'],))
        result=self.service.collect(self.fx.task['id'],self.fx.req['id'])['runs'][0]
        self.assertEqual(result['scheduler_state'],'failed')
        self.assertEqual(result['scientific_status'],'not_evaluated')
        self.assertEqual(self.fx.ledger.get(self.fx.req['id'])['state'],'failed')

    def test_real_capture_process_encoding_matches_controller_protocol(self):
        from auto_lammps.slurm_read import _capture
        def capture(command,**options):
            remote=shlex.split(command[-1])
            self.assertEqual(remote[:3],['python3','-I','-c'])
            return _capture([sys.executable,*remote[1:]],**options)
        self.service.capture=capture
        result=self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(result['runs'][0]['state'],'retained')
        record=self.raw.listing(self.fx.task['id'])['files'][0]
        _,_,stream=self.raw.download(self.fx.task['id'],record['id'])
        with stream:self.assertEqual(stream.read(),self.data)

    def test_another_task_or_request_cannot_adopt_author_outputs(self):
        other=self.fx.tasks.create('Another task','No science','research')
        for task,request in [(other['id'],self.fx.req['id']),(self.fx.task['id'],'f'*32)]:
            with self.assertRaises(TaskError):self.service.collect(task,request)
        self.assertEqual(self.captured,[])

    def test_live_or_unaccounted_reference_is_rejected_before_transport(self):
        with self.fx.ledger._transaction() as db:
            db.execute('UPDATE requests SET accounted=0 WHERE id=?',(self.fx.req['id'],))
        with self.assertRaises(TaskError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(self.captured,[])

    def test_changed_inventory_and_wrong_job_are_rejected(self):
        path=self.root/'observed-output-inventory.json';path.write_bytes(b'changed')
        with self.assertRaises(TaskError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(self.captured,[])
        wrong=dict(self.binding,job_id='999')
        with self.assertRaises(TaskError):
            ReferenceOutputs(self.raw,self.root/'other',[wrong],transport=self.transport,capture=self.capture)

    def test_changed_raw_bytes_are_never_published(self):
        (self.remote/'values.txt').write_bytes(self.data.replace(b'1.5',b'9.5'))
        with self.assertRaises(ValueError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(self.raw.listing(self.fx.task['id'])['files'],[])

    def test_replaced_retained_file_is_rejected_without_refetch(self):
        self.service.collect(self.fx.task['id'],self.fx.req['id'])
        path=next((self.root/'retrieved').glob('*/author-values.txt'));path.write_bytes(b'changed')
        self.assertEqual(self.service.status(self.fx.task['id'])['runs'][0]['state'],'retained_unavailable')
        with self.assertRaises(TaskError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(len(self.captured),1)

    def test_transport_error_is_not_published_or_copied_to_audit(self):
        canary='PRIVATE_ERROR_CANARY'
        self.service.capture=lambda *a,**kw:dict(returncode=255,failure=canary,stdout=canary,stderr=canary)
        with self.assertRaises(TaskError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(self.raw.listing(self.fx.task['id'])['files'],[])
        self.assertTrue(all(canary not in p.read_text() for p in (self.root/'retrieved').rglob('*.json')))

    def test_wrong_transport_identity_is_rejected_without_capture(self):
        self.transport.identity['revision']=99
        with self.assertRaises(ValueError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(self.captured,[])

    def test_read_failure_history_cannot_exceed_reserved_audit_storage(self):
        self.service.capture=lambda *a,**kw:dict(returncode=255,failure='failure',stdout='',stderr='')
        with self.assertRaises(TaskError):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        folder=next((self.root/'retrieved').iterdir())
        (folder/'synthetic-audit.json').write_bytes(b' '*(262144-1024))
        self.service.capture=self.capture
        with self.assertRaisesRegex(TaskError,'审计存储'):self.service.collect(self.fx.task['id'],self.fx.req['id'])
        self.assertEqual(self.captured,[])
