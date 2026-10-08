"""Loopback task application with optional, administrator-configured NLP.

The browser can save operator model connections, and versioned HPC settings. Existing job connections stay fixed.
"""
import argparse
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, SecretStr
from fastapi.exceptions import RequestValidationError

from .tasks import FIELDS, FrozenTask, StaleTask, TaskError, TaskStore
from .literature import preview_csv
from .papers import PaperStore
from .deepseek import DeepSeekClient, ModelCalls, ModelError
from .condition_generation import complete_condition_draft, generate_condition_draft, legacy_condition_request_status
from .reference_generation import accounting_binding, generate_reference_draft, recover_reference_draft
from .manifest import ManifestError, canonical, read_file, root_descriptor, sha256
from .candidate_jobs import CandidateHistory, CandidateService
from .agent_candidates import CandidateError, PlanIterationLimit
from .results import ResultsReader
from .operator_workspace import ModelPreferences, ReferenceViews
from .paper_evidence import PaperEvidenceViews
from .model_connections import ModelConnections
from .hpc_connections import HPCConnections
from .raw_outputs import RawOutputs
from .runtime_launcher import ExecutionDenied as runtime_denied
from .session_activity import SessionActivity

ASSETS = Path(__file__).parent/'web_assets'


class LocalBoundary:
    """Strict origin/Host boundary and bounded bodies before framework parsing."""
    def __init__(self, app, *, authority):
        self.app, self.authority = app, authority

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope['headers']:
            headers.setdefault(key.lower(), []).append(value.decode('latin1'))
        status, error = None, ''
        if headers.get(b'host') != [self.authority]:
            status, error = 403, '只能从本机地址访问'
        if scope['method'] not in {'GET', 'HEAD'} and (
            headers.get(b'origin') != ['http://'+self.authority]
            or headers.get(b'x-task-review') != ['1']
            or headers.get(b'content-type') != ['application/json']
        ):
            status, error = 403, '请通过本机任务页面操作'
        body = bytearray()
        if status is None:
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > 131072:
                    status, error = 413, '请求过大'
                    break
                if not message.get('more_body', False):
                    break
        if status:
            return await JSONResponse({'detail': error}, status_code=status)(scope, receive, send)
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
        async def protected_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] += [(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    (b'referrer-policy', b'no-referrer'), (b'content-security-policy',
                     b"default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
            await send(message)
        await self.app(scope, bounded_receive, protected_send)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class NewTask(Input):
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=12000)
    mode: str


class Revision(Input):
    revision: StrictInt = Field(ge=1)


class TaskLifecycleInput(Revision):
    lifecycle_revision: StrictInt = Field(ge=0)
    action: str


class AddCondition(Revision):
    value: str
    unit: str
    origin: str
    source_locator: str
    applicability: str
    evidence_role: str


class SelectCondition(Revision):
    candidate_id: str
    reason: str


class ConfirmConditions(Revision):
    fields: list[str]


class InitialGeometrySelection(Revision):
    catalog_sha256: str = Field(pattern=r'^[a-f0-9]{64}$', min_length=64, max_length=64)
    pin: str = Field(pattern=r'^[a-f0-9]{64}$', min_length=64, max_length=64)


class LiteraturePreview(Input):
    csv_text: str


class LiteratureImport(Revision):
    csv_text: str
    source_sha256: str
    column: str
    field: str
    evidence_role: str
    method_class: str
    classification_basis: str


class LinkPaperTask(Revision):
    task_id: str


class PreferenceInput(Input):
    provider: str
    model: str = Field(max_length=100)
    revision: StrictInt = Field(ge=0)


class TargetSelection(Revision):
    selected_ids: list[str] = Field(min_length=1, max_length=256)
    exclusion_reason: str = Field(max_length=2000)


class ConditionInput(Revision):
    refine: bool = False
    attempt: int = Field(default=0, ge=0, le=20)
    retry_of: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class ApprovalInput(Revision):
    note: str | None = Field(default=None, max_length=1000)


class ReviseInput(Revision):
    note: str = Field(min_length=1, max_length=2000)


class CandidateInput(Revision):
    answers: str | None = Field(default=None, max_length=4000)


class GuidanceInput(Revision):
    note: str = Field(min_length=1, max_length=2000)


class PauseInput(Revision):
    paused: bool


class TargetPreview(Revision):
    selected_ids: list[str] = Field(max_length=256)
    exclusion_reason: str = Field(max_length=2000)


class ReferenceDraft(Revision):
    csv_texts: list[str] = Field(min_length=1, max_length=8)


class ConnectionInput(Input):
    provider: str
    model: str = Field(max_length=100)
    api_key: SecretStr | None = None
    remove: bool = False


class ProviderInput(Input):
    provider: str


class DiscussionInput(ProviderInput):
    request_id: str = Field(min_length=32, max_length=32)
    question: str = Field(min_length=1, max_length=4000)


class HPCInput(Input):
    connection_id: str | None = None
    as_new: bool = False
    management_revision: StrictInt | None = None
    revision: StrictInt = Field(ge=0)
    label: str
    host: str
    port: StrictInt = Field(ge=1,le=65535)
    username: str
    work_directory: str
    partition: str = ''
    account: str = ''
    authentication: str
    private_key: SecretStr | None = None
    known_hosts: SecretStr | None = None
    certificate: SecretStr | None = None

class HPCManagementInput(Input):
    connection_id: str
    operation: str
    management_revision: StrictInt = Field(ge=1)


class HPCCheckInput(Input):
    revision: StrictInt = Field(ge=1)


def create_app(store: TaskStore, *, port=8765, papers=None, model_client=None, candidate_service=None, results_reader=None,
               reference_model_client=None, reference_views=None, paper_evidence_views=None, model_connections=None, result_assistant_enabled=None, hpc_connections=None, collections_directory=None, execution_jobs=None, discovery_library=None, session_activity=None, geometry_catalog_client=None):
    if execution_jobs:
        if execution_jobs.tasks.path!=store.path:raise ValueError('Execution must share the task store')
        controller=execution_jobs.controller
        if results_reader is None:
            results_reader=ResultsReader(store,execution_jobs.ledger,controller.following.analysis.collector.directory,controller.following.analysis.directory)
        elif (results_reader.ledger.path!=execution_jobs.ledger.path or
              results_reader.collections!=controller.following.analysis.collector.directory or
              results_reader.reports!=controller.following.analysis.directory):
            raise ValueError('Execution and results must share the ledger and artifact directories')
        if candidate_service and candidate_service.snapshots!=controller.snapshots:
            raise ValueError('Candidate and execution services must share snapshots')
        if candidate_service:
            from .failure_recovery import failure_context
            candidate_service.failure_context_provider=lambda identifier:failure_context(execution_jobs,identifier)
            execution_jobs.on_failure=lambda identifier:candidate_service.enqueue(identifier,store.get(identifier)['revision'],
                answers='应用自动恢复：读取本任务最新已核验失败日志，诊断并最小修改现有方案；保留全部需求。修订待批准，不自动再次提交。')
    papers = PaperStore(store) if papers is None else papers
    if paper_evidence_views is None and reference_views is not None:
        paper_evidence_views = PaperEvidenceViews(reference_views.directory, papers)
    preferences = ModelPreferences(store)
    connections = model_connections or ModelConnections(store, assistant_enabled=result_assistant_enabled)
    hpc = hpc_connections or HPCConnections(store)
    if results_reader and results_reader.tasks.path!=store.path:
        raise ValueError('Results must belong to the same task store')
    if results_reader and collections_directory is not None and Path(collections_directory).absolute()!=results_reader.collections:
        raise ValueError('Raw downloads and results must share the same collection directory')
    raw_outputs = RawOutputs(store,papers,
        collections=results_reader.collections if results_reader else collections_directory,
        ledger=results_reader.ledger if results_reader else None)
    from .closeout import CloseoutViews
    closeouts = CloseoutViews(reference_views, raw_outputs) if reference_views else None
    from .discovery_library import DiscoveryLibrary
    discoveries = discovery_library or DiscoveryLibrary()
    preparations = CandidateHistory(store)
    if candidate_service and (candidate_service.tasks.path != store.path or candidate_service.client is not model_client):
        raise ValueError('Candidate service must share the task store and model policy')
    from .geometry_catalog import GeometryCatalogClient, validate_view
    # TaskStore's fixed-geometry save/freeze protocol currently supports at most
    # 100000 atoms. Larger generated-structure candidate limits do not extend it.
    fixed_geometry_max_atoms = min(candidate_service.max_atoms if candidate_service is not None else 100000, 100000)
    # Reuse the deployment's fixed helper, identity and audit trail. The browser
    # cannot supply another endpoint, resource path or structure bytes.
    if geometry_catalog_client is None and execution_jobs is not None:
        geometry_catalog_client = GeometryCatalogClient(execution_jobs.controller.staging.client,
            max_atoms=fixed_geometry_max_atoms)

    def geometry_catalog_view():
        if geometry_catalog_client is None:
            return {'configured': False, 'entries': [], 'reason': '尚未配置固定初始结构目录。可继续保存研究需求；已有文件需先由资源服务核对。'}
        try:
            view = validate_view(geometry_catalog_client.list(),
                max_atoms=fixed_geometry_max_atoms)
        except (ValueError, TypeError, KeyError, OSError):
            # Resource errors may contain a remote path or private diagnostics.
            # Reject the entire view and keep these outside browser responses.
            raise TaskError('初始结构目录暂未通过核对；未选择结构，请稍后重新读取') from None
        return {**view, 'configured': True,
                'reason': '' if view['entries'] else '目录已配置，目前没有已核对的初始结构。可继续保存研究需求。'}
    from .research_workflow import ResearchWorkflow
    workflow = ResearchWorkflow(candidate_service, execution_jobs) if candidate_service and execution_jobs else None
    @asynccontextmanager
    async def lifespan(app):
        if candidate_service:
            candidate_service.start()
        if execution_jobs:execution_jobs.start()
        if workflow:workflow.start()
        try:
            yield
        finally:
            if workflow:workflow.close()
            if execution_jobs:execution_jobs.close()
            if candidate_service:
                candidate_service.close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(LocalBoundary, authority=f'127.0.0.1:{port}')

    @app.exception_handler(TaskError)
    async def task_error(request: Request, exc: TaskError):
        return JSONResponse({'detail': str(exc)}, status_code=409 if isinstance(exc, (StaleTask, FrozenTask)) else 422)

    @app.exception_handler(KeyError)
    async def internal_key_missing(request: Request, exc: KeyError):
        # 之前把所有 KeyError 都报成"任务不存在"，会误导排查（例如事件缺少标签）。
        # 真正的"任务不存在"由 store 显式抛 TaskError；这里如实说明缺少哪个键。
        missing = exc.args[0] if exc.args else 'unknown'
        return JSONResponse({'detail': '内部状态缺少条目：' + str(missing)[:80]}, status_code=404)

    @app.exception_handler(ModelError)
    async def model_error(request: Request, exc: ModelError):
        explanations = {
            'model_budget_exhausted': '模型调用额度已用完，未发出新请求。',
            'request_already_reserved': '这次整理已发起，不会重复调用模型。请刷新查看已有条件。',
            'model_key_missing_or_invalid': '运行模型尚未完成服务端配置。',
            'incomplete_generation': '模型回答不完整，未导入条件。请求记录已保留。',
            'input_too_large': '需求文本超出当前模型输入限制，未发出请求。',
            'reference_request_requires_attention': '已有整理请求尚无可恢复的完成回执，请核对记录；不会重复调用。',
        }
        return JSONResponse({'detail': explanations.get(str(exc), '模型整理未完成，未自动重试。请求记录已保留。')}, status_code=422)

    @app.exception_handler(PlanIterationLimit)
    async def plan_iteration_limit(request: Request, exc: PlanIterationLimit):
        return JSONResponse({'detail': '方案已达到首版在内三轮上限，或历史轮次无法核验；已有完整方案仍可查看和批准，不能追加生成。',
                             'code': 'plan_iteration_limit'}, status_code=422)

    @app.exception_handler(CandidateError)
    async def candidate_error(request: Request, exc: CandidateError):
        return JSONResponse({'detail': '尚不能准备方案，请核对条件、服务配置与已有准备记录。'}, status_code=422)

    @app.exception_handler(ManifestError)
    async def artifact_error(request: Request, exc: ManifestError):
        return JSONResponse({'detail': '资料完整性检查未通过，未提供文件。请联系管理员核对。'}, status_code=409)

    @app.get('/')
    def index():
        return FileResponse(ASSETS/'index.html')

    @app.get('/assets/{name}')
    def asset(name: str):
        if name not in {'app.js', 'app.css', 'session.js', 'results-view.js', 'math-view.js', 'markdown-view.js'}:
            return JSONResponse({'detail': '文件不存在'}, status_code=404)
        return FileResponse(ASSETS/name)

    @app.get('/assets/katex/{name:path}')
    def math_asset(name: str):
        allowed=json.loads((ASSETS/'katex'/'manifest.json').read_text())['files']
        if name not in allowed:return JSONResponse({'detail':'文件不存在'},status_code=404)
        path=ASSETS/'katex'/name
        try:proof=sha256(path.read_bytes())
        except OSError:return JSONResponse({'detail':'公式渲染文件尚未正确安装'},status_code=409)
        if proof!=allowed[name]:return JSONResponse({'detail':'文件核验未通过'},status_code=409)
        return FileResponse(path)

    @app.get('/assets/markdown/{name}')
    def markdown_asset(name: str):
        allowed=json.loads((ASSETS/'markdown'/'manifest.json').read_text())['files']
        if name not in allowed:return JSONResponse({'detail':'文件不存在'},status_code=404)
        path=ASSETS/'markdown'/name
        try:proof=sha256(path.read_bytes())
        except OSError:return JSONResponse({'detail':'问答渲染文件尚未正确安装'},status_code=409)
        if proof!=allowed[name]:return JSONResponse({'detail':'文件核验未通过'},status_code=409)
        return FileResponse(path)

    # Local-entry page activity. Enabled only by --session-activity-file (desktop entry);
    # an ordinary launch keeps these routes inert and records nothing.
    @app.api_route('/api/session/activity', methods=['GET'])
    def session_state():
        if session_activity is None:
            return {'enabled': False, 'updated_at': None, 'sessions': {}}
        return {'enabled': True, **session_activity.snapshot()}

    def record_session(action):
        if session_activity is None:
            return JSONResponse({'detail': '本服务未启用页面活动记录'}, status_code=409)
        return {'enabled': True, **action}

    @app.api_route('/api/session/heartbeat', methods=['GET', 'POST'])
    def session_heartbeat(session: str = '', hidden: int = 0):
        if session_activity is None:
            return record_session(None)
        return record_session(session_activity.heartbeat(session, hidden=bool(hidden)))

    @app.api_route('/api/session/close', methods=['GET', 'POST'])
    def session_close(session: str = ''):
        if session_activity is None:
            return record_session(None)
        return record_session(session_activity.close(session))

    @app.get('/api/model-preference')
    def model_preference():
        return preferences.get()

    @app.post('/api/model-preference')
    def save_model_preference(data: PreferenceInput):
        return preferences.save(data.provider,data.model,data.revision)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request: Request, exc: RequestValidationError):
        # Framework validation bodies can contain secret input; never reflect them.
        return JSONResponse({'detail': '输入格式不正确，请检查字段。'}, status_code=422)

    @app.get('/api/model-connections')
    def connection_status():
        return connections.status()

    @app.post('/api/model-connections')
    def save_connection(data: ConnectionInput):
        return connections.save(data.provider, data.model,
            data.api_key.get_secret_value() if data.api_key else None, remove=data.remove)

    @app.post('/api/model-connections/models')
    def connection_models(data: ProviderInput):
        return connections.list_models(data.provider)

    @app.post('/api/model-connections/check')
    def check_connection(data: ProviderInput):
        if model_client is None:
            return JSONResponse({'detail':'尚未配置模型账本，无法进行真实调用自检。'},status_code=409)
        try:
            return connections.check(data.provider, calls=model_client.calls)
        except (TaskError, ModelError) as error:
            return JSONResponse({'detail':str(error)},status_code=409)

    @app.get('/api/hpc-connection')
    def hpc_status():
        # The execution service is wired from the private deployment; report the real state
        # instead of a hardcoded False that made the page and the AI believe it was absent.
        return hpc.status(execution_enabled=execution_jobs is not None)

    @app.post('/api/hpc-connection')
    def save_hpc(data: HPCInput):
        value=data.model_dump(exclude={'revision','private_key','known_hosts','certificate','connection_id','as_new','management_revision'})
        return hpc.save(value,data.revision,connection_id=data.connection_id,as_new=data.as_new,management_revision=data.management_revision,**{k:(getattr(data,k).get_secret_value() if getattr(data,k) is not None else None) for k in ('private_key','known_hosts','certificate')})

    @app.post('/api/hpc-connection/manage')
    def manage_hpc(data: HPCManagementInput):
        return hpc.manage(data.connection_id,data.operation,data.management_revision)

    @app.post('/api/hpc-connection/check')
    def check_hpc(data: HPCCheckInput):
        return hpc.check(data.revision)

    @app.get('/api/tasks/{identifier}/raw-files')
    def raw_files(identifier: str):
        try:return raw_outputs.listing(identifier)
        except (ValueError,KeyError,TypeError,OSError,runtime_denied):
            return JSONResponse({'detail':'原始输出记录暂不可核验。'},status_code=409)

    @app.get('/api/tasks/{identifier}/raw-files/{file_id}')
    def raw_file(identifier: str,file_id: str):
        try:name,size,stream=raw_outputs.download(identifier,file_id)
        except (ValueError,KeyError,TypeError,OSError,runtime_denied):
            return JSONResponse({'detail':'原始文件或所属计算未通过核验。'},status_code=409)
        def chunks():
            try:
                while data:=stream.read(1024*1024):yield data
            finally:stream.close()
        from starlette.background import BackgroundTask
        return StreamingResponse(chunks(),media_type='application/octet-stream',background=BackgroundTask(stream.close),
            headers={'Content-Disposition':'attachment; filename="'+name+'"','Content-Length':str(size)})

    @app.get('/api/tasks/{identifier}/discussion')
    def discussion_history(identifier: str):
        provider = preferences.get()['provider']
        return {'messages': connections.history(identifier), 'enabled': connections.available(provider),
                'provider': provider}

    @app.post('/api/tasks/{identifier}/discussion')
    def discuss_result(identifier: str, data: DiscussionInput):
        document=store.get(identifier)
        report = reference_views.get(identifier) if reference_views else None
        if report:
            context = {k: report[k] for k in ('scope','metrics','curves','limitations','scientific_status','report_sha256')}
        elif results_reader:
            context = results_reader.task(identifier)
            if not context.get('evaluations'): raise TaskError('尚无可分析的计算结果。')
            context['source_tables']=[]
            for group in context['evaluations']:
                for request in group['requests']:
                    # Execution history stays in the product; repeated scheduler
                    # events are not scientific evidence for a result question.
                    request.pop('history',None)
                    for analysis in request['reports']:
                        if analysis['status']=='analyzed':
                            preview=results_reader.tables(identifier,analysis['id'])
                            # Structural data is already fully verified in the
                            # report. Keep one copy in the model context instead
                            # of doubling every frame and pair statistic.
                            preview.pop('structural_results',None)
                            context['source_tables'].append(preview)
        else:
            raise TaskError('尚无可分析的计算结果。')
        # The result assistant needs the actual method and convergence criteria,
        # not generic assumptions inferred from a material or an energy unit.
        if document['status']=='conditions_frozen':
            frozen=json.loads(store.export(identifier))
            conditions={}
            for field, choices in frozen['conditions'].items():
                if field in {'reference','resources'}:continue
                selected=next((item for item in choices['candidates'] if item['id']==choices['selected']),None)
                if selected:
                    conditions[field]={key:selected[key] for key in ('value','unit','applicability')}
            context['frozen_scientific_conditions']=conditions
            context['condition_record_sha256']=document['record_sha256']
        return connections.discuss(identifier, data.request_id, data.provider, data.question, context)

    @app.get('/api/tasks/{identifier}/reference-result')
    def reference_result(identifier: str):
        store.get(identifier)
        if reference_views is None:return {'report':None}
        try:
            report = reference_views.get(identifier)
            if report is not None:
                report['closeout'] = closeouts.get(identifier, report)
            return {'report':report}
        except (ValueError,KeyError,TypeError,OSError,runtime_denied):
            return JSONResponse({'detail':'参考报告与原始记录未通过核验，暂不展示数值。'},status_code=409)

    def paper_projection(identifier):
        # Human-only route decoration: TaskStore/export/CandidateService remain
        # unchanged, so paper figures and targets never become B model inputs.
        return paper_evidence_views.get(identifier) if paper_evidence_views else None

    @app.get('/api/tasks/{identifier}/paper-evidence')
    def paper_evidence(identifier: str):
        store.get(identifier)
        try:
            return {'report': paper_projection(identifier)}
        except (ValueError, KeyError, TypeError, OSError, TaskError, runtime_denied):
            return JSONResponse({'detail':'文献工作台证据未通过来源或文件核验，原任务保留。'}, status_code=409)

    @app.get('/api/tasks/{identifier}/paper-evidence/files/{name}')
    def paper_evidence_file(identifier: str, name: str):
        if paper_evidence_views is None:
            return JSONResponse({'detail':'文献工作台证据尚未接入。'}, status_code=404)
        try:
            data = paper_evidence_views.download(identifier, name)
        except (ValueError, KeyError, TypeError, OSError, TaskError, runtime_denied):
            return JSONResponse({'detail':'文件不在已核验的文献证据中。'}, status_code=409)
        media = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.pdf':'application/pdf',
                 '.csv':'text/csv; charset=utf-8', '.json':'application/json', '.md':'text/markdown; charset=utf-8'}
        return Response(data, media_type=media.get(Path(name).suffix, 'application/octet-stream'),
                        headers={'Content-Disposition':'inline; filename="'+name+'"'})

    @app.get('/api/tasks/{identifier}/reference-result/files/{name}')
    def reference_file(identifier: str, name: str):
        if reference_views is None:return JSONResponse({'detail':'参考报告尚未接入。'},status_code=404)
        try:data=reference_views.download(identifier,name)
        except (ValueError,KeyError,TypeError,OSError,runtime_denied):
            return JSONResponse({'detail':'文件不在已核验的报告中。'},status_code=409)
        media={'.png':'image/png','.pdf':'application/pdf','.csv':'text/csv; charset=utf-8','.md':'text/markdown; charset=utf-8','.json':'application/json'}.get(Path(name).suffix,'application/octet-stream')
        return Response(data,media_type=media,headers={'Content-Disposition':'attachment; filename="'+name+'"'})

    @app.get('/api/tasks/{identifier}/closeout/files/{name}')
    def closeout_file(identifier: str, name: str):
        if closeouts is None:return JSONResponse({'detail':'验收资料尚未接入。'},status_code=404)
        try:data=closeouts.download(identifier,name)
        except (ValueError,KeyError,TypeError,OSError,runtime_denied):
            return JSONResponse({'detail':'验收资料未通过来源核验。'},status_code=409)
        media={'.png':'image/png','.pdf':'application/pdf','.csv':'text/csv; charset=utf-8',
               '.md':'text/markdown; charset=utf-8','.json':'application/json'}.get(Path(name).suffix,'application/octet-stream')
        return Response(data,media_type=media,headers={'Content-Disposition':'attachment; filename="'+name+'"'})

    @app.get('/api/schema')
    def schema():
        status = model_client.calls.status() if model_client else None
        reference_status = reference_model_client.calls.status() if reference_model_client else None
        from .resource_limits import POLICY_RECORD, description
        return {'fields': FIELDS, 'task_resource_policy': POLICY_RECORD | {'description': description()},
                'model_calls_enabled': bool(status and status['remaining_requests']),
                'model_status': status, 'execution_enabled': execution_jobs is not None,
                # Which copy of the application is really serving this port; the desktop launcher
                # refuses to run when it differs from the environment its configuration names.
                'installation': {'package': str(Path(__file__).resolve().parent), 'python': sys.executable},
                # 前端资产指纹：页面据此判断自己是否为旧版本并自动刷新。
                'assets': {name: (hashlib.sha256((ASSETS/name).read_bytes()).hexdigest()
                                  if (ASSETS/name).is_file() else None)
                           for name in ('app.js', 'app.css')},
                'automatic_workflow': workflow.availability() if workflow else {'configured':False,'enabled':False},
                'reference_generation': {'configured': reference_model_client is not None, 'model_status': reference_status},
                'candidate_preparation': candidate_service.availability() if candidate_service else
                    {'enabled': False, 'reason': '方案准备服务尚未配置。'}}

    def reproduction_status(document):
        """A user-accepted scope is a real reproduction result; science stays gated.

        Previously this version never emitted 'reproduced' at all, so a paper the user
        had accepted kept showing 复现中 and the in-app AI described it as unfinished.
        The accepted scope comes from the same closeout the task page shows.
        """
        if not closeouts or document.get('status') == 'pending':
            return document
        accepted = []
        for task in document.get('tasks') or []:
            try:
                acceptance = (closeouts.get(task['id']) or {}).get('acceptance') or {}
            except (ValueError, KeyError, TypeError, OSError, runtime_denied):
                continue
            if acceptance.get('status') == 'accepted_by_user':
                accepted.append({'task': task['id'], 'scope': acceptance.get('scope'),
                                 'date': acceptance.get('date')})
        if not accepted:
            return document
        scopes = '；'.join((item['scope'] or '').rstrip('。；') for item in accepted if item.get('scope'))
        return document | {'status': 'reproduced',
                           'stage': ('用户已验收的复现范围：' + (scopes or '（范围未记录）')
                                     + '。该范围是基准工况；论文其余工况、独立科学核验与评分发布尚未完成。'),
                           'reproduction_accepted': accepted}

    @app.get('/api/papers')
    def paper_list():
        return papers.list(transform=reproduction_status)

    @app.get('/api/resource-discoveries')
    def resource_discoveries():
        try:
            return discoveries.get()
        except (ValueError, KeyError, TypeError, OSError, runtime_denied):
            return JSONResponse({'detail': '发现清单未通过格式检查，已登记资源仍保留。'}, status_code=409)

    @app.get('/api/papers/{identifier}')
    def paper_get(identifier: str):
        return reproduction_status(papers.get(identifier))

    @app.post('/api/papers/{identifier}/select')
    def paper_select(identifier: str, data: Revision):
        return papers.select(identifier, data.revision)

    @app.post('/api/papers/{identifier}/tasks')
    def paper_task(identifier: str, data: LinkPaperTask):
        return papers.link_task(identifier, data.revision, data.task_id)

    # Serialize browser lifecycle changes with browser start intents in this process.
    from threading import RLock
    from functools import wraps
    task_management_lock=RLock()

    def serialized_task_action(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with task_management_lock:
                return fn(*args, **kwargs)
        return wrapped

    def require_open_task(identifier):
        state=store.lifecycle(identifier)
        if state['deleted'] or state['user_finished']:
            raise TaskError('此任务记录已结束或删除，请新建任务开展后续计算。')

    condition_failure_details = {
            'source_quote_mismatch': 'AI 改写了原文引文，来源核对未通过；条件没有导入。',
            'value_quote_mismatch': '提取的条件值没有出现在引用原文中；条件没有导入。',
            'unit_quote_mismatch': '单位没有出现在引用原文中；条件没有导入。',
            'source_identity_invalid': '原文位置或来源身份未通过核对；条件没有导入。',
            'output_contract_invalid': 'AI 答复格式未通过检查；条件没有导入。',
            'task_changed': '需求版本已变化，旧答复没有写入新版本。',
            'model_budget_exhausted': '模型调用额度不足，请核对模型设置；本次未发送，已有费用与记录保留。',
            'model_input_too_large': '需求超出当前模型输入容量；本次未发送，请核对需求与模型设置。',
            'model_request_failed': '模型请求未完成；实际调用与费用按账本保留，未发送不计为调用。',
            'model_state_unknown': '调用状态尚需核对，不会重复发送。',
            'import_rejected': '条件导入未通过检查，原条件和调用记录保留。',
    }

    def condition_progress(identifier):
        records = (legacy_condition_request_status(model_client, store, identifier)
                   if model_client is not None else store.condition_requests(identifier))
        if not records:
            return records, None
        latest = records[-1]
        summary = {k: latest.get(k) for k in ('request_id', 'state', 'label', 'error_code', 'updated_at', 'reconstructed', 'recovery_required')}
        detail = condition_failure_details.get(latest['error_code'], '模型答复与原文依据分开检查；核对通过后仍需你确认条件。')
        if latest.get('recovery_required'):
            detail = 'AI 答复已保存，条件尚未导入；恢复使用同一回执，不新增模型调用。'
        summary.update(call_count=sum(bool(call.get('reserved')) for call in latest['calls']), detail=detail)
        return records, summary

    @app.get('/api/tasks')
    def tasks():
        rows=store.list()
        for row in rows:
            _, progress = condition_progress(row['id'])
            if progress is not None:
                row['condition_preparation_state'] = progress['state']
                row['condition_preparation_label'] = progress['label']
        if papers.ledger is not None:
            for paper in papers.list()['papers']:
                for row in rows:
                    refs=[e for e in paper['evaluations'] if e.get('available') and
                          e['identity']['role']=='reference' and e['task_id']==row['id']]
                    state=('作者参考 A 已结束' if any(r['state']=='completed' and r['accounted'] for e in refs for r in e['requests']) else
                           '作者参考 A 已提交' if any(e['dispatch_claims'] for e in refs) else None)
                    if state:row['reference_stage']=state
                    agents=[e for e in paper['evaluations'] if e.get('available') and
                            e['identity']['role']=='agent' and e['task_id']==row['id']]
                    runs=[r for e in agents for r in e['requests']]
                    if runs:
                        row['execution_state']=runs[-1]['state']
                        row['job_id']=runs[-1]['job_id']
                        row['submission_count']=sum(e['dispatch_claims'] for e in agents)
        for row in rows:
            reference = papers.reference_progress(row['id'])
            if reference['entries']:
                evaluations = [item['evaluation'] for item in reference['entries']]
                runs = [request for evaluation in evaluations for request in evaluation.get('requests', [])]
                row['reference_available'] = reference['available']
                row['reference_submission_count'] = (sum(e.get('dispatch_claims', 0) for e in evaluations)
                                                     if reference['available'] else None)
                row['reference_state'] = (runs[-1]['state'] if runs else 'prepared') if reference['available'] else 'reconcile_required'
                row['reference_job_id'] = runs[-1]['job_id'] if runs else None
                row['reference_stage'] = ('作者参考 A · ' + {
                    'prepared': '尚未提交', 'running': '运行中', 'queued': '排队中', 'accepted': '已提交',
                    'completed': '计算结束 · 科学结果待核验', 'failed': '计算失败',
                    'timeout': '超时', 'unknown': '提交结果待核对',
                    'reconcile_required': '记录暂不可读，保留已有提交',
                }.get(row['reference_state'], '状态见提交记录'))
        if preparations:
            # 任务列表必须反映"方案准备"的真实进展，否则冻结后无论准备成功、失败还是
            # 需要补充条件，都会一律显示为"待准备"。
            for row in rows:
                try:
                    job = preparations.get(row['id'])
                except (ValueError, KeyError, TypeError, OSError, runtime_denied):
                    job = None
                if job is not None:
                    row['preparation_state'] = job.get('state')
                    row['preparation_label'] = job.get('label')
        if results_reader:
            for row in rows:
                try:row.update(results_reader.summary(row['id']))
                except (ValueError,KeyError,TypeError,OSError,runtime_denied):
                    row['execution_state']='reconcile_required'
                    row['execution_stage']='计算记录暂不可核对'
        if closeouts:
            for row in rows:
                try:
                    report=closeouts.get(row['id'])
                    if report:row['scoped_acceptance']=report['acceptance']
                except (ValueError,KeyError,TypeError,OSError,runtime_denied):
                    row['acceptance_unavailable']=True
        return {'tasks':rows}

    @app.get('/api/tasks/{identifier}/reference-progress')
    def reference_progress(identifier: str):
        # Human-only local ledger projection. Never passed to model/plan inputs.
        return papers.reference_progress(identifier)

    @app.post('/api/tasks/{identifier}/lifecycle')
    @serialized_task_action
    def task_lifecycle(identifier: str, data: TaskLifecycleInput):
        store.get(identifier)
        if workflow and (workflow.status(identifier).get('workflow') or {}).get('state') in {'queued','preparing'}:
            raise TaskError('任务仍在自动准备，不能删除或确认结束。')
        preparation=preparations.get(identifier)
        if preparation and preparation['state'] in {'queued','running','model_requested','checking_plan','repairing_plan','preparing_files','interrupted'}:
            raise TaskError('方案仍在准备或状态待核对，不能删除或确认结束。')
        if execution_jobs:
            job=execution_jobs.status(identifier).get('job')
            if job and job['state'] in {'queued','running','waiting','attention'}:
                raise TaskError('计算仍在处理或状态待核对，不能删除或确认结束。')
        terminal={'completed','failed','cancelled','timeout','rejected','cancelled_before_dispatch'}
        evaluations=[]
        for paper in papers.list()['papers']:
            evaluations.extend(e for e in paper['evaluations'] if e['task_id']==identifier)
        if results_reader:evaluations.extend(results_reader.task(identifier)['evaluations'])
        for evaluation in evaluations:
            if evaluation.get('available') is False:raise TaskError('计算记录待核对，暂不能修改结束状态。')
            if any(r['state'] not in terminal for r in evaluation['requests']):
                raise TaskError('仍有未结束或待核对的计算；本操作不会取消作业。')
        return store.manage_lifecycle(identifier,data.revision,data.lifecycle_revision,data.action)

    @app.get('/api/tasks/{identifier}/results')
    def task_results(identifier: str):
        store.get(identifier)
        if results_reader is None:
            return dict(configured=False,evaluations=[],scientific_status='not_evaluated',
                        message='结果服务尚未配置。任务和已有准备记录已保存。')
        try:return results_reader.task(identifier)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
            return JSONResponse({'detail':'结果记录暂不可读，请联系管理员核对。'},status_code=409)

    @app.get('/api/tasks/{identifier}/results/{analysis_id}/tables')
    def task_tables(identifier: str, analysis_id: str):
        store.get(identifier)
        if results_reader is None:return JSONResponse({'detail':'结果服务尚未配置。'},status_code=404)
        try:return results_reader.tables(identifier,analysis_id)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
            return JSONResponse({'detail':'原始数据或来源核验未通过，未展示数据与图表。'},status_code=409)

    @app.get('/api/tasks/{identifier}/results/{analysis_id}/download')
    def task_report(identifier: str, analysis_id: str):
        store.get(identifier)
        if results_reader is None:return JSONResponse({'detail':'结果服务尚未配置。'},status_code=404)
        try:report=results_reader.report(identifier,analysis_id)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
            return JSONResponse({'detail':'这项任务没有可核验的对应报告。'},status_code=409)
        return Response(canonical(report),media_type='application/json',
                        headers={'Content-Disposition':'attachment; filename="analysis-report.json"'})

    @app.get('/api/tasks/{identifier}/results/{analysis_id}/derived/{name}')
    def task_derived_csv(identifier: str, analysis_id: str, name: str):
        store.get(identifier)
        if results_reader is None:return JSONResponse({'detail':'结果服务尚未配置。'},status_code=404)
        try:data=results_reader.derived(identifier,analysis_id,name)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
            return JSONResponse({'detail':'完整数据或关联来源未通过核验，未提供下载。'},status_code=409)
        return Response(data,media_type='text/csv',
                        headers={'Content-Disposition':'attachment; filename="'+name+'"'})

    @app.post('/api/literature/preview')
    def preview(data: LiteraturePreview):
        return preview_csv(data.csv_text)

    @app.post('/api/tasks/{identifier}/literature')
    def import_literature(identifier: str, data: LiteratureImport):
        return store.import_literature(identifier, data.revision, **data.model_dump(exclude={'revision'}))

    @app.post('/api/tasks/{identifier}/reference-evidence')
    def reference_generate(identifier: str, data: ReferenceDraft):
        if reference_model_client is None:
            return JSONResponse({'detail': '文献自动整理尚未配置。已有资料与历史不会改变。'}, status_code=422)
        return generate_reference_draft(reference_model_client, store, identifier, data.revision, data.csv_texts)

    @app.get('/api/tasks/{identifier}/reference-evidence')
    def reference_history(identifier: str):
        requests = store.reference_requests(identifier)
        binding = accounting_binding(reference_model_client) if reference_model_client else None
        for item in requests:
            record_binding = item.pop('accounting_sha256')
            same_accounting = binding is not None and record_binding == binding
            record = reference_model_client.calls.lookup(item['request_id']) if same_accounting else None
            receipt = record['receipt'] if record else None
            state = 'saved' if item['imported'] else (receipt['state'] if receipt else 'unresolved')
            item['state'] = state
            item['label'] = {'saved':'证据草稿已保存', 'completed':'模型已返回，草稿尚未导入',
                             'unknown':'模型请求状态不明', 'not_sent':'模型请求未发出',
                             'rejected':'模型服务拒绝请求', 'response_invalid':'模型返回格式无效',
                             'unresolved':'尚无可核对的完成记录'}.get(state, '整理状态待核对')
        return {'requests': requests, 'configured': reference_model_client is not None,
                'execution_authorized': False}

    @app.post('/api/tasks/{identifier}/reference-evidence/{request_id}/recover')
    def reference_recover(identifier: str, request_id: str, data: Revision):
        if reference_model_client is None:
            return JSONResponse({'detail': '请先恢复原文献整理服务配置；不会发出模型请求。'}, status_code=422)
        return recover_reference_draft(reference_model_client, store, identifier, data.revision, request_id)

    @app.post('/api/tasks', status_code=201)
    def create(data: NewTask):
        return store.create(data.title, data.prompt, data.mode)

    @app.get('/api/tasks/{identifier}')
    def get(identifier: str):
        document = store.get(identifier)
        try:
            projection = paper_projection(identifier)
            if projection is not None:
                document['paper_evidence'] = projection
        except (ValueError, KeyError, TypeError, OSError, TaskError, runtime_denied):
            document['paper_evidence_error'] = '已登记文献证据暂未通过核验，请检查工作台来源；原任务保留。'
        return document

    @app.get('/api/tasks/{identifier}/guidance')
    def task_guidance(identifier: str):
        return {'task_id': identifier, 'guidance': store.guidance(identifier),
                'paused': store.paused(identifier)}

    @app.post('/api/tasks/{identifier}/guidance')
    @serialized_task_action
    def task_guidance_add(identifier: str, data: GuidanceInput):
        return {'task_id': identifier, 'guidance': store.add_guidance(identifier, data.revision, data.note),
                'paused': store.paused(identifier)}

    @app.post('/api/tasks/{identifier}/pause')
    @serialized_task_action
    def task_pause(identifier: str, data: PauseInput):
        return store.set_paused(identifier, data.revision, data.paused)

    @app.get('/api/tasks/{identifier}/ai-activity')
    def ai_activity(identifier: str):
        """面向用户的进度播报：每一步都是一句看得懂的中文，并标明"现在进行到哪"。

        内部事件名、字段名、账本术语都不直接暴露给用户；需要的技术细节
        （模型、用量、失败原因）放在 detail 里，作为补充而不是主体。
        """
        document = store.get(identifier)
        steps = []

        def add(at, title, detail='', kind='ai', state='ok'):
            if not at:
                return
            steps.append(dict(at=at, title=title, detail=detail, kind=kind,
                              state=state, done=state == 'ok'))

        # ① 需求 → 条件阶段（来自任务历史）
        field_labels = FIELDS if isinstance(FIELDS, dict) else {}
        phrasing = {
            'created': ('已收到你的需求，开始理解研究目标', 'user'),
            'guidance_added': ('记录了你的引导意见', 'user'),
            'conditions_generated': ('已从需求中整理出计算条件', 'ai'),
            'confirmed': ('你确认了当前条件', 'user'),
            'conditions_frozen': ('条件已冻结，可以开始准备计算方案', 'ai'),
            'plan_approved': ('你批准了计算方案，可以提交计算', 'user'),
        }
        for event in store.history(identifier):
            name = event['event']
            if name in phrasing:
                title, kind = phrasing[name]
                detail = ''
                if name == 'conditions_frozen':
                    detail = '条件版本 ' + str(event['revision'])
                add(event['at'], title, detail, kind)
            elif name.startswith('candidate_added:'):
                field = name.split(':', 1)[1]
                add(event['at'], '补充了条件：' + field_labels.get(field, field), '', 'ai')
            elif name.startswith('literature_imported:'):
                field = name.split(':', 1)[1]
                add(event['at'], '从文献中导入条件：' + field_labels.get(field, field), '', 'ai')

        # ② 应用内 AI 的模型调用（整理条件 / 生成方案）
        def add_model(request_id, purpose):
            if model_client is None or not request_id:
                return
            try:
                found = model_client.calls.lookup(request_id)
            except (ValueError, KeyError, ModelError):
                return
            receipt = (found or {}).get('receipt') or {}
            usage = receipt.get('usage') or {}
            tokens = usage.get('total_tokens')
            state = receipt.get('state') or 'unknown'
            detail = '模型 ' + str(receipt.get('requested_model') or '')
            if tokens:
                detail += ' · ' + str(tokens) + ' tokens'
            if state != 'completed':
                detail += ' · 未完成（' + str(receipt.get('error') or state) + '）'
            add(receipt.get('at'), '应用内 AI ' + purpose, detail,
                'ai', 'ok' if state == 'completed' else 'attention')

        condition_records, condition_summary = condition_progress(identifier)
        recorded_calls = set()
        for record in condition_records:
            for event in record['events']:
                detail = ''
                if event.get('call_id'):
                    recorded_calls.add(event['call_id'])
                    call = next((c for c in record['calls'] if c['call_id']==event['call_id']), {})
                    usage = call.get('usage') or {}
                    if usage.get('total_tokens'):
                        detail = '已记账 ' + str(usage['total_tokens']) + ' tokens'
                if event.get('error_code'):
                    detail = (condition_failure_details.get(event['error_code'], '条件整理未完成，已有记录保留。') if event['state']=='failed'
                              else '正在基于具体检查问题修正，已有费用与答复保留。')
                if record.get('reconstructed'):
                    detail += ' · 根据已有调用回执只读核对，未写入新条件。'
                add(event.get('at'), event['label'], detail, 'ai',
                    'attention' if event['state'] in ('failed', 'uncertain') else 'ok')
        for request_id in (document.get('generated_batches') or {}):
            if request_id not in recorded_calls:
                add_model(request_id, '整理了你的计算条件')

        # ③ 方案准备阶段
        job = preparations.get(identifier) if preparations else None
        candidate_steps = {
            'queued': '正在排队准备计算方案',
            'running': '正在核对准备条件',
            'model_requested': '正在让应用内 AI 生成计算方案',
            'checking_plan':'正在核对需求、计算步骤与结果', 'repairing_plan':'正在自动修正方案',
            'preparing_files': '正在生成待提交的脚本与结构文件',
            'clarification_answered': '已把你补充的信息交给应用内 AI',
            'config_rebased': '已按当前部署重新核对方案基线',
            'prepared': '方案已准备完成，等待你审核批准',
            'clarification': '需要你补充信息才能继续准备方案',
            'failed': '方案准备未通过，需要处理',
            'rejected': '方案被拒绝',
        }
        for event in (job or {}).get('events') or []:
            state = event.get('state')
            if state not in candidate_steps:
                continue
            payload = event.get('payload') or {}
            detail = str(payload.get('detail') or payload.get('message') or '')[:200]
            if payload.get('questions'):
                detail = ('需要确认：' + str(payload['questions'][0]))[:200]
            add(event.get('at'), candidate_steps[state], detail, 'ai',
                'attention' if state in ('failed', 'clarification', 'rejected') else 'ok')
        if job and (job.get('result') or {}).get('request_id'):
            add_model(job['result']['request_id'], '生成了计算方案')

        # ④ 计算阶段（提交 → 运行 → 回收 → 分析）
        execution = (execution_jobs.get(identifier) if execution_jobs is not None else None) or {}
        execution_steps = {
            'queued': '正在提交计算到 HPC',
            'running': '正在核验许可并推进计算',
            'awaiting_approval': '等待你批准方案后提交',
            'waiting': '计算已在 HPC 运行，正在跟进状态',
            'analyzed': '计算完成，结果与分析已就绪',
            'analysis_failed': '计算完成，但自动分析未完成',
            'diagnostics_saved': '计算未成功，已保存诊断信息',
            'attention': '计算需要你处理后继续',
            'rejected': '提交被拒绝',
        }
        for event in execution.get('events') or []:
            state = event.get('state')
            if state in execution_steps:
                add(event.get('at'), execution_steps[state], '', 'compute',
                    'attention' if state in ('attention', 'rejected', 'diagnostics_saved', 'analysis_failed') else 'ok')
        job_id = execution.get('job_id')
        if job_id:
            add(execution.get('updated_at'), '已提交到 HPC，作业号 ' + str(job_id),
                '你可以在结果页签查看实时状态', 'compute')

        steps.sort(key=lambda item: str(item['at']))
        latest = steps[-1] if steps else None
        return {'task_id': identifier, 'steps': steps,
                'condition_preparation': condition_summary,
                'now': (latest or {}).get('title') or '',
                'note': '这是应用内 AI 的实时进度。技术细节（模型、用量、具体失败原因）在每一步的补充说明里。'}

    @app.get('/api/tasks/{identifier}/history')
    def history(identifier: str):
        preparation = preparations.reconcile(identifier)
        return {'events': store.history(identifier), 'preparation_events': preparation['events'] if preparation else [], 'lifecycle_events':store.lifecycle(identifier)['lifecycle_events']}

    @app.get('/api/tasks/{identifier}/execution')
    def execution_status(identifier: str):
        store.get(identifier)
        if execution_jobs is None:
            return dict(configured=False,worker_alive=False,can_start=False,job=None,message='自动执行服务尚未接入，任务已保存。')
        result=execution_jobs.status(identifier)
        if workflow:result['automatic_workflow']=workflow.status(identifier)
        return result

    @app.post('/api/tasks/{identifier}/execution',status_code=202)
    @serialized_task_action
    def execution_start(identifier: str,data: Revision):
        require_open_task(identifier)
        if execution_jobs is None:
            return JSONResponse({'detail':'自动执行服务尚未接入。'},status_code=422)
        from .ledger import LedgerError
        try:return execution_jobs.enqueue(identifier,data.revision)
        except (ValueError,OSError,LedgerError,runtime_denied) as error:
            # 笼统的"未通过核验"无法定位；给出异常类型与简短原因（不含密钥或路径细节）。
            detail = '方案或计算部署未通过核验，未发起新的计算。原因：' + type(error).__name__
            extra = str(error)[:200]
            if extra and extra != type(error).__name__:
                detail += '：' + extra
            return JSONResponse({'detail':detail},status_code=409)

    @app.post('/api/tasks/{identifier}/execution/recheck',status_code=202)
    @serialized_task_action
    def execution_recheck(identifier: str,data: Revision):
        require_open_task(identifier)
        if execution_jobs is None:
            return JSONResponse({'detail':'自动执行服务尚未接入。'},status_code=422)
        return execution_jobs.recheck(identifier,data.revision)

    @app.post('/api/tasks/{identifier}/execution/retry',status_code=202)
    @serialized_task_action
    def execution_retry(identifier: str,data: Revision):
        require_open_task(identifier)
        if execution_jobs is None:
            return JSONResponse({'detail':'自动执行服务尚未接入。'},status_code=422)
        return execution_jobs.retry(identifier,data.revision)

    @app.post('/api/tasks/{identifier}/workflow',status_code=202)
    @serialized_task_action
    def workflow_start(identifier: str,data: Revision):
        require_open_task(identifier)
        if workflow is None:
            return JSONResponse({'detail':'自动计算服务尚未就绪。'},status_code=422)
        return workflow.enqueue(identifier,data.revision)

    @app.get('/api/tasks/{identifier}/candidate')
    def candidate_get(identifier: str):
        return {'candidate': preparations.reconcile(identifier), 'downloads_enabled': candidate_service is not None}

    @app.post('/api/tasks/{identifier}/candidate', status_code=202)
    @serialized_task_action
    def candidate_start(identifier: str, data: CandidateInput):
        require_open_task(identifier)
        if store.paused(identifier):
            raise TaskError('任务已暂停：请先继续，再启动准备。已提交的作业不受影响。')
        if candidate_service is None:
            return JSONResponse({'detail': '方案准备服务尚未配置。条件和历史已保存。'}, status_code=422)
        if execution_jobs is not None:execution_jobs.register_for_generation(identifier,data.revision)
        return {'candidate': candidate_service.enqueue(identifier, data.revision, data.answers)}

    @app.get('/api/tasks/{identifier}/plan')
    def plan_review(identifier: str):
        """第一道人工关卡：把已准备的方案（脚本/结构/分析）交给用户审阅。"""
        if candidate_service is None:
            return JSONResponse({'detail': '方案服务尚未配置。'}, status_code=422)
        review = candidate_service.plan_review(identifier)
        scope = None
        job = preparations.get(identifier) if preparations else None
        digest = (job or {}).get('result', {}).get('snapshot_sha256') if job else None
        if isinstance(digest, str) and digest:
            scope = 'plan:' + digest
        review['approved'] = bool(scope) and store.plan_approved(identifier, scope)
        review['approvals'] = store.approvals(identifier)
        from .plan_workspace import workspace
        review['workspace'] = workspace(candidate_service, identifier)
        return review

    @app.post('/api/tasks/{identifier}/plan/approve')
    @serialized_task_action
    def plan_approve(identifier: str, data: ApprovalInput):
        job = preparations.get(identifier) if preparations else None
        digest = (job or {}).get('result', {}).get('snapshot_sha256') if job else None
        if not isinstance(digest, str) or not digest or (job or {}).get('state') != 'prepared':
            raise TaskError('当前没有可批准的方案；请先在"计算方案"里准备方案。')
        approvals = store.approve_plan(identifier, data.revision, scope='plan:' + digest, note=data.note or '')
        return {'approved': True, 'scope': 'plan:' + digest, 'approvals': approvals}

    @app.post('/api/tasks/{identifier}/plan/revise', status_code=202)
    @serialized_task_action
    def plan_revise(identifier: str, data: ReviseInput):
        """用户对方案有意见：写入引导并立刻按意见重新组织一次方案。"""
        if candidate_service is None:
            return JSONResponse({'detail': '方案服务尚未配置。'}, status_code=422)
        candidate_service.ensure_proposal_round(identifier)
        store.add_guidance(identifier, data.revision, data.note)
        current = store.get(identifier)
        return {'candidate': candidate_service.enqueue(identifier, current['revision'],
                                                       answers='用户对当前方案的意见（必须据此修改方案）：' + data.note)}

    @app.get('/api/tasks/{identifier}/candidate/files/{name}')
    def candidate_file(identifier: str, name: str):
        if candidate_service is None:
            return JSONResponse({'detail': '方案资料服务尚未配置。'}, status_code=422)
        return Response(candidate_service.file(identifier, name), media_type='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.post('/api/tasks/{identifier}/complete-conditions')
    def complete_conditions(identifier: str, data: ConditionInput):
        if model_client is None:
            return JSONResponse({'detail': '尚未启用运行模型。任务已保存，可以稍后整理。'}, status_code=422)
        # The completion must know which potentials are actually installed, otherwise it
        # invents a format the cluster cannot run (for example EAM for a W task while the
        # only available W resource is MEAM) and the later preparation has to refuse it.
        if candidate_service is None:
            raise TaskError('势函数资源适配器尚未配置，未发送条件补全请求。')
        try:
            resources = [{'pin':model['pin'], 'elements': model['elements'], 'format': model['format'],
                          'units': model['units'], 'applicability': model.get('applicability')}
                         for model in candidate_service.adapter.compatible_models()]
        except (ValueError, KeyError, TypeError, OSError):
            raise TaskError('已有势函数目录核验失败，未发送条件补全请求。请核对资源连接。') from None
        if not resources:
            raise TaskError('现有登记势函数未找到可用资源，未发送条件补全请求。')
        request_id = sha256(canonical(dict(task_id=identifier, revision=data.revision,
                                           operation='complete-conditions-v2', refine=data.refine,
                                           attempt=getattr(data, 'attempt', 0))))[:32]
        guidance = [item['note'] for item in store.guidance(identifier)]
        return complete_condition_draft(model_client, store, identifier, data.revision, request_id, resources, guidance, refine=data.refine)

    @app.post('/api/tasks/{identifier}/conditions/{field}')
    def add(identifier: str, field: str, data: AddCondition):
        return store.add_candidate(identifier, data.revision, field, data.model_dump(exclude={'revision'}))

    @app.post('/api/tasks/{identifier}/conditions/{field}/select')
    def select(identifier: str, field: str, data: SelectCondition):
        return store.select(identifier, data.revision, field, data.candidate_id, data.reason)

    @app.post('/api/tasks/{identifier}/confirm')
    def confirm(identifier: str, data: ConfirmConditions):
        return store.confirm(identifier, data.revision, data.fields)

    @app.get('/api/geometry-catalog')
    def initial_geometries():
        # Metadata read only: no import, model request, staging or job action.
        return geometry_catalog_view()

    @app.post('/api/tasks/{identifier}/initial-geometry')
    @serialized_task_action
    def select_initial_geometry(identifier: str, data: InitialGeometrySelection):
        require_open_task(identifier)
        document = store.get(identifier)
        if document['revision'] != data.revision:
            raise StaleTask('任务已更新，请刷新后选择初始结构')
        if document['status'] == 'conditions_frozen':
            raise FrozenTask('已冻结的初始结构不可覆盖')
        view = geometry_catalog_view()
        if not view['configured']:
            raise TaskError(view['reason'])
        if view['catalog_sha256'] != data.catalog_sha256:
            raise StaleTask('初始结构目录已更新，请重新读取并核对选择')
        entry = next((item for item in view['entries'] if item['pin'] == data.pin), None)
        if entry is None:
            raise TaskError('所选初始结构不在当前受信目录内；未修改任务')
        return store.select_initial_geometry(identifier, data.revision,
            {'catalog_sha256': view['catalog_sha256'], 'entry': entry})

    @app.post('/api/tasks/{identifier}/initial-geometry/clear')
    @serialized_task_action
    def clear_initial_geometry(identifier: str, data: Revision):
        require_open_task(identifier)
        return store.clear_initial_geometry(identifier, data.revision)

    def require_reference_preregistration_open(identifier):
        if papers is None:
            return
        progress = papers.reference_progress(identifier)
        if not progress['available'] or any(e['evaluation'].get('reserved_attempts', 0) > 0
                or e['evaluation'].get('dispatch_claims', 0) > 0 for e in progress['entries']):
            raise TaskError('此任务已有作者计算预留或提交记录，或记录待核对；不能补作事前复现目标。')

    @app.post('/api/tasks/{identifier}/paper-evidence/import-targets')
    @serialized_task_action
    def import_workbench_targets(identifier: str, data: Revision):
        # Only the preconfigured, hash-checked adapter supplies content. Browser
        # input contains a revision, never an arbitrary inventory or local path.
        require_open_task(identifier)
        require_reference_preregistration_open(identifier)
        report = paper_projection(identifier)
        if report is None or report['target_inventory'] is None:
            raise TaskError('尚无已登记的文献工作台目标清单。')
        if not report['target_import_allowed']:
            raise TaskError('此任务已有计算预留或提交记录，或记录暂不可核对；P 仅作事后展示，不能补作事前目标。')
        return store.import_target_inventory(identifier, data.revision, report['target_inventory'])

    @app.post('/api/tasks/{identifier}/targets/preview')
    def preview_targets(identifier: str, data: TargetPreview):
        from .target_planning import selection_readiness
        from .tasks import StaleTask, TaskError
        document=store.get(identifier)
        if document['mode']!='reproduction': raise TaskError('普通研究不要求论文目标')
        if document['revision']!=data.revision: raise StaleTask('任务已更新，请刷新后选择')
        return selection_readiness(document,data.selected_ids,data.exclusion_reason)

    @app.post('/api/tasks/{identifier}/targets')
    def select_targets(identifier: str, data: TargetSelection):
        require_reference_preregistration_open(identifier)
        return store.select_targets(identifier, data.revision, data.selected_ids, data.exclusion_reason)

    @app.post('/api/tasks/{identifier}/freeze')
    def freeze(identifier: str, data: Revision):
        if store.get(identifier)['mode'] == 'reproduction':
            require_reference_preregistration_open(identifier)
        return store.freeze(identifier, data.revision)

    @app.post('/api/tasks/{identifier}/generate-conditions')
    def generate(identifier: str, data: ConditionInput):
        if model_client is None:
            return JSONResponse({'detail': '尚未启用运行模型。任务已保存，可以稍后整理。'}, status_code=422)
        doc = store.get(identifier)
        # The public research entry sends only the user's saved request.
        # Reference documents never enter through a browser-supplied path/URL.
        sources = [dict(id='user-request', origin='user', locator='用户原始任务描述', text=doc['prompt'])]
        request_id = sha256(canonical(dict(task_id=identifier, revision=data.revision, sources=sources,
                                           operation='generate-conditions-v2')))[:32]
        return generate_condition_draft(model_client, store, identifier, data.revision, sources, request_id,
                                        retry_of=data.retry_of)

    @app.get('/api/tasks/{identifier}/export')
    def export(identifier: str):
        return Response(store.export(identifier), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="confirmed-conditions.json"'})

    @app.get('/api/tasks/{identifier}/packages/{kind}')
    def export_package(identifier: str, kind: str):
        filenames = {'execution': 'execution-task-draft.json', 'reference': 'reference-preparation.json'}
        if kind not in filenames:
            return JSONResponse({'detail': '资料类型不存在'}, status_code=404)
        return Response(store.export_packages(identifier)[kind], media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="{filenames[kind]}"'})

    return app


def main():
    parser = argparse.ArgumentParser(description='Local research workspace with explicitly configured model and execution services.')
    parser.add_argument('--data-directory', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--ledger', help='Existing private ledger shared by execution and operator history')
    parser.add_argument('--model-connections-directory', type=Path, help='Explicit private credential directory shared by this project; no discovery or environment fallback')
    parser.add_argument('--model-ledger', help='Existing private DeepSeek policy and usage database; no automatic enablement')
    parser.add_argument('--reference-model-ledger', help='Explicit existing reference-side model policy; no automatic enablement')
    parser.add_argument('--execution-config',type=Path,help='Private deployment with fixed task/evaluation bindings and existing grants')
    parser.add_argument('--candidate-config', help='Private administrator resource configuration; no browser configuration')
    parser.add_argument('--collections-directory',help='Existing private output collection directory for read-only results')
    parser.add_argument('--reference-reports-directory',help='Private controller reference reports for the human operator only')
    parser.add_argument('--resource-discoveries', type=Path, help='Operator-only discovery handoff; no execution permission')
    parser.add_argument('--resource-discovery-reviews', type=Path, help='Controller conflict/missing-resource annotations')
    parser.add_argument('--reports-directory',help='Existing private analysis report directory for read-only results')
    parser.add_argument('--enable-result-assistant', action='store_true', help='Legacy option; saving a model connection enables explicit result questions')
    parser.add_argument('--session-activity-file', help='Desktop entry only: record page heartbeat/close activity in this file; otherwise no page activity is recorded')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Use an unprivileged TCP port')
    import uvicorn
    store = TaskStore(Path(args.data_directory)/'tasks.sqlite')
    ledger = None
    if args.ledger:
        from .ledger import Ledger
        if not Path(args.ledger).is_file(): parser.error('Ledger must already exist')
        ledger = Ledger(Path(args.ledger))
    model_client = DeepSeekClient(ModelCalls.open_existing(args.model_ledger)) if args.model_ledger else None
    connections=ModelConnections(store,assistant_enabled=True if args.enable_result_assistant else None,
                                 credentials_directory=args.model_connections_directory,
                                 calls=model_client.calls if model_client else None)
    if model_client is not None:
        # Wire the saved connection before any service is built from it: the candidate
        # service and the runtime route must hold the same client object, and the
        # environment key stays the fallback while no connection is configured.
        saved=connections.status().get('connections',{}).get('deepseek-official',{})
        if saved.get('configured'):
            model_client=connections.client('deepseek-official', calls=model_client.calls)
    reference_model_client = (DeepSeekClient(ModelCalls.open_existing(args.reference_model_ledger),
        key_reader=lambda: os.environ.get('DEEPSEEK_REFERENCE_API_KEY')) if args.reference_model_ledger else None)
    candidate_service = None
    results_reader=None
    if args.collections_directory and not ledger:
        parser.error('Raw downloads require an existing ledger')
    if args.reports_directory:
        if not (ledger and args.collections_directory):
            parser.error('Result viewing requires an existing ledger and both artifact directories')
        results_reader=ResultsReader(store,ledger,args.collections_directory,args.reports_directory)
    if args.candidate_config:
        if model_client is None:
            parser.error('Candidate preparation requires an existing model ledger')
        from .ledger import Resources
        from .potentials import PotentialAdapter, PotentialCatalog
        config_path = Path(args.candidate_config).expanduser()
        with root_descriptor(config_path.parent) as root:
            config = json.loads(read_file(root, config_path.name, 100000))
        if set(config) - {'legacy_snap_pins', 'output_layout', 'remote_potential_catalog'} != {'potential_catalog', 'allowed_pins', 'software_sha256', 'packages', 'resources', 'max_atoms'}:
            parser.error('Invalid candidate configuration fields')
        catalog = PotentialCatalog(config['potential_catalog'])
        if config.get('remote_potential_catalog'):
            from .remote_potentials import RemotePotentialCatalog, CombinedPotentialCatalog
            catalog = CombinedPotentialCatalog(catalog, RemotePotentialCatalog(config['remote_potential_catalog']))
        adapter = PotentialAdapter(catalog, allowed_pins=config['allowed_pins'],
                    software_sha256=config['software_sha256'], packages=config['packages'],
                    legacy_snap_pins=config.get('legacy_snap_pins', ()))
        candidate_service = CandidateService(store, model_client, adapter, resources=Resources(**config['resources']),
                    snapshots=store.path.parent / 'candidate-snapshots', max_atoms=config['max_atoms'], output_layout=config.get('output_layout','isolated'), review_plan=True)
    execution_jobs=None
    if args.execution_config:
        if ledger is None:parser.error('Execution requires an existing ledger')
        from .execution_jobs import load_execution_jobs
        execution_jobs=load_execution_jobs(store,ledger,args.execution_config)
        if candidate_service and candidate_service.snapshots!=execution_jobs.controller.snapshots:
            parser.error('Candidate and execution services must share snapshots')
    papers=PaperStore(store,ledger=ledger)
    reference_views=ReferenceViews(args.reference_reports_directory,papers) if args.reference_reports_directory else None
    from .discovery_library import DiscoveryLibrary
    uvicorn.run(create_app(store, port=args.port, papers=papers, model_client=model_client,
                          candidate_service=candidate_service,results_reader=results_reader,
                          reference_model_client=reference_model_client,reference_views=reference_views,
                          model_connections=connections,
                          result_assistant_enabled=True if args.enable_result_assistant else None,collections_directory=args.collections_directory,execution_jobs=execution_jobs,
                          discovery_library=DiscoveryLibrary(args.resource_discoveries,args.resource_discovery_reviews),
                          session_activity=SessionActivity(args.session_activity_file) if args.session_activity_file else None), host='127.0.0.1', port=args.port,
                proxy_headers=False, access_log=False, server_header=False)


if __name__ == '__main__':
    main()
