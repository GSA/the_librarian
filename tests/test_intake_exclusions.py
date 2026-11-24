"""Inbox exclusion rules and the selection ``deselect`` command (Kata bvvc), on synthetic fixtures."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.intake import cohort, deselect, exclusions, store
from tools.intake.contracts import IntakeError
from tests.test_intake_cohort import QualifyCase, tree

RULES = {"version": 1, "rules": [
    {"id": "node-modules", "reason": "dependency tree", "globs": ["**/node_modules/**"]},
    {"id": "maven-cache", "extensions": [".pom", ".LastUpdated"]},
    {"id": "svn", "globs": ["**/.svn/**", ".svn/**"]}]}
MANIFEST = "config/selection.local.json"
POLICY = "config/standing-policy.local.json"
RULES_FILE = "config/intake-exclusions.local.json"


def rules(value=None):
    return exclusions.parse(json.dumps(value or RULES).encode())


class RuleTests(unittest.TestCase):
    def test_globs_and_extensions_match_inbox_relative_paths(self):
        found = rules()
        cases = {
            "node_modules/x/index.md": "node-modules", "web/app/node_modules/a/b/c.txt": "node-modules",
            "web/node_modules": "node-modules", "my_node_modules/x.md": None,
            "lib/a.pom": "maven-cache", "lib/a.LASTUPDATED": "maven-cache", "lib/a.pom.txt": None,
            "a/.svn/pristine/ab/x.md": "svn", ".svn/entries": "svn", "a/svn/x.md": None,
            "docs/notes.md": None,
            "bundle.zip!/node_modules/x.md": "node-modules"}
        self.assertEqual({k: found.match(k) for k in cases}, cases)

    def test_single_star_stays_in_one_segment_and_the_first_rule_wins(self):
        found = rules({"version": 1, "rules": [
            {"id": "one", "globs": ["a/*/z.md"]}, {"id": "two", "globs": ["**/z.md"]}]})
        self.assertEqual((found.match("a/b/z.md"), found.match("a/b/c/z.md"), found.match("z.md")),
                         ("one", "two", "two"))

    def test_malformed_rule_files_are_refused(self):
        bad = [
            [], {"version": 2, "rules": RULES["rules"]}, {"version": 1}, {"version": 1, "rules": []},
            {"version": 1, "rules": [{"id": "x"}]}, {"version": 1, "rules": [{"id": "X", "globs": ["a"]}]},
            {"version": 1, "rules": [{"id": "x", "globs": ["a"]}, {"id": "x", "globs": ["b"]}]},
            {"version": 1, "rules": [{"id": "x", "globs": ["../a"]}]},
            {"version": 1, "rules": [{"id": "x", "globs": ["/a"]}]},
            {"version": 1, "rules": [{"id": "x", "extensions": ["pom"]}]},
            {"version": 1, "rules": [{"id": "x", "extensions": [".a/b"]}]},
            {"version": 1, "rules": [{"id": "x", "globs": ["a"], "extra": 1}]},
            {"version": 1, "rules": [{"id": "x", "globs": "a"}]}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(IntakeError) as caught:
                exclusions.validate(value)
            self.assertEqual(caught.exception.code, "invalid_exclusions")
        for raw in (b"{", b'{"version": 1, "version": 1}', b"\xff", b"x" * (exclusions.FILE_LIMIT + 1)):
            with self.subTest(raw=raw[:8]), self.assertRaises(IntakeError):
                exclusions.parse(raw)

    def test_the_shipped_example_is_valid(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue(exclusions.load(root / "config/intake-exclusions.example.json").rules)


class ExcludedCase(QualifyCase):
    """The shared cohort fixture plus an excluded dependency tree, Maven file and a duplicate."""

    def setUp(self):
        super().setUp()
        self.writable()
        extra = {"web/node_modules/pkg/readme.md": b"# Dependency\nVendored text\n",
                 "repo/.svn/pristine/ab/copy.md": b"# Pristine\nA pristine copy\n",
                 "cache/x.pom": b"<project>maven</project>\n",
                 "cache/pic.png": b"\x89PNG\r\n\x1a\nnot really"}
        for name, raw in extra.items():
            (self.inbox / name).parent.mkdir(parents=True, exist_ok=True)
            (self.inbox / name).write_bytes(raw)
        (self.root / "config").mkdir(exist_ok=True)
        self.rules_path = self.root / RULES_FILE
        self.rules_path.write_text(json.dumps(RULES))
        self.manifest = self.root / MANIFEST
        self.transport, self.calls = self._transport()

    def qualify(self, **extra):
        return self._qualify(self.transport, exclusions=exclusions.discover(self.root), **extra)

    def selected(self):
        return json.loads(self.manifest.read_text())["renditions"] if self.manifest.exists() else []

    def entry(self, report, relative):
        return next(e for e in report["entries"] if e["relative"] == relative)


class ClassifyAndIngestTests(ExcludedCase):
    def test_classify_reports_excluded_files_with_their_rule_and_underlying_disposition(self):
        before = tree(self.inbox)
        entries = cohort.classify(self.root, self.inbox, exclusions=exclusions.discover(self.root))
        got = {e["relative"]: e for e in entries if e["disposition"] == "excluded"}
        self.assertEqual({k: (v["rule"], v["underlying"]) for k, v in got.items()}, {
            "web/node_modules/pkg/readme.md": ("node-modules", "admit"),
            "repo/.svn/pristine/ab/copy.md": ("svn", "admit"),
            "cache/x.pom": ("maven-cache", "admit")})
        self.assertEqual(tree(self.inbox), before)
        plain = {e["relative"]: e["disposition"] for e in cohort.classify(self.root, self.inbox)}
        self.assertEqual(plain["cache/x.pom"], "admit")

    def test_an_excluded_file_that_is_not_admitted_is_still_reported_with_what_it_would_have_been(self):
        (self.inbox / "web/node_modules/empty.md").write_bytes(b"")
        entries = cohort.classify(self.root, self.inbox, exclusions=exclusions.discover(self.root))
        empty = next(e for e in entries if e["relative"] == "web/node_modules/empty.md")
        self.assertEqual((empty["disposition"], empty["underlying"], empty["rule"]),
                         ("excluded", "empty", "node-modules"))
        self.assertFalse(exclusions.receipted(empty))

    def test_cli_classify_counts_exclusions_by_default_file_and_without_bodies(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cohort.main(["classify", "--root", str(self.root), "--inbox", str(self.inbox)])
        self.assertEqual(code, 0)
        counts = json.loads(out.getvalue())["counts"]
        self.assertEqual(counts["disposition"]["excluded"], 3)
        self.assertEqual(counts["excluded"]["by_rule"], {"maven-cache": 1, "node-modules": 1, "svn": 1})
        self.assertEqual(counts["excluded"]["underlying"], {"admit": 3})
        self.assertEqual(counts["excluded"]["receipt_expected"], 3)
        self.assertNotIn("received", counts["excluded"])
        self.assertEqual(counts["excluded"]["rules"], ["node-modules", "maven-cache", "svn"])
        for body in ("Vendored text", "pristine copy", "<project>"):
            self.assertNotIn(body, out.getvalue())

    def test_ingest_gives_excluded_files_receipts_and_never_extracts_them(self):
        report = cohort_ingest_report(self)
        for name in ("web/node_modules/pkg/readme.md", "repo/.svn/pristine/ab/copy.md", "cache/x.pom"):
            entry = self.entry(report, name)
            self.assertEqual((entry["disposition"], entry["outcome"], entry["received"]),
                             ("excluded", "receipt-only", True), name)
            self.assertRegex(entry["receipt_id"], r"\AD\d+\Z")
            self.assertNotIn("rendition", entry)
        self.assertEqual(report["counts"]["excluded"]["received"], 3)
        self.assertEqual(report["counts"]["excluded"]["outcome"], {"receipt-only": 3})
        # Receipts are durable: the ledger's docs rows hold them as ``received``, not ``extracted``.
        with store.locked(self.root) as c:
            states = {r["path"]: r["state"] for r in c.execute("SELECT path, state FROM docs")}
        self.assertEqual(states["cache/x.pom"], "received")
        self.assertEqual(states["a/notes.md"], "extracted")
        held = catalog.read_records(self.root, kind="receipt", limit=100)["records"]
        self.assertEqual(len(held), 6 + 3)

    def test_ingest_rerun_is_idempotent_and_changed_bytes_still_correct_the_receipt(self):
        first = cohort_ingest_report(self)
        state = catalog.status(self.root)["state_token"]
        second = cohort_ingest_report(self)
        self.assertEqual(catalog.status(self.root)["state_token"], state)
        self.assertEqual(self.entry(first, "cache/x.pom")["receipt_id"], self.entry(second, "cache/x.pom")["receipt_id"])
        (self.inbox / "cache/x.pom").write_bytes(b"<project>changed</project>\n")
        third = cohort_ingest_report(self)
        self.assertEqual(self.entry(third, "cache/x.pom")["outcome"], "receipt-only")
        self.assertNotEqual(self.entry(third, "cache/x.pom")["receipt_id"], self.entry(first, "cache/x.pom")["receipt_id"])

    def test_ingest_limit_does_not_hold_back_receipts_and_trust_stat_never_extracts_an_excluded_file(self):
        from tools.intake import cohort_ingest
        cohort_ingest.ingest(self.root, self.inbox, system="inbox", actor="runner")  # no rules: all extracted
        report = cohort_ingest.ingest(self.root, self.inbox, system="inbox", actor="runner", trust_stat=True,
                                      exclusions=exclusions.discover(self.root))
        entry = self.entry(report, "cache/x.pom")
        self.assertEqual((entry["disposition"], entry["outcome"]), ("excluded", "receipt-only"))
        self.assertNotIn("rendition", entry)


def cohort_ingest_report(case, **extra):
    from tools.intake import cohort_ingest
    return cohort_ingest.ingest(case.root, case.inbox, system="inbox", actor="runner",
                                exclusions=exclusions.discover(case.root), **extra)


class QualifyExclusionTests(ExcludedCase):
    def test_excluded_files_are_never_sent_to_the_model_nor_selected_but_are_reported(self):
        report = self.qualify()
        self.assertEqual(len(self.calls), 6)
        sent = "\n".join(m[-1]["content"] for _, m in self.calls)
        for body in ("Vendored text", "pristine copy", "<project>"):
            self.assertNotIn(body, sent)
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        self.assertEqual(len(self.selected()), 6)
        self.assertEqual(report["counts"]["excluded"]["received"], 3)
        entry = self.entry(report, "cache/x.pom")
        self.assertEqual((entry["disposition"], entry["rule"], entry["outcome"]),
                         ("excluded", "maven-cache", "receipt-only"))
        self.assertNotIn("qualification", entry)
        self.assertEqual(report["counts"]["published"], 1)

    def test_rerun_is_idempotent_without_model_calls_or_new_receipts(self):
        self.qualify()
        state = catalog.status(self.root)["state_token"]
        manifest = self.manifest.read_bytes()
        calls = len(self.calls)
        report = self.qualify()
        self.assertEqual((len(self.calls), catalog.status(self.root)["state_token"], self.manifest.read_bytes()),
                         (calls, state, manifest))
        self.assertEqual(report["counts"]["excluded"]["received"], 3)

    def test_a_rule_added_later_leaves_existing_selections_until_deselect_removes_them(self):
        self.rules_path.unlink()
        first = self.qualify()
        self.assertEqual(len(self.selected()), 6 + 4 - 1)  # the png fixture is not a readable image
        self.assertEqual(len(first["entries"]), len(first["entries"]))
        self.rules_path.write_text(json.dumps(RULES))
        calls = len(self.calls)
        again = self.qualify()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(self.selected()), 9, "an exclusion never removes a selection by itself")
        self.assertEqual(again["counts"]["excluded"]["files"], 3)

    def test_a_selection_survives_its_file_changing_or_becoming_unadmittable_while_excluded(self):
        self.rules_path.unlink()
        self.qualify()
        selected = {s["receipt_id"] for s in self.selected()}
        self.rules_path.write_text(json.dumps(RULES))
        self.writable()
        (self.inbox / "cache/x.pom").write_bytes(b"<project>changed bytes</project>\n")  # a new receipt
        (self.inbox / "web/node_modules/pkg/readme.md").write_bytes(b"")  # no longer admitted
        calls = len(self.calls)
        report = self.qualify()
        self.assertEqual(len(self.calls), calls)
        self.assertTrue(selected <= {s["receipt_id"] for s in self.selected()})
        self.assertEqual(self.entry(report, "web/node_modules/pkg/readme.md")["underlying"], "empty")

    def test_an_explicit_exclusions_file_overrides_the_default_and_agent_origin_refuses_it(self):
        other = Path(self.temp.name) / "rules.json"
        other.write_text(json.dumps({"version": 1, "rules": [{"id": "txt", "extensions": [".txt"]}]}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cohort.main(["classify", "--root", str(self.root), "--inbox", str(self.inbox),
                         "--exclusions", str(other)])
        self.assertEqual(json.loads(out.getvalue())["counts"]["excluded"]["rules"], ["txt"])
        origin = Path(self.temp.name) / "origin.json"
        origin.write_text(json.dumps({"version": 1, "generator": "a-generator"}))
        with self.assertRaises(IntakeError) as caught:
            exclusions.resolve(self.root, other, {"generator": "x"})
        self.assertEqual(caught.exception.code, "exclusions_not_for_agent_origin")
        self.assertIsNone(exclusions.resolve(self.root, None, {"generator": "x"}))

    def test_a_malformed_or_symlinked_default_file_refuses_before_anything_is_written(self):
        self.rules_path.write_text("{")
        with self.assertRaises(IntakeError) as caught:
            exclusions.discover(self.root)
        self.assertEqual(caught.exception.code, "invalid_exclusions")
        self.rules_path.unlink()
        os.symlink(Path(self.temp.name) / "elsewhere.json", self.rules_path)
        with self.assertRaises(IntakeError):
            exclusions.discover(self.root)
        self.rules_path.unlink()
        self.assertIsNone(exclusions.discover(self.root))


class DeselectCase(ExcludedCase):
    """A cohort qualified without rules, so the excluded classes are selected and can be removed."""

    def setUp(self):
        super().setUp()
        self.writable()
        for name in ("copy-a.md", "docs/copy-b.md", "docs/deep/copy-c.md"):
            (self.inbox / name).parent.mkdir(parents=True, exist_ok=True)
            (self.inbox / name).write_bytes(b"# Same\nIdentical duplicate text\n")
        self.rules_path.unlink()
        self.first = self._qualify(self.transport)
        self.rules_path.write_text(json.dumps(RULES))
        self.rules = exclusions.discover(self.root)

    def run_deselect(self, **extra):
        args = dict(rules=None)
        args.update(extra)
        return deselect.deselect(self.root, MANIFEST, **args)

    def receipt(self, relative):
        return self.entry(self.first, relative)["receipt_id"]

    def publish(self):
        return cohort.publish(self.root, policy_path=POLICY, manifest=MANIFEST)

    def hits(self, text):
        from tools.context_index import context_index as idx
        with contextlib.closing(idx.readonly(self.root / cohort.DEFAULT_INDEX)) as db:
            return idx.search(db, text, 10, None, root=self.root)["hits"]

    def log(self):
        _, home, _ = store.locations(self.root)
        path = home / deselect.LOG
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class DeselectTests(DeselectCase):
    def test_the_fixture_selected_the_low_value_classes(self):
        names = {self.entry(self.first, n)["receipt_id"] for n in
                 ("web/node_modules/pkg/readme.md", "repo/.svn/pristine/ab/copy.md", "cache/x.pom")}
        self.assertTrue(names <= {s["receipt_id"] for s in self.selected()})

    def test_dry_run_is_the_default_writes_nothing_and_reports_by_rule(self):
        before = (self.manifest.read_bytes(), catalog.status(self.root)["state_token"])
        report = self.run_deselect(rules=self.rules, rule_ids=["node-modules", "svn"])
        self.assertFalse(report["apply"])
        self.assertEqual(report["counts"]["deselect"], 2)
        self.assertEqual(report["counts"]["by_rule"], {"node-modules": 1, "svn": 1})
        self.assertEqual(report["counts"]["remaining"], len(self.selected()) - 2)
        self.assertEqual(self.log(), [])
        self.assertEqual((self.manifest.read_bytes(), catalog.status(self.root)["state_token"]), before)
        self.assertEqual(len(report["entries"]), 2)

    def test_apply_removes_only_the_matching_selections_and_keeps_every_receipt(self):
        receipts_before = {r["id"] for r in catalog.read_records(self.root, kind="receipt", limit=100)["records"]}
        dropped = {self.receipt("web/node_modules/pkg/readme.md"), self.receipt("repo/.svn/pristine/ab/copy.md"),
                   self.receipt("cache/x.pom")}
        report = self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True, reason="low value")
        self.assertEqual(report["counts"]["deselect"], 3)
        self.assertEqual(report["review_events"], 3)
        left = {s["receipt_id"] for s in self.selected()}
        self.assertTrue(left.isdisjoint(dropped))
        self.assertEqual(len(left), report["counts"]["remaining"])
        index.load_manifest(self.manifest, self.root)
        self.assertEqual({r["id"] for r in catalog.read_records(self.root, kind="receipt", limit=100)["records"]},
                         receipts_before)
        # Ledger evidence: a begin record naming every entry, then a done record.
        begin, done = self.log()
        self.assertEqual((begin["kind"], done["kind"]), ("begin", "done"))
        self.assertEqual({e[0] for e in begin["entries"]}, dropped)
        self.assertEqual(begin["actor"], "operator")
        self.assertEqual((done["begin_id"], done["plan_sha256"]), (begin["id"], begin["plan_sha256"]))
        self.assertEqual(done["manifest_sha256_after"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        # Review-event evidence: each removed review is superseded by a deselect event.
        from tools.review_events import event_reader
        snapshot = event_reader.read_snapshot(self.root)
        for entry in report["entries"]:
            (event_id,) = entry["review_event_ids"]
            successor = snapshot.event(snapshot.superseded_by(event_id))
            self.assertEqual((successor["outcome"], successor["reviewer"]), ("dispositioned", "operator"))
            self.assertIn("exclusion rule", successor["reason"])
            self.assertIn("low value", successor["reason"])

    def test_apply_is_idempotent_and_a_rerun_changes_nothing(self):
        self.run_deselect(rules=self.rules, actor="operator", apply=True, rule_ids=[])
        state = (self.manifest.read_bytes(), catalog.status(self.root)["state_token"],
                 __import__("tools.review_events.event_store", fromlist=["x"]).status(self.root)["state_token"],
                 self.log())
        again = self.run_deselect(rules=self.rules, actor="operator", apply=True, rule_ids=[])
        self.assertEqual(again["counts"]["deselect"], 0)
        self.assertEqual((self.manifest.read_bytes(), catalog.status(self.root)["state_token"],
                          __import__("tools.review_events.event_store", fromlist=["x"]).status(self.root)["state_token"],
                          self.log()), state)

    def test_apply_needs_an_actor_and_validates_selectors_before_writing(self):
        before = self.manifest.read_bytes()
        for extra, code in (({"apply": True}, "actor_required"), ({"receipts": ["x1"]}, "invalid_receipts"),
                            ({"rule_ids": ["nope"], "rules": self.rules}, "unknown_rule"),
                            ({"rule_ids": ["svn"]}, "exclusions_missing")):
            with self.subTest(extra), self.assertRaises(IntakeError) as caught:
                self.run_deselect(**extra)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.manifest.read_bytes(), self.log()), (before, []))

    def test_deselect_by_receipt_and_by_report_file(self):
        target = self.receipt("a/notes.md")
        report = self.run_deselect(receipts=[target, "D999999"], actor="operator", apply=True)
        self.assertEqual((report["counts"]["deselect"], report["unmatched"]), (1, ["D999999"]))
        self.assertNotIn(target, {s["receipt_id"] for s in self.selected()})
        dry = self.run_deselect(rules=self.rules, rule_ids=["svn"])
        path = Path(self.temp.name) / "plan.json"
        path.write_text(json.dumps(dry))
        self.assertEqual(deselect.read_receipts(path), [self.receipt("repo/.svn/pristine/ab/copy.md")])
        listing = Path(self.temp.name) / "ids.txt"
        listing.write_text("# comment\n" + self.receipt("m/plain.txt") + "\n\n")
        self.assertEqual(deselect.read_receipts(listing), [self.receipt("m/plain.txt")])
        listing.write_text("not-a-receipt\n")
        with self.assertRaises(IntakeError):
            deselect.read_receipts(listing)

    def test_a_deselected_entry_is_not_selected_again_by_a_rerun(self):
        self.run_deselect(rules=self.rules, actor="operator", apply=True, rule_ids=[])
        selected = {s["receipt_id"] for s in self.selected()}
        calls = len(self.calls)
        self.rules_path.unlink()  # the classes are admitted again, as if the rule never existed
        report = self._qualify(self.transport)
        self.assertEqual({s["receipt_id"] for s in self.selected()}, selected)
        self.assertEqual(len(self.calls), calls, "no new model call for what was deselected")
        codes = {e["relative"]: e.get("review_code") for e in report["entries"]}
        self.assertEqual(codes["cache/x.pom"], "review-superseded")

    def test_publish_after_deselect_serves_only_what_is_left(self):
        self.publish()
        self.assertEqual(len(self.hits("Vendored")), 1)
        self.run_deselect(rules=self.rules, actor="operator", apply=True, rule_ids=["node-modules"])
        report = self.publish()
        self.assertEqual(report["counts"]["published"], 1)
        self.assertEqual(self.hits("Vendored"), [])
        self.assertTrue(self.hits("pristine"))
        self.assertEqual(report["counts"]["selections"], len(self.selected()))

    def test_publish_before_deselect_is_stale_until_republished(self):
        self.publish()
        self.run_deselect(rules=self.rules, actor="operator", apply=True, rule_ids=["node-modules"])
        self.assertEqual(len(self.hits("Vendored")), 1, "the old index still serves until publish runs")
        self.publish()
        self.assertEqual(self.hits("Vendored"), [])


class DeselectCrashTests(DeselectCase):
    def crash(self, target, **extra):
        with mock.patch.object(target[0], target[1], side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True, **extra)

    def test_a_crash_after_the_log_but_before_the_events_is_finished_by_a_rerun(self):
        before = self.manifest.read_bytes()
        self.crash((deselect, "_supersessions"))
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual([r["kind"] for r in self.log()], ["begin"])
        report = self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True)
        self.assertEqual((report["counts"]["deselect"], report["review_events"]), (3, 3))
        self.assertEqual([r["kind"] for r in self.log()], ["begin", "done"])
        self.assertEqual(len(self.selected()), report["counts"]["remaining"])

    def test_a_crash_after_the_events_but_before_the_manifest_rewrite_is_finished_by_a_rerun(self):
        from tools.review_events import event_store as events
        self.crash((standing_publication_module(), "_replace_validated"))
        committed = events.status(self.root)["state_token"]
        self.assertEqual(len(self.selected()), len(json.loads(self.manifest.read_text())["renditions"]))
        self.assertEqual(len(self.selected()), 6 + 3 + 3)
        report = self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True)
        self.assertEqual(report["counts"]["deselect"], 3)
        self.assertEqual(report["review_events"], 0, "the earlier run's events are not appended twice")
        self.assertEqual(events.status(self.root)["state_token"], committed)
        self.assertEqual(len(self.selected()), 6 + 3)
        self.assertEqual([r["kind"] for r in self.log()], ["begin", "done"])

    def test_a_crash_before_the_done_record_leaves_a_clean_rerun_that_only_closes_the_log(self):
        original = deselect._append

        def flaky(path, record):
            if record["kind"] == "done":
                raise KeyboardInterrupt
            return original(path, record)
        with mock.patch.object(deselect, "_append", flaky), self.assertRaises(KeyboardInterrupt):
            self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True)
        self.assertEqual([r["kind"] for r in self.log()], ["begin"])
        remaining = self.manifest.read_bytes()
        report = self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True)
        self.assertEqual((report["counts"]["deselect"], self.manifest.read_bytes()), (0, remaining))
        self.assertEqual([r["kind"] for r in self.log()], ["begin", "done"])

    def test_an_interrupted_append_is_recovered_before_the_selection_is_planned(self):
        self.crash((deselect, "_supersessions"))
        intent = selection_intent(self.root)
        self.assertFalse(os.path.lexists(intent))
        self.assertTrue(self.log())

    def test_a_torn_last_log_line_is_ignored_and_repaired(self):
        self.crash((deselect, "_supersessions"))
        _, home, _ = store.locations(self.root)
        with open(home / deselect.LOG, "ab") as stream:
            stream.write(b'{"kind": "don')
        report = self.run_deselect(rules=self.rules, rule_ids=[], actor="operator", apply=True)
        self.assertEqual(report["counts"]["deselect"], 3)
        self.assertEqual([r["kind"] for r in self.log()], ["begin", "done"])


def standing_publication_module():
    from tools.intake import standing_publication
    return standing_publication


def selection_intent(root):
    from tools.intake import selection_manifest
    return selection_manifest.intent_path(root, MANIFEST)


class DuplicateTests(DeselectCase):
    def groups(self, report):
        return {tuple(g["receipts"]): g for g in report["groups"]}

    def test_byte_identical_selections_across_receipts_are_reported_as_groups_without_removing(self):
        report = self.run_deselect()
        copies = sorted((self.receipt(n) for n in ("copy-a.md", "docs/copy-b.md", "docs/deep/copy-c.md")),
                        key=lambda r: int(r[1:]))
        group = next(g for g in report["groups"] if set(copies) <= set(g["receipts"]))
        self.assertEqual((group["receipts"], group["keep"], group["removed"]), (copies, copies[0], []))
        self.assertEqual(report["counts"]["deselect"], 0)
        self.assertEqual(report["counts"]["duplicates"]["deselected"], 0)
        self.assertGreaterEqual(report["counts"]["duplicates"]["extra_selections"], 2)

    def test_dedupe_keeps_the_lowest_receipt_and_removes_the_others(self):
        copies = sorted((self.receipt(n) for n in ("copy-a.md", "docs/copy-b.md", "docs/deep/copy-c.md")),
                        key=lambda r: int(r[1:]))
        report = self.run_deselect(dedupe=True, actor="operator", apply=True)
        left = {s["receipt_id"] for s in self.selected()}
        self.assertIn(copies[0], left)
        self.assertTrue(left.isdisjoint(copies[1:]))
        self.assertEqual(report["counts"]["by_reason"], {"duplicate-of": 2})
        group = next(g for g in report["groups"] if copies[0] in g["receipts"])
        self.assertEqual(group["removed"], copies[1:])
        # Receipts of the removed copies still exist; only the selections went.
        held = {r["id"] for r in catalog.read_records(self.root, kind="receipt", limit=100)["records"]}
        self.assertTrue(set(copies) <= held)

    def test_dedupe_never_removes_the_last_copy_when_a_rule_removes_the_others(self):
        sub = {"version": 1, "rules": [{"id": "docs-copies", "globs": ["docs/**"]}]}
        rules_ = rules(sub)
        report = self.run_deselect(rules=rules_, rule_ids=["docs-copies"], dedupe=True, actor="operator", apply=True)
        kept = {s["receipt_id"] for s in self.selected()}
        self.assertIn(self.receipt("copy-a.md"), kept)
        self.assertNotIn(self.receipt("docs/copy-b.md"), kept)
        self.assertNotIn(self.receipt("docs/deep/copy-c.md"), kept)
        self.assertEqual(report["counts"]["by_reason"].get("duplicate-of", 0), 0)

    def test_when_a_rule_removes_the_first_copy_dedupe_keeps_one_of_the_rest(self):
        sub = {"version": 1, "rules": [{"id": "root-copy", "globs": ["copy-a.md"]}]}
        self.run_deselect(rules=rules(sub), rule_ids=["root-copy"], dedupe=True, actor="operator", apply=True)
        kept = {s["receipt_id"] for s in self.selected()}
        copies = [self.receipt(n) for n in ("copy-a.md", "docs/copy-b.md", "docs/deep/copy-c.md")]
        self.assertEqual(sum(c in kept for c in copies), 1)
        self.assertNotIn(copies[0], kept)


class DuplicateWithoutDocsTests(DeselectCase):
    """The same duplicate detection when the optional ``docs`` table is absent or lacks a row."""

    def test_dedupe_finds_groups_from_the_catalog_without_the_docs_table(self):
        with store.locked(self.root) as c:
            c.execute("DROP TABLE docs")
        copies = sorted((self.receipt(n) for n in ("copy-a.md", "docs/copy-b.md", "docs/deep/copy-c.md")),
                        key=lambda r: int(r[1:]))
        report = self.run_deselect(dedupe=True, actor="operator", apply=True)
        left = {s["receipt_id"] for s in self.selected()}
        self.assertIn(copies[0], left)
        self.assertTrue(left.isdisjoint(copies[1:]))
        self.assertEqual(report["counts"]["unmapped"], 2)

    def test_a_selection_whose_row_moved_to_a_newer_receipt_is_still_grouped(self):
        copies = sorted((self.receipt(n) for n in ("copy-a.md", "docs/copy-b.md", "docs/deep/copy-c.md")),
                        key=lambda r: int(r[1:]))
        with store.locked(self.root) as c:
            c.execute("DELETE FROM docs WHERE receipt_id=?", (copies[1],))
        report = self.run_deselect()
        group = next(g for g in report["groups"] if copies[0] in g["receipts"])
        self.assertEqual(group["receipts"], copies)


class DeselectCliTests(DeselectCase):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cohort.main(["deselect", "--root", str(self.root), "--manifest", MANIFEST, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_cli_dry_run_prints_body_free_counts_and_a_private_report(self):
        report = Path(self.temp.name) / "plan.json"
        code, out, _ = self.main("--all-rules", "--report", str(report))
        self.assertEqual(code, 0)
        printed = json.loads(out)
        self.assertFalse(printed["apply"])
        self.assertEqual(printed["counts"]["deselect"], 3)
        self.assertNotIn("entries", printed)
        for text in ("node_modules", "pristine", "x.pom", "Vendored"):
            self.assertNotIn(text, out)
        saved = json.loads(report.read_text())
        self.assertEqual(len(saved["entries"]), 3)
        self.assertEqual(oct(report.stat().st_mode & 0o777), "0o600")
        # The report feeds a later apply.
        code, out, _ = self.main("--receipts-file", str(report), "--apply", "--actor", "operator")
        self.assertEqual((code, json.loads(out)["counts"]["deselect"]), (0, 3))

    def test_cli_errors_are_body_free_and_apply_needs_an_actor(self):
        code, _, err = self.main("--all-rules", "--apply")
        self.assertEqual((code, json.loads(err)), (2, {"error": "actor_required"}))
        self.rules_path.unlink()
        code, _, err = self.main("--rule", "svn")
        self.assertEqual((code, json.loads(err)), (2, {"error": "exclusions_missing"}))


class PolicyBumpTests(DeselectCase):
    def test_a_new_policy_version_cannot_silently_reselect_a_deselected_entry(self):
        self.run_deselect(rules=self.rules, actor="operator", apply=True, rule_ids=[])
        dropped = {self.receipt("cache/x.pom"), self.receipt("repo/.svn/pristine/ab/copy.md")}
        policy_file = self.root / POLICY
        raw = json.loads(policy_file.read_text())
        raw["version"] += 1
        policy_file.write_text(json.dumps(raw))
        self.rules_path.unlink()
        report = self._qualify(self.transport)
        self.assertTrue(dropped.isdisjoint({s["receipt_id"] for s in self.selected()}))
        codes = {e["relative"]: e.get("review_code") for e in report["entries"]}
        self.assertEqual(codes["cache/x.pom"], "standing-pending-review-conflicting-review")
