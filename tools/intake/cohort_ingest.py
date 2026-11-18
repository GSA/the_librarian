"""Inbox classification and ingestion for a read-only local cohort.

Every file below a read-only inbox gets exactly one disposition. Admitted files are submitted
through manual upload intake as ``local-path`` copies (the inbox is never written, moved or given a
remote identity) and extracted through the durable ledger. Extraction is not review: renditions stay
deferred until a standing-policy review qualifies them (``cohort.qualify``).
"""
from collections import Counter
import hashlib
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import quote

from tools.catalog import catalog_store as catalog
from tools.extraction import extract, mhtml
from tools.durable import sha256_file
from . import agent_origin, archives, cohort_runtime, contracts, docs, runner, store, upload
from .contracts import IntakeError, LedgerBusy
from .docsync import suffix_of

ASSET_SUFFIXES = {".js", ".css", ".ico", ".map", ".woff", ".woff2", ".ttf", ".svg"}
RUNTIME_FORMATS = cohort_runtime.SUFFIXES
EXPANDED_DIRECTORY = "_extracted"
MAX_NESTING = 8  # archive levels classify looks into (``a.zip!/b.zip!/...``)
REASON = "Read-only local cohort intake; copy only, not extraction review or qualification."
OUTCOMES = ("received", "extracted", "failed", "changed-since-intake", "not-processed")
FLUSH = 500  # documents whose rows are written (or confirmed) in one ledger open
# Documents whose catalog registrations share one revision, per stage (the receipt, then the extraction
# registration). 1 keeps the one-revision-per-registration path.
BATCH_SIZE = 50
BUSY_FAILURE = {"outcome": "failed", "stage": "ledger", "status": "retryable", "category": "retryable",
                "code": "ledger_busy", "attempts": runner.BUSY_RETRIES}
# A correction chain the store cannot follow to one valid head fails its entry, not the run.
CHAIN_REFUSALS = {"ambiguous_correction", "correction_mismatch"}


def submission(relative, system, fmt=None, content_sha256=None):
    """Stable ASCII-safe submission identity, name and locator for one inbox path.

    Files read as MIME HTML get a distinct identity from the plain-text identity an
    earlier toolkit version may have used, so they are received and extracted again.
    When an inbox file's bytes change after intake, ``content_sha256`` derives a new
    submission identity that supersedes the prior one via ``correction_of``.
    """
    salt = ("\0" + fmt if fmt == "mhtml" else "") + ("\0" + content_sha256 if content_sha256 else "")
    key = hashlib.sha256((system + "\0" + relative + salt).encode("utf-8")).hexdigest()[:24]
    base = unicodedata.normalize("NFKD", relative.rsplit("/", 1)[-1])
    base = base.encode("ascii", "ignore").decode("ascii")
    stem, dot, suffix = base.rpartition(".") if "." in base else (base, "", "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "file"
    suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix)
    name = (stem[:180] + (dot + suffix if suffix else "")).lstrip("-._")
    if name.endswith(upload.DESCRIPTOR_SUFFIX) or name == upload.SIDECAR:
        name = "file-" + name
    return {"submission_id": "inbox-" + key, "name": name,
            "locator": quote(relative, safe="/")}


def _format(path, raw, root):
    """Return (format, refusal code); MIME content wins over any text suffix."""
    suffix = path.suffix.lower()
    if suffix == ".odt":
        return "odt", None
    if suffix in extract.TEXT_SUFFIXES and mhtml.recognised(raw):
        try:
            mhtml.payload(raw)
        except mhtml.MhtmlError as error:
            return None, error.code
        return "mhtml", None
    if suffix == ".txt":
        try:
            extract.gemini(raw, root)
            return "gemini-txt", None
        except (extract.ExtractionError, UnicodeDecodeError):
            return "text", None
    if suffix in extract.TEXT_SUFFIXES:
        return "text", None
    return None, None


def _stat(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _unchanged(row, identity):
    """True when an extracted document's row recorded exactly this (non-empty) file's stat identity."""
    return (row is not None and row["state"] == "extracted" and row["ino"] is not None
            and identity == tuple(row[k] for k in docs.STAT) and identity[2] > 0)


def _unread_disposition(path, size, provisioned=False):
    """The disposition a file gets from its name and size alone, or None when it must be read.

    PDFs and images are read only when the instance's Docling runtime is ``provisioned``.
    """
    suffix = path.suffix.lower()
    if ":" in path.name:
        # Windows alternate-data-stream sidecars (Zone.Identifier, DLP tags).
        return {"disposition": "skipped-artifact"}
    if suffix in ASSET_SUFFIXES:
        return {"disposition": "skipped-asset"}
    if size == 0:
        return {"disposition": "empty"}
    if suffix in RUNTIME_FORMATS:
        return cohort_runtime.unread_disposition(path, size, provisioned)
    if suffix not in extract.TEXT_SUFFIXES | {".odt"}:
        return {"disposition": "needs-reader"}
    if size > max(upload.FORMATS[f][1] for f in ("text", "gemini-txt", "mhtml", "odt")):
        return {"disposition": "over-limit"}
    return None


def _read_disposition(root, path, entry):
    """The disposition of a text, ODT, PDF or image file from its bytes; ``entry`` gains its format and hash."""
    if path.suffix.lower() in RUNTIME_FORMATS:
        return cohort_runtime.read_disposition(path, entry)
    try:
        raw = upload.read_file(path, max(upload.FORMATS[f][1] for f in ("text", "gemini-txt", "mhtml", "odt")))
    except IntakeError as error:
        return {**entry, "disposition": "unsafe-path", "code": error.code}
    fmt, refusal = _format(path, raw, root)
    entry.update(format=fmt, sha256=hashlib.sha256(raw).hexdigest())
    if refusal:
        # MIME that cannot be read strictly is never admitted as raw encoded text.
        return {**entry, "disposition": "needs-reader", "code": refusal}
    if entry["bytes"] > upload.FORMATS[fmt][1]:
        return {**entry, "disposition": "over-limit"}
    try:
        upload.check_type(raw, fmt, path.name)
    except IntakeError as error:
        return {**entry, "disposition": "unreadable", "code": error.code}
    return {**entry, "disposition": "admit"}


# Cohort dispositions of archive members that carry no stored bytes (``archives`` member codes).
MEMBER_DISPOSITIONS = dict.fromkeys(archives.UNREADABLE, "unreadable")


def _archive_disposition(root, path, inbox_file):
    """An archive's disposition: expanded by a sibling ``_extracted/<stem>/`` folder (inbox files
    only) or by a saved instance expansion, refused, or ``needs-reader`` until it is expanded.

    An instance-expanded entry carries its manifest as the private ``_manifest`` field.
    """
    if inbox_file and (path.parent / EXPANDED_DIRECTORY / archives.stem(path.name)).is_dir():
        return {"disposition": "archive-expanded", "expansion": "sibling"}
    directory = archives.store_dir(root)
    if directory is None:
        return {"disposition": "needs-reader", "code": "archive-not-expanded"}
    sha256 = sha256_file(path)
    manifest = archives.load(directory, sha256)
    if manifest is None or manifest["status"] == "pre-expanded":
        return {"sha256": sha256, "disposition": "needs-reader", "code": "archive-not-expanded"}
    if manifest["status"] == "refused":
        return {"sha256": sha256, "disposition": "needs-reader", "code": "archive-" + manifest["code"]}
    return {"sha256": sha256, "disposition": "archive-expanded", "expansion": "instance",
            "_manifest": manifest}


def _member_entries(root, relative, manifest, trusted, provisioned=False):
    """One entry per file member of an instance-expanded archive, named ``<archive>!/<member>``.

    Stored members are classified like inbox files from their stored bytes (``_source``); members
    stored without bytes keep their archive code. A nested archive deeper than ``MAX_NESTING`` is
    not looked into.
    """
    directory = archives.store_dir(root)
    stored = archives.members_dir(directory, manifest["archive"]["sha256"])
    entries = []
    for index, member in enumerate(manifest["members"]):
        name = relative + archives.SEPARATOR + (member["path"] or "#%d" % index)
        if member["disposition"] != "extracted":
            entries.append({"relative": name, "disposition": MEMBER_DISPOSITIONS.get(member["code"], "unsafe-path"),
                            "format": None, "bytes": member["bytes"], "code": "archive-" + member["code"]})
            continue
        source = stored / member["path"]
        entry = _disposition(root, source, name, trusted, nested=name.count(archives.SEPARATOR),
                             provisioned=provisioned)
        current = entry.get("sha256")
        if current is None and source.is_file() and not source.is_symlink():
            # Dispositions decided without reading (empty, assets, over-limit, ...) still verify the bytes.
            current = sha256_file(source)
        if current != member["sha256"]:
            entry = {"relative": name, "disposition": "unsafe-path", "format": None,
                     "code": "archive-member-changed"}
        entries.append({**entry, "_source": source})
    return entries


def _disposition(root, path, relative, trusted=None, nested=0, provisioned=False):
    """One file's disposition.

    ``trusted`` (``{relative: ledger row}``, only under ``--trust-stat``) lets a file whose stat
    identity equals its extracted row's skip the read and hash: the row's format and SHA-256 stand in
    for them, and the entry is marked ``_fast``. Every entry carries its stat identity as ``_stat``, taken
    before the read, so a file that changes while it is read is recorded with the older identity and
    is hashed again next run. ``ingest`` removes both private keys before it reports.
    """
    entry = {"relative": relative, "disposition": None, "format": None}
    if path.is_symlink() or not path.is_file():
        return {**entry, "disposition": "unsafe-path"}
    info = path.stat(follow_symlinks=False)
    entry["_stat"] = _stat(info)
    row = (trusted or {}).get(relative)
    if _unchanged(row, entry["_stat"]):
        return {**entry, "format": row["format"], "bytes": info.st_size, "sha256": row["sha256"],
                "disposition": "admit", "_fast": True}
    entry["bytes"] = info.st_size
    if ":" not in path.name and archives.kind(path.name) and info.st_size > 0:
        if nested >= MAX_NESTING:
            return {**entry, "disposition": "needs-reader", "code": "archive-nesting"}
        return {**entry, **_archive_disposition(root, path, not nested)}
    unread = _unread_disposition(path, info.st_size, provisioned)
    if unread is not None:
        return {**entry, **unread}
    return _read_disposition(root, path, entry)


def classify(root, inbox, trusted=None, keep_stat=False, origin=None):
    """Return one explicit disposition per inbox file; never writes the inbox.

    ``ingest`` passes ``keep_stat`` to keep each entry's private ``_stat`` (and ``_fast``) fields.
    An agent ``origin`` (``agent_origin.load``) limits the walk to its includes and marks files
    under its excludes ``excluded`` without reading them.
    """
    root, inbox = Path(root).resolve(), Path(inbox).resolve()
    if root == inbox or inbox in root.parents:
        raise IntakeError("instance_inside_inbox")
    if not inbox.is_dir():
        raise IntakeError("inbox_unavailable")

    def unavailable(error):
        raise IntakeError("inbox_unavailable") from error
    provisioned = cohort_runtime.ready(root)
    entries = []

    def add(path):
        relative = path.relative_to(inbox).as_posix()
        if agent_origin.excluded(origin, relative):
            entries.append({"relative": relative, "disposition": "excluded", "format": None})
            return
        pending = [_disposition(root, path, relative, trusted, provisioned=provisioned)]
        while pending:
            entry = pending.pop()
            manifest = entry.pop("_manifest", None)
            if manifest is not None:
                pending.extend(_member_entries(root, entry["relative"], manifest, trusted, provisioned))
            if not keep_stat:
                for key in ("_stat", "_fast", "_source"):
                    entry.pop(key, None)
            entries.append(entry)
    for include, start in agent_origin.starts(origin, inbox):
        if include and any(p.is_symlink() for p in (start, *start.parents) if inbox in p.parents):
            entries.append({"relative": include, "disposition": "unsafe-path", "format": None})
            continue
        if include and not start.is_dir():
            add(start)
            continue
        for directory, names, files in os.walk(start, onerror=unavailable, followlinks=False):
            here = Path(directory)
            for name in [*files, *(n for n in names if (here / n).is_symlink())]:
                add(here / name)
    return sorted(entries, key=lambda e: e["relative"])


def source_path(inbox, entry):
    """The file an entry's bytes are read from: its inbox path, or a stored archive member."""
    return entry.get("_source") or Path(inbox) / entry["relative"]


def _staged(root, correlation_id, stage_id, operation, arguments, identity=None, heads=None):
    """Stage one single-stage request unless it exists; return the summary of its chain head.

    A request that was corrected is represented by the newest request of its validated
    ``correction_of`` chain, whatever it is named (``heads`` shares one chain index across a
    run). The head must repeat this request's operation and the ``identity`` arguments (receipt and
    format), or ``correction_mismatch`` refuses it. ``arguments`` may be a callable, evaluated
    only when the request must be staged.
    """
    current = store.find_head(root, correlation_id, heads)
    if current is None:
        return store.stage_request(root, correlation_id, [{
            "id": stage_id, "kind": "extraction", "operation": operation,
            "arguments": arguments() if callable(arguments) else arguments}])
    if current["correlation_id"] != correlation_id:
        saved = store.request(root, current["correlation_id"])["stages"]
        if (len(saved) != 1 or saved[0]["operation"] != operation
                or any(saved[0]["arguments"].get(k) != v for k, v in (identity or {}).items())):
            raise IntakeError("correction_mismatch")
    return current


def _request(root, correlation_id, stage_id, operation, arguments, identity=None, heads=None):
    """``_staged``, then settle the stage when it can still run; return the stage summary.

    The head is settled and reported like any other request: succeeded, blocked, or still staged.
    """
    current = _staged(root, correlation_id, stage_id, operation, arguments, identity, heads)
    stage = current["stages"][0]
    if stage["status"] in {"staged", "running", "retryable"}:
        stage = runner.settle(root, current["correlation_id"], stage["id"])["stages"][0]
    return stage


def _conflict(entry, count=1):
    entry["conflicts"] = entry.get("conflicts", 0) + count


def _failure(stage_id, stage):
    """A failed entry with its stage, status, category, code and attempt count (no private text)."""
    last = stage["attempts"][-1] if stage["attempts"] else {}
    return {"outcome": "failed", "stage": stage_id, "status": stage["status"],
            "category": last.get("category"), "code": last.get("code") or stage["status"],
            "attempts": len(stage["attempts"])}


def _settled_outcome(stage_id, stage):
    """The entry outcome of a settled extraction stage."""
    if stage["status"] != "succeeded":
        return _failure(stage_id, stage)
    evidence = stage["evidence"]
    return {"outcome": "extracted", "rendition": evidence["rendition"],
            "rendition_sha256": evidence["rendition_sha256"]}


def _receipt_refused(error):
    return {"outcome": "changed-since-intake" if error.code == "changed_input" else "failed",
            "stage": "receipt", "code": error.code}


def _identity(entry):
    """The extraction requests' shared arguments and reader-version name suffix of one entry."""
    # The reader version is part of the request identity: a new MIME reader version extracts again.
    return {"receipt": entry["receipt_id"], "format_name": entry["format"]}, suffix_of(entry["format"])


def _prepare_name(entry):
    return "extract-" + entry["submission_id"] + _identity(entry)[1]


def _prepare(root, entry, heads):
    base, _ = _identity(entry)
    stage = _request(root, _prepare_name(entry), "prepare",
                     "extract-prepare", base, base, heads)
    _conflict(entry, runner.conflicts(stage))
    return stage


def _extract(root, entry, actor, heads=None):
    base, version = _identity(entry)
    step = "prepare"
    try:
        stage = _prepare(root, entry, heads)
        if stage["status"] == "succeeded":
            step = "register"
            # Registration pins the catalog state when staged, immediately before it runs;
            # a repeated registration is idempotent by its saved request.
            # The catalog state is read only when a new registration is staged.
            stage = _request(root, "register-" + entry["submission_id"] + version, "register",
                             "extract-register", lambda: {
                                 **base, "actor": actor,
                                 "expected_state": catalog.status(root)["state_token"]}, base, heads)
            _conflict(entry, runner.conflicts(stage))
    except IntakeError as error:
        if error.code not in CHAIN_REFUSALS:
            raise
        return {"outcome": "failed", "stage": step, "code": error.code}
    return _settled_outcome(stage["id"], stage)


def _identify(root, entry, system, known):
    """The submission identity of one entry (``entry`` gains it) and the request it supersedes, if any.

    ``known`` maps correlation IDs to the saved ``(request, summary)`` read earlier. Dropped, with the
    identity replaced, as soon as the file's bytes differ from the saved original. Returns
    ``(superseded, known)``.
    """
    identity = submission(entry["relative"], system, entry["format"])
    superseded = None
    prior = next((request for request, _ in known.values()), None)
    if prior is not None:
        orig = next((r for r in upload.submit_records(prior)
                     if r["kind"] == "artifact" and r["data"].get("role") == "original"), None)
        if orig is not None and orig["data"]["sha256"] != entry["sha256"]:
            superseded = identity["submission_id"]
            identity = submission(entry["relative"], system, entry["format"], entry["sha256"])
            known = {}
        elif prior.get("correction_of"):
            # An existing identity keeps the correction it was first submitted with.
            superseded = prior["correction_of"].removeprefix("upload-")
    elif entry["format"] == "mhtml":
        # An entry ingested before MIME recognition (as plain text) is corrected, not kept.
        earlier = submission(entry["relative"], system)["submission_id"]
        if upload.ledger_request(root, upload.correlation(earlier)) is not None:
            superseded = earlier
    entry.update(identity)
    return superseded, known


def _descriptor(entry, system, actor, superseded):
    return upload.descriptor(
        submission_id=entry["submission_id"], format_name=entry["format"], correction_of=superseded,
        name=entry["name"], actor=actor, reason=REASON, origin_kind="local-path",
        origin_system=system, origin_locator=entry["locator"], expected_sha256=entry["sha256"],
        agent_generated=entry.get("agent_generated"))


def _correlation(entry, system):
    return upload.correlation(submission(entry["relative"], system, entry["format"])["submission_id"])


def _acknowledged(entry, ack):
    entry.update(original=ack["original"]["path"], receipt_id=ack["records"]["receipt_id"])


def _received(entry, ack, receipt):
    """Count an acknowledged receipt's conflicts on ``entry``; return its failure, or None once it committed."""
    _conflict(entry, runner.conflicts(receipt))
    if ack["receipt"]["state"] != "committed":
        return _failure("receipt", receipt)
    entry["received"] = True
    return None


def _process(root, inbox, entry, system, actor, heads=None):
    # One ledger open yields the saved receipt request and its stage summary; submit and the receipt
    # check below reuse them for a succeeded receipt.
    known = upload.known_requests(root, [_correlation(entry, system)])
    superseded, known = _identify(root, entry, system, known)
    try:
        ack = upload.submit(root, source_path(inbox, entry), _descriptor(entry, system, actor, superseded),
                            known=known)
    except LedgerBusy:
        raise  # retried by _settled, never reported as a receipt refusal
    except IntakeError as error:
        return _receipt_refused(error)
    _acknowledged(entry, ack)
    receipt = (upload.settled_summary(known, ack["correlation_id"])
               or store.inspect(root, ack["correlation_id"]))["stages"][0]
    return _received(entry, ack, receipt) or _extract(root, entry, actor, heads)


def _settled(root, inbox, entry, system, actor, heads=None):
    """Process one entry, retrying a busy ledger within the entry's attempt budget.

    Every step is idempotent by request identity, so repeating the entry is safe. An
    entry that is still busy after the budget fails visibly as retryable ``ledger_busy``
    and the run continues; the next run resumes it.
    """
    for _ in range(runner.BUSY_RETRIES):
        try:
            return _process(root, inbox, entry, system, actor, heads)
        except LedgerBusy:
            _conflict(entry)
    return dict(BUSY_FAILURE)


def _trusted_entry(entry, row, system):
    """The report entry of an extracted document whose file still has the stat identity its row recorded.

    Everything but the ledger-side facts is a function of the path and format; those come from the row.
    The result equals what ``_process`` returns for the same unchanged document.
    """
    identity = submission(entry["relative"], system, entry["format"])
    entry.update(submission_id=row["submission_id"], name=identity["name"], locator=identity["locator"],
                 original=row["original"], receipt_id=row["receipt_id"], conflicts=row["conflicts"],
                 received=True)
    return {"outcome": "extracted", "rendition": row["rendition"], "rendition_sha256": row["rendition_sha256"]}


def _derived(c, heads, entry, row, suffix):
    """The ``docs`` row ``entry``'s saved requests derive, reusing ``row``'s receipt facts when it matches."""
    sid = entry["submission_id"]
    try:
        extract_head = heads.resolve(c, "extract-" + sid + suffix)
        register_head = heads.resolve(c, "register-" + sid + suffix)
        if not (row and row["submission_id"] == sid and row["suffix"] == suffix):
            return docs.derive(c, sid, suffix, entry["format"], extract_head, register_head)
        derived = {k: row[k] for k in ("submission_id", "suffix", "receipt_id", "original", "sha256")}
        derived.update(extract_head=extract_head, register_head=register_head,
                       **docs.measure(c, sid, suffix, entry["format"], row["receipt_id"],
                                      extract_head, register_head))
        return derived
    except IntakeError as error:
        if error.code != "ambiguous_correction":
            raise
        return docs.ambiguous(c, sid, suffix, entry["format"])


def _record(root, system, pending, heads):
    """Write or refresh the ``docs`` rows of ``pending`` entries in one ledger open.

    Each row is a function of the saved requests (``docs.derive``) plus the file's stat identity. The
    identity is kept only when the file's hash equals the retained original's, so a changed file can
    never be trusted by its old row. A row that already equals what would be written is left alone, so
    replaying an unchanged inbox writes nothing. A ledger without the table is not migrated.
    """
    if not pending:
        return
    now = time.time()
    with store.locked(root) as c:
        if not docs.present(c):
            return
        saved = docs.load(c, system, [entry["relative"] for entry, _ in pending])
        with c:
            for entry, stat in pending:
                if not entry.get("submission_id"):
                    continue
                if c.execute("SELECT 1 FROM requests WHERE correlation_id=?",
                             ("upload-" + entry["submission_id"],)).fetchone() is None:
                    continue
                row = saved.get(entry["relative"])
                derived = _derived(c, heads, entry, row, suffix_of(entry["format"]))
                keep = stat if entry.get("sha256") == derived["sha256"] else None
                wanted = {**derived, "format": entry["format"], **dict(zip(docs.STAT, keep or (None,) * 5))}
                if row and all(row[k] == wanted[k] for k in (*docs.DERIVED, *docs.STAT)):
                    continue
                docs.upsert(c, system, entry["relative"], entry["format"], derived, keep, now)


def _stage_receipts(root, inbox, entries, system, actor, outcomes):
    """Stage each entry's receipt request; return ``(entry, correlation ID, existing, known)`` per staged entry.

    An entry that cannot be staged gets its outcome in ``outcomes``. Raises ``LedgerBusy`` when the
    saved requests of the chunk cannot be read at all.
    """
    correlations = {e["relative"]: _correlation(e, system) for e in entries}
    known_all = upload.known_requests(root, list(correlations.values()))
    staged = []
    for entry in entries:
        try:
            known = {c: v for c, v in known_all.items() if c == correlations[entry["relative"]]}
            superseded, known = _identify(root, entry, system, known)
            correlation_id, existing = upload.stage_submission(
                root, source_path(inbox, entry), _descriptor(entry, system, actor, superseded), known=known)
            staged.append((entry, correlation_id, existing, known))
        except LedgerBusy:
            _conflict(entry)
            outcomes[entry["relative"]] = dict(BUSY_FAILURE)
        except IntakeError as error:
            outcomes[entry["relative"]] = _receipt_refused(error)
    return staged


def _process_batch(root, inbox, entries, system, actor, heads, concurrency=1):
    """``_process`` for a chunk of entries: their receipts commit as one catalog revision, then
    their extraction registrations as one. Each entry gets exactly the outcome ``_process`` would give.

    Every step is idempotent by request identity (and the catalog's per-document marker records), so a
    crash or refusal anywhere is completed by running the chunk again; a refused entry never stops
    the others. Returns ``{relative path: outcome}``.
    """
    outcomes = {}
    with extract.keeping_parsed(len(entries)):
        try:
            staged = _stage_receipts(root, inbox, entries, system, actor, outcomes)
        except LedgerBusy:
            return {e["relative"]: dict(BUSY_FAILURE) for e in entries}
        try:
            pending = [(corr, "receipt") for _, corr, _, known in staged if upload.settled(known, corr) is None]
            if pending:
                runner.settle_batch(root, pending)
            fresh = upload.known_requests(root, [corr for _, corr, _, _ in staged])
        except LedgerBusy:
            outcomes.update((entry["relative"], dict(BUSY_FAILURE)) for entry, *_ in staged)
            return outcomes
        registering = []
        for entry, corr, existing, known in staged:
            saved = known[corr] if upload.settled(known, corr) else fresh[corr]
            try:
                ack = upload.acknowledgment(root, corr, existing, saved)
            except (IntakeError, OSError) as error:
                outcomes[entry["relative"]] = {"outcome": "failed", "stage": "receipt",
                                               "code": error.code if isinstance(error, IntakeError) else "os-error"}
                continue
            _acknowledged(entry, ack)
            failure = _received(entry, ack, saved[1]["stages"][0])
            if failure is None:
                registering.append(entry)
            else:
                outcomes[entry["relative"]] = failure
        outcomes.update(_extract_batch(root, registering, actor, heads, concurrency))
    return outcomes


def _stage_registration(root, entry, heads, arguments):
    """Stage (or find) one entry's registration request; return ``(correlation ID, stage ID)``."""
    base, version = _identity(entry)
    current = _staged(root, "register-" + entry["submission_id"] + version, "register",
                      "extract-register", lambda: arguments(base), base, heads)
    return current["correlation_id"], current["stages"][0]["id"]


def _prepare_batch(root, entries, heads, outcomes):
    """Prepare each entry's extraction in turn; return those ready to register (``outcomes`` gets the rest)."""
    registering = []
    for entry in entries:
        try:
            stage = _prepare(root, entry, heads)
            if stage["status"] != "succeeded":
                outcomes[entry["relative"]] = _failure(stage["id"], stage)
                continue
            registering.append(entry)
        except LedgerBusy:
            _conflict(entry)
            outcomes[entry["relative"]] = dict(BUSY_FAILURE)
        except IntakeError as error:
            if error.code not in CHAIN_REFUSALS:
                raise
            outcomes[entry["relative"]] = {"outcome": "failed", "stage": "prepare", "code": error.code}
    return registering


def _extract_batch(root, entries, actor, heads, concurrency=1):
    """``_extract`` for a chunk: each document prepared in turn, their registrations committed together.

    PDFs and images are converted first, up to ``concurrency`` at once and outside the ledger.
    """
    outcomes = {}
    cohort_runtime.convert_ahead(root, entries, heads, concurrency, _prepare_name)
    registering = _prepare_batch(root, entries, heads, outcomes)
    state = None

    def arguments(base):
        nonlocal state
        # One state read pins every registration staged now; each is rebased on its own.
        state = state or catalog.status(root)["state_token"]
        return {**base, "actor": actor, "expected_state": state}
    heads_of = []  # (entry, correlation ID, stage ID)
    for entry in registering:
        try:
            heads_of.append((entry, *_stage_registration(root, entry, heads, arguments)))
        except LedgerBusy:
            outcomes[entry["relative"]] = dict(BUSY_FAILURE)
        except IntakeError as error:
            if error.code not in CHAIN_REFUSALS:
                raise
            outcomes[entry["relative"]] = {"outcome": "failed", "stage": "register", "code": error.code}
    try:
        pending = [(corr, stage_id) for _, corr, stage_id in heads_of]
        summaries = runner.settle_batch(root, pending) if pending else {}
    except LedgerBusy:
        outcomes.update((entry["relative"], dict(BUSY_FAILURE)) for entry, *_ in heads_of)
        return outcomes
    for entry, corr, stage_id in heads_of:
        stage = next(s for s in summaries[corr]["stages"] if s["id"] == stage_id)
        _conflict(entry, runner.conflicts(stage))
        outcomes[entry["relative"]] = _settled_outcome(stage_id, stage)
    return outcomes


def _settled_batch(root, inbox, chunk, system, actor, heads, concurrency=1):
    """``_settled`` for a chunk: a busy ledger repeats the chunk (every step is idempotent), then fails
    only the entries it left busy, visibly and retryable, and the run continues."""
    todo, results = list(chunk), {}
    for _ in range(runner.BUSY_RETRIES):
        outcomes = _process_batch(root, inbox, todo, system, actor, heads, concurrency)
        results.update(outcomes)
        todo = [e for e in todo if outcomes[e["relative"]].get("code") == "ledger_busy"]
        if not todo:
            break
    return results


def _counts(entries, admitted):
    outcome = Counter({name: 0 for name in OUTCOMES})
    for entry in admitted:
        outcome["received"] += bool(entry.get("received"))
        outcome[entry["outcome"]] += 1
    retried = [e for e in admitted if e.get("conflicts")]
    return {"files": len(entries),
            "disposition": dict(Counter(e["disposition"] for e in entries)),
            "format": dict(Counter(e["format"] for e in admitted)),
            "outcome": dict(outcome),
            # Entries that lost a race with another writer and were retried; those that
            # then succeeded are not failures.
            "retried_after_conflict": {
                "entries": len(retried),
                "succeeded": sum(e["outcome"] != "failed" for e in retried),
                "attempts": sum(e["conflicts"] for e in retried)},
            "failed_by_code": dict(Counter(
                "%s/%s" % (e["stage"], e["code"]) for e in admitted if e.get("outcome") == "failed"))}


def ingest(root, inbox, *, system, actor, limit=None, batch_size=BATCH_SIZE, trust_stat=False,
           extract_concurrency=cohort_runtime.DEFAULT_CONCURRENCY,
           origin=None):
    """Classify the inbox, then submit and extract admitted files; return the report.

    Documents are processed in chunks of ``batch_size`` whose catalog registrations (the receipt, then
    the extraction registration) each commit as one revision; ``batch_size=1`` commits every
    registration on its own. Each document's outcome is its own whichever way it is processed.

    Every file is read and hashed, so a changed file is always found, unless ``trust_stat`` is set:
    then an extracted document whose file still has the (device, inode, size, mtime, ctime) its
    ``docs`` row recorded is reported from that row without being read, hashed or looked up in the
    ledger. ``ctime`` moves on every write, so an in-place edit that restores size and mtime changes
    it; a filesystem or copy tool that sets ctime back is outside what stat can show, which is why
    this is opt-in. Without the ``docs`` table (or a recorded stat) nothing is trusted.

    PDFs and images are read only when the instance's Docling runtime is provisioned. Each chunk's
    conversions run before its ledger stages, ``extract_concurrency`` at once (each in its own limited
    child process); with ``batch_size=1`` they run one at a time inside the prepare stage.

    An agent ``origin`` (``agent_origin.load``) selects the files of an agent-written tree and
    submits each admitted one with its ``agent_generated`` declaration (counted in ``agent_origin``).
    """
    contracts.require(type(batch_size) is int and batch_size >= 1, "invalid_batch_size")
    contracts.require(type(extract_concurrency) is int and extract_concurrency >= 1, "invalid_concurrency")
    trusted = {}
    if trust_stat:
        with store.locked(root) as c:
            trusted = docs.load(c, system)
    entries = classify(root, inbox, trusted, True, origin)
    declared = agent_origin.declare(inbox, origin, entries) if origin is not None else None
    admitted = [e for e in entries if e["disposition"] == "admit"]
    # Private stat fields never reach the report: take them off every entry now.
    stats = {e["relative"]: e.pop("_stat", None) for e in entries}
    fast = {e["relative"] for e in entries if e.pop("_fast", False)}
    todo = admitted if limit is None else admitted[:limit]
    for entry in admitted[len(todo):]:
        entry["outcome"] = "not-processed"
    for entry in todo:
        if entry["relative"] in fast:
            entry.update(_trusted_entry(entry, trusted[entry["relative"]], system))
    _process_all(root, inbox, [e for e in todo if e["relative"] not in fast], system, actor,
                 batch_size, stats, extract_concurrency)
    for entry in entries:
        entry.pop("_source", None)
    counts = _counts(entries, admitted)
    if declared is not None:
        counts["agent_origin"] = declared
    return {"counts": counts, "entries": entries}


def _process_all(root, inbox, todo, system, actor, batch_size, stats, concurrency=1):
    """Process ``todo`` in chunks of ``batch_size``, recording ``docs`` rows every ``FLUSH`` entries."""
    # One correction-chain index serves every entry, so following chains never rescans the ledger.
    heads = store.Heads()
    pending = []
    for chunk in (todo[i:i + batch_size] for i in range(0, len(todo), batch_size)):
        if batch_size == 1:
            results = {chunk[0]["relative"]: _settled(root, inbox, chunk[0], system, actor, heads)}
        else:
            results = _settled_batch(root, inbox, chunk, system, actor, heads, concurrency)
        for entry in chunk:
            entry.update(results[entry["relative"]])
            pending.append((entry, stats[entry["relative"]]))
        if len(pending) >= FLUSH:
            _record(root, system, pending, heads)
            pending = []
    _record(root, system, pending, heads)
