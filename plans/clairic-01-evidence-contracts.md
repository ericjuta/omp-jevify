# ExecPlan 1: source registry, immutable captures, and scope accounting

This is a living implementation plan, not an implemented feature. Update Progress, Surprises & Discoveries, Decision Log, and Outcomes & Retrospective after each milestone. Follow [planning conventions](../docs/agents/issue-tracker.md).

## Purpose / Big Picture

An agent can import review comments and their supporting code once, retain exact citations, and account for every record even when extraction fails. The same records work with deterministic preparation alone or optional Clairic extraction. A source occurrence proves that text exists, not that a review finding is true.

## Progress

- [x] (2026-11-07) Captured the visible proposal and mapped the v0.4.0 contracts.
- [ ] Resolve the [evidence contract decision](../.scratch/clairic-evidence/issues/01-evidence-contract.md) and obtain implementation authority.
- [ ] Implement source registry, immutable capture, bounded occurrence validation, and scope ledger.
- [ ] Integrate the registry with existing review invalidation and prove the installed helper path.

## Context and Orientation

`skills/jevify/jevify.py` is the currently shipped Python helper. `jv.extraction` binds declared units and independent source hits; `jv.validate_extraction` checks drift; `jv.review_queue` persists review state and `jv.adjudicate` records self-declared reviewers. `jv.delete_spans` supports optional manifest/review guards; it is not universally guarded or a transactional multi-file writer. The new workflow must require source confirmation and separate action authority and must exclude imported snapshots from deletion. `evals/findings.py` validates exact claim IDs, source revisions and quotes equal to complete LF-joined source lines. Do not change that grading contract to admit arbitrary extracted fragments. `package.json` currently ships the skill without an extension entry point.

A registry is a bounded list of explicitly consulted source objects, not another model-generated inventory. An occurrence is a checked span inside one of those objects. A dependency is a source or policy whose change makes a result stale. A scope ledger records one disposition for each declared original record and each required question; it must not drop failed/empty records.

## Interfaces and Dependencies

Propose a new `jevify-evidence/1` contract, separate from the existing extraction manifest. Source entries contain a deterministic ID, role, raw UTF-8 digest/byte length, immutable snapshot locator, origin locator, source revision or external snapshot identity, and read-only/deletion eligibility. Roles include original_comment, current_code, base_code, governing_contract, helper, consumer and external_history. Coverage hits remain coverage hits; do not use them as fake dependency entries.

Allocate record IDs before provider calls. Each record binds its original source, subject/version hints, required field names and required question IDs. A field result records candidate/missing/ambiguous/rejected/error, exact value and half-open code-point occurrence when admitted, enclosing full-line range, extraction-plan digest and unresolved reason. Keep model-derived values in `observations`, distinct from deterministic `facts`. A later deterministic comparison of a selected observation retains that dependency.

Proposed helper interfaces are `jv.prepare_evidence(records, fields, *, root, sources, limits)`, `jv.adopt_evidence(plan, result_path)` and `jv.evidence_states(evidence, Q)`. `prepare_evidence` makes no network request and returns artifact identity; adopt validates rather than repairs. The names are not available in v0.4.0. Freeze exact signatures with the decision ticket before changing callers.

## Plan of Work

First implement pure bounded schema/identity routines in a packaged Python evidence module under `skills/jevify/`, with minimal wrappers in `jevify.py`. Before committing to the module split, prove sibling module loading through the actual `%load skill://jevify/jevify.py` environment; use the bridge prototype if the host lacks a stable loader path. Do not rely on the user's current working directory. Avoid a wholesale helper refactor.

Capture imported text without changing whitespace or line endings in its raw digest. Derive line-addressable views with an explicit raw-byte mapping; preserve the complete original content and original record ID. Convert provider code-point offsets to UTF-8 offsets and separately to enclosing complete lines. Reject out-of-range, wrong-source, mismatched, wrong-subject and nonunique unlocated occurrences. Repeated strings require the supplied occurrence, not a first-match search. A null value remains missing regardless of guess/confidence.

Add explicit supporting sources and dependencies to new evidence-aware review records. Before approval, resume, or reuse, verify both immutable snapshot integrity and each live origin/revision under its declared refresh policy. A changed contract must invalidate its dependents even if primary code and extracted text are unchanged. Re-enumerate the declared inventory on refresh so additions and removals are visible. Preserve previous artifacts rather than rewriting historical decisions.

Imported comments and snapshot files are categorically non-deletable. Update every deletion admission path to reject these roles before opening a writable target. Keep ordinary source-only review behavior intact and reject unknown schema versions explicitly. Add narrow regression tests for stale dependencies, identity/line mapping, unresolved facets, new/removed records, and snapshot deletion refusal.

## Concrete Steps

From the repository root, inspect the functions above and all their callers before exported changes. Run `python3 -m unittest discover -s tests` after integration. Extend existing `tests/test_review_inbox.py` and `tests/test_findings.py` for behavioral regressions; add a dedicated evidence-contract suite for new state transitions, not copied constants.

Exercise the actual helper in a temporary directory: import two comments, materialize current/base/contract sources, prepare a plan, supply a deterministic candidate artifact, adopt it, build states and open the existing inbox. Change only the contract file and reopen in a fresh process. Capture status, counts and citations; then remove only the smoke's temporary files. This is a provider-free demonstration, not Clairic quality evidence.

## Validation and Acceptance

Every original ID remains accounted for, including a missing required field and a failed extraction. A single answered question does not resolve a record with another required facet unresolved. A quote containing an astral character maps to the exact bytes and complete cited lines. Repeated text cannot shift attribution. Changing a supporting contract makes the result stale; adding a source unit changes inventory coverage. Snapshot deletion is refused even when a reviewer confirmed the finding. Existing exact-ID grader rejects prose and wrong revision as before. A fresh installed helper can execute the flow without relying on checkout paths.

## Idempotence and Recovery

Plans and results use canonical digest identities and atomic publication. Repeating identical preparation yields the same identity; conflicting content at the same immutable path is refused. An interrupted write leaves no admitted partial result. Resume reads a frozen plan, never reinterprets it under changed policy. New source or policy requires a new plan and explicit refresh. Existing v0.4.0 bundles retain their documented interpretation; never synthesize dependencies they did not record.

## Surprises & Discoveries

The strict findings grader accepts complete lines, while Clairic documents sub-line code-point spans. Both representations are required; changing the grader would hide the mismatch rather than solve it. Supporting source dependencies are not equivalent to independent extraction coverage hits.

## Decision Log

2026-11-07, agent proposal: use a separate versioned evidence artifact and explicit source registry; retain current grading semantics. The user requested planning only. Artifact schema, loader and migration details remain gated by the evidence decision ticket.

## Outcomes & Retrospective

Plan only. No helper code changed, no runtime acceptance executed, and no new capability or quality gain is claimed.
