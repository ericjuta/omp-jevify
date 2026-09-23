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
omp plugin install 'git+https://github.com/ericjuta/omp-jevify.git#v0.1.2'
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
error and fallback-model flags; slow-model calibration; dry-run-by-default span deletion).
See [`skills/jevify/SKILL.md`](skills/jevify/SKILL.md) for the workflow, the exact `jv` API,
measured harness facts and recipes.

## Requirements

- OMP with the eval kernel's `judge_batch`, `judge` and `completion`.
- `rg` and `git` for the diff and search extractors.
- `uv` (or pip) for `jv.ast_units`, which installs `ast-grep-py` on first use into
  `${XDG_CACHE_HOME:-~/.cache}/jevify/` (the kernel's Python may have no pip).

## Development

`python3 -m unittest discover -s tests` runs the helper tests (no model calls; needs `git`,
`rg`, and `uv` for the ast-grep test). CI runs them on every push.

`evals/run.py` measures whether the skill triggers. Each case in `evals/cases.json` runs as
a fresh `omp --mode json` session in a generated fixture repository
(`evals/fixture.py`); prompts state a goal and never name the skill. The skill under test
is pinned with a one-shot `--config` overlay, so the installed plugin does not interfere.

```sh
python3 evals/run.py --ref v0.1.0 --out /tmp/base.json          # a released version
python3 evals/run.py --out /tmp/cand.json --compare /tmp/base.json   # working tree
```

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
- `infra`: sessions with no tool call and no answer after one retry (provider or host
  trouble), excluded from the rates

## License

MIT
