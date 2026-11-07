# Clairic evidence preparation: assessment and design boundaries

Date: 2026-11-07. Jevify source inspected at `9fe70623875f6ced578448aab79358e388ef1cbc` (v0.4.0); sibling OptChat checkout at `a45bb300ca1a74d57994af0d4e54741e9659e25d`. Source inspection is not runtime proof. No Clairic/Jev evaluation or implementation was performed for this planning task.

## Reference and intent

The [captured conversation](../references/clairic-feature-spec-2026-11-07.md) proposes improving Jevify's original method-first review purpose by preparing useful evidence from messy prose. Preserve the division: deterministic tools establish inventory/identity/facts; Clairic locates candidate occurrences; existing judgment evaluates suitable independent questions; direct review resolves dependent reasoning and confirms source. Do not turn optional extraction into a requirement for every task.

Both visible turns were captured from the rendered page, including the full visible proposal. The linked `omp-jevify-clairic-feature-spec.md` points to a ChatGPT library login surface and was not captured. Several source links render with empty hrefs. Therefore the saved conversation is not claimed to include the separate attachment or inaccessible citations. The plan rests on visible content plus inspected code and provider documentation.

## What is present and what is missing

| Surface | Source-read behavior | Required improvement |
| --- | --- | --- |
| `skills/jevify/jevify.py:546–664` | `extraction`/`validate_extraction` bind units, files, states, grouping and independent hits. Revision is metadata over current files. | Explicit preparation dependencies, model observations and actual source-version resolution; keep coverage hits distinct. |
| `skills/jevify/jevify.py:1326–1408` | `facts` attaches deterministic results; `requires` warns about missing fields but leaves states eligible. | Separate required-evidence admission; do not describe warning-only checks as an existing readiness gate. |
| `skills/jevify/jevify.py:746–1149` | Durable locked inbox, bound model rows, atomic adjudication, source/rubric/policy and decision-citation drift. Reviewer identity is self-declared. | Extend existing drift to preparation instructions, mappings and all supporting dependencies; do not build a second inbox. |
| `skills/jevify/jevify.py:4059–4126` | `delete_spans` defaults dry-run, but guards are optional; writes are sequential. | New imported comments/snapshots must never be deletion targets; do not claim existing deletion is universally guarded or transactional. |
| `evals/findings.py:83–286` | Frozen HEAD/BASE trees, exact IDs and complete LF-joined line quotes; no semantic-support proof. | Keep sub-line code-point occurrences separate from complete-line review citations; no model-assisted repair of evaluated answers. |
| `evals/run.py:1348–1691`, `evals/grade.py:735–943` | Frozen identity, gold/exposure, independent-case and forward/reverse method gates; strategy is the treatment. | Separate preparation-treatment comparison with B/C held at the same strategy; preserve old report semantics. |
| `evals/run.py:72–77`, `package.json:16–17` | Eval hosts exclude extensions; package ships skills, README and LICENSE with empty OMP extension metadata. | Explicitly admit only the candidate extraction extension in isolated evaluation; verify installed packaging and credential bridge. |

Existing review categories and documentation recipes live at `skills/jevify/reference.md:936–1017`. First review comparison preserves its rubric. Missing evidence belongs to preparation outcomes, never the `invalid` category. An unresolved duplicate requires the related finding; an introduced-by-diff answer requires explicit current/base sources.

## Current OptChat precedent, not transferred authority

The relevant historical session was consulted through a read-only session agent; current source corroborates the important direction. OptChat's [evidence plan](https://github.com/ericjuta/omp-optchat/blob/a45bb300ca1a74d57994af0d4e54741e9659e25d/plans/evidence-layer.md) explicitly supersedes the host-extraction-role proposal with plugin-only integration. Its [adapter](https://github.com/ericjuta/omp-optchat/blob/a45bb300ca1a74d57994af0d4e54741e9659e25d/src/system1/extract.ts) uses bounded fetch, exact echo/span checks and unverified occurrence rendering. Its host integration resolves the OpenRouter credential at call time; the plugin does not read environment variables itself. Host credential cascade behavior is a policy question, not proof of credential-store-only sourcing.

Reuse its deadline (including credentials), streamed body cap, null handling, cancellation and redaction lessons. Do not import its narrow chat-message representation wholesale. Jevify needs multi-role repository evidence and a Python/extension artifact bridge.

OptChat records B/C ties on synthetic grounded outcomes and a small real-chat wrong-version selection. Those reports do not establish incremental Clairic quality for Jevify. Its default-on extraction, prior paid caps, releases and later delegated authority remain OptChat-specific. The older managed guidance that required an upstream extraction role is stale relative to the plugin-only decision. No upstream host work is part of this plan.

## Provider contract checked for this plan

[OpenRouter extraction documentation](https://openrouter.ai/docs/guides/community/extractions) describes `POST /api/alpha/extractions`, `cloudflare/clairic`, `state`, named typed `fields`, and `provider.options.cloudflare.return_state_text`. It explicitly defines half-open Unicode code-point offsets, `value: null` above a field's null threshold, a separate plausible `guess`, `truncated`, and usage passes/cost. The route is alpha; revalidate its wire contract before implementation. No live compatibility call was made.

First scope is verbatim text, not derived numbers, lists or images. Non-null exact occurrence plus subject checks is candidate admission, not truth. Unknown or zero-reported cost without corroboration remains unknown; provider passes, request attempts, answered fields and completed original records are distinct counts.

## Proposed implementation sequence

1. [Source registry and snapshots](../../plans/clairic-01-evidence-contracts.md): exact bytes, immutable captures, live origins, role-labeled dependencies, complete original-record/question accounting.
2. [Optional adapter](../../plans/clairic-02-extraction-adapter.md): agent-mediated Python prepare → OMP tool → Python adopt; explicit caps and safe installed packaging.
3. [Review workflow](../../plans/clairic-03-review-workflow.md): structured-input bypass, deterministic reference resolution, readiness, existing judge/inbox, dependency-aware refresh.
4. [Controlled evaluation](../../plans/clairic-04-controlled-evaluation.md): A existing, B deterministic preparation, C identical B plus extraction, D direct review; separate versioned treatment contract and preregistration.
5. [Recipes/import](../../plans/clairic-05-recipes-and-import.md): qualified documentation claims, migration dependency expansion, explicit OptChat interchange and issue/log consumers.

## Trust and measurement requirements

Source text is untrusted data, never tool/config authority. Bind artifact roots and digests before network access; no model-selected paths, URLs, credentials or endpoint overrides. Keep original content and rejected/failed/ambiguous outcomes. Re-enumerate inventory on refresh; semantic claim recall remains uncertain even with complete processing counts. Snapshot immutability alone cannot hide live-origin drift.

Imported sources are not deletion targets. Review confirmation retains `grants_authority: false`. Existing deletion concerns discovered during source review are not silently expanded into a general deletion rewrite; protect the new workflow explicitly and scope unrelated repairs separately.

The controlled experiment must distinguish software proof, synthetic model measurements and realistic task evidence. Freeze gold, primary metrics, minimum useful gain, safety bounds, independence and null denominators before running. Budget enforcement requires admission/attempt accounting; uncertain in-flight billing is not a hard invoice cap. Actual review-time reduction requires measured human time, not model latency or a smaller flag list.

## Planning status

Two [local Wayfinder maps](../README.md) contain six open decision tickets. No GitHub issues were created because this repo had no tracker convention; the documented local fallback is canonical for this effort. No owner policy question was silently resolved. Five ExecPlans are written; none is implemented. Runtime v0.4.0 and the installed plugin remain unchanged.
