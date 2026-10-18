"""Actual user routes must reuse the upstream port and isolate P from B."""
import unittest
import time
import threading
from dataclasses import replace
from types import SimpleNamespace
from fastapi.testclient import TestClient
import test_workbench_bridge as fixture
from auto_lammps.model_connections import ModelConnections
from auto_lammps.web import create_app


class WebWorkbenchTests(unittest.TestCase):
    def setUp(self):
        fixture.WorkbenchBridgeTests.setUp(self)
        self.provider_client = self.client
        self.runtime = replace(self.runtime, native_source_reader=lambda _:self.source['pdf_sha256'])
        self.bridge.runtime = self.runtime
        self.connections = ModelConnections(self.tasks, calls=self.calls, assistant_enabled=True)
        self.connections.save('deepseek-official', 'synthetic-model', 'synthetic-project-key')
        self.connections.client = lambda *args, **kwargs: self.provider_client
        self.runtime.exports.export = lambda **kwargs: SimpleNamespace(
            content=b'synthetic-csv', content_type='text/csv; charset=utf-8',
            filename='paper-evidence.csv')
        self.client = TestClient(create_app(self.tasks, papers=self.papers,
            model_client=self.provider_client, model_connections=self.connections,
            workbench_bridge=self.bridge, workbench_bindings={self.paper['id']:self.binding}),
            base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.base='/api/tasks/'+self.task['id']+'/workbench'
        self.headers={'Origin':'http://127.0.0.1:8765', 'X-Task-Review':'1'}

    def wait_job(self, request_id):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            reply=self.client.get(self.base+'/jobs/'+request_id)
            self.assertEqual(reply.status_code,200,reply.text)
            if reply.json()['state'] not in {'queued','dispatching'}:
                return reply.json()
            time.sleep(.01)
        self.fail('Synthetic background extraction did not finish')

    def test_user_can_extract_view_and_download_without_mutating_B(self):
        before=self.tasks.get(self.task['id'])
        status=self.client.get(self.base).json()
        self.assertTrue(status['enabled']); self.assertEqual(self.outgoing, [])
        request=dict(request_id='c'*32, source_sha256=self.binding.pdf_sha256)
        response=self.client.post(self.base+'/extract',json=request,headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.wait_job(request['request_id'])['state'],'completed')
        result=self.client.get(self.base).json()
        self.assertEqual(len(result['messages']),1)
        self.assertEqual(result['evidence']['role'],'paper_reference_human_only')
        self.assertEqual(self.tasks.get(self.task['id']),before)
        self.assertEqual(len(self.outgoing),1)
        self.client.post(self.base+'/extract',json=request,headers=self.headers)
        self.assertEqual(len(self.outgoing),1)
        reply=self.client.get(self.base+'/export/item/11?source_sha256='+self.binding.pdf_sha256)
        self.assertEqual(reply.content,b'synthetic-csv')
        self.assertEqual(reply.headers['content-disposition'], 'attachment; filename="paper-evidence.csv"')

    def test_browser_cannot_inject_pdf_context_or_change_source(self):
        request=dict(request_id='c'*32,source_sha256='f'*64)
        self.assertEqual(self.client.post(self.base+'/extract',json=request,headers=self.headers).status_code,409)
        request['source_sha256']=self.binding.pdf_sha256
        request['context']={'pdf_path':'/private/forged.pdf', 'B_answer':'answer'}
        self.assertEqual(self.client.post(self.base+'/extract',json=request,headers=self.headers).status_code,422)
        self.assertEqual(self.outgoing,[])

    def test_unrelated_task_does_not_enable_extraction_or_download(self):
        other=self.tasks.create('Unrelated','Ordinary research','research')
        url='/api/tasks/'+other['id']+'/workbench'
        self.assertFalse(self.client.get(url).json()['enabled'])
        response=self.client.post(url+'/extract',json=dict(request_id='c'*32,
            source_sha256=self.binding.pdf_sha256),headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.outgoing,[])

    def test_absent_installed_bridge_is_an_explicit_gap(self):
        with TestClient(create_app(self.tasks,papers=self.papers),
                base_url='http://127.0.0.1:8765') as client:
            status=client.get(self.base).json()
            self.assertFalse(status['enabled'])
            self.assertIsNone(status['evidence'])

    def test_completed_continuation_proofs_are_read_only_status_not_cost_settlement(self):
        proof=dict(unknown_request_id='d'*32,superseded_by='e'*32,
            task_id=self.task['id'],source_sha256=self.binding.pdf_sha256,
            data_continuation_completed=True,unknown_charge_unsettled=True,
            repair_visuals_only=True,automatic_retry=False)
        self.bridge.completed_unknown_reissues=lambda identifier:[proof] if identifier==self.task['id'] else []
        before=self.tasks.get(self.task['id'])
        status=self.client.get(self.base)
        self.assertEqual(status.status_code,200,status.text)
        self.assertEqual(status.json()['completed_unknown_reissues'],[proof])
        self.assertEqual(self.tasks.get(self.task['id']),before)
        self.assertEqual(self.outgoing,[])

    def test_invalid_completed_continuation_proof_fails_closed_without_hiding_saved_status(self):
        from auto_lammps.tasks import TaskError
        def invalid(identifier):raise TaskError('Synthetic sealed proof mismatch')
        self.bridge.completed_unknown_reissues=invalid
        response=self.client.get(self.base)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['completed_unknown_reissues'],[])
        self.assertEqual(self.outgoing,[])

    def test_invalid_workbench_source_does_not_hide_other_reference_routes(self):
        from auto_lammps.tasks import TaskError
        def unavailable(*args):
            raise TaskError('Synthetic source mismatch')
        self.bridge.evidence = unavailable
        status = self.client.get(self.base)
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()['enabled'])
        self.assertIn('既有 P/A 记录仍可查看', status.json()['disabled_reason'])
        self.assertEqual(self.client.get('/api/tasks/'+self.task['id']+'/paper-workflow').status_code, 200)
        self.assertEqual(self.outgoing, [])

    def test_invalid_export_artifact_is_rejected_before_http_download(self):
        self.runtime.exports.export = lambda **kwargs: SimpleNamespace(
            content=b'synthetic-csv', content_type='text/html', filename='../source.csv')
        response = self.client.get(self.base+'/export/item/11?source_sha256='+self.binding.pdf_sha256)
        self.assertEqual(response.status_code, 422)

    def test_recovery_route_checks_source_and_never_accepts_native_tokens(self):
        request=dict(request_id='c'*32,source_sha256=self.binding.pdf_sha256,
                     resume_of='d'*32,job_token='forged-native-token')
        self.assertEqual(self.client.post(self.base+'/extract',json=request,
            headers=self.headers).status_code,422)
        del request['job_token'];request['source_sha256']='f'*64
        self.assertEqual(self.client.post(self.base+'/extract',json=request,
            headers=self.headers).status_code,409)
        request['source_sha256']=self.binding.pdf_sha256;request['resume_of']='invalid'
        self.assertEqual(self.client.post(self.base+'/extract',json=request,
            headers=self.headers).status_code,422)
        self.assertEqual(self.outgoing,[])

    def test_recovery_options_are_read_only_and_user_action_routes_bound_origin(self):
        original='d'*32
        self.bridge.recovery_options=lambda *args,**kwargs:[dict(
            resume_of=original,enabled=True,reused_model_calls=24,reason='Saved stage')]
        self.bridge._recovery_context=lambda identifier,origin,*args,**kwargs: (
            {} if identifier==self.task['id'] and origin==original else self.fail('Wrong recovery identity'))
        before=self.tasks.get(self.task['id'])
        self.assertEqual(self.client.get(self.base).json()['recoveries'][0]['resume_of'],original)
        received=[]
        self.bridge.extract=lambda *args,**kwargs:received.append((args,kwargs)) or dict(state='failed')
        request=dict(request_id='c'*32,source_sha256=self.binding.pdf_sha256,resume_of=original)
        response=self.client.post(self.base+'/extract',json=request,headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        self.wait_job(request['request_id'])
        self.assertEqual(received[0][1]['resume_of'],original)
        self.assertFalse(received[0][1]['force_rescan'])
        self.assertEqual(self.tasks.get(self.task['id']),before)
        self.assertEqual(self.outgoing,[])

    def test_displayed_recovery_claim_without_native_evidence_cannot_dispatch(self):
        self.bridge.recovery_options=lambda *args,**kwargs:[dict(
            resume_of='d'*32,enabled=True,reused_model_calls=24,reason='Unverified synthetic display claim')]
        request=dict(request_id='f'*32,source_sha256=self.binding.pdf_sha256,resume_of='d'*32)
        response=self.client.post(self.base+'/extract',json=request,headers=self.headers)
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.outgoing,[])

    def test_queue_returns_before_model_and_user_can_read_other_tasks(self):
        entered,release=threading.Event(),threading.Event()
        execute=self.runtime.ports.executor.execute
        def blocked(**kwargs):
            entered.set()
            if not release.wait(5):raise RuntimeError('Synthetic gate timed out')
            return execute(**kwargs)
        self.runtime.ports.executor.execute=blocked
        self.addCleanup(release.set)
        request=dict(request_id='e'*32,source_sha256=self.binding.pdf_sha256)
        response=self.client.post(self.base+'/extract',json=request,headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.outgoing,[])
        self.assertEqual(self.client.get('/api/tasks').status_code,200)
        status=self.client.get(self.base).json()
        self.assertEqual(status['jobs'][0]['request_id'],request['request_id'])
        self.assertEqual(status['jobs'][0]['state'],'dispatching')
        self.client.post(self.base+'/extract',json=request,headers=self.headers)
        self.assertEqual(self.outgoing,[])
        release.set()
        self.assertEqual(self.wait_job(request['request_id'])['state'],'completed')
        self.assertEqual(len(self.outgoing),1)


if __name__=='__main__':
    unittest.main()
