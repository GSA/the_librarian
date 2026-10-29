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


def run(root, correlation_id, stage_id, *, now=None):
    now = time.time() if now is None else now
    with store.locked(root) as c:
        request, stage, saved = selected(c, correlation_id, stage_id)
        if saved["status"] in {"succeeded", "return-pending"}:
            return store.summary(c, correlation_id)
        require(saved["status"] not in {"blocked", "dead-letter"}, "explicit_replay_required")
        attempts = c.execute("SELECT * FROM attempts WHERE correlation_id=? AND stage_id=? AND cycle=? ORDER BY ordinal", (correlation_id, stage_id, saved["cycle"])).fetchall()
        unfinished = attempts[-1] if attempts and attempts[-1]["outcome"] is None else None
        if attempts and unfinished is None:
            require(now >= attempts[-1]["eligible_at"], "backoff_pending")
        if unfinished is None:
            require(len(attempts) < request["retry"]["max_attempts"], "attempt_limit")
            ordinal = len(attempts) + 1
            with c:
                c.execute("INSERT INTO attempts(correlation_id,stage_id,cycle,ordinal,started) VALUES (?,?,?,?,?)", (correlation_id, stage_id, saved["cycle"], ordinal, now))
                c.execute("UPDATE stages SET status='running' WHERE correlation_id=? AND stage_id=?", (correlation_id, stage_id))
        else:
            ordinal = unfinished["ordinal"]
        # Native errors only are classified. Ledger acknowledgment failures propagate,
        # leaving the durable unfinished attempt available for reconciliation.
        try:
            require(policy.load()[1] == request["policy"]["sha256"], "policy_changed")
            intent = decode(saved["intent"].encode()) if saved["intent"] else adapters.prepare(store.safe(root), stage)
            if saved["intent"] is None:
                with c:
                    save_artifact(c, correlation_id, stage_id, "intent", intent)
            previous = decode(saved["evidence"].encode()) if saved["evidence"] else None
            evidence = adapters.execute(store.safe(root), stage, intent, previous)
        except (adapters.StageFailure, OSError, sqlite3.Error, IntakeError) as error:
            failure = classify(error)
            if isinstance(error, IntakeError) and error.code == "policy_changed":
                failure = adapters.StageFailure("blocked", "policy-changed")
            status = "blocked" if failure.category == "blocked" else "dead-letter" if failure.category == "permanent" or ordinal >= request["retry"]["max_attempts"] else "retryable"
            with c:
                c.execute("UPDATE attempts SET ended=?,outcome='failed',category=?,code=?,eligible_at=? WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?", (now, failure.category, failure.code, now + request["retry"]["backoff_seconds"], correlation_id, stage_id, saved["cycle"], ordinal))
                c.execute("UPDATE stages SET status=? WHERE correlation_id=? AND stage_id=?", (status, correlation_id, stage_id))
            return store.summary(c, correlation_id)
        status = "return-pending" if stage["kind"] == "return" else "succeeded"
        with c:
            save_artifact(c, correlation_id, stage_id, "evidence", evidence)
            c.execute("UPDATE attempts SET ended=?,outcome=?,eligible_at=? WHERE correlation_id=? AND stage_id=? AND cycle=? AND ordinal=?", (now, status, now, correlation_id, stage_id, saved["cycle"], ordinal))
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
