"""Bounded GitHub discovery for the trusted reference side, never an Agent tool."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import quote, urlencode

from .manifest import canonical, private_directory, sha256
from .papers import doi_text
from .slurm_read import _capture, _write_new
from .tasks import text


class DiscoveryError(ValueError):
    pass


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}', value):
        raise DiscoveryError('Invalid repository identity')
    if value.split('/')[1] in {'.', '..'}:
        raise DiscoveryError('Invalid repository identity')
    return value


def title_words(value):
    return ' '.join(re.findall(r'[^\W_]+', value.casefold()))


class GitHubReader:
    """Uses the operator's existing gh login; credentials never enter results."""
    def __init__(self, *, executable='gh', capture=_capture):
        self.executable, self.capture = executable, capture

    def get(self, endpoint):
        # All endpoints are built internally. Explicit host/method, no shell,
        # user-supplied URL, checkout, install or target-code execution.
        if not endpoint.startswith(('search/code?', 'repos/')) or len(endpoint) > 4096:
            raise DiscoveryError('Unsupported GitHub endpoint')
        result = self.capture([self.executable, 'api', '--hostname', 'github.com', '--method', 'GET',
                               '-H', 'Accept: application/vnd.github+json',
                               '-H', 'X-GitHub-Api-Version: 2022-11-28', endpoint],
                              timeout=30, max_bytes=2_000_000)
        return result


class SourceDiscovery:
    def __init__(self, audit_directory, *, reader=None):
        self.root = private_directory(audit_directory)
        self.reader = reader or GitHubReader()

    def discover(self, title, doi):
        title, doi = text(title, 500), doi_text(doi)
        identifier = uuid.uuid4().hex
        folder = self.root/identifier
        folder.mkdir(mode=0o700)
        report = dict(schema_version=1, id=identifier, title=title, doi=doi,
                      at=datetime.now(timezone.utc).isoformat(), state='searching',
                      queries=[], candidates=[], failures=[], search_exhaustive=False,
                      author_identity_verified=False, scientific_validation=False)
        _write_new(folder/'intent.json', report)
        calls = 0

        def get(endpoint):
            nonlocal calls
            calls += 1
            if calls > 18:
                raise DiscoveryError('Request budget exhausted')
            _write_new(folder/f'{calls:02d}-request.json', {'endpoint': endpoint})
            try:
                response = self.reader.get(endpoint)
            except (OSError, ValueError) as exc:
                _write_new(folder/f'{calls:02d}-failure.json', {'error_type': type(exc).__name__})
                raise DiscoveryError('transport_failed') from exc
            _write_new(folder/f'{calls:02d}-response.json', response)
            if response.get('failure') or response.get('returncode') != 0:
                raise DiscoveryError('github_request_failed')
            try:
                raw = base64.b64decode(response['stdout'], validate=True)
                if len(raw) > 2_000_000:
                    raise ValueError('response limit')
                return json.loads(raw)
            except (KeyError, ValueError, TypeError) as exc:
                raise DiscoveryError('invalid_github_response') from exc

        # Identifier suffix finds repositories whose indexed text punctuation
        # prevents an exact DOI search. It never constitutes matching evidence.
        suffix = re.findall(r'[a-z0-9]{4,}', doi.rsplit('/', 1)[-1])
        queries = list(dict.fromkeys([f'"{doi}"',
                                     suffix[-1] if suffix else f'"{doi}"',
                                     '"'+title_words(title)[:240]+'"']))
        repositories = []
        for query in queries:
            record = dict(query=query, state='failed')
            report['queries'].append(record)
            try:
                data = get('search/code?'+urlencode({'q': query, 'per_page': 10}))
                if (not isinstance(data, dict) or not isinstance(data.get('items'), list)
                        or len(data['items']) > 10 or type(data.get('total_count')) is not int
                        or data['total_count'] < len(data['items'])
                        or type(data.get('incomplete_results')) is not bool):
                    raise DiscoveryError('invalid_search_response')
                found = []
                for item in data['items']:
                    name = repository_name(item['repository']['full_name'])
                    found.append(name)
                record.update(state='received', total_count=data['total_count'],
                              incomplete_results=data['incomplete_results'], repositories=found,
                              limited=data['total_count'] > len(found))
                for name in found:
                    if name not in repositories:
                        repositories.append(name)
            except (DiscoveryError, KeyError, TypeError) as exc:
                record['reason'] = str(exc) if isinstance(exc, DiscoveryError) else 'invalid_search_response'
        report['candidate_limit_reached'] = len(repositories) > 5
        for name in repositories[:5]:
            try:
                metadata = get(f'repos/{name}')
                if (metadata.get('full_name', '').casefold() != name.casefold()
                        or metadata.get('private') is not False):
                    raise DiscoveryError('repository_identity_or_visibility_mismatch')
                commits = get(f'repos/{name}/commits?per_page=1')
                commit = commits[0]['sha']
                if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit):
                    raise DiscoveryError('invalid_commit_identity')
                readme = get(f'repos/{name}/readme?ref={commit}')
                if (readme.get('type') != 'file' or readme.get('encoding') != 'base64'
                        or type(readme.get('size')) is not int or not 0 <= readme['size'] <= 500_000):
                    raise DiscoveryError('unsupported_readme')
                content = base64.b64decode(''.join(readme['content'].split()), validate=True)
                blob = hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
                if len(content) != readme['size'] or blob != readme['sha']:
                    raise DiscoveryError('readme_identity_mismatch')
                path = readme['path']
                if not isinstance(path, str) or len(path) > 1000:
                    raise DiscoveryError('invalid_readme_path')
                decoded = content.decode('utf-8')
                doi_match = bool(re.search(r'(?<![a-z0-9])'+re.escape(doi)+r'(?![a-z0-9._/-])', decoded.casefold()))
                normalized_title = title_words(title)
                title_match = bool(normalized_title and normalized_title in title_words(decoded))
                license_data = metadata.get('license')
                license_id = license_data.get('spdx_id') if isinstance(license_data, dict) else None
                if license_id is not None and (not isinstance(license_id, str) or len(license_id) > 100):
                    raise DiscoveryError('invalid_license_metadata')
                report['candidates'].append(dict(repository=name, url='https://github.com/'+name,
                    commit=commit, readme_url=f'https://github.com/{name}/blob/{commit}/'+quote(path, safe='/'),
                    readme_sha256=sha256(content), readme_blob_sha1=blob,
                    doi_match=doi_match, title_match=title_match,
                    association='doi_and_title' if doi_match and title_match else 'unconfirmed',
                    license_spdx=license_id, redistribution_authorized=False))
            except (DiscoveryError, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
                report['failures'].append(dict(repository=name,
                    reason=str(exc) if isinstance(exc, DiscoveryError) else 'invalid_repository_response'))
        report['state'] = 'partial' if report['failures'] or any(
            q['state'] != 'received' or q.get('incomplete_results') or q.get('limited')
            for q in report['queries']) or report['candidate_limit_reached'] else 'finished'
        report['request_count'] = calls
        _write_new(folder/'result.json', report)
        return report


def main():
    parser = argparse.ArgumentParser(description='Reference-side paper source discovery')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--paper-id', required=True)
    parser.add_argument('--audit-directory', type=Path, required=True)
    args = parser.parse_args()
    from .papers import PaperStore
    from .tasks import TaskStore
    papers = PaperStore(TaskStore(args.database))
    paper = papers.get(args.paper_id)
    report = SourceDiscovery(args.audit_directory).discover(paper['title'], paper['doi'])
    papers.record_source_search(args.paper_id, report)
    print(json.dumps({'state': report['state'], 'candidates': len(report['candidates']),
                      'matched': sum(c['association'] == 'doi_and_title' for c in report['candidates'])}))


if __name__ == '__main__':
    main()
