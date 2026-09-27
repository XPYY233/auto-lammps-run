"""Synthetic operator reports: no models, simulations, network or real targets."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore, TaskError
from auto_lammps.papers import PaperStore
from auto_lammps.ledger import Ledger
from auto_lammps.manifest import canonical, sha256
from auto_lammps.operator_workspace import ReferenceViews, ModelPreferences
from auto_lammps.web import create_app
from test_ledger import POLICY, RESOURCE, H1, H2
from test_web import HEADERS, ORIGIN


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.tasks=TaskStore(self.root/'tasks.sqlite');self.ledger=Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('synthetic',POLICY)
        self.papers=PaperStore(self.tasks,ledger=self.ledger)
        self.paper=self.papers.add('Synthetic reference','10.1234/example','Synthetic scope','No physics')
        self.paper=self.papers.select(self.paper['id'],self.paper['revision'])
        self.task=self.tasks.create('Synthetic task','No target simulation','reproduction')
        self.paper=self.papers.link_task(self.paper['id'],self.paper['revision'],self.task['id'])
        self.evaluation=self.ledger.register_evaluation('synthetic',task_sha256=H1,repetition=0,role='reference',system_sha256=H2)
        self.papers.bind_reference_evaluation(self.paper['id'],self.task['id'],self.evaluation)
        self.req=self.ledger.reserve(self.evaluation,'first',H2,RESOURCE)
        self.ledger.begin_dispatch(self.req['id']);self.ledger.accepted(self.req['id'],'123',{'synthetic':True})
        self.ledger.observe(self.req['id'],'123','completed',{'synthetic':True});self.ledger.account(self.req['id'],10,H2)
        self.directory=self.root/'reports';self.directory.mkdir(mode=0o700)
        self.folder=self.directory/self.task['id'];self.folder.mkdir(mode=0o700)
        evidence={'runtime':{'job_id':'123','elapsed_seconds':10,'cores':1,'hours':10/3600,'core_hours':10/3600,'B_max_core_hours':1,'prior_failure_core_seconds':0},
                  'sample':{'peak':{'values':{'max':3}},'curve':[[0,0],[.5,3]]},'private_path':'PRIVATE_SOURCE_SENTINEL'}
        raw=canonical(evidence);self.write('analysis.json',raw)
        self.report=dict(version=1,task_id=self.task['id'],paper_id=self.paper['id'],evaluation=self.evaluation,request_id=self.req['id'],job_id='123',manifest_sha256=H2,
                         title=self.paper['title'],doi=self.paper['doi'],scientific_status='diagnostic',at='2026-01-01',scope='Synthetic',summary='Synthetic',limitations=['Synthetic'],stages=[],analysis_file='analysis.json',
                         files=[dict(name='analysis.json',label='Synthetic',size=len(raw),sha256=sha256(raw))],
                         metrics=[dict(label='Synthetic peak',paper=4,reference=3,unit='GPa',method='Synthetic',channel='sample',operation='peak')],curves=[dict(channel='sample',label='Synthetic')])
        paper_raw=canonical({'doi':self.paper['doi'],'primary_stage_reference_proposal':{'tensile_strength_GPa':4}})
        self.write('paper.json',paper_raw);self.report['files'].append(dict(name='paper.json',label='Synthetic paper evidence',size=len(paper_raw),sha256=sha256(paper_raw)))
        self.report['paper_evidence_file']='paper.json';self.report['metrics'][0]['paper_key']='tensile_strength_GPa'
        self.save();self.views=ReferenceViews(self.directory,self.papers)
        self.client=TestClient(create_app(self.tasks,papers=self.papers,reference_views=self.views),base_url=ORIGIN);self.addCleanup(self.client.close)
        self.url=f"/api/tasks/{self.task['id']}/reference-result"

    def write(self,name,raw):
        p=self.folder/name;p.write_bytes(raw);p.chmod(0o600)

    def save(self):self.write('report.json',canonical(self.report))

    def test_reference_binding_does_not_freeze_or_authorize_agent(self):
        self.assertEqual(self.tasks.get(self.task['id'])['status'],'draft')
        self.papers.bind_reference_evaluation(self.paper['id'],self.task['id'],self.evaluation)
        agent=self.ledger.register_evaluation('synthetic',task_sha256=H1,repetition=1,role='agent',system_sha256=H2)
        with self.assertRaises(TaskError):self.papers.bind_reference_evaluation(self.paper['id'],self.task['id'],agent)
        self.assertEqual(self.papers.get(self.paper['id'])['status'],'in_progress')
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],1)

    def test_read_reference_differences_without_dispatch_or_private_context(self):
        events=self.ledger.events(self.req['id'])
        with patch.object(Ledger,'begin_dispatch',side_effect=AssertionError('GET must not submit')):
            r=self.client.get(self.url);self.assertEqual(r.status_code,200,r.text)
            metric=r.json()['report']['metrics'][0]
            self.assertEqual(r.json()['report']['agent_progress']['dispatch_claims'],0)
            self.assertEqual((metric['absolute_difference'],metric['relative_difference_percent']),(1,25))
            self.assertNotIn('PRIVATE_SOURCE_SENTINEL',r.text)
            self.assertEqual(self.client.get(self.url).json(),r.json())
        self.assertEqual(events,self.ledger.events(self.req['id']))
        self.assertEqual(self.client.post(self.url,json=self.report,headers=HEADERS).status_code,405)

    def test_changed_evidence_or_comparison_fails_closed(self):
        self.report['metrics'][0]['reference']=2;self.save()
        self.assertEqual(self.client.get(self.url).status_code,409)
        self.report['metrics'][0]['reference']=3;self.save();self.write('analysis.json',b'{}')
        self.assertEqual(self.client.get(self.url).status_code,409)
        self.assertEqual(self.client.get(self.url+'/files/analysis.json').status_code,409)

    def test_agent_progress_is_live_and_unavailable_is_not_zero(self):
        agent=self.ledger.register_evaluation('synthetic',task_sha256=H1,repetition=1,role='agent',system_sha256=H2)
        req=self.ledger.reserve(agent,'agent-first',H2,RESOURCE)
        self.ledger.begin_dispatch(req['id'])
        paper=self.papers.get(self.paper['id'])
        paper['evaluations'].append(self.ledger.evaluation_snapshot(agent) | {'task_id':self.task['id'],'available':True})
        with patch.object(self.papers,'get',return_value=paper):
            progress=self.views.get(self.task['id'])['agent_progress']
            self.assertEqual(progress['dispatch_claims'],1)
            self.assertEqual(progress['stage'],'已有计算记录')
        paper['evaluations'][-1]={'id':agent,'task_id':self.task['id'],'available':False}
        with patch.object(self.papers,'get',return_value=paper):
            progress=self.views.get(self.task['id'])['agent_progress']
            self.assertIsNone(progress['dispatch_claims'])
            self.assertFalse(progress['available'])

    def test_cross_paper_and_unlisted_download_are_rejected(self):
        self.assertEqual(self.client.get(self.url+'/files/not-listed.md').status_code,409)
        self.report['doi']='10.1234/other';self.save()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_changed_paper_target_rejected(self):
        self.report['metrics'][0]['paper']=3;self.save()
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_symlink_artifact_rejected(self):
        p=self.folder/'analysis.json';p.unlink();target=self.root/'elsewhere';target.write_text('{}');p.symlink_to(target)
        self.assertEqual(self.client.get(self.url).status_code,409)

    def test_preference_persists_without_models_budget_or_credentials(self):
        with patch('auto_lammps.deepseek.DeepSeekClient.generate',side_effect=AssertionError('No model call'),create=True):
            initial=self.client.get('/api/model-preference').json()
            for i,provider in enumerate(['glm','anthropic','openai','deepseek-official']):
                reply=self.client.post('/api/model-preference',json={'provider':provider,'model':'synthetic-id','revision':i},headers=HEADERS)
                self.assertEqual(reply.status_code,200,reply.text)
                self.assertFalse(reply.json()['activates_runtime'])
            reopened=ModelPreferences(TaskStore(self.tasks.path)).get()
            self.assertEqual((reopened['provider'],reopened['model'],reopened['revision']),('deepseek-official','synthetic-id',4))
            self.assertFalse(self.client.get('/api/schema').json()['model_calls_enabled'])
            self.assertEqual(self.client.post('/api/model-preference',json={'provider':'glm','model':'x','revision':0},headers=HEADERS).status_code,409)
            self.assertEqual(self.client.post('/api/model-preference',json={'provider':'glm','model':'x','revision':4,'api_key':'synthetic'},headers=HEADERS).status_code,422)
            self.assertEqual(self.client.post('/api/model-preference',json={'provider':'glm','model':'x','revision':4},headers={'Origin':'https://example.invalid'}).status_code,403)
