"""Normal reference HTTP actions over existing synthetic source/plan services."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
from fastapi.testclient import TestClient

from auto_lammps.author_reference_workspace import AuthorReferenceWorkspace
from auto_lammps.deepseek import ModelError
from auto_lammps.manifest import canonical,sha256
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
from test_web import ORIGIN,HEADERS
import test_author_reference_plan as fixture


class AuthorWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.AuthorReferencePlanTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.task=self.fx.task['id'];self.source=self.fx.reader.service()
        self.association=dict(scope='reference_only',paper_id=self.fx.paper['id'],
            paper={key:self.fx.paper[key] for key in ('title','doi')},repository=self.fx.reader.receipt['repository'],
            commit=self.fx.reader.receipt['commit'],resource_receipt_sha256='f'*64,reading_approved=True,execution_authorized=False)
        self.binding=dict(paper_id=self.fx.paper['id'],format='acquisition_v1',reader=self.source,
            load_receipt=lambda:deepcopy(self.fx.reader.receipt),receipt_file_sha256='f'*64,
            load_association=lambda:deepcopy(self.association),verify_license=Mock(),
            selections=[dict(path='README.md',start_line=1,end_line=20),dict(path='workflow/example.txt',start_line=1,end_line=20)],
            invocation_paths=['README.md'],deployment=deepcopy(self.fx.input['deployment']),missing=[])
        self.model=lambda task:dict(enabled=True,client=self.fx.client,connection_revision=1,credential_generation=1)
        self.workspace=self.make()
        self.http=TestClient(create_app(self.fx.tasks,papers=self.fx.papers,author_workspace_service=self.workspace),base_url=ORIGIN)
        self.addCleanup(self.http.close);self.url='/api/tasks/'+self.task+'/author-plan'

    def make(self):
        return AuthorReferenceWorkspace(self.fx.tasks,self.fx.papers,self.fx.scopes,{self.task:self.binding},
            model_provider=self.model,connection_provider=self.fx.connection_provider,model_calls=self.fx.calls)

    def post(self,request='4'*32,note=''):
        return self.http.post(self.url,json=dict(request_id=request,revision_note=note),headers=HEADERS)

    def test_read_only_status_normal_prepare_full_plan_and_no_dispatch(self):
        status=self.http.get(self.url);self.assertEqual(status.status_code,200,status.text)
        self.assertTrue(status.json()['can_prepare']);self.assertEqual(self.fx.reader.calls,0);self.assertEqual(self.fx.sent,[])
        workflow=self.http.get('/api/tasks/'+self.task+'/paper-workflow').json()
        action=next(row for row in workflow['actions'] if row['id']=='prepare_A_plan')
        self.assertTrue(action['enabled']);self.assertEqual(action['endpoint'],self.url)
        result=self.post();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'prepared',result.text)
        self.assertEqual(result.json()['report']['reference_scope']['scope']['selected_ids'],['first','second'])
        self.assertEqual(self.fx.reader.calls,1);self.assertEqual(len(self.fx.sent),1)
        self.assertFalse(result.json()['can_submit']);self.assertFalse(result.json()['can_approve'])
        self.assertEqual(self.post().json()['state'],'prepared');self.assertEqual(self.fx.reader.calls,1)
        self.assertEqual(len(self.fx.sent),1)
        changed=self.post(note='changed request')
        self.assertEqual(changed.status_code,422,changed.text)
        self.assertEqual(len(self.fx.sent),1);self.assertEqual(self.fx.reader.calls,1)

    def test_retained_source_pairs_are_reused_after_restart_for_a_real_revision(self):
        self.assertEqual(self.post().json()['state'],'prepared')
        self.source.capture=Mock(side_effect=AssertionError('must reuse retained read proof'))
        restarted=self.make()
        result=restarted.prepare(self.task,'5'*32,revision_note='仅完善合成说明，保持范围')
        self.assertEqual(result['state'],'prepared',result)
        self.assertEqual(result['round'],2);self.source.capture.assert_not_called();self.assertEqual(len(self.fx.sent),2)
        self.assertEqual(result['revision']['complete_previous_answer'],self.fx.value)
        with self.fx.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM author_source_evidence').fetchone()[0],1)

    def test_wrong_paper_or_changed_license_blocks_before_remote_read_and_model(self):
        self.association['paper']['title']='another paper'
        result=self.post();self.assertEqual(result.status_code,422,result.text)
        self.assertFalse(result.json()['request_recorded']);self.assertEqual(self.fx.reader.calls,0)
        self.assertEqual(len(self.fx.sent),0)
        self.association['paper']['title']=self.fx.paper['title']
        self.binding['verify_license'].side_effect=TaskError('license no longer matches')
        self.assertEqual(self.post().status_code,422);self.assertEqual(self.fx.reader.calls,0)

    def test_source_cache_corruption_never_sends_or_silently_rereads(self):
        self.assertEqual(self.post().json()['state'],'prepared')
        with self.fx.tasks.transaction() as db:
            db.execute('DROP TRIGGER immutable_author_source_evidence_UPDATE')
            db.execute('UPDATE author_source_evidence SET document=?',('{}',))
        with self.assertRaises(TaskError):self.workspace.prepare(self.task,'5'*32,revision_note='update')
        self.assertEqual(self.fx.reader.calls,1);self.assertEqual(len(self.fx.sent),1)

    def test_no_binding_explains_real_missing_without_calls(self):
        empty=AuthorReferenceWorkspace(self.fx.tasks,self.fx.papers,self.fx.scopes,{},model_provider=self.model,connection_provider=self.fx.connection_provider)
        status=empty.status(self.task);self.assertFalse(status['can_prepare']);self.assertIn('尚未连接',status['disabled_reason'])
        self.assertEqual(self.fx.reader.calls,0);self.assertEqual(self.fx.sent,[])

    def test_invalid_pin_or_deployment_is_not_ready_and_returns_safe_error(self):
        original=self.source.policy['receipt_sha256']
        self.source.policy['receipt_sha256']='0'*64
        status=self.http.get(self.url)
        self.assertEqual(status.status_code,200,status.text)
        self.assertFalse(status.json()['can_prepare'])
        result=self.post();self.assertEqual(result.status_code,422,result.text)
        self.assertFalse(result.json()['request_recorded'])
        self.source.policy['receipt_sha256']=original
        self.binding['deployment']['resources']['gpu_allowed']=True
        self.assertFalse(self.http.get(self.url).json()['can_prepare'])
        self.assertEqual(self.post().status_code,422)
        self.assertEqual(self.fx.reader.calls,0);self.assertEqual(self.fx.sent,[])

    def test_unknown_call_does_not_reread_source_or_accept_new_request(self):
        self.fx.client.transport=Mock(side_effect=TimeoutError('private unknown'))
        result=self.post();self.assertEqual(result.json()['state'],'unknown',result.text)
        self.assertEqual(self.fx.reader.calls,1)
        again=self.post('5'*32);self.assertEqual(again.status_code,422,again.text)
        self.assertEqual(self.fx.reader.calls,1);self.assertEqual(self.fx.client.transport.call_count,1)

    def test_model_input_preflight_reports_capacity_and_keeps_private_diagnostics(self):
        self.workspace.planner.prepare=Mock(side_effect=ModelError('input_too_large'))
        result=self.post()
        self.assertEqual(result.status_code,422,result.text)
        self.assertIn('超过模型输入容量',result.json()['detail'])
        self.assertFalse(result.json()['request_recorded'])
        self.assertEqual(self.fx.sent,[])
        traces=list((self.fx.tasks.path.parent/'author-workspace-errors').glob('*.json'))
        self.assertEqual(len(traces),1)
        self.assertIn('input_too_large',traces[0].read_text())

    def test_browser_cannot_supply_scientific_source_or_allow_dispatch(self):
        for payload in (dict(source='private replacement'),dict(deployment={}),dict(execution_authorized=True)):
            result=self.http.post(self.url,json=dict(request_id='4'*32,**payload),headers=HEADERS)
            self.assertEqual(result.status_code,422,result.text)
        self.assertEqual(self.fx.reader.calls,0);self.assertEqual(self.fx.sent,[])

    def test_invalid_answer_is_visible_from_original_receipt_without_new_call(self):
        self.fx.value['coverage'][0]['citations'][0]['quote']='MISSING_QUOTE'
        self.assertEqual(self.post().json()['state'],'invalid_output')
        self.binding['verify_license'].side_effect=TaskError('license unavailable')
        data=self.workspace.status(self.task)
        self.assertFalse(data['can_prepare'])
        self.assertEqual(data['history'][0]['proposal'],self.fx.value)
        self.assertEqual(len(self.fx.sent),1);self.assertEqual(self.fx.reader.calls,1)

    def test_resource_status_and_pinned_text_read_do_not_consume_model_or_plan_rounds(self):
        url='/api/tasks/'+self.task+'/author-resources'
        status=self.http.get(url)
        self.assertEqual(status.status_code,200,status.text)
        self.assertFalse(status.json()['enabled'])  # Synthetic reader has no saved HPC transport.
        self.assertEqual(self.fx.reader.calls,0)
        reading=self.http.post(url+'/read',json={},headers=HEADERS)
        self.assertEqual(reading.status_code,200,reading.text)
        self.assertEqual(reading.json()['read_files'],2)
        self.assertFalse(reading.json()['calls_model']);self.assertFalse(reading.json()['submits_HPC'])
        self.assertEqual(self.workspace.planner.status(self.task)['used_rounds'],0)
        self.assertEqual(self.fx.sent,[])
        self.assertEqual(self.http.post(url+'/read',json={},headers=HEADERS).status_code,200)
        self.assertEqual(self.fx.reader.calls,1)  # Retained source proof, not another read.

    def test_cached_analysis_source_miss_does_not_read_or_call_model(self):
        before=self.workspace.planner.status(self.task)['used_rounds']
        result=self.workspace.cached_analysis_sources(self.task)
        self.assertFalse(result['available']);self.assertEqual(result['sources'],[])
        self.assertIsNone(result['source_binding']);self.assertTrue(result['missing'])
        self.assertIn('缓存尚未就绪',result['missing'][0])
        self.assertFalse(result['reads_remote']);self.assertFalse(result['calls_model'])
        self.assertEqual(self.fx.reader.calls,0);self.assertEqual(self.fx.sent,[])
        self.assertEqual(self.workspace.planner.status(self.task)['used_rounds'],before)
        with self.fx.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM author_source_evidence').fetchone()[0],0)

    def test_cached_analysis_sources_reuse_exact_proofs_as_reference_only_code(self):
        from auto_lammps.author_reference_plan import author_source_inputs
        from auto_lammps.condition_generation import source_bundle
        self.workspace.collect_sources(self.task)
        self.source.capture=Mock(side_effect=AssertionError('cached analysis must not access HPC'))
        result=self.workspace.cached_analysis_sources(self.task)
        self.assertTrue(result['available']);self.assertEqual(result['missing'],[])
        self.assertEqual(result['scope'],'reference_only');self.assertFalse(result['B_input'])
        self.assertEqual(len(source_bundle(result['sources'])),len(result['sources']))
        with self.fx.tasks.transaction() as db:
            row=db.execute('SELECT document,sha256 FROM author_source_evidence WHERE task_id=?',(self.task,)).fetchone()
        import json
        retained=json.loads(row['document']);checked=author_source_inputs(retained,self.task)
        self.assertEqual([(s['id'],s['text'],s['locator']) for s in result['sources']],
            [(s['id'],s['text'],s['locator']) for s in checked['sources']])
        self.assertTrue(all(s['origin']=='code' for s in result['sources']))
        proof=result['source_binding']
        self.assertEqual(proof['task_id'],self.task);self.assertEqual(proof['paper_id'],self.fx.paper['id'])
        self.assertEqual(proof['retained_evidence_sha256'],row['sha256'])
        self.assertEqual(proof['read_proofs_sha256'],sha256(canonical(checked['reads'])))
        self.assertEqual(proof['sources_sha256'],sha256(canonical(result['sources'])))
        self.assertEqual(self.fx.reader.calls,1);self.source.capture.assert_not_called()
        self.assertEqual(self.fx.sent,[]);self.assertEqual(self.workspace.planner.status(self.task)['used_rounds'],0)

    def test_cached_analysis_sources_reject_corruption_or_changed_paper_and_license_without_fallback(self):
        self.workspace.collect_sources(self.task)
        self.source.capture=Mock(side_effect=AssertionError('must not reread'))
        self.association['paper']['title']='another paper'
        result=self.workspace.cached_analysis_sources(self.task)
        self.assertFalse(result['available']);self.assertEqual(result['sources'],[])
        self.assertIn('关联回执',result['missing'][0])
        self.association['paper']['title']=self.fx.paper['title']
        self.binding['verify_license'].side_effect=TaskError('license unavailable')
        self.assertIn('license unavailable',self.workspace.cached_analysis_sources(self.task)['missing'])
        self.binding['verify_license'].side_effect=None
        with self.fx.tasks.transaction() as db:
            db.execute('DROP TRIGGER immutable_author_source_evidence_UPDATE')
            db.execute('UPDATE author_source_evidence SET document=?',('{}',))
        result=self.workspace.cached_analysis_sources(self.task)
        self.assertFalse(result['available']);self.assertEqual(result['sources'],[])
        self.assertIn('摘要不一致',result['missing'][0])
        self.source.capture.assert_not_called();self.assertEqual(self.fx.sent,[])

    def test_cached_analysis_sources_cannot_borrow_another_tasks_proof(self):
        self.workspace.collect_sources(self.task)
        other=self.fx.tasks.create('Independent ordinary research','No author answers','research')['id']
        self.source.capture=Mock(side_effect=AssertionError('must not read'))
        result=self.workspace.cached_analysis_sources(other)
        self.assertFalse(result['available']);self.assertEqual(result['sources'],[])
        self.assertIsNone(result['source_binding']);self.assertTrue(result['missing'])
        self.source.capture.assert_not_called();self.assertEqual(self.fx.sent,[])

    def test_resource_routes_reject_browser_supplied_paths_pins_or_execution_permission(self):
        url='/api/tasks/'+self.task+'/author-resources'
        for payload in ({'repository':'someone/else'},{'commit':'a'*40},{'remote_directory':'/tmp'},{'execution_authorized':True}):
            acquire=self.http.post(url,json=dict(request_id='b'*32,**payload),headers=HEADERS)
            self.assertEqual(acquire.status_code,422,acquire.text)
            read=self.http.post(url+'/read',json=payload,headers=HEADERS)
            self.assertEqual(read.status_code,422,read.text)
        self.assertEqual(self.fx.reader.calls,0);self.assertEqual(self.fx.sent,[])

if __name__=='__main__':unittest.main()
