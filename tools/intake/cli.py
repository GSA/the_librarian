"""Explicit private intake operator CLI; never an automatic ingestion service."""
import argparse
import json
from pathlib import Path
import sys

from . import adapters, progress, runner, store, upload
from .contracts import IntakeError, decode, decode_request, require


def read_input(path):
    path = store.safe(path)
    require(path.is_file(), "missing_input")
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    require(len(raw) <= 1024 * 1024, "input_budget")
    return raw


def inspection(root, correlation):
    with store.locked(root) as c:
        result = store.summary(c, correlation)
        request = decode_request(c.execute("SELECT raw FROM requests WHERE correlation_id=?", (correlation,)).fetchone()[0])
        for stage, summary in zip(request["stages"], result["stages"]):
            if stage["kind"] == "publication" and summary["evidence"]:
                summary["availability"] = adapters.availability(store.safe(root), stage, summary["evidence"])
        return result


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("initialize")
    stage = commands.add_parser("stage")
    stage.add_argument("--request", type=Path, required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--state")
    commands.add_parser("dead-letter")
    submit = commands.add_parser("submit-file", help="Stage and register one explicitly supplied file")
    submit.add_argument("file", type=Path)
    submit.add_argument("--submission-id", required=True)
    submit.add_argument("--format", choices=sorted(upload.FORMATS), required=True)
    submit.add_argument("--name", help="Retained file name; defaults to the supplied file name")
    submit.add_argument("--actor", required=True)
    submit.add_argument("--reason", required=True)
    submit.add_argument("--correction-of")
    submit.add_argument("--origin-kind", choices=("declared-remote", "local-path"))
    submit.add_argument("--origin-system")
    submit.add_argument("--origin-locator")
    submit.add_argument("--origin-version")
    submit.add_argument("--expected-sha256")
    for name in ("drop-scan", "drop-submit"):
        commands.add_parser(name).add_argument("--drop", type=Path, required=True)
    for name in ("progress", "progress-serve"):
        view = commands.add_parser(name, help="Read-only, body-free intake progress" + (
            " over loopback HTTP" if name == "progress-serve" else ""))
        view.add_argument("--limit", type=int, default=50, help="Recent and attention items to list")
        view.add_argument("--run", action="append", default=[], metavar="NAME=PREFIX",
                          help="Group correlation IDs starting with PREFIX as a named run")
        if name == "progress":
            view.add_argument("--format", choices=("json", "text"), default="json")
        else:
            view.add_argument("--host", default="127.0.0.1")
            view.add_argument("--port", type=int, default=8765)
            view.add_argument("--refresh", type=int, default=5, help="Page refresh seconds")
            view.add_argument("--allow-non-loopback", action="store_true",
                              help="Explicit approval to expose the view beyond this machine")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("correlation")
    for name in ("run", "resume", "replay", "acknowledge"):
        action = commands.add_parser(name)
        action.add_argument("correlation")
        action.add_argument("--stage", required=True)
        if name == "acknowledge":
            action.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def runs(values):
    result = {}
    for value in values:
        name, separator, prefix = value.partition("=")
        require(separator and name and prefix and name not in result, "invalid_run")
        result[name] = prefix
    return result


def main(argv=None):
    args = arguments(argv)
    try:
        if args.command == "progress":
            require(1 <= args.limit <= 1000, "invalid_limit")
            report = progress.snapshot(args.root, limit=args.limit, runs=runs(args.run))
            sys.stdout.write(progress.render_text(report) if args.format == "text"
                             else json.dumps(report, sort_keys=True) + "\n")
            return 0
        if args.command == "progress-serve":
            require(1 <= args.limit <= 1000 and 1 <= args.refresh <= 3600
                    and 0 <= args.port <= 65535, "invalid_option")
            progress.check_bind(args.host, args.allow_non_loopback)
            result = progress.serve(args.root, host=args.host, port=args.port, refresh=args.refresh,
                                    limit=args.limit, runs=runs(args.run),
                                    allow_non_loopback=args.allow_non_loopback)
        elif args.command == "initialize":
            result = store.initialize(args.root)
        elif args.command == "stage":
            result = store.stage(args.root, read_input(args.request))
        elif args.command in {"list", "dead-letter"}:
            result = {"requests": store.list_requests(args.root, "dead-letter" if args.command == "dead-letter" else args.state)}
        elif args.command == "submit-file":
            value = upload.descriptor(
                submission_id=args.submission_id, format_name=args.format,
                name=args.name or args.file.name, actor=args.actor, reason=args.reason,
                correction_of=args.correction_of, origin_system=args.origin_system,
                origin_locator=args.origin_locator, origin_version=args.origin_version,
                origin_kind=args.origin_kind,
                expected_sha256=args.expected_sha256)
            result = upload.submit(args.root, args.file, value)
        elif args.command == "drop-scan":
            result = upload.drop_scan(args.drop)
        elif args.command == "drop-submit":
            result = upload.drop_submit(args.root, args.drop)
        elif args.command == "inspect":
            result = inspection(args.root, args.correlation)
        elif args.command in {"run", "resume"}:
            result = runner.run(args.root, args.correlation, args.stage)
        elif args.command == "replay":
            result = runner.replay(args.root, args.correlation, args.stage)
        else:
            result = runner.acknowledge(args.root, args.correlation, args.stage, decode(read_input(args.receipt)))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (IntakeError, OSError, adapters.StageFailure) as error:
        code = error.code if isinstance(error, (IntakeError, adapters.StageFailure)) else "os-error"
        print(json.dumps({"error": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
