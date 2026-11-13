#!/usr/bin/env python3
"""Owner-run A/B of the two ``inspected_records`` forms on a sample of renditions.

For each named rendition this asks the configured model the same review question twice, once
with the reviewer listing every inspected record index (``list``, today's default) and once with
inclusive ranges (``ranges``), and prints a body-free report: per form the completion and prompt
tokens, seconds, complete inspections and failed replies, and across the forms the decision
agreement and whether the parsed inspections are identical. Nothing is written to the instance:
findings are not recorded and no ledger, catalog or event store is touched. Record the report
privately before making ``--inspected-form ranges`` the production default; it is a measurement,
not an acceptability gate (the inspection list is self-attested in either form).

This calls the real model and spends tokens, so run it deliberately, on about 20 representative
renditions (mix short, median and long; include a few the policy rejects or defers):

    python -m tools.diagnostics.inspection_form_ab --root ROOT --models-config FILE \\
        --model MODEL --rendition PATH ... [--rendition-list FILE] [--repeat 1] [--order alternate]

``--rendition-list`` names a file with one instance-relative rendition path per line. ``--order
alternate`` (default) swaps which form goes first on every other rendition so provider warm-up
or load drift does not favor one form. The report holds counts only, no document text.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from tools.intake import model_review

FORMS = model_review.FORMS


def _one(prepared, model, transport, clock):
    started = clock()
    result = model_review.assess(prepared, model, transport)
    seconds = clock() - started
    parsed = result.get("parsed")
    return {"status": result["status"], "code": result.get("code"), "seconds": seconds,
            "usage": result["usage"], "decision": result.get("decision"),
            "complete": bool(result.get("complete")),
            "records": parsed["records"] if parsed else None,
            "warnings": parsed["warnings"] if parsed else None}


def run_ab(root, renditions, model, transport, *, repeat=1, order="alternate", clock=time.monotonic):
    """Run both forms over ``renditions``; return the body-free comparison report."""
    if order not in {"alternate", "list-first", "ranges-first"} or repeat < 1:
        raise ValueError("invalid order or repeat")
    prepared = {name: {form: model_review.task(root, name, form) for form in FORMS}
                for name in renditions}
    rows = []
    for number, name in enumerate(renditions):
        first = {"list-first": FORMS[0], "ranges-first": FORMS[1]}.get(order, FORMS[number % 2])
        sequence = [first, FORMS[1 - FORMS.index(first)]]
        for attempt in range(repeat):
            rows.append({"index": number, "attempt": attempt,
                         "records": len(prepared[name]["list"]["expected"]["records"]),
                         **{form: _one(prepared[name][form], model, transport, clock)
                            for form in sequence}})

    def totals(form):
        entries = [r[form] for r in rows]
        return {"documents": len(entries),
                "reviewed": sum(e["status"] == "reviewed" for e in entries),
                "failed": sum(e["status"] != "reviewed" for e in entries),
                "complete": sum(e["complete"] for e in entries),
                "prompt_tokens": sum(e["usage"]["prompt_tokens"] for e in entries),
                "completion_tokens": sum(e["usage"]["completion_tokens"] for e in entries),
                "seconds": round(sum(e["seconds"] for e in entries), 3),
                "decisions": {d: sum(e["decision"] == d for e in entries)
                              for d in sorted({e["decision"] for e in entries if e["decision"]})}}
    both = [r for r in rows if r["list"]["status"] == r["ranges"]["status"] == "reviewed"]
    agree = sum(r["list"]["decision"] == r["ranges"]["decision"] for r in both)
    same = sum(r["list"]["records"] == r["ranges"]["records"]
               and r["list"]["warnings"] == r["ranges"]["warnings"] for r in both)
    summary = {form: totals(form) for form in FORMS}
    saved = summary["list"]["completion_tokens"] - summary["ranges"]["completion_tokens"]
    return {"documents": len(renditions), "model": model, "repeat": repeat, "order": order,
            "forms": summary,
            "comparison": {
                "paired": len(both), "decision_agreement": agree,
                "decision_disagreement": len(both) - agree, "identical_inspection": same,
                "completion_tokens_saved": saved,
                "completion_tokens_saved_pct": round(100 * saved / summary["list"]["completion_tokens"], 1)
                if summary["list"]["completion_tokens"] else None,
                "seconds_saved": round(summary["list"]["seconds"] - summary["ranges"]["seconds"], 3)},
            "per_document": [{"index": r["index"], "attempt": r["attempt"], "records": r["records"],
                              **{form: {k: r[form][k] for k in ("status", "code", "decision", "complete")}
                                 | {"completion_tokens": r[form]["usage"]["completion_tokens"],
                                    "seconds": round(r[form]["seconds"], 3)} for form in FORMS}}
                             for r in rows]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--models-config", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--rendition", action="append", default=[], dest="renditions")
    parser.add_argument("--rendition-list", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--order", default="alternate", choices=("alternate", "list-first", "ranges-first"))
    args = parser.parse_args(argv)
    renditions = list(args.renditions)
    if args.rendition_list:
        renditions += [line.strip() for line in args.rendition_list.read_text().splitlines()
                       if line.strip() and not line.startswith("#")]
    if not renditions:
        parser.error("name at least one --rendition or --rendition-list")
    report = run_ab(args.root, renditions, args.model, model_review.usai_transport(args.models_config),
                    repeat=args.repeat, order=args.order)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
