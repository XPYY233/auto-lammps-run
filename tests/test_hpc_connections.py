"""Synthetic connection and original-byte downloads; no real SSH or simulation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore,TaskError,StaleTask
from auto_lammps.hpc_connections import HPCConnections
from auto_lammps.web import create_app

PROFILE=dict(label='Synthetic cluster',host='login.example.edu',port=2222,username='scientist',work_directory='/scratch/research',partition='compute',account='',authentication='agent')
HEADERS={'Origin':'http://127.0.0.1:8765','X-Task-Review':'1'}

class HPCConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.store=TaskStore(self.root/'tasks.sqlite')
        self.probe=Mock(return_value=True);self.hpc=HPCConnections(self.store,probe=self.probe)
        self.client=TestClient(create_app(self.store,hpc_connections=self.hpc),base_url='http://127.0.0.1:8765');self.addCleanup(self.client.close)

    def test_save_private_fields_no_calls_no_echo_and_real_argv(self):
        secret='synthetic-private-key';body={**PROFILE,'authentication':'private_key','revision':0,'private_key':secret,'known_hosts':'synthetic-public-host-key'}
        r=self.client.post('/api/hpc-connection',json=body,headers=HEADERS)
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn(secret,r.text);self.probe.assert_not_called()
        self.assertNotIn(secret,self.client.get('/api/hpc-connection').text)
        files=list(self.hpc.directory.glob('*.json'));self.assertEqual(files[0].stat().st_mode&0o777,0o600)
        r=self.client.post('/api/hpc-connection/check',json={'revision':1},headers=HEADERS)
        self.assertTrue(r.json()['connected']);argv=self.probe.call_args.args[0]
        self.assertEqual(argv[-6:],['-p','2222','-l','scientist','login.example.edu','true'])
        self.assertIn('StrictHostKeyChecking=yes',argv);self.assertNotIn(secret,' '.join(argv))

    def test_old_connection_stays_fixed_and_new_target_does_not_inherit_key(self):
        self.hpc.save({**PROFILE,'authentication':'private_key'},0,private_key='synthetic-key')
        with self.assertRaises(TaskError):self.hpc.save({**PROFILE,'host':'other.example.edu','authentication':'private_key'},1)
        self.hpc.save({**PROFILE,'host':'other.example.edu'},1)
        self.assertIn('login.example.edu',self.hpc.ssh_arguments(1));self.assertIn('other.example.edu',self.hpc.ssh_arguments(2))
        with self.assertRaises(StaleTask):self.hpc.save(PROFILE,1)
        self.assertEqual(HPCConnections(self.store).status()['revision'],2)

    def test_injection_and_invalid_secret_body_rejected_without_reflection(self):
        for key,value in [('host','-oProxyCommand=touch'),('host','user@host'),('username','x; whoami'),('port',True),('work_directory','/scratch/../secret'),('partition','x\n#SBATCH')]:
            with self.subTest(key=key,value=value),self.assertRaises(TaskError):self.hpc.save({**PROFILE,key:value},0)
        reply=self.client.post('/api/hpc-connection',json={**PROFILE,'revision':0,'private_key':{'secret':'sentinel-private'}},headers=HEADERS)
        self.assertEqual(reply.status_code,422);self.assertNotIn('sentinel-private',reply.text)
        self.probe.assert_not_called()

    def test_origin_failure_is_not_recorded_as_connected(self):
        self.assertEqual(self.client.post('/api/hpc-connection',json={**PROFILE,'revision':0}).status_code,403)
        self.hpc.save(PROFILE,0);self.probe.return_value=False
        self.assertFalse(self.hpc.check(1)['connected']);self.assertEqual(self.hpc.status()['last_check']['connected'],0)
