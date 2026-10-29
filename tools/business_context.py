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

