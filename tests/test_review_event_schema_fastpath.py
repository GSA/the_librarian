"""Review-event schema validation: the inlined fast path accepts exactly what the full walk accepts (Kata 17b0).

The fast path is only ever allowed to say "valid"; every other verdict is the full jsonschema walk,
which reports the exact error. These tests compare the two verdicts over many single-field mutations
and pin the memo to a fixed-size key.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from tests.test_review_event_memo import event
from tools.review_events import event_history, review_events as contract

ROOT = Path(__file__).resolve().parents[1]


class FastPathCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        contract._SCHEMA_VALID.clear()
        self.addCleanup(contract._SCHEMA_VALID.clear)
        self.schema = event_history.store_schema(self.root)

    def bundle(self, *events):
        return event_history.bundle(list(events), self.root)

    def full_verdict(self, value):
        """Verdict and message of the unoptimized walk over the whole bundle."""
        contract._SCHEMA_VALID.clear()
        with patch.object(contract, "_fast_item_validator", lambda *args, **kwargs: None):
            try:
                contract.schema_validate(value, self.schema, root=self.root)
            except contract.ReviewError as error:
                return str(error)
        return None

    def fast_verdict(self, value):
        contract._SCHEMA_VALID.clear()
        try:
            contract.schema_validate(value, self.schema, root=self.root)
        except contract.ReviewError as error:
            return str(error)
        return None


class Inlining(FastPathCase):
    def test_the_item_schema_compiles_to_a_predicate_that_accepts_a_valid_event(self):
        checker = FormatChecker()
        compiled = contract._fast_item_validator(self.schema, checker)
        self.assertTrue(compiled(event(self.root, 0)))
        self.assertFalse(compiled({**event(self.root, 0), "unexpected": 1}))
        self.assertIsNone(contract._fast_item_validator(self.schema))  # format needs a checker

    def test_valid_events_cost_one_full_walk_for_the_envelope_not_one_per_event(self):
        value = self.bundle(*[event(self.root, n) for n in range(20)])
        full, walks = Draft202012Validator.iter_errors, []

        def spy(self_, instance, *args, **kwargs):
            if isinstance(instance, dict) and "events" in instance:
                walks.append(len(instance["events"]))
            return full(self_, instance, *args, **kwargs)
        with patch.object(Draft202012Validator, "iter_errors", spy):
            self.assertIsNone(self.fast_verdict(value))
        self.assertEqual(walks, [1])

    def test_unsupported_references_leave_validation_to_the_full_walk(self):
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["id"] = {"$ref": "#/$defs/text"}
        schema["$defs"]["text"] = {"$ref": "#/$defs/id"}  # a cycle cannot be inlined
        self.assertIsNone(contract._fast_item_validator(schema, FormatChecker()))
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["hash"] = {"$ref": "https://example.invalid/hash"}
        self.assertIsNone(contract._fast_item_validator(schema, FormatChecker()))
        schema = copy.deepcopy(self.schema)
        schema["$defs"]["hash"] = {"$id": "urn:other", "type": "string"}
        self.assertIsNone(contract._fast_item_validator(schema, FormatChecker()))


class Differential(FastPathCase):
    """Whatever the full walk refuses, the fast path refuses with the same message."""

    REPLACEMENTS = [None, 0, -1, 1.5, True, "", "x", " ", "x" * 5000, [], {}, ["a"], [{"a": 1}],
                    "2026-02-30", "not-a-time", "rev-UPPER", "D0", "S0", "f" * 64, "F" * 64]

    @staticmethod
    def paths(node, path=()):
        yield path
        if isinstance(node, dict):
            for key, value in node.items():
                yield from Differential.paths(value, path + (key,))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from Differential.paths(value, path + (index,))

    def mutations(self, base):
        """Every single-field deletion, replacement, addition and list duplication of ``base``."""
        for path in self.paths(base):
            if not path:
                continue
            for how in ("delete", "replace", "add", "duplicate"):
                variants = self.REPLACEMENTS if how == "replace" else [None]
                for replacement in variants:
                    value = copy.deepcopy(base)
                    parent = value
                    for step in path[:-1]:
                        parent = parent[step]
                    target = parent[path[-1]]
                    if how == "delete":
                        del parent[path[-1]]
                    elif how == "replace":
                        parent[path[-1]] = copy.deepcopy(replacement)
                    elif how == "add" and isinstance(target, dict):
                        target["extra"] = "extra"
                    elif how == "duplicate" and isinstance(target, list) and target:
                        target.append(copy.deepcopy(target[0]))
                    else:
                        continue
                    yield value

    def test_every_single_field_mutation_gets_the_full_walks_verdict(self):
        base = event(self.root, 3)
        base["relationships"] = [{"kind": "duplicate_content", "target_event_id": "rev-memo-9",
                                  "scope": "Same text.", "basis": "Identical bytes.",
                                  "independent_corroboration": False}]
        base["provenance"]["record"] = {"path": "a.json", "sha256": "a" * 64, "pointer": "/x"}
        base["source"]["checksum"] = {"sha256": "b" * 64, "basis": "reported"}
        base["supersedes"] = ["rev-memo-1"]
        base["correction_reason"] = "Because."
        base["scope"]["extent"] = "selected_ranges"
        base["scope"]["ranges"] = [{"start": 1, "end": 2}]
        checked = refused = accepted = 0
        for mutated in self.mutations(base):
            value = self.bundle(event(self.root, 0), mutated)  # the first event carries the envelope check
            expected = self.full_verdict(value)
            self.assertEqual(self.fast_verdict(value), expected, json.dumps(mutated)[:300])
            if expected is None:
                accepted += 1
            checked += 1
            refused += expected is not None
        self.assertGreater(checked, 500)
        self.assertGreater(refused, 300)
        self.assertGreater(accepted, 5)  # benign mutations (extra list item, etc.) exercise acceptance too

    def test_an_invalid_event_still_reports_the_original_error(self):
        bad = event(self.root, 1, outcome="bogus")
        with self.assertRaisesRegex(contract.ReviewError,
                                    r"Schema violation at /events/0/outcome \(enum\); input value omitted"):
            contract.schema_validate(self.bundle(bad), self.schema, root=self.root)
        later = event(self.root, 2)
        later["scope"]["extra"] = 1
        with self.assertRaisesRegex(contract.ReviewError, r"/events/1/scope \(additionalProperties\)"):
            contract.schema_validate(self.bundle(event(self.root, 1), later), self.schema, root=self.root)


class Memo(FastPathCase):
    def test_the_memo_holds_fixed_size_digests_not_event_structure(self):
        contract.schema_validate(self.bundle(*[event(self.root, n) for n in range(5)]), self.schema,
                                 root=self.root)
        keys = [key for known in contract._SCHEMA_VALID.values() for key in known]
        self.assertEqual(len(keys), 5)
        self.assertTrue(all(isinstance(key, bytes) and len(key) == 32 for key in keys))

    def test_a_known_event_is_not_validated_again_but_the_envelope_always_is(self):
        events = [event(self.root, n) for n in range(4)]
        contract.schema_validate(self.bundle(*events), self.schema, root=self.root)
        built = []
        real = contract._fast_item_validator

        def counting(*args, **kwargs):
            built.append(1)
            return real(*args, **kwargs)
        with patch.object(contract, "_fast_item_validator", counting):
            contract.schema_validate(self.bundle(*events), self.schema, root=self.root)
        self.assertEqual(built, [])
        wrong = {**self.bundle(*events), "audience": "someone-else"}
        with self.assertRaisesRegex(contract.ReviewError, r"Schema violation at /audience"):
            contract.schema_validate(wrong, self.schema, root=self.root)

    def test_events_that_differ_only_in_type_are_not_conflated(self):
        good = event(self.root, 1)
        contract.schema_validate(self.bundle(good), self.schema, root=self.root)
        for change in ({"counts": {**good["counts"], "total": True}},
                       {"counts": {**good["counts"], "total": 1.0}},
                       {"reason": None, "supersedes": ["rev-memo-1"]}):
            bad = {**good, **change}
            if self.full_verdict(self.bundle(bad)) is None:
                continue
            with self.assertRaises(contract.ReviewError):
                contract.schema_validate(self.bundle(bad), self.schema, root=self.root)
            contract._SCHEMA_VALID.clear()
            contract.schema_validate(self.bundle(good), self.schema, root=self.root)


if __name__ == "__main__":
    unittest.main()
