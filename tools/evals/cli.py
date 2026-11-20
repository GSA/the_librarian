"""Retrieval-quality evaluation: generate cases, run them against the index, compare scorecards."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from tools import policy
from tools.context_index.context_index import IndexError_, readonly, validate_db_path
from tools.evals import cases as case_file
from tools.evals import compare as comparison
from tools.evals import generate as generation
from tools.evals import runner
from tools.evals.cases import EvalError

TOOLKIT = Path(__file__).resolve().parents[2]


def parser():
    value = argparse.ArgumentParser(prog="python -m tools.evals", description=__doc__, allow_abbrev=False)
    value.add_argument("--root", type=Path, default=TOOLKIT, help="Instance root (default: this checkout)")
    value.add_argument("--cases", help=f"Cases file (default: <root>/{case_file.DEFAULT_CASES})")
    commands = value.add_subparsers(dest="command", required=True)
    make = commands.add_parser("generate", help="Add seeded known-item and absence cases from the index")
    make.add_argument("--db", type=Path)
    make.add_argument("--seed", type=int, default=1)
    make.add_argument("--count", type=int, default=200, help="Known-item cases (default 200)")
    make.add_argument("--absences", type=int, default=10, help="Absence cases (default 10)")
    make.add_argument("--k", type=int, default=10, help="Rank cutoff for generated cases (default 10)")
    make.add_argument("--terms", type=int, default=3, help="Query terms per known-item case (default 3)")
    execute = commands.add_parser("run", help="Score the cases against a fresh index and write a scorecard")
    execute.add_argument("--manifest", type=Path)
    execute.add_argument("--db", type=Path)
    execute.add_argument("--tag", action="append", default=[], help="Run only cases with this tag (repeatable)")
    execute.add_argument("--standing-policy", default=runner.STANDING_POLICY,
                         help=f"Standing policy recorded in provenance (default: {runner.STANDING_POLICY})")
    execute.add_argument("--output", type=Path,
                         help=f"Scorecard path (default: <root>/{runner.RUNS}/<timestamp>-<run>.json)")
    check = commands.add_parser("compare", help="Compare two scorecards; exit 1 on a regression")
    check.add_argument("base", type=Path)
    check.add_argument("new", type=Path)
    check.add_argument("--tolerance", action="append", default=[], metavar="METRIC=VALUE",
                       help="Allowed drop (rates) or increase (counts); repeatable. Defaults: " + ", ".join(
                           f"{k}={v}" for k, v in {**comparison.RATES, **comparison.COUNTS}.items()))
    check.add_argument("--max-regressions", type=int, default=0,
                       help="Per-case pass-to-fail regressions allowed (default 0)")
    check.add_argument("--json", action="store_true", help="Print the comparison as JSON")
    return value


def generate(args):
    root = case_file.no_symlinks(args.root)
    path = case_file.cases_path(root, args.cases)
    existing = case_file.read(path)[0] if path.exists() else None
    db = validate_db_path(root, args.db or root / policy.validate_instance_roots(root)["context_index_db"])
    connection = readonly(db)
    try:
        cases, summary = generation.generate(connection, seed=args.seed, count=args.count,
                                             absences=args.absences, k=args.k, terms=args.terms)
    finally:
        connection.close()
    document, replaced = generation.merge(existing, cases, args.seed)
    case_file.private_directory(path.parent, root if path.is_relative_to(root) else path.parent)
    case_file.write_private(path, case_file.render(document), replace=True)
    return {"command": "generate", "cases_file": str(path), "seed": args.seed, "replaced": replaced,
            "total_cases": len(document["cases"]), **summary}


def run(args):
    root = case_file.no_symlinks(args.root)
    document, sha = case_file.read(case_file.cases_path(root, args.cases))
    scorecard = runner.run(root, document, sha, manifest=args.manifest, db=args.db, tags=args.tag,
                           standing_name=args.standing_policy)
    path = runner.write(root, scorecard, args.output)
    return {"command": "run", "scorecard": str(path), "run_id": scorecard["run_id"],
            "aggregates": scorecard["aggregates"], "timings": scorecard["timings"]}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "compare":
            require_count = args.max_regressions
            if require_count < 0:
                raise EvalError("--max-regressions must be nonnegative")
            report = comparison.compare(comparison.load(args.base), comparison.load(args.new),
                                        tolerance=comparison.tolerances(args.tolerance),
                                        max_regressions=require_count)
            sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else comparison.text(report))
            return 0 if report["ok"] else 1
        result = generate(args) if args.command == "generate" else run(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (EvalError, IndexError_, OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        print(f"evals: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
