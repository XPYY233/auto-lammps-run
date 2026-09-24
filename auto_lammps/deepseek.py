"""Server-side, non-streaming DeepSeek JSON connector with durable call limits.

No browser endpoint configures this client. Administrators must separately approve
the policy, data scope and model; the default request allowance is zero.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .manifest import canonical, private_directory, sha256

APP_ID = 0x414C4D43
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ModelError(ValueError):
    """Safe classification only; never include upstream bodies or credentials."""


def strict_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite value')
    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ModelError('invalid_json') from None


@dataclass(frozen=True)
class DeepSeekConfig:
    model: str
    max_output_tokens: int = 4096
    max_input_bytes: int = 65536
    timeout_seconds: int = 45

    def __post_init__(self):
        if not isinstance(self.model, str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,100}', self.model):
            raise ModelError('invalid_model')
        for value, limit in ((self.max_output_tokens, 32768), (self.max_input_bytes, 262144), (self.timeout_seconds, 120)):
            if type(value) is not int or not 1 <= value <= limit:
                raise ModelError('invalid_model_limits')


def request_body(config, messages):
    if not isinstance(messages, list) or not messages or len(messages) > 32:
        raise ModelError('invalid_messages')
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                or message['role'] not in ('system', 'user', 'assistant')
                or not isinstance(message['content'], str) or not message['content'].strip()):
            raise ModelError('invalid_messages')
    if not any('json' in item['content'].lower() for item in messages):
        raise ModelError('json_instruction_required')
    body = canonical(dict(model=config.model, messages=messages, stream=False,
                          thinking={'type': 'disabled'}, max_tokens=config.max_output_tokens,
                          response_format={'type': 'json_object'}))
    if len(body) > config.max_input_bytes:
        raise ModelError('input_too_large')
    return body


def https_transport(body, key, timeout):
    """Fixed official host; no redirects, proxy discovery or automatic retries."""
    connection = http.client.HTTPSConnection('api.deepseek.com', timeout=timeout)
    try:
        connection.request('POST', '/chat/completions', body=body,
                           headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
        response = connection.getresponse()
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ModelError('response_too_large')
        return response.status, content
    finally:
        connection.close()


class ModelCalls:
    """One immutable policy per private campaign database; every intent is spent.

    A crash, missing receipt or rejected request never releases a request slot.
    Reusing an ID never sends again, including after backend restart.
    """
    def __init__(self, path, config, *, max_requests=0):
        if type(max_requests) is not int or not 0 <= max_requests <= 100000:
            raise ModelError('invalid_request_limit')
        path = Path(path)
        self.path = private_directory(path.parent)/path.name
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ModelError('private_model_database_required')
        self.config = config
        policy = canonical(dict(provider='deepseek', protocol='chat-completions-json-v1',
                                config=asdict(config), max_requests=max_requests)).decode()
        with self.transaction() as db:
            app_id = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if app_id not in (0, APP_ID) or (app_id == 0 and tables) or version not in (0, 1):
                raise ModelError('unrecognized_model_database')
            db.execute('CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY REFERENCES calls(id), document TEXT NOT NULL)')
            for table in ('policy', 'calls', 'receipts'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable model accounting'); END")
            row = db.execute('SELECT document FROM policy WHERE id=1').fetchone()
            if row is None:
                db.execute('INSERT INTO policy VALUES (1, ?)', (policy,))
            elif row[0] != policy:
                raise ModelError('model_policy_mismatch')
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, identifier, body):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ModelError('invalid_request_id')
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM calls WHERE id=?', (identifier,)).fetchone():
                raise ModelError('request_already_reserved')
            limit = json.loads(db.execute('SELECT document FROM policy WHERE id=1').fetchone()[0])['max_requests']
            if db.execute('SELECT count(*) FROM calls').fetchone()[0] >= limit:
                raise ModelError('model_budget_exhausted')
            db.execute('INSERT INTO calls VALUES (?,?,?)', (identifier, sha256(body), datetime.now(timezone.utc).isoformat()))

    @classmethod
    def open_existing(cls, path):
        path = Path(path).expanduser()
        if not path.is_file() or path.is_symlink():
            raise ModelError('existing_model_database_required')
        info = path.stat()
        if info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ModelError('private_model_database_required')
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
                raise ModelError('unrecognized_model_database')
            policy = strict_json(db.execute('SELECT document FROM policy WHERE id=1').fetchone()[0])
        except (sqlite3.Error, TypeError):
            raise ModelError('unrecognized_model_database') from None
        finally:
            db.close()
        try:
            return cls(path, DeepSeekConfig(**policy['config']), max_requests=policy['max_requests'])
        except (KeyError, TypeError):
            raise ModelError('unrecognized_model_database') from None

    def status(self):
        with self.transaction() as db:
            policy = json.loads(db.execute('SELECT document FROM policy WHERE id=1').fetchone()[0])
            used = db.execute('SELECT count(*) FROM calls').fetchone()[0]
            return dict(provider='deepseek', model=self.config.model, used_requests=used,
                        max_requests=policy['max_requests'], remaining_requests=max(0, policy['max_requests']-used))

    def record(self, identifier, receipt):
        with self.transaction() as db:
            db.execute('INSERT INTO receipts VALUES (?,?)', (identifier, canonical(receipt).decode()))

    def history(self):
        with self.transaction() as db:
            return [dict(request_id=row[0], request_sha256=row[1], at=row[2],
                         receipt=json.loads(row[3]) if row[3] else None)
                    for row in db.execute('SELECT c.id,c.request_sha256,c.at,r.document FROM calls c '
                                          'LEFT JOIN receipts r ON c.id=r.id ORDER BY c.at,c.id')]


def parse_completion(content):
    data = strict_json(content)
    if not isinstance(data, dict):
        raise ModelError('invalid_response')
    choices = data.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ModelError('invalid_response')
    choice = choices[0]
    if choice.get('finish_reason') != 'stop':
        raise ModelError('incomplete_generation')
    message = choice.get('message')
    if (not isinstance(message, dict) or message.get('role') != 'assistant' or message.get('tool_calls')
            or not isinstance(message.get('content'), str) or not message['content'].strip()):
        raise ModelError('empty_or_unexpected_output')
    value = strict_json(message['content'])
    if not isinstance(value, dict):
        raise ModelError('json_object_required')
    return value


def usage_from_response(content):
    try:
        envelope = strict_json(content)
        usage = envelope.get('usage') if isinstance(envelope, dict) else None
        if not isinstance(usage, dict):
            return None
        keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys):
            return None
        if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
            return None
        return {k: usage[k] for k in keys}
    except ModelError:
        return None


class DeepSeekClient:
    def __init__(self, calls, *, transport=https_transport, key_reader=None):
        self.calls, self.transport = calls, transport
        self.key_reader = key_reader or (lambda: os.environ.get('DEEPSEEK_API_KEY'))

    def complete_json(self, identifier, messages):
        body = request_body(self.calls.config, messages)
        self.calls.reserve(identifier, body)
        receipt = dict(provider='deepseek', requested_model=self.calls.config.model,
                       request_sha256=sha256(body), state='not_sent', usage=None,
                       http_status=None, response_sha256=None, output_sha256=None)
        try:
            key = self.key_reader()
            if (not isinstance(key, str) or not key or len(key) > 4096
                    or any(ord(char) < 33 or ord(char) > 126 for char in key)):
                raise ModelError('model_key_missing_or_invalid')
            receipt['state'] = 'unknown'
            status, content = self.transport(body, key, self.calls.config.timeout_seconds)
            if type(status) is not int or not isinstance(content, bytes) or len(content) > MAX_RESPONSE_BYTES:
                raise ModelError('invalid_transport_response')
            receipt.update(http_status=status, response_sha256=sha256(content))
            if status != 200:
                receipt['state'] = 'rejected'
                raise ModelError('provider_request_failed')
            receipt.update(state='response_invalid', usage=usage_from_response(content))
            value = parse_completion(content)
            receipt.update(state='completed', output_sha256=sha256(canonical(value)))
        except Exception as exc:
            # Only known local classifications may reach logs. An upstream
            # exception/body can echo headers, prompts or keys.
            safe = str(exc) if type(exc) is ModelError and str(exc) in {
                'model_key_missing_or_invalid', 'invalid_transport_response', 'provider_request_failed',
                'invalid_json', 'invalid_response', 'incomplete_generation', 'empty_or_unexpected_output',
                'json_object_required', 'response_too_large'} else 'model_transport_unknown'
            receipt['error'] = safe
            self.calls.record(identifier, receipt)
            raise ModelError(safe) from None
        # Keep successful structured output privately even if subsequent
        # scientific validation or an optimistic task import fails.
        self.calls.record(identifier, {**receipt, 'structured_output': value})
        return {'value': value, 'request_id': identifier, 'receipt': receipt}
