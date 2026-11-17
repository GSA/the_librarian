"""What a qualified standing review publishes: its selection entry, catalog child and manifest write,
and the authorization check publication makes on the whole manifest."""
import json
import os
from pathlib import Path
import stat
import tempfile

from tools.context_index import context_index as index
from tools.context_index import renditions
from tools.durable import fsync_directory
from tools.review_events import event_reader
from tools.review_events import event_store as events
from . import selection_manifest
from .contracts import canonical
from .standing import Refusal, permits, read_private
from .standing_events import standing_claims


def authorize_publication(root, manifest_name, standing, pinned):
    """Refuse a manifest whose standing-authorized selections this policy did not authorize.

    Selections backed only by independent reviews are outside standing authority
    and are left to the native qualification rules.
    """
    try:
        path = index.no_symlinks(Path(root) / manifest_name)
        index.load_manifest(path, root)
        raw = json.loads(path.read_bytes().decode("utf-8"),
                         object_pairs_hook=index.strict_object)
        # Publication re-validates the whole history it authorizes, not just what changed.
        snapshot = event_reader.read_snapshot(root, full=True)
    except (OSError, UnicodeError, ValueError, index.IndexError_,
            events.contract.ReviewError) as error:
        raise Refusal("blocked", "standing-selection-unauthorized") from error
    expected = (pinned["policy_id"], str(pinned["version"]), pinned["sha256"])
    for item in raw.get("renditions", []):
        for event_id in item.get("review_event_ids") or []:
            claims = standing_claims(snapshot.event(event_id))
            if not claims:
                continue
            if (claims != [expected] or not permits(
                    standing["scope"], item["receipt_id"], _audience(root, item["path"]) or "")):
                raise Refusal("blocked", "standing-selection-unauthorized")


def _audience(root, name):
    try:
        raw = read_private(root, name, 64 * 1024 * 1024)
        return json.loads(raw.decode("utf-8"))["source"]["audience"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, Refusal):
        return None


def selection_entry(result, args):
    source = result["source"]
    return {"path": args["rendition"], "sha256": args["rendition_sha256"],
            "receipt_id": source["receipt_id"], "format": result["format"],
            "context": list(result["context"]), "review_event_ids": [args["event_id"]]}


def selected_child(result, args):
    child = json.loads(canonical(result["child"]))
    child["data"]["coverage"] = "indexed"
    child["data"]["reason"] = (
        f"Selected under standing policy {result['policy']['policy_id']} "
        f"v{result['policy']['version']} after exact review {args['event_id']}.")
    return child


def _replace_validated(root, path, data):
    """Atomically replace ``path`` with ``data`` once the new bytes validate as a manifest."""
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=".selection-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        index.load_manifest(Path(temporary), root)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    fsync_directory(path.parent)


def write_manifest(root, name, entry, replaces=()):
    """Atomically add one selection entry to the manifest; idempotent for retries.

    An existing entry for the same path is replaced only when every review it
    cites is one this review supersedes (``replaces``). A new path is appended in
    constant time (``selection_manifest``); a replacement, an empty list or a file
    the append cannot extend in place takes the full read, validate and rewrite.
    The caller holds the ledger writer lock."""
    path = index.no_symlinks(Path(root) / name)
    selection_manifest.recover(root, name)
    known = selection_manifest.view(root, name)
    if not known.has(entry["path"]):
        if known.count >= renditions.MAX_SELECTIONS:
            raise Refusal("blocked", "selection-capacity")
        digest = selection_manifest.append(root, name, entry, known)
        if digest is not None:
            return digest
    selection_manifest.forget(path)
    index.load_manifest(path, root)
    raw = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=index.strict_object)
    selected = raw.setdefault("renditions", [])
    current = [item for item in selected if item["path"] == entry["path"]]
    if current == [entry]:
        return index.digest(path.read_bytes())
    if current and all(item.get("review_event_ids") and set(item["review_event_ids"]) <= set(replaces)
                       for item in current):
        selected[:] = [item for item in selected if item["path"] != entry["path"]]
        current = []
    if current:
        raise Refusal("blocked", "selection-conflict")
    if len(selected) >= renditions.MAX_SELECTIONS:
        # Not a conflict: the manifest is full, and room may appear when another cohort frees some.
        raise Refusal("blocked", "selection-capacity")
    selected.append(entry)
    data = (json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    _replace_validated(root, path, data)
    return index.digest(data)
