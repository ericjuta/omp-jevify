#!/usr/bin/env python3
"""Trigger and outcome eval for the jevify skill.

Runs each case in evals/cases.json as a fresh non-interactive `omp` session inside a
freshly built fixture repository, and records whether the model opened the skill,
loaded the jv helpers, and ran a judge batch, plus the session's final answer and token
usage for outcome grading (evals/grade.py). Sessions run to completion, bounded by
--max-time and --max-calls.

A case may set "scale": N to build a larger fixture (default 1; evals/cases-large.json runs
at scale 12). "expect": "either" cases are controls whose trigger is not scored: they are
reported as their own `either` count, and their answer is still graded.

Arms, each set through a one-shot `--config` overlay for that process only:
  skill  the skill under test is pinned via skills.customDirectories, overriding any
         installed jevify plugin.
  none   jevify is removed via skills.ignoredSkills; every other skill stays available.
         The jevify magic-keyword notice still fires.

  python3 evals/run.py --ref v0.1.0 --out evals/results/baseline-v0.1.0.json
  python3 evals/run.py --out /tmp/candidate.json --compare evals/results/baseline-v0.1.0.json
  python3 evals/run.py --arm none --omp /path/to/omp --out /tmp/noskill.json

Needs a working `omp` with model access. Results vary run to run; use --reps >= 3.
Usage sums the main session's assistant messages only; model calls made inside the eval
kernel (judge/judge_batch) are not included.
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import fixture  # noqa: E402

TOOLS = "read,eval,grep,glob,bash,find,edit,write"
LIVE = set()  # running omp sessions, reaped if the runner is interrupted
JUDGE_MARKERS = ("judge_batch", "jv.run(", "judge(")
AGENT_END_GRACE = 15  # seconds an omp process may linger after agent_end before it is reaped


def overlay_text(arm, skills_dir):
    if arm == "skill":
        return f"skills:\n  customDirectories:\n    - {json.dumps(str(skills_dir))}\n"
    return 'skills:\n  ignoredSkills:\n    - "jevify"\n'


def message_text(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def add_usage(total, usage):
    if not isinstance(usage, dict):
        return
    cost = usage.get("cost")
    for key, value in (("input", usage.get("input")), ("output", usage.get("output")),
                       ("cache_read", usage.get("cacheRead")), ("cache_write", usage.get("cacheWrite")),
                       ("cost", cost.get("total") if isinstance(cost, dict) else cost)):
        if isinstance(value, (int, float)):
            total[key] = (total[key] or 0) + value


def skill_from_ref(ref, dest):
    target = dest / "jevify"
    target.mkdir(parents=True)
    for name in ("SKILL.md", "jevify.py"):
        blob = subprocess.run(["git", "show", f"{ref}:skills/jevify/{name}"], cwd=ROOT,
                              check=True, capture_output=True).stdout
        (target / name).write_bytes(blob)
    return dest


def description(skill_md):
    for line in skill_md.read_text().splitlines()[1:]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip('"')
        if line.strip() == "---":
            break
    return ""


def _die_with_parent():
    """Linux: kill the omp session if the runner itself dies (even by SIGKILL)."""
    os.setsid()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except Exception:
        pass


def run_one(case, rep, overlay, desc_probe, args, workdir):
    """Run a case, retrying sessions that never produced a tool call or an answer.

    Those are provider or startup failures, not trigger decisions; a run that still fails
    after the retries is marked infra and excluded from the rates.
    """
    for attempt in range(args.retries + 1):
        result = attempt_one(case, rep, attempt, overlay, desc_probe, args, workdir)
        if not result["infra"]:
            break
    result["attempts"] = attempt + 1
    return result


def attempt_one(case, rep, attempt, overlay, desc_probe, args, workdir):
    repo = fixture.build(workdir / f"{case['id']}-{rep}-{attempt}", scale=case.get("scale", 1))
    argv = [args.omp, "--no-session", "--mode", "json", "--auto-approve", "--config", str(overlay),
            "--tools", TOOLS, "--max-time", str(args.max_time)]
    if args.model:
        argv += ["--model", args.model]
    if args.thinking:
        argv += ["--thinking", args.thinking]
    argv.append(case["prompt"])
    started = time.time()
    proc = subprocess.Popen(argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, preexec_fn=_die_with_parent)
    LIVE.add(proc)
    calls, errors, model = [], 0, None
    thinking = None  # first thinking_level_changed "resolved" level (what auto picked)
    skill_at = helpers = judged = None
    skill_source_ok = None
    stop = None
    answered = False
    answer = ""
    usage = {"input": None, "output": None, "cache_read": None, "cache_write": None, "cost": None}
    grace = None  # reaps a process that lingers after agent_end
    watchdog = threading.Timer(args.max_time + 30, kill_tree, (proc,))
    watchdog.start()
    raw = open(Path(args.raw_dir) / f"{case['id']}-{rep}-{attempt}.jsonl", "w") if args.raw_dir else None
    try:
        for line in proc.stdout:
            if raw:
                raw.write(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            msg = ev.get("message")
            if model is None and isinstance(msg, dict) and isinstance(msg.get("model"), str):
                model = msg["model"]
            if kind == "thinking_level_changed" and thinking is None:
                thinking = ev.get("resolved")
            if kind == "message_end" and isinstance(msg, dict) and msg.get("role") == "assistant":
                add_usage(usage, msg.get("usage"))
                text = message_text(msg).strip()
                if text:
                    answer = text
                if msg.get("stopReason") == "stop":
                    answered = True
            if kind == "agent_end" and grace is None:
                grace = threading.Timer(AGENT_END_GRACE, kill_tree, (proc,))
                grace.start()
            elif kind == "agent_start" and grace is not None:
                grace.cancel()
                grace = None
            if kind == "tool_execution_start":
                tool, targs = ev.get("toolName"), ev.get("args") or {}
                calls.append({"id": ev.get("toolCallId"), "tool": tool, "args": targs})
                idx = len(calls) - 1
                text = json.dumps(targs)
                if skill_at is None and tool == "read" and str(targs.get("path", "")).startswith("skill://jevify"):
                    skill_at = idx
                if tool == "eval":
                    code = str(targs.get("code", ""))
                    if helpers is None and "skill://jevify/jevify.py" in code:
                        helpers = idx
                    if judged is None and any(m in code for m in JUDGE_MARKERS):
                        judged = idx
                calls[-1]["data"] = "skill://" not in text
            elif kind == "tool_execution_end":
                call = next((c for c in calls if c["id"] == ev.get("toolCallId")), None)
                if call and ev.get("isError") and call["tool"] == "eval":
                    errors += 1
                if desc_probe and call and calls.index(call) == skill_at and skill_source_ok is None:
                    skill_source_ok = desc_probe in json.dumps(ev.get("result"))
            if len(calls) >= args.max_calls:
                stop = "max_calls"
                break
    finally:
        timed_out = not watchdog.is_alive() and stop is None
        watchdog.cancel()
        if grace is not None:
            grace.cancel()
        kill_tree(proc)
        LIVE.discard(proc)
        if raw:
            raw.close()
    if timed_out:
        stop = "deadline"
    first_data = next((i for i, c in enumerate(calls) if c.get("data")), None)
    triggered = skill_at is not None
    return {
        "case": case["id"], "rep": rep, "expect": case["expect"], "keyword": bool(case.get("keyword")),
        "infra": not calls and not answered,
        # Trigger cases pass when the skill is opened; no-trigger cases when it is not; either always.
        "pass": True if case["expect"] == "either" else triggered if case["expect"] == "trigger" else not triggered,
        "skill_read": triggered,
        "skill_before_data": triggered and (first_data is None or skill_at < first_data),
        "helpers_loaded": helpers is not None, "judged": judged is not None,
        "skill_source_ok": skill_source_ok, "eval_errors": errors, "calls": len(calls),
        "stop": stop or "exit", "secs": round(time.time() - started, 1), "model": model,
        "first_tools": [c["tool"] for c in calls[:5]],
        "answer": answer, "usage": usage, "thinking": thinking,
    }


def summarize(runs):
    valid = [r for r in runs if not r["infra"]]
    per_case = {}
    for r in valid:
        c = per_case.setdefault(r["case"], {"expect": r["expect"], "keyword": r["keyword"], "n": 0, "pass": 0,
                                            "skill_before_data": 0, "judged": 0})
        c["n"] += 1
        c["pass"] += r["pass"]
        c["skill_before_data"] += r["skill_before_data"]
        c["judged"] += r["judged"]
    costs = [r["usage"]["cost"] for r in valid if (r.get("usage") or {}).get("cost") is not None]
    auto = [r for r in valid if r["expect"] == "trigger" and not r["keyword"]]
    neg = [r for r in valid if r["expect"] == "no_trigger"]
    kw = [r for r in valid if r["keyword"]]
    either = [r for r in valid if r["expect"] == "either"]
    return {
        "auto_trigger": f"{sum(r['skill_read'] for r in auto)}/{len(auto)}",
        "auto_judged": f"{sum(r['judged'] for r in auto)}/{len(auto)}",
        "false_trigger": f"{sum(r['skill_read'] for r in neg)}/{len(neg)}",
        "keyword": f"{sum(r['skill_read'] for r in kw)}/{len(kw)}",
        "either": f"{sum(r['skill_read'] for r in either)}/{len(either)}",
        "infra_failures": len(runs) - len(valid),
        "eval_errors": sum(r["eval_errors"] for r in runs),
        "skill_source_mismatch": sum(1 for r in runs if r["skill_source_ok"] is False),
        "mean_secs": round(sum(r["secs"] for r in valid) / len(valid), 1) if valid else None,
        "mean_cost": round(sum(costs) / len(costs), 4) if costs else None,
        "per_case": per_case,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--ref", help="git ref whose skills/jevify is tested (default: working tree)")
    src.add_argument("--skill-dir", help="directory containing jevify/SKILL.md")
    ap.add_argument("--cases", default=str(HERE / "cases.json"))
    ap.add_argument("--only", nargs="*", help="case ids to run")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--model", help="model selector (default: your default role)")
    ap.add_argument("--thinking", help="thinking level passed to omp (default: the configured level, usually auto)")
    ap.add_argument("--arm", choices=("skill", "none"), default="skill",
                    help="skill: pin the skill under test; none: make jevify unavailable")
    ap.add_argument("--omp", default=shutil.which("omp"), help="omp binary to run (default: omp on PATH)")
    ap.add_argument("--max-time", type=int, default=420)
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--retries", type=int, default=1, help="retries for sessions with no tool call or answer")
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", help="also save each session's raw JSON event stream here")
    ap.add_argument("--compare", help="earlier result file to diff against")
    args = ap.parse_args()
    if not args.omp or not shutil.which(args.omp):
        ap.error(f"omp binary not found: {args.omp!r}")
    args.omp = os.path.abspath(shutil.which(args.omp))
    omp_version = subprocess.run([args.omp, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True).stdout.strip()
    if args.raw_dir:
        Path(args.raw_dir).mkdir(parents=True, exist_ok=True)

    cases = json.loads(Path(args.cases).read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    for c in cases:
        if c.get("expect") not in ("trigger", "no_trigger", "either"):
            ap.error(f"case {c.get('id')!r}: expect must be trigger, no_trigger or either, not {c.get('expect')!r}")
    with tempfile.TemporaryDirectory(prefix="jevify-eval-") as tmp:
        tmp = Path(tmp)
        if args.ref:
            skills_dir = skill_from_ref(args.ref, tmp / "skills")
        else:
            skills_dir = Path(args.skill_dir or ROOT / "skills").resolve()
        skill_md = skills_dir / "jevify" / "SKILL.md"
        overlay = tmp / "overlay.yml"
        overlay.write_text(overlay_text(args.arm, skills_dir))
        # In the none arm a skill:// read fails by design, so there is no source to verify.
        desc_probe = description(skill_md)[:60] if args.arm == "skill" else None
        work = tmp / "work"
        work.mkdir()
        jobs = [(c, k) for c in cases for k in range(1, args.reps + 1)]
        runs = []
        # Each finished run is appended here at once, so a killed runner keeps its evidence.
        partial = Path(args.out + ".partial.jsonl")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text("")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        ex = cf.ThreadPoolExecutor(max_workers=args.jobs)
        try:
            futs = [ex.submit(run_one, c, k, overlay, desc_probe, args, work) for c, k in jobs]
            for f in cf.as_completed(futs):
                r = f.result()
                runs.append(r)
                with partial.open("a") as sink:
                    sink.write(json.dumps(r) + "\n")
                print(f"{r['case']:22} r{r['rep']} pass={r['pass']!s:5} infra={r['infra']!s:5} skill={r['skill_read']!s:5} "
                      f"helpers={r['helpers_loaded']!s:5} judged={r['judged']!s:5} {r['stop']:9} {r['secs']}s "
                      f"calls={r['calls']} cost={r['usage']['cost']}", flush=True)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
            for proc in list(LIVE):
                kill_tree(proc)
        runs.sort(key=lambda r: (r["case"], r["rep"]))
        result = {
            "schema": 2, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "arm": args.arm,
            "ref": args.ref or ("skill-dir" if args.skill_dir else "worktree"),
            "skill_sha256": hashlib.sha256(skill_md.read_bytes()).hexdigest()[:12] if args.arm == "skill" else None,
            "cases_sha256": hashlib.sha256(Path(args.cases).read_bytes()).hexdigest()[:12],
            "omp": omp_version, "omp_path": args.omp, "model": args.model or "default-role",
            "thinking": args.thinking or "configured",
            "max_time": args.max_time, "max_calls": args.max_calls,
            "models_seen": sorted({r["model"] for r in runs if r["model"]}),
            "reps": args.reps, "summary": summarize(runs), "runs": runs,
        }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    s = result["summary"]
    print(f"\nauto_trigger {s['auto_trigger']} · auto_judged {s['auto_judged']} · false_trigger {s['false_trigger']} · "
          f"keyword {s['keyword']} · either {s['either']} · infra {s['infra_failures']} · eval_errors {s['eval_errors']} · "
          f"skill_source_mismatch {s['skill_source_mismatch']} · mean_secs {s['mean_secs']} · mean_cost {s['mean_cost']}")
    print(f"arm {result['arm']} · {result['omp']} ({result['omp_path']})")
    if args.compare:
        base = json.loads(Path(args.compare).read_text())
        if base.get("cases_sha256") != result["cases_sha256"]:
            print("warning: case file differs from the baseline; comparison is not one-variable")
        base_setup = {"omp": base.get("omp"), "arm": base.get("arm", "skill")}  # schema 1 had no arm
        diffs = [f"{k} {v} -> {result[k]}" for k, v in base_setup.items() if v != result[k]]
        if diffs:
            print("note: baseline differs in " + "; ".join(diffs))
        bs = base["summary"]
        print(f"baseline auto_trigger {bs['auto_trigger']} · auto_judged {bs.get('auto_judged', '?')} · "
              f"false_trigger {bs['false_trigger']} · keyword {bs['keyword']} · either {bs.get('either', '?')}")
        if base.get("schema", 1) < 2:
            print("note: baseline is schema 1 (sessions stopped early at the first judge call)")
        print(f"  {'case':22} pass    judged   (baseline pass / judged)")
        for cid, c in sorted(s["per_case"].items()):
            b = bs["per_case"].get(cid)
            line = f"  {cid:22} {c['pass']}/{c['n']}     {c['judged']}/{c['n']}"
            if b:
                line += f"      ({b['pass']}/{b['n']} / {b.get('judged', '?')}/{b['n']})"
            print(line)


if __name__ == "__main__":
    main()
