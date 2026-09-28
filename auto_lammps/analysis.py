"""Frozen numeric-table analysis; no engine, model code or answer-based fitting."""
import json
import fcntl
import io
import math
import os
from pathlib import Path
import re
import statistics
import stat
import sys

from .manifest import canonical, private_directory, sha256
from . import runtime_launcher as runtime
from .slurm_read import _write_new

VERSION = 1
UNITS = {'1','step','K','bar','atm','Pa','MPa','GPa','eV','kcal/mol',
         'angstrom','angstrom^2','nm','nm^2','ps','fs','g/cm^3'}
MAX_TABLE_BYTES = 8*1024*1024
MAX_ROWS = 100000


def adapter_identity():
    return dict(source_sha256=sha256(Path(__file__).read_bytes()),python=sys.version,
                implementation=sys.implementation.name,adapter_version=VERSION)


class AnalysisError(ValueError):
    pass


def _name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',value):
        raise AnalysisError('Invalid analysis identifier')
    return value


def _finite(value):
    try:valid=type(value) in (int,float) and math.isfinite(value)
    except OverflowError:valid=False
    if not valid:
        raise AnalysisError('Analysis requires finite numeric values')
    return value


def validate_plan(plan, files):
    if not isinstance(plan,dict) or set(plan) != {'tables','operations'}:
        raise AnalysisError('Explicit analysis tables and operations are required')
    tables,operations=plan['tables'],plan['operations']
    if not isinstance(tables,list) or not 1 <= len(tables) <= 16:
        raise AnalysisError('Declare one to sixteen analysis tables')
    declared={}
    for table in tables:
        if not isinstance(table,dict) or set(table) != {'file','columns'}:
            raise AnalysisError('Invalid table declaration')
        name=table['file']
        if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name)
                or name not in files or name in declared):
            raise AnalysisError('Analysis must use a distinct declared output file')
        columns=table['columns']
        if not isinstance(columns,list) or not 2 <= len(columns) <= 16:
            raise AnalysisError('Declare two to sixteen labeled columns')
        names=set()
        for column in columns:
            if not isinstance(column,dict) or set(column) != {'name','unit'}:
                raise AnalysisError('Explicit column name and unit are required')
            _name(column['name'])
            if column['name'] in names or not isinstance(column['unit'],str) or column['unit'] not in UNITS:
                raise AnalysisError('Duplicate column or unsupported unit')
            names.add(column['name'])
        declared[name]=names
    if not isinstance(operations,list) or not 1 <= len(operations) <= 32:
        raise AnalysisError('Declare one to thirty-two analysis operations')
    ids=set()
    for operation in operations:
        if not isinstance(operation,dict) or set(operation) != {'id','method','file','x','y','window'}:
            raise AnalysisError('Invalid analysis operation')
        _name(operation['id'])
        if operation['id'] in ids or operation['method'] not in ('summary','last','linear_fit'):
            raise AnalysisError('Duplicate operation or unsupported analysis method')
        ids.add(operation['id'])
        if not isinstance(operation['file'],str) or operation['file'] not in declared:
            raise AnalysisError('Operation table is not declared')
        _name(operation['x']);_name(operation['y'])
        if operation['x'] == operation['y'] or not {operation['x'],operation['y']} <= declared[operation['file']]:
            raise AnalysisError('Operation columns are not distinct declared columns')
        window=operation['window']
        if not isinstance(window,list) or len(window) != 2 or _finite(window[0]) > _finite(window[1]):
            raise AnalysisError('Freeze an inclusive analysis window before execution')
    return plan


def parse_table(data, table):
    """Exactly two labeled header lines, then finite whitespace-delimited rows."""
    if len(data) > MAX_TABLE_BYTES:
        raise AnalysisError('Table exceeds analysis limit')
    try:
        text=data.decode('ascii')
    except UnicodeDecodeError as exc:
        raise AnalysisError('Numeric tables must be ASCII') from exc
    expected=['# columns: '+' '.join(c['name'] for c in table['columns']),
              '# units: '+' '.join(c['unit'] for c in table['columns'])]
    rows=[];headers=0
    for line_no,line in enumerate(io.StringIO(text),1):
        line=line.strip()
        if not line:continue
        if headers<2:
            if line!=expected[headers]:
                raise AnalysisError('Table labels or units differ from the frozen plan')
            headers+=1
            continue
        if len(rows)>=MAX_ROWS or (len(rows)+1)*len(table['columns'])>500000:
            raise AnalysisError('Table row or cell limit exceeded')
        if len(line) > 4096 or len(line.split()) != len(table['columns']):
            raise AnalysisError('Malformed numeric row')
        try:
            values=[float(token) for token in line.split()]
        except ValueError as exc:
            raise AnalysisError('Nonnumeric table value') from exc
        for value in values:_finite(value)
        rows.append((line_no,values))
    if headers!=2 or not rows:
        raise AnalysisError('Table is missing labels or data')
    return rows


def calculate(rows, table, operation):
    names=[c['name'] for c in table['columns']]
    xi,yi=names.index(operation['x']),names.index(operation['y'])
    low,high=operation['window']
    selected=[(line,values[xi],values[yi]) for line,values in rows if low <= values[xi] <= high]
    if not selected:
        raise AnalysisError('Frozen window contains no samples')
    x=[r[1] for r in selected]; y=[r[2] for r in selected]
    method=operation['method']
    try:
        if method == 'last':
            values={'value':y[-1],'x':x[-1]}
        elif method == 'summary':
            values={'mean':statistics.fmean(y),'sample_std':statistics.stdev(y) if len(y)>1 else None,
                    'min':min(y),'max':max(y)}
        else:
            if len(x)<3:
                raise AnalysisError('Linear fit requires at least three samples')
            fit=statistics.linear_regression(x,y)
            residual=math.fsum((v-(fit.slope*u+fit.intercept))**2 for u,v in zip(x,y))
            mean=statistics.fmean(y)
            variation=math.fsum((v-mean)**2 for v in y)
            values={'slope':fit.slope,'intercept':fit.intercept,
                    'rmse':math.sqrt(residual/len(y)), 'r_squared':1-residual/variation if variation else None}
        for value in values.values():
            if value is not None:_finite(value)
    except (OverflowError,statistics.StatisticsError,ZeroDivisionError) as exc:
        raise AnalysisError('Numerically undefined analysis') from exc
    # Compact contiguous source-line ranges preserve exact selection, not just
    # extrema. Reject excessive fragmentation rather than silently dropping it.
    ranges=[]
    for line,_,_ in selected:
        if ranges and line == ranges[-1][1]+1:ranges[-1][1]=line
        else:ranges.append([line,line])
    if len(ranges)>128:
        raise AnalysisError('Selected row provenance exceeds report limit')
    xu,yu=table['columns'][xi]['unit'],table['columns'][yi]['unit']
    value_units=({'value':yu,'x':xu} if method=='last' else
                 {'mean':yu,'sample_std':yu,'min':yu,'max':yu} if method=='summary' else
                 {'slope':yu if xu=='1' else yu+'/'+xu,'intercept':yu,'rmse':yu,'r_squared':'1'})
    return dict(id=operation['id'],method=method,file=table['file'],x=operation['x'],y=operation['y'],
                window=operation['window'],sample_count=len(selected),source_line_ranges=ranges,
                values=values,value_units=value_units,units_origin='declared_and_header_matched',
                independent_replicate_uncertainty='not_estimated')


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
    if spec.get('implementation_status') != 'numeric_tables_v1' or 'plan' not in spec.get('proposal',{}):
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
        rows=parse_table(data,table)
        cells+=len(rows)*len(table['columns'])
        if cells>500000:
            raise AnalysisError('Analysis cell limit exceeded')
        tables[table['file']]=(table,rows)
        provenance.append(dict(file=table['file'],sha256=item['sha256'],size=item['size'],columns=table['columns']))
    results=[calculate(tables[op['file']][1],tables[op['file']][0],op) for op in plan['operations']]
    report=dict(schema_version=1,adapter_version=VERSION,status='analyzed',scientific_status='not_evaluated',
                adapter_identity=adapter_identity(),
                manifest_sha256=snapshot.digest,analysis_sha256=sha256(raw),sources=provenance,results=results,
                declared_quantity=spec['proposal']['quantity'],declared_method=spec['proposal']['method'],
                limitations=['Declared units are checked against table labels, not independently against the physical workflow.',
                             'Frame scatter is descriptive; independent-replicate uncertainty and fit confidence intervals are not estimated.',
                             'No reference answers, scientific thresholds or automatic fit-window selection are used.'])
    if len(canonical(report))>60000:
        raise AnalysisError('Analysis report exceeds retained artifact limit')
    return report


class AnalysisService:
    def __init__(self, collector, directory):
        self.collector=collector
        self.directory=private_directory(directory)

    def run(self, request_id, snapshot):
        fd=os.open(self.directory/'.analysis.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink!=1:
                raise AnalysisError('Unsafe analysis lock')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise AnalysisError('Another analysis is running') from exc
            return self._run(request_id,snapshot)
        finally:os.close(fd)

    def _run(self, request_id, snapshot):
        snapshot.verify()
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
