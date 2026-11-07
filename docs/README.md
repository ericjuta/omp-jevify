# Clairic evidence-preparation planning

Planning checkpoint: 2026-11-07. Runtime baseline: omp-jevify v0.4.0 (`9fe7062`). These documents propose implementation; they do not report it as shipped.

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

The user requested reference capture, Wayfinder maps, and ExecPlans. No runtime implementation, paid experiment, credential access, commit, push, installation, or release is authorized by these documents. OptChat's prior approvals and spending caps are precedent only, not permissions for this repo. Engineering defaults are proposals until their decision tickets are resolved; owner decisions remain open. Keep each plan's Progress and Decision Log current.

No quality, savings, or review-time improvement is established by this planning work. No new model requests were made to Clairic or Jev for evaluation.

## Documentation verification

The two captured turns match the browser extraction verbatim (23,007 characters of conversation). All 66 relative links across the 17 planning/reference documents resolve; the six-ticket blocker graph is acyclic, and all five ExecPlans contain the required living-plan sections. `git diff --check` passed. These are documentation checks, not runtime feature tests.
