"""Bounded user-requested charts from already verified numeric results.

The model may choose one declared source and two axes. It never supplies data,
code, a fitting window, a physical simulation or a scientific verdict.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .analysis import MAX_TABLE_BYTES
from .deepseek import response_diagnostics
from .manifest import canonical, private_directory, sha256
from .model_connections import MODEL_ID
from .runtime_launcher import read_regular
from .scientific_adapters import ScientificAdapterError, prepare_stage_messages
from .tasks import TaskError, task_id


class ChartSourceChanged(ValueError):
    pass


class ChartSelectionInvalid(ValueError):
    """Trusted diagnostic code, never upstream exception text."""


def _non_json_constant(value):
    raise ChartSelectionInvalid('chart_output_json_invalid')


def chart_selection_context(options):
    """Small stage-specific menu rebuilt from verified sources, not model IDs."""
    menu = []
    if not isinstance(options, list) or not 1 <= len(options) <= 32:
        raise ScientificAdapterError('Verified chart source menu is missing')
    for number, source in enumerate(options):
        columns = source.get('columns') if isinstance(source, dict) else None
        if (not isinstance(columns, list) or len(columns) < 2
                or any(not isinstance(column, dict) or not isinstance(column.get('name'), str)
                    or not column['name'] or not isinstance(column.get('unit'), str) for column in columns)):
            raise ScientificAdapterError('Verified chart column contract is missing')
        categories = source.get('filter_categories', [])
        if not isinstance(categories, list) or len(categories) > 20:
            raise ScientificAdapterError('Verified chart category contract is missing')
        seen = set()
        for category in categories:
            if (not isinstance(category, dict) or set(category) != {'name', 'values'}
                    or not isinstance(category['name'], str) or not category['name']
                    or category['name'] in seen or not isinstance(category['values'], list)
                    or not 1 <= len(category['values']) <= 128
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 100
                           for value in category['values'])
                    or len(set(category['values'])) != len(category['values'])):
                raise ScientificAdapterError('Verified chart category values are missing')
            seen.add(category['name'])
        menu.append({'table_index': number, 'analysis_id': source['analysis_id'], 'file': source['file'],
            'axes': [{'index': index, **column} for index, column in enumerate(columns)],
            'category_filters': [{'column_index': index, 'name': category['name'],
                'values': [{'value_index': value_index, 'value': value}
                           for value_index, value in enumerate(category['values'])]}
                for index, category in enumerate(categories)]})
    return {'tool': 'verified_numeric_chart_selection', 'version': 2,
        'source_sha256': sha256(Path(__file__).read_bytes()), 'menu': menu,
        'output_schema': {'table_index': 'integer menu index', 'x_index': 'integer axis index',
            'y_index': 'different integer axis index',
            'filters': 'optional 1..4 objects {column_index: integer, value_index: integer}; distinct columns; exact AND'},
        'execution': 'trusted_source_revalidation_and_full_two_column_CSV',
        'scientific_status': 'not_evaluated', 'new_physics_or_fit': False}


def _selection(selection, options):
    if isinstance(selection, dict) and set(selection) in (
            {'table_index', 'x_index', 'y_index'}, {'table_index', 'x_index', 'y_index', 'filters'}):
        values = [selection[key] for key in ('table_index', 'x_index', 'y_index')]
        if any(type(value) is not int for value in values):
            raise ChartSelectionInvalid('chart_selection_index_invalid')
        table, x, y = values
        if not 0 <= table < len(options):
            raise ChartSelectionInvalid('chart_source_not_declared')
        source = options[table]
        if x == y or any(not 0 <= axis < len(source['columns']) for axis in (x, y)):
            raise ChartSelectionInvalid('chart_axes_not_declared')
        axes = dict(analysis_id=source['analysis_id'], file=source['file'],
                    x=source['columns'][x]['name'], y=source['columns'][y]['name'])
        if 'filters' in selection:
            filters = selection['filters']
            categories = source.get('filter_categories', [])
            if not isinstance(filters, list) or not 1 <= len(filters) <= 4:
                raise ChartSelectionInvalid('chart_filter_not_declared')
            seen, resolved = set(), []
            for item in filters:
                if (not isinstance(item, dict) or set(item) != {'column_index', 'value_index'}
                        or type(item['column_index']) is not int or type(item['value_index']) is not int
                        or not 0 <= item['column_index'] < len(categories)
                        or item['column_index'] in seen):
                    raise ChartSelectionInvalid('chart_filter_not_declared')
                category = categories[item['column_index']]
                if not 0 <= item['value_index'] < len(category['values']):
                    raise ChartSelectionInvalid('chart_filter_not_declared')
                seen.add(item['column_index'])
                resolved.append(dict(column=category['name'], value=category['values'][item['value_index']]))
            axes['filters'] = resolved
        return axes
    if not isinstance(selection, dict) or set(selection) != {'analysis_id', 'file', 'x', 'y'}:
        raise ChartSelectionInvalid('chart_output_shape_invalid')
    return selection


class ResultCharts:
    def __init__(self, tasks, connections, reader, *, source_providers=(), channel="ordinary"):
        self.tasks, self.connections, self.reader = tasks, connections, reader
        self.source_providers = tuple(source_providers)
        if channel not in {'ordinary', 'author_reference'}:
            raise ValueError('Unsupported chart channel')
        self.intent_table = 'result_chart_intents' if channel == 'ordinary' else 'reference_chart_intents'
        self.receipt_table = 'result_chart_receipts' if channel == 'ordinary' else 'reference_chart_receipts'
        self.directory=private_directory(tasks.path.parent/('result-charts' if channel == 'ordinary' else 'reference-charts'))
        with tasks.transaction() as db:
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.intent_table} '
                       '(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, provider TEXT NOT NULL, '
                       'model TEXT NOT NULL, question TEXT NOT NULL, context_sha256 TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.receipt_table} '
                       f'(id TEXT PRIMARY KEY REFERENCES {self.intent_table}(id), state TEXT NOT NULL, '
                       'document TEXT NOT NULL, usage TEXT NOT NULL)')
            for table in (self.intent_table, self.receipt_table):
                for action in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable chart history'); END")

    def _options(self, identifier):
        options=[]
        evaluations = self.reader.task(identifier)['evaluations'] if self.reader is not None else []
        for evaluation in evaluations:
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
        for provider in self.source_providers:
            sources = provider.options(identifier)
            if any(not provider.owns(source['analysis_id']) for source in sources):
                raise TaskError('图表来源适配器不能接管其他来源。')
            options.extend(sources)
        identities = [(source['analysis_id'], source['file']) for source in options]
        if len(set(identities)) != len(identities):
            raise TaskError('图表来源身份冲突，未发送模型请求。')
        if len(options)>32:raise TaskError('结果表超过自动选图上限，请先在已有结果中自选图表。')
        return options

    def _chart_data(self, identifier, analysis_id, name, x, y, filters=None):
        owners = [provider for provider in self.source_providers if provider.owns(analysis_id)]
        if len(owners) > 1:
            raise TaskError('图表来源适配器身份冲突。')
        if owners:
            if filters is not None:
                return owners[0].chart_data(identifier, analysis_id, name, x, y, filters=filters)
            return owners[0].chart_data(identifier, analysis_id, name, x, y)
        if filters is not None:
            raise TaskError('此结果来源未声明分类筛选能力。')
        if self.reader is None:
            raise TaskError('没有登记此图表来源。')
        return self.reader.chart_data(identifier, analysis_id, name, x, y)

    def history(self, identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            rows=db.execute(f'SELECT i.*,r.state,r.document,r.usage FROM {self.intent_table} i '
                            f'LEFT JOIN {self.receipt_table} r ON r.id=i.id '
                            'WHERE i.task_id=? ORDER BY i.at,i.id',(identifier,)).fetchall()
        items=[]
        for row in rows:
            chart=json.loads(row['document']) if row['document'] else None
            state=row['state'] or 'unknown'
            if state=='completed' and chart:
                try:
                    data=self._chart_data(identifier,chart['analysis_id'],chart['file'],chart['x'],chart['y'],chart.get('filters'))
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
            db.execute(f'INSERT INTO {self.receipt_table} VALUES (?,?,?,?)',
                       (request_id,state,canonical(document).decode(),canonical(usage).decode()))

    def create(self, identifier, request_id, provider, question):
        task_id(request_id);self.tasks.get(identifier)
        if provider!='deepseek-official':raise TaskError('自动绘图目前使用已连接的 DeepSeek 模型。')
        if not isinstance(question,str) or not 1<=len(question.strip())<=1000:
            raise TaskError('请用 1000 字以内描述希望绘制的结果图。')
        with self.tasks.transaction() as db:
            prior=db.execute(f'SELECT * FROM {self.intent_table} WHERE id=?',(request_id,)).fetchone()
            if prior and (prior['task_id']!=identifier or prior['question']!=question or prior['provider']!=provider):
                raise TaskError('请求标识已用于其他内容。')
        if prior:return next(item for item in self.history(identifier) if item['id']==request_id)
        if not self.connections.assistant_enabled:
            raise TaskError('结果助手尚未启用；可先下载数据或保存模型连接。')
        value=self.connections._read(provider)
        if not value or not MODEL_ID.fullmatch(value.get('model', '')):
            raise TaskError('请先在模型设置中保存 DeepSeek 密钥和模型 ID。')
        if self.connections.calls is None:
            raise TaskError('模型请求账本尚未配置，未发送选图请求。')
        options=self._options(identifier)
        if not options:raise TaskError('尚无可核验的数值表；计算结果保存后才能让 AI 选图。')
        evidence=dict(task_id=identifier,question=question,verified_tables=options)
        messages=[{'role':'system','content':(
            'You select ONE chart from verified numeric table metadata for a researcher. '
            'Return only JSON {"table_index":0,"x_index":0,"y_index":1}, using integer indices from the '
            'scientific_adapter verified numeric chart menu. The two axis indices must be different. '
            'When a requested category or element pair is a declared category filter, add "filters":'
            '[{"column_index":0,"value_index":0}] using only the chosen table menu. This is a shape example, '
            'not a selection; use the actual category values. Filters are exact AND, distinct columns, at most four. '
            'Omit filters when no subset was requested. Never use a text category as a numeric axis. '
            'This is an output SHAPE example, not a recommendation to choose table 0 or these axes. '
            'Never copy or guess hashes, filenames or column names. '
            'For requests that cannot be answered by one existing two-axis chart, return exactly '
            '{"unavailable":"brief reason"}. Never invent data, fit, change a scientific report or claim success. JSON only.')},
            {'role':'user','content':canonical(evidence).decode()}]
        try:messages,proof=prepare_stage_messages('result_chart',messages,evidence)
        except ScientificAdapterError as error:raise TaskError(str(error)) from None
        payload=dict(model=value['model'],messages=messages,stream=False,max_tokens=512,
                     thinking={'type':'disabled'},response_format={'type':'json_object'})
        if len(canonical(payload))>262144:raise TaskError('结果表信息过多，未发送模型请求。')
        with self.tasks.transaction() as db:
            concurrent=db.execute(f'SELECT * FROM {self.intent_table} WHERE id=?',(request_id,)).fetchone()
            if concurrent:
                if (concurrent['task_id']!=identifier or concurrent['question']!=question
                        or concurrent['provider']!=provider):raise TaskError('请求标识已用于其他内容。')
            else:
                count=db.execute(f'SELECT count(*) FROM {self.intent_table} WHERE task_id=?',(identifier,)).fetchone()[0]
                if count>=16:raise TaskError('本任务已保存 16 次自动选图请求，请使用已有图表或自选坐标。')
                db.execute(f'INSERT INTO {self.intent_table} VALUES (?,?,?,?,?,?,?)',
                           (request_id,identifier,provider,value['model'],question,
                            sha256(canonical(evidence)),datetime.now(timezone.utc).isoformat()))
        if concurrent:return next(item for item in self.history(identifier) if item['id']==request_id)
        reserved=sent=response_received=False;usage={};state='not_sent';document={'message':'请求尚未发送。'}
        diagnostics={}; selection=None; response_sha256=None
        try:
            if self.connections.calls is not None:
                self.connections.calls.reserve(request_id,canonical(payload));reserved=True
            sent=True
            response=self.connections.transport(provider,value['key'],'POST','/chat/completions',payload)
            response_received=True
            raw_usage = response.get('usage', {}) if isinstance(response, dict) else {}
            usage={key:number for key,number in (raw_usage.items() if isinstance(raw_usage,dict) else ())
                   if key in {'prompt_tokens','completion_tokens','total_tokens'} and type(number) is int and number>=0}
            response_sha256=sha256(canonical(response))
            diagnostics=response_diagnostics(canonical(response))
            content=response['choices'][0]['message']['content']
            if diagnostics.get('finish_reason') not in (None, 'stop'):
                raise ChartSelectionInvalid('chart_incomplete_generation')
            if not isinstance(content,str) or len(content)>5000:raise ValueError('Invalid chart selection')
            selection=json.loads(content,parse_constant=_non_json_constant)
            if isinstance(selection,dict) and set(selection)=={'unavailable'}:
                reason=selection['unavailable']
                if not isinstance(reason,str) or not 1<=len(reason)<=200:raise ValueError('Invalid reason')
                state,document='unavailable',{'message':reason.replace(value['key'], '[已隐藏]')}
            else:
                axes=_selection(selection,options)
                matches=[option for option in options if all(axes[key]==option[key] for key in ('analysis_id','file'))]
                if len(matches)!=1:
                    raise ChartSelectionInvalid('chart_source_not_declared')
                if axes['x']==axes['y'] or not all(
                    axes[key] in [column['name'] for column in matches[0]['columns']] for key in ('x','y')):
                    raise ChartSelectionInvalid('chart_axes_not_declared')
                try:data=self._chart_data(identifier,axes['analysis_id'],axes['file'],axes['x'],axes['y'],axes.get('filters'))
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
        except ChartSelectionInvalid as error:
            state='invalid_output'
            document={'error_code': str(error), 'message': '模型返回的表或坐标选择未通过核验，未生成图；原请求已保留。'}
        except (ValueError,KeyError,TypeError,IndexError):
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
                response_sha256=response_sha256, **diagnostics,
                structured_output=(json.loads(canonical(selection).decode().replace(value['key'], '[已隐藏]'))
                    if selection is not None else None), error_code=document.get('error_code'),
                transport_attempts=1 if sent else 0,scientific_adapter={**proof,
                    'output_check':'trusted_numeric_source_and_distinct_axes' if state=='completed' else state}))
        self._finish(request_id,state,document,usage)
        return next(item for item in self.history(identifier) if item['id']==request_id)
