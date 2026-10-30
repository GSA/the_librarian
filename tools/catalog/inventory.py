"""Explicit-root metadata discovery; no source-body reads or automatic registration."""

import argparse
import json
import os
from pathlib import Path
import sys

from tools import policy


def inventory(root, directory, limit):
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")
    root = root.absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("Root symlinks are not permitted")
    settings = policy.for_root(root)[0]
    if directory not in settings["markdown_roots"] + settings["original_roots"]:
        raise ValueError("Directory must be an explicitly configured content root")
    start = root / directory
    if not start.is_dir() or any(part.is_symlink() for part in (start, *start.parents)):
        raise ValueError("Content root is absent or uses a symlink")
    records = []
    skipped = 0
    for base, directories, names in os.walk(start, followlinks=False):
        kept = []
        for name in sorted(directories):
            path = Path(base) / name
            if path.is_symlink() or name in settings["excluded_directory_names"]:
                skipped += 1
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file() or not any(
                policy.allowed(relative, category, root) for category in ("markdown_roots", "original_roots")
            ):
                skipped += 1
                continue
            if len(records) == limit:
                raise ValueError("Inventory exceeds limit; no partial result returned")
            records.append({"path": relative, "bytes": path.stat().st_size})
    return {"records": records, "count": len(records), "skipped_entries": skipped,
            "notice": "Filesystem metadata only; not registration, content review or release clearance."}


def parser():
    value = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    value.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    value.add_argument("--limit", type=int, default=1000)
    value.add_argument("directory")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(inventory(args.root, args.directory, args.limit)))
        return 0
    except (OSError, ValueError) as error:
        print(f"inventory: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
