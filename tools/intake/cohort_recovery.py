"""Recovery of cohort registrations blocked as ``changed-extraction-identity``."""
from collections import Counter
from pathlib import Path

from tools.catalog import catalog_store as catalog
from . import adapters, cohort_review, contracts, runner, standing, store
from .contracts import IntakeError


def _blocked_heads(root, operation, code):
    """``(correlation ID, stage, stage count)`` of the chain heads whose ``operation`` stage is blocked
    and whose last attempt failed with ``code``."""
    found = []
    with store.locked(root) as c:
        rows = c.execute("SELECT s.correlation_id, s.ordinal, r.raw FROM stages s "
                         "JOIN requests r USING(correlation_id) WHERE s.status='blocked' "
                         "ORDER BY s.correlation_id").fetchall()
        for row in rows:
            stages = contracts.decode_request(row["raw"])["stages"]
            stage = stages[row["ordinal"]]
            if stage["operation"] != operation:
                continue
            if c.execute("SELECT 1 FROM requests WHERE correction_of=?", (row["correlation_id"],)).fetchone():
                continue  # not the head: a correction follows it
            last = c.execute("SELECT code FROM attempts WHERE correlation_id=? AND stage_id=? "
                             "ORDER BY cycle DESC, ordinal DESC LIMIT 1",
                             (row["correlation_id"], stage["id"])).fetchone()
            if last is not None and last[0] == code:
                found.append((row["correlation_id"], stage, len(stages)))
    return found


def _identity_blocked(root):
    """Chain heads that are blocked ``extract-register`` stages refused as a changed identity."""
    return [(corr, stage) for corr, stage, _ in
            _blocked_heads(root, "extract-register", "changed-extraction-identity")]


def _root_of(root, corr):
    """The first request of ``corr``'s correction chain (its own name when it corrects nothing)."""
    seen = {corr}
    while (parent := store.request(root, corr)["correction_of"]) is not None and parent not in seen:
        corr = parent
        seen.add(corr)
    return corr


def _correct(root, head, rewrite=lambda arguments: arguments):
    """Stage, and return the ID of, a correction of ``head`` repeating its single stage."""
    (stage,) = store.request(root, head["correlation_id"])["stages"]
    corrected = store.next_correction(root, _root_of(root, head["correlation_id"]))
    store.stage_request(root, corrected, [{**stage, "arguments": rewrite(stage["arguments"])}],
                        correction_of=head["correlation_id"])
    return corrected


def _candidate(root, corr, stage, heads):
    """``(prepare head, reuse)`` for one blocked registration, or the reason it is skipped.

    ``reuse`` is True when the paired prepare already succeeded under today's extractor, False when
    it succeeded under another, and None when it can still run.
    """
    base = _root_of(root, corr)
    args = stage["arguments"]
    prefix = "register-"
    prepare = store.find_head(root, "extract-" + base[len(prefix):], heads) if base.startswith(prefix) else None
    saved = store.request(root, prepare["correlation_id"])["stages"] if prepare else []
    if (len(saved) != 1 or saved[0]["operation"] != "extract-prepare"
            or any(saved[0]["arguments"].get(k) != args[k] for k in ("receipt", "format_name"))):
        return "prepare-mismatch"
    status = prepare["stages"][0]["status"]
    if status == "succeeded":
        try:
            fresh = adapters.prepare(store.safe(root), saved[0])
        except (adapters.StageFailure, OSError):
            # A removed, unreadable or no longer valid source: counted, not fatal to the rest.
            return "source-unavailable"
        return prepare, prepare["stages"][0]["evidence"]["run_id"] == fresh["run_id"]
    if status not in {"staged", "running", "retryable"}:
        return "prepare-blocked"
    return prepare, None


def _repair(root, corr, prepare, reuse):
    """Re-prepare (unless reusable) and re-register one blocked chain; return the result to count."""
    if reuse is not True:
        prepare_id = _correct(root, prepare) if reuse is False else prepare["correlation_id"]
        done = runner.settle(root, prepare_id, prepare["stages"][0]["id"])["stages"][0]
        if done["status"] != "succeeded":
            return "prepare-" + done["status"]
    fixed = _correct(root, store.inspect(root, corr), lambda a: {
        **a, "expected_state": catalog.status(root)["state_token"]})
    return runner.settle(root, fixed, store.inspect(root, fixed)["stages"][0]["id"])["stages"][0]["status"]


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
    skipped, work = Counter(), []
    for corr, stage in _identity_blocked(root):
        candidate = _candidate(root, corr, stage, heads)
        if isinstance(candidate, str):
            skipped[candidate] += 1
        else:
            work.append((corr, *candidate))
    counts = {"extract-register/changed-extraction-identity": len(work)} if work else {}
    report = {"apply": apply, "candidates": len(work), "by_operation_code": counts,
              "skipped": dict(skipped)}
    if not apply:
        return report
    results = Counter()
    for corr, prepare, reuse in work:
        try:
            results[_repair(root, corr, prepare, reuse)] += 1
        except IntakeError as error:
            results[error.code] += 1
    report["results"] = dict(results)
    return report


def recover_blocked_extractions(root, code, *, apply=False):
    """Report, and with ``apply`` restage, extractions blocked with ``code`` by a since-fixed toolkit.

    A blocked ``extract-prepare`` pins the extractor identity it first prepared under, so after the
    toolkit fix that changes the identity, a replay can only block again. For each chain head that
    is a blocked ``extract-prepare`` whose last attempt failed with ``code``, ``apply`` stages a
    correction repeating its operation and arguments and does not run it: the next ``ingest``
    follows ``correction_of`` to it and converts it like any other open extraction (PDFs and images
    concurrently), then registers it. The correction pins today's extractor when it first prepares,
    so a source or toolkit that still fails blocks it again. Repaired chains are not candidates again.
    Only the single-stage requests that cohort ingest stages are corrected; a multi-stage request
    is counted in ``skipped`` and untouched.
    """
    root = Path(root).resolve()
    work, skipped = [], Counter()
    for corr, _, count in _blocked_heads(root, "extract-prepare", code):
        if count == 1:
            work.append(corr)
        else:
            skipped["multi-stage"] += 1
    report = {"apply": apply, "code": code, "candidates": len(work), "skipped": dict(skipped)}
    if not apply:
        return report
    results = Counter()
    for corr in work:
        try:
            _correct(root, {"correlation_id": corr})
            results["staged"] += 1
        except IntakeError as error:
            results[error.code] += 1
    report["results"] = dict(results)
    return report


UNRESOLVED = "standing-pending-review-unresolved-evidence"


def _unresolved_blocked(root, code=UNRESOLVED):
    """Chain heads whose standing review is blocked with ``code`` (default ``unresolved-evidence``):
    ``(head correlation ID, rendition, manifest)``."""
    found = []
    with store.locked(root) as c:
        rows = c.execute("SELECT s.correlation_id, r.raw FROM stages s JOIN requests r USING(correlation_id) "
                         "WHERE s.status='blocked' AND s.ordinal=0 ORDER BY s.correlation_id").fetchall()
        for row in rows:
            request = contracts.decode_request(row["raw"])
            review = request["stages"][0]
            if review["operation"] != "standing-review" or len(request["stages"]) != 2:
                continue
            if c.execute("SELECT 1 FROM requests WHERE correction_of=?", (row["correlation_id"],)).fetchone():
                continue  # not the head: a correction follows it
            last = c.execute("SELECT code FROM attempts WHERE correlation_id=? AND stage_id=? "
                             "ORDER BY cycle DESC, ordinal DESC LIMIT 1",
                             (row["correlation_id"], review["id"])).fetchone()
            if last is not None and last[0] == code:
                found.append((row["correlation_id"], review["arguments"]["rendition"],
                              request["stages"][1]["arguments"].get("manifest")))
    return found


def recover_unresolved_evidence(root, *, apply=False):
    """Report, and with ``apply`` re-evaluate, reviews blocked as ``unresolved-evidence``.

    An accepted review's reported contradictions and missing evidence now qualify it with the
    issues attached instead of blocking it. Each chain head blocked that way is re-evaluated from
    its saved, hash-verified findings under its pinned policy: no model call is made. One that
    now qualifies is restaged as a correction with the same arguments (the ledger stays
    append-only), reviewed, and selected under the shared manifest's capacity (a full manifest
    leaves its select staged for a later ``qualify``). One that still does not qualify (incomplete
    inspection, a conflicting review, a changed policy, ...) is counted in ``skipped`` and
    untouched, as is every qualified, rejected, deferred or exception review. Run ``publish`` after.
    """
    root = Path(root).resolve()
    return _recover(root, apply, UNRESOLVED,
                    lambda current: cohort_review._issues_now_qualify(root, current))


def recover_deferred(root, *, apply=False, deferred="qualify-low-confidence"):
    """Report, and with ``apply`` qualify as low confidence, reviews held as reviewer deferrals.

    The operator opt-in ``qualify --deferred qualify-low-confidence`` applied to reviews already
    held: each chain head blocked ``reviewer-deferred`` is re-evaluated from its saved,
    hash-verified findings under its pinned policy with the opt-in, without a model call. One that
    now qualifies (a complete inspection under the current policy, no conflicting review, ...) is
    restaged as a correction carrying the opt-in, reviewed, and selected under the shared
    manifest's capacity. Anything else (incomplete inspection, a changed or older policy, ...) is
    counted in ``skipped`` and untouched. Run ``publish`` after.
    """
    root = Path(root).resolve()
    return _recover(root, apply, cohort_review.DEFERRED_HELD,
                    lambda current: cohort_review._deferral_now_qualifies(root, current, deferred),
                    deferred)


def _recover(root, apply, code, now_qualifies, deferred="hold"):
    """Re-evaluate the chain heads blocked with ``code`` that ``now_qualifies`` and, with
    ``apply``, restage, review and select them as corrections."""
    heads = store.Heads()
    work, skipped = [], Counter()
    for corr, rendition, manifest in _unresolved_blocked(root, code):
        current = store.find_head(root, corr, heads)
        if not now_qualifies(current):
            skipped["not-qualified"] += 1
        else:
            work.append((corr, current, rendition, manifest))
    report = {"apply": apply, "candidates": len(work), "skipped": dict(skipped)}
    if not apply:
        return report
    results = Counter()
    for corr, current, rendition, manifest in work:
        try:
            active, current = cohort_review._run_review(root, corr, current, {"rendition": rendition}, deferred)
            current = cohort_review._run_select(root, active, current, {"rendition": rendition}, manifest)
        except (IntakeError, standing.Refusal) as error:
            results[getattr(error, "code", "refused")] += 1
            continue
        if current is None:
            results["selection-capacity"] += 1
        else:
            results["qualified" if current["complete"] else "not-complete"] += 1
    report["results"] = dict(results)
    return report
