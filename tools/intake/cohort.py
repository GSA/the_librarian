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
from . import cli, model_review, runner, standing, store, upload
from . import contracts
from .contracts import IntakeError

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


def _request(root, correlation_id, stage_id, operation, arguments):
    """Stage one single-stage request unless it already exists; return its stage summary.

    ``arguments`` may be a callable, evaluated only when the request must be staged.
    """
    current = _inspect(root, correlation_id)
    if current is None:
        current = store.stage(root, json.dumps({
            "version": 1, "correlation_id": correlation_id, "correction_of": None,
            "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
            "retry": {"max_attempts": 3, "backoff_seconds": 0},
            "stages": [{"id": stage_id, "kind": "extraction", "operation": operation,
                        "arguments": arguments() if callable(arguments) else arguments}]}).encode())
    stage = current["stages"][0]
    if stage["status"] in {"staged", "running", "retryable"}:
        stage = runner.run(root, correlation_id, stage_id)["stages"][0]
    return stage


def _extract(root, entry, actor):
    base = {"receipt": entry["receipt_id"], "format_name": entry["format"]}
    # The reader version is part of the request identity: a new MIME reader version extracts again.
    version = "-m%d" % mhtml.METHOD_VERSION if entry["format"] == "mhtml" else ""
    stage = _request(root, "extract-" + entry["submission_id"] + version, "prepare", "extract-prepare", base)
    if stage["status"] == "succeeded":
        # Registration pins the catalog state when staged, immediately before it runs;
        # a repeated registration is idempotent by its saved request.
        # The catalog state is read only when a new registration is staged.
        stage = _request(root, "register-" + entry["submission_id"] + version, "register",
                         "extract-register", lambda: {
                             **base, "actor": actor,
                             "expected_state": catalog.status(root)["state_token"]})
    if stage["status"] != "succeeded":
        code = stage["attempts"][-1]["code"] if stage["attempts"] else stage["status"]
        return {"outcome": "failed", "stage": stage["id"], "code": code}
    evidence = stage["evidence"]
    return {"outcome": "extracted", "rendition": evidence["rendition"],
            "rendition_sha256": evidence["rendition_sha256"]}


def _process(root, inbox, entry, system, actor):
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
    except IntakeError as error:
        outcome = "changed-since-intake" if error.code == "changed_input" else "failed"
        return {"outcome": outcome, "stage": "receipt", "code": error.code}
    entry.update(original=ack["original"]["path"], receipt_id=ack["records"]["receipt_id"])
    if ack["receipt"]["state"] != "committed":
        return {"outcome": "failed", "stage": "receipt",
                "code": ack["receipt"].get("code", ack["receipt"]["state"])}
    entry["received"] = True
    return _extract(root, entry, actor)


def ingest(root, inbox, *, system, actor, limit=None):
    """Classify the inbox, then submit and extract admitted files; return the report."""
    entries = classify(root, inbox)
    admitted = [e for e in entries if e["disposition"] == "admit"]
    outcome = Counter({name: 0 for name in OUTCOMES})
    for number, entry in enumerate(admitted):
        if limit is not None and number >= limit:
            entry["outcome"] = "not-processed"
            outcome["not-processed"] += 1
            continue
        entry.update(_process(root, inbox, entry, system, actor))
        outcome["received"] += bool(entry.get("received"))
        outcome[entry["outcome"]] += 1
    counts = {"files": len(entries),
              "disposition": dict(Counter(e["disposition"] for e in entries)),
              "format": dict(Counter(e["format"] for e in admitted)),
              "outcome": dict(outcome)}
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


def _stage_request(root, corr, stages):
    store.stage(root, json.dumps({
        "version": 1, "correlation_id": corr, "correction_of": None,
        "policy": {"version": 1, "sha256": policy.for_root(root)[1]},
        "retry": {"max_attempts": 3, "backoff_seconds": 0},
        "stages": stages,
    }).encode())


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


def _standing_token(entry, policy_sha):
    return "%s-%s-%s" % (entry["submission_id"], entry["rendition_sha256"][:12], policy_sha[:8])


def _review_and_select(root, entry, standing_policy, policy_path, policy_sha, manifest,
                       model, transport, usage, snapshot=None, prefetched=None):
    """Review and select one extracted rendition; identity is bound to the exact rendition.

    ``prefetched`` optionally returns the model review already started for this entry
    on a worker thread; every ledger step still runs here, on the calling thread.
    """
    token = _standing_token(entry, policy_sha)
    corr, event_id = "standing-" + token, "rev-" + token
    current = _inspect(root, corr)
    if current is None:
        reviewed = (prefetched() if prefetched is not None else model_review.review(
            root, entry["rendition"], standing_policy, model, transport))
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
    elif prefetched is not None:
        _discard(prefetched, usage)
    settled = all(st["status"] == "succeeded" for st in current["stages"])
    if settled and snapshot is not None:
        live = {e["event_id"] for e in snapshot.events if e["event_id"] not in snapshot.successors}
        if event_id not in live:
            # An operator superseded this review; never resurrect its selection.
            entry.update(qualification="pending-review", review_code="review-superseded",
                         superseded_event=event_id)
            return
    if current["stages"][0]["status"] in {"staged", "running", "retryable"}:
        current = runner.run(root, corr, "review")
    if (current["stages"][0]["status"] == "succeeded"
            and current["stages"][1]["status"] in {"staged", "running", "retryable"}):
        current = runner.run(root, corr, "select")
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


def _discard(prefetched, usage):
    """Another writer staged this review meanwhile; keep only the tokens actually spent."""
    try:
        usage.update(prefetched()["usage"])
    except Exception:  # an unused review's failure must not fail the run
        pass


def _review_all(root, extracted, standing_policy, policy_path, policy_sha, manifest,
                model, transport, usage, snapshot, concurrency):
    """Review and select each entry in input order, with up to ``concurrency`` model calls in flight.

    Only model_review.review runs on worker threads, and only for entries without a
    standing request; staging, runs, selection and usage stay on this thread in input
    order, so the outcome does not depend on completion order.
    """
    def review(entry):
        return model_review.review(root, entry["rendition"], standing_policy, model, transport)

    if concurrency == 1:
        for entry in extracted:
            _review_and_select(root, entry, standing_policy, policy_path, policy_sha, manifest,
                               model, transport, usage, snapshot)
        return
    # Requests saved before this run need no model call; one read covers the run.
    # A request another writer stages later is still found by the fresh per-entry
    # read in _review_and_select, which then discards the unused review.
    existing = {r["correlation_id"] for r in store.list_requests(root)}
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
                                   future.result if future is not None else None)
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
    # Drop selections for renditions the cohort no longer has before selecting replacements,
    # so replacing a rendition never needs spare manifest capacity.
    current = {e["rendition"] for e in extracted}
    _set_manifest_renditions(root, manifest, [
        r for r in _manifest_renditions(root, manifest) if r["path"] in current])
    usage = Counter({"prompt_tokens": 0, "completion_tokens": 0})
    snapshot = event_reader.read_snapshot(root)
    retries = getattr(transport, "retries", None)
    retried = Counter(retries) if retries is not None else None
    _review_all(root, extracted, standing_policy, policy_path, policy_sha, manifest,
                model, transport, usage, snapshot, concurrency)
    selected = _manifest_renditions(root, manifest)
    qualified = [e for e in extracted if e.get("qualification") == "qualified"]
    # Reconcile the manifest to exactly the qualified renditions from validated review
    # evidence, keeping an operator's explicit selection whose saved review was superseded.
    stale = {e["superseded_event"] for e in extracted if "superseded_event" in e}
    kept = {e["rendition"] for e in extracted if e.get("review_code") == "review-superseded"}
    selected = [r for r in selected if not stale & set(r.get("review_event_ids") or [])]
    _set_manifest_renditions(root, manifest, [r for r in selected if r["path"] in kept]
                             + [e["selection"] for e in qualified])
    pub, inspected = _publish(root, policy_path, policy_sha, manifest, index_path)
    report["counts"]["qualification"] = dict(
        Counter(e["qualification"] for e in report["entries"] if "qualification" in e))
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
