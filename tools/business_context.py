"""Pure business-context conformance, not truth, freshness or authorization."""
from datetime import datetime, timezone
import math
import re
import unicodedata


class BusinessContextError(ValueError):
    """The supplied object does not conform to the business-context contract."""


CATEGORIES = frozenset({'capability', 'rule', 'workflow', 'domain-term',
                        'domain-entity', 'role', 'system', 'integration', 'decision'})


def _require(condition, message):
    if not condition:
        raise BusinessContextError(message)


def _exact(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields.split()),
             'Invalid object fields: ' + fields)


def _text(value, maximum=500):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
             and not any(unicodedata.category(c) in {'Cc', 'Cs'} for c in value),
             'Expected bounded nonempty text without controls')


def _list(value, maximum=30, nonempty=False):
    _require(isinstance(value, list) and len(value) <= maximum
             and (not nonempty or bool(value)), 'Invalid bounded list')
    for i, item in enumerate(value):
        _require(item not in value[:i], 'Duplicate list item')


def _choice(value, choices):
    _require(isinstance(value, str) and value in choices, 'Invalid vocabulary value')


def _identifier(value, prefix):
    _pattern(value, prefix + r'-[A-Za-z0-9][A-Za-z0-9-]{0,99}')


def _pattern(value, pattern):
    _require(isinstance(value, str) and re.fullmatch(pattern, value) is not None,
             'Invalid identifier or hash')


def _time(value):
    _text(value)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BusinessContextError('Invalid ISO-8601 time') from exc
    _require(result.tzinfo is not None and result.utcoffset() is not None,
             'Time must have a timezone')
    return result.astimezone(timezone.utc)


def _strings(value, pattern=None, nonempty=False):
    _list(value, nonempty=nonempty)
    for item in value:
        _text(item)
        if pattern:
            _pattern(item, pattern)


def _evidence_list(value):
    _list(value, nonempty=True)
    for item in value:
        _evidence(item)


def _evidence(item):
    _exact(item, 'source_id artifact_id source_version sha256 locator scope')
    _pattern(item['source_id'], r'[SIO][0-9]{1,199}')
    _identifier(item['artifact_id'], 'A')
    _text(item['source_version'])
    _pattern(item['sha256'], r'[0-9a-f]{64}')
    _text(item['scope'])
    loc = item['locator']
    _exact(loc, 'path anchor line_start line_end json_pointer')
    path = loc['path']
    _text(path, 4096)
    _require(not path.startswith('/') and '\\' not in path
             and ':' not in path.split('/')[0]
             and all(p not in {'', '.', '..'} for p in path.split('/')),
             'Path must be canonical root-relative')
    if loc['anchor'] is not None:
        _text(loc['anchor'])
    start, end, pointer = loc['line_start'], loc['line_end'], loc['json_pointer']
    if pointer is None:
        _require(type(start) is int and type(end) is int and 0 < start <= end,
                 'Invalid inclusive line range')
    else:
        _require(start is None and end is None, 'Mixed locator forms')
        _require(isinstance(pointer, str) and len(pointer) <= 1000
                 and (pointer == '' or pointer.startswith('/'))
                 and re.search(r'~(?![01])', pointer) is None
                 and not any(unicodedata.category(c) in {'Cc', 'Cs'} for c in pointer),
                 'Invalid exact JSON pointer')


_RELATIONS = {
    'constrains': (frozenset({'rule'}), frozenset({'workflow', 'system', 'integration', 'capability'})),
    'realizes': (frozenset({'workflow'}), frozenset({'capability'})),
    'participates-in': (frozenset({'role'}), frozenset({'workflow'})),
    'supports': (frozenset({'system', 'integration'}), frozenset({'workflow', 'capability'})),
    'connects-to': (frozenset({'integration'}), frozenset({'system'})),
    'governs': (frozenset({'decision'}), frozenset({'rule', 'workflow', 'system', 'integration', 'capability'})),
    'denotes': (frozenset({'domain-term'}), frozenset({'domain-entity'})),
    'references-code': (frozenset({'rule', 'capability', 'workflow', 'decision'}), frozenset()),
}
QUALIFICATIONS = frozenset({'extracted', 'inferred-candidate', 'confirmed', 'rejected', 'retracted'})


def _claim(item, entities):
    _exact(item, 'id subject_id relation object_id external_reference basis qualification origin '
           'confidence evidence observed_at valid_from valid_until review_event_ids contradicts '
           'supersedes correction_reason handling')
    _identifier(item['id'], 'C')
    _identifier(item['subject_id'], 'E')
    _require(item['subject_id'] in entities, 'Missing subject')
    _choice(item['relation'], _RELATIONS)
    subjects, objects = _RELATIONS[item['relation']]
    _require(entities[item['subject_id']]['category'] in subjects, 'Invalid subject category')
    if item['relation'] == 'references-code':
        _require(item['object_id'] is None, 'Code target must be external only')
        ref = item['external_reference']
        _exact(ref, 'provider repository revision snapshot locator')
        _choice(ref['provider'], {'knowitall'})
        for key in ('repository', 'revision', 'snapshot', 'locator'):
            _text(ref[key])
    else:
        _require(item['external_reference'] is None, 'Internal relation cannot contain code')
        _identifier(item['object_id'], 'E')
        _require(item['object_id'] in entities
                 and entities[item['object_id']]['category'] in objects, 'Invalid object category')
    _choice(item['basis'], {'reported'})
    _choice(item['qualification'], QUALIFICATIONS)
    origin = item['origin']
    _exact(origin, 'actor method tool_version')
    _text(origin['actor'])
    _choice(origin['method'], {'manual', 'extraction', 'model'})
    if origin['tool_version'] is not None:
        _text(origin['tool_version'])
    _require(origin['method'] == 'manual' or origin['tool_version'] is not None,
             'Producing route requires tool version')
    confidence = item['confidence']
    _require(confidence is None or (type(confidence) in (int, float)
             and 0 <= confidence <= 1 and math.isfinite(confidence)), 'Invalid confidence')
    _evidence_list(item['evidence'])
    _time(item['observed_at'])
    lower, upper = item['valid_from'], item['valid_until']
    if lower is not None:
        lower = _time(lower)
    if upper is not None:
        upper = _time(upper)
    _require(lower is None or upper is None or lower < upper, 'Invalid validity interval')
    _strings(item['review_event_ids'], r'rev-[a-z0-9-]{1,100}')
    _require(item['qualification'] not in {'confirmed', 'rejected', 'retracted'}
             or bool(item['review_event_ids']), 'Disposition requires review references')
    _strings(item['contradicts'], r'C-[A-Za-z0-9][A-Za-z0-9-]{0,99}')
    if item['supersedes'] is not None:
        _identifier(item['supersedes'], 'C')
        _text(item['correction_reason'])
    else:
        _require(item['correction_reason'] is None, 'Unlinked correction reason')
    _strings(item['handling'], nonempty=True)


def _records(items):
    result = {}
    for item in items:
        _require(item['id'] not in result, 'Duplicate record ID')
        result[item['id']] = item
    return result


def _history(records):
    successors = {}
    for item in records.values():
        predecessor = item['supersedes']
        if predecessor is not None:
            _require(predecessor in records and predecessor not in successors,
                     'Missing or competing predecessor')
            successors[predecessor] = item['id']
    for start in records:
        seen = set()
        while start in successors:
            _require(start not in seen, 'Cyclic history')
            seen.add(start)
            start = successors[start]
    return successors


def validate_bundle(bundle):
    """Check a version-1 bundle without mutating it; return None or raise."""
    _exact(bundle, 'contract_version entities claims identity_decisions')
    _require(type(bundle['contract_version']) is int and bundle['contract_version'] == 1,
             'Unsupported contract version')
    for key in ('entities', 'claims', 'identity_decisions'):
        _list(bundle[key], 256)
    for item in bundle['entities']:
        _exact(item, 'id namespace category name aliases evidence')
        for key in ('id', 'namespace', 'name'):
            _text(item[key])
        _require(isinstance(item['category'], str) and item['category'] in CATEGORIES,
                 'Unknown entity category')
        _list(item['aliases'], 100)
        for alias in item['aliases']:
            _text(alias)
        _identifier(item['id'], 'E')
        _evidence_list(item['evidence'])
    entities = _records(bundle['entities'])
    for item in bundle['claims']:
        _claim(item, entities)
    claims = _records(bundle['claims'])
    for item in claims.values():
        for target in item['contradicts']:
            _require(target in claims and target != item['id'], 'Invalid contradiction')
    _history(claims)
    _identity(bundle['identity_decisions'], entities)


def _identity(items, entities):
    for item in items:
        _exact(item, 'id operation inputs outputs actor decided_at reason evidence supersedes')
        _identifier(item['id'], 'M')
        _choice(item['operation'], {'merge', 'split'})
        for key in ('inputs', 'outputs'):
            _strings(item[key], r'E-[A-Za-z0-9][A-Za-z0-9-]{0,99}', nonempty=True)
            _require(all(id in entities for id in item[key]), 'Missing identity entity')
        _require(not set(item['inputs']) & set(item['outputs']), 'Overlapping identity endpoints')
        _require((item['operation'] == 'merge' and len(item['inputs']) >= 2
                  and len(item['outputs']) == 1) or
                 (item['operation'] == 'split' and len(item['inputs']) == 1
                  and len(item['outputs']) >= 2), 'Invalid identity operation arity')
        _text(item['actor'])
        _text(item['reason'])
        _time(item['decided_at'])
        _evidence_list(item['evidence'])
        if item['supersedes'] is not None:
            _identifier(item['supersedes'], 'M')
    records = _records(items)
    successors = _history(records)
    owners, graph, redirects, retired = {}, {}, {}, set()
    for item in items:
        predecessor = item['supersedes']
        if predecessor is not None:
            _require(set(item['inputs']) == set(records[predecessor]['inputs']),
                     'Corrected identity must consume same inputs')
        for output in item['outputs']:
            _require(output not in owners, 'Output must have one new identity owner')
            owners[output] = item['id']
        for input in item['inputs']:
            graph.setdefault(input, set()).update(item['outputs'])
        if item['id'] in successors:
            retired.update(item['outputs'])
        else:
            for input in item['inputs']:
                _require(input not in redirects, 'Competing active identity consumers')
                redirects[input] = item['outputs']
    # Include historical consumers; array order must not determine NEW identity validity.
    times = {id: _time(item['decided_at']) for id, item in records.items()}
    for item in items:
        for input in item['inputs']:
            if input in owners:
                _require(times[owners[input]] <= times[item['id']],
                         'Identity output cannot be created after consumption')
    # Iterative traversal avoids recursion limits, even with bounded dense histories.
    for start in graph:
        stack = [(start, frozenset())]
        visited = set()
        while stack:
            node, path = stack.pop()
            _require(node not in path, 'Cyclic identity redirects')
            if node in visited:
                continue
            visited.add(node)
            stack.extend((target, path | {node}) for target in graph.get(node, ()))
    return redirects, retired


OPEN_WORLD_CAVEAT = ('Missing records do not prove absence; reverse navigation is not '
                     'complete impact; a link is not support.')


def validate_answer(answer, bundle):
    """Check supplied answer metadata; does not verify any runtime authority."""
    validate_bundle(bundle)
    _exact(answer, 'contract_version as_of status claim_ids evidence review_event_ids '
           'handling limitations freshness')
    _require(type(answer['contract_version']) is int and answer['contract_version'] == 1,
             'Unsupported answer version')
    _time(answer['as_of'])
    _choice(answer['status'], {'qualified', 'partial', 'unknown', 'stale', 'denied'})
    _strings(answer['claim_ids'], r'C-[A-Za-z0-9][A-Za-z0-9-]{0,99}')
    _list(answer['evidence'])
    for item in answer['evidence']:
        _evidence(item)
    _strings(answer['review_event_ids'], r'rev-[a-z0-9-]{1,100}')
    _strings(answer['handling'])
    _strings(answer['limitations'], nonempty=True)
    _require(OPEN_WORLD_CAVEAT in answer['limitations'], 'Open-world caveat required')
    fresh = answer['freshness']
    _exact(fresh, 'catalog_state review_state vocabulary_hash selection_hash policy_hash '
           'source_state code_state checked_at')
    _require(_time(fresh['checked_at']) <= _time(answer['as_of']), 'Future freshness check')
    for key in ('catalog_state', 'review_state'):
        if fresh[key] is not None:
            _text(fresh[key])
    for key in ('vocabulary_hash', 'selection_hash', 'policy_hash'):
        if fresh[key] is not None:
            _pattern(fresh[key], r'[0-9a-f]{64}')
    _choice(fresh['source_state'], {'current', 'stale', 'unknown', 'denied'})
    _choice(fresh['code_state'], {'current', 'stale', 'unknown', 'denied', 'not-applicable'})
    status = answer['status']
    bindings = ('catalog_state', 'review_state', 'vocabulary_hash', 'selection_hash', 'policy_hash')
    if status != 'qualified':
        _require(len(answer['limitations']) >= 2, 'Explicit status limitation required')
    if status in {'stale', 'denied', 'unknown'}:
        _require(not answer['claim_ids'] and not answer['evidence']
                 and not answer['review_event_ids'], 'Status refuses evidence-bearing fields')
    if status == 'denied':
        _require(not answer['handling'] and all(fresh[k] is None for k in bindings)
                 and fresh['source_state'] == 'denied' and fresh['code_state'] == 'denied',
                 'Denial must remove identifying bindings and handling')
        return
    _require(fresh['source_state'] != 'denied' and fresh['code_state'] != 'denied',
             'Denied source/code requires denied envelope')
    if answer['claim_ids'] or answer['evidence'] or answer['review_event_ids']:
        _require(fresh['source_state'] != 'stale' and fresh['code_state'] != 'stale',
                 'Stale source/code refuses evidence')
    claims = _records(bundle['claims'])
    successors = _history(claims)
    for id in answer['claim_ids']:
        _require(id in claims, 'Unknown answer claim')
        item = claims[id]
        _require(all(e in answer['evidence'] for e in item['evidence'])
                 and all(r in answer['review_event_ids'] for r in item['review_event_ids'])
                 and all(h in answer['handling'] for h in item['handling']),
                 'Missing evidence/review/handling companions')
        if status == 'qualified':
            _require(item['qualification'] == 'confirmed' and id not in successors,
                     'Qualified answer requires active confirmed claims')
            _require(item['valid_from'] is not None and item['valid_until'] is not None,
                     'Unknown validity cannot qualify')
            _require(_time(item['valid_from']) <= _time(answer['as_of']) < _time(item['valid_until']),
                     'Claim is outside half-open validity')
            for other in claims.values():
                if other['id'] not in successors and other['qualification'] not in {'rejected', 'retracted'}:
                    _require(other['id'] not in item['contradicts'] and id not in other['contradicts'],
                             'Unresolved active contradiction')
    has_code = any(claims[id]['external_reference'] is not None for id in answer['claim_ids'])
    _require(not has_code or fresh['code_state'] != 'not-applicable',
             'Code reference requires applicable freshness')
    if status == 'qualified':
        _require(bool(answer['claim_ids']) and all(fresh[k] is not None for k in bindings)
                 and fresh['source_state'] == 'current'
                 and fresh['code_state'] in {'current', 'not-applicable'},
                 'Qualified answer requires complete current bindings')


def _normalize(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def resolve_entity(bundle, query, namespace, category=None):
    """Return scoped exact-ID/name resolution; collisions remain ambiguous."""
    validate_bundle(bundle)
    _text(query)
    _text(namespace)
    if category is not None:
        _choice(category, CATEGORIES)
    scoped = {e['id']: e for e in bundle['entities']
              if e['namespace'] == namespace and (category is None or e['category'] == category)}
    if re.fullmatch(r'E-[A-Za-z0-9][A-Za-z0-9-]{0,99}', query):
        candidates = [query] if query in scoped else []
    else:
        key = _normalize(query)
        candidates = sorted(e['id'] for e in scoped.values()
                            if key in {_normalize(x) for x in [e['name'], *e['aliases']]})
    entities = _records(bundle['entities'])
    redirects, retired = _identity(bundle['identity_decisions'], entities)
    pending, targets, visited = list(candidates), set(), set()
    while pending:
        id = pending.pop()
        if id in visited:
            continue
        visited.add(id)
        if id in redirects:
            pending.extend(redirects[id])
        elif id not in retired and id in scoped:
            targets.add(id)
    candidates = sorted(targets)
    return {'status': 'resolved' if len(candidates) == 1 else
            'ambiguous' if candidates else 'unknown', 'candidates': candidates}

