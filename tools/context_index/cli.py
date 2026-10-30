#!/usr/bin/env python3
"""Command-line adapter for the derived context index."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from tools import policy
from tools.context_index.context_index import (
    IndexError_, build, compact_packet, count_index, engine_fingerprint, envelope,
    fail, freshness, load_catalog, load_compaction, load_manifest, no_symlinks,
    packet, packet_many, publication_lock, readonly, render_result, search,
    validate_db_path, validate_many_limits,
)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    commands.add_parser("status")
    find = commands.add_parser("search")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=5)
    find.add_argument("--path")
    get = commands.add_parser("get")
    get.add_argument("locator")
    get.add_argument("--max-chars", type=int, default=30000)
    get.add_argument("--format", choices=("full", "compact"), default="full",
                     help="Compact shares document metadata and omits only the navigation inventory")
    get.add_argument("--max-output-chars", type=int,
                     help="Limit the complete serialized get response, including its final newline")
    many = commands.add_parser("get-many", help="Retrieve 1..10 complete packets sharing exact source spans")
    many.add_argument("locators", nargs="*")
    many.add_argument("--max-chars", type=int, default=30000,
                      help="Combined unique exact-span source-character budget (default: 30000)")
    many.add_argument("--format", choices=("full", "compact"), default="compact",
                      help="Compact omits only each packet's navigation inventory (default: compact)")
    many.add_argument("--max-output-chars", type=int, default=60000,
                      help="Complete serialized response budget including newline (default: 60000)")
    catalog = commands.add_parser("catalog", help="Query live metadata without requiring a document index")
    for option in ("kind", "receipt", "source", "path", "coverage", "audience", "after", "expected-state"):
        catalog.add_argument("--" + option)
    catalog.add_argument("--limit", type=int, default=25)
    catalog.add_argument("--max-chars", type=int, default=60000)
    catalog.add_argument("--format", choices=("full", "compact"), default="full")
    return parser.parse_args(argv)


def main(argv=None):
    connection = None
    try:
        args = arguments(argv)
        if args.command == "get-many":
            validate_many_limits(args.locators, args.max_chars, args.max_output_chars)
        root = no_symlinks(args.root)
        if not root.is_dir():
            fail(f"Corpus root is not a directory: {root}")
        if args.command == "catalog":
            fingerprint = engine_fingerprint()
            catalog = load_catalog()
            result = catalog.read_records(root, **{key: getattr(args, key) for key in
                ("kind", "receipt", "source", "path", "coverage", "audience", "after", "limit", "expected_state")})
            if args.format == "compact":
                result = load_compaction().compact_catalog(result)
            output = render_result(result, compact=args.format == "compact")
            if engine_fingerprint() != fingerprint:
                fail("Catalog presentation code changed during read; retry")
            if args.max_chars < 1 or len(output) > args.max_chars:
                fail("Complete catalog output exceeds budget; no fields were truncated")
            sys.stdout.write(output)
            return 0
        manifest = policy.selection_path(root, args.manifest)
        config, manifest_hash = load_manifest(manifest, root)
        default_db = root / policy.validate_instance_roots(root)["context_index_db"]
        db = validate_db_path(root, args.db or default_db)
        if db == no_symlinks(manifest):
            fail("Database output cannot overwrite the manifest")
        if args.command == "build":
            with publication_lock(db):
                result = build(root, manifest, db, config, manifest_hash)
        else:
            connection = readonly(db)
            reasons = freshness(connection, root, manifest, db, config, manifest_hash)
            if reasons:
                if args.command == "status":
                    print(json.dumps(envelope(root=root, command="status", fresh=False, reasons=reasons,
                                              database=str(db), **count_index(connection)), ensure_ascii=False))
                fail("Index is stale; run build. " + "; ".join(reasons))
            if args.command == "status":
                result = envelope(root=root, command="status", fresh=True, database=str(db), **count_index(connection))
            elif args.command == "search":
                result = search(connection, args.query, args.limit, args.path)
            elif args.command == "get":
                result = packet(connection, config, args.locator, args.max_chars, root=root)
                if args.format == "compact":
                    result = compact_packet(result)
            elif args.command == "get-many":
                result = packet_many(connection, config, args.locators, args.max_chars,
                                     format_=args.format, root=root)
            # Refuse a result if the inputs changed while assembling it.
            reasons = freshness(connection, root, manifest, db, config, manifest_hash)
            if reasons:
                fail("Index became stale during read; run build. " + "; ".join(reasons))
        sys.stdout.write(render_result(
            result, compact=args.command in ("get", "get-many") and args.format == "compact",
            max_output_chars=args.max_output_chars if args.command in ("get", "get-many") else None,
        ))
        return 0
    except (IndexError_, OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        print(f"context-index: {error}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()



if __name__ == "__main__":
    sys.exit(main())
