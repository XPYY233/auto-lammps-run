"""Pinned saved SSH connection used by the existing accounted adapters."""
import json
from pathlib import Path
import shlex

from .hpc_connections import validate
from .ledger import Conflict
from .manifest import canonical, sha256
from .runtime_launcher import read_regular


class SavedHPCTransport:
    def __init__(self, connections, revision, host_alias):
        if type(revision) is not int or revision < 1:
            raise ValueError('An explicit saved HPC revision is required')
        self.connections,self.revision,self.host_alias=connections,revision,host_alias
        self.identity=self._identity()

    def _identity(self):
        row=self.connections._row(self.revision)
        if row is None:raise ValueError('Saved HPC revision does not exist')
        profile=validate(json.loads(row['configuration']))
        raw=read_regular(self.connections.directory/(row['credential_id']+'.json'),100000,private=True)
        return dict(revision=self.revision,host_alias=self.host_alias,profile=profile,
            store=str(self.connections.tasks.path.absolute()),credential_id=row['credential_id'],
            credential_sha256=sha256(raw),source_sha256=sha256(Path(__file__).read_bytes()),
            connection_source_sha256=sha256(Path(__file__).with_name('hpc_connections.py').read_bytes()))

    def command(self, host_alias, remote):
        if host_alias!=self.host_alias or self._identity()!=self.identity:
            raise ValueError('Pinned HPC connection changed')
        return self.connections.ssh_arguments(self.revision)+[shlex.join(remote)]


def transport_identity(adapter):
    transport=getattr(adapter,'transport',None)
    return transport.identity if transport is not None else None


def bind_request(ledger, request_id, adapter):
    """Bind once before any external effect; legacy in-flight requests cannot move."""
    value=transport_identity(adapter)
    digest=sha256(canonical(value)) if value else None
    with ledger._transaction() as db:
        row=ledger._request(db,request_id)
        saved=db.execute("SELECT payload FROM events WHERE request_id=? AND kind='hpc_connection_bound' ORDER BY seq LIMIT 1",(request_id,)).fetchone()
        if saved:
            if json.loads(saved['payload'])['identity_sha256']!=digest:
                raise Conflict('Request belongs to another HPC connection')
        elif value:
            used=db.execute("SELECT 1 FROM events WHERE request_id=? AND kind IN ('upload_intent','inputs_staged','upload_failed','dispatch_intent') LIMIT 1",(request_id,)).fetchone()
            if row['state']!='prepared' or row['dispatch_claimed'] or used:
                raise Conflict('Cannot migrate a request that already used another transport')
            ledger._event(db,request_id,'hpc_connection_bound',dict(identity_sha256=digest,revision=value['revision']))
