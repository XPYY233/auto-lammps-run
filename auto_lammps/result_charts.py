"""Bounded user-requested charts from already verified numeric results.

The model may choose one declared source and two axes. It never supplies data,
code, a fitting window, a physical simulation or a scientific verdict.
"""
import json
import os
from datetime import datetime, timezone

from .analysis import MAX_TABLE_BYTES
from .manifest import canonical, private_directory, sha256
from .runtime_launcher import read_regular
from .scientific_adapters import ScientificAdapterError, prepare_stage_messages
from .tasks import TaskError, task_id


class ChartSourceChanged(ValueError):
    pass


class ResultCharts:
    def __init__(self, tasks, connections, reader):
        self.tasks, self.connections, self.reader = tasks, connections, reader
        self.directory=private_directory(tasks.path.parent/'result-charts')
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS result_chart_intents '
                       '(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, provider TEXT NOT NULL, '
                       'model TEXT NOT NULL, question TEXT NOT NULL, context_sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS result_chart_receipts '
                       '(id TEXT PRIMARY KEY REFERENCES result_chart_intents(id), state TEXT NOT NULL, '
                       'document TEXT NOT NULL, usage TEXT NOT NULL)')
            for table in ('result_chart_intents','result_chart_receipts'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable chart history'); END")

    def _options(self, identifier):
        options=[]
        for evaluation in self.reader.task(identifier)['evaluations']:
            for request in evaluation['requests']:
                for report in request['reports']:
                    if report['status']!='analyzed':continue
                    preview=self.reader.tables(identifier,report['id'])
                    declared={source['file']:source for source in report['sources'] if source.get('format') is None}
                    for table in preview['tables']:
                        if table['file'] not in declared:continue
                        options.append(dict(analysis_id=report['id'],file=table['file'],
                            source_sha256=table['sha256'],columns=table['columns'],rows=table['total_rows'],
                            examples=table['rows'][:2]))
        if len(options)>32:raise TaskError('结果表超过自动选图上限，请先在已有结果中自选图表。')
        return options

    def history(self, identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            rows=db.execute('SELECT i.*,r.state,r.document,r.usage FROM result_chart_intents i '
                            'LEFT JOIN result_chart_receipts r ON r.id=i.id '
                            'WHERE i.task_id=? ORDER BY i.at,i.id',(identifier,)).fetchall()
        items=[]
        for row in rows:
            chart=json.loads(row['document']) if row['document'] else None
            state=row['state'] or 'unknown'
            if state=='completed' and chart:
                try:
                    data=self.reader.chart_data(identifier,chart['analysis_id'],chart['file'],chart['x'],chart['y'])
                    saved=read_regular(self.directory/(row['id']+'.csv'),MAX_TABLE_BYTES,private=True)
                    if (data['csv_sha256']!=chart['csv_sha256'] or data['source_sha256']!=chart['source_sha256']
                            or saved!=data['csv']):
                        raise ValueError('Chart receipt differs from source')
                except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
                    state,chart='source_unavailable',None
            items.append(dict(id=row['id'],task_id=identifier,provider=row['provider'],model=row['model'],
                question=row['question'],at=row['at'],state=state,chart=chart,
                usage=json.loads(row['usage']) if row['usage'] else None))
        return items

    def download(self, identifier, request_id):
        task_id(request_id)
        item=next((item for item in self.history(identifier) if item['id']==request_id),None)
        if item is None or item['state']!='completed':raise TaskError('绘图数据或来源暂不可核验。')
        chart=item['chart']
        raw=read_regular(self.directory/(request_id+'.csv'),MAX_TABLE_BYTES,private=True)
        if sha256(raw)!=chart['csv_sha256'] or len(raw)!=chart['csv_size']:
            raise TaskError('绘图数据摘要不符，未提供下载。')
        return raw,chart

    def _finish(self, request_id, state, document, usage):
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO result_chart_receipts VALUES (?,?,?,?)',
                       (request_id,state,canonical(document).decode(),canonical(usage).decode()))

    def create(self, identifier, request_id, provider, question):
        task_id(request_id);self.tasks.get(identifier)
        if provider!='deepseek-official':raise TaskError('自动绘图目前使用已连接的 DeepSeek 模型。')
        if not isinstance(question,str) or not 1<=len(question.strip())<=1000:
            raise TaskError('请用 1000 字以内描述希望绘制的结果图。')
        with self.tasks.transaction() as db:
            prior=db.execute('SELECT * FROM result_chart_intents WHERE id=?',(request_id,)).fetchone()
            if prior and (prior['task_id']!=identifier or prior['question']!=question or prior['provider']!=provider):
                raise TaskError('请求标识已用于其他内容。')
        if prior:return next(item for item in self.history(identifier) if item['id']==request_id)
        if not self.connections.available(provider):
            raise TaskError('请先在模型设置中保存 DeepSeek 密钥和模型 ID。')
        value=self.connections._read(provider)
        options=self._options(identifier)
        if not options:raise TaskError('尚无可核验的数值表；计算结果保存后才能让 AI 选图。')
        evidence=dict(task_id=identifier,question=question,verified_tables=options)
        messages=[{'role':'system','content':(
            'You select ONE chart from verified numeric table metadata for a researcher. '
            'Return only a JSON object with exactly analysis_id, file, x, y selected from the supplied options. '
            'For requests that cannot be answered by one existing two-axis chart, return exactly '
            '{"unavailable":"brief reason"}. Never invent data, fit, change a scientific report or claim success. JSON only.')},
            {'role':'user','content':canonical(evidence).decode()}]
        try:messages,proof=prepare_stage_messages('result_chart',messages,evidence)
        except ScientificAdapterError as error:raise TaskError(str(error)) from None
        payload=dict(model=value['model'],messages=messages,stream=False,max_tokens=512,
                     thinking={'type':'disabled'},response_format={'type':'json_object'})
        if len(canonical(payload))>262144:raise TaskError('结果表信息过多，未发送模型请求。')
        with self.tasks.transaction() as db:
            concurrent=db.execute('SELECT * FROM result_chart_intents WHERE id=?',(request_id,)).fetchone()
            if concurrent:
                if (concurrent['task_id']!=identifier or concurrent['question']!=question
                        or concurrent['provider']!=provider):raise TaskError('请求标识已用于其他内容。')
            else:
                count=db.execute('SELECT count(*) FROM result_chart_intents WHERE task_id=?',(identifier,)).fetchone()[0]
                if count>=16:raise TaskError('本任务已保存 16 次自动选图请求，请使用已有图表或自选坐标。')
                db.execute('INSERT INTO result_chart_intents VALUES (?,?,?,?,?,?,?)',
                           (request_id,identifier,provider,value['model'],question,
                            sha256(canonical(evidence)),datetime.now(timezone.utc).isoformat()))
        if concurrent:return next(item for item in self.history(identifier) if item['id']==request_id)
        reserved=sent=response_received=False;usage={};state='not_sent';document={'message':'请求尚未发送。'}
        try:
            if self.connections.calls is not None:
                self.connections.calls.reserve(request_id,canonical(payload));reserved=True
            sent=True
            response=self.connections.transport(provider,value['key'],'POST','/chat/completions',payload)
            response_received=True
            content=response['choices'][0]['message']['content']
            usage={key:number for key,number in response.get('usage',{}).items()
                   if key in {'prompt_tokens','completion_tokens','total_tokens'} and type(number) is int and number>=0}
            if not isinstance(content,str) or len(content)>5000:raise ValueError('Invalid chart selection')
            selection=json.loads(content)
            if isinstance(selection,dict) and set(selection)=={'unavailable'}:
                reason=selection['unavailable']
                if not isinstance(reason,str) or not 1<=len(reason)<=200:raise ValueError('Invalid reason')
                state,document='unavailable',{'message':reason}
            else:
                if not isinstance(selection,dict) or set(selection)!={'analysis_id','file','x','y'}:
                    raise ValueError('Invalid chart selection shape')
                matches=[option for option in options if all(selection[key]==option[key] for key in ('analysis_id','file'))]
                if len(matches)!=1 or selection['x']==selection['y'] or not all(
                    selection[key] in [column['name'] for column in matches[0]['columns']] for key in ('x','y')):
                    raise ValueError('Chart source or axes not declared')
                try:data=self.reader.chart_data(identifier,selection['analysis_id'],selection['file'],selection['x'],selection['y'])
                except (ValueError,KeyError,TypeError,OSError,RuntimeError) as error:
                    raise ChartSourceChanged('Verified chart source changed') from error
                if data['source_sha256']!=matches[0]['source_sha256']:
                    raise ChartSourceChanged('Source changed after model selection')
                raw=data.pop('csv')
                path=self.directory/(request_id+'.csv')
                try:
                    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as output:
                        output.write(raw);output.flush();os.fsync(output.fileno())
                except FileExistsError:
                    if read_regular(path,MAX_TABLE_BYTES,private=True)!=raw:raise ValueError('Chart file differs')
                state,document='completed',data
        except ChartSourceChanged:
            state,document='source_unavailable',{'message':'来源数据在模型选图后已变化，未生成图。请求已保留。'}
        except (ValueError,KeyError,TypeError):
            state='invalid_output' if response_received else ('failed_or_unknown' if sent else 'not_sent')
            document={'message':('AI 选择的表或坐标不在已核验数据中，未生成图。请求已保留。'
                                 if response_received else '模型请求未完成；没有自动重发。请核对连接和额度。')}
        except Exception:
            state='failed_or_unknown' if sent else 'not_sent'
            document={'message':'模型或来源核验未完成；没有自动重发。请查看请求记录。' if sent
                      else '模型请求未发送；请检查连接和额度。'}
        if reserved:
            self.connections.calls.record(request_id,dict(provider=provider,requested_model=value['model'],
                state=state,request_sha256=sha256(canonical(payload)),usage=usage,purpose='result_chart',
                transport_attempts=1 if sent else 0,scientific_adapter={**proof,
                    'output_check':'trusted_numeric_source_and_distinct_axes' if state=='completed' else state}))
        self._finish(request_id,state,document,usage)
        return next(item for item in self.history(identifier) if item['id']==request_id)
