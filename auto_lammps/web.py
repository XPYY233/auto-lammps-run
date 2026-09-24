"""Loopback-only task review application. No model, scheduler or shell endpoint."""
import argparse
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .tasks import FIELDS, FrozenTask, StaleTask, TaskError, TaskStore
from .literature import preview_csv

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


def create_app(store: TaskStore, *, port=8765):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalBoundary, authority=f'127.0.0.1:{port}')

    @app.exception_handler(TaskError)
    async def task_error(request: Request, exc: TaskError):
        return JSONResponse({'detail': str(exc)}, status_code=409 if isinstance(exc, (StaleTask, FrozenTask)) else 422)

    @app.exception_handler(KeyError)
    async def not_found(request: Request, exc: KeyError):
        return JSONResponse({'detail': '任务不存在'}, status_code=404)

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
        return {'fields': FIELDS, 'model_calls_enabled': False, 'execution_enabled': False}

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
        return {'events': store.history(identifier)}

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

    @app.get('/api/tasks/{identifier}/export')
    def export(identifier: str):
        return Response(store.export(identifier), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="confirmed-conditions.json"'})

    return app


def main():
    parser = argparse.ArgumentParser(description='Local task conditions; no simulation or model execution.')
    parser.add_argument('--data-directory', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Use an unprivileged TCP port')
    import uvicorn
    store = TaskStore(Path(args.data_directory)/'tasks.sqlite')
    uvicorn.run(create_app(store, port=args.port), host='127.0.0.1', port=args.port,
                proxy_headers=False, access_log=False, server_header=False)


if __name__ == '__main__':
    main()
