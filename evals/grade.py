"""Extract captured answer assertions, then score them locally against fixed keys.

Load in OMP's Python eval kernel and call ``await grade(paths, cases_path)``. Schema2
keys independently describe factual truth, action under the public policy, and origin.
Legacy Boolean keys are explicitly historical schema1, never converted into factual
truth. Schema2 answers are structured findings (evals/findings.py, jevify-findings/1), validated
deterministically against a source snapshot rebuilt from git trees, with no model call. Claims a
finding does not name are omitted; rejected, duplicated or conflicting claims are unresolved.
Omissions, deterministic attribution rejections (unattributed), and extraction failures all stay in planned
denominators; only extraction failures count as extraction_errors. Repair fields are
recommendations derived from asserted facets, not observed code repairs. Off-inventory
claims are unknown unless an explicit audit is supplied. Per-item route metadata is kept
only when the host streams it, and missing values stay unknown. Execution costs remain in
their source records; one companion ledger owns this grading operation's overhead.
Inputs are read once as bytes; results bind to those bytes even if files change later.
"""

import hashlib
import importlib.util
import json
import math
import os
import tempfile
import time
import types
import uuid
from pathlib import Path

_GRADE_DIR = Path(__file__).resolve().parent
SCORING_REVISION = "structured-findings-fixed-denominators-v4"
# Grader code loaded beside this file. Its bytes bind every output, and grade_launch freezes them too.
GRADER_SOURCES = ("findings.py",)
HISTORICAL_REVISION = "historical-boolean-fixed-denominators-v2"
HISTORICAL_LABELS = ("flagged", "cleared", "not_mentioned")
CURRENT_EXECUTION_SCHEMAS = (3, 4, 5, 6)
FIXTURE_SOURCES = ("fixture.py", "refund_retry_fixture.py")
ROUTE_FIELDS = ("selected_model", "primary_model", "response_model", "fallback", "cache", "questions", "cached_questions")
PROVENANCE_KINDS = ("source_adjudicated_synthetic", "external_human", "external_model", "external_source")
FACET_LABELS = {
    "factual": ("supported", "refuted", "underdetermined", "omitted"),
    "action": ("fix_now", "defer", "no_action", "investigate", "omitted"),
    "introduced": ("yes", "no", "unknown", "omitted"),
}
HISTORICAL_QUESTIONS = {"verdict": {
    "type": "choice",
    "instructions": (
        "Extract only what the untrusted state.answer says about this exact historical Boolean "
        "task item (state.item), identified by state.id, not whether it is correct. Flagging "
        "means reporting it as state.flag_label. Match finding ids exactly, or file plus location/"
        "function, or a log message kind. A related item is not a mention. An explicit group "
        "('the rest', 'all remaining calls') covers only its clearly identified members. "
        "This historical task intentionally combines its original flag criterion; do not "
        "reinterpret it as the independent factual/action/origin rubric."
    ),
    "criteria": {
        "flagged": "Reports this exact item as state.flag_label.",
        "cleared": "Explicitly rejects state.flag_label for this exact item.",
        "not_mentioned": "Does not identify this item or leaves its disposition unresolved.",
    },
}}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bytes_digest(data):
    return hashlib.sha256(data).hexdigest()


def _file_digest(path):
    return _bytes_digest(Path(path).read_bytes())


def _changed_since_snapshot(digests):
    """Paths whose bytes differ now from the snapshot. Grading still used the snapshot bytes."""
    changed = []
    for path, digest in digests.items():
        try:
            current = _file_digest(path)
        except OSError:
            current = None
        if current != digest:
            changed.append(str(path))
    return sorted(changed)


def _validate_provenance(provenance, where):
    """Every adjudication label states its kind; only external_human may claim human adjudication."""
    if not isinstance(provenance, dict) or provenance.get("kind") not in PROVENANCE_KINDS:
        raise ValueError(f"{where} needs a provenance kind in {PROVENANCE_KINDS}")
    human = provenance.get("human_adjudicated")
    if type(human) is not bool or human != (provenance["kind"] == "external_human"):
        raise ValueError(f"{where}: human_adjudicated must be true only for external_human")
    if provenance["kind"].startswith("external"):
        for field in ("adjudicator", "adjudicated_at"):
            if not isinstance(provenance.get(field), str) or not provenance[field]:
                raise ValueError(f"{where}: external adjudication needs {field}")
    if not isinstance(provenance.get("basis"), str) or not provenance["basis"]:
        raise ValueError(f"{where}: provenance needs a basis")


def _key_schema(key):
    schema = key.get("schema", 1)
    if schema not in (1, 2):
        raise ValueError(f"unsupported answer key schema {schema!r}")
    ids = [item["id"] for item in key["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("answer key has duplicate claim ids")
    for item in key["items"]:
        if not item.get("desc"):
            raise ValueError(f"answer key item {item['id']} has no description")
        if schema == 1:
            if item.get("flag") is not None and type(item["flag"]) is not bool:
                raise ValueError("historical schema1 keys require Boolean or unresolved flag")
        else:
            if item.get("factual") not in FACET_LABELS["factual"][:-1]:
                raise ValueError(f"invalid factual key for {item['id']}")
            if item.get("introduced") not in FACET_LABELS["introduced"][:-1]:
                raise ValueError(f"invalid introduced key for {item['id']}")
            actions = item.get("actions")
            if not isinstance(actions, list) or not actions or any(action not in FACET_LABELS["action"][:-1] for action in actions):
                raise ValueError(f"invalid accepted actions for {item['id']}")
            if item.get("defect") is not None and not isinstance(item["defect"], str):
                raise ValueError(f"invalid canonical defect for {item['id']}")
            if "fix_now" in actions and (not item.get("defect") or item["factual"] != "supported" or item["introduced"] == "unknown"):
                raise ValueError(f"definite fix_now key {item['id']} lacks supported defect/origin evidence")
    if schema == 1 and not key.get("flag_label"):
        raise ValueError("historical schema1 key has no original flag criterion")
    return schema


def score_items(truth, verdicts, errors=None):
    """Explicit historical Boolean scoring; absent/error/unknown items never gain credit."""
    errors = errors or {}
    labels = {item: verdicts.get(item) for item in truth if item not in errors}
    tp = sum(labels.get(item) == "flagged" and flag is True for item, flag in truth.items())
    fp = sum(labels.get(item) == "flagged" and flag is False for item, flag in truth.items())
    unknown_flags = sum(labels.get(item) == "flagged" and flag is None for item, flag in truth.items())
    fn = sum(flag is True and labels.get(item) != "flagged" for item, flag in truth.items())
    correct = sum((flag is True and labels.get(item) == "flagged") or (flag is False and labels.get(item) == "cleared")
                  for item, flag in truth.items())
    unresolved = sum(flag is None or labels.get(item) not in HISTORICAL_LABELS for item, flag in truth.items())
    return {"tp": tp, "fp": fp, "fn": fn, "recall": tp / (tp + fn) if tp + fn else None,
            "precision": tp / (tp + fp + unknown_flags) if tp + fp + unknown_flags else None,
            "false_flags": fp, "unresolved_flags": unknown_flags, "planned_items": len(truth),
            "correct": correct, "accuracy": correct / len(truth) if truth else None,
            "unresolved": unresolved, "key_unresolved": sum(flag is None for flag in truth.values()),
            "omitted": sum(label == "not_mentioned" for label in labels.values())}


def score_facets(items, verdicts, errors=None, unattributed=()):
    """Score independent facets; only evidence-backed definite repairs earn recommendation credit.

    Repeated claims about one defect count once. If any selected repair claim for that
    defect is false, unresolved, or misattributed, the candidate is not a justified
    recommendation. Unselected duplicates cannot increase recall or conceal a spurious
    selection. These are scores of asserted facets; no code repair was observed.

    unattributed names items whose attribution was deterministically rejected (findings validation).
    Any verdict supplied for them is ignored; they stay unresolved in every denominator and are
    counted as unattributed, not as extraction_errors. Every other missing or invalid facet is an
    extraction error, so a caller that supplies no attribution reports all gaps as failures.
    """
    errors = errors or {}
    rejected = set(unattributed)
    facets = {}
    normalized = {}
    for item in items:
        iid = item["id"]
        if iid in rejected:
            normalized[iid] = dict.fromkeys(FACET_LABELS)
            continue
        extracted = verdicts.get(iid) or {}
        failures = errors.get(iid) or {}
        normalized[iid] = {facet: extracted.get(facet) if not failures or isinstance(failures, dict) and facet not in failures else None
                           for facet in FACET_LABELS}
    for facet, choices in FACET_LABELS.items():
        correct = covered = omitted = unresolved = rejected_count = 0
        for item in items:
            value = normalized[item["id"]][facet]
            if value not in choices:
                unresolved += 1
                rejected_count += item["id"] in rejected
            elif value == "omitted":
                omitted += 1
            else:
                covered += 1
                expected = item["actions"] if facet == "action" else (item[facet],)
                correct += value in expected
        planned = len(items)
        uncertain = {"factual": "underdetermined", "action": "investigate", "introduced": "unknown"}[facet]
        facets[facet] = {"planned": planned, "correct": correct, "accuracy": correct / planned if planned else None,
                         "covered": covered, "coverage": covered / planned if planned else None,
                         "omitted": omitted, "extraction_errors": unresolved - rejected_count,
                         "unattributed": rejected_count, "unresolved": unresolved,
                         "asserted_uncertain": sum(decision[facet] == uncertain for decision in normalized.values()),
                         "key_uncertain": sum(uncertain in item["actions"] if facet == "action" else item[facet] == uncertain for item in items)}
    expected_defects = {item["defect"] for item in items if "fix_now" in item["actions"]}
    candidates = {}
    unsupported = []
    for item in items:
        decision = normalized[item["id"]]
        if decision["action"] != "fix_now":
            continue
        defect = ("defect", item["defect"]) if item.get("defect") else ("claim", item["id"])
        warranted = ("fix_now" in item["actions"] and item["factual"] == decision["factual"] == "supported"
                     and item["introduced"] == decision["introduced"] and decision["introduced"] in ("yes", "no"))
        candidates.setdefault(defect, []).append(warranted)
        if not warranted:
            unsupported.append(item["id"])
    recommended = {defect for (kind, defect), claims in candidates.items() if kind == "defect" and all(claims)}
    tp, fp, fn = len(recommended), len(candidates) - len(recommended), len(expected_defects - recommended)
    unresolved_items = sum(any(value not in FACET_LABELS[facet] for facet, value in decision.items())
                           for decision in normalized.values())
    repair = {"tp": tp, "fp": fp, "fn": fn, "recall": tp / len(expected_defects) if expected_defects else None,
              "precision": tp / len(candidates) if candidates else None, "false_flags": fp,
              "planned_defects": len(expected_defects), "predicted_defects": len(candidates),
              "recommended_defects": sorted(recommended), "unrecommended_defects": sorted(expected_defects - recommended),
              "unsupported_fix_now_claims": unsupported, "basis": "asserted_facets_not_observed_repairs"}
    return {"planned_items": len(items), "facets": facets,
            **{f"{facet}_{metric}": values[metric] for facet, values in facets.items() for metric in ("accuracy", "coverage")},
            **{field: repair[field] for field in ("tp", "fp", "fn", "recall", "precision", "false_flags")},
            "repair": repair, "unresolved_items": unresolved_items,
            "unattributed_items": sum(item["id"] in rejected for item in items),
            "unresolved_facets": sum(values["unresolved"] for values in facets.values()),
            "evidence_unresolved_items": [item["id"] for item in items
                                          if item["factual"] == "underdetermined" or item["introduced"] == "unknown"]}


def _mean(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize_graded(runs):
    """Never mix historical Boolean semantics with schema2 facet semantics."""
    by_schema = {}
    for schema in (1, 2):
        selected = [row for row in runs if row["key_schema"] == schema]
        if not selected:
            continue
        summary = {"n": len(selected), "planned_items": sum(row["planned_items"] for row in selected),
                   "empty": sum(row["empty"] for row in selected), "judge_errors": sum(row["judge_errors"] for row in selected),
                   **{field: _mean(row[field] for row in selected) for field in ("recall", "precision", "false_flags")}}
        if schema == 2:
            summary.update({field: _mean(row[field] for row in selected)
                            for field in ("factual_accuracy", "action_accuracy", "introduced_accuracy",
                                          "factual_coverage", "action_coverage", "introduced_coverage")})
            summary["unresolved"] = sum(row["unresolved_items"] for row in selected)
            summary["unattributed"] = sum(row["unattributed_items"] for row in selected)
            summary["evidence_unresolved"] = sum(len(row["evidence_unresolved_items"]) for row in selected)
        else:
            summary["accuracy"] = _mean(row["accuracy"] for row in selected)
            summary["unresolved"] = sum(row["unresolved"] for row in selected)
        by_schema[str(schema)] = summary
    return {"n": len(runs), "empty": sum(row["empty"] for row in runs), "by_key_schema": by_schema}


def summarize_controls(rows):
    """Deterministic runner controls. An unscored (None) attempt stays in the denominator."""
    def block(selected):
        n = len(selected)
        correct = sum(row["control_correct"] is True for row in selected)
        return {"n": n, "scored": sum(row["control_correct"] is not None for row in selected), "correct": correct,
                "control_accuracy": round(correct / n, 3) if n else None}
    return {"overall": block(rows), "final_attempt": block([row for row in rows if row["final_attempt"]]),
            "per_case": {case: block([row for row in rows if row["case"] == case])
                         for case in sorted({row["case"] for row in rows})}}


def summarize_off_inventory(rows):
    statuses = {}
    for row in rows:
        status = row["off_inventory"]["status"]
        statuses[status] = statuses.get(status, 0) + 1
    audited = [row["off_inventory"] for row in rows if row["off_inventory"]["audited"]]
    return {"status_counts": dict(sorted(statuses.items())), "audited_answers": len(audited),
            "false_claims": sum(item["false_claims"] for item in audited),
            "unknown_claims": sum(item["unknown_claims"] for item in audited)}


def _load_runs(path, *, captured_bytes=None):
    """Parse JSON or partial JSONL, optionally from an already captured byte snapshot."""
    text = (Path(path).read_bytes() if captured_bytes is None else captured_bytes).decode("utf-8")
    if Path(path).suffix == ".jsonl":
        meta, runs = {"schema": None, "partial": True}, [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        meta = json.loads(text)
        runs = meta.pop("runs")
    if any("answer" not in row and "attempt_records" not in row for row in runs):
        raise ValueError(f"{path}: no captured answer; execution schema1 cannot be graded")
    return meta, runs


def _sibling_module(name, source):
    """Execute a sibling module from the exact bytes this grading pass snapshotted (no import path)."""
    module = types.ModuleType(f"jevify_{name}")
    module.__file__ = str(_GRADE_DIR / f"{name}.py")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _fixture_keys(specs, sources=None):
    """Build each (dataset, scale) once from the bytes this grading pass snapshotted.

    Each bundle also carries the reviewed source snapshot and the schema2 output contract digest, both
    reconstructed from the frozen fixture repository (never from answers).
    """
    sources = sources or {name: (_GRADE_DIR / name).read_bytes() for name in (*FIXTURE_SOURCES, *GRADER_SOURCES)}
    fixture = _sibling_module("fixture", sources["fixture.py"])
    findings = _sibling_module("findings", sources["findings.py"])
    bundles = {}
    with tempfile.TemporaryDirectory(prefix="jevify-grade-") as tmp:
        for n, (dataset, scale) in enumerate(sorted(set(specs))):
            root = fixture.build(Path(tmp) / f"repo-{n}", scale=scale, dataset=dataset)
            public_identity = {path: _file_digest(root / path)
                               for path in ("review/rubric.json", "review/policy.md", "review/change.md") if (root / path).exists()}
            keys = fixture.answer_keys(root, dataset=dataset)
            files = {str(path.relative_to(root)): _file_digest(path)
                     for path in sorted(root.rglob("*")) if path.is_file() and ".git" not in path.parts}
            snapshot = findings.source_snapshot(root)
            bundles[(dataset, scale)] = {"keys": keys, "public_identity": public_identity,
                                         "private_keys_sha256": _digest(keys), "fixture_sha256": _digest(files),
                                         "snapshot": snapshot,
                                         "output_contract_sha256": findings.contract_digest(
                                             findings.contract_from_snapshot(snapshot))}
    return bundles


def _route_unknown(fields):
    """Mirror the helper's rule: any missing or inconsistent route field keeps provenance unknown."""
    selected, primary, fallback = fields["selected_model"], fields["primary_model"], fields["fallback"]
    if (not isinstance(selected, str) or not selected or not isinstance(primary, str) or not primary
            or type(fallback) is not bool or fallback != (selected != primary)):
        return True
    total, cached = fields["questions"], fields["cached_questions"]
    return (fields["cache"] not in ("hit", "partial", "miss") or type(total) is not int or total < 1
            or type(cached) is not int or not 0 <= cached <= total)


def _item_provenance(item, batch_id):
    fields = {field: getattr(item, field, None) for field in ROUTE_FIELDS}
    return {"batch_id": batch_id, **fields, "provenance_unknown": _route_unknown(fields), "source": "host_item"}


def _unknown_provenance(source, batch_id=None):
    return {"batch_id": batch_id, **{field: None for field in ROUTE_FIELDS}, "provenance_unknown": True, "source": source}


async def _drain_completions(batch, seconds, found):
    """Advance one batch. Keep host item objects when the handle streams them; True if it did."""
    stream = getattr(batch, "drain_iter", None)
    if callable(stream):
        iterator = stream(seconds)
        if hasattr(iterator, "__aiter__"):
            async for identifier, item in iterator:
                found[identifier] = item
            return True
    await batch.drain(seconds)
    return False


async def _judge(states, questions, timeout, concurrency):
    """Return per-facet extraction, failures, one explicit cost estimate, and per-state route provenance.

    Route metadata comes only from host item objects. Display model names never stand in
    for routing proof; absent fields stay unknown.
    """
    judge_batch = globals().get("judge_batch")
    if judge_batch is None:
        raise RuntimeError("grade() needs judge_batch: %load this file in OMP's Python eval kernel")
    batch = None
    problem = f"no extraction within {timeout}s"
    answers, failures, status = {}, {}, {}
    found, streamed = {}, False
    cleanup_errors = []
    try:
        if timeout > 0:
            batch = judge_batch(states, questions, intent="Extracting captured answer assertions", concurrency=concurrency)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    if await _drain_completions(batch, min(60.0, max(0.01, deadline - time.monotonic())), found):
                        streamed = True
                except Exception as error:
                    problem = f"extraction batch failed ({type(error).__name__})"
                    break
                status = batch.status()
                if status.get("done", 0) >= len(states) or not status.get("running", False):
                    break
            answers, failures, status = batch.results(), batch.failed(), batch.status()
    except Exception as error:
        problem = f"extraction unavailable ({type(error).__name__})"
    finally:
        if batch is not None:
            operations = [batch.cancel, batch.close] if len(answers) + len(failures) < len(states) else [batch.close]
            for operation in operations:
                try:
                    operation()
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
    labels, errors = {}, {}
    for state in states:
        labels[state], errors[state] = {}, {}
        for facet, question in questions.items():
            result = (answers.get(state) or {}).get(facet)
            choice = result.get("choice") if isinstance(result, dict) else None
            if choice in question["criteria"]:
                labels[state][facet] = choice
            else:
                errors[state][facet] = str(failures[state]) if state in failures else (
                    f"invalid {facet} extraction" if state in answers else problem)
        if not errors[state]:
            del errors[state]
    cost = status.get("cost")
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        cost = None
    batch_id = str(getattr(batch, "id", None) or status.get("id") or uuid.uuid4().hex)
    missing_source = "not_submitted" if batch is None else ("host_item_missing" if streamed else "host_item_stream_unavailable")
    provenance = {state: _item_provenance(found[state], batch_id) if state in found
                  else _unknown_provenance(missing_source, batch_id) for state in states}
    known = sum(not item["provenance_unknown"] for item in provenance.values())
    record = {"id": batch_id,
              "kind": "judgment_batch" if batch is not None else "unsubmitted_extraction", "items": len(states), "model": status.get("model"),
              "cost_usd": cost, "cost_basis": "batch_status_estimate", "billing_complete": False,
              "cleanup_errors": cleanup_errors,
              "cache": {field: status[field] for field in ("cache", "questions", "cachedQuestions") if field in status},
              "provenance_summary": {"items": len(provenance), "known": known, "unknown": len(provenance) - known,
                                     "item_stream": streamed},
              "provenance": provenance}
    return labels, errors, record


def _captured_answers(run):
    """Historical runs contain one answer; current runs retain each retry attempt separately."""
    attempts = run.get("attempt_records")
    if attempts is None:
        return [{"answer": run.get("answer") or "", "attempt_id": None, "answer_index": 0,
                 "final_attempt": True, "execution_status": "historical_unrecorded", "answer_status": None,
                 "process_status": None, "process_problems": None}]
    if not attempts:
        raise ValueError("retained-attempt execution has no retained attempts")
    captured = []
    attempt_ids = [attempt["attempt_id"] for attempt in attempts]
    if len(set(attempt_ids)) != len(attempt_ids) or run.get("final_attempt_id") not in attempt_ids:
        raise ValueError("invalid retained attempt identities")
    for attempt in attempts:
        answers = attempt["final_answers"]
        if not isinstance(answers, list):
            raise ValueError("retained-attempt final_answers must be a list")
        # A failed/empty attempt is still a planned answer, not its compatibility last-text field.
        for index, answer in enumerate(answers or [{"text": ""}]):
            text = answer["text"]
            if not isinstance(text, str):
                raise ValueError("captured assistant answer must be text")
            captured.append({"answer": text, "attempt_id": attempt["attempt_id"], "answer_index": index,
                             "final_attempt": attempt["attempt_id"] == run["final_attempt_id"],
                             "execution_status": attempt.get("execution_status"),
                             "answer_status": attempt.get("answer_status"),
                             "process_status": attempt.get("process_status"),
                             "process_problems": attempt.get("process_problems")})
    return captured


def _control_rows(run, source):
    """One deterministic control row per attempt; historical runs carry no control, so unscored."""
    attempts = run.get("attempt_records")
    if attempts is None:
        return [{"source": source, "case": run["case"], "rep": run.get("rep"), "attempt_id": None,
                 "final_attempt": True, "execution_status": None, "control_kind": None,
                 "control_correct": None, "control_evidence_class": None}]
    rows = []
    for attempt in attempts:
        control = attempt.get("control")
        control = control if isinstance(control, dict) else {}
        correct = control.get("correct")
        rows.append({"source": source, "case": run["case"], "rep": run.get("rep"), "attempt_id": attempt.get("attempt_id"),
                     "final_attempt": attempt.get("attempt_id") == run.get("final_attempt_id"),
                     "execution_status": attempt.get("execution_status"), "control_kind": control.get("kind"),
                     "control_correct": correct if type(correct) is bool else None,
                     "control_evidence_class": control.get("evidence_class")})
    return rows


def _execution_record(meta, runs):
    """Retain execution accounting once, never add grading or duplicate it across answers."""
    retained_attempts = bool(runs) and all("attempt_records" in run for run in runs)
    execution = {"source_schema": meta.get("schema"), "cost_ledger": meta.get("cost_ledger"),
                 "metadata": {field: value for field, value in meta.items() if field not in ("cost_ledger", "summary", "grading_cost")},
                 "runs": [{"run_index": n, "case": run["case"], "rep": run.get("rep"),
                           "final_attempt_id": run.get("final_attempt_id"),
                           "total_secs": run.get("total_secs"), "final_attempt_secs": run.get("final_attempt_secs"),
                           "attempts": [{field: attempt.get(field) for field in (
                               "attempt_id", "execution_status", "answer_status", "process_status", "process_problems",
                               "strategy_adherence", "secs", "raw_path")}
                               for attempt in run.get("attempt_records", [])]}
                          for n, run in enumerate(runs)],
                 "historical_outer_only": meta.get("schema") not in CURRENT_EXECUTION_SCHEMAS and not retained_attempts}
    if "cost_ledger" not in meta:
        # Partial files lack the full-file deduplicated ledger; keep one original run ledger.
        execution["run_cost_ledgers"] = {str(n): run.get("cost_ledger", run.get("accounting")) for n, run in enumerate(runs)}
    if execution["historical_outer_only"]:
        execution["historical_outer_usage"] = {str(n): run.get("usage") for n, run in enumerate(runs)}
        execution["total_cost_usd"] = None
    return execution


def _print_table(result, target):
    print(f"{result['results']} -> {target}")
    for case, summary in result["summary"]["per_case"].items():
        for schema, row in summary["by_key_schema"].items():
            metrics = ("factual_accuracy", "action_accuracy", "introduced_accuracy") if schema == "2" else ("accuracy",)
            values = " ".join(f"{field}={row[field]:.3f}" if row[field] is not None else f"{field}=-" for field in metrics)
            print(f"  {case} key_schema={schema} n={row['n']} {values} recall={row['recall']} precision={row['precision']} unresolved={row['unresolved']}")
    print(f"  grading overhead: {result['grading']['ledger_path']} (shared {result['grading']['ledger_id']})")


def _load_off_inventory(data):
    """An explicit audit of claims outside the key inventory, keyed by answer digest."""
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("off-inventory audit schema must be 1")
    _validate_provenance(document.get("provenance"), "off-inventory audit")
    answers = document.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("off-inventory audit needs an answers object")
    for digest, entry in answers.items():
        claims = entry.get("claims") if isinstance(entry, dict) else None
        if not isinstance(claims, list):
            raise ValueError(f"off-inventory audit {digest[:12]} needs a claims list")
        for claim in claims:
            if (not isinstance(claim, dict) or claim.get("verdict") not in ("true", "false", "unknown")
                    or not isinstance(claim.get("text"), str) or not claim["text"]):
                raise ValueError(f"off-inventory audit {digest[:12]} has an invalid claim")
    return document


def off_inventory_boundary(audit, answer_sha256):
    """Claims outside the key are unknown by default; they are neither credited nor penalized."""
    unknown = {"audited": False, "claims": None, "false_claims": None, "unknown_claims": None, "audit_provenance": None}
    if audit is None:
        return {**unknown, "status": "unknown",
                "reason": "no off-inventory audit supplied; off-inventory claims are not scored"}
    entry = audit["answers"].get(answer_sha256)
    if entry is None:
        return {**unknown, "status": "unaudited", "reason": "answer absent from the off-inventory audit"}
    verdicts = [claim["verdict"] for claim in entry["claims"]]
    return {"status": "audited", "audited": True, "claims": len(verdicts), "false_claims": verdicts.count("false"),
            "unknown_claims": verdicts.count("unknown"), "audit_provenance": audit["provenance"],
            "reason": "auditor-listed claims only; completeness of claim extraction is not established"}


CALIBRATION_ACTIONS = FACET_LABELS["action"]


def calibration_effect(records):
    """Effect of calibration on source-adjudicated truth, with reference agreement kept separate.

    Each record: id, stratum, source_truth (action or None), initial and final decisions
    (action), reference (action or None: a slower model or human, used for agreement only),
    overhead_usd (number or None), manual_resolution (bool). Records without source truth
    are unscored and excluded from effects, but still count toward overhead. Results describe
    this sample only; no population estimate is made without sampling weights.
    """
    ids = [record.get("id") for record in records]
    if any(not isinstance(rid, str) or not rid for rid in ids) or len(set(ids)) != len(ids):
        raise ValueError("calibration records need unique string ids")
    for record in records:
        for field in ("initial", "final"):
            if record.get(field) not in CALIBRATION_ACTIONS:
                raise ValueError(f"{record['id']}: {field} must be an action in {CALIBRATION_ACTIONS}")
        for field in ("source_truth", "reference"):
            if record.get(field) is not None and record[field] not in CALIBRATION_ACTIONS:
                raise ValueError(f"{record['id']}: {field} must be an action or null")
        if type(record.get("manual_resolution")) is not bool:
            raise ValueError(f"{record['id']}: manual_resolution must be boolean")
    scored = [record for record in records if record["source_truth"] is not None]

    def unsupported(decision, truth):
        return decision == "fix_now" and truth != "fix_now"

    def missed(decision, truth):
        return truth == "fix_now" and decision != "fix_now"

    referenced = [record for record in scored if record["reference"] is not None]
    overhead = [record["overhead_usd"] for record in records]
    strata = {}
    for record in scored:
        bucket = strata.setdefault(str(record["stratum"]), {"records": 0, "unsupported_before": 0, "unsupported_after": 0,
                                                             "missed_before": 0, "missed_after": 0})
        bucket["records"] += 1
        bucket["unsupported_before"] += unsupported(record["initial"], record["source_truth"])
        bucket["unsupported_after"] += unsupported(record["final"], record["source_truth"])
        bucket["missed_before"] += missed(record["initial"], record["source_truth"])
        bucket["missed_after"] += missed(record["final"], record["source_truth"])
    before = {"unsupported": sum(unsupported(r["initial"], r["source_truth"]) for r in scored),
              "missed": sum(missed(r["initial"], r["source_truth"]) for r in scored)}
    after = {"unsupported": sum(unsupported(r["final"], r["source_truth"]) for r in scored),
             "missed": sum(missed(r["final"], r["source_truth"]) for r in scored)}
    return {
        "schema": 1, "sample": {"records": len(records), "scored": len(scored), "unscored": len(records) - len(scored),
                                "strata": dict(sorted(strata.items()))},
        "effect_on_source_truth": {
            "unsupported_immediate_repairs": {"before": before["unsupported"], "after": after["unsupported"],
                                              "delta": after["unsupported"] - before["unsupported"]},
            "missed_defects": {"before": before["missed"], "after": after["missed"],
                               "delta": after["missed"] - before["missed"]},
            "changed_decisions": sum(r["initial"] != r["final"] for r in scored),
            "manual_resolutions": sum(r["manual_resolution"] for r in records)},
        "agreement_with_reference": {
            "referenced": len(referenced),
            "before": sum(r["initial"] == r["reference"] for r in referenced),
            "after": sum(r["final"] == r["reference"] for r in referenced),
            "agreement_not_correctness": True},
        "overhead_usd": {"total": None if any(o is None for o in overhead) else round(sum(overhead), 6),
                         "records_with_unknown_overhead": sum(o is None for o in overhead)},
        "scope": "sample_only_no_population_estimate_without_sampling_weights",
    }


def _as_graded(value):
    if isinstance(value, (str, os.PathLike)):
        return json.loads(Path(value).read_text())
    if isinstance(value, dict):
        return value
    raise TypeError("expected a graded result path or dict")


_COMPARED_OUTCOME_FIELDS = ("factual_accuracy", "action_accuracy", "introduced_accuracy", "factual_coverage",
                            "action_coverage", "introduced_coverage", "recall", "precision")
UNSAFE_FIELD = "unsupported_fix_now_claims"


def _per_case_metrics(result):
    """Case-level values of final-attempt outcomes. Repetitions and clones are not independent units."""
    per_case = {}
    for row in result["runs"]:
        if not row["final_attempt"] or row["key_schema"] != 2:
            continue
        values = {field: row.get(field) for field in _COMPARED_OUTCOME_FIELDS}
        values[UNSAFE_FIELD] = float(len(row["repair"]["unsupported_fix_now_claims"]))
        per_case.setdefault(row["case"], []).append(values)
    metrics = {field: {case: _mean(v[field] for v in values) for case, values in per_case.items()}
               for field in (*_COMPARED_OUTCOME_FIELDS, UNSAFE_FIELD)}
    controls = {}
    for row in result.get("control_runs", []):
        # Unscored controls have no correctness value; they are reported in summarize_controls.
        if row["final_attempt"] and row["control_correct"] is not None:
            controls.setdefault(row["case"], []).append(float(row["control_correct"]))
    metrics["control_accuracy"] = {case: _mean(values) for case, values in controls.items()}
    return metrics


def _identity(result):
    return {"results": result.get("results"), "input_sha256": result.get("input_sha256"),
            "grader_source_sha256": (result.get("identities") or {}).get("source_sha256"),
            "arm": result.get("arm"), "strategy": result.get("strategy"), "omp": result.get("omp")}


def _run_module():
    """Shared execution-side statistics. Loaded from the sibling file so one implementation serves both."""
    spec = importlib.util.spec_from_file_location("jevify_eval_run_shared", _GRADE_DIR / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GRADER_IDENTITY_FIELDS = ("source_sha256", "cases_sha256", "extraction_rubric_sha256", "fixture_sources",
                          "grader_sources", "scoring_revision", "historical_revision")


def _key_identities(result):
    """Per case, the key identity and binding its rows record (rows of one case share them)."""
    keys = {}
    for row in result.get("runs", []):
        keys.setdefault(row["case"], {"identity": row.get("key_identity"), "binding": row.get("key_binding")})
    return keys


def _identity_check(base, candidate):
    """Differing recorded identities are controlled differences; unrecorded ones prevent any claim."""
    differences, unrecorded = [], []
    base_ids, candidate_ids = base.get("identities") or {}, candidate.get("identities") or {}
    for field in GRADER_IDENTITY_FIELDS:
        left, right = base_ids.get(field), candidate_ids.get(field)
        if left is None or right is None:
            unrecorded.append(f"grader:{field}")
        elif left != right:
            differences.append(f"grader:{field}")
    base_keys, candidate_keys = _key_identities(base), _key_identities(candidate)
    for case in sorted(set(base_keys) & set(candidate_keys)):
        left, right = base_keys[case], candidate_keys[case]
        if left["identity"] is None or right["identity"] is None:
            unrecorded.append(f"key:{case}")
        elif left["identity"] != right["identity"]:
            differences.append(f"key:{case}")
        statuses = [(side["binding"] or {}).get("status") for side in (left, right)]
        if "changed" in statuses:
            differences.append(f"key:{case}:binding")
        elif any(status != "matched" for status in statuses):
            unrecorded.append(f"key:{case}:binding")
    return differences, unrecorded


def _contract_check(run, base, candidate):
    """Compatible needs one recorded contract that differs only in treatment, identical grader and keys.

    The contract SHA is display only: a one-variable comparison always changes it.
    """
    base_contract, candidate_contract = base.get("comparison_contract"), candidate.get("comparison_contract")
    if not isinstance(base_contract, dict) or not isinstance(candidate_contract, dict):
        check = {"compatible": False, "one_variable": False, "controlled_differences": [],
                 "treatment_differences": [], "unrecorded": ["comparison_contract"],
                 "reason": "comparison_contract unrecorded in one or both results"}
    else:
        check = dict(run.contract_differences(base_contract, candidate_contract))
    identity_differences, identity_unrecorded = _identity_check(base, candidate)
    reasons = [check["reason"]] if check.get("reason") else []
    if identity_unrecorded:
        reasons.append("identity unrecorded: " + ", ".join(identity_unrecorded))
    check["controlled_differences"] = [*check["controlled_differences"], *identity_differences]
    check["unrecorded"] = [*check["unrecorded"], *identity_unrecorded]
    check["reason"] = "; ".join(reasons) or None
    check["compatible"] = bool(check["compatible"]) and not check["controlled_differences"] and not check["unrecorded"]
    check["comparison_contract_sha256"] = {"base": base.get("comparison_contract_sha256"),
                                           "candidate": candidate.get("comparison_contract_sha256")}
    return check


def _final_schema2_rows(result):
    return [row for row in result.get("runs", []) if row.get("final_attempt") and row.get("key_schema") == 2]


def _pilot_side(result):
    """One side's observed outcomes. Quantities the run did not observe are null, never imputed."""
    rows = _final_schema2_rows(result)
    repairs = [row["repair"] for row in rows]
    predicted = sum(repair["predicted_defects"] for repair in repairs)
    planned = sum(repair["planned_defects"] for repair in repairs)
    false_repairs = sum(repair["fp"] for repair in repairs)
    misses = sum(repair["fn"] for repair in repairs)
    execution = result.get("execution") or {}
    runs = execution.get("runs") or []
    ledger = execution.get("cost_ledger") or {}
    entries = ledger.get("entries") or []
    observed = [entry for entry in entries if not entry.get("cost_unknown")]
    total = (ledger.get("totals") or {}).get("total") or {}
    return {
        "rows": len(rows),
        "false_repair_recommendations": false_repairs,
        "false_repair_rate": round(false_repairs / predicted, 3) if predicted else None,
        "misses": misses,
        "miss_rate": round(misses / planned, 3) if planned else None,
        "unsupported_fix_now_claims": sum(len(repair["unsupported_fix_now_claims"]) for repair in repairs),
        "review_burden": {"unresolved_items": sum(row["unresolved_items"] for row in rows),
                          "evidence_unresolved_items": sum(len(row["evidence_unresolved_items"]) for row in rows),
                          "manual_review_minutes": None},
        "latency_secs": {"runs": sum(run.get("total_secs") is not None for run in runs),
                         "mean_total": _mean([run.get("total_secs") for run in runs])},
        "observed_cost": {"observed_subtotal_usd": total.get("reported_subtotal") if entries else None,
                          "ledger_entries": len(entries),
                          "entries_with_observed_cost": len(observed) if entries else None,
                          "coverage": round(len(observed) / len(entries), 3) if entries else None,
                          "monetary_status": ledger.get("monetary_status"),
                          "billing_complete": False, "imputed": False},
    }


# Each failed gate is named with its reason; a failed gate is never silently passed.
_PILOT_COMPARABILITY = {
    "same_pilot_manifest": "results were not run under one pilot manifest",
    "one_method_treatment": "the contracts do not differ in exactly the strategy over identical controls",
    "method_candidate": "the candidate strategy has no method outcome label",
    "strategies_as_declared": "run strategies differ from the manifest comparison",
    "keys_and_rubric_bound": "graded answer keys or public rubric differ from the manifest",
    "frozen_before_runs": "the manifest was not frozen before each run started",
    "observed_model_and_thinking": "observed model or thinking level differs, or is unrecorded on a side",
}
_PILOT_EVIDENCE = {
    "heldout_phase": "the pilot phase is not heldout",
    "heldout_unexposed_cases": "a case is not an unexposed held-out case",
    "independent_gold": "gold is not independently attested for every case",
    "predeclared_thresholds": "no predeclared decision rule is bound to both results",
    "case_bindings_recorded": "a case's graded fixture and answer-key digests are not both recorded on each side",
    "distinct_case_evidence": "two case IDs bind the same answer key or fixture/key pair; aliases are not "
                              "independent cases",
}


def _keys_and_rubric_match(result, cases):
    keys = _key_identities(result)
    for cid, case in cases.items():
        identity = (keys.get(cid) or {}).get("identity") or {}
        public = identity.get("public_identity") or {}
        if identity.get("key_sha256") != case["key_sha256"] \
                or public.get("review/rubric.json") != case["public_rubric_sha256"]:
            return False
    return True


def _case_evidence_bindings(result, cases):
    """Per manifest case, the (fixture_sha256, key_sha256) its graded rows record; None when either is unrecorded."""
    keys = _key_identities(result)
    bindings = {}
    for cid in cases:
        identity = (keys.get(cid) or {}).get("identity") or {}
        fixture, key = identity.get("fixture_sha256"), identity.get("key_sha256")
        bindings[cid] = None if fixture is None or key is None else (fixture, key)
    return bindings


def _distinct_case_evidence(results, cases):
    """Whether every case ID is backed by its own answer key, on the manifest and on each side's rows.

    Two case IDs over one underlying answer key are one piece of evidence, never two independent cases; a shared
    key digest therefore also covers a repeated (fixture_sha256, key_sha256) pair. Unrecorded bindings are not
    inferred here; the case_bindings_recorded gate withholds on them.
    """
    bound = [case["key_sha256"] for case in cases.values()]
    if len(set(bound)) != len(bound):
        return False
    for result in results:
        keys = [binding[1] for binding in _case_evidence_bindings(result, cases).values() if binding is not None]
        if len(set(keys)) != len(keys):
            return False
    return True


def _observed_identity(result):
    """Models and thinking levels the sessions actually reported; None when either is unrecorded or empty."""
    metadata = (result.get("execution") or {}).get("metadata") or {}
    models, thinking = metadata.get("models_seen"), metadata.get("thinking_seen")
    if not isinstance(models, list) or not isinstance(thinking, list) or not models or not thinking:
        return None
    return sorted(models), sorted(thinking)


def _pilot_decision(run, base, candidate, contract, stats, reversed_stats, predeclared_matched):
    """Method recommendation: a use_* outcome only when every comparability and evidence gate passes.

    stats applies the predeclared rule with the candidate as challenger; reversed_stats applies the same rule with
    the baseline as challenger. The candidate is recommended only when stats supports it, and a baseline method only
    when reversed_stats supports it. A contradicted candidate alone never names the baseline: it can lose on safety
    while the baseline is much less correct.
    """
    base_pilot = (base.get("run_manifest") or {}).get("pilot")
    candidate_pilot = (candidate.get("run_manifest") or {}).get("pilot")
    if base_pilot is None or candidate_pilot is None:
        return {"verdict": "withheld", "strategy": None, "stats_verdict": stats["verdict"],
                "reverse_stats_verdict": reversed_stats["verdict"], "gates": {},
                "reasons": ["no pilot manifest is recorded on both results"], "attestation": None}
    declared, cases = base_pilot["comparison"], base_pilot["cases"]
    comparability = {
        "same_pilot_manifest": base_pilot["sha256"] == candidate_pilot["sha256"],
        "one_method_treatment": bool(contract["compatible"] and contract["one_variable"]
                                     and contract["treatment_differences"] == ["strategy"]),
        "method_candidate": declared["candidate"] in run.METHOD_OUTCOMES,
        "strategies_as_declared": base.get("strategy") == declared["base"]
                                  and candidate.get("strategy") == declared["candidate"],
        "keys_and_rubric_bound": _keys_and_rubric_match(base, cases) and _keys_and_rubric_match(candidate, cases),
        "frozen_before_runs": all(side["frozen_at"] < side["run_started_at"]
                                  for side in (base_pilot, candidate_pilot)),
        "observed_model_and_thinking": _observed_identity(base) is not None
                                       and _observed_identity(base) == _observed_identity(candidate),
    }
    evidence = {
        "heldout_phase": base_pilot["phase"] == "heldout",
        "heldout_unexposed_cases": all(case["split"] == "heldout" and case["exposure"] == "unexposed"
                                       for case in cases.values()),
        "independent_gold": all(case["gold"]["status"] == run.PILOT_INDEPENDENT_GOLD for case in cases.values()),
        "predeclared_thresholds": predeclared_matched and base_pilot["thresholds_sha256"] is not None,
        "case_bindings_recorded": all(binding is not None for result in (base, candidate)
                                      for binding in _case_evidence_bindings(result, cases).values()),
        "distinct_case_evidence": _distinct_case_evidence((base, candidate), cases),
    }
    failed_comparability = [name for name, ok in comparability.items() if not ok]
    failed_evidence = [name for name, ok in evidence.items() if not ok]
    recommended = None
    if failed_comparability:
        verdict, reasons = "not_comparable", [_PILOT_COMPARABILITY[name] for name in failed_comparability]
    elif failed_evidence:
        verdict, reasons = "withheld", [_PILOT_EVIDENCE[name] for name in failed_evidence]
    elif stats["verdict"] == "supports_candidate":
        verdict, recommended = run.METHOD_OUTCOMES[declared["candidate"]], declared["candidate"]
        reasons = []
    elif reversed_stats["verdict"] == "supports_candidate" and declared["base"] in run.METHOD_OUTCOMES:
        # The same predeclared rule, applied with the baseline as challenger, clears for the baseline method.
        verdict, recommended = run.METHOD_OUTCOMES[declared["base"]], declared["base"]
        reasons = [f"baseline {declared['base']} is supported by the predeclared rule applied in reverse"] \
            + list(stats["reasons"])
    elif stats["verdict"] == "contradicts_candidate":
        verdict = "contradicts_candidate"
        if declared["base"] in run.METHOD_OUTCOMES:
            # Losing to the baseline does not show the baseline clears the rule; no method outcome is inferred.
            basis = [f"baseline {declared['base']} is not supported by the predeclared rule applied in reverse: "
                     + ("; ".join(reversed_stats["reasons"]) or reversed_stats["verdict"])]
        else:
            basis = ["the baseline is auto; no method outcome is inferred"]
        reasons = basis + list(stats["reasons"])
    else:
        verdict, reasons = "inconclusive", list(stats["reasons"]) or ["the predeclared rule did not clear"]
    return {"verdict": verdict, "strategy": recommended, "candidate": declared["candidate"],
            "baseline": declared["base"], "stats_verdict": stats["verdict"],
            "reverse_stats_verdict": reversed_stats["verdict"],
            "gates": {**comparability, **evidence}, "reasons": reasons,
            "pilot_sha256": base_pilot["sha256"], "attestation": run.PILOT_ATTESTATION}


def compare_graded(base, candidate):
    """Identity-checked, paired comparison of two graded results.

    Thresholds come only from the base run's predeclared manifest, and only when both runs
    carry the same predeclared digest. Otherwise the shared statistics withhold the
    recommendation, and the comparison is reported as post hoc.
    """
    base, candidate = _as_graded(base), _as_graded(candidate)
    run = _run_module()
    contract = _contract_check(run, base, candidate)
    base_metrics, candidate_metrics = _per_case_metrics(base), _per_case_metrics(candidate)
    base_pre = (base.get("run_manifest") or {}).get("predeclared_thresholds")
    candidate_pre = (candidate.get("run_manifest") or {}).get("predeclared_thresholds")
    matched = base_pre is not None and candidate_pre is not None and base_pre.get("sha256") == candidate_pre.get("sha256")
    predeclared = base_pre if matched else None
    # Thresholds are the base's predeclared object; a digest mismatch withholds via predeclared=None.
    thresholds = base_pre.get("thresholds") if base_pre is not None else None
    confidence = thresholds.get("confidence", 0.95) if thresholds else 0.95
    comparisons = {field: run.paired_case_comparison(base_metrics[field], candidate_metrics[field], confidence=confidence)
                   for field in base_metrics}
    # Descriptive interval outcome only. It is not a recommendation, and pilot evidence never gates it.
    statistical_result = run.threshold_recommendation(comparisons, thresholds, contract, predeclared)
    # The identical predeclared rule with roles swapped: the baseline as challenger over the candidate.
    reversed_comparisons = {field: run.paired_case_comparison(candidate_metrics[field], base_metrics[field],
                                                              confidence=confidence)
                            for field in base_metrics}
    reversed_result = run.threshold_recommendation(reversed_comparisons, thresholds, contract, predeclared)
    return {"schema": 1, "base": _identity(base), "candidate": _identity(candidate), "contract_check": contract,
            "comparisons": comparisons, "statistical_result": statistical_result,
            "reversed_statistical_result": reversed_result,
            "predeclared_thresholds_matched": matched, "post_hoc": predeclared is None,
            "observed_outcomes": {"base": _pilot_side(base), "candidate": _pilot_side(candidate)},
            "method_recommendation": _pilot_decision(run, base, candidate, contract, statistical_result,
                                                     reversed_result, matched)}


async def grade(results_path, cases_path, out_path=None, *, timeout=1800, concurrency=4,
                cache_mode="uncontrolled", ledger_path=None, launch=None, off_inventory_audit=None):
    """Grade all captured answers, including failed/method-bypassed attempts, with fixed keys.

    ``cache_mode`` records requested conditions. ``launch`` is the JSON descriptor that
    evals/grade_launch.py wrote into the grading cell; it is recorded verbatim, not verified
    here. Batch status costs are estimates; grading-session outer overhead remains unmeasured
    here. Each output references one companion ledger. Results, cases, fixture sources and an
    optional off-inventory audit are read once as bytes and bound by digest. Historical
    captures without a selected key identity remain unrecorded, not evidence of a match.
    """
    single = isinstance(results_path, (str, os.PathLike))
    paths = [Path(results_path)] if single else [Path(path) for path in results_path]
    if not paths or len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("grade requires distinct nonempty input paths")
    if out_path is not None and not single:
        raise ValueError("out_path needs a single results file")
    if timeout <= 0 or concurrency < 1:
        raise ValueError("timeout and concurrency must be positive")
    if cache_mode not in ("cold", "warm", "uncontrolled"):
        raise ValueError("cache_mode must be cold, warm, or uncontrolled")
    if launch is not None:
        json.dumps(launch, sort_keys=True, allow_nan=False)  # rejects non-JSON descriptors before any work
    audit_path = Path(off_inventory_audit) if off_inventory_audit is not None else None
    targets = [Path(out_path) if out_path is not None else path.with_suffix(".graded.json") for path in paths]
    if len({path.resolve() for path in targets}) != len(targets) or any(target.resolve() in {path.resolve() for path in paths} for target in targets):
        raise ValueError("grading output would overwrite an input or another output")
    ledger_id = uuid.uuid4().hex
    ledger_target = Path(ledger_path) if ledger_path is not None else targets[0].parent / f"grading-{ledger_id}.ledger.json"
    protected = [*paths, *targets, Path(cases_path), *([audit_path] if audit_path is not None else [])]
    if ledger_target.resolve() in {path.resolve() for path in protected}:
        raise ValueError("grading ledger would overwrite source or results")
    if ledger_target.exists():
        raise ValueError("grading ledger already exists; choose a fresh companion path")
    input_snapshots = {path: path.read_bytes() for path in paths}
    cases_snapshot = Path(cases_path).read_bytes()
    audit_snapshot = audit_path.read_bytes() if audit_path is not None else None
    fixture_snapshots = {name: (_GRADE_DIR / name).read_bytes() for name in FIXTURE_SOURCES}
    grader_snapshots = {name: (_GRADE_DIR / name).read_bytes() for name in GRADER_SOURCES}
    findings = _sibling_module("findings", grader_snapshots["findings.py"])
    snapshot_digests = {**{path: _bytes_digest(data) for path, data in input_snapshots.items()},
                        Path(cases_path): _bytes_digest(cases_snapshot),
                        **{_GRADE_DIR / name: _bytes_digest(data)
                           for name, data in {**fixture_snapshots, **grader_snapshots}.items()}}
    if audit_path is not None:
        snapshot_digests[audit_path] = _bytes_digest(audit_snapshot)
    input_hashes = {str(path): _bytes_digest(snapshot) for path, snapshot in input_snapshots.items()}
    off_inventory = _load_off_inventory(audit_snapshot) if audit_snapshot is not None else None
    grader_identity = {"source_sha256": _file_digest(__file__), "cases_sha256": _bytes_digest(cases_snapshot),
                       "extraction_rubric_sha256": {"1": _digest(HISTORICAL_QUESTIONS)},
                       "fixture_sources": {name: _bytes_digest(data) for name, data in fixture_snapshots.items()},
                       "grader_sources": {name: _bytes_digest(data) for name, data in grader_snapshots.items()},
                       "off_inventory_audit_sha256": None if audit_snapshot is None else _bytes_digest(audit_snapshot),
                       "scoring_revision": SCORING_REVISION, "historical_revision": HISTORICAL_REVISION}
    cases_doc = json.loads(cases_snapshot)
    cases = {case["id"]: case for case in cases_doc["cases"]}
    if len(cases) != len(cases_doc["cases"]):
        raise ValueError("duplicate case ids")
    fixture_specs = [(case.get("dataset", "billing"), case.get("scale", 1))
                     for case in cases.values() if case.get("key") is not None]
    bundles = _fixture_keys(fixture_specs, sources={**fixture_snapshots, **grader_snapshots})
    identities, frozen_keys = {}, {}
    for cid, case in cases.items():
        if case.get("key") is None:
            continue
        bundle = bundles[(case.get("dataset", "billing"), case.get("scale", 1))]
        if case["key"] not in bundle["keys"]:
            raise ValueError(f"{cases_path}: no answer key {case['key']!r}")
        key = bundle["keys"][case["key"]]
        schema = _key_schema(key)
        frozen_keys[cid] = key
        identities[cid] = {"key_schema": schema, "dataset": case.get("dataset", "billing"), "scale": case.get("scale", 1),
                           "key": case["key"], "key_sha256": _digest(key), "public_identity": bundle["public_identity"],
                           "private_keys_sha256": bundle.get("private_keys_sha256"),
                           "fixture_sha256": bundle.get("fixture_sha256"),
                           "source_revision": bundle["snapshot"]["head"] if schema == 2 else None,
                           "base_revision": bundle["snapshot"]["base"] if schema == 2 else None,
                           "output_contract_sha256": bundle["output_contract_sha256"] if schema == 2 else None,
                           "semantics": SCORING_REVISION if schema == 2 else HISTORICAL_REVISION}
    states, slots = {1: {}}, {1: {}}
    files = []
    for path in paths:
        meta, runs = _load_runs(path, captured_bytes=input_snapshots[path])
        missing = sorted({row["case"] for row in runs} - set(cases))
        if missing:
            raise ValueError(f"{path}: cases absent from {cases_path}: {missing}")
        if meta.get("schema") in CURRENT_EXECUTION_SCHEMAS and any(run.get("attempt_records") is None for run in runs):
            raise ValueError("current execution omitted retained attempts")
        key_bindings = {}
        for cid in {run["case"] for run in runs} & set(identities):
            captured_identity = (meta.get("fixture_identities") or {}).get(cid) or {}
            grading_identity = identities[cid]
            selected_key_recorded = any(captured_identity.get(field) is not None for field in ("key", "key_sha256"))
            key_bindings[cid] = {"captured": captured_identity,
                                 "status": "matched" if selected_key_recorded else "unrecorded"}
            for field in ("key", "dataset", "scale", "key_sha256", "fixture_sha256", "private_keys_sha256",
                          "source_revision", "output_contract_sha256"):
                captured_value, grading_value = captured_identity.get(field), grading_identity.get(field)
                if captured_value is not None and grading_value is not None and captured_value != grading_value:
                    key_bindings[cid]["status"] = "changed"
            captured_rubric = captured_identity.get("public_rubric_sha256")
            grading_rubric = grading_identity["public_identity"].get("review/rubric.json")
            if captured_rubric and grading_rubric and captured_rubric != grading_rubric:
                key_bindings[cid]["status"] = "changed"
            if key_bindings[cid]["status"] == "changed":
                print(f"  {path}: {cid} is regraded under changed fixed keys/evidence; identities are retained")
        planned, controls = [], []
        for run_index, run in enumerate(runs):
            case = cases[run["case"]]
            if case.get("control") is not None:
                controls.extend(_control_rows(run, str(path)))
            if case.get("key") is None:
                continue
            bundle = bundles[(case.get("dataset", "billing"), case.get("scale", 1))]
            key = bundle["keys"][case["key"]]
            schema = identities[run["case"]]["key_schema"]
            for capture in _captured_answers(run):
                answer = capture["answer"].strip()
                row = {"case": run["case"], "rep": run.get("rep"), "key": case["key"], "key_schema": schema,
                       "empty": not answer, "final_attempt": capture["final_attempt"],
                       "execution_status": capture["execution_status"],
                       "answer_status": capture["answer_status"], "process_status": capture["process_status"],
                       "process_problems": capture["process_problems"],
                       "execution_ref": {"run_index": run_index, "attempt_id": capture["attempt_id"],
                                         "answer_index": capture["answer_index"]},
                       "answer_sha256": _digest(answer), "key_identity": identities[run["case"]],
                       "key_binding": key_bindings[run["case"]],
                       "verdicts": {}, "errors": {}, "attribution": {}, "extraction_inputs": {}, "extraction_provenance": {}}
                if schema == 2:
                    # Structured findings are validated deterministically from the answer and the frozen snapshot.
                    record = findings.validate_findings(answer, bundle["snapshot"], frozenset(item["id"] for item in key["items"])) if answer else None
                    row["findings"] = None if record is None else {**record, "rendered": findings.render_findings(record)}
                    for item in key["items"]:
                        claim = record["claims"][item["id"]] if record is not None else {"status": "omitted"}
                        if claim["status"] == "asserted":
                            row["verdicts"][item["id"]] = {facet: claim[facet] for facet in findings.LABELS}
                            row["attribution"][item["id"]] = {"status": "asserted", "reason": None}
                        elif claim["status"] == "omitted":
                            row["verdicts"][item["id"]] = {facet: "omitted" for facet in FACET_LABELS}
                        else:
                            # Unresolved (rejected, duplicated or conflicting): never credited, and not a judge failure.
                            row["verdicts"][item["id"]] = {}
                            row["attribution"][item["id"]] = {"status": "unattributed", "reason": claim["reason"]}
                    planned.append((row, key))
                    continue
                for item in key["items"]:
                    if not answer:
                        row["verdicts"][item["id"]] = "not_mentioned"
                        continue
                    state = {"answer": answer, "id": item["id"], "item": item["desc"], "flag_label": key["flag_label"]}
                    sid = str(len(states[1]))
                    states[1][sid] = state
                    slots[1][sid] = (row, item["id"])
                    row["extraction_inputs"][item["id"]] = _digest(state)
                planned.append((row, key))
        files.append((path, meta, runs, planned, controls))
    batch_records = []
    provenance_by_state = {}
    deadline = time.monotonic() + timeout
    if states[1]:
        labels, failures, batch = await _judge(states[1], HISTORICAL_QUESTIONS, max(0, deadline - time.monotonic()), concurrency)
        for sid, item in batch.pop("provenance", {}).items():
            provenance_by_state[sid] = item
        batch_records.append(batch)
        for sid, (row, iid) in slots[1].items():
            row["verdicts"][iid] = labels.get(sid, {}).get("verdict")
            row["extraction_provenance"][iid] = provenance_by_state.get(sid, _unknown_provenance("not_submitted"))
            if sid in failures:
                row["errors"][iid] = failures[sid]
    changed = _changed_since_snapshot(snapshot_digests)
    if changed:
        print(f"  inputs changed during grading; results remain bound to snapshot bytes: {changed}")
    # Even an empty grading plan has unmeasured session overhead; absence is not free billing.
    ledger = {"schema": 1, "id": ledger_id, "scope": "shared_grading_overhead", "inputs": [str(path) for path in paths],
              "input_sha256": input_hashes, "identities": grader_identity,
              "key_identities": identities, "frozen_keys": frozen_keys,
              "extraction_rubrics": {"1": HISTORICAL_QUESTIONS},
              "run_config": {"timeout_seconds": timeout, "concurrency": concurrency, "cache_mode": cache_mode},
              "inputs_changed_during_grading": changed,
              "off_inventory_audit": None if audit_path is None else {"path": str(audit_path), "sha256": grader_identity["off_inventory_audit_sha256"]},
              "batches": batch_records,
              "batch_cost_estimate_usd": sum(record["cost_usd"] for record in batch_records) if all(record["cost_usd"] is not None for record in batch_records) else None,
              "session_outer_cost_usd": None, "total_cost_usd": None, "billing_complete": False,
              "cache": {"requested": cache_mode, "effective": "unverified", "launch": launch,
                        "observed": [record["cache"] for record in batch_records if record.get("cache")]}}
    with ledger_target.open("x") as handle:
        handle.write(json.dumps(ledger, indent=1) + "\n")
    results = []
    for (path, meta, execution_runs, planned, control_rows), target in zip(files, targets):
        graded = []
        for row, key in planned:
            if row["key_schema"] == 2:
                # Deterministic attribution rejections are unresolved, not extraction failures; read from row attribution.
                rejected = [iid for iid, record in row["attribution"].items() if record["status"] == "unattributed"]
                score = score_facets(key["items"], row["verdicts"], row["errors"], rejected)
            else:
                score = score_items({item["id"]: item["flag"] for item in key["items"]}, row["verdicts"], row["errors"])
            row.update(score)
            row["judge_errors"] = len(row["errors"])
            row["off_inventory"] = off_inventory_boundary(off_inventory, row["answer_sha256"])
            graded.append(row)
        per_case = {}
        for row in graded:
            per_case.setdefault(row["case"], []).append(row)
        result = {"schema": 2, "results": str(path), "input_sha256": ledger["input_sha256"][str(path)],
                  "cases": str(cases_path), "identities": grader_identity,
                  **{field: meta.get(field) for field in ("arm", "strategy", "omp", "thinking", "model", "model_requested", "thinking_requested")},
                  "comparison_contract": meta.get("comparison_contract"),
                  "comparison_contract_sha256": meta.get("comparison_contract_sha256"),
                  "run_manifest": meta.get("run_manifest"),
                  "execution": _execution_record(meta, execution_runs),
                  "grading": {"ledger_id": ledger_id, "ledger_path": str(ledger_target)},
                  "summary": {"overall": summarize_graded(graded),
                              "final_attempt": summarize_graded([row for row in graded if row["final_attempt"]]),
                              "per_case": {case: summarize_graded(rows) for case, rows in sorted(per_case.items())},
                              "final_per_case": {case: summarize_graded([row for row in rows if row["final_attempt"]])
                                                 for case, rows in sorted(per_case.items())}},
                  "controls": summarize_controls(control_rows),
                  "off_inventory": summarize_off_inventory(graded),
                  "runs": graded, "control_runs": control_rows}
        target.write_text(json.dumps(result, indent=1) + "\n")
        _print_table(result, target)
        results.append(result)
    return results[0] if single else results
