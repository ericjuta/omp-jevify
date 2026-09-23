---
name: jevify
description: "Use when asked to go through, check, triage or classify every item in a long list and say which ones stand out: every changed hunk or file in a commit or PR (which aren't explained by its message), every call site after a refactor (which still need changing), every log line, compiler diagnostic, review finding or test. Also on the word jevify. Judges each item with the eval kernel's judge_batch (Jev) against questions frozen up front, with %load-able jv helpers. Not for summaries, counts, single-file reviews or plain edits."
---

# Jevify

Rubric first: Jev scores EVERY unit in a complete pass, not repeat-until-no-changes subagent
sweeps. The orchestrator decides and reads only flagged units; subagents edit, never scan.
This generalises Can Bölük's Jevict ([thread](https://x.com/_can1357/status/2102524777776677104);
`skill://jevict` if installed) beyond tests; scores are not proof.

Contents: use · 0–8 loop · groups · API · facts · recipes A–F · write-time rule · guardrails.

## When to use

- Use for roughly 20+ homogeneous items: changed hunks, migration calls, existing
  diagnostics, sanitized logs, findings or test units. Below ~20, read them directly.
- Use a compiler, grep count, ast-grep rule or test instead when it answers deterministically.
  Cross-unit reasoning needs sibling/group context; otherwise read directly.

## 0. Load the helper

In the Python kernel, `%load` defines `jv` and prints its helper names; `judge_batch`,
`completion` and `wait` come from the kernel. Re-`%load` resets `jv.frozen`/`jv.stats`.
Never `%pip`: the kernel Python may lack pip, so `jv.ast_units` installs `ast-grep-py`
through `jv.dep`. The notice's `wait(judge(...))` example fails here.

```python
%load skill://jevify/jevify.py
```

## 1. Decide the rubric before seeing data

Write questions against a concrete contract, before data. Labels are mutually exclusive;
`UNKNOWN` needs investigation. Fix the 0.7 policy now, not after seeing pilot scores.

```python
TARGET = "legacyApi"
paths = ["**/*.ts", "**/*.tsx"]
SKIP_PREFIX = "generated/"  # deterministic pre-filter; keep both probes aligned
STATE_CAP = jv.CAP
Q = {"migration": jv.choice(
    "For this call site, decide whether it still depends on legacyApi's old behavior. "
    "A renamed alias or wrapper is not a successful migration; missing context is UNKNOWN.",
    NEEDS_CHANGE="The old behavior or obsolete contract still reaches a consumer.",
    SAFE="The call is already migrated or is unrelated despite its name.",
    UNKNOWN="The available unit cannot prove either conclusion.")}
ACTION_AT = 0.7
def need(r):
    top = max(r.get(f"migration.{k}", 0) for k in ("NEEDS_CHANGE", "SAFE", "UNKNOWN"))
    if top < ACTION_AT or r.get("migration") == "UNKNOWN": return "uncertain"
    return "obsolete call" if r.get("migration.NEEDS_CHANGE", 0) >= ACTION_AT else False
```

## 2. Extract units and check coverage

Use `git_units` for diffs, `rg_units` for hits, `ast_units` for syntax, `line_units`
for signatures or Jevict's parser for tests. Count naive heads against parser units
in the same paths; inspect unmapped comments/strings versus real missed calls.
Equal totals alone prove nothing.

```python
raw = jv.ast_units(f"{TARGET}($$$ARGS)", lang="typescript", globs=paths)
units = {k: u for k, u in raw.items() if not u["file"].startswith(SKIP_PREFIX)}
heads = {k: h for k, h in jv.rg_units(TARGET, fixed=True, globs=paths).items()
         if not h["file"].startswith(SKIP_PREFIX)}
print(f"pre-filter removed={len(raw)-len(units)}")
uncovered = [(h["file"], h["line"]) for h in heads.values() if not any(
    u["file"] == h["file"] and u["line"] <= h["line"] <= u["end_line"]
    for u in units.values())]
print(f"naive={len(heads)} mapped={len(heads)-len(uncovered)} units={len(units)}", uncovered[:20])
```

## 3. Build states, spot-check extraction

A state is per-unit text/JSON. `jv.states` caps strings and drops empties (one
rejects a batch); `jv.MAX_STATE` warns on overflow. Inspect boundaries; use `render`
to attach helpers/siblings when AST call text lacks context (`jv.hunk_states` does
this for diff hunks).

```python
states = jv.states(units, cap=STATE_CAP)
print(f"extracted={len(units)} states={len(states)}", jv.stats["states"])
jv.show(list(states)[:5], states, width=1200, limit=5)
```

## 4. Pilot; edit WORDING, not the threshold

Pilot 40–150 units (for 20–39, read all). Read **every** flag and near misses;
name each misread idiom: helper, span, near-miss, allow-list, transition or duplicate.
Fix Q's **wording**, never the threshold; rejudge the same pilot for precision/recall.
Two or three rounds are normal; after five fix extraction/context. Freeze when satisfied.

```python
pilot = jv.sample(states, n=min(100, len(states)))
prows = await jv.run(pilot, Q, intent="jevify pilot: migration calls")
pflags = jv.flag(prows, need)
jv.show(pflags, pilot, fields=["migration", "migration.NEEDS_CHANGE"],
        width=STATE_CAP, limit=len(pflags))
jv.show([r for r in prows if .5 <= r.get("migration.NEEDS_CHANGE", 0) < ACTION_AT], pilot, limit=20)
```

## 5. Freeze; judge every state

Freeze Q to JSON (12-hex hash), then judge **all** states. The helper drains in
bounded slices, retries errors/undrained keys once and keeps failures as error rows.
A post-freeze wording change invalidates every verdict; re-freeze and re-judge all.

```python
seal = jv.freeze(Q)
rows = await jv.run(states, Q, intent="jevify: complete migration sweep")
print(jv.report(rows, Q, title="Migration call-site sweep"))
```

## 6. Calibrate before bulk or destructive action

Get up to 20 slow-model opinions per choice-probability bin. Agreement should rise
monotonically to ~100% in the top bin and ≥80% in [0.7, 0.9). Flat bins mean a broken
state build; top-bin <80% means return to the pilot and re-judge **all**. The threshold
table estimates true/false flags; empty bins are forward-filled estimates, not proof.
Calibrate before bulk edits/deletion; optional for read-only reports. No file authority.

```python
assert not jv.flag(rows), "Resolve failures/fallbacks first"
cal = jv.calibrate(rows, states, Q, qid="migration", label="NEEDS_CHANGE",
                   agree={"NEEDS_CHANGE"}, per_bin=20,
                   rubric="Migration requires consumer-equivalent behavior, not a rename.")
```

## 7. Escalate exact flagged units and act

`jv.flag` includes errors/fallback models regardless of rule. Read every flagged source
in full (`jv.show` may truncate), including `UNKNOWN`. Give `task` subagents the exact
confirmed ID list grouped by file to **edit, never scan**. Do not act on error/fallback
rows; re-run or read. Deletion needs authority and `jv.delete_spans` dry-run first;
preserve dirty work. Use LSP references for exported-symbol refactors.

```python
flags = jv.flag(rows, need)
jv.show(flags, states, fields=["migration", "migration.NEEDS_CHANGE"], limit=len(flags))
candidates = {}
for r in flags:
    if r["why"] == ["obsolete call"]:
        candidates.setdefault(units[r["id"]]["file"], []).append(r["id"])
print("Review before assigning:", candidates)
```

## 8. Re-extract, re-judge SAME Q, report

After edits, re-extract **and remap naive heads**; re-judge with identical frozen Q.
Closure needs zero flags/errors/fallbacks/unknowns and no uncovered executable hits.
Report counts then reviewed IDs: "judge output tuned on an N-unit pilot, calibrated
on M, not a full read." Zero units is not a model verdict.

```python
again_raw = jv.ast_units(f"{TARGET}($$$ARGS)", lang="typescript", globs=paths)
again = {k: u for k, u in again_raw.items() if not u["file"].startswith(SKIP_PREFIX)}
heads_after = {k: h for k, h in jv.rg_units(TARGET, fixed=True, globs=paths).items()
               if not h["file"].startswith(SKIP_PREFIX)}
uncovered_after = [(h["file"], h["line"]) for h in heads_after.values() if not any(
    u["file"] == h["file"] and u["line"] <= h["line"] <= u["end_line"]
    for u in again.values())]
print(f"after: heads={len(heads_after)} units={len(again)} skipped={len(again_raw)-len(again)}", uncovered_after[:20])
now = jv.states(again)
post = await jv.run(now, Q, intent="jevify: after action") if now else []
assert all(r["qhash"] == seal for r in post)
remaining = jv.flag(post, need)
print(jv.report(post, Q, title="After action"), f"remaining flags={len(remaining)}")
assert not remaining, "Flagged units remain; inspect uncovered heads separately"
```

## Grouped mode: one shared header, multiple per-unit questions

Group independently judgeable units sharing context. `jv.group` chunks by key into
≤8 slots; `jv.run(..., slots=slots)` returns per-unit rows. The header is billed once:
eight units with a 3k-character header cost 5.4× less; a real 646-line sweep (1.6k
header, 111 groups averaging 5.8 units) cost 1.4× less and ran 4× faster. Keep groups
below `jv.MAX_STATE` to avoid silent fallback.

```python
gstates, slots = jv.group(units, by=lambda u: u["file"], k=8,
                          render=lambda u: u["text"], shared=lambda file: {"file": file})
grows = await jv.run(gstates, Q, intent="jevify: grouped call sites", slots=slots)
gflags = jv.flag(grows, need)
```

Grouping is a different instrument, not a cheaper copy: on that sweep grouped and flat
labels agreed 73%, bool P(yes) correlated 0.66, and ≥0.5 flags overlapped 24 of 112;
a slow tie-break on 16 disagreements sided with flat 6, grouped 7, neither 3. Pilot
and calibrate grouped rows separately; never mix grouped and flat rows in one decision.

## Helper reference: `jv` (exact public API)

`jv` owns all public names; units are ordered `{id: unit}` with collisions `#2`, `#3`.

| Member / signature | Contract |
| --- | --- |
| `jv.CAP = 12_000` | Maximum characters per string field in a state. |
| `jv.MAX_STATE = 100_000` | Soft serialized-state warning; oversize may fall back. |
| `jv.SEED = 11` | Default deterministic sampling seed. |
| `jv.EDGES = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.01)` | Default left-closed probability bins. |
| `jv.stats = {"states": {...}, "runs": [...]}` | Built-state and run statistics. |
| `jv.frozen = None \| qhash` | Last frozen question hash. |
| `jv.choice(instructions, criteria=None, **labels) -> dict` | Choice question; at least two labels via dict or kwargs. |
| `jv.yes(instructions, *, true=None, false=None) -> dict` | Bool question; optional explicit criteria. |
| `jv.scale(instructions, *levels) -> dict` | Score question; ≥2 levels, lowest to highest. |
| `jv.qhash(Q) -> str` | First 12 hex of SHA-256 of sorted-key JSON questions. |
| `jv.freeze(Q, path="/tmp/jevify-questions.json") -> str` | Write Q as JSON, set `jv.frozen`, return qhash. |
| `jv.git_units(rev, *, by="file"\|"hunk", cwd=".", paths=None, context=3)` | Diff range `A..B`/`A...B`, `WORKTREE`, `STAGED`, or single commit (merges vs first parent). |
| `jv.rg_units(pattern, *, cwd=".", globs=None, context=3, fixed=False, case=True, max_units=50_000)` | `rg --json -n -C`, no stdin; no matches `{}`, over cap raises. |
| `jv.ast_units(pattern=None, *, lang, rule=None, cwd=".", globs=None)` | Exactly one ast-grep pattern or rule dict (`kind`/`regex`/`has`/`inside`/`any`/`not`); installs via `jv.dep`. |
| `jv.dep(dist, module=None) -> module` | Import; else `uv pip install --target $XDG_CACHE_HOME/jevify/pyX.Y` (pip fallback), then import. |
| `jv.line_units(source, *, keep=None, drop=None, normalize=True, examples=3)` | Path/text/lines → grouped normalized signature counts. |
| `jv.states(units, render=None, *, cap=None) -> {id: state}` | Render str/dict, recursive cap; drop empty; print/record counts. |
| `jv.hunk_states(units, *, siblings=6, users=3, width=700, cap=None)` | Hunk states plus nearest same-file hunks by line distance and hunks using names it defines. |
| `jv.group(units, *, by, k=8, render=None, shared=None, cap=None) -> (gstates, slots)` | Shared state, ≤k per group; slots map gid to original ids. |
| `jv.sample(states, n=60, *, seed=None) -> dict` | Sorted ids, seeded random subset. |
| `await jv.run(states, Q, *, intent, concurrency=32, retries=1, timeout=1800, slots=None) -> list[row]` | Drain/retry once; flat rows, grouped expansion when slots given. |
| `jv.slot_questions(Q, k) -> dict` | Copy Q into per-slot `qid@sN` questions; absent slots discarded. |
| `jv.flag(rows, rule=None, *, primary=None) -> list[row]` | Copies with `why`; always errors/fallbacks, plus truthy rule. |
| `jv.show(rows_or_ids, states, *, fields=None, width=1200, limit=30)` | Print verdict fields and state, truncate to width. |
| `jv.tally(rows, key, *, edges=None) -> dict` | Counts labels or numeric histogram; prints table. |
| `jv.calibrate(rows, states, Q, *, qid, label, agree=None, per_bin=20, edges=None, model="slow", rubric="", seed=None) -> dict` | Slow choice agreement and threshold estimates. |
| `jv.save(rows, path="/tmp/jevify-rows.jsonl") -> path` | Persist flat JSONL rows. |
| `jv.load(path) -> rows` | Recover JSONL rows. |
| `jv.delete_spans(units, ids, *, root=".", dry_run=True) -> {file: lines_removed}` | Merge spans; remove bottom-up, plus one trailing blank line. |
| `jv.report(rows, Q, *, title="jevify") -> str` | Markdown counts, model/qhash, label distribution, state/run summaries. |

`git_units`: no-color/no-ext-diff, paths after `--`; file id `path` →
`{file,status,diff}`, hunk id `path:+NEW_START` (or `-OLD_START`) →
`{file,status,header,hunk,line,old_line}`; binary/rename/mode-only → `path:meta`.
`rg_units`: explicit `.` and DEVNULL stdin; `path:line` → `{file,line,match,context}`.
`ast_units`: `rg --files`, `path:line` → `{file,line,end_line,text}` (1-based); `.tsx`/`.jsx`
parse as tsx/jsx and files with parse errors are still searched. A `def`/`function` pattern
starts at the keyword (decorators excluded) and also matches nested definitions; use a
`rule` (recipe F) for deletable spans.
`hunk_states`: `{file,status,hunk,nearest_sibling_hunks,hunks_using_names_defined_here}`;
users come from names defined at the hunk's outermost indentation, dunders excluded.
`line_units`: UUID/hex/timestamp/number-normalized `sha1(signature)[:10]` →
`{signature,count,examples}`, most frequent first.

| `jv.run` flat row field | Meaning |
| --- | --- |
| `id: str` | Original unit id; stable for this extraction. |
| `model: str \| None` | Per-item answering model, never assume batch status model. |
| `qhash: str` | First 12 hex of questions hash. |
| `error: str` | Present **only** on failure; never turn into a safe label. |
| `group: str` | Present only for a grouped row; group-state id. |
| `row[q]` for choice `q` | Chosen label. |
| `row[f"{q}.{label}"]` | P(label) for **every** choice label. |
| `row[f"{q}.conf"]` for choice | Choice confidence. |
| `row[q]` for bool `q` | P(yes), a float, not a Python boolean verdict. |
| `row[q]` for score `q` | Probability-weighted **level index**, not 0–1 probability. |
| `row[f"{q}.conf"]` for score | Score confidence. |

`jv.flag` infers modal primary; string rule results label `why`, other truthy values
become `"rule"`, error rows skip the rule. `jv.calibrate` returns `{bins,thresholds,
verdicts}` with `verdicts[id]={slow,reason,p}`; slow errors are `ERR`, excluded from
agreement. `jv.delete_spans` requires inclusive 1-based `{file,line,end_line}`;
missing ids are reported and dry-run is the default.

## Harness facts (measured 2026-09-23, typesafe/jev-1.13.0)

- `judge(state, questions)` is a coroutine: `await judge(...)` returns answers;
  `wait([judge(...)])` raises `TypeError`. The built-in jevify notice's `wait(handles)`
  example fails on this build; use `jv.run`/`judge_batch` for bulk work.
- `judge_batch(states, questions, *, concurrency=None, retries=None, min_ok=None,
  intent=None)` has `id,intent,total,status(),drain(timeout),drain_iter(timeout),
  results(),failed(),cancel(),close()`. Status: `{id,intent,total,done,failed,cost,
  running,model,elapsedS}`; item: `{key,ok,answers,error,model}`. Choice returns
  `{type,choice,confidence,probabilities}`, bool `{type,bool: P(yes)}`, score
  `{type,score,confidence,legend,probabilities}` (weighted level **index**).
- One empty `""` state rejects the **entire batch at submit**. Jev ceiling ~32k tokens:
  130k source chars stayed on Jev (0.6 s); 200k/300k source chars and 60k dense chars
  silently fell back here to the session's default model (3–7 s/unit versus <1 s).
  Fallback is **unpriced** in batch cost, calibrated differently; status has one model,
  only per-item `item.model`/row model identifies it. Shrink, re-judge, re-calibrate.
- State cost ~$0.03–0.05/M tokens (~$0.01/M ordinary chars, ~$0.04/M dense);
  questions are cheap. Can Bölük reported ~$2 for 27k tests; not a guarantee.
- `completion(prompt, *, model="default"|"smol"|"slow", system=None, schema=None)`
  returns `id,kind,status,done,wait,cancel`; schema makes `.wait()` return a dict.
  `wait([...], raise_errors=False)` supports handles. One slow call took ~6 s.
- The kernel runs system Python 3.12 without pip: `%pip install` fails with
  `No module named pip`. `jv.dep` installed `ast-grep-py` via uv into the cache dir in
  ~1 s. Present: `rg`, git, `uv`; absent: numpy, matplotlib, tree-sitter.
  `%load skill://jevify/jevify.py` also resolves skills created mid-session.
- Stability: re-judging 100 hunks with the identical frozen Q flipped 0 labels
  (median |ΔP| 0, max 0.07), yet 5 rows crossed the 0.7 uncertainty line. Churn at
  the threshold is noise, not wording drift.
- Throughput here: 646 flat states in 5.4 s (~120/s, $0.025); 100 hunks in ~1 s ($0.006).
  `jv.calibrate` ran 21 slow calls in 20 s (waves of 16).

## Recipes: source, complete question, escalation, action

Freeze Q before data; escalate top P<0.7, ambiguous bools (0.3–0.7), failures/fallbacks and named labels; labels grant no authority.

### A. Diff scope audit (commit, PR, branch, or worktree)

Source: `jv.git_units(rev, by='hunk')` (keep binary/meta units) and the **full**
commit message or PR body as the boundary, not its title. Give every declared purpose
an operational label (what its hunks look like) plus `undeclared` and `mixed`. Render
with `jv.hunk_states`: a setup, helper or parametrization hunk serves the purpose its
tests assert, which only neighbouring hunks reveal.

```python
import subprocess
REV = "HEAD"  # or 'origin/main...HEAD' with MESSAGE from `gh pr view --json body`
MESSAGE = subprocess.run(["git", "log", "-1", "--format=%B", REV],
                         capture_output=True, text=True, check=True).stdout
Q = {"purpose": jv.choice(
    "Assign THIS hunk to the declared purpose it serves. Sibling hunks and hunks using names "
    "it defines are context: helpers, fixtures and parametrizations serve the purpose their "
    "tests assert; a change's own proposal/tasks/metadata serve that change.\n"
    f"MESSAGE:\n{MESSAGE}",
    feature="<declared purpose 1, operationally: its code, tests, docs and reporting>",
    validation_repair="<declared repair: formatter blank lines, type-narrowing asserts, mock fixes>",
    undeclared="Serves none of the declared purposes.",
    mixed="Separable changes serving different purposes, including an undeclared one."),
    "behaviour_undeclared": jv.yes(
        "Does this hunk change runtime (non-test) behavior for a reason OTHER than the declared "
        "purposes? A behavior change that implements a declared purpose answers no."),
    "debug_leftover": jv.yes("Does this hunk add debug output, commented-out code or TODOs?")}
units = jv.git_units(REV, by="hunk")
states = jv.hunk_states(units)
```

Escalate `undeclared`/`mixed`, top label P < 0.7, either bool ≥ 0.5 and errors/fallbacks;
read every flag in full. Measured on a 100-hunk, three-purpose commit: a title-only
in/out-of-scope rubric flagged 41–42 hunks (declared fixture repairs, a package upgrade,
change metadata); declared purposes plus `hunk_states` flagged 21–24. A double-negative
behaviour question sat at P≈0.5 on 7 core hunks until reworded as above. Fast
`undeclared` at P 0.5–0.9 matched the slow model on 1 of 6; the slow model matched hand
reading on 8 of 8 reviewed flags, leaving one truly undeclared spec reflow. Preserve user
work, repair authorized scope, re-extract against the same base and re-judge SAME Q.

### B. Refactor or migration call-site completeness

Source: LSP references first for exports; `rg_units` or `ast_units` plus a naive
grep count. Include aliases/wrappers; syntax matches alone cannot prove completeness.

```python
Q = {"migration": jv.choice(
    "Classify THIS call against the new API contract; a rename alone is not migration.",
    migrated="New contract used with equivalent consumer-visible behavior.",
    needs_change="Obsolete contract or lost required behavior remains reachable.",
    not_applicable="Lookalike hit not governed by this migration.",
    unsure="Alias, type or surrounding context is insufficient.")}
units = jv.ast_units("oldApi($$$ARGS)", lang="typescript")
```

Escalate `migration.needs_change ≥ 0.7`, `unsure`, errors/fallbacks; read consumers.
Fan confirmed IDs by file for edits; re-extract/re-judge SAME Q to zero residual heads.

### C. Existing compiler/diagnostic output triage

Source: parse **captured** tsc/eslint/cargo output into one unit per diagnostic
`{file,line,code,message,context}`. Preserve the location and neighboring source;
`line_units` alone groups away locations. Example below parses tsc; adapt the parser
for eslint/cargo, without running diagnostics solely for this sweep.

```python
import re
from pathlib import Path
Q = {"origin": jv.choice(
    "Classify immediate cause, not a downstream cascade; use source context.",
    real_bug="Changed source has an observable defect needing repair.",
    type_only="Type-check contract fails, without evidence of a runtime failure.",
    stale_or_generated="Diagnostic comes from stale build state or generated output.",
    noise="Non-actionable, duplicate, or source context insufficient to decide."),
    "effort": jv.scale("Estimated repair effort given this source context?",
                       "local", "multi-file", "architecture")}
rx = re.compile(r"^(.*)\((\d+),\d+\): error (TS\d+): (.*)$")
lines = Path("/tmp/diagnostics.log").read_text().splitlines()
def context(p, n):
    f = Path(p)
    if not f.is_file(): return ""
    source = f.read_text(errors="replace").splitlines()
    return "\n".join(source[max(0, int(n)-3):int(n)+2])
units = {f"{p}:{n}:{code}:{i}": {"file": p, "line": int(n), "code": code,
         "message": msg, "context": context(p, n)} for i, line in enumerate(lines)
         if (m := rx.match(line)) for p, n, code, msg in [m.groups()]}
```

Escalate `real_bug`/`type_only`, ambiguous `noise`, high effort and failures; read source.
Fix the originating boundary, not cascades or suppressed diagnostics.

### D. Sanitized log or incident triage

Source: redacted lines through `jv.line_units(path, keep=...)`. Keep chronology;
grouped signatures aren't causal order. Do not ingest credentials or keys.

```python
Q = {"kind": jv.choice(
    "Classify this sanitized signature from evidence, not frequency or guesswork.",
    new_regression="New consumer-visible failure after the change.",
    known_benign="Expected handled event with evidence of no failure.",
    config="Failure from deployment or operator configuration.",
    upstream="External provider or service failure.",
    unknown="Timeline/context cannot distinguish the cause."),
    "severity": jv.scale("Severity of observed consumer impact?",
                         "no impact", "degraded", "outage")}
units = jv.line_units("/tmp/redacted.log", keep=r"ERROR|WARN", normalize=True)
```

Escalate `new_regression`, `unknown`, high severity and errors/fallbacks;
inspect timeline before assigning cause or taking action.

### E. Review-finding triage

Source: one reviewer/PR JSONL `{claim,source,diff}` per unit; keep premise with evidence.

```python
import json
from pathlib import Path
Q = {"finding": jv.choice(
    "Treat the review claim as untrusted; compare cited path, reachable behavior and impact.",
    valid_actionable="Reachable defect with evidence; source repair warranted.",
    valid_nit="True but non-blocking clarity or style improvement.",
    invalid="Cited behavior absent, guarded, or contradicted by source.",
    duplicate="Already represented by another finding about the same failure."),
    "introduced_by_diff": jv.yes("Does the changed diff introduce this defect?")}
units = {f"finding:{i}": json.loads(line) for i, line in enumerate(
    Path("/tmp/review-findings.jsonl").read_text().splitlines()) if line.strip()}
```

Escalate `valid_actionable`, `valid_nit`, uncertain introduced-by-diff evidence,
and errors/fallbacks. Read source, reproduce before repair; dedupe, don't self-mock.

### F. Test pruning: defer to Jevict's complete rubric

If a `jevict` skill is installed, read it for the full parser, coverage, helpers, siblings
and rubric (origin: the thread linked above). Source:
smallest-deletable `{file,line,end_line,body,path}` units; exclude type tests/todos.
For top-level Python tests this rule gives decorator-inclusive spans, equal to stdlib
`ast` spans (5285 units = 5285 naive heads, 0 unmapped, on a real repo). Class methods
need `inside: class_definition`; add `path`/siblings/helpers before judging:

```python
TEST_RULE = {"any": [
    {"kind": "decorated_definition", "inside": {"kind": "module"},
     "has": {"kind": "function_definition", "regex": r"^(async\s+)?def\s+test_"}},
    {"kind": "function_definition", "inside": {"kind": "module"},
     "regex": r"^(async\s+)?def\s+test_"}]}
units = jv.ast_units(lang="python", rule=TEST_RULE, globs=["tests/**/*.py"])
```

Compile this compact Q after preparing those states:

```python
Q = {"verdict": jv.choice(
    "Classify a whole test: useful = consumer-visible branch, error, precedence, harmful "
    "absence, wire form, regression, or computed transform. Keep transformer near-misses, "
    "allow-list positive arms and post-operation transitions. Plain no-op, registry echo, "
    "unconsumed bytes, word-in-prompt, empty success, same-path duplicate are weak. "
    "Separate weak assertions in a useful block are TRIM.",
    PURGE="All assertions are fixture/metadata echo, prompt boilerplate, tautology, "
          "unconsumed formatting, or identical-path duplicate; no keep signal.",
    KEEP="At least one consumer-visible branch/error/precedence/negative/wire/regression "
         "or computed transformation, with no separable purge-only assertion.",
    TRIM="At least one useful assertion and a separable purge-only assertion; retain the unit.")}
```

Escalate `verdict.PURGE ≥ 0.7`, TRIM, errors/fallbacks; read each purge and calibrate.
With deletion authority dry-run `jv.delete_spans(units, ids, dry_run=True)` (verified on
a copy: file untouched, unknown ids reported; the real run removed exactly the chosen
tests with their decorators and left other decorators intact), inspect spans,
apply confirmed deletions, hand-edit TRIM, re-extract/re-judge SAME Q. Assertion helpers
count; an absent keyword alone isn't grounds to delete.

## Promote to a write-time rule

When requested, write `<repo>/.omp/rules/test-value.md` or
`~/.omp/agent/rules/test-value.md`: one yes/no question, gated by `scope`/`condition`.

```markdown
---
description: Warn when a changed test asserts only incidental implementation details
question: Does this test edit add assertions that only echo fixtures, inspect wiring, or check successful completion without pinning a plausible consumer-visible failure?
scope: "tool:edit(*.test.ts), tool:write(*.test.ts)"
condition: "expect\\(|assert"
---
Read the test: replace incidental assertions with a consumer contract, or remove them; preserve useful assertions.
```

TTSR judges `{output,content}` (≤60k chars) **after** completion, never interrupts.
P(yes) ≥ 0.7 delivers the body as a hidden warning; failure delivers nothing.
`ttsr.judge: auto` requires native TypeSafe Jev. Optional `globs`, regex `condition`
or ast-grep `astCondition` gate cost; scopes include `text`, `thinking`, `tool` and
`tool:<name>(<glob>)`. `omp ttsr list` shows questions; `omp ttsr test/scan`
do not exercise the judge. `/omfg` drafts rules; rules do not replace bulk sweeps.

## Guardrails and handoff

- Rubric first; cover extraction before judgment; read flagged source, not just scores.
  Choice probability is not correctness; scale scores are weighted level indices.
- Preserve Q's hash across full/after-action runs; any wording change means re-judge all.
  Inspect errors and fallback rows; never turn them into automatic edits.
- Calibrate before bulk/destructive action, inspect disagreements and dry-run spans.
  Review authority is not deletion authority. Never stash, reset, commit or touch unrelated work.
- Report counts first: extracted/covered, dropped/truncated, piloted/judged, errors,
  models, flags, calibration, changed/remaining; separate estimates from reviewed source.
