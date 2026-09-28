"""Trusted controller for HPC-only engine source preparation. Never builds or runs."""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import uuid

from .engine_source_worker import (EnginePreparation, EnginePreparationError,
    requirements_from_models, inspect_help, assess_engine, unpack_source, cmake_plan,
    validate_requirements, validate_request, download_source)
from .manifest import canonical, private_directory, sha256
from .slurm_read import _capture, _write_new

# Only our checked-in worker is executable. Paper/model contents are JSON data.
_BOOTSTRAP = '''import sys,json,hashlib
raw=sys.stdin.buffer.read(131073)
if len(raw)>131072:raise ValueError('Worker request too large')
envelope=json.loads(raw)
source=envelope['source'];request=envelope['request']
if hashlib.sha256(source.encode()).hexdigest()!=request['worker_sha256']:raise ValueError('Worker hash mismatch')
namespace={'__name__':'engine_source_worker'}
exec(compile(source,'engine_source_worker.py','exec'),namespace)
print(json.dumps(namespace['remote_main'](request,source)))
'''


class RemoteEnginePreparation:
    def __init__(self, directory, *, host_alias, remote_directory, operation_id, capture=_capture):
        if (not isinstance(host_alias, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', host_alias)
                or not isinstance(operation_id, str) or not re.fullmatch('[a-f0-9]{32}', operation_id)):
            raise EnginePreparationError('Configured HPC alias and operation identity required')
        self.root = private_directory(directory)
        self.host_alias, self.remote_directory, self.identifier = host_alias, remote_directory, operation_id
        self.capture = capture

    def prepare_source(self, requirements, *, commit, storage_bytes):
        source = Path(__file__).with_name('engine_source_worker.py').read_text()
        request = validate_request(dict(id=self.identifier, directory=self.remote_directory,
            requirements=validate_requirements(requirements), commit=commit, storage_bytes=storage_bytes,
            worker_sha256=sha256(source.encode())))
        intent = dict(host_alias=self.host_alias, request=request)
        folder = self.root/self.identifier
        folder.mkdir(mode=0o700, exist_ok=True)
        intent_path = folder/'intent.json'
        try:
            _write_new(intent_path, intent)
        except FileExistsError:
            if intent_path.is_symlink() or json.loads(intent_path.read_text()) != intent:
                raise EnginePreparationError('Operation already bound to another preparation')
        observation = uuid.uuid4().hex
        _write_new(folder/(observation+'-start.json'), dict(at=datetime.now(timezone.utc).isoformat()))
        envelope = canonical(dict(source=source, request=request))
        if len(envelope) > 131072:
            raise EnginePreparationError('Worker request too large')
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes',
            '-o', 'PermitLocalCommand=no', '-o', 'RemoteCommand=none', '-T', self.host_alias,
            shlex.join(['python3', '-c', _BOOTSTRAP])]
        try:
            result = self.capture(command, timeout=240, max_bytes=100000,
                input_chunks=[envelope[i:i+65536] for i in range(0, len(envelope), 65536)])
        except OSError as exc:
            result = dict(returncode=None, failure=type(exc).__name__, stdout='', stderr='')
        _write_new(folder/(observation+'-transport.json'), result)
        report = dict(id=self.identifier, schema_version=1, state='unknown', location='hpc',
            requirements=requirements, commit=commit, storage_bytes=storage_bytes,
            worker_sha256=request['worker_sha256'], request_sha256=sha256(canonical(request)),
            built=False, environment_verified=False, scientific_validation=False, execution_authorized=False)
        if result.get('returncode') == 0 and not result.get('failure'):
            try:
                observed = json.loads(base64.b64decode(result['stdout'], validate=True))
                if (not isinstance(observed, dict) or len(canonical(observed)) > 20000
                        or any(observed.get(k) != report[k] for k in report if k != 'state')
                        or observed.get('state') not in {'source_ready', 'failed', 'unknown'}):
                    raise ValueError('Remote receipt binding mismatch')
                if observed['state'] == 'source_ready' and (
                        not re.fullmatch('[a-f0-9]{64}', observed.get('archive_sha256', ''))
                        or not re.fullmatch('[a-f0-9]{64}', observed.get('inventory_sha256', ''))
                        or type(observed.get('files')) is not int or not 1 <= observed['files'] <= 50000
                        or type(observed.get('source_bytes')) is not int or not 0 < observed['source_bytes'] <= storage_bytes):
                    raise ValueError('Incomplete source inventory receipt')
                report = observed
            except (ValueError, TypeError):
                report['observation_failure'] = 'invalid_remote_receipt'
        else:
            report['observation_failure'] = result.get('failure') or 'transport_failed'
        _write_new(folder/(observation+'-result.json'), report)
        return report


def prepare_paper_engine(papers, identifier, directory, policy, *, capture=_capture):
    keys = {'release', 'commit', 'cores', 'storage_bytes', 'host_alias', 'remote_directory', 'operation_id'}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise EnginePreparationError('Explicit HPC engine preparation policy required')
    paper = papers.get(identifier)
    requirements = requirements_from_models(paper.get('potential_acquisition', {}),
                                           release=policy['release'], cores=policy['cores'])
    report = RemoteEnginePreparation(directory, host_alias=policy['host_alias'],
        remote_directory=policy['remote_directory'], operation_id=policy['operation_id'], capture=capture
        ).prepare_source(requirements, commit=policy['commit'], storage_bytes=policy['storage_bytes'])
    papers.record_engine_preparation(identifier, report)
    return report


def main():
    import argparse
    from .manifest import read_file, root_descriptor
    from .papers import PaperStore
    from .tasks import TaskStore
    parser = argparse.ArgumentParser(description='Prepare official engine source on HPC; no build or simulation')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--paper-id', required=True)
    parser.add_argument('--audit-directory', required=True, type=Path)
    parser.add_argument('--policy', required=True, type=Path)
    args = parser.parse_args()
    with root_descriptor(args.policy.parent) as root:
        policy = json.loads(read_file(root, args.policy.name, 10000))
    report = prepare_paper_engine(PaperStore(TaskStore(args.database)), args.paper_id,
                                  args.audit_directory, policy)
    print(json.dumps({key: report.get(key) for key in ('id', 'state', 'files', 'source_bytes', 'failure')}))


if __name__ == '__main__':
    main()
