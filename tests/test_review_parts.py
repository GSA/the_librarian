"""Reviewing a rendition too large for one prompt in parts, and reducing inline data for the reviewer."""
import hashlib
import json
import unittest

from contextlib import closing

from tools.context_index import context_index as index
from tools.context_index import packets
from tools.extraction import extract
from tools.catalog import catalog_store as catalog
from tools.intake import model_review, standing, store
from tools.review_events import event_store as events

from tests.support import WorkspaceCase
from tests.test_intake_adapters import stage, stage_request
from tools.intake import runner

CAP = 7000  # a small prompt cap so a short synthetic note is "too large"


def uri(size, mime="image/png"):
    return f"data:{mime};base64," + "QUJD" * (size // 4)


def lines(count, width=90):
    return "".join(f"Row {n:04d} " + "x" * width + "\n" for n in range(count)).encode()


def reply_for(messages, decision="accept", skip=(), **extra):
    """A reviewer that inspects every record and warning it is shown (except ``skip`` indices)."""
    body = json.loads(messages[-1]["content"])
    return {"content": json.dumps({
        "decision": decision, "explanation": "Checked what was shown.",
        "inspected_records": [r["i"] for r in body["records"] if r["i"] not in skip],
        "inspected_warnings": [w["ref"] for w in body["warnings"]],
        "contradictions": [], "missing_evidence": [], **extra}),
        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


class Transport:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, model, messages):
        self.calls.append(messages)
        step = self.script.pop(0) if self.script else {}
        if isinstance(step, Exception):
            raise step
        return reply_for(messages, **step)


class PartsCase(WorkspaceCase):
    SOURCE = lines(120)

    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        self.registration("sources/notes.md", self.SOURCE)
        prepared = extract.run(self.root, "prepare", "D1", "text")
        extract.run(self.root, "register", "D1", "text", "test-runner",
                    catalog.status(self.root)["state_token"])
        self.rendition, self.rendition_sha = prepared["rendition"], prepared["rendition_sha256"]
        events.initialize(self.root)
        self.docs()
        self.policy = {"schema": standing.POLICY_SCHEMA, "policy_id": "routine-text",
                       "version": 1, "status": "active", "reviewer": "usai:model-a",
                       "scope": {"receipts": ["D1"], "audiences": ["source"]},
                       "budget": {"max_pages": 100, "max_records": 10000,
                                  "max_findings_bytes": 64 * 1024},
                       "selection": {"context": ["documents/AGENTS.md"]}}

    def plan(self, **extra):
        return model_review.plan(self.root, self.rendition, max_prompt_chars=CAP, **extra)

    def review(self, transport, **extra):
        return model_review.review(self.root, self.rendition, self.policy, "model-a", transport,
                                   max_prompt_chars=CAP, **extra)

    def findings(self, result):
        return json.loads((self.root / result["findings_path"]).read_text())

    def evaluate(self, result):
        raw = json.dumps(self.policy).encode()
        path = self.root / "config/standing-policy.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        return standing.evaluate(self.root, {
            "policy_path": "config/standing-policy.json", "policy_sha256": hashlib.sha256(raw).hexdigest(),
            "rendition": self.rendition, "rendition_sha256": self.rendition_sha,
            "findings_path": result["findings_path"], "findings_sha256": result["findings_sha256"],
            "event_id": "rev-x"})


class PlanTests(PartsCase):
    def test_a_rendition_that_fits_is_one_unchanged_prompt(self):
        planned = model_review.plan(self.root, self.rendition)
        self.assertEqual((planned["status"], len(planned["tasks"])), (None, 1))
        self.assertEqual(planned["tasks"][0]["messages"], model_review.task(self.root, self.rendition)["messages"])
        self.assertNotIn("part 1 of", planned["tasks"][0]["messages"][0]["content"])

    def test_an_oversized_rendition_is_cut_into_contiguous_parts_that_each_fit(self):
        planned = self.plan()
        tasks = planned["tasks"]
        self.assertIsNone(planned["status"])
        self.assertGreater(len(tasks), 2)
        covered = [i for t in tasks for i in t["expected"]["records"]]
        self.assertEqual(covered, list(range(120)))  # every record once, in order
        for number, t in enumerate(tasks, 1):
            with self.subTest(part=number):
                self.assertLessEqual(sum(len(m["content"]) for m in t["messages"]), CAP)
                self.assertIn(f"part {number} of {len(tasks)}", t["messages"][0]["content"])
        self.assertEqual(planned["chars"], sum(len(m["content"]) for t in tasks for m in t["messages"]))

    def test_every_warning_belongs_to_the_first_part_only(self):
        tasks = self.plan()["tasks"]
        self.assertEqual(tasks[0]["expected"]["warnings"], ["#/payload/warnings/0"])
        self.assertTrue(all(t["expected"]["warnings"] == [] for t in tasks[1:]))

    def test_a_record_count_over_the_policy_budget_is_split_by_record_count(self):
        planned = model_review.plan(self.root, self.rendition, max_records=50)
        self.assertEqual([len(t["expected"]["records"]) for t in planned["tasks"]], [50, 50, 20])

    def test_too_many_parts_is_over_budget(self):
        planned = model_review.plan(self.root, self.rendition, max_records=1)
        self.assertEqual((planned["status"], planned["tasks"]), ("over-budget", []))


class PlanCommandTests(PartsCase):
    def run_plan(self, *extra):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = model_review.main(["plan", "--root", str(self.root), "--rendition", self.rendition,
                                      "--max-prompt-chars", str(CAP), "--criteria", "standing-review-v1", *extra])
        return code, json.loads(out.getvalue())

    def test_the_read_only_plan_reports_parts_calls_and_prompt_size_without_a_body(self):
        code, report = self.run_plan()
        planned = self.plan()
        self.assertEqual(code, 0)
        self.assertEqual(report["totals"], {"renditions": 1, "model_calls": len(planned["tasks"]),
                                            "prompt_chars": planned["chars"],
                                            "prompt_tokens_approx": -(-planned["chars"] // 4),
                                            "by_status": {"parts": 1}})
        row = report["renditions"][0]
        self.assertEqual((row["receipt_id"], row["parts"], row["status"]), ("D1", len(planned["tasks"]), "parts"))
        self.assertNotIn("Row 0000", json.dumps(report))

    def test_the_plan_names_what_stays_too_large_or_over_budget(self):
        _, report = self.run_plan("--max-records", "1")
        self.assertEqual(report["totals"]["by_status"], {"over-budget": 1})
        self.assertEqual(report["totals"]["model_calls"], 0)

    def test_the_plan_writes_nothing(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        self.run_plan()
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")), before)


class BlankPagesTests(PartsCase):
    """A PDF whose page inventory exceeds the page budget but whose text sits on fewer pages."""

    def setUp(self):
        super().setUp()
        path = self.root / self.rendition
        value = json.loads(path.read_text())
        payload = value["payload"]
        payload["format"] = "pdf-text"
        payload["lines"] = [{"line": n + 1, "page": 1, "text": line["text"]}
                            for n, line in enumerate(payload["lines"][:3])]
        payload["pages"] = [{"page": n} for n in (1, 2, 3)]
        path.write_text(json.dumps(value))

    def test_a_single_part_cut_for_the_page_budget_is_recorded_as_a_part_so_it_can_qualify(self):
        planned = model_review.plan(self.root, self.rendition, max_pages=1)
        self.assertEqual((len(planned["tasks"]), planned["parted"]), (1, True))
        transport = Transport()
        self.policy["budget"]["max_pages"] = 1
        result = model_review.review(self.root, self.rendition, self.policy, "model-a", transport)
        findings = self.findings(result)
        self.assertEqual(len(findings["parts"]), 1)
        # The same part check ``standing.evaluate`` applies: one part of three inventory pages, budget one.
        value = json.loads((self.root / self.rendition).read_text())
        expected = {"records": list(range(3)), "warnings": ["#/payload/warnings/0"]}
        self.assertIsNone(standing._parts_failure(findings, value, expected, self.policy["budget"]))
        self.assertEqual(findings["inspected"]["records"], ["0-2"])


class PartBoundaryTests(PartsCase):
    SOURCE = b"short line\n" + b"y" * 6000 + b"\nanother short line\n"

    def test_a_record_is_too_large_only_when_its_own_part_prompt_exceeds_the_cap(self):
        def status(cap):
            return model_review.plan(self.root, self.rendition, max_prompt_chars=cap)["status"]
        lowest = next(cap for cap in range(6000, 20000) if status(cap) is None)
        tasks = model_review.plan(self.root, self.rendition, max_prompt_chars=lowest)["tasks"]
        real = max(sum(len(m["content"]) for m in t["messages"]) for t in tasks)
        self.assertLessEqual(real, lowest)
        # The reserve for the part note is its widest form, a few digits, not a fixed block.
        self.assertLessEqual(lowest - real, 12)
        self.assertEqual(status(lowest - 1), "too-large")


class ReviewPartsTests(PartsCase):
    def test_parts_combine_into_one_findings_record_that_qualifies(self):
        transport = Transport()
        result = self.review(transport)
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(len(transport.calls), len(self.plan()["tasks"]))
        self.assertEqual(result["usage"], {"prompt_tokens": 100 * len(transport.calls),
                                           "completion_tokens": 10 * len(transport.calls)})
        findings = self.findings(result)
        self.assertEqual(findings["decision"], "accept")
        self.assertNotIn("reductions", findings)
        self.assertEqual(findings["inspected"]["records"], ["0-119"])
        self.assertEqual(findings["parts"][0]["first"], 0)
        self.assertEqual(findings["parts"][-1]["last"], 119)
        outcome = self.evaluate(result)
        self.assertEqual((outcome["outcome"], outcome["inspected"]), ("qualified", {"records": 120, "warnings": 1}))
        self.assertEqual(len(outcome["parts"]), len(transport.calls))

    def test_the_strictest_part_decision_is_the_document_decision(self):
        n = len(self.plan()["tasks"])
        for script, expected in (({0: "defer"}, "defer"), ({0: "defer", 1: "reject"}, "reject")):
            with self.subTest(script=script):
                steps = [{"decision": script.get(k, "accept")} for k in range(n)]
                result = self.review(Transport(*steps))
                findings = self.findings(result)
                self.assertEqual(findings["decision"], expected)
                self.assertEqual(findings["parts"][0]["decision"], script.get(0, "accept"))

    def test_a_record_a_part_left_uninspected_stays_unfilled_and_pending(self):
        steps = [{}, {"skip": {self.plan()["tasks"][1]["expected"]["records"][0]}}]
        result = self.review(Transport(*steps))
        findings = self.findings(result)
        self.assertNotIn(self.plan()["tasks"][1]["expected"]["records"][0],
                         standing.expand_records(findings["inspected"]["records"], 1000))
        self.assertEqual(self.evaluate(result)["reasons"], ["insufficient-inspected-scope"])

    def test_issues_from_every_part_are_kept_and_say_which_part_noted_them(self):
        n = len(self.plan()["tasks"])
        steps = [{"contradictions": [f"c{k}"]} if k in (0, 2) else {} for k in range(n)]
        findings = self.findings(self.review(Transport(*steps)))
        self.assertEqual(findings["contradictions"], [f"[part 1/{n}] c0", f"[part 3/{n}] c2"])

    def test_sparse_only_when_every_part_is_sparse(self):
        self.policy.update(schema=standing.POLICY_SCHEMA_V2, criteria="standing-review-v2", carries=[])
        n = len(self.plan(criteria="standing-review-v2")["tasks"])
        mixed = self.findings(self.review(Transport(*[{"sparse": k == 0} for k in range(n)])))
        every = self.findings(self.review(Transport(*[{"sparse": True} for _ in range(n)])))
        self.assertEqual((mixed["sparse"], every["sparse"]), (False, True))

    def test_a_failed_part_ends_the_review_without_findings_and_counts_the_spent_calls(self):
        result = self.review(Transport({}, {}, model_review.TransportError("http-500")))
        self.assertEqual((result["status"], result["usage"]["prompt_tokens"]), ("transport-error", 200))
        self.assertNotIn("findings_path", result)

    def test_over_budget_makes_no_model_call(self):
        transport = Transport()
        self.policy["budget"]["max_records"] = 1
        self.assertEqual(self.review(transport)["status"], "over-budget")
        self.assertEqual(transport.calls, [])

    def test_findings_that_outgrow_the_policy_budget_are_over_budget(self):
        self.policy["budget"]["max_findings_bytes"] = 1024
        n = len(self.plan()["tasks"])
        result = self.review(Transport(*[{"missing_evidence": ["y" * 900]} for _ in range(n)]))
        self.assertEqual(result["status"], "over-budget")


class SingleRecordTooLargeTests(PartsCase):
    SOURCE = b"short line\n" + b"x" * (2 * CAP) + b"\nanother short line\n"

    def test_a_record_bigger_than_a_prompt_is_too_large_and_never_cut_or_sent(self):
        self.assertEqual((self.plan()["status"], self.plan()["tasks"]), ("too-large", []))
        transport = Transport()
        result = self.review(transport)
        self.assertEqual((result["status"], transport.calls), ("too-large", []))

    def test_it_stays_too_large_when_the_cap_is_raised_only_slightly(self):
        self.assertEqual(model_review.plan(self.root, self.rendition, max_prompt_chars=CAP + 500)["status"],
                         "too-large")
        self.assertIsNone(model_review.plan(self.root, self.rendition, max_prompt_chars=4 * CAP)["status"])


class InlineDataTests(PartsCase):
    SOURCE = b"# Notes\n\nDecision recorded.\n\n![shot](" + uri(40000).encode() + b")\n\nNext steps.\n"

    def test_the_prompt_sees_a_placeholder_and_the_original_is_untouched(self):
        planned = model_review.plan(self.root, self.rendition)
        prompt = planned["tasks"][0]["messages"][-1]["content"]
        self.assertNotIn("QUJDQUJD", prompt)
        self.assertIn("[inline data omitted: image/png, 40000 bytes, sha256 ", prompt)
        self.assertIn("Decision recorded.", prompt)
        self.assertIn("[inline data omitted", planned["tasks"][0]["messages"][0]["content"])
        self.assertEqual(hashlib.sha256((self.root / self.rendition).read_bytes()).hexdigest(),
                         self.rendition_sha)
        self.assertIn("QUJDQUJD", (self.root / self.rendition).read_text())

    def test_a_note_whose_size_is_all_image_is_reviewed_in_one_call_and_records_the_reduction(self):
        transport = Transport()
        result = model_review.review(self.root, self.rendition, self.policy, "model-a", transport,
                                     max_prompt_chars=CAP)
        self.assertEqual((result["status"], len(transport.calls)), ("reviewed", 1))
        findings = self.findings(result)
        self.assertNotIn("parts", findings)
        self.assertEqual((findings["reductions"]["count"], findings["reductions"]["bytes"]), (1, 40000))
        image = findings["reductions"]["images"][0]
        self.assertEqual((image["mime"], image["bytes"]), ("image/png", 40000))
        self.assertEqual(self.evaluate(result)["reductions"], {"count": 1, "bytes": 40000})


DB = "tools/context_index/.local/index.sqlite3"


class PublishedTests(PartsCase):
    """What an agent sees after a part-reviewed or image-reduced rendition is published."""

    SOURCE = (b"# Transition notes\n\nThe cutover decision was recorded here.\n\n![shot](" + uri(40000).encode()
              + b")\n\nNext steps follow.\n" + b"".join(b"Filler line %03d with some words\n" % n for n in range(240)))

    def publish(self, transport, cap=CAP):
        result = model_review.review(self.root, self.rendition, self.policy, "model-a", transport,
                                     max_prompt_chars=cap)
        self.assertEqual(result["status"], "reviewed")
        raw = json.dumps(self.policy).encode()
        (self.root / "config/standing-policy.json").write_bytes(raw)
        (self.root / "config/standing-policy.json").chmod(0o600)
        args = {"policy_path": "config/standing-policy.json", "policy_sha256": hashlib.sha256(raw).hexdigest(),
                "rendition": self.rendition, "rendition_sha256": self.rendition_sha,
                "findings_path": result["findings_path"], "findings_sha256": result["findings_sha256"],
                "event_id": "rev-parts"}
        stage_request(self.root, [
            stage("review", "standing-review", {**args, "request_id": "req-r"}, "review"),
            stage("publication", "standing-select", {**args, "request_id": "req-s",
                                                     "manifest": "config/selection.json"}, "select"),
            stage("publication", "standing-publish", {"policy_path": args["policy_path"],
                  "policy_sha256": args["policy_sha256"], "manifest": "config/selection.json", "db": DB},
                  "publish")], "req-parts")
        for name in ("review", "select", "publish"):
            done = runner.run(self.root, "req-parts", name)
            self.assertEqual(next(x for x in done["stages"] if x["id"] == name)["status"], "succeeded", name)
        self.config = json.loads((self.root / "config/selection.json").read_text())
        return result

    def read(self, term):
        with closing(index.readonly(self.root / DB)) as db:
            hits = index.search(db, term, 10, None, root=self.root)["hits"]
            packet = packets.packet(db, self.config, hits[0]["locator"], 2_000_000, root=self.root) if hits else None
        return hits, packet

    def test_the_review_event_and_published_scope_show_each_parts_inspection(self):
        n = len(model_review.plan(self.root, self.rendition, max_prompt_chars=CAP)["tasks"])
        self.assertGreater(n, 1)
        self.publish(Transport(*[{} for _ in range(n)]))
        hits, packet = self.read("cutover")
        scope = packet["renditions"][self.rendition]["qualification"]["reviewed_scope"][0]
        self.assertEqual(len(scope["parts"]), 1)
        statement = scope["parts"][0]
        self.assertIn(f"{n} parts", statement)
        for number in range(1, n + 1):
            self.assertIn(f"part {number}: records ", statement)
        self.assertIn("inline data", scope["reductions"][0].lower())
        self.assertIn("1 image", scope["reductions"][0])

    def test_the_search_text_is_reduced_and_the_label_and_metadata_say_images_were_omitted(self):
        self.publish(Transport(), cap=400_000)
        self.assertEqual(self.read("QUJDQUJD")[0], [])
        hits, packet = self.read("cutover")
        self.assertEqual(hits[0]["confidence"], {"level": "low", "reasons": ["inline-data-reduced"]})
        info = packet["renditions"][self.rendition]["inline_data"]
        self.assertEqual((info["count"], info["bytes"]), (1, 40000))
        self.assertEqual(info["images"][0]["mime"], "image/png")
        self.assertIn("original", packet["confidence"]["reasons"]["inline-data-reduced"]["correction"])
        # The cited text stays the exact extraction lines: the original bytes are not edited.
        image_hits = self.read("shot")[1]
        self.assertTrue(any("QUJDQUJD" in piece["text"] for piece in image_hits["pieces"]))


    def test_an_authored_eval_case_reaches_the_part_reviewed_image_note_by_search(self):
        from tools.evals import cases as case_file, runner as eval_runner
        n = len(model_review.plan(self.root, self.rendition, max_prompt_chars=CAP)["tasks"])
        self.publish(Transport(*[{} for _ in range(n)]))
        case = {"id": "cutover-decision", "origin": "authored", "query": "cutover decision recorded", "k": 5,
                "expect_paths": [self.rendition], "expect_confidence": "low"}
        document = {"schema": case_file.SCHEMA, "cases": [case]}
        case_file.validate(document)
        card = eval_runner.run(self.root, document, "0" * 64)
        self.assertEqual(card["aggregates"]["cases"], 1)
        self.assertEqual(card["cases"][0]["status"], "pass", card["cases"][0])


class PlainPublishedTests(PublishedTests):
    test_the_review_event_and_published_scope_show_each_parts_inspection = None
    test_the_search_text_is_reduced_and_the_label_and_metadata_say_images_were_omitted = None
    test_an_authored_eval_case_reaches_the_part_reviewed_image_note_by_search = None

    SOURCE = b"# Plain notes\n\nThe cutover decision was recorded here.\n"

    def test_a_rendition_without_inline_data_is_normal_and_has_no_inline_metadata(self):
        self.publish(Transport(), cap=400_000)
        hits, packet = self.read("cutover")
        self.assertEqual(hits[0]["confidence"], {"level": "normal", "reasons": []})
        self.assertNotIn("inline_data", packet["renditions"][self.rendition])
        scope = packet["renditions"][self.rendition]["qualification"]["reviewed_scope"][0]
        self.assertNotIn("parts", scope)
        self.assertNotIn("reductions", scope)


if __name__ == "__main__":
    unittest.main()
