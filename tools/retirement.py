"""Read-only retirement manifest of a legacy source tree, joined to catalog receipts (Kata j5wh).

``manifest`` walks a tree without following links, hashes every file (SHA-256, streamed) and joins
each one to the instance: first by the intake ledger's ``docs`` rows (system and path, then the
recorded SHA-256), then by content against the catalog's current ``original`` artifacts. Every
file ends in exactly one category; a file with neither a receipt nor an explicit, reasoned
disposition is *unresolved*, and the manifest is ``ready`` only when nothing is unresolved.
``verify`` re-hashes a restored or archived copy against a manifest (with ``--deletions`` it also accepts
the originals ``deletions.jsonl`` records as deleted from a live tree). Neither command writes the
tree or the instance; output goes to a private directory outside the tree.

The catalog and the ledger are opened read-only with plain SQLite. This is a dry-run join, not an
integrity check: run ``python -m tools.store_audit`` for that. ``--catalog-file`` may name a
verified catalog backup so the live catalog is not touched at all.

Categories: ``receipt-verified`` (ledger row for the path with the same SHA-256 whose receipt's
original has it), ``receipt-by-content`` (the bytes are a catalog original under another path),
``archive-expanded`` (an archive with an expansion manifest in the instance), ``empty-file``,
``symlink`` (target recorded), ``git-internal`` (below ``.git``, covered by the history bundle),
``disposition:<name>`` (an explicit rule), and the unresolved ``no-receipt``,
``ledger-hash-mismatch`` (bytes changed since intake), ``ledger-not-in-catalog``,
``archive-not-expanded``, ``unreadable`` and ``changed-during-read``.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from tools import policy

MANIFEST_SCHEMA = "retirement-manifest/1"
RULES_SCHEMA = "retirement-dispositions/1"
MANIFEST = "manifest.jsonl"
SUMMARY = "summary.json"
CHUNK = 1024 * 1024
# A file whose receipt exists but whose bytes changed since intake (``ledger-hash-mismatch``) is never
# disposed of by a rule: re-ingest it as a correction or remove the stale receipt claim.
DISPOSITIONABLE = {"no-receipt", "ledger-not-in-catalog", "archive-not-expanded"}
UNRESOLVED = DISPOSITIONABLE | {"ledger-hash-mismatch", "unreadable", "changed-during-read"}
ORIGINALS = (
    "SELECT json_extract(v.payload,'$.data.sha256'), json_extract(v.payload,'$.data.receipt_id') "
    "FROM current_records c JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal "
    "WHERE json_extract(v.payload,'$.kind')='artifact' AND json_extract(v.payload,'$.data.role')='original'")


class RetirementError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise RetirementError(code)


def _line(value):
    return json.dumps(value, sort_keys=True)


def _name(path):
    return os.fsdecode(path)


def _hash(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(tree):
    """``(paths, failures)``: every non-directory path below ``tree`` (links included, never followed), sorted,
    and the directories that could not be listed (their files are unknown, so they are never skipped silently)."""
    found, failures = [], []
    for directory, names, files in os.walk(tree, followlinks=False, onerror=failures.append):
        names.sort()
        here = Path(directory)
        found += [here / name for name in files]
        found += [here / name for name in names if (here / name).is_symlink()]
    return sorted(found), sorted(Path(error.filename) for error in failures if error.filename)


def _scan(path):
    """Size, hash and state of one path from one read, noticing a change while it was read."""
    try:
        before = os.lstat(path)
        if os.path.islink(path):
            return {"size": before.st_size, "sha256": None, "state": "symlink", "target": os.readlink(path)}
        sha256 = _hash(path)
        after = os.lstat(path)
    except OSError:
        return {"size": None, "sha256": None, "state": "unreadable"}
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return {"size": after.st_size, "sha256": sha256, "state": "changed"}
    return {"size": before.st_size, "sha256": sha256, "state": "ok"}


def _scan_all(paths, workers):
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(_scan, paths))


def _readonly(path, code):
    path = Path(path)
    _require(path.is_file(), code)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    return connection


def _originals(root, catalog_file):
    """``{sha256: sorted receipt IDs}`` of the catalog's current original artifacts."""
    path = Path(catalog_file) if catalog_file else root / policy.storage_roots(root)["catalog_home"] / "catalog.sqlite3"
    connection = _readonly(path, "catalog_unavailable")
    try:
        found = defaultdict(set)
        for sha256, receipt in connection.execute(ORIGINALS):
            if sha256 and receipt:
                found[sha256].add(receipt)
        revision = connection.execute("SELECT max(revision) FROM revisions").fetchone()[0]
    except sqlite3.Error as error:
        raise RetirementError("catalog_unreadable") from error
    finally:
        connection.close()
    return {sha: sorted(ids, key=lambda r: (len(r), r)) for sha, ids in found.items()}, revision, path


def _ledger(root, systems):
    """``{(system, path): (sha256, receipt_id, state)}`` of the intake ledger's docs rows for ``systems``."""
    from tools.intake import store
    path = store.locations(root)[2]
    connection = _readonly(path, "ledger_unavailable")
    rows = {}
    try:
        marks = ",".join("?" * len(systems))
        for row in connection.execute(
                "SELECT system, path, sha256, receipt_id, state FROM docs WHERE system IN (%s)" % marks,
                sorted(systems)):
            rows[(row["system"], row["path"])] = (row["sha256"], row["receipt_id"], row["state"])
    except sqlite3.Error as error:
        raise RetirementError("ledger_unreadable") from error
    finally:
        connection.close()
    return rows


def _archives(root):
    from tools.intake import archives
    return archives, archives.store_dir(root)


def load_rules(path):
    """The validated disposition rules: ``[(kind, value, disposition, reason)]`` applied in order."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RetirementError("rules_unreadable") from error
    _require(isinstance(value, dict) and value.get("schema") == RULES_SCHEMA
             and isinstance(value.get("rules"), list), "rules_invalid")
    rules = []
    for rule in value["rules"]:
        match = rule.get("match") if isinstance(rule, dict) else None
        _require(isinstance(match, dict) and len(match) == 1 and set(match) <= {"path", "prefix", "suffix", "sha256"}
                 and all(isinstance(v, str) and v for v in match.values()), "rules_invalid")
        for key in ("disposition", "reason"):
            _require(isinstance(rule.get(key), str) and rule[key].strip(), "rules_invalid")
        (kind, text), = match.items()
        rules.append((kind, text, rule["disposition"], rule["reason"]))
    return rules


def _ruled(rules, relative, sha256):
    for kind, text, disposition, reason in rules:
        hit = {"path": relative == text, "prefix": relative.startswith(text), "suffix": relative.endswith(text),
               "sha256": sha256 == text}[kind]
        if hit:
            return disposition, reason
    return None


def _parse_systems(items):
    mapping = []
    for item in items:
        prefix, separator, system = item.partition("=")
        _require(separator == "=" and system, "system_invalid")
        mapping.append((prefix.strip("/"), system))
    return mapping


def _classify(relative, scan, systems, ledger, originals, archive_store, rules):
    """The entry (without ``path``) for one scanned file."""
    entry = {"size": scan["size"], "sha256": scan["sha256"]}
    state, sha256 = scan["state"], scan["sha256"]
    if state in ("unreadable", "changed"):
        return {**entry, "category": "unreadable" if state == "unreadable" else "changed-during-read"}
    if state == "symlink":
        return {**entry, "category": "symlink", "target": scan["target"]}
    if relative == ".git" or relative.startswith(".git/"):
        return {**entry, "category": "git-internal"}
    held = originals.get(sha256, [])
    mismatch = None
    for prefix, system in systems:
        if prefix and not relative.startswith(prefix + "/"):
            continue
        row = ledger.get((system, relative[len(prefix) + 1:] if prefix else relative))
        if row is None:
            continue
        if row[0] == sha256:
            if row[1] in held:
                return {**entry, "category": "receipt-verified", "receipt": row[1], "ledger_state": row[2],
                        "system": system}
            mismatch = mismatch or {"category": "ledger-not-in-catalog", "receipt": row[1], "system": system}
        else:
            mismatch = mismatch or {"category": "ledger-hash-mismatch", "receipt": row[1], "system": system}
    if held:
        return {**entry, "category": "receipt-by-content", "receipt": held[0], "receipts": len(held)}
    if scan["size"] == 0:
        return {**entry, "category": "empty-file"}
    archives, directory = archive_store
    if archives.kind(Path(relative).name) is not None:
        manifest = archives.load(directory, sha256) if directory is not None else None
        if manifest is not None and manifest["status"] in ("expanded", "pre-expanded"):
            members = manifest["members"]
            return {**entry, "category": "archive-expanded", "archive": {
                "status": manifest["status"], "members": len(members),
                "members_in_catalog": sum(1 for m in members if m.get("sha256") in originals)}}
        entry["category"] = "archive-not-expanded"
    else:
        entry["category"] = (mismatch or {"category": "no-receipt"})["category"]
        entry.update({k: v for k, v in (mismatch or {}).items() if k != "category"})
    ruled = _ruled(rules, relative, sha256) if entry["category"] in DISPOSITIONABLE else None
    if ruled is not None:
        entry.update(underlying=entry["category"], category="disposition:" + ruled[0], reason=ruled[1])
    return entry


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def build(root, tree, out, *, systems=(), rules=(), workers=2, catalog_file=None):
    """Hash ``tree``, join it to the instance, write ``manifest.jsonl`` and ``summary.json`` into ``out``."""
    root, tree, out = Path(root).resolve(), Path(tree).resolve(), Path(out).resolve()
    _require(tree.is_dir(), "tree_unavailable")
    _require(root.is_dir(), "root_unavailable")
    _require(out != tree and tree not in out.parents and out not in tree.parents, "output_inside_tree")
    _require(out != root and root not in out.parents and out not in root.parents, "output_inside_instance")
    _require(not (out / MANIFEST).exists(), "output_exists")
    mapping = _parse_systems(systems)
    originals, revision, catalog_path = _originals(root, catalog_file)
    ledger = _ledger(root, {system for _, system in mapping}) if mapping else {}
    archive_store = _archives(root)
    paths, failures = _walk(tree)
    scans = _scan_all(paths, workers)
    entries, categories = [], defaultdict(lambda: {"files": 0, "bytes": 0})
    for directory in failures:
        entries.append({"path": _name(directory.relative_to(tree)), "size": None, "sha256": None,
                        "category": "unreadable", "kind": "directory"})
        categories["unreadable"]["files"] += 1
    for path, scan in zip(paths, scans):
        relative = _name(path.relative_to(tree))
        entry = {"path": relative, **_classify(relative, scan, mapping, ledger, originals, archive_store, rules)}
        entries.append(entry)
        bucket = categories[entry["category"]]
        bucket["files"] += 1
        bucket["bytes"] += entry["size"] or 0
    entries.sort(key=lambda e: e["path"])
    body = "".join(_line(e) + "\n" for e in entries)
    unresolved = sum(b["files"] for name, b in categories.items() if name in UNRESOLVED)
    header = {"schema": MANIFEST_SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "tree": str(tree), "entries": len(entries), "entries_sha256": hashlib.sha256(body.encode()).hexdigest(),
              "catalog_revision": revision, "catalog_file": catalog_path.name,
              "systems": [list(item) for item in mapping]}
    summary = {"schema": MANIFEST_SCHEMA, "manifest": str(out / MANIFEST),
               "counts": {"files": len(entries), "bytes": sum(e["size"] or 0 for e in entries)},
               "with_receipt": {"files": sum(b["files"] for n, b in categories.items()
                                             if n in ("receipt-verified", "receipt-by-content")),
                                "bytes": sum(b["bytes"] for n, b in categories.items()
                                             if n in ("receipt-verified", "receipt-by-content"))},
               "categories": {name: categories[name] for name in sorted(categories)},
               "unresolved": unresolved, "ready": unresolved == 0, "catalog_revision": revision,
               "manifest_sha256": hashlib.sha256((_line(header) + "\n" + body).encode()).hexdigest()}
    _private_directory(out)
    _write_private(out / MANIFEST, _line(header) + "\n" + body)
    _write_private(out / SUMMARY, json.dumps(summary, sort_keys=True, indent=1) + "\n")
    return summary


def read_manifest(path):
    """``(header, entries)`` of a manifest whose entries still hash to the header's ``entries_sha256``."""
    try:
        raw = Path(path).read_bytes()
        head, _, body = raw.partition(b"\n")
        header = json.loads(head)
        entries = [json.loads(line) for line in body.decode("utf-8").splitlines()]
    except (OSError, ValueError) as error:
        raise RetirementError("manifest_invalid") from error
    _require(isinstance(header, dict) and header.get("schema") == MANIFEST_SCHEMA
             and header.get("entries") == len(entries)
             and header.get("entries_sha256") == hashlib.sha256(body).hexdigest(), "manifest_invalid")
    return header, entries


def _recorded_removals(tree, deletions):
    """``{tree-relative path: sha256}`` of the originals ``deletions.jsonl`` records as deleted below ``tree``."""
    removed = {}
    try:
        lines = Path(deletions).read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.strip():
                record = json.loads(line)
                path = (Path(record["inbox"]) / record["relative"]).resolve().relative_to(tree)
                removed[_name(path)] = record["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RetirementError("deletions_invalid") from error
    return removed


def verify(manifest, tree, *, workers=2, report=None, deletions=None):
    """Compare ``tree`` with ``manifest``; return body-free counts (details go to the private ``report``).

    Against a cold archive or restored copy, use no ``deletions``: every entry must be present. Against the
    live tree after archive originals were deleted, pass the instance's ``deletions.jsonl``: an entry that
    is missing is accepted only when a record names that path and the manifest's SHA-256 (``removed``);
    anything else missing, changed or extra is still a mismatch.
    """
    header, entries = read_manifest(manifest)
    tree = Path(tree).resolve()
    _require(tree.is_dir(), "tree_unavailable")
    wanted = {e["path"]: e for e in entries}
    paths, failures = _walk(tree)
    present = {_name(p.relative_to(tree)): p for p in paths}
    removed = _recorded_removals(tree, deletions) if deletions else {}
    problems = {"missing": [], "extra": sorted(set(present) - set(wanted)), "size_mismatch": [],
                "hash_mismatch": [], "link_mismatch": [], "unreadable": [_name(d.relative_to(tree)) for d in failures],
                "removed": []}
    check = []
    for relative, entry in wanted.items():
        path = present.get(relative)
        if path is None:
            bucket = "removed" if removed.get(relative) == entry["sha256"] is not None else "missing"
            problems[bucket].append(relative)
        elif entry["category"] == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry["target"]:
                problems["link_mismatch"].append(relative)
        elif entry["sha256"] is None:
            problems["unreadable"].append(relative)  # the manifest itself never saw these bytes
        elif path.is_symlink() or os.lstat(path).st_size != entry["size"]:
            problems["size_mismatch"].append(relative)
        else:
            check.append(relative)
    for relative, scan in zip(check, _scan_all([present[r] for r in check], workers)):
        if scan["state"] != "ok":
            problems["unreadable"].append(relative)
        elif scan["sha256"] != wanted[relative]["sha256"]:
            problems["hash_mismatch"].append(relative)
    counts = {name: len(items) for name, items in problems.items()}
    mismatches = sum(v for k, v in counts.items() if k != "removed")
    result = {"entries": len(entries), "matched": len(entries) - len(set().union(
        *(set(problems[k]) for k in problems if k != "extra"))),
        **counts, "mismatches": mismatches,
        "manifest_sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest()}
    if report is not None:
        _write_private(Path(report), json.dumps({**result, **problems}, sort_keys=True, indent=1) + "\n")
    return result


def parser():
    value = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = value.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("manifest", help="hash a tree and join it to catalog receipts (read-only)")
    build_command.add_argument("--root", type=Path, required=True, help="the instance root")
    build_command.add_argument("--tree", type=Path, required=True, help="the tree to retire")
    build_command.add_argument("--out", type=Path, required=True, help="private output directory outside the tree")
    build_command.add_argument("--system", action="append", default=[], metavar="PREFIX=SYSTEM",
                               help="ledger system whose paths are relative to PREFIX in the tree "
                                    "(PREFIX may be empty); repeatable")
    build_command.add_argument("--dispositions", type=Path, help=f"a {RULES_SCHEMA} file of ordered rules")
    build_command.add_argument("--catalog-file", type=Path, help="a verified catalog backup to read instead of the live catalog")
    build_command.add_argument("--workers", type=int, default=2)
    check_command = commands.add_parser("verify", help="re-hash a restored or archived copy against a manifest")
    check_command.add_argument("--manifest", type=Path, required=True)
    check_command.add_argument("--tree", type=Path, required=True)
    check_command.add_argument("--report", type=Path, help="private (0600) file listing every difference")
    check_command.add_argument("--deletions", type=Path, help="deletions.jsonl: accept a missing entry only when "
                                                              "it records that path and hash as deleted")
    check_command.add_argument("--workers", type=int, default=2)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "manifest":
        result = build(args.root, args.tree, args.out, systems=args.system,
                       rules=load_rules(args.dispositions) if args.dispositions else (),
                       workers=args.workers, catalog_file=args.catalog_file)
        status = 0 if result["ready"] else 1
    else:
        result = verify(args.manifest, args.tree, workers=args.workers, report=args.report,
                        deletions=args.deletions)
        status = 0 if result["mismatches"] == 0 else 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return status


def cli(argv=None):
    try:
        return main(argv)
    except RetirementError as error:
        sys.stderr.write(f"retirement: {error.code}\n")
        return 2


if __name__ == "__main__":
    sys.exit(cli())
