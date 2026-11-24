"""Office/RTF conversion to derived Markdown renditions, disguised-text admission and hostile inputs."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import unittest.mock
import warnings
import zipfile

from tools.catalog import catalog_store as catalog
from tools.extraction import convert, convert_worker as worker, office_runtime
from tools.intake import adapters, cohort, cohort_ingest, upload
from tools.intake.contracts import IntakeError

from tests.test_intake_cohort import QualifyCase

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
WR = W + ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
PNS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
SNS = ('xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
VNS = ('xmlns="http://schemas.microsoft.com/office/visio/2012/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def package(parts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in {"[Content_Types].xml": "<Types/>", **parts}.items():
            archive.writestr(zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0)), value, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def rels(*entries):
    return "<Relationships %s>%s</Relationships>" % (RELS, "".join(
        '<Relationship Id="%s" Type="http://x/%s" Target="%s"/>' % entry for entry in entries))


def run(text):
    return "<w:r><w:t xml:space=\"preserve\">%s</w:t></w:r>" % text


def docx(body=None):
    body = body or (
        '<w:p><w:pPr><w:pStyle w:val="H1"/></w:pPr>' + run("Plan overview") + "</w:p>"
        "<w:p>" + run("Fund codes are ") + run("four characters")
        + "<w:del><w:r><w:delText>removed words</w:delText></w:r></w:del>"
        '<w:r><w:drawing><wp:inline xmlns:wp="urn:x"/></w:drawing></w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>' + run("first step") + "</w:p>"
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="2"/></w:numPr></w:pPr>' + run("nested") + "</w:p>"
        "<w:tbl><w:tr><w:tc><w:p>" + run("Code") + "</w:p></w:tc><w:tc><w:p>" + run("Meaning") + "</w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p>" + run("A|1") + "</w:p></w:tc><w:tc><w:p>" + run("Alpha") + "</w:p></w:tc></w:tr></w:tbl>"
        '<w:p><w:r><w:pict><v:shape xmlns:v="urn:v"><v:textbox><w:txbxContent><w:p>' + run("boxed note")
        + "</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>")
    return package({
        "word/document.xml": "<w:document %s><w:body>%s</w:body></w:document>" % (WR, body),
        "word/styles.xml": '<w:styles %s><w:style w:styleId="H1"><w:name w:val="heading 1"/></w:style></w:styles>' % W,
        "word/numbering.xml": (
            "<w:numbering %s><w:abstractNum w:abstractNumId=\"0\"><w:lvl w:ilvl=\"0\"><w:numFmt w:val=\"decimal\"/>"
            "</w:lvl></w:abstractNum><w:abstractNum w:abstractNumId=\"1\"><w:lvl w:ilvl=\"1\"><w:numFmt "
            "w:val=\"bullet\"/></w:lvl></w:abstractNum><w:num w:numId=\"1\"><w:abstractNumId w:val=\"0\"/></w:num>"
            "<w:num w:numId=\"2\"><w:abstractNumId w:val=\"1\"/></w:num></w:numbering>") % W,
        "word/_rels/document.xml.rels": rels(("rId1", "footnotes", "footnotes.xml")),
        "word/footnotes.xml": ("<w:footnotes %s><w:footnote w:type=\"separator\" w:id=\"0\"><w:p>" + run("----")
                               + "</w:p></w:footnote><w:footnote w:id=\"1\"><w:p>" + run("See annex")
                               + "</w:p></w:footnote></w:footnotes>") % W,
    })


def pptx():
    def slide(title, body, extra=""):
        return ("<p:sld %s><p:cSld><p:spTree>"
                "<p:sp><p:nvSpPr><p:cNvPr id=\"1\" name=\"t\"/><p:cNvSpPr/><p:nvPr><p:ph type=\"title\"/></p:nvPr>"
                "</p:nvSpPr><p:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p></p:txBody></p:sp>"
                "<p:sp><p:nvSpPr><p:cNvPr id=\"2\" name=\"b\"/><p:cNvSpPr/><p:nvPr><p:ph idx=\"1\"/></p:nvPr>"
                "</p:nvSpPr><p:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p></p:txBody></p:sp>%s"
                "</p:spTree></p:cSld></p:sld>") % (PNS, title, body, extra)
    table = ("<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>K</a:t></a:r>"
             "</a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>V</a:t></a:r></a:p></a:txBody></a:tc></a:tr>"
             "<a:tr><a:tc><a:txBody><a:p><a:r><a:t>k1</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p>"
             "<a:r><a:t>v1</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic>"
             "</p:graphicFrame><p:pic/>")
    notes = ("<p:notes %s><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id=\"1\" name=\"n\"/><p:cNvSpPr/><p:nvPr>"
             "<p:ph type=\"sldNum\"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>7</a:t></a:r></a:p></p:txBody>"
             "</p:sp><p:sp><p:nvSpPr><p:cNvPr id=\"2\" name=\"m\"/><p:cNvSpPr/><p:nvPr><p:ph type=\"body\"/>"
             "</p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Speaker reminder</a:t></a:r></a:p></p:txBody></p:sp>"
             "</p:spTree></p:cSld></p:notes>") % PNS
    return package({
        "ppt/presentation.xml": ("<p:presentation %s><p:sldIdLst><p:sldId id=\"256\" r:id=\"rB\"/>"
                                 "<p:sldId id=\"257\" r:id=\"rA\"/></p:sldIdLst></p:presentation>") % PNS,
        "ppt/_rels/presentation.xml.rels": rels(("rA", "slide", "slides/slide1.xml"),
                                                ("rB", "slide", "slides/slide2.xml")),
        "ppt/slides/slide1.xml": slide("Second title", "Later body"),
        "ppt/slides/slide2.xml": slide("First title", "Opening body", table),
        "ppt/slides/_rels/slide2.xml.rels": rels(("rN", "notesSlide", "../notesSlides/notesSlide1.xml")),
        "ppt/notesSlides/notesSlide1.xml": notes,
    })


def xlsx(rows=None, extra_sheet=""):
    rows = rows or ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="inlineStr">'
                    '<is><t>When</t></is></c></row>'
                    '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>4.0</v></c><c r="C2" s="1"><v>45292</v></c></row>'
                    '<row r="4"><c r="A4" t="str"><v>x|y</v></c><c r="B4" t="b"><v>1</v></c></row>'
                    '<row r="5"><c r="A5" t="str"><v>times</v></c><c r="B5" s="2"><v>0.5</v></c>'
                    '<c r="C5" s="3"><v>1.5</v></c></row>')
    return package({
        "xl/workbook.xml": ('<workbook %s><sheets><sheet name="Codes" sheetId="1" r:id="rId1"/>%s</sheets>'
                            "</workbook>") % (SNS, extra_sheet),
        "xl/_rels/workbook.xml.rels": rels(("rId1", "worksheet", "worksheets/sheet1.xml"),
                                           ("rId2", "sharedStrings", "sharedStrings.xml"),
                                           ("rId3", "styles", "styles.xml")),
        "xl/sharedStrings.xml": ('<sst %s><si><t>Code</t></si><si><r><t>Amo</t></r><r><t>unt</t></r>'
                                 "<rPh><t>ignored</t></rPh></si><si><t>Rent</t></si></sst>") % SNS,
        "xl/styles.xml": ('<styleSheet %s><numFmts><numFmt numFmtId="164" formatCode="[h]:mm"/></numFmts>'
                          '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="20"/><xf numFmtId="164"/>'
                          "</cellXfs></styleSheet>") % SNS,
        "xl/worksheets/sheet1.xml": "<worksheet %s><sheetData>%s</sheetData></worksheet>" % (SNS, rows),
    })


def vsdx():
    return package({
        "visio/document.xml": "<VisioDocument/>",
        "visio/pages/pages.xml": ('<Pages %s><Page ID="0" NameU="Flow"><Rel r:id="rId1"/></Page></Pages>') % VNS,
        "visio/pages/_rels/pages.xml.rels": rels(("rId1", "page", "page1.xml")),
        "visio/pages/page1.xml": ('<PageContents %s><Shapes><Shape ID="1"><Text>Submit <cp IX="0"/>request</Text>'
                                  '</Shape><Shape ID="2"><Shapes><Shape ID="3"><Text>Approve</Text></Shape></Shapes>'
                                  "</Shape></Shapes></PageContents>") % VNS,
    })


RTF = (rb"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0 Arial;}}{\*\generator Hidden;}{\info{\title Secret}}"
       rb"Budget caf\'e9 \b plan\b0\par Euro \u8364?\tab done\par{\pict\bin3 }}}}{\*\bkmkstart x}Cell A\cell B\row}}")


def convert_bytes(fmt, raw):
    markdown, fidelity = worker.convert(fmt, raw)
    return markdown, fidelity


class WorkerFormatTests(unittest.TestCase):
    def test_docx_keeps_headings_lists_tables_notes_and_text_boxes(self):
        markdown, fidelity = convert_bytes("docx", docx())
        self.assertIn("## Plan overview\n", markdown)
        self.assertIn("Fund codes are four characters\n", markdown)
        self.assertNotIn("removed words", markdown)
        self.assertIn("1. first step\n", markdown)
        self.assertIn("  - nested\n", markdown)
        self.assertIn("| Code | Meaning |\n|---|---|\n| A\\|1 | Alpha |\n", markdown)
        self.assertIn("> boxed note\n", markdown)
        self.assertIn("## Footnotes", markdown)
        self.assertIn("See annex", markdown)
        self.assertNotIn("----", markdown)
        self.assertEqual(fidelity["dropped"], {"images": 1})

    def test_pptx_follows_presentation_order_with_tables_and_notes(self):
        markdown, fidelity = convert_bytes("pptx", pptx())
        self.assertLess(markdown.index("## Slide 1"), markdown.index("### First title"))
        self.assertLess(markdown.index("### First title"), markdown.index("## Slide 2"))
        self.assertLess(markdown.index("## Slide 2"), markdown.index("### Second title"))
        self.assertIn("- Opening body\n", markdown)
        self.assertIn("| K | V |\n|---|---|\n| k1 | v1 |\n", markdown)
        self.assertIn("#### Notes\n\n- Speaker reminder\n", markdown)
        self.assertNotIn("7\n", markdown)
        self.assertEqual(fidelity["dropped"], {"images": 1})

    def test_xlsx_sheets_become_row_numbered_markdown_tables(self):
        markdown, fidelity = convert_bytes("xlsx", xlsx())
        self.assertIn("## Sheet: Codes\n", markdown)
        self.assertIn("| Row | Code | Amount | When |\n|---|---|---|---|\n", markdown)
        self.assertIn("| 2 | Rent | 4 | 2024-01-01 |\n", markdown)
        self.assertIn("| 4 | x\\|y | TRUE |  |\n", markdown)
        self.assertIn("| 5 | times | 12:00:00 | 36:00:00 |\n", markdown)
        self.assertNotIn("ignored", markdown)
        self.assertFalse(fidelity["truncated"])

    def test_huge_sheet_is_chunked_and_truncation_is_recorded(self):
        rows = "".join('<row r="%d"><c r="A%d"><v>%d</v></c><c r="BZ%d"><v>1</v></c></row>' % (i, i, i, i)
                       for i in range(1, 131))
        with unittest.mock.patch.object(worker, "MAX_SHEET_ROWS", 120):
            markdown, fidelity = convert_bytes("xlsx", xlsx(rows))
        self.assertEqual(markdown.count("| Row | 1 |"), 3)  # 119 data rows in chunks of 50
        self.assertIn("| 120 | 120 |", markdown)
        self.assertNotIn("| 121 |", markdown)
        self.assertTrue(fidelity["truncated"])
        self.assertEqual(fidelity["dropped"], {"sheet_rows": 10})
        self.assertIn("sheet columns cut at 64", fidelity["notes"])

    def test_vsdx_pages_list_shape_text(self):
        markdown, fidelity = convert_bytes("vsdx", vsdx())
        self.assertIn("## Page: Flow\n\n- Submit request\n- Approve\n", markdown)
        self.assertIn("diagram geometry and connections omitted", fidelity["notes"])

    def test_rtf_strips_control_words_and_skips_hidden_destinations(self):
        markdown, fidelity = convert_bytes("rtf", RTF)
        self.assertEqual(markdown, "Budget café plan\nEuro €\tdone\nCell A | B\n")
        self.assertEqual(fidelity["dropped"], {"images": 1})

    def test_rtf_literal_bytes_follow_the_code_page_and_surrogate_pairs_combine(self):
        self.assertEqual(convert_bytes("rtf", b"{\\rtf1\\ansi\\ansicpg1252 Price \x80 5}")[0], "Price \u20ac 5\n")
        self.assertEqual(convert_bytes("rtf", b"{\\rtf1\\ansi\\ansicpg1251 \xcf\xf0\xe8\xe2\xe5\xf2}")[0],
                         "\u041f\u0440\u0438\u0432\u0435\u0442\n")
        self.assertEqual(convert_bytes("rtf", rb"{\rtf1\uc1 A\u-10179?{\b}\u-8704?B}")[0], "A\U0001f600B\n")
        markdown, fidelity = convert_bytes("rtf", rb"{\rtf1\uc1 Lone \u-10179? end}")
        self.assertEqual(markdown, "Lone \ufffd end\n")
        self.assertEqual(fidelity["notes"], ["unpaired RTF surrogate replaced"])

    def test_empty_document_is_refused_with_a_code(self):
        with self.assertRaises(worker.Refused) as caught:
            convert_bytes("docx", package({"word/document.xml": "<w:document %s><w:body><w:p/></w:body>"
                                                                "</w:document>" % W}))
        self.assertEqual(caught.exception.code, "conversion-empty")


class HostileInputTests(unittest.TestCase):
    def refused(self, fmt, raw):
        with self.assertRaises(worker.Refused) as caught:
            convert_bytes(fmt, raw)
        return caught.exception.code

    def test_doctype_and_entities_are_refused(self):
        for document in (
                '<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><w:document %s><w:body><w:p>'
                '<w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>' % W,
                '<!DOCTYPE d [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]><w:document %s/>' % W):
            self.assertEqual(self.refused("docx", package({"word/document.xml": document})), "xml-dtd")

    def test_zip_bomb_ratio_and_size_are_refused(self):
        bomb = package({"word/document.xml": "<w:document %s/>" % W, "word/media/pad.bin": b"\0" * (4 << 20)})
        self.assertEqual(self.refused("docx", bomb), "zip-bomb")

    def test_unsafe_duplicate_and_encrypted_members_are_refused(self):
        self.assertEqual(self.refused("docx", package({"../evil.xml": "x", "word/document.xml": "<a/>"})),
                         "unsafe-member")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("word/document.xml", "<a/>")
            archive.writestr("word/document.xml", "<b/>")
        self.assertEqual(self.refused("docx", buffer.getvalue()), "unsafe-member")
        raw = bytearray(package({"word/document.xml": "<a/>"}))
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = raw.find(signature)
            while at >= 0:
                raw[at + offset] |= 0x1
                at = raw.find(signature, at + 1)
        self.assertEqual(self.refused("docx", bytes(raw)), "encrypted-document")

    def test_relationship_targets_cannot_escape_the_package(self):
        raw = package({"ppt/presentation.xml": ("<p:presentation %s><p:sldIdLst><p:sldId id=\"256\" r:id=\"r1\"/>"
                                                "</p:sldIdLst></p:presentation>") % PNS,
                       "ppt/_rels/presentation.xml.rels": rels(("r1", "slide", "../../outside.xml")),
                       "outside.xml": "<p:sld %s><a:t>Outside</a:t></p:sld>" % PNS})
        markdown, fidelity = convert_bytes("pptx", raw)
        self.assertNotIn("Outside", markdown)
        self.assertEqual(fidelity["notes"], ["missing slide part"])

    def test_deep_rtf_and_malformed_xml_are_refused(self):
        self.assertEqual(self.refused("rtf", b"{\\rtf1" + b"{" * 2000 + b"x"), "rtf-invalid")
        self.assertEqual(self.refused("docx", package({"word/document.xml": "<w:document"})), "xml-invalid")

    def test_compound_or_non_zip_bytes_are_refused(self):
        self.assertEqual(self.refused("docx", convert.OLE + b"\0" * 512), "encrypted-document")
        self.assertEqual(self.refused("xlsx", b"not a zip"), "container-invalid")


class SniffAndAdmissionTests(unittest.TestCase):
    def test_content_decides_the_format_within_plausible_suffixes(self):
        self.assertEqual(convert.sniff(docx(), ".docx"), ("docx", None))
        self.assertEqual(convert.sniff(docx(), ".doc"), ("docx", None))
        self.assertEqual(convert.sniff(docx(), ".xlsx"), (None, "suffix-mismatch"))
        self.assertEqual(convert.sniff(xlsx(), ".xlsx"), ("xlsx", None))
        self.assertEqual(convert.sniff(pptx(), ".ppt"), ("pptx", None))
        self.assertEqual(convert.sniff(vsdx(), ".vsdx"), ("vsdx", None))
        self.assertEqual(convert.sniff(RTF, ".rtf"), ("rtf", None))
        self.assertEqual(convert.sniff(package({"a.txt": "x"}), ".zip"), (None, "unsupported-container"))
        self.assertEqual(convert.sniff(b"PK\x03\x04", ".docx"), (None, "container-invalid"))
        self.assertEqual(convert.sniff(b"PK", ".xlsx"), (None, "container-invalid"))
        self.assertEqual(convert.sniff(b"PKG notes", ".txt"), (None, None))
        self.assertEqual(convert.sniff(convert.OLE + convert.ENCRYPTED, ".docx"), (None, "encrypted-document"))
        self.assertEqual(convert.sniff(convert.OLE, ".mpp"), (None, "unsupported-container"))
        self.assertEqual(convert.sniff(b"plain", ".sql"), (None, None))

    def test_legacy_binary_needs_a_converter_runtime_when_soffice_is_absent(self):
        with unittest.mock.patch.object(office_runtime, "locate", return_value=None):
            self.assertEqual(convert.admission(convert.OLE, ".doc"), (None, "needs-converter-runtime"))
        with unittest.mock.patch.object(office_runtime, "locate", return_value=Path("/opt/soffice")), \
                unittest.mock.patch.object(office_runtime, "sandbox_mode", return_value="bwrap"):
            self.assertEqual(convert.admission(convert.OLE, ".doc"), ("office-legacy", None))
        with unittest.mock.patch.object(office_runtime, "locate", return_value=Path("/opt/soffice")), \
                unittest.mock.patch.object(office_runtime, "sandbox_mode", return_value=None):
            self.assertEqual(convert.admission(convert.OLE, ".doc"), (None, "needs-converter-sandbox"))

    def test_text_refusal_codes(self):
        self.assertIsNone(convert.text_refusal(b"SELECT 1;\r\n\tx\n"))
        self.assertEqual(convert.text_refusal(b"\xff\xfeA\x00"), "unsupported-encoding")
        self.assertEqual(convert.text_refusal(b"caf\xe9"), "not-utf8-text")
        self.assertEqual(convert.text_refusal(b"ab\x00cd"), "binary-content")
        self.assertEqual(convert.text_refusal(b"a\x01\x02b" + b"c" * 50), "control-characters")


class IsolatedWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def write(self, name, raw):
        path = self.home / name
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_worker_converts_in_a_child_process_and_checks_the_source_hash(self):
        path, sha = self.write("a.docx", docx())
        markdown, fidelity = convert._worker("docx", path, sha)
        self.assertIn("Fund codes are four characters", markdown)
        with self.assertRaises(convert.ConversionError) as caught:
            convert._worker("docx", path, "0" * 64)
        self.assertEqual(caught.exception.code, "source-changed")

    def test_wall_clock_timeout_and_output_cap_kill_the_child(self):
        slow, _ = self.write("slow.py", b"import time\ntime.sleep(30)\n")
        noisy, _ = self.write("noisy.py", b"import sys\nsys.stdout.write('x' * 4000000)\n")
        for script, patch, code in ((slow, {"WALL_SECONDS": 1}, "conversion-timeout"),
                                    (noisy, {"MAX_OUTPUT": 1000}, "conversion-limit")):
            with unittest.mock.patch.multiple(convert, WORKER=script, **patch):
                with self.assertRaises(convert.ConversionError) as caught:
                    convert._worker("docx", script, "0" * 64)
            self.assertEqual(caught.exception.code, code)

    def test_converter_identity_changes_with_converter_code(self):
        before = convert.code_sha256()
        changed, _ = self.write("convert_worker.py", convert.WORKER.read_bytes() + b"\n# changed\n")
        with unittest.mock.patch.object(convert, "WORKER", changed):
            self.assertNotEqual(convert.code_sha256(), before)

    def test_converter_identity_changes_with_the_shared_runner_and_the_sandbox(self):
        before = convert.code_sha256()
        for module in (convert.bounded, convert.office_runtime):
            changed, _ = self.write("changed.py", Path(module.__file__).read_bytes() + b"\n# changed\n")
            with self.subTest(module=module.__name__), unittest.mock.patch.object(module, "__file__", str(changed)):
                self.assertNotEqual(convert.code_sha256(), before)

    def test_failure_code_reaches_the_stage_failure(self):
        failure = adapters.native_failure(convert.ConversionError("zip-bomb", "x"))
        self.assertEqual((failure.category, failure.code), ("blocked", "zip-bomb"))


def fake_soffice(directory, produced):
    """A stand-in ``soffice`` that 'converts' by copying ``produced`` to the requested output."""
    (directory / "produced.bin").write_bytes(produced)
    script = directory / "soffice"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "LibreOffice 9.9 fake"; exit 0; fi\n'
        'out=""; target=""\n'
        'while [ $# -gt 0 ]; do case "$1" in --outdir) out="$2"; shift;; --convert-to) target="$2"; shift;; esac;'
        ' last="$1"; shift; done\n'
        'cp "%s" "$out/source.$target"\n' % (directory / "produced.bin"))
    script.chmod(stat.S_IRWXU)
    return script


class CohortConversionTests(QualifyCase):
    def setUp(self):
        super().setUp()
        convert.forget_parsed()  # a remembered conversion of an earlier test's identical bytes would hide a mocked refusal
        self.addCleanup(convert.forget_parsed)
        self.writable()
        extra = {"conv/plan.docx": docx(), "conv/deck.pptx": pptx(), "conv/book.xlsx": xlsx(),
                 "conv/flow.vsdx": vsdx(), "conv/memo.rtf": RTF, "conv/query.sql": b"SELECT code FROM fund;\n",
                 "conv/README": b"Release notes for the build\n", "conv/report.198": b"ROW 1 total\n",
                 "conv/blob.bin": b"\x00\x01\x02\x03", "conv/old.doc": convert.OLE + b"\0" * 64,
                 "conv/locked.docx": convert.OLE + convert.ENCRYPTED}
        for name, raw in extra.items():
            path = self.inbox / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        for path in [self.inbox, *self.inbox.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() else 0o444)

    def test_classify_routes_documents_to_converters_and_disguised_text_to_the_text_reader(self):
        with unittest.mock.patch.object(office_runtime, "locate", return_value=None):
            entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
        got = {k: (v["disposition"], v["format"], v.get("code")) for k, v in entries.items() if k.startswith("conv/")}
        self.assertEqual(got, {
            "conv/plan.docx": ("admit", "docx", None), "conv/deck.pptx": ("admit", "pptx", None),
            "conv/book.xlsx": ("admit", "xlsx", None), "conv/flow.vsdx": ("admit", "vsdx", None),
            "conv/memo.rtf": ("admit", "rtf", None), "conv/query.sql": ("admit", "text", None),
            "conv/README": ("admit", "text", None), "conv/report.198": ("admit", "text", None),
            "conv/blob.bin": ("needs-reader", None, "binary-content"),
            "conv/old.doc": ("needs-reader", None, "needs-converter-runtime"),
            "conv/locked.docx": ("needs-reader", None, "encrypted-document"),
        })
        self.assertEqual(cohort_ingest.submission("conv/query.sql", "inbox", "text")["name"], "query.sql.txt")
        self.assertEqual(cohort_ingest.submission("conv/README", "inbox", "text")["name"], "README.txt")
        self.assertEqual(cohort_ingest.submission("conv/plan.docx", "inbox", "docx")["name"], "plan.docx")
        # Text-suffixed names and identities are unchanged.
        self.assertEqual(cohort_ingest.submission("m/plain.txt", "inbox", "text"),
                         cohort_ingest.submission("m/plain.txt", "inbox"))

    def test_converted_documents_are_derived_renditions_reviewed_selected_and_retrievable(self):
        from tools.context_index import context_index as index, packets
        transport, calls = self._transport()
        with unittest.mock.patch.object(office_runtime, "locate", return_value=None):
            report = self._qualify(transport)
        entries = {e["relative"]: e for e in report["entries"]}
        for name in ("conv/plan.docx", "conv/deck.pptx", "conv/book.xlsx", "conv/flow.vsdx", "conv/memo.rtf",
                     "conv/query.sql", "conv/README", "conv/report.198"):
            self.assertEqual(entries[name]["qualification"], "qualified", name)
        plan = entries["conv/plan.docx"]
        value = json.loads((self.root / plan["rendition"]).read_text())
        conversion = value["payload"]["conversion"]
        self.assertEqual(value["payload"]["format"], "converted")
        self.assertEqual((conversion["source_format"], conversion["converter"], conversion["code_sha256"]),
                         ("docx", "librarian-convert", convert.code_sha256()))
        self.assertEqual(value["source"]["sha256"],
                         hashlib.sha256((self.inbox / "conv/plan.docx").read_bytes()).hexdigest())
        self.assertTrue(plan["original"].endswith("/plan.docx"))
        records = {r["id"]: r for r in catalog.read_records(self.root, limit=500)["records"]}
        child = records["A-extract-" + value["run_id"]]["data"]
        self.assertEqual((child["role"], child["parent_id"]), ("generated-rendition", value["source"]["artifact_id"]))
        query = entries["conv/query.sql"]
        self.assertTrue(query["original"].endswith("/query.sql.txt"))
        sql = json.loads((self.root / query["rendition"]).read_text())["payload"]
        self.assertEqual((sql["format"], sql["lines"][0]["text"]), ("text", "SELECT code FROM fund;\n"))
        config, _ = index.load_manifest(self.root / "config/selection.local.json", self.root)
        with closing(index.readonly(self.root / cohort.DEFAULT_INDEX)) as db:
            hit = index.search(db, "Speaker reminder", 10, None, root=self.root)["hits"][0]
            self.assertRegex(hit["locator"], r"#line-\d+$")
            got = packets.packet(db, config, hit["locator"], 60000, root=self.root)
            self.assertEqual(hit["confidence"], {"level": "low", "reasons": ["converted-format"]})
            self.assertEqual(index.search(db, "SELECT code", 10, None, root=self.root)["hits"][0]
                             ["confidence"]["level"], "normal")
            metadata = got["renditions"][hit["path"]]
            self.assertEqual(metadata["format"], "converted")
            self.assertEqual(metadata["conversion"]["source_format"], "pptx")
            self.assertTrue(index.search(db, "Approve", 10, None, root=self.root)["hits"])

    def test_converter_change_extracts_again_even_under_trust_stat(self):
        with unittest.mock.patch.object(office_runtime, "locate", return_value=None):
            first = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
            old = {e["relative"]: e for e in first["entries"]}
            again = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner", trust_stat=True)
            self.assertEqual(next(e for e in again["entries"] if e["relative"] == "conv/plan.docx")["rendition"],
                             old["conv/plan.docx"]["rendition"])
            real = convert.code_sha256
            with unittest.mock.patch.object(convert, "code_sha256", lambda: real()[::-1]):
                second = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner",
                                       trust_stat=True)
        new = {e["relative"]: e for e in second["entries"]}
        self.assertEqual(new["conv/plan.docx"]["outcome"], "extracted")
        self.assertNotEqual(new["conv/plan.docx"]["rendition"], old["conv/plan.docx"]["rendition"])
        self.assertEqual(new["conv/query.sql"]["rendition"], old["conv/query.sql"]["rendition"])

    def test_conversion_failure_stays_visible_with_its_code(self):
        def refuse(*_):
            raise convert.ConversionError("zip-bomb", "refused")
        with unittest.mock.patch.object(convert, "_worker", side_effect=refuse), \
                unittest.mock.patch.object(office_runtime, "locate", return_value=None):
            report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        plan = next(e for e in report["entries"] if e["relative"] == "conv/plan.docx")
        self.assertEqual((plan["outcome"], plan["code"]), ("failed", "zip-bomb"))
        query = next(e for e in report["entries"] if e["relative"] == "conv/query.sql")
        self.assertEqual(query["outcome"], "extracted")

    def test_legacy_binary_converts_through_soffice_when_present(self):
        tools = Path(self.temp.name) / "bin"
        tools.mkdir()
        fake_soffice(tools, docx())
        with unittest.mock.patch.dict(os.environ, {office_runtime.RUNTIME_ENV: str(tools / "soffice")}):
            office_runtime.reset()
            entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
            self.assertEqual(entries["conv/old.doc"]["format"], "office-legacy")
            report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        office_runtime.reset()
        old = next(e for e in report["entries"] if e["relative"] == "conv/old.doc")
        self.assertEqual(old["outcome"], "extracted")
        payload = json.loads((self.root / old["rendition"]).read_text())["payload"]
        self.assertEqual(payload["conversion"]["via"], "docx")
        self.assertEqual(payload["conversion"]["runtime"]["soffice"], "LibreOffice 9.9 fake")
        self.assertIn("Fund codes are four characters\n", [line["text"] for line in payload["lines"]])
        self.assertFalse(any(p.name.startswith("convert-") for p in (self.root / "data/extractions/.local").iterdir()))

    def legacy_ingest(self, body, seconds=None):
        """Ingest twice with a fake soffice running ``body`` (unconfined, so it can count its launches).

        Returns (first report, second report, number of soffice conversions started).
        """
        tools = Path(self.temp.name) / "bin"
        tools.mkdir(exist_ok=True)
        counter = Path(self.temp.name) / "launches"
        script = tools / "soffice"
        script.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "LibreOffice 9.9 fake"; exit 0; fi\n'
                          'echo x >> "%s"\n' % counter + body)
        script.chmod(stat.S_IRWXU)
        environment = {office_runtime.RUNTIME_ENV: str(script), office_runtime.SANDBOX_ENV: "none"}
        with unittest.mock.patch.dict(os.environ, environment), \
                unittest.mock.patch.object(convert, "SOFFICE_SECONDS", seconds or convert.SOFFICE_SECONDS):
            office_runtime.reset()
            report = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
            again = cohort.ingest(self.root, self.inbox, system="inbox", actor="cohort-runner")
        office_runtime.reset()
        return report, again, len(counter.read_text().split()) if counter.exists() else 0

    def check_legacy_failure(self, body, code, seconds=None):
        report, again, launches = self.legacy_ingest(body, seconds)
        old = next(e for e in report["entries"] if e["relative"] == "conv/old.doc")
        self.assertEqual((old["outcome"], old.get("code")), ("failed", code))
        other = next(e for e in report["entries"] if e["relative"] == "conv/plan.docx")
        self.assertEqual(other["outcome"], "extracted")  # one bad file never halts the cohort
        self.assertEqual(launches, 1, "the failed conversion must not be launched again on a rerun")
        self.assertEqual(next(e for e in again["entries"] if e["relative"] == "conv/old.doc")["outcome"], "failed")
        self.assertFalse(any(p.name.startswith("convert-") for p in (self.root / "data/extractions/.local").iterdir()))

    def test_a_hung_legacy_conversion_is_a_durable_disposition_and_not_repeated(self):
        self.check_legacy_failure("sleep 60\n", "conversion-timeout", seconds=2)

    def test_a_crashing_legacy_conversion_is_a_durable_disposition_and_not_repeated(self):
        self.check_legacy_failure("sh -c 'kill -SEGV $$'\nexit $?\n", "legacy-conversion-crashed")

    def test_a_failing_legacy_conversion_is_a_durable_disposition_and_not_repeated(self):
        self.check_legacy_failure("exit 1\n", "legacy-conversion-failed")

    def test_without_a_sandbox_legacy_files_wait_with_their_own_code(self):
        with unittest.mock.patch.object(office_runtime, "locate", return_value=Path("/opt/soffice")), \
                unittest.mock.patch.object(office_runtime, "sandbox_mode", return_value=None):
            entries = {e["relative"]: e for e in cohort.classify(self.root, self.inbox)}
        self.assertEqual((entries["conv/old.doc"]["disposition"], entries["conv/old.doc"]["code"]),
                         ("needs-reader", "needs-converter-sandbox"))

    def test_upload_accepts_converter_formats_only_for_matching_content(self):
        upload.check_type(docx(), "docx", "plan.doc")
        with self.assertRaises(IntakeError) as caught:
            upload.check_type(xlsx(), "docx", "plan.docx")
        self.assertEqual(caught.exception.code, "type_mismatch")


if __name__ == "__main__":
    unittest.main()
