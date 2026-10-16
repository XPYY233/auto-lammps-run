"""Synthetic additive author-source evidence; no network, model API or physics."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from auto_lammps import author_reference_plan as plans
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
import test_author_reference_plan as plan_fixtures
import test_author_source_reader as standard_fixtures
import test_legacy_author_source_reader as legacy_fixtures


class AuthorSourceAdditionsTests(unittest.TestCase):
    def setUp(self):
        self.fx = plan_fixtures.AuthorReferencePlanTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.identifier = self.fx.task['id']
        self.original = deepcopy(self.fx.input['author_source'])

    def package(self, texts, *, legacy=False, repository=None, commit=None, task=None):
        factory = (legacy_fixtures.LegacyAuthorSourceReaderTests if legacy
                   else standard_fixtures.AuthorSourceReaderTests)
        fx = factory()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.policy.update(repository=repository or self.fx.reader.policy['repository'],
                         commit=commit or self.fx.reader.policy['commit'],
                         reference_task_id=task or self.identifier)
        fx.texts = deepcopy(texts)
        if legacy:
            fx.commit = fx.policy['commit']
            fx.refresh()
        else:
            fx._write_sources()
            fx._bind_receipt()
        selections = [dict(path=name, start_line=1, end_line=20) for name in list(texts)[:8]]
        request = fx.request(selections)
        value = dict(capability=(fx.service().capability(fx.receipt, fx.intent, purpose='author_reference')
                                 if legacy else fx.service().capability(fx.receipt, purpose='author_reference')),
                     acquisition_receipt=deepcopy(fx.receipt),
                     reads=[dict(request=request, response=fx.worker(request))])
        if legacy:
            value['legacy_staging_intent'] = deepcopy(fx.intent)
        return value

    def aggregate(self, *packages):
        return plans.author_source_inputs(dict(source_packages=list(packages)), self.identifier)

    def evidence(self, source):
        value = dict(deepcopy(self.fx.input), author_source=deepcopy(source))
        return plans.author_reference_input(value, self.fx.scope,
                                            self.fx.fixture.input['sources'])

    def test_legacy_and_standard_packages_preserve_members_pins_and_read_proofs(self):
        extra = self.package({'extra.txt': b'Synthetic independent source note.\n'}, legacy=True)
        previous = plans.author_source_inputs(self.original, self.identifier)
        added = plans.author_source_inputs(extra, self.identifier)
        merged = self.aggregate(self.original, extra)
        self.assertEqual(merged['capability']['adapter'], 'pinned_author_source_packages')
        self.assertEqual(merged['capability']['members'], [previous['capability'], added['capability']])
        for key in ('files', 'reads', 'sources'):
            self.assertEqual(merged[key], previous[key] + added[key])
        self.assertFalse(merged['capability']['execution_authorized'])
        self.assertFalse(merged['capability']['B_input'])
        self.assertEqual(plans.author_source_inputs(self.original, self.identifier), previous)
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)

    def test_addition_revises_complete_saved_answer_once_and_preserves_history(self):
        complete = deepcopy(self.fx.value)
        self.fx.value.update(status='needs_clarification', questions=['Synthetic source addition required.'])
        old_answer = deepcopy(self.fx.value)
        first = self.fx.prepare()
        old_context = deepcopy(self.fx.service._stored(self.identifier, first['request_id'])[1])
        extra = self.package({'extra.txt': b'Additional synthetic invocation evidence.\n'}, legacy=True)
        self.fx.input['author_source'] = dict(source_packages=[self.original, extra])
        self.fx.input['invocation_paths'].append('extra.txt')
        self.fx.value = complete
        second = self.fx.prepare('5'*32, revision_note='Append the independently verified source package.')
        self.assertEqual((first['state'], second['state'], second['round']), ('needs_clarification', 'prepared', 2))
        self.assertEqual(second['revision']['complete_previous_answer'], old_answer)
        self.assertEqual(len(second['revision']['source_updates']['added_source_package_sha256']), 1)
        self.assertTrue(second['revision']['source_updates']['added_source_ids'])
        self.assertEqual(self.fx.service._stored(self.identifier, first['request_id'])[1], old_context)
        self.assertEqual(self.fx.service.get(self.identifier, first['request_id']), first)
        self.assertEqual(self.fx.calls.status()['used_requests'], 2)
        self.assertEqual(len(self.fx.sent), 2)
        self.assertFalse(second['execution_authorized'])
        self.assertFalse(second['can_submit'])
        self.assertFalse(second['can_approve'])
        current = self.fx.service._stored(self.identifier, second['request_id'])[1]
        self.assertEqual(current['input']['deployment']['resources'], old_context['input']['deployment']['resources'])

    def test_duplicate_source_ids_merge_only_identical_text_and_retain_both_read_identities(self):
        second = self.package(self.fx.reader.texts)
        old = plans.author_source_inputs(self.original, self.identifier)
        merged = self.aggregate(self.original, second)
        self.assertEqual(merged['sources'], old['sources'])
        self.assertEqual(merged['files'], old['files'])
        self.assertEqual(len(merged['capability']['members']), 2)
        self.assertEqual(len(merged['reads']), 2*len(old['reads']))
        self.assertEqual(self.aggregate(self.original, self.original)['reads'], old['reads'])
        native_source = plans._source

        def collided_source(*args):
            row = native_source(*args)
            row['id'] = 'synthetic-id-collision'
            row['source_sha256'] = sha256(canonical({k: row[k] for k in ('id', 'origin', 'locator', 'text')}))
            return row

        with patch.object(plans, '_source', side_effect=collided_source), self.assertRaisesRegex(TaskError, '编号'):
            self.aggregate(self.original)

    def test_same_path_file_pin_conflicts_are_rejected_even_with_valid_independent_receipts(self):
        for content in (b'Changed synthetic original bytes.\n', b'x'*len(self.fx.reader.texts['README.md'])):
            with self.subTest(size=len(content)):
                extra = self.package({'README.md': content})
                with self.assertRaisesRegex(TaskError, '同路径'):
                    self.aggregate(self.original, extra)

    def test_different_repository_commit_cross_task_and_forged_pairs_are_rejected(self):
        for kwargs in (dict(repository='other/synthetic'), dict(commit='e'*40), dict(task='e'*32)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.aggregate(self.original, self.package({'extra.txt': b'Synthetic extra.\n'}, **kwargs))
        good = self.package({'extra.txt': b'Synthetic extra.\n'}, legacy=True)
        for kind in ('capability', 'receipt', 'request', 'response', 'text'):
            changed = deepcopy(good)
            if kind == 'capability':
                changed['capability']['receipt_sha256'] = 'f'*64
            elif kind == 'receipt':
                changed['acquisition_receipt']['files'][0]['sha256'] = 'f'*64
            elif kind == 'request':
                changed['reads'][0]['request']['reference_task_id'] = 'e'*32
            elif kind == 'response':
                changed['reads'][0]['response']['request_sha256'] = 'f'*64
            else:
                changed['reads'][0]['response']['files'][0]['text'] = 'Invented source.\n'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.aggregate(self.original, changed)

    def test_missing_nested_and_excess_packages_are_rejected(self):
        for value in (dict(source_packages=[]), dict(source_packages=[self.original]*5),
                      dict(source_packages=[dict(source_packages=[self.original])]),
                      dict(source_packages=[None]), dict(source_packages=[dict(self.original, reads=[])]),
                      dict(source_packages=[dict(self.original, capability=['invalid'])]),
                      dict(source_packages=[self.original], approval=True)):
            with self.subTest(keys=list(value)), self.assertRaises(TaskError):
                plans.author_source_inputs(value, self.identifier)
        packages = [self.package({f'package{i}.txt': b'Synthetic independent text.\n'}) for i in range(4)]
        self.assertEqual(len(self.aggregate(*packages)['capability']['members']), 4)

    def test_unique_file_limit_and_unique_utf8_text_limit_are_global(self):
        first = self.package({f'a{i}.txt': b'Synthetic a.\n' for i in range(64)})
        second = self.package({f'b{i}.txt': b'Synthetic b.\n' for i in range(64)})
        self.assertEqual(len(self.aggregate(first, second)['files']), 128)
        third = self.package({'overflow.txt': b'Synthetic overflow.\n'})
        with self.assertRaisesRegex(TaskError, '128'):
            self.aggregate(first, second, third)
        half = plans.author_source_worker.MAX_TEXT_BYTES // 2
        a = self.package({'alpha.txt': b'a'*(half-1)+b'\n'})
        b = self.package({'beta.txt': b'b'*(half-1)+b'\n'})
        self.assertEqual(sum(len(row['text'].encode()) for row in self.aggregate(a, b)['sources']), 2*half)
        duplicate = self.package({'alpha.txt': b'a'*(half-1)+b'\n'})
        self.assertEqual(len(self.aggregate(a, b, duplicate)['sources']), 2)
        too_large = self.package({'beta.txt': b'b'*half+b'\n'})
        with self.assertRaisesRegex(TaskError, '256 KiB'):
            self.aggregate(a, too_large)

    def test_removing_replacing_members_or_old_read_proofs_cannot_revise_saved_evidence(self):
        previous = deepcopy(self.fx.evidence)
        extra = self.package({'extra.txt': b'Synthetic extra.\n'})
        replacement = self.package(self.fx.reader.texts)
        for value in (extra, replacement, dict(source_packages=[extra])):
            with self.subTest(adapter=value.get('capability', {}).get('adapter')), self.assertRaises(TaskError):
                plans._revision_sources(previous, self.evidence(value))
        merged = self.evidence(dict(source_packages=[self.original, replacement]))
        with self.assertRaisesRegex(TaskError, '来源包'):
            plans._revision_sources(merged, previous)
        # Equivalent text from another package cannot replace the original
        # request identity. Keep its capability, but remove one old read proof.
        changed = deepcopy(self.original)
        request = self.fx.reader.request([dict(path='README.md', start_line=1, end_line=20)])
        changed['reads'] = [dict(request=request, response=self.fx.reader.worker(request))]
        with self.assertRaisesRegex(TaskError, '读取回执'):
            plans._revision_sources(previous, self.evidence(dict(source_packages=[changed, replacement])))
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)

    def test_valid_but_replacement_receipt_does_not_consume_a_new_round_or_model_call(self):
        first = self.fx.prepare()
        self.fx.input['author_source'] = self.package(self.fx.reader.texts)
        with self.assertRaisesRegex(TaskError, '来源包'):
            self.fx.prepare('5'*32, revision_note='Synthetic invalid replacement.')
        self.assertEqual(self.fx.service.status(self.identifier)['used_rounds'], 1)
        self.assertEqual(self.fx.calls.status()['used_requests'], 1)
        self.assertEqual(self.fx.service.get(self.identifier, first['request_id']), first)


if __name__ == '__main__':
    unittest.main()
