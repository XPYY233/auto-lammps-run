"""Pinned historical scope selection after explicit replay; synthetic data only."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_scope import ReferenceScopeService, ReferenceScopeUnavailable
from auto_lammps.reference_targets import reference_target_input
from auto_lammps.tasks import TaskError
import test_reference_target_unknown_reissue as recovery_fixture


class RecoveredReferenceScopeTests(unittest.TestCase):
    def setUp(self):
        self.r = recovery_fixture.ReferenceTargetUnknownReissueTests()
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        self.f = self.r.f
        self.service = ReferenceScopeService(self.f.tasks, self.f.papers, self.r.service)
        self.request = 'f' * 32

    def complete_revision(self):
        self.r.failed_original_replay()
        self.f.value['targets'][0]['citations'][0]['quote'] = 'Common neighbor analysis (CNA)'
        result = self.r.revise()
        self.assertEqual(result['state'], 'completed', result)
        return result

    def preview(self, request=None):
        return self.service.preview(self.r.task, request or self.request,
            ['target-raw_lammps'], 'Synthetic selection; retain all other inventory targets.')

    def create(self, preview):
        return self.service.create(self.r.task, self.f.task['revision'], 'a' * 32,
            self.request, preview['scope']['selected_ids'], preview['scope']['exclusion_reason'],
            expected_scope_sha256=preview['scope_sha256'])

    def test_recovered_third_revision_uses_original_p_and_keeps_scope_schema_and_fees(self):
        self.complete_revision()
        parent_before = deepcopy(self.r.service._stored(self.r.task, self.r.parent))
        costs = self.f.calls.status()
        self.f.provider.side_effect = AssertionError('Never replace pinned original P with latest')
        preview = self.preview()
        self.assertTrue(preview['historical_source'])
        self.assertTrue(preview['original_source_preserved'])
        self.assertEqual(preview['source_version_status'], 'historical_original_snapshot')
        scope = preview['scope']; provenance = scope['provenance']
        self.assertNotIn('historical_source', scope)
        self.assertNotIn('original_source_preserved', provenance)
        self.assertEqual(provenance['input_sha256'], sha256(canonical(reference_target_input(self.r.original))))
        self.assertEqual(provenance['target_request_id'], self.request)
        self.assertEqual(preview['scope_sha256'], sha256(canonical(scope)))
        saved = self.create(preview)
        self.assertEqual(saved['state'], 'recorded', saved)
        self.assertTrue(saved['can_prepare_A_plan'])
        self.assertFalse(saved['execution_authorized'])
        self.assertEqual(saved['scope'], scope)
        self.assertEqual(self.create(preview), saved)
        restarted = ReferenceScopeService(self.f.tasks, self.f.papers, self.r.make())
        self.assertEqual(restarted.get(self.r.task, saved['id']), saved)
        self.assertEqual(self.r.service._stored(self.r.task, self.r.parent), parent_before)
        self.assertEqual(self.f.calls.status(), costs)
        self.assertEqual(self.f.calls.lookup(self.r.parent)['receipt']['state'], 'unknown')

    def test_untreated_unknown_and_unknown_child_still_block(self):
        self.r.unknown()
        with self.assertRaises(ReferenceScopeUnavailable) as raised:
            self.preview(self.r.parent)
        self.assertEqual(raised.exception.code, 'target_request_unknown')
        self.f.client.transport = Mock(side_effect=TimeoutError('synthetic child unknown'))
        child = self.r.reissue()
        self.assertEqual(child['state'], 'unknown')
        with self.assertRaises(ReferenceScopeUnavailable) as raised:
            self.preview(self.r.child)
        self.assertEqual(raised.exception.code, 'target_request_unknown')
        self.assertEqual(self.service.history(self.r.task), [])
        self.assertEqual(self.f.calls.status()['used_requests'], 2)

    def test_changed_original_snapshot_cannot_fall_back_to_latest(self):
        self.complete_revision(); preview = self.preview(); saved = self.create(preview)
        self.r.original['sources'][0]['text'] += ' Altered retained source.'
        with self.assertRaises(TaskError): self.preview()
        old = self.service.get(self.r.task, saved['id'])
        self.assertEqual(old['state'], 'source_unavailable')
        self.assertEqual(old['scope'], preview['scope'])
        self.assertFalse(old['can_prepare_A_plan'])

    def test_recovery_authorization_and_revision_parent_tampering_are_rejected(self):
        self.complete_revision()
        original_replay = self.r.service._unknown_reissue
        original_revision = self.r.service._original_revision
        def replay(*args, **kwargs):
            value = deepcopy(original_replay(*args, **kwargs))
            if value: value['supplier_checked'] = False
            return value
        def revision(*args, **kwargs):
            value = deepcopy(original_revision(*args, **kwargs))
            if value: value['parent_context_sha256'] = '0' * 64
            return value
        for method, changed in (('_unknown_reissue', replay), ('_original_revision', revision)):
            with self.subTest(method=method), patch.object(self.r.service, method, side_effect=changed):
                with self.assertRaises(TaskError): self.preview()
        self.assertEqual(self.service.history(self.r.task), [])

    def test_unknown_appearing_between_preview_and_save_still_blocks_atomic_save(self):
        self.complete_revision(); preview = self.preview()
        pending = self.r.service._pending
        calls = []
        def newly_unknown(db, identifier, **kwargs):
            calls.append(True)
            return len(calls) > 1 or pending(db, identifier, **kwargs)
        with patch.object(self.r.service, '_pending', side_effect=newly_unknown):
            with self.assertRaises(ReferenceScopeUnavailable) as raised: self.create(preview)
        self.assertEqual(raised.exception.code, 'target_request_unknown')
        self.assertEqual(self.service.history(self.r.task), [])
