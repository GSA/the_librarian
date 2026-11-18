"""Hand-written minimal PDF, PNG and JPEG bytes for extraction tests (no third-party writers)."""
import hashlib
import struct
import zlib

PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
FILE_ID = bytes(range(16))


def document(objects, trailer=b""):
    """Serialize numbered ``objects`` (bytes bodies, object 1 is the catalog) with a correct xref."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, trailer, start)
    return bytes(out)


def stream(data, extra=b""):
    return b"<< /Length %d %s>>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def _escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def text_pdf(pages):
    """One page per item; each item is a list of lines (or one string) drawn as real text."""
    pages = [[page] if isinstance(page, str) else page for page in pages]
    count = len(pages)
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(count))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, count),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, lines in enumerate(pages):
        body = b"BT /F1 11 Tf 14 TL 50 760 Td " + b" ".join(b"(%s) Tj T*" % _escape(line) for line in lines) + b" ET"
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i))
        objects.append(stream(body))
    return document(objects)


def image_pdf(width=64, height=64):
    """A page holding only a grey raster image: no text layer at all."""
    pixels = bytes([0x80]) * (width * height)
    return document([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>",
        stream(pixels, b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                       b"/ColorSpace /DeviceGray /BitsPerComponent 8 " % (width, height)),
        stream(b"q 512 0 0 512 50 140 cm /Im1 Do Q"),
    ])


def _rc4(key, data):
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) % 256
        box[i], box[j] = box[j], box[i]
    out, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(byte ^ box[(box[i] + box[j]) % 256])
    return bytes(out)


def encrypted_pdf(user=b"", owner=b"owner-secret"):
    """Standard security handler, revision 2 (40-bit RC4); an empty ``user`` password still opens it."""
    owner_key = hashlib.md5((owner + PAD)[:32]).digest()[:5]
    o_value = _rc4(owner_key, (user + PAD)[:32])
    permissions = -4
    key = hashlib.md5((user + PAD)[:32] + o_value + struct.pack("<i", permissions) + FILE_ID).digest()[:5]
    u_value = _rc4(key, PAD)
    encrypt = b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>" % (
        o_value.hex().encode(), u_value.hex().encode(), permissions)
    return document([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        encrypt,
    ], trailer=b"/Encrypt 4 0 R /ID [<%s> <%s>] " % (FILE_ID.hex().encode(), FILE_ID.hex().encode()))


def png(width=8, height=8, value=0xFF):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + bytes([value]) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def png_header(width, height):
    """Only the signature and IHDR: enough for a header-declared pixel count (a decompression bomb)."""
    data = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + data
            + struct.pack(">I", zlib.crc32(b"IHDR" + data)))


def jpeg_header(width, height):
    """SOI, an APP0 segment and a baseline SOF0 declaring ``width`` x ``height``."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"
