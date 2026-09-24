"""Trusted, ledger-bound native SSH adapter for the installed submission helper.

There is no Agent-facing terminal or arbitrary command API. Deployment and grant
issuance remain administrator responsibilities; this module does not approve jobs.
"""
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
import shlex

from .ledger import Conflict, Ledger
from .manifest import private_directory
from .slurm_read import _capture, _write_new, identity
from .staging import BOOTSTRAP, StageEndpoint
from .submission import SchedulerReceipt, Submission


class SubmitUncertain(RuntimeError):
    pass


class SlurmSubmitter:
    def __init__(self, ledger: Ledger, endpoint: StageEndpoint, audit_directory):
        # endpoint.helper_path pins remote_submit.py, not the upload receiver.
        self.ledger, self.endpoint = ledger, endpoint
        self.audit_directory = private_directory(audit_directory)

    def _reserved(self, submission, allowed_states):
        identity(submission.request_id, submission.manifest_sha256)
        row = self.ledger.get(submission.request_id)
        if (row['state'] not in allowed_states or row['manifest_sha256'] != submission.manifest_sha256
                or json.loads(row['resources']) != asdict(submission.resources)):
            raise Conflict('Submission differs from the durable request or its allowed state')
        if not any(event['kind'] == 'inputs_staged' for event in self.ledger.events(submission.request_id)):
            raise Conflict('Complete input upload must be recorded before dispatch')
        return row

    def prepare(self, submission: Submission):
        self._reserved(submission, {'prepared'})

    def submit(self, submission: Submission):
        row = self._reserved(submission, {'dispatching'})
        if not row['dispatch_claimed']:
            raise Conflict('A durable dispatch intent is required')
        endpoint = self.endpoint
        remote = [endpoint.python_path, '-I', '-c', BOOTSTRAP, endpoint.helper_path, endpoint.helper_sha256,
                  '--root', endpoint.root_path, '--request-id', submission.request_id,
                  '--manifest-sha256', submission.manifest_sha256]
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
                   '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no', endpoint.host_alias, shlex.join(remote)]
        trace = self.audit_directory / submission.request_id
        # Even direct misuse of the adapter cannot repeat network dispatch.
        trace.mkdir(mode=0o700, exist_ok=False)
        intent = _write_new(trace / 'intent.json', dict(argv=command, request_id=submission.request_id,
                            manifest_sha256=submission.manifest_sha256,
                            started_utc=datetime.now(timezone.utc).isoformat()))
        try:
            result = _capture(command, timeout=40, max_bytes=65536)
        except (OSError, ValueError) as exc:
            result = dict(returncode=None, failure=type(exc).__name__, stdout='', stderr='')
        proof = _write_new(trace / 'result.json', {**result, 'intent_sha256': intent,
                           'finished_utc': datetime.now(timezone.utc).isoformat()})
        if result['failure'] or result['returncode'] != 0:
            raise SubmitUncertain('Scheduler acceptance not confirmed; reconcile before any further submission')
        try:
            receipt = json.loads(base64.b64decode(result['stdout'], validate=True).decode('utf-8'))
            if (not isinstance(receipt, dict) or receipt.get('state') != 'accepted'
                    or receipt.get('request_id') != submission.request_id
                    or receipt.get('manifest_sha256') != submission.manifest_sha256
                    or not isinstance(receipt.get('job_id'), str)
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', receipt['job_id'])
                    or not isinstance(receipt.get('evidence_sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', receipt['evidence_sha256'])):
                raise ValueError('No matching acceptance receipt')
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise SubmitUncertain('Invalid scheduler receipt; retain dispatch reservation') from exc
        return SchedulerReceipt(receipt['job_id'], proof)
