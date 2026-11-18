"""Cohort archive commands: explicit expansion, and verified deletion of an archive original (Kata 12sc).

``expand`` finds every archive a read-only inbox reaches (inbox files, and archive members of saved
expansions) and, with ``apply``, expands those not yet expanded into instance storage (one nesting
level per run: a nested archive found in this run's expansion is expanded by the next run). An
archive with an operator's sibling ``_extracted/<stem>/`` folder is verified against that folder
instead. Without ``apply`` every archive is read and hashed and nothing is written.

``delete_original`` is the one command that writes an inbox, and only with ``apply``: it deletes
one archive after proving that nothing it holds would be lost, appending a durable deletion record
first. It never runs as part of ``classify``, ``ingest`` or ``qualify``.
"""
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from tools.catalog import catalog_store as catalog
from tools.durable import fsync_directory, fsync_file, sha256_file
from . import archives, cohort_ingest, docs, store, upload
from .contracts import IntakeError, require

DELETIONS = "deletions.jsonl"
DELETION_SCHEMA = "librarian-archive-deletion/1"
# Cohort dispositions that never need catalog registration: the file is explicitly skipped or empty.
SKIPPED = {"skipped-asset", "skipped-artifact", "empty"}


def _inbox(root, inbox):
    root, inbox = Path(root).resolve(), Path(inbox).resolve()
    if root == inbox or inbox in root.parents:
        raise IntakeError("instance_inside_inbox")
    if not inbox.is_dir():
        raise IntakeError("inbox_unavailable")
    return root, inbox


def _nested(root, relative, entry):
    """``(relative, path, entry)`` for every archive member of an instance-expanded ``entry``, recursively."""
    manifest = entry.pop("_manifest", None)
    if manifest is None:
        return
    stored = archives.members_dir(archives.store_dir(root), manifest["archive"]["sha256"])
    for member in manifest["members"]:
        if member["disposition"] != "extracted" or not archives.kind(member["path"].rsplit("/", 1)[-1]):
            continue
        name = relative + archives.SEPARATOR + member["path"]
        path = stored / member["path"]
        found = cohort_ingest._disposition(root, path, name, nested=name.count(archives.SEPARATOR))
        yield name, path, found
        yield from _nested(root, name, found)


def _found(root, inbox):
    """``(relative, path, entry)`` for every archive the inbox reaches, in path order."""
    def unavailable(error):
        raise IntakeError("inbox_unavailable") from error
    for directory, names, files in sorted(os.walk(inbox, onerror=unavailable, followlinks=False)):
        names.sort()
        here = Path(directory)
        for name in sorted(files):
            path = here / name
            if ":" in name or not archives.kind(name) or path.is_symlink():
                continue
            relative = path.relative_to(inbox).as_posix()
            entry = cohort_ingest._disposition(root, path, relative)
            yield relative, path, entry
            yield from _nested(root, relative, entry)


def _row(relative, entry, status, manifest=None, code=None):
    row = {"relative": relative, "status": status, "code": code or entry.get("code"),
           "bytes": entry.get("bytes"), "sha256": entry.get("sha256")}
    if manifest is not None:
        row.update(sha256=manifest["archive"]["sha256"], kind=manifest["archive"]["kind"],
                   code=manifest["code"], **archives.summary(manifest))
        if manifest["status"] == "refused":
            row["status"] = "refused"
    return row


def _process(root, inbox, directory, relative, path, entry, apply):
    """The report row of one archive, expanding or verifying it when ``apply`` and it is due."""
    disposition, code = entry["disposition"], entry.get("code")
    if disposition == "archive-expanded" and entry.get("expansion") == "instance":
        return _row(relative, entry, "existing")
    sibling = None
    if entry.get("expansion") == "sibling":
        sibling = path.parent / cohort_ingest.EXPANDED_DIRECTORY / archives.stem(path.name)
    elif code != "archive-not-expanded":
        return _row(relative, entry, "skipped")
    try:
        if directory is not None:
            saved = archives.load(directory, entry.get("sha256") or sha256_file(path))
            if saved is not None:
                return _row(relative, entry, "existing", saved)
        if apply:
            manifest = archives.expand(directory, path, relative, sibling=sibling, inbox=inbox)
            return _row(relative, entry, "pre-expanded" if sibling else "expanded", manifest)
        manifest = archives.read(path, relative, sibling=sibling, inbox=inbox)
        manifest.pop("_run")
        return _row(relative, entry, "would-verify" if sibling else "would-expand", manifest)
    except (IntakeError, OSError) as error:
        return _row(relative, entry, "failed", code=getattr(error, "code", "os-error"))


def expand(root, inbox, *, apply=False):
    """Report every archive the inbox reaches; with ``apply``, expand (or verify) those that are due.

    Statuses: ``existing`` (saved manifest), ``expanded``, ``pre-expanded`` (sibling folder verified;
    members that differ or are missing are ``refused``), ``refused`` (archive-level cap or read
    failure), ``would-expand`` / ``would-verify`` (dry run), ``skipped`` (empty, unsafe or too deep)
    and ``failed``. Nothing is written without ``apply``, and the inbox is never written.
    """
    root, inbox = _inbox(root, inbox)
    directory = archives.store_dir(root, create=apply)
    rows = []
    if apply:
        with archives.locked(directory):
            archives.clear_partials(directory)
            for relative, path, entry in list(_found(root, inbox)):
                rows.append(_process(root, inbox, directory, relative, path, entry, True))
    else:
        for relative, path, entry in _found(root, inbox):
            rows.append(_process(root, inbox, directory, relative, path, entry, False))
    members = Counter()
    for row in rows:
        for key, label in (("member_dispositions", "disposition"), ("member_codes", "code")):
            for value, n in row.get(key, {}).items():
                members["%s:%s" % (label, value)] += n
    return {"counts": {"archives": len(rows), "status": dict(Counter(r["status"] for r in rows)),
                       "members": sum(r.get("members", 0) for r in rows),
                       "expanded_bytes": sum(r.get("expanded_bytes", 0) for r in rows),
                       "member_dispositions": dict(members)},
            "entries": rows}


def _inbox_file(inbox, relative):
    """The inbox path of ``relative`` with no symlink component below the inbox, or ``unsafe_path``."""
    parts = relative.split("/")
    require(relative and not relative.startswith("/") and "\\" not in relative
            and all(p not in {"", ".", ".."} for p in parts), "unsafe_path")
    cursor = inbox
    for part in parts:
        cursor = cursor / part
        require(not cursor.is_symlink(), "unsafe_path")
    return cursor


def _deletions(directory):
    """Every saved deletion record; a torn last line (a crash while appending) is ignored."""
    path = store.safe(Path(directory) / DELETIONS)
    if not path.is_file():
        return []
    lines = upload.read_file(path, archives.MAX_MANIFEST_BYTES).split(b"\n")
    records = []
    for index, line in enumerate(lines):
        if not line:
            continue
        try:
            records.append(json.loads(line.decode("utf-8")))
        except (UnicodeError, ValueError) as error:
            if index != len(lines) - 1:
                raise IntakeError("deletion_record_invalid") from error
    return records


def _append(directory, record):
    """Append one deletion record and make it durable before anything is unlinked."""
    path = store.safe(Path(directory) / DELETIONS)
    created = not path.exists()
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        if stream.tell() and _torn(path):
            stream.write(b"\n")
        stream.write((json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    if created:
        fsync_directory(directory)
    fsync_file(path)


def _torn(path):
    with open(path, "rb") as stream:
        stream.seek(-1, os.SEEK_END)
        return stream.read(1) != b"\n"


def _registered(root, wanted):
    """The subset of ``{relative: sha256}`` whose bytes the catalog holds as a receipt's original.

    Candidate receipts come from the ledger's ``docs`` rows for each path (any cohort); a path counts
    only when the catalog's original artifact of such a receipt has exactly the member's SHA-256.
    """
    if not wanted:
        return set()
    paths = sorted(wanted)
    with store.locked(root) as c:
        require(docs.present(c), "docs_unavailable")
        rows = []
        for start in range(0, len(paths), 500):
            chunk = paths[start:start + 500]
            rows += c.execute("SELECT path, receipt_id FROM docs WHERE path IN (%s)" % ",".join("?" * len(chunk)),
                              chunk).fetchall()
    found = set()
    for path, receipt_id in rows:
        try:
            registration = catalog.receipt_metadata(root, [receipt_id])["registrations"][receipt_id]
        except catalog.CatalogError:
            continue
        artifact = registration["artifact"]["data"]
        if artifact.get("role") == "original" and artifact.get("sha256") == wanted[path]:
            found.add(path)
    return found


def _blockers(root, inbox, relative, manifest, wanted, depth=0):
    """Why deleting the archive would lose something; ``wanted`` gains every path needing a catalog check."""
    blockers = []
    directory = archives.store_dir(root)
    stored = archives.members_dir(directory, manifest["archive"]["sha256"])

    def block(member, name, code, disposition=None):
        blockers.append({"member": name, "code": code, "disposition": disposition,
                         "bytes": member["bytes"]})
    for index, member in enumerate(manifest["members"]):
        name = relative + archives.SEPARATOR + (member["path"] or "#%d" % index)
        if member["disposition"] == "skipped":
            continue
        if member["disposition"] not in {"extracted", "pre-expanded"}:
            block(member, name, member["code"])
            continue
        if member["disposition"] == "pre-expanded":
            name = member["source"]
            path = _inbox_file(inbox, name)
        else:
            path = stored / member["path"]
        if not path.is_file() or path.is_symlink() or sha256_file(path) != member["sha256"]:
            block(member, name, "member-changed")
            continue
        if member["bytes"] == 0:
            continue
        unread = cohort_ingest._unread_disposition(path, member["bytes"]) or {}
        if unread.get("disposition") in SKIPPED:
            continue
        if archives.kind(path.name) and member["disposition"] == "extracted":
            nested = archives.load(directory, member["sha256"])
            if nested is None or nested["status"] != "expanded" or depth + 1 >= cohort_ingest.MAX_NESTING:
                block(member, name, "nested-not-expanded" if nested is None or nested["status"] != "refused"
                      else "nested-" + nested["code"])
            else:
                blockers += _blockers(root, inbox, name, nested, wanted, depth + 1)
            continue
        wanted[name] = (member, path)
    return blockers


def delete_original(root, inbox, relative, *, actor=None, apply=False):
    """Verify that one inbox archive can be deleted without losing anything; with ``apply``, delete it.

    Refused (reported, nothing written) unless the archive's bytes still equal its saved expansion
    manifest's SHA-256, every member has a recorded disposition, every stored member still has its
    recorded bytes, every nested archive is itself expanded and verified, and every member that is
    not explicitly skipped (links, special files, empty files, web assets, stream sidecars) has its
    bytes registered in the catalog as a receipt's original (``not-registered`` carries the member's
    current cohort disposition, such as ``needs-reader`` or ``needs-runtime``).

    With ``apply`` a deletion record (inbox, path, size, SHA-256, manifest SHA-256, actor, time) is
    appended to ``<intake home>/archives/deletions.jsonl`` and fsynced before the file is unlinked.
    Repeating the command after a deletion reports ``already-deleted``.
    """
    root, inbox = _inbox(root, inbox)
    require(not apply or (isinstance(actor, str) and actor.strip()), "actor_required")
    path = _inbox_file(inbox, relative)
    require(archives.kind(path.name) is not None, "not_an_archive")
    directory = archives.store_dir(root, create=apply)
    if directory is None:
        raise IntakeError("archive_not_expanded")
    with archives.locked(directory) if apply else nullcontext():
        report = {"inbox": str(inbox), "relative": relative, "deleted": False}
        if not os.path.lexists(path):
            done = [r for r in _deletions(directory) if r["inbox"] == str(inbox) and r["relative"] == relative]
            require(done, "archive_missing")
            return {**report, "status": "already-deleted", "sha256": done[-1]["sha256"]}
        require(path.is_file(), "not_regular_file")
        before = upload.snapshot(path.stat(follow_symlinks=False))
        sha256 = sha256_file(path)
        report.update(bytes=before[2], sha256=sha256)
        manifest = archives.load(directory, sha256)
        if manifest is None or manifest["status"] == "refused":
            code = "archive-not-expanded" if manifest is None else "archive-" + manifest["code"]
            return {**report, "status": "blocked", "blockers": [{"member": relative, "code": code}]}
        wanted = {}
        blockers = _blockers(root, inbox, relative, manifest, wanted)
        registered = _registered(root, {name: member["sha256"] for name, (member, _) in wanted.items()})
        for name, (member, member_path) in sorted(wanted.items()):
            if name not in registered:
                current = cohort_ingest._disposition(root, member_path, name, nested=1)
                blockers.append({"member": name, "code": "not-registered",
                                 "disposition": current["disposition"], "bytes": member["bytes"]})
        manifest_sha256 = archives.manifest_sha256(directory, sha256)
        report.update(manifest_sha256=manifest_sha256, members=len(manifest["members"]),
                      registered=len(registered), blockers=blockers,
                      blocked_by_code=dict(Counter(b["code"] for b in blockers)))
        if blockers:
            return {**report, "status": "blocked"}
        if not apply:
            return {**report, "status": "verified"}
        if upload.snapshot(path.stat(follow_symlinks=False)) != before:
            raise IntakeError("changed_input")
        record = {"schema": DELETION_SCHEMA, "inbox": str(inbox), "relative": relative, "bytes": before[2],
                  "sha256": sha256, "manifest_sha256": manifest_sha256, "actor": actor,
                  "deleted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        prior = [r for r in _deletions(directory) if all(r.get(k) == record[k] for k in
                                                         ("inbox", "relative", "sha256", "manifest_sha256"))]
        if not prior:
            _append(directory, record)
        os.unlink(path)
        fsync_directory(path.parent)
        return {**report, "status": "deleted", "deleted": True}
