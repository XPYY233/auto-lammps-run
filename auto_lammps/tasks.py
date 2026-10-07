"""Persistent condition review, independent of execution and hidden scoring data."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .manifest import canonical, private_directory, sha256

FIELDS = {
    'scope': '验收范围', 'reference': '论文标识', 'material': '材料与成分',
    'structure': '初始结构', 'size': '尺寸与晶向', 'boundary': '边界条件',
    'defects_loading': '缺陷与加载', 'potential': '势函数与版本', 'units': '单位制',
    'temperature': '温度', 'pressure': '压力', 'ensemble': '系综',
    'timestep': '时间步长', 'stages': '平衡与生产阶段',
    'initialization': '初始化与随机种子', 'sampling': '采样规则',
    'outputs': '输出文件与内容', 'quantity': '目标物理量', 'analysis': '分析方法',
    'resources': '计算资源上限',
}
ESSENTIAL = {'scope', 'material', 'structure', 'potential', 'units', 'quantity', 'analysis', 'resources'}
GEOMETRY_CONDITION_FIELDS = ('structure', 'size', 'boundary', 'material', 'units', 'initialization')
ORIGINS = {'user', 'paper', 'code', 'proposed'}
APP_ID = 0x414C5453

# Only fixed public-facing classifications are kept in condition progress.
# Raw model answers, exception text and source excerpts stay in their own ledgers.
CONDITION_EVENTS = {
    'prepared': ('prepared', '已保存条件整理请求'),
    'call_started': ('generating', 'AI 正在整理原始需求'),
    'model_completed': ('validating', 'AI 已返回，正在核对原文依据'),
    'validation_failed': ('repairing', '原文依据核对未通过，正在有限修正'),
    'repair_started': ('repairing', 'AI 正在修正原文依据'),
    'imported': ('imported', '条件已整理，待用户确认'),
    'failed': ('failed', '条件整理未完成，已有调用和失败记录已保留'),
}
CONDITION_ERROR_CODES = {
    '', 'source_quote_mismatch', 'value_quote_mismatch', 'unit_quote_mismatch',
    'source_identity_invalid', 'output_contract_invalid', 'task_changed',
    'model_not_completed', 'model_request_failed', 'model_state_unknown',
    'model_budget_exhausted', 'model_input_too_large', 'import_rejected',
}


class TaskError(ValueError):
    pass


class StaleTask(TaskError):
    pass


class FrozenTask(TaskError):
    pass


def text(value, limit, *, required=True):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value or (required and not value.strip()):
        raise TaskError('字段为空、过长或包含无效字符')
    return value.strip()


def task_id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise TaskError('任务标识无效')
    return value


def candidate(data):
    if not isinstance(data, dict) or set(data) != {'value', 'unit', 'origin', 'source_locator', 'applicability', 'evidence_role'}:
        raise TaskError('条件字段不完整')
    if data['origin'] not in ORIGINS or data['applicability'] not in {'required', 'not_applicable'}:
        raise TaskError('条件来源或适用性无效')
    if data['evidence_role'] != 'input':
        raise TaskError('论文结果和参考答案不能作为任务输入条件')
    if data['origin'] in {'paper', 'code'} and not text(data['source_locator'], 1000, required=False):
        raise TaskError('论文或代码条件必须注明来源位置')
    clean = {**data, 'value': text(data['value'], 4000), 'unit': text(data['unit'], 80, required=False),
             'source_locator': text(data['source_locator'], 1000, required=data['origin'] in {'paper', 'code'})}
    # The value for not_applicable is the explicit reason, never an implicit default.
    return clean


def explicit_conditions(prompt):
    """Read only exact, user-labelled lines. Do not infer prose or physics.

    An ordinary sentence remains original text. Repeated labels are independent
    pieces of evidence and may conflict; none is silently selected or confirmed.
    """
    labels = {label: key for key, label in FIELDS.items()}
    for number, line in enumerate(prompt.splitlines(), 1):
        match = re.fullmatch(r'\s*([^：:]+)[：:]\s*(.+?)\s*', line)
        if match and match[1].strip() in labels:
            yield labels[match[1].strip()], candidate(dict(
                value=match[2], unit='', origin='user', source_locator=f'原始任务描述第 {number} 行',
                applicability='required', evidence_role='input'))


def append_condition(document, field, choice):
    condition = document['fields'][field]
    if len(condition['candidates']) >= 16:
        raise TaskError('此字段已达候选上限，请核对已有证据')
    if choice in [{k:v for k,v in item.items() if k != 'id'} for item in condition['candidates']]:
        raise TaskError('相同证据已记录')
    choice = {**choice, 'id': uuid.uuid4().hex}
    condition['candidates'].append(choice)
    values = {(item['value'], item['unit'], item['applicability']) for item in condition['candidates']}
    condition.update(selected=choice['id'] if len(values) == 1 else None, confirmed=False, resolution='')


def field_state(field):
    choices = field['candidates']
    selected = next((item for item in choices if item['id'] == field['selected']), None)
    if not choices:
        return 'missing'
    if selected is None:
        return 'conflict' if len(choices) > 1 else 'unselected'
    return 'confirmed' if field['confirmed'] else 'pending'


def issues(document):
    return [{'field': key, 'label': label, 'status': field_state(document['fields'][key])}
            for key, label in FIELDS.items() if not (key == 'reference' and document['mode'] == 'research')
            and field_state(document['fields'][key]) != 'confirmed']


class TaskStore:
    def __init__(self, path):
        path = Path(path).expanduser()
        parent = private_directory(path.parent)
        self.path = parent/path.name
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            os.close(fd)
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise TaskError('任务数据库必须是独立的私人文件')
        with self.transaction() as db:
            app = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app not in {0, APP_ID} or (app == 0 and tables):
                raise TaskError('拒绝修改其他应用的数据库')
            if db.execute('PRAGMA user_version').fetchone()[0] not in {0, 1}:
                raise TaskError('不支持此任务数据库版本')
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS revisions (task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL, '
                       'event TEXT NOT NULL, at TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(task_id, revision))')
            db.execute('CREATE TABLE IF NOT EXISTS frozen (task_id TEXT PRIMARY KEY REFERENCES tasks(id), '
                       'sha256 TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS reference_intents (id TEXT PRIMARY KEY, '
                       'task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL, '
                       'operation_sha256 TEXT NOT NULL, at TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS task_lifecycle (task_id TEXT NOT NULL REFERENCES tasks(id), '
                       'sequence INTEGER NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(task_id,sequence))')
            # 用户中途引导与暂停：都是追加历史，不覆盖任何既有记录。
            db.execute('CREATE TABLE IF NOT EXISTS task_guidance (task_id TEXT NOT NULL REFERENCES tasks(id), '
                       'sequence INTEGER NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(task_id,sequence))')
            db.execute('CREATE TABLE IF NOT EXISTS task_control (task_id TEXT NOT NULL REFERENCES tasks(id), '
                       'sequence INTEGER NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(task_id,sequence))')
            # 用户对"方案"的批准：绑定方案摘要，方案一变就需要重新批准。
            db.execute('CREATE TABLE IF NOT EXISTS task_approvals (task_id TEXT NOT NULL REFERENCES tasks(id), '
                       'sequence INTEGER NOT NULL, scope TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL, '
                       'PRIMARY KEY(task_id,sequence))')
            db.execute('CREATE TABLE IF NOT EXISTS condition_requests (id TEXT PRIMARY KEY, '
                       'task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL, '
                       'source_sha256 TEXT NOT NULL, messages_sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS condition_request_events (request_id TEXT NOT NULL REFERENCES condition_requests(id), '
                       'sequence INTEGER NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, document TEXT NOT NULL, '
                       'PRIMARY KEY(request_id,sequence))')
            for table in ('revisions', 'frozen', 'reference_intents', 'task_lifecycle', 'task_guidance', 'task_control', 'task_approvals',
                          'condition_requests', 'condition_request_events'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable task evidence'); END")
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
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

    def _read(self, db, identifier):
        row = db.execute('SELECT r.document FROM revisions r JOIN tasks t ON t.id=r.task_id AND t.revision=r.revision '
                         'WHERE t.id=?', (task_id(identifier),)).fetchone()
        if row is None:
            raise KeyError('任务不存在')
        return json.loads(row['document'])

    def get(self, identifier):
        with self.transaction() as db:
            document = self._read(db, identifier)
        return {**document, 'issues': issues(document)}

    def begin_condition_request(self, identifier, revision, request_id, source_sha256, messages_sha256):
        """Reserve a durable preparation identity before any model I/O.

        This does not advance the scientific condition revision or reset model
        accounting. A concurrent or abandoned request must be reconciled first.
        """
        task_id(request_id)
        for digest in (source_sha256, messages_sha256):
            if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                raise TaskError('条件整理意图摘要无效')
        with self.transaction() as db:
            self._editable(db, identifier, revision)
            old = db.execute('SELECT * FROM condition_requests WHERE id=?', (request_id,)).fetchone()
            if old:
                if (old['task_id'], old['revision'], old['source_sha256'], old['messages_sha256']) != (
                        identifier, revision, source_sha256, messages_sha256):
                    raise TaskError('条件整理请求身份不能替换')
                return False
            rows = db.execute('SELECT id FROM condition_requests WHERE task_id=?', (identifier,)).fetchall()
            for row in rows:
                last = db.execute('SELECT kind FROM condition_request_events WHERE request_id=? '
                                  'ORDER BY sequence DESC LIMIT 1', (row['id'],)).fetchone()
                if last is None or last['kind'] not in {'imported', 'failed'}:
                    raise TaskError('已有条件整理请求尚未核对，未发送新的模型调用')
            if len(rows) >= 32:
                raise TaskError('此任务条件整理请求已达上限，请核对已有记录')
            db.execute('INSERT INTO condition_requests VALUES (?,?,?,?,?,?)',
                       (request_id, identifier, revision, source_sha256, messages_sha256,
                        datetime.now(timezone.utc).isoformat()))
            self._condition_event(db, request_id, 'prepared')
        return True

    def _condition_event(self, db, request_id, kind, *, call_id=None, error_code='', receipt=None, reserved=None):
        if kind not in CONDITION_EVENTS or error_code not in CONDITION_ERROR_CODES:
            raise TaskError('条件整理进度分类无效')
        if call_id is not None:
            task_id(call_id)
            repair_id = sha256(canonical({'base': request_id, 'repair': 1}))[:32]
            if call_id not in {request_id, repair_id}:
                raise TaskError('模型调用不属于此条件整理请求')
        if db.execute('SELECT 1 FROM condition_requests WHERE id=?', (request_id,)).fetchone() is None:
            raise TaskError('缺少条件整理意图')
        last = db.execute('SELECT sequence,kind,document FROM condition_request_events WHERE request_id=? '
                          'ORDER BY sequence DESC LIMIT 1', (request_id,)).fetchone()
        document = dict(call_id=call_id, error_code=error_code)
        if reserved is not None:
            if type(reserved) is not bool:
                raise TaskError('模型请求预留状态无效')
            document['reserved'] = reserved
        if receipt is not None:
            if not isinstance(receipt, dict) or receipt.get('state') not in {
                    'not_sent', 'unknown', 'rejected', 'response_invalid', 'completed'}:
                raise TaskError('模型进度回执状态无效')
            document['model_state'] = receipt['state']
            document['reserved'] = True
            usage = receipt.get('usage')
            if isinstance(usage, dict):
                document['usage'] = {k: v for k, v in usage.items()
                                     if k in {'prompt_tokens', 'completion_tokens', 'total_tokens'}
                                     and type(v) is int and v >= 0}
        encoded = canonical(document).decode()
        if last and last['kind'] == kind and last['document'] == encoded:
            return
        if last and last['kind'] in {'imported', 'failed'}:
            raise TaskError('条件整理终态不可覆盖')
        if last and last['sequence'] >= 24:
            raise TaskError('条件整理进度记录已达上限')
        db.execute('INSERT INTO condition_request_events VALUES (?,?,?,?,?)',
                   (request_id, (last['sequence'] + 1) if last else 1, kind,
                    datetime.now(timezone.utc).isoformat(), encoded))

    def record_condition_request_event(self, identifier, request_id, kind, **data):
        with self.transaction() as db:
            self._read(db, identifier)
            row = db.execute('SELECT task_id FROM condition_requests WHERE id=?', (request_id,)).fetchone()
            if row is None or row['task_id'] != identifier:
                raise TaskError('条件整理请求不属于此任务')
            self._condition_event(db, request_id, kind, **data)

    def condition_requests(self, identifier):
        """Safe progress DTO: no source excerpts, private paths or raw answers."""
        with self.transaction() as db:
            self._read(db, identifier)
            rows = db.execute('SELECT * FROM condition_requests WHERE task_id=? ORDER BY at,id', (identifier,)).fetchall()
            result = []
            for row in rows:
                events = []
                calls = {}
                for event in db.execute('SELECT * FROM condition_request_events WHERE request_id=? ORDER BY sequence',
                                        (row['id'],)):
                    data = json.loads(event['document'])
                    state, label = CONDITION_EVENTS[event['kind']]
                    events.append(dict(sequence=event['sequence'], kind=event['kind'], at=event['at'],
                                       state=state, label=label, **data))
                    if data.get('call_id'):
                        previous = calls.get(data['call_id'], {})
                        reserved = data.get('reserved', previous.get('reserved'))
                        calls[data['call_id']] = dict(call_id=data['call_id'],
                                                     state='not_reserved' if reserved is False else data.get('model_state', previous.get('state', 'pending')),
                                                     reserved=reserved,
                                                     usage=data.get('usage', previous.get('usage')))
                latest = events[-1]
                result.append(dict(request_id=row['id'], revision=row['revision'], at=row['at'],
                                   source_sha256=row['source_sha256'], messages_sha256=row['messages_sha256'],
                                   state=latest['state'], label=latest['label'], error_code=latest['error_code'],
                                   updated_at=latest['at'], events=events, calls=list(calls.values()),
                                   reconstructed=False))
        return result

    def lifecycle(self, identifier):
        with self.transaction() as db:
            rows = db.execute('SELECT sequence,action,at FROM task_lifecycle WHERE task_id=? ORDER BY sequence', (identifier,)).fetchall()
        return dict(lifecycle_revision=len(rows), deleted=any(r['action']=='delete' for r in rows),
                    user_finished=any(r['action']=='finish' for r in rows), lifecycle_events=[dict(r) for r in rows])

    def guidance(self, identifier):
        with self.transaction() as db:
            self._read(db, identifier)
            return [dict(row) for row in db.execute(
                'SELECT sequence,note,at FROM task_guidance WHERE task_id=? ORDER BY sequence', (identifier,))]

    def approvals(self, identifier):
        with self.transaction() as db:
            self._read(db, identifier)
            return [dict(row) for row in db.execute(
                'SELECT sequence,scope,note,at FROM task_approvals WHERE task_id=? ORDER BY sequence', (identifier,))]

    def approve_plan(self, identifier, revision, *, scope, note=''):
        """记录用户对当前方案的批准；scope 绑定方案摘要，方案变化即失效。"""
        value = text(scope, 200)
        with self.transaction() as db:
            doc = self._control_plane(db, identifier, revision)
            sequence = (db.execute('SELECT MAX(sequence) FROM task_approvals WHERE task_id=?',
                                   (identifier,)).fetchone()[0] or 0) + 1
            db.execute('INSERT INTO task_approvals VALUES (?,?,?,?,?)',
                       (identifier, sequence, value, text(note, 1000, required=False) or '',
                        datetime.now(timezone.utc).isoformat()))
            # 批准是控制面动作：不改动冻结条件，也不推进 revision，
            # 因此批准后可以立即提交，不会让调用方的版本变成陈旧。
        return self.approvals(identifier)

    def plan_approved(self, identifier, scope):
        with self.transaction() as db:
            self._read(db, identifier)
            row = db.execute('SELECT 1 FROM task_approvals WHERE task_id=? AND scope=? LIMIT 1',
                             (identifier, text(scope, 200))).fetchone()
        return row is not None

    def _control_plane(self, db, identifier, revision):
        """引导与暂停不改动冻结的条件记录，因此允许在冻结之后使用；只校验版本。"""
        doc = self._read(db, identifier)
        if doc['revision'] != revision:
            raise StaleTask('任务已更新，请刷新后再操作')
        return doc

    def add_guidance(self, identifier, revision, note):
        """Append one user steering note; the next model calls are told about it."""
        value = text(note, 2000)
        with self.transaction() as db:
            doc = self._control_plane(db, identifier, revision)
            sequence = (db.execute('SELECT MAX(sequence) FROM task_guidance WHERE task_id=?',
                                   (identifier,)).fetchone()[0] or 0) + 1
            db.execute('INSERT INTO task_guidance VALUES (?,?,?,?)',
                       (identifier, sequence, value, datetime.now(timezone.utc).isoformat()))
            self._write(db, doc, 'guidance_added')
        return self.guidance(identifier)

    def paused(self, identifier):
        with self.transaction() as db:
            self._read(db, identifier)
            row = db.execute('SELECT action FROM task_control WHERE task_id=? ORDER BY sequence DESC LIMIT 1',
                             (identifier,)).fetchone()
        return bool(row and row['action'] == 'pause')

    def set_paused(self, identifier, revision, paused):
        action = 'pause' if paused else 'resume'
        wanted = bool(paused)
        with self.transaction() as db:
            doc = self._control_plane(db, identifier, revision)
            row = db.execute('SELECT action FROM task_control WHERE task_id=? ORDER BY sequence DESC LIMIT 1',
                             (identifier,)).fetchone()
            if bool(row and row['action'] == 'pause') == wanted:
                return {'paused': wanted, 'unchanged': True, 'revision': doc['revision']}
            sequence = (db.execute('SELECT MAX(sequence) FROM task_control WHERE task_id=?',
                                   (identifier,)).fetchone()[0] or 0) + 1
            db.execute('INSERT INTO task_control VALUES (?,?,?,?)',
                       (identifier, sequence, action, datetime.now(timezone.utc).isoformat()))
            doc = self._write(db, doc, 'task_' + ('paused' if wanted else 'resumed'))
        return {'paused': wanted, 'revision': doc['revision']}

    def manage_lifecycle(self, identifier, revision, lifecycle_revision, action):
        if action not in {'delete', 'finish'}: raise TaskError('未知的任务记录操作')
        with self.transaction() as db:
            document = self._read(db, identifier)
            rows = db.execute('SELECT action FROM task_lifecycle WHERE task_id=? ORDER BY sequence', (identifier,)).fetchall()
            if document['revision'] != revision or len(rows) != lifecycle_revision:
                raise StaleTask('任务记录已变化，请刷新后重试')
            if any(r['action']=='delete' for r in rows): raise TaskError('任务已从列表删除')
            if action=='finish' and any(r['action']=='finish' for r in rows): raise TaskError('已确认结束')
            db.execute('INSERT INTO task_lifecycle VALUES (?,?,?,?)',
                       (identifier, len(rows)+1, action, datetime.now(timezone.utc).isoformat()))
        return {**self.get(identifier), **self.lifecycle(identifier)}

    def list(self):
        with self.transaction() as db:
            rows = db.execute("SELECT r.document FROM revisions r JOIN tasks t ON t.id=r.task_id AND t.revision=r.revision "
                              "WHERE NOT EXISTS(SELECT 1 FROM task_lifecycle l WHERE l.task_id=t.id AND l.action='delete') "
                              'ORDER BY r.at DESC LIMIT 200').fetchall()
        return [dict(id=d['id'], title=d['title'], mode=d['mode'], status=d['status'], revision=d['revision'],
                     updated_at=d['updated_at'], outstanding=len(issues(d)), **self.lifecycle(d['id']))
                for row in rows for d in [json.loads(row['document'])]]

    def _write(self, db, document, event):
        if document['status'] != 'conditions_frozen' and 'initial_geometry' in document:
            previous = self._read(db, document['id'])
            def geometry_conditions(doc):
                return {field: {key: doc['fields'][field][key] for key in ('candidates', 'selected')}
                        for field in GEOMETRY_CONDITION_FIELDS}
            if geometry_conditions(previous) != geometry_conditions(document):
                document.pop('initial_geometry')
                document.pop('target_selection', None)
                event += ':initial_geometry_invalidated'
        # A selection applies to the conditions inspected at that moment.
        # Invalidate it in the same revision if any condition evidence changes.
        # Already frozen records remain immutable and are never backfilled.
        selection = document.get('target_selection')
        if (document['status'] != 'conditions_frozen' and selection
                and selection.get('conditions_sha256') != sha256(canonical(document['fields']))):
            document.pop('target_selection')
        document['revision'] += 1
        document['updated_at'] = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO revisions VALUES (?,?,?,?,?)', (document['id'], document['revision'], event,
                   document['updated_at'], canonical(document).decode()))
        db.execute('UPDATE tasks SET revision=? WHERE id=?', (document['revision'], document['id']))
        return {**document, 'issues': issues(document)}

    def _editable(self, db, identifier, revision):
        doc = self._read(db, identifier)
        if type(revision) is not int or revision != doc['revision']:
            raise StaleTask('任务已在其他页面更新，请刷新后再操作')
        if doc['status'] == 'conditions_frozen':
            raise FrozenTask('已冻结版本不可修改；这不是重新开始正式评测的入口')
        return doc

    def create(self, title, prompt, mode):
        if mode not in {'reproduction', 'research'}:
            raise TaskError('请选择复现或开放研究模式')
        doc = dict(schema_version=1, id=uuid.uuid4().hex, title=text(title, 160), prompt=text(prompt, 12000),
                   mode=mode, revision=0, status='draft',
                   fields={key: dict(candidates=[], selected=None, confirmed=False, resolution='') for key in FIELDS})
        for field, choice in explicit_conditions(doc['prompt']):
            append_condition(doc, field, choice)
        with self.transaction() as db:
            db.execute('INSERT INTO tasks VALUES (?,0)', (doc['id'],))
            return self._write(db, doc, 'created')

    def add_candidate(self, identifier, revision, field, data):
        if field not in FIELDS:
            raise TaskError('未知条件字段')
        choice = candidate(data)
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if choice['applicability'] == 'not_applicable' and (field in ESSENTIAL or (field == 'reference' and doc['mode'] == 'reproduction')):
                raise TaskError('此任务的必要字段不能标记为不适用')
            append_condition(doc, field, choice)
            return self._write(db, doc, 'candidate_added:'+field)

    def select(self, identifier, revision, field, candidate_id, reason):
        if field not in FIELDS:
            raise TaskError('未知条件字段')
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            condition = doc['fields'][field]
            if not any(item['id'] == candidate_id for item in condition['candidates']):
                raise TaskError('候选条件不存在')
            values = {(item['value'], item['unit'], item['applicability']) for item in condition['candidates']}
            reason = text(reason, 2000, required=len(values) > 1)
            condition.update(selected=candidate_id, confirmed=False, resolution=reason)
            return self._write(db, doc, 'condition_selected:'+field)

    def select_initial_geometry(self, identifier, revision, selection):
        """Save a trusted resource selection, without loading or evaluating atoms."""
        from .geometry_selection import validate_initial_geometry
        from .geometry_catalog import GeometryCatalogError
        try:
            clean = validate_initial_geometry(selection)
        except GeometryCatalogError as exc:
            raise TaskError('初始结构元数据无效；请选择已核对的固定结构') from exc
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if doc.get('initial_geometry') != clean:
                # Target selection predates this geometry; preserve its old
                # revision, but require a new target review before freezing.
                doc.pop('target_selection', None)
            doc['initial_geometry'] = clean
            return self._write(db, doc, 'initial_geometry_selected')

    def clear_initial_geometry(self, identifier, revision):
        """Clear an editable resource selection while retaining every old version."""
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if 'initial_geometry' not in doc:
                return {**doc, 'issues': issues(doc)}
            doc.pop('initial_geometry')
            doc.pop('target_selection', None)
            return self._write(db, doc, 'initial_geometry_cleared')

    def import_literature(self, identifier, revision, csv_text, **mapping):
        from .literature import input_from_csv
        choice, snapshot = input_from_csv(csv_text, **mapping)
        field = mapping['field']
        digest = choice['literature_source']['source_sha256']
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            sources = doc.setdefault('literature_sources', {})
            if digest not in sources and len(sources) >= 32:
                raise TaskError('此任务已达 32 份来源上限，请核对已有资料')
            # Same file/column/field is an import retry, not new evidence. Do not
            # let changed classification wording create duplicate candidates.
            if any(c.get('literature_source', {}).get('source_sha256') == digest
                   and c['literature_source']['column'] == mapping['column']
                   for c in doc['fields'][field]['candidates']):
                raise TaskError('此来源列已导入该条件，请核对已有记录')
            append_condition(doc, field, choice)
            sources[digest] = snapshot
            return self._write(db, doc, 'literature_imported:'+field)

    def confirm(self, identifier, revision, fields):
        if (not isinstance(fields, list) or not fields or any(not isinstance(key, str) or key not in FIELDS for key in fields)
                or len(fields) != len(set(fields))):
            raise TaskError('请选择需要确认的条件')
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if any(doc['fields'][key]['selected'] is None for key in fields):
                raise TaskError('先补齐缺项并解决矛盾，再确认条件')
            for key in fields:
                doc['fields'][key]['confirmed'] = True
            return self._write(db, doc, 'user_confirmed:'+','.join(fields))

    def import_generated_conditions(self, identifier, revision, sources, completion, *, condition_request_id=None):
        from .condition_generation import validate_conditions
        if (not isinstance(completion, dict) or set(completion) != {'value', 'request_id', 'receipt'}
                or not isinstance(completion['request_id'], str)
                or not re.fullmatch('[a-f0-9]{32}', completion['request_id'])
                or not isinstance(completion['receipt'], dict)
                or completion['receipt'].get('state') != 'completed'
                or completion['receipt'].get('output_sha256') != sha256(canonical(completion['value']))):
            raise TaskError('生成记录与条件输出不一致')
        choices, questions, sources = validate_conditions(sources, completion['value'])
        source_digest = sha256(canonical(sources))
        request_id = completion['request_id']
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if condition_request_id is not None:
                intent = db.execute('SELECT * FROM condition_requests WHERE id=?', (condition_request_id,)).fetchone()
                repair_id = sha256(canonical({'base': condition_request_id, 'repair': 1}))[:32]
                if (intent is None or intent['task_id'] != identifier or intent['revision'] != revision
                        or intent['source_sha256'] != source_digest or request_id not in {condition_request_id, repair_id}):
                    raise TaskError('条件整理导入与意图不一致')
            batches = doc.setdefault('generated_batches', {})
            if request_id in batches:
                raise TaskError('这次生成结果已经导入')
            if len(batches) >= 16:
                raise TaskError('此任务已达生成记录上限，请核对已有结果')
            if doc['mode'] == 'research' and (any(e['field'] == 'reference' for e in choices)
                                               or any(q['field'] == 'reference' for q in questions)):
                raise TaskError('科研计算不要求论文标识，模型整理结果未导入')
            for entry in choices:
                existing = [{k: v for k, v in c.items() if k != 'id'} for c in doc['fields'][entry['field']]['candidates']]
                if entry['candidate'] not in existing:
                    append_condition(doc, entry['field'], entry['candidate'])
            batches[request_id] = dict(sources=sources, sources_sha256=source_digest,
                                      questions=questions, response=completion['value'], receipt=completion['receipt'],
                                      revision=doc['revision']+1)
            result = self._write(db, doc, 'conditions_generated')
            if condition_request_id is not None:
                self._condition_event(db, condition_request_id, 'imported', call_id=request_id,
                                      receipt=completion['receipt'])
            return result

    def save_reference_intent(self, identifier, revision, request_id, context, operation):
        """Trusted reference service only; preserve source bytes before model I/O."""
        if (not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id)
                or operation != sha256(canonical(context)) or request_id != operation[:32]
                or context.get('task_id') != identifier
                or not isinstance(context.get('accounting_sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', context['accounting_sha256'])):
            raise TaskError('参考整理意图身份不一致')
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if doc['mode'] != 'reproduction':
                raise TaskError('参考整理仅适用于文献任务')
            for row in db.execute('SELECT document FROM reference_intents WHERE task_id=?', (identifier,)):
                previous = json.loads(row['document'])
                if (previous['request_sha256'] == context['request_sha256']
                        and previous.get('accounting_sha256') != context.get('accounting_sha256')):
                    raise TaskError('参考模型记账配置与已有请求不同，请核对原记录；未发送新请求')
            existing = db.execute('SELECT operation_sha256,document FROM reference_intents WHERE id=?',
                                  (request_id,)).fetchone()
            if existing:
                if existing['operation_sha256'] != operation or existing['document'] != canonical(context).decode():
                    raise TaskError('参考整理意图不能替换')
                return
            if db.execute('SELECT count(*) FROM reference_intents WHERE task_id=?', (identifier,)).fetchone()[0] >= 16:
                raise TaskError('此任务参考整理意图已达上限')
            db.execute('INSERT INTO reference_intents VALUES (?,?,?,?,?,?)',
                       (request_id, identifier, revision, operation, datetime.now(timezone.utc).isoformat(),
                        canonical(context).decode()))

    def reference_requests(self, identifier):
        """Operator-only request metadata, without source text or model output."""
        with self.transaction() as db:
            doc = self._read(db, identifier)
            if doc['mode'] != 'reproduction':
                raise TaskError('普通科研没有文献参考请求')
            rows = db.execute('SELECT id,revision,at,document FROM reference_intents WHERE task_id=? '
                              'ORDER BY at,id', (identifier,)).fetchall()
        return [dict(request_id=row['id'], revision=row['revision'], at=row['at'],
                     accounting_sha256=json.loads(row['document']).get('accounting_sha256'),
                     imported=row['id'] in doc.get('reference_batches', {})) for row in rows]

    def reference_intent(self, identifier, request_id):
        with self.transaction() as db:
            self._read(db, identifier)
            row = db.execute('SELECT document,operation_sha256 FROM reference_intents WHERE task_id=? AND id=?',
                             (identifier, request_id)).fetchone()
        if row is None:
            raise KeyError('这项任务没有对应的文献整理记录')
        return json.loads(row['document']), row['operation_sha256']

    def import_generated_reference(self, identifier, revision, request_id, context, operation, completion):
        from .reference_generation import validate_reference
        if (not isinstance(completion, dict) or set(completion) != {'value', 'request_id', 'receipt'}
                or completion['request_id'] != request_id or not isinstance(completion['receipt'], dict)
                or completion['receipt'].get('state') != 'completed'
                or completion['receipt'].get('output_sha256') != sha256(canonical(completion['value']))
                or completion['receipt'].get('request_sha256') != context['request_sha256']
                or operation != sha256(canonical(context)) or request_id != operation[:32]):
            raise TaskError('参考模型回执与意图不一致')
        choices, results, questions, sources = validate_reference(context['sources'], completion['value'])
        with self.transaction() as db:
            intent = db.execute('SELECT task_id,document,operation_sha256 FROM reference_intents WHERE id=?',
                                (request_id,)).fetchone()
            if (intent is None or intent['task_id'] != identifier or intent['operation_sha256'] != operation
                    or intent['document'] != canonical(context).decode()):
                raise TaskError('缺少相符的参考来源意图')
            doc = self._read(db, identifier)
            if request_id in doc.get('reference_batches', {}):
                return {**doc, 'issues': issues(doc)}
            doc = self._editable(db, identifier, revision)
            if doc['mode'] != 'reproduction':
                raise TaskError('参考结果不能进入普通科研任务')
            for entry in choices:
                existing = [{k: v for k, v in c.items() if k != 'id'} for c in doc['fields'][entry['field']]['candidates']]
                if entry['candidate'] not in existing:
                    append_condition(doc, entry['field'], entry['candidate'])
            doc.setdefault('reference_batches', {})[request_id] = dict(
                operation_sha256=operation, sources=sources, exports=context['exports'],
                sources_sha256=sha256(canonical(sources)), reported_results=results, questions=questions,
                response=completion['value'], receipt=completion['receipt'], revision=doc['revision'] + 1,
                automatic_semantic_verification='not_performed', reference_qualified=False,
                runtime_actor='independent_api', execution_authorized=False)
            return self._write(db, doc, 'reference_evidence_generated')

    def import_target_inventory(self, identifier, revision, value):
        """Trusted literature-side adapter; deliberately not a browser write API."""
        from .target_planning import inventory
        clean = inventory(value)
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if doc['mode'] != 'reproduction':
                raise TaskError('普通研究任务不要求论文目标清单')
            doc['target_inventory'] = clean
            doc.pop('target_selection', None)
            return self._write(db, doc, 'target_inventory_assessed')

    def select_targets(self, identifier, revision, selected_ids, exclusion_reason):
        from .target_planning import selected_plan
        with self.transaction() as db:
            doc = self._editable(db, identifier, revision)
            if doc['mode'] != 'reproduction':
                raise TaskError('普通研究任务不要求论文目标清单')
            doc['target_selection'] = selected_plan(doc, selected_ids, exclusion_reason)
            return self._write(db, doc, 'targets_selected')

    def freeze(self, identifier, revision):
        with self.transaction() as db:
            doc = self._read(db, identifier)
            if doc['status'] == 'conditions_frozen':
                return {**doc, 'issues': []}
            doc = self._editable(db, identifier, revision)
            if issues(doc):
                raise TaskError('仍有缺失、矛盾或未确认的条件，不能冻结')
            contract = dict(schema_version=1, purpose='condition_review_record', task_id=doc['id'], mode=doc['mode'],
                            title=doc['title'], prompt=doc['prompt'], conditions=doc['fields'],
                            scientific_validation='not_performed', execution_authorized=False)
            if 'initial_geometry' in doc:
                from .geometry_selection import validate_initial_geometry
                from .geometry_catalog import GeometryCatalogError
                try:
                    contract['initial_geometry'] = validate_initial_geometry(doc['initial_geometry'])
                except GeometryCatalogError as exc:
                    raise TaskError('初始结构记录未通过核对，不能冻结') from exc
            if doc['mode'] == 'reproduction':
                from .target_planning import freeze_plan
                contract['target_plan'] = freeze_plan(doc)
            if doc.get('literature_sources'):
                contract['literature_sources'] = doc['literature_sources']
            if doc.get('generated_batches'):
                contract['generated_batches'] = doc['generated_batches']
            if doc.get('reference_batches'):
                contract['reference_batches'] = doc['reference_batches']
            content = canonical(contract)
            digest = sha256(content)
            db.execute('INSERT INTO frozen VALUES (?,?,?)', (doc['id'], digest, content.decode()))
            doc.update(status='conditions_frozen', record_sha256=digest)
            return self._write(db, doc, 'conditions_frozen')

    def export(self, identifier):
        with self.transaction() as db:
            self._read(db, identifier)
            row = db.execute('SELECT sha256, document FROM frozen WHERE task_id=?', (identifier,)).fetchone()
            if row is None:
                raise TaskError('只有冻结后的条件可以导出')
            content = row['document'].encode()
            if sha256(content) != row['sha256']:
                raise TaskError('冻结记录校验失败')
            return content

    def history(self, identifier):
        with self.transaction() as db:
            self._read(db, identifier)
            return [dict(row) for row in db.execute('SELECT revision,event,at FROM revisions WHERE task_id=? ORDER BY revision', (identifier,))]

    def export_packages(self, identifier):
        from .task_packages import split_condition_record
        # export verifies the immutable record's digest before projection.
        return split_condition_record(self.export(identifier))
