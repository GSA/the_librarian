"""Standing-policy review, qualification and publication through durable intake."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.context_index import confidence, packets
from tools.extraction import docling_bridge
from tools.review_events import event_history
from tools.review_events import event_store as events
from tools.intake import adapters, contracts, runner, standing, standing_events, standing_publication, store

from tests.support import WorkspaceCase
from test_intake_adapters import stage, stage_request

RUN = "a" * 64
RENDITION = f"data/extractions/.local/D1/{RUN}/extraction.json"
DB = "tools/context_index/.local/index.sqlite3"


def manifest_entry(root, name, path):
    raw = json.loads((root / name).read_bytes().decode("utf-8"), object_pairs_hook=index.strict_object)
    return [item for item in raw.get("renditions", []) if item["path"] == path]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class StandingCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        raw_source = b"%PDF-synthetic-standing-test"
        records, _ = self.registration("sources/input.pdf", raw_source)
        source = {"receipt_id": "D1", "artifact_id": "A-input", "source_ids": ["S1"],
                  "path": "sources/input.pdf", "sha256": sha(raw_source),
                  "bytes": len(raw_source), "audience": "source", "handling_refs": []}
        bbox = {"l": 1, "t": 2, "r": 80, "b": 40, "coord_origin": "TOPLEFT"}
        self.value = {
            "schema": "docling-rendition-v1", "run_id": RUN,
            "extractor": {"schema_version": 1, "label": "synthetic-docling"},
            "source": source, "boundary": "Machine rendition; exact review still required.",
            "conversion": {"schema": "docling-conversion-v1", "format": "pdf", "document": {
                "texts": [{"self_ref": "#/texts/0", "label": "text", "text": "Standing phrase",
                           "prov": [{"page_no": 1, "bbox": bbox}]}],
                "tables": [{"self_ref": "#/tables/0", "label": "table",
                            "prov": [{"page_no": 2, "bbox": bbox}], "data": {
                                "table_cells": [{"text": "Cell", "start_row_offset_idx": 0,
                                                 "start_col_offset_idx": 0, "end_row_offset_idx": 1,
                                                 "end_col_offset_idx": 1}],
                                "num_rows": 1, "num_cols": 1}}]},
                "pages": [{"page_no": 1, "width": 100, "height": 100, "textless": False},
                          {"page_no": 2, "width": 100, "height": 100, "textless": False}],
                "warnings": [{"code": "low_confidence", "message": "Synthetic warning"}],
                "confidence": {"layout": 0.99}, "omissions": []}}
        self.raw = self.write_rendition(self.value)
        registered = deepcopy(self.value)
        registered["canonical_bytes"] = self.raw
        catalog.submit(self.root, docling_bridge.registration_records(
            self.root, records[2], self.root / RENDITION, registered), actor="test-runner",
            request_id="register-docling", expected_state=catalog.status(self.root)["state_token"],
            reason="Synthetic Docling registration")
        events.initialize(self.root)
        self.docs()
        self.policy = {"schema": standing.POLICY_SCHEMA, "policy_id": "routine-docling",
                       "version": 1, "status": "active", "reviewer": "standing-reviewer",
                       "scope": {"receipts": ["D1"], "audiences": ["source"]},
                       "budget": {"max_pages": 10, "max_records": 10,
                                  "max_findings_bytes": 64 * 1024},
                       "selection": {"context": ["documents/AGENTS.md"]}}
        self.findings = {"schema": standing.FINDINGS_SCHEMA,
                         "policy": {"policy_id": "routine-docling", "version": 1},
                         "rendition": RENDITION, "rendition_sha256": sha(self.raw),
                         "reviewer": "standing-reviewer", "decision": "accept",
                         "inspected": {"pages": [1, 2], "texts": ["#/texts/0"],
                                       "tables": ["#/tables/0"], "warnings": ["#/warnings/0"]},
                         "explanation": "Compared every page, text, table and warning to the original.",
                         "contradictions": [], "missing_evidence": [],
                         "unverifiable_references": [], "correction_of": None,
                         "new_mapping": False, "exception_resolution": None}
        self.write_private("config/standing-policy.json", self.policy)
        self.write_private("config/standing-findings.json", self.findings)

    def write_rendition(self, value):
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        path = self.root / RENDITION
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
        return raw

    def write_private(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value).encode() if not isinstance(value, bytes) else value
        path.write_bytes(raw)
        path.chmod(0o600)
        return sha(raw)

    def review_args(self):
        return {"policy_path": "config/standing-policy.json",
                "policy_sha256": sha((self.root / "config/standing-policy.json").read_bytes()),
                "rendition": RENDITION, "rendition_sha256": sha(self.raw),
                "findings_path": "config/standing-findings.json",
                "findings_sha256": sha((self.root / "config/standing-findings.json").read_bytes()),
                "event_id": "rev-standing"}

    def stage_all(self, correlation="req-standing", **extra):
        review = {**self.review_args(), **extra}
        stages = [
            stage("review", "standing-review", {**review, "request_id": "req-standing-review"},
                  "review"),
            stage("publication", "standing-select",
                  {**review, "request_id": "req-standing-select",
                   "manifest": "config/selection.json"}, "select"),
            stage("publication", "standing-publish",
                  {"policy_path": review["policy_path"], "policy_sha256": review["policy_sha256"],
                   "manifest": "config/selection.json", "db": DB}, "publish"),
        ]
        stage_request(self.root, stages, correlation)
        return correlation

    def run_stage(self, name, correlation="req-standing"):
        result = runner.run(self.root, correlation, name)
        return next(s for s in result["stages"] if s["id"] == name)

    def stored_events(self):
        _, db, _ = events.locations(self.root)
        with closing(events.connect(db)) as connection:
            connection.execute("BEGIN")
            return event_history.inspect(connection, self.root)[1]

    def evaluate(self):
        return standing.evaluate(self.root, self.review_args())


class StandingEvaluationTests(StandingCase):
    def test_complete_attributed_findings_qualify(self):
        result = self.evaluate()
        self.assertEqual(result["outcome"], "qualified")
        self.assertEqual(result["inspected"], {"pages": 2, "texts": 1, "tables": 1, "warnings": 1})
        self.assertEqual(result["content"], ["tables", "text"])

    def assert_outcome(self, outcome, reason):
        result = self.evaluate()
        self.assertEqual((result["outcome"], result["reasons"]), (outcome, [reason]))
        return result

    def test_fabricated_success_without_inspection_is_not_qualified(self):
        # Parser/model success and confidence claims without inspected scope.
        self.findings["inspected"] = {"pages": [], "texts": [], "tables": [], "warnings": []}
        self.findings["explanation"] = "Docling reported success with 0.99 confidence."
        self.write_private("config/standing-findings.json", self.findings)
        self.assert_outcome("pending-review", "insufficient-inspected-scope")

    def test_fabricated_extra_fields_are_invalid_findings(self):
        self.findings["confidence"] = 1.0
        self.write_private("config/standing-findings.json", self.findings)
        self.assert_outcome("exception", "invalid-findings")

    def test_insufficient_inspected_scope(self):
        for key in ("pages", "texts", "tables", "warnings"):
            with self.subTest(key=key):
                partial = deepcopy(self.findings)
                partial["inspected"][key] = partial["inspected"][key][:-1]
                self.write_private("config/standing-findings.json", partial)
                self.assert_outcome("pending-review", "insufficient-inspected-scope")

    def test_inspected_claims_beyond_rendition_are_insufficient(self):
        self.findings["inspected"]["pages"] = [1, 2, 3]
        self.write_private("config/standing-findings.json", self.findings)
        self.assert_outcome("pending-review", "insufficient-inspected-scope")

    def test_docling_fidelity_failures_are_exceptions(self):
        broken = deepcopy(self.value)
        broken["conversion"]["document"]["tables"][0]["prov"][0]["page_no"] = 7
        raw = self.write_rendition(broken)
        self.raw = raw
        self.findings["rendition_sha256"] = sha(raw)
        self.write_private("config/standing-findings.json", self.findings)
        # The catalog child binds the original bytes, so this is never qualified.
        self.assertEqual(self.evaluate()["outcome"], "exception")

    def test_docling_fidelity_checks_page_and_table_content(self):
        with patch.object(standing, "_registered", return_value={"id": "x"}):
            for label, mutate in (
                    ("page", lambda v: v["conversion"]["document"]["tables"][0]["prov"][0]
                     .update(page_no=7)),
                    ("table", lambda v: v["conversion"]["document"]["tables"][0]["data"]
                     .update(num_rows=0))):
                with self.subTest(label=label):
                    broken = deepcopy(self.value)
                    mutate(broken)
                    self.raw = self.write_rendition(broken)
                    self.findings["rendition_sha256"] = sha(self.raw)
                    self.write_private("config/standing-findings.json", self.findings)
                    self.assert_outcome("exception", "rendition-fidelity")

    def test_stale_policy_and_revoked_policy(self):
        args = self.review_args()
        self.policy["version"] = 2
        self.write_private("config/standing-policy.json", self.policy)
        with self.assertRaises(standing.Refusal) as caught:
            standing.evaluate(self.root, args)
        self.assertEqual(caught.exception.code, "standing-policy-changed")
        self.policy.update(version=1, status="revoked")
        self.write_private("config/standing-policy.json", self.policy)
        self.assert_outcome("exception", "policy-revoked")

    def test_each_warning_instance_must_be_inspected(self):
        self.value["conversion"]["warnings"].append(
            {"code": "low_confidence", "message": "Second field"})
        self.assertEqual(len(self.value["conversion"]["warnings"]), 2)
        with patch.object(standing, "_registered", return_value={"id": "x"}):
            self.raw = self.write_rendition(self.value)
            self.findings["rendition_sha256"] = sha(self.raw)
            self.write_private("config/standing-findings.json", self.findings)
            self.assert_outcome("pending-review", "insufficient-inspected-scope")
            self.findings["inspected"]["warnings"] = ["#/warnings/0", "#/warnings/1"]
            self.write_private("config/standing-findings.json", self.findings)
            self.assert_outcome("qualified", "complete-inspected-scope")

    def test_malformed_enum_values_refuse_durably(self):
        self.write_private("config/standing-findings.json", {**self.findings, "decision": []})
        self.assert_outcome("exception", "invalid-findings")
        self.policy["status"] = []
        self.write_private("config/standing-policy.json", self.policy)
        self.stage_all()
        result = self.run_stage("review")
        self.assertEqual((result["status"], result["attempts"][-1]["code"]),
                         ("blocked", "standing-policy-invalid"))

    def test_excluded_original_refuses_before_review(self):
        with patch.object(standing.index, "original_allowed", return_value=False):
            with self.assertRaises(standing.Refusal) as caught:
                self.evaluate()
        self.assertEqual(caught.exception.code, "prohibited-source")

    def assert_not_reusable(self, mutate):
        result = self.evaluate()
        event = standing_events.review_event(result, self.review_args())
        mutate(event)
        event["evidence"] = [{"kind": "agent_statement", "pointer": None,
                              "quote": "Independent review outside the standing policy."}]
        state = events.status(self.root)
        events.append(self.root, event_history.bundle([event]), "standing-reviewer", "req-other",
                      state["revision"], state["state_token"])
        self.assertIsNone(standing_events.bound_event(self.root, result, self.review_args()))
        self.assert_outcome("pending-review", "conflicting-review")

    def test_select_cannot_reuse_partial_review_event(self):
        self.assert_not_reusable(lambda e: (e.update(outcome="partial"), e["counts"].update(
            total=2, reviewed=1, pending=1)))

    def test_select_cannot_reuse_independent_complete_review_event(self):
        self.assert_not_reusable(lambda e: None)

    def test_policy_must_be_private(self):
        (self.root / "config/standing-policy.json").chmod(0o644)
        with self.assertRaises(standing.Refusal) as caught:
            self.evaluate()
        self.assertEqual(caught.exception.code, "standing-private-file")

    def test_unavailable_review_route_is_retryable(self):
        args = self.review_args()
        (self.root / "config/standing-findings.json").unlink()
        with self.assertRaises(standing.Refusal) as caught:
            standing.evaluate(self.root, args)
        self.assertEqual((caught.exception.category, caught.exception.code),
                         ("retryable", "review-route-unavailable"))

    def test_budget_failures(self):
        self.policy["budget"]["max_pages"] = 1
        self.write_private("config/standing-policy.json", self.policy)
        self.assert_outcome("exception", "budget-exceeded")
        self.policy["budget"].update(max_pages=10, max_findings_bytes=10)
        self.write_private("config/standing-policy.json", self.policy)
        self.assert_outcome("exception", "budget-exceeded")

    def test_prohibited_scope(self):
        self.policy["scope"]["receipts"] = ["D2"]
        self.write_private("config/standing-policy.json", self.policy)
        self.assert_outcome("exception", "prohibited-scope")

    def test_any_receipt_scope_entry_covers_upload_receipts_only(self):
        self.policy["scope"]["receipts"] = ["D*"]
        self.write_private("config/standing-policy.json", self.policy)
        self.assertEqual(self.evaluate()["outcome"], "qualified")
        scope = {"receipts": ["D*"], "audiences": ["source"]}
        self.assertTrue(standing.permits(scope, "D90000", "source"))
        self.assertFalse(standing.permits(scope, "D90000", "agent"))
        self.assertFalse(standing.permits(scope, "X1", "source"))
        self.assertFalse(standing.permits({"receipts": ["D1"], "audiences": ["source"]}, "D2", "source"))

    def test_out_of_scope_rendition_without_findings_is_prohibited_scope_not_retryable(self):
        self.policy["scope"]["receipts"] = ["D2"]
        self.write_private("config/standing-policy.json", self.policy)
        args = self.review_args()
        (self.root / "config/standing-findings.json").unlink()
        self.assertTrue(standing.refused_scope(
            self.root, self.policy, RENDITION, sha(self.raw)))
        result = standing.evaluate(self.root, args)
        self.assertEqual((result["outcome"], result["reasons"]), ("exception", ["prohibited-scope"]))
        # In scope, a missing review stays the retryable route-unavailable refusal.
        self.policy["scope"]["receipts"] = ["D1"]
        args["policy_sha256"] = self.write_private("config/standing-policy.json", self.policy)
        self.assertFalse(standing.refused_scope(self.root, self.policy, RENDITION, sha(self.raw)))
        with self.assertRaises(standing.Refusal) as caught:
            standing.evaluate(self.root, args)
        self.assertEqual(caught.exception.code, "review-route-unavailable")

    def test_scope_precheck_does_not_judge_a_stale_or_missing_rendition(self):
        self.policy["scope"]["receipts"] = ["D2"]
        self.assertFalse(standing.refused_scope(self.root, self.policy, RENDITION, "0" * 64))
        self.assertFalse(standing.refused_scope(
            self.root, self.policy, RENDITION.replace("D1", "D9"), sha(self.raw)))

    def test_stale_source(self):
        (self.root / "sources/input.pdf").write_bytes(b"%PDF-changed")
        with self.assertRaises(standing.Refusal) as caught:
            self.evaluate()
        self.assertEqual(caught.exception.code, "stale-source")

    def test_disputed_corrections_and_new_mappings_need_independent_review(self):
        for update in ({"correction_of": "rev-prior"}, {"new_mapping": True}):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json", {**self.findings, **update})
                self.assert_outcome("pending-review", "independent-review-required")

    def test_existing_independent_review_is_not_bypassed(self):
        event = self.pending(RENDITION)
        event["event_id"] = "rev-independent"
        events.append(self.root, event_history.bundle([event]), "test-runner", "req-independent", 0,
                      events.status(self.root)["state_token"])
        self.assert_outcome("pending-review", "conflicting-review")

    def test_rejected_and_deferred_stay_so_even_with_reported_issues(self):
        issues = {"contradictions": ["Table total differs."], "missing_evidence": ["Figure 2 is absent."]}
        for update, expected in (({"decision": "reject"}, ("rejected", "reviewer-rejected")),
                                 ({"decision": "defer"}, ("deferred", "reviewer-deferred")),
                                 ({"decision": "reject", **issues}, ("rejected", "reviewer-rejected")),
                                 ({"decision": "defer", **issues}, ("deferred", "reviewer-deferred"))):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json", {**self.findings, **update})
                self.assert_outcome(*expected)

    def test_accepted_review_with_reported_issues_qualifies_and_attaches_them(self):
        for update in ({"contradictions": ["Table total differs."]},
                       {"missing_evidence": ["Referenced figure 2 is absent."],
                        "unverifiable_references": ["Cites an external spec."]},
                       {"contradictions": ["Title says 7 MB, body says 8 MB.", "Lambda named two ways."],
                        "missing_evidence": ["README is not in the rendition."]}):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json", {**self.findings, **update})
                result = self.assert_outcome("qualified", "complete-inspected-scope")
                event = standing_events.review_event(result, self.review_args())
                quotes = [e["quote"] for e in event["evidence"]]
                contradictions, missing = update.get("contradictions", []), update.get("missing_evidence", [])
                self.assertEqual([q for q in quotes if q.startswith("Reviewer-noted contradiction: ")],
                                 ["Reviewer-noted contradiction: " + c for c in contradictions])
                self.assertEqual([q for q in quotes if q.startswith("Reviewer-noted missing evidence: ")],
                                 ["Reviewer-noted missing evidence: " + m for m in missing])
                self.assertIn(f"Reviewer-noted issues: {len(contradictions)} contradictions and "
                              f"{len(missing)} missing-evidence items; accepted with these attached.", quotes)

    def use_criteria_v2(self, version=1, carries=()):
        self.policy.update(schema=standing.POLICY_SCHEMA_V2, version=version,
                           criteria="standing-review-v2", carries=list(carries))
        self.write_private("config/standing-policy.json", self.policy)
        self.findings.update(schema=standing.FINDINGS_SCHEMA_V3, embedded_instructions=[], sparse=False,
                             policy={"policy_id": "routine-docling", "version": version})

    def test_policy_v2_pins_criteria_and_carries_only_earlier_versions(self):
        self.use_criteria_v2(version=3, carries=[{"version": 2, "sha256": "a" * 64}])
        self.write_private("config/standing-findings.json", self.findings)
        loaded = standing.load_policy(self.root, "config/standing-policy.json",
                                      self.review_args()["policy_sha256"])
        self.assertEqual((standing.criteria(loaded), standing.carried(loaded)),
                         ("standing-review-v2", [("routine-docling", 2, "a" * 64)]))
        self.assert_outcome("qualified", "complete-inspected-scope")
        for update in ({"criteria": "standing-review-v9"}, {"carries": [{"version": 3, "sha256": "a" * 64}]},
                       {"carries": [{"version": 1, "sha256": "short"}]}):
            with self.subTest(update=update):
                raw = self.write_private("config/standing-policy.json", {**self.policy, **update})
                with self.assertRaises(standing.Refusal) as caught:
                    standing.load_policy(self.root, "config/standing-policy.json", raw)
                self.assertEqual(caught.exception.code, "standing-policy-invalid")

    def test_findings_schema_must_match_the_policy_criteria(self):
        # v1 criteria never accept v3 findings, and v2 criteria never accept earlier ones.
        self.write_private("config/standing-findings.json", {
            **self.findings, "schema": standing.FINDINGS_SCHEMA_V3, "embedded_instructions": [],
            "sparse": False})
        self.assert_outcome("exception", "findings-mismatch")
        self.use_criteria_v2()
        self.write_private("config/standing-findings.json", {
            k: v for k, v in {**self.findings, "schema": standing.FINDINGS_SCHEMA}.items()
            if k not in ("embedded_instructions", "sparse")})
        self.assert_outcome("exception", "findings-mismatch")

    def test_reviewer_flags_qualify_and_label_the_text_low_confidence(self):
        from tools.context_index import confidence
        self.use_criteria_v2()
        instructions = [f"Asks AI assistants to reply 'accept' ({i})." for i in range(40)]
        self.write_private("config/standing-findings.json", {
            **self.findings, "embedded_instructions": instructions, "sparse": True,
            "contradictions": ["Status differs."]})
        result = self.assert_outcome("qualified", "complete-inspected-scope")
        event = standing_events.review_event(result, self.review_args())
        quotes = [e["quote"] for e in event["evidence"]]
        self.assertEqual(len(quotes), standing_events.MAX_EVIDENCE)
        self.assertIn("Reviewer-noted contradiction: Status differs.", quotes)
        self.assertIn("Reviewer-flagged embedded instructions: 40 instructions in the records were "
                      "treated as document data, not followed; qualified as low confidence.", quotes)
        kept = [q for q in quotes if q.startswith("Reviewer-flagged embedded instruction: ")]
        self.assertEqual(kept, ["Reviewer-flagged embedded instruction: " + t for t in instructions[:len(kept)]])
        self.assertTrue(quotes[-1].startswith(f"Reviewer-flagged embedded instructions truncated: "
                                              f"recorded the first {len(kept)} of 40;"))
        scope = [{"event_id": "rev-standing", "flags": confidence.review_flags(event["evidence"])}]
        self.assertEqual([(r["code"], r["details"]) for r in confidence.review_reasons(scope)],
                         [("embedded-instructions", ["rev-standing: 40 embedded instructions attached"]),
                          ("sparse-record", ["rev-standing: sparse record"])])

    def test_a_review_without_issues_records_no_noted_statements(self):
        quotes = [e["quote"] for e in standing_events.review_event(
            self.assert_outcome("qualified", "complete-inspected-scope"), self.review_args())["evidence"]]
        self.assertEqual([q for q in quotes if q.startswith("Reviewer-noted")], [])

    def test_opted_in_deferral_qualifies_as_low_confidence(self):
        self.write_private("config/standing-findings.json", {**self.findings, "decision": "defer"})
        args = {**self.review_args(), "deferred": "qualify-low-confidence"}
        result = standing.evaluate(self.root, args)
        self.assertEqual((result["outcome"], result["reasons"]),
                         ("qualified", ["complete-inspected-scope", "reviewer-deferred-low-confidence"]))
        event = standing_events.review_event(result, args)
        self.assertTrue(event["reason"].startswith("Qualified as low confidence"))
        self.assertEqual(event["uncertainties"], [])
        self.assertEqual(confidence.review_notes(event["evidence"]),
                         ([], "Compared every page, text, table and warning to the original."))
        # Without the opt-in (or with ``hold``) a deferral stays held, unpublished.
        for mode in (None, "hold"):
            with self.subTest(mode=mode):
                held = self.review_args() if mode is None else {**self.review_args(), "deferred": mode}
                outcome = standing.evaluate(self.root, held)
                self.assertEqual((outcome["outcome"], outcome["reasons"]), ("deferred", ["reviewer-deferred"]))
                self.assertNotIn("deferred", outcome)

    def test_deferral_opt_in_never_rescues_rejection_or_the_other_gates(self):
        args = {**self.review_args(), "deferred": "qualify-low-confidence"}
        partial = deepcopy(self.findings)
        partial["inspected"]["texts"] = []
        for findings, expected in (
                ({**self.findings, "decision": "reject"}, ("rejected", ["reviewer-rejected"])),
                ({**partial, "decision": "defer"}, ("pending-review", ["insufficient-inspected-scope"])),
                ({**self.findings, "decision": "defer", "correction_of": "rev-prior"},
                 ("pending-review", ["independent-review-required"])),
                ({**self.findings, "decision": "defer", "reviewer": "someone-else"},
                 ("exception", ["findings-mismatch"]))):
            with self.subTest(findings=findings):
                self.write_private("config/standing-findings.json", findings)
                args["findings_sha256"] = self.review_args()["findings_sha256"]
                result = standing.evaluate(self.root, args)
                self.assertEqual((result["outcome"], result["reasons"]), expected)
        self.write_private("config/standing-findings.json", {**self.findings, "decision": "defer"})
        args["findings_sha256"] = self.review_args()["findings_sha256"]
        event = self.pending(RENDITION)
        event["event_id"] = "rev-independent"
        events.append(self.root, event_history.bundle([event]), "test-runner", "req-independent", 0,
                      events.status(self.root)["state_token"])
        result = standing.evaluate(self.root, args)
        self.assertEqual((result["outcome"], result["reasons"]), ("pending-review", ["conflicting-review"]))
        with self.assertRaises(standing.Refusal):
            standing.evaluate(self.root, {**args, "deferred": "qualify"})

    def test_issues_never_rescue_incomplete_inspection(self):
        partial = deepcopy(self.findings)
        partial["inspected"]["texts"] = []
        partial["contradictions"] = ["Table total differs."]
        partial["missing_evidence"] = ["Figure 2 is absent."]
        self.write_private("config/standing-findings.json", partial)
        self.assert_outcome("pending-review", "insufficient-inspected-scope")

    def test_issues_never_override_a_conflicting_review(self):
        self.write_private("config/standing-findings.json",
                           {**self.findings, "contradictions": ["Table total differs."]})
        event = self.pending(RENDITION)
        event["event_id"] = "rev-independent"
        events.append(self.root, event_history.bundle([event]), "test-runner", "req-independent", 0,
                      events.status(self.root)["state_token"])
        self.assert_outcome("pending-review", "conflicting-review")

    def test_issues_never_rescue_the_other_gates(self):
        issues = {"contradictions": ["Table total differs."], "missing_evidence": ["Figure 2 is absent."]}
        for update, expected in (({"correction_of": "rev-prior"}, ("pending-review", "independent-review-required")),
                                 ({"new_mapping": True}, ("pending-review", "independent-review-required")),
                                 ({"reviewer": "someone-else"}, ("exception", "findings-mismatch")),
                                 ({"rendition_sha256": "f" * 64}, ("exception", "findings-mismatch"))):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json", {**self.findings, **issues, **update})
                self.assert_outcome(*expected)
        self.write_private("config/standing-findings.json", {**self.findings, **issues})
        self.policy["scope"]["receipts"] = ["D2"]
        self.write_private("config/standing-policy.json", self.policy)
        self.assert_outcome("exception", "prohibited-scope")

    def test_every_issue_is_recorded_or_the_truncation_is_stated(self):
        contradictions = [f"Contradiction {n}." for n in range(50)]
        missing = [f"Missing {n}." for n in range(50)]
        self.write_private("config/standing-findings.json", {
            **self.findings, "contradictions": contradictions, "missing_evidence": missing,
            "unverifiable_references": ["Cites a repo."], "exception_resolution": "Operator note."})
        result = self.assert_outcome("qualified", "complete-inspected-scope")
        event = standing_events.review_event(result, self.review_args())
        quotes = [e["quote"] for e in event["evidence"]]
        self.assertEqual(len(quotes), 30)
        recorded = [q for q in quotes if q.startswith(("Reviewer-noted contradiction: ",
                                                       "Reviewer-noted missing evidence: "))]
        # Items are kept in reported order, contradictions first, verbatim.
        self.assertEqual(recorded, [("Reviewer-noted contradiction: " + c) for c in contradictions]
                         [:len(recorded)])
        truncation = [q for q in quotes if q.startswith("Reviewer-noted issues truncated: ")]
        self.assertEqual(len(truncation), 1)
        self.assertIn(f"first {len(recorded)} of 100", truncation[0])
        # The event stays valid under the review-event contract and is stored.
        self.stage_all()
        self.assertEqual(self.run_stage("review")["status"], "succeeded")

    def test_advisory_references_do_not_block_and_are_counted_in_the_event(self):
        cited = "Relies on a linked analysis document that is not included."
        self.write_private("config/standing-findings.json",
                           {**self.findings, "unverifiable_references": [cited, "Cites a repo."]})
        result = self.assert_outcome("qualified", "complete-inspected-scope")
        quotes = [e["quote"] for e in standing_events.review_event(result, self.review_args())["evidence"]]
        self.assertIn("Reviewer noted 2 advisory references to material outside the rendition.",
                      quotes)
        self.assertNotIn(cited, " ".join(quotes))

    def test_v1_findings_without_advisory_references_stay_valid(self):
        v1 = {k: v for k, v in self.findings.items() if k != "unverifiable_references"}
        for update, expected in (({}, ("qualified", "complete-inspected-scope")),
                                 ({"missing_evidence": ["Section 3 is truncated."]},
                                  ("qualified", "complete-inspected-scope")),
                                 ({"unverifiable_references": []},
                                  ("exception", "invalid-findings"))):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json",
                                   {**v1, "schema": "standing-findings-v1", **update})
                self.assert_outcome(*expected)
        self.write_private("config/standing-findings.json", v1)
        self.assert_outcome("exception", "invalid-findings")

    def test_findings_must_bind_policy_rendition_and_reviewer(self):
        for update in ({"reviewer": "someone-else"}, {"rendition_sha256": "f" * 64},
                       {"policy": {"policy_id": "routine-docling", "version": 9}}):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json", {**self.findings, **update})
                self.assert_outcome("exception", "findings-mismatch")

    def test_eleven_or_more_warnings_qualify_when_each_is_inspected(self):
        self.value["conversion"]["warnings"] = [
            {"code": "low_confidence", "message": f"Field {i}"} for i in range(12)]
        refs = [f"#/warnings/{i}" for i in range(12)]
        with patch.object(standing, "_registered", return_value={"id": "x"}):
            self.raw = self.write_rendition(self.value)
            self.findings["rendition_sha256"] = sha(self.raw)
            self.findings["inspected"]["warnings"] = refs
            self.write_private("config/standing-findings.json", self.findings)
            self.assert_outcome("qualified", "complete-inspected-scope")
            self.findings["inspected"]["warnings"] = refs[:2] + refs[3:]
            self.write_private("config/standing-findings.json", self.findings)
            self.assert_outcome("pending-review", "insufficient-inspected-scope")

    def test_catalog_identity_change_after_extraction_is_not_qualified(self):
        records = catalog.read_records(self.root, receipt="D1")["records"]
        for field, value in (("audience", "human"), ("handling_refs", ["H-changed"])):
            with self.subTest(field=field):
                original = deepcopy(next(r for r in records if r["id"] == "A-input"))
                original["data"][field] = value
                with patch.object(standing.catalog, "receipt_metadata",
                                  side_effect=self.metadata_with(original)):
                    self.assert_outcome("exception", "unregistered-rendition")

    def test_only_an_absent_registration_is_unregistered(self):
        with self.assertRaises(catalog.MissingRecord):
            catalog.receipt_metadata(self.root, ["D999"])
        with patch.object(standing.catalog, "receipt_metadata",
                          side_effect=catalog.MissingRecord("Record missing from authoritative catalog: D1")):
            self.assert_outcome("exception", "unregistered-rendition")
        # A catalog this process can no longer read is never saved as a review outcome.
        with patch.object(standing.catalog, "receipt_metadata", side_effect=catalog.CatalogError(
                "Catalog validator changed after import; restart the reader")):
            with self.assertRaises(standing.CatalogUnavailable):
                self.evaluate()
        real, reads = catalog.read_records, []

        def stale_once(*args, **kwargs):
            reads.append(1)
            if len(reads) == 1:
                raise catalog.StaleState("Stale query state")
            return real(*args, **kwargs)
        with patch.object(standing.catalog, "read_records", side_effect=stale_once):
            self.assertEqual(self.evaluate()["outcome"], "qualified")
        with patch.object(standing.catalog, "read_records",
                          side_effect=catalog.StaleState("Stale query state")):
            with self.assertRaises(standing.Refusal) as caught:
                self.evaluate()
        self.assertEqual((caught.exception.category, caught.exception.code),
                         ("retryable", "catalog-stale-state"))

    def metadata_with(self, original):
        real = catalog.receipt_metadata

        def metadata(root, receipts):
            value = real(root, receipts)
            value["registrations"]["D1"]["artifact"] = original
            return value
        return metadata


class StandingIntakeTests(StandingCase):
    def test_qualified_rendition_is_reviewed_selected_and_published(self):
        self.stage_all()
        self.assertEqual(self.run_stage("review")["status"], "succeeded")
        self.assertEqual(self.run_stage("select")["status"], "succeeded")
        self.assertEqual(self.run_stage("publish")["status"], "succeeded")
        stored = [e for e in self.stored_events() if e["event_id"] == "rev-standing"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["reviewer"], "standing-reviewer")
        self.assertIn("routine-docling", stored[0]["evidence"][0]["quote"])
        child = next(r for r in catalog.read_records(self.root, receipt="D1")["records"]
                     if r["id"] == "A-docling-" + RUN)
        self.assertEqual(child["data"]["coverage"], "indexed")
        with closing(index.readonly(self.root / DB)) as db:
            hit = index.search(db, "Standing phrase", 10, None, root=self.root)["hits"][0]
            self.assertEqual(hit["locator"], RENDITION + "#text-1")
        summary = adapters.availability(self.root, self.publish_stage(), self.evidence("publish"))
        self.assertTrue(summary["available"])

    def publish_stage(self):
        with store.locked(self.root) as c:
            request = json.loads(c.execute("SELECT raw FROM requests").fetchone()[0])
        return next(s for s in request["stages"] if s["id"] == "publish")

    def evidence(self, name):
        with store.locked(self.root) as c:
            return store.summary(c, "req-standing")["stages"][
                [s["id"] for s in store.summary(c, "req-standing")["stages"]].index(name)]["evidence"]

    def test_non_qualified_outcome_blocks_visibly_without_writes(self):
        self.write_private("config/standing-findings.json", {**self.findings, "decision": "defer"})
        self.stage_all()
        result = self.run_stage("review")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["attempts"][-1]["code"], "standing-deferred-reviewer-deferred")
        self.assertEqual(self.stored_events(), [])

    def test_unavailable_route_is_retryable_then_recovers(self):
        findings = (self.root / "config/standing-findings.json").read_bytes()
        self.stage_all()
        (self.root / "config/standing-findings.json").unlink()
        result = self.run_stage("review")
        self.assertEqual((result["status"], result["attempts"][-1]["code"]),
                         ("retryable", "review-route-unavailable"))
        self.write_private("config/standing-findings.json", findings)
        self.assertEqual(self.run_stage("review")["status"], "succeeded")

    def test_crash_after_review_commit_retries_without_duplicates(self):
        self.stage_all()
        original = events.append

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError(5, "lost acknowledgment")
        with patch.object(adapters.events, "append", crash):
            result = self.run_stage("review")
        self.assertEqual(result["status"], "retryable")
        self.assertEqual(len(self.stored_events()), 1)
        result = self.run_stage("review")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.stored_events()), 1)
        self.assertTrue(self.evidence("review")["idempotent"])

    def test_crash_after_selection_commit_retries_without_false_completion(self):
        self.stage_all()
        self.run_stage("review")
        with patch.object(standing_publication, "write_manifest", side_effect=OSError(5, "crash")):
            result = self.run_stage("select")
        self.assertEqual(result["status"], "retryable")
        self.assertEqual(manifest_entry(self.root, "config/selection.json", RENDITION), [])
        self.assertEqual(self.run_stage("select")["status"], "succeeded")
        self.assertEqual(len(manifest_entry(self.root, "config/selection.json",
                                            RENDITION)), 1)

    def test_failed_refresh_stays_not_retrievable(self):
        self.stage_all()
        self.run_stage("review")
        self.run_stage("select")
        with patch.object(adapters.index, "build", side_effect=OSError(5, "crash")):
            result = self.run_stage("publish")
        self.assertEqual(result["status"], "retryable")
        self.assertFalse(adapters.availability(self.root, self.publish_stage(),
                                               {"manifest_sha256": "0" * 64, "metadata": {},
                                                "standing_policy": None})["available"])
        with closing(index.readonly(self.root / DB)) as db:
            self.assertEqual(index.search(db, "Standing phrase", 10, None, root=self.root)["hits"], [])

    def test_stale_policy_or_source_invalidates_publication(self):
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.assertEqual(self.run_stage(name)["status"], "succeeded")
        evidence = self.evidence("publish")
        self.assertTrue(adapters.availability(self.root, self.publish_stage(), evidence)["available"])
        policy = (self.root / "config/standing-policy.json").read_bytes()
        self.policy["status"] = "revoked"
        self.write_private("config/standing-policy.json", self.policy)
        stale = adapters.availability(self.root, self.publish_stage(), evidence)
        self.assertFalse(stale["available"])
        self.assertEqual(stale["reason"], "standing-policy-stale-or-revoked")
        self.write_private("config/standing-policy.json", policy)
        self.assertTrue(adapters.availability(self.root, self.publish_stage(), evidence)["available"])
        (self.root / "sources/input.pdf").write_bytes(b"%PDF-changed-original")
        self.assertFalse(adapters.availability(self.root, self.publish_stage(), evidence)["available"])
        (self.root / RENDITION).write_bytes(self.raw.replace(b"Standing", b"Changed!"))
        self.assertFalse(adapters.availability(self.root, self.publish_stage(), evidence)["available"])

    def test_revoked_policy_blocks_publish(self):
        self.stage_all()
        self.run_stage("review")
        self.run_stage("select")
        self.policy["status"] = "revoked"
        self.write_private("config/standing-policy.json", self.policy)
        result = self.run_stage("publish")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.root / DB).exists() and self.has_hit())

    def has_hit(self):
        with closing(index.readonly(self.root / DB)) as db:
            return bool(index.search(db, "Standing phrase", 10, None, root=self.root)["hits"])

    def test_publication_under_unrelated_policy_after_revocation_is_refused(self):
        self.stage_all()
        self.run_stage("review")
        self.run_stage("select")
        self.policy["status"] = "revoked"
        self.write_private("config/standing-policy.json", self.policy)
        other = {**self.policy, "policy_id": "other-routine", "status": "active",
                 "scope": {"receipts": ["D2"], "audiences": ["source"]}}
        other_sha = self.write_private("config/other-policy.json", other)
        stage_request(self.root, [stage("publication", "standing-publish",
                                        {"policy_path": "config/other-policy.json",
                                         "policy_sha256": other_sha,
                                         "manifest": "config/selection.json", "db": DB},
                                        "publish")], "req-other")
        result = self.run_stage("publish", "req-other")
        self.assertEqual((result["status"], result["attempts"][-1]["code"]),
                         ("blocked", "standing-selection-unauthorized"))
        self.assertFalse(self.has_hit())

    def test_malformed_publication_manifest_blocks_durably(self):
        for renditions in ([None], None):
            with self.subTest(renditions=renditions):
                manifest = self.root / "config/selection.json"
                config = json.loads(manifest.read_bytes())
                manifest.write_text(json.dumps({**config, "renditions": renditions}))
                with self.assertRaises(standing.Refusal) as caught:
                    adapters.standing_publish_guard(self.root, {
                        **self.review_args(), "manifest": "config/selection.json", "db": DB})
                self.assertEqual(caught.exception.code, "standing-selection-unauthorized")

    def test_availability_requires_selections_authorized_by_publish_policy(self):
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.assertEqual(self.run_stage(name)["status"], "succeeded")
        evidence = self.evidence("publish")
        narrowed = deepcopy(self.policy)
        narrowed["scope"]["receipts"] = ["D2"]
        stage_ = deepcopy(self.publish_stage())
        stage_["arguments"]["policy_sha256"] = self.write_private(
            "config/standing-policy.json", narrowed)
        evidence = {**evidence, "standing_policy": {**evidence["standing_policy"],
                                                    "sha256": stage_["arguments"]["policy_sha256"]}}
        stale = adapters.availability(self.root, stage_, evidence)
        self.assertEqual((stale["available"], stale["reason"]),
                         (False, "standing-policy-stale-or-revoked"))

    def test_changed_inputs_between_review_and_select_block(self):
        self.stage_all()
        self.run_stage("review")
        self.write_private("config/standing-findings.json", {**self.findings, "decision": "reject"})
        result = self.run_stage("select")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(manifest_entry(self.root, "config/selection.json", RENDITION), [])

    def test_confidence_never_yields_verification(self):
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.run_stage(name)
        event = next(e for e in self.stored_events() if e["event_id"] == "rev-standing")
        self.assertEqual(event["outcome"], "reviewed")
        self.assertNotIn("verified", json.dumps(event).lower().replace("not verification", ""))
        with closing(index.readonly(self.root / DB)) as db:
            packet = packets.packet(db, self.config | json.loads(
                (self.root / "config/selection.json").read_text()), RENDITION + "#text-1", 60000, root=self.root)
        self.assertEqual(packet["renditions"][RENDITION]["confidence"], {"layout": 0.99})
        self.assertEqual(packet["renditions"][RENDITION]["qualification"]["review_event_ids"],
                         ["rev-standing"])


    def test_retrieval_surfaces_the_issues_a_qualified_document_carries(self):
        issues = {"contradictions": ["Title says 7 MB, body says 8 MB."],
                  "missing_evidence": ["README is cited but absent."]}
        self.write_private("config/standing-findings.json", {**self.findings, **issues})
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.assertEqual(self.run_stage(name)["status"], "succeeded")
        with closing(index.readonly(self.root / DB)) as db:
            packet = packets.packet(db, self.config | json.loads(
                (self.root / "config/selection.json").read_text()), RENDITION + "#text-1", 60000, root=self.root)
        scope = packet["renditions"][RENDITION]["qualification"]["reviewed_scope"]
        self.assertEqual(scope[0]["noted_issues"], [
            "Reviewer-noted issues: 1 contradictions and 1 missing-evidence items; accepted with these attached.",
            "Reviewer-noted contradiction: Title says 7 MB, body says 8 MB.",
            "Reviewer-noted missing evidence: README is cited but absent."])

    def published_packet(self):
        with closing(index.readonly(self.root / DB)) as db:
            return packets.packet(db, self.config | json.loads(
                (self.root / "config/selection.json").read_text()), RENDITION + "#text-1", 60000, root=self.root)

    def test_noted_issues_and_machine_reading_mark_text_low_confidence(self):
        self.write_private("config/standing-findings.json",
                           {**self.findings, "contradictions": ["Title says 7 MB, body says 8 MB."]})
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.assertEqual(self.run_stage(name)["status"], "succeeded")
        piece = next(p for p in self.published_packet()["pieces"] if p["path"] == RENDITION)
        self.assertEqual(piece["confidence"], {"level": "low", "reasons": [
            {"code": "noted-issues", "details": ["rev-standing: 1 contradictions, 0 missing-evidence items attached"]},
            {"code": "machine-ocr", "details": ["docling-pdf"]}]})

    def test_opted_in_deferral_publishes_visibly_marked_low_confidence(self):
        self.write_private("config/standing-findings.json", {**self.findings, "decision": "defer"})
        self.stage_all(deferred="qualify-low-confidence")
        for name in ("review", "select", "publish"):
            self.assertEqual(self.run_stage(name)["status"], "succeeded", name)
        packet = self.published_packet()
        by_path = {p["path"]: p["confidence"] for p in packet["pieces"]}
        self.assertEqual(by_path[RENDITION]["level"], "low")
        self.assertEqual([r["code"] for r in by_path[RENDITION]["reasons"]], ["reviewer-deferred", "machine-ocr"])
        self.assertEqual(by_path[RENDITION]["reasons"][0]["details"],
                         ["rev-standing: Compared every page, text, table and warning to the original."])
        self.assertEqual(by_path["documents/AGENTS.md"], {"level": "normal", "reasons": []})
        self.assertEqual(packet["confidence"]["level"], "low")
        self.assertEqual(list(packet["confidence"]["reasons"]), ["reviewer-deferred", "machine-ocr"])
        self.assertIn("meeting transcript", packet["confidence"]["reasons"]["reviewer-deferred"]["correction"])
        compact = packets.compact_packet(packet)
        self.assertEqual(compact["format"], "qualified-compact-v2")
        self.assertEqual({d["path"]: d["confidence"]["level"] for d in compact["documents"].values()},
                         {RENDITION: "low", "documents/AGENTS.md": "normal"})
        self.assertTrue(all("confidence" not in p for p in compact["pieces"]))
        with closing(index.readonly(self.root / DB)) as db:
            self.assertEqual(index.count_index(db)["confidence"], {
                "contract": "confidence-v1", "normal": 2, "low": 1,
                "low_reasons": {"machine-ocr": 1, "reviewer-deferred": 1}})
            found = index.search(db, "Standing phrase", 10, None, root=self.root)
            self.assertEqual(found["hits"][0]["confidence"],
                             {"level": "low", "reasons": ["reviewer-deferred", "machine-ocr"]})
            self.assertEqual(found["confidence"]["level"], "low")
            excluded = index.search(db, "Standing phrase", 10, None, root=self.root, low_confidence="exclude")
            self.assertEqual((excluded["hits"], excluded["low_confidence_excluded"]), ([], 1))
            self.assertNotIn("absence", excluded)
            self.assertEqual(excluded["filtered"]["classification"], "low-confidence-excluded")
            last = index.search(db, "standing", 50, None, root=self.root, low_confidence="last")
            levels = [hit["confidence"]["level"] for hit in last["hits"]]
            self.assertEqual(levels[-1], "low")
            self.assertEqual(levels, sorted(levels, key=lambda level: level == "low"))
            self.assertIn("normal", levels)
            with self.assertRaises(index.IndexError_):
                index.search(db, "standing", 5, None, root=self.root, low_confidence="hide")

    def test_deferral_opt_in_is_a_contract_checked_stage_argument(self):
        self.stage_all(deferred="qualify-low-confidence")
        with self.assertRaises(contracts.IntakeError):
            self.stage_all("req-other", deferred="hold")

    def test_a_document_without_issues_carries_no_noted_issues(self):
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.run_stage(name)
        with closing(index.readonly(self.root / DB)) as db:
            packet = packets.packet(db, self.config | json.loads(
                (self.root / "config/selection.json").read_text()), RENDITION + "#text-1", 60000, root=self.root)
        self.assertNotIn("noted_issues", packet["renditions"][RENDITION]["qualification"]["reviewed_scope"][0])


class StandingExtractTests(WorkspaceCase):
    """Standing review of strict text renditions inspects every record and warning."""

    def setUp(self):
        super().setUp()
        store.initialize(self.root)
        from tools.extraction import extract
        self.registration("sources/notes.md", b"# Extract phrase\nSecond line\n")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        extract.run(self.root, "register", "D1", "text", "test-runner",
                    catalog.status(self.root)["state_token"])
        self.rendition, self.rendition_sha = prepared["rendition"], prepared["rendition_sha256"]
        events.initialize(self.root)
        self.docs()
        self.policy = {"schema": standing.POLICY_SCHEMA, "policy_id": "routine-text",
                       "version": 1, "status": "active", "reviewer": "standing-reviewer",
                       "scope": {"receipts": ["D1"], "audiences": ["source"]},
                       "budget": {"max_pages": 1, "max_records": 10,
                                  "max_findings_bytes": 64 * 1024},
                       "selection": {"context": ["documents/AGENTS.md"]}}
        self.findings = {"schema": standing.FINDINGS_SCHEMA,
                         "policy": {"policy_id": "routine-text", "version": 1},
                         "rendition": self.rendition, "rendition_sha256": self.rendition_sha,
                         "reviewer": "standing-reviewer", "decision": "accept",
                         "inspected": {"records": [0, 1], "warnings": ["#/payload/warnings/0"]},
                         "explanation": "Compared both lines and the warning to the original.",
                         "contradictions": [], "missing_evidence": [],
                         "unverifiable_references": [], "correction_of": None,
                         "new_mapping": False, "exception_resolution": None}
        self.save("config/standing-policy.json", self.policy)
        self.save("config/standing-findings.json", self.findings)

    def save(self, name, value):
        path = self.root / name
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return sha(raw)

    def args(self):
        return {"policy_path": "config/standing-policy.json",
                "policy_sha256": sha((self.root / "config/standing-policy.json").read_bytes()),
                "rendition": self.rendition, "rendition_sha256": self.rendition_sha,
                "findings_path": "config/standing-findings.json",
                "findings_sha256": sha((self.root / "config/standing-findings.json").read_bytes()),
                "event_id": "rev-standing-text"}

    def outcome(self):
        result = standing.evaluate(self.root, self.args())
        return result["outcome"], result["reasons"]

    def test_complete_record_and_warning_inspection_qualifies(self):
        result = standing.evaluate(self.root, self.args())
        self.assertEqual((result["outcome"], result["content"]), ("qualified", ["text"]))
        self.assertEqual(result["inspected"], {"records": 2, "warnings": 1})

    def test_partial_or_docling_shaped_inspection_does_not_qualify(self):
        for inspected, expected in (
                ({"records": [0], "warnings": ["#/payload/warnings/0"]},
                 ("pending-review", ["insufficient-inspected-scope"])),
                ({"records": [0, 1], "warnings": []},
                 ("pending-review", ["insufficient-inspected-scope"])),
                ({"pages": [], "texts": [], "tables": [], "warnings": []},
                 ("exception", ["findings-mismatch"]))):
            with self.subTest(inspected=inspected):
                self.save("config/standing-findings.json", {**self.findings, "inspected": inspected})
                self.assertEqual(self.outcome(), expected)

    def test_record_budget_applies(self):
        self.policy["budget"]["max_records"] = 1
        self.save("config/standing-policy.json", self.policy)
        self.assertEqual(self.outcome(), ("exception", ["budget-exceeded"]))

    def test_review_select_publish_records_qualification(self):
        review = self.args()
        stage_request(self.root, [
            stage("review", "standing-review", {**review, "request_id": "req-text-review"}, "review"),
            stage("publication", "standing-select", {**review, "request_id": "req-text-select",
                                                     "manifest": "config/selection.json"}, "select"),
            stage("publication", "standing-publish",
                  {"policy_path": review["policy_path"], "policy_sha256": review["policy_sha256"],
                   "manifest": "config/selection.json", "db": DB}, "publish")], "req-text")
        for name in ("review", "select", "publish"):
            result = runner.run(self.root, "req-text", name)
            self.assertEqual(next(s for s in result["stages"] if s["id"] == name)["status"],
                             "succeeded", name)
        manifest = json.loads((self.root / "config/selection.json").read_text())
        self.assertEqual(manifest["renditions"][0]["format"], "text")
        self.assertEqual(manifest["renditions"][0]["review_event_ids"], ["rev-standing-text"])
        with closing(index.readonly(self.root / DB)) as db:
            hit = index.search(db, "Extract phrase", 10, None, root=self.root)["hits"][0]
            packet = packets.packet(db, manifest, hit["locator"], 60000, root=self.root)
        qualified = packet["renditions"][self.rendition]["qualification"]
        self.assertEqual(qualified["review_event_ids"], ["rev-standing-text"])
        self.assertEqual(qualified["reviewed_content"], ["text"])

    def test_text_renditions_are_normal_and_rendition_kinds_map_to_reasons(self):
        review = self.args()
        stage_request(self.root, [
            stage("review", "standing-review", {**review, "request_id": "req-text-review"}, "review"),
            stage("publication", "standing-select", {**review, "request_id": "req-text-select",
                                                     "manifest": "config/selection.json"}, "select")],
                      "req-text")
        for name in ("review", "select"):
            runner.run(self.root, "req-text", name)
        self.config = json.loads((self.root / "config/selection.json").read_text())
        self.build()

        def label():
            with closing(index.readonly(self.root / DB)) as db:
                return index.search(db, "Extract phrase", 10, None, root=self.root)["hits"][0]["confidence"]
        self.assertEqual(label(), {"level": "normal", "reasons": []})
        # The seam a converter or OCR kind uses: one RENDITION_REASONS entry, then a rebuild.
        with patch.dict(confidence.RENDITION_REASONS, {"text": "converted-format"}):
            with closing(index.readonly(self.root / DB)) as db:
                self.assertEqual(index.freshness(db, self.root, self.manifest, self.db,
                                                 *index.load_manifest(self.manifest, self.root)),
                                 ["metadata changed: rendition_identity"])
            self.build()
            self.assertEqual(label(), {"level": "low", "reasons": ["converted-format"]})

    def test_open_correction_hook_adds_a_reason_to_an_existing_label(self):
        low = confidence.with_reason(confidence.label(), "open-correction", ["correction C-1 is open"])
        self.assertEqual(low, {"level": "low", "reasons": [
            {"code": "open-correction", "details": ["correction C-1 is open"]}]})
        merged = confidence.with_reason(confidence.label([confidence.reason("machine-ocr", ["docling-pdf"])]),
                                        "noted-issues", ["rev-1: 1 contradictions"])
        self.assertEqual([r["code"] for r in merged["reasons"]], ["noted-issues", "machine-ocr"])
        with self.assertRaises(ValueError):
            confidence.reason("unsure")

    def test_an_index_without_the_confidence_table_is_stale(self):
        self.build()
        import sqlite3
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("DROP TABLE document_confidence")
            db.commit()
        with closing(index.readonly(self.db)) as db:
            self.assertIn("confidence schema missing; run build", index.freshness(
                db, self.root, self.manifest, self.db, *index.load_manifest(self.manifest, self.root)))
            self.assertNotIn("confidence", index.count_index(db))

    def test_text_selection_with_unknown_review_event_is_refused(self):
        self.config["renditions"] = [{"path": self.rendition, "sha256": self.rendition_sha,
                                      "receipt_id": "D1", "format": "text",
                                      "context": ["documents/AGENTS.md"],
                                      "review_event_ids": ["rev-missing"]}]
        child = next(r for r in catalog.read_records(self.root, receipt="D1")["records"]
                     if r["id"].startswith("A-extract-"))
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="index-text",
                       expected_state=catalog.status(self.root)["state_token"], reason="Synthetic")
        with self.assertRaisesRegex(index.IndexError_, "missing or superseded"):
            self.build()


if __name__ == "__main__":
    unittest.main()
