"""Versioned collected-output analysis, preserving frozen v1 implementations.

Native scalar and legacy labeled tables can share a plan. No runtime patching,
model code execution, post-hoc windows or scientific acceptance is performed.
"""
import json
from pathlib import Path

from . import analysis as legacy
from . import scalar_analysis as scalar
from . import runtime_launcher as runtime
from .manifest import canonical, sha256
from .slurm_read import _write_new

AnalysisError = legacy.AnalysisError
MAX_TABLE_BYTES = legacy.MAX_TABLE_BYTES
MAX_TABLES = legacy.MAX_TABLES
VERSION = 2


def adapter_identity():
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                numeric_tables=legacy.adapter_identity(), native_scalar=scalar.adapter_identity())


def validate_plan(plan, files):
    if not isinstance(plan,dict) or set(plan) != {'tables','operations'}:
        raise AnalysisError('Explicit analysis tables and operations are required')
    if not isinstance(plan['tables'],list) or not 1 <= len(plan['tables']) <= MAX_TABLES:
        raise AnalysisError('Declare one to sixteen analysis tables')
    tables=[]
    for table in plan['tables']:
        if isinstance(table,dict) and 'format' in table:
            scalar.validate_table(table)
            tables.append(scalar.numeric_table(table))
        else:
            tables.append(table)
    legacy.validate_plan(dict(tables=tables,operations=plan['operations']),files)
    return plan


def plan_adapter(plan):
    """Called by the trusted freezer, never selected by model authority."""
    if any('format' in table for table in plan['tables']):
        return 'numeric_tables_v2', adapter_identity()
    return 'numeric_tables_v1', legacy.adapter_identity()


def analyze_collected(snapshot, collection):
    """Trusted adapter core; service obtains collection through OutputCollector."""
    manifest=snapshot.verify()
    context=collection['context']
    if context['manifest_sha256'] != snapshot.digest or collection['state'] != 'collected':
        raise AnalysisError('Analysis input does not match collected outputs')
    header=collection['header']
    if (context['scheduler_state'] != 'completed' or header['execution'] != {
            'state':'finished','returncode':0,'timed_out':False} or header['missing_outputs']):
        raise AnalysisError('A complete successful execution is required for numeric analysis')
    specs=[item for item in manifest['files'] if item['path']=='analysis.json' and item['role']=='analysis_spec']
    if len(specs)!=1 or specs[0]['sha256'] != manifest['provenance']['analysis_sha256']:
        raise AnalysisError('Frozen analysis identity is missing')
    raw=runtime.read_regular(snapshot.path/'analysis.json',65536)
    if sha256(raw)!=specs[0]['sha256']:
        raise AnalysisError('Frozen analysis changed')
    spec=json.loads(raw)
    if spec.get('implementation_status') != 'numeric_tables_v2' or 'plan' not in spec.get('proposal',{}):
        raise AnalysisError('This candidate has no executable frozen analysis plan')
    if spec.get('adapter_identity')!=adapter_identity():
        raise AnalysisError('Analysis implementation differs from the frozen plan')
    plan=validate_plan(spec['proposal']['plan'],spec['proposal']['files'])
    inventory={item['path']:item for item in header['files']}
    from .outputs import verify_payload
    payload=Path(collection['directory'])/'payload'
    verify_payload(payload,header,context)
    tables,provenance={},[]
    total,cells=0,0
    for table in plan['tables']:
        name='output/'+table['file']
        if name not in inventory:
            raise AnalysisError('Required analysis table was not collected')
        item=inventory[name]
        total+=item['size']
        if total>MAX_TABLE_BYTES:
            raise AnalysisError('Aggregate analysis table limit exceeded')
        data=runtime.read_regular(payload/name,item['size'])
        if sha256(data)!=item['sha256']:
            raise AnalysisError('Analysis table changed after collection')
        rows=(scalar.parse_scalar(data,table) if 'format' in table else legacy.parse_table(data,table))
        cells+=len(rows)*len(table['columns'])
        if cells>500000:
            raise AnalysisError('Analysis cell limit exceeded')
        tables[table['file']]=(table,rows)
        source=dict(file=table['file'],sha256=item['sha256'],size=item['size'],columns=table['columns'])
        if 'format' in table:
            source.update(format=table['format'],headers=table['headers'],steps=table['steps'])
        provenance.append(source)
    results=[]
    for op in plan['operations']:
        table,rows=tables[op['file']]
        result=legacy.calculate(rows,table,op)
        if 'format' in table:
            result['units_origin']='declared_only_not_present_in_scalar_header'
        results.append(result)
    report=dict(schema_version=1,adapter_version=VERSION,status='analyzed',scientific_status='not_evaluated',
                adapter_identity=adapter_identity(),
                manifest_sha256=snapshot.digest,analysis_sha256=sha256(raw),sources=provenance,results=results,
                declared_quantity=spec['proposal']['quantity'],declared_method=spec['proposal']['method'],
                limitations=['Native scalar units are declarations only; labeled-table units match headers, not an independent physical verification.',
                             'Frame scatter is descriptive; independent-replicate uncertainty and fit confidence intervals are not estimated.',
                             'No reference answers, scientific thresholds or automatic fit-window selection are used.'])
    if len(canonical(report))>60000:
        raise AnalysisError('Analysis report exceeds retained artifact limit')
    return report


class VersionedAnalysisService(legacy.AnalysisService):
    @staticmethod
    def identity():
        return adapter_identity()

    def _run(self, request_id, snapshot):
        snapshot.verify()
        spec=json.loads(runtime.read_regular(snapshot.path/'analysis.json',65536))
        if spec.get('implementation_status') != 'numeric_tables_v2':
            return super()._run(request_id,snapshot)
        collection=self.collector.fetch(request_id)
        if collection['state']!='collected':
            raise AnalysisError('Outputs must be collected before analysis')
        collection_sha256=sha256(canonical({k:v for k,v in collection.items() if k!='directory'}))
        adapter_sha256=sha256(canonical(adapter_identity()))
        context=self.collector.ledger.begin_output_analysis(request_id, snapshot.digest,
            collection_sha256, adapter_sha256, sha256(str(self.directory).encode()))
        path=self.directory/(context['analysis_id']+'.json')
        if path.exists():
            raw=runtime.read_regular(path,65536,private=True)
            saved=json.loads(raw)
            if saved['context']!=context:
                raise AnalysisError('Analysis report identity mismatch')
            self.collector.ledger.finish_output_analysis(request_id,context['analysis_id'],sha256(raw))
            return saved
        try:
            report=analyze_collected(snapshot,collection)
        except (ValueError,KeyError,TypeError,OSError,runtime.ExecutionDenied) as exc:
            report=dict(status='analysis_failed',scientific_status='not_evaluated',error_type=type(exc).__name__,
                        reason=str(exc) if isinstance(exc,AnalysisError) else 'Analysis data or filesystem validation failed')
        value=dict(context=context,report=report)
        if len(canonical(value))>65536:
            raise AnalysisError('Analysis artifact exceeds storage reservation')
        try:
            proof=_write_new(path,value)
        except FileExistsError:
            # Another caller published the same deterministic analysis identity.
            raw=runtime.read_regular(path,65536,private=True)
            if json.loads(raw)!=value:
                raise AnalysisError('Concurrent analysis result differs')
            proof=sha256(raw)
        self.collector.ledger.finish_output_analysis(request_id,context['analysis_id'],proof)
        return value
