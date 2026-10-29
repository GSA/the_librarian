"""Intake contract and recovery tests, using private temporary instances."""
from contextlib import closing
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
        with closing(sqlite3.connect(self.root / ".local/intake/intake.sqlite3")) as connection, connection:
            self.assertEqual(connection.execute("select raw from requests where correlation_id='req-one'").fetchone()[0], raw)

    def test_reserved_uri_characters_in_root(self):
        for name in ("instance?query", "instance#fragment"):
            with self.subTest(name=name):
                root = self.root / name
                root.mkdir()
                store.initialize(root)
                store.stage(root, request())
                self.assertEqual(store.inspect(root, "req-one")["correlation_id"], "req-one")

    def test_private_store_contract_and_integrity(self):
        with self.assertRaises(contracts.IntakeError):
            store.initialize(self.root)
        store.stage(self.root, request())
        db = self.root / ".local/intake/intake.sqlite3"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("update requests set request_sha256=?", ("0" * 64,))
        with self.assertRaises(contracts.IntakeError):
            store.inspect(self.root, "req-one")

    def test_wrong_identity_modes_and_missing_store_refuse(self):
        db = self.root / ".local/intake/intake.sqlite3"
        db.chmod(0o644)
        with self.assertRaises(contracts.IntakeError):
            store.list_requests(self.root)
        db.chmod(0o600)
        with closing(sqlite3.connect(db)) as connection, connection:
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
        with closing(sqlite3.connect(self.root / ".local/intake/intake.sqlite3")) as connection, connection:
            connection.execute("update stages set status='succeeded'")
        with self.assertRaises(contracts.IntakeError):
            store.inspect(self.root, "req-one")

    def test_rejected_ledger_releases_lock_for_later_operations(self):
        import multiprocessing
        store.stage(self.root, request())
        db = self.root / ".local/intake/intake.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("update stages set status='succeeded'")
        with self.assertRaises(contracts.IntakeError):
            store.inspect(self.root, "req-one")
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("update stages set status='staged'")
        # A separate process would block forever on a leaked exclusive lock.
        def probe(root, sink):
            sink.send(store.inspect(root, "req-one")["stages"][0]["status"])
            sink.close()
        receiver, sender = multiprocessing.Pipe(duplex=False)
        worker = multiprocessing.get_context("fork").Process(target=probe, args=(self.root, sender))
        worker.start()
        self.addCleanup(worker.join)
        self.assertTrue(receiver.poll(30), "intake lock was not released")
        self.assertEqual(receiver.recv(), "staged")


class RunnerTests(unittest.TestCase):
    setUp = LedgerTests.setUp
    def stage_live(self):
        from tools import policy
        raw = request(policy={"version": 1, "sha256": policy.load()[1]})
        store.stage(self.root, raw)
        return raw

    def test_intent_and_attempt_precede_effect(self):
        from unittest.mock import patch
        from tools.intake import runner
        self.stage_live()
        def effect(root, stage, intent, previous):
            with closing(sqlite3.connect(root / ".local/intake/intake.sqlite3")) as c:
                self.assertEqual(c.execute("select status from stages").fetchone()[0], "running")
                self.assertIsNotNone(c.execute("select intent from stages").fetchone()[0])
                self.assertEqual(c.execute("select count(*) from attempts").fetchone()[0], 1)
            return {"boundary": "return-pending"}
        with patch("tools.intake.adapters.prepare", return_value={"pinned": "original"}), patch("tools.intake.adapters.execute", side_effect=effect):
            result = runner.run(self.root, "req-one", "delivery", now=0)
        self.assertEqual(result["stages"][0]["status"], "return-pending")
        self.assertFalse(result["complete"])

    def test_prepared_intent_commits_before_attempt(self):
        from unittest.mock import patch
        from tools.intake import runner
        self.stage_live()
        original = runner.save_artifact
        def persist(c, correlation, stage_id, key, value):
            if key == "intent":
                self.assertEqual(c.execute("select count(*) from attempts").fetchone()[0], 0)
            return original(c, correlation, stage_id, key, value)
        with patch.object(runner, "save_artifact", side_effect=persist), patch("tools.intake.adapters.prepare", return_value={"pinned": "original"}), patch("tools.intake.adapters.execute", return_value={"boundary": "return-pending"}):
            runner.run(self.root, "req-one", "delivery")

    def test_intent_ledger_failure_does_not_consume_native_attempt(self):
        from unittest.mock import patch
        from tools.intake import runner
        self.stage_live()
        with patch("tools.intake.adapters.prepare", return_value={"pinned": "original"}), patch.object(runner, "save_artifact", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaises(contracts.IntakeError):
                runner.run(self.root, "req-one", "delivery")
        self.assertEqual(store.inspect(self.root, "req-one")["stages"][0]["attempts"], [])

    def test_restart_ambiguous_attempt(self):
        from unittest.mock import patch
        from tools.intake import runner
        self.stage_live()
        artifact = self.root / "effect"
        def effect(root, stage, intent, previous):
            self.assertEqual(intent, {"pinned": "original"})
            if not artifact.exists():
                artifact.write_text("one")
                raise SystemExit("crash")
            return {"boundary": "return-pending"}
        with patch("tools.intake.adapters.prepare", return_value={"pinned": "original"}), patch("tools.intake.adapters.execute", side_effect=effect):
            with self.assertRaises(SystemExit):
                runner.run(self.root, "req-one", "delivery", now=0)
            result = runner.run(self.root, "req-one", "delivery", now=10)
        self.assertEqual(len(result["stages"][0]["attempts"]), 1)
        self.assertEqual(artifact.read_text(), "one")

    def test_retry_blocked_dead_letter_replay(self):
        from unittest.mock import patch
        from tools.intake import runner, adapters
        self.stage_live()
        with patch("tools.intake.adapters.prepare", return_value={"pinned": "original"}), patch("tools.intake.adapters.execute", side_effect=adapters.StageFailure("retryable", "busy")):
            self.assertEqual(runner.run(self.root, "req-one", "delivery", now=0)["stages"][0]["status"], "retryable")
            with self.assertRaises(contracts.IntakeError):
                runner.run(self.root, "req-one", "delivery", now=4)
            result = runner.run(self.root, "req-one", "delivery", now=5)
        self.assertEqual(result["stages"][0]["status"], "dead-letter")
        replayed = runner.replay(self.root, "req-one", "delivery", now=6)
        self.assertEqual(replayed["stages"][0]["cycle"], 1)
        with patch("tools.intake.adapters.execute", side_effect=adapters.StageFailure("blocked", "permission")):
            result = runner.run(self.root, "req-one", "delivery", now=7)
        self.assertEqual(result["stages"][0]["status"], "blocked")
        with self.assertRaises(contracts.IntakeError):
            runner.run(self.root, "req-one", "delivery", now=8)
        self.assertEqual(len(result["stages"][0]["attempts"]), 3)

    def test_failure_backoff_starts_at_completion(self):
        from unittest.mock import patch
        from tools.intake import runner, adapters
        self.stage_live()
        clock_value = [100]
        def execute(*args):
            clock_value[0] = 120
            raise adapters.StageFailure("retryable", "busy")
        with patch.object(adapters, "prepare", return_value={"pinned": "original"}), patch.object(adapters, "execute", side_effect=execute):
            result = runner.run(self.root, "req-one", "delivery", clock=lambda: clock_value[0])
        attempt = result["stages"][0]["attempts"][0]
        self.assertEqual(attempt["started"], 100)
        self.assertEqual(attempt["ended"], 120)
        self.assertEqual(attempt["eligible_at"], 125)
        with self.assertRaisesRegex(contracts.IntakeError, "backoff_pending"):
            runner.run(self.root, "req-one", "delivery", now=124)

    def test_success_records_completion_time(self):
        from unittest.mock import patch
        from tools.intake import runner, adapters
        self.stage_live()
        with patch.object(adapters, "prepare", return_value={"pinned": "original"}), patch.object(adapters, "execute", return_value={"boundary": "return-pending"}):
            result = runner.run(self.root, "req-one", "delivery", clock=iter([100, 120]).__next__)
        self.assertEqual(result["stages"][0]["attempts"][0]["ended"], 120)

    def test_return_pending_and_matching_ack(self):
        from tools.intake import runner
        from tools import policy
        path = self.root / "disposition.json"
        path.write_bytes(b"private disposition")
        raw = json.loads(request(policy={"version": 1, "sha256": policy.load()[1]}))
        raw["stages"][0]["arguments"]["disposition_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        raw["stages"][0]["arguments"]["delivery_correlation"] = "d" * 500
        store.stage(self.root, json.dumps(raw).encode())
        pending = runner.run(self.root, "req-one", "delivery")
        self.assertEqual(pending["stages"][0]["status"], "return-pending")
        ack = {"delivery_correlation": "d" * 500, "disposition_sha256": raw["stages"][0]["arguments"]["disposition_sha256"], "transport_receipt": "receipt-1"}
        with self.assertRaises(contracts.IntakeError):
            runner.acknowledge(self.root, "req-one", "delivery", {**ack, "delivery_correlation": "wrong"})
        self.assertTrue(runner.acknowledge(self.root, "req-one", "delivery", ack)["complete"])
        self.assertTrue(runner.acknowledge(self.root, "req-one", "delivery", ack)["complete"])
        with self.assertRaises(contracts.IntakeError):
            runner.acknowledge(self.root, "req-one", "delivery", {**ack, "transport_receipt": "receipt-2"})

    def test_return_artifact_changed_or_missing_blocks_ack(self):
        from tools.intake import runner
        from tools import policy
        path = self.root / "disposition.json"
        path.write_bytes(b"private disposition")
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        raw = json.loads(request(policy={"version": 1, "sha256": policy.load()[1]}))
        raw["stages"][0]["arguments"]["disposition_sha256"] = checksum
        store.stage(self.root, json.dumps(raw).encode())
        runner.run(self.root, "req-one", "delivery")
        path.unlink()
        with self.assertRaises(contracts.IntakeError):
            runner.acknowledge(self.root, "req-one", "delivery", {"delivery_correlation": "delivery-one", "disposition_sha256": checksum, "transport_receipt": "receipt"})
        self.assertFalse(store.inspect(self.root, "req-one")["complete"])

    def test_cli_clean_copy_end_to_end(self):
        from support import clone_release
        import subprocess
        import sys
        clone = self.root / "clone"
        clone_release(clone)
        instance = clone / "instance"
        instance.mkdir()
        def command(*args, expected=0):
            result = subprocess.run([sys.executable, "-m", "tools.intake.cli", "--root", str(instance), *args], cwd=clone, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            return json.loads(result.stdout)
        command("initialize")
        payload = clone / "request.json"
        payload.write_bytes(request())
        command("stage", "--request", str(payload))
        self.assertFalse(command("inspect", "req-one")["complete"])
        self.assertEqual(len(command("list")["requests"]), 1)
        self.assertEqual(command("run", "req-one", "--stage", "delivery")["stages"][0]["status"], "blocked")
        command("replay", "req-one", "--stage", "delivery")
        command("resume", "req-one", "--stage", "delivery")
        self.assertNotIn("private disposition", json.dumps(command("inspect", "req-one")))

    def test_concurrent_duplicate_execution(self):
        import multiprocessing
        from unittest.mock import patch
        from tools.intake import runner
        raw = self.stage_live()
        artifact = self.root / "effect"
        def effect(root, stage, intent, previous):
            with artifact.open("a") as stream:
                stream.write("one\n")
            return {"boundary": "return-pending"}
        def run():
            store.stage(self.root, raw)
            runner.run(self.root, "req-one", "delivery", now=0)
        with patch("tools.intake.adapters.prepare", return_value={"pinned": "original"}), patch("tools.intake.adapters.execute", side_effect=effect):
            processes = [multiprocessing.get_context("fork").Process(target=run) for _ in range(2)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        self.assertEqual(artifact.read_text(), "one\n")
