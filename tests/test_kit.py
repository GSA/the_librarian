"""Structural contracts against this toolkit's actual documentation, never a seeded corpus."""

from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
for directory in ("tools", "tools/catalog", "tools/context-index", "tools/review-events", "tools/extraction"):
    sys.path.insert(0, str(ROOT / directory))
import policy
import catalog_store as catalog
import context_index as index
import compact_records
import event_reader
import event_store as events
import review_events as contract
import extract
import package


def clone(destination):
    for name, source in package.release_paths(ROOT):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "config/selection.json"
        self.manifest.parent.mkdir()
        self.config = {"schema_version": 1, "audience": policy.SETTINGS["audience"],
                       "documents": [], "context_rules": {}, "renditions": []}
        self.db = self.root / "tools/context-index/.local/index.sqlite3"
        self.save_config()

    def save_config(self):
        self.manifest.write_text(json.dumps(self.config))

    def build(self):
        self.save_config()
        config, checksum = index.load_manifest(self.manifest)
        with index.publication_lock(self.db):
            return index.build(self.root, self.manifest, self.db, config, checksum)

    def docs(self):
        (self.root / "documents").mkdir()
        for name in ("README.md", "AGENTS.md"):
            shutil.copyfile(ROOT / name, self.root / "documents" / name)
        self.config["documents"] = [
            {"path": "documents/README.md", "role": "toolkit-documentation",
             "context": ["documents/AGENTS.md"]},
            {"path": "documents/AGENTS.md", "role": "toolkit-instructions", "context": []},
        ]
        self.config["context_rules"] = {
            "documents/README.md#interfaces": ["documents/AGENTS.md"]
        }
        self.build()

    def registration(self, name="sources/README.txt", raw=None):
        raw = raw if raw is not None else (ROOT / "README.md").read_bytes()
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        record = {"id": "A-input", "kind": "artifact", "data": {
            "path": name, "parent_id": None, "receipt_id": "D1", "role": "original",
            "audience": "source", "coverage": "pending-extraction",
            "reason": "Toolkit documentation used for an isolated structural test.",
            "presence": "present", "sha256": hashlib.sha256(raw).hexdigest(),
            "hash_basis": "observed", "bytes": len(raw), "extraction": "pending",
            "handling_refs": [], "provenance": [],
        }}
        records = [
            {"id": "S1", "kind": "source", "data": {"category": "artifact", "provenance": []}},
            {"id": "D1", "kind": "receipt", "data": {
                "source_ids": ["S1"], "artifact_id": "A-input", "provenance": []}},
            record,
        ]
        state = catalog.initialize(self.root)["state"]
        result = catalog.submit(self.root, records, actor="test-runner", request_id="register-input",
                                expected_state=state["state_token"], reason="Structural contract test")
        return records, result

    def pending(self, locator="sources/README.txt"):
        now = datetime.now(timezone.utc)
        return {
            "event_id": "rev-pending", "source": {
                "receipt_id": "D1", "source_id": "S1", "locator": locator,
                "parent_locator": None, "checksum": {"sha256": None, "basis": "unknown"}},
            "scope": {"content": ["text"], "unit": "file", "extent": "defined_scope",
                      "coordinates": "not_applicable", "ranges": [],
                      "description": "Toolkit documentation input; no semantic review performed."},
            "outcome": "pending", "counts": {
                "total": 1, "reviewed": 0, "pending": 1, "unreadable": 0, "dispositioned": 0},
            "reason": "Only structural contract behavior is under test.",
            "reviewer": "test-runner", "reviewed_on": None, "recorded_at": now.isoformat(),
            "evidence": [{"kind": "agent_statement", "pointer": None,
                          "quote": "No semantic review performed by this test."}],
            "uncertainties": ["Source version is not attested by this event."],
            "handling": [policy.SETTINGS["handling"]],
            "provenance": {"origin": "agent_review", "record": None,
                           "mapping_version": None, "unmapped_fields": []},
            "relationships": [], "supersedes": [], "correction_reason": None,
        }

    def test_empty_initialization(self):
        self.assertEqual(catalog.initialize(self.root)["records"], 0)
        self.assertEqual(events.initialize(self.root)["events"], 0)
        self.assertEqual(self.build()["documents"], 0)
        with closing(index.readonly(self.db)) as db:
            self.assertEqual(index.search(db, "documentation", 5, None)["hits"], [])
        self.assertEqual(event_reader.read_snapshot(self.root).query()["records"], [])

    def test_exact_packets_and_compaction(self):
        self.docs()
        with closing(index.readonly(self.db)) as db:
            packet = index.packet(db, self.config, "documents/README.md#interfaces", 60000)
            self.assertIn("documents/AGENTS.md", {p["path"] for p in packet["pieces"]})
            for piece in packet["pieces"]:
                raw = (self.root / piece["path"]).read_bytes()
                lines = raw.decode().splitlines(keepends=True)
                self.assertEqual(piece["source_hash"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(piece["text"], "".join(lines[piece["line_start"]-1:piece["line_end"]]))
            compact = index.compact_packet(packet)
            self.assertEqual([p["text"] for p in compact["pieces"]], [p["text"] for p in packet["pieces"]])
            many = index.packet_many(db, self.config,
                                     ["documents/README.md#interfaces", "documents/AGENTS.md"], 60000)
            self.assertEqual(len(many["requests"]), 2)
            with self.assertRaises(index.IndexError_):
                index.packet(db, self.config, "documents/README.md", 1)
            with self.assertRaises(index.IndexError_):
                index.packet_many(db, self.config, [], 60000)

    def test_missing_context_refuses_build(self):
        self.docs()
        before = self.db.read_bytes()
        self.config["context_rules"]["documents/README.md"] = ["documents/AGENTS.md#not-present"]
        with self.assertRaises(index.IndexError_):
            self.build()
        self.assertEqual(self.db.read_bytes(), before)

    def test_stale_content_detected(self):
        self.docs()
        (self.root / "documents/AGENTS.md").write_bytes((ROOT / "README.md").read_bytes())
        config, digest = index.load_manifest(self.manifest)
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
        snapshot = Path(result["backup"]["snapshot"])
        self.assertEqual(catalog.verify_backup(snapshot), result["state"])
        restored = self.root / "restored"
        restored.mkdir()
        catalog.restore(restored, snapshot)
        self.assertEqual(catalog.status(restored)["state_token"], result["state_token"])

    def test_native_review_correction_retry_and_restore(self):
        self.registration()
        state = events.initialize(self.root)
        event = self.pending()
        result = events.append(self.root, events.bundle([event]), "test-runner", "req-pending",
                               0, state["state_token"])
        retry = events.append(self.root, events.bundle([event]), "test-runner", "req-pending",
                              0, state["state_token"])
        self.assertTrue(retry["idempotent"])
        updated = deepcopy(event)
        updated.update(event_id="rev-correction", supersedes=[event["event_id"]],
                       correction_reason="Clarify pending reason without asserting review.",
                       reason="This remains pending after the structural test.",
                       recorded_at=(datetime.fromisoformat(event["recorded_at"]) + timedelta(seconds=1)).isoformat())
        result = events.append(self.root, events.bundle([updated]), "test-runner", "req-correction",
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
        events.restore(destination, Path(result["backup"]["snapshot"]))
        self.assertEqual(events.status(destination)["state_token"], result["state_token"])

    def test_invalid_native_event_rejected(self):
        events.initialize(self.root)
        event = self.pending()
        event["counts"]["reviewed"] = 1
        with self.assertRaises(contract.ReviewError):
            contract.validate(events.bundle([event]), schema=events.store_schema(), stored=True)
        event = self.pending()
        event["provenance"]["origin"] = "unattributed"
        with self.assertRaises(contract.ReviewError):
            events.prepare_submission(events.bundle([event]), self.root)

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
            packet = index.packet(db, self.config, row[0], 60000)
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
            self.assertTrue(index.packet(db, self.config, locator, 60000)["pieces"])

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

    def test_clean_copy_and_policy_freshness(self):
        clone(self.root / "kit")
        kit = self.root / "kit"
        def run(*args):
            result = subprocess.run([sys.executable, *args], cwd=kit, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        report = run("tools/initialize.py")
        self.assertEqual(report["seeded_records"], 0)
        (kit / "config/policy.local.json").write_text(json.dumps({"excluded_name_patterns": ["private"]}))
        result = subprocess.run([sys.executable, "tools/context-index/context_index.py", "status"],
                                cwd=kit, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr.lower())
        self.assertEqual(run("tools/context-index/context_index.py", "build")["documents"], 0)
        output = kit / "bundle.zip"
        run("tools/package.py", "--output", str(output))
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), set(json.loads((kit / "release-files.json").read_text())))
            self.assertNotIn("config/policy.local.json", archive.namelist())


if __name__ == "__main__":
    unittest.main()
