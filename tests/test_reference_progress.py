"""Synthetic A progress and atomic task linkage; no reports, network or physics."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.ledger import Ledger
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from auto_lammps.web import create_app
from test_ledger import H1, H2, H3, POLICY, RESOURCE
from test_tasks import evidence, target_ready
from test_web import ORIGIN


class ReferenceProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.ledger = Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('synthetic', replace(POLICY, total_storage_bytes=100000))
        self.papers = PaperStore(self.tasks, ledger=self.ledger)
        self.paper = self.add_paper('first')
        self.task = self.tasks.create('Synthetic research', 'No simulation or reference answer.', 'research')
        self.evaluation = self.ledger.register_evaluation('synthetic', task_sha256=H1,
                    repetition=0, role='reference', system_sha256=H2)

    def add_paper(self, tag, selected=True):
        paper = self.papers.add('Full synthetic paper '+tag, '10.1234/'+tag,
                    'Explicit synthetic scope '+tag, 'PRIVATE_NOTE_CANARY')
        return self.papers.select(paper['id'], paper['revision']) if selected else paper

    def link(self):
        return self.papers.link_reference_task(self.paper['id'], self.task['id'], self.evaluation)

    def running(self):
        self.link()
        row = self.ledger.reserve(self.evaluation, 'first', H2, RESOURCE)
        self.ledger.begin_dispatch(row['id'])
        self.ledger.accepted(row['id'], '123', {'private_path': 'PRIVATE_PATH_CANARY', 'answer': 'PRIVATE_RESULT_CANARY'})
        self.ledger.observe(row['id'], '123', 'running', {'raw_log': 'PRIVATE_LOG_CANARY'})
        return row

    def generic_binding(self, role):
        task = self.tasks.create('Legacy binding', 'Synthetic metadata only.', 'reproduction')
        for field in FIELDS:
            task = self.tasks.add_candidate(task['id'], task['revision'], field, evidence())
        task = self.tasks.confirm(task['id'], task['revision'], list(FIELDS))
        task = target_ready(self.tasks, task)
        self.tasks.freeze(task['id'], task['revision'])
        paper = self.add_paper('generic-'+role)
        self.papers.link_task(paper['id'], paper['revision'], task['id'])
        evaluation = self.ledger.register_evaluation('synthetic', task_sha256=H3,
                    repetition=0, role=role, system_sha256=H2)
        self.papers.bind_evaluation(paper['id'], task['id'], evaluation, H3)
        return paper, task, evaluation

    def test_trusted_link_accepts_draft_research_without_freezing_or_dispatch(self):
        before = self.tasks.get(self.task['id'])
        progress = self.link()
        self.assertTrue(progress['available'])
        entry = progress['entries'][0]
        self.assertEqual(entry['paper']['title'], self.paper['title'])
        self.assertEqual(entry['paper']['doi'], self.paper['doi'])
        self.assertEqual(entry['paper']['scope'], self.paper['scope'])
        self.assertEqual(entry['paper']['doi_url'], 'https://doi.org/10.1234/first')
        self.assertEqual(entry['evaluation']['id'], self.evaluation)
        self.assertEqual(entry['evaluation']['requests'], [])
        self.assertEqual(self.tasks.get(self.task['id']), before)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'], 0)
        self.assertEqual(progress['scientific_validation'], 'not_performed')
        self.assertIs(progress['execution_authorized'], False)

    def test_browser_link_restriction_and_reference_role_are_unchanged(self):
        with self.assertRaisesRegex(TaskError, '复现任务'):
            self.papers.link_task(self.paper['id'], self.paper['revision'], self.task['id'])
        agent = self.ledger.register_evaluation('synthetic', task_sha256=H1,
                    repetition=0, role='agent', system_sha256=H2)
        with self.assertRaisesRegex(TaskError, '作者参考'):
            self.papers.link_reference_task(self.paper['id'], self.task['id'], agent)
        self.assertEqual(self.papers.get(self.paper['id'])['tasks'], [])

    def test_repeated_link_is_idempotent_and_old_history_survives_restart(self):
        first = self.link()
        history = self.papers.get(self.paper['id'])['history']
        self.assertEqual(self.link(), first)
        self.assertEqual(self.papers.get(self.paper['id'])['history'], history)
        reopened = PaperStore(TaskStore(self.tasks.path), ledger=Ledger(self.ledger.path))
        self.assertEqual(reopened.reference_progress(self.task['id']), first)
        self.assertEqual(reopened.get(self.paper['id'])['history'], history)
        self.assertEqual([item['event'].split(':')[0] for item in history],
                         ['candidate_added', 'paper_selected', 'task_linked', 'reference_evaluation_linked'])

    def test_conflicting_evaluation_rolls_back_new_task_link_and_history(self):
        self.link()
        other = self.tasks.create('Other research', 'No private evidence.', 'research')
        history = self.papers.get(self.paper['id'])['history']
        with self.assertRaisesRegex(TaskError, '不可转移'):
            self.papers.link_reference_task(self.paper['id'], other['id'], self.evaluation)
        self.assertEqual(self.papers.get(self.paper['id'])['history'], history)
        self.assertEqual(self.papers.reference_progress(other['id'])['entries'], [])
        with self.tasks.transaction() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM paper_tasks WHERE task_id=?', (other['id'],)).fetchone())

    def test_task_cannot_move_to_another_paper(self):
        self.link()
        other = self.add_paper('second')
        different = self.ledger.register_evaluation('synthetic', task_sha256=H3,
                    repetition=0, role='reference', system_sha256=H2)
        history = self.papers.get(other['id'])['history']
        with self.assertRaisesRegex(TaskError, '其他论文'):
            self.papers.link_reference_task(other['id'], self.task['id'], different)
        self.assertEqual(self.papers.get(other['id'])['history'], history)
        self.assertEqual(self.papers.get(other['id'])['tasks'], [])

    def test_candidate_paper_and_malformed_reference_identity_are_rejected(self):
        paper = self.add_paper('unselected', selected=False)
        with self.assertRaisesRegex(TaskError, '已选论文'):
            self.papers.link_reference_task(paper['id'], self.task['id'], self.evaluation)
        value = self.ledger.evaluation_snapshot(self.evaluation)
        value['identity']['task'] = '/PRIVATE_SOURCE_CANARY'
        with patch.object(self.ledger, 'evaluation_snapshot', return_value=value), self.assertRaises(TaskError):
            self.link()
        self.assertEqual(self.papers.get(self.paper['id'])['tasks'], [])

    def test_running_a_visible_without_any_result_report_and_projection_is_safe(self):
        row = self.running()
        before = self.ledger.events(row['id'])
        history = self.papers.get(self.paper['id'])['history']
        progress = self.papers.reference_progress(self.task['id'])
        visible = progress['entries'][0]['evaluation']
        self.assertTrue(visible['available'])
        self.assertEqual((visible['dispatch_claims'], visible['max_attempts'], visible['remaining_attempts']), (1, 2, 1))
        request = visible['requests'][0]
        self.assertEqual((request['job_id'], request['state']), ('123', 'running'))
        self.assertEqual(request['resources'], {'cores': 2, 'wall_seconds': 10, 'memory_bytes': 100, 'storage_bytes': 100})
        self.assertEqual(request['charge_core_seconds'], 20)
        self.assertEqual(request['charge_storage_bytes'], 100)
        self.assertIsNone(request['actual_core_seconds'])
        self.assertFalse(request['accounted'])
        self.assertTrue(all(set(item) == {'kind', 'at'} for item in request['events']))
        self.assertNotIn('PRIVATE_', json.dumps(progress))
        self.assertNotIn('identity', visible)
        self.assertNotIn('manifest_sha256', request)
        self.assertEqual(self.ledger.events(row['id']), before)
        self.assertEqual(self.papers.get(self.paper['id'])['history'], history)

    def test_monitoring_codes_are_visible_without_raw_diagnostics(self):
        row = self.running()
        self.ledger.register_monitor(row['id'], H2, interval_seconds=15, poll_storage_bytes=16384)
        self.assertTrue(self.ledger.claim_monitor_poll(row['id']))
        self.ledger.monitor_result(row['id'], reason='connection_failed', failed=True)
        progress = self.papers.reference_progress(self.task['id'])
        monitor = progress['entries'][0]['evaluation']['requests'][0]['monitoring']
        self.assertEqual(set(monitor), {'last_checked', 'next_due', 'reason', 'failures'})
        self.assertEqual((monitor['reason'], monitor['failures']), ('connection_failed', 1))

    def test_completed_accounted_a_is_not_claimed_scientifically_passed(self):
        row = self.running()
        self.ledger.observe(row['id'], '123', 'completed', {'private': 'PRIVATE_NUMERIC_CANARY'})
        self.ledger.account(row['id'], 12, H2)
        progress = self.papers.reference_progress(self.task['id'])
        request = progress['entries'][0]['evaluation']['requests'][0]
        self.assertEqual((request['state'], request['actual_core_seconds'], request['accounted']), ('completed', 12, 1))
        self.assertEqual(progress['scientific_validation'], 'not_performed')
        self.assertFalse(progress['execution_authorized'])
        self.assertNotIn('passed', json.dumps(progress))

    def test_missing_ledger_retains_paper_and_reference_identity_as_unavailable(self):
        self.running()
        progress = PaperStore(self.tasks).reference_progress(self.task['id'])
        self.assertFalse(progress['available'])
        visible = progress['entries'][0]
        self.assertEqual(visible['paper']['title'], self.paper['title'])
        self.assertEqual(visible['evaluation'], {'id': self.evaluation, 'available': False,
                                                'reason': 'reference_record_unavailable'})
        self.assertNotIn('dispatch_claims', visible['evaluation'])
        self.assertNotIn('requests', visible['evaluation'])

    def test_identity_conflict_and_request_association_do_not_guess_unsubmitted(self):
        row = self.running()
        bad = deepcopy(self.ledger.evaluation_snapshot(self.evaluation))
        bad['identity']['task'] = H3
        with patch.object(self.ledger, 'evaluation_snapshot', return_value=bad):
            value = self.papers.reference_progress(self.task['id'])
        self.assertFalse(value['available'])
        original = self.ledger.get(row['id'])
        original['evaluation'] = 'f'*64
        with patch.object(self.ledger, 'get', return_value=original):
            value = self.papers.reference_progress(self.task['id'])
        self.assertFalse(value['available'])
        self.assertNotIn('dispatch_claims', value['entries'][0]['evaluation'])

    def test_arbitrary_resource_event_and_monitor_text_are_not_exported(self):
        row = self.running()
        base = self.ledger.evaluation_snapshot(self.evaluation)
        bad_values = []
        bad = deepcopy(base); bad['requests'][0]['events'][0]['kind'] = '/PRIVATE_EVENT_CANARY'; bad_values.append(bad)
        bad = deepcopy(base); bad['requests'][0]['events'][0]['at'] = float('nan'); bad_values.append(bad)
        bad = deepcopy(base); bad['requests'][0]['events'][0]['at'] = 10**1000; bad_values.append(bad)
        bad = deepcopy(base); bad['requests'][0]['monitoring'] = dict(last_checked=1, next_due=2, failures=0,
                                                                    reason='/PRIVATE_PATH_CANARY'); bad_values.append(bad)
        bad = deepcopy(base); bad['remaining_attempts'] = 'PRIVATE_COUNT_CANARY'; bad_values.append(bad)
        for value in bad_values:
            with self.subTest(value=value), patch.object(self.ledger, 'evaluation_snapshot', return_value=value):
                progress = self.papers.reference_progress(self.task['id'])
            self.assertFalse(progress['available'])
            self.assertNotIn('PRIVATE_', json.dumps(progress))
        original = self.ledger.get(row['id'])
        resources = json.loads(original['resources']); resources['path'] = '/PRIVATE_RESOURCE_CANARY'
        original['resources'] = json.dumps(resources)
        with patch.object(self.ledger, 'get', return_value=original):
            progress = self.papers.reference_progress(self.task['id'])
        self.assertFalse(progress['available'])
        self.assertNotIn('PRIVATE_', json.dumps(progress))

    def test_unrelated_task_and_generic_agent_binding_do_not_leak_into_a_progress(self):
        self.running()
        other = self.tasks.create('Other task', 'Nothing to submit.', 'research')
        progress = self.papers.reference_progress(other['id'])
        self.assertTrue(progress['available'])
        self.assertEqual(progress['entries'], [])
        self.assertNotIn(self.evaluation, json.dumps(progress))
        _, task, agent = self.generic_binding('agent')
        for store in (self.papers, PaperStore(self.tasks)):
            progress = store.reference_progress(task['id'])
            self.assertEqual(progress['entries'], [])
            self.assertNotIn(agent, json.dumps(progress))

    def test_old_generic_reference_binding_requires_append_only_role_confirmation(self):
        paper, task, evaluation = self.generic_binding('reference')
        old_history = self.papers.get(paper['id'])['history']
        self.assertEqual(self.papers.reference_progress(task['id'])['entries'], [])
        self.papers.bind_reference_evaluation(paper['id'], task['id'], evaluation)
        new_history = self.papers.get(paper['id'])['history']
        self.assertEqual(new_history[:-1], old_history)
        self.assertEqual(new_history[-1]['event'], 'reference_evaluation_linked:'+evaluation)
        self.assertEqual(self.papers.reference_progress(task['id'])['entries'][0]['evaluation']['id'], evaluation)
        self.papers.bind_reference_evaluation(paper['id'], task['id'], evaluation)
        self.assertEqual(self.papers.get(paper['id'])['history'], new_history)

    def test_http_refresh_and_task_list_show_a_without_creating_b_or_touching_task_json(self):
        row = self.running()
        events = self.ledger.events(row['id'])
        history = self.papers.get(self.paper['id'])['history']
        before = self.tasks.get(self.task['id'])
        with TestClient(create_app(self.tasks, papers=self.papers), base_url=ORIGIN) as client:
            for _ in range(2):
                response = client.get('/api/tasks/'+self.task['id']+'/reference-progress')
                self.assertEqual(response.status_code, 200)
                evaluation = response.json()['entries'][0]['evaluation']
                self.assertEqual(evaluation['dispatch_claims'], 1)
                self.assertEqual(evaluation['requests'][0]['job_id'], '123')
                self.assertEqual(evaluation['requests'][0]['state'], 'running')
                response = client.get('/api/tasks')
                self.assertEqual(response.status_code, 200)
                task = next(item for item in response.json()['tasks'] if item['id'] == self.task['id'])
                self.assertEqual(task['reference_job_id'], '123')
                self.assertEqual(task['reference_state'], 'running')
                self.assertEqual(task['reference_submission_count'], 1)
                self.assertNotIn('submission_count', task)
                self.assertNotIn('job_id', task)
                response = client.get('/api/tasks/'+self.task['id'])
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(self.evaluation, json.dumps(response.json()))
                self.assertEqual(response.json(), before)
            missing = client.get('/api/tasks/'+'f'*32+'/reference-progress')
            self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.ledger.events(row['id']), events)
        self.assertEqual(self.papers.get(self.paper['id'])['history'], history)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'], 1)

    def test_existing_reproduction_reference_binding_is_preserved_and_idempotent(self):
        task = self.tasks.create('Legacy reproduction', 'No real result.', 'reproduction')
        paper = self.papers.link_task(self.paper['id'], self.paper['revision'], task['id'])
        self.papers.bind_reference_evaluation(paper['id'], task['id'], self.evaluation)
        before = self.papers.get(paper['id'])['history']
        value = self.papers.reference_progress(task['id'])
        self.assertTrue(value['available'])
        self.assertEqual(value['entries'][0]['evaluation']['id'], self.evaluation)
        self.papers.bind_reference_evaluation(paper['id'], task['id'], self.evaluation)
        self.assertEqual(self.papers.get(paper['id'])['history'], before)
        self.assertEqual(self.tasks.get(task['id'])['status'], 'draft')

    def test_recorded_unlimited_reference_does_not_change_candidate_limit(self):
        self.ledger.approve_reference_continuation(self.evaluation, approval_sha256=H3)
        self.link()
        value = self.papers.reference_progress(self.task['id'])['entries'][0]['evaluation']
        self.assertIsNone(value['max_attempts'])
        self.assertIsNone(value['remaining_attempts'])
        agent = self.ledger.register_evaluation('synthetic', task_sha256=H1,
                    repetition=1, role='agent', system_sha256=H2)
        self.assertEqual(self.ledger.evaluation_snapshot(agent)['max_attempts'], 2)


if __name__ == '__main__':
    unittest.main()
