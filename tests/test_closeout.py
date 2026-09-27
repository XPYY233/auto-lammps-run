"""Scoped acceptance is read-only; synthetic values never imply real science."""
import json
import unittest
from unittest.mock import patch
from auto_lammps.manifest import canonical, sha256
from auto_lammps.raw_outputs import RawOutputs
from auto_lammps.scalar_analysis import analyze_scalar
import test_operator_workspace as workspace
from test_ledger import H1, H2, RESOURCE
from test_web import HEADERS


class CloseoutTests(unittest.TestCase):
    write = workspace.WorkspaceTests.write
    save = workspace.WorkspaceTests.save

    def setUp(self):
        workspace.WorkspaceTests.setUp(self)
        self.agent = self.ledger.register_evaluation('synthetic', task_sha256=H1, repetition=1, role='agent', system_sha256=H2)
        # Synthetic controller binding; no new production registration interface.
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO paper_evaluations VALUES (?,?,?,?)', (self.paper['id'], self.task['id'], self.agent, H1))
        self.b = self.ledger.reserve(self.agent, 'b', H2, RESOURCE)
        self.ledger.begin_dispatch(self.b['id']);self.ledger.accepted(self.b['id'], '456', {'synthetic':True})
        self.ledger.observe(self.b['id'], '456', 'completed', {'synthetic':True});self.ledger.account(self.b['id'], 10, H2)
        raw=b'# Synthetic scalar\n# TimeStep v_strain v_stress\n1 0.1 1\n2 0.2 2\n3 0.3 3.5\n'
        self.source=self.root/'stress.dat';self.source.write_bytes(raw);self.source.chmod(0o600)
        proof=dict(request_id=self.b['id'],job_id='456',manifest_sha256=H2,
                   files=[dict(name='stress.dat',kind='output',size=len(raw),sha256=sha256(raw))])
        receipt=self.root/'receipt.json';receipt.write_bytes(canonical(proof));receipt.chmod(0o600)
        outputs=RawOutputs(self.tasks,self.papers)
        source_id=outputs.publish(self.task['id'],self.b['id'],'stress.dat',self.source,receipt)
        table=dict(file='stress.dat',format='lammps_ave_time_scalar',headers=['# Synthetic scalar','# TimeStep v_strain v_stress'],
                   columns=[dict(name='step',unit='step',source='TimeStep'),dict(name='strain',unit='1',source='v_strain'),dict(name='stress',unit='GPa',source='v_stress')],
                   steps=dict(first=1,last=3,stride=1))
        operations=[dict(id='peak',method='summary',file='stress.dat',x='strain',y='stress',window=[0,.5])]
        self.doc=dict(doi=self.paper['doi'],title=self.paper['title'],scientific_status='diagnostic',formal_blind=False,
            request_id=self.b['id'],job_id='456',manifest_sha256=H2,
            acceptance=dict(status='accepted_by_user',source='explicit_user_message',date='2026-01-02',scope='One synthetic condition',record_file='acceptance.md'),
            source=dict(id=source_id,name='stress.dat',size=len(raw),sha256=sha256(raw)),table=table,operations=operations,
            analysis_plan_sha256=analyze_scalar(raw,table,operations)['plan_sha256'],
            metrics=[dict(reference_index=0,operation_id='peak')],curve=dict(x='strain',y='stress',reference_index=0),
            coverage=[dict(target='Fig. 1',content='Synthetic',evidence='One condition',status='Other conditions missing',additional_work='No new runs authorized')],
            figures=[dict(name='figure.png',label='Synthetic original',caption='No digitized P curve')],limitations=['Not a formal score'],private_path='DO_NOT_EXPOSE')
        self.closefolder=self.folder/'closeout';self.closefolder.mkdir(mode=0o700)
        self.manifest=dict(version=1,task_id=self.task['id'],doi=self.paper['doi'],reference_report_sha256=sha256((self.folder/'report.json').read_bytes()),evidence_file='evidence.json',files=[])
        self.assets={'evidence.json':canonical(self.doc),'acceptance.md':b'Explicit scoped synthetic acceptance','figure.png':b'synthetic image'}
        self.save_closeout()

    def save_closeout(self):
        self.assets['evidence.json']=canonical(self.doc)
        self.manifest['files']=[]
        for name,content in self.assets.items():
            p=self.closefolder/name;p.write_bytes(content);p.chmod(0o600)
            self.manifest['files'].append(dict(name=name,label=name,size=len(content),sha256=sha256(content)))
        p=self.closefolder/'manifest.json';p.write_bytes(canonical(self.manifest));p.chmod(0o600)

    def test_recomputes_b_and_preserves_ledger_and_scientific_status(self):
        before=self.ledger.events(self.b['id'])
        reply=self.client.get(self.url);self.assertEqual(reply.status_code,200,reply.text)
        report=reply.json()['report'];c=report['closeout'];m=c['metrics'][0]
        self.assertEqual((m['P'],m['A'],m['B'],m['absolute_PA'],m['absolute_AB'],m['absolute_PB']),(4,3,3.5,1,.5,.5))
        self.assertEqual(c['acceptance']['status'],'accepted_by_user');self.assertEqual(report['scientific_status'],'diagnostic')
        self.assertFalse(c['formal_blind']);self.assertNotIn('DO_NOT_EXPOSE',reply.text)
        self.assertEqual(before,self.ledger.events(self.b['id']))
        self.assertEqual(self.papers.get(self.paper['id'])['status'],'in_progress')
        self.assertEqual(self.client.post(self.url,json={},headers=HEADERS).status_code,405)
        download=self.client.get(f"/api/tasks/{self.task['id']}/closeout/files/figure.png")
        self.assertEqual(download.content,b'synthetic image')

    def test_tampered_raw_bytes_refuse_display_and_download(self):
        self.source.write_bytes(b'changed')
        self.assertEqual(self.client.get(self.url).status_code,409)
        self.assertEqual(self.client.get(f"/api/tasks/{self.task['id']}/closeout/files/figure.png").status_code,409)

    def test_wrong_job_or_manifest_rejected(self):
        for key in ('job_id','manifest_sha256'):
            original=self.doc[key];self.doc[key]='wrong';self.save_closeout()
            self.assertEqual(self.client.get(self.url).status_code,409)
            self.doc[key]=original

    def test_cannot_upgrade_diagnostic_to_formal_pass(self):
        self.doc['scientific_status']='passed';self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_reference_revision_mismatch_rejected(self):
        self.report['summary']='Changed';self.save()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_retained_plan_tampering_is_rejected(self):
        self.doc['operations'][0]['window']=[0,.2];self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_artifact_changed_or_symlink_rejected(self):
        p=self.closefolder/'figure.png';p.unlink();p.symlink_to(self.source)
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_no_acceptance_record_is_not_implicit_acceptance(self):
        self.assets['acceptance.md']=b'';self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_other_task_cannot_retrieve_artifact(self):
        other=self.tasks.create('Other','Synthetic','research')
        self.assertEqual(self.client.get(f"/api/tasks/{other['id']}/closeout/files/figure.png").status_code,409)
