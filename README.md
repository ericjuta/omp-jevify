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
omp plugin install 'git+https://github.com/ericjuta/omp-jevify.git#v0.1.0'
```

Start a new OMP session afterwards. The skill appears as `jevify`; in the Python eval kernel
load its helpers with:

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

## License

MIT
