"""Explicit single-stage execution, resumable attempts and safe replay."""
import errno
import sqlite3
import time

from tools import policy
from . import adapters, store
from .contracts import IntakeError, canonical, decode, decode_request, digest, require


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


def run(root, correlation_id, stage_id, *, now=None, clock=None):
    # Explicit now retains the fixed-time interface used by existing callers.
    clock = clock if clock is not None else time.time if now is None else lambda: now
    started = clock()
    with store.locked(root) as c:
        request, stage, saved = selected(c, correlation_id, stage_id)
        if saved["status"] in {"succeeded", "return-pending"}:
            return store.summary(c, correlation_id)
        require(saved["status"] not in {"blocked", "dead-letter"}, "explicit_replay_required")
        attempts = c.execute("SELECT * FROM attempts WHERE correlation_id=? AND stage_id=? AND cycle=? ORDER BY ordinal", (correlation_id, stage_id, saved["cycle"])).fetchall()
        unfinished = attempts[-1] if attempts and attempts[-1]["outcome"] is None else None
        if attempts and unfinished is None:
            require(started >= attempts[-1]["eligible_at"], "backoff_pending")
        ordinal = unfinished["ordinal"] if unfinished else len(attempts) + 1
        if unfinished is None:
            require(ordinal <= request["retry"]["max_attempts"], "attempt_limit")

        def begin():
            if unfinished is None:
                with c:
                    c.execute("INSERT INTO attempts(correlation_id,stage_id,cycle,ordinal,started) VALUES (?,?,?,?,?)", (correlation_id, stage_id, saved["cycle"], ordinal, started))
                    c.execute("UPDATE stages SET status='running' WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id))

        def failed(error):
            failure = classify(error)
            if isinstance(error, IntakeError):
                failure = adapters.StageFailure("blocked", "policy-changed" if error.code == "policy_changed" else error.code.replace("_", "-"))
            status = "blocked" if failure.category == "blocked" else "dead-letter" if failure.category == "permanent" or ordinal >= request["retry"]["max_attempts"] else "retryable"
            ended = clock()
            with c:
                c.execute("UPDATE attempts SET ended=?,outcome='failed',category=?,code=?,eligible_at=? WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?", (ended, failure.category, failure.code, ended + request["retry"]["backoff_seconds"], correlation_id, stage_id, saved["cycle"], ordinal))
                c.execute("UPDATE stages SET status=? WHERE correlation_id=? AND stage_id=?", (status, correlation_id, stage_id))
            return store.summary(c, correlation_id)

        # Separate native preparation from ledger transactions: a failed ledger
        # commit is never a native failure and cannot consume the retry budget.
        try:
            require(policy.load()[1] == request["policy"]["sha256"], "policy_changed")
            intent = decode(saved["intent"].encode()) if saved["intent"] else adapters.prepare(store.safe(root), stage)
        except (adapters.StageFailure, OSError, sqlite3.Error, IntakeError) as error:
            begin()
            return failed(error)
        if saved["intent"] is None:
            with c:
                save_artifact(c, correlation_id, stage_id, "intent", intent)
        begin()
        previous = decode(saved["evidence"].encode()) if saved["evidence"] else None
        try:
            evidence = adapters.execute(store.safe(root), stage, intent, previous)
        except (adapters.StageFailure, OSError, sqlite3.Error, IntakeError) as error:
            return failed(error)
        status = "return-pending" if stage["kind"] == "return" else "succeeded"
        ended = clock()
        with c:
            save_artifact(c, correlation_id, stage_id, "evidence", evidence)
            c.execute("UPDATE attempts SET ended=?,outcome=?,eligible_at=? WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?", (ended, status, ended, correlation_id, stage_id, saved["cycle"], ordinal))
            c.execute("UPDATE stages SET status=? WHERE correlation_id=? AND stage_id=?", (status, correlation_id, stage_id))
        return store.summary(c, correlation_id)


def replay(root, correlation_id, stage_id, *, now=None):
    now = time.time() if now is None else now
    with store.locked(root) as c:
        _, _, saved = selected(c, correlation_id, stage_id)
        require(saved["status"] in {"blocked", "dead-letter"}, "not_replayable")
        cycle = saved["cycle"] + 1
        with c:
            c.execute("INSERT INTO replays VALUES (?,?,?,?)", (correlation_id, stage_id, cycle, now))
            c.execute("UPDATE stages SET status='staged',cycle=? WHERE correlation_id=? AND stage_id=?", (cycle, correlation_id, stage_id))
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
        require(policy.load()[1] == request["policy"]["sha256"], "policy_changed")
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
        return store.summary(c, correlation_id)
