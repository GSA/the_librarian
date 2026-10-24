#!/usr/bin/env python3
"""Activate this installation's pre-commit gate using local Git config only."""

import argparse
import os
from pathlib import Path
import sys

from roborev_precommit import GateError, LAUNCHER_KEY, config_values, git, reject_links, repository_root


HOOK = b"#!/bin/sh\nexec python3 tools/review/roborev_precommit.py\n"


def install(root, launcher):
    launcher = Path(launcher)
    if not launcher.is_absolute() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise GateError("launcher must be an existing absolute executable path")
    reject_links(root, ".githooks/pre-commit")
    hook = root / ".githooks/pre-commit"
    if not hook.is_file() or hook.read_bytes() != HOOK or not os.access(hook, os.X_OK):
        raise GateError("expected executable shared pre-commit hook is missing or conflicting")
    for entry in hook.parent.iterdir():
        if entry.name != "pre-commit":
            raise GateError(f"unexpected shared hook preserved: {entry.name}")
    for name in ("tools/review/roborev_precommit.py", "tools/review/install_hooks.py"):
        reject_links(root, name)
        if not (root / name).is_file():
            raise GateError(f"missing shared hook code: {name}")
    common = Path(os.fsdecode(git(root, "rev-parse", "--git-common-dir").stdout).strip())
    if not common.is_absolute():
        common = root / common
    native = common / "hooks"
    if native.exists():
        for path in native.iterdir():
            if not path.name.endswith(".sample"):
                raise GateError(f"existing hook preserved; refusing to shadow it: {path.name}")
    expected = {"core.hooksPath": ".githooks", LAUNCHER_KEY: str(launcher)}
    pending = {}
    for key, value in expected.items():
        effective = config_values(root, key)
        local = config_values(root, key, local=True)
        if effective and (effective != [value] or local != [value]):
            raise GateError(f"conflicting {key} configuration preserved (launcher/hooks are local-only)")
        if not local:
            pending[key] = value
    # Set the launcher before activation; never replace a previous value.
    for key in (LAUNCHER_KEY, "core.hooksPath"):
        if key in pending:
            git(root, "config", "--local", "--add", key, pending[key])
    print("Installed repository-local pre-commit gate; existing hooks/config were not replaced.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--launcher", required=True, help="existing scoped roborev executable wrapper")
    args = parser.parse_args()
    try:
        install(repository_root(args.root), args.launcher)
        return 0
    except (GateError, OSError, ValueError) as error:
        print(f"INSTALL REFUSED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
