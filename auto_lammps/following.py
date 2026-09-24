"""Trusted post-dispatch worker: reconcile, collect, analyze; never submit."""
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import stat
import threading

from . import runtime_launcher as runtime
from .analysis import adapter_identity
from .ledger import JOB_TERMINAL, LimitExceeded
from .manifest import Snapshot, canonical, private_directory, sha256

DONE={'analyzed','analysis_failed','diagnostics_saved','attention'}


class FollowingService:
    def __init__(self, ledger, reconciliation, analysis, snapshots, *, max_polls=240, interval_seconds=15):
        self.ledger,self.reconciliation,self.analysis=ledger,reconciliation,analysis
        collector=analysis.collector
        reader=reconciliation.reader
        if ledger.path!=reconciliation.ledger.path or ledger.path!=collector.ledger.path:
            raise ValueError('All services must share the same accounted ledger')
        if reader.host_alias!=collector.endpoint.host_alias:
            raise ValueError('Query and collection must use the same approved login alias')
        self.snapshots=private_directory(snapshots)
        self.max_polls,self.interval_seconds=max_polls,interval_seconds
        # Two bounded base64 query receipts, plus intents and interpretation.
        self.poll_storage_bytes=3*(reader.max_bytes+1)+16384
        config=dict(snapshots=str(self.snapshots),collection=str(collector.directory),reports=str(analysis.directory),
            endpoint=asdict(collector.endpoint),query=dict(host=reader.host_alias,audit=str(reader.audit_directory),
            timeout=reader.timeout,max_bytes=reader.max_bytes),collection_timeout=collector.timeout,
            analysis=adapter_identity(),follower_sha256=sha256(Path(__file__).read_bytes()))
        self.config_sha256=sha256(canonical(config))

    def _progress(self, request_id, state, reason=''):
        return dict(request_id=request_id,scientific_status='not_evaluated',
                    **self.ledger.following_progress(request_id,state,reason))

    def advance(self, request_id):
        """At most one due query and one fresh download; durable state survives exit."""
        lock=self.ledger.path.with_name(self.ledger.path.name+'.following.lock')
        fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077:
                raise ValueError('Unsafe following lock')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(request_id=request_id,state='waiting',reason='worker_busy',scientific_status='not_evaluated')
            return self._advance(request_id)
        finally:os.close(fd)

    def _advance(self, request_id):
        row=self.ledger.get(request_id)
        self.ledger.register_following(request_id,self.config_sha256,max_polls=self.max_polls,
            interval_seconds=self.interval_seconds,poll_storage_bytes=self.poll_storage_bytes)
        if row['state']=='reconcile_required':return self._progress(request_id,'attention','scheduler_conflict')
        previous=[json.loads(e['payload']) for e in self.ledger.events(request_id) if e['kind']=='following_progress']
        if previous and previous[-1]['state'] in DONE:
            return dict(request_id=request_id,scientific_status='not_evaluated',**previous[-1])
        snapshot=Snapshot(self.snapshots/row['manifest_sha256'],row['manifest_sha256'])
        try:
            manifest=snapshot.verify()
            if manifest['resources']!=json.loads(row['resources']):raise ValueError('Snapshot resource mismatch')
        except (ValueError,OSError,runtime.ExecutionDenied):
            return self._progress(request_id,'attention','input_verification_failed')
        if row['state'] not in JOB_TERMINAL or not row['accounted']:
            try:
                if not self.ledger.claim_following_poll(request_id):
                    return dict(request_id=request_id,state='waiting',reason='poll_not_due',scientific_status='not_evaluated')
            except LimitExceeded:
                return self._progress(request_id,'attention','poll_or_storage_limit')
            try:row=self.reconciliation.refresh(request_id)
            except (ValueError,OSError,RuntimeError):
                return self._progress(request_id,'waiting','scheduler_read_failed')
            if row['state']=='reconcile_required':return self._progress(request_id,'attention','scheduler_conflict')
            if row['state'] not in JOB_TERMINAL or not row['accounted']:
                return self._progress(request_id,'waiting',row['state'])
        events=self.ledger.events(request_id)
        started=any(e['kind']=='output_fetch_started' for e in events)
        collected=any(json.loads(e['payload'])['collected'] for e in events if e['kind']=='output_fetch_finished')
        if started and not collected:
            return self._progress(request_id,'attention','collection_incomplete')
        self._progress(request_id,'collecting')
        try:
            result=self.analysis.collector.fetch(request_id)
            if result['state']!='collected':return self._progress(request_id,'attention','collection_failed')
            if row['state']!='completed':return self._progress(request_id,'diagnostics_saved',row['state'])
            self._progress(request_id,'analyzing')
            value=self.analysis.run(request_id,snapshot)
            return self._progress(request_id,value['report']['status'])
        except (ValueError,KeyError,TypeError,OSError,RuntimeError,runtime.ExecutionDenied):
            return self._progress(request_id,'attention','result_processing_failed')

    def run(self, request_id, *, stop=None, notify=None):
        """Run until done, attention, or supervisor stop; stopping does not cancel HPC."""
        stop=stop or threading.Event()
        while not stop.is_set():
            result=self.advance(request_id)
            if notify:notify(result)
            if result['state'] in DONE:return result
            stop.wait(min(self.interval_seconds,60))
        return dict(request_id=request_id,state='stopped',scientific_status='not_evaluated')
