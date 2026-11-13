# Kata efz3 throwaway prototypes (scratch branch, never merged)

Synthetic data only; run from a checkout root with its `.venv`, `TMPDIR` on a real disk, and (for fsync counts)
`gcc -O2 -shared -fPIC -o libcount.so count.c -ldl` + `LD_PRELOAD`. Paths inside the scripts assume they sit in
`.local/plans/proto/` (copy them there). They monkeypatch the toolkit in-process; nothing here is production code.

- `seed_prod.py DIR N` build a production-shaped N-document instance directly in SQL
- `e1_first_ingest.py`, `e4_steady.py` first-ingest cost per document at a seeded size (cProfile buckets, fsync counts)
- `e2_sql_attrib.py` SQL time by statement; `e3_scan.py` the instr() scans; `e5_integrity_costs.py` each integrity mechanism
- `e6_qualify_serial.py`, `e8_concurrency.py`, `p4_events_cache.py`, `p4_e8.py` qualify serial cost and concurrency scaling
- `e7_catalog_batch.py`, `e9_batch_full.py`, `p1_index_patch.py`, `p3_expr_index.py` catalog batching and index prototypes
- `p2_docstore.py`, `p2_detect.py`, `mkinbox.py` per-document state table + stat-based change detection prototype
- `m1_storage_micro.py` fsync / WAL / blob-vs-file micro-benchmarks
