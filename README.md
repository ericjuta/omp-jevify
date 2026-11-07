# omp-jevify

An [Oh My Pi](https://github.com/can1357/oh-my-pi) plugin for source-backed evidence
preparation, review, and optional rubric-based classification with `judge_batch` (Jev).
It ships the `jevify` skill and the `jevify_extract` extraction tool.

Choose the method before judging:

- Prefer a compiler, search, AST rule or short script when a deterministic rule decides
  the answer.
- Prefer direct reading for small jobs or tightly cross-dependent reasoning.
- Consider Jev for repeated, independently judgeable semantic decisions with sufficient
  context, such as whether changed hunks match a stated contract or review findings
  match the cited code.

There is no item-count mandate. Loading the skill, repeating a task or explicitly asking
for Jev does not make judgment the right method. When choosing Jev, define and pilot the
rubric, freeze it, then cover the declared scope. Read flagged sources and resolve errors,
unknowns and coverage gaps before claiming closure. Calibrate before bulk or destructive
action; read-only calibration is optional. Scores and model agreement are not proof or
permission to edit. It generalises Can Bölük's Jevict
([thread](https://x.com/_can1357/status/2102524777776677104)) beyond test pruning.

## Evidence guide

The [evidence workflow index](docs/README.md) links the captured design reference,
implementation decisions, Wayfinder maps, and five living ExecPlans. See the
[on-demand API and workflow guide](skills/jevify/reference.md#source-backed-evidence-workflows)
for preparation, extraction, source review, refresh and explicit history imports.

## Install

```sh
omp plugin install 'git+https://github.com/ericjuta/omp-jevify.git#v0.5.1'
```

To replace an existing installation with this version, add `--force` to the command.

The install command pins `v0.5.1` (see [Release 0.5.1](#release-051)). Earlier release
sections retain their historical evidence. The extraction extension requires OMP
18.10.3–18.x and Bun >=1.3.14; descriptor-confined transport currently requires Linux.
Deterministic Python preparation remains usable without a provider credential.

Start a new OMP session afterwards:

- Ask for the kind of job it is for ("go through every changed hunk in this commit and tell
  me which aren't explained by the message", "check every call site of X…") and the model
  picks the skill from its description.
- Or include the word `jevify` in the prompt. OMP's built-in `jevify` magic keyword adds
  its own instructions for that turn, and the skill is picked up alongside.
- Or type `/skill:jevify` to inject it directly.

OMP 18.12.5 (audited live on 2026-10-21) injects a `jevify-notice` on the keyword "jevify"
telling agents to call the built-in `judge_batch()` for any list of about 20 or more items.
It never mentions `jv.*`, and it conflicts with this skill's method-first guidance. In 4 of 4
recent real sessions agents followed it and never used the helper; in 3 of those 4 a direct
script did the real work. The earlier 18.8.7 through 18.9.1 audit found the same item-count
notice and is kept as historical evidence. Installing the plugin alone does not change any
host notice; the `jv.adopt` helper lets work the notice already started be
bound to receipts (see the adoption subsection below).

The model loads the helpers itself; by hand, in the Python eval kernel:

```python
%load skill://jevify/jevify.py
```

This defines `jv`: extraction from diffs/search/ASTs, full-request budget planning,
no-truncation group packing, durable checkpoint/resume bundles, source-bound review
inbox, ordered rubrics, score distributions, bounded sensitivity pilots, primitive-aware
calibration, and guarded dry-run-by-default span deletion.

The reference includes executable recipes for documentation/contract drift, issue
intake, and source/skill relevance alongside the existing migration and review sweeps.
Each preserves uncertainty and requires source confirmation; none grants edit authority.
See [`skills/jevify/SKILL.md`](skills/jevify/SKILL.md) for suitability and workflow;
read [`skills/jevify/reference.md`](skills/jevify/reference.md) on demand for helper APIs
and recipes.

### Provenance is not inferred

The audit of stock OMP 18.8.7 through 18.9.1, repeated live on 18.12.5, found that drained
judgment items expose only `key`, `ok`, `error`, `answers` and `model`, and `batch.status()`
exposes `id`, `intent`, `total`, `done`, `failed`, `cost`, `running`, `model` and `elapsedS`.
There is no selected/primary model, fallback or cache-hit field. The helper marks missing
provenance `provenance_unknown`, and `jv.flag` still adds it to every row. Fresh answers
report a versioned served model (for example `openrouter/typesafe/jev-1.14-20261014`); repeat
judgments answered from cache reported the alias `openrouter/~typesafe/jev-latest` with cost 0,
so `comparable` is False across cached and fresh rows and the helper cannot prove the alias is
the same model. `jev-latest` moved from 1.13 to 1.14. Treat unknown provenance as unresolved,
not as evidence that the primary model answered.

## Release 0.5.1

The initial v0.5.0 tag failed CI because Bun 1.3.14 cannot parse Bun 1.4's lockfile format.
The tag is preserved; v0.5.1 uses the minimum-version-compatible format with all dependency
versions and integrity hashes unchanged. Frozen installation, TypeScript, all 29 adapter
tests and the packaged smoke passed under Bun 1.3.14 as well as the development runtime.

All five Clairic implementation proposals are included:

- **Immutable evidence:** explicit source roles, exact Unicode/byte/full-line spans,
  complete scope accounting and live dependency/inventory validation.
- **Default-on Clairic preparation:** the installed tool uses host-resolved OpenRouter
  credentials for selected public/private non-secret inputs. `--no-jevify-extract` opts
  out; structured fields and deterministic preparation do not need a provider. Missing
  keys and failures preserve unresolved scope. Zero-transmission recovery is explicit;
  uncertain attempts are never silently retried.
- **Source-backed review:** question-specific readiness, supporting-source citations,
  existing-inbox integration, required-facet completeness and explainable refresh.
  Stale dependencies are rejected before judging/adopting/resuming, not only at review.
  Imported comments and immutable snapshots cannot become deletion targets.
- **Additional workflows:** documentation qualifications, bounded migration dependencies,
  inert issue/log intake and explicit source-verified OptChat/history imports. Historical
  text and reviewer confirmation never grant edit authority.
- **Controlled evaluation:** `evals/preparation.py` freezes four treatments and isolates
  Clairic's incremental effect against identical deterministic preparation. Approval is
  bound to one durable experiment ledger; accounting comes from completed host events,
  not model-writable files. Synthetic runs cannot recommend real-world adoption.

Verification: **575 Python tests**, **29 adapter tests**, adapter TypeScript checking,
and a packaged fresh-process SDK/Python loopback smoke. The packaged smoke captured four
controller receipts and four real SDK observer lifecycle frames. It exercised initial
fixed thinking, in-memory usage accounting, Unicode/CRLF, null abstention, opt-out,
zero-request recovery, unsafe paths and interruption with **zero outbound provider requests**.

No new live Clairic/Jev experiment ran. A real-provider wire smoke still requires exact
manifest/cap approval; controlled live comparisons additionally require independently
adjudicated realistic gold. No accuracy, savings or review-time improvement is claimed.
Provider-reported usage is not a settled invoice; unknown costs remain unknown.

## Release 0.4.0

Historical reports and tagged artifacts are immutable; none were regenerated.

0.4.0 adds three features plus adoption helpers for the host notice:

1. **Structured findings for schema-2 grading** (`evals/findings.py`, `jevify-findings/1`).
2. **A durable, source-bound review inbox** (`jv.review_queue` and companions in `skills/jevify/jevify.py`).
3. **Stricter pilot reporting** (`evals/run.py --pilot`, `direct-reading`, result schema 6, and
   `evals/grade.py` `compare_graded`).

Release verification: **456 unit tests pass**. Offline runtime checks exercised structured
`grade()`, cross-process inbox persistence and atomic decisions, stale-evidence rejection,
replay of retained real judgment rows, and pilot preflight/comparison withholding. These
checks made **no new model requests**; they are workflow proof, not a quality benchmark.

Evidence status: this section records no new measured quality or savings claim and no
independent benchmark. The repository has one schema-2 answer key (`refund_retry`, scale 1).
It cannot supply the independent held-out cases a method recommendation needs. Existing
development cases therefore produce withheld or inconclusive pilot results.

### Adoption helpers for the host notice (`skills/jevify/jevify.py`)

Session evidence behind these helpers: the host notice sent agents to raw `judge_batch()`, so
none of the receipts, flags or review applied; in 3 of 4 sessions a direct script was the better
method; a jev-1.13 route limit of about 32.7k input tokens and 255 labels was measured; and one
audit reported a 16-page splice as a full sweep.

- `jv.adopt(items, states, Q, batch=b)` and `jv.scalar` bind built-in `judge_batch()` output to
  receipt-bound rows, so flag, policy rules, review and export work on notice-driven runs.
- `jv.pilot_check` returns `use_direct` when the flag, uncertainty-band or catch-all rate is
  above about 40%; `jv.remaining` reuses unchanged pilot verdicts; `jv.power` sizes a sample;
  `jv.ledger` keeps tuned units out of held-out evidence.
- `jv.facts`, `jv.units_from_table`, `jv.refuse_elided`, `jv.choice` (255-label limit),
  `jv.plan` (`labels`, `over_labels`, `pack_suggestion`) and `jv.flag(report=)` keep script-decidable
  work out of the judge and refuse elided or truncated input.
- `jv.splice`, `jv.comparable`, `jv.gate`, `jv.export` and `jv.receipt_md` state what a
  spliced or mixed-model receipt supports. Run/adopt receipts gain `served_models`,
  `served_unknown`, `comparable`, `host_counts`, `billing` and `assembly`; rows gain
  `state_sha256`, `judged_at` and `assembly`.

Live smokes on OMP 18.12.5 cost about $0.0002 in total and exercised adopt, run, remaining,
splice, gate, `receipt_md` and export. Provenance stayed unknown and billing is an unverified
estimate. These are workflow checks only: no quality or savings claim.

### 1. Structured findings grading (schema 2)

A schema-2 answer is one JSON object, optionally inside one full ```` ```json ```` fence,
with no other content:

- `schema`: `"jevify-findings/1"`.
- `source_revision`: the git tree id (`HEAD^{tree}`) of the reviewed change.
- `findings`: a list. Each finding has exactly `claim_ids` (distinct exact ids from the key),
  `factual` (`supported`, `refuted`, `underdetermined`), `action` (`fix_now`, `defer`,
  `no_action`, `investigate`), `introduced` (`yes`, `no`, `unknown`) and `evidence`.
- Each evidence item has exactly `revision` (the HEAD tree or the BASE tree at `HEAD~1`),
  `file`, 1-based inclusive `start` and `end`, and `quote`. The quote must equal the full text
  of those lines joined with LF. No substrings, stripped text or partial lines.

Validation is deterministic and makes no model call. Duplicate JSON keys and non-finite numbers
are rejected. A claim named by a rejected finding, by several findings, or by none is
unresolved or omitted, and is never credited. Passing validation proves form, exact ids,
revision and full-line quote identity against the source snapshot. It does **not** prove that
the labels are true. Labels are scored only against the frozen key.

Source snapshot, validate and render, run from `evals/` against the reviewed repository:

```python
import findings

snapshot = findings.source_snapshot("/path/to/reviewed/repo")  # trees of HEAD and HEAD~1
inventory = ["C01", "C02"]   # exact claim ids from the frozen answer key, not from the answer
record = findings.validate_findings(answer_text, snapshot, inventory)
print(record["status"], record["reason"])   # whole document: accepted or rejected
print(findings.render_findings(record))     # text built only from the validation record
```

`findings.output_contract(repo)` returns the prompt suffix that states this format.
`evals/grade.py` (`SCORING_REVISION = "structured-findings-fixed-denominators-v4"`) runs the same
validation for every schema-2 answer. In the OMP kernel:

```python
%load evals/grade.py
await grade("/tmp/cand.json", "evals/cases.json")
```

The prose parser and `validate_extractor_host` are not part of schema-2 grading in this working
tree. `evals/extractor-corpus.json` and earlier prose-based results are retained only as
historical evidence (see [Historical: prose extractor](#historical-v031-prose-extractor)).

### 2. Durable review inbox

The inbox records source-bound confirm, overturn or unresolved decisions on units from an
extraction. The review is a durable directory on disk. Pass its **path** to every call. Legacy
in-memory review dicts are refused. Every decision is an append-only journal entry.
Decisions grant no edit authority.

Create or reopen the review (`Q` is the rubric; `rows` is one run that judged this `Q` and this
manifest):

```python
policy = jv.policy(Q, {"verdict": {"top": 0.7, "margin": 0.2}})   # keys are ids of Q
REVIEW = "/tmp/jevify-review"                                       # durable path
items = {"c1": {"unit": "u07", "claim": "handler drops the retry error", "question": "verdict"}}
summary = jv.review_queue(REVIEW, manifest, items, Q=Q, policy=policy, rows=rows)
```

- `items` maps item id to `{"unit", "claim"}` with an optional `"question"`. Those three keys are
  the only accepted fields. Model verdict, uncertainty and model are **derived from `rows`**,
  never caller-supplied. `rows=None` leaves them unknown. Error rows are refused.
- Each excerpt is the unit's exact manifest span, frozen with its quote and file sha256.
- `policy` must come from `jv.policy(Q, bounds)`.
- Items start `pending`. Calling `review_queue` again with the same path and an identical plan
  reopens the review and returns its status. A different plan at that path is refused.

Ask, then decide. `review_questions` returns at most `limit` open items as ask-compatible
questions, plus a binding per item to echo back:

```python
asked = jv.review_questions(REVIEW, Q=Q, limit=5)
# asked = {"questions": [...], "bindings": {id: {"fingerprint", "view", "citation"}},
#          "open": N, "stale": {...}}
# Present asked["questions"] with ask. Options are confirmed, overturned, unresolved, in that order.
# The example below assumes an actual human selected confirmed. Agent source review uses kind="agent".
b = asked["bindings"]["c1"]
decisions = [{"item": "c1", "fingerprint": b["fingerprint"], "view": b["view"],
              "verdict": "confirmed", "evidence": [b["citation"]], "note": "excerpt matches claim"}]
out = jv.adjudicate(REVIEW, decisions, Q=Q, reviewer={"kind": "human", "id": "maintainer"})
```

- Queue order: `pending` items first, then invalidated `stale_evidence` items, then `unresolved`
  items, each by id. Paging therefore reaches items not yet asked.
- `confirmed` and `overturned` must cite `{"file", "start", "end"}` inside the frozen snapshot
  (at most 400 lines each). `unresolved` may cite nothing. `evidence` must be a list.
- A batch is atomic. Every decision is checked against the current view first. Any refusal raises
  `jv.ReviewRefused` (`.problems` lists each `item`, `reason`, `detail`) and records nothing.
  Reasons include `unknown_item`, `duplicate_item`, `fingerprint_mismatch`, `superseded`
  (view changed), `stale_source`, `stale_rubric`, `stale_policy`, `protected_decision`,
  `invalid_evidence` and `invalid_decision`.
- `reviewer` is `{"kind": "human" | "agent", "id": str}`, recorded as **self-declared**, not
  authenticated. An agent cannot replace a human's latest decision.
- The return value is `{"batch", "applied", "review"}`.
- `jv.review_items(REVIEW, Q=Q)` lists every item with its excerpt, model fields, verdict,
  stale reason and history.

Status. `jv.review_status(REVIEW, Q=Q)` returns:

- `pending`, `unresolved`, `confirmed`, `overturned`: sorted item ids for each verdict.
- `stale`: every item with a drift reason (`stale_rubric`, `stale_policy`, `stale_source`,
  `stale_evidence`), **whatever its verdict**. A confirmed item whose excerpt file changed is
  reported here.
- `drift`: `rubric`, `policy`, and the changed `source` files.
- `complete`: no pending and no unresolved items.
- `approved_units`: units whose items are all `confirmed` and none stale.
- `settled`: complete, with no stale items and no drift.
- `grants_authority`: always `False`.

`stale_source` items cannot be decided again and are left out of the questions. `stale_evidence`
means the latest decision cites a changed file. That decision no longer counts, and the unchanged
excerpt can be decided again. Any changed frozen file keeps `settled` false.

Removed, with no compatibility alias: the `authorized` status field, positional
`adjudicate(review, item_id, verdict)`, and in-memory review dicts.

Deletion, dry run first. `delete_spans` removes merged inclusive unit spans. It defaults to
`dry_run=True`:

```python
status = jv.review_status(REVIEW, Q=Q)
assert status["settled"] and status["grants_authority"] is False
targets = ["u07"]   # only units you approve for this deletion; each must be in approved_units
jv.delete_spans(units, targets, root=REPO, dry_run=True, manifest=manifest, review=REVIEW, Q=Q)
# Inspect the printed spans. Only with explicit deletion authority:
# jv.delete_spans(units, targets, root=REPO, dry_run=False, manifest=manifest, review=REVIEW, Q=Q)
```

- `review` requires both `manifest` and `Q`. Without `review`, only the manifest guard applies.
- The guard checks source against the manifest, unit content against its record, the review's
  manifest digest, and no rubric, policy or source drift. It also requires no pending or unresolved
  items, and every target to be an approved unit. The review lock is held through the write.
- Any failure raises before a file is written, including in a dry run.
- A review grants no deletion authority beyond the explicit call.

### 3. Stricter pilot reporting

Pilot manifests are predeclared JSON (`evals/run.py --pilot FILE`, schema `jevify-pilot/1`) with
exactly these keys: `schema`, `pilot_id`, `phase`, `rubric_frozen_at`, `frozen_at`,
`thresholds_sha256`, `comparison`, `cases`.

- `phase` is `development` or `heldout`. Every case's `split` must equal the phase. Held-out cases
  must be `unexposed`. `evals/holdout.json` is the exposed sealed holdout and cannot be fresh
  held-out evidence.
- `frozen_at` must not precede `rubric_frozen_at`, and must be before the run starts.
  `thresholds_sha256` must equal the digest of `--thresholds`, or be `null` when none is given.
  A held-out pilot requires `--thresholds`.
- `comparison`: `base` may be any strategy. `candidate` must be a method: `direct-script`,
  `direct-reading`, `jev-flat` or `jev-grouped`.
- `cases` must equal the selected cases exactly. Each case's `key_sha256` and
  `public_rubric_sha256` must match the launch fixture.
- `gold` is `null` (recorded as `absent_non_independent`), or an independent adjudication:
  `establishment: "independent_adjudication"`, `key_author`, `key_sha256`,
  `adjudicator: {"kind": "human" | "agent" | "organization", "id"}`, `adjudicated_at` (not after
  `frozen_at`), and `evidence: {"path", "sha256"}`. Evidence must be a file inside the manifest's
  directory. The adjudicator must differ from the key author and from `jevify-repository`.
  Evidence cannot be a key, rubric, fixture or runner artifact.
- These identities are operator attestations (`operator_attested_unauthenticated`), not
  authenticated proof. The manifest and gold evidence bytes are retained beside the output.

Strategies and shared context. Every strategy receives the same schema-2 output contract from
the reviewed HEAD tree, and the same public-rubric obligation. Only the method instruction differs.
`direct-reading` reads source and reasons from it. It may not write or run scripts, and may not
call `judge`, `judge_batch`, `completion` or the helper to decide the answer. Result schema 6
(`RESULT_SCHEMA = 6`) binds each case's fixture identity to its key, rubric and output-contract digests.

`compare_graded(base, candidate)` returns separate results:

- `statistical_result`: a descriptive verdict from the predeclared thresholds and paired
  per-case intervals. Pilot gates do not change it, and it is **not** a recommendation.
- `method_recommendation`: a pilot-gated decision with `verdict`, `strategy`, `gates`, `reasons`,
  `pilot_sha256` and `attestation`. Its verdicts are:
  - `not_comparable`: a comparability gate failed. Gates require one pilot manifest, one treatment
    difference (the strategy), a method candidate, the declared strategies, bound keys and rubric,
    a manifest frozen before the runs, and identical observed model and thinking.
  - `withheld`: an evidence gate failed. Gates require a held-out phase, unexposed held-out cases,
    independent gold for every case, predeclared thresholds bound to both results, and each side
    recording the graded answer-key and fixture/key digests for every case. Two case IDs that bind the
    same answer key or fixture/key pair are **aliases**, not independent cases, and withhold the
    recommendation. A shortfall below the thresholds' `min_cases` is reported by the statistical result
    as `inconclusive`, and the method recommendation stays `inconclusive` with it.
  - `use_direct_script`, `use_direct_reading` or `use_jev` (with `strategy` set): the candidate
    is supported by the statistical verdict.
  - A baseline method preference (`use_<base>`) when the candidate is contradicted. This also
    requires `reversed_statistical_result`, the same predeclared rule applied with the baseline as
    challenger, to support the baseline. A forward contradiction alone is insufficient. An `auto`
    baseline names no method, so no method outcome is inferred.
  - `contradicts_candidate` when the candidate is contradicted and no method outcome is supported.
    `inconclusive` otherwise. `withheld` and `inconclusive` keep their meaning and do not become
    method claims.

Observed side fields are recorded from the run and are never imputed. Latency, cost and review
burden each keep their observed value or `null`. Manual review minutes are always `null` because
no run measures them. Cost is an observed subtotal with `billing_complete: false` and
`imputed: false`.

Current status: `evals/thresholds-release-smoke.json` is a predeclared rule file (`min_cases: 2`)
for smoke runs. It is not a result. The repository has one schema-2 key, so the development cases
cannot establish a method recommendation. A method claim needs separate, independently adjudicated
held-out gold for at least `min_cases` distinct cases. None exists in this repository, and none is
claimed here.

## Historical: 0.3.1

0.3.1 was the tagged checkpoint of the corrected evaluation grader (`9109a59`,
`cb9aacd`; scoring revision `independent-facets-fixed-denominators-v3`). It adds no plugin runtime
capability. Its tag's skill and helper files match 0.3.0 apart from the version field. The
0.4.0 helper and grader differ from that tag, as described above. The 0.3.1 release
notes are at the [v0.3.1 release page](https://github.com/ericjuta/omp-jevify/releases/tag/v0.3.1).
The 0.3.0 evidence below remains historical and unchanged.

## Historical: upgrading to 0.3.0

These steps describe the 0.3.0 release. The `slots=` parameter they mention has since been removed.

### What changed

| Workflow | New in 0.3.0 |
| --- | --- |
| Plan a sweep | Full-request token planning and budget-aware packing without truncating evidence |
| Recover interrupted work | Durable bundles and explicit resume instead of manual recovery |
| Review model findings | Source snapshots, evidence-linked adjudication and guarded dry-run deletion |
| Assess rubric reliability | Order-sensitive identities, retained score distributions and bounded sensitivity pilots |
| Evaluate outcomes | Revision-bound comparisons, scheduling envelopes and isolated grading launches |

See the [expanded release notes](https://github.com/ericjuta/omp-jevify/releases/tag/v0.3.0)
for the before/after comparison, new recipes and measured limitations.

### Migration checklist (0.3.0)

Re-freeze questions for new sweeps: question and choice ordering contribute to rubric identity, so
earlier hashes are not interchangeable. Run inputs require string unit IDs. Execution artifacts use
schema 5; the grader calls justified repair recommendations `recommended_defects`, not observed code
repairs. Existing saved schema-2 helper receipts remain readable; they do not gain missing historical
evidence.

At 0.3.0, `slots=` was replaced with `grouping=grouping`, passing the full manifest from `jv.group`
or `jv.pack`. Recovery started with `jv.run(..., bundle_path=...)` and `jv.resume` after
interruption. Missing deliveries were resubmitted only with explicit `resubmit_missing=True`.

Historical 0.3.0 release verification, retained in
[`evals/results/release-v0.3.0-smoke.json`](evals/results/release-v0.3.0-smoke.json), recorded 281
passing tests at that time, real kernel-loss recovery without resubmission, source-bound review,
grouped exclusions, calibration, recipes and isolated grading launches. That count describes 0.3.0
and does not describe the current suite. The direct-versus-flat comparison was **inconclusive** (one
independent outcome case). The 18-record synthetic extractor check made four identity-misattribution
errors: it applied a fix-now verdict to a target the answer never named exactly (a different exact
ID, an off-inventory ID, an inexact ID, or a group that excludes the target). Against the fixture's
source truth that is two unwarranted repairs (r03 → refuted C07, r06 → refuted C06) and two unearned
credits (r02 → C02, r04 → C01, true defects credited without an exact-ID mention). The retained report
lists all four under its historical `high_impact_false_repairs` field. Source review remained required.
These observations established exercised paths, not a quality or savings advantage. Host provenance and
exhaustive billing remained unknown. Recovery was **not exactly-once execution**: an orphaned submission
may still bill. Reattachment required a surviving batch in the owning OMP session, and did not survive
host process or session loss.

### Historical: v0.3.1 prose extractor

This section describes the v0.3.1 prose extractor, which v0.4.0 replaced with structured
findings. The v0.3.1 grader bound extraction to exact claim IDs and bounded answer excerpts before
judging. Missing IDs were recorded as omitted. Ambiguous attribution stayed unresolved and earned no
repair credit. Explicit groups remained valid when they unambiguously included the target. Ordinary
paragraph boundaries stopped continuation. Claim headings and labels kept their own body, but stopped
before another heading or any block naming another claim. Bold or code formatting around individual IDs
did not invalidate an explicit group. Bare labels without assertions stayed unresolved. Attribution
rejections were counted as `unattributed`, not `extraction_errors`; both remained unresolved and in the
scoring denominators.

That scoring used `independent-facets-fixed-denominators-v3`. It constrained judge input but did not
prove the judge's interpretation was correct. Assigning ID-free prose to a neighbouring block was a
heuristic, so source review remained necessary. Post-release agent review of `9109a59` checked r02,
r03, r04 and r06 against the [synthetic fixture source](evals/refund_retry_fixture.py), and confirmed
r05's valid explicit-group attribution. That was agent adjudication, not human validation.

The v0.3.1 closeout recorded 306 passing tests and an offline extractor-entrypoint replay with a
simulated judge, with no new model judgments. Those results are historical. They do not describe the
current suite.

## Requirements

- OMP with the eval kernel's `judge_batch`, `judge` and `completion`.
- `omp toks` (omp ≥ 18.3.0) for sizing large states in Jev tokens; without it `jv.states`
  warns that they are unmeasured.
- `rg` and `git` for the diff and search extractors.
- `uv` (or pip) for `jv.ast_units`, which installs `ast-grep-py` on first use into
  `${XDG_CACHE_HOME:-~/.cache}/jevify/` (the kernel's Python may have no pip).

## Development and evals

Local tests: `python3 -m unittest discover -s tests`.

`evals/run.py` runs bounded OMP sessions in generated fixture repositories. Skill
availability and requested method are separate controls:

- `--arm skill` (default) pins the tested skill bundle; `--arm none` hides jevify.
- `--strategy auto` (default) leaves the case goal unchanged. `direct-script`,
  `direct-reading`, `jev-flat` and `jev-grouped` append method instructions, not expected answers.
- `--ref` tests a Git revision's skill directory; `--skill-dir` tests another complete
  bundle. With neither, the runner uses the working-tree skill.
- `--omp` selects the executable; `--omp-source` records a supplied source checkout's
  identity separately. `--model` and `--thinking` select the subject configuration.
- `--reps`, `--jobs`, `--max-time`, `--max-calls` and `--retries` bound execution.
- `--preflight` prints planned exposure without starting sessions. `--authorize-attempts`
  and `--authorize-concurrency` gate scheduling. `--authorize-questions` stops future
  attempts when observed questions exhaust the limit **or** question coverage is unknown;
  stock hosts cannot establish exhaustive question counts, so this guard stops after the
  first attempt. It is not a hard question or dollar ceiling for in-flight nested calls.
- `--thresholds` freezes a predeclared correctness/coverage/unsafe-recommendation rule.
  Independent cases, not repeated fixture items, form paired uncertainty intervals.
- `--pilot` binds a predeclared `jevify-pilot/1` manifest (see
  [pilot reporting](#3-stricter-pilot-reporting)).

Model runs require model access and spend real tokens. Opening the skill is activation
evidence, not evidence that classification was suitable or that an answer was correct.
Explicit strategies measure requested-method adherence separately from answer quality.

## Evaluation evidence and cache controls

Execution results use schema 6 since v0.4.0. Schema 5 and earlier artifacts remain
historical and are not regenerated. Both record skill/source, cases and fixture identities;
requested configuration; `runs`; `summary`; and a `cost_ledger`. Runs retain `attempt_records`,
including failed or retried attempts, captured answers, execution status and strategy adherence.
Completed runs are appended to `<out>.partial.jsonl`; raw artifacts are kept beside the output
unless `--raw-dir` selects another location.

The runner snapshots case and skill bytes before launch and binds the manifest to
those same inputs. Comparison contracts record host executable identity separately
from any supplied source checkout, model/thinking, fixture and runner identities,
cache seed, concurrency, retries and predeclared thresholds. Captured answers remain
available after process failure, but `answer_status` and `process_status` are separate;
a crashed or malformed session is not complete. Summaries expose all-scheduled and
completed cohorts, total retry elapsed time, and deterministic control outcomes.

The runner starts each attempt with a private `--session-dir` and reads persisted
session JSONL `type: model_usage` entries after the process settles. It does not depend
on a `model_usage_summary` print event. The retained ledger is an accounting-only
projection; the transient full session is deleted after collection. Outer assistant
usage and auxiliary usage remain separate. `snapshot_statuses` and `collection_status`
describe collection completeness, not overall billing coverage. Stock OMP through 18.9.1 does
not journal eval `completion` calls in this ledger. Even a complete file containing
judge rows cannot establish zero completion cost or a complete execution total.
Completion, auxiliary and total buckets retain `coverage_unknown: true`,
`cost_unknown: true` and null `reported_cost`, including after attempts are merged.
`reported_subtotal` and token sums count only observed rows; a zero subtotal is not
proof of free execution, and unknown coverage leaves `tokens_complete` false.
Observed costs remain estimates unless attested otherwise. Unknown costs remain
unknown, not zero; execution accounting does not include grading overhead.

`judged` can be unknown. `auto_judged` reports observed judging over all eligible runs;
`auto_judged_unverified` reports unknowns included in that denominator. Fully cached judging may produce
no billed usage rows, so an empty ledger does not prove that no judgment occurred.
Helper receipts and source markers are not host-attested execution evidence. These
limits also constrain strategy-adherence claims.

### Cache controls

The host's default judgment cache is shared, not session-local. The runner provides:

- `--cache-mode cold` (default): a fresh, independent `OMP_JUDGMENT_CACHE_DB` for each
  attempt.
- `--cache-mode warm --cache-namespace PATH`: freezes an explicit seed once with a consistent
  SQLite backup, then clones it independently for every attempt. A seed alone never proves cache
  hits.
- `--cache-mode uncontrolled`: leaves the host cache untouched.

For comparisons across invocations, keep the seed stable and compare the recorded seed
fingerprints. Do not hash only the main file of a live WAL database or let arms share a
single growing writable cache.

```sh
python3 evals/run.py --arm skill --strategy auto --cache-mode cold --jobs 1 --out /tmp/cand.json
python3 evals/run.py --arm none --strategy auto --cache-mode cold --jobs 1 --out /tmp/none.json --compare /tmp/cand.json
```

For an explicitly requested flat-method run against an existing seed:

```sh
python3 evals/run.py --arm skill --strategy jev-flat --cache-mode warm --cache-namespace /path/to/seed.sqlite --jobs 1 --out /tmp/warm-flat.json
```

`--compare` reports full contract mismatches and paired case-level execution differences;
it does not grade answer correctness. `compare_graded(base, candidate)` returns the separate
`statistical_result` and `method_recommendation` described above. Missing evidence or too few
distinct independent cases gives an inconclusive or withheld result, not a quality win.

### Grading and evidence limits

`evals/grade.py` grades saved answers in OMP's Python eval kernel. Schema-2 answers are
structured findings validated deterministically against a source snapshot rebuilt from git trees
(see [structured findings](#1-structured-findings-grading-schema-2)). Schema-2 answer keys separate
factual truth, action under the public policy, and origin. Historical Boolean keys remain separate
as schema 1. Grading outputs reference a shared grading-cost ledger, rather than adding grading
overhead to each execution arm. Batch costs are estimates; total billing remains unknown even for
zero-cost or empty batches. Direct kernel `cache_mode` records intent only. The isolated launcher
configures cold/warm cache clones, freezes grader/results/cases/fixture bytes, verifies the two exact
eval cells and output bindings, and separately collects the settled session usage ledger.
Per-item route/cache provenance stays unknown when the host does not supply it.

```sh
python3 evals/grade_launch.py --results /tmp/cand.json --cases evals/cases.json --out /tmp/cand.graded.json --cache-mode cold --concurrency 2
```

Omissions, attribution rejections (`unattributed`) and extraction failures all remain in the planned
denominators. Repair fields (`recommended_defects`) count recommendations, never observed code repairs.
Off-inventory claims are unknown unless a separately bound audit is supplied.

`evals/extractor-corpus.json` is historical, source-adjudicated **synthetic** evidence from the
v0.3.1 prose extractor. It is not a current grading input, and its former `validate_extractor_host`
entrypoint is not part of v0.4.0.

`evals/holdout.json` is sealed. Do not read or edit its prompts while tuning the skill
or eval machinery. Once used for tuning, it is no longer a holdout.

Recorded artifacts in `evals/results/` describe their captured revisions and conditions,
not current quality guarantees.

## License

MIT
