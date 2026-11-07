# ExecPlan 3: source-backed review intake and refresh

Maintain this living plan under [planning conventions](../docs/agents/issue-tracker.md), including Progress, Surprises & Discoveries, Decision Log and Outcomes & Retrospective.

## Purpose / Big Picture

Given review comments, an agent obtains source-backed cases showing what was claimed, what current/base code and the contract establish, what remains missing, and which results a reviewer confirmed. The workflow must complete deterministically without Clairic when structured inputs already provide the fields. Missing evidence is not an invalid finding.

## Progress

- [x] (2026-11-07) Identified the existing review inbox, facts and source-binding contracts.
- [ ] Resolve [readiness and refresh](../.scratch/clairic-workflows/issues/01-review-readiness.md) and obtain implementation authority.
- [ ] Implement intake, bounded resolution, question-specific readiness and source-backed state rendering.
- [ ] Connect existing run/adopt/inbox APIs and dependency refresh.
- [ ] Demonstrate the full user workflow with deterministic and injected extraction paths.

## Context and Orientation

`skills/jevify/jevify.py` contains deterministic `jv.facts`, extraction manifests, request planning, judgment, adoption, persisted review and receipts. `skills/jevify/reference.md` contains review-triage and documentation-alignment recipes. The first release preserves the existing triage categories `valid_actionable`, `valid_nit`, `invalid`, `duplicate` and separate `introduced_by_diff` question. This workflow is not the schema-2 evaluation output format in `evals/findings.py`; do not reconstruct missing evaluated claim IDs or rewrite answers to award credit.

The [source registry plan](clairic-01-evidence-contracts.md) supplies immutable captures, live origins and dependency identities. The [adapter](clairic-02-extraction-adapter.md) is optional. A packet is the original comment plus role-labeled current code, base code when required, governing contract, qualifications and any missing dependency. Readiness is a deterministic prerequisite check for a specific question, not a confidence score from an extractor.

Existing `jv.facts(requires=...)` only warns and leaves incomplete states eligible. Add a real evidence-readiness gate for this workflow; do not reuse warning output as admission proof.

## Interfaces and Dependencies

Propose `jv.prepare_review(records, *, sources, resolver, limits)` using the evidence contract, and render judge states through `jv.evidence_states(evidence, Q)`. The resolver is an injected agent/tool boundary: it accepts declared reference hints and returns exact source identity/ranges or an unresolved reason. It must not grant the Python library ambient access to arbitrary files or LSP services. Resolve paths against declared roots and symbols through available AST/LSP facilities; references from model text are hints only.

Define readiness outcomes ready, missing_evidence, conflicting_evidence, version_mismatch and expansion_exhausted. Keep the unresolved reason and missing roles. Require current behavior and governing requirement for actionable judgments; introduced_by_diff requires both admitted base and current revisions; duplicate requires a resolved related finding. Base choice must be explicit, not assumed HEAD~1 for an arbitrary PR. Freeze budgets for source bytes, lookup count and expansion depth; exhaustion keeps the record visible.

Track facts, model observations, judgment and reviewer decision separately per question. Record all required question dispositions. Extend source-bound review/receipt views rather than making a second approval store. Review summary retains `grants_authority: false`.

## Plan of Work

Import comments with stable IDs and raw captures. Parse existing JSON fields without paying for extraction; use optional extraction only for prose fields. Preserve full enclosing context, exceptions, negation and versions rather than truncating to a convenient clause. Resolve cited references deterministically and gather explicit current/base/contract dependencies. If a reference is ambiguous, display alternatives and mark unresolved; do not choose the first matching symbol.

Build a packet and compute question readiness before invoking any judge. Render original source and candidate status visibly so an extracted assertion is not presented as an established fact. Call existing `jv.plan`, then `jv.run` or `jv.adopt` on admitted questions. Feed bound rows and source dependencies into `jv.review_queue`; source review may confirm, overturn or leave unresolved. A missing question remains in the scope ledger even if another facet received a confident answer.

Implement dependency-aware refresh with current inventory re-enumeration. Reuse a judgment only if its original record, all consulted source identities, field instructions, null policy, readiness rules, rendered state and rubric match. Reuse by equal extracted string alone is forbidden. Changed supporting evidence invalidates reviewer confirmation and dependent results. Unaffected records may be reused with explicit receipts; new/removed records are accounted for. Never mutate earlier receipts or silently preserve stale approvals.

Update concise skill guidance and on-demand examples only after actual workflow proof. Document unresolved preparation separately from invalid and distinguish source confirmation from permission to edit.

## Concrete Steps

From repository root, inspect all callers of the review/facts APIs before changing contracts. Run `python3 -m unittest discover -s tests` after integration. Extend inbox tests for missing-question and supporting-source drift; add readiness regressions for missing base, unresolved wrappers, duplicate without a related finding, and same-name symbols in different versions.

Run the actual helper in a disposable fixture with four comments: structured actionable, prose with retained negation, introduced finding missing base, and duplicate missing its target. Prepare deterministic inputs, inject an extraction result only for the prose record, render states, inject settled judge rows through the supported seam, create the inbox and source-confirm one ready item. Reopen in a fresh process after changing only a contract. The two missing-evidence cases must never have reached judgment; the changed-contract approval must be stale. Report that injected extraction/judgment demonstrates software behavior only. A live workflow requires separate approval.

## Validation and Acceptance

All original comments remain visible. Structured JSON does not call Clairic. Wrong or unresolved references prevent dependent judgments. Missing evidence never becomes `invalid`, and a missing base cannot produce an introduced verdict. Negation survives in the packet. Every verdict can be traced to source roles and the exact frozen rubric. Changed dependencies and new/removed records invalidate the appropriate reuse. Review confirmation grants no edit authority, and snapshot/comment sources cannot become deletion targets.

## Idempotence and Recovery

Use immutable stage artifacts and the existing locked review ledger. Resume verifies source, plan and question identities before reuse. Failures retain unresolved dispositions and original comments. A failed resolver or extractor does not cause an implicit judge call over partial evidence. Rebuild changed packets into a new plan; retain earlier receipts as historical evidence.

## Surprises & Discoveries

The existing inbox is already durable and source-bound. The missing capability is assembling explicit supporting evidence and readiness, not a replacement review database. A single original record may be ready for one question but unresolved for another.

## Decision Log

2026-11-07, agent proposal: preserve the existing triage rubric for the first comparison, keep unresolved preparation outside its invalid category, and inject resolution through an explicit bounded boundary. Detailed required-role policy remains in the readiness ticket.

## Outcomes & Retrospective

Plan only. No review intake implementation or live judgment ran during this documentation task.
