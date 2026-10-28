#!/usr/bin/env python3
"""Generate or check schema-only snapshots from versioned code; never open databases."""

import argparse
import ast
import json
from pathlib import Path
import sys


HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[1]
SOURCES = {
    "catalog.sql": ("tools/catalog/catalog_store.py", "SQL", "VERSION"),
    "review-events.sql": ("tools/review_events/event_store.py", "SQL", "VERSION"),
    "context-index.sql": ("tools/context_index/context_index.py", "DDL", "SCHEMA"),
}


def snapshot(source: Path, symbol: str, version_symbol: str) -> str:
    values = {}
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {symbol, version_symbol, "APPLICATION_ID"}:
                    values[target.id] = ast.literal_eval(node.value)
    ddl, version = values[symbol], values[version_symbol]
    if not isinstance(ddl, str) or type(version) is not int:
        raise ValueError("Expected literal DDL and integer schema version")
    if symbol == "SQL":
        if source.name == "catalog_store.py":
            from tools.catalog import catalog_store as module
        else:
            from tools.review_events import event_store as module
        ddl = module.schema_sql()
    else:
        application_id = values["APPLICATION_ID"]
        if type(application_id) is not int:
            raise ValueError("Expected an integer SQLite application identity")
        ddl = f"PRAGMA application_id={application_id};\nPRAGMA user_version={version};\n{ddl}"
    return (
        f"-- Generated from {source.relative_to(ROOT).as_posix()} ({symbol}); do not edit.\n"
        f"-- Schema version {version}; structure only, not a data backup or restore procedure.\n"
        + ddl.strip() + "\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if any tracked snapshot is missing or stale")
    args = parser.parse_args(argv)
    try:
        expected = {
            name: snapshot(ROOT / path, symbol, version)
            for name, (path, symbol, version) in SOURCES.items()
        }
        changed = []
        for name, text in expected.items():
            output = HOME / name
            if not output.exists() or output.read_text(encoding="utf-8") != text:
                changed.append(name)
                if not args.check:
                    output.write_text(text, encoding="utf-8")
        ok = not changed if args.check else True
        print(json.dumps({"command": "schema-snapshots", "ok": ok,
                          "mode": "check" if args.check else "generate", "changed": changed}))
        if not ok:
            print("Schema snapshots missing or stale: " + ", ".join(changed), file=sys.stderr)
        return 0 if ok else 1
    except (OSError, UnicodeError, SyntaxError, ValueError, KeyError, ImportError) as error:
        print(f"Schema snapshot failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
