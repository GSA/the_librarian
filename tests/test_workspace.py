"""Structural contracts against this toolkit's actual documentation, never a seeded corpus."""

from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape
from zipfile import ZipFile

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.catalog import storage
from tools.context_index import compact_records, integrity
from tools.context_index import context_index as index
from tools.context_index import packets
from tools.context_index import proof_cache
from tools.context_index import qualification
from tools.diagnostics import retrieval_log as diagnostics
from tools.extraction import docling_bridge, extract
from tools.review_events import event_reader
from tools.review_events import event_history
from tools.review_events import event_store as events
from tools.review_events import review_events as contract
from tests.support import ROOT, WorkspaceCase, clone_release


class DiscoveryTests(unittest.TestCase):
    def test_workspace_scenarios_are_not_inherited_by_absence_tests(self):
        def flatten(suite):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    yield from flatten(test)
                else:
                    yield test.id()

        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        ids = list(flatten(suite))
        workspace = {name.rsplit(".", 1)[-1] for name in ids if ".WorkspaceTests." in name}
        absence = {name.rsplit(".", 1)[-1] for name in ids if ".AbsenceClassificationTests." in name}
        self.assertFalse(workspace & absence)
        worker_modules = {
            name.rsplit(".", 2)[0] for name in ids
            if "docling_worker_contract_tests." in name
        }
        self.assertEqual(worker_modules, {"docling_worker_contract_tests"})


class StoreIdentity(unittest.TestCase):
    def test_new_application_ids_and_superseded_refusal(self):
        self.assertEqual(storage.APPLICATION_ID, 0x4C434154)
        self.assertEqual(event_history.APPLICATION_ID, 0x4C524556)
        self.assertEqual(index.APPLICATION_ID, 0x4C435458)
        for module, old, message in (
                (catalog, 0x46434154, "Superseded FCAT"),
                (events, 0x46524556, "Superseded FREV")):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                module.initialize(root)
                database = module.locations(root)[1]
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(f"PRAGMA application_id={old}")
                with self.assertRaisesRegex(Exception, message + ".*migration or re-initialization"):
                    module.status(root)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA application_id=0x46435458")
            with self.assertRaisesRegex(Exception, "Superseded FCTX.*migration or re-initialization"):
                index.readonly(database)


class WorkspaceTests(WorkspaceCase):
    def test_empty_initialization(self):
        self.assertEqual(catalog.initialize(self.root)["records"], 0)
        self.assertEqual(events.initialize(self.root)["events"], 0)
        self.assertEqual(self.build()["documents"], 0)
        with closing(index.readonly(self.db)) as db:
            self.assertEqual(index.search(db, "documentation", 5, None, root=self.root)["hits"], [])
        self.assertEqual(event_reader.read_snapshot(self.root).query()["records"], [])

    def test_exact_packets_and_compaction(self):
        self.docs()
        with closing(index.readonly(self.db)) as db:
            packet = packets.packet(db, self.config, "documents/README.md#interfaces", 60000, root=self.root)
            self.assertIn("documents/AGENTS.md", {p["path"] for p in packet["pieces"]})
            for piece in packet["pieces"]:
                raw = (self.root / piece["path"]).read_bytes()
                lines = raw.decode().splitlines(keepends=True)
                self.assertEqual(piece["source_hash"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(piece["text"], "".join(lines[piece["line_start"]-1:piece["line_end"]]))
            compact = packets.compact_packet(packet)
            self.assertEqual([p["text"] for p in compact["pieces"]], [p["text"] for p in packet["pieces"]])
            many = packets.packet_many(db, self.config,
                                     ["documents/README.md#interfaces", "documents/AGENTS.md"], 60000, root=self.root)
            self.assertEqual(len(many["requests"]), 2)
            with self.assertRaises(index.IndexError_):
                packets.packet(db, self.config, "documents/README.md", 1, root=self.root)
            with self.assertRaises(index.IndexError_):
                packets.packet_many(db, self.config, [], 60000, root=self.root)

    def test_missing_context_refuses_build(self):
        self.docs()
        before = self.db.read_bytes()
        self.config["context_rules"]["documents/README.md"] = ["documents/AGENTS.md#not-present"]
        with self.assertRaises(index.IndexError_):
            self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_refusing_publication_guard_preserves_prior_index(self):
        self.docs()
        before = self.db.read_bytes()
        config, checksum = index.load_manifest(self.manifest, self.root)
        def refuse():
            raise index.IndexError_("Caller refused publication")
        with index.publication_lock(self.db), self.assertRaises(index.IndexError_):
            index.build(self.root, self.manifest, self.db, config, checksum,
                        publication_guard=refuse)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(list(self.db.parent.glob(f".{self.db.name}.build-*")))

    def test_stale_content_detected(self):
        self.docs()
        (self.root / "documents/AGENTS.md").write_bytes((ROOT / "README.md").read_bytes())
        config, digest = index.load_manifest(self.manifest, self.root)
        with closing(index.readonly(self.db)) as db:
            self.assertTrue(index.freshness(db, self.root, self.manifest, self.db, config, digest))

    def test_paths_and_external_dictionary(self):
        self.assertFalse(index.eligible("../README.md"))
        self.assertFalse(index.eligible(".local/database.md"))
        self.assertFalse(policy.allowed("sources/.git/config", "original_roots"))
        with patch.dict(policy.SETTINGS, {"excluded_name_patterns": [r"^README\.md$"]}):
            self.assertFalse(index.eligible("documents/README.md"))
        (self.root / "documents").mkdir()
        (self.root / "documents/README.md").symlink_to(ROOT / "README.md")
        with self.assertRaises(index.IndexError_):
            index.source_path(self.root, "documents/README.md")

    def test_catalog_retry_state_and_backup(self):
        records, result = self.registration()
        retry = catalog.submit(self.root, records, actor="test-runner", request_id="register-input",
                               expected_state="outdated", reason="Structural contract test")
        self.assertTrue(retry["idempotent"])
        with self.assertRaises(catalog.CatalogError):
            catalog.submit(self.root, records, actor="test-runner", request_id="different",
                           expected_state="outdated", reason="Structural contract test")
        packet = catalog.read_records(self.root, receipt="D1")
        self.assertEqual(compact_records.expand_catalog(compact_records.compact_catalog(packet)), packet)
        # Initialisation already took today's snapshot, so the write and its retry take none.
        self.assertIsNone(result["backup"])
        self.assertIsNone(retry["backup"])
        snapshot = Path(catalog.backup(self.root)["snapshot"])
        self.assertEqual(catalog.verify_backup(snapshot), result["state"])
        restored = self.root / "restored"
        restored.mkdir()
        catalog.restore(restored, snapshot)
        self.assertEqual(catalog.status(restored)["state_token"], result["state_token"])

    def test_batched_receipt_reads_match_one_receipt_reads(self):
        records, first = self.registration()
        raw = b"second registration\n"
        (self.root / "sources/two.txt").write_bytes(raw)
        second = [
            {"id": "S2", "kind": "source", "data": {"category": "artifact", "provenance": []}},
            {"id": "D2", "kind": "receipt", "data": {
                "source_ids": ["S2"], "artifact_id": "A-two", "provenance": []}},
            {"id": "A-two", "kind": "artifact", "data": {
                **records[2]["data"], "path": "sources/two.txt", "receipt_id": "D2",
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}},
            # Touches both receipts' artifacts, so it belongs to each scope.
            {"id": "R-two", "kind": "relationship", "data": {
                "from_id": "A-two", "to_id": "A-input", "relation": "version-of",
                "scope": "Synthetic.", "basis": "reported", "provenance": []}},
        ]
        written = catalog.submit(self.root, second, actor="test-runner", request_id="register-two",
                                 expected_state=first["state_token"], reason="Structural contract test")
        receipts = ["D1", "D2", "D1", "D9"]
        for limit in (100, 2):
            batch = catalog.read_records_by_receipt(self.root, receipts, limit=limit,
                                                    expected_state=written["state_token"])
            self.assertEqual(list(batch), ["D1", "D2", "D9"])
            for receipt in batch:
                self.assertEqual(batch[receipt], catalog.read_records(self.root, receipt=receipt, limit=limit))
            if limit == 100:
                self.assertIn("R-two", {r["id"] for r in batch["D1"]["records"]})
                self.assertIn("R-two", {r["id"] for r in batch["D2"]["records"]})
        self.assertEqual(catalog.read_records_by_receipt(self.root, ["D9"])["D9"]["records"], [])
        with self.assertRaises(catalog.CatalogError):
            catalog.read_records_by_receipt(self.root, ["D1"], expected_state="outdated")
        # One snapshot yields exactly what the two separate reads yield, with a single state.
        metadata, records = catalog.receipt_snapshot(self.root, ["D1", "D2"], limit=100)
        self.assertEqual(metadata, catalog.receipt_metadata(self.root, ["D1", "D2"]))
        self.assertEqual(records, catalog.read_records_by_receipt(self.root, ["D1", "D2"], limit=100))
        self.assertEqual(metadata["state"], records["D1"]["state"])
        with self.assertRaises(catalog.MissingRecord):
            catalog.receipt_snapshot(self.root, ["D9"])

    def test_native_review_correction_retry_and_restore(self):
        self.registration()
        state = events.initialize(self.root)
        event = self.pending()
        result = events.append(self.root, event_history.bundle([event]), "test-runner", "req-pending",
                               0, state["state_token"])
        retry = events.append(self.root, event_history.bundle([event]), "test-runner", "req-pending",
                              0, state["state_token"])
        self.assertTrue(retry["idempotent"])
        updated = deepcopy(event)
        updated.update(event_id="rev-correction", supersedes=[event["event_id"]],
                       correction_reason="Clarify pending reason without asserting review.",
                       reason="This remains pending after the structural test.",
                       recorded_at=(datetime.fromisoformat(event["recorded_at"]) + timedelta(seconds=1)).isoformat())
        result = events.append(self.root, event_history.bundle([updated]), "test-runner", "req-correction",
                               1, result["state_token"])
        snapshot = event_reader.read_snapshot(self.root)
        self.assertEqual(snapshot.query()["records"][0]["event"]["event_id"], "rev-correction")
        self.assertEqual(snapshot.query(include_history=True)["count"], 2)
        with self.assertRaises(contract.ReviewError):
            snapshot.query(after=1)
        with self.assertRaises(contract.ReviewError):
            snapshot.query(expected_state="0" * 64)
        with self.assertRaises(contract.ReviewError):
            snapshot.query(max_chars=1)
        with closing(events.connect(events.locations(self.root)[1], write=True)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events")
        destination = self.root / "recovered"
        destination.mkdir()
        self.assertIsNone(result["backup"])
        events.restore(destination, Path(events.backup(self.root)["snapshot"]))
        self.assertEqual(events.status(destination)["state_token"], result["state_token"])

    def test_invalid_native_event_rejected(self):
        events.initialize(self.root)
        event = self.pending()
        event["counts"]["reviewed"] = 1
        with self.assertRaises(contract.ReviewError):
            contract.validate(event_history.bundle([event]), schema=event_history.store_schema(), stored=True)
        event = self.pending()
        event["provenance"]["origin"] = "unattributed"
        with self.assertRaises(contract.ReviewError):
            events.prepare_submission(event_history.bundle([event]), self.root)

    def selected_text(self, raw=b"first\r\n\xce\xb2\nlast", name="sources/notes.txt", fmt="text"):
        self.registration(name, raw)
        prepared = extract.run(self.root, "prepare", "D1", fmt)
        extract.run(self.root, "verify", "D1", fmt)
        extract.run(self.root, "register", "D1", fmt, "test-runner",
                    catalog.status(self.root)["state_token"])
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["kind"] == "artifact" and record["data"]["role"] == "generated-rendition")
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-text-rendition",
                       expected_state=catalog.status(self.root)["state_token"],
                       reason="Explicit synthetic text selection")
        self.docs()
        self.config["renditions"] = [{"path": prepared["rendition"],
            "sha256": prepared["rendition_sha256"], "receipt_id": "D1", "format": fmt,
            "context": ["documents/AGENTS.md"]}]
        self.build()
        return prepared

    def test_selected_mime_html_is_searchable_as_decoded_text_not_raw_markup(self):
        raw = (b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n"
               b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
               b"<html><body><h1>Fund Codes</h1><p class=3D\"rule\">A fund code must be exactly "
               b"four=\r\n characters.</p></body></html>\r\n")
        prepared = self.selected_text(raw, "sources/page.doc", "mhtml")
        with closing(index.readonly(self.db)) as db:
            found = index.search(db, "exactly four characters", 10, None, root=self.root)
            self.assertEqual(found["hits"][0]["locator"], prepared["rendition"] + "#line-2")
            self.assertEqual(index.search(db, "class", 10, None, root=self.root)["hits"], [])
            packet = packets.packet(db, self.config, prepared["rendition"] + "#line-2", 60000, root=self.root)
            self.assertEqual(packet["renditions"][prepared["rendition"]]["method"]["name"],
                             "mhtml-html-text")

    def test_mime_html_rendition_refuses_a_payload_that_differs_from_a_fresh_conversion(self):
        from tools.extraction import mhtml
        raw = (b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               b"<p>one</p>\r\n")
        api = index.rendition_api()
        payload = mhtml.payload(raw)
        payload["lines"][0]["text"] = "two\n"
        with self.assertRaises(index.IndexError_):
            index.renditions.validate_mhtml(payload, raw, api)

    def test_selected_text_round_trips_exact_lines_and_byte_locations(self):
        prepared = self.selected_text()
        with closing(index.readonly(self.db)) as db:
            rows = db.execute("SELECT locator, payload FROM rendition_locations "
                              "ORDER BY locator").fetchall()
            self.assertEqual([row[0].rsplit("#", 1)[1] for row in rows],
                             ["line-1", "line-2", "line-3"])
            self.assertEqual(json.loads(rows[0][1]), {
                "json_pointer": "/payload/lines/0", "original_line": 1,
                "byte_start": 0, "byte_end": 7, "terminator": "crlf",
            })
            packet = packets.packet(db, self.config, prepared["rendition"] + "#line-2", 60000, root=self.root)
            piece = next(piece for piece in packet["pieces"] if piece["locator"].endswith("#line-2"))
            self.assertIn('"text": "β\\n"', piece["text"])
            metadata = packet["renditions"][prepared["rendition"]]
            self.assertEqual(metadata["format"], "text")
            self.assertEqual(metadata["bom_bytes"], 0)
            found = index.search(db, "β", 10, None, root=self.root)
            self.assertEqual(found["hits"][0]["locator"], prepared["rendition"] + "#line-2")
            self.assertIsInstance(found["hits"][0]["line_start"], int)
            self.assertGreaterEqual(found["hits"][0]["line_end"], found["hits"][0]["line_start"])
            self.assertEqual(found["hits"][0]["line_basis"],
                             "extraction-json; source_location identifies original line or XML element")

    def test_selected_text_unicode_separators_keep_physical_json_line_citations(self):
        content = "a\u0085b\u2028c\u2029d"
        prepared = self.selected_text(content.encode("utf-8"))
        locator = prepared["rendition"] + "#line-1"
        with closing(index.readonly(self.db)) as db:
            packet = packets.packet(db, self.config, locator, 60000, root=self.root)
        piece = next(piece for piece in packet["pieces"] if piece["locator"] == locator)
        physical = (self.root / prepared["rendition"]).read_text().splitlines(keepends=True)
        # splitlines() is intentionally unsuitable here; derive physical JSON
        # lines solely from the encoder's LF bytes.
        raw_text = (self.root / prepared["rendition"]).read_text()
        physical = [part + "\n" for part in raw_text.split("\n")[:-1]]
        self.assertEqual(piece["text"], "".join(physical[piece["line_start"] - 1:piece["line_end"]]))
        self.assertIn(content, piece["text"])
        self.assertEqual(piece["line_end"] - piece["line_start"], 6)

    def test_text_rendition_refuses_inconsistent_byte_mapping(self):
        api = index.rendition_api()
        payload = extract.text(b"one\r\ntwo")
        payload["lines"][1]["byte_start"] = 4
        with self.assertRaisesRegex(index.IndexError_, "line mapping"):
            index.renditions.validate_text_mapping(payload, b"one\r\ntwo", api)

        for label, mutate in (
            ("truncated", lambda payload: payload["lines"].pop()),
            ("final-state", lambda payload: payload.update(ends_with_terminator=True)),
        ):
            payload = extract.text(b"one\ntwo")
            mutate(payload)
            with self.subTest(label=label), self.assertRaisesRegex(index.IndexError_, "mapping"):
                index.renditions.validate_text_mapping(payload, b"one\ntwo", api)

        payload = extract.text(b"one\ntwo")
        payload.update(bom_bytes=3)
        for record in payload["lines"]:
            record["byte_start"] += 3
            record["byte_end"] += 3
        with self.assertRaisesRegex(index.IndexError_, "BOM declaration"):
            index.renditions.validate_text_mapping(payload, b"one\ntwo", api)

        for label, lines in (
            ("merged-lines", [{"line": 1, "text": "a\nb\n", "terminator": "lf",
                               "byte_start": 0, "byte_end": 4}]),
            ("split-crlf", [{"line": 1, "text": "a\r", "terminator": "cr",
                             "byte_start": 0, "byte_end": 2},
                            {"line": 2, "text": "\n", "terminator": "lf",
                             "byte_start": 2, "byte_end": 3}]),
        ):
            payload = extract.text(b"a\nb\n" if label == "merged-lines" else b"a\r\n")
            payload["lines"] = lines
            with self.subTest(label=label), self.assertRaisesRegex(index.IndexError_, "physical line mapping"):
                index.renditions.validate_text_mapping(
                    payload, b"a\nb\n" if label == "merged-lines" else b"a\r\n", api)

        payload = extract.text(b"one\ntwo")
        payload["lines"][0]["text"] = "six\n"
        with self.assertRaisesRegex(index.IndexError_, "mapping"):
            index.renditions.validate_text_mapping(payload, b"one\ntwo", api)
        payload["lines"][0]["text"] = None
        with self.assertRaisesRegex(index.IndexError_, "field types"):
            index.renditions.validate_text_mapping(payload, b"one\ntwo", api)

    def docling_qualification_fixture(self, *, tables=False):
        path = "data/extractions/.local/D1/" + "a" * 64 + "/extraction.json"
        sha = "b" * 64
        selection = {"path": path, "sha256": sha, "receipt_id": "D1", "format": "docling",
                     "context": ["documents/README.md"], "review_event_ids": ["rev-exact"]}
        value = {"schema": "docling-rendition-v1", "source": {
            "receipt_id": "D1", "source_ids": ["S1"], "path": "sources/input.pdf",
            "sha256": "c" * 64}, "conversion": {
                "schema": "docling-rendition-v1", "format": "pdf",
                "document": {"texts": [], "tables": [{}] if tables else []}, "pages": []}}
        event = self.pending(path)
        event.update(event_id="rev-exact", outcome="reviewed", reviewer="test-reviewer",
                     reviewed_on="2026-10-28")
        event["source"].update(parent_locator="sources/input.pdf",
                               checksum={"sha256": sha, "basis": "reported"})
        event["scope"].update(content=["text", "tables"] if tables else ["text"],
                              description="Complete exact Docling rendition review.")
        event["evidence"] = [{"kind": "agent_statement", "pointer": None,
                              "quote": "Reviewed the complete exact retained rendition."}]
        event["uncertainties"] = []
        event["counts"].update(reviewed=1, pending=0)
        return selection, value, event

    def test_rendition_selection_limit_is_shared_and_above_ten(self):
        api = index.rendition_api()
        api.validate_context = lambda context: None
        api.eligible = lambda path: True
        api.canonical_locator = lambda value: value
        def selections(count):
            return [{"path": f"data/extractions/.local/D{n + 1}/{'a' * 64}/extraction.json",
                     "sha256": "b" * 64, "receipt_id": f"D{n + 1}", "format": "text",
                     "context": ["documents/context.md"]} for n in range(count)]
        self.assertGreater(index.renditions.MAX_SELECTIONS, 10)
        index.renditions.validate({"renditions": selections(11)}, api, root=self.root)
        with self.assertRaises(index.IndexError_):
            index.renditions.validate(
                {"renditions": selections(index.renditions.MAX_SELECTIONS + 1)}, api, root=self.root)

    def test_strict_docling_qualification_accepts_only_active_exact_file_review(self):
        api = index.rendition_api()
        selection, value, event = self.docling_qualification_fixture(tables=True)
        snapshot = event_reader.Snapshot({"state_token": "d" * 64}, [event], {})
        result = qualification.docling(selection, value, {}, snapshot, api)
        self.assertEqual(result["policy_version"], qualification.POLICY_VERSION)
        self.assertEqual(result["review_event_ids"], ["rev-exact"])
        self.assertEqual(result["review_state"], "d" * 64)
        self.assertEqual(result["reviewed_content"], ["tables", "text"])
        text_event = deepcopy(event)
        text_event["scope"]["content"] = ["text"]
        table_event = deepcopy(event)
        table_event["event_id"] = "rev-table"
        table_event["scope"]["content"] = ["tables"]
        split_selection = deepcopy(selection)
        split_selection["review_event_ids"] = ["rev-exact", "rev-table"]
        split = qualification.docling(split_selection, value, {}, event_reader.Snapshot(
            {"state_token": "d" * 64}, [text_event, table_event], {}), api)
        self.assertEqual(split["reviewed_content"], ["tables", "text"])

        def assert_refused(label, mutate=None, *, events_=None, successors=None):
            selected, envelope, candidate = self.docling_qualification_fixture(tables=True)
            if mutate:
                mutate(selected, envelope, candidate)
            history = [candidate] if events_ is None else events_
            with self.subTest(label=label), self.assertRaises(index.IndexError_):
                qualification.docling(selected, envelope, {}, event_reader.Snapshot(
                    {"state_token": "e" * 64}, history, successors or {}), api)

        assert_refused("original-only", lambda s, v, e: e["source"].update(
            locator=v["source"]["path"], parent_locator=None,
            checksum={"sha256": v["source"]["sha256"], "basis": "reported"}))
        assert_refused("wrong-receipt", lambda s, v, e: e["source"].update(receipt_id="D2"))
        assert_refused("wrong-source", lambda s, v, e: e["source"].update(source_id="S2"))
        assert_refused("wrong-path", lambda s, v, e: e["source"].update(locator="other.json"))
        assert_refused("wrong-hash", lambda s, v, e: e["source"].update(
            checksum={"sha256": "f" * 64, "basis": "reported"}))
        assert_refused("partial", lambda s, v, e: e.update(outcome="partial"))
        assert_refused("uncertain", lambda s, v, e: e.update(
            uncertainties=["Table review completeness is unresolved."]))
        assert_refused("ranged", lambda s, v, e: e["scope"].update(
            extent="selected_ranges", unit="page", coordinates="one_based_inclusive",
            ranges=[{"start": 1, "end": 1}]))
        assert_refused("narrow-defined-scope", lambda s, v, e: e["scope"].update(
            description="Only selected paragraphs of the exact retained rendition."))
        assert_refused("missing-table-scope", lambda s, v, e: e["scope"].update(content=["text"]))
        assert_refused("missing", events_=[])
        _, _, superseded = self.docling_qualification_fixture(tables=True)
        assert_refused("superseded", events_=[superseded], successors={"rev-exact": "rev-new"})

    def selected_docling(self, *, missing_page=False):
        raw_source = b"%PDF-synthetic-structural-test"
        records, _ = self.registration("sources/input.pdf", raw_source)
        run = "a" * 64
        path = f"data/extractions/.local/D1/{run}/extraction.json"
        source = {"receipt_id": "D1", "artifact_id": "A-input", "source_ids": ["S1"],
                  "path": "sources/input.pdf", "sha256": hashlib.sha256(raw_source).hexdigest(),
                  "bytes": len(raw_source), "audience": "source", "handling_refs": []}
        bbox = {"l": 1, "t": 2, "r": 80, "b": 40, "coord_origin": "TOPLEFT"}
        text_item = {"self_ref": "#/texts/0", "label": "text", "text": "Reviewed phrase",
                     "prov": [{"page_no": 2 if missing_page else 1, "bbox": bbox}]}
        table = {"self_ref": "#/tables/0", "label": "table",
                 "prov": [{"page_no": 1, "bbox": bbox}], "data": {
                     "table_cells": [{"text": "Cell", "start_row_offset_idx": 0,
                                      "start_col_offset_idx": 0, "end_row_offset_idx": 1,
                                      "end_col_offset_idx": 1}], "num_rows": 1, "num_cols": 1}}
        value = {"schema": "docling-rendition-v1", "run_id": run,
                 "extractor": {"schema_version": 1, "label": "synthetic-docling"},
                 "source": source, "boundary": "Machine rendition; exact review still required.",
                 "conversion": {"schema": "docling-conversion-v1", "format": "pdf",
                                "document": {"texts": [text_item], "tables": [table]},
                                "pages": [{"page_no": 1, "width": 100, "height": 100,
                                           "textless": False}],
                                "warnings": [{"code": "low_confidence", "message": "Synthetic warning"}],
                                "confidence": {"layout": 0.75},
                                "omissions": ["generated_page_images"]}}
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        rendition = self.root / path
        rendition.parent.mkdir(parents=True)
        rendition.write_bytes(raw)
        rendition.chmod(0o600)
        registered_value = deepcopy(value)
        registered_value["canonical_bytes"] = raw
        catalog.submit(self.root, docling_bridge.registration_records(
            self.root, records[2], rendition, registered_value), actor="test-runner",
            request_id="register-docling", expected_state=catalog.status(self.root)["state_token"],
            reason="Synthetic Docling registration")
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["id"] == "A-docling-" + run)
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-docling",
                       expected_state=catalog.status(self.root)["state_token"],
                       reason="Explicit synthetic Docling selection")
        event = self.pending(path)
        event.update(event_id="rev-exact", outcome="reviewed", reviewer="test-reviewer",
                     reviewed_on=datetime.now(timezone.utc).date().isoformat())
        event["source"].update(parent_locator=source["path"],
                               checksum={"sha256": hashlib.sha256(raw).hexdigest(),
                                         "basis": "reported"})
        event["scope"].update(content=["text", "tables"],
                              description="Complete exact Docling rendition review.")
        event["evidence"] = [{"kind": "agent_statement", "pointer": None,
                              "quote": "Reviewed the complete exact retained rendition."}]
        event["uncertainties"] = []
        event["counts"].update(reviewed=1, pending=0)
        events.initialize(self.root)
        events.append(self.root, event_history.bundle([event]), "test-reviewer", "req-review-docling", 0,
                      events.status(self.root)["state_token"])
        self.docs()
        self.config["renditions"] = [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                                     "receipt_id": "D1", "format": "docling",
                                     "context": ["documents/AGENTS.md"],
                                     "review_event_ids": ["rev-exact"]}]
        return path, value

    def test_selected_docling_builds_exact_text_and_table_records(self):
        path, value = self.selected_docling()
        self.build()
        with closing(index.readonly(self.db)) as db:
            rows = db.execute("SELECT locator,payload FROM rendition_locations ORDER BY locator").fetchall()
            self.assertEqual([row[0] for row in rows], [path + "#table-1", path + "#text-1"])
            text_packet = packets.packet(db, self.config, path + "#text-1", 60000, root=self.root)
            text_piece = next(piece for piece in text_packet["pieces"]
                              if piece["locator"] == path + "#text-1")
            self.assertEqual(json.loads(text_piece["text"]), value["conversion"]["document"]["texts"][0])
            self.assertIsNone(text_piece["line_start"])
            self.assertIsNone(text_piece["line_end"])
            rendition_raw = (self.root / path).read_bytes()
            text_location = text_piece["source_location"]
            self.assertEqual(text_piece["text"].encode(),
                             rendition_raw[text_location["byte_start"]:text_location["byte_end"]])
            table_packet = packets.packet(db, self.config, path + "#table-1", 60000, root=self.root)
            table_piece = next(piece for piece in table_packet["pieces"]
                               if piece["locator"] == path + "#table-1")
            self.assertEqual(json.loads(table_piece["text"]), value["conversion"]["document"]["tables"][0])
            location = json.loads(dict(rows)[path + "#table-1"])
            self.assertEqual(table_piece["text"].encode(),
                             rendition_raw[location["byte_start"]:location["byte_end"]])
            self.assertIsNone(table_piece["line_start"])
            self.assertIsNone(table_piece["line_end"])
            self.assertEqual(location["page_no"], 1)
            self.assertEqual(location["json_pointer"], "/conversion/document/tables/0")
            hit = index.search(db, "Reviewed phrase", 10, None, root=self.root)["hits"][0]
            self.assertEqual(hit["locator"], path + "#text-1")
            self.assertIsNone(hit["line_start"])
            self.assertIsNone(hit["line_end"])
            self.assertEqual(hit["source_location"], text_location)
            self.assertEqual(hit["line_basis"], "retained-extraction-byte-range-and-json-pointer")
            self.assertEqual(text_packet["renditions"][path]["qualification"]["review_event_ids"],
                             ["rev-exact"])
            self.assertEqual(text_packet["renditions"][path]["warnings"],
                             value["conversion"]["warnings"])
            self.assertEqual(text_packet["renditions"][path]["confidence"],
                             value["conversion"]["confidence"])
            self.assertEqual(text_packet["renditions"][path]["omissions"],
                             value["conversion"]["omissions"])
            # A broad context rule attached to the table must not leak into a
            # packet for the distinct non-line-based text record.
            self.config["context_rules"] = {path + "#table-1": ["documents/README.md"]}
            isolated = packets.packet(db, self.config, path + "#text-1", 60000, root=self.root)
            self.assertNotIn("documents/README.md", {piece["path"] for piece in isolated["pieces"]})
            self.assertIn("null", text_packet["renditions"][path]["citation_contract"])
            self.assertIn("byte", text_packet["renditions"][path]["citation_contract"])

    def test_malformed_docling_page_and_bbox_geometry_preserve_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        base = json.loads(rendition.read_text())
        before = self.db.read_bytes()
        bbox = lambda value: value["conversion"]["document"]["texts"][0]["prov"][0]["bbox"]
        for label, mutate in (
            ("negative", lambda value: bbox(value).update(l=-1)),
            ("inverted", lambda value: bbox(value).update(l=60, r=40)),
            ("outside", lambda value: bbox(value).update(r=101)),
            ("nonfinite", lambda value: bbox(value).update(l=float("nan"))),
            ("oversized-coordinate", lambda value: bbox(value).update(r=10 ** 1000)),
            ("non-object", lambda value: value["conversion"]["document"]["texts"][0]
             ["prov"][0].update(bbox=[])),
            ("bad-page-width", lambda value: value["conversion"]["pages"][0].update(width=-1)),
            ("oversized-page-width", lambda value: value["conversion"]["pages"][0]
             .update(width=10 ** 1000)),
            ("finite-excessive-page-width", lambda value: value["conversion"]["pages"][0]
             .update(width=1e308)),
            ("excessive-page-count", lambda value: value["conversion"].update(
                pages=[{"page_no": number, "width": 1, "height": 1, "textless": True}
                       for number in range(1, 102)])),
            ("noncanonical-pages", lambda value: value["conversion"]["pages"][0].update(page_no=2)),
        ):
            value = deepcopy(base)
            mutate(value)
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                   + "\n").encode()
            with self.subTest(label=label), self.assertRaises(index.IndexError_):
                list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_excessive_docling_record_count_is_refused(self):
        _, value, _ = self.docling_qualification_fixture()
        value["conversion"].update(
            schema="docling-conversion-v1", format="pdf",
            pages=[{"page_no": 1, "width": 1, "height": 1}],
            warnings=[], confidence={}, omissions=[])
        value["conversion"]["document"]["texts"] = [None] * (
            index.renditions.DOCLING_MAX_RECORDS + 1)
        with self.assertRaisesRegex(index.IndexError_, "record count"):
            list(index.renditions.records(b"{}", value, index.rendition_api()))

    def test_malformed_docling_table_geometry_and_label_preserve_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        base = json.loads(rendition.read_text())
        before = self.db.read_bytes()
        for label, mutate in (
            ("geometry", lambda value: value["conversion"]["document"]["tables"][0]
             ["data"]["table_cells"][0].update(start_row_offset_idx=5, end_row_offset_idx=6)),
            ("label", lambda value: value["conversion"]["document"]["texts"][0].update(label={})),
        ):
            value = deepcopy(base)
            mutate(value)
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                   + "\n").encode()
            with self.subTest(label=label), self.assertRaises(index.IndexError_):
                list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_unsupported_docling_content_collection_preserves_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        value = json.loads(rendition.read_text())
        value["conversion"]["document"]["key_value_items"] = [{"self_ref": "#/key_value_items/0"}]
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "unsupported content"):
            list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_malformed_docling_warnings_preserve_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        value = json.loads(rendition.read_text())
        value["conversion"]["warnings"] = "not-a-list"
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "warnings"):
            list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_unknown_docling_conversion_schema_preserves_prior_index(self):
        self.selected_docling()
        rendition = next(self.root.glob("data/extractions/.local/D1/*/extraction.json"))
        value = json.loads(rendition.read_text())
        value["conversion"]["schema"] = "docling-conversion-v2"
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
               + "\n").encode()
        # Exercise the parser atomically without weakening catalog/hash checks.
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "conversion schema"):
            list(index.renditions.records(raw, value, index.rendition_api()))
        self.assertEqual(self.db.read_bytes(), before)

    def test_docling_missing_page_preserves_prior_index(self):
        self.selected_docling(missing_page=True)
        before = self.db.read_bytes()
        with self.assertRaisesRegex(index.IndexError_, "page provenance"):
            self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_docling_mid_build_review_change_preserves_prior_index(self):
        # Build-start qualification sees the active event; the publication
        # recheck must observe the newer successor map and refuse replacement.
        self.selected_docling()
        before = self.db.read_bytes()
        active = event_reader.read_snapshot(self.root)
        changed = event_reader.Snapshot({**active.state, "state_token": "f" * 64},
                                        active.events, {"rev-exact": "rev-correction"})
        with patch.object(event_reader, "read_snapshot", side_effect=[active, changed]):
            with self.assertRaisesRegex(index.IndexError_, "missing or superseded"):
                self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_qualification_code_is_bound_into_engine_fingerprint(self):
        qualification_path = ROOT / "tools/context_index/qualification.py"
        before = index.engine_fingerprint()
        paths = [Path(index.__file__), ROOT / "tools/context_index/compact_records.py",
                 ROOT / "tools/context_index/renditions.py", qualification_path,
                 ROOT / "tools/context_index/packets.py", ROOT / "tools/context_index/confidence.py",
                 Path(policy.__file__)]
        contents = {path: path.read_bytes() for path in paths}
        with patch.object(Path, "read_bytes", autospec=True,
                          side_effect=lambda path: contents[path]
                          + (b"# changed" if path == qualification_path else b"")):
            self.assertNotEqual(index.engine_fingerprint(), before)

    def test_docling_review_state_binds_freshness_and_failed_rebuild_preserves_index(self):
        selection, value, event = self.docling_qualification_fixture()
        api = index.rendition_api()
        first = qualification.docling(selection, value, {}, event_reader.Snapshot(
            {"state_token": "1" * 64}, [event], {}), api)
        second = qualification.docling(selection, value, {}, event_reader.Snapshot(
            {"state_token": "2" * 64}, [event], {}), api)
        self.assertNotEqual(
            index.metadata_expected(self.root, "manifest", rendition_identity=api.digest(
                json.dumps(first, sort_keys=True).encode())),
            index.metadata_expected(self.root, "manifest", rendition_identity=api.digest(
                json.dumps(second, sort_keys=True).encode())))

        self.docs()
        before = self.db.read_bytes()
        config, manifest_hash = index.load_manifest(self.manifest, self.root)
        with patch.object(index.renditions, "inputs", side_effect=index.IndexError_(
                "Configured Docling review event is missing or superseded")):
            with self.assertRaisesRegex(index.IndexError_, "missing or superseded"):
                index.build(self.root, self.manifest, self.db, config, manifest_hash)
        self.assertEqual(self.db.read_bytes(), before)

    def test_docling_selection_requires_explicit_unique_review_events(self):
        api = index.rendition_api()
        base = {"path": "data/extractions/.local/D1/" + "a" * 64 + "/extraction.json",
                "sha256": "b" * 64, "receipt_id": "D1", "format": "docling",
                "context": ["documents/README.md"], "review_event_ids": ["rev-exact"]}
        index.renditions.validate({"renditions": [base]}, api, root=self.root)
        for review_ids in ([], ["rev-exact", "rev-exact"], [1], [{}]):
            invalid = deepcopy(base)
            invalid["review_event_ids"] = review_ids
            with self.subTest(review_ids=review_ids), self.assertRaises(index.IndexError_):
                index.renditions.validate({"renditions": [invalid]}, api, root=self.root)
        reviewed_text = deepcopy(base)
        reviewed_text["format"] = "text"
        index.renditions.validate({"renditions": [reviewed_text]}, api, root=self.root)
        for review_ids in ([], ["rev-exact", "rev-exact"], [1]):
            invalid = deepcopy(reviewed_text)
            invalid["review_event_ids"] = review_ids
            with self.subTest(text_review_ids=review_ids), self.assertRaises(index.IndexError_):
                index.renditions.validate({"renditions": [invalid]}, api, root=self.root)

    def test_selected_empty_text_is_refused_without_replacing_index(self):
        self.docs()
        preserved = self.db.read_bytes()
        self.config["renditions"] = []
        # Build the empty rendition lifecycle in a fresh workspace state would
        # duplicate the same receipt IDs, so use the current empty selection as
        # the prior index and then register the zero-record source.
        self.registration("sources/empty.txt", b"\xef\xbb\xbf")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        extract.run(self.root, "register", "D1", "text", "test-runner",
                    catalog.status(self.root)["state_token"])
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["kind"] == "artifact" and record["data"]["role"] == "generated-rendition")
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-empty-text",
                       expected_state=catalog.status(self.root)["state_token"], reason="Synthetic empty selection")
        self.config["renditions"] = [{"path": prepared["rendition"],
            "sha256": prepared["rendition_sha256"], "receipt_id": "D1", "format": "text",
            "context": ["documents/AGENTS.md"]}]
        with self.assertRaisesRegex(index.IndexError_, "no retrievable records"):
            self.build()
        self.assertEqual(self.db.read_bytes(), preserved)

    def selected_odt(self):
        text = (ROOT / "AGENTS.md").read_text()
        content = ('<office:document-content xmlns:office="' + extract.NS["office"] +
                   '" xmlns:text="' + extract.NS["text"] +
                   '"><office:body><office:text><text:p>' + escape(text) +
                   '</text:p></office:text></office:body></office:document-content>')
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            z.writestr("content.xml", content)
            z.writestr("meta.xml", '<office:document-meta xmlns:office="' + extract.NS["office"] +
                       '"><office:meta/></office:document-meta>')
            z.writestr("META-INF/manifest.xml", "<manifest/>")
        self.registration("sources/AGENTS.odt", buffer.getvalue())
        prepared = extract.run(self.root, "prepare", "D1", "odt")
        extract.run(self.root, "verify", "D1", "odt")
        result = extract.run(self.root, "register", "D1", "odt", "test-runner",
                             catalog.status(self.root)["state_token"])
        self.assertTrue(result["registered"])
        child = next(r for r in catalog.read_records(self.root, receipt="D1")["records"]
                     if r["kind"] == "artifact" and r["data"]["role"] == "generated-rendition")
        child["data"]["coverage"] = "indexed"
        catalog.submit(self.root, [child], actor="test-runner", request_id="select-rendition",
                       expected_state=catalog.status(self.root)["state_token"], reason="Explicit test selection")
        self.docs()
        self.config["renditions"] = [{"path": prepared["rendition"],
            "sha256": prepared["rendition_sha256"], "receipt_id": "D1", "format": "odt",
            "context": ["documents/AGENTS.md"]}]
        self.build()
        return prepared

    def test_odt_registration_and_rendition_retrieval(self):
        self.selected_odt()
        with closing(index.readonly(self.db)) as db:
            row = db.execute("SELECT locator FROM rendition_locations ORDER BY locator LIMIT 1").fetchone()
            packet = packets.packet(db, self.config, row[0], 60000, root=self.root)
            self.assertIn("renditions", packet)
            self.assertIn("documents/AGENTS.md", {p["path"] for p in packet["pieces"]})

    def test_rendition_policy_tightening_blocks_build_and_reads(self):
        prepared = self.selected_odt()
        preserved = self.db.read_bytes()
        with closing(index.readonly(self.db)) as db:
            locator = db.execute("SELECT locator FROM rendition_locations LIMIT 1").fetchone()[0]
        protected = {self.root / "sources/AGENTS.odt", self.root / prepared["rendition"]}
        original_open = Path.open

        def guard(path, *args, **kwargs):
            self.assertNotIn(path, protected, "Excluded source or rendition was opened")
            return original_open(path, *args, **kwargs)

        for override in (
            {"excluded_name_patterns": [r"^AGENTS\.odt$"]},
            {"original_roots": []},
            {"excluded_directory_names": ["sources"]},
        ):
            with self.subTest(override=override), patch.dict(policy.SETTINGS, override):
                self.assertFalse(policy.allowed("sources/AGENTS.odt", "original_roots"))
                with patch.object(Path, "open", guard):
                    with self.assertRaisesRegex(index.IndexError_, "Original source excluded by current policy"):
                        self.build()
                    for operation in (["get", locator], ["get-many", locator], ["search", "Never seed"]):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            code = index.main([
                                "--root", str(self.root), "--manifest", str(self.manifest),
                                "--db", str(self.db), *operation,
                            ])
                        self.assertEqual(code, 1)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertIn("Original source excluded by current policy", stderr.getvalue())
                self.assertEqual(self.db.read_bytes(), preserved)
        self.build()
        with closing(index.readonly(self.db)) as db:
            self.assertTrue(packets.packet(db, self.config, locator, 60000, root=self.root)["pieces"])

    def test_text_extraction_contract(self):
        raw = b"\xef\xbb\xbfalpha\r\n\r\n\xce\xb2\nlast\r"
        self.assertEqual(extract.text(raw), {
            "format": "text",
            "scope": "all supplied UTF-8 text, byte-mapped without line-ending normalization",
            "bom_bytes": 3,
            "ends_with_terminator": True,
            "lines": [
                {"line": 1, "text": "alpha\r\n", "terminator": "crlf",
                 "byte_start": 3, "byte_end": 10},
                {"line": 2, "text": "\r\n", "terminator": "crlf",
                 "byte_start": 10, "byte_end": 12},
                {"line": 3, "text": "β\n", "terminator": "lf",
                 "byte_start": 12, "byte_end": 15},
                {"line": 4, "text": "last\r", "terminator": "cr",
                 "byte_start": 15, "byte_end": 20},
            ],
            "warnings": ["Strict UTF-8 text decoding only; successful decoding is not review or qualification."],
        })
        self.assertEqual(extract.text(b"one\ntwo")["lines"][-1],
                         {"line": 2, "text": "two", "terminator": "none",
                          "byte_start": 4, "byte_end": 7})
        self.assertFalse(extract.text(b"one\ntwo")["ends_with_terminator"])
        unicode_separators = "a\u0085b\u2028c\u2029d".encode("utf-8")
        self.assertEqual(extract.text(unicode_separators)["lines"], [
            {"line": 1, "text": "a\u0085b\u2028c\u2029d", "terminator": "none",
             "byte_start": 0, "byte_end": len(unicode_separators)},
        ])
        self.assertEqual(extract.text(b"")["lines"], [])
        self.assertEqual(extract.text(b"\xef\xbb\xbf")["lines"], [])
        for raw in (b"\xff", b"\xc3(", b"\xff\xfeA\x00", b"\xfe\xff\x00A",
                    b"\xff\xfe\x00\x00A\x00\x00\x00", b"\x00\x00\xfe\xff\x00\x00\x00A",
                    b"valid\x00text"):
            with self.subTest(raw=raw), self.assertRaises(extract.ExtractionError):
                extract.text(raw)
        with self.assertRaisesRegex(extract.ExtractionError, "line record limit"):
            extract.text(b"x\n" * (extract.MAX_ELEMENTS + 1))
        with patch.object(extract, "MAX_EXPANDED", 100):
            with self.assertRaisesRegex(extract.ExtractionError, "rendition exceeds output limit"):
                extract.text(b"one\ntwo\n")

    def test_text_extraction_lifecycle_is_deferred_and_idempotent(self):
        raw = b"first\r\n\xce\xb2\nlast"
        self.registration("sources/notes.txt", raw)
        source_before = (self.root / "sources/notes.txt").read_bytes()
        events.initialize(self.root)
        review_before = events.status(self.root)["state_token"]
        manifest_before = self.manifest.read_bytes()

        prepared = extract.run(self.root, "prepare", "D1", "text")
        repeated = extract.run(self.root, "prepare", "D1", "text")
        self.assertTrue(prepared["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(extract.run(self.root, "verify", "D1", "text")["run_id"],
                         prepared["run_id"])
        state = catalog.status(self.root)["state_token"]
        registered = extract.run(self.root, "register", "D1", "text", "test-runner", state)
        retry = extract.run(self.root, "register", "D1", "text", "test-runner", state)
        self.assertTrue(registered["registered"])
        self.assertTrue(retry["idempotent"])
        child = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                     if record["kind"] == "artifact" and record["data"]["role"] == "generated-rendition")
        self.assertEqual(child["data"]["coverage"], "deferred")
        self.assertEqual(child["data"]["sha256"], prepared["rendition_sha256"])
        self.assertEqual((self.root / "sources/notes.txt").read_bytes(), source_before)
        self.assertEqual(events.status(self.root)["state_token"], review_before)
        self.assertEqual(self.manifest.read_bytes(), manifest_before)

    def test_text_extraction_accepts_shared_text_like_suffixes(self):
        self.registration("sources/page.md", b"# Title\nbody\n")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        self.assertTrue(prepared["created"])
        with self.assertRaisesRegex(extract.ExtractionError, "disagrees with source extension"):
            extract.run(self.root, "prepare", "D1", "odt")
        with self.assertRaisesRegex(extract.ExtractionError, "disagrees with source extension"):
            extract.run(self.root, "prepare", "D1", "gemini-txt")

    def test_text_lifecycle_refuses_changed_source_and_conflicting_publication(self):
        self.registration("sources/notes.txt", b"stable\n")
        prepared = extract.run(self.root, "prepare", "D1", "text")
        source = self.root / "sources/notes.txt"
        source.write_bytes(b"changed\n")
        with self.assertRaisesRegex(extract.ExtractionError, "catalog hash"):
            extract.run(self.root, "verify", "D1", "text")
        source.write_bytes(b"stable\n")
        rendition = self.root / prepared["rendition"]
        rendition.write_bytes(b"conflicting bytes")
        with self.assertRaisesRegex(extract.ExtractionError, "differs from repeat extraction"):
            extract.run(self.root, "verify", "D1", "text")
        self.assertEqual(rendition.read_bytes(), b"conflicting bytes")

    def test_transcript_dictionary_and_xml_rejection(self):
        text = (ROOT / "AGENTS.md").read_text().splitlines()[0]
        raw = ("Transcript\nReader: " + text + "\n").encode()
        parsed = extract.gemini(raw)
        self.assertEqual("".join(line["text"] for line in parsed["lines"]).encode(), raw)
        changed = dict(policy.SETTINGS["transcript"], transcript_heading="transcript section")
        with patch.dict(policy.SETTINGS, {"transcript": changed}):
            with self.assertRaises(extract.ExtractionError):
                extract.gemini(raw)
        with self.assertRaises(extract.ExtractionError):
            extract.xml(b'<!DOCTYPE x><x/>', "content.xml")

    def test_initialize_refuses_an_unmigrated_legacy_index(self):
        clone_release(self.root / "kit")
        kit = self.root / "kit"
        legacy = kit / "tools/context-index/.local/index.sqlite3"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy derived index")
        result = subprocess.run(
            [sys.executable, "tools/initialize.py"], cwd=kit,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy derived index", result.stderr.lower())
        self.assertFalse((kit / "tools/context_index/.local/index.sqlite3").exists())
        self.assertFalse((kit / ".local/catalog").exists())
        self.assertFalse((kit / ".local/review-events").exists())

    def test_clean_copy_and_policy_freshness(self):
        clone_release(self.root / "kit")
        kit = self.root / "kit"
        def run(*args):
            result = subprocess.run([sys.executable, *args], cwd=kit, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        report = run("tools/initialize.py")
        self.assertEqual(report["seeded_records"], 0)
        (kit / "config/policy.local.json").write_text(json.dumps({"excluded_name_patterns": ["private"]}))
        result = subprocess.run([sys.executable, "-m", "tools.context_index.context_index", "status"],
                                cwd=kit, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr.lower())
        self.assertEqual(run("-m", "tools.context_index.context_index", "build")["documents"], 0)
        output = kit / "bundle.zip"
        run("tools/package.py", "--output", str(output))
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), set(json.loads((kit / "release-files.json").read_text())))
            self.assertNotIn("config/policy.local.json", archive.namelist())

    def test_portable_roots_relocation_substitution_and_migration_recovery(self):
        from tools.intake import store as intake_store

        kit = self.root / "instance_a"
        clone_release(kit)
        custom_roots = {
            "catalog_home": ".local/stores/catalog",
            "review_events_home": ".local/stores/review-events",
            "context_index_db": ".local/stores/index.sqlite3",
            "intake_home": ".local/stores/intake",
            "extractions_home": "data/extractions/.local/custom",
            "diagnostics_log": ".local/stores/diagnostics.jsonl",
            "selection_manifest": "config/selection.json",
        }
        (kit / "config/policy.local.json").write_text(json.dumps({"storage_roots": custom_roots}))
        (kit / "documents").mkdir(exist_ok=True)
        (kit / "documents/guide.md").write_text("# Guide\n\nPortable instance content.\n")
        (kit / "config/selection.local.json").write_text(json.dumps({
            "schema_version": 1,
            "audience": "authorized-local",
            "documents": [{"path": "documents/guide.md", "role": "guide", "context": []}],
            "context_rules": {},
        }))

        def run_in(target, *args, check=True):
            result = subprocess.run([sys.executable, *args], cwd=target, capture_output=True, text=True)
            if check:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            return result

        init_report = run_in(kit, "tools/initialize.py")
        self.assertEqual(init_report["storage_roots"]["catalog_home"], ".local/stores/catalog")
        self.assertTrue((kit / ".local/instance.json").is_file())
        self.assertTrue((kit / ".local/stores/catalog/catalog.sqlite3").is_file())

        cat_state = catalog.status(kit)["state_token"]
        catalog.submit(
            kit,
            [{"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}}],
            actor="operator", request_id="req-portable-s1", expected_state=cat_state,
            reason="Seed portable instance catalog",
        )
        intake_store.initialize(kit)
        request_raw = json.dumps({
            "version": 1, "correlation_id": "req-relocate-1", "correction_of": None,
            "policy": {"version": 1, "sha256": policy.for_root(kit)[1]},
            "retry": {"max_attempts": 2, "backoff_seconds": 5},
            "stages": [{"id": "delivery", "kind": "return", "operation": "await-ack",
                        "arguments": {"delivery_correlation": "delivery-one",
                                      "disposition_path": "disposition.json",
                                      "disposition_sha256": "b" * 64}}],
        }).encode()
        intake_store.stage(kit, request_raw)

        build_report = run_in(kit, "-m", "tools.context_index.context_index", "build")
        self.assertEqual(build_report["documents"], 1)
        self.assertTrue((kit / ".local/stores/index.sqlite3").is_file())

        # Move the entire instance to a new host path: index stays fresh without rebuild.
        relocated = self.root / "relocated_instance"
        shutil.move(str(kit), str(relocated))
        status_after_move = run_in(relocated, "-m", "tools.context_index.context_index", "status")
        self.assertTrue(status_after_move["fresh"])
        hits = run_in(relocated, "-m", "tools.context_index.context_index", "search", "Portable")
        self.assertEqual(len(hits["hits"]), 1)

        # Intake ledger refuses the old host path until explicitly relocated.
        with self.assertRaises(intake_store.IntakeError):
            intake_store.inspect(relocated, "req-relocate-1")
        reloc_report = run_in(relocated, "tools/initialize.py", "--relocate")
        self.assertEqual(reloc_report["intake"]["updated_requests"], 1)
        self.assertEqual(intake_store.inspect(relocated, "req-relocate-1")["correlation_id"], "req-relocate-1")

        # Substituting another instance's catalog, review-events, or index store must fail closed.
        other = self.root / "instance_b"
        clone_release(other)
        run_in(other, "tools/initialize.py")
        run_in(other, "-m", "tools.context_index.context_index", "build")

        cat_db = relocated / ".local/stores/catalog/catalog.sqlite3"
        saved_cat = cat_db.read_bytes()
        cat_db.write_bytes((other / ".local/catalog/catalog.sqlite3").read_bytes())
        try:
            with self.assertRaisesRegex(catalog.CatalogError, "Cross-instance or substituted catalog store"):
                catalog.status(relocated)
            stale_index = run_in(relocated, "-m", "tools.context_index.context_index", "status", check=False)
            self.assertNotEqual(stale_index.returncode, 0)
        finally:
            cat_db.write_bytes(saved_cat)

        idx_db = relocated / ".local/stores/index.sqlite3"
        saved_idx = idx_db.read_bytes()
        idx_db.write_bytes((other / "tools/context_index/.local/index.sqlite3").read_bytes())
        try:
            substituted_idx = run_in(relocated, "-m", "tools.context_index.context_index", "search", "Portable", check=False)
            self.assertNotEqual(substituted_idx.returncode, 0)
            self.assertIn("corpus_root", substituted_idx.stderr)
        finally:
            idx_db.write_bytes(saved_idx)

        # Changing storage_roots without --migrate-storage refuses operation; interrupted migration rolls back cleanly.
        migrated_roots = dict(custom_roots, catalog_home=".local/migrated/catalog",
                              review_events_home=".local/migrated/review-events")
        (relocated / "config/policy.local.json").write_text(json.dumps({"storage_roots": migrated_roots}))
        with self.assertRaisesRegex(catalog.CatalogError, "migrate-storage"):
            catalog.status(relocated)

        pre_cat_token = saved_cat
        with self.assertRaisesRegex(RuntimeError, "Simulated interruption"):
            policy.migrate_storage(relocated, interrupt_after=1)
        self.assertTrue((relocated / ".local/migration.json").is_file())
        with self.assertRaisesRegex(catalog.CatalogError, "recover-migration"):
            catalog.status(relocated)

        recovered = run_in(relocated, "tools/initialize.py", "--recover-migration")
        self.assertTrue(recovered["recovered"])
        self.assertFalse((relocated / ".local/migration.json").exists())
        (relocated / "config/policy.local.json").write_text(json.dumps({"storage_roots": custom_roots}))
        self.assertEqual(cat_db.read_bytes(), pre_cat_token)

        # Complete migration moves stores and preserves state tokens.
        (relocated / "config/policy.local.json").write_text(json.dumps({"storage_roots": migrated_roots}))
        before_token = policy.read_instance_binding(relocated)["catalog_store_id"]
        mig_report = run_in(relocated, "tools/initialize.py", "--migrate-storage")
        self.assertTrue(mig_report["migrated"])
        self.assertEqual(catalog.status(relocated)["store_id"], before_token)
        self.assertTrue((relocated / ".local/migrated/catalog/catalog.sqlite3").is_file())

    def test_portable_roots_edge_cases_and_instance_policy_propagation(self):
        from tools.intake import adapters as intake_adapters, store as intake_store
        from tools.extraction import docling_adapter, docling_bridge

        kit = self.root / "instance_custom"
        clone_release(kit)
        custom_roots = {
            "catalog_home": ".local/custom/catalog",
            "review_events_home": ".local/custom/review-events",
            "context_index_db": ".local/custom/index.sqlite3",
            "intake_home": ".local/queues/recovery/intake",
            "extractions_home": "data/extractions/.local/custom-runs",
            "diagnostics_log": ".local/custom/diagnostics.jsonl",
            "selection_manifest": "config/custom-selection.json",
        }
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": custom_roots,
        }))
        (kit / "custom_docs").mkdir(exist_ok=True)
        (kit / "custom_docs/AGENTS.md").write_text("# Custom Context\n\nCompanion context rules.\n")
        (kit / "custom_docs/guide.md").write_text("# Custom Guide\n\nUniqueCustomToken content.\n")
        (kit / "config/custom-selection.json").write_text(json.dumps({
            "schema_version": 1,
            "audience": "custom-instance-audience",
            "documents": [
                {
                    "path": "custom_docs/AGENTS.md",
                    "role": "rules",
                    "context": [],
                },
                {
                    "path": "custom_docs/guide.md",
                    "role": "guide",
                    "context": ["custom_docs/AGENTS.md"],
                },
            ],
            "context_rules": {},
        }))
        # Keep shipped default config/selection.json template alongside custom-selection.json
        self.assertTrue((kit / "config/selection.json").is_file())

        def run_in(target, *args, check=True):
            result = subprocess.run([sys.executable, *args], cwd=target, capture_output=True, text=True)
            if check:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            return result

        # 1. Initialize instance + deeply nested intake_home (.local/queues/recovery/intake)
        run_in(kit, "tools/initialize.py")
        intake_store.initialize(kit)
        self.assertTrue((kit / ".local/queues/recovery/intake/intake.sqlite3").is_file())
        self.assertEqual(stat.S_IMODE((kit / ".local/queues/recovery").stat().st_mode), 0o700)

        # 2. Search envelope (hit and zero-result) reflects custom instance audience and handling
        stage_spec = {
            "id": "build-idx",
            "kind": "publication",
            "operation": "index-build",
            "arguments": {
                "manifest": "config/custom-selection.json",
                "db": ".local/custom/index.sqlite3",
            },
        }
        intent = intake_adapters.prepare(kit, stage_spec)
        self.assertTrue(intent["selection_validated"])
        ev = intake_adapters.execute(kit, stage_spec, intent)
        self.assertEqual(ev["boundary"], "publication-confirmed")
        self.assertTrue(intake_adapters.availability(kit, stage_spec, ev)["available"])

        hit_res = run_in(kit, "-m", "tools.context_index.context_index", "search", "UniqueCustomToken")
        self.assertEqual(hit_res["audience"], "custom-instance-audience")
        self.assertEqual(hit_res["handling"], "Custom instance handling notice.")
        empty_res = run_in(kit, "-m", "tools.context_index.context_index", "search", "AbsentTokenXYZ")
        self.assertEqual(empty_res["audience"], "custom-instance-audience")
        self.assertEqual(empty_res["handling"], "Custom instance handling notice.")
        self.assertIn("absence", empty_res)

        # 3. Missing authority DB restore works on an initialized instance and enforces store_id lineage
        cat_backup = catalog.backup(kit)
        rev_backup = events.backup(kit)
        cat_db = kit / ".local/custom/catalog/catalog.sqlite3"
        rev_db = kit / ".local/custom/review-events/events.sqlite3"
        cat_db.unlink()
        rev_db.unlink()
        restored_cat = catalog.restore(kit, Path(cat_backup["snapshot"]))
        self.assertEqual(restored_cat["command"], "restore")
        self.assertTrue(cat_db.is_file())
        restored_rev = events.restore(kit, Path(rev_backup["snapshot"]))
        self.assertEqual(restored_rev["command"], "restore")
        self.assertTrue(rev_db.is_file())

        # 4. rendition_api(kit) binds validate_context to kit (custom_docs/AGENTS.md)
        api = index.rendition_api(kit)
        api.validate_context(["custom_docs/AGENTS.md"])

        # 5. Interruption before journal update rolls back planned moves cleanly
        diagnostics.Log(kit, custom_roots["diagnostics_log"]).record_read(
            {"command": "get"},
            started_at="2026-10-22T12:00:00+00:00",
            completed_at="2026-10-22T12:00:01+00:00",
            duration_ms=10,
        )
        next_roots = dict(
            custom_roots,
            catalog_home=".local/moved/catalog",
            review_events_home=".local/moved/review-events",
            diagnostics_log=".local/moved/diagnostics.jsonl",
            selection_manifest="config/moved-selection.json",
        )
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": next_roots,
        }))
        with self.assertRaisesRegex(RuntimeError, "before journal update"):
            policy.migrate_storage(kit, interrupt_before_journal=1)
        self.assertFalse(cat_db.exists())
        self.assertTrue((kit / ".local/moved/catalog/catalog.sqlite3").is_file())
        recovered = policy.recover_migration(kit)
        self.assertTrue(recovered["recovered"])
        self.assertIn("catalog_home", recovered["rolled_back"])
        self.assertTrue(cat_db.is_file())
        self.assertFalse((kit / ".local/migration.json").exists())

        # Complete migration moves diagnostics_log and selection_manifest too
        completed = policy.migrate_storage(kit)
        self.assertTrue(completed["migrated"])
        self.assertTrue((kit / ".local/moved/diagnostics.jsonl").is_file())
        self.assertTrue((kit / "config/moved-selection.json").is_file())

        # 6. Docling prepare/verify/register succeeds on an instance with custom policy and extractions_home
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": next_roots,
        }))
        self.assertEqual(docling_bridge.binding(kit)["policy_sha256"], policy.for_root(kit)[1])
        self.assertEqual(docling_adapter._extractions_home(kit), Path("data/extractions/.local/custom-runs"))
        raw_pdf = b"%PDF-1.7\ncustom instance docling input"
        (kit / "custom_sources").mkdir(exist_ok=True)
        (kit / "custom_sources/input.pdf").write_bytes(raw_pdf)
        cat_state = catalog.status(kit)["state_token"]
        catalog.submit(
            kit,
            [
                {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}},
                {"id": "D1", "kind": "receipt", "data": {
                    "source_ids": ["S1"], "artifact_id": "A-pdf", "provenance": []}},
                {"id": "A-pdf", "kind": "artifact", "data": {
                    "path": "custom_sources/input.pdf", "parent_id": None, "receipt_id": "D1",
                    "role": "original", "audience": "source", "coverage": "pending-extraction",
                    "reason": "Custom instance Docling test.", "presence": "present",
                    "sha256": hashlib.sha256(raw_pdf).hexdigest(), "hash_basis": "observed",
                    "bytes": len(raw_pdf), "extraction": "pending", "handling_refs": [],
                    "provenance": [],
                }},
            ],
            actor="operator", request_id="seed-custom-docling", expected_state=cat_state,
            reason="Seed custom instance Docling receipt",
        )
        stub_report = {
            "conversion": {"schema": "docling-rendition-v1", "format": "pdf",
                           "document": {"texts": [], "tables": []}, "pages": [],
                           "confidence": {}, "warnings": [], "omissions": []},
            "runtime": {"docling": "test"},
            "profile": {"version": 2, "limits": {}},
            "assets": {"schema": "docling-assets-v1", "files": [], "sha256": "a" * 64},
        }
        worker_sha = hashlib.sha256(docling_adapter.WORKER.read_bytes()).hexdigest()
        with patch.object(
            docling_adapter,
            "_convert_with_identity",
            side_effect=lambda *args: (deepcopy(stub_report), worker_sha),
        ):
            prep = docling_adapter.run(
                kit, "prepare", "D1", "pdf", Path("/unused/models"), Path("/unused/python")
            )
            self.assertTrue(prep["rendition"].startswith("data/extractions/.local/custom-runs/D1/"))
            docling_adapter.run(
                kit, "verify", "D1", "pdf", Path("/unused/models"), Path("/unused/python")
            )
            reg = docling_adapter.run(
                kit, "register", "D1", "pdf", Path("/unused/models"), Path("/unused/python"),
                actor="operator", expected_state=catalog.status(kit)["state_token"],
            )
            self.assertTrue(reg["registered"])

        # 7. Populated extractions_home refuses auto-migration before moving anything
        bad_roots = dict(next_roots, extractions_home="data/extractions/.local/other-runs")
        (kit / "config/policy.local.json").write_text(json.dumps({
            "audience": "custom-instance-audience",
            "handling": "Custom instance handling notice.",
            "markdown_roots": ["documents", "custom_docs"],
            "original_roots": ["sources", "custom_sources"],
            "storage_roots": bad_roots,
        }))
        with self.assertRaisesRegex(ValueError, "populated extractions_home"):
            policy.migrate_storage(kit)
        self.assertFalse((kit / ".local/migration.json").exists())

    # Freshness identity memo (Kata t5df, simplified by xv0n). The memo records the identity that was read
    # from the catalog and review-event pins. Stores a fixture just wrote are "racily" new, so these
    # tests stand the memo's clock a few seconds ahead unless they test that rule itself.
    def aged_inputs(self):
        clock = patch.object(proof_cache, "_now_ns", lambda: time.time_ns() + 3 * 10 ** 9)
        clock.start()
        self.addCleanup(clock.stop)

    def reader_check(self, config=None, checksum=None):
        if config is None:
            config, checksum = index.load_manifest(self.manifest, self.root)
        with closing(index.readonly(self.db)) as db:
            return index.freshness(db, self.root, self.manifest, self.db, config, checksum, reuse=True)

    def pin_reads(self):
        return patch.object(index.renditions, "pinned_identity", wraps=index.renditions.pinned_identity)

    def test_build_leaves_a_private_freshness_proof_that_readers_reuse_without_the_full_proof(self):
        self.aged_inputs()
        self.selected_text()
        memo = proof_cache.location(self.db)
        self.assertEqual(stat.S_IMODE(memo.stat().st_mode), 0o600)
        refused = AssertionError("identity recomputed on a memo hit")
        with patch.object(index.renditions, "inputs", side_effect=refused), \
                patch.object(index.renditions, "pinned_identity", side_effect=refused), \
                patch.object(event_reader, "read_snapshot", side_effect=refused):
            self.assertEqual(self.reader_check(), [])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(index.main(["--root", str(self.root), "search", "last"]), 0)
            self.assertTrue(json.loads(stdout.getvalue())["hits"])
            # Publication never reuses the memo.
            config, checksum = index.load_manifest(self.manifest, self.root)
            with closing(index.readonly(self.db)) as db, self.assertRaises(AssertionError):
                index.freshness(db, self.root, self.manifest, self.db, config, checksum)

    def test_freshness_proof_memo_misses_on_store_and_configuration_changes(self):
        self.aged_inputs()
        self.selected_text()
        # A catalog commit, a new selection context and a policy change each recompute the identity.
        self.assertEqual(self.reader_check(), [])
        unrelated = {"id": "S2", "kind": "source", "data": {"category": "artifact", "provenance": []}}
        catalog.submit(self.root, [unrelated], actor="test-runner", request_id="after-proof",
                       expected_state=catalog.status(self.root)["state_token"], reason="Memo test")
        with self.pin_reads() as pinned:
            self.assertEqual(self.reader_check(), [])
            self.assertEqual(pinned.call_count, 1)
            self.assertEqual(self.reader_check(), [])
            self.assertEqual(pinned.call_count, 1)
        self.config["renditions"][0]["context"] = ["documents/README.md"]
        self.save_config()
        with self.pin_reads() as pinned:
            self.assertIn("metadata changed: manifest_hash", self.reader_check())
            self.assertEqual(pinned.call_count, 1)
        (self.root / "config/policy.local.json").write_text(json.dumps({"excluded_name_patterns": ["private"]}))
        with self.pin_reads() as pinned:
            self.assertIn("metadata changed: policy_hash", self.reader_check())
            self.assertEqual(pinned.call_count, 1)

    def test_a_changed_retained_file_is_no_freshness_failure_but_the_audit_and_verified_reads_report_it(self):
        """Kata xv0n changes this guarantee: readers prove freshness from the catalog's pins and no longer
        reopen the selected files. The files are checked by ``integrity`` (audit; read-time verification),
        and a build still verifies every one in full."""
        self.aged_inputs()
        prepared = self.selected_text()
        extraction = prepared["rendition"]
        cases = {
            "original bytes (same size)": ("sources/notes.txt", lambda path: path.write_bytes(b"First\r\n\xce\xb2\nlast"),
                                           "original", "hash-differs"),
            "extraction bytes": (extraction, lambda path: path.write_bytes(path.read_bytes() + b" "),
                                 "extraction", "hash-differs"),
            "extraction removed": (extraction, lambda path: path.unlink(), "extraction", "missing"),
            "extraction replaced by a symlink": (
                extraction, lambda path: (path.rename(path.with_name("moved.json")),
                                          path.symlink_to(path.with_name("moved.json"))),
                "extraction", "symlink"),
        }
        for name, (relative, change, kind, problem) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as saved:
                shutil.copytree(self.root, Path(saved) / "root", symlinks=True)
                self.assertEqual(self.reader_check(), [])
                self.assertTrue(integrity.audit(self.root)["clean"])
                change(self.root / relative)
                self.assertEqual(self.reader_check(), [])
                report = integrity.audit(self.root)
                self.assertFalse(report["clean"])
                self.assertEqual([(item["path"], item["kind"], item["problem"]) for item in report["items"]],
                                 [(relative, kind, problem)])
                with self.assertRaises(integrity.IntegrityMismatch) as refused:
                    integrity.read_verified(self.root, relative)
                self.assertEqual(refused.exception.mismatch["problem"], problem)
                with self.assertRaises((index.IndexError_, ValueError)):
                    config, checksum = index.load_manifest(self.manifest, self.root)
                    index.build(self.root, self.manifest, self.db, config, checksum)
                shutil.rmtree(self.root)
                shutil.copytree(Path(saved) / "root", self.root, symlinks=True)

    def test_identity_read_from_the_pins_equals_the_fully_verified_one(self):
        prepared = self.selected_text()
        api = index.rendition_api(self.root)
        config, _ = index.load_manifest(self.manifest, self.root)
        verified = index.renditions.inputs(self.root, config, api, keep=False)[1]
        self.assertEqual(index.renditions.pinned_identity(self.root, config, api), verified)
        # A different pin is a different identity: a catalog commit that re-pins the original changes it.
        original = next(record for record in catalog.read_records(self.root, receipt="D1")["records"]
                        if record["id"] == "A-input")
        original["data"]["reason"] = "Re-pinned by a later revision"
        catalog.submit(self.root, [original], actor="test-runner", request_id="repin",
                       expected_state=catalog.status(self.root)["state_token"], reason="Pin change")
        self.assertNotEqual(index.renditions.pinned_identity(self.root, config, api), verified)
        self.assertEqual(index.renditions.pinned_identity(self.root, config, api),
                         index.renditions.inputs(self.root, config, api, keep=False)[1])
        self.assertTrue(prepared)

    def test_freshness_proof_memo_binds_the_review_event_store_when_selections_are_reviewed(self):
        self.aged_inputs()
        self.registration()
        state = events.initialize(self.root)
        config = {"renditions": [{"path": "x", "format": "text", "review_event_ids": ["rev-pending"]}]}
        self.db.parent.mkdir(parents=True)
        computed = []

        def compute():
            computed.append(1)
            return "a" * 64
        for _ in range(2):
            self.assertEqual(proof_cache.identity(self.root, config, self.db, compute), "a" * 64)
        self.assertEqual(len(computed), 1)
        events.append(self.root, event_history.bundle([self.pending()]), "test-runner", "req-pending",
                      0, state["state_token"])
        proof_cache.identity(self.root, config, self.db, compute)
        self.assertEqual(len(computed), 2)

    def test_corrupt_forged_or_unsafe_freshness_proof_memo_is_ignored(self):
        self.aged_inputs()
        self.selected_text()
        memo = proof_cache.location(self.db)
        record = json.loads(memo.read_text())
        forged = dict(record, identity="0" * 64)
        tampered = {
            "corrupt": lambda: memo.write_bytes(b"{not json"),
            "forged identity": lambda: memo.write_text(json.dumps(forged)),
            "world readable": lambda: memo.chmod(0o644),
            "symlink": lambda: (memo.rename(memo.with_name("elsewhere.json")),
                                memo.symlink_to(memo.with_name("elsewhere.json"))),
        }
        for name, tamper in tampered.items():
            with self.subTest(name):
                tamper()
                with self.pin_reads() as full:
                    self.assertEqual(self.reader_check(), [])
                    self.assertEqual(full.call_count, 1)
                self.assertFalse(memo.is_symlink())
                self.assertEqual(stat.S_IMODE(memo.stat().st_mode), 0o600)
                rewritten = json.loads(memo.read_text())
                self.assertEqual((rewritten["identity"], rewritten["stores"]), (record["identity"], record["stores"]))

    def test_freshness_proof_with_racily_new_inputs_is_not_remembered(self):
        self.selected_text()
        self.assertFalse(proof_cache.location(self.db).exists())
        with self.pin_reads() as full:
            self.assertEqual(self.reader_check(), [])
            self.assertEqual(full.call_count, 1)

    def test_freshness_identity_opens_no_selected_file_and_the_memo_signs_the_stores(self):
        self.aged_inputs()
        prepared = self.selected_text()
        proof_cache.location(self.db).unlink()
        opened, active = set(), []
        pinned = index.renditions.pinned_identity

        def hook(event, args):
            if active and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
                opened.add(os.path.abspath(os.fsdecode(args[0])))
            elif active and event == "sqlite3.connect":
                opened.add(os.path.abspath(unquote(urlsplit(str(args[0])).path)))

        def recorded(*args, **kwargs):
            active.append(True)
            try:
                return pinned(*args, **kwargs)
            finally:
                active.clear()
        sys.addaudithook(hook)
        with patch.object(index.renditions, "pinned_identity", side_effect=recorded):
            self.assertEqual(self.reader_check(), [])
        catalog_home = self.root / policy.validate_instance_roots(self.root)["catalog_home"]
        selected = {str(self.root / prepared["rendition"]), str(self.root / "sources/notes.txt")}
        self.assertEqual(opened & selected, set(), "a selected file was opened to prove freshness")
        self.assertIn(str(catalog_home / "catalog.sqlite3"), opened)
        self.assertEqual(set(json.loads(proof_cache.location(self.db).read_text())["stores"]),
                         {str(Path(policy.validate_instance_roots(self.root)["catalog_home"]) / "catalog.sqlite3")})
