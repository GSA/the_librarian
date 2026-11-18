#!/usr/bin/env python3
"""Local, derived Markdown retrieval. No source writes or remote access."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import types
import unicodedata
from urllib.parse import quote, unquote
import uuid

from tools import policy
from tools.context_index import confidence, renditions


SCHEMA = 1
APPLICATION_ID = 0x4C435458
SUPERSEDED_APPLICATION_ID = 0x46435458
NOTICE = (
    "Local retrieval for already-authorized local users only. This label is "
    "not access authorization, release permission, verification, or operational acceptance. "
    "Roles are retrieval routing labels, not review or acceptance assessments."
)
DISCOVERY = (
    "Discovery only: excerpts are incomplete, not qualified answers or verified "
    "facts. Use get on a locator for exact text and required context."
)
ABSENCE = {
    "absent-corpus": "No query term appears in the searched scope. This reports what has not been "
                     "collected and indexed; it is not evidence that the subject does not exist.",
    "absent-partial": "Some query terms do not appear in the searched scope. Coverage is partial; "
                      "this is not evidence about the missing subject matter.",
    "present-uncombined": "Every query term appears in the searched scope, but no indexed section "
                          "contains them together. This is a retrieval or selection limit, not an "
                          "absence of collected material.",
}
ANCHOR = re.compile(r'<a\s+id=["\']([^"\']+)["\']\s*>\s*</a>', re.I)
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
REFERENCE = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))", re.M)


class IndexError_(Exception):
    """An explicit input, freshness, storage, or retrieval failure."""


def fail(message):
    raise IndexError_(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def engine_fingerprint():
    return digest(b"\0".join(path.read_bytes() for path in (
        Path(__file__), no_symlinks(Path(__file__).with_name("compact_records.py")),
        no_symlinks(Path(__file__).with_name("renditions.py")),
        no_symlinks(Path(__file__).with_name("qualification.py")),
        no_symlinks(Path(__file__).with_name("packets.py")),
        no_symlinks(Path(__file__).with_name("confidence.py")),
        no_symlinks(Path(policy.__file__)),
    )))


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            fail(f"Symlinks are not allowed: {part}")
    return path


def relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        fail(f"Invalid root-relative path: {value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        fail(f"Invalid root-relative path: {value!r}")
    return value


def eligible(path, root=None):
    return isinstance(path, str) and path.endswith(".md") and policy.allowed(path, "markdown_roots", root)


def source_path(root, name):
    relative_name(name)
    if not eligible(name, root):
        fail(f"Forbidden source path (not a maintained Markdown home): {name}")
    path = no_symlinks(root / name)
    if not path.is_file():
        fail(f"Missing or non-regular source: {name}")
    return path


def canonical_locator(value, root=None):
    if not isinstance(value, str):
        fail("A locator must be a string")
    path, separator, fragment = value.partition("#")
    relative_name(path)
    if not eligible(path, root) and not renditions.eligible(path, root):
        fail(f"Forbidden locator path: {path}")
    fragment = unquote(fragment)
    if any(c in fragment for c in ("\\0", "\\n", "\\r")):
        fail("Invalid locator fragment")
    return path + ("#" + fragment if separator and fragment else "")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def fields(value, expected, description, optional=()):
    if (not isinstance(value, dict) or not set(expected) <= set(value)
            or set(value) - set(expected) - set(optional)):
        suffix = f"; optional: {', '.join(optional)}" if optional else ""
        fail(f"{description} must contain exactly these fields: {', '.join(expected)}{suffix}")


def load_event_reader():
    try:
        from tools.review_events import event_reader
    except ImportError as error:
        fail(f"Event reader unavailable; install ./tools/context-mcp: {error}")
    return event_reader


def load_catalog():
    try:
        from tools.catalog import catalog_store
    except ImportError as error:
        fail(f"Catalog reader unavailable: {error}")
    return catalog_store


def load_compaction():
    path = no_symlinks(Path(__file__).with_name("compact_records.py"))
    raw = path.read_bytes()
    module = types.ModuleType("_context_index_compact_records")
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    if path.read_bytes() != raw:
        fail("Compaction code changed during load; retry")
    return module


def original_allowed(path, root=None, settings=None):
    """``settings`` is a policy the caller already loaded for ``root``; omitted, it is read now."""
    if settings is None:
        return isinstance(path, str) and policy.allowed(path, "original_roots", root)
    return isinstance(path, str) and policy.allowed_in(settings, path, "original_roots")


def rendition_api(root=None):
    api = {
        name: globals()[name] for name in (
            "fail", "fields", "validate_context", "no_symlinks", "relative_name", "digest",
            "load_catalog", "load_event_reader", "strict_object", "eligible", "canonical_locator", "original_allowed",
        )
    }
    if root is not None:
        loaded = []

        def settings():
            # The instance policy is read once per API object (one operation), not once per selection.
            if not loaded:
                loaded.append(policy.for_root(root)[0])
            return loaded[0]
        api["eligible"] = lambda p: eligible(p, root)
        api["canonical_locator"] = lambda v: canonical_locator(v, root)
        api["original_allowed"] = lambda p: original_allowed(p, root, settings())
        api["validate_context"] = lambda c: validate_context(c, root)
    return types.SimpleNamespace(**api)


def load_manifest(path, root=None):
    path = no_symlinks(path)
    raw = path.read_bytes()
    config = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object)
    fields(config, ("schema_version", "audience", "documents", "context_rules"),
           "Manifest", optional=("renditions",))
    renditions.validate(config, rendition_api(root), root=root)
    if type(config["schema_version"]) is not int or config["schema_version"] != SCHEMA:
        fail(f"Unsupported manifest schema_version; expected {SCHEMA}")
    expected_audience = policy.for_root(root)[0]["audience"]
    if config["audience"] != expected_audience:
        fail(f"Manifest audience must be {expected_audience!r}")
    if not isinstance(config["documents"], list):
        fail("Manifest documents must be a list (empty is supported)")
    seen = set()
    for document in config["documents"]:
        fields(document, ("path", "role", "context"), "Document")
        name = relative_name(document["path"])
        if not eligible(name, root):
            fail(f"Forbidden source path: {name}")
        if name in seen:
            fail(f"Duplicate document: {name}")
        seen.add(name)
        if not isinstance(document["role"], str) or not document["role"].strip():
            fail(f"Document role must be a nonempty string: {name}")
        validate_context(document["context"], root=root)
    if not isinstance(config["context_rules"], dict):
        fail("context_rules must be an object")
    normalized = {}
    for locator, contexts in config["context_rules"].items():
        locator = canonical_locator(locator, root)
        if locator in normalized:
            fail(f"Duplicate normalized context rule: {locator}")
        validate_context(contexts, root=root)
        normalized[locator] = contexts
    config["context_rules"] = normalized
    return config, digest(raw)


def validate_context(context, root=None):
    if not isinstance(context, list):
        fail("Context must be a list of locators")
    for locator in context:
        canonical_locator(locator, root)


def github_slug(title):
    title = re.sub(r"<[^>]*>", "", title).lower()
    return "".join(
        char for char in title
        if char in "-_ " or unicodedata.category(char)[0] in "LNM"
    ).replace(" ", "-")


def visible_lines(lines):
    """Retain line positions while excluding fenced code from structure/link parsing."""
    result = []
    fence = None
    for line in lines:
        match = FENCE.match(line.rstrip("\r\n"))
        if fence:
            result.append("")
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
        elif match and not (match[1][0] == "`" and "`" in match[2]):
            fence = (match[1][0], len(match[1]))
            result.append("")
        else:
            result.append(line)
    return result


def parse_document(path, text):
    lines = text.splitlines(keepends=True)
    visible = visible_lines(lines)
    nodes = [{
        "anchor": "", "aliases": [], "title": path, "start": 0,
        "end": len(lines), "level": 0, "parent": None, "kind": "document",
    }]
    heading_stack = [0]
    used_slugs = set()
    pending = []

    def add(anchor, aliases, title, start, level, kind, parent):
        nodes.append({
            "anchor": anchor, "aliases": aliases, "title": title, "start": start,
            "end": len(lines), "level": level, "parent": parent, "kind": kind,
        })
        return len(nodes) - 1

    def flush():
        if pending:
            start = pending[0][0]
            aliases = [anchor for _, anchors in pending for anchor in anchors]
            for position in range(len(heading_stack) - 1, 0, -1):
                if nodes[heading_stack[position]]["kind"] == "entry":
                    del heading_stack[position:]
                    break
            entry = add(aliases[0], aliases[1:], aliases[0], start,
                        nodes[heading_stack[-1]]["level"] + 1, "entry", heading_stack[-1])
            heading_stack.append(entry)
            pending.clear()

    for number, line in enumerate(visible):
        if not line and lines[number].strip():
            flush()
        stripped = line.strip()
        anchors = ANCHOR.findall(line)
        standalone = bool(anchors) and not ANCHOR.sub("", stripped).strip()
        if standalone:
            pending.append((number, anchors))
            continue
        heading = HEADING.match(line.rstrip("\r\n"))
        if heading:
            level = len(heading[1])
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
            base = github_slug(title)
            slug, suffix = base, 0
            while slug in used_slugs:
                suffix += 1
                slug = f"{base}-{suffix}"
            used_slugs.add(slug)
            aliases = [a for _, group in pending for a in group] + anchors
            start = pending[0][0] if pending else number
            pending.clear()
            while len(heading_stack) > 1 and nodes[heading_stack[-1]]["level"] >= level:
                heading_stack.pop()
            node_id = add(slug, aliases, title, start, level, "heading", heading_stack[-1])
            heading_stack.append(node_id)
        elif stripped:
            flush()
            if anchors:
                kind = "table-row" if stripped.startswith("|") else "entry"
                add(anchors[0], anchors[1:], anchors[0], number,
                    nodes[heading_stack[-1]]["level"] + 1, kind, heading_stack[-1])
    flush()
    for index, node in enumerate(nodes[1:], 1):
        if node["kind"] == "table-row":
            node["end"] = node["start"] + 1
        else:
            for following in nodes[index + 1:]:
                if following["level"] <= node["level"]:
                    node["end"] = following["start"]
                    break
        children = [
            child["start"] for child in nodes[index + 1:]
            if child["start"] < node["end"] and child["parent"] == index
        ]
        node["lead_end"] = min(children, default=node["end"])
    nodes[0]["lead_end"] = nodes[1]["start"] if len(nodes) > 1 else len(lines)
    # A title heading's introduction is document context even for a later H1.
    introduction = []
    if nodes[0]["lead_end"]:
        introduction.append((0, nodes[0]["lead_end"]))
    if len(nodes) > 1 and nodes[1]["kind"] == "heading":
        introduction.append((nodes[1]["start"], nodes[1]["lead_end"]))
    aliases = {}
    for index, node in enumerate(nodes):
        for anchor in [node["anchor"], *node["aliases"]]:
            if anchor in aliases and aliases[anchor] != index:
                fail(f"Ambiguous duplicate anchor in {path}: #{anchor}")
            aliases[anchor] = index
        node["text"] = "".join(lines[node["start"]:node["end"]])
        node["lead_text"] = "".join(lines[node["start"]:node["lead_end"]])
        node["table_header"] = None
        if node["kind"] == "table-row":
            top = node["start"]
            while top > 0 and visible[top - 1].lstrip().startswith("|"):
                top -= 1
            if top + 1 < node["start"] and re.match(r"^\s*\|[\s:|\-]+\|\s*$", visible[top + 1]):
                node["table_header"] = (top, top + 2, "".join(lines[top:top + 2]))
    return nodes, aliases, introduction, lines


def validate_db_path(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = no_symlinks(path)
    if path.suffix != ".sqlite3":
        fail("Database output must have a .sqlite3 suffix")
    try:
        path.relative_to(root)
    except ValueError:
        fail("Database must be inside the specified corpus root")
    if path.exists() and not path.is_file():
        fail(f"Database path is not a regular file: {path}")
    return path


def readonly(path):
    if not path.is_file():
        fail(f"Index missing: {path}; run build")
    connection = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id == SUPERSEDED_APPLICATION_ID:
            fail("Superseded FCTX index identity; explicit migration or re-initialization is required")
        if application_id != APPLICATION_ID:
            fail(f"Refusing non-index database: {path}")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
            fail(f"Unsupported index database schema: {path}")
        connection.execute("SELECT key, value FROM metadata LIMIT 1").fetchall()
    except Exception:
        connection.close()
        raise
    return connection


def metadata_expected(root, manifest_hash, config=None, rendition_identity=None, review_snapshot=None):
    try:
        corpus_root = policy.logical_corpus_root(root)
        settings = policy.for_root(root)[0]
        policy_fp = policy.fingerprint(root)
    except ValueError as error:
        fail(str(error))
    if rendition_identity is None:
        _, rendition_identity = renditions.inputs(
            root, config or {}, rendition_api(root), review_snapshot=review_snapshot)
    return {
        "schema_version": str(SCHEMA), "engine_fingerprint": engine_fingerprint(),
        "manifest_hash": manifest_hash, "corpus_root": corpus_root,
        "audience": settings["audience"], "policy_hash": policy_fp,
        "rendition_identity": rendition_identity,
    }


def get_section(connection, locator, root=None):
    locator = canonical_locator(locator, root)
    row = connection.execute(
        "SELECT s.* FROM aliases a JOIN sections s ON s.id=a.section_id WHERE a.locator=?",
        (locator,),
    ).fetchone()
    if row is None:
        fail(f"Locator not indexed: {locator}; use search or select a manifest-listed section")
    return row


def validate_contexts(connection, config, root=None):
    for document in [*config["documents"], *config.get("renditions", [])]:
        for locator in document["context"]:
            get_section(connection, locator, root)
    for locator, contexts in config["context_rules"].items():
        get_section(connection, locator, root)
        for companion in contexts:
            get_section(connection, companion, root)


DDL = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
    path TEXT PRIMARY KEY, hash TEXT NOT NULL, role TEXT NOT NULL,
    introduction TEXT NOT NULL, reference_definitions TEXT NOT NULL
);
CREATE TABLE sections (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES documents(path),
    locator TEXT NOT NULL UNIQUE, title TEXT NOT NULL, kind TEXT NOT NULL,
    line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
    parent_id INTEGER REFERENCES sections(id), lead_end INTEGER NOT NULL,
    text TEXT NOT NULL, lead_text TEXT NOT NULL, table_header TEXT
);
CREATE TABLE aliases (locator TEXT PRIMARY KEY, section_id INTEGER REFERENCES sections(id));
CREATE TABLE rendition_documents (path TEXT PRIMARY KEY REFERENCES documents(path), payload TEXT NOT NULL);
CREATE TABLE rendition_locations (locator TEXT PRIMARY KEY REFERENCES sections(locator), payload TEXT NOT NULL);
CREATE TABLE document_confidence (
    path TEXT PRIMARY KEY REFERENCES documents(path),
    level TEXT NOT NULL CHECK (level IN ('normal', 'low')), label TEXT NOT NULL
);
CREATE VIRTUAL TABLE search_fts USING fts5(title, text, tokenize='unicode61');
"""


def snapshot_hashes(root, config):
    return {
        document["path"]: digest(source_path(root, document["path"]).read_bytes())
        for document in config["documents"]
    }


@contextmanager
def publication_lock(db):
    try:
        import fcntl
    except ImportError:
        fail("Index publication requires POSIX flock; build on a supported local host")
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = no_symlinks(db.with_name(db.name + ".lock"))
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another build holds this database's publication lock; retry after it finishes")
        yield
    finally:
        os.close(descriptor)


def build(root, manifest, db, config, manifest_hash, *, publication_guard=None):
    review_snapshot = (load_event_reader().read_snapshot(root, full=True)
                       if any(item["format"] == "docling" for item in config.get("renditions", []))
                       else None)
    selected, rendition_identity = renditions.inputs(
        root, config, rendition_api(root), review_snapshot=review_snapshot)
    expected = metadata_expected(root, manifest_hash, config, rendition_identity, review_snapshot)
    if db.exists():
        existing = readonly(db)
        existing.close()
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    no_symlinks(db.parent)
    # Only dedicated generated directories receive a permission change.
    local = root / "tools/context_index/.local"
    if local.exists():
        no_symlinks(local)
        local.chmod(0o700)
    if db.parent.name == ".local":
        db.parent.chmod(0o700)
    temporary = db.with_name(f".{db.name}.build-{uuid.uuid4().hex}.sqlite3")
    connection = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA}")
        connection.executescript(DDL)
        connection.executemany("INSERT INTO metadata VALUES (?,?)", expected.items())
        hashes = {}
        for document in config["documents"]:
            path = document["path"]
            raw = source_path(root, path).read_bytes()
            hashes[path] = digest(raw)
            text = raw.decode("utf-8")
            nodes, aliases, introduction, lines = parse_document(path, text)
            definitions = reference_definitions(text)
            intro = [(start, end, "".join(lines[start:end])) for start, end in introduction]
            connection.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (
                path, hashes[path], document["role"], json.dumps(intro), json.dumps(definitions),
            ))
            connection.execute("INSERT INTO document_confidence VALUES (?,?,?)",
                               (path, "normal", json.dumps(confidence.label())))
            ids = {}
            for index, node in enumerate(nodes):
                locator = path + ("#" + node["anchor"] if node["anchor"] else "")
                cursor = connection.execute(
                    "INSERT INTO sections (path,locator,title,kind,line_start,line_end,"
                    "parent_id,lead_end,text,lead_text,table_header) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (path, locator, node["title"], node["kind"], node["start"] + 1,
                     node["end"], ids.get(node["parent"]), node["lead_end"],
                     node["text"], node["lead_text"], json.dumps(node["table_header"])),
                )
                ids[index] = cursor.lastrowid
                # Index lead-ins, not overlapping whole descendant trees.
                connection.execute("INSERT INTO search_fts(rowid,title,text) VALUES (?,?,?)",
                                   (cursor.lastrowid, node["title"], node["lead_text"]))
            connection.executemany("INSERT INTO aliases VALUES (?,?)", (
                (path + ("#" + anchor if anchor else ""), ids[index])
                for anchor, index in aliases.items()
            ))
        renditions.store(connection, selected, rendition_api(root))
        validate_contexts(connection, config, root)
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            fail("Index integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            fail("Index relationship validation failed")
        counts = count_index(connection)
        connection.close()
        connection = None
        if load_manifest(manifest, root)[1] != manifest_hash:
            fail("Manifest changed during build; prior index preserved, retry build")
        if (snapshot_hashes(root, config) != hashes
                or metadata_expected(root, manifest_hash, config) != expected):
            fail("Source, engine, or coverage inputs changed during build; prior index preserved, retry build")
        no_symlinks(db)
        if db.exists():
            existing = readonly(db)
            existing.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if publication_guard is not None:
            publication_guard()
        os.replace(temporary, db)
        return envelope(command="build", fresh=True, database=str(db), root=root, **counts)
    finally:
        if connection is not None:
            connection.close()
        if temporary.exists():
            temporary.unlink()


def count_index(connection):
    counts = {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
              for name in ("documents", "sections")}
    if has_table(connection, "document_confidence"):
        levels = dict(connection.execute("SELECT level,count(*) FROM document_confidence GROUP BY level"))
        reasons = dict(connection.execute(
            "SELECT json_extract(r.value,'$.code') AS code,count(*) FROM document_confidence c, "
            "json_each(c.label,'$.reasons') r GROUP BY code ORDER BY code"))
        counts["confidence"] = {"contract": confidence.CONTRACT,
                                **{level: levels.get(level, 0) for level in confidence.LEVELS},
                                "low_reasons": reasons}
    return counts


def has_table(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def freshness(connection, root, manifest, db, config, manifest_hash, expected=None):
    """Reasons the index is stale; ``expected`` is a ``metadata_expected`` the caller just computed."""
    no_symlinks(db)
    actual = dict(connection.execute("SELECT key,value FROM metadata"))
    review_snapshot = (load_event_reader().read_snapshot(root, full=True)
                       if any(item["format"] == "docling" for item in config.get("renditions", []))
                       else None)
    if expected is None:
        expected = metadata_expected(root, manifest_hash, config, review_snapshot=review_snapshot)
    reasons = [f"metadata changed: {key}" for key in expected if actual.get(key) != expected[key]]
    if not has_table(connection, "rendition_documents"):
        return [*reasons, "rendition schema missing; run build"]
    if not has_table(connection, "document_confidence"):
        return [*reasons, "confidence schema missing; run build"]
    stored = dict(connection.execute(
        "SELECT path,hash FROM documents WHERE path NOT IN (SELECT path FROM rendition_documents)"))
    current = {}
    for document in config["documents"]:
        path = document["path"]
        try:
            current[path] = digest(source_path(root, path).read_bytes())
        except (IndexError_, OSError) as error:
            reasons.append(f"source unavailable: {path}: {error}")
    if set(stored) != {document["path"] for document in config["documents"]}:
        reasons.append("manifest document set changed")
    reasons.extend(f"source changed: {path}" for path in current if current[path] != stored.get(path))
    if load_manifest(manifest, root)[1] != manifest_hash:
        reasons.append("manifest changed during read")
    return reasons


def envelope(*, root=None, **values):
    settings = policy.for_root(root)[0]
    return {"audience": settings["audience"], "notice": NOTICE, "handling": settings["handling"], **values}


def query_terms(query):
    terms = re.findall(r"[^\W_]+", query, re.UNICODE)
    if not terms:
        fail("Search requires at least one word or number, not just punctuation")
    if len(query) > 4096 or len(terms) > 100:
        fail("Search is limited to 4096 characters and 100 literal terms")
    return terms


def fts_term(term):
    return '"' + term.replace('"', '""') + '"'


def literal_query(query):
    return " AND ".join(fts_term(term) for term in query_terms(query))


def term_presence(connection, terms, path):
    """Count sections matching each term alone, over the existing index; adds no schema."""
    restriction = " AND s.path=?" if path is not None else ""
    counts = {}
    for term in dict.fromkeys(terms):
        parameters = [fts_term(term)] + ([path] if path is not None else [])
        counts[term] = connection.execute(
            "SELECT count(*) FROM search_fts JOIN sections s ON s.id=search_fts.rowid "
            "WHERE search_fts MATCH ?" + restriction, parameters,
        ).fetchone()[0]
    return counts


def classify_absence(connection, query, path):
    """Explain a zero-result search without quoting the corpus or claiming a fact is absent."""
    counts = term_presence(connection, query_terms(query), path)
    absent = [term for term, count in counts.items() if not count]
    classification = ("absent-corpus" if len(absent) == len(counts)
                      else "absent-partial" if absent else "present-uncombined")
    if len(absent) == len(counts):
        matching_any = 0
    else:
        restriction = " AND s.path=?" if path is not None else ""
        parameters = [" OR ".join(fts_term(term) for term in counts)] + ([path] if path is not None else [])
        matching_any = connection.execute(
            "SELECT count(DISTINCT s.id) FROM search_fts JOIN sections s ON s.id=search_fts.rowid "
            "WHERE search_fts MATCH ?" + restriction, parameters,
        ).fetchone()[0]
    return {
        "classification": classification, "terms": len(counts), "absent_terms": len(absent),
        "sections_matching_any": matching_any,
        "scope": "selection" if path is None else "path",
        "caveat": ABSENCE[classification],
    }


LOW_CONFIDENCE = ("include", "last", "exclude")


def search(connection, query, limit, path, *, root=None, low_confidence="include"):
    """Literal search; ``low_confidence`` keeps low-confidence hits in rank order (``include``),
    ranks them after every normal hit (``last``) or leaves them out and counts them (``exclude``)."""
    if not 1 <= limit <= 50:
        fail("--limit must be between 1 and 50")
    if low_confidence not in LOW_CONFIDENCE:
        fail("--low-confidence must be one of " + ", ".join(LOW_CONFIDENCE))
    parameters = [literal_query(query)]
    restriction = ""
    if path is not None:
        relative_name(path)
        if connection.execute("SELECT 1 FROM documents WHERE path=?", (path,)).fetchone() is None:
            fail(f"Search path is not in the selection: {path}")
        restriction = " AND s.path=?"
        parameters.append(path)
    matching = ("FROM search_fts JOIN sections s ON s.id=search_fts.rowid "
                "JOIN documents d ON d.path=s.path JOIN document_confidence c ON c.path=s.path "
                "WHERE search_fts MATCH ?" + restriction)
    order = " ORDER BY " + ("c.level='low'," if low_confidence == "last" else "")
    rows = connection.execute(
        "SELECT s.*,d.role,c.label AS confidence,snippet(search_fts,1,'','',' … ',36) AS excerpt "
        + matching + (" AND c.level='normal'" if low_confidence == "exclude" else "")
        + order + "bm25(search_fts),s.path,s.line_start LIMIT ?", [*parameters, limit],
    ).fetchall()
    hits = [{
        "locator": row["locator"], "path": row["path"], "title": row["title"],
        "role": row["role"], "line_start": row["line_start"], "line_end": row["line_end"],
        "excerpt": row["excerpt"][:600], "caveat": DISCOVERY,
        "confidence": confidence.codes(json.loads(row["confidence"])),
    } for row in rows]
    for hit in hits:
        location = connection.execute("SELECT payload FROM rendition_locations WHERE locator=?",
                                      (hit["locator"],)).fetchone()
        if location:
            hit["source_location"] = json.loads(location[0])
            payload = connection.execute("SELECT payload FROM rendition_documents WHERE path=?",
                                         (hit["path"],)).fetchone()[0]
            if json.loads(payload)["format"] == "docling":
                hit["line_start"] = None
                hit["line_end"] = None
                hit["line_basis"] = "retained-extraction-byte-range-and-json-pointer"
            else:
                hit["line_basis"] = "extraction-json; source_location identifies original line or XML element"
    result = envelope(root=root, command="search", query=query, discovery_only=True, caveat=DISCOVERY,
                      hits=hits, low_confidence=low_confidence,
                      confidence=confidence.summary(json.loads(row["confidence"]) for row in rows))
    if low_confidence == "exclude":
        result["low_confidence_excluded"] = connection.execute(
            "SELECT count(*) " + matching + " AND c.level='low'", parameters).fetchone()[0]
    if not hits:
        result["absence"] = classify_absence(connection, query, path)
    return result


def reference_definitions(text):
    definitions = {}
    lines = text.splitlines(keepends=True)
    for number, line in enumerate(visible_lines(lines), 1):
        match = REFERENCE.match(line)
        if match:
            key = " ".join(match[1].lower().split())
            definitions.setdefault(key, {
                "target": match[2] or match[3], "text": lines[number - 1], "line": number,
            })
    return definitions


def main(argv=None):
    """Lazy delegation to the command adapter, which the MCP server runs as this module."""
    from tools.context_index.cli import main as run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
