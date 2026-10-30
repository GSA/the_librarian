#!/usr/bin/env python3
"""Body-free retrieval diagnostics. Never a catalog, review, or retrieval authority.

Every recorded string comes from a closed vocabulary defined here. Query text,
prompts, model output, locators and source excerpts cannot be represented in a
record, so they cannot be written by accident. A read is counted but never
attributed, so the log cannot say which records were read. Diagnostics describe
this toolkit's own retrieval behavior; they are never returned as context and
are never evidence about the subject matter of a corpus.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import uuid

SCHEMA = "the-librarian-retrieval-diagnostics-v1"
MAX_RECORD_BYTES = 1024
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 200000
OPERATIONS = {"search", "read"}
OUTCOMES = {"hit", "empty", "error"}
CLASSIFICATIONS = {"absent-corpus", "absent-partial", "present-uncombined"}
SCOPES = {"selection", "path"}
COUNTS = {"terms", "absent_terms", "sections_matching_any", "hits"}
BASE = {"schema", "run_id", "started_at", "completed_at", "duration_ms", "operation", "outcome"}
FIELDS = COUNTS | {"classification", "scope", "error_type"}
NOTICE = (
    "Body-free diagnostics about this toolkit's retrieval behavior. Not qualified context, "
    "not a review, not evidence about any subject matter, and not authorization to ingest, "
    "publish or prune anything. An absence classification reports what is indexed, never "
    "that a fact does not exist."
)


class DiagnosticsError(Exception):
    """An explicit diagnostics input, budget or storage failure."""


def require(condition, message):
    if not condition:
        raise DiagnosticsError(message)


def timestamp(value):
    require(isinstance(value, str), "Diagnostic timestamps must be ISO strings")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise DiagnosticsError(f"Invalid diagnostic timestamp: {error}") from error
    require(result.tzinfo is not None, "Diagnostic timestamps must include a timezone")
    return result


def validate(record):
    require(isinstance(record, dict) and BASE <= record.keys() <= BASE | FIELDS,
            "Invalid diagnostic fields; queries, locators and bodies cannot be recorded")
    require(record["schema"] == SCHEMA, "Unsupported diagnostics schema; audit logs are not diagnostics")
    require(record["operation"] in OPERATIONS, "Invalid diagnostic operation")
    require(record["outcome"] in OUTCOMES, "Invalid diagnostic outcome")
    if record["operation"] != "search":
        require(record["outcome"] != "empty",
                "Only a search can be empty; a read either returns or fails")
        require(not record.keys() & ({"classification", "scope"} | COUNTS),
                "Term counts and absence classification describe a search, never a read")
    try:
        require(str(uuid.UUID(record["run_id"])) == record["run_id"], "Invalid diagnostic run identity")
    except (ValueError, AttributeError, TypeError) as error:
        raise DiagnosticsError("Invalid diagnostic run identity") from error
    elapsed = record["duration_ms"]
    require(type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0,
            "Diagnostic duration must be finite and nonnegative")
    require(timestamp(record["completed_at"]) >= timestamp(record["started_at"]),
            "Diagnostic completion precedes its start")
    for key in record.keys() & COUNTS:
        require(type(record[key]) is int and 0 <= record[key] <= MAX_RECORDS,
                "Diagnostic counts must be bounded nonnegative integers")
    if "classification" in record:
        require(record["classification"] in CLASSIFICATIONS, "Invalid absence classification")
        require(record["outcome"] == "empty", "Absence classification requires an empty outcome")
        require({"terms", "absent_terms", "scope"} <= record.keys(),
                "Absence classification requires term counts and scope")
        require(record["absent_terms"] <= record["terms"], "Absent terms cannot exceed query terms")
        require(record["terms"] >= 1, "An absence classification needs at least one query term")
        consistent = {
            "absent-corpus": record["absent_terms"] == record["terms"],
            "absent-partial": 0 < record["absent_terms"] < record["terms"],
            "present-uncombined": record["absent_terms"] == 0,
        }[record["classification"]]
        require(consistent, "Absence classification contradicts its term counts")
    else:
        require(record["outcome"] != "empty" or "scope" not in record,
                "An empty outcome must carry its classification or no scope")
    if "scope" in record:
        require(record["scope"] in SCOPES, "Invalid diagnostic scope")
    if "hits" in record:
        require((record["hits"] > 0) == (record["outcome"] == "hit"),
                "Diagnostic hit count contradicts its outcome")
    if "error_type" in record:
        value = record["error_type"]
        require(isinstance(value, str) and 1 <= len(value) <= 64 and value.isidentifier(),
                "Diagnostic errors must be exception class names, not messages")
        require(record["outcome"] == "error", "Only an error outcome carries an error type")
    return record


def log_path(root, value):
    path = Path(value)
    path = path if path.is_absolute() else Path(root) / path
    require(".." not in path.parts, "Diagnostics log path cannot traverse directories")
    require(path.suffix == ".jsonl", "Diagnostics log must be a .jsonl file")
    require(not path.is_symlink(), f"Diagnostics log cannot be a symlink: {path}")
    require(not path.parent.is_symlink(), f"Diagnostics directory cannot be a symlink: {path.parent}")
    return path


def private_descriptor(path, flags):
    descriptor = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        status = os.fstat(descriptor)
        require(stat.S_ISREG(status.st_mode), "Diagnostics log must be a regular file")
        if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def log_lock(path):
    """Serialize append and prune across a stable lock file so replace cannot orphan writes."""
    target = Path(path)
    lock_file = target.with_name(target.name + ".lock")
    require(not lock_file.is_symlink(), f"Diagnostics lock cannot be a symlink: {lock_file}")
    require(not lock_file.parent.is_symlink(), f"Diagnostics directory cannot be a symlink: {lock_file.parent}")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = private_descriptor(lock_file, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class Log:
    """Append-only JSONL writer. Opt-in; a write failure is reported, never swallowed."""

    def __init__(self, root, value):
        self.path = log_path(root, value)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **fields):
        record = validate({"schema": SCHEMA, **fields})
        raw = (json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
               + "\n").encode()
        require(len(raw) <= MAX_RECORD_BYTES, "Diagnostic exceeds its record budget")
        with log_lock(self.path):
            descriptor = private_descriptor(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                size = os.fstat(descriptor).st_size
                require(size + len(raw) <= MAX_LOG_BYTES,
                        f"Diagnostics log is full at {size} bytes; prune it explicitly")
                require(os.write(descriptor, raw) == len(raw), "Short diagnostics write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return record

    def record_search(self, payload, *, started_at, completed_at, duration_ms, run_id=None):
        """Derive a body-free record from a reader search envelope."""
        require(isinstance(payload, dict) and payload.get("command") == "search",
                "Only a search envelope produces a retrieval diagnostic")
        hits = payload.get("hits")
        require(isinstance(hits, list), "Search envelope is missing its hit list")
        fields = {
            "run_id": str(uuid.uuid4()) if run_id is None else run_id,
            "started_at": started_at, "completed_at": completed_at,
            "duration_ms": duration_ms, "operation": "search",
            "outcome": "hit" if hits else "empty", "hits": len(hits),
        }
        absence = payload.get("absence")
        if not hits and isinstance(absence, dict):
            for key in ("classification", "scope"):
                if key in absence:
                    fields[key] = absence[key]
            for key in ("terms", "absent_terms", "sections_matching_any"):
                if key in absence:
                    fields[key] = absence[key]
        return self.write(**fields)

    def record_read(self, payload, *, started_at, completed_at, duration_ms, run_id=None):
        """Count that a read happened. No locator, document, hash or excerpt is recorded.

        This is a raw count only. No ratio is derived from it, because reads per
        answered search cannot distinguish an unconvincing excerpt from an agent that
        never needed the full record. It also deliberately cannot say which records
        were read, so it never becomes a usage profile of the corpus or of the asking
        agent.
        """
        require(isinstance(payload, dict) and payload.get("command") in {"get", "get-many"},
                "Only a get or get-many envelope produces a read diagnostic")
        return self.write(
            run_id=str(uuid.uuid4()) if run_id is None else run_id,
            started_at=started_at, completed_at=completed_at, duration_ms=duration_ms,
            operation="read", outcome="hit",
        )

    def record_error(self, error_type, *, started_at, completed_at, duration_ms,
                     operation="search", run_id=None):
        return self.write(
            run_id=str(uuid.uuid4()) if run_id is None else run_id,
            started_at=started_at, completed_at=completed_at, duration_ms=duration_ms,
            operation=operation, outcome="error", error_type=error_type,
        )


def read_records(path, *, limit=MAX_RECORDS):
    records = []
    with open(path, encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            require(len(records) < limit, f"Diagnostics log exceeds {limit} records; prune it")
            try:
                record = json.loads(line)
            except ValueError as error:
                raise DiagnosticsError(f"Unreadable diagnostic on line {number}: {error}") from error
            records.append(validate(record))
    return records


def report(path, *, since=None):
    """Summarize coverage gaps and retrieval limits for an operator decision."""
    records = read_records(path)
    if since is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since)
        records = [r for r in records if timestamp(r["completed_at"]) >= cutoff]
    searches = [r for r in records if r["operation"] == "search"]
    reads = [r for r in records if r["operation"] == "read"]
    empty = [r for r in searches if r["outcome"] == "empty"]
    classified = Counter(r["classification"] for r in empty if "classification" in r)
    absent = [r["absent_terms"] for r in empty if "absent_terms" in r]
    return {
        "schema": SCHEMA, "notice": NOTICE, "log": str(path),
        "bytes": os.path.getsize(path), "window_days": since,
        "searches": len(searches), "reads": len(reads),
        "outcomes": dict(Counter(r["outcome"] for r in searches)),
        "empty_classifications": dict(classified),
        "unclassified_empty": sum(1 for r in empty if "classification" not in r),
        "absent_terms_total": sum(absent),
        "collection_gap_rate": (round(classified["absent-corpus"] / len(searches), 4)
                                if searches else None),
        "findability_gap_rate": (round(classified["present-uncombined"] / len(searches), 4)
                                 if searches else None),
        "interpretation": {
            "absent-corpus": "Connect or select a source covering this material.",
            "absent-partial": "Coverage is partial; widen selection or ingest the missing scope.",
            "present-uncombined": "Material is collected but not jointly retrievable; "
                                  "review selection, extraction or the search path.",
        },
        "limits": [
            "Repeated identical questions are counted separately; terms are not stored, so "
            "recurring gaps cannot be ranked by subject.",
            "Counts describe requests this instance observed, not corpus completeness.",
            "An absence classification is never evidence that a fact does not exist.",
            "Reads are counted but never attributed; no locator is recorded, so this cannot say "
            "which records were read and is not a basis for pruning selection.",
            "No search-to-read ratio is reported. Reads per answered search cannot distinguish "
            "unconvincing excerpts from an agent that never needed the full record, so the raw "
            "counts are kept and no quality conclusion is drawn from them.",
        ],
    }


def prune(path, keep):
    """Explicitly drop all but the newest records. Never automatic; reports both counts."""
    require(type(keep) is int and 0 <= keep <= MAX_RECORDS, "--keep must be a bounded nonnegative integer")
    with log_lock(path):
        records = read_records(path)
        retained = records[len(records) - keep:] if keep else []
        descriptor, temporary = tempfile.mkstemp(dir=str(Path(path).parent), suffix=".jsonl")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for record in retained:
                    stream.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise
    return {"schema": SCHEMA, "log": str(path), "notice": NOTICE,
            "before": len(records), "after": len(retained),
            "removed": len(records) - len(retained),
            "caveat": "Diagnostics are derived observations; pruning them destroys no authoritative record."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--log", default=".local/diagnostics/retrieval.jsonl")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("report", help="Summarize observed coverage and findability gaps")
    summary.add_argument("--since-days", type=int)
    removal = commands.add_parser("prune", help="Explicitly retain only the newest records")
    removal.add_argument("--keep", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        path = log_path(args.root, args.log)
        if not path.is_file():
            raise DiagnosticsError(f"No diagnostics log at {path}; retrieval diagnostics are opt-in")
        if args.command == "report":
            if args.since_days is not None and args.since_days < 1:
                raise DiagnosticsError("--since-days must be a positive number of days")
            result = report(path, since=args.since_days)
        else:
            result = prune(path, args.keep)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (DiagnosticsError, OSError, UnicodeError, ValueError) as error:
        print(f"retrieval-diagnostics: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
