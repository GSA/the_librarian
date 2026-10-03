"""Initialize missing empty stores; validate existing stores without replacing them."""

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def command(script, action):
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--root", str(ROOT), action],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if result.returncode or result.stderr:
        raise RuntimeError(f"{script} {action}: {result.stderr.strip() or 'command failed'}")
    return json.loads(result.stdout)


def main():
    try:
        reports = []
        for script, path in (
            ("tools/catalog/catalog_store.py", ".local/catalog/catalog.sqlite3"),
            ("tools/review-events/event_store.py", ".local/review-events/events.sqlite3"),
        ):
            reports.append(command(script, "status" if (ROOT / path).exists() else "init"))
        path = ROOT / "tools/context-index/.local/index.sqlite3"
        reports.append(command("tools/context-index/context_index.py", "status" if path.exists() else "build"))
        print(json.dumps({"initialized": True, "seeded_records": 0, "results": reports}))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"initialize: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
