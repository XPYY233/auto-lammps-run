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
ORIGINS = {'user', 'paper', 'code', 'proposed'}
APP_ID = 0x414C5453


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
            for table in ('revisions', 'frozen'):
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

    def list(self):
        with self.transaction() as db:
            rows = db.execute('SELECT r.document FROM revisions r JOIN tasks t ON t.id=r.task_id AND t.revision=r.revision '
                              'ORDER BY r.at DESC LIMIT 200').fetchall()
        return [dict(id=d['id'], title=d['title'], mode=d['mode'], status=d['status'], revision=d['revision'],
                     updated_at=d['updated_at'], outstanding=len(issues(d))) for row in rows for d in [json.loads(row['document'])]]

    def _write(self, db, document, event):
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

    def import_generated_conditions(self, identifier, revision, sources, completion):
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
            return self._write(db, doc, 'conditions_generated')

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
            if doc.get('literature_sources'):
                contract['literature_sources'] = doc['literature_sources']
            if doc.get('generated_batches'):
                contract['generated_batches'] = doc['generated_batches']
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
