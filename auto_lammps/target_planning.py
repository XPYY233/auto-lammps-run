"""Reference-side target inventory and pre-execution scope, never B input.

The trusted literature adapter supplies assessed inventory. Validation checks its
structure and provenance bindings, not scientific truth. No provider or physics
is invoked here. A frozen historical task cannot be retroactively preregistered.
"""
from copy import deepcopy
import re

from .manifest import canonical, sha256
from .tasks import TaskError, text

AVAILABILITY = {'retained_data', 'new_calculation', 'missing_resources', 'not_simulation', 'unresolved'}


def inventory(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'paper', 'source_sha256', 'coverage_note', 'targets'}
            or type(value['version']) is not int or value['version'] != 1):
        raise TaskError('目标清单格式不完整')
    result = deepcopy(value)
    paper = result['paper']
    if not isinstance(paper, dict) or set(paper) != {'title', 'doi'}:
        raise TaskError('目标清单需要论文完整题名与 DOI')
    paper['title'] = text(paper['title'], 1000)
    paper['doi'] = text(paper['doi'], 300)
    if not re.fullmatch(r'10\.\d{4,9}/\S+', paper['doi']):
        raise TaskError('论文 DOI 格式无效')
    if not isinstance(value['source_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['source_sha256']):
        raise TaskError('目标清单需要文献提取来源摘要')
    result['coverage_note'] = text(value['coverage_note'], 4000)
    targets = result['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 256:
        raise TaskError('目标清单应包含 1 至 256 个条目')
    ids = set()
    required = {'id', 'label', 'parent', 'locator', 'kind', 'condition_group', 'conditions',
                'resources', 'outputs', 'sampling', 'analysis', 'criterion', 'availability', 'limitations'}
    for row in targets:
        if not isinstance(row, dict) or set(row) != required:
            raise TaskError('图表目标缺少工况、资源、输出或分析依据')
        for key in required - {'availability', 'kind'}:
            row[key] = text(row[key], 4000 if key not in {'id', 'parent', 'condition_group'} else 160,
                            required=key not in {'criterion'})
        if row['id'] in ids:
            raise TaskError('目标标识重复')
        ids.add(row['id'])
        if row['availability'] not in AVAILABILITY or row['kind'] not in {'figure', 'subfigure', 'table', 'supplement'}:
            raise TaskError('目标类型或可用性无效')
    return result


def selected_plan(document, selected_ids, exclusion_reason):
    source = document.get('target_inventory')
    if source is None:
        raise TaskError('请先完成整篇图表与工况识别，再选择复现目标')
    source = inventory(source)
    if (not isinstance(selected_ids, list) or not selected_ids or not all(isinstance(x, str) for x in selected_ids)
            or len(selected_ids) != len(set(selected_ids))):
        raise TaskError('请至少选择一个目标，且不能重复')
    by_id = {r['id']: r for r in source['targets']}
    if set(selected_ids) - by_id.keys():
        raise TaskError('所选目标不在当前清单中')
    excluded = [r['id'] for r in source['targets'] if r['id'] not in selected_ids]
    reason = text(exclusion_reason, 2000, required=bool(excluded))
    # Ordered by inventory, not click order; a stable digest binds all exclusions.
    selected = [r for r in source['targets'] if r['id'] in selected_ids]
    return dict(version=1, inventory_sha256=sha256(canonical(source)),
                selected_ids=[r['id'] for r in selected], excluded_ids=excluded,
                exclusion_reason=reason, condition_groups=sorted({r['condition_group'] for r in selected}),
                submission_rule='B_max_2_per_frozen_evaluation',
                sharing_rule='same_condition_outputs_share_one_workflow_no_hidden_retries')


def freeze_plan(document):
    plan = document.get('target_selection')
    if not plan:
        raise TaskError('尚未选定图表目标和范围，不能冻结复现任务')
    current = selected_plan(document, plan['selected_ids'], plan['exclusion_reason'])
    if plan != current:
        raise TaskError('图表清单已更新，请重新选择并确认范围')
    rows = [r for r in document['target_inventory']['targets'] if r['id'] in plan['selected_ids']]
    if any(r['availability'] in {'missing_resources', 'not_simulation', 'unresolved'} for r in rows):
        raise TaskError('所选目标仍有资源或方法缺项，不能冻结')
    if any(not r['criterion'].strip() for r in rows):
        raise TaskError('所选目标的比较标准尚未明确，不能冻结')
    # One TaskStore condition record describes exactly one physical workflow.
    # Multi-condition planning stays visible but must be split before this gate.
    if len(plan['condition_groups']) != 1:
        raise TaskError('所选目标跨越多个工况；请先按工况拆分任务并明确计数')
    return dict(**current, inventory=document['target_inventory'],
                preregistration='before_new_task_freeze', scientific_validation='not_performed')
