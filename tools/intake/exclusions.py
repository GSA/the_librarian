"""Instance exclusion rules for inbox cohorts (``cohort classify|ingest|qualify``; Kata bvvc).

An exclusion file is private instance configuration (version 1, at most 64 KiB; template
``config/intake-exclusions.example.json``; the instance copy is ``config/intake-exclusions.local.json``,
ignored by Git, and is read automatically when it exists). It names low-value material that must not
reach model review: dependency trees, VCS internals, build-tool caches.

    {"version": 1, "rules": [
      {"id": "node-modules", "reason": "dependency tree", "globs": ["**/node_modules/**"]},
      {"id": "maven-cache", "extensions": [".pom", ".lastUpdated", ".sha1", ".repositories"]}]}

A rule has a unique ``id`` (lower-case letters, digits and hyphens), an optional ``reason`` and at
least one of ``globs`` and ``extensions``. A glob is matched against the whole inbox-relative path
(``ARCHIVE!/MEMBER`` for an archive member): ``*`` and ``?`` stay within one path segment, ``**``
crosses segments (``**/`` any leading directories, ``/**`` anything below). An extension (with its
leading dot, compared case-insensitively) matches the end of the file name. The first matching rule
in file order wins.

A matched file is classified as usual and then reported ``excluded`` with its ``rule`` and what it
``underlying``-ly would have been. A file that would have been ``admit`` still gets a receipt (its
bytes are retained and registered, so a retirement manifest can join it) but is never extracted,
sent to model review or selected; any other file is simply reported ``excluded``. The rules never
touch the standing policy: they narrow what ``qualify`` reviews and cannot widen anything.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from .contracts import IntakeError

VERSION = 1
FILE_LIMIT = 64 * 1024
RULE_LIMIT = 200
LIST_LIMIT = 200
DEFAULT_FILE = "config/intake-exclusions.local.json"
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
FIELDS = {"version", "rules"}
RULE_FIELDS = {"id", "reason", "globs", "extensions"}


def _invalid(condition):
    if not condition:
        raise IntakeError("invalid_exclusions")


def _glob(pattern):
    """The compiled regular expression of one glob (see the module doc)."""
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("/**", index) and index + 3 == len(pattern):
            out.append("(?:/.*)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out), re.DOTALL)


def _texts(value, check):
    _invalid(isinstance(value, list) and len(value) <= LIST_LIMIT)
    for item in value:
        _invalid(isinstance(item, str) and check(item))
    return value


def validate(value):
    """The validated rules: ``[{id, reason, globs, extensions, patterns}]`` in file order."""
    _invalid(isinstance(value, dict) and set(value) == FIELDS and type(value["version"]) is int
             and value["version"] == VERSION)
    rules, seen = value["rules"], set()
    _invalid(isinstance(rules, list) and 0 < len(rules) <= RULE_LIMIT)
    found = []
    for rule in rules:
        _invalid(isinstance(rule, dict) and {"id"} <= set(rule) <= RULE_FIELDS
                 and isinstance(rule["id"], str) and ID.fullmatch(rule["id"]) and rule["id"] not in seen)
        seen.add(rule["id"])
        reason = rule.get("reason")
        _invalid(reason is None or (isinstance(reason, str) and 0 < len(reason) <= 200 and reason.isprintable()))
        globs = _texts(rule.get("globs", []), lambda g: (
            0 < len(g) <= 500 and "\0" not in g and "\\" not in g and not g.startswith("/")
            and ".." not in g.split("/") and "" not in g.split("/")[:-1]))
        extensions = _texts(rule.get("extensions", []), lambda e: (
            2 <= len(e) <= 32 and e.startswith(".") and "/" not in e and "\0" not in e))
        _invalid(globs or extensions)
        found.append({"id": rule["id"], "reason": reason, "globs": list(globs), "extensions": list(extensions),
                      "patterns": [_glob(g) for g in globs],
                      "suffixes": tuple(e.lower() for e in extensions)})
    return found


class Rules:
    """Validated exclusion rules plus the SHA-256 of the file they were read from."""

    def __init__(self, rules, sha256):
        self.rules, self.sha256 = rules, sha256

    @property
    def ids(self):
        return [rule["id"] for rule in self.rules]

    def match(self, relative):
        """The id of the first rule that excludes the inbox-relative ``relative``, or ``None``."""
        name = relative.rsplit("/", 1)[-1].lower()
        for rule in self.rules:
            if name.endswith(rule["suffixes"]) if rule["suffixes"] else False:
                return rule["id"]
            if any(pattern.fullmatch(relative) for pattern in rule["patterns"]):
                return rule["id"]
        return None

    def summary(self):
        return {"sha256": self.sha256, "rules": self.ids}


def parse(raw):
    try:
        _invalid(len(raw) <= FILE_LIMIT)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError) as error:
        raise IntakeError("invalid_exclusions") from error
    return Rules(validate(value), hashlib.sha256(raw).hexdigest())


def _pairs(items):
    found = {}
    for key, value in items:
        _invalid(key not in found)
        found[key] = value
    return found


def _read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read(FILE_LIMIT + 1)
    except OSError as error:
        raise IntakeError("invalid_exclusions") from error


def load(path):
    """Read and validate an explicitly named exclusion file."""
    return parse(_read(path))


def discover(root):
    """The instance's ``config/intake-exclusions.local.json`` rules, or ``None`` when it does not exist.

    A symlinked file or directory is refused, as for every other instance file.
    """
    root = Path(root)
    path = root / DEFAULT_FILE
    for part in (path, *path.relative_to(root).parents):
        if part != Path(".") and (root / part if not part.is_absolute() else part).is_symlink():
            raise IntakeError("unsafe_path")
    if not os.path.lexists(path):
        return None
    return load(path)


def receipted(entry):
    """True for an excluded file that still gets a receipt (it would have been admitted)."""
    return entry.get("disposition") == "excluded" and entry.get("underlying") == "admit"


def mark(entry, rules):
    """Report ``entry`` ``excluded`` by the first matching rule; return whether one matched."""
    if rules is None:
        return False
    rule = rules.match(entry["relative"])
    if rule is None:
        return False
    # A stat-trusted entry must not be handed to the extraction fast path: it is not extracted.
    entry.pop("_fast", None)
    entry.update(underlying=entry["disposition"], disposition="excluded", rule=rule)
    return True


def counts(entries, rules, ingested=False):
    """Body-free counts of what ``rules`` excluded, for a report.

    ``receipt_expected`` is how many of the files get a receipt (those that would have been admitted);
    an ``ingested`` run adds ``received`` and each such file's ``outcome``.
    """
    excluded = [e for e in entries if e.get("rule") is not None]
    found = {**rules.summary(), "files": len(excluded),
             "by_rule": dict(sorted(_tally(e["rule"] for e in excluded).items())),
             "underlying": dict(sorted(_tally(e["underlying"] for e in excluded).items())),
             "bytes": sum(e.get("bytes") or 0 for e in excluded),
             "receipt_expected": sum(receipted(e) for e in excluded)}
    if ingested:
        found["received"] = sum(bool(e.get("received")) for e in excluded)
        found["outcome"] = dict(sorted(_tally(e.get("outcome", "none") for e in excluded
                                              if receipted(e)).items()))
    return found


def _tally(items):
    found = {}
    for item in items:
        found[item] = found.get(item, 0) + 1
    return found


def resolve(root, explicit, origin):
    """The rules a command applies: the named file, else the instance's, else ``None``.

    The rules are for inbox cohorts; an agent-origin cohort has its own ``exclude`` list, so naming a
    file with it is refused and the instance file is not applied to it.
    """
    if origin is not None:
        if explicit is not None:
            raise IntakeError("exclusions_not_for_agent_origin")
        return None
    return load(explicit) if explicit is not None else discover(root)
