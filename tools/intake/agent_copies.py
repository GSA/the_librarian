"""Inbox files that copy a declared agent-written tree (``cohort agent-copies``; Kata rqr2).

Agent-written context that is copied into another inbox (a snapshot, a backup, a work-tree copy)
is ingested without the declaration the original carries, so it would qualify at normal confidence
and could outrank the correctly low-confidence original. This report finds such copies in the
intake ledger. The files of the named ``agent_systems`` that were submitted with an
``origin.agent_generated`` declaration are the reference; every other ingested file whose inbox path
ends with a reference file's inbox-relative path (whole segments; ``ARCHIVE!/MEMBER`` counts as
``ARCHIVE/MEMBER``) is compared by content:

* ``identical``: the same original SHA-256;
* ``similar``: at least ``min_similarity`` (default 0.9) of the lines in common (``2 * shared /
  (lines + lines)``, counting repeated lines by multiplicity and ignoring order, so the work is linear
  in the file size), so a snapshot that predates or follows an edit is found;
* anything else is only counted ``different``, since a path tail alone proves nothing (many trees
  have a ``README.md``).

A file that itself carries an agent declaration is counted ``already_declared`` and not reported.
The report is read-only and decides nothing: it lists receipts, and a report written with
``--report`` is accepted by ``deselect --receipts-file`` once the owner has looked at it. Files over
``COMPARE_LIMIT`` bytes, and retained originals that are missing or no longer match their recorded
SHA-256, are not compared by lines (``not_compared``) unless byte-identical by the ledger's hashes.
With ``manifest``, a selected receipt that a newer submission of the same inbox path has replaced
(so no ``docs`` row names it) is still examined, from its catalog registration and pinned sidecar,
because ``deselect`` can only remove what the report names.
"""
from collections import Counter
from pathlib import Path

from tools.context_index import context_index as index
from tools.catalog import catalog_store as catalog
from . import docs, store, upload
from .contracts import IntakeError, decode, digest, relative, require
from urllib.parse import unquote

SCHEMA = "librarian-agent-copies-report/1"
MIN_SIMILARITY = 0.9
COMPARE_LIMIT = 1024 * 1024


def _tail(path):
    return path.replace("!/", "/")


def _declared(root, original):
    """The ``agent_generated`` declaration in the submission sidecar next to ``original``, else ``None``."""
    if not original:
        return None
    try:
        body = upload.read_file((Path(root) / original).with_name(upload.SIDECAR), upload.MIB)
        declared = decode(body)["origin"].get("agent_generated")
        if declared is not None:
            upload.agent_declaration(declared)
        return declared
    except (IntakeError, OSError, KeyError, TypeError, AttributeError):
        return None


def _lines(root, row):
    """The lines of ``row``'s retained original, only when its bytes are the ones the ledger recorded."""
    try:
        raw = upload.read_file(Path(root) / row["original"], COMPARE_LIMIT)
    except (IntakeError, OSError):
        return None
    return Counter(raw.decode("utf-8", "replace").splitlines()) if digest(raw) == row["sha256"] else None


def _similarity(root, agent, other):
    first, second = _lines(root, agent), _lines(root, other)
    if first is None or second is None:
        return None
    total = sum(first.values()) + sum(second.values())
    return 1.0 if total == 0 else 2 * sum((first & second).values()) / total


def _selected(root, manifest):
    if manifest is None:
        return None
    try:
        value = decode(index.no_symlinks(Path(root) / manifest).read_bytes())
        return {e["receipt_id"] for e in value.get("renditions", []) if isinstance(e, dict) and "receipt_id" in e}
    except (index.IndexError_, OSError, AttributeError) as error:
        raise IntakeError("agent_copies_manifest_invalid") from error


def _historical(root, receipts):
    """Rows for selected ``receipts`` no ``docs`` row names, from the catalog and each pinned sidecar."""
    found = []
    if not receipts:
        return found
    try:
        held = catalog.receipt_metadata(root, sorted(receipts))["registrations"]
    except (catalog.CatalogError, OSError, ValueError) as error:
        raise IntakeError("agent_copies_catalog_unavailable") from error
    for receipt, registration in sorted(held.items()):
        data = registration["artifact"]["data"]
        pinned = next((p for p in registration["receipt"]["data"]["provenance"]
                       if p["path"].endswith("/" + upload.SIDECAR)), None)
        try:
            relative(data["path"])
            body = upload.read_file(Path(root) / pinned["path"], upload.MIB)
            require(digest(body) == pinned["sha256"])
            origin = decode(body)["origin"]
            require(origin["kind"] == "local-path")
            declared = origin.get("agent_generated")
            found.append({"system": origin["system"], "path": unquote(origin["locator"]),
                          "sha256": data["sha256"], "receipt_id": receipt, "original": data["path"],
                          "declared": declared is not None, "current": False})
        except (IntakeError, OSError, KeyError, TypeError, AttributeError):
            continue
    return found


def report(root, *, agent_systems, min_similarity=MIN_SIMILARITY, manifest=None):
    """The copies of declared agent files in ``root``'s other cohorts: ``{schema, counts, entries}``.

    ``manifest`` (an instance-relative selection manifest) marks each copy ``selected`` or not.
    """
    root = Path(root).resolve()
    require(agent_systems, "agent_copies_system_required")
    require(isinstance(min_similarity, (int, float)) and 0 < min_similarity <= 1, "invalid_similarity")
    selected = _selected(root, manifest)
    with store.locked(root) as c:
        require(docs.present(c), "agent_copies_docs_unavailable")
        rows = [dict(r) for r in c.execute(
            "SELECT system, path, sha256, receipt_id, original FROM docs WHERE receipt_id IS NOT NULL "
            "ORDER BY system, path")]
    for row in rows:
        row["declared"] = _declared(root, row["original"]) is not None
        row["current"] = True
    if selected:
        rows += _historical(root, selected - {r["receipt_id"] for r in rows})
    systems = set(agent_systems)
    agents = [r for r in rows if r["system"] in systems and r["declared"] and r["current"]]
    require(agents, "agent_copies_no_declared_files")
    others = [r for r in rows if r["system"] not in systems]
    by_tail = {}
    for agent in agents:
        by_tail.setdefault(_tail(agent["path"]), []).append(agent)
    counts = {"agent_files": len(agents), "candidates": len(others), "tail_matches": 0, "identical": 0,
              "similar": 0, "different": 0, "already_declared": 0, "not_compared": 0}
    entries = []
    for other in others:
        path = _tail(other["path"])
        parts = path.split("/")
        matches = [a for i in range(len(parts)) for a in by_tail.get("/".join(parts[i:]), [])]
        if not matches:
            continue
        counts["tail_matches"] += 1
        if other["declared"]:
            counts["already_declared"] += 1
            continue
        found = None
        for agent in sorted(matches, key=lambda a: (-len(a["path"]), a["system"], a["path"])):
            if agent["sha256"] and agent["sha256"] == other["sha256"]:
                found = ("identical", 1.0, agent)
                break
            ratio = _similarity(root, agent, other)
            if ratio is None:
                counts["not_compared"] += 1
            elif ratio >= min_similarity and (found is None or ratio > found[1]):
                found = ("similar", round(ratio, 4), agent)
        if found is None:
            counts["different"] += 1
            continue
        kind, ratio, agent = found
        counts[kind] += 1
        entry = {"receipt_id": other["receipt_id"], "system": other["system"], "inbox_path": other["path"],
                 "sha256": other["sha256"], "match": kind, "similarity": ratio,
                 "agent_receipt_id": agent["receipt_id"], "agent_system": agent["system"],
                 "agent_path": agent["path"], "current": other["current"]}
        if selected is not None:
            entry["selected"] = other["receipt_id"] in selected
        entries.append(entry)
    if selected is not None:
        counts["selected"] = sum(e["selected"] for e in entries)
    return {"schema": SCHEMA, "min_similarity": min_similarity, "counts": counts, "entries": entries}
