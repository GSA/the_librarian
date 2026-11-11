"""Catalog writes validate only their delta on a trusted head (fg46), keeping every integrity gate."""
from contextlib import closing
import itertools
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import backups as snapshots
from tools.catalog import catalog_store as catalog
from tools.catalog.contracts import CatalogError, validate_delta, validate_graph
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import store
from tests.test_validation_cost import artifact, receipt, relationship, source

ROOT = Path(__file__).resolve().parents[1]


class DeltaMatchesFullValidation(unittest.TestCase):
    """validate_delta(batch) refuses exactly when validate_graph(committed + batch) does."""

    @staticmethod
    def accessors(committed):
        def get(record_id):
            return committed.get(record_id)

        def referrers(ids):
            wanted = set(ids)
            from tools.catalog.contracts import _references
            return [r for r in committed.values() if wanted & set(_references(r))]

        def claimants(paths):
            return [r for r in committed.values()
                    if r["kind"] == "artifact" and r["data"]["path"] in set(paths)]
        return get, referrers, claimants

    def outcomes(self, committed, batch):
        merged = dict(committed)
        merged.update((r["id"], r) for r in batch)
        try:
            validate_graph(merged)
            full = None
        except CatalogError as error:
            full = str(error)
        get, referrers, claimants = self.accessors(committed)
        try:
            validate_delta(batch, get=get, referrers=referrers, claimants=claimants)
            delta = None
        except CatalogError as error:
            delta = str(error)
        return full, delta

    def test_random_batches_agree_with_full_validation(self):
        rng = random.Random(20260511)
        universe = [f"{n}" for n in range(1, 6)]

        def random_record():
            n = rng.choice(universe)
            kind = rng.choice(("source", "artifact", "artifact", "receipt", "relationship"))
            if kind == "source":
                return source(f"S{n}")
            if kind == "receipt":
                return receipt(f"D{n}", [f"S{rng.choice(universe)}"], f"A-{rng.choice(universe)}")
            if kind == "relationship":
                return relationship(f"R-{n}", f"A-{rng.choice(universe)}", rng.choice(
                    (f"A-{rng.choice(universe)}", f"S{rng.choice(universe)}")))
            return artifact(f"A-{n}", path=rng.choice((None, f"p/{rng.choice(universe)}")),
                            parent=rng.choice((None, None, f"A-{rng.choice(universe)}")),
                            receipt=rng.choice((None, f"D{rng.choice(universe)}")))
        checked = accepted = 0
        for _ in range(4000):
            committed = {}
            for _ in range(rng.randint(0, 3)):  # grow a valid committed state
                batch = list({r["id"]: r for r in (random_record() for _ in range(rng.randint(1, 4)))}.values())
                merged = {**committed, **{r["id"]: r for r in batch}}
                try:
                    validate_graph(merged)
                except CatalogError:
                    continue
                committed = merged
            batch = list({r["id"]: r for r in (random_record() for _ in range(rng.randint(1, 4)))}.values())
            full, delta = self.outcomes(committed, batch)
            self.assertEqual(full is None, delta is None, (committed, batch, full, delta))
            checked += 1
            accepted += full is None
        self.assertGreater(accepted, 100)  # both branches are actually exercised
        self.assertGreater(checked - accepted, 100)

    def test_named_violation_classes_are_refused_with_the_same_message(self):
        base = {r["id"]: r for r in [source("S1"), receipt("D1", ["S1"], "A-1"),
                                     artifact("A-1", path="p/1", receipt="D1")]}
        cases = {
            "backlink broken": [artifact("A-1", path="p/1", receipt="D9")],
            "duplicate path": [artifact("A-2", path="p/1")],
            "duplicate path in batch": [artifact("A-2", path="q"), artifact("A-3", path="q")],
            "missing parent": [artifact("A-2", parent="A-9")],
            "cycle": [artifact("A-2", parent="A-3"), artifact("A-3", parent="A-2")],
            "dangling relationship": [relationship("R-1", "A-1", "A-9")],
            "parent receipt mismatch": [artifact("A-2", parent="A-1", receipt="D2")],
        }
        for name, batch in cases.items():
            with self.subTest(name):
                full, delta = self.outcomes(base, batch)
                self.assertIsNotNone(full)
                self.assertIsNotNone(delta)
                if name != "backlink broken":  # several rules apply; the first reported may differ
                    self.assertEqual(full, delta)

    def test_moving_a_path_within_one_batch_is_accepted(self):
        base = {r["id"]: r for r in [artifact("A-1", path="p"), artifact("A-2", path="q")]}
        swap = [artifact("A-1", path="q"), artifact("A-2", path="p")]
        self.assertEqual(self.outcomes(base, swap), (None, None))


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.home, self.db, self.backups = catalog.locations(self.root)
        self.receipt = self.home / ".local" / "read-validation.json"
        self.clock = itertools.count(1)
        patcher = patch.object(catalog, "now", lambda: f"2026-01-01T00:00:{next(self.clock):02d}+00:00")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, n, **kwargs):
        if "expected_state" not in kwargs:
            kwargs["expected_state"] = catalog.status(self.root)["state_token"]
        return catalog.submit(self.root, registration(n), actor="a", request_id=f"r{n}",
                              reason="test", **kwargs)

    def full_inspection(self):
        with closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            return catalog.inspect(connection)

    def writes_inspect(self):
        """Count full replays a block of work performs."""
        calls = []
        real = catalog.inspect

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        return calls, patch.object(catalog, "inspect", counting)

    def mutate(self, sql, *params):
        """Edit the database outside the writer, as an attacker or a bad restore would."""
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            for table in ("metadata", "revisions", "versions"):
                for action in ("update", "delete"):
                    connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
            connection.execute(sql, params)
        os.chmod(self.db, 0o600)


class FastPathEquivalence(Case):
    def test_delta_commits_equal_full_replay_commits(self):
        """Same fixture through both paths: identical state tokens, hash chain, records and graph."""
        results = {}
        for mode in ("delta", "full"):
            self.setUp()
            tokens = []
            for n in range(8):
                if mode == "full" and self.receipt.exists():
                    self.receipt.unlink()  # no receipt: every write replays the full history
                tokens.append(self.write(n)["revision"])
            state, current, histories, batches, _ = self.full_inspection()
            results[mode] = (state["revision"], state["records"], state["head_hash"], current, histories, batches)
        self.assertEqual(results["delta"], results["full"])

    def test_fast_path_skips_full_replay_after_the_first_write(self):
        self.write(0)
        calls, patched = self.writes_inspect()
        with patched:
            for n in range(1, 4):
                self.write(n)
        self.assertEqual(calls, [])
        self.assertEqual(self.full_inspection()[0], catalog.status(self.root, audit=True))

    def test_audit_and_full_inspection_accept_the_fast_path_history(self):
        for n in range(6):
            self.write(n)
        self.assertEqual(catalog.status(self.root, audit=True)["revision"], 6)
        self.assertEqual(self.full_inspection()[0]["records"], 24)

    def test_overwriting_a_record_counts_it_once(self):
        self.write(0)
        records = registration(0)
        records[3]["data"]["reason"] = "Changed."
        result = catalog.submit(self.root, records, actor="a", request_id="again", reason="edit",
                                expected_state=catalog.status(self.root)["state_token"])
        self.assertEqual(result["records"], 4)
        self.assertEqual(self.full_inspection()[0], catalog.status(self.root, audit=True))

    def test_invalid_graph_is_refused_and_leaves_the_catalog_unchanged(self):
        self.write(0)
        before = catalog.status(self.root)
        broken = registration(1)
        broken[1]["data"]["artifact_id"] = "A-upload-nowhere-original"
        with self.assertRaises(CatalogError):
            catalog.submit(self.root, broken, actor="a", request_id="bad", reason="x",
                           expected_state=before["state_token"])
        duplicate = registration(2)
        duplicate[2]["data"]["path"] = registration(0)[2]["data"]["path"]
        with self.assertRaisesRegex(CatalogError, "share path"):
            catalog.submit(self.root, duplicate, actor="a", request_id="dup", reason="x",
                           expected_state=before["state_token"])
        self.assertEqual(catalog.status(self.root), before)


class TamperDetection(Case):
    def setUp(self):
        super().setUp()
        for n in range(3):
            self.write(n)
        self.assertTrue(self.receipt.exists())  # the fast path is armed

    def assert_refused(self):
        with self.assertRaises(CatalogError):
            self.write(10)
        with self.assertRaises(CatalogError):
            catalog.status(self.root)
        with self.assertRaises(CatalogError):
            catalog.read_records(self.root, limit=5)

    def test_edited_payload_is_refused(self):
        self.mutate("UPDATE versions SET payload=replace(payload,'Synthetic.','Edited.') WHERE record_id='S1'"
                    " OR record_id LIKE 'A-upload-1-%'")
        self.assert_refused()

    def test_rewritten_history_row_is_refused(self):
        self.mutate("UPDATE revisions SET reason='rewritten' WHERE revision=2")
        self.assert_refused()

    def test_deleted_revision_is_refused(self):
        self.mutate("DELETE FROM current_records WHERE record_id='S2'")
        self.assert_refused()

    def test_projection_edit_is_refused(self):
        self.mutate("UPDATE current_records SET ordinal=ordinal+1 WHERE record_id='D1'")
        self.assert_refused()

    def test_inserted_unchained_revision_is_refused(self):
        self.mutate("INSERT INTO revisions VALUES (4,'forged','a','x','2026-01-01T00:00:00+00:00','00',1,'00','00')")
        self.assert_refused()

    def test_forged_receipt_for_other_bytes_does_not_vouch_for_tampered_bytes(self):
        saved = self.receipt.read_text()
        self.mutate("UPDATE versions SET payload_hash='0' WHERE record_id='S0'")
        self.receipt.write_text(saved)
        self.assert_refused()

    def forge_receipt_for_current_bytes(self):
        """An actor who can rewrite the database and the unkeyed receipt makes both agree."""
        with closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            binding = catalog._binding(connection)
            state = catalog._head_state(connection)
        catalog._write_receipt(self.home, binding, state)

    def test_first_write_of_each_day_replays_in_full_even_with_a_matching_forged_receipt(self):
        self.mutate("UPDATE versions SET payload=replace(payload,'Synthetic.','Edited.') WHERE record_id='S1'"
                    " OR record_id LIKE 'A-upload-1-%'")
        self.forge_receipt_for_current_bytes()
        for path in self.backups.iterdir():
            path.unlink()  # a new UTC day: the write must take a verified snapshot, which replays history
        with self.assertRaises(CatalogError):
            self.write(12)

    def test_documented_residual_forged_receipt_is_trusted_until_the_next_daily_snapshot(self):
        self.mutate("UPDATE versions SET payload=replace(payload,'Synthetic.','Edited.') WHERE record_id='S1'"
                    " OR record_id LIKE 'A-upload-1-%'")
        self.forge_receipt_for_current_bytes()
        self.assertEqual(self.write(13)["committed_revision"], 4)  # same residual 0012 accepts for readers
        with self.assertRaises(CatalogError):
            catalog.status(self.root, audit=True)  # an audit still finds it

    def test_corrupt_receipt_costs_a_full_replay_but_never_blocks_a_write(self):
        pinned = catalog.status(self.root)["state_token"]
        self.receipt.write_text("{not json")
        calls, patched = self.writes_inspect()
        with patched:
            self.write(11, expected_state=pinned)
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(self.full_inspection()[0], catalog.status(self.root, audit=True))

    def test_restored_older_bytes_are_replayed_in_full(self):
        snapshot = Path(catalog.backup(self.root)["snapshot"])
        self.write(20)
        calls, patched = self.writes_inspect()
        with patched, closing(sqlite3.connect(snapshot)) as source_db, \
                closing(sqlite3.connect(self.db)) as live:
            source_db.backup(live)  # bytes differ from the receipt's binding
            os.chmod(self.db, 0o600)
            self.write(21)
        self.assertGreaterEqual(len(calls), 1)


class RequestSemantics(Case):
    def test_same_request_and_input_is_idempotent_and_different_input_refused(self):
        first = self.write(0)
        again = self.write(0, expected_state="outdated")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["committed_state"], first["committed_state"])
        self.assertEqual(again["committed_revision"], 1)
        self.write(1)
        late = self.write(0, expected_state="outdated")  # an older request still resolves to its state
        self.assertEqual(late["committed_state"], first["committed_state"])
        changed = registration(0)
        changed[0]["data"]["provenance"] = [{"path": "x/y", "sha256": None, "line_start": None, "line_end": None}]
        with self.assertRaisesRegex(CatalogError, "reused"):
            catalog.submit(self.root, changed, actor="a", request_id="r0", reason="test",
                           expected_state=catalog.status(self.root)["state_token"])

    def test_rebase_accepts_disjoint_and_refuses_overlapping_writes(self):
        self.write(0)
        pinned = catalog.status(self.root)["state_token"]
        self.write(1)
        disjoint = catalog.submit(self.root, registration(2), actor="a", request_id="r2", reason="t",
                                  expected_state=pinned, rebase=True)
        self.assertEqual(disjoint["rebased_from"], 1)
        with self.assertRaises(catalog.StaleState) as stale:
            catalog.submit(self.root, registration(3), actor="a", request_id="r3", reason="t",
                           expected_state=pinned)
        self.assertNotIsInstance(stale.exception, catalog.ConflictingWrite)
        with self.assertRaises(catalog.ConflictingWrite):
            catalog.submit(self.root, registration(1), actor="a", request_id="other", reason="t",
                           expected_state=pinned, rebase=True)
        with self.assertRaises(catalog.ConflictingWrite):
            catalog.submit(self.root, registration(4), actor="a", request_id="r4", reason="t",
                           expected_state="not-a-state", rebase=True)

    def test_pin_at_the_empty_catalog_rebases(self):
        empty = catalog.status(self.root)["state_token"]
        self.write(0)
        result = catalog.submit(self.root, registration(1), actor="a", request_id="r1", reason="t",
                                expected_state=empty, rebase=True)
        self.assertEqual(result["rebased_from"], 0)


class CrashSafety(Case):
    def test_failure_before_commit_leaves_the_prior_revision_and_retries_cleanly(self):
        self.write(0)
        before = catalog.status(self.root)

        def fail():
            raise CatalogError("guard refused")
        with self.assertRaises(CatalogError):
            self.write(1, precommit_check=fail)
        self.assertEqual(catalog.status(self.root), before)
        self.assertEqual(self.write(1)["committed_revision"], 2)
        self.assertEqual(catalog.status(self.root, audit=True)["revision"], 2)

    def test_killed_after_commit_before_attestation_validates_in_full_then_continues(self):
        self.write(0)
        script = (
            "import sys, os\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
            "from tools.catalog import catalog_store as c\nfrom tools.diagnostics.catalog_lock_benchmark import registration\n"
            "root = Path(%r)\nc._attest_commit = lambda *a, **k: os._exit(9)\n"
            "c.submit(root, registration(1), actor='a', request_id='r1', reason='t',\n"
            "         expected_state=c.status(root)['state_token'])\n" % (str(ROOT), str(self.root)))
        process = subprocess.run([sys.executable, "-c", script], cwd=ROOT)
        self.assertEqual(process.returncode, 9)
        calls, patched = self.writes_inspect()
        with patched:
            state = catalog.status(self.root)  # the stale receipt covers older bytes: full replay
            self.assertEqual(state["revision"], 2)
            self.assertGreaterEqual(len(calls), 1)
            retry = catalog.submit(self.root, registration(1), actor="a", request_id="r1", reason="t",
                                   expected_state="outdated")  # committed before the kill
            self.assertTrue(retry["idempotent"])
            self.assertEqual(self.write(2)["committed_revision"], 3)
        self.assertEqual(self.full_inspection()[0]["revision"], 3)


class SnapshotsUnchanged(Case):
    def test_first_write_of_a_day_takes_one_verified_snapshot_on_the_fast_path(self):
        for path in self.backups.iterdir():
            path.unlink()  # a new UTC day: no snapshot yet
        self.write(0)  # no receipt yet: full path
        self.assertEqual(len([p for p in self.backups.iterdir() if p.suffix == ".sqlite3"]), 1)
        result = self.write(1)  # fast path, snapshot already taken today
        self.assertIsNone(result["backup"])
        self.assertEqual(len([p for p in self.backups.iterdir() if p.suffix == ".sqlite3"]), 1)

    def test_fast_path_takes_a_missing_daily_snapshot_and_verifies_it(self):
        self.write(0)
        self.write(1)
        for path in self.backups.iterdir():
            path.unlink()
        result = self.write(2)
        self.assertIsNotNone(result["backup"])
        self.assertEqual(catalog.verify_backup(Path(result["backup"]["snapshot"]))["revision"], 2)
        self.assertTrue(snapshots.has_snapshot_today(self.backups, result["store_id"]))


if __name__ == "__main__":
    unittest.main()
