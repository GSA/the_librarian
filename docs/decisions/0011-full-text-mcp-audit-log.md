# 0011 - Opt-in full-text MCP audit log, separate from diagnostics

**Audience:** maintainers and operators.
**Date:** November 2, 2026.
**Status:** Accepted. Complements [0007](0007-execution-traces.md); does not change it.

## Context

The body-free retrieval diagnostics of decision 0007 cannot say what an agent
asked or what it was given: query text, locators and excerpts are not
representable in a diagnostic record. The owner relies on a record of exactly
what the MCP adapter returned to each agent, which the earlier falcon-docs
adapter kept with `--audit-log --audit-log-mode full --agent-label`. Rebinding
clients to the git-clone instance dropped that record.

## Decision

1. **Opt-in, per binding.** `librarian-mcp --audit-log [PATH] --agent-label
   LABEL` enables the audit; without the flag nothing is recorded. The label
   matches `[a-z0-9._-]{1,64}` and is required; a bare `--audit-log` writes to
   `.local/audit/<label>-mcp-access.jsonl`. The path must be a `.jsonl` file
   below the ignored `<root>/.local`, without symlinks, and cannot be the
   diagnostics log.
2. **One record per tool call, at the protocol boundary.** Every tool
   (`status`, `search`, `get`, `get_many`, `catalog`, `review_events`) writes
   one `librarian-mcp-audit-v1` record with run and call ids, timestamps,
   duration, label, tool, the full arguments as sent, outcome (`ok` or `error`
   with type), the full returned text, its character count and SHA-256, and the
   locators, source hashes and record ids it mentions. Argument-validation and
   reader errors are recorded with the message the client received.
3. **Private and durable.** Files are 0600 in a 0700 directory, opened with
   `O_APPEND|O_NOFOLLOW`, written under an exclusive lock and fsynced per
   record. A wider mode is refused rather than repaired.
4. **Failure withholds the result.** If the record cannot be written, the call
   returns an explicit error and not the result, so no returned text goes
   unrecorded.
5. **Rotation without deletion.** Before a record would take the active file
   past 64 MiB it is renamed to the next numbered file
   (`<name>.<n>.jsonl`). Nothing is truncated, overwritten or deleted by the
   toolkit; removing old audit files is an explicit operator act.
6. **Separation and sensitivity.** The audit log holds corpus text and agent
   queries at the instance's audience. It is never retrieval context, never
   indexed, searched or returned by a tool, and is not evidence of review. The
   body-free `--diagnostics-log` is unchanged. Operators summarize the audit
   with `librarian-audit --root <root> report [--log PATH]`, which prints
   counts by tool, agent and outcome, returned sizes and hash mismatches, and no
   text or queries.

## Consequences

Operators can reconstruct exactly what each labelled agent received and verify
it by hash. The log grows with returned text, so its disk use is the operator's
to watch. Client bindings must pass the flags explicitly; The Engineer's
`configure-librarian` follow-up adds them for its bindings.
