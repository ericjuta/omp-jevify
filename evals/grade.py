"""Outcome grading for evals/run.py results: did each saved answer get the fixture's items right?

Load it in OMP's Python eval kernel, which provides judge_batch:

    %load /abs/path/to/evals/grade.py
    graded = await grade("/tmp/cand.json", "evals/cases.json")
    both = await grade(["/tmp/skill.json", "/tmp/none.json"], "evals/holdout.json")

A case's "key" names an answer key from fixture.answer_keys, computed on a freshly built
fixture at the case's "scale" (default 1), one fixture per distinct scale. For every run whose
case has a key and whose answer is not empty, Jev judges each key
item with a known truth (flag True or False; None items are skipped) as flagged, cleared or
not_mentioned by the answer. A run scores tp/fp/fn over its judged items, recall (None when the
key flags nothing), precision (None when the answer flags nothing) and false_flags (= fp, the
number to watch when the key flags nothing). An empty answer flags nothing and is counted. A
failed judgment is left out of the run's counts and reported in its judge_errors and errors.
Several results files (run.py's JSON output, or its .partial.jsonl) share one judge batch;
each gets a table and a graded file beside it (cand.json -> cand.graded.json).
"""

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

_GRADE_DIR = Path(__file__).resolve().parent
GRADE_LABELS = ("flagged", "cleared", "not_mentioned")
GRADE_QUESTIONS = {"verdict": {
    "type": "choice",
    "instructions": (
        "state.answer is the final answer an assistant gave to a task about a small billing code "
        "repository. state.item is one item that task covers (a changed hunk, function, call site, "
        "docstring, kind of log line or review finding), with its location and code. Decide what "
        "state.answer says about this exact item, where flagging it means reporting it as "
        "state.flag_label. Many items look alike, so match the item's identity, not its content: a "
        "review finding by its id (F07 is not F17); anything else by its file together with a line "
        "number in the item's range or its function name, or, for a kind of log line, by its message. "
        "Compare line numbers and ids exactly (billing/fees.py:21 is not billing/fees.py:31). "
        "Another item in the same file, the same line number in another file, or another finding "
        "making the same claim is a different item and says nothing about this one. An explicit "
        "list of ids or locations covers only the items it lists; a group statement covers this "
        "item only when the group clearly includes it ('all other call sites', 'every naming "
        "finding', 'the rest'). Flagging part of the item (one of its lines or changes) flags the "
        "item. Judge only what state.answer asserts, not whether it is correct."),
    "criteria": {
        "flagged": ("state.answer reports this exact item as state.flag_label, by its own id or "
                    "location or within a group that clearly includes it, even if hedged "
                    "('probably', 'worth checking')."),
        "cleared": ("state.answer says or clearly implies this exact item is not state.flag_label "
                    "(fine, explained, correct, noise or not worth fixing), by its own id or location "
                    "or within a group that clearly includes it, such as 'the rest'."),
        "not_mentioned": ("state.answer does not identify this item, neither by its own id or location "
                          "nor within a group that clearly includes it."),
    },
}}


def score_items(truth, verdicts):
    """Scores one answer. truth: {item id: True/False}; verdicts: {item id: label} for the items
    that got a verdict (failed judgments are absent)."""
    tp = sum(1 for item, label in verdicts.items() if label == "flagged" and truth[item])
    fp = sum(1 for item, label in verdicts.items() if label == "flagged" and not truth[item])
    fn = sum(1 for item, label in verdicts.items() if label != "flagged" and truth[item])
    return {"tp": tp, "fp": fp, "fn": fn,
            "recall": tp / (tp + fn) if tp + fn else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "false_flags": fp}


def _mean(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize_graded(runs):
    """Means over graded runs; recall and precision skip the runs where they are None."""
    return {"n": len(runs), "empty": sum(run["empty"] for run in runs),
            "recall": _mean(run["recall"] for run in runs),
            "precision": _mean(run["precision"] for run in runs),
            "false_flags": _mean(run["false_flags"] for run in runs),
            "secs": _mean(run["secs"] for run in runs), "cost": _mean(run["cost"] for run in runs),
            "skill_read": _mean(run["skill_read"] for run in runs),
            "judge_errors": sum(run["judge_errors"] for run in runs)}


def _load_runs(path):
    """(top-level fields, runs) of a run.py results file or its .partial.jsonl."""
    text = Path(path).read_text()
    if Path(path).suffix == ".jsonl":
        meta, runs = {}, [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        meta = json.loads(text)
        runs = meta.pop("runs")
    if any("answer" not in run for run in runs):
        raise ValueError(f"{path}: runs have no saved answer; grading needs run.py schema 2")
    return meta, runs


def _fixture_keys(scales):
    """{scale: answer keys} of one freshly built fixture per scale, from the fixture.py next to this file."""
    spec = importlib.util.spec_from_file_location("jevify_eval_fixture", _GRADE_DIR / "fixture.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    with tempfile.TemporaryDirectory(prefix="jevify-grade-") as tmp:
        return {scale: fixture.answer_keys(fixture.build(Path(tmp) / f"repo-{scale}", scale=scale))
                for scale in sorted(set(scales))}


async def _judge(states, timeout):
    """({state key: label}, {state key: error}, batch status); every state lands in one of the two."""
    judge_batch = globals().get("judge_batch")
    if judge_batch is None:
        raise RuntimeError("grade() needs judge_batch: %load this file in OMP's Python eval kernel")
    batch = judge_batch(states, GRADE_QUESTIONS, intent="Grading eval answers")
    problem = f"no verdict within {timeout}s"
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await batch.drain(min(60.0, max(1.0, deadline - time.monotonic())))
            except Exception as error:  # drain raises only when the whole run died
                problem = f"judge run failed: {error}"
                break
            status = batch.status()
            if status.get("done", 0) >= len(states) or not status.get("running", False):
                break
        answers, failures, status = batch.results(), batch.failed(), batch.status()
        if len(answers) + len(failures) < len(states):
            batch.cancel()
    finally:
        batch.close()
    labels, errors = {}, {}
    for key in states:
        verdict = (answers.get(key) or {}).get("verdict")
        choice = verdict.get("choice") if isinstance(verdict, dict) else None
        if choice in GRADE_LABELS:
            labels[key] = choice
        elif key in failures:
            errors[key] = str(failures[key])
        else:
            errors[key] = f"invalid verdict: {verdict!r}" if key in answers else problem
    return labels, errors, status


def _cell(value, width, digits=2):
    return f"{'-' if value is None else f'{value:.{digits}f}':>{width}}"


def _print_table(result, target):
    setup = " · ".join(f"{field} {result[field]}" for field in ("arm", "omp", "thinking") if result.get(field))
    print(f"{result['results']}{' · ' + setup if setup else ''} -> {target}")
    rows = [*result["summary"]["per_case"].items(), ("overall", result["summary"]["overall"])]
    cw = max(len(case) for case, _ in rows)
    kw = max(3, *(len(s.get("key", "")) for _, s in rows))
    print(f"  {'case':{cw}} {'key':{kw}} {'n':>3} {'empty':>5} {'recall':>6} {'prec':>5} {'false':>5} "
          f"{'skill':>5} {'secs':>6} {'cost':>6} {'jerr':>4}")
    for case, s in rows:
        print(f"  {case:{cw}} {s.get('key', ''):{kw}} {s['n']:>3} {s['empty']:>5} {_cell(s['recall'], 6)} "
              f"{_cell(s['precision'], 5)} {_cell(s['false_flags'], 5, 1)} {_cell(s['skill_read'], 5)} "
              f"{_cell(s['secs'], 6, 1)} {_cell(s['cost'], 6, 3)} {s['judge_errors']:>4}")


async def grade(results_path, cases_path, out_path=None, *, timeout=1800):
    """Grade a run.py results file, or a list of them in one judge batch (see the module doc).

    Returns the graded result (a list of them for a list of paths) and writes each one to
    out_path, by default beside its results file (cand.json -> cand.graded.json).
    """
    single = isinstance(results_path, (str, os.PathLike))
    paths = [Path(results_path)] if single else [Path(path) for path in results_path]
    if out_path is not None and not single:
        raise ValueError("out_path needs a single results file")
    cases = json.loads(Path(cases_path).read_text())["cases"]
    key_of = {case["id"]: case.get("key") for case in cases}
    scale_of = {case["id"]: case.get("scale", 1) for case in cases}
    keys = _fixture_keys(scale_of[cid] for cid, key in key_of.items() if key is not None)
    unknown = sorted({key for cid, key in key_of.items() if key is not None and key not in keys[scale_of[cid]]})
    if unknown:
        raise ValueError(f"{cases_path}: no answer key named {unknown}")

    files, states, slots = [], {}, {}
    for path in paths:
        meta, runs = _load_runs(path)
        missing = sorted({run["case"] for run in runs} - set(key_of))
        if missing:
            raise ValueError(f"{path}: cases missing from {cases_path}: {missing}")
        planned = []
        for run in runs:
            key = key_of[run["case"]]
            if key is None:
                continue
            case_keys = keys[scale_of[run["case"]]][key]
            answer = (run.get("answer") or "").strip()
            verdicts, errors = {}, {}
            for item in case_keys["items"]:
                if item["flag"] is None:
                    continue
                if not answer:
                    verdicts[item["id"]] = "not_mentioned"
                    continue
                state = str(len(states))
                states[state] = {"answer": answer, "item": item["desc"], "flag_label": case_keys["flag_label"]}
                slots[state] = (verdicts, errors, item["id"])
            planned.append((run, key, answer, verdicts, errors))
        files.append((path, meta, planned))

    labels, failures, status = await _judge(states, timeout) if states else ({}, {}, {})
    for state, (verdicts, errors, item) in slots.items():
        if state in labels:
            verdicts[item] = labels[state]
        else:
            errors[item] = failures[state]

    results = []
    for path, meta, planned in files:
        graded = []
        for run, key, answer, verdicts, errors in planned:
            case_keys = keys[scale_of[run["case"]]][key]
            truth = {item["id"]: item["flag"] for item in case_keys["items"] if item["flag"] is not None}
            graded.append({"case": run["case"], "rep": run.get("rep"), "key": key, "empty": not answer,
                           **score_items(truth, verdicts), "judge_errors": len(errors),
                           "skill_read": run.get("skill_read"), "judged": run.get("judged"),
                           "secs": run.get("secs"), "cost": (run.get("usage") or {}).get("cost"),
                           "verdicts": verdicts, "errors": errors})
        per_case = {}
        for row in graded:
            per_case.setdefault(row["case"], []).append(row)
        result = {
            "schema": 1, "results": str(path), "cases": str(cases_path),
            **{field: meta.get(field) for field in ("arm", "omp", "thinking", "model")},
            "judge": {"model": status.get("model"), "batch_items": len(states), "batch_cost": status.get("cost")},
            "summary": {"overall": summarize_graded(graded),
                        "per_case": {case: {"key": rows[0]["key"], **summarize_graded(rows)}
                                     for case, rows in sorted(per_case.items())}},
            "runs": graded,
        }
        target = Path(out_path) if out_path is not None else path.with_suffix(".graded.json")
        target.write_text(json.dumps(result, indent=1) + "\n")
        _print_table(result, target)
        results.append(result)
    return results[0] if single else results
