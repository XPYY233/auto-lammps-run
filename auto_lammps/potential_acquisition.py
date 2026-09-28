"""Reference-side collection of pinned MEAM files; never executes source code.

Repository scripts stay in private transport receipts, outside product Agent
inputs. Collection is not catalog admission, license approval or a simulation.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import uuid

from .manifest import private_directory, sha256
from .potentials import ELEMENTS, PotentialError, inspect_meam
from .source_discovery import GitHubReader, repository_name
from .slurm_read import _write_new


class AcquisitionError(ValueError):
    pass


def safe_path(value):
    if (not isinstance(value, str) or len(value) > 500 or not value
            or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-' for c in value)
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise AcquisitionError('unsupported_resource_path')
    return value


def meam_declarations(data):
    """Literal dependency extraction, not interpretation of a LAMMPS program."""
    if len(data) > 100000:
        raise AcquisitionError('input_size_limit')
    try:
        source = data.decode('ascii')
        rows = [shlex.split(line, comments=True, posix=True) for line in source.splitlines()]
    except (UnicodeError, ValueError) as exc:
        raise AcquisitionError('unsupported_input_syntax') from exc
    rows = [row for row in rows if row]
    styles = [row[1:] for row in rows if row[0] == 'pair_style']
    if not any('meam' in row for row in styles):
        return []
    if styles != [['meam']] or any(row[0] in {'include', 'jump', 'if', 'clear', 'suffix'} for row in rows):
        raise AcquisitionError('dynamic_or_multiple_potential_declarations')
    units = [row[1:] for row in rows if row[0] == 'units']
    if units != [['metal']]:
        raise AcquisitionError('MEAM_units_unresolved')
    coeffs = [row for row in rows if row[0] == 'pair_coeff']
    if len(coeffs) != 1:
        raise AcquisitionError('ambiguous_MEAM_coefficients')
    row = coeffs[0]
    if len(row) < 7 or row[1:3] != ['*', '*']:
        raise AcquisitionError('unsupported_MEAM_coefficients')
    library = safe_path(row[3])
    i = 4
    while i < len(row) and row[i] in ELEMENTS:
        i += 1
    elements = row[4:i]
    if not 1 <= len(elements) <= 8 or len(set(elements)) != len(elements) or i >= len(row)-1:
        raise AcquisitionError('MEAM_index_order_unresolved')
    parameters = safe_path(row[i])
    mapping = row[i+1:]
    if parameters == 'NULL' or len(mapping) > 118 or any(x not in elements for x in mapping):
        raise AcquisitionError('MEAM_type_mapping_unresolved')
    return [dict(library=library, parameters=parameters, elements=elements,
                 type_elements=mapping, units='metal', required_package='MEAM', pair_style='meam')]


class MeamAcquisition:
    def __init__(self, audit_directory, *, reader=None):
        self.root = private_directory(audit_directory)
        if reader is None:
            raise AcquisitionError('Use the configured HPC resource preparation entry')
        self.reader = reader

    def acquire(self, repository, commit):
        repository_name(repository)
        if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit):
            raise AcquisitionError('immutable_commit_required')
        identifier = uuid.uuid4().hex
        folder = self.root/identifier
        folder.mkdir(mode=0o700)
        report = dict(schema_version=1, id=identifier, at=datetime.now(timezone.utc).isoformat(),
            repository=repository, commit=commit, state='collecting', files=[], bindings=[], failures=[],
            license_files=[], license_status='unresolved', catalog_admitted=False,
            engine_verified=False, scientific_validation=False, execution_authorized=False)
        _write_new(folder/'intent.json', report)
        calls, downloaded = 0, {}

        def get(endpoint):
            nonlocal calls
            if calls >= 30:
                raise AcquisitionError('request_limit')
            calls += 1
            _write_new(folder/f'{calls:02d}-request.json', {'endpoint': endpoint})
            try:
                response = self.reader.get(endpoint)
                _write_new(folder/f'{calls:02d}-response.json', response)
                if response.get('failure') or response.get('returncode') != 0:
                    raise AcquisitionError('download_failed')
                raw = base64.b64decode(response['stdout'], validate=True)
                if len(raw) > 2_000_000:
                    raise AcquisitionError('response_size_limit')
                return json.loads(raw)
            except AcquisitionError:
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                _write_new(folder/f'{calls:02d}-failure.json', {'error_type': type(exc).__name__})
                raise AcquisitionError('invalid_transport_response') from exc

        def blob(entry):
            path, identity = entry['path'], entry['sha']
            if path in downloaded:
                return downloaded[path]
            if not 0 <= entry['size'] <= 1000000:
                raise AcquisitionError('resource_size_limit')
            value = get(f'repos/{repository}/git/blobs/{identity}')
            try:
                if value['encoding'] != 'base64' or value['sha'] != identity:
                    raise AcquisitionError('blob_identity_mismatch')
                data = base64.b64decode(''.join(value['content'].split()), validate=True)
                actual = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                if len(data) != entry['size'] or value['size'] != len(data) or actual != identity:
                    raise AcquisitionError('blob_integrity_mismatch')
            except (KeyError, ValueError, TypeError) as exc:
                if isinstance(exc, AcquisitionError):
                    raise
                raise AcquisitionError('invalid_blob') from exc
            downloaded[path] = data
            return data

        try:
            commit_data = get(f'repos/{repository}/git/commits/{commit}')
            if commit_data.get('sha') != commit:
                raise AcquisitionError('commit_identity_mismatch')
            tree_sha = commit_data['tree']['sha']
            if not re.fullmatch('[a-f0-9]{40}', tree_sha):
                raise AcquisitionError('invalid_tree_identity')
            tree = get(f'repos/{repository}/git/trees/{tree_sha}?recursive=1')
            if tree.get('sha') != tree_sha or tree.get('truncated') is not False or len(tree['tree']) > 2000:
                raise AcquisitionError('incomplete_repository_tree')
            entries = {}
            for item in tree['tree']:
                if item.get('type') != 'blob' or item.get('mode') not in {'100644', '100755'}:
                    continue
                try:
                    path = safe_path(item['path'])
                except AcquisitionError:
                    continue
                if (path in entries or type(item.get('size')) is not int or item['size'] < 0
                        or not re.fullmatch('[a-f0-9]{40}', item.get('sha', ''))):
                    raise AcquisitionError('invalid_tree_entry')
                entries[path] = item
            inputs = sorted(p for p, e in entries.items() if e['size'] <= 100000 and (
                PurePosixPath(p).suffix in {'.lmp', '.in'} or PurePosixPath(p).name.startswith('in.')))
            if len(inputs) > 8:
                report['failures'].append(dict(reason='input_scan_limit'))
            for path in inputs[:8]:
                try:
                    data = blob(entries[path])
                    for declaration in meam_declarations(data):
                        resolved = {}
                        for role in ('library', 'parameters'):
                            name = declaration[role]
                            choices = {name, str(PurePosixPath(path).parent/name)} & entries.keys()
                            if len(choices) != 1:
                                raise AcquisitionError('resource_path_missing_or_ambiguous')
                            resolved[role] = choices.pop()
                        binding = declaration | dict(input_path=path, input_sha256=sha256(data), files=resolved)
                        content = {role: blob(entries[name]) for role, name in resolved.items()}
                        for role, name in resolved.items():
                            if not any(f['path'] == name for f in report['files']):
                                model = content[role]
                                digest = sha256(model)
                                dest = folder/(digest+'.model')
                                if not dest.exists():
                                    with dest.open('xb') as handle:
                                        handle.write(model)
                                        handle.flush()
                                        os.fsync(handle.fileno())
                                    dest.chmod(0o400)
                                report['files'].append(dict(path=name, sha256=digest, size=len(model),
                                    blob_sha1=entries[name]['sha'], source_url=f'https://github.com/{repository}/blob/{commit}/{name}'))
                        try:
                            binding['inspection'] = inspect_meam(content['library'], content['parameters'], declaration['elements'])
                            binding['static_status'] = 'blocked' if binding['inspection']['blockers'] else 'checked'
                        except PotentialError as exc:
                            binding['static_status'] = 'needs_review'
                            binding['inspection_error'] = str(exc)
                        report['bindings'].append(binding)
                except AcquisitionError as exc:
                    report['failures'].append(dict(input_path=path, reason=str(exc)))
            licenses = sorted(p for p in entries if PurePosixPath(p).name.lower() in
                              {'license', 'license.txt', 'license.md', 'copying', 'copying.txt'})
            if len(licenses) > 2:
                report['failures'].append(dict(reason='license_scan_limit'))
            for path in licenses[:2]:
                data = blob(entries[path])
                report['license_files'].append(dict(path=path, sha256=sha256(data), blob_sha1=entries[path]['sha']))
            report['license_status'] = 'text_found_scope_unverified' if report['license_files'] else 'not_declared_in_scanned_tree'
        except (AcquisitionError, KeyError, TypeError, ValueError, AttributeError) as exc:
            report['failures'].append(dict(reason=str(exc) if isinstance(exc, AcquisitionError) else 'invalid_repository_response'))
        report['state'] = 'partial' if report['failures'] else 'finished'
        report['request_count'] = calls
        _write_new(folder/'result.json', report)
        return report


def prepare_paper_resources(papers, identifier, audit_directory, *, reader=None, policy=None, capture=None):
    paper = papers.get(identifier)
    candidates = [c for c in paper.get('source_discovery', {}).get('candidates', [])
                  if c.get('association') == 'doi_and_title']
    if len(candidates) != 1:
        return {'state': 'source_unresolved'}
    candidate, = candidates
    if reader is None:
        from .remote_resources import RemoteResources
        options = {'capture': capture} if capture is not None else {}
        acquisition = RemoteResources(audit_directory, policy, **options).acquire(candidate['repository'], candidate['commit'])
        papers.record_reference_resources(identifier, acquisition)
        report = acquisition.get('potential_acquisition')
        if report is None:
            return {'state': acquisition['state'], 'bindings': [], 'files': []}
    else:
        if policy is not None:
            raise AcquisitionError('Synthetic reader cannot be combined with HPC policy')
        report = MeamAcquisition(audit_directory, reader=reader).acquire(candidate['repository'], candidate['commit'])
    papers.record_potential_acquisition(identifier, report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Collect pinned reference MEAM resources without running them')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--paper-id', required=True)
    parser.add_argument('--audit-directory', required=True, type=Path)
    parser.add_argument('--hpc-policy', required=True, type=Path)
    args = parser.parse_args()
    from .papers import PaperStore
    from .tasks import TaskStore
    from .manifest import read_file, root_descriptor
    with root_descriptor(args.hpc_policy.parent) as root:
        policy = json.loads(read_file(root, args.hpc_policy.name, 10000))
    report = prepare_paper_resources(PaperStore(TaskStore(args.database)), args.paper_id, args.audit_directory, policy=policy)
    print(json.dumps({'state': report['state'], 'files': len(report.get('files', [])),
                      'bindings': len(report.get('bindings', []))}))


if __name__ == '__main__':
    main()
