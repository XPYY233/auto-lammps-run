"""User-requested AI plans over retained raw bytes, never a physics executor.

The same entry serves ordinary research and author-reference outputs. Only a
reference run can receive its associated paper methods; those inputs are never
offered to an independent research run. Model text is not an analysis result.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .condition_generation import source_bundle
from .deepseek import ModelError,request_body
from .ledger import LedgerError
from .manifest import canonical, private_directory, sha256
from .raw_outputs import RawOutputs
from .reference_targets import model_ledger_identity
from .runtime_launcher import read_regular
from .scientific_adapters import prepare_stage_messages, validate_prepared_messages
from .tasks import TaskError, task_id, text
from . import postprocess_tables as tables
from . import retained_analysis_bridge as mixed

VERSION=1
STAGE='output_postprocessing'
MAX_ROUNDS=3
MAX_QUESTIONS=16


def _pdf_layout_quote(source,quote):
    """Match PDF line wrapping only, retaining the exact original span.

    No case folding, word substitution, formula repair or numeric normalization.
    The receipt labels this match explicitly rather than claiming verbatim text.
    """
    def view(value):
        omitted=set()
        for match in re.finditer(r'(?<=[A-Za-z])-[ \t]*\r?\n[ \t]*(?=[a-z])',value):
            omitted.update(range(match.start(),match.end()))
        chars=[];positions=[]
        for index,char in enumerate(value):
            if index in omitted:continue
            if char.isspace():
                if chars and chars[-1]!=' ':chars.append(' ');positions.append(index)
            else:chars.append(char);positions.append(index)
        while chars and chars[-1]==' ':chars.pop();positions.pop()
        return ''.join(chars),positions
    normalized,positions=view(source);selected,_=view(quote)
    start=normalized.find(selected) if selected else -1
    if start<0:return None
    first,last=positions[start],positions[start+len(selected)-1]+1
    if last-first>12000:return None
    return dict(match='pdf_layout_normalized',source_quote=source[first:last],
                source_character_range=[first,last],normalization='whitespace_and_line_end_word_hyphen_only')


def _publish_bytes(path,data):
    """An incomplete write never acquires a final artifact name."""
    path=Path(path)
    if path.exists():
        # Recover the sole temporary hard link left by a killed atomic publish.
        info=path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink>1:
            for temporary in path.parent.glob('.write-*'):
                other=temporary.lstat()
                if (other.st_dev,other.st_ino)==(info.st_dev,info.st_ino):temporary.unlink()
        if read_regular(path,64*1024*1024,private=True)!=data:
            raise TaskError('已保存分析产物不同；旧文件保留，未覆盖。')
        return
    descriptor,temporary=tempfile.mkstemp(prefix='.write-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,path,follow_symlinks=False)
        except FileExistsError:
            if read_regular(path,64*1024*1024,private=True)!=data:raise TaskError('产物发布期间已有不同文件，未覆盖。')
    finally:Path(temporary).unlink(missing_ok=True)


def selection_context(evidence):
    tool=tables.tool_context()
    model_schema=deepcopy(tool['plan_schema'])
    table_schema=model_schema['properties']['tables']['items']
    del table_schema['properties']['sha256'];table_schema['required'].remove('sha256')
    example=deepcopy(tool['minimal_example'])
    for table in example['tables']:del table['sha256']
    from .postprocess_figures import tool_context as figure_context
    context=dict(tool='retained_output_analysis',version=VERSION,
        source_sha256=sha256(Path(__file__).read_bytes()),numeric_tool=tool,figure_tool=figure_context(),
        model_plan_schema=model_schema,synthetic_model_plan_example=example,
        output_fields=dict(status='ready or needs_evidence',summary='brief rationale, not observed results',
            limitations='list of explicit limitations',plan='model_plan_schema, or null if needs_evidence',
            citations='objects {id,source_id,quote}; exact nonempty quotes; PDF whitespace/line-end word wrapping may match with original span retained, never word/formula/number substitutions',
            column_evidence='objects {table,column,meaning,unit,citations:[citation ids]}; every raw and derived column'),
        assembly='file uses supplied logical source id; sha256 is bound by the service, omit it in model output',
        interpretation='meanings and units are model declarations supported by exact quotes, not semantic proof',
        constants='literal.evidence must name a citation that contains that exact numeric value',
        role=evidence['role'],scientific_status='not_evaluated',physical_evaluation_performed=False,
        new_HPC_action=False,scope_reduction_allowed=False)
    if evidence.get('input_representation')==mixed.REPRESENTATION:
        context['model_plan_schema']=dict(oneOf=[model_schema,mixed.plan_schema()],
            **{'$defs':model_schema.get('$defs',{}),'$schema':model_schema['$schema']})
        context.update(mixed_native_tool=mixed.tool_context(),
            alternative_plan='Existing version=1 plan, OR explicit mixed_native_v1 wrapper described by mixed_native_tool; no automatic dispatch by guessed file semantics.',
            source_view='Bounded previews plus full-source SHA/physical line counts. All computations use complete retained files; never compute from previews.',
            intake_limits=dict(files=mixed.MAX_FILES,input_bytes=mixed.MAX_BYTES,physical_lines=mixed.MAX_ROWS))
    return context


def _validate_output(evidence,value,files):
    if not isinstance(value,dict) or set(value)!={'status','summary','limitations','plan','citations','column_evidence'}:
        raise TaskError('分析答复需要完整状态、方案、逐列依据与局限。')
    text(value['summary'],4000)
    if not isinstance(value['limitations'],list) or len(value['limitations'])>32:
        raise TaskError('分析局限列表无效。')
    for item in value['limitations']:text(item,2000)
    allowed={s['id']:s for s in evidence['sources']};citations={}
    if not isinstance(value['citations'],list) or len(value['citations'])>128:
        raise TaskError('分析出处列表无效。')
    for row in value['citations']:
        if not isinstance(row,dict) or set(row)!={'id','source_id','quote'}:
            raise TaskError('分析出处需要来源标识和逐字原文。')
        key=text(row['id'],80);source=allowed.get(row['source_id']);quote=text(row['quote'],6000)
        if key in citations or source is None:
            raise TaskError('分析出处标识重复或不属于允许来源，未自动修写引用。')
        layout=None
        if quote not in source['text']:
            if source['origin']=='paper':layout=_pdf_layout_quote(source['text'],quote)
            if layout is None:
                raise TaskError(f'分析出处 {key} 未逐字匹配允许来源 {row["source_id"]}；PDF排版之外的改写不能通过。')
        if source['origin']=='output' and quote.encode('ascii') not in files.get(source['id'],b''):
            raise TaskError('原始数据引用必须逐字存在于完整源文件；预览间隔标记不是原文。')
        citations[key]=dict(row,source_sha256=sha256(canonical(source)),locator=source['locator'],**(layout or {}))
        if source.get('complete_source_sha256'):
            citations[key]['complete_source_sha256']=source['complete_source_sha256']
    if value['status']=='needs_evidence':
        if value['plan'] is not None or value['column_evidence']!=[]:
            raise TaskError('缺少依据时不能同时执行分析方案。')
        return None,list(citations.values())
    if value['status']!='ready' or not isinstance(value['plan'],dict):
        raise TaskError('分析状态或方案无效。')
    plan=deepcopy(value['plan'])
    if mixed.is_mixed(plan):
        if evidence.get('input_representation')!=mixed.REPRESENTATION:
            raise TaskError('原请求的固定工具菜单没有混合分析模式，不能在恢复时更换契约。')
        mixed.validate_plan(plan,files)
        expected=mixed.column_units(plan,files)
        if any(ref not in citations for item in plan['method_evidence'] for ref in item['citations']):
            raise TaskError('原始解析、元素映射与每项分析方法需要允许来源的引用。')
    else:
        for table in plan.get('tables',[]):
            if not isinstance(table,dict) or table.get('file') not in files:
                raise TaskError('分析表必须选择本次已核验的原始文件。')
            digest=sha256(files[table['file']])
            if table.get('sha256',digest)!=digest:raise TaskError('模型不能改变原始文件摘要。')
            table['sha256']=digest
        tables.validate_plan(plan,files)
        expected=tables.column_units(plan,files)
    seen=set()
    if not isinstance(value['column_evidence'],list):raise TaskError('每列分析数据都需要说明与出处。')
    for row in value['column_evidence']:
        if not isinstance(row,dict) or set(row)!={'table','column','meaning','unit','citations'}:
            raise TaskError('分析列依据字段不完整。')
        key=(row['table'],row['column']);refs=row['citations']
        if (key not in expected or key in seen or row['unit']!=expected[key]
                or not isinstance(refs,list) or not 1<=len(refs)<=16 or len(set(refs))!=len(refs)
                or any(ref not in citations for ref in refs)):
            raise TaskError('分析列说明、单位或来源与方案不一致。')
        text(row['meaning'],2000);seen.add(key)
    if seen!=set(expected):raise TaskError('原始及派生数据列不能遗漏依据。')
    def literals(node):
        if isinstance(node,dict):
            if 'literal' in node:
                citation=citations.get(node['evidence'])
                source=allowed.get(citation['source_id']) if citation else None
                numbers=[] if source is None or source['origin']=='output' else re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?',citation['quote'])
                if not any(float(number)==node['literal'] for number in numbers):
                    raise TaskError('分析常量没有匹配的逐字数值依据。')
            for child in node.values():literals(child)
        elif isinstance(node,list):
            for child in node:literals(child)
    literals(plan)
    return plan,list(citations.values())


class OutputAnalysis:
    def __init__(self,raw_outputs,*,connection_provider,reference_sources=None,model_calls=None):
        if not isinstance(raw_outputs,RawOutputs) or not callable(connection_provider):
            raise TaskError('原始输出分析需要同一任务、账本与模型连接。')
        self.raw=raw_outputs;self.tasks=raw_outputs.tasks
        self.connection_provider=connection_provider;self.reference_sources=reference_sources
        self.model_calls=model_calls
        self.directory=private_directory(self.tasks.path.parent/'output-analyses')
        self.locks=private_directory(self.directory/'locks')
        with self.tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS output_analysis_intents(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,scope TEXT NOT NULL,document TEXT NOT NULL,sha256 TEXT NOT NULL,at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS output_analysis_events(id TEXT NOT NULL,sequence INTEGER NOT NULL,state TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            db.execute('CREATE TABLE IF NOT EXISTS output_analysis_receipts(id TEXT PRIMARY KEY,document TEXT NOT NULL,sha256 TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS output_analysis_failures(id TEXT NOT NULL,sequence INTEGER NOT NULL,document TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(id,sequence))')
            for table in ('output_analysis_intents','output_analysis_events','output_analysis_receipts','output_analysis_failures'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,"immutable output analysis history"); END')

    @contextmanager
    def _lease(self,identifier):
        fd=os.open(self.locks/(identifier+'.lock'),os.O_CREAT|os.O_NOFOLLOW|os.O_RDWR,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1:raise TaskError('分析进程锁身份无效。')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise TaskError('同一任务正在分析，未重复调用模型。') from None
            yield
        finally:os.close(fd)

    def _active(self,identifier):
        fd=os.open(self.locks/(identifier+'.lock'),os.O_CREAT|os.O_NOFOLLOW|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
            return False
        finally:os.close(fd)

    def _input(self,identifier,request,question,representation=mixed.REPRESENTATION):
        row=self.raw._request(identifier,request)
        identity=self.raw.ledger.evaluation_snapshot(row['evaluation'])['identity']
        role='reference' if identity['role']=='reference' else 'research'
        listing=[f for f in self.raw.listing(identifier)['files'] if f['request_id']==request]
        bounded=representation==mixed.REPRESENTATION
        maximum=mixed.MAX_FILES if bounded else tables.MAX_TABLES
        if not 1<=len(listing)<=maximum:raise TaskError('本次作业须先回收完整且符合分析工具容量的原始输出清单；未调用模型。')
        files={};sources=[dict(id='user-request',origin='user',locator='analysis request',text=question)]
        inventory=[];total=0;line_count=0
        for index,item in enumerate(sorted(listing,key=lambda f:f['name'])):
            if not bounded and Path(item['name']).suffix.lower() not in {'.txt','.csv','.dat'}:
                raise TaskError('本次输出包含尚未支持的原始格式；未静默跳过文件或调用模型。')
            total+=item['size']
            limit=mixed.MAX_BYTES if bounded else tables.MAX_INPUT_BYTES
            if item['size']>limit or total>limit:
                raise TaskError('完整原始数据超过分析容量；未截断或发送模型。')
            _,_,stream=self.raw.download(identifier,item['id'])
            with stream:data=stream.read(item['size']+1)
            if len(data)!=item['size'] or sha256(data)!=item['sha256']:
                raise TaskError('实际读取字节与原始清单不一致，未重新绑定变化的数据。')
            try:content=data.decode('ascii')
            except UnicodeError:raise TaskError('本分析入口只接收完整的 ASCII 数值文本。') from None
            key='source'+str(index+1);files[key]=data
            if bounded:
                metadata,content=mixed.describe(data)
                line_count+=metadata['line_count']
                if line_count>mixed.MAX_ROWS:raise TaskError('完整源文件总行数超过分析容量；未静默抽样。')
                # Excerpts have one owner in the model input. The inventory keeps
                # complete-source identity and counts, avoiding duplicate text.
                preview=metadata.pop('preview')
                sources.append(dict(id=key,origin='output',locator=item['name'],text=content,
                    preview=preview,complete_source_sha256=metadata['sha256'],
                    complete_source_line_count=metadata['line_count']))
                inventory.append(dict(id=key,name=item['name'],**metadata))
            else:
                sources.append(dict(id=key,origin='output',locator=item['name'],text=content))
                inventory.append(dict(id=key,name=item['name'],size=len(data),sha256=sha256(data)))
        methods=[];paper=None;source_binding=None;missing=[]
        if role=='reference' and self.reference_sources is not None:
            try:
                supplied=self.reference_sources(identifier)
                methods=source_bundle(supplied['sources']);paper=supplied['paper'];source_binding=supplied['source_binding']
            except (ValueError,KeyError,TypeError,OSError):
                missing.append('论文方法来源当前未通过核验；不得自行补充原文方法或单位。')
        if any(s['id'] in {x['id'] for x in sources} for s in methods):
            raise TaskError('原始输出与方法来源身份冲突。')
        sources+=methods
        evidence=dict(task_id=identifier,source_request_id=request,job_id=row['job_id'],role=role,
            scheduler_state=row['state'],accounted=True,manifest_sha256=row['manifest_sha256'],
            files=inventory,sources=sources,paper=paper,source_binding=source_binding,missing=missing,
            scientific_status='not_evaluated',question=question)
        if bounded:evidence.update(input_representation=mixed.REPRESENTATION,
            full_source_bytes=total,full_source_physical_lines=line_count,
            preview_is_analysis_input=False)
        return evidence,files

    def _event(self,identifier,state):
        with self.tasks.transaction() as db:
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM output_analysis_events WHERE id=?',(identifier,)).fetchone()[0]
            db.execute('INSERT INTO output_analysis_events VALUES(?,?,?,?)',(identifier,sequence,state,datetime.now(timezone.utc).isoformat()))

    def _stored(self,task,request):
        with self.tasks.transaction() as db:
            row=db.execute('SELECT i.*,r.document receipt,r.sha256 receipt_sha FROM output_analysis_intents i LEFT JOIN output_analysis_receipts r ON r.id=i.id WHERE i.id=?',(request,)).fetchone()
            if row is None:return None
            if row['task_id']!=task:raise TaskError('分析请求属于另一项任务。')
            event=db.execute('SELECT state FROM output_analysis_events WHERE id=? ORDER BY sequence DESC LIMIT 1',(request,)).fetchone()
            state=event[0] if event else 'prepared'
        value=json.loads(row['document']);receipt=json.loads(row['receipt']) if row['receipt'] else None
        if sha256(canonical(value))!=row['sha256'] or (receipt and sha256(canonical(receipt))!=row['receipt_sha']):
            raise TaskError('已保存分析记录摘要不一致。')
        return value,receipt,state

    def get(self,identifier,request):
        self.tasks.get(identifier);saved=self._stored(identifier,request)
        if saved is None:return None
        context,receipt,state=saved;result=None
        failure=None;call=None
        if receipt is None:
            with self.tasks.transaction() as db:
                failure_row=db.execute('SELECT document,sha256 FROM output_analysis_failures WHERE id=? ORDER BY sequence DESC LIMIT 1',(request,)).fetchone()
            if failure_row:
                failure=json.loads(failure_row[0])
                if sha256(canonical(failure))!=failure_row[1]:raise TaskError('技术恢复记录摘要不符。')
            if self.model_calls is not None and sha256(str(self.model_calls.path.resolve()).encode())==context['model_binding']['ledger']:
                call=self.model_calls.lookup(request)
        model_receipt=receipt.get('model_receipt') if receipt else (call or {}).get('receipt')
        if receipt:state=receipt['state']
        if state=='completed':
            try:
                evidence,_=self._input(identifier,context['input']['source_request_id'],context['input']['question'],
                    context['input'].get('input_representation'))
                if evidence!=context['input']:raise TaskError('source changed')
                folder=self.directory/request
                result=json.loads(read_regular(folder/'result.json',64*1024*1024,private=True))
                if sha256(canonical(result))!=receipt['result_sha256']:raise TaskError('result changed')
                for item in result['files']:
                    if sha256(read_regular(folder/item['name'],32*1024*1024,private=True))!=item['sha256']:raise TaskError('artifact changed')
            except (ValueError,KeyError,TypeError,OSError):state='source_unavailable';result=None
        return dict(request_id=request,task_id=identifier,at=context['at'],state=state,
            source_request_id=context['input']['source_request_id'],job_id=context['input']['job_id'],
            role=context['input']['role'],question=context['input']['question'],round=context['round'],
            maximum_rounds=MAX_ROUNDS,scientific_status='not_evaluated',
            model_receipt=model_receipt,
            recoverable=receipt is None and state in {'prepared','dispatching','analysis_execution_failed'} and not self._active(identifier),
            message=receipt.get('message','') if receipt else failure['message'] if failure else 'AI 分析方案尚未取得完整回执；没有重复发送。',
            proposal=receipt.get('proposal') if receipt else (model_receipt or {}).get('structured_output'),result=result)

    def history(self,identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:ids=[r[0] for r in db.execute('SELECT id FROM output_analysis_intents WHERE task_id=? ORDER BY at,id',(identifier,))]
        return [self.get(identifier,key) for key in ids]

    def status(self,identifier):
        self.tasks.get(identifier);runs={}
        for f in self.raw.listing(identifier)['files']:
            row=self.raw._request(identifier,f['request_id'])
            role=self.raw.ledger.evaluation_snapshot(row['evaluation'])['identity']['role']
            run=runs.setdefault(f['request_id'],dict(request_id=f['request_id'],job_id=row['job_id'],role='reference' if role=='reference' else 'research',files=[],total_bytes=0))
            run['files'].append(dict(name=f['name'],size=f['size']));run['total_bytes']+=f['size']
        return dict(task_id=identifier,runs=list(runs.values()),history=self.history(identifier))

    def _finish(self,identifier,request,state,*,receipt=None,proposal=None,message='',result_sha256=None):
        document=dict(state=state,message=message,proposal=proposal,model_receipt=receipt,result_sha256=result_sha256)
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO output_analysis_receipts VALUES(?,?,?)',(request,canonical(document).decode(),sha256(canonical(document))))
        self._event(request,state)
        return self.get(identifier,request)

    def _execution_failure(self,identifier,request,message):
        document=dict(message=text(message,2000),state='analysis_execution_failed')
        with self.tasks.transaction() as db:
            used=db.execute('SELECT COALESCE(SUM(length(document)),0) FROM output_analysis_failures WHERE id=?',(request,)).fetchone()[0]
            if used+len(canonical(document))>131072:raise TaskError('技术恢复审计容量已用完；原模型答复仍保留。')
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM output_analysis_failures WHERE id=?',(request,)).fetchone()[0]
            db.execute('INSERT INTO output_analysis_failures VALUES(?,?,?,?)',(request,sequence,canonical(document).decode(),sha256(canonical(document))))
        self._event(request,'analysis_execution_failed')
        return self.get(identifier,request)

    def generate(self,identifier,request,source_request,question):
        task_id(identifier);task_id(request);task_id(source_request);question=text(question,4000)
        with self._lease(identifier):
            life=self.tasks.lifecycle(identifier)
            if life['deleted'] or life['user_finished']:raise TaskError('任务已结束，不能新增分析。')
            saved=self._stored(identifier,request)
            if saved:
                context,receipt,_=saved
                if context['input']['source_request_id']!=source_request or context['input']['question']!=question:
                    raise TaskError('同一分析标识已用于其他内容。')
                if receipt:return self.get(identifier,request)
            representation=context['input'].get('input_representation') if saved else mixed.REPRESENTATION
            evidence,files=self._input(identifier,source_request,question,representation)
            scope=sha256(canonical(dict(task=identifier,request=source_request,question=question)))
            connection=self.connection_provider(identifier)
            if not connection.get('enabled'):raise TaskError('请先配置并启用本项目模型连接。')
            client=connection['client'];binding=dict(model=client.model,ledger=model_ledger_identity(client),
                revision=connection['connection_revision'],credential_generation=connection['credential_generation'])
            if saved:
                original_calls=self.model_calls if self.model_calls is not None else client.calls
                same_ledger=sha256(str(original_calls.path.resolve()).encode())==context['model_binding']['ledger']
                original=original_calls.lookup(request) if same_ledger else None
                original_receipt=(original or {}).get('receipt') or {}
                uncertain=not same_ledger or (original is not None and original_receipt.get('state') not in {'completed','not_sent','rejected','response_invalid'})
                try:body_matches=sha256(request_body(client.calls.config,context['messages'],model=client.model,thinking=client.thinking))==context['request_sha256']
                except ModelError:body_matches=False
                if context['input']!=evidence or context['model_binding']!=binding or not body_matches:
                    return self._finish(identifier,request,'unknown' if uncertain else 'paused',receipt=original_receipt,
                        proposal=original_receipt.get('structured_output'),message='原数据、模型正文或连接变化；未核对的原调用仍阻断重发。' if uncertain else '原数据、模型正文或连接已变化；未重发，原答复与费用保留。')
            else:
                with self.tasks.transaction() as db:
                    rows=[r[0] for r in db.execute('SELECT id FROM output_analysis_intents WHERE scope=? ORDER BY at,id',(scope,))]
                    total=db.execute('SELECT count(DISTINCT scope) FROM output_analysis_intents WHERE task_id=?',(identifier,)).fetchone()[0]
                prior=[self.get(identifier,key) for key in rows]
                if any(r['state'] in {'prepared','dispatching','unknown'} for r in prior):raise TaskError('本分析的原请求尚未核对，不能换标识重发。')
                if any(r['state']=='analysis_execution_failed' for r in prior):raise TaskError('已有有效模型答复等待技术恢复；请恢复原分析执行，不新增付费改稿。')
                complete=next((r for r in reversed(prior) if r['state']=='completed'),None)
                if complete:return complete
                if len(rows)>=MAX_ROUNDS:raise TaskError('同一分析方案已达三轮，完整失败与费用保留。')
                if not rows and total>=MAX_QUESTIONS:raise TaskError('任务已保存十六组后续分析，请复用已有结果。')
                previous=None
                if prior:
                    original=self._stored(identifier,prior[-1]['request_id'])[1]
                    previous=dict(complete_previous_answer=original['proposal'],feedback=original['message'],
                        instruction='Modify this complete prior proposal; do not rebuild or reduce the scope.')
                payload=dict(evidence,retained_output_analysis=selection_context(evidence),previous=previous)
                messages=[dict(role='system',content=(
                    'You are the application scientific AI selecting a complete bounded numeric postprocessing plan. '
                    'Return exactly JSON matching retained_output_analysis. Read every supplied source inventory, bounded preview and method source. '
                    'Previews are incomplete and only support method/column selection; trusted tools always compute from full retained bytes. '
                    'Choose either the existing version=1 numeric plan or the explicit mixed_native_v1 wrapper and unchanged native contracts. '
                    'Use all supplied files; do not omit failed-run limitations or invent missing results. '
                    'Declare each raw/derived column meaning and unit with exact source citations. '
                    'For a genuinely unknown unit use "unknown" with a citation to the raw header and state this limitation. '
                    'Scientific derived arithmetic must be justified by supplied methods; no invented formula or unit conversion. '
                    'Choose histogram bounds that include all requested observations; report excluded samples if any. '
                    'A scalar read from data must use the explicit one-based row selector, not a copied literal. '
                    'Table file is a logical source ID; omit table sha256, which the service binds. '
                    'No code, shell, HPC, new physics, target score, scientific pass or author-workflow modification. '
                    'If required scientific evidence is missing return status needs_evidence, plan null and empty column_evidence. '
                    'Explain rationale in concise Chinese; summary is proposed method, not fabricated observed statistics.')),
                    dict(role='user',content=canonical(payload).decode())]
                messages,proof=prepare_stage_messages(STAGE,messages,evidence)
                body=request_body(client.calls.config,messages,model=client.model,thinking=client.thinking)
                context=dict(input=evidence,model_binding=binding,round=len(rows)+1,at=datetime.now(timezone.utc).isoformat(),
                    messages=messages,scientific_adapter=proof,request_sha256=sha256(body))
                if client.calls.status()['remaining_requests']==0:raise TaskError('当前模型额度未允许新调用；未发送或消耗分析轮次。')
                self.raw.ledger.reserve_raw_export(source_request,sha256(canonical(dict(kind='output_analysis_intent',request=request))),len(canonical(context))+262144)
                with self.tasks.transaction() as db:
                    db.execute('INSERT INTO output_analysis_intents VALUES(?,?,?,?,?,?)',(request,identifier,scope,canonical(context).decode(),sha256(canonical(context)),context['at']))
                    db.execute('INSERT INTO output_analysis_events VALUES(?,?,?,?)',(request,1,'prepared',context['at']))
            call=client.calls.lookup(request)
            if call is None:
                validate_prepared_messages(STAGE,context['messages'],evidence)
                self._event(request,'dispatching')
                send_error=None
                try:client.complete_json(request,context['messages'],expected_request_sha256=context['request_sha256'])
                except Exception as error:send_error=error
                call=client.calls.lookup(request)
                if call is None and isinstance(send_error,ModelError) and str(send_error) in {'model_budget_exhausted','model_request_changed','input_too_large'}:
                    return self._finish(identifier,request,'not_sent',message='模型预算或冻结正文检查阻止发送；没有供应商调用。')
            receipt=(call or {}).get('receipt') or {}
            if (not call or call.get('request_sha256')!=context['request_sha256']
                    or receipt.get('request_sha256')!=context['request_sha256']):
                return self._finish(identifier,request,'unknown',receipt=receipt,message='模型原请求与回执未核对；未重复发送。')
            if receipt.get('state')!='completed':
                known=receipt.get('state') in {'not_sent','rejected','response_invalid'}
                return self._finish(identifier,request,'failed' if known else 'unknown',receipt=receipt,
                    message='模型请求未取得完整可用答复；原费用与记录保留，没有自动重发。')
            proposal=receipt.get('structured_output')
            if receipt.get('output_sha256')!=sha256(canonical(proposal)):
                return self._finish(identifier,request,'unknown',receipt=receipt,message='模型答复摘要不符，未执行分析。')
            try:
                if self._input(identifier,source_request,question,representation)[0]!=evidence:
                    return self._finish(identifier,request,'paused',receipt=receipt,proposal=proposal,message='原始来源在返回期间改变；原答复与费用保留，未执行分析。')
                current=self.connection_provider(identifier)
                if (not current.get('enabled') or dict(model=current['client'].model,ledger=model_ledger_identity(current['client']),
                        revision=current['connection_revision'],credential_generation=current['credential_generation'])!=binding):
                    return self._finish(identifier,request,'paused',receipt=receipt,proposal=proposal,message='模型连接在返回期间变化；原答复和费用保留，未执行分析。')
                validate_prepared_messages(STAGE,context['messages'],evidence)
            except (ValueError,TypeError,KeyError,OSError):
                return self._execution_failure(identifier,request,'模型完整答复已保留；来源或工具契约暂不可核验。请恢复原执行，不新增模型调用。')
            try:
                plan,citations=_validate_output(evidence,proposal,files)
                if plan is None:return self._finish(identifier,request,'needs_evidence',receipt=receipt,proposal=proposal,message=proposal['summary'])
                numeric=mixed.execute_plan(plan,files) if mixed.is_mixed(plan) else tables.execute_plan(plan,files)
                from .postprocess_figures import render_figures
            except (mixed.NativeExecutionError,OSError):
                return self._execution_failure(identifier,request,'模型完整答复已保留；固定分析工具或本地环境未完成。请恢复原执行，不新增模型调用。')
            except (ValueError,TypeError,KeyError) as error:
                message=str(error) if isinstance(error,(TaskError,tables.PostprocessError,mixed.AnalysisError)) else '分析方案或来源未通过检查，未发布结果。'
                return self._finish(identifier,request,'invalid_output',receipt=receipt,proposal=proposal,message=message)
            try:
                plots=render_figures(numeric.get('figure_report',numeric),role=evidence['role'],job_id=evidence['job_id'])
                artifacts=dict(plots['files']);plot_metadata={k:v for k,v in plots.items() if k!='files'}
                for derived in numeric['derived_tables']:artifacts[derived['id']+'.csv']=derived['csv'].encode('ascii')
                artifacts['numeric-report.json']=canonical(numeric)
                result=dict(version=VERSION,task_id=identifier,request_id=request,role=evidence['role'],
                    source_request_id=source_request,job_id=evidence['job_id'],scheduler_state=evidence['scheduler_state'],
                    input_sha256=sha256(canonical(evidence)),source_identity=evidence['files'],
                    proposal=proposal,citations=citations,scientific_adapter=context['scientific_adapter'],
                    plan_sha256=numeric['plan_sha256'],numeric_results=mixed.display_results(numeric),figures=plot_metadata,
                    files=[dict(name=name,size=len(data),sha256=sha256(data)) for name,data in sorted(artifacts.items())],
                    scientific_status='not_evaluated',new_physical_evaluation=False,
                    limitations=proposal['limitations']+numeric['limitations'])
                if mixed.is_mixed(plan):result.update(analysis_mode=mixed.MODE,
                    native_numeric_results=numeric['results'],
                    structural_results=numeric['structural_results'],operation_sources=numeric['operation_sources'],
                    analysis_sources=numeric['sources'],parsed_numeric_row_count=numeric['parsed_numeric_row_count'])
                data=canonical(result)
                self.raw.ledger.reserve_raw_export(source_request,sha256(canonical(dict(request=request,input_sha256=result['input_sha256']))),
                    sum(len(content) for content in artifacts.values())+len(data)+262144)
                folder=private_directory(self.directory/request)
                for name,content in artifacts.items():
                    _publish_bytes(folder/name,content)
                _publish_bytes(folder/'result.json',data)
            except (ValueError,TypeError,KeyError,OSError,LedgerError):
                return self._execution_failure(identifier,request,'模型完整答复已保留；本地分析、绘图或存储未完成。请恢复原执行，不新增模型调用。')
            return self._finish(identifier,request,'completed',receipt=receipt,proposal=proposal,result_sha256=sha256(data),
                message='原始数据分析与图表已保存；计算范围及科学验收未因此改变。')

    def download(self,identifier,request,name):
        task_id(request)
        record=self.get(identifier,request)
        if not record or record['state']!='completed':raise TaskError('分析来源或结果尚不可核验。')
        item=next((f for f in record['result']['files'] if f['name']==name),None)
        if item is None:raise TaskError('文件不属于这次分析。')
        data=read_regular(self.directory/request/name,item['size'],private=True)
        if len(data)!=item['size'] or sha256(data)!=item['sha256']:raise TaskError('最终下载字节与已核验产物不符。')
        return data
