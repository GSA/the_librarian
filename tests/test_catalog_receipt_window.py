"""A reader that lands between a writer's commit and its receipt waits for the receipt, never replays for it (Kata 17b0).

The writer holds the writer lock from COMMIT through the attestation of the new bytes. A reader that
meets the new file with a receipt for the old one is in that window: it ends its read transaction,
waits boundedly for the writer to finish, and trusts the receipt only if it then covers exactly the
file. Anything else (no writer, a writer that never attests, an edited file) still gets the full replay.
"""
from contextlib import closing
import threading
import time
from unittest.mock import patch

from tools.catalog import catalog_store as catalog
from tools.catalog import storage
from tools.catalog.contracts import CatalogError
from tests.test_catalog_delta_commit import Case
from tests.test_file_identity_locks import needs_dbpage


class ReceiptWindow(Case):
    def setUp(self):
        super().setUp()
        for n in range(3):
            self.write(n)

    def commit_then_hold(self, hold, attest=True):
        """Start a write whose attestation is delayed ``hold`` seconds after its commit."""
        real = catalog._attest_commit
        committed = threading.Event()

        def late(*args, **kwargs):
            committed.set()
            time.sleep(hold)
            if attest:
                real(*args, **kwargs)
        patcher = patch.object(catalog, "_attest_commit", late)
        patcher.start()
        self.addCleanup(patcher.stop)
        state = catalog.status(self.root)["state_token"]
        writer = threading.Thread(target=lambda: catalog.submit(
            self.root, catalog_records(5), actor="a", request_id="r5", reason="test", expected_state=state))
        writer.start()
        self.addCleanup(writer.join)
        self.assertTrue(committed.wait(10))
        return writer

    @needs_dbpage
    def test_reader_in_the_commit_to_attestation_window_waits_instead_of_replaying(self):
        self.commit_then_hold(0.6)
        calls, patched = self.writes_inspect()
        with patched:
            state = catalog.status(self.root)
        self.assertEqual(state["revision"], 4)
        self.assertEqual(calls, [])

    @needs_dbpage
    def test_all_reads_share_the_wait(self):
        self.commit_then_hold(0.6)
        calls, patched = self.writes_inspect()
        with patched:
            catalog.read_records(self.root, limit=1)
            catalog.receipt_metadata(self.root, ["D0"])
        self.assertEqual(calls, [])

    @needs_dbpage
    def test_a_writer_that_never_attests_costs_the_full_replay_after_the_wait(self):
        self.commit_then_hold(0.3, attest=False)
        calls, patched = self.writes_inspect()
        with patched:
            self.assertEqual(catalog.status(self.root)["revision"], 4)
        self.assertEqual(len(calls), 1)

    @needs_dbpage
    def test_an_edited_file_is_still_refused_after_the_wait(self):
        self.mutate("UPDATE versions SET payload_hash='0' WHERE record_id='S0'")
        held = threading.Event()
        release = threading.Event()

        def hold():
            with storage.writer_lock(self.home):
                held.set()
                release.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.assertTrue(held.wait(10))
        threading.Timer(0.3, release.set).start()
        with self.assertRaises(CatalogError):
            catalog.status(self.root)

    @needs_dbpage
    def test_the_wait_is_bounded(self):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with storage.writer_lock(self.home):
                held.set()
                release.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(10))
        self.mutate_receipt_binding()
        calls, patched = self.writes_inspect()
        started = time.monotonic()
        with patched, patch.object(catalog, "ATTESTATION_WAIT_SECONDS", 0.3):
            catalog.status(self.root)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(calls), 1)

    def mutate_receipt_binding(self):
        """Make the receipt describe a different file, as a stale receipt does."""
        import json
        saved = json.loads(self.receipt.read_text())
        saved["binding"]["identity"][2] += 4096
        saved.pop("checksum")
        saved["checksum"] = catalog.fingerprint(saved)
        self.receipt.write_text(catalog.canonical(saved) + "\n")


from tools.diagnostics.catalog_lock_benchmark import registration as catalog_records  # noqa: E402
