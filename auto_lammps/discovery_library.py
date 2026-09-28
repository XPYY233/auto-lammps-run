"""Operator discoveries. Source claims never grant scientific/execution readiness."""
import json
from pathlib import Path
import re

from .manifest import sha256
from .runtime_launcher import read_regular
from .tasks import text

KINDS = ('potential', 'author_source')
NAMES = {
    'Nb': '铌', 'Ti': '钛', 'Zr': '锆', 'Mo': '钼', 'V': '钒', 'Fe': '铁', 'Cu': '铜', 'Al': '铝',
    'W': '钨', 'Ta': '钽', 'Ni': '镍', 'Co': '钴', 'Cr': '铬', 'Si': '硅', 'C': '碳', 'O': '氧',
    'H': '氢', 'Na': '钠', 'Sn': '锡', 'Mg': '镁', 'Mn': '锰', 'Ga': '镓', 'Ge': '锗', 'N': '氮',
}


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
        """Elements with the Chinese name beside each symbol, as the potential
        entries already read; unknown symbols keep the symbol alone."""
        return ' · '.join(f'{e}（{NAMES[e]}）' if e in NAMES else e for e in elements)

    @classmethod
    def default_title(cls, kind, row, styles, elements):
        """Naming: potentials read `<element system> · <potential type>`, author
        sources read `<paper title> · <process>`. Handoff labels win when given."""
        if kind == 'potential':
            label = text(row.get('potential_label') or '', 300, required=False)
            if not label:
                system = '—'.join(elements) or '元素待核'
                family = ' / '.join(s.upper() for s in styles) or '类型待核'
                label = f'{system} · {family}'
            return label
        label = text(row.get('source_label') or '', 400, required=False)
        if label:
            return label
        title = text(row.get('title') or '题名待核验', 2000)
        process = '、'.join(cls.strings(row.get('process', []), 120)[:2])
        return f'{title} · {process}' if process else title

    @classmethod
    def gaps(cls, kind, tier, doi, elements, potential_files, input_files, license_name):
        rows = ['文件 SHA-256 与远端获取回执待核验', '尚无控制端运行验证']
        if kind == 'author_source':
            rows.insert(0, '结构/依赖闭包待核验')
        if not doi:
            rows.append('缺少论文 DOI')
        elif tier != 'tier_a':
            rows.append('论文题名与 DOI 关联待核验')
        if kind == 'potential':
            if not elements:
                rows.append('元素与原子类型映射待补充')
            if not potential_files:
                rows.append('势函数文件或内建势参数依据待补充')
        if kind == 'author_source' and not input_files:
            rows.append('LAMMPS 入口文件待补充')
        if license_name in {'NOASSERTION', '未声明', 'NONE'}:
            rows.append('许可证范围待核验')
        return rows

    def get(self):
        if self.source is None:
            return dict(configured=False, entries=[], repository_count=0, source=None)
        raw = read_regular(self.source, 2*1024*1024)
        document = json.loads(raw)
        review_rows = []
        if self.reviews:
            review = json.loads(read_regular(self.reviews, 262144))
            if review['version'] != 1:
                raise ValueError('Unknown discovery review format')
            review_rows = review['reviews']
        entries, repositories, identities = [], set(), set()
        for tier in ('tier_a', 'tier_b', 'tier_c'):
            rows = document[tier]
            if not isinstance(rows, list) or len(rows) > 2000:
                raise ValueError('Invalid discovery list')
            for row in rows:
                repository, commit = row['repo'], row['commit']
                if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
                        or not re.fullmatch(r'[0-9a-f]{40}', commit)):
                    raise ValueError('Unpinned or invalid discovery repository')
                repositories.add(repository.lower())
                mirrors = self.strings(row.get('mirrors', []), 300)
                for mirror in mirrors:
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', mirror):
                        raise ValueError('Invalid mirror repository')
                    repositories.add(mirror.lower())
                doi = row.get('doi') or row.get('candidate_doi') or ''
                if doi and not re.fullmatch(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]+', doi):
                    raise ValueError('Invalid candidate DOI')
                potential_files = self.strings(row.get('potentials', []))
                input_files = self.strings(row.get('inputs', []))
                styles = self.strings(row.get('pair_style', []), 120)
                elements = self.strings(row.get('elements', []), 8)
                if any(not re.fullmatch(r'[A-Z][a-z]?', e) for e in elements):
                    raise ValueError('Invalid claimed elements')
                license_name = text(row.get('license') or '未声明', 300)
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
                # One handoff row describes both halves of a bundle; each half is
                # published as its own entry so potentials read by element system
                # and author sources read by paper and process.
                for kind in KINDS:
                    if kind == 'potential' and not (potential_files or styles or elements):
                        continue
                    if kind == 'author_source' and not input_files:
                        continue
                    identity = repository.lower() + '@' + commit + '#' + kind
                    if identity in identities:
                        raise ValueError('Duplicate discovery identity')
                    identities.add(identity)
                    gaps = self.gaps(kind, tier, doi, elements, potential_files, input_files, license_name)
                    gaps.extend(review_notes)
                    entries.append(dict(id=sha256(identity.encode()), kind=kind,
                        repository=repository, commit=commit,
                        title=self.default_title(kind, row, styles, elements),
                        paper_title=text(row.get('title') or '题名待核验', 2000), doi=doi,
                        description=text(row.get('desc') or '', 4000, required=False),
                        evidence=text(row.get('evidence') or row.get('reason') or '', 4000, required=False),
                        source_tier=tier, state=state, execution_ready=False,
                        pair_styles=styles, elements=elements,
                        element_label=text(row.get('element_label') or self.element_label(elements), 600, required=False),
                        process=self.strings(row.get('process', []), 120),
                        potential_files=potential_files, input_files=input_files,
                        license=license_name, gaps=gaps, mirrors=mirrors,
                        url='https://github.com/'+repository+'/tree/'+commit))
        return dict(configured=True, source=dict(sha256=sha256(raw),
                    generated=text(document.get('generated') or '未记录', 100)),
                    repository_count=len(repositories), entries=entries, verified_execution_count=0)
