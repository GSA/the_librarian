"""Per-file upload planning reads only the highest ID per prefix and memoizes reserved IDs (Kata 3re5)."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import runner, store, upload
from tools.intake.contracts import IntakeError

TEXT = b"Operator supplied notes.\n"


def value(sid, name="notes.txt", **extra):
    return upload.descriptor(submission_id=sid, format_name="text", name=name,
                             actor="test-runner", reason="Planning test", **extra)


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        store.initialize(self.root)
        catalog.initialize(self.root)
        upload.PROMISES.clear()
        self.addCleanup(upload.PROMISES.clear)
        upload.forget_reserved()
        self.addCleanup(upload.forget_reserved)

    def file(self, name="notes.txt", raw=TEXT):
        path = self.outside / name
        path.write_bytes(raw)
        return path

    def commit(self, *numbers, request_id="seed"):
        """Commit one upload-shaped registration (S<n>, D<n>, two artifacts) per number."""
        for number in numbers:
            catalog.submit(self.root, registration(number), actor="test-runner", request_id=f"{request_id}-{number}",
                           reason="Seed", expected_state=catalog.status(self.root)["state_token"])

    def plan(self, sid="new", **extra):
        descriptor = value(sid, **extra)
        raw = TEXT
        directory, original, sidecar_path, sidecar = upload.plan(self.root, descriptor, raw)
        return upload.records_for(self.root, descriptor, raw, original, sidecar_path, sidecar)[1]

    def ids(self, records):
        return {r["kind"]: r["id"] for r in records}


class AllocationTests(Case):
    def test_catalog_highest_matches_the_python_scan_over_awkward_ids(self):
        identifiers = ["S1", "S9", "S10", "S007", "Sx", "S", "S1a", "S-3", "D4", "D12", "Dz", "I77", "O5",
                       "S" + "9" * 18, "D" + "7" * 30, "T1", "R1"]
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE current_records(record_id TEXT PRIMARY KEY, revision INT, ordinal INT)")
            connection.executemany("INSERT INTO current_records VALUES (?,1,0)", [(i,) for i in identifiers])
            for prefix in upload.PREFIXES:
                self.assertEqual(upload.catalog_highest(connection, prefix),
                                 upload.highest_number(identifiers, prefix), prefix)
        # The rule for promised IDs is the one the planner always used (str.isdigit).
        self.assertEqual(upload.highest_number(["S1", "S\u0663", "S02"], "S"), 3)

    def test_next_ids_follow_the_catalog_and_reserved_maxima_exactly(self):
        self.commit(3, 10)
        ids = self.ids(self.plan("one"))
        self.assertEqual((ids["source"], ids["receipt"]), ("S11", "D11"))

    def test_reserved_by_a_staged_uncommitted_request_is_excluded_until_committed(self):
        with patch.object(runner, "run", side_effect=SystemExit("crash before run")), self.assertRaises(SystemExit):
            upload.submit(self.root, self.file(), value("pending"))
        self.assertEqual(store.inspect(self.root, "upload-pending")["stages"][0]["status"], "staged")
        ids = self.ids(self.plan("other"))
        self.assertEqual((ids["source"], ids["receipt"]), ("S2", "D2"))

    def test_blocked_and_dead_letter_requests_keep_their_ids_reserved(self):
        with patch.object(runner.adapters.catalog, "submit", side_effect=catalog.CatalogError("refused")):
            ack = upload.submit(self.root, self.file(), value("blocked"))
        self.assertEqual(ack["receipt"]["state"], "blocked")
        self.assertEqual(self.ids(self.plan("after-blocked"))["receipt"], "D2")
        # A second submission through the blocked one's allocation never reuses its IDs.
        again = upload.submit(self.root, self.file("b.txt", b"b\n"), value("after-blocked", "b.txt"))
        self.assertEqual(again["receipt"]["state"], "committed")
        self.assertNotEqual(again["records"]["receipt_id"], ack["records"]["receipt_id"])

    def test_retryable_request_is_reserved(self):
        with patch.object(runner.adapters.catalog, "submit", side_effect=catalog.WriterBusy("busy")):
            upload.submit(self.root, self.file(), value("retrying"))
        self.assertEqual(self.ids(self.plan("later"))["receipt"], "D2")

    def test_identifier_conflict_is_still_refused_from_the_probe(self):
        upload.submit(self.root, self.file(), value("same"))
        sid = "same"
        taken = upload.catalog_view(self.root, {f"A-upload-{sid}-original"})[1]
        self.assertEqual(taken.present, {f"A-upload-{sid}-original"})
        reserved = upload.reserved_ids(self.root, {f"A-upload-{sid}-original", "A-none"})
        self.assertEqual(reserved.present, {f"A-upload-{sid}-original"})
        directory, original, sidecar_path, sidecar = upload.plan(self.root, value(sid), TEXT)
        self.assertRaises(IntakeError, upload.records_for, self.root, value(sid), TEXT, original, sidecar_path, sidecar)

    def test_correction_parent_pending_still_detects_committed_records(self):
        with patch.object(runner.adapters.catalog, "submit", side_effect=catalog.CatalogError("refused")):
            upload.submit(self.root, self.file(), value("parent"))
        fixed = upload.submit(self.root, self.file(), value("parent-fix", correction_of="parent"))
        self.assertEqual(fixed["receipt"]["state"], "committed")


class MemoTests(Case):
    def test_second_planning_decodes_no_request_it_has_seen(self):
        for number in range(4):
            upload.submit(self.root, self.file(f"n{number}.txt", b"n%d\n" % number), value(f"s{number}", f"n{number}.txt"))
        upload.PROMISES.clear()
        upload.forget_reserved()
        upload.reserved_ids(self.root)
        with patch.object(upload, "decode", wraps=upload.decode) as decode:
            upload.reserved_ids(self.root)
        self.assertEqual(decode.call_count, 0)
        upload.submit(self.root, self.file("n9.txt", b"n9\n"), value("s9", "n9.txt"))
        with patch.object(upload, "decode", wraps=upload.decode) as decode:
            upload.reserved_ids(self.root)
        self.assertEqual(decode.call_count, 1)  # only the request added since is decoded

    def test_memo_result_equals_a_fresh_scan_in_a_new_process_state(self):
        for number in range(5):
            upload.submit(self.root, self.file(f"m{number}.txt", b"m%d\n" % number), value(f"m{number}", f"m{number}.txt"))
        warm = upload.reserved_ids(self.root, {"A-upload-m2-original"})
        upload.PROMISES.clear()
        upload.forget_reserved()
        cold = upload.reserved_ids(self.root, {"A-upload-m2-original"})
        self.assertEqual((warm.highest, warm.present), (cold.highest, cold.present))
        self.assertEqual(cold.highest, {"S": 5, "D": 5})

    def test_rewritten_request_is_a_different_key_and_is_refused(self):
        upload.submit(self.root, self.file(), value("rewrite"))
        upload.reserved_ids(self.root)
        db = self.root / ".local/intake/intake.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            raw = connection.execute("SELECT raw FROM requests WHERE correlation_id='upload-rewrite'").fetchone()[0]
            connection.execute("UPDATE requests SET raw=? WHERE correlation_id='upload-rewrite'",
                               (raw.replace(b'"S1"', b'"S5"'),))
        # The stored digest no longer matches the bytes: the ledger refuses before any memo is consulted.
        with self.assertRaises(IntakeError) as caught:
            upload.reserved_ids(self.root)
        self.assertEqual(caught.exception.code, "corrupt_request")

    def test_row_whose_digest_names_a_memoized_request_is_still_caught_by_ledger_validation(self):
        upload.submit(self.root, self.file(), value("one"))
        upload.submit(self.root, self.file("two.txt", b"two\n"), value("two", "two.txt"))
        upload.reserved_ids(self.root)
        db = self.root / ".local/intake/intake.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            one, two = (connection.execute("SELECT raw, request_sha256 FROM requests WHERE correlation_id=?",
                                           (f"upload-{n}",)).fetchone() for n in ("one", "two"))
            connection.execute("UPDATE requests SET raw=?, request_sha256=? WHERE correlation_id='upload-two'",
                               (one[0], one[1]))
        with self.assertRaises(IntakeError):
            upload.reserved_ids(self.root)

    def test_a_request_staged_by_another_agent_after_the_memo_was_filled_is_never_missed(self):
        upload.submit(self.root, self.file(), value("mine"))
        upload.reserved_ids(self.root)  # memo is warm
        other = self.outside / "other.txt"
        other.write_bytes(b"other\n")
        directory, original, sidecar_path, sidecar = upload.plan(self.root, value("theirs", "other.txt"), b"other\n")
        state, records = upload.records_for(self.root, value("theirs", "other.txt"), b"other\n",
                                            original, sidecar_path, sidecar)
        # Another process stages its request (not yet run) between this process's two plannings.
        store.stage(self.root, upload.canonical({
            "version": 1, "correlation_id": "upload-theirs", "correction_of": None,
            "policy": {"version": 1, "sha256": upload.policy.for_root(self.root)[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 0},
            "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                "records": records, "actor": "a", "request_id": "upload-theirs",
                "expected_state": state["state_token"], "reason": "Manual upload: x"}}]}).encode())
        ids = self.ids(self.plan("mine-too"))
        self.assertEqual((ids["source"], ids["receipt"]), ("S3", "D3"))

    def test_memo_is_bounded(self):
        with patch.object(upload, "PROMISES_LIMIT", 2), patch.object(upload, "INDEX_LIMIT", 0):
            for number in range(5):
                upload.submit(self.root, self.file(f"b{number}.txt", b"b%d\n" % number), value(f"b{number}", f"b{number}.txt"))
            result = upload.reserved_ids(self.root)
        self.assertLessEqual(len(upload.PROMISES), 2)
        self.assertEqual(result.highest, {"S": 5, "D": 5})


class IncrementalTests(Case):
    """``reserved_ids`` reads only the requests added since its last call (rowid high-water mark, Kata rqqq)."""

    def upload_n(self, *names):
        for name in names:
            upload.submit(self.root, self.file(name + ".txt", name.encode() + b"\n"), value(name, name + ".txt"))

    def statements(self, call):
        seen = []
        real = store.locked

        def traced(root, wait=None):
            manager = real(root, wait)

            class Wrapper:
                def __enter__(inner):
                    connection = manager.__enter__()
                    connection.set_trace_callback(seen.append)
                    return connection

                def __exit__(inner, *exc):
                    return manager.__exit__(*exc)
            return Wrapper()
        with patch.object(upload.store, "locked", traced):
            result = call()
        return result, [s for s in seen if "FROM requests" in s]

    def test_a_warm_call_reads_only_rows_after_the_high_water_mark(self):
        self.upload_n("a", "b", "c")
        upload.reserved_ids(self.root)
        _, reads = self.statements(lambda: upload.reserved_ids(self.root))
        self.assertTrue(reads)
        self.assertTrue(all("rowid" in read for read in reads), reads)
        self.assertFalse(any("correlation_id LIKE" in read and "rowid" not in read for read in reads))

    def test_requests_added_after_a_warm_call_are_all_picked_up_in_order(self):
        self.upload_n("a")
        self.assertEqual(upload.reserved_ids(self.root).highest, {"S": 1, "D": 1})
        self.upload_n("b", "c")
        taken = upload.reserved_ids(self.root, {"A-upload-c-original", "A-upload-zz-original"})
        self.assertEqual(taken.highest, {"S": 3, "D": 3})
        self.assertEqual(taken.present, {"A-upload-c-original"})

    def test_a_replaced_ledger_with_the_same_number_of_requests_resets_the_index(self):
        self.upload_n("a", "b")
        self.assertEqual(upload.reserved_ids(self.root).highest, {"S": 2, "D": 2})
        db = self.root / ".local/intake/intake.sqlite3"
        copy = db.with_name("copy.sqlite3")
        with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
        with closing(sqlite3.connect(copy)) as connection, connection:
            raw = connection.execute("SELECT raw FROM requests WHERE correlation_id='upload-b'").fetchone()[0]
            raw = raw.replace(b'"S2"', b'"S9"').replace(b'"D2"', b'"D9"')
            connection.execute("UPDATE requests SET raw=?, request_sha256=? WHERE correlation_id='upload-b'",
                               (raw, upload.digest(raw)))
        copy.chmod(0o600)
        copy.replace(db)  # a different file (inode) now holds the ledger
        self.assertEqual(upload.reserved_ids(self.root).highest, {"S": 9, "D": 9})

    def test_a_smaller_ledger_than_the_index_remembers_resets_it(self):
        self.upload_n("a", "b")
        upload.reserved_ids(self.root)
        other = Path(self.temp.name) / "other"
        other.mkdir()
        store.initialize(other)
        catalog.initialize(other)
        upload.submit(other, self.file("o.txt", b"o\n"), value("o", "o.txt"))
        self.assertEqual(upload.reserved_ids(other).highest, {"S": 1, "D": 1})
        self.assertEqual(upload.reserved_ids(self.root).highest, {"S": 2, "D": 2})

    def test_a_request_with_a_corrupt_digest_is_refused_when_first_read(self):
        self.upload_n("a")
        upload.reserved_ids(self.root)
        store.stage(self.root, upload.canonical({
            "version": 1, "correlation_id": "upload-late", "correction_of": None,
            "policy": {"version": 1, "sha256": upload.policy.for_root(self.root)[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 0},
            "stages": [{"id": "receipt", "kind": "receipt", "operation": "catalog-submit", "arguments": {
                "records": [], "actor": "a", "request_id": "upload-late",
                "expected_state": "0" * 64, "reason": "Manual upload: x"}}]}).encode())
        db = self.root / ".local/intake/intake.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("UPDATE requests SET request_sha256=? WHERE correlation_id='upload-late'", ("0" * 64,))
        with self.assertRaises(IntakeError):
            upload.reserved_ids(self.root)

    def test_over_the_index_bound_the_answer_is_the_same_scan(self):
        self.upload_n("a", "b", "c")
        warm = upload.reserved_ids(self.root, {"A-upload-b-original"})
        upload.forget_reserved()
        with patch.object(upload, "INDEX_LIMIT", 2):
            bounded = upload.reserved_ids(self.root, {"A-upload-b-original"})
            again = upload.reserved_ids(self.root, {"A-upload-b-original"})
        for taken in (bounded, again):
            self.assertEqual((taken.highest, taken.present), (warm.highest, warm.present))


class EquivalenceTests(Case):
    def test_allocation_matches_the_pre_change_algorithm_across_a_mixed_history(self):
        """Replay a history with commits, a blocked request and a correction; every planned record
        must equal what the original set-based algorithm computes from the same state."""
        def legacy(root, descriptor):
            home, db, _ = catalog.locations(root)
            with closing(catalog.connect(db)) as connection:
                connection.execute("BEGIN")
                catalog.validated_state(connection, home)
                catalog.current_view(connection)
                taken = {row[0] for row in connection.execute("SELECT id FROM catalog_current")}
            with store.locked(root) as c:
                for (raw,) in c.execute("SELECT raw FROM requests WHERE correlation_id LIKE 'upload-%'"):
                    for stage in upload.decode(raw)["stages"]:
                        if stage["operation"] == "catalog-submit":
                            taken.update(record["id"] for record in stage["arguments"]["records"])
            numbers = lambda p: max((int(i[1:]) for i in taken if i[:1] == p and i[1:].isdigit()), default=0) + 1
            return f"S{numbers('S')}", f"D{numbers('D')}"
        self.commit(40, request_id="preexisting")
        with patch.object(runner.adapters.catalog, "submit", side_effect=catalog.CatalogError("refused")):
            upload.submit(self.root, self.file("a.txt", b"a\n"), value("a", "a.txt"))
        for sid in ("b", "c", "d"):
            expected = legacy(self.root, value(sid))
            ack = upload.submit(self.root, self.file(f"{sid}.txt", sid.encode()), value(sid, f"{sid}.txt"))
            self.assertEqual((ack["records"]["source_id"], ack["records"]["receipt_id"]), expected)
        fix = upload.submit(self.root, self.file("a.txt", b"a2\n"), value("a-fix", "a.txt", correction_of="a"))
        self.assertEqual(fix["receipt"]["state"], "committed")
        self.assertEqual(legacy(self.root, value("z"))[0], self.ids(self.plan("z"))["source"])


if __name__ == "__main__":
    unittest.main()
