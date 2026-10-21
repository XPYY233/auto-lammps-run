"""Inherit unchanged author-resource capabilities from an accounted A run.

There is no new scientific review or approval queue. The resource library pins
the old execution receipt; unchanged original bytes/modes inherit its approved
campaign evidence. New/changed files do not acquire that execution capability.
"""
import ast
from copy import deepcopy
import json
from pathlib import Path
import sys

from .author_reference_plan import author_source_inputs
from .author_program_metadata import runtime_python_candidates
from .manifest import canonical,sha256
from .runtime_launcher import hash_value,read_regular,relative
from .tasks import TaskError


class InheritedAuthorResourcePolicy:
    def __init__(self,lane,reference_inputs,*,resource_receipt_provider,library_policy=None):
        self.lane,self.workspace,self.tasks,self.inputs=lane,lane.workspace,lane.tasks,reference_inputs
        if not callable(resource_receipt_provider):raise TaskError('作者资源库须提供原已批准运行的固定资源回执。')
        self.provider=resource_receipt_provider
        self.library=library_policy
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS author_resource_inheritance (sha256 TEXT PRIMARY KEY,task_id TEXT NOT NULL,document TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_author_resource_inheritance_{action} BEFORE {action} ON author_resource_inheritance BEGIN SELECT RAISE(ABORT,"immutable author resource inheritance"); END')

    def is_first_library(self,identifier):
        if self.library is None:return False
        try:source,_,_,_=self.workspace._metadata(identifier)
        except (TaskError,KeyError,TypeError,OSError):return False
        return not bool(source.get('execution_resource_receipt'))

    def approved(self,identifier):
        if self.is_first_library(identifier):return self.library.approved(identifier)
        spec=self.provider(identifier)
        if (not isinstance(spec,dict) or set(spec)!={'path','sha256','request_id'} or not Path(spec['path']).is_absolute()):
            raise TaskError('作者资源库尚未绑定原运行资源回执；不接受浏览器路径。')
        raw=read_regular(spec['path'],1024**2,private=True)
        if sha256(raw)!=spec['sha256']:raise TaskError('原已批准作者资源回执摘要已改变。')
        resource=json.loads(raw);hash_value(resource['manifest_sha256']);hash_value(resource['batch_sha256'])
        inputs=self.inputs.planner_inputs(identifier)
        matches=[row for row in inputs if row['request_id']==spec['request_id']]
        if len(matches)!=1 or matches[0]['root']!=resource['directory']:
            raise TaskError('原资源包与已记账作者输入不属于同一运行目录和请求。')
        original=self.lane.controller.ledger.get(spec['request_id'])
        evaluation=self.lane.controller.ledger.evaluation_snapshot(original['evaluation'])
        if not original['accounted'] or evaluation['identity']['role']!='reference':
            raise TaskError('作者资源能力只继承原已批准且已记账的参考作业。')
        with self.lane.controller.ledger._transaction() as db:
            row=db.execute('SELECT campaign FROM evaluations WHERE id=?',(original['evaluation'],)).fetchone()
            campaign=self.lane.controller.ledger._policy(db,row['campaign'])
        hash_value(campaign['approval_sha256'])
        source,paper,receipt,scope=self.workspace._metadata(identifier)
        retained=self.workspace._cached_sources(identifier,source,receipt);checked=author_source_inputs(retained,identifier)
        known={row['path']:row for row in checked['files']};old={};modes=[]
        for row in resource['originals']:
            relative(row['path'])
            if row['path'] in old or row['content_line_diff'] or row['run_sha256']!=row['source_sha256'] or row['run_mode'] not in {'0o400','0o500'}:
                raise TaskError('原资源回执含改写、重复或未知执行位，不能自动继承。')
            hash_value(row['run_sha256']);old[row['path']]=row
            if row['path'] in known and known[row['path']]['sha256']!=row['run_sha256']:
                raise TaskError('作者原件与既有已批准运行字节变化；只对变化项停止继承。')
            if row['run_mode']=='0o500' and row['path'] in known:
                modes.append(dict(path=row['path'],sha256=row['run_sha256'],mode=0o500))
        history=self.workspace.planner.history(identifier)
        plans=[row['report'] for row in history if row.get('report')]
        if not plans:raise TaskError('应用尚无已保留的原作者调用，不能新设计 A。')
        entries=[]
        for plan in plans:
            for call in plan['invocations']:
                entry=call['entrypoint']
                if (call['executable']['kind']=='lammps' and entry['path'] in old
                        and old[entry['path']]['run_sha256']==entry['sha256'] and entry not in entries):
                    entries.append(deepcopy(entry))
        entries.sort(key=lambda row:row['path'])
        if not entries:raise TaskError('原资源库没有与应用原调用匹配的既有引擎入口。')
        environment=self.workspace.environment_inventory.status(identifier)['latest']
        binaries,_=runtime_python_candidates(self.workspace.environment_inventory,identifier,environment)
        python=[row for row in binaries if
            (row['path']==self.lane.controller.environment.python_path or row['real_path']==self.lane.controller.environment.python_path)]
        if len(python)!=1:raise TaskError('原作者运行器缺少唯一已核验的既有解释器。')
        # Execution modes are copied from the old run receipt, never inferred
        # from a filename. Only unchanged retained stdlib child utilities inherit.
        sources={row['id']:row for row in checked['sources']};children=[]
        for mode in modes:
            proofs=[row for row in checked['reads'] if row.get('path')==mode['path'] and row.get('state')=='read'
                    and row.get('start_line')==1 and row.get('truncated') is False and row.get('source_id') in sources]
            if not proofs:raise TaskError('既有可执行原件的完整读取证明缺失：'+mode['path'])
            text=sources[proofs[-1]['source_id']]['text']
            if text.startswith('#!') and 'python' in text.splitlines()[0]:
                tree=ast.parse(text);imports={alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names}
                imports|={node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node,ast.ImportFrom) and node.module}
                if imports-set(sys.stdlib_module_names):raise TaskError('既有子程序含尚未核验的额外依赖：'+mode['path'])
                children.append(mode['path'])
        profile=json.loads(read_regular(self.lane.controller.runtime_profile_path,1000000,private=True));native=profile['native_slurm']
        programs=[]
        for menu in source['deployment']['environments']:
            registered=[]
            for program in menu['executables']:
                if program['kind']!='lammps' or program['sha256']!=native['files'][native['engine_path']]:continue
                registered.append(dict(id=program['id'],sha256=program['sha256'],kind='lammps',path=native['engine_path'],
                    launch_mode='mpi',entrypoints=entries,child_process_policy='reviewed_same_allocation'))
            if registered:programs.append(dict(id=menu['id'],label=menu['label'],sha256=menu['sha256'],executables=registered))
        latest=self.inputs.status(identifier)['latest'];scheduler=(latest.get('report') or {}).get('submission_environment') if latest else None
        if scheduler is None:raise TaskError('原固定提交工具元数据尚未核验。')
        key=read_regular(Path(self.lane.controller.authorization.directory)/'grant.key',32,private=True)
        if len(key)!=32:raise TaskError('原签名服务材料不完整。')
        validation=self.workspace.scopes.get_author_validation(identifier)
        proof=dict(kind='inherited_accounted_author_resource_bundle',task_id=identifier,
            original_request_id=original['id'],original_job_id=original['job_id'],original_evaluation=original['evaluation'],
            original_request_manifest_sha256=original['manifest_sha256'],resource_receipt_sha256=spec['sha256'],
            original_package_manifest_sha256=resource['manifest_sha256'],original_batch_sha256=resource['batch_sha256'],
            campaign_approval_sha256=campaign['approval_sha256'],original_file_modes=modes,
            original_sources_unchanged=True,new_scientific_review=False,new_user_gate=False,
            current_environment_report_sha256=environment['report_sha256'],
            interpreter_identity_origin='current_existing_deployment_metadata',
            author_validation_sha256=validation['validation_sha256'] if validation else None)
        review_sha=sha256(canonical(proof))
        with self.tasks.transaction() as db:
            db.execute('INSERT OR IGNORE INTO author_resource_inheritance VALUES(?,?,?)',(review_sha,identifier,canonical(proof).decode()))
        candidate=dict(programs=programs,python=python[0],child_scripts=children,file_modes=modes,
            accounted_inputs=inputs,submission_environment=scheduler,existing_signing_key_sha256=sha256(key))
        return dict(candidate=candidate,candidate_sha256=sha256(canonical(candidate)),review_receipt=proof,review_sha256=review_sha)
