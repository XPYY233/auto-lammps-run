"""Local protocol bridge to the existing literature workbench extraction runner.

The separately installed workbench owns PDF capture, extraction, scientific
verification, figure/table processing and atomic publication. No implementation
of those operations is copied here. The controller supplies trusted paper
bindings and persistent workbench services; the browser supplies neither PDFs,
credentials nor independent-calculation conditions.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import importlib
import json
import math
from pathlib import Path
import re
import time

from .deepseek import (DeepSeekClient, MAX_RESPONSE_BYTES, ModelError, parse_completion,
                       request_body, usage_from_response)
from .manifest import canonical, sha256
from .papers import doi_text
from .runtime_launcher import read_regular
from .tasks import TaskError, task_id
from .workbench_adapter import WorkbenchAdapterError, WorkbenchScientificAdapter


HUMAN_ROLE = 'paper_reference_human_only'
_HASH = re.compile(r'[a-f0-9]{64}')
_MODEL_ERRORS = {
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


def installed_workbench_runtime(*, database, snapshot_blobs, checkpoint_runtime,
                                session_id):
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
        workflow = load('evidence.literature_extraction_checkpoint_workflow')
        stages = load('evidence.literature_extraction_stages')
        snapshots = job.LiteraturePDFSnapshotAuthority(blob_store=snapshot_blobs)
        if snapshots.persistent is not True:
            raise ValueError('persistent snapshots required')
        jobs = job.LiteratureExtractionJobStore(snapshots=snapshots)
        ports = business.literature_extraction_business_ports(jobs,
            session_id=session_id, db=database,
            finalizer=finalizer.AtomicEvidenceDBFinalizer(database),
            checkpoint_runtime=checkpoint_runtime)
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
            return {'planner': {'id': workflow._PLANNER_ID, 'version': workflow._PLANNER_VERSION},
                'executor': {'id': workflow._EXECUTOR_ID, 'version': workflow._EXECUTOR_VERSION},
                'runtime_tasks': sorted(workflow.LITERATURE_POLICY_TASKS),
                'model_stages': sorted(job.MODEL_STAGES),
                'operations': {key: fn.__module__ + '.' + fn.__qualname__ for key, fn in operations.items()},
                'modules': {module.__name__: sha256(Path(module.__file__).read_bytes())
                    for module in (job, business, workflow, stages, persistence, finalizer)}}
        def native_stage(token):
            state = persistence.decode_job_private_state(jobs.export_private_state(token, session_id=session_id))
            return {'paper_id': state.paper_id, 'paper': dict(state.paper),
                'pdf_sha256': state.snapshot['pdf_sha256'], 'stage': state.stage,
                'runtime_task': workflow._runtime_task}
        return WorkbenchRuntime(database, jobs, ports, session_id,
            prepared.PreparedOutbound, budget.LiteratureDerivedBudgetBusinessAIClient,
            persistence.decode_job_private_state, exports,
            six.list_current_data, visuals.list_visual_assets,
            visuals.visual_asset_image_path, native_capabilities, native_stage)
    except (ImportError, AttributeError, TypeError, ValueError):
        raise TaskError('本机文献工作台依赖或持久服务尚未接好，未开始提取。') from None


class WorkbenchModelClient:
    """Adapt a product-configured DeepSeek client to the workbench JSON port.

    Each stage uses the existing project ModelCalls ledger. Options may tighten
    the saved per-call policy, never raise it. There is no credential fallback,
    provider selection, tool execution, network redirect or automatic retry.
    """
    def __init__(self, client, *, task, paper, source_sha256, request_id, adapter=None):
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
        identifier = sha256(canonical({**self.identity, 'ordinal': len(self.requests) + 1}))[:32]
        self.client.calls.reserve(identifier, body)
        self.requests.append(identifier)
        receipt = dict(provider='deepseek', requested_model=self.client.model,
            purpose='literature_workbench_extraction', role=HUMAN_ROLE,
            task=task, request_sha256=sha256(body), state='not_sent',
            usage=None, transport_attempts=0, scientific_adapter=proof)
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
            receipt.update(http_status=status, response_sha256=sha256(content))
            if status != 200:
                receipt['state'] = 'rejected'
                raise ModelError('provider_request_failed')
            receipt.update(state='response_invalid', usage=usage_from_response(content))
            result = parse_completion(content)
            receipt.update(state='completed', output_sha256=sha256(canonical(result)),
                structured_output=result)
        except Exception:
            self.client.calls.record(identifier, receipt)
            raise WorkbenchBridgeError('workbench_model_request_not_completed') from None
        self.client.calls.record(identifier, receipt)
        return result


class WorkbenchExtractionBridge:
    def __init__(self, papers, runtime):
        self.papers, self.runtime = papers, runtime
        with papers.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_intents ('
                'id TEXT PRIMARY KEY, task_id TEXT NOT NULL, paper_id TEXT NOT NULL, '
                'binding_sha256 TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workbench_extraction_receipts ('
                'id TEXT PRIMARY KEY REFERENCES workbench_extraction_intents(id), document TEXT NOT NULL)')
            for table in ('workbench_extraction_intents', 'workbench_extraction_receipts'):
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

    def extract(self, identifier, request_id, binding, client, *, force_rescan=False,
                repair_visuals=False, connection_revision, credential_generation):
        """Controller-authorized user click; no B fields are accepted or read."""
        task_id(request_id)
        if (type(force_rescan) is not bool or type(repair_visuals) is not bool
                or (force_rescan and repair_visuals)
                or type(connection_revision) is not int or connection_revision < 0
                or type(credential_generation) is not int or credential_generation < 0):
            raise TaskError('文献提取选项或模型连接身份无效。')
        paper = self._paper(identifier, binding)
        intent = dict(request_id=request_id, task_id=identifier, paper_id=paper['id'],
            title=paper['title'], doi=paper['doi'], role=HUMAN_ROLE,
            source_sha256=binding.pdf_sha256, model=client.model,
            connection_revision=connection_revision, credential_generation=credential_generation,
            force_rescan=force_rescan, repair_visuals=repair_visuals,
            scientific_status='not_evaluated', execution_authorized=False,
            automatic_retry=False)
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
            same_source = db.execute('SELECT i.document intent, r.document receipt '
                'FROM workbench_extraction_intents i LEFT JOIN workbench_extraction_receipts r '
                'ON r.id=i.id WHERE i.paper_id=?', (paper['id'],)).fetchall()
            for row in same_source:
                old_intent = json.loads(row['intent'])
                if old_intent['source_sha256'] != binding.pdf_sha256:
                    continue
                old = json.loads(row['receipt']) if row['receipt'] else None
                if old is None or old['state'] in {'unknown', 'failed_or_unknown'}:
                    raise TaskError('同一 PDF 的先前提取状态未知；请先核对，不能换请求编号再次收费。')
                if (not force_rescan and not repair_visuals and (old['state'] in
                        {'completed', 'completed_with_limitations', 'no_evidence_published'}
                        or old.get('model_request_ids'))):
                    raise TaskError('这份 PDF 已有提取记录；请查看现有证据，重扫须明确发起。')
            if db.execute("SELECT 1 FROM task_lifecycle WHERE task_id=? AND action IN ('delete','finish')",
                    (identifier,)).fetchone():
                raise TaskError('任务已结束，不能开始新的文献提取。')
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (request_id, identifier, paper['id'], binding_hash, canonical(intent).decode()))
        adapter = WorkbenchModelClient(client, task=identifier, paper=paper['id'],
            source_sha256=binding.pdf_sha256, request_id=request_id)
        result = {**intent, 'state': 'failed', 'model_request_ids': []}
        try:
            request = dict(paper_id=binding.workbench_paper_id, force_rescan=force_rescan)
            if repair_visuals:
                request['repair_visuals'] = True
            ports, jobs = self.runtime.ports, self.runtime.jobs
            ports.assembler.preflight(request)
            draft = ports.assembler.assemble(request)
            token = draft.outbound['job_handle']
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
        result['model_request_ids'] = list(adapter.requests)
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
