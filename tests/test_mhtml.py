"""Confluence-style MIME/MHTML recognition and strict HTML-to-text decoding (synthetic fixtures)."""
import base64
import unittest

from tools.extraction import mhtml

B = "----=_Part_7_1"
HEAD = ("Date: Mon, 1 Jan 2024 00:00:00 +0000\r\nMessage-ID: <x@example.invalid>\r\n"
        "Subject: Exported From Confluence\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="%s"; type="text/html"\r\n\r\n' % B)
HTML = ('<html><head><title>Rules</title><style>.hidden{color:red}</style>'
        '<script>alert("SCRIPTBODY")</script></head><body><h1>Fund Code Maintenance</h1>'
        '<p>A fund code must be exactly four characters &amp; unique. See '
        '<a href="http://example.invalid/LINKTARGET">the policy</a>.</p>'
        '<table><tr><th>Field</th><th>Rule</th></tr>'
        '<tr><td>Code</td><td>Required \u2014 uppercase</td></tr></table>'
        '<ul><li>First</li><li>Second</li></ul><img src="cid:image1"/></body></html>')


def qp(text, width=60):
    """Quoted-printable with =3D escapes and soft line breaks, as exporters write it."""
    out, line = [], ""
    for byte in text.encode("utf-8"):
        char = chr(byte)
        piece = char if (33 <= byte <= 126 and char != "=") or char == " " else "=%02X" % byte
        if char in "\r\n":
            piece = char
        if len(line) + len(piece) > width and char not in "\r\n":
            out.append(line + "=")
            line = ""
        line += piece
        if char == "\n":
            out.append(line.rstrip("\n"))
            line = ""
    out.append(line)
    return "\r\n".join(out)


def html_part(html=HTML, encoding="quoted-printable", charset="UTF-8", body=None):
    body = qp(html) if body is None else body
    return ("--%s\r\nContent-Type: text/html; charset=%s\r\nContent-Transfer-Encoding: %s\r\n"
            "Content-Location: file:///exportedContent.html\r\n\r\n%s\r\n" % (B, charset, encoding, body))


def image_part():
    data = base64.b64encode(b"IMAGEBYTES-NOT-TEXT").decode()
    return ("--%s\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
            'Content-Disposition: attachment; filename="a.png"\r\n\r\n%s\r\n' % (B, data))


def message(*parts, close=True):
    return (HEAD + "".join(parts) + ("--%s--\r\n" % B if close else "")).encode()


class RecognitionTests(unittest.TestCase):
    def test_recognises_mime_by_content_not_suffix(self):
        self.assertTrue(mhtml.recognised(message(html_part())))
        self.assertFalse(mhtml.recognised(b"Plain notes about MIME-Version: 1.0\n"))
        # MIME-Version at the start of the file claims MIME; a broken body is refused, not read as text.
        self.assertTrue(mhtml.recognised(b"MIME-Version: 1.0\r\nbody only\r\n"))
        self.assertFalse(mhtml.recognised(b"Notes\r\nMIME-Version: 1.0\r\n"))
        self.assertFalse(mhtml.recognised(b"<html><body>hi</body></html>"))
        self.assertFalse(mhtml.recognised(b"\xff\xfe"))


class ConvertTests(unittest.TestCase):
    def lines(self, raw):
        return [line["text"] for line in mhtml.payload(raw)["lines"]]

    def test_decodes_soft_breaks_escapes_and_converts_html(self):
        raw = message(html_part(), image_part())
        lines = self.lines(raw)
        joined = "".join(lines)
        self.assertIn("Fund Code Maintenance\n", lines)
        self.assertIn("A fund code must be exactly four characters & unique. See the policy.\n", lines)
        self.assertIn("Field | Rule\n", lines)
        self.assertIn("Code | Required \u2014 uppercase\n", lines)
        self.assertIn("- First\n", lines)
        for noise in ("=3D", "=\r", "<", "SCRIPTBODY", "color:red", "LINKTARGET", "IMAGEBYTES", "cid:"):
            self.assertNotIn(noise, joined)

    def test_raw_fixture_really_contains_encoding_artifacts(self):
        raw = message(html_part()).decode()
        self.assertIn("=3D", raw)
        self.assertRegex(raw, r"=\r\n")

    def test_payload_records_method_version_and_parts(self):
        value = mhtml.payload(message(html_part(), image_part()))
        self.assertEqual(value["format"], "mhtml")
        self.assertEqual(value["method"], {"name": "mhtml-html-text", "version": mhtml.METHOD_VERSION})
        self.assertEqual(value["parts"], [{"index": 1, "content_type": "text/html",
                                           "charset": "utf-8", "transfer_encoding": "quoted-printable"}])
        self.assertEqual([r["line"] for r in value["lines"]], list(range(1, len(value["lines"]) + 1)))
        self.assertTrue(all(r["text"].endswith("\n") for r in value["lines"]))
        self.assertTrue(any("not review" in w for w in value["warnings"]))

    def test_nested_message_and_attachment_html_are_ignored(self):
        attached = ("--%s\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: 7bit\r\n"
                    'Content-Disposition: attachment; filename="x.html"\r\n\r\n<p>ATTACHED</p>\r\n' % B)
        forwarded = ("--%s\r\nContent-Type: message/rfc822\r\n\r\nContent-Type: text/html\r\n\r\n"
                     "<p>FORWARDED</p>\r\n" % B)
        joined = "".join(self.lines(message(html_part(), attached, forwarded)))
        self.assertNotIn("ATTACHED", joined)
        self.assertNotIn("FORWARDED", joined)

    def test_html_inside_an_attached_multipart_container_is_ignored(self):
        inner = "--%s\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>NESTEDATTACHED</p>\r\n" % "inner"
        container = ("--%s\r\nContent-Type: multipart/mixed; boundary=inner\r\n"
                     "Content-Disposition: attachment\r\n\r\n%s--inner--\r\n" % (B, inner))
        joined = "".join(self.lines(message(html_part(), container)))
        self.assertNotIn("NESTEDATTACHED", joined)
        self.assertIn("Fund Code Maintenance", joined)

    def test_multiple_html_parts_are_all_included_in_order(self):
        second = html_part("<p>Second part text</p>")
        lines = self.lines(message(html_part("<p>First part text</p>"), second))
        self.assertEqual(lines, ["First part text\n", "Second part text\n"])

    def refuses(self, raw, code):
        with self.assertRaises(mhtml.MhtmlError) as caught:
            mhtml.payload(raw)
        self.assertEqual(caught.exception.code, code)

    def test_malformed_mime_is_refused(self):
        self.refuses(message(html_part(), close=False), "mime-malformed")
        self.refuses(HEAD.replace('boundary="%s"; ' % B, "").encode() + b"--x\r\nbody\r\n", "mime-malformed")
        self.refuses((HEAD + "no boundary lines here\r\n").encode(), "mime-malformed")

    def test_missing_html_part_is_refused(self):
        plain = ("--%s\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 7bit\r\n\r\n"
                 "only text\r\n" % B)
        self.refuses(message(plain, image_part()), "mime-no-html")

    def test_unknown_transfer_encoding_and_charset_are_refused(self):
        self.refuses(message(html_part(encoding="x-uuencode", body="<p>x</p>")), "mime-encoding")
        self.refuses(message(html_part(charset="x-not-a-charset", body="<p>x</p>", encoding="7bit")),
                     "mime-charset")

    def test_invalid_bytes_for_declared_encoding_are_refused(self):
        self.refuses(message(html_part(body="<p>bad =ZZ escape</p>")), "mime-encoding")
        self.refuses(message(html_part(body="<p>dangling =", encoding="quoted-printable")), "mime-encoding")
        self.refuses(message(html_part(body="!!!not base64!!!", encoding="base64")), "mime-encoding")
        self.refuses(message(html_part(body="<p>caf\u00e9</p>", encoding="7bit", charset="us-ascii")),
                     "mime-encoding")
        bad = HEAD.encode() + (("--%s\r\nContent-Type: text/html; charset=utf-8\r\n"
                                "Content-Transfer-Encoding: 8bit\r\n\r\n" % B).encode()
                               + b"<p>\xff\xfe</p>\r\n--" + B.encode() + b"--\r\n")
        self.refuses(bad, "mime-charset")

    def test_html_without_text_is_refused(self):
        self.refuses(message(html_part("<html><body><img src='cid:x'/></body></html>")), "mime-empty")

    def test_base64_html_is_decoded(self):
        body = base64.b64encode("<p>Encoded \u2014 text</p>".encode()).decode()
        self.assertEqual(self.lines(message(html_part(encoding="base64", body=body))),
                         ["Encoded \u2014 text\n"])


if __name__ == "__main__":
    unittest.main()
