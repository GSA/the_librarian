"""Synthetic protocol cases; no installed corpus or business acceptance."""
import copy
import unittest

from tools import business_context as bc

CATEGORIES = ('capability', 'rule', 'workflow', 'domain-term', 'domain-entity',
              'role', 'system', 'integration', 'decision')
TIME = '2026-01-01T00:00:00+00:00'


def evidence():
    return {'source_id': 'S1', 'artifact_id': 'A-one', 'source_version': 'v1',
            'sha256': 'a' * 64, 'scope': 'inspected passage',
            'locator': {'path': 'documents/item.md', 'anchor': None,
                        'line_start': 1, 'line_end': 2, 'json_pointer': None}}


def entity(id='E-one', category='rule', **overrides):
    result = dict(id=id, namespace='scope', category=category, name='Item',
                  aliases=[], evidence=[evidence()])
    result.update(overrides)
    return result


def claim(id='C-one', **overrides):
    result = dict(id=id, subject_id='E-one', relation='constrains',
                  object_id='E-two', external_reference=None, basis='reported',
                  qualification='inferred-candidate',
                  origin={'actor': 'synthetic-route', 'method': 'model',
                          'tool_version': 'v1'}, confidence=1,
                  evidence=[evidence()], observed_at=TIME, valid_from=TIME,
                  valid_until='2027-01-01T00:00:00Z', review_event_ids=[],
                  contradicts=[], supersedes=None, correction_reason=None,
                  handling=['restricted'])
    result.update(overrides)
    return result


def bundle(*entities, claims=(), identity_decisions=()):
    return dict(contract_version=1, entities=list(entities), claims=list(claims),
                identity_decisions=list(identity_decisions))


class BundleTests(unittest.TestCase):
    def test_empty_bundle_and_all_categories(self):
        bc.validate_bundle(bundle())
        data = bundle(*(entity('E-' + str(i), category)
                        for i, category in enumerate(CATEGORIES)))
        before = copy.deepcopy(data)
        self.assertIsNone(bc.validate_bundle(data))
        self.assertEqual(data, before)

    def refuse(self, data):
        with self.assertRaises(bc.BusinessContextError):
            bc.validate_bundle(data)

    def pair(self, **overrides):
        return bundle(entity(), entity('E-two', 'workflow'),
                      claims=[claim(**overrides)])

    def test_malformed_fields_and_bounds(self):
        for value in (None, [], {'contract_version': 1}):
            self.refuse(value)
        for key, value in [('contract_version', True), ('entities', [None]),
                           ('entities', [entity(id=[])]), ('claims', [{}]),
                           ('entities', [entity(category='code-symbol')]),
                           ('entities', [entity(name='bad\u0000name')]),
                           ('entities', [entity(aliases=['a'] * 101)]),
                           ('entities', [entity('E-' + str(i)) for i in range(257)])]:
            data = bundle()
            data[key] = value
            self.refuse(data)
        data = bundle(entity())
        data['entities'][0]['extra'] = 'no'
        self.refuse(data)
        for id in ('E-', 'E-!bad', 'E-' + 'a' * 101, 'S1'):
            self.refuse(bundle(entity(id)))
        for value in ([entity(), entity()], [entity(evidence=[])],
                      [entity(aliases=['x', 'x'])]):
            self.refuse(bundle(*value))

    def test_exact_locations(self):
        bc.validate_bundle(self.pair())
        for key, value in [('source_id', 'E-no'), ('artifact_id', 'S1'),
                           ('source_version', ''), ('sha256', None),
                           ('scope', ''), ('sha256', 'A' * 64)]:
            data = self.pair()
            data['claims'][0]['evidence'][0][key] = value
            self.refuse(data)
        for key, value in [('path', '../item'), ('path', '/item'), ('path', 'a//b'),
                           ('path', 'a/./b'), ('path', 'C:item'), ('path', 'a\\b'),
                           ('line_start', True), ('line_start', 0), ('line_end', 0),
                           ('json_pointer', '/field'), ('anchor', '')]:
            data = self.pair()
            data['claims'][0]['evidence'][0]['locator'][key] = value
            self.refuse(data)
        data = self.pair()
        loc = data['claims'][0]['evidence'][0]['locator']
        loc.update(line_start=None, line_end=None, json_pointer='/field/~0~1')
        bc.validate_bundle(data)
        for pointer in ('bad', '/~2', '/bad~'):
            loc['json_pointer'] = pointer
            self.refuse(data)
        loc['json_pointer'] = ''  # exact JSON document root
        bc.validate_bundle(data)
        data['claims'][0]['evidence'] *= 2
        self.refuse(data)

    def test_claim_axes_and_direction(self):
        relations = [('rule', 'constrains', 'workflow'),
                     ('rule', 'constrains', 'system'),
                     ('rule', 'constrains', 'integration'),
                     ('rule', 'constrains', 'capability'),
                     ('workflow', 'realizes', 'capability'),
                     ('role', 'participates-in', 'workflow'),
                     ('system', 'supports', 'workflow'),
                     ('system', 'supports', 'capability'),
                     ('integration', 'supports', 'workflow'),
                     ('integration', 'supports', 'capability'),
                     ('integration', 'connects-to', 'system'),
                     ('domain-term', 'denotes', 'domain-entity')]
        relations += [('decision', 'governs', c) for c in
                      ('rule', 'workflow', 'system', 'integration', 'capability')]
        for subject, relation, obj in relations:
            data = bundle(entity(category=subject), entity('E-two', obj),
                          claims=[claim(relation=relation)])
            bc.validate_bundle(data)
            data['claims'][0].update(subject_id='E-two', object_id='E-one')
            self.refuse(data)
        for key, value in [('basis', 'inferred'), ('qualification', 'verified'),
                           ('confidence', True), ('confidence', float('nan')),
                           ('confidence', float('inf')), ('confidence', -0.1),
                           ('confidence', 1.1), ('relation', 'implements'),
                           ('subject_id', []), ('observed_at', '2026-01-01'),
                           ('valid_until', TIME), ('handling', ['x'] * 31)]:
            self.refuse(self.pair(**{key: value}))
        for status in ('extracted', 'inferred-candidate', 'confirmed', 'rejected', 'retracted'):
            data = self.pair(qualification=status)
            if status in ('confirmed', 'rejected', 'retracted'):
                self.refuse(data)
                data['claims'][0]['review_event_ids'] = ['rev-one']
            before = copy.deepcopy(data)
            bc.validate_bundle(data)
            self.assertEqual(data, before)
        for method in ('model', 'extraction'):
            self.refuse(self.pair(origin={'actor': 'route', 'method': method,
                                         'tool_version': None}))

    def test_external_only_code_reference(self):
        reference = dict(provider='knowitall', repository='repo-id', revision='rev-id',
                         snapshot='snapshot-id', locator='symbol-id')
        for category in ('rule', 'capability', 'workflow', 'decision'):
            data = bundle(entity(category=category), claims=[claim(
                relation='references-code', object_id=None, external_reference=reference)])
            bc.validate_bundle(data)
        for change in ({'provider': 'other'}, {'code_body': 'body'}):
            ref = dict(reference, **change)
            self.refuse(bundle(entity(), claims=[claim(relation='references-code',
                object_id=None, external_reference=ref)]))
        self.refuse(self.pair(external_reference=reference))
        self.refuse(self.pair(object_id=None))

    def test_claim_reference_history(self):
        for changes in ({'subject_id': 'E-missing'}, {'contradicts': ['C-one']},
                        {'contradicts': ['C-missing']}, {'supersedes': 'C-missing'},
                        {'correction_reason': 'unlinked'},
                        {'review_event_ids': ['rev-UPPER']}):
            self.refuse(self.pair(**changes))
        old = claim()
        new = claim('C-two', supersedes='C-one', correction_reason='correct endpoint')
        data = bundle(entity(), entity('E-two', 'workflow'), claims=[new, old])
        bc.validate_bundle(data)
        new['correction_reason'] = None
        self.refuse(data)
        new['correction_reason'] = 'correct endpoint'
        old.update(supersedes='C-two', correction_reason='loop')
        self.refuse(data)
        old.update(supersedes=None, correction_reason=None)
        data['claims'].append(claim('C-three', supersedes='C-one', correction_reason='branch'))
        self.refuse(data)


if __name__ == '__main__':
    unittest.main()
