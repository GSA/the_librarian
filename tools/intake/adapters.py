"""Fixed native stage adapters; no executable request-defined callbacks."""
from contextlib import closing
import errno
import json
from pathlib import Path
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from zipfile import BadZipFile

from tools.catalog import catalog_store as catalog
from tools.review_events import event_store as events
from tools.extraction import docling_adapter as docling
from tools.extraction import extract
from tools.context_index import context_index as index
from . import standing
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
        # Concurrent catalog writers: a lost race retries; an overlapping write cannot.
        if isinstance(current, catalog.ConflictingWrite):
            return StageFailure("blocked", "catalog-conflict")
        if (isinstance(current, (catalog.StaleState, catalog.WriterBusy))
                or isinstance(current, docling.AdapterError) and current.code == "catalog-stale-state"):
            return StageFailure("retryable", "catalog-busy" if isinstance(current, catalog.WriterBusy)
                                else "catalog-stale-state")
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


STANDING_REVIEW = ("policy_path", "policy_sha256", "rendition", "rendition_sha256",
                   "findings_path", "findings_sha256", "event_id")


def standing_intent(root, stage):
    """Evaluate a standing-policy stage; the outcome is pinned before any write."""
    args = {key: stage["arguments"][key] for key in STANDING_REVIEW}
    result = standing.evaluate(root, args)
    result.pop("child", None)
    return {"operation": stage["operation"], **result}


def standing_publish_guard(root, args):
    policy = standing.load_policy(root, args["policy_path"], args["policy_sha256"])
    if policy["status"] != "active":
        raise standing.Refusal("blocked", "standing-exception-policy-revoked")
    pinned = {"policy_id": policy["policy_id"], "version": policy["version"],
              "sha256": args["policy_sha256"]}
    standing.authorize_publication(root, args["manifest"], policy, pinned)
    return pinned


def extract_intent(operation, root, prepared):
    """The pinned identity of one ``extract.bundle`` result."""
    _, _, path, value = prepared
    return {"operation": operation, "run_id": value["run_id"], "source": value["source"],
            "rendition": path.relative_to(root).as_posix(), "extractor": value["extractor"]}


def prepare(root, stage):
    try:
        return _prepare(root, stage)
    except standing.Refusal as error:
        raise StageFailure(error.category, error.code) from error


def _prepare(root, stage):
    try:
        args = stage["arguments"]
        operation = stage["operation"]
        if operation in {"standing-review", "standing-select"}:
            intent = standing_intent(root, stage)
            if operation == "standing-review" and intent["outcome"] == "qualified":
                # Fixed once in the saved intent so a crash-after-commit retry
                # resubmits the identical event under the same request ID. A
                # correction must be recorded strictly after what it supersedes.
                recorded = standing.datetime.now(standing.timezone.utc).replace(microsecond=0)
                for predecessor in intent.get("supersedes", ()):
                    prior = standing.datetime.fromisoformat(
                        events.status(root, predecessor)["event"]["recorded_at"])
                    recorded = max(recorded, prior.replace(microsecond=0) + standing.timedelta(seconds=1))
                intent["recorded_at"] = recorded.isoformat()
            return intent
        if operation == "standing-publish":
            pinned = standing_publish_guard(root, args)
            return {**_prepare(root, {**stage, "operation": "index-build"}),
                    "operation": operation, "standing_policy": pinned}
        if operation == "await-ack":
            path = catalog.safe_path(root / args["disposition_path"])
            raw = docling.bridge._read_source(path)
            require(len(raw) <= 1024 * 1024 and docling.digest(raw) == args["disposition_sha256"], "changed_disposition")
            return {"operation": operation, "delivery_correlation": args["delivery_correlation"], "disposition_sha256": args["disposition_sha256"]}
        if operation == "index-build":
            manifest = index.no_symlinks(root / args["manifest"])
            config, manifest_hash = index.load_manifest(manifest, root)
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
        if operation.startswith("extract-"):
            return extract_intent(operation, root, extract.bundle(root, args["receipt"], args["format_name"]))
        if operation.startswith("docling-"):
            _, _, path, value, _ = docling._bundle(root, args["receipt"], args["format_name"], Path(args["artifacts_path"]), Path(args["python"]))
            return {"operation": operation, "run_id": value["run_id"], "source": value["source"],
                    "rendition": path.relative_to(root).as_posix(), "extractor": value["extractor"]}
        raise StageFailure("blocked", "unsupported-stage")
    except (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError,
            index.IndexError_, extract.ExtractionError, BadZipFile, ET.ParseError) as error:
        raise native_failure(error) from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Undecodable native inputs block durably rather than escaping as a crash.
        raise StageFailure("blocked", "native-input") from error


def execute(root, stage, intent, previous=None):
    try:
        return _execute(root, stage, intent, previous)
    except standing.Refusal as error:
        raise StageFailure(error.category, error.code) from error


def _standing_result(root, stage, intent):
    """Re-evaluate against current inputs; stale policy, source or findings refuse."""
    args = {key: stage["arguments"][key] for key in STANDING_REVIEW}
    result = standing.evaluate(root, args)
    current = {"operation": stage["operation"], **{k: v for k, v in result.items() if k != "child"}}
    expected = {k: v for k, v in intent.items() if k != "recorded_at"}
    if current != expected:
        raise StageFailure("blocked", "changed-standing-inputs")
    standing.refusal(result)
    return args, result


def _execute(root, stage, intent, previous=None, extra_guard=None):
    try:
        args = stage["arguments"]
        operation = stage["operation"]
        if operation == "standing-review":
            review_args, result = _standing_result(root, stage, intent)
            event = standing.review_event(result, review_args,
                                          standing.datetime.fromisoformat(intent["recorded_at"]))
            state = events.status(root)
            committed = events.append(root, events.bundle([event]), result["reviewer"], args["request_id"],
                                      state["revision"], state["state_token"])
            if standing.bound_event(root, result, review_args) is None:
                raise StageFailure("blocked", "review-event-mismatch")
            return {"boundary": "review-committed", "outcome": "qualified",
                    "event_ids": [args["event_id"]], "request_id": args["request_id"],
                    "policy": result["policy"], "idempotent": committed["idempotent"]}
        if operation == "standing-select":
            review_args, result = _standing_result(root, stage, intent)
            if standing.bound_event(root, result, review_args) is None:
                raise StageFailure("blocked", "review-event-missing")
            child = standing.selected_child(result, review_args)
            submitted = catalog.submit(
                root, [child], actor=result["reviewer"], request_id=args["request_id"],
                expected_state=catalog.status(root)["state_token"],
                reason=child["data"]["reason"])
            entry = standing.selection_entry(result, review_args)
            manifest_sha = standing.write_manifest(root, args["manifest"], entry,
                                                   result.get("supersedes", ()))
            return {"boundary": "selection-committed", "outcome": "qualified",
                    "catalog_revision": submitted["committed_revision"],
                    "request_id": args["request_id"], "selection": entry,
                    "manifest_bytes_sha256": manifest_sha, "policy": result["policy"]}
        if operation == "standing-publish":
            publish = {**stage, "operation": "index-build"}
            index_intent = {**{k: v for k, v in intent.items() if k != "standing_policy"},
                            "operation": "index-build"}

            def guard():
                require(standing_publish_guard(root, args) == intent["standing_policy"],
                        "changed_publication_inputs")
            guard()
            evidence = _execute(root, publish, index_intent, previous, guard)
            guard()
            return {**evidence, "standing_policy": intent["standing_policy"]}
        if operation == "await-ack":
            require(prepare(root, stage) == intent, "changed_disposition")
            return {"boundary": "return-pending", **intent}
        if operation == "index-build":
            require(prepare(root, stage) == intent, "changed_publication_inputs")
            manifest = root / args["manifest"]
            db = index.validate_db_path(root, root / args["db"])
            config, checksum = index.load_manifest(manifest, root)
            with index.publication_lock(db):
                fresh = False
                if db.exists():
                    with closing(index.readonly(db)) as connection:
                        fresh = not index.freshness(connection, root, manifest, db, config, checksum)

                def guard():
                    require(prepare(root, stage) == intent, "changed_publication_inputs")
                    if extra_guard is not None:
                        extra_guard()
                if not fresh:
                    index.build(root, manifest, db, config, checksum, publication_guard=guard)
                require(prepare(root, stage) == intent, "changed_publication_inputs")
                with closing(index.readonly(db)) as connection:
                    require(not index.freshness(connection, root, manifest, db, config, checksum), "stale_publication")
            return {"boundary": "publication-confirmed", "selection_validated": True,
                    "manifest_sha256": checksum, "metadata": intent["metadata"], "database": args["db"]}
        if operation == "catalog-submit":
            # The pinned state may be outdated by another writer; the commit still
            # refuses when a later revision wrote one of these records.
            result = catalog.submit(root, **args, rebase=True)
            return {"boundary": "receipt-committed", "committed_revision": result["committed_revision"],
                    "request_id": args["request_id"], "record_ids": [r["id"] for r in args["records"]],
                    "receipt_ids": [r["id"] for r in args["records"] if r["kind"] == "receipt"],
                    "committed_state": result["committed_state"], "backup": result["backup"]}
        if operation == "review-append":
            result = events.append(root, **args)
            # The current store revision is not the original request revision.
            with events.writer_lock(events.locations(root)[0]), closing(events.connect(events.locations(root)[1])) as c:
                events.inspect(c, root, reuse=True)
                batch = c.execute("SELECT revision FROM batches WHERE request_id=?", (args["request_id"],)).fetchone()
                revision = batch[0]
            return {"boundary": "review-committed", "committed_revision": revision,
                    "event_ids": [e["event_id"] for e in args["value"]["events"]],
                    "request_id": args["request_id"], "backup": result["backup"]}
        if operation.startswith("extract-"):
            # Refuse a changed source/extractor before any publication or registration. The bundle
            # read for this check is the one ``run`` uses; ``run`` still re-reads and re-hashes the
            # source and recomputes the extractor identity under the catalog writer lock.
            prepared = extract.bundle(root, args["receipt"], args["format_name"])
            require(extract_intent(operation, root, prepared) == intent, "changed_extraction_identity")
            command = operation.removeprefix("extract-")
            result = extract.run(root, command, args["receipt"], args["format_name"],
                                 args.get("actor"), args.get("expected_state"), rebase=True,
                                 prepared=prepared)
            require(result["run_id"] == intent["run_id"] and result["rendition"] == intent["rendition"],
                    "changed_extraction_identity")
            evidence = {"boundary": "extraction-" + command, "run_id": result["run_id"],
                        "rendition": result["rendition"], "rendition_sha256": result["rendition_sha256"],
                        "source_sha256": result["source_sha256"]}
            if command == "register":
                evidence["request_id"] = result["request_id"]
            return evidence
        if operation.startswith("docling-"):
            # The native lifecycle validates the pinned identity against its own
            # snapshot, so one conversion per attempt decides the effect.
            identity = {key: intent[key] for key in ("run_id", "source", "extractor")}
            result = docling.run(root, operation.removeprefix("docling-"), args["receipt"], args["format_name"],
                                 Path(args["artifacts_path"]), Path(args["python"]),
                                 actor=args.get("actor"), expected_state=args.get("expected_state"),
                                 expected_identity=identity, rebase=True)
            require(result["run_id"] == intent["run_id"] and result["rendition"] == intent["rendition"], "changed_extraction_identity")
            evidence = {"boundary": "extraction-" + operation.removeprefix("docling-"),
                        "run_id": result["run_id"], "rendition": result["rendition"],
                        "rendition_sha256": result["rendition_sha256"], "source_sha256": result["source_sha256"]}
            if "catalog" in result:
                evidence.update(committed_revision=result["catalog"]["committed_revision"], backup=result["catalog"]["backup"])
            return evidence
        raise StageFailure("blocked", "unsupported-stage")
    except (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError,
            index.IndexError_, extract.ExtractionError, BadZipFile, ET.ParseError) as error:
        raise native_failure(error) from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Undecodable native inputs block durably rather than escaping as a crash.
        raise StageFailure("blocked", "native-input") from error


def availability(root, stage, evidence):
    if stage["operation"] not in {"index-build", "standing-publish"}:
        return {"available": False, "reason": "not-publication"}
    try:
        if stage["operation"] == "standing-publish":
            try:
                pinned = standing_publish_guard(root, stage["arguments"])
            except standing.Refusal:
                return {"available": False, "reason": "standing-policy-stale-or-revoked",
                        "scope": "selected-index"}
            if pinned != evidence.get("standing_policy"):
                return {"available": False, "reason": "standing-policy-stale-or-revoked",
                        "scope": "selected-index"}
        manifest = index.no_symlinks(root / stage["arguments"]["manifest"])
        config, checksum = index.load_manifest(manifest, root)
        db = index.validate_db_path(root, root / stage["arguments"]["db"])
        with closing(index.readonly(db)) as connection:
            fresh = not index.freshness(connection, root, manifest, db, config, checksum)
        return {"available": fresh and checksum == evidence["manifest_sha256"]
                and index.metadata_expected(root, checksum, config) == evidence["metadata"],
                "scope": "selected-index"}
    except (OSError, ValueError, index.IndexError_, KeyError, sqlite3.Error):
        return {"available": False, "reason": "unavailable-or-stale", "scope": "selected-index"}
