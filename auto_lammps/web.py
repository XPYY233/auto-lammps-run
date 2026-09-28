"""Loopback task application with optional, administrator-configured NLP.

The browser can save operator model connections, and versioned HPC settings. Existing job connections stay fixed.
"""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, SecretStr
from fastapi.exceptions import RequestValidationError

from .tasks import FIELDS, FrozenTask, StaleTask, TaskError, TaskStore
from .literature import preview_csv
from .papers import PaperStore
from .deepseek import DeepSeekClient, ModelCalls, ModelError
from .condition_generation import generate_condition_draft
from .reference_generation import accounting_binding, generate_reference_draft, recover_reference_draft
from .manifest import ManifestError, canonical, read_file, root_descriptor, sha256
from .candidate_jobs import CandidateHistory, CandidateService
from .agent_candidates import CandidateError
from .results import ResultsReader
from .operator_workspace import ModelPreferences, ReferenceViews
from .model_connections import ModelConnections
from .hpc_connections import HPCConnections
from .raw_outputs import RawOutputs
from .runtime_launcher import ExecutionDenied as runtime_denied

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
                     b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
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
               reference_model_client=None, reference_views=None, model_connections=None, result_assistant_enabled=False, hpc_connections=None, collections_directory=None, execution_jobs=None, discovery_library=None):
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
    papers = PaperStore(store) if papers is None else papers
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
    async def not_found(request: Request, exc: KeyError):
        return JSONResponse({'detail': '任务不存在'}, status_code=404)

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
        if name not in {'app.js', 'app.css'}:
            return JSONResponse({'detail': '文件不存在'}, status_code=404)
        return FileResponse(ASSETS/name)

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
        return hpc.status()

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
        return {'messages': connections.history(identifier), 'enabled': connections.assistant_enabled}

    @app.post('/api/tasks/{identifier}/discussion')
    def discuss_result(identifier: str, data: DiscussionInput):
        store.get(identifier)
        report = reference_views.get(identifier) if reference_views else None
        if report:
            context = {k: report[k] for k in ('scope','metrics','curves','limitations','scientific_status','report_sha256')}
        elif results_reader:
            context = results_reader.task(identifier)
            if not context.get('evaluations'): raise TaskError('尚无可分析的计算结果。')
        else:
            raise TaskError('尚无可分析的计算结果。')
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
        return {'fields': FIELDS, 'model_calls_enabled': bool(status and status['remaining_requests']),
                'model_status': status, 'execution_enabled': False,
                'automatic_workflow': workflow.availability() if workflow else {'configured':False,'enabled':False},
                'reference_generation': {'configured': reference_model_client is not None, 'model_status': reference_status},
                'candidate_preparation': candidate_service.availability() if candidate_service else
                    {'enabled': False, 'reason': '方案准备服务尚未配置。'}}

    @app.get('/api/papers')
    def paper_list():
        return papers.list()

    @app.get('/api/resource-discoveries')
    def resource_discoveries():
        try:
            return discoveries.get()
        except (ValueError, KeyError, TypeError, OSError, runtime_denied):
            return JSONResponse({'detail': '发现清单未通过格式检查，已登记资源仍保留。'}, status_code=409)

    @app.get('/api/papers/{identifier}')
    def paper_get(identifier: str):
        return papers.get(identifier)

    @app.post('/api/papers/{identifier}/select')
    def paper_select(identifier: str, data: Revision):
        return papers.select(identifier, data.revision)

    @app.post('/api/papers/{identifier}/tasks')
    def paper_task(identifier: str, data: LinkPaperTask):
        return papers.link_task(identifier, data.revision, data.task_id)

    @app.get('/api/tasks')
    def tasks():
        rows=store.list()
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
        return {'tasks':rows}

    @app.get('/api/tasks/{identifier}/results')
    def task_results(identifier: str):
        store.get(identifier)
        if results_reader is None:
            return dict(configured=False,evaluations=[],scientific_status='not_evaluated',
                        message='结果服务尚未配置。任务和已有准备记录已保存。')
        try:return results_reader.task(identifier)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
            return JSONResponse({'detail':'结果记录暂不可读，请联系管理员核对。'},status_code=409)

    @app.get('/api/tasks/{identifier}/results/{analysis_id}/download')
    def task_report(identifier: str, analysis_id: str):
        store.get(identifier)
        if results_reader is None:return JSONResponse({'detail':'结果服务尚未配置。'},status_code=404)
        try:report=results_reader.report(identifier,analysis_id)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
            return JSONResponse({'detail':'这项任务没有可核验的对应报告。'},status_code=409)
        return Response(canonical(report),media_type='application/json',
                        headers={'Content-Disposition':'attachment; filename="analysis-report.json"'})

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
        return store.get(identifier)

    @app.get('/api/tasks/{identifier}/history')
    def history(identifier: str):
        preparation = preparations.reconcile(identifier)
        return {'events': store.history(identifier), 'preparation_events': preparation['events'] if preparation else []}

    @app.get('/api/tasks/{identifier}/execution')
    def execution_status(identifier: str):
        store.get(identifier)
        if execution_jobs is None:
            return dict(configured=False,worker_alive=False,can_start=False,job=None,message='自动执行服务尚未接入，任务已保存。')
        result=execution_jobs.status(identifier)
        if workflow:result['automatic_workflow']=workflow.status(identifier)
        return result

    @app.post('/api/tasks/{identifier}/execution',status_code=202)
    def execution_start(identifier: str,data: Revision):
        if execution_jobs is None:
            return JSONResponse({'detail':'自动执行服务尚未接入。'},status_code=422)
        from .ledger import LedgerError
        try:return execution_jobs.enqueue(identifier,data.revision)
        except (ValueError,OSError,LedgerError,runtime_denied):
            return JSONResponse({'detail':'方案或计算部署未通过核验，未发起新的计算。'},status_code=409)

    @app.post('/api/tasks/{identifier}/workflow',status_code=202)
    def workflow_start(identifier: str,data: Revision):
        if workflow is None:
            return JSONResponse({'detail':'自动计算服务尚未就绪。'},status_code=422)
        return workflow.enqueue(identifier,data.revision)

    @app.get('/api/tasks/{identifier}/candidate')
    def candidate_get(identifier: str):
        return {'candidate': preparations.reconcile(identifier), 'downloads_enabled': candidate_service is not None}

    @app.post('/api/tasks/{identifier}/candidate', status_code=202)
    def candidate_start(identifier: str, data: Revision):
        if candidate_service is None:
            return JSONResponse({'detail': '方案准备服务尚未配置。条件和历史已保存。'}, status_code=422)
        if execution_jobs is not None:execution_jobs.register_for_generation(identifier,data.revision)
        return {'candidate': candidate_service.enqueue(identifier, data.revision)}

    @app.get('/api/tasks/{identifier}/candidate/files/{name}')
    def candidate_file(identifier: str, name: str):
        if candidate_service is None:
            return JSONResponse({'detail': '方案资料服务尚未配置。'}, status_code=422)
        return Response(candidate_service.file(identifier, name), media_type='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.post('/api/tasks/{identifier}/conditions/{field}')
    def add(identifier: str, field: str, data: AddCondition):
        return store.add_candidate(identifier, data.revision, field, data.model_dump(exclude={'revision'}))

    @app.post('/api/tasks/{identifier}/conditions/{field}/select')
    def select(identifier: str, field: str, data: SelectCondition):
        return store.select(identifier, data.revision, field, data.candidate_id, data.reason)

    @app.post('/api/tasks/{identifier}/confirm')
    def confirm(identifier: str, data: ConfirmConditions):
        return store.confirm(identifier, data.revision, data.fields)

    @app.post('/api/tasks/{identifier}/targets')
    def select_targets(identifier: str, data: TargetSelection):
        return store.select_targets(identifier, data.revision, data.selected_ids, data.exclusion_reason)

    @app.post('/api/tasks/{identifier}/freeze')
    def freeze(identifier: str, data: Revision):
        return store.freeze(identifier, data.revision)

    @app.post('/api/tasks/{identifier}/generate-conditions')
    def generate(identifier: str, data: Revision):
        if model_client is None:
            return JSONResponse({'detail': '尚未启用运行模型。任务已保存，可以稍后整理。'}, status_code=422)
        doc = store.get(identifier)
        # The public research entry sends only the user's saved request.
        # Reference documents never enter through a browser-supplied path/URL.
        sources = [dict(id='user-request', origin='user', locator='用户原始任务描述', text=doc['prompt'])]
        request_id = sha256(canonical(dict(task_id=identifier, revision=data.revision, sources=sources,
                                           operation='generate-conditions-v1')))[:32]
        return generate_condition_draft(model_client, store, identifier, data.revision, sources, request_id)

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
    parser.add_argument('--enable-result-assistant', action='store_true', help='Allow explicit user requests to the separately configured result discussion model')
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
        if set(config) - {'legacy_snap_pins', 'output_layout'} != {'potential_catalog', 'allowed_pins', 'software_sha256', 'packages', 'resources', 'max_atoms'}:
            parser.error('Invalid candidate configuration fields')
        adapter = PotentialAdapter(PotentialCatalog(config['potential_catalog']), allowed_pins=config['allowed_pins'],
                    software_sha256=config['software_sha256'], packages=config['packages'],
                    legacy_snap_pins=config.get('legacy_snap_pins', ()))
        candidate_service = CandidateService(store, model_client, adapter, resources=Resources(**config['resources']),
                    snapshots=store.path.parent / 'candidate-snapshots', max_atoms=config['max_atoms'], output_layout=config.get('output_layout','isolated'))
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
    connections=ModelConnections(store,assistant_enabled=args.enable_result_assistant,credentials_directory=args.model_connections_directory)
    if model_client is not None:
        # The runtime route prefers the connection saved in the product settings and
        # keeps using the same ledger; the environment key stays the fallback while
        # no connection is configured.
        saved=connections.status().get('connections',{}).get('deepseek-official',{})
        if saved.get('configured'):
            model_client=connections.client('deepseek-official', calls=model_client.calls)
    uvicorn.run(create_app(store, port=args.port, papers=papers, model_client=model_client,
                          candidate_service=candidate_service,results_reader=results_reader,
                          reference_model_client=reference_model_client,reference_views=reference_views,
                          model_connections=connections,
                          result_assistant_enabled=args.enable_result_assistant,collections_directory=args.collections_directory,execution_jobs=execution_jobs,
                          discovery_library=DiscoveryLibrary(args.resource_discoveries,args.resource_discovery_reviews)), host='127.0.0.1', port=args.port,
                proxy_headers=False, access_log=False, server_header=False)


if __name__ == '__main__':
    main()
