#!/usr/bin/env python3
"""Explicit local-only Docling prepare, verify and registration CLI."""

import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import docling_adapter as adapter


def parser():
    value = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    value.add_argument("--root", type=Path, default=HERE.parents[1])
    commands = value.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", allow_abbrev=False)
    inspect.add_argument("--artifacts-path", type=Path, required=True)
    inspect.add_argument("--python", type=Path, required=True)
    for name in ("prepare", "verify", "register"):
        operation = commands.add_parser(name, allow_abbrev=False)
        operation.add_argument("receipt")
        operation.add_argument("--format", choices=("pdf", "image"), required=True)
        operation.add_argument("--artifacts-path", type=Path, required=True)
        operation.add_argument("--python", type=Path, required=True)
        if name == "register":
            operation.add_argument("--actor", required=True)
            operation.add_argument("--expected-state", required=True)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = adapter.inspect(args.artifacts_path, args.python)
        else:
            result = adapter.run(args.root, args.command, args.receipt, args.format,
                                 args.artifacts_path, args.python,
                                 actor=getattr(args, "actor", None),
                                 expected_state=getattr(args, "expected_state", None))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (adapter.AdapterError, adapter.bridge.BridgeError,
            adapter.bridge.catalog.CatalogError, OSError, ValueError, KeyError) as error:
        code = getattr(error, "code", "error")
        print(f"docling_extract: {code}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
