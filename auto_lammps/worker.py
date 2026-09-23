"""Administrator-only, one-pass recovery entrypoint. No submit or engine command."""
import argparse
import json
from pathlib import Path

from .ledger import Ledger
from .reconciliation import ReconciliationService
from .slurm_read import SlurmReader


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reconcile existing dispatched requests; never submit jobs.')
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--ssh-alias', required=True)
    parser.add_argument('--audit-directory', type=Path, required=True)
    parser.add_argument('--request-id', help='Refresh one existing dispatched request, including a finished job.')
    args = parser.parse_args(argv)
    # A typo must not silently create a fresh, empty ledger and report success.
    if not args.ledger.is_file() or args.ledger.is_symlink():
        parser.error('An existing private ledger file is required')
    ledger = Ledger(args.ledger)
    service = ReconciliationService(ledger, SlurmReader(args.ssh_alias, args.audit_directory))
    rows = [service.refresh(args.request_id)] if args.request_id else service.recover_once()
    print(json.dumps({'requests': [{'request_id': row['id'], 'state': row['state'],
                                   'accounted': bool(row['accounted'])} for row in rows]}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
