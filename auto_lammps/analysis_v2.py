"""Versioned collected-output analysis, preserving frozen v1 implementations.

Native scalar and legacy labeled tables can share a plan. No runtime patching,
model code execution, post-hoc windows or scientific acceptance is performed.
"""
import json
from pathlib import Path

from . import analysis as legacy
from . import scalar_analysis as scalar
from . import coordination_analysis as coordination
from . import site_thermodynamics as site
from . import runtime_launcher as runtime
from .manifest import canonical, sha256
from .slurm_read import _write_new

AnalysisError = legacy.AnalysisError
MAX_TABLE_BYTES = legacy.MAX_TABLE_BYTES
MAX_TABLES = legacy.MAX_TABLES
MAX_V3_SOURCES = 29
VERSION = 3
SITE_VERSION = 4


def adapter_identity():
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                numeric_tables=legacy.adapter_identity(), native_scalar=scalar.adapter_identity(),
                structural_statistics=coordination.adapter_identity())


def site_adapter_identity():
    """Distinct version: existing frozen identities are never relabeled as v4.

    Legacy v1 dispatch stays unchanged. Frozen v2/v3 source mismatches still
    require their original deployment; this is an explicit upgrade for new plans.
    """
    return dict(adapter_version=SITE_VERSION, mixed_tables=adapter_identity(),
                site_thermodynamics=site.adapter_identity())


def validate_plan(plan, files):
    if not isinstance(plan,dict) or set(plan) != {'tables','operations'}:
        raise AnalysisError('Explicit analysis tables and operations are required')
    if not isinstance(plan['tables'],list):
        raise AnalysisError('Declare explicit analysis sources')
    is_structural=any(isinstance(t,dict) and t.get('format')==coordination.FORMAT for t in plan['tables'])
    is_site=any(isinstance(t,dict) and t.get('format')==site.FORMAT for t in plan['tables'])
    limit=MAX_V3_SOURCES if is_structural or is_site else MAX_TABLES
    if not 1 <= len(plan['tables']) <= limit:
        raise AnalysisError('Declare at most sixteen numeric tables or twenty-nine combined numeric/trajectory sources')
    tables=[]
    structural={}
    sites={}
    declared=set()
    for table in plan['tables']:
        if isinstance(table,dict) and table.get('format') == coordination.FORMAT:
            coordination.validate_table(table)
            structural[table['file']]=table
        elif isinstance(table,dict) and table.get('format') == site.FORMAT:
            site.validate_table(table)
            sites[table['file']]=table
        elif isinstance(table,dict) and 'format' in table:
            scalar.validate_table(table)
            tables.append(scalar.numeric_table(table))
        else:
            tables.append(table)
        if (not isinstance(table,dict) or not isinstance(table.get('file'),str)
                or table['file'] not in files or table['file'] in declared):
            raise AnalysisError('Analysis must use a distinct declared output file')
        declared.add(table['file'])
    if not isinstance(plan['operations'],list) or not 1 <= len(plan['operations']) <= legacy.MAX_OPERATIONS:
        raise AnalysisError('Declare one to thirty-two analysis operations')
    operations=[];ids=set();used_structural=set();used_sites=set()
    for operation in plan['operations']:
        if not isinstance(operation,dict):
            raise AnalysisError('Explicit analysis operation is required')
        if not isinstance(operation.get('file'),str):
            raise AnalysisError('Analysis operations require a declared output basename')
        identifier=legacy._name(operation.get('id'))
        if identifier in ids:
            raise AnalysisError('Duplicate analysis operation')
        ids.add(identifier)
        if operation.get('method') == site.METHOD:
            table=sites.get(operation.get('file'))
            if table is None:
                raise AnalysisError('Site thermodynamics requires a declared complete array source')
            site.validate_operation(operation,table)
            used_sites.add(table['file'])
        elif operation.get('method') == coordination.METHOD:
            table=structural.get(operation.get('file'))
            if table is None:
                raise AnalysisError('Warren-Cowley requires a declared LAMMPS dump source')
            coordination.validate_operation(operation,table)
            used_structural.add(table['file'])
        else:
            if operation.get('file') in structural or operation.get('file') in sites:
                raise AnalysisError('Numeric-table operations cannot read a trajectory as a scalar table')
            operations.append(operation)
    if set(structural) != used_structural:
        raise AnalysisError('Each structural source requires an explicit frozen structural operation')
    if set(sites) != used_sites:
        raise AnalysisError('Each complete site-array source requires a frozen thermodynamics operation')
    if site.reservation_bytes(plan)>site.MAX_DERIVED_BYTES:
        raise AnalysisError('Complete derived-table reservation exceeds the version-four storage limit')
    if len(tables)>MAX_TABLES or len(structural)>MAX_TABLES:
        raise AnalysisError('Version-three analysis supports at most sixteen numeric and sixteen trajectory sources')
    if tables:
        if not operations:
            raise AnalysisError('Numeric tables require an explicit numeric operation')
        legacy.validate_plan(dict(tables=tables,operations=operations),files)
    elif operations:
        raise AnalysisError('Numeric operations require declared numeric tables')
    return plan


def plan_adapter(plan):
    """Called by the trusted freezer, never selected by model authority."""
    if any(table.get('format') == site.FORMAT for table in plan['tables']):
        return 'numeric_tables_v4', site_adapter_identity()
    if any(table.get('format') == coordination.FORMAT for table in plan['tables']):
        return 'numeric_tables_v3', adapter_identity()
    if any('format' in table for table in plan['tables']):
        return 'numeric_tables_v2', adapter_identity()
    return 'numeric_tables_v1', legacy.adapter_identity()


def analyze_collected(snapshot, collection, *, derived_directory=None):
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
    status=spec.get('implementation_status')
    if status not in {'numeric_tables_v2','numeric_tables_v3','numeric_tables_v4'} or 'plan' not in spec.get('proposal',{}):
        raise AnalysisError('This candidate has no executable frozen analysis plan')
    implementation=site_adapter_identity() if status=='numeric_tables_v4' else adapter_identity()
    if spec.get('adapter_identity')!=implementation:
        raise AnalysisError('Analysis implementation differs from the frozen plan')
    plan=validate_plan(spec['proposal']['plan'],spec['proposal']['files'])
    inventory={item['path']:item for item in header['files']}
    from .outputs import verify_payload
    payload=Path(collection['directory'])/'payload'
    verify_payload(payload,header,context)
    provenance=[]
    results=[];structural_results=[];site_results=[]
    is_structural=status in {'numeric_tables_v3','numeric_tables_v4'}
    total,cells=0,0
    for table in plan['tables']:
        name='output/'+table['file']
        if name not in inventory:
            raise AnalysisError('Required analysis table was not collected')
        item=inventory[name]
        if table.get('format') == site.FORMAT:
            if status!='numeric_tables_v4' or derived_directory is None:
                raise AnalysisError('Complete site arrays require a reserved version-four derived directory')
            if item['size']>site.MAX_ARRAY_BYTES:
                raise AnalysisError('Complete site array exceeds the explicit byte limit')
            data=runtime.read_regular(payload/name,item['size'])
            if sha256(data)!=item['sha256']:
                raise AnalysisError('Site array changed after collection')
            source=dict(table,sha256=item['sha256'],size=item['size'])
            provenance.append(source)
            for op in plan['operations']:
                if op['file']==table['file']:
                    site_results.append(site.analyze(data,table,op,source,derived_directory))
            del data
            continue
        if table.get('format') == coordination.FORMAT:
            if status not in {'numeric_tables_v3','numeric_tables_v4'}:
                raise AnalysisError('Structural sources require the frozen version-three adapter')
            if item['size'] > coordination.MAX_TRAJECTORY_BYTES:
                raise AnalysisError('Structural trajectory exceeds the explicit byte limit')
            provenance.append(dict(table,sha256=item['sha256'],size=item['size']))
            for op in plan['operations']:
                if op['file']==table['file']:
                    structural_results.append(coordination.analyze_isolated(
                        payload/'output'/table['file'],table,op,item))
            continue
        total+=item['size']
        if (not is_structural and total>MAX_TABLE_BYTES) or item['size']>MAX_TABLE_BYTES:
            raise AnalysisError('Aggregate analysis table limit exceeded')
        data=runtime.read_regular(payload/name,item['size'])
        if sha256(data)!=item['sha256']:
            raise AnalysisError('Analysis table changed after collection')
        rows=(scalar.parse_scalar(data,table) if 'format' in table else legacy.parse_table(data,table))
        file_cells=len(rows)*len(table['columns'])
        cells+=file_cells
        if file_cells>500000 or (not is_structural and cells>500000):
            raise AnalysisError('Analysis cell limit exceeded')
        source=dict(file=table['file'],sha256=item['sha256'],size=item['size'],columns=table['columns'])
        if 'format' in table:
            source.update(format=table['format'],headers=table['headers'],steps=table['steps'])
        provenance.append(source)
        for op in plan['operations']:
            if op['file']!=table['file']:
                continue
            result=legacy.calculate(rows,table,op)
            if 'format' in table:
                result['units_origin']='declared_only_not_present_in_scalar_header'
            results.append(result)
        del rows,data
    # Preserve the originally declared operation order for existing consumers.
    positions={op['id']:index for index,op in enumerate(plan['operations'])}
    results.sort(key=lambda result:positions[result['id']])
    structural_results.sort(key=lambda result:positions[result['id']])
    site_results.sort(key=lambda result:positions[result['id']])
    report=dict(schema_version=1,adapter_version=SITE_VERSION if status=='numeric_tables_v4' else VERSION,status='analyzed',scientific_status='not_evaluated',
                adapter_identity=implementation,
                manifest_sha256=snapshot.digest,analysis_sha256=sha256(raw),sources=provenance,results=results,
                declared_quantity=spec['proposal']['quantity'],declared_method=spec['proposal']['method'],
                limitations=['Native scalar units are declarations only; labeled-table units match headers, not an independent physical verification.',
                             'Frame scatter is descriptive; independent-replicate uncertainty and fit confidence intervals are not estimated.',
                             'No reference answers, scientific thresholds or automatic fit-window selection are used.'])
    if structural_results:
        report['structural_results']=structural_results
    if site_results:
        report['site_thermodynamic_results']=site_results
    if len(canonical(report))>60000:
        raise AnalysisError('Analysis report exceeds retained artifact limit')
    return report


class VersionedAnalysisService(legacy.AnalysisService):
    @staticmethod
    def identity():
        return site_adapter_identity()

    def _run(self, request_id, snapshot):
        snapshot.verify()
        spec=json.loads(runtime.read_regular(snapshot.path/'analysis.json',65536))
        if spec.get('implementation_status') not in {'numeric_tables_v2','numeric_tables_v3','numeric_tables_v4'}:
            return super()._run(request_id,snapshot)
        collection=self.collector.fetch(request_id)
        if collection['state']!='collected':
            raise AnalysisError('Outputs must be collected before analysis')
        collection_sha256=sha256(canonical({k:v for k,v in collection.items() if k!='directory'}))
        is_site=spec['implementation_status']=='numeric_tables_v4'
        implementation=site_adapter_identity() if is_site else adapter_identity()
        adapter_sha256=sha256(canonical(implementation))
        extra={}
        if is_site:
            plan=validate_plan(spec['proposal']['plan'],spec['proposal']['files'])
            extra['storage_bytes']=site.reservation_bytes(plan)
        context=self.collector.ledger.begin_output_analysis(request_id, snapshot.digest,
            collection_sha256, adapter_sha256, sha256(str(self.directory).encode()),**extra)
        path=self.directory/(context['analysis_id']+'.json')
        if path.exists():
            raw=runtime.read_regular(path,65536,private=True)
            saved=json.loads(raw)
            if saved['context']!=context:
                raise AnalysisError('Analysis report identity mismatch')
            for result in saved.get('report',{}).get('site_thermodynamic_results',[]):
                for receipt in result['derived_files']:
                    site.read_derived(self.directory/context['analysis_id'],receipt)
            self.collector.ledger.finish_output_analysis(request_id,context['analysis_id'],sha256(raw))
            return saved
        try:
            report=analyze_collected(snapshot,collection,
                derived_directory=self.directory/context['analysis_id'] if is_site else None)
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
