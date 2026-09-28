"""Persistent read-only scheduler monitoring. No submission/model/physics path."""
import fcntl
import json
import os
from pathlib import Path
import stat
import threading

from .ledger import JOB_TERMINAL, LimitExceeded
from .manifest import canonical, sha256


class MonitoringService:
    def __init__(self, reconciliation, *, interval_seconds=900):
        self.reconciliation = reconciliation
        self.ledger = reconciliation.ledger
        self.interval_seconds = interval_seconds
        reader = reconciliation.reader
        config = dict(host=reader.host_alias, audit=str(reader.audit_directory), timeout=reader.timeout,
                      max_bytes=reader.max_bytes, retain_queue_identity=reader.retain_queue_identity,
                      interval_seconds=interval_seconds, implementation=sha256(Path(__file__).read_bytes()))
        self.config_sha256 = sha256(canonical(config))
        self.poll_storage_bytes = 3 * (reader.max_bytes + 1) + 65536

    def _result(self, request_id, row, reason='', changed=False):
        return dict(request_id=request_id, job_id=row['job_id'], state=row['state'],
                    accounted=bool(row['accounted']), reason=reason, changed=changed,
                    scientific_status='not_evaluated',
                    terminal=row['state'] == 'reconcile_required' or
                    (row['state'] in JOB_TERMINAL and bool(row['accounted'])))

    def advance(self, request_id):
        self.ledger.register_monitor(request_id, self.config_sha256,
            interval_seconds=self.interval_seconds, poll_storage_bytes=self.poll_storage_bytes)
        # Shared lock also prevents overlap with the existing following worker.
        lock = self.ledger.path.with_name(self.ledger.path.name + '.following.lock')
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError('Unsafe monitor lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._result(request_id, self.ledger.get(request_id), 'worker_busy')
            row = self.ledger.get(request_id)
            if self._result(request_id, row)['terminal']:
                # Completion might have been recorded by another trusted observer.
                # Preserve one transition even after process death before notification.
                events = self.ledger.events(request_id)
                last = next((e for e in reversed(events) if e['kind'] == 'monitor_transition'), None)
                expected = dict(state=row['state'], accounted=bool(row['accounted']), reason='')
                if last and json.loads(last['payload']) == expected:
                    return self._result(request_id, row)
                transition = self.ledger.monitor_result(request_id)
                return self._result(request_id, row, changed=transition['changed'])
            try:
                if not self.ledger.claim_monitor_poll(request_id):
                    return self._result(request_id, row, 'poll_not_due')
            except LimitExceeded:
                result = self.ledger.monitor_result(request_id, reason='receipt_storage_limit', failed=True)
                return self._result(request_id, row, 'receipt_storage_limit', result['changed']) | {'terminal': True}
            reason = ''
            try:
                row = self.reconciliation.refresh(request_id)
                finished = next((json.loads(e['payload']) for e in reversed(self.ledger.events(request_id))
                                 if e['kind'] == 'reconciliation_finished'), None)
                if (not finished or finished['outcome'] != 'applied'
                        or finished['observation']['state'] == 'unknown'):
                    reason = 'scheduler_unverified'
            except (ValueError, OSError, RuntimeError):
                reason = 'scheduler_read_failed'
                row = self.ledger.get(request_id)
            result = self.ledger.monitor_result(request_id, reason=reason, failed=bool(reason))
            return self._result(request_id, row, reason, result['changed'])
        finally:
            os.close(fd)

    def run(self, request_id, *, stop=None, notify=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            result = self.advance(request_id)
            if result['changed'] and notify:
                notify(result)
            if result['terminal']:
                return result
            stop.wait(min(self.interval_seconds, 60))
        return dict(request_id=request_id, state='stopped', scientific_status='not_evaluated')
