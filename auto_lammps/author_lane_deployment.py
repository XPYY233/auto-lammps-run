"""Immutable, explicitly approved installation of reference-lane metadata.

The server derives every path and byte from existing project services. Installing
these control helpers neither registers an executable policy nor issues a grant.
No author input, engine, scientific interpreter or scheduler is executed here.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path, PurePosixPath
import re

from . import remote_stage, remote_submit, runtime_launcher as runtime
from .author_reference_plan import author_source_inputs
from .hpc_transport import SavedHPCTransport, transport_identity
from .manifest import canonical, private_directory, sha256
from .slurm_read import _capture, _write_new
from .tasks import TaskError, task_id


VERSION = 1
PURPOSE = 'author_lane_metadata_installation_only'
FLAGS = dict(scope='reference_only', B_input=False, execution_authorized=False,
    execution_ready=False, physical_evaluation_performed=False, scientific_validation=False,
    child_process_policy_verified=False, author_entrypoints_reviewed=False,
    calls_model=False, submits_HPC=False)
HELPERS = {'runtime_launcher.py': runtime, 'remote_stage.py': remote_stage,
           'remote_submit.py': remote_submit}
EXECUTION_MISSING = [
    'author_entrypoint_and_same_allocation_child_policy_review',
    'original_file_executable_modes_and_interpreter_resolution',
    'existing_accounted_reference_output_input_binding',
    'reference_grant_signing_registration',
    'scheduler_submission_configuration_and_request_storage_policy',
]
LIMIT = 2 * 1024 * 1024


def _remote_install(request, *, action='install', _test_host=False):
    """Standalone stdlib-only worker. Reconciliation never creates or repairs."""
    import base64
    import hashlib
    import json
    import os
    from pathlib import Path, PurePosixPath
    import re
    import stat
    import sys

    def encoded(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    digest = lambda value: hashlib.sha256(value).hexdigest()
    def absolute(value):
        if (not isinstance(value, str) or not value.startswith('/') or value == '/'
                or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts
                or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('invalid_lane_path')
        return Path(value)
    def read(path, size):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o400 or info.st_size != size):
                raise ValueError('unsafe_lane_file')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                value = stream.read(size + 1)
            if len(value) != size:
                raise ValueError('changed_lane_file')
            return value
        finally:
            os.close(fd)
    def directory(path, *, create=False, private=False):
        # Walk from / using O_NOFOLLOW, including all existing ancestors.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(path.parts[1:]):
                if create:
                    try: os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError: pass
                access = os.O_RDONLY if index == len(path.parts) - 2 else getattr(os, 'O_PATH', os.O_RDONLY)
                next_fd = os.open(part, access | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = next_fd
                if private and index == len(path.parts) - 2:
                    info = os.fstat(fd)
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise ValueError('private_lane_directory_required')
            return fd
        except Exception:
            os.close(fd); raise
    def once(path, value):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())

    if not _test_host and (sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION')):
        raise ValueError('saved_HPC_SSH_required')
    expected = {'version', 'purpose', 'id', 'task_id', 'candidate_sha256', 'base_directory',
                'lane_directory', 'files', 'worker_sha256', 'existing_files', 'aliases'}
    if (not isinstance(request, dict) or set(request) != expected or request['version'] != 1
            or request['purpose'] != 'author_lane_metadata_installation_only'
            or action not in {'install', 'reconcile'}
            or any(not isinstance(request[key], str) or not re.fullmatch('[a-f0-9]{32}', request[key])
                   for key in ('id', 'task_id'))
            or any(not isinstance(request[key], str) or not re.fullmatch('[a-f0-9]{64}', request[key])
                   for key in ('candidate_sha256', 'worker_sha256'))):
        raise ValueError('invalid_lane_request')
    base = absolute(request['base_directory']); lane = absolute(request['lane_directory'])
    if lane != base / 'author-lanes' / request['id']:
        raise ValueError('lane_outside_fixed_namespace')
    existing = request['existing_files']
    if not isinstance(existing, dict) or not 5 <= len(existing) <= 136:
        raise ValueError('existing_environment_pins_required')
    for name, expected_sha in existing.items():
        path = absolute(name)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 256*1024**2:
                raise ValueError('unsafe_existing_environment_file')
            checksum = hashlib.sha256()
            with os.fdopen(os.dup(fd), 'rb') as stream:
                while True:
                    block = stream.read(1024**2)
                    if not block: break
                    checksum.update(block)
            if checksum.hexdigest() != expected_sha:
                raise ValueError('existing_environment_bytes_changed')
        finally:
            os.close(fd)
    files = request['files']
    required = {'runtime/runtime.json', 'runtime/runtime_launcher.py', 'runtime/remote_stage.py',
                'runtime/remote_submit.py', 'metadata.json'}
    complete = required | {'runtime/submission.json','requests/policy.json','requests/.stage.lock','control/source-policy.json','control/grant.key'}
    if (not isinstance(files, list) or {row.get('path') for row in files} not in (required,complete)
            or len(files) != len({row.get('path') for row in files})):
        raise ValueError('invalid_lane_file_inventory')
    decoded = {}
    for row in files:
        if not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256', 'base64'}:
            raise ValueError('invalid_lane_file')
        value = base64.b64decode(row['base64'], validate=True)
        if (type(row['size']) is not int or len(value) != row['size'] or not 0 <= len(value) <= 1000000
                or digest(value) != row['sha256']):
            raise ValueError('lane_file_digest_mismatch')
        decoded[row['path']] = value
    aliases=request['aliases']
    if not isinstance(aliases,list) or len(aliases)>16:raise ValueError('invalid_child_alias_inventory')
    for alias in aliases:
        if (not isinstance(alias,dict) or set(alias)!={'name','path','sha256'}
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',alias['name'])
                or existing.get(alias['path'])!=alias['sha256']):raise ValueError('unverified_child_alias')
    fixed = dict(version=1, purpose=request['purpose'], id=request['id'], task_id=request['task_id'],
        candidate_sha256=request['candidate_sha256'], request_sha256=digest(encoded(request)),
        worker_sha256=request['worker_sha256'], lane_directory=str(lane),
        files=[{key: row[key] for key in ('path', 'size', 'sha256')} for row in files],
        aliases=aliases,
        scope='reference_only', B_input=False, execution_authorized=False, execution_ready=False,
        physical_evaluation_performed=False, scientific_validation=False)
    base_fd = directory(base); os.close(base_fd)
    try:
        lane_fd = directory(lane, private=True); os.close(lane_fd)
    except FileNotFoundError:
        if action == 'reconcile': return dict(fixed, state='not_started')
        parent_fd = directory(base / 'author-lanes', create=True, private=True); os.close(parent_fd)
        os.mkdir(lane, 0o700)
        for name in ('runtime', 'control', 'requests'): os.mkdir(lane / name, 0o700)
        for name, value in decoded.items(): once(lane / name, value)
        if aliases:
            (lane/'runtime/child-bin').mkdir(mode=0o700)
            for alias in aliases:os.symlink(alias['path'],lane/'runtime/child-bin'/alias['name'])
        report = dict(fixed, state='metadata_installed')
        once(lane / 'installation.json', encoded(report))
        lane_fd = directory(lane, private=True)
        try: os.fsync(lane_fd)
        finally: os.close(lane_fd)
    else:
        marker = lane / 'installation.json'
        if not marker.exists(): return dict(fixed, state='unknown')
        report = dict(fixed, state='metadata_installed')
        if read(marker, len(encoded(report))) != encoded(report):
            raise ValueError('existing_lane_receipt_differs')
    for name in ('runtime', 'control', 'requests'):
        fd = directory(lane / name, private=True); os.close(fd)
    for name, value in decoded.items():
        if read(lane / name, len(value)) != value: raise ValueError('installed_lane_bytes_changed')
    for alias in aliases:
        path=lane/'runtime/child-bin'/alias['name']
        if not path.is_symlink() or os.readlink(path)!=alias['path']:raise ValueError('installed_child_alias_changed')
    return report


REMOTE_CODE = inspect.getsource(_remote_install) + '''
import base64,json,sys
raw=sys.stdin.buffer.read(2097153)
if len(raw)>2097152:raise ValueError('oversized_lane_envelope')
value=json.loads(raw)
if set(value)!={'request','action'}:raise ValueError('invalid_lane_envelope')
print(base64.b64encode(json.dumps(_remote_install(value['request'],action=value['action']),ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).decode())
'''


class AuthorLaneDeployment:
    """Browser actions take IDs/digests only; providers belong to server startup.

    ``reviewed_helpers`` is the installed candidate's reviewed source manifest:
    {reviewed_commit: <40 hex>, files: {<three fixed names>: <SHA-256>}}.
    It cannot be inferred from a different historical B-helper review. Omitting
    it still gives a concrete source/policy preview, but cannot install bytes.
    """
    def __init__(self, workspace, controller, hpc, *, audit_directory,
                 reviewed_helpers=None, capture=_capture):
        self.workspace, self.controller, self.hpc = workspace, controller, hpc
        self.tasks = workspace.tasks
        self.audit = private_directory(audit_directory)
        self.reviewed_helpers = deepcopy(reviewed_helpers)
        self.policy = None
        self.capture = capture
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_lane_candidates ('
                'id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),binding_sha256 TEXT NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS author_lane_events ('
                'id TEXT NOT NULL REFERENCES author_lane_candidates(id),sequence INTEGER NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            for table in ('author_lane_candidates', 'author_lane_events'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} '
                               'BEGIN SELECT RAISE(ABORT,"immutable author lane deployment"); END')

    def _context(self, identifier, request):
        task = self.tasks.get(task_id(identifier)); task_id(request)
        life = self.tasks.lifecycle(identifier)
        if self.tasks.paused(identifier) or life['deleted'] or life['user_finished']:
            raise TaskError('任务已暂停或结束；作者部署历史保留。')
        controller = self.controller
        if (controller.tasks.path.resolve() != self.tasks.path.resolve()
                or controller.ledger.path.resolve() != self.workspace.papers.ledger.path.resolve()):
            raise TaskError('作者部署须复用当前项目控制器与原账本。')
        path = Path(controller.runtime_profile_path)
        raw = runtime.read_regular(path, 1000000, private=True)
        profile_sha = sha256(raw)
        if profile_sha != controller.authorization.pins['profile_sha256']:
            raise TaskError('原控制器固定 profile 摘要已变化。')
        profile = json.loads(raw); runtime.validate_deployment_paths(profile)
        if runtime.execution_mode(profile) != 'native_slurm':
            raise TaskError('作者基础部署只复用已有 native Slurm CPU 环境。')
        native = runtime.deployment_parallelism(profile, 1)
        adapters = (controller.staging.client, controller.submission.scheduler,
                    controller.following.analysis.collector, controller.following.reconciliation.reader)
        transport = getattr(adapters[0], 'transport', None)
        if (not isinstance(transport, SavedHPCTransport) or transport._identity() != transport.identity
                or any(transport_identity(adapter) != transport.identity for adapter in adapters)):
            raise TaskError('作者基础部署须使用同一个已保存 HPC 连接。')
        endpoints = [adapter.endpoint for adapter in adapters[:3]]
        if any((item.host_alias, item.root_path, item.python_path) !=
               (endpoints[0].host_alias, profile['requests_root'], endpoints[0].python_path) for item in endpoints):
            raise TaskError('原部署端点与固定 profile 不一致。')
        source, paper, receipt, scope = self.workspace._metadata(identifier)
        with self.tasks.transaction() as db:
            evaluations = [row[0] for row in db.execute('SELECT evaluation FROM paper_evaluations WHERE task_id=? ORDER BY evaluation', (identifier,))]
        policies = []
        with controller.ledger._transaction() as db:
            for evaluation in evaluations:
                row = db.execute('SELECT campaign,identity FROM evaluations WHERE id=?', (evaluation,)).fetchone()
                if row is None: raise TaskError('原参考账本身份缺失。')
                if json.loads(row['identity'])['role'] == 'reference':
                    policies.append(dict(evaluation=evaluation, policy=controller.ledger._policy(db, row['campaign'])))
        if not policies: raise TaskError('作者部署必须保留既有参考评测身份和资源策略。')
        limits = source['deployment']['resources']
        resources = dict(cores=min([32, native['max_ranks'], limits.get('cores', native['max_ranks'])] + [row['policy']['max_cores'] for row in policies]),
            wall_seconds=min([604800] + [row['policy']['max_wall_seconds'] for row in policies]),
            memory_bytes=min([8*1024**3] + [row['policy']['max_memory_bytes'] for row in policies]),
            storage_bytes=min(row['policy']['total_storage_bytes'] for row in policies))
        source_transport = getattr(source['reader'], 'transport', None)
        if not isinstance(source_transport, SavedHPCTransport):
            raise TaskError('作者原件须属于已保存的项目连接。')
        identity = lambda value: {key: item for key, item in value.items() if key != 'host_alias'}
        if (source_transport._identity() != source_transport.identity
                or identity(source_transport.identity) != identity(transport.identity)):
            raise TaskError('作者原件与执行部署不能切换保存连接。')
        retained = self.workspace._cached_sources(identifier, source, receipt)
        checked = author_source_inputs(retained, identifier)
        if not checked['sources'] or not any(row.get('state') == 'read' for row in checked['reads']):
            raise TaskError('作者部署候选必须引用实有已读原件。')
        packages = retained.get('source_packages', [retained]); roots = []
        for package in packages:
            member = author_source_inputs(package, identifier)
            paths = {row['request']['remote_root'] for row in package['reads']}
            if not paths: continue
            if len(paths) != 1: raise TaskError('同一作者来源包不能指向多个源根。')
            root = paths.pop(); runtime.absolute(root)
            pin = sha256(canonical(member['capability']))
            row = dict(binding_sha256=pin, path=root)
            if row not in roots: roots.append(row)
        if not 1 <= len(roots) <= 4: raise TaskError('实有作者原件源根尚未核验。')
        inventory = self.workspace.environment_inventory
        if inventory is None: raise TaskError('请先通过普通页面保存已有 CPU/Python 环境核验回执。')
        latest = inventory.status(identifier)['latest']
        if latest is None or latest['state'] != 'metadata_ready':
            raise TaskError('已有 CPU/Python 元数据尚未核验；未知请求须核对原请求。')
        menu = inventory.planner_environments(identifier, latest['request_id'])
        environment_binding, environment_transport = inventory._context(identifier)
        if (environment_binding['profile_sha256'] != profile_sha
                or identity(environment_transport.identity) != identity(transport.identity)
                or menu['report_sha256'] != latest['report_sha256']):
            raise TaskError('环境核验回执不属于当前固定部署。')
        report = latest['report']
        verified = {row['path']: row['sha256'] for row in report['native_files'] if row['status'] == 'verified'}
        if verified != native['files']: raise TaskError('当前 CPU 固定文件没有完整核验。')
        python = [row for row in report['python_candidates'] if row['status'] == 'verified']
        if not any(row['path'] == endpoints[0].python_path or row['real_path'] == endpoints[0].python_path for row in python):
            raise TaskError('传输端点解释器尚无相同字节的环境核验。')
        helper_bytes = {name: Path(module.__file__).read_bytes() for name, module in HELPERS.items()}
        helper_sha = {name: sha256(value) for name, value in helper_bytes.items()}
        review = self.reviewed_helpers
        install_missing = []
        if review is None:
            install_missing.append('installed_helper_source_review_manifest')
        elif (not isinstance(review, dict) or set(review) != {'reviewed_commit', 'files'}
                or not isinstance(review['reviewed_commit'], str)
                or not re.fullmatch('[a-f0-9]{40}', review['reviewed_commit']) or review['files'] != helper_sha):
            raise TaskError('当前安装工具源码与受审候选清单不一致，未沿用旧 B 审核。')
        validation = self.workspace.scopes.get_author_validation(identifier)
        binding = dict(task_id=identifier, task_revision=task['revision'], paper_id=paper['id'],
            scope_sha256=scope['scope_sha256'], scope_receipt_sha256=scope['receipt_sha256'],
            author_validation_sha256=validation.get('validation_sha256') if validation else None,
            base_profile_sha256=profile_sha, retained_source_sha256=sha256(canonical(retained)),
            source_files_sha256=sha256(canonical(checked['files'])), source_roots=roots,
            environment_request_id=latest['request_id'], environment_report_sha256=latest['report_sha256'],
            transport_identity_sha256=sha256(canonical(transport.identity)), helper_sha256=helper_sha,
            helper_review=deepcopy(review), worker_sha256=sha256(REMOTE_CODE.encode()))
        binding['reference_policies_sha256'] = sha256(canonical(policies))
        binding['resources'] = resources
        base = str(PurePosixPath(profile['requests_root']).parent)
        lane = str(PurePosixPath(base) / 'author-lanes' / request)
        for row in roots:
            original = PurePosixPath(row['path']); target = PurePosixPath(lane)
            if original == target or original in target.parents or target in original.parents:
                raise TaskError('作者原件与新控制目录必须分离。')
        # This fingerprint identifies the exact metadata-install approval bytes.
        # It does not claim any executable/entrypoint review has taken place.
        approval = sha256(canonical(dict(purpose=PURPOSE, request_id=request, binding=binding)))
        new_profile = deepcopy(profile)
        new_profile.update(requests_root=lane+'/requests', control_root=lane+'/control', runtime_tree=lane+'/runtime',
            author_reference_runtime_sha256=helper_sha['runtime_launcher.py'], author_reference=dict(
                schema_version=1, role='author_reference', approval_sha256=approval,
                execution_mode='native_slurm', formal_isolation=False, gpu_allowed=False,
                resources=resources, source_roots=roots, environments=[]))
        runtime.validate_deployment_paths(new_profile)
        metadata = dict(version=VERSION, purpose=PURPOSE, **FLAGS, task_id=identifier, request_id=request,
            binding=binding, metadata_approval_sha256=approval,
            execution_missing=EXECUTION_MISSING, environment_evidence_sha256=latest['report_sha256'],
            programs_metadata=deepcopy(python), installs_engine=False, installs_dependencies=False,
            installs_original_source=False, copies_grant_key=False)
        payloads = {'runtime/'+name: value for name, value in helper_bytes.items()}
        payloads.update({'runtime/runtime.json': canonical(new_profile), 'metadata.json': canonical(metadata)})
        files = [dict(path=name, size=len(value), sha256=sha256(value), base64=base64.b64encode(value).decode())
                 for name, value in sorted(payloads.items())]
        candidate = dict(version=VERSION, purpose=PURPOSE, **FLAGS, task_id=identifier, request_id=request,
            binding_sha256=sha256(canonical(binding)), lane_directory=lane, base_profile_sha256=profile_sha,
            profile_sha256=sha256(canonical(new_profile)), profile=new_profile,
            files=[{key: row[key] for key in ('path', 'size', 'sha256')} for row in files],
            helper_review=deepcopy(review), metadata_approval_sha256=approval,
            install_missing=install_missing, execution_missing=deepcopy(EXECUTION_MISSING),
            can_install_metadata=not install_missing)
        candidate_sha = sha256(canonical(candidate))
        remote = dict(version=VERSION, purpose=PURPOSE, id=request, task_id=identifier,
            candidate_sha256=candidate_sha, base_directory=base, lane_directory=lane, files=files,
            worker_sha256=binding['worker_sha256'], existing_files=dict(native['files'], **{
                row['real_path']: row['sha256'] for row in python}), aliases=[])
        return dict(binding=binding, candidate=candidate, candidate_sha256=candidate_sha, request=remote,
                    host_alias=endpoints[0].host_alias, python_path=endpoints[0].python_path), transport

    def _prepared_context(self, identifier, request):
        context, transport = self._context(identifier, request)
        reviewed = self.policy.approved(identifier) if self.policy is not None else None
        if reviewed is None: return context, transport
        policy = reviewed['candidate']; review_sha = reviewed['review_sha256']
        candidate = context['candidate']; remote = context['request']; profile = candidate['profile']
        binding = context['binding']; binding['source_policy_sha256'] = reviewed['candidate_sha256']
        binding['source_review_receipt_sha256'] = review_sha
        binding['existing_signing_key_sha256'] = policy['existing_signing_key_sha256']
        children = []
        python = policy['python']
        profile['native_slurm']['files'][python['real_path']] = python['sha256']
        if policy['child_scripts']:
            children = [dict(name=name,path=python['real_path'],sha256=python['sha256']) for name in ('python','python3')]
        roots = profile['author_reference']['source_roots']
        for receipt in policy['accounted_inputs']:
            row=dict(binding_sha256=receipt['binding_sha256'],path=receipt['root'])
            if row not in roots: roots.append(row)
        if len(roots)>4: raise TaskError('作者原件及独立记账输入合计不能超过四个固定源根。')
        environments=[];planner=[]
        for menu in policy['programs']:
            mapped=dict(id=menu['id'],sha256=menu['sha256'],executables=[dict(program,review_sha256=review_sha) for program in menu['executables']])
            environments.append(mapped)
            identifier_value='approved-lane-'+sha256(canonical(dict(menu=menu,review_sha256=review_sha)))[:24]
            environments.append(dict(mapped,id=identifier_value))
            planner.append(dict(id=identifier_value,label='已审作者入口与同分配子进程：'+menu['label'],sha256=menu['sha256'],
                executables=[dict(id=row['id'],label='已审原作者入口：'+row['id'],sha256=row['sha256'],kind=row['kind']) for row in menu['executables']]))
        approval = sha256(canonical(dict(purpose=PURPOSE,request_id=request,binding=binding)))
        profile['author_reference'].update(approval_sha256=approval,environments=environments,
            execution_policy=dict(schema_version=1,review_sha256=review_sha,file_modes=policy['file_modes'],
                child_bin_path=profile['runtime_tree']+'/child-bin' if children else None,
                child_executables=children,accounted_inputs=policy['accounted_inputs']))
        runtime.author_execution_policy(profile['author_reference'],native=profile['native_slurm'],runtime_tree=profile['runtime_tree'])
        payloads={row['path']:base64.b64decode(row['base64'],validate=True) for row in remote['files']}
        payloads['runtime/runtime.json']=canonical(profile)
        payloads['runtime/submission.json']=canonical(dict(runtime_path=profile['runtime_tree']+'/runtime_launcher.py',
            runtime_sha256=binding['helper_sha256']['runtime_launcher.py'],
            **{key:policy['submission_environment'][key] for key in ('sbatch_path','sbatch_sha256')}))
        payloads['requests/policy.json']=canonical(dict(schema_version=1,max_total_bytes=binding['resources']['storage_bytes'],approval_sha256=approval))
        payloads['requests/.stage.lock']=b''
        payloads['control/source-policy.json']=canonical(dict(candidate_sha256=reviewed['candidate_sha256'],
            review_receipt=reviewed['review_receipt'],review_sha256=review_sha))
        key=runtime.read_regular(Path(self.controller.authorization.directory)/'grant.key',32,private=True)
        if sha256(key)!=policy['existing_signing_key_sha256']:raise TaskError('既有签名材料已变化，未复制或沿用批准。')
        payloads['control/grant.key']=key
        metadata=json.loads(payloads['metadata.json'])
        metadata.update(binding=binding,source_policy_sha256=reviewed['candidate_sha256'],source_review_receipt_sha256=review_sha,
            copies_grant_key=True,metadata_approval_sha256=approval,
            execution_missing=['prepared_bounded_author_plan_and_separate_per_package_user_approval'])
        payloads['metadata.json']=canonical(metadata)
        files=[dict(path=name,size=len(value),sha256=sha256(value),base64=base64.b64encode(value).decode()) for name,value in sorted(payloads.items())]
        candidate.update(binding_sha256=sha256(canonical(binding)),profile_sha256=sha256(canonical(profile)),
            files=[{key:row[key] for key in ('path','size','sha256')} for row in files],metadata_approval_sha256=approval,
            source_review_sha256=review_sha,source_policy_sha256=reviewed['candidate_sha256'],
            execution_missing=metadata['execution_missing'],planner_environments=planner,copies_existing_signing_key=True)
        context['candidate_sha256']=sha256(canonical(candidate))
        remote.update(candidate_sha256=context['candidate_sha256'],files=files,aliases=children,
            existing_files=deepcopy(profile['native_slurm']['files']))
        return context,transport

    def planner_binding(self, identifier, binding):
        """Project the actually installed approved lane, without a remote read."""
        latest=self.status(identifier)['latest']
        if latest is None or latest['state']!='metadata_installed' or 'source_review_sha256' not in latest['candidate']:
            return binding
        row,_=self._current(identifier,latest['request_id']);candidate=row['context']['candidate']
        result=dict(binding,deployment=deepcopy(binding['deployment']),missing=deepcopy(binding.get('missing',[])))
        ids={menu['id'] for menu in result['deployment']['environments']}
        for menu in candidate['planner_environments']:
            if menu['id'] not in ids:result['deployment']['environments'].append(deepcopy(menu))
        resolved={'作者驱动的同一计算分配子进程策略尚须独立审核。','作者原始入口与执行部署尚须独立登记和批准。',
            '此次仅核验解释器元数据；作者流程所需依赖尚待核验，不能视为依赖完备。'}
        # A reviewed stdlib child is not evidence for scientific Python modules;
        # unregistered analysis programs remain unavailable in the new menu.
        result['missing']=[item for item in result['missing'] if item not in resolved]
        unavailable=[item for item in result['deployment']['missing'] if item not in resolved]
        result['deployment']['unavailable_capabilities']=unavailable
        result['deployment']['missing']=[]
        result['deployment']['registered_execution_environment_ids']=[menu['id'] for menu in candidate['planner_environments']]
        return result

    def activate(self, identifier):
        """Assemble the existing A controller from an installed immutable lane.

        This writes service-owned private metadata, not a browser-supplied
        configuration. It neither reserves an attempt nor issues a run grant.
        """
        latest=self.status(identifier)['latest']
        if latest is None or latest['state']!='metadata_installed' or 'source_review_sha256' not in latest['candidate']:
            raise TaskError('需先安装已明确批准源码策略的作者 lane。')
        row,_=self._current(identifier,latest['request_id']);context=row['context'];candidate=context['candidate']
        source,paper,receipt,scope=self.workspace._metadata(identifier)
        retained=self.workspace._cached_sources(identifier,source,receipt)
        packages=retained.get('source_packages',[retained]);files={}
        for package in packages:
            checked=author_source_inputs(package,identifier);pin=sha256(canonical(checked['capability']))
            for item in checked['files']:
                files.setdefault(item['path'],dict(item,binding_sha256=pin,source_path=item['path']))
        policy=candidate['profile']['author_reference']['execution_policy']
        if not policy['accounted_inputs']:raise TaskError('作者 lane 尚缺明确核验的旧记账输入。')
        original=self.controller.ledger.get(policy['accounted_inputs'][0]['request_id'])
        evaluation=self.controller.ledger.evaluation_snapshot(original['evaluation'])
        identity=evaluation['identity']
        if identity['role']!='reference' or not original['accounted']:
            raise TaskError('作者 lane 必须保留原参考评测和记账身份。')
        directory=private_directory(self.audit/'installed'/candidate['profile_sha256'])
        payloads={item['path']:base64.b64decode(item['base64'],validate=True) for item in context['request']['files']}
        from .remote_submit import private_bytes_once
        profile_path=directory/'runtime.json';private_bytes_once(profile_path,payloads['runtime/runtime.json'])
        signing=private_directory(directory/'signing');private_bytes_once(signing/'grant.key',payloads['control/grant.key'])
        deployment=dict(candidate['profile']['author_reference'],profile_sha256=candidate['profile_sha256'])
        resources=json.loads(original['resources'])
        from .author_execution import _deployment,_resources
        _deployment(deployment,_resources(resources))
        profile=candidate['profile'];runtime_path=profile['runtime_tree']
        def endpoint(name):return dict(host_alias=context['host_alias'],python_path=context['python_path'],
            root_path=profile['requests_root'],helper_path=runtime_path+'/'+name,
            helper_sha256=context['binding']['helper_sha256'][name])
        registration=dict(task_id=identifier,connection_revision=self.controller.staging.client.transport.revision,
            evaluation=original['evaluation'],resources=resources,source_files=list(files.values()),deployment=deployment,
            provenance=dict(task_sha256=identity['task'],software_sha256=identity['system'],analysis_sha256=scope['scope_sha256']),
            profile=dict(path=str(profile_path),sha256=candidate['profile_sha256']),
            stage_endpoint=endpoint('remote_stage.py'),submission_endpoint=endpoint('remote_submit.py'),
            collection_endpoint=endpoint('runtime_launcher.py'),
            authorization=dict(directory=str(signing),reviewed_commit=candidate['helper_review']['reviewed_commit'],
                scoring_sha256=scope['scope_sha256'],lifetime_seconds=30*86400),
            batch=dict(partition=self.controller.environment.partition,account=self.controller.environment.account),
            following=dict(max_polls=240,interval_seconds=60,query_timeout=20,query_max_bytes=1000000,retain_queue_identity=True))
        registrations=deepcopy(getattr(self,'_registrations',{}));registrations[identifier]=registration
        collections=self.policy.inputs.outputs.raw.collections
        config=dict(version=1,audit_directory=str(self.audit/'execution'),snapshots_directory=str(self.audit/'snapshots'),
            collections_directory=str(collections),registrations=list(registrations.values()))
        for key in ('audit_directory','snapshots_directory','collections_directory'):private_directory(config[key])
        raw=canonical(config);config_path=directory/('controller-'+sha256(raw)+'.json');private_bytes_once(config_path,raw)
        from .author_dispatch_config import load_author_dispatch
        self.workspace.execution=load_author_dispatch(self.workspace,config_path,self.hpc)
        self._registrations=registrations
        return dict(task_id=identifier,state='registered_not_submitted',profile_sha256=candidate['profile_sha256'],
            source_review_sha256=candidate['source_review_sha256'],execution_authorized=False,
            reserves_attempt=False,submits_HPC=False,scientific_validation=False)

    def _stored(self, identifier, request=None):
        self.tasks.get(task_id(identifier))
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT * FROM author_lane_candidates WHERE '+('id=?' if request else 'task_id=?')+' ORDER BY rowid',
                              (task_id(request) if request else identifier,)).fetchall()
            result = []
            for row in rows:
                context = json.loads(row['document'])
                if row['task_id'] != identifier or sha256(canonical(context)) != row['sha256']:
                    raise TaskError('作者部署候选身份或摘要不一致。')
                events = []; previous = row['sha256']
                for item in db.execute('SELECT * FROM author_lane_events WHERE id=? ORDER BY sequence', (row['id'],)):
                    value = json.loads(item['document'])
                    if sha256(canonical(value)) != item['sha256'] or value['previous_sha256'] != previous:
                        raise TaskError('作者部署追加历史摘要不一致。')
                    previous = item['sha256']; events.append(value)
                result.append(dict(context=context, events=events, context_sha256=row['sha256'], at=row['at']))
        return result

    @staticmethod
    def _summary(row):
        context = row['context']; events = row['events']
        latest = events[-1] if events else None
        approved = any(item['kind'] in {'metadata_install_approved','existing_author_resource_authorization_inherited'} for item in events)
        state = latest['state'] if latest else 'previewed'
        return dict(version=VERSION, purpose=PURPOSE, **FLAGS,
            task_id=context['candidate']['task_id'], request_id=context['candidate']['request_id'],
            candidate=deepcopy(context['candidate']), candidate_sha256=context['candidate_sha256'],
            state=state, metadata_install_approved=approved, can_approve=state == 'previewed',
            can_install=approved and state == 'approved' and context['candidate']['can_install_metadata'],
            can_reconcile=state == 'unknown', events=deepcopy(events), at=row['at'])

    def get(self, identifier, request):
        rows = self._stored(identifier, request)
        return self._summary(rows[0]) if rows else None

    def status(self, identifier):
        history = [self._summary(row) for row in self._stored(identifier)]
        return dict(version=VERSION, purpose=PURPOSE, **FLAGS, task_id=identifier,
            history=history, latest=history[-1] if history else None, reads_remote=False)

    def preview(self, identifier, request):
        existing = self.get(identifier, request)
        if existing is not None: return existing
        context, _ = self._prepared_context(identifier, request)
        data = canonical(context)
        with self.tasks.transaction() as db:
            old = db.execute('SELECT 1 FROM author_lane_candidates WHERE id=?', (request,)).fetchone()
            if not old:
                db.execute('INSERT INTO author_lane_candidates VALUES(?,?,?,?,?,?)',
                    (request, identifier, context['candidate']['binding_sha256'], data.decode(), sha256(data),
                     datetime.now(timezone.utc).isoformat()))
        return self.get(identifier, request)

    def _current(self, identifier, request):
        rows = self._stored(identifier, request)
        if not rows: raise TaskError('请先预览并保存作者基础部署候选。')
        row = rows[0]; current, transport = self._prepared_context(identifier, request)
        if current != row['context']:
            raise TaskError('作者来源、环境、安装工具或保存连接已变化；旧候选保留，未沿用批准。')
        return row, transport

    def _event(self, request, kind, state, **fields):
        with self.tasks.transaction() as db:
            row = db.execute('SELECT sha256 FROM author_lane_candidates WHERE id=?', (request,)).fetchone()
            previous = db.execute('SELECT sequence,sha256 FROM author_lane_events WHERE id=? ORDER BY sequence DESC LIMIT 1', (request,)).fetchone()
            value = dict(kind=kind, state=state, at=datetime.now(timezone.utc).isoformat(),
                previous_sha256=previous['sha256'] if previous else row['sha256'], **fields)
            db.execute('INSERT INTO author_lane_events VALUES(?,?,?,?)',
                (request, previous['sequence']+1 if previous else 1, canonical(value).decode(), sha256(canonical(value))))

    def approve(self, identifier, request, *, expected_candidate_sha256):
        row, _ = self._current(identifier, request)
        if expected_candidate_sha256 != row['context']['candidate_sha256']:
            raise TaskError('批准摘要必须与已预览的作者部署字节完全一致。')
        value = self._summary(row)
        if value['metadata_install_approved']: return value
        if not value['candidate']['can_install_metadata']:
            raise TaskError('当前安装工具缺少受审清单；先保留具体候选和缺项，不能批准安装。')
        self._event(request, 'metadata_install_approved', 'approved',
                    candidate_sha256=expected_candidate_sha256, execution_authorized=False)
        return self.get(identifier, request)

    def install(self, identifier, request):
        row, transport = self._current(identifier, request); value = self._summary(row)
        if value['state'] in {'metadata_installed', 'unknown', 'not_started'}: return value
        if not value['can_install']: raise TaskError('请先明确批准同一候选的元数据安装；这不批准作者执行。')
        for other in self._stored(identifier):
            if (other['context']['candidate']['binding_sha256'] == row['context']['candidate']['binding_sha256']
                    and self._summary(other)['state'] == 'unknown'):
                raise TaskError('同一作者部署仍有未知安装，须先核对原请求，不能换号重发。')
        self._event(request, 'metadata_install_intent', 'unknown', candidate_sha256=value['candidate_sha256'])
        return self._observe(identifier, request, 'install', transport)

    def ensure_resources(self,identifier,request):
        """Ordinary A action: inherit unchanged resources, with no new user gate.

        The explicit bounded-validation intent and original campaign/resource
        approvals authorize preparation. This never approves a scientific plan,
        consumes a model round, reserves an attempt, or submits a job.
        """
        task_id(identifier);task_id(request)
        validation=self.workspace.scopes.get_author_validation(identifier)
        if not validation or validation.get('state')!='recorded' or not validation.get('can_prepare_A_plan'):
            raise TaskError('作者有界验证资源准备须绑定已有显式用户范围；未改变原 P/B。')
        if self.policy is None:raise TaskError('原作者资源库尚未连接，不能另建科学流程。')
        inventory=self.policy.inputs
        latest=inventory.status(identifier)['latest']
        if latest is None:
            inventory.inspect(identifier,sha256(('accounted-inputs:'+identifier+':'+request).encode())[:32])
        else:
            if latest['state']=='unknown':latest=inventory.reconcile(identifier,latest['request_id'])
            if latest['state']=='not_started':inventory.inspect(identifier,latest['request_id'])
        inventory.planner_inputs(identifier)
        self.policy.approved(identifier)
        latest_lane=self.status(identifier)['latest']
        if latest_lane and latest_lane['state']=='unknown':
            self.reconcile(identifier,latest_lane['request_id'])
            latest_lane=self.status(identifier)['latest']
        if latest_lane and latest_lane['state']=='metadata_installed' and 'source_review_sha256' in latest_lane['candidate']:
            self.activate(identifier)
            return dict(state='resources_ready',task_id=identifier,source='inherited_existing_author_resource_bundle',
                execution_authorized=False,calls_model=False,submits_HPC=False)
        lane_request=sha256(('author-resources:'+identifier+':'+request).encode())[:32]
        preview=self.preview(identifier,lane_request)
        if not preview['candidate']['can_install_metadata']:
            raise TaskError('当前已安装工具缺少真实受审版本清单；没有新增科学审核。')
        if not preview['metadata_install_approved']:
            inherited=self.policy.approved(identifier)
            self._event(lane_request,'existing_author_resource_authorization_inherited','approved',
                candidate_sha256=preview['candidate_sha256'],resource_inheritance_sha256=inherited['review_sha256'],
                bounded_validation_receipt_sha256=validation['receipt_sha256'],new_user_gate=False,execution_authorized=False)
        value=self.get(identifier,lane_request)
        if value['state']=='unknown':value=self.reconcile(identifier,lane_request)
        elif value['state']=='approved':value=self.install(identifier,lane_request)
        if value['state']!='metadata_installed':
            raise TaskError('作者资源准备回执尚未核实；保留原请求，未消耗模型稿次或提交计算。')
        self.activate(identifier)
        return dict(state='resources_ready',task_id=identifier,source='inherited_existing_author_resource_bundle',
            execution_authorized=False,calls_model=False,submits_HPC=False)

    def reconcile(self, identifier, request):
        row, transport = self._current(identifier, request)
        if self._summary(row)['state'] != 'unknown': return self._summary(row)
        return self._observe(identifier, request, 'reconcile', transport)

    def _observe(self, identifier, request, action, transport):
        row = self._stored(identifier, request)[0]; context = row['context']
        command = transport.command(context['host_alias'], [context['python_path'], '-I', '-S', '-c', REMOTE_CODE])
        trace = private_directory(self.audit/(request+'-'+action+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
        _write_new(trace/'intent.json', dict(action=action, candidate_sha256=context['candidate_sha256'],
            command_sha256=sha256(canonical(command)), request_sha256=sha256(canonical(context['request']))))
        try:
            result = self.capture(command, timeout=60, max_bytes=LIMIT,
                input_chunks=[canonical(dict(request=context['request'], action=action))])
        except (OSError, ValueError):
            result = dict(returncode=None, failure='lane_transport_unverified', stdout='', stderr='')
        # Do not publish remote exception text, commands, credentials or source.
        summary = {key: result.get(key) for key in ('returncode', 'failure')}
        report = None
        if result.get('returncode') == 0 and not result.get('failure'):
            try:
                wire = base64.b64decode(result['stdout'], validate=True).strip()
                decoded = json.loads(base64.b64decode(wire, validate=True))
                remote = context['request']
                fixed = dict(version=VERSION, purpose=PURPOSE, id=request, task_id=identifier,
                    candidate_sha256=context['candidate_sha256'], request_sha256=sha256(canonical(remote)),
                    worker_sha256=sha256(REMOTE_CODE.encode()), lane_directory=remote['lane_directory'],
                    files=context['candidate']['files'], scope='reference_only', B_input=False,
                    aliases=remote['aliases'],
                    execution_authorized=False, execution_ready=False,
                    physical_evaluation_performed=False, scientific_validation=False)
                if (set(decoded) == set(fixed)|{'state'} and decoded['state'] in {'metadata_installed','unknown','not_started'}
                        and all(decoded.get(key) == value and type(decoded.get(key)) is type(value) for key,value in fixed.items())
                        and (action == 'reconcile' or decoded['state'] != 'not_started')):
                    report = decoded
            except (ValueError, KeyError, TypeError): pass
        state = report['state'] if report else 'unknown'
        _write_new(trace/'transport.json', dict(**summary, report_verified=report is not None, report=report))
        self._event(request, 'metadata_install_receipt', state, report=report,
                    transport=summary, candidate_sha256=context['candidate_sha256'])
        return self.get(identifier, request)
