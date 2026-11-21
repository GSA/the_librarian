#!/usr/bin/env python3
"""Read-only integrity audit: retained files against the hashes recorded at ingest (Kata xv0n).

The catalog pins every retained file's SHA-256 and size when intake registers it. Readers do not
reopen those files to prove an index fresh; this module is where a change to one is found.

- ``audit`` re-hashes every present artifact with a path and a hash (originals, retained extractions,
  submission sidecars, anything else registered) and REPORTS each mismatch (path, kind, expected,
  found). It never fails a read, never repairs, and writes nothing to the instance. Run it on a
  schedule (README, "Integrity audit"); the exit status is 0 clean, 1 mismatches found, 2 not run.
- ``read_verified`` is what retrieval uses to open one retained file: it checks that file's SHA-256
  against the catalog's current pin and returns the bytes, or raises ``IntegrityMismatch``.

The catalog's current record is the reference, so a document-update ingestion that re-pins a path
changes what both check without a code change here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from tools.catalog import pins
from tools.context_index import forked
from tools.context_index.context_index import IndexError_, no_symlinks

WORKERS = 4
PARALLEL_MIN = 2000
READ_BLOCK = 1024 * 1024
KINDS = {"original": "original", "generated-rendition": "extraction", "preserved-metadata": "sidecar"}
CONTRACT = "integrity-audit-v1"


class IntegrityMismatch(IndexError_):
    """A retained file does not match the hash the catalog pinned for it."""

    def __init__(self, mismatch):
        super().__init__("Retained file does not match its catalog pin: " + mismatch["path"]
                         + " (" + mismatch["problem"] + ")")
        self.mismatch = mismatch


def _kind(role):
    return KINDS.get(role, role or "other")


def _below(root, relative):
    """The path of ``relative`` under ``root``; None for anything that is not a plain root-relative path."""
    parts = relative.split("/")
    if (not relative or relative.startswith("/") or "\\" in relative or "\0" in relative
            or any(part in ("", ".", "..") for part in parts)):
        return None
    return os.path.join(root, *parts)


def _open_plain(root, relative):
    """``(stream, None)`` for the plain file ``relative`` under ``root``, or ``(None, problem)``.

    No component below the root may be a symlink and the file must be regular, as for every read.
    """
    path = _below(root, relative)
    if path is None:
        return None, "unsafe-path"
    current = root
    try:
        for part in relative.split("/"):
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return None, "symlink"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None, "not-a-file"
    return os.fdopen(descriptor, "rb"), None


def _hash(root, relative):
    """``(sha256, size)`` of the plain file ``relative`` under ``root``, or ``(problem, None)``."""
    stream, problem = _open_plain(root, relative)
    if stream is None:
        return problem, None
    digest, size = hashlib.sha256(), 0
    try:
        with stream:
            while block := stream.read(READ_BLOCK):
                digest.update(block)
                size += len(block)
    except OSError:
        return "unreadable", None
    return digest.hexdigest(), size


def _compare(root, row):
    """The mismatch of one pinned file, or None when it is exactly as pinned."""
    record_id, relative, expected, size, role, receipt = row
    found, actual = _hash(root, relative)
    if actual is None:
        problem, found = found, None
    elif found != expected:
        problem = "hash-differs"
    elif size is not None and actual != size:
        problem = "size-differs"
    else:
        return None
    return {"path": relative, "kind": _kind(role), "problem": problem, "expected": expected,
            "found": found, "record": record_id, "receipt": receipt,
            **({"expected_bytes": size, "found_bytes": actual} if problem == "hash-differs" else {})}


def _chunk(bounds):
    root, rows = forked.JOB
    return [mismatch for mismatch in (_compare(root, row) for row in rows[bounds[0]:bounds[1]])
            if mismatch is not None]


def audit(root, *, workers=WORKERS, limit=1000):
    """Re-hash every retained file the catalog pins; the report lists each mismatch (at most ``limit``).

    ``checked`` counts files hashed, ``mismatches`` all found, ``listed`` those in ``items``.
    """
    root = no_symlinks(Path(root))
    with pins.snapshot(root, write=False) as snapshot:
        rows, state = snapshot.retained_files(), snapshot.state
    # The snapshot ends here: hashing gigabytes must not hold the catalog's read lock.
    base = str(root)
    found = None
    if len(rows) >= PARALLEL_MIN:
        found = forked.run((base, rows), _chunk, len(rows), min(workers, os.cpu_count() or 1))
    if found is None:
        found = [_chunk_sequential(base, rows)]
    mismatches = [mismatch for part in found for mismatch in part]
    return {"contract": CONTRACT, "root": base, "catalog_state": state["state_token"],
            "catalog_revision": state["revision"], "checked": len(rows), "mismatches": len(mismatches),
            "listed": min(len(mismatches), limit), "clean": not mismatches,
            "items": mismatches[:limit],
            "notice": "Read-only: nothing was repaired or rewritten. A mismatch means a retained file "
                      "differs from the hash recorded at ingest; restore it from backup or re-ingest it."}


def _chunk_sequential(root, rows):
    return [mismatch for mismatch in (_compare(root, row) for row in rows) if mismatch is not None]


def read_verified(root, relative):
    """The bytes of the retained file ``relative``, after checking its SHA-256 against the catalog pin.

    One hash per file actually opened. Raises ``IntegrityMismatch`` (carrying the report entry) when the
    file is missing, unsafe or differs from the pin, and ``IndexError_`` when the catalog pins no
    present file at that path.
    """
    root = no_symlinks(Path(root))
    with pins.snapshot(root, write=False) as snapshot:
        claims = [record["data"] for record in snapshot.artifacts_at(relative)
                  if record["data"].get("presence") == "present" and record["data"].get("sha256")]
    if not claims:
        raise IndexError_("No catalog pin for a present file at: " + relative)
    expected = claims[0]
    stream, problem = _open_plain(str(root), relative)
    raw = None
    if stream is not None:
        try:
            with stream:
                raw = stream.read()
        except OSError:
            problem = "unreadable"
    found = None if raw is None else hashlib.sha256(raw).hexdigest()
    if raw is not None and found in {claim["sha256"] for claim in claims}:
        return raw
    raise IntegrityMismatch({"path": relative, "kind": _kind(expected.get("role")),
                             "problem": problem or "hash-differs", "expected": expected["sha256"],
                             "found": found})


def parser():
    value = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    value.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = value.add_subparsers(dest="command", required=True)
    full = commands.add_parser("audit", help="Re-hash every retained file against its recorded hash")
    full.add_argument("--workers", type=int, default=WORKERS)
    full.add_argument("--limit", type=int, default=1000, help="Most mismatches to list (all are counted)")
    check = commands.add_parser("check", help="Verify named retained files against their catalog pins")
    check.add_argument("paths", nargs="+")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "audit":
            if args.workers < 1 or args.limit < 0:
                raise IndexError_("--workers must be at least 1 and --limit not negative")
            report = audit(args.root, workers=args.workers, limit=args.limit)
        else:
            items = []
            for relative in args.paths:
                try:
                    read_verified(args.root, relative)
                except IntegrityMismatch as error:
                    items.append(error.mismatch)
            report = {"contract": CONTRACT, "root": str(args.root), "checked": len(args.paths),
                      "mismatches": len(items), "listed": len(items), "clean": not items, "items": items}
        sys.stdout.write(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
        return 0 if report["clean"] else 1
    except (IndexError_, ValueError, OSError) as error:
        print(f"integrity: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
