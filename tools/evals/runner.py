"""Run cases in process against the published index and build a body-free scorecard.

Freshness is checked once before and once after the whole batch with the same functions the CLI
and MCP adapter use; a stale index is never scored. A scorecard holds case ids, ranks, counts,
confidence levels and provenance hashes only: no query text, locator, path or excerpt.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
import uuid

from tools import policy
from tools.context_index import corrections
from tools.context_index.context_index import (
    IndexError_, count_index, engine_fingerprint, freshness, load_manifest, readonly, search,
    validate_db_path,
)
from tools.evals.cases import EvalError, no_symlinks, private_directory, require, write_private

SCHEMA = "retrieval-eval-scorecard-v1"
TOOLKIT = Path(__file__).resolve().parents[2]
RUNS = "tools/evals/.local/runs"
STANDING_POLICY = "config/standing-policy.local.json"
METADATA = ("schema_version", "engine_fingerprint", "manifest_hash", "audience", "policy_hash",
            "rendition_identity")
NOTICE = ("Body-free retrieval evaluation of this toolkit's behavior on this instance's cases. Not "
          "qualified context, not a review and not evidence about any subject matter; an absence "
          "case reports what is indexed, never that a fact does not exist.")


def score(case, result, elapsed_ms):
    """One case's body-free outcome from a search envelope."""
    hits = result["hits"][:case["k"]]
    paths, locators = case.get("expect_paths", []), case.get("expect_locators", [])
    targets = [("path", value) for value in paths] + [("locator", value) for value in locators]
    found, rank, observed = set(), None, None
    for position, hit in enumerate(hits, 1):
        matched = [t for t in targets if hit[t[0]] == t[1]]
        if matched:
            found.update(matched)
            if rank is None:
                rank, observed = position, hit["confidence"]["level"]
    reasons = []
    outcome = {"id": case["id"], "origin": case["origin"], "tags": case.get("tags", []),
               "hits": len(hits), "duration_ms": round(elapsed_ms, 3),
               "returned_confidence": dict(Counter(hit["confidence"]["level"] for hit in hits))}
    if targets:
        outcome.update(expected=len(targets), found=len(found), rank=rank, observed_confidence=observed)
        if len(found) < len(targets):
            reasons.append("expected-not-in-top-k")
    if "expect_confidence" in case:
        outcome["expected_confidence"] = case["expect_confidence"]
        if observed != case["expect_confidence"]:
            reasons.append("confidence-mismatch")
    if "must_not_paths" in case:
        violations = sum(1 for hit in hits if hit["path"] in case["must_not_paths"])
        outcome["must_not_returned"] = violations
        if violations:
            reasons.append("must-not-returned")
    if case.get("expect_absent"):
        classification = (result.get("absence") or result.get("filtered") or {}).get("classification")
        outcome["absence"] = classification
        if hits:
            reasons.append("unexpected-hits")
        elif case.get("expect_absence") not in (None, classification):
            reasons.append("absence-mismatch")
    outcome["status"] = "fail" if reasons else "pass"
    outcome["reasons"] = reasons
    return outcome


def errored(case, error, elapsed_ms):
    return {"id": case["id"], "origin": case["origin"], "tags": case.get("tags", []),
            "status": "error", "reasons": ["error:" + type(error).__name__],
            "hits": 0, "duration_ms": round(elapsed_ms, 3), "returned_confidence": {}}


def rate(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def aggregate(outcomes, cases):
    by_id = {case["id"]: case for case in cases}
    known = [o for o in outcomes if by_id[o["id"]].get("expect_paths") or by_id[o["id"]].get("expect_locators")]
    absent = [o for o in outcomes if by_id[o["id"]].get("expect_absent")]
    must_not = [o for o in outcomes if "must_not_paths" in by_id[o["id"]]]
    expected_level = [o for o in outcomes if "expect_confidence" in by_id[o["id"]]]
    returned = Counter()
    for outcome in outcomes:
        returned.update(outcome["returned_confidence"])
    observed = Counter(o.get("observed_confidence") for o in known if o.get("observed_confidence"))
    return {
        "cases": len(outcomes),
        "passed": sum(o["status"] == "pass" for o in outcomes),
        "failed": sum(o["status"] == "fail" for o in outcomes),
        "errors": sum(o["status"] == "error" for o in outcomes),
        "pass_rate": rate(sum(o["status"] == "pass" for o in outcomes), len(outcomes)),
        "known_item_cases": len(known),
        "hit_at_1": rate(sum(o.get("rank") == 1 for o in known), len(known)),
        "hit_at_k": rate(sum(o.get("rank") is not None for o in known), len(known)),
        "recall_at_k": rate(sum(o.get("found", 0) / o["expected"] for o in known if o.get("expected")),
                            len(known)),
        "mrr": rate(sum(1 / o["rank"] for o in known if o.get("rank")), len(known)),
        "must_not_cases": len(must_not),
        "must_not_violations": sum(o.get("must_not_returned", 0) > 0 for o in must_not),
        "absence_cases": len(absent),
        "absence_accuracy": rate(sum(o["status"] == "pass" for o in absent), len(absent)),
        "confidence_cases": len(expected_level),
        "confidence_match_rate": rate(sum("confidence-mismatch" not in o["reasons"] and o["status"] != "error"
                                          for o in expected_level), len(expected_level)),
        "returned_confidence": {level: returned.get(level, 0) for level in ("normal", "low")},
        "expected_hit_confidence": {level: observed.get(level, 0) for level in ("normal", "low")},
    }


def grouped(outcomes, cases):
    groups = {}
    for outcome in outcomes:
        for key in ("origin:" + outcome["origin"], *("tag:" + tag for tag in outcome["tags"])):
            groups.setdefault(key, []).append(outcome)
    return {key: aggregate(items, cases) for key, items in sorted(groups.items())}


def git_commit():
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=TOOLKIT, capture_output=True,
                              text=True, timeout=30)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=TOOLKIT,
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    if head.returncode:
        return {"commit": None, "dirty": None}
    return {"commit": head.stdout.strip(), "dirty": bool(dirty.stdout.strip()) if not dirty.returncode else None}


def standing_policy(root, name):
    """Hash, id and version of the standing policy file if present; never its scope or context."""
    path = no_symlinks(Path(root) / name)
    if not path.exists():
        return {"present": False}
    require(path.is_file(), f"Standing policy is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    value = value if isinstance(value, dict) else {}
    return {"present": True, "sha256": hashlib.sha256(raw).hexdigest(),
            **{key: value.get(key) for key in ("schema", "policy_id", "version", "status")}}


def provenance(connection, root, cases_sha, cases_count, standing_name):
    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    counts = count_index(connection)
    return {
        "index": {**{key: metadata.get(key) for key in METADATA},
                  "corpus_root_sha256": hashlib.sha256(metadata.get("corpus_root", "").encode()).hexdigest(),
                  "documents": counts["documents"], "sections": counts["sections"],
                  "confidence": counts.get("confidence")},
        "standing_policy": standing_policy(root, standing_name),
        "claim_corrections": {key: value for key, value in corrections.summary(
            connection, corrections.load(root)).items() if key != "open_drifted"},
        "cases": {"sha256": cases_sha, "count": cases_count},
        "toolkit": {**git_commit(), "engine_fingerprint": engine_fingerprint()},
    }


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 3)


def run(root, document, cases_sha, *, manifest=None, db=None, tags=(), standing_name=STANDING_POLICY,
        clock=time.monotonic):
    """Score every selected case against a fresh index; refuse if it is stale before or after."""
    root = no_symlinks(root)
    cases = [case for case in document["cases"] if not tags or set(tags) & set(case.get("tags", []))]
    require(cases, "No cases selected")
    started_at = datetime.now(timezone.utc)
    manifest = policy.selection_path(root, manifest)
    config, manifest_hash = load_manifest(manifest, root)
    db = validate_db_path(root, db or root / policy.validate_instance_roots(root)["context_index_db"])
    connection = readonly(db)
    try:
        begin = clock()
        reasons = freshness(connection, root, manifest, db, config, manifest_hash)
        freshness_before = clock() - begin
        if reasons:
            raise EvalError("Index is stale; refusing to score it. Rebuild first. " + "; ".join(reasons))
        record = provenance(connection, root, cases_sha, len(cases), standing_name)
        outcomes = []
        batch = clock()
        for case in cases:
            begin = clock()
            try:
                result = search(connection, case["query"], case["k"], case.get("path"), root=root,
                                low_confidence=case.get("low_confidence", "include"))
            except (IndexError_, ValueError) as error:
                outcomes.append(errored(case, error, (clock() - begin) * 1000))
                continue
            outcomes.append(score(case, result, (clock() - begin) * 1000))
        searching = clock() - batch
        begin = clock()
        reasons = freshness(connection, root, manifest, db, config, manifest_hash)
        freshness_after = clock() - begin
        if reasons:
            raise EvalError("Index became stale during the run; no scorecard written. " + "; ".join(reasons))
        if dict(connection.execute("SELECT key, value FROM metadata")).get("engine_fingerprint") != \
                record["index"]["engine_fingerprint"]:
            raise EvalError("Index changed during the run; no scorecard written")
        snapshot = corrections.load(root)
        if (None if snapshot is None else snapshot.state["state_token"]) != \
                record["claim_corrections"].get("state_token"):
            raise EvalError("Claim corrections changed during the run; no scorecard written")
    finally:
        connection.close()
    durations = [o["duration_ms"] for o in outcomes]
    return {
        "schema": SCHEMA, "run_id": str(uuid.uuid4()), "notice": NOTICE,
        "started_at": started_at.isoformat(), "completed_at": datetime.now(timezone.utc).isoformat(),
        "selection": {"tags": sorted(tags)},
        "provenance": record,
        "aggregates": aggregate(outcomes, cases),
        "groups": grouped(outcomes, cases),
        "timings": {"freshness_before_s": round(freshness_before, 3),
                    "freshness_after_s": round(freshness_after, 3), "search_total_s": round(searching, 3),
                    "search_p50_ms": percentile(durations, 0.5), "search_p95_ms": percentile(durations, 0.95),
                    "search_max_ms": max(durations) if durations else None,
                    "search_mean_ms": round(statistics.fmean(durations), 3) if durations else None},
        "cases": outcomes,
    }


def write(root, scorecard, output=None):
    root = no_symlinks(root)
    if output is None:
        local = root / "tools/evals/.local"
        private_directory(root / RUNS, root)
        local.chmod(0o700)
        stamp = scorecard["started_at"][:19].replace("-", "").replace(":", "")
        output = root / RUNS / f"{stamp}Z-{scorecard['run_id'][:8]}.json"
    output = no_symlinks(Path(output) if Path(output).is_absolute() else root / output)
    require(output.suffix == ".json", "Scorecard output must be a .json file")
    private_directory(output.parent, root if output.is_relative_to(root) else output.parent)
    data = (json.dumps(scorecard, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return write_private(output, data, replace=False)
