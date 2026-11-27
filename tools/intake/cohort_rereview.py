"""Re-review of standing reviews held under an older policy or after an incomplete inspection.

``qualify`` only reviews renditions of the inbox it is given, and leaves a blocked standing review
as it is: a review held under an older policy (which now refuses ``standing-policy-changed``), a
reviewer deferral, or an ``insufficient-inspected-scope`` review stays blocked forever.
``rereview`` finds those chain heads in the ledger, one per rendition, and with ``apply`` reviews
each again with the model under the current policy. The new review is staged as a correction of
the blocked head (the ledger stays append-only) under the name ``qualify`` itself uses for the
current policy, so a later ``qualify`` finds it and never pays for it twice. Out-of-scope
renditions are never selected; rejected ones only with ``include_older_rejected``, when every
rejection was under an older policy (new review criteria may accept them). Run ``cohort publish``
after an applied run.
"""
from collections import Counter
from pathlib import Path
import json
import math
import re

from tools.context_index import context_index as index
from . import adapters, cohort_review, docs, model_review, standing, store, throttle
from .contracts import IntakeError

INSUFFICIENT = "standing-pending-review-insufficient-inspected-scope"
# A review refused on the per-review record or page budget: it was made in one call, before a
# rendition could be reviewed in parts. Only a rendition that now needs parts is worth a new review.
BUDGET = "standing-exception-budget-exceeded"
UNRESOLVED = "standing-pending-review-unresolved-evidence"
# Codes of an older-policy review that a review under the current policy can settle.
OLDER_PENDING = frozenset({UNRESOLVED, INSUFFICIENT,
                           "standing-policy-changed"})
REASONS = ("resume", "older-policy-pending", "older-policy-deferred", "older-policy-rejected",
           "insufficient-inspected-scope", "current-policy-deferred", "budget-exceeded")
# ``standing-<submission>-<rendition sha256[:12]>-<policy sha256[:8]>`` and its ``-c<n>`` corrections.
NAME = re.compile(r"standing-(.+)-([0-9a-f]{12})-[0-9a-f]{8}(?:-c[0-9]+)?")
CHARS_PER_TOKEN = model_review.CHARS_PER_TOKEN


def _heads(c):
    """Every standing review chain head on an open ledger connection."""
    corrected = {p for (p,) in c.execute("SELECT correction_of FROM requests WHERE correction_of IS NOT NULL")}
    statuses = {}
    for corr, ordinal, status in c.execute(
            "SELECT correlation_id, ordinal, status FROM stages WHERE correlation_id LIKE 'standing-%'"):
        statuses.setdefault(corr, {})[ordinal] = status
    heads = []
    for corr, raw in c.execute("SELECT correlation_id, raw FROM requests WHERE correlation_id LIKE 'standing-%'"):
        if corr in corrected:
            continue
        stages = json.loads(raw)["stages"]
        if len(stages) != 2 or stages[0]["operation"] != "standing-review":
            continue
        args = stages[0]["arguments"]
        name = NAME.fullmatch(corr)
        if name is None or name[2] != args["rendition_sha256"][:12]:
            continue
        head = {"corr": corr, "submission_id": name[1], "args": args,
                "review": statuses[corr][0], "select": statuses[corr][1], "code": None}
        if head["review"] == "blocked":
            last = c.execute("SELECT code FROM attempts WHERE correlation_id=? AND stage_id=? "
                             "ORDER BY cycle DESC, ordinal DESC LIMIT 1", (corr, stages[0]["id"])).fetchone()
            head["code"] = last[0] if last else None
        heads.append(head)
    return heads


def _reason(root, head, current, include_current_deferred, snapshot):
    """Why ``head`` (a blocked review) is re-reviewed, or ``(None, skip reason)``."""
    code = head["code"]
    if code == BUDGET:
        return "budget-exceeded", None
    if head["args"]["policy_sha256"] != current:
        if code == cohort_review.DEFERRED_HELD:
            return "older-policy-deferred", None
        return ("older-policy-pending", None) if code in OLDER_PENDING else (None, code)
    if code == INSUFFICIENT:
        return "insufficient-inspected-scope", None
    if code == UNRESOLVED or (code == cohort_review.DEFERRED_HELD and include_current_deferred):
        # Saved findings that qualify (with the low-confidence opt-in) need no model call:
        # ``recover-unresolved-evidence`` / ``recover-deferred`` settle them. Only an incomplete
        # inspection is worth a new review.
        try:
            result = standing.evaluate(root, {**adapters.standing_arguments(head["args"]),
                                              "deferred": "qualify-low-confidence"}, snapshot)
        except standing.Refusal as error:
            return None, error.code
        if result["outcome"] == "qualified":
            return None, "recoverable-without-model"
        if result["reasons"] != ["insufficient-inspected-scope"]:
            return None, "standing-%s-%s" % (result["outcome"], result["reasons"][0])
        return ("insufficient-inspected-scope" if code == UNRESOLVED else "current-policy-deferred"), None
    return None, code


def _stale(c, args):
    """True when the docs table names a different current rendition for this receipt."""
    receipt = Path(args["rendition"]).parts[-3]
    known = {sha for (sha,) in c.execute("SELECT rendition_sha256 FROM docs WHERE receipt_id=?", (receipt,))}
    return bool(known) and args["rendition_sha256"] not in known


def select(root, c, policy_sha, include_current_deferred=False, include_older_rejected=False):
    """``(selected, skipped)`` from the ledger on connection ``c``; reads, never writes.

    Chain heads are grouped by rendition. A rendition settled under any chain (reviewed and
    selected), rejected or refused as out of scope under any chain is skipped. Otherwise, when any
    head is pinned under the current policy only those decide: a head whose review or selection is
    still unfinished is resumed without a model call, and a current-policy review whose saved
    findings are re-evaluated (``unresolved-evidence``, and deferrals when included) is selected
    only when its inspection was incomplete. With ``include_older_rejected``, a rendition rejected
    only under older policies is not skipped: without a current-policy review it is selected
    (``older-policy-rejected``); with one, that review decides as above.
    """
    snapshot = adapters.OneRead(root)
    groups = {}
    for head in _heads(c):
        groups.setdefault((head["args"]["rendition"], head["args"]["rendition_sha256"]), []).append(head)
    with_docs = docs.present(c)
    selected, skipped = [], Counter()
    for _, every in sorted(groups.items()):
        codes = {h["code"] or "" for h in every}
        heads = sorted([h for h in every if h["args"]["policy_sha256"] == policy_sha] or every,
                       key=lambda h: h["corr"])
        # A succeeded review settles its rendition, except that a current-policy one whose
        # selection is unfinished (interrupted, or the manifest was full) is resumed.
        reviewed = [h for h in every if h["review"] == "succeeded"]
        selecting = [h for h in reviewed if h["select"] in cohort_review.RUNNABLE | {"blocked"}
                     and h["args"]["policy_sha256"] == policy_sha]
        rejected = [h for h in every if (h["code"] or "").startswith("standing-rejected-")]
        current = any(h["args"]["policy_sha256"] == policy_sha for h in every)
        reconsidered = (include_older_rejected and rejected
                        and all(h["args"]["policy_sha256"] != policy_sha for h in rejected))
        if reviewed and len(selecting) < len(reviewed):
            skipped["reviewed"] += 1
        elif selecting:
            selected.append({**selecting[0], "reason": "resume"})
        elif rejected and not reconsidered:
            skipped["rejected"] += 1
        elif reconsidered and not current:
            head = sorted(rejected, key=lambda h: h["corr"])[-1]
            if with_docs and _stale(c, head["args"]):
                skipped["stale-rendition"] += 1
            else:
                selected.append({**head, "reason": "older-policy-rejected"})
        elif "standing-exception-prohibited-scope" in codes:
            skipped["out-of-scope"] += 1
        elif with_docs and _stale(c, heads[0]["args"]):
            skipped["stale-rendition"] += 1
        elif runnable := [h for h in heads if h["review"] in cohort_review.RUNNABLE]:
            selected.append({**runnable[0], "reason": "resume"})
        else:
            reason, skip = _reason(root, heads[0], policy_sha, include_current_deferred, snapshot)
            if reason is None:
                skipped[skip or "unknown"] += 1
            else:
                selected.append({**heads[0], "reason": reason})
    return selected, dict(skipped)


def _entry(head):
    args = head["args"]
    return {"submission_id": head["submission_id"], "rendition": args["rendition"],
            "rendition_sha256": args["rendition_sha256"]}


def _check(root, head, standing_policy, inspected_form):
    """``(model calls, prompt chars)`` of a head's review under the current policy, or the reason no
    call is made. A rendition too large for one prompt or one budget is reviewed in parts: one call each."""
    args = head["args"]
    try:
        _, value, failure = standing._rendition(root, args, index.rendition_api(root))
    except standing.Refusal as error:
        return None, error.code
    except OSError:
        return None, "rendition-unreadable"
    if failure is not None:
        return None, failure
    if not standing.permits(standing_policy["scope"], value["source"]["receipt_id"], value["source"]["audience"]):
        return None, "out-of-scope"
    budget = standing_policy["budget"]
    try:
        planned = model_review.plan(root, args["rendition"], inspected_form, standing.criteria(standing_policy),
                                    max_records=budget["max_records"], max_pages=budget["max_pages"])
    except (OSError, ValueError, KeyError):
        return None, "rendition-unreadable"
    if planned["status"]:
        return None, planned["status"]
    if head["reason"] == "budget-exceeded" and not planned["parted"]:
        return None, "not-over-budget"  # the findings, not the rendition, were over budget
    return (len(planned["tasks"]), planned["chars"]), None


def _receipt(head):
    return Path(head["args"]["rendition"]).parts[-3]


def _per_document(cost_estimate, model):
    """Per-document tokens for ``model`` from a ``model_review.estimate`` file, or None."""
    if cost_estimate is None:
        return None
    try:
        per = json.loads(Path(cost_estimate).read_text(encoding="utf-8"))[model]["per_document"]
        return {key: int(per[key]) for key in ("prompt_tokens", "completion_tokens")}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise IntakeError("cost_estimate_invalid") from error


def plan(root, selected, skipped, standing_policy, *, limit=None, inspected_form="list",
         model=None, cost_estimate=None, receipts=None, max_parts=None):
    """The dry-run report for ``select``'s result, and the heads an applied run works through.

    Reads each rendition to be reviewed (never the ledger): one the current policy would not
    review (out of scope, over budget, too large for a prompt, stale) is skipped without a call.
    A rendition reviewed in parts costs one call per part (``model_calls``; ``documents`` counts the
    renditions). ``limit`` bounds the renditions reviewed, so a part-reviewed one is never started
    and left half done; resumed heads cost none and are always included. ``receipts`` (a set of receipt
    IDs) keeps only those heads (``not-in-receipts`` for the rest); ``max_parts`` skips a rendition that
    would need more calls than that as ``parts-deferred``, for a separately approved run.
    """
    skipped = Counter(skipped)
    work, chars, calls, held = [], 0, 0, Counter()
    for head in selected:
        if receipts is not None and _receipt(head) not in receipts:
            skipped["not-in-receipts"] += 1
            continue
        if head["reason"] != "resume":
            size, reason = _check(root, head, standing_policy, inspected_form)
            if reason is None and max_parts is not None and size[0] > max_parts:
                reason = "parts-deferred"
            if reason is not None:
                skipped[reason] += 1
                continue
            if limit is not None and sum(h["reason"] != "resume" for h in work) >= limit:
                held[head["reason"]] += 1
                continue
            calls += size[0]
            chars += size[1]
        work.append(head)
    documents = sum(h["reason"] != "resume" for h in work)
    estimate = {"documents": documents, "model_calls": calls, "prompt_chars": chars,
                "prompt_tokens_approx": math.ceil(chars / CHARS_PER_TOKEN)}
    per = _per_document(cost_estimate, model)
    if per is not None:
        estimate["by_count"] = {key: value * calls for key, value in per.items()}
    report = {"apply": False, "selected": dict(Counter(h["reason"] for h in work)),
              "skipped": dict(skipped), "estimate": estimate}
    if held:
        report["over_limit"] = dict(held)
    return report, work


def _settle(root, head, run, prefetched, results):
    """Review (unless resumed) and stage one head as a correction, then run and select it."""
    entry = _entry(head)
    base = "standing-" + cohort_review._standing_token(entry, run["policy_sha"])
    current, event_id = None, head["args"]["event_id"]
    if head["reason"] != "resume":
        if prefetched is None and cohort_review._held_back(root, entry, run["policy"], run["manifest"]):
            results["selection-capacity"] += 1
            return
        reviewed = prefetched() if prefetched is not None else cohort_review._review(root, entry, run)
        run["usage"].update(reviewed["usage"])
        if reviewed["status"] != "reviewed":
            results[reviewed["status"]] += 1
            return
        if store.find_head(root, head["corr"], run["heads"])["correlation_id"] != head["corr"]:
            results["head-moved"] += 1  # another writer corrected it meanwhile
            return
        # A head pinned under the current policy is corrected under its own name; an older one by
        # the request ``qualify`` would stage for the current policy, which does not exist yet.
        older = head["args"]["policy_sha256"] != run["policy_sha"]
        if older and store.find(root, base) is not None:
            results["head-moved"] += 1
            return
        event_id = "rev-" + base[len("standing-"):]
        name = base if older else store.next_correction(root, base)
        current = cohort_review._stage_review(root, entry, run, name, event_id, reviewed,
                                              correction_of=head["corr"])
    else:
        current = store.inspect(root, head["corr"])
    active, current = cohort_review._run_review(root, base, current, entry, run["deferred"])
    current = cohort_review._run_select(root, active, current, entry, run["manifest"])
    if current is None:
        results["selection-capacity"] += 1
        return
    cohort_review._conclude(entry, current, event_id)
    outcome = entry["qualification"]
    if outcome == "qualified" and head["reason"] != "resume" and reviewed["decision"] == "defer":
        outcome = "qualified-low-confidence"
    results[outcome] += 1


def apply(root, work, run, concurrency, *, adaptive=True, initial_concurrency=None):
    """Work through ``work`` with up to ``concurrency`` model calls in flight; return the report part."""
    transport = run["transport"]
    retries = getattr(transport, "retries", None)
    retried = Counter(retries) if retries is not None else None
    limiter = throttle.Limiter(concurrency, initial=initial_concurrency, adaptive=adaptive)
    results = Counter()
    run = {**run, "heads": store.Heads()}
    with limiter.gating(transport) as gated:
        run["transport"] = gated

        def ahead(head, waiting):
            return (head["reason"] != "resume"
                    and cohort_review._free_room(root, run["manifest"]) > waiting)
        cohort_review.pipeline(work, concurrency, ahead,
                               lambda head: cohort_review._review(root, _entry(head), run),
                               lambda head, prefetched: _settle(root, head, run, prefetched, results))
    report = {"results": dict(results),
              "usage": {k: run["usage"][k] for k in ("prompt_tokens", "completion_tokens")},
              "throughput": {**limiter.report(), "inspected_form": run["inspected_form"]}}
    if retries is not None:
        report["retries"] = {code: n for code, n in sorted((Counter(retries) - retried).items())}
    return report
