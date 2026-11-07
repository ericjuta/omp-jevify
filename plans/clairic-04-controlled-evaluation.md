# ExecPlan 4: isolate extraction benefit with a controlled experiment

This living plan follows [planning conventions](../docs/agents/issue-tracker.md). Update Progress, Surprises & Discoveries, Decision Log and Outcomes & Retrospective without rewriting historical outcomes.

## Purpose / Big Picture

Users can determine whether Clairic adds value over the exact same deterministic evidence preparation, rather than attributing better retrieval or structure to the model. Rejecting extraction or retaining direct reading is a valid result. No paid evaluation is authorized by this plan.

## Progress

- [x] (2026-11-07) Identified that existing pilot gates compare methods, not preparation treatments.
- [x] (2026-11-07) Implemented the preregistration contract, independent-case admission and fixed C-versus-B primary contrast.
- [x] (2026-11-07) Implemented `evals/preparation.py` freeze/preflight/run/report and four provider-free executable demonstration scenarios without changing old strategy gates.
- [ ] Freeze independently adjudicated cases, thresholds, artifacts and provider-free preflight.
- [ ] Obtain owner approval of the exact manifest digest and total spend cap before any live call.
- [ ] Execute the approved run, adjudicate outcomes and record adoption/withholding without post-hoc threshold changes.

## Context and Orientation

`evals/run.py` validates frozen identities, pilot manifests, source fixtures, held-out exposure and operator gold attestations. `evals/grade.py` compares graded runs and withholds method recommendations on comparability or evidence failure; duplicate fixture/key aliases do not create independent cases. `evals/findings.py` deterministically validates exact IDs, revisions and full-line quotes. `evals/grade_launch.py` binds grading execution. Reuse these identity and admission patterns, but do not pretend a fixed-method preparation treatment is a change of strategy.

Earlier structural smoke results and the existing synthetic fixture do not establish Clairic benefit. OptChat recorded deterministic retrieval and extraction ties on synthetic grounded outcomes plus a real wrong-version selection; those are precedent for challenge cases, not Jevify measurements. Exposed historical holdouts cannot be relabeled fresh.

## Interfaces and Dependencies

Add a separate schema, proposed `jevify-preparation-pilot/1`, and corresponding report identity. Retain the current `jevify-pilot/1` method contract unchanged. Bind implementation and grader digests, source snapshot/inventory, retrieval passages, field instructions/null thresholds, rubric, judge configuration and observed model where available, fallback policy, completion policy, randomization/repetition, independent gold identity and exposure history. Record requested and observed provenance separately; unknowns cannot establish comparability.

Four arms are A current workflow, B deterministic evidence preparation, C B plus Clairic, and D direct review for suitable tasks. The incremental extraction conclusion is C versus B only. B and C have identical inventory, retrieved passages, rubric, judge configuration, fallback and completion rules. Changing retrieval or rubric requires a new comparison, not an unexplained improvement attributed to C. A/B and D comparisons are secondary and separately labeled.

Freeze a primary benefit metric, minimum useful improvement, quality noninferiority bound, unsafe-recommendation limit, interval/confidence method, minimum independent cases and repeated-run design before results. Use independently source-adjudicated gold with adjudicator/evidence identities; operator attestations are not cryptographic authentication of truth. Include missing/ambiguous cases and per-record required-question denominators. Model self-grading cannot establish ground truth.

## Plan of Work

Implement treatment validation adjacent to current pilot loading and comparison code; do not add an escape hatch to the old strategy gate. Reject mismatched source inventories, B/C retrieval differences, unbound instructions, missing required outcomes, incomparable models or unknown cost when the chosen benefit criterion requires cost. Keep descriptive outcomes separate from adoption recommendations.

The current runner launches hosts with `--no-extensions`. Admit only the candidate extraction extension explicitly in isolated runs, bind its code identity, and account for its direct extraction receipts separately from host judgment/completion usage. Keep unrelated installed extensions excluded, and prove the installed candidate path rather than accidentally evaluating the extension-disabled baseline twice.

Construct a public synthetic adversarial set and an independently adjudicated realistic set with documented provenance and exposure. Include wrong neighboring claim IDs, reused symbols, wrong versions, conditions, exceptions, negation, repeated quotes, Unicode, missing base/contract, wrapper dependencies, stale sources, null-with-plausible-guess, truncation and interrupted extraction. Distinguish processed-section coverage from semantic missing-claim recall measured against gold.

Measure occurrence accuracy, subject attribution, qualification retention, missing claims, abstention, final decision accuracy, unsafe recommendations, unresolved rate, reviewer overturns, latency and observed total cost across preparation, extraction, judgment and retries. Measure human preparation/review time with an agreed blinded or counterbalanced protocol before claiming saved labor. Report input tokens, attempts and provider passes as different quantities. Missing/zero-reported cost remains unknown without corroboration; do not claim free inference.

Generate and hash the preregistration manifest only after the runner, cases and thresholds are fixed. Provider-free preflight proves fixture admission, identities, denominators and withholding. Obtain owner approval of the digest and cap; the approved run must verify all bound files before transmitting. Stop admission at the approved attempt/allocation cap or incomplete evidence and report unresolved results. This does not guarantee a hard invoice cap: retries, in-flight attempts and missing provider usage may leave actual billing unknown. Freeze conservative per-attempt reservations and how uncertainty consumes the approved budget; require the owner to accept any best-effort rather than hard-dollar policy explicitly. Preserve failed and aborted attempts in the ledger.

## Concrete Steps

From repository root run `python3 -m unittest discover -s tests` after integration. Extend `tests/test_pilot_reports.py`, `tests/test_evals.py` and runner coverage with actual mismatched-comparison and alias scenarios. Establish the exact new runner options in `evals/run.py --help` and document runnable invocations in this plan when implemented; no currently nonexistent CLI is asserted here.

Exercise the real new runner/report path without a network provider using four frozen fixture outcomes: C improves under all frozen gates, C worsens, C ties B, and C has missing cost/provenance. Observe that only the admitted beneficial scenario recommends C, ties keep the baseline or remain inconclusive per preregistration, and incomplete comparisons cannot recommend adoption. Validate the output artifacts against their bound digests. Then, and only after explicit approval, execute the live manifest once under its declared repeat and stop policy.

## Validation and Acceptance

Changing one B/C retrieved passage makes the extraction comparison inadmissible. Duplicate case aliases do not increase the independent denominator. Good source quotations cannot repair wrong claim IDs or prove label semantics. Correct null abstentions are scored under an explicitly frozen denominator, not retrospectively counted as span failures or successes. A worse C result does not automatically recommend B without B meeting its own requirements. Reports distinguish software/injected evidence, synthetic model measurements and real-world measurements. The outcome can be adopt, keep deterministic, direct-review preferred, or inconclusive; no evidence is fabricated to force adoption.

## Idempotence and Recovery

Freeze immutable manifests and per-attempt receipts; do not overwrite a completed experiment with a rerun. Resume only from a bound attempt ledger and count uncertain spend conservatively against the authorization. Changing scorer, cases, treatment or thresholds creates a new preregistration and approval, with the prior result retained. Offline tests never read sealed real holdouts implicitly.

## Surprises & Discoveries

The existing strategy gate deliberately rejects many comparisons that look superficially similar. The new treatment comparison must preserve that safety rather than loosen it. A source-matching occurrence can still be semantically wrong for the requested subject/version.

Final review identified that model-writable session journals cannot attest judge identity or costs, requested effort is not observed effort, and irrelevant source padding cannot make a repeated gold key independent. Completion receipts must remain readable during interrupted shutdown. Release acceptance includes regressions at these boundaries, rather than relying on successful synthetic reports alone.

## Decision Log

2026-11-07, superseding owner decision: Clairic defaults on for selected public/private non-secret source, with explicit opt-out and deterministic fallback, using host-resolved credentials including host environment-key fallback. Implementation and shipping are authorized. Exact manifest digest and cap approval remain mandatory before live experiments.

2026-11-07, agent proposal: separate treatment schema; C/B is the extraction attribution comparison; no transfer of OptChat's budgets or adoption decisions. Exact statistical policy, independent case counts and the paid cap remain unresolved until preregistration and owner approval.

## Outcomes & Retrospective

The executable controlled runner is implemented. The final repaired Python suite passed 575 tests, and the focused static rereview cleared all six remaining release blockers. Four final CLI demonstrations exited successfully: simulated improvement selected extraction, worsening/tie selected deterministic preparation, and missing results withheld the software gate. All four top-level real-world recommendations remained `inconclusive`. These were injected synthetic software checks, not experiments. Independent realistic gold and owner manifest/cap approval remain prerequisites for live measurement. No cost, quality, review-burden or savings improvement is claimed.
