"""Retrieval-quality evaluation: case schema, generation, scoring, staleness and comparison."""

from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools.context_index import context_index as index
from tools.evals import cases as case_file
from tools.evals import cli, compare, generate, runner
from tools.evals.cases import EvalError
from tests.support import WorkspaceCase

DOCUMENTS = {
    "orbit.md": "# Orbital Mechanics\n\nKepler ellipse periapsis apoapsis notes.\n\n"
                "## Transfer Windows\n\nHohmann transfer burn timing.\n",
    "garden.md": "# Garden Planning\n\nTomato trellis compost rotation.\n\n"
                 "## Irrigation\n\nDrip emitter spacing schedule.\n",
    "kitchen.md": "# Kitchen Inventory\n\nSaffron cardamom pantry rotation.\n",
    "shared.md": "# Shared Rotation\n\nRotation rotation rotation overview.\n",
}


def case(**fields):
    return {"id": "c1", "origin": "authored", "query": "alpha", "k": 5, **fields}


class CaseSchemaTests(unittest.TestCase):
    def refused(self, cases, message):
        with self.assertRaisesRegex(EvalError, message):
            case_file.validate({"schema": case_file.SCHEMA, "cases": cases})

    def test_valid_cases_of_every_kind(self):
        case_file.validate({"schema": case_file.SCHEMA, "cases": [
            case(id="hit", expect_paths=["documents/a.md"], expect_confidence="low", tags=["x"]),
            case(id="locator", expect_locators=["documents/a.md#alpha"], low_confidence="last"),
            case(id="never", must_not_paths=["documents/b.md"], path="documents/a.md"),
            case(id="gone", expect_absent=True, expect_absence="absent-corpus"),
            case(id="gen", origin="generated", expect_paths=["documents/a.md"],
                 generator={"method": "known-item-v1", "seed": 3}),
        ]})

    def test_refuses_malformed_cases(self):
        for cases, message in (
            ([case(expect_paths=["documents/a.md"], extra=1)], "unknown fields: extra"),
            ([case(expect_paths=["a.md"]), case(expect_paths=["a.md"])], "Duplicate case id"),
            ([case()], "expects nothing"),
            ([case(expect_absent=True, expect_paths=["a.md"])], "absence case cannot"),
            ([case(expect_absence="absent-corpus", expect_paths=["a.md"])], "requires expect_absent"),
            ([case(expect_confidence="low", must_not_paths=["a.md"])], "needs expect_paths"),
            ([case(expect_paths=["../a.md"])], "root-relative"),
            ([case(expect_paths=["a.md#x"])], "not locators"),
            ([case(k=0, expect_paths=["a.md"])], "k must be"),
            ([case(query="!!", expect_paths=["a.md"])], "at least one word"),
            ([case(origin="generated", expect_paths=["a.md"])], "needs generator"),
            ([case(expect_paths=["a.md"], generator={"method": "known-item-v1", "seed": 1})],
             "only a generated case"),
            ([case(id="Bad Id", expect_paths=["a.md"])], "needs an id"),
            ([case(expect_paths=["a.md"], low_confidence="hide")], "low_confidence must be"),
            ([case(expect_paths=["a.md"], must_not_paths=["a.md"])], "must-not path cannot"),
            ([case(expect_locators=["a.md#x"], must_not_paths=["a.md"])], "must-not path cannot"),
        ):
            with self.subTest(message=message):
                self.refused(cases, message)
        with self.assertRaisesRegex(EvalError, "Unsupported cases schema"):
            case_file.validate({"schema": "retrieval-eval-cases-v0", "cases": []})

    def test_read_refuses_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text('{"schema": "retrieval-eval-cases-v1", "schema": "x", "cases": []}')
            with self.assertRaisesRegex(EvalError, "Duplicate JSON field"):
                case_file.read(path)


class EvalWorkspace(WorkspaceCase):
    def setUp(self):
        super().setUp()
        (self.root / "documents").mkdir()
        for name, text in DOCUMENTS.items():
            (self.root / "documents" / name).write_text(text)
        self.config["documents"] = [{"path": f"documents/{name}", "role": "toolkit-documentation",
                                     "context": []} for name in DOCUMENTS]
        self.build()
        self.cases = self.root / case_file.DEFAULT_CASES

    def write_cases(self, cases):
        self.cases.write_text(json.dumps({"schema": case_file.SCHEMA, "cases": cases}))

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def scorecard(self, cases, **options):
        self.write_cases(cases)
        document, sha = case_file.read(self.cases)
        return runner.run(self.root, document, sha, **options)


class GenerateTests(EvalWorkspace):
    def test_generation_is_deterministic_and_keeps_authored_cases(self):
        authored = case(id="mine", query="saffron", expect_paths=["documents/kitchen.md"])
        self.write_cases([authored])
        code, out, err = self.main("generate", "--seed", "7", "--count", "3", "--absences", "2")
        self.assertEqual(code, 0, err)
        first = self.cases.read_bytes()
        self.assertEqual(stat.S_IMODE(self.cases.stat().st_mode), 0o600)
        document = json.loads(first)
        self.assertEqual(document["cases"][0], authored)
        generated = [c for c in document["cases"] if c["origin"] == "generated"]
        self.assertEqual(len([c for c in generated if c["generator"]["method"] == "known-item-v1"]), 3)
        self.assertEqual(len([c for c in generated if c.get("expect_absent")]), 2)
        for item in generated:
            if "expect_paths" in item:
                terms = index.query_terms(item["query"])
                self.assertTrue(all(t not in generate.STOPWORDS for t in terms))
                with closing(index.readonly(self.db)) as db:
                    self.assertTrue(index.search(db, item["query"], 50, item["expect_paths"][0],
                                                 root=self.root)["hits"], item["id"])
        self.assertEqual(self.main("generate", "--seed", "7", "--count", "3", "--absences", "2")[0], 0)
        self.assertEqual(self.cases.read_bytes(), first)
        code, out, err = self.main("generate", "--seed", "8", "--count", "2", "--absences", "1")
        self.assertEqual(code, 0, err)
        seeds = [c["generator"]["seed"] for c in json.loads(self.cases.read_bytes())["cases"]
                 if c["origin"] == "generated"]
        self.assertEqual((seeds.count(7), seeds.count(8)), (5, 3))
        self.assertEqual(json.loads(self.cases.read_bytes())["cases"][0], authored)

    def test_generated_id_clashing_with_an_authored_case_is_refused(self):
        clash = generate.case_id(7, "absent", "001")
        self.write_cases([case(id=clash, expect_absent=True)])
        before = self.cases.read_bytes()
        code, _, err = self.main("generate", "--seed", "7", "--count", "0", "--absences", "1")
        self.assertEqual(code, 2)
        self.assertIn("collide", err)
        self.assertEqual(self.cases.read_bytes(), before)

    def test_documents_without_usable_terms_are_skipped(self):
        (self.root / "documents/empty.md").write_text("# The\n\nand or but the it.\n")
        self.config["documents"] = [{"path": "documents/empty.md", "role": "toolkit-documentation",
                                     "context": []}]
        self.build()
        with closing(index.readonly(self.db)) as db:
            cases, summary = generate.generate(db, seed=1, count=1, absences=0)
        self.assertEqual((cases, summary["skipped"]), ([], 1))

    def test_near_duplicate_documents_do_not_share_a_known_item_query(self):
        for name in ("copy1.md", "copy2.md"):
            (self.root / "documents" / name).write_text("# Copy\n\nquokka wombat numbat.\n")
        self.config["documents"] = [{"path": f"documents/{name}", "role": "toolkit-documentation",
                                     "context": []} for name in ("copy1.md", "copy2.md")]
        self.build()
        with closing(index.readonly(self.db)) as db:
            cases, summary = generate.generate(db, seed=1, count=2, absences=0)
        self.assertEqual((len(cases), summary["skipped"]), (1, 1))
        with closing(index.readonly(self.db)) as db, patch.object(generate, "SPECIFIC", 1):
            cases, summary = generate.generate(db, seed=1, count=2, absences=0)
        self.assertEqual((cases, summary["skipped"]), ([], 2))

    def test_allocation_is_proportional_with_a_floor(self):
        strata = {("r", "normal", "text"): ["p"] * 90, ("r", "low", "text"): ["p"] * 10,
                  ("r", "low", "mhtml"): ["p"]}
        quotas = generate.allocate(strata, 20)
        self.assertEqual(sum(quotas.values()), 20)
        self.assertEqual(quotas[("r", "low", "mhtml")], 1)
        self.assertGreater(quotas[("r", "normal", "text")], quotas[("r", "low", "text")])


class RunTests(EvalWorkspace):
    def test_scores_hits_ranks_misses_must_not_absence_and_confidence(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE document_confidence SET level='low', label=? WHERE path=?",
                       (json.dumps({"level": "low", "reasons": [
                           {"code": "reviewer-deferred", "details": []}]}), "documents/garden.md"))
        card = self.scorecard([
            case(id="hit", query="saffron cardamom", expect_paths=["documents/kitchen.md"],
                 expect_confidence="normal"),
            case(id="locator", query="hohmann", expect_locators=["documents/orbit.md#transfer-windows"]),
            case(id="lowhit", query="tomato trellis", expect_paths=["documents/garden.md"],
                 expect_confidence="normal"),
            case(id="miss", query="rotation", k=1, expect_paths=["documents/kitchen.md"]),
            case(id="never", query="rotation", must_not_paths=["documents/shared.md"]),
            case(id="gone", query="zzqqxx", expect_absent=True, expect_absence="absent-corpus"),
            case(id="partial", query="saffron zzqqxx", expect_absent=True, expect_absence="absent-corpus"),
            case(id="present", query="saffron", expect_absent=True),
            case(id="excluded", query="tomato", low_confidence="exclude", expect_absent=True,
                 expect_absence="low-confidence-excluded"),
            case(id="broken", query="saffron", path="documents/missing.md",
                 expect_paths=["documents/kitchen.md"]),
        ])
        results = {item["id"]: item for item in card["cases"]}
        self.assertEqual((results["hit"]["status"], results["hit"]["rank"]), ("pass", 1))
        self.assertEqual(results["locator"]["status"], "pass")
        self.assertEqual(results["lowhit"]["reasons"], ["confidence-mismatch"])
        self.assertEqual(results["lowhit"]["observed_confidence"], "low")
        self.assertEqual(results["miss"]["reasons"], ["expected-not-in-top-k"])
        self.assertIsNone(results["miss"]["rank"])
        self.assertEqual(results["never"]["reasons"], ["must-not-returned"])
        self.assertEqual((results["gone"]["status"], results["gone"]["absence"]), ("pass", "absent-corpus"))
        self.assertEqual(results["partial"]["reasons"], ["absence-mismatch"])
        self.assertEqual(results["present"]["reasons"], ["unexpected-hits"])
        self.assertEqual(results["excluded"]["status"], "pass")
        self.assertEqual(results["broken"]["reasons"], ["error:IndexError_"])
        totals = card["aggregates"]
        self.assertEqual((totals["cases"], totals["passed"], totals["errors"]), (10, 4, 1))
        self.assertEqual(totals["known_item_cases"], 5)
        self.assertEqual((totals["hit_at_1"], totals["hit_at_k"]), (0.6, 0.6))
        self.assertEqual(totals["mrr"], 0.6)
        self.assertEqual(totals["must_not_violations"], 1)
        self.assertEqual(totals["absence_accuracy"], 0.5)
        self.assertEqual(totals["confidence_match_rate"], 0.5)
        self.assertEqual(totals["expected_hit_confidence"], {"normal": 2, "low": 1})
        provenance = card["provenance"]
        with closing(sqlite3.connect(self.db)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        for key in ("engine_fingerprint", "manifest_hash", "policy_hash", "rendition_identity"):
            self.assertEqual(provenance["index"][key], metadata[key])
        self.assertEqual(provenance["cases"]["count"], 10)
        self.assertEqual(provenance["standing_policy"], {"present": False})
        self.assertIn("origin:authored", card["groups"])

    def test_scorecard_is_body_free_and_private(self):
        self.write_cases([case(id="hit", query="saffron cardamom", expect_paths=["documents/kitchen.md"])])
        code, out, err = self.main("run")
        self.assertEqual(code, 0, err)
        path = Path(json.loads(out)["scorecard"])
        self.assertTrue(path.is_relative_to(self.root / runner.RUNS))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "tools/evals/.local").stat().st_mode), 0o700)
        raw = path.read_text()
        for leaked in ("saffron", "cardamom", "kitchen", "pantry", "documents/"):
            self.assertNotIn(leaked, raw)
        self.assertEqual(json.loads(raw)["aggregates"]["passed"], 1)

    def test_standing_policy_identity_is_recorded_without_its_scope(self):
        (self.root / runner.STANDING_POLICY).write_text(json.dumps(
            {"schema": "standing-policy-v1", "policy_id": "cohort", "version": 3, "status": "active",
             "scope": {"receipts": ["D*"]}}))
        card = self.scorecard([case(id="hit", query="saffron", expect_paths=["documents/kitchen.md"])])
        recorded = card["provenance"]["standing_policy"]
        self.assertEqual((recorded["policy_id"], recorded["version"]), ("cohort", 3))
        self.assertNotIn("scope", recorded)

    def test_stale_index_is_refused_without_a_scorecard(self):
        self.write_cases([case(id="hit", query="saffron", expect_paths=["documents/kitchen.md"])])
        (self.root / "documents/kitchen.md").write_text("# Kitchen\n\nChanged.\n")
        code, _, err = self.main("run")
        self.assertEqual(code, 2)
        self.assertIn("Index is stale", err)
        self.assertFalse((self.root / "tools/evals").exists())

    def test_index_going_stale_during_the_run_is_refused(self):
        with patch.object(runner, "freshness", side_effect=[[], ["source changed: x"]]) as check, \
                self.assertRaisesRegex(EvalError, "became stale"):
            self.scorecard([case(id="hit", query="saffron", expect_paths=["documents/kitchen.md"])])
        self.assertEqual(check.call_count, 2)

    def test_a_correction_recorded_during_the_run_is_refused(self):
        from tools.review_events import corrections, event_store
        self.registration()
        event_store.initialize(self.root)
        corrections.initialize(self.root)
        self.build()
        search = runner.search

        def correcting(*args, **kwargs):
            corrections.append(self.root, [{
                "kind": "dispute", "locator": "documents/kitchen.md#kitchen-inventory",
                "claim": "Saffron cardamom",
                "summary": "Disputed.", "source": {"type": "question", "question": "Is it saffron?",
                                                   "answer": "No.", "answered_by": "cook",
                                                   "answered_on": "2024-05-01"}}],
                "test-runner", "req-1", corrections.status(self.root)["state_token"])
            return search(*args, **kwargs)
        with patch.object(runner, "search", side_effect=correcting), \
                self.assertRaisesRegex(EvalError, "Claim corrections changed"):
            self.scorecard([case(id="hit", query="saffron", expect_paths=["documents/kitchen.md"])])

    def test_freshness_is_checked_once_before_and_once_after_the_batch(self):
        cases = [case(id=f"c{n}", query="saffron", expect_paths=["documents/kitchen.md"]) for n in range(5)]
        with patch.object(runner, "freshness", wraps=runner.freshness) as check:
            self.scorecard(cases)
        self.assertEqual(check.call_count, 2)


class CompareTests(EvalWorkspace):
    def cards(self):
        base = self.scorecard([
            case(id="a", query="saffron", expect_paths=["documents/kitchen.md"]),
            case(id="b", query="tomato", expect_paths=["documents/garden.md"]),
            case(id="c", query="zzqqxx", expect_absent=True),
        ])
        new = copy.deepcopy(base)
        new["run_id"] = "new"
        return base, new

    def files(self, base, new):
        paths = []
        for name, card in (("base.json", base), ("new.json", new)):
            path = self.root / name
            path.write_text(json.dumps(card))
            paths.append(str(path))
        return paths

    def test_malformed_scorecards_are_refused(self):
        base, new = self.cards()
        hollow = {"schema": runner.SCHEMA, "aggregates": {}, "cases": [], "provenance": {}}
        twice = copy.deepcopy(new)
        twice["cases"].append(copy.deepcopy(twice["cases"][0]))
        for card, message in ((hollow, "aggregates.pass_rate"), (twice, "unique id")):
            with self.subTest(message=message):
                code, _, err = self.main("compare", *self.files(base, card))
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_identical_scorecards_pass(self):
        base, new = self.cards()
        code, out, _ = self.main("compare", *self.files(base, new))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK"))

    def test_regressions_and_metric_drops_exit_nonzero(self):
        base, new = self.cards()
        regressed = next(item for item in new["cases"] if item["id"] == "b")
        regressed.update(status="fail", rank=None, found=0, reasons=["expected-not-in-top-k"])
        new["aggregates"].update(passed=2, pass_rate=0.666667, hit_at_1=0.5, hit_at_k=0.5,
                                 recall_at_k=0.5, mrr=0.5)
        new["provenance"]["standing_policy"] = {"present": True, "sha256": "x", "version": 4}
        code, out, _ = self.main("compare", *self.files(base, new), "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertFalse(report["ok"])
        self.assertEqual([item["id"] for item in report["regressions"]], ["b"])
        self.assertIn("metric:hit_at_1", report["failures"])
        self.assertIn("regressions", report["failures"])
        self.assertIn("standing_policy.sha256", report["provenance_changed"])
        code, out, _ = self.main("compare", *self.files(base, new), "--max-regressions", "1",
                                 "--tolerance", "hit_at_1=0.5", "--tolerance", "hit_at_k=0.5",
                                 "--tolerance", "recall_at_k=0.5", "--tolerance", "mrr=0.5",
                                 "--tolerance", "pass_rate=0.5")
        self.assertEqual(code, 0, out)
        self.assertIn("- b: pass -> fail", out)

    def test_improvements_and_must_not_increase(self):
        base, new = self.cards()
        new["aggregates"]["must_not_violations"] = 1
        report = compare.compare(new, base)
        self.assertTrue(report["ok"])
        report = compare.compare(base, new)
        self.assertEqual(report["failures"], ["metric:must_not_violations"])
        base["cases"][0].update(status="fail")
        self.assertEqual([item["id"] for item in compare.compare(base, new)["improvements"]], ["a"])

    def test_rejects_unknown_tolerances_and_non_scorecards(self):
        with self.assertRaisesRegex(EvalError, "--tolerance takes"):
            compare.tolerances(["bogus=1"])
        path = self.root / "other.json"
        path.write_text(json.dumps({"schema": "something-else"}))
        code, _, err = self.main("compare", str(path), str(path))
        self.assertEqual(code, 2)
        self.assertIn("Not a retrieval-eval-scorecard-v1", err)


if __name__ == "__main__":
    unittest.main()
