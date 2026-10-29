"""Synthetic protocol cases; no installed corpus or business acceptance."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from tests.support import ROOT, clone_release
from tools import package
from tools.catalog import catalog_store as catalog

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


def decision(id='M-one', operation='merge', inputs=('E-one', 'E-two'),
             outputs=('E-three',), **overrides):
    result = dict(id=id, operation=operation, inputs=list(inputs), outputs=list(outputs),
                  actor='synthetic-reviewer', decided_at=TIME, reason='explicit identity decision',
                  evidence=[evidence()], supersedes=None)
    result.update(overrides)
    return result


class ResolutionTests(unittest.TestCase):
    def test_collision_scope_and_normalization(self):
        data = bundle(entity(name='Café  Item', aliases=['CAFÉ Item', 'cafe\u0301 item']),
                      entity('E-two', 'workflow', name='cafe\u0301 item'),
                      entity('E-three', name='Café Item', namespace='other'))
        before = copy.deepcopy(data)
        self.assertEqual(bc.resolve_entity(data, '  CAFÉ   ITEM ', 'scope'),
                         {'status': 'ambiguous', 'candidates': ['E-one', 'E-two']})
        self.assertEqual(bc.resolve_entity(data, 'CAFÉ Item', 'scope', 'rule'),
                         {'status': 'resolved', 'candidates': ['E-one']})
        self.assertEqual(bc.resolve_entity(data, 'E-one', 'scope'),
                         {'status': 'resolved', 'candidates': ['E-one']})
        for query in ('E-three', 'E-absent', 'missing'):
            self.assertEqual(bc.resolve_entity(data, query, 'scope'),
                             {'status': 'unknown', 'candidates': []})
        for query, namespace, category in ((None, 'scope', None), ('x', [], None),
                                           ('x', 'scope', 'code'), ('', 'scope', None)):
            with self.assertRaises(bc.BusinessContextError):
                bc.resolve_entity(data, query, namespace, category)
        self.assertEqual(data, before)

    def history_bundle(self, decisions):
        return bundle(*(entity('E-' + name, name=name) for name in
                        ('one', 'two', 'three', 'four', 'five', 'six')),
                      identity_decisions=decisions)

    def test_merge_split_and_chained_resolution(self):
        data = self.history_bundle([decision(), decision('M-two', 'split',
                                   ('E-three',), ('E-four', 'E-five'))])
        before = copy.deepcopy(data)
        for query in ('E-one', 'one', 'E-two', 'E-three'):
            self.assertEqual(bc.resolve_entity(data, query, 'scope'),
                             {'status': 'ambiguous', 'candidates': ['E-five', 'E-four']})
        self.assertEqual(data, before)
        data['entities'].reverse()
        data['identity_decisions'].reverse()
        self.assertEqual(bc.resolve_entity(data, 'one', 'scope'),
                         {'status': 'ambiguous', 'candidates': ['E-five', 'E-four']})
        data = self.history_bundle([decision()])
        self.assertEqual(bc.resolve_entity(data, 'E-one', 'scope'),
                         {'status': 'resolved', 'candidates': ['E-three']})

    def test_invalid_identity_history(self):
        invalid = [
            [decision(inputs=('E-missing', 'E-two'))],
            [decision(outputs=('E-one',))],
            [decision(inputs=('E-one',))],
            [decision(operation='split', outputs=('E-three',))],
            [decision(), decision('M-two', inputs=('E-four', 'E-five'))],
            [decision(), decision('M-two', inputs=('E-one', 'E-four'), outputs=('E-five',))],
            [decision(), decision('M-two', inputs=('E-three', 'E-four'), outputs=('E-one',))],
            [decision(), decision('M-two', inputs=('E-one', 'E-four'), outputs=('E-five',),
                                  supersedes='M-one')],
            [decision(), decision('M-two', outputs=('E-four',), supersedes='M-one'),
             decision('M-three', outputs=('E-five',), supersedes='M-one')],
            [decision(supersedes='M-missing')],
            [decision(supersedes='M-two'), decision('M-two', outputs=('E-four',), supersedes='M-one')],
            [None], [dict(decision(), extra='no')], [decision(inputs=[[]])],
        ]
        for decisions in invalid:
            with self.subTest(decisions=decisions), self.assertRaises(bc.BusinessContextError):
                bc.validate_bundle(self.history_bundle(decisions))

    def test_superseded_output_and_cross_scope_redirect(self):
        data = self.history_bundle([decision(), decision('M-two', outputs=('E-four',),
                                                        supersedes='M-one')])
        self.assertEqual(bc.resolve_entity(data, 'E-one', 'scope'),
                         {'status': 'resolved', 'candidates': ['E-four']})
        for query in ('E-three', 'three'):
            self.assertEqual(bc.resolve_entity(data, query, 'scope'),
                             {'status': 'unknown', 'candidates': []})
        data['entities'][3]['namespace'] = 'other'
        self.assertEqual(bc.resolve_entity(data, 'E-one', 'scope'),
                         {'status': 'unknown', 'candidates': []})
        data['entities'][3]['namespace'] = 'scope'
        data['entities'][3]['category'] = 'workflow'
        self.assertEqual(bc.resolve_entity(data, 'E-one', 'scope', 'rule'),
                         {'status': 'unknown', 'candidates': []})

    def test_corrections_preserve_claim_evidence(self):
        data = self.history_bundle([decision()])
        data['entities'][1]['category'] = 'workflow'
        data['claims'] = [claim()]
        before = copy.deepcopy(data)
        self.assertEqual(bc.resolve_entity(data, 'E-one', 'scope'),
                         {'status': 'resolved', 'candidates': ['E-three']})
        self.assertEqual(data, before)
        self.assertEqual(data['claims'][0]['subject_id'], 'E-one')


CAVEAT = 'Missing records do not prove absence; reverse navigation is not complete impact; a link is not support.'


def answer(**overrides):
    """Bindings are supplied assertions, not authenticated authorities."""
    result = dict(contract_version=1, as_of=TIME, status='qualified',
                  claim_ids=['C-one'], evidence=[evidence()], review_event_ids=['rev-one'],
                  handling=['restricted'], limitations=[CAVEAT],
                  freshness=dict(catalog_state='catalog-token', review_state='review-token',
                                 vocabulary_hash='a' * 64, selection_hash='b' * 64,
                                 policy_hash='c' * 64, source_state='current',
                                 code_state='not-applicable', checked_at=TIME))
    result.update(overrides)
    return result


class AnswerTests(unittest.TestCase):
    def data(self, **changes):
        return bundle(entity(), entity('E-two', 'workflow'),
                      claims=[claim(qualification='confirmed', review_event_ids=['rev-one'], **changes)])

    def test_qualified_complete_current_answer(self):
        data, envelope = self.data(), answer()
        before = copy.deepcopy((data, envelope))
        self.assertIsNone(bc.validate_answer(envelope, data))
        self.assertEqual((data, envelope), before)

    def refuse(self, envelope, data=None):
        with self.assertRaises(bc.BusinessContextError):
            bc.validate_answer(envelope, self.data() if data is None else data)

    def test_qualification_and_companion_refusals(self):
        for status in ('extracted', 'inferred-candidate', 'rejected', 'retracted'):
            data = self.data()
            data['claims'][0]['qualification'] = status
            self.refuse(answer(), data)
        for key, value in [('claim_ids', []), ('claim_ids', ['C-missing']),
                           ('claim_ids', [[]]), ('claim_ids', ['C-one', 'C-one']),
                           ('evidence', []), ('review_event_ids', []), ('handling', []),
                           ('contract_version', True), ('freshness', None),
                           ('limitations', []), ('evidence', [None])]:
            self.refuse(answer(**{key: value}))
        for key in ('catalog_state', 'review_state', 'vocabulary_hash', 'selection_hash', 'policy_hash'):
            env = answer()
            env['freshness'][key] = None
            self.refuse(env)
        for key in ('source_state', 'code_state'):
            for state in ('unknown', 'stale', 'denied'):
                env = answer()
                env['freshness'][key] = state
                self.refuse(env)
        data = self.data()
        data['claims'].append(claim('C-two', supersedes='C-one', correction_reason='correction'))
        self.refuse(answer(), data)
        # A contradiction can be declared only by the uncited side.
        for outgoing in (True, False):
            data = self.data()
            data['claims'].append(claim('C-two'))
            data['claims'][0 if outgoing else 1]['contradicts'] = ['C-two' if outgoing else 'C-one']
            self.refuse(answer(), data)
            data['claims'][1]['qualification'] = 'rejected'
            data['claims'][1]['review_event_ids'] = ['rev-rejected']
            bc.validate_answer(answer(), data)
        data = self.data()
        data['claims'][0].update(relation='references-code', object_id=None,
            external_reference=dict(provider='knowitall', repository='repo-id',
                                    revision='rev-id', snapshot='snapshot-id', locator='symbol-id'))
        self.refuse(answer(), data)
        env = answer()
        env['freshness']['code_state'] = 'current'
        bc.validate_answer(env, data)

    def test_temporal_instants_and_half_open_bounds(self):
        data = self.data(valid_from='2025-12-31T19:00:00-05:00')
        env = answer(as_of='2026-01-01T01:00:00+01:00')
        bc.validate_answer(env, data)
        for time in ('2025-12-31T23:59:59Z', '2027-01-01T00:00:00Z', '2026-01-01'):
            env = answer(as_of=time)
            env['freshness']['checked_at'] = '2025-01-01T00:00:00Z'
            self.refuse(env, data)
        for key in ('valid_from', 'valid_until'):
            data = self.data(**{key: None})
            self.refuse(answer(), data)
        env = answer()
        env['freshness']['checked_at'] = '2026-01-01T00:00:01Z'
        self.refuse(env)

    def test_stale_and_denied_leakage(self):
        self.refuse(answer(status='stale'))
        stale = answer(status='stale', claim_ids=[], evidence=[], review_event_ids=[],
                       limitations=[CAVEAT, 'Source is stale'])
        stale['freshness']['source_state'] = 'stale'
        bc.validate_answer(stale, self.data())
        denied = copy.deepcopy(stale)
        denied.update(status='denied', handling=[], limitations=[CAVEAT, 'Access denied'])
        for key in ('catalog_state', 'review_state', 'vocabulary_hash', 'selection_hash', 'policy_hash'):
            denied['freshness'][key] = None
        denied['freshness'].update(source_state='denied', code_state='denied')
        bc.validate_answer(denied, bundle())
        for key, value in [('claim_ids', ['C-one']), ('evidence', [evidence()]),
                           ('review_event_ids', ['rev-one']), ('handling', ['restricted'])]:
            env = copy.deepcopy(denied)
            env[key] = value
            self.refuse(env)
        for key, value in [('catalog_state', 'identifying-token'), ('review_state', 'identifying-token'),
                           ('policy_hash', 'a' * 64), ('source_state', 'current'), ('code_state', 'unknown')]:
            env = copy.deepcopy(denied)
            env['freshness'][key] = value
            self.refuse(env)
        for status in ('stale', 'denied'):
            self.refuse(answer(status=status, claim_ids=[], evidence=[], review_event_ids=[],
                               handling=[]))

    def test_partial_unknown_do_not_promote_candidates(self):
        data = self.data()
        data['claims'][0]['qualification'] = 'inferred-candidate'
        partial = answer(status='partial', limitations=[CAVEAT, 'Candidate only'])
        bc.validate_answer(partial, data)
        self.refuse(answer(), data)
        self.refuse(answer(status='partial'), data)
        for key in ('evidence', 'review_event_ids', 'handling'):
            self.refuse(dict(partial, **{key: []}), data)
        for key in ('source_state', 'code_state'):
            for state in ('denied', 'stale'):
                env = copy.deepcopy(partial)
                env['freshness'][key] = state
                self.refuse(env, data)
        unknown = answer(status='unknown', claim_ids=[], evidence=[], review_event_ids=[],
                         handling=[], limitations=[CAVEAT, 'No selected records'])
        unknown['freshness'].update(source_state='unknown', code_state='unknown')
        bc.validate_answer(unknown, bundle())
        self.refuse(answer(status='unknown', limitations=[CAVEAT, 'No records']))
        self.refuse(dict(unknown, limitations=[CAVEAT]))


class CompatibilityTests(unittest.TestCase):
    def test_existing_catalog_refuses_business_kind(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = catalog.initialize(root)
            records = [{'id': 'S1', 'kind': 'source',
                        'data': {'category': 'artifact', 'provenance': []}}]
            catalog.submit(root, records, actor='synthetic-runner', request_id='retain',
                           expected_state=state['state_token'], reason='Structural preservation test')
            db = catalog.locations(root)[1]
            before = db.read_bytes()
            state = catalog.status(root)
            with self.assertRaises(catalog.CatalogError):
                catalog.submit(root, [{'id': 'E-one', 'kind': 'entity', 'data': entity()}],
                               actor='synthetic-runner', request_id='refused',
                               expected_state=state['state_token'], reason='Structural refusal test')
            self.assertEqual(db.read_bytes(), before)
            with self.assertRaises(catalog.CatalogError):
                catalog.initialize(root)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(catalog.status(root)['state_token'], state['state_token'])
            self.assertEqual(catalog.status(root)['records'], 1)

    def test_release_includes_contract_without_instance_data(self):
        names = package.load_release_manifest()
        self.assertTrue({'tools/business_context.py', 'tests/test_business_context.py',
                         'docs/contracts/business-context.md',
                         'docs/decisions/0010-business-context-contract.md'} <= set(names))
        with TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'release.zip'
            result = subprocess.run([sys.executable, '-m', 'tools.package', '--output', str(archive)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with ZipFile(archive) as zipfile:
                self.assertEqual(set(zipfile.namelist()), set(names))
            clone = root / 'clone'
            clone_release(clone)
            result = subprocess.run([sys.executable, '-c',
                "from tools.business_context import validate_bundle; validate_bundle(dict(contract_version=1, entities=[], claims=[], identity_decisions=[]))"],
                cwd=clone, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
