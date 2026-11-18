"""Explicit, checked expansion of zip and tar archives into instance storage (Kata 12sc).

An archive is read with the standard library only (``zipfile``, ``tarfile``) and never extracted by
those modules: every member name is checked and normalized here, and member bytes are streamed into
``<intake home>/archives/<archive sha256>/members/<path>`` with their SHA-256. The expansion and its
``manifest.json`` (archive size and hash, and every member's path, size, hash and disposition) are
built in a private partial directory and published by one rename, so a crash leaves either nothing
or a complete expansion. Nothing here writes the inbox; ``cohort_archives`` owns the one explicit
inbox write (deleting a verified original).

Member dispositions: ``extracted`` (bytes stored), ``pre-expanded`` (equal bytes found in an
operator's sibling ``_extracted/<stem>/`` folder), ``skipped`` (links and special files, which carry
no content: ``link``, ``special``) and ``refused`` (``unsafe-name``, ``duplicate-name``,
``case-collision``, ``path-conflict``, ``encrypted``, ``unsupported-compression``, ``corrupt-member``,
``pre-expanded-missing``, ``pre-expanded-differs``). Archive-level refusals (``status: refused``):
``archive-unreadable``, ``too-many-members``, ``expanded-size-cap``, ``ratio-cap``.
"""
from collections import Counter
from contextlib import contextmanager, nullcontext
import bz2
import fcntl
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import unicodedata
import uuid
import zipfile
import zlib

from tools.durable import fsync_directory, sha256_file, write_new_private
from . import store, upload
from .contracts import IntakeError, require

SCHEMA = "librarian-archive-manifest/1"
DIRECTORY = "archives"
MANIFEST = "manifest.json"
MEMBERS = "members"
LOCK = ".lock"
PARTIAL = ".partial-"
SEPARATOR = "!/"
SUFFIXES = {".zip": "zip", ".tar": "tar", ".tgz": "tar", ".tbz2": "tar", ".txz": "tar",
            ".tar.gz": "tar", ".tar.bz2": "tar", ".tar.xz": "tar"}
# Zip-bomb and resource caps for one archive (one nesting level).
MAX_MEMBERS = 100_000
MAX_EXPANDED_BYTES = 16 * 1024 ** 3
MAX_RATIO = 200  # expanded bytes per archive byte, enforced once RATIO_FLOOR bytes are expanded
RATIO_FLOOR = 64 * 1024 ** 2
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_COMPONENT_BYTES = 255
CHUNK = 1024 * 1024
LIMITS = {"max_members": MAX_MEMBERS, "max_expanded_bytes": MAX_EXPANDED_BYTES,
          "max_ratio": MAX_RATIO, "ratio_floor": RATIO_FLOOR}
SKIPPED = ("link", "special")
UNREADABLE = ("encrypted", "unsupported-compression", "corrupt-member")
READ_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, tarfile.TarError, EOFError, OSError,
               zlib.error, lzma.LZMAError, ValueError, RuntimeError, NotImplementedError)


class Refused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def kind(name):
    """``zip``, ``tar`` or ``None`` from a file name's (case-insensitive) suffix."""
    lower = name.lower()
    return next((SUFFIXES[s] for s in sorted(SUFFIXES, key=len, reverse=True) if lower.endswith(s)), None)


def stem(name):
    """The name without its archive suffix (``a.tar.gz`` -> ``a``; ``a.zip`` -> ``a``)."""
    lower = name.lower()
    suffix = next(s for s in sorted(SUFFIXES, key=len, reverse=True) if lower.endswith(s))
    return name[:-len(suffix)]


def member_path(name):
    """The normalized relative path of one member name, or ``None`` when it is unsafe.

    Refused: empty names, absolute paths, ``..``, drive letters and any ``:``, backslashes, control
    characters, components longer than 255 bytes, and components ending in ``!`` (which would make a
    ``<archive>!/<member>`` provenance ambiguous). Empty and ``.`` components are dropped and the
    name is NFC-normalized.
    """
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or ":" in name:
        return None
    name = unicodedata.normalize("NFC", name)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.endswith("!") or len(p.encode("utf-8", "surrogatepass")) > MAX_COMPONENT_BYTES
                        for p in parts):
        return None
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return "/".join(parts)


def _ancestors(path):
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _zip_entries(handle):
    with zipfile.ZipFile(handle) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise Refused("too-many-members")
        if sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
            raise Refused("expanded-size-cap")
        for info in infos:
            mode = info.external_attr >> 16 if info.create_system == 3 else 0
            if info.is_dir() or stat.S_ISDIR(mode):
                yield info.filename, "directory", 0, None
            elif stat.S_ISLNK(mode):
                yield info.filename, "link", info.file_size, None
            elif stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                yield info.filename, "special", info.file_size, None
            elif info.flag_bits & 0x1:
                yield info.filename, "encrypted", info.file_size, None
            else:
                yield info.filename, "file", info.file_size, lambda info=info: archive.open(info)


# A tar stream's headers and padding add at most this much to its members' bytes before
# ``_TarBudget`` refuses it (a pax header can be large, so this is generous).
TAR_OVERHEAD_PER_MEMBER = 64 * 1024
COMPRESSED_TAR = {b"\x1f\x8b": "gzip", b"BZh": "bz2", b"\xfd7zXZ\x00": "xz"}


class _TarBudget:
    """A decompressed tar stream that counts every byte ``tarfile`` decompresses, including the
    payloads of refused or skipped members it seeks past, against the expanded-size and ratio caps."""

    def __init__(self, stream, archive_bytes):
        self.stream, self.archive_bytes = stream, archive_bytes

    def _check(self):
        position = self.stream.tell()
        if position > MAX_EXPANDED_BYTES + TAR_OVERHEAD_PER_MEMBER * MAX_MEMBERS:
            raise Refused("expanded-size-cap")
        if position > RATIO_FLOOR and position > MAX_RATIO * max(self.archive_bytes, 1):
            raise Refused("ratio-cap")

    def read(self, size=-1):
        if size is None or size < 0:
            return b"".join(iter(lambda: self.read(CHUNK), b""))
        data = self.stream.read(size)
        self._check()
        return data

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_CUR:
            offset, whence = self.stream.tell() + offset, os.SEEK_SET
        if whence != os.SEEK_SET or offset < self.stream.tell():
            return self.stream.seek(offset, whence)
        # A forward seek decompresses everything it skips: do it in checked reads.
        while self.stream.tell() < offset and self.read(min(CHUNK, offset - self.stream.tell())):
            pass
        return self.stream.tell()

    def tell(self):
        return self.stream.tell()


def _decompressed(handle):
    """The tar stream inside ``handle``: gzip, bzip2 or xz by magic bytes, else ``handle`` itself."""
    magic = handle.read(6)
    handle.seek(0)
    compression = next((name for prefix, name in COMPRESSED_TAR.items() if magic.startswith(prefix)), None)
    if compression == "gzip":
        return gzip.GzipFile(fileobj=handle, mode="rb")
    if compression == "bz2":
        return bz2.BZ2File(handle, "rb")
    if compression == "xz":
        return lzma.LZMAFile(handle, "rb")
    return nullcontext(handle)


def _tar_entries(handle, archive_bytes):
    with _decompressed(handle) as stream, tarfile.open(fileobj=_TarBudget(stream, archive_bytes), mode="r:") as archive:
        while (info := archive.next()) is not None:
            if info.isdir():
                yield info.name, "directory", 0, None
            elif info.issym() or info.islnk():
                yield info.name, "link", 0, None
            elif not info.isreg():
                yield info.name, "special", 0, None
            else:
                yield info.name, "file", info.size, lambda info=info: archive.extractfile(info)
            # tarfile keeps every header it read; a long archive must not grow memory without bound.
            archive.members.clear()


class _Run:
    """One pass over one archive: member checks, caps, hashing, and optional writing or comparing."""

    def __init__(self, archive_bytes, sink=None, sibling=None, inbox=None):
        self.archive_bytes = archive_bytes
        self.sink, self.sibling, self.inbox = sink, sibling, inbox
        self.members, self.exact, self.folded, self.dirs = [], set(), set(), set()
        self.total = self.count = self.directories = 0
        self.created = []

    def charge(self, size):
        self.total += size
        if self.total > MAX_EXPANDED_BYTES:
            raise Refused("expanded-size-cap")
        if self.total > RATIO_FLOOR and self.total > MAX_RATIO * max(self.archive_bytes, 1):
            raise Refused("ratio-cap")

    def add(self, name, what, size, opener):
        self.count += 1
        if self.count > MAX_MEMBERS:
            raise Refused("too-many-members")
        path = member_path(name)
        if what == "directory":
            self.directories += 1
            if path is not None:
                self.dirs.update(p.casefold() for p in (*_ancestors(path), path))
            return
        record = {"name": name, "path": path, "bytes": size, "sha256": None}
        if what != "file":
            code = what
            record.update(disposition="skipped" if what in SKIPPED else "refused", code=code)
        elif path is None:
            record.update(disposition="refused", code="unsafe-name")
        elif path in self.exact:
            record.update(disposition="refused", code="duplicate-name")
        elif path.casefold() in self.folded:
            record.update(disposition="refused", code="case-collision")
        elif path.casefold() in self.dirs or any(a.casefold() in self.folded for a in _ancestors(path)):
            record.update(disposition="refused", code="path-conflict")
        else:
            self.exact.add(path)
            self.folded.add(path.casefold())
            self.dirs.update(a.casefold() for a in _ancestors(path))
            record.update(self.read(path, size, opener))
        self.members.append(record)

    def read(self, path, declared, opener):
        """Stream one member: hash it, count it against the caps, and store or compare it."""
        if self.total + declared > MAX_EXPANDED_BYTES:
            raise Refused("expanded-size-cap")
        target = self._target(path) if self.sink is not None else None
        digest, size = hashlib.sha256(), 0
        try:
            with opener() as stream, (target or _Null()) as out:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    size += len(chunk)
                    self.charge(len(chunk))
                    if size > declared:
                        raise _Corrupt()
                    digest.update(chunk)
                    out.write(chunk)
                if target is not None:
                    out.flush()
                    os.fsync(out.fileno())
        except NotImplementedError:
            return self._lost(path, "unsupported-compression")
        except (_Corrupt, zipfile.BadZipFile, EOFError, zlib.error, lzma.LZMAError, OSError, RuntimeError):
            return self._lost(path, "corrupt-member")
        if size != declared:
            return self._lost(path, "corrupt-member")
        result = {"bytes": size, "sha256": digest.hexdigest(), "disposition": "extracted", "code": None}
        if self.sibling is not None:
            result.update(self._compare(path, result["sha256"]))
        return result

    def _lost(self, path, code):
        if self.sink is not None:
            (self.sink / path).unlink(missing_ok=True)
        return {"disposition": "refused", "code": code, "sha256": None}

    def _target(self, path):
        cursor = self.sink
        for part in path.split("/")[:-1]:
            cursor = cursor / part
            if not cursor.exists():
                cursor.mkdir(mode=0o700)
                self.created.append(cursor)
        descriptor = os.open(self.sink / path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return os.fdopen(descriptor, "wb")

    def _compare(self, path, sha256):
        """Match a member against the operator's sibling folder: ``pre-expanded`` only for equal bytes."""
        cursor = self.sibling
        for part in path.split("/"):
            cursor = cursor / part
            if cursor.is_symlink():
                return {"disposition": "refused", "code": "pre-expanded-missing"}
        if not cursor.is_file():
            return {"disposition": "refused", "code": "pre-expanded-missing"}
        if sha256_file(cursor) != sha256:
            return {"disposition": "refused", "code": "pre-expanded-differs"}
        return {"disposition": "pre-expanded", "source": cursor.relative_to(self.inbox).as_posix()}

    def sync(self):
        for directory in reversed(self.created):
            fsync_directory(directory)
        fsync_directory(self.sink)


class _Corrupt(Exception):
    pass


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def write(self, _):
        pass


def _open(path):
    """The archive opened once with ``O_NOFOLLOW``, its stat identity and SHA-256 from that descriptor."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    handle = os.fdopen(descriptor, "rb")
    before = os.fstat(handle.fileno())
    if not stat.S_ISREG(before.st_mode):
        handle.close()
        raise IntakeError("not_regular_file")
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(CHUNK), b""):
        digest.update(chunk)
    handle.seek(0)
    return handle, before, digest.hexdigest()


def read(path, relative, *, sink=None, sibling=None, inbox=None):
    """Read one archive in one pass and return its manifest.

    ``sink`` (an empty private directory) receives member bytes; ``sibling`` (with ``inbox``) is an
    operator's pre-expanded folder to compare members against instead. Neither: a dry run that
    reads and hashes every member and writes nothing. Raises ``IntakeError("changed_input")`` when
    the archive changes while it is read.
    """
    archive_kind = kind(Path(path).name)
    require(archive_kind is not None, "not_an_archive")
    handle, before, sha256 = _open(path)
    run = _Run(before.st_size, sink, sibling, inbox)
    status, code = ("pre-expanded" if sibling is not None else "expanded"), None
    with handle:
        try:
            entries = _zip_entries(handle) if archive_kind == "zip" else _tar_entries(handle, before.st_size)
            for name, what, size, opener in entries:
                run.add(name, what, size, opener)
        except Refused as refusal:
            status, code = "refused", refusal.code
        except READ_ERRORS:
            status, code = "refused", "archive-unreadable"
        after = os.fstat(handle.fileno())
    if upload.snapshot(before) != upload.snapshot(after):
        raise IntakeError("changed_input")
    return {"schema": SCHEMA, "status": status, "code": code, "limits": dict(LIMITS),
            "archive": {"relative": relative, "kind": archive_kind, "bytes": before.st_size, "sha256": sha256},
            "expanded_bytes": run.total, "directories": run.directories,
            "members": run.members if status != "refused" else [],
            "examined": len(run.members), "_run": run}


def summary(manifest):
    """Body-free counts of one manifest: members by disposition and by code, nested archives."""
    members = manifest["members"]
    return {"members": len(members), "directories": manifest["directories"],
            "expanded_bytes": manifest["expanded_bytes"],
            "member_dispositions": dict(Counter(m["disposition"] for m in members)),
            "member_codes": dict(Counter(m["code"] for m in members if m["code"])),
            "nested_archives": sum(bool(m["path"] and kind(m["path"].rsplit("/", 1)[-1]))
                                   and m["disposition"] in ("extracted", "pre-expanded") for m in members)}


def store_dir(root, create=False):
    """``<intake home>/archives``, or ``None`` when it does not exist and ``create`` is false."""
    _, home, _ = store.locations(root)
    directory = store.safe(home / DIRECTORY)
    if create:
        store.private(home, True)
        upload.private_directory(home, Path(DIRECTORY), 1)
    elif not directory.is_dir():
        return None
    return store.private(directory, True)


def members_dir(directory, sha256):
    return store.safe(Path(directory) / sha256 / MEMBERS)


def manifest_bytes(directory, sha256):
    path = store.safe(Path(directory) / sha256 / MANIFEST)
    if not path.is_file():
        return None
    return upload.read_file(path, MAX_MANIFEST_BYTES)


def manifest_sha256(directory, sha256):
    return hashlib.sha256(manifest_bytes(directory, sha256)).hexdigest()


def load(directory, sha256):
    """The saved manifest of the archive with this SHA-256, or ``None`` when it was never expanded."""
    require(re.fullmatch(r"[a-f0-9]{64}", sha256) is not None, "invalid_sha256")
    raw = manifest_bytes(directory, sha256)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise IntakeError("archive_manifest_invalid") from error
    require(isinstance(value, dict) and value.get("schema") == SCHEMA
            and value.get("archive", {}).get("sha256") == sha256, "archive_manifest_invalid")
    return value


@contextmanager
def locked(directory):
    """Serialize writers of the archive store (expansion and deletion)."""
    descriptor = os.open(Path(directory) / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def clear_partials(directory):
    """Remove partial expansions a crashed writer left behind (call holding ``locked``)."""
    for stale in Path(directory).glob(PARTIAL + "*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
    fsync_directory(directory)


def expand(directory, path, relative, *, sibling=None, inbox=None):
    """Expand (or, with ``sibling``, verify) one archive into ``directory`` and publish its manifest.

    Call holding ``locked(directory)``. Idempotent by archive SHA-256: an archive whose manifest exists
    is not read again and its saved manifest is returned. A refused archive publishes a manifest with
    ``status: refused`` and no member bytes; remove ``<sha256>/`` to retry it under other caps.
    """
    directory = Path(directory)
    partial = directory / (PARTIAL + uuid.uuid4().hex)
    partial.mkdir(mode=0o700)
    try:
        sink = None
        if sibling is None:
            sink = partial / MEMBERS
            sink.mkdir(mode=0o700)
        manifest = read(path, relative, sink=sink, sibling=sibling, inbox=inbox)
        run = manifest.pop("_run")
        final = directory / manifest["archive"]["sha256"]
        existing = load(directory, manifest["archive"]["sha256"])
        if existing is not None:
            return existing
        if sink is not None:
            if manifest["status"] == "refused":
                shutil.rmtree(sink)
            else:
                run.sync()
        write_new_private(partial / MANIFEST, (json.dumps(manifest, indent=1, sort_keys=True,
                                                          ensure_ascii=False) + "\n").encode("utf-8"))
        fsync_directory(partial)
        os.rename(partial, final)
        fsync_directory(directory)
        return manifest
    finally:
        if partial.exists():
            shutil.rmtree(partial)
