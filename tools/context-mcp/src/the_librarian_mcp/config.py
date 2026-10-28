"""Shared operator configuration; importing it requires only the standard library."""

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class Config:
    root: Path
    manifest: Path = Path("config/selection.json")
    db: Path = Path("tools/context_index/.local/index.sqlite3")
    max_source_chars: int = 30000
    max_output_chars: int = 60000
    max_results: int = 25
    timeout_seconds: float = 30
    diagnostics_log: Path | None = None

    context_module: ClassVar[str] = "tools.context_index.context_index"
    event_module: ClassVar[str] = "tools.review_events.event_reader"
    catalog_module: ClassVar[str] = "tools.catalog.catalog_store"

    @property
    def toolkit_root(self) -> Path:
        """The explicitly selected, validated full-clone installation."""
        return self.root.resolve()

    @property
    def diagnostics_path(self) -> Path:
        return self.toolkit_root / "tools/diagnostics/retrieval_log.py"

    def __post_init__(self):
        if not self.root.is_absolute() or not self.root.is_dir():
            raise ValueError("--root must be an absolute, existing instance directory")
        required = (
            self.toolkit_root / "release-files.json",
            self.toolkit_root / "config/policy.json",
            self.toolkit_root / "tools/__init__.py",
            self.toolkit_root / "tools/context_index/__init__.py",
            self.toolkit_root / "tools/context_index/context_index.py",
            self.toolkit_root / "tools/review_events/event_reader.py",
            self.toolkit_root / "tools/catalog/catalog_store.py",
        )
        if not all(path.is_file() for path in required):
            raise ValueError("--root must identify a complete trusted toolkit clone")
        for name in ("manifest", "db"):
            value = getattr(self, name)
            path = value if value.is_absolute() else self.root / value
            if ".." in path.parts or not path.is_relative_to(self.root):
                raise ValueError(f"--{name} must stay within --root")
        if self.diagnostics_log is not None:
            path = (self.diagnostics_log if self.diagnostics_log.is_absolute()
                    else self.root / self.diagnostics_log)
            if ".." in path.parts or not path.is_relative_to(self.root):
                raise ValueError("--diagnostics-log must stay within --root")
            if path.suffix != ".jsonl":
                raise ValueError("--diagnostics-log must be a .jsonl file")
        for name in ("max_source_chars", "max_output_chars", "max_results"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_results > 50:
            raise ValueError("--max-results cannot exceed the reader's search limit of 50")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("--timeout-seconds must be finite and positive")


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--root", required=True, type=Path, help="Absolute corpus directory")
    result.add_argument("--manifest", type=Path, default=Config.manifest)
    result.add_argument("--db", type=Path, default=Config.db)
    result.add_argument("--max-source-chars", type=int, default=30000)
    result.add_argument("--max-output-chars", type=int, default=60000)
    result.add_argument("--max-results", type=int, default=25)
    result.add_argument("--timeout-seconds", type=float, default=30)
    result.add_argument("--diagnostics-log", type=Path, default=None,
                        help="Opt in to body-free retrieval diagnostics at this .jsonl path")
    return result
