"""Read-only, operator evidence for scoped user acceptance; never a score gate.

Reports are private controller artifacts. No browser publication, model context,
submission, retrospective plan freeze or evaluation mutation is provided here.
"""
import json
from pathlib import Path
import re

from . import runtime_launcher as runtime, scalar_analysis
from .manifest import sha256
from .results import existing_private_directory, ResultUnavailable
from .tasks import text


class CloseoutViews:
    def __init__(self, references, raw_outputs):
        self.references, self.outputs = references, raw_outputs

    def _load(self, identifier, reference=None):
        reference = reference or self.references.get(identifier)
        if reference is None:
            return None, None, None
        folder = self.references.directory / identifier / 'closeout'
        if not folder.exists():
            return None, None, None
        existing_private_directory(folder)
        raw = runtime.read_regular(folder / 'manifest.json', 262144, private=True)
        manifest = json.loads(raw)
        if (manifest['version'] != 1 or manifest['task_id'] != identifier
                or manifest['reference_report_sha256'] != reference['report_sha256']
                or manifest['doi'] != reference['doi']):
            raise ResultUnavailable('Closeout reference identity differs')
        files = manifest['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 32:
            raise ResultUnavailable('Invalid closeout files')
        data, total = {}, 0
        for item in files:
            name = item['name']
            if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', name)
                    or name in data or Path(name).suffix not in {'.json', '.png', '.csv', '.md', '.pdf'}):
                raise ResultUnavailable('Invalid closeout artifact')
            content = runtime.read_regular(folder / name, 5*1024*1024, private=True)
            total += len(content)
            if total > 32*1024*1024 or len(content) != item['size'] or sha256(content) != item['sha256']:
                raise ResultUnavailable('Closeout artifact changed')
            data[name] = content
        document = json.loads(data[manifest['evidence_file']])
        if (document['doi'] != reference['doi'] or document['title'] != reference['title']
                or document['scientific_status'] != 'diagnostic'
                or document['formal_blind'] is not False):
            raise ResultUnavailable('Closeout is not a formal verdict')
        acceptance = document['acceptance']
        if (acceptance['status'] != 'accepted_by_user' or acceptance['source'] != 'explicit_user_message'
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', acceptance['date'])
                or not data[acceptance['record_file']]):
            raise ResultUnavailable('Missing scoped user acceptance record')
        acceptance = {k: text(acceptance[k], 2000) for k in ('status', 'source', 'date', 'scope')}
        # Bind B to this task's actual agent ledger, not an arbitrary completed job.
        request = self.outputs.ledger.get(document['request_id'])
        bound = reference['agent_progress']['evaluations']
        matches = [e for e in bound if e.get('available') and e['id'] == request['evaluation']]
        if (len(matches) != 1 or matches[0]['identity']['role'] != 'agent'
                or request['state'] != 'completed' or not request['accounted']
                or any(document[k] != request[k] for k in ('job_id', 'manifest_sha256'))):
            raise ResultUnavailable('Closeout B identity is not completed and accounted')
        source = document['source']
        listed = [f for f in self.outputs.listing(identifier)['files'] if f['id'] == source['id']]
        if (len(listed) != 1 or listed[0]['request_id'] != request['id']
                or any(listed[0][k] != source[k] for k in ('name', 'size', 'sha256'))
                or source['size'] > scalar_analysis.analysis.MAX_TABLE_BYTES):
            raise ResultUnavailable('B source differs from accounted output')
        _, _, stream = self.outputs.download(identifier, source['id'])
        with stream:
            content = stream.read(scalar_analysis.analysis.MAX_TABLE_BYTES + 1)
        table, operations = document['table'], document['operations']
        if table['file'] != source['name']:
            raise ResultUnavailable('Analysis source name differs')
        analysis = scalar_analysis.analyze_scalar(content, table, operations)
        if analysis['plan_sha256'] != document['analysis_plan_sha256']:
            raise ResultUnavailable('Retained diagnostic plan differs')
        results = {r['id']: r for r in analysis['results']}
        metrics = []
        if not isinstance(document['metrics'], list) or not 1 <= len(document['metrics']) <= 32:
            raise ResultUnavailable('Invalid closeout metrics')
        for item in document['metrics']:
            index = item['reference_index']
            if type(index) is not int or not 0 <= index < len(reference['metrics']):
                raise ResultUnavailable('Invalid reference metric')
            a = reference['metrics'][index]
            b = results[item['operation_id']]
            key = 'max' if a['operation'] == 'peak' else 'slope'
            if ((key == 'max' and b['method'] != 'summary')
                    or (key == 'slope' and b['method'] != 'linear_fit')
                    or b['value_units'][key] != a['unit']
                    or b['x'] != document['curve']['x'] or b['y'] != document['curve']['y']
                    or (key == 'slope' and b['window'] != [0, float(a['window_end'])])):
                raise ResultUnavailable('Comparison operation or unit differs')
            # Both method descriptions remain visible; matching numbers alone is not a score.
            p, av, bv = a['paper'], a['reference'], b['values'][key]
            metrics.append(dict(label=a['label'], unit=a['unit'], method=a['method'],
                                P=p, A=av, B=bv, absolute_PA=abs(p-av),
                                absolute_AB=abs(av-bv), absolute_PB=abs(p-bv),
                                B_window=b['window']))
        curve = document['curve']
        names = [c['name'] for c in table['columns']]
        xi, yi = names.index(curve['x']), names.index(curve['y'])
        rows = scalar_analysis.parse_scalar(content, table)
        if table['columns'][xi]['unit'] != '1' or table['columns'][yi]['unit'] != 'GPa':
            raise ResultUnavailable('Unsupported stress curve units')
        index = curve['reference_index']
        if type(index) is not int or not 0 <= index < len(reference['curves']):
            raise ResultUnavailable('Invalid reference curve')
        curves = [dict(label='作者参考 A', points=reference['curves'][index]['points']),
                  dict(label='独立生成 B', points=[[v[xi], v[yi]] for _, v in rows])]
        coverage = document['coverage']
        if not isinstance(coverage, list) or len(coverage) > 256:
            raise ResultUnavailable('Invalid target coverage')
        coverage = [{k: text(row[k], 4000) for k in ('target', 'content', 'evidence', 'status', 'additional_work')}
                    for row in coverage]
        if 'coverage.json' in data:
            exported = json.loads(data['coverage.json'])
            if (exported.get('doi') != document['doi']
                    or exported.get('paper_title') != document['title']
                    or exported.get('coverage') != document['coverage']):
                raise ResultUnavailable('Displayed and downloadable coverage differ')
        retained = self.outputs.listing(identifier)['files']
        for item in document.get('postprocessing', []):
            receipt = json.loads(data[item['receipt_file']])
            derived_source = receipt['plan']['source']
            derived_matches = [f for f in retained if f['id'] == derived_source['id']]
            if (len(derived_matches) != 1 or receipt['status'] != 'posthoc_diagnostic'
                    or receipt['physics_simulation'] is not False or receipt['scientific_pass'] is not None
                    or any(derived_matches[0][k] != derived_source[k] for k in ('request_id', 'name', 'size', 'sha256'))):
                raise ResultUnavailable('Postprocessing is not bound to retained output')
            # The controller verified raw bytes during analysis. The web view binds
            # that receipt to its accounted catalog, avoiding full trajectory rehash
            # per image request. This is not a fresh scientific verdict.
            for artifact in item['artifacts']:
                originals = [f for f in receipt['files'] if f['name'] == artifact['source_name']]
                content = data[artifact['name']]
                if (len(originals) != 1 or originals[0]['sha256'] != sha256(content)
                        or originals[0]['size'] != len(content)):
                    raise ResultUnavailable('Derived output differs from analysis receipt')
        figures = []
        for figure in document['figures']:
            if figure['name'] not in data or Path(figure['name']).suffix != '.png':
                raise ResultUnavailable('Missing paper figure')
            kind = figure.get('kind', 'plot')
            if kind not in {'plot', 'structure'}:
                raise ResultUnavailable('Invalid figure kind')
            figures.append(dict({k: text(figure[k], 2000) for k in ('name', 'label', 'caption')}, kind=kind))
        public = dict(acceptance=acceptance, scientific_status='diagnostic', formal_blind=False,
                      metrics=metrics, curves=curves, coverage=coverage, figures=figures,
                      limitations=[text(v, 4000) for v in document['limitations']],
                      files=[{k: f[k] for k in ('name', 'label', 'size')} for f in files],
                      job_id=request['job_id'], core_hours=request['actual_core_seconds']/3600,
                      dispatch_claims=matches[0]['dispatch_claims'],
                      manifest_sha256=sha256(raw), source_sha256=source['sha256'])
        return public, data, raw

    def get(self, identifier, reference=None):
        return self._load(identifier, reference)[0]

    def download(self, identifier, name):
        report, data, _ = self._load(identifier)
        if report is None or name not in data:
            raise ResultUnavailable('No declared closeout artifact')
        return data[name]
