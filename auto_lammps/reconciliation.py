"""One-pass recovery for the trusted worker. Contains no submission path."""
from datetime import datetime, timezone
from typing import Protocol

from .ledger import Ledger
from .slurm_read import Observation


class SchedulerReader(Protocol):
    def lookup(self, request_id: str, manifest_sha256: str, *, since_utc: str) -> Observation:
        """Return a validated observation linked to durably saved raw receipts."""


class ReconciliationService:
    def __init__(self, ledger: Ledger, reader: SchedulerReader):
        self.ledger, self.reader = ledger, reader

    def refresh(self, request_id: str):
        context = self.ledger.begin_reconciliation(request_id)
        # Derive the range from the durable dispatch, including a small clock-skew
        # margin. Never let the Agent select a window that hides previous jobs.
        since = datetime.fromtimestamp(context['dispatch_at'] - 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        observation = self.reader.lookup(context['request_id'], context['manifest_sha256'], since_utc=since)
        return self.ledger.apply_reconciliation(
            request_id, context['ticket'], state=observation.state, job_id=observation.job_id,
            core_seconds=observation.allocated_core_seconds, reason=observation.reason,
            evidence_sha256=observation.evidence_sha256)

    def recover_once(self):
        """Recover dispatched/unaccounted work; never start a prepared request.

        Audit/reader errors propagate. A later worker pass can query again safely.
        No background daemon, polling loop or model request is started here.
        """
        return [self.refresh(row['id']) for row in self.ledger.recoverable() if row['dispatch_claimed']]
