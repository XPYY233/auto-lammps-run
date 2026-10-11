"""Read-only, deterministic feedback for independently checkable proposal errors.

These diagnostics do not replace validate_proposal, authorize execution, evaluate
physics, or repair a proposal. Paths are JSON pointers; workflow/lines indexes
refer to zero-based lines in the original workflow string.
"""
from copy import deepcopy
import re

from . import agent_candidates as candidates
from . import analysis as numeric
from . import analysis_v2
from . import coordination_analysis as coordination
from . import scalar_analysis as scalar
from . import site_thermodynamics as site
from .candidate_tools import workflow_tokens
from .structures import validate_structure


VERSION = 1
_FIELDS = {'summary', 'questions', 'structure', 'potential_pin', 'workflow', 'analysis'}
_ANALYSIS_FIELDS = {'quantity', 'method', 'files'}
_STATIC_ERRORS = (ValueError, KeyError, TypeError, IndexError, AttributeError)
_FRAME_MESSAGE = 'Freeze an inclusive frame interval with one to eight exact selected frames'


def collect_proposal_diagnostics(value, *, max_atoms, output_layout='isolated',
                                 require_analysis_plan=False, packages=(), initial_geometry=None):
    """Collect independent failures using the supplied static validation context.

The existing validators remain the authority. A final whole-proposal check also
retains failures that require workflow or cross-source context. Nothing returned
here means the proposal is scientifically valid or safe to submit.
    """
    proposal = deepcopy(value)
    issues = []
    seen = set()
    observed_messages = set()

    def add(code, path, message):
        identity = (code, path)
        if identity not in seen:
            issues.append(dict(code=code, path=path, message=message))
            seen.add(identity)
        observed_messages.add(message)

    def check(code, path, function, *args, **kwargs):
        try:
            return True, function(*args, **kwargs)
        except _STATIC_ERRORS as error:
            # Shape errors in a dependent validator are not useful raw Python
            # exception strings (and may echo untrusted object contents).
            message = str(error) if isinstance(error, ValueError) else 'Invalid shape for this static contract'
            if not any(issue['message'] == message and
                       (issue['path'] == path or issue['path'].startswith(path + '/')) for issue in issues):
                add(code, path, message)
            observed_messages.add(message)
            return False, None

    if not isinstance(proposal, dict):
        add('proposal.fields', '', 'Candidate proposal fields are incomplete')
        return issues
    if set(proposal) not in (_FIELDS, _FIELDS | {'additional_structures'}):
        add('proposal.fields', '', 'Candidate proposal fields are incomplete')
    check('proposal.text', '/summary', candidates._text, proposal.get('summary'), 4000)
    questions = proposal.get('questions')
    if not isinstance(questions, list) or len(questions) > 30:
        add('proposal.questions', '/questions', 'Invalid clarification questions')
    elif questions:
        for index, question in enumerate(questions):
            path = f'/questions/{index}'
            if isinstance(question, dict):
                check('proposal.text', path + '/question', candidates._text, question.get('question'), 2000)
                for key in ('why', 'suggestion'):
                    if question.get(key) is not None:
                        check('proposal.text', path + '/' + key, candidates._text, question[key], 1000)
            else:
                check('proposal.text', path, candidates._text, question, 2000)
        if any(proposal.get(key) is not None for key in ('structure', 'potential_pin', 'workflow', 'analysis')):
            add('proposal.clarification_mode', '/questions', 'Clarification proposals must not contain a runnable candidate')
        return issues

    if initial_geometry is None:
        check('structure.invalid', '/structure', validate_structure, proposal.get('structure'), max_atoms=max_atoms)
        extras = proposal.get('additional_structures', [])
        if isinstance(extras, list):
            for index, extra in enumerate(extras):
                if isinstance(extra, dict) and 'structure' in extra:
                    check('structure.invalid', f'/additional_structures/{index}/structure',
                          validate_structure, extra['structure'], max_atoms=max_atoms)
    # Combined count, fixed-geometry identity and additional-structure bindings
    # need the original whole structure context, rather than invented defaults.
    try:
        candidates.structure_counts(proposal, max_atoms=max_atoms, initial_geometry=initial_geometry)
    except _STATIC_ERRORS as error:
        message = str(error) if isinstance(error, ValueError) else 'Invalid shape for this static contract'
        if message not in observed_messages:
            add('structure.context', '/structure', message)
    pin = proposal.get('potential_pin')
    if not isinstance(pin, str) or not re.fullmatch('[a-f0-9]{64}', pin):
        add('potential.pin', '/potential_pin', 'Select an exact supplied potential pin')

    analysis = proposal.get('analysis')
    files = None
    if not isinstance(analysis, dict) or set(analysis) not in (_ANALYSIS_FIELDS, _ANALYSIS_FIELDS | {'plan'}):
        add('analysis.fields', '/analysis', 'Explicit analysis quantity, method and files are required')
    if isinstance(analysis, dict):
        for key in ('quantity', 'method'):
            check('proposal.text', '/analysis/' + key, candidates._text, analysis.get(key), 4000)
        files = analysis.get('files')
        # Match the existing validator's basename convention on our private copy.
        if isinstance(files, list):
            files = [name.replace('\\', '/').split('/')[-1] if isinstance(name, str) else name for name in files]
        valid_files = (isinstance(files, list) and 1 <= len(files) <= 29
                       and all(isinstance(name, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name)
                               and name not in candidates.RESERVED_OUTPUTS for name in files)
                       and len(set(files)) == len(files))
        if not valid_files:
            add('analysis.files', '/analysis/files', 'Analysis output names must be distinct flat filenames')
        if require_analysis_plan and 'plan' not in analysis:
            add('analysis.plan_required', '/analysis/plan',
                'Executable research requires analysis.plan with tables and operations; never omit it')
        if 'plan' in analysis:
            _analysis_diagnostics(analysis['plan'], files if valid_files else [], add, check)

    workflow = proposal.get('workflow')
    if not isinstance(workflow, str):
        add('workflow.text', '/workflow', 'Invalid shape for this static contract')
    else:
        # The authoritative size/line limits apply AFTER tool expansion. Raw
        # whitespace/comments may disappear there, so do not reject them here.
        commands = 0
        for index, line in enumerate(workflow.splitlines()):
            path = f'/workflow/lines/{index}'
            valid, tokens = check('workflow.tokens', path, workflow_tokens, line)
            if not valid or not tokens:
                continue
            commands += 1
            if commands > 2000:
                break
            if tokens[0] not in candidates.COMMANDS | {'capture', 'emit_table'}:
                add('workflow.command', path, 'Unsupported candidate workflow command')
            if tokens[0] == 'fix' and len(tokens) >= 4 and tokens[3] == 'atom/swap':
                # This syntax fact is independent of package availability, type
                # mapping and sampler scope. Those still need the whole validator.
                _swap_type_diagnostics(tokens, path, add)

    try:
        candidates.validate_proposal(proposal, max_atoms=max_atoms, output_layout=output_layout,
                                     require_analysis_plan=require_analysis_plan, packages=packages,
                                     initial_geometry=initial_geometry)
    except _STATIC_ERRORS as error:
        message = str(error) if isinstance(error, ValueError) else 'Invalid shape for this static contract'
        # Do not repeat an already localized error as an unhelpful root error.
        localized = (message in observed_messages or
                     message.removeprefix('Structure specification: ') in observed_messages or
                     (message.startswith("atom/swap is missing the literal 'types' keyword") and
                      any(issue['code'] == 'workflow.atom_swap_types_keyword' for issue in issues)))
        if not localized:
            add('proposal.context', '', message)
    return issues


def _analysis_diagnostics(plan, files, add, check):
    prefix = '/analysis/plan'
    if not isinstance(plan, dict) or set(plan) != {'tables', 'operations'}:
        add('analysis.plan_fields', prefix, 'Explicit analysis tables and operations are required')
        return
    tables, operations = plan['tables'], plan['operations']
    usable = {}
    if isinstance(tables, list):
        for index, table in enumerate(tables):
            path = f'{prefix}/tables/{index}'
            if not isinstance(table, dict):
                add('analysis.table', path, 'Invalid table declaration')
                continue
            format_ = table.get('format')
            if format_ == coordination.FORMAT:
                valid = check('analysis.table', path, coordination.validate_table, table)[0]
            elif format_ == site.FORMAT:
                valid = check('analysis.table', path, site.validate_table, table)[0]
            elif 'format' in table:
                valid = check('analysis.table', path, scalar.validate_table, table)[0]
            else:
                # Numeric v1 has only a plan-level validator. Its real table and
                # real operations are checked together below, without placeholders.
                valid = True
            name = table.get('file')
            if not isinstance(name, str) or name not in files or name in usable:
                add('analysis.source', path + '/file', 'Analysis must use a distinct declared output file')
                valid = False
            if valid:
                usable[name] = table
    ids = set()
    if isinstance(operations, list):
        for index, operation in enumerate(operations):
            path = f'{prefix}/operations/{index}'
            if not isinstance(operation, dict):
                add('analysis.operation', path, 'Explicit analysis operation is required')
                continue
            valid_id, identifier = check('analysis.operation_id', path + '/id', numeric._name, operation.get('id'))
            if valid_id:
                if identifier in ids:
                    add('analysis.operation_id', path + '/id', 'Duplicate analysis operation')
                ids.add(identifier)
            method = operation.get('method')
            if method == coordination.METHOD:
                _frame_diagnostic(operation.get('frames'), path + '/frames', add)
            name = operation.get('file')
            table = usable.get(name) if isinstance(name, str) else None
            if table is None:
                add('analysis.operation_source', path + '/file', 'Operation requires a valid declared analysis source')
                continue
            if method == coordination.METHOD and table.get('format') == coordination.FORMAT:
                check('analysis.operation', path, coordination.validate_operation, operation, table)
            elif method == site.METHOD and table.get('format') == site.FORMAT:
                check('analysis.operation', path, site.validate_operation, operation, table)
            else:
                # The v2 plan validator selects the correct numeric/structural
                # contract, including forbidden trajectory-as-scalar operations.
                check('analysis.operation', path, analysis_v2.validate_plan,
                      dict(tables=[table], operations=[operation]), files)
    # Cross-source counts, unused sources, derived-byte reservation and the
    # original mixed-plan rules still come from the complete existing validator.
    check('analysis.plan_context', prefix, analysis_v2.validate_plan, plan, files)


def _frame_diagnostic(frames, path, add):
    if (not isinstance(frames, dict) or set(frames) != {'first', 'last', 'stride'}
            or any(type(value) is not int or not 0 <= value <= 2147483647 for value in frames.values())
            or frames['stride'] <= 0 or frames['first'] > frames['last']
            or (frames['last'] - frames['first']) % frames['stride']):
        add('analysis.frames_interval', path, _FRAME_MESSAGE)
    elif (frames['last'] - frames['first']) // frames['stride'] + 1 > coordination.MAX_FRAMES:
        add('analysis.frames_limit', path, _FRAME_MESSAGE)


def _swap_type_diagnostics(tokens, path, add):
    positions = [index for index in range(8, len(tokens)) if tokens[index] == 'types']
    if not positions:
        add('workflow.atom_swap_types_keyword', path,
            "atom/swap requires the literal 'types' keyword before its two type IDs")
        return
    if len(positions) != 1:
        add('workflow.atom_swap_types_options', path,
            'atom/swap supports distinct types, ke and semi-grand no only; no region or mu')
        return
    pair = tokens[positions[0] + 1:positions[0] + 3]
    if len(pair) != 2:
        add('workflow.atom_swap_types_pair', path, 'atom/swap option types is incomplete')
        return
    literal = [bool(re.fullmatch('[0-9]{1,10}', item)) and int(item) > 0 for item in pair]
    sampler = [bool(re.fullmatch(r'\$\{[A-Za-z][A-Za-z0-9_]*\}', item)) for item in pair]
    if (not all(literal) and not all(sampler)) or (all(literal) and int(pair[0]) == int(pair[1])):
        add('workflow.atom_swap_types_pair', path,
            'atom/swap types must be two distinct declared numeric atom types')
