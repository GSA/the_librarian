"""PDF text renditions page by page: each page's embedded text layer, or OCR of that page alone.

A PDF is unstapled into pages. One limited child process lists the pages (``pdf_page_worker.py``
``inventory``: pypdfium2 from the Docling lock, ``-I`` inside ``unshare -Urn``): each page's text layer,
image objects and size. A page with enough real text keeps its text layer; any other page is an OCR
unit: its own limited child renders just that page to a grayscale PNG (at most 216 DPI, reduced so it
fits the pixel cap, which is how oversized drawings are read) and converts it through the unchanged
pinned Docling adapter. Every unit is retried once on a timeout or crash, then recorded as a failed
page with its code. Pages are reassembled in order into one page-located ``lines`` rendition
(``payload.format == "pdf-pages"``) whose ``pages`` record each page's outcome; a failed page leaves a
visible placeholder line. Only a document whose every page fails, or that cannot be opened, is
refused. A per-document budget (pages read, OCR seconds) records what it leaves unread.

Renditions use the ordinary extraction envelope and lifecycle (``prepare`` publishes, ``register``
commits the catalog records), like ``pdf_image`` (which still reads images and keeps its identity).
Machine conversion is not review, qualification or indexing.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time

from tools import policy
from tools.catalog import catalog_store as catalog
from tools.context_index import renditions
from tools.extraction import docling_bridge as bridge
from tools.extraction import docling_worker
from tools.extraction import extract
from tools.extraction import pdf_image


VERSION = 1
HOME = extract.HOME
FORMATS = ("pdf",)
HERE = Path(__file__).resolve().parent
TOOLKIT = HERE.parents[1]
WORKER = HERE / "pdf_page_worker.py"
LOCK = pdf_image.LOCK
ConversionFailed = pdf_image.ConversionFailed
digest = pdf_image.digest
require = pdf_image.require
# The page thresholds of ``pdf_image.TEXT_LAYER``, applied to each page: a page keeps its text layer
# when it has at least ``min_page_chars`` letters or digits making up at least ``min_alnum_ratio`` of
# its non-space characters, or when it draws no image (OCR has nothing more to read) and the document
# has at least ``min_document_chars`` letters or digits overall (else its text may be drawn as paths).
TEXT_LAYER = dict(pdf_image.TEXT_LAYER)
# OCR pages are rendered at Docling's own OCR scale (3, i.e. 216 DPI) and declared at 216 DPI, so the
# Docling raster is exactly the rendered pixels; a larger page is rendered smaller to fit the cap.
RENDER = {"scale": 3.0, "dpi": 216, "max_pixels": renditions.DOCLING_MAX_PAGE_PIXELS * 98 // 100}
LIMITS = {
    "source_bytes": pdf_image.MAX_SOURCE, "pages": 5000, "lines": 200_000, "document_seconds": 7200,
    "page_wall_seconds": 300, "inventory_wall_seconds": 330, "cpu_seconds": 600,
    "address_space_bytes": docling_worker.LIMITS["address_space_bytes"], "attempts": 2,
}
GRACE_SECONDS = 2
TRANSIENT = pdf_image.TRANSIENT
DEFAULT_CONCURRENCY = pdf_image.DEFAULT_CONCURRENCY
SCOPE = ("Every page of the PDF in order: its embedded text layer (pypdfium2) when that has enough real "
         "text, else machine OCR/layout of that page alone (pinned Docling), one record per non-blank "
         "line; a page that could not be read is one placeholder line. Layout, tables and images are "
         "flattened or not read.")
FAILED_WARNING = ("%d page(s) were not extracted (%s); each has a placeholder line and its code in "
                  "payload.pages. Check the original pages.")


# Routing and assembly: pure functions of the page inventory and unit outcomes.

def needs_ocr(page, document_chars):
    """Whether an inventoried page goes to OCR rather than keeping its text layer."""
    alnum, visible = pdf_image.page_chars(page["text"])
    if alnum >= TEXT_LAYER["min_page_chars"] and alnum >= TEXT_LAYER["min_alnum_ratio"] * visible:
        return False
    return bool(page["image_objects"]) or document_chars < TEXT_LAYER["min_document_chars"]


def plan(pages):
    """``{page: "text-layer" | "ocr" | "failed"}`` for the inventoried pages."""
    total = sum(pdf_image.page_chars(page["text"])[0] for page in pages if "error" not in page)
    return {page["page"]: "failed" if "error" in page else "ocr" if needs_ocr(page, total) else "text-layer"
            for page in pages}


def _code(raw):
    return "extract-" + raw.replace("_", "-")


def placeholder(first, last, code, attempts=None):
    span = "Page %d" % first if first == last else "Pages %d-%d" % (first, last)
    tried = "" if not attempts else " after %d attempt%s" % (attempts, "" if attempts == 1 else "s")
    return "[%s not extracted: %s%s]" % (span, _code(code), tried)


def assemble(inventory, outcomes, limits=None):
    """The ``pdf-pages`` payload of an inventory and its OCR/failed page ``outcomes``.

    ``outcomes[page]`` is ``{"texts", "textless", "attempts", ...}`` for a read OCR page or
    ``{"code", "attempts"}`` for a failed one. Placeholders count against the line cap: once a page
    does not fit, it and every later page fail as ``record_limit`` behind one range placeholder.
    Raises ``ConversionFailed`` when every read page failed (with their shared code, else
    ``all_pages_failed``) or nothing has any text.
    """
    limits = dict(LIMITS, **(limits or {}))
    cap = limits["lines"] - 2  # room for the record-limit and unread range placeholders
    routes = plan(inventory["pages"])
    lines, pages, notes, ocr = [], [], [], None
    used, texts, stopped = set(), 0, None
    for page in inventory["pages"]:
        number, route = page["page"], routes[page["page"]]
        outcome = outcomes.get(number, {}) if route == "ocr" else {}
        if route == "failed":
            entry = {"page": number, "outcome": "failed", "code": _code(page["error"]), "attempts": 1}
        elif route == "text-layer":
            entry = {"page": number, "outcome": "text-layer", "text_chars": pdf_image.page_chars(page["text"])[0],
                     "image_objects": page["image_objects"]}
            chunks = [page["text"]]
        elif "code" in outcome:
            entry = {"page": number, "outcome": "failed", "code": _code(outcome["code"]),
                     "attempts": outcome["attempts"]}
        else:
            entry = {"page": number, "outcome": "ocr", "textless": outcome["textless"],
                     "attempts": outcome["attempts"], "render_scale": outcome["render"]["scale"],
                     "confidence": outcome["confidence"]}
            chunks = outcome["texts"]
        if entry["outcome"] == "failed":
            found = [placeholder(number, number, entry["code"].removeprefix("extract-").replace("-", "_"),
                                 entry["attempts"])]
        else:
            found = [text.rstrip() for chunk in chunks for text in chunk.split("\n") if text.strip()]
        if stopped is None and len(lines) + len(found) > cap:
            stopped = number
        if stopped is not None:
            entry = {"page": number, "outcome": "failed", "code": _code("record_limit"),
                     "attempts": entry.get("attempts", 1)}
        else:
            start = len(lines) + 1
            lines.extend({"line": start + i, "page": number, "text": text} for i, text in enumerate(found))
            if entry["outcome"] != "failed":
                texts += len(found)
            if entry["outcome"] == "ocr":
                ocr = ocr or outcome["method"]
                notes.extend("page %d: %s" % (number, note) for note in outcome["notes"])
        used.add(entry["outcome"])
        pages.append(entry)
    if stopped is not None:
        lines.append({"line": len(lines) + 1, "page": stopped,
                      "text": placeholder(stopped, pages[-1]["page"], "record_limit")})
    read, count = len(inventory["pages"]), inventory["page_count"]
    unread = None
    if count > read:
        reason = inventory.get("unread") or "page_budget"
        unread = {"first": read + 1, "last": count, "code": _code(reason)}
        lines.append({"line": len(lines) + 1, "page": read + 1, "text": placeholder(read + 1, count, reason)})
    failed = [entry for entry in pages if entry["outcome"] == "failed"]
    if len(failed) == len(pages):
        codes = {entry["code"] for entry in failed}
        raise ConversionFailed(codes.pop().removeprefix("extract-").replace("-", "_") if len(codes) == 1
                               else "all_pages_failed", "every page of the PDF failed")
    if not texts:
        raise ConversionFailed("empty_conversion", "no page has any text")
    warnings = []
    if "text-layer" in used:
        warnings.append(pdf_image.TEXT_WARNING)
    if "ocr" in used:
        warnings.append(pdf_image.OCR_WARNING)
    if failed or unread:
        codes = sorted({entry["code"] for entry in failed} | ({unread["code"]} if unread else set()))
        warnings.append(FAILED_WARNING % (len(failed) + (count - read if unread else 0), ", ".join(codes)))
    return {
        "format": "pdf-pages", "source_format": "pdf", "scope": SCOPE, "warnings": warnings + notes,
        "lines": lines, "pages": pages, "page_count": count, "unread_pages": unread,
        "method": {"kind": "pdf-pages", "reader": inventory["reader"], "version": inventory["version"],
                   "threshold": dict(TEXT_LAYER), "render_dpi": RENDER["dpi"], "ocr": ocr},
    }


def ocr_result(report, render):
    """The compact outcome of one page's Docling conversion (page numbers are the caller's)."""
    conversion = report["conversion"]
    notes = pdf_image._confidence_warnings(conversion.get("warnings", []))
    return {"texts": [text for _, text in pdf_image._flatten(conversion["document"])],
            "textless": all(page["textless"] for page in conversion["pages"]),
            "confidence": conversion["confidence"], "notes": notes, "render": render,
            "method": {"kind": "docling-ocr", "runtime": report["runtime"],
                       "profile_version": report["profile"]["version"],
                       "assets_sha256": digest(docling_worker.canonical_bytes(report["assets"]))}}


# The limited unit child: one inventory or one OCR page, then exit.

def _alarm(_signal, _frame):
    raise ConversionFailed("timeout", "page unit wall timeout exceeded")


def _cpu(_signal, _frame):
    # As PID 1 of its namespace the unit ignores signals it has no handler for, SIGXCPU included.
    raise ConversionFailed("cpu_limit", "page unit exceeded CPU time")


def _worker(home, job, python, timeout):
    """The page worker's payload for ``job``, run in the pinned interpreter inside a network namespace."""
    work = home / "worker"
    work.mkdir(mode=0o700)
    worker = work / WORKER.name
    worker.write_bytes(WORKER.read_bytes())
    (work / LOCK.name).write_bytes(LOCK.read_bytes())
    path = work / "job.json"
    path.write_text(json.dumps({"schema": "pdf-page-job-v1", **job}))
    for item in (worker, work / LOCK.name, path):
        item.chmod(0o600)
    result = work / "result.json"
    try:
        process = subprocess.Popen(["unshare", "-Urn", str(python), "-I", str(worker), str(path), str(result)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    except OSError as error:
        raise ConversionFailed("isolation_unavailable", "page worker launch unavailable") from error
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ConversionFailed("timeout", "page worker exceeded wall timeout") from error
    finally:
        pdf_image._kill(process)
    envelope = pdf_image._read_result(result)
    if envelope is None:
        if process.returncode == -signal.SIGXCPU:
            raise ConversionFailed("cpu_limit", "page worker exceeded CPU time")
        raise ConversionFailed("worker_crashed", "page worker produced no result")
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise ConversionFailed(str(error.get("code") or "worker_failed"), error.get("message"))
    return envelope["payload"]


def read_unit(home, job):
    """The payload of one unit: the page inventory, or one page rendered and converted by Docling."""
    from tools.extraction import docling_adapter
    remaining = max(1, job["wall_seconds"] - 5)
    if job["kind"] == "inventory":
        return _worker(home, {"operation": "inventory", "source_path": job["source"],
                              "max_pages": job["max_pages"]}, job["python"], remaining)
    image = home / "worker-page.png"
    render = _worker(home, {"operation": "render", "source_path": job["source"], "page": job["page"],
                            "scale": RENDER["scale"], "max_pixels": RENDER["max_pixels"],
                            "image_path": str(image)}, job["python"], remaining)
    raw = pdf_image._with_png_dpi(image.read_bytes(), RENDER["dpi"])
    try:
        report = docling_adapter.convert(raw, "page-%d.png" % job["page"], "image",
                                         Path(job["artifacts"]), Path(job["python"]))
    except docling_adapter.AdapterError as error:
        if error.code == "empty_conversion":  # a blank page
            return {"texts": [], "textless": True, "confidence": None, "notes": [], "render": render,
                    "method": None}
        raise ConversionFailed(error.code, str(error)) from error
    return ocr_result(report, render)


def unit(home):
    """Entry of the limited unit child: run ``home/job.json``, write ``home/result.json``."""
    home = Path(home)
    started = time.monotonic()
    try:
        job = json.loads((home / "job.json").read_text())
        pdf_image._limit(resource.RLIMIT_AS, job["address_space_bytes"])
        pdf_image._limit(resource.RLIMIT_CPU, job["cpu_seconds"], job["cpu_seconds"] + 10)
        pdf_image._limit(resource.RLIMIT_FSIZE, pdf_image.MAX_RESULT * 2)
        signal.signal(signal.SIGALRM, _alarm)
        signal.signal(signal.SIGXCPU, _cpu)
        signal.alarm(job["wall_seconds"])
        result = {"ok": True, "payload": read_unit(home, job)}
    except ConversionFailed as error:
        result = {"ok": False, "code": error.raw_code, "message": str(error)[:4096]}
    except MemoryError:
        result = {"ok": False, "code": "memory_limit", "message": "page unit exceeded its address space"}
    except Exception as error:  # noqa: BLE001 - every other failure is this unit's runtime error
        result = {"ok": False, "code": "runtime_error", "message": type(error).__name__}
    signal.alarm(0)
    usage = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    result["stats"] = {"seconds": round(time.monotonic() - started, 3), "peak_rss_kib": usage}
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > pdf_image.MAX_RESULT:
        raw = json.dumps({"ok": False, "code": "output_limit", "message": "result exceeds 64 MiB",
                          "stats": result["stats"]}).encode()
    descriptor = os.open(home / "result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return 0 if result["ok"] else 1


# Each unit is PID 1 of its own PID namespace: when it ends or is killed, the kernel kills every
# process it started, including the page and Docling workers that run in sessions of their own.
NAMESPACE = ["unshare", "-Ur", "--pid", "--fork", "--kill-child"]


def _child_argv(home):
    return [*NAMESPACE, sys.executable, "-E", "-s", "-m", "tools.extraction.pdf_pages", "unit", str(home)]


def _attempt(directory, job, wall_seconds):
    """One unit in its own limited child process (its process group is killed afterwards)."""
    with tempfile.TemporaryDirectory(prefix="unit-", dir=directory) as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        (home / "job.json").write_text(json.dumps({**job, "wall_seconds": max(1, wall_seconds - 15)}))
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": str(home), "PYTHONHASHSEED": "0"}
        try:
            process = subprocess.Popen(_child_argv(home), cwd=TOOLKIT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
        except OSError as error:
            raise ConversionFailed("worker_failed", "page unit launch failed") from error
        try:
            process.wait(timeout=wall_seconds)
        except subprocess.TimeoutExpired as error:
            raise ConversionFailed("timeout", "page unit exceeded wall timeout") from error
        finally:
            pdf_image._kill(process)
        result = pdf_image._read_result(home / "result.json")
    if result is None:
        if process.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
            raise ConversionFailed("cpu_limit" if process.returncode == -signal.SIGXCPU else "worker_crashed",
                                   "page unit was killed")
        raise ConversionFailed("worker_crashed", "page unit produced no result")
    if not result.get("ok"):
        failure = ConversionFailed(str(result.get("code") or "worker_failed"), result.get("message"))
        failure.stats = result.get("stats")
        raise failure
    return result["payload"], result.get("stats", {})


def _run(directory, job, wall_seconds, limits, log=None):
    """``(payload, attempts)`` of one unit, retried once on a transient failure; a refusal raises
    ``ConversionFailed`` with ``attempts`` set."""
    for attempt in range(1, limits["attempts"] + 1):
        started = time.monotonic()
        try:
            payload, stats = _attempt(directory, job, wall_seconds)
            if log is not None:
                log.append({"kind": job["kind"], "page": job.get("page"), "attempt": attempt, "ok": True,
                            "seconds": round(time.monotonic() - started, 3), **stats})
            return payload, attempt
        except ConversionFailed as error:
            if log is not None:
                log.append({"kind": job["kind"], "page": job.get("page"), "attempt": attempt, "ok": False,
                            "code": error.code, "seconds": round(time.monotonic() - started, 3),
                            **(getattr(error, "stats", None) or {})})
            error.attempts = attempt
            if not error.transient or attempt >= limits["attempts"]:
                raise


class Budget:
    """OCR seconds spent on one document, shared by its page units."""

    def __init__(self, seconds):
        self.seconds, self.spent, self.lock = seconds, 0.0, threading.Lock()

    def exhausted(self):
        with self.lock:
            return self.spent >= self.seconds

    def charge(self, seconds):
        with self.lock:
            self.spent += seconds


def convert(raw, name, paths, work, limits=None, units=None, log=None):
    """``(payload, stats)`` of one PDF; raises ``ConversionFailed`` when it cannot be read at all.

    Units run on ``units`` (an executor shared across documents bounds every conversion child
    process); without one they run one at a time.
    """
    limits = dict(LIMITS, **(limits or {}))
    if len(raw) > limits["source_bytes"]:
        raise ConversionFailed("source_limit", "source exceeds byte limit")
    own = units is None
    units = units or ThreadPoolExecutor(max_workers=1)
    started = time.monotonic()
    base = {"python": str(paths["python"]), "artifacts": str(paths["artifacts"]),
            "address_space_bytes": limits["address_space_bytes"], "cpu_seconds": limits["cpu_seconds"]}
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-pages-", dir=work) as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            source = directory / "source.pdf"
            source.write_bytes(raw)
            source.chmod(0o600)
            base["source"] = str(source)
            inventory, _ = units.submit(_run, directory, {**base, "kind": "inventory",
                                                          "max_pages": limits["pages"]},
                                        limits["inventory_wall_seconds"], limits, log).result()
            budget = Budget(limits["document_seconds"])

            def page(number):
                if budget.exhausted():
                    return {"code": "time_budget", "attempts": 0}
                began = time.monotonic()
                try:
                    payload, attempts = _run(directory, {**base, "kind": "ocr", "page": number},
                                             limits["page_wall_seconds"], limits, log)
                    return {**payload, "attempts": attempts}
                except ConversionFailed as error:
                    return {"code": error.raw_code, "attempts": error.attempts}
                finally:
                    budget.charge(time.monotonic() - began)
            numbers = [n for n, route in plan(inventory["pages"]).items() if route == "ocr"]
            futures = {n: units.submit(page, n) for n in numbers}
            outcomes = {n: future.result() for n, future in futures.items()}
    finally:
        if own:
            units.shutdown()
    payload = assemble(inventory, outcomes, limits)
    counts = {}
    for entry in payload["pages"]:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    return payload, {"seconds": round(time.monotonic() - started, 3), "kind": "pdf-pages", "pages": counts}


# Identity and lifecycle: the ordinary extraction envelope, registered as ``A-extract-<run>``.

def extractor(root):
    """The extraction identity: this code, the page worker, the shared ``pdf_image`` helpers, the lock,
    the Docling code and profile, the thresholds, render settings and limits, and the instance policy."""
    parts = {
        "code": digest(b"\0".join(path.read_bytes() for path in (Path(__file__), WORKER, Path(pdf_image.__file__)))),
        "lock": digest(LOCK.read_bytes()),
        "docling": {"code": digest(b"\0".join(path.read_bytes() for path in pdf_image.DOCLING_FILES)),
                    "profile": docling_worker.profile()},
        "profile": {"text_layer": TEXT_LAYER, "render": RENDER, "limits": LIMITS},
        "policy": policy.fingerprint(root),
    }
    return {"version": VERSION, "sha256": digest(extract.encoded(parts))}


def bundle(root, receipt, format_name):
    """``(state, original, path, value)``; ``value["payload"]`` is None until the rendition is published."""
    root = catalog.safe_path(root)
    require(format_name in FORMATS, "Unsupported PDF page format")
    state, original, identity, _ = bridge.source(root, receipt, format_name)
    settings = policy.for_root(root)[0]
    home = policy.validate_instance_roots(root)["extractions_home"]
    engine = extractor(root)
    run = digest(extract.encoded({"source": identity, "extractor": engine, "format": format_name}))
    value = {"schema_version": VERSION, "run_id": run, "extractor": engine, "source": identity,
             "boundary": extract.BOUNDARY + " " + settings["handling"], "payload": None}
    path = root / home / receipt / run / "extraction.json"
    if path.exists():
        value = _published(path, value)
    return state, original, path, value


def _published(path, expected):
    raw = catalog.private_file(path).read_bytes()
    value = json.loads(raw.decode("utf-8"))
    require(isinstance(value, dict) and {k: v for k, v in value.items() if k != "payload"}
            == {k: v for k, v in expected.items() if k != "payload"}
            and isinstance(value.get("payload"), dict) and value["payload"].get("format") == "pdf-pages"
            and extract.encoded(value) == raw,
            "Existing extraction differs from this source and extractor; preserve it and investigate")
    return value


def unchanged(root, value):
    raw = bridge._read_source(catalog.safe_path(root / value["source"]["path"]))
    require(digest(raw) == value["source"]["sha256"], "Source changed during extraction/registration")
    require(extractor(root) == value["extractor"], "Extractor changed during operation")


# Conversion failures of this process, by (receipt, run) -> (code, message), consumed by the ledger
# stage that follows a failed ahead-of-time conversion (as in ``pdf_image``).
_FAILED = {}


def _payload(root, value, paths=None, limits=None, units=None):
    key = (value["source"]["receipt_id"], value["run_id"])
    if key in _FAILED:
        raise ConversionFailed(*_FAILED.pop(key))
    raw = bridge._read_source(catalog.safe_path(root / value["source"]["path"]))
    require(digest(raw) == value["source"]["sha256"], "Source changed during extraction")
    try:
        payload, _ = convert(raw, Path(value["source"]["path"]).name, paths or pdf_image.runtime(root),
                             pdf_image.work_directory(root), limits, units)
    except ConversionFailed as error:
        _FAILED[key] = (error.raw_code, str(error))
        raise
    return {**value, "payload": payload}


def prepare(root, receipt, format_name, prepared=None, paths=None, limits=None):
    """Convert (unless published) and publish one rendition; returns the extract-style result."""
    root = catalog.safe_path(root)
    state, original, path, value = prepared or bundle(root, receipt, format_name)
    require(value["source"]["receipt_id"] == receipt and path.is_relative_to(root),
            "Prepared bundle is for a different receipt or root")
    if value["payload"] is None:
        value = _payload(root, value, paths, limits)
    data = extract.encoded(value)
    require(len(data) <= pdf_image.MAX_RESULT, "Complete rendition exceeds output limit")
    with catalog.writer_lock(root / HOME):
        catalog.private_directory(path.parent.parent)
        catalog.private_directory(path.parent)
        unchanged(root, value)
        created = extract.publish(path, data)
        unchanged(root, value)
    return {"command": "prepare", "receipt_id": receipt, "run_id": value["run_id"],
            "rendition": path.relative_to(root).as_posix(), "created": created,
            "source_sha256": value["source"]["sha256"], "rendition_sha256": digest(data),
            "catalog_state": state, "boundary": extract.BOUNDARY, "warnings": value["payload"]["warnings"],
            "kind": value["payload"]["method"]["kind"]}


def prepare_many(root, requests, concurrency=DEFAULT_CONCURRENCY, paths=None, limits=None):
    """``prepare`` for many ``(receipt, format)`` pairs; at most ``concurrency`` page units (each its
    own limited child process) run at once across all the documents. Returns ``{receipt: prepare
    result or the exception that refused it}``; one file's failure never stops another."""
    require(type(concurrency) is int and concurrency >= 1, "Extraction concurrency must be a positive integer")
    root = catalog.safe_path(root)
    outcomes, bundles = {}, {}
    for receipt, format_name in requests:
        try:
            bundles[receipt] = (format_name, bundle(root, receipt, format_name))
        except pdf_image.PREPARE_ERRORS as error:
            outcomes[receipt] = error
    todo = [receipt for receipt, (_, prepared) in bundles.items() if prepared[3]["payload"] is None]
    with ThreadPoolExecutor(max_workers=concurrency) as units, \
            ThreadPoolExecutor(max_workers=concurrency) as documents:
        def one(receipt):
            try:
                return _payload(root, bundles[receipt][1][3], paths, limits, units)
            except pdf_image.PREPARE_ERRORS as error:
                return error
        converted = dict(zip(todo, documents.map(one, todo)))
    for receipt, (format_name, prepared) in bundles.items():
        value = converted.get(receipt, prepared[3])
        if isinstance(value, Exception):
            outcomes[receipt] = value
            continue
        try:
            outcomes[receipt] = prepare(root, receipt, format_name, (*prepared[:3], value), paths, limits)
        except pdf_image.PREPARE_ERRORS as error:
            outcomes[receipt] = error
    return outcomes


def forget_failures():
    _FAILED.clear()


def registration_records(root, original, path, value):
    relative = path.relative_to(root).as_posix()
    ref = extract.reference(relative, digest(extract.encoded(value)))
    what = "PDF text layer and machine OCR, page by page"
    parent = deepcopy(original)
    data = parent["data"]
    data.update(sha256=value["source"]["sha256"], hash_basis="observed", bytes=value["source"]["bytes"],
                extraction="existing-rendition", coverage="deferred",
                reason="Text rendition (" + what + ") exists; visual content is not reproduced. "
                       "Review/indexing remain separate.")
    if ref not in data["provenance"]:
        data["provenance"].append(ref)
    identifier = "A-extract-" + value["run_id"]
    child = {"id": identifier, "kind": "artifact", "data": {
        "path": relative, "parent_id": original["id"], "receipt_id": value["source"]["receipt_id"],
        "role": "generated-rendition", "audience": data["audience"], "coverage": "deferred",
        "reason": "Machine text rendition (" + what + "); inherits source restrictions; not reviewed, "
                  "indexed or independent evidence.",
        "presence": "present", "sha256": ref["sha256"], "hash_basis": "observed",
        "bytes": len(extract.encoded(value)), "extraction": "not-required",
        "handling_refs": deepcopy(data["handling_refs"]),
        "provenance": [extract.reference(value["source"]["path"], value["source"]["sha256"]), ref],
    }}
    relation = {"id": "R-extract-" + value["run_id"], "kind": "relationship", "data": {
        "from_id": identifier, "to_id": original["id"], "relation": "rendition-of",
        "scope": "Machine text-only rendition (" + what + "); not visual equivalence, independent "
                 "evidence, review or acceptance.",
        "basis": "reported", "provenance": [ref],
    }}
    return [parent, child, relation]


def _recipe(root, original, path, value, state, actor, marker):
    """The registration to submit, or the one an earlier attempt saved beside the rendition."""
    recipe = {"actor": actor, "request_id": "extract-" + value["source"]["receipt_id"] + "-" + value["run_id"],
              "reason": "Explicit machine-extraction registration; no review, index or intake-stage change.",
              "records": registration_records(root, original, path, value)}
    catalog.validate_records(recipe["records"])
    saved_path = path.parent / "registration.json"
    if not saved_path.exists():
        extract.publish(saved_path, extract.encoded(recipe))
        return recipe
    saved = catalog.decode(catalog.private_file(saved_path).read_text())
    require(set(saved) == set(recipe) and all(saved[k] == recipe[k] for k in ("actor", "request_id", "reason")),
            "Saved registration differs in actor, request or reason")
    catalog.validate_records(saved["records"])
    if saved != recipe:
        history = catalog.read_records(root, kind="artifact", path=value["source"]["path"],
                                       include_history=True, limit=1, expected_state=state["state_token"])
        require(catalog.marker_present(root, marker)
                or any(version["request_id"] == recipe["request_id"] for record in history["records"]
                       if record["id"] == original["id"] for version in record["versions"]),
                "Uncommitted registration metadata drift; preserve the saved request and reconcile")
    return saved


def _finish(root, path, value, recipe, outcome):
    try:
        completion = path.parent / "completion.json"
        if not completion.exists():
            extract.publish(completion, extract.encoded(outcome))
        else:
            saved = catalog.decode(catalog.private_file(completion).read_text())
            require(saved["request_id"] == recipe["request_id"]
                    and saved["committed_state"] == outcome["committed_state"],
                    "Committed registration differs from saved completion")
        unchanged(root, value)
    except (extract.ExtractionError, catalog.CatalogError, OSError, ValueError, KeyError) as error:
        raise extract.ExtractionError("Catalog registration committed, but completion/final check failed; "
                                      "preserve registration.json and retry the same request: " + str(error)) from error


def register_batch(root, items, *, rebase=True):
    """``extract.register_batch`` for PDF page renditions: one catalog revision for the batch.

    Each item needs its published rendition (``prepared`` from ``bundle``); the source and extractor
    are re-checked under the extraction lock before and after its saved registration is read.
    """
    root = catalog.safe_path(root)
    errors = pdf_image.REGISTER_ERRORS
    outcomes = [None] * len(items)
    with catalog.writer_lock(root / HOME):
        ready = []
        for index, item in enumerate(items):
            try:
                state, original, path, value = item["prepared"]
                require(value["source"]["receipt_id"] == item["receipt"] and path.is_relative_to(root),
                        "Prepared bundle is for a different receipt or root")
                require(value["payload"] is not None and path.exists(),
                        "Prepared rendition missing; run prepare for this source/extractor version")
                raw = extract.encoded(value)
                require(catalog.private_file(path).read_bytes() == raw,
                        "Rendition differs from the prepared one; preserve it and investigate")
                require(item["actor"] is not None and item["expected_state"] is not None,
                        "Registration requires actor and explicit expected catalog state")
                unchanged(root, value)
                marker = "A-extract-" + value["run_id"]
                recipe = _recipe(root, original, path, value, state, item["actor"], marker)
                unchanged(root, value)
            except errors as error:
                outcomes[index] = error
                continue
            result = {"command": "register", "receipt_id": item["receipt"], "run_id": value["run_id"],
                      "rendition": path.relative_to(root).as_posix(), "created": False,
                      "source_sha256": value["source"]["sha256"], "rendition_sha256": digest(raw),
                      "catalog_state": state, "boundary": extract.BOUNDARY,
                      "warnings": value["payload"]["warnings"]}
            ready.append((index, item, path, value, recipe, result, marker))
        groups = {}
        for entry in ready:
            groups.setdefault((entry[4]["actor"], entry[4]["reason"]), []).append(entry)
        for (actor, reason), entries in groups.items():
            members = [{"marker": marker, "records": recipe["records"], "expected_state": item["expected_state"]}
                       for _, item, _, _, recipe, _, marker in entries]
            try:
                submitted = catalog.submit_batch(root, members, actor=actor, reason=reason, rebase=rebase)
            except errors as error:
                for entry in entries:
                    outcomes[entry[0]] = error
                continue
            for (index, item, path, value, recipe, result, _), member in zip(entries, submitted["members"]):
                if member["status"] == "refused":
                    outcomes[index] = member["error"]
                    continue
                outcome = {"command": "submit", "idempotent": member["status"] == "present",
                           **member["committed_state"], "state": member["committed_state"],
                           "request_id": recipe["request_id"], "committed_revision": member["committed_revision"],
                           "committed_state": member["committed_state"], "backup": None, "retention": None}
                try:
                    _finish(root, path, value, recipe, outcome)
                except errors as error:
                    outcomes[index] = error
                    continue
                result.update(catalog_state=outcome["state"], registered=True, idempotent=outcome["idempotent"],
                              request_id=recipe["request_id"], committed_revision=member["committed_revision"])
                outcomes[index] = result
    return outcomes


def run(root, command, receipt, format_name, actor=None, expected_state=None, rebase=False, prepared=None):
    """``extract.run`` for PDF page renditions: ``prepare`` converts and publishes, ``register`` commits."""
    root = catalog.safe_path(root)
    if command == "prepare":
        return prepare(root, receipt, format_name, prepared)
    require(command == "register", "Unsupported PDF page extraction command")
    prepared = prepared or bundle(root, receipt, format_name)
    outcome = register_batch(root, [{"receipt": receipt, "format_name": format_name, "actor": actor,
                                     "expected_state": expected_state, "prepared": prepared}],
                             rebase=rebase)[0]
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("probe", help="Convert local PDFs without storing anything; print "
                                                "per-document and per-unit stats as JSON lines")
    command.add_argument("--root", type=Path, default=TOOLKIT)
    command.add_argument("--work", type=Path, required=True, help="Private scratch directory")
    command.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    command.add_argument("files", nargs="+", type=Path)
    command = commands.add_parser("unit")
    command.add_argument("home", type=Path)
    args = parser.parse_args(argv)
    if args.command == "unit":
        return unit(args.home)
    paths = pdf_image.runtime(args.root)
    lock = threading.Lock()

    def probe(path):
        log, started = [], time.monotonic()
        record = {"file": path.name, "bytes": path.stat().st_size}
        try:
            payload, stats = convert(path.read_bytes(), path.name, paths, args.work, units=units, log=log)
            record.update(ok=True, lines=len(payload["lines"]), page_count=payload["page_count"],
                          outcomes=stats["pages"], failed={e["page"]: e["code"] for e in payload["pages"]
                                                           if e["outcome"] == "failed"},
                          unread=payload["unread_pages"])
        except ConversionFailed as error:
            record.update(ok=False, code=error.code)
        record.update(seconds=round(time.monotonic() - started, 3), units=log)
        with lock:
            print(json.dumps(record, sort_keys=True), flush=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as units, \
            ThreadPoolExecutor(max_workers=args.concurrency) as documents:
        list(documents.map(probe, args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
