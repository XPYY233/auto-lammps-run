"""Read-only, task-linked plan presentation; never prepare or execute a plan."""
import difflib
import json
import re

from .manifest import Snapshot, canonical, read_file, root_descriptor, sha256


CHANGE_LABELS = {'structure': '初始结构', 'additional_structures': '其他尺寸结构',
                 'potential_pin': '势函数版本', 'workflow': '计算步骤与脚本',
                 'analysis': '分析方法与输出', 'summary': '方案说明'}


def workspace(service, identifier):
    job = service.history.get(identifier)
    if not job:
        return {'versions': [], 'current': None}
    if sha256(service.tasks.export(identifier)) != job['condition_sha256']:
        raise ValueError('Prepared conditions changed')
    guidance = service.tasks.guidance(identifier)
    versions, previous, seen, latest = [], None, set(), None
    for event in job['events']:
        digest = event['payload'].get('snapshot_sha256')
        if event['state'] != 'prepared' or not digest or digest in seen:
            continue
        seen.add(digest)
        version = {'number': len(versions) + 1, 'at': event['at'], 'snapshot_sha256': digest}
        try:
            snapshot = Snapshot(service.snapshots / digest, digest)
            manifest = snapshot.verify()
            with root_descriptor(snapshot.path) as root:
                generation = json.loads(read_file(root, 'generation.json', 2000000))
                if generation['input'].get('condition_record_sha256') != job['condition_sha256']:
                    raise ValueError('Plan belongs to different conditions')
                proposal = generation['proposal']
                files = []
                for item in manifest['files']:
                    name = item['path']
                    if name not in {'in.lammps', 'analysis.json'} and not re.fullmatch(r'structure(?:-[A-Za-z0-9_-]+)?\.data', name):
                        continue
                    external = item.get('external_source')
                    content = (read_file(root, name, item['size']).decode('utf-8', 'replace')
                               if item['size'] <= 200000 and not external else None)
                    visible = {'name': name, 'size': item['size'], 'sha256': item['sha256'], 'content': content}
                    if external:
                        visible['external_source'] = external
                        visible['source_label'] = 'HPC 上固定的初始结构；原字节不会下载到控制端。'
                    files.append(visible)
            changed = ([label for key, label in CHANGE_LABELS.items()
                        if canonical(proposal.get(key)) != canonical(previous.get(key))]
                       if previous is not None else [])
            before = (previous or {}).get('workflow', '').splitlines()
            after = proposal.get('workflow', '').splitlines()
            diff = '\n'.join(list(difflib.unified_diff(before, after, fromfile='上一版步骤',
                                                     tofile='这一版步骤', lineterm=''))[:250]) if previous else ''
            notes = [item['note'] for item in guidance if item['at'] <= event['at']]
            version.update(available=True, changes=changed, workflow_diff=diff,
                           reason=notes[-1] if notes else '根据已确认的研究条件准备方案。')
            receipt = generation.get('potential_receipt') or {}
            latest = {'version': version['number'], 'snapshot_sha256': digest, 'files': files,
                      'structure': proposal.get('structure') or {},
                      'additional_structures': proposal.get('additional_structures') or [],
                      'analysis': proposal.get('analysis') or {}, 'summary': proposal.get('summary') or '',
                      'workflow': proposal.get('workflow') or '', 'resources': manifest['resources'],
                      'potential': {'elements': receipt.get('atom_type_elements') or [],
                                    'style': next((c.removeprefix('pair_style ') for c in receipt.get('commands', [])
                                                   if c.startswith('pair_style ')), ''),
                                    'pin': proposal.get('potential_pin'), 'units': receipt.get('units')},
                      'automatic_check': (generation.get('plan_reviews') or [None])[-1],
                      'failure_recovery': generation.get('failure_recovery')}
            previous = proposal
        except (OSError, ValueError, KeyError, TypeError):
            version.update(available=False, changes=[], reason='这一版的文件或关联证据未通过核验。')
        versions.append(version)
    # A previous verified plan remains visible while its next iteration is prepared.
    # It cannot be approved: the existing endpoint still requires state=prepared.
    if latest:
        latest['historical'] = job['state'] != 'prepared' or latest['snapshot_sha256'] != job['result'].get('snapshot_sha256')
    return {'versions': versions, 'current': latest, 'updated_at': job['updated_at'],
            'repair_count': sum(e['state'] in {'repairing_plan', 'repairing_format'} for e in job['events'])}
