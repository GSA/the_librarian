"""Source-only pre-commit gate regressions; all fixtures are synthetic."""

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
GATE = HERE / "roborev_precommit.py"
INSTALL = HERE / "install_hooks.py"
HOOK = HERE.parents[1] / ".githooks/pre-commit"
SOURCE = "tools/extraction/docling_worker.py"
ADAPTER = "tools/extraction/docling_adapter.py"
BRIDGE = "tools/extraction/docling_bridge.py"


class PrecommitTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(GATE.is_file(), "staged-only review gate is not implemented")
        private = HERE.parents[1] / ".local/review-tests"
        private.mkdir(parents=True, exist_ok=True)
        self.workspace = tempfile.TemporaryDirectory(prefix="case-", dir=private)
        self.addCleanup(self.workspace.cleanup)
        self.home = Path(self.workspace.name)
        self.root = self.home / "repo"
        self.root.mkdir()
        self.env = {
            "PATH": os.defpath,
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PRIVATE_TEST_SECRET": "must-not-be-inherited",
        }
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / ".git/info/exclude").write_text(".local/\n")
        self.write(SOURCE, "def identity(value):\n    return value\n")
        self.write(ADAPTER, "LIMIT = 1\n")
        self.write("INBOX/private.txt", "synthetic excluded source\n")
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic base")
        self.git("remote", "add", "origin", "https://example.invalid/private")
        self.record = self.home / "observed.json"
        self.launcher = self.home / "launcher"
        self.fake()
        self.git("config", "--local", "roborev.precommitLauncher", str(self.launcher))

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], env=self.env,
            capture_output=True, check=check,
        )

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def stage(self, name=SOURCE, text="def identity(value):\n    return str(value)\n"):
        self.write(name, text)
        self.git("add", "--", name)

    def fake(self, exit_code=0, status="done", verdict=1, mutation="", jobs=True,
             scrub_git_env=False, attributes_file=None, output="", foreign_job=False):
        script = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys
root = pathlib.Path.cwd()
record = pathlib.Path({str(self.record)!r})
def git(*args):
    env = {{k: v for k, v in os.environ.items() if not ({scrub_git_env!r} and k.startswith('GIT_'))}}
    extra = ['-c', 'core.attributesFile=' + {str(attributes_file)!r}] if {attributes_file is not None!r} else []
    return subprocess.check_output(['git', *extra, *args], env=env).decode()
def blob_hex(ref):
    return {{p: git('show', ref + p).encode().hex()
            for p in git('ls-files').splitlines()}}
job = {{'id': 42, 'status': {status!r}, 'repo_path': str(root),
       'git_ref': 'dirty', 'agent': 'pi'}}
if {foreign_job!r}:
    job['repo_path'] = '/unrelated/repository'
if sys.argv[1] == 'review':
    files = {{str(p.relative_to(root)): p.read_text() for p in root.rglob('*')
             if p.is_file() and '.git' not in p.relative_to(root).parts}}
    base = {{p: git('show', 'HEAD:' + p) for p in git('ls-tree', '-r', '--name-only', 'HEAD').splitlines()}}
    record.write_text(json.dumps({{
        'repo': str(root), 'files': files, 'base': base,
        'base_hex': {{p: git('show', 'HEAD:' + p).encode().hex() for p in base}},
        'index_hex': blob_hex(':'),
        'files_hex': {{str(p.relative_to(root)): p.read_bytes().hex() for p in root.rglob('*')
                      if p.is_file() and '.git' not in p.relative_to(root).parts}},
        'diff': git('diff', '--cached', '--binary', 'HEAD'),
        'remote': git('remote'), 'hooks': git('config', '--get', 'core.hooksPath').strip(),
        'secret': os.environ.get('PRIVATE_TEST_SECRET'),
        'index_env': os.environ.get('GIT_INDEX_FILE'),
        'dir_env': os.environ.get('GIT_DIR'),
        'worktree_env': os.environ.get('GIT_WORK_TREE'),
        'config_env': os.environ.get('GIT_CONFIG_COUNT'),
        'argv': sys.argv[1:], 'mode': root.stat().st_mode & 0o777,
    }}))
    {mutation or 'pass'}
    sys.exit({exit_code})
elif sys.argv[1] == 'list':
    print(json.dumps([job] if {jobs!r} else []))
elif sys.argv[1] == 'show':
    prompt = '### Diff\\n\\n```diff\\n' + git('diff', 'HEAD', '--', '.') + '```\\n'
    print(json.dumps({{'job_id': 42, 'agent': 'pi', 'verdict_bool': {verdict!r}, 'job': job, 'prompt': prompt, 'output': {output!r}}}))
else:
    sys.exit(97)
"""
        self.launcher.write_text(script)
        self.launcher.chmod(0o700)

    def run_gate(self, extra_env=None, stdin_text=None):
        return subprocess.run(
            [sys.executable, str(GATE), "--root", str(self.root)],
            env={**self.env, **(extra_env or {})}, capture_output=True, text=True, input=stdin_text,
        )

    def observed(self):
        return json.loads(self.record.read_text())

    def assert_blocked(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)

    def install_files(self):
        hook = self.root / ".githooks/pre-commit"
        hook.parent.mkdir(exist_ok=True)
        shutil.copy2(HOOK, hook)
        for path in (GATE, INSTALL):
            destination = self.root / "tools/review" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    def installer(self):
        return subprocess.run(
            [sys.executable, str(INSTALL), "--root", str(self.root),
             "--launcher", str(self.launcher)],
            env=self.env, capture_output=True, text=True,
        )

    def test_staged_bytes_not_unstaged_or_untracked(self):
        self.stage(text="STAGED = 2\n")
        self.write(SOURCE, "UNSTAGED = 3\n")
        self.write(BRIDGE, "UNTRACKED = 4\n")
        index = (self.root / ".git/index").read_bytes()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.observed()
        self.assertEqual(data["files"][SOURCE], "STAGED = 2\n")
        self.assertNotIn(BRIDGE, data["files"])
        self.assertEqual(data["base"][SOURCE], "def identity(value):\n    return value\n")
        self.assertEqual(index, (self.root / ".git/index").read_bytes())
        self.assertIn("selected staged technical scope ONLY", result.stdout)

    def fixture_attributes(self, value):
        path = self.home / ".config/git/attributes"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_user_attributes_cannot_hide_python_hunks(self):
        self.stage(text="VISIBLE_STAGED_CODE = 27\n")
        attributes = self.fixture_attributes("*.py -diff\n")
        before = attributes.read_bytes()
        self.fake(scrub_git_env=True)
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.observed()
        self.assertIn("+VISIBLE_STAGED_CODE = 27\n", data["diff"])
        self.assertNotIn("GIT binary patch", data["diff"])
        self.assertNotIn("Binary files ", data["diff"])
        self.assertEqual(attributes.read_bytes(), before)

    def test_user_attributes_cannot_normalize_staged_crlf(self):
        base = b"BASE_BYTES = 1\r\n"
        current = b"STAGED_BYTES = 2\r\n"
        (self.root / SOURCE).write_bytes(base)
        self.git("add", SOURCE)
        self.git("commit", "-qm", "CRLF base")
        (self.root / SOURCE).write_bytes(current)
        self.git("add", SOURCE)
        self.assertEqual(self.git("show", f"HEAD:{SOURCE}").stdout, base)
        self.assertEqual(self.git("show", f":{SOURCE}").stdout, current)
        attributes = self.fixture_attributes("*.py text eol=lf\n")
        before = attributes.read_bytes()
        self.fake(scrub_git_env=True)
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.observed()
        self.assertEqual(bytes.fromhex(data["base_hex"][SOURCE]), base)
        self.assertEqual(bytes.fromhex(data["index_hex"][SOURCE]), current)
        self.assertEqual(bytes.fromhex(data["files_hex"][SOURCE]), current)
        self.assertEqual(self.git("show", f":{SOURCE}").stdout, current)
        self.assertEqual(attributes.read_bytes(), before)

    def test_repo_attributes_pin_reviewers_with_another_attribute_source(self):
        current = b"VISIBLE_CRLF_BYTES = 17\r\n"
        (self.root / SOURCE).write_bytes(current)
        self.git("add", SOURCE)
        attributes = self.fixture_attributes("*.py -diff text eol=lf\n")
        before = attributes.read_bytes()
        # Emulate an independent reader's lower-priority attribute source
        # without writing any actual system attributes or configuration.
        self.fake(scrub_git_env=True, attributes_file=attributes)
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.observed()
        self.assertIn("+VISIBLE_CRLF_BYTES = 17\r\n", data["diff"])
        self.assertEqual(bytes.fromhex(data["index_hex"][SOURCE]), current)
        self.assertEqual(attributes.read_bytes(), before)

    def load_gate(self):
        spec = importlib.util.spec_from_file_location("gate_under_test", GATE)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        return gate

    def test_snapshot_verification_detects_base_index_and_worktree_changes(self):
        gate = self.load_gate()
        base = {SOURCE: ("100644", b"BASE = 1\r\n")}
        current = {SOURCE: ("100644", b"CURRENT = 2\r\n")}
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            for layer in ("base", "index", "worktree"):
                with self.subTest(layer=layer):
                    run = self.home / layer
                    run.mkdir()
                    repo = gate.make_snapshot(run, base, current)
                    (repo / SOURCE).write_bytes(b"CORRUPTED = 3\n")
                    if layer != "worktree":
                        gate.git(repo, "add", "--all", source=False)
                    if layer == "base":
                        gate.git(repo, "commit", "-qm", "altered fixture base", source=False)
                    with self.assertRaisesRegex(gate.GateError, "snapshot .* bytes"):
                        gate.verify_snapshot(repo, base, current)

    def test_nontextual_or_incomplete_snapshot_diff_is_refused(self):
        gate = self.load_gate()
        base = {SOURCE: ("100644", b"BASE = 1\n")}
        current = {SOURCE: ("100644", b"CURRENT = 2\n")}
        run = self.home / "snapshot"
        run.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            repo = gate.make_snapshot(run, base, current)
            original_git = gate.git
            for hidden in (b"Binary files a/file.py and b/file.py differ\n", b""):
                def altered_git(root, *args, **kwargs):
                    if args == ("diff", "HEAD", "--", "."):
                        return subprocess.CompletedProcess([], 0, stdout=hidden)
                    return original_git(root, *args, **kwargs)
                with self.subTest(hidden=hidden), mock.patch.object(gate, "git", altered_git):
                    with self.assertRaisesRegex(gate.GateError, "incomplete or nontextual"):
                        gate.snapshot_diff(repo)

    def test_review_side_hidden_diff_is_refused(self):
        self.stage()
        self.fake(mutation="(root / '.git/info/attributes').write_text('* -diff\\n')")
        self.assert_blocked(self.run_gate(), "stored review diff differs")

    def test_explicit_allowlist_excludes_dirty_tools_documents_secrets(self):
        self.stage()
        excluded = [
            "INBOX/private.txt", "REFERENCE/program/private.md", ".env",
            "tools/other/private.py", "tools/extraction/private.pdf",
            "tools/extraction/private.py", "tools/extraction/.credentials.json",
            "docs/private.md", "docs/decisions/private.md", "config/policy.local.json",
            "tools/catalog/.local/catalog.sqlite3", "data/extractions/private.json",
            "tools/extraction/.local/models/config.json", "tests/private.py",
        ]
        for name in excluded:
            self.write(name, "SYNTHETIC_PRIVATE_MARKER\n")
            self.git("add", "-f", "--", name)
        result = self.run_gate()
        self.assert_blocked(result, "Mixed staged scope")
        self.assertFalse(self.record.exists())
        for name in excluded:
            self.assertIn(f"NOT reviewed: {name}", result.stdout)

    def test_final_task_exact_paths_are_reviewed_without_private_neighbors(self):
        selected = {
            "tests/test_kit.py", "release-files.json", "tools/extraction/README.md",
            "tools/extraction/extract.py",
            "tools/catalog/catalog_store.py", "tools/context-index/context_index.py",
            "tools/context-index/renditions.py",
            "tools/review-events/event_store.py", "tools/schemas/catalog.sql",
            "tools/schemas/context-index.sql", "tools/schemas/review-events.sql",
            "README.md", "ROADMAP.md", "docs/decisions/0001-project-identity.md",
            "docs/decisions/0003-store-compatibility.md",
            "docs/decisions/0008-proof-of-concept-closure.md", "docs/decisions/README.md",
            "docs/superpowers/plans/2026-10-20-docling-ingestion.md",
            "docs/superpowers/plans/2026-10-22-ingestion-completion.md",
            "docs/superpowers/specs/2026-10-22-ingestion-completion-design.md",
        }
        excluded = {"docs/decisions/private.md", "tools/schemas/private.sql",
                    "tools/catalog/private.py", "tests/private.py"}
        for name in selected:
            self.stage(name, "SELECTED_FINAL_TASK_BYTES\n")
            self.write(name, "UNSTAGED_FINAL_TASK_BYTES\n")
        for name in excluded:
            self.write(name, "PRIVATE_NEIGHBOR_BYTES\n")
        index = (self.root / ".git/index").read_bytes()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.observed()
        for name in selected:
            self.assertEqual(data["files"][name], "SELECTED_FINAL_TASK_BYTES\n")
            self.assertNotIn(f"NOT reviewed: {name}", result.stdout)
        self.assertNotIn("PRIVATE_NEIGHBOR_BYTES", json.dumps(data))
        self.assertNotIn("UNSTAGED_FINAL_TASK_BYTES", json.dumps(data))
        for name in excluded:
            self.assertNotIn(name, data["files"])
        self.assertEqual(index, (self.root / ".git/index").read_bytes())

    def test_additions_deletions_renames_and_executable_mode(self):
        self.git("mv", SOURCE, BRIDGE)
        self.git("rm", ADAPTER)
        self.stage("tools/extraction/docling_extract.py", "print('fixture')\n")
        self.git("update-index", "--chmod=+x", "tools/extraction/docling_extract.py")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.observed()
        self.assertNotIn(SOURCE, data["files"])
        self.assertNotIn(ADAPTER, data["files"])
        self.assertIn(BRIDGE, data["files"])
        self.assertIn("new file mode 100755", data["diff"])
        self.assertIn("deleted file mode", data["diff"])

    def test_excluded_only_is_not_a_review_pass(self):
        self.stage("INBOX/private.txt", "changed excluded fixture\n")
        self.git("config", "--unset", "roborev.precommitLauncher")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No selected staged technical changes", result.stdout)
        self.assertIn("NOT reviewed: INBOX/private.txt", result.stdout)
        self.assertNotIn("PASS", result.stdout)
        self.assertFalse(self.record.exists())

    def test_missing_launcher_blocks(self):
        self.stage()
        self.git("config", "--unset", "roborev.precommitLauncher")
        self.assert_blocked(self.run_gate(), "launcher")

    def test_nonexistent_launcher_blocks(self):
        self.stage()
        self.launcher.unlink()
        self.assert_blocked(self.run_gate(), "launcher")

    def test_findings_exit_code_blocks(self):
        self.stage()
        self.fake(exit_code=1, verdict=0, output="Medium: fixture defect; fix fixture input")
        result = self.run_gate()
        self.assert_blocked(result, "exit 1")
        self.assertIn("completed failed Pi dirty-review job 42", result.stderr)
        self.assertIn("Medium: fixture defect; fix fixture input", result.stderr)
        run = Path(self.observed()["repo"]).parent
        self.assertIn("fixture defect", (run / "review.log").read_text())
        self.assertEqual(json.loads((run / "result.json").read_text())["status"], "blocked")

    def test_silent_review_failure_without_verified_job_blocks(self):
        self.stage()
        for options in ({"jobs": False}, {"status": "queued"}, {"foreign_job": True},
                        {"verdict": 1}, {"verdict": None}):
            with self.subTest(options=options):
                self.fake(exit_code=1, output="UNVERIFIED FINDING", **options)
                result = self.run_gate()
                self.assert_blocked(result, "exit 1")
                self.assertIn("no verified completed failed review", result.stderr)
                self.assertNotIn("UNVERIFIED FINDING", result.stderr)

    def test_repeated_snapshot_failures_report_each_verified_job(self):
        self.stage()
        self.fake(exit_code=1, verdict=0, output="Medium: fixture defect")
        self.launcher.write_text(self.launcher.read_text().replace(
            "print(json.dumps([job] if True else []))",
            "assert sys.argv[sys.argv.index('--repo') + 1] == str(root)\n"
            "    assert '--all-branches' in sys.argv\n"
            "    print(json.dumps([job, {**job, 'id': 40}]))",
        ).replace("'job_id': 42", "'job_id': int(sys.argv[3])").replace(
            "'job': job, 'prompt': prompt",
            "'job': {**job, 'id': int(sys.argv[3])}, 'prompt': prompt",
        ))
        result = self.run_gate()
        self.assert_blocked(result, "exit 1")
        for job_id in (42, 40):
            self.assertIn(f"completed failed Pi dirty-review job {job_id}", result.stderr)
        self.assertIn("not proof of which job caused this command exit", result.stderr)
        run = Path(self.observed()["repo"]).parent
        record = json.loads((run / "result.json").read_text())
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["diagnostic_job_ids"], [42, 40])
        self.assertNotIn("job_id", record)
        self.assertIn("job 40", (run / "review.log").read_text())

    def test_one_unavailable_result_does_not_hide_other_failed_jobs(self):
        spec = importlib.util.spec_from_file_location("gate_under_test", GATE)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        job = {"id": 42, "status": "done", "repo_path": str(self.root),
               "git_ref": "dirty", "agent": "pi"}
        older = {**job, "id": 40}
        review = {"job_id": 40, "agent": "pi", "job": older,
                  "verdict_bool": 0, "output": "VERIFIED OLDER FINDING"}
        with mock.patch.object(gate, "launcher_call", side_effect=[
                json.dumps([job, older]), gate.GateError("unavailable"), json.dumps(review)]):
            ids = []
            diagnostic = gate.failed_review_diagnostic(str(self.launcher), self.root, ids)
        self.assertIn("diagnostic lookup failed for job 42", diagnostic)
        self.assertIn("VERIFIED OLDER FINDING", diagnostic)
        self.assertEqual(ids, [40])

    def test_many_large_results_have_bounded_combined_findings_and_queries(self):
        spec = importlib.util.spec_from_file_location("gate_under_test", GATE)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        jobs = [{"id": n, "status": "done", "repo_path": str(self.root),
                 "git_ref": "dirty", "agent": "pi"} for n in range(1, 21)]
        def call(launcher, repo, args):
            if args[0] == "list":
                return json.dumps(jobs)
            job = jobs[int(args[2]) - 1]
            return json.dumps({"job_id": job["id"], "agent": "pi", "job": job,
                               "verdict_bool": 0, "output": "X" * 20000})
        with mock.patch.object(gate, "launcher_call", side_effect=call) as calls:
            ids = []
            diagnostic = gate.failed_review_diagnostic(str(self.launcher), self.root, ids)
        self.assertEqual(ids, list(range(1, 11)))
        self.assertEqual(calls.call_count, 11)
        self.assertLess(len(diagnostic), 19000)
        self.assertIn("diagnostic truncated", diagnostic)

    def test_multiple_jobs_still_cannot_authorize_commit(self):
        self.stage()
        self.fake()
        self.launcher.write_text(self.launcher.read_text().replace(
            "print(json.dumps([job] if True else []))",
            "print(json.dumps([job, {**job, 'id': 40}]))",
        ))
        self.assert_blocked(self.run_gate(), "no unique completed review job")

    def test_infrastructure_failure_is_identified_without_findings(self):
        self.stage()
        self.fake(exit_code=1, status="failed", output="UNVERIFIED FINDING")
        result = self.run_gate()
        self.assert_blocked(result, "infrastructure failure: Pi dirty-review job 42")
        self.assertNotIn("UNVERIFIED FINDING", result.stderr)

    def test_failed_review_diagnostic_is_bounded(self):
        self.stage()
        self.fake(exit_code=1, verdict=0, output="X" * 20000)
        result = self.run_gate()
        self.assert_blocked(result, "commit remains blocked")
        self.assertIn("diagnostic truncated", result.stderr)
        self.assertLess(len(result.stderr), 17000)

    def test_mismatched_embedded_job_is_not_reported_as_verified(self):
        self.stage()
        self.fake(exit_code=1, verdict=0, output="UNVERIFIED FINDING")
        self.launcher.write_text(self.launcher.read_text().replace(
            "'job': job, 'prompt': prompt", "'job': {**job, 'id': 99}, 'prompt': prompt",
        ))
        result = self.run_gate()
        self.assert_blocked(result, "no verified completed failed review")
        self.assertNotIn("UNVERIFIED FINDING", result.stderr)

    def test_evidence_query_failure_preserves_original_review_failure(self):
        self.stage()
        self.fake(exit_code=23)
        self.launcher.write_text(self.launcher.read_text().replace(
            "elif sys.argv[1] == 'list':", "elif sys.argv[1] == 'list':\n    sys.exit(97)",
        ))
        result = self.run_gate()
        self.assert_blocked(result, "review exit 23")
        self.assertIn("diagnostic lookup failed", result.stderr)

    def test_unavailable_daemon_or_launcher_failure_blocks(self):
        self.stage()
        self.fake(exit_code=23)
        self.assert_blocked(self.run_gate(), "exit 23")

    def test_queued_is_not_a_pass(self):
        self.stage()
        self.fake(status="queued")
        self.assert_blocked(self.run_gate(), "completed")

    def test_skipped_without_job_is_not_a_pass(self):
        self.stage()
        self.fake(jobs=False)
        self.assert_blocked(self.run_gate(), "completed")

    def test_stored_failing_or_missing_verdict_blocks(self):
        self.stage()
        for verdict in (0, None):
            with self.subTest(verdict=verdict):
                self.fake(verdict=verdict)
                self.assert_blocked(self.run_gate(), "stored passing verdict")

    def test_stored_diff_mismatch_blocks(self):
        self.stage()
        self.fake()
        self.launcher.write_text(self.launcher.read_text().replace(
            "'prompt': prompt", "'prompt': 'different staged bytes'",
        ))
        self.assert_blocked(self.run_gate(), "stored review diff differs")

    def test_concurrent_index_change_blocks(self):
        self.stage()
        mutation = (
            f"subprocess.run(['git', '-C', {str(self.root)!r}, 'update-index', "
            f"'--chmod=+x', {SOURCE!r}], check=True)"
        )
        self.fake(mutation=mutation)
        self.assert_blocked(self.run_gate(), "index or HEAD changed")

    def test_concurrent_head_change_blocks(self):
        self.stage()
        # Moving HEAD to a distinct existing commit must also invalidate review.
        self.git("commit", "--allow-empty", "-qm", "second base")
        previous = self.git("rev-parse", "HEAD^").stdout.decode().strip()
        self.fake(mutation=(
            f"subprocess.run(['git', '-C', {str(self.root)!r}, 'update-ref', "
            f"'HEAD', {previous!r}], check=True)"
        ))
        self.stage(text="VALUE = 4\n")
        self.assert_blocked(self.run_gate(), "index or HEAD changed")

    def test_staged_symlink_rejected(self):
        (self.root / SOURCE).unlink()
        (self.root / SOURCE).symlink_to(self.home / "outside")
        self.git("add", "--", SOURCE)
        self.assert_blocked(self.run_gate(), "symlink")
        self.assertFalse(self.record.exists())

    def test_head_symlink_rejected_even_when_replaced_in_index(self):
        (self.root / SOURCE).unlink()
        (self.root / SOURCE).symlink_to(self.home / "outside")
        self.git("add", "--", SOURCE)
        self.git("commit", "-qm", "symlink base")
        (self.root / SOURCE).unlink()
        self.stage()
        self.assert_blocked(self.run_gate(), "symlink")

    def test_worktree_ancestor_symlink_rejected_without_reading_it(self):
        self.stage()
        extraction = self.root / "tools/extraction"
        extraction.rename(self.home / "outside")
        extraction.symlink_to(self.home / "outside", target_is_directory=True)
        self.assert_blocked(self.run_gate(), "symlink")

    def test_snapshot_storage_symlink_escape_rejected(self):
        self.stage()
        (self.root / ".local").symlink_to(self.home, target_is_directory=True)
        self.assert_blocked(self.run_gate(), "symlink")

    def test_path_traversal_is_rejected(self):
        spec = importlib.util.spec_from_file_location("gate_under_test", GATE)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        for path in ("../outside", "/outside", "tools/../../outside", ".git/config"):
            with self.subTest(path=path), self.assertRaisesRegex(gate.GateError, "unsafe path"):
                gate.reject_links(self.root, path)

    def test_unignored_snapshot_storage_blocks(self):
        self.stage()
        (self.root / ".git/info/exclude").write_text("")
        self.assert_blocked(self.run_gate(), "must be Git-ignored")

    def test_binary_source_and_submodule_mode_rejected(self):
        self.stage(text="BINARY = '\0'\n")
        self.assert_blocked(self.run_gate(), "binary technical source")
        self.git("update-index", "--cacheinfo", "160000",
                 self.git("rev-parse", "HEAD").stdout.decode().strip(), SOURCE)
        self.assert_blocked(self.run_gate(), "non-regular technical path")

    def test_snapshot_private_no_origin_no_env_no_recursive_hook(self):
        self.stage()
        self.stage(".githooks/pre-commit", "#!/bin/sh\nexit 79\n")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.observed()
        self.assertEqual(data["hooks"], "/dev/null")
        self.assertEqual(data["remote"], "")
        self.assertIsNone(data["secret"])
        self.assertIsNone(data["index_env"])
        self.assertEqual(data["mode"], 0o700)
        self.assertIn("--wait", data["argv"])
        self.assertIn("--quiet", data["argv"])
        self.assertIn("--dirty", data["argv"])
        self.assertNotIn("--staged", data["argv"])
        snapshot = Path(data["repo"])
        self.assertTrue(snapshot.is_relative_to(self.root / ".local"))
        self.assertFalse((snapshot / ".git/hooks/pre-commit").exists())

    def test_partial_commit_index_is_used_but_not_leaked(self):
        alt_index = self.root / ".git/alternate-index"
        shutil.copyfile(self.root / ".git/index", alt_index)
        self.stage(text="REAL_INDEX = 4\n")
        self.write(SOURCE, "ALTERNATE_INDEX = 5\n")
        subprocess.run(["git", "-C", str(self.root), "add", SOURCE],
                       env={**self.env, "GIT_INDEX_FILE": str(alt_index)}, check=True)
        result = self.run_gate({"GIT_INDEX_FILE": str(alt_index)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed()["files"][SOURCE], "ALTERNATE_INDEX = 5\n")
        self.assertIsNone(self.observed()["index_env"])

    def test_staged_identity_persisted_and_content_sensitive(self):
        self.stage(text="VALUE = 1\n")
        self.assertEqual(self.run_gate().returncode, 0)
        first = Path(self.observed()["repo"]).parent / "result.json"
        first_record = json.loads(first.read_text())
        self.write(SOURCE, "UNSTAGED = 9\n")
        self.assertEqual(self.run_gate().returncode, 0)
        second = json.loads((Path(self.observed()["repo"]).parent / "result.json").read_text())
        self.assertEqual(first_record["staged_diff_sha256"], second["staged_diff_sha256"])
        self.stage(text="VALUE = 2\n")
        self.assertEqual(self.run_gate().returncode, 0)
        third = json.loads((Path(self.observed()["repo"]).parent / "result.json").read_text())
        self.assertNotEqual(first_record["staged_diff_sha256"], third["staged_diff_sha256"])
        self.assertEqual(third["job_id"], 42)

    def test_installer_local_only_and_idempotent(self):
        self.install_files()
        self.git("config", "--unset", "roborev.precommitLauncher")
        global_config = self.home / ".gitconfig"
        global_config.write_text("[user]\n\tname = Global fixture\n")
        before = global_config.read_bytes()
        for _ in range(2):
            result = self.installer()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(global_config.read_bytes(), before)
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath").stdout.strip(),
                         b".githooks")
        self.assertEqual(self.git("config", "--local", "--get-all", "roborev.precommitLauncher").stdout.strip(),
                         os.fsencode(self.launcher))

    def test_installer_refuses_native_hooks_without_changes(self):
        self.install_files()
        for hook_name in ("pre-commit", "post-commit", "pre-push"):
            with self.subTest(hook_name=hook_name):
                path = self.root / ".git/hooks" / hook_name
                path.write_text("#!/bin/sh\nexit 17\n")
                path.chmod(0o700)
                before = (self.root / ".git/config").read_bytes()
                self.assert_blocked(self.installer(), "existing hook")
                self.assertEqual(path.read_text(), "#!/bin/sh\nexit 17\n")
                self.assertEqual((self.root / ".git/config").read_bytes(), before)
                path.unlink()

    def test_installer_refuses_unexpected_shared_hooks_without_changes(self):
        self.install_files()
        for hook_name in ("post-commit", "pre-push", "post-rewrite"):
            with self.subTest(hook_name=hook_name):
                path = self.root / ".githooks" / hook_name
                path.write_text("#!/bin/sh\nexit 17\n")
                path.chmod(0o700)
                before = (self.root / ".git/config").read_bytes()
                self.assert_blocked(self.installer(), "unexpected shared hook")
                self.assertEqual((self.root / ".git/config").read_bytes(), before)
                self.assertEqual(path.read_text(), "#!/bin/sh\nexit 17\n")
                path.unlink()
        path = self.root / ".githooks" / "unknown"
        path.symlink_to(self.root / "absent")
        before = (self.root / ".git/config").read_bytes()
        self.assert_blocked(self.installer(), "unexpected shared hook")
        self.assertEqual((self.root / ".git/config").read_bytes(), before)
        self.assertTrue(path.is_symlink())

    def test_installer_refuses_local_and_global_hooks_config(self):
        self.install_files()
        for scope in ("--local", "--global"):
            with self.subTest(scope=scope):
                self.git("config", scope, "core.hooksPath", "other-hooks")
                before = (self.root / ".git/config").read_bytes()
                self.assert_blocked(self.installer(), "core.hooksPath")
                self.assertEqual((self.root / ".git/config").read_bytes(), before)
                self.git("config", scope, "--unset", "core.hooksPath")

    def test_installer_refuses_launcher_conflict_or_invalid_hook(self):
        self.install_files()
        self.git("config", "roborev.precommitLauncher", "/other/launcher")
        before = (self.root / ".git/config").read_bytes()
        self.assert_blocked(self.installer(), "launcher")
        self.assertEqual((self.root / ".git/config").read_bytes(), before)
        self.git("config", "roborev.precommitLauncher", str(self.launcher))
        (self.root / ".githooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
        self.assert_blocked(self.installer(), "hook")

    def test_real_precommit_invokes_gate_and_blocks_real_commit(self):
        self.install_files()
        self.assertEqual(self.installer().returncode, 0)
        self.stage()
        self.write(SOURCE, "UNSTAGED = 'preserved'\n")
        self.fake(exit_code=1)
        head = self.git("rev-parse", "HEAD").stdout
        # Prepopulate Git's own TREE cache so commit's preparation is not
        # mistaken for a write by the hook. The gate never calls write-tree.
        self.git("write-tree")
        index = (self.root / ".git/index").read_bytes()
        refs = self.git("show-ref").stdout
        staged = self.git("diff", "--cached", "--binary").stdout
        worktree = self.git("diff", "--binary").stdout
        result = self.git("commit", "-qm", "must block", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("diff", "--cached", "--binary").stdout, staged)
        self.assertEqual(self.git("diff", "--binary").stdout, worktree)

    def test_real_partial_commit_index_lock_and_git_env_isolation(self):
        self.install_files()
        self.assertEqual(self.installer().returncode, 0)
        self.stage(text="STAGED_BUT_NOT_COMMIT_BYTES = 7\n")
        self.stage(ADAPTER, "OTHER_STAGED_BYTES = 8\n")
        self.write(SOURCE, "PARTIAL_COMMIT_BYTES = 9\n")
        self.fake(exit_code=1)
        index = (self.root / ".git/index").read_bytes()
        refs = self.git("show-ref").stdout
        staged = self.git("diff", "--cached", "--binary").stdout
        worktree = self.git("diff", "--binary").stdout
        # --only invokes pre-commit with a locked temporary GIT_INDEX_FILE.
        result = self.git("-c", "color.ui=always", "commit", "-qm", "must block",
                          "--only", "--", SOURCE, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.record.exists(), result.stderr)
        data = self.observed()
        self.assertEqual(data["files"][SOURCE], "PARTIAL_COMMIT_BYTES = 9\n")
        self.assertEqual(data["files"][ADAPTER], "LIMIT = 1\n")
        self.assertEqual(self.git("show-ref").stdout, refs)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("diff", "--cached", "--binary").stdout, staged)
        self.assertEqual(self.git("diff", "--binary").stdout, worktree)
        for key in ("index_env", "dir_env", "worktree_env", "config_env"):
            self.assertIsNone(data[key])

    def test_source_repo_git_env_not_forwarded_to_snapshot(self):
        self.stage()
        result = self.run_gate({
            "GIT_DIR": str(self.root / ".git"), "GIT_WORK_TREE": str(self.root),
            "GIT_INDEX_FILE": str(self.root / ".git/index"),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "must-not-leak",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.observed()
        for key in ("index_env", "dir_env", "worktree_env", "config_env"):
            self.assertIsNone(data[key])
        self.assertEqual(data["hooks"], "/dev/null")

    def test_installed_hook_permits_controlled_passing_fixture_commit(self):
        self.install_files()
        self.assertEqual(self.installer().returncode, 0)
        self.stage(text="STAGED_FIXTURE = 2\n")
        self.write(SOURCE, "UNSTAGED_FIXTURE = 3\n")
        result = self.git("commit", "-qm", "controlled passing fixture", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", f"HEAD:{SOURCE}").stdout, b"STAGED_FIXTURE = 2\n")
        self.assertEqual((self.root / SOURCE).read_text(), "UNSTAGED_FIXTURE = 3\n")
        self.assertEqual(self.observed()["files"][SOURCE], "STAGED_FIXTURE = 2\n")

    def test_unborn_repository_addition(self):
        empty = self.home / "empty"
        empty.mkdir()
        self.root = empty
        self.git("init", "-q")
        (self.root / ".git/info/exclude").write_text(".local/\n")
        self.git("config", "roborev.precommitLauncher", str(self.launcher))
        self.stage()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed()["base"], {})

    def test_unborn_gate_does_not_consume_callers_stdin(self):
        empty = self.home / "empty"
        empty.mkdir()
        self.root = empty
        self.git("init", "-q")
        (self.root / ".git/info/exclude").write_text(".local/\n")
        self.git("config", "roborev.precommitLauncher", str(self.launcher))
        self.stage()
        result = self.run_gate(stdin_text="synthetic caller input, not a Git tree")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
