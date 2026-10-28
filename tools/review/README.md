# Selected staged-source pre-commit review

This opt-in gate reviews **only the explicit technical allowlist** in
`roborev_precommit.py`. It is not a whole-commit review, secret scanner,
publication approval or content-handling authorization. Excluded staged paths
are printed as `NOT reviewed`. Mixed selected/excluded staged changes block
before any review launch; commit those scopes separately rather than exporting
restricted paths. An excluded-only commit receives no review,
not a passing review. The allowlist has exact paths, not directory globs:
the ordinary extractor, Docling modules/tests/requirements/guide, these hook sources/guide,
`tests/test_kit.py`, `release-files.json`, the context rendition adapter, the three store modules and their
schema snapshots, and the exact generic identity/closure/roadmap/ingestion-plan
documents named there. Root `README.md` and `ROADMAP.md` are selected; neighboring
documents are not implicitly selected. Customer documents, raw inputs,
unlisted tools/tests/documents, configuration, operational stores, models and
history are excluded, including from unchanged comparison context.
Never put restricted material or credentials inside allowlisted source files.

## Install in this repository

Use Python 3, Git and an **existing executable, scoped roborev wrapper**.
That wrapper must select the intended daemon and preserve its approved
environment, agent/provider routing and credentials boundary. No launcher,
daemon or routing is installed or changed here.

```sh
python3 tools/review/install_hooks.py --launcher /absolute/path/to/scoped-wrapper
git config --local --get core.hooksPath
git config --local --get roborev.precommitLauncher
```

The installer sets only local Git keys: `core.hooksPath=.githooks` and
`roborev.precommitLauncher`. Machine paths are not versioned. Run it separately
in each installation. A data-empty downstream checkout needs its own existing
wrapper; no development-machine path is assumed. Installation refuses
conflicting effective/local configuration, unexpected shared hook contents,
symlinked hook paths and existing native hooks (including other hook types
that changing `hooksPath` would shadow). It does not chain, remove or replace
them. Resolve those conflicts deliberately outside this installer.
Repeated installation with identical local configuration is harmless.

The release allowlist includes the portable hook, installer, gate, guide and
regressions, not machine-generated hooks or local launcher configuration.
After extracting a source archive, run `chmod +x .githooks/pre-commit` if its
executable bit was not retained. `.githooks/pre-commit` must be executable. Snapshot storage
`.local/roborev-precommit/` must already be Git-ignored. If a downstream
installation does not ignore `.local/`, its operator can explicitly add this
scoped directory to its local `.git/info/exclude` before use. Neither installer
nor hook edits ignore files automatically.

## Commit and inspect

Stage intended changes, then use ordinary `git commit`. You can also run:

```sh
python3 tools/review/roborev_precommit.py
python3 -m unittest discover -s tools/review -p 'test_precommit.py' -v
```

The hook reads blobs from HEAD and the actual commit index, including Git's
temporary index for partial commits. It never runs `write-tree` against the
source index and never reads source bytes from the worktree. Symlinked paths,
symlink/gitlink tree entries, binary/non-UTF-8 files and oversized selections
are refused (4 MiB per source file; 32 MiB per base/current source inventory).
Unstaged/untracked files are not exported.

Each run creates an owned private source-only repository under ignored
`.local/roborev-precommit/<random-id>/repo`, with no origin, copied Git history,
caller configuration, full worktree or caller environment. Its initial commit
contains only allowlisted HEAD context; its staged dirty changes contain only
allowlisted index bytes. Hooks and Git templates are disabled **only inside
that synthetic repository**, preventing recursive reviews. The real feature
commit still runs its pre-commit gate normally.

Snapshot Git ignores system attributes in its own child environment and pins
`core.attributesFile=/dev/null` locally. Its private `.git/info/attributes`
also forces textual diffs and disables text/EOL, encoding, ident and filter
conversion at the highest attribute precedence. Those rules protect independent
reviewer/daemon Git readers even when the existing wrapper replaces environment
variables or selects a different home. Actual user/system configuration and
attributes are never edited. Snapshot HEAD/index blobs, modes and worktree
bytes are compared with the original selected inventories before and after
review, preserving CRLF rather than silently normalizing it.

The existing wrapper is called synchronously with:
`review --dirty --wait --agent pi --panel none --quiet`. Quiet mode prevents
roborev's automatic hook installation. There is no `--staged` fallback,
automatic fix/refine, direct daemon HTTP call, global install or service restart.
The pinned CLI must also support `list --json` and `show --job --json`.
After the wait, the hook requires one completed Pi dirty-review job for that
unique snapshot, an explicit stored passing verdict and a stored diff exactly
matching the snapshot diff in the saved prompt (the pinned CLI does not expose
`diff_content` in job reads). Filtered, truncated or incompatible prompt formats
block rather than silently reducing review scope; split overly large staged
changes if needed. Queueing, skipping, missing/legacy verdicts,
findings, timeouts and unavailable tools/services all block the commit.
The expected diff is forced textual from the verified index, with external
diff/textconv and rename summarization disabled; ordinary worktree diff output
must match it exactly. Binary summaries, omitted hunks and normalization cannot
serve as complete review evidence.
The review command and each evidence query have a 900-second timeout.
Changing the real index or HEAD during review also blocks; retry the commit.
This final check is not an atomic lock against later concurrent Git writers.

`result.json` beside the synthetic repo records selected/excluded paths, HEAD,
index-entry SHA-256, exact selected staged-diff SHA-256 and result/job identity.
Snapshots and their results are retained on success or failure for inspection,
not silently expired. A failed review command's error is recorded there.
A quiet review may exit 1 with no output because its completed verdict is fail,
not because enqueue failed. On a nonzero review exit the gate queries the exact
snapshot with `--all-branches --limit 10`, verifies each completed failed Pi
dirty-review job against its stored identity/verdict, and reports job IDs and
findings (a combined 16,384-character findings budget divided across returned
jobs). Stored failed execution statuses are identified separately as
infrastructure failures. Repeated snapshot reviews are reported as history,
not proof of which job caused the command exit; the gate never chooses a
historical pass. A failed result query does not hide other verified findings.
This diagnostic is retained in `review.log`, and `result.json` preserves verified
failing IDs in `diagnostic_job_ids`, separately from the approval `job_id`.
Missing, mismatched or unavailable evidence is reported without claiming a
verified review; the original nonzero exit still blocks. Diagnostics never
replace the unchanged passing-verdict, exact-diff and source/index checks.
After a review is finished and its evidence is no longer needed, an operator
may remove that **specific run directory**; there is no automatic pruning.
Do not delete another run, active daemon workspace or authoritative store.
Private permissions limit ordinary local access; this is not an OS sandbox or
a guarantee against secrets embedded in otherwise allowlisted source.
