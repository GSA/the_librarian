"""Model review and standing selection of a cohort's extracted renditions.

Each extracted rendition is reviewed by the configured model (unless the standing policy scope
refuses it, or the shared manifest has no room for it) and then settled through a two-stage
``standing-review``/``standing-select`` ledger request bound to the exact rendition. ``review_all``
records each entry's ``qualification`` (and ``review_code``) on the entry.

``run`` is the per-run state every step shares: ``policy`` (the loaded standing policy),
``policy_path``, ``policy_sha``, ``manifest``, ``model``, ``transport``, ``usage`` (a token
``Counter``), ``snapshot`` (the run-start review-event snapshot) and ``inspected_form``.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib

from tools.context_index import renditions
from tools.review_events import event_reader
from . import adapters, model_review, runner, selection_manifest, standing, store, store_busy
from .contracts import IntakeError

RUNNABLE = {"staged", "running", "retryable"}


def _selection_room(root, manifest, rendition):
    """True when the manifest already selects ``rendition`` or has room for one more selection."""
    known = selection_manifest.view(root, manifest)
    return known.count < renditions.MAX_SELECTIONS or known.has(rendition)


def _free_room(root, manifest):
    """Selections the shared manifest can still take (an exact read, advisory across processes)."""
    return renditions.MAX_SELECTIONS - selection_manifest.view(root, manifest).count


def _held_back(root, entry, standing_policy, manifest):
    """True when a model review would be paid for an entry that cannot be selected.

    The manifest has no room for the rendition and the policy scope does not already refuse it
    (a refused entry ends ``prohibited-scope`` without a call, as before). The check reads the
    manifest and never writes it; ``standing-select`` stays the authority on capacity.
    """
    return (not _selection_room(root, manifest, entry["rendition"])
            and not standing.refused_scope(root, standing_policy, entry["rendition"],
                                           entry["rendition_sha256"]))


def _standing_token(entry, policy_sha):
    return "%s-%s-%s" % (entry["submission_id"], entry["rendition_sha256"][:12], policy_sha[:8])


def _live_events(snapshot):
    """Event IDs in ``snapshot`` that no later event supersedes."""
    return {e["event_id"] for e in snapshot.events if e["event_id"] not in snapshot.successors}


def _blocked_with(stage, code):
    """True when ``stage`` is blocked and its last attempt ended with ``code``."""
    return stage["status"] == "blocked" and bool(stage["attempts"]) and stage["attempts"][-1]["code"] == code


def _registration_recovered(root, current, entry):
    """A review saved as ``unregistered-rendition`` whose registration the catalog confirms now.

    Before catalog reads distinguished an absent record from other failures, any catalog error
    (such as a validator changed under a running process) was saved as this durable outcome.
    """
    return (_blocked_with(current["stages"][0], "standing-exception-unregistered-rendition")
            and standing.registered(root, entry["rendition"], entry["rendition_sha256"]))


def _issues_now_qualify(root, current):
    """A review saved as ``unresolved-evidence`` that its saved, hash-verified findings now qualify.

    Reported contradictions and missing evidence used to block an accepted review; they are now
    attached to its event. Evaluating (which never writes or calls a model) leaves every other
    blocked review as it is."""
    if not _blocked_with(current["stages"][0], "standing-pending-review-unresolved-evidence"):
        return False
    arguments = store.request(root, current["correlation_id"])["stages"][0]["arguments"]
    try:
        result = standing.evaluate(root, adapters.standing_arguments(arguments))
    except standing.Refusal:
        return False
    return result["outcome"] == "qualified"


DEFERRED_HELD = "standing-deferred-reviewer-deferred"


def _deferral_now_qualifies(root, current, deferred):
    """A review saved as a held reviewer deferral that ``deferred`` (the operator's opt-in) now
    qualifies as low confidence, from its saved, hash-verified findings under its pinned policy.

    Only a deferral after a complete inspection under the current policy qualifies; evaluating
    never writes or calls a model."""
    if deferred == "hold" or not _blocked_with(current["stages"][0], DEFERRED_HELD):
        return False
    arguments = store.request(root, current["correlation_id"])["stages"][0]["arguments"]
    try:
        result = standing.evaluate(root, {**adapters.standing_arguments(arguments), "deferred": deferred})
    except standing.Refusal:
        return False
    return result["outcome"] == "qualified"


def _stage_correction(root, corr, active, extra=None):
    """Restage a standing review whose pinned intent no longer matches its inputs as a
    correction of it: same review arguments (plus ``extra``) and event, fresh request identities."""
    corrected = store.next_correction(root, corr)
    stages = store.request(root, active)["stages"]
    store.stage_request(root, corrected, [
        {**s, "arguments": {**s["arguments"], **(extra or {}), "request_id": f"req-{corrected}-{s['id']}"}}
        for s in stages], correction_of=active)
    return corrected


def _standing_outcome(stage):
    code = stage["attempts"][-1]["code"] if stage["attempts"] else stage["status"]
    for name in ("pending-review", "rejected", "deferred", "exception"):
        if code.startswith("standing-" + name + "-"):
            return name, code
    return "pending-review", code


def _review(root, entry, run):
    """Model-review one rendition, unless the policy scope refuses it: that costs no model call.

    A refused entry is staged with an empty findings pin that is never written; the
    ``standing-review`` adapter then ends it as ``prohibited-scope`` by its own scope check.
    """
    if standing.refused_scope(root, run["policy"], entry["rendition"], entry["rendition_sha256"]):
        home = store_busy.instance_roots(root)["intake_home"]
        return {"status": "reviewed", "skipped": "scope",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "findings_path": f"{home}/findings/out-of-scope/{entry['rendition_sha256'][:24]}.json",
                "findings_sha256": hashlib.sha256(b"").hexdigest()}
    return model_review.review(root, entry["rendition"], run["policy"], run["model"], run["transport"],
                               inspected_form=run["inspected_form"])


def _deferred_argument(deferred):
    return {} if deferred == "hold" else {"deferred": deferred}


def _stage_review(root, entry, run, corr, event_id, reviewed, correction_of=None):
    """Stage the review and selection of a reviewed rendition (as a correction of
    ``correction_of`` when given); return the request summary."""
    review_args = {
        "policy_path": run["policy_path"], "policy_sha256": run["policy_sha"],
        "rendition": entry["rendition"], "rendition_sha256": entry["rendition_sha256"],
        "findings_path": reviewed["findings_path"],
        "findings_sha256": reviewed["findings_sha256"], "event_id": event_id,
        **_deferred_argument(run.get("deferred", "hold")),
    }
    return store.stage_request(root, corr, [
        {"id": "review", "kind": "review", "operation": "standing-review",
         "arguments": {**review_args, "request_id": "req-" + corr + "-review"}},
        {"id": "select", "kind": "publication", "operation": "standing-select",
         "arguments": {**review_args, "request_id": "req-" + corr + "-select",
                       "manifest": run["manifest"]}}], correction_of=correction_of)


def _new_review(root, entry, run, corr, event_id, reviewed, spent=None):
    """Review (unless prefetched) and stage an entry without a standing request.

    Returns the staged request summary, or None when the entry's outcome is already decided.
    A review made here is also appended to ``spent``, so a retry can reuse it.
    """
    if reviewed is None:
        if _held_back(root, entry, run["policy"], run["manifest"]):
            # Nothing is reviewed or staged: a later run reviews it once when room appears.
            entry.update(qualification="pending-review", review_code="selection-capacity",
                         capacity_call_skipped=True)
            return None
        reviewed = _review(root, entry, run)
        if spent is not None:
            spent.append(reviewed)
    entry["model_call_skipped"] = reviewed.get("skipped") == "scope"
    run["usage"].update(reviewed["usage"])
    if reviewed["status"] != "reviewed":
        entry.update(qualification="pending-review", review_status=reviewed["status"])
        return None
    try:
        return _stage_review(root, entry, run, corr, event_id, reviewed)
    except IntakeError as error:
        if error.code != "correlation_conflict":
            raise
    # Another writer staged this review after the ledger lookup, with its own model reply;
    # reuse its request exactly as if the lookup had found it.
    return store.find_head(root, corr, run["heads"])


def _superseded(root, current, event_id, live):
    """True when a settled review's event is superseded, by the run-start snapshot or a fresh read."""
    # An event absent from the run-start snapshot may come from another writer during
    # this run; only a fresh read can show that it was superseded.
    return (all(st["status"] == "succeeded" for st in current["stages"])
            and event_id not in live
            and event_id not in _live_events(event_reader.read_snapshot(root)))


def _run_review(root, corr, current, entry, deferred="hold"):
    """Run the review stage, restaging it as a correction when its pinned intent went stale, or
    when ``deferred`` opts a held reviewer deferral in to qualifying as low confidence.

    Returns ``(active correlation ID, summary)``.
    """
    active = current["correlation_id"]
    if current["stages"][0]["status"] in RUNNABLE:
        current = runner.run(root, active, "review")
    if _deferral_now_qualifies(root, current, deferred):
        active = _stage_correction(root, corr, active, _deferred_argument(deferred))
        current = runner.run(root, active, "review")
    elif (_blocked_with(current["stages"][0], "changed-standing-inputs")
            or _registration_recovered(root, current, entry)
            or _issues_now_qualify(root, current)):
        # The saved review's pinned intent is stale (e.g. an earlier-version review it
        # conflicted with is now supersedable, a registration a failed catalog read
        # missed is confirmed now, or reported issues no longer block); re-pin it under a
        # correction request.
        active = _stage_correction(root, corr, active)
        current = runner.run(root, active, "review")
    return active, current


def _run_select(root, active, current, entry, manifest):
    """Run (or resume) the select stage; None when the full manifest leaves it pending."""
    if (_blocked_with(current["stages"][1], "selection-capacity")
            and _selection_room(root, manifest, entry["rendition"])):
        # The manifest was full when this was selected and has room now: resume it.
        current = runner.replay(root, active, "select")
    if current["stages"][0]["status"] != "succeeded" or current["stages"][1]["status"] not in RUNNABLE:
        return current
    if not _selection_room(root, manifest, entry["rendition"]):
        # Shared with other cohorts, the manifest is full: leave the saved review's select
        # stage unrun so a later qualify selects it once room appears, without a model call.
        return None
    return runner.run(root, active, "select")


def _conclude(entry, current, event_id):
    """Record the qualification a settled (or refused) standing request gives ``entry``."""
    if not all(s["status"] == "succeeded" for s in current["stages"]):
        failed = next(s for s in current["stages"] if s["status"] != "succeeded")
        outcome_name, code = _standing_outcome(failed)
        entry.update(qualification=outcome_name, review_code=code)
        return
    selection = current["stages"][1]["evidence"]["selection"]
    if selection["path"] != entry["rendition"] or selection["sha256"] != entry["rendition_sha256"]:
        entry.update(qualification="exception", review_code="selection-evidence-mismatch")
        return
    entry.update(qualification="qualified", review_event_id=event_id, selection=selection)


def _review_and_select(root, entry, run, prefetched=None, spent=None):
    """Review and select one extracted rendition; identity is bound to the exact rendition.

    ``prefetched`` optionally returns the model review already started for this entry
    on a worker thread; every ledger step still runs here, on the calling thread.
    ``run["heads"]`` shares one correction-chain index across the run and ``run["live"]`` the live
    event IDs of the run-start snapshot, so a settled entry costs neither a ledger scan nor an
    event scan.
    """
    token = _standing_token(entry, run["policy_sha"])
    corr, event_id = "standing-" + token, "rev-" + token
    reviewed, failure = None, None
    if prefetched is not None:
        # Wait for the prefetched review before the ledger lookup, so a request another
        # writer staged while the model call ran is found and staging follows at once.
        try:
            reviewed = prefetched()
        except Exception as error:
            failure = error
    current = store.find_head(root, corr, run["heads"])
    if current is None:
        if failure is not None:
            raise failure
        current = _new_review(root, entry, run, corr, event_id, reviewed, spent)
        if current is None:
            return
    elif reviewed is not None:
        # Another writer staged this review meanwhile; keep only the tokens actually spent.
        run["usage"].update(reviewed["usage"])
    if _superseded(root, current, event_id, run["live"]):
        # An operator superseded this review; never resurrect its selection.
        entry.update(qualification="pending-review", review_code="review-superseded",
                     superseded_event=event_id)
        return
    active, current = _run_review(root, corr, current, entry, run.get("deferred", "hold"))
    current = _run_select(root, active, current, entry, run["manifest"])
    if current is None:
        entry.update(qualification="pending-review", review_code="selection-capacity")
        return
    _conclude(entry, current, event_id)


def _settle(root, entry, run, prefetched=None):
    """``_review_and_select``, resumed while it fails only because a store is briefly locked.

    Each step it takes is a durable, idempotent ledger step, so a retry finds what was staged and
    goes on from there. The entry and token usage are restored before each attempt, and a model
    review that succeeded (prefetched, or made by an earlier attempt) is handed to the retry as its
    prefetched review: it is charged once and never paid for again (Kata 8as8).
    """
    entry_before, usage_before = dict(entry), run["usage"].copy()
    fetched, spent = [prefetched], []

    def attempt():
        entry.clear()
        entry.update(entry_before)
        run["usage"].clear()
        run["usage"].update(usage_before)
        try:
            _review_and_select(root, entry, run, fetched[0], spent)
        except Exception:
            if spent:
                reviewed = spent[-1]
                fetched[0] = lambda: reviewed
            elif fetched[0] is not None:
                try:
                    fetched[0]()
                except Exception:
                    fetched[0] = None  # a failed prefetch is not replayed; review in turn instead
            raise
    store_busy.retry(attempt)


def review_all(root, extracted, run, concurrency):
    """Review and select each entry in input order, with up to ``concurrency`` model calls in flight.

    Only model_review.review runs on worker threads, and only for entries without a
    standing request; staging, runs, selection and usage stay on this thread in input
    order, so the outcome does not depend on completion order.
    """
    # One correction-chain index and one live-event set serve every entry of the run.
    run = {**run, "heads": store.Heads(), "live": _live_events(run["snapshot"])}
    if concurrency == 1:
        for entry in extracted:
            _settle(root, entry, run)
        return
    # Requests saved before this run need no model call; one read covers the run.
    # A request another writer stages later is still found by the fresh per-entry
    # read in _review_and_select, which then discards the unused review.
    existing = store.correlation_ids(root)

    def ahead(entry, waiting):
        # A review is started ahead only while the manifest has room for every entry
        # still in the window; any other entry is checked exactly, in turn, in
        # _review_and_select, so no call is paid for an entry that cannot be selected.
        corr = "standing-" + _standing_token(entry, run["policy_sha"])
        return corr not in existing and _free_room(root, run["manifest"]) > waiting
    pipeline(extracted, concurrency, ahead, lambda entry: _review(root, entry, run),
             lambda entry, prefetched: _settle(root, entry, run, prefetched))


def pipeline(entries, concurrency, ahead, review, settle):
    """``settle(entry, prefetched)`` each entry in input order on this thread, while up to
    ``concurrency`` ``review(entry)`` calls run ahead on workers for entries ``ahead(entry, waiting)``
    accepts (``waiting``: entries already in the window); ``prefetched`` is the started review's
    ``result`` callable, or None."""
    window = deque()
    upcoming = iter(entries)
    with ThreadPoolExecutor(max_workers=concurrency,
                            thread_name_prefix="cohort-review") as pool:
        try:
            while True:
                # At most `concurrency` calls run; a bounded lookahead keeps workers busy
                # while this thread finishes the ledger work for the oldest entry.
                while len(window) < 2 * concurrency and (entry := next(upcoming, None)) is not None:
                    started = ahead(entry, len(window))
                    window.append((entry, pool.submit(review, entry) if started else None))
                if not window:
                    return
                entry, future = window.popleft()
                settle(entry, future.result if future is not None else None)
        except BaseException:
            for _, future in window:
                if future is not None:
                    future.cancel()
            raise
