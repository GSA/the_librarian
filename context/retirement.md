# Retiring a legacy source tree

Runbook for removing a legacy working copy (for example the pre-Librarian docs repository an
instance was populated from) once the instance holds everything worth keeping. It is generic:
instance paths, counts and the owner's decisions belong in the Kata comments of the retirement
item, never here. Everything before the final removal is read-only against the tree and
reversible; the steps that delete or remove are marked **OWNER-APPROVED** and are never run on an
agent's own authority.

The Librarian instance, not the legacy tree, becomes the system of record. The cold archive is
the recovery route for anything the instance did not take, so it is built and proven before any
original is deleted.

## Preconditions (gates)

Do not start the irreversible steps until each gate has a recorded result:

1. **Intake complete.** The instance runs current `main` and every inbox of the tree has been
   through `classify`, `ingest`/`qualify` and `publish` (see
   [Read-only cohort intake](intake.md#read-only-cohort-intake)); remaining `needs-*` items have
   dispositions.
2. **Dispositions.** Uncommitted work in the tree is committed to a branch or has a recorded
   per-file disposition; the tree's `git status --porcelain -uall` is empty or explained.
3. **Retrieval parity.** The instance retrieves at least as well as the tree did (side-by-side
   evaluation signed off by the owner).
4. **Consumers repointed.** Nothing still reads the tree (agents, scripts, scheduled jobs).
5. **Restore drill.** The restore procedure below was run once from the cold archive and from the
   instance backups with zero mismatches.

## 1. Freeze and record the starting state

- Stop writers: no agent edits the tree and no intake run is in flight. Make the tree read-only for
  the duration (`chmod -R a-w` on a copy is not enough; ask the owner to stop the writers).
- Record the tree's refs (`git for-each-ref`), `HEAD`, `git status --porcelain -uall --ignored`
  counts, free space on the archive destination, and the instance revision and `status`.

## 2. Final qualify over every inbox

Run `classify` (dry run) over **each** inbox directory of the tree with the same `--system` label
and `--agent-origin` file used when it was first ingested, then `ingest` / `qualify` /
`publish`. Reusing a label with a changed declaration for unchanged bytes is refused as
`receipt/submission_conflict` ([Agent-written context](intake.md#agent-written-context)); decide
the label before the run, not after. Archives are expanded explicitly
(`cohort expand-archives`, dry run first); a nested archive needs one run per level.

The final `classify` must leave no file as `admit` without a receipt. What remains
(`needs-reader`, `needs-runtime`, `unreadable`, `over-limit`, `unsafe-path`) is carried into the
manifest and gets a disposition in step 3.

## 3. Hash manifest with receipt join

`python -m tools.retirement manifest` hashes every file below the tree (streamed SHA-256, links
recorded not followed, `.git` included) and joins it, read-only, to the instance:

    python -m tools.retirement manifest --root INSTANCE --tree TREE --out PRIVATE_DIR \
        --system PREFIX=LEDGER_SYSTEM [--system ...] [--dispositions RULES.json] \
        [--catalog-file VERIFIED_CATALOG_BACKUP] [--workers N]

`--system` maps a tree prefix (may be empty) to the ledger `--system` label used at ingest.
`--catalog-file` reads a verified catalog backup instead of the live catalog, so the join cannot
disturb a running writer. Output goes to a new 0700 directory outside the tree and the instance's
git checkout: `manifest.jsonl` (header line, then one entry per file: `path`, `size`, `sha256`,
`category`, `receipt`) and a body-free `summary.json` with per-category file and byte counts. The
exit status is 0 only when nothing is unresolved. Neither file is committed anywhere public.

Categories, in the order they are decided:

| Category | Meaning | Unresolved |
| --- | --- | --- |
| `receipt-verified` | the ledger row for this path has the same SHA-256 and its receipt's original has it | no |
| `receipt-by-content` | the bytes are a catalog original under another path (duplicate, moved file) | no |
| `archive-expanded` | an archive with an expansion manifest in the instance; its members are checked by `delete-archive-original` | no |
| `empty-file`, `symlink`, `git-internal` | nothing to register; symlink target recorded; `.git` is covered by the history bundle | no |
| `disposition:NAME` | an explicit rule from `--dispositions` applied to a gap (never over a receipt) | no |
| `no-receipt` | no ledger row and no catalog original with these bytes | **yes** |
| `ledger-hash-mismatch` | the file changed since intake: re-ingest it as a correction; **no rule can resolve it** | **yes** |
| `ledger-not-in-catalog` | the ledger names a receipt the catalog does not hold | **yes** |
| `archive-not-expanded` | an archive without a stored expansion | **yes** |
| `unreadable`, `changed-during-read` | the manifest could not get stable bytes, or a directory could not be listed (`kind: directory`) | **yes** |

A `receipt-verified` file can still have an incomplete extraction (the entry carries
`ledger_state`); having a receipt means the original's bytes are registered, not that they are
searchable. Retrieval gaps are tracked by the intake items, not by this manifest.

Every unresolved file needs a recorded disposition before the archive step. Dispositions are a
private `retirement-dispositions/1` file of ordered rules, each with exactly one matcher (`path`,
`prefix`, `suffix` or `sha256`), a `disposition` name and a `reason`:

    {"schema": "retirement-dispositions/1", "rules": [
      {"match": {"prefix": "tools/"}, "disposition": "tooling", "reason": "legacy tooling, archived only"},
      {"match": {"suffix": ":Zone.Identifier"}, "disposition": "stream-sidecar", "reason": "download marker"}]}

Typical dispositions: `tooling`, `regenerable` (virtual environments, caches, build output),
`legacy-store` (the tree's own ignored databases; archived only), `stream-sidecar`,
`deferred-local-copy`, `superseded`, `owner-waived`. A rule may only dispose of a registration gap (`no-receipt`, `ledger-not-in-catalog`,
`archive-not-expanded`); it can never hide a hash mismatch for a file that has a receipt. Rules are an owner-reviewed decision, so keep
the rules file with the manifest and record who approved it.

**Deferred local copies.** The tree's own inbox policy governs removing the local copy of a video or
a failed-extraction file: record its original filename and path, observed size and hash, the exact
catalog identity, why it was not reviewed and how to request it again; prefer a byte-verified
archive member or remote identity; label an unverified remote copy as such; mark the artifact
unavailable in the catalog; removing it does not advance its stage or justify an answer from
content nobody inspected. The cold archive keeps these bytes, so with an archive this is a
record, not a loss; it matters if a file is excluded from the archive.

Keep the pre-deletion `manifest.jsonl` and its `manifest_sha256` unchanged: it is the baseline the cold archive is
proven against (step 4). A later regenerated manifest never replaces it.

Keep `manifest.jsonl` and its `manifest_sha256` (printed in `summary.json`); the cold archive and
the restore drill are verified against it. Re-run the manifest after any change to the tree or the
catalog; an old manifest is evidence only for the state it hashed.

## 4. Byte-verified cold archive

Archive **before** any original is deleted (step 5), so the archive holds every byte the tree held.

1. **Git history of every branch.** The tree may have no remote, so the working copy is the only
   copy of its history. Bundle all refs, then prove the bundle:

       git -C TREE for-each-ref > refs.txt
       git -C TREE bundle create DEST/tree.bundle --all
       git bundle verify DEST/tree.bundle
       git clone --mirror DEST/tree.bundle TMP/clone.git
       git -C TMP/clone.git fsck --full --strict
       git -C TMP/clone.git for-each-ref | diff - refs.txt   # same refs and object IDs

   Compare each branch's tree object as well (`git rev-parse BRANCH^{tree}` in both) and confirm
   the stash and any worktree-only branches were recorded in step 1.
2. **Tree archive.** Write a tar of the whole tree, `.git` included, with no exclusions: any
   exclusion is a disposition and must be in the manifest. Do not rewrite or recompress bytes
   that are already compressed; a plain tar is verifiable and cheap to restore:

       tar --create --file DEST/tree.tar --sort=name --numeric-owner -C PARENT TREE_NAME
       sha256sum DEST/tree.tar > DEST/tree.tar.sha256
3. **Prove it.** Extract into an empty temporary directory and compare with the manifest:

       tar --extract --file DEST/tree.tar -C TMP/restore
       python -m tools.retirement verify --manifest PRIVATE_DIR/manifest.jsonl \
           --tree TMP/restore/TREE_NAME --report TMP/verify.json

   `verify` reports counts of `missing`, `extra`, `size_mismatch`, `hash_mismatch`,
   `link_mismatch` and `unreadable` and exits 1 unless all are zero; the per-file detail goes to the
   private report. Then run `git fsck --full` in the restored tree. Verify the same manifest against
   the live tree too, so an archive of a file that changed between the manifest and the archive is
   caught.
4. **Place two copies** of `tree.tar`, `tree.bundle`, their `.sha256` files and the manifest on
   storage that survives the machine, and re-verify the checksum from each location after the copy
   (a copy is not proof). Record sizes, hashes, locations and timings in the retirement Kata.

Destination space must hold the archive, the bundle and the temporary extraction at once.

## 5. Delete verified archive originals (OWNER-APPROVED)

Only after step 4. Originals of archives in the tree's inboxes are removed with
`cohort delete-archive-original`, which refuses unless every member has a disposition, each
admitted member's bytes are registered in the catalog, and the archive still matches its
expansion manifest ([Archives](intake.md#archives)). Run it as a dry run over every archive and
review the `blocked` list; there is no override. `--apply` appends a fsynced record to
`archives/deletions.jsonl` before it unlinks; keep that file with the manifest. Never delete
archives in bulk without reading the dry-run output, and never delete the tree's copy of anything
the cold archive does not hold.

**Instance backup caveat.** The instance's archive expansions
(`<intake home>/archives/<sha256>/members/`) can be very large and an instance backup may exclude
them because the originals still exist in the tree. Once an original is deleted, the expanded
members are the only retained bytes of those archives in the instance. So before the first
deletion, either (a) take a **fresh instance backup that includes the expanded members** and verify
it, or (b) confirm the original archives are in the verified cold archive. Do both when space
allows. The member bytes are the receipts' retained originals; losing them leaves the catalog
describing files that no longer exist.

## 6. Soak, then owner-approved removal (OWNER-APPROVED)

Keep the tree read-only (not deleted) for an owner-chosen period of normal use of the instance.
Before removal, repeat: the checksum of every archive copy, the restore drill below, and a check of
the live tree against the **pre-deletion manifest** that accounts for the originals step 5 removed:

    python -m tools.retirement verify --manifest PRIVATE_DIR/manifest.jsonl --tree TREE \
        --deletions INSTANCE/<intake home>/archives/deletions.jsonl --report TMP/live.json

`--deletions` accepts a missing entry only when a deletion record names that tree-relative path and the
manifest's SHA-256 (counted as `removed`); everything else missing, changed or extra, and every
directory that cannot be listed, is still a mismatch. The cold archive is verified without
`--deletions`, since it holds every original. Removal is the owner's explicit decision on the evidence in
the Kata (counts, mismatches, soak dates). Remove only the tree; the archive copies, manifest,
dispositions file and `deletions.jsonl` stay.

## Restore drill

Proves the archive and the instance backups are recoverable before the tree is removed (Kata r49h
owns the drill's results and the soak record; this is the procedure):

1. Restore the cold archive into a temporary location (step 4.3) and verify every file against the
   manifest and `git fsck`.
2. Restore the instance from its backups into a temporary root following the instance backup
   policy ([backups](../README.md#configuration)); run `python -m tools.store_audit --root TEMP_ROOT` and
   a `status`/`search` smoke test against the restored root.
3. Confirm the restored catalog accounts for the manifest: re-run `manifest --catalog-file` against
   the restored catalog with the same dispositions file and compare the `summary.json` category counts
   with the original run. Every `receipt-verified` and `receipt-by-content` entry must have its
   receipt (its SHA-256 among the restored catalog's originals), and the resolved categories are checked
   separately: `disposition:*` entries against the dispositions file, `archive-expanded` entries
   against their stored expansion manifests (each member's bytes present or in the catalog),
   `git-internal` by the bundle check in step 4.1, and `symlink` and `empty-file` by the byte check
   of step 4.3.
4. Record counts, mismatches (must be 0) and timings.

## Safety notes

- The manifest and its report hold paths and hashes, which can themselves be sensitive. Keep them
  in a private (0700) directory outside any repository; never paste paths, titles or content into
  public issues or PRs, only counts.
- `tools.retirement` opens SQLite read-only and never writes the tree or the instance. A
  `--catalog-file` backup keeps it off the live writer's files; it is a dry-run join, not an
  integrity check, so run `tools.store_audit` separately.
- The archive tar and the bundle contain everything the tree contained, including restricted
  material. Store them under the same handling rules as the tree.
