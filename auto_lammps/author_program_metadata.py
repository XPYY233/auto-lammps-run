"""Reuse current environment evidence without promoting its coarse status.

These helpers neither run source nor authorize execution. Entry registration is
limited to complete original Python with source-bound dependency metadata; the
normal author-plan, package, approval, grant and accounted HPC gates still apply.
"""
from copy import deepcopy
from pathlib import PurePosixPath

from .manifest import canonical, sha256
from .tasks import TaskError


def runtime_python_candidates(inventory, identifier, latest):
    """Return original binary rows and a freshly rebound optional projection."""
    if not isinstance(latest, dict) or latest.get('state') not in {'metadata_ready', 'incomplete'}:
        raise TaskError('已有 CPU/Python 元数据尚未核验；未知请求须核对原请求。')
    report = latest.get('report')
    if not isinstance(report, dict):
        raise TaskError('已有环境的原始回执缺失。')
    native = report.get('native_files')
    if not native or any(row.get('status') != 'verified' for row in native):
        raise TaskError('当前 CPU 固定文件没有完整核验。')
    advertised = latest.get('capabilities')
    if advertised is None:
        # Old receipts keep their original meaning and need no new metadata
        # request when the prior binary-only check was already sufficient.
        if latest['state'] != 'metadata_ready':
            raise TaskError('旧环境回执未就绪，不能推断解释器已经可用。')
        return [deepcopy(row) for row in report['python_candidates']
                if row['status'] == 'verified'], None
    from .author_python_dependencies import validate_capabilities
    projection = validate_capabilities(inventory.capabilities(identifier, latest['request_id']))
    report_sha = sha256(canonical(report))
    if (projection != validate_capabilities(advertised)
            or projection['task_id'] != identifier
            or projection['environment_report_sha256'] != report_sha
            or latest.get('report_sha256') != report_sha
            or projection['base_native_ready'] is not True):
        raise TaskError('原Python依赖投影与当前任务或环境回执不一致。')
    original = report['python_candidates']
    projected = projection['interpreters']
    if len(original) != len(projected):
        raise TaskError('原Python解释器与逐入口投影数量不一致。')
    candidates = []
    for raw, view in zip(original, projected):
        if (any(raw[key] != view[key] for key in ('id', 'path', 'real_path', 'sha256'))
                or raw['status'] != view['raw_status']
                or raw['distributions'] != view['distributions']):
            raise TaskError('原Python解释器身份或依赖元数据与投影不一致。')
        if view['binary_verified']:
            if raw['status'] not in {'verified', 'dependencies_unverified'}:
                raise TaskError('解释器未通过二进制核验，不能仅凭投影准入。')
            candidates.append(deepcopy(raw))
    return candidates, projection


def python_entrypoints(candidate, projection, complete_files):
    """Select immutable original entries; return explicit unavailable reasons.

    A metadata-only inventory is not a child-process review. Dynamic execution,
    network/child drivers and an unproven virtualenv prefix remain unavailable.
    """
    if projection is None:
        return [], ['original_python_dependency_metadata_missing']
    views = [row for row in projection['interpreters']
             if all(row[key] == candidate[key] for key in ('id', 'path', 'real_path', 'sha256'))]
    if len(views) != 1 or not views[0]['binary_verified']:
        raise TaskError('原Python入口缺少相同解释器身份的依赖证据。')
    known = {row['path']: row['sha256'] for row in complete_files}
    selected, missing = [], []
    prefix_verified = PurePosixPath(candidate['path']).parent == PurePosixPath(candidate['real_path']).parent
    for entry in views[0]['entries']:
        reasons = list(entry['missing'])
        if known.get(entry['path']) != entry['sha256']:
            reasons.append('complete_original_sha_missing')
        if entry['parse_status'] != 'parsed':
            reasons.append('original_python_parse_unverified')
        if entry['source_requires_child_review']:
            reasons.append('original_child_or_dynamic_execution_review_missing')
        if not prefix_verified:
            reasons.append('interpreter_prefix_runtime_unverified')
        if any(known.get(local['path']) != local['sha256'] for local in entry['local_files']):
            reasons.append('complete_local_import_sha_missing')
        if entry['state'] != 'metadata_verified' and not reasons:
            reasons.append('original_python_metadata_unverified')
        if reasons:
            missing.append(dict(path=entry['path'], missing=list(dict.fromkeys(reasons))))
        else:
            selected.append({key: entry[key] for key in ('path', 'sha256')})
    return selected, missing
