"""Loopback task application with optional, administrator-configured NLP.

The browser cannot configure model budgets, credentials or scheduler access.
"""
import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .tasks import FIELDS, FrozenTask, StaleTask, TaskError, TaskStore
from .literature import preview_csv
from .papers import PaperStore
from .deepseek import DeepSeekClient, ModelCalls, ModelError
from .condition_generation import generate_condition_draft
from .manifest import ManifestError, canonical, read_file, root_descriptor, sha256
from .candidate_jobs import CandidateHistory, CandidateService
from .agent_candidates import CandidateError

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


def create_app(store: TaskStore, *, port=8765, papers=None, model_client=None, candidate_service=None):
    papers = PaperStore(store) if papers is None else papers
    preparations = CandidateHistory(store)
    if candidate_service and (candidate_service.tasks.path != store.path or candidate_service.client is not model_client):
        raise ValueError('Candidate service must share the task store and model policy')
    @asynccontextmanager
    async def lifespan(app):
        if candidate_service:
            candidate_service.start()
        try:
            yield
        finally:
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

    @app.get('/api/schema')
    def schema():
        status = model_client.calls.status() if model_client else None
        return {'fields': FIELDS, 'model_calls_enabled': bool(status and status['remaining_requests']),
                'model_status': status, 'execution_enabled': False,
                'candidate_preparation': candidate_service.availability() if candidate_service else
                    {'enabled': False, 'reason': '方案准备服务尚未配置。'}}

    @app.get('/api/papers')
    def paper_list():
        return papers.list()

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
        return {'tasks': store.list()}

    @app.post('/api/literature/preview')
    def preview(data: LiteraturePreview):
        return preview_csv(data.csv_text)

    @app.post('/api/tasks/{identifier}/literature')
    def import_literature(identifier: str, data: LiteratureImport):
        return store.import_literature(identifier, data.revision, **data.model_dump(exclude={'revision'}))

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

    @app.get('/api/tasks/{identifier}/candidate')
    def candidate_get(identifier: str):
        return {'candidate': preparations.reconcile(identifier), 'downloads_enabled': candidate_service is not None}

    @app.post('/api/tasks/{identifier}/candidate', status_code=202)
    def candidate_start(identifier: str, data: Revision):
        if candidate_service is None:
            return JSONResponse({'detail': '方案准备服务尚未配置。条件和历史已保存。'}, status_code=422)
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
    parser = argparse.ArgumentParser(description='Local task conditions; no simulation or model execution.')
    parser.add_argument('--data-directory', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--ledger', help='Existing private ledger for operator history; no submission endpoint')
    parser.add_argument('--model-ledger', help='Existing private DeepSeek policy and usage database; no automatic enablement')
    parser.add_argument('--candidate-config', help='Private administrator resource configuration; no browser configuration')
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
    candidate_service = None
    if args.candidate_config:
        if model_client is None:
            parser.error('Candidate preparation requires an existing model ledger')
        from .ledger import Resources
        from .potentials import PotentialAdapter, PotentialCatalog
        config_path = Path(args.candidate_config).expanduser()
        with root_descriptor(config_path.parent) as root:
            config = json.loads(read_file(root, config_path.name, 100000))
        if set(config) - {'legacy_snap_pins'} != {'potential_catalog', 'allowed_pins', 'software_sha256', 'packages', 'resources', 'max_atoms'}:
            parser.error('Invalid candidate configuration fields')
        adapter = PotentialAdapter(PotentialCatalog(config['potential_catalog']), allowed_pins=config['allowed_pins'],
                    software_sha256=config['software_sha256'], packages=config['packages'],
                    legacy_snap_pins=config.get('legacy_snap_pins', ()))
        candidate_service = CandidateService(store, model_client, adapter, resources=Resources(**config['resources']),
                    snapshots=store.path.parent / 'candidate-snapshots', max_atoms=config['max_atoms'])
    uvicorn.run(create_app(store, port=args.port, papers=PaperStore(store, ledger=ledger), model_client=model_client,
                          candidate_service=candidate_service), host='127.0.0.1', port=args.port,
                proxy_headers=False, access_log=False, server_header=False)


if __name__ == '__main__':
    main()
