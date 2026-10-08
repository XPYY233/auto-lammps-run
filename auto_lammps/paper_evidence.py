"""Human-only views of existing literature workbench artifacts, never B inputs.

This adapter validates registered files and paper identity. It does not extract a
paper, authorize physics, verify scientific truth or create a retrospective plan.
"""
import json
from pathlib import Path
import re

from . import runtime_launcher as runtime
from .closeout import evidence_views
from .manifest import sha256
from .results import existing_private_directory, ResultUnavailable
from .target_planning import inventory
from .tasks import task_id, text


class PaperEvidenceViews:
    def __init__(self, directory, papers):
        self.directory = existing_private_directory(directory)
        self.papers = papers

    def _load(self, identifier):
        task_id(identifier)
        self.papers.tasks.get(identifier)
        folder = self.directory / identifier / 'paper-evidence'
        if not folder.exists():
            return None, None
        existing_private_directory(folder)
        raw = runtime.read_regular(folder / 'manifest.json', 262144, private=True)
        document = json.loads(raw)
        if (document.get('version') != 1 or document.get('task_id') != identifier
                or document.get('role') != 'paper_evidence_human_only'):
            raise ResultUnavailable('Invalid paper evidence identity')
        paper = self.papers.get(document['paper_id'])
        if (document['title'] != paper['title'] or document['doi'] != paper['doi']
                or identifier not in [t['id'] for t in paper['tasks']]):
            raise ResultUnavailable('Paper evidence belongs to another task or paper')
        source = document['source_sha256']
        if not isinstance(source, str) or not re.fullmatch('[a-f0-9]{64}', source):
            raise ResultUnavailable('Missing workbench provenance digest')
        files = document['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 128:
            raise ResultUnavailable('Invalid paper evidence files')
        data, total, public_files = {}, 0, []
        for item in files:
            name = item['name']
            if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', name)
                    or name in data or Path(name).suffix not in {'.png', '.jpg', '.jpeg', '.csv', '.pdf', '.md', '.json'}):
                raise ResultUnavailable('Invalid paper artifact filename')
            content = runtime.read_regular(folder / name, 5 * 1024 * 1024, private=True)
            total += len(content)
            if (total > 32 * 1024 * 1024 or type(item['size']) is not int
                    or len(content) != item['size'] or sha256(content) != item['sha256']):
                raise ResultUnavailable('Paper artifact changed')
            data[name] = content
            if name != document.get('source_receipt_file'):
                public_files.append(dict(name=name, label=text(item['label'], 300), size=len(content)))
        receipt_name = document['source_receipt_file']
        if receipt_name not in data or not receipt_name.endswith('.json') or sha256(data[receipt_name]) != source:
            raise ResultUnavailable('Workbench source receipt changed')
        receipt = json.loads(data[receipt_name])
        if (receipt.get('paper', {}).get('title') != paper['title']
                or receipt.get('paper', {}).get('doi', '').lower() != paper['doi'].lower()):
            raise ResultUnavailable('Workbench source receipt belongs to another paper')
        figures = document['figures']
        if not isinstance(figures, list) or len(figures) > 64:
            raise ResultUnavailable('Invalid paper figures')
        seen, public_figures = set(), []
        for figure in figures:
            name = figure['name']
            if name not in data or name in seen or Path(name).suffix not in {'.png', '.jpg', '.jpeg'}:
                raise ResultUnavailable('Undeclared paper figure')
            seen.add(name)
            public_figures.append(dict(name=name, label=text(figure['label'], 300),
                caption=text(figure['caption'], 4000), kind='plot'))
        # Reuse the same bounded CSV preview and role/group validation as the
        # result comparison viewer; no second literature parser is introduced.
        views = evidence_views(document, data, public_figures, [], allow_missing_cells=True)
        if any(item['role'] != 'paper' for view in views for item in view['figures'] + view['tables']):
            raise ResultUnavailable('Paper package cannot claim calculation results')
        source_inventory = document.get('target_inventory')
        if source_inventory is not None:
            source_inventory = inventory(source_inventory)
            if (source_inventory['paper'] != dict(title=paper['title'], doi=paper['doi'])
                    or source_inventory['source_sha256'] != source):
                raise ResultUnavailable('Target inventory has another provenance')
        priorities = document.get('priorities', {})
        ids = {row['id'] for row in (source_inventory or {}).get('targets', [])}
        if (not isinstance(priorities, dict) or set(priorities) - ids
                or any(type(v) is not int or not 1 <= v <= 3 for v in priorities.values())):
            raise ResultUnavailable('Invalid target priorities')
        public = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'], doi=paper['doi'],
            source_sha256=source, source_note=text(document['source_note'], 4000),
            limitations=[text(v, 4000) for v in document['limitations']],
            figures=public_figures, files=public_files, views=views, target_inventory=source_inventory,
            priorities=priorities, scientific_status='not_evaluated', execution_authorized=False,
            manifest_sha256=sha256(raw))
        return public, data

    def get(self, identifier):
        return self._load(identifier)[0]

    def download(self, identifier, name):
        report, data = self._load(identifier)
        if report is None or name not in {f['name'] for f in report['files']}:
            raise ResultUnavailable('No declared paper artifact')
        return data[name]
