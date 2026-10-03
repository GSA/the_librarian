# Optional refinement contract

Use refinement only when a task needs context synthesis. Apply the
[coder contract](coder.md), verify your own MCP reads and preserve original
acceptance criteria with exact locators.

Return one bounded packet: original input/version and allowed mode; intended
outcome and exclusions; criterion-to-evidence mapping; existing/reusable work;
material unknowns classified as artifact investigations, business decisions,
reversible technical choices or execution prerequisites; readiness by activity;
and exact evidence locators, hashes and qualifications.

Do not create new acceptance criteria from optional suggestions, infer
completion from historical status, or treat model confidence as authority.
The receiver gets the original input as well as the packet. Revisit affected
decisions when evidence changes rather than repeating all discovery.

Stop after the handoff unless explicitly assigned more work. Refinement is
not authorization to modify code, data, external tickets or deployment state.
