"""Bounded validation cost: incremental history graph checks must match full validation (2rye)."""
from copy import deepcopy
import unittest

from tools.catalog import contracts
from tools.catalog.contracts import CatalogError, GraphValidator, validate_graph


def source(identifier):
    return {"id": identifier, "kind": "source", "data": {"category": "artifact", "provenance": []}}


def artifact(identifier, *, path=None, parent=None, receipt=None):
    return {"id": identifier, "kind": "artifact", "data": {
        "path": path, "parent_id": parent, "receipt_id": receipt, "role": "original",
        "audience": "source", "coverage": "pending-extraction", "reason": "Synthetic.",
        "presence": "present" if path else "absent", "sha256": None, "hash_basis": "unknown",
        "bytes": None, "extraction": "pending", "handling_refs": [], "provenance": []}}


def receipt(identifier, sources, artifact_id):
    return {"id": identifier, "kind": "receipt", "data": {
        "source_ids": sources, "artifact_id": artifact_id, "provenance": []}}


def relationship(identifier, from_id, to_id):
    return {"id": identifier, "kind": "relationship", "data": {
        "from_id": from_id, "to_id": to_id, "relation": "version-of", "scope": "Synthetic.",
        "basis": "reported", "provenance": []}}


def replay_full(batches):
    """Reference semantics: the full graph check after every revision."""
    current = {}
    for batch in batches:
        current.update((r["id"], r) for r in batch)
        validate_graph(current)


def replay_incremental(batches):
    validator = GraphValidator()
    for batch in batches:
        validator.apply(batch)
    return validator


class IncrementalGraphTests(unittest.TestCase):
    def assertSame(self, batches, valid):
        if valid:
            replay_full(batches)
            replay_incremental(batches)
        else:
            with self.assertRaises(CatalogError):
                replay_full(batches)
            with self.assertRaises(CatalogError):
                replay_incremental(batches)

    def registration(self, n):
        return [source(f"S{n}"), receipt(f"D{n}", [f"S{n}"], f"A-{n}"),
                artifact(f"A-{n}", path=f"p/{n}", receipt=f"D{n}")]

    def test_valid_history_matches_full_validation(self):
        batches = [self.registration(1), self.registration(2),
                   [relationship("R-1", "A-1", "A-2")],
                   [artifact("A-3", path="p/3", parent="A-1", receipt="D1")],
                   [artifact("A-1", path="p/1-moved", receipt="D1")]]
        self.assertSame(batches, True)

    def test_every_violation_class_is_refused_like_full_validation(self):
        base = self.registration(1)
        cases = {
            "missing source": [[receipt("D1", ["S1"], "A-1"), artifact("A-1", path="p", receipt="D1")]],
            "missing artifact": [[source("S1"), receipt("D1", ["S1"], "A-1")]],
            "receipt backlink": [[source("S1"), receipt("D1", ["S1"], "A-1"), artifact("A-1", path="p")]],
            "relationship endpoint": [base, [relationship("R-1", "A-1", "A-9")]],
            "duplicate path": [base, [artifact("A-2", path="p/1")]],
            "missing parent": [base, [artifact("A-2", parent="A-9")]],
            "self parent": [base, [artifact("A-2", parent="A-2")]],
            "missing artifact receipt": [[artifact("A-2", receipt="D9")]],
            "parent receipt disagreement": [base, [artifact("A-2", parent="A-1", receipt="D2")]],
            "cycle": [[artifact("A-1", parent="A-2"), artifact("A-2")], [artifact("A-2", parent="A-1")]],
        }
        for name, batches in cases.items():
            with self.subTest(name):
                self.assertSame(deepcopy(batches), False)

    def test_later_change_that_breaks_an_existing_referrer_is_refused(self):
        base = self.registration(1)
        # Re-pointing the artifact's receipt breaks the receipt's backlink, a record not in the batch.
        self.assertSame([base, [artifact("A-1", path="p/1", receipt="D9")]], False)
        # Releasing a path lets another artifact take it.
        self.assertSame([base, [artifact("A-1", path="p/other", receipt="D1")],
                         [artifact("A-2", path="p/1")]], True)
        # An earlier-introduced dangling reference is refused at its own revision, not forgiven
        # because a later revision supplies the target.
        self.assertSame([[relationship("R-1", "A-1", "A-2")], [artifact("A-1"), artifact("A-2")]], False)

    def test_work_per_revision_does_not_grow_with_history(self):
        validator = GraphValidator()
        validator.apply(self.registration(0))
        costs = []
        for n in range(1, 60):
            before = validator.checked
            validator.apply(self.registration(n))
            costs.append(validator.checked - before)
        self.assertEqual(costs[0], costs[-1])
        self.assertLessEqual(max(costs), 12)


if __name__ == "__main__":
    unittest.main()
