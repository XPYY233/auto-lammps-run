"""Grounded static review references; no execution or scientific certification."""
from .manifest import canonical, sha256


class ReviewEvidenceError(ValueError):
    def __init__(self, message, *, diagnostics=None, allowed_sources=None):
        self.diagnostics = diagnostics or []
        self.allowed_sources = list(allowed_sources or [])
        if self.diagnostics:
            message += '; report_diagnostics=' + canonical({
                'errors': self.diagnostics, 'allowed_sources': self.allowed_sources}).decode()
        super().__init__(message)


def normalized_review_issues(value):
    """Accept the existing single-key issue aliases without dropping content."""
    if not isinstance(value, list):
        return value
    return [next(iter(item.values()))
            if isinstance(item, dict) and set(item) in ({'error'}, {'issue'})
            and isinstance(next(iter(item.values())), str) else item for item in value]


def validate_report(value, required, sources):
    """Return a complete grounded report, preserving every blocking issue.

    This validates report shape and literal references, not scientific meaning.
    An overlong issue list must be repaired by its author, never truncated here.
    """
    errors = []
    if not isinstance(value, dict):
        raise ReviewEvidenceError('Static review report must contain exactly issues, coverage and summary',
            diagnostics=[{'path': '$', 'code': 'report_shape'}], allowed_sources=sorted(sources))
    if set(value) != {'issues', 'coverage', 'summary'}:
        errors.append({'path': '$', 'code': 'report_shape'})
    issues = normalized_review_issues(value.get('issues'))
    if not isinstance(issues, list):
        errors.append({'path': 'issues', 'code': 'issues_shape'})
    else:
        if len(issues) > 12:
            errors.append({'path': 'issues', 'code': 'issues_limit', 'count': len(issues), 'maximum': 12})
        for index, issue in enumerate(issues):
            if not isinstance(issue, str) or not issue.strip():
                errors.append({'path': f'issues[{index}]', 'code': 'issue_shape'})
    if not isinstance(value.get('summary'), str):
        errors.append({'path': 'summary', 'code': 'summary_shape'})
    errors.extend(_coverage_diagnostics(value.get('coverage'), required, sources,
        blocking_issues=issues if isinstance(issues, list) else []))
    if errors:
        message = 'Static review report failed validation'
        if isinstance(issues, list) and len(issues) > 12:
            message += (f': returned {len(issues)} issues; maximum is 12. '
                'Merge duplicate issues and include only genuine blocking errors; '
                'retain every genuine blocking error without truncation')
        raise ReviewEvidenceError(message, diagnostics=errors, allowed_sources=sorted(sources))
    # Keep the existing exact, all-or-nothing acceptance gate. Diagnostics only
    # explain rejected reports; they cannot normalize or replace an evidence quote.
    validate_coverage(value['coverage'], required, sources, blocking_issues=issues)
    return {**value, 'issues': issues}


def _coverage_diagnostics(coverage, required, sources, *, blocking_issues):
    """Collect independent failures without echoing untrusted quotes or names."""
    errors = []
    wanted = {item['id'] for item in required}
    if not isinstance(coverage, list):
        return [{'path': 'coverage', 'code': 'coverage_shape'}]
    if len(coverage) != len(wanted):
        errors.append({'path': 'coverage', 'code': 'coverage_count',
                       'count': len(coverage), 'expected': len(wanted)})
    seen = set()
    for index, item in enumerate(coverage):
        path = f'coverage[{index}]'
        if not isinstance(item, dict):
            errors.append({'path': path, 'code': 'coverage_item_shape'})
            continue
        # Only IDs from the trusted requirement inventory may reach diagnostics.
        requirement = item.get('requirement')
        known = isinstance(requirement, str) and requirement in wanted
        detail = {'requirement': requirement} if known else {}
        if set(item) != {'requirement', 'evidence'}:
            errors.append({'path': path, 'code': 'coverage_item_shape', **detail})
        if not known:
            errors.append({'path': path + '.requirement', 'code': 'unknown_requirement'})
        elif requirement in seen:
            errors.append({'path': path + '.requirement', 'code': 'duplicate_requirement', **detail})
        else:
            seen.add(requirement)
        evidence = item.get('evidence')
        if not isinstance(evidence, list):
            errors.append({'path': path + '.evidence', 'code': 'evidence_shape', **detail})
            continue
        if len(evidence) > 16:
            errors.append({'path': path + '.evidence', 'code': 'evidence_limit',
                           'count': len(evidence), 'maximum': 16, **detail})
        if not evidence and not blocking_issues:
            errors.append({'path': path + '.evidence', 'code': 'missing_evidence_without_blocker', **detail})
        for reference_index, reference in enumerate(evidence):
            reference_path = path + f'.evidence[{reference_index}]'
            if not isinstance(reference, dict):
                errors.append({'path': reference_path, 'code': 'reference_shape', **detail})
                continue
            if set(reference) != {'source', 'quote'}:
                errors.append({'path': reference_path, 'code': 'reference_shape', **detail})
            source = reference.get('source')
            source_known = isinstance(source, str) and source in sources
            source_detail = {'source': source} if source_known else {}
            if not source_known:
                errors.append({'path': reference_path + '.source', 'code': 'unknown_source', **detail})
            quote = reference.get('quote')
            quote_valid = isinstance(quote, str) and quote.strip() and len(quote) <= 4000
            if not quote_valid:
                errors.append({'path': reference_path + '.quote', 'code': 'quote_shape',
                               **detail, **source_detail})
            elif source_known and quote not in sources[source]:
                errors.append({'path': reference_path + '.quote', 'code': 'quote_not_literal',
                               **detail, **source_detail})
    for requirement in sorted(wanted - seen):
        errors.append({'path': 'coverage', 'code': 'missing_requirement', 'requirement': requirement})
    return errors


def requirements(text, *, guidance=None, answers=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 24000:
        raise ReviewEvidenceError('Missing permitted research requirements')
    # Preserve every nonempty source line, including wrapped condition values.
    parts = [line for line in text.splitlines() if line.strip()]
    if len(parts) > 128:
        raise ReviewEvidenceError('Requirement reference inventory exceeds its bound')
    result = [dict(id=f'r{index:03d}_'+sha256(canonical(line))[:16], text=line,
                   origin='frozen_conditions') for index, line in enumerate(parts)]
    if guidance is not None and (not isinstance(guidance, list) or len(guidance) > 128
            or any(not isinstance(note, str) or not note.strip() or len(note) > 2000 for note in guidance)):
        raise ReviewEvidenceError('Invalid bounded user guidance inventory')
    if answers is not None and (not isinstance(answers, str) or len(answers) > 4000):
        raise ReviewEvidenceError('Invalid bounded clarification answer')
    # Do not silently truncate later requirements. Each steering event remains
    # visible; the reviewer applies latest-guidance priority without changing
    # frozen scientific conditions or claiming semantic interpretation is proven.
    for index, note in enumerate(guidance or []):
        result.append(dict(id=f'g{index:03d}_'+sha256(canonical(note))[:16],
                           text=note, origin='user_guidance'))
    if answers and answers.strip():
        result.append(dict(id='answer_'+sha256(canonical(answers))[:16],
                           text=answers, origin='clarification_answer'))
    return result


def validate_coverage(coverage, required, sources, *, blocking_issues):
    """Every supplied requirement must be addressed, with literal actual evidence.

    A missing implementation may have no quotes only when the reviewer reports
    a blocking issue. Literal matching grounds the review, not its interpretation.
    """
    wanted = {item['id'] for item in required}
    if not isinstance(coverage, list) or len(coverage) != len(wanted):
        raise ReviewEvidenceError('Static review must cover every supplied requirement ID')
    seen = set()
    for item in coverage:
        if (not isinstance(item, dict) or set(item) != {'requirement', 'evidence'}
                or not isinstance(item['requirement'], str) or item['requirement'] not in wanted
                or item['requirement'] in seen or not isinstance(item['evidence'], list)
                or len(item['evidence']) > 16 or (not item['evidence'] and not blocking_issues)):
            raise ReviewEvidenceError('Invalid or incomplete requirement evidence')
        seen.add(item['requirement'])
        for reference in item['evidence']:
            if (not isinstance(reference, dict) or set(reference) != {'source', 'quote'}
                    or not isinstance(reference['source'], str) or reference['source'] not in sources
                    or not isinstance(reference['quote'], str) or not reference['quote'].strip()
                    or len(reference['quote']) > 4000
                    or reference['quote'] not in sources[reference['source']]):
                raise ReviewEvidenceError('Static review evidence is absent from actual supplied tool outputs')
    return coverage
