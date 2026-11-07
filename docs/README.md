# Clairic source-backed evidence workflows

Implementation checkpoint for v0.5.0: 2026-11-07. Baseline: omp-jevify v0.4.0 (`9fe7062`). All five feature plans are implemented and passed provider-free release checks. The captured reference and repository assessment preserve the pre-implementation design context, not evidence of model quality.

## Start here

- [Captured shared conversation](references/clairic-feature-spec-2026-11-07.md): both visible turns, including the complete visible proposal. The linked private-library Markdown attachment was inaccessible without login and is not included.
- [Repository assessment](research/clairic-evidence-preparation.md): current contracts, OptChat precedent, differences from the share, and evidence limits.
- [Evidence contracts and transport map](../.scratch/clairic-evidence/map.md): source identity, artifact bridge, and provider/privacy decisions.
- [Review workflows and adoption map](../.scratch/clairic-workflows/map.md): readiness, controlled comparison, and additional recipes.
- [Tracker conventions](agents/issue-tracker.md): local Markdown maps, explicit blockers, no GitHub issues created.

## Implementation ExecPlans

1. [Source registry, snapshots, and scope ledger](../plans/clairic-01-evidence-contracts.md).
2. [Optional plugin-side Clairic adapter](../plans/clairic-02-extraction-adapter.md).
3. [Review intake, readiness, and dependency refresh](../plans/clairic-03-review-workflow.md).
4. [Controlled preparation-treatment evaluation](../plans/clairic-04-controlled-evaluation.md).
5. [Documentation, migration, and external-evidence recipes](../plans/clairic-05-recipes-and-import.md).

The first complete user journey is review comments → source-backed cases → existing rubric → source-confirmed inbox. Deterministic preparation must work without Clairic. An extraction occurrence is not proof of the claim, a completed request is not a completed review, and reviewer confirmation never grants edit authority.

## Authority and status

The owner subsequently authorized implementation and shipping of all five proposals. Clairic defaults on for selected evidence inputs, with explicit opt-out and deterministic fallback. Selected public/private code and review text may be transmitted, excluding secrets, using host-resolved OpenRouter credentials (including the host's environment fallback); no plugin-owned key path is allowed. This supersedes the earlier opt-in policy. Live experiments still require separate approval of the exact manifest digest and spending cap. OptChat approvals do not transfer. Implementation and provider-free release checks are complete; publication and installation are separate from model-quality evidence.

No quality, savings, or review-time improvement is established. New verification is provider-free: source/readiness/inbox/recipe smokes, synthetic four-arm CLI scenarios, and a packaged fresh-process SDK/Python loopback bridge. No new Clairic or Jev provider requests were made. A real-provider wire smoke still needs manifest/cap approval; a controlled live comparison additionally needs independently adjudicated realistic gold.

## Release verification

- 575 Python tests, 29 adapter regressions, and TypeScript checking passed.
- Packaged fresh-process SDK/Python smoke passed: four controller captures, four real SDK observer lifecycle frames, five loopback requests, zero outbound provider requests.
- Initial fixed thinking, in-memory usage identity and once-only accounting were exercised through the generated observer and real SDK session/credential components without a provider.
- All four final CLI demonstrations passed; real-world recommendations remained `inconclusive`.
- Final focused static reviews cleared the reported transport and evidence/evaluation blockers. These reviews did not run provider calls or establish model quality.
- Package inventory contains only the 12 intended source/documentation files, excluding Python bytecode caches.

## Documentation verification

The two captured turns match the browser extraction verbatim (23,007 characters of conversation). All 64 current relative links across the 17 planning/reference documents resolve; the initial six-ticket blocker graph was checked as acyclic, and all five ExecPlans contain the required living-plan sections. `git diff --check` passed. These are documentation checks, not runtime feature tests.
