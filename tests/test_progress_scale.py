"""The progress view stays cheap at production size without weakening what it verifies (Kata 4er0).

Synthetic ledgers in temporary workspaces only. Counts are compared with a straightforward
reference (every request decoded and turned into an item, as the view once did).
"""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from tests.support import default_policy_hash
from tools.catalog import catalog_store as catalog
from tools.intake import contracts, progress, store

SECRET = "SECRET-BODY-TEXT"
LAYOUTS = (
    (("receipt", "catalog-submit", "receipt"),),
    (("receipt", "catalog-submit", "receipt"), ("extraction", "docling-prepare", "extract")),
    (("receipt", "catalog-submit", "receipt"), ("extraction", "docling-prepare", "extract"),
     ("review", "standing-review", "review")),
)
ARGUMENTS = {
    "catalog-submit": lambda n: {"records": [{"body": SECRET}], "actor": "bench", "request_id": f"req-{n}",
                                 "expected_state": "0" * 64, "reason": "synthetic"},
    "docling-prepare": lambda n: {"receipt": f"D{n}", "format_name": "pdf", "artifacts_path": "x", "python": "p"},
    "standing-review": lambda n: {"policy_path": "p", "policy_sha256": "a" * 64, "rendition": "r",
                                  "rendition_sha256": "b" * 64, "findings_path": "f",
                                  "findings_sha256": "c" * 64, "event_id": "rev-x", "request_id": "req-x"},
}
CODES = ("prohibited-scope", "native-busy", "unsupported-type", "needs-reader-docx",
         "standing-rejected-reviewer-rejected", "standing-pending-review-x", None)


def raw_request(number, layout, prefix="bulk-"):
    return json.dumps({
        "version": 1, "correlation_id": f"{prefix}{number:06d}", "correction_of": None,
        "policy": {"version": 1, "sha256": default_policy_hash()},
        "retry": {"max_attempts": 3, "backoff_seconds": 0},
        "stages": [{"id": stage, "kind": kind, "operation": operation,
                    "arguments": ARGUMENTS[operation](number)} for kind, operation, stage in layout]}).encode()


def seed(root, count, *, seed_value=1, prefix="bulk-", now=100000.0):
    """Insert ``count`` requests with randomized stage statuses and attempts, bypassing writers."""
    chooser = random.Random(seed_value)
    store.initialize(root)
    with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as c, c:
        for number in range(count):
            layout = chooser.choice(LAYOUTS)
            raw = raw_request(number, layout, prefix)
            correlation = f"{prefix}{number:06d}"
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                      (correlation, None, raw, contracts.digest(raw), str(root)))
            failed = False
            for ordinal, (_, _, stage) in enumerate(layout):
                if failed:
                    status = "staged"
                else:
                    status = chooser.choice(("succeeded", "succeeded", "succeeded", "blocked",
                                             "retryable", "dead-letter", "running", "staged"))
                failed = failed or status != "succeeded"
                done = status == "succeeded"
                intent, evidence = json.dumps({"n": number, "o": ordinal}), json.dumps({"ok": True})
                c.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)", (
                    correlation, stage, ordinal, status, 0, intent, contracts.digest(intent.encode()),
                    evidence if done else None, contracts.digest(evidence.encode()) if done else None))
                if status != "staged":
                    started = now + number * 2 + ordinal
                    ended = None if status == "running" else started + 1
                    outcome = "succeeded" if done else None if status == "running" else "failed"
                    code = chooser.choice(CODES) if status in {"blocked", "retryable", "dead-letter"} else None
                    c.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        correlation, stage, 0, 1, started, ended, outcome,
                        None if done or ended is None else "permanent", code, None))


def reference(root, runs=None, limit=50):
    """The old computation: snapshot everything, decode every request, one item per request."""
    root, home, db = store.locations(root)
    with closing(sqlite3.connect(db)) as c:
        c.row_factory = sqlite3.Row
        requests, stages_of, attempts_of, _ = store.snapshot(c)
    items = []
    for row in requests:
        saved = {s["ordinal"]: dict(s) for s in stages_of[row["correlation_id"]]}
        attempts = {key: [dict(a) for a in found] for key, found in attempts_of.items()}
        items.append(progress.item(row["correlation_id"], row["correction_of"],
                                   contracts.decode_request(row["raw"]), saved, attempts))
    items.sort(key=lambda v: (-(v["updated"] or 0), v["correlation_id"]))

    def aggregate(values):
        funnel = dict.fromkeys(progress.FUNNEL, 0)
        matrix = {kind: dict.fromkeys(progress.STATUSES, 0) for kind in progress.KINDS}
        for value in values:
            funnel[value["funnel"]] += 1
            for kind, status in value["kinds"]:
                matrix[kind][status] += 1
        updated = [v["updated"] for v in values if v["updated"] is not None]
        return {"requests": len(values), "funnel": funnel, "stages": matrix,
                "exceptions": sum(any(s in {"blocked", "dead-letter"} for _, s in v["kinds"]) for v in values),
                "retrying": sum(any(s == "retryable" for _, s in v["kinds"]) for v in values),
                "last_activity": max(updated) if updated else None}
    return {"overall": aggregate(items),
            "runs": {name: aggregate([v for v in items if v["correlation_id"].startswith(prefix)])
                     for name, prefix in (runs or {}).items()},
            "recent": [progress.public(v) for v in items[:limit]], "items": items}


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        self.db = self.root / ".local/intake/intake.sqlite3"
        progress.forget()
        self.addCleanup(progress.forget)


class EquivalenceTests(Case):
    def test_counts_runs_and_recent_equal_the_reference_across_pages(self):
        seed(self.root, 230)
        runs = {"low": "bulk-0000", "high": "bulk-0001", "none": "zzz"}
        expected = reference(self.root, runs, limit=20)
        for page in (7, 64, 5000):
            progress.forget()
            with self.subTest(page=page), mock.patch.object(progress, "PAGE", page):
                got = progress.snapshot(self.root, limit=20, runs=runs)
            self.assertEqual(got["overall"], expected["overall"])
            self.assertEqual({k: {f: v for f, v in got["runs"][k].items() if f != "prefix"} for k in runs},
                             expected["runs"])
            self.assertEqual(got["recent"], expected["recent"])
            self.assertEqual(got["attention_total"], sum(
                v["state"] in {"blocked", "dead-letter", "retryable"} for v in expected["items"]))
        self.assertGreater(expected["overall"]["exceptions"], 0)
        self.assertGreater(len({v["funnel"] for v in expected["items"]}), 3)

    def test_attention_is_the_attention_items_and_grouped_by_cause(self):
        seed(self.root, 300, seed_value=3)
        expected = reference(self.root)
        wanted = [progress.public(v) for v in expected["items"]
                  if v["state"] in {"blocked", "dead-letter", "retryable"}]
        got = progress.snapshot(self.root, limit=5)
        self.assertEqual(len(got["attention"]), 5)
        by_id = {v["correlation_id"]: v for v in wanted}
        for shown in got["attention"]:
            self.assertEqual(shown, by_id[shown["correlation_id"]])
        groups = Counter((v["state"], v["funnel"], (v["last_attempt"] or {}).get("code")) for v in wanted)
        self.assertEqual({(g["state"], g["funnel"], g["code"]): g["count"] for g in got["attention_groups"]},
                         dict(groups))
        counts = [g["count"] for g in got["attention_groups"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_one_repeated_code_does_not_use_the_whole_attention_limit(self):
        seed(self.root, 120, seed_value=5)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE attempts SET code='prohibited-scope' WHERE code IS NOT NULL "
                      "AND rowid % 5 != 0")
        got = progress.snapshot(self.root, limit=6)
        codes = {(v["last_attempt"] or {}).get("code") for v in got["attention"]}
        self.assertGreater(len(codes), 1, got["attention_groups"])
        self.assertEqual(len(got["attention"]), 6)

    def test_snapshot_pages_cover_the_same_rows_as_one_snapshot(self):
        seed(self.root, 50)
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            whole = store.snapshot(c)
            after, seen = None, []
            while True:
                page = store.snapshot(c, after, 8)
                if not page[0]:
                    break
                seen += [(r["correlation_id"], [s["stage_id"] for s in page[1][r["correlation_id"]]])
                         for r in page[0]]
                after = page[0][-1]["correlation_id"]
        self.assertEqual(seen, sorted((r["correlation_id"], [s["stage_id"] for s in whole[1][r["correlation_id"]]])
                                      for r in whole[0]))

    def test_the_reported_fields_are_body_free(self):
        seed(self.root, 40)
        report = progress.snapshot(self.root)
        text = json.dumps(report) + progress.render_html(report) + progress.render_text(report)
        self.assertNotIn(SECRET, text)
        self.assertIn("Needs attention by cause", progress.render_html(report))


class CachedResultTests(Case):
    def test_an_unchanged_ledger_is_not_verified_or_decoded_again(self):
        seed(self.root, 60)
        first = progress.snapshot(self.root)
        self.assertFalse(first["render"]["ledger_cached"])
        with mock.patch.object(store, "verify_page", side_effect=AssertionError("re-verified")), \
                mock.patch.object(store, "check_identity", side_effect=AssertionError("re-checked")), \
                mock.patch.object(progress, "decode_request", side_effect=AssertionError("decoded")):
            second = progress.snapshot(self.root)
        self.assertTrue(second["render"]["ledger_cached"])
        for key in ("overall", "runs", "recent", "attention", "attention_groups", "attention_total"):
            self.assertEqual(first[key], second[key])

    def test_a_commit_between_renders_is_seen(self):
        seed(self.root, 20)
        self.assertEqual(progress.snapshot(self.root)["overall"]["requests"], 20)
        store.stage(self.root, raw_request(999, LAYOUTS[0], prefix="late-"))
        again = progress.snapshot(self.root)
        self.assertFalse(again["render"]["ledger_cached"])
        self.assertEqual(again["overall"]["requests"], 21)
        self.assertTrue(progress.snapshot(self.root)["render"]["ledger_cached"])

    def test_a_cached_result_is_not_served_after_the_ledger_is_tampered_with(self):
        edits = {
            "request digest": ("UPDATE requests SET request_sha256=? WHERE correlation_id='bulk-000003'",
                               ("0" * 64,), "corrupt_request"),
            "artifact bytes": ("UPDATE stages SET intent='{\"n\": 1}' WHERE correlation_id='bulk-000003' "
                               "AND ordinal=0", (), "corrupt_artifact"),
            "stage status": ("UPDATE stages SET status='bogus' WHERE correlation_id='bulk-000003'",
                             (), "corrupt_stage"),
            "dropped stage": ("DELETE FROM attempts WHERE correlation_id='bulk-000003'; "
                              "DELETE FROM stages WHERE correlation_id='bulk-000003'", (), None),
        }
        for name, (statement, parameters, code) in edits.items():
            with self.subTest(name):
                self.setUp()
                seed(self.root, 20)
                self.assertTrue(progress.snapshot(self.root)["ledger"]["available"])
                self.assertTrue(progress.snapshot(self.root)["render"]["ledger_cached"])
                with closing(sqlite3.connect(self.db)) as c, c:
                    for part in statement.split("; "):
                        c.execute(part, parameters if "?" in part else ())
                report = progress.snapshot(self.root)
                self.assertFalse(report["ledger"]["available"])
                self.assertEqual(report["overall"]["requests"], 0)
                if code:
                    self.assertEqual(report["ledger"]["error"], code)

    def test_a_forged_digest_on_unchanged_bytes_is_refused_after_a_warm_pass(self):
        seed(self.root, 10)
        progress.snapshot(self.root)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE stages SET intent_sha256=? WHERE correlation_id='bulk-000002' AND ordinal=0",
                      ("1" * 64,))
        self.assertEqual(progress.snapshot(self.root)["ledger"]["error"], "corrupt_artifact")

    def test_a_request_rewritten_to_another_valid_request_with_a_fresh_digest_is_refused(self):
        seed(self.root, 10)
        progress.snapshot(self.root)
        other = raw_request(5, LAYOUTS[0], prefix="bulk-").replace(b"bulk-000005", b"bulk-000009")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE requests SET raw=?, request_sha256=? WHERE correlation_id='bulk-000002'",
                      (other, contracts.digest(other)))
        self.assertEqual(progress.snapshot(self.root)["ledger"]["error"], "corrupt_request")

    def test_request_bytes_rewritten_under_their_old_digest_are_refused_after_a_warm_pass(self):
        seed(self.root, 10)
        progress.snapshot(self.root)
        with closing(sqlite3.connect(self.db)) as c:
            current = c.execute("SELECT raw FROM requests WHERE correlation_id='bulk-000002'").fetchone()[0]
        other = current.replace(b'"max_attempts": 3', b'"max_attempts": 4')
        self.assertNotEqual(other, current)
        with closing(sqlite3.connect(self.db)) as c, c:  # valid request, stale stored digest
            c.execute("UPDATE requests SET raw=? WHERE correlation_id='bulk-000002'", (other,))
        self.assertEqual(progress.snapshot(self.root)["ledger"]["error"], "corrupt_request")

    def test_a_failed_pass_is_not_remembered(self):
        seed(self.root, 10)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE requests SET request_sha256=? WHERE correlation_id='bulk-000002'", ("0" * 64,))
        self.assertFalse(progress.snapshot(self.root)["ledger"]["available"])
        self.assertEqual(len(progress._memo.requests), 2, "only rows verified before the failure")
        self.assertEqual(progress._cached, {})
        self.assertFalse(progress.snapshot(self.root)["ledger"]["available"])

    def test_the_cache_is_per_limit_run_set_and_day(self):
        seed(self.root, 20)
        progress.snapshot(self.root, limit=5)
        self.assertFalse(progress.snapshot(self.root, limit=6)["render"]["ledger_cached"])
        self.assertFalse(progress.snapshot(self.root, limit=6, runs={"a": "bulk-"})["render"]["ledger_cached"])
        self.assertTrue(progress.snapshot(self.root, limit=6, runs={"a": "bulk-"})["render"]["ledger_cached"])
        later = mock.Mock(wraps=progress.datetime)
        later.now.return_value = progress.datetime(2999, 1, 1, tzinfo=progress.timezone.utc)
        with mock.patch.object(progress, "datetime", later):
            self.assertFalse(progress.snapshot(self.root, limit=6, runs={"a": "bulk-"})["render"]["ledger_cached"])

    def test_progress_writes_nothing(self):
        seed(self.root, 30)
        catalog.initialize(self.root)
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")}
        progress.snapshot(self.root)
        progress.snapshot(self.root)
        self.assertEqual({p: (p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")}, before)


class MemoTests(Case):
    def verify(self, memo, **kw):
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            return store.verify_page(store.snapshot(c, None, 1000), self.root, memo)

    def test_the_memo_keys_are_digests_not_bytes_and_repeat_passes_decode_nothing(self):
        seed(self.root, 40)
        memo = store.PageMemo()
        self.verify(memo)
        self.assertTrue(all(len(key) == 64 for key in memo.requests))
        self.assertTrue(all(len(key) == 64 for key in memo.artifacts))
        with mock.patch.object(contracts, "decode_request", side_effect=AssertionError), \
                mock.patch.object(store, "decode", side_effect=AssertionError):
            self.verify(memo)

    def test_the_memo_is_bounded(self):
        seed(self.root, 40)
        memo = store.PageMemo(limit=10)
        self.verify(memo)
        self.assertLessEqual(len(memo), 10)

    def test_layouts_equal_the_validated_requests(self):
        seed(self.root, 30)
        layouts = self.verify(store.PageMemo())
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            rows = store.snapshot(c)[0]
        rows = sorted(rows, key=lambda r: r["correlation_id"])
        for row, layout in zip(rows, layouts):
            value = contracts.decode_request(row["raw"])
            self.assertEqual(layout, tuple((s["id"], s["kind"], s["operation"]) for s in value["stages"]))

    def test_every_refusal_of_verify_is_a_refusal_of_verify_page(self):
        statements = {
            "corrupt_request": ["UPDATE requests SET root='/elsewhere'", "UPDATE requests SET correction_of='x'"],
            "corrupt_stage": ["UPDATE stages SET cycle=-1", "UPDATE stages SET status='x'",
                              "UPDATE stages SET evidence=NULL, evidence_sha256=NULL WHERE status='succeeded'"],
            "corrupt_artifact": ["UPDATE stages SET intent_sha256=NULL", "UPDATE stages SET intent='[]'"],
            "corrupt_attempt": ["UPDATE attempts SET ordinal=0", "UPDATE attempts SET outcome='x'"],
            "corrupt_replay": ["UPDATE stages SET cycle=1"],
        }
        for expected, edits in statements.items():
            for edit in edits:
                with self.subTest(edit=edit):
                    self.setUp()
                    seed(self.root, 12)
                    with closing(sqlite3.connect(self.db)) as c, c:
                        c.execute("PRAGMA foreign_keys=OFF")
                        c.execute(edit)
                        if edit == "UPDATE stages SET intent='[]'":
                            c.execute("UPDATE stages SET intent_sha256=?", (contracts.digest(b"[]"),))
                    with closing(sqlite3.connect(self.db)) as c:
                        c.row_factory = sqlite3.Row
                        rows = store.snapshot(c)
                    with self.assertRaises(contracts.IntakeError) as old:
                        store.verify(rows, self.root)
                    with self.assertRaises(contracts.IntakeError) as new:
                        store.verify_page(rows, self.root, store.PageMemo())
                    self.assertEqual(new.exception.code, old.exception.code)
                    self.assertEqual(new.exception.code, expected)

    def test_text_fast_path_equals_the_per_character_rule(self):
        def old(value, limit=200):
            return (isinstance(value, str) and 0 < len(value) <= limit
                    and all(32 <= ord(c) < 127 for c in value))
        samples = [chr(c) for c in range(0, 0x3000)] + ["", "ab c", "a\tb", "a\x7fb", "é", "x" * 201,
                                                          "a\u2028b", "\U0001f600", None, 5, b"x"]
        for value in samples:
            try:
                new = contracts.text(value) is None
            except contracts.IntakeError:
                new = False
            self.assertEqual(new, old(value), repr(value))


class CatalogTests(Case):
    def test_catalog_counts_are_unchanged_and_a_receipt_avoids_the_full_inspection(self):
        catalog.initialize(self.root)
        store.initialize(self.root)
        from tools.diagnostics.catalog_lock_benchmark import seed as seed_catalog
        seed_catalog(self.root, 30)
        catalog._VALID_PAYLOADS.clear()
        full = progress.read_catalog(self.root)
        self.assertEqual((full["receipts"], full["artifacts"], full["sources"]), (30, 60, 30))
        self.assertEqual(full["coverage"]["pending-extraction"], 30)
        self.assertEqual(full["extraction"]["pending"], 30)
        catalog.status(self.root)  # a validated read leaves the receipt
        progress._catalog_seen.clear()
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        with mock.patch.object(catalog, "inspect", side_effect=AssertionError("replayed")):
            self.assertEqual(progress.read_catalog(self.root), full)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}, before)

    def test_a_catalog_changed_after_its_receipt_is_inspected_and_refused_when_corrupt(self):
        catalog.initialize(self.root)
        from tools.diagnostics.catalog_lock_benchmark import seed as seed_catalog
        seed_catalog(self.root, 5)
        catalog.status(self.root)
        self.assertTrue(progress.read_catalog(self.root)["available"])
        db = self.root / ".local/catalog/catalog.sqlite3"
        with closing(sqlite3.connect(db)) as c:
            c.execute("DROP TRIGGER IF EXISTS immutable_versions_update")
            c.execute("UPDATE versions SET payload_hash='0'")
            c.commit()
        result = progress.read_catalog(self.root)
        self.assertFalse(result["available"], result)

    def test_without_a_receipt_a_repeat_render_reuses_this_processes_own_inspection(self):
        catalog.initialize(self.root)
        from tools.diagnostics.catalog_lock_benchmark import seed as seed_catalog
        seed_catalog(self.root, 5)
        progress._catalog_seen.clear()
        real = catalog.inspect
        with mock.patch.object(catalog, "inspect", side_effect=real) as inspected:
            first = progress.read_catalog(self.root)
            second = progress.read_catalog(self.root)
        self.assertEqual(first, second)
        self.assertEqual(inspected.call_count, 1)


class SingleFlightTests(unittest.TestCase):
    def test_concurrent_requests_share_one_render(self):
        gate, started, calls = threading.Event(), threading.Event(), []

        def render():
            calls.append(1)
            started.set()
            gate.wait(5)
            return len(calls)
        latest = progress.Latest(render, reuse=0)
        results = []
        threads = [threading.Thread(target=lambda: results.append(latest.get())) for _ in range(5)]
        threads[0].start()
        started.wait(5)
        for thread in threads[1:]:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(calls, [1])
        self.assertEqual(results, [1] * 5)

    def test_a_fresh_result_is_reused_and_an_old_one_is_rendered_again(self):
        now = [0.0]
        latest = progress.Latest(lambda: object(), reuse=1.0, clock=lambda: now[0])
        first = latest.get()
        now[0] = 0.5
        self.assertIs(latest.get(), first)
        now[0] = 2.0
        self.assertIsNot(latest.get(), first)

    def test_a_failed_render_is_not_shared_and_does_not_wedge_later_requests(self):
        outcomes = [RuntimeError("boom"), "ok"]

        def render():
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        latest = progress.Latest(render, reuse=0)
        with self.assertRaises(RuntimeError):
            latest.get()
        self.assertEqual(latest.get(), "ok")


class RateTests(Case):
    def test_rates_count_attempts_finished_in_each_window(self):
        store.initialize(self.root)
        raw = raw_request(1, LAYOUTS[0])
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                      ("bulk-000001", None, raw, contracts.digest(raw), str(self.root)))
            c.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)",
                      ("bulk-000001", "receipt", 0, "staged", 0, None, None, None, None))
            for ordinal, (age, outcome) in enumerate(((10, "succeeded"), (200, "succeeded"),
                                                      (600, "failed"), (5000, "succeeded")), 1):
                c.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    "bulk-000001", "receipt", 0, ordinal, 1.0, 1000.0 - age, outcome, None, None, None))
        with closing(sqlite3.connect(self.db)) as c:
            moving = progress.rates(c, 1000.0)
        self.assertEqual(moving["finished"], {"failed": {"60": 0, "300": 0, "900": 1},
                                              "succeeded": {"60": 1, "300": 2, "900": 2}})
        text = progress.render_text(progress.snapshot(self.root))
        self.assertIn("finished attempts (last 1/5/15 min)", text)


class ServerTests(Case):
    def test_server_serves_concurrent_requests_from_one_render(self):
        seed(self.root, 20)
        server = progress.make_server(self.root, port=0, reuse=30)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        import urllib.request
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.object(progress, "read_ledger", wraps=progress.read_ledger) as read:
            bodies = []
            threads = [threading.Thread(target=lambda: bodies.append(
                json.loads(urllib.request.urlopen(base + "/progress.json").read()))) for _ in range(4)]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
        self.assertEqual(read.call_count, 1)
        self.assertEqual({b["overall"]["requests"] for b in bodies}, {20})


if __name__ == "__main__":
    unittest.main()
