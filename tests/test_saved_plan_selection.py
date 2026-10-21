"""Ordinary saved-plan choice, zero new model calls and unchanged science bytes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from auto_lammps.agent_candidates import CandidateError
from auto_lammps.authorization import candidate_check
from auto_lammps.manifest import Snapshot, canonical, sha256
from auto_lammps.ledger import Resources
from auto_lammps.web import create_app
from test_deepseek import response
from test_web import ORIGIN, HEADERS
import test_supplier_checked_candidate_review as supplier_fixture


class SavedPlanSelectionTests(unittest.TestCase):
    def setUp(self):
        self.f=supplier_fixture.SupplierCheckedCandidateReviewTests();self.f.max_input_bytes=131072
        self.f.resources_override=Resources(32,604800,8*1024**3,4*1024**3)
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.service,self.task,self.client=self.f.service,self.f.doc['id'],self.f.client
        self.f.enqueue();self.service.run(self.task)
        self.original=self.service.history.get(self.task)['result']['snapshot_sha256']
        self.proposal=deepcopy(self.f.f.value)
        self.issue='Synthetic unresolved executed sampling limitation, not a passed scientific review.'
        def rejected(body,*args):
            _,raw=self.f.valid_review(body,*args)
            value=json.loads(json.loads(raw)['choices'][0]['message']['content']);value['issues']=[self.issue]
            return 200,response(value)
        self.f.transport.side_effect=rejected
        self.f.event('failed',{'detail':'Synthetic earlier execution failed; retained prepared snapshot.'})
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.task,self.f.doc['revision'],request_id='c'*32)
        self.service.run(self.task)
        self.service.history.reserve_proposal(self.task,'d'*32,'synthetic-sixth-clarification')
        self.f.transport.side_effect=lambda *a:(200,response(dict(summary='Synthetic sixth clarification',
            questions=[{'question':'Unresolved synthetic question','why':'Unresolved','suggestion':'None'}],
            structure=None,potential_pin=None,workflow=None,analysis=None)))
        answer=self.client.complete_json('d'*32,[{'role':'user','content':'Synthetic sixth JSON clarification.'}])
        self.f.event('model_proposal',dict(request_id='d'*32,condition_sha256=self.f.before['condition_sha256'],
            proposal_sha256=answer['receipt']['output_sha256']))
        self.f.event('clarification',dict(summary='Synthetic retained sixth clarification',questions=[]))
        self.service.resources_provider=Mock(return_value=dict(remaining_core_seconds=506016,
            approval_sha256='e'*64,gpu_allowed=False))
        self.before=self.service.history.get(self.task);self.calls=self.client.calls.status()['used_requests']
        self.f.transport.reset_mock()

    def select(self,**kwargs):
        return self.service.select_saved_plan(self.task,self.f.doc['revision'],snapshot_sha256=self.original,
            request_id='f'*32,accept_unresolved_review=True,**kwargs)

    def test_http_selection_keeps_six_rounds_original_receipt_script_and_unresolved_review(self):
        old=Snapshot(self.service.snapshots/self.original,self.original).verify()
        with patch.object(self.client,'complete_json',side_effect=AssertionError('Saved choice cannot call AI')):
            with TestClient(create_app(self.f.tasks,candidate_service=self.service,model_client=self.client),base_url=ORIGIN) as web:
                view=web.get(f'/api/tasks/{self.task}/plan').json()
                self.assertEqual(view['saved_plan_options'][0]['proposal_round'],5)
                self.assertEqual(view['saved_plan_options'][0]['review_feedback']['report']['issues'],[self.issue])
                result=web.post(f'/api/tasks/{self.task}/plan/select-saved',headers=HEADERS,json=dict(
                    revision=self.f.doc['revision'],request_id='f'*32,snapshot_sha256=self.original,
                    accept_unresolved_review=True))
                self.assertEqual(result.status_code,200,result.text)
                selected=web.get(f'/api/tasks/{self.task}/plan').json()
        final=self.service.history.get(self.task)
        self.assertEqual(final['state'],'prepared')
        self.assertEqual(final['proposal_rounds'],self.before['proposal_rounds'])
        self.assertEqual(final['proposal_rounds']['used'],6)
        self.assertEqual(final['events'][:len(self.before['events'])],self.before['events'])
        self.assertEqual(selected['latest_review_feedback']['report']['issues'],[self.issue])
        self.assertEqual(selected['saved_plan_selection']['scientific_review_status'],'unresolved')
        snapshot=Snapshot(self.service.snapshots/final['result']['snapshot_sha256'],final['result']['snapshot_sha256'])
        manifest=snapshot.verify();generation=json.loads((snapshot.path/'generation.json').read_bytes())
        self.assertEqual(generation['proposal'],self.proposal)
        self.assertEqual(generation['plan_reviews'],[])  # never forge a passed review
        self.assertEqual(manifest['resources']['wall_seconds'],15780)
        self.assertLessEqual(manifest['resources']['wall_seconds']*32,506016)
        for name in ('in.lammps','structure.data','analysis.json'):
            self.assertEqual(next(f['sha256'] for f in old['files'] if f['path']==name),
                             next(f['sha256'] for f in manifest['files'] if f['path']==name))
        candidate_check(snapshot,max_atoms=self.service.max_atoms)
        self.assertEqual(self.client.calls.status()['used_requests'],self.calls)
        self.f.transport.assert_not_called()
        self.assertEqual(self.select()['result']['snapshot_sha256'],snapshot.digest)

    def test_missing_acceptance_other_snapshot_or_missing_budget_stops_without_ai(self):
        with self.assertRaises(CandidateError):
            self.service.select_saved_plan(self.task,self.f.doc['revision'],snapshot_sha256=self.original,
                request_id='f'*32,accept_unresolved_review=False)
        with self.assertRaises(CandidateError):
            self.service.select_saved_plan(self.task,self.f.doc['revision'],snapshot_sha256='0'*64,
                request_id='f'*32,accept_unresolved_review=True)
        self.service.resources_provider=None
        with self.assertRaises(CandidateError):self.select()
        self.assertEqual(self.service.history.get(self.task),self.before)
        self.assertEqual(self.client.calls.status()['used_requests'],self.calls)
        self.f.transport.assert_not_called()

