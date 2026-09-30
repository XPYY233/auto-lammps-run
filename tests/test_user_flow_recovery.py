"""Recovery and multi-geometry contracts; no target physics or network."""
from copy import deepcopy
import unittest
from auto_lammps.agent_candidates import validate_proposal, CandidateError, render_candidate_script
from auto_lammps.ledger import Conflict
import test_agent_candidates as candidate_tests
import test_ledger as ledger_tests
import test_execution_jobs as execution_tests

class CandidateRecoveryTests(unittest.TestCase):
    setUp=candidate_tests.AgentCandidateTests.setUp
    # Reuse fixture setup only, not all tests of the parent class.
    def test_empty_analysis_plan_is_never_discarded(self):
        for plan in ({'tables':[], 'operations':[]},None):
            value=deepcopy(self.value)
            value['analysis'].pop('plan',None)
            if plan is not None:value['analysis']['plan']=plan
            with self.assertRaises(CandidateError):
                validate_proposal(value,max_atoms=10000,require_analysis_plan=True)

    def test_three_geometries_delete_after_relax_and_append_output(self):
        from test_analysis import PLAN
        value=deepcopy(self.value)
        value['additional_structures']=[{'id':x,'structure':deepcopy(value['structure'])} for x in ('medium','large')]
        value['analysis']={'quantity':'synthetic','method':'last', 'files':['trajectory.dump'],'plan':deepcopy(PLAN)}
        stage='minimize 0 1e-6 10 100\ngroup vacancy id 5\ndelete_atoms group vacancy compress no\nminimize 0 1e-6 10 100\n'
        value['workflow']=stage+'load_structure medium\n'+stage+'load_structure large\n'+stage+'print "1 2" append /output/trajectory.dump'
        screen=validate_proposal(value,max_atoms=10000,require_analysis_plan=True)
        self.assertEqual(screen['calculation_commands'],6)
        script=render_candidate_script(value,'metal',['pair_style zero 2.0','pair_coeff * *']).decode()
        self.assertEqual(script.count('\nclear\n'),2)
        self.assertIn('read_data structure-medium.data',script)
        self.assertEqual(script.count('pair_style zero 2.0'),3)
        for bad in ('delete_atoms group all compress no','delete_atoms random count 1 yes all NULL 99','load_structure ../secret'):
            rejected=deepcopy(value);rejected['workflow']=bad+'\n'+value['workflow']
            with self.assertRaises(CandidateError):validate_proposal(rejected,max_atoms=10000)

class LedgerRecoveryTests(unittest.TestCase):
    setUp=ledger_tests.LedgerTests.setUp
    tearDown=ledger_tests.LedgerTests.tearDown
    register=ledger_tests.LedgerTests.register
    reserve=ledger_tests.LedgerTests.reserve
    def test_cancel_retains_staged_or_unknown_storage(self):
        for uploaded in (False,True):
            row=self.reserve(key='staged' if uploaded else 'unknown')
            self.ledger.begin_staging(row['id'])
            if uploaded:self.ledger.staging_result(row['id'],evidence_sha256='d'*64)
            self.assertTrue(self.ledger.cancel_prepared(row['id']))
            now=self.ledger.get(row['id'])
            self.assertEqual(now['charge_storage_bytes'],row['charge_storage_bytes'])
            self.assertEqual(now['state'],'cancelled_before_dispatch')
    def test_claimed_dispatch_is_not_cancelled(self):
        row=self.reserve();self.ledger.begin_dispatch(row['id'])
        self.assertFalse(self.ledger.cancel_prepared(row['id']))
        self.assertEqual(self.ledger.get(row['id'])['state'],'dispatching')

class ExecutionRecoveryTests(unittest.TestCase):
    setUp=execution_tests.ExecutionJobTests.setUp
    service=execution_tests.ExecutionJobTests.service
    enqueue=execution_tests.ExecutionJobTests.enqueue
    def test_replacement_is_current_before_dispatch_and_survives_restart(self):
        self.enqueue()
        job=self.jobs.get(self.task)
        with self.f.tasks.transaction() as db:self.jobs._event(db,job['id'],'attention')
        # Above nested transaction is read-only for get, but use existing event path.
        status=self.jobs.recheck(self.task,self.revision)
        new_id=status['job']['request_id']
        self.assertNotEqual(new_id,self.f.request_id)
        reopened=self.service()
        self.assertEqual(reopened.current_request_id(self.task),new_id)
        self.assertEqual(reopened.status(self.task)['job']['request_id'],new_id)
        self.assertEqual(reopened.status(self.task)['job']['dispatch_count'],0)
