"""Grounded static review references; no execution or scientific certification."""
from .manifest import canonical, sha256


class ReviewEvidenceError(ValueError):
    pass


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
