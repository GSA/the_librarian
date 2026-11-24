#!/usr/bin/env python3
"""Install the pinned LibreOffice runtime for legacy Office conversion without root.

``install`` downloads the upstream Linux x86-64 ``deb`` bundle (or reads one given with ``--archive``),
checks its size and SHA-256 against the pins below, unpacks only the Writer, Calc, Impress and Draw
packages with the standard library (no ``dpkg``, no ``sudo``, nothing outside the toolkit) into
``tools/extraction/.local/libreoffice/<version>/`` and points ``current`` at it. ``verify`` converts a
synthetic document inside the sandbox; ``status`` reports readiness. Standard library only.

The pins come from The Document Foundation's published checksum for the release; re-pin by editing
``PINS`` (version, URL, size, SHA-256, package list) and re-running ``verify``.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import uuid

from tools.extraction import bounded, office_runtime

PINS = {
    "version": "26.2.7.3",
    "series": "26.2",
    "url": "https://download.documentfoundation.org/libreoffice/stable/26.2.7/deb/x86_64/"
           "LibreOffice_26.2.7_Linux_x86-64_deb.tar.gz",
    "archive_name": "LibreOffice_26.2.7_Linux_x86-64_deb.tar.gz",
    "bytes": 218569368,
    "sha256": "8cd04a88396a9230264ff784ec89cc090cbfd0960c192483ba2b80fa53939416",
    "architecture": "x86_64",
}
# Package name stems (``libobasis<series>-<stem>_`` or ``libreoffice<series>-<stem>_``) the converter needs.
PACKAGES = ("ure", "core", "writer", "calc", "impress", "draw", "graphicfilter", "en-us")
LAUNCHER = "libreoffice{series}_"
MAX_UNPACKED = 3 * 1024 ** 3
MAX_MEMBER = 1024 ** 3
DOWNLOAD_TIMEOUT = 120
SAMPLE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3" '
    'office:mimetype="application/vnd.oasis.opendocument.text"><office:body><office:text>'
    "<text:p>Librarian converter verification paragraph</text:p></office:text></office:body></office:document>\n")


class ProvisionError(Exception):
    pass


def _local():
    return office_runtime.TOOLKIT / office_runtime.LOCAL


def digest(path, limit):
    """``(size, sha256)`` of ``path``; refuses a file larger than ``limit`` before reading it all."""
    size, sha = 0, hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ProvisionError("Archive is larger than the pinned size")
            sha.update(chunk)
    return size, sha.hexdigest()


def check_archive(path, pins):
    size, sha = digest(path, pins["bytes"])
    if size != pins["bytes"] or sha != pins["sha256"]:
        raise ProvisionError("Archive does not match the pinned size and SHA-256 (got %d bytes, %s); "
                             "refusing to unpack it" % (size, sha))


def download(url, destination, pins):
    """Fetch ``url`` over HTTPS to ``destination``, never past the pinned size."""
    if not url.startswith("https://"):
        raise ProvisionError("Downloads must use https")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, open(partial, "wb") as out:
            total = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                total += len(chunk)
                if total > pins["bytes"]:
                    raise ProvisionError("Download is larger than the pinned size")
                out.write(chunk)
        os.replace(partial, destination)
    except OSError as error:
        raise ProvisionError("Download failed: " + str(error)) from error
    finally:
        partial.unlink(missing_ok=True)


def ar_members(raw):
    """``(name, bytes)`` of each member of an ``ar`` archive (a ``.deb``)."""
    if raw[:8] != b"!<arch>\n":
        raise ProvisionError("Not a deb package")
    position = 8
    while position + 60 <= len(raw):
        header = raw[position:position + 60]
        name, size = header[:16].decode("ascii", "replace").strip().rstrip("/"), header[48:58].strip()
        try:
            length = int(size)
        except ValueError as error:
            raise ProvisionError("Malformed deb package") from error
        position += 60
        yield name, raw[position:position + length]
        position += length + (length & 1)


def unpack_deb(raw, destination, prefix):
    """Extract a deb's data under ``prefix`` into ``destination``; return the number of bytes written."""
    for name, body in ar_members(raw):
        if name.startswith("data.tar"):
            written = 0
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
                for member in archive:
                    relative = member.name[2:] if member.name.startswith("./") else member.name
                    if not (relative == prefix.rstrip("/") or relative.startswith(prefix)):
                        continue
                    if member.size > MAX_MEMBER or not (member.isreg() or member.isdir() or member.issym()):
                        raise ProvisionError("Unexpected member in package: " + relative[:80])
                    member.name = relative[len(prefix):] if relative != prefix.rstrip("/") else "."
                    if member.name in ("", "."):
                        continue
                    archive.extract(member, destination, filter="data")
                    written += max(member.size, 0)
            return written
    raise ProvisionError("Package has no data member")


def wanted(names, pins):
    series = pins["series"]
    stems = [("libobasis%s-%s_" % (series, stem)) for stem in PACKAGES]
    stems += [("libreoffice%s-%s_" % (series, stem)) for stem in ("ure", "en-us")]
    stems.append(LAUNCHER.format(series=series))
    chosen = []
    for name in sorted(names):
        base = Path(name).name
        if base.endswith(".deb") and any(base.startswith(stem) for stem in stems):
            chosen.append(name)
    return chosen


def install_archive(archive, pins=PINS, local=None):
    """Unpack the verified bundle ``archive``; return the installed version directory."""
    local = Path(local) if local is not None else _local()
    check_archive(archive, pins)
    target = local / pins["version"]
    if target.exists():
        raise ProvisionError("%s already exists; remove it to reinstall" % target)
    local.mkdir(parents=True, exist_ok=True)
    staging = local / (".install-" + uuid.uuid4().hex)
    prefix = "opt/libreoffice%s/" % pins["series"]
    total, packages = 0, []
    try:
        os.mkdir(staging, 0o755)
        with tarfile.open(archive, "r:gz") as bundle:
            debs = {member.name: member for member in bundle if member.isreg() and member.name.endswith(".deb")}
            chosen = wanted(debs, pins)
            if not chosen:
                raise ProvisionError("The bundle holds none of the expected packages")
            for name in chosen:
                stream = bundle.extractfile(debs[name])
                total += unpack_deb(stream.read(), staging, prefix)
                packages.append(Path(name).name)
                if total > MAX_UNPACKED:
                    raise ProvisionError("Unpacked runtime exceeds its size limit")
        program = staging / "program" / "soffice"
        if not program.is_file():
            raise ProvisionError("The unpacked runtime has no program/soffice")
        (staging / office_runtime.PROVENANCE).write_text(json.dumps({
            "version": "LibreOffice " + pins["version"], "archive_sha256": pins["sha256"],
            "source": pins["url"], "packages": sorted(packages)}, indent=1, sort_keys=True) + "\n")
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    link = local / "current"
    temporary = local / (".current-" + uuid.uuid4().hex)
    os.symlink(pins["version"], temporary)
    os.replace(temporary, link)
    return target


def verify():
    """Convert a synthetic document in the sandbox; return the readiness report. Raises on failure."""
    report = office_runtime.status()
    if not report["ready"]:
        raise ProvisionError("Runtime is not ready: %s" % (report["action"] or report["code"]))
    with tempfile.TemporaryDirectory(prefix="librarian-verify-") as parent:
        try:
            with office_runtime.exported(parent, SAMPLE.encode(), ".fodt", "docx") as produced:
                if produced.stat().st_size < 100 or not produced.read_bytes().startswith(b"PK"):
                    raise ProvisionError("The sandboxed conversion produced no document")
        except bounded.BoundedError as error:
            raise ProvisionError("Sandboxed test conversion failed (%s)%s" % (
                error.code, ("\nLibreOffice said:\n" + error.detail) if error.detail else "")) from error
    report["verified"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Download (or read --archive), verify and unpack the pinned runtime")
    install.add_argument("--archive", type=Path, help="A bundle already downloaded; still checked against the pins")
    install.add_argument("--print-pins", action="store_true", help="Print the pinned URL, size and SHA-256 and exit")
    commands.add_parser("verify", help="Run a synthetic conversion inside the sandbox")
    commands.add_parser("status", help="Report whether legacy conversion is ready (JSON)")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(office_runtime.status(), sort_keys=True))
            return 0
        if args.command == "verify":
            print(json.dumps(verify(), sort_keys=True))
            return 0
        if args.print_pins:
            print(json.dumps(PINS, indent=1, sort_keys=True))
            return 0
        archive = args.archive
        if archive is None:
            archive = _local() / "downloads" / PINS["archive_name"]
            if not archive.exists():
                download(PINS["url"], archive, PINS)
        target = install_archive(archive)
        print(json.dumps({"installed": str(target), "current": str(_local() / "current")}, sort_keys=True))
        print(json.dumps(verify(), sort_keys=True))
        return 0
    except (ProvisionError, bounded.BoundedError, OSError, tarfile.TarError) as error:
        print("provision_libreoffice: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
