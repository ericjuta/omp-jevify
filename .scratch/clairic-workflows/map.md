# Map: source-backed review workflows and measured adoption

Type: wayfinder:map
Status: implemented; live measurement separately blocked

## Destination

Make review-finding intake the first end-to-end workflow, then documentation/migration and explicit OptChat evidence import. Establish whether Clairic improves over the identical deterministic preparation baseline; retaining deterministic-only behavior is a valid outcome.

## Decisions so far

The owner subsequently authorized implementation and shipping of all proposals, with Clairic default-on and an explicit opt-out. Paid experiments still require exact-manifest and cap approval. The [source proposal](../../docs/references/clairic-feature-spec-2026-11-07.md) records design intent; the [repository assessment](../../docs/research/clairic-evidence-preparation.md) records the pre-implementation baseline, not current release proof.

## Frontier query

Use the open/unclaimed/unblocked ticket rule in [tracker conventions](../../docs/agents/issue-tracker.md). The [evidence map](../clairic-evidence/map.md) owns source and provider prerequisites.

## Implementation boundaries

Documentation, migration and inert issue/log inputs consume the shared registry. External history uses `jevify-external-evidence/1` with explicit allowlisted origins and source-owner reopening; no ambient OptChat traversal or invented upstream exporter is provided. Human review-time protocol and sufficient independently adjudicated cases still must be frozen before any benefit claim. All recipe implementations are included; live measurement remains separate.

## Out of scope

Automatically fixing findings, treating imported conversation text as current authority, silently repairing evaluated answers, inferring missing claim IDs, or claiming semantic completeness from processed section counts.

## Notes

Children: [Define review readiness and refresh semantics](issues/01-review-readiness.md), [Freeze a fair preparation-treatment experiment](issues/02-treatment-evaluation.md), [Bind recipe scope and external evidence authority](issues/03-recipes-import.md).

Implementation: [review intake](../../plans/clairic-03-review-workflow.md), [controlled evaluation](../../plans/clairic-04-controlled-evaluation.md), [recipes and import](../../plans/clairic-05-recipes-and-import.md). The linked ExecPlans include future runtime acceptance; this chart is not evidence that any feature runs.
