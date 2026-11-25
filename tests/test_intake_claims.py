"""Claim, work, record (Kata 527a): the ledger writer lock is not held while a stage's native work runs.

A stage run commits its intent and a running attempt with a claim (owner and lease), releases the ledger
lock, does the native work, and retakes the lock only to record the evidence. A live claim keeps a second
process off the stage; an expired claim, or one whose process is gone, is taken over without charging the
attempt; a process that outlives its lease cannot record over its successor.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_intake_cohort as cohort_tests
from tests.support import default_policy_hash
from tests.test_intake import request
from tools.catalog import catalog_store as catalog
from tools.diagnostics.ingest_benchmark import fixture
from tools.intake import adapters, cohort, runner, store
from tools.intake.contracts import LedgerBusy

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"version": 1, "sha256": default_policy_hash()}


def ledger_is_free(root):
    """True when another writer could take the ledger lock right now."""
    try:
        with store.locked(root, wait=0):
            return True
    except LedgerBusy:
        return False


class Stage(unittest.TestCase):
    """A one-stage request whose preparation and effect are replaced, so the test decides what the effect does."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root)
        store.stage(self.root, request(policy=POLICY))
        self.calls, self.gates = [], {}
        self.errors = []

    def effect(self, root, stage, intent, previous):
        """The nth call blocks on ``gates[n]`` (when there is one) and returns evidence naming it."""
        number = len(self.calls) + 1
        self.calls.append(number)
        gate = self.gates.get(number)
        if gate is not None:
            gate["entered"].set()
            self.assertTrue(gate["release"].wait(30))
        return {"boundary": "return-pending", "call": number}

    def gate(self, number):
        self.gates[number] = {"entered": threading.Event(), "release": threading.Event()}
        return self.gates[number]

    def patched(self):
        stack = unittest.mock.patch.multiple(adapters, prepare=lambda root, stage: {"pinned": "original"},
                                              execute=self.effect)
        return stack

    def start(self, **kwargs):
        def work():
            try:
                runner.run(self.root, "req-one", "delivery", **kwargs)
            except BaseException as error:  # noqa: BLE001 - reported by the test thread
                self.errors.append(error)
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 30)
        return thread

    def stage_row(self):
        return store.inspect(self.root, "req-one")["stages"][0]

    def claims(self):
        with closing(sqlite3.connect(store.locations(self.root)[2])) as c:
            return c.execute("SELECT owner, lease_until FROM claims").fetchall()


class LockIsFreeDuringNativeWork(Stage):
    def test_the_ledger_lock_is_free_while_the_effect_runs_and_the_claim_is_visible(self):
        gate = self.gate(1)
        with self.patched():
            worker = self.start()
            self.assertTrue(gate["entered"].wait(30))
            self.assertTrue(ledger_is_free(self.root))  # was held for the whole effect before
            stage = self.stage_row()
            self.assertEqual(stage["status"], "running")
            self.assertIsNone(stage["attempts"][0]["outcome"])
            self.assertEqual(len(self.claims()), 1)
            gate["release"].set()
            worker.join(30)
        self.assertEqual(self.errors, [])
        done = self.stage_row()
        self.assertEqual((done["status"], len(done["attempts"]), done["evidence"]["call"]), ("return-pending", 1, 1))
        self.assertEqual(self.claims(), [])  # a settled attempt keeps no claim

    def test_the_ledger_lock_is_free_while_a_stage_is_prepared(self):
        free = []

        def prepare(root, stage):
            free.append(ledger_is_free(root))
            return {"pinned": "original"}
        with patch.object(adapters, "prepare", prepare), patch.object(adapters, "execute", self.effect):
            runner.run(self.root, "req-one", "delivery")
        self.assertEqual(free, [True])

    def test_a_failed_stage_records_its_failure_and_drops_its_claim(self):
        failure = adapters.StageFailure("retryable", "busy")
        with patch.object(adapters, "prepare", lambda root, stage: {"pinned": "original"}), \
                patch.object(adapters, "execute", side_effect=failure):
            runner.run(self.root, "req-one", "delivery", now=0)
        stage = self.stage_row()
        self.assertEqual((stage["status"], stage["attempts"][0]["code"]), ("retryable", "busy"))
        self.assertEqual(self.claims(), [])


class LiveClaims(Stage):
    def test_a_stage_whose_lease_is_live_is_skipped_and_its_effect_runs_once(self):
        gate = self.gate(1)
        with self.patched():
            worker = self.start(clock=lambda: 0)
            self.assertTrue(gate["entered"].wait(30))
            skipped = runner.run(self.root, "req-one", "delivery", clock=lambda: runner.LEASE_SECONDS - 1)
            self.assertEqual(skipped["stages"][0]["status"], "running")
            self.assertEqual(len(skipped["stages"][0]["attempts"]), 1)
            self.assertEqual(self.calls, [1])
            gate["release"].set()
            worker.join(30)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.stage_row()["status"], "return-pending")
        self.assertEqual(self.calls, [1])

    def test_a_stage_whose_lease_expired_is_taken_over_once_and_the_attempt_is_not_charged(self):
        first, second = self.gate(1), self.gate(2)
        with self.patched():
            one = self.start(clock=lambda: 0)
            self.assertTrue(first["entered"].wait(30))
            later = runner.LEASE_SECONDS + 1
            two = self.start(clock=lambda: later)
            self.assertTrue(second["entered"].wait(30))
            # the takeover holds a live lease now: a third process skips the stage
            third = runner.run(self.root, "req-one", "delivery", clock=lambda: later + 1)
            self.assertEqual(third["stages"][0]["status"], "running")
            self.assertEqual(self.calls, [1, 2])
            self.assertEqual(len(self.stage_row()["attempts"]), 1)  # reused, not charged
            second["release"].set()
            two.join(30)
            first["release"].set()
            one.join(30)
        self.assertEqual(self.errors, [])

    def test_a_process_that_outlives_its_lease_cannot_record_over_its_successor(self):
        first = self.gate(1)
        with self.patched():
            one = self.start(clock=lambda: 0)
            self.assertTrue(first["entered"].wait(30))
            taken = runner.run(self.root, "req-one", "delivery", clock=lambda: runner.LEASE_SECONDS + 1)
            self.assertEqual(taken["stages"][0]["evidence"]["call"], 2)  # the successor settled the stage
            first["release"].set()
            one.join(30)
        self.assertEqual(self.errors, [])
        stage = self.stage_row()
        self.assertEqual((stage["status"], len(stage["attempts"]), stage["evidence"]["call"]), ("return-pending", 1, 2))
        self.assertEqual(self.claims(), [])

    def test_a_late_failure_does_not_overwrite_a_successors_evidence(self):
        first = self.gate(1)
        real = self.effect

        def effect(root, stage, intent, previous):
            if not self.calls:
                real(root, stage, intent, previous)
                raise adapters.StageFailure("permanent", "late")
            return real(root, stage, intent, previous)
        with patch.object(adapters, "prepare", lambda root, stage: {"pinned": "original"}), \
                patch.object(adapters, "execute", effect):
            one = self.start(clock=lambda: 0)
            self.assertTrue(first["entered"].wait(30))
            runner.run(self.root, "req-one", "delivery", clock=lambda: runner.LEASE_SECONDS + 1)
            first["release"].set()
            one.join(30)
        self.assertEqual(self.errors, [])
        stage = self.stage_row()
        self.assertEqual((stage["status"], stage["attempts"][0]["outcome"]), ("return-pending", "return-pending"))


class ReviewFixes(Stage):
    """Findings of the one review of this change (job 971): backoff timing, lease timing, heartbeat, intent reconciliation."""

    def test_a_chunk_whose_preparation_fails_after_a_long_time_still_backs_off_from_the_failure(self):
        store.stage(self.root, request("req-b", policy=POLICY))
        times = iter([0, 100, 100, 100, 100])
        clock = lambda: next(times, 100)
        failure = adapters.StageFailure("retryable", "busy")

        def prepare(root, stage):
            raise failure
        with patch.object(adapters, "prepare", prepare):
            runner.run_batch(self.root, [("req-one", "delivery")], clock=clock)
        attempt = self.stage_row()["attempts"][0]
        self.assertEqual((attempt["ended"], attempt["eligible_at"]), (100, 105))  # from the failure, not from the start
        with patch.object(adapters, "prepare", lambda root, stage: {"pinned": "original"}), \
                patch.object(adapters, "execute_batch", lambda root, entries: [{"boundary": "return-pending"} for _ in entries]):
            again = runner.run_batch(self.root, [("req-one", "delivery")], clock=lambda: 101)
        self.assertEqual(len(again["req-one"]["stages"][0]["attempts"]), 1)  # still in backoff: not retried
        self.assertEqual(again["req-one"]["stages"][0]["status"], "retryable")

    def test_the_lease_runs_from_the_claim_not_from_the_start_of_a_slow_preparation(self):
        seen = []

        def slow_prepare(root, stage):
            time.sleep(1.2)
            return {"pinned": "original"}

        def execute_batch(root, entries):
            seen.append(self.claims()[0][1])
            return [{"boundary": "return-pending"} for _ in entries]
        with patch.object(runner, "LEASE_SECONDS", 1), patch.object(adapters, "prepare", slow_prepare), \
                patch.object(adapters, "execute_batch", execute_batch):
            runner.run_batch(self.root, [("req-one", "delivery")], clock=lambda: 0)
        self.assertGreaterEqual(seen[0], 2.1)  # 1.2 s of preparation plus a full 1 s lease, not 1 s from the start

    def test_a_run_in_native_work_renews_its_lease_so_a_long_chunk_is_not_taken_over(self):
        gate = self.gate(1)
        with patch.object(runner, "LEASE_SECONDS", 1), patch.object(runner, "HEARTBEAT_SECONDS", 0.1), self.patched():
            worker = self.start(clock=lambda: 0)
            self.assertTrue(gate["entered"].wait(30))
            first = self.claims()[0][1]
            deadline = time.monotonic() + 10
            while self.claims()[0][1] <= first and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreater(self.claims()[0][1], first)
            gate["release"].set()
            worker.join(30)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.claims(), [])  # the renewal ends with the run and never resurrects a claim

    def test_a_stage_another_run_pinned_meanwhile_runs_under_that_intent_not_a_stale_preparation(self):
        efforts, intents = [], []

        def prepare(root, stage):
            if not efforts:
                efforts.append(1)
                with patch.object(adapters, "prepare", lambda r, s: {"pinned": "other"}), \
                        patch.object(adapters, "execute", side_effect=adapters.StageFailure("retryable", "busy")):
                    runner.run(self.root, "req-one", "delivery", clock=lambda: 0)  # pins its intent, then fails
                return {"pinned": "mine"}
            return {"pinned": "unexpected"}

        def execute(root, stage, intent, previous):
            intents.append(intent)
            return {"boundary": "return-pending"}
        with patch.object(adapters, "prepare", prepare), patch.object(adapters, "execute", execute):
            runner.run(self.root, "req-one", "delivery", clock=lambda: 100)
        self.assertEqual(intents, [{"pinned": "other"}])
        stage = self.stage_row()
        self.assertEqual((stage["status"], len(stage["attempts"])), ("return-pending", 2))

    def test_a_preparation_failure_that_another_run_made_moot_does_not_fail_the_stage(self):
        efforts = []

        def prepare(root, stage):
            if not efforts:
                efforts.append(1)
                with patch.object(adapters, "prepare", lambda r, s: {"pinned": "other"}), \
                        patch.object(adapters, "execute", side_effect=adapters.StageFailure("retryable", "busy")):
                    runner.run(self.root, "req-one", "delivery", clock=lambda: 0)
                raise adapters.StageFailure("permanent", "stale-preparation")
            return {"pinned": "unexpected"}
        with patch.object(adapters, "prepare", prepare), patch.object(adapters, "execute", self.effect):
            runner.run(self.root, "req-one", "delivery", clock=lambda: 100)
        self.assertEqual(self.stage_row()["status"], "return-pending")
        self.assertEqual(self.calls, [1])

    def test_a_chunk_runs_a_stage_another_run_pinned_meanwhile_under_that_intent(self):
        intents, efforts = [], []

        def prepare(root, stage):
            if not efforts:
                efforts.append(1)
                with patch.object(adapters, "prepare", lambda r, s: {"pinned": "other"}), \
                        patch.object(adapters, "execute", side_effect=adapters.StageFailure("retryable", "busy")):
                    runner.run(self.root, "req-one", "delivery", clock=lambda: 0)
                return {"pinned": "mine"}
            return {"pinned": "unexpected"}

        def execute_batch(root, entries):
            intents.extend(intent for _, intent, _ in entries)
            return [{"boundary": "return-pending"} for _ in entries]
        with patch.object(adapters, "prepare", prepare), patch.object(adapters, "execute_batch", execute_batch):
            runner.run_batch(self.root, [("req-one", "delivery")], clock=lambda: 100)
        self.assertEqual(intents, [{"pinned": "other"}])


class DeadOwners(Stage):
    def killed_run(self):
        script = ("import os, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                  "from tools.intake import adapters, runner\n"
                  "adapters.prepare = lambda root, stage: {'pinned': 'original'}\n"
                  "adapters.execute = lambda *a, **k: os._exit(9)\n"
                  "runner.run(Path(%r), 'req-one', 'delivery')\n") % (str(ROOT), str(self.root))
        self.assertEqual(subprocess.run([sys.executable, "-c", script], cwd=ROOT).returncode, 9)

    def test_a_kill_mid_effect_leaves_a_running_attempt_that_the_restart_takes_over_at_once_uncharged(self):
        self.killed_run()
        stage = self.stage_row()
        self.assertEqual((stage["status"], len(stage["attempts"]), stage["attempts"][0]["outcome"]), ("running", 1, None))
        self.assertEqual(len(self.claims()), 1)  # the dead process's lease is still in the future
        with self.patched():
            runner.run(self.root, "req-one", "delivery")
        stage = self.stage_row()
        self.assertEqual((stage["status"], len(stage["attempts"]), self.calls), ("return-pending", 1, [1]))
        self.assertEqual(self.claims(), [])

    def test_an_unfinished_attempt_of_a_ledger_that_never_had_claims_is_taken_over_and_gains_the_table(self):
        self.killed_run()
        with closing(sqlite3.connect(store.locations(self.root)[2])) as c, c:
            c.execute("DROP TABLE claims")
        with self.patched():
            runner.run(self.root, "req-one", "delivery")
        stage = self.stage_row()
        self.assertEqual((stage["status"], len(stage["attempts"])), ("return-pending", 1))
        self.assertEqual(self.claims(), [])  # the table exists again

    def test_a_ledger_that_never_had_claims_gains_the_table_without_losing_data(self):
        with closing(sqlite3.connect(store.locations(self.root)[2])) as c, c:
            c.execute("DROP TABLE claims")
        self.assertEqual(store.inspect(self.root, "req-one")["stages"][0]["status"], "staged")
        self.assertEqual(self.claims(), [])
        self.assertEqual(store.audit(self.root), {"audited": True, "requests": 1})


class TwoProcesses(Stage):
    def test_a_second_process_proceeds_while_the_first_is_in_native_work(self):
        mark = self.root / "entered"
        done = self.root / "release"
        counter = self.root / "effects"
        script = ("import sys, time\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                  "from tools.intake import adapters, runner\n"
                  "def effect(root, stage, intent, previous):\n"
                  "    with open(%r, 'a') as f: f.write('x')\n"
                  "    Path(%r).write_text('in')\n"
                  "    while not Path(%r).exists(): time.sleep(0.02)\n"
                  "    return {'boundary': 'return-pending'}\n"
                  "adapters.prepare = lambda root, stage: {'pinned': 'original'}\n"
                  "adapters.execute = effect\n"
                  "runner.run(Path(%r), 'req-one', 'delivery')\n") % (
                      str(ROOT), str(counter), str(mark), str(done), str(self.root))
        first = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT)
        self.addCleanup(first.wait, 30)
        deadline = time.monotonic() + 60
        while not mark.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(mark.exists(), "the first process never reached its effect")
        try:
            # reads, other requests and a second run of the same stage all proceed at once
            started = time.monotonic()
            self.assertEqual(self.stage_row()["status"], "running")
            store.stage(self.root, request("req-two", policy=POLICY))
            with patch.object(adapters, "prepare", lambda root, stage: {"pinned": "original"}), \
                    patch.object(adapters, "execute", self.effect):
                other = runner.run(self.root, "req-two", "delivery")
                again = runner.run(self.root, "req-one", "delivery")
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(other["stages"][0]["status"], "return-pending")
            self.assertEqual(again["stages"][0]["status"], "running")  # skipped: the first process's lease is live
            self.assertEqual(self.calls, [1])  # only req-two's effect ran here
        finally:
            done.write_text("go")
        first.wait(60)
        self.assertEqual(self.stage_row()["status"], "return-pending")
        self.assertEqual(counter.read_text(), "x")  # one process ran the stage's effect
        self.assertEqual(len(self.stage_row()["attempts"]), 1)


class BatchClaims(unittest.TestCase):
    NAMES = ("req-a", "req-b", "req-c")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root)
        for name in self.NAMES:
            store.stage(self.root, request(name, policy=POLICY))
        self.items = [(name, "delivery") for name in self.NAMES]
        self.ran = []

    def execute_batch(self, root, entries):
        self.ran.append(len(entries))
        return [{"boundary": "return-pending"} for _ in entries]

    def patched(self, **extra):
        return patch.multiple(adapters, prepare=lambda root, stage: {"pinned": "original"},
                              execute_batch=extra.get("execute_batch", self.execute_batch))

    def test_the_ledger_lock_is_free_while_a_chunk_is_prepared_and_executed(self):
        free = []

        def prepare(root, stage):
            free.append(ledger_is_free(root))
            return {"pinned": "original"}

        def execute_batch(root, entries):
            free.append(ledger_is_free(root))
            return [{"boundary": "return-pending"} for _ in entries]
        with patch.object(adapters, "prepare", prepare), patch.object(adapters, "execute_batch", execute_batch):
            result = runner.run_batch(self.root, self.items)
        self.assertEqual(free, [True] * 4)
        self.assertEqual({r["stages"][0]["status"] for r in result.values()}, {"succeeded"})

    def test_a_chunk_skips_a_stage_another_process_holds_and_runs_the_rest(self):
        entered, release = threading.Event(), threading.Event()
        order = []

        def execute_batch(root, entries):
            order.append(len(entries))
            if len(order) == 1:
                entered.set()
                release.wait(30)
            return [{"boundary": "return-pending"} for _ in entries]
        with self.patched(execute_batch=execute_batch):
            holder = threading.Thread(target=runner.run_batch, args=(self.root, [self.items[0]]), kwargs={"clock": lambda: 0})
            holder.start()
            self.addCleanup(holder.join, 30)
            self.assertTrue(entered.wait(30))
            result = runner.run_batch(self.root, self.items, clock=lambda: 1)
            self.assertEqual(order, [1, 2])  # only the two unclaimed stages ran
            statuses = [result[name]["stages"][0]["status"] for name in self.NAMES]
            self.assertEqual(statuses, ["running", "succeeded", "succeeded"])
            release.set()
            holder.join(30)
        self.assertEqual(store.inspect(self.root, "req-a")["stages"][0]["status"], "succeeded")

    def test_a_chunk_takes_over_an_expired_claim_without_charging_and_the_old_owner_cannot_record(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def execute_batch(root, entries):
            calls.append(len(entries))
            if len(calls) == 1:
                entered.set()
                release.wait(30)
                return [{"boundary": "return-pending", "who": "old"} for _ in entries]
            return [{"boundary": "return-pending", "who": "new"} for _ in entries]
        with self.patched(execute_batch=execute_batch):
            holder = threading.Thread(target=runner.run_batch, args=(self.root, [self.items[0]]), kwargs={"clock": lambda: 0})
            holder.start()
            self.addCleanup(holder.join, 30)
            self.assertTrue(entered.wait(30))
            result = runner.run_batch(self.root, [self.items[0]], clock=lambda: runner.LEASE_SECONDS + 1)
            release.set()
            holder.join(30)
        stage = store.inspect(self.root, "req-a")["stages"][0]
        self.assertEqual((stage["status"], len(stage["attempts"]), stage["evidence"]["who"]), ("succeeded", 1, "new"))
        self.assertEqual(result["req-a"]["stages"][0]["evidence"]["who"], "new")

    def test_a_kill_in_a_chunks_native_work_is_taken_over_uncharged(self):
        script = ("import os, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                  "from tools.intake import adapters, runner\n"
                  "adapters.prepare = lambda root, stage: {'pinned': 'original'}\n"
                  "adapters.execute_batch = lambda *a, **k: os._exit(9)\n"
                  "runner.run_batch(Path(%r), %r)\n") % (str(ROOT), str(self.root), self.items)
        self.assertEqual(subprocess.run([sys.executable, "-c", script], cwd=ROOT).returncode, 9)
        self.assertEqual([r["stages"][0]["status"] for r in store.list_requests(self.root)], ["running"] * 3)
        with self.patched():
            result = runner.run_batch(self.root, self.items)
        self.assertEqual(self.ran, [3])
        for name in self.NAMES:
            stage = result[name]["stages"][0]
            self.assertEqual((stage["status"], len(stage["attempts"])), ("succeeded", 1))


class IngestKeepsTheLedgerFree(unittest.TestCase):
    def make(self, files, **kind):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root, inbox = base / "instance", base / "inbox"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        fixture(inbox, files, mixed=True)
        return root, inbox

    def test_no_native_step_of_an_ingest_runs_under_the_ledger_lock(self):
        for batch_size in (1, 4):
            with self.subTest(batch_size=batch_size):
                root, inbox = self.make(6)
                held = []

                def guard(real, held=held):
                    def wrapped(*args, **kwargs):
                        held.append(not ledger_is_free(args[0]))
                        return real(*args, **kwargs)
                    return wrapped
                with patch.object(adapters, "prepare", guard(adapters.prepare)), \
                        patch.object(adapters, "execute", guard(adapters.execute)), \
                        patch.object(adapters, "execute_batch", guard(adapters.execute_batch)):
                    report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=batch_size)
                self.assertEqual(report["counts"]["outcome"]["extracted"], 6)
                self.assertTrue(held)
                self.assertFalse(any(held), "a native step ran while the ledger lock was held")

    def test_the_longest_ledger_hold_of_a_chunk_does_not_include_its_native_work(self):
        root, inbox = self.make(8)
        holds, real = [], store.locked

        def timed(root_, wait=None):
            import contextlib

            @contextlib.contextmanager
            def hold():
                with real(root_, wait) as c:
                    start = time.perf_counter()
                    try:
                        yield c
                    finally:
                        holds.append(time.perf_counter() - start)
            return hold()
        slow = 0.4

        def slowly(real_call):
            def wrapped(*args, **kwargs):
                time.sleep(slow)
                return real_call(*args, **kwargs)
            return wrapped
        with patch.object(store, "locked", timed), \
                patch.object(adapters, "prepare", slowly(adapters.prepare)), \
                patch.object(adapters, "execute_batch", slowly(adapters.execute_batch)):
            report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=8)
        self.assertEqual(report["counts"]["outcome"]["extracted"], 8)
        self.assertLess(max(holds), slow)

    def test_converted_office_documents_prepare_in_the_chunks_batch_path(self):
        from tests.test_convert import docx
        calls = []
        real = adapters.execute_batch

        def spy(root, entries):
            calls.append([(stage["operation"], stage["arguments"].get("format_name")) for stage, _, _ in entries])
            return real(root, entries)
        commits = []
        for count in (2, 4):
            root, inbox = self.make(0)
            inbox.mkdir(parents=True, exist_ok=True)
            for number in range(count):
                body = docx(f"Synthetic paragraph {number}")
                (inbox / f"doc-{number}.docx").write_bytes(body)
            before = int.from_bytes(store.locations(root)[2].read_bytes()[24:28], "big")
            calls.clear()
            with patch.object(adapters, "execute_batch", spy):
                report = cohort.ingest(root, inbox, system="s", actor="a", batch_size=count)
            self.assertEqual(report["counts"]["outcome"]["extracted"], count, report["counts"])
            prepared = [entry for call in calls for entry in call if entry[0] == "extract-prepare"]
            self.assertEqual(prepared, [("extract-prepare", "docx")] * count)
            commits.append(int.from_bytes(store.locations(root)[2].read_bytes()[24:28], "big") - before)
        self.assertEqual(commits[0], commits[1])  # a chunk's ledger commits do not grow with its converted documents


class QualifyStagesKeepTheLedgerLock(cohort_tests.QualifyCase):
    """``qualify``'s stages commit against a pinned review-event revision and write the shared selection manifest,
    so they keep the ledger lock (the decision for the review path); the model call is made outside it already."""

    def ingest(self):
        return cohort.ingest(self.root, self.inbox, system="example-docs-inbox", actor="cohort-runner")

    def test_the_review_event_commit_and_the_manifest_append_run_under_the_ledger_lock_and_are_brief(self):
        from tools.intake import standing_publication
        from tools.review_events import event_store as events
        seen, holds = {}, {}

        def watch(label, real):
            def wrapped(*args, **kwargs):
                seen.setdefault(label, []).append(ledger_is_free(self.root))
                start = time.perf_counter()
                try:
                    return real(*args, **kwargs)
                finally:
                    holds.setdefault(label, []).append(time.perf_counter() - start)
            return wrapped
        self.ingest()
        transport, _ = self._transport()
        with patch.object(events, "append", watch("review", events.append)), \
                patch.object(standing_publication, "write_manifest",
                             watch("manifest", standing_publication.write_manifest)):
            report = self._qualify(transport, system="example-docs-inbox")
        self.assertEqual(report["counts"]["qualification"], {"qualified": 6})
        for label in ("review", "manifest"):
            self.assertTrue(seen[label])
            self.assertEqual(set(seen[label]), {False}, label)
            self.assertLess(max(holds[label]), 1.0, label)  # a stub-transport hold is milliseconds, not seconds


class LockOrder(unittest.TestCase):
    def test_no_catalog_review_event_or_extraction_module_takes_the_ledger_lock(self):
        # The ledger lock is taken before any catalog lock (a stage's work takes the catalog lock alone, with no
        # ledger lock held); no path takes the catalog lock first because nothing under tools/ outside
        # tools/intake reaches the ledger.
        offenders = []
        for package in ("catalog", "review_events", "extraction", "context_index"):
            for path in (ROOT / "tools" / package).glob("*.py"):
                text = path.read_text(encoding="utf-8")
                if "tools.intake" in text or "from tools import intake" in text:
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
