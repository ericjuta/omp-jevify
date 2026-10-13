---
name: jevify
description: "Assess suitability when explicitly asked to use Jev/jevify, or for repeated independent semantic classifications that deterministic tools cannot decide. Choose a method first; neither item count nor loading this skill requires judgment. Not for summaries, counts or plain edits."
metadata:
  version: 0.3.1
---

# Jevify

Jev is an optional classifier, not evidence of correctness or permission to edit.
An explicit Jev request still needs a suitability decision; explain a better method
when deterministic tools or direct reading answer the actual question more reliably.

## Choose the method

- Prefer a compiler, search, AST rule or short script when a rule decides the answer.
- Prefer direct reading for small jobs or tightly cross-dependent reasoning.
- Consider Jev for repeated, independently judgeable semantic decisions with the
  necessary shared context. Repetition or list length alone is not a reason to use it.
- Loading this skill or its helper does not require a judge call. If choosing Jev,
  pilot before relying on results, including for read-only answers.

Optional Python helper: `%load skill://jevify/jevify.py`.
Read [the on-demand reference](skill://jevify/reference.md) only for needed helper
APIs, extraction details or existing recipes; it is not a second mandatory workflow.

## If choosing Jev

1. Declare the question, source scope and unit boundaries. Define mutually exclusive
   labels, including uncertainty, against a concrete contract before judging data.
   Set escalation/action thresholds before the pilot; do not tune them to obtain
   desired outcomes. Scores and threshold estimates are not correctness guarantees.
   Default escalation: top choice probability below 0.7, unknown labels, and bool
   probabilities in 0.3–0.7; name additional flag labels in the rubric up front.
2. Check extraction coverage and context. Map independent source hits to units;
   matching totals alone do not prove coverage. Account for filters, dropped/empty
   states, truncation and oversize inputs. Include relevant helpers, siblings and
   consumers; sanitize logs and never submit credentials or keys.
   `jv.plan` checks full-request budgets; `jv.pack` groups without silent truncation.
   Bind source hashes and exclusions with `jv.extraction` before relying on coverage.
3. Pilot the chosen representation. Read flagged sources and near misses, compare
   verdicts with evidence, and repair misread wording or missing context. Rejudge the
   pilot after changes; abandon Jev if the judgments remain unsuitable.
4. Freeze the rubric/questions and preserve their hash for full and after-action
   runs. A post-freeze wording change invalidates prior verdicts: re-freeze and
   rejudge the affected declared sweep, not just the flags.
5. When doing a sweep, judge every unit in the declared scope and reconcile every
   missing result, including originals dropped before grouping. A pilot is not
   complete coverage. Check the run's bound receipt as well as row flags; an empty
   flag list cannot prove completeness. Retain errors, fallback routes and unknowns
   as unresolved, never as safe labels; rerun or inspect their sources.
6. Persist the frozen questions/hash, source-to-unit mapping, rendered states and
   group slots, batch IDs, returned rows and receipt before discarding evidence.
   Use `bundle_path` for checkpointed `jv.run` recovery and `jv.resume` after a kernel
   loss. Recovery reconciles saved items and a surviving host batch; stale or missing
   batches remain unresolved unless resubmission is explicitly requested. This is
   not exactly-once execution; see the reference for crash and host-session limits.

## Confirm before action

- Read every flagged source in full, not just a truncated render or score; confirm
  or overturn the claim from reachable behavior and the governing contract.
  Escalate uncertainty, near-threshold ambiguity, errors, explicit fallback rows
  and unknown provenance. Stock OMP through 18.9.1 does not expose the route/cache fields
  needed to resolve provenance; a familiar model label is not a substitute.
- Calibrate before bulk edits or destructive action; read-only calibration is
  optional. Model agreement is an estimate, not source confirmation or authority.
  Poor agreement calls for revisiting the pilot/context, not relaxing thresholds.
- Grouping is a separate instrument, not an interchangeable cheaper flat run.
  Pilot grouped judgments separately; calibrate them separately before bulk or
  destructive action. Do not mix grouped and flat rows in one decision.
- Act only on confirmed items within granted authority. Review authority is not
  deletion authority. Dry-run deletable spans, preserve dirty/unrelated work, and
  use LSP references for exported-symbol refactors. Delegate exact confirmed IDs
  for edits rather than asking editors to rediscover candidates.
- After edits, re-extract and remap coverage, then rejudge with the same frozen
  questions. Unresolved flags/errors/unknowns or uncovered hits prevent a clean
  closure claim. Receipt completeness is independent of row flags and source
  coverage; a zero-unit extraction is not a model verdict.

## Report honestly

Report the chosen method and declared scope; extracted/covered, dropped/truncated,
piloted/judged and manually reviewed counts; errors, models/fallbacks, flags,
calibration if performed, receipt completeness, and changed/remaining items.
Separate judge estimates from source-confirmed conclusions. Say when the result is
pilot-tuned or sampled, not a full read; do not claim complete coverage or successful
action without evidence.
