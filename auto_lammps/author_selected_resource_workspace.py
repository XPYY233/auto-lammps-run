"""User-triggered acquisition of the application's saved reference-AI selection."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from .manifest import canonical, sha256
from .reference_selected_resources import ReferenceSelectedResources
from .reference_selected_resource_worker import (validate_selected_report,
    validate_selection_inventory, selection_identity)
from .tasks import TaskError, task_id


class AuthorSelectedResourceWorkspace:
    def __init__(self,workspace,*,factory=ReferenceSelectedResources):
        self.workspace=workspace;self.tasks=workspace.tasks;self.factory=factory
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_selected_operations ('
                'id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),selection_request_id TEXT NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS author_selected_events ('
                'id TEXT NOT NULL REFERENCES author_selected_operations(id),sequence INTEGER NOT NULL,'
                'document TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            for table in ('author_selected_operations','author_selected_events'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} '
                               'BEGIN SELECT RAISE(ABORT,"immutable selected author resources"); END')

    def _rows(self,identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            result=[]
            for row in db.execute('SELECT * FROM author_selected_operations WHERE task_id=? ORDER BY rowid',(identifier,)):
                context=json.loads(row['document'])
                if (sha256(canonical(context))!=row['sha256'] or context['task_id']!=identifier
                        or context['operation_id']!=row['id'] or context['selection_request_id']!=row['selection_request_id']):
                    raise TaskError('所选原件获取意图摘要不一致。')
                self._selection_binding(context['selection_report'],dict(tree_request=context['inventory_request'],
                    acquisition_receipt=context['acquisition_receipt'],tree_report=context['inventory']))
                if (context['selection_sha256']!=sha256(canonical(context['selection_report']))
                        or context['request']['id']!=row['id']
                        or context['request']['inventory_request']!=context['inventory_request']
                        or context['request']['selected']!=context['selections']
                        or context['selections']!=sorted(
                            [{key:item[key] for key in ('path','blob_sha1','size','git_mode')}
                             for item in context['selection_report']['selections']],key=lambda item:item['path'])):
                    raise TaskError('所选原件获取与原模型答复或请求绑定不一致。')
                validate_selection_inventory(context['request'],context['inventory'])
                events=[]
                for item in db.execute('SELECT document,sha256 FROM author_selected_events WHERE id=? ORDER BY sequence',(row['id'],)):
                    event=json.loads(item['document'])
                    if sha256(canonical(event))!=item['sha256']:raise TaskError('所选原件获取历史摘要不一致。')
                    if event.get('report') is not None:validate_selected_report(context['request'],event['report'])
                    events.append(event)
                result.append(dict(context=context,events=events))
        return result

    @staticmethod
    def _selection_binding(report,raw):
        original=raw['tree_request']['acquisition_request']
        expected=dict(repository=original['repository'],commit=original['commit'],
            tree_sha1=raw['tree_request']['tree_sha1'],
            acquisition_receipt_sha256=sha256(canonical(raw['acquisition_receipt'])),
            tree_report_sha256=sha256(canonical(raw['tree_report'])))
        if (not isinstance(report,dict) or any(report.get(key)!=value for key,value in expected.items())
                or report.get('status') not in {'selected','needs_clarification'}
                or not isinstance(report.get('selections'),list) or not report['selections']
                or (report.get('status')=='needs_clarification'
                    and (not isinstance(report.get('missing'),list) or not report['missing']))
                or any(report.get(key,False) is not False for key in
                    ('source_complete','execution_ready','physical_evaluation_performed',
                     'scientific_applicability_verified'))
                or report.get('scope')!='reference_only'
                or report.get('B_input') is not False or report.get('execution_authorized') is not False
                or raw['tree_request']['receipt_sha256']!=expected['acquisition_receipt_sha256']):
            raise TaskError('原模型选择与当前固定仓库、版本或清单回执不同；未取件。')

    def _tool(self,context):
        current,transport=self.workspace.resources._binding(context['task_id'])
        if not self.workspace.resources._same_binding(context['source_context'],current):
            raise TaskError('所选原件的来源、许可或保存的HPC身份已变化；不能迁移获取。')
        return self.factory(self.workspace.resources.audit/'selected',host_alias=current['host_alias'],
            acquisition_request=context['inventory_request']['acquisition_request'],
            acquisition_receipt=context['acquisition_receipt'],inventory=context['inventory'],transport=transport)

    def _event(self,operation,report=None,message=''):
        event=dict(at=datetime.now(timezone.utc).isoformat(),report=deepcopy(report),message=message)
        with self.tasks.transaction() as db:
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM author_selected_events WHERE id=?',(operation,)).fetchone()[0]
            db.execute('INSERT INTO author_selected_events VALUES(?,?,?,?)',
                       (operation,sequence,canonical(event).decode(),sha256(canonical(event))))

    def status(self,identifier):
        rows=self._rows(identifier);history=[]
        for row in rows:
            context=row['context'];events=row['events'];latest=events[-1] if events else {}
            report=latest.get('report') or {}
            resume=dict(resumable=False,resume_reason='known_partial_with_missing_files_required')
            if report.get('state')=='partial':
                try:
                    tool=self._tool(context)
                    # Status is a local immutable receipt; no SSH/model call.
                    if tool.status(context['operation_id'])!=report:
                        resume['resume_reason']='selected_receipt_changed'
                    else:resume=tool.resume_options(context['operation_id'])
                except (ValueError,KeyError,TypeError,OSError):
                    resume['resume_reason']='selected_operation_binding_unverified'
            history.append(dict(operation_id=context['operation_id'],selection_request_id=context['selection_request_id'],
                state=report.get('state','unknown'),selected_complete=report.get('selected_complete',False),
                source_complete=False,report=deepcopy(report),message=latest.get('message',''),
                recoverable=report.get('state','unknown')=='unknown',
                can_resume=resume['resumable'],**resume))
        return dict(task_id=identifier,scope='reference_only',B_input=False,execution_authorized=False,
                    physical_evaluation_performed=False,scientific_validation=False,history=history,
                    latest=history[-1] if history else None,calls_model=False,submits_HPC=False)

    def acquire(self,identifier,operation,selection_request):
        task_id(identifier);task_id(operation);task_id(selection_request)
        with self.workspace.resources._lease(identifier):
            with self.tasks.transaction() as db:
                owner=db.execute('SELECT task_id FROM author_selected_operations WHERE id=?',(operation,)).fetchone()
            if owner and owner[0]!=identifier:raise TaskError('所选原件获取标识属于另一任务。')
            rows=self._rows(identifier)
            saved=next((row for row in rows if row['context']['operation_id']==operation),None)
            if saved:
                context=saved['context']
                if context['selection_request_id']!=selection_request:raise TaskError('原获取已绑定另一选择，不能覆盖。')
                report=(saved['events'][-1].get('report') if saved['events'] else None)
                if report and report['state']=='selected_complete':return self.status(identifier)
                action='resume' if report and report['state']=='partial' else 'reconcile'
            else:
                if any((row['events'][-1].get('report') if row['events'] else None) is None
                       or row['events'][-1]['report']['state']=='unknown' for row in rows):
                    raise TaskError('原所选资料获取待核对；不能新建请求重发。')
                if any(row['context']['selection_request_id']==selection_request for row in rows):
                    raise TaskError('这份选择已有获取记录，请核对原标识，不另建重复获取。')
                if len(rows)>=8:raise TaskError('所选原件获取容量已用完；原记录保留。')
                report=self.workspace.resource_selections.selection(identifier,selection_request)
                raw=self.workspace._resource_selection_input(identifier)
                self._selection_binding(report,raw)
                source_context,_=self.workspace.resources._binding(identifier)
                context=dict(task_id=identifier,operation_id=operation,selection_request_id=selection_request,
                    selection_sha256=sha256(canonical(report)),selection_report=deepcopy(report),source_context=source_context,
                    inventory_request=raw['tree_request'],acquisition_receipt=raw['acquisition_receipt'],
                    inventory=raw['tree_report'],selections=sorted(
                        [{key:item[key] for key in ('path','blob_sha1','size','git_mode')} for item in report['selections']],
                        key=lambda item:item['path']))
                tool=self._tool(context);context['request']=tool.selection_request(operation,context['selections'])
                if any(selection_identity(row['context']['request'])==selection_identity(context['request']) for row in rows):
                    raise TaskError('相同原件集合已有获取记录；请核对原标识，不换选择编号重取。')
                with self.tasks.transaction() as db:
                    db.execute('INSERT INTO author_selected_operations VALUES(?,?,?,?,?,?)',
                        (operation,identifier,selection_request,canonical(context).decode(),sha256(canonical(context)),
                         datetime.now(timezone.utc).isoformat()))
                self._event(operation,message='所选原件获取意图已保存；未提交物理计算。')
                action='acquire'
            try:
                tool=self._tool(context)
                observed=(tool.acquire(operation,context['selections']) if action=='acquire'
                    else tool.resume(operation) if action=='resume' else tool.reconcile(operation))
                validate_selected_report(context['request'],observed)
                self._tool(context)  # Recheck the saved connection after transport.
            except (ValueError,KeyError,TypeError,OSError):
                self._event(operation,message='原获取状态尚未核对；保留原标识，不自动重取。')
                return self.status(identifier)
            self._event(operation,observed,'所选作者原件回执已保留；完整性仅限明确所选集合，不代表整仓或运行就绪。')
            return self.status(identifier)

    def completed(self,identifier):
        result=[]
        for row in self._rows(identifier):
            if not row['events']:continue
            report=row['events'][-1].get('report')
            if report and report['state']=='selected_complete' and report['selected_complete'] is True:
                self._tool(row['context'])
                result.append((deepcopy(report),deepcopy(row['context'])))
        return result
