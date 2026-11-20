"""Read-only, body-free loopback progress view over the private intake stores."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from tests.support import default_policy_hash
from tools import policy
from tools.catalog import catalog_store as catalog
from tools.intake import adapters, cli, progress, progress_render, progress_serve, runner, store, upload

SECRET = "SECRET-BODY-TEXT"


def request(correlation, stages, correction_of=None):
    from tools.intake.contracts import OPERATIONS
    full = []
    for kind, operation, name in stages:
        args = {}
        for key in OPERATIONS[operation][1]:
            if key.endswith("sha256"):
                args[key] = "a" * 64
            elif key in {"records", "value"}:
                args[key] = [{"body": SECRET}]
            elif key == "expected_revision":
                args[key] = 0
            elif key == "format_name":
                args[key] = "pdf"
            elif key == "request_id":
                args[key] = "req-" + correlation.lower()
            elif key == "event_id":
                args[key] = "rev-" + correlation.lower()
            else:
                args[key] = "dir/" + SECRET
        full.append({"id": name, "kind": kind, "operation": operation, "arguments": args})
    return json.dumps({"version": 1, "correlation_id": correlation, "correction_of": correction_of,
                       "policy": {"version": 1, "sha256": default_policy_hash()},
                       "retry": {"max_attempts": 2, "backoff_seconds": 0},
                       "stages": full}).encode()


PIPELINE = [("receipt", "catalog-submit", "receipt"),
            ("extraction", "docling-prepare", "extract"),
            ("review", "standing-review", "review"),
            ("publication", "standing-select", "select"),
            ("publication", "standing-publish", "publish")]


class ProgressCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()

    def files(self):
        return {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")}

    def drive(self, correlation, outcomes):
        """Run stages in order with synthetic adapters; outcomes map stage id -> failure or None."""
        store.stage(self.root, request(correlation, PIPELINE))
        for _, _, name in PIPELINE:
            if name not in outcomes:
                return
            failure = outcomes[name]

            def execute(root, stage_, intent, previous=None, failure=failure):
                if failure is not None:
                    raise adapters.StageFailure(*failure)
                return {"boundary": "synthetic", "note": SECRET}
            with patch.object(adapters, "prepare", return_value={"intent": SECRET}), \
                    patch.object(adapters, "execute", side_effect=execute):
                runner.run(self.root, correlation, name, now=1000.0 + len(correlation))
            if failure is not None:
                return


class EmptyInstanceTests(ProgressCase):
    def test_uninitialized_instance_reports_unavailable_sources_without_crashing(self):
        result = progress.snapshot(self.root)
        self.assertFalse(result["ledger"]["available"])
        self.assertEqual(result["ledger"]["error"], "missing_store")
        self.assertFalse(result["catalog"]["available"])
        self.assertFalse(result["review_events"]["available"])
        self.assertFalse(result["index"]["available"])
        self.assertEqual(result["overall"]["requests"], 0)
        self.assertEqual(set(result["overall"]["funnel"]), set(progress.FUNNEL))
        self.assertFalse(any(result["overall"]["funnel"].values()))
        self.assertTrue(result["body_free"])
        self.assertFalse((self.root / ".local").exists(), "progress must not create stores")

    def test_initialized_empty_ledger_has_zero_counts(self):
        store.initialize(self.root)
        catalog.initialize(self.root)
        result = progress.snapshot(self.root)
        self.assertTrue(result["ledger"]["available"])
        self.assertEqual(result["overall"]["requests"], 0)
        self.assertEqual(result["recent"], [])
        self.assertTrue(result["catalog"]["available"])
        self.assertEqual(result["catalog"]["artifacts"], 0)
        page = progress_render.render_html(result, refresh=5)
        self.assertIn('http-equiv="refresh" content="5"', page)
        self.assertEqual(page.lower().count("<script"), 1)
        self.assertIn("<time datetime=", page)
        self.assertNotIn("http://", page.replace('http-equiv', ''))


class MixedStateTests(ProgressCase):
    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        catalog.initialize(self.root)
        self.drive("run-staged", {})
        self.drive("run-received", {"receipt": None})
        self.drive("run-extract-retry", {"receipt": None, "extract": ("retryable", "native-busy")})
        self.drive("run-extracted", {"receipt": None, "extract": None})
        self.drive("run-pending", {"receipt": None, "extract": None,
                                   "review": ("blocked", "standing-pending-review-unresolved-evidence")})
        self.drive("run-rejected", {"receipt": None, "extract": None,
                                    "review": ("blocked", "standing-rejected-reviewer-rejected")})
        self.drive("run-deferred", {"receipt": None, "extract": None,
                                    "review": ("blocked", "standing-deferred-reviewer-deferred")})
        self.drive("run-exception", {"receipt": None, "extract": None,
                                     "review": ("blocked", "standing-exception-budget-exceeded")})
        self.drive("run-format", {"receipt": None, "extract": ("blocked", "unsupported-type")})
        self.drive("run-reader", {"receipt": None, "extract": ("blocked", "needs-reader-docx")})
        self.drive("run-dead", {"receipt": ("permanent", "invalid-artifact")})
        self.drive("run-blocked", {"receipt": None, "extract": ("blocked", "native-input")})
        self.drive("run-qualified", {"receipt": None, "extract": None, "review": None})
        self.drive("run-selected", {"receipt": None, "extract": None, "review": None, "select": None})
        self.drive("run-published", {"receipt": None, "extract": None, "review": None,
                                     "select": None, "publish": None})

    def test_funnel_counts_and_stage_matrix(self):
        before = self.files()
        result = progress.snapshot(self.root, limit=100)
        self.assertEqual(self.files(), before, "progress must not write the stores")
        funnel = result["overall"]["funnel"]
        expected = {"staged": 1, "received": 1, "extracting": 1, "extracted": 1,
                    "pending-review": 1, "qualified": 1, "selected": 1, "rejected": 1,
                    "deferred": 1, "exception": 1, "skipped-by-format": 1, "needs-reader": 1,
                    "dead-letter": 1, "blocked": 1, "published": 1}
        self.assertEqual({k: v for k, v in funnel.items() if v}, expected)
        self.assertEqual(result["overall"]["requests"], 15)
        stages = result["overall"]["stages"]
        self.assertEqual(stages["receipt"]["succeeded"], 13)
        self.assertEqual(stages["receipt"]["dead-letter"], 1)
        self.assertEqual(stages["extraction"]["retryable"], 1)
        self.assertEqual(stages["publication"]["succeeded"], 3)
        self.assertEqual(result["overall"]["exceptions"], 8)

    def test_recent_items_are_metadata_only_with_last_attempt(self):
        result = progress.snapshot(self.root, limit=100)
        items = {item["correlation_id"]: item for item in result["recent"]}
        self.assertEqual(len(items), 15)
        dead = items["run-dead"]
        self.assertEqual((dead["stage"], dead["state"], dead["funnel"]),
                         ("receipt", "dead-letter", "dead-letter"))
        self.assertEqual(dead["last_attempt"]["outcome"], "failed")
        self.assertEqual(dead["last_attempt"]["category"], "permanent")
        self.assertEqual(dead["last_attempt"]["code"], "invalid-artifact")
        self.assertEqual(items["run-staged"]["last_attempt"], None)
        self.assertEqual(items["run-published"]["state"], "succeeded")
        self.assertEqual(items["run-published"]["stages"], {"succeeded": 5})
        text = json.dumps(result) + progress_render.render_html(result) + progress_render.render_text(result)
        self.assertNotIn(SECRET, text)

    def test_limit_and_named_runs(self):
        result = progress.snapshot(self.root, limit=2, runs={"ex": "run-ex", "none": "zzz"})
        self.assertEqual(len(result["recent"]), 2)
        self.assertEqual(result["overall"]["requests"], 15)
        self.assertEqual(result["runs"]["ex"]["requests"], 3)
        self.assertEqual(result["runs"]["ex"]["prefix"], "run-ex")
        self.assertEqual({k: v for k, v in result["runs"]["ex"]["funnel"].items() if v},
                         {"extracting": 1, "extracted": 1, "exception": 1})
        self.assertEqual(result["runs"]["none"]["requests"], 0)
        self.assertIn("ex", progress_render.render_html(result))

    def test_html_escapes_metadata(self):
        result = progress.snapshot(self.root)
        result["recent"][0]["correlation_id"] = "<b>x</b>"
        page = progress_render.render_html(result)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn("<b>x</b>", page)


class CatalogAndIndexTests(ProgressCase):
    def test_catalog_coverage_counts_and_index_status(self):
        store.initialize(self.root)
        catalog.initialize(self.root)
        source = self.root / "notes.txt"
        source.write_bytes(b"Operator notes " + SECRET.encode())
        value = upload.descriptor(submission_id="sub-1", format_name="text", name="notes.txt",
                                  actor="test-runner", reason="Progress test")
        upload.submit(self.root, source, value)
        result = progress.snapshot(self.root)
        self.assertEqual(result["catalog"]["receipts"], 1)
        self.assertEqual(result["catalog"]["coverage"]["pending-extraction"], 1)
        self.assertEqual(result["catalog"]["extraction"]["pending"], 1)
        self.assertEqual(result["overall"]["funnel"]["received"], 1)
        self.assertFalse(result["index"]["available"])
        self.assertIn("error", result["index"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_index_freshness_counts_and_public_index(self):
        from tools.context_index import cli as index_cli
        manifest = self.root / "config/selection.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"schema_version": 1, "audience": policy.for_root(self.root)[0]["audience"],
                                        "documents": [], "context_rules": {}, "renditions": []}))
        with patch("sys.stdout"):
            self.assertEqual(index_cli.main(["--root", str(self.root), "build"]), 0)
        before = self.files()
        result = progress.snapshot(self.root)["index"]
        self.assertEqual(self.files(), before)
        self.assertTrue(result["available"])
        self.assertTrue(result["fresh"])
        self.assertEqual(result["indexed"], {"documents": 0, "sections": 0, "confidence": {
            "contract": "confidence-v1", "normal": 0, "low": 0, "low_reasons": {}}})
        self.assertEqual(result["selected"], {"documents": 0, "renditions": 0})
        db = self.root / "tools/context_index/.local/index.sqlite3"
        db.chmod(0o644)
        self.assertEqual(progress.snapshot(self.root)["index"]["error"], "not_private")


class CorruptLedgerTests(ProgressCase):
    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        self.db = self.root / ".local/intake/intake.sqlite3"

    def test_garbage_ledger_is_reported(self):
        self.db.write_bytes(b"not a database" * 100)
        result = progress.snapshot(self.root)
        self.assertFalse(result["ledger"]["available"])
        self.assertEqual(result["ledger"]["error"], "corrupt_store")
        self.assertIn("ledger unavailable", progress_render.render_html(result))

    def test_tampered_request_is_reported(self):
        self.drive_ok()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE requests SET request_sha256=?", ("0" * 64,))
        result = progress.snapshot(self.root)
        self.assertEqual(result["ledger"]["error"], "corrupt_request")
        self.assertEqual(result["overall"]["requests"], 0)

    def drive_ok(self):
        store.stage(self.root, request("run-one", PIPELINE))

    def test_public_ledger_and_locked_ledger_are_reported(self):
        self.db.chmod(0o644)
        self.assertEqual(progress.snapshot(self.root)["ledger"]["error"], "not_private")
        self.db.chmod(0o600)
        self.drive_ok()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            result = progress.snapshot(self.root, timeout=0.05)
        self.assertEqual(result["ledger"]["error"], "ledger_unavailable")

    def test_cli_reports_without_crashing(self):
        self.db.write_bytes(b"garbage")
        for argv in (["progress"], ["progress", "--format", "text"]):
            with patch("sys.stdout") as out:
                self.assertEqual(cli.main(["--root", str(self.root), *argv]), 0)
            written = "".join(call.args[0] for call in out.write.call_args_list)
            self.assertIn("corrupt_store", written)


RECEIPT = [("receipt", "catalog-submit", "receipt")]
PREPARE = [("extraction", "extract-prepare", "prepare")]
REGISTER = [("extraction", "extract-register", "register")]
STANDING = [("review", "standing-review", "review"), ("publication", "standing-select", "select")]
PUBLISH = [("publication", "standing-publish", "publish")]
OLD, NEW = "869257cf", "143c7b29"  # standing policy hashes: OLD first attempted before NEW


class DocumentTests(ProgressCase):
    """Kata nbta: the headline counts documents, each once, and leaves superseded reviews out."""

    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        self.db = self.root / ".local/intake/intake.sqlite3"

    def put(self, correlation, stages, statuses, code=None, at=1000.0, correction_of=None):
        """Insert one request with the given stage statuses, bypassing writers; failed stages get ``code``."""
        from tools.intake.contracts import digest
        raw = request(correlation, stages, correction_of)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                      (correlation, correction_of, raw, digest(raw), str(self.root)))
            for ordinal, ((_, _, name), status) in enumerate(zip(stages, statuses)):
                done = status == "succeeded"
                intent, evidence = json.dumps({"n": ordinal}), json.dumps({"ok": True})
                c.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?)", (
                    correlation, name, ordinal, status, 0, intent, digest(intent.encode()),
                    evidence if done else None, digest(evidence.encode()) if done else None))
                if status != "staged":
                    c.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        correlation, name, 0, 1, at + ordinal, at + ordinal + 0.5,
                        "succeeded" if done else "failed", None if done else "permanent",
                        None if done else code, None))

    def document(self, n, extract="succeeded", register="succeeded", rendition=None, code=None):
        """A cohort document: receipt, extraction and registration requests and its ``docs`` row."""
        sid = "inbox-%024x" % n
        self.put("upload-" + sid, RECEIPT, ["succeeded"])
        if extract is not None:
            self.put("extract-" + sid, PREPARE, [extract], code)
        if register is not None:
            self.put("register-" + sid, REGISTER, [register], code)
        state = "extracted" if extract == register == "succeeded" else (
            "received" if extract is None else extract if extract in {"blocked", "dead-letter"} else "extracting")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO docs(system, path, format, submission_id, sha256, receipt_id, original, state, "
                      "rendition, rendition_sha256, extract_head, register_head) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("sys", f"p{n}", "pdf", sid, "0" * 64, f"R{n}", "o", state, "r" if rendition else None,
                       rendition, None if extract is None else "extract-" + sid,
                       None if register is None else "register-" + sid))
        return sid

    def review(self, sid, rendition, policy_hash, statuses, code=None, at=1000.0):
        name = f"standing-{sid}-{rendition[:12]}-{policy_hash}"
        self.put(name, STANDING, statuses, code, at)
        return name

    def build(self):
        renditions = {n: hashlib.sha256(b"%d" % n).hexdigest() for n in range(10)}
        # Rejected and out of scope under the old policy, then reviewed again under the new one.
        a = self.document(1, rendition=renditions[1])
        old_scope = self.review(a, renditions[1], OLD, ["blocked", "staged"], "standing-exception-prohibited-scope", 100)
        self.review(a, renditions[1], NEW, ["succeeded", "staged"], at=200)
        b = self.document(2, rendition=renditions[2])
        old_rejected = self.review(b, renditions[2], OLD, ["blocked", "staged"],
                                   "standing-rejected-reviewer-rejected", 110)
        self.review(b, renditions[2], NEW, ["staged", "staged"])  # not attempted yet: still current
        self.document(3, rendition=renditions[3])  # extracted, awaiting review
        self.document(4, extract="blocked", register=None, code="needs-reader-docx")
        e = "inbox-%024x" % 5
        self.put("upload-" + e, RECEIPT, ["staged"])  # a manual upload: no docs row
        # The document was extracted again: its earlier rendition's review no longer counts.
        f = self.document(6, rendition=renditions[6])
        stale = self.review(f, renditions[7], NEW, ["blocked", "staged"], "standing-rejected-reviewer-rejected", 210)
        g = self.document(7, rendition=renditions[8])
        self.review(g, renditions[8], NEW, ["succeeded", "succeeded"], at=300)
        self.put("publish-" + "1" * 12 + "-x", PUBLISH, ["succeeded"], at=400)
        h = self.document(8, rendition=renditions[9])
        self.review(h, renditions[9], OLD, ["blocked", "staged"], "standing-pending-review-unresolved-evidence", 120)
        return {"old_scope": old_scope, "old_rejected": old_rejected, "stale": stale}

    def test_documents_are_counted_once_in_their_furthest_current_state(self):
        names = self.build()
        result = progress.snapshot(self.root, limit=100)
        documents = result["documents"]
        self.assertEqual((documents["source"], documents["documents"]), ("docs", 8))
        self.assertEqual(sum(documents["funnel"].values()), 8)
        self.assertEqual({k: v for k, v in documents["funnel"].items() if v},
                         {"qualified": 1, "pending-review": 2, "extracted": 2, "needs-reader": 1,
                          "staged": 1, "published": 1})
        self.assertEqual({(g["state"], g["funnel"], g["code"]): g["count"] for g in documents["attention_groups"]},
                         {("blocked", "needs-reader", "needs-reader-docx"): 1,
                          ("blocked", "pending-review", "standing-pending-review-unresolved-evidence"): 1})
        self.assertEqual(result["publications"]["requests"], 1)
        self.assertEqual(result["publications"]["last_published"], 400.5)
        self.assertEqual(documents["requests"]["publication"], 1)
        overall = result["overall"]
        self.assertEqual(overall["superseded_by"], {"correction": 0, "policy": 2, "rendition": 1, "submission": 0})
        self.assertEqual(overall["superseded"], 3)
        self.assertEqual((overall["funnel"]["exception"], overall["funnel"]["rejected"]), (0, 0))
        shown = {v["correlation_id"] for v in result["attention"] + result["recent"]}
        self.assertFalse(shown & set(names.values()))
        self.assertNotIn("standing-exception-prohibited-scope", {g["code"] for g in result["attention_groups"]})
        text = progress_render.render_text(result)
        self.assertIn("documents: 8 (from the docs table), 2 need attention;", text)
        self.assertIn("  group: 1 x blocked needs-reader needs-reader-docx", text)
        self.assertIn("policy=2, rendition=1", text)
        self.assertIn("Documents: 8", progress_render.render_html(result))
        self.assertNotIn(SECRET, json.dumps(result) + text + progress_render.render_html(result))
        self.assertEqual(progress.snapshot(self.root, limit=100)["documents"], documents)  # cached pass agrees

    def test_without_the_docs_table_documents_come_from_request_names(self):
        self.build()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("DROP TABLE docs")
        result = progress.snapshot(self.root, limit=100)
        documents = result["documents"]
        self.assertEqual((documents["source"], documents["documents"]), ("requests", 8))
        self.assertEqual(sum(documents["funnel"].values()), 8)
        # Without the docs table a rendition's staleness is unknown, so document 6 shows its review.
        self.assertEqual({k: v for k, v in documents["funnel"].items() if v},
                         {"qualified": 1, "pending-review": 2, "extracted": 1, "rejected": 1,
                          "needs-reader": 1, "staged": 1, "published": 1})
        self.assertEqual(result["overall"]["superseded_by"]["policy"], 2)
        self.assertIn("request names (no docs table)", progress_render.render_text(result))

    def test_a_corrected_submission_is_superseded_with_its_requests(self):
        old = self.document(1, rendition="d" * 64)
        self.review(old, "d" * 64, NEW, ["succeeded", "staged"])
        new = "inbox-%024x" % 2
        self.put("upload-" + new, RECEIPT, ["succeeded"], correction_of="upload-" + old)
        result = progress.snapshot(self.root)
        self.assertEqual(result["documents"]["documents"], 1)
        self.assertEqual({k: v for k, v in result["documents"]["funnel"].items() if v}, {"received": 1})
        self.assertEqual(result["overall"]["superseded_by"],
                         {"correction": 1, "policy": 0, "rendition": 0, "submission": 3})


class BindTests(unittest.TestCase):
    def test_non_loopback_binds_are_refused_unless_approved(self):
        for host in ("0.0.0.0", "::", "192.168.1.5", "example.com", ""):
            with self.subTest(host=host):
                with self.assertRaises(progress_serve.BindRefused):
                    progress_serve.check_bind(host)
        self.assertEqual(progress_serve.check_bind("0.0.0.0", allow_non_loopback=True), "0.0.0.0")
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertIn(progress_serve.check_bind(host), {"127.0.0.1", "::1"})

    def test_cli_refuses_non_loopback_before_binding(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout") as out, \
                patch.object(progress_serve, "serve") as serve:
            self.assertEqual(cli.main(["--root", root, "progress-serve", "--host", "0.0.0.0"]), 1)
        serve.assert_not_called()
        self.assertIn("non_loopback_bind", "".join(c.args[0] for c in out.write.call_args_list))


class ServerTests(ProgressCase):
    def test_serves_html_and_json_on_loopback_get_only(self):
        store.initialize(self.root)
        server = progress_serve.make_server(self.root, port=0, refresh=7)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/") as response:
            page = response.read().decode()
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertIn(progress_render.SCRIPT_SRC, response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn('content="7"', page)
        with urllib.request.urlopen(base + "/progress.json") as response:
            self.assertTrue(json.loads(response.read())["ledger"]["available"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/missing")
        caught.exception.close()
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(base + "/", data=b"x", method="POST"))
        caught.exception.close()
        self.assertEqual(caught.exception.code, 405)
        rebound = urllib.request.Request(base + "/", headers={"Host": "attacker.example"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(rebound)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 421)


if __name__ == "__main__":
    unittest.main()
