# Integrity of retained files

How freshness and file integrity are separated, and what an operator or agent does about each.

## The rule

The owner is the only provider of documents, and automated intake is the only other writer. **Nobody
edits files under an instance's data and source roots by hand**: not originals under `sources/`, not
retained extractions or submission sidecars under the extractions home. A document changes only by going
through intake, which records its SHA-256 and size in the catalog. Those recorded hashes are the reference.

## What a read proves, and what it does not

- `status`, `search`, `get`, `get-many` and claim corrections check that the index is fresh against what
  the catalog and review events pin (`rendition_identity`): the pinned hashes of every selected original,
  extraction and sidecar, the selection, policy, manifest and code. They do **not** reopen the files, so a
  file edited by hand after ingest does not make the index stale and does not stop a read.
- A change to a pin (a catalog commit that re-pins a path, which is how an ingest-controlled document
  update will arrive) does make the index stale: rebuild it.
- Build and publication open and verify every selected file in full and refuse a file that differs.

## Finding a changed file

- `python -m tools.context_index.integrity --root ROOT audit` re-hashes every registered file and lists
  each mismatch (`path`, `kind`, `problem`, `expected`, `found`). It writes nothing and never fails a
  read. Run it on a schedule (README, "Integrity audit and the no-hand-edit rule" has cron and systemd);
  exit status 1 means mismatches, 2 means it could not run.
- Code that opens a retained file for anything beyond the index (a deeper search of an original, a
  re-extraction) reads it with `integrity.read_verified(root, path)`, which checks that one file's SHA-256
  against the catalog's current pin and raises `IntegrityMismatch` instead of returning changed bytes.

## What readers show after an audit

`audit --record` also writes the result beside the index (`<index>.sqlite3.integrity.json`, private,
checksummed, tied to that index file). Readers then mark each affected document (the extraction itself,
and every rendition made from a changed original or whose submission sidecar changed) with the
low-confidence reason `integrity-changed` instead of failing: the text is what was verified at ingest, but
do not cite its source locations as verified, and say so when you cite it. `search --low-confidence
exclude` leaves flagged documents out. `status` and the progress page give the count, the list and the
command to resolve it; `integrity: audit none-for-this-index` means no audit has been recorded, not that
the files are clean. A rebuild clears the flags (it verified every selected file); re-run the audit.
`build` refuses to publish while a selected file differs from its pin, names all of them and leaves the
current index in place.

## When the audit reports a mismatch

Do not repair in place. Restore the file from backup, or re-ingest the document through intake so the
catalog re-pins it, then rebuild the index and re-run `audit --record`. Treat the file's text as untrusted either way. Report an
unexplained mismatch to the owner: nobody is meant to edit these files.
