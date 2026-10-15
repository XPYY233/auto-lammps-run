"""Bind configured catalog identities to a paper, without acquiring or running.

Catalog declarations remain declarations. This reference-only bridge records
which exact existing rows were selected; it grants no license, source-read,
engine, execution or scientific approval.
"""
from copy import deepcopy
import re

from .discovery_library import DiscoveryLibrary
from .manifest import canonical, sha256
from .papers import doi_text
from .source_discovery import repository_name, title_words
from .tasks import TaskError, task_id

VERSION = 1
FLAGS = dict(scope='reference_only', role='author_reference_human_only',
             author_identity_verified=False, scientific_validation=False,
             execution_authorized=False, execution_ready=False, source_ready=False,
             B_input=False)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise TaskError('目录或条目摘要无效。')
    return value


def catalog_association(paper, discovery_library, entry_ids, *, catalog_sha256):
    """Re-read the configured library; browser-supplied repositories are absent."""
    if not isinstance(discovery_library, DiscoveryLibrary):
        raise TaskError('需要服务端配置的真实资源目录。')
    task_id(paper['id'])
    if (not isinstance(entry_ids, list) or len(entry_ids) != 2
            or len(set(_digest(item) for item in entry_ids)) != 2):
        raise TaskError('请选择同一固定仓库的作者源码与势函数两个条目。')
    catalog = discovery_library.get()
    expected = _digest(catalog_sha256)
    if not catalog.get('configured') or (catalog.get('source') or {}).get('sha256') != expected:
        raise TaskError('资源目录已变化或未配置；请刷新后重新选择。')
    selected = [deepcopy(row) for row in catalog['entries'] if row['id'] in entry_ids]
    if len(selected) != 2 or {row['id'] for row in selected} != set(entry_ids):
        raise TaskError('所选条目不属于当前配置目录。')
    if {row['kind'] for row in selected} != {'author_source', 'potential'}:
        raise TaskError('需要作者源码与势函数两种配套条目。')
    for row in selected:
        if (row['doi_state'] != 'verified_association' or doi_text(row['doi']) != doi_text(paper['doi'])
                or title_words(row['paper_title']) != title_words(paper['title'])):
            raise TaskError('目录完整题名与论文 DOI 未精确核对，不能关联。')
        if row['source_type'] != 'github' or row['state'] != 'candidate_needs_verification':
            raise TaskError('所选目录来源或冲突状态不支持固定作者仓库关联。')
        if row['kind'] == 'author_source' and row['repository_role'] != 'author_source':
            raise TaskError('示例、验证或数据仓库不能冒充作者原始源码。')
        if row['kind'] == 'potential' and row['repository_role'] not in {'author_source', 'potential_library'}:
            raise TaskError('所选势函数条目的仓库用途尚未核对。')
    source = next(row for row in selected if row['kind'] == 'author_source')
    if (any(row['bundle_id'] != source['bundle_id'] or row['repository'] != source['repository']
            or row['commit'] != source['commit'] for row in selected)
            or not re.fullmatch('[a-f0-9]{40}', source['commit'])):
        raise TaskError('源码与势函数必须属于同一明确固定 repo@commit。')
    repository_name(source['repository'])
    selected.sort(key=lambda row: row['id'])
    missing = list(dict.fromkeys(gap for row in selected for gap in row['gaps']))
    missing.append('目录关联不包含许可证使用、再分发或执行批准。')
    body = dict(schema_version=VERSION, **FLAGS, paper_id=paper['id'],
        paper=dict(title=paper['title'], doi=doi_text(paper['doi'])),
        source_kind='discovery_catalog', catalog_sha256=expected,
        catalog_snapshot_sha256=sha256(canonical(catalog)),
        entry_ids=sorted(entry_ids), entries=selected, bundle_id=source['bundle_id'],
        repository=source['repository'], commit=source['commit'], source_type='github',
        license=dict(status='declared_scope_unverified',
            claims=[dict(entry_id=row['id'], declared=row['license']) for row in selected],
            use_approved=False, redistribution_approved=False, execution_approved=False),
        missing=missing)
    digest = sha256(canonical(body))
    return dict(id=digest[:32], association_sha256=digest, **body)


def validate_catalog_association(paper, report, discovery_library):
    """An unchanged recorded association must still match today's trusted rows."""
    if not isinstance(report, dict) or len(canonical(report)) > 100000:
        raise TaskError('目录关联凭证不完整或超限。')
    current = catalog_association(paper, discovery_library, report.get('entry_ids'),
                                  catalog_sha256=report.get('catalog_sha256'))
    if current != report:
        raise TaskError('目录关联凭证、固定版本或审查记录已变化；保留旧记录，请重新核对。')
    return deepcopy(current)


def legacy_reference_candidates(paper):
    """Keep previously verified source-discovery selection semantics unchanged."""
    return [deepcopy(row) for row in paper.get('source_discovery', {}).get('candidates', [])
            if row.get('association') == 'doi_and_title']


def reference_resource_candidate(paper, *, discovery_library=None):
    """Choose an explicit current catalog binding, otherwise the legacy source."""
    stored = paper.get('resource_catalog_association')
    if stored is not None and discovery_library is not None:
        report = validate_catalog_association(paper, stored, discovery_library)
        return dict(repository=report['repository'], commit=report['commit'],
                    association='catalog_doi_and_title', association_id=report['id'])
    candidates = legacy_reference_candidates(paper)
    return candidates[0] if len(candidates) == 1 else None


def reference_resource_matches(paper, report, *, discovery_library=None):
    """No catalog-only receipt is accepted without fresh trusted validation."""
    if any(row.get('repository') == report.get('repository') and row.get('commit') == report.get('commit')
           for row in legacy_reference_candidates(paper)):
        return True
    stored = paper.get('resource_catalog_association')
    if stored is None or discovery_library is None:
        return False
    current = validate_catalog_association(paper, stored, discovery_library)
    return all(current[key] == report.get(key) for key in ('repository', 'commit'))


class ReferenceResourceAssociation:
    """Trusted controller service; all arguments select existing catalog IDs."""
    def __init__(self, papers, discovery_library):
        if not isinstance(discovery_library, DiscoveryLibrary):
            raise ValueError('Configured DiscoveryLibrary required')
        self.papers, self.library = papers, discovery_library

    def associate(self, paper_id, entry_ids, *, catalog_sha256):
        paper = self.papers.get(paper_id)
        report = catalog_association(paper, self.library, entry_ids, catalog_sha256=catalog_sha256)
        self.papers.record_catalog_association(paper_id, report, discovery_library=self.library)
        return self.get(paper_id)

    def get(self, paper_id):
        paper = self.papers.get(paper_id)
        report = paper.get('resource_catalog_association')
        return validate_catalog_association(paper, report, self.library) if report is not None else None
