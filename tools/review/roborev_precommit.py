#!/usr/bin/env python3
"""Review an explicit technical subset of the commit index, never the worktree."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import subprocess
import sys


ALLOWLIST = frozenset({
    ".githooks/pre-commit",
    "tools/review/roborev_precommit.py",
    "tools/review/install_hooks.py",
    "tools/review/test_precommit.py",
    "tools/review/README.md",
    "tools/extraction/docling_worker.py",
    "tools/extraction/docling_adapter.py",
    "tools/extraction/docling_bridge.py",
    "tools/extraction/docling_extract.py",
    "tools/extraction/extract.py",
    "tools/extraction/requirements-docling.txt",
    "tools/extraction/test_docling_worker.py",
    "tools/extraction/test_docling.py",
    "tests/test_docling.py",
    "tests/test_worker_contracts.py",
    "tests/test_business_context.py",
  "tools/business_context.py",
  "tests/test_kit.py",
    "tests/support.py",
    "tests/test_mcp.py",
    "tools/__init__.py",
    "tools/catalog/__init__.py",
    "tools/catalog/inventory.py",
    "tools/context_index/__init__.py",
    "tools/context_index/compact_records.py",
    "tools/context_index/renditions.py",
    "tools/context-mcp/src/the_librarian_mcp/config.py",
    "tools/context-mcp/src/the_librarian_mcp/preflight.py",
    "tools/context-mcp/src/the_librarian_mcp/server.py",
    "tools/diagnostics/__init__.py",
    "tools/extraction/__init__.py",
    "tools/extraction/extract.py",
    "tools/initialize.py",
    "tools/package.py",
    "tools/review_events/__init__.py",
    "tools/review_events/event_reader.py",
    "tools/review_events/requirements.txt",
    "tools/review_events/review-event-v1.schema.json",
    "tools/review_events/review_events.py",
    "tools/review/__init__.py",
    "tools/schemas/__init__.py",
    "tools/schemas/export_schemas.py",
    "release-files.json",
    "config/ontology.json",
    "tools/ontology.py",
    "tools/extraction/README.md",
    "tools/catalog/catalog_store.py",
    "tools/catalog/contracts.py",
    "tools/context_index/context_index.py",
    "tools/context_index/cli.py",
    "tools/context_index/qualification.py",
    "tools/review_events/event_store.py",
    "tools/schemas/catalog.sql",
    "tools/schemas/context-index.sql",
    "tools/schemas/review-events.sql",
    "README.md",
    "ROADMAP.md",
    "docs/decisions/0001-project-identity.md",
    "docs/decisions/0003-store-compatibility.md",
    "docs/decisions/0008-proof-of-concept-closure.md",
    "docs/decisions/0009-declared-ontology-and-entailment.md",
    "docs/decisions/README.md",
})
LAUNCHER_KEY = "roborev.precommitLauncher"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
REVIEW_TIMEOUT = 900


class GateError(RuntimeError):
    pass


def environment(source=False):
    env = {
        "HOME": os.path.expanduser("~"),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
    }
    if source:
        # Git supplies a temporary index for partial commits. Read it, do not
        # write-tree or otherwise acquire the real commit's index lock.
        for key in (
            "GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
        env["GIT_OPTIONAL_LOCKS"] = "0"
    else:
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_ATTR_NOSYSTEM="1")
    return env


def git(root, *args, source=True, check=True):
    result = subprocess.run(
        ["git", "-C", str(root), *args], env=environment(source),
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
    )
    if check and result.returncode:
        raise GateError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def config_values(root, key, local=False):
    args = ["config", *(["--local"] if local else []), "--get-all", key]
    result = git(root, *args, check=False)
    if result.returncode not in (0, 1):
        raise GateError(f"cannot read Git configuration: {key}")
    return result.stdout.decode().splitlines()


def reject_links(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or any(p in ("", ".", "..", ".git") for p in relative.split("/")):
        raise GateError(f"unsafe path: {relative!r}")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise GateError(f"symlink path rejected: {relative}")


def repository_root(value):
    root = Path(value).absolute()
    if root.resolve() != root:
        raise GateError("symlink repository root rejected")
    actual = git(root, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if Path(actual) != root:
        raise GateError("run from the repository root (or supply --root)")
    return root


def head(root):
    result = git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if result.returncode not in (0, 1):
        raise GateError("cannot determine HEAD")
    return result.stdout.decode().strip()


def index_token(root):
    index = Path(os.fsdecode(git(root, "rev-parse", "--git-path", "index").stdout).strip())
    if not index.is_absolute():
        index = root / index
    if index.is_symlink():
        raise GateError("symlink index rejected")
    if not index.exists():
        return (head(root), None)
    before = index.stat()
    digest = hashlib.sha256(index.read_bytes()).hexdigest()
    after = index.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after):
        raise GateError("index or HEAD changed while reading")
    return (head(root), digest, identity(after))


def entries(raw, index=False):
    values = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, oid_or_kind, stage_or_oid = metadata.decode().split()
        if index and stage_or_oid != "0":
            raise GateError("unmerged index cannot be reviewed")
        path = os.fsdecode(name)
        if path in ALLOWLIST:
            if mode not in ("100644", "100755"):
                raise GateError(f"symlink or non-regular technical path rejected: {path}")
            values[path] = (mode, oid_or_kind if index else stage_or_oid)
    return values


def load_blobs(root, tree):
    result = {}
    total = 0
    for path, (mode, oid) in sorted(tree.items()):
        reject_links(root, path)
        size = int(git(root, "cat-file", "-s", oid).stdout)
        total += size
        if size > MAX_FILE_BYTES or total > MAX_SNAPSHOT_BYTES:
            raise GateError("technical source snapshot byte limit exceeded")
        data = git(root, "cat-file", "blob", oid).stdout
        try:
            data.decode("utf-8")
        except UnicodeError as error:
            raise GateError(f"non-UTF-8 technical source rejected: {path}") from error
        if b"\0" in data:
            raise GateError(f"binary technical source rejected: {path}")
        result[path] = (mode, data)
    return result


def private_home(root):
    relative = ".local/roborev-precommit"
    reject_links(root, relative)
    ignored = git(root, "check-ignore", "--quiet", "--", relative + "/", check=False)
    if ignored.returncode != 0:
        raise GateError(".local/roborev-precommit/ must be Git-ignored before review")
    for name in (".local", relative):
        path = root / name
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise GateError(f"unsafe private snapshot parent: {name}")
    path = root / relative
    if path.stat().st_mode & 0o077:
        raise GateError("snapshot home must have private permissions (0700)")
    run = path / secrets.token_hex(16)
    run.mkdir(mode=0o700)
    return run


def materialize(repo, blobs):
    for name, (mode, data) in blobs.items():
        path = repo / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o700 if mode == "100755" else 0o600)


def make_snapshot(run, base, current):
    repo = run / "repo"
    repo.mkdir(mode=0o700)
    git(repo, "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=",
        "init", "--quiet", source=False)
    for key, value in {
        "core.hooksPath": "/dev/null",
        "user.name": "Source review",
        "user.email": "source-review@example.invalid",
        "commit.gpgsign": "false",
        "core.autocrlf": "false",
        "core.attributesFile": "/dev/null",
        "diff.renames": "false",
        "color.ui": "false",
    }.items():
        git(repo, "config", "--local", key, value, source=False)
    # Wrappers/daemon readers may replace our environment. Highest-precedence
    # repository attributes also pin their readers against user/system rules.
    info = repo / ".git/info"
    info.mkdir(mode=0o700, exist_ok=True)
    attributes = info / "attributes"
    attributes.write_text("* diff -text -crlf -eol -ident -filter -working-tree-encoding\n")
    attributes.chmod(0o600)
    materialize(repo, base)
    git(repo, "add", "--all", source=False)
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "Selected technical base", source=False)
    for path in base:
        if path not in current:
            (repo / path).unlink()
    materialize(repo, current)
    git(repo, "add", "--all", source=False)
    verify_snapshot(repo, base, current)
    return repo


def verify_snapshot(repo, base, current):
    for label, args, expected, is_index in (
        ("base", ("ls-tree", "-rz", "HEAD"), base, False),
        ("index", ("ls-files", "--stage", "-z"), current, True),
    ):
        raw = git(repo, *args, source=False).stdout
        actual = entries(raw, index=is_index)
        if set(actual) != set(expected) or len(raw.split(b"\0")) - 1 != len(actual):
            raise GateError(f"snapshot {label} inventory differs from selected source")
        for path, (mode, data) in expected.items():
            actual_mode, oid = actual[path]
            if actual_mode != mode or git(repo, "cat-file", "blob", oid, source=False).stdout != data:
                raise GateError(f"snapshot {label} bytes or mode differ from selected source: {path}")
    for path, (mode, data) in current.items():
        reject_links(repo, path)
        file = repo / path
        if file.read_bytes() != data or bool(file.stat().st_mode & 0o111) != (mode == "100755"):
            raise GateError(f"snapshot worktree bytes or mode differ from selected source: {path}")
    for path in base.keys() - current.keys():
        reject_links(repo, path)
        if (repo / path).exists():
            raise GateError(f"snapshot worktree bytes retain deleted source: {path}")


def snapshot_diff(repo):
    # The verified index is the byte-preserving source of truth. Force a full
    # textual patch and require ordinary review-side worktree readers to agree.
    expected = git(repo, "diff", "--cached", "--text", "--no-ext-diff", "--no-textconv",
                   "--no-renames", "--no-color", "HEAD", "--", ".", source=False).stdout
    actual = git(repo, "diff", "HEAD", "--", ".", source=False).stdout
    if not expected or actual != expected:
        raise GateError("incomplete or nontextual snapshot diff; refusing reduced review scope")
    return expected.decode()


def launcher_call(launcher, repo, args):
    result = subprocess.run(
        [launcher, *args], cwd=repo, env=environment(),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=REVIEW_TIMEOUT,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if args[0] == "review":
            job_ids = []
            diagnostic = failed_review_diagnostic(launcher, repo, job_ids)
            detail = "\n".join(part for part in (detail, diagnostic) if part)
            (repo.parent / "review.log").write_text(detail + "\n")
        error = GateError(f"roborev {args[0]} exit {result.returncode}: {detail}")
        if args[0] == "review":
            error.diagnostic_job_ids = job_ids
        raise error
    return result.stdout


def failed_review_diagnostic(launcher, repo, job_ids=None):
    """Report snapshot evidence, not approval or attribution of a quiet exit."""
    try:
        jobs = json.loads(launcher_call(
            launcher, repo, ["list", "--repo", str(repo), "--all-branches",
                             "--limit", "10", "--json"],
        ))
    except (GateError, OSError, ValueError, subprocess.SubprocessError) as error:
        return f"diagnostic lookup failed: {error}"
    if not isinstance(jobs, list) or not jobs:
        return "no verified completed failed review for snapshot; command cause unconfirmed"
    messages = []
    if len(jobs) > 1:
        messages.append("Snapshot job history (up to 10 jobs); not proof of which job caused this command exit.")
    seen = set()
    # Bound both the number of queries and combined findings, including when a
    # wrapper returns more rows than requested. Never choose a historical pass.
    limit = 16384 // min(len(jobs), 10)
    for job in jobs[:10]:
        if (not isinstance(job, dict) or job.get("repo_path") != str(repo)
                or job.get("git_ref") != "dirty" or job.get("agent") != "pi"
                or type(job.get("id")) is not int or job["id"] <= 0
                or job["id"] in seen):
            messages.append("no verified completed failed review for snapshot: invalid job identity")
            continue
        seen.add(job["id"])
        if job.get("status") == "failed":
            messages.append(f"stored infrastructure failure: Pi dirty-review job {job['id']}; commit remains blocked")
            continue
        if job.get("status") != "done":
            messages.append(f"no verified completed failed review for snapshot: job {job['id']} is not done")
            continue
        try:
            review = json.loads(launcher_call(
                launcher, repo, ["show", "--job", str(job["id"]), "--json"],
            ))
        except (GateError, OSError, ValueError, subprocess.SubprocessError) as error:
            messages.append(f"diagnostic lookup failed for job {job['id']}: {error}")
            continue
        embedded = review.get("job") if isinstance(review, dict) else None
        if (not isinstance(review, dict) or type(review.get("job_id")) is not int
                or review["job_id"] != job["id"] or review.get("agent") != "pi"
                or type(review.get("verdict_bool")) is not int
                or review["verdict_bool"] != 0 or not isinstance(embedded, dict)
                or type(embedded.get("id")) is not int
                or any(embedded.get(key) != job.get(key) for key in
                       ("id", "repo_path", "status", "git_ref", "agent"))):
            messages.append(f"no verified completed failed review for snapshot: job {job['id']} identity/verdict not verified")
            continue
        if job_ids is not None:
            job_ids.append(job["id"])
        output = review.get("output")
        text = output.strip() if isinstance(output, str) and output.strip() else "No review text available; inspect the stored job."
        if len(text) > limit:
            text = text[:limit] + "\n[diagnostic truncated; inspect stored job for full findings]"
        messages.append(f"completed failed Pi dirty-review job {job['id']}; commit remains blocked\n{text}")
    return "\n".join(messages)


def completed_verdict(launcher, repo, diff):
    jobs = json.loads(launcher_call(
        launcher, repo, ["list", "--repo", str(repo), "--all-branches", "--limit", "2", "--json"],
    ))
    if not isinstance(jobs, list) or len(jobs) != 1:
        raise GateError("no unique completed review job for snapshot")
    job = jobs[0]
    if (not isinstance(job, dict) or job.get("status") != "done" or job.get("repo_path") != str(repo)
            or job.get("git_ref") != "dirty" or job.get("agent") != "pi"
            or type(job.get("id")) is not int):
        raise GateError("snapshot review has no completed Pi dirty-review job")
    review = json.loads(launcher_call(
        launcher, repo, ["show", "--job", str(job["id"]), "--json"],
    ))
    if (not isinstance(review, dict) or review.get("job_id") != job["id"] or review.get("agent") != "pi"
            or type(review.get("verdict_bool")) is not int or review["verdict_bool"] != 1):
        raise GateError("snapshot review has no stored passing verdict")
    # The pinned CLI omits diff_content from show/list. Its saved prompt
    # contains the actual inline dirty diff; refuse filtered/truncated prompts.
    prompt = review.get("prompt")
    expected = "### Diff\n\n```diff\n" + diff + "```"
    if not isinstance(prompt, str) or not prompt.rstrip("\n").endswith(expected):
        raise GateError("stored review diff differs from selected staged snapshot")
    return job["id"]


def review_index(root):
    token = index_token(root)
    raw_index = git(root, "ls-files", "--stage", "-z").stdout
    current_tree = entries(raw_index, index=True)
    if token[0]:
        raw_base = git(root, "ls-tree", "-rz", token[0], "--", *sorted(ALLOWLIST)).stdout
    else:
        raw_base = b""
    base_tree = entries(raw_base)
    base_ref = token[0] or git(root, "hash-object", "-t", "tree", "--stdin").stdout.decode().strip()
    changed = git(root, "diff", "--cached", "--name-only", "-z", "--no-renames", base_ref).stdout
    paths = {os.fsdecode(p) for p in changed.split(b"\0") if p}
    selected = sorted(paths & ALLOWLIST)
    excluded = sorted(paths - ALLOWLIST)
    for path in excluded:
        print(f"NOT reviewed: {path.encode('unicode_escape').decode()}", flush=True)
    if not selected:
        print("No selected staged technical changes; no review performed (not a whole-commit pass).")
        return
    if excluded:
        raise GateError("Mixed staged scope: unreviewed paths must be committed separately; "
                        "no review launched")
    launchers = config_values(root, LAUNCHER_KEY, local=True)
    if len(launchers) != 1 or not Path(launchers[0]).is_absolute():
        raise GateError(f"configure one absolute local launcher via {LAUNCHER_KEY}")
    launcher = launchers[0]
    if not Path(launcher).is_file() or not os.access(launcher, os.X_OK):
        raise GateError("configured launcher is missing or not executable")
    base, current = load_blobs(root, base_tree), load_blobs(root, current_tree)
    run = private_home(root)
    print(f"Reviewing selected staged technical scope ONLY ({len(selected)} paths); snapshot: {run}", flush=True)
    record = {
        "selected": selected, "excluded_not_reviewed": excluded,
        "source_head": token[0], "index_entries_sha256": hashlib.sha256(raw_index).hexdigest(),
        "status": "blocked",
    }
    try:
        repo = make_snapshot(run, base, current)
        diff = snapshot_diff(repo)
        record["staged_diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest()
        if index_token(root) != token:
            raise GateError("index or HEAD changed during snapshot construction; retry commit")
        output = launcher_call(
            launcher, repo, ["review", "--repo", str(repo), "--dirty", "--wait",
                             "--agent", "pi", "--panel", "none", "--quiet"],
        )
        (run / "review.log").write_text(output)
        record["job_id"] = completed_verdict(launcher, repo, diff)
        verify_snapshot(repo, base, current)
        if snapshot_diff(repo) != diff:
            raise GateError("snapshot diff changed during review")
        if index_token(root) != token:
            raise GateError("index or HEAD changed during review; retry commit")
        record.update(status="passed-selected-scope-only", verdict=1, source_blobs_verified=True)
    except (GateError, OSError, ValueError, subprocess.SubprocessError) as error:
        record["error"] = str(error)
        if hasattr(error, "diagnostic_job_ids"):
            record["diagnostic_job_ids"] = error.diagnostic_job_ids
        raise
    finally:
        path = run / "result.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        path.chmod(0o600)
    print(f"PASS selected staged technical scope ONLY; job {record['job_id']}; "
          f"diff sha256 {record['staged_diff_sha256']} (not whole-commit coverage).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.getcwd())
    args = parser.parse_args()
    os.umask(0o077)
    try:
        review_index(repository_root(args.root))
        return 0
    except (GateError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"PRE-COMMIT BLOCKED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
