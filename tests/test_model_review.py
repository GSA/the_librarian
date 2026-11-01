"""Model-assisted standing review: prompts, findings, sample evaluation and cost estimate."""
import json
from pathlib import Path
import stat
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.extraction import extract
from tools.intake import model_review, standing, store
from tools.review_events import event_store as events

import test_kit


def reply(decision="accept", records=(0, 1), warnings=("#/payload/warnings/0",), **extra):
    body = {"decision": decision, "inspected_records": list(records),
            "inspected_warnings": list(warnings), "explanation": "Checked every record.",
            "contradictions": [], "missing_evidence": [], **extra}
    return {"content": json.dumps(body), "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class Transport:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, model, messages):
        self.calls.append((model, messages))
        value = self.replies.pop(0) if self.replies else reply()
        if isinstance(value, Exception):
            raise value
        return value


class ReviewCase(test_kit.WorkspaceCase):
    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        self.registration("sources/notes.md", b"# Distinct heading phrase\nSecond line\n")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        extract.run(self.root, "register", "D1", "text", "test-runner",
                    catalog.status(self.root)["state_token"])
        self.rendition, self.rendition_sha = prepared["rendition"], prepared["rendition_sha256"]
        events.initialize(self.root)
        self.docs()
        self.policy = {"schema": standing.POLICY_SCHEMA, "policy_id": "routine-text",
                       "version": 1, "status": "active", "reviewer": "usai:model-a",
                       "scope": {"receipts": ["D1"], "audiences": ["source"]},
                       "budget": {"max_pages": 1, "max_records": 10,
                                  "max_findings_bytes": 64 * 1024},
                       "selection": {"context": ["documents/AGENTS.md"]}}


class PromptTests(ReviewCase):
    def test_prompt_numbers_every_record_and_warning(self):
        task = model_review.task(self.root, self.rendition)
        self.assertEqual(task["expected"], {"records": [0, 1], "warnings": ["#/payload/warnings/0"]})
        user = json.loads(task["messages"][-1]["content"])
        self.assertEqual([r["i"] for r in user["records"]], [0, 1])
        self.assertIn("Distinct heading phrase", user["records"][0]["text"])
        self.assertEqual([w["ref"] for w in user["warnings"]], ["#/payload/warnings/0"])
        self.assertIn("untrusted", task["messages"][0]["content"].lower())


class FindingsTests(ReviewCase):
    def review(self, transport):
        return model_review.review(self.root, self.rendition, self.policy, "model-a", transport)

    def test_accepted_reply_becomes_findings_that_qualify(self):
        result = self.review(Transport(reply()))
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(result["usage"], {"prompt_tokens": 100, "completion_tokens": 20})
        path = self.root / result["findings_path"]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        findings = json.loads(path.read_text())
        self.assertEqual(findings["reviewer"], "usai:model-a")
        self.assertEqual(findings["inspected"], {"records": [0, 1],
                                                 "warnings": ["#/payload/warnings/0"]})
        policy_sha = self.save_policy()
        outcome = standing.evaluate(self.root, {
            "policy_path": "config/standing-policy.json", "policy_sha256": policy_sha,
            "rendition": self.rendition, "rendition_sha256": self.rendition_sha,
            "findings_path": result["findings_path"], "findings_sha256": result["findings_sha256"],
            "event_id": "rev-x"})
        self.assertEqual(outcome["outcome"], "qualified")

    def save_policy(self):
        import hashlib
        raw = json.dumps(self.policy).encode()
        path = self.root / "config/standing-policy.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def test_partial_inspection_is_recorded_as_reported_not_filled_in(self):
        result = self.review(Transport(reply(records=[0])))
        findings = json.loads((self.root / result["findings_path"]).read_text())
        self.assertEqual(findings["inspected"]["records"], [0])

    def test_unparseable_or_out_of_range_replies_are_invalid_without_findings(self):
        for value in ({"content": "not json", "usage": {}},
                      reply(records=[0, 7]),
                      reply(decision="maybe"),
                      reply(warnings=["#/payload/warnings/9"])):
            with self.subTest(value=value["content"][:20]):
                result = self.review(Transport(value))
                self.assertEqual(result["status"], "invalid-reply")
                self.assertNotIn("findings_path", result)

    def test_code_fenced_json_is_accepted(self):
        value = reply()
        value["content"] = "```json\n" + value["content"] + "\n```"
        self.assertEqual(self.review(Transport(value))["status"], "reviewed")

    def test_transport_failure_and_oversize_are_counted_without_findings(self):
        self.assertEqual(self.review(Transport(model_review.TransportError("http-500")))["status"],
                         "transport-error")
        transport = Transport()
        result = model_review.review(self.root, self.rendition, self.policy, "model-a",
                                     transport, max_prompt_chars=10)
        self.assertEqual(result["status"], "too-large")
        self.assertEqual(transport.calls, [])

    def test_reviewer_must_name_the_model(self):
        self.policy["reviewer"] = "usai:other"
        with self.assertRaises(ValueError):
            self.review(Transport())


class EvaluationTests(ReviewCase):
    def test_sample_scores_are_body_free_and_choose_the_best_cheapest_model(self):
        transports = {"model-a": [reply(), reply()],
                      "model-b": [reply(records=[0]), reply()],
                      "model-c": [{"content": "nope", "usage": {}}, reply()]}

        def transport(model, messages):
            value = transports[model].pop(0)
            if model == "model-a":
                value["usage"] = {"prompt_tokens": 300, "completion_tokens": 50}
            return value
        report = model_review.evaluate_models(
            self.root, [self.rendition, self.rendition], ["model-a", "model-b", "model-c"],
            transport, clock=iter(range(100)).__next__)
        scores = report["models"]
        self.assertEqual(scores["model-a"]["complete"], 2)
        self.assertEqual(scores["model-b"]["complete"], 1)
        self.assertEqual(scores["model-c"]["invalid-reply"], 1)
        self.assertEqual(scores["model-a"]["usage"],
                         {"prompt_tokens": 600, "completion_tokens": 100})
        self.assertEqual(report["selected"], "model-a")
        raw = json.dumps(report)
        self.assertNotIn("Distinct heading phrase", raw)
        self.assertNotIn("Checked every record", raw)

    def test_control_task_must_not_be_accepted(self):
        def transport(model, messages):
            control = "CONTROL" in messages[-1]["content"]
            return reply(decision="accept" if model == "model-a" or not control else "reject",
                         records=range(len(json.loads(messages[-1]["content"])["records"])),
                         warnings=[w["ref"] for w in json.loads(messages[-1]["content"])["warnings"]])
        report = model_review.evaluate_models(self.root, [self.rendition], ["model-a", "model-b"],
                                              transport, controls=True)
        self.assertEqual(report["models"]["model-a"]["control-caught"], 0)
        self.assertEqual(report["models"]["model-b"]["control-caught"], 1)
        self.assertEqual(report["selected"], "model-b")


class EstimateTests(ReviewCase):
    def test_estimate_extrapolates_sample_tokens_per_document(self):
        estimate = model_review.estimate(
            {"documents": 4, "usage": {"prompt_tokens": 4000, "completion_tokens": 400}},
            admitted=1000, sample_bytes=40000, admitted_bytes=20_000_000)
        self.assertEqual(estimate["per_document"], {"prompt_tokens": 1000, "completion_tokens": 100})
        self.assertEqual(estimate["by_count"], {"prompt_tokens": 1_000_000,
                                                "completion_tokens": 100_000})
        self.assertEqual(estimate["by_bytes"]["prompt_tokens"], 2_000_000)


class TransportTests(ReviewCase):
    def test_usai_transport_refuses_unconfigured_models_and_providers(self):
        models = Path(self.directory.name) / "models.json"
        models.write_text(json.dumps({"providers": {
            "usai": {"baseUrl": "https://api.example.invalid/v1", "api": "openai-completions",
                     "apiKey": "k", "models": [{"id": "model-a"}]},
            "other": {"baseUrl": "https://elsewhere", "apiKey": "k", "models": [{"id": "model-b"}]}}}))
        transport = model_review.usai_transport(models)
        with self.assertRaises(model_review.TransportError):
            transport("model-b", [])
        sent = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}],
                                   "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()

        def urlopen(request, timeout):
            sent.update(url=request.full_url, auth=request.get_header("Authorization"),
                        body=json.loads(request.data))
            return Response()
        with patch.object(model_review.urllib.request, "urlopen", urlopen):
            value = transport("model-a", [{"role": "user", "content": "x"}])
        self.assertEqual(sent["url"], "https://api.example.invalid/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer k")
        self.assertEqual(sent["body"]["model"], "model-a")
        self.assertEqual(value, {"content": "{}", "usage": {"prompt_tokens": 3,
                                                            "completion_tokens": 1}})


if __name__ == "__main__":
    import unittest
    unittest.main()
