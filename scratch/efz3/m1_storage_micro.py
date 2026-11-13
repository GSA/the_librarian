"""M1: storage-primitive micro-benchmarks on this disk (ext4, virtual disk): fsync-per-file vs batched, sqlite journal/sync modes, blobs in sqlite vs files."""
import os, sys, time, sqlite3, hashlib, tempfile, statistics
from pathlib import Path
d = Path(tempfile.mkdtemp(dir=os.environ["TMPDIR"]))
N, SZ = 400, 70_000
data = [os.urandom(SZ) for _ in range(8)]
def rate(label, fn):
    t = time.perf_counter(); fn(); e = time.perf_counter()-t; print(f"{label:70s} {e/N*1e3:8.2f} ms/doc"); return e
# files
def files_fsync_each():
    p = d/"a"; p.mkdir()
    for i in range(N):
        tmp = p/f"{i}.partial"; fd = os.open(tmp, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); os.write(fd, data[i%8]); os.fsync(fd); os.close(fd)
        os.link(tmp, p/f"{i}.json"); os.unlink(tmp); dfd = os.open(p, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
rate("file/doc: tmp+fsync+link+unlink+dir fsync (today's extraction publish)", files_fsync_each)
def files_batched():
    p = d/"b"; p.mkdir(); fds = []
    for i in range(N):
        fd = os.open(p/f"{i}.json", os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); os.write(fd, data[i%8]); fds.append(fd)
        if len(fds) == 100:
            for f in fds: os.fdatasync(f); os.close(f)
            fds = []; dfd = os.open(p, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
rate("file/doc: write 100, fdatasync each, one dir fsync per 100", files_batched)
def files_syncfs():
    p = d/"c"; p.mkdir()
    for i in range(N):
        fd = os.open(p/f"{i}.json", os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); os.write(fd, data[i%8]); os.close(fd)
        if i % 100 == 99: os.sync()
rate("file/doc: write, one sync() per 100", files_syncfs)
def files_nofsync():
    p = d/"e"; p.mkdir()
    for i in range(N):
        fd = os.open(p/f"{i}.json", os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); os.write(fd, data[i%8]); os.close(fd)
rate("file/doc: write only, no fsync (floor)", files_nofsync)
# sqlite blobs
for jm, sync, batch in (("DELETE","FULL",1), ("WAL","FULL",1), ("WAL","NORMAL",1), ("WAL","FULL",100), ("WAL","NORMAL",100), ("DELETE","FULL",100)):
    def blobs(jm=jm, sync=sync, batch=batch):
        c = sqlite3.connect(d/f"blob-{jm}-{sync}-{batch}.db", isolation_level=None)
        c.execute(f"PRAGMA journal_mode={jm}"); c.execute(f"PRAGMA synchronous={sync}")
        c.execute("CREATE TABLE b(id INTEGER PRIMARY KEY, h TEXT, body BLOB)")
        for i in range(N):
            if i % batch == 0: c.execute("BEGIN")
            c.execute("INSERT INTO b VALUES (?,?,?)", (i, hashlib.sha256(data[i%8]).hexdigest(), data[i%8]))
            if i % batch == batch-1: c.execute("COMMIT")
        c.close()
    rate(f"sqlite blob 70KB/doc journal={jm} sync={sync} batch={batch}", blobs)
# small-row ledger-like inserts
for jm, sync, batch in (("DELETE","FULL",1), ("WAL","FULL",1), ("WAL","NORMAL",1), ("WAL","NORMAL",100), ("WAL","FULL",100)):
    def rows(jm=jm, sync=sync, batch=batch):
        c = sqlite3.connect(d/f"row-{jm}-{sync}-{batch}.db", isolation_level=None)
        c.execute(f"PRAGMA journal_mode={jm}"); c.execute(f"PRAGMA synchronous={sync}")
        c.execute("CREATE TABLE r(id INTEGER PRIMARY KEY, a TEXT, b TEXT, c INT)"); c.execute("CREATE INDEX ri ON r(a)")
        for i in range(N):
            if i % batch == 0: c.execute("BEGIN")
            c.execute("INSERT INTO r VALUES (?,?,?,?)", (i, f"path/{i}", "x"*200, i))
            if i % batch == batch-1: c.execute("COMMIT")
        c.close()
    rate(f"sqlite 3 small rows/doc? (1 row 250B) journal={jm} sync={sync} batch={batch}", rows)
import shutil; shutil.rmtree(d)
