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

    def test_visual_evidence_uses_declared_files_and_does_not_change_ledger(self):
        self.assets['values.csv']=b'time_ps,temperature_K\n0,100\n1,200\n'
        self.doc['views']=[dict(id='thermal',title='Temperature',description='Synthetic diagnostic',
            figures=[dict(name='figure.png',role='paper')],metric_labels=[],
            tables=[dict(name='values.csv',role='reference',label='Temperature data',
                         columns=[dict(key='time_ps',label='Time (ps)'),
                                  dict(key='temperature_K',label='Temperature (K)')])])]
        self.save_closeout()
        before=self.ledger.events(self.b['id'])
        reply=self.client.get(self.url);self.assertEqual(reply.status_code,200,reply.text)
        view=reply.json()['report']['closeout']['views'][0]
        self.assertEqual(view['tables'][0]['rows'],[['0','100'],['1','200']])
        self.assertEqual(view['tables'][0]['total_rows'],2)
        self.assertFalse(view['tables'][0]['truncated'])
        self.assertEqual(self.ledger.events(self.b['id']),before)
        self.doc['views'][0]['tables'][0]['name']='../other.csv';self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_comparison_image_identity_comes_from_verified_bytes(self):
        self.assets['second.png']=self.assets['figure.png']
        self.doc['figures'].append(dict(name='second.png',label='Second',caption='Synthetic'))
        self.doc['views']=[dict(id='pair',title='Pair',description='Same bytes',figures=[
            dict(name='figure.png',role='reference',sha256='invented'),
            dict(name='second.png',role='agent')])]
        self.save_closeout()
        figures=self.client.get(self.url).json()['report']['closeout']['views'][0]['figures']
        self.assertEqual(figures[0]['sha256'],sha256(self.assets['figure.png']))
        self.assertEqual(figures[0]['sha256'],figures[1]['sha256'])
        self.assets['second.png']=b'different synthetic image';self.save_closeout()
        figures=self.client.get(self.url).json()['report']['closeout']['views'][0]['figures']
        self.assertNotEqual(figures[0]['sha256'],figures[1]['sha256'])

    def test_visual_evidence_rejects_unknown_role_metric_or_figure(self):
        valid=dict(id='curve',title='Curve',description='Synthetic',figures=[dict(name='figure.png',role='paper')])
        for change in [dict(figures=[dict(name='unknown.png',role='paper')]),
                       dict(figures=[dict(name='figure.png',role='invented')]),
                       dict(metric_labels=['fabricated metric'])]:
            self.doc['views']=[dict(valid,**change)];self.save_closeout()
            self.assertEqual(self.client.get(self.url).status_code,409)

    def test_visual_table_preview_is_explicitly_bounded(self):
        self.assets['values.csv']=('x\n'+''.join(f'{i}\n' for i in range(30))).encode()
        self.doc['views']=[dict(id='samples',title='Samples',description='Synthetic',
            tables=[dict(name='values.csv',role='agent',label='Data',columns=[dict(key='x',label='x')])])]
        self.save_closeout();reply=self.client.get(self.url)
        self.assertEqual(reply.status_code,200,reply.text)
        table=reply.json()['report']['closeout']['views'][0]['tables'][0]
        self.assertEqual(table['total_rows'],30);self.assertTrue(table['truncated'])
        self.assertEqual(table['rows'],[[str(i)] for i in [*range(6),*range(24,30)]])

    def test_tampered_raw_bytes_refuse_display_and_download(self):
        self.source.write_bytes(b'changed')
        self.assertEqual(self.client.get(self.url).status_code,409)
        self.assertEqual(self.client.get(f"/api/tasks/{self.task['id']}/closeout/files/figure.png").status_code,409)

    def test_postprocessing_bound_to_retained_source_and_derived_bytes(self):
        source=dict(self.doc['source'], request_id=self.b['id'])
        receipt=dict(status='posthoc_diagnostic', physics_simulation=False, scientific_pass=None,
                     plan=dict(source=source), files=[dict(name='render.png', size=len(self.assets['figure.png']),
                     sha256=sha256(self.assets['figure.png']))])
        self.assets['derived.json']=canonical(receipt)
        self.doc['postprocessing']=[dict(receipt_file='derived.json',
                                         artifacts=[dict(name='figure.png',source_name='render.png')])]
        self.doc['figures'][0]['kind']='structure'
        self.save_closeout()
        reply=self.client.get(self.url)
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertEqual(reply.json()['report']['closeout']['figures'][0]['kind'],'structure')
        self.assertEqual(reply.json()['report']['closeout']['source_sha256'],source['sha256'])
        source['request_id']='other-request';self.assets['derived.json']=canonical(receipt);self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)
        source['request_id']=self.b['id'];self.assets['derived.json']=canonical(receipt)
        self.assets['figure.png']=b'different';self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_downloaded_coverage_cannot_disagree_with_page(self):
        export=dict(doi=self.doc['doi'],paper_title=self.doc['title'],coverage=self.doc['coverage'])
        self.assets['coverage.json']=canonical(export);self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,200)
        export['coverage']=[]
        self.assets['coverage.json']=canonical(export);self.save_closeout()
        self.assertEqual(self.client.get(self.url).status_code,409)
        self.assertEqual(self.client.get(f"/api/tasks/{self.task['id']}/closeout/files/coverage.json").status_code,409)

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
