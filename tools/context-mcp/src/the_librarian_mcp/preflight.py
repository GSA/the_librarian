"""Check local agent prerequisites; optionally refresh only the disposable index."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .config import Config, parser


RUNTIME = """
import json, sqlite3, sys
from importlib.metadata import version
import anyio, jsonschema
from mcp.server import MCPServer
assert sys.version_info >= (3, 14), 'Python 3.14+ required'
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


def pdf_image_runtime(config: Config) -> dict:
    """Informational: whether cohort intake can read PDFs and images (the pinned Docling runtime).

    An unprovisioned runtime is not a failure; those files stay ``needs-runtime`` until it is.
    """
    check = run_check(
        "pdf_image_runtime",
        module_command(config, "tools.extraction.pdf_image", "status", "--root", str(config.root)),
        config, "Run python -m tools.extraction.pdf_image status --root ROOT and resolve its diagnostic.",
    )
    if check["ok"]:
        result = check["result"]
        check["provisioned"] = result.get("provisioned") is True
        check["action"] = result.get("action") or (
            "PDF and image files in cohort intake are read through the pinned runtime.")
    return check


def legacy_converter_runtime(config: Config) -> dict:
    """Informational: whether cohort intake can convert legacy Office files (a LibreOffice and a sandbox).

    An absent runtime is not a failure; those files stay ``needs-reader`` until it is provisioned.
    """
    check = run_check(
        "legacy_converter_runtime",
        module_command(config, "tools.extraction.office_runtime", "status"),
        config, "Run python -m tools.extraction.office_runtime status and resolve its diagnostic.",
    )
    if check["ok"]:
        result = check["result"]
        check["provisioned"] = result.get("provisioned") is True
        check["ready"] = result.get("ready") is True
        check["action"] = result.get("action") or (
            "Legacy .doc/.xls/.ppt/.vsd files in cohort intake are converted by the sandboxed runtime.")
    return check


START_ACTION = (
    "Start the server the way the client configuration does and read its stderr; after an upgrade, "
    "reinstall the adapter into the instance .venv (pip install ./tools/context-mcp) and re-run this check.")


def default_server_command() -> list[str]:
    """The console script beside this interpreter, which is what a client configuration names."""
    script = Path(sys.executable).parent / "librarian-mcp"
    return [str(script)] if script.is_file() else [sys.executable, "-m", "the_librarian_mcp.server"]


def server_arguments(config: Config) -> list[str]:
    """The server options a client configuration would carry; logs stay off so the check records nothing."""
    return ["--root", str(config.root), "--manifest", str(config.manifest), "--db", str(config.db),
            "--max-source-chars", str(config.max_source_chars),
            "--max-output-chars", str(config.max_output_chars),
            "--max-results", str(config.max_results), "--timeout-seconds", str(config.timeout_seconds)]


async def _fresh_session(command: list[str], config: Config, query: str, limit: float,
                         scratch: Path, errlog, timings: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # env=None gives the client's default environment (HOME, LOGNAME, PATH, SHELL, TERM, USER): no
    # PYTHONPATH and no inherited virtual environment. cwd is outside the instance.
    parameters = StdioServerParameters(command=command[0], args=[*command[1:], *server_arguments(config)],
                                       cwd=scratch)

    async def timed(name, call):
        begin = time.monotonic()
        try:
            return await asyncio.wait_for(call, limit)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{name} exceeded the {limit:g} s client timeout") from None
        finally:
            timings[name] = round(time.monotonic() - begin, 3)

    begin = time.monotonic()
    async with stdio_client(parameters, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await timed("initialize", session.initialize())
            status = await timed("status", session.call_tool("status", {}))
            if status.is_error:
                raise RuntimeError(f"status returned an error: {status.content[0].text[:500]}")
            status_payload = json.loads(status.content[0].text)
            search = await timed("search", session.call_tool("search", {"query": query}))
            if search.is_error:
                raise RuntimeError(f"search returned an error: {search.content[0].text[:500]}")
            if not isinstance(json.loads(search.content[0].text).get("hits"), list):
                raise RuntimeError("search returned no hits list")
    timings["total"] = round(time.monotonic() - begin, 3)
    return {"documents": status_payload.get("documents"), "fresh": status_payload.get("fresh")}


def fresh_start(config: Config, *, command: list[str] | None = None, query: str = "documentation",
                client_timeout: float = 60) -> dict:
    """Start the MCP server like a client does (new process, default client environment, cwd outside the
    instance), call ``status`` and one ``search``, and fail on any error or any step over ``client_timeout``.

    A process-fresh start is not a cold page cache: the timings are cold only when the host's cache is.
    """
    command = command or default_server_command()
    timings: dict = {}
    base = {"name": "fresh_mcp_start", "command": command, "client_timeout_seconds": client_timeout}
    with tempfile.TemporaryDirectory(prefix="librarian-start-") as scratch, \
            tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errlog:
        try:
            result = asyncio.run(_fresh_session(command, config, query, client_timeout,
                                                Path(scratch), errlog, timings))
        except Exception as error:  # includes ExceptionGroup raised by the stdio transport
            errlog.seek(0)
            diagnostic = errlog.read()[-2000:].strip()
            while isinstance(error, BaseExceptionGroup) and error.exceptions:
                error = error.exceptions[0]
            summary = f"{type(error).__name__}: {error}"
            return {**base, "ok": False, "timings": timings, "action": START_ACTION,
                    "error": f"{summary}\n{diagnostic}" if diagnostic else summary}
    return {**base, "ok": True, "timings": timings, "result": result,
            "action": "Record these timings in the upgrade notes; the slowest step must stay under the client timeout."}


def preflight(config: Config, *, refresh_index: bool = False, verify_start: bool = False,
              server_command: list[str] | None = None, client_timeout: float = 60) -> dict:
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
    checks.append(pdf_image_runtime(config))
    checks.append(legacy_converter_runtime(config))
    if verify_start:
        checks.append(fresh_start(config, command=server_command, client_timeout=client_timeout))
    return {
        "command": "preflight", "ok": all(check["ok"] for check in checks),
        "refresh_requested": refresh_index, "start_verification_requested": verify_start, "checks": checks,
        "notice": "Local engineering readiness, not client connection, authorization or operational acceptance. "
                  "No database backup, authoritative write, intake or selection expansion is performed. "
                  "Store status may maintain private validation caches.",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = parser(__doc__)
    arguments.add_argument("--refresh-index", action="store_true",
                           help="Explicitly rebuild the disposable index after prerequisite checks")
    arguments.add_argument("--verify-start", action="store_true",
                           help="Also start a fresh librarian-mcp as a client does and call status and search")
    arguments.add_argument("--server-command", type=Path, default=None,
                           help="Server executable named by the client configuration "
                                "(default: librarian-mcp beside this interpreter)")
    arguments.add_argument("--client-timeout-seconds", type=float, default=60,
                           help="Fail if the start, status or search exceeds the client's timeout (default 60)")
    options = vars(arguments.parse_args(argv))
    refresh_index = options.pop("refresh_index")
    verify_start = options.pop("verify_start")
    server_command = options.pop("server_command")
    client_timeout = options.pop("client_timeout_seconds")
    if client_timeout <= 0:
        arguments.error("--client-timeout-seconds must be positive")
    try:
        config = Config(**options)
    except ValueError as error:
        arguments.error(str(error))
    report = preflight(config, refresh_index=refresh_index, verify_start=verify_start,
                       server_command=None if server_command is None else [str(server_command)],
                       client_timeout=client_timeout)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
