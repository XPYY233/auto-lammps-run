"""Operator discoveries. Source claims never grant scientific/execution readiness."""
import json
from pathlib import Path
import re

from .manifest import sha256
from .runtime_launcher import read_regular
from .tasks import text


class DiscoveryLibrary:
    def __init__(self, source=None, reviews=None):
        self.source = Path(source) if source else None
        self.reviews = Path(reviews) if reviews else None

    @staticmethod
    def strings(values, limit=2000):
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError('Invalid discovery field')
        return [text(v, limit) for v in values]

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
                identity = repository.lower() + '@' + commit
                if identity in identities:
                    raise ValueError('Duplicate discovery identity')
                identities.add(identity)
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
                gaps = ['结构/依赖闭包待核验', '文件 SHA-256 与远端获取回执待核验', '尚无控制端运行验证']
                if not doi:
                    gaps.append('缺少论文 DOI')
                elif tier != 'tier_a':
                    gaps.append('论文题名与 DOI 关联待核验')
                if not elements:
                    gaps.append('元素与原子类型映射待补充')
                if not potential_files:
                    gaps.append('势函数文件或内建势参数依据待补充')
                if not input_files:
                    gaps.append('LAMMPS 入口文件待补充')
                license_name = text(row.get('license') or '未声明', 300)
                if license_name in {'NOASSERTION', '未声明', 'NONE'}:
                    gaps.append('许可证范围待核验')
                state = 'candidate_needs_verification'
                for review in review_rows:
                    if review['repository'].lower() != repository.lower() or review['commit'] != commit:
                        continue
                    if review['state'] not in {'conflict', 'missing_resources'}:
                        raise ValueError('Discovery review cannot grant execution readiness')
                    if state != 'conflict':
                        state = review['state']
                    gaps.extend(self.strings(review['notes']))
                entries.append(dict(id=sha256(identity.encode()), repository=repository, commit=commit,
                    title=text(row.get('title') or '题名待核验', 2000), doi=doi,
                    description=text(row.get('desc') or '', 4000, required=False),
                    evidence=text(row.get('evidence') or row.get('reason') or '', 4000, required=False),
                    source_tier=tier, state=state, execution_ready=False,
                    pair_styles=styles, elements=elements, potential_files=potential_files,
                    input_files=input_files, license=license_name, gaps=gaps, mirrors=mirrors,
                    url='https://github.com/'+repository+'/tree/'+commit))
        return dict(configured=True, source=dict(sha256=sha256(raw),
                    generated=text(document.get('generated') or '未记录', 100)),
                    repository_count=len(repositories), entries=entries, verified_execution_count=0)
