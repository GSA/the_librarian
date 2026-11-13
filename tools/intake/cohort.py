"""Read-only local cohort intake with explicit, counted dispositions.

Every file below a read-only inbox gets exactly one disposition. Admitted files
are submitted through manual upload intake as ``local-path`` copies (the inbox
is never written, moved or given a remote identity) and extracted through the
durable ledger. Extraction is not review: renditions stay deferred until a
standing-policy review qualifies them. Printed output is body-free counts.
"""
import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
from urllib.parse import quote

from tools import policy
from tools.review_events import event_reader
from tools.catalog import catalog_store as catalog
from tools.context_index import context_index as index, renditions
from tools.extraction import extract, mhtml
from . import adapters, cli, model_review, runner, standing, store, upload
from . import contracts
from .contracts import IntakeError, LedgerBusy

ASSET_SUFFIXES = {".js", ".css", ".ico", ".map", ".woff", ".woff2", ".ttf", ".svg"}
RUNTIME_FORMATS = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image"}
EXPANDED_DIRECTORY = "_extracted"
DEFAULT_INDEX = "tools/context_index/.local/index.sqlite3"
REASON = "Read-only local cohort intake; copy only, not extraction review or qualification."
OUTCOMES = ("received", "extracted", "failed", "changed-since-intake", "not-processed")


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


def _format(path, relative, raw, root):
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


def _disposition(root, inbox, path, relative):
    entry = {"relative": relative, "disposition": None, "format": None}
    name, suffix = path.name, path.suffix.lower()
    if path.is_symlink() or not path.is_file():
        return {**entry, "disposition": "unsafe-path"}
    size = path.stat(follow_symlinks=False).st_size
    entry["bytes"] = size
    if ":" in name:
        # Windows alternate-data-stream sidecars (Zone.Identifier, DLP tags).
        return {**entry, "disposition": "skipped-artifact"}
    if suffix in ASSET_SUFFIXES:
        return {**entry, "disposition": "skipped-asset"}
    if suffix == ".zip":
        expanded = path.parent / EXPANDED_DIRECTORY / path.stem
        return {**entry, "disposition": "archive-expanded" if expanded.is_dir() else "needs-reader"}
    if size == 0:
        return {**entry, "disposition": "empty"}
    if suffix in RUNTIME_FORMATS:
        fmt = RUNTIME_FORMATS[suffix]
        over = size > upload.FORMATS[fmt][1]
        return {**entry, "format": fmt, "disposition": "over-limit" if over else "needs-runtime"}
    if suffix not in extract.TEXT_SUFFIXES | {".odt"}:
        return {**entry, "disposition": "needs-reader"}
    limit = max(upload.FORMATS[f][1] for f in ("text", "gemini-txt", "mhtml", "odt"))
    if size > limit:
        return {**entry, "disposition": "over-limit"}
    try:
        raw = upload.read_file(path, limit)
    except IntakeError as error:
        return {**entry, "disposition": "unsafe-path", "code": error.code}
    fmt, refusal = _format(path, relative, raw, root)
    entry.update(format=fmt, sha256=hashlib.sha256(raw).hexdigest())
    if refusal:
        # MIME that cannot be read strictly is never admitted as raw encoded text.
        return {**entry, "disposition": "needs-reader", "code": refusal}
    if size > upload.FORMATS[fmt][1]:
        return {**entry, "disposition": "over-limit"}
    try:
        upload.check_type(raw, fmt, name)
    except IntakeError as error:
        return {**entry, "disposition": "unreadable", "code": error.code}
    return {**entry, "disposition": "admit"}


def classify(root, inbox):
    """Return one explicit disposition per inbox file; never writes the inbox."""
    root, inbox = Path(root).resolve(), Path(inbox).resolve()
    if root == inbox or inbox in root.parents:
        raise IntakeError("instance_inside_inbox")
    if not inbox.is_dir():
        raise IntakeError("inbox_unavailable")

    def unavailable(error):
        raise IntakeError("inbox_unavailable") from error
    entries = []
    for directory, names, files in os.walk(inbox, onerror=unavailable, followlinks=False):
        here = Path(directory)
        for name in [*files, *(n for n in names if (here / n).is_symlink())]:
            path = here / name
            relative = path.relative_to(inbox).as_posix()
            entries.append(_disposition(root, inbox, path, relative))
    return sorted(entries, key=lambda e: e["relative"])


def _request(root, correlation_id, stage_id, operation, arguments, identity=None, heads=None):
    """Stage one single-stage request unless it exists; return its chain head's stage summary.

    A request that was corrected is represented by the newest request of its validated
    ``correction_of`` chain, whatever it is named (``heads`` shares one chain index across a
    run). The head is settled and reported like any other request: succeeded, blocked, or still
    staged. It must repeat this request's operation and the ``identity`` arguments (receipt and
    format), or ``correction_mismatch`` refuses it. ``arguments`` may be a callable, evaluated
    only when the request must be staged.
    """
    current = _head(root, correlation_id, heads)
    if current is not None and current["correlation_id"] != correlation_id:
        saved = store.request(root, current["correlation_id"])["stages"]
        if (len(saved) != 1 or saved[0]["operation"] != operation
                or any(saved[0]["arguments"].get(k) != v for k, v in (identity or {}).items())):
            raise IntakeError("correction_mismatch")
    if current is None:
        current = store.stage(root, json.dumps({
            "version": 1, "correlation_id": correlation_id, "correction_of": None,
            "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 0},
            "stages": [{"id": stage_id, "kind": "extraction", "operation": operation,
                        "arguments": arguments() if callable(arguments) else arguments}]}).encode())
    stage = current["stages"][0]
    if stage["status"] in {"staged", "running", "retryable"}:
        stage = runner.settle(root, current["correlation_id"], stage["id"])["stages"][0]
    return stage


def _extract(root, entry, actor, heads=None):
    base = {"receipt": entry["receipt_id"], "format_name": entry["format"]}
    # The reader version is part of the request identity: a new MIME reader version extracts again.
    version = "-m%d" % mhtml.METHOD_VERSION if entry["format"] == "mhtml" else ""
    step = "prepare"
    try:
        stage = _request(root, "extract-" + entry["submission_id"] + version, "prepare",
                         "extract-prepare", base, base, heads)
        entry["conflicts"] = entry.get("conflicts", 0) + runner.conflicts(stage)
        if stage["status"] == "succeeded":
            step = "register"
            # Registration pins the catalog state when staged, immediately before it runs;
            # a repeated registration is idempotent by its saved request.
            # The catalog state is read only when a new registration is staged.
            stage = _request(root, "register-" + entry["submission_id"] + version, "register",
                             "extract-register", lambda: {
                                 **base, "actor": actor,
                                 "expected_state": catalog.status(root)["state_token"]}, base, heads)
            entry["conflicts"] += runner.conflicts(stage)
    except IntakeError as error:
        if error.code not in {"ambiguous_correction", "correction_mismatch"}:
            raise
        # A chain the store cannot follow to one valid head fails its entry, not the run.
        return {"outcome": "failed", "stage": step, "code": error.code}
    if stage["status"] != "succeeded":
        return _failure(stage["id"], stage)
    evidence = stage["evidence"]
    return {"outcome": "extracted", "rendition": evidence["rendition"],
            "rendition_sha256": evidence["rendition_sha256"]}


def _failure(stage_id, stage):
    """A failed entry with its stage, status, category, code and attempt count (no private text)."""
    last = stage["attempts"][-1] if stage["attempts"] else {}
    return {"outcome": "failed", "stage": stage_id, "status": stage["status"],
            "category": last.get("category"), "code": last.get("code") or stage["status"],
            "attempts": len(stage["attempts"])}


def _process(root, inbox, entry, system, actor, heads=None):
    identity = submission(entry["relative"], system, entry["format"])
    superseded = None
    prior = upload.ledger_request(root, upload.correlation(identity["submission_id"]))
    if prior is not None:
        orig = next((r for r in upload.submit_records(prior)
                     if r["kind"] == "artifact" and r["data"].get("role") == "original"), None)
        if orig is not None and orig["data"]["sha256"] != entry["sha256"]:
            superseded = identity["submission_id"]
            identity = submission(entry["relative"], system, entry["format"], entry["sha256"])
        elif prior.get("correction_of"):
            # An existing identity keeps the correction it was first submitted with.
            superseded = prior["correction_of"].removeprefix("upload-")
    elif entry["format"] == "mhtml":
        # An entry ingested before MIME recognition (as plain text) is corrected, not kept.
        earlier = submission(entry["relative"], system)["submission_id"]
        if upload.ledger_request(root, upload.correlation(earlier)) is not None:
            superseded = earlier
    entry.update(identity)
    try:
        ack = upload.submit(root, Path(inbox) / entry["relative"], upload.descriptor(
            submission_id=identity["submission_id"], format_name=entry["format"],
            correction_of=superseded,
            name=identity["name"], actor=actor, reason=REASON, origin_kind="local-path",
            origin_system=system, origin_locator=identity["locator"],
            expected_sha256=entry["sha256"]))
    except LedgerBusy:
        raise  # retried by _settled, never reported as a receipt refusal
    except IntakeError as error:
        outcome = "changed-since-intake" if error.code == "changed_input" else "failed"
        return {"outcome": outcome, "stage": "receipt", "code": error.code}
    entry.update(original=ack["original"]["path"], receipt_id=ack["records"]["receipt_id"])
    receipt = store.inspect(root, ack["correlation_id"])["stages"][0]
    entry["conflicts"] = entry.get("conflicts", 0) + runner.conflicts(receipt)
    if ack["receipt"]["state"] != "committed":
        return _failure("receipt", receipt)
    entry["received"] = True
    return _extract(root, entry, actor, heads)


def _settled(root, inbox, entry, system, actor, heads=None):
    """Process one entry, retrying a busy ledger within the entry's attempt budget.

    Every step is idempotent by request identity, so repeating the entry is safe. An
    entry that is still busy after the budget fails visibly as retryable ``ledger_busy``
    and the run continues; the next run resumes it.
    """
    for attempt in range(runner.BUSY_RETRIES):
        try:
            return _process(root, inbox, entry, system, actor, heads)
        except LedgerBusy:
            entry["conflicts"] = entry.get("conflicts", 0) + 1
    return {"outcome": "failed", "stage": "ledger", "status": "retryable", "category": "retryable",
            "code": "ledger_busy", "attempts": runner.BUSY_RETRIES}


def ingest(root, inbox, *, system, actor, limit=None):
    """Classify the inbox, then submit and extract admitted files; return the report."""
    entries = classify(root, inbox)
    admitted = [e for e in entries if e["disposition"] == "admit"]
    outcome = Counter({name: 0 for name in OUTCOMES})
    # One correction-chain index serves every entry, so following chains never rescans the ledger.
    heads = store.Heads()
    for number, entry in enumerate(admitted):
        if limit is not None and number >= limit:
            entry["outcome"] = "not-processed"
            outcome["not-processed"] += 1
            continue
        entry.update(_settled(root, inbox, entry, system, actor, heads))
        outcome["received"] += bool(entry.get("received"))
        outcome[entry["outcome"]] += 1
    retried = [e for e in admitted if e.get("conflicts")]
    counts = {"files": len(entries),
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
    return {"counts": counts, "entries": entries}


def _write_json(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    os.fchmod(descriptor, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")


def _write_private(path, value):
    _write_json(path, value, 0o600)


def _replace_json(path, value, mode=0o644):
    """Atomically replace a JSON file; the old file stays intact if anything fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=".cohort-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _instance_file(root, name):
    """An instance-relative path with no symlink component, checked before any read or write."""
    try:
        index.relative_name(name)
        path = root / name
        for part in path.relative_to(root).parents:
            if (root / part).is_symlink():
                raise IntakeError("unsafe_path")
        if path.is_symlink():
            raise IntakeError("unsafe_path")
    except index.IndexError_ as error:
        raise IntakeError("unsafe_path") from error
    return path


def _validate_inputs(root, policy_path, manifest, context, index_path, model):
    """Refuse bad paths and a missing maintained context before ingesting or writing anything."""
    _instance_file(root, policy_path)
    _instance_file(root, manifest)
    try:
        # The ledger requires ASCII relative paths; refuse the same ones before any mutation.
        for name in (policy_path, manifest, index_path):
            contracts.relative(name)
            name.encode("ascii")
        contracts.relative(index_path)
        index.validate_db_path(root, root / index_path)
        if (root / manifest).is_file():
            index.load_manifest(index.no_symlinks(root / manifest), root)
        if (root / policy_path).is_file():
            raw = standing._read(root, policy_path, standing.LIMIT)[1]
            loaded = standing.load_policy(root, policy_path, hashlib.sha256(raw).hexdigest())
            if loaded["reviewer"] != f"{model_review.PROVIDER}:{model}":
                raise IntakeError("policy_reviewer_mismatch")
    except (index.IndexError_, standing.Refusal, IntakeError, OSError, UnicodeError) as error:
        raise IntakeError(getattr(error, "code", None) if isinstance(error, IntakeError)
                          else "preflight_invalid") from error
    locators = context
    policy_file = root / policy_path
    if policy_file.is_file():
        try:
            locators = json.loads(policy_file.read_text(encoding="utf-8"))["selection"]["context"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise IntakeError("context_invalid") from error
    try:
        for locator in locators:
            canonical = index.canonical_locator(locator, root)
            document, _, fragment = canonical.partition("#")
            source = index.source_path(root, document)
            _, aliases, _, _ = index.parse_document(
                document, source.read_text(encoding="utf-8"))
            if fragment and fragment not in aliases:
                raise IntakeError("context_invalid")
    except (index.IndexError_, OSError, UnicodeError) as error:
        raise IntakeError("context_invalid") from error


def _ensure_standing_inputs(root, policy_path, manifest, model, context):
    policy_file = root / policy_path
    if not policy_file.is_file():
        _replace_json(policy_file, {
            "schema": standing.POLICY_SCHEMA,
            "policy_id": "cohort-standing",
            "version": 1,
            "status": "active",
            "reviewer": f"{model_review.PROVIDER}:{model}",
            "scope": {
                "receipts": [f"D{i}" for i in range(1, 2501)],
                "audiences": ["source"],
            },
            "budget": {
                "max_pages": renditions.DOCLING_MAX_PAGES,
                "max_records": renditions.DOCLING_MAX_RECORDS,
                "max_findings_bytes": standing.LIMIT,
            },
            "selection": {"context": list(context)},
        }, 0o600)
    policy_sha = hashlib.sha256(policy_file.read_bytes()).hexdigest()
    standing_policy = standing.load_policy(root, policy_path, policy_sha)
    manifest_file = root / manifest
    # Another cohort's run may be selecting into this manifest: read and write under the ledger lock.
    with store.locked(root):
        raw = (json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.is_file() else {
            "schema_version": 1,
            "audience": policy.for_root(root)[0]["audience"],
            "documents": [],
            "context_rules": {},
            "renditions": [],
        })
        docs = {d["path"] for d in raw.setdefault("documents", [])}
        changed = not manifest_file.is_file()
        for locator in standing_policy["selection"]["context"]:
            doc = locator.split("#", 1)[0]
            if doc not in docs:
                raw["documents"].append({"path": doc, "role": "context", "context": []})
                docs.add(doc)
                changed = True
        if changed:
            _replace_json(manifest_file, raw)
    return standing_policy, policy_sha


def _standing_outcome(stage):
    code = stage["attempts"][-1]["code"] if stage["attempts"] else stage["status"]
    for name in ("pending-review", "rejected", "deferred", "exception"):
        if code.startswith("standing-" + name + "-"):
            return name, code
    return "pending-review", code


def _stage_request(root, corr, stages, correction_of=None):
    store.stage(root, json.dumps({
        "version": 1, "correlation_id": corr, "correction_of": correction_of,
        "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
        "retry": {"max_attempts": 3, "backoff_seconds": 0},
        "stages": stages,
    }).encode())


def _head(root, corr, heads=None):
    try:
        return (heads.head if heads is not None else store.head)(root, corr)
    except IntakeError as error:
        if error.code != "unknown_request":
            raise
        return None


def _root_of(root, corr):
    """The first request of ``corr``'s correction chain (its own name when it corrects nothing)."""
    seen = {corr}
    while (parent := store.request(root, corr)["correction_of"]) is not None and parent not in seen:
        corr = parent
        seen.add(corr)
    return corr


def _identity_blocked(root):
    """Chain heads that are blocked ``extract-register`` stages refused as a changed identity."""
    found = []
    with store.locked(root) as c:
        rows = c.execute("SELECT s.correlation_id, s.ordinal, r.raw FROM stages s "
                         "JOIN requests r USING(correlation_id) WHERE s.status='blocked' "
                         "ORDER BY s.correlation_id").fetchall()
        for row in rows:
            stage = contracts.decode_request(row["raw"])["stages"][row["ordinal"]]
            if stage["operation"] != "extract-register":
                continue
            if c.execute("SELECT 1 FROM requests WHERE correction_of=?", (row["correlation_id"],)).fetchone():
                continue  # not the head: a correction follows it
            last = c.execute("SELECT code FROM attempts WHERE correlation_id=? AND stage_id=? "
                             "ORDER BY cycle DESC, ordinal DESC LIMIT 1",
                             (row["correlation_id"], stage["id"])).fetchone()
            if last is not None and last[0] == "changed-extraction-identity":
                found.append((row["correlation_id"], stage))
    return found


def recover_identity(root, *, apply=False):
    """Report, and with ``apply`` correct, registrations blocked as ``changed-extraction-identity``.

    The extractor identity (its code and policy) is pinned when a stage first prepares, and a
    replay keeps that pin, so the stage can never register after the toolkit changed. The supported
    repair is the one the store already validates: a correction request with a fresh pin. For each
    chain whose head is such a blocked ``extract-register``, ``apply`` stages a correction of the
    paired ``extract-prepare`` chain (unless its head already prepared under today's extractor)
    and then of the registration (a fresh catalog pin), and settles both. Both corrections repeat
    the original operation and arguments, so ``prepare == intent``, the catalog pin and the
    source-hash checks all apply to them unchanged; a source or extractor that moves again blocks
    again. ``ingest`` finds the result by following ``correction_of``, never by name. The pairing
    uses the ingest's own ``extract-``/``register-`` names only to find the prepare chain, and
    checks the receipt and format against it. A chain not recoverable is counted in ``skipped``.
    """
    root = Path(root).resolve()
    heads = store.Heads()
    found, skipped, work = [], Counter(), []
    for corr, stage in _identity_blocked(root):
        base = _root_of(root, corr)
        args = stage["arguments"]
        prefix = "register-"
        prepare = _head(root, "extract-" + base[len(prefix):], heads) if base.startswith(prefix) else None
        saved = store.request(root, prepare["correlation_id"])["stages"] if prepare else []
        if (len(saved) != 1 or saved[0]["operation"] != "extract-prepare"
                or any(saved[0]["arguments"].get(k) != args[k] for k in ("receipt", "format_name"))):
            skipped["prepare-mismatch"] += 1
            continue
        reuse = None
        if prepare["stages"][0]["status"] == "succeeded":
            try:
                fresh = adapters.prepare(store.safe(root), saved[0])
            except (adapters.StageFailure, OSError):
                # A removed, unreadable or no longer valid source: counted, not fatal to the rest.
                skipped["source-unavailable"] += 1
                continue
            reuse = prepare["stages"][0]["evidence"]["run_id"] == fresh["run_id"]
        elif prepare["stages"][0]["status"] not in {"staged", "running", "retryable"}:
            skipped["prepare-blocked"] += 1
            continue
        found.append(corr)
        work.append((corr, base, prepare, reuse))
    counts = {"extract-register/changed-extraction-identity": len(found)} if found else {}
    report = {"apply": apply, "candidates": len(found), "by_operation_code": counts,
              "skipped": dict(skipped)}
    if not apply:
        return report
    results = Counter()
    for corr, base, prepare, reuse in work:
        try:
            if reuse is not True:
                if reuse is False:
                    prepare_id = _correct(root, prepare)
                else:
                    prepare_id = prepare["correlation_id"]
                done = runner.settle(root, prepare_id, prepare["stages"][0]["id"])["stages"][0]
                if done["status"] != "succeeded":
                    results["prepare-" + done["status"]] += 1
                    continue
            fixed = _correct(root, store.inspect(root, corr), lambda a: {
                **a, "expected_state": catalog.status(root)["state_token"]})
            done = runner.settle(root, fixed, store.inspect(root, fixed)["stages"][0]["id"])["stages"][0]
            results[done["status"]] += 1
        except IntakeError as error:
            results[error.code] += 1
    report["results"] = dict(results)
    return report


def _correct(root, head, rewrite=lambda arguments: arguments):
    """Stage, and return the ID of, a correction of ``head`` repeating its single stage."""
    request = store.request(root, head["correlation_id"])
    base = _root_of(root, head["correlation_id"])
    number = 1
    while _exists(root, f"{base}-c{number}"):
        number += 1
    corrected = f"{base}-c{number}"
    (stage,) = request["stages"]
    _stage_request(root, corrected, [{**stage, "arguments": rewrite(stage["arguments"])}],
                   correction_of=head["correlation_id"])
    return corrected


def _exists(root, corr):
    try:
        store.request(root, corr)
    except IntakeError as error:
        if error.code != "unknown_request":
            raise
        return False
    return True


def _inputs_changed(current):
    review = current["stages"][0]
    return (review["status"] == "blocked" and bool(review["attempts"])
            and review["attempts"][-1]["code"] == "changed-standing-inputs")


def _stage_correction(root, corr, active):
    """Restage a standing review whose pinned intent no longer matches its inputs as a
    correction of it: same review arguments and event, fresh request identities."""
    number = 1
    while _exists(root, f"{corr}-c{number}"):
        number += 1
    corrected = f"{corr}-c{number}"
    stages = store.request(root, active)["stages"]
    _stage_request(root, corrected, [
        {**s, "arguments": {**s["arguments"], "request_id": f"req-{corrected}-{s['id']}"}}
        for s in stages], correction_of=active)
    return corrected


def _inspect(root, corr):
    try:
        return store.inspect(root, corr)
    except IntakeError as error:
        if error.code != "unknown_request":
            raise
        return None


def _manifest_renditions(root, manifest):
    return json.loads((root / manifest).read_text(encoding="utf-8")).get("renditions", [])


def _set_manifest_renditions(root, manifest, renditions_):
    raw = json.loads((root / manifest).read_text(encoding="utf-8"))
    if raw.get("renditions", []) != renditions_:
        raw["renditions"] = renditions_
        _replace_json(root / manifest, raw)


# Records of one receipt read to find its submission sidecar (a receipt holds a few per extraction).
ATTRIBUTION_RECORDS = 1000


def _submission_system(root, receipt):
    """The cohort label a receipt was submitted under, from validated catalog evidence.

    ``receipt`` is one receipt's scoped catalog records. The label is the ``origin.system`` of
    the ``local-path`` submission sidecar whose SHA-256 the catalog pins on the receipt's
    ``preserved-metadata`` artifact, so a changed or swapped sidecar attributes nothing.
    ``None`` when any link is missing or inconsistent.
    """
    by_id = {record["id"]: record for record in receipt["records"]}
    try:
        (head,) = (r for r in receipt["records"] if r["kind"] == "receipt")
        original = by_id[head["data"]["artifact_id"]]
        prefix, _, sid = original["id"].partition("A-upload-")
        sid = sid.removesuffix("-original")
        meta = by_id["A-upload-%s-submission" % sid]
        if (prefix or not sid or original["data"]["role"] != "original"
                or meta["data"]["role"] != "preserved-metadata"
                or meta["data"]["parent_id"] != original["id"]
                or meta["data"]["receipt_id"] != head["id"]
                or original["data"]["receipt_id"] != head["id"]):
            return None
        contracts.relative(meta["data"]["path"])
        raw = upload.read_file(Path(store.safe(root)) / meta["data"]["path"], upload.MIB)
        sidecar = contracts.decode(raw)
        if (contracts.digest(raw) != meta["data"]["sha256"] or sidecar["submission_id"] != sid
                or sidecar["original"]["sha256"] != original["data"]["sha256"]):
            return None
        origin = sidecar["origin"]
        return origin["system"] if origin["kind"] == "local-path" else None
    except (KeyError, TypeError, ValueError, OSError, IntakeError):
        return None


ABSENT = "\0absent"


def _attribute(root, cache, entries):
    """Record in ``cache`` the cohort label of each manifest entry's receipt not yet attributed.

    The value is the label, ``None`` when the receipt exists but no cohort is proven for it, or
    ``ABSENT`` when the catalog holds no such receipt. A catalog that cannot be read raises, before
    the manifest is touched: nothing is dropped on a guess.
    """
    wanted = sorted({e["receipt_id"] for e in entries} - cache.keys())
    if not wanted:
        return
    try:
        found = catalog.read_records_by_receipt(root, wanted, limit=ATTRIBUTION_RECORDS)
    except catalog.CatalogError as error:
        raise IntakeError("selection_attribution_unavailable") from error
    for receipt, scoped in found.items():
        if not scoped["records"]:
            cache[receipt] = ABSENT
        else:
            # A receipt too large to show its sidecar record is unproven, so it is kept.
            cache[receipt] = (None if scoped["next_after"] is not None
                              else _submission_system(root, scoped))


def _merge_cohort(listed, own, mine):
    """Replace this cohort's slots in ``listed`` with ``mine``; other entries stay where they are.

    ``own`` flags the entries of ``listed`` that belong to the cohort. Slots are refilled in
    order and any surplus is appended, so a manifest holding only this cohort becomes exactly
    ``mine``, and an unchanged cohort leaves the manifest unchanged.
    """
    fill, merged = iter(mine), []
    for item, mine_ in zip(listed, own):
        if not mine_:
            merged.append(item)
        elif (replacement := next(fill, None)) is not None:
            merged.append(replacement)
    merged.extend(fill)
    return merged


def _reconcile(root, manifest, system, current, cache, compose):
    """Rewrite only this cohort's manifest entries, atomically, under the ledger writer lock.

    An entry is the cohort's when its rendition is one the run extracted or its receipt was
    submitted under ``system``. Every other entry (another cohort's, an operator's, or one whose
    submission cannot be proven) is kept as it is, in its place. The exception is an orphan: an
    entry whose receipt is not in the catalog or whose retained rendition file is gone belongs to
    no cohort and nothing can publish it, so it is dropped, as before. ``compose(own, room)``
    returns the cohort's new entries, at most ``room`` of them. The read-modify-write holds the
    lock that ``standing-select`` holds when it adds an entry, so a concurrent run for another
    cohort cannot lose either write. Returns the counts of entries left alone and dropped.
    """
    _attribute(root, cache, [r for r in _manifest_renditions(root, manifest)
                             if r["path"] not in current])
    with store.locked(root):
        listed = _manifest_renditions(root, manifest)
        # Entries another run added since the read above.
        _attribute(root, cache, [r for r in listed if r["path"] not in current])

        def orphan(r):
            return r["path"] not in current and (
                cache[r["receipt_id"]] == ABSENT or not os.path.isfile(root / r["path"]))
        own = [r["path"] in current or orphan(r) or cache[r["receipt_id"]] == system
               for r in listed]
        foreign = [r for r, mine in zip(listed, own) if not mine]
        merged = _merge_cohort(listed, own, compose(
            [r for r, mine in zip(listed, own) if mine], renditions.MAX_SELECTIONS - len(foreign)))
        _set_manifest_renditions(root, manifest, merged)
    return {"selections": len(foreign),
            "unattributed": sum(cache[r["receipt_id"]] is None for r in foreign),
            "orphaned": sum(orphan(r) for r in listed)}


def _selection_room(root, manifest, rendition):
    """True when the manifest already selects ``rendition`` or has room for one more selection."""
    selected = _manifest_renditions(root, manifest)
    return len(selected) < renditions.MAX_SELECTIONS or any(r["path"] == rendition for r in selected)


def _standing_token(entry, policy_sha):
    return "%s-%s-%s" % (entry["submission_id"], entry["rendition_sha256"][:12], policy_sha[:8])


def _live_events(snapshot):
    """Event IDs in ``snapshot`` that no later event supersedes."""
    return {e["event_id"] for e in snapshot.events if e["event_id"] not in snapshot.successors}


def _review_and_select(root, entry, standing_policy, policy_path, policy_sha, manifest,
                       model, transport, usage, snapshot=None, prefetched=None,
                       heads=None, live=None):
    """Review and select one extracted rendition; identity is bound to the exact rendition.

    ``prefetched`` optionally returns the model review already started for this entry
    on a worker thread; every ledger step still runs here, on the calling thread.
    ``heads`` shares one correction-chain index across the run and ``live`` the live event
    IDs of ``snapshot``, so a settled entry costs neither a ledger scan nor an event scan.
    """
    token = _standing_token(entry, policy_sha)
    corr, event_id = "standing-" + token, "rev-" + token
    reviewed, failure = None, None
    if prefetched is not None:
        # Wait for the prefetched review before the ledger lookup, so a request another
        # writer staged while the model call ran is found and staging follows at once.
        try:
            reviewed = prefetched()
        except Exception as error:
            failure = error
    current = _head(root, corr, heads)
    if current is None:
        if failure is not None:
            raise failure
        if reviewed is None:
            reviewed = model_review.review(
                root, entry["rendition"], standing_policy, model, transport)
        usage.update(reviewed["usage"])
        if reviewed["status"] != "reviewed":
            entry.update(qualification="pending-review", review_status=reviewed["status"])
            return
        review_args = {
            "policy_path": policy_path, "policy_sha256": policy_sha,
            "rendition": entry["rendition"], "rendition_sha256": entry["rendition_sha256"],
            "findings_path": reviewed["findings_path"],
            "findings_sha256": reviewed["findings_sha256"], "event_id": event_id,
        }
        _stage_request(root, corr, [
            {"id": "review", "kind": "review", "operation": "standing-review",
             "arguments": {**review_args, "request_id": "req-" + corr + "-review"}},
            {"id": "select", "kind": "publication", "operation": "standing-select",
             "arguments": {**review_args, "request_id": "req-" + corr + "-select",
                           "manifest": manifest}}])
        current = store.inspect(root, corr)
    elif reviewed is not None:
        # Another writer staged this review meanwhile; keep only the tokens actually spent.
        usage.update(reviewed["usage"])
    settled = all(st["status"] == "succeeded" for st in current["stages"])
    if settled and snapshot is not None:
        if live is None:
            live = _live_events(snapshot)
        # An event absent from the run-start snapshot may come from another writer during
        # this run; only a fresh read can show that it was superseded.
        if event_id not in live and event_id not in _live_events(event_reader.read_snapshot(root)):
            # An operator superseded this review; never resurrect its selection.
            entry.update(qualification="pending-review", review_code="review-superseded",
                         superseded_event=event_id)
            return
    active = current["correlation_id"]
    if current["stages"][0]["status"] in {"staged", "running", "retryable"}:
        current = runner.run(root, active, "review")
    if _inputs_changed(current):
        # The saved review's pinned intent is stale (e.g. an earlier-version review it
        # conflicted with is now supersedable); re-pin it under a correction request.
        active = _stage_correction(root, corr, active)
        current = runner.run(root, active, "review")
    select = current["stages"][1]
    if (select["status"] == "blocked" and select["attempts"]
            and select["attempts"][-1]["code"] == "selection-capacity"
            and _selection_room(root, manifest, entry["rendition"])):
        # The manifest was full when this was selected and has room now: resume it.
        current = runner.replay(root, active, "select")
        select = current["stages"][1]
    if current["stages"][0]["status"] == "succeeded" and select["status"] in {
            "staged", "running", "retryable"}:
        if not _selection_room(root, manifest, entry["rendition"]):
            # Shared with other cohorts, the manifest is full: leave the saved review's select
            # stage unrun so a later qualify selects it once room appears, without a model call.
            entry.update(qualification="pending-review", review_code="selection-capacity")
            return
        current = runner.run(root, active, "select")
    if all(s["status"] == "succeeded" for s in current["stages"]):
        selection = current["stages"][1]["evidence"]["selection"]
        if (selection["path"] != entry["rendition"]
                or selection["sha256"] != entry["rendition_sha256"]):
            entry.update(qualification="exception", review_code="selection-evidence-mismatch")
            return
        entry.update(qualification="qualified", review_event_id=event_id, selection=selection)
    else:
        failed = next(s for s in current["stages"] if s["status"] != "succeeded")
        outcome_name, code = _standing_outcome(failed)
        entry.update(qualification=outcome_name, review_code=code)


def _review_all(root, extracted, standing_policy, policy_path, policy_sha, manifest,
                model, transport, usage, snapshot, concurrency):
    """Review and select each entry in input order, with up to ``concurrency`` model calls in flight.

    Only model_review.review runs on worker threads, and only for entries without a
    standing request; staging, runs, selection and usage stay on this thread in input
    order, so the outcome does not depend on completion order.
    """
    def review(entry):
        return model_review.review(root, entry["rendition"], standing_policy, model, transport)

    # One correction-chain index and one live-event set serve every entry of the run.
    shared = {"heads": store.Heads(), "live": _live_events(snapshot)}
    if concurrency == 1:
        for entry in extracted:
            _review_and_select(root, entry, standing_policy, policy_path, policy_sha, manifest,
                               model, transport, usage, snapshot, **shared)
        return
    # Requests saved before this run need no model call; one read covers the run.
    # A request another writer stages later is still found by the fresh per-entry
    # read in _review_and_select, which then discards the unused review.
    existing = store.correlation_ids(root)
    window = deque()
    upcoming = iter(extracted)
    with ThreadPoolExecutor(max_workers=concurrency,
                            thread_name_prefix="cohort-review") as pool:
        try:
            while True:
                # At most `concurrency` calls run; a bounded lookahead keeps workers busy
                # while this thread finishes the ledger work for the oldest entry.
                while len(window) < 2 * concurrency:
                    entry = next(upcoming, None)
                    if entry is None:
                        break
                    corr = "standing-" + _standing_token(entry, policy_sha)
                    window.append((entry, None if corr in existing
                                   else pool.submit(review, entry)))
                if not window:
                    return
                entry, future = window.popleft()
                _review_and_select(root, entry, standing_policy, policy_path, policy_sha,
                                   manifest, model, transport, usage, snapshot,
                                   future.result if future is not None else None, **shared)
        except BaseException:
            for _, future in window:
                if future is not None:
                    future.cancel()
            raise


def _publish(root, policy_path, policy_sha, manifest, index_path):
    """Return the publish stage, staging a fresh request when saved success is stale."""
    manifest_sha = hashlib.sha256((root / manifest).read_bytes()).hexdigest()[:16]
    destination = hashlib.sha256(str(index_path).encode("utf-8")).hexdigest()[:8]
    base = f"publish-{policy_sha[:12]}-{manifest_sha}-{destination}"
    previous = None
    for number in range(100):
        corr = base if number == 0 else f"{base}-r{number}"
        current = _inspect(root, corr)
        if current is None:
            store.stage(root, json.dumps({
                "version": 1, "correlation_id": corr, "correction_of": previous,
                "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
                "retry": {"max_attempts": 3, "backoff_seconds": 0},
                "stages": [{"id": "publish", "kind": "publication",
                            "operation": "standing-publish",
                            "arguments": {"policy_path": policy_path, "policy_sha256": policy_sha,
                                          "manifest": manifest, "db": str(index_path)}}],
            }).encode())
            current = store.inspect(root, corr)
        stage = current["stages"][0]
        if stage["status"] in {"staged", "running", "retryable"}:
            stage = runner.run(root, corr, "publish")["stages"][0]
            return stage, cli.inspection(root, corr)["stages"][0]
        if stage["status"] != "succeeded":
            return stage, stage
        inspected = cli.inspection(root, corr)["stages"][0]
        if inspected.get("availability", {}).get("available"):
            return stage, inspected
        previous = corr
    raise IntakeError("publication_revisions_exhausted")


def qualify(root, inbox, *, system, actor, policy_path, manifest, model, transport,
            context=None, index_path=DEFAULT_INDEX, concurrency=1):
    """Ingest, model-review, select and publish the cohort; return body-free counts and usage."""
    if type(concurrency) is not int or concurrency < 1:
        raise IntakeError("invalid_concurrency")
    root = Path(root).resolve()
    if not context:
        # A maintained context document must be named; guessing one would not satisfy the policy.
        raise IntakeError("context_required")
    _validate_inputs(root, policy_path, manifest, context, index_path, model)
    report = ingest(root, inbox, system=system, actor=actor)
    standing_policy, policy_sha = _ensure_standing_inputs(
        root, policy_path, manifest, model, context)
    extracted = [e for e in report["entries"] if e.get("outcome") == "extracted"]
    # Only this cohort's selections are pruned and reconciled: those of renditions it extracted
    # and those whose receipt was submitted under ``system``. Every other entry is kept as it is.
    # Dropping the cohort's selections for renditions it no longer has comes before selecting
    # replacements, so replacing a rendition never needs spare manifest capacity.
    current = {e["rendition"] for e in extracted}
    cohorts = {}
    pruned = _reconcile(root, manifest, system, current, cohorts,
                        lambda own, room: [r for r in own if r["path"] in current])
    usage = Counter({"prompt_tokens": 0, "completion_tokens": 0})
    snapshot = event_reader.read_snapshot(root)
    retries = getattr(transport, "retries", None)
    retried = Counter(retries) if retries is not None else None
    _review_all(root, extracted, standing_policy, policy_path, policy_sha, manifest,
                model, transport, usage, snapshot, concurrency)
    qualified = [e for e in extracted if e.get("qualification") == "qualified"]
    # Reconcile the cohort's manifest entries to exactly its qualified renditions from validated
    # review evidence, keeping an operator's explicit selection whose saved review was superseded.
    stale = {e["superseded_event"] for e in extracted if "superseded_event" in e}
    kept = {e["rendition"] for e in extracted if e.get("review_code") == "review-superseded"}

    def reconciled(own, room):
        own = [r for r in own if not stale & set(r.get("review_event_ids") or [])]
        chosen = [r for r in own if r["path"] in kept] + [e["selection"] for e in qualified]
        # A selection restored from saved evidence cannot exceed what the manifest can hold
        # next to other cohorts' entries; the surplus stays visibly pending, never invalid.
        surplus = {r["path"] for r in chosen[room:]}
        for entry in qualified:
            if entry["rendition"] in surplus:
                entry.update(qualification="pending-review", review_code="selection-capacity")
        return chosen[:room]
    others = _reconcile(root, manifest, system, current, cohorts, reconciled)
    pub, inspected = _publish(root, policy_path, policy_sha, manifest, index_path)
    report["counts"]["qualification"] = dict(
        Counter(e["qualification"] for e in report["entries"] if "qualification" in e))
    others["orphaned"] = pruned["orphaned"]
    if others["selections"] or others["orphaned"]:
        # ``selections`` belong to other cohorts or to none provable (``unattributed``) and were
        # left as they were; ``orphaned`` named a receipt the catalog does not hold and were dropped.
        report["counts"]["other_cohorts"] = others
    report["counts"]["published"] = int(
        pub["status"] == "succeeded"
        and bool(inspected.get("availability", {}).get("available")))
    report["usage"] = {"prompt_tokens": usage["prompt_tokens"],
                       "completion_tokens": usage["completion_tokens"]}
    if retries is not None:
        # Body-free per-code retry counts made by the transport during this run.
        report["retries"] = {code: count for code, count in sorted(
            (Counter(retries) - retried).items())}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser(
        "recover-identity", allow_abbrev=False,
        help="Report (or with --apply correct) registrations blocked as changed-extraction-identity")
    recover.add_argument("--root", required=True, type=Path)
    recover.add_argument("--apply", action="store_true")
    for name in ("classify", "ingest", "qualify"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--inbox", required=True, type=Path)
        command.add_argument("--report", type=Path,
                             help="private (0600) per-file report with paths and hashes")
        if name in {"ingest", "qualify"}:
            command.add_argument("--system", required=True)
            command.add_argument("--actor", required=True)
        if name == "ingest":
            command.add_argument("--limit", type=int)
        if name == "qualify":
            command.add_argument("--policy", required=True, dest="policy_path")
            command.add_argument("--manifest", required=True)
            command.add_argument("--models-config", required=True, type=Path)
            command.add_argument("--model", required=True)
            command.add_argument("--context", action="append", dest="context")
            command.add_argument("--index", default=DEFAULT_INDEX, dest="index_path")
            command.add_argument("--concurrency", type=int, default=6,
                                 help="model review calls in flight (default 6; 1 is serial)")
    args = parser.parse_args(argv)
    try:
        if args.command == "recover-identity":
            print(json.dumps(recover_identity(args.root, apply=args.apply), sort_keys=True))
            return 0
        if args.command == "qualify":
            report = qualify(
                args.root, args.inbox, system=args.system, actor=args.actor,
                policy_path=args.policy_path, manifest=args.manifest, model=args.model,
                transport=model_review.usai_transport(args.models_config),
                context=args.context, index_path=args.index_path,
                concurrency=args.concurrency)
        elif args.command == "ingest":
            report = ingest(args.root, args.inbox, system=args.system, actor=args.actor,
                            limit=args.limit)
        else:
            entries = classify(args.root, args.inbox)
            report = {"counts": {"files": len(entries),
                                 "disposition": dict(Counter(e["disposition"] for e in entries))},
                      "entries": entries}
    except IntakeError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    if args.report:
        _write_private(args.report, report)
    summary = {"counts": report["counts"]}
    for key in ("usage", "retries"):
        if key in report:
            summary[key] = report[key]
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
