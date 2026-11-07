# Map: source-bound evidence preparation for Jevify

Type: wayfinder:map
Status: implemented; provider-free release checks passed (see docs index)

## Destination

Define an implementable optional Clairic preparation stage that turns prose into source-attributed candidate fields without weakening Jevify's deterministic facts, exact-ID grading, source review, or action authority. Deterministic-only preparation remains useful.

## Decisions so far

The owner authorized implementation and shipping on 2026-11-07. See [planning index](../../docs/README.md). The superseding [provider decision](issues/03-provider-authority.md) makes Clairic default-on for selected inputs, with explicit opt-out and deterministic fallback. Selected private source is allowed, secrets are excluded, and credentials use the host resolver including its environment fallback. Live experiments still require exact-manifest and cap approval.

## Frontier query

Inspect the child [decision tickets](issues/). Select open, unclaimed tickets with all linked blockers resolved, per [tracker rules](../../docs/agents/issue-tracker.md). All three evidence-map decisions are resolved; their tickets record implementation and provider-free proof.

## Implementation boundaries

Packaged helper loading, SDK registration and interrupted-request handling are implemented and exercised without a provider. Transport is Linux-only, uses host credentials, and preserves immutable attempt history. No host fork was created. Numeric/list/image extraction remains out of scope; this implementation admits verbatim text only. Real-provider measurement still requires separate approval.

## Out of scope

Upstream OMP extraction roles, provider SDKs, automatic source edits, and changes to exact-ID grader credit. No generic cross-repo memory service. Release and installation are now owner-authorized shipping steps, not evidence of feature completion.

## Notes

Children: [Freeze evidence identity and source ownership](issues/01-evidence-contract.md), [Prove the installed Python/extension artifact bridge](issues/02-artifact-bridge.md), [Approve provider transmission and experiment authority](issues/03-provider-authority.md).

Implementation plans: [contracts](../../plans/clairic-01-evidence-contracts.md), [adapter](../../plans/clairic-02-extraction-adapter.md). This map supplies prerequisites to the [workflow map](../clairic-workflows/map.md). Source: [captured proposal](../../docs/references/clairic-feature-spec-2026-11-07.md).
