"""Initialize missing empty stores; validate existing stores without replacing them."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import policy
from tools.intake import store as intake_store


def command(root, module, action, extra=()):
    result = subprocess.run(
        [sys.executable, "-m", module, "--root", str(root), *extra, action], cwd=root,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if result.returncode or result.stderr:
        raise RuntimeError(f"{module} {action}: {result.stderr.strip() or 'command failed'}")
    return json.loads(result.stdout)


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--root", type=Path, default=ROOT)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--migrate-storage", action="store_true",
                      help="Transactionally relocate initialized stores to configured storage_roots")
    mode.add_argument("--recover-migration", action="store_true",
                      help="Roll back an interrupted storage_roots migration")
    mode.add_argument("--relocate", action="store_true",
                      help="Rebind operational recovery ledgers after an approved host-path relocation")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root = policy._no_symlinks(args.root)
        if args.recover_migration:
            report = policy.recover_migration(root)
            print(json.dumps(report))
            return 0
        if args.migrate_storage:
            report = policy.migrate_storage(root)
            roots = policy.storage_roots(root)
            manifest = policy.selection_path(root)
            db = root / roots["context_index_db"]
            index_report = command(
                root, "tools.context_index.context_index", "build",
                ("--manifest", str(manifest), "--db", str(db)),
            )
            print(json.dumps({**report, "index": index_report}))
            return 0
        if args.relocate:
            roots = policy.validate_instance_roots(root)
            intake_db = root / roots["intake_home"] / "intake.sqlite3"
            intake_report = intake_store.relocate(root) if intake_db.exists() else {"relocated": False}
            manifest = policy.selection_path(root)
            db = root / roots["context_index_db"]
            status_reports = [
                command(root, "tools.catalog.catalog_store", "status"),
                command(root, "tools.review_events.event_store", "status"),
                command(root, "tools.context_index.context_index", "status",
                        ("--manifest", str(manifest), "--db", str(db))),
            ]
            print(json.dumps({"relocated": True, "intake": intake_report, "results": status_reports}))
            return 0
        legacy = root / "tools/context-index/.local/index.sqlite3"
        if legacy.exists() or legacy.is_symlink():
            raise RuntimeError(
                "Legacy derived index remains at tools/context-index/.local/index.sqlite3; "
                "remove it deliberately, then rebuild the disposable index"
            )
        roots = policy.validate_instance_roots(root)
        reports = []
        for module, path in (
            ("tools.catalog.catalog_store", Path(roots["catalog_home"]) / "catalog.sqlite3"),
            ("tools.review_events.event_store", Path(roots["review_events_home"]) / "events.sqlite3"),
        ):
            reports.append(command(root, module, "status" if (root / path).exists() else "init"))
        instance = policy.record_instance_binding(root)
        manifest = policy.selection_path(root)
        path = root / roots["context_index_db"]
        reports.append(command(
            root, "tools.context_index.context_index",
            "status" if path.exists() else "build",
            ("--manifest", str(manifest), "--db", str(path)),
        ))
        print(json.dumps({
            "initialized": True,
            "instance_id": instance["instance_id"],
            "storage_roots": roots,
            "seeded_records": 0,
            "results": reports,
        }))
        return 0
    except (OSError, RuntimeError, ValueError, intake_store.IntakeError) as error:
        print(f"initialize: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
