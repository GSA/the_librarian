"""Command line for the catalog store; run as ``python -m tools.catalog.catalog_store``."""

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

from tools.catalog import catalog_store as catalog
from tools.catalog.contracts import CatalogError, canonical, exact, now, require

MUTATIONS = {"init", "submit", "backup", "restore", "migrate-indexes"}
FILTERS = ("kind", "receipt", "source", "path", "coverage", "audience", "after", "limit", "expected_state",
           "include_history")


def render_export(page, filters):
    payload = json.dumps({"generated_at": now(), "filters": filters, **page},
                         ensure_ascii=True, sort_keys=True, indent=2)
    longest = max((len(match[0]) for match in re.finditer(r"`+", payload)), default=0)
    fence = "`" * max(3, longest + 1)
    return ("# Catalog metadata query export\n\n"
            "One-off view; metadata only, not source verification or readiness acceptance.\n\n"
            f"{fence}json\n{payload}\n{fence}\n")


def parser():
    parser = argparse.ArgumentParser(description=catalog.__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--max-chars", type=int, default=60000)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "audit", "backup"):
        commands.add_parser(name)
    migrate = commands.add_parser("migrate-indexes", help="Add the lookup indexes to a catalog that lacks them")
    migrate.add_argument("--check", action="store_true", help="Report the lookup shape and verify; write nothing")
    migrate.add_argument("--rebuild", action="store_true", help="Drop and recreate damaged or incomplete lookup structures")
    migrate.add_argument("--recover", action="store_true",
                         help="Let SQLite roll back the journal of a migration that was killed, then verify and migrate")
    verify = commands.add_parser("verify-backup")
    verify.add_argument("snapshot", type=Path)
    recover = commands.add_parser("restore")
    recover.add_argument("snapshot", type=Path)
    recover.add_argument("--expected-state")
    write = commands.add_parser("submit")
    write.add_argument("input", type=Path)
    for option in ("actor", "request-id", "expected-state", "reason"):
        write.add_argument("--" + option, required=True)
    for name in ("query", "export"):
        query = commands.add_parser(name)
        for option in ("kind", "receipt", "source", "path", "coverage", "audience", "after", "expected-state"):
            query.add_argument("--" + option)
        query.add_argument("--limit", type=int, default=25)
        query.add_argument("--include-history", action="store_true")
    return parser


def migrate_indexes(args):
    require(not (args.check and (args.rebuild or args.recover)), "--check writes nothing, so it cannot rebuild or recover")
    return catalog.migrate_indexes(args.root, rebuild=args.rebuild, check=args.check, recover=args.recover)


def submit(args):
    value = catalog.decode(catalog.safe_path(args.input).read_text(encoding="utf-8"))
    exact(value, {"records"}, "submission")
    return catalog.submit(args.root, value["records"], actor=args.actor, request_id=args.request_id,
                          expected_state=args.expected_state, reason=args.reason)


def filters(args):
    return {key: getattr(args, key) for key in FILTERS}


def run(args):
    """The command's result; ``query`` and ``export`` read records with the given filters."""
    commands = {
        "init": lambda: catalog.initialize(args.root),
        "status": lambda: catalog.status(args.root),
        "audit": lambda: catalog.status(args.root, audit=True),
        "backup": lambda: catalog.backup(args.root),
        "migrate-indexes": lambda: migrate_indexes(args),
        "verify-backup": lambda: catalog.verify_backup(args.snapshot),
        "restore": lambda: catalog.restore(args.root, args.snapshot, args.expected_state),
        "submit": lambda: submit(args),
    }
    if args.command in commands:
        return commands[args.command]()
    return catalog.read_records(args.root, **filters(args))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        require(type(args.max_chars) is int and args.max_chars > 0, "Output budget must be positive")
        if args.command in MUTATIONS:
            require(args.max_chars >= 60000, "Mutations require max-chars >=60000")
        result = run(args)
        output = render_export(result, filters(args)) if args.command == "export" else canonical(result) + "\n"
        require(len(output) <= args.max_chars, "Complete output exceeds budget; no fields were truncated")
        sys.stdout.write(output)
        return 0
    except (CatalogError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as error:
        message = str(error) if isinstance(error, CatalogError) else type(error).__name__
        print("catalog-store: " + message, file=sys.stderr)
        return 1
