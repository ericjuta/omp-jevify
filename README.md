# omp-jevify

An [Oh My Pi](https://github.com/can1357/oh-my-pi) plugin that ships one skill, `jevify`:
rubric-first bulk classification in the eval kernel with `judge_batch` (Jev).

Write the questions first, pull out every item, try the questions on a small sample, then
freeze them and let Jev judge every item in one complete pass. Check the result against
a slower model, then act only on flagged items. It generalises Can Bölük's Jevict
([thread](https://x.com/_can1357/status/2102524777776677104)) beyond test pruning:

- does each changed section of a commit or PR match its stated purpose
- which call sites still need changing after a refactor
- triage of compiler diagnostics, sanitized logs and review findings
- test pruning (use a `jevict` skill for the full rubric, if you have one)
- turning a frozen rubric into a check that runs while code is written

Use it for roughly 20+ similar items, each needing the same question answered. Scores are
one model's verdicts, not proof: read flagged items before acting on them.

## Install

```sh
omp plugin install 'git+https://github.com/ericjuta/omp-jevify.git#v0.1.4'
```

Start a new OMP session afterwards. There is nothing else to set up:

- Ask for the kind of job it is for ("go through every changed hunk in this commit and tell
  me which aren't explained by the message", "check every call site of X…") and the model
  picks the skill from its description.
- Or include the word `jevify` in the prompt. OMP's built-in `jevify` magic keyword adds
  its own instructions for that turn, and the skill is picked up alongside.
- Or type `/skill:jevify` to inject it directly.

The model loads the helpers itself; by hand, in the Python eval kernel:

```python
%load skill://jevify/jevify.py
```

This defines `jv` (extraction from diffs, `rg` and ast-grep; batch runs with a retry;
Jev-token state sizing; error and non-primary-model flags; slow-model calibration;
dry-run-by-default span deletion).
See [`skills/jevify/SKILL.md`](skills/jevify/SKILL.md) for the workflow, the exact `jv` API,
measured harness facts and recipes.

## Requirements

- OMP with the eval kernel's `judge_batch`, `judge` and `completion`.
- `omp toks` (omp ≥ 18.3.0) for sizing large states in Jev tokens; without it `jv.states`
  warns that they are unmeasured.
- `rg` and `git` for the diff and search extractors.
- `uv` (or pip) for `jv.ast_units`, which installs `ast-grep-py` on first use into
  `${XDG_CACHE_HOME:-~/.cache}/jevify/` (the kernel's Python may have no pip).

## Development

`python3 -m unittest discover -s tests` runs the helper and answer-key tests (no model calls;
needs `git`, `rg`, and `uv` for the ast-grep test). CI runs them on every push.

`evals/run.py` measures whether the skill triggers and saves each session's answer. Each case
in `evals/cases.json` runs as a fresh `omp --mode json` session in a generated fixture
repository (`evals/fixture.py`); prompts state a goal and never name the skill. A one-shot
`--config` overlay sets the arm for that process only, so the installed plugin does not
interfere: `--arm skill` (default) pins the skill under test, `--arm none` hides jevify and
leaves every other skill available. `--omp` picks the `omp` binary (default: the one on
`PATH`); `--thinking` pins the thinking level (default: the configured one).

```sh
python3 evals/run.py --ref v0.1.0 --out /tmp/base.json          # a released version
python3 evals/run.py --out /tmp/cand.json --compare /tmp/base.json   # working tree
python3 evals/run.py --arm none --omp /path/to/omp --thinking low --out /tmp/none.json
```

A case may set `"scale": N` (default 1) to run in a larger fixture: `fixture.build(root,
scale=N)`, for N up to 16, tells the same story N times over. It has 7N billing modules whose
28N line items the rounding fix all touches, and unexplained drift in about 4% of HEAD's hunks,
varied so that no shared token or grep finds it. It also has 28N review findings, each citing a
function, whose validity depends on that function's code, 400N log lines with 5 + 3N kinds of
ERROR/WARN line (about 18% of them real bugs), and 28N `fetch_rate` call sites. Scale 1 is
byte-identical to the fixture behind every recorded result. `evals/cases-large.json` runs at
scale 12, where reading every item directly is costly: about 340 hunks (a 149 KB, roughly
43k-token `git show HEAD`), 336 findings and 4,800 log lines. Running both arms on it compares
correctness, time and cost:

```sh
python3 evals/run.py --cases evals/cases-large.json --arm skill --max-time 900 --max-calls 60 --out /tmp/large-skill.json
python3 evals/run.py --cases evals/cases-large.json --arm none --max-time 900 --max-calls 60 --out /tmp/large-none.json
```

A case with `"expect": "either"` is a control whose trigger is not scored (`callsite_sweep_large`:
one `rg` answers it, with or without the skill). It passes whatever the model does, is
reported as its own `either` count outside `auto_trigger` and `false_trigger`, and its answer
is still graded.

Results are schema 2: each run records its final `answer`, token `usage` with cost, `secs`,
`calls`, `skill_read` and `judged`; the file records `arm`, `omp` and `thinking`. Each
finished run is also appended at once to `<out>.partial.jsonl`, so a killed runner keeps its
evidence.

`evals/holdout.json` is a sealed set of 12 differently worded prompts (7 should trigger,
5 should not), written by a separate agent that never saw the skill or `cases.json`. Do not
read or edit it while tuning `SKILL.md`; run it only to check a candidate
(`--cases evals/holdout.json`). Once someone tunes against it, it stops being a holdout
and should be replaced.

It needs model access, costs real tokens and varies run to run, so it is run by hand
before releases that change `SKILL.md`, not in CI. Recorded results live in
`evals/results/`. Reported rates:

- `auto_trigger`: goal-only prompts that should use the skill and opened it
- `auto_judged`: of those, how many went on to run a judge batch
- `false_trigger`: prompts that should not use it (summaries, counts, a single-file review,
  a rename, an explanation) but opened it
- `keyword`: the `jevify` keyword case, reported separately
- `either`: controls whose trigger is not scored, reported for reference
- `infra`: sessions with no tool call and no answer after one retry (provider or host
  trouble), excluded from the rates

Each case's `key` names an answer key that `fixture.answer_keys` derives from the fixture
built at the case's scale (`null`: not graded; one fixture per distinct scale).
`evals/grade.py` scores saved answers in OMP's Python eval kernel: Jev
judges each key item as flagged, cleared or not mentioned by the answer, and every run gets
recall, precision and false flags (an empty answer scores recall 0; judge errors are counted
per run). Debatable items (`flag: null`) are not scored. Several files, or a
`.partial.jsonl`, can be graded in one judge batch; each gets a table and a `.graded.json`.

```python
%load evals/grade.py
await grade("/tmp/cand.json", "evals/cases.json")   # writes /tmp/cand.graded.json
await grade(["/tmp/skill.json", "/tmp/none.json"], "evals/holdout.json")
```

Verdicts are one model's. On synthetic answers about 1 item in 200 was misjudged, always as
a false flag on a look-alike of a flagged item (same file, same line number in another file,
or same claim).

## License

MIT
