"""P1 prototype: replace the substring scans in catalog_store._CurrentRecords by lookups in derived side
tables (paths, refs) keyed by value. Builds the side tables on a COPY of the seeded catalog once (not timed
as per-write cost), then patches referrers/claimants. Throwaway: new docs never reference old ones here, so the
side tables not tracking the benchmark's own inserts does not change any result."""
import json, sqlite3, time
from contextlib import closing

def build(db):
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("DROP TABLE IF EXISTS paths"); c.execute("DROP TABLE IF EXISTS refs")
        c.execute("CREATE TABLE paths(path TEXT, record_id TEXT)"); c.execute("CREATE TABLE refs(ref TEXT, record_id TEXT)")
        rows = c.execute("SELECT c.record_id, v.payload FROM current_records c JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal").fetchall()
        paths, refs = [], []
        for rid, payload in rows:
            r = json.loads(payload); d = r["data"]; k = r["kind"]
            if k == "artifact":
                if d["path"] is not None: paths.append((d["path"], rid))
                for x in (d["receipt_id"], d["parent_id"]):
                    if x: refs.append((x, rid))
            elif k == "receipt":
                for x in [*d["source_ids"], d["artifact_id"]]: refs.append((x, rid))
            elif k == "relationship":
                for x in (d["from_id"], d["to_id"]): refs.append((x, rid))
        c.executemany("INSERT INTO paths VALUES (?,?)", paths); c.executemany("INSERT INTO refs VALUES (?,?)", refs)
        c.execute("CREATE INDEX paths_p ON paths(path)"); c.execute("CREATE INDEX refs_r ON refs(ref)")

def patch():
    from tools.catalog import catalog_store as cs
    def by_ids(self, ids):
        ids = list(ids); out = []
        for i in range(0, len(ids), 200):
            ch = ids[i:i+200]
            q = "SELECT DISTINCT v.payload FROM refs r JOIN current_records c ON c.record_id=r.record_id JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal WHERE r.ref IN (%s)" % ",".join("?"*len(ch))
            out += [cs.decode(x[0]) for x in self.connection.execute(q, ch)]
        return out
    def referrers(self, ids):
        ids = set(ids); return [r for r in by_ids(self, ids) if ids.intersection(cs._references(r))]
    def claimants(self, paths):
        paths = list(set(paths)); out = []
        for i in range(0, len(paths), 200):
            ch = paths[i:i+200]
            q = "SELECT v.payload FROM paths p JOIN current_records c ON c.record_id=p.record_id JOIN versions v ON v.revision=c.revision AND v.ordinal=c.ordinal WHERE p.path IN (%s)" % ",".join("?"*len(ch))
            out += [cs.decode(x[0]) for x in self.connection.execute(q, ch)]
        return [r for r in out if r["kind"] == "artifact" and r["data"]["path"] in set(paths)]
    cs._CurrentRecords.referrers = referrers; cs._CurrentRecords.claimants = claimants

# --- P3 variant: expression indexes on versions (maintained by SQLite itself) -------------------------
EX = {"path": "$.data.path", "receipt_id": "$.data.receipt_id", "parent_id": "$.data.parent_id",
      "artifact_id": "$.data.artifact_id", "from_id": "$.data.from_id", "to_id": "$.data.to_id"}
def build_expr(db):
    with closing(sqlite3.connect(db)) as c, c:
        for k, p in EX.items(): c.execute(f"CREATE INDEX IF NOT EXISTS versions_{k} ON versions(json_extract(payload,'{p}'))")
def patch_expr():
    from tools.catalog import catalog_store as cs
    CUR = " AND EXISTS(SELECT 1 FROM current_records c WHERE c.revision=v.revision AND c.ordinal=v.ordinal)"
    def referrers(self, ids):
        ids = list(set(ids)); out = []
        for i in range(0, len(ids), 50):
            ch = ids[i:i+50]; ph = ",".join("?"*len(ch))
            q = "SELECT v.payload FROM versions v WHERE (" + " OR ".join(f"json_extract(v.payload,'{EX[k]}') IN ({ph})" for k in ("receipt_id","parent_id","artifact_id","from_id","to_id")) + ")" + CUR
            out += [cs.decode(r[0]) for r in self.connection.execute(q, ch*5)]
        s = set(ids); return [r for r in out if s.intersection(cs._references(r))]
    def claimants(self, paths):
        paths = list(set(paths)); out = []
        for i in range(0, len(paths), 200):
            ch = paths[i:i+200]
            out += [cs.decode(r[0]) for r in self.connection.execute("SELECT v.payload FROM versions v WHERE json_extract(v.payload,'$.data.path') IN (%s)" % ",".join("?"*len(ch)) + CUR, ch)]
        s = set(paths); return [r for r in out if r["kind"] == "artifact" and r["data"]["path"] in s]
    cs._CurrentRecords.referrers = referrers; cs._CurrentRecords.claimants = claimants
