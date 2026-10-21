"""First author A from pinned library bytes, using the existing reference ledger.

This module neither proposes science nor dispatches. The ordinary resource-prepare
action may register one reference identity; the existing AuthorDispatch still owns
package preparation, user approval, grants, submission, collection and accounting.
"""
from copy import deepcopy
import base64
import inspect
import json
from pathlib import Path

from .author_reference_plan import author_source_inputs, author_source_text_coverage
from .manifest import canonical, sha256
from .runtime_launcher import hash_value, read_regular
from .tasks import TaskError, task_id


def _scheduler_metadata(request):
    """Standalone metadata reader, never executes sbatch or an author program."""
    import hashlib,json,os,stat
    from pathlib import Path
    def read(path,expected=None,limit=64*1024**2):
        if not Path(path).is_absolute() or '..' in Path(path).parts:raise ValueError('invalid_scheduler_path')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_mode&0o022 or before.st_size>limit:raise ValueError('unsafe_scheduler_metadata')
            value=stream.read(limit+1);after=os.fstat(stream.fileno())
        if len(value)>limit or any(getattr(before,k)!=getattr(after,k) for k in ('st_ino','st_size','st_mtime_ns','st_ctime_ns')):raise ValueError('changed_scheduler_metadata')
        digest=hashlib.sha256(value).hexdigest()
        if expected is not None and digest!=expected:raise ValueError('scheduler_source_changed')
        return value,digest
    if set(request)!={'helper_path','helper_sha256','runtime_path','runtime_sha256'}:raise ValueError('invalid_scheduler_metadata_request')
    read(request['helper_path'],request['helper_sha256'],1000000)
    raw,config_sha=read(str(Path(request['helper_path']).parent/'submission.json'),limit=16384)
    config=json.loads(raw)
    if config['runtime_path']!=request['runtime_path'] or config['runtime_sha256']!=request['runtime_sha256']:raise ValueError('scheduler_runtime_changed')
    read(config['sbatch_path'],config['sbatch_sha256'])
    return dict(sbatch_path=config['sbatch_path'],sbatch_sha256=config['sbatch_sha256'],original_config_sha256=config_sha)


SCHEDULER_CODE=inspect.getsource(_scheduler_metadata)+'''\nimport base64,json,sys\nprint(base64.b64encode(json.dumps(_scheduler_metadata(json.loads(sys.stdin.buffer.read(16385))),sort_keys=True,separators=(',',':')).encode()).decode())\n'''


class LibraryAuthorResourcePolicy:
    def __init__(self,lane,*,campaign,system_sha256):
        self.lane,self.workspace,self.tasks=lane,lane.workspace,lane.tasks
        self.campaign=campaign;self.system_sha256=hash_value(system_sha256)

    def campaign_policy(self):
        # Only a startup-owned existing campaign; never create or amend policy.
        with self.lane.controller.ledger._transaction() as db:
            return self.lane.controller.ledger._policy(db,self.campaign)

    def prepare_environment(self,identifier):
        """Read the existing fixed submit configuration during ordinary prepare."""
        controller=self.lane.controller
        endpoint=controller.submission.scheduler.endpoint
        runtime_endpoint=controller.following.analysis.collector.endpoint
        request=dict(helper_path=endpoint.helper_path,helper_sha256=endpoint.helper_sha256,
                     runtime_path=runtime_endpoint.helper_path,runtime_sha256=runtime_endpoint.helper_sha256)
        binding=dict(task_id=task_id(identifier),request=request,worker_sha256=sha256(SCHEDULER_CODE.encode()))
        path=self.lane.audit/('library-scheduler-'+sha256(canonical(binding))+'.json')
        if path.exists():
            saved=json.loads(read_regular(path,65536,private=True))
            if saved['binding']!=binding:raise TaskError('首次作者提交器核验绑定变化。')
            self.lane.submission_environment=saved['report'];return
        transport=controller.staging.client.transport
        result=self.lane.capture(transport.command(endpoint.host_alias,[controller.environment.python_path,'-I','-S','-c',SCHEDULER_CODE]),
            timeout=30,max_bytes=65536,input_chunks=[canonical(request)])
        if result.get('returncode')!=0 or result.get('failure'):raise TaskError('首次作者已有提交器元数据未核实；未运行引擎或提交。')
        try:
            wire=base64.b64decode(result['stdout'],validate=True).strip()
            report=json.loads(base64.b64decode(wire,validate=True))
            if set(report)!={'sbatch_path','sbatch_sha256','original_config_sha256'}:raise ValueError()
            for key in ('sbatch_sha256','original_config_sha256'):hash_value(report[key])
            if not Path(report['sbatch_path']).is_absolute():raise ValueError()
        except (ValueError,KeyError,TypeError):raise TaskError('首次作者提交器返回未通过身份核对。') from None
        from .remote_submit import private_bytes_once
        private_bytes_once(path,canonical(dict(binding=binding,report=report)))
        self.lane.submission_environment=report

    def _source(self,identifier):
        task_id(identifier)
        source,paper,receipt,scope=self.workspace._metadata(identifier)
        if scope.get('state')!='recorded' or scope.get('task_id')!=identifier:
            raise TaskError('首次作者 A 需要本任务已批准且来源有效的 P 范围。')
        if source.get('execution_resource_receipt'):
            raise TaskError('已有作者运行须保留原继承分支，不能登记成首次运行。')
        policy=getattr(source['reader'],'policy',None)
        if not isinstance(policy,dict) or policy.get('license_approved') is not True:
            raise TaskError('首次作者 A 的资源库许可尚未批准。')
        for key in ('license_approval_sha256','receipt_sha256'):
            hash_value(policy.get(key))
        if policy['receipt_sha256']!=sha256(canonical(receipt)):
            raise TaskError('资源库原获取回执与当前读取能力不一致。')
        hash_value(receipt.get('inventory_sha256'));task_id(receipt.get('id'))
        retained=self.workspace._cached_sources(identifier,source,receipt)
        checked=author_source_inputs(retained,identifier)
        if not checked['files'] or not checked['sources']:
            raise TaskError('资源库原件尚未固定并实际读取。')
        known={row['path']:row for row in checked['files']}
        # Initial selected bundles have no guessed invocation list. Complete
        # retained text is the capability menu; AI still cites the original
        # instructions to select the actual entry and argument bytes.
        paths=source.get('invocation_paths',[])
        if not paths:
            paths=[row['path'] for row in author_source_text_coverage(checked)['complete_files']]
        if not paths or any(path not in known for path in paths):
            raise TaskError('作者调用说明路径尚未绑定实际资源 SHA。')
        return dict(source,invocation_paths=paths),paper,scope,dict(policy,acquisition_id=receipt.get('acquisition_id',receipt['id']),
            resource_receipt_id=receipt['id'],
            inventory_sha256=receipt['inventory_sha256']),checked

    def reference_evaluation(self,identifier,*,create=False):
        source,paper,scope,policy,checked=self._source(identifier)
        identity=sha256(canonical(dict(kind='library_author_reference_v1',task_id=identifier,
            paper_id=paper['id'],scope_id=scope['id'],scope_sha256=scope['scope_sha256'])))
        with self.tasks.transaction() as db:
            candidates=[row[0] for row in db.execute('SELECT evaluation FROM paper_evaluations WHERE task_id=?',(identifier,))]
        ledger=self.lane.controller.ledger
        existing=[]
        for key in candidates:
            value=ledger.evaluation_snapshot(key)
            if value['identity']['role']=='reference':existing.append(value)
        if existing:
            if len(existing)!=1 or existing[0]['identity']['task']!=identity or existing[0]['identity']['system']!=self.system_sha256:
                raise TaskError('首次作者身份已有关联，不能换范围或重置历史。')
            return existing[0]['id']
        if not create:raise TaskError('请先通过普通准备动作登记首次作者参考身份。')
        self.campaign_policy()
        evaluation=ledger.register_evaluation(self.campaign,task_sha256=identity,repetition=0,
            role='reference',system_sha256=self.system_sha256)
        self.workspace.papers.bind_reference_evaluation(paper['id'],identifier,evaluation)
        return evaluation

    def approved(self,identifier):
        source,paper,scope,source_policy,checked=self._source(identifier)
        policy=self.campaign_policy()
        profile=json.loads(read_regular(self.lane.controller.runtime_profile_path,1000000,private=True))
        native=profile['native_slurm']
        environment=self.workspace.environment_inventory.status(identifier)['latest']
        if not environment or environment['state']!='metadata_ready':
            raise TaskError('首次作者 A 需要已有引擎与解释器的真实元数据核验。')
        report=environment['report']
        verified={row['path']:row['sha256'] for row in report['native_files'] if row['status']=='verified'}
        if verified!=native['files']:raise TaskError('资源库作者引擎与当前核验字节不一致。')
        python=[row for row in report['python_candidates'] if row['status']=='verified' and
                (row['path']==self.lane.controller.environment.python_path or row['real_path']==self.lane.controller.environment.python_path)]
        if len(python)!=1:raise TaskError('首次作者运行器缺少唯一已核验解释器。')
        complete={row['path'] for row in author_source_text_coverage(checked)['complete_files']}
        entries=[{key:row[key] for key in ('path','sha256')} for row in checked['files'] if row['path'] in complete]
        if not entries:raise TaskError('作者入口需要完整原件读取证明；不能猜测入口或摘要。')
        programs=[]
        for menu in source['deployment']['environments']:
            engines=[dict(id=program['id'],sha256=program['sha256'],kind='lammps',path=native['engine_path'],
                launch_mode='mpi',entrypoints=deepcopy(entries),child_process_policy='no_children')
                for program in menu['executables'] if program['kind']=='lammps' and
                program['sha256']==native['files'][native['engine_path']]]
            if engines:programs.append(dict(id=menu['id'],label=menu['label'],sha256=menu['sha256'],executables=engines))
        if not programs:raise TaskError('资源库尚无匹配当前已核验 CPU 引擎的作者调用能力。')
        endpoint=self.lane.controller.submission.scheduler.endpoint
        if not getattr(endpoint,'helper_path',None) or not getattr(endpoint,'helper_sha256',None):
            raise TaskError('原项目固定提交工具未配置。')
        # sbatch identity is already pinned in the actual environment metadata.
        scheduler=report.get('submission_environment')
        if scheduler is None:
            scheduler=getattr(self.lane,'submission_environment',None)
        if not isinstance(scheduler,dict):
            raise TaskError('首次作者资源缺少已核验 sbatch 路径与 SHA 元数据。')
        hash_value(scheduler.get('sbatch_sha256'))
        key=read_regular(Path(self.lane.controller.authorization.directory)/'grant.key',32,private=True)
        if len(key)!=32:raise TaskError('既有项目签名材料不完整。')
        proof=dict(kind='fixed_library_author_resource_bundle',task_id=identifier,paper_id=paper['id'],
            scope_sha256=scope['scope_sha256'],scope_receipt_sha256=scope['receipt_sha256'],
            acquisition_id=source_policy['acquisition_id'],acquisition_receipt_sha256=source_policy['receipt_sha256'],
            resource_receipt_id=source_policy['resource_receipt_id'],
            inventory_sha256=source_policy['inventory_sha256'],license_approval_sha256=source_policy['license_approval_sha256'],
            author_files_sha256=sha256(canonical(checked['files'])),source_capability_sha256=sha256(canonical(checked['capability'])),
            invocation_paths=[dict(path=path,sha256=next(row['sha256'] for row in checked['files'] if row['path']==path))
                              for path in source['invocation_paths']],
            environment_report_sha256=environment['report_sha256'],campaign=self.campaign,
            campaign_approval_sha256=policy['approval_sha256'],accounted_inputs=[],
            new_scientific_review=False,new_user_gate=False,execution_authorized=False,
            scientific_validation=False,child_process_policy='no_children')
        candidate=dict(programs=programs,python=python[0],child_scripts=[],file_modes=[],accounted_inputs=[],
            submission_environment={key:scheduler[key] for key in ('sbatch_path','sbatch_sha256')},
            existing_signing_key_sha256=sha256(key))
        return dict(candidate=candidate,candidate_sha256=sha256(canonical(candidate)),
                    review_receipt=proof,review_sha256=sha256(canonical(proof)))
