"""Initialize missing empty stores; validate existing stores without replacing them."""

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def command(module, action):
    result = subprocess.run(
        [sys.executable, "-m", module, "--root", str(ROOT), action], cwd=ROOT,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if result.returncode or result.stderr:
        raise RuntimeError(f"{module} {action}: {result.stderr.strip() or 'command failed'}")
    return json.loads(result.stdout)


def main():
    try:
        legacy = ROOT / "tools/context-index/.local/index.sqlite3"
        if legacy.exists() or legacy.is_symlink():
            raise RuntimeError(
                "Legacy derived index remains at tools/context-index/.local/index.sqlite3; "
                "remove it deliberately, then rebuild the disposable index"
            )
        reports = []
        for module, path in (
            ("tools.catalog.catalog_store", ".local/catalog/catalog.sqlite3"),
            ("tools.review_events.event_store", ".local/review-events/events.sqlite3"),
        ):
            reports.append(command(module, "status" if (ROOT / path).exists() else "init"))
        path = ROOT / "tools/context_index/.local/index.sqlite3"
        reports.append(command("tools.context_index.context_index", "status" if path.exists() else "build"))
        print(json.dumps({"initialized": True, "seeded_records": 0, "results": reports}))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"initialize: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
