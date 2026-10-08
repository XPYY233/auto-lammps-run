"""Read-only human A evidence, isolated from research/B model inputs.

The private controller registers existing artifacts, never browser supplied
results. Reading checks hashes and the original reference ledger; it neither
recomputes science nor publishes a scientific verdict. ``assistant_context`` is
for a separately namespaced human A conversation, never ordinary B discussion.
"""
import csv
import io
import json
import math
from pathlib import Path
import re

from . import runtime_launcher as runtime
from .closeout import evidence_views
from .manifest import canonical, sha256
from .results import existing_private_directory, ResultUnavailable
from .tasks import task_id, text


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ASSISTANT_CELLS = 50000
MAX_ASSISTANT_BYTES = 220000
_HASH = re.compile(r'[a-f0-9]{64}')
_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}')
_NON_A_COLUMN = re.compile(r'^(p|paper|b|agent|target|score)(_|$)|_minus_(p|b)(_|$)', re.I)
_TERMINAL = {'completed', 'failed', 'timeout', 'cancelled'}


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ResultUnavailable('Invalid reference evidence digest')
    return value


def _coverage(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        raise ResultUnavailable('Missing reference coverage')
    result, labels = [], set()
    for item in items:
        label = text(item['label'], 300)
        required, available = item['required'], item['available']
        if (label in labels or type(required) is not int or required < 1
                or type(available) is not int or not 0 <= available <= required):
            raise ResultUnavailable('Invalid reference coverage counts')
        labels.add(label)
        result.append(dict(label=label, required=required, available=available,
                           unit=text(item['unit'], 100)))
    return result


def _methods(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise ResultUnavailable('Missing reference analysis methods')
    result, seen = [], set()
    for item in items:
        identifier = item['id']
        if (not isinstance(identifier, str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', identifier)
                or identifier in seen):
            raise ResultUnavailable('Invalid reference method identity')
        seen.add(identifier)
        result.append(dict(id=identifier, label=text(item['label'], 300),
            description=text(item['description'], 4000), unit=text(item['unit'], 100),
            source_sha256=_hash(item['source_sha256'])))
    return result


def _table(item, data):
    """Read declared A columns only; preserve source strings and missing cells."""
    name, columns = item['name'], item['columns']
    if (item['role'] != 'reference' or name not in data or not name.endswith('.csv')
            or not isinstance(columns, list) or not 1 <= len(columns) <= 20):
        raise ResultUnavailable('Invalid reference table declaration')
    reader = csv.DictReader(io.StringIO(data[name].decode('utf-8-sig')))
    headers = reader.fieldnames or []
    if not headers or len(headers) != len(set(headers)):
        raise ResultUnavailable('Duplicate or missing reference table headers')
    selected, seen = [], set()
    for column in columns:
        key = column['key']
        if (not isinstance(key, str) or key not in headers or key in seen
                or _NON_A_COLUMN.search(key)
                or column.get('source_role', 'reference') != 'reference'
                or column.get('kind', 'text') not in {'text', 'number'}):
            raise ResultUnavailable('Reference table cannot contain P/B answer columns')
        seen.add(key)
        selected.append(dict(key=key, label=text(column['label'], 100),
                             unit=text(column['unit'], 100)))
    rows = []
    for row in reader:
        if len(rows) >= 10000 or None in row or any(value is None for value in row.values()):
            raise ResultUnavailable('Malformed or oversized reference table')
        values = []
        for column in columns:
            value = text(row[column['key']], 100, required=False)
            if value.lower() in {'nan', 'inf', '+inf', '-inf', 'infinity', '+infinity', '-infinity'}:
                raise ResultUnavailable('Nonfinite reference table value')
            if value and column.get('kind') == 'number':
                try:
                    finite = math.isfinite(float(value))
                except (ValueError, OverflowError):
                    finite = False
                if not finite:
                    raise ResultUnavailable('Invalid numeric reference table value')
            values.append(value)
        rows.append(values)
    return dict(name=name, role='reference', label=text(item['label'], 200),
                columns=selected, rows=rows, total_rows=len(rows),
                sha256=sha256(data[name]), truncated=False)


class ReferenceEvidenceViews:
    """Bound evidence in ``directory/task_id/reference-evidence/manifest.json``.

    ``get`` returns a human-safe report or None. ``download`` returns only files
    explicitly marked human. ``assistant_context`` includes complete selected A
    table columns with units, methods, coverage and the exact A source identity;
    oversize contexts fail before a model request instead of silently sampling.
    Raw controller receipts, images, P comparisons, author code and B conditions
    never become assistant input.
    """
    def __init__(self, directory, papers):
        self.directory = existing_private_directory(directory)
        self.papers = papers

    def _load(self, identifier):
        task_id(identifier)
        self.papers.tasks.get(identifier)
        folder = self.directory / identifier / 'reference-evidence'
        if not folder.exists():
            return None, None, []
        existing_private_directory(folder)
        raw = runtime.read_regular(folder / 'manifest.json', 262144, private=True)
        document = json.loads(raw)
        if (type(document.get('version')) is not int or document.get('version') != 1
                or document.get('task_id') != identifier
                or document.get('role') != 'reference_evidence_human_only'
                or document.get('scientific_status') != 'not_evaluated'):
            raise ResultUnavailable('Invalid human reference evidence identity')
        paper = self.papers.get(document['paper_id'])
        if (document['title'] != paper['title'] or document['doi'] != paper['doi']
                or identifier not in [item['id'] for item in paper['tasks']]):
            raise ResultUnavailable('Reference evidence belongs to another task or paper')
        progress = self.papers.reference_progress(identifier)
        matches = [entry for entry in progress['entries']
                   if entry['paper']['id'] == paper['id']
                   and entry['evaluation']['id'] == document['evaluation_id']]
        if len(matches) != 1 or not matches[0]['evaluation']['available'] or self.papers.ledger is None:
            raise ResultUnavailable('Reference ledger is unavailable or unbound')
        evaluation = matches[0]['evaluation']
        request_id = task_id(document['request_id'])
        requests = [item for item in evaluation['requests'] if item['id'] == request_id]
        if len(requests) != 1:
            raise ResultUnavailable('Reference request belongs to another evaluation')
        request = requests[0]
        original = self.papers.ledger.get(request_id)
        if (original['evaluation'] != document['evaluation_id']
                or original['manifest_sha256'] != _hash(document['input_manifest_sha256'])
                or request['job_id'] != document['job_id'] or original['job_id'] != document['job_id']
                or original['state'] != request['state'] or request['state'] not in _TERMINAL
                or not request['accounted'] or not original['accounted']):
            raise ResultUnavailable('Reference execution is not terminal, accounted and matched')
        coverage, methods = _coverage(document['coverage']), _methods(document['methods'])
        status, valid = document['output_status'], document['output_valid']
        complete = all(item['available'] == item['required'] for item in coverage)
        if (type(valid) is not bool or status not in {'complete', 'partial'}
                or (status == 'complete' and (request['state'] != 'completed' or not valid or not complete))
                or (status == 'partial' and (valid or complete))):
            raise ResultUnavailable('Reference output status contradicts execution or coverage')
        limitations = document['limitations']
        if not isinstance(limitations, list) or not 1 <= len(limitations) <= 64:
            raise ResultUnavailable('Reference limitations must remain explicit')
        limitations = [text(item, 4000) for item in limitations]
        files, data, public_files, total = document['files'], {}, [], 0
        if not isinstance(files, list) or not 1 <= len(files) <= 128:
            raise ResultUnavailable('Invalid reference artifacts')
        for item in files:
            name = item['name']
            if (not isinstance(name, str) or not _NAME.fullmatch(name) or name in data or name == 'manifest.json'
                    or Path(name).suffix not in {'.json', '.md', '.csv', '.png', '.jpg', '.jpeg', '.pdf'}
                    or item['visibility'] not in {'human', 'private_receipt'}):
                raise ResultUnavailable('Invalid reference artifact filename or visibility')
            content = runtime.read_regular(folder / name, MAX_FILE_BYTES, private=True)
            total += len(content)
            if (total > MAX_TOTAL_BYTES or type(item['size']) is not int or item['size'] != len(content)
                    or _hash(item['sha256']) != sha256(content)):
                raise ResultUnavailable('Reference artifact changed')
            data[name] = content
            if item['visibility'] == 'human':
                public_files.append(dict(name=name, label=text(item['label'], 300), size=len(content)))
        receipt_name = document['source_receipt_file']
        source = _hash(document['source_sha256'])
        if (receipt_name not in data or not receipt_name.endswith('.json')
                or any(item['name'] == receipt_name and item['visibility'] != 'private_receipt' for item in files)
                or sha256(data[receipt_name]) != source):
            raise ResultUnavailable('Reference source receipt changed or exposed')
        receipt = json.loads(data[receipt_name])
        identity = ('task_id', 'evaluation_id', 'request_id', 'job_id', 'input_manifest_sha256',
                    'output_status', 'output_valid', 'coverage', 'methods')
        expected_files = [dict(name=item['name'], size=item['size'], sha256=item['sha256'])
                          for item in files if item['name'] != receipt_name]
        if (type(receipt.get('version')) is not int or receipt.get('version') != 1
                or receipt.get('role') != 'reference_source_receipt'
                or receipt.get('paper') != dict(title=paper['title'], doi=paper['doi'])
                or canonical({key: receipt.get(key) for key in identity})
                    != canonical({key: document[key] for key in identity})
                or receipt.get('scheduler_state') != request['state']
                or canonical(receipt.get('files')) != canonical(expected_files)):
            raise ResultUnavailable('Reference receipt identity, coverage or artifact binding changed')
        public_names = {item['name'] for item in public_files}
        figures = document['figures']
        if not isinstance(figures, list) or len(figures) > 64:
            raise ResultUnavailable('Invalid reference figures')
        public_figures, seen = [], set()
        for figure in figures:
            name = figure['name']
            if name not in public_names or name in seen or Path(name).suffix not in {'.png', '.jpg', '.jpeg'}:
                raise ResultUnavailable('Undeclared or private reference figure')
            seen.add(name)
            public_figures.append(dict(name=name, label=text(figure['label'], 300),
                caption=text(figure['caption'], 4000), kind='plot'))
        views = evidence_views(document, data, public_figures, [], allow_missing_cells=True)
        full_tables, table_by_name = [], {}
        for view, declaration in zip(views, document.get('views', [])):
            if view['stress_curve']:
                raise ResultUnavailable('Reference view cannot borrow a legacy stress curve')
            for item in view['figures'] + view['tables']:
                if item['role'] != 'reference' or item['name'] not in public_names:
                    raise ResultUnavailable('Reference views cannot expose P/B or private receipts')
            for table, declared in zip(view['tables'], declaration.get('tables', [])):
                full = _table(declared, data)
                table['columns'] = full['columns']
                table['sha256'] = full['sha256']
                if table['name'] in table_by_name and table_by_name[table['name']] != full:
                    raise ResultUnavailable('Reference table has conflicting column selections')
                if table['name'] not in table_by_name:
                    table_by_name[table['name']] = full
                    full_tables.append(full)
        if status == 'complete' and (not full_tables or any(not item['rows'] for item in full_tables)):
            raise ResultUnavailable('Complete reference lacks readable analysis data')
        public = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'], doi=paper['doi'],
            role='reference', scope=text(document['scope'], 4000), summary=text(document['summary'], 8000),
            limitations=limitations, scientific_status='not_evaluated', execution_authorized=False,
            output_status=status, output_valid=valid, scheduler_state=request['state'],
            request_id=request_id, job_id=request['job_id'], evaluation_id=evaluation['id'],
            source_sha256=source, manifest_sha256=sha256(raw), coverage=coverage, methods=methods,
            figures=public_figures, files=public_files, views=views, history=evaluation['requests'],
            accounted=True, actual_core_hours=request['actual_core_seconds'] / 3600,
            raw_output_status='not_published')
        return public, data, full_tables

    def get(self, identifier):
        return self._load(identifier)[0]

    def download(self, identifier, name):
        report, data, _ = self._load(identifier)
        if report is None or name not in {item['name'] for item in report['files']}:
            raise ResultUnavailable('No declared human reference artifact')
        return data[name]

    def assistant_context(self, identifier):
        report, _, tables = self._load(identifier)
        if report is None:
            raise ResultUnavailable('No verified author reference analysis is available')
        if sum(len(item['rows']) * len(item['columns']) for item in tables) > MAX_ASSISTANT_CELLS:
            raise ResultUnavailable('Reference analysis context is too large; no rows were silently omitted')
        keys = ('task_id', 'paper_id', 'title', 'doi', 'role', 'scope', 'summary', 'limitations',
                'scientific_status', 'output_status', 'output_valid', 'scheduler_state', 'request_id',
                'job_id', 'evaluation_id', 'source_sha256', 'manifest_sha256', 'coverage', 'methods',
                'accounted', 'actual_core_hours', 'raw_output_status')
        context = {key: report[key] for key in keys}
        context.update(role='author_reference_A_human_only',
            analysis_role='human_author_reference_only', source_tables=tables,
            analysis_policy='Only read verified A evidence. No P answers, B conditions, author code, new analysis or HPC action.')
        if len(canonical(context)) > MAX_ASSISTANT_BYTES:
            raise ResultUnavailable('Reference analysis context is too large; no rows were silently omitted')
        return context
