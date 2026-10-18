"""Local protocol bridge to the existing literature workbench extraction runner.

The separately installed workbench owns PDF capture, extraction, scientific
verification, figure/table processing and atomic publication. No implementation
of those operations is copied here. The controller supplies trusted paper
bindings and persistent workbench services; the browser supplies neither PDFs,
credentials nor independent-calculation conditions.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import importlib
import json
import math
from pathlib import Path
import re
import time

from .deepseek import (DeepSeekClient, MAX_RESPONSE_BYTES, ModelError, parse_completion,
                       request_body, response_diagnostics, usage_from_response)
from .manifest import canonical, sha256
from .papers import doi_text
from .runtime_launcher import read_regular
from .tasks import TaskError, task_id
from .workbench_adapter import WorkbenchAdapterError, WorkbenchScientificAdapter, native_digest


HUMAN_ROLE = 'paper_reference_human_only'
WORKBENCH_CHUNK_PAGES = 1
_HASH = re.compile(r'[a-f0-9]{64}')
_MODEL_ERRORS = {
    'literature_visual_unavailable': '原生文献工作台未发现可补全图表；未创建模型调用，已发布数据保留。',
    'workbench_context_changed': '提取来源、模型配置或授权已变化；后续模型请求暂停，原记录保留。',
    'workbench_model_options_outside_policy': '工作台调用选项超出本项目已配置的模型策略，未发送。',
    'workbench_model_context_too_large': '工作台来源上下文超出本项目已配置的单次模型容量，未发送。',
    'workbench_model_request_not_completed': '工作台模型请求未完成；请求和费用记录保留，不会自动重试。',
}


class WorkbenchBridgeError(TaskError):
    def __init__(self, code):
        self.code = code
        super().__init__(_MODEL_ERRORS[code])


def _digest(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise TaskError('文献工作台来源摘要无效。')
    return value


@dataclass(frozen=True)
class WorkbenchPaperBinding:
    """Trusted mapping created outside the renderer, not an import request."""
    paper_id: str
    workbench_paper_id: int
    pdf_sha256: str
    role: str = HUMAN_ROLE

    def __post_init__(self):
        task_id(self.paper_id)
        _digest(self.pdf_sha256)
        if type(self.workbench_paper_id) is not int or self.workbench_paper_id < 1:
            raise TaskError('工作台论文标识无效。')
        if self.role != HUMAN_ROLE:
            raise TaskError('文献提取只能进入人类论文参考页面。')


@dataclass(frozen=True)
class WorkbenchRecoveryBinding:
    """Controller-only migration of a verified legacy continuation identity.

    Both dependency digests are explicit: a reviewed local codec repair may
    change the installed implementation without rewriting the old receipt.
    This type is never accepted from a renderer or included in a public view.
    """
    request_id: str
    job_token: str
    intent_sha256: str
    receipt_sha256: str
    model_ledger_sha256: str
    model_policy_sha256: str
    prior_native_capability_sha256: str
    native_capability_sha256: str

    def __post_init__(self):
        task_id(self.request_id)
        if (not isinstance(self.job_token, str) or not 1 <= len(self.job_token) <= 256
                or any(ord(c) < 33 or ord(c) > 126 for c in self.job_token)):
            raise TaskError('工作台恢复身份无效。')
        for key, value in asdict(self).items():
            if key not in {'request_id', 'job_token'}:
                _digest(value)


@dataclass(frozen=True)
class WorkbenchRuntime:
    database: object
    jobs: object
    ports: object
    session_id: str
    action_factory: object
    budget_client_factory: object
    decode_job_state: object
    exports: object
    data_items: object
    visual_assets: object
    visual_image: object = None
    native_capability_reader: object = None
    native_stage_reader: object = None
    native_recovery_reader: object = None
    native_source_reader: object = None
    pdf_intake_root: object = None
    native_paper_sources_reader: object = None
    native_known_failure_planner: object = None
    native_known_failure_creator: object = None
    native_unknown_reissue_creator: object = None
    completed_native_capability_allowlist: tuple = ()

    def __post_init__(self):
        values = self.completed_native_capability_allowlist
        if (type(values) is not tuple or len(values) > 8
                or any(not isinstance(value, str) for value in values)
                or len(set(values)) != len(values)):
            raise TaskError('已完成文献历史的原生能力许可无效。')
        for value in values:
            _digest(value)


def _reference_paper_sources(pages):
    """Retain all complete PDF pages within the existing source-text capacity.

    Small papers keep individual page locators. Long papers use contiguous
    whole-page blocks; neither a page nor the final block is truncated.
    Outer whitespace follows the existing per-source canonicalization;
    characters inside every page remain unchanged.
    """
    from .condition_generation import source_bundle
    if not isinstance(pages, (list, tuple)) or not pages:
        raise TaskError('缺少文献工作台的完整原文页。')
    rows, blocks, texts, start, end, unknown_glyphs = [], [], [], None, None, {}
    for number, page in enumerate(pages, 1):
        if not isinstance(page, str):
            raise TaskError('文献原文页格式不完整。')
        if not page.strip():
            continue
        if len(page) > 24000:
            raise TaskError('单页完整原文超过目标分析来源容量；未截断原文。')
        # A PDF can contain an undecodable glyph represented by NUL. Preserve
        # its unknown status visibly rather than guessing or dropping a letter.
        unknown_glyphs[number] = page.count('\x00')
        page = page.replace('\x00', '\uFFFD').strip()
        if len(pages) <= 32:
            note=(f' · {unknown_glyphs[number]} undecodable NUL glyph(s) marked U+FFFD; original PDF retained'
                if unknown_glyphs[number] else '')
            rows.append(dict(id='paper-page-' + str(number), origin='paper',
                locator='PDF page ' + str(number)+note, text=page))
            continue
        if texts and len('\n\n'.join([*texts, page])) > 24000:
            blocks.append((start, end, '\n\n'.join(texts)));texts=[];start=None
        if start is None:start=number
        end=number;texts.append(page)
    if texts:blocks.append((start, end, '\n\n'.join(texts)))
    for start,end,content in blocks:
        count=sum(unknown_glyphs[n] for n in range(start,end+1) if n in unknown_glyphs)
        note=(f' · {count} undecodable NUL glyph(s) marked U+FFFD; original PDF retained' if count else '')
        rows.append(dict(id=f'paper-pages-{start}-{end}',origin='paper',
            locator=f'PDF pages {start}-{end}'+note,text=content))
    return source_bundle(rows)


def installed_workbench_runtime(*, database, snapshot_blobs, checkpoint_runtime,
                                session_id, explicit_reissue_store=None):
    """Compose existing exported classes; never open another project's settings.

    ``auto_research`` must already be installed in this service's Python runtime.
    Storage and its authenticated-encryption authorities are injected by the
    local controller, including an existing EvidenceDB with registered PDFs.
    This factory neither imports a PDF nor configures an API key.
    """
    if (checkpoint_runtime is None or snapshot_blobs is None
            or not isinstance(session_id, str) or not 1 <= len(session_id) <= 256):
        raise TaskError('请配置工作台持久检查点、PDF 快照和专属会话。')
    try:
        def load(name):
            return importlib.import_module('auto_research.' + name)
        job = load('evidence.literature_extraction_job')
        business = load('evidence.literature_extraction_business_action')
        finalizer = load('evidence.literature_extraction_finalizer')
        prepared = load('ai.prepared_actions')
        budget = load('ai.business_actions')
        persistence = load('evidence.literature_job_persistence')
        export = load('evidence.evidence_export')
        six = load('evidence.six_column')
        visuals = load('evidence.visual_evidence')
        visual_review = load('evidence.literature_visual_review')
        workflow = load('evidence.literature_extraction_checkpoint_workflow')
        stages = load('evidence.literature_extraction_stages')
        stage_budget = load('evidence.literature_extraction_budget')
        extraction = load('evidence.deepseek_extraction')
        execution = load('evidence.literature_checkpoint_runtime')
        context = load('evidence.context_chat')
        try:
            known_length = load('evidence.literature_known_length_recovery')
        except ImportError:
            known_length = None
        recovery_modules = (() if known_length is None else (execution, known_length,
            load('evidence.literature_task_checkpoint'),
            load('evidence.literature_task_checkpoint_codec'),
            load('evidence.literature_task_checkpoint_store'),
            load('evidence.literature_task_checkpoint_service')))
        snapshots = job.LiteraturePDFSnapshotAuthority(blob_store=snapshot_blobs)
        if snapshots.persistent is not True:
            raise ValueError('persistent snapshots required')
        jobs = job.LiteratureExtractionJobStore(snapshots=snapshots)
        ports = business.literature_extraction_business_ports(jobs,
            session_id=session_id, db=database,
            finalizer=finalizer.AtomicEvidenceDBFinalizer(database),
            checkpoint_runtime=checkpoint_runtime)
        # Configure the exported native starter, not its extraction/planner
        # implementation. Dense four-page blocks exceeded response limits;
        # one-page blocks retain every source page and native budget checks.
        class PageBoundedStarter(business.EvidenceDBLiteratureJobStarter):
            CHUNK_PAGES = WORKBENCH_CHUNK_PAGES
        starter = PageBoundedStarter(database, jobs)
        ports = replace(ports, assembler=business.LiteratureExtractionBusinessAssembler(
            jobs, session_id=session_id, starter=starter,
            checkpoint_runtime=checkpoint_runtime))
        exports = export.EvidenceExportService(
            workspace_resolver=export.WorkspaceEvidenceProjectionResolver(database),
            federated_resolver=None)
        def native_capabilities():
            operations = {'assemble': ports.assembler.assemble,
                'execute': ports.executor.execute, 'project': ports.projector.project,
                'plan_next': stages.ExistingLiteratureStagePlanner.plan_next,
                'finalize': finalizer.AtomicEvidenceDBFinalizer.finalize}
            if any(not callable(fn) for fn in operations.values()):
                raise WorkbenchAdapterError('实装工作台提取接口不完整。')
            partition = {'chunk_pages': starter.CHUNK_PAGES, 'max_pages': starter.MAX_PAGES,
                         'policy': 'complete_single_page_blocks_v1'}
            if callable(getattr(job, '_source_fragment_chunks', None)):
                partition.update(policy='whole_line_source_fragments_v1',
                    max_lines=extraction.MAX_SOURCE_FRAGMENT_LINES,
                    max_chars=extraction.MAX_SOURCE_FRAGMENT_CHARS,
                    max_fragments=min((job.MAX_STAGE_CALLS-8)//8,
                                      (job.MAX_STAGE_TOKENS-52000)//72000))
            return {'planner': {'id': workflow._PLANNER_ID, 'version': workflow._PLANNER_VERSION},
                'executor': {'id': workflow._EXECUTOR_ID, 'version': workflow._EXECUTOR_VERSION},
                'runtime_tasks': sorted(workflow.LITERATURE_POLICY_TASKS),
                'model_stages': sorted(job.MODEL_STAGES),
                'output_limits': {'extraction_records_per_response':
                    stage_budget.MAX_EXTRACTED_RECORDS_PER_RESPONSE},
                'source_partition': partition,
                'operations': {key: fn.__module__ + '.' + fn.__qualname__ for key, fn in operations.items()},
                'modules': {module.__name__: sha256(Path(module.__file__).read_bytes())
                    for module in (job, business, workflow, stages, stage_budget,
                                   extraction, persistence, finalizer, context, visuals, visual_review,
                                   *recovery_modules)}}
        def native_stage(token):
            state = persistence.decode_job_private_state(jobs.export_private_state(token, session_id=session_id))
            return {'paper_id': state.paper_id, 'paper': dict(state.paper),
                'pdf_sha256': state.snapshot['pdf_sha256'], 'stage': state.stage,
                'runtime_task': workflow._runtime_task}
        def native_recovery(token):
            # Public load on the existing sealed authority is read-only.
            # recover_job_state would normalize a lease or unknown call and
            # therefore must not be used by the browser's status endpoint.
            checkpoint = checkpoint_runtime._service.load(workflow._checkpoint_task_id(token))
            state = execution.decode_execution_state(checkpoint.private_payload)
            return {'checkpoint': checkpoint, 'execution': state,
                'job': persistence.decode_job_private_state(state.job_state)}
        def known_plan(token, *, continuation_token, continuation_request_id,
                       proof_kwargs, verify_proof, now):
            if known_length is None:
                raise TaskError('原生工作台尚未提供受信截断恢复接口。')
            proof = known_length.KnownLengthProof(**proof_kwargs)
            plan = known_length.plan_known_length_continuation(
                native_recovery(token)['checkpoint'], continuation_token=continuation_token,
                continuation_request_id=continuation_request_id, proof=proof,
                verify_proof=lambda value: verify_proof(asdict(value)), now=now)
            state = execution.decode_execution_state(plan.checkpoint.private_payload)
            return {'checkpoint': plan.checkpoint, 'execution': state,
                'job': persistence.decode_job_private_state(state.job_state),
                'replacement_index': plan.replacement_index, 'replacement_call': plan.replacement_call}
        def known_create(token, *, continuation_token, continuation_request_id,
                         proof_kwargs, verify_proof, now):
            # The native store admits one child atomically; the public caller
            # cannot rewrite or revive an already executing/unknown child.
            plan = known_plan(token, continuation_token=continuation_token,
                continuation_request_id=continuation_request_id, proof_kwargs=proof_kwargs,
                verify_proof=verify_proof, now=now)
            checkpoint = checkpoint_runtime.create_known_length_continuation(
                workflow._checkpoint_task_id(token), continuation_token=continuation_token,
                continuation_request_id=continuation_request_id,
                proof=known_length.KnownLengthProof(**proof_kwargs),
                verify_proof=lambda value: verify_proof(asdict(value)), now=now,
                snapshot_blobs=snapshot_blobs)
            state = execution.decode_execution_state(checkpoint.private_payload)
            decoded = persistence.decode_job_private_state(state.job_state)
            ready = (checkpoint.state in {'authorized', 'paused'}
                and checkpoint.stage == plan['checkpoint'].stage
                and checkpoint.stage in {'initial_focus', 'coverage_gap'}
                and checkpoint.lease_owner_digest is None
                and state.completed_results == plan['execution'].completed_results
                and checkpoint.receipts == plan['checkpoint'].receipts)
            if ready:
                try:
                    jobs.peek_stage(continuation_token, session_id=session_id)
                except job.LiteratureExtractionJobError as exc:
                    if exc.code == 'literature_job_expired':
                        jobs.restore_private_state(state.job_state, session_id=session_id,
                            allow_authenticated_session_rebind=True)
                    elif exc.code != 'literature_stage_busy':
                        raise
            return {'checkpoint': checkpoint, 'execution': state, 'job': decoded,
                'replacement_index': plan['replacement_index'], 'replacement_call': plan['replacement_call']}
        def unknown_reissue_create(token, parent_token, *, request_id, authorization, now):
            """Explicit fresh consent, not an automatic retry of an unknown call.

            Use the original native types, codec, immutable PDF and CAS API.
            The old known-failure parent is copied byte-for-byte to a separate
            authorization store. Both original checkpoints remain untouched.
            """
            if explicit_reissue_store is None:
                raise TaskError('显式重发的独立持久授权存储尚未接好。')
            from copy import deepcopy
            from .workbench_adapter import native_digest
            from auto_research.evidence.literature_checkpoint_runtime import (
                LiteratureExecutionState, encode_execution_state)
            from auto_research.evidence.literature_task_checkpoint import (
                LiteratureTaskCheckpoint, LiteratureContinuationLineage)
            from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
            old, parent = native_recovery(token), native_recovery(parent_token)
            checkpoint, original = old['checkpoint'], parent['checkpoint']
            state, job_state = old['execution'], old['job']
            if (authorization['unknown_checkpoint_sha256'] != sha256(encode_checkpoint(checkpoint))
                    or authorization['parent_checkpoint_sha256'] != sha256(encode_checkpoint(original))):
                raise TaskError('原密封断点已变化，未重发。')
            child_token = sha256(('explicit-unknown-reissue:'+token+':'+request_id).encode())
            native_id = 'literature_'+sha256(child_token.encode())
            store = explicit_reissue_store.reissue_store(request_id)
            try:
                copied = store.load(original.manifest.task_id)
            except Exception as exc:
                if getattr(exc,'code',None) != 'literature_checkpoint_not_found': raise
                store.create(original)
            else:
                if encode_checkpoint(copied) != encode_checkpoint(original):
                    raise TaskError('显式重发的原断点副本不一致。')
            prior = store.find_continuation(original.manifest.task_id)
            if prior is not None:
                if prior.manifest.task_id != native_id:
                    raise TaskError('显式重发已经使用，不能另建原生身份。')
                admitted = prior
            else:
                lifetime = min(86400, int(job_state.expires_at-job_state.issued_at))
                if lifetime <= 0: raise TaskError('原生任务授权期限无效。')
                content = snapshot_blobs.get(job_state.snapshot_ref,
                    expected_sha256=job_state.snapshot['pdf_sha256'])
                new_ref = snapshot_blobs.put(content, expected_sha256=job_state.snapshot['pdf_sha256'])
                payload = json.loads(state.job_state)
                payload.update(job_token=child_token, issued_at=now, expires_at=now+lifetime,
                    snapshot_ref=new_ref)
                payload['state_fingerprint'] = native_digest({k:v for k,v in payload.items()
                    if k != 'state_fingerprint'})
                encoded_job = canonical(payload)
                decoded_job = persistence.decode_job_private_state(encoded_job)
                lineage = replace(checkpoint.continuation_lineage,
                    continuation_request_id=request_id, proof_bytes=canonical({
                        'schema_version':'auto-lammps-explicit-unknown-reissue-v1',
                        'original_known_length_proof':json.loads(checkpoint.continuation_lineage.proof_bytes),
                        'authorization':deepcopy(authorization)}))
                admitted = LiteratureTaskCheckpoint(
                    manifest=replace(checkpoint.manifest, task_id=native_id,
                        issued_at=now, expires_at=now+lifetime),
                    revision=0, state='authorized', stage=checkpoint.stage,
                    receipts=original.receipts[:-1], spent_calls=original.spent_calls,
                    spent_tokens=original.spent_tokens, updated_at=now,
                    private_payload=encode_execution_state(LiteratureExecutionState(
                        job_state=encoded_job, stage_fingerprint=state.stage_fingerprint,
                        receipt_offset=state.receipt_offset, completed_results=state.completed_results)),
                    continuation_lineage=lineage)
                admitted = store.create_continuation(admitted)
            explicit_reissue_store.register(native_id, request_id, sha256(canonical(authorization)))
            child_state = execution.decode_execution_state(admitted.private_payload)
            child_job = persistence.decode_job_private_state(child_state.job_state)
            try: jobs.peek_stage(child_token, session_id=session_id)
            except job.LiteratureExtractionJobError as exc:
                if exc.code == 'literature_job_expired':
                    jobs.restore_private_state(child_state.job_state, session_id=session_id,
                        allow_authenticated_session_rebind=True)
                elif exc.code != 'literature_stage_busy': raise
            return {'job_token':child_token, 'checkpoint':admitted,
                'execution':child_state,'job':child_job}
        def native_source(paper_id):
            source = database.get_paper(paper_id)
            return context._read_stable_pdf_snapshot(str(source['pdf_path'])).sha256
        def native_paper_sources(paper_id):
            # Reuse the workbench's stable snapshot and PDF text reader. This
            # is the entire reference-side paper, not a selected answer or an
            # author-code excerpt assembled by a developer.
            source = database.get_paper(paper_id)
            snapshot = context._read_stable_pdf_snapshot(str(source['pdf_path']))
            pages = context._pdf_pages_from_snapshot(snapshot)
            return {'pdf_sha256': snapshot.sha256, 'sources': _reference_paper_sources(pages)}
        return WorkbenchRuntime(database, jobs, ports, session_id,
            prepared.PreparedOutbound, budget.LiteratureDerivedBudgetBusinessAIClient,
            persistence.decode_job_private_state, exports,
            six.list_current_data, visuals.list_visual_assets,
            visuals.visual_asset_image_path, native_capabilities, native_stage,
            native_recovery, native_source, native_paper_sources_reader=native_paper_sources,
            native_known_failure_planner=known_plan if known_length is not None else None,
            native_known_failure_creator=known_create if known_length is not None else None,
            native_unknown_reissue_creator=unknown_reissue_create
                if explicit_reissue_store is not None and known_length is not None else None)
    except (ImportError, AttributeError, TypeError, ValueError):
        raise TaskError('本机文献工作台依赖或持久服务尚未接好，未开始提取。') from None


class WorkbenchModelClient:
    """Adapt a product-configured DeepSeek client to the workbench JSON port.

    Each stage uses the existing project ModelCalls ledger. Options may tighten
    the saved per-call policy, never raise it. There is no credential fallback,
    provider selection, tool execution, network redirect or automatic retry.
    """
    def __init__(self, client, *, task, paper, source_sha256, request_id, adapter=None,
                 before_model_request=None):
        task_id(task); task_id(paper); task_id(request_id)
        _digest(source_sha256)
        if not isinstance(client, DeepSeekClient):
            raise TaskError('文献提取需要本项目已配置并记账的 DeepSeek 连接。')
        self.client = client
        self.identity = dict(task_id=task, paper_id=paper, role=HUMAN_ROLE,
            source_sha256=source_sha256, request_id=request_id)
        self.requests = []
        self._credential_digest = None
        self.adapter = adapter
        self.before_model_request = before_model_request

    def request_json(self, messages, *, task, max_tokens, thinking=None, temperature=None):
        if (task not in {'extraction', 'analysis'} or type(max_tokens) is not int
                or not 1 <= max_tokens <= self.client.calls.config.max_output_tokens
                or thinking not in (None, True, False)
                or type(thinking) not in (type(None), bool)
                or (temperature is not None and (type(temperature) not in (int, float)
                    or not math.isfinite(temperature) or not 0 <= temperature <= 1.5))):
            raise WorkbenchBridgeError('workbench_model_options_outside_policy')
        config = replace(self.client.calls.config, max_output_tokens=max_tokens)
        original = [dict(m) for m in messages]
        options = dict(task=task, max_tokens=max_tokens, thinking=thinking, temperature=temperature)
        if not isinstance(self.adapter, WorkbenchScientificAdapter):
            raise WorkbenchAdapterError('工作台科研 Adapter 缺失，未发送。')
        if any(self.adapter.identity.get(key) != value for key, value in self.identity.items()):
            raise WorkbenchAdapterError('工作台科研 Adapter 与本次模型请求身份不符，未发送。')
        prepared, proof = self.adapter.prepare(original, options)
        proof = self.adapter.validate(prepared, proof, original, options)
        payload = json.loads(request_body(config, prepared,
            model=self.client.model,
            thinking=self.client.thinking if thinking is None else thinking))
        if temperature is not None:
            payload['temperature'] = temperature
        body = canonical(payload)
        if len(body) > config.max_input_bytes:
            raise WorkbenchBridgeError('workbench_model_context_too_large')
        if self.before_model_request is not None:
            try:
                self.before_model_request()
            except Exception:
                raise WorkbenchBridgeError('workbench_context_changed') from None
        identifier = sha256(canonical({**self.identity, 'ordinal': len(self.requests) + 1}))[:32]
        self.client.calls.reserve(identifier, body)
        self.requests.append(identifier)
        receipt = dict(provider='deepseek', requested_model=self.client.model,
            purpose='literature_workbench_extraction', role=HUMAN_ROLE,
            task=task, request_sha256=sha256(body), state='not_sent',
            usage=None, transport_attempts=0, scientific_adapter=proof,
            request_bytes=len(body), max_output_tokens=max_tokens)
        try:
            key = self.client.key_reader()
            if (not isinstance(key, str) or not key or len(key) > 4096
                    or any(ord(c) < 33 or ord(c) > 126 for c in key)):
                raise ModelError('model_key_missing_or_invalid')
            credential_digest = sha256(key.encode())
            if self._credential_digest is not None and self._credential_digest != credential_digest:
                raise ModelError('workbench_model_connection_changed')
            self._credential_digest = credential_digest
            self.adapter.consumed(original, options)
            receipt.update(state='unknown', transport_attempts=1)
            status, content = self.client.transport(body, key, config.timeout_seconds)
            if type(status) is not int or not isinstance(content, bytes) or len(content) > MAX_RESPONSE_BYTES:
                raise ModelError('invalid_transport_response')
            receipt.update(http_status=status, response_sha256=sha256(content),
                **response_diagnostics(content))
            if status != 200:
                receipt['state'] = 'rejected'
                raise ModelError('provider_request_failed')
            receipt.update(state='response_invalid', usage=usage_from_response(content))
            result = parse_completion(content)
            receipt.update(state='completed', output_sha256=sha256(canonical(result)),
                structured_output=result)
        except Exception as exc:
            receipt['error'] = (str(exc) if type(exc) is ModelError and str(exc) in {
                'model_key_missing_or_invalid', 'workbench_model_connection_changed',
                'invalid_transport_response', 'provider_request_failed', 'invalid_json',
                'invalid_response', 'incomplete_generation', 'empty_or_unexpected_output',
                'json_object_required', 'response_too_large', 'model_tls_trust_unavailable'}
                else 'model_transport_unknown')
            self.client.calls.record(identifier, receipt)
            raise WorkbenchBridgeError('workbench_model_request_not_completed') from None
        self.client.calls.record(identifier, receipt)
        return result


class WorkbenchExtractionBridge:
    def __init__(self, papers, runtime, *, recoveries=()):
        self.papers, self.runtime = papers, runtime
        self.recoveries = {}
        for binding in recoveries:
            if (not isinstance(binding, WorkbenchRecoveryBinding)
                    or binding.request_id in self.recoveries):
                raise TaskError('受信工作台恢复登记无效或重复。')
            self.recoveries[binding.request_id] = binding
        if len({b.job_token for b in self.recoveries.values()}) != len(self.recoveries):
            raise TaskError('受信工作台恢复登记重复使用原生任务。')
        with papers.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_intents ('
                'id TEXT PRIMARY KEY, task_id TEXT NOT NULL, paper_id TEXT NOT NULL, '
                'binding_sha256 TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_receipts ('
                'id TEXT PRIMARY KEY REFERENCES workbench_extraction_intents(id), document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_continuations ('
                'id TEXT PRIMARY KEY REFERENCES workbench_extraction_intents(id), '
                'document TEXT NOT NULL, document_sha256 TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_recoveries ('
                'id TEXT PRIMARY KEY REFERENCES workbench_extraction_intents(id), '
                'source_request_id TEXT NOT NULL UNIQUE REFERENCES workbench_extraction_intents(id), '
                'document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_unknown_reissues ('
                'id TEXT PRIMARY KEY, unknown_request_id TEXT NOT NULL UNIQUE, '
                'document TEXT NOT NULL, document_sha256 TEXT NOT NULL)')
            for table in ('workbench_extraction_unknown_reissues', 'workbench_extraction_intents', 'workbench_extraction_receipts',
                    'workbench_extraction_continuations', 'workbench_extraction_recoveries'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} '
                        f'BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, \'immutable workbench history\'); END')

    def _paper(self, identifier, binding):
        task_id(identifier)
        if not isinstance(binding, WorkbenchPaperBinding) or binding.role != HUMAN_ROLE:
            raise TaskError('需要受信的人类论文参考绑定。')
        # PaperStore.get also loads linked task documents. Read only the paper
        # identity and relation, so frozen B conditions never enter this port.
        with self.papers.tasks.transaction() as db:
            registered = self.papers._read(db, binding.paper_id)
            if not db.execute('SELECT 1 FROM paper_tasks WHERE paper_id=? AND task_id=?',
                    (binding.paper_id, identifier)).fetchone():
                raise TaskError('工作台来源不属于这项任务的登记论文。')
        paper = {key: registered[key] for key in ('id', 'title', 'doi')}
        source = self.runtime.database.get_paper(binding.workbench_paper_id)
        if (not isinstance(source, dict) or source.get('title') != paper['title']
                or doi_text(source.get('doi', '')) != paper['doi']
                or source.get('pdf_sha256') != binding.pdf_sha256
                or not source.get('pdf_path')):
            raise TaskError('工作台论文题名、DOI 或 PDF 摘要与登记来源不一致。')
        return paper

    def history(self, identifier):
        task_id(identifier)
        with self.papers.tasks.transaction() as db:
            rows = db.execute('SELECT i.document intent, r.document receipt '
                'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
                'ON r.id=i.id WHERE i.task_id=? ORDER BY i.rowid', (identifier,)).fetchall()
        return [json.loads(row['receipt']) if row['receipt'] else
            {**json.loads(row['intent']), 'state': 'unknown', 'automatic_retry': False} for row in rows]

    def _completed_unknown_reissues(self, db, identifier, *, repair_identity=None):
        """Prove publication without settling or rewriting an unknown charge.

        This is a read-only projection. Only a separately authorized, unique
        child with an authenticated native completion and current published
        data can supersede the old data-flow blocker. It never authorizes a
        rescan, another unknown reissue, or a retry of either original call.
        """
        resolved = []
        rows = db.execute('SELECT u.unknown_request_id FROM workbench_extraction_unknown_reissues u '
            'JOIN workbench_extraction_intents i ON i.id=u.unknown_request_id WHERE i.task_id=?',
            (identifier,)).fetchall()
        for row in rows:
            try:
                from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
                claim = self._unknown_reissue_claim(db, row['unknown_request_id'])
                old, unknown = self._request(db, row['unknown_request_id'])
                child, receipt = self._request(db, claim['request_id'])
                identity_keys = ('task_id', 'paper_id', 'source_sha256', 'model',
                    'connection_revision', 'credential_generation', 'role', 'title', 'doi')
                if (claim.get('task_id') != identifier or old.get('task_id') != identifier
                        or unknown.get('state') != 'failed_or_unknown'
                        or claim.get('authorization_kind') != 'explicit_user_unknown_reissue_once'
                        or claim.get('supplier_checked') is not True
                        or claim.get('retain_unknown_charge') is not True
                        or claim.get('automatic_retry') is not False
                        or child.get('unknown_reissue_of') != old['request_id']
                        or any(child.get(k) != old.get(k) for k in identity_keys)
                        or any(claim.get(k) != old.get(k) for k in
                            ('task_id', 'paper_id', 'source_sha256', 'connection_revision', 'credential_generation'))
                        or receipt.get('state') not in {'completed', 'completed_with_limitations'}
                        or receipt.get('workbench_status') != 'completed'
                        or receipt.get('authorization_kind') != claim['authorization_kind']
                        or receipt.get('supplier_checked') is not True
                        or receipt.get('retain_unknown_charge') is not True):
                    continue
                saved = self._continuation(db, child['request_id'])
                prior = self._continuation(db, old['request_id'])
                if (not saved or not prior
                        or saved.get('intent_sha256') != sha256(canonical(child))
                        or prior.get('intent_sha256') != sha256(canonical(old))
                        or saved.get('job_token') != sha256(('explicit-unknown-reissue:'
                            + prior['job_token'] + ':' + child['request_id']).encode())
                        or any(saved.get(k) != claim.get(k) for k in
                            ('model_ledger_sha256', 'model_policy_sha256', 'native_capability_sha256'))
                        or saved.get('workbench_paper_id') != prior.get('workbench_paper_id')
                        or saved.get('source_sha256') != child['source_sha256']
                        or saved.get('session_sha256') != sha256(self.runtime.session_id.encode())):
                    continue
                if repair_identity is not None and (any(
                        repair_identity.get(k) != child.get(k) for k in identity_keys)
                        or any(repair_identity.get(k) != saved.get(k) for k in
                            ('model_ledger_sha256', 'model_policy_sha256'))):
                    continue
                binding = WorkbenchPaperBinding(child['paper_id'], saved['workbench_paper_id'],
                    child['source_sha256'])
                registered = self.papers._read(db, binding.paper_id)
                source = self.runtime.database.get_paper(binding.workbench_paper_id)
                if (not db.execute('SELECT 1 FROM paper_tasks WHERE paper_id=? AND task_id=?',
                            (binding.paper_id, identifier)).fetchone()
                        or any(registered.get(k) != child[k] for k in ('title', 'doi'))
                        or not isinstance(source, dict) or source.get('title') != child['title']
                        or doi_text(source.get('doi', '')) != child['doi']
                        or source.get('pdf_sha256') != binding.pdf_sha256):
                    continue
                native = self.runtime.native_recovery_reader(saved['job_token'])
                sealed_unknown = self.runtime.native_recovery_reader(prior['job_token'])
                original = self.runtime.native_recovery_reader(claim['parent_job_token'])
                summary = native['execution'].completion_result
                counts = receipt.get('counts', {})
                published = counts.get('published_item_count', 0) + counts.get('existing_item_count', 0)
                if (native['checkpoint'].state != 'completed'
                        or (sha256(canonical(self.runtime.native_capability_reader())) != claim['native_capability_sha256']
                            and claim['native_capability_sha256'] not in
                                self.runtime.completed_native_capability_allowlist)
                        or self.runtime.native_source_reader(binding.workbench_paper_id) != binding.pdf_sha256
                        or sha256(encode_checkpoint(sealed_unknown['checkpoint'])) != claim['unknown_checkpoint_sha256']
                        or sha256(encode_checkpoint(original['checkpoint'])) != claim['parent_checkpoint_sha256']
                        or native['job'].paper_id != binding.workbench_paper_id
                        or native['job'].snapshot['pdf_sha256'] != binding.pdf_sha256
                        or summary is None or sha256(canonical(summary)) != receipt.get('source_receipt_sha256')
                        or summary.get('status') != 'completed'
                        or summary.get('schema_version') != 'literature-extraction-commit-result-v2'
                        or any(summary.get(k) != v for k, v in counts.items())
                        or type(published) is not int or published < 1
                        or len(self.runtime.data_items(self.runtime.database, binding.workbench_paper_id)) < published):
                    continue
                resolved.append(dict(unknown_request_id=old['request_id'],
                    superseded_by=child['request_id'], task_id=identifier,
                    paper_id=child['paper_id'], source_sha256=child['source_sha256'],
                    data_continuation_completed=True, unknown_charge_unsettled=True,
                    repair_visuals_only=True, automatic_retry=False))
            except Exception:
                # Missing, changed or unauthenticated evidence never lifts a
                # blocker. Status reads cannot mutate native recovery state.
                continue
        return resolved

    def completed_unknown_reissues(self, identifier):
        task_id(identifier)
        with self.papers.tasks.transaction() as db:
            return self._completed_unknown_reissues(db, identifier)

    @staticmethod
    def _model_failure_details(record, calls):
        """Read safe provider rejection evidence; never infer a retry permit."""
        ids = record.get('model_request_ids')
        if record.get('state') not in {'failed', 'failed_or_unknown'} or not isinstance(ids, list) or not ids:
            return None
        try:
            base = {key: record[key] for key in
                    ('task_id', 'paper_id', 'role', 'source_sha256', 'request_id')}
            if base['role'] != HUMAN_ROLE or ids[-1] != sha256(canonical({**base, 'ordinal': len(ids)}))[:32]:
                return None
            saved = calls.lookup(ids[-1]) or {}
            receipt = saved.get('receipt') or {}
            identity = (receipt.get('scientific_adapter') or {}).get('identity') or {}
            status = receipt.get('http_status')
            if (receipt.get('state') != 'rejected' or type(status) is not int or not 400 <= status <= 599
                    or receipt.get('transport_attempts') != 1 or receipt.get('provider') != 'deepseek'
                    or receipt.get('purpose') != 'literature_workbench_extraction'
                    or receipt.get('request_sha256') != saved.get('request_sha256')
                    or any(identity.get(key) != value for key, value in base.items())):
                return None
            reasons = {401: '身份验证失败，请检查本项目 API 连接',
                       402: '账户余额不足，请补足本项目 API 账户余额',
                       403: '账户权限不足，请检查模型访问权限',
                       429: '请求受到限流，请稍后核对再继续'}
            reason = reasons.get(status, '供应商未接受本次请求，请核对服务状态')
            return dict(http_status=status, state='rejected', transport_attempts=1,
                error_code='provider_insufficient_balance' if status == 402 else 'provider_rejected',
                message=f'DeepSeek 拒绝本次提取请求（HTTP {status}）：{reason}。'
                    '已保存答复、断点及失败记录保留；未发布 P，没有自动重试。')
        except (ValueError, KeyError, TypeError, OSError):
            return None

    def history_with_model_diagnostics(self, identifier, calls):
        """Project bound ledger diagnostics without rewriting old receipts."""
        records = self.history(identifier)
        for record in records:
            failure = self._model_failure_details(record, calls)
            if failure:
                record['provider_failure'] = failure
                record['original_error'] = record.get('error')
                record['error'] = failure['message']
        return records

    @staticmethod
    def _request(db, request_id):
        row = db.execute('SELECT i.document intent, i.binding_sha256, r.document receipt '
            'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
            'ON r.id=i.id WHERE i.id=?', (request_id,)).fetchone()
        if not row or not row['receipt']:
            raise TaskError('原提取状态未知；请先核对，不能恢复收费。')
        intent, receipt = json.loads(row['intent']), json.loads(row['receipt'])
        if (sha256(canonical(intent)) != row['binding_sha256']
                or any(receipt.get(k) != v for k, v in intent.items())
                or intent.get('request_id') != request_id):
            raise TaskError('原文献提取身份或回执摘要不一致，不能恢复。')
        return intent, receipt

    @staticmethod
    def _model_binding(client):
        if not isinstance(client, DeepSeekClient):
            raise TaskError('文献提取需要本项目已配置并记账的 DeepSeek 连接。')
        return {'model_ledger_sha256': sha256(str(client.calls.path.resolve()).encode()),
            'model_policy_sha256': sha256(canonical(asdict(client.calls.config)))}

    def _continuation(self, db, request_id):
        row = db.execute('SELECT document, document_sha256 '
            'FROM workbench_extraction_continuations WHERE id=?', (request_id,)).fetchone()
        if row:
            data = json.loads(row['document'])
            if (sha256(canonical(data)) != row['document_sha256']
                    or data.get('request_id') != request_id):
                raise TaskError('原生工作台恢复绑定摘要不一致，不能恢复。')
            return data
        return None

    def _recovery_context(self, identifier, request_id, binding, client, *,
                          connection_revision, credential_generation):
        try:
            return self._standard_recovery_context(identifier, request_id, binding, client,
                connection_revision=connection_revision, credential_generation=credential_generation)
        except TaskError as original_error:
            if (not callable(getattr(self.runtime, 'native_known_failure_planner', None))
                    or not callable(getattr(self.runtime, 'native_known_failure_creator', None))):
                raise
            from .workbench_known_failure import known_failure_context
            try:
                context = known_failure_context(self, identifier, request_id, binding, client,
                    connection_revision=connection_revision, credential_generation=credential_generation)
                return context.recovery_metadata
            except TaskError:
                # A child may recover an unpaid local interruption under its
                # existing identity, but can never create a second child.
                return self._known_child_recovery_context(identifier, request_id, binding, client,
                    connection_revision=connection_revision, credential_generation=credential_generation,
                    original_error=original_error)

    def _standard_recovery_context(self, identifier, request_id, binding, client, *,
                          connection_revision, credential_generation):
        """Validate private history against the original sealed native state.

        No starter, preflight, lease acquisition or provider is invoked here.
        The native runner alone replays its successful prefix during execute.
        """
        task_id(request_id)
        paper = self._paper(identifier, binding)
        try:
            if (not callable(self.runtime.native_source_reader)
                    or self.runtime.native_source_reader(binding.workbench_paper_id) != binding.pdf_sha256):
                raise ValueError('source changed')
        except Exception:
            raise TaskError('原 PDF 字节或受信来源读取已变化；未恢复或收费。') from None
        model_binding = self._model_binding(client)
        identity = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'],
            doi=paper['doi'], role=HUMAN_ROLE, source_sha256=binding.pdf_sha256,
            model=client.model, connection_revision=connection_revision,
            credential_generation=credential_generation)
        with self.papers.tasks.transaction() as db:
            original, failure = self._request(db, request_id)
            if db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('delete','finish')",
                    (identifier,)).fetchone():
                raise TaskError('任务已结束，不能恢复文献提取。')
            if (failure.get('state') != 'failed'
                    or any(original.get(k) != v for k, v in identity.items())):
                raise TaskError('只能恢复同一任务、论文、PDF 和模型连接的已核定失败。')
            if db.execute('SELECT 1 FROM workbench_extraction_recoveries '
                    'WHERE source_request_id=?', (request_id,)).fetchone():
                raise TaskError('原提取已发起恢复；请查看该恢复记录，不能重复收费。')
            continuation = self._continuation(db, request_id)
            trusted = self.recoveries.get(request_id)
            if not continuation and not trusted:
                raise TaskError('缺少受信的原生工作台恢复绑定；未重新提取或收费。')
            if trusted:
                if (trusted.intent_sha256 != sha256(canonical(original))
                        or trusted.receipt_sha256 != sha256(canonical(failure))
                        or any(getattr(trusted, k) != v for k, v in model_binding.items())
                        or (continuation and (continuation['job_token'] != trusted.job_token
                            or continuation['native_capability_sha256'] !=
                                trusted.prior_native_capability_sha256))):
                    raise TaskError('受信恢复登记与原失败、模型账本或原生身份不一致。')
                token = trusted.job_token
                expected_native = trusted.native_capability_sha256
            else:
                if (continuation['intent_sha256'] != sha256(canonical(original))
                        or any(continuation.get(k) != v for k, v in model_binding.items())
                        or continuation.get('workbench_paper_id') != binding.workbench_paper_id
                        or continuation.get('source_sha256') != binding.pdf_sha256
                        or continuation.get('session_sha256') != sha256(self.runtime.session_id.encode())):
                    raise TaskError('原生工作台恢复绑定与当前来源或连接不一致。')
                token = continuation['job_token']
                expected_native = continuation['native_capability_sha256']
            # A recovery is a new append-only action. Walk the same token's
            # history to account for every successful call, including earlier
            # explicitly authorized recoveries, without copying target data.
            lineage, seen = [], set()
            cursor = request_id
            while cursor is not None:
                if cursor in seen:
                    raise TaskError('工作台恢复历史不完整，不能恢复。')
                seen.add(cursor)
                old_intent, old_receipt = self._request(db, cursor)
                old_continuation = self._continuation(db, cursor)
                old_trusted = self.recoveries.get(cursor)
                if (old_receipt.get('state') != 'failed'
                        or any(old_intent.get(k) != v for k, v in identity.items())):
                    raise TaskError('工作台恢复历史属于其他来源或未知调用。')
                old_native = (old_continuation['native_capability_sha256'] if old_continuation
                    else old_trusted.prior_native_capability_sha256 if old_trusted else None)
                if old_native is None or (old_continuation and old_continuation['job_token'] != token):
                    raise TaskError('工作台恢复历史缺少受信的原生绑定。')
                lineage.append((old_intent, old_receipt, old_native))
                cursor = old_intent.get('resume_of')
            unknown = db.execute('SELECT i.id, i.document intent, r.document receipt '
                'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
                'ON r.id=i.id WHERE i.paper_id=?', (paper['id'],)).fetchall()
            for row in unknown:
                if json.loads(row['intent']).get('source_sha256') == binding.pdf_sha256:
                    receipt = json.loads(row['receipt']) if row['receipt'] else None
                    if receipt is None or receipt.get('state') in {'unknown', 'failed_or_unknown'}:
                        raise TaskError('同一 PDF 有未知提取状态；请先核对，不能恢复收费。')
        native_adapter = WorkbenchScientificAdapter({
            **{k: identity[k] for k in ('task_id', 'paper_id', 'title', 'doi', 'role', 'source_sha256')},
            'request_id': request_id, 'workbench_paper_id': binding.workbench_paper_id},
            capability_reader=self.runtime.native_capability_reader,
            stage_reader=lambda: None)
        current_native = sha256(canonical(native_adapter.installed))
        if current_native != expected_native:
            raise TaskError('工作台依赖版本已变化；须受信登记修复版本后才能恢复。')
        if not callable(self.runtime.native_recovery_reader):
            raise TaskError('工作台密封检查点读取接口未接好，不能恢复。')
        try:
            native = self.runtime.native_recovery_reader(token)
            checkpoint, execution, job = native['checkpoint'], native['execution'], native['job']
            manifest = checkpoint.manifest
            if (job.token != token or job.paper_id != binding.workbench_paper_id
                    or dict(job.paper) != {'title': paper['title'], 'doi': paper['doi']}
                    or job.snapshot['pdf_sha256'] != binding.pdf_sha256
                    or manifest.pdf_snapshot_fingerprint != job.snapshot['content_fingerprint']
                    or manifest.task_id != 'literature_' + sha256(token.encode())
                    or manifest.session_digest != sha256(self.runtime.session_id.encode())
                    or manifest.provider_id != 'deepseek'
                    or manifest.runtime_revision != connection_revision
                    or manifest.credential_generation != credential_generation
                    or tuple(manifest.task_models) != (('analysis', client.model), ('extraction', client.model))
                    or manifest.executor_id != native_adapter.installed['executor']['id']
                    or manifest.executor_version != native_adapter.installed['executor']['version']
                    or checkpoint.state in {'completed', 'outcome_unknown', 'cancelled'}
                    or manifest.expires_at <= time.time()
                    or any(r.state not in {'succeeded', 'planned'} for r in checkpoint.receipts)):
                raise ValueError('native recovery identity')
            ledger = []
            for old_intent, old_receipt, old_native in reversed(lineage):
                call_ids = old_receipt['model_request_ids']
                if not isinstance(call_ids, list) or len(set(call_ids)) != len(call_ids):
                    raise ValueError('ledger history')
                base = {k: old_intent[k] for k in ('task_id', 'paper_id', 'role', 'source_sha256', 'request_id')}
                proof_identity = {**base, 'title': paper['title'], 'doi': paper['doi'],
                    'workbench_paper_id': binding.workbench_paper_id}
                for ordinal, call_id in enumerate(call_ids, 1):
                    if call_id != sha256(canonical({**base, 'ordinal': ordinal}))[:32]:
                        raise ValueError('foreign call')
                    record = client.calls.lookup(call_id)
                    receipt = record['receipt'] if record else None
                    proof = receipt.get('scientific_adapter', {}) if receipt else {}
                    if (not receipt or receipt.get('state') != 'completed'
                            or receipt.get('provider') != 'deepseek'
                            or receipt.get('requested_model') != client.model
                            or receipt.get('purpose') != 'literature_workbench_extraction'
                            or receipt.get('role') != HUMAN_ROLE
                            or receipt.get('transport_attempts') != 1
                            or receipt.get('request_sha256') != record['request_sha256']
                            or receipt.get('output_sha256') != sha256(canonical(receipt['structured_output']))
                            or proof.get('name') != 'literature_workbench_scientific_adapter'
                            or proof.get('adapter_source_sha256') != native_adapter.adapter_sha256
                            or proof.get('identity') != proof_identity
                            or proof.get('native_contract_sha256') != old_native
                            or sha256(canonical(proof.get('native_capabilities'))) != old_native):
                        raise ValueError('unverified ledger')
                    ledger.append(receipt)
            succeeded = [r for r in checkpoint.receipts if r.state == 'succeeded']
            if len(ledger) != len(succeeded) or checkpoint.spent_calls != len(ledger):
                raise ValueError('unreconciled calls')
            for receipt, native_receipt in zip(ledger, succeeded):
                proof = receipt['scientific_adapter']
                if (proof['native_call_digest'] != native_receipt.call_digest
                        or proof['stage'] != native_receipt.stage
                        or receipt['task'] != native_receipt.task
                        or native_receipt.model != client.model
                        or native_digest(receipt['structured_output']) != native_receipt.result_digest):
                    raise ValueError('checkpoint call mismatch')
            offset = execution.receipt_offset
            relevant = checkpoint.receipts[offset:]
            prefix = execution.completed_results
            stage = job.stage
            if (not 0 <= offset <= len(succeeded)
                    or execution.stage_fingerprint != stage['stage_fingerprint']
                    or checkpoint.stage != stage['name']
                    or len(prefix) != sum(r.state == 'succeeded' for r in relevant)
                    or len(prefix) > len(stage['calls'])):
                raise ValueError('native prefix')
            for index, result in enumerate(prefix):
                if (relevant[index].state != 'succeeded'
                        or native_digest(result) != relevant[index].result_digest
                        or stage['calls'][index]['call_digest'] != relevant[index].call_digest):
                    raise ValueError('native prefix mismatch')
        except Exception:
            raise TaskError('原失败、成功调用账本与密封检查点未完全核对；未恢复或收费。') from None
        return {'job_token': token, 'reused_model_calls': len(ledger),
            'prefix_count': len(prefix), 'stage_fingerprint': execution.stage_fingerprint,
            'native_capability_sha256': current_native,
            'prior_native_capability_sha256': lineage[0][2],
            'intent_sha256': sha256(canonical(original)),
            'receipt_sha256': sha256(canonical(failure)),
            'checkpoint_revision': checkpoint.revision}

    @staticmethod
    def _known_child_token(parent_token, request_id):
        return sha256(canonical({'known_length_child': request_id,
            'parent_token': parent_token, 'parent_native_task_id': 'literature_' + sha256(parent_token.encode())}))

    def _known_child_recovery_context(self, identifier, request_id, binding, client, *,
                                      connection_revision, credential_generation, original_error):
        """Resume the same proven child after a local, fully known interruption."""
        from .workbench_known_failure import known_failure_context
        try:
            paper = self._paper(identifier, binding)
            model_binding = self._model_binding(client)
            identity = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'], doi=paper['doi'],
                role=HUMAN_ROLE, source_sha256=binding.pdf_sha256, model=client.model,
                connection_revision=connection_revision, credential_generation=credential_generation)
            with self.papers.tasks.transaction() as db:
                latest, failure = self._request(db, request_id)
                if failure['state'] != 'failed' or any(latest.get(k) != v for k, v in identity.items()):
                    raise ValueError('unknown child request')
                if db.execute('SELECT 1 FROM workbench_extraction_recoveries WHERE source_request_id=?',
                        (request_id,)).fetchone():
                    raise ValueError('child request already recovered')
                history, cursor, seen = [], request_id, set()
                while True:
                    if cursor in seen:
                        raise ValueError('cycle')
                    seen.add(cursor)
                    intent, receipt = self._request(db, cursor)
                    continuation = self._continuation(db, cursor)
                    claim = db.execute('SELECT source_request_id,document FROM workbench_extraction_recoveries WHERE id=?',
                        (cursor,)).fetchone()
                    if (receipt.get('state') != 'failed' or continuation is None or claim is None
                            or any(intent.get(k) != v for k, v in identity.items())
                            or continuation.get('intent_sha256') != sha256(canonical(intent))
                            or any(continuation.get(k) != v for k, v in model_binding.items())
                            or continuation.get('session_sha256') != sha256(self.runtime.session_id.encode())
                            or continuation.get('source_sha256') != binding.pdf_sha256
                            or continuation.get('workbench_paper_id') != binding.workbench_paper_id
                            or claim['source_request_id'] != intent.get('resume_of')):
                        raise ValueError('child history')
                    consent = json.loads(claim['document'])
                    history.append((intent, receipt, continuation))
                    if consent.get('kind') == 'known_length_continuation':
                        origin, parent_request = cursor, intent['resume_of']
                        break
                    if consent.get('kind') != 'known_length_existing_child':
                        raise ValueError('not a known child')
                    cursor = intent['resume_of']
            parent = known_failure_context(self, identifier, parent_request, binding, client,
                connection_revision=connection_revision, credential_generation=credential_generation,
                continuation_request_id=origin)
            token = self._known_child_token(parent.parent_token, origin)
            if any(saved['job_token'] != token for _, _, saved in history):
                raise ValueError('foreign child')
            native = self.runtime.native_recovery_reader(token)
            checkpoint, execution, job = native['checkpoint'], native['execution'], native['job']
            manifest, lineage = checkpoint.manifest, checkpoint.continuation_lineage
            current_native = parent.recovery_metadata['native_capability_sha256']
            if (lineage is None or lineage.parent_task_id != parent.proof_kwargs['native_task_id']
                    or lineage.parent_revision != parent.proof_kwargs['native_revision']
                    or lineage.continuation_request_id != origin
                    or tuple(lineage.prefix_result_digests) != parent.prefix_digests
                    or json.loads(lineage.proof_bytes) != {'schema_version': 'literature-known-length-proof-v1', **parent.proof_kwargs}
                    or manifest.task_id != 'literature_' + sha256(token.encode())
                    or manifest.session_digest != sha256(self.runtime.session_id.encode())
                    or manifest.provider_id != 'deepseek' or manifest.runtime_revision != connection_revision
                    or manifest.credential_generation != credential_generation
                    or tuple(manifest.task_models) != (('analysis', client.model), ('extraction', client.model))
                    or manifest.executor_id != 'literature_extraction_executor' or manifest.executor_version != 'v1'
                    or manifest.pdf_snapshot_fingerprint != parent.proof_kwargs['source_fingerprint']
                    or job.snapshot['pdf_sha256'] != binding.pdf_sha256 or job.token != token
                    or job.paper_id != binding.workbench_paper_id
                    or dict(job.paper) != {'title': paper['title'], 'doi': paper['doi']}
                    or manifest.max_calls != parent.recovery_metadata['max_calls']
                    or manifest.max_tokens != parent.recovery_metadata['max_tokens']
                    or manifest.expires_at <= time.time()
                    or checkpoint.state not in {'authorized','paused','validated','failed'}
                    or checkpoint.lease_owner_digest is not None or checkpoint.lease_expires_at is not None
                    or any(r.state not in {'succeeded','planned'} for r in checkpoint.receipts)):
                raise ValueError('child identity')
            original_native = self.runtime.native_recovery_reader(parent.parent_token)['checkpoint']
            reused = parent.recovery_metadata['reused_model_calls']
            if checkpoint.receipts[:reused] != original_native.receipts[:-1]:
                raise ValueError('inherited prefix changed')
            ledger = []
            for intent, receipt, saved in reversed(history):
                if saved['native_capability_sha256'] != current_native:
                    raise ValueError('child capability changed')
                ids = receipt.get('model_request_ids')
                if not isinstance(ids, list) or len(ids) != len(set(ids)):
                    raise ValueError('child ledger')
                base = {k: intent[k] for k in ('task_id','paper_id','role','source_sha256','request_id')}
                adapter_identity = {**base, 'title': paper['title'],'doi': paper['doi'],
                    'workbench_paper_id': binding.workbench_paper_id}
                adapter = WorkbenchScientificAdapter(adapter_identity,
                    capability_reader=self.runtime.native_capability_reader,stage_reader=lambda:None)
                for ordinal, call_id in enumerate(ids,1):
                    if call_id != sha256(canonical({**base,'ordinal':ordinal}))[:32]:
                        raise ValueError('foreign paid call')
                    row = client.calls.lookup(call_id) or {}; result = row.get('receipt') or {}
                    proof = result.get('scientific_adapter') or {}
                    usage = result.get('usage') or {}
                    if (result.get('state') != 'completed' or result.get('http_status') != 200
                            or result.get('finish_reason') != 'stop' or result.get('provider') != 'deepseek'
                            or result.get('requested_model') != client.model or result.get('role') != HUMAN_ROLE
                            or result.get('purpose') != 'literature_workbench_extraction'
                            or result.get('transport_attempts') != 1
                            or result.get('request_sha256') != row.get('request_sha256')
                            or result.get('output_sha256') != sha256(canonical(result.get('structured_output')))
                            or _HASH.fullmatch(str(result.get('response_sha256'))) is None
                            or any(type(usage.get(k)) is not int or usage[k] < 0
                                for k in ('prompt_tokens','completion_tokens','total_tokens'))
                            or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']
                            or proof.get('identity') != adapter_identity
                            or proof.get('adapter_source_sha256') != adapter.adapter_sha256
                            or proof.get('native_contract_sha256') != current_native
                            or sha256(canonical(proof.get('native_capabilities'))) != current_native):
                        raise ValueError('child response not verified')
                    ledger.append(result)
            active = [r for r in checkpoint.receipts[reused:] if r.state == 'succeeded']
            if (len(ledger) != len(active) or checkpoint.spent_calls != reused + len(ledger) + 1
                    or checkpoint.spent_tokens != sum(r.max_tokens for r in active)
                        + sum(r.max_tokens for r in original_native.receipts)):
                raise ValueError('inherited fee mismatch')
            for receipt, native_receipt in zip(ledger, active):
                proof = receipt['scientific_adapter']
                if (proof.get('native_call_digest') != native_receipt.call_digest
                        or proof.get('stage') != native_receipt.stage or receipt.get('task') != native_receipt.task
                        or receipt.get('max_output_tokens') != native_receipt.max_tokens
                        or native_digest(receipt['structured_output']) != native_receipt.result_digest):
                    raise ValueError('child paid call mismatch')
            prefix, offset, stage = execution.completed_results, execution.receipt_offset, job.stage
            relevant = checkpoint.receipts[offset:]
            if (not 0 <= offset <= len(checkpoint.receipts)
                    or execution.stage_fingerprint != stage['stage_fingerprint'] or checkpoint.stage != stage['name']
                    or len(relevant) > len(stage['calls'])
                    or len(prefix) != sum(r.state == 'succeeded' for r in relevant)):
                raise ValueError('child sealed prefix')
            for i,result in enumerate(prefix):
                if (relevant[i].state != 'succeeded' or native_digest(result) != relevant[i].result_digest
                        or stage['calls'][i]['call_digest'] != relevant[i].call_digest):
                    raise ValueError('child stage mismatch')
            return {'kind':'known_length_existing_child','job_token':token,'reused_model_calls':reused+len(ledger),
                'prefix_count':len(prefix),'inherited_failed_model_calls':1,
                'inherited_failed_tokens':parent.recovery_metadata['inherited_failed_tokens'],
                'stage_fingerprint':execution.stage_fingerprint,'native_capability_sha256':current_native,
                'prior_native_capability_sha256':current_native,'intent_sha256':sha256(canonical(latest)),
                'receipt_sha256':sha256(canonical(failure)),'checkpoint_revision':checkpoint.revision,
                'max_calls':manifest.max_calls,'max_tokens':manifest.max_tokens}
        except Exception:
            raise original_error from None

    def _unknown_reissue_claim(self, db, unknown_request_id):
        row = db.execute('SELECT id,document,document_sha256 FROM workbench_extraction_unknown_reissues '
            'WHERE unknown_request_id=?', (unknown_request_id,)).fetchone()
        if row is None: return None
        document = json.loads(row['document'])
        if (sha256(canonical(document)) != row['document_sha256']
                or document.get('request_id') != row['id']
                or document.get('unknown_reissue_of') != unknown_request_id):
            raise TaskError('显式重发授权记录不一致。')
        return document

    def _unknown_reissue_context(self, identifier, unknown_request_id, binding, client, *,
            connection_revision, credential_generation, authorization_request_id=None):
        """Authenticate a zero-progress unknown child against its real parent.

        This does not change the old outcome, renew its expiry, lease a native
        task, allocate a checkpoint or send any provider request. The explicit
        user action authorizes a new child with the same successful prefix.
        """
        from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
        task_id(unknown_request_id)
        if authorization_request_id is not None: task_id(authorization_request_id)
        error = TaskError('未知断点、成功答复、原文或连接未完全核对；未重发或收费。')
        def require(value):
            if not value: raise error
        paper = self._paper(identifier, binding)
        runtime = self.runtime
        require(callable(runtime.native_unknown_reissue_creator)
            and callable(runtime.native_recovery_reader) and callable(runtime.native_source_reader)
            and runtime.native_source_reader(binding.workbench_paper_id) == binding.pdf_sha256)
        model_binding = self._model_binding(client)
        identity = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'], doi=paper['doi'],
            role=HUMAN_ROLE, source_sha256=binding.pdf_sha256, model=client.model,
            connection_revision=connection_revision, credential_generation=credential_generation)
        with self.papers.tasks.transaction() as db:
            old_intent, old_receipt = self._request(db, unknown_request_id)
            require(old_receipt.get('state') == 'failed_or_unknown'
                and all(old_intent.get(k) == v for k,v in identity.items())
                and old_intent.get('resume_of') is not None
                and 'unknown_reissue_of' not in old_intent)
            claim = self._unknown_reissue_claim(db, unknown_request_id)
            require(claim is None or claim['request_id'] == authorization_request_id)
            require(not db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('delete','finish')",
                (identifier,)).fetchone())
            child_binding = self._continuation(db, unknown_request_id)
            require(child_binding is not None
                and child_binding['intent_sha256'] == sha256(canonical(old_intent))
                and all(child_binding.get(k) == v for k,v in model_binding.items())
                and child_binding['workbench_paper_id'] == binding.workbench_paper_id
                and child_binding['source_sha256'] == binding.pdf_sha256
                and child_binding['session_sha256'] == sha256(runtime.session_id.encode()))
            parent_id = old_intent['resume_of']; parent_intent, parent_receipt = self._request(db, parent_id)
            require(parent_receipt['state'] == 'failed'
                and all(parent_intent.get(k) == v for k,v in identity.items()))
            parent_binding = self._continuation(db, parent_id)
            trusted = self.recoveries.get(parent_id)
            require(parent_binding is not None)
            if trusted:
                require(trusted.intent_sha256 == sha256(canonical(parent_intent))
                    and trusted.receipt_sha256 == sha256(canonical(parent_receipt))
                    and all(getattr(trusted,k) == v for k,v in model_binding.items())
                    and trusted.job_token == parent_binding['job_token']
                    and trusted.prior_native_capability_sha256 == parent_binding['native_capability_sha256'])
                expected_native = trusted.native_capability_sha256
            else:
                require(parent_binding['intent_sha256'] == sha256(canonical(parent_intent))
                    and all(parent_binding.get(k) == v for k,v in model_binding.items()))
                expected_native = parent_binding['native_capability_sha256']
            lineage = []; seen = set(); cursor = parent_id
            while cursor is not None:
                require(cursor not in seen); seen.add(cursor)
                intent, receipt = self._request(db, cursor)
                saved = self._continuation(db, cursor)
                require(receipt['state'] == 'failed' and saved is not None
                    and saved['job_token'] == parent_binding['job_token']
                    and all(intent.get(k) == v for k,v in identity.items()))
                lineage.append((intent, receipt, saved['native_capability_sha256']))
                cursor = intent.get('resume_of')
            for row in db.execute('SELECT i.id,i.document intent,r.document receipt FROM workbench_extraction_intents i '
                    'LEFT JOIN workbench_extraction_receipts r ON r.id=i.id WHERE i.paper_id=?', (paper['id'],)):
                other = json.loads(row['intent'])
                if (other.get('source_sha256') != binding.pdf_sha256
                        or row['id'] in {unknown_request_id, authorization_request_id}): continue
                receipt = json.loads(row['receipt']) if row['receipt'] else None
                require(receipt is not None and receipt.get('state') not in {'unknown','failed_or_unknown'})
        adapter_identity = {**{k:identity[k] for k in
            ('task_id','paper_id','title','doi','role','source_sha256')},
            'request_id':unknown_request_id, 'workbench_paper_id':binding.workbench_paper_id}
        adapter = WorkbenchScientificAdapter(adapter_identity,
            capability_reader=runtime.native_capability_reader, stage_reader=lambda:None)
        current_native = sha256(canonical(adapter.installed))
        require(current_native == expected_native == child_binding['native_capability_sha256'])
        with client.calls.transaction() as db:
            _, policy = client.calls._latest_policy(db)
        require(json.loads(policy).get('config') == asdict(client.calls.config)
            and client.calls.config.max_output_tokens >= 32000)
        old = runtime.native_recovery_reader(child_binding['job_token'])
        parent = runtime.native_recovery_reader(parent_binding['job_token'])
        checkpoint, execution, job = old['checkpoint'], old['execution'], old['job']
        original, previous, parent_job = parent['checkpoint'], parent['execution'], parent['job']
        native_lineage = checkpoint.continuation_lineage
        require(original.continuation_lineage is None and native_lineage is not None
            and original.state == checkpoint.state == 'outcome_unknown'
            and checkpoint.lease_owner_digest is original.lease_owner_digest is None
            and native_lineage.parent_task_id == original.manifest.task_id
            and native_lineage.parent_revision == original.revision
            and native_lineage.parent_checkpoint_digest == sha256(encode_checkpoint(original))
            and native_lineage.parent_job_state_digest == sha256(previous.job_state)
            and native_lineage.continuation_request_id == unknown_request_id
            and native_lineage.failed_receipt == original.receipts[-1]
            and original.stage == checkpoint.stage in {'initial_focus','coverage_gap'}
            and original.receipts[-1].state == checkpoint.receipts[-1].state == 'outcome_unknown'
            and checkpoint.receipts[:-1] == original.receipts[:-1]
            and len(checkpoint.receipts) == len(original.receipts)
            and execution.receipt_offset == previous.receipt_offset
            and execution.completed_results == previous.completed_results
            and job.stage_outputs == parent_job.stage_outputs
            and job.chunks == parent_job.chunks and job.snapshot == parent_job.snapshot
            and dict(job.paper) == dict(parent_job.paper) == {'title':paper['title'],'doi':paper['doi']}
            and job.paper_id == parent_job.paper_id == binding.workbench_paper_id
            and job.snapshot['pdf_sha256'] == binding.pdf_sha256
            and checkpoint.receipts[-1].max_tokens == 32000
            and original.receipts[-1].max_tokens == {'initial_focus':16000,'coverage_gap':12000}[original.stage]
            and len(execution.completed_results) == len(native_lineage.prefix_result_digests)
            and checkpoint.spent_calls == original.spent_calls+1
            and checkpoint.spent_tokens == original.spent_tokens+32000
            and checkpoint.manifest.max_calls == original.manifest.max_calls+1
            and checkpoint.manifest.max_tokens == original.manifest.max_tokens+32000)
        for field in ('session_digest','provider_id','runtime_revision','credential_generation',
                'task_models','executor_id','executor_version','pdf_snapshot_fingerprint'):
            require(getattr(checkpoint.manifest,field) == getattr(original.manifest,field))
        require(original.manifest.session_digest == sha256(runtime.session_id.encode())
            and original.manifest.provider_id == 'deepseek'
            and original.manifest.runtime_revision == connection_revision
            and original.manifest.credential_generation == credential_generation
            and original.manifest.task_models == (('analysis',client.model),('extraction',client.model))
            and original.manifest.executor_id == adapter.installed['executor']['id']
            and original.manifest.executor_version == adapter.installed['executor']['version'])
        rows = []
        for intent, receipt, native_digest_sha in reversed(lineage):
            ids = receipt.get('model_request_ids'); require(isinstance(ids,list) and len(set(ids)) == len(ids))
            base = {k:intent[k] for k in ('task_id','paper_id','role','source_sha256','request_id')}
            for ordinal, call_id in enumerate(ids,1):
                require(call_id == sha256(canonical({**base,'ordinal':ordinal}))[:32])
                record = client.calls.lookup(call_id) or {}; result = record.get('receipt') or {}
                proof = result.get('scientific_adapter') or {}; n = len(rows)
                require(n < len(original.receipts)); native = original.receipts[n]
                require(record.get('request_sha256') == result.get('request_sha256')
                    and result.get('provider') == 'deepseek' and result.get('requested_model') == client.model
                    and result.get('purpose') == 'literature_workbench_extraction'
                    and result.get('transport_attempts') == 1 and result.get('http_status') == 200
                    and proof.get('adapter_source_sha256') == adapter.adapter_sha256
                    and proof.get('native_contract_sha256') == native_digest_sha
                    and sha256(canonical(proof.get('native_capabilities'))) == native_digest_sha
                    and proof.get('identity') == {**base,'title':paper['title'],'doi':paper['doi'],
                        'workbench_paper_id':binding.workbench_paper_id}
                    and proof.get('native_call_digest') == native.call_digest
                    and proof.get('stage') == native.stage and result.get('task') == native.task
                    and result.get('max_output_tokens') == native.max_tokens)
                _digest(result.get('response_sha256')); _digest(result.get('request_sha256'))
                usage = result.get('usage') or {}
                require(all(type(usage.get(k)) is int and usage[k]>=0 for k in
                    ('prompt_tokens','completion_tokens','total_tokens'))
                    and usage['total_tokens'] == usage['prompt_tokens']+usage['completion_tokens'])
                if n < len(original.receipts)-1:
                    require(native.state == 'succeeded' and result.get('state') == 'completed'
                        and result.get('finish_reason') == 'stop'
                        and result.get('output_sha256') == sha256(canonical(result.get('structured_output')))
                        and native_digest(result['structured_output']) == native.result_digest)
                else:
                    require(result.get('state') == 'response_invalid' and result.get('finish_reason') == 'length'
                        and result.get('error') == 'incomplete_generation')
                rows.append((call_id, result))
        require(len(rows) == len(original.receipts))
        known_proof = json.loads(native_lineage.proof_bytes)
        require(known_proof.get('original_request_id') == parent_id
            and known_proof.get('model_request_id') == rows[-1][0]
            and known_proof.get('request_sha256') == rows[-1][1]['request_sha256']
            and known_proof.get('response_sha256') == rows[-1][1]['response_sha256']
            and known_proof.get('native_task_id') == original.manifest.task_id)
        ids = old_receipt.get('model_request_ids')
        require(isinstance(ids,list) and len(ids) == 1)
        base = {k:old_intent[k] for k in ('task_id','paper_id','role','source_sha256','request_id')}
        require(ids[0] == sha256(canonical({**base,'ordinal':1}))[:32])
        unknown_row = client.calls.lookup(ids[0]) or {}; unknown = unknown_row.get('receipt') or {}
        proof = unknown.get('scientific_adapter') or {}
        require(unknown.get('state') == 'unknown' and unknown.get('usage') is None
            and unknown.get('transport_attempts') == 1
            and unknown.get('requested_model') == client.model and unknown.get('provider') == 'deepseek'
            and unknown.get('purpose') == 'literature_workbench_extraction'
            and unknown.get('request_sha256') == unknown_row.get('request_sha256')
            and unknown.get('max_output_tokens') == 32000
            and proof.get('identity') == adapter_identity
            and proof.get('adapter_source_sha256') == adapter.adapter_sha256
            and proof.get('native_contract_sha256') == current_native
            and proof.get('native_call_digest') == checkpoint.receipts[-1].call_digest
            and proof.get('stage_fingerprint') == execution.stage_fingerprint)
        for idx,result in enumerate(execution.completed_results):
            require(native_digest(result) == native_lineage.prefix_result_digests[idx]
                == checkpoint.receipts[execution.receipt_offset+idx].result_digest)
        return dict(version=1, task_id=identifier, paper_id=paper['id'],
            source_sha256=binding.pdf_sha256, unknown_reissue_of=unknown_request_id,
            parent_request_id=parent_id, unknown_model_request_id=ids[0],
            unknown_model_record_sha256=sha256(canonical(unknown_row)),
            unknown_checkpoint_sha256=sha256(encode_checkpoint(checkpoint)),
            parent_checkpoint_sha256=sha256(encode_checkpoint(original)),
            successful_prefix_sha256=sha256(canonical(execution.completed_results)),
            reused_model_calls=len(original.receipts)-1, prefix_count=len(execution.completed_results),
            stage_fingerprint=execution.stage_fingerprint, inherited_unknown_model_calls=1,
            inherited_unknown_reserved_tokens=32000, inherited_failed_model_calls=1,
            inherited_failed_reserved_tokens=original.receipts[-1].max_tokens,
            native_capability_sha256=current_native, **model_binding,
            connection_revision=connection_revision, credential_generation=credential_generation,
            unknown_job_token=child_binding['job_token'], parent_job_token=parent_binding['job_token'])

    def unknown_reissue_options(self, identifier, binding, client, *, connection_revision, credential_generation):
        options = []
        for receipt in self.history(identifier):
            if receipt.get('state') != 'failed_or_unknown': continue
            old_id = receipt['request_id']
            option = dict(unknown_reissue_of=old_id, enabled=False, reused_model_calls=0,
                reason='未知请求尚未形成可核验的零进度断点；未重发。')
            with self.papers.tasks.transaction() as db:
                claimed = self._unknown_reissue_claim(db,old_id)
            if claimed:
                option.update(request_id=claimed['request_id'],
                    reason='这条未知断点的显式重发已使用；请查看原恢复请求，不能再次重发。')
            else:
                try:
                    context = self._unknown_reissue_context(identifier,old_id,binding,client,
                        connection_revision=connection_revision,credential_generation=credential_generation)
                    option.update(enabled=True, reused_model_calls=context['reused_model_calls'],
                        inherited_unknown_reserved_tokens=context['inherited_unknown_reserved_tokens'],
                        reason='供应商已核对后，可明确授权一次断点重发；保留未知费用与已保存答复，不重扫全文。')
                except (TaskError,ImportError,ValueError,TypeError,KeyError): pass
            options.append(option)
        return options

    def authorize_unknown_reissue(self, identifier, request_id, binding, client, *, unknown_reissue_of,
            supplier_checked, retain_unknown_charge, connection_revision, credential_generation):
        task_id(request_id);task_id(unknown_reissue_of)
        if supplier_checked is not True or retain_unknown_charge is not True or request_id == unknown_reissue_of:
            raise TaskError('必须明确确认供应商已核对并保留未知费用，且使用新的恢复编号。')
        context = self._unknown_reissue_context(identifier,unknown_reissue_of,binding,client,
            connection_revision=connection_revision,credential_generation=credential_generation,
            authorization_request_id=request_id)
        document = {**context,'request_id':request_id,'supplier_checked':True,'retain_unknown_charge':True,
            'automatic_retry':False,'authorization_kind':'explicit_user_unknown_reissue_once'}
        with self.papers.tasks.transaction() as db:
            prior = self._unknown_reissue_claim(db,unknown_reissue_of)
            if prior:
                if canonical(prior) != canonical(document): raise TaskError('显式重发授权身份已变化。')
                return prior
            db.execute('INSERT INTO workbench_extraction_unknown_reissues VALUES (?,?,?,?)',
                (request_id,unknown_reissue_of,canonical(document).decode(),sha256(canonical(document))))
        return document

    def recovery_options(self, identifier, binding, client, *, connection_revision,
                         credential_generation):
        """Human status view; private tokens and source data never leave it."""
        options = []
        for receipt in self.history(identifier):
            if receipt.get('state') not in {'failed', 'failed_or_unknown', 'unknown'}:
                continue
            option = {'resume_of': receipt['request_id'], 'enabled': False,
                'reused_model_calls': 0, 'reason': '原提取状态未知；请先核对，不能恢复收费。'}
            try:
                context = self._recovery_context(identifier, receipt['request_id'], binding, client,
                    connection_revision=connection_revision, credential_generation=credential_generation)
                option.update(enabled=True, reused_model_calls=context['reused_model_calls'],
                    reason='复用已成功调用；只对后续阶段新增记账，原失败记录保留。')
                if context.get('kind') == 'known_length_continuation':
                    option.update(replacement_max_tokens=32_000, extra_calls=1, extra_tokens=32_000,
                        inherited_failed_model_calls=1,
                        reason='复用已成功答复；已返回的截断调用保留，新增一次有界局部恢复及费用。')
            except TaskError as exc:
                option['reason'] = str(exc)
            options.append(option)
        return options

    def _continuation_document(self, request_id, intent, binding, client, token, capability_digest):
        return {'version': 1, 'request_id': request_id, 'job_token': token,
            'intent_sha256': sha256(canonical(intent)), **self._model_binding(client),
            'workbench_paper_id': binding.workbench_paper_id, 'source_sha256': binding.pdf_sha256,
            'session_sha256': sha256(self.runtime.session_id.encode()),
            'native_capability_sha256': capability_digest}

    def _save_continuation(self, request_id, intent, binding, client, token, adapter):
        document = self._continuation_document(request_id, intent, binding, client, token,
            sha256(canonical(adapter.installed)))
        with self.papers.tasks.transaction() as db:
            prior = self._continuation(db, request_id)
            if prior is not None:
                if canonical(prior) != canonical(document):
                    raise TaskError('已登记的恢复绑定与工作台当前准备不一致，未发送。')
                return
            db.execute('INSERT INTO workbench_extraction_continuations VALUES (?,?,?)',
                (request_id, canonical(document).decode(), sha256(canonical(document))))

    def extraction_identity(self, identifier, request_id, binding, client, *, force_rescan=False,
                repair_visuals=False, resume_of=None, unknown_reissue_of=None, connection_revision, credential_generation):
        """Read-only identity/options check shared by queued and synchronous actions."""
        task_id(request_id)
        if (type(force_rescan) is not bool or type(repair_visuals) is not bool
                or (force_rescan and repair_visuals)
                or (resume_of is not None and (force_rescan or repair_visuals))
                or type(connection_revision) is not int or connection_revision < 0
                or type(credential_generation) is not int or credential_generation < 0):
            raise TaskError('文献提取选项或模型连接身份无效。')
        if unknown_reissue_of is not None:
            task_id(unknown_reissue_of)
            if unknown_reissue_of == request_id or resume_of is not None or force_rescan or repair_visuals:
                raise TaskError('显式未知重发不能同时重扫、修图或恢复其他任务。')
        if resume_of is not None:
            task_id(resume_of)
            if resume_of == request_id:
                raise TaskError('恢复须使用新的请求编号，并保留原提取记录。')
        self._model_binding(client)
        paper = self._paper(identifier, binding)
        intent = dict(request_id=request_id, task_id=identifier, paper_id=paper['id'],
            title=paper['title'], doi=paper['doi'], role=HUMAN_ROLE,
            source_sha256=binding.pdf_sha256, model=client.model,
            connection_revision=connection_revision, credential_generation=credential_generation,
            force_rescan=force_rescan, repair_visuals=repair_visuals,
            scientific_status='not_evaluated', execution_authorized=False,
            automatic_retry=False)
        if resume_of is not None:
            intent['resume_of'] = resume_of
        if unknown_reissue_of is not None:
            intent['unknown_reissue_of'] = unknown_reissue_of
        return intent

    def extract(self, identifier, request_id, binding, client, *, force_rescan=False,
                repair_visuals=False, resume_of=None, unknown_reissue_of=None, connection_revision, credential_generation,
                before_model_request=None):
        """Controller-authorized user click; no B fields are accepted or read."""
        intent=self.extraction_identity(identifier,request_id,binding,client,
            force_rescan=force_rescan,repair_visuals=repair_visuals,resume_of=resume_of,
            unknown_reissue_of=unknown_reissue_of, connection_revision=connection_revision,credential_generation=credential_generation)
        paper={key:intent['paper_id'] if key=='id' else intent[key] for key in ('id','title','doi')}
        binding_hash = sha256(canonical(intent))
        with self.papers.tasks.transaction() as db:
            prior = db.execute('SELECT i.binding_sha256, i.document intent, r.document receipt '
                'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
                'ON r.id=i.id WHERE i.id=?',
                (request_id,)).fetchone()
            if prior:
                if prior['binding_sha256'] != binding_hash:
                    raise TaskError('提取请求编号已用于其他来源或模型配置。')
                return json.loads(prior['receipt']) if prior['receipt'] else {
                    **json.loads(prior['intent']), 'state': 'unknown', 'automatic_retry': False}
        explicit_reissue = None
        if unknown_reissue_of is not None:
            with self.papers.tasks.transaction() as db:
                explicit_reissue = self._unknown_reissue_claim(db, unknown_reissue_of)
            if explicit_reissue is None or explicit_reissue['request_id'] != request_id:
                raise TaskError('缺少同一未知断点的显式一次重发授权。')
            current = self._unknown_reissue_context(identifier,unknown_reissue_of,binding,client,
                connection_revision=connection_revision,credential_generation=credential_generation,
                authorization_request_id=request_id)
            if any(explicit_reissue.get(k) != v for k,v in current.items()):
                raise TaskError('显式重发绑定的来源、旧断点或模型已变化。')
        recovery = self._recovery_context(identifier, resume_of, binding, client,
            connection_revision=connection_revision, credential_generation=credential_generation
            ) if resume_of is not None else None
        with self.papers.tasks.transaction() as db:
            # Recheck under the write lock after validating the sealed state.
            prior = db.execute('SELECT i.binding_sha256, i.document intent, r.document receipt '
                'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
                'ON r.id=i.id WHERE i.id=?', (request_id,)).fetchone()
            if prior:
                if prior['binding_sha256'] != binding_hash:
                    raise TaskError('提取请求编号已用于其他来源或模型配置。')
                return json.loads(prior['receipt']) if prior['receipt'] else {
                    **json.loads(prior['intent']), 'state': 'unknown', 'automatic_retry': False}
            if recovery and db.execute('SELECT 1 FROM workbench_extraction_recoveries '
                    'WHERE source_request_id=?', (resume_of,)).fetchone():
                raise TaskError('原提取已发起恢复；不能重复收费。')
            same_source = db.execute('SELECT i.document intent, r.document receipt '
                'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
                'ON r.id=i.id WHERE i.paper_id=?', (paper['id'],)).fetchall()
            resolved = {r['unknown_request_id'] for r in self._completed_unknown_reissues(db, identifier,
                repair_identity={**intent, **self._model_binding(client)})} if repair_visuals else set()
            for row in same_source:
                old_intent = json.loads(row['intent'])
                if old_intent['source_sha256'] != binding.pdf_sha256:
                    continue
                old = json.loads(row['receipt']) if row['receipt'] else None
                if old is None or old['state'] in {'unknown', 'failed_or_unknown'}:
                    if (old_intent.get('request_id') not in resolved
                            and (not explicit_reissue or old_intent['request_id'] != unknown_reissue_of)):
                        raise TaskError('同一 PDF 的先前提取状态未知；请先核对，不能换请求编号再次收费。')
                if (not recovery and not explicit_reissue and not force_rescan and not repair_visuals and (old['state'] in
                        {'completed', 'completed_with_limitations', 'no_evidence_published'}
                        or old.get('model_request_ids'))):
                    raise TaskError('这份 PDF 已有提取记录；请查看现有证据，重扫须明确发起。')
            if db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('delete','finish')",
                    (identifier,)).fetchone():
                raise TaskError('任务已结束，不能开始新的文献提取。')
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (request_id, identifier, paper['id'], binding_hash, canonical(intent).decode()))
            if recovery:
                db.execute('INSERT INTO workbench_extraction_recoveries VALUES (?,?,?)',
                    (request_id, resume_of, canonical({k: v for k, v in recovery.items()
                        if k != 'job_token'}).decode()))
                # Persist the already-authenticated token in the same commit
                # as consent. A free preflight/assembly failure must still be
                # recoverable through this new failed action, never lock the
                # source behind a consumed parent without a continuation.
                continuation = self._continuation_document(request_id, intent, binding,
                    client, (self._known_child_token(recovery['job_token'], request_id)
                        if recovery.get('kind') == 'known_length_continuation' else recovery['job_token']),
                    recovery['native_capability_sha256'])
                db.execute('INSERT INTO workbench_extraction_continuations VALUES (?,?,?)',
                    (request_id, canonical(continuation).decode(), sha256(canonical(continuation))))
        if explicit_reissue:
            recovery = {**explicit_reissue, 'kind':'explicit_unknown_reissue',
                'inherited_failed_tokens':explicit_reissue['inherited_failed_reserved_tokens']}
        adapter = WorkbenchModelClient(client, task=identifier, paper=paper['id'],
            source_sha256=binding.pdf_sha256, request_id=request_id,
            before_model_request=before_model_request)
        result = {**intent, 'state': 'failed', 'model_request_ids': []}
        if recovery:
            result.update(reused_model_calls=recovery['reused_model_calls'],
                further_model_calls_accounted=True)
            if recovery.get('inherited_failed_model_calls'):
                result.update(inherited_failed_model_calls=1,
                    inherited_failed_reserved_tokens=recovery['inherited_failed_tokens'],
                    local_recovery_used=True)
        if explicit_reissue:
            result.update(inherited_unknown_model_calls=1,
                inherited_unknown_reserved_tokens=explicit_reissue['inherited_unknown_reserved_tokens'],
                supplier_checked=True,retain_unknown_charge=True,
                authorization_kind='explicit_user_unknown_reissue_once')
        try:
            if explicit_reissue:
                child = self.runtime.native_unknown_reissue_creator(
                    explicit_reissue['unknown_job_token'],explicit_reissue['parent_job_token'],
                    request_id=request_id,authorization=explicit_reissue,now=int(time.time()))
                recovery.update(job_token=child['job_token'])
            if recovery and recovery.get('kind') == 'known_length_continuation':
                from .workbench_known_failure import create_known_failure_child
                recovery = create_known_failure_child(self, identifier, resume_of, binding, client,
                    continuation_request_id=request_id, connection_revision=connection_revision,
                    credential_generation=credential_generation)
            request = ({'job_token': recovery['job_token']} if recovery else
                dict(paper_id=binding.workbench_paper_id, force_rescan=force_rescan))
            if repair_visuals:
                request['repair_visuals'] = True
            ports, jobs = self.runtime.ports, self.runtime.jobs
            ports.assembler.preflight(request)
            draft = ports.assembler.assemble(request)
            token = draft.outbound['job_handle']
            if recovery and token != recovery['job_token']:
                raise TaskError('工作台返回了其他原生任务，未恢复或收费。')
            decoded = self.runtime.decode_job_state(jobs.export_private_state(
                token, session_id=self.runtime.session_id))
            if decoded.snapshot.get('pdf_sha256') != binding.pdf_sha256:
                raise TaskError('工作台实际捕获的 PDF 已变化，未发送模型请求。')
            adapter.adapter = WorkbenchScientificAdapter({**adapter.identity,
                'title': paper['title'], 'doi': paper['doi'],
                'workbench_paper_id': binding.workbench_paper_id},
                capability_reader=self.runtime.native_capability_reader,
                stage_reader=(lambda: self.runtime.native_stage_reader(token))
                    if callable(self.runtime.native_stage_reader) else None)
            self._save_continuation(request_id, intent, binding, client, token, adapter.adapter)
            if recovery:
                if (decoded.stage['stage_fingerprint'] != recovery['stage_fingerprint']
                        or draft.outbound['stage_fingerprint'] != recovery['stage_fingerprint']):
                    raise TaskError('工作台内存阶段与密封检查点不一致；须重新启动持久服务后恢复。')
                # The upstream budget client consumes sealed results itself;
                # advance only our mandatory adapter's call-position tracker.
                adapter.adapter.positions[recovery['stage_fingerprint']] = recovery['prefix_count']
            fresh = getattr(jobs, 'assert_source_fresh', None)
            if callable(fresh):
                fresh(token, session_id=self.runtime.session_id)
            if recovery and recovery.get('kind') in {'known_length_continuation','known_length_existing_child',
                    'explicit_unknown_reissue'}:
                model_binding = self._model_binding(client)
                expected_native = recovery['native_capability_sha256']
                def recovery_model_guard():
                    if before_model_request is not None:
                        before_model_request()
                    self._paper(identifier, binding)
                    if (self.runtime.native_source_reader(binding.workbench_paper_id) != binding.pdf_sha256
                            or self._model_binding(client) != model_binding
                            or sha256(canonical(self.runtime.native_capability_reader())) != expected_native):
                        raise TaskError('恢复来源、模型策略或原生版本已变化。')
                    with client.calls.transaction() as db:
                        _sequence, policy = client.calls._latest_policy(db)
                        if json.loads(policy).get('config') != asdict(client.calls.config):
                            raise TaskError('恢复模型账本策略已变化。')
                # Recheck after local preflight and before every paid call.
                # This guard neither admits a task nor retries a response.
                recovery_model_guard()
                adapter.before_model_request = recovery_model_guard
            models = (('analysis', client.model), ('extraction', client.model))
            outbound = {'payload': dict(draft.outbound),
                'call_plan': [call.canonical_dict() for call in draft.call_plan]}
            payload = json.dumps(outbound, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()
            now = int(time.time())
            action = self.runtime.action_factory(action_id='workbench_' + request_id,
                session_digest=sha256(self.runtime.session_id.encode()),
                scope='literature_extraction', provider_id='deepseek',
                runtime_revision=connection_revision, credential_generation=credential_generation,
                runtime_activation='product_user_action', runtime_task_models=models,
                task='extraction', task_models=models, models=(client.model,),
                executor_id=adapter.adapter.installed['executor']['id'],
                executor_version=adapter.adapter.installed['executor']['version'],
                estimated_calls=draft.estimated_calls, max_calls=draft.max_calls,
                max_tokens=draft.max_tokens, outbound=outbound,
                outbound_digest=sha256(payload), manifest_digest=binding_hash,
                units=draft.content_units, byte_count=len(payload),
                issued_at=now, expires_at=now + 300)
            budget = self.runtime.budget_client_factory(client=adapter, action=action)
            raw = ports.executor.execute(action=action, ai_client=budget)
            summary = dict(ports.projector.project(raw))
            if (summary.get('schema_version') != 'literature-extraction-commit-result-v2'
                    or summary.get('status') not in {'completed', 'saved_index_pending'}
                    or summary.get('paper') != {'title': paper['title'], 'doi': paper['doi']}):
                raise TaskError('工作台尚未完成这篇论文的验证与保存。')
            count_keys = ('candidate_count', 'published_item_count', 'existing_item_count',
                'manual_review_count', 'table_candidate_count', 'figure_candidate_count',
                'table_structure_candidate_count', 'table_structure_manual_review_count',
                'table_structure_unavailable_count')
            counts = {key: summary[key] for key in count_keys}
            if any(type(n) is not int or n < 0 for n in counts.values()):
                raise TaskError('工作台提取数量回执无效。')
            state = 'completed'
            if not any(counts[k] for k in ('published_item_count', 'existing_item_count',
                    'table_candidate_count', 'figure_candidate_count')):
                state = 'no_evidence_published'
            elif counts['manual_review_count'] or counts['table_structure_manual_review_count']:
                state = 'completed_with_limitations'
            result.update(state=state, workbench_status=summary['status'], counts=counts,
                visual_evidence_ready=summary['visual_evidence_ready'],
                visual_stage_status=summary['visual_stage_status'],
                source_receipt_sha256=sha256(canonical(summary)),
                limitations=['提取与保存回执不等于复现科学通过；未核实或缺失内容不能补值。'])
        except Exception as exc:
            code = getattr(exc, 'cause_code', '') or getattr(exc, 'code', '')
            result.update(error_code=code if isinstance(code, str) and re.fullmatch('[a-z][a-z0-9_]{2,95}', code)
                else 'workbench_extraction_not_completed',
                error=_MODEL_ERRORS.get(code, '文献工作台未完成提取；原请求记录保留，未自动重试。'))
            if isinstance(exc, WorkbenchAdapterError):
                result['error'] = str(exc)
            if code in {'literature_call_outcome_unknown', 'workbench_model_request_not_completed'}:
                saved = client.calls.lookup(adapter.requests[-1]) if adapter.requests else None
                receipt = (saved or {}).get('receipt') or {}
                proof = receipt.get('scientific_adapter') or {}
                if (receipt.get('state') == 'response_invalid'
                        and proof.get('identity') == adapter.adapter.identity):
                    result['error_code'] = 'workbench_model_response_invalid'
                    result['error'] = ('模型已返回，但输出不完整或 JSON 无效；原请求与费用保留，未自动重试。')
                    if receipt.get('finish_reason') == 'length':
                        result['error'] = ('模型答复达到单次输出上限，未得到完整 JSON；原请求与费用保留，未自动重试。')
                    result['validation_details'] = [{key: receipt[key] for key in (
                        'state', 'finish_reason', 'max_output_tokens', 'error') if key in receipt}]
            if code == 'literature_stage_invalid':
                # Explain a native shape/budget failure from our own saved
                # receipts. Do not rewrite the response, skip validation,
                # publish a partial P, or issue another model request.
                limit = adapter.adapter.installed['output_limits']['extraction_records_per_response']
                details = []
                for call_id in adapter.requests:
                    saved = client.calls.lookup(call_id) or {}
                    receipt = saved.get('receipt') or {}
                    proof = receipt.get('scientific_adapter') or {}
                    payload = receipt.get('structured_output')
                    if (receipt.get('state') != 'completed'
                            or proof.get('stage') not in {'initial_focus', 'coverage_gap'}
                            or not isinstance(payload, dict)
                            or receipt.get('output_sha256') != sha256(canonical(payload))
                            or proof.get('identity') != adapter.adapter.identity):
                        continue
                    data, findings = payload.get('data'), payload.get('findings')
                    if (isinstance(data, list) and isinstance(findings, list)
                            and len(data) + len(findings) > limit):
                        details.append({'request_id': call_id, 'stage': proof['stage'],
                            'data_count': len(data), 'finding_count': len(findings),
                            'limit': limit})
                if details:
                    result['error_code'] = 'workbench_response_record_limit'
                    result['validation_details'] = details
                    totals = '、'.join(str(d['data_count'] + d['finding_count']) for d in details)
                    result['error'] = (f'AI 的单次返回有 {totals} 条数据与结论，超过文献工作台每次 {limit} 条上限。'
                        '原输出与费用已保留；尚未发布 P，未自动重试。')
        result['model_request_ids'] = list(adapter.requests)
        if recovery:
            result['new_model_calls'] = len(adapter.requests)
        if result['state'] == 'failed':
            receipts = [client.calls.lookup(r) for r in adapter.requests]
            if any(not r or not r.get('receipt') or r['receipt'].get('state') == 'unknown'
                    for r in receipts):
                result['state'] = 'failed_or_unknown'
        result['at'] = datetime.now(timezone.utc).isoformat()
        with self.papers.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_receipts VALUES (?,?)',
                (request_id, canonical(result).decode()))
        return result

    def export(self, identifier, binding, *, entity_type, entity_uid):
        """Reuse original single-evidence CSV export after identity membership."""
        self._paper(identifier, binding)
        if not isinstance(entity_uid, str) or re.fullmatch('[1-9][0-9]{0,18}', entity_uid) is None:
            raise TaskError('工作台证据标识无效。')
        if entity_type in {'item', 'finding'}:
            rows = self.runtime.data_items(self.runtime.database, binding.workbench_paper_id)
            present = any(str(row['item_id']) == entity_uid for row in rows)
        elif entity_type in {'table', 'figure'}:
            rows = self.runtime.visual_assets(self.runtime.database,
                paper_id=binding.workbench_paper_id, asset_type=entity_type)
            present = any(str(row['id']) == entity_uid for row in rows)
        else:
            raise TaskError('工作台证据类型无效。')
        if not present:
            raise TaskError('这项证据不属于当前登记论文。')
        return self.runtime.exports.export(source_scope='workspace', source_id='workspace',
            entity_type=entity_type, entity_uid=entity_uid, format='csv')

    def evidence(self, identifier, binding):
        """Project existing source values, units and original captions for humans."""
        paper = self._paper(identifier, binding)
        rows = self.runtime.data_items(self.runtime.database, binding.workbench_paper_id)
        assets = self.runtime.visual_assets(self.runtime.database, paper_id=binding.workbench_paper_id)
        if len(rows) > 10000 or len(assets) > 512:
            raise TaskError('工作台证据数量超过当前完整展示容量；未返回截断结果。')
        columns = ('value_text', 'meaning', 'unit', 'context_explanation',
            'source_page', 'source_locator', 'source_excerpt', 'quality_gate_status')
        items = [{'entity_uid': str(row['item_id']),
            **{key: row.get(key) for key in columns}} for row in rows]
        figures = [{'entity_uid': str(row['id']), **{key: row.get(key) for key in
            ('asset_type', 'label', 'display_name', 'caption', 'page_start', 'page_end',
             'image_sha256', 'quality_gate_status')}} for row in assets]
        result = dict(task_id=identifier, paper_id=paper['id'], title=paper['title'],
            doi=paper['doi'], role=HUMAN_ROLE, source_sha256=binding.pdf_sha256,
            items=items, visuals=figures, scientific_status='not_evaluated',
            execution_authorized=False)
        if len(canonical(result)) > 2 * 1024 * 1024:
            raise TaskError('工作台完整证据超过当前展示容量，未省略来源内容。')
        return result

    def image(self, identifier, binding, *, entity_uid):
        """Read a registered original crop with its stored content digest."""
        self._paper(identifier, binding)
        if not isinstance(entity_uid, str) or re.fullmatch('[1-9][0-9]{0,18}', entity_uid) is None:
            raise TaskError('工作台图像标识无效。')
        rows = self.runtime.visual_assets(self.runtime.database, paper_id=binding.workbench_paper_id)
        row = next((a for a in rows if str(a['id']) == entity_uid), None)
        if row is None or not callable(self.runtime.visual_image):
            raise TaskError('这张原图未登记于当前论文。')
        _digest(row.get('image_sha256'))
        try:
            path = self.runtime.visual_image(self.runtime.database, int(entity_uid))
            content = read_regular(path, 5 * 1024 * 1024)
        except Exception:
            raise TaskError('工作台原图暂时无法安全读取。') from None
        if not content.startswith(b'\x89PNG\r\n\x1a\n') or sha256(content) != row['image_sha256']:
            raise TaskError('工作台原图摘要已变化，不能作为既有证据展示。')
        return content
