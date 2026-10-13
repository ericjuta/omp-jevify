# omp-jevify

An [Oh My Pi](https://github.com/can1357/oh-my-pi) plugin that ships one skill, `jevify`:
method-first suitability guidance and optional rubric-based classification in the eval
kernel with `judge_batch` (Jev).

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

## Install

```sh
omp plugin install 'git+https://github.com/ericjuta/omp-jevify.git#v0.3.0'
```

To replace an existing installation with this version, add `--force` to the command.

The install command pins the published tag. The helper and eval contracts below
describe this checked-out source; local changes and the separate host companion
patch are not made available by reinstalling that tag.

Start a new OMP session afterwards:

- Ask for the kind of job it is for ("go through every changed hunk in this commit and tell
  me which aren't explained by the message", "check every call site of X…") and the model
  picks the skill from its description.
- Or include the word `jevify` in the prompt. OMP's built-in `jevify` magic keyword adds
  its own instructions for that turn, and the skill is picked up alongside.
- Or type `/skill:jevify` to inject it directly.

Stock OMP 18.8.7 through 18.9.1 still injects an item-count-based mandatory `jevify` keyword notice
that conflicts with this skill's method-first guidance. Installing the plugin alone
does not change that host notice. The separate, unreleased local companion host patch
targets method-first guidance: Jev stays optional, there is no size requirement, and
context gathering is allowed before choosing the method.

The model loads the helpers itself; by hand, in the Python eval kernel:

```python
%load skill://jevify/jevify.py
```

This defines `jv`: extraction from diffs/search/ASTs, full-request budget planning,
no-truncation group packing, durable checkpoint/resume bundles, source-bound review
manifests, ordered rubrics, score distributions, bounded sensitivity pilots, primitive-aware
calibration, and guarded dry-run-by-default span deletion.

The reference includes executable recipes for documentation/contract drift, issue
intake, and source/skill relevance alongside the existing migration and review sweeps.
Each preserves uncertainty and requires source confirmation; none grants edit authority.
See [`skills/jevify/SKILL.md`](skills/jevify/SKILL.md) for suitability and workflow;
read [`skills/jevify/reference.md`](skills/jevify/reference.md) on demand for helper APIs
and recipes.

### Provenance is not inferred

Stock OMP 18.8.7 through 18.9.1 exposes only `key`, `answers`, `error` and `model` on judgment items.
It does not expose selected/response model, fallback or cache-hit provenance. The helper
marks missing provenance `provenance_unknown`; a model label or a change in that label
does not establish fallback or a cache hit. Treat unknown provenance as unresolved,
not as evidence that the primary model answered.

## Upgrading to 0.3.0

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

### Migration checklist

Re-freeze questions for new sweeps: question and choice ordering now contribute to
rubric identity, so earlier hashes are not interchangeable. Run inputs require
string unit IDs. Execution artifacts use schema 5; the grader calls justified repair
recommendations `recommended_defects`, not observed code repairs. Existing saved
schema-2 helper receipts remain readable; they do not gain missing historical evidence.

Replace `slots=` with `grouping=grouping`, passing the full manifest from `jv.group`
or `jv.pack`. For durable recovery, start with `jv.run(..., bundle_path=...)` and use
`jv.resume` after interruption. Missing deliveries are resubmitted only with explicit
`resubmit_missing=True`; stale batches otherwise stay incomplete. See the
[API reference](skills/jevify/reference.md) for the complete contracts.

Release verification: [retained smoke evidence](evals/results/release-v0.3.0-smoke.json)
records 281 passing tests, real kernel-loss recovery without resubmission, source-bound
review, grouped exclusions, calibration, recipes, and isolated grading launches.
The direct-versus-flat comparison is **inconclusive** (one independent outcome case).
The 18-record synthetic extractor check made four identity-misattribution errors: it
applied a fix-now verdict to a target the answer never named exactly (a different exact
ID, an off-inventory ID, an inexact ID, or a group that excludes the target). Against
the fixture's source truth
that is two unwarranted repairs (r03 → refuted C07, r06 → refuted C06) and two unearned
credits (r02 → C02, r04 → C01, true defects credited without an exact-ID mention). The
retained report lists all four under its historical `high_impact_false_repairs` field;
source review remains required. These observations establish exercised paths, not a
quality or savings advantage. Host provenance and exhaustive billing remain unknown.
Recovery is **not exactly-once execution**: an orphaned submission may still bill.
Reattachment requires a surviving batch in the owning OMP session; it does not survive
host process or session loss.

### Post-release extractor correction

The evaluation grader now binds extraction to exact claim IDs and bounded answer
excerpts before judging. Missing IDs are recorded as omitted; ambiguous attribution
stays unresolved and grants no repair credit. Explicit groups remain valid when they
unambiguously include the target. Ordinary paragraph boundaries stop continuation;
claim headings and labels retain their own body, including text that repeats the same
claim ID, but stop before another heading or any block naming another claim—even if
that other ID appears only after ID-free sentences. Bold or code formatting around
individual IDs does not invalidate an explicit group. Bare
labels without assertions stay unresolved. Attribution rejections are counted as
`unattributed`, not `extraction_errors`; both remain unresolved and in the scoring
denominators.

Schema-2 scoring now uses `independent-facets-fixed-denominators-v3`; historical
v0.3.0 results are unchanged. This constrains judge input; it does not prove the judge's
interpretation is correct. Assigning ID-free
prose to the preceding single-claim or explicit-group block, or owned section, is still
a heuristic, so source review remains necessary. This correction is evaluation tooling,
not a new plugin runtime.

Post-release agent review of `9109a59` checked r02, r03, r04, and r06 against the
[synthetic fixture source](evals/refund_retry_fixture.py), confirming the distinction
above, and confirmed r05's valid explicit-group attribution. This is agent adjudication,
not human validation; the corpus and historical smoke report remain unchanged. The
approved policy requires exact IDs, accepts unambiguous groups and bounded claim
sections, and requires source confirmation before any repair. Unresolved ambiguity
must be escalated, not treated as edit authority.

Closeout verification: 306 tests passed, plus an offline extractor-entrypoint replay
with a simulated judge. No new model judgments were run.

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
  `jev-flat` and `jev-grouped` append method instructions, not expected answers.
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

Model runs require model access and spend real tokens. Opening the skill is activation
evidence, not evidence that classification was suitable or that an answer was correct.
Explicit strategies measure requested-method adherence separately from answer quality.

## Evaluation evidence and cache controls

Execution results use schema 5, distinct from grading and answer-key schemas. They
record skill/source, cases and fixture identities; requested configuration; `runs`;
`summary`; and a `cost_ledger`. Runs retain `attempt_records`, including failed or
retried attempts, captured answers, execution status and strategy adherence. Completed
runs are appended to `<out>.partial.jsonl`; raw artifacts are kept beside the output
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
  attempt, including retries. No seed path is accepted.
- `--cache-mode warm --cache-namespace /path/to/seed.sqlite`: a read-only SQLite backup
  of an existing seed, including committed WAL contents, frozen once per eval invocation.
  Each attempt gets an independent writable clone of that snapshot. The runner records
  its starting fingerprint and does not judge against or update the original seed.
  A seed's existence does not prove cache hits.
- `--cache-mode uncontrolled`: use host cache state without asserting isolation.

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
it does not grade answer correctness. `compare_graded(base, candidate)` applies the
predeclared rule only to compatible measured outcomes with sufficient independent cases.
Missing evidence or too few cases gives an inconclusive result, not a quality win.

### Grading and evidence limits

`evals/grade.py` grades saved answers in OMP's Python eval kernel. Schema-2 answer keys
separate factual truth, action under the public policy and origin; historical Boolean
keys remain separate. Grading outputs use schema 2 and reference a shared grading-cost
ledger, rather than adding grading overhead to each execution arm. Batch costs are
estimates; total billing remains unknown even for zero-cost or empty batches. Direct
kernel `cache_mode` records intent only. The isolated launcher configures cold/warm
cache clones, freezes grader/results/cases/fixture bytes, verifies the two exact eval
cells and output bindings, and separately collects the settled session usage ledger.
Per-item route/cache provenance stays unknown when the host does not supply it.

```sh
python3 evals/grade_launch.py --results /tmp/cand.json --cases evals/cases.json --out /tmp/cand.graded.json --cache-mode cold --concurrency 2
```

`evals/extractor-corpus.json` is source-adjudicated **synthetic** evidence, not a
human study. In the eval kernel after loading `grade.py`, run
`await validate_extractor_host("evals/extractor-corpus.json", concurrency=2)` to
measure facet confusion, omissions and unsupported recommendations. `calibration_effect`
compares initial/final decisions against separately supplied source truth; agreement
with a reference model remains a different metric. Off-inventory invented findings
remain unaudited unless a separately bound audit is supplied. `recommended_defects`
counts recommendations, never observed code repairs.

```python
%load evals/grade.py
await grade("/tmp/cand.json", "evals/cases.json")
```

Use the same case file that produced the captured answers. Omissions and extraction
failures remain visible; model-extracted assertions are not independent quality proof.
Recorded artifacts in `evals/results/` describe their captured revisions and conditions,
not current quality guarantees.

`evals/holdout.json` is sealed. Do not read or edit its prompts while tuning the skill
or eval machinery. Once used for tuning, it is no longer a holdout.

## License

MIT
