"""Saved GUI settings route existing adapters; no actual SSH/model/physics."""
from dataclasses import asdict
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from auto_lammps.execution_jobs import load_execution_jobs
from auto_lammps.hpc_connections import HPCConnections
from auto_lammps.hpc_transport import SavedHPCTransport, bind_request
from auto_lammps.ledger import Conflict
from auto_lammps.manifest import canonical
from auto_lammps.slurm_read import SlurmReader
import test_execution as execution


class SavedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f=execution.ExecutionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.hpc=HPCConnections(self.f.tasks)
        self.profile=dict(label='Synthetic saved cluster',host='login.example.edu',port=2222,
            username='scientist',work_directory=str(self.f.remote.root),partition='synthetic',account='',authentication='private_key')
        self.hpc.save(self.profile,0,private_key='synthetic-private-key',known_hosts='synthetic-host-key')
        c=self.f.controller
        self.config=dict(snapshots_directory=str(c.snapshots),collections_directory=str(c.following.analysis.collector.directory),
            reports_directory=str(c.following.analysis.directory),audit_directory=str(self.f.root/'saved-audit'),
            stage_endpoint=asdict(c.staging.client.endpoint),submit_endpoint=asdict(c.submission.scheduler.endpoint),
            collect_endpoint=asdict(c.following.analysis.collector.endpoint),environment=asdict(c.environment),
            authorization=dict(directory=str(c.authorization.directory),**self.f.pins),runtime_profile_path=None,
            max_polls=3,interval_seconds=15,query_max_bytes=1024,task_evaluations={self.f.doc['id']:self.f.evaluation},
            hpc_connection_revision=1)

    def load(self,**changes):
        path=self.f.root/'saved-deployment.json';path.write_bytes(canonical({**self.config,**changes}));path.chmod(0o600)
        jobs=load_execution_jobs(self.f.tasks,self.f.ledger,path);self.addCleanup(jobs.close);return jobs

    def point_fixture(self,jobs):
        self.f.controller=jobs.controller;self.f.following=jobs.controller.following

    def test_saved_profile_routes_upload_submit_query_and_download_after_settings_change(self):
        jobs=self.load();self.point_fixture(jobs);self.f.authorize_fixture()
        jobs.enqueue(self.f.doc['id'],self.f.doc['revision'])
        self.hpc.save({**self.profile,'host':'other.example.edu','port':2200,'username':'other'},1,
                      private_key='other-synthetic-key',known_hosts='other-synthetic-host')
        reopened=self.load();self.point_fixture(reopened)
        with self.f.transports():
            result=reopened.advance(self.f.doc['id'])
            self.assertEqual(result['job']['state'],'analyzed')
            self.assertEqual(result['job']['dispatch_count'],1)
            calls=[mock.call_args.args[0] for mock in (self.f.upload,self.f.dispatch,self.f.download)]
        reader=reopened.controller.following.reconciliation.reader
        with patch('auto_lammps.slurm_read._capture',return_value=dict(returncode=0,failure='',stdout='',stderr='')) as query:
            reader._query(['squeue','--noheader']);calls.append(query.call_args.args[0])
        for argv in calls:
            self.assertEqual(argv[-6:-1],['-p','2222','-l','scientist','login.example.edu'])
            self.assertIn('StrictHostKeyChecking=yes',argv);self.assertIn('IdentitiesOnly=yes',argv)
            self.assertNotIn('synthetic-private-key',' '.join(argv));self.assertNotIn('other.example.edu',argv)
        self.assertNotIn('synthetic-private-key',json.dumps(result))
        bindings=[e for e in self.f.ledger.events(self.f.request_id) if e['kind']=='hpc_connection_bound']
        self.assertEqual(len(bindings),1);self.assertEqual(json.loads(bindings[0]['payload'])['revision'],1)
        self.assertEqual(reopened.get(self.f.doc['id'])['id'],jobs.get(self.f.doc['id'])['id'])

    def test_changed_deployment_revision_cannot_redirect_queued_request(self):
        first=self.load();first.enqueue(self.f.doc['id'],self.f.doc['revision'])
        self.hpc.save({**self.profile,'host':'other.example.edu'},1,private_key='other-key')
        second=self.load(hpc_connection_revision=2)
        with self.f.transports():
            result=second.advance(self.f.doc['id']);self.f.upload.assert_not_called();self.f.dispatch.assert_not_called()
        self.assertEqual(result['job']['reason'],'deployment_changed')
        with self.assertRaises(Conflict):second.controller.prepare(self.f.doc['id'],self.f.evaluation)
        with self.assertRaises(Conflict):bind_request(self.f.ledger,self.f.request_id,SlurmReader('fixture-login',self.f.root/'legacy-query'))

    def test_legacy_upload_intent_cannot_be_adopted_by_new_connection(self):
        self.f.ledger.begin_staging(self.f.request_id)
        jobs=self.load()
        with self.assertRaises(Conflict):jobs.controller.prepare(self.f.doc['id'],self.f.evaluation)

    def test_credential_tampering_and_alias_mismatch_fail_before_network(self):
        jobs=self.load();transport=jobs.controller.staging.client.transport
        with self.assertRaises(ValueError):transport.command('another-alias',['true'])
        row=self.hpc._row(1);path=self.hpc.directory/(row['credential_id']+'.json')
        path.write_bytes(canonical(dict(private_key='changed',known_hosts='',certificate='')))
        with self.assertRaises(ValueError):transport.command('fixture-login',['true'])

    def test_missing_revision_invalid_revision_root_partition_and_account_rejected(self):
        for revision in (None,True,0,99):
            with self.subTest(revision=revision),self.assertRaises(ValueError):self.load(hpc_connection_revision=revision)
        for field,value in [('partition','different'),('account','different')]:
            with self.subTest(field=field),self.assertRaises(ValueError):self.load(environment={**self.config['environment'],field:value})
        self.hpc.save({**self.profile,'work_directory':'/unrelated'},1)
        with self.assertRaises(ValueError):self.load(hpc_connection_revision=2)

    def test_loading_is_read_only_for_job_ledger_and_does_not_connect(self):
        before=self.f.ledger.events(self.f.request_id)
        with patch('auto_lammps.hpc_connections.ssh_probe') as probe:
            one=self.load();two=self.load();probe.assert_not_called()
        self.assertEqual(one.config_sha256,two.config_sha256)
        self.assertEqual(self.f.ledger.events(self.f.request_id),before)
        self.assertIsNone(one.thread)
