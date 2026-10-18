"""HTTP authorization boundary; synthetic evidence and zero model/HPC dispatch."""
import unittest
import importlib.util
from unittest.mock import patch
import test_workbench_targets_web as fixture
from auto_lammps.workbench_jobs import WorkbenchExtractionJobs


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class UnknownReissueWebTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.WorkbenchTargetsWebTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.body = dict(request_id='e'*32, source_sha256=self.f.binding.pdf_sha256,
            unknown_reissue_of='f'*32, supplier_checked=True, retain_unknown_charge=True)
        self.url = self.f.base + '/reissue-unknown'
        self.options = patch.object(WorkbenchExtractionJobs, 'unknown_reissue_options',
            return_value=[dict(unknown_reissue_of='f'*32, enabled=True, reused_model_calls=155)],
            create=True)
        self.options.start()
        self.addCleanup(self.options.stop)
        self.enqueue = patch.object(WorkbenchExtractionJobs, 'enqueue_unknown_reissue',
            return_value=dict(task_id=self.f.task['id'], request_id='e'*32,
                state='queued', execution_authorized=False), create=True)
        self.send = self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

    def post(self, body=None, headers=None):
        return self.f.client.post(self.url, json=self.body if body is None else body,
            headers=fixture.HEADERS if headers is None else headers)

    def test_explicit_confirmation_routes_to_accounted_queue_and_get_exposes_options(self):
        status = self.f.client.get(self.f.base)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()['unknown_reissues'][0]['reused_model_calls'], 155)
        result = self.post()
        self.assertEqual(result.status_code, 202, result.text)
        self.send.assert_called_once_with(self.f.task['id'], 'e'*32,
            source_sha256=self.body['source_sha256'], unknown_reissue_of='f'*32,
            supplier_checked=True, retain_unknown_charge=True)
        self.assertEqual(self.f.sent, [])

    def test_false_coerced_or_missing_confirmation_never_dispatches(self):
        for key in ('supplier_checked', 'retain_unknown_charge'):
            for value in (False, 1, 'true', None):
                result = self.post({**self.body, key:value})
                self.assertEqual(result.status_code, 422, result.text)
            body = dict(self.body); del body[key]
            self.assertEqual(self.post(body).status_code, 422)
        self.send.assert_not_called()

    def test_stale_source_extra_native_identity_and_cross_origin_never_dispatch(self):
        self.assertEqual(self.post({**self.body, 'source_sha256':'a'*64}).status_code, 409)
        for key in ('native_base_request', 'job_token', 'force_rescan', 'max_tokens'):
            self.assertEqual(self.post({**self.body, key:True}).status_code, 422)
        for headers in ({}, {**fixture.HEADERS, 'Origin':'https://invalid.example'}):
            self.assertEqual(self.post(headers=headers).status_code, 403)
        self.send.assert_not_called()
