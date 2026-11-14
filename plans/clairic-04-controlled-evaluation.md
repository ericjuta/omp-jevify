# ExecPlan 4: isolate extraction benefit with a controlled experiment

This living plan follows [planning conventions](../docs/agents/issue-tracker.md). Update Progress, Surprises & Discoveries, Decision Log and Outcomes & Retrospective without rewriting historical outcomes.

## Purpose / Big Picture

Users can determine whether Clairic adds value over the exact same deterministic evidence preparation, rather than attributing better retrieval or structure to the model. Rejecting extraction or retaining direct reading is a valid result. No paid evaluation is authorized by this plan.

## Progress

- [x] (2026-11-07) Identified that existing pilot gates compare methods, not preparation treatments.
- [x] (2026-11-07) Implemented the preregistration contract, independent-case admission and fixed C-versus-B primary contrast.
- [x] (2026-11-07) Implemented `evals/preparation.py` freeze/preflight/run/report and four provider-free executable demonstration scenarios without changing old strategy gates.
- [x] (2026-11-14) Froze 11 owner-adjudicated realistic cases (7 valid, 4 invalid findings), thresholds, artifacts and provider-free preflight as manifest `0b6374d2c164b45b75fa4e71bcd5306c5694588919def653957bebdff774efc0`.
- [x] (2026-11-14) Owner approved that exact digest with a $15 best-effort ceiling and 88 attempts.
- [x] (2026-11-14) Executed the approved run once. It stopped under its frozen rule after 34 of 88 attempts; the recommendation is `inconclusive` and nothing is adopted. No threshold was changed.

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

Generate and hash the preregistration manifest only after the runner, cases and thresholds are fixed. Provider-free preflight proves fixture admission, identities, denominators and withholding. Obtain owner approval of the digest and cap; the approved run must verify all bound files before transmitting. Stop admission at the approved attempt/allocation cap or when incomplete attempts exceed the frozen tolerance, and report unresolved results. This does not guarantee a hard invoice cap: retries, in-flight attempts and missing provider usage may leave actual billing unknown. Freeze conservative per-attempt reservations and how uncertainty consumes the approved budget; require the owner to accept any best-effort rather than hard-dollar policy explicitly. Preserve failed and aborted attempts in the ledger.

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

2026-11-14: the approved live wire smoke showed the Clairic route reports cost `0` with no corroboration, and the host's completion receipts are catalog estimates. The runner treated any unknown cost as incomplete evidence, so a live run would have stopped after its first attempt and could never reach a verdict, which was stricter than this plan. Cost completeness now stops a run and gates a verdict only when the frozen primary metric is `total_cost_usd`. Unknown-cost attempts still consume their full reservation, cost totals stay null, and the report states whether observed cost was complete. Missing answers or provenance still stop a run.

2026-11-14, owner-approved rehearsal (four arm-B attempts on a public synthetic case, outside any manifest): the host returned its answer inside a code fence with prose, which the runner refused, and it answered without calling the judge, which leaves judge provenance empty and stops a run. The runner now accepts a bare JSON object or one leading fenced block, and arms A to C are told to decide each question with a judge call. The fourth attempt then completed with the judge observed as `openrouter/cloudflare/clef`. Catalog estimates were about $0.04 per attempt without a judge call and about $0.10 with one, plus about $0.10 once for an uncached first prompt. Arms A, C and D are not yet rehearsed live.

2026-11-14, owner-approved rehearsal of arms A, C and D (one attempt each, same public synthetic case, outside any manifest): all three completed with required answers and provenance. In arm C the host itself called `jevify_extract` twice and the observed extraction model was `cloudflare/clairic-20261029`; both extraction receipts carry unknown cost. Arm D ran without a judge, as designed. Catalog estimates were about $0.16 (A), $0.19 (C) and $0.12 (D). Each included about $0.10 for an uncached first prompt, because arms use different tool sets and do not share a prompt cache; a repeated arm-B attempt had paid about $0.02 for the same turn. The expected total for 88 attempts is therefore about $7 to $8 when same-arm attempts reuse the cache, and about $14.5 if they never do. Receipts had labelled successful tool-use turns `failed`; they are now `complete`, and only other stop reasons are `failed`.

2026-11-14, approved live run (manifest `0b6374d2…efc0`, case text kept outside this repository): 33 attempts completed, then one arm-C attempt ended with a host reply that was not a JSON object. With no retries and stop-on-incomplete frozen, the run stopped at attempt 34. The `complete_outcomes` gate withholds the verdict, and the 11 cases are now exposed, so they cannot serve as fresh held-out gold again. The runner does not keep the unparsable reply, so its content is unknown.

The partial outcomes are descriptive only (first repetition, unbalanced across arms, no interval): correct decisions were 4 of 7 in A, 4 of 9 in B, 1 of 9 in C and 1 of 8 in D. In all 10 arm-C attempts, and in the earlier arm-C rehearsal, Clairic completed both requests and returned zero candidates. The frozen field asked for the code line that a review comment is about. Each source went to Clairic as the `state` of its own request. (An earlier version of this entry concluded that the code was therefore read without the comment. That was wrong; see the correction dated 2026-11-14 below.) Under the frozen fallback (an absent extraction is never replaced by a guess) arm C left 7 of 9 decisions unresolved. This design therefore could not have shown an extraction benefit, and the rehearsal should have been checked for a non-zero candidate count before freezing.

Cost: the catalog estimate for the 34 attempts was $2.82, with Clairic cost unknown (20 requests, each reporting `0`). The runner's ceiling counts only provider-reported cost, so it charged the $0.05 reservation per attempt ($1.70) and would not have stopped the run at $15; the estimate was checked by hand against the approved ceiling.

2026-11-14, correction after rebuilding one arm-C extraction request from its retained inputs: the adapter already puts the whole target record, including both retrieved passages (the review comment and the code), into each field's instructions. The request for a 3,255-byte code source was 9,043 bytes. Clairic therefore had the comment in its instructions while reading the code, and still returned no candidate with zero output tokens. Why it abstained is not known. Untested possibilities: the instructions forbid quoting from the context, and the context repeats the code being searched; the field needs a judgment about which line a finding concerns, which may be outside what an extraction model does; or the long JSON context degrades the instructions. The wire smoke, which did return candidates, used short self-contained field instructions and no record facts. A one-case live check on public synthetic text must separate these before any second comparison is designed.

2026-11-14, runner repairs from the stopped run (provider-free, 55 preparation tests pass):

- The final host reply is now written to `host-answer.txt` in the attempt directory, beside the request, so a refused reply can be read afterwards. It is bound into the attempt's artifact hashes like every other file there.
- The budget key `stop_on_incomplete` is replaced by a frozen `max_incomplete_attempts`. An incomplete attempt is still never retried and still charges its reservation. Up to the frozen number, it fails only its own cell: the report scores it as answering nothing (every question unresolved, none correct) and marks it `failed`, so a failing arm is not flattered by dropping its failures. One more failure than the tolerance stops the run as before; `0` keeps the old behaviour. An interruption always stops the run.
- The allocation now charges each finished attempt the larger of its reservation and its provider-reported plus catalog-estimated cost. Before, catalog estimates counted as zero, so the ceiling could not bind on stock host completions. Unknown-cost receipts, such as Clairic's reported `0`, still add nothing beyond the reservation, so the reservation must be sized to cover them. This remains a best-effort allocation, not an invoice cap: the last admitted attempt can overshoot by its own cost.

## Decision Log

2026-11-07, superseding owner decision: Clairic defaults on for selected public/private non-secret source, with explicit opt-out and deterministic fallback, using host-resolved credentials including host environment-key fallback. Implementation and shipping are authorized. Exact manifest digest and cap approval remain mandatory before live experiments.

2026-11-07, agent proposal: separate treatment schema; C/B is the extraction attribution comparison; no transfer of OptChat's budgets or adoption decisions. Exact statistical policy, independent case counts and the paid cap remain unresolved until preregistration and owner approval.

## Outcomes & Retrospective

The executable controlled runner is implemented. The final repaired Python suite passed 575 tests, and the focused static rereview cleared all six remaining release blockers. Four final CLI demonstrations exited successfully: simulated improvement selected extraction, worsening/tie selected deterministic preparation, and missing results withheld the software gate. All four top-level real-world recommendations remained `inconclusive`. These were injected synthetic software checks, not experiments. Independent realistic gold and owner manifest/cap approval remain prerequisites for live measurement. No cost, quality, review-burden or savings improvement is claimed.

The one approved live comparison (2026-11-14) ended `inconclusive` after 34 of 88 attempts. It produced no evidence for or against extraction: Clairic abstained on every case, for a reason not yet established (the other source was present in its instructions). Deterministic preparation stays the default recommendation, and no cost, quality, review-burden or savings improvement is claimed.
