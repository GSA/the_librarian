"""Pin-only catalog reads: the stored text of current records, looked up by key, no file bytes (Kata xv0n).

A reader that proves an index fresh needs only what the catalog pinned at ingest (hashes, paths and
sizes, in the records themselves), not a copy of the whole current-record table. ``snapshot`` yields one
validated read transaction; ``texts`` and ``sidecars`` fetch a few records by key through the primary
key and the lookup indexes. Record payloads come back as the exact canonical text the validated store
holds, so two readers of the same revision derive the same digest from them.

Holding the read transaction blocks a writer's commit until it ends, so callers fetch in bounded
chunks and leave the ``with`` block as soon as they have what they need.
"""

from contextlib import closing, contextmanager
import json
import stat

from tools.catalog import catalog_store as store
from tools.catalog.contracts import require
from tools.catalog.storage import connect, locations, lookup_shape, _lookup_expression

CHUNK = 2000
_CURRENT = ("current_records c JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal")
_ROLE = "json_extract(v.payload,'$.data.role')='preserved-metadata'"
_ARTIFACT = "json_extract(v.payload,'$.kind')='artifact'"


class Snapshot:
    """Keyed lookups in one validated read transaction of the catalog."""

    def __init__(self, connection, state):
        self.connection, self.state = connection, state
        self.indexed = lookup_shape(connection) == "indexed"
        self._all_sidecars = None

    def texts(self, record_ids):
        """``{record id: payload text}`` of the current records with these ids; absent ids are omitted."""
        record_ids = list(record_ids)
        found = {}
        for start in range(0, len(record_ids), CHUNK):
            for row in self.connection.execute(
                    f"SELECT c.record_id, v.payload FROM {_CURRENT} "
                    "WHERE c.record_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(record_ids[start:start + CHUNK]),)):
                found[row[0]] = row[1]
        return found

    def sidecars(self, parent_ids):
        """``{parent id: [payload text]}`` of current ``preserved-metadata`` artifacts under these parents."""
        parent_ids = list(parent_ids)
        found = {}
        if not self.indexed:
            # No lookup structures: one scan serves every parent, instead of one scan per chunk.
            if self._all_sidecars is None:
                self._all_sidecars = {}
                for row in self.connection.execute(
                        f"SELECT json_extract(v.payload,'$.data.parent_id'), v.payload FROM {_CURRENT} "
                        f"WHERE {_ARTIFACT} AND {_ROLE}"):
                    self._all_sidecars.setdefault(row[0], []).append(row[1])
            return {parent: self._all_sidecars[parent] for parent in parent_ids if parent in self._all_sidecars}
        for start in range(0, len(parent_ids), CHUNK):
            for row in self.connection.execute(
                    f"SELECT json_extract(v.payload,'$.data.parent_id'), v.payload FROM versions v WHERE "
                    f"{_lookup_expression('parent_id')} IN (SELECT value FROM json_each(?)) "
                    f"AND {_ARTIFACT} AND {_ROLE}" + store._CurrentRecords.CURRENT,
                    (json.dumps(parent_ids[start:start + CHUNK]),)):
                found.setdefault(row[0], []).append(row[1])
        return found

    def artifacts_at(self, path):
        """The decoded current artifact records that claim ``path``."""
        if self.indexed:
            rows = self.connection.execute(
                f"SELECT v.payload FROM versions v WHERE {_lookup_expression('path')}=? AND {_ARTIFACT}"
                + store._CurrentRecords.CURRENT, (path,))
        else:
            rows = self.connection.execute(
                f"SELECT v.payload FROM {_CURRENT} WHERE {_ARTIFACT} "
                "AND json_extract(v.payload,'$.data.path')=?", (path,))
        return [store.decode(row[0]) for row in rows]

    def retained_files(self):
        """``(id, path, sha256, bytes, role, receipt id)`` of every present artifact with a path and a hash."""
        return list(self.connection.execute(
            "SELECT c.record_id, json_extract(v.payload,'$.data.path'), json_extract(v.payload,'$.data.sha256'),"
            "json_extract(v.payload,'$.data.bytes'), json_extract(v.payload,'$.data.role'),"
            f"json_extract(v.payload,'$.data.receipt_id') FROM {_CURRENT} WHERE {_ARTIFACT} "
            "AND json_extract(v.payload,'$.data.presence')='present' "
            "AND json_extract(v.payload,'$.data.path') IS NOT NULL "
            "AND json_extract(v.payload,'$.data.sha256') IS NOT NULL ORDER BY c.record_id"))


@contextmanager
def snapshot(root):
    """One validated read transaction of the catalog at ``root`` as a ``Snapshot``."""
    home, db, _ = locations(root)
    require(home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700, "Catalog home missing or not private")
    with closing(connect(db)) as connection:
        connection.execute("BEGIN")
        yield Snapshot(connection, store.validated_state(connection, home))
