"""Inbox files that copy a declared agent-written tree (``cohort agent-copies``; Kata rqr2).

Agent-written context that is copied into another inbox (a snapshot, a backup, a work-tree copy)
is ingested without the declaration the original carries, so it would qualify at normal confidence
and could outrank the correctly low-confidence original. This report finds such copies in the
intake ledger. The files of the named ``agent_systems`` that were submitted with an
``origin.agent_generated`` declaration are the reference; every other ingested file whose inbox path
ends with a reference file's inbox-relative path (whole segments; ``ARCHIVE!/MEMBER`` counts as
``ARCHIVE/MEMBER``) is compared by content:

* ``identical``: the same original SHA-256;
* ``similar``: at least ``min_similarity`` (default 0.9) of the lines in common, so a snapshot that
  predates or follows an edit is found;
* anything else is only counted ``different``, since a path tail alone proves nothing (many trees
  have a ``README.md``).

A file that itself carries an agent declaration is counted ``already_declared`` and not reported.
The report is read-only and decides nothing: it lists receipts, and a report written with
``--report`` is accepted by ``deselect --receipts-file`` once the owner has looked at it. Files over
``COMPARE_LIMIT`` bytes are not compared by lines (``not_compared``) unless byte-identical.
"""
import difflib
from pathlib import Path

from tools.context_index import context_index as index
from . import docs, store, upload
from .contracts import IntakeError, decode, require

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


def _lines(root, original):
    try:
        raw = upload.read_file(Path(root) / original, COMPARE_LIMIT)
    except (IntakeError, OSError):
        return None
    return raw.decode("utf-8", "replace").splitlines()


def _similarity(root, agent, other):
    first, second = _lines(root, agent["original"]), _lines(root, other["original"])
    if first is None or second is None:
        return None
    return difflib.SequenceMatcher(None, first, second, autojunk=False).ratio()


def _selected(root, manifest):
    if manifest is None:
        return None
    try:
        value = decode(index.no_symlinks(Path(root) / manifest).read_bytes())
        return {e["receipt_id"] for e in value.get("renditions", []) if isinstance(e, dict) and "receipt_id" in e}
    except (index.IndexError_, OSError, AttributeError) as error:
        raise IntakeError("agent_copies_manifest_invalid") from error


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
    systems = set(agent_systems)
    agents = [r for r in rows if r["system"] in systems and _declared(root, r["original"]) is not None]
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
        if _declared(root, other["original"]) is not None:
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
                 "agent_path": agent["path"]}
        if selected is not None:
            entry["selected"] = other["receipt_id"] in selected
        entries.append(entry)
    if selected is not None:
        counts["selected"] = sum(e["selected"] for e in entries)
    return {"schema": SCHEMA, "min_similarity": min_similarity, "counts": counts, "entries": entries}
