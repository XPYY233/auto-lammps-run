"""Controller-only reconciliation of one returned native length failure.

This module never sends a model request, reserves a model slot, prepares a new
PDF extraction or changes a historical receipt. Native callbacks own the child
checkpoint, immutable PDF copy and original scientific pipeline. Context and
prefix results are private reference-side data, never a browser or B payload.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import json
import time

from .deepseek import request_body
from .manifest import canonical, sha256
from .tasks import TaskError, task_id
from .workbench_adapter import ROLE, WorkbenchScientificAdapter, native_digest


class KnownFailureError(TaskError):
    code = 'workbench_known_failure_unavailable'


def _deny():
    return KnownFailureError('原失败、HTTP 截断证据、成功前缀或来源身份未完全核对；未恢复或收费。')


def _require(value):
    if not value:
        raise _deny()


def _hash(value):
    _require(isinstance(value, str) and len(value) == 64
        and all(c in '0123456789abcdef' for c in value))
    return value


@dataclass(frozen=True)
class KnownFailureContext:
    """Private reconciliation result; serialize only ``recovery_metadata``."""
    parent_token: str
    proof_kwargs: dict
    successful_prefix: tuple
    prefix_digests: tuple
    model_request_ids: tuple
    recovery_metadata: dict
    planning_time: int


def _evidence(bridge, identifier, request_id, binding, client, *,
              connection_revision, credential_generation, now,
              continuation_request_id=None):
    task_id(identifier); task_id(request_id)
    if continuation_request_id is not None:
        task_id(continuation_request_id)
        _require(continuation_request_id != request_id)
    _require(type(connection_revision) is int and connection_revision >= 0)
    _require(type(credential_generation) is int and credential_generation >= 0)
    _require(type(now) in (int, float) and now >= 0)
    paper = bridge._paper(identifier, binding)
    runtime = bridge.runtime
    _require(binding.role == ROLE and callable(runtime.native_source_reader))
    _require(runtime.native_source_reader(binding.workbench_paper_id) == binding.pdf_sha256)
    model_binding = bridge._model_binding(client)
    with client.calls.transaction() as db:
        _sequence, policy = client.calls._latest_policy(db)
        _require(json.loads(policy).get('config') == asdict(client.calls.config))
    _require(client.calls.config.max_output_tokens >= 32_000)
    identity = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'],
        doi=paper['doi'], role=ROLE, source_sha256=binding.pdf_sha256, model=client.model,
        connection_revision=connection_revision, credential_generation=credential_generation)
    with bridge.papers.tasks.transaction() as db:
        original, failure = bridge._request(db, request_id)
        _require(failure.get('state') == 'failed'
            and all(original.get(k) == v for k, v in identity.items()))
        _require(not db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('delete','finish')",
            (identifier,)).fetchone())
        claimed = db.execute('SELECT id FROM workbench_extraction_recoveries WHERE source_request_id=?',
            (request_id,)).fetchone()
        _require(claimed is None or claimed['id'] == continuation_request_id)
        if claimed is not None:
            new_intent = db.execute('SELECT document FROM workbench_extraction_intents WHERE id=?',
                (continuation_request_id,)).fetchone()
            _require(new_intent is not None)
            pending = json.loads(new_intent['document'])
            _require(pending.get('resume_of') == request_id
                and all(pending.get(k) == v for k, v in identity.items()))
        continuation = bridge._continuation(db, request_id)
        trusted = bridge.recoveries.get(request_id)
        _require(continuation is not None or trusted is not None)
        if trusted is not None:
            _require(trusted.intent_sha256 == sha256(canonical(original))
                and trusted.receipt_sha256 == sha256(canonical(failure))
                and all(getattr(trusted, k) == v for k, v in model_binding.items()))
            token = trusted.job_token
            expected_native = trusted.native_capability_sha256
            prior_native = trusted.prior_native_capability_sha256
            if continuation is not None:
                _require(continuation['job_token'] == token
                    and continuation['native_capability_sha256'] == prior_native)
        else:
            _require(continuation['intent_sha256'] == sha256(canonical(original))
                and all(continuation.get(k) == v for k, v in model_binding.items())
                and continuation.get('workbench_paper_id') == binding.workbench_paper_id
                and continuation.get('source_sha256') == binding.pdf_sha256
                and continuation.get('session_sha256') == sha256(runtime.session_id.encode()))
            token = continuation['job_token']
            expected_native = prior_native = continuation['native_capability_sha256']
        lineage, seen, cursor = [], set(), request_id
        while cursor is not None:
            task_id(cursor)
            _require(cursor not in seen); seen.add(cursor)
            intent, receipt = bridge._request(db, cursor)
            _require(receipt.get('state') == 'failed'
                and all(intent.get(k) == v for k, v in identity.items()))
            saved = bridge._continuation(db, cursor)
            registered = bridge.recoveries.get(cursor)
            old_native = saved['native_capability_sha256'] if saved else (
                registered.prior_native_capability_sha256 if registered else None)
            _require(old_native is not None and (saved is None or saved['job_token'] == token))
            lineage.append((intent, receipt, old_native))
            cursor = intent.get('resume_of')
        for row in db.execute('SELECT i.id,i.document intent,r.document receipt FROM workbench_extraction_intents i '
                'LEFT JOIN workbench_extraction_receipts r ON r.id=i.id WHERE i.paper_id=?', (paper['id'],)):
            other = json.loads(row['intent'])
            if other.get('source_sha256') != binding.pdf_sha256 or row['id'] == continuation_request_id:
                continue
            receipt = json.loads(row['receipt']) if row['receipt'] else None
            _require(receipt is not None and receipt.get('state') not in {'unknown', 'failed_or_unknown'})
    adapter_identity = {**{k: identity[k] for k in ('task_id', 'paper_id', 'title', 'doi', 'role', 'source_sha256')},
        'request_id': request_id, 'workbench_paper_id': binding.workbench_paper_id}
    adapter = WorkbenchScientificAdapter(adapter_identity,
        capability_reader=runtime.native_capability_reader, stage_reader=lambda: None)
    current_native = sha256(canonical(adapter.installed))
    _require(current_native == expected_native and callable(runtime.native_recovery_reader))
    native = runtime.native_recovery_reader(token)
    checkpoint, execution, job = native['checkpoint'], native['execution'], native['job']
    manifest = checkpoint.manifest
    _require(getattr(checkpoint, 'continuation_lineage', None) is None)
    stage_limits = {'initial_focus': 16_000, 'coverage_gap': 12_000}
    _require(checkpoint.state == 'outcome_unknown' and checkpoint.stage in stage_limits
        and checkpoint.reason_code == 'literature_call_outcome_unknown')
    _require(checkpoint.lease_owner_digest is None and checkpoint.lease_expires_at is None)
    _require(job.token == token and job.paper_id == binding.workbench_paper_id
        and dict(job.paper) == {'title': paper['title'], 'doi': paper['doi']}
        and job.snapshot['pdf_sha256'] == binding.pdf_sha256
        and manifest.pdf_snapshot_fingerprint == job.snapshot['content_fingerprint']
        and manifest.task_id == 'literature_' + sha256(token.encode())
        and manifest.session_digest == sha256(runtime.session_id.encode())
        and manifest.provider_id == 'deepseek' and manifest.runtime_revision == connection_revision
        and manifest.credential_generation == credential_generation
        and tuple(manifest.task_models) == (('analysis', client.model), ('extraction', client.model))
        and manifest.executor_id == adapter.installed['executor']['id']
        and manifest.executor_version == adapter.installed['executor']['version'])
    stage = job.stage
    prefix = execution.completed_results
    receipts = checkpoint.receipts
    offset = execution.receipt_offset
    _require(type(offset) is int and 0 <= offset < len(receipts)
        and execution.completion_result is None
        and execution.stage_fingerprint == stage['stage_fingerprint']
        and checkpoint.stage == stage['name'] and len(receipts) == offset + len(prefix) + 1
        and len(prefix) < len(stage['calls'])
        and all(r.state == 'succeeded' for r in receipts[:-1])
        and receipts[-1].state == 'outcome_unknown')
    # Earlier stages are sealed separately from the current stage's call plan.
    # Their receipts remain global; never index them into the coverage plan or
    # mistake the current-stage prefix for every already paid response.
    prior_stages = {row['stage']: row['stage_fingerprint'] for row in job.stage_outputs}
    _require((checkpoint.stage == 'initial_focus' and offset == 0 and not prior_stages)
        or (checkpoint.stage == 'coverage_gap' and offset > 0
            and set(prior_stages) == {'initial_focus'}
            and all(r.stage == 'initial_focus' and r.task == 'extraction'
                and r.max_tokens == 16_000 for r in receipts[:offset])))
    _require(checkpoint.spent_calls == len(receipts)
        and checkpoint.spent_tokens == sum(r.max_tokens for r in receipts))
    call_records, call_ids = [], []
    for intent, receipt, old_native in reversed(lineage):
        ids = receipt.get('model_request_ids')
        _require(isinstance(ids, list) and len(set(ids)) == len(ids))
        base = {k: intent[k] for k in ('task_id', 'paper_id', 'role', 'source_sha256', 'request_id')}
        proof_identity = {**base, 'title': paper['title'], 'doi': paper['doi'],
            'workbench_paper_id': binding.workbench_paper_id}
        for ordinal, call_id in enumerate(ids, 1):
            _require(call_id == sha256(canonical({**base, 'ordinal': ordinal}))[:32])
            record = client.calls.lookup(call_id)
            _require(record is not None and record.get('request_id') == call_id
                and isinstance(record.get('receipt'), dict))
            saved = record['receipt']; index = len(call_records)
            _require(index < len(receipts))
            native_receipt = receipts[index]
            actual_proof = saved.get('scientific_adapter')
            _require(isinstance(actual_proof, dict)
                and actual_proof.get('identity') == proof_identity
                and actual_proof.get('name') == 'literature_workbench_scientific_adapter'
                and actual_proof.get('adapter_source_sha256') == adapter.adapter_sha256
                and actual_proof.get('native_contract_sha256') == old_native
                and sha256(canonical(actual_proof.get('native_capabilities'))) == old_native
                and actual_proof.get('source_identity_sha256') == sha256(canonical(proof_identity))
                and actual_proof.get('scientific_validation') is False
                and actual_proof.get('release_to_B') is False
                and actual_proof.get('native_call_digest') == native_receipt.call_digest
                and actual_proof.get('stage') == native_receipt.stage
                and native_receipt.task == saved.get('task') == 'extraction'
                and native_receipt.model == client.model
                and native_receipt.ordinal == index + 1
                and native_receipt.max_tokens == saved.get('max_output_tokens'))
            if index < offset:
                _require(native_receipt.state == 'succeeded'
                    and actual_proof.get('stage_fingerprint') == prior_stages.get(native_receipt.stage))
                expected_request_sha256 = _hash(saved.get('request_sha256'))
            else:
                local_index = index - offset
                _require(local_index < len(stage['calls']))
                call = stage['calls'][local_index]
                _require(call['call_digest'] == native_receipt.call_digest
                    and native_receipt.stage == stage['name']
                    and native_receipt.max_tokens == call['max_tokens'])
                historical_adapter = WorkbenchScientificAdapter(proof_identity,
                    capability_reader=lambda p=actual_proof: p['native_capabilities'],
                    stage_reader=lambda: {'paper_id': job.paper_id, 'paper': dict(job.paper),
                        'pdf_sha256': job.snapshot['pdf_sha256'], 'stage': stage,
                        'runtime_task': lambda task: task})
                historical_adapter.positions[stage['stage_fingerprint']] = local_index
                options = dict(task='extraction', max_tokens=call['max_tokens'],
                    thinking=call['options'].get('thinking'), temperature=call['options'].get('temperature'))
                messages, expected_proof = historical_adapter.prepare(call['messages'], options)
                _require(canonical(actual_proof) == canonical(expected_proof))
                payload = json.loads(request_body(replace(client.calls.config, max_output_tokens=call['max_tokens']),
                    messages, model=client.model,
                    thinking=client.thinking if options['thinking'] is None else options['thinking']))
                if options['temperature'] is not None:
                    payload['temperature'] = options['temperature']
                expected_request_sha256 = sha256(canonical(payload))
            _require(saved.get('request_sha256') == record.get('request_sha256') == expected_request_sha256
                and saved.get('provider') == 'deepseek' and saved.get('requested_model') == client.model
                and saved.get('purpose') == 'literature_workbench_extraction' and saved.get('role') == ROLE
                and type(saved.get('transport_attempts')) is int and saved['transport_attempts'] == 1
                and type(saved.get('http_status')) is int and saved['http_status'] == 200
                and saved.get('max_output_tokens') == native_receipt.max_tokens)
            _hash(saved.get('response_sha256'))
            usage = saved.get('usage')
            _require(isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
                and usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'])
            if index < len(receipts) - 1:
                _require(saved.get('state') == 'completed' and saved.get('finish_reason') == 'stop'
                    and isinstance(saved.get('structured_output'), dict)
                    and saved.get('output_sha256') == sha256(canonical(saved['structured_output']))
                    and native_digest(saved['structured_output']) == native_receipt.result_digest)
                if index >= offset:
                    _require(native_receipt.result_digest == native_digest(prefix[index - offset]))
            else:
                _require(saved.get('state') == 'response_invalid' and saved.get('finish_reason') == 'length'
                    and saved.get('error') == 'incomplete_generation'
                    and call['max_tokens'] == stage_limits[stage['name']]
                    and call['options'].get('thinking') is False
                    and 0 < usage['completion_tokens'] <= call['max_tokens'])
            call_records.append(saved); call_ids.append(call_id)
    _require(len(set(call_ids)) == len(call_ids) == len(receipts))
    terminal = call_records[-1]
    proof_kwargs = dict(original_request_id=request_id, model_request_id=call_ids[-1],
        native_task_id=manifest.task_id, native_revision=checkpoint.revision,
        native_call_digest=receipts[-1].call_digest, stage_fingerprint=execution.stage_fingerprint,
        source_fingerprint=manifest.pdf_snapshot_fingerprint, pdf_sha256=binding.pdf_sha256,
        provider_id=manifest.provider_id, runtime_revision=manifest.runtime_revision,
        credential_generation=manifest.credential_generation, session_digest=manifest.session_digest,
        executor_id=manifest.executor_id, executor_version=manifest.executor_version, model=client.model,
        request_sha256=terminal['request_sha256'], response_sha256=terminal['response_sha256'],
        http_status=200, finish_reason='length', response_state='response_invalid',
        error_code='incomplete_generation', transport_attempts=1, max_output_tokens=receipts[-1].max_tokens,
        usage_total_tokens=terminal['usage']['total_tokens'], usage_completion_tokens=terminal['usage']['completion_tokens'])
    metadata = dict(kind='known_length_continuation', job_token=token, source_request_id=request_id,
        reused_model_calls=len(receipts) - 1, prefix_count=len(prefix), inherited_failed_model_calls=1,
        inherited_failed_tokens=receipts[-1].max_tokens, stage_fingerprint=execution.stage_fingerprint,
        native_capability_sha256=current_native, prior_native_capability_sha256=prior_native,
        intent_sha256=sha256(canonical(original)), receipt_sha256=sha256(canonical(failure)),
        checkpoint_revision=checkpoint.revision)
    context = KnownFailureContext(token, proof_kwargs, tuple(deepcopy(prefix)),
        tuple(native_digest(r) for r in prefix), tuple(call_ids), metadata, int(now))
    if manifest.expires_at <= now:
        # A consumed parent may be re-proved for its existing child only. A
        # renderer claim alone cannot renew it or admit another child.
        _require(claimed is not None and claimed['id'] == continuation_request_id)
        planning_time = _existing_child_planning_time(bridge, context, native,
            continuation_request_id, now)
        context = replace(context, planning_time=planning_time)
    return context, native


def _existing_child_planning_time(bridge, context, parent, request_id, now):
    _require(request_id is not None)
    from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
    token = bridge._known_child_token(context.parent_token, request_id)
    child = bridge.runtime.native_recovery_reader(token)
    checkpoint, job = child['checkpoint'], child['job']
    original, original_job = parent['checkpoint'], parent['job']
    lineage = checkpoint.continuation_lineage
    issued = checkpoint.manifest.issued_at
    lifetime = min(86_400, original.manifest.expires_at - original.manifest.issued_at,
        int(original_job.expires_at - original_job.issued_at))
    _require(lineage is not None and lineage.parent_task_id == original.manifest.task_id
        and lineage.parent_revision == original.revision
        and lineage.parent_checkpoint_digest == sha256(encode_checkpoint(original))
        and lineage.parent_job_state_digest == sha256(parent['execution'].job_state)
        and lineage.failed_receipt == original.receipts[-1]
        and lineage.continuation_request_id == request_id
        and tuple(lineage.prefix_result_digests) == context.prefix_digests
        and canonical(json.loads(lineage.proof_bytes)) == canonical({
            'schema_version': 'literature-known-length-proof-v1', **context.proof_kwargs})
        and lineage.original_max_calls == original.manifest.max_calls
        and lineage.original_max_tokens == original.manifest.max_tokens
        and type(issued) is int and original.manifest.issued_at <= issued < original.manifest.expires_at
        and lifetime > 0 and checkpoint.manifest.expires_at == issued + lifetime > now
        and job.issued_at == issued and job.expires_at == issued + lifetime
        and checkpoint.manifest.task_id == 'literature_' + sha256(token.encode())
        and checkpoint.manifest.max_calls == original.manifest.max_calls + 1
        and checkpoint.manifest.max_tokens == original.manifest.max_tokens + 32_000
        and checkpoint.receipts[:len(original.receipts) - 1] == original.receipts[:-1]
        and checkpoint.spent_calls >= original.spent_calls
        and checkpoint.spent_tokens >= original.spent_tokens
        and job.token == token and job.paper_id == original_job.paper_id
        and dict(job.paper) == dict(original_job.paper) and job.snapshot == original_job.snapshot
        and job.chunks == original_job.chunks and job.experiment_profile == original_job.experiment_profile
        and job.focuses == original_job.focuses and job.learning_guidance == original_job.learning_guidance)
    for field in ('session_digest', 'provider_id', 'runtime_revision', 'credential_generation',
            'task_models', 'executor_id', 'executor_version', 'pdf_snapshot_fingerprint'):
        _require(getattr(checkpoint.manifest, field) == getattr(original.manifest, field))
    return issued


def _proof_verifier(bridge, identifier, request_id, binding, client, settings, expected):
    def verify(candidate):
        try:
            if hasattr(candidate, '__dataclass_fields__'):
                candidate = asdict(candidate)
            fresh, _ = _evidence(bridge, identifier, request_id, binding, client, **settings)
            return (canonical(candidate) == canonical(expected.proof_kwargs)
                and fresh == expected)
        except Exception:
            return False
    return verify


def known_failure_context(bridge, identifier, request_id, binding, client, *,
                          connection_revision, credential_generation, now=None,
                          continuation_request_id=None):
    """Read-only status reconciliation; preview identities are never admitted."""
    try:
        settings = dict(connection_revision=connection_revision, credential_generation=credential_generation,
            now=time.time() if now is None else now, continuation_request_id=continuation_request_id)
        context, native = _evidence(bridge, identifier, request_id, binding, client, **settings)
        planner = getattr(bridge.runtime, 'native_known_failure_planner', None)
        _require(callable(planner))
        preview_request = sha256(canonical({'known_length_preview': request_id}))[:32]
        preview_token = sha256(('known-length-preview:' + context.parent_token).encode())
        plan = planner(context.parent_token, continuation_token=preview_token,
            continuation_request_id=preview_request, proof_kwargs=deepcopy(context.proof_kwargs),
            verify_proof=_proof_verifier(bridge, identifier, request_id, binding, client, settings, context),
            now=context.planning_time)
        _validate_child(plan, context, native, preview_token, preview_request, now=context.planning_time)
        metadata = {**context.recovery_metadata, 'max_calls': plan['checkpoint'].manifest.max_calls,
            'max_tokens': plan['checkpoint'].manifest.max_tokens, 'replacement_max_tokens': 32_000}
        return replace(context, recovery_metadata=metadata)
    except KnownFailureError:
        raise
    except Exception:
        raise _deny() from None


def _validate_child(child, context, parent, token, request_id, *, now):
    checkpoint, execution, job = child['checkpoint'], child['execution'], child['job']
    original, original_job = parent['checkpoint'], parent['job']
    lineage = checkpoint.continuation_lineage
    _require(lineage is not None and lineage.parent_task_id == original.manifest.task_id
        and lineage.parent_revision == original.revision
        and lineage.failed_receipt == original.receipts[-1]
        and tuple(lineage.prefix_result_digests) == context.prefix_digests
        and lineage.continuation_request_id == request_id)
    _require(canonical(json.loads(lineage.proof_bytes)) == canonical({
        'schema_version': 'literature-known-length-proof-v1', **context.proof_kwargs}))
    _require(lineage.original_max_calls == original.manifest.max_calls
        and lineage.original_max_tokens == original.manifest.max_tokens)
    _require(checkpoint.manifest.task_id == 'literature_' + sha256(token.encode())
        and original.manifest.max_calls <= 512
        and checkpoint.manifest.max_calls == original.manifest.max_calls + 1 <= 513
        and checkpoint.manifest.max_tokens == original.manifest.max_tokens + 32_000 <= 8_200_000
        and checkpoint.spent_calls == original.spent_calls
        and checkpoint.spent_tokens == original.spent_tokens
        and checkpoint.receipts == original.receipts[:-1]
        and checkpoint.state in {'authorized', 'paused'} and checkpoint.lease_owner_digest is None
        and checkpoint.lease_expires_at is None)
    for field in ('session_digest', 'provider_id', 'runtime_revision', 'credential_generation',
            'task_models', 'executor_id', 'executor_version', 'pdf_snapshot_fingerprint'):
        _require(getattr(checkpoint.manifest, field) == getattr(original.manifest, field))
    lifetime = min(86_400, original.manifest.expires_at - original.manifest.issued_at,
        int(original_job.expires_at - original_job.issued_at))
    _require(type(now) is int and original.manifest.issued_at <= now < original.manifest.expires_at
        and lifetime > 0 and checkpoint.manifest.issued_at == job.issued_at == now
        and checkpoint.manifest.expires_at == job.expires_at == now + lifetime)
    _require(job.token == token and job.paper_id == original_job.paper_id
        and dict(job.paper) == dict(original_job.paper) and job.snapshot == original_job.snapshot
        and job.chunks == original_job.chunks and job.experiment_profile == original_job.experiment_profile
        and job.focuses == original_job.focuses and job.learning_guidance == original_job.learning_guidance
        and job.stage_outputs == original_job.stage_outputs)
    _require(execution.receipt_offset == parent['execution'].receipt_offset
        and tuple(execution.completed_results) == context.successful_prefix
        and execution.stage_fingerprint == job.stage['stage_fingerprint']
        and job.stage['name'] == checkpoint.stage == original_job.stage['name']
        and len(job.stage['calls']) == len(original_job.stage['calls']))
    index = len(context.successful_prefix)
    _require(child['replacement_index'] == index)
    _require(child['replacement_call'].call_digest == lineage.replacement_call_digest
        and child['replacement_call'].max_tokens == 32_000)
    for i, call in enumerate(job.stage['calls']):
        prior = original_job.stage['calls'][i]
        if i == index:
            _require(call['max_tokens'] == 32_000 and call['messages'] == prior['messages']
                and call['options'] == prior['options'] and call['task'] == prior['task']
                and call['call_digest'] == lineage.replacement_call_digest
                and call['call_id'] == lineage.replacement_call_id
                and call['call_digest'] != prior['call_digest'])
        else:
            _require(call == prior)


def create_known_failure_child(bridge, identifier, request_id, binding, client, *,
                               continuation_request_id, connection_revision,
                               credential_generation, now=None):
    """One approved application action; never issue or retry a provider call."""
    try:
        task_id(continuation_request_id); _require(continuation_request_id != request_id)
        context = known_failure_context(bridge, identifier, request_id, binding, client,
            connection_revision=connection_revision, credential_generation=credential_generation,
            now=now, continuation_request_id=continuation_request_id)
        settings = dict(connection_revision=connection_revision, credential_generation=credential_generation,
            now=time.time() if now is None else now, continuation_request_id=continuation_request_id)
        evidence, parent = _evidence(bridge, identifier, request_id, binding, client, **settings)
        _require(evidence.proof_kwargs == context.proof_kwargs and evidence.prefix_digests == context.prefix_digests)
        _require(parent['checkpoint'].manifest.expires_at > settings['now'])
        token = sha256(canonical({'known_length_child': continuation_request_id,
            'parent_token': context.parent_token, 'parent_native_task_id': context.proof_kwargs['native_task_id']}))
        creator = getattr(bridge.runtime, 'native_known_failure_creator', None)
        _require(callable(creator))
        child = creator(context.parent_token, continuation_token=token,
            continuation_request_id=continuation_request_id, proof_kwargs=deepcopy(context.proof_kwargs),
            verify_proof=_proof_verifier(bridge, identifier, request_id, binding, client, settings, evidence),
            now=int(settings['now']))
        _validate_child(child, evidence, parent, token, continuation_request_id, now=int(settings['now']))
        _require(child['job'].snapshot_ref != parent['job'].snapshot_ref)
        return {**context.recovery_metadata, 'job_token': token,
            'parent_job_token': context.parent_token, 'continuation_request_id': continuation_request_id,
            'stage_fingerprint': child['execution'].stage_fingerprint,
            'checkpoint_revision': child['checkpoint'].revision,
            'parent_checkpoint_revision': context.proof_kwargs['native_revision'],
            'parent_native_task_id': context.proof_kwargs['native_task_id'],
            'native_task_id': child['checkpoint'].manifest.task_id,
            'replacement_call_digest': child['checkpoint'].continuation_lineage.replacement_call_digest,
            'replacement_call_id': child['checkpoint'].continuation_lineage.replacement_call_id,
            'known_failure_response_sha256': context.proof_kwargs['response_sha256'],
            'prefix_result_digests': list(context.prefix_digests)}
    except KnownFailureError:
        raise
    except Exception:
        raise _deny() from None
