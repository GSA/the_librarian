"""Compare two scorecards: metric deltas, per-case regressions and a gating verdict."""

import json
from pathlib import Path

from tools.evals.cases import EvalError, no_symlinks, require, strict_object
from tools.evals.runner import SCHEMA

# Higher is better; the tolerance is the largest allowed drop.
RATES = {"pass_rate": 0.01, "hit_at_1": 0.02, "hit_at_k": 0.01, "recall_at_k": 0.01, "mrr": 0.02,
         "absence_accuracy": 0.0, "confidence_match_rate": 0.02}
# Lower is better; the tolerance is the largest allowed increase.
COUNTS = {"must_not_violations": 0, "errors": 0}
PROVENANCE = (("index", "engine_fingerprint"), ("index", "manifest_hash"), ("index", "policy_hash"),
              ("index", "rendition_identity"), ("standing_policy", "sha256"),
              ("standing_policy", "version"), ("cases", "sha256"), ("toolkit", "commit"))


def load(path):
    path = no_symlinks(Path(path))
    require(path.is_file(), f"No scorecard at {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_object)
    except (UnicodeError, ValueError) as error:
        raise EvalError(f"Unreadable scorecard {path}: {error}") from error
    require(isinstance(value, dict) and value.get("schema") == SCHEMA
            and isinstance(value.get("aggregates"), dict) and isinstance(value.get("cases"), list)
            and isinstance(value.get("provenance"), dict),
            f"Not a {SCHEMA} scorecard: {path}")
    return value


def tolerances(overrides=()):
    result = {**RATES, **COUNTS}
    for item in overrides:
        name, separator, value = item.partition("=")
        require(separator and name in result, f"--tolerance takes METRIC=VALUE for one of: {', '.join(result)}")
        try:
            number = float(value)
        except ValueError as error:
            raise EvalError(f"Tolerance for {name} must be a number") from error
        require(number >= 0, f"Tolerance for {name} must be nonnegative")
        result[name] = number
    return result


def compare(base, new, *, tolerance=None, max_regressions=0):
    tolerance = tolerance or tolerances()
    metrics, failures = {}, []
    for name in (*RATES, *COUNTS):
        before, after = base["aggregates"].get(name), new["aggregates"].get(name)
        delta = None if before is None or after is None else round(after - before, 6)
        worse = delta is not None and (delta < -tolerance[name] if name in RATES else delta > tolerance[name])
        metrics[name] = {"base": before, "new": after, "delta": delta, "tolerance": tolerance[name],
                         "regressed": worse}
        if worse:
            failures.append(f"metric:{name}")
    for name in ("returned_confidence", "expected_hit_confidence"):
        before, after = base["aggregates"].get(name) or {}, new["aggregates"].get(name) or {}
        metrics[name] = {level: {"base": before.get(level), "new": after.get(level)} for level in ("normal", "low")}
    before = {case["id"]: case for case in base["cases"]}
    after = {case["id"]: case for case in new["cases"]}
    common = sorted(before.keys() & after.keys())
    regressions = [{"id": key, "base": before[key]["status"], "new": after[key]["status"],
                    "reasons": after[key]["reasons"], "base_rank": before[key].get("rank"),
                    "new_rank": after[key].get("rank")}
                   for key in common if before[key]["status"] == "pass" and after[key]["status"] != "pass"]
    improvements = [{"id": key, "base": before[key]["status"], "new": after[key]["status"]}
                    for key in common if before[key]["status"] != "pass" and after[key]["status"] == "pass"]
    rank_changes = sum(1 for key in common if before[key].get("rank") != after[key].get("rank"))
    if len(regressions) > max_regressions:
        failures.append("regressions")
    changed = [".".join(item) for item in PROVENANCE
               if (base["provenance"].get(item[0]) or {}).get(item[1])
               != (new["provenance"].get(item[0]) or {}).get(item[1])]
    return {
        "command": "compare", "ok": not failures, "failures": failures,
        "base_run": base.get("run_id"), "new_run": new.get("run_id"),
        "provenance_changed": changed, "metrics": metrics,
        "cases": {"common": len(common), "only_base": len(before.keys() - after.keys()),
                  "only_new": len(after.keys() - before.keys()), "rank_changes": rank_changes,
                  "regressions": len(regressions), "improvements": len(improvements),
                  "max_regressions": max_regressions},
        "regressions": regressions, "improvements": improvements,
    }


def text(report):
    def number(value):
        return "-" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))
    lines = [f"{'OK' if report['ok'] else 'REGRESSED'}: {report['base_run']} -> {report['new_run']}"]
    if report["provenance_changed"]:
        lines.append("changed inputs: " + ", ".join(report["provenance_changed"]))
    lines.append(f"{'metric':<24}{'base':>10}{'new':>10}{'delta':>10}{'tol':>8}")
    for name in (*RATES, *COUNTS):
        item = report["metrics"][name]
        flag = "  <- regressed" if item["regressed"] else ""
        lines.append(f"{name:<24}{number(item['base']):>10}{number(item['new']):>10}"
                     f"{number(item['delta']):>10}{number(item['tolerance']):>8}{flag}")
    for name in ("returned_confidence", "expected_hit_confidence"):
        item = report["metrics"][name]
        lines.append(f"{name}: " + ", ".join(
            f"{level} {number(item[level]['base'])} -> {number(item[level]['new'])}" for level in item))
    cases = report["cases"]
    lines.append(f"cases: {cases['common']} common, {cases['only_base']} only in base, "
                 f"{cases['only_new']} only in new, {cases['rank_changes']} rank changes")
    lines.append(f"regressions: {cases['regressions']} (allowed {cases['max_regressions']}), "
                 f"improvements: {cases['improvements']}")
    for item in report["regressions"]:
        lines.append(f"  - {item['id']}: {item['base']} -> {item['new']} "
                     f"({', '.join(item['reasons'])}; rank {number(item['base_rank'])} -> {number(item['new_rank'])})")
    for item in report["improvements"]:
        lines.append(f"  + {item['id']}: {item['base']} -> {item['new']}")
    return "\n".join(lines) + "\n"
