"""Accounted dispatch coordinator; no production scheduler adapter yet.

Only a trusted controller may construct this service. The runtime Agent will
not have access to policy, evaluation registration, accounting or reconciliation.
"""
from dataclasses import dataclass
import json
from typing import Protocol

from .ledger import Ledger, Resources


class SchedulerRejected(Exception):
    """Adapter has positive evidence that no scheduler job was accepted."""


@dataclass(frozen=True)
class Submission:
    request_id: str
    manifest_sha256: str
    resources: Resources


class Scheduler(Protocol):
    def submit(self, submission: Submission) -> str:
        """Submit exactly once; tag remote directory/job with request_id.

        Return a single job ID. On ambiguous transport failures raise a normal
        exception. SchedulerRejected is reserved for an explicit rejection.
        """


class SubmissionService:
    def __init__(self, ledger: Ledger, scheduler: Scheduler):
        self.ledger = ledger
        self.scheduler = scheduler

    def submit(self, evaluation: str, key: str, manifest_sha256: str, resources: Resources):
        row = self.ledger.reserve(evaluation, key, manifest_sha256, resources)
        request_id = row["id"]
        if not self.ledger.begin_dispatch(request_id):
            # Repeat calls and recovery return saved state, never redo network I/O.
            return self.ledger.get(request_id)
        try:
            job_id = self.scheduler.submit(Submission(request_id, row["manifest_sha256"],
                                                     Resources(**json.loads(row["resources"]))))
        except SchedulerRejected:
            self.ledger.rejected(request_id, {"kind": "explicit_scheduler_rejection"})
        except Exception as exc:
            self.ledger.uncertain(request_id, {"exception_type": type(exc).__name__})
        else:
            # If this write fails, the committed dispatch intent remains for lookup
            # by request_id. Never turn a recording failure into a second submit.
            try:
                self.ledger.accepted(request_id, job_id, {"kind": "scheduler_receipt"})
            except ValueError:
                self.ledger.uncertain(request_id, {"kind": "invalid_scheduler_receipt"})
        return self.ledger.get(request_id)
