"""User PDF intake into the project's existing, private literature workbench.

This owns only registration and recoverable linkage. The separately installed
workbench still owns extraction, sealed snapshots, verification and publication.
No model call, condition import, A/B execution or source discovery occurs here.
"""
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
import unicodedata

from .manifest import canonical, sha256
from .papers import doi_text
from .tasks import TaskError, issues, task_id, text
from .workbench_bridge import WorkbenchPaperBinding
from .workbench_runtime import _private_root, _write_once
from .runtime_launcher import read_regular

MAX_PDF_BYTES = 20 * 1024 * 1024


def _normal(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def inspect_pdf(data):
    """Read metadata/identity with the same installed PDF library, without AI."""
    try:
        pdf = importlib.import_module('pymupdf')
        with pdf.open(stream=data, filetype='pdf') as document:
            if document.needs_pass or not 1 <= document.page_count <= 2000:
                raise ValueError('unsupported PDF')
            content = '\n'.join(document.load_page(i).get_text() for i in range(min(3, document.page_count)))
            title = (document.metadata or {}).get('title', '').strip()
            if not title or len(title) > 500 or _normal(title) not in _normal(content):
                title = ''
            dois = sorted(set(re.findall(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]+', content, re.I)))[:16]
            return dict(pages=document.page_count, title=title,
                        dois=[d.rstrip('.,;)') for d in dois], content=content)
    except ImportError:
        raise TaskError('PDF 读取组件尚未安装。请在帮助页查看工作台安装说明。') from None
    except Exception:
        raise TaskError('PDF 无法读取或需要密码。请上传可打开、可检索正文的论文 PDF。') from None


class PaperIntake:
    def __init__(self, papers, bridge, configured_bindings=None, *, pdf_reader=inspect_pdf):
        self.papers, self.tasks, self.bridge = papers, papers.tasks, bridge
        self.configured = configured_bindings or {}
        self.pdf_reader = pdf_reader
        self.root = getattr(getattr(bridge, 'runtime', None), 'pdf_intake_root', None)
        if self.root is not None:
            self.root = Path(self.root)
            _private_root(str(self.root.parent))
            self.root.mkdir(mode=0o700, exist_ok=True)
            _private_root(str(self.root))
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS paper_imports ('
                       'id TEXT PRIMARY KEY, pdf_sha256 TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS paper_import_revisions ('
                       'id TEXT NOT NULL REFERENCES paper_imports(id), revision INTEGER NOT NULL, '
                       'event TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(id, revision))')
            db.execute('CREATE TABLE IF NOT EXISTS user_workbench_bindings ('
                       'paper_id TEXT PRIMARY KEY REFERENCES papers(id), workbench_paper_id INTEGER NOT NULL UNIQUE, '
                       'pdf_sha256 TEXT NOT NULL, import_id TEXT NOT NULL REFERENCES paper_imports(id))')
            for table in ('paper_import_revisions', 'user_workbench_bindings'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} '
                               "BEGIN SELECT RAISE(ABORT, 'immutable PDF registration history'); END")

    def availability(self):
        return dict(configured=self.root is not None, max_pdf_bytes=MAX_PDF_BYTES,
                    reason='' if self.root else '尚未连接本项目文献工作台，暂不能上传 PDF。')

    def _enabled(self):
        if self.root is None:
            raise TaskError(self.availability()['reason'])

    def _read(self, db, identifier):
        row = db.execute('SELECT r.document FROM paper_imports p JOIN paper_import_revisions r '
                         'ON p.id=r.id AND p.revision=r.revision WHERE p.id=?', (task_id(identifier),)).fetchone()
        if row is None:
            raise TaskError('文献导入记录不存在。请重新选择 PDF。')
        return json.loads(row['document'])

    def _save(self, db, doc, event):
        doc = dict(doc, revision=doc['revision']+1, updated_at=datetime.now(timezone.utc).isoformat())
        db.execute('INSERT INTO paper_import_revisions VALUES (?,?,?,?)',
                   (doc['id'], doc['revision'], event, canonical(doc).decode()))
        db.execute('UPDATE paper_imports SET revision=? WHERE id=?', (doc['revision'], doc['id']))
        return doc

    @staticmethod
    def _view(doc):
        return {k: doc[k] for k in ('id', 'revision', 'state', 'pdf_sha256', 'size_bytes', 'pages',
            'suggested_title', 'suggested_dois', 'title', 'doi', 'scope', 'paper_id', 'task_id', 'updated_at') if k in doc}

    def get(self, identifier):
        with self.tasks.transaction() as db:
            return self._view(self._read(db, identifier))

    def list(self):
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT r.document FROM paper_imports p JOIN paper_import_revisions r '
                'ON p.id=r.id AND p.revision=r.revision ORDER BY p.rowid DESC LIMIT 50').fetchall()
        return dict(self.availability(), imports=[self._view(json.loads(row['document'])) for row in rows])

    def _pdf(self, doc):
        self._enabled()
        data = read_regular(self.root/(doc['pdf_sha256']+'.pdf'), MAX_PDF_BYTES, private=True)
        if sha256(data) != doc['pdf_sha256'] or len(data) != doc['size_bytes']:
            raise TaskError('已保存 PDF 的内容发生变化。原记录保留，不能替换来源。')
        return data

    def upload(self, identifier, data):
        self._enabled(); task_id(identifier)
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_PDF_BYTES:
            raise TaskError('请选择不超过 20 MiB 的 PDF。')
        if not data.startswith(b'%PDF-'):
            raise TaskError('文件不是 PDF。请重新选择论文原文。')
        digest = sha256(data)
        # Serialize file publication with IDs/deduplication; a crash can leave
        # an immutable orphan, which the same upload safely verifies and adopts.
        with self.tasks.transaction() as db:
            prior = db.execute('SELECT id FROM paper_imports WHERE id=? OR pdf_sha256=?',
                               (identifier, digest)).fetchall()
            if prior:
                doc = self._read(db, prior[0]['id'])
                if len(prior) != 1 or doc['pdf_sha256'] != digest:
                    raise TaskError('该导入编号已绑定另一份 PDF。不能替换来源。')
                self._pdf(doc)
                return self._view(doc)
            metadata = self.pdf_reader(data)
            location = self.root/(digest+'.pdf')
            if location.exists() or location.is_symlink():
                if read_regular(location, MAX_PDF_BYTES, private=True) != data:
                    raise TaskError('保存的 PDF 摘要冲突，未覆盖任何文件。')
            else:
                _write_once(location, data)
            doc = dict(id=identifier, revision=0, state='uploaded', pdf_sha256=digest,
                size_bytes=len(data), pages=metadata['pages'], suggested_title=metadata['title'],
                suggested_dois=metadata['dois'], title='', doi='', scope='', paper_id=None, task_id=None)
            db.execute('INSERT INTO paper_imports VALUES (?,?,0)', (identifier, digest))
            return self._view(self._save(db, doc, 'pdf_uploaded'))

    def binding(self, paper_id):
        with self.tasks.transaction() as db:
            return self._binding(db, paper_id)

    def _binding(self, db, paper_id):
        row = db.execute('SELECT paper_id,workbench_paper_id,pdf_sha256 FROM user_workbench_bindings '
                         'WHERE paper_id=?', (paper_id,)).fetchone()
        saved = WorkbenchPaperBinding(**dict(row)) if row else None
        configured = self.configured.get(paper_id)
        if saved and configured and saved != configured:
            raise TaskError('论文已有不同的工作台绑定，不能转移来源。')
        return saved or configured

    def confirm(self, identifier, *, title, doi, scope):
        self._enabled()
        title, doi, scope = text(title, 500), doi_text(doi), text(scope, 4000)
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            if doc['state'] in {'prepared', 'bound', 'task_created'}:
                if (doc['title'], doc['doi'], doc['scope']) != (title, doi, scope):
                    raise TaskError('登记已固定论文身份和范围。请继续原记录，不要替换来源。')
                if doc['state'] in {'bound', 'task_created'}:
                    self._pdf(doc)
                    return self._view(doc)
            else:
                content = self.pdf_reader(self._pdf(doc))['content']
                if len(_normal(title)) < 10 or _normal(title) not in _normal(content):
                    raise TaskError('题目与 PDF 前三页不匹配。请填写完整原文题目；扫描件需先转为可检索 PDF。')
                if doi not in re.sub(r'\s+', '', unicodedata.normalize('NFKC', content)).casefold():
                    raise TaskError('未在 PDF 前三页找到这个 DOI。请核对原文 DOI 或选择包含出版信息的版本。')
                existing = db.execute('SELECT id FROM papers WHERE doi=?', (doi,)).fetchone()
                if existing:
                    paper = self.papers._read(db, existing['id'])
                    if paper['title'] != title:
                        raise TaskError('该 DOI 已登记不同题名。请使用现有论文记录或核对身份，不会覆盖历史。')
                    old_binding = self._binding(db, paper['id'])
                    if old_binding and old_binding.pdf_sha256 != doc['pdf_sha256']:
                        raise TaskError('该论文已绑定另一份 PDF。请使用已有记录，不能替换提取来源。')
                else:
                    paper = self.papers._add(db, title, doi, scope, '用户 PDF 登记；待选择复现目标')
                doc.update(title=title, doi=doi, scope=scope, paper_id=paper['id'], state='prepared')
                doc = self._save(db, doc, 'registration_prepared')
        # This separate, durable intent recovers the two stores after interruption.
        # The native upsert is used only for an absent DOI; existing evidence is
        # verified and reused, never passed to a source-overwriting upsert.
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            if doc['state'] in {'bound', 'task_created'}:
                return self._view(doc)
            self._pdf(doc)
            native = self.bridge.runtime.database
            old_binding = self._binding(db, doc['paper_id'])
            rows = [row for row in native.list_papers() if row.get('doi') and doi_text(row['doi']) == doi]
            if len(rows) > 1:
                raise TaskError('工作台存在多个相同 DOI 来源。请核对登记，原记录保留。')
            if rows:
                source = rows[0]
                if source.get('title') != title or source.get('pdf_sha256') != doc['pdf_sha256']:
                    raise TaskError('工作台已有不同题目或 PDF 来源，未覆盖。请使用已有记录。')
                native_id = source['id']
            else:
                native_id = native.upsert_paper(title=title, doi=doi,
                    pdf_path=str(self.root/(doc['pdf_sha256']+'.pdf')), pdf_sha256=doc['pdf_sha256'],
                    authenticity_status='user_pdf_identity_matched', parse_status='queued')
            source = native.get_paper(native_id)
            if (not source or source.get('title') != title or doi_text(source.get('doi', '')) != doi
                    or source.get('pdf_sha256') != doc['pdf_sha256']
                    or self.bridge.runtime.native_source_reader(native_id) != doc['pdf_sha256']):
                raise TaskError('工作台 PDF 登记尚未完成核对。可继续同一导入，未发模型请求。')
            binding = WorkbenchPaperBinding(doc['paper_id'], native_id, doc['pdf_sha256'])
            if old_binding and old_binding != binding:
                raise TaskError('已有工作台绑定与登记来源不同，不能转移。')
            if not old_binding:
                db.execute('INSERT INTO user_workbench_bindings VALUES (?,?,?,?)',
                    (binding.paper_id, binding.workbench_paper_id, binding.pdf_sha256, identifier))
            doc['state'] = 'bound'
            return self._view(self._save(db, doc, 'workbench_bound'))

    def create_task(self, identifier):
        self._enabled()
        with self.tasks.transaction() as db:
            doc = self._read(db, identifier)
            if doc['task_id']:
                task = self.tasks._read(db, doc['task_id'])
                return {**task, 'issues': issues(task)}
            if doc['state'] != 'bound':
                raise TaskError('请先核对题目、DOI 并完成文献登记。')
            self._pdf(doc)
            if not self._binding(db, doc['paper_id']):
                raise TaskError('文献工作台绑定尚未完成。请继续原导入。')
            paper = self.papers._read(db, doc['paper_id'])
            if paper['selection'] != 'selected':
                paper['selection'] = 'selected'
                self.papers._write(db, paper, 'paper_selected')
            task = self.tasks._create(db, doc['title'][:160],
                '请先从论文全文提取图表与计算条件，再核对复现目标。\n研究范围：'+doc['scope'], 'reproduction')
            db.execute('INSERT INTO paper_tasks VALUES (?,?)', (paper['id'], task['id']))
            self.papers._write(db, paper, 'task_linked:'+task['id'])
            doc.update(state='task_created', task_id=task['id'])
            self._save(db, doc, 'reproduction_task_created')
            return task
