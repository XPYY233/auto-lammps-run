"""Compile reference-only author recipes without rewriting or executing them.

This is a metadata boundary, not another submission service.  Original files stay
on the HPC; the ordinary controller must still bind an approval, reserve the
existing ledger, stage verified copies and use the reviewed runtime.  In
particular, a compiled package is not an agent candidate or an execution grant.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import PurePosixPath
import re

from .author_reference_plan import validate_author_plan
from .ledger import Resources
from .manifest import canonical, relative_name, sha256, validate_provenance
from .runtime_launcher import validate_author_argv, ExecutionDenied


VERSION = 1
_HASH = re.compile(r'[a-f0-9]{64}')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
_ROLE = 'author_reference'
_BLOCKERS = ('reference_manifest_and_verified_HPC_copy',
             'reference_allocation_runtime_and_output_collection',
             'ordinary_approval_accounted_dispatch_and_following')
_RESOURCE_KEYS = {'cores', 'wall_seconds', 'memory_bytes', 'storage_bytes'}


class AuthorExecutionError(ValueError):
    pass


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise AuthorExecutionError('A fixed SHA-256 identity is required')
    return value


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AuthorExecutionError('Invalid reference capability identity')
    return value


def _relative(value, *, directory=False):
    if directory and value == '.':
        return value
    try:
        return relative_name(value)
    except ValueError as exc:
        raise AuthorExecutionError('Only concrete bounded relative author paths are allowed') from exc


def _absolute(value):
    if (not isinstance(value, str) or not value.startswith('/') or value == '/'
            or len(value) > 2048 or str(PurePosixPath(value)) != value
            or '..' in PurePosixPath(value).parts
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise AuthorExecutionError('An explicit trusted HPC path is required')
    return value


def _resources(value):
    if isinstance(value, Resources):
        value = asdict(value)
    if not isinstance(value, dict) or set(value) != _RESOURCE_KEYS:
        raise AuthorExecutionError('Exact frozen resource bounds are required')
    try:
        Resources(**value)
    except (TypeError, ValueError) as exc:
        raise AuthorExecutionError('Invalid reference resource bounds') from exc
    # Reference relaxations never waive the single-allocation CPU-only envelope.
    if (value['cores'] > 32 or value['wall_seconds'] > 7 * 86400
            or value['memory_bytes'] > 8 * 1024**3):
        raise AuthorExecutionError('Reference allocation exceeds the approved CPU-only envelope')
    return deepcopy(value)


def _source_files(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise AuthorExecutionError('An explicit bounded HPC original-file inventory is required')
    result = {}
    keys = {'path', 'size', 'sha256', 'binding_sha256', 'source_path'}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != keys
                or type(row['size']) is not int or row['size'] < 0):
            raise AuthorExecutionError('Invalid HPC original-file record')
        name = _relative(row['path']); _relative(row['source_path'])
        _hash(row['sha256']); _hash(row['binding_sha256'])
        if name in result or any(name.startswith(old + '/') or old.startswith(name + '/') for old in result):
            raise AuthorExecutionError('Duplicate or overlapping original-file paths')
        result[name] = deepcopy(row)
    return result


def _deployment(value, resources):
    # approval_sha256 identifies the administrator's deployment approval, not a
    # per-run scientific/user approval. The latter is separately signed by the
    # ordinary author authorization controller as user_plan_approval_sha256.
    keys = {'schema_version', 'role', 'profile_sha256', 'approval_sha256',
            'execution_mode', 'formal_isolation', 'gpu_allowed', 'resources',
            'source_roots', 'environments'}
    if (not isinstance(value, dict) or set(value) not in (keys, keys | {'execution_policy'})
            or type(value['schema_version']) is not int or value['schema_version'] != VERSION
            or value['role'] != _ROLE or value['gpu_allowed'] is not False
            or value['execution_mode'] != 'native_slurm' or value['formal_isolation'] is not False):
        raise AuthorExecutionError('An explicit trusted reference deployment is required')
    _hash(value['profile_sha256']); _hash(value['approval_sha256'])
    maximum = _resources(value['resources'])
    if any(resources[key] > maximum[key] for key in _RESOURCE_KEYS):
        raise AuthorExecutionError('Author resources exceed the registered deployment')
    roots = value['source_roots']
    if not isinstance(roots, list) or not 1 <= len(roots) <= 4:
        raise AuthorExecutionError('Pin one to four trusted HPC source roots')
    source_roots = {}
    for row in roots:
        if (not isinstance(row, dict) or set(row) != {'binding_sha256', 'path'}
                or _hash(row['binding_sha256']) in source_roots):
            raise AuthorExecutionError('Invalid or duplicate HPC source binding')
        source_roots[row['binding_sha256']] = _absolute(row['path'])
    environments = value['environments']
    if not isinstance(environments, list) or not 1 <= len(environments) <= 32:
        raise AuthorExecutionError('A registered interpreter/engine environment is required')
    registry = {}
    for row in environments:
        if (not isinstance(row, dict) or set(row) != {'id', 'sha256', 'executables'}
                or _identifier(row['id']) in registry):
            raise AuthorExecutionError('Invalid or duplicate environment mapping')
        _hash(row['sha256'])
        programs = row['executables']
        if not isinstance(programs, list) or not 1 <= len(programs) <= 32:
            raise AuthorExecutionError('A fixed executable mapping is required')
        seen = set()
        for program in programs:
            fields = {'id', 'sha256', 'kind', 'path', 'launch_mode', 'entrypoints',
                      'child_process_policy', 'review_sha256'}
            if (not isinstance(program, dict) or set(program) != fields
                    or _identifier(program['id']) in seen
                    or program['kind'] not in {'lammps', 'python', 'shell', 'author_binary'}
                    or program['launch_mode'] not in {'direct', 'mpi'}
                    or program['child_process_policy'] not in {'no_children', 'reviewed_same_allocation'}):
                raise AuthorExecutionError('Unknown or unreviewed author executable mapping')
            seen.add(program['id']); _hash(program['sha256']); _hash(program['review_sha256'])
            _absolute(program['path'])
            if program['kind'] in {'python', 'shell', 'author_binary'} and program['child_process_policy'] != 'reviewed_same_allocation':
                raise AuthorExecutionError('Author drivers require reviewed same-allocation child-process policy')
            entries = program['entrypoints']
            if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
                raise AuthorExecutionError('Register the exact author entrypoint bytes before approval')
            names = set()
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}
                        or _relative(entry['path']) in names):
                    raise AuthorExecutionError('Invalid registered author entrypoint')
                _hash(entry['sha256']); names.add(entry['path'])
        registry[row['id']] = deepcopy(row)
    from .runtime_launcher import author_execution_policy
    author_execution_policy(value)
    return source_roots, registry


def _raw_plan(report):
    """Recover the model's literal recipe, then reuse the existing source checks."""
    def citations(rows):
        return [{key: row[key] for key in ('source_id', 'quote')} for row in rows]

    try:
        result = dict(status=report['status'], summary=report['summary'],
            invocations=[dict(id=row['id'], entrypoint=deepcopy(row['entrypoint']),
                argv=deepcopy(row['argv']), working_directory=row['working_directory'],
                environment_id=row['environment']['id'], executable_id=row['executable']['id'],
                dependency_ids=deepcopy(row['dependency_ids']), required_files=deepcopy(row['required_files']),
                output_files=[dict(item, citations=citations(item['citations'])) for item in row['output_files']],
                citations=citations(row['citations']),
                missing=deepcopy(row['missing'])) for row in report['invocations']],
            coverage=[dict(dependency_id=row['dependency_id'], invocation_ids=deepcopy(row['invocation_ids']),
                analysis_capability_ids=[item['id'] for item in row['analysis_capabilities']],
                citations=citations(row['citations']), missing=deepcopy(row['missing'])) for row in report['coverage']],
            questions=deepcopy(report['questions']), limitations=deepcopy(report['limitations']))
        if 'author_validation' in report:
            validation = report['author_validation']
            result['validation_selection'] = dict(target_ids=deepcopy(validation['selected_ids']), states=[dict(
                id=row['id'], label=row['label'], citations=citations(row['citations']),
                invocation_ids=deepcopy(row['invocation_ids']), argument_bindings=deepcopy(row['argument_bindings']))
                for row in validation['states']])
        for raw,normalized in zip(result['coverage'],report['coverage']):
            if 'accounted_input_files' in normalized:raw['accounted_input_files']=deepcopy(normalized['accounted_input_files'])
        return result
    except (TypeError, KeyError) as exc:
        raise AuthorExecutionError('A complete previously validated author recipe is required') from exc


def compile_author_package(report, *, evidence, source_files, deployment, resources, provenance):
    """Bind an existing validated report to trusted HPC metadata; never dispatch.

    ``evidence`` is the reference service's source-verified frozen context, not
    browser/model-supplied deployment data.  ``source_files`` contains only HPC
    identities and relative copy targets.  No source byte is downloaded here.
    Registered executable paths are explicit, never inferred from model IDs.
    """
    try:
        checked = validate_author_plan(evidence, _raw_plan(report))
        if canonical(checked) != canonical(report):
            raise AuthorExecutionError('Author report differs from its source-validated recipe')
        if checked['status'] != 'proposed' or checked['questions'] or not checked['invocations']:
            raise AuthorExecutionError('Unresolved author recipe cannot become an execution package')
        resources = _resources(resources)
        validate_provenance(provenance)
        roots, registry = _deployment(deployment, resources)
        files = _source_files(source_files)
        known = {row['path']: row for row in evidence['author_source']['files']}
        accounted = {}
        policy = deployment.get('execution_policy', {})
        for receipt in policy.get('accounted_inputs', []):
            if receipt not in evidence.get('reference_inputs', []):
                raise AuthorExecutionError('Accounted reference inputs differ from the source-verified planner receipt')
            for row in receipt['files']:
                accounted[(receipt['binding_sha256'], row['path'])] = row
        for name, row in files.items():
            original = accounted.get((row['binding_sha256'], row['source_path']))
            if original is None:
                original = known.get(name)
                if row['binding_sha256'] in {receipt['binding_sha256'] for receipt in policy.get('accounted_inputs', [])}:
                    raise AuthorExecutionError('An accounted output cannot claim arbitrary original-source bytes')
            if (original is None or any(row[key] != original[key] for key in ('size', 'sha256'))
                    or row['binding_sha256'] not in roots):
                raise AuthorExecutionError('HPC original file is not bound to the validated author inventory')
        invocations, signatures, outputs = [], set(), {}
        for row in checked['invocations']:
            environment = registry.get(row['environment']['id'])
            if environment is None or environment['sha256'] != row['environment']['sha256']:
                raise AuthorExecutionError('Selected author environment has no trusted path mapping')
            program = next((item for item in environment['executables'] if item['id'] == row['executable']['id']), None)
            if program is None or any(program[key] != row['executable'][key] for key in ('sha256', 'kind')):
                raise AuthorExecutionError('Selected interpreter/engine differs from its registered bytes')
            if row['entrypoint'] not in program['entrypoints']:
                raise AuthorExecutionError('Original entrypoint has not been reviewed for this executable')
            cwd = _relative(row['working_directory'], directory=True)
            _relative(row['entrypoint']['path'])
            try:
                validate_author_argv(program['kind'], row['argv'], working_directory=cwd,
                    entrypoint=row['entrypoint']['path'])
            except ExecutionDenied as exc:
                raise AuthorExecutionError(str(exc)) from exc
            for required in row['required_files']:
                if required['path'] not in files or files[required['path']]['sha256'] != required['sha256']:
                    raise AuthorExecutionError('Required original-file bytes are absent from the HPC copy inventory')
            action = dict(id=row['id'], entrypoint=deepcopy(row['entrypoint']), argv=deepcopy(row['argv']),
                working_directory=cwd, environment_id=environment['id'], executable_id=program['id'],
                executable_sha256=program['sha256'], dependency_ids=deepcopy(row['dependency_ids']),
                required_files=deepcopy(row['required_files']), output_files=[])
            signature = sha256(canonical({key: action[key] for key in ('entrypoint', 'argv', 'working_directory', 'environment_id', 'executable_id')}))
            if signature in signatures:
                raise AuthorExecutionError('Duplicate original action must share one physical evaluation')
            signatures.add(signature)
            for output in row['output_files']:
                name = _relative(output['path'])
                path = _relative(str(PurePosixPath(cwd) / name))
                if any(path == item or path.startswith(item + '/') or item.startswith(path + '/') for item in files):
                    raise AuthorExecutionError('Declared author output collides with immutable original inputs')
                identity = dict(path=path, kind=output['kind'])
                # Shared outputs across distinct original actions are retained as
                # one collected byte identity, not mislabeled independent samples.
                if path in outputs and outputs[path]['kind'] != output['kind']:
                    raise AuthorExecutionError('Shared author output has conflicting declarations')
                outputs[path] = identity
                action['output_files'].append(identity)
            invocations.append(action)
        payload = dict(schema_version=VERSION, kind='original_author_execution', role=_ROLE,
            scope='reference_only', B_input=False, original_files_rewritten=False,
            plan_sha256=sha256(canonical(report)), source_binding_sha256=checked['source_binding_sha256'],
            reference_scope=deepcopy(checked['reference_scope']), resources=resources,
            provenance=deepcopy(provenance), deployment=deepcopy(deployment),
            files=sorted(files.values(), key=lambda item: item['path']), invocations=invocations,
            outputs=sorted(outputs.values(), key=lambda item: item['path']))
        # Keep the existing exact runtime schema. The full revalidated report
        # (including bounded-validation proof) is already pinned by plan_sha256;
        # source_binding_sha256 also pins the explicit authorization overlay.
        if len(canonical(payload)) > 1_000_000:
            raise AuthorExecutionError('Author package exceeds the existing bounded manifest capacity')
        if sum(row['size'] for row in files.values()) + len(canonical(payload)) + 8192 > resources['storage_bytes']:
            raise AuthorExecutionError('Original bytes and package exceed the storage reservation')
        return dict(package=payload, package_sha256=sha256(canonical(payload)),
            state='compiled_not_authorized', can_dispatch=False, execution_authorized=False,
            physical_evaluation_performed=False, required_integrations=list(_BLOCKERS))
    except AuthorExecutionError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AuthorExecutionError('Author package cannot be compiled from the pinned recipe and deployment') from exc


def validate_bound_author_package(value, *, report, evidence, source_files, deployment, resources, provenance):
    """Recompute the entire binding before approval; digest alone is not trust."""
    expected = compile_author_package(report, evidence=evidence, source_files=source_files,
        deployment=deployment, resources=resources, provenance=provenance)
    if not isinstance(value, dict) or canonical(value) != canonical(expected):
        raise AuthorExecutionError('Reference execution package or approval input changed')
    return deepcopy(expected)


def hpc_copy_plan(value, *, deployment, request_root):
    """Project exact HPC-to-HPC copies for a future reviewed stage adapter.

    No filesystem operation occurs.  The caller must use no-follow descriptors,
    recheck size/SHA and preserve immutable originals.  This projection cannot
    replace an accounted stage request or a complete upload receipt.
    """
    if (not isinstance(value, dict) or set(value) != {'package', 'package_sha256', 'state',
            'can_dispatch', 'execution_authorized', 'physical_evaluation_performed', 'required_integrations'}
            or value['state'] != 'compiled_not_authorized' or value['can_dispatch'] is not False
            or value['execution_authorized'] is not False or value['physical_evaluation_performed'] is not False
            or value['required_integrations'] != list(_BLOCKERS)):
        raise AuthorExecutionError('Only an unmodified reference package is accepted')
    package = value['package']
    if (not isinstance(package, dict) or package.get('role') != _ROLE
            or package.get('kind') != 'original_author_execution' or package.get('B_input') is not False
            or sha256(canonical(package)) != _hash(value['package_sha256'])
            or canonical(package.get('deployment')) != canonical(deployment)):
        raise AuthorExecutionError('Reference package identity or trusted deployment changed')
    request_root = PurePosixPath(_absolute(request_root))
    resources = _resources(package['resources']); roots, _ = _deployment(deployment, resources)
    copies = []
    for row in _source_files(package['files']).values():
        if row['binding_sha256'] not in roots:
            raise AuthorExecutionError('Unknown source binding')
        root = PurePosixPath(roots[row['binding_sha256']])
        if root == request_root or root in request_root.parents or request_root in root.parents:
            raise AuthorExecutionError('Author source roots must be disjoint from request storage')
        copies.append(dict(source=str(root / row['source_path']), destination=row['path'],
            size=row['size'], sha256=row['sha256'], binding_sha256=row['binding_sha256']))
    return dict(schema_version=VERSION, role=_ROLE, package_sha256=value['package_sha256'],
        copies=sorted(copies, key=lambda item: item['destination']),
        total_input_bytes=sum(row['size'] for row in copies), execution_authorized=False)
