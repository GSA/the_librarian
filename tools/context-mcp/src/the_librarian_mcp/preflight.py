"""Check local agent prerequisites; optionally refresh only the disposable index."""

import json
from pathlib import Path
import subprocess
import sys

from .config import Config, parser


RUNTIME = """
import json, sqlite3, sys
from importlib.metadata import version
import anyio, jsonschema
from mcp.server import MCPServer
assert sys.version_info >= (3, 11), 'Python 3.11+ required'
assert version('mcp') == '2.2.0', 'This adapter requires mcp==2.2.0'
with sqlite3.connect(':memory:') as db:
    db.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')
print(json.dumps({'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version,
                  'mcp': version('mcp'), 'fts5': True}))
"""


def run_check(name: str, argv: list[str], config: Config, action: str) -> dict:
    try:
        result = subprocess.run(
            argv, cwd=config.root, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=config.timeout_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"name": name, "ok": False, "error": str(error), "action": action}
    if result.returncode or result.stderr:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        return {
            "name": name, "ok": False, "exit_code": result.returncode,
            "error": diagnostic or "Command failed without a stderr diagnostic",
            "action": action,
        }
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
    except (UnicodeError, ValueError) as error:
        return {"name": name, "ok": False, "error": str(error), "action": action}
    return {"name": name, "ok": True, "result": payload}


def preflight(
    config: Config, *, refresh_index: bool = False,
    catalog_store: Path | None = None, event_store: Path | None = None,
) -> dict:
    tools = config.reader.parent.parent
    catalog = catalog_store or tools / "catalog/catalog_store.py"
    events = event_store or config.event_reader_path.with_name("event_store.py")
    checks = [run_check(
        "runtime", [sys.executable, "-B", "-c", RUNTIME], config,
        "Install the adapter's declared dependencies in this interpreter; do not alter the stores.",
    )]
    checks.append({
        "name": "configuration",
        "ok": config.event_reader_path.is_absolute() and config.event_reader_path.is_file(),
        "root": str(config.root), "reader": str(config.reader),
        "event_reader": str(config.event_reader_path),
        "manifest": str(config.manifest), "index": str(config.db),
        "action": "Use the same trusted paths and interpreter in the MCP client configuration.",
    })
    if not checks[-1]["ok"]:
        checks[-1]["error"] = "Trusted event reader is missing or not absolute"
    checks.append(run_check(
        "schema_snapshots",
        [sys.executable, "-B", str(tools / "schemas/export_schemas.py"), "--check"],
        config, "Review schema changes, regenerate schema-only snapshots and commit them with the code.",
    ))
    for name, script in (("catalog", catalog), ("review_events", events)):
        if not script.is_absolute() or not script.is_file():
            checks.append({
                "name": name, "ok": False, "error": "Store CLI path must be absolute and exist",
                "action": "Set the corresponding --catalog-store or --event-store operator override.",
            })
            continue
        checks.append(run_check(
            name, [sys.executable, "-B", str(script), "--root", str(config.root), "status"],
            config, "Inspect this authoritative store with its CLI; never rebuild it as an index.",
        ))
    index_argv = [
        sys.executable, "-B", str(config.reader), "--root", str(config.root),
        "--manifest", str(config.manifest), "--db", str(config.db),
    ]
    if refresh_index:
        if all(check["ok"] for check in checks):
            checks.append(run_check(
                "refresh_index", [*index_argv, "build"], config,
                "Resolve the build diagnostic and retry explicitly; no store restore or initialization is automatic.",
            ))
        else:
            checks.append({
                "name": "refresh_index", "ok": False, "skipped": True,
                "error": "Refresh refused because a prerequisite failed",
                "action": "Resolve the preceding errors before requesting --refresh-index again.",
            })
    checks.append(run_check(
        "index", [*index_argv, "status"], config,
        "After maintained-context edits, run this command with --refresh-index; this refreshes only derived data.",
    ))
    return {
        "command": "preflight", "ok": all(check["ok"] for check in checks),
        "refresh_requested": refresh_index, "checks": checks,
        "notice": "Local engineering readiness, not client connection, authorization or operational acceptance. "
                  "No database backup, authoritative write, intake or selection expansion is performed. "
                  "Store status may maintain private validation caches.",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = parser(__doc__)
    arguments.add_argument("--refresh-index", action="store_true",
                           help="Explicitly rebuild the disposable index after prerequisite checks")
    arguments.add_argument("--catalog-store", type=Path, help="Absolute trusted catalog_store.py override")
    arguments.add_argument("--event-store", type=Path, help="Absolute trusted event_store.py override")
    options = vars(arguments.parse_args(argv))
    operations = {name: options.pop(name) for name in ("refresh_index", "catalog_store", "event_store")}
    try:
        config = Config(**options)
    except ValueError as error:
        arguments.error(str(error))
    report = preflight(config, **operations)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
