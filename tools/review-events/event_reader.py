#!/usr/bin/env python3
"""Read validated review events locally; query JSON or export one-off Markdown to stdout."""

import argparse
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import sys

import event_store as store
import review_events as contract


NOTICE = (store.NOTICE + " Source statements are not verification or operational acceptance. "
          "Content filters use recorded scope.content only; scope is a case-insensitive "
          "literal substring of scope.description. Filters are ANDed; no content is inferred.")


def bounded(output, max_chars):
    contract.require(type(max_chars) is int and 1 <= max_chars <= 1000000,
                     "max_chars must be an integer from 1 to 1000000")
    contract.require(len(output) <= max_chars,
                     "Complete output exceeds max_chars; no fields were truncated")
    return output


def json_output(packet):
    """The query wire format, including its terminating newline."""
    return store.canonical(packet) + "\n"


@dataclass
class Snapshot:
    """One fully validated history, detached from its read-only database transaction."""

    state: dict
    events: list
    successors: dict

    def query(self, *, receipt_id=None, source_id=None, outcome=None, content=None,
              unit=None, scope=None, origin=None, include_history=False, limit=10,
              after=0, expected_state=None, max_chars=60000):
        """AND exact filters; scope is literal casefold substring, never inferred content.

        Sequences are store positions, not result offsets. total_matching includes
        matches before the cursor. A continuation must pin this snapshot's state.
        """
        contract.require(type(limit) is int and 1 <= limit <= 100,
                         "limit must be an integer from 1 to 100")
        contract.require(type(after) is int and after >= 0,
                         "after must be a nonnegative event sequence integer")
        contract.require(type(include_history) is bool, "include_history must be boolean")
        bounded("", max_chars)
        for name, value, prefix in (("receipt_id", receipt_id, "D"),
                                    ("source_id", source_id, "S")):
            pattern = r"D0*[1-9][0-9]*" if prefix == "D" else r"S[1-9][0-9]*"
            contract.require(value is None or isinstance(value, str)
                             and re.fullmatch(pattern, value.upper()) is not None,
                             name + " must be an exact " + prefix + " identifier")
        receipt_id = receipt_id.upper() if receipt_id is not None else None
        source_id = source_id.upper() if source_id is not None else None
        properties = store.store_schema()["$defs"]["event"]["properties"]
        scope_properties = properties["scope"]["properties"]
        enums = {
            "outcome": properties["outcome"]["enum"],
            "content": scope_properties["content"]["items"]["enum"],
            "unit": scope_properties["unit"]["enum"],
            "origin": properties["provenance"]["properties"]["origin"]["enum"],
        }
        for name, value in (("outcome", outcome), ("content", content),
                            ("unit", unit), ("origin", origin)):
            contract.require(value is None or isinstance(value, str) and value in enums[name],
                             name + " must be a recorded contract enum value")
        contract.require(scope is None or isinstance(scope, str) and bool(scope),
                         "scope must be a nonempty literal substring")
        contract.require(after == 0 or expected_state is not None,
                         "expected_state is required whenever after > 0")
        if expected_state is not None:
            contract.require(isinstance(expected_state, str)
                             and re.fullmatch(r"[a-f0-9]{64}", expected_state) is not None,
                             "expected_state must be a state token")
            contract.require(expected_state == self.state["state_token"],
                             "Changed state: expected_state does not match; restart pagination")
        filters = dict(receipt_id=receipt_id, source_id=source_id, outcome=outcome,
                       content=content, unit=unit, scope=scope, origin=origin,
                       include_history=include_history, limit=limit, after=after,
                       expected_state=expected_state, max_chars=max_chars)
        matches = []
        for sequence, event in enumerate(self.events, 1):
            if not include_history and event["event_id"] in self.successors:
                continue
            if (receipt_id is not None and event["source"]["receipt_id"] != receipt_id
                    or source_id is not None and event["source"]["source_id"] != source_id
                    or outcome is not None and event["outcome"] != outcome
                    or content is not None and content not in event["scope"]["content"]
                    or unit is not None and event["scope"]["unit"] != unit
                    or scope is not None and scope.casefold() not in event["scope"]["description"].casefold()
                    or origin is not None and event["provenance"]["origin"] != origin):
                continue
            matches.append((sequence, event))
        remaining = [(sequence, event) for sequence, event in matches if sequence > after]
        selected = remaining[:limit]
        packet = {
            "contract_version": 1, "audience": contract.policy.SETTINGS["audience"],
            "authority": store.AUTHORITY, "notice": NOTICE,
            "handling": contract.policy.SETTINGS["handling"], "generated_at": store.now(),
            "state": deepcopy(self.state), "filters": filters,
            "records": [{"sequence": sequence, "event": deepcopy(event),
                         "superseded_by": self.successors.get(event["event_id"])}
                        for sequence, event in selected],
            "count": len(selected), "total_matching": len(matches),
            "next_after": selected[-1][0] if len(remaining) > limit else None,
        }
        bounded(json_output(packet), max_chars)
        return packet


def read_snapshot(root):
    """Read once with mode=ro and BEGIN; validate all history before any filtering."""
    try:
        _, db, _ = store.locations(root)
        contract.require(db.exists(), "Review-event database is missing; no legacy fallback")
        with closing(store.connect(db)) as connection:
            connection.execute("BEGIN")
            state, events = store.inspect(connection)
            successors = {predecessor: event["event_id"]
                          for event in events for predecessor in event["supersedes"]}
        return Snapshot(state, events, successors)
    except contract.ReviewError:
        raise
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError) as error:
        raise contract.ReviewError(
            "Cannot read validated review-event store: " + type(error).__name__) from error


def fenced_json(value):
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", payload)), default=0))
    return fence + "json\n" + payload + "\n" + fence + "\n"


def export_markdown(packet, *, max_chars=60000):
    """Render exactly this bounded page, without writes or dynamic Markdown interpretation."""
    page = ("Incomplete page: more matching records remain; continue with next_after and "
            "the same expected_state token."
            if packet["next_after"] is not None else "Final page: no further matching records.")
    if packet["count"] == 0:
        page += (" Empty page: no records match the selected filters."
                 if packet["total_matching"] == 0
                 else " Empty page: matching records exist only at or before the cursor.")
    output = ("# Review-event export\n\nOne-off selected page; not a published report.\n\n"
              + NOTICE + "\n\n" + page + "\n\n"
              "Page scope: only the recorded filters and sequences after the cursor; "
              "earlier pages and nonmatching events are not included. Event counts are "
              "not normalized source-unit totals.\n\n## Query and state\n\n")
    output += fenced_json({key: value for key, value in packet.items() if key != "records"})
    for record in packet["records"]:
        output += "\n## Event " + str(record["sequence"]) + "\n\n" + fenced_json(record)
    return bounded(output, max_chars)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=contract.ROOT)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("query", "export"):
        command = subcommands.add_parser(name)
        command.add_argument("--receipt", dest="receipt_id")
        command.add_argument("--source", dest="source_id")
        for option in ("outcome", "content", "unit", "origin"):
            command.add_argument("--" + option)
        command.add_argument("--scope", help="Case-insensitive literal substring of scope.description")
        command.add_argument("--include-history", action="store_true")
        command.add_argument("--limit", type=int, default=10)
        command.add_argument("--after", type=int, default=0, help="Exclusive store event sequence cursor")
        command.add_argument("--expected-state", help="State token; required when --after is positive")
        command.add_argument("--max-chars", type=int, default=60000)
    args = parser.parse_args(argv)
    try:
        options = {key: value for key, value in vars(args).items() if key not in ("root", "command")}
        packet = read_snapshot(args.root).query(**options)
        output = (json_output(packet) if args.command == "query"
                  else export_markdown(packet, max_chars=args.max_chars))
        sys.stdout.write(output)
        return 0
    except (contract.ReviewError, OSError, ValueError, TypeError, RecursionError) as error:
        message = str(error) if isinstance(error, contract.ReviewError) else type(error).__name__
        print("event-reader: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
