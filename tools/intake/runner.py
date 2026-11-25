"""Explicit single-stage execution, resumable attempts and safe replay."""
from contextlib import ExitStack, contextmanager
import errno
import os
import socket
import sqlite3
import threading
import time
import uuid

from tools import policy
from . import adapters, docs, store
from .contracts import IntakeError, LedgerBusy, canonical, decode, decode_request, digest, require


def selected(c, correlation, stage_id):
    request = c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation,)).fetchone()
    require(request is not None, "unknown_request")
    request = decode_request(request[0])
    saved = c.execute("SELECT * FROM stages WHERE correlation_id=? AND stage_id=?", (correlation, stage_id)).fetchone()
    require(saved is not None, "unknown_stage")
    require(not c.execute("SELECT 1 FROM stages WHERE correlation_id=? AND ordinal<? AND status!='succeeded'", (correlation, saved["ordinal"])).fetchone(), "prior_stage_pending")
    return request, request["stages"][saved["ordinal"]], saved


def save_artifact(c, correlation, stage_id, key, value):
    require(key in {"intent", "evidence"} and isinstance(value, dict), "invalid_artifact")
    raw = canonical(value)
    require(len(raw.encode()) <= 1024 * 1024, "artifact_budget")
    c.execute(f"UPDATE stages SET {key}=?, {key}_sha256=? WHERE correlation_id=? AND stage_id=?", (raw, digest(raw.encode()), correlation, stage_id))


# Retryable outcomes of a lost catalog race: another writer held the lock for the whole wait or
# committed after the pin. Nothing was written, like a busy ledger, so they are not input faults.
CONTENTION_CODES = {"catalog-busy", "catalog-stale-state"}
# Lost catalog races one stage records per cycle before it dead-letters (explicit replay or
# ``recover-conflicts`` resumes it): 5 minutes of lost 10 s waits, so a lock stuck for good
# cannot grow the ledger or retry forever.
CONTENTION_BUDGET = 30
# Consecutive lost catalog races one ``settle`` call retries before it leaves the stage retryable.
# Each already waited the catalog's bounded wait, so a call spends at most this many waits (60 s,
# which covers the 58 s first-of-day hold measured at 100k registrations before the first-write-of-day replay was made cheaper; see "Concurrent writers" in docs/design/current-state.md).
CONTENTION_RETRIES = 6


def contention(failure):
    return failure.category == "retryable" and failure.code in CONTENTION_CODES


def lost_race(attempt):
    return attempt["outcome"] == "failed" and attempt["category"] == "retryable" and attempt["code"] in CONTENTION_CODES


def spent(attempts):
    """(charged, lost): attempts counting against ``max_attempts``, and lost catalog races."""
    lost = sum(lost_race(a) for a in attempts)
    return len(attempts) - lost, lost


def classify(error):
    if isinstance(error, adapters.StageFailure):
        return error
    if isinstance(error, PermissionError):
        return adapters.StageFailure("blocked", "permission")
    if isinstance(error, OSError):
        category = "retryable" if error.errno in {errno.EAGAIN, errno.EBUSY, errno.ENOSPC, errno.EIO, errno.ETIMEDOUT, errno.EINTR} else "blocked"
        return adapters.StageFailure(category, "os-error")
    if isinstance(error, sqlite3.OperationalError):
        return adapters.StageFailure("retryable", "sqlite-operational")
    return adapters.StageFailure("permanent", "invalid-artifact")


def failure_status(request, attempts, ordinal, error):
    """``(failure, stage status)`` a failed attempt records; the one rule for single and batch runs.

    A lost catalog race wrote nothing: it is charged to its own bounded budget, not to the fault
    budget (each recorded race is one bounded wait).
    """
    failure = classify(error)
    if isinstance(error, IntakeError):
        failure = adapters.StageFailure("retryable" if error.retryable else "blocked", "policy-changed" if error.code == "policy_changed" else error.code.replace("_", "-"))
    charged, lost = spent([a for a in attempts if a["ordinal"] != ordinal])
    charged, lost = (charged, lost + 1) if contention(failure) else (charged + 1, lost)
    status = ("blocked" if failure.category == "blocked" else "dead-letter"
              if failure.category == "permanent" or charged >= request["retry"]["max_attempts"]
              or lost >= CONTENTION_BUDGET else "retryable")
    return failure, status


# Claim, work, record (Kata 527a). A stage run commits its intent, a running attempt and a claim (owner and
# lease) in one ledger commit, releases the ledger lock, does the native work, and retakes the lock only to
# record the evidence and drop the claim. So a ``progress`` read, a second ``ingest`` or a ``qualify`` run is
# not queued behind extraction work, and "one attempt per stage" still holds: a process skips a stage whose
# claim is live and takes over one whose claim expired or whose owner process is gone, reusing the unfinished
# attempt without charging it. A lease race can at worst run an effect twice (its markers and request IDs make
# that idempotent), never record twice: the evidence is written only while the claim is still the writer's.
LEASE_SECONDS = 900
# A run in native work renews its lease this often (a third of it), so a chunk whose work outlasts one lease
# is not taken over while its owner is alive; a process that stops, or whose host dies, stops renewing.
HEARTBEAT_SECONDS = LEASE_SECONDS / 3
# Operations that keep the ledger lock for the whole run. ``standing-select`` writes the shared selection
# manifest, whose writers exclude each other through the ledger lock; ``standing-publish`` and ``index-build`` must
# see the manifest they pinned; ``standing-review`` and ``review-append`` commit review events against a pinned
# store revision, which another qualify process would otherwise stale. Each is model-free and short (the model call
# is made before the stage, outside the ledger), so the lock costs little there; ``events.append`` is timed in
# ``tests/test_intake_claims.py`` and a stale-revision retry is the follow-up that would let them release it too.
LOCKED_OPERATIONS = {"standing-review", "standing-select", "standing-publish", "index-build", "review-append"}
NATIVE_ERRORS = (adapters.StageFailure, OSError, sqlite3.Error, IntakeError)


def owner_id():
    """A new claim owner: host, process and a nonce for this run (so two runs of one process differ)."""
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:12]}"


_active, _active_lock = set(), threading.Lock()


@contextmanager
def running(owner):
    """Mark ``owner`` as a run in progress in this process for as long as the block runs."""
    with _active_lock:
        _active.add(owner)
    try:
        yield
    finally:
        with _active_lock:
            _active.discard(owner)


def owner_alive(owner):
    """False for a claim whose owner is gone: a process of this host that no longer exists, or a run of this
    process that has ended without recording (a ledger that stayed busy, say). Another host's claim is
    judged by its lease alone."""
    host, _, rest = owner.partition("/")
    pid = rest.partition("/")[0]
    if host != socket.gethostname() or not pid.isdigit():
        return True
    if int(pid) == os.getpid():
        with _active_lock:
            return owner in _active
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def claim_live(claim, now):
    return claim is not None and claim["lease_until"] > now and owner_alive(claim["owner"])


def position(c, correlation_id, stage_id, request, saved, started):
    """``(attempts of the cycle, unfinished attempt or None, ordinal to work on, refusal code or None)``.

    The one rule ``run`` and ``run_batch`` share for when a stage may be worked: an unfinished attempt is
    resumed without charge; otherwise the backoff must have passed and the attempt budgets must have room.
    """
    attempts = c.execute("SELECT * FROM attempts WHERE correlation_id=? AND stage_id=? AND cycle=? ORDER BY ordinal",
                         (correlation_id, stage_id, saved["cycle"])).fetchall()
    unfinished = attempts[-1] if attempts and attempts[-1]["outcome"] is None else None
    ordinal = unfinished["ordinal"] if unfinished else len(attempts) + 1
    if unfinished is not None:
        return attempts, unfinished, ordinal, None
    if attempts and started < attempts[-1]["eligible_at"]:
        return attempts, unfinished, ordinal, "backoff_pending"
    charged, lost = spent(attempts)
    if not (charged < request["retry"]["max_attempts"] and lost < CONTENTION_BUDGET):
        return attempts, unfinished, ordinal, "attempt_limit"
    return attempts, unfinished, ordinal, None


def look(c, correlation_id, stage_id, started, owner):
    """``(turn, reason)``: the stage as it stands under the lock, or why it is not to be worked now.

    ``reason`` is ``None`` (``turn`` is the state to work from), ``"settled"`` (nothing left to run),
    ``"held"`` (another process holds a live claim), or the refusal code ``run`` raises.
    """
    request, stage, saved = selected(c, correlation_id, stage_id)
    if saved["status"] in {"succeeded", "return-pending"}:
        return None, "settled"
    if saved["status"] in {"blocked", "dead-letter"}:
        return None, "explicit_replay_required"
    attempts, unfinished, ordinal, refusal = position(c, correlation_id, stage_id, request, saved, started)
    if refusal is not None:
        return None, refusal
    if unfinished is not None and stage["operation"] not in LOCKED_OPERATIONS:
        claim = c.execute("SELECT * FROM claims WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id)).fetchone()
        if claim_live(claim, started) and claim["owner"] != owner:
            return None, "held"
    return {"correlation_id": correlation_id, "stage_id": stage_id, "request": request, "stage": stage,
            "saved": saved, "attempts": attempts, "unfinished": unfinished, "ordinal": ordinal,
            "claimed": stage["operation"] not in LOCKED_OPERATIONS}, None


def owns(c, turn, owner):
    """True while the claim on the turn's attempt is still this run's (nobody took the stage over)."""
    row = c.execute("SELECT owner, cycle, ordinal FROM claims WHERE correlation_id=? AND stage_id=?",
                    (turn["correlation_id"], turn["stage_id"])).fetchone()
    return row is not None and (row[0], row[1], row[2]) == (owner, turn["saved"]["cycle"], turn["ordinal"])


def lease_end(started, since):
    """The end of a lease taken now: the run's clock at ``started`` plus the time elapsed since, plus a lease.

    Measured from the claim, not from the start of the run, whatever the injected clock is (a long preparation
    must not use up the lease before it exists).
    """
    return started + (time.monotonic() - since) + LEASE_SECONDS


@contextmanager
def heartbeat(root, turns, owner, started, since):
    """Renew the claims of ``turns`` every ``HEARTBEAT_SECONDS`` while the block runs (their native work).

    A renewal only extends a claim that is still this owner's, so it never resurrects a settled or taken-over
    one, and a ledger that is busy or unavailable skips that beat (the lease is three beats long).
    """
    turns = [turn for turn in turns if turn["claimed"]]
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with store.locked(root, wait=5) as c, c:
                    for turn in turns:
                        c.execute("UPDATE claims SET lease_until=? WHERE correlation_id=? AND stage_id=? AND owner=?",
                                  (lease_end(started, since), turn["correlation_id"], turn["stage_id"], owner))
            except Exception:  # noqa: BLE001 - a missed beat is covered by the rest of the lease
                pass
    thread = threading.Thread(target=beat, name="intake-claim-heartbeat", daemon=True)
    if turns:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if turns:
            thread.join()


def begin(c, turn, intent, owner, started, since):
    """Save the new ``intent``, record the attempt as running and take the claim in one commit (the intent is
    written first, so it is durable whenever the attempt is). The caller's transaction commits it."""
    if intent is not None:
        save_artifact(c, turn["correlation_id"], turn["stage_id"], "intent", intent)
    if turn["unfinished"] is None:
        c.execute("INSERT INTO attempts(correlation_id,stage_id,cycle,ordinal,started) VALUES (?,?,?,?,?)",
                  (turn["correlation_id"], turn["stage_id"], turn["saved"]["cycle"], turn["ordinal"], started))
        c.execute("UPDATE stages SET status='running' WHERE correlation_id=? AND stage_id=?",
                  (turn["correlation_id"], turn["stage_id"]))
        docs.refresh(c, turn["correlation_id"])
    if turn["claimed"]:
        c.execute("INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?,?)",
                  (turn["correlation_id"], turn["stage_id"], turn["saved"]["cycle"], turn["ordinal"],
                   owner, lease_end(started, since)))


def record_failure(c, turn, error, started, ended):
    """Record a failed attempt (inserting it when none was begun) and drop any claim, in the caller's transaction."""
    correlation_id, stage_id, saved = turn["correlation_id"], turn["stage_id"], turn["saved"]
    failure, status = failure_status(turn["request"], turn["attempts"], turn["ordinal"], error)
    if turn["unfinished"] is None and c.execute(
            "SELECT 1 FROM attempts WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?",
            (correlation_id, stage_id, saved["cycle"], turn["ordinal"])).fetchone() is None:
        c.execute("INSERT INTO attempts(correlation_id,stage_id,cycle,ordinal,started) VALUES (?,?,?,?,?)",
                  (correlation_id, stage_id, saved["cycle"], turn["ordinal"], started))
    c.execute("UPDATE attempts SET ended=?,outcome='failed',category=?,code=?,eligible_at=? WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?",
              (ended, failure.category, failure.code, ended + turn["request"]["retry"]["backoff_seconds"],
               correlation_id, stage_id, saved["cycle"], turn["ordinal"]))
    c.execute("UPDATE stages SET status=? WHERE correlation_id=? AND stage_id=?", (status, correlation_id, stage_id))
    c.execute("DELETE FROM claims WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id))
    docs.refresh(c, correlation_id)


def record_success(c, turn, evidence, status, ended):
    """Record the evidence and settle the attempt and stage, dropping the claim, in the caller's transaction."""
    correlation_id, stage_id, saved = turn["correlation_id"], turn["stage_id"], turn["saved"]
    save_artifact(c, correlation_id, stage_id, "evidence", evidence)
    c.execute("UPDATE attempts SET ended=?,outcome=?,eligible_at=? WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?",
              (ended, status, ended, correlation_id, stage_id, saved["cycle"], turn["ordinal"]))
    c.execute("UPDATE stages SET status=? WHERE correlation_id=? AND stage_id=?", (status, correlation_id, stage_id))
    c.execute("DELETE FROM claims WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id))
    docs.refresh(c, correlation_id)


def release(root, turns, owner):
    """Drop this run's claims after a failure nothing recorded (best effort: a lease expires anyway), so the
    next run resumes the unfinished attempt at once."""
    try:
        with store.locked(root, wait=5) as c, c:
            for turn in turns:
                if turn["claimed"]:
                    c.execute("DELETE FROM claims WHERE correlation_id=? AND stage_id=? AND owner=?",
                              (turn["correlation_id"], turn["stage_id"], owner))
    except Exception:  # noqa: BLE001 - the original failure is what the caller must see
        pass


def pinned_policy(root, turn):
    """The failure when the instance policy is no longer the request's, else ``None``."""
    try:
        require(policy.for_root(root)[1] == turn["request"]["policy"]["sha256"], "policy_changed")
    except NATIVE_ERRORS as error:
        return error
    return None


def reconcile(root, turn, intent, error, prepared_unpinned):
    """``(intent, error)`` to run under after the second look.

    A run that prepared because no intent was saved may find, under the lock, that another run pinned one meanwhile:
    that saved intent decides (the stage runs under what is pinned, never under a rival preparation), the
    preparation's own failure is moot, and the policy is checked again.
    """
    if prepared_unpinned and turn["saved"]["intent"]:
        return decode(turn["saved"]["intent"].encode()), pinned_policy(root, turn)
    return intent, error


def guarded(root, turn):
    """``(intent, error)``: the stage's pinned intent (saved, else freshly prepared) or the failure that refused it."""
    try:
        require(policy.for_root(root)[1] == turn["request"]["policy"]["sha256"], "policy_changed")
        saved = turn["saved"]
        return (decode(saved["intent"].encode()) if saved["intent"] else adapters.prepare(store.safe(root), turn["stage"])), None
    except NATIVE_ERRORS as error:
        return None, error


def run(root, correlation_id, stage_id, *, now=None, clock=None):
    # Explicit now retains the fixed-time interface used by existing callers.
    clock = clock if clock is not None else time.time if now is None else lambda: now
    started, since, owner = clock(), time.monotonic(), owner_id()
    with running(owner), ExitStack() as stack:
        c = stack.enter_context(store.locked(root))
        turn, reason = look(c, correlation_id, stage_id, started, owner)
        if turn is None:
            require(reason in {"settled", "held"}, reason)
            return store.summary(c, correlation_id)
        if turn["claimed"] and not turn["saved"]["intent"]:
            # Preparing a stage reads and parses its source: do it without the ledger lock, then look again
            # under the lock, since another process may have claimed or pinned the stage meanwhile.
            stack.close()
            intent, error = guarded(root, turn)
            c = stack.enter_context(store.locked(root))
            turn, reason = look(c, correlation_id, stage_id, started, owner)
            if turn is None:
                require(reason in {"settled", "held"}, reason)
                return store.summary(c, correlation_id)
            intent, error = reconcile(root, turn, intent, error, True)
        else:
            intent, error = guarded(root, turn)
        if error is not None:
            with c:
                record_failure(c, turn, error, started, clock())
            return store.summary(c, correlation_id)
        with c:
            begin(c, turn, intent if turn["saved"]["intent"] is None else None, owner, started, since)
        saved = turn["saved"]
        previous = decode(saved["evidence"].encode()) if saved["evidence"] else None
        if turn["claimed"]:
            stack.close()  # the ledger lock is free during the effect
        try:
            with heartbeat(root, [turn], owner, started, since):
                evidence, error = adapters.execute(store.safe(root), turn["stage"], intent, previous), None
        except NATIVE_ERRORS as failure:
            evidence, error = None, failure
        except BaseException:
            if turn["claimed"]:
                release(root, [turn], owner)
            raise
        if turn["claimed"]:
            c = stack.enter_context(store.locked(root))
            if not owns(c, turn, owner):
                # Another process took the stage over after this lease ran out: it decides the stage now.
                return store.summary(c, correlation_id)
        with c:
            if error is not None:
                record_failure(c, turn, error, started, clock())
            else:
                record_success(c, turn, evidence, "return-pending" if turn["stage"]["kind"] == "return" else "succeeded", clock())
        return store.summary(c, correlation_id)


def run_batch(root, items, *, clock=None):
    """``run`` for many single-stage requests of one batchable operation, committing their catalog work once.

    ``items`` are ``(correlation_id, stage_id)``. Returns ``{correlation_id: summary}``. Each stage keeps
    its own attempt, intent, evidence and status exactly as ``run`` records them, and one stage's failure
    is recorded on that stage alone. A stage that is blocked, dead-lettered, in backoff, out of
    attempts or claimed by another process is left as it is. The ledger sees three short lock holds for the
    whole batch (look, then intents, attempts and claims, then evidence); the native work between them holds no
    ledger lock, and the catalog sees one revision. A crash anywhere leaves unfinished attempts that the next
    call takes over without charging them; a stage whose registration committed is found committed by its
    marker and its evidence is recorded without a second write.
    """
    clock = clock if clock is not None else time.time
    started, since, owner = clock(), time.monotonic(), owner_id()
    with running(owner):
        with store.locked(root) as c:
            live = []
            for correlation_id, stage_id in items:
                turn, _ = look(c, correlation_id, stage_id, started, owner)
                if turn is not None:
                    turn.update(error=None, intent=None, recorded=False, unpinned=not turn["saved"]["intent"])
                    live.append(turn)
            operations = {turn["stage"]["operation"] for turn in live}
            require(len(operations) <= 1 and not operations & LOCKED_OPERATIONS, "unsupported_batch")
            if not live:
                return {correlation_id: store.summary(c, correlation_id) for correlation_id, _ in items}
        with adapters.batch_scope(next(iter(operations), None), len(live)):
            for turn in live:
                turn["intent"], turn["error"] = guarded(root, turn)
            with store.locked(root) as c:
                current = []
                for turn in live:
                    again, _ = look(c, turn["correlation_id"], turn["stage_id"], started, owner)
                    if again is not None:  # not claimed by another process since the first look
                        intent, error = reconcile(root, again, turn["intent"], turn["error"], turn["unpinned"])
                        again.update(error=error, intent=intent, recorded=False)
                        current.append(again)
                live = current
                with c:
                    for turn in live:
                        if turn["error"] is not None:
                            record_failure(c, turn, turn["error"], started, clock())
                            turn["recorded"] = True
                        else:
                            begin(c, turn, turn["intent"] if turn["saved"]["intent"] is None else None, owner, started, since)
            ready = [turn for turn in live if turn["error"] is None]
            if ready:
                try:
                    with heartbeat(root, ready, owner, started, since):
                        outcomes = adapters.execute_batch(store.safe(root), [
                            (turn["stage"], turn["intent"], decode(turn["saved"]["evidence"].encode()) if turn["saved"]["evidence"] else None)
                            for turn in ready])
                except BaseException:
                    release(root, ready, owner)
                    raise
                for turn, outcome in zip(ready, outcomes):
                    if isinstance(outcome, Exception):
                        turn["error"] = outcome
                    else:
                        turn["evidence"] = outcome
        ended = clock()
        with store.locked(root) as c:
            with c:
                for turn in live:
                    if turn["recorded"] or not owns(c, turn, owner):
                        continue  # recorded with its intent, or taken over by another process after the lease ran out
                    if turn["error"] is not None:
                        record_failure(c, turn, turn["error"], started, ended)
                    else:
                        record_success(c, turn, turn["evidence"], "succeeded", ended)
            return {correlation_id: store.summary(c, correlation_id) for correlation_id, _ in items}


def settle_batch(root, items):
    """``run_batch`` retried while some stage lost a catalog race, like ``settle`` for one stage.

    Returns ``{correlation_id: summary}``. A busy ledger raises ``LedgerBusy`` after the same bounded
    retries ``settle`` makes; nothing was written then.
    """
    busy = lost = 0
    result = {}
    while True:
        try:
            summaries = run_batch(root, items)
        except LedgerBusy:
            busy += 1
            if busy >= BUSY_RETRIES:
                raise
            continue
        result.update(summaries)
        retry = []
        for correlation_id, stage_id in items:
            stage = next(s for s in summaries[correlation_id]["stages"] if s["id"] == stage_id)
            current = [a for a in stage["attempts"] if a["cycle"] == stage["cycle"]]
            if stage["status"] == "retryable" and current and current[-1]["code"] in CONTENTION_CODES:
                retry.append((correlation_id, stage_id))
        if not retry:
            return result
        lost += 1
        if lost >= CONTENTION_RETRIES:
            return result
        items = retry


def settle(root, correlation_id, stage_id):
    """Run a stage and retry it while it is retryable, within its attempt budget.

    Concurrent catalog writers surface as retryable stale-state or busy attempts; each retry
    re-reads the catalog, so the stage settles without manual replay. Those attempts write
    nothing, so they do not spend the fault budget; one call retries at most
    ``CONTENTION_RETRIES`` of them in a row and then leaves the stage retryable.
    """
    busy = lost = 0
    while True:
        try:
            result = run(root, correlation_id, stage_id)
        except LedgerBusy:
            # Nothing was written, so a busy ledger is retried within the stage's attempt
            # budget (read from the ledger, which may itself be busy); then it surfaces.
            busy += 1
            if busy >= BUSY_RETRIES:
                raise
            continue
        except IntakeError as error:
            if error.code != "backoff_pending":
                raise
            result = store.inspect(root, correlation_id)
            stage = next(s for s in result["stages"] if s["id"] == stage_id)
            current = [a for a in stage["attempts"] if a["cycle"] == stage["cycle"]]
            # Honour the request's backoff outside the ledger lock; a long backoff
            # leaves the stage visibly retryable instead of stalling the caller.
            wait = current[-1]["eligible_at"] - time.time()
            if wait > SETTLE_MAX_WAIT:
                return result
            time.sleep(max(wait, 0))
            continue
        stage = next(s for s in result["stages"] if s["id"] == stage_id)
        if stage["status"] != "retryable":
            return result
        current = [a for a in stage["attempts"] if a["cycle"] == stage["cycle"]]
        if current[-1]["code"] in CONTENTION_CODES:
            # Each lost race already waited the catalog's bounded wait; leave a still-contended
            # stage visibly retryable for the next run rather than waiting on without limit.
            lost += 1
            if lost >= CONTENTION_RETRIES:
                return result
        else:
            lost = 0


SETTLE_MAX_WAIT = 60
# Consecutive ledger-busy outcomes (each already a full bounded lock wait) before settle gives up.
BUSY_RETRIES = 3


def conflicts(stage):
    """Count attempts of this stage that lost a race with another catalog writer."""
    return sum(a["code"] in CONFLICT_CODES for a in stage["attempts"])


CONFLICT_CODES = {"catalog-stale-state", "catalog-busy", "sqlite-operational"}


def replay(root, correlation_id, stage_id, *, now=None):
    now = time.time() if now is None else now
    with store.locked(root) as c:
        _, _, saved = selected(c, correlation_id, stage_id)
        require(saved["status"] in {"blocked", "dead-letter"}, "not_replayable")
        cycle = saved["cycle"] + 1
        with c:
            c.execute("INSERT INTO replays VALUES (?,?,?,?)", (correlation_id, stage_id, cycle, now))
            c.execute("UPDATE stages SET status='staged',cycle=? WHERE correlation_id=? AND stage_id=?", (cycle, correlation_id, stage_id))
            docs.refresh(c, correlation_id)
        return store.summary(c, correlation_id)


def acknowledge(root, correlation_id, stage_id, acknowledgment):
    from .contracts import fields, sha, text
    fields(acknowledgment, {"delivery_correlation", "disposition_sha256", "transport_receipt"})
    text(acknowledgment["delivery_correlation"], 500)
    sha(acknowledgment["disposition_sha256"])
    text(acknowledgment["transport_receipt"], 500)
    with store.locked(root) as c:
        request, stage, saved = selected(c, correlation_id, stage_id)
        require(stage["kind"] == "return" and saved["status"] in {"return-pending", "succeeded"}, "not_awaiting_ack")
        raw = canonical(acknowledgment)
        prior = c.execute("SELECT payload FROM acknowledgments WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id)).fetchone()
        if prior:
            require(prior[0] == raw, "acknowledgment_conflict")
            return store.summary(c, correlation_id)
        require(policy.for_root(root)[1] == request["policy"]["sha256"], "policy_changed")
        intent = decode(saved["intent"].encode())
        try:
            require(adapters.prepare(store.safe(root), stage) == intent, "changed_disposition")
        except (OSError, adapters.StageFailure) as error:
            raise IntakeError("disposition_unavailable") from error
        require(all(acknowledgment[k] == intent[k] for k in ("delivery_correlation", "disposition_sha256")), "acknowledgment_mismatch")
        with c:
            c.execute("INSERT INTO acknowledgments VALUES (?,?,?,?)", (correlation_id, stage_id, raw, digest(raw.encode())))
            save_artifact(c, correlation_id, stage_id, "evidence", {"boundary": "delivered", "acknowledgment": acknowledgment, "basis": "explicit-transport-acknowledgment"})
            c.execute("UPDATE stages SET status='succeeded' WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id))
            docs.refresh(c, correlation_id)
        return store.summary(c, correlation_id)


REGISTER_OPERATIONS = {"catalog-submit", "extract-register", "docling-register"}
# Legacy concurrent-writer failures were recorded as native-input before
# catalog-stale-state and catalog-busy existed.
RECOVERABLE_CODES = {"native-input", "catalog-stale-state", "catalog-busy", "sqlite-operational"}


def written_ids(root, stage):
    """Pre-existing records a registration stage rewrites (new rendition records cannot overlap)."""
    args = stage["arguments"]
    if stage["operation"] == "catalog-submit":
        return [r["id"] for r in args["records"]]
    from tools.catalog import catalog_store as catalog
    try:
        return [catalog.receipt_metadata(store.safe(root), [args["receipt"]])
                ["registrations"][args["receipt"]]["artifact"]["id"]]
    except (catalog.CatalogError, KeyError):
        return None  # unresolvable receipt: never replay blindly


def conflict_candidates(root):
    """Blocked or dead-letter registration stages whose failure was a writer race.

    Skips requests already corrected by another request; a catalog-submit is
    included only when its records would rebase onto the current catalog.
    """
    from tools.catalog import catalog_store as catalog
    tokens, last_writer = catalog.rebase_index(store.safe(root))
    found, skipped = [], {}
    with store.locked(root) as c:
        rows = c.execute("SELECT s.correlation_id, s.stage_id, s.ordinal, s.status, s.cycle, r.raw "
                         "FROM stages s JOIN requests r USING(correlation_id) "
                         "WHERE s.status IN ('blocked','dead-letter') ORDER BY s.correlation_id").fetchall()
        for row in rows:
            stage = decode_request(row["raw"])["stages"][row["ordinal"]]
            if stage["operation"] not in REGISTER_OPERATIONS:
                continue
            last = c.execute("SELECT code FROM attempts WHERE correlation_id=? AND stage_id=? AND cycle=? "
                             "ORDER BY ordinal DESC LIMIT 1",
                             (row["correlation_id"], row["stage_id"], row["cycle"])).fetchone()
            reason = None
            if last is None or last[0] not in RECOVERABLE_CODES:
                continue
            if c.execute("SELECT 1 FROM requests WHERE correction_of=?", (row["correlation_id"],)).fetchone():
                reason = "corrected"
            else:
                args = stage["arguments"]
                pinned = tokens.get(args["expected_state"])
                ids = written_ids(root, stage)
                if pinned is None or ids is None or any(last_writer.get(i, 0) > pinned for i in ids):
                    reason = "conflicting-write"
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            found.append({"correlation_id": row["correlation_id"], "stage": row["stage_id"],
                          "operation": stage["operation"], "status": row["status"], "code": last[0]})
    return found, skipped


def recover_conflicts(root, *, apply=False):
    """Report, and with ``apply`` replay and settle, stages blocked by writer races."""
    found, skipped = conflict_candidates(root)
    counts = {}
    for item in found:
        key = item["operation"] + "/" + item["code"]
        counts[key] = counts.get(key, 0) + 1
    report = {"apply": apply, "candidates": len(found), "by_operation_code": counts,
              "skipped": skipped}
    if not apply:
        return report
    results = {}
    for item in found:
        replay(root, item["correlation_id"], item["stage"])
        try:
            status = next(s["status"] for s in settle(root, item["correlation_id"], item["stage"])["stages"]
                          if s["id"] == item["stage"])
        except IntakeError as error:
            status = error.code
        results[status] = results.get(status, 0) + 1
    report["results"] = results
    return report
