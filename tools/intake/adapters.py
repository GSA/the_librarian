"""Fixed native stage adapters; no executable request-defined callbacks."""
from contextlib import closing
from pathlib import Path

from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events
from tools.extraction import docling_adapter as docling
from .contracts import IntakeError, identifier, require


class StageFailure(Exception):
    def __init__(self, category, code):
        require(category in {"retryable", "blocked", "permanent"}, "invalid_failure")
        identifier(code)
        self.category, self.code = category, code
        super().__init__(code)


def prepare(root, stage):
    try:
        args = stage["arguments"]
        operation = stage["operation"]
        if operation in {"catalog-submit", "review-append"}:
            if operation == "catalog-submit":
                catalog.validate_records(args["records"])
            else:
                events.prepare_submission(args["value"], root)
            return {"operation": operation}
        if operation.startswith("docling-"):
            _, _, path, value, _ = docling._bundle(root, args["receipt"], args["format_name"], Path(args["artifacts_path"]), Path(args["python"]))
            return {"operation": operation, "run_id": value["run_id"], "source": value["source"],
                    "rendition": path.relative_to(root).as_posix(), "extractor": value["extractor"]}
        raise StageFailure("blocked", "unsupported-stage")
    except (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError) as error:
        raise StageFailure("blocked", "native-input") from error


def execute(root, stage, intent, previous=None):
    try:
        args = stage["arguments"]
        operation = stage["operation"]
        if operation == "catalog-submit":
            result = catalog.submit(root, **args)
            return {"boundary": "receipt-committed", "committed_revision": result["committed_revision"],
                    "request_id": args["request_id"], "record_ids": [r["id"] for r in args["records"]],
                    "receipt_ids": [r["id"] for r in args["records"] if r["kind"] == "receipt"],
                    "committed_state": result["committed_state"], "backup": result["backup"]}
        if operation == "review-append":
            result = events.append(root, **args)
            # The current store revision is not the original request revision.
            with events.writer_lock(events.locations(root)[0]), closing(events.connect(events.locations(root)[1])) as c:
                events.inspect(c)
                batch = c.execute("SELECT revision FROM batches WHERE request_id=?", (args["request_id"],)).fetchone()
                revision = batch[0]
            return {"boundary": "review-committed", "committed_revision": revision,
                    "event_ids": [e["event_id"] for e in args["value"]["events"]],
                    "request_id": args["request_id"], "backup": result["backup"]}
        if operation.startswith("docling-"):
            require(prepare(root, stage) == intent, "changed_extraction_identity")
            result = docling.run(root, operation.removeprefix("docling-"), args["receipt"], args["format_name"],
                                 Path(args["artifacts_path"]), Path(args["python"]),
                                 actor=args.get("actor"), expected_state=args.get("expected_state"))
            require(result["run_id"] == intent["run_id"], "changed_extraction_identity")
            evidence = {"boundary": "extraction-" + operation.removeprefix("docling-"),
                        "run_id": result["run_id"], "rendition": result["rendition"],
                        "rendition_sha256": result["rendition_sha256"], "source_sha256": result["source_sha256"]}
            if "catalog" in result:
                evidence.update(committed_revision=result["catalog"]["committed_revision"], backup=result["catalog"]["backup"])
            return evidence
        raise StageFailure("blocked", "unsupported-stage")
    except (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError) as error:
        raise StageFailure("blocked", "native-input") from error


def availability(root, stage, evidence):
    return {"available": False, "reason": "not-publication"}
