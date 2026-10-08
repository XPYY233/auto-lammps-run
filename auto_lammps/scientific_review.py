"""Immutable, task-bound evidence inventory for ordinary research results.

This checks what the application can prove from saved receipts. It deliberately
does not turn a numerical report or model explanation into scientific success.
"""
import json
import re
from datetime import datetime, timezone

from .manifest import canonical, sha256
from .results import ResultUnavailable
from .tasks import TaskError, task_id


VERSION = 1


class ScientificReview:
    def __init__(self, tasks, reader):
        if tasks.path != reader.tasks.path:
            raise ValueError('Scientific review must share the task store')
        self.tasks, self.reader = tasks, reader
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scientific_reviews '
                       '(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, analysis_id TEXT NOT NULL, '
                       'evidence_sha256 TEXT NOT NULL, at TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS scientific_reviews_task ON scientific_reviews(task_id,at)')
            for action in ('UPDATE', 'DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_scientific_reviews_{action} '
                           f'BEFORE {action} ON scientific_reviews '
                           "BEGIN SELECT RAISE(ABORT, 'immutable scientific review'); END")

    def _evidence(self, identifier, analysis_id):
        task_id(identifier)
        if not isinstance(analysis_id, str) or not re.fullmatch(r'[a-f0-9]{64}', analysis_id):
            raise TaskError('分析报告标识无效。')
        task = self.tasks.get(identifier)
        if task['status'] != 'conditions_frozen':
            raise TaskError('请先冻结研究条件；未确认的需求不能进行结果核验。')
        groups = self.reader.task(identifier)['evaluations']
        matches = [(request, report) for group in groups for request in group['requests']
                   for report in request['reports'] if report.get('id') == analysis_id]
        if len(matches) != 1 or matches[0][1].get('status') != 'analyzed':
            raise ResultUnavailable('这项任务没有已完成且可核验的数值分析。')
        request, report = matches[0]
        if request['state'] != 'completed' or not request['accounted']:
            raise ResultUnavailable('计算终态或资源核算尚未确认。')
        # Both calls recheck the saved analysis, collection receipt and task binding.
        report = self.reader.report(identifier, analysis_id)
        tables = self.reader.tables(identifier, analysis_id)['tables']
        sources = [{'file': table['file'], 'sha256': table['sha256'],
                    'rows': table['total_rows'], 'columns': table['columns']}
                   for table in tables]
        results = [{'id': item['id'], 'method': item['method'], 'file': item['file'],
                    'sample_count': item['sample_count'], 'values': item['values'],
                    'value_units': item['value_units']}
                   for item in report.get('results', [])]
        evidence = {'task_id': identifier, 'task_record_sha256': task['record_sha256'],
                    'analysis_id': analysis_id, 'report_sha256': sha256(canonical(report)),
                    'job_id': request['job_id'], 'source_tables': sources,
                    'numerical_results': results,
                    'structural_result_count': len(report.get('structural_results', [])),
                    'site_result_count': len(report.get('site_thermodynamic_results', []))}
        if not (sources or evidence['structural_result_count'] or evidence['site_result_count']):
            raise ResultUnavailable('分析报告没有可核对的数据来源。')
        return evidence

    def _present(self, row):
        saved = json.loads(row['document'])
        try:
            current = self._evidence(row['task_id'], row['analysis_id'])
            if sha256(canonical(current)) != row['evidence_sha256']:
                raise ResultUnavailable('Review evidence changed')
        except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError):
            return {'id': row['id'], 'task_id': row['task_id'], 'analysis_id': row['analysis_id'],
                    'at': row['at'], 'state': 'source_unavailable',
                    'message': '计算报告或原始来源已变化，旧核验记录保留，但不展示旧数值。'}
        return saved

    def history(self, identifier):
        self.tasks.get(identifier)
        with self.tasks.transaction() as db:
            rows = db.execute('SELECT * FROM scientific_reviews WHERE task_id=? ORDER BY at,id',
                              (identifier,)).fetchall()
        return [self._present(row) for row in rows]

    def create(self, identifier, analysis_id):
        evidence = self._evidence(identifier, analysis_id)
        proof = sha256(canonical({'version': VERSION, 'evidence': evidence}))
        review_id = proof[:32]
        at = datetime.now(timezone.utc).isoformat()
        document = {'id': review_id, 'task_id': identifier, 'analysis_id': analysis_id,
                    'at': at, 'state': 'criteria_missing',
                    'summary': '计算、来源和已完成的数值分析可追溯；尚无事前冻结的机器可判定科学验收标准，不能宣称研究目标通过。',
                    'checks': [
                        {'id': 'task', 'state': 'verified', 'label': '研究条件已冻结',
                         'evidence': evidence['task_record_sha256']},
                        {'id': 'execution', 'state': 'verified', 'label': '作业结束且资源已核算',
                         'evidence': evidence['job_id']},
                        {'id': 'sources', 'state': 'verified', 'label': '已保存报告和数据来源通过完整性核对',
                         'evidence': evidence['analysis_id']},
                        {'id': 'scientific_criteria', 'state': 'missing',
                         'label': '缺少事前冻结的机器可判定科学通过条件',
                         'evidence': None}],
                    'evidence': evidence, 'evidence_sha256': sha256(canonical(evidence)),
                    'scientific_status': 'not_evaluated',
                    'next_step': '可让应用内 AI 对照已有数据指出方法、收敛和证据缺口；若需补充物理计算，回到正常方案与 HPC 审批。AI 答复不能批准科学结论。'}
        with self.tasks.transaction() as db:
            prior = db.execute('SELECT * FROM scientific_reviews WHERE id=?', (review_id,)).fetchone()
            if prior is None:
                db.execute('INSERT INTO scientific_reviews VALUES (?,?,?,?,?,?)',
                           (review_id, identifier, analysis_id, document['evidence_sha256'], at,
                            canonical(document).decode('utf-8')))
            elif prior['task_id'] != identifier or prior['analysis_id'] != analysis_id or prior['evidence_sha256'] != document['evidence_sha256']:
                raise TaskError('核验记录身份冲突；未覆盖旧记录。')
        return next(item for item in self.history(identifier) if item['id'] == review_id)

    def download(self, identifier, review_id):
        task_id(review_id)
        item = next((item for item in self.history(identifier) if item['id'] == review_id), None)
        if item is None or item['state'] == 'source_unavailable':
            raise ResultUnavailable('核验记录或原始来源暂不可读。')
        return canonical(item)
