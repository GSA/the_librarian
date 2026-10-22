# 0004 - One Librarian per project, with its own data root

**Audience:** maintainers and operators.
**Date:** October 21, 2026.
**Status:** Accepted. Describes the intended deployment shape; the
configuration work it depends on is not implemented.

## Context

The intent is one Librarian per project, each with its own generated data
store, plus a personal instance. Projects do not share sources, handling
rules, audiences or review history, and context from one project must never
appear in another's answers.

Three topologies were considered.

**Shared toolkit, many data roots.** One installed copy of the code, selected
per invocation with `--root`. Smallest upgrade surface: one environment, one
pin set, one place to fix a bug. Blocked today, and not superficially:
`tools/policy.py` resolves policy relative to the toolkit directory and loads
it at import time, so one toolkit has exactly one policy. Policy decides what
may be read and served and is hashed into the derived index. A shared toolkit
would therefore serve every data root under a single project's restrictions —
precisely the isolation failure to avoid. Store homes are also code constants
rather than configuration, so a second data root cannot even choose its own
layout.

**Container per instance.** Strong isolation and a clean upgrade story in
principle. Today the derived index binds the absolute corpus root as identity,
so a mount path change invalidates the index; and the MCP adapter is a stdio
process a client launches, so containerizing it moves a new failure class
between the client and the reader. Neither is fatal, both are unresolved. See
[0003](0003-store-compatibility.md).

**Full clone per instance.** Each instance is a complete copy of the toolkit
plus its own data root, policy, selection and stores — exactly what the
originating installation already is. Isolation comes from the filesystem,
which needs no new code. The cost is `N` environments and `N` upgrade paths.

## Decision

Each Librarian instance is a full clone: its own checkout of the distribution,
its own virtual environment, its own `config/policy.local.json` and
`config/selection.local.json`, its own inbox and its own stores. Instances
share nothing at runtime. No instance reads another's data root, imports code
from another's checkout, or inherits another's policy.

An instance's identity is its directory. Name it for the project it serves.
An instance records, in its own private workspace: which distribution version
it runs, which contract versions its stores hold, which sources it is
authorized to ingest, and who may read its answers. The distribution itself
records none of this.

Instances may run on different distribution versions indefinitely. This is a
feature: a project in the middle of a deadline is not forced to upgrade
because another project did. It is also already true — the originating
installation runs the pre-rename identity described in
[0001](0001-project-identity.md).

Upgrade procedure per instance, in order: replace the code from a
distribution artifact, refresh the pinned environment, run the preflight,
rebuild the derived index, and only then resume serving. If the preflight
reports a required migration, stop and perform the migration deliberately
with its recovery path.

A personal instance is an instance like any other, with its own policy and
audience. "Personal" is not a reason to relax handling rules; a home-server
instance guiding an assistant agent is still serving qualified context under
a declared audience.

## Consequences

- Isolation is strong and needs no new mechanism. A bug in cross-instance
  access control cannot exist because there is no cross-instance access.
- Upgrades are `N` separate operations. The mitigation is that an upgrade is
  "replace code, refresh environment, rebuild index" and touches no authority
  data, per [0003](0003-store-compatibility.md).
- Disk cost is `N` × (code + environment + corpus + stores + unbounded
  retained backups). Backup volume dominates and grows; see
  [0003](0003-store-compatibility.md).
- A fix must be applied `N` times, so the distribution artifact and its
  allowlist become the single source of truth for what an instance should
  contain. Hand-editing one instance's code creates a divergent installation
  that no later artifact can reconcile.
- Cross-project questions are not answerable by one instance, and must not be
  made answerable by pointing an instance at another's data. If such a need
  appears, it is a new design with its own authorization, handling and
  attribution rules — not a configuration change.

## Path toward containers without blocking on them

The clone model is the shape a container image would deploy anyway: one
instance, one data root, nothing shared. Making store homes and index path
binding configurable (per [0003](0003-store-compatibility.md)) converts a
clone into a container with a mounted volume, without changing the topology.
The decision here is therefore not a commitment against containers; it is the
sustainable shape to run today while the portability work is done.

## Not decided

- Whether instances are created by cloning a git checkout, unpacking a release
  artifact, or an installer command.
- How many instances are expected, and therefore when `N` environments stops
  being acceptable.
- Whether the personal instance runs on the same host as project instances.
