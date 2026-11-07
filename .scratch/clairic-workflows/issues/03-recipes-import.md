# Bind recipe scope and external evidence authority

Type: wayfinder:research
Status: resolved
Owner: Main
Blocked by: [Define review readiness and refresh semantics](01-review-readiness.md)

## Question

How can documentation clauses, migration dependencies and OptChat passages share preparation machinery without claiming semantic completeness or importing historical authority?

## Scope

Preserve whole enclosing passages, conditions, negation, versions and exceptions. An unresolved wrapper blocks migration completeness. OptChat imports must carry store/message/byte/snapshot identities and completeness, distinguish user decisions, assistant proposals, tool reports and unresolved history, and verify original-source reopening. No arbitrary store traversal or standing-rule import. Define minimal issue/log intake as later consumers of the same contract, without new provider behavior.

## Resolution evidence

Implemented `jevify-external-evidence/1` with explicit allowlisted origins, exact byte/source identity and injected source-owner reopening; never ambient store traversal. [ExecPlan 5](../../../plans/clairic-05-recipes-and-import.md) records runtime checks for qualification retention, missing-wrapper refusal and verified fixture occurrences without authority transfer. Issue/log inputs remain inert data. Unavailable originals remain unverifiable; this does not claim a newly shipped upstream OptChat exporter.
