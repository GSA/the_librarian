"""Opt-in full-text audit log of every MCP tool call; standard library only.

Each record keeps the agent label, the tool name, the complete arguments and the
complete text returned to the client, with its SHA-256 and the locators, source
hashes and record identities it mentions. The log therefore holds corpus text and
agent queries at the instance's audience. It is never retrieval context, never
searched or returned by a tool, and is separate from the body-free diagnostics log.
Files are private (0600 in a 0700 directory), appended with fsync, rotated into
numbered files at a stated size and never truncated, overwritten or deleted here.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

SCHEMA = "librarian-mcp-audit-v1"
DIRECTORY = Path(".local/audit")
DEFAULT_TEMPLATE = "{label}-mcp-access.jsonl"
MIN_FILE_BYTES = 1 << 20
MAX_FILE_BYTES = 64 << 20
OUTCOMES = {"ok", "error"}
LABEL = re.compile(r"[a-z0-9._-]{1,64}")
LOCATOR_KEYS = {"locator", "input_locator", "requested_locator"}
HASH_KEYS = {"source_hash"}
RECORD_KEYS = {"id", "event_id"}


class AuditError(ValueError):
    """An audit configuration or write failure; never swallowed."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def label(value):
    require(isinstance(value, str) and LABEL.fullmatch(value) is not None
            and value not in {".", ".."},
            "--agent-label must match [a-z0-9._-]{1,64}")
    return value


def default_path(agent_label):
    return DIRECTORY / DEFAULT_TEMPLATE.format(label=label(agent_label))


def log_path(root, value):
    """Resolve an audit path that stays below <root>/.local without symlinks."""
    root = Path(root).resolve()
    path = Path(value)
    require(".." not in path.parts, "--audit-log cannot traverse directories")
    path = path if path.is_absolute() else root / path
    require(path.is_relative_to(root / ".local"),
            "--audit-log must stay within the ignored <root>/.local directory")
    require(path.suffix == ".jsonl", "--audit-log must be a .jsonl file")
    relative = path.relative_to(root)
    for end in range(1, len(relative.parts) + 1):
        require(not root.joinpath(*relative.parts[:end]).is_symlink(),
                f"Audit log path cannot use symlinks: {path}")
    return path


def references(text):
    """Collect locators, source hashes and record ids mentioned by a JSON result."""
    found = {"locators": [], "source_hashes": [], "record_ids": []}
    try:
        payload = json.loads(text)
    except ValueError:
        return found

    def add(bucket, value):
        if isinstance(value, str) and value not in found[bucket]:
            found[bucket].append(value)

    stack = [payload]
    while stack:
        value = stack.pop(0)
        if isinstance(value, dict):
            for key, item in value.items():
                if key in LOCATOR_KEYS:
                    add("locators", item)
                elif key in HASH_KEYS:
                    add("source_hashes", item)
                elif key in RECORD_KEYS:
                    add("record_ids", item)
                if isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)
    return found


def numbered(path, index):
    return path.with_name(f"{path.stem}.{index}{path.suffix}")


def log_files(path):
    """Rotated files in rotation order, then the active file."""
    path = Path(path)
    pattern = re.compile(re.escape(path.stem) + r"\.([1-9][0-9]*)" + re.escape(path.suffix))
    rotated = sorted((int(match.group(1)), candidate) for candidate in path.parent.iterdir()
                     if (match := pattern.fullmatch(candidate.name))) if path.parent.is_dir() else []
    return [candidate for _, candidate in rotated] + ([path] if path.exists() else [])


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_directories(path):
    """Create missing ancestors 0700, fsyncing each new entry in its parent."""
    path = Path(path)
    missing = []
    for candidate in [path, *path.parents]:
        if candidate.exists() or candidate.is_symlink():
            break
        missing.append(candidate)
    for candidate in reversed(missing):
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:  # a concurrent binding created it first
            pass
        fsync_directory(candidate.parent)


def private_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mode = os.fstat(descriptor).st_mode
    finally:
        os.close(descriptor)
    require(not mode & (stat.S_IRWXG | stat.S_IRWXO),
            f"Audit directory must be private (0700): {path}")


def private_file(path, flags):
    descriptor = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        status = os.fstat(descriptor)
        require(stat.S_ISREG(status.st_mode), f"Audit log must be a regular file: {path}")
        require(not status.st_mode & (stat.S_IRWXG | stat.S_IRWXO),
                f"Audit log must be private (0600): {path}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def same_file(path, status):
    other = Path(path).lstat()
    return (other.st_dev, other.st_ino) == (status.st_dev, status.st_ino)


@contextmanager
def locked(path):
    descriptor = private_file(Path(path).with_name(Path(path).name + ".lock"), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class Log:
    """Append-only, rotated, private full-text audit writer."""

    def __init__(self, root, value, agent_label, *, max_bytes=MAX_FILE_BYTES):
        self.agent_label = label(agent_label)
        require(type(max_bytes) is int and max_bytes >= MIN_FILE_BYTES,
                f"Audit rotation size must be at least {MIN_FILE_BYTES} bytes")
        self.max_bytes = max_bytes
        self.path = log_path(root, value)
        self.run_id = str(uuid.uuid4())
        # A concurrent binding may create the directory first; validate, never repair.
        create_directories(self.path.parent)
        private_directory(self.path.parent)

    def locked(self):
        return locked(self.path)

    def sync_directory(self):
        fsync_directory(self.path.parent)

    def recover(self):
        """Finish an interrupted rotation: the active name still links a rotated file."""
        if not self.path.exists():
            return
        status = self.path.lstat()
        if any(name != self.path and same_file(name, status) for name in log_files(self.path)):
            os.unlink(self.path)
            self.sync_directory()

    def ends_record(self):
        descriptor = private_file(self.path, os.O_RDONLY)
        try:
            return os.pread(descriptor, 1, max(os.fstat(descriptor).st_size - 1, 0)) == b"\n"
        finally:
            os.close(descriptor)

    def rotate(self):
        index = max([int(name.stem.rsplit(".", 1)[1]) for name in log_files(self.path)
                     if name != self.path] or [0]) + 1
        os.link(self.path, numbered(self.path, index), follow_symlinks=False)
        self.sync_directory()
        os.unlink(self.path)

    def write(self, *, tool, arguments, outcome, text, started_at, completed_at,
              duration_ms, error_type=None):
        require(outcome in OUTCOMES, "Audit outcome must be ok or error")
        require(isinstance(text, str), "Audit records the returned text exactly")
        require(isinstance(arguments, dict), "Audit arguments must be a JSON object")
        record = {
            "schema": SCHEMA, "run_id": self.run_id, "call_id": str(uuid.uuid4()),
            "started_at": started_at, "completed_at": completed_at,
            "duration_ms": duration_ms, "agent_label": self.agent_label,
            "tool": tool, "arguments": arguments, "outcome": outcome,
            **({"error_type": error_type} if error_type is not None else {}),
            "returned_chars": len(text),
            "returned_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "references": references(text), "returned_text": text,
        }
        raw = (json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
               + "\n").encode()
        private_directory(self.path.parent)
        with self.locked():
            self.recover()
            if self.path.exists():
                status = self.path.lstat()
                if status.st_size and not self.ends_record():
                    self.rotate()  # preserve a record left unterminated by a failed write
                elif status.st_size and status.st_size + len(raw) > self.max_bytes:
                    descriptor = private_file(self.path, os.O_RDONLY)
                    os.close(descriptor)
                    self.rotate()
            created = not self.path.exists()
            descriptor = private_file(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                view = memoryview(raw)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if created:
                self.sync_directory()
        return record


def read_lines(path):
    require(not path.is_symlink(), f"Audit log cannot be a symlink: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def dedupe(files, active):
    """Drop the active name when an interrupted rotation left it linked to a rotated file."""
    if active in files and active.exists():
        status = active.lstat()
        if any(name != active and same_file(name, status) for name in files):
            return [name for name in files if name != active]
    return files


def report(root, log=None):
    """Body-free summary: counts by tool, agent and outcome, sizes and hash checks.

    Without ``log`` every ``*.jsonl`` in ``.local/audit`` is read; with it, that
    log (validated like the writer's path) and its rotated files.
    """
    summary = {"files": 0, "records": 0, "invalid_lines": 0, "hash_mismatches": 0,
               "returned_chars": 0, "returned_bytes": 0}
    tools, agents, outcomes = Counter(), Counter(), Counter()
    if log is None:
        folder = Path(root).resolve() / DIRECTORY
        # Every log with a writer lock is one active log plus its rotated files.
        actives = sorted(lock.with_name(lock.name[:-len(".lock")])
                         for lock in folder.glob("*.jsonl.lock")) if folder.is_dir() else []
    else:
        actives = [log_path(root, log)]
    groups, covered = [], set()
    for active in actives:
        if not active.parent.is_dir():
            continue
        with locked(active):  # writers rotate under this lock; read a stable view
            files = log_files(active)
            groups.append([(path, read_lines(path)) for path in dedupe(files, active)])
        covered.update(files)
    if log is None and folder.is_dir():
        groups.append([(path, read_lines(path)) for path in sorted(folder.glob("*.jsonl"))
                       if path not in covered])
    for lines in groups:
        for path, content in lines:
            summary["files"] += 1
            for line in content:
                try:
                    record = json.loads(line)
                    assert record["schema"] == SCHEMA
                    text = record["returned_text"]
                except (ValueError, KeyError, TypeError, AssertionError):
                    summary["invalid_lines"] += 1
                    continue
                summary["records"] += 1
                tools[str(record.get("tool"))] += 1
                agents[str(record.get("agent_label"))] += 1
                outcomes[str(record.get("outcome"))] += 1
                summary["returned_chars"] += len(text)
                summary["returned_bytes"] += len(text.encode("utf-8"))
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != record.get("returned_sha256"):
                    summary["hash_mismatches"] += 1
    return {**summary, "by_tool": dict(sorted(tools.items())),
            "by_agent": dict(sorted(agents.items())),
            "by_outcome": dict(sorted(outcomes.items())),
            "generated_at": datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    arguments = argparse.ArgumentParser(description="Summarize the MCP audit log without bodies")
    arguments.add_argument("--root", required=True, type=Path)
    commands = arguments.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("report", help="Counts by tool, agent and outcome; no text or queries")
    summary.add_argument("--log", type=Path, default=None,
                         help="One audit log below <root>/.local, with its rotated files; "
                              "default: every log in .local/audit")
    options = arguments.parse_args(argv)
    try:
        print(json.dumps(report(options.root, options.log), sort_keys=True))
    except (AuditError, OSError, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
