"""Ordinary reference plans and an explicitly configured accounted A controller.

Only private deployment bindings supply source roots, license decisions and
fixed executables. Browser inputs select a request and a revision note. Source
quotes are retained as reference evidence, not installed as a local source tree.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import traceback

from .author_reference_plan import AuthorReferencePlan, author_source_inputs, _deployment
from .author_source_reader import AuthorSourceReader
from .deepseek import ModelError
from .hpc_transport import SavedHPCTransport
from .manifest import canonical, private_directory, sha256
from .runtime_launcher import ExecutionDenied, read_regular
from .tasks import TaskError, task_id, text


class AuthorReferenceWorkspace:
    def __init__(self,tasks,papers,scopes,bindings,*,model_provider,connection_provider,model_calls=None):
        self.tasks,self.papers,self.scopes=tasks,papers,scopes
        self.bindings=bindings;self.model_provider=model_provider
        self.execution=None
        self.execution_services={}
        self.initial=None
        self.environment_inventory=None
        self.reference_inputs=None
        self.lane_deployment=None
        self.model_calls=model_calls
        self.current=ContextVar('author_reference_current',default=None)
        self.metadata_action=ContextVar('author_reference_metadata_action',default=None)
        self.locks=private_directory(tasks.path.parent/'author-workspace-locks')
        self.planner=AuthorReferencePlan(tasks,papers,scopes,self._input,connection_provider=connection_provider)
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_source_evidence (id TEXT PRIMARY KEY,task_id TEXT NOT NULL,document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_author_source_evidence_{action} BEFORE {action} ON author_source_evidence BEGIN SELECT RAISE(ABORT,"immutable author source evidence"); END')
        from .author_reference_resources import AuthorReferenceResources
        self.resources=AuthorReferenceResources(self)
        from .author_resource_selection import AuthorResourceSelection
        self.resource_selections=AuthorResourceSelection(self.planner,self._resource_selection_input,
                                                        connection_provider=connection_provider,model_calls=model_calls)
        from .author_selected_resource_workspace import AuthorSelectedResourceWorkspace
        self.selected_resources=AuthorSelectedResourceWorkspace(self)

    def _resource_selection_input(self,identifier):
        """Read only pinned retained metadata; no source fetch or model on GET."""
        if identifier not in self.bindings and self.initial is not None:
            return self.initial.selection_input(identifier)
        binding,_,receipt,_=self._metadata(identifier)
        context,_=self.resources._binding(identifier)
        for row in reversed(self.resources._rows(identifier)):
            if not self.resources._same_binding(row['context'],context):continue
            for inspection in reversed(row.get('inspections',[])):
                if inspection['report']['state']!='inspected':continue
                history=self.planner.history(identifier)
                if not history:raise TaskError('请先保留作者原调用方案及具体原件缺项。')
                return dict(task_id=identifier,scope='reference_only',role='author_reference_human_only',
                    B_input=False,author_plan_request_id=history[-1]['request_id'],
                    tree_request=deepcopy(inspection['request']),tree_report=deepcopy(inspection['report']),
                    acquisition_receipt=deepcopy(inspection['acquisition_receipt']),
                    existing_files=[{key:item[key] for key in ('path','size','sha256')} for item in receipt['files']],
                    binding={key:context[key] for key in ('transport_identity_sha256','license_approval_sha256')})
        raise TaskError('请先核对原有获取记录具体清单；没有固定缓存树时不调用模型选原件。')

    def resource_selection_status(self,identifier):
        result=self.resource_selections.status(identifier)
        ready=False;reason=''
        try:
            self.resource_selections._input(identifier)
            if not self.model_provider(identifier)['enabled']:raise TaskError('请先连接本项目运行模型。')
            ready=True
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            reason=str(error) if isinstance(error,TaskError) else '原件选择的固定来源或连接尚未核验。'
        result.update(enabled=ready,can_prepare=ready and result['remaining_rounds']>0
                      and not result['pending_reconciliation'],disabled_reason=reason,
                      calls_model_on_GET=False,submits_HPC=False,
                      acquisitions=self.selected_resources.status(identifier))
        return result

    def prepare_resource_selection(self,identifier,request,*,revision_note=''):
        task_id(identifier);task_id(request)
        model=self.model_provider(identifier)
        if not model['enabled']:raise TaskError('请先连接本项目运行模型。')
        with self.resources._lease(identifier):
            return self.resource_selections.prepare(identifier,request,model['client'],
                connection_revision=model['connection_revision'],credential_generation=model['credential_generation'],
                revision_note=revision_note)

    @contextmanager
    def _metadata_reads(self,identifier,request):
        task_id(identifier);task_id(request)
        if self.metadata_action.get() is not None:
            raise TaskError('作者元数据读取域不能跨任务或请求复用。')
        state=dict(task_id=identifier,request_id=request,cache={},probes={})
        token=self.metadata_action.set(state)
        try:yield
        finally:
            state['cache'].clear();state['probes'].clear()
            self.metadata_action.reset(token)

    def _metadata_read(self,key,identifier,load,*,fingerprint=canonical,clone=deepcopy):
        state=self.metadata_action.get()
        if state is None:return load()
        if state['task_id']!=identifier:raise TaskError('作者元数据读取域不能跨任务复用。')
        if key not in state['cache']:
            value=load();digest=sha256(fingerprint(value))
            prior=state['probes'].get(key)
            if prior is not None and prior['digest']!=digest:
                raise TaskError('准备期间作者来源、范围、许可、连接或环境已变化；未沿用读取快照。')
            state['probes'][key]=dict(identifier=identifier,load=load,fingerprint=fingerprint,clone=clone,digest=digest)
            state['cache'][key]=clone(value)
        return clone(state['cache'][key])

    def _invalidate_metadata_reads(self):
        state=self.metadata_action.get()
        if state is not None:state['cache'].clear()

    def _validate_metadata_reads(self):
        state=self.metadata_action.get()
        if state is None:return
        # Verify from fresh reads, sharing only within this independent pass.
        # Never validate a cached value against itself or replace its baseline.
        fresh=dict(task_id=state['task_id'],request_id=state['request_id'],cache={},probes={})
        token=self.metadata_action.set(fresh)
        try:
            for key,probe in list(state['probes'].items()):
                value=self._metadata_read(key,probe['identifier'],probe['load'],
                    fingerprint=probe['fingerprint'],clone=probe['clone'])
                if sha256(probe['fingerprint'](value))!=probe['digest']:
                    raise TaskError('准备期间作者来源、范围、许可、连接或环境已变化；未发送旧快照。')
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            if isinstance(error,TaskError):raise
            raise TaskError('准备期间作者来源、范围、许可、连接或环境未通过重新核验；未发送旧快照。') from None
        finally:
            fresh['cache'].clear();fresh['probes'].clear()
            self.metadata_action.reset(token)

    @staticmethod
    def _clone_metadata(value):
        binding,paper,receipt,scope=value
        # Keep the trusted reader/verification callbacks; copy caller-visible
        # metadata so one consumer cannot mutate another consumer's snapshot.
        binding={key:item if key=='reader' or callable(item) else deepcopy(item) for key,item in binding.items()}
        return binding,deepcopy(paper),deepcopy(receipt),deepcopy(scope)

    def _metadata_fingerprint(self,value):
        binding,paper,receipt,scope=value;reader=binding['reader']
        transport=getattr(reader,'transport',None)
        task=self.tasks.get(binding['reader'].policy['reference_task_id'])
        return canonical(dict(binding={key:item for key,item in binding.items() if key!='reader' and not callable(item)},
            reader_policy=reader.policy,transport_identity=transport.identity if transport else None,
            paper={key:paper[key] for key in ('id','title','doi')},receipt=receipt,scope=scope,
            task_revision=task['revision']))

    def _metadata(self,identifier,*,require_scope=True):
        return self._metadata_read(('metadata',identifier,require_scope),identifier,
            lambda:self._load_metadata(identifier,require_scope=require_scope),
            fingerprint=self._metadata_fingerprint,clone=self._clone_metadata)

    def _load_metadata(self,identifier,*,require_scope=True):
        task=self.tasks.get(identifier)
        if task['mode']!='reproduction':raise TaskError('作者运行方案只属于论文参考任务。')
        binding=self.bindings.get(identifier)
        if binding is None and self.initial is not None:
            binding=self.initial.load_binding(identifier)
        if binding is None:raise TaskError('已保存的作者源码、许可与固定部署尚未连接；未发起读取或模型调用。')
        with self.tasks.transaction() as db:
            papers=[r[0] for r in db.execute('SELECT paper_id FROM paper_tasks WHERE task_id=?',(identifier,))]
        if papers!=[binding['paper_id']]:raise TaskError('作者绑定与本任务唯一论文身份不一致。')
        paper=self.papers.get(binding['paper_id'])
        receipt=binding['load_receipt']();association=binding['load_association']()
        if (association!={'scope':'reference_only','paper_id':paper['id'],
                'paper':{key:paper[key] for key in ('title','doi')},
                'repository':receipt['repository'],'commit':receipt['commit'],
                'resource_receipt_sha256':binding['receipt_file_sha256'],
                'reading_approved':True,'execution_authorized':False}):
            raise TaskError('作者文件与论文关联回执不符；不能借用另一篇源码。')
        binding['verify_license']()
        transport=getattr(binding['reader'],'transport',None)
        if transport is not None and transport._identity()!=transport.identity:
            raise TaskError('已固定的作者来源连接变化；未读取或迁移旧证据。')
        reader=binding['reader'];legacy=binding['format']=='legacy_selected_resource_v3'
        intent=binding['load_intent']() if legacy else None
        if legacy:reader.capability(receipt,intent,purpose='author_reference')
        else:reader.capability(receipt,purpose='author_reference')
        _deployment(binding['deployment'])
        selections=binding['selections']
        if not isinstance(selections,list) or not 1<=len(selections)<=128:
            raise TaskError('作者读取选择须明确且有界，不能默默删减源码。')
        for index in range(0,len(selections),8):
            batch=selections[index:index+8]
            if legacy:reader.selection_request(receipt,intent,batch)
            else:reader._selection(receipt,batch)
        scope=self.scopes.get(identifier)
        if require_scope and (not scope or scope['state']!='recorded'):raise TaskError('请先保存并核对来自 P 的目标与共享依赖范围。')
        if self.tasks.paused(identifier) or self.tasks.lifecycle(identifier)['user_finished'] or self.tasks.lifecycle(identifier)['deleted']:
            raise TaskError('任务已暂停或结束；旧方案仍可查看。')
        return self._environment_metadata(identifier,binding),paper,receipt,scope

    def _environment_metadata(self,identifier,binding):
        """Append current saved metadata menus; never check the remote on GET.

        Reading permission, original source pins and an executable metadata
        receipt are separate evidence. None of them authorizes an author entry
        point or an otherwise unreviewed child process.
        """
        inventory=self.environment_inventory
        if inventory is None:return binding
        result=dict(binding,deployment=deepcopy(binding['deployment']),missing=deepcopy(binding.get('missing',[])))
        evidence=dict(state='unavailable',execution_authorized=False,execution_ready=False,
                      reads_remote=False,calls_model=False)
        gaps=[]
        try:
            if inventory.tasks.path.resolve()!=self.tasks.path.resolve():
                raise TaskError('环境记录不属于当前项目。')
            latest=inventory.status(identifier)['latest']
            if latest is None:
                # Optional new metadata must not invalidate an already pinned
                # author deployment or invent a new preparation prerequisite.
                return binding
            elif latest['state']!='metadata_ready':
                evidence.update(state=latest['state'],request_id=latest['request_id'])
                gaps.append('本任务最新环境核验尚未就绪；原回执保留，未自动重新检查。')
            else:
                menu=inventory.planner_environments(identifier,latest['request_id'])
                current,environment_transport=inventory._context(identifier)
                reader_transport=getattr(binding['reader'],'transport',None)
                if not isinstance(reader_transport,SavedHPCTransport):
                    raise TaskError('作者读取尚未绑定保存的项目连接。')
                # The alias is only a transport label. Everything identifying
                # the saved project/profile/revision/credential must match.
                author_identity={key:value for key,value in reader_transport.identity.items() if key!='host_alias'}
                environment_identity={key:value for key,value in environment_transport.identity.items() if key!='host_alias'}
                if (reader_transport._identity()!=reader_transport.identity
                        or environment_transport._identity()!=environment_transport.identity
                        or author_identity!=environment_identity
                        or menu['task_id']!=identifier or menu['metadata_status']!='metadata_ready'
                        or menu['report_sha256']!=latest['report_sha256']
                        or any(menu.get(key) is not False for key in ('B_input','execution_authorized',
                            'physical_evaluation_performed','scientific_validation','execution_ready',
                            'child_process_policy_verified','author_entrypoints_reviewed'))):
                    raise TaskError('环境与作者保存连接或原回执不一致。')
                deployment=result['deployment'];environments=deepcopy(deployment['environments'])
                existing={item['id']:item for item in environments}
                for item in menu['environments']:
                    if item['id'] in existing:
                        if existing[item['id']]!=item:raise TaskError('环境菜单编号冲突；原菜单保留。')
                    else:
                        environments.append(deepcopy(item));existing[item['id']]=item
                candidate=dict(deployment,environments=environments)
                _deployment(candidate)
                result['deployment']=candidate
                gaps.extend(menu['missing'])
                if not current['required_distributions']:
                    gaps.append('此次仅核验解释器元数据；作者流程所需依赖尚待核验，不能视为依赖完备。')
                evidence.update(state='metadata_ready',request_id=latest['request_id'],
                    report_sha256=latest['report_sha256'],
                    checked_distributions=deepcopy(current['required_distributions']),
                    author_dependencies_complete=False)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied):
            evidence.update(state='unavailable')
            gaps=['已有环境来源、保存连接或核验回执已变化或尚未核实；旧菜单与历史保留，未自动访问HPC。']
        for gap in gaps:
            if gap not in result['missing']:result['missing'].append(gap)
            if gap not in result['deployment']['missing']:result['deployment']['missing'].append(gap)
        result['environment_metadata']=evidence
        return result

    def _source(self,identifier,binding,receipt,*,cached_only=False):
        reader=binding['reader'];legacy=binding['format']=='legacy_selected_resource_v3'
        selected=binding['format']=='selected_acquisition_v1'
        intent=binding['load_intent']() if legacy else None
        capability=(reader.capability(receipt,intent,purpose='author_reference') if legacy
                    else reader.capability(receipt,purpose='author_reference'))
        selections=binding['selections']
        if not isinstance(selections,list) or not (0 if selected else 1)<=len(selections)<=128:
            raise TaskError('作者读取选择须明确且有界，不能默默删减源码。')
        requests=[]
        for index in range(0,len(selections),8):
            batch=selections[index:index+8]
            request=(reader.selection_request(receipt,intent,batch) if legacy else reader._selection(receipt,batch))
            requests.append(request)
        key=sha256(canonical(dict(task_id=identifier,capability=capability,requests=requests)))
        with self.tasks.transaction() as db:
            row=db.execute('SELECT document,sha256 FROM author_source_evidence WHERE id=? AND task_id=?',(key,identifier)).fetchone()
        if row:
            value=json.loads(row[0])
            if sha256(canonical(value))!=row[1]:raise TaskError('已保存作者读取证据摘要不一致。')
            author_source_inputs(value,identifier)
            return value
        if cached_only:
            raise TaskError('作者原件文本缓存尚未就绪；请先使用读取原件入口，未自动访问HPC。')
        with self.tasks.transaction() as db:
            used=db.execute('SELECT count(*) FROM author_source_evidence WHERE task_id=?',(identifier,)).fetchone()[0]
            if used>=32:raise TaskError('参考源码证据版本容量已用完；已有版本和费用保留。')
        reads=[]
        for index,request in enumerate(requests):
            batch=selections[index*8:(index+1)*8]
            report=(reader.read(receipt,intent,batch,purpose='author_reference') if legacy
                    else reader.read(receipt,batch,purpose='author_reference'))
            if report.get('state')!='read':raise TaskError('固定作者文本读取未完成；未发送方案请求，原读取记录保留。')
            reads.append(dict(request=request,response=report))
        value=dict(capability=capability,acquisition_receipt=receipt,reads=reads)
        if legacy:value['legacy_staging_intent']=intent
        if selected:value=reader.retained(receipt,reads)
        author_source_inputs(value,identifier)
        data=canonical(value)
        with self.tasks.transaction() as db:
            used=db.execute('SELECT count(*) FROM author_source_evidence WHERE task_id=?',(identifier,)).fetchone()[0]
            if used>=32:raise TaskError('参考源码证据版本容量已用完；已有版本和费用保留。')
            db.execute('INSERT INTO author_source_evidence VALUES(?,?,?,?,?)',(key,identifier,data.decode(),sha256(data),datetime.now(timezone.utc).isoformat()))
        self._invalidate_metadata_reads()
        return value

    def _sources(self,identifier,binding,receipt,*,cached_only=False):
        """Retain the original package and append separately pinned new text.

        File-type selection is a bounded reading tool, not a scientific scope
        decision. Unread or truncated files remain explicit in the returned
        inventory and are never interpreted as fully inspected source.
        """
        original=self._source(identifier,binding,receipt,cached_only=cached_only)
        packages=[original]
        completed=self.resources.completed(identifier) if isinstance(getattr(binding['reader'],'transport',None),SavedHPCTransport) else None
        previous={row['path'] for row in receipt['files']}
        text_extensions={'.py','.sh','.in','.lmp','.lammps','.md','.txt','.json','.yaml','.yml','.toml','.cfg'}
        def choices(report):
            return [dict(path=row['path'],start_line=1,end_line=400) for row in report['files']
                if row['path'] not in previous and row['size']<=256*1024
                and (PurePosixPath(row['path']).suffix.lower() in text_extensions
                     or PurePosixPath(row['path']).name.lower() in {'readme','license','makefile'})]
        if completed is not None:
            report,context=completed;selections=choices(report)
            if selections:
                policy=dict(host_alias=context['host_alias'],remote_directory=context['remote_directory'],
                    reference_task_id=identifier,repository=context['repository'],commit=context['commit'],
                    acquisition_id=report['id'],receipt_sha256=sha256(canonical(report)),
                    inventory_sha256=report['inventory_sha256'],license_approved=True,
                    license_approval_sha256=context['license_approval_sha256'])
                audit=self.resources.audit/'source-reads'
                if cached_only and (audit.is_symlink() or not audit.is_dir()):
                    raise TaskError('补充作者原件文本缓存尚未就绪；未自动访问HPC。')
                reader=AuthorSourceReader(audit,policy,transport=binding['reader'].transport)
                packages.append(self._source(identifier,dict(reader=reader,format='acquisition_v1',selections=selections),report,cached_only=cached_only))
                previous.update(row['path'] for row in report['files'])
        if isinstance(getattr(binding['reader'],'transport',None),SavedHPCTransport):
            from .selected_author_source_reader import SelectedAuthorSourceReader
            for report,context in self.selected_resources.completed(identifier):
                if (binding['format']=='selected_acquisition_v1'
                        and sha256(canonical(report))==sha256(canonical(receipt))):
                    continue
                source=context['source_context']
                policy=dict(host_alias=source['host_alias'],remote_directory=source['remote_directory'],
                    reference_task_id=identifier,repository=source['repository'],commit=source['commit'],
                    license_approved=True,license_approval_sha256=source['license_approval_sha256'],
                    selection_request_sha256=sha256(canonical(context['request'])),receipt_sha256=sha256(canonical(report)))
                audit=self.resources.audit/'selected-source-reads'
                if cached_only and (audit.is_symlink() or not audit.is_dir()):
                    raise TaskError('所选作者原件文本缓存尚未就绪；未自动访问HPC。')
                reader=SelectedAuthorSourceReader(audit,policy,
                    selection_request=context['request'],tree_inventory=context['inventory'],
                    original_receipt=context['acquisition_receipt'],transport=binding['reader'].transport)
                packages.append(self._source(identifier,dict(reader=reader,format='selected_acquisition_v1',selections=choices(report)),report,cached_only=cached_only))
                previous.update(row['path'] for row in report['files'])
        if len(packages)==1:return original
        combined=dict(source_packages=packages)
        author_source_inputs(combined,identifier)
        return combined

    def _cached_sources(self,identifier,binding,receipt):
        key=('cached_sources',identifier,sha256(canonical(receipt)))
        return self._metadata_read(key,identifier,lambda:self._sources(identifier,binding,receipt,cached_only=True))

    def cached_analysis_sources(self,identifier):
        """Reference-only retained text; never read remote files or invoke AI."""
        task_id(identifier)
        result=dict(task_id=identifier,paper_id=None,paper=None,scope='reference_only',B_input=False,
            available=False,sources=[],source_binding=None,missing=[],calls_model=False,
            reads_remote=False,submits_HPC=False)
        try:
            binding,paper,receipt,_=self._metadata(identifier,require_scope=False)
            result.update(paper_id=paper['id'],paper={key:paper[key] for key in ('title','doi')})
            retained=self._cached_sources(identifier,binding,receipt)
            checked=author_source_inputs(retained,identifier)
            from .condition_generation import source_bundle
            sources=[{key:row[key] for key in ('id','locator','text')} | dict(origin='code')
                for row in checked['sources']]
            # Validate the shared input envelope without stripping byte-proven
            # source text; a downstream normalized view is a separate value.
            source_bundle(sources)
            if not sources:raise TaskError('作者原件缓存没有可核验文本；未自行补充方法或元素映射。')
            source_binding=dict(kind='cached_author_method_sources',task_id=identifier,paper_id=paper['id'],
                role='author_reference_human_only',B_input=False,
                original_receipt_sha256=sha256(canonical(receipt)),
                original_receipt_file_sha256=binding['receipt_file_sha256'],
                retained_evidence_sha256=sha256(canonical(retained)),
                capability_sha256=sha256(canonical(checked['capability'])),
                files_sha256=sha256(canonical(checked['files'])),
                read_proofs_sha256=sha256(canonical(checked['reads'])),
                sources_sha256=sha256(canonical(sources)))
            result.update(available=True,sources=sources,source_binding=source_binding)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            result['missing']=[str(error) if isinstance(error,TaskError)
                else '作者方法的固定原件、许可或读取回执尚未通过核验；未自动访问HPC。']
        return result

    def collect_sources(self,identifier):
        """Ordinary reference action; read pinned data without a model call."""
        task_id(identifier)
        with self.resources._lease(identifier):
            binding,_,receipt,_=self._metadata(identifier,require_scope=False)
            value=author_source_inputs(self._sources(identifier,binding,receipt),identifier)
            read={row['path'] for row in value['reads'] if row.get('state')=='read' and row.get('path')}
            return dict(version=1,task_id=identifier,scope='reference_only',B_input=False,
                calls_model=False,submits_HPC=False,execution_authorized=False,
                physical_evaluation_performed=False,scientific_validation=False,
                files=len(value['files']),read_files=len(read),source_fragments=len(value['sources']),
                unread_files=[row['path'] for row in value['files'] if row['path'] not in read],
                truncated_files=[row['path'] for row in value['reads'] if row.get('truncated')],
                message='原作者文件与读取证明已保留；未读或截断文件明确列出，不消耗方案轮次。')

    def _input(self,identifier):
        value=self.current.get()
        if value is None or value['task_id']!=identifier:raise TaskError('作者来源只在当前用户准备动作内装配。')
        self._validate_metadata_reads()
        binding,paper,receipt,scope=self._metadata(identifier)
        if (scope['id']!=value['scope_id'] or sha256(canonical(receipt))!=value['receipt_document_sha256']):
            raise TaskError('准备期间原目标或固定源码绑定改变。')
        validation=self.scopes.get_author_validation(identifier)
        if (validation['id'] if validation else None)!=value['input'].get('author_validation_id'):
            raise TaskError('准备期间显式作者验证子集改变；未发送旧范围。')
        return deepcopy(value['input'])

    def status(self,identifier):
        result=self.planner.status(identifier)
        if self.model_calls is not None:
            for item in result['history']:
                _,context,_,_=self.planner._stored(identifier,item['request_id'])
                if sha256(str(self.model_calls.path.resolve()).encode())!=context['model_binding']['model_ledger_sha256']:
                    continue
                call=self.model_calls.lookup(item['request_id']) or {};receipt=call.get('receipt') or {}
                if (receipt.get('state')=='completed' and call.get('request_sha256')==context['request_sha256']
                        and receipt.get('request_sha256')==context['request_sha256']
                        and receipt.get('output_sha256')==sha256(canonical(receipt.get('structured_output')))):
                    item['proposal']=deepcopy(receipt['structured_output']);item['model_usage']=receipt.get('usage')
        ready=False;reason=''
        try:
            binding,paper,receipt,scope=self._metadata(identifier)
            model=self.model_provider(identifier)
            if not model['enabled']:raise TaskError('请先连接本项目运行模型。')
            ready=True
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            reason=str(error) if isinstance(error,TaskError) else '作者来源或固定部署暂未通过核验。'
        result.update(configured=identifier in self.bindings or ready,enabled=ready,
            can_prepare=ready and result['remaining_rounds']>0 and not result['pending_reconciliation'],
            disabled_reason=reason,source_reads_on_GET=False,can_submit=False,can_approve=False)
        validation=self.scopes.author_validation_status(identifier)
        result['author_validation']=validation
        if validation['latest'] is not None and not validation['latest']['can_prepare_A_plan']:
            result.update(can_prepare=False,disabled_reason=validation['latest']['disabled_reason'])
        execution=self.execution_status(identifier)
        result.update(execution=execution,can_submit=execution['can_submit'],can_approve=execution['can_approve'])
        return result

    def execution_status(self,identifier):
        self.tasks.get(identifier)
        execution=self.execution_services.get(identifier,self.execution)
        if execution is None:
            inherited=bool(self.lane_deployment is not None and
                (self.bindings.get(identifier,{}).get('execution_resource_receipt') or
                 (self.lane_deployment.policy is not None and getattr(self.lane_deployment.policy,'is_first_library',lambda _:False)(identifier))))
            return dict(task_id=identifier,configured=inherited,can_prepare=inherited,can_approve=False,
                can_submit=False,latest=None,history=[],role='author_reference',scope='reference_only',B_input=False,
                scientific_status='not_evaluated',disabled_reason=('准备时直接复用原已批准作者资源包；不新增科学审核。' if inherited else '作者 A 的可信执行部署尚未登记；方案可查看，不能提交计算。'))
        return execution.status(identifier)

    def _execution(self,identifier):
        execution=self.execution_services.get(identifier,self.execution)
        if execution is None:
            raise TaskError('作者 A 执行未配置：需要现有参考账本、原件目录、固定环境与提交/回收端点。')
        return execution

    def prepare_execution(self,identifier):
        with self.resources._lease(task_id(identifier)):
            library=bool(self.lane_deployment is not None and self.lane_deployment.policy is not None
                and getattr(self.lane_deployment.policy,'is_first_library',lambda _:False)(identifier))
            if self.lane_deployment is not None and identifier not in self.execution_services and (self.execution is None or library):
                history=self.planner.history(identifier)
                if history:self.lane_deployment.ensure_resources(identifier,history[-1]['request_id'])
            return self._execution(identifier).prepare(identifier)

    def approve_execution(self,identifier,revision,package_sha256,*,note=''):
        with self.resources._lease(task_id(identifier)):
            return self._execution(identifier).approve(identifier,revision,package_sha256,note=note)

    def submit_execution(self,identifier,package_sha256):
        with self.resources._lease(task_id(identifier)):
            return self._execution(identifier).dispatch(identifier,package_sha256)

    def follow_execution(self,identifier):
        return self._execution(identifier).follow(task_id(identifier))

    def prepare(self,identifier,request,*,revision_note=''):
        with self._metadata_reads(identifier,request):
            return self._prepare(identifier,request,revision_note=revision_note)

    def _prepare(self,identifier,request,*,revision_note=''):
        task_id(identifier);task_id(request)
        revision_note=text(revision_note,4000,required=False)
        fd=os.open(self.locks/(identifier+'.lock'),os.O_CREAT|os.O_NOFOLLOW|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise TaskError('本任务正在准备作者方案；未重复读取或调用。') from None
            saved=self.planner.get(identifier,request)
            if saved and revision_note!=saved['revision_note']:
                raise TaskError('该作者方案请求已绑定另一修订说明；不能覆盖原请求。')
            if saved and saved['receipt'] is not None:return saved
            model=self.model_provider(identifier)
            if not model['enabled']:raise TaskError('请先连接本项目模型。')
            if saved is None and model['client'].calls.status()['remaining_requests']==0:
                raise TaskError('模型额度未允许新请求；未读取源码或消耗方案轮次。')
            state=self.planner.status(identifier)
            if saved is None and (state['pending_reconciliation'] or state['remaining_rounds']==0):
                raise TaskError('作者方案原请求待核对或三轮已用完；未重新读取或发送。')
            binding,paper,receipt,scope=self._metadata(identifier)
            validation=self.scopes.get_author_validation(identifier)
            if validation is not None and not validation['can_prepare_A_plan']:
                raise TaskError(validation['disabled_reason'])
            # Ordinary plan preparation already owns the approved source read.
            # Retain it before the library policy verifies its complete receipts.
            sources=self._sources(identifier,binding,receipt)
            if self.lane_deployment is not None and (binding.get('execution_resource_receipt') or
                    (self.lane_deployment.policy is not None and getattr(self.lane_deployment.policy,'is_first_library',lambda _:False)(identifier))):
                self.lane_deployment.ensure_resources(identifier,request)
                # The ordinary resource action may install/update metadata.
                # Keep its original proof baselines, but re-read subsequent
                # metadata instead of carrying a pre-write result forward.
                self._invalidate_metadata_reads()
            checked=author_source_inputs(sources,identifier)
            paths=list(binding['invocation_paths'])
            for row in checked['reads']:
                path=row.get('path','')
                if (row.get('state')=='read' and path not in paths
                        and PurePosixPath(path).suffix.lower() in {'.py','.sh','.in','.lmp','.lammps'}):
                    paths.append(path)
            value=dict(task_id=identifier,paper_id=paper['id'],paper={key:paper[key] for key in ('title','doi')},
                scope='reference_only',role='author_reference_human_only',scope_id=scope['id'],
                author_source=sources,invocation_paths=paths,
                deployment=deepcopy(binding['deployment']),missing=deepcopy(binding.get('missing',[])))
            if self.lane_deployment is not None:
                enriched=self.lane_deployment.planner_binding(identifier,binding)
                value['deployment']=deepcopy(enriched['deployment']);value['missing']=deepcopy(enriched.get('missing',[]))
            if self.reference_inputs is not None:
                retained_inputs=self.reference_inputs.planner_inputs(identifier)
                if retained_inputs:value['reference_inputs']=retained_inputs
            if validation is not None:
                value['author_validation_id']=validation['id']
            token=self.current.set(dict(task_id=identifier,scope_id=scope['id'],receipt_document_sha256=sha256(canonical(receipt)),input=value))
            try:
                self._validate_metadata_reads()
                return self.planner.prepare(identifier,request,model['client'],
                    connection_revision=model['connection_revision'],credential_generation=model['credential_generation'],revision_note=revision_note)
            finally:self.current.reset(token)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            if isinstance(error,TaskError):raise
            # Preserve bounded diagnostics privately; opaque preflight failures
            # otherwise force another blind retry without a model request.
            trace=private_directory(self.tasks.path.parent/'author-workspace-errors')
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            path=trace/(request+'-'+stamp+'.json')
            trace_fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            with os.fdopen(trace_fd,'wb') as stream:
                stream.write(canonical(dict(task_id=identifier,request_id=request,
                    exception=type(error).__name__,traceback=traceback.format_exc()[-16384:])))
            if isinstance(error,ModelError) and str(error)=='input_too_large':
                raise TaskError('完整作者来源、前稿与反馈超过模型输入容量；未截断、未发送。应用需调整重复输入后恢复同一任务。') from None
            raise TaskError('固定作者来源、读取选择或部署配置未通过核验；未自动重发，已有记录保留。') from None
        finally:os.close(fd)


def load_author_workspace(tasks,papers,scopes,path,hpc,*,model_provider,connection_provider,model_calls=None):
    """Explicit controller configuration, no scan for private assets or secrets."""
    original=read_regular(Path(path),2*1024*1024,private=True);config=json.loads(original)
    if set(config)!={'version','audit_directory','registrations'} or config['version']!=1 or not isinstance(config['registrations'],list) or len(config['registrations'])>256:
        raise TaskError('作者方案私有部署配置无效。')
    directory=Path(config['audit_directory'])
    if not directory.is_absolute():raise TaskError('作者读取审计需显式私有绝对目录。')
    bindings={}
    def artifact(spec):
        if not isinstance(spec,dict) or set(spec)!={'path','sha256'} or not Path(spec['path']).is_absolute():raise TaskError('固定参考回执配置无效。')
        raw=read_regular(Path(spec['path']),2*1024*1024,private=True)
        if sha256(raw)!=spec['sha256']:raise TaskError('固定参考回执原字节已改变。')
        return raw
    for row in config['registrations']:
        expected={'task_id','paper_id','format','policy','connection_revision','receipt','intent','license_approval','association','selections','invocation_paths','deployment','missing'}
        if set(row) not in (expected,expected|{'execution_resource_receipt'}) or row['format'] not in {'acquisition_v1','legacy_selected_resource_v3'}:raise TaskError('作者部署注册字段不完整。')
        identifier=task_id(row['task_id']);task_id(row['paper_id'])
        if identifier in bindings or row['policy']['reference_task_id']!=identifier:raise TaskError('作者任务绑定重复或不一致。')
        if sha256(artifact(row['license_approval']))!=row['policy']['license_approval_sha256']:raise TaskError('许可读取批准摘要不一致。')
        transport=SavedHPCTransport(hpc,row['connection_revision'],row['policy']['host_alias'])
        if row['format']=='legacy_selected_resource_v3':
            from .legacy_author_source_reader import LegacyAuthorSourceReader
            reader=LegacyAuthorSourceReader(directory,row['policy'],transport=transport)
        else:reader=AuthorSourceReader(directory,row['policy'],transport=transport)
        def license_check(spec=deepcopy(row['license_approval'])):artifact(spec)
        binding={key:deepcopy(row[key]) for key in ('paper_id','format','selections','invocation_paths','deployment','missing')}
        if 'execution_resource_receipt' in row:
            original=row['execution_resource_receipt']
            if not isinstance(original,dict) or set(original)!={'path','sha256','request_id'}:
                raise TaskError('作者资源库需要原执行资源回执、摘要和原记账请求。')
            task_id(original['request_id']);artifact({key:original[key] for key in ('path','sha256')})
            binding['execution_resource_receipt']=deepcopy(original)
        binding.update(reader=reader,load_receipt=lambda spec=deepcopy(row['receipt']):json.loads(artifact(spec)),
            receipt_file_sha256=row['receipt']['sha256'],load_intent=lambda spec=deepcopy(row['intent']):json.loads(artifact(spec)) if spec else None,
            load_association=lambda spec=deepcopy(row['association']):json.loads(artifact(spec)),verify_license=license_check)
        bindings[identifier]=binding
    return AuthorReferenceWorkspace(tasks,papers,scopes,bindings,model_provider=model_provider,
        connection_provider=connection_provider,model_calls=model_calls)
