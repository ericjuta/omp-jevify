# Define review readiness and refresh semantics

Type: wayfinder:research
Status: resolved
Owner: Main
Blocked by: [Freeze evidence identity and source ownership](../../clairic-evidence/issues/01-evidence-contract.md)

## Question

What evidence makes a finding ready for each existing review question, and what dependency changes require re-preparation, rejudgment, or source re-review?

## Scope and proposal

Preserve finding categories valid_actionable, valid_nit, invalid, duplicate and the separate introduced_by_diff question for the first controlled comparison. Missing current/base/contract context is unresolved preparation, not invalid. Duplicate needs a resolved related finding. Bound resolver lookups by declared roots, depth, byte count and attempts. Re-enumerate scope on refresh. Record question-level dependencies and preserve old receipts; one answered facet never resolves an entire required question set.

## Resolution evidence

Implemented question-specific readiness, explicit dependency refresh and existing-inbox integration in [workflows.py](../../../skills/jevify/workflows.py) and [ExecPlan 3](../../../plans/clairic-03-review-workflow.md). Missing base/wrapper/related evidence cannot enter its dependent judgment. Required facets and frozen source spans constrain review scope; unrelated drift cannot clear prior prerequisite blocks. Runtime smoke confirmed source-backed review then revoked approval after contract-only drift. Confirmation still reports grants_authority=false.
