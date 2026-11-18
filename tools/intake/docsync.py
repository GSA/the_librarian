"""Derive, backfill, audit and summarise the ledger's per-document state table (Kata e22r).

Everything here recomputes ``docs`` rows from the requests, which stay authoritative. A document's
cohort and inbox path are not in the ledger's own columns, so they come from the one place the
catalog pins them: the submission sidecar named (with its SHA-256) by each receipt request. A
sidecar that is missing, changed or not a cohort ``local-path`` submission leaves that request out
(counted under ``skipped``), exactly as ``cohort`` attribution treats it.
"""
from pathlib import Path
from urllib.parse import unquote

from tools.extraction import convert, mhtml
from . import docs, store, upload
from .contracts import IntakeError, decode, decode_request, digest, relative


def suffix_of(fmt):
    """The reader-version suffix of a format's extraction request names (see ``cohort_ingest._extract``)."""
    if fmt in convert.FORMATS:
        return convert.request_version(fmt)
    return "-m%d" % mhtml.METHOD_VERSION if fmt == "mhtml" else ""


def _sidecar(root, c, correlation):
    """(system, path, format) a cohort receipt request names in its pinned sidecar, or a skip reason."""
    raw = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation,)).fetchone()[0]
    stage = next(s for s in decode_request(raw)["stages"] if s["operation"] == "catalog-submit")
    meta = next((r["data"] for r in stage["arguments"]["records"]
                 if r["kind"] == "artifact" and r["data"].get("role") == "preserved-metadata"), None)
    if meta is None:
        return "not-a-submission"
    try:
        relative(meta["path"])
        body = upload.read_file(Path(root) / meta["path"], upload.MIB)
        if digest(body) != meta["sha256"]:
            return "sidecar-unavailable"
        value = decode(body)
        origin = value["origin"]
        if origin["kind"] != "local-path" or not origin["system"] or not origin["locator"]:
            return "not-a-cohort-document"
        return origin["system"], unquote(origin["locator"]), value["format"]
    except (IntakeError, OSError, KeyError, TypeError):
        return "sidecar-unavailable"


def derived(root, c):
    """``({(system, path): columns}, skipped)`` recomputed from the requests alone.

    A path's live submission is the head of its receipt requests' correction chain. Two heads for
    one path, or a chain that ends in no single head, leave that path out as ``ambiguous``.
    """
    heads_of = store.Heads()
    heads, skipped, ambiguous = {}, {}, set()
    for (correlation,) in c.execute("SELECT correlation_id FROM requests WHERE correlation_id LIKE 'upload-inbox-%' "
                                    "ORDER BY correlation_id").fetchall():
        try:
            head = heads_of.resolve(c, correlation)
        except IntakeError:
            skipped["ambiguous"] = skipped.get("ambiguous", 0) + 1
            continue
        if head != correlation:
            continue  # superseded by a correction
        named = _sidecar(root, c, correlation)
        if isinstance(named, str):
            skipped[named] = skipped.get(named, 0) + 1
            continue
        system, path, fmt = named
        if (system, path) in heads:
            ambiguous.add((system, path))
        heads[(system, path)] = (correlation, fmt)
    for key in ambiguous:
        del heads[key]
        skipped["ambiguous"] = skipped.get("ambiguous", 0) + 1
    found = {}
    for (system, path), (correlation, fmt) in sorted(heads.items()):
        suffix = suffix_of(fmt)
        submission_id = correlation.removeprefix("upload-")
        try:
            extract = heads_of.resolve(c, "extract-" + submission_id + suffix)
            register = heads_of.resolve(c, "register-" + submission_id + suffix)
            row = docs.derive(c, submission_id, suffix, fmt, extract, register)
        except IntakeError as error:
            if error.code != "ambiguous_correction":
                raise
            row = docs.ambiguous(c, submission_id, suffix, fmt)
        found[(system, path)] = {"format": fmt, **row}
    return found, skipped


def _differences(recomputed, saved):
    out = []
    for key in sorted(set(recomputed) | set(saved)):
        system, path = key
        if key not in saved:
            out.append({"system": system, "path": path, "kind": "missing"})
        elif key not in recomputed:
            out.append({"system": system, "path": path, "kind": "extra"})
        else:
            columns = [n for n in docs.DERIVED if recomputed[key].get(n) != saved[key].get(n)]
            if columns:
                out.append({"system": system, "path": path, "kind": "differing", "columns": columns})
    return out


def _saved(c):
    return {(r["system"], r["path"]): dict(r) for r in c.execute("SELECT * FROM docs")}


def audit(root):
    """Recompute every row from the requests and report each difference; writes nothing."""
    with store.locked(root) as c:
        if not docs.present(c):
            return {"available": False, "differences": [], "counts": {}}
        recomputed, skipped = derived(root, c)
        saved = _saved(c)
        found = _differences(recomputed, saved)
    kinds = {k: sum(d["kind"] == k for d in found) for k in ("missing", "extra", "differing")}
    return {"available": True, "differences": found,
            "counts": {"derived": len(recomputed), "rows": len(saved), **kinds, "skipped": skipped}}


def backfill(root, *, apply=False):
    """Report, and with ``apply`` create the table and add every missing row, from the requests.

    Additive only: the five authoritative tables are never written, and rows already present are
    left as they are (``audit`` reports any that differ). Dropping ``docs`` and running this again
    rebuilds it.
    """
    with store.locked(root) as c:
        present = docs.present(c)
        recomputed, skipped = derived(root, c)
        saved = _saved(c) if present else {}
        missing = [key for key in recomputed if key not in saved]
        report = {"apply": apply, "table_present": present, "derived": len(recomputed),
                  "missing": len(missing), "skipped": skipped}
        if apply:
            with c:
                c.execute("BEGIN")
                if not present:
                    for statement in store.DOCS_DDL.split(";"):
                        if statement.strip():
                            c.execute(statement)
                for system, path in missing:
                    row = recomputed[(system, path)]
                    docs.upsert(c, system, path, row["format"], row)
            report["created"] = len(missing)
        return report


def status(root):
    """Documents per state from the table (one indexed query); ``available`` False without it."""
    with store.locked(root) as c:
        by_state = docs.counts(c)
    if by_state is None:
        return {"available": False}
    return {"available": True, "total": sum(by_state.values()), "by_state": by_state}
