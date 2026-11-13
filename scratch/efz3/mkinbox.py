import sys, os
sys.path.insert(0, os.getcwd())
from pathlib import Path
from tools.diagnostics.ingest_benchmark import fixture
d = Path(sys.argv[1]); n = int(sys.argv[2])
fixture(d, n, seed="inbox40k", padding=int(sys.argv[3]), mixed="mixed")
print(sum(f.stat().st_size for f in d.rglob("*") if f.is_file())>>20, "MB", sum(1 for _ in d.rglob("*") if _.is_file()), "files")
