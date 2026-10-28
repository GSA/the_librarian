# Repository Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the data-empty toolkit easier to navigate, import, run, test and maintain before its first authorized corpus, without weakening its safety contracts.

**Architecture:** Normalize repository modules into ordinary Python packages, keep operator entry points thin, and make the MCP adapter invoke fixed toolkit modules rather than caller-supplied script paths. Split only two proven mixed-responsibility modules, remove accidental duplicate tests and retired execution documents, and leave process isolation and authority-store semantics unchanged.

**Tech Stack:** Python 3.11+, standard-library `unittest`, SQLite/FTS5, MCP 2.2.0, JSON Schema, Git, Kata, RoboRev

**Spec:** `docs/superpowers/specs/2026-10-28-repository-simplification-design.md`

## Global Constraints

- Optimize for human readability, agent understanding and maintainability, not raw line reduction.
- Selective breaking changes require a concrete simplification and coordinated updates to every repository caller, test and document.
- Keep the repository data-empty; do not add deployment identities, source material, corpus facts, private paths, tickets or task history.
- Preserve authority-store, append-only, hash-chain, policy, freshness, attribution, budget, transaction, backup, mutation-race and fail-closed guarantees.
- Do not turn Docling worker or namespace isolation into in-process execution.
- Tests use temporary workspaces and never mutate an authoritative installation.
- Use `/home/nkirrish/the_librarian/.venv/bin/python` only as an installed dependency interpreter against this worktree; do not edit or initialize `/home/nkirrish/the_librarian`.
- Commit each task separately, inspect its RoboRev result, and make review fixes in new commits rather than amending.
- Any storage-definition change requires regenerated schema-only snapshots; this plan does not intentionally change storage definitions.

## Review Focus

- Invoking a module from outside the repository root must fail actionably rather than importing a same-named untrusted package; Task 2 pins the fixed-root subprocess behavior.
- A policy or source mutation during extraction must still refuse publication after imports move; Tasks 1 and 3 retain the existing race and clean-copy suites.
- Removing script-path overrides must not let MCP read tools from a different checkout; Task 2 tests the derived trusted toolkit paths.
- Package creation must reject duplicate, unsafe, missing and symlinked entries after retired documents leave the allowlist; Task 5 adds focused package tests.
- Test discovery must execute each ordinary behavior once while still including lightweight Docling worker contracts; Task 4 removes inherited duplicate workspace scenarios and retains the worker loader test.

---

### Task 1: Normalize toolkit packages and imports

**Files:**
- Create: `tools/__init__.py`
- Create: `tools/catalog/__init__.py`
- Rename: `tools/context-index/` to `tools/context_index/`
- Create: `tools/context_index/__init__.py`
- Rename: `tools/review-events/` to `tools/review_events/`
- Create: `tools/review_events/__init__.py`
- Create: `tools/extraction/__init__.py`
- Create: `tools/diagnostics/__init__.py`
- Create: `tools/review/__init__.py`
- Create: `tools/schemas/__init__.py`
- Modify: imports in `tools/catalog/inventory.py`, `tools/context_index/context_index.py`, `tools/review_events/*.py`, `tools/extraction/*.py`, and `tools/schemas/export_schemas.py`
- Modify: path references in `tools/initialize.py`, `tools/context-mcp/src/the_librarian_mcp/*.py`, tests, README/tool READMEs, decisions, roadmap, review tooling and `release-files.json`
- Test: `tests/test_kit.py`
- Test: `tests/test_docling.py`
- Test: `tools/extraction/test_docling_worker.py`
- Test: `tools/review/test_precommit.py`

**Interfaces:**
- Consumes: current public functions and CLI behavior.
- Produces: importable packages `tools.catalog`, `tools.context_index`, `tools.review_events`, `tools.extraction`, `tools.diagnostics`, `tools.review`, and `tools.schemas`; operator modules runnable as `python -m tools.<package>.<module>` from the toolkit root.

- [ ] **Step 1: Add failing package-import and module-help tests**

Add `PackageLayoutTests` to `tests/test_kit.py`. Assert imports work without modifying `sys.path`, all importable modules resolve beneath `ROOT / "tools"`, and these commands return zero from `ROOT`: `python -m tools.catalog.inventory --help`, `python -m tools.context_index.context_index --help`, `python -m tools.review_events.event_store --help`, `python -m tools.extraction.extract --help`, and `python -m tools.diagnostics.retrieval_log --help`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.PackageLayoutTests -v`

Expected: FAIL because hyphenated directories are not packages and tests currently rely on `sys.path` mutation.

- [ ] **Step 3: Rename directories and replace ordinary path-loading imports**

Use `git mv` for the two hyphenated directories. Add empty package markers. Replace same-process `sys.path` mutation and `spec_from_file_location` with absolute `tools.*` imports in production code, tests and schema export. Retain dynamic loading only for the separately installed MCP diagnostics seam until Task 2 and for fault-isolated test fixtures that intentionally load a copied file.

- [ ] **Step 4: Update every repository path and release entry atomically**

Use `rg 'context-index|review-events'` and update commands, defaults, schema snapshot source comments, links, clean-copy fixtures, review allowlists and `release-files.json`. Keep protocol names, SQLite authority strings and schema identities unchanged.

- [ ] **Step 5: Run package, extraction, review and schema checks**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest \
  tests.test_kit.PackageLayoutTests tests.test_docling tools.review.test_precommit -v
/home/nkirrish/the_librarian/.venv/bin/python -m tools.extraction.test_docling_worker
/home/nkirrish/the_librarian/.venv/bin/python -m tools.schemas.export_schemas --check
```

Expected: PASS; 7 real-Docling tests may report explicit opt-in skips.

- [ ] **Step 6: Commit and address RoboRev**

```bash
git add -A
git commit -m "Normalize toolkit package layout"
roborev show HEAD
```

Record valid fixes in a new commit and comment/close the reviewed job only after verification.

### Task 2: Remove caller-supplied MCP script paths

**Files:**
- Modify: `tools/context-mcp/src/the_librarian_mcp/config.py`
- Modify: `tools/context-mcp/src/the_librarian_mcp/server.py`
- Modify: `tools/context-mcp/src/the_librarian_mcp/preflight.py`
- Modify: `tests/test_mcp.py`
- Modify: `README.md`
- Modify: `tools/context-mcp/README.md`

**Interfaces:**
- Consumes: Task 1 module names.
- Produces: `Config(root: Path, manifest: Path = ..., db: Path = ..., ...)` with read-only `toolkit_root`, `context_module`, `event_module`, `catalog_module`, and `diagnostics_path` properties; `librarian-mcp` and `librarian-preflight` no longer accept `--reader`, `--event-reader`, `--catalog-store`, or `--event-store`.

- [ ] **Step 1: Write failing configuration and stdio tests**

Add tests asserting the parser rejects removed options, `Config(root)` derives tools only from that absolute toolkit root, subprocess argument lists use `-m tools.context_index.context_index` and `-m tools.review_events.event_reader`, and MCP starts with only `--root`. Add a test that changes the parent process working directory before constructing `Reader` and still gets commands rooted at `config.root`.

- [ ] **Step 2: Run tests and verify the old path-injection interface fails them**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_mcp -v`

Expected: FAIL on the new fixed-module assertions.

- [ ] **Step 3: Simplify `Config` and reader execution**

Remove script-path fields and parser options. Have `Reader.execute()` run `sys.executable -B -m <fixed module>` with `cwd=config.root`. Keep manifest, database, budgets, timeout and diagnostics-log validation unchanged. Load diagnostics from the fixed `config.diagnostics_path` and verify it is beneath the toolkit root before loading.

- [ ] **Step 4: Simplify preflight**

Remove store override parameters. Run schema, catalog, event and context checks through fixed module names with `cwd=config.root`. Preserve explicit prerequisite ordering, refresh gating and actionable error reports.

- [ ] **Step 5: Update commands and run MCP tests**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_mcp -v
/home/nkirrish/the_librarian/.venv/bin/librarian-preflight --help
/home/nkirrish/the_librarian/.venv/bin/librarian-mcp --help
```

Expected: all four MCP tests PASS and help omits all removed path overrides.

- [ ] **Step 6: Commit and address RoboRev**

```bash
git add tools/context-mcp tests/test_mcp.py README.md
git commit -m "Bind MCP commands to the toolkit root"
roborev show HEAD
```

### Task 3: Separate contracts from command adapters

**Files:**
- Create: `tools/catalog/contracts.py`
- Modify: `tools/catalog/catalog_store.py`
- Create: `tools/context_index/cli.py`
- Modify: `tools/context_index/context_index.py`
- Modify: imports and tests that refer to moved symbols
- Modify: `tools/schemas/export_schemas.py` only if its explicit source mapping changes

**Interfaces:**
- Consumes: Task 1 package imports.
- Produces: `tools.catalog.contracts` containing catalog record constants and validation functions; `tools.context_index.cli.arguments(argv)` and `tools.context_index.cli.main(argv)` as presentation adapters. `catalog_store` continues to expose existing validation names by explicit import during this cleanup; `python -m tools.context_index.context_index` delegates to `cli.main` for one release snapshot.

- [ ] **Step 1: Add failing boundary tests**

Assert `tools.catalog.contracts` validates the existing valid/invalid record fixtures without importing `sqlite3`, and importing `tools.context_index.context_index` does not import `argparse`. Assert both `python -m tools.context_index.cli --help` and the transitional context-index module command produce the same command set.

- [ ] **Step 2: Run boundary tests and verify they fail**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.PackageLayoutTests tests.test_kit.Workspace -v`

Expected: FAIL because validation and CLI presentation still live in the large modules.

- [ ] **Step 3: Move catalog contract code without generalizing store mechanics**

Move `KINDS`, artifact enums, field definitions and the exact validation helpers (`text` through `validate_graph`) to `tools/catalog/contracts.py`. Keep catalog-specific language and `CatalogError`; do not create a generic validation utility shared with review events.

- [ ] **Step 4: Move context CLI parsing and dispatch**

Move `arguments`, `main`, CLI exception formatting and command-only imports to `tools/context_index/cli.py`. Leave indexing, freshness, query and packet functions in `context_index.py`. Keep a minimal `if __name__ == "__main__"` delegation in `context_index.py` while repository docs switch to `tools.context_index.cli`.

- [ ] **Step 5: Verify authority and mutation-race contracts**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest \
  tests.test_kit.StoreIdentity tests.test_kit.Workspace tests.test_docling.DoclingLifecycle -v
/home/nkirrish/the_librarian/.venv/bin/python -m tools.schemas.export_schemas --check
```

Expected: PASS with no schema snapshot changes.

- [ ] **Step 6: Commit and address RoboRev**

```bash
git add tools/catalog tools/context_index tests tools/schemas
git commit -m "Separate domain contracts from CLI adapters"
roborev show HEAD
```

### Task 4: Remove accidental duplicate tests and repeated setup

**Files:**
- Create: `tests/support.py`
- Modify: `tests/test_kit.py`
- Modify: `tests/test_mcp.py`
- Modify: `tests/test_docling.py`
- Modify: `tests/test_worker_contracts.py`

**Interfaces:**
- Consumes: Task 1 package layout and Task 2 MCP invocation.
- Produces: `tests.support.clone_release(destination)`, `run_json(*argv, cwd, expected=0)`, and a non-test `WorkspaceCase`; ordinary discovery runs each shared workspace scenario once and still loads worker contract tests once.

- [ ] **Step 1: Add a discovery regression test**

Add `DiscoveryTests.test_workspace_scenarios_are_not_inherited_by_absence_tests` that loads `tests`, flattens the discovered IDs and asserts no method name appears under both `WorkspaceTests` and `AbsenceClassificationTests`. Assert the discovered IDs contain worker contract tests under exactly one loaded module name.

- [ ] **Step 2: Run it and verify duplicate inherited workspace scenarios fail**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.DiscoveryTests -v`

Expected: FAIL because `AbsenceClassification` currently inherits every `Workspace.test_*` scenario.

- [ ] **Step 3: Extract only intent-revealing test support**

Move release cloning and JSON subprocess assertions to `tests/support.py`. Rename the shared workspace fixture to `WorkspaceCase` with no `test_*` methods; derive separate `WorkspaceTests` and `AbsenceClassificationTests`. Keep race injection, subprocess seams and security setup inline where abstraction would hide the behavior.

- [ ] **Step 4: Replace repeated MCP initialization setup**

Add one MCP test helper that clones and initializes a temporary kit and one helper that yields `ClientSession`; do not combine the distinct diagnostics assertions.

- [ ] **Step 5: Run ordinary discovery and count unique IDs**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS; shared workspace scenarios run only under `WorkspaceTests`, worker contracts appear once, and 7 real-Docling tests remain explicit opt-in skips.

- [ ] **Step 6: Commit and address RoboRev**

```bash
git add tests
git commit -m "Clarify test fixtures and discovery"
roborev show HEAD
```

### Task 5: Simplify active documentation and release packaging

**Files:**
- Delete: `docs/superpowers/plans/2026-10-20-docling-ingestion.md`
- Delete: `docs/superpowers/plans/2026-10-22-ingestion-completion.md`
- Delete: `docs/superpowers/specs/2026-10-20-docling-ingestion-design.md`
- Delete: `docs/superpowers/specs/2026-10-22-ingestion-completion-design.md`
- Modify: `ROADMAP.md`
- Modify: `tools/extraction/README.md`
- Modify: `tools/review/roborev_precommit.py`
- Modify: `tools/review/test_precommit.py`
- Modify: `release-files.json`
- Modify: `tests/test_kit.py`
- Modify: `tools/package.py`

**Interfaces:**
- Consumes: Task 4 test helpers.
- Produces: current decisions, roadmap and tool documentation as the maintained design record; Git as the historical execution record; deterministic package validation with named `load_release_manifest(root)` and `release_paths(root)` functions.

- [ ] **Step 1: Add failing package-manifest tests**

Test duplicate entries, non-string entries, traversal, operational suffixes, missing files, symlinked files and a symlinked output parent. Assert the four retired execution documents are absent from `release_paths()` and every relative Markdown link in the release still resolves to an allowlisted file.

- [ ] **Step 2: Run focused documentation/package tests and verify they fail**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.Documentation tests.test_kit.PackageTests -v`

Expected: FAIL because retired plans remain allowlisted and manifest validation does not report entry-specific type errors.

- [ ] **Step 3: Remove retired execution artifacts and repair maintained references**

Delete the four completed plan/spec files from the active tree. Replace ROADMAP links with concise completion statements linking to decision 0008 and `tools/extraction/README.md`. Remove the historical files from the review gate allowlist and release allowlist. Do not delete accepted decisions or operator guidance.

- [ ] **Step 4: Make release validation explicit**

Add `load_release_manifest(root: Path) -> list[str]` to validate the JSON shape and each entry before filesystem access. Keep `release_paths(root: Path) -> list[tuple[str, Path]]` responsible for existence and symlink checks. Preserve the explicit allowlist; do not replace it with directory crawling.

- [ ] **Step 5: Verify artifact contents**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest \
  tests.test_kit.Documentation tests.test_kit.PackageTests tools.review.test_precommit -v
rm -f .local/the-librarian-cleanup.zip
/home/nkirrish/the_librarian/.venv/bin/python -m tools.package \
  --output .local/the-librarian-cleanup.zip
unzip -l .local/the-librarian-cleanup.zip
```

Expected: PASS; archive contains exactly the manifest, no retired plans, local policy, database, cache or source content.

- [ ] **Step 6: Commit and address RoboRev**

```bash
git add -A
git commit -m "Retire completed execution documents"
roborev show HEAD
```

### Task 6: Audit and normalize maintained scripts

**Files:**
- Modify: `tools/initialize.py`
- Modify: `tools/package.py`
- Modify: `tools/schemas/export_schemas.py`
- Modify: `tools/catalog/inventory.py`
- Modify: `tools/extraction/docling_extract.py`
- Modify: `tools/review/install_hooks.py`
- Modify: corresponding tests and tool READMEs

**Interfaces:**
- Consumes: Tasks 1-5 modules and helpers.
- Produces: every maintained command exposes `parser()` and `main(argv=None) -> int`, uses `allow_abbrev=False`, emits one JSON object on success, and emits `<command>: <diagnostic>` on stderr with nonzero status on expected operator errors.

- [ ] **Step 1: Add a parameterized CLI convention test**

For the six maintained commands, assert `main` accepts an argument list without replacing `sys.argv`, `--help` rejects abbreviated long options, and malformed input exits or returns nonzero with the documented command prefix. Keep store and worker CLIs out of this mechanical task because their parsers are part of larger domain contracts.

- [ ] **Step 2: Run convention tests and verify inconsistent signatures fail**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.ScriptConventionTests -v`

Expected: FAIL for commands whose `main()` or `parser()` cannot be called with arguments.

- [ ] **Step 3: Normalize parsers without introducing a generic CLI framework**

Give each listed module a local `parser()` and `main(argv=None)`. Add `allow_abbrev=False`; retain each command's domain-specific exceptions and messages. Do not add a shared utility module for a few lines of argparse or JSON code.

- [ ] **Step 4: Run script, clean-copy and hook tests**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest \
  tests.test_kit.ScriptConventionTests tests.test_kit.DoclingCleanCopy \
  tools.review.test_precommit -v
/home/nkirrish/the_librarian/.venv/bin/python -m tools.schemas.export_schemas --check
```

Expected: PASS.

- [ ] **Step 5: Commit and address RoboRev**

```bash
git add tools tests README.md
git commit -m "Normalize maintained command adapters"
roborev show HEAD
```

### Task 7: Record deferred extension work and current architecture

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `docs/decisions/README.md`
- Modify: a relevant decision only if an implemented interface changed its status or path
- Modify: `context/project.md`, `context/status.md`, and `context/tools.md` in the authorized private workspace, not this repository
- External state: Kata issues in project `librarian`

**Interfaces:**
- Consumes: final source layout and interfaces from Tasks 1-6.
- Produces: concise component map and exact supported commands; prioritized generic Katas related to existing `pwhw`, `xzkj`, `8a5h`, `zqdh`, `9hqs`, `1md4`, and decisions 0002-0005 without duplicating them.

- [ ] **Step 1: Add documentation contract assertions**

Assert README's component paths exist, every documented `python -m` command returns help successfully, and no versioned file contains the private validation target identity supplied in conversation.

- [ ] **Step 2: Run documentation tests and verify stale paths fail**

Run: `/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.Documentation -v`

Expected: FAIL until the final architecture map and commands are updated.

- [ ] **Step 3: Update maintained documentation**

Document package boundaries, thin command adapters, fixed-root MCP execution, supported test commands, deliberate Docling subprocess isolation and breaking path/option changes. Remove claims about interfaces deleted by this branch. Keep planned capabilities labeled as planned.

- [ ] **Step 4: Search and create only missing future Katas**

Use `kata search` before each mutation. Reuse/refine the existing ontology/entity, rendition, text normalization, intake and source-onboarding issues. Create only missing generic tasks for per-instance portable configuration or a first-corpus extension contract, relate them to the existing issues and decisions, and assign priorities/dependencies for the minimum first-corpus path. Keep deployment identity in the authorized private task system, not repository text.

- [ ] **Step 5: Verify docs and commit**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest tests.test_kit.Documentation -v
rg -n 'context-index|review-events|--reader|--event-reader|--catalog-store|--event-store' \
  README.md ROADMAP.md docs tools tests release-files.json
```

Expected: tests PASS; remaining legacy strings are either protocol/storage names explicitly retained or no matches.

```bash
git add README.md ROADMAP.md docs release-files.json tests
git commit -m "Document the simplified toolkit architecture"
roborev show HEAD
```

### Task 8: Whole-branch verification, review and PR

**Files:**
- Modify: implementation files only for verified failures or review findings
- Modify: private `context/` status files before handoff
- External state: Kata `djrb`, RoboRev reviews, GitHub pull request, Forge workspace target

**Interfaces:**
- Consumes: all prior tasks.
- Produces: verified branch, resolved review findings, closed implementation Kata and generated PR.

- [ ] **Step 1: Run the full supported verification suite**

Run:

```bash
/home/nkirrish/the_librarian/.venv/bin/python -m unittest discover -s tests -v
/home/nkirrish/the_librarian/.venv/bin/python -m tools.extraction.test_docling_worker
/home/nkirrish/the_librarian/.venv/bin/python -m tools.review.test_precommit
/home/nkirrish/the_librarian/.venv/bin/python -m tools.schemas.export_schemas --check
rm -f .local/the-librarian-final.zip
/home/nkirrish/the_librarian/.venv/bin/python -m tools.package --output .local/the-librarian-final.zip
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: all required tests PASS, only 7 explicitly opt-in real-Docling tests SKIP, schema check reports no changes, package succeeds, diff check is empty and tracked worktree is clean.

- [ ] **Step 2: Request independent whole-branch review**

Give the reviewer the original request, approved spec, this plan, base/head revisions, scoped diff, safety constraints, exact commands/results and known optional-test limitation. Require criterion mapping and inspection of original code rather than conclusions from this plan.

- [ ] **Step 3: Run RoboRev branch refinement**

Review every branch commit, inspect all findings, implement valid fixes one at a time with focused tests and new commits, and add technical rebuttals for invalid findings. Repeat until current commits and the branch review pass. Do not close a failed review merely because a later commit exists; link the fixing commit in its disposition.

- [ ] **Step 4: Re-run full verification after review fixes**

Repeat Step 1 exactly and record counts, skips, archive hash and artifact file count in Kata `djrb` and private `context/status.md`.

- [ ] **Step 5: Close Kata only with evidence**

Close `librarian#djrb` with commit range, commands/results, RoboRev job references, breaking changes and remaining deferred issue references. Leave it open with `needs-review` if any criterion or review remains unresolved.

- [ ] **Step 6: Push and create the PR**

Confirm `origin` targets the verified `gsa/the_librarian` repository, push `kenn-forge/work-22874a66`, and create a PR against `main`. The PR body must include goal, structural changes, breaking changes, safety invariants, verification, RoboRev/independent-review disposition, deferred Katas and the statement that packaging is not publication or deployment approval.

- [ ] **Step 7: Register and verify the workspace target**

When Forge target tools are available, register the canonical PR URL against workspace `22874a665ef96e79`, list workspace targets and correct any missing PR registration. Report registration failure rather than silently omitting it.
