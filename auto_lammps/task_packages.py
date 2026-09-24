"""Deterministic operator drafts, never automatic clearance for Agent access.

Selection projects input values, not their semantic meaning. A value can itself
contain a hidden answer, URL or target script. These exports remain operator-only
until input/resource review and actual runtime isolation have been established.
"""
import json

from .manifest import canonical, sha256
from .tasks import ESSENTIAL, FIELDS, TaskError, candidate

GENERATOR_VERSION = 1
EXECUTION_FIELDS = tuple(key for key in FIELDS if key != 'reference')


def split_condition_record(content: bytes):
    """Project a frozen review into paired, reproducible private JSON exports."""
    try:
        record = json.loads(content)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise TaskError('条件记录无法读取') from exc
    if (not isinstance(record, dict) or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('purpose') != 'condition_review_record'
            or record.get('mode') not in ('reproduction', 'research')
            or record.get('execution_authorized') is not False
            or not isinstance(record.get('conditions'), dict)
            or set(record['conditions']) != set(FIELDS)):
        raise TaskError('需要完整的冻结条件审阅记录')

    inputs, mapping = {}, {}
    origins = set()
    for key, field in record['conditions'].items():
        if (not isinstance(field, dict) or field.get('confirmed') is not True
                or not isinstance(field.get('candidates'), list)
                or not isinstance(field.get('selected'), str)):
            raise TaskError('条件未确认，不能生成任务资料')
        choices = [item for item in field['candidates'] if isinstance(item, dict)
                   and item.get('id') == field['selected']]
        if len(choices) != 1:
            raise TaskError('所选条件缺失或不唯一')
        selected = choices[0]
        try:
            clean = candidate({name: selected[name] for name in (
                'value', 'unit', 'origin', 'source_locator', 'applicability', 'evidence_role')})
        except (KeyError, TypeError) as exc:
            raise TaskError('所选条件格式不完整') from exc
        if clean['applicability'] == 'not_applicable' and (
                key in ESSENTIAL or (key == 'reference' and record['mode'] == 'reproduction')):
            raise TaskError('必要条件不能标记为不适用')
        if key == 'reference':
            continue
        # Copy an allowlist only: no IDs, source metadata, alternatives,
        # original prompt, resolution notes or imported CSV context.
        inputs[key] = {name: clean[name] for name in ('value', 'unit', 'applicability')}
        mapping[key] = dict(candidate_id=selected['id'], origin=clean['origin'],
                            source_locator=clean['source_locator'],
                            input_sha256=sha256(canonical(inputs[key])))
        origins.add(clean['origin'])

    lines = ['请按以下已确认条件独立准备计算和分析方案。']
    for key in EXECUTION_FIELDS:
        item = inputs[key]
        value = item['value'] + (f"（单位：{item['unit']}）" if item['unit'] else '')
        if item['applicability'] == 'not_applicable':
            value = '不适用；理由：' + value
        lines.append(f'{FIELDS[key]}：{value}')
    draft = dict(schema_version=1, generator_version=GENERATOR_VERSION,
                 purpose='execution_task_draft', mode=record['mode'],
                 release_status='operator_review_required', execution_authorized=False,
                 input_semantics_verified=False, resources_verified=False,
                 runtime_isolation_verified=False, conditions=inputs,
                 task_text='\n'.join(lines), resources=[])
    draft_bytes = canonical(draft)
    reference = dict(schema_version=1, generator_version=GENERATOR_VERSION,
                     purpose='reference_preparation_record',
                     condition_record_sha256=sha256(content),
                     execution_draft_sha256=sha256(draft_bytes),
                     qualification_status='not_evaluated',
                     results={'P': None, 'A': None, 'B': None},
                     condition_origin='code_supplemented' if 'code' in origins else 'no_code_inputs',
                     input_mapping=mapping, condition_review_record=record)
    return {'execution': draft_bytes, 'reference': canonical(reference)}
