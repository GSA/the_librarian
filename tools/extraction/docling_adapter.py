#!/usr/bin/env python3
"""Common offline Docling lifecycle: isolate, identify, publish, verify, register."""

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import select
import threading
import stat
import subprocess
import tempfile
import time
import uuid

from tools.extraction import docling_bridge as bridge


LOADED_CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
VERSION = 1
HOME = Path("data/extractions/.local")
WORKER = Path(__file__).with_name("docling_worker.py")
MAX_RESULT = 64 * 1024 * 1024
MAX_DIAGNOSTIC = 64 * 1024
WALL_SECONDS = 600
TERM_GRACE_SECONDS = 2
BOUNDARY = ("Machine OCR/layout rendition only; not review, indexing, retrieval selection, "
            "independent evidence or publication authorization.")


class AdapterError(ValueError):
    def __init__(self, message, code="adapter_error"):
        super().__init__(message)
        self.code = code


def require(condition, message, code="adapter_error"):
    if not condition:
        raise AdapterError(message, code)


def canonical_bytes(value):
    def reject_numbers(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise AdapterError("Non-finite number in rendition", "invalid_result")
        if isinstance(item, dict):
            for child in item.values():
                reject_numbers(child)
        elif isinstance(item, list):
            for child in item:
                reject_numbers(child)
    reject_numbers(value)
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AdapterError("Duplicate result field", "invalid_result")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              AdapterError("Non-finite result number", "invalid_result")))
    except AdapterError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("Worker returned invalid JSON", "invalid_result") from error


def _validate_executable(path):
    requested = Path(path).absolute()
    try:
        path = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AdapterError("Dedicated Docling interpreter is missing or unsafe",
                           "missing_dependency") from error
    require(path.is_file() and not path.is_symlink(),
            "Dedicated Docling interpreter target is missing or unsafe", "missing_dependency")
    require(os.access(path, os.X_OK), "Dedicated Docling interpreter is not executable",
            "missing_dependency")
    return requested


def _executable_identity(path):
    """Bind the venv invocation path and resolved executable, permitting venv links."""
    path = Path(path)
    target = path.resolve(strict=True)
    return (str(target), _file_identity(path.lstat()), _file_identity(target.stat()))


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read_result(path):
    before = path.stat(follow_symlinks=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode) and stat.S_IMODE(opened.st_mode) == 0o600,
                "Docling worker result is not a private regular file", "invalid_result")
        require(_file_identity(before) == _file_identity(opened),
                "Worker result changed while opening", "mutation")
        raw = stream.read(MAX_RESULT + 1)
        after = os.fstat(stream.fileno())
    require(_file_identity(opened) == _file_identity(after)
            == _file_identity(path.stat(follow_symlinks=False)),
            "Worker result changed while reading", "mutation")
    return raw


def _terminate_group(process):
    # The leader may already have exited while descendants retain this group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # A reaped leader cannot tell us whether descendants obeyed SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _worker_snapshot():
    before = WORKER.stat(follow_symlinks=False)
    require(stat.S_ISREG(before.st_mode), "Docling worker is missing or unsafe")
    descriptor = os.open(WORKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino),
                "Docling worker changed while being opened")
        raw = stream.read(2 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) <= 2 * 1024 * 1024, "Docling worker exceeds code-size limit")
    require((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
             opened.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns), "Docling worker changed while being read")
    return raw, digest(raw)


def _require_worker(expected):
    require(_worker_snapshot()[1] == expected,
            "Docling worker changed during operation; retry", "mutation")


class _DiagnosticCollector:
    """Drain stderr without disk spooling; retain only a bounded tail."""

    def __init__(self, limit):
        self.limit = limit
        self.tail = b""
        self.peak_bytes = 0
        self.stop = threading.Event()

    def __enter__(self):
        self.reader, writer = os.pipe()
        self.stream = os.fdopen(writer, "wb", buffering=0)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()
        return self

    def _drain(self):
        try:
            while True:
                ready, _, _ = select.select([self.reader], [], [], 0.05)
                if not ready:
                    if self.stop.is_set():
                        return
                    continue
                chunk = os.read(self.reader, 8192)
                if not chunk:
                    return
                self.tail = (self.tail + chunk)[-self.limit:]
                self.peak_bytes = max(self.peak_bytes, len(self.tail))
                if self.stop.is_set():
                    # Do not hang on descendants retaining or flooding stderr.
                    return
        finally:
            os.close(self.reader)

    def __exit__(self, *exc):
        self.stream.close()
        # Normal EOF drains all buffered output. Bound cleanup if an inherited
        # write descriptor remains open after worker exit/termination.
        self.thread.join(timeout=TERM_GRACE_SECONDS)
        if self.thread.is_alive():
            self.stop.set()
            self.thread.join()


def _operation(operation, artifacts_path: Path, python: Path, *, source=None,
               name=None, format_name=None):
    require(operation in ("inspect", "convert"), "Unsupported worker operation")
    python = _validate_executable(python)
    artifacts_path = Path(artifacts_path).absolute()
    require(artifacts_path.is_dir() and not artifacts_path.is_symlink(),
            "Docling model directory is missing or unsafe", "missing_assets")
    worker_raw, worker_sha = _worker_snapshot()
    interpreter_identity = _executable_identity(python)
    env = dict(PATH=os.defpath, LANG="C.UTF-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1",
               TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="",
               OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    with tempfile.TemporaryDirectory(prefix="docling-adapter-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        job_path, result_path = home / "job.json", home / "result.json"
        worker_path = home / "docling_worker.py"
        worker_path.write_bytes(worker_raw)
        worker_path.chmod(0o600)
        requirements = WORKER.with_name("requirements-docling.txt")
        requirements_path = home / requirements.name
        requirements_path.write_bytes(requirements.read_bytes())
        requirements_path.chmod(0o600)
        job = {"schema": "docling-worker-job-v1", "operation": operation,
               "artifacts_path": str(artifacts_path)}
        if operation == "convert":
            require(isinstance(source, bytes) and isinstance(name, str) and name,
                    "Conversion requires source bytes and a source name")
            require(format_name in ("pdf", "image"), "Unsupported Docling format")
            source_path = home / "source"
            source_path.write_bytes(source)
            source_path.chmod(0o600)
            job.update(source_path=str(source_path), name=name, format=format_name)
        job_path.write_bytes(canonical_bytes(job))
        job_path.chmod(0o600)
        with _DiagnosticCollector(MAX_DIAGNOSTIC) as diagnostics:
            try:
                process = subprocess.Popen(
                    ["unshare", "-Urn", str(python), "-I", str(worker_path),
                     str(job_path), str(result_path)], stdout=subprocess.DEVNULL,
                    stderr=diagnostics.stream, start_new_session=True, env=env)
            except (FileNotFoundError, PermissionError, OSError) as error:
                raise AdapterError("Network namespace or worker launch unavailable",
                                   "isolation_unavailable") from error
            try:
                require(_executable_identity(python) == interpreter_identity,
                        "Dedicated interpreter changed during launch", "mutation")
                process.wait(timeout=WALL_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise AdapterError("Docling worker exceeded wall timeout", "timeout") from error
            finally:
                _terminate_group(process)
        diagnostic = diagnostics.tail
        require(result_path.exists() and not result_path.is_symlink(),
                "Docling worker produced no safe result" +
                ((": " + diagnostic.decode("utf-8", "replace")) if diagnostic else ""),
                "worker_failed")
        raw = _read_result(result_path)
        require(len(raw) <= MAX_RESULT, "Docling worker result exceeds 64 MiB", "output_limit")
        _require_worker(worker_sha)
        envelope = _decode(raw)
        require(isinstance(envelope, dict) and envelope.get("schema") == "docling-worker-result-v1"
                and type(envelope.get("ok")) is bool, "Invalid worker result envelope",
                "invalid_result")
        if not envelope["ok"]:
            error = envelope.get("error")
            require(isinstance(error, dict) and set(error) == {"code", "message"}
                    and all(isinstance(error[k], str) and error[k] for k in error),
                    "Invalid worker error envelope", "invalid_result")
            raise AdapterError(error["message"], error["code"])
        require(set(envelope) == {"schema", "ok", "payload"},
                "Unexpected worker success fields", "invalid_result")
        require(process.returncode == 0, "Worker exited nonzero with a success result",
                "invalid_result")
        return envelope["payload"], worker_sha


def inspect(artifacts_path: Path, python: Path):
    report, _ = _operation("inspect", artifacts_path, python)
    require(isinstance(report, dict)
            and set(report) == {"schema", "profile", "runtime", "assets"}
            and report["schema"] == "docling-runtime-v1",
            "Invalid worker inspection report", "invalid_result")
    return report


def _convert_with_identity(source: bytes, name: str, format_name: str,
                           artifacts_path: Path, python: Path):
    report, worker_sha = _operation("convert", artifacts_path, python, source=source,
                                    name=name, format_name=format_name)
    require(isinstance(report, dict)
            and set(report) == {"conversion", "runtime", "profile", "assets"},
            "Invalid worker conversion report", "invalid_result")
    return report, worker_sha


def convert(source: bytes, name: str, format_name: str,
            artifacts_path: Path, python: Path):
    return _convert_with_identity(source, name, format_name, artifacts_path, python)[0]


def _bundle(root, receipt, format_name, artifacts_path, python):
    installation = bridge.binding()
    state, original, source_identity, raw = bridge.source(root, receipt, format_name)
    report, worker_sha = _convert_with_identity(
        raw, Path(source_identity["path"]).name, format_name, artifacts_path, python)
    require(bridge.binding() == installation,
            "Policy or extraction code changed during conversion", "mutation")
    _require_worker(worker_sha)
    extractor = {"schema_version": VERSION, "label": "docling-" + format_name,
                 "worker_sha256": worker_sha,
                 "installation": installation, "runtime": report["runtime"],
                 "profile": report["profile"], "assets": report["assets"]}
    run_id = digest(canonical_bytes({"source": source_identity, "extractor": extractor,
                                     "format": format_name}))
    value = {"schema": "docling-rendition-v1", "run_id": run_id,
             "extractor": extractor, "source": source_identity,
             "boundary": BOUNDARY, "conversion": report["conversion"]}
    raw_value = canonical_bytes(value)
    require(len(raw_value) <= MAX_RESULT, "Complete rendition exceeds 64 MiB", "output_limit")
    path = root / HOME / receipt / run_id / "extraction.json"
    return state, original, path, value, raw_value


def _publish(path, raw):
    path = bridge.catalog.safe_path(path)
    if path.exists():
        require(bridge.catalog.private_file(path).read_bytes() == raw,
                "Existing rendition differs; preserve it and investigate")
        return False
    temporary = path.with_name(uuid.uuid4().hex + ".partial")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        try:
            bridge.catalog.fsync_directory(path.parent)
        except OSError:
            # The hard link was created by this invocation, not an earlier run.
            if path.stat(follow_symlinks=False).st_ino == temporary.stat().st_ino:
                path.unlink()
                bridge.catalog.fsync_directory(path.parent)
            raise
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _unchanged(root, receipt, format_name, expected_source, expected_binding,
               expected_worker):
    state, _, identity, _ = bridge.source(root, receipt, format_name)
    require(identity == expected_source, "Source changed during Docling operation")
    require(bridge.binding() == expected_binding, "Policy or extraction code changed during operation")
    _require_worker(expected_worker)
    return state


def run(root: Path, command: str, receipt: str, format_name: str,
        artifacts_path: Path, python: Path, actor=None, expected_state=None,
        expected_identity=None):
    require(command in ("prepare", "verify", "register"), "Unsupported Docling command")
    root = bridge.catalog.safe_path(root)
    state, original, path, value, raw = _bundle(
        root, receipt, format_name, artifacts_path, python)
    if expected_identity is not None:
        # A caller holding a pinned identity refuses before any publication or
        # registration instead of discovering the difference afterward.
        require(isinstance(expected_identity, dict)
                and set(expected_identity) == {"run_id", "source", "extractor"},
                "Expected identity must name run, source and extractor", "invalid_request")
        require(all(expected_identity[key] == value[key] for key in expected_identity),
                "Computed identity differs from the expected identity", "mutation")
    binding = value["extractor"]["installation"]
    worker_sha = value["extractor"]["worker_sha256"]
    home = root / HOME
    with bridge.catalog.writer_lock(home):
        if command == "prepare":
            bridge.catalog.private_directory(path.parent.parent)
            bridge.catalog.private_directory(path.parent)
            # Serialize the state check and publication with supported catalog
            # writers. Source/code/policy changes still need rollback below.
            with bridge.catalog.writer_lock(bridge.catalog.locations(root)[0]):
                current_state = _unchanged(root, receipt, format_name, value["source"],
                                           binding, worker_sha)
                require(current_state == state, "Catalog changed during Docling operation; retry")
                created = _publish(path, raw)
                published = path.stat(follow_symlinks=False) if created else None
                try:
                    current_state = _unchanged(root, receipt, format_name, value["source"],
                                               binding, worker_sha)
                    require(current_state == state, "Catalog changed during Docling operation; retry")
                except Exception:
                    if published is not None:
                        observed = path.stat(follow_symlinks=False)
                        require((observed.st_dev, observed.st_ino) ==
                                (published.st_dev, published.st_ino),
                                "Rendition replaced during rollback; preserve and investigate")
                        path.unlink()
                        bridge.catalog.fsync_directory(path.parent)
                    raise
        else:
            require(path.exists(), "Prepared rendition missing; run prepare for this identity")
            require(bridge.catalog.private_file(path).read_bytes() == raw,
                    "Rendition differs from repeat conversion; preserve it and investigate")
            created = False
        if command != "prepare":
            current_state = _unchanged(root, receipt, format_name, value["source"], binding,
                                       worker_sha)
            require(current_state == state, "Catalog changed during Docling operation; retry")
        result = {"command": command, "receipt_id": receipt,
                  "run_id": value["run_id"], "rendition": path.relative_to(root).as_posix(),
                  "created": created, "source_sha256": value["source"]["sha256"],
                  "rendition_sha256": digest(raw), "catalog_state": state,
                  "profile": value["extractor"]["profile"],
                  "runtime": value["extractor"]["runtime"],
                  "assets": value["extractor"]["assets"],
                  "boundary": BOUNDARY}
        if command == "register":
            require(isinstance(actor, str) and actor and isinstance(expected_state, str)
                    and expected_state, "Registration requires actor and expected catalog state")
            registration_value = deepcopy(value)
            registration_value["canonical_bytes"] = raw
            recipe = {"actor": actor,
                      "request_id": "docling-" + receipt + "-" + value["run_id"],
                      "reason": "Explicit Docling rendition registration; no review or indexing.",
                      "records": bridge.registration_records(root, original, path,
                                                              registration_value)}
            bridge.catalog.validate_records(recipe["records"])
            saved_path = path.parent / "registration.json"
            if saved_path.exists():
                saved = bridge.catalog.decode(bridge.catalog.private_file(saved_path).read_text())
                require(isinstance(saved, dict) and set(saved) == set(recipe)
                        and all(saved.get(k) == recipe[k]
                                for k in ("actor", "request_id", "reason")),
                        "Saved registration differs in shape, actor, request or reason")
                bridge.catalog.validate_records(saved["records"])
                if saved != recipe:
                    history = bridge.catalog.read_records(
                        root, kind="artifact", path=value["source"]["path"],
                        include_history=True, limit=1,
                        expected_state=state["state_token"])
                    require(any(version["request_id"] == recipe["request_id"]
                                for record in history["records"] if record["id"] == original["id"]
                                for version in record["versions"]),
                            "Uncommitted registration metadata drift; reconcile saved request")
                    recipe = saved
            else:
                _publish(saved_path, canonical_bytes(recipe))
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as rendition:
                opened = os.fstat(rendition.fileno())
                def check_rendition():
                    try:
                        current_source = bridge._read_source(root / value["source"]["path"])
                        bridge.catalog.require(digest(current_source) == value["source"]["sha256"]
                                               and len(current_source) == value["source"]["bytes"],
                                               "Source changed before catalog commit")
                        bridge.catalog.require(bridge.binding() == binding,
                                               "Policy or code changed before catalog commit")
                        _require_worker(worker_sha)
                    except (OSError, bridge.BridgeError, AdapterError) as error:
                        raise bridge.catalog.CatalogError(
                            "Source/policy/worker changed before catalog commit: " + str(error)) from error
                    bridge.catalog.require(
                        _file_identity(os.fstat(rendition.fileno())) == _file_identity(opened)
                        == _file_identity(path.stat(follow_symlinks=False)),
                        "Prepared rendition changed before catalog commit")
                    rendition.seek(0)
                    bridge.catalog.require(rendition.read(MAX_RESULT + 1) == raw,
                                           "Prepared rendition bytes changed before catalog commit")
                    bridge.catalog.require(
                        _file_identity(os.fstat(rendition.fileno())) == _file_identity(opened)
                        == _file_identity(path.stat(follow_symlinks=False)),
                        "Prepared rendition changed during commit check")
                check_rendition()
                outcome = bridge.catalog.submit(root, expected_state=expected_state,
                                                precommit_check=check_rendition, **recipe)
                try:
                    check_rendition()
                except (OSError, bridge.catalog.CatalogError) as error:
                    raise AdapterError("Catalog registration committed, but rendition changed; "
                                       "preserve registration.json and reconcile: " + str(error)) from error
            try:
                completion = path.parent / "completion.json"
                if completion.exists():
                    saved = bridge.catalog.decode(bridge.catalog.private_file(completion).read_text())
                    require(saved["request_id"] == recipe["request_id"]
                            and saved["committed_state"] == outcome["committed_state"],
                            "Committed registration differs from saved completion")
                else:
                    _publish(completion, canonical_bytes(outcome))
                _unchanged(root, receipt, format_name, value["source"], binding, worker_sha)
            except (AdapterError, bridge.BridgeError, bridge.catalog.CatalogError,
                    OSError, ValueError, KeyError) as error:
                raise AdapterError("Catalog registration committed, but completion/final check failed; "
                                   "preserve registration.json and retry: " + str(error)) from error
            result.update(registered=True, catalog=outcome)
        return result
