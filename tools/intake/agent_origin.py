"""Declared origin of a cohort of agent-written context (``cohort classify|ingest|qualify --agent-origin FILE``).

An origin file (version 1, private instance configuration) says which AI agent wrote the inbox,
which inbox-relative paths belong to the cohort (``include``, default the whole inbox) and which
do not (``exclude``, reported as ``excluded``), an optional ``source_repository`` label and whether
per-file provenance is read from git (``provenance``: ``git`` or ``none``). Every admitted file is
submitted with an ``agent_generated`` declaration (``upload.descriptor``), so the context index
labels it low confidence (``agent-generated``) with the generator, date and commit. Review and
qualification are unchanged: the declaration never admits or withholds anything by itself.

With ``git`` provenance the inbox must be the top level of a git work tree. A file that is tracked
and unmodified is declared with the last commit that touched it and that commit's author date
(``committed``); a modified, untracked or ignored file carries neither (``uncommitted``), because its
bytes are in no commit. git runs read-only: no optional locks, no fsmonitor, no signatures or pager.
"""
import json
import os
from pathlib import Path
import subprocess

from . import archives, upload
from .contracts import IntakeError, relative, text

VERSION = 1
FIELDS = {"version", "generator", "source_repository", "provenance", "include", "exclude"}
PROVENANCE = ("git", "none")
FILE_LIMIT = 64 * 1024
LIST_LIMIT = 1000
GIT_TIMEOUT = 120
GIT = ("git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "-c", "log.showSignature=false",
       "-c", "core.pager=cat", "--no-optional-locks", "--no-pager")


def _invalid(condition):
    if not condition:
        raise IntakeError("invalid_agent_origin")


def validate(value):
    """A validated origin file value; ``include`` is ``None`` for the whole inbox."""
    _invalid(isinstance(value, dict) and {"version", "generator"} <= set(value) <= FIELDS)
    _invalid(type(value["version"]) is int and value["version"] == VERSION)
    origin = {"generator": value["generator"], "source_repository": value.get("source_repository"),
              "provenance": value.get("provenance", "none"), "include": value.get("include"),
              "exclude": value.get("exclude", [])}
    try:
        text(origin["generator"], 200)
        if origin["source_repository"] is not None:
            text(origin["source_repository"], 200)
        for paths in (origin["include"] or [], origin["exclude"]):
            _invalid(isinstance(paths, list) and len(paths) <= LIST_LIMIT)
            for path in paths:
                relative(path)
    except IntakeError as error:
        raise IntakeError("invalid_agent_origin") from error
    _invalid(origin["provenance"] in PROVENANCE)
    _invalid(origin["include"] is None or (isinstance(origin["include"], list) and origin["include"]))
    return origin


def load(path):
    """Read and validate an origin file (at most 64 KiB of UTF-8 JSON)."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(FILE_LIMIT + 1)
        _invalid(len(raw) <= FILE_LIMIT)
        return validate(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise IntakeError("invalid_agent_origin") from error


def _under(path, prefixes):
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def excluded(origin, path):
    """True when the inbox-relative ``path`` is excluded from the cohort."""
    return origin is not None and _under(path, origin["exclude"])


def starts(origin, inbox):
    """``(relative, path)`` of each walk start below ``inbox``: the inbox itself or each include."""
    if origin is None or origin["include"] is None:
        return [("", inbox)]
    found = []
    for include in sorted(set(origin["include"])):
        if any(_under(include, [other]) for other in origin["include"] if other != include):
            continue  # walked from its enclosing include
        path = inbox / include
        if not os.path.lexists(path):
            raise IntakeError("agent_origin_include_missing")
        found.append((include, path))
    return found


def _git(inbox, *args):
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                   "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        return subprocess.run([*GIT, "-C", str(inbox), *args], capture_output=True, check=True,
                              timeout=GIT_TIMEOUT, env=environment, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise IntakeError("agent_origin_git_unavailable") from error


def _decode(raw):
    return raw.decode("utf-8", "surrogateescape")


def _last_commits(raw):
    """``{path: (commit, author date)}`` from ``git log --name-only -z`` output, newest first."""
    found = {}
    for chunk in raw.split(b"\x01")[1:]:
        header, _, names = chunk.partition(b"\0")
        commit, date = _decode(header).split(" ")
        for name in names.lstrip(b"\n").split(b"\0"):
            if name:
                found.setdefault(_decode(name), (commit, date))
    return found


def _modified(raw):
    """Paths ``git status --porcelain=v1 -z`` lists (renames and copies name the new path first)."""
    fields_ = raw.split(b"\0")
    found, index = set(), 0
    while index < len(fields_):
        entry = fields_[index]
        index += 1
        if len(entry) < 4:
            continue
        found.add(_decode(entry[3:]))
        if entry[:1] in (b"R", b"C"):
            index += 1
    return found


def git_provenance(inbox, origin):
    """``{"head", "commits": {path: (commit, date)}, "modified": set}`` for the inbox work tree."""
    inbox = Path(inbox).resolve()
    top = _decode(_git(inbox, "rev-parse", "--show-toplevel")).strip()
    if Path(top).resolve() != inbox:
        raise IntakeError("agent_origin_not_repository_root")
    pathspec = ["--", *(origin["include"] or ["."])]
    try:
        head = _decode(_git(inbox, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")).strip()
    except IntakeError:
        return {"head": None, "commits": {}, "modified": set()}
    log = _git(inbox, "log", "--no-renames", "--format=%x01%H %as", "--name-only", "-z", *pathspec)
    status = _git(inbox, "status", "--porcelain=v1", "-z", "--untracked-files=no", *pathspec)
    # History also names deleted paths; only files in HEAD's tree can have committed bytes.
    tracked = {_decode(name) for name in
               _git(inbox, "ls-tree", "-r", "-z", "--name-only", "HEAD", *pathspec).split(b"\0") if name}
    commits = {path: found for path, found in _last_commits(log).items() if path in tracked}
    return {"head": head, "commits": commits, "modified": _modified(status)}


def declare(inbox, origin, entries):
    """Give each admitted entry its ``agent_generated`` declaration and ``provenance``; return the
    body-free ``agent_origin`` counts block."""
    known = git_provenance(inbox, origin) if origin["provenance"] == "git" else None
    counts = {"committed": 0, "uncommitted": 0, "none": 0}
    for entry in entries:
        if entry["disposition"] != "admit":
            continue
        commit = date = None
        state = "none"
        if known is not None:
            state = "uncommitted"
            # An expanded archive's members are declared with the archive file they came from.
            path = entry["relative"].split(archives.SEPARATOR, 1)[0]
            if path in known["commits"] and path not in known["modified"]:
                commit, date = known["commits"][path]
                state = "committed"
        declaration = {"generator": origin["generator"], "generated_on": date, "source_commit": commit}
        if origin["source_repository"] is not None:
            declaration["source_repository"] = origin["source_repository"]
        upload.agent_declaration(declaration)
        entry.update(agent_generated=declaration, provenance=state)
        counts[state] += 1
    return {"generator": origin["generator"], "source_repository": origin["source_repository"],
            "provenance": origin["provenance"], "head": known and known["head"],
            "include": origin["include"], "exclude": origin["exclude"],
            "declared": counts}
