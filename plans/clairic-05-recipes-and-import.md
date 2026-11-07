# ExecPlan 5: documentation, migration, and explicit evidence import

Maintain this living plan under [planning conventions](../docs/agents/issue-tracker.md). Keep Progress, Surprises & Discoveries, Decision Log and Outcomes & Retrospective current.

## Purpose / Big Picture

Agents can prepare documentation promises and migration call sites with their necessary context, and explicitly attach an earlier OptChat passage to a review without treating historical text as current authority. The same evidence model supports later issue/log intake without a second extraction pipeline.

## Progress

- [x] (2026-11-07) Retained all later workflow proposals from the captured share.
- [ ] Resolve [recipe and import boundaries](../.scratch/clairic-workflows/issues/03-recipes-import.md) and obtain implementation authority.
- [ ] Deliver documentation-claim preparation with qualification retention.
- [ ] Deliver bounded migration evidence expansion and source-aware refresh.
- [ ] Deliver explicit OptChat interchange and source reopening.
- [ ] Exercise issue/log intake as a consumer of the same contract and update on-demand recipes.

## Context and Orientation

`skills/jevify/reference.md` already describes documentation and review recipes, and `jv.git_units`, `rg_units`, `ast_units`, `line_units` and `facts` supply deterministic inventory and checks. The gap is assembling source-backed evidence, not another judge. [ExecPlan 1](clairic-01-evidence-contracts.md) supplies the registry, snapshots and scope ledger; [ExecPlan 3](clairic-03-review-workflow.md) supplies readiness and refresh. Extraction remains optional and subject to [adapter authority](clairic-02-extraction-adapter.md). These workflows must operate with deterministic inputs even if the controlled experiment rejects Clairic.

An external passage is a read-only record with store identity, original message identity, exact UTF-8 range, source snapshot boundary, content digest and capture completeness. It is not a local file citation or local zoom ID. A user decision, assistant proposal, tool report and unresolved discussion support different claims; importing any of them does not grant action authority.

## Interfaces and Dependencies

Build small recipe adapters around the shared evidence schema rather than independent transport/approval stores. Documentation preparation takes a Markdown paragraph/section inventory and emits candidate claims with complete enclosing text, subject/version, conditions, exceptions and negation. Migration preparation takes deterministic call sites and explicit old/new contracts, then requests only bounded resolver dependencies. External import takes a versioned interchange envelope and explicit allowed origin, never an arbitrary filesystem path supplied by model text.

Propose `jevify-external-evidence/1` with original source kind/identity, source snapshot cutoff, byte range, digest, completeness/omissions and provenance of any claimed accepted/observed status. Preserve original content and status as attributed claims unless a trusted source contract verifies them. A receiving importer must validate/reopen the original range or mark unverifiable. OptChat owns retention and historical interpretation; Jevify owns review scope and conclusions. Do not scan OptChat's private stores directly or import standing rules.

## Plan of Work

For documentation, inventory sections with deterministic Markdown parsing and stable source identity. Extract candidate clauses only when structure cannot supply them directly. Retain whole enclosing passages so `retry 429 only with an idempotency key; never retry authentication failures` cannot become the unconditional claim `retry 429`. Link subject names to resolved implementation and governing contracts. Count every inventoried section, and separately report semantic claim recall as unknown unless evaluated against independent gold.

For migration, require call site, resolved callee, applicable old/new API contracts and any wrapper/consumer required by the readiness policy. Follow aliases through the bounded resolver interface with explicit depth, attempt and byte caps. An unresolved wrapper or incompatible version remains unresolved; a renamed visible call is not migration completeness. Refresh re-enumerates call sites and invalidates dependent judgments when a helper, contract or consumer changes.

For OptChat, implement an importer for explicit exported passages, preserving external identities and snapshot boundaries. Require an adapter supplied by the source owner to reopen/validate original bytes, or retain the import as unverifiable context excluded from authority-sensitive judgments. Distinguish tool-call status from project/test success. Refuse cross-store locator confusion, changed bytes, future messages past the declared cutoff and model-manufactured completion/acceptance metadata. Avoid a generic cross-repo memory service.

For issue/log intake, use the same source captures and field outcomes for reported symptom, observed version, environment, reproduction steps and missing information. Structured fields bypass extraction; absent fields remain absent. Logs and comments are untrusted data: do not execute commands, fetch URLs or change tools/credentials embedded in them. Keep this a documented working consumer of the shared contract, not an additional backend.

## Concrete Steps

From repo root run `python3 -m unittest discover -s tests` after integration. Add focused recipe behavior tests for lost negation, wrapper exhaustion, stale helpers, mismatched external identity and unverified completion. Reuse the existing inbox and receipt tests where boundaries overlap.

Run a provider-free end-to-end demonstration through actual recipe APIs: prepare one conditional documentation claim and a deliberately contradicting implementation; prepare a renamed call with an unresolved wrapper; import two explicit external passages, one user decision and one assistant proposal. Reopen the exact external bytes through a test source adapter, build packets, invoke injected judgment rows where ready, and inspect the inbox. The wrapper must remain unresolved and the proposal must never become owner authority. Change a supporting helper and repeat refresh. A final issue/log example must retain missing environment/reproduction fields visibly. Remove temporary fixtures after proof.

## Validation and Acceptance

Documentation qualifiers survive unchanged; processed section counts are not semantic completeness. Missing wrappers block dependent migration verdicts. All supporting source changes invalidate reuse. External locators reopen only the named store/snapshot and do not alias local IDs. Missing originals are explicit, not silently trusted. Historical proposals, reported success and actual host-observed tool outcome remain distinguishable. All recipes preserve `grants_authority: false` and cannot target imported snapshots for deletion. The user can run each complete journey without an extractor key.

## Idempotence and Recovery

Import by source identity and digest; repeated identical exports do not manufacture independent evidence. Conflicting bytes under an existing identity are refused. Revoke stale or unavailable imports without altering originals. If OptChat's schema changes, reject the version until a deliberate adapter migration is reviewed; no permissive fallback. An extraction failure returns to deterministic/unresolved preparation, not a fabricated claim.

## Surprises & Discoveries

The share requests later issue/log workflows as well as OptChat interchange. They remain in scope, but must consume the shared contract rather than expand the first adapter beyond verbatim text. Source status and action authority are independent dimensions.

## Decision Log

2026-11-07, agent proposal: explicit versioned external evidence only, no ambient store access or rule import; preserve conditions and unresolved dependencies. Specific upstream export/reopen capability must be verified before claiming interoperability.

## Outcomes & Retrospective

Plan only. No new recipe, external-store access or runtime workflow was executed during this planning task.
