"""Change-detection check for the stat-based skip: same-size edit with mtime restored, truncate+rewrite, rename-over, touch only."""
import os, sys, tempfile, shutil, time
sys.path.insert(0, os.getcwd()+"/.local/plans/proto"); sys.path.insert(0, os.getcwd())
from pathlib import Path
import p2_docstore as P
d = Path(tempfile.mkdtemp(dir=os.environ["TMPDIR"])); inbox = d/"in"; inbox.mkdir()
for n in "abcde": (inbox/f"{n}.md").write_text(f"# {n}\nbody {n}\n")
db = str(d/"x.db"); print("first", P.ingest(db, str(inbox), 1, run=1))
st = os.stat(inbox/"a.md"); (inbox/"a.md").write_bytes(b"# a\nBODY a\n"); os.utime(inbox/"a.md", ns=(st.st_atime_ns, st.st_mtime_ns))  # same size, mtime restored
(inbox/"b.md").write_text("# b\nlonger body now\n")                                                                                   # size change
os.replace(inbox/"c.md", inbox/"c.old"); (inbox/"c.md").write_text("# c\nbody c\n")                                                  # same content, new inode
os.utime(inbox/"d.md")                                                                                                                # touch only
print("replay", P.ingest(db, str(inbox), 1, run=2), "(expect hashed 4: a,b,c,d(touch changes mtime+ctime)+c.old new => 5 files total hashed 5?)")
print("e unchanged skipped; changed counts content-different only")
shutil.rmtree(d)
