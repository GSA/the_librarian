"""Read-only local cohort intake with explicit, counted dispositions.

Every file below a read-only inbox gets exactly one disposition. Admitted files
are submitted through manual upload intake as ``local-path`` copies (the inbox
is never written, moved or given a remote identity) and extracted through the
durable ledger. Extraction is not review: renditions stay deferred until a
standing-policy review qualifies them. Printed output is body-free counts.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from tools import policy
from tools.review_events import event_reader
from tools.catalog import catalog_store as catalog
from tools.durable import fsync_directory
from tools.context_index import context_index as index, renditions
from . import (agent_origin, cli, cohort_archives, deselect as deselection, exclusions as exclusion_rules, cohort_rereview, cohort_review, docsync, model_review, runner,
               selection_manifest, standing, store, throttle, upload, usage_log)
from . import cohort_runtime, contracts, parse_pool
from .cohort_ingest import BATCH_SIZE, classify, ingest
from .cohort_recovery import (recover_blocked_extractions, recover_deferred, recover_identity,
                              recover_unresolved_evidence)
from .contracts import IntakeError

DEFAULT_INDEX = "tools/context_index/.local/index.sqlite3"


def _write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")


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
    fsync_directory(path.parent)


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


def _recover_manifest(root, manifest):
    """Finish an append a crashed writer left half written, before anything reads the manifest."""
    try:
        if os.path.lexists(selection_manifest.intent_path(root, manifest)):
            with store.locked(root):
                selection_manifest.recover(root, manifest)
    except (index.IndexError_, OSError, ValueError) as error:
        raise IntakeError("preflight_invalid") from error


def _validate_inputs(root, policy_path, manifest, context, index_path, model):
    """Refuse bad paths and a missing maintained context before ingesting or writing anything."""
    _instance_file(root, policy_path)
    _instance_file(root, manifest)
    _recover_manifest(root, manifest)
    locators = context
    try:
        # The ledger requires ASCII relative paths; refuse the same ones before any mutation.
        for name in (policy_path, manifest, index_path):
            contracts.relative(name)
            name.encode("ascii")
        index.validate_db_path(root, root / index_path)
        if (root / manifest).is_file():
            index.load_manifest(index.no_symlinks(root / manifest), root)
        if (root / policy_path).is_file():
            raw = standing.read_private(root, policy_path, standing.LIMIT)
            loaded = standing.load_policy(root, policy_path, hashlib.sha256(raw).hexdigest())
            if loaded["reviewer"] != f"{model_review.PROVIDER}:{model}":
                raise IntakeError("policy_reviewer_mismatch")
            locators = loaded["selection"]["context"]
    except (index.IndexError_, standing.Refusal, IntakeError, OSError, UnicodeError) as error:
        raise IntakeError(error.code if isinstance(error, IntakeError) else "preflight_invalid") from error
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
            "schema": standing.POLICY_SCHEMA_V2,
            "policy_id": "cohort-standing",
            "version": 1,
            "criteria": standing.CRITERIA[-1],
            "carries": [],
            "status": "active",
            "reviewer": f"{model_review.PROVIDER}:{model}",
            "scope": {
                "receipts": [standing.ANY_RECEIPT],
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
        if manifest_file.is_file():
            selection_manifest.recover(root, manifest)
        raw = (json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.is_file() else {
            "schema_version": 1,
            "audience": policy.for_root(root)[0]["audience"],
            "documents": [],
            "context_rules": {},
            "renditions": [],
        })
        listed = {d["path"] for d in raw.setdefault("documents", [])}
        changed = not manifest_file.is_file()
        for locator in standing_policy["selection"]["context"]:
            doc = locator.split("#", 1)[0]
            if doc not in listed:
                raw["documents"].append({"path": doc, "role": "context", "context": []})
                listed.add(doc)
                changed = True
        if changed:
            _replace_json(manifest_file, raw)
    return standing_policy, policy_sha


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
        selection_manifest.recover(root, manifest)
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


def _publish(root, policy_path, policy_sha, manifest, index_path):
    """Return the publish stage, staging a fresh request when saved success is stale."""
    manifest_sha = hashlib.sha256((root / manifest).read_bytes()).hexdigest()[:16]
    destination = hashlib.sha256(str(index_path).encode("utf-8")).hexdigest()[:8]
    base = f"publish-{policy_sha[:12]}-{manifest_sha}-{destination}"
    previous = None
    for number in range(100):
        corr = base if number == 0 else f"{base}-r{number}"
        current = store.find(root, corr) or store.stage_request(root, corr, [{
            "id": "publish", "kind": "publication", "operation": "standing-publish",
            "arguments": {"policy_path": policy_path, "policy_sha256": policy_sha,
                          "manifest": manifest, "db": str(index_path)}}], correction_of=previous)
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


def _check_options(concurrency, initial_concurrency, inspected_form, deferred="hold"):
    if type(concurrency) is not int or concurrency < 1:
        raise IntakeError("invalid_concurrency")
    if initial_concurrency is not None and (
            type(initial_concurrency) is not int or not 1 <= initial_concurrency <= concurrency):
        raise IntakeError("invalid_concurrency")
    if inspected_form not in model_review.FORMS:
        raise IntakeError("invalid_inspected_form")
    if deferred not in standing.DEFERRED_MODES:
        raise IntakeError("invalid_deferred_mode")


def _reconcile_qualified(root, manifest, system, current, cohorts, extracted, retained=frozenset()):
    """Reconcile the cohort's manifest entries to exactly its qualified renditions.

    Selections come from validated review evidence, keeping an operator's explicit selection whose
    saved review was superseded. ``retained`` receipts (those of excluded files) keep their entries:
    an exclusion never removes a selection, ``deselect`` does. Returns ``_reconcile``'s counts.
    """
    qualified = [e for e in extracted if e.get("qualification") == "qualified"]
    stale = {e["superseded_event"] for e in extracted if "superseded_event" in e}
    kept = {e["rendition"] for e in extracted if e.get("review_code") == "review-superseded"}

    def reconciled(own, room):
        own = [r for r in own if not stale & set(r.get("review_event_ids") or [])]
        chosen = [r for r in own if r["path"] in kept or r["receipt_id"] in retained
                  ] + [e["selection"] for e in qualified]
        # A selection restored from saved evidence cannot exceed what the manifest can hold
        # next to other cohorts' entries; the surplus stays visibly pending, never invalid.
        surplus = {r["path"] for r in chosen[room:]}
        for entry in qualified:
            if entry["rendition"] in surplus:
                entry.update(qualification="pending-review", review_code="selection-capacity")
        return chosen[:room]
    return _reconcile(root, manifest, system, current, cohorts, reconciled)


def _qualification_counts(report, extracted, others, published):
    counts = report["counts"]
    counts["qualification"] = dict(
        Counter(e["qualification"] for e in report["entries"] if "qualification" in e))
    if others["selections"] or others["orphaned"]:
        # ``selections`` belong to other cohorts or to none provable (``unattributed``) and were
        # left as they were; ``orphaned`` named a receipt the catalog does not hold and were dropped.
        counts["other_cohorts"] = others
    refused = [e for e in extracted if e.get("review_code") == "standing-exception-prohibited-scope"]
    # Entries the policy scope refuses, and how many of them this run reached without a model call.
    counts["out_of_scope"] = {
        "entries": len(refused), "model_calls_skipped": sum(bool(e.get("model_call_skipped")) for e in refused)}
    # Entries left pending for manifest capacity, and how many of them cost no model call at all.
    counts["selection_capacity"] = {
        "entries": sum(e.get("review_code") == "selection-capacity" for e in extracted),
        "model_calls_skipped": sum(bool(e.get("capacity_call_skipped")) for e in extracted)}
    counts["published"] = int(published)


def qualify(root, inbox, *, system, actor, policy_path, manifest, model, transport,
            context=None, index_path=DEFAULT_INDEX, concurrency=1, adaptive=True,
            initial_concurrency=None, inspected_form="list", trust_stat=False, deferred="hold",
            extract_concurrency=cohort_runtime.DEFAULT_CONCURRENCY,
            origin=None, parse_workers=None, exclusions=None):
    """Ingest, model-review, select and publish the cohort; return body-free counts and usage.

    ``concurrency`` is the most model calls ever in flight. With ``adaptive`` (the default) the
    run starts at ``initial_concurrency`` (default: ``concurrency``) and halves the number in flight
    when the provider answers 429 or 503, adding one back per window of clean replies, so it never
    exceeds ``concurrency``; ``adaptive=False`` holds it fixed. ``report["throughput"]`` is the
    effective rate. ``inspected_form`` (``list`` or ``ranges``) only changes how the reviewer is
    asked to write the inspected record indices; findings and outcomes are identical.
    ``trust_stat``, ``extract_concurrency`` and ``parse_workers`` are passed to ``ingest`` (see there).
    ``deferred="qualify-low-confidence"`` (opt-in; default ``hold``) qualifies a reviewer deferral
    after a complete inspection under the current policy as low confidence instead of holding it,
    and restages this cohort's held deferrals that way from their saved findings (no model call).
    An agent ``origin`` (``agent_origin.load``) is passed to ``ingest``: the cohort's files are
    declared agent-generated and reviewed under the same standing policy as any other.
    ``exclusions`` (``exclusions.Rules``) keeps the files its rules match out of extraction, model
    review and selection; they get receipts and are reported ``excluded`` (see ``exclusions``). Their
    earlier selections of this cohort are dropped like those of any file no longer extracted.
    """
    _check_options(concurrency, initial_concurrency, inspected_form, deferred)
    if not context:
        # A maintained context document must be named; guessing one would not satisfy the policy.
        raise IntakeError("context_required")
    root = Path(root).resolve()
    _validate_inputs(root, policy_path, manifest, context, index_path, model)
    report = ingest(root, inbox, system=system, actor=actor, trust_stat=trust_stat,
                    extract_concurrency=extract_concurrency, origin=origin, parse_workers=parse_workers,
                    exclusions=exclusions)
    standing_policy, policy_sha = _ensure_standing_inputs(
        root, policy_path, manifest, model, context)
    extracted = [e for e in report["entries"] if e.get("outcome") == "extracted"]
    # Only this cohort's selections are pruned and reconciled: those of renditions it extracted
    # and those whose receipt was submitted under ``system``. Every other entry is kept as it is.
    # Dropping the cohort's selections for renditions it no longer has comes before selecting
    # replacements, so replacing a rendition never needs spare manifest capacity.
    current = {e["rendition"] for e in extracted}
    # A file an exclusion rule now matches keeps whatever selection it already has, until ``deselect``.
    retained = {e["receipt_id"] for e in report["entries"]
                if exclusion_rules.receipted(e) and e.get("receipt_id")}
    cohorts = {}
    pruned = _reconcile(root, manifest, system, current, cohorts,
                        lambda own, room: [r for r in own if r["path"] in current or r["receipt_id"] in retained])
    usage = Counter({"prompt_tokens": 0, "completion_tokens": 0})
    snapshot = event_reader.read_snapshot(root)
    retries = getattr(transport, "retries", None)
    retried = Counter(retries) if retries is not None else None
    limiter = throttle.Limiter(concurrency, initial=initial_concurrency, adaptive=adaptive)
    with limiter.gating(transport) as gated:
        cohort_review.review_all(root, extracted, {
            "policy": standing_policy, "policy_path": policy_path, "policy_sha": policy_sha,
            "manifest": manifest, "model": model, "transport": gated, "usage": usage,
            "snapshot": snapshot, "inspected_form": inspected_form, "deferred": deferred}, concurrency)
    others = _reconcile_qualified(root, manifest, system, current, cohorts, extracted, retained)
    pub, inspected = _publish(root, policy_path, policy_sha, manifest, index_path)
    others["orphaned"] = pruned["orphaned"]
    _qualification_counts(report, extracted, others, pub["status"] == "succeeded"
                          and bool(inspected.get("availability", {}).get("available")))
    report["usage"] = {"prompt_tokens": usage["prompt_tokens"],
                       "completion_tokens": usage["completion_tokens"]}
    # Body-free effective model-call rate and what the adaptive limit did; timing-dependent, so
    # kept apart from ``counts`` and ``entries``, which never vary with it.
    report["throughput"] = {**limiter.report(), "inspected_form": inspected_form}
    if retries is not None:
        # Body-free per-code retry counts made by the transport during this run.
        report["retries"] = {code: count for code, count in sorted(
            (Counter(retries) - retried).items())}
    return report


def rereview(root, *, policy_path, manifest, model, transport=None, apply=False, limit=None,
             include_current_deferred=False, include_older_rejected=False,
             deferred="qualify-low-confidence", concurrency=6,
             adaptive=True, initial_concurrency=None, inspected_form="list", cost_estimate=None):
    """Report, and with ``apply`` re-review under the current policy, held standing reviews.

    See ``cohort_rereview``. Selected: chain heads held under an older policy (pending review or
    deferred), ``insufficient-inspected-scope`` under the current one and, with
    ``include_current_deferred``, current-policy deferrals and, with ``include_older_rejected``,
    renditions rejected only under older policies (for new review criteria); never an out-of-scope
    rendition. ``deferred`` (default ``qualify-low-confidence``, the operator's choice for this
    command) qualifies a reviewer deferral after a complete inspection as low confidence. The dry
    run makes no model call and writes nothing; it reports the selection per reason and the
    estimated model calls and tokens (``cost_estimate``: a ``model_review.estimate`` file keyed by
    model). ``limit`` bounds the model calls of one run; rerunning continues where it stopped.
    """
    _check_options(concurrency, initial_concurrency, inspected_form, deferred)
    if limit is not None and (type(limit) is not int or limit < 0):
        raise IntakeError("invalid_limit")
    root = Path(root).resolve()
    for name in (policy_path, manifest):
        if not _instance_file(root, name).is_file():
            raise IntakeError("rereview_input_missing")
    _validate_inputs(root, policy_path, manifest, None, DEFAULT_INDEX, model)
    policy_sha = hashlib.sha256((root / policy_path).read_bytes()).hexdigest()
    standing_policy = standing.load_policy(root, policy_path, policy_sha)
    with store.locked(root) as c:
        selected, skipped = cohort_rereview.select(root, c, policy_sha, include_current_deferred,
                                                   include_older_rejected)
    report, work = cohort_rereview.plan(root, selected, skipped, standing_policy, limit=limit,
                                        inspected_form=inspected_form, model=model,
                                        cost_estimate=cost_estimate)
    report["deferred"] = deferred
    if not apply:
        return report
    if transport is None:
        raise IntakeError("transport_required")
    report["apply"] = True
    report.update(cohort_rereview.apply(root, work, {
        "policy": standing_policy, "policy_path": policy_path, "policy_sha": policy_sha,
        "manifest": manifest, "model": model, "transport": transport,
        "usage": Counter({"prompt_tokens": 0, "completion_tokens": 0}),
        "inspected_form": inspected_form, "deferred": deferred},
        concurrency, adaptive=adaptive, initial_concurrency=initial_concurrency))
    return report


def publish(root, *, policy_path, manifest, index_path=DEFAULT_INDEX):
    """Publish the current manifest under the current standing policy; return a body-free report.

    The same ``standing-publish`` stage ``qualify`` ends with (its pinning, freshness, availability
    and selection-authorization checks, and its ledger evidence), without ingesting or reviewing
    anything: it recovers an interrupted ``qualify``, whose reviews and selections are saved, at
    the cost of one publication. Idempotent: an unchanged manifest with a fresh index is reused.
    """
    root = Path(root).resolve()
    try:
        for name in (policy_path, manifest):
            _instance_file(root, name)
            contracts.relative(name)
            name.encode("ascii")
        contracts.relative(index_path)
        index.validate_db_path(root, root / index_path)
        _recover_manifest(root, manifest)
        if not (root / policy_path).is_file() or not (root / manifest).is_file():
            raise IntakeError("publish_input_missing")
        policy_sha = hashlib.sha256((root / policy_path).read_bytes()).hexdigest()
        standing.load_policy(root, policy_path, policy_sha)
        selections = len(index.load_manifest(index.no_symlinks(root / manifest), root)[0]
                         .get("renditions", []))
    except (index.IndexError_, standing.Refusal, OSError, UnicodeError, ValueError) as error:
        raise IntakeError("preflight_invalid") from error
    stage, inspected = _publish(root, policy_path, policy_sha, manifest, index_path)
    available = bool(inspected.get("availability", {}).get("available"))
    published = stage["status"] == "succeeded" and available
    publication = {"status": stage["status"], "available": available}
    if stage["status"] != "succeeded" and stage["attempts"]:
        publication["code"] = stage["attempts"][-1]["code"]
    return {"counts": {"published": int(published), "selections": selections},
            "publication": publication}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser(
        "recover-identity", allow_abbrev=False,
        help="Report (or with --apply correct) registrations blocked as changed-extraction-identity")
    recover.add_argument("--root", required=True, type=Path)
    recover.add_argument("--apply", action="store_true")
    blocked = commands.add_parser(
        "recover-blocked-extractions", allow_abbrev=False,
        help="Report (or with --apply restage for the next ingest) extractions blocked with --code "
             "by a since-fixed toolkit")
    blocked.add_argument("--root", required=True, type=Path)
    blocked.add_argument("--code", required=True, help="the blocked attempt's code, e.g. extract-mutation")
    blocked.add_argument("--apply", action="store_true")
    unresolved = commands.add_parser(
        "recover-unresolved-evidence", allow_abbrev=False,
        help="Report (or with --apply qualify) accepted reviews blocked as unresolved-evidence, "
             "from their saved findings and without a model call")
    unresolved.add_argument("--root", required=True, type=Path)
    unresolved.add_argument("--apply", action="store_true")
    held = commands.add_parser(
        "recover-deferred", allow_abbrev=False,
        help="Report (or with --apply qualify as low confidence) reviews held as reviewer-deferred "
             "after a complete inspection under the current policy, from their saved findings "
             "and without a model call")
    held.add_argument("--root", required=True, type=Path)
    held.add_argument("--apply", action="store_true")
    backfill = commands.add_parser(
        "backfill-docs", allow_abbrev=False,
        help="Report (or with --apply create) the derived docs table and its missing rows from the requests")
    backfill.add_argument("--root", required=True, type=Path)
    backfill.add_argument("--apply", action="store_true")
    for name, text in (("audit-docs", "Recompute the docs table from the requests and report differences"),
                       ("docs-status", "Count documents per state from the docs table")):
        command = commands.add_parser(name, allow_abbrev=False, help=text)
        command.add_argument("--root", required=True, type=Path)
    command = commands.add_parser("publish", allow_abbrev=False, help=publish.__doc__.split("\n")[0])
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--policy", required=True, dest="policy_path")
    command.add_argument("--manifest", required=True)
    command.add_argument("--index", default=DEFAULT_INDEX, dest="index_path")
    command = commands.add_parser(
        "expand-archives", allow_abbrev=False,
        help="Report every zip/tar archive the inbox reaches; with --apply expand those not yet "
             "expanded into instance storage (one nesting level per run)")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--inbox", required=True, type=Path)
    command.add_argument("--apply", action="store_true")
    command.add_argument("--report", type=Path, help="private (0600) per-archive report with paths")
    command = commands.add_parser(
        "delete-archive-original", allow_abbrev=False,
        help="Verify that an expanded inbox archive can be deleted without loss; with --apply "
             "record the deletion and delete it")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--inbox", required=True, type=Path)
    command.add_argument("--archive", required=True, help="inbox-relative path of the archive")
    command.add_argument("--actor")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--report", type=Path, help="private (0600) report listing blocking members")
    for name in ("classify", "ingest", "qualify"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--inbox", required=True, type=Path)
        command.add_argument("--report", type=Path,
                             help="private (0600) per-file report with paths and hashes")
        command.add_argument("--exclusions", type=Path,
                             help="exclusion rules file (JSON); default config/intake-exclusions.local.json "
                                  "in the instance when it exists; not for --agent-origin cohorts")
        command.add_argument("--agent-origin", type=Path,
                             help="origin file (JSON) declaring the inbox agent-written: generator, "
                                  "include/exclude paths and git provenance; indexed as low confidence")
        if name in {"ingest", "qualify"}:
            command.add_argument("--system", required=True)
            command.add_argument("--actor", required=True)
            command.add_argument("--trust-stat", action="store_true",
                                 help="skip reading and hashing an extracted file whose stat identity "
                                      "equals its docs row (default: every file is hashed)")
            command.add_argument("--parse-workers", type=int, default=None,
                                 help="processes that read, hash and parse text-family files while this "
                                      "process commits (default: CPUs, at most %d, and none for a small "
                                      "inbox; 1 parses in this process); output is identical at any count"
                                      % parse_pool.DEFAULT_WORKERS)
            command.add_argument("--extract-concurrency", type=int, default=cohort_runtime.DEFAULT_CONCURRENCY,
                                 help="PDF/image conversions in flight, each its own limited process "
                                      "(default %d; ~1.5 GiB resident each)" % cohort_runtime.DEFAULT_CONCURRENCY)
        if name == "ingest":
            command.add_argument("--limit", type=int)
            command.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                                 help="documents whose catalog registrations share one revision "
                                      "(default %d; 1 commits each registration on its own)" % BATCH_SIZE)
        if name == "qualify":
            _model_options(command, models_required=True, deferred="hold")
            command.add_argument("--context", action="append", dest="context")
            command.add_argument("--index", default=DEFAULT_INDEX, dest="index_path")
    command = commands.add_parser(
        "deselect", allow_abbrev=False,
        help="Report (or with --apply remove) selections from the manifest by receipt, exclusion rule "
             "or byte-identical duplicate; then run publish")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--manifest", required=True)
    command.add_argument("--receipt", action="append", default=[], metavar="D123",
                         help="deselect this receipt's selection (repeatable)")
    command.add_argument("--receipts-file", type=Path,
                         help="receipt IDs (one per line) or the --report of an earlier dry run")
    command.add_argument("--rule", action="append", default=[], metavar="ID",
                         help="deselect selections whose inbox path this exclusion rule matches (repeatable)")
    command.add_argument("--all-rules", action="store_true", help="every rule of the exclusion file")
    command.add_argument("--exclusions", type=Path,
                         help="exclusion rules file; default config/intake-exclusions.local.json")
    command.add_argument("--system", help="only selections ingested under this --system label (with --rule)")
    command.add_argument("--dedupe", action="store_true",
                         help="of byte-identical selections keep the lowest receipt and deselect the rest")
    command.add_argument("--actor", help="required with --apply; recorded in the evidence")
    command.add_argument("--reason", help="optional operator note (printable ASCII, at most 200)")
    command.add_argument("--report", type=Path, help="private (0600) report with paths, receipts and groups")
    command.add_argument("--apply", action="store_true")
    command = commands.add_parser(
        "rereview", allow_abbrev=False,
        help="Report (or with --apply re-review with the model under the current policy) standing "
             "reviews held under an older policy or after an incomplete inspection")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--apply", action="store_true")
    _model_options(command, models_required=False, deferred="qualify-low-confidence")
    command.add_argument("--limit", type=int, help="most model calls this run (default: all selected)")
    command.add_argument("--include-current-deferred", action="store_true",
                         help="also re-review reviewer deferrals held under the current policy")
    command.add_argument("--include-older-rejected", action="store_true",
                         help="also re-review renditions rejected only under older policies "
                              "(after the review criteria change)")
    command.add_argument("--cost-estimate", type=Path,
                         help="token estimate file keyed by model (per_document tokens) for the dry run")
    return parser


def _model_options(command, *, models_required, deferred):
    """The model review options ``qualify`` and ``rereview`` share."""
    command.add_argument("--policy", required=True, dest="policy_path")
    command.add_argument("--manifest", required=True)
    command.add_argument("--models-config", required=models_required, type=Path,
                         help=None if models_required else "required with --apply")
    command.add_argument("--model", required=True)
    command.add_argument("--concurrency", type=int, default=6,
                         help="most model review calls in flight (default 6; 1 is serial); "
                              "an adaptive run backs off below it on 429/503, never above")
    command.add_argument("--initial-concurrency", type=int,
                         help="calls in flight at the start (default: --concurrency)")
    command.add_argument("--no-adaptive", action="store_true",
                         help="hold --concurrency fixed instead of backing off on 429/503")
    command.add_argument("--inspected-form", choices=model_review.FORMS, default="list",
                         help="ask the reviewer for inspected records as every index "
                              "(list, default) or as ranges (ranges)")
    command.add_argument("--deferred", choices=standing.DEFERRED_MODES, default=deferred,
                         help="hold reviewer deferrals unpublished (hold) or qualify completely "
                              "inspected ones as low confidence (qualify-low-confidence); "
                              "default %s" % deferred)


def _transport(models_config):
    return model_review.usai_transport(
        models_config, recorder=usage_log.Recorder("librarian intake", model_review.PROVIDER))


def _rereview(args):
    if args.apply and args.models_config is None:
        raise IntakeError("models_config_required")
    return rereview(
        args.root, policy_path=args.policy_path, manifest=args.manifest, model=args.model,
        transport=_transport(args.models_config) if args.apply else None, apply=args.apply,
        limit=args.limit, include_current_deferred=args.include_current_deferred,
        include_older_rejected=args.include_older_rejected, deferred=args.deferred, concurrency=args.concurrency, adaptive=not args.no_adaptive,
        initial_concurrency=args.initial_concurrency, inspected_form=args.inspected_form,
        cost_estimate=args.cost_estimate)


def _deselect(args):
    """Run ``deselect``; return the report (printed body-free apart from ``--report``)."""
    selecting = args.all_rules or args.rule
    rules = (exclusion_rules.load(args.exclusions) if args.exclusions is not None
             else exclusion_rules.discover(args.root)) if selecting or args.exclusions else None
    contracts.require(not selecting or rules is not None, "exclusions_missing")
    receipts = [*args.receipt, *(deselection.read_receipts(args.receipts_file) if args.receipts_file else [])]
    return deselection.deselect(
        args.root, args.manifest, actor=args.actor, receipts=receipts,
        rules=rules if selecting else None, rule_ids=args.rule, system=args.system, dedupe=args.dedupe,
        reason=args.reason, apply=args.apply)


def _maintenance(args):
    """The report of a ledger maintenance command (identity recovery or the docs table)."""
    if args.command == "recover-identity":
        return recover_identity(args.root, apply=args.apply)
    if args.command == "recover-blocked-extractions":
        return recover_blocked_extractions(args.root, args.code, apply=args.apply)
    if args.command == "recover-unresolved-evidence":
        return recover_unresolved_evidence(args.root, apply=args.apply)
    if args.command == "recover-deferred":
        return recover_deferred(args.root, apply=args.apply)
    root = args.root.resolve()
    if args.command == "backfill-docs":
        return docsync.backfill(root, apply=args.apply)
    return docsync.audit(root) if args.command == "audit-docs" else docsync.status(root)


def _inbox_report(args):
    """The report of an inbox command: ``qualify``, ``ingest`` or ``classify``."""
    origin = agent_origin.load(args.agent_origin) if args.agent_origin else None
    rules = exclusion_rules.resolve(args.root, args.exclusions, origin)
    if args.command == "qualify":
        return qualify(
            args.root, args.inbox, system=args.system, actor=args.actor,
            policy_path=args.policy_path, manifest=args.manifest, model=args.model,
            transport=_transport(args.models_config),
            context=args.context, index_path=args.index_path,
            concurrency=args.concurrency, adaptive=not args.no_adaptive,
            initial_concurrency=args.initial_concurrency,
            inspected_form=args.inspected_form, trust_stat=args.trust_stat,
            deferred=args.deferred, extract_concurrency=args.extract_concurrency, origin=origin,
            parse_workers=args.parse_workers, exclusions=rules)
    if args.command == "ingest":
        return ingest(args.root, args.inbox, system=args.system, actor=args.actor,
                      limit=args.limit, batch_size=args.batch_size, trust_stat=args.trust_stat,
                      extract_concurrency=args.extract_concurrency, origin=origin,
                      parse_workers=args.parse_workers, exclusions=rules)
    entries = classify(args.root, args.inbox, origin=origin, exclusions=rules)
    admitted = [e for e in entries if e["disposition"] == "admit"]
    counts = {"files": len(entries), "disposition": dict(Counter(e["disposition"] for e in entries)),
              "format": dict(Counter(e["format"] for e in admitted)),
              "admitted_bytes": sum(e["bytes"] for e in admitted)}
    if origin is not None:
        counts["agent_origin"] = agent_origin.declare(args.inbox, origin, entries)
    if rules is not None:
        counts["excluded"] = exclusion_rules.counts(entries, rules)
    return {"counts": counts, "entries": entries}


def _archive_command(args):
    """Run ``expand-archives`` or ``delete-archive-original``; print body-free counts only."""
    if args.command == "expand-archives":
        report = cohort_archives.expand(args.root, args.inbox, apply=args.apply)
        summary, status = {"counts": report["counts"]}, 0
    else:
        report = cohort_archives.delete_original(args.root, args.inbox, args.archive,
                                                 actor=args.actor, apply=args.apply)
        summary = {k: report.get(k) for k in ("status", "deleted", "members", "registered",
                                              "blocked_by_code")}
        status = 0 if report["status"] in {"verified", "deleted", "already-deleted"} else 1
    if args.report:
        _write_private(args.report, report)
    print(json.dumps(summary, sort_keys=True))
    return status


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command in {"recover-identity", "recover-blocked-extractions", "recover-unresolved-evidence", "recover-deferred", "backfill-docs", "audit-docs", "docs-status"}:
            print(json.dumps(_maintenance(args), sort_keys=True))
            return 0
        if args.command in {"expand-archives", "delete-archive-original"}:
            return _archive_command(args)
        if args.command == "rereview":
            print(json.dumps(_rereview(args), sort_keys=True))
            return 0
        if args.command == "deselect":
            report = _deselect(args)
            if args.report:
                _write_private(args.report, report)
            print(json.dumps({k: v for k, v in report.items() if k not in {"entries", "groups", "unmatched"}},
                             sort_keys=True))
            return 0
        if args.command == "publish":
            report = publish(args.root, policy_path=args.policy_path, manifest=args.manifest,
                             index_path=args.index_path)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["counts"]["published"] else 1
        report = _inbox_report(args)
    except (IntakeError, standing.CatalogUnavailable) as error:
        # After CatalogUnavailable nothing about the entry was saved; restart once the catalog is readable.
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    if args.report:
        _write_private(args.report, report)
    summary = {"counts": report["counts"]}
    for key in ("usage", "retries", "throughput"):
        if key in report:
            summary[key] = report[key]
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
