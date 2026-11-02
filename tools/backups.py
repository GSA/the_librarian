"""Shared daily-snapshot gate, bounded retention and inventory for authority-store backups.

The catalog and review-event stores each write verified SQLite snapshots with
their own validators. This module decides *when* a write needs one (no intact
snapshot of the same store for the current UTC day) and *what* may be deleted
(snapshots, receipts and ``.partial`` leftovers older than the retention window),
so both stores follow one rule. Source document text is never read here.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

DEFAULT_RETENTION_DAYS = 7
RECEIPT_FIELDS = {"backup_version", "created_at", "reason", "database", "sha256", "bytes", "state"}
MAX_RECEIPT_BYTES = 1_000_000
_SUFFIXES = (".sqlite3", ".json", ".partial")


class BackupError(ValueError):
    pass


def utcnow():
    """Single clock for receipts and retention; tests replace this."""
    return datetime.now(timezone.utc)


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _regular(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Entry:
    """One backup stem: a snapshot, its receipt and any partial leftovers."""

    def __init__(self, stem):
        self.stem = stem
        self.files = {}
        self.record = None
        self.created = None
        self.age_basis = None

    @property
    def snapshot(self):
        return self.files.get(".sqlite3")

    @property
    def size(self):
        return sum(os.lstat(path).st_size for path in self.files.values())

    @property
    def revision(self):
        state = (self.record or {}).get("state")
        revision = state.get("revision") if isinstance(state, dict) else None
        return revision if type(revision) is int else None

    @property
    def store_id(self):
        state = (self.record or {}).get("state")
        return state.get("store_id") if isinstance(state, dict) else None

    def intact(self, store_id):
        """Per-write gate: receipt names this store and the snapshot bytes match its hash.

        Only today's snapshot is hashed (one sequential read, stopping at the first
        intact one), so write cost grows with store size, not with write count.
        """
        record = self.record
        if record is None or self.snapshot is None or self.store_id != store_id:
            return False
        try:
            return (record["database"] == self.snapshot.name and os.lstat(self.snapshot).st_size == record["bytes"]
                    and _sha256(self.snapshot) == record["sha256"])
        except OSError:
            return False


def scan(backups):
    """Group recognised regular files by stem; symlinks and unknown files are left alone."""
    backups = Path(backups)
    entries = {}
    if not backups.is_dir():
        return []
    with os.scandir(backups) as listing:
        for item in listing:
            path = Path(item.path)
            suffix = path.suffix
            if suffix not in _SUFFIXES or not _regular(path):
                continue
            entries.setdefault(path.stem, Entry(path.stem)).files[suffix] = path
    for entry in entries.values():
        receipt = entry.files.get(".json")
        if receipt is not None and os.lstat(receipt).st_size <= MAX_RECEIPT_BYTES:
            try:
                record = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
            if isinstance(record, dict) and set(record) == RECEIPT_FIELDS:
                created = _parse(record["created_at"])
                if created is not None:
                    entry.record, entry.created, entry.age_basis = record, created, "receipt"
        if entry.created is None:
            entry.created = datetime.fromtimestamp(
                max(os.lstat(path).st_mtime for path in entry.files.values()), timezone.utc)
            entry.age_basis = "mtime"
    return sorted(entries.values(), key=lambda e: (e.created, e.stem))


def _verified(entry, store_id, verify, errors):
    if entry.snapshot is None or entry.record is None:
        return False
    try:
        state = verify(entry.snapshot)
    except (*errors, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False
    return isinstance(state, dict) and state.get("store_id") == store_id


def _fresh_ok(snapshot, store_id, verify, errors):
    try:
        state = verify(Path(snapshot["snapshot"]))
    except (*errors, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False
    return isinstance(state, dict) and state.get("store_id") == store_id


def has_snapshot_today(backups, store_id, now=None):
    now = now or utcnow()
    return any(e.created.date() == now.date() and e.intact(store_id) for e in scan(backups))


def inventory(backups):
    """Body-free backup inventory; reads receipts only and does not verify."""
    entries = scan(backups)
    snapshots = [e for e in entries if e.snapshot is not None]
    return {
        "snapshots": len(snapshots),
        "total_bytes": sum(e.size for e in entries),
        "partial_files": sum(1 for e in entries if ".partial" in e.files),
        "newest": snapshots[-1].created.isoformat() if snapshots else None,
        "oldest": snapshots[0].created.isoformat() if snapshots else None,
        "oldest_revision": min((e.revision for e in snapshots if e.revision is not None), default=None),
    }


def retention_days(value=None):
    days = DEFAULT_RETENTION_DAYS if value is None else value
    if type(days) is not int or days < 1:
        raise BackupError("Backup retention must be an integer number of days of at least 1")
    return days


def prune(backups, *, store_id, verify, errors=(), days=DEFAULT_RETENTION_DAYS, apply=False,
          thin=False, protect=(), now=None):
    """Delete backups older than the window, never leaving the store without a verified snapshot.

    With ``thin`` (operator prune) only the newest verified snapshot of each UTC day
    in the window is kept. ``protect`` names snapshots that must survive, such as a
    restore source. Without ``apply`` nothing is deleted.
    """
    days = retention_days(days)
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    protected = {Path(p).absolute() for p in protect}
    entries = scan(backups)
    report = {"applied": False, "window_days": days, "kept": len(entries), "deleted": 0,
              "bytes_freed": 0, "oldest_kept_revision": None, "unverified_kept": 0, "refused": None}
    inside = [e for e in entries if e.created >= cutoff]
    verified = {}

    def good(entry):
        if entry.stem not in verified:
            verified[entry.stem] = _verified(entry, store_id, verify, errors)
        return verified[entry.stem]

    if not any(good(e) for e in reversed(inside)):
        report["refused"] = "no verified snapshot inside the retention window; nothing deleted"
        return report
    doomed = [e for e in entries if e.created < cutoff]
    if thin:
        days_seen = {}
        for entry in reversed(inside):
            days_seen.setdefault(entry.created.date(), []).append(entry)
        for same_day in days_seen.values():
            keeper = next((e for e in same_day if good(e)), None)
            if keeper is None:
                report["unverified_kept"] += len(same_day)
                continue
            doomed.extend(e for e in same_day if e is not keeper)
    doomed = [e for e in doomed if not (protected & {Path(p).absolute() for p in e.files.values()})]
    freed = 0
    for entry in doomed:
        freed += entry.size
        if apply:
            for suffix in (".sqlite3", ".json", ".partial"):
                path = entry.files.get(suffix)
                if path is not None and _regular(path):
                    os.unlink(path)
    if apply and doomed:
        _fsync_directory(backups)
    gone = {e.stem for e in doomed}
    remaining = [e for e in entries if e.stem not in gone]
    report.update(applied=bool(apply), kept=len(remaining), deleted=len(doomed), bytes_freed=freed,
                  oldest_kept_revision=min((e.revision for e in remaining
                                            if e.snapshot is not None and e.revision is not None), default=None))
    return report


def daily_snapshot(backups, *, store_id, take, verify, errors=(), days=DEFAULT_RETENTION_DAYS, protect=()):
    """Take ``take()`` only when today has no intact snapshot, then apply retention.

    Returns ``(snapshot, retention)``; both are None when today is already covered.
    A retention failure never fails the write that triggered it.
    """
    days = retention_days(days)
    if has_snapshot_today(backups, store_id):
        return None, None
    snapshot = take()
    try:
        # Retention starts only after the new snapshot verifies; otherwise delete nothing.
        if not _fresh_ok(snapshot, store_id, verify, errors):
            return snapshot, {"applied": False, "refused": "new snapshot did not verify; nothing deleted"}
        retention = prune(backups, store_id=store_id, verify=verify, errors=errors, days=days,
                          apply=True, protect=protect)
    except (OSError, BackupError) as error:
        retention = {"applied": False, "refused": type(error).__name__}
    return snapshot, retention


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    prune_command = commands.add_parser("prune")
    prune_command.add_argument("--store", choices=("catalog", "review-events", "all"), default="all")
    prune_command.add_argument("--apply", action="store_true", help="delete; the default is a dry run")
    args = parser.parse_args(argv)
    try:
        from tools.catalog import catalog_store
        from tools.review_events import event_store
        stores = {"catalog": catalog_store, "review-events": event_store}
        if args.command == "status":
            result = {name: module.backup_inventory(args.root) for name, module in stores.items()}
        else:
            chosen = stores if args.store == "all" else {args.store: stores[args.store]}
            result = {name: module.prune(args.root, apply=args.apply) for name, module in chosen.items()}
        sys.stdout.write(json.dumps({"command": args.command, **result}, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (BackupError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        print("backups: " + (str(error) if isinstance(error, BackupError) else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
