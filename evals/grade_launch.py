#!/usr/bin/env python3
"""Launch one grading pass over frozen inputs under the execution cache contract, then verify it.

judge_batch exists only inside OMP's Python eval kernel, so grading runs there. Before the session
starts, the launcher reads grade.py, its fixture sources, the results and the cases once each and
copies those exact bytes into a private scratch directory. The kernel loads the frozen grader and
grades only the frozen inputs, so live edits during a launch cannot change what is graded; any
such edit is reported as a problem. Fixture sources sit beside the frozen grader, so its fixture
imports resolve to the snapshot.

The session gets the cache plan evals/run.py uses for execution: cold means an empty independent
SQLite file, warm means an independent writable clone of one frozen SQLite backup (WAL included).
The clone path reaches the session only through OMP_JUDGMENT_CACHE_DB.

Evidence is host-unattested. A launch is "graded" only when its problems list is empty: the session
reached agent_end without a deadline, malformed events, or a nonzero exit; the eval calls were
exactly the two approved cells in order with no other tool calls; the grading cell finished without
error; and the graded output, ledger and launch descriptor are bound to the frozen input bytes. A
failed launch keeps its outputs and lists every problem. Scratch files are removed on every exit
path. Ledger cache effective stays "unverified"; observed cache counts appear only as reported by
the batch.

  python3 evals/grade_launch.py --results /tmp/cand.json --cases evals/cases.json \
      --out /tmp/cand.graded.json --cache-mode cold --omp "$(command -v omp)"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import grade as grader_module  # noqa: E402  (FIXTURE_SOURCES only; the kernel loads the frozen copy)
import run  # noqa: E402

GRADE = HERE / "grade.py"
CELL_NAMESPACE = "grading"
SESSION_MARGIN_SECONDS = 600
SCRATCH_PREFIX = "grade-launch-"


def build_cells(grader, results, cases, out, *, timeout, concurrency, cache_mode, launch):
    """The two exact eval cells: load the frozen grader, then grade the frozen inputs with the descriptor."""
    load = f"%load {json.dumps(str(grader))}"
    call = (f"await grade({str(results)!r}, {str(cases)!r}, {str(out)!r}, timeout={int(timeout)}, "
            f"concurrency={int(concurrency)}, cache_mode={cache_mode!r}, launch={launch!r})")
    return load, call


def cache_descriptor(plan, requested):
    """What this launch set, without the clone path: the grader's ledger binds to this object."""
    return {"kind": "grading_cache_launch", "requested": requested, "status": plan["status"],
            "isolation": plan.get("isolation"), "initial_sha256": plan.get("initial_sha256"),
            "initial_bytes": plan.get("initial_bytes"),
            "seed_source": plan.get("source"), "snapshot": plan.get("snapshot"),
            "env": "OMP_JUDGMENT_CACHE_DB" if plan["path"] is not None else None,
            "host_attested": False}


def prompt_text(load, call):
    return ("Run exactly these two Python eval cells, in order, exactly as written. Do not edit, split, "
            "or add cells. Then stop.\n\n"
            f"Cell 1 (language py):\n{load}\n\nCell 2 (language py):\n{call}\n")


def session_argv(omp, session_dir, prompt, *, max_time, model, thinking):
    argv = [omp, "--session-dir", str(session_dir), "--mode", "json", "--auto-approve", "--no-skills",
            *run.SESSION_FLAGS, "--tools", "eval", "--max-time", str(max_time)]
    if model:
        argv += ["--model", model]
    if thinking:
        argv += ["--thinking", thinking]
    return [*argv, prompt]


def freeze_inputs(scratch, results, cases):
    """Copy each grader input from one read of its bytes; grading uses only these copies.

    Suffixes are kept because the grader chooses JSON or partial-JSONL parsing by suffix.
    """
    directory = scratch / "inputs"
    directory.mkdir()
    named = {"grader": (GRADE, "grade.py"), "results": (results, f"results{results.suffix}"),
             "cases": (cases, f"cases{cases.suffix}"),
             **{f"fixture:{name}": (HERE / name, name) for name in grader_module.FIXTURE_SOURCES}}
    frozen = {}
    for role, (live, name) in named.items():
        data = live.read_bytes()
        target = directory / name
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(0o444)
        frozen[role] = {"live": str(live), "snapshot": str(target),
                        "sha256": run.digest_bytes(data), "bytes": len(data)}
    return frozen


def input_drift(frozen):
    """Live inputs whose bytes differ from the frozen copies now; a missing file counts as drift."""
    drifted = []
    for entry in frozen.values():
        try:
            current = run.digest_bytes(Path(entry["live"]).read_bytes())
        except OSError:
            current = None
        if current != entry["sha256"]:
            drifted.append(entry["live"])
    return drifted


def verify_cells(calls, load, call):
    """Exactly two approved eval cells, in order: the load cell, then the grading cell."""
    expected = [load.strip(), call.strip()]
    codes = [item["code"].strip() for item in calls]
    load_seen = bool(codes) and codes[0] == expected[0]
    grade_seen = len(codes) >= 2 and codes[1] == expected[1]
    return {"eval_calls": len(calls), "sequence_exact": codes == expected,
            "unapproved_cell_indexes": [i for i, code in enumerate(codes) if i >= 2 or code != expected[i]],
            "load_seen": load_seen, "load_ok": load_seen and calls[0]["is_error"] is False,
            "grade_seen": grade_seen, "grade_ok": grade_seen and calls[1]["is_error"] is False,
            "load_sha256": run.digest_bytes(load.encode()), "call_sha256": run.digest_bytes(call.encode())}


def verify_outputs(out, frozen, descriptor):
    """Bind the graded output and its ledger to the frozen inputs and to this launch descriptor.

    The launch grades one results file, so grade() returns its single-result shape: the output's
    ``input_sha256`` is that file's digest, while the shared ledger maps each input path to its digest.
    """
    if not out.is_file():
        return {"ok": False, "problems": ["graded output was not written"]}
    try:
        graded = json.loads(out.read_text())
    except ValueError:
        return {"ok": False, "problems": ["graded output is not valid JSON"]}
    if not isinstance(graded, dict):
        return {"ok": False, "problems": ["graded output is not a JSON object"]}
    grading = graded.get("grading") if isinstance(graded.get("grading"), dict) else {}
    ledger_path = Path(str(grading.get("ledger_path") or ""))
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, ValueError):
        return {"ok": False, "problems": ["grading ledger was not written or is not valid JSON"]}
    if not isinstance(ledger, dict):
        return {"ok": False, "problems": ["grading ledger is not a JSON object"]}
    identities = graded.get("identities") if isinstance(graded.get("identities"), dict) else {}
    results, cases = frozen["results"], frozen["cases"]
    fixtures = {name: frozen[f"fixture:{name}"]["sha256"] for name in grader_module.FIXTURE_SOURCES}
    problems = []
    if ledger.get("id") != grading.get("ledger_id"):
        problems.append("ledger id does not match the graded output")
    if (ledger.get("cache") or {}).get("launch") != descriptor:
        problems.append("ledger does not record this launch descriptor")
    if graded.get("results") != results["snapshot"] or ledger.get("inputs") != [results["snapshot"]]:
        problems.append("graded output is not bound to the frozen results file")
    if (graded.get("input_sha256") != results["sha256"]
            or ledger.get("input_sha256") != {results["snapshot"]: results["sha256"]}):
        problems.append("graded output is not bound to the frozen results bytes")
    if graded.get("cases") != cases["snapshot"]:
        problems.append("graded output is not bound to the frozen cases file")
    if identities.get("source_sha256") != frozen["grader"]["sha256"]:
        problems.append("graded output was not produced by the frozen grader bytes")
    if identities.get("cases_sha256") != cases["sha256"]:
        problems.append("graded output's cases digest differs from the frozen cases")
    if identities.get("fixture_sources") != fixtures:
        problems.append("graded output's fixture sources differ from the frozen fixtures")
    if ledger.get("identities") != identities:
        problems.append("ledger identities differ from the graded output")
    if ledger.get("inputs_changed_during_grading"):
        problems.append("grading inputs changed during grading")
    return {"ok": not problems, "problems": problems, "graded_sha256": run.digest_bytes(out.read_bytes()),
            "ledger_path": str(ledger_path), "ledger_sha256": run.digest_bytes(ledger_path.read_bytes()),
            "ledger_id": ledger.get("id"),
            "inputs_changed_during_grading": ledger.get("inputs_changed_during_grading", [])}


def session_max_time(timeout, max_time=None):
    """The OMP session limit must outlive grade()'s batch deadline, or the session is killed first.

    Default: the batch deadline plus SESSION_MARGIN_SECONDS for loading, cleanup and ledger writes.
    An explicit limit at or below the deadline is rejected.
    """
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("timeout must be a positive integer number of seconds")
    if max_time is None:
        return timeout + SESSION_MARGIN_SECONDS
    if type(max_time) is not int or max_time <= timeout:
        raise ValueError(f"max_time ({max_time}s) must exceed the grading timeout ({timeout}s)")
    return max_time


def _plan_cache(cache_mode, cache_namespace, scratch):
    if cache_mode == "warm":
        seed = run.freeze_cache_seed(cache_namespace, scratch)
        return run.cache_plan("warm", seed, scratch, CELL_NAMESPACE, 1, 1)
    return run.cache_plan(cache_mode, None, scratch, CELL_NAMESPACE, 1, 1)


def _observe(state, line):
    """Fold one stream line into the launch state. Blank lines are skipped; unparsable ones are malformed."""
    if not line.strip():
        return
    try:
        event = json.loads(line)
    except ValueError:
        state["malformed"] = True
        return
    if not isinstance(event, dict):
        state["malformed"] = True
        return
    kind = event.get("type")
    if kind == "agent_start":
        state["agent_ended"] = False
    elif kind == "agent_end":
        state["agent_ended"] = True
    elif kind == "tool_execution_start":
        if event.get("toolName") != "eval":
            state["other_tools"].add(str(event.get("toolName")))
            return
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        code = args.get("code") if isinstance(args.get("code"), str) else ""
        state["calls"].append({"id": event.get("toolCallId"), "code": code, "is_error": None})
    elif kind == "tool_execution_end":
        for item in state["calls"]:
            if item["id"] == event.get("toolCallId"):
                item["is_error"] = event.get("isError") is True


def _session(argv, *, env, cwd, session_path, stderr_path, max_time):
    """Run the grading session under a watchdog; return the process, its stream state, and the deadline flag."""
    state = {"calls": [], "agent_ended": False, "malformed": False, "other_tools": set()}
    deadline = threading.Event()
    with stderr_path.open("x") as stderr_sink:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=stderr_sink, text=True, preexec_fn=run._die_with_parent)
    run.LIVE.add(proc)

    def expire():
        if proc.poll() is None:
            deadline.set()
            run.kill_tree(proc)

    watchdog = threading.Timer(max_time + 30, expire)
    watchdog.start()
    try:
        with session_path.open("x") as raw:
            for line in proc.stdout:
                raw.write(line)
                _observe(state, line)
        proc.wait()
    except BaseException:
        if proc.poll() is None:
            run.kill_tree(proc)
        proc.wait()
        raise
    finally:
        watchdog.cancel()
        if proc.stdout is not None:
            proc.stdout.close()
        run.LIVE.discard(proc)
    return proc, state, deadline.is_set()


def _problems(proc, state, deadline_hit, cells, outputs, drifted):
    """Every reason this launch is not graded. An empty list is the only success."""
    problems = []
    if deadline_hit:
        problems.append("session exceeded its deadline and was terminated")
    if proc.returncode != 0:
        problems.append(f"grading session exited with {proc.returncode}")
    if not state["agent_ended"]:
        problems.append("session did not reach agent_end")
    if state["malformed"]:
        problems.append("session event stream contained malformed lines")
    if state["other_tools"]:
        problems.append("unapproved tool calls: " + ", ".join(sorted(state["other_tools"])))
    if not cells["sequence_exact"]:
        problems.append(f"eval cells were not exactly the two approved cells in order "
                        f"(observed {cells['eval_calls']} eval calls)")
    if not cells["load_ok"]:
        problems.append("load cell was not observed without error")
    if not cells["grade_ok"]:
        problems.append("grading cell was not observed without error")
    if drifted:
        problems.append("live inputs changed during the launch; grading used the frozen copies: "
                        + ", ".join(drifted))
    problems.extend(outputs["problems"])
    return problems


def _grade(scratch, *, results, cases, out, paths, omp, cache_mode, cache_namespace, max_time, timeout,
           concurrency, model, thinking):
    frozen = freeze_inputs(scratch, results, cases)
    source = run.source_identity(omp, None)
    plan = _plan_cache(cache_mode, cache_namespace, scratch)
    descriptor = cache_descriptor(plan, cache_mode)
    load, call = build_cells(frozen["grader"]["snapshot"], frozen["results"]["snapshot"],
                             frozen["cases"]["snapshot"], out, timeout=timeout, concurrency=concurrency,
                             cache_mode=cache_mode, launch=descriptor)
    grading_cwd, session_dir = scratch / "cwd", scratch / "session"
    grading_cwd.mkdir()
    session_dir.mkdir()
    env = os.environ.copy()
    if plan["path"] is not None:
        env["OMP_JUDGMENT_CACHE_DB"] = str(plan["path"])
    argv = session_argv(omp, session_dir, prompt_text(load, call), max_time=max_time, model=model,
                        thinking=thinking)
    proc, state, deadline_hit = _session(argv, env=env, cwd=grading_cwd, session_path=paths["session"],
                                         stderr_path=paths["stderr"], max_time=max_time)
    complete = proc.returncode == 0 and state["agent_ended"] and not deadline_hit and not state["malformed"]
    entries, conflicts, usage_state, evidence = run.read_session_usage(session_dir, CELL_NAMESPACE,
                                                                       grading_cwd, complete=complete)
    accounting = run.finalize_accounting(entries, conflicts, [usage_state])
    accounting["session_evidence"] = {**evidence, "process_returncode": proc.returncode,
                                      "stream_malformed": state["malformed"],
                                      "process_settled": proc.poll() is not None}
    cells = verify_cells(state["calls"], load, call)
    outputs = verify_outputs(out, frozen, descriptor)
    problems = _problems(proc, state, deadline_hit, cells, outputs, input_drift(frozen))
    stderr = paths["stderr"]
    if stderr.stat().st_size == 0:
        stderr.unlink()
        stderr_record = None
    else:
        stderr_record = {"path": str(stderr), "bytes": stderr.stat().st_size,
                         "sha256": run.digest_bytes(stderr.read_bytes())}
    status = "not_graded" if problems else "graded"
    record = {"schema": 1, "kind": "grading_launch", "status": status, "host_attested": False,
              "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "cache": descriptor, "omp": source,
              "model_requested": model or "default-role", "thinking_requested": thinking or "configured",
              "inputs": {role: {"live": entry["live"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
                         for role, entry in frozen.items()},
              "session": {"returncode": proc.returncode, "max_time": max_time, "agent_ended": state["agent_ended"],
                          "deadline": deadline_hit, "stream_malformed": state["malformed"],
                          "eval_calls": len(state["calls"]), "other_tools": sorted(state["other_tools"]),
                          "complete": complete},
              "cells": cells, "accounting": accounting, "outputs": outputs, "stderr": stderr_record,
              "problems": problems,
              "notes": ["Accounting covers this grading session's persisted model_usage rows only.",
                        "judge_batch estimates stay in the grading ledger; no hosted billing proof is implied.",
                        "Cache evidence is the batch-reported counts, not the existence of a seed or clone.",
                        "Scratch files, including the frozen inputs and cache copies, were removed after the launch."]}
    with paths["record"].open("x") as handle:
        handle.write(json.dumps(record, indent=1) + "\n")
    print(f"grading launch {status}: {out} (launch record {paths['record']})")
    return record


def launch(*, results, cases, out, omp, cache_mode="cold", cache_namespace=None, workdir=None,
           max_time=None, timeout=1800, concurrency=4, model=None, thinking=None):
    """Grade frozen copies of the inputs in one OMP session and return the launch record.

    Scratch (frozen inputs, cache files, session and working directories) lives in a fresh directory
    under ``workdir`` (default: the system temporary directory) and is removed on every exit path.
    Outputs (graded JSON, ledger, session stream, stderr, launch record) are written beside ``out``.
    """
    max_time = session_max_time(timeout, max_time)
    results, cases, out = Path(results).resolve(), Path(cases).resolve(), Path(out).resolve()
    paths = {"record": Path(str(out) + ".launch.json"), "session": Path(str(out) + ".session.jsonl"),
             "stderr": Path(str(out) + ".stderr")}
    for path in (out, *paths.values()):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
    parent = None if workdir is None else Path(workdir).resolve()
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=parent))
    try:
        return _grade(scratch, results=results, cases=cases, out=out, paths=paths, omp=omp,
                      cache_mode=cache_mode, cache_namespace=cache_namespace, max_time=max_time,
                      timeout=timeout, concurrency=concurrency, model=model, thinking=thinking)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="captured execution JSON to grade")
    ap.add_argument("--cases", required=True, help="case file that produced the captured answers")
    ap.add_argument("--out", required=True, help="graded output path; a fresh path")
    ap.add_argument("--cache-mode", choices=run.CACHE_MODES, default="cold")
    ap.add_argument("--cache-namespace", help="seeded judgment DB for warm mode; frozen once by backup")
    ap.add_argument("--omp", default=shutil.which("omp"), help="omp executable (default: omp on PATH)")
    ap.add_argument("--workdir", help="parent for scratch files, removed on exit (default: system temporary directory)")
    ap.add_argument("--max-time", type=int, default=None,
                    help=f"OMP session limit in seconds; default timeout + {SESSION_MARGIN_SECONDS}")
    ap.add_argument("--timeout", type=int, default=1800, help="grading batch deadline in seconds")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--model")
    ap.add_argument("--thinking")
    args = ap.parse_args()
    if args.omp is None or shutil.which(args.omp) is None:
        ap.error(f"omp executable not found: {args.omp!r}")
    args.omp = os.path.abspath(shutil.which(args.omp))
    try:
        session_max_time(args.timeout, args.max_time)
    except ValueError as error:
        ap.error(str(error))
    if args.cache_mode == "warm" and not args.cache_namespace:
        ap.error("warm mode requires --cache-namespace")
    if args.cache_mode != "warm" and args.cache_namespace is not None:
        ap.error("--cache-namespace is only for warm mode")
    record = launch(results=args.results, cases=args.cases, out=args.out, omp=args.omp, cache_mode=args.cache_mode,
                    cache_namespace=args.cache_namespace, workdir=args.workdir, max_time=args.max_time,
                    timeout=args.timeout, concurrency=args.concurrency, model=args.model, thinking=args.thinking)
    if record["status"] != "graded":
        for problem in record["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
