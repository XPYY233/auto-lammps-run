"""Verified evidence boundaries and model diagnosis, without HPC or paid calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from auto_lammps.failure_recovery import diagnose, failure_context
from auto_lammps.manifest import canonical, sha256
import test_execution as execution


class DiagnosisTests(unittest.TestCase):
    def test_real_client_json_contract_without_network(self):
        import test_agent_candidates as candidates
        f=candidates.AgentCandidateTests();f.setUp();self.addCleanup(f.doCleanups)
        value=dict(summary='原因',evidence=['ERROR: failure'],cause='原因',repair='修订',proposed_lesson='待验证')
        f.transport.side_effect=lambda *args:(200,candidates.response(value))
        result=diagnose(f.client,dict(request_id='failed',logs=[dict(tail='ERROR: failure')]),{})
        self.assertEqual(result['receipt']['state'],'completed')
        self.assertEqual(f.transport.call_count,1)

    def test_diagnosis_is_accounted_proposed_and_evidence_grounded(self):
        evidence=dict(request_id='failed',logs=[dict(tail='ERROR: Compute used in variable between runs is not current')])
        value=dict(summary='未更新',evidence=['Compute used in variable between runs is not current'],
            cause='调用状态错误',repair='修改消费者',proposed_lesson='定义不是调用')
        client=Mock();client.complete_json.return_value=dict(value=value,
            receipt=dict(state='completed',output_sha256=sha256(canonical(value))))
        record=diagnose(client,evidence,dict(workflow='old'))
        self.assertEqual(record['validation_status'],'proposed_not_verified')
        self.assertEqual(record['failure_request_id'],'failed')
        value['evidence']=['Invented error'];client.complete_json.return_value['receipt']['output_sha256']=sha256(canonical(value))
        with self.assertRaisesRegex(ValueError,'actual supplied'):diagnose(client,evidence,{})

    def test_failure_reader_rejects_other_evaluation_before_collection(self):
        from unittest.mock import patch
        jobs=Mock();jobs.tasks.get.return_value=dict(revision=1)
        jobs.ledger.get.return_value=dict(evaluation='other')
        jobs.ledger.evaluation_snapshot.return_value=dict(identity=dict(role='reference',task='other'))
        with patch('auto_lammps.agent_candidates.research_inputs',return_value=dict(condition_record_sha256='ours')):
            with self.assertRaisesRegex(ValueError,'frozen research'):failure_context(jobs,'task')
        jobs.controller.following.analysis.collector.fetch.assert_not_called()


class CancelledFailureContextTests(unittest.TestCase):
    def setUp(self):
        self.f = execution.ExecutionTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.task, self.parent = self.f.doc['id'], self.f.request_id
        self.jobs = SimpleNamespace(tasks=self.f.tasks, ledger=self.f.ledger, controller=self.f.controller,
            get=Mock(return_value=dict(id='synthetic-job')),
            current_request_id=Mock(return_value=self.parent))
        folder = self.f.root/'synthetic-failure-collection'/'payload'
        (folder/'output').mkdir(parents=True)
        logs = {'output/log.lammps': b'Synthetic progress stopped\nERROR: synthetic runtime condition\n',
                'output/not-a-permitted-log.txt': b'PRIVATE_P_A_OR_SCORE_DIFFERENCE_CANARY'}
        files = []
        for name, raw in logs.items():
            (folder/name).write_bytes(raw)
            files.append(dict(path=name, size=len(raw), sha256=sha256(raw)))
        self.receipt = dict(state='collected', directory=str(folder.parent), header=dict(files=files))
        self.fetch = Mock(return_value=self.receipt)
        patcher = patch.object(self.f.following.analysis.collector, 'fetch', self.fetch)
        patcher.start(); self.addCleanup(patcher.stop)

    def finish(self, state='cancelled', *, account=True):
        ledger = self.f.ledger
        ledger.begin_dispatch(self.parent); ledger.accepted(self.parent, '123', {})
        ledger.observe(self.parent, '123', state, {})
        if account: ledger.account(self.parent, 1, 'a'*64)

    def test_cancelled_context_requires_grant_and_retains_actual_scheduler_state_and_only_b_logs(self):
        self.finish()
        self.assertIsNone(failure_context(self.jobs, self.task))
        self.fetch.assert_not_called()
        execution.grant_cancelled_development(self.f)
        before = self.f.ledger.get(self.parent)
        events = self.f.ledger.events(self.parent)
        result = failure_context(self.jobs, self.task)
        self.assertEqual(result['request_id'], self.parent)
        self.assertEqual(result['scheduler_state'], 'cancelled')
        self.assertEqual(result['scientific_status'], 'not_evaluated')
        self.assertEqual([row['name'] for row in result['logs']], ['output/log.lammps'])
        self.assertNotIn('PRIVATE_P_A_OR_SCORE_DIFFERENCE_CANARY', canonical(result).decode())
        self.assertIn('ERROR: synthetic runtime condition', result['logs'][0]['tail'])
        self.assertTrue(result['failed_proposal'])
        self.assertEqual(self.f.ledger.get(self.parent), before)
        self.assertEqual(self.f.ledger.events(self.parent), events)
        self.fetch.assert_called_once_with(self.parent)

    def test_unaccounted_cancelled_never_queries_grant_or_collects(self):
        self.finish(account=False)
        with patch.object(self.f.ledger, 'formal_b_development_supplement',
                          side_effect=AssertionError('No accounted parent')):
            self.assertIsNone(failure_context(self.jobs, self.task))
        self.fetch.assert_not_called()

    def test_failed_context_keeps_existing_behavior_without_supplement(self):
        self.finish('failed')
        with patch.object(self.f.ledger, 'formal_b_development_supplement',
                          side_effect=AssertionError('Ordinary failed path must be unchanged')):
            result = failure_context(self.jobs, self.task)
        self.assertEqual(result['scheduler_state'], 'failed')
        self.fetch.assert_called_once_with(self.parent)

    def test_grant_for_another_parent_does_not_admit_cancellation(self):
        self.finish()
        with patch.object(self.f.ledger, 'formal_b_development_supplement', return_value=None) as getter:
            self.assertIsNone(failure_context(self.jobs, self.task))
        getter.assert_called_once_with(self.f.evaluation, self.parent)
        self.fetch.assert_not_called()
