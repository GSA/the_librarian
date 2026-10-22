# 0002 - Reproducible runtime and an updateable distribution

**Audience:** maintainers and operators.
**Date:** October 21, 2026.
**Status:** Accepted. Not implemented; no lock file, image or installer exists
in this repository yet.

## Context

The goal is an updateable application that can be installed per project and
upgraded without touching the data it has already accumulated. Today an
installation is a directory of Python source plus a hand-built virtual
environment, and the only reproducibility guarantee is:

- `requires-python = ">=3.11"`, while the current working interpreter is
  3.14.4 with SQLite 3.46.1.
- `mcp==2.2.0` exactly pinned, `anyio` and `jsonschema` pinned only to a
  major range, and every transitive dependency unpinned.
- The optional Docling worker, which is the opposite extreme: a fully resolved
  CPU environment including transitive pins, installed into its own dedicated
  interpreter.

So two installations built a week apart can resolve different dependency sets.
The adapter's preflight already probes the runtime it is actually running on
(Python version floor, exact `mcp` version, SQLite version, FTS5 availability)
and refuses to proceed on a mismatch, which converts drift into a visible
failure rather than a silent behavior change. That is the property worth
preserving, and it is also why unpinned resolution has not yet caused damage.

Three separable concerns were being conflated: pinning what gets installed,
packaging what gets shipped, and isolating what runs.

## Decision

### Pinning

Pin the complete resolved runtime for the reader and adapter, the way the
Docling worker already does, and keep the preflight probe as the enforcement
point. A lock file states intent; the probe proves the intent held. Pin the
interpreter's minor version in the installation record, not only a floor,
because the derived index and the extraction engines are fingerprinted against
code that can change behavior across interpreter releases.

Declare and verify the SQLite requirements explicitly, including FTS5, rather
than assuming the host build provides them. The probe does this already; the
distribution must document it as a requirement, not an accident.

### Packaging

Keep the explicit source allowlist (`release-files.json`) as the definition of
what a distribution contains. It is already the mechanism that keeps
operational data, populated stores, private overrides and credentials out of an
artifact. An image or installer is a second packaging format over the same
allowlist, never a parallel definition.

A distribution contains code, versioned configuration, schema snapshots and
documents. It never contains a database, a corpus, an inbox or a policy
override.

### Isolation

Containers are accepted as a deployment option, with two constraints:

1. The data root is a mounted volume, never a layer in the image. An image is
   disposable; the data root is not. See
   [0003](0003-store-compatibility.md).
2. The heavy optional model runtime (Docling, torch, OCR assets) stays a
   separate image or a separate interpreter from the reader and adapter. The
   reader must remain installable and runnable on a machine that will never do
   OCR. Collapsing them would make a multi-gigabyte download a prerequisite
   for reading text.

The MCP adapter speaks stdio. A containerized adapter therefore needs the
client to launch the container as the server process, which is a real change
to every client registration and adds a failure mode (volume not mounted,
wrong user, path mismatch) between the client and the reader. Containerizing
the *ingestion and extraction* workers is valuable sooner than containerizing
the adapter, because workers are batch processes with no client contract.

### Sustainability constraints

- An installation must be upgradeable by replacing code and re-running the
  preflight, with no data migration in the common case.
- A version that changes persisted structure must ship its migration and
  recovery path with it, and must refuse to run against an unmigrated store
  rather than adapting silently.
- Every pinned environment needs a stated rebuild procedure. A lock file that
  no longer resolves is a worse failure than no lock file, because it is
  discovered during an emergency rebuild.

## Consequences

- `N` installations means `N` pinned environments to refresh. This is the
  accepted cost of the topology in [0004](0004-instance-topology.md); the
  mitigation is that upgrading is "replace code, run preflight", not
  "reconcile data".
- Pinning will occasionally block a security update until the pin is refreshed
  deliberately. That is the intended trade: unexpected behavior change in a
  system of record is worse than a scheduled dependency bump.
- Preflight becomes load-bearing, so it must stay fast, dependency-light and
  runnable before any store is touched.

## Not decided

- Whether the first container target is the ingestion worker, the whole
  instance, or both.
- Which packaging tool produces the lock file.
- Whether the adapter is ever containerized, or stays a host process against
  a mounted data root.
