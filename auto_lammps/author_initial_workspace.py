"""Reading-only bridge from verified bootstrap metadata to selected originals.

This controller neither selects scientific files nor acquires them. The existing
selection ledger and selected-resource controller retain those responsibilities.
Its acknowledgement grants this project's bounded reading capability only; the
upstream license, redistribution and execution decisions remain unverified.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import PurePosixPath
import re

from .author_source_worker import MAX_FILE_BYTES, MAX_LINES
from .hpc_transport import SavedHPCTransport
from .manifest import canonical, sha256
from .remote_resources import tool_bundle
from .runtime_launcher import ExecutionDenied
from .selected_author_source_reader import SelectedAuthorSourceReader
from .tasks import TaskError, task_id, text


FLAGS = dict(scope='reference_only', B_input=False, execution_authorized=False,
             physical_evaluation_performed=False, scientific_validation=False)
READING_PURPOSE = 'read_author_originals_for_this_research_project'
_TEXT_EXTENSIONS = {'.py', '.sh', '.in', '.lmp', '.lammps', '.md', '.txt',
                    '.json', '.yaml', '.yml', '.toml', '.cfg'}
_TEXT_NAMES = {'readme', 'license', 'copying', 'makefile'}
_MISSING = ['许可证使用范围尚未核验；本次仅确认本项目研究用途的原件阅读。',
            '作者执行环境、可执行程序和执行许可尚未核验。',
            '所选原件完整不代表整个仓库、势函数配套或科学适用性已核验。']


class InitialAuthorWorkspace:
    def __init__(self, workspace, bootstrap):
        self.workspace, self.bootstrap, self.tasks = workspace, bootstrap, workspace.tasks
        if (bootstrap.tasks.path != self.tasks.path or bootstrap.papers is not workspace.papers
                or bootstrap.scopes is not workspace.scopes):
            raise TaskError('初始阅读绑定必须使用本项目同一论文、P范围和任务存储。')
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_initial_read_approvals ('
                'id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            for action in ('UPDATE', 'DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_author_initial_read_approvals_{action} '
                    'BEFORE '+action+' ON author_initial_read_approvals BEGIN SELECT RAISE(ABORT,'
                    '"immutable initial author reading approval"); END')

    def _task(self, identifier, *, active=True):
        task = self.tasks.get(task_id(identifier))
        if task['mode'] != 'reproduction':
            raise TaskError('作者原件阅读仅属于参考任务，不能进入研究或B输入。')
        if identifier in self.workspace.bindings:
            raise TaskError('本任务已有固定作者绑定，请沿原来源和历史操作。')
        if active and (self.tasks.paused(identifier) or any(self.tasks.lifecycle(identifier)[key]
                for key in ('user_finished', 'deleted'))):
            raise TaskError('任务已暂停或结束；阅读历史保留。')

    def _metadata(self, identifier, request=None):
        self._task(identifier)
        value = self.bootstrap.metadata_for_selection(identifier, request)
        if (value['task_id'] != identifier or value['scope'] != 'reference_only'
                or value['B_input'] is not False or value['execution_authorized'] is not False
                or value['bootstrap_receipt_sha256'] != sha256(canonical(value['bootstrap_receipt']))
                or value['existing_files'] != []):
            raise TaskError('初始作者清单回执身份或摘要不一致。')
        return value

    @staticmethod
    def _approval_binding(metadata):
        # Hash the complete trusted metadata, including the original license
        # bytes, scope and catalog evidence; retain no source text here.
        return dict(task_id=metadata['task_id'], paper_id=metadata['paper_id'],
            bootstrap_request_id=metadata['bootstrap_request_id'],
            bootstrap_receipt_sha256=metadata['bootstrap_receipt_sha256'],
            metadata_sha256=sha256(canonical(metadata)),
            reference_scope_sha256=sha256(canonical(metadata['reference_scope'])),
            catalog_association_sha256=sha256(canonical(metadata['catalog_association'])),
            license_evidence_sha256=sha256(canonical(metadata['license_evidence'])),
            **deepcopy(metadata['binding']))

    def _approvals(self, identifier):
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT * FROM author_initial_read_approvals WHERE task_id=? '
                              'ORDER BY rowid', (identifier,)).fetchall()
        result = []
        for row in rows:
            value = json.loads(row['document'])
            if (sha256(canonical(value)) != row['sha256'] or value.get('task_id') != identifier
                    or row['id'] != sha256(canonical(value.get('binding')))
                    or value.get('purpose') != READING_PURPOSE
                    or value.get('reading_approved') is not True
                    or any(value.get(key) is not False for key in
                        ('use_approved', 'redistribution_approved', 'execution_authorized',
                         'contract_signed', 'B_input', 'physical_evaluation_performed'))):
                raise TaskError('初始原件阅读确认记录摘要或用途不一致。')
            result.append(dict(document=value, sha256=row['sha256']))
        return result

    def _approved(self, identifier, metadata):
        binding = self._approval_binding(metadata)
        match = next((row for row in self._approvals(identifier)
                      if row['document']['binding'] == binding), None)
        if match is None:
            raise TaskError('请先明确确认本项目研究用途的原件阅读；未批准使用、再分发或执行。')
        return match

    def approve_reading(self, identifier, bootstrap_request_id, receipt_sha256, note=''):
        self._task(identifier)
        task_id(bootstrap_request_id)
        if not isinstance(receipt_sha256, str) or not re.fullmatch('[a-f0-9]{64}', receipt_sha256):
            raise TaskError('请提交页面显示的初始清单回执摘要。')
        note = text(note, 2000, required=False)
        with self.workspace.resources._lease(identifier):
            metadata = self._metadata(identifier, bootstrap_request_id)
            if metadata['bootstrap_receipt_sha256'] != receipt_sha256:
                raise TaskError('初始清单或许可证证据已变化，请核对原回执。')
            if not metadata['license_evidence']['files']:
                raise TaskError('尚未取得固定版本的许可证原文；阅读确认缺项保留，未取作者源码。')
            binding = self._approval_binding(metadata)
            identifier_sha = sha256(canonical(binding))
            existing = next((row for row in self._approvals(identifier)
                             if row['document']['binding'] == binding), None)
            if existing is None:
                at = datetime.now(timezone.utc).isoformat()
                document = dict(version=1, **FLAGS, task_id=identifier, purpose=READING_PURPOSE,
                    reading_approved=True, use_approved=False, redistribution_approved=False,
                    contract_signed=False, license_status=metadata['license_evidence']['status'],
                    binding=binding, note=note, at=at)
                raw = canonical(document)
                with self.tasks.transaction() as db:
                    db.execute('INSERT INTO author_initial_read_approvals VALUES(?,?,?,?,?)',
                        (identifier_sha, identifier, raw.decode(), sha256(raw), at))
        return self.status(identifier)

    def selection_input(self, identifier):
        metadata = self._metadata(identifier)
        approval = self._approved(identifier, metadata)
        result = dict(task_id=identifier, scope='reference_only', role='author_reference_human_only',
            B_input=False, author_plan_request_id=None,
            tree_request=deepcopy(metadata['tree_request']), tree_report=deepcopy(metadata['tree_report']),
            acquisition_receipt=deepcopy(metadata['acquisition_receipt']), existing_files=[],
            binding=dict(transport_identity_sha256=metadata['binding']['transport_identity_sha256'],
                         license_approval_sha256=approval['sha256']))
        history = self.workspace.planner.history(identifier)
        if history:
            # Once an actual author plan exists, use the unchanged established
            # revision protocol and its full prior answer, not another first run.
            self._completed(identifier)
            files = {}
            for receipt, context in self.workspace.selected_resources.completed(identifier):
                if (context['inventory_request'] != metadata['tree_request']
                        or context['inventory'] != metadata['tree_report']
                        or context['acquisition_receipt'] != metadata['acquisition_receipt']):
                    raise TaskError('补充原件与原固定清单不同；未改写前稿来源。')
                for row in receipt['files']:
                    item = {key: row[key] for key in ('path', 'size', 'sha256')}
                    if row['path'] in files and files[row['path']] != item:
                        raise TaskError('已取原件存在同路径不同摘要，不能合并为已有文件。')
                    files[row['path']] = item
            result.update(author_plan_request_id=history[-1]['request_id'],
                          existing_files=[files[path] for path in sorted(files)])
        else:
            sources = self.workspace.scopes.targets._input(identifier, active=False)['sources']
            result.update(mode='initial_scope_selection',
                reference_scope=deepcopy(metadata['reference_scope']), reference_sources=deepcopy(sources))
        return result

    def resource_binding(self, identifier):
        metadata = self._metadata(identifier)
        approval = self._approved(identifier, metadata)
        saved = metadata['binding']
        transport = SavedHPCTransport(self.bootstrap.hpc, saved['connection_revision'], saved['host_alias'])
        if sha256(canonical(transport.identity)) != saved['transport_identity_sha256']:
            raise TaskError('初始原件已固定的HPC连接变化，未迁移原资料。')
        request = metadata['acquisition_request']
        context = dict(version=1, **FLAGS, task_id=identifier, paper_id=metadata['paper_id'],
            repository=request['repository'], commit=request['commit'],
            original_receipt_sha256=sha256(canonical(metadata['acquisition_receipt'])),
            license_approval_sha256=approval['sha256'],
            transport_identity_sha256=saved['transport_identity_sha256'],
            transport_revision=saved['connection_revision'], host_alias=saved['host_alias'],
            remote_directory=request['directory'], storage_bytes=request['storage_bytes'],
            tool_bundle_sha256=sha256(tool_bundle()))
        return context, transport

    def _completed(self, identifier):
        metadata = self._metadata(identifier)
        source, transport = self.resource_binding(identifier)
        completed = self.workspace.selected_resources.completed(identifier)
        if not completed:
            raise TaskError('应用AI所选原件尚未真实取件完成；不能形成作者阅读绑定。')
        receipt, context = completed[0]
        if (not self.workspace.resources._same_binding(context['source_context'], source)
                or context['inventory_request'] != metadata['tree_request']
                or context['inventory'] != metadata['tree_report']
                or context['acquisition_receipt'] != metadata['acquisition_receipt']
                or receipt.get('state') != 'selected_complete'
                or receipt.get('selected_complete') is not True):
            raise TaskError('所选原件与原固定树、阅读确认或真实完成回执不一致。')
        return metadata, source, transport, receipt, context

    def load_binding(self, identifier):
        metadata, source, transport, receipt, context = self._completed(identifier)
        digest = sha256(canonical(receipt))
        policy = dict(host_alias=source['host_alias'], remote_directory=source['remote_directory'],
            reference_task_id=identifier, repository=source['repository'], commit=source['commit'],
            # Existing reader admission means permission to read, not a legal
            # license finding. Its digest points to our explicit purpose record.
            license_approved=True, license_approval_sha256=source['license_approval_sha256'],
            selection_request_sha256=sha256(canonical(context['request'])), receipt_sha256=digest)
        reader = SelectedAuthorSourceReader(self.workspace.resources.audit/'selected-source-reads', policy,
            selection_request=context['request'], tree_inventory=context['inventory'],
            original_receipt=context['acquisition_receipt'], transport=transport)
        reader.capability(receipt, purpose='author_reference')
        selections = [dict(path=row['path'], start_line=1, end_line=MAX_LINES) for row in receipt['files']
            if row['size'] <= MAX_FILE_BYTES and (PurePosixPath(row['path']).suffix.lower() in _TEXT_EXTENSIONS
                or PurePosixPath(row['path']).name.lower() in _TEXT_NAMES)]
        if not selections:
            raise TaskError('所选原件已保存，但没有可由现有有界文本工具读取的文件；原二进制文件保留未读。')
        selected_paths = {row['path'] for row in selections}
        unread = [row['path'] for row in receipt['files'] if row['path'] not in selected_paths]
        missing = list(dict.fromkeys(_MISSING + metadata['catalog_association'].get('missing', [])
            + context['selection_report'].get('missing', [])
            + (['以下原件未作为文本读取：'+', '.join(unread)] if unread else [])))
        association = dict(scope='reference_only', paper_id=metadata['paper_id'], paper=deepcopy(metadata['paper']),
            repository=receipt['repository'], commit=receipt['commit'], resource_receipt_sha256=digest,
            reading_approved=True, execution_authorized=False)

        def verify():
            current = self._completed(identifier)
            if (sha256(canonical(current[3])) != digest or current[4] != context
                    or current[1] != source):
                raise TaskError('作者原件阅读绑定已变化；旧回执保留，未重新解释。')

        def load_receipt():
            verify()
            return deepcopy(receipt)

        def load_association():
            verify()
            return deepcopy(association)

        return dict(paper_id=metadata['paper_id'], format='selected_acquisition_v1', reader=reader,
            load_receipt=load_receipt, load_association=load_association, load_intent=lambda: None,
            verify_license=verify, receipt_file_sha256=digest,
            receipt_proof=dict(kind='canonical_selected_receipt', operation_id=context['operation_id'],
                               sha256=digest, disk_artifact=False),
            selections=selections, unread_files=unread, invocation_paths=[],
            deployment=dict(environments=[], analysis_capabilities=[], resources=dict(gpu_allowed=False),
                            missing=deepcopy(missing)), missing=missing)

    def status(self, identifier):
        reason = ''
        try:
            self._task(identifier, active=False)
        except (ValueError, KeyError, TypeError, OSError, ExecutionDenied) as error:
            return dict(version=1, **FLAGS, task_id=identifier, enabled=False, disabled_reason=str(error),
                history=[], latest=None, reading_approved=False, read_approval_required=True,
                receipt_sha256=None, bootstrap_request_id=None, reading_approval=None, approval_history=[],
                binding_ready=False, missing=[str(error)], calls_model=False, submits_HPC=False)
        result = self.bootstrap.status(identifier)
        approvals = self._approvals(identifier)
        metadata = approval = None
        ready = False
        try:
            metadata = self._metadata(identifier)
            approval = self._approved(identifier, metadata)
        except (ValueError, KeyError, TypeError, OSError, ExecutionDenied) as error:
            reason = str(error)
        if approval is not None:
            try:
                self.load_binding(identifier)
                ready = True
            except (ValueError, KeyError, TypeError, OSError, ExecutionDenied) as error:
                reason = str(error)
        result.update(disabled_reason=result['disabled_reason'] or reason, reading_approved=approval is not None,
            read_approval_required=approval is None,
            receipt_sha256=metadata['bootstrap_receipt_sha256'] if metadata else None,
            bootstrap_request_id=metadata['bootstrap_request_id'] if metadata else None,
            reading_approval=deepcopy(approval), approval_history=approvals,
            binding_ready=ready, missing=list(_MISSING)+([reason] if reason else []),
            use_approved=False, redistribution_approved=False, calls_model=False, submits_HPC=False)
        return result
