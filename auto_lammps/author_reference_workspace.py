"""Ordinary reference plans and an explicitly configured accounted A controller.

Only private deployment bindings supply source roots, license decisions and
fixed executables. Browser inputs select a request and a revision note. Source
quotes are retained as reference evidence, not installed as a local source tree.
"""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath

from .author_reference_plan import AuthorReferencePlan, author_source_inputs, _deployment
from .author_source_reader import AuthorSourceReader
from .hpc_transport import SavedHPCTransport
from .manifest import canonical, private_directory, sha256
from .runtime_launcher import ExecutionDenied, read_regular
from .tasks import TaskError, task_id, text


class AuthorReferenceWorkspace:
    def __init__(self,tasks,papers,scopes,bindings,*,model_provider,connection_provider,model_calls=None):
        self.tasks,self.papers,self.scopes=tasks,papers,scopes
        self.bindings=bindings;self.model_provider=model_provider
        self.execution=None
        self.model_calls=model_calls
        self.current=ContextVar('author_reference_current',default=None)
        self.locks=private_directory(tasks.path.parent/'author-workspace-locks')
        self.planner=AuthorReferencePlan(tasks,papers,scopes,self._input,connection_provider=connection_provider)
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_source_evidence (id TEXT PRIMARY KEY,task_id TEXT NOT NULL,document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_author_source_evidence_{action} BEFORE {action} ON author_source_evidence BEGIN SELECT RAISE(ABORT,"immutable author source evidence"); END')
        from .author_reference_resources import AuthorReferenceResources
        self.resources=AuthorReferenceResources(self)

    def _metadata(self,identifier,*,require_scope=True):
        task=self.tasks.get(identifier)
        if task['mode']!='reproduction':raise TaskError('作者运行方案只属于论文参考任务。')
        binding=self.bindings.get(identifier)
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
        return binding,paper,receipt,scope

    def _source(self,identifier,binding,receipt):
        reader=binding['reader'];legacy=binding['format']=='legacy_selected_resource_v3'
        intent=binding['load_intent']() if legacy else None
        capability=(reader.capability(receipt,intent,purpose='author_reference') if legacy
                    else reader.capability(receipt,purpose='author_reference'))
        selections=binding['selections']
        if not isinstance(selections,list) or not 1<=len(selections)<=128:
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
        author_source_inputs(value,identifier)
        data=canonical(value)
        with self.tasks.transaction() as db:
            used=db.execute('SELECT count(*) FROM author_source_evidence WHERE task_id=?',(identifier,)).fetchone()[0]
            if used>=32:raise TaskError('参考源码证据版本容量已用完；已有版本和费用保留。')
            db.execute('INSERT INTO author_source_evidence VALUES(?,?,?,?,?)',(key,identifier,data.decode(),sha256(data),datetime.now(timezone.utc).isoformat()))
        return value

    def _sources(self,identifier,binding,receipt):
        """Retain the original package and append separately pinned new text.

        File-type selection is a bounded reading tool, not a scientific scope
        decision. Unread or truncated files remain explicit in the returned
        inventory and are never interpreted as fully inspected source.
        """
        original=self._source(identifier,binding,receipt)
        completed=self.resources.completed(identifier) if isinstance(getattr(binding['reader'],'transport',None),SavedHPCTransport) else None
        if completed is None:return original
        report,context=completed
        previous={row['path'] for row in receipt['files']}
        text_extensions={'.py','.sh','.in','.lmp','.lammps','.md','.txt','.json','.yaml','.yml','.toml','.cfg'}
        selections=[dict(path=row['path'],start_line=1,end_line=400) for row in report['files']
            if row['path'] not in previous and row['size']<=256*1024
            and (PurePosixPath(row['path']).suffix.lower() in text_extensions
                 or PurePosixPath(row['path']).name.lower() in {'readme','license','makefile'})]
        if not selections:return original
        policy=dict(host_alias=context['host_alias'],remote_directory=context['remote_directory'],
            reference_task_id=identifier,repository=context['repository'],commit=context['commit'],
            acquisition_id=report['id'],receipt_sha256=sha256(canonical(report)),
            inventory_sha256=report['inventory_sha256'],license_approved=True,
            license_approval_sha256=context['license_approval_sha256'])
        reader=AuthorSourceReader(self.resources.audit/'source-reads',policy,transport=binding['reader'].transport)
        supplement=self._source(identifier,dict(reader=reader,format='acquisition_v1',selections=selections),report)
        combined=dict(source_packages=[original,supplement])
        author_source_inputs(combined,identifier)
        return combined

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
        binding,paper,receipt,scope=self._metadata(identifier)
        if (scope['id']!=value['scope_id'] or sha256(canonical(receipt))!=value['receipt_document_sha256']):
            raise TaskError('准备期间原目标或固定源码绑定改变。')
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
        result.update(configured=identifier in self.bindings,enabled=ready,
            can_prepare=ready and result['remaining_rounds']>0 and not result['pending_reconciliation'],
            disabled_reason=reason,source_reads_on_GET=False,can_submit=False,can_approve=False)
        execution=self.execution_status(identifier)
        result.update(execution=execution,can_submit=execution['can_submit'],can_approve=execution['can_approve'])
        return result

    def execution_status(self,identifier):
        self.tasks.get(identifier)
        if self.execution is None:
            return dict(task_id=identifier,configured=False,can_prepare=False,can_approve=False,
                can_submit=False,latest=None,history=[],role='author_reference',scope='reference_only',B_input=False,
                scientific_status='not_evaluated',disabled_reason='作者 A 的可信执行部署尚未登记；方案可查看，不能提交计算。')
        return self.execution.status(identifier)

    def _execution(self):
        if self.execution is None:
            raise TaskError('作者 A 执行未配置：需要现有参考账本、原件目录、固定环境与提交/回收端点。')
        return self.execution

    def prepare_execution(self,identifier):
        with self.resources._lease(task_id(identifier)):
            return self._execution().prepare(identifier)

    def approve_execution(self,identifier,revision,package_sha256,*,note=''):
        with self.resources._lease(task_id(identifier)):
            return self._execution().approve(identifier,revision,package_sha256,note=note)

    def submit_execution(self,identifier,package_sha256):
        with self.resources._lease(task_id(identifier)):
            return self._execution().dispatch(identifier,package_sha256)

    def follow_execution(self,identifier):
        return self._execution().follow(task_id(identifier))

    def prepare(self,identifier,request,*,revision_note=''):
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
            sources=self._sources(identifier,binding,receipt)
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
            token=self.current.set(dict(task_id=identifier,scope_id=scope['id'],receipt_document_sha256=sha256(canonical(receipt)),input=value))
            try:return self.planner.prepare(identifier,request,model['client'],
                connection_revision=model['connection_revision'],credential_generation=model['credential_generation'],revision_note=revision_note)
            finally:self.current.reset(token)
        except (ValueError,KeyError,TypeError,OSError,ExecutionDenied) as error:
            if isinstance(error,TaskError):raise
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
        if set(row)!=expected or row['format'] not in {'acquisition_v1','legacy_selected_resource_v3'}:raise TaskError('作者部署注册字段不完整。')
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
        binding.update(reader=reader,load_receipt=lambda spec=deepcopy(row['receipt']):json.loads(artifact(spec)),
            receipt_file_sha256=row['receipt']['sha256'],load_intent=lambda spec=deepcopy(row['intent']):json.loads(artifact(spec)) if spec else None,
            load_association=lambda spec=deepcopy(row['association']):json.loads(artifact(spec)),verify_license=license_check)
        bindings[identifier]=binding
    return AuthorReferenceWorkspace(tasks,papers,scopes,bindings,model_provider=model_provider,
        connection_provider=connection_provider,model_calls=model_calls)
