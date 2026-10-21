"""Explicit private deployment assembly for the ordinary author A controller.

Only the administrator startup argument names this file. No browser/API field can
provide paths, source bytes, executable mappings, evaluation identities or grants.
GET/prepare inspect local metadata only; transport is used solely by user actions.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

from . import runtime_launcher as runtime
from .author_authorization import AuthorAuthorization
from .author_dispatch import (AuthorDispatch, AuthorFollowingService, AuthorOutputCollector,
    AuthorSlurmSubmitter, AuthorStageClient)
from .author_execution import _deployment, _resources
from .batch_plan import BatchEnvironment
from .hpc_transport import SavedHPCTransport, transport_identity
from .manifest import canonical, private_directory, sha256, validate_provenance
from .reconciliation import ReconciliationService
from .slurm_read import SlurmReader
from .staging import StageEndpoint, StagingService
from .submission import SubmissionService
from .tasks import TaskError, task_id


def _artifact(spec):
    if (not isinstance(spec, dict) or set(spec) != {'path', 'sha256'}
            or not isinstance(spec['path'], str) or not Path(spec['path']).is_absolute()):
        raise TaskError('作者执行 profile 字段需要固定的本地私有元数据文件及 SHA-256。')
    runtime.hash_value(spec['sha256'])
    raw = runtime.read_regular(Path(spec['path']), 1024**2, private=True)
    if sha256(raw) != spec['sha256']:
        raise TaskError('作者执行 profile 元数据摘要发生变化。')
    return raw


def _plan_source_files(row,evidence,report):
    """Add only AI-declared required accounted inputs, with a distinct origin."""
    files=deepcopy(row['source_files']);known={item['path'] for item in files}
    receipts=row['deployment'].get('execution_policy',{}).get('accounted_inputs',[])
    if not receipts:return files
    if receipts!=evidence.get('reference_inputs'):
        raise TaskError('作者方案没有保留固定记账输入的独立回执。')
    required={item['path']:item['sha256'] for call in report['invocations'] for item in call['required_files']}
    for receipt in receipts:
        for item in receipt['files']:
            if item['path'] in required and item['path'] not in known:
                if item['sha256']!=required[item['path']]:raise TaskError('AI 所需旧输入摘要与记账回执不一致。')
                files.append(dict(item,binding_sha256=receipt['binding_sha256'],source_path=item['path']));known.add(item['path'])
    return files


def load_author_dispatch(workspace, path, hpc):
    """Build real services from explicit existing credentials and ledger entries."""
    path = Path(path)
    raw = runtime.read_regular(path, 4*1024**2, private=True)
    checksum = sha256(raw); config = json.loads(raw)
    if (not isinstance(config, dict) or set(config) != {'version', 'audit_directory',
            'snapshots_directory', 'collections_directory', 'registrations'}
            or type(config['version']) is not int or config['version'] != 1
            or not isinstance(config['registrations'], list) or len(config['registrations']) > 256):
        raise TaskError('作者 A 执行私有配置 version、目录或 registrations 字段不完整。')
    ledger = workspace.papers.ledger
    if ledger is None:
        raise TaskError('作者 A 执行缺少既有参考账本。')
    directories = {}
    for key in ('audit_directory', 'snapshots_directory', 'collections_directory'):
        value = config[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise TaskError('作者执行 '+key+' 必须是显式私有绝对目录。')
        directories[key] = private_directory(value)
        if any((parent/'.git').exists() for parent in (directories[key], *directories[key].parents)):
            raise TaskError('作者执行产物目录不能位于代码仓库内。')
    registrations = {}
    expected = {'task_id', 'connection_revision', 'evaluation', 'resources', 'source_files',
        'deployment', 'provenance', 'profile', 'stage_endpoint', 'submission_endpoint',
        'collection_endpoint', 'authorization', 'batch', 'following'}
    for row in config['registrations']:
        if not isinstance(row, dict) or set(row) != expected:
            missing = sorted(expected-set(row)) if isinstance(row, dict) else sorted(expected)
            raise TaskError('作者 A 执行注册缺少字段：'+', '.join(missing or ['unexpected_fields']))
        identifier = task_id(row['task_id'])
        if identifier in registrations:
            raise TaskError('作者执行任务注册不能重复。')
        workspace.tasks.get(identifier)
        resources = _resources(row['resources']); _deployment(row['deployment'], resources)
        validate_provenance(row['provenance'])
        evaluation = ledger.evaluation_snapshot(row['evaluation'])
        if (evaluation['identity']['role'] != 'reference'
                or evaluation['identity']['task'] != row['provenance']['task_sha256']
                or evaluation['identity']['system'] != row['provenance']['software_sha256']):
            raise TaskError('作者 execution.evaluation 必须绑定原参考角色、任务和软件身份；未创建新评测。')
        endpoints = [StageEndpoint(**row[name]) for name in
                     ('stage_endpoint', 'submission_endpoint', 'collection_endpoint')]
        if len({(item.host_alias, item.root_path, item.python_path) for item in endpoints}) != 1:
            raise TaskError('作者 stage/submission/collection 端点必须属于同一部署。')
        transport = SavedHPCTransport(hpc, row['connection_revision'], endpoints[0].host_alias)
        profile_raw = _artifact(row['profile']); profile = json.loads(profile_raw)
        runtime.validate_deployment_paths(profile); runtime.deployment_parallelism(profile, resources['cores'])
        deployment = row['deployment']
        registered = {key: value for key, value in deployment.items() if key != 'profile_sha256'}
        if (runtime.execution_mode(profile) != 'native_slurm'
                or profile.get('requests_root') != endpoints[0].root_path
                or profile.get('author_reference') != registered
                or profile.get('author_reference_runtime_sha256') != endpoints[2].helper_sha256
                or sha256(profile_raw) != deployment['profile_sha256']):
            raise TaskError('作者 deployment/profile、源目录或 runtime 摘要不一致。')
        batch = row['batch']; following = row['following']; authorization = row['authorization']
        if not isinstance(batch, dict) or set(batch) != {'partition', 'account'}:
            raise TaskError('作者 batch 缺少明确 partition/account。')
        BatchEnvironment(**batch, root_path=endpoints[0].root_path, python_path=endpoints[0].python_path,
            launcher_path=endpoints[2].helper_path, launcher_sha256=endpoints[2].helper_sha256)
        if (not isinstance(following, dict) or set(following) != {'max_polls', 'interval_seconds',
                'query_timeout', 'query_max_bytes', 'retain_queue_identity'}
                or type(following['max_polls']) is not int or not 1 <= following['max_polls'] <= 240
                or type(following['interval_seconds']) is not int or not 15 <= following['interval_seconds'] <= 3600
                or type(following['query_timeout']) is not int or not 1 <= following['query_timeout'] <= 60
                or type(following['query_max_bytes']) is not int or not 1024 <= following['query_max_bytes'] <= 4_000_000
                or type(following['retain_queue_identity']) is not bool):
            raise TaskError('作者 following 需要明确且有界的查询/间隔配置。')
        if (not isinstance(authorization, dict) or set(authorization) != {'directory', 'reviewed_commit',
                'scoring_sha256', 'lifetime_seconds'}
                or not isinstance(authorization['reviewed_commit'], str)
                or not re.fullmatch('[a-f0-9]{40}', authorization['reviewed_commit'])
                or type(authorization['lifetime_seconds']) is not int
                or not 60 <= authorization['lifetime_seconds'] <= 30*86400):
            raise TaskError('作者 authorization 缺少既有签名目录、代码版本、比较规则或有效期。')
        runtime.hash_value(authorization['scoring_sha256'])
        directory = Path(authorization['directory'])
        if not directory.is_absolute() or len(runtime.read_regular(directory/'grant.key', 32, private=True)) != 32:
            raise TaskError('作者 authorization.directory 缺少现有私有 grant.key；不会生成密钥。')
        registrations[identifier] = deepcopy(row)

    def verify_config():
        if sha256(runtime.read_regular(path, 4*1024**2, private=True)) != checksum:
            raise TaskError('作者执行私有配置已改变；请重新载入固定部署，未沿用旧批准。')

    def binding_provider(identifier):
        verify_config()
        row = registrations.get(identifier)
        if row is None:
            raise TaskError('本任务尚未登记作者 A 的可信执行部署。')
        source, paper, receipt, scope = workspace._metadata(identifier)
        if getattr(workspace,'lane_deployment',None) is not None:
            source=workspace.lane_deployment.planner_binding(identifier,source)
        history = workspace.planner.history(identifier)
        if not history or any(item['receipt'] is None or item['state'] == 'unknown' for item in history):
            raise TaskError('作者 A 原方案尚未准备或有未知模型请求，不能换旧稿执行。')
        latest = history[-1]
        if latest['state'] != 'prepared' or latest.get('report') is None:
            raise TaskError('最新作者 A 方案尚未完整通过；保留原稿与轮次。')
        _, context, result, _ = workspace.planner._stored(identifier, latest['request_id'])
        evidence = context['input']; report = result['report']
        if (evidence['paper_id'] != paper['id'] or evidence['paper'] != {key:paper[key] for key in ('title','doi')}
                or evidence['reference_scope']['id'] != scope['id']
                or evidence['reference_scope']['receipt_sha256'] != scope['receipt_sha256']
                or evidence['reference_scope']['scope_sha256'] != scope['scope_sha256']
                or evidence['deployment'] != source['deployment']):
            raise TaskError('作者方案的当前论文、P 范围或环境菜单已经变化。')
        original = {item['path']: item for item in evidence['author_source']['files']}
        if any(item['path'] not in original or any(item[key] != original[item['path']][key] for key in ('size','sha256'))
               for item in receipt['files']):
            raise TaskError('作者方案未保留原始已核验文件绑定。')
        endpoint = StageEndpoint(**row['stage_endpoint'])
        transport = SavedHPCTransport(hpc, row['connection_revision'], endpoint.host_alias)
        if transport_identity(source['reader']) != transport.identity:
            raise TaskError('作者源码与执行不能切换保存的 HPC 连接。')
        if sha256(_artifact(row['profile'])) != row['deployment']['profile_sha256']:
            raise TaskError('作者运行 profile 已改变。')
        return dict(report=deepcopy(report), evidence=deepcopy(evidence),source_files=_plan_source_files(row,evidence,report),
            **{key:deepcopy(row[key]) for key in ('deployment', 'resources', 'provenance', 'evaluation')},
            task_revision=workspace.tasks.get(identifier)['revision'], transport_identity=deepcopy(transport.identity))

    def runtime_provider(binding, package):
        verify_config()
        identifier = binding['evidence']['task_id']; row = registrations[identifier]
        if (binding['source_files']!=_plan_source_files(row,binding['evidence'],binding['report'])
                or any(binding[key] != row[key] for key in ('deployment', 'resources', 'provenance', 'evaluation'))):
            raise TaskError('作者原作业的部署或评测绑定与当前注册不同；未迁移或重新提交。')
        stage, submit, collect = [StageEndpoint(**row[key]) for key in
                                  ('stage_endpoint', 'submission_endpoint', 'collection_endpoint')]
        transport = SavedHPCTransport(hpc, row['connection_revision'], stage.host_alias)
        if transport.identity != binding['transport_identity']:
            raise TaskError('作者原执行连接已改变，未迁移已有作业。')
        audit = private_directory(directories['audit_directory']/identifier)
        client = AuthorStageClient(stage, audit/'stage', transport=transport)
        scheduler = AuthorSlurmSubmitter(ledger, submit, audit/'submission', transport=transport)
        collector = AuthorOutputCollector(ledger, collect, directories['collections_directory'],
            snapshots=directories['snapshots_directory'], transport=transport)
        follow = row['following']
        reader = SlurmReader(stage.host_alias, audit/'queries', timeout=follow['query_timeout'],
            max_bytes=follow['query_max_bytes'], retain_queue_identity=follow['retain_queue_identity'],
            transport=transport, accepted_receipt=scheduler.accepted_identity)
        following = AuthorFollowingService(ledger, ReconciliationService(ledger, reader), collector,
            max_polls=follow['max_polls'], interval_seconds=follow['interval_seconds'])
        auth = row['authorization']; deployment = row['deployment']
        authorization = AuthorAuthorization(auth['directory'], tasks=workspace.tasks, task_id=identifier,
            ledger=ledger, endpoint=submit, audit_directory=audit/'authorization',
            profile_sha256=deployment['profile_sha256'], reviewed_commit=auth['reviewed_commit'],
            approval_sha256=deployment['approval_sha256'], scoring_sha256=auth['scoring_sha256'],
            software_sha256=row['provenance']['software_sha256'], lifetime_seconds=auth['lifetime_seconds'], transport=transport)
        environment = BatchEnvironment(**row['batch'], root_path=collect.root_path, python_path=collect.python_path,
            launcher_path=collect.helper_path, launcher_sha256=collect.helper_sha256)
        return dict(staging=StagingService(ledger, client), submission=SubmissionService(ledger, scheduler),
            authorization=authorization, following=following, environment=environment)

    service=AuthorDispatch(workspace.tasks, workspace.papers, ledger, binding_provider=binding_provider,
        runtime_provider=runtime_provider, snapshots=directories['snapshots_directory'])
    service.collections=directories['collections_directory']
    return service
