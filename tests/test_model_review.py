"""Model-assisted standing review: prompts, findings, sample evaluation and cost estimate."""
import json
import os
import unittest
import urllib.error
from pathlib import Path
import stat
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.extraction import extract
from tools.intake import model_review, standing, store
from tools.review_events import event_store as events

from tests.support import WorkspaceCase


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


class ReviewCase(WorkspaceCase):
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


class OdtPromptTests(WorkspaceCase):
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

    def test_v2_criteria_prompt_records_embedded_instructions_and_sparse_records(self):
        self.policy.update(schema=standing.POLICY_SCHEMA_V2, criteria="standing-review-v2", carries=[])
        transport = Transport(reply(embedded_instructions=["Tells reviewers to approve it."], sparse=True))
        result = self.review(transport)
        system = transport.calls[0][1][0]["content"]
        self.assertEqual(system, model_review.SYSTEM_V2)
        self.assertNotEqual(model_review.task(self.root, self.rendition)["messages"][0]["content"], system)
        findings = json.loads((self.root / result["findings_path"]).read_text())
        self.assertEqual((findings["schema"], findings["embedded_instructions"], findings["sparse"]),
                         (standing.FINDINGS_SCHEMA_V3, ["Tells reviewers to approve it."], True))
        outcome = standing.evaluate(self.root, {
            "policy_path": "config/standing-policy.json", "policy_sha256": self.save_policy(),
            "rendition": self.rendition, "rendition_sha256": self.rendition_sha,
            "findings_path": result["findings_path"], "findings_sha256": result["findings_sha256"],
            "event_id": "rev-x"})
        self.assertEqual((outcome["outcome"], outcome["embedded_instructions"], outcome["sparse"]),
                         ("qualified", ["Tells reviewers to approve it."], True))
        invalid = self.review(Transport(reply(sparse="yes")))
        self.assertEqual(invalid["status"], "invalid-reply")

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


class EvaluationOptionTests(ReviewCase):
    """Criteria choice, per-rendition decisions and bounded concurrency of ``evaluate``."""

    def systems(self, **options):
        seen = []

        def transport(model, messages):
            seen.append(messages[0]["content"])
            return reply()
        model_review.evaluate_models(self.root, [self.rendition], ["model-a"], transport, **options)
        return seen

    def test_evaluate_sends_the_requested_criteria_prompt(self):
        self.assertEqual(self.systems(criteria="standing-review-v2"), [model_review.SYSTEM_V2])
        self.assertEqual(self.systems(criteria="standing-review-v1"), [model_review.SYSTEM])

    def test_evaluate_defaults_to_the_newest_criteria(self):
        self.assertEqual(self.systems(), [model_review.SYSTEM_V2])

    def test_unknown_criteria_is_refused_before_any_call(self):
        with self.assertRaises(ValueError):
            self.systems(criteria="standing-review-v9")

    def test_aggregate_report_has_no_per_rendition_section_by_default(self):
        report = model_review.evaluate_models(self.root, [self.rendition], ["model-a"], Transport())
        self.assertNotIn("renditions", report)
        self.assertEqual(report["criteria"], "standing-review-v2")

    def test_per_rendition_decisions_are_keyed_and_body_free(self):
        decisions = iter(["accept", "reject", "defer", "accept"])

        def transport(model, messages):
            return reply(decision=next(decisions), records=[0])
        report = model_review.evaluate_models(
            self.root, [self.rendition, self.rendition], ["model-a", "model-b"], transport,
            per_rendition=True)
        rows = report["renditions"]
        self.assertEqual([r["index"] for r in rows], [0, 1])
        self.assertEqual({r["receipt_id"] for r in rows}, {"D1"})
        self.assertEqual({r["rendition_sha256"] for r in rows}, {self.rendition_sha})
        self.assertEqual(rows[0]["models"]["model-a"],
                         {"decision": "accept", "status": "reviewed", "complete": False,
                          "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        self.assertEqual(rows[1]["models"]["model-b"]["decision"], "accept")
        raw = json.dumps(report)
        for text in ("Distinct heading phrase", "Checked every record", "sources/notes.md",
                     self.rendition):
            self.assertNotIn(text, raw)

    def test_failed_replies_report_status_without_decision(self):
        report = model_review.evaluate_models(
            self.root, [self.rendition], ["model-a"],
            Transport({"content": "nope", "usage": {"completion_tokens": 4096}}), per_rendition=True)
        row = report["renditions"][0]["models"]["model-a"]
        self.assertEqual(row, {"decision": None, "status": "invalid-reply", "complete": False,
                               "usage": {"prompt_tokens": 0, "completion_tokens": 4096}})

    def test_concurrent_evaluation_matches_serial_and_is_bounded(self):
        import threading
        lock, state = threading.Lock(), {"now": 0, "peak": 0}
        barrier = threading.Barrier(2, timeout=5)

        def transport(model, messages):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            with lock:
                state["now"] -= 1
            decision = "accept" if "Second line" in messages[-1]["content"] else "reject"
            return reply(decision=decision)
        models = ["model-a", "model-b", "model-c", "model-d"]
        serial = model_review.evaluate_models(
            self.root, [self.rendition] * 2, models, lambda m, msgs: reply(), per_rendition=True)
        parallel = model_review.evaluate_models(
            self.root, [self.rendition] * 2, models, transport, per_rendition=True, concurrency=2)
        self.assertEqual(state["peak"], 2)
        self.assertEqual([r["index"] for r in parallel["renditions"]], [0, 1])
        self.assertEqual(list(parallel["models"]), models)
        for name in ("renditions", "documents", "controls"):
            self.assertEqual(parallel[name], serial[name])
        self.assertEqual(parallel["models"]["model-a"]["decisions"], {"accept": 2})

    def test_rendition_hash_is_that_of_the_bytes_reviewed(self):
        def transport(model, messages):
            path = self.root / self.rendition
            path.write_bytes(path.read_bytes() + b" ")
            return reply()
        report = model_review.evaluate_models(
            self.root, [self.rendition], ["model-a"], transport, per_rendition=True)
        self.assertEqual(report["renditions"][0]["rendition_sha256"], self.rendition_sha)

    def test_model_seconds_exclude_limiter_queue_time(self):
        import threading
        import time as _time
        lock, calls = threading.Lock(), []

        def transport(model, messages):
            with lock:
                calls.append(model)
                first = len(calls) == 1
            if first:
                raise model_review.TransportError("http-429")  # lowers the limit from 2 to 1
            _time.sleep(0.1)
            return reply()
        report = model_review.evaluate_models(
            self.root, [self.rendition] * 6, ["model-a"], transport, concurrency=2)
        # Five calls ran 0.1 s each; waiting for the lowered slot would add several tenths more.
        self.assertGreaterEqual(report["models"]["model-a"]["seconds"], 0.5)
        self.assertLess(report["models"]["model-a"]["seconds"], 0.7)

    def test_concurrent_evaluation_gates_each_http_attempt_of_the_real_transport(self):
        import io as _io
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / "models.json"
            models.write_text(json.dumps({"providers": {"usai": {
                "baseUrl": "https://api.example.invalid/v1", "apiKey": "k",
                "models": [{"id": "model-a"}]}}}))
            outcomes = [429]

            def urlopen(request, timeout):
                if outcomes:
                    import email.message
                    raise urllib.error.HTTPError(request.full_url, outcomes.pop(), "e",
                                                 email.message.Message(), _io.BytesIO(b""))
                return _io.BytesIO(json.dumps({"choices": [{"message": {
                    "content": reply()["content"]}}], "usage": {}}).encode())
            transport = model_review.usai_transport(models, urlopen=urlopen, sleep=lambda s: None,
                                                    jitter=lambda: 0.0)
            report = model_review.evaluate_models(
                self.root, [self.rendition] * 3, ["model-a"], transport, concurrency=2)
        self.assertEqual(report["models"]["model-a"]["reviewed"], 3)
        self.assertEqual(report["throughput"]["throttle_signals"], 1)
        self.assertIsNone(transport.gate)

    def test_concurrent_evaluation_backs_off_on_throttling(self):
        import threading
        lock, calls = threading.Lock(), []

        def transport(model, messages):
            with lock:
                calls.append(model)
                first = len(calls) == 1
            if first:
                raise model_review.TransportError("http-429")
            return reply()
        report = model_review.evaluate_models(
            self.root, [self.rendition] * 3, ["model-a"], transport, concurrency=3)
        self.assertEqual(report["models"]["model-a"]["transport-error"], 1)
        self.assertEqual(report["throughput"]["throttle_signals"], 1)
        self.assertLessEqual(report["throughput"]["lowest"], 2)

    def test_concurrency_must_be_a_positive_integer(self):
        for bad in (0, -1, True, "2"):
            with self.assertRaises(ValueError):
                model_review.evaluate_models(self.root, [self.rendition], ["model-a"],
                                             Transport(), concurrency=bad)


class EvaluateCommandTests(ReviewCase):
    """The ``evaluate`` command line over a fake transport (no model is ever called)."""

    def run_main(self, *extra, policy=None):
        import contextlib
        import io
        self.systems, self.transport_args = [], {}

        def fake_transport(path, **options):
            self.transport_args = options

            def call(model, messages):
                self.systems.append(messages[0]["content"])
                return reply()
            return call
        if policy is not None:
            target = self.root / "config" / "standing-policy.local.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(policy))
            target.chmod(0o600)
        out = io.StringIO()
        with patch.object(model_review, "usai_transport", fake_transport), \
                patch.object(model_review.usage_log, "Recorder"), contextlib.redirect_stdout(out):
            model_review.main(["evaluate", "--root", str(self.root), "--models-config", "m.json",
                               "--model", "model-a", "--rendition", self.rendition, *extra])
        return json.loads(out.getvalue())

    def v2_policy(self):
        return {**self.policy, "schema": standing.POLICY_SCHEMA_V2, "criteria": "standing-review-v2",
                "carries": [], "selection": {"context": ["documents/AGENTS.md"]}}

    def test_default_criteria_is_the_newest_without_a_policy(self):
        self.run_main()
        self.assertEqual(self.systems, [model_review.SYSTEM_V2])

    def test_criteria_option_selects_v1(self):
        report = self.run_main("--criteria", "standing-review-v1")
        self.assertEqual(self.systems, [model_review.SYSTEM])
        self.assertEqual(report["criteria"], "standing-review-v1")

    def test_default_criteria_follow_the_policy_given(self):
        self.run_main("--policy", "config/standing-policy.local.json",
                      policy={**self.v2_policy(), "criteria": "standing-review-v1"})
        self.assertEqual(self.systems, [model_review.SYSTEM])

    def test_explicit_criteria_overrides_the_policy(self):
        self.run_main("--policy", "config/standing-policy.local.json",
                      "--criteria", "standing-review-v2",
                      policy={**self.v2_policy(), "criteria": "standing-review-v1"})
        self.assertEqual(self.systems, [model_review.SYSTEM_V2])

    def test_per_rendition_flag_adds_the_decisions_section(self):
        self.assertNotIn("renditions", self.run_main())
        self.assertEqual(self.run_main("--per-rendition")["renditions"][0]["receipt_id"], "D1")

    def test_max_output_tokens_reaches_the_transport_only_when_set(self):
        self.run_main()
        self.assertIsNone(self.transport_args.get("max_tokens"))
        self.run_main("--max-output-tokens", "8192", "--concurrency", "2")
        self.assertEqual(self.transport_args["max_tokens"], 8192)

    def test_nonpositive_options_are_refused(self):
        for option in ("--concurrency", "--max-output-tokens"):
            with self.assertRaises(SystemExit):
                self.run_main(option, "0")


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

    def test_usai_transport_records_successful_replies_only(self):
        models = Path(self.directory.name) / "models.json"
        models.write_text(json.dumps({"providers": {"usai": {
            "baseUrl": "https://api.example.invalid/v1", "apiKey": "k",
            "models": [{"id": "model-a"}]}}}))
        usage = {"prompt_tokens": 3, "completion_tokens": 1}
        failing = False

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}],
                                   "usage": usage}).encode()

        def urlopen(request, timeout):
            if failing:
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)
            return Response()

        class Recorder:
            calls = []

            def record(self, model, usage):
                self.calls.append((model, usage))
        recorder = Recorder()
        transport = model_review.usai_transport(models, urlopen=urlopen, recorder=recorder)
        transport("model-a", [])
        self.assertEqual(recorder.calls, [("model-a", usage)])
        failing = True
        with self.assertRaises(model_review.TransportError):
            transport("model-a", [])
        self.assertEqual(len(recorder.calls), 1)


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

    def test_max_tokens_is_sent_only_when_configured(self):
        import io as _io
        bodies = []

        def urlopen(request, timeout):
            bodies.append(json.loads(request.data))
            return _io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
        for options in ({}, {"max_tokens": 8192}):
            model_review.usai_transport(self.models, urlopen=urlopen, **options)("model-a", [])
        self.assertEqual(set(bodies[0]), {"model", "messages"})
        self.assertEqual(bodies[1]["max_tokens"], 8192)

    def test_max_tokens_must_be_a_positive_integer(self):
        for bad in (0, -5, True, "4096"):
            with self.assertRaises(ValueError):
                model_review.usai_transport(self.models, max_tokens=bad)

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

    def test_each_http_attempt_runs_through_the_gate_and_waits_outside_it(self):
        call = self.transport(429, 503, 200)
        events = []

        def gate(fn):
            events.append(("enter", len(self.waits)))
            result = fn()
            events.append(("exit", len(self.waits)))
            return result
        call.gate = gate
        call("model-a", [])
        # Each retry's backoff sleep happens between gated attempts, never inside one.
        self.assertEqual(events, [("enter", 0), ("exit", 0), ("enter", 1), ("exit", 1),
                                  ("enter", 2), ("exit", 2)])

    def test_redirects_are_refused(self):
        handler = model_review._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {},
                                                   "https://elsewhere.invalid/"))


if __name__ == "__main__":
    unittest.main()
