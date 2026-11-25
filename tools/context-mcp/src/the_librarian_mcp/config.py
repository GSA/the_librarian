"""Shared operator configuration; importing it requires only the standard library."""

import argparse
from dataclasses import dataclass
import importlib.util
import json
import math
from pathlib import Path
from typing import ClassVar

from . import audit

AUDIT_DEFAULT = "derive-from-agent-label"


# The toolkit is not importable from this package's environment: the server runs its CLIs with
# ``cwd=--root`` and loads the few modules it calls in-process by file path. Every file it relies on must
# be listed in the root's release-files.json, so Config refuses a root whose manifest omits one.
TOOLKIT_FILES = frozenset({
    "release-files.json", "config/policy.json", "config/selection.json",
    "tools/__init__.py",
    "tools/policy.py", "tools/backups.py", "tools/durable.py", "tools/file_identity.py",
    "tools/context_index/__init__.py", "tools/context_index/context_index.py",
    "tools/context_index/cli.py", "tools/context_index/compact_records.py",
    "tools/context_index/renditions.py", "tools/context_index/qualification.py",
    "tools/context_index/packets.py", "tools/context_index/confidence.py",
    "tools/context_index/inline_data.py",
    "tools/context_index/corrections.py", "tools/context_index/proof_cache.py",
    "tools/context_index/flags.py", "tools/context_index/forked.py",
    "tools/review_events/__init__.py", "tools/review_events/corrections.py", "tools/review_events/event_history.py",
    "tools/review_events/event_reader.py",
    "tools/review_events/event_store.py", "tools/review_events/review_events.py",
    "tools/review_events/review-event-v1.schema.json",
    "tools/catalog/__init__.py", "tools/catalog/catalog_store.py",
    "tools/catalog/contracts.py", "tools/catalog/storage.py", "tools/catalog/backups.py",
    "tools/catalog/pins.py",
    "tools/catalog/cli.py",
    "tools/diagnostics/__init__.py", "tools/diagnostics/retrieval_log.py",
    "tools/schemas/__init__.py", "tools/schemas/export_schemas.py",
    "tools/schemas/catalog.sql", "tools/schemas/context-index.sql",
    "tools/schemas/review-events.sql", "tools/schemas/intake.sql", "tools/schemas/corrections.sql",
})


def load_toolkit_module(path: Path, name: str):
    """Execute one toolkit file from the validated root without putting the toolkit on sys.path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Toolkit module unavailable at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    audit_log: Path | str | None = None
    agent_label: str | None = None

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
        if not _trusted_clone(self.toolkit_root):
            raise ValueError("--root must identify a complete trusted toolkit clone")
        self._resolve_store_paths()
        self._check_logs()
        self._check_limits()

    def _resolve_store_paths(self):
        policy_mod = load_toolkit_module(self.toolkit_root / "tools/policy.py", "librarian_instance_policy")
        roots = policy_mod.validate_instance_roots(self.root)
        if self.manifest == Path("config/selection.json"):
            resolved_manifest = policy_mod.selection_path(self.root).relative_to(self.toolkit_root)
            object.__setattr__(self, "manifest", resolved_manifest)
        if self.db == Path("tools/context_index/.local/index.sqlite3"):
            object.__setattr__(self, "db", Path(roots["context_index_db"]))
        for name in ("manifest", "db"):
            if not _within(self.root, getattr(self, name)):
                raise ValueError(f"--{name} must stay within --root")

    def _check_logs(self):
        diagnostics = None if self.diagnostics_log is None else _under(self.root, self.diagnostics_log)
        if diagnostics is not None:
            if not _within(self.root, diagnostics):
                raise ValueError("--diagnostics-log must stay within --root")
            if diagnostics.suffix != ".jsonl":
                raise ValueError("--diagnostics-log must be a .jsonl file")
        if self.audit_log is None:
            if self.agent_label is not None:
                raise ValueError("--agent-label only labels an enabled --audit-log")
            return
        if self.agent_label is None:
            raise ValueError("--audit-log requires --agent-label")
        audit.label(self.agent_label)
        if str(self.audit_log) == AUDIT_DEFAULT:
            object.__setattr__(self, "audit_log", audit.default_path(self.agent_label))
        if audit.log_path(self.root, Path(self.audit_log)) == diagnostics:
            raise ValueError("--audit-log must not be the body-free --diagnostics-log")

    def _check_limits(self):
        for name in ("max_source_chars", "max_output_chars", "max_results"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_results > 50:
            raise ValueError("--max-results cannot exceed the reader's search limit of 50")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("--timeout-seconds must be finite and positive")


def _under(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _within(root: Path, value: Path) -> bool:
    path = _under(root, value)
    return ".." not in path.parts and path.is_relative_to(root)


def _trusted_clone(root: Path) -> bool:
    """Whether ``root`` holds a well-formed release manifest naming every toolkit file as a regular file."""
    try:
        manifest = json.loads((root / "release-files.json").read_text())
    except (OSError, TypeError, ValueError):
        return False
    if (not isinstance(manifest, list) or not manifest
            or any(not isinstance(name, str) for name in manifest)
            or len(manifest) != len(set(manifest))):
        return False
    names = set(manifest)
    return TOOLKIT_FILES <= names and all(_trusted_file(root, name) for name in names)


def _trusted_file(root: Path, name: str) -> bool:
    """A plain relative path to a regular file with no symlink anywhere below ``root``."""
    if (not name or "\\" in name or Path(name).is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))):
        return False
    path = root / name
    parts = path.relative_to(root).parts
    return path.is_file() and not any(root.joinpath(*parts[:end]).is_symlink()
                                      for end in range(1, len(parts) + 1))


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
    result.add_argument("--audit-log", type=Path, nargs="?", const=AUDIT_DEFAULT, default=None,
                        help="Opt in to a FULL-TEXT audit of every tool call (queries and returned "
                             "corpus text) at this .jsonl path below <root>/.local; without a value, "
                             ".local/audit/<agent-label>-mcp-access.jsonl")
    result.add_argument("--agent-label", default=None,
                        help="Agent name recorded in each audit record, [a-z0-9._-]{1,64}")
    return result
