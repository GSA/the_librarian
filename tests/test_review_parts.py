"""Reviewing a rendition too large for one prompt in parts, and reducing inline data for the reviewer."""
import hashlib
import json
import unittest

from tools.extraction import extract
from tools.catalog import catalog_store as catalog
from tools.intake import model_review, standing, store
from tools.review_events import event_store as events

from tests.support import WorkspaceCase

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


if __name__ == "__main__":
    unittest.main()
