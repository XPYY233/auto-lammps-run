"""Operator discoveries. Source claims never grant scientific/execution readiness."""
import json
from pathlib import Path
import re

from .manifest import sha256
from .runtime_launcher import read_regular
from .tasks import text

KINDS = ('potential', 'author_source')
ROLES = ('author_source', 'validation_tests', 'example_suite', 'artifact', 'potential_library')
SOURCE_TYPES = ('github', 'openkim', 'nist_ipr')
GITHUB = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
VERSION = re.compile(r'[A-Za-z0-9_.:/+~-]{1,120}')
NAMES = {
    'Nb': '铌', 'Ti': '钛', 'Zr': '锆', 'Mo': '钼', 'V': '钒', 'Fe': '铁', 'Cu': '铜', 'Al': '铝',
    'W': '钨', 'Ta': '钽', 'Ni': '镍', 'Co': '钴', 'Cr': '铬', 'Si': '硅', 'C': '碳', 'O': '氧',
    'H': '氢', 'Na': '钠', 'Sn': '锡', 'Mg': '镁', 'Mn': '锰', 'Ga': '镓', 'Ge': '锗', 'N': '氮',
}
ARTIFACT_DOI = re.compile(r'^10\.(11578|5281|48550)/|/supp-|/data\d*$', re.I)


class DiscoveryLibrary:
    def __init__(self, source=None, reviews=None):
        self.source = Path(source) if source else None
        self.reviews = Path(reviews) if reviews else None

    @staticmethod
    def strings(values, limit=2000):
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError('Invalid discovery field')
        return [text(v, limit) for v in values]

    @staticmethod
    def element_label(elements):
        return ' · '.join(f'{e}（{NAMES[e]}）' if e in NAMES else e for e in elements)

    @staticmethod
    def doi_state(tier, doi, declared):
        """A candidate or artifact DOI must never read as a verified paper DOI."""
        if not doi:
            return 'absent'
        if ARTIFACT_DOI.search(doi):
            return 'artifact_or_dataset'
        if declared == 'verified' or tier == 'tier_a':
            return 'verified_association'
        return 'candidate_unverified'

    @classmethod
    def default_title(cls, kind, row, styles, elements, potential_files, basis):
        if kind == 'potential':
            label = text(row.get('potential_label') or '', 300, required=False)
            if label:
                return label
            if basis == 'built_in_analytic' and not potential_files:
                family = ' / '.join(s.upper() for s in styles) or '类型待核'
                return f'内建解析势 {family} · 参数依据待补'
            system = '—'.join(elements) or '元素待核'
            family = ' / '.join(s.upper() for s in styles) or '类型待核'
            return f'{system} · {family}'
        label = text(row.get('source_label') or '', 400, required=False)
        if label:
            return label
        title = text(row.get('title') or '题名待核验', 2000)
        process = '、'.join(cls.strings(row.get('process', []), 120)[:2])
        return f'{title} · {process}' if process else title

    @staticmethod
    def gaps(kind, doi_state, elements, complete, potential_files, input_files, license_name):
        rows = ['文件 SHA-256 与远端获取回执待核验', '尚无控制端运行验证']
        if kind == 'author_source':
            rows.insert(0, '结构/依赖闭包待核验')
        if doi_state == 'absent':
            rows.append('缺少论文 DOI')
        elif doi_state != 'verified_association':
            rows.append('论文题名与 DOI 关联待核验')
        if doi_state == 'artifact_or_dataset':
            rows.append('所附 DOI 属 artifact/数据集，不能作为论文 DOI')
        if kind == 'potential':
            if not elements:
                rows.append('元素与原子类型映射待补充')
            elif not complete:
                rows.append('元素映射证据被截断，未构成完整映射')
            if not potential_files:
                rows.append('势函数文件或内建势参数依据待补充')
        if kind == 'author_source' and not input_files:
            rows.append('LAMMPS 入口文件待补充')
        if license_name in {'NOASSERTION', '未声明', 'NONE'}:
            rows.append('许可证范围待核验')
        return rows

    def _resolve_source(self, row):
        """GitHub rows keep repo@commit; external registries carry a version."""
        repository, commit = row['repo'], row.get('commit') or ''
        origin = row.get('source') or {}
        source_type = text(origin.get('type') or 'github', 40)
        if source_type not in SOURCE_TYPES:
            raise ValueError('Unknown discovery source type')
        if source_type == 'github':
            if not GITHUB.fullmatch(repository) or not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise ValueError('Unpinned or invalid discovery repository')
            return source_type, repository, commit, 'https://github.com/'+repository+'/tree/'+commit
        locator = text(origin.get('locator') or repository, 200)
        raw_version = origin.get('version') or commit
        if not raw_version:
            raise ValueError('External discovery source needs a pinned version')
        version = text(raw_version, 200)
        if not VERSION.fullmatch(version):
            raise ValueError('External discovery source needs a pinned version')
        source_url = text(row.get('source_url') or '', 500, required=False)
        if source_url and not source_url.startswith('https://'):
            raise ValueError('Invalid external source URL')
        return source_type, locator, version, source_url

    def get(self):
        if self.source is None:
            return dict(configured=False, entries=[], row_count=0, bundle_count=0, repository_count=0,
                        paper_doi_count=0, verified_doi_count=0, source=None)
        raw = read_regular(self.source, 2*1024*1024)
        document = json.loads(raw)
        review_rows = []
        if self.reviews:
            review = json.loads(read_regular(self.reviews, 262144))
            if review['version'] != 1:
                raise ValueError('Unknown discovery review format')
            review_rows = review['reviews']
        entries, repositories, identities = [], set(), set()
        bundles, dois, verified = set(), set(), set()
        for tier in ('tier_a', 'tier_b', 'tier_c'):
            rows = document[tier]
            if not isinstance(rows, list) or len(rows) > 2000:
                raise ValueError('Invalid discovery list')
            for row in rows:
                source_type, repository, commit, url = self._resolve_source(row)
                role = text(row.get('repository_role') or 'author_source', 40)
                if role not in ROLES:
                    raise ValueError('Unknown repository role')
                bundle_key = f'{source_type}:{repository.lower()}@{commit}'
                repositories.add(repository.lower())
                for mirror in self.strings(row.get('mirrors', []), 300):
                    if not GITHUB.fullmatch(mirror):
                        raise ValueError('Invalid mirror repository')
                    repositories.add(mirror.lower())
                bundles.add(bundle_key)
                doi = row.get('doi') or row.get('candidate_doi') or ''
                if doi and not re.fullmatch(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]+', doi):
                    raise ValueError('Invalid candidate DOI')
                declared = text(row.get('doi_declared') or '', 40, required=False)
                if declared and declared not in ('verified', 'candidate', 'artifact'):
                    raise ValueError('Unknown DOI declaration')
                state_doi = self.doi_state(tier, doi, declared)
                if state_doi == 'verified_association':
                    verified.add(doi.lower())
                if doi and state_doi != 'artifact_or_dataset':
                    dois.add(doi.lower())
                potential_files = self.strings(row.get('potentials', []))
                input_files = self.strings(row.get('inputs', []))
                styles = self.strings(row.get('pair_style', []), 120)
                elements = self.strings(row.get('elements', []), 8)
                if any(not re.fullmatch(r'[A-Z][a-z]?', e) for e in elements):
                    raise ValueError('Invalid claimed elements')
                library_elements = self.strings(row.get('potential_library_elements', []), 24)
                type_order = self.strings(row.get('type_order', []), 24)
                complete = bool(row.get('elements_complete'))
                basis = text(row.get('potential_basis') or
                             ('external_files' if potential_files else
                              'built_in_analytic' if styles else 'unknown'), 40)
                if basis not in ('external_files', 'built_in_analytic', 'unknown'):
                    raise ValueError('Unknown potential basis')
                license_name = text(row.get('license') or '未声明', 300)
                declared_kind = text(row.get('kind') or '', 40, required=False)
                if declared_kind and declared_kind not in KINDS:
                    raise ValueError('Unknown discovery kind')
                state = 'candidate_needs_verification'
                review_notes = []
                for review in review_rows:
                    if review['repository'].lower() != repository.lower() or review['commit'] != commit:
                        continue
                    if review['state'] not in {'conflict', 'missing_resources'}:
                        raise ValueError('Discovery review cannot grant execution readiness')
                    if state != 'conflict':
                        state = review['state']
                    review_notes.extend(self.strings(review['notes']))
                for kind in KINDS:
                    if declared_kind and declared_kind != kind:
                        continue
                    if kind == 'potential' and not (potential_files or styles or elements):
                        continue
                    if kind == 'author_source' and not input_files:
                        continue
                    identity = bundle_key + '#' + kind
                    if identity in identities:
                        raise ValueError('Duplicate discovery identity')
                    identities.add(identity)
                    gaps = self.gaps(kind, state_doi, elements, complete,
                                     potential_files, input_files, license_name)
                    gaps.extend(review_notes)
                    entries.append(dict(id=sha256(identity.encode()), bundle_id=sha256(bundle_key.encode()),
                        kind=kind, repository=repository, commit=commit, source_type=source_type,
                        repository_role=role,
                        title=self.default_title(kind, row, styles, elements, potential_files, basis),
                        paper_title=text(row.get('title') or '题名待核验', 2000),
                        doi=doi, doi_state=state_doi,
                        description=text(row.get('desc') or '', 4000, required=False),
                        evidence=text(row.get('evidence') or row.get('reason') or '', 4000, required=False),
                        source_tier=tier, state=state, execution_ready=False,
                        pair_styles=styles, elements=elements,
                        element_label=text(row.get('element_label') or self.element_label(elements), 600, required=False),
                        potential_library_elements=library_elements, type_order=type_order,
                        elements_complete=complete, potential_basis=basis,
                        process=self.strings(row.get('process', []), 120),
                        potential_files=potential_files, input_files=input_files,
                        license=license_name, gaps=gaps, mirrors=self.strings(row.get('mirrors', []), 300),
                        url=url))
        return dict(configured=True, source=dict(sha256=sha256(raw),
                    generated=text(document.get('generated') or '未记录', 100)),
                    row_count=len(entries), bundle_count=len(bundles), repository_count=len(repositories),
                    paper_doi_count=len(dois), verified_doi_count=len(verified),
                    entries=entries, verified_execution_count=0)
