"""Standing-policy review, qualification and publication through durable intake."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index
from tools.extraction import docling_bridge
from tools.review_events import event_store as events
from tools.intake import adapters, runner, standing, store

import test_kit
from test_intake_adapters import stage, stage_request

RUN = "a" * 64
RENDITION = f"data/extractions/.local/D1/{RUN}/extraction.json"
DB = "tools/context_index/.local/index.sqlite3"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class StandingCase(test_kit.WorkspaceCase):
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
                         "contradictions": [], "missing_evidence": [], "correction_of": None,
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

    def stage_all(self, correlation="req-standing"):
        review = self.review_args()
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
            return events.inspect(connection, self.root)[1]

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
        with patch.object(standing, "_registered", return_value=({"id": "x"}, {})):
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
        with patch.object(standing, "_registered", return_value=({"id": "x"}, {})):
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
        event = standing.review_event(result, self.review_args())
        mutate(event)
        event["evidence"] = [{"kind": "agent_statement", "pointer": None,
                              "quote": "Independent review outside the standing policy."}]
        state = events.status(self.root)
        events.append(self.root, events.bundle([event]), "standing-reviewer", "req-other",
                      state["revision"], state["state_token"])
        self.assertIsNone(standing.bound_event(self.root, result, self.review_args()))
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
        events.append(self.root, events.bundle([event]), "test-runner", "req-independent", 0,
                      events.status(self.root)["state_token"])
        self.assert_outcome("pending-review", "conflicting-review")

    def test_rejected_deferred_and_unresolved(self):
        for update, expected in (({"decision": "reject"}, ("rejected", "reviewer-rejected")),
                                 ({"decision": "defer"}, ("deferred", "reviewer-deferred")),
                                 ({"contradictions": ["Table total differs."]},
                                  ("pending-review", "unresolved-evidence"))):
            with self.subTest(update=update):
                self.write_private("config/standing-findings.json", {**self.findings, **update})
                self.assert_outcome(*expected)

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
        with patch.object(standing, "_registered", return_value=({"id": "x"}, {})):
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
            hit = index.search(db, "Standing phrase", 10, None)["hits"][0]
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
        with patch.object(standing, "write_manifest", side_effect=OSError(5, "crash")):
            result = self.run_stage("select")
        self.assertEqual(result["status"], "retryable")
        self.assertEqual(standing.manifest_entry(self.root, "config/selection.json", RENDITION), [])
        self.assertEqual(self.run_stage("select")["status"], "succeeded")
        self.assertEqual(len(standing.manifest_entry(self.root, "config/selection.json",
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
            self.assertEqual(index.search(db, "Standing phrase", 10, None)["hits"], [])

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
            return bool(index.search(db, "Standing phrase", 10, None)["hits"])

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
        self.assertEqual(standing.manifest_entry(self.root, "config/selection.json", RENDITION), [])

    def test_confidence_never_yields_verification(self):
        self.stage_all()
        for name in ("review", "select", "publish"):
            self.run_stage(name)
        event = next(e for e in self.stored_events() if e["event_id"] == "rev-standing")
        self.assertEqual(event["outcome"], "reviewed")
        self.assertNotIn("verified", json.dumps(event).lower().replace("not verification", ""))
        with closing(index.readonly(self.root / DB)) as db:
            packet = index.packet(db, self.config | json.loads(
                (self.root / "config/selection.json").read_text()), RENDITION + "#text-1", 60000)
        self.assertEqual(packet["renditions"][RENDITION]["confidence"], {"layout": 0.99})
        self.assertEqual(packet["renditions"][RENDITION]["qualification"]["review_event_ids"],
                         ["rev-standing"])


if __name__ == "__main__":
    unittest.main()
