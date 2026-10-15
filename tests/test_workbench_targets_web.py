"""Real HTTP/native publication/Adapter/accounting; only HTTP model transport is fake."""
from copy import deepcopy
import importlib.util
import json
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.ledger import Ledger
from auto_lammps.manifest import canonical, sha256
from auto_lammps.model_connections import ModelConnections
from auto_lammps.scientific_adapters import validate_prepared_messages
from auto_lammps.tasks import FIELDS
from auto_lammps.web import create_app
from test_ledger import H1, H2, POLICY, RESOURCE
from test_tasks import evidence
import test_reference_target_analysis_web as old_fixture
import test_workbench_recovery as fixture


ORIGIN='http://127.0.0.1:8765'
HEADERS={'Origin':ORIGIN,'X-Task-Review':'1'}


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class WorkbenchTargetsWebTests(unittest.TestCase):
    def setUp(self):
        import fitz
        self.f=fixture.NativeWorkbenchRecoveryTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.tasks,self.papers,self.task,self.paper=self.f.tasks,self.f.papers,self.f.task,self.f.paper
        # Register a real synthetic PDF before any native extraction. Its crop,
        # numeric items and committed publication are produced by native ports.
        document=fitz.open();page=document.new_page()
        page.insert_text((72,72),'Results. Sample C has hardness 4.0 GPa at 400 K.')
        page.draw_rect(fitz.Rect(72,115,320,250),fill=(.85,.9,1))
        page.draw_line((90,230),(300,140),color=(.1,.3,.7))
        page.insert_text((72,270),'Figure 1. Hardness of sample C.')
        generated=self.f.root/'visual.pdf';document.save(generated);document.close()
        self.f.source.write_bytes(generated.read_bytes())
        self.f.source_sha=sha256(self.f.source.read_bytes())
        self.f.bridge.runtime.database.upsert_paper(title=self.paper['title'],doi=self.paper['doi'],
            pdf_path=str(self.f.source),pdf_sha256=self.f.source_sha)
        self.f.config['bindings'][0]['pdf_sha256']=self.f.source_sha
        self.f.save_config();self.f.restart()
        self.bridge,self.binding=self.f.bridge,self.f.binding
        self.connections=ModelConnections(self.tasks,calls=self.f.calls,assistant_enabled=True)
        self.connections.save('deepseek-official','synthetic-model','synthetic-project-key')
        self.sent=[];self.invalid_inventory=False;self.unknown_publication=False
        self.transport=patch('auto_lammps.model_connections.https_transport',self.response)
        self.transport.start();self.addCleanup(self.transport.stop)
        self.client=self.new_client()
        self.base='/api/tasks/'+self.task['id']+'/workbench'
        self.request='d'*32

    def new_client(self,*,views=None):
        client=TestClient(create_app(self.tasks,papers=self.papers,model_client=self.f.client,
            model_connections=self.connections,paper_evidence_views=views,
            workbench_bridge=self.bridge,workbench_bindings={self.paper['id']:self.binding}),base_url=ORIGIN)
        self.addCleanup(client.close);return client

    def response(self,body,key,timeout):
        payload=json.loads(body);self.sent.append(payload)
        self.assertEqual(key,'synthetic-project-key')
        user=payload['messages'][-1]['content']
        try:context=json.loads(user)
        except ValueError:context={}
        if 'workbench_target_inventory_menu' in context:
            value=self.inventory_output(context)
            if self.invalid_inventory:value['coverage']['items'].pop()
        elif 'reference_target_dependency_menu' in context:
            citation=[dict(source_id='paper-page-1',quote='Figure 1. Hardness of sample C.')]
            value=dict(targets=[dict(target_id=t['id'],data_class='unclear',priority=1,
                reason='合成文本缺少具体求值方法。',citations=deepcopy(citation),dependency_ids=['shared'],
                missing=['Fixture does not establish the LAMMPS method.']) for t in context['inventory']['targets']],
                dependencies=[dict(id='shared',data_class='unclear',label='Synthetic source data',
                    description='Synthetic dependency',requires=[],citations=citation,
                    missing=['Fixture method unavailable.'])],limitations=['Synthetic HTTP test only.'])
        else:
            if self.unknown_publication:raise TimeoutError('Synthetic uncertain native request')
            if 'Verify these candidates:' in user:
                rows=json.loads(user.split('Verify these candidates:\n',1)[1].split('\n\nSource pages:',1)[0])
                value=dict(verdicts=[dict(candidate_id=r['candidate_id'],verdict='supported',
                    reason='Exact synthetic source.') for r in rows])
            elif 'original_caption' in user:
                rows=json.loads(user)['visual_evidence']
                value=dict(assets=[dict(asset_id=r['asset_id'],display_name='合成硬度图',
                    context_explanation='来源图呈现样品硬度。',physical_quantities=['hardness'],
                    materials=[],variables={},tags=['hardness']) for r in rows])
            else:
                value=dict(data=[dict(value_text='4.0',meaning='样品硬度',unit='GPa',
                    context_explanation='样品 C 在 400 K 条件下的测量',source_page=1,source_locator='Results',
                    source_excerpt='Sample C has hardness 4.0 GPa at 400 K',evidence_type='measured',
                    source_precision='exact_text')],findings=[],pending_tasks=[])
        return 200,canonical(dict(choices=[dict(finish_reason='stop',message=dict(role='assistant',
            content=json.dumps(value)))],usage=dict(prompt_tokens=8,completion_tokens=5,total_tokens=13)))

    def inventory_output(self,context):
        citation=[dict(source_id='paper-page-1',quote='Figure 1. Hardness of sample C.')]
        row=dict(id='fig1',label='Synthetic Fig. 1',parent='Fig. 1',locator='Page 1 Fig. 1',kind='figure',
            condition_group='fixture-condition',conditions='Synthetic conditions',resources='Synthetic resources',
            outputs='Synthetic retained figure data',sampling='Synthetic sample',analysis='Await dependency assessment',
            criterion='Synthetic comparison, no scientific pass claimed',availability='new_calculation',
            limitations='Fixture model output only')
        coverage={section:[dict(entity_uid=r['entity_uid'],target_ids=['fig1'],disposition='target',
            reason='合成条目映射至同一原图，不代替科学核验。',citations=deepcopy(citation))
            for r in context[section]] for section in ('items','visuals')}
        return dict(inventory=dict(version=1,paper=context['paper'],source_sha256=context['source_binding']['pdf_sha256'],
            coverage_note='Every native record mapped without truncation.',targets=[row]),
            target_evidence=[dict(target_id='fig1',priority=1,citations=citation)],coverage=coverage,
            limitations=['Synthetic model output; no scientific result.'])

    def post_targets(self,request=None,*,client=None,headers=None,**extra):
        return (client or self.client).post(self.base+'/targets',json=dict(request_id=request or self.request,**extra),
            headers=HEADERS if headers is None else headers)

    def publish(self):
        response=self.client.post(self.base+'/extract',json=dict(request_id='a'*32,
            source_sha256=self.binding.pdf_sha256),headers=HEADERS)
        self.assertEqual(response.status_code,202,response.text)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            reply=self.client.get(self.base+'/jobs/'+'a'*32)
            self.assertEqual(reply.status_code,200,reply.text)
            if reply.json()['state'] not in {'queued','dispatching'}:return reply.json()
            time.sleep(.01)
        self.fail('Synthetic native publication did not finish')

    def complete_inventory(self):
        published=self.publish()
        self.assertIn(published['state'],{'completed','completed_with_limitations'},published)
        response=self.post_targets();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'completed',response.json())
        return response.json()

    def test_unfinished_and_unknown_native_P_cannot_generate_targets_or_charge_again(self):
        before=self.f.calls.status()['used_requests']
        self.assertEqual(self.post_targets().status_code,422)
        menu=self.client.get(self.base+'/targets').json()
        self.assertFalse(menu['published_P_ready']);self.assertEqual(menu['history'],[])
        self.assertEqual(self.f.calls.status()['used_requests'],before)
        self.unknown_publication=True
        result=self.publish();self.assertNotIn(result['state'],{'completed','completed_with_limitations'})
        charged=self.f.calls.status()['used_requests'];self.assertGreater(charged,before)
        self.assertEqual(self.post_targets().status_code,422)
        self.assertEqual(self.f.calls.status()['used_requests'],charged)
        self.assertFalse(self.client.get(self.base+'/targets').json()['published_P_ready'])

    def test_native_publication_HTTP_inventory_uses_all_items_visuals_real_adapter_and_ledger(self):
        task_before=self.tasks.get(self.task['id'])
        result=self.complete_inventory()
        native=self.bridge.evidence(self.task['id'],self.binding)
        self.assertTrue(native['items']);self.assertTrue(native['visuals'])
        publication=self.bridge.history(self.task['id'])[-1]
        self.assertIn(publication['state'],{'completed','completed_with_limitations'})
        self.assertIn(publication['workbench_status'],{'completed','saved_index_pending'})
        payload=self.sent[-1];context=json.loads(payload['messages'][-1]['content'])
        self.assertEqual(context['items'],native['items']);self.assertEqual(context['visuals'],native['visuals'])
        self.assertEqual(context['publication']['receipt_sha256'],sha256(canonical(publication)))
        permitted={k:context[k] for k in ('task_id','paper_id','paper','role','sources','items','visuals','publication','source_binding')}
        self.assertEqual(validate_prepared_messages('workbench_target_inventory',payload['messages'],permitted)['input_check'],'passed')
        for section in ('items','visuals'):
            self.assertEqual([r['entity_uid'] for r in result['report']['coverage'][section]],
                [r['entity_uid'] for r in native[section]])
        ledger=self.f.calls.lookup(self.request)
        self.assertEqual(ledger['receipt']['state'],'completed')
        self.assertEqual(ledger['request_sha256'],ledger['receipt']['request_sha256'])
        self.assertEqual(result['usage']['total_tokens'],13)
        self.assertEqual(self.tasks.get(self.task['id']),task_before)
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['B_input'])

    def test_failed_inventory_history_and_readonly_restart_never_read_credentials(self):
        self.assertIn(self.publish()['state'],{'completed','completed_with_limitations'})
        self.invalid_inventory=True
        result=self.post_targets().json();self.assertEqual(result['state'],'invalid_output',result)
        charged=self.f.calls.status()['used_requests'];sent=len(self.sent)
        with patch.object(self.connections,'_read',side_effect=AssertionError('Read-only must not open credentials')):
            history=self.client.get(self.base+'/targets')
            self.assertEqual(history.status_code,200,history.text)
            self.assertEqual(history.json()['history'],[result])
            self.assertEqual(self.new_client().get(self.base+'/targets').json()['history'],[result])
        self.assertEqual(self.f.calls.status()['used_requests'],charged);self.assertEqual(len(self.sent),sent)

    def test_import_is_explicit_revision_bound_and_stale_import_preserves_task(self):
        result=self.complete_inventory();original=self.tasks.get(self.task['id'])
        self.assertNotIn('target_inventory',original)
        url=self.base+'/targets/'+self.request+'/import'
        response=self.client.post(url,json=dict(revision=original['revision']),headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        changed=response.json();self.assertEqual(changed['revision'],original['revision']+1)
        self.assertEqual(changed['target_inventory'],result['report']['inventory'])
        response=self.client.post(url,json=dict(revision=original['revision']),headers=HEADERS)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.tasks.get(self.task['id']),changed)

    def test_import_cannot_retroactively_change_frozen_task_or_reserved_dispatch_history(self):
        result=self.complete_inventory();doc=self.tasks.get(self.task['id'])
        doc=self.tasks.import_target_inventory(doc['id'],doc['revision'],result['report']['inventory'])
        for field in FIELDS:doc=self.tasks.add_candidate(doc['id'],doc['revision'],field,evidence('SYNTHETIC_CONDITION'))
        doc=self.tasks.confirm(doc['id'],doc['revision'],list(FIELDS))
        doc=self.tasks.select_targets(doc['id'],doc['revision'],['fig1'],'')
        doc=self.tasks.freeze(doc['id'],doc['revision'])
        url=self.base+'/targets/'+self.request+'/import'
        self.assertEqual(self.client.post(url,json=dict(revision=doc['revision']),headers=HEADERS).status_code,422)
        self.assertEqual(self.tasks.get(doc['id']),doc)

    def test_existing_reference_dispatch_blocks_import_without_model_or_physics(self):
        self.complete_inventory()
        ledger=Ledger(self.f.root/'reference-ledger.sqlite');ledger.create_campaign('synthetic',POLICY)
        self.papers.ledger=ledger
        evaluation=ledger.register_evaluation('synthetic',task_sha256=H1,repetition=0,role='reference',system_sha256=H2)
        self.papers.bind_reference_evaluation(self.paper['id'],self.task['id'],evaluation)
        row=ledger.reserve(evaluation,'synthetic-intent',H2,RESOURCE)
        ledger.begin_dispatch(row['id']);ledger.uncertain(row['id'],dict(synthetic=True))
        doc=self.tasks.get(self.task['id']);sent=len(self.sent)
        response=self.client.post(self.base+'/targets/'+self.request+'/import',
            json=dict(revision=doc['revision']),headers=HEADERS)
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.tasks.get(self.task['id']),doc);self.assertEqual(len(self.sent),sent)

    def test_cross_origin_and_browser_scientific_payload_cannot_start_or_import(self):
        for headers in ({},{'Origin':'https://different.invalid','X-Task-Review':'1'}):
            self.assertEqual(self.post_targets(headers=headers).status_code,403)
        for extra in (dict(inventory={}),dict(publication=dict(state='published')),dict(sources=[]),
                dict(items=[]),dict(visuals=[]),dict(B_answer='forged')):
            self.assertEqual(self.post_targets(**extra).status_code,422)
        reply=self.client.post(self.base+'/targets/'+self.request+'/import',
            json=dict(revision=self.task['revision'],inventory={}),headers=HEADERS)
        self.assertEqual(reply.status_code,422,reply.text)
        self.assertEqual(self.f.calls.status()['used_requests'],0);self.assertEqual(self.sent,[])

    def test_generated_native_inventory_feeds_existing_dependency_route_without_legacy_package(self):
        self.complete_inventory();before=self.tasks.get(self.task['id'])
        response=self.client.post('/api/tasks/'+self.task['id']+'/reference-target-analysis',
            json=dict(request_id='e'*32),headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertEqual(result['state'],'completed',result)
        context=json.loads(self.sent[-1]['messages'][-1]['content'])
        self.assertEqual(context['inventory']['source_sha256'],self.f.source_sha)
        self.assertEqual(context['scientific_adapter']['stage'],'reference_target_analysis')
        self.assertEqual(context['source_binding']['package_manifest_sha256'],None)
        self.assertEqual([t['target_id'] for t in result['report']['targets']],['fig1'])
        self.assertEqual(self.tasks.get(self.task['id']),before)

    def test_real_pdf_change_marks_inventory_stale_and_blocks_import_without_new_calls(self):
        self.complete_inventory();before=self.tasks.get(self.task['id'])
        charged=self.f.calls.status()['used_requests'];sent=len(self.sent)
        self.f.source.write_bytes(self.f.source.read_bytes()+b'\n% synthetic changed PDF identity\n')
        with patch.object(self.connections,'_read',side_effect=AssertionError('No credentials for source projection')):
            response=self.client.get(self.base+'/targets')
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertFalse(result['published_P_ready'])
        self.assertEqual(result['source']['state'],'stale');self.assertIsNone(result['source']['report'])
        self.assertEqual(result['history'][0]['original_state'],'completed')
        imported=self.client.post(self.base+'/targets/'+self.request+'/import',
            json=dict(revision=before['revision']),headers=HEADERS)
        self.assertEqual(imported.status_code,422,imported.text)
        self.assertEqual(self.tasks.get(self.task['id']),before)
        self.assertEqual(self.f.calls.status()['used_requests'],charged);self.assertEqual(len(self.sent),sent)

    def test_legacy_package_inventory_still_has_priority_over_native_fallback(self):
        # Reuse the actual package/native reader fixture, not a mocked view.
        f=old_fixture.ReferenceTargetAnalysisWebTests();f.setUp();self.addCleanup(f.doCleanups)
        # Its configured model transport is intentionally the only fake edge.
        response=f.post()
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertEqual(result['state'],'completed',result)
        self.assertEqual(result['report']['inventory'],f.document['target_inventory'])
        self.assertEqual(result['source_binding']['package_manifest_sha256'],sha256((f.folder/'manifest.json').read_bytes()))
        self.assertEqual(f.bridge.history(f.task['id']),[])


if __name__=='__main__':unittest.main()
