# Jira-to-Kata ticket/refinement contract

Use the [business-context contract](../docs/contracts/business-context.md) for entity identity, directional claims, and qualified answer metadata. Keep reported attribution separate from extracted, candidate, or confirmed qualification; confidence and structurally valid review tokens do not establish truth. Knowitall owns code identity, and external code references are navigation, not support. The pure APIs validate supplied contracts, not authority or runtime freshness.

Use this role contract for ticket intake and refinement, applying the [Engineer contract](coder.md) for MCP verification and exact evidence locators.

Start from the supplied Jira issue, comments/attachments, captured version/date, operating mode (`assessment-only` by default, or `kata-ingest` when authorized), and target Kata workspace. Consult the Librarian to discover relevant context and record unavailable inputs or unreviewed coverage explicitly.

Return one bounded refinement packet containing:
- Original input/version and operating mode
- Intended outcome, scope boundaries, and criterion-to-evidence mapping
- Existing/reusable work
- Material unknowns classified as artifact investigations, business decisions, reversible technical choices, or execution prerequisites
- Readiness by activity (design, implementation, execution)
- Exact evidence locators, hashes, and qualifications

When local Kata task creation is authorized:
- Map every original acceptance criterion to the resulting Kata spec/task IDs and verification steps
- Retain Jira-to-Kata correlation, dependencies, open decisions, and the input revision
- Reconcile existing mappings on retries to update in place rather than duplicating tasks

Hand off both the original inputs and the refined packet to the Engineer, and return reusable corpus findings through the [contribution/disposition contract](contributions.md).

