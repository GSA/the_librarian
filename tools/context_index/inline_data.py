"""Inline ``data:`` payloads (embedded images, drawings): the reduced form reviewers and indexes see.

A note exported with its pictures inlined can be 95% base64 text. Reviewing that text tells a reader
nothing and makes the document too large for one review call. ``reduce_text`` replaces each large
payload by a short placeholder naming its media type, size and hash; the extraction itself, and so
every byte of the original, is never changed. ``summary`` is the deterministic record of what a
reviewer did not see (counts, byte totals, per-image hashes), published with the rendition so an
agent can tell that an image exists and which original file retains it.
"""
import hashlib
import re

# Payloads shorter than this stay in the text: a tiny icon is not what makes a document too large.
MIN_PAYLOAD = 1000
# Images listed individually in a summary; the rest are counted (``more``).
MAX_LISTED = 20
_URI = re.compile(r"data:([A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+)?((?:;[A-Za-z0-9=.+-]+)*),"
                  r"([A-Za-z0-9+/=%_-]{" + str(MIN_PAYLOAD) + ",})")


def _image(match):
    payload = match.group(3)
    return {"mime": match.group(1) or "unknown", "bytes": len(payload),
            "sha256": hashlib.sha256(payload.encode("ascii")).hexdigest()}


def _placeholder(match):
    image = _image(match)
    return (f"[inline data omitted: {image['mime']}, {image['bytes']} bytes, "
            f"sha256 {image['sha256'][:16]}]")


def reduce_text(text):
    """``(text with large data: payloads replaced by placeholders, the images replaced)``."""
    if "data:" not in text:
        return text, []
    images = [_image(m) for m in _URI.finditer(text)]
    return (_URI.sub(_placeholder, text) if images else text), images


def reduce_value(value, found):
    """``value`` (nested dicts, lists and strings) with every string reduced; images go to ``found``."""
    if isinstance(value, str):
        text, images = reduce_text(value)
        found.extend(images)
        return text
    if isinstance(value, list):
        return [reduce_value(item, found) for item in value]
    if isinstance(value, dict):
        return {key: reduce_value(item, found) for key, item in value.items()}
    return value


def summary(records):
    """The recorded reduction of a rendition's ``records``, or None when nothing was reduced.

    ``{"count", "bytes", "images": [{"record", "mime", "bytes", "sha256"}, ...], "more"?}``: the
    index of the record holding each image, its media type, its payload size as written and the
    SHA-256 of that payload. At most ``MAX_LISTED`` images are listed; ``more`` counts the rest.
    """
    listed, count, total = [], 0, 0
    for position, record in enumerate(records):
        found = []
        reduce_value(record, found)
        for image in found:
            count += 1
            total += image["bytes"]
            if len(listed) < MAX_LISTED:
                listed.append({"record": position, **image})
    if not count:
        return None
    result = {"count": count, "bytes": total, "images": listed}
    if count > len(listed):
        result["more"] = count - len(listed)
    return result
