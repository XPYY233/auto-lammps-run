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
        if not isinstance(row['availability'], str) or not isinstance(row['kind'], str) or row['availability'] not in AVAILABILITY or row['kind'] not in {'figure', 'subfigure', 'table', 'supplement'}:
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
                conditions_sha256=sha256(canonical(document['fields'])),
                selected_ids=[r['id'] for r in selected], excluded_ids=excluded,
                exclusion_reason=reason, condition_groups=sorted({r['condition_group'] for r in selected}),
                submission_rule='B_max_2_per_frozen_evaluation',
                sharing_rule='same_condition_outputs_share_one_workflow_no_hidden_retries')


def selection_readiness(document, selected_ids, exclusion_reason):
    """Read-only projection of the same rules used by the freezer, no authority grant."""
    from .tasks import issues
    source = document.get('target_inventory')
    blockers=[]
    if source is None:
        return dict(selected_count=0,total_count=0,groups=[],blockers=[dict(code='inventory_missing',message='文献图表清单尚未整理完成',target_ids=[])],
                    selection_saved=False,can_freeze=False,execution_authorized=False)
    source=inventory(source)
    if not isinstance(selected_ids,list) or not all(isinstance(x,str) for x in selected_ids) or len(selected_ids)!=len(set(selected_ids)):
        raise TaskError('目标选择格式无效或重复')
    by_id={r['id']:r for r in source['targets']}
    if set(selected_ids)-by_id.keys(): raise TaskError('所选目标不在当前清单中')
    rows=[r for r in source['targets'] if r['id'] in selected_ids]
    def add(code,message,targets=()):
        blockers.append(dict(code=code,message=message,target_ids=list(targets)))
    if not rows: add('selection_missing','请至少选择一个图表目标')
    excluded=[r['id'] for r in source['targets'] if r['id'] not in selected_ids]
    reason=text(exclusion_reason,2000,required=False)
    if excluded and not reason.strip(): add('scope_missing','请说明本次范围和未选目标的原因')
    names={'missing_resources':'资源仍有缺项','not_simulation':'属于资料核对项目，不能计为模拟复现','unresolved':'方法或工况尚未明确'}
    for row in rows:
        if row['availability'] in names:
            add('target_unavailable',row['label']+'：'+names[row['availability']],[row['id']])
        if not row['criterion'].strip():
            add('criterion_missing',row['label']+'：比较标准尚未明确',[row['id']])
    groups=[]
    for key in sorted({r['condition_group'] for r in rows}):
        matches=[r for r in rows if r['condition_group']==key]
        groups.append(dict(id=key,target_ids=[r['id'] for r in matches],labels=[r['label'] for r in matches],
                           conditions=list(dict.fromkeys(r['conditions'] for r in matches))))
    if len(groups)>1:add('multiple_conditions','所选目标跨越多个工况，请按工况分别规划；不能按图数重复提交')
    pending=issues(document)
    if pending:add('conditions_incomplete',f'研究条件还有 {len(pending)} 项缺失、矛盾或未确认')
    candidate=(selected_plan(document,selected_ids,reason) if rows and (not excluded or reason.strip()) else None)
    saved=candidate is not None and document.get('target_selection')==candidate
    return dict(selected_count=len(rows),total_count=len(source['targets']),excluded_count=len(excluded),
                groups=groups,blockers=blockers,selection_saved=saved,
                can_freeze=not blockers and saved and document.get('status')!='conditions_frozen',
                execution_authorized=False)


def freeze_plan(document):
    plan = document.get('target_selection')
    if not plan:
        raise TaskError('尚未选定图表目标和范围，不能冻结复现任务')
    state=selection_readiness(document,plan['selected_ids'],plan['exclusion_reason'])
    if not state['selection_saved']:
        raise TaskError('图表清单或任务条件已更新，请重新选择并确认范围')
    if state['blockers']:
        raise TaskError('；'.join(item['message'] for item in state['blockers']))
    return dict(**plan, inventory=document['target_inventory'],
                preregistration='before_new_task_freeze', scientific_validation='not_performed')
