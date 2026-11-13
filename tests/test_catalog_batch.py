"""One catalog revision per batch of documents (Kata herq): idempotency, rebase, refusal, crash."""
from contextlib import closing
import itertools
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.catalog.contracts import CatalogError, validate_graph
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import store

ROOT = Path(__file__).resolve().parents[1]


def member(n, pin, **extra):
    return {"marker": f"A-upload-{n}-original", "records": registration(n), "expected_state": pin, **extra}


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.home, self.db, self.backups = catalog.locations(self.root)
        clock = itertools.count(1)
        patcher = patch.object(catalog, "now", lambda: f"2026-01-01T00:{next(clock) // 60:02d}:{next(clock) % 60:02d}+00:00")
        patcher.start()
        self.addCleanup(patcher.stop)

    def pin(self):
        return catalog.status(self.root)["state_token"]

    def batch(self, numbers, **kwargs):
        pin = kwargs.pop("pin", None) or self.pin()
        return catalog.submit_batch(self.root, [member(n, pin) for n in numbers], actor="a", reason="batch",
                                    **kwargs)

    def single(self, n, **kwargs):
        kwargs.setdefault("expected_state", self.pin())
        return catalog.submit(self.root, registration(n), actor="a", request_id=f"r{n}", reason="t", **kwargs)

    def revisions(self):
        with closing(catalog.connect(self.db)) as c:
            return [dict(r) for r in c.execute("SELECT * FROM revisions ORDER BY revision")]

    def current(self):
        with closing(catalog.connect(self.db)) as c:
            c.execute("BEGIN")
            return {k: v for k, v in catalog.inspect(c)[1].items()}

    def statuses(self, result):
        return {m["marker"]: m["status"] for m in result["members"]}


class OneRevisionPerBatch(Case):
    def test_a_batch_commits_every_member_in_one_revision_with_an_unchanged_chain(self):
        result = self.batch(range(5))
        self.assertEqual(len(self.revisions()), 1)
        self.assertEqual(set(self.statuses(result).values()), {"committed"})
        self.assertEqual({m["committed_revision"] for m in result["members"]}, {1})
        self.assertEqual(self.revisions()[0]["record_count"], 20)
        audit = catalog.status(self.root, audit=True)
        self.assertEqual((audit["revision"], audit["records"]), (1, 20))
        self.assertEqual(result["members"][0]["committed_state"], audit)
        self.assertEqual(result["state"], audit)

    def test_the_resulting_catalog_equals_committing_the_members_one_by_one(self):
        self.batch(range(6))
        batched = self.current()
        other = Case("run")
        other.setUp()
        self.addCleanup(other.tearDown)
        for n in range(6):
            other.single(n)
        self.assertEqual(batched, other.current())
        self.assertEqual(len(other.revisions()), 6)

    def test_the_batch_is_valid_to_replay_with_lookup_indexes_and_a_snapshot(self):
        self.batch(range(3))
        self.batch(range(3, 6))
        self.assertEqual(catalog.status(self.root, audit=True)["revision"], 2)
        snapshot = Path(catalog.backup(self.root)["snapshot"])
        self.assertEqual(catalog.verify_backup(snapshot)["revision"], 2)
        self.assertEqual(catalog.lookup_shape(catalog.connect(self.db)), "indexed")

    def test_a_batch_writes_one_daily_snapshot_and_one_receipt(self):
        for path in self.backups.iterdir():
            path.unlink()  # a new UTC day: no snapshot yet
        result = self.batch(range(4))
        self.assertIsNotNone(result["backup"])
        self.assertEqual(catalog.verify_backup(Path(result["backup"]["snapshot"]))["revision"], 0)
        self.assertIsNone(self.batch(range(4, 8))["backup"])
        self.assertEqual(len([p for p in self.backups.iterdir() if p.suffix == ".sqlite3"]), 1)

    def test_old_single_revision_history_and_batches_mix_in_one_catalog(self):
        self.single(0)
        self.single(1)
        result = self.batch(range(2, 5))
        self.assertEqual([r["revision"] for r in self.revisions()], [1, 2, 3])
        self.assertEqual({m["committed_revision"] for m in result["members"]}, {3})
        self.single(5)
        self.assertEqual(catalog.status(self.root, audit=True)["revision"], 4)

    def test_empty_or_duplicate_marker_input_is_refused_before_any_write(self):
        with self.assertRaises(CatalogError):
            catalog.submit_batch(self.root, [], actor="a", reason="r")
        pin = self.pin()
        with self.assertRaises(CatalogError):
            catalog.submit_batch(self.root, [member(0, pin), member(0, pin)], actor="a", reason="r")
        self.assertEqual(self.revisions(), [])


class Idempotency(Case):
    def test_repeating_a_committed_batch_writes_nothing_and_reports_the_same_revision(self):
        first = self.batch(range(4))
        again = self.batch(range(4), pin="stale")
        self.assertEqual(len(self.revisions()), 1)
        self.assertEqual(set(self.statuses(again).values()), {"present"})
        self.assertEqual([m["committed_revision"] for m in again["members"]],
                         [m["committed_revision"] for m in first["members"]])
        self.assertEqual([m["committed_state"] for m in again["members"]],
                         [m["committed_state"] for m in first["members"]])

    def test_a_retry_builds_the_next_batch_from_members_not_yet_present(self):
        self.batch(range(3))
        result = self.batch(range(5))
        self.assertEqual(self.statuses(result), {**{f"A-upload-{n}-original": "present" for n in range(3)},
                                                  **{f"A-upload-{n}-original": "committed" for n in (3, 4)}})
        revisions = self.revisions()
        self.assertEqual([r["record_count"] for r in revisions], [12, 8])
        self.assertEqual(len(self.current()), 20)

    def test_a_member_committed_by_the_one_at_a_time_path_is_present(self):
        self.single(0)
        result = self.batch([0, 1])
        self.assertEqual(self.statuses(result), {"A-upload-0-original": "present", "A-upload-1-original": "committed"})
        self.assertEqual(result["members"][0]["committed_revision"], 1)

    def test_a_present_member_stays_present_after_its_records_were_rewritten_later(self):
        self.batch([0])
        rewritten = registration(0)
        rewritten[2]["data"]["extraction"] = "existing-rendition"
        catalog.submit(self.root, [rewritten[2]], actor="a", request_id="later", reason="t",
                       expected_state=self.pin())
        result = self.batch([0])
        self.assertEqual(self.statuses(result), {"A-upload-0-original": "present"})
        self.assertEqual(result["members"][0]["committed_revision"], 1)
        self.assertEqual(len(self.revisions()), 2)

    def test_the_marker_existing_with_other_content_refuses_that_member_only(self):
        self.batch([0])
        clash = member(0, self.pin())
        clash["records"][2]["data"]["bytes"] = 999
        result = catalog.submit_batch(self.root, [clash, member(1, self.pin())], actor="a", reason="r")
        self.assertEqual(self.statuses(result), {"A-upload-0-original": "refused", "A-upload-1-original": "committed"})
        self.assertIn("different", str(result["members"][0]["error"]))
        self.assertEqual(len(self.revisions()), 2)


class SingleSubmitMarker(Case):
    """The one-at-a-time path stays valid beside batches: a marker already committed by a batch is idempotent."""

    def test_a_document_committed_by_a_batch_is_idempotent_for_the_single_path_under_a_stale_pin(self):
        old = self.pin()
        first = self.batch([0, 1])
        again = catalog.submit(self.root, registration(1), actor="a", request_id="upload-one", reason="t",
                               expected_state=old, rebase=True, marker="A-upload-1-original")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["committed_revision"], 1)
        self.assertEqual(again["committed_state"], first["members"][1]["committed_state"])
        self.assertEqual(len(self.revisions()), 1)

    def test_without_the_marker_the_same_retry_is_the_conflict_the_marker_exists_to_avoid(self):
        old = self.pin()
        self.batch([0, 1])
        with self.assertRaises(catalog.ConflictingWrite):
            catalog.submit(self.root, registration(1), actor="a", request_id="upload-one", reason="t",
                           expected_state=old, rebase=True)

    def test_a_marker_with_other_content_is_refused_not_treated_as_present(self):
        self.batch([0])
        other = registration(0)
        other[2]["data"]["bytes"] = 5
        with self.assertRaisesRegex(CatalogError, "different content"):
            catalog.submit(self.root, other, actor="a", request_id="x", reason="t",
                           expected_state=self.pin(), marker="A-upload-0-original")

    def test_an_absent_marker_commits_normally(self):
        result = catalog.submit(self.root, registration(0), actor="a", request_id="x", reason="t",
                                expected_state=self.pin(), marker="A-upload-0-original")
        self.assertFalse(result["idempotent"])


class RebaseAndRefusal(Case):
    def test_each_member_rebases_over_unrelated_later_revisions_with_its_own_pin(self):
        old = self.pin()
        self.single(0)
        newer = self.pin()
        self.single(1)
        result = catalog.submit_batch(self.root, [member(2, old), member(3, newer), member(4, self.pin())],
                                      actor="a", reason="r")
        self.assertEqual(set(self.statuses(result).values()), {"committed"})
        self.assertEqual([m["rebased_from"] for m in result["members"]], [0, 1, None])

    def test_a_member_whose_records_a_later_revision_wrote_is_refused_and_the_rest_commit(self):
        old = self.pin()
        self.single(1)
        result = catalog.submit_batch(self.root, [member(0, old), member(1, old), member(2, old)],
                                      actor="a", reason="r")
        self.assertEqual(self.statuses(result), {"A-upload-0-original": "committed",
                                                  "A-upload-1-original": "present",
                                                  "A-upload-2-original": "committed"})
        changed = member(1, old)
        changed["marker"] = "S1"
        changed["records"] = [registration(1)[0]]
        result = catalog.submit_batch(self.root, [changed, member(3, self.pin())], actor="a", reason="r")
        self.assertEqual(self.statuses(result), {"S1": "present", "A-upload-3-original": "committed"})

    def test_overlapping_later_write_is_a_conflict_not_a_stale_pin(self):
        old = self.pin()
        self.single(7)
        overlap = member(9, old)
        overlap["records"].append({"id": "S7", "kind": "source", "data": {"category": "artifact", "provenance": []}})
        result = catalog.submit_batch(self.root, [overlap, member(8, old)], actor="a", reason="r")
        self.assertIsInstance(result["members"][0]["error"], catalog.ConflictingWrite)
        self.assertIn("later revision", str(result["members"][0]["error"]))
        self.assertEqual(result["members"][0]["status"], "refused")
        self.assertEqual(result["members"][1]["status"], "committed")

    def test_without_rebase_an_outdated_pin_refuses_only_that_member(self):
        old = self.pin()
        self.single(0)
        result = catalog.submit_batch(self.root, [member(1, old), member(2, self.pin())], actor="a",
                                      reason="r", rebase=False)
        self.assertIsInstance(result["members"][0]["error"], catalog.StaleState)
        self.assertEqual(result["members"][1]["status"], "committed")

    def test_a_pin_outside_the_history_is_refused_for_that_member(self):
        result = catalog.submit_batch(self.root, [member(0, "not-a-state"), member(1, self.pin())],
                                      actor="a", reason="r")
        self.assertIsInstance(result["members"][0]["error"], catalog.ConflictingWrite)
        self.assertEqual(result["members"][1]["status"], "committed")

    def test_a_pin_at_the_empty_catalog_rebases(self):
        empty = self.pin()
        self.single(0)
        result = catalog.submit_batch(self.root, [member(1, empty)], actor="a", reason="r")
        self.assertEqual(result["members"][0]["rebased_from"], 0)


class FailedMemberDoesNotBlockTheBatch(Case):
    def bad(self, n, pin):
        broken = member(n, pin)
        broken["records"][1]["data"]["source_ids"] = ["S-missing-0"]
        return broken

    def test_a_member_with_an_unresolvable_reference_is_refused_and_the_others_commit(self):
        pin = self.pin()
        members = [member(0, pin), self.bad(1, pin), member(2, pin), member(3, pin)]
        result = catalog.submit_batch(self.root, members, actor="a", reason="r")
        self.assertEqual([m["status"] for m in result["members"]], ["committed", "refused", "committed", "committed"])
        self.assertIsInstance(result["members"][1]["error"], CatalogError)
        self.assertEqual(len(self.current()), 12)
        catalog.status(self.root, audit=True)

    def test_members_are_validated_together_unless_the_union_is_refused(self):
        pin = self.pin()
        with patch.object(catalog, "validate_delta", wraps=catalog.validate_delta) as delta:
            catalog.submit_batch(self.root, [member(n, pin) for n in range(8)], actor="a", reason="r")
        self.assertEqual(delta.call_count, 1)
        self.assertEqual(len(self.revisions()), 1)

    def test_members_that_conflict_with_each_other_commit_in_order_and_refuse_the_later(self):
        pin = self.pin()
        first, second = member(0, pin), member(1, pin)
        for record in second["records"]:
            if record["kind"] == "artifact" and record["data"]["role"] == "original":
                record["data"]["path"] = first["records"][2]["data"]["path"]
        result = catalog.submit_batch(self.root, [first, second, member(2, pin)], actor="a", reason="r")
        self.assertEqual([m["status"] for m in result["members"]], ["committed", "refused", "committed"])
        self.assertIn("share path", str(result["members"][1]["error"]))
        self.assertEqual([r["revision"] for r in self.revisions()], [1])
        self.assertEqual(self.revisions()[0]["record_count"], 8)

    def test_members_may_reference_each_other_inside_one_batch(self):
        pin = self.pin()
        parent = member(0, pin)
        child = member(1, pin)
        child["records"].append({"id": "R-child-of-0", "kind": "relationship", "data": {
            "from_id": "A-upload-1-original", "to_id": "A-upload-0-original", "relation": "version-of",
            "scope": "t", "basis": "reported", "provenance": []}})
        result = catalog.submit_batch(self.root, [child, parent], actor="a", reason="r")
        self.assertEqual(self.statuses(result), {"A-upload-1-original": "committed", "A-upload-0-original": "committed"})
        self.assertEqual(len(self.revisions()), 1)

    def test_an_invalid_member_input_is_refused_alone(self):
        pin = self.pin()
        broken = member(0, pin)
        broken["records"][0]["kind"] = "nonsense"
        result = catalog.submit_batch(self.root, [broken, member(1, pin)], actor="a", reason="r")
        self.assertEqual([m["status"] for m in result["members"]], ["refused", "committed"])

    def test_accepted_set_equals_committing_members_one_by_one_in_order(self):
        rng = random.Random(20261113)
        for round_ in range(12):
            batch_case, sequential = Case("run"), Case("run")
            for case in (batch_case, sequential):
                case.setUp()
                self.addCleanup(case.tearDown)
            members = []
            for n in range(rng.randint(2, 7)):
                m = member(n, None)
                if rng.random() < .3:
                    m["records"][1]["data"]["source_ids"] = [rng.choice(["S-missing", f"S{rng.randint(0, n)}"])]
                if rng.random() < .3:
                    m["records"][2]["data"]["path"] = f"originals/uploads/s{rng.randint(0, 3)}/note.md"
                members.append(m)
            pin = batch_case.pin()
            for m in members:
                m["expected_state"] = pin
            result = catalog.submit_batch(batch_case.root, members, actor="a", reason="r")
            expected = []
            for m in members:
                try:
                    catalog.submit(sequential.root, m["records"], actor="a", request_id="m-" + m["marker"],
                                   reason="r", expected_state=sequential.pin())
                    expected.append("committed")
                except CatalogError:
                    expected.append("refused")
            self.assertEqual([m["status"] for m in result["members"]], expected, round_)
            self.assertEqual(batch_case.current(), sequential.current(), round_)


KILL = """
import os, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from tools.catalog import catalog_store as c
from tests.test_catalog_batch import member
root = Path({root!r})
pin = c.status(root)["state_token"]
point = {point!r}
if point == "before-commit":
    real_connect = c.connect

    def connect(path, write=False):
        connection = real_connect(path, write)
        connection.execute("PRAGMA cache_size=2")  # spill pages so a rollback journal exists at the kill
        return connection
    c.connect = connect
    hook = lambda: os._exit(9)
else:
    hook = None
    if point == "after-commit":
        c._attest_commit = lambda *a, **k: os._exit(9)
    elif point == "after-attest":
        real = c._attest_commit
        def attested(*a, **k):
            real(*a, **k)
            os._exit(9)
        c._attest_commit = attested
c.submit_batch(root, [member(n, pin) for n in range(6)], actor="a", reason="r", precommit_check=hook)
"""


class CrashInjection(Case):
    def kill_at(self, point):
        script = KILL.format(repo=str(ROOT), root=str(self.root), point=point)
        process = subprocess.run([sys.executable, "-c", script], cwd=ROOT)
        self.assertEqual(process.returncode, 9)

    def rerun_completes_exactly_once(self):
        result = self.batch(range(6))
        self.assertEqual(len(self.current()), 24)
        markers = [r["record_id"] for r in self.versions()]
        self.assertEqual(len(markers), len(set(markers)))  # no record written twice
        self.assertEqual(len(self.revisions()), 1)
        self.assertEqual(catalog.status(self.root, audit=True)["records"], 24)
        return result

    def versions(self):
        with closing(catalog.connect(self.db)) as c:
            return [dict(r) for r in c.execute("SELECT * FROM versions")]

    def test_killed_before_commit_leaves_nothing_and_the_rerun_commits_once(self):
        self.kill_at("before-commit")
        journal = Path(str(self.db) + "-journal")
        self.assertTrue(journal.exists())  # the kill left SQLite's rollback journal behind
        with self.assertRaisesRegex(CatalogError, "sidecar present"):
            self.batch(range(6))  # like any killed writer, it needs the explicit recovery
        catalog.migrate_indexes(self.root, recover=True)
        self.assertFalse(journal.exists())
        self.assertEqual(self.revisions(), [])
        self.assertEqual(set(self.statuses(self.rerun_completes_exactly_once()).values()), {"committed"})

    def test_killed_after_commit_before_attestation_reports_every_member_present_on_the_rerun(self):
        self.kill_at("after-commit")
        self.assertEqual(len(self.revisions()), 1)
        result = self.batch(range(6), pin="outdated")
        self.assertEqual(set(self.statuses(result).values()), {"present"})
        self.assertEqual(len(self.revisions()), 1)
        self.assertEqual(len(self.current()), 24)
        self.assertEqual(catalog.status(self.root, audit=True)["revision"], 1)

    def test_killed_after_attestation_reports_every_member_present_on_the_rerun(self):
        self.kill_at("after-attest")
        result = self.batch(range(6), pin="outdated")
        self.assertEqual(set(self.statuses(result).values()), {"present"})
        self.assertEqual(len(self.revisions()), 1)

    def test_a_failing_guard_inside_the_transaction_rolls_the_whole_batch_back(self):
        def refuse():
            raise CatalogError("guard refused")
        with self.assertRaises(CatalogError):
            self.batch(range(6), precommit_check=refuse)
        self.assertEqual(self.revisions(), [])
        self.assertEqual(set(self.statuses(self.rerun_completes_exactly_once()).values()), {"committed"})

    def test_a_half_applied_member_list_is_impossible_because_the_transaction_is_atomic(self):
        real = catalog._commit_revision
        calls = []

        def failing(writer, incoming, **kwargs):
            calls.append(len(incoming))
            original = writer.connection.execute

            class Spy:
                def __init__(self, inner):
                    self.inner, self.inserts = inner, 0

                def __getattr__(self, name):
                    return getattr(self.inner, name)

                def execute(self, sql, *args):
                    if sql.startswith("INSERT INTO versions"):
                        self.inserts += 1
                        if self.inserts == 11:
                            raise catalog.sqlite3.OperationalError("injected between members")
                    return self.inner.execute(sql, *args)
            writer.connection = Spy(writer.connection)
            return real(writer, incoming, **kwargs)
        with patch.object(catalog, "_commit_revision", failing):
            with self.assertRaises(catalog.sqlite3.OperationalError):
                self.batch(range(6))
        self.assertEqual(self.revisions(), [])
        self.assertEqual(self.current(), {})
        self.rerun_completes_exactly_once()


class Contention(Case):
    def test_two_processes_batching_distinct_documents_commit_every_document_once(self):
        script = (
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
            "from tools.catalog import catalog_store as c\nfrom tests.test_catalog_batch import member\n"
            "root = Path(%r)\nbase = int(sys.argv[1])\nfor start in range(base, base + 40, 8):\n"
            "    pin = c.status(root)['state_token']\n"
            "    c.submit_batch(root, [member(n, pin) for n in range(start, start + 8)], actor='a', reason='r')\n"
            % (str(ROOT), str(self.root)))
        processes = [subprocess.Popen([sys.executable, "-c", script, str(base)], cwd=ROOT) for base in (0, 100)]
        self.assertEqual([p.wait(timeout=300) for p in processes], [0, 0])
        self.assertEqual(len(self.current()), 80 * 4 // 1)
        self.assertEqual(sum(r["record_count"] for r in self.revisions()), 320)
        self.assertEqual(catalog.status(self.root, audit=True)["records"], 320)

    def test_two_processes_batching_the_same_documents_commit_each_once(self):
        script = (
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
            "from tools.catalog import catalog_store as c\nfrom tests.test_catalog_batch import member\n"
            "root = Path(%r)\npin = c.status(root)['state_token']\n"
            "c.submit_batch(root, [member(n, pin) for n in range(30)], actor='a', reason='r')\n"
            % (str(ROOT), str(self.root)))
        processes = [subprocess.Popen([sys.executable, "-c", script], cwd=ROOT) for _ in range(2)]
        self.assertEqual([p.wait(timeout=300) for p in processes], [0, 0])
        self.assertEqual(len(self.revisions()), 1)
        self.assertEqual(len(self.current()), 120)


if __name__ == "__main__":
    unittest.main()
