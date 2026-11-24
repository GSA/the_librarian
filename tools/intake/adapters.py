"""Fixed native stage adapters; no executable request-defined callbacks."""
from contextlib import ExitStack, closing, nullcontext
from datetime import timedelta
import errno
import json
from pathlib import Path
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from zipfile import BadZipFile

from tools import durable
from tools.catalog import catalog_store as catalog
from tools.review_events import event_reader
from tools.review_events import event_history, event_store as events
from tools.extraction import convert, docling_adapter as docling
from tools.extraction import extract
from tools.extraction import pdf_image, pdf_pages
from tools.context_index import context_index as index
from . import standing, standing_events, standing_publication
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
        # A refused PDF/image conversion names its own durable code (``extract-encrypted``, ...).
        if isinstance(current, pdf_image.ConversionFailed):
            return StageFailure("blocked", current.code)
        # A converter refusal (container, encryption, limits, timeout) keeps its precise code.
        if isinstance(current, convert.ConversionError):
            return StageFailure("blocked", current.code)
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


# Exceptions the catch-all must leave alone: the runner classifies them (contention, the ledger's own
# refusals), and an unreadable catalog deliberately stops the run rather than failing one file.
PASS_THROUGH = (StageFailure, standing.Refusal, standing.CatalogUnavailable, OSError, sqlite3.Error, IntakeError)


def unexpected_failure(error):
    """The failure recorded for an exception no adapter anticipated (a parser bug, an unsupported member,
    exhausted recursion): permanent, so the stage dead-letters with a stable code and the run goes on.

    A recognised cause (a timeout, a lost catalog race) keeps the precise failure ``native_failure``
    gives it; only the catch-all ``native-input`` is replaced. The exception text is never persisted.
    """
    failure = native_failure(error)
    if failure.code != "native-input":
        return failure
    return StageFailure("permanent", "native-unexpected")


STANDING_REVIEW = ("policy_path", "policy_sha256", "rendition", "rendition_sha256",
                   "findings_path", "findings_sha256", "event_id")


def standing_arguments(arguments):
    """The pinned standing-review arguments of a stage, plus ``deferred`` when the operator opted
    in (requests staged without it keep the ``hold`` default)."""
    args = {key: arguments[key] for key in STANDING_REVIEW}
    if "deferred" in arguments:
        args["deferred"] = arguments["deferred"]
    return args


class OneRead:
    """A review-event snapshot read at most once, on first use, shared by the steps of one stage.

    Calling it returns the snapshot; an unreadable store refuses exactly as ``standing.evaluate``
    does when it reads for itself."""

    def __init__(self, root):
        self.root, self.value = root, None

    def __call__(self):
        if self.value is None:
            try:
                self.value = event_reader.read_snapshot(self.root)
            except events.contract.ReviewError as error:
                raise standing.Refusal("blocked", "review-store-unavailable") from error
        return self.value


def standing_intent(root, stage, snapshot=None):
    """Evaluate a standing-policy stage; the outcome is pinned before any write."""
    args = standing_arguments(stage["arguments"])
    result = standing.evaluate(root, args, snapshot)
    result.pop("child", None)
    return {"operation": stage["operation"], **result}


def standing_publish_guard(root, args):
    policy = standing.load_policy(root, args["policy_path"], args["policy_sha256"])
    if policy["status"] != "active":
        raise standing.Refusal("blocked", "standing-exception-policy-revoked")
    pinned = {"policy_id": policy["policy_id"], "version": policy["version"],
              "sha256": args["policy_sha256"]}
    standing_publication.authorize_publication(root, args["manifest"], policy, pinned)
    return pinned


def pinned_metadata(intent, checksum):
    """The metadata a ``prepare`` equal to ``intent`` just derived, for the manifest read as ``checksum``.

    Reusing it spares a second validation of every selection per freshness check. It applies only
    to the very manifest the intent pins; any other manifest recomputes (``None``).
    """
    return intent["metadata"] if intent.get("manifest_sha256") == checksum else None


def extractor_for(format_name):
    """The extraction lifecycle module for ``format_name``: PDF pages, images, the office/RTF
    document converter or ``extract``."""
    if format_name in pdf_pages.FORMATS:
        return pdf_pages
    if format_name in pdf_image.FORMATS:
        return pdf_image
    return convert if format_name in convert.FORMATS else extract


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
            snapshot = OneRead(root)
            intent = standing_intent(root, stage, snapshot)
            if operation == "standing-review" and intent["outcome"] == "qualified":
                # Fixed once in the saved intent so a crash-after-commit retry
                # resubmits the identical event under the same request ID. A
                # correction must be recorded strictly after what it supersedes.
                recorded = standing_events.datetime.now(standing_events.timezone.utc).replace(microsecond=0)
                for predecessor in intent.get("supersedes", ()):
                    prior = standing_events.datetime.fromisoformat(snapshot().event(predecessor)["recorded_at"])
                    recorded = max(recorded, prior.replace(microsecond=0) + timedelta(seconds=1))
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
            module = extractor_for(args["format_name"])
            return extract_intent(operation, root, module.bundle(root, args["receipt"], args["format_name"]))
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
    except PASS_THROUGH:
        raise  # the runner classifies these itself, or (an unreadable catalog) they stop the run by design
    except Exception as error:  # noqa: BLE001 - one file's unexpected failure must not end the run
        raise unexpected_failure(error) from error


def execute(root, stage, intent, previous=None):
    try:
        return _execute(root, stage, intent, previous)
    except standing.Refusal as error:
        raise StageFailure(error.category, error.code) from error


def _standing_result(root, stage, intent, snapshot=None):
    """Re-evaluate against current inputs; stale policy, source or findings refuse.

    ``snapshot`` is the one review-event read the caller shares across this stage's steps."""
    args = standing_arguments(stage["arguments"])
    result = standing.evaluate(root, args, snapshot)
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
            # One read serves the evaluation and the expected state; the commit and the binding check
            # after it each read what they must see (Kata m0tg).
            snapshot = OneRead(root)
            review_args, result = _standing_result(root, stage, intent, snapshot)
            event = standing_events.review_event(
                result, review_args, standing_events.datetime.fromisoformat(intent["recorded_at"]))
            state = snapshot().state
            committed = events.append(root, event_history.bundle([event]), result["reviewer"], args["request_id"],
                                      state["revision"], state["state_token"])
            if standing_events.bound_event(root, result, review_args) is None:
                raise StageFailure("blocked", "review-event-mismatch")
            return {"boundary": "review-committed", "outcome": "qualified",
                    "event_ids": [args["event_id"]], "request_id": args["request_id"],
                    "policy": result["policy"], "idempotent": committed["idempotent"]}
        if operation == "standing-select":
            snapshot = OneRead(root)
            review_args, result = _standing_result(root, stage, intent, snapshot)
            if standing_events.bound_event(root, result, review_args, snapshot()) is None:
                raise StageFailure("blocked", "review-event-missing")
            child = standing_publication.selected_child(result, review_args)
            submitted = catalog.submit(
                root, [child], actor=result["reviewer"], request_id=args["request_id"],
                expected_state=catalog.status(root)["state_token"],
                reason=child["data"]["reason"])
            entry = standing_publication.selection_entry(result, review_args)
            manifest_sha = standing_publication.write_manifest(root, args["manifest"], entry,
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
                        fresh = not index.freshness(connection, root, manifest, db, config, checksum,
                                                    pinned_metadata(intent, checksum))

                def guard():
                    require(prepare(root, stage) == intent, "changed_publication_inputs")
                    if extra_guard is not None:
                        extra_guard()
                if not fresh:
                    index.build(root, manifest, db, config, checksum, publication_guard=guard)
                require(prepare(root, stage) == intent, "changed_publication_inputs")
                with closing(index.readonly(db)) as connection:
                    require(not index.freshness(connection, root, manifest, db, config, checksum,
                                                pinned_metadata(intent, checksum)), "stale_publication")
            return {"boundary": "publication-confirmed", "selection_validated": True,
                    "manifest_sha256": checksum, "metadata": intent["metadata"], "database": args["db"]}
        if operation == "catalog-submit":
            # The pinned state may be outdated by another writer; the commit still
            # refuses when a later revision wrote one of these records.
            result = catalog.submit(root, **args, rebase=True, marker=receipt_marker(args))
            return {"boundary": "receipt-committed", "committed_revision": result["committed_revision"],
                    "request_id": args["request_id"], "record_ids": [r["id"] for r in args["records"]],
                    "receipt_ids": [r["id"] for r in args["records"] if r["kind"] == "receipt"],
                    "committed_state": result["committed_state"], "backup": result["backup"]}
        if operation == "review-append":
            result = events.append(root, **args)
            # The current store revision is not the original request revision.
            with events.writer_lock(events.locations(root)[0]), closing(events.connect(events.locations(root)[1])) as c:
                event_history.inspect(c, root, reuse=True)
                batch = c.execute("SELECT revision FROM batches WHERE request_id=?", (args["request_id"],)).fetchone()
                revision = batch[0]
            return {"boundary": "review-committed", "committed_revision": revision,
                    "event_ids": [e["event_id"] for e in args["value"]["events"]],
                    "request_id": args["request_id"], "backup": result["backup"]}
        if operation.startswith("extract-"):
            # Refuse a changed source/extractor before any publication or registration. The bundle
            # read for this check is the one ``run`` uses; ``run`` still re-reads and re-hashes the
            # source and recomputes the extractor identity under the catalog writer lock.
            module = extractor_for(args["format_name"])
            prepared = module.bundle(root, args["receipt"], args["format_name"])
            require(extract_intent(operation, root, prepared) == intent, "changed_extraction_identity")
            command = operation.removeprefix("extract-")
            result = module.run(root, command, args["receipt"], args["format_name"],
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
    except PASS_THROUGH:
        raise  # the runner classifies these itself, or (an unreadable catalog) they stop the run by design
    except Exception as error:  # noqa: BLE001 - one file's unexpected failure must not end the run
        raise unexpected_failure(error) from error


def receipt_marker(args):
    """The record ID that makes an upload registration idempotent across batch revisions, or None.

    Only a manual-upload registration has one: its request ID is ``upload-<submission>`` and it creates
    the receipt and the original artifact ``A-upload-<submission>-original`` that the receipt names, a
    record no other request creates. Any other ``catalog-submit`` (a receipt update that merely carries
    the original artifact, say) has none and stays idempotent by request ID alone, so its changes are
    always written.
    """
    request_id, records = args["request_id"], args["records"]
    if not request_id.startswith("upload-"):
        return None
    marker = "A-" + request_id + "-original"
    receipts = [r for r in records if r["kind"] == "receipt"]
    if len(receipts) != 1 or receipts[0]["data"]["artifact_id"] != marker:
        return None
    return marker if any(r["id"] == marker for r in records) else None


BATCH_OPERATIONS = ("catalog-submit", "extract-register", "extract-prepare")
NATIVE_ERRORS = (catalog.CatalogError, events.contract.ReviewError, docling.AdapterError, docling.bridge.BridgeError,
                 index.IndexError_, extract.ExtractionError, BadZipFile, ET.ParseError)


def batch_scope(operation, count):
    """What a batch of ``count`` stages of ``operation`` runs inside (a parse memo for registrations)."""
    if operation not in {"extract-register", "extract-prepare"}:
        return nullcontext()
    scope = ExitStack()
    scope.enter_context(extract.keeping_parsed(count))
    scope.enter_context(convert.keeping_parsed(count))
    return scope


def _failure_of(error):
    """The failure ``_execute`` would raise for ``error`` (native families classified), else the error itself."""
    if isinstance(error, NATIVE_ERRORS):
        failure = native_failure(error)
        failure.__cause__ = error
        return failure
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        failure = StageFailure("blocked", "native-input")
        failure.__cause__ = error
        return failure
    if isinstance(error, PASS_THROUGH):
        return error
    failure = unexpected_failure(error)
    failure.__cause__ = error
    return failure


def execute_batch(root, entries):
    """``execute`` for many stages of one operation; one entry per stage, evidence or its failure.

    ``entries`` are ``(stage, intent, previous)``. Each result is the evidence ``execute`` would return
    or the exception that would have ended that stage alone (a ``StageFailure``, ``OSError``,
    ``sqlite3.Error`` or ``IntakeError``); one stage's refusal never decides another's. Registrations
    commit together as one catalog revision; everything else a stage checks is checked per stage.
    """
    operations = {stage["operation"] for stage, _, _ in entries}
    require(len(operations) == 1 and operations <= set(BATCH_OPERATIONS), "unsupported_batch")
    operation = operations.pop()
    outcomes = [None] * len(entries)

    def single(index):
        stage, intent, previous = entries[index]
        try:
            outcomes[index] = _execute(root, stage, intent, previous)
        except (StageFailure, OSError, sqlite3.Error, IntakeError) as error:
            outcomes[index] = error

    if operation == "extract-prepare":
        # Publishes each rendition under its own run directory; nothing is shared, so each stage runs alone
        # (the parse memo of ``batch_scope`` serves the guards and the registration that follow).
        # Their directory entries are synced once, before the ledger records the stages as prepared.
        with durable.batched_directory_syncs() as syncs:
            for index in range(len(entries)):
                single(index)
            try:
                syncs.flush()
            except OSError as error:
                # Files whose entries cannot be made durable are not prepared: each stage that would have
                # succeeded fails (retryably, as the same error from its own sync always did) and the rest stand.
                for index, outcome in enumerate(outcomes):
                    if not isinstance(outcome, Exception):
                        outcomes[index] = error
        return outcomes
    if operation == "catalog-submit":
        groups = {}
        for index, (stage, intent, previous) in enumerate(entries):
            args = stage["arguments"]
            if receipt_marker(args) is None:
                single(index)
            else:
                groups.setdefault((args["actor"], args["reason"]), []).append(index)
        for (actor, reason), indexes in groups.items():
            members = [{"marker": receipt_marker(entries[i][0]["arguments"]),
                        "records": entries[i][0]["arguments"]["records"],
                        "expected_state": entries[i][0]["arguments"]["expected_state"]} for i in indexes]
            try:
                submitted = catalog.submit_batch(root, members, actor=actor, reason=reason, rebase=True)
            except (OSError, sqlite3.Error) as error:
                for i in indexes:
                    outcomes[i] = error
                continue
            except NATIVE_ERRORS as error:
                for i in indexes:
                    outcomes[i] = _failure_of(error)
                continue
            first = True
            for i, member in zip(indexes, submitted["members"]):
                args = entries[i][0]["arguments"]
                if member["status"] == "refused":
                    outcomes[i] = _failure_of(member["error"])
                    continue
                outcomes[i] = {"boundary": "receipt-committed", "committed_revision": member["committed_revision"],
                               "request_id": args["request_id"], "record_ids": [r["id"] for r in args["records"]],
                               "receipt_ids": [r["id"] for r in args["records"] if r["kind"] == "receipt"],
                               "committed_state": member["committed_state"],
                               "backup": submitted["backup"] if first and member["status"] == "committed" else None}
                first = first and member["status"] != "committed"
        return outcomes
    items, owners = [], []
    for index, (stage, intent, previous) in enumerate(entries):
        args = stage["arguments"]
        try:
            # The same guard ``_execute`` makes before it registers: the source, extractor and catalog
            # are read afresh and must still be the pinned identity.
            prepared = extractor_for(args["format_name"]).bundle(root, args["receipt"], args["format_name"])
            require(extract_intent(operation, root, prepared) == intent, "changed_extraction_identity")
        except NATIVE_ERRORS as error:
            outcomes[index] = _failure_of(error)
            continue
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            outcomes[index] = _failure_of(error)
            continue
        except PASS_THROUGH as error:
            outcomes[index] = error
            continue
        except Exception as error:  # noqa: BLE001 - this stage's unexpected failure, recorded on it alone
            outcomes[index] = unexpected_failure(error)
            continue
        items.append({"receipt": args["receipt"], "format_name": args["format_name"], "actor": args.get("actor"),
                      "expected_state": args.get("expected_state"), "prepared": prepared})
        owners.append(index)
    registered = {}
    # Text-family, PDF, image and converted renditions register through their own lifecycle, one batch each.
    for module in (extract, pdf_image, pdf_pages, convert):
        chosen = [(owner, item) for owner, item in zip(owners, items)
                  if extractor_for(item["format_name"]) is module]
        if not chosen:
            continue
        try:
            results = module.register_batch(root, [item for _, item in chosen])
        except Exception as error:  # noqa: BLE001 - unexpected faults are each stage's failure too
            # A failure of the whole batch (the extraction lock held too long, say) is each stage's failure,
            # classified like the single path's, so its attempt is recorded and contention is retried.
            results = [error] * len(chosen)
        registered.update(zip((owner for owner, _ in chosen), results))
    for index, result in registered.items():
        _, intent, _ = entries[index]
        if isinstance(result, (OSError, sqlite3.Error)):
            outcomes[index] = result
            continue
        if isinstance(result, Exception):
            outcomes[index] = _failure_of(result)
            continue
        try:
            require(result["run_id"] == intent["run_id"] and result["rendition"] == intent["rendition"],
                    "changed_extraction_identity")
        except IntakeError as error:
            outcomes[index] = error
            continue
        outcomes[index] = {"boundary": "extraction-register", "run_id": result["run_id"],
                           "rendition": result["rendition"], "rendition_sha256": result["rendition_sha256"],
                           "source_sha256": result["source_sha256"], "request_id": result["request_id"],
                           "committed_revision": result["committed_revision"]}
    return outcomes


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
            # One validation of the selection serves both the freshness check and the comparison.
            expected = index.metadata_expected(root, checksum, config)
            fresh = not index.freshness(connection, root, manifest, db, config, checksum, expected)
        return {"available": fresh and checksum == evidence["manifest_sha256"]
                and expected == evidence["metadata"],
                "scope": "selected-index"}
    except (OSError, ValueError, index.IndexError_, KeyError, sqlite3.Error):
        return {"available": False, "reason": "unavailable-or-stale", "scope": "selected-index"}
