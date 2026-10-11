#!/usr/bin/env python3
"""Availability and strategy comparison for the jevify skill.

Runs every case as a bounded non-interactive `omp` session in a fresh fixture. `--arm`
controls whether the product skill is available. `--strategy` independently controls
how the subject is instructed to work. A goal is never rewritten into an expected
answer or source string. Sessions run to completion, bounded by --max-time and
--max-calls, with extensions, rules, LSP and titling disabled identically in every arm.

Arms, each set through a one-shot `--config` overlay for that process only:
  skill  the skill under test is pinned via skills.customDirectories as bare jevify;
         every namespaced jevify copy, such as a displaced installed plugin, is ignored.
  none   bare and every namespaced jevify skill are ignored via skills.ignoredSkills.

Strategies:
  auto           the case goal is unchanged.
  direct-script  compute or inspect directly; no task-driven judge/completion.
  jev-flat       one flat Jevify judgment per planned unit with the prescribed settings.
  jev-grouped    grouped Jevify judgments with the prescribed settings.

Cold cache runs get a distinct, initially empty judgment database for every attempt.
Warm mode freezes an explicit seeded database with a consistent SQLite backup (including
WAL) once per evaluation, then clones it independently for every attempt. A seed alone
never proves cache hits. Uncontrolled mode leaves the host cache untouched.

  python3 evals/run.py --ref v0.1.0 --out evals/results/baseline-v0.1.0.json
  python3 evals/run.py --strategy jev-flat --cache-mode cold --out /tmp/flat.json
  python3 evals/run.py --arm none --strategy auto --out /tmp/noskill.json

Authorized envelope (optional; each limit is checked before an attempt starts):
  python3 evals/run.py --cases evals/cases-smoke.json --preflight --authorize-attempts 3 --out /tmp/x.json
  --authorize-attempts N      attempt sessions, including retries, that may start
  --authorize-questions N     stop scheduling once observed known judgment questions reach N
  --authorize-concurrency N   simultaneous attempts (the smaller of N and --jobs)
  --thresholds FILE           predeclared decision rule; its canonical digest binds the run
No dollar envelope exists: dollar totals stay unknown or catalog-estimated, with no hard guarantee.

Needs a working `omp` with model access. Results vary run to run; use --reps >= 3.
Outer assistant usage and persisted session model_usage entries are kept separately.
An absent or incomplete session ledger, or an unknown-cost attempt, leaves the monetary total
unknown; it is never stored as zero. Outer chat costs are catalog estimates. Grading
cost is not measured or copied into an execution result.
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import fixture  # noqa: E402

TOOLS = "read,eval,grep,glob,bash,find,edit,write"
SESSION_FLAGS = ("--no-extensions", "--no-rules", "--no-lsp", "--no-title")
# Host wire purposes map to stable report buckets; runtime-only purposes remain other.
TASK_PURPOSES = {"judge": "judgment", "judge_batch": "judgment", "eval_completion": "completion"}
LIVE = set()  # running omp sessions, reaped if the runner is interrupted
SOURCE_MARKERS = ("judge_batch", "jv.run(", "judge(", "completion(")
STRATEGIES = ("auto", "direct-script", "jev-flat", "jev-grouped")
JEV_STRATEGIES = ("jev-flat", "jev-grouped")
CACHE_MODES = ("cold", "warm", "uncontrolled")
SNAPSHOT_STATES = ("complete", "incomplete", "absent")
HELPER_CONCURRENCY = 4
BACKEND_RETRIES = 0
SHARED_DEADLINE_SECONDS = 60
GROUPED_K = 4
PUBLIC_RUBRIC_REL = Path("review/rubric.json")
PUBLIC_CLAIMS_REL = Path("review/claims.json")
REQUIRED_SKILL_FILES = ("SKILL.md", "jevify.py")
TOKEN_FIELDS = {
    "input": ("input", "inputTokens", "prompt_tokens"),
    "output": ("output", "outputTokens", "completion_tokens"),
    "cache_read": ("cache_read", "cacheRead", "cached_tokens"),
    "cache_write": ("cache_write", "cacheWrite"),
}
RESULT_SCHEMA = 5
CONTROL_KINDS = ("answer_integer", "repo_rename")
CONFIDENCE = 0.95
# Two-sided 95% Student t critical values by degrees of freedom; beyond 30 the normal value 1.96 is used.
T_CRITICAL_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
                 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120,
                 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
                 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}
RUNNER_MODULES = ("run.py", "fixture.py", "refund_retry_fixture.py")
METHOD_SETTINGS = {"helper_concurrency": HELPER_CONCURRENCY, "backend_retries": BACKEND_RETRIES,
                   "shared_deadline_seconds": SHARED_DEADLINE_SECONDS, "grouped_k": GROUPED_K,
                   "evidence_class": "prescribed_method_settings_not_execution_proof"}
# Fields a one-variable comparison must hold fixed; treatment fields are the only ones allowed to differ.
CONTRACT_CONTROLLED = ("runner", "source", "executable", "host", "model_requested", "thinking_requested",
                       "cases", "fixtures", "cache", "method_settings", "limits", "session_flags", "tools",
                       "envelope", "predeclared_thresholds")
# skill folds the arm and its bundle into one field, so skill vs none is a single treatment difference.
CONTRACT_TREATMENT = ("skill", "strategy")
THRESHOLD_ROLES = ("correctness", "coverage", "unsafe")


# OMP filters raw skill names before namespacing, then final names afterward. These patterns
# need a slash, so the pinned bare jevify stays available in the skill arm.
NAMESPACED_JEVIFY = ("*/**/jevify", "*/**/jevify~*")


def overlay_text(arm, skills_dir):
    ignored = NAMESPACED_JEVIFY if arm == "skill" else ("jevify", *NAMESPACED_JEVIFY)
    text = "skills:\n"
    if arm == "skill":
        text += f"  customDirectories:\n    - {json.dumps(str(skills_dir))}\n"
    return text + "  ignoredSkills:\n" + "".join(f"    - {json.dumps(pattern)}\n" for pattern in ignored)


def message_text(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def load_json_object(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def structured_values(value):
    """Objects in an actual tool result, including JSON printed one object per output line."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from structured_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from structured_values(child)
    elif isinstance(value, str):
        whole = load_json_object(value.strip())
        parsed = [whole] if whole is not None else [load_json_object(line.strip()) for line in value.splitlines()
                                                     if line.strip().startswith("{")]
        for item in parsed:
            if item is not None:
                yield from structured_values(item)


def first_number(record, names):
    if not isinstance(record, dict):
        return None
    for name in names:
        value = record.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


def usage_tokens(usage):
    return {name: first_number(usage, names) for name, names in TOKEN_FIELDS.items()}


def usage_reported_cost(usage):
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if isinstance(cost, dict):
        value = cost.get("total")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return cost
    return None


def add_usage(total, usage):
    """Compatibility accumulator for the historical outer-only usage object."""
    for key, value in usage_tokens(usage).items():
        if isinstance(value, (int, float)):
            total[key] = (total[key] or 0) + value
    cost = usage_reported_cost(usage)
    if isinstance(cost, (int, float)):
        total["cost"] = (total["cost"] or 0) + cost


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_json(value):
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def rubric_ordered_identity(questions):
    """jv._ordered_identity: question order and criterion order are identity; fields within a question are not."""
    identity = []
    for qid, question in questions.items():
        record = []
        for field in sorted(question):
            value = question[field]
            if field == "criteria" and isinstance(value, dict):
                value = [[label, description] for label, description in value.items()]
            record.append([field, value])
        identity.append([qid, record])
    return identity


def helper_qhash(questions):
    """The ordered question hash jv.run writes into its receipt (jv.qhash, ordered=True)."""
    text = json.dumps(rubric_ordered_identity(questions), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def string_ids(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def git_text(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def git_paths(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True)
    if result.returncode != 0:
        return None
    return [part.decode("utf-8", "surrogateescape") for part in result.stdout.split(b"\0") if part]


def file_identity(path):
    path = Path(path)
    if not path.is_file():
        return {"path": str(path), "present": False, "sha256": None, "bytes": None}
    data = path.read_bytes()
    return {"path": str(path.resolve()), "present": True, "sha256": digest_bytes(data), "bytes": len(data)}


def skill_from_ref(ref, dest):
    """Materialize every tracked file under skills/jevify at ref, reference material included."""
    names = git_paths(ROOT, "ls-tree", "-r", "-z", "--name-only", ref, "--", "skills/jevify/")
    if not names:
        raise RuntimeError(f"git ref {ref!r} has no skills/jevify directory")
    for name in names:
        result = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=ROOT, stdin=subprocess.DEVNULL,
                                capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"cannot read {name} at {ref!r}")
        target = dest / "jevify" / Path(name).relative_to("skills/jevify")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result.stdout)
    return dest


def description(skill_md):
    for line in skill_md.read_text().splitlines()[1:]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip('"')
        if line.strip() == "---":
            break
    return ""


def skill_identity(skills_dir, arm):
    root = Path(skills_dir) / "jevify"
    paths = sorted(path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts) \
        if root.is_dir() else []
    files = {str(path.relative_to(root)): file_identity(path) for path in paths}
    return {
        "available": arm == "skill",
        "directory": str(root.resolve()) if root.is_dir() else None,
        "files": files,
        "materialized_sha256": digest_json({name: record["sha256"] for name, record in files.items()})
        if arm == "skill" and files else None,
        "complete": arm != "skill" or all(name in files for name in REQUIRED_SKILL_FILES),
        "notes": ["File hashes identify the tested bundle; a mixed bundle is not a historical release."],
    }


def runner_identity():
    """Identity of the code that builds fixtures and launches attempts; not subject behavior."""
    modules = {name: file_identity(HERE / name)["sha256"] for name in RUNNER_MODULES}
    return {"modules": modules, "sha256": digest_json(modules), "python": sys.version.split()[0]}


def retain_content(directory, name, data):
    """Write launch bytes once; a later mismatch means the retained snapshot was altered."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.exists():
        if target.read_bytes() != data:
            raise RuntimeError(f"retained snapshot {target} differs from the launch bytes")
        return target
    target.write_bytes(data)
    return target


def retain_skill_snapshot(skills_dir, directory):
    """Copy the tested bundle's bytes once; every attempt loads this tree, never the live source."""
    root = Path(skills_dir) / "jevify"
    contents = {path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}
    if not contents:
        raise RuntimeError(f"no skill files to snapshot under {root}")
    digest = digest_json({name: digest_bytes(data) for name, data in contents.items()})
    target = Path(directory) / f"skill-snapshot-{digest[:12]}"
    if not (target / "jevify").is_dir():
        staging = Path(tempfile.mkdtemp(prefix="skill-stage-", dir=Path(directory)))
        for name, data in contents.items():
            destination = staging / "jevify" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        try:
            staging.rename(target)
        except OSError:
            shutil.rmtree(staging)
            if not (target / "jevify").is_dir():
                raise
    if skill_identity(target, "skill")["materialized_sha256"] != digest:
        raise RuntimeError("retained skill snapshot does not match its digest")
    return target, digest


def source_changes(root):
    """Working-tree state against HEAD: tracked edits and deletions plus untracked, non-ignored files."""
    tracked = git_paths(root, "diff", "--name-status", "--no-renames", "-z", "HEAD")
    untracked = git_paths(root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or untracked is None or len(tracked) % 2:
        return None
    changes = {}
    for status, name in zip(tracked[0::2], tracked[1::2]):
        kind = "deleted" if status.startswith("D") else "added" if status.startswith("A") else "modified"
        changes[name] = {"status": kind, "sha256": None if kind == "deleted" else file_identity(root / name)["sha256"]}
    for name in untracked:
        changes[name] = {"status": "untracked", "sha256": file_identity(root / name)["sha256"]}
    return dict(sorted(changes.items()))


def source_identity(executable, requested_source):
    executable = Path(executable).resolve()
    record = file_identity(executable)
    record["version"] = subprocess.run([str(executable), "--version"], stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True).stdout.strip() or None
    if requested_source is None:
        return {"kind": "executable_only", "status": "source_unspecified", "executable": record,
                "source_root": None, "revision": None, "changes": None, "source_manifest_sha256": None,
                "notes": ["The executable version label is not evidence of the source tree it runs."]}
    root = Path(requested_source).resolve()
    if not root.is_dir():
        raise RuntimeError(f"--omp-source does not exist: {root}")
    revision = git_text(root, "rev-parse", "HEAD")
    changes = source_changes(root)
    complete = revision is not None and changes is not None
    return {
        "kind": "explicit_source_tree",
        "status": "complete" if complete else "incomplete",
        "executable": record,
        "source_root": str(root),
        "revision": revision,
        "changes": changes,
        "source_manifest_sha256": digest_json({"revision": revision, "changes": changes}) if complete else None,
        "notes": ["Executable identity and source-tree identity are recorded independently."],
    }


def fixture_identity(case):
    dataset = case.get("dataset", "billing")
    with tempfile.TemporaryDirectory(prefix="jevify-fixture-id-") as tmp:
        repo = fixture.build(Path(tmp) / "repo", scale=case.get("scale", 1), dataset=dataset)
        files = {}
        for path in sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts):
            files[str(path.relative_to(repo))] = digest_bytes(path.read_bytes())
        keys = fixture.answer_keys(repo, dataset=dataset)
    return {
        "dataset": dataset,
        "scale": case.get("scale", 1),
        "key": case.get("key"),
        "fixture_sha256": digest_json(files),
        "private_keys_sha256": digest_json(keys),
        "public_rubric_sha256": files.get(str(PUBLIC_RUBRIC_REL)),
        "file_count": len(files),
    }


def public_plan(repo):
    """Planned unit IDs and the helper hash of the public rubric, read before the session starts."""
    plan = {"planned_ids": None, "rubric_hash": None}
    rubric = load_json_object((repo / PUBLIC_RUBRIC_REL).read_text()) if (repo / PUBLIC_RUBRIC_REL).is_file() else None
    if rubric:
        plan["rubric_hash"] = helper_qhash(rubric)
    claims = load_json_object((repo / PUBLIC_CLAIMS_REL).read_text()) if (repo / PUBLIC_CLAIMS_REL).is_file() else None
    entries = claims.get("claims") if claims else None
    if isinstance(entries, list):
        ids = string_ids([entry.get("id") if isinstance(entry, dict) else None for entry in entries])
        if ids and all(ids) and len(set(ids)) == len(ids):
            plan["planned_ids"] = ids
    return plan


def public_rubric_obligation(repo):
    rubric = load_json_object((repo / PUBLIC_RUBRIC_REL).read_text()) if (repo / PUBLIC_RUBRIC_REL).is_file() else None
    if not rubric:
        return ""
    names = ", ".join(sorted(str(name) for name in rubric))
    return (f" The public rubric is already in {PUBLIC_RUBRIC_REL.as_posix()} with questions {names}. "
            "Use that rubric unchanged; do not replace, narrow, or reveal its criteria.")


def strategy_prompt(case, repo, strategy):
    goal = case["prompt"].rstrip()
    if strategy == "auto":
        return goal
    if strategy == "direct-script":
        method = (" Use direct inspection, deterministic tools, or an ordinary script. "
                  "Do not call judge, judge_batch, completion, or the Jevify helper.")
    elif strategy == "jev-flat":
        method = (f" Use the Jevify helper for one flat judgment batch: concurrency={HELPER_CONCURRENCY}, "
                  f"backend retries={BACKEND_RETRIES}, and a shared deadline of {SHARED_DEADLINE_SECONDS} seconds. "
                  "Judge each planned unit independently. Keep the returned rows and their rows.receipt. "
                  "Check rows.receipt['complete'] as well as jv.flag(rows) before closing or acting; "
                  "manually resolve flagged rows or incomplete coverage.")
    elif strategy == "jev-grouped":
        method = (f" Use the Jevify helper with grouped judgments: group size k={GROUPED_K}, "
                  f"concurrency={HELPER_CONCURRENCY}, backend retries={BACKEND_RETRIES}, and a shared deadline "
                  f"of {SHARED_DEADLINE_SECONDS} seconds. Cover every planned unit. "
                  "Use grouped_states, grouping = jv.group(...), then pass grouping=grouping to jv.run; "
                  "do not pass only the slots or discard exclusions. Keep the returned rows and rows.receipt. "
                  "Check rows.receipt['complete'] as well as jv.flag(rows) before closing or acting. "
                  "If calibrating, pass the same grouped_states to jv.calibrate(rows, grouped_states, Q, ...); "
                  "the helper retains group/slot/shared context. Manually resolve flags and exclusions.")
    else:
        raise ValueError(f"unknown strategy {strategy!r}")
    return goal + method + public_rubric_obligation(repo)


def freeze_cache_seed(requested_namespace, workdir):
    """Freeze once before scheduling jobs; pass this descriptor to warm cache_plan.

    The SQLite backup includes committed WAL contents, unlike copying the seed file.
    Only the backup is fingerprinted: source identity is not a claim that the live
    source bytes stay fixed. Invalid input raises ValueError, SQLite failures raise
    RuntimeError, and filesystem failures propagate as OSError.
    """
    if not isinstance(requested_namespace, (str, os.PathLike)) or not str(requested_namespace):
        raise ValueError("warm cache mode requires --cache-namespace pointing at a seeded database")
    source = Path(requested_namespace).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError(f"warm cache namespace is missing or empty: {source}")
    identity = source.stat()
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="cache-seed-", dir=workdir))
    path = directory / "frozen.sqlite"
    try:
        reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            writer = sqlite3.connect(path)
            try:
                reader.backup(writer)
            finally:
                writer.close()
        finally:
            reader.close()
        contents = path.read_bytes()
        path.chmod(0o444)
        return {"kind": "frozen_sqlite_seed", "path": path,
                "initial_sha256": digest_bytes(contents), "initial_bytes": len(contents),
                "source": {"path": str(source), "device": identity.st_dev, "inode": identity.st_ino},
                "snapshot": {"method": "sqlite_backup", "source_access": "read_only", "wal_included": True},
                "isolation": "frozen_seed"}
    except (sqlite3.Error, OSError) as exc:
        shutil.rmtree(directory)
        if isinstance(exc, sqlite3.Error):
            raise RuntimeError(f"could not freeze warm cache seed {source}: {exc}") from exc
        raise


def _cache_attempt_path(workdir, mode):
    """Reserve a distinct path even for repeated case/rep/attempt identifiers."""
    directory = Path(workdir).resolve() / f"{mode}-cache"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="attempt-", suffix=".sqlite", dir=directory, delete=False) as db:
        return Path(db.name)


def cache_plan(mode, requested_namespace, workdir, case_id, rep, attempt):
    """Plan independent writable attempts; warm accepts only a frozen descriptor."""
    if mode == "uncontrolled":
        if requested_namespace is not None:
            raise ValueError("an explicit cache namespace requires cold or warm mode")
        return {"mode": mode, "status": "uncontrolled", "path": None, "seeded": None,
                "notes": ["Host cache location and initial contents were intentionally not controlled."]}
    if mode == "warm":
        seed = requested_namespace
        if (not isinstance(seed, dict) or seed.get("kind") != "frozen_sqlite_seed"
                or not isinstance(seed.get("path"), (str, os.PathLike))
                or not isinstance(seed.get("initial_sha256"), str)
                or not isinstance(seed.get("initial_bytes"), int)
                or not isinstance(seed.get("source"), dict) or not isinstance(seed.get("snapshot"), dict)):
            raise ValueError("warm cache mode requires a descriptor from freeze_cache_seed")
        frozen = Path(seed["path"]).resolve()
        if not frozen.is_relative_to(Path(workdir).resolve()) or not frozen.is_file():
            raise ValueError("frozen warm cache seed must exist under workdir")
        path = _cache_attempt_path(workdir, mode)
        try:
            # Copy data, not the frozen file's read-only permissions.
            shutil.copyfile(frozen, path)
            contents = path.read_bytes()
            fingerprint = digest_bytes(contents)
            if fingerprint != seed["initial_sha256"] or len(contents) != seed["initial_bytes"]:
                raise ValueError("frozen warm cache seed no longer matches its starting fingerprint")
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            raise
        return {"mode": mode, "status": "frozen_seed_clone", "path": path, "seeded": True,
                "initial_sha256": fingerprint, "initial_bytes": len(contents),
                "seed_path": str(frozen), "source": seed["source"], "snapshot": seed["snapshot"],
                "isolation": "independent_per_attempt",
                "notes": ["Each attempt starts from the same frozen SQLite backup in an independent writable database.",
                          "Warm evidence is the observed cache counts, not the existence of a seed."]}
    if mode != "cold":
        raise ValueError(f"unknown cache mode {mode!r}")
    path = _cache_attempt_path(workdir, mode)
    return {"mode": mode, "status": "fresh_empty_namespace", "path": path, "seeded": False,
            "initial_sha256": digest_bytes(b""), "initial_bytes": 0,
            "isolation": "independent_per_attempt",
            "notes": ["This database starts empty and is not shared with another attempt or arm."]}


def purpose_bucket(purpose):
    return TASK_PURPOSES.get(purpose, "other")


def ledger_row(raw, origin, scope):
    """One ledger attempt. IDs are unique within one OMP invocation, so the key is scoped to the attempt."""
    row_id = raw.get("id")
    if not isinstance(row_id, str) or not row_id:
        return None
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    reported = usage_reported_cost(usage)
    tokens = usage_tokens(usage)
    empty_failed_usage = raw.get("stopReason") in ("error", "aborted") and not any(
        value is not None and value > 0 for value in tokens.values())
    cost_unknown = raw.get("costUnknown") is True or reported is None or empty_failed_usage
    # Stock usage does not attest actual provider billing rather than a catalog calculation.
    estimated = raw.get("estimated") is not False
    retained = {key: raw[key] for key in (
        "id", "parentId", "timestamp", "purpose", "role", "api", "provider", "model", "stopReason",
        "selectedModel", "primaryModel", "fallback", "responseModel", "cache", "questions",
        "cachedQuestions", "estimated", "costUnknown") if key in raw}
    retained["usage"] = {**tokens, "cost": reported}
    return {
        "key": f"{scope}/{origin}/{row_id}",
        "id": row_id,
        "attempt_id": scope,
        "origin": origin,
        "purpose": raw.get("purpose"),
        "role": raw.get("role"),
        "bucket": purpose_bucket(raw.get("purpose")),
        "timestamp": raw.get("timestamp"),
        "api": raw.get("api"),
        "provider": raw.get("provider"),
        "model": raw.get("model"),
        "stop_reason": raw.get("stopReason"),
        "selected_model": raw.get("selectedModel"),
        "primary_model": raw.get("primaryModel"),
        "fallback": raw.get("fallback"),
        "response_model": raw.get("responseModel"),
        "cache": raw.get("cache"),
        "questions": raw.get("questions"),
        "cached_questions": raw.get("cachedQuestions"),
        "estimated": estimated,
        "cost_unknown": cost_unknown,
        "cost_basis": "unknown" if cost_unknown else "catalog_estimate" if estimated else "reported",
        "reported_cost": None if cost_unknown else reported,
        "tokens": tokens,
        "raw": retained,
    }


def read_session_usage(session_dir, scope, repo, complete):
    """Project only accounting rows from this invocation's private persisted session.

    An isolated invocation should own exactly one top-level session. Ambiguous, foreign,
    truncated or unfinished sessions never establish complete ledger collection. Collection
    does not establish billing coverage: stock completion calls are not journaled here. No session
    transcripts, credential pins, diagnostic error strings or paths enter the projection.
    """
    entries, conflicts = [], []
    files = sorted(Path(session_dir).glob("*.jsonl"))
    evidence = {"source": "persisted_session_model_usage", "session_files": len(files),
                "invocation_complete": complete, "retained": "accounting_projection_only"}
    if not files:
        return entries, conflicts, "absent", evidence
    if len(files) != 1:
        conflicts.append({"reason": "isolated invocation has ambiguous session files"})
        return entries, conflicts, "incomplete", evidence
    path = files[0]
    if path.is_symlink() or not path.is_file():
        conflicts.append({"reason": "session ledger is not a regular owned file"})
        return entries, conflicts, "incomplete", evidence
    headers, valid_header, malformed = 0, False, False
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                if not line.endswith("\n"):
                    malformed = True
                try:
                    record = json.loads(line)
                except ValueError:
                    malformed = True
                    continue
                if not isinstance(record, dict):
                    malformed = True
                    continue
                if record.get("type") == "session":
                    headers += 1
                    cwd = record.get("cwd")
                    valid_header = (isinstance(record.get("id"), str) and bool(record["id"])
                                    and isinstance(cwd, str) and Path(cwd).resolve() == repo.resolve())
                elif record.get("type") == "model_usage":
                    required = ("id", "timestamp", "purpose", "api", "provider", "model", "stopReason")
                    if not all(isinstance(record.get(key), str) and record[key] for key in required) \
                            or not isinstance(record.get("usage"), dict):
                        malformed = True
                    row = ledger_row(record, "auxiliary", scope)
                    if row is not None:
                        entries.append(row)
                    else:
                        malformed = True
    except (OSError, UnicodeError, ValueError):
        malformed = True
    if headers != 1 or not valid_header:
        conflicts.append({"reason": "session header does not identify this invocation workspace"})
        # Never attach unrelated session costs to this attempt, even as a subtotal.
        entries = []
    if malformed:
        conflicts.append({"reason": "persisted session ledger is malformed or truncated"})
    state = "complete" if complete and not conflicts else "incomplete"
    return entries, conflicts, state, evidence


def accumulate_bucket(entries):
    tokens = {name: 0 for name in TOKEN_FIELDS}
    reported, unknown, estimated, tokens_complete = 0.0, False, False, True
    for entry in entries:
        estimated = estimated or entry["estimated"]
        unknown = unknown or entry["cost_unknown"]
        if entry["reported_cost"] is not None:
            reported += entry["reported_cost"]
        for name, value in entry["tokens"].items():
            if value is None:
                tokens_complete = False
            else:
                tokens[name] += value
    return {"entries": len(entries), "reported_cost": None if unknown else reported,
            "reported_subtotal": reported, "cost_unknown": unknown, "estimated": estimated,
            "tokens": tokens, "tokens_complete": tokens_complete}


def finalize_accounting(entries, conflicts, snapshots):
    """Sum scoped ledger keys once; snapshot states describe collection, not billing coverage.

    Stock completion calls are not journaled in model_usage. Even a settled, fully read
    session (including one with judgment rows) cannot attest completion or total cost.
    """
    unique, seen, duplicates = [], {}, []
    conflicts = list(conflicts)
    for entry in entries:
        previous = seen.get(entry["key"])
        if previous is None:
            seen[entry["key"]] = entry
            unique.append(entry)
            continue
        duplicates.append(entry["key"])
        if previous["raw"] != entry["raw"]:
            conflicts.append({"key": entry["key"], "reason": "same ledger id has conflicting payloads"})
    reasons = []
    if not snapshots or "absent" in snapshots:
        reasons.append("auxiliary ledger snapshot absent")
    if "incomplete" in snapshots:
        reasons.append("persisted auxiliary ledger coverage incomplete")
    if conflicts:
        reasons.append("ledger conflict")
    collection_complete = not reasons
    buckets = {
        "outer": [entry for entry in unique if entry["origin"] == "outer"],
        "judgment": [entry for entry in unique if entry["origin"] == "auxiliary" and entry["bucket"] == "judgment"],
        "completion": [entry for entry in unique if entry["origin"] == "auxiliary" and entry["bucket"] == "completion"],
        "other": [entry for entry in unique if entry["origin"] == "auxiliary" and entry["bucket"] == "other"],
    }
    buckets["auxiliary"] = buckets["judgment"] + buckets["completion"] + buckets["other"]
    buckets["total"] = buckets["outer"] + buckets["auxiliary"]
    totals = {name: accumulate_bucket(rows) for name, rows in buckets.items()}
    if totals["total"]["cost_unknown"]:
        reasons.append("an observed attempt has unknown cost")
    observed_cost_incomplete = bool(reasons)
    reasons.append("stock completion journaling unavailable; completion billing coverage unknown")
    for name, bucket in totals.items():
        # Row/token sums remain observed subtotals, never evidence of missing calls costing zero.
        bucket["coverage_unknown"] = not collection_complete or name in ("completion", "auxiliary", "total")
        if bucket["coverage_unknown"]:
            bucket["tokens_complete"] = False
        if observed_cost_incomplete or bucket["coverage_unknown"]:
            bucket["reported_cost"] = None
            bucket["cost_unknown"] = True
    return {
        "entries": unique,
        "conflicts": conflicts,
        "snapshot_statuses": list(snapshots),
        "collection_status": "complete" if collection_complete else "incomplete",
        "duplicate_keys_ignored": sorted(set(duplicates)),
        "monetary_status": "incomplete",
        "monetary_reasons": reasons,
        "totals": totals,
        "grading_cost": {"measured": False, "reported_cost": None,
                         "notes": ["Execution results never include grading overhead."]},
    }


def merge_accounting(records):
    # Reapply stock coverage limits after merging; complete collection never seals billing.
    return finalize_accounting([entry for record in records for entry in record["entries"]],
                               [conflict for record in records for conflict in record["conflicts"]],
                               [state for record in records for state in (record["snapshot_statuses"] or ["absent"])])


def cache_evidence(rows):
    counts = {"miss": 0, "partial": 0, "hit": 0, "unknown": 0}
    known = {"questions": 0, "cached_questions": 0}
    unknown = {"questions": 0, "cached_questions": 0}
    for row in rows:
        counts[row.get("cache") if row.get("cache") in ("miss", "partial", "hit") else "unknown"] += 1
        for name in known:
            value = row.get(name)
            if type(value) is int and value >= 0:
                known[name] += value
            else:
                unknown[name] += 1
    return {"rows": len(rows), "row_counts": counts,
            "questions_known": known["questions"], "questions_unknown_rows": unknown["questions"],
            "cached_questions_known": known["cached_questions"],
            "cached_questions_unknown_rows": unknown["cached_questions"]}


def receipt_projection(receipt):
    """Keep method evidence, not provider diagnostic strings or rubric/transcript copies."""
    fields = ("schema", "strategy", "question_hash", "frozen_question_hash", "batch_ids", "unit_ids",
              "original_ids", "answered_ids", "error_ids", "excluded_ids", "group_sizes", "group_slots",
              "complete", "provenance_complete", "cache_counts", "questions", "cached_questions",
              "question_counts_unknown")
    projected = {key: receipt[key] for key in fields if key in receipt}
    for key in ("coverage_errors",):
        errors = receipt.get(key)
        projected[key] = ["host coverage failure"] * len(errors) if isinstance(errors, list) else None
    exclusions = receipt.get("exclusions")
    if isinstance(exclusions, dict):
        projected["exclusions"] = {
            uid: reason if reason in ("empty rendered unit", "empty group state") else "excluded"
            for uid, reason in exclusions.items()}
    batches = receipt.get("batches")
    if isinstance(batches, list):
        projected["batches"] = []
        for batch in batches:
            if not isinstance(batch, dict):
                projected["batches"].append(None)
                continue
            value = {key: batch[key] for key in ("batch_id", "planned_ids", "answered_ids", "error_ids")
                     if key in batch}
            errors = batch.get("coverage_errors")
            value["coverage_errors"] = ["host coverage failure"] * len(errors) if isinstance(errors, list) else None
            items = batch.get("items")
            if isinstance(items, list):
                value["items"] = [
                    {key: item[key] for key in (
                        "id", "batch_id", "selected_model", "primary_model", "response_model",
                        "fallback", "cache", "questions", "cached_questions") if key in item}
                    if isinstance(item, dict) else None for item in items]
            projected["batches"].append(value)
    return projected


def parse_receipts(results):
    """Helper receipts found in actual eval tool results; identical receipt objects count once."""
    receipts, seen = [], set()
    for value in structured_values(results):
        receipt = value.get("jevify_receipt") if isinstance(value.get("jevify_receipt"), dict) else None
        if receipt is None and {"strategy", "batch_ids", "unit_ids"} <= set(value):
            receipt = value
        if receipt is None or receipt.get("schema") != 2 or not isinstance(receipt.get("unit_ids"), list):
            continue
        receipt = receipt_projection(receipt)
        identity = digest_json(receipt)
        if identity not in seen:
            seen.add(identity)
            receipts.append(receipt)
    return receipts


def receipt_outcomes(receipts, planned_ids):
    """Latest actual outcome per unit across receipts. Unresolved units are reported, not scored as adherence."""
    latest = {}
    for receipt in receipts:
        for uid in string_ids(receipt.get("answered_ids")) or []:
            latest[uid] = "answered"
        for uid in string_ids(receipt.get("error_ids")) or []:
            latest[uid] = "error"
        for uid in string_ids(receipt.get("excluded_ids")) or []:
            latest[uid] = "excluded"
    planned = planned_ids if planned_ids is not None else list(latest)
    return {
        "planned": None if planned_ids is None else len(planned_ids),
        "answered_ids": [uid for uid in planned if latest.get(uid) == "answered"],
        "error_ids": [uid for uid in planned if latest.get(uid) == "error"],
        "excluded_ids": [uid for uid in planned if latest.get(uid) == "excluded"],
        "uncovered_ids": [uid for uid in planned if uid not in latest],
        "unplanned_ids": [] if planned_ids is None else sorted(set(latest) - set(planned_ids)),
    }


def receipt_problems(receipt, strategy, rubric_hash):
    if any(receipt.get(name) is None for name in ("unit_ids", "answered_ids", "error_ids")):
        return []  # Missing evidence is handled as unverified, not a method violation.
    units, answered, errors = (string_ids(receipt.get(name)) for name in ("unit_ids", "answered_ids", "error_ids"))
    if units is None or answered is None or errors is None:
        return ["receipt ID lists are malformed"]
    problems = []
    if any(len(set(ids)) != len(ids) for ids in (units, answered, errors)):
        problems.append("receipt repeats a unit ID")
    if set(answered) & set(errors) or set(answered) | set(errors) != set(units):
        problems.append("receipt answered and error IDs do not partition its unit IDs")
    originals, excluded = (string_ids(receipt.get(name)) for name in ("original_ids", "excluded_ids"))
    exclusions = receipt.get("exclusions")
    if all(receipt.get(name) is not None for name in ("original_ids", "excluded_ids", "exclusions")) and \
            (originals is None or excluded is None or not isinstance(exclusions, dict)):
        problems.append("receipt original/exclusion manifest is malformed")
    elif originals is not None and excluded is not None and isinstance(exclusions, dict) and (\
            len(set(originals)) != len(originals) or len(set(excluded)) != len(excluded) or \
            set(units) & set(excluded) or set(units) | set(excluded) != set(originals) or \
            set(exclusions) != set(excluded)):
        problems.append("receipt units and exclusions do not partition its original IDs")
    if receipt.get("coverage_errors"):
        problems.append("receipt reports host coverage failures")
    if rubric_hash is not None and receipt.get("question_hash") is not None and receipt["question_hash"] != rubric_hash:
        problems.append("receipt question hash differs from the unchanged public rubric")
    sizes, slots = receipt.get("group_sizes"), receipt.get("group_slots")
    if strategy == "jev-grouped":
        if sizes is None or slots is None:
            return problems
        if not isinstance(sizes, list) or not sizes or any(type(size) is not int for size in sizes) \
                or not isinstance(slots, dict):
            return problems + ["grouped receipt lacks group sizes and slots"]
        members = [string_ids(ids) for ids in slots.values()]
        if any(ids is None for ids in members) or [len(ids) for ids in members] != sizes:
            problems.append("grouped receipt sizes do not match its slots")
        elif sorted(uid for ids in members for uid in ids) != sorted(units):
            problems.append("grouped receipt slots do not cover exactly its unit IDs")
        if any(not 1 <= size <= GROUPED_K for size in sizes):
            problems.append(f"grouped receipt has a group outside 1..{GROUPED_K}")
        if max(sizes) < 2:
            problems.append("grouped receipt has no multi-slot group")
    elif sizes or slots:
        problems.append("flat receipt reports groups")
    if receipt.get("complete") is False and not errors and not receipt.get("excluded_ids"):
        problems.append("receipt is incomplete without unit errors or exclusions")
    return problems


def answered_host_items(receipt):
    """Host judgment items a receipt claims returned answers: states for flat, groups for grouped."""
    answered = set(string_ids(receipt.get("answered_ids")) or [])
    slots = receipt.get("group_slots")
    if isinstance(slots, dict) and slots:
        return sum(1 for ids in slots.values() if answered & set(string_ids(ids) or []))
    return len(answered)


def receipt_cache_evidence(receipt):
    """Corroboration from a model-printable helper result, never independent host billing proof."""
    counts = receipt.get("cache_counts")
    batch_ids = string_ids(receipt.get("batch_ids"))
    batches = receipt.get("batches")
    if not batch_ids or not all(batch_ids) or not isinstance(batches, list) or not batches:
        return None
    if any(not isinstance(batch, dict) for batch in batches) or \
            [batch.get("batch_id") for batch in batches] != batch_ids or \
            len(set(batch_ids)) != len(batch_ids) or \
            any(batch.get("coverage_errors") != [] for batch in batches):
        return None
    slots = receipt.get("group_slots")
    units = string_ids(receipt.get("unit_ids"))
    if units is None or (slots is not None and not isinstance(slots, dict)):
        return None
    if slots and any(string_ids(ids) is None for ids in slots.values()):
        return None
    host_ids = set(slots or units)
    latest, final_items = {}, {}
    for batch in batches:
        planned, answered, errors = (string_ids(batch.get(key)) for key in
                                     ("planned_ids", "answered_ids", "error_ids"))
        if any(ids is None or len(ids) != len(set(ids)) for ids in (planned, answered, errors)):
            return None
        if not set(planned) <= host_ids or set(answered) & set(errors) or \
                set(answered) | set(errors) != set(planned):
            return None
        items = batch.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return None
        seen = set()
        for item in items:
            key = item.get("id")
            if not isinstance(key, str) or key not in planned or key in seen or \
                    item.get("batch_id") != batch["batch_id"]:
                return None
            seen.add(key)
        if not set(answered) <= seen:
            return None
        # A retry supersedes the earlier item even if that retry delivered no item.
        final_items.update({key: None for key in planned})
        final_items.update({item["id"]: item for item in items})
        latest.update({key: "answered" for key in answered})
        latest.update({key: "error" for key in errors})
    if set(latest) != host_ids:
        return None
    answered_units = string_ids(receipt.get("answered_ids"))
    if answered_units is None:
        return None
    expected_answers = ({key for key, ids in slots.items() if set(ids) & set(answered_units)}
                        if slots else set(answered_units))
    if {key for key, outcome in latest.items() if outcome == "answered"} != expected_answers:
        return None
    if not expected_answers:
        return None
    observed = {"hit": 0, "partial": 0, "miss": 0, "unknown": 0}
    total_questions = total_cached = unknown_questions = cached_answers = 0
    for key, item in final_items.items():
        if item is None:
            observed["unknown"] += 1
            unknown_questions += 1
            continue  # A reported unit error is an outcome, not proof of successful judging.
        selected, primary = item.get("selected_model"), item.get("primary_model")
        cache, questions, cached = item.get("cache"), item.get("questions"), item.get("cached_questions")
        counts_known = type(questions) is int and type(cached) is int and 0 <= cached <= questions
        if key in expected_answers:
            if not isinstance(selected, str) or not selected or not isinstance(primary, str) or not primary or \
                    type(item.get("fallback")) is not bool or item["fallback"] != (selected != primary):
                return None
            if cache not in ("hit", "partial", "miss") or not counts_known or questions < 1:
                return None
            expected_cache = "hit" if cached == questions else "miss" if cached == 0 else "partial"
            if cache != expected_cache:
                return None
            cached_answers += cache == "hit"
        observed[cache if cache in ("hit", "partial", "miss") else "unknown"] += 1
        if counts_known:
            total_questions += questions
            total_cached += cached
        else:
            unknown_questions += 1
    if not isinstance(counts, dict) or any(type(counts.get(key)) is not int or counts[key] < 0
                                           for key in ("hit", "partial", "miss", "unknown")):
        return None
    host_items = len(receipt.get("group_slots") or receipt.get("unit_ids") or [])
    if sum(counts[key] for key in ("hit", "partial", "miss", "unknown")) != host_items or \
            receipt.get("question_counts_unknown") != unknown_questions:
        return None
    questions, cached = receipt.get("questions"), receipt.get("cached_questions")
    if type(questions) is not int or type(cached) is not int or not 0 <= cached <= questions:
        return None
    if observed != counts or questions != total_questions or cached != total_cached:
        return None
    if counts["hit"] == host_items and host_items and (questions <= 0 or cached != questions):
        return None
    return {"cached_items": cached_answers, "uncached_questions": questions - cached,
            "unknown_error_items": observed["unknown"],
            "fully_cached": host_items > 0 and counts["hit"] == host_items
                            and receipt.get("complete") is True,
            "evidence_class": "helper_receipt_not_independent_host_proof"}


def method_evidence(calls, receipts, accounting, plan, strategy):
    auxiliary = [row for row in accounting["entries"] if row["origin"] == "auxiliary"]
    judgments = [row for row in auxiliary if row["bucket"] == "judgment"]
    completions = [row for row in auxiliary if row["bucket"] == "completion"]
    runtime = [row for row in auxiliary if row["bucket"] == "other"]
    markers = []
    for index, call in enumerate(calls):
        code = str((call.get("args") or {}).get("code", "")) if call["tool"] == "eval" else ""
        found = [marker for marker in SOURCE_MARKERS if marker in code]
        if found:
            markers.append({"call_index": index, "markers": found})
    relevant = [receipt for receipt in receipts if receipt.get("strategy") == strategy] \
        if strategy in JEV_STRATEGIES else receipts
    valid_receipts = [receipt for receipt in receipts
                      if receipt.get("strategy") in JEV_STRATEGIES
                      and not receipt_problems(receipt, receipt["strategy"], None)
                      and string_ids(receipt.get("original_ids")) is not None
                      and string_ids(receipt.get("excluded_ids")) is not None
                      and isinstance(receipt.get("exclusions"), dict)
                      and answered_host_items(receipt) > 0
                      and (cache_receipt := receipt_cache_evidence(receipt)) is not None
                      and cache_receipt["cached_items"] > 0]
    return {
        "judged": True if judgments or valid_receipts else None,
        "judged_evidence_class": ("persisted_model_usage" if judgments else
                                  "helper_receipt_not_independent_host_proof" if valid_receipts else
                                  "unverified"),
        "receipt_evidence_class": "tool_result_model_printable_not_independent_host_proof",
        "receipt_cache": [receipt_cache_evidence(receipt) for receipt in receipts],
        "snapshot_statuses": accounting["snapshot_statuses"],
        "conflicts": accounting["conflicts"],
        "actual_receipts": receipts,
        "actual_receipt_strategies": sorted({receipt.get("strategy") for receipt in receipts
                                             if isinstance(receipt.get("strategy"), str)}),
        "task_judgment_ledger_rows": len(judgments),
        "task_completion_ledger_rows": len(completions),
        "runtime_auxiliary_ledger_rows": len(runtime),
        "runtime_auxiliary_purposes": sorted({str(row.get("purpose")) for row in runtime}),
        "cache": cache_evidence(judgments),
        "plan": plan,
        "unit_outcomes": receipt_outcomes(relevant, plan["planned_ids"]),
        "source_marker_diagnostics": markers,
        "source_marker_diagnostics_are_not_execution_proof": True,
    }


def adherence(strategy, method):
    """Whether the requested method actually ran. Unit errors are outcomes, reported in unit_outcomes."""
    if strategy == "auto":
        return {"status": "not_applicable", "passed": None, "reasons": [], "unverified": []}
    receipts = method["actual_receipts"]
    judgments = method["task_judgment_ledger_rows"]
    reasons, unverified = [], []
    if strategy == "direct-script":
        if judgments or method["task_completion_ledger_rows"]:
            reasons.append("task-driven nested judgment or completion attempt observed")
        if receipts:
            reasons.append("Jevify helper receipt observed")
        snapshots = method.get("snapshot_statuses")
        if not snapshots or any(state != "complete" for state in snapshots) or method.get("conflicts"):
            unverified.append("auxiliary ledger coverage is not complete")
        if not reasons:
            unverified.append("persisted usage does not record cache-only judgments")
    else:
        plan = method["plan"]
        matching = [receipt for receipt in receipts if receipt.get("strategy") == strategy]
        if not matching:
            unverified.append(f"no actual {strategy} helper receipt")
        if len(matching) != len(receipts):
            reasons.append("a helper receipt with another strategy was observed")
        for index, receipt in enumerate(matching):
            required = ("original_ids", "excluded_ids", "exclusions", "answered_ids", "error_ids",
                        "group_sizes", "group_slots", "complete", "batch_ids", "batches", "question_hash",
                        "coverage_errors")
            missing = [name for name in required if receipt.get(name) is None]
            if missing:
                unverified.append(f"receipt {index}: missing evidence fields: {', '.join(missing)}")
            reasons.extend(f"receipt {index}: {problem}"
                           for problem in receipt_problems(receipt, strategy, plan["rubric_hash"]))
            if not string_ids(receipt.get("batch_ids")) or not all(receipt["batch_ids"]):
                unverified.append(f"receipt {index}: host batch identity unavailable")
        if plan["rubric_hash"] is None:
            unverified.append("public rubric unavailable; question hash not checked")
        covered = {uid for receipt in matching for uid in string_ids(receipt.get("unit_ids")) or []}
        if plan["planned_ids"] is None:
            unverified.append("public unit plan unavailable; coverage not checked")
        elif matching:
            planned = set(plan["planned_ids"])
            if planned - covered:
                reasons.append(f"receipts do not cover {len(planned - covered)} planned IDs")
            if covered - planned:
                reasons.append(f"receipts cover {len(covered - planned)} IDs outside the public plan")
        cached = [receipt_cache_evidence(receipt) for receipt in matching]
        cached_items = sum(item["cached_items"] for item in cached if item is not None)
        if judgments + cached_items < sum(answered_host_items(receipt) for receipt in matching):
            unverified.append("billed rows and explicit cache receipts do not corroborate every answered item")
        if matching and any(item is None for item in cached):
            unverified.append("explicit helper cache metadata unavailable for corroboration")
        snapshots = method.get("snapshot_statuses")
        if not snapshots or any(state != "complete" for state in snapshots) or method.get("conflicts"):
            unverified.append("auxiliary ledger coverage is not complete")
    status = "violation" if reasons else "unverified" if unverified else "pass"
    return {"status": status, "passed": False if reasons else None if unverified else True,
            "reasons": reasons, "unverified": unverified,
            "evidence_class": "persisted_usage_and_model_printable_helper_receipts"}


def process_outcome(*, deadline, max_calls, forced, returncode, malformed, agent_ended):
    """Process facts beside the answer. A captured answer is kept even when the process failed."""
    problems = []
    if deadline:
        problems.append("forced_deadline")
    elif max_calls:
        problems.append("forced_max_calls")
    elif forced:
        problems.append("forced_runner")
    if returncode != 0:
        problems.append("nonzero_exit")
    if malformed:
        problems.append("malformed_stream")
    if not agent_ended:
        problems.append("missing_agent_end")
    return (problems[0] if problems else "clean_exit"), problems


def answer_outcome(final_answers, answered):
    if final_answers:
        return "final_answer"
    return "empty_stop" if answered else "no_answer"


def execution_status(answered, calls, stop, process_status="clean_exit"):
    if not calls and not answered:
        return "infra"
    if stop in ("deadline", "max_calls"):
        return "incomplete"
    if not answered:
        return "failed"
    return "complete" if process_status == "clean_exit" else "process_failed"


def route_evidence(rows, receipts=()):
    rows = [row for row in rows if row["origin"] == "auxiliary"]
    judgments = [row for row in rows if row.get("bucket") == "judgment"]
    items = [item for receipt in receipts for batch in receipt.get("batches", []) if isinstance(batch, dict)
             for item in batch.get("items", []) if isinstance(item, dict)]
    return {
        "evidence_class": "persisted_usage_metadata",
        "helper_receipts": {
            "evidence_class": "model_printable_helper_items_not_independent_host_proof",
            "items": len(items),
            "selected_models": sorted({item["selected_model"] for item in items
                                       if isinstance(item.get("selected_model"), str)}),
            "primary_models": sorted({item["primary_model"] for item in items
                                      if isinstance(item.get("primary_model"), str)}),
            "response_models": sorted({item["response_model"] for item in items
                                       if isinstance(item.get("response_model"), str)}),
            "fallback_items": sum(item.get("fallback") is True for item in items),
            "unknown_route_items": sum(not isinstance(item.get("selected_model"), str)
                                       or not isinstance(item.get("primary_model"), str)
                                       or type(item.get("fallback")) is not bool for item in items),
        },
        "selected_models": sorted({row["selected_model"] for row in rows if isinstance(row.get("selected_model"), str)}),
        "primary_models": sorted({row["primary_model"] for row in rows if isinstance(row.get("primary_model"), str)}),
        "response_models": sorted({row["response_model"] for row in rows if isinstance(row.get("response_model"), str)}),
        "fallback_rows": sum(row.get("fallback") is True for row in rows),
        "judgment_rows": len(judgments),
        "provenance_unknown_rows": sum(row.get("selected_model") is None or row.get("primary_model") is None
                                       or type(row.get("fallback")) is not bool
                                       for row in judgments),
        "response_unknown_rows": sum(not isinstance(row.get("response_model"), str) for row in judgments),
    }


def count_fetch_rate_callsites(repo):
    """The fixture key's call-site rule, applied to the launch fixture the subject will see."""
    return sum("fetch_rate(" in line and not line.lstrip().startswith("def ")
               for path in sorted((Path(repo) / "billing").rglob("*.py"))
               for line in path.read_text(encoding="utf-8").splitlines())


def _repo_python_files(repo):
    return [path for path in sorted(Path(repo).rglob("*.py")) if ".git" not in path.relative_to(repo).parts]


def control_expectation(case, repo):
    """Control facts taken from the launch fixture before the subject acts."""
    spec = case.get("control")
    if spec is None:
        return None
    if spec["kind"] == "answer_integer":
        present = (Path(repo) / "billing").is_dir()
        return {"kind": spec["kind"], "precondition": present,
                "expected": count_fetch_rate_callsites(repo) if present else None}
    target = Path(repo) / spec["file"]
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    match = re.search(rf"^{re.escape(spec['old'])}\s*=\s*(.+?)\s*$", text, re.M)
    value = None if match is None else match.group(1)
    return {"kind": spec["kind"], "precondition": value is not None,
            "expected": {"old": spec["old"], "new": spec["new"], "file": spec["file"], "value": value}}


def control_outcome(expectation, repo, final_answer, answer_status):
    """Deterministic correctness: a bare integer answer, or the post-session repository state."""
    if expectation is None:
        return None
    result = {"schema": 1, "kind": expectation["kind"], "expected": expectation["expected"],
              "precondition": expectation["precondition"], "observed": None, "correct": None, "reasons": [],
              "evidence_class": "deterministic_runner_check"}
    if not expectation["precondition"]:
        result["reasons"].append("control precondition absent from the launch fixture")
        return result
    if expectation["kind"] == "answer_integer":
        if answer_status != "final_answer":
            result.update(correct=False, reasons=["no final answer"])
            return result
        match = re.fullmatch(r"\s*(\d+)\.?\s*", final_answer)
        if match is None:
            result.update(correct=False, reasons=["final answer is not a single integer"])
            return result
        result["observed"] = int(match.group(1))
        result["correct"] = result["observed"] == expectation["expected"]
        return result
    spec = expectation["expected"]
    target = Path(repo) / spec["file"]
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    stale = [path.relative_to(repo).as_posix() for path in _repo_python_files(repo)
             if re.search(rf"\b{re.escape(spec['old'])}\b", path.read_text(encoding="utf-8", errors="replace"))]
    renamed = re.search(rf"^{re.escape(spec['new'])}\s*=\s*{re.escape(spec['value'])}\s*$", text, re.M) is not None
    result["observed"] = {"stale_references": stale, "renamed_with_original_value": renamed}
    result["correct"] = not stale and renamed
    if stale:
        result["reasons"].append("old identifier is still referenced")
    if not renamed:
        result["reasons"].append("new identifier with the original value is absent")
    return result


def question_coverage(method):
    """Whether an attempt's question counts are exhaustive. Stock hosts supply no proof of that.

    Ledger rows are not completeness proof: cached judging can emit zero rows, and an unverified judge
    (judged None) can leave none. Only explicit exhaustive host evidence may set complete; no supported
    channel exists yet, so every attempt is incomplete. Change this function, not the callers, when one does.
    """
    return {"complete": False, "evidence_class": "no_exhaustive_host_question_count",
            "judged": method["judged"], "ledger_rows": method["cache"]["rows"]}


class Envelope:
    """Authorized exposure gate: reserves each attempt before it starts and observes its question coverage.

    Attempts and concurrency are enforced independently. A question limit is an observed-only stop guard:
    it counts host-reported known questions as a lower bound, and it stops scheduling as
    questions_unverifiable whenever question coverage is incomplete, which is never counted as zero.
    An attempt already running may finish past a limit; the envelope bounds scheduling, not billing.
    """

    def __init__(self, attempts=None, questions=None):
        self.limits = {"attempts": attempts, "questions": questions}
        self._lock = threading.Lock()
        self.attempts_reserved = 0
        self.questions_observed = 0
        self.questions_unknown_rows = 0
        self.coverage_unknown = False
        self.exhausted_by = None

    def reserve(self):
        with self._lock:
            if self.limits["attempts"] is not None and self.attempts_reserved >= self.limits["attempts"]:
                return self._refuse("attempts")
            if self.limits["questions"] is not None:
                # Incomplete coverage is not zero: without exhaustive counts a question limit cannot be checked.
                if self.coverage_unknown or self.questions_unknown_rows:
                    return self._refuse("questions_unverifiable")
                if self.questions_observed >= self.limits["questions"]:
                    return self._refuse("questions")
            self.attempts_reserved += 1
            return True

    def _refuse(self, reason):
        self.exhausted_by = self.exhausted_by or reason
        return False

    def question_exposure_status(self):
        """Actual question exposure. Known questions alone are a lower bound, never an exposure."""
        if not self.attempts_reserved:
            return "not_observed"
        return "unknown" if self.coverage_unknown or self.questions_unknown_rows else "observed_complete"

    def observe(self, questions, unknown_rows, coverage_complete=False):
        with self._lock:
            self.questions_observed += questions
            self.questions_unknown_rows += unknown_rows
            self.coverage_unknown = self.coverage_unknown or not coverage_complete

    def record(self):
        return {"schema": 2, "limits": dict(self.limits), "attempts_reserved": self.attempts_reserved,
                "questions_observed": self.questions_observed,
                "questions_unknown_rows": self.questions_unknown_rows,
                "question_coverage_unknown": self.coverage_unknown,
                "question_guard": "observed_only_stop" if self.limits["questions"] is not None else "none",
                "question_exposure": self.question_exposure_status(),
                "exhausted_by": self.exhausted_by,
                "dollar_envelope": None, "hard_dollar_guarantee": False,
                "notes": ["Limits gate scheduling; an in-flight attempt may finish past a limit.",
                          "questions_observed is a known subtotal, a lower bound on actual questions.",
                          "Incomplete question coverage, including zero ledger rows, stops a question-limited "
                          "envelope as questions_unverifiable; it is never counted as zero.",
                          "Question exposure is never reported as bounded: the limit is an observed-only stop guard."]}


def preflight_plan(args, planned_runs, concurrency):
    """Planned exposure before any session. Refusals are explicit; nothing is silently reduced."""
    authorized = {"attempts": args.authorize_attempts, "questions": args.authorize_questions,
                  "concurrency": args.authorize_concurrency}
    refusals = []
    if authorized["attempts"] is not None and authorized["attempts"] < planned_runs:
        refusals.append(f"authorized attempts {authorized['attempts']} cannot cover one attempt per planned run "
                        f"({planned_runs})")
    return {"schema": 1, "planned_runs": planned_runs,
            "attempt_upper_bound": planned_runs * (args.retries + 1), "authorized": authorized,
            "effective_concurrency": concurrency,
            "per_attempt": {"max_time_seconds": args.max_time, "max_calls": args.max_calls,
                            "watchdog_seconds": args.max_time + 30},
            "question_exposure": ("unbounded: no question limit was authorized; actual question exposure is unknown"
                                  if authorized["questions"] is None
                                  else "observed-only stop guard: scheduling stops once observed known questions reach "
                                       "the limit, and stops as questions_unverifiable once question coverage is "
                                       "incomplete (stock hosts never prove completeness); actual question exposure "
                                       "is not bounded"),
            "dollar_exposure": {"ceiling": None, "hard_dollar_guarantee": False,
                                "reason": "billing is unknown or catalog-estimated; no dollar envelope is enforced"},
            "refusals": refusals}


def load_thresholds(raw):
    """Parse the predeclared decision rule. Its identity is the canonical digest of the parsed rule."""
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("confidence") != CONFIDENCE:
        raise ValueError("thresholds need schema 1 and confidence 0.95")
    if type(value.get("min_cases")) is not int or value["min_cases"] < 2:
        raise ValueError("thresholds need min_cases of at least 2 independent cases")
    metrics, rules = value.get("metrics"), value.get("rules")
    if not isinstance(metrics, dict) or set(metrics) != set(THRESHOLD_ROLES) \
            or not all(isinstance(name, str) and name for name in metrics.values()):
        raise ValueError("thresholds need correctness, coverage and unsafe metric names")
    rule_names = ("correctness_min_delta", "coverage_max_regression", "unsafe_max_increase")
    if not isinstance(rules, dict) or set(rules) != set(rule_names) \
            or not all(isinstance(rules[name], (int, float)) and not isinstance(rules[name], bool)
                       for name in rule_names):
        raise ValueError("thresholds need numeric correctness, coverage and unsafe rules")
    if rules["coverage_max_regression"] < 0 or rules["unsafe_max_increase"] < 0:
        raise ValueError("coverage and unsafe limits are magnitudes and must be non-negative")
    return value, digest_json(value)


def mean_or_none(values):
    numbers = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return round(sum(numbers) / len(numbers), 1) if numbers else None


def count_by(runs, field):
    counts = {}
    for run in runs:
        name = run.get(field)
        key = "unrecorded" if name is None else str(name)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def control_means(runs):
    """Per-case mean runner-graded control correctness (1/0); unscored attempts are omitted."""
    values = {}
    for run in runs:
        control = run.get("control")
        if isinstance(control, dict) and control.get("correct") is not None:
            values.setdefault(run["case"], []).append(1.0 if control["correct"] else 0.0)
    return {case: sum(items) / len(items) for case, items in values.items()}


def control_summary(runs):
    controls = [run["control"] for run in runs if isinstance(run.get("control"), dict)]
    scored = [control for control in controls if control.get("correct") is not None]
    return {"scored": len(scored), "correct": sum(control["correct"] is True for control in scored),
            "unscored": len(controls) - len(scored)}


def paired_case_comparison(base_case_values, current_case_values, confidence=CONFIDENCE):
    """Case is the independent unit: one value per case per run, then a t-interval over case deltas."""
    if confidence != CONFIDENCE:
        raise ValueError("only the predeclared 0.95 two-sided interval is supported")
    cases = {case: round(current_case_values[case] - base_case_values[case], 6)
             for case in sorted(set(base_case_values) & set(current_case_values))
             if base_case_values[case] is not None and current_case_values[case] is not None}
    unpaired = sorted(case for case in set(base_case_values) | set(current_case_values) if case not in cases)
    count = len(cases)
    if count == 0:
        return {"method": "paired_case_mean_t_interval", "confidence": confidence, "n": 0, "mean_delta": None,
                "interval": None, "cases": cases, "unpaired": unpaired}
    mean = sum(cases.values()) / count
    interval = None
    if count >= 2:
        df = count - 1
        variance = sum((delta - mean) ** 2 for delta in cases.values()) / df
        t_value = T_CRITICAL_95[df] if df <= 30 else 1.96
        half_width = t_value * math.sqrt(variance / count)
        interval = {"low": round(mean - half_width, 6), "high": round(mean + half_width, 6),
                    "df": df, "t_critical": t_value}
    return {"method": "paired_case_mean_t_interval", "confidence": confidence, "n": count,
            "mean_delta": round(mean, 6), "interval": interval, "cases": cases, "unpaired": unpaired}


def threshold_recommendation(metrics, thresholds, contract_check, predeclared):
    """Predeclared rule over case-level intervals. Withholds rather than choosing a threshold after results."""
    reasons = [f"controlled: {name}" for name in contract_check["controlled_differences"]]
    if contract_check.get("reason"):
        reasons.append(contract_check["reason"])
    if not contract_check["one_variable"] and not reasons:
        reasons.append("treatment does not differ in exactly one field")
    if reasons or not contract_check["compatible"] or not contract_check["one_variable"]:
        return {"verdict": "not_comparable", "reasons": reasons}
    if predeclared is None or predeclared.get("sha256") != digest_json(thresholds) \
            or predeclared.get("thresholds") != thresholds:
        return {"verdict": "withheld", "reasons": ["thresholds are not the identical predeclared rule"]}
    results = {role: metrics.get(thresholds["metrics"][role]) for role in THRESHOLD_ROLES}
    if any(result is None or result["interval"] is None for result in results.values()):
        return {"verdict": "inconclusive", "reasons": ["a required metric has no interval"]}
    shared = min(result["n"] for result in results.values())
    if shared < thresholds["min_cases"]:
        return {"verdict": "inconclusive",
                "reasons": [f"{shared} shared independent cases; min_cases is {thresholds['min_cases']}"]}
    rules = thresholds["rules"]
    correct, coverage, unsafe = (results[role]["interval"] for role in THRESHOLD_ROLES)
    if unsafe["low"] > rules["unsafe_max_increase"]:
        return {"verdict": "contradicts_candidate",
                "reasons": ["unsafe metric increase exceeds its limit beyond chance"]}
    if correct["high"] < 0 or coverage["high"] < -rules["coverage_max_regression"]:
        return {"verdict": "contradicts_candidate",
                "reasons": ["correctness or coverage falls below its bound beyond chance"]}
    if correct["low"] >= rules["correctness_min_delta"] and coverage["low"] >= -rules["coverage_max_regression"] \
            and unsafe["high"] <= rules["unsafe_max_increase"]:
        return {"verdict": "supports_candidate", "reasons": []}
    return {"verdict": "inconclusive", "reasons": ["intervals do not clear the predeclared rule"]}


def contract_differences(base_contract, current_contract):
    """Full controlled-contract comparison; a missing field on either side is a difference, not a match."""
    if not isinstance(base_contract, dict) or not isinstance(base_contract.get("controlled"), dict) \
            or not isinstance(base_contract.get("treatment"), dict):
        return {"compatible": False, "one_variable": False, "controlled_differences": [],
                "treatment_differences": [], "unrecorded": ["comparison_contract"],
                "reason": "baseline predates the recorded comparison contract"}
    now_controlled, now_treatment = current_contract["controlled"], current_contract["treatment"]
    controlled = [name for name in CONTRACT_CONTROLLED
                  if base_contract["controlled"].get(name) != now_controlled.get(name)]
    treatment = [name for name in CONTRACT_TREATMENT
                 if base_contract["treatment"].get(name) != now_treatment.get(name)]
    return {"compatible": not controlled, "one_variable": not controlled and len(treatment) == 1,
            "controlled_differences": controlled, "treatment_differences": treatment,
            "unrecorded": [], "reason": None}


def comparison_contract(args, *, cases_sha, selected_ids, identities, source, skill, runner, cache_seed,
                        thresholds_sha, concurrency):
    """What a one-variable comparison must hold fixed, plus the treatment fields allowed to differ."""
    controlled = {
        "runner": runner["sha256"],
        "source": digest_json({"kind": source["kind"], "source_manifest": source["source_manifest_sha256"]}),
        "executable": source["executable"]["sha256"],
        "host": {"system": platform.system(), "machine": platform.machine(), "python": runner["python"]},
        "model_requested": args.model or "default-role",
        "thinking_requested": args.thinking or "configured",
        "cases": {"sha256": cases_sha, "selected_ids": selected_ids},
        "fixtures": digest_json(identities),
        "cache": {"mode": args.cache_mode,
                  "seed_initial_sha256": None if cache_seed is None else cache_seed["initial_sha256"]},
        "method_settings": METHOD_SETTINGS,
        "limits": {"max_time": args.max_time, "max_calls": args.max_calls, "retries": args.retries,
                   "reps": args.reps, "concurrency": concurrency},
        "session_flags": list(SESSION_FLAGS),
        "tools": TOOLS.split(","),
        "envelope": {"attempts": args.authorize_attempts, "questions": args.authorize_questions},
        "predeclared_thresholds": thresholds_sha,
    }
    treatment = {"skill": {"arm": args.arm,
                           "bundle": skill["materialized_sha256"] if args.arm == "skill" else None},
                 "strategy": args.strategy}
    return {"controlled": controlled, "treatment": treatment}


def _die_with_parent():
    """Linux: kill the omp session if the runner itself dies (even by SIGKILL)."""
    os.setsid()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def kill_tree(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_one(case, rep, overlay, desc_probe, args, workdir):
    """Run a case, retrying sessions that never produced a tool call or an answer.

    Those are provider or startup failures, not strategy decisions. Every attempt,
    including failed and incomplete ones, remains in the result with its own costs.
    The authorized envelope reserves each attempt, retries included, before it starts.
    """
    envelope = getattr(args, "envelope", None) or Envelope()
    if not envelope.reserve():
        return {"not_scheduled": True, "case": case["id"], "rep": rep, "reason": envelope.exhausted_by}
    attempts, retry_blocked = [], None
    for attempt in range(args.retries + 1):
        if attempt and not envelope.reserve():
            retry_blocked = envelope.exhausted_by
            break
        attempts.append(attempt_one(case, rep, attempt, overlay, desc_probe, args, workdir))
        known = attempts[-1]["method"]["cache"]
        envelope.observe(known["questions_known"], known["questions_unknown_rows"],
                         coverage_complete=attempts[-1]["question_coverage"]["complete"])
        if attempts[-1]["execution_status"] != "infra":
            break
    result = dict(attempts[-1])
    result["attempts"] = len(attempts)
    result["attempt_records"] = attempts
    result["final_attempt_id"] = attempts[-1]["attempt_id"]
    result["retry_blocked_by_envelope"] = retry_blocked
    result["attempt_secs"] = [item["secs"] for item in attempts]
    result["total_secs"] = round(sum(result["attempt_secs"]), 1)
    result["final_attempt_secs"] = attempts[-1]["secs"]
    result["accounting"] = merge_accounting([item["accounting"] for item in attempts])
    result["cost_ledger"] = result["accounting"]
    return result


def attempt_one(case, rep, attempt, overlay, desc_probe, args, workdir):
    attempt_id = f"{case['id']}:{rep}:{attempt}"
    invocation = uuid.uuid4().hex  # namespaces outer messages that carry no provider response id
    dataset = case.get("dataset", "billing")
    repo = fixture.build(workdir / f"{case['id']}-{rep}-{attempt}", scale=case.get("scale", 1), dataset=dataset)
    plan = public_plan(repo)
    control = control_expectation(case, repo)
    prompt = strategy_prompt(case, repo, args.strategy)
    namespace = getattr(args, "cache_seed", None) if args.cache_mode == "warm" else args.cache_namespace
    cache = cache_plan(args.cache_mode, namespace, workdir, case["id"], rep, attempt)
    session_dir = Path(tempfile.mkdtemp(prefix="session-", dir=workdir))
    argv = [args.omp, "--session-dir", str(session_dir), "--mode", "json", "--auto-approve", "--config", str(overlay),
            *SESSION_FLAGS, "--tools", TOOLS, "--max-time", str(args.max_time)]
    if args.model:
        argv += ["--model", args.model]
    if args.thinking:
        argv += ["--thinking", args.thinking]
    argv.append(prompt)
    env = os.environ.copy()
    if cache["path"] is not None:
        env["OMP_JUDGMENT_CACHE_DB"] = str(cache["path"])
    stem = f"{case['id']}-{rep}-{attempt}"
    raw_path, stderr_path = Path(args.raw_dir) / f"{stem}.jsonl", Path(args.raw_dir) / f"{stem}.stderr"
    started = time.time()
    # stderr goes to a file: a pipe nobody drains while stdout is read can block the session.
    with stderr_path.open("w") as stderr_sink:
        proc = subprocess.Popen(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=stderr_sink, text=True, preexec_fn=_die_with_parent)
    LIVE.add(proc)
    calls, errors, model, thinking = [], 0, None, None
    skill_at = helpers = skill_source_ok = stop = None
    answered = False
    messages, final_answers = [], []
    usage = {"input": None, "output": None, "cache_read": None, "cache_write": None, "cost": None}
    entries, conflicts = [], []
    agent_ended = False
    stream_malformed = False
    forced, deadline_reached = threading.Event(), threading.Event()

    def terminate(deadline=False):
        if proc.poll() is None:
            forced.set()
            if deadline:
                deadline_reached.set()
            kill_tree(proc)

    watchdog = threading.Timer(args.max_time + 30, terminate, kwargs={"deadline": True})
    watchdog.start()
    try:
        with raw_path.open("w") as raw:
            for line in proc.stdout:
                raw.write(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    stream_malformed = True
                    continue
                if not isinstance(ev, dict):
                    stream_malformed = True
                    continue
                kind = ev.get("type")
                msg = ev.get("message")
                if model is None and isinstance(msg, dict) and isinstance(msg.get("model"), str):
                    model = msg["model"]
                if kind == "thinking_level_changed" and thinking is None:
                    thinking = ev.get("resolved")
                if kind == "message_end" and isinstance(msg, dict) and msg.get("role") == "assistant":
                    index = len(messages)
                    text = message_text(msg).strip()
                    reason = msg.get("stopReason")
                    response_id = msg.get("responseId")
                    row_id = response_id if isinstance(response_id, str) and response_id else f"outer:{invocation}:{index}"
                    messages.append({"index": index, "stop_reason": reason, "text": text, "ledger_id": row_id})
                    add_usage(usage, msg.get("usage"))
                    failed_outer_unknown_cost = reason in ("error", "aborted") and not any(
                        value is not None and value > 0 for value in usage_tokens(msg.get("usage")).values())
                    # Chat usage carries no billing proof, so its cost is a catalog estimate.
                    entries.append(ledger_row({"id": row_id, "purpose": "outer_assistant", "api": msg.get("api"),
                                               "provider": msg.get("provider"), "model": msg.get("model"),
                                               "usage": msg.get("usage"), "stopReason": reason,
                                               "timestamp": msg.get("timestamp"), "estimated": True,
                                               "costUnknown": failed_outer_unknown_cost},
                                              "outer", attempt_id))
                    if reason == "stop":
                        answered = True
                        if text:
                            final_answers.append({"source": "assistant_message", "text": text})
                if kind == "agent_end":
                    agent_ended = True
                elif kind == "agent_start":
                    agent_ended = False
                if kind == "tool_execution_start":
                    tool, targs = ev.get("toolName"), ev.get("args") or {}
                    calls.append({"id": ev.get("toolCallId"), "tool": tool, "args": targs, "result": None,
                                  "data": "skill://" not in json.dumps(targs)})
                    if skill_at is None and tool == "read" and str(targs.get("path", "")).startswith("skill://jevify"):
                        skill_at = len(calls) - 1
                    if tool == "eval" and helpers is None and "skill://jevify/jevify.py" in str(targs.get("code", "")):
                        helpers = len(calls) - 1
                elif kind == "tool_execution_end":
                    call = next((item for item in calls if item["id"] == ev.get("toolCallId")), None)
                    if call is not None:
                        call["result"] = ev.get("result")
                        call["is_error"] = ev.get("isError") is True
                        if call["is_error"] and call["tool"] == "eval":
                            errors += 1
                        if desc_probe and calls.index(call) == skill_at and skill_source_ok is None:
                            skill_source_ok = desc_probe in json.dumps(ev.get("result"))
                if len(calls) >= args.max_calls:
                    stop = "max_calls"
                    break
    finally:
        try:
            if stop is not None or sys.exc_info()[0] is not None:
                terminate()
            else:
                # agent_end and stdout EOF precede session.dispose in stock print mode.
                # Keep the invocation watchdog active while waiting for natural flush/exit.
                proc.wait()
        finally:
            watchdog.cancel()
            if proc.stdout is not None:
                proc.stdout.close()
            LIVE.discard(proc)
    if deadline_reached.is_set():
        stop = "deadline"
    process_status, process_problems = process_outcome(
        deadline=deadline_reached.is_set(), max_calls=stop == "max_calls", forced=forced.is_set(),
        returncode=proc.returncode, malformed=stream_malformed, agent_ended=agent_ended)
    try:
        auxiliary, session_conflicts, snapshot, session_evidence = read_session_usage(
            session_dir, attempt_id, repo,
            complete=proc.returncode == 0 and agent_ended and not forced.is_set()
                     and not stream_malformed and stop is None)
    finally:
        shutil.rmtree(session_dir)
    entries.extend(auxiliary)
    conflicts.extend(session_conflicts)
    receipts = parse_receipts([call["result"] for call in calls if call["tool"] == "eval"])
    accounting = finalize_accounting(entries, conflicts, [snapshot])
    accounting["session_evidence"] = {**session_evidence, "process_settled": proc.poll() is not None,
                                      "process_returncode": proc.returncode,
                                      "stream_malformed": stream_malformed}
    method = method_evidence(calls, receipts, accounting, plan, args.strategy)
    answer_status = answer_outcome(final_answers, answered)
    status = execution_status(answered, calls, stop, process_status)
    answer_text = final_answers[-1]["text"] if final_answers else ""
    control_result = control_outcome(control, repo, answer_text, answer_status)
    first_data = next((i for i, call in enumerate(calls) if call["data"]), None)
    triggered = skill_at is not None
    return {
        "attempt_id": attempt_id,
        "case": case["id"], "rep": rep, "attempt": attempt, "expect": case["expect"],
        "keyword": bool(case.get("keyword")), "dataset": dataset, "key": case.get("key"),
        "scale": case.get("scale", 1),
        "execution_status": status,
        "answer_status": answer_status,
        "process_status": process_status,
        "process_problems": process_problems,
        "control": control_result,
        "infra": status == "infra",
        # Availability diagnostic: the skill was opened (trigger) or not (no_trigger). Not outcome quality.
        "pass": True if case["expect"] == "either" else triggered if case["expect"] == "trigger" else not triggered,
        "strategy_adherence": adherence(args.strategy, method),
        "skill_read": triggered,
        "skill_before_data": triggered and (first_data is None or skill_at < first_data),
        "helpers_loaded": helpers is not None,
        "helpers_loaded_evidence_class": "source_marker_only",
        "judged": method["judged"],
        "judged_evidence_class": method["judged_evidence_class"],
        "skill_source_ok": skill_source_ok, "eval_errors": errors, "calls": len(calls),
        "stop": stop or "exit", "secs": round(time.time() - started, 1), "model": model,
        "thinking": thinking,
        "first_tools": [call["tool"] for call in calls[:5]],
        "answer": final_answers[-1]["text"] if final_answers else "",
        "final_answers": final_answers,
        "assistant_messages": messages,
        "usage": usage,
        "usage_evidence_class": "outer_assistant_only_compatibility",
        "accounting": accounting,
        "cache": {**cache, "path": None if cache["path"] is None else str(cache["path"]),
                  "observed": method["cache"]},
        "method": method,
        "question_coverage": question_coverage(method),
        "route": route_evidence(accounting["entries"], receipts),
        "prompt_sha256": digest_bytes(prompt.encode()),
        "raw_path": str(raw_path),
        "stderr_path": str(stderr_path),
    }


def summarize(runs, *, planned=None, not_scheduled=0):
    valid = [run for run in runs if not run["infra"]]
    per_case = {}
    for run in valid:
        case = per_case.setdefault(run["case"], {"expect": run["expect"], "keyword": run["keyword"], "n": 0, "pass": 0,
                                                "skill_before_data": 0, "judged": 0, "judged_unverified": 0,
                                                "strategy_adherence_pass": 0, "strategy_adherence_scored": 0,
                                                "execution_incomplete": 0, "control_scored": 0, "control_correct": 0})
        case["n"] += 1
        case["pass"] += run["pass"]
        case["skill_before_data"] += run["skill_before_data"]
        case["judged"] += run.get("judged") is True
        case["judged_unverified"] += run.get("judged") is None
        passed = (run.get("strategy_adherence") or {}).get("passed")
        if passed is not None:
            case["strategy_adherence_scored"] += 1
            case["strategy_adherence_pass"] += passed
        case["execution_incomplete"] += run.get("execution_status") in ("failed", "incomplete")
        control = run.get("control")
        if isinstance(control, dict) and control.get("correct") is not None:
            case["control_scored"] += 1
            case["control_correct"] += control["correct"] is True
    costs = [run["usage"]["cost"] for run in valid if (run.get("usage") or {}).get("cost") is not None]
    auto = [run for run in valid if run["expect"] == "trigger" and not run["keyword"]]
    neg = [run for run in valid if run["expect"] == "no_trigger"]
    kw = [run for run in valid if run["keyword"]]
    either = [run for run in valid if run["expect"] == "either"]
    return {
        "availability_diagnostics_are_not_outcome_quality": True,
        "auto_trigger": f"{sum(run['skill_read'] for run in auto)}/{len(auto)}",
        "cohorts": {
            "planned_runs": planned if planned is not None else len(runs) + not_scheduled,
            "scheduled_runs": len(runs),
            "not_scheduled_runs": not_scheduled,
            "completed_runs": len(valid),
            "infra_final_runs": len(runs) - len(valid),
            "scheduled_mean_total_secs": mean_or_none(run.get("total_secs", run["secs"]) for run in runs),
            "scheduled_mean_final_attempt_secs": mean_or_none(run["secs"] for run in runs),
            "completed_mean_total_secs": mean_or_none(run.get("total_secs", run["secs"]) for run in valid),
            "completed_mean_final_attempt_secs": mean_or_none(run["secs"] for run in valid),
        },
        "auto_judged": f"{sum(run.get('judged') is True for run in auto)}/{len(auto)}",
        "method_use_evidence": count_by(auto, "judged_evidence_class"),
        "auto_judged_unverified": sum(run.get("judged") is None for run in auto),
        "false_trigger": f"{sum(run['skill_read'] for run in neg)}/{len(neg)}",
        "keyword": f"{sum(run['skill_read'] for run in kw)}/{len(kw)}",
        "either": f"{sum(run['skill_read'] for run in either)}/{len(either)}",
        "infra_failures": len(runs) - len(valid),
        "failed_or_incomplete": sum(run.get("execution_status") in ("failed", "incomplete") for run in runs),
        "strategy_violations": sum((run.get("strategy_adherence") or {}).get("passed") is False for run in runs),
        "eval_errors": sum(run["eval_errors"] for run in runs),
        "skill_source_mismatch": sum(1 for run in runs if run["skill_source_ok"] is False),
        "answer_outcomes": count_by(valid, "answer_status"),
        "process_outcomes": count_by(runs, "process_status"),
        "process_failed": sum(run.get("execution_status") == "process_failed" for run in runs),
        "controls": control_summary(valid),
        "mean_outer_compatibility_cost": round(sum(costs) / len(costs), 4) if costs else None,
        "per_case": per_case,
    }


def validate_case(case):
    if not isinstance(case.get("id"), str) or not case["id"]:
        raise ValueError("every case needs a non-empty id")
    if case.get("expect") not in ("trigger", "no_trigger", "either"):
        raise ValueError(f"case {case['id']!r}: expect must be trigger, no_trigger or either")
    if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
        raise ValueError(f"case {case['id']!r} needs a goal prompt")
    if not isinstance(case.get("scale", 1), int):
        raise ValueError(f"case {case['id']!r} has a non-integer scale")
    control = case.get("control")
    if control is not None:
        if not isinstance(control, dict) or control.get("kind") not in CONTROL_KINDS:
            raise ValueError(f"case {case['id']!r}: control kind must be one of {', '.join(CONTROL_KINDS)}")
        if control["kind"] == "repo_rename" and not all(isinstance(control.get(name), str) and control[name]
                                                         for name in ("old", "new", "file")):
            raise ValueError(f"case {case['id']!r}: repo_rename control needs old, new and file")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--ref", help="git ref whose complete skills/jevify directory is tested")
    src.add_argument("--skill-dir", help="directory containing the complete jevify/ skill bundle")
    ap.add_argument("--skill-bundle-label", default="actual tested skill bundle",
                    help="label recorded for the tested bundle; file hashes remain the identity evidence")
    ap.add_argument("--cases", default=str(HERE / "cases.json"))
    ap.add_argument("--only", nargs="*", help="case ids to run")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--model", help="model selector (default: your default role)")
    ap.add_argument("--thinking", help="thinking level passed to omp (default: the configured level, usually auto)")
    ap.add_argument("--arm", choices=("skill", "none"), default="skill",
                    help="skill: pin the skill under test; none: make bare and namespaced jevify unavailable")
    ap.add_argument("--strategy", choices=STRATEGIES, default="auto",
                    help="auto leaves the goal unchanged; explicit strategies append only method instructions")
    ap.add_argument("--cache-mode", choices=CACHE_MODES, default="cold",
                    help="cold isolates a fresh DB per attempt; warm clones one frozen SQLite seed per attempt")
    ap.add_argument("--cache-namespace", help="explicit seeded judgment DB for warm mode")
    ap.add_argument("--omp", default=shutil.which("omp"), help="omp executable to run (default: omp on PATH)")
    ap.add_argument("--omp-source", help="source checkout the executable runs; its revision and working-tree "
                                         "changes, untracked files and deletions are hashed")
    ap.add_argument("--max-time", type=int, default=420)
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--retries", type=int, default=1, help="retries for sessions with no tool call or answer")
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", help="directory for raw JSON events and stderr (default: beside --out)")
    ap.add_argument("--authorize-attempts", type=int,
                    help="attempt sessions, including retries, that may start; scheduling stops at this count")
    ap.add_argument("--authorize-questions", type=int,
                    help="stop scheduling once observed known judgment questions reach this count")
    ap.add_argument("--authorize-concurrency", type=int,
                    help="simultaneous attempt cap (the smaller of this and --jobs)")
    ap.add_argument("--preflight", action="store_true",
                    help="print the planned exposure and exit before any session starts")
    ap.add_argument("--thresholds", help="predeclared decision JSON; its canonical digest binds the run")
    ap.add_argument("--compare", help="earlier result file to diff against")
    args = ap.parse_args()
    for name in ("authorize_attempts", "authorize_questions", "authorize_concurrency"):
        if getattr(args, name) is not None and getattr(args, name) < 1:
            ap.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.jobs < 1:
        ap.error("--jobs must be at least 1")
    resolved = shutil.which(args.omp) if args.omp else None
    if resolved is None:
        ap.error(f"omp executable not found: {args.omp!r}")
    args.omp = os.path.abspath(resolved)
    if args.cache_mode != "warm" and args.cache_namespace is not None:
        ap.error("--cache-namespace is only for warm mode; cold mode creates its own empty databases")
    if args.raw_dir is None:
        args.raw_dir = str(Path(args.out).with_suffix("")) + ".raw"
    Path(args.raw_dir).mkdir(parents=True, exist_ok=True)
    try:
        source = source_identity(args.omp, args.omp_source)
    except (RuntimeError, ValueError) as exc:
        ap.error(str(exc))

    cases_bytes = Path(args.cases).read_bytes()
    try:
        payload = json.loads(cases_bytes.decode("utf-8"))
        cases = payload["cases"]
        known = {case["id"] for case in cases}
        if args.only and not set(args.only) <= known:
            raise ValueError(f"--only names cases absent from {args.cases}: {sorted(set(args.only) - known)}")
        if args.only:
            cases = [case for case in cases if case["id"] in set(args.only)]
        for case in cases:
            validate_case(case)
        identities = {case["id"]: fixture_identity(case) for case in cases}
        thresholds, thresholds_sha = (None, None)
        if args.thresholds:
            thresholds, thresholds_sha = load_thresholds(Path(args.thresholds).read_bytes())
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
        ap.error(str(exc))
    selected_ids = [case["id"] for case in cases]
    concurrency = min(args.jobs, args.authorize_concurrency or args.jobs)
    planned = [(case, rep) for case in cases for rep in range(1, args.reps + 1)]
    plan = preflight_plan(args, len(planned), concurrency)
    if plan["refusals"]:
        ap.error("; ".join(plan["refusals"]))
    print("preflight " + json.dumps(plan, sort_keys=True), flush=True)
    if args.preflight:
        return
    args.envelope = Envelope(attempts=args.authorize_attempts, questions=args.authorize_questions)
    with tempfile.TemporaryDirectory(prefix="jevify-eval-") as tmp:
        tmp = Path(tmp)
        try:
            if args.ref:
                source_skills = skill_from_ref(args.ref, tmp / "skills")
            else:
                source_skills = Path(args.skill_dir or ROOT / "skills").resolve()
            launch_skill = skill_identity(source_skills, args.arm)
            if not launch_skill["complete"]:
                raise RuntimeError("skill arm requires SKILL.md and jevify.py")
            cases_snapshot = retain_content(args.raw_dir, f"cases-{digest_bytes(cases_bytes)[:12]}.json", cases_bytes)
            if args.arm == "skill":
                skills_dir, snapshot_sha = retain_skill_snapshot(source_skills, args.raw_dir)
                if snapshot_sha != launch_skill["materialized_sha256"]:
                    raise RuntimeError("skill bundle changed between identity and snapshot")
                skill = {**skill_identity(skills_dir, args.arm), "snapshot_of": str(source_skills / "jevify")}
            else:
                skills_dir, skill = source_skills, launch_skill
        except RuntimeError as exc:
            ap.error(str(exc))
        skill_md = skills_dir / "jevify" / "SKILL.md"
        overlay = tmp / "overlay.yml"
        overlay.write_text(overlay_text(args.arm, skills_dir))
        desc_probe = description(skill_md)[:60] if args.arm == "skill" else None
        work = tmp / "work"
        work.mkdir()
        try:
            args.cache_seed = freeze_cache_seed(args.cache_namespace, work) if args.cache_mode == "warm" else None
        except (RuntimeError, ValueError, OSError) as exc:
            ap.error(str(exc))
        runner = runner_identity()
        contract = comparison_contract(args, cases_sha=digest_bytes(cases_bytes), selected_ids=selected_ids,
                                       identities=identities, source=source, skill=skill, runner=runner,
                                       cache_seed=args.cache_seed, thresholds_sha=thresholds_sha,
                                       concurrency=concurrency)
        runs, not_scheduled = [], []
        # Each finished run is appended here at once, so a killed runner keeps its evidence.
        partial = Path(args.out + ".partial.jsonl")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text("")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        ex = cf.ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = [ex.submit(run_one, case, rep, overlay, desc_probe, args, work) for case, rep in planned]
            for future in cf.as_completed(futures):
                run = future.result()
                if run.get("not_scheduled"):
                    not_scheduled.append({"case": run["case"], "rep": run["rep"], "reason": run["reason"]})
                    print(f"{run['case']:22} r{run['rep']} not scheduled: {run['reason']} authorization exhausted",
                          flush=True)
                    continue
                runs.append(run)
                with partial.open("a") as sink:
                    sink.write(json.dumps(run) + "\n")
                adherence_status = run["strategy_adherence"]["status"]
                print(f"{run['case']:22} r{run['rep']} {run['execution_status']:10} attempts={run['attempts']} "
                      f"skill={run['skill_read']!s:5} judged={run['judged']!s:5} adherence={adherence_status} "
                      f"{run['stop']:9} {run['secs']}s calls={run['calls']} "
                      f"cost={run['accounting']['monetary_status']}", flush=True)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
            for proc in list(LIVE):
                kill_tree(proc)
        runs.sort(key=lambda run: (run["case"], run["rep"]))
        skill_after = skill_identity(source_skills, args.arm)["materialized_sha256"]
        run_manifest = {
            "schema": 1,
            "runner": runner,
            "jobs": args.jobs,
            "effective_concurrency": concurrency,
            "retries": args.retries,
            "reps": args.reps,
            "max_time": args.max_time,
            "max_calls": args.max_calls,
            "cases_snapshot": {"path": str(cases_snapshot), "sha256": digest_bytes(cases_bytes),
                               "bytes": len(cases_bytes), "selected_ids": selected_ids},
            "skill_snapshot": {"arm": args.arm, "launch_sha256": launch_skill["materialized_sha256"],
                               "tested_sha256": skill["materialized_sha256"],
                               "source_sha256_after_runs": skill_after,
                               "source_unchanged_after_runs": skill_after == launch_skill["materialized_sha256"],
                               "directory": skill["directory"]},
            "predeclared_thresholds": None if thresholds is None else {
                "sha256": thresholds_sha, "thresholds": thresholds,
                "path": str(Path(args.thresholds).resolve())},
        }
        result = {
            "schema": RESULT_SCHEMA,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "arm": args.arm,
            "strategy": args.strategy,
            "cache_mode": args.cache_mode,
            "cache_namespace": None if args.cache_namespace is None else str(Path(args.cache_namespace).resolve()),
            "cache_seed": None if args.cache_seed is None else {
                **args.cache_seed, "path": str(args.cache_seed["path"]), "retained": False},
            "skill_source": args.ref or ("skill-dir" if args.skill_dir else "worktree"),
            "skill_bundle_label": args.skill_bundle_label,
            "skill": skill,
            "cases_path": str(Path(args.cases).resolve()),
            "cases_schema": payload.get("schema"),
            "cases_sha256": digest_bytes(cases_bytes),
            "fixture_identities": identities,
            "method_settings": METHOD_SETTINGS,
            "source": source,
            "omp": source["executable"]["version"],
            "omp_path": source["executable"]["path"],
            "model_requested": args.model or "default-role",
            "thinking_requested": args.thinking or "configured",
            "max_time": args.max_time,
            "max_calls": args.max_calls,
            "session_flags": list(SESSION_FLAGS),
            "usage_source": "isolated_persisted_session_model_usage",
            "session_retention": "temporary_until_process_settlement_then_accounting_projection_only",
            "tools": TOOLS.split(","),
            "models_seen": sorted({run["model"] for run in runs if run["model"]}),
            "thinking_seen": sorted({run["thinking"] for run in runs if run["thinking"]}),
            "reps": args.reps,
            "raw_dir": str(Path(args.raw_dir).resolve()),
            "summary": summarize(runs, planned=len(planned), not_scheduled=len(not_scheduled)),
            "cost_ledger": merge_accounting([run["accounting"] for run in runs]),
            "runs": runs,
            "not_scheduled": not_scheduled,
            "envelope": args.envelope.record(),
            "comparison_contract": contract,
            "comparison_contract_sha256": digest_json(contract),
            "run_manifest": run_manifest,
            "grading_cost": {"measured": False, "reported_cost": None,
                             "notes": ["Grade this execution artifact separately; do not copy its cost per arm."]},
        }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    summary = result["summary"]
    print(f"\nauto_trigger {summary['auto_trigger']} · auto_judged {summary['auto_judged']} · "
          f"judged_unverified {summary['auto_judged_unverified']} · "
          f"false_trigger {summary['false_trigger']} · keyword {summary['keyword']} · either {summary['either']} · "
          f"infra {summary['infra_failures']} · failed_or_incomplete {summary['failed_or_incomplete']} · "
          f"strategy_violations {summary['strategy_violations']}")
    print(f"arm {result['arm']} · strategy {result['strategy']} · cache {result['cache_mode']} · "
          f"cost {result['cost_ledger']['monetary_status']} · source {source['status']}")
    print(f"executable {result['omp_path']} version={result['omp'] or 'unknown'}")
    cohorts = summary["cohorts"]
    print(f"cohorts planned={cohorts['planned_runs']} scheduled={cohorts['scheduled_runs']} "
          f"completed={cohorts['completed_runs']} not_scheduled={cohorts['not_scheduled_runs']} · "
          f"envelope exhausted_by={args.envelope.exhausted_by} · question exposure {args.envelope.question_exposure_status()} · dollar envelope none")
    if args.compare:
        base = json.loads(Path(args.compare).read_text())
        diff = contract_differences(base.get("comparison_contract"), contract)
        print(f"contract compatible={diff['compatible']} one_variable={diff['one_variable']} "
              f"controlled={diff['controlled_differences']} treatment={diff['treatment_differences']} "
              f"unrecorded={diff['unrecorded']}")
        if diff["reason"]:
            print(f"note: {diff['reason']}")
        controls = paired_case_comparison(control_means(base.get("runs", [])), control_means(runs))
        interval = controls["interval"]
        print(f"runner-graded controls: n={controls['n']} mean_delta={controls['mean_delta']} "
              f"ci95={None if interval is None else [interval['low'], interval['high']]}")
        base_summary = base["summary"]
        print(f"baseline auto_judged {base_summary.get('auto_judged', '?')} · "
              f"false_trigger {base_summary['false_trigger']} · keyword {base_summary['keyword']} · "
              f"either {base_summary.get('either', '?')}")
        print(f"  {'case':22} pass    judged   adherence")
        for case_id, case in sorted(summary["per_case"].items()):
            print(f"  {case_id:22} {case['pass']}/{case['n']}     "
                  f"{case['judged']}/{case['n'] - case['judged_unverified']} "
                  f"({case['judged_unverified']} unverified)     "
                  f"{case['strategy_adherence_pass']}/{case['strategy_adherence_scored']}")


if __name__ == "__main__":
    main()
