"""Administrator recovery or bounded post-dispatch following; no job submission."""
import argparse
import json
from pathlib import Path
import signal
import threading

from .analysis import AnalysisService
from .following import FollowingService
from .ledger import Ledger
from .outputs import OutputCollector
from .reconciliation import ReconciliationService
from .runtime_launcher import read_regular
from .slurm_read import SlurmReader
from .staging import StageEndpoint


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reconcile or follow existing dispatched requests; never submit jobs.')
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--ssh-alias', required=True)
    parser.add_argument('--audit-directory', type=Path, required=True)
    parser.add_argument('--request-id', help='Refresh one existing dispatched request, including a finished job.')
    parser.add_argument('--follow-config',type=Path,help='Private post-dispatch automation configuration; requires request-id')
    args = parser.parse_args(argv)
    # A typo must not silently create a fresh, empty ledger and report success.
    if not args.ledger.is_file() or args.ledger.is_symlink():
        parser.error('An existing private ledger file is required')
    ledger = Ledger(args.ledger)
    if args.follow_config:
        if not args.request_id:parser.error('Following requires one explicit existing request')
        config=json.loads(read_regular(args.follow_config,16384,private=True))
        if set(config)!={'snapshots_directory','collections_directory','reports_directory','endpoint',
                         'max_polls','interval_seconds','query_max_bytes'}:
            parser.error('Unsupported following configuration')
        reader=SlurmReader(args.ssh_alias,args.audit_directory,max_bytes=config['query_max_bytes'])
        collector=OutputCollector(ledger,StageEndpoint(**config['endpoint']),config['collections_directory'])
        following=FollowingService(ledger,ReconciliationService(ledger,reader),
            AnalysisService(collector,config['reports_directory']),config['snapshots_directory'],
            max_polls=config['max_polls'],interval_seconds=config['interval_seconds'])
        stop=threading.Event()
        old={sig:signal.signal(sig,lambda *_:stop.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
        try:
            result=following.run(args.request_id,stop=stop,notify=lambda result:print(json.dumps(result),flush=True))
        finally:
            for sig,handler in old.items():signal.signal(sig,handler)
        return 0 if result['state']=='analyzed' else 2
    service = ReconciliationService(ledger, SlurmReader(args.ssh_alias, args.audit_directory))
    rows = [service.refresh(args.request_id)] if args.request_id else service.recover_once()
    print(json.dumps({'requests': [{'request_id': row['id'], 'state': row['state'],
                                   'accounted': bool(row['accounted'])} for row in rows]}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
