"""Usage recorder: body-free AgentsView session files for direct model calls."""
import json
import tempfile
import threading
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.intake.usage_log import Recorder


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def path(self):
        (path,) = self.root.glob("*.jsonl")
        return path

    def lines(self):
        return [json.loads(line) for line in self.path().read_text().splitlines()]

    def test_first_record_writes_header_name_and_mapped_usage(self):
        recorder = Recorder("librarian intake", "usai", directory=self.root, cwd="/work")
        recorder.record("m", {"prompt_tokens": 1000, "completion_tokens": 50,
                              "prompt_tokens_details": {"cached_tokens": 200}})
        session, info, message = self.lines()
        self.assertEqual((session["type"], session["version"], session["cwd"]),
                         ("session", 3, "/work"))
        self.assertEqual(info["type"], "session_info")
        self.assertEqual(info["name"], "librarian intake")
        self.assertEqual(info["parentId"], None)
        self.assertEqual(message["parentId"], info["id"])
        self.assertEqual(message["message"]["role"], "assistant")
        self.assertEqual(message["message"]["provider"], "usai")
        self.assertEqual(message["message"]["model"], "m")
        self.assertEqual(message["message"]["content"], [])
        self.assertEqual(message["message"]["usage"], {
            "input": 800, "output": 50, "cacheRead": 200, "cacheWrite": 0, "totalTokens": 1050})
        self.assertEqual(self.path().stem, session["id"])

    def test_no_record_means_no_file(self):
        Recorder("n", "usai", directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_or_inconsistent_usage_is_recorded_as_zero_or_clamped(self):
        recorder = Recorder("n", "usai", directory=self.root)
        recorder.record("m", None)
        recorder.record("m", {"prompt_tokens": 100,
                              "prompt_tokens_details": {"cached_tokens": 300}})
        _, _, empty, clamped = self.lines()
        self.assertEqual(empty["message"]["usage"], {
            "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0})
        self.assertEqual(clamped["message"]["usage"]["input"], 0)
        self.assertEqual(clamped["message"]["usage"]["cacheRead"], 300)

    def test_concurrent_records_share_one_well_formed_file(self):
        recorder = Recorder("n", "usai", directory=self.root)
        threads = [threading.Thread(target=lambda: [recorder.record("m", {}) for _ in range(5)])
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = self.lines()
        self.assertEqual(len(lines), 32)
        for previous, line in zip(lines[1:], lines[2:]):
            self.assertEqual(line["parentId"], previous["id"])

    def test_unwritable_directory_is_ignored(self):
        blocker = self.root / "file"
        blocker.write_text("x")
        self.assertIsNone(Recorder("n", "usai", directory=blocker).record("m", {}))

    def test_malformed_usage_is_ignored(self):
        recorder = Recorder("n", "usai", directory=self.root)
        for usage in ({"prompt_tokens": "many"}, {"prompt_tokens_details": 5}):
            self.assertIsNone(recorder.record("m", usage))

    def test_environment_directory_is_used_without_explicit_directory(self):
        with patch.dict(os.environ, {"AGENTSVIEW_DIRECT_API_DIR": str(self.root / "env")}):
            Recorder("n", "usai").record("m", {})
        self.assertEqual(len(list((self.root / "env").glob("*.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()
