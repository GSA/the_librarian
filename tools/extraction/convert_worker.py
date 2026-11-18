"""Isolated stdlib document-to-Markdown converter; run only by ``convert.py`` in a child process.

Usage: ``python -I convert_worker.py FORMAT PATH SHA256``. Prints one JSON object on stdout:
``{"ok": true, "markdown": ..., "fidelity": ...}`` or ``{"ok": false, "code": ..., "message": ...}``.
It imports nothing outside the standard library, never follows links or entities in the input,
writes no files and runs under the resource limits it sets before it reads the input.
"""
from datetime import datetime, timedelta
import hashlib
import io
import json
import posixpath
import re
import sys
import zipfile
from xml.parsers import expat
import xml.etree.ElementTree as ET

MAX_SOURCE = 64 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_MEMBER = 128 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_RATIO = 200
MAX_ELEMENTS = 4000000
MAX_DEPTH = 1000
MAX_LINES = 100000
MAX_MARKDOWN = 16 * 1024 * 1024
MAX_CELL = 1000
MAX_COLUMNS = 64
MAX_SHEET_ROWS = 10000
CHUNK_ROWS = 50

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
V = "http://schemas.microsoft.com/office/visio/2012/main"
ODF_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODF_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"


def q(namespace, name):
    return "{" + namespace + "}" + name


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Fidelity:
    """What a conversion left out or cut; recorded beside the converted text."""

    def __init__(self):
        self.dropped = {}
        self.notes = set()
        self.truncated = False

    def drop(self, kind, count=1):
        self.dropped[kind] = self.dropped.get(kind, 0) + count

    def truncate(self, note):
        self.truncated = True
        self.notes.add(note)

    def value(self):
        return {"dropped": dict(sorted(self.dropped.items())), "notes": sorted(self.notes),
                "truncated": self.truncated}


# --- hardened container and XML reading -------------------------------------------------------

def _safe_member(name):
    parts = name.split("/")
    return (bool(name) and "\\" not in name and "\0" not in name and not name.startswith("/")
            and not re.match(r"^[A-Za-z]:", name) and ".." not in parts)


class Package:
    """A zip whose members are checked before any is read and counted while they are read."""

    def __init__(self, raw):
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(raw))
            infos = self.archive.infolist()
        except (zipfile.BadZipFile, ValueError, OSError, EOFError) as error:
            raise Refused("container-invalid", "Not a readable zip container") from error
        if len(infos) > MAX_MEMBERS:
            raise Refused("container-limit", "Too many zip members")
        self.members = {}
        declared = 0
        for info in infos:
            name = info.filename
            if not _safe_member(name):
                raise Refused("unsafe-member", "Unsafe zip member name")
            if name in self.members:
                raise Refused("unsafe-member", "Duplicate zip member name")
            if info.flag_bits & 0x1:
                raise Refused("encrypted-document", "Encrypted zip member")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Refused("container-invalid", "Unsupported zip compression")
            if info.file_size > MAX_MEMBER or (
                    info.file_size > 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
                raise Refused("zip-bomb", "Zip member expansion exceeds limits")
            declared += info.file_size
            self.members[name] = info
        if declared > MAX_TOTAL:
            raise Refused("zip-bomb", "Zip expansion exceeds limits")
        self.read_total = 0

    def has(self, name):
        return name in self.members

    def read(self, name):
        info = self.members.get(name)
        if info is None:
            raise Refused("container-invalid", "Missing package part")
        chunks, size = [], 0
        try:
            with self.archive.open(info) as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    self.read_total += len(chunk)
                    if size > MAX_MEMBER or size > info.file_size or self.read_total > MAX_TOTAL:
                        raise Refused("zip-bomb", "Zip member expands beyond its declared size")
                    chunks.append(chunk)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as error:
            raise Refused("container-invalid", "Unreadable zip member") from error
        return b"".join(chunks)


def _parser(handlers):
    parser = expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_):
        raise Refused("xml-dtd", "XML document type declarations and entities are refused")
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.buffer_text = True
    for key, value in handlers.items():
        setattr(parser, key, value)
    return parser


def _name(raw):
    return "{" + raw if "}" in raw else raw


def stream_xml(raw, start, end, data):
    """Feed one XML part to callbacks ``start(tag, attrs)``, ``end(tag)`` and ``data(text)``."""
    depth = [0]
    count = [0]

    def on_start(tag, attrs):
        depth[0] += 1
        count[0] += 1
        if depth[0] > MAX_DEPTH or count[0] > MAX_ELEMENTS:
            raise Refused("xml-limit", "XML element depth or count exceeds limits")
        start(_name(tag), {_name(k): v for k, v in attrs.items()})

    def on_end(tag):
        depth[0] -= 1
        end(_name(tag))
    parser = _parser({"StartElementHandler": on_start, "EndElementHandler": on_end,
                      "CharacterDataHandler": data})
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as error:
        raise Refused("xml-invalid", "Malformed XML part") from error


def tree(raw):
    builder = ET.TreeBuilder()
    stream_xml(raw, builder.start, builder.end, builder.data)
    return builder.close()


def relationships(package, part):
    """``{id: (type, target part path)}`` for one part; external and escaping targets are omitted."""
    directory, base = posixpath.split(part)
    rels = posixpath.join(directory, "_rels", base + ".rels")
    if not package.has(rels):
        return {}
    result = {}
    for rel in tree(package.read(rels)).iter(q(PR, "Relationship")):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                  else posixpath.join(directory, target))
        if path.startswith("../") or path == ".." or not _safe_member(path):
            continue
        result[rel.get("Id")] = (rel.get("Type", "").rsplit("/", 1)[-1], path)
    return result


# --- Markdown helpers -------------------------------------------------------------------------

def clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\t\n" or (ord(c) >= 32 and not 0x7f <= ord(c) < 0xa0))


def cell(text, fidelity):
    text = " ".join(clean(text).split())
    if len(text) > MAX_CELL:
        fidelity.truncate("cell text cut at %d characters" % MAX_CELL)
        text = text[:MAX_CELL] + "…"
    return text.replace("\\", "\\\\").replace("|", "\\|")


def table(rows, fidelity, leading=None):
    """Markdown table lines; the first row is the header. ``leading`` prefixes a row-number column."""
    width = max((len(row) for row in rows), default=0)
    if width > MAX_COLUMNS:
        fidelity.truncate("table columns cut at %d" % MAX_COLUMNS)
        width = MAX_COLUMNS
    if width == 0:
        return []
    out = []
    for index, row in enumerate(rows):
        values = [cell(value, fidelity) for value in row[:width]] + [""] * (width - min(len(row), width))
        if leading is not None:
            values.insert(0, leading[index])
        out.append("| " + " | ".join(values) + " |")
        if index == 0:
            out.append("|" + "---|" * (width + (leading is not None)))
    return out


# --- DOCX -------------------------------------------------------------------------------------

def docx(package, fidelity):
    document = "word/document.xml"
    root = tree(package.read(document))
    rels = relationships(package, document)
    styles = {}
    if package.has("word/styles.xml"):
        for style in tree(package.read("word/styles.xml")).iter(q(W, "style")):
            name = style.find(q(W, "name"))
            styles[style.get(q(W, "styleId"))] = (name.get(q(W, "val")) if name is not None else "") or ""
    ordered = {}
    if package.has("word/numbering.xml"):
        numbering = tree(package.read("word/numbering.xml"))
        abstract = {}
        for item in numbering.iter(q(W, "abstractNum")):
            levels = {}
            for level in item.iter(q(W, "lvl")):
                fmt = level.find(q(W, "numFmt"))
                levels[level.get(q(W, "ilvl"))] = fmt is not None and fmt.get(q(W, "val")) not in (
                    "bullet", "none", None)
            abstract[item.get(q(W, "abstractNumId"))] = levels
        for item in numbering.iter(q(W, "num")):
            ref = item.find(q(W, "abstractNumId"))
            if ref is not None:
                ordered[item.get(q(W, "numId"))] = abstract.get(ref.get(q(W, "val")), {})
    lines = []
    body = root.find(q(W, "body"))
    if body is None:
        raise Refused("ooxml-invalid", "Word document has no body")
    _docx_blocks(body, lines, styles, ordered, fidelity)
    for part, kind, title, tag in (("word/footnotes.xml", "footnotes", "Footnotes", "footnote"),
                                   ("word/endnotes.xml", "endnotes", "Endnotes", "endnote"),
                                   ("word/comments.xml", "comments", "Comments", "comment")):
        target = next((path for _, (rtype, path) in rels.items() if rtype == kind), None)
        if target is None or not package.has(target):
            continue
        section = []
        for note in tree(package.read(target)).iter(q(W, tag)):
            if tag != "comment" and note.get(q(W, "type")) in ("separator", "continuationSeparator",
                                                              "continuationNotice"):
                continue
            body_lines = []
            _docx_blocks(note, body_lines, styles, ordered, fidelity)
            text = " ".join(line for line in body_lines if line)
            if text:
                author = note.get(q(W, "author"))
                label = note.get(q(W, "id"), "")
                section.append("- [%s %s]%s %s" % (tag, label, " (" + author + ")" if author else "", text))
        if section:
            lines += ["", "## " + title, ""] + section
    if any(rtype in ("header", "footer") for rtype, _ in rels.values()):
        fidelity.notes.add("headers and footers omitted")
    return lines


def _docx_blocks(parent, lines, styles, ordered, fidelity):
    for child in parent:
        if child.tag == q(W, "p"):
            _docx_paragraph(child, lines, styles, ordered, fidelity)
        elif child.tag == q(W, "tbl"):
            rows = []
            for row in child.findall(q(W, "tr")):
                cells = []
                for tc in row.findall(q(W, "tc")):
                    inner = []
                    _docx_blocks(tc, inner, {}, {}, fidelity)
                    cells.append(" <br> ".join(line.strip() for line in inner if line.strip()))
                if cells:
                    rows.append(cells)
            if rows:
                lines += [""] + table(rows, fidelity) + [""]
        elif child.tag in (q(W, "sdt"), q(W, "sdtContent"), q(W, "customXml"), q(W, "smartTag"),
                           q(W, "ins"), q(MC, "AlternateContent"), q(MC, "Choice")):
            _docx_blocks(child, lines, styles, ordered, fidelity)


def _docx_paragraph(paragraph, lines, styles, ordered, fidelity):
    parts, boxes = [], []

    def walk(element):
        for child in element:
            tag = child.tag
            if tag in (q(W, "delText"), q(W, "instrText"), q(MC, "Fallback"), q(W, "pPr"), q(W, "rPr"),
                       q(W, "del")):
                continue
            if tag == q(W, "t"):
                parts.append(child.text or "")
            elif tag == q(W, "tab"):
                parts.append("\t")
            elif tag in (q(W, "br"), q(W, "cr")):
                parts.append(" ")
            elif tag == q(W, "noBreakHyphen"):
                parts.append("-")
            elif tag == q(W, "txbxContent"):
                boxes.append(child)
                continue
            elif tag in (q(W, "drawing"), q(W, "pict"), q(W, "object")):
                if not any(True for _ in child.iter(q(W, "txbxContent"))):
                    fidelity.drop("images" if tag != "object" else "embedded_objects")
            walk(child)
    walk(paragraph)
    text = clean("".join(parts)).replace("\n", " ").strip()
    properties = paragraph.find(q(W, "pPr"))
    prefix = ""
    if properties is not None and text:
        style = properties.find(q(W, "pStyle"))
        name = styles.get(style.get(q(W, "val")), style.get(q(W, "val"))) if style is not None else ""
        heading = re.fullmatch(r"(?i)heading\s*([1-9])", name or "")
        numbering = properties.find(q(W, "numPr"))
        if heading:
            prefix = "#" * min(int(heading.group(1)) + 1, 6) + " "
        elif (name or "").lower() == "title":
            prefix = "# "
        elif numbering is not None:
            level = numbering.find(q(W, "ilvl"))
            number = numbering.find(q(W, "numId"))
            depth = level.get(q(W, "val"), "0") if level is not None else "0"
            numbered = ordered.get(number.get(q(W, "val")) if number is not None else None, {}).get(depth)
            indent = "  " * min(int(depth) if depth.isdigit() else 0, 8)
            prefix = indent + ("1. " if numbered else "- ")
    if text:
        if prefix.startswith("#") or (lines and lines[-1].lstrip().startswith(("- ", "1. ")) and not prefix):
            lines.append("")
        lines.append(prefix + text)
        if prefix.startswith("#"):
            lines.append("")
    elif not lines or lines[-1]:
        lines.append("")
    for box in boxes:
        inner = []
        _docx_blocks(box, inner, styles, ordered, fidelity)
        for line in inner:
            if line.strip():
                lines.append("> " + line.strip())


# --- PPTX -------------------------------------------------------------------------------------

def _drawing_paragraphs(body):
    result = []
    for paragraph in body.iter(q(A, "p")):
        parts = []
        for child in paragraph.iter():
            if child.tag == q(A, "t"):
                parts.append(child.text or "")
            elif child.tag == q(A, "br"):
                parts.append(" ")
        text = clean("".join(parts)).replace("\n", " ").strip()
        properties = paragraph.find(q(A, "pPr"))
        level = int(properties.get("lvl", "0")) if properties is not None and properties.get("lvl", "0").isdigit() else 0
        if text:
            result.append((min(level, 8), text))
    return result


def _placeholder(shape):
    for nv in shape:
        if nv.tag.startswith("{" + P + "}nv"):
            properties = nv.find(q(P, "nvPr"))
            ph = properties.find(q(P, "ph")) if properties is not None else None
            if ph is not None:
                return ph.get("type", "body")
    return None


def _slide_shapes(container, lines, fidelity, skip=()):
    for shape in container:
        tag = shape.tag
        if tag == q(P, "grpSp"):
            _slide_shapes(shape, lines, fidelity, skip)
        elif tag == q(P, "sp"):
            kind = _placeholder(shape)
            if kind in skip:
                continue
            body = shape.find(q(P, "txBody"))
            if body is None:
                continue
            for level, text in _drawing_paragraphs(body):
                if kind in ("title", "ctrTitle"):
                    lines += ["", "### " + text, ""]
                elif kind in ("body", "obj") or level:
                    lines.append("  " * level + "- " + text)
                else:
                    lines.append(text)
        elif tag == q(P, "graphicFrame"):
            found = False
            for grid in shape.iter(q(A, "tbl")):
                found = True
                rows = [[" ".join(text for _, text in _drawing_paragraphs(tc)) for tc in tr.findall(q(A, "tc"))]
                        for tr in grid.findall(q(A, "tr"))]
                if rows:
                    lines += [""] + table(rows, fidelity) + [""]
            if not found:
                fidelity.drop("charts_or_diagrams")
        elif tag == q(P, "pic"):
            fidelity.drop("images")
        elif tag == q(MC, "AlternateContent"):
            choice = shape.find(q(MC, "Choice"))
            if choice is not None:
                _slide_shapes(choice, lines, fidelity, skip)


def pptx(package, fidelity):
    presentation = "ppt/presentation.xml"
    root = tree(package.read(presentation))
    rels = relationships(package, presentation)
    lines = []
    listing = root.find(q(P, "sldIdLst"))
    for number, entry in enumerate(listing if listing is not None else (), 1):
        target = rels.get(entry.get(q(R, "id")))
        lines += ["", "## Slide %d" % number, ""]
        if target is None or not package.has(target[1]):
            fidelity.notes.add("missing slide part")
            continue
        slide = tree(package.read(target[1]))
        tree_root = slide.find(q(P, "cSld") + "/" + q(P, "spTree"))
        if tree_root is not None:
            _slide_shapes(tree_root, lines, fidelity)
        for rtype, path in relationships(package, target[1]).values():
            if rtype == "notesSlide" and package.has(path):
                notes = tree(package.read(path)).find(q(P, "cSld") + "/" + q(P, "spTree"))
                note_lines = []
                if notes is not None:
                    _slide_shapes(notes, note_lines, fidelity,
                                  skip=("sldNum", "sldImg", "hdr", "ftr", "dt"))
                note_lines = [line for line in note_lines if line.strip()]
                if note_lines:
                    lines += ["", "#### Notes", ""] + note_lines
    return lines


# --- XLSX -------------------------------------------------------------------------------------

# Built-in number formats that are calendar dates, date-times, times of day or elapsed durations.
DATE_FORMATS = {14: "date", 15: "date", 16: "date", 17: "date", 22: "datetime", 18: "time", 19: "time",
                20: "time", 21: "time", 45: "time", 47: "time", 46: "elapsed"}


def _date_kind(code):
    """``date``, ``datetime``, ``time``, ``elapsed`` or None for a custom number format code."""
    code = code or ""
    if re.search(r"\[(h+|m+|s+)\]", code, re.I):
        return "elapsed"
    stripped = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code).lower()
    if "general" in stripped or not re.search(r"[dmyhs]", stripped):
        return None
    day = re.search(r"[dy]", stripped)
    clock = re.search(r"[hs]", stripped)
    if day:
        return "datetime" if clock else "date"
    return "time" if clock else "date"


def _column(reference):
    letters = re.match(r"[A-Z]+", reference or "")
    if not letters:
        return None
    number = 0
    for letter in letters.group():
        number = number * 26 + ord(letter) - 64
    return number


def _column_name(number):
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _clock(seconds):
    hours, rest = divmod(round(seconds), 3600)
    return "%d:%02d:%02d" % (hours, rest // 60, rest % 60)


def _number(text, kind, date1904):
    """A cell number as text; ``kind`` (``_date_kind``) renders dates, times and durations."""
    try:
        value = float(text)
    except ValueError:
        return text
    if kind == "elapsed" and abs(value) < 2958466:
        return ("-" if value < 0 else "") + _clock(abs(value) * 86400)
    if kind == "time" and 0 <= value < 2958466:
        return _clock(round((value % 1) * 86400) % 86400)
    if kind in ("date", "datetime") and 0 <= value < 2958466:
        base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
        moment = base + timedelta(days=value)
        if kind == "date" and value == int(value):
            return moment.date().isoformat()
        return moment.replace(microsecond=0).isoformat(sep=" ")
    if value == int(value) and abs(value) < 1e15 and "e" not in text.lower():
        return str(int(value))
    return text


def xlsx(package, fidelity):
    workbook = "xl/workbook.xml"
    root = tree(package.read(workbook))
    rels = relationships(package, workbook)
    properties = root.find(q(S, "workbookPr"))
    date1904 = properties is not None and properties.get("date1904") in ("1", "true")
    shared = []
    strings = next((path for rtype, path in rels.values() if rtype == "sharedStrings"), None)
    if strings and package.has(strings):
        for item in tree(package.read(strings)).findall(q(S, "si")):
            # Direct and rich-run text only; phonetic (rPh) runs are reading aids, not cell text.
            texts = item.findall(q(S, "t")) + item.findall(q(S, "r") + "/" + q(S, "t"))
            shared.append("".join(t.text or "" for t in texts))
    date_styles = {}
    style_part = next((path for rtype, path in rels.values() if rtype == "styles"), None)
    if style_part and package.has(style_part):
        styles = tree(package.read(style_part))
        custom = {fmt.get("numFmtId"): fmt.get("formatCode") for fmt in styles.iter(q(S, "numFmt"))}
        cell_formats = styles.find(q(S, "cellXfs"))
        for index, xf in enumerate(cell_formats.findall(q(S, "xf")) if cell_formats is not None else ()):
            identifier = xf.get("numFmtId", "0")
            kind = (DATE_FORMATS.get(int(identifier)) if identifier.isdigit() else None) \
                or _date_kind(custom.get(identifier))
            if kind:
                date_styles[str(index)] = kind
    lines = []
    sheets = root.find(q(S, "sheets"))
    for sheet in sheets if sheets is not None else ():
        name = clean(sheet.get("name", "")).replace("\n", " ")
        hidden = " (hidden)" if sheet.get("state") in ("hidden", "veryHidden") else ""
        lines += ["", "## Sheet: " + name + hidden, ""]
        target = rels.get(sheet.get(q(R, "id")))
        if target is None or target[0] != "worksheet" or not package.has(target[1]):
            fidelity.drop("chart_or_missing_sheets")
            lines.append("(sheet content not converted)")
            continue
        rows = _sheet_rows(package.read(target[1]), shared, date_styles, date1904, fidelity)
        if not rows:
            lines.append("(empty sheet)")
            continue
        lines += _sheet_tables(rows, fidelity)
        for rtype, _ in relationships(package, target[1]).values():
            if rtype in ("drawing", "chart"):
                fidelity.drop("drawings_or_charts")
    return lines


def _sheet_rows(raw, shared, date_styles, date1904, fidelity):
    """``[(row number, {column: text})]`` of the non-empty rows, capped at ``MAX_SHEET_ROWS``."""
    rows = []
    state = {"row": None, "cells": None, "cell": None, "type": None, "style": None, "text": None,
             "collect": False, "next_row": 0, "next_col": 0, "skipped": 0}

    def start(tag, attrs):
        if tag == q(S, "row"):
            number = attrs.get("r")
            state["next_row"] = int(number) if number and number.isdigit() else state["next_row"] + 1
            state["row"], state["cells"], state["next_col"] = state["next_row"], {}, 0
        elif tag == q(S, "c") and state["cells"] is not None:
            column = _column(attrs.get("r"))
            state["next_col"] = column if column else state["next_col"] + 1
            state["cell"], state["type"], state["style"] = state["next_col"], attrs.get("t", "n"), attrs.get("s", "0")
            state["text"] = []
        elif tag in (q(S, "v"), q(S, "t")) and state["cell"] is not None:
            state["collect"] = True

    def data(text):
        if state["collect"]:
            state["text"].append(text)

    def end(tag):
        if tag in (q(S, "v"), q(S, "t")):
            state["collect"] = False
        elif tag == q(S, "c") and state["cell"] is not None:
            raw_text, kind = "".join(state["text"]), state["type"]
            if kind == "s":
                value = shared[int(raw_text)] if raw_text.isdigit() and int(raw_text) < len(shared) else ""
            elif kind == "b":
                value = "TRUE" if raw_text == "1" else "FALSE"
            elif kind in ("str", "inlineStr", "e", "d"):
                value = raw_text
            else:
                value = _number(raw_text, date_styles.get(state["style"]), date1904) if raw_text else ""
            if value.strip():
                if state["cell"] <= MAX_COLUMNS:
                    state["cells"][state["cell"]] = value
                else:
                    fidelity.truncate("sheet columns cut at %d" % MAX_COLUMNS)
            state["cell"] = None
        elif tag == q(S, "row") and state["cells"] is not None:
            if state["cells"]:
                if len(rows) < MAX_SHEET_ROWS:
                    rows.append((state["row"], state["cells"]))
                else:
                    state["skipped"] += 1
            state["cells"] = None
    stream_xml(raw, start, end, data)
    if state["skipped"]:
        fidelity.truncate("sheet rows cut at %d non-empty rows" % MAX_SHEET_ROWS)
        fidelity.drop("sheet_rows", state["skipped"])
    return rows


def _sheet_tables(rows, fidelity):
    """Row chunks as Markdown tables; each repeats the first non-empty row as its header."""
    width = max(max(cells) for _, cells in rows)
    header_row, header_cells = rows[0]
    header = [header_cells.get(c, "") or _column_name(c) for c in range(1, width + 1)]
    body = rows[1:]
    if not body:
        return table([header], fidelity, leading=["Row %d" % header_row])
    lines = []
    for start in range(0, len(body), CHUNK_ROWS):
        chunk = body[start:start + CHUNK_ROWS]
        grid = [header] + [[cells.get(c, "") for c in range(1, width + 1)] for _, cells in chunk]
        numbers = ["Row"] + [str(number) for number, _ in chunk]
        lines += table(grid, fidelity, leading=numbers) + [""]
    return lines


# --- VSDX and ODG -----------------------------------------------------------------------------

def vsdx(package, fidelity):
    pages_part = "visio/pages/pages.xml"
    lines = []
    if not package.has(pages_part):
        raise Refused("ooxml-invalid", "Visio package has no pages")
    rels = relationships(package, pages_part)
    for number, page in enumerate(tree(package.read(pages_part)).findall(q(V, "Page")), 1):
        name = clean(page.get("NameU") or page.get("Name") or "Page %d" % number).replace("\n", " ")
        background = " (background)" if page.get("Background") in ("1", "true") else ""
        lines += ["", "## Page: " + name + background, ""]
        rel = page.find(q(V, "Rel"))
        target = rels.get(rel.get(q(R, "id"))) if rel is not None else None
        if target is None or not package.has(target[1]):
            lines.append("(page content not converted)")
            continue
        content = tree(package.read(target[1]))
        found = False
        for shape in content.iter(q(V, "Shape")):
            text = shape.find(q(V, "Text"))
            if text is None:
                continue
            value = " ".join(clean("".join(text.itertext())).split())
            if value:
                found = True
                lines.append("- " + value)
        if not found:
            lines.append("(no shape text)")
        if any(rtype == "image" for rtype, _ in relationships(package, target[1]).values()):
            fidelity.drop("images")
    fidelity.notes.add("diagram geometry and connections omitted")
    return lines


def odg(package, fidelity):
    content = tree(package.read("content.xml"))
    lines = []
    for number, page in enumerate(content.iter(q(ODF_DRAW, "page")), 1):
        lines += ["", "## Page: " + (page.get(q(ODF_DRAW, "name")) or "Page %d" % number), ""]
        for paragraph in page.iter(q(ODF_TEXT, "p")):
            value = " ".join(clean("".join(paragraph.itertext())).split())
            if value:
                lines.append("- " + value)
    fidelity.notes.add("diagram geometry and connections omitted")
    return lines


# --- RTF --------------------------------------------------------------------------------------

RTF_SKIP = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata", "colorschememapping",
    "latentstyles", "datastore", "xmlnstbl", "listtable", "listoverridetable", "rsidtbl", "generator",
    "header", "headerl", "headerr", "headerf", "footer", "footerl", "footerr", "footerf", "fldinst",
    "filetbl", "revtbl", "pgdsctbl", "listtext", "pntext", "pntxta", "pntxtb", "objdata", "bkmkstart",
    "bkmkend", "xe", "tc", "txe", "mmathPr", "wgrffmtfilter", "defchp", "defpap", "nonshppict",
}
RTF_TOKEN = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+",
                       re.S)


def rtf(raw, fidelity):
    if not raw.startswith(b"{\\rtf"):
        raise Refused("rtf-invalid", "Not an RTF document")
    codepage = "cp1252"
    match = re.search(rb"\\ansicpg(\d{3,5})", raw[:4096])
    if match:
        candidate = "cp" + match.group(1).decode()
        try:
            "".encode(candidate)
            codepage = candidate
        except LookupError:
            fidelity.notes.add("unknown RTF code page; cp1252 used")
    out, pending = [], bytearray()
    stack, skip, uc, skip_chars = [], False, 1, 0
    group_start = False
    position = 0
    high = None  # a \u high surrogate waiting for its low half

    def settle():
        nonlocal high
        if high is not None:
            out.append("\ufffd")
            fidelity.notes.add("unpaired RTF surrogate replaced")
            high = None

    def flush(paired=False):
        # Literal and \' bytes decode together in the document code page (multibyte pages too).
        if pending:
            settle()
            out.append(pending.decode(codepage, errors="replace"))
            pending.clear()
        if not paired:
            settle()
    while position < len(raw):
        token = RTF_TOKEN.match(raw, position)
        if token is None:
            position += 1
            continue
        position = token.end()
        word, argument, hexbyte, symbol, brace, text = token.groups()
        if brace == b"{":
            flush(paired=True)
            if len(stack) >= MAX_DEPTH:
                raise Refused("rtf-invalid", "RTF group depth exceeds limits")
            stack.append((skip, uc))
            group_start = True
            continue
        if brace == b"}":
            flush(paired=True)
            if stack:
                skip, uc = stack.pop()
            group_start = False
            continue
        starting, group_start = group_start, False
        if symbol == b"*" and starting:
            skip = True
            continue
        if word is not None:
            name = word.decode()
            if name == "bin" and argument:
                position += max(int(argument), 0)
                continue
            if starting and name in RTF_SKIP:
                if name in ("pict", "object") and not skip:
                    fidelity.drop("images" if name == "pict" else "embedded_objects")
                skip = True
                continue
            if skip:
                continue
            if name == "uc" and argument:
                uc = max(int(argument), 0)
            elif name == "u" and argument:
                flush(paired=True)
                code = int(argument)
                code = code + 65536 if code < 0 else code
                if 0xDC00 <= code < 0xE000 and high is not None:
                    out.append(chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00)))
                    high = None
                else:
                    settle()
                    if 0xD800 <= code < 0xDC00:
                        high = code
                    elif 0xDC00 <= code < 0xE000:
                        out.append("\ufffd")
                        fidelity.notes.add("unpaired RTF surrogate replaced")
                    elif 0 < code < 0x110000:
                        out.append(chr(code))
                skip_chars = uc
            elif name in ("par", "line", "row", "sect", "page"):
                flush()
                out.append("\n")
            elif name == "tab":
                flush()
                out.append("\t")
            elif name == "cell":
                flush()
                out.append(" | ")
            elif name in ("emdash", "endash"):
                flush()
                out.append("—" if name == "emdash" else "–")
            elif name in ("lquote", "rquote"):
                flush()
                out.append("'")
            elif name in ("ldblquote", "rdblquote"):
                flush()
                out.append('"')
            elif name == "bullet":
                flush()
                out.append("•")
            continue
        if skip:
            continue
        if hexbyte is not None:
            if skip_chars:
                skip_chars -= 1
                continue
            if not pending:
                settle()
            pending.append(int(hexbyte, 16))
            continue
        if symbol is not None:
            flush()
            if skip_chars:
                skip_chars -= 1
                continue
            if symbol in (b"\\", b"{", b"}"):
                out.append(symbol.decode())
            elif symbol == b"~":
                out.append("\u00a0")
            elif symbol in (b"\n", b"\r"):
                out.append("\n")
            elif symbol == b"-":
                pass
            elif symbol == b"_":
                out.append("-")
            continue
        if text is not None:
            if skip_chars:
                dropped = min(skip_chars, len(text))
                text, skip_chars = text[dropped:], skip_chars - dropped
            if text:
                if not pending:
                    settle()
                pending.extend(text)
    flush()
    return [line.rstrip() for line in clean("".join(out)).split("\n")]


# --- entry point ------------------------------------------------------------------------------

CONVERTERS = {"docx": docx, "xlsx": xlsx, "pptx": pptx, "vsdx": vsdx, "odg": odg}


def convert(format_name, raw):
    fidelity = Fidelity()
    if format_name == "rtf":
        lines = rtf(raw, fidelity)
    else:
        if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise Refused("encrypted-document", "Compound file where a zip package is expected")
        lines = CONVERTERS[format_name](Package(raw), fidelity)
    result, blank, size = [], True, 0
    for line in lines:
        line = line.rstrip()
        if not line:
            if blank:
                continue
            blank = True
        else:
            blank = False
        size += len(line) + 1
        if len(result) >= MAX_LINES or size > MAX_MARKDOWN:
            fidelity.truncate("converted text cut at %d lines or %d characters" % (MAX_LINES, MAX_MARKDOWN))
            break
        result.append(line)
    while result and not result[-1]:
        result.pop()
    if not any(line.strip() for line in result):
        raise Refused("conversion-empty", "The document has no convertible text")
    return "\n".join(result) + "\n", fidelity.value()


def _limit():
    try:
        import resource
    except ImportError:
        return
    for name, value in (("RLIMIT_AS", 3 * 1024 * 1024 * 1024), ("RLIMIT_FSIZE", 0), ("RLIMIT_NOFILE", 32),
                        ("RLIMIT_CORE", 0)):
        limit = getattr(resource, name, None)
        if limit is not None:
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass


def main(argv):
    try:
        format_name, path, expected = argv
        _limit()
        with open(path, "rb") as stream:
            raw = stream.read(MAX_SOURCE + 1)
        if len(raw) > MAX_SOURCE:
            raise Refused("over-limit", "Source exceeds size limit")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise Refused("source-changed", "Source differs from the expected hash")
        if format_name not in CONVERTERS and format_name != "rtf":
            raise Refused("unsupported-format", "Unknown conversion format")
        markdown, fidelity = convert(format_name, raw)
        result = {"ok": True, "markdown": markdown, "fidelity": fidelity}
    except Refused as error:
        result = {"ok": False, "code": error.code, "message": str(error)}
    except MemoryError:
        result = {"ok": False, "code": "conversion-limit", "message": "Conversion exceeded its memory limit"}
    except (RecursionError, ValueError, KeyError, IndexError, TypeError, OSError) as error:
        result = {"ok": False, "code": "conversion-failed", "message": type(error).__name__}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
