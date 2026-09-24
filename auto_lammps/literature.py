"""Data-format adapter only; no upstream code, network or source execution."""
import csv
import io
import re

from .manifest import sha256
from .tasks import FIELDS, TaskError, candidate, text

MAX_CSV_BYTES = 65536
METHODS = {
    'lammps_direct': 'LAMMPS 直接结果',
    'lammps_postprocessed': 'LAMMPS 结果经后处理',
    'other': '其他方法',
    'unclear': '来源不明确',
}
CONDITION_FIELDS = tuple(key for key in FIELDS if key not in {
    'scope', 'reference', 'material', 'quantity', 'analysis', 'resources'})
MAPPINGS = {'material': ('material',), 'conditions': CONDITION_FIELDS,
            'conditions_text': CONDITION_FIELDS}
REQUIRED_COLUMNS = {'source_scope', 'source_id', 'entity_type', 'entity_uid',
                    'doi', 'article_title', 'source_locator', 'source_page'}


def preview_csv(content):
    """One UTF-8 export row. Preserve text, including spreadsheet protection.

    A format match is not authentication or scientific verification. Output
    values and captions remain operator-only context, never automatic inputs.
    """
    if not isinstance(content, str) or '\x00' in content:
        raise TaskError('请提供 UTF-8 CSV 文本')
    try:
        raw = content.encode('utf-8')
    except UnicodeError:
        raise TaskError('CSV 编码无效') from None
    if not raw or len(raw) > MAX_CSV_BYTES:
        raise TaskError('单条文献导出文件必须小于等于 64 KiB')
    try:
        rows = list(csv.reader(io.StringIO(content.lstrip('\ufeff'), newline=''), strict=True))
    except csv.Error:
        raise TaskError('CSV 引号或字段格式无效') from None
    if len(rows) != 2:
        raise TaskError('每次导入一条证据：需要表头和一条数据行')
    names, values = rows
    if (len(names) > 50 or len(names) != len(values) or len(set(names)) != len(names)
            or any(not re.fullmatch('[a-z][a-z0-9_]{0,63}', name) for name in names)
            or not REQUIRED_COLUMNS.issubset(names)):
        raise TaskError('CSV 列缺失、重复或格式不兼容')
    row = dict(zip(names, values))
    if (row['source_scope'] not in {'workspace', 'official', 'private'}
            or row['entity_type'] not in {'item', 'finding', 'table', 'figure'}
            or not row['source_id'].strip() or not row['entity_uid'].strip()
            or not (row['doi'].strip() or row['article_title'].strip())):
        raise TaskError('导出缺少文献或证据条目标识')
    return dict(schema_version=1, format='auto_research_evidence_csv',
                source_sha256=sha256(raw), row=row,
                available_columns={key: {field: FIELDS[field] for field in fields}
                                   for key, fields in MAPPINGS.items() if row.get(key, '').strip()},
                method_classes=METHODS, scientific_validation='not_performed')


def input_from_csv(content, *, source_sha256, column, field, evidence_role,
                   method_class, classification_basis):
    source = preview_csv(content)
    if source_sha256 != source['source_sha256']:
        raise TaskError('来源文件与预览不一致，请重新预览')
    if evidence_role != 'input':
        raise TaskError('只有明确核对为输入的条件可以加入任务；结果或未知用途不能导入')
    if not isinstance(column, str) or column not in MAPPINGS or field not in MAPPINGS[column]:
        raise TaskError('此来源列不能映射到所选输入条件')
    if not isinstance(method_class, str) or method_class not in METHODS:
        raise TaskError('请选择此证据条目的方法来源，不能沿用整篇论文的分类')
    basis = text(classification_basis, 2000)
    row = source['row']
    position = row['source_locator'].strip() or ('页码 '+row['source_page'].strip() if row['source_page'].strip() else '')
    if not position:
        raise TaskError('来源缺少页码或原文位置，请先在文献工作台补齐')
    if not any(row.get(key, '').strip() for key in ('source_excerpt', 'caption', 'source_context')):
        raise TaskError('来源缺少原文、图表注或上下文，无法核对输入条件')
    reference = row['doi'].strip() or row['article_title'].strip()
    choice = candidate(dict(value=row.get(column, ''), unit='', origin='paper',
                            source_locator=reference+' · '+position,
                            applicability='required', evidence_role='input'))
    choice['literature_source'] = dict(schema_version=1, source_sha256=source['source_sha256'],
                                     column=column, method_class=method_class,
                                     classification_basis=basis,
                                     classification_status='operator_declared_not_verified')
    # The entire row may contain target results: store only in the operator area.
    snapshot = dict(format=source['format'], csv_text=content, row=row)
    return choice, snapshot
