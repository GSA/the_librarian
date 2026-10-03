"""Create an allowlisted source package; never include runtime data or local configuration."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]


def release_paths(root=ROOT):
    value = json.loads((root / "release-files.json").read_text())
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise ValueError("Release file list must be nonempty and unique")
    result = []
    for name in value:
        path = PurePosixPath(name)
        if (path.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/"))
                or any(p in {".local", ".venv", ".git", "__pycache__", "node_modules", "data", "sources", "documents"} for p in path.parts)
                or ".local." in path.name or path.suffix in {".sqlite3", ".db", ".zip", ".pyc"}):
            raise ValueError("Operational or unsafe release path")
        absolute = root / name
        if not absolute.is_file() or any(p.is_symlink() for p in (absolute, *absolute.parents)):
            raise ValueError(f"Missing or symlinked release file: {name}")
        result.append((name, absolute))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        paths = release_paths()
        output = args.output.absolute()
        if any(p.is_symlink() for p in (output, *output.parents)):
            raise ValueError("Output cannot use symlinks")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream, ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            for name, path in paths:
                archive.writestr(name, path.read_bytes())
        print(json.dumps({"output": str(output), "files": len(paths),
                          "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                          "notice": "Allowlisted source package, not publication authorization or content clearance."}))
        return 0
    except (OSError, ValueError, TypeError) as error:
        print(f"package: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
