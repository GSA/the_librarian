"""Strict MIME/MHTML (Confluence-style export) to plain-text reader; stdlib only.

A message is recognised by its content (MIME headers), never by file suffix. Only
``text/html`` leaf parts that are not attachments are read; the transfer encoding and
charset are decoded strictly. Images, attachments, nested messages, links and scripts
are ignored and nothing is fetched or executed. Any malformed or unsupported input
raises ``MhtmlError`` with a stable code so the caller can route it to ``needs-reader``
instead of indexing raw encoded text.
"""

import base64
import binascii
import codecs
from email import message_from_bytes
from email.parser import BytesHeaderParser
from html.parser import HTMLParser
import quopri
import re

METHOD = "mhtml-html-text"
# Bump when conversion output can change so ingested entries are extracted again.
METHOD_VERSION = 3
MAX_HEADER = 64 * 1024
MAX_PARTS = 5000
MAX_DEPTH = 16
MAX_LINES = 100000
ENCODINGS = {"7bit", "8bit", "binary", "quoted-printable", "base64"}
FIRST_HEADER = re.compile(rb"[!-9;-~]+:")
BLANK = re.compile(rb"\r?\n\r?\n")
SKIPPED = {"script", "style", "head", "noscript", "template", "svg"}
BLOCKS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table",
          "tr", "pre", "blockquote", "section", "article", "header", "footer", "hr",
          "dl", "dt", "dd", "fieldset", "form", "caption", "figure", "figcaption"}


class MhtmlError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise MhtmlError(code)


def recognised(raw):
    """True when the bytes begin with an RFC 5322 header block that declares MIME-Version."""
    head = raw[:MAX_HEADER]
    if FIRST_HEADER.match(head) is None:
        return False
    end = BLANK.search(head)
    headers = BytesHeaderParser().parsebytes(head[:end.start()] if end else head)
    return headers["MIME-Version"] is not None


def _decode(part):
    """Return the strictly decoded text of one leaf part."""
    encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    require(encoding in ENCODINGS, "mime-encoding")
    # The parsed body holds non-ASCII bytes as surrogate escapes, so encoding it back recovers
    # the exact wire bytes. as_bytes() normalises CR and CRLF, and get_payload() decodes the
    # surrogates with the declared charset, so neither can feed a strict decoder.
    body = part._payload
    require(isinstance(body, str), "mime-malformed")
    try:
        data = body.encode("ascii", "surrogateescape")
    except UnicodeError as error:
        raise MhtmlError("mime-malformed") from error
    if encoding != "8bit" and encoding != "binary":
        require(data.isascii(), "mime-encoding")
    if encoding == "base64":
        try:
            data = base64.b64decode(re.sub(rb"\s+", b"", data), validate=True)
        except (binascii.Error, ValueError) as error:
            raise MhtmlError("mime-encoding") from error
    elif encoding == "quoted-printable":
        require(re.search(rb"=(?![0-9A-Fa-f]{2}|[ \t]*\r?\n)", data) is None, "mime-encoding")
        data = quopri.decodestring(data)
    charset = (part.get_content_charset() or "us-ascii").strip()
    try:
        info = codecs.lookup(charset)
        require(info._is_text_encoding, "mime-charset")
        return data.decode(info.name, "strict")
    except (LookupError, UnicodeError, ValueError) as error:
        raise MhtmlError("mime-charset") from error


def _html_parts(message):
    found, leaves = [], 0
    stack = [(message, 1)]
    while stack:
        part, depth = stack.pop()
        require(depth <= MAX_DEPTH and part.defects == [], "mime-malformed")
        if part.get_content_maintype() == "message":
            continue  # forwarded or embedded messages are not the exported page
        if str(part.get("Content-Disposition", "")).split(";")[0].strip().lower() == "attachment":
            continue  # an attachment, and everything inside it, is not the exported page
        if part.is_multipart():
            stack.extend((child, depth + 1) for child in reversed(part.get_payload()))
            continue
        require(part.get_content_maintype() != "multipart", "mime-malformed")
        leaves += 1
        require(leaves <= MAX_PARTS, "mime-malformed")
        if part.get_content_type() == "text/html":
            found.append((leaves, part))
    return found


class Converter(HTMLParser):
    """HTML to lines: block structure and table rows are kept, markup and links are not."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.current = [], []
        self.skip = []
        self.cells = 0
        self.preformatted = 0
        self.row = 0
        self.open = []  # per open table: whether a cell is currently open at that level

    def close_cell(self):
        """Close the open cell of the innermost table (HTML allows omitting </td> and </th>)."""
        if self.open and self.open[-1]:
            self.open[-1] = False
            self.cells = max(0, self.cells - 1)

    def flush(self):
        line = re.sub(r"\s+", " ", "".join(self.current)).strip()
        self.current = []
        if line:
            self.lines.append(line)

    def break_line(self):
        if self.cells:
            self.current.append(" ")
        else:
            self.flush()

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.skip = []
        if tag in SKIPPED:
            self.skip.append(tag)
            return
        if self.skip:
            return
        if tag == "table":
            self.break_line()
            self.open.append(False)
        elif tag in {"td", "th"}:
            if not self.open:
                self.open.append(False)
            self.close_cell()
            if self.cells == 0 and self.row:
                self.current.append(" | ")
            elif self.cells:
                self.current.append(" ")
            self.cells += 1
            self.open[-1] = True
            self.row += self.cells == 1
        elif tag == "tr" and (self.close_cell() or not self.cells):
            self.flush()
            self.row = 0
        elif tag in BLOCKS:
            self.break_line()
            if tag == "li":
                self.current.append("- ")
            elif tag == "pre":
                self.preformatted += 1

    def handle_startendtag(self, tag, attrs):
        if tag in SKIPPED or tag in {"td", "th"}:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in {"td", "th"}:
            self.close_cell()
        elif tag == "tr" and (self.close_cell() or not self.cells):
            self.flush()
            self.row = 0
        elif tag == "table":
            self.close_cell()
            if self.open:
                self.open.pop()
            self.break_line()
        elif tag in BLOCKS:
            self.break_line()
            if tag == "pre":
                self.preformatted = max(0, self.preformatted - 1)

    def handle_data(self, data):
        if self.skip:
            return
        if self.preformatted and not self.cells:
            pieces = data.split("\n")
            for number, piece in enumerate(pieces):
                self.current.append(piece)
                if number < len(pieces) - 1:
                    self.flush()
        else:
            self.current.append(data)

    def result(self):
        self.close()
        self.flush()
        return self.lines


def payload(raw):
    """Extract the text/html parts of a MIME message as numbered plain-text lines."""
    require(recognised(raw), "mime-unrecognised")
    message = message_from_bytes(raw)
    parts = _html_parts(message)
    require(parts, "mime-no-html")
    records, used = [], []
    for index, part in parts:
        text = _decode(part)
        require("\x00" not in text, "mime-charset")
        converter = Converter()
        try:
            converter.feed(text)
            lines = converter.result()
        except (AssertionError, ValueError, RecursionError) as error:
            raise MhtmlError("mime-html") from error
        used.append({"index": index, "content_type": "text/html",
                     "charset": (part.get_content_charset() or "us-ascii").lower(),
                     "transfer_encoding": str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()})
        for line in lines:
            records.append({"line": len(records) + 1, "text": line + "\n", "part": index})
            require(len(records) <= MAX_LINES, "mime-too-large")
    require(records, "mime-empty")
    return {
        "format": "mhtml",
        "scope": "plain text converted from the decoded text/html parts; attachments, images, "
                 "links and scripts are not read",
        "method": {"name": METHOD, "version": METHOD_VERSION},
        "parts": used,
        "lines": records,
        "warnings": ["MIME HTML converted to plain text by a stdlib reader; layout, images and "
                     "attachments are not preserved, and conversion is not review or qualification."],
    }


def verify(value, raw):
    """Confirm a stored payload is exactly what this reader produces for these source bytes."""
    require(value == payload(raw), "mime-mismatch")
