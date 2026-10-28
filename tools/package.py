"""Create an allowlisted source package; never include runtime data or local configuration."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]


def load_release_manifest(root: Path = ROOT) -> list[str]:
    value = json.loads((root / "release-files.json").read_text())
    if not isinstance(value, list) or not value:
        raise ValueError("Release file list must be nonempty")
    for position, name in enumerate(value, 1):
        if not isinstance(name, str):
            raise TypeError(f"Release manifest entry {position} must be a string")
        path = PurePosixPath(name)
        if (path.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/"))
                or any(p in {".local", ".venv", ".git", "__pycache__", "node_modules", "data", "sources", "documents"} for p in path.parts)
                or ".local." in path.name or path.suffix in {".sqlite3", ".db", ".zip", ".pyc"}):
            raise ValueError(f"Operational or unsafe release path at entry {position}")
    if len(set(value)) != len(value):
        raise ValueError("Release file list must be unique")
    return value


def release_paths(root: Path = ROOT) -> list[tuple[str, Path]]:
    result = []
    for name in load_release_manifest(root):
        absolute = root / name
        if not absolute.is_file() or any(p.is_symlink() for p in (absolute, *absolute.parents)):
            raise ValueError(f"Missing or symlinked release file: {name}")
        result.append((name, absolute))
    return result


def validate_output_path(output: Path) -> Path:
    output = output.absolute()
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Output cannot use symlinks")
    return output


def parser():
    value = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        paths = release_paths()
        output = validate_output_path(args.output)
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
