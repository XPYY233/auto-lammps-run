"""Explicit unknown replay over synthetic P snapshots; no API or HPC access."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import unittest
from unittest.mock import Mock,patch

from auto_lammps.deepseek import request_body
from auto_lammps.manifest import canonical,sha256
from auto_lammps.reference_target_recovery import ReferenceTargetAnalysis
from auto_lammps.tasks import TaskError
import test_reference_targets as fixtures


class ReferenceTargetUnknownReissueTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ReferenceTargetTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.task=self.f.task['id'];self.parent=self.f.request_id;self.child='e'*32
        self.original=deepcopy(self.f.inputs)
        self.historical=Mock(side_effect=lambda identifier,**pins:deepcopy(self.original))
        self.service=self.make();self.f.service=self.service
        self.transport=self.f.client.transport

    def make(self,**kwargs):
        return ReferenceTargetAnalysis(self.f.tasks,self.f.papers,self.f.provider,
            connection_provider=self.f.connection_provider,
            historical_reference_provider=kwargs.get('historical',self.historical),
            max_suggestions=kwargs.get('max_suggestions',3))

    def unknown(self):
        self.f.client.transport=Mock(side_effect=TimeoutError('synthetic unknown transport'))
        result=self.f.analyze(service=self.service)
        self.assertEqual(result['state'],'unknown')
        self.parent_call=deepcopy(self.f.calls.lookup(self.parent))
        self.parent_receipt=deepcopy(result['receipt'])
        self.parent_context=deepcopy(self.service._stored(self.task,self.parent)[1])
        self.f.client.transport=self.transport
        return result

    def reissue(self,request=None,parent=None,service=None,**flags):
        return (service or self.service).reissue_unknown(self.task,request or self.child,self.f.client,
            unknown_reissue_of=parent or self.parent,supplier_checked=flags.get('supplier_checked',True),
            retain_unknown_charge=flags.get('retain_unknown_charge',True),
            connection_revision=flags.get('connection_revision',1),
            credential_generation=flags.get('credential_generation',1))

    def test_explicit_reissue_sends_exact_original_body_once_and_preserves_unknown_charge(self):
        self.unknown();wire=[]
        def capture(body,*args):wire.append(body);return self.transport(body,*args)
        self.f.client.transport=capture
        options=self.service.unknown_reissue_options(self.task,self.f.client,
            connection_revision=1,credential_generation=1)
        self.assertTrue(options[0]['enabled']);self.assertEqual(self.f.sent,[])
        result=self.reissue();self.assertEqual(result['state'],'completed',result)
        old=self.parent_context
        expected=request_body(self.f.calls.config,old['messages'],model=self.f.client.model,thinking=self.f.client.thinking)
        self.assertEqual(wire,[expected]);self.assertEqual(sha256(wire[0]),old['request_sha256'])
        self.assertEqual(self.f.calls.lookup(self.parent),self.parent_call)
        self.assertEqual(self.service.get(self.task,self.parent)['receipt'],self.parent_receipt)
        parent=self.service.get(self.task,self.parent)
        self.assertEqual(parent['state'],'unknown');self.assertFalse(parent['blocking'])
        self.assertTrue(parent['unknown_reissue']['retain_unknown_charge'])
        self.assertTrue(result['original_source_preserved']);self.assertTrue(result['historical_source'])
        self.assertEqual(result['unknown_reissue']['unknown_reissue_of'],self.parent)
        self.assertEqual(self.f.calls.status()['used_requests'],2)
        self.assertEqual(len(self.service.history(self.task)),2)
        self.assertEqual(self.reissue(),result);self.assertEqual(len(wire),1)
        disabled=self.service.unknown_reissue_options(self.task,self.f.client,
            connection_revision=1,credential_generation=1)[0]
        self.assertFalse(disabled['enabled']);self.assertEqual(disabled['request_id'],self.child)
        with self.assertRaises(TaskError):self.reissue(request='f'*32)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.f.tasks.transaction() as db:db.execute('DELETE FROM reference_target_unknown_reissues')

    def test_snapshot_replay_and_completed_get_do_not_use_latest_p(self):
        self.unknown()
        self.f.provider.side_effect=AssertionError('latest P must not replace original snapshot')
        result=self.reissue();self.assertEqual(result['state'],'completed',result)
        self.assertEqual(self.service.get(self.task,self.child)['state'],'completed')
        self.assertEqual(self.service._stored(self.task,self.child)[1]['input'],self.parent_context['input'])
        self.assertTrue(self.historical.call_count)
        for call in self.historical.call_args_list:
            self.assertEqual(call.kwargs['source_binding'],self.parent_context['input']['source_binding'])
            self.assertEqual(call.kwargs['inventory_sha256'],sha256(canonical(self.parent_context['input']['inventory'])))

    def test_unknown_child_cannot_be_reissued_or_bypass_normal_unknown_gate(self):
        self.unknown();self.f.client.transport=Mock(side_effect=TimeoutError('synthetic second unknown'))
        result=self.reissue();self.assertEqual(result['state'],'unknown')
        self.assertTrue(result['blocking']);self.assertTrue(self.service.get(self.task,self.parent)['blocking'])
        with self.assertRaises(TaskError):self.reissue(request='f'*32)
        with self.assertRaises(TaskError):self.reissue(request='f'*32,parent=self.child)
        with self.assertRaises(TaskError):self.f.analyze(service=self.service,request_id='f'*32)
        self.assertEqual(self.f.calls.status()['used_requests'],2)
        self.assertEqual(self.f.client.transport.call_count,1)

    def test_supplier_flags_are_strict_and_original_id_is_never_reserved_again(self):
        self.unknown()
        for value in (False,1,'true',None):
            with self.subTest(value=value),self.assertRaises(TaskError):self.reissue(supplier_checked=value)
            with self.assertRaises(TaskError):self.reissue(retain_unknown_charge=value)
        with self.assertRaises(TaskError):self.reissue(request=self.parent)
        self.assertEqual(self.f.calls.status()['used_requests'],1)
        self.assertEqual(len(self.service.history(self.task)),1)

    def test_missing_history_or_changed_original_evidence_never_replays(self):
        self.unknown()
        missing=self.make(historical=None)
        with self.assertRaises(TaskError):self.reissue(service=missing)
        self.original['sources'][0]['text']+=' changed historical snapshot'
        with self.assertRaises(TaskError):self.reissue()
        self.assertEqual(self.f.calls.status()['used_requests'],1)
        self.assertEqual(len(self.service.history(self.task)),1)

    def test_changed_model_identity_or_known_response_is_not_eligible(self):
        self.unknown();self.f.connection.update(connection_revision=2,credential_generation=2)
        with self.assertRaises(TaskError):self.reissue(connection_revision=2,credential_generation=2)
        self.f.connection.update(connection_revision=1,credential_generation=1)
        lookup=self.f.calls.lookup
        def changed(identifier):
            value=deepcopy(lookup(identifier))
            if identifier==self.parent:value['receipt']['response_sha256']='f'*64
            return value
        with patch.object(self.f.calls,'lookup',side_effect=changed),self.assertRaises(TaskError):self.reissue()
        self.assertEqual(self.f.calls.status()['used_requests'],1)

    def test_same_original_task_three_round_cap_is_retained(self):
        self.unknown();limited=self.make(max_suggestions=1)
        # Persisted cap belongs to the original task, not a newly made service.
        self.assertEqual(self.reissue(service=limited)['state'],'completed')
        self.f.value['targets'].pop()
        third=self.f.analyze(service=self.service,request_id='f'*32)
        self.assertEqual(third['state'],'invalid_output')
        with self.assertRaisesRegex(TaskError,'三次'):self.f.analyze(service=self.service,request_id='a'*32)
        self.assertEqual(self.f.calls.status()['used_requests'],3)
        self.assertEqual(len(self.service.history(self.task)),3)

    def test_reissue_cannot_cross_tasks_or_use_ordinary_research(self):
        self.unknown()
        other=self.f.tasks.create('Other reference','No borrowed answer','reproduction')['id']
        with self.assertRaises(TaskError):self.service.reissue_unknown(other,self.child,self.f.client,
            unknown_reissue_of=self.parent,supplier_checked=True,retain_unknown_charge=True,
            connection_revision=1,credential_generation=1)
        ordinary=self.f.tasks.create('Ordinary research','No P','research')['id']
        self.historical.reset_mock()
        with self.assertRaises(TaskError):self.service.reissue_unknown(ordinary,self.child,self.f.client,
            unknown_reissue_of=self.parent,supplier_checked=True,retain_unknown_charge=True,
            connection_revision=1,credential_generation=1)
        self.historical.assert_not_called();self.assertEqual(self.f.calls.status()['used_requests'],1)

    def test_original_scientific_controller_and_body_are_not_migrated(self):
        import auto_lammps.reference_targets as original
        before=sha256(__import__('pathlib').Path(original.__file__).read_bytes())
        self.unknown();self.reissue()
        self.assertEqual(sha256(__import__('pathlib').Path(original.__file__).read_bytes()),before)
        child=self.service._stored(self.task,self.child)[1]
        self.assertEqual(child['controller_sha256'],before)
        self.assertEqual(child['messages'],self.parent_context['messages'])
        self.assertEqual(child['scientific_adapter'],self.parent_context['scientific_adapter'])

    def test_duplicate_clicks_during_transport_share_original_child_and_one_reservation(self):
        self.unknown();entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def waiting(*args):
            entered.set()
            if not release.wait(5):raise TimeoutError('synthetic test wait')
            return self.transport(*args)
        self.f.client.transport=waiting
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(self.reissue)
            self.assertTrue(entered.wait(3))
            duplicate=self.reissue();self.assertEqual(duplicate['request_id'],self.child)
            self.assertEqual(duplicate['state'],'dispatching')
            with self.assertRaises(TaskError):self.reissue(request='f'*32)
            release.set();self.assertEqual(future.result(timeout=5)['state'],'completed')
        self.assertEqual(self.f.calls.status()['used_requests'],2)
        self.assertEqual(len(self.f.sent),1)

    def test_authorized_unsent_crash_resumes_same_child_without_another_round(self):
        self.unknown()
        with patch.object(self.f.client,'complete_json',side_effect=KeyboardInterrupt('synthetic local stop')):
            with self.assertRaises(KeyboardInterrupt):self.reissue()
        self.assertEqual(self.f.calls.status()['used_requests'],1)
        self.assertEqual(len(self.service.history(self.task)),2)
        result=self.reissue(service=self.make())
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.f.calls.status()['used_requests'],2)
        self.assertEqual(len(self.service.history(self.task)),2)

    def test_completed_child_call_reconciles_after_local_crash_without_resending(self):
        self.unknown()
        with patch.object(self.service,'_accept',side_effect=KeyboardInterrupt('synthetic receipt interruption')):
            with self.assertRaises(KeyboardInterrupt):self.reissue()
        saved=deepcopy(self.f.calls.lookup(self.child))
        self.f.key.side_effect=AssertionError('completed child must never resend')
        result=self.reissue(service=self.make())
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.f.calls.lookup(self.child),saved)
        self.assertEqual(self.f.calls.status()['used_requests'],2)
        self.assertEqual(len(self.f.sent),1)

    def test_full_persisted_three_round_budget_refuses_even_authorized_reissue(self):
        self.f.value['targets'].pop()
        self.f.analyze(service=self.service)
        self.f.analyze(service=self.service,request_id=self.child)
        self.parent='f'*32
        self.f.client.transport=Mock(side_effect=TimeoutError('synthetic third unknown'))
        original=self.f.analyze(service=self.service,request_id=self.parent)
        self.assertEqual(original['state'],'unknown')
        options=self.service.unknown_reissue_options(self.task,self.f.client,
            connection_revision=1,credential_generation=1)
        self.assertFalse(options[0]['enabled'])
        with self.assertRaisesRegex(TaskError,'三次'):self.reissue(request='a'*32)
        self.assertEqual(self.f.calls.status()['used_requests'],3)
        self.assertEqual(len(self.service.history(self.task)),3)


if __name__=='__main__':unittest.main()
