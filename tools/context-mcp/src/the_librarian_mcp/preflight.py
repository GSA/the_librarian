"""Check local agent prerequisites; optionally refresh only the disposable index."""

import json
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
            argv, cwd=config.toolkit_root, stdin=subprocess.DEVNULL,
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


def module_command(config: Config, module: str, *arguments: str) -> list[str]:
    return [sys.executable, "-B", "-m", module, *arguments]


def preflight(config: Config, *, refresh_index: bool = False) -> dict:
    checks = [run_check(
        "runtime", [sys.executable, "-B", "-c", RUNTIME], config,
        "Install the adapter's declared dependencies in this interpreter; do not alter the stores.",
    )]
    checks.append({
        "name": "configuration", "ok": True, "root": str(config.root),
        "context_module": config.context_module, "event_module": config.event_module,
        "manifest": str(config.manifest), "index": str(config.db),
        "action": "Use this absolute toolkit root and interpreter in the MCP client configuration.",
    })
    checks.append(run_check(
        "schema_snapshots", module_command(config, "tools.schemas.export_schemas", "--check"),
        config, "Review schema changes, regenerate schema-only snapshots and commit them with the code.",
    ))
    for name, module in (("catalog", config.catalog_module),
                         ("review_events", "tools.review_events.event_store")):
        checks.append(run_check(
            name, module_command(config, module, "--root", str(config.root), "status"),
            config, "Inspect this authoritative store with its CLI; never rebuild it as an index.",
        ))
    index_argv = module_command(
        config, config.context_module, "--root", str(config.root),
        "--manifest", str(config.manifest), "--db", str(config.db),
    )
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
    options = vars(arguments.parse_args(argv))
    refresh_index = options.pop("refresh_index")
    try:
        config = Config(**options)
    except ValueError as error:
        arguments.error(str(error))
    report = preflight(config, refresh_index=refresh_index)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
