"""Verified evidence boundaries and model diagnosis, without HPC or paid calls."""
import unittest
from unittest.mock import Mock
from auto_lammps.failure_recovery import diagnose, failure_context
from auto_lammps.manifest import canonical, sha256


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
