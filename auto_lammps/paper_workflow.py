"""Human paper-workflow facade over existing stores and trusted adapters.

Discovery, literature P and author A stay on the reference side. This module
does not generate a scientific proposal, execute author code or release a blind
B package. Missing product capabilities remain explicit, disabled actions.
"""
import json
import re

from .discovery_library import DiscoveryLibrary
from .ledger import ACTIVE
from .manifest import canonical, sha256
from .papers import doi_text
from .potential_acquisition import safe_path
from .source_discovery import title_words
from .runtime_launcher import ExecutionDenied
from .target_planning import selection_readiness
from .tasks import StaleTask, TaskError


STATES = ('resource_check', 'resource_review', 'evidence_ready', 'target_selection',
          'reference_active', 'reference_results', 'independent_preparation', 'comparison')
LABELS = dict(zip(STATES, ('核对配套资源', '配套资源待核实', '查看论文图表', '选择复现范围',
                          '作者参考正在执行', '查看作者参考结果', '独立计算准备', '查看结果对比')))
ERRORS = (ValueError, KeyError, TypeError, OSError, ExecutionDenied)


def _files(rows):
    """Validate retained metadata, without reading/downloading simulation bytes."""
    if not isinstance(rows, list) or not rows or len(rows) > 128:
        return None
    found = {}
    try:
        for row in rows:
            name = safe_path(row['path'])
            if (name in found or type(row['size']) is not int or row['size'] < 0
                    or not isinstance(row['sha256'], str)
                    or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
                return None
            found[name] = {key: row[key] for key in ('path', 'size', 'sha256')}
    except ERRORS:
        return None
    return found


class PaperWorkflowService:
    def __init__(self, tasks, papers, *, discovery_library=None, paper_evidence_views=None,
                 source_discovery=None, reference_views=None, closeout_views=None,
                 workbench_status=None):
        if papers.tasks.path != tasks.path:
            raise ValueError('Paper workflow must share the task store')
        self.tasks, self.papers = tasks, papers
        self.library = discovery_library or DiscoveryLibrary()
        self.evidence = paper_evidence_views
        self.discovery = source_discovery
        self.references, self.closeouts = reference_views, closeout_views
        self.workbench_status = workbench_status

    def _paper(self, identifier):
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?', (identifier,)).fetchall()
        if len(rows) > 1:
            raise TaskError('任务关联的论文身份不唯一')
        return self.papers.get(rows[0][0]) if rows else None

    def _editable(self, identifier, revision, *, before_execution=False):
        task = self.tasks.get(identifier)
        if type(revision) is not int or task['revision'] != revision:
            raise StaleTask('任务记录已更新，请刷新后再操作')
        life = self.tasks.lifecycle(identifier)
        if life['deleted'] or life['user_finished']:
            raise TaskError('任务记录已结束，不能开始新步骤')
        if task['mode'] != 'reproduction':
            raise TaskError('普通科研任务不要求论文复现流程')
        paper = self._paper(identifier)
        if paper is None:
            raise TaskError('请先关联本次复现的论文')
        if before_execution:
            if task['status'] == 'conditions_frozen':
                raise TaskError('已冻结任务不能补作事前目标选择')
            reason = self._history_reason(paper, identifier)
            if reason:
                raise TaskError(reason)
        return task, paper

    @staticmethod
    def _history_reason(paper, identifier):
        linked = [e for e in paper['evaluations'] if e['task_id'] == identifier]
        if any(not e['available'] for e in linked):
            return '运行账本暂不可核对，不能补作事前目标或重置历史'
        if any(e.get('reserved_attempts', 0) > 0 or e.get('dispatch_claims', 0) > 0 for e in linked):
            return '已有运行预留或派发；论文图表仍可查看，不能补作事前目标'
        return ''

    def _resources(self, paper):
        catalog = self.library.get()
        groups, missing = {}, []
        for row in catalog['entries']:
            if (not row.get('doi') or doi_text(row['doi']) != paper['doi']
                    or title_words(row['paper_title']) != title_words(paper['title'])):
                continue
            # A repository example/test suite cannot stand in for author code.
            key = row['bundle_id']
            group = groups.setdefault(key, dict(id=key, repository=row['repository'], commit=row['commit'],
                source_type=row['source_type'], url=row['url'], kinds=[], entries=[],
                association_verified=True, candidate_complete=False, execution_ready=False))
            group['association_verified'] &= row['doi_state'] == 'verified_association'
            if row['kind'] == 'author_source' and row['repository_role'] != 'author_source':
                continue
            group['kinds'].append(row['kind'])
            group['entries'].append({k: row[k] for k in ('id', 'kind', 'title', 'paper_title', 'doi',
                'doi_state', 'pair_styles', 'elements', 'type_order', 'elements_complete',
                'potential_files', 'input_files', 'license', 'state', 'gaps')})
            group['execution_ready'] = False
        for group in groups.values():
            group['candidate_complete'] = (group['association_verified']
                and set(group['kinds']) == {'author_source', 'potential'}
                and all(e['state'] == 'candidate_needs_verification' for e in group['entries']))
        if not catalog['configured']:
            missing.append('资源库尚未配置；不能据此认定 GitHub 没有资源')
        elif not groups:
            missing.append('资源库未找到题名与 DOI 同时匹配的作者源码和势函数')
        elif not any(g['candidate_complete'] for g in groups.values()):
            missing.append('目录中配套资源、论文关联或冲突核验仍有缺项')

        # Reuse the existing reference acquisition receipts, never promote a
        # discovery row merely because it lists two kinds or a familiar name.
        source = paper.get('reference_resources') or {}
        potential = paper.get('potential_acquisition') or source.get('potential_acquisition') or {}
        matches = [c for c in paper.get('source_discovery', {}).get('candidates', [])
            if c.get('association') == 'doi_and_title' and c.get('repository') == source.get('repository')
            and c.get('commit') == source.get('commit')]
        source_files, models = _files(source.get('files')), _files(potential.get('files'))
        ready = bool(matches and source.get('state') == 'finished' and source.get('location') == 'hpc'
            and source.get('source_complete') is True and source_files
            and source.get('inventory_sha256') == sha256(canonical(source['files']))
            and potential.get('state') == 'finished' and potential.get('location') == 'hpc'
            and all(potential.get(k) == source.get(k) for k in ('repository', 'commit')) and models)
        bindings = potential.get('bindings')
        if not isinstance(bindings, list) or not bindings:
            ready = False
        else:
            for binding in bindings:
                if not isinstance(binding, dict):
                    ready = False
                    continue
                path = binding.get('input_path')
                files = binding.get('files')
                if (binding.get('static_status') != 'checked' or not source_files or path not in source_files
                        or source_files[path]['sha256'] != binding.get('input_sha256')
                        or not isinstance(files, dict) or not files or not models
                        or any(name not in models or name not in source_files
                               or models[name] != source_files[name] for name in files.values())):
                    ready = False
        if not ready:
            missing.append('固定版本的 HPC 源码文件、势函数完整映射及获取回执尚未全部核对')
        search = paper.get('source_discovery')
        github = dict(state='not_searched', search_exhaustive=False, matched_repositories=[], failures=0)
        if search:
            github.update(state=search['state'], matched_repositories=[
                {k: c[k] for k in ('repository', 'commit', 'association')}
                for c in search['candidates']], failures=len(search.get('failures', [])))
        return dict(catalog_checked=bool(catalog['configured']), catalog_sha256=(catalog.get('source') or {}).get('sha256'),
            source_ready=ready, bundles=list(groups.values()), missing=missing,
            github=github, scientific_validation=False, execution_authorized=False,
            receipt_sha256=sha256(canonical(source)) if source else None,
            source_note='source_ready 仅说明既有文件和静态依赖回执齐备，不等于可提交或科学通过')

    def _b_status(self, task):
        available = task['status'] == 'conditions_frozen'
        return dict(preview_available=available, release_status='operator_review_required', released=False,
            disabled_reason='草稿仅供人类审阅；条件语义、允许资源与运行隔离的受信发布尚未接入'
                if available else '先完成条件确认与复现范围冻结，才能查看独立计算条件草稿')

    def get(self, identifier):
        task = self.tasks.get(identifier)
        if task['mode'] != 'reproduction':
            return dict(schema_version=1, task_id=identifier, mode=task['mode'], applicable=False,
                        paper=None, state=None, actions=[], execution_authorized=False)
        paper = self._paper(identifier)
        if paper is None:
            return dict(schema_version=1, task_id=identifier, mode=task['mode'], applicable=True,
                paper=None, state='resource_check', title=LABELS['resource_check'], actions=[],
                notice='请先在文献清单中关联要复现的论文', execution_authorized=False)
        life = self.tasks.lifecycle(identifier)
        closed = life['deleted'] or life['user_finished']
        resources = self._resources(paper)
        errors = []
        evidence, reference, comparison = None, None, None
        for name, provider in (('paper_evidence', self.evidence), ('reference_result', self.references)):
            if provider is None:
                continue
            try:
                report = provider.get(identifier)
                if report is not None and (report['task_id'] != identifier or report['doi'] != paper['doi']
                                           or report['title'] != paper['title']):
                    raise TaskError('展示证据的论文或任务身份不一致')
                if name == 'paper_evidence':
                    evidence = report
                else:
                    reference = report
            except ERRORS:
                errors.append(dict(code=name+'_unavailable', message='对应证据的来源或文件暂未通过核验'))
        if self.closeouts:
            try:
                # CloseoutViews owns the legacy P/A/B report binding; the new
                # A-only evidence package is not that report's identity.
                comparison = self.closeouts.get(identifier)
            except ERRORS:
                errors.append(dict(code='comparison_unavailable', message='结果对比的来源或文件暂未通过核验'))
        progress = self.papers.reference_progress(identifier)
        history_reason = self._history_reason(paper, identifier)
        reason = ('任务已结束，历史图表仍可查看' if closed else
                  '已冻结任务不能补作事前目标选择' if task['status'] == 'conditions_frozen' else history_reason)
        target = task.get('target_inventory')
        selected = task.get('target_selection') or {}
        readiness = selection_readiness(task, selected.get('selected_ids', []), selected.get('exclusion_reason', ''))
        targets = dict(imported=target is not None, selected_ids=selected.get('selected_ids', []),
            can_import=bool(evidence and evidence['target_inventory'] and not reason
                            and evidence['target_import_allowed']),
            can_select=bool(target and not reason), can_freeze=bool(readiness['can_freeze'] and not reason),
            disabled_reason=reason, readiness=readiness)
        priorities = []
        if evidence and evidence['target_inventory']:
            priorities = [dict(id=r['id'], label=r['label'], priority=evidence['priorities'].get(r['id'], 3),
                availability=r['availability'], condition_group=r['condition_group'])
                for r in evidence['target_inventory']['targets']]
            priorities.sort(key=lambda row: row['priority'])
        b_status = self._b_status(task)
        active = any(r['state'] in ACTIVE
                     for entry in progress['entries'] for r in entry['evaluation'].get('requests', []))
        state = ('comparison' if comparison else 'reference_active' if active else
                 'reference_results' if reference else 'independent_preparation' if b_status['preview_available'] else
                 'target_selection' if target else 'evidence_ready' if evidence else
                 'resource_review' if resources['bundles'] or resources['github']['state'] != 'not_searched'
                 else 'resource_check')
        base = '/api/tasks/'+identifier
        workbench = self.workbench_status(identifier) if self.workbench_status else {}
        def action(key, label, enabled, disabled_reason='', method=None, endpoint=None):
            return dict(id=key, label=label, enabled=bool(enabled), disabled_reason='' if enabled else disabled_reason,
                        method=method, endpoint=endpoint)
        actions = [
            action('check_resources', '核对已有源码与势函数', not closed, '任务已结束', 'POST', base+'/paper-workflow/resources'),
            action('search_github', '查找公开 GitHub 来源', not closed and self.discovery is not None
                and not resources['source_ready'] and not any(g['candidate_complete'] for g in resources['bundles'])
                and resources['github']['state'] == 'not_searched',
                '已复用目录或已有检索记录；未配置检索服务时不能声称已搜索', 'POST', base+'/paper-workflow/search'),
            action('view_P', '查看 P：论文原图与数据', evidence is not None, '既有文献工作台产物尚未登记', 'GET', base+'/paper-evidence'),
            action('extract_P', '从文献工作台提取 P', not closed and workbench.get('enabled', False),
                workbench.get('disabled_reason', '用户端全文提取调用入口尚未接入；已登记 P 可直接查看'),
                'GET', base+'/workbench'),
            action('import_targets', '使用已提取的图表目标', targets['can_import'], reason or '缺少已核验的工作台图表目标', 'POST', base+'/paper-evidence/import-targets'),
            action('select_targets', '选择本次复现范围', targets['can_select'], reason or '先导入图表目标', 'POST', base+'/targets'),
            action('freeze_targets', '确认范围与分析标准', targets['can_freeze'], reason or '先保存目标范围并确认研究条件', 'POST', base+'/freeze'),
            action('approve_A', '批准作者源码运行 A', False, '作者完整调用配方、环境与记账提交的用户入口尚未发布；不另写替代 A'),
            action('view_A', '查看 A：作者源码运行结果', reference is not None, '已有参考进度可查看；结果分析产物尚未接入', 'GET', base+'/reference-result'),
            action('analyze_A', '处理并可视化 A', False, '作者原始输出的通用受信分析入口尚未接入'),
            action('preview_B_draft', '查看独立 B 条件草稿', b_status['preview_available'], b_status['disabled_reason'], 'GET', base+'/paper-workflow/b-draft'),
            action('start_B', '在普通科研流程开始独立 B', False, b_status['disabled_reason']),
            action('compare', '查看论文 P 与计算 A/B 对比', comparison is not None, '对比报告与各来源绑定尚未齐备', 'GET', base+'/reference-result')]
        return dict(schema_version=1, task_id=identifier, mode=task['mode'], applicable=True,
            paper={key: paper[key] for key in ('id', 'title', 'doi', 'doi_url')}, state=state, title=LABELS[state],
            notice='P 为论文结果，A 为作者源码运行，B 为应用内 AI 独立计算；来源齐备不等于科学通过',
            resources=resources, evidence=dict(available=evidence is not None,
                source_sha256=evidence['source_sha256'] if evidence else None,
                manifest_sha256=evidence['manifest_sha256'] if evidence else None, priorities=priorities),
            targets=targets, reference=dict(progress=progress, report_available=reference is not None,
                scientific_status=reference['scientific_status'] if reference else 'not_evaluated'),
            b_draft=b_status, comparison=dict(available=comparison is not None), errors=errors,
            actions=actions, execution_authorized=False)

    def prepare_resources(self, identifier, revision):
        self._editable(identifier, revision)
        # Intentionally a fresh catalog/receipt read, not a download or run.
        return self.get(identifier)

    def search_github(self, identifier, revision):
        _, paper = self._editable(identifier, revision)
        resources = self._resources(paper)
        if resources['source_ready'] or any(g['candidate_complete'] for g in resources['bundles']) or paper.get('source_discovery'):
            return self.get(identifier)
        if self.discovery is None:
            raise TaskError('公开源码检索服务尚未配置，不能声称已搜索 GitHub')
        report = self.discovery.discover(paper['title'], paper['doi'])
        # Existing controller validation checks exact title/DOI and retains the
        # immutable discovery history. Search never downloads author source.
        self.papers.record_source_search(paper['id'], report)
        return self.get(identifier)

    def import_targets(self, identifier, revision):
        self._editable(identifier, revision, before_execution=True)
        report = self.evidence.get(identifier) if self.evidence else None
        if not report or not report['target_inventory'] or not report['target_import_allowed']:
            raise TaskError('缺少可在执行前导入的工作台目标清单')
        return self.tasks.import_target_inventory(identifier, revision, report['target_inventory'])

    def select_targets(self, identifier, revision, selected_ids, exclusion_reason):
        self._editable(identifier, revision, before_execution=True)
        return self.tasks.select_targets(identifier, revision, selected_ids, exclusion_reason)

    def freeze(self, identifier, revision):
        self._editable(identifier, revision, before_execution=True)
        return self.tasks.freeze(identifier, revision)

    def get_B_draft(self, identifier):
        task = self.tasks.get(identifier)
        if task['mode'] != 'reproduction' or self._paper(identifier) is None:
            raise TaskError('需要已关联论文的复现任务')
        # Reuse the frozen-record verifier and existing allowlist projection.
        # The draft intentionally retains all existing non-release flags. No P
        # viewer, author report, target criteria or source code is consulted.
        package = self.tasks.export_packages(identifier)
        draft = json.loads(package['execution'])
        return dict(schema_version=1, task_id=identifier, role='human_only_unreleased_condition_draft',
            released=False, execution_authorized=False, draft_sha256=sha256(package['execution']),
            draft=draft, disabled_reason=self._b_status(task)['disabled_reason'])
