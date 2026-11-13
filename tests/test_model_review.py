"""Model-assisted standing review: prompts, findings, sample evaluation and cost estimate."""
import json
import os
import unittest
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


class OdtPromptTests(test_kit.WorkspaceCase):
    def test_odt_prompt_carries_every_node_field(self):
        import io
        from zipfile import ZipFile
        office, text = extract.NS["office"], extract.NS["text"]
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            z.writestr("content.xml", f'<office:document-content xmlns:office="{office}" '
                       f'xmlns:text="{text}" xmlns:xlink="http://www.w3.org/1999/xlink">'
                       '<office:body><office:text><text:p>See <text:a xlink:href='
                       '"https://example.invalid/target">label</text:a> after</text:p>'
                       '</office:text></office:body></office:document-content>')
            z.writestr("meta.xml", f'<office:document-meta xmlns:office="{office}">'
                       '<office:meta/></office:document-meta>')
            z.writestr("META-INF/manifest.xml", "<manifest/>")
        store.initialize(self.root)
        self.registration("sources/plan.odt", buffer.getvalue())
        prepared = extract.run(self.root, "prepare", "D1", "odt")
        body = model_review.task(self.root, prepared["rendition"])["messages"][-1]["content"]
        self.assertIn("https://example.invalid/target", body)
        records = json.loads(body)["records"]
        self.assertTrue(all({"member", "locator", "tag", "attributes", "text", "tail"} <= set(r)
                            for r in records))


class FindingsTests(ReviewCase):
    def review(self, transport):
        return model_review.review(self.root, self.rendition, self.policy, "model-a", transport)

    def test_accepted_reply_becomes_findings_that_qualify(self):
        cited = "Cites a repository README that this rendition does not contain."
        result = self.review(Transport(reply(unverifiable_references=[cited])))
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(result["usage"], {"prompt_tokens": 100, "completion_tokens": 20})
        path = self.root / result["findings_path"]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        findings = json.loads(path.read_text())
        self.assertEqual(findings["reviewer"], "usai:model-a")
        self.assertEqual(findings["schema"], "standing-findings-v2")
        self.assertEqual(findings["unverifiable_references"], [cited])
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
                      reply(warnings=["#/payload/warnings/9"]),
                      reply(decision=["accept"]), reply(decision={"a": 1})):
            with self.subTest(value=value["content"][:20]):
                result = self.review(Transport(value))
                self.assertEqual(result["status"], "invalid-reply")
                self.assertNotIn("findings_path", result)

    def test_advisory_references_are_bounded_and_default_to_empty(self):
        expected = {"records": [0, 1], "warnings": ["#/payload/warnings/0"]}
        legacy = json.loads(reply()["content"])
        self.assertEqual(model_review._parse(json.dumps(legacy), expected)
                         ["unverifiable_references"], [])
        many = [" ", " cited spec "] + ["x" * 1200] * 60
        parsed = model_review._parse(reply(unverifiable_references=many)["content"], expected)
        self.assertEqual(parsed["unverifiable_references"][0], "cited spec")
        self.assertEqual(len(parsed["unverifiable_references"]), 50)
        self.assertEqual({len(s) for s in parsed["unverifiable_references"][1:]}, {1000})
        for bad in ("cited spec", [1]):
            self.assertIsNone(model_review._parse(
                reply(unverifiable_references=bad)["content"], expected))

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

    def test_findings_over_policy_budget_are_not_written(self):
        self.policy["budget"]["max_findings_bytes"] = 1024
        result = self.review(Transport(reply(missing_evidence=["x" * 900, "y" * 900])))
        self.assertEqual(result["status"], "over-budget")
        self.assertNotIn("findings_path", result)

    def test_relative_root_writes_findings(self):
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        result = model_review.review(Path("."), self.rendition, self.policy, "model-a",
                                     Transport(reply()))
        self.assertTrue((self.root / result["findings_path"]).is_file())

    def test_reviewer_must_name_the_model(self):
        self.policy["reviewer"] = "usai:other"
        with self.assertRaises(ValueError):
            self.review(Transport())


class RangeFindingsTests(ReviewCase):
    def test_range_reply_records_the_same_explicit_findings_as_a_list_reply(self):
        listed = model_review.review(self.root, self.rendition, self.policy, "model-a",
                                     Transport(reply()))
        ranged_transport = Transport(reply(records=["0-1"]))
        ranged = model_review.review(self.root, self.rendition, self.policy, "model-a",
                                     ranged_transport, inspected_form="ranges")
        self.assertEqual(listed["findings_sha256"], ranged["findings_sha256"])
        self.assertEqual(listed["findings_path"], ranged["findings_path"])
        self.assertIn("first-last", ranged_transport.calls[0][1][0]["content"])


class InspectionFormABTests(ReviewCase):
    def test_report_is_body_free_and_compares_forms_without_writing(self):
        from tools.diagnostics import inspection_form_ab as ab

        def transport(model, messages):
            wants_ranges = "first-last" in messages[0]["content"]
            return reply(records=["0-1"] if wants_ranges else [0, 1])
        before = sorted(p for p in self.root.rglob("*") if p.is_file())
        report = ab.run_ab(self.root, [self.rendition, self.rendition], "model-a", transport)
        self.assertEqual(sorted(p for p in self.root.rglob("*") if p.is_file()), before)
        self.assertEqual((report["comparison"]["paired"], report["comparison"]["decision_agreement"],
                          report["comparison"]["identical_inspection"]), (2, 2, 2))
        self.assertEqual(report["forms"]["list"]["complete"], 2)
        self.assertNotIn("Distinct heading phrase", json.dumps(report))
        with self.assertRaises(ValueError):
            ab.run_ab(self.root, [self.rendition], "model-a", transport, order="sideways")

    def test_a_failed_reply_in_one_form_is_counted_not_paired(self):
        from tools.diagnostics import inspection_form_ab as ab

        def transport(model, messages):
            if "first-last" in messages[0]["content"]:
                return reply(records=["5-9"])
            return reply()
        report = ab.run_ab(self.root, [self.rendition], "model-a", transport)
        self.assertEqual((report["forms"]["ranges"]["failed"], report["comparison"]["paired"]), (1, 0))


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


class CredentialTests(unittest.TestCase):
    def test_api_key_follows_pi_interpolation(self):
        with patch.dict(os.environ, {"SECRET_KEY": "s3"}):
            self.assertEqual(model_review.api_key("${SECRET_KEY}"), "s3")
            self.assertEqual(model_review.api_key("$SECRET_KEY"), "s3")
            self.assertEqual(model_review.api_key("lit$$eral"), "lit$eral")
            self.assertEqual(model_review.api_key("plain"), "plain")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(model_review.TransportError) as caught:
                model_review.api_key("${SECRET_KEY}")
            self.assertEqual(caught.exception.code, "credential-unavailable")
        with self.assertRaises(model_review.TransportError) as caught:
            model_review.api_key("!secret-tool lookup usai")
        self.assertEqual(caught.exception.code, "credential-command-unsupported")


class TransportTests(ReviewCase):
    def test_usai_transport_refuses_unconfigured_models_and_providers(self):
        models = Path(self.directory.name) / "models.json"
        models.write_text(json.dumps({"providers": {
            "usai": {"baseUrl": "https://api.example.invalid/v1", "api": "openai-completions",
                     "apiKey": "k", "models": [{"id": "model-a"}]},
            "other": {"baseUrl": "https://elsewhere", "apiKey": "k", "models": [{"id": "model-b"}]}}}))
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
        transport = model_review.usai_transport(models, urlopen=urlopen)
        with self.assertRaises(model_review.TransportError):
            transport("model-b", [])
        value = transport("model-a", [{"role": "user", "content": "x"}])
        self.assertEqual(sent["url"], "https://api.example.invalid/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer k")
        self.assertEqual(sent["body"]["model"], "model-a")
        self.assertEqual(value, {"content": "{}", "usage": {"prompt_tokens": 3,
                                                            "completion_tokens": 1}})


class RetryTests(unittest.TestCase):
    """usai_transport retries only transient failures, with bounded, injectable waits."""

    def setUp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.models = Path(directory.name) / "models.json"
        self.models.write_text(json.dumps({"providers": {"usai": {
            "baseUrl": "https://api.example.invalid/v1", "apiKey": "k",
            "models": [{"id": "model-a"}]}}}))

    def transport(self, *outcomes, **options):
        """Each outcome is an HTTP status (with optional Retry-After) or an exception."""
        import email.message
        import io as _io
        import urllib.error
        self.urls, self.waits, pending = [], [], list(outcomes)

        def urlopen(request, timeout):
            self.urls.append(request.full_url)
            outcome = pending.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            status, after = outcome if isinstance(outcome, tuple) else (outcome, None)
            if status == 200:
                return _io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}],
                                               "usage": {}}).encode())
            headers = email.message.Message()
            if after is not None:
                headers["Retry-After"] = after
            raise urllib.error.HTTPError(request.full_url, status, "error", headers,
                                         _io.BytesIO(b"secret error body"))
        options.setdefault("jitter", lambda: 1.0)
        call = model_review.usai_transport(self.models, urlopen=urlopen,
                                           sleep=self.waits.append, clock=lambda: 0.0,
                                           **options)
        return call

    def test_rate_limits_and_server_errors_are_retried_honouring_retry_after(self):
        import urllib.error
        call = self.transport((429, "7"), 503, urllib.error.URLError("down"), 200)
        self.assertEqual(call("model-a", [])["content"], "ok")
        self.assertEqual(self.waits, [7.0, 4.0, 8.0])
        self.assertEqual(dict(call.retries), {"http-429": 1, "http-503": 1, "network": 1})
        self.assertEqual(set(self.urls), {"https://api.example.invalid/v1/chat/completions"})

    def test_waits_are_capped(self):
        call = self.transport((429, "3600"), (429, "soon"), 200, max_wait_seconds=30.0,
                              jitter=lambda: 0.0)
        call("model-a", [])
        self.assertEqual(self.waits, [30.0, 2.0])

    def test_client_errors_and_invalid_replies_are_not_retried(self):
        for status in (400, 401, 404, 302):
            with self.subTest(status):
                call = self.transport(status, 200)
                with self.assertRaises(model_review.TransportError) as caught:
                    call("model-a", [])
                self.assertEqual(caught.exception.code, "http-%d" % status)
                self.assertEqual((len(self.urls), self.waits), (1, []))
        call = self.transport(ValueError("not json"), 200)
        with self.assertRaises(model_review.TransportError):
            call("model-a", [])
        self.assertEqual(len(self.urls), 1)

    def test_gives_up_after_max_attempts(self):
        call = self.transport(*[503] * 5, 200, max_attempts=3)
        with self.assertRaises(model_review.TransportError) as caught:
            call("model-a", [])
        self.assertEqual(caught.exception.code, "http-503")
        self.assertEqual((len(self.urls), len(self.waits)), (3, 2))
        self.assertEqual(dict(call.retries), {"http-503": 2})

    def test_observers_hear_each_retried_code_before_the_wait(self):
        call = self.transport(429, 503, 200)
        heard = []
        call.observers.append(lambda code: heard.append((code, len(self.waits))))
        call("model-a", [])
        self.assertEqual(heard, [("http-429", 0), ("http-503", 1)])

    def test_redirects_are_refused(self):
        handler = model_review._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {},
                                                   "https://elsewhere.invalid/"))


if __name__ == "__main__":
    unittest.main()
