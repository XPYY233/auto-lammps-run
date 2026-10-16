"""Crash/race recovery over the existing fully synthetic analysis fixture."""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import request_body
from auto_lammps.manifest import sha256
from auto_lammps.output_analysis import OutputAnalysis
from auto_lammps.tasks import TaskError
import test_output_analysis as fixture


class OutputAnalysisRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.OutputAnalysisTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.request = 'a' * 32
        self.question = 'Synthetic retained-output recovery'

    def generate(self, request=None, service=None):
        case = self.fx
        return (service or case.service).generate(case.task, request or self.request,
                                                  case.source, self.question)

    def restart(self, connection=None):
        case = self.fx
        return OutputAnalysis(case.raw, connection_provider=connection or case.connection,
                              reference_sources=case.reference, model_calls=case.calls)

    def prepare_without_dispatch(self):
        case = self.fx
        with patch.object(case.calls, 'lookup', side_effect=SystemExit('synthetic before dispatch')):
            with self.assertRaises(SystemExit):
                self.generate()
        context, receipt, state = case.service._stored(case.task, self.request)
        self.assertEqual(state, 'prepared')
        self.assertIsNone(receipt)
        self.assertIsNone(case.calls.lookup(self.request))
        case.transport.assert_not_called()
        return context

    def test_same_inode_same_size_change_during_consumption_is_rejected_before_model(self):
        case = self.fx
        path = next((case.root / 'retrieved').glob('*/author-values.txt'))
        before = path.stat()
        original = path.read_bytes()
        changed = original.replace(b'1.5', b'9.5')
        self.assertNotEqual(changed, original)
        self.assertEqual(len(changed), len(original))
        download = case.raw.download
        consumed = []

        class InterruptedRead:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def read(self, size=-1):
                # Bypass BufferedReader caching so the unread suffix really is
                # obtained after the same inode has changed on disk.
                descriptor = self.handle.fileno()
                prefix = os.read(descriptor, 1)
                with path.open('r+b') as writer:
                    writer.write(changed)
                    writer.flush()
                data = prefix + os.read(descriptor, size - 1 if size >= 0 else len(changed))
                consumed.append(data)
                return data

        def changing_download(*args):
            name, size, handle = download(*args)
            return name, size, InterruptedRead(handle)

        with patch.object(case.raw, 'download', side_effect=changing_download):
            with self.assertRaises(TaskError):
                self.generate()
        self.assertEqual(consumed, [changed])
        self.assertEqual((path.stat().st_ino, path.stat().st_size), (before.st_ino, before.st_size))
        self.assertNotEqual(sha256(consumed[0]), sha256(original))
        case.transport.assert_not_called()
        self.assertEqual(case.calls.status()['used_requests'], 0)
        self.assertEqual(case.service.history(case.task), [])

    def test_reserved_model_call_remains_unknown_after_connection_change_and_blocks_new_id(self):
        case = self.fx

        def reserve_then_crash(identifier, messages, **options):
            body = request_body(case.calls.config, messages, model=case.client.model,
                                thinking=case.client.thinking)
            self.assertEqual(sha256(body), options['expected_request_sha256'])
            case.calls.reserve(identifier, body)
            raise SystemExit('synthetic after durable model reservation')

        with patch.object(case.client, 'complete_json', side_effect=reserve_then_crash):
            with self.assertRaises(SystemExit):
                self.generate()
        self.assertIsNone(case.calls.lookup(self.request)['receipt'])
        changed_connection = lambda task: dict(client=case.client, enabled=True,
                                               connection_revision=2, credential_generation=2)
        restarted = self.restart(changed_connection)
        record = self.generate(service=restarted)
        self.assertEqual(record['state'], 'unknown')
        self.assertEqual(record['round'], 1)
        self.assertIsNone(case.calls.lookup(self.request)['receipt'])
        with self.assertRaises(TaskError):
            self.generate('b' * 32, service=restarted)
        self.assertIsNone(restarted.get(case.task, 'b' * 32))
        self.assertEqual(self.generate(service=restarted)['state'], 'unknown')
        case.transport.assert_not_called()
        self.assertEqual(case.calls.status()['used_requests'], 1)

    def test_first_event_crash_rolls_back_intent_and_history_remains_readable(self):
        case = self.fx
        transaction = case.tasks.transaction
        reached = []

        class DatabaseProxy:
            def __init__(self, database):
                self.database = database

            def execute(self, sql, parameters=()):
                cursor = self.database.execute(sql, parameters)
                if ('INSERT INTO output_analysis_events' in sql
                        and len(parameters) >= 3 and parameters[2] == 'prepared'):
                    reached.append(True)
                    raise SystemExit('synthetic first-event transaction interruption')
                return cursor

            def __getattr__(self, name):
                return getattr(self.database, name)

        @contextmanager
        def interrupted_transaction():
            with transaction() as database:
                yield DatabaseProxy(database)

        with patch.object(case.tasks, 'transaction', interrupted_transaction):
            with self.assertRaises(SystemExit):
                self.generate()
        self.assertEqual(reached, [True])
        with case.tasks.transaction() as database:
            for table in ('output_analysis_intents', 'output_analysis_events', 'output_analysis_receipts'):
                self.assertEqual(database.execute('SELECT count(*) FROM ' + table + ' WHERE id=?',
                                                  (self.request,)).fetchone()[0], 0)
        restarted = self.restart()
        self.assertEqual(restarted.history(case.task), [])
        self.assertIsNone(restarted.get(case.task, self.request))
        response = case.http.get(case.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['history'], [])
        case.transport.assert_not_called()
        self.assertEqual(case.calls.status()['used_requests'], 0)
        recovered = self.generate(service=restarted)
        self.assertEqual((recovered['state'], recovered['round']), ('completed', 1))
        self.assertEqual(case.transport.call_count, 1)

    def test_changed_frozen_model_body_resumes_without_reserving_or_sending(self):
        case = self.fx
        context = self.prepare_without_dispatch()
        old_config = case.calls.config
        case.calls.amend_limits(replace(old_config, max_output_tokens=old_config.max_output_tokens + 1),
                                reason_sha256='c' * 64)
        changed = request_body(case.calls.config, context['messages'], model=case.client.model,
                               thinking=case.client.thinking)
        self.assertNotEqual(sha256(changed), context['request_sha256'])
        restarted = self.restart()
        record = self.generate(service=restarted)
        self.assertEqual((record['state'], record['round']), ('paused', 1))
        self.assertEqual(restarted.get(case.task, self.request)['state'], 'paused')
        self.assertIsNone(case.calls.lookup(self.request))
        case.transport.assert_not_called()
        self.assertEqual(case.calls.status()['used_requests'], 0)

    def test_dispatch_guard_catches_body_change_after_service_precheck(self):
        case = self.fx
        event = case.service._event
        changed = []

        def change_at_dispatch(identifier, state):
            event(identifier, state)
            if state == 'dispatching':
                config = case.calls.config
                case.calls.amend_limits(replace(config, max_output_tokens=config.max_output_tokens + 1),
                                        reason_sha256='d' * 64)
                changed.append(True)

        with patch.object(case.service, '_event', side_effect=change_at_dispatch):
            record = self.generate()
        self.assertEqual(changed, [True])
        self.assertEqual(record['state'], 'not_sent')
        self.assertIsNone(case.calls.lookup(self.request))
        case.transport.assert_not_called()
        self.assertEqual(case.calls.status()['used_requests'], 0)

    def test_numeric_output_quote_cannot_authorize_literal_but_explicit_row_can(self):
        case = self.fx
        case.proposal['citations'].append(dict(id='rawvalue', source_id='source1', quote='1.5'))
        case.proposal['column_evidence'].append(dict(table='difference', column='delta',
            meaning='Synthetic difference', unit='1', citations=['meaning']))
        expression = dict(op='subtract', left=dict(table='samples', column='value'),
                          right=dict(literal=1.5, evidence='rawvalue'))
        case.proposal['plan']['operations'] = [dict(id='difference', method='derive', alignment='row_index',
            columns=[dict(name='delta', unit='1', expression=expression)])]
        answer = self.generate()
        self.assertEqual(answer['state'], 'invalid_output')
        self.assertIsNone(answer['result'])
        self.assertEqual(answer['proposal'], case.proposal)
        self.assertEqual(case.transport.call_count, 1)
        expression['right'] = dict(table='samples', column='value', row=1)
        corrected = self.generate('b' * 32)
        self.assertEqual((corrected['state'], corrected['round']), ('completed', 2))
        self.assertEqual(case.transport.call_count, 2)
        report = next(item for item in corrected['result']['files'] if item['name'] == 'numeric-report.json')
        numeric = json.loads(case.service.download(case.task, 'b' * 32, report['name']))
        reference = numeric['derived_tables'][0]['references']
        scalar = next(item for item in reference if item['selection'] == 'scalar')
        self.assertEqual(scalar['source_lines'], [2])

    def test_interrupted_atomic_artifact_can_resume_same_model_receipt_once(self):
        case = self.fx
        fdopen = os.fdopen
        interrupted = []

        class PartialWrite:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, data):
                self.handle.write(data[:17])
                self.handle.flush()
                interrupted.append(17)
                raise SystemExit('synthetic partial artifact write')

        def interrupt_artifact(descriptor, mode='r', *args, **options):
            handle = fdopen(descriptor, mode, *args, **options)
            if mode == 'wb' and not interrupted:
                return PartialWrite(handle)
            return handle

        with patch('auto_lammps.output_analysis.os.fdopen', side_effect=interrupt_artifact):
            with self.assertRaises(SystemExit):
                self.generate()
        self.assertEqual(interrupted, [17])
        folder = case.service.directory / self.request
        self.assertTrue(folder.is_dir())
        self.assertEqual([path.name for path in folder.iterdir() if not path.name.startswith('.')], [])
        self.assertIsNone(case.service._stored(case.task, self.request)[1])
        self.assertEqual(case.calls.lookup(self.request)['receipt']['state'], 'completed')
        self.assertEqual(case.transport.call_count, 1)
        charged = case.ledger.get(case.source)['charge_storage_bytes']
        restarted = self.restart()
        self.assertEqual(restarted.get(case.task, self.request)['model_receipt']['state'], 'completed')
        recovered = self.generate(service=restarted)
        self.assertEqual((recovered['state'], recovered['round']), ('completed', 1))
        self.assertEqual(case.transport.call_count, 1)
        self.assertEqual(case.calls.status()['used_requests'], 1)
        self.assertEqual(case.ledger.get(case.source)['charge_storage_bytes'], charged)
        for item in recovered['result']['files']:
            data = restarted.download(case.task, self.request, item['name'])
            self.assertEqual((len(data), sha256(data)), (item['size'], item['sha256']))

    def test_technical_artifact_failure_retains_receipt_for_same_id_recovery(self):
        case = self.fx
        with patch('auto_lammps.output_analysis._publish_bytes', side_effect=OSError('synthetic storage failure')):
            failed = self.generate()
        self.assertEqual(failed['state'], 'analysis_execution_failed')
        self.assertEqual(failed['model_receipt']['state'], 'completed')
        self.assertIsNone(failed['result'])
        self.assertIsNone(case.service._stored(case.task, self.request)[1])
        self.assertEqual(case.transport.call_count, 1)
        restarted = self.restart()
        with self.assertRaises(TaskError):
            self.generate('b' * 32, service=restarted)
        recovered = self.generate(service=restarted)
        self.assertEqual((recovered['state'], recovered['round']), ('completed', 1))
        self.assertEqual(case.transport.call_count, 1)
        self.assertEqual(case.calls.status()['used_requests'], 1)


if __name__ == '__main__':
    unittest.main()
