"""Fixed native stage adapters; no executable request-defined callbacks."""
from contextlib import closing
import errno
import json
from pathlib import Path
import sqlite3
import subprocess

from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events
from tools.extraction import docling_adapter as docling
from tools.context_index import context_index as index
from .contracts import IntakeError, identifier, require


class StageFailure(Exception):
    def __init__(self, category, code):
        require(category in {"retryable", "blocked", "permanent"}, "invalid_failure")
        identifier(code)
        self.category, self.code = category, code
        super().__init__(code)


def native_failure(error):
    """Classify wrapped native failures without persisting private diagnostics."""
    current = error
    while current is not None:
        if (isinstance(current, subprocess.TimeoutExpired)
                or isinstance(current, docling.AdapterError) and current.code == "timeout"):
            return StageFailure("retryable", "native-timeout")
        if isinstance(current, BlockingIOError):
            return StageFailure("retryable", "native-busy")
        if (isinstance(current, OSError) and current.errno in
                {errno.EAGAIN, errno.EBUSY, errno.ENOSPC, errno.EIO, errno.ETIMEDOUT, errno.EINTR}
                or isinstance(current, sqlite3.OperationalError)):
            return StageFailure("retryable", "native-transient")
        current = current.__cause__ or current.__context__
    return StageFailure("blocked", "native-input")


def prepare(root, stage):
    try:
        args = stage["arguments"]
        operation = stage["operation"]
        if operation == "await-ack":
            path = catalog.safe_path(root / args["disposition_path"])
            raw = docling.bridge._read_source(path)
            require(len(raw) <= 1024 * 1024 and docling.digest(raw) == args["disposition_sha256"], "changed_disposition")
            return {"operation": operation, "delivery_correlation": args["delivery_correlation"], "disposition_sha256": args["disposition_sha256"]}
        if operation == "index-build":
            manifest = index.no_symlinks(root / args["manifest"])
            config, manifest_hash = index.load_manifest(manifest)
            index.validate_db_path(root, root / args["db"])
            return {"operation": operation, "manifest_sha256": manifest_hash,
                    "manifest_bytes_sha256": index.digest(manifest.read_bytes()),
                    "sources": index.snapshot_hashes(root, config),
                    "metadata": index.metadata_expected(root, manifest_hash, config),
                    "selection_validated": True}
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
    except (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError, index.IndexError_) as error:
        raise native_failure(error) from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Undecodable native inputs block durably rather than escaping as a crash.
        raise StageFailure("blocked", "native-input") from error


def execute(root, stage, intent, previous=None):
    try:
        args = stage["arguments"]
        operation = stage["operation"]
        if operation == "await-ack":
            require(prepare(root, stage) == intent, "changed_disposition")
            return {"boundary": "return-pending", **intent}
        if operation == "index-build":
            require(prepare(root, stage) == intent, "changed_publication_inputs")
            manifest = root / args["manifest"]
            db = index.validate_db_path(root, root / args["db"])
            config, checksum = index.load_manifest(manifest)
            with index.publication_lock(db):
                fresh = False
                if db.exists():
                    with closing(index.readonly(db)) as connection:
                        fresh = not index.freshness(connection, root, manifest, db, config, checksum)
                if not fresh:
                    index.build(root, manifest, db, config, checksum,
                                publication_guard=lambda: require(prepare(root, stage) == intent, "changed_publication_inputs"))
                require(prepare(root, stage) == intent, "changed_publication_inputs")
                with closing(index.readonly(db)) as connection:
                    require(not index.freshness(connection, root, manifest, db, config, checksum), "stale_publication")
            return {"boundary": "publication-confirmed", "selection_validated": True,
                    "manifest_sha256": checksum, "metadata": intent["metadata"], "database": args["db"]}
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
            # The native lifecycle validates the pinned identity against its own
            # snapshot, so one conversion per attempt decides the effect.
            identity = {key: intent[key] for key in ("run_id", "source", "extractor")}
            result = docling.run(root, operation.removeprefix("docling-"), args["receipt"], args["format_name"],
                                 Path(args["artifacts_path"]), Path(args["python"]),
                                 actor=args.get("actor"), expected_state=args.get("expected_state"),
                                 expected_identity=identity)
            require(result["run_id"] == intent["run_id"] and result["rendition"] == intent["rendition"], "changed_extraction_identity")
            evidence = {"boundary": "extraction-" + operation.removeprefix("docling-"),
                        "run_id": result["run_id"], "rendition": result["rendition"],
                        "rendition_sha256": result["rendition_sha256"], "source_sha256": result["source_sha256"]}
            if "catalog" in result:
                evidence.update(committed_revision=result["catalog"]["committed_revision"], backup=result["catalog"]["backup"])
            return evidence
        raise StageFailure("blocked", "unsupported-stage")
    except (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError, index.IndexError_) as error:
        raise native_failure(error) from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Undecodable native inputs block durably rather than escaping as a crash.
        raise StageFailure("blocked", "native-input") from error


def availability(root, stage, evidence):
    if stage["operation"] != "index-build":
        return {"available": False, "reason": "not-publication"}
    try:
        manifest = index.no_symlinks(root / stage["arguments"]["manifest"])
        config, checksum = index.load_manifest(manifest)
        db = index.validate_db_path(root, root / stage["arguments"]["db"])
        with closing(index.readonly(db)) as connection:
            fresh = not index.freshness(connection, root, manifest, db, config, checksum)
        return {"available": fresh and checksum == evidence["manifest_sha256"]
                and index.metadata_expected(root, checksum, config) == evidence["metadata"],
                "scope": "selected-index"}
    except (OSError, ValueError, index.IndexError_, KeyError, sqlite3.Error):
        return {"available": False, "reason": "unavailable-or-stale", "scope": "selected-index"}
