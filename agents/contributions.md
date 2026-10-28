# Context contribution and disposition

Use this private handoff contract to submit reusable corpus findings from engineering or refinement work to an authorized Librarian maintenance session. Keep instance-specific findings in the program's private workspace.

## Contribution

Use one bounded block per reusable finding and keep task-only implementation choices on the originating task. Mark missing evidence as `unknown` or `not inspected`.

```markdown
### Context contribution: <task/run reference>:C1

- Kind: new context | correction | conflict | question | retrieval problem
- Finding and reuse: <bounded claim and why it matters beyond this task>
- Basis: observed | reported | working interpretation | unresolved
- Evidence: <source locator, author, version/commit, observation date,
  environment, exact qualified piece locator/hash and actual inspected scope>
- Affected context: <canonical destination or unknown; proposed versus confirmed>
- Limits and handling: <missing scope, conflicts and source restrictions>
- Origin: <ticket/Kata/run references, attributed contributor/role and date>
- Return route: <same task or explicitly authorized private destination;
  unavailable when none exists>
```

Keep the task/run reference and local `C1`, `C2` correlation keys stable across agents. Preserve original submitted bytes and link corrections as new submissions.

## Authorized maintenance handoff

1. Return the contribution block or save it to an authorized private destination.
2. Transfer it to Librarian maintenance, preserve the received artifact, and register it through the catalog writer. On retries, match correlation and bytes to return the existing receipt or record a linked revision.
3. Acknowledge with the receipt reference and `pending review`, then verify the underlying evidence, handling, and scope.
4. Save supported narrative in its canonical home and record a scoped review event when substantive review is performed. Apply independent review to new evidence mappings and disputed corrections.
5. Refresh selected retrieval when affected and return the disposition below (or record `return pending` in intake tracking if the return route is unavailable).

## Disposition

```markdown
### Context disposition: <task/run reference>:C1

- Receipt: <actual receipt and private intake reference>
- Outcome: pending review | incorporated | already covered | deferred | rejected
- Reason and scope: <what was accepted or not, and why>
- Destinations: <canonical anchors, existing answer or none; actual review refs>
- Remaining work: <missing evidence/decision and revisit trigger, or none>
- Recorded by/date: <attributed maintenance actor and actual date>
- Retrieval: refreshed selected context | not selected | refresh blocked |
  no retrieval change
```

Outcome meanings:
- `incorporated`: supported update saved and checked in the corpus
- `already covered`: linked to an existing answer
- `deferred`: retained as pending with a revisit trigger
- `rejected`: unsupported or out of scope for the corpus

