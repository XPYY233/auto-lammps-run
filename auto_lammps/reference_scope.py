"""User-selected, append-only P scopes; no planning, models, physics or grants.

The target service's read-only ``_stored``/``_input`` are deliberately reused:
its public projection omits the original input and model binding. No caller may
supply scientific contents. Historical target contracts are verified from their
retained messages/proof, not replaced with today's adapter during selection.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .deepseek import DeepSeekConfig, request_body
from .ledger import LedgerError
from .manifest import canonical, sha256
from .reference_targets import STAGE, reference_target_input, validate_target_analysis
from .scientific_adapters import MANDATORY_INSTRUCTION
from .tasks import StaleTask, TaskError, task_id, text


VERSION = 1
FLAGS = dict(source_scope='reference_only', role='reference_scope_human_only', B_input=False,
             execution_authorized=False, scientific_status='not_evaluated',
             classification_status='model_proposed_not_verified',
             selection_status='user_selected_A_plan_approval_required')


class ReferenceScopeUnavailable(TaskError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise TaskError('参考范围的来源摘要无效。')
    return value


def _closure(report, selected_ids, exclusion_reason):
    """Select existing targets and their upstream DAG once, without inference."""
    by_target = {row['target_id']: row for row in report['targets']}
    if (not isinstance(selected_ids, list) or not selected_ids
            or any(not isinstance(item, str) for item in selected_ids)
            or len(selected_ids) != len(set(selected_ids)) or set(selected_ids) - by_target.keys()):
        raise TaskError('请选择本次核定清单中的目标，不能重复或新增目标。')
    selected = [row['target_id'] for row in report['targets'] if row['target_id'] in selected_ids]
    excluded = [row['target_id'] for row in report['targets'] if row['target_id'] not in selected_ids]
    reason = text(exclusion_reason, 2000, required=bool(excluded))
    nodes = {row['id']: row for row in report['dependencies']}
    reached, pending = set(), [key for key in selected for key in by_target[key]['dependency_ids']]
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        reached.add(key)
        pending.extend(nodes[key]['requires'])
    ordered, done = [], set()
    while done != reached:
        ready = [row for row in report['dependencies']
                 if row['id'] in reached - done and set(row['requires']) <= done]
        if not ready:
            raise TaskError('参考依赖不能形成完整的有向无环图。')
        ordered.extend(deepcopy(ready))
        done.update(row['id'] for row in ready)
    targets = [deepcopy(by_target[key]) for key in selected]
    blockers = [dict(kind='target', id=row['target_id'], missing=deepcopy(row['missing']),
                     data_class=row['data_class']) for row in targets
                if row['missing'] or row['data_class'] == 'unclear']
    blockers += [dict(kind='dependency', id=row['id'], missing=deepcopy(row['missing']),
                      data_class=row['data_class']) for row in ordered
                 if row['missing'] or row['data_class'] == 'unclear']
    return dict(selected_ids=selected, excluded_ids=excluded, exclusion_reason=reason,
                targets=targets, dependencies=ordered, dependency_ids=[row['id'] for row in ordered],
                inventory_targets=[deepcopy(row) for row in report['inventory']['targets'] if row['id'] in selected],
                limitations=deepcopy(report['limitations']), blockers=blockers,
                sharing_rule='each_reachable_dependency_once_no_added_evaluations')


class ReferenceScopeService:
    def __init__(self, tasks, papers, target_analysis):
        if (papers.tasks.path != tasks.path or target_analysis.tasks.path != tasks.path
                or target_analysis.papers is not papers):
            raise TaskError('参考范围、目标核定与论文必须使用同一任务记录。')
        self.tasks, self.papers, self.targets = tasks, papers, target_analysis
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reference_scope_receipts ('
                       'id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), '
                       'sequence INTEGER NOT NULL, document TEXT NOT NULL, document_sha256 TEXT NOT NULL, '
                       'UNIQUE(task_id,sequence))')
            for action in ('UPDATE', 'DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_reference_scope_{action} '
                           f'BEFORE {action} ON reference_scope_receipts '
                           "BEGIN SELECT RAISE(ABORT, 'immutable reference scope history'); END")

    def _task(self, identifier, *, active=False):
        with self.tasks.transaction() as db:
            task = self.tasks._read(db, identifier)
            links = [row[0] for row in db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?', (identifier,))]
            life = [row[0] for row in db.execute('SELECT action FROM task_lifecycle WHERE task_id=?', (identifier,))]
            frozen = db.execute('SELECT sha256 FROM frozen WHERE task_id=?', (identifier,)).fetchone()
            control = db.execute('SELECT action FROM task_control WHERE task_id=? ORDER BY sequence DESC LIMIT 1',
                                 (identifier,)).fetchone()
        if task['mode'] != 'reproduction' or len(links) != 1:
            raise TaskError('参考范围需要已关联唯一论文的复现任务。')
        if active and any(action in {'finish', 'delete'} for action in life):
            raise TaskError('任务已结束，不能保存新的参考范围。')
        if active and control and control['action'] == 'pause':
            raise TaskError('任务已暂停，不能预览或保存新的参考范围。')
        return dict(task_id=identifier, paper_id=links[0], revision=task['revision'],
                    status=task['status'], lifecycle=life,
                    paused=bool(control and control['action'] == 'pause'),
                    frozen_sha256=frozen[0] if frozen else None)

    def _history_binding(self, identifier):
        with self.tasks.transaction() as db:
            bindings = [dict(row) for row in db.execute('SELECT paper_id,task_id,evaluation,task_sha256 '
                         'FROM paper_evaluations WHERE task_id=? ORDER BY evaluation', (identifier,))]
        reference_ids, others = set(), []
        try:
            if bindings and self.papers.ledger is None:
                raise ValueError('Unconfigured bound ledger')
            for binding in bindings:
                snapshot = self.papers.ledger.evaluation_snapshot(binding['evaluation'])
                identity = snapshot['identity']
                if (snapshot['id'] != binding['evaluation'] or identity['task'] != binding['task_sha256']
                        or identity['role'] not in {'reference', 'agent', 'development'}):
                    raise ValueError('Unmatched evaluation')
                if identity['role'] == 'reference':
                    reference_ids.add(binding['evaluation'])
                else:
                    # Presence/counts establish post-execution scope provenance.
                    # No B conditions, scientific output, request logs or model
                    # history is included in the reference scope.
                    counts = {key: snapshot[key] for key in ('dispatch_claims', 'reserved_attempts')}
                    if any(type(value) is not int or value < 0 for value in counts.values()):
                        raise ValueError('Invalid execution history counts')
                    others.append(dict(evaluation_id=binding['evaluation'], role=identity['role'], **counts))
        except (LedgerError, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            raise ReferenceScopeUnavailable('reference_history_unavailable', '已有运行身份或账本未通过核对，不能补作新范围。') from None
        progress = self.papers.reference_progress(identifier)
        if (progress.get('task_id') != identifier or progress.get('available') is not True
                or reference_ids !=
                   {entry['evaluation']['id'] for entry in progress['entries']}):
            raise ReferenceScopeUnavailable('reference_history_unavailable', '已有作者运行账本未通过核对，不能补作新范围。')
        history = []
        for entry in progress['entries']:
            evaluation = entry['evaluation']
            if evaluation.get('available') is not True:
                raise ReferenceScopeUnavailable('reference_history_unavailable', '已有作者运行账本未通过核对。')
            history.append(dict(evaluation_id=evaluation['id'], dispatch_claims=evaluation['dispatch_claims'],
                reserved_attempts=evaluation['reserved_attempts'], requests=[{
                    key: row[key] for key in ('id', 'job_id', 'state', 'dispatch_claimed', 'accounted',
                                             'charge_core_seconds', 'actual_core_seconds')}
                    for row in evaluation['requests']]))
        return dict(bindings=bindings, evaluations=history, other_evaluations=others)

    @staticmethod
    def _adapter(context, evidence):
        messages, proof = context['messages'], context['scientific_adapter']
        if (not isinstance(messages, list) or not messages
                or messages[0].get('role') != 'system'
                or MANDATORY_INSTRUCTION not in messages[0].get('content', '')):
            raise TaskError('原目标核定缺少主动 Adapter 声明。')
        inputs = []
        for message in messages:
            if message.get('role') != 'user':
                continue
            try:
                payload = json.loads(message['content'])
            except (ValueError, TypeError, KeyError):
                continue
            if isinstance(payload, dict) and 'scientific_adapter' in payload:
                inputs.append(payload)
        if len(inputs) != 1:
            raise TaskError('原目标核定的主动 Adapter 不唯一。')
        payload, adapter = inputs[0], inputs[0]['scientific_adapter']
        menu = payload['reference_target_dependency_menu']
        expected = dict(name=adapter['name'], version=adapter['version'], stage=STAGE,
                        contract_sha256=sha256(canonical(adapter)), evidence_sha256=sha256(canonical(evidence)),
                        input_check='passed', scientific_status='not_evaluated')
        if (proof != expected or adapter['stage'] != STAGE
                or adapter['evidence_sha256'] != expected['evidence_sha256']
                or any(payload.get(key) != value for key, value in evidence.items())
                or adapter['capabilities']['reference_target_dependency_menu']['contract_sha256'] != sha256(canonical(menu))
                or menu['inventory_sha256'] != sha256(canonical(evidence['inventory']))
                or menu['target_ids'] != [row['id'] for row in evidence['inventory']['targets']]
                or menu['source_ids'] != [row['id'] for row in evidence['sources']]
                or menu['scope'] != 'reference_only' or menu['B_input'] is not False
                or menu['source_sha256'] != context['controller_sha256']
                or adapter['rules']['scope'] != 'reference_only'
                or adapter['rules']['B_input'] is not False):
            raise TaskError('原目标核定的 Adapter、来源或工具合同不一致。')
        _digest(adapter['source_sha256']); _digest(context['controller_sha256'])
        return deepcopy(proof)

    def _analysis(self, identifier, target_request_id, target_review_id=None):
        saved = self.targets._stored(identifier, task_id(target_request_id))
        if saved is None or saved[2] is None:
            raise ReferenceScopeUnavailable('target_request_unavailable', '目标核定请求尚未形成完整回执。')
        row, context, receipt, _events = saved
        with self.tasks.transaction() as db:
            unknown = self._pending(db, identifier)
        if unknown:
            raise ReferenceScopeUnavailable('target_request_unknown', '目标核定仍有未知请求，请先核对。')
        evidence = reference_target_input(context['input'])
        source = (self.targets.scope_source(identifier, target_request_id)
                  if callable(getattr(self.targets, 'scope_source', None))
                  else self.targets._input(identifier, active=False))
        if (context['input'] != evidence or context['task_id'] != identifier or source != evidence):
            raise ReferenceScopeUnavailable('source_unavailable', '完整 P 输入或论文来源已变化，请重新查看。')
        model, binding = receipt['model_receipt'], context['model_binding']
        if (model.get('state') != 'completed' or model.get('provider') != 'deepseek'
                or model.get('requested_model') != binding['model']
                or model.get('request_sha256') != context['request_sha256']
                or model.get('http_status') != 200 or model.get('finish_reason') != 'stop'
                or model.get('transport_attempts') != 1 or binding.get('enabled') is not True):
            raise TaskError('目标核定的模型来源或完整回执未通过核对。')
        for key in ('request_sha256', 'response_sha256', 'output_sha256'):
            _digest(model[key])
        for key in ('model_ledger_sha256', 'authorization_sha256'):
            _digest(binding[key])
        for key in ('connection_revision', 'credential_generation'):
            if type(binding[key]) is not int or binding[key] < 0:
                raise TaskError('目标核定模型连接版本无效。')
        proof = self._adapter(context, evidence)
        # A self-consistent adapter is insufficient: bind the actual original
        # serialized model request, including its model/options, without keys or
        # network I/O. This also works with a retained legacy adapter contract.
        try:
            original_body = request_body(DeepSeekConfig(**binding['config']), context['messages'],
                                         model=binding['model'], thinking=binding['thinking'])
        except (ValueError, KeyError, TypeError):
            raise TaskError('原目标核定请求的模型参数或消息无法精确核对。') from None
        if sha256(original_body) != context['request_sha256']:
            raise TaskError('原目标核定消息与实际模型请求摘要不一致。')
        review = None
        if target_review_id is not None:
            reviews = [item for item in self.targets.reviews(identifier, target_request_id)
                       if item['review_id'] == task_id(target_review_id)]
            if len(reviews) != 1 or reviews[0]['state'] != 'completed':
                raise TaskError('所选目标复核尚未完成或来源已变化。')
            review = reviews[0]
            if (receipt['state'] not in {'invalid_output', 'unavailable'}
                    or review['parent_context_sha256'] != row['document_sha256']
                    or review['parent_receipt_sha256'] != row['receipt_sha256']
                    or review['original_model_request_sha256'] != context['request_sha256']
                    or review['input_sha256'] != sha256(canonical(evidence))
                    or review['source_output_sha256'] != model['output_sha256']
                    or review['new_model_calls'] != 0 or review['new_model_cost'] != 0):
                raise TaskError('目标复核与原来源、请求或回执不一致。')
            for key in ('normalization_rules_sha256', 'semantic_validator_sha256', 'parent_model_receipt_sha256'):
                _digest(review[key])
            report = review['report']
        else:
            if receipt['state'] != 'completed':
                raise ReferenceScopeUnavailable('target_request_unavailable', '请选择已完成的目标核定或其完整复核。')
            report = receipt['report']
        proposed = {key: deepcopy(report[key]) for key in ('targets', 'dependencies', 'limitations')}
        # The validated report enriches citations with locators and source SHA.
        # Recheck the original two-field citation input, then compare the entire
        # enriched report so altered derived provenance is not silently accepted.
        for section in ('targets', 'dependencies'):
            for item in proposed[section]:
                item['citations'] = [{key: citation[key] for key in ('source_id', 'quote')}
                                     for citation in item['citations']]
        checked = validate_target_analysis(evidence, proposed)
        if report != checked:
            raise TaskError('完整目标报告的引用、依赖或来源不一致。')
        provenance = dict(task_id=identifier, paper_id=evidence['paper_id'], paper=evidence['paper'],
            target_request_id=target_request_id, target_review_id=target_review_id,
            target_context_sha256=row['document_sha256'], target_receipt_sha256=row['receipt_sha256'],
            target_review_sha256=sha256(canonical(review)) if review else None,
            target_report_sha256=sha256(canonical(report)), input_sha256=sha256(canonical(evidence)),
            inventory_sha256=report['inventory_sha256'], source_binding=evidence['source_binding'],
            model_binding_sha256=sha256(canonical(binding)), model_receipt_sha256=sha256(canonical(model)),
            model={key: binding[key] for key in ('model', 'connection_revision', 'credential_generation',
                                                'model_ledger_sha256', 'authorization_sha256')},
            model_request_sha256=model['request_sha256'], model_response_sha256=model['response_sha256'],
            model_output_sha256=model['output_sha256'], scientific_adapter=proof,
            controller_sha256=context['controller_sha256'])
        return report, provenance

    def _pending(self, db, identifier):
        # Recovery owns the immutable, explicit disposition of unknown calls.
        # Reuse that check; a raw UNKNOWN SQL filter would block forever even
        # after an authorized child has a verified, known terminal receipt.
        check = getattr(self.targets, '_pending', None)
        if callable(check):
            return check(db, identifier)
        return bool(db.execute('SELECT 1 FROM reference_target_intents i LEFT JOIN reference_target_receipts r '
            'ON r.id=i.id WHERE i.task_id=? AND (r.id IS NULL OR json_extract(r.document,"$.state")="unknown")',
            (identifier,)).fetchone())

    def _source_view(self, identifier, target_request_id):
        # Display only; never add fields to the pinned scope/provenance schema.
        if not callable(getattr(self.targets, 'scope_source', None)):
            return {}
        target = self.targets.get(identifier, target_request_id) or {}
        if target.get('historical_source') is True and target.get('original_source_preserved') is True:
            return dict(historical_source=True, original_source_preserved=True,
                        source_version_status='historical_original_snapshot')
        return {}

    def preview(self, identifier, target_request_id, selected_ids, exclusion_reason='', *, target_review_id=None):
        task = self._task(task_id(identifier), active=True)
        report, provenance = self._analysis(identifier, target_request_id, target_review_id)
        if task['paper_id'] != provenance['paper_id']:
            raise TaskError('参考范围与当前论文身份不一致。')
        history = self._history_binding(identifier)
        kind = ('continuation_scope' if history['bindings'] or task['frozen_sha256']
                or task['status'] == 'conditions_frozen' else 'preregistered_scope')
        scope = dict(version=VERSION, **FLAGS, kind=kind, task_revision=task['revision'],
            frozen_record_sha256=task['frozen_sha256'], provenance=provenance, prior_execution=history,
            **_closure(report, selected_ids, exclusion_reason))
        return dict(task_id=identifier, scope=scope, scope_sha256=sha256(canonical(scope)),
                    can_prepare_A_plan=not scope['blockers'], **FLAGS,
                    **self._source_view(identifier, target_request_id))

    def _stored(self, identifier, scope_id):
        with self.tasks.transaction() as db:
            row = db.execute('SELECT * FROM reference_scope_receipts WHERE id=?', (task_id(scope_id),)).fetchone()
            if row is None:
                return None
            if row['task_id'] != identifier:
                raise TaskError('范围请求属于其他任务。')
            value = json.loads(row['document'])
            previous = db.execute('SELECT id,document_sha256 FROM reference_scope_receipts '
                'WHERE task_id=? AND sequence=?', (identifier, row['sequence'] - 1)).fetchone()
        if (sha256(canonical(value)) != row['document_sha256'] or value['id'] != row['id']
                or value['task_id'] != identifier or value['sequence'] != row['sequence']
                or value['scope_sha256'] != sha256(canonical(value['scope']))
                or value['previous_scope_id'] != (previous['id'] if previous else None)
                or value['previous_receipt_sha256'] != (previous['document_sha256'] if previous else None)):
            raise TaskError('保存的范围回执或追加历史摘要不一致。')
        return value, row['document_sha256']

    def get(self, identifier, scope_id=None):
        task = self._task(task_id(identifier))
        if scope_id is None:
            with self.tasks.transaction() as db:
                row = db.execute('SELECT id FROM reference_scope_receipts WHERE task_id=? '
                                 'ORDER BY sequence DESC LIMIT 1', (identifier,)).fetchone()
            if row is None:
                return None
            scope_id = row['id']
        stored = self._stored(identifier, scope_id)
        if stored is None:
            return None
        value, receipt_sha256 = stored
        state, reason = 'recorded', ''
        try:
            scope = value['scope']
            report, provenance = self._analysis(identifier, scope['provenance']['target_request_id'],
                                                 scope['provenance']['target_review_id'])
            selected = _closure(report, scope['selected_ids'], scope['exclusion_reason'])
            if provenance != scope['provenance'] or any(scope[key] != item for key, item in selected.items()):
                raise TaskError('范围与原目标来源不一致。')
        except (TaskError, ValueError, KeyError, TypeError, OSError):
            state, reason = 'source_unavailable', '原目标、P 来源或回执暂未通过核验；旧范围记录仍保留。'
        inactive = task['paused'] or any(action in {'finish', 'delete'} for action in task['lifecycle'])
        if not reason and inactive:
            reason = '任务已暂停或结束；旧范围仍可查看。'
        return dict(**value, original_state='recorded', state=state, receipt_sha256=receipt_sha256,
                    disabled_reason=reason,
                    can_prepare_A_plan=state == 'recorded' and not value['scope']['blockers'] and not inactive,
                    **self._source_view(identifier, value['scope']['provenance']['target_request_id']))

    def history(self, identifier):
        self._task(task_id(identifier))
        with self.tasks.transaction() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM reference_scope_receipts WHERE task_id=? '
                                               'ORDER BY sequence', (identifier,))]
        return [self.get(identifier, key) for key in ids]

    def status(self, identifier):
        history = self.history(identifier)
        return dict(task_id=identifier, history=history, latest=history[-1] if history else None, **FLAGS)

    def create(self, identifier, revision, scope_id, target_request_id, selected_ids, exclusion_reason='', *,
               target_review_id=None, expected_scope_sha256=None):
        task_id(identifier); task_id(scope_id); task_id(target_request_id)
        expected_scope_sha256 = _digest(expected_scope_sha256)
        prior = self._stored(identifier, scope_id)
        if prior is not None:
            value = prior[0]; scope = value['scope']; source = scope['provenance']
            if (expected_scope_sha256 != value['scope_sha256'] or source['target_request_id'] != target_request_id
                    or source['target_review_id'] != target_review_id or not isinstance(selected_ids, list)
                    or any(not isinstance(item, str) for item in selected_ids)
                    or len(selected_ids) != len(set(selected_ids)) or set(selected_ids) != set(scope['selected_ids'])
                    or text(exclusion_reason, 2000, required=bool(scope['excluded_ids'])) != scope['exclusion_reason']):
                raise TaskError('范围请求标识已绑定其他选择，不能覆盖。')
            return self.get(identifier, scope_id)
        task = self._task(identifier, active=True)
        if type(revision) is not int or revision != task['revision']:
            raise StaleTask('任务已更新，请刷新后选择参考范围。')
        preview = self.preview(identifier, target_request_id, selected_ids, exclusion_reason,
                               target_review_id=target_review_id)
        if expected_scope_sha256 != preview['scope_sha256']:
            raise StaleTask('参考范围、来源或历史已变化，请重新查看；未保存或授权运行。')
        with self.tasks.transaction() as db:
            current = self.tasks._read(db, identifier)
            links = [row[0] for row in db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?', (identifier,))]
            bindings = [dict(row) for row in db.execute('SELECT paper_id,task_id,evaluation,task_sha256 '
                         'FROM paper_evaluations WHERE task_id=? ORDER BY evaluation', (identifier,))]
            control = db.execute('SELECT action FROM task_control WHERE task_id=? ORDER BY sequence DESC LIMIT 1',
                                 (identifier,)).fetchone()
            if (current['revision'] != revision or current['status'] != task['status']
                    or links != [task['paper_id']] or bindings != preview['scope']['prior_execution']['bindings']
                    or (control and control['action'] == 'pause')
                    or db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('finish','delete')", (identifier,)).fetchone()):
                raise StaleTask('任务或运行历史已变化，请重新查看参考范围。')
            source = preview['scope']['provenance']
            target = db.execute('SELECT i.document_sha256 context_sha,r.document_sha256 receipt_sha '
                'FROM reference_target_intents i JOIN reference_target_receipts r ON r.id=i.id WHERE i.id=? AND i.task_id=?',
                (target_request_id, identifier)).fetchone()
            if (target is None or target['context_sha'] != source['target_context_sha256']
                    or target['receipt_sha'] != source['target_receipt_sha256']):
                raise StaleTask('目标核定回执已变化，未保存范围。')
            if self._pending(db, identifier):
                raise ReferenceScopeUnavailable('target_request_unknown', '目标核定仍有未知请求，未保存范围。')
            existing = db.execute('SELECT id FROM reference_scope_receipts WHERE id=?', (scope_id,)).fetchone()
            if existing is not None:
                # Resolve a same-ID concurrent click outside this transaction.
                raced = True
            else:
                raced = False
                previous = db.execute('SELECT id,sequence,document_sha256 FROM reference_scope_receipts '
                    'WHERE task_id=? ORDER BY sequence DESC LIMIT 1', (identifier,)).fetchone()
                value = dict(version=VERSION, **FLAGS, id=scope_id, task_id=identifier,
                    sequence=previous['sequence'] + 1 if previous else 1,
                    previous_scope_id=previous['id'] if previous else None,
                    previous_receipt_sha256=previous['document_sha256'] if previous else None,
                    at=datetime.now(timezone.utc).isoformat(), scope=preview['scope'],
                    scope_sha256=preview['scope_sha256'], service_sha256=sha256(Path(__file__).read_bytes()))
                db.execute('INSERT INTO reference_scope_receipts VALUES (?,?,?,?,?)',
                    (scope_id, identifier, value['sequence'], canonical(value).decode(), sha256(canonical(value))))
        if raced:
            return self.create(identifier, revision, scope_id, target_request_id, selected_ids, exclusion_reason,
                               target_review_id=target_review_id, expected_scope_sha256=expected_scope_sha256)
        return self.get(identifier, scope_id)
