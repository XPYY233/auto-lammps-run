"""Static dependencies of byte-proven author text; never imports author modules.

Distribution metadata is evidence about installed metadata, not executable or
scientific approval. Unknown imports and incomplete source stay explicit.
"""
import ast
from copy import deepcopy
from pathlib import PurePosixPath
import re

from .manifest import canonical, sha256
from .runtime_launcher import hash_value, relative
from .tasks import TaskError, task_id

FLAGS=dict(scope='reference_only',B_input=False,execution_authorized=False,
    scientific_validation=False,child_process_policy_verified=False,author_entrypoints_reviewed=False)
# A conservative, version-independent subset, not the developer interpreter's
# stdlib list (which could contain modules absent from an older remote Python).
STDLIB=set(('__future__ abc argparse array ast base64 bisect builtins calendar cmath collections '
    'contextlib copy csv ctypes dataclasses datetime decimal enum errno fnmatch fractions functools '
    'glob gzip hashlib heapq hmac importlib inspect io itertools json logging math multiprocessing '
    'operator os pathlib pickle platform pprint queue random re resource select selectors shlex shutil '
    'signal socket sqlite3 ssl stat statistics string struct subprocess sys tempfile textwrap threading '
    'time timeit traceback types typing unittest urllib uuid warnings weakref zipfile zlib').split())
DISTRIBUTIONS={name:name for name in ('numpy','pandas','scipy','matplotlib','ase','ovito','torch','tensorflow','seaborn','requests')}
DISTRIBUTIONS.update(mpl_toolkits='matplotlib',sklearn='scikit-learn',PIL='pillow',yaml='pyyaml')


def requirements_from_sources(checked,identifier):
    """Only complete, contiguous SHA-proven files contribute import names."""
    from .author_reference_plan import author_source_text_coverage
    task_id(identifier);coverage=author_source_text_coverage(checked)
    sources={row['id']:row['text'] for row in checked['sources']}
    complete={row['path']:row for row in coverage['complete_files']}
    files={row['path']:row for row in checked['files']}
    texts={path:''.join(sources[fragment['source_id']] for fragment in row['fragments'])
           for path,row in complete.items()}
    parsed={};failures={}
    for path,txt in texts.items():
        if PurePosixPath(path).suffix.lower()!='.py':continue
        try:parsed[path]=ast.parse(txt)
        except (SyntaxError,ValueError):failures[path]='python_source_parse_unverified'
    def imports(path,seen):
        if path in seen:return set(),set(),set(),[],False
        seen=seen|{path};names=set();distributions=set();local=set();missing=[]
        if path not in parsed:return names,distributions,local,[failures.get(path,'complete_python_source_missing')+': '+path],True
        aliases={};child=False
        for node in ast.walk(parsed[path]):
            if isinstance(node,ast.Import):
                for alias in node.names:aliases[alias.asname or alias.name.split('.')[0]]=alias.name if alias.asname else alias.name.split('.')[0]
            elif isinstance(node,ast.ImportFrom) and node.module:
                for alias in node.names:aliases[alias.asname or alias.name]=node.module+'.'+alias.name
        def target(node):
            if isinstance(node,ast.Name):return aliases.get(node.id,node.id)
            if isinstance(node,ast.Attribute):return target(node.value)+'.'+node.attr
            return ''
        for node in ast.walk(parsed[path]):
            if isinstance(node,ast.Call):
                called=target(node.func)
                if called in {'__import__','exec','eval','compile','builtins.__import__','builtins.exec','builtins.eval',
                        'importlib.import_module','getattr','globals','locals','vars'}:
                    missing.append('dynamic_import_or_code_requires_separate_evidence: '+path+':'+str(node.lineno));child=True
                if (called.startswith(('subprocess.','multiprocessing.','socket.','urllib.','requests.','http.','ftplib.','paramiko.'))
                        or called in {'os.system','os.popen','os.fork','os.forkpty'} or called.startswith(('os.spawn','os.exec'))):child=True
            if not isinstance(node,(ast.Import,ast.ImportFrom)):continue
            modules=([alias.name for alias in node.names] if isinstance(node,ast.Import)
                     else ([node.module] if node.module else [alias.name for alias in node.names]))
            for name in modules:
                top=name.split('.')[0];names.add(top)
                if top in {'subprocess','multiprocessing','socket','urllib','requests','http','ftplib','paramiko',
                        'importlib','ctypes','runpy','marshal'}:child=True
                level=getattr(node,'level',0);parent=PurePosixPath(path).parent
                if level:
                    for _ in range(level-1):parent=parent.parent
                candidates=[str(parent/PurePosixPath(*name.split('.')))+'.py',str(parent/PurePosixPath(*name.split('.'))/'__init__.py')]
                match=next((p for p in candidates if p in files),None)
                if match:
                    local.add(match);child_names,child_dist,child_local,child_missing,child_review=imports(match,seen)
                    names|=child_names;distributions|=child_dist;local|=child_local;missing+=child_missing
                    child=child or child_review
                elif level:missing.append('local_import_source_missing: '+name)
                elif top in DISTRIBUTIONS:distributions.add(DISTRIBUTIONS[top])
                elif top not in STDLIB:missing.append('import_distribution_mapping_unverified: '+top)
        return names,distributions,local,list(dict.fromkeys(missing)),child
    entries=[];union=set()
    for path,file in files.items():
        if PurePosixPath(path).suffix.lower()!='.py':continue
        names,dist,local,missing,child=imports(path,set());union|=dist
        entries.append(dict(path=path,sha256=file['sha256'],imports=sorted(names),required_distributions=sorted(dist),
            local_files=[dict(path=p,sha256=files[p]['sha256']) for p in sorted(local)],missing=missing,
            parse_status='parsed' if path in parsed else 'invalid' if path in failures else 'incomplete',
            source_requires_child_review=child))
    result=dict(version=1,**FLAGS,task_id=identifier,source_sha256=sha256(canonical(checked['capability'])),
        entries=entries,required_distributions=sorted(union))
    return validate_requirements(result,identifier)


def validate_requirements(value,identifier=None):
    try:
        if (not isinstance(value,dict) or set(value)!={'version',*FLAGS,'task_id','source_sha256','entries','required_distributions'}
                or value['version']!=1 or any(type(value[k])is not type(v) or value[k]!=v for k,v in FLAGS.items())):raise ValueError()
        task_id(value['task_id']);hash_value(value['source_sha256'])
        if identifier is not None and value['task_id']!=identifier:raise ValueError()
        if not isinstance(value['entries'],list) or len(value['entries'])>128:raise ValueError()
        known=set();union=set()
        def strings(rows):
            if (not isinstance(rows,list) or len(rows)>128 or any(not isinstance(x,str) or not 1<=len(x)<=2000 for x in rows)
                    or len(rows)!=len(set(rows))):raise ValueError()
        for entry in value['entries']:
            if not isinstance(entry,dict) or set(entry)!={'path','sha256','imports','required_distributions','local_files','missing','parse_status','source_requires_child_review'}:raise ValueError()
            if entry['parse_status'] not in {'parsed','invalid','incomplete'} or type(entry['source_requires_child_review'])is not bool:raise ValueError()
            relative(entry['path']);hash_value(entry['sha256'])
            if entry['path'] in known:raise ValueError()
            known.add(entry['path'])
            for k in ('imports','required_distributions','missing'):strings(entry[k])
            for name in entry['required_distributions']:
                if not re.fullmatch('[a-z0-9][a-z0-9-]{0,99}',name):raise ValueError()
            union.update(entry['required_distributions'])
            if not isinstance(entry['local_files'],list) or len(entry['local_files'])>128:raise ValueError()
            for row in entry['local_files']:
                if set(row)!={'path','sha256'}:raise ValueError()
                relative(row['path']);hash_value(row['sha256'])
        if len(union)>32 or value['required_distributions']!=sorted(union) or len(canonical(value))>256*1024:raise ValueError()
        return deepcopy(value)
    except (ValueError,KeyError,TypeError):
        raise TaskError('原Python依赖清单的任务、全文SHA、入口或容量未通过核验。') from None


def project_capabilities(requirements,report):
    """Separate binary/source/entry metadata from the unchanged coarse report."""
    requirements=validate_requirements(requirements)
    native_ready=bool(report and report.get('native_files') and all(row['status']=='verified' for row in report['native_files']))
    interpreters=[]
    for row in (report or {}).get('python_candidates',[]):
        binary=bool(native_ready and row['status'] in {'verified','dependencies_unverified'})
        installed={item['name']:item for item in row['distributions']}
        entries=[]
        for entry in requirements['entries']:
            missing=deepcopy(entry['missing'])
            if not binary:missing.append('python_binary_metadata_unverified')
            for name in entry['required_distributions']:
                state=installed.get(name,{}).get('state','not_requested')
                if state!='installed':missing.append('distribution_metadata_'+state+': '+name)
            entries.append(dict(entry,missing=missing,state='metadata_verified' if not missing else 'missing'))
        interpreters.append(dict(id=row['id'],path=row['path'],real_path=row['real_path'],sha256=row['sha256'],
            raw_status=row['status'],binary_verified=binary,distributions=deepcopy(row['distributions']),entries=entries))
    return dict(version=1,**FLAGS,task_id=requirements['task_id'],source_sha256=requirements['source_sha256'],
        requirements_sha256=sha256(canonical(requirements)),environment_report_sha256=sha256(canonical(report)),
        base_native_ready=native_ready and any(row['binary_verified'] for row in interpreters),
        required_distributions=requirements['required_distributions'],interpreters=interpreters)


def validate_capabilities(value):
    """Bounded metadata-only envelope accepted by the author-plan Adapter."""
    try:
        fields={'version',*FLAGS,'task_id','source_sha256','requirements_sha256','environment_report_sha256',
            'base_native_ready','required_distributions','interpreters'}
        if (not isinstance(value,dict) or set(value)!=fields or value['version']!=1
                or any(type(value[k])is not type(v) or value[k]!=v for k,v in FLAGS.items())
                or type(value['base_native_ready'])is not bool or len(canonical(value))>512*1024):raise ValueError()
        task_id(value['task_id'])
        for key in ('source_sha256','requirements_sha256','environment_report_sha256'):hash_value(value[key])
        if (not isinstance(value['required_distributions'],list) or len(value['required_distributions'])>32
                or len(value['required_distributions'])!=len(set(value['required_distributions']))
                or any(not isinstance(name,str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,99}',name) for name in value['required_distributions'])):raise ValueError()
        if not isinstance(value['interpreters'],list) or len(value['interpreters'])>8:raise ValueError()
        for row in value['interpreters']:
            if (set(row)!={'id','path','real_path','sha256','raw_status','binary_verified','distributions','entries'}
                    or type(row['binary_verified'])is not bool or row['raw_status'] not in {'verified','dependencies_unverified','unverified'}
                    or not isinstance(row['entries'],list) or len(row['entries'])>128):raise ValueError()
            entry_union=set()
            for entry in row['entries']:
                if (set(entry)!={'path','sha256','imports','required_distributions','local_files','missing','state','parse_status','source_requires_child_review'}
                        or entry['state'] not in {'metadata_verified','missing'}
                        or entry['parse_status'] not in {'parsed','incomplete','invalid'}
                        or type(entry['source_requires_child_review'])is not bool
                        or (entry['state']=='metadata_verified')!= (not entry['missing'])):raise ValueError()
                relative(entry['path']);hash_value(entry['sha256'])
                for key in ('imports','required_distributions','missing'):
                    if not isinstance(entry[key],list) or len(entry[key])>256 or any(not isinstance(item,str) or not 1<=len(item)<=2000 for item in entry[key]):raise ValueError()
                entry_union.update(entry['required_distributions'])
            if sorted(entry_union)!=value['required_distributions']:raise ValueError()
        return deepcopy(value)
    except (ValueError,KeyError,TypeError):raise TaskError('逐入口Python依赖元数据投影未通过核验。') from None
