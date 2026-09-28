"""Operator model connections and result discussion, isolated from B generation.

Secrets never enter task history or response DTOs. Requests have durable intents;
an interrupted or failed request is never retried automatically.
"""
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone

from .manifest import canonical, private_directory, sha256
from .runtime_launcher import read_regular
from .tls_context import ssl_context
from .tasks import TaskError, task_id

PROVIDERS = {
    'deepseek-official': ('DeepSeek', 'api.deepseek.com', '/models', '/chat/completions'),
    'glm': ('GLM', 'open.bigmodel.cn', None, '/api/paas/v4/chat/completions'),
    'anthropic': ('Claude', 'api.anthropic.com', '/v1/models?limit=1000', '/v1/messages'),
    'openai': ('GPT', 'api.openai.com', '/v1/models', '/v1/chat/completions'),
}
MODEL_ID = re.compile(r'[A-Za-z0-9._:/-]{1,100}')


def official_request(provider, key, method, path, payload=None):
    """Fixed hosts and paths; no redirects, proxy discovery or retries."""
    headers = {'Content-Type': 'application/json'}
    if provider == 'anthropic':
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    else:
        headers['Authorization'] = 'Bearer ' + key
    try:
        context = ssl_context()
    except OSError:
        raise TaskError('本机缺少可用的证书包，无法建立 HTTPS 连接；请设置 SSL_CERT_FILE。') from None
    connection = http.client.HTTPSConnection(PROVIDERS[provider][1], timeout=60, context=context)
    try:
        connection.request(method, path, body=canonical(payload) if payload is not None else None, headers=headers)
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 2 * 1024 * 1024:
            raise TaskError('模型服务未完成请求，请检查密钥、模型 ID 和账户状态。不会自动重试。')
        return json.loads(raw)
    except TaskError:
        raise
    except Exception:
        raise TaskError('模型连接中断或返回格式异常，记录已保留，不会自动重试。') from None
    finally:
        connection.close()


class ModelConnections:
    def __init__(self, tasks, *, transport=official_request, assistant_enabled=False, credentials_directory=None):
        self.tasks, self.transport, self.assistant_enabled = tasks, transport, assistant_enabled
        self.directory = private_directory(Path(credentials_directory) if credentials_directory is not None else tasks.path.parent / 'model-connections')
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS connection_events (id INTEGER PRIMARY KEY, provider TEXT, event TEXT, at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS result_questions (id TEXT PRIMARY KEY, task_id TEXT, provider TEXT, model TEXT, question TEXT, context_sha256 TEXT, at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS result_answers (id TEXT PRIMARY KEY REFERENCES result_questions(id), state TEXT, answer TEXT, usage TEXT)')
            for table in ('connection_events', 'result_questions', 'result_answers'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable model history'); END")

    def _path(self, provider):
        if provider not in PROVIDERS:
            raise TaskError('请选择支持的模型商。')
        return self.directory / (provider + '.json')

    def _read(self, provider):
        path = self._path(provider)
        if not path.exists():
            return None
        return json.loads(read_regular(path, 16384, private=True))

    def status(self):
        connections = {}
        for provider, (label, host, models, _) in PROVIDERS.items():
            value = self._read(provider)
            connections[provider] = dict(label=label, endpoint='https://' + host,
                configured=bool(value), model=value['model'] if value else '',
                model_listing=bool(models))
        return {'connections': connections, 'result_assistant_enabled': self.assistant_enabled,
                'automatic_simulation_enabled': False}

    def save(self, provider, model, key=None, *, remove=False):
        path = self._path(provider)
        if model and not MODEL_ID.fullmatch(model) and not remove:
            raise TaskError('请填写提供方支持的具体模型 ID。')
        if remove:
            path.unlink(missing_ok=True)
        else:
            existing = self._read(provider)
            secret = key or (existing['key'] if existing else None)
            if not isinstance(secret, str) or not 8 <= len(secret) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
                raise TaskError('请填写有效的 API 密钥。密钥不会回传到页面。')
            fd, temporary = tempfile.mkstemp(dir=self.directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(canonical({'model': model, 'key': secret}))
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO connection_events(provider,event,at) VALUES (?,?,?)',
                (provider, 'removed' if remove else 'saved', datetime.now(timezone.utc).isoformat()))
        return self.status()

    def list_models(self, provider):
        value = self._read(provider)
        if not value:
            raise TaskError('请先保存该模型商的 API 密钥。')
        path = PROVIDERS[provider][2]
        if path is None:
            raise TaskError('此连接暂不提供模型目录，请按模型商文档填写模型 ID。')
        result = self.transport(provider, value['key'], 'GET', path)
        try:
            models = sorted({item['id'] for item in result['data'] if isinstance(item.get('id'), str) and MODEL_ID.fullmatch(item['id'])})
        except (TypeError, KeyError):
            raise TaskError('模型目录格式无法识别。') from None
        return {'provider': provider, 'models': models, 'has_more': bool(result.get('has_more', False)),
                'note': '提供方返回的模型目录；不代表每个模型都支持科研流程所需能力。'}

    def history(self, identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT q.*, a.state, a.answer, a.usage FROM result_questions q LEFT JOIN result_answers a ON q.id=a.id WHERE q.task_id=? ORDER BY q.at,q.id', (identifier,)).fetchall()
        return [dict(row) | {'state': row['state'] or 'unknown', 'usage': json.loads(row['usage']) if row['usage'] else None} for row in rows]

    def discuss(self, identifier, request_id, provider, question, context):
        task_id(request_id); self.tasks.get(identifier)
        if not self.assistant_enabled:
            raise TaskError('结果助手尚未启用；可先下载数据或保存模型连接。')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 4000:
            raise TaskError('请用 4000 字以内描述分析需求。')
        value = self._read(provider)
        if not value:
            raise TaskError('请先在模型设置中连接 API。')
        if not MODEL_ID.fullmatch(value['model']):
            raise TaskError('密钥已保存，请先读取模型目录并选定模型 ID。')
        context_hash = sha256(canonical(context))
        with self.tasks.transaction() as db:
            prior = db.execute('SELECT * FROM result_questions WHERE id=?', (request_id,)).fetchone()
            if prior:
                if prior['task_id'] != identifier or prior['question'] != question or prior['provider'] != provider:
                    raise TaskError('请求标识已用于其他内容。')
                # Never resend after a crash, timeout, or changed connection.
            else:
                db.execute('INSERT INTO result_questions VALUES (?,?,?,?,?,?,?)',
                    (request_id, identifier, provider, value['model'], question, context_hash, datetime.now(timezone.utc).isoformat()))
        if prior:
            return next(item for item in self.history(identifier) if item['id'] == request_id)
        system = ('You are the researcher-facing result assistant, NOT the evaluation generator. '
            'Reply in concise Chinese using ONLY the supplied verified result data and conversation. '
            'Do not invent numbers, claim new calculations or pretend to have executed tools. '
            'Identify result limitations. No code execution, HPC access or hidden retry is available. '
            'For new plots, explain which existing data support them; clearly distinguish suggestions from completed plots.')
        previous = [{'role': role, 'content': text} for item in self.history(identifier)[-9:]
                    if item['id'] != request_id and item['state'] == 'completed'
                    for role, text in [('user', item['question']), ('assistant', item['answer'])]]
        messages = previous + [{'role': 'user', 'content': canonical({'question': question, 'verified_results': context}).decode()}]
        payload = dict(model=value['model'], messages=messages, stream=False)
        if provider == 'anthropic':
            payload.update(system=system, max_tokens=4096)
        else:
            payload['messages'] = [{'role': 'system', 'content': system}] + messages
            payload['max_completion_tokens' if provider == 'openai' else 'max_tokens'] = 4096
            if provider in {'deepseek-official', 'glm'}: payload['thinking'] = {'type': 'disabled'}
        try:
            if len(canonical(payload)) > 262144: raise TaskError('结果上下文过大，尚未发送。请下载后分析。')
            response = self.transport(provider, value['key'], 'POST', PROVIDERS[provider][3], payload)
            answer = ('\n'.join(item['text'] for item in response['content'] if item.get('type') == 'text')
                      if provider == 'anthropic' else response['choices'][0]['message']['content'])
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 50000:
                raise ValueError('invalid answer')
            # Never reflect a credential if an upstream service unexpectedly echoes one.
            answer = answer.replace(value['key'], '[已隐藏密钥]')
            usage = {key: val for key, val in response.get('usage', {}).items()
                     if key in {'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens'} and type(val) is int and val >= 0}
            state = 'completed'
        except Exception:
            state, answer, usage = 'failed_or_unknown', '请求未完成，未自动重试。请核对模型连接与账户记录。', {}
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO result_answers VALUES (?,?,?,?)', (request_id, state, answer, json.dumps(usage)))
        return next(item for item in self.history(identifier) if item['id'] == request_id)
