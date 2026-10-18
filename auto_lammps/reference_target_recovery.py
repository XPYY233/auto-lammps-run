"""One explicit, supplier-checked replay of a pinned reference-only request.

The scientific controller module stays byte-identical. Its original Adapter,
messages, validators and model client remain authoritative; this layer adds only
append-only user authorization and same-task recovery accounting.
"""
from copy import deepcopy
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path

from .deepseek import DeepSeekConfig,request_body
from .manifest import canonical, sha256
from .reference_targets import (ReferenceTargetAnalysis as OriginalReferenceTargetAnalysis,
    ReferenceTargetUnavailable, reference_target_input, _FLAGS, _MODEL_RECEIPT_FIELDS, STAGE,
    model_ledger_identity, VERSION)
from .scientific_adapters import validate_prepared_messages
from .tasks import TaskError, task_id


class ReferenceTargetAnalysis(OriginalReferenceTargetAnalysis):
    def __init__(self,*args,historical_reference_provider=None,**kwargs):
        if historical_reference_provider is not None and not callable(historical_reference_provider):
            raise TaskError('历史参考来源核验需受信的只读服务。')
        super().__init__(*args,**kwargs)
        self.historical_reference_provider=historical_reference_provider
        self.original_revision_context=ContextVar('reference_target_original_revision',default=None)
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reference_target_unknown_reissues ('
                'id TEXT PRIMARY KEY REFERENCES reference_target_intents(id), '
                'unknown_reissue_of TEXT NOT NULL UNIQUE REFERENCES reference_target_intents(id), '
                'task_id TEXT NOT NULL REFERENCES reference_target_scopes(task_id), '
                'document TEXT NOT NULL,document_sha256 TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_reference_target_unknown_reissues_{action} '
                    f'BEFORE {action} ON reference_target_unknown_reissues BEGIN '
                    "SELECT RAISE(ABORT,'immutable reference target reissue'); END")
            db.execute('CREATE TABLE IF NOT EXISTS reference_target_original_revisions ('
                'id TEXT PRIMARY KEY REFERENCES reference_target_intents(id),'
                'parent_request TEXT NOT NULL UNIQUE REFERENCES reference_target_intents(id),'
                'task_id TEXT NOT NULL REFERENCES reference_target_scopes(task_id),'
                'document TEXT NOT NULL,document_sha256 TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_reference_target_original_revisions_{action} '
                    f'BEFORE {action} ON reference_target_original_revisions BEGIN '
                    "SELECT RAISE(ABORT,'immutable original-source target revision'); END")

    def _input(self,identifier,*,active=True):
        original=self.original_revision_context.get()
        if original is not None and original['task_id']==identifier:
            return self._historical_input(identifier,original,active=active)
        return super()._input(identifier,active=active)

    def _generation_context(self,identifier,request_id,client,connection_revision,credential_generation):
        reissued=self._authorized_reissue_context(identifier,request_id,client,connection_revision,credential_generation)
        if reissued is not None:return reissued
        revision=self._original_revision(identifier,request_id=request_id)
        if revision is not None:
            return self._original_revision_generation(identifier,request_id,revision,client,
                connection_revision,credential_generation)
        return super()._generation_context(identifier,request_id,client,connection_revision,credential_generation)

    def _original_revision(self,identifier,*,request_id=None,parent_request=None):
        with self.tasks.transaction() as db:
            self._task(db,identifier)
            row=db.execute('SELECT * FROM reference_target_original_revisions WHERE '+
                ('id=?' if request_id is not None else 'parent_request=?'),
                (task_id(request_id or parent_request),)).fetchone()
        if row is None:return None
        value=json.loads(row['document'])
        if (row['task_id']!=identifier or value['task_id']!=identifier or value['request_id']!=row['id']
                or value['parent_request']!=row['parent_request']
                or sha256(canonical(value))!=row['document_sha256']):
            raise TaskError('原来源修订关系摘要或本任务身份不一致。')
        return value

    def _original_revision_proof(self,identifier,parent_request,client,connection_revision,credential_generation):
        saved=self._stored(identifier,parent_request)
        if saved is None or saved[2] is None or saved[2]['state'] not in {'invalid_output','unavailable'}:
            raise TaskError('只允许基于原来源已知完整失败答复修订；未知请求不能修订或重发。')
        row,parent,receipt,_events=saved
        reissued=self._unknown_reissue(identifier,request_id=parent_request)
        revised=self._original_revision(identifier,request_id=parent_request)
        if not ((reissued and parent.get('unknown_reissue')) or (revised and parent.get('original_source_revision'))):
            raise TaskError('本入口只修订已保留原来源的恢复答复，不能借用其他来源或任务。')
        self._historical_input(identifier,parent,active=True)
        model=self._model(identifier,client,connection_revision,credential_generation)
        if model!=parent['model_binding']:
            raise TaskError('原来源修订的模型、授权、配置或凭据版本已变化。')
        call=client.calls.lookup(parent_request) or {};model_receipt=call.get('receipt') or {}
        if (call.get('request_sha256')!=parent['request_sha256']
                or model_receipt.get('request_sha256')!=parent['request_sha256']
                or model_receipt.get('state')!='completed' or model_receipt.get('http_status')!=200
                or model_receipt.get('finish_reason')!='stop' or model_receipt.get('transport_attempts')!=1
                or model_receipt.get('provider')!='deepseek' or model_receipt.get('requested_model')!=client.model
                or model_receipt.get('output_sha256')!=sha256(canonical(model_receipt.get('structured_output')))
                or {k:v for k,v in model_receipt.items() if k in _MODEL_RECEIPT_FIELDS}!=receipt['model_receipt']):
            raise TaskError('原完整模型答复、用量或失败回执未通过核验；未修订或重发。')
        validate_prepared_messages(STAGE,parent['messages'],parent['input'])
        return dict(parent=parent,parent_context_sha256=row['document_sha256'],
            parent_receipt_sha256=row['receipt_sha256'],parent_model_call_sha256=sha256(canonical(call)),
            original_input_sha256=sha256(canonical(parent['input'])),
            source_output_sha256=model_receipt['output_sha256'])

    def _original_revision_generation(self,identifier,request_id,relation,client,connection_revision,credential_generation):
        proof=self._original_revision_proof(identifier,relation['parent_request'],client,
            connection_revision,credential_generation)
        fixed={key:proof[key] for key in ('parent_context_sha256','parent_receipt_sha256',
            'parent_model_call_sha256','original_input_sha256','source_output_sha256')}
        if any(relation.get(key)!=value for key,value in fixed.items()):
            raise TaskError('原来源修订的父答复或原文绑定已变化；未重建方案。')
        token=self.original_revision_context.set(proof['parent'])
        try:
            context=super()._generation_context(identifier,request_id,client,connection_revision,credential_generation)
        finally:self.original_revision_context.reset(token)
        if (context['input']!=proof['parent']['input']
                or context.get('revision_parent',{}).get('parent_request')!=relation['parent_request']
                or context['revision_parent']['source_output_sha256']!=proof['source_output_sha256']):
            raise TaskError('修订必须包含声明父稿的完整答复和原始来源，不能推倒重写。')
        return {**context,'original_source_revision':dict(parent_request=relation['parent_request'],
            relation_sha256=sha256(canonical(relation)))}

    def revise_original(self,identifier,request_id,client,*,parent_request,connection_revision,credential_generation):
        task_id(identifier);task_id(request_id);task_id(parent_request)
        if request_id==parent_request:raise TaskError('修订需新版本编号；原稿与费用不能覆盖。')
        with self._lease(identifier) as acquired:
            if not acquired:
                prior=self._original_revision(identifier,parent_request=parent_request)
                if prior and prior['request_id']==request_id:return self.get(identifier,request_id)
                raise TaskError('本任务正在核定或修订；未重复发送。')
            prior=self._original_revision(identifier,parent_request=parent_request)
            if prior:
                if prior['request_id']!=request_id:raise TaskError('这份父稿已有后继修订，不能重复另建版本。')
            else:
                proof=self._original_revision_proof(identifier,parent_request,client,connection_revision,credential_generation)
                relation=dict(version=1,task_id=identifier,request_id=request_id,parent_request=parent_request,
                    operation='revise_complete_original_source_answer',original_source_preserved=True,
                    automatic_retry=False,at=datetime.now(timezone.utc).isoformat(),
                    **{key:proof[key] for key in ('parent_context_sha256','parent_receipt_sha256',
                        'parent_model_call_sha256','original_input_sha256','source_output_sha256')})
                context=self._original_revision_generation(identifier,request_id,relation,client,
                    connection_revision,credential_generation)
                with self.tasks.transaction() as db:
                    self._task(db,identifier,active=True)
                    if self._pending(db,identifier):raise TaskError('本任务另有未知或待定请求；未修订。')
                    if db.execute('SELECT 1 FROM reference_target_intents WHERE id=?',(request_id,)).fetchone():
                        raise TaskError('修订编号已有其他意图；不能覆盖。')
                    limit=db.execute('SELECT max_suggestions FROM reference_target_scopes WHERE task_id=?',(identifier,)).fetchone()[0]
                    count=db.execute('SELECT count(*) FROM reference_target_intents WHERE task_id=?',(identifier,)).fetchone()[0]
                    if count>=limit:raise TaskError('原任务最多三次目标建议已用完；未增加第四轮。')
                    db.execute('INSERT INTO reference_target_intents VALUES (?,?,?,?,?)',(request_id,identifier,
                        canonical(context).decode(),sha256(canonical(context)),relation['at']))
                    db.execute('INSERT INTO reference_target_original_revisions VALUES (?,?,?,?,?)',(request_id,
                        parent_request,identifier,canonical(relation).decode(),sha256(canonical(relation))))
                    self._event(db,request_id,'prepared','沿用原 P 和完整前稿，仅修订已指出的问题；原失败与费用保留。')
        return self.analyze(identifier,request_id,client,connection_revision=connection_revision,
            credential_generation=credential_generation)

    def _unknown_reissue(self,identifier,*,request_id=None,parent_request=None):
        with self.tasks.transaction() as db:
            self._task(db,identifier)
            row=db.execute('SELECT * FROM reference_target_unknown_reissues WHERE '+
                ('id=?' if request_id is not None else 'unknown_reissue_of=?'),
                (task_id(request_id or parent_request),)).fetchone()
        if row is None:return None
        value=json.loads(row['document'])
        if (row['task_id']!=identifier or value['task_id']!=identifier or value['request_id']!=row['id']
                or value['unknown_reissue_of']!=row['unknown_reissue_of']
                or sha256(canonical(value))!=row['document_sha256']):
            raise TaskError('目标核定显式重发授权摘要或任务身份不一致。')
        return value

    @staticmethod
    def _pending(db,identifier,*,except_parent=None):
        # An explicitly abandoned unknown remains unknown/charged. It ceases
        # blocking new proposals only after its single authorized child has a
        # known terminal receipt; an unknown child cannot be replayed again.
        rows=db.execute('SELECT i.id,r.document FROM reference_target_intents i LEFT JOIN '
            'reference_target_receipts r ON r.id=i.id WHERE i.task_id=? AND '
            '(r.id IS NULL OR json_extract(r.document,"$.state")="unknown")',(identifier,)).fetchall()
        for row in rows:
            if row['id']==except_parent:continue
            child=db.execute('SELECT a.id,a.task_id,a.unknown_reissue_of,a.document authorization,'
                'a.document_sha256 authorization_sha,r.document,r.document_sha256 receipt_sha '
                'FROM reference_target_unknown_reissues a '
                'JOIN reference_target_receipts r ON r.id=a.id WHERE a.unknown_reissue_of=?',
                (row['id'],)).fetchone()
            if child:
                authorization=json.loads(child['authorization']);receipt=json.loads(child['document'])
                if (sha256(canonical(authorization))!=child['authorization_sha']
                        or sha256(canonical(receipt))!=child['receipt_sha']
                        or child['task_id']!=identifier or authorization.get('task_id')!=identifier
                        or authorization.get('request_id')!=child['id']
                        or authorization.get('unknown_reissue_of')!=row['id']
                        or receipt.get('task_id')!=identifier or receipt.get('request_id')!=child['id']
                        or authorization.get('supplier_checked') is not True
                        or authorization.get('retain_unknown_charge') is not True):
                    raise TaskError('原未知调用的恢复处置或终态回执摘要不一致。')
                if receipt['state']!='unknown':continue
            return True
        return False

    def _historical_input(self,identifier,context,*,active=False):
        if self.historical_reference_provider is None:
            raise TaskError('原版 P 发布和完整库存的历史核验尚未连接；未重发。')
        old=reference_target_input(context['input'])
        supplied=reference_target_input(self.historical_reference_provider(identifier,
            source_binding=deepcopy(old['source_binding']),inventory_sha256=sha256(canonical(old['inventory']))))
        if old!=supplied or old['task_id']!=identifier:
            raise TaskError('原版 P、完整库存或原文与未知请求不一致；不能替换为最新来源。')
        with self.tasks.transaction() as db:
            self._task(db,identifier,active=active)
            rows=db.execute('SELECT p.id,json_extract(r.document,"$.title") title,'
                'json_extract(r.document,"$.doi") doi FROM paper_tasks l JOIN papers p ON p.id=l.paper_id '
                'JOIN paper_revisions r ON r.paper_id=p.id AND r.revision=p.revision WHERE l.task_id=?',
                (identifier,)).fetchall()
        if (len(rows)!=1 or rows[0]['id']!=old['paper_id']
                or old['paper']!={key:rows[0][key] for key in ('title','doi')}):
            raise TaskError('原目标请求与当前任务的唯一论文身份不一致。')
        return supplied

    def scope_source(self,identifier,request_id):
        """Verify the retained disposition chain without models or credentials."""
        saved=self._stored(identifier,task_id(request_id))
        if saved is None or saved[2] is None:raise TaskError('目标核定的完整历史回执尚未保留。')
        final_context=saved[1];context=final_context;current=request_id;visited=set();replays=0
        historical=bool(self._unknown_reissue(identifier,request_id=request_id)
            or self._original_revision(identifier,request_id=request_id))
        if not historical:return self._input(identifier,active=False)
        while True:
            if current in visited or len(visited)>=3:raise TaskError('原来源关系循环或超过原任务三轮。')
            visited.add(current)
            row,context,receipt,_events=self._stored(identifier,current)
            if reference_target_input(context['input'])!=final_context['input']:
                raise TaskError('原恢复与修订链的完整 P 快照不一致。')
            binding=context['model_binding'];model=receipt['model_receipt']
            body=request_body(DeepSeekConfig(**binding['config']),context['messages'],
                model=binding['model'],thinking=binding['thinking'])
            if (sha256(body)!=context['request_sha256'] or model.get('request_sha256')!=context['request_sha256']
                    or model.get('provider')!='deepseek' or model.get('requested_model')!=binding['model']):
                raise TaskError('原恢复或修订的模型正文与已保存回执不一致。')
            validate_prepared_messages(STAGE,context['messages'],context['input'])
            replay=self._unknown_reissue(identifier,request_id=current)
            revision=self._original_revision(identifier,request_id=current)
            if replay:
                if revision:raise TaskError('同一版本不能同时是重发与修订。')
                parent=self._stored(identifier,replay['unknown_reissue_of'])
                if parent is None or parent[2] is None or parent[2]['state']!='unknown':
                    raise TaskError('原显式重发缺少保留的未知父请求。')
                parent_row,parent_context,parent_receipt,_=parent
                expected={**deepcopy(parent_context),'unknown_reissue':dict(
                    unknown_reissue_of=replay['unknown_reissue_of'],authorization_sha256=sha256(canonical(replay)))}
                if (context!=expected or replay.get('supplier_checked') is not True
                        or replay.get('retain_unknown_charge') is not True or replay.get('automatic_retry') is not False
                        or replay.get('authorization_kind')!='supplier_checked_unknown_reissue_once'
                        or replay['original_context_sha256']!=parent_row['document_sha256']
                        or replay['original_receipt_sha256']!=parent_row['receipt_sha256']
                        or replay['original_input_sha256']!=sha256(canonical(parent_context['input']))
                        or replay['original_request_sha256']!=parent_context['request_sha256']):
                    raise TaskError('原未知调用的显式处置、父来源或费用保留关系不一致。')
                replays+=1;current=replay['unknown_reissue_of'];continue
            if revision:
                parent=self._stored(identifier,revision['parent_request'])
                if parent is None or parent[2] is None or parent[2]['state'] not in {'invalid_output','unavailable'}:
                    raise TaskError('原来源修订缺少已知完整失败父稿。')
                parent_row,parent_context,parent_receipt,_=parent
                previous=context.get('revision_parent') or {};answer=previous.get('complete_previous_answer')
                if (context.get('original_source_revision')!=dict(parent_request=revision['parent_request'],
                        relation_sha256=sha256(canonical(revision)))
                        or context['model_binding']!=parent_context['model_binding']
                        or revision.get('operation')!='revise_complete_original_source_answer'
                        or revision.get('original_source_preserved') is not True
                        or revision.get('automatic_retry') is not False
                        or revision['parent_context_sha256']!=parent_row['document_sha256']
                        or revision['parent_receipt_sha256']!=parent_row['receipt_sha256']
                        or revision['original_input_sha256']!=sha256(canonical(parent_context['input']))
                        or revision['source_output_sha256']!=parent_receipt['model_receipt'].get('output_sha256')
                        or previous.get('parent_request')!=revision['parent_request']
                        or previous.get('parent_context_sha256')!=parent_row['document_sha256']
                        or previous.get('parent_receipt_sha256')!=parent_row['receipt_sha256']
                        or sha256(canonical(answer))!=revision['source_output_sha256']):
                    raise TaskError('原来源修订、完整前稿或父回执绑定不一致。')
                current=revision['parent_request'];continue
            if (replays!=1 or receipt['state']!='unknown' or model.get('state')!='unknown'
                    or context.get('unknown_reissue') or context.get('original_source_revision')):
                raise TaskError('原来源关系未回溯到唯一已处置的未知请求。')
            break
        return self._historical_input(identifier,final_context)

    def _unknown_reissue_context(self,identifier,parent_request,client,connection_revision,credential_generation):
        saved=self._stored(identifier,parent_request)
        if saved is None or saved[2] is None or saved[2]['state']!='unknown':
            raise TaskError('只允许核对供应商后的原未知目标请求显式重发一次。')
        row,parent,receipt,_events=saved
        if parent.get('unknown_reissue') or self._unknown_reissue(identifier,request_id=parent_request):
            raise TaskError('显式恢复请求不能再次重发；未知状态和费用继续保留。')
        self._historical_input(identifier,parent,active=True)
        current=self._model(identifier,client,connection_revision,credential_generation)
        old=parent['model_binding'];old_view,new_view=deepcopy(old),deepcopy(current)
        old_authority=old_view.pop('authorization_sha256');new_authority=new_view.pop('authorization_sha256')
        if old_view!=new_view:
            raise TaskError('原模型、配置、凭据或账本身份已变化；不能换模型重发。')
        with client.calls.transaction() as db:
            _,policy=client.calls._latest_policy(db)
            policies=[r[0] for r in db.execute('SELECT document FROM policy WHERE id=1')]
            policies.extend(r[0] for r in db.execute('SELECT document FROM policy_revisions ORDER BY seq'))
        if (not any(sha256(p.encode())==old_authority and json.loads(p)['config']==old['config'] for p in policies)
                or sha256(policy.encode())!=new_authority):
            raise TaskError('原模型授权政策或当前记账许可未通过核验。')
        call=client.calls.lookup(parent_request) or {};model_receipt=call.get('receipt') or {}
        if (call.get('request_sha256')!=parent['request_sha256']
                or model_receipt.get('state')!='unknown' or model_receipt.get('usage') is not None
                or model_receipt.get('response_sha256') is not None or model_receipt.get('output_sha256') is not None
                or model_receipt.get('transport_attempts')!=1
                or model_receipt.get('request_sha256')!=parent['request_sha256']
                or model_receipt.get('requested_model')!=old['model']
                or {k:v for k,v in model_receipt.items() if k in _MODEL_RECEIPT_FIELDS}!=receipt['model_receipt']
                or sha256(request_body(client.calls.config,parent['messages'],model=client.model,
                    thinking=client.thinking))!=parent['request_sha256']):
            raise TaskError('原未知调用、完整请求正文或费用状态未通过核验；未重发。')
        validate_prepared_messages(STAGE,parent['messages'],parent['input'])
        return dict(parent=parent,parent_context_sha256=row['document_sha256'],
            parent_receipt_sha256=row['receipt_sha256'],parent_model_call_sha256=sha256(canonical(call)),
            original_input_sha256=sha256(canonical(parent['input'])),current_model_binding=current)

    def _authorized_reissue_context(self,identifier,request_id,client,connection_revision,credential_generation):
        authorization=self._unknown_reissue(identifier,request_id=request_id)
        if authorization is None:return None
        proof=self._unknown_reissue_context(identifier,authorization['unknown_reissue_of'],client,
            connection_revision,credential_generation)
        fixed=dict(original_context_sha256=proof['parent_context_sha256'],
            original_receipt_sha256=proof['parent_receipt_sha256'],
            original_model_call_sha256=proof['parent_model_call_sha256'],
            original_input_sha256=proof['original_input_sha256'],
            original_request_sha256=proof['parent']['request_sha256'],
            current_model_binding=proof['current_model_binding'])
        if (any(authorization.get(key)!=value for key,value in fixed.items())
                or authorization.get('supplier_checked') is not True
                or authorization.get('retain_unknown_charge') is not True
                or authorization.get('automatic_retry') is not False
                or authorization.get('authorization_kind')!='supplier_checked_unknown_reissue_once'):
            raise TaskError('原显式授权、未知费用或源模型绑定已变化；未继续恢复。')
        return {**deepcopy(proof['parent']),'unknown_reissue':dict(
            unknown_reissue_of=authorization['unknown_reissue_of'],
            authorization_sha256=sha256(canonical(authorization)))}

    def unknown_reissue_options(self,identifier,client,*,connection_revision,credential_generation):
        options=[]
        for item in self.history(identifier):
            if item['original_state']!='unknown':continue
            parent=item['request_id'];claimed=self._unknown_reissue(identifier,parent_request=parent)
            option=dict(unknown_reissue_of=parent,enabled=False,reason='原未知请求尚未通过完整历史核验。',
                original_context_sha256=item['context_sha256'],retains_unknown_charge=True)
            if claimed:
                option.update(request_id=claimed['request_id'],reason='这笔未知请求的一次显式恢复已使用；请查看原恢复记录。')
            else:
                try:
                    self._unknown_reissue_context(identifier,parent,client,connection_revision,credential_generation)
                    with self.tasks.transaction() as db:
                        count=db.execute('SELECT count(*) FROM reference_target_intents WHERE task_id=?',(identifier,)).fetchone()[0]
                        limit=db.execute('SELECT max_suggestions FROM reference_target_scopes WHERE task_id=?',(identifier,)).fetchone()[0]
                        if count>=limit:raise TaskError('原任务三次目标建议已用完；未增加轮次。')
                        if self._pending(db,identifier,except_parent=parent):raise TaskError('本任务另有待定请求；未重发。')
                    option.update(enabled=True,reason='明确确认供应商已核对后，仅原完整正文可显式重发一次；未知费用保留。')
                except (ValueError,KeyError,TypeError,OSError):pass
            options.append(option)
        return options

    def reissue_unknown(self,identifier,request_id,client,*,unknown_reissue_of,supplier_checked,
                        retain_unknown_charge,connection_revision,credential_generation):
        task_id(identifier);task_id(request_id);task_id(unknown_reissue_of)
        if supplier_checked is not True or retain_unknown_charge is not True or request_id==unknown_reissue_of:
            raise TaskError('必须明确确认供应商已核对、保留未知费用，并使用一次恢复的独立记账编号。')
        with self._lease(identifier) as acquired:
            if not acquired:
                claimed=self._unknown_reissue(identifier,parent_request=unknown_reissue_of)
                if claimed and claimed['request_id']==request_id:return self.get(identifier,request_id)
                raise TaskError('本任务正在处理原请求；未重复发送。')
            claimed=self._unknown_reissue(identifier,parent_request=unknown_reissue_of)
            if claimed:
                if claimed['request_id']!=request_id:raise TaskError('这笔未知请求已经授权恢复一次；不能再建恢复请求。')
            else:
                proof=self._unknown_reissue_context(identifier,unknown_reissue_of,client,connection_revision,credential_generation)
                authorization=dict(version=1,task_id=identifier,request_id=request_id,unknown_reissue_of=unknown_reissue_of,
                    authorization_kind='supplier_checked_unknown_reissue_once',supplier_checked=True,retain_unknown_charge=True,
                    automatic_retry=False,original_context_sha256=proof['parent_context_sha256'],
                    original_receipt_sha256=proof['parent_receipt_sha256'],
                    original_model_call_sha256=proof['parent_model_call_sha256'],
                    original_input_sha256=proof['original_input_sha256'],
                    original_request_sha256=proof['parent']['request_sha256'],
                    current_model_binding=proof['current_model_binding'],at=datetime.now(timezone.utc).isoformat())
                context={**deepcopy(proof['parent']),'unknown_reissue':dict(
                    unknown_reissue_of=unknown_reissue_of,authorization_sha256=sha256(canonical(authorization)))}
                with self.tasks.transaction() as db:
                    self._task(db,identifier,active=True)
                    if db.execute('SELECT 1 FROM reference_target_intents WHERE id=?',(request_id,)).fetchone():
                        raise TaskError('恢复编号已有其他意图；不能覆盖。')
                    if self._pending(db,identifier,except_parent=unknown_reissue_of):raise TaskError('本任务另有待定请求；未恢复。')
                    limit=db.execute('SELECT max_suggestions FROM reference_target_scopes WHERE task_id=?',(identifier,)).fetchone()[0]
                    count=db.execute('SELECT count(*) FROM reference_target_intents WHERE task_id=?',(identifier,)).fetchone()[0]
                    if count>=limit:raise TaskError('原任务最多三次目标建议已用完；未增加轮次。')
                    db.execute('INSERT INTO reference_target_intents VALUES (?,?,?,?,?)',(request_id,identifier,
                        canonical(context).decode(),sha256(canonical(context)),authorization['at']))
                    db.execute('INSERT INTO reference_target_unknown_reissues VALUES (?,?,?,?,?)',(request_id,
                        unknown_reissue_of,identifier,canonical(authorization).decode(),sha256(canonical(authorization))))
                    self._event(db,request_id,'prepared','供应商已核对；原未知费用保留，仅显式恢复原请求一次。')
        # The regular service performs dispatch/reconciliation under its own
        # lease and normal same-task accounting. No alternate model client.
        return self.analyze(identifier,request_id,client,connection_revision=connection_revision,
            credential_generation=credential_generation)

    def get(self, identifier, request_id=None):
        """Historical status without model connection, credentials or B access."""
        if request_id is None: return self.history(identifier)
        saved = self._stored(identifier, request_id)
        if saved is None: return None
        row, context, receipt, events = saved
        original_state = receipt['state'] if receipt else events[-1]['state']
        state = original_state
        reissued=self._unknown_reissue(identifier,request_id=request_id)
        superseded=self._unknown_reissue(identifier,parent_request=request_id)
        revision=self._original_revision(identifier,request_id=request_id)
        successor=self._original_revision(identifier,parent_request=request_id)
        historical=bool(reissued or revision)
        blocking=original_state in {'prepared','dispatching','unknown'}
        if superseded:
            child=self._stored(identifier,superseded['request_id'])
            if child and child[2] and child[2]['state']!='unknown':blocking=False
        if original_state == 'completed':
            try:
                current=(self._historical_input(identifier,context) if historical
                    else self._input(identifier,active=False))
                if current != context['input']:
                    state = 'source_unavailable'
            except Exception:
                state = 'source_unavailable'
        with self.tasks.transaction() as db:
            count=db.execute('SELECT count(*) FROM reference_target_intents WHERE task_id=?',(identifier,)).fetchone()[0]
            limit=db.execute('SELECT max_suggestions FROM reference_target_scopes WHERE task_id=?',(identifier,)).fetchone()[0]
            pending=self._pending(db,identifier)
        can_revise=(historical and original_state in {'invalid_output','unavailable'}
            and (receipt.get('model_receipt') or {}).get('state')=='completed'
            and count<limit and not pending and successor is None)
        return dict(**_FLAGS, task_id=identifier, request_id=request_id, state=state,
            original_state=original_state,
            at=row['at'], context_sha256=row['document_sha256'], source_binding=(context['input'] or {}).get('source_binding'),
            scientific_adapter=context['scientific_adapter'],
            paper=(context['input'] or {}).get('paper'), events=events, receipt=receipt,
            blocking=blocking,original_source_preserved=historical,historical_source=historical,
            can_revise_original=can_revise,original_revision_of=revision['parent_request'] if revision else None,
            original_revision=(dict(request_id=revision['request_id'],parent_request=revision['parent_request']) if revision
                else dict(request_id=successor['request_id'],parent_request=request_id) if successor else None),
            unknown_reissue=(dict(request_id=reissued['request_id'],unknown_reissue_of=reissued['unknown_reissue_of'],
                retain_unknown_charge=True,authorization_kind=reissued['authorization_kind']) if reissued
                else dict(request_id=superseded['request_id'],unknown_reissue_of=request_id,
                    retain_unknown_charge=True,authorization_kind=superseded['authorization_kind']) if superseded else None),
            report=receipt.get('report') if receipt else None, usage=receipt.get('usage') if receipt else None,
            can_select=False, selection_note='建议仅供参考；本服务不选择、冻结或发布任何 A/B 输入。',
            reviews=self.reviews(identifier, request_id))

    def analyze(self, identifier, request_id, client, *, connection_revision, credential_generation):
        task_id(identifier); task_id(request_id)
        with self._lease(identifier) as acquired:
            if not acquired:
                prior = self.get(identifier, request_id)
                if prior: return prior
                raise TaskError('同一论文任务正在核定目标；未新增模型请求。')
            saved = self._stored(identifier, request_id)
            if saved and saved[2]: return self.get(identifier, request_id)
            if saved:
                context = saved[1]
                if context.get('preflight_error'):
                    return self._finish(identifier, request_id, 'unavailable', code=context['preflight_error'],
                        message=context['preflight_message'])
                if model_ledger_identity(client) != context['model_binding']['model_ledger_sha256']:
                    raise TaskError('请先用原模型账本核对待定请求；未切换账本或重发。')
                call = client.calls.lookup(request_id)
                if call is not None:
                    receipt = call.get('receipt')
                    if receipt and receipt.get('state') == 'completed' and 'structured_output' in receipt:
                        try:
                            current = self._generation_context(identifier, request_id, client, connection_revision, credential_generation)
                            if current != context: raise TaskError('context changed')
                        except Exception:
                            return self._finish(identifier, request_id, 'paused', model_receipt=receipt,
                                code='reference_context_changed', message='已完成模型回执保留；当前来源、连接或契约已变化，未重复发送。')
                        return self._accept(identifier, request_id, context, call)
                    state = 'unknown' if not receipt or receipt.get('state') == 'unknown' else 'failed'
                    return self._finish(identifier, request_id, state, model_receipt=receipt,
                        code='model_request_requires_attention', message='原模型调用未形成完整可核定建议；未重新发送，费用保留。')
                try:
                    current = self._generation_context(identifier, request_id, client, connection_revision, credential_generation)
                    if current != context: raise TaskError('context changed')
                except Exception:
                    return self._finish(identifier, request_id, 'paused', code='unsent_context_changed',
                        message='尚未收费的原意图与当前 P、来源、连接或契约不一致；未发送。')
            else:
                with self.tasks.transaction() as db:
                    self._task(db, identifier, active=True)
                    if self._pending(db,identifier):
                        raise TaskError('旧目标建议或模型状态待核对；不能换请求编号再次发送。')
                try:
                    context = self._generation_context(identifier, request_id, client, connection_revision, credential_generation)
                    unavailable = None
                except ReferenceTargetUnavailable as error:
                    unavailable = (error.code, str(error))
                except TaskError:
                    unavailable = ('target_preflight_invalid', '目标核定发送前的文献来源或连接检查未通过；未发送或收费。')
                except Exception:
                    unavailable = ('target_preflight_unavailable', 'P 来源、参考连接或主动 Adapter 尚未通过核验；未发送或收费。')
                if unavailable:
                    context = dict(version=VERSION, scope='reference_only', task_id=identifier, input=None,
                        model_binding=None, messages=None, request_sha256=None, scientific_adapter=None,
                        controller_sha256=sha256(Path(__file__).read_bytes()), preflight_error=unavailable[0],
                        preflight_message=unavailable[1])
                with self.tasks.transaction() as db:
                    self._task(db, identifier, active=True)
                    db.execute('INSERT OR IGNORE INTO reference_target_scopes VALUES (?,?,?)',
                        (identifier, self.max_suggestions, datetime.now(timezone.utc).isoformat()))
                    limit = db.execute('SELECT max_suggestions FROM reference_target_scopes WHERE task_id=?',
                        (identifier,)).fetchone()[0]
                    count = db.execute('SELECT count(*) FROM reference_target_intents WHERE task_id=?', (identifier,)).fetchone()[0]
                    if count >= limit:
                        raise TaskError('本任务已达到原有最多三次目标建议上限；全部失败与费用保留。')
                    db.execute('INSERT INTO reference_target_intents VALUES (?,?,?,?,?)', (request_id, identifier,
                        canonical(context).decode(), sha256(canonical(context)), datetime.now(timezone.utc).isoformat()))
                    self._event(db, request_id, 'prepared')
                if unavailable:
                    return self._finish(identifier, request_id, 'unavailable', code=unavailable[0], message=unavailable[1])
            try:
                current = self._generation_context(identifier, request_id, client, connection_revision, credential_generation)
                if current != context: raise TaskError('context changed')
            except Exception:
                return self._finish(identifier, request_id, 'paused', code='reference_context_changed',
                    message='固定 P、目标来源、连接或授权已变化；未继续发送。')
            with self.tasks.transaction() as db: self._event(db, request_id, 'dispatching')
            try:
                if context.get('unknown_reissue') or context.get('original_source_revision'):
                    validate_prepared_messages(STAGE,context['messages'],context['input'])
                    client.complete_json(request_id,context['messages'],expected_request_sha256=context['request_sha256'])
                else:
                    validate_prepared_messages(STAGE, context['messages'], context['input'])
                    client.complete_json(request_id, context['messages'])
            except Exception:
                call = client.calls.lookup(request_id)
                receipt = call.get('receipt') if call else None
                state = 'unknown' if call and (not receipt or receipt.get('state') == 'unknown') else 'failed'
                return self._finish(identifier, request_id, state, model_receipt=receipt,
                    code='model_request_requires_attention', message='目标建议调用未完成；已保留原意图、模型记录与费用，未自动重试。')
            call = client.calls.lookup(request_id)
            try:
                current = self._generation_context(identifier, request_id, client, connection_revision, credential_generation)
                if current != context: raise TaskError('context changed')
            except Exception:
                return self._finish(identifier, request_id, 'paused', model_receipt=call.get('receipt') if call else None,
                    code='reference_context_changed', message='模型返回期间 P、目标来源、连接或授权变化；原回复与费用保留，建议暂停。')
            if not call or not call.get('receipt'):
                return self._finish(identifier, request_id, 'unknown', code='model_receipt_unavailable',
                    message='模型回执尚未核对；未发布建议或重复请求。')
            return self._accept(identifier, request_id, context, call)
