#!/usr/bin/env python3
"""Frozen four-treatment preparation experiments; never changes jevify-pilot/1.

Commands (all paths are explicit; no sealed corpus is read implicitly)::

    python3 evals/preparation.py freeze draft.json --out manifest.json
    python3 evals/preparation.py preflight manifest.json
    python3 evals/preparation.py run manifest.json --out run-dir --injected outcomes.json
    python3 evals/preparation.py run manifest.json --out run-dir --approval approval.json --live
    python3 evals/preparation.py report manifest.json run-dir

Injection is software evidence, not a model measurement. Gold and adjudication never
enter execution requests. Live execution additionally requires an exact manifest/cap
approval and the installed candidate's bound files. Dollar reservations bound admission,
not invoices. Unknown costs are not zero, and aborted reservations are never replayed.
"""

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = "jevify-preparation-pilot/1"
REPORT_SCHEMA = "jevify-preparation-report/1"
OUTCOME_SCHEMA = "jevify-preparation-outcome/1"
APPROVAL_SCHEMA = "jevify-preparation-approval/1"
ARMS = ("A", "B", "C", "D")
STAGES = ("preparation", "extraction", "judgment", "completion")
METRICS = ("occurrence_accuracy", "subject_accuracy", "qualification_retention", "claim_recall",
           "abstention_accuracy", "decision_accuracy", "unsafe_rate", "unresolved_rate",
           "reviewer_overturn_rate", "latency_seconds", "total_cost_usd", "human_minutes")
CHALLENGES = frozenset({"neighboring_claim_ids", "reused_symbols", "wrong_versions", "conditions",
                        "exceptions", "negation", "repeated_quotes", "unicode", "missing_base",
                        "missing_contract", "wrapper_dependencies", "stale_sources", "null_guess",
                        "truncation", "interruption"})
CODE_FILES = ("preparation.py", "run.py", "grade.py", "findings.py", "fixture.py", "refund_retry_fixture.py")
MAX_FILE_BYTES = 16 * 1024 * 1024
AUTHORIZATION_ROOT = Path.home() / ".local" / "state" / "omp-jevify" / "preparation-authorizations"

# Loaded before model execution, identically for all arms. Only host-owned in-memory
# entries are observed; disk journals and tool-supplied metadata are never attestations.
HOST_OBSERVER = r'''export default function (api) {
  const seen = new Set();
  function observe(event, ctx) {
    const usage = [];
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type !== "model_usage" || seen.has(entry.id)) continue;
      seen.add(entry.id);
      const row = {};
      for (const key of ["id", "purpose", "role", "api", "provider", "model", "stopReason", "estimated", "costUnknown"]) {
        if (entry[key] !== undefined) row[key] = entry[key];
      }
      row.usage = {};
      for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]) {
        if (typeof entry.usage?.[key] === "number") row.usage[key] = entry.usage[key];
      }
      row.usage.cost = {};
      for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
        if (typeof entry.usage?.cost?.[key] === "number") row.usage.cost[key] = entry.usage.cost[key];
      }
      usage.push(row);
    }
    process.stdout.write(JSON.stringify({type: "preparation_observation", schema: "jevify-host-observation/1",
      session_id: ctx.sessionManager.getSessionId(), thinking: api.getThinkingLevel() ?? "off", usage,
      final: event.type === "agent_end" || event.type === "session_shutdown"}) + "\n");
  }
  for (const event of ["session_start", "before_agent_start", "tool_execution_end", "agent_end", "session_shutdown"]) {
    api.on(event, observe);
  }
}
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(canonical(value))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(data):
    def bad_constant(value):
        raise ValueError("non-finite JSON number")
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=bad_constant)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(value, keys, where):
    _require(isinstance(value, dict) and set(value) == set(keys), f"{where}: expected exactly {sorted(keys)}")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value, low=0, high=None):
    return type(value) in (float, int) and math.isfinite(value) and value >= low and (high is None or value <= high)


def _money(value):
    _require(isinstance(value, str), "money must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid money") from None
    _require(amount.is_finite() and amount >= 0, "money must be finite and nonnegative")
    return amount


def _timestamp(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value), "UTC timestamp required")
    time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def code_identity():
    return {name: sha((HERE / name).read_bytes()) for name in CODE_FILES}


def _shared():
    spec = importlib.util.spec_from_file_location("jevify_preparation_shared_run", HERE / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file(root, ref):
    _object(ref, ("path", "sha256"), "file reference")
    relative = ref["path"]
    _require(_text(relative) and not Path(relative).is_absolute() and ".." not in Path(relative).parts,
             "bound path must be relative and contained")
    target = (root / relative).resolve()
    _require(root in target.parents and target.is_file(), "bound file escaped root or is absent")
    _require(target.stat().st_size <= MAX_FILE_BYTES, "bound file exceeds byte limit")
    data = target.read_bytes()
    _require(sha(data) == ref["sha256"], f"bound bytes changed: {relative}")
    return data


def file_ref(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return {"path": str(path.relative_to(root)), "sha256": sha(path.read_bytes())}


def _candidate(value):
    _object(value, ("root", "entry", "files"), "candidate")
    root = Path(value["root"]).resolve()
    _require(Path(value["root"]).is_absolute() and root.is_dir(), "candidate needs an explicit installed directory")
    _require(isinstance(value["files"], dict) and value["entry"] in value["files"], "candidate entry must be bound")
    required = {value["entry"]}
    if (root / "package.json").is_file():
        required.add("package.json")
    for directory in (root / "src", root / "skills" / "jevify"):
        if directory.is_dir():
            required.update(str(path.relative_to(root)) for path in directory.rglob("*")
                            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    _require(required <= set(value["files"]), "candidate dependency or skill/prompt file is not bound")
    for path, checksum in value["files"].items():
        _file(root, {"path": path, "sha256": checksum})
    return {"path": str(root / value["entry"]), "sha256": value["files"][value["entry"]],
            "bundle_sha256": digest(value["files"])}


def _thresholds(t):
    _object(t, ("primary", "min_cases", "quality_noninferiority", "unsafe_max", "unresolved_max",
                "baseline_quality_min", "confidence", "interval", "tie", "human_time_protocol", "secondary_direct"), "thresholds")
    _object(t["primary"], ("metric", "direction", "min_improvement"), "primary")
    p = t["primary"]
    _require(p["metric"] in METRICS and p["direction"] in ("higher", "lower") and _number(p["min_improvement"])
             and p["min_improvement"] > 0, "primary metric/direction/useful improvement required")
    _require(type(t["min_cases"]) is int and t["min_cases"] >= 2, "at least two independent cases required")
    _require(t["confidence"] == .95 and t["interval"] == "paired_case_mean_t_interval", "unsupported interval policy")
    _require(t["tie"] in ("keep_deterministic", "inconclusive") and type(t["secondary_direct"]) is bool, "invalid decision policy")
    for key in ("quality_noninferiority", "unsafe_max", "unresolved_max", "baseline_quality_min"):
        _require(_number(t[key], 0, 1), f"invalid threshold {key}")
    protocol = t["human_time_protocol"]
    if protocol is not None:
        _object(protocol, ("design", "protocol_sha256", "protocol_file", "reviewers"), "human time protocol")
        _require(protocol["design"] in ("blinded", "counterbalanced") and _text(protocol["protocol_sha256"])
                 and isinstance(protocol["reviewers"], list) and bool(protocol["reviewers"]), "human labor protocol required")
    _require(p["metric"] != "human_minutes" or protocol is not None, "labor benefit requires frozen human protocol")


def _inventory(value):
    _require(isinstance(value, list) and bool(value), "nonempty record inventory required")
    seen = set()
    for record in value:
        _object(record, ("id", "subject", "revision", "required_questions", "source_id", "dependencies", "passages"), "inventory record")
        _require(all(_text(record[key]) for key in ("id", "subject", "revision")) and record["id"] not in seen,
                 "record identity missing or duplicated")
        seen.add(record["id"])
        _require(_text(record["source_id"]) and isinstance(record["dependencies"], list)
                 and all(_text(sid) for sid in record["dependencies"])
                 and len(set(record["dependencies"])) == len(record["dependencies"])
                 and record["source_id"] in record["dependencies"], "record original/dependency binding required")
        _object(record["passages"], ARMS, "record passage bindings")
        _require(record["passages"]["B"] == record["passages"]["C"], "B/C record passage bindings differ")
        for indices in record["passages"].values():
            _require(isinstance(indices, list) and all(type(index) is int and index >= 0 for index in indices)
                     and len(set(indices)) == len(indices), "record passage indices must be unique nonnegative integers")
        q = record["required_questions"]
        _require(isinstance(q, list) and bool(q) and all(_text(x) for x in q) and len(set(q)) == len(q),
                 "required-question denominator must be nonempty and unique")
    return value


def _citation(citation, sources):
    if not isinstance(citation, dict) or set(citation) != {"source_id", "revision", "start", "end", "quote"}:
        return False
    source = sources.get(citation["source_id"])
    if source is None or citation["revision"] != source["revision"]:
        return False
    text = source["text"]
    start, end = citation["start"], citation["end"]
    return type(start) is int and type(end) is int and 0 <= start < end <= len(text) \
        and citation["quote"] == text[start:end]


def _gold(value, inventory, sources):
    _object(value, ("schema", "claims"), "gold")
    _require(value["schema"] == "jevify-preparation-gold/1" and isinstance(value["claims"], dict), "invalid gold schema")
    public = {r["id"]: r for r in inventory}
    _require(set(public) <= set(value["claims"]), "gold must cover every public record")
    for cid, claim in value["claims"].items():
        _object(claim, ("subject", "revision", "qualifications", "questions", "occurrences"), "gold claim")
        _require(_text(cid) and _text(claim["subject"]) and _text(claim["revision"]), "invalid gold identity")
        _require(isinstance(claim["questions"], dict) and bool(claim["questions"]), "gold questions required")
        if cid in public:
            _require(set(public[cid]["required_questions"]) == set(claim["questions"])
                     and public[cid]["subject"] == claim["subject"] and public[cid]["revision"] == claim["revision"],
                     "gold must match public identity and required questions")
        _require(isinstance(claim["qualifications"], list) and all(_text(x) for x in claim["qualifications"]), "invalid qualifications")
        _require(isinstance(claim["occurrences"], list) and all(_citation(x, sources) for x in claim["occurrences"]),
                 "gold occurrence is not source-bound")
        for q, answer in claim["questions"].items():
            _object(answer, ("value", "unsafe_values"), "gold answer")
            _require(_text(q) and isinstance(answer["unsafe_values"], list), "invalid gold answer")
    return value


def load_manifest(path, *, started_at=None):
    """Read every frozen byte; no provider or credential access. Returns a private grading bundle."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    _require(len(raw) <= MAX_FILE_BYTES, "manifest too large")
    m = decode(raw)
    _object(m, ("schema", "pilot_id", "frozen_at", "phase", "implementation", "candidate", "host", "controls",
                "treatments", "cases", "thresholds", "design", "budget"), "manifest")
    _require(m["schema"] == SCHEMA and _text(m["pilot_id"]), "invalid preparation manifest")
    _require(m["phase"] in ("development", "heldout"), "invalid phase")
    _timestamp(m["frozen_at"])
    if started_at is not None:
        _require(m["frozen_at"] < _timestamp(started_at), "manifest must be frozen before execution")
    _require(m["implementation"] == code_identity(), "runner/grader implementation changed")
    candidate = _candidate(m["candidate"])
    host = m["host"]
    _object(host, ("path", "sha256", "model", "thinking", "max_seconds", "max_calls"), "host")
    _require(Path(host["path"]).is_absolute() and sha(Path(host["path"]).read_bytes()) == host["sha256"], "host identity changed")
    _require(_text(host["model"]) and _text(host["thinking"]) and type(host["max_calls"]) is int and host["max_calls"] > 0
             and _number(host["max_seconds"]) and host["max_seconds"] > 0, "explicit bounded host configuration required")
    root = path.parent
    controls = decode(_file(root, m["controls"]))
    _object(controls, ("rubric", "fields", "judge", "fallback", "completion"), "controls")
    _require(_text(controls["rubric"]) and isinstance(controls["fields"], dict) and bool(controls["fields"])
             and isinstance(controls["judge"], dict) and bool(controls["judge"])
             and _text(controls["fallback"]) and _text(controls["completion"]), "unbound rubric/judge/fallback/completion")
    judge = controls["judge"]
    _object(judge, ("model", "thinking", "fallback_models", "rubric_revision"), "judge configuration")
    _require(_text(judge["model"]) and judge["thinking"] in ("off", "minimal", "low", "medium", "high", "xhigh", "max")
             and _text(judge["rubric_revision"]) and isinstance(judge["fallback_models"], list)
             and all(_text(model) for model in judge["fallback_models"])
             and len(set(judge["fallback_models"])) == len(judge["fallback_models"]), "explicit judge role and fallback policy required")
    _require(judge["thinking"] == "off", "supported host judgment disables reasoning; judge thinking must be off")
    for field in controls["fields"].values():
        _object(field, ("instructions", "null_threshold"), "field")
        _require(_text(field["instructions"]) and field["null_threshold"] == .5,
                 "field instructions and adapter's frozen 0.5 null threshold required")
    _object(m["treatments"], ARMS, "treatments")
    for arm, treatment in m["treatments"].items():
        _object(treatment, ("strategy", "preparation", "extraction", "controls_sha256"), "treatment")
        _require(_text(treatment["strategy"]) and type(treatment["extraction"]) is bool
                 and treatment["controls_sha256"] == m["controls"]["sha256"], "treatment controls are unbound")
    b, c = m["treatments"]["B"], m["treatments"]["C"]
    _require({k: v for k, v in b.items() if k != "extraction"} == {k: v for k, v in c.items() if k != "extraction"},
             "B/C must differ only by extraction")
    _require([m["treatments"][a]["preparation"] for a in ARMS] == ["current", "deterministic", "deterministic", "direct"],
             "four prescribed preparation arms required")
    _require([m["treatments"][a]["extraction"] for a in ARMS] == [False, False, True, False], "only C admits extraction")
    _thresholds(m["thresholds"])
    protocol = m["thresholds"]["human_time_protocol"]
    if protocol is not None:
        _require(sha(_file(root, protocol["protocol_file"])) == protocol["protocol_sha256"], "human time protocol bytes changed")
    d = m["design"]
    _object(d, ("repetitions", "seed", "order", "max_retries"), "design")
    _require(type(d["repetitions"]) is int and d["repetitions"] >= 2 and type(d["seed"]) is int
             and d["order"] == "seeded_shuffled_blocks" and type(d["max_retries"]) is int and d["max_retries"] == 0,
             "frozen repeated randomized design required")
    budget = m["budget"]
    _object(budget, ("max_attempts", "max_allocation_usd", "reservation_per_attempt_usd", "policy", "uncertain_charge", "stop_on_incomplete"), "budget")
    _require(type(budget["max_attempts"]) is int and budget["max_attempts"] > 0
             and _money(budget["max_allocation_usd"]) > 0 and _money(budget["reservation_per_attempt_usd"]) > 0
             and budget["policy"] == "best_effort_reservation" and budget["uncertain_charge"] == "full_reservation"
             and budget["stop_on_incomplete"] is True, "conservative best-effort budget required")
    _require(isinstance(m["cases"], list) and bool(m["cases"]), "cases required")
    cases, ids = [], set()
    for case in m["cases"]:
        _object(case, ("id", "unit_id", "kind", "provenance", "split", "exposure_history", "direct_suitable", "challenges",
                       "sources", "inventory", "retrieval", "gold", "adjudication"), "case")
        cid = case["id"]
        _require(_text(cid) and cid not in ids and _text(case["unit_id"]), "unique case and independent unit identities required")
        ids.add(cid)
        _require(case["kind"] in ("synthetic", "realistic") and _text(case["provenance"]), "case provenance required")
        _require(case["split"] == m["phase"] and isinstance(case["exposure_history"], list), "exposure history required")
        for event in case["exposure_history"]:
            _object(event, ("at", "kind", "actor"), "exposure")
            _timestamp(event["at"])
            _require(event["kind"] in ("authored", "adjudicated", "tuned", "evaluated", "published") and _text(event["actor"]), "invalid exposure")
        _require(case["exposure_history"], "empty history cannot establish unexposed evidence")
        _require(type(case["direct_suitable"]) is bool and isinstance(case["challenges"], list)
                 and set(case["challenges"]) <= CHALLENGES, "invalid case applicability/challenges")
        _require(isinstance(case["sources"], dict) and bool(case["sources"]), "source inventory required")
        sources = {}
        for sid, source in case["sources"].items():
            _object(source, ("revision", "role", "file", "subject", "version"), "source")
            _require(_text(sid) and _text(source["revision"]) and _text(source["role"])
                     and all(source[key] is None or _text(source[key]) for key in ("subject", "version")), "source identity required")
            sources[sid] = {**source, "text": _file(root, source["file"]).decode("utf-8")}
        inventory = _inventory(decode(_file(root, case["inventory"])))
        _object(case["retrieval"], ARMS, "retrieval")
        _require(case["retrieval"]["B"]["sha256"] == case["retrieval"]["C"]["sha256"], "B/C retrieved passages differ")
        retrieval = {}
        for arm, ref in case["retrieval"].items():
            passages = decode(_file(root, ref))
            _require(isinstance(passages, list) and all(_citation(p, sources) for p in passages), "retrieval must be exact source passages")
            retrieval[arm] = passages
        for record in inventory:
            _require(set(record["dependencies"]) <= set(sources), "record names an undeclared dependency")
            original = sources[record["source_id"]]
            _require(original["subject"] in (None, record["subject"]) and original["version"] in (None, record["revision"]),
                     "record original source subject/version mismatch")
            for arm, indices in record["passages"].items():
                _require(all(index < len(retrieval[arm]) and retrieval[arm][index]["source_id"] in record["dependencies"]
                             for index in indices), "record passage is outside its declared dependency inventory")
                if arm in ("B", "C"):
                    _require(any(retrieval[arm][index]["source_id"] == record["source_id"] for index in indices),
                             "record requires its own retrieved original passage")
        gold_raw = _file(root, case["gold"])
        gold = _gold(decode(gold_raw), inventory, sources)
        adjudication = case["adjudication"]
        _object(adjudication, ("key_author", "adjudicator", "kind", "at", "key_sha256", "source_inventory_sha256", "evidence"), "adjudication")
        _require(_text(adjudication["key_author"]) and _text(adjudication["adjudicator"])
                 and (adjudication["kind"] in ("human", "organization")
                      or (case["kind"] == "synthetic" and adjudication["kind"] == "fixture")),
                 "independent source adjudication required; no model self-grading")
        _require(adjudication["adjudicator"].strip().casefold() not in
                 {adjudication["key_author"].strip().casefold(), "jevify-repository"}, "gold adjudicator is the key author or repository")
        _require(adjudication["key_sha256"] == case["gold"]["sha256"], "adjudication binds a different gold key")
        _require(_timestamp(adjudication["at"]) <= m["frozen_at"]
                 and adjudication["source_inventory_sha256"] == digest(case["sources"]), "adjudication is not frozen/source-bound")
        evidence = _file(root, adjudication["evidence"])
        _require(bool(evidence.strip()) and sha(evidence) not in {sha(gold_raw), case["inventory"]["sha256"], m["controls"]["sha256"],
                                                               *[s["file"]["sha256"] for s in sources.values()], *m["implementation"].values()},
                 "adjudication evidence must be separate from key/source/implementation")
        cases.append({"manifest": case, "sources": sources, "inventory": inventory, "retrieval": retrieval, "gold": gold})
    return {"manifest": m, "manifest_sha256": digest(m), "raw_sha256": sha(raw), "path": path,
            "controls": controls, "candidate": candidate, "cases": cases}


def independent_units(bundle):
    """Conservative connected components: changing an alias ID cannot increase n."""
    groups = []
    for c in bundle["cases"]:
        case = c["manifest"]
        semantic_claims = []
        for claim in c["gold"]["claims"].values():
            # Unrelated source padding must not turn the same adjudicated key into
            # another independent case. Source integrity is checked at admission.
            occurrences = [{k: v for k, v in occurrence.items() if k != "source_id"}
                           for occurrence in claim["occurrences"]]
            semantic_claims.append({**claim, "occurrences": sorted(occurrences, key=canonical),
                                    "qualifications": sorted(set(claim["qualifications"]))})
        signatures = {("unit", case["unit_id"]), ("key", digest(sorted(semantic_claims, key=canonical))),
                      ("sources", digest(sorted({s["file"]["sha256"] for s in case["sources"].values()})))}
        matching = [g for g in groups if signatures & g["signatures"]]
        merged = {"ids": [case["id"]], "signatures": set(signatures)}
        for group in matching:
            groups.remove(group)
            merged["ids"].extend(group["ids"])
            merged["signatures"].update(group["signatures"])
        groups.append(merged)
    return [sorted(g["ids"]) for g in groups]


def schedule(bundle):
    rng = random.Random(bundle["manifest"]["design"]["seed"])
    rows = []
    for rep in range(bundle["manifest"]["design"]["repetitions"]):
        block = [(case["manifest"]["id"], arm, rep) for case in bundle["cases"] for arm in ARMS
                 if arm != "D" or case["manifest"]["direct_suitable"]]
        rng.shuffle(block)
        rows.extend(block)
    return rows


def preflight(path):
    bundle = load_manifest(path)
    units = independent_units(bundle)
    return {"schema": "jevify-preparation-preflight/1", "manifest_sha256": bundle["manifest_sha256"],
            "implementation": bundle["manifest"]["implementation"], "candidate": bundle["candidate"],
            "independent_cases": len(units), "aliases": [u for u in units if len(u) > 1],
            "required_questions": {c["manifest"]["id"]: {r["id"]: len(r["required_questions"]) for r in c["inventory"]}
                                   for c in bundle["cases"]},
            "gold_questions": {c["manifest"]["id"]: sum(len(r["questions"]) for r in c["gold"]["claims"].values()) for c in bundle["cases"]},
            "planned_attempts_without_retries": len(schedule(bundle)), "transmissions": 0,
            "recommendation": "inconclusive", "reason": "preflight observes no outcomes",
            "paid_admission": "eligible_pending_exact_approval" if live_evidence_ready(bundle) else "withheld_no_independent_fresh_realistic_gold",
            "attestation": "operator_attested_unauthenticated", "hard_invoice_cap": False,
            "provenance_trust": "trusted_host_process_no_same_uid_isolation"}


def live_evidence_ready(bundle):
    """Synthetic fixture attestations never authorize a paid experiment."""
    units = independent_units(bundle)
    return len(units) >= bundle["manifest"]["thresholds"]["min_cases"] and all(len(unit) == 1 for unit in units) \
        and bundle["manifest"]["phase"] == "heldout" and all(
        c["manifest"]["kind"] == "realistic" and c["manifest"]["adjudication"]["kind"] in ("human", "organization")
        and not any(e["kind"] in ("tuned", "evaluated", "published") for e in c["manifest"]["exposure_history"])
        for c in bundle["cases"])


def approve(bundle, approval):
    _object(approval, ("schema", "manifest_sha256", "max_attempts", "max_allocation_usd", "owner", "approved_at", "accept_best_effort"), "approval")
    budget = bundle["manifest"]["budget"]
    _require(live_evidence_ready(bundle), "paid execution requires independently adjudicated fresh realistic gold")
    _require(approval["schema"] == APPROVAL_SCHEMA and approval["manifest_sha256"] == bundle["manifest_sha256"], "approval manifest mismatch")
    _require(approval["max_attempts"] == budget["max_attempts"] and approval["max_allocation_usd"] == budget["max_allocation_usd"], "approval cap mismatch")
    _require(_text(approval["owner"]) and approval["accept_best_effort"] is True
             and bundle["manifest"]["frozen_at"] <= _timestamp(approval["approved_at"]) <= now(), "owner must accept frozen best-effort policy")
    return digest(approval)


@contextmanager
def authorization_claim(bundle, approval_sha, output, resume):
    """One authorization owns one ledger, independently of caller-selected output paths."""
    AUTHORIZATION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    claim_path = AUTHORIZATION_ROOT / f"{approval_sha}.json"
    lock_path = AUTHORIZATION_ROOT / f"{approval_sha}.lock"
    claim = {"schema": "jevify-preparation-authorization-claim/1", "approval_sha256": approval_sha,
             "manifest_sha256": bundle["manifest_sha256"], "output": str(Path(output).resolve())}
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("authorization already has an active experiment") from error
        try:
            if resume:
                _require(claim_path.is_file() and decode(claim_path.read_bytes()) == claim,
                         "authorization belongs to another or missing experiment ledger")
            else:
                try:
                    _exclusive(claim_path, canonical(claim))
                except FileExistsError as error:
                    raise ValueError("authorization is already claimed; resume its ledger or obtain new approval") from error
                directory_fd = os.open(AUTHORIZATION_ROOT, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def request_for(bundle, cid, arm, rep, attempt):
    """Public-only boundary: no gold, adjudicator files, manifest paths, or expected labels."""
    c = next(c for c in bundle["cases"] if c["manifest"]["id"] == cid)
    identity = {"manifest_sha256": bundle["manifest_sha256"], "case": cid, "arm": arm, "rep": rep, "attempt": attempt,
                "source_inventory_sha256": digest(c["manifest"]["sources"]),
                "inventory_sha256": c["manifest"]["inventory"]["sha256"],
                "retrieval_sha256": c["manifest"]["retrieval"][arm]["sha256"],
                "treatment_sha256": digest(bundle["manifest"]["treatments"][arm]),
                "controls_sha256": bundle["manifest"]["controls"]["sha256"]}
    return {"schema": "jevify-preparation-request/1", "identity": identity, "inventory": copy.deepcopy(c["inventory"]),
            "sources": copy.deepcopy(c["sources"]), "passages": copy.deepcopy(c["retrieval"][arm]),
            "controls": copy.deepcopy(bundle["controls"]), "treatment": copy.deepcopy(bundle["manifest"]["treatments"][arm]),
            "requested": {"model": bundle["manifest"]["host"]["model"], "thinking": bundle["manifest"]["host"]["thinking"],
                          "judge": copy.deepcopy(bundle["controls"]["judge"])},
            "candidate": copy.deepcopy(bundle["candidate"]) if arm == "C" else None}


def account(receipts):
    """Complete cost coverage means provider-reported usage, never an inferred invoice.

    Catalog estimates and uncorroborated zero usage remain incomplete. Passes,
    tokens and attempts are separate denominators; billing reconciliation is absent.
    """
    totals = {stage: {"reported_subtotal_usd": Decimal(0), "catalog_estimate_subtotal_usd": Decimal(0), "cost_unknown": False, "input_tokens": 0,
                      "tokens_unknown": False, "provider_passes": 0, "passes_unknown": False, "attempts": 0} for stage in STAGES}
    ids = set()
    _require(isinstance(receipts, list), "receipt list required")
    for r in receipts:
        _object(r, ("id", "stage", "status", "cost_usd", "cost_evidence", "input_tokens", "provider_passes"), "usage receipt")
        _require(_text(r["id"]) and r["id"] not in ids and r["stage"] in STAGES, "duplicate receipt or unknown stage")
        ids.add(r["id"])
        _require(r["status"] in ("complete", "failed", "aborted", "not_used") and r["cost_evidence"] in
                 ("provider_reported", "corroborated_zero", "catalog_estimate", "unknown", "not_used"), "invalid usage provenance")
        bucket = totals[r["stage"]]
        unused = r["status"] == "not_used"
        if unused:
            _require(r["cost_evidence"] == "not_used" and r["cost_usd"] == "0" and r["input_tokens"] == 0 and r["provider_passes"] == 0,
                     "not-used receipt cannot carry inference usage")
        else:
            bucket["attempts"] += 1
        cost = _money(r["cost_usd"]) if r["cost_usd"] is not None else None
        known = unused or (cost is not None and ((cost > 0 and r["cost_evidence"] == "provider_reported")
                                                 or (cost == 0 and r["cost_evidence"] == "corroborated_zero")))
        if known:
            bucket["reported_subtotal_usd"] += cost
        else:
            bucket["cost_unknown"] = True
        if cost is not None and r["cost_evidence"] == "catalog_estimate":
            bucket["catalog_estimate_subtotal_usd"] += cost
        for field, unknown in (("input_tokens", "tokens_unknown"), ("provider_passes", "passes_unknown")):
            v = r[field]
            _require(v is None or (type(v) is int and v >= 0), "usage counts must be nonnegative integers or null")
            if v is None:
                bucket[unknown] = True
            else:
                bucket[field] += v
    for stage, bucket in totals.items():
        if not any(r["stage"] == stage for r in receipts):
            bucket["cost_unknown"] = bucket["tokens_unknown"] = bucket["passes_unknown"] = True
        bucket["reported_subtotal_usd"] = str(bucket["reported_subtotal_usd"])
        bucket["catalog_estimate_subtotal_usd"] = str(bucket["catalog_estimate_subtotal_usd"])
    known = all(not b["cost_unknown"] for b in totals.values())
    subtotal = sum((_money(b["reported_subtotal_usd"]) for b in totals.values()), Decimal(0))
    return {"stages": totals, "reported_subtotal_usd": str(subtotal),
            "catalog_estimate_subtotal_usd": str(sum((_money(b["catalog_estimate_subtotal_usd"]) for b in totals.values()), Decimal(0))),
            "total_cost_usd": str(subtotal) if known else None,
            "cost_complete": known, "accounting_basis": "provider_reported_not_invoice", "billing_complete": False}


def validate_outcome(outcome, request):
    _object(outcome, ("schema", "identity", "observed", "records", "processed_passages", "receipts", "human_review"), "outcome")
    _require(outcome["schema"] == OUTCOME_SCHEMA and outcome["identity"] == request["identity"], "outcome identity mismatch")
    observed = outcome["observed"]
    _object(observed, ("model", "thinking", "judge", "candidate", "extraction_invoked", "extraction"), "observed provenance")
    _require(type(observed["extraction_invoked"]) is bool and isinstance(outcome["records"], list), "invalid observed outcome")
    _require(isinstance(outcome["processed_passages"], list) and all(type(x) is int and 0 <= x < len(request["passages"])
             for x in outcome["processed_passages"]) and len(set(outcome["processed_passages"])) == len(outcome["processed_passages"]), "invalid processed coverage")
    ids = set()
    for r in outcome["records"]:
        _object(r, ("id", "subject", "revision", "qualifications", "answers", "occurrences"), "outcome record")
        _require(_text(r["id"]) and r["id"] not in ids and isinstance(r["answers"], dict)
                 and isinstance(r["qualifications"], list) and isinstance(r["occurrences"], list), "invalid or duplicate outcome record")
        ids.add(r["id"])
    accounting = account(outcome["receipts"])
    public = {r["id"]: r for r in request["inventory"]}
    rows = {r["id"]: r for r in outcome["records"]}
    required = all(cid in rows and set(record["required_questions"]) <= set(rows[cid]["answers"]) for cid, record in public.items())
    judge = observed["judge"]
    expected_judge = request["requested"]["judge"]
    judge_known = isinstance(judge, dict) and set(judge) == {"configuration", "models"} \
        and judge["configuration"] == expected_judge and isinstance(judge["models"], list) and bool(judge["models"]) \
        and all(model in [expected_judge["model"], *expected_judge["fallback_models"]] for model in judge["models"])
    if request["identity"]["arm"] == "D":
        judge_known = judge == {"mode": "direct_no_judge"}
    provenance = _text(observed["model"]) and _text(observed["thinking"]) and judge_known
    if request["identity"]["arm"] == "C":
        extraction = observed["extraction"]
        extraction_known = isinstance(extraction, dict) and set(extraction) == {"requested_model", "observed_models", "observed_providers"} \
            and extraction["requested_model"] == "cloudflare/clairic" \
            and isinstance(extraction["observed_models"], list) and bool(extraction["observed_models"]) \
            and all(_text(x) for x in extraction["observed_models"]) \
            and isinstance(extraction["observed_providers"], list) and bool(extraction["observed_providers"]) \
            and all(_text(x) for x in extraction["observed_providers"])
        provenance = provenance and extraction_known and observed["candidate"] == request["candidate"] and observed["extraction_invoked"] \
            and any(r["stage"] == "extraction" and r["status"] != "not_used" for r in outcome["receipts"])
    else:
        provenance = provenance and observed["extraction"] is None and observed["candidate"] is None and not observed["extraction_invoked"] \
            and all(r["status"] == "not_used" for r in outcome["receipts"] if r["stage"] == "extraction")
    return {"required_complete": required, "provenance_complete": bool(provenance), "accounting": accounting}


def score(case, outcome, latency, accounting, protocol):
    """Source occurrence validity and semantic correctness are separate frozen denominators."""
    claims = case["gold"]["claims"]
    rows = {r["id"]: r for r in outcome["records"]}
    denominators = {"questions": sum(len(c["questions"]) for c in claims.values()), "claims": len(claims),
                    "null_questions": sum(a["value"] is None for c in claims.values() for a in c["questions"].values()),
                    "qualifications": sum(len(c["qualifications"]) for c in claims.values()),
                    "occurrences": sum(len(r["occurrences"]) for r in rows.values())}
    correct = unsafe = unresolved = abstained = subjects = retained = recalled = valid_occurrences = 0
    for row in rows.values():
        valid_occurrences += sum(_citation(c, case["sources"]) for c in row["occurrences"])
    for cid, gold in claims.items():
        row = rows.get(cid)
        identity_ok = row is not None and row["subject"] == gold["subject"] and row["revision"] == gold["revision"]
        subjects += identity_ok
        # A quote from a neighboring claim is not semantic recall: a gold-adjudicated occurrence must match.
        occurrence_ok = identity_ok and (not gold["occurrences"] or any(c in gold["occurrences"] for c in row["occurrences"]))
        recalled += occurrence_ok
        retained += sum(q in row["qualifications"] for q in gold["qualifications"]) if identity_ok else 0
        qualifications_ok = identity_ok and all(q in row["qualifications"] for q in gold["qualifications"])
        for q, answer in gold["questions"].items():
            present = row is not None and q in row["answers"]
            value = row["answers"].get(q) if present else None
            unresolved += not present or not occurrence_ok or (value is None and answer["value"] is not None)
            correct += bool(present and occurrence_ok and qualifications_ok and value == answer["value"])
            abstained += bool(present and identity_ok and value is None and answer["value"] is None)
            unsafe += bool(present and value in answer["unsafe_values"])
    # Unknown ids/subjects never earn semantic credit; actionable unknowns count as unsafe.
    unsafe += sum(any(value is not None for value in row["answers"].values()) for cid, row in rows.items() if cid not in claims)
    qn = denominators["questions"]
    human = outcome["human_review"]
    human_valid = isinstance(human, dict) and protocol is not None and set(human) == {"protocol_sha256", "reviewer", "preparation_minutes", "review_minutes", "overturned", "reviewed"} \
        and human["protocol_sha256"] == protocol["protocol_sha256"] and human["reviewer"] in protocol["reviewers"] \
        and _number(human["preparation_minutes"]) and _number(human["review_minutes"]) \
        and type(human["reviewed"]) is int and type(human["overturned"]) is int and 0 <= human["overturned"] <= human["reviewed"]
    return {"metrics": {"occurrence_accuracy": valid_occurrences / denominators["occurrences"] if denominators["occurrences"] else None,
                        "subject_accuracy": subjects / len(claims),
                        "qualification_retention": retained / denominators["qualifications"] if denominators["qualifications"] else None,
                        "claim_recall": recalled / len(claims),
                        "abstention_accuracy": abstained / denominators["null_questions"] if denominators["null_questions"] else None,
                        "decision_accuracy": correct / qn, "unsafe_rate": unsafe / qn, "unresolved_rate": unresolved / qn,
                        "reviewer_overturn_rate": human["overturned"] / human["reviewed"] if human_valid and human["reviewed"] else None,
                        "human_minutes": human["preparation_minutes"] + human["review_minutes"] if human_valid else None,
                        "latency_seconds": latency, "total_cost_usd": float(accounting["total_cost_usd"]) if accounting["cost_complete"] else None},
            "denominators": denominators, "processed_section_coverage": len(outcome["processed_passages"]) / len(case["retrieval"][outcome["identity"]["arm"]])
            if case["retrieval"][outcome["identity"]["arm"]] else None}


def _exclusive(path, data):
    with Path(path).open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _journal(path, payload, previous):
    record = {"previous": previous, "payload": payload}
    record["sha256"] = digest(record)
    with Path(path).open("ab") as out:
        out.write(canonical(record) + b"\n")
        out.flush()
        os.fsync(out.fileno())
    return record["sha256"]


def read_ledger(path):
    records, previous = [], None
    for line in Path(path).read_bytes().splitlines():
        record = decode(line)
        _object(record, ("previous", "payload", "sha256"), "journal entry")
        _require(record["previous"] == previous and record["sha256"] == digest({k: v for k, v in record.items() if k != "sha256"}), "ledger chain changed")
        records.append(record["payload"])
        previous = record["sha256"]
    return records, previous


class InjectedExecutor:
    """Fixture outcomes keyed case:arm:rep; identity is still supplied and checked by the fixture."""
    def __init__(self, path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self.identity = {"kind": "injected", "sha256": sha(self.raw)}
        self.outcomes = decode(self.raw)

    def __call__(self, request, directory):
        i = request["identity"]
        return copy.deepcopy(self.outcomes[f'{i["case"]}:{i["arm"]}:{i["rep"]}:{i["attempt"]}'])


def artifact_hashes(directory):
    result = {}
    for path in sorted(Path(directory).iterdir()):
        _require(path.is_file() and not path.is_symlink(), "attempt artifact is not a regular file")
        _require(path.stat().st_size <= MAX_FILE_BYTES, "attempt artifact exceeds byte limit")
        result[path.name] = sha(path.read_bytes())
    return result


def provider_decimal(value):
    if type(value) not in (str, int, float):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def provider_count(value):
    amount = provider_decimal(value)
    return int(amount) if amount is not None and amount == amount.to_integral_value() else None


def failed_accounting(directory, outcome):
    """Retain observed spend on invalid/aborted final answers, plus an unknown remainder."""
    captured = Path(directory) / "captured-receipts.json"
    receipts = []
    try:
        receipts = decode(captured.read_bytes()) if captured.is_file() else outcome.get("receipts", []) if isinstance(outcome, dict) else []
        account(receipts)
    except (ValueError, TypeError, KeyError, OSError):
        receipts = []
    receipts = [*receipts, {"id": "runner:uncertain_failure", "stage": "completion", "status": "aborted",
                           "cost_usd": None, "cost_evidence": "unknown", "input_tokens": None, "provider_passes": None}]
    _exclusive(Path(directory) / "failed-receipts.json", canonical(receipts))
    return account(receipts)


def run_experiment(path, output, *, executor=None, approval=None, live=False, resume=False):
    """Serial, durable admission: reserve before invocation, revalidate bound bytes before every attempt."""
    started = now()
    bundle = load_manifest(path, started_at=started)
    m, output = bundle["manifest"], Path(output).resolve()
    if live:
        _require(executor is None, "live runs must use the isolated installed host executor")
        _require(approval is not None, "paid execution needs exact manifest approval")
        approval_sha = approve(bundle, approval)
        executor = HostExecutor(bundle)
    else:
        _require(executor is not None and callable(executor), "provider-free execution needs an injected executor")
        approval_sha = None
    with authorization_claim(bundle, approval_sha, output, resume) if live else nullcontext():
        return _run_admitted_experiment(path, output, bundle, executor, approval_sha, live, resume, started)


def _run_admitted_experiment(path, output, bundle, executor, approval_sha, live, resume, started):
    m = bundle["manifest"]
    execution_identity = getattr(executor, "identity", {"kind": "injected_callable", "sha256": None})
    header = {"event": "start", "manifest_sha256": bundle["manifest_sha256"], "raw_sha256": bundle["raw_sha256"],
              "started_at": started, "approval_sha256": approval_sha, "execution": execution_identity,
              "evidence_class": "model_measurement" if live else "injected_software_evidence"}
    ledger = output / "attempts.jsonl"
    if resume:
        _require(not (output / "report.json").exists(), "completed experiments are immutable")
        records, previous = read_ledger(ledger)
        _require(records and {k: v for k, v in records[0].items() if k != "started_at"}
                 == {k: v for k, v in header.items() if k != "started_at"}, "resume identity/approval mismatch")
        report(path, output)  # verify all prior bound artifacts before any new admission
    else:
        output.mkdir(parents=True, exist_ok=False)
        _exclusive(output / "manifest.json", canonical(m))
        _exclusive(ledger, b"")
        previous = _journal(ledger, header, None)
        records = [header]
    reservations = [r for r in records if r["event"] == "reserve"]
    finishes = {r["attempt_id"]: r for r in records if r["event"] == "finish"}
    unresolved_reservations = [r for r in reservations if r["attempt_id"] not in finishes]
    stop = "uncertain_prior_attempt" if unresolved_reservations else None
    if any(not r["complete"] for r in finishes.values()):
        stop = "incomplete_prior_evidence"
    reserved = _money(m["budget"]["reservation_per_attempt_usd"])
    spent = sum((max(reserved, _money(finishes[r["attempt_id"]]["accounting"]["reported_subtotal_usd"]))
                 if r["attempt_id"] in finishes else reserved for r in reservations), Decimal(0))
    count = len(reservations)
    finished_cells = {(r["case"], r["arm"], r["rep"]) for r in reservations if r["attempt_id"] in finishes}
    for cid, arm, rep in schedule(bundle):
        if stop:
            break
        if (cid, arm, rep) in finished_cells:
            continue
        for attempt in range(m["design"]["max_retries"] + 1):
            if count >= m["budget"]["max_attempts"] or spent + reserved > _money(m["budget"]["max_allocation_usd"]):
                stop = "authorized_admission_cap"
                break
            fresh = load_manifest(path)
            _require(fresh["manifest_sha256"] == bundle["manifest_sha256"] and fresh["raw_sha256"] == bundle["raw_sha256"], "manifest changed before transmission")
            request = request_for(bundle, cid, arm, rep, attempt)
            attempt_id = sha(canonical(request["identity"]))
            directory = output / attempt_id
            directory.mkdir()
            _exclusive(directory / "request.json", canonical(request))
            reservation = {"event": "reserve", "attempt_id": attempt_id, "case": cid, "arm": arm, "rep": rep,
                           "attempt": attempt, "request_sha256": digest(request), "reservation_usd": str(reserved)}
            previous = _journal(ledger, reservation, previous)
            count += 1
            spent += reserved
            begin = time.monotonic()
            outcome = None
            try:
                outcome = executor(copy.deepcopy(request), directory)
                _require(len(canonical(outcome)) <= MAX_FILE_BYTES, "outcome exceeds byte limit")
                validation = validate_outcome(outcome, request)
                elapsed = time.monotonic() - begin
                _exclusive(directory / "outcome.json", canonical(outcome))
                accounting = validation["accounting"]
                complete = validation["required_complete"] and validation["provenance_complete"] and accounting["cost_complete"]
                finish = {"event": "finish", "attempt_id": attempt_id, "outcome_sha256": digest(outcome),
                          "latency_seconds": elapsed, "accounting": accounting, "complete": complete,
                          "validation": validation, "error": None, "artifacts": artifact_hashes(directory)}
            except BaseException as error:
                # An exception may follow transmission. Never print/persist arbitrary provider error text.
                accounting = failed_accounting(directory, outcome)
                finish = {"event": "finish", "attempt_id": attempt_id, "outcome_sha256": None,
                          "latency_seconds": time.monotonic() - begin, "accounting": accounting,
                          "complete": False, "validation": None, "error": type(error).__name__, "artifacts": artifact_hashes(directory)}
                spent += max(Decimal(0), _money(accounting["reported_subtotal_usd"]) - reserved)
                previous = _journal(ledger, finish, previous)
                stop = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "incomplete_evidence"
                break
            previous = _journal(ledger, finish, previous)
            spent += max(Decimal(0), _money(accounting["reported_subtotal_usd"]) - reserved)
            if not complete:
                stop = "incomplete_evidence"
            # No automatic retry of billed/uncertain attempts; retry allowance is a ceiling, not permission to replay.
            break
    previous = _journal(ledger, {"event": "stop", "reason": stop or "schedule_complete", "attempts": count,
                                "allocation_consumed_usd": str(spent)}, previous)
    result = report(path, output)
    _exclusive(output / "report.json", canonical(result))
    return result


def _means(values):
    return sum(values) / len(values) if values and all(v is not None for v in values) else None


def _decision(stats, absolute, t):
    p = t["primary"]
    interval = stats[p["metric"]]["interval"]
    quality = stats["decision_accuracy"]["interval"]
    if interval is None or quality is None or stats[p["metric"]]["n"] < t["min_cases"]:
        return False
    low = interval["low"] if p["direction"] == "higher" else -interval["high"]
    return low >= p["min_improvement"] and quality["low"] >= -t["quality_noninferiority"] \
        and absolute["decision_accuracy"] is not None and absolute["decision_accuracy"] >= t["baseline_quality_min"] \
        and absolute["unsafe_rate"] is not None and absolute["unsafe_rate"] <= t["unsafe_max"] \
        and absolute["unresolved_rate"] is not None and absolute["unresolved_rate"] <= t["unresolved_max"]


def report(path, output):
    """Recompute from the bound ledger and outcomes, never trust a previous report's scores."""
    bundle, output = load_manifest(path), Path(output).resolve()
    m = bundle["manifest"]
    _require(decode((output / "manifest.json").read_bytes()) == m, "retained manifest changed")
    records, ledger_sha = read_ledger(output / "attempts.jsonl")
    _require(records and records[0]["event"] == "start" and records[0]["manifest_sha256"] == bundle["manifest_sha256"], "ledger belongs to another manifest")
    _require(m["frozen_at"] < _timestamp(records[0]["started_at"]), "ledger is not a post-freeze execution")
    reservations, finishes = {}, {}
    for r in records[1:]:
        if r["event"] == "reserve":
            _require(r["attempt_id"] not in reservations, "duplicate attempt reservation")
            reservations[r["attempt_id"]] = r
        elif r["event"] == "finish":
            _require(r["attempt_id"] in reservations and r["attempt_id"] not in finishes, "unreserved/duplicate finish")
            finishes[r["attempt_id"]] = r
        else:
            _require(r["event"] == "stop", "unknown ledger event")
    cases = {c["manifest"]["id"]: c for c in bundle["cases"]}
    results, observed, complete = [], {a: [] for a in ARMS}, True
    for aid, reserve in reservations.items():
        expected = request_for(bundle, reserve["case"], reserve["arm"], reserve["rep"], reserve["attempt"])
        _require(aid == sha(canonical(expected["identity"])) and reserve["attempt"] == 0
                 and (reserve["case"], reserve["arm"], reserve["rep"]) in schedule(bundle), "invalid attempt identity")
        request = decode((output / aid / "request.json").read_bytes())
        _require(request == expected and digest(request) == reserve["request_sha256"], "attempt request changed")
        finish = finishes.get(aid)
        if finish is not None:
            _require(finish["artifacts"] == artifact_hashes(output / aid), "outcome artifact changed or attempt artifacts changed")
        if finish is None or finish["outcome_sha256"] is None:
            if finish is not None:
                _require(account(decode((output / aid / "failed-receipts.json").read_bytes())) == finish["accounting"], "failed attempt accounting changed")
            complete = False
            continue
        outcome = decode((output / aid / "outcome.json").read_bytes())
        _require(digest(outcome) == finish["outcome_sha256"], "outcome artifact changed")
        validation = validate_outcome(outcome, request)
        _require(validation == finish["validation"] and validation["accounting"] == finish["accounting"], "ledger accounting changed")
        complete = complete and finish["complete"] and validation["required_complete"] and validation["provenance_complete"]
        scored = score(cases[reserve["case"]], outcome, finish["latency_seconds"], validation["accounting"], m["thresholds"]["human_time_protocol"])
        results.append({"case": reserve["case"], "arm": reserve["arm"], "rep": reserve["rep"], "attempt_id": aid, **scored})
        observed[reserve["arm"]].append(outcome["observed"])
    cells = {(r["case"], r["arm"], r["rep"]) for r in results}
    expected_cells = set(schedule(bundle))
    complete = complete and cells == expected_cells and len(results) == len(expected_cells)
    units = independent_units(bundle)
    per_arm = {arm: {metric: {} for metric in METRICS} for arm in ARMS}
    for arm in ARMS:
        for unit in units:
            uid = digest(unit)
            rows = [r for r in results if r["arm"] == arm and r["case"] in unit]
            expected_count = sum((cid, arm, rep) in expected_cells for cid in unit for rep in range(m["design"]["repetitions"]))
            if expected_count == 0:
                continue
            for metric in METRICS:
                per_arm[arm][metric][uid] = _means([r["metrics"][metric] for r in rows]) if len(rows) == expected_count else None
    shared = _shared()
    def contrast(left, right):
        return {metric: shared.paired_case_comparison(per_arm[left][metric], per_arm[right][metric]) for metric in METRICS}
    bc, cb = contrast("B", "C"), contrast("C", "B")
    absolute = {a: {metric: _means(list(values.values())) for metric, values in per_arm[a].items()} for a in ARMS}
    models = {a: {digest({key: r[key] for key in ("model", "thinking", "judge")}) for r in observed[a]} for a in ARMS}
    direct_models = {a: {digest({key: r[key] for key in ("model", "thinking")}) for r in observed[a]} for a in ARMS}
    evidence_class = records[0]["evidence_class"]
    frozen_gold = all(c["manifest"]["split"] == "heldout" and not any(e["kind"] in ("tuned", "evaluated", "published")
                      for e in c["manifest"]["exposure_history"]) for c in bundle["cases"])
    gates = {"complete_outcomes": bool(complete), "observed_B_C_identity": len(models["B"]) == 1 and models["B"] == models["C"],
             "heldout_unexposed_gold": frozen_gold, "independent_minimum": len(units) >= m["thresholds"]["min_cases"],
             "no_duplicate_aliases": all(len(u) == 1 for u in units),
             "observed_cost_complete": len(finishes) == len(reservations) and bool(finishes) and all(f["accounting"]["cost_complete"] for f in finishes.values())}
    verdict = "inconclusive"
    if all(gates.values()):
        if _decision(bc, absolute["C"], m["thresholds"]):
            verdict = "adopt"
        elif _decision(cb, absolute["B"], m["thresholds"]):
            verdict = "keep_deterministic"
        elif m["thresholds"]["tie"] == "keep_deterministic":
            primary = bc[m["thresholds"]["primary"]["metric"]]["interval"]
            if primary and max(abs(primary["low"]), abs(primary["high"])) < m["thresholds"]["primary"]["min_improvement"] \
                    and absolute["B"]["decision_accuracy"] >= m["thresholds"]["baseline_quality_min"] \
                    and absolute["B"]["unsafe_rate"] <= m["thresholds"]["unsafe_max"] \
                    and absolute["B"]["unresolved_rate"] <= m["thresholds"]["unresolved_max"]:
                verdict = "keep_deterministic"
    direct = "inconclusive"
    if all(gates.values()) and m["thresholds"]["secondary_direct"] and direct_models["D"] == direct_models["B"] \
            and _decision(contrast("B", "D"), absolute["D"], m["thresholds"]):
        direct = "direct-review preferred"
    observed_subtotal = sum((_money(f["accounting"]["reported_subtotal_usd"]) for f in finishes.values()), Decimal(0))
    monetary_complete = bool(finishes) and len(finishes) == len(reservations) and all(f["accounting"]["cost_complete"] for f in finishes.values())
    recommendation = verdict if evidence_class == "model_measurement" and all(c["manifest"]["kind"] == "realistic" for c in bundle["cases"]) else "inconclusive"
    stages = {}
    for stage in STAGES:
        buckets = [f["accounting"]["stages"][stage] for f in finishes.values()]
        stages[stage] = {"reported_subtotal_usd": str(sum((_money(b["reported_subtotal_usd"]) for b in buckets), Decimal(0))),
                         "catalog_estimate_subtotal_usd": str(sum((_money(b["catalog_estimate_subtotal_usd"]) for b in buckets), Decimal(0))),
                         "cost_complete": bool(buckets) and len(finishes) == len(reservations) and not any(b["cost_unknown"] for b in buckets),
                         "input_tokens": sum(b["input_tokens"] for b in buckets), "tokens_complete": bool(buckets) and len(finishes) == len(reservations) and not any(b["tokens_unknown"] for b in buckets),
                         "provider_passes": sum(b["provider_passes"] for b in buckets), "passes_complete": bool(buckets) and len(finishes) == len(reservations) and not any(b["passes_unknown"] for b in buckets),
                         "attempts": sum(b["attempts"] for b in buckets)}
    return {"schema": REPORT_SCHEMA, "manifest_sha256": bundle["manifest_sha256"], "ledger_sha256": ledger_sha,
            "report_identity": {"implementation": m["implementation"], "thresholds_sha256": digest(m["thresholds"]),
                                "source_inventories": {cid: digest(c["manifest"]["sources"]) for cid, c in cases.items()}},
            "evidence_class": evidence_class, "case_kind": sorted({c["manifest"]["kind"] for c in bundle["cases"]}),
            "attestation": "operator_attested_unauthenticated", "independent_cases": len(units), "gates": gates,
            "provenance_trust": "trusted_host_process_no_same_uid_isolation" if evidence_class == "model_measurement" else "injected_software_only",
            "primary_contrast": {"label": "C minus B: incremental extraction only", "metrics": bc,
                                 "reverse_metrics": cb, "gate_verdict": verdict},
            "secondary_contrasts": {"A_to_B": contrast("A", "B"), "B_to_D": contrast("B", "D"), "direct_verdict": direct},
            "recommendation": recommendation,
            "withholding": [key for key, ok in gates.items() if not ok] + ([] if recommendation == verdict else ["not real-world model measurements"]),
            "observed": observed, "absolute_means": absolute, "outcomes": results,
            "cost": {"observed_subtotal_usd": str(observed_subtotal), "observed_total_usd": str(observed_subtotal) if monetary_complete else None,
                     "catalog_estimate_subtotal_usd": str(sum((_money(f["accounting"]["catalog_estimate_subtotal_usd"]) for f in finishes.values()), Decimal(0))),
                     "accounting_basis": "provider_reported_not_invoice", "billing_complete": False,
                     "hard_invoice_cap": False, "attempts": len(reservations), "stages": stages,
                     "uncertain_attempts": len(reservations) - sum(f["accounting"]["cost_complete"] for f in finishes.values())},
            "stop": next((r for r in reversed(records) if r["event"] == "stop"), None)}


class HostExecutor:
    """Separate-workspace OMP launch; only C admits the pinned extraction extension.

    The host process/event stream is trusted, not same-UID OS-sandboxed. Model-written
    journals are not attestations. Stock catalog costs remain estimates; unknown
    reported costs can deliberately stop an experiment after its first paid attempt.
    """
    def __init__(self, bundle):
        self.bundle = bundle
        self.identity = {"kind": "installed_omp", "host": bundle["manifest"]["host"], "candidate": bundle["candidate"],
                         "host_observer_sha256": sha(HOST_OBSERVER.encode())}

    def argv(self, request, workspace, session, config, prompt):
        host = self.bundle["manifest"]["host"]
        argv = [host["path"], "--session-dir", str(session), "--mode", "json", "--auto-approve", "--config", str(config),
                "--no-extensions", "--trusted-extension", str(config.parent / "observer.ts"),
                "--no-rules", "--no-lsp", "--no-title", "--model", host["model"],
                "--thinking", host["thinking"], "--max-time", str(host["max_seconds"])]
        if request["identity"]["arm"] == "C":
            argv += ["--trusted-extension", self.bundle["candidate"]["path"]]
        tools = ["read", "eval", "grep", "glob", "bash", "find", "edit", "write"]
        if request["identity"]["arm"] == "D":
            tools = ["read", "grep", "glob", "find"]
        if request["identity"]["arm"] == "C":
            tools.append("jevify_extract")
        argv += ["--tools", ",".join(tools), prompt]
        return argv

    @staticmethod
    def judge_overlay(judge):
        _require(judge["thinking"] == "off", "supported host judgment disables reasoning; judge thinking must be off")
        selector = lambda model: model + ":" + judge["thinking"]
        return ("modelRoles:\n  judge: " + json.dumps(selector(judge["model"]))
                + "\nretry:\n  fallbackChains:\n    judge: "
                + json.dumps([selector(model) for model in judge["fallback_models"]]) + "\n")

    @staticmethod
    def evidence_inputs(request):
        arm = request["identity"]["arm"]
        sources = [{"id": sid, "role": source["role"], "text": source["text"], "revision": source["revision"],
                    "subject": source["subject"], "version": source["version"],
                    "snapshot_identity": source["file"]["sha256"], "classification": "private"}
                   for sid, source in request["sources"].items()]
        fields = {name: {"description": value["instructions"]} for name, value in request["controls"]["fields"].items()}
        records = [{"id": r["id"], "source_id": r["source_id"], "required_fields": list(fields),
                    "required_questions": r["required_questions"], "dependencies": r["dependencies"],
                    "subject": r["subject"], "version": r["revision"],
                    "facts": {"retrieved_passages": [request["passages"][index] for index in r["passages"][arm]]}}
                   for r in request["inventory"]]
        return records, fields, sources

    def __call__(self, request, directory):
        shared = _shared()
        host = self.bundle["manifest"]["host"]
        root = Path(self.bundle["manifest"]["candidate"]["root"])
        package = decode((root / "package.json").read_bytes())
        _require(package.get("name") == "@ericjuta/omp-jevify" and self.bundle["manifest"]["candidate"]["entry"]
                 in [str(Path(entry)) for entry in package.get("omp", {}).get("extensions", [])],
                 "live candidate is not the bound packaged Jevify extension")
        with tempfile.TemporaryDirectory(prefix="jevify-preparation-") as temporary:
            base = Path(temporary)
            workspace, session = base / "workspace", base / "session"
            workspace.mkdir()
            session.mkdir()
            (workspace / "request.json").write_bytes(canonical(request))
            config = base / "config.yml"
            configuration = shared.overlay_text("skill", root / "skills") + self.judge_overlay(request["controls"]["judge"])
            config.write_text(configuration)
            (base / "observer.ts").write_text(HOST_OBSERVER)
            arm = request["identity"]["arm"]
            plan = None
            preparation = "Read request.json and perform the current workflow using the frozen strategy."
            if arm in ("B", "C"):
                evidence_path = root / "skills" / "jevify" / "evidence.py"
                _require("skills/jevify/evidence.py" in self.bundle["manifest"]["candidate"]["files"],
                         "candidate must bind the installed deterministic evidence implementation")
                spec = importlib.util.spec_from_file_location("jevify_preparation_evidence", evidence_path)
                evidence = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(evidence)
                records, fields, source_inputs = self.evidence_inputs(request)
                plan = evidence.prepare_evidence(records, fields, root=workspace, sources=source_inputs)
                states = evidence.evidence_states(plan, {q: q for r in request["inventory"] for q in r["required_questions"]})
                (workspace / "prepared.json").write_bytes(canonical(states))
                preparation = "Read request.json and prepared.json. Use the frozen deterministic evidence and strategy."
                if arm == "C":
                    preparation += (f" First call jevify_extract exactly once with artifact_root={plan['artifact_root']!r} and "
                                    f"plan_digest={plan['plan_digest']!r}; read the returned artifact. Never retry extraction. "
                                    "On failure preserve deterministic evidence. Do not treat a candidate span as a label.")
            elif arm == "D":
                preparation = "Read request.json and review sources directly, without scripts, judgment helpers or extraction."
            prompt = preparation + (
                " Apply controls.rubric, controls.judge, controls.fallback and controls.completion exactly. "
                "Return one JSON object with records and processed_passages. Each record has exactly id, subject, revision, "
                "qualifications (strings), answers (question-to-value map, null for an explicit abstention), and occurrences "
                "(objects with source_id, revision, start, end, quote; start/end are Unicode codepoint offsets). "
                "Preserve every inventory ID and required question; do not invent answers. processed_passages lists zero-based "
                "request.passages indices actually reviewed. No cost, model provenance or gold may be invented.")
            argv = self.argv(request, workspace, session, config, prompt)
            _exclusive(directory / "launch.json", canonical({"argv": argv, "candidate": request["candidate"],
                       "host_observer_sha256": sha(HOST_OBSERVER.encode())}))
            env = os.environ.copy()
            env["OMP_JUDGMENT_CACHE_DB"] = str(base / "cold-cache.sqlite")
            proc = subprocess.Popen(argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            watchdog = threading.Timer(host["max_seconds"] + 30, shared.kill_tree, args=(proc,))
            watchdog.start()
            messages, calls, models, thinking, answers = [], [], set(), set(), []
            extraction_results, observations = [], []
            ended = malformed = False
            abort_host = threading.Event()

            def consume(line):
                nonlocal ended, malformed
                try:
                    event = decode(line)
                except (ValueError, UnicodeError):
                    malformed = True
                    return
                if not isinstance(event, dict):
                    malformed = True
                    return
                msg = event.get("message")
                if event.get("type") == "message_end" and isinstance(msg, dict) and msg.get("role") == "assistant":
                    messages.append(msg)
                    if _text(msg.get("model")):
                        models.add(msg["model"])
                    if msg.get("stopReason") == "stop":
                        answers.append(shared.message_text(msg))
                if event.get("type") == "thinking_level_changed":
                    effective = event.get("thinkingLevel", event.get("resolved"))
                    if _text(effective):
                        thinking.add(effective)
                if event.get("type") == "preparation_observation":
                    try:
                        self.capture_observation(event, directory, len(observations))
                        observations.append(event)
                        thinking.add(event["thinking"])
                    except (ValueError, KeyError, TypeError, OSError):
                        malformed = True
                if event.get("type") == "tool_execution_start":
                    calls.append({"id": event.get("toolCallId"), "tool": event.get("toolName"), "args": event.get("args")})
                    if len(calls) >= host["max_calls"]:
                        malformed = True
                        abort_host.set()
                if event.get("type") == "tool_execution_end" and event.get("toolName") == "jevify_extract":
                    call = next((call for call in calls if call["id"] == event.get("toolCallId")), None)
                    if arm != "C" or call is None:
                        malformed = True
                    else:
                        try:
                            captured = self.capture_extraction(event, call, plan, directory, len(extraction_results))
                            extraction_results.append(captured)
                        except (ValueError, KeyError, TypeError, OSError):
                            malformed = True
                if event.get("type") == "agent_end":
                    ended = True

            stop_reader = threading.Event()
            reader_failed = []

            def drain():
                pending = bytearray()
                try:
                    fd = proc.stdout.fileno()
                    while not stop_reader.is_set():
                        if not select.select([fd], [], [], .1)[0]:
                            continue
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            if pending:
                                consume(pending)
                            return
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, remainder = pending.partition(b"\n")
                            pending = bytearray(remainder)
                            if len(line) > MAX_FILE_BYTES:
                                raise ValueError("host event exceeds byte limit")
                            consume(line)
                        if len(pending) > MAX_FILE_BYTES:
                            raise ValueError("host event exceeds byte limit")
                except Exception as error:
                    reader_failed.append(type(error).__name__)
                    abort_host.set()

            reader = threading.Thread(target=drain, name="jevify-host-events", daemon=True)
            reader.start()
            try:
                while not abort_host.is_set():
                    try:
                        proc.wait(timeout=.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                watchdog.cancel()
                try:
                    # The reader remains active while termination produces final receipts.
                    shared.kill_tree(proc)
                finally:
                    reader.join(timeout=5)
                    if reader.is_alive():
                        malformed = True
                        stop_reader.set()
                        reader.join(timeout=1)
                    proc.stdout.close()
                    malformed = malformed or bool(reader_failed)
                    receipts, judge_models = self.collect_receipts(shared, messages, extraction_results, arm, observations,
                        digest(request["identity"]), proc.returncode == 0 and ended and not malformed)
                    _exclusive(directory / "captured-receipts.json", canonical(receipts))
            invoked = arm == "C" and len(extraction_results) == 1 and extraction_results[0]["published"]
            _require(proc.returncode == 0 and ended and not malformed and bool(answers), "host execution incomplete")
            answer = decode(answers[-1])
            _object(answer, ("records", "processed_passages"), "host answer")
            _require(config.read_text() == configuration, "frozen host judge configuration changed")
            judge = {"configuration": request["controls"]["judge"], "models": sorted(judge_models)} if judge_models else {"mode": "direct_no_judge"} if arm == "D" else None
            extraction = None
            if invoked:
                captured = extraction_results[0]
                self.verify_extraction_publication(captured, plan)
                attempts = captured["result"].get("provenance", {}).get("attempts", [])
                extraction = {"requested_model": "cloudflare/clairic",
                              "observed_models": sorted({a["observed_model"] for a in attempts if _text(a.get("observed_model"))}),
                              "observed_providers": sorted({a["observed_provider"] for a in attempts if _text(a.get("observed_provider"))})}
                if any(not _text(a.get("observed_model")) or not _text(a.get("observed_provider")) for a in attempts):
                    extraction = None
            return {"schema": OUTCOME_SCHEMA, "identity": request["identity"],
                    "observed": {"model": next(iter(models)) if len(models) == 1 else None,
                                 "thinking": next(iter(thinking)) if len(thinking) == 1 else None,
                                 "judge": judge, "candidate": request["candidate"] if invoked else None,
                                 "extraction_invoked": invoked, "extraction": extraction},
                    **answer, "receipts": receipts, "human_review": None}

    @staticmethod
    def capture_observation(event, directory, index):
        _object(event, ("type", "schema", "session_id", "thinking", "usage", "final"), "host observation")
        _require(event["type"] == "preparation_observation" and event["schema"] == "jevify-host-observation/1"
                 and _text(event["session_id"]) and event["thinking"] in ("off", "minimal", "low", "medium", "high", "xhigh", "max")
                 and isinstance(event["usage"], list) and type(event["final"]) is bool, "invalid host observation")
        allowed = {"id", "purpose", "role", "api", "provider", "model", "stopReason", "estimated", "costUnknown", "usage"}
        for row in event["usage"]:
            _require(isinstance(row, dict) and set(row) <= allowed and _text(row.get("id"))
                     and _text(row.get("purpose")) and isinstance(row.get("usage"), dict), "invalid host usage projection")
            for key, value in row.items():
                if key in ("estimated", "costUnknown"):
                    _require(type(value) is bool, "invalid host usage flag")
                elif key != "usage":
                    _require(_text(value) and len(value) <= 512, "invalid host usage identity")
            usage = row["usage"]
            _require(set(usage) <= {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"}
                     and all(_number(value) and value >= 0 for key, value in usage.items() if key != "cost"),
                     "host usage must contain only numeric counts")
            cost = usage.get("cost", {})
            _require(isinstance(cost, dict) and set(cost) <= {"input", "output", "cacheRead", "cacheWrite", "total"}
                     and all(_number(value) and value >= 0 for value in cost.values()), "invalid host cost projection")
        raw = canonical(event)
        _require(len(raw) <= MAX_FILE_BYTES, "host observation exceeds byte limit")
        _exclusive(directory / f"host-observation-{index}.json", raw)

    @staticmethod
    def capture_extraction(event, call, plan, directory, index):
        _require(event.get("toolName") == call["tool"] == "jevify_extract" and _text(call["id"])
                 and event.get("toolCallId") == call["id"], "extraction completion identity mismatch")
        args = call["args"]
        _require(isinstance(args, dict) and args.get("artifact_root") == plan["artifact_root"]
                 and args.get("plan_digest") == plan["plan_digest"], "extraction invocation used another plan")
        details = event.get("result", {}).get("details", {})
        receipt = details.get("controllerReceipt") if isinstance(details, dict) else None
        _object(receipt, ("schema", "tool_call_id", "plan_digest", "artifact_root", "result_sha256", "result", "published"),
                "controller extraction receipt")
        _require(receipt["schema"] == "jevify-extraction-receipt/1" and receipt["tool_call_id"] == call["id"]
                 and receipt["plan_digest"] == plan["plan_digest"] and receipt["artifact_root"] == plan["artifact_root"]
                 and type(receipt["published"]) is bool, "controller receipt identity mismatch")
        result = receipt["result"]
        _object(result, ("schema", "plan_digest", "run_id", "generation", "disposition", "records", "fields", "candidates",
                         "request_attempts", "reused", "transmission_uncertain", "result_sha256", "provenance"),
                "sanitized extraction accounting")
        raw = canonical(result)
        _require(len(raw) <= MAX_FILE_BYTES and sha(raw) == receipt["result_sha256"]
                 and result["schema"] == "jevify-extraction-accounting/1" and result["plan_digest"] == plan["plan_digest"],
                 "controller result binding mismatch")
        _require(all(type(result[key]) is int and result[key] >= 0 for key in ("records", "fields", "candidates", "request_attempts"))
                 and result["reused"] is False and type(result["transmission_uncertain"]) is bool
                 and (result["provenance"] is None or isinstance(result["provenance"], dict)),
                 "sanitized accounting counts or provenance invalid")
        _require(result["generation"] is None or type(result["generation"]) is int and 0 <= result["generation"] <= 32,
                 "invalid extraction generation")
        if receipt["published"]:
            _require(type(result["generation"]) is int and _text(result["run_id"])
                     and isinstance(result["result_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", result["result_sha256"]),
                     "published result identity missing")
        _require(type(event.get("isError")) is bool, "controller completion status missing")
        _exclusive(directory / f"extraction-receipt-{index}.json", canonical(receipt))
        return copy.deepcopy(receipt)

    @staticmethod
    def verify_extraction_publication(captured, plan):
        accounting = captured["result"]
        generation = accounting["generation"]
        key = plan["plan_digest"] if generation == 0 else f'{plan["plan_digest"]}--{generation}'
        path = Path(plan["artifact_root"]) / "results" / f"{key}.json"
        _require(path.is_file() and path.stat().st_size <= MAX_FILE_BYTES
                 and sha(path.read_bytes()) == accounting["result_sha256"],
                 "published extraction result differs from controller receipt")

    @staticmethod
    def collect_receipts(shared, messages, extraction_results, arm, observations, scope, complete):
        receipts = []
        for index, msg in enumerate(messages):
            cost = shared.usage_reported_cost(msg.get("usage"))
            receipts.append({"id": f"outer:{index}", "stage": "completion", "status": "complete" if msg.get("stopReason") == "stop" else "failed",
                             "cost_usd": str(cost) if cost is not None else None, "cost_evidence": "catalog_estimate",
                             "input_tokens": shared.usage_tokens(msg.get("usage"))["input"], "provider_passes": None})
        # These top-level frames rely on the trusted host/process event-stream boundary,
        # not OS isolation. Model-writable sibling session JSONL files are ignored.
        auxiliary, conflicts = {}, False
        for observation in observations:
            for row in observation["usage"]:
                entry = shared.ledger_row(row, "host_observer", scope)
                if entry is None:
                    conflicts = True
                    continue
                previous = auxiliary.get(entry["key"])
                if previous is not None and previous != entry:
                    conflicts = True
                else:
                    auxiliary[entry["key"]] = entry
        snapshot_complete = (complete and bool(observations) and any(row["final"] for row in observations)
                             and len({row["session_id"] for row in observations}) == 1 and not conflicts)
        judge_models = set()
        for entry in auxiliary.values():
            stage = "judgment" if entry["bucket"] == "judgment" else "completion"
            if stage == "judgment" and entry["model"] is not None:
                judge_models.add(f'{entry["provider"]}/{entry["model"]}' if _text(entry["provider"]) else entry["model"])
            receipts.append({"id": "host:" + entry["key"], "stage": stage, "status": "complete",
                             "cost_usd": str(entry["reported_cost"]) if entry["reported_cost"] is not None else None,
                             "cost_evidence": "provider_reported" if entry["cost_basis"] == "reported" else "catalog_estimate",
                             "input_tokens": entry["tokens"]["input"], "provider_passes": None})
        receipts.append({"id": "preparation:local", "stage": "preparation", "status": "not_used", "cost_usd": "0",
                         "cost_evidence": "not_used", "input_tokens": 0, "provider_passes": 0})
        if arm == "C":
            for index, captured in enumerate(extraction_results):
                receipts.extend(HostExecutor.extraction_receipts(captured["result"], prefix=f"extraction:{index}"))
            if not any(receipt["stage"] == "extraction" for receipt in receipts):
                receipts.append({"id": "extraction:unrecorded", "stage": "extraction", "status": "failed", "cost_usd": None,
                                 "cost_evidence": "unknown", "input_tokens": None, "provider_passes": None})
        else:
            receipts.append({"id": "extraction:not_used", "stage": "extraction", "status": "not_used", "cost_usd": "0",
                             "cost_evidence": "not_used", "input_tokens": 0, "provider_passes": 0})
        if arm == "D" and not judge_models and snapshot_complete:
            receipts.append({"id": "judgment:not_used", "stage": "judgment", "status": "not_used", "cost_usd": "0",
                             "cost_evidence": "not_used", "input_tokens": 0, "provider_passes": 0})
        if not snapshot_complete:
            receipts.append({"id": "host:unobserved_remainder", "stage": "judgment", "status": "aborted", "cost_usd": None,
                             "cost_evidence": "unknown", "input_tokens": None, "provider_passes": None})
        return receipts, judge_models if not conflicts else set()

    @staticmethod
    def extraction_receipts(result, prefix="extraction"):
        # Only a controller-captured adapter completion may supply this object; never read the model's file.
        provenance = result.get("provenance", {})
        rows = provenance.get("attempts", []) if isinstance(provenance, dict) else []
        receipts = []
        for index, row in enumerate(rows):
            usage = row.get("usage", {}) if isinstance(row, dict) else {}
            cost = provider_decimal(usage.get("cost_usd"))
            receipts.append({"id": f"{prefix}:{index}", "stage": "extraction", "status": "complete" if row.get("disposition") == "complete" else "failed",
                             "cost_usd": str(cost) if cost is not None else None,
                             "cost_evidence": "provider_reported" if cost is not None and cost > 0 else "unknown",
                             "input_tokens": provider_count(usage.get("input_tokens")), "provider_passes": provider_count(usage.get("reported_passes"))})
        if result.get("transmission_uncertain") is True or result.get("request_attempts", len(rows)) != len(rows):
            receipts.append({"id": f"{prefix}:uncertain", "stage": "extraction", "status": "aborted", "cost_usd": None,
                             "cost_evidence": "unknown", "input_tokens": None, "provider_passes": None})
        return receipts


def freeze(draft, output):
    """Freeze exactly the supplied design. No thresholds, facts, or gold are synthesized."""
    draft, output = Path(draft).resolve(), Path(output).resolve()
    _require(draft.parent == output.parent, "freeze output must keep bound paths relative to the draft directory")
    value = decode(draft.read_bytes())
    value["implementation"] = code_identity()
    value["frozen_at"] = now()
    _exclusive(output, canonical(value))
    # Invalid frozen artifacts are retained for inspection, never overwritten on retry.
    return preflight(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("freeze", help="bind current implementation and freeze an explicit draft")
    p.add_argument("draft")
    p.add_argument("--out", required=True)
    p = sub.add_parser("preflight", help="provider-free source/identity/denominator admission")
    p.add_argument("manifest")
    p = sub.add_parser("run", help="run all four treatments with immutable attempt receipts")
    p.add_argument("manifest")
    p.add_argument("--out", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--injected", help="JSON outcomes keyed case:arm:rep:attempt; never model evidence")
    mode.add_argument("--live", action="store_true", help="requires independently adjudicated gold and exact paid approval")
    p.add_argument("--approval")
    p.add_argument("--resume", action="store_true")
    p = sub.add_parser("report", help="verify all artifacts and recompute the causal contrast")
    p.add_argument("manifest")
    p.add_argument("directory")
    p = sub.add_parser("demo", help="execute public synthetic software scenarios without any provider")
    p.add_argument("--out", required=True)
    p.add_argument("--scenario", choices=("improves", "worsens", "ties", "missing"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze(args.draft, args.out)
        elif args.command == "preflight":
            result = preflight(args.manifest)
        elif args.command == "report":
            result = report(args.manifest, args.directory)
        elif args.command == "demo":
            result = synthetic_demo(args.out, args.scenario)
        else:
            approval = decode(Path(args.approval).read_bytes()) if args.approval else None
            executor = InjectedExecutor(args.injected) if args.injected else None
            result = run_experiment(args.manifest, args.out, executor=executor, approval=approval, live=args.live, resume=args.resume)
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"preparation refused: {error}\n")


def synthetic_fixture(root, scenario="improves"):
    """Public, adversarial software fixtures. Fixture adjudicators are explicitly NOT humans.

    Returns manifest/outcomes paths. No real holdout, provider, credential or existing
    experiment is read. The generated files make the complete injection CLI reproducible.
    """
    _require(scenario in ("improves", "worsens", "ties", "missing"), "unknown software scenario")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    frozen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2))
    def put(name, value):
        target = root / name
        _exclusive(target, canonical(value) if not isinstance(value, str) else value.encode())
        return file_ref(target, root)
    controls = {"rubric": "Decide whether the exact versioned claim is supported, retaining conditions, exceptions and negation. "
                            "Use null when a required base/contract is absent. Read-only recommendations only.",
                "fields": {"evidence": {"instructions": "Return the exact qualified subject/version occurrence, never its neighbor.", "null_threshold": .5}},
                "judge": {"model": "fixture-judge", "thinking": "off", "fallback_models": [], "rubric_revision": "software-example/1"},
                "fallback": "retain deterministic sources; unresolved on missing evidence",
                "completion": "answer every required question or explicitly abstain; never omit a record"}
    controls_ref = put("controls.json", controls)
    groups = [sorted(CHALLENGES)[:5], sorted(CHALLENGES)[5:10], sorted(CHALLENGES)[10:]]
    cases = []
    for index, challenges in enumerate(groups):
        cid = f"public-adversarial-{index}"
        subject = f"wrapper_{index}"
        sentence = f"🧪 Claim {cid}: {subject}@v2 does NOT retry unless the caller opts in; except cancelled requests."
        text = (f"{subject}@v1 always retries. This is the wrong version.\n" + sentence + "\n"
                f"Neighbor {cid}-other: reused symbol {subject} permits retries without conditions.\n"
                "Identical repeated quote.\nIdentical repeated quote.\n"
                "The wrapper delegates to a helper; no BASE snapshot or governing contract was supplied.\n"
                "A stale external report supplies a plausible guess, not evidence.\n")
        source = put(f"{cid}.txt", text)
        sources = {"comment": {"revision": "v2", "role": "original_comment", "file": source, "subject": subject, "version": "v2"}}
        citation = {"source_id": "comment", "revision": "v2", "start": text.index(sentence),
                    "end": text.index(sentence) + len(sentence), "quote": sentence}
        inventory = [{"id": cid, "subject": subject, "revision": "v2", "required_questions": ["supported", "base_proves_introduced"],
                      "source_id": "comment", "dependencies": ["comment"], "passages": {a: [0] for a in ARMS}}]
        gold = {"schema": "jevify-preparation-gold/1", "claims": {cid: {"subject": subject, "revision": "v2",
                "qualifications": ["unless the caller opts in", "except cancelled requests"], "occurrences": [citation],
                "questions": {"supported": {"value": "qualified_only", "unsafe_values": ["repair_now"]},
                              "base_proves_introduced": {"value": None, "unsafe_values": ["yes"]}}}}}
        retrieval = put(f"{cid}-passages.json", [citation])
        cases.append({"id": cid, "unit_id": cid, "kind": "synthetic", "provenance": "Public generated adversarial SOFTWARE fixture, not independently human-adjudicated evidence",
                      "split": "heldout", "exposure_history": [{"at": frozen, "kind": "authored", "actor": "software-fixture-generator"}],
                      "direct_suitable": True, "challenges": challenges, "sources": sources,
                      "inventory": put(f"{cid}-inventory.json", inventory), "retrieval": {a: retrieval for a in ARMS},
                      "gold": put(f"{cid}-gold.json", gold),
                      "adjudication": {"key_author": "synthetic-author", "adjudicator": "synthetic-independent-fixture",
                                       "kind": "fixture", "at": frozen, "key_sha256": sha(canonical(gold)), "source_inventory_sha256": digest(sources),
                                       "evidence": put(f"{cid}-adjudication.txt", f"SOFTWARE FIXTURE ONLY: {cid}; expected semantic labels are authored for runner testing, not human adjudication.\n")}})
    treatments = {a: {"strategy": "fixture-fixed-method" if a != "D" else "direct-reading",
                      "preparation": {"A": "current", "B": "deterministic", "C": "deterministic", "D": "direct"}[a],
                      "extraction": a == "C", "controls_sha256": controls_ref["sha256"]} for a in ARMS}
    manifest = {"schema": SCHEMA, "pilot_id": "public-software-" + scenario, "frozen_at": frozen, "phase": "heldout",
                "implementation": code_identity(), "candidate": {"root": str(HERE), "entry": "preparation.py", "files": {"preparation.py": sha(Path(__file__).read_bytes())}},
                "host": {"path": str(Path(sys.executable).resolve()), "sha256": sha(Path(sys.executable).read_bytes()),
                         "model": "fixture-model", "thinking": "off", "max_seconds": 30, "max_calls": 4},
                "controls": controls_ref, "treatments": treatments, "cases": cases,
                "thresholds": {"primary": {"metric": "decision_accuracy", "direction": "higher", "min_improvement": .2},
                               "min_cases": 3, "quality_noninferiority": 0, "unsafe_max": 0, "unresolved_max": 0,
                               "baseline_quality_min": .9, "confidence": .95, "interval": "paired_case_mean_t_interval",
                               "tie": "keep_deterministic", "human_time_protocol": None, "secondary_direct": True},
                "design": {"repetitions": 2, "seed": 907, "order": "seeded_shuffled_blocks", "max_retries": 0},
                "budget": {"max_attempts": 24, "max_allocation_usd": "24", "reservation_per_attempt_usd": "1",
                           "policy": "best_effort_reservation", "uncertain_charge": "full_reservation", "stop_on_incomplete": True}}
    manifest_path = root / "manifest.json"
    _exclusive(manifest_path, canonical(manifest))
    bundle = load_manifest(manifest_path)
    outcomes = {}
    for cid, arm, rep in schedule(bundle):
        request = request_for(bundle, cid, arm, rep, 0)
        gold = next(c for c in bundle["cases"] if c["manifest"]["id"] == cid)["gold"]["claims"][cid]
        good = arm == "C" if scenario in ("improves", "missing") else arm != "C" if scenario == "worsens" else True
        receipts = [{"id": stage, "stage": stage, "status": "complete" if stage != "extraction" or arm == "C" else "not_used",
                     "cost_usd": "0.01" if stage != "extraction" or arm == "C" else "0",
                     "cost_evidence": "provider_reported" if stage != "extraction" or arm == "C" else "not_used",
                     "input_tokens": 20 if stage != "extraction" or arm == "C" else 0,
                     "provider_passes": 1 if stage != "extraction" or arm == "C" else 0} for stage in STAGES]
        observed = {"model": "fixture-model", "thinking": "off",
                    "judge": {"configuration": controls["judge"], "models": [controls["judge"]["model"]]} if arm != "D" else {"mode": "direct_no_judge"},
                    "candidate": request["candidate"], "extraction_invoked": arm == "C",
                    "extraction": {"requested_model": "cloudflare/clairic", "observed_models": ["fixture-clairic"],
                                   "observed_providers": ["fixture-provider"]} if arm == "C" else None}
        if scenario == "missing" and arm == "C":
            receipts[1]["cost_usd"] = None
            receipts[1]["cost_evidence"] = "unknown"
            observed["model"] = None
        outcomes[f"{cid}:{arm}:{rep}:0"] = {"schema": OUTCOME_SCHEMA, "identity": request["identity"], "observed": observed,
                    "records": [{"id": cid, "subject": gold["subject"], "revision": gold["revision"],
                                 "qualifications": gold["qualifications"], "occurrences": gold["occurrences"],
                                 "answers": {"supported": "qualified_only" if good else "unqualified", "base_proves_introduced": None}}],
                    "processed_passages": [0], "receipts": receipts, "human_review": None}
    outcomes_path = root / "injected-outcomes.json"
    _exclusive(outcomes_path, canonical(outcomes))
    return manifest_path, outcomes_path


def synthetic_demo(output, scenario):
    output = Path(output).resolve()
    manifest, outcomes = synthetic_fixture(output, scenario)
    return run_experiment(manifest, output / "run", executor=InjectedExecutor(outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
