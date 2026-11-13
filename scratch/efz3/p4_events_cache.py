"""P4: memoize event_store.inspect() by (file identity, root) - same trust model as the 0017 catalog receipt, in memory only.
Wrap, run e6 (zero-latency qualify) at N docs, compare per-entry serial ms."""
import os, sys, runpy
sys.path.insert(0, os.getcwd())
from tools.review_events import event_store as es
from tools.file_identity import file_identity
_orig = es.inspect; _memo = {}
def inspect(connection, root=None):
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    ident = file_identity(path, connection)
    key = (str(root), tuple(ident)) if ident else None
    if key and key in _memo: return _memo[key]
    out = _orig(connection, root)
    if key and file_identity(path, connection) == ident: _memo.clear(); _memo[key] = out
    return out
es.inspect = inspect
sys.argv = ["e6", sys.argv[1]]
runpy.run_path(".local/plans/proto/e6_qualify_serial.py", run_name="__main__")
