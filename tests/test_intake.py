"""Intake contract and recovery tests, using private temporary instances."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.intake import contracts, store


def request(correlation="req-one", **changes):
    value = {"version": 1, "correlation_id": correlation, "correction_of": None,
             "policy": {"version": 1, "sha256": "a" * 64},
             "retry": {"max_attempts": 2, "backoff_seconds": 5},
             "stages": [{"id": "delivery", "kind": "return", "operation": "await-ack",
                         "arguments": {"delivery_correlation": "delivery-one",
                                       "disposition_path": "disposition.json",
                                       "disposition_sha256": "b" * 64}}]}
    value.update(changes)
    return json.dumps(value).encode()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store.initialize(self.root)

    def test_exact_bytes_and_conflicting_correlation(self):
        raw = request()
        first = store.stage(self.root, raw)
        self.assertEqual(first["request_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(store.stage(self.root, raw)["correlation_id"], "req-one")
        with self.assertRaises(contracts.IntakeError):
            store.stage(self.root, raw + b"\n")
        store.stage(self.root, request("req-two", correction_of="req-one"))
        self.assertEqual(store.inspect(self.root, "req-two")["correction_of"], "req-one")
        self.assertEqual(len(store.list_requests(self.root)), 2)
        self.assertNotIn("arguments", json.dumps(first))
        with sqlite3.connect(self.root / ".local/intake/intake.sqlite3") as connection:
            self.assertEqual(connection.execute("select raw from requests where correlation_id='req-one'").fetchone()[0], raw)

    def test_private_store_contract_and_integrity(self):
        with self.assertRaises(contracts.IntakeError):
            store.initialize(self.root)
        store.stage(self.root, request())
        db = self.root / ".local/intake/intake.sqlite3"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
        with sqlite3.connect(db) as connection:
            connection.execute("update requests set request_sha256=?", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError):
            store.inspect(self.root, "req-one")

    def test_wrong_identity_modes_and_missing_store_refuse(self):
        db = self.root / ".local/intake/intake.sqlite3"
        db.chmod(0o644)
        with self.assertRaises(contracts.IntakeError):
            store.list_requests(self.root)
        db.chmod(0o600)
        with sqlite3.connect(db) as connection:
            connection.execute("pragma application_id=1")
        with self.assertRaises(contracts.IntakeError):
            store.list_requests(self.root)
        db.unlink()
        with self.assertRaises(contracts.IntakeError):
            store.list_requests(self.root)
        self.assertFalse(db.exists())

    def test_symlink_root_refuses(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(contracts.IntakeError):
            store.list_requests(alias)

    def test_request_validation(self):
        valid = json.loads(request())
        mutations = [dict(valid, version=True), dict(valid, extra=1), dict(valid, stages=[]),
                     dict(valid, retry={"max_attempts": 11, "backoff_seconds": 0}),
                     dict(valid, retry={"max_attempts": 1, "backoff_seconds": True}),
                     dict(valid, stages=[dict(valid["stages"][0], operation="shell")]),
                     dict(valid, stages=[dict(valid["stages"][0], arguments={"command": "secret"})])]
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(contracts.IntakeError):
                contracts.decode_request(json.dumps(value).encode())
        with self.assertRaises(contracts.IntakeError):
            contracts.decode_request(b'{"version":1,"version":1}')
        with self.assertRaises(contracts.IntakeError):
            contracts.decode_request(b" " * (1024 * 1024 + 1))
        with self.assertRaises(contracts.IntakeError):
            store.stage(self.root, request("req-two", correction_of="missing"))

    def test_tampered_stage_refuses(self):
        store.stage(self.root, request())
        with sqlite3.connect(self.root / ".local/intake/intake.sqlite3") as connection:
            connection.execute("update stages set status='succeeded'")
        with self.assertRaises(contracts.IntakeError):
            store.inspect(self.root, "req-one")
