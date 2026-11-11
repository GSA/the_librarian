"""Small shared helpers for isolated release-copy tests."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tools import package, policy

ROOT = Path(__file__).resolve().parents[1]


def clone_release(destination: Path) -> None:
    for name, source in package.release_paths(ROOT):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def run_json(*argv: str, cwd: Path, expected: int = 0):
    result = subprocess.run([sys.executable, *argv], cwd=cwd, capture_output=True, text=True)
    if result.returncode != expected:
        raise AssertionError(
            f"expected exit {expected}, got {result.returncode}: {result.stderr}"
        )
    return json.loads(result.stdout)


def default_policy_hash():
    """Policy hash a temporary workspace without its own policy files resolves to."""
    with tempfile.TemporaryDirectory() as directory:
        return policy.for_root(Path(directory))[1]
