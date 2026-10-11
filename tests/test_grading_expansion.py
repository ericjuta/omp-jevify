"""Behavioral contracts for grading expansions: route provenance, extractor validation,
calibration effects, off-inventory boundaries, controls, immutable inputs, cache-isolated
launch evidence, and paired comparison (evals/grade.py, evals/grade_launch.py)."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"jevify_grading_{name}", ROOT / "evals" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grade = _load("grade")
run = _load("run")
launch = _load("grade_launch")

GOOD_ANSWERS = {"factual": {"choice": "supported"}, "action": {"choice": "fix_now"}, "introduced": {"choice": "yes"}}
SYNTHETIC = {"kind": "source_adjudicated_synthetic", "human_adjudicated": False, "basis": "unit test"}
EXTERNAL_HUMAN = {"kind": "external_human", "human_adjudicated": True, "adjudicator": "reviewer-1",
                  "adjudicated_at": "2026-10-11", "basis": "blind review"}


def _gold(factual="supported", action="fix_now", introduced="yes"):
    return {"factual": factual, "action": action, "introduced": introduced}


def _record(rid, category, gold, *, answer="answer text", provenance=SYNTHETIC):
    claim = "C" + rid.upper()
    return {"id": rid, "claim": claim, "category": category, "item": f"review claim {claim} (unit): item {rid}",
            "answer": answer, "gold": gold, "gold_basis": "unit test", "provenance": provenance}


def _grader_ids():
    return {"source_sha256": "g", "cases_sha256": "c", "extraction_rubric_sha256": {"1": "r1", "2": "r2"},
            "fixture_sources": {"fixture.py": "f1", "refund_retry_fixture.py": "f2"},
            "scoring_revision": grade.SCORING_REVISION, "historical_revision": grade.HISTORICAL_REVISION}


def _batch_record(count):
    return {"id": "b", "kind": "judgment_batch", "items": count, "model": "m", "cost_usd": None,
            "cost_basis": "batch_status_estimate", "billing_complete": False, "cleanup_errors": [], "cache": {}}


class _Item:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _StreamBatch:
    """A completed judge_batch handle that streams item objects, as the host helper does."""
    id = "stream-batch"

    def __init__(self, items, answers):
        self._items, self._answers = items, answers

    async def drain_iter(self, seconds):
        for identifier, item in self._items.items():
            yield identifier, item

    async def drain(self, seconds):
        raise AssertionError("a streaming handle must not fall back to drain")

    def status(self):
        return {"done": len(self._answers), "running": False, "cost": 0.5, "model": "judge-x", "cache": "miss"}

    def results(self):
        return self._answers

    def failed(self):
        return {}

    def cancel(self):
        pass

    def close(self):
        pass


def _known_route(**overrides):
    fields = {"selected_model": "m-1", "primary_model": "m-1", "response_model": "m-1", "fallback": False,
              "cache": "hit", "questions": 3, "cached_questions": 3}
    fields.update(overrides)
    return fields


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_item_routes_are_kept_and_inconsistent_fields_stay_unknown(self):
        states = {"a": {"id": "a"}, "b": {"id": "b"}, "c": {"id": "c"}}
        answers = {sid: dict(GOOD_ANSWERS) for sid in states}
        items = {
            "a": _Item(**_known_route()),
            "b": _Item(**_known_route(fallback=True)),  # fallback claimed although selected == primary
            "c": _Item(**_known_route(cache=None)),      # cache not reported
        }
        batch = _StreamBatch(items, answers)
        with mock.patch.object(grade, "judge_batch", return_value=batch, create=True):
            _, _, record = await grade._judge(states, grade.FACET_QUESTIONS, 10, 1)
        provenance = record["provenance"]
        self.assertFalse(provenance["a"]["provenance_unknown"])
        self.assertEqual(provenance["a"]["selected_model"], "m-1")
        self.assertEqual(provenance["a"]["source"], "host_item")
        self.assertTrue(provenance["b"]["provenance_unknown"])
        self.assertTrue(provenance["c"]["provenance_unknown"])
        self.assertIsNone(provenance["c"]["cache"])
        self.assertEqual(record["provenance_summary"], {"items": 3, "known": 1, "unknown": 2, "item_stream": True})

    async def test_handle_without_item_stream_reports_unknown_and_never_infers_from_status(self):
        batch = mock.Mock()
        batch.id = "plain-batch"
        batch.drain = mock.AsyncMock()
        batch.status.return_value = {"done": 1, "running": False, "cost": 0.1, "model": "judge-x", "cache": "hit"}
        batch.results.return_value = {"slot": dict(GOOD_ANSWERS)}
        batch.failed.return_value = {}
        states = {"slot": {"id": "x", "item": "Synthetic claim", "answer": "text"}}
        with mock.patch.object(grade, "judge_batch", return_value=batch, create=True):
            labels, errors, record = await grade._judge(states, grade.FACET_QUESTIONS, 10, 1)
        self.assertEqual(labels["slot"]["factual"], "supported")
        self.assertEqual(record["provenance"]["slot"]["source"], "host_item_stream_unavailable")
        self.assertTrue(record["provenance"]["slot"]["provenance_unknown"])
        self.assertIsNone(record["provenance"]["slot"]["selected_model"])
        self.assertEqual(record["provenance_summary"]["known"], 0)


class ValidateExtractorTests(unittest.TestCase):
    def _corpus(self, records, provenance=SYNTHETIC):
        return {"schema": 1, "provenance": provenance, "records": records}

    def test_confusion_denominators_and_high_impact_false_repair_are_exact(self):
        records = [
            _record("r1", "exact_id", _gold()),
            _record("r2", "exact_id", _gold("supported", "no_action", "no")),
            _record("r3", "omission", _gold()),
        ]
        extractions = {
            "r1": _gold(),
            "r2": {"factual": "supported", "action": "fix_now", "introduced": "no"},
            # r3 has no extraction: every facet is an unresolved error.
        }
        report = grade.validate_extractor(self._corpus(records), extractions)
        self.assertEqual(report["facets"]["factual"]["correct"], 2)
        self.assertEqual(report["facets"]["action"]["correct"], 1)
        self.assertAlmostEqual(report["facets"]["action"]["accuracy"], 0.333, places=3)
        self.assertEqual(report["facets"]["action"]["unresolved"], 1)
        self.assertEqual(report["facets"]["action"]["confusion"]["no_action"]["fix_now"], 1)
        self.assertEqual(report["facets"]["action"]["confusion"]["fix_now"]["unresolved"], 1)
        self.assertEqual(report["false_repairs"], [{"id": "r2", "gold_action": "no_action", "high_impact": True}])
        self.assertEqual(report["high_impact_false_repairs"], ["r2"])
        self.assertEqual(report["missed_repairs"], [{"id": "r3", "predicted_action": "unresolved"}])
        self.assertEqual(report["unresolved_records"], ["r3"])
        self.assertEqual(report["categories"]["exact_id"]["records"], 2)
        self.assertEqual(report["categories"]["exact_id"]["action"]["correct"], 1)
        self.assertFalse(report["quality_claim"])

    def test_synthetic_labels_never_count_as_human_adjudication(self):
        records = [_record("r1", "exact_id", _gold())]
        report = grade.validate_extractor(self._corpus(records), {"r1": _gold()})
        self.assertEqual(report["provenance"]["human_adjudicated_records"], 0)
        self.assertEqual(report["provenance"]["records_by_kind"], {"source_adjudicated_synthetic": 1})

    def test_external_human_needs_a_named_adjudicator_and_is_counted_only_when_declared(self):
        unnamed = {**EXTERNAL_HUMAN, "adjudicator": ""}
        with self.assertRaisesRegex(ValueError, "adjudicator"):
            grade.validate_extractor(self._corpus([_record("r1", "exact_id", _gold(), provenance=unnamed)], unnamed),
                                     {"r1": _gold()})
        mislabelled = {**SYNTHETIC, "human_adjudicated": True}
        with self.assertRaisesRegex(ValueError, "human_adjudicated"):
            grade.validate_extractor(self._corpus([_record("r1", "exact_id", _gold(), provenance=mislabelled)]),
                                     {"r1": _gold()})
        report = grade.validate_extractor(
            self._corpus([_record("r1", "exact_id", _gold(), provenance=EXTERNAL_HUMAN)], EXTERNAL_HUMAN),
            {"r1": _gold()})
        self.assertEqual(report["provenance"]["human_adjudicated_records"], 1)

    def test_host_validation_sends_the_claim_id_and_keys_each_record_uniquely(self):
        corpus_path = ROOT / "evals" / "extractor-corpus.json"
        corpus = json.loads(corpus_path.read_text())
        records = {record["id"]: record for record in corpus["records"]}
        gold = {rid: record["gold"] for rid, record in records.items()}
        sent = {}

        async def judge(states, questions, timeout, concurrency):
            sent.update(states)
            return {sid: dict(gold[sid]) for sid in states}, {}, _batch_record(len(states))

        with mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)):
            report = asyncio.run(grade.validate_extractor_host(corpus_path))
        self.assertEqual(set(sent), set(records))
        for rid, state in sent.items():
            self.assertEqual(state["id"], records[rid]["claim"], rid)
            self.assertEqual(state["item"], records[rid]["item"], rid)
        self.assertEqual(sent["r01"]["id"], sent["r04"]["id"])  # one claim, two records, distinct keys
        self.assertEqual(report["facets"]["action"]["accuracy"], 1.0)

    def test_host_validation_scores_judged_labels_and_keeps_extraction_provenance(self):
        corpus_path = ROOT / "evals" / "extractor-corpus.json"
        corpus = json.loads(corpus_path.read_text())
        labels = {record["id"]: record["gold"] for record in corpus["records"]}
        provenance = {rid: {"provenance_unknown": True, "source": "host_item_stream_unavailable"} for rid in labels}
        batch = {"id": "b", "kind": "judgment_batch", "items": len(labels), "provenance": provenance,
                 "cost_usd": None, "cost_basis": "batch_status_estimate", "billing_complete": False,
                 "cleanup_errors": [], "cache": {}, "model": "judge-x"}
        with mock.patch.object(grade, "_judge", mock.AsyncMock(return_value=(labels, {}, batch))):
            report = asyncio.run(grade.validate_extractor_host(corpus_path))
        self.assertEqual(report["facets"]["action"]["accuracy"], 1.0)
        self.assertEqual(report["false_repairs"], [])
        self.assertEqual(report["extraction"]["provenance"]["r01"]["source"], "host_item_stream_unavailable")
        self.assertNotIn("provenance", report["extraction"]["batch"])


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads((ROOT / "evals" / "extractor-corpus.json").read_text())

    def test_a_claim_absent_from_its_item_is_rejected(self):
        bad = json.loads(json.dumps(self.corpus))
        bad["records"][0]["claim"] = "C99"
        with self.assertRaisesRegex(ValueError, "claim"):
            grade._validate_corpus(bad)


class CalibrationTests(unittest.TestCase):
    def _records(self):
        return [
            {"id": "a", "stratum": "s1", "source_truth": "no_action", "initial": "fix_now", "final": "no_action",
             "reference": "fix_now", "overhead_usd": 0.1, "manual_resolution": False},
            {"id": "b", "stratum": "s2", "source_truth": "fix_now", "initial": "no_action", "final": "no_action",
             "reference": "no_action", "overhead_usd": 0.1, "manual_resolution": True},
            {"id": "c", "stratum": "s1", "source_truth": "defer", "initial": "defer", "final": "defer",
             "reference": "fix_now", "overhead_usd": 0.1, "manual_resolution": False},
        ]

    def test_source_truth_effect_is_reported_apart_from_reference_agreement(self):
        report = grade.calibration_effect(self._records())
        effect = report["effect_on_source_truth"]
        self.assertEqual(effect["unsupported_immediate_repairs"], {"before": 1, "after": 0, "delta": -1})
        self.assertEqual(effect["missed_defects"], {"before": 1, "after": 1, "delta": 0})
        self.assertEqual(effect["changed_decisions"], 1)
        self.assertEqual(effect["manual_resolutions"], 1)
        agreement = report["agreement_with_reference"]
        self.assertEqual((agreement["before"], agreement["after"], agreement["referenced"]), (2, 1, 3))
        self.assertTrue(agreement["agreement_not_correctness"])
        self.assertLess(agreement["after"], agreement["before"])  # agreement fell while truth improved
        self.assertEqual(report["overhead_usd"], {"total": 0.3, "records_with_unknown_overhead": 0})
        self.assertEqual(report["sample"]["strata"]["s1"]["records"], 2)

    def test_records_without_source_truth_are_unscored_but_keep_unknown_overhead(self):
        records = self._records()[:1] + [{"id": "z", "stratum": "s1", "source_truth": None, "initial": "fix_now",
                                          "final": "fix_now", "reference": None, "overhead_usd": None,
                                          "manual_resolution": False}]
        report = grade.calibration_effect(records)
        self.assertEqual(report["sample"]["scored"], 1)
        self.assertEqual(report["sample"]["unscored"], 1)
        self.assertIsNone(report["overhead_usd"]["total"])
        self.assertEqual(report["overhead_usd"]["records_with_unknown_overhead"], 1)

    def test_invalid_decisions_are_rejected(self):
        records = self._records()
        records[0]["final"] = "fix-now"
        with self.assertRaisesRegex(ValueError, "final"):
            grade.calibration_effect(records)


class OffInventoryTests(unittest.TestCase):
    def test_claims_outside_the_inventory_are_unknown_without_an_audit(self):
        boundary = grade.off_inventory_boundary(None, "any-digest")
        self.assertEqual(boundary["status"], "unknown")
        self.assertFalse(boundary["audited"])
        self.assertIsNone(boundary["false_claims"])

    def test_audited_answers_count_verdicts_and_unaudited_answers_stay_unknown(self):
        audit = grade._load_off_inventory(json.dumps({
            "schema": 1, "provenance": SYNTHETIC,
            "answers": {"sha": {"claims": [
                {"text": "t1", "verdict": "false"}, {"text": "t2", "verdict": "true"}, {"text": "t3", "verdict": "unknown"}]}},
        }).encode())
        audited = grade.off_inventory_boundary(audit, "sha")
        self.assertEqual((audited["status"], audited["claims"], audited["false_claims"], audited["unknown_claims"]),
                         ("audited", 3, 1, 1))
        self.assertEqual(grade.off_inventory_boundary(audit, "other")["status"], "unaudited")

    def test_invalid_verdicts_are_rejected(self):
        bad = json.dumps({"schema": 1, "provenance": SYNTHETIC,
                          "answers": {"sha": {"claims": [{"text": "t", "verdict": "maybe"}]}}}).encode()
        with self.assertRaisesRegex(ValueError, "invalid claim"):
            grade._load_off_inventory(bad)


class ControlSummaryTests(unittest.TestCase):
    def test_unscored_controls_stay_in_the_denominator_of_control_accuracy(self):
        rows = [{"case": "count", "final_attempt": True, "control_correct": value} for value in (True, None, False)]
        summary = grade.summarize_controls(rows)
        self.assertEqual(summary["overall"], {"n": 3, "scored": 2, "correct": 1, "control_accuracy": 0.333})
        self.assertEqual(summary["final_attempt"]["n"], 3)
        self.assertEqual(summary["per_case"]["count"]["correct"], 1)


class ControlRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_cases_that_declare_a_control_emit_control_rows(self):
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [item]}}, "public_identity": {},
                                  "private_keys_sha256": "p", "fixture_sha256": "f"}}

        def attempt(attempt_id, text, control=None):
            return {"attempt_id": attempt_id, "execution_status": "complete", "answer_status": "final_answer",
                    "process_status": "clean_exit", "process_problems": [], "control": control,
                    "final_answers": [{"text": text}]}

        runs = [
            {"case": "facets", "rep": 1, "final_attempt_id": "a1", "attempt_records": [attempt("a1", "Bug is real.")]},
            {"case": "summary", "rep": 1, "final_attempt_id": "s1", "attempt_records": [attempt("s1", "Three bullets.")]},
            {"case": "count", "rep": 1, "final_attempt_id": "c1",
             "attempt_records": [attempt("c1", "3", {"kind": "answer_integer", "correct": True,
                                                     "evidence_class": "runner_graded"})]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "captured.json", root / "cases.json"
            results.write_text(json.dumps({"schema": 4, "cost_ledger": {}, "runs": runs}))
            cases.write_text(json.dumps({"cases": [
                {"id": "facets", "dataset": "local", "key": "facets"},
                {"id": "summary", "dataset": "local", "key": None},
                {"id": "count", "dataset": "local", "key": None, "control": {"kind": "answer_integer"}}]}))

            async def judge(states, questions, timeout, concurrency):
                return {sid: dict(GOOD_ANSWERS) for sid in states}, {}, _batch_record(len(states))

            with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)), \
                    mock.patch.object(grade, "_print_table"):
                result = await grade.grade(results, cases, out_path=root / "out.json", ledger_path=root / "ledger.json")
        self.assertEqual([row["case"] for row in result["control_runs"]], ["count"])
        self.assertEqual(result["controls"]["overall"], {"n": 1, "scored": 1, "correct": 1, "control_accuracy": 1.0})
        self.assertEqual(set(result["controls"]["per_case"]), {"count"})


class ImmutableInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_during_grading_does_not_change_the_bytes_graded(self):
        original = "Original answer about the bug."
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [item]}}, "public_identity": {},
                                  "private_keys_sha256": "p", "fixture_sha256": "f"}}
        sent = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "captured.json", root / "cases.json"
            ledger, out = root / "grading.ledger.json", root / "captured.graded.json"
            results.write_text(json.dumps({"schema": 4, "cost_ledger": {}, "runs": [{
                "case": "facets", "rep": 1, "final_attempt_id": "a1",
                "attempt_records": [{"attempt_id": "a1", "execution_status": "complete", "answer_status": "final_answer",
                                     "process_status": "clean_exit", "process_problems": [], "control": None,
                                     "final_answers": [{"text": original}]}]}]}))
            cases.write_text(json.dumps({"cases": [{"id": "facets", "dataset": "local", "key": "facets"}]}))

            async def judge(states, questions, timeout, concurrency):
                sent.update({sid: state["answer"] for sid, state in states.items()})
                results.write_text("MUTATED after the snapshot was taken")  # intervening edit
                return ({sid: dict(GOOD_ANSWERS) for sid in states}, {},
                        {"id": "b", "kind": "judgment_batch", "items": len(states), "model": "m",
                         "cost_usd": None, "cost_basis": "batch_status_estimate", "billing_complete": False,
                         "cleanup_errors": [], "cache": {}})

            with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)), \
                    mock.patch.object(grade, "_print_table"):
                result = await grade.grade(results, cases, out_path=out, ledger_path=ledger)
            self.assertEqual(list(sent.values()), [original])
            self.assertEqual(result["runs"][0]["answer_sha256"], grade._digest(original))
            self.assertEqual(json.loads(ledger.read_text())["inputs_changed_during_grading"], [str(results)])


class LaunchEvidenceTests(unittest.TestCase):
    def test_verify_cells_requires_load_before_an_error_free_grading_call(self):
        load, call = "%load x", "await grade('r', 'c', 'o')"
        in_order_ok = [{"code": load, "is_error": False}, {"code": call, "is_error": False}]
        self.assertTrue(launch.verify_cells(in_order_ok, load, call)["grade_ok"])
        grade_first = [{"code": call, "is_error": False}, {"code": load, "is_error": False}]
        self.assertFalse(launch.verify_cells(grade_first, load, call)["grade_seen"])
        errored = [{"code": load, "is_error": False}, {"code": call, "is_error": True}]
        result = launch.verify_cells(errored, load, call)
        self.assertTrue(result["grade_seen"])
        self.assertFalse(result["grade_ok"])

    def test_cache_descriptor_never_names_the_private_clone_path(self):
        plan = {"status": "fresh_empty_namespace", "isolation": "independent_per_attempt", "path": Path("/tmp/x.sqlite"),
                "initial_sha256": "e", "initial_bytes": 0}
        descriptor = launch.cache_descriptor(plan, "cold")
        self.assertNotIn("path", descriptor)
        self.assertEqual(descriptor["env"], "OMP_JUDGMENT_CACHE_DB")
        self.assertFalse(descriptor["host_attested"])

    def test_warm_clones_start_from_the_frozen_backup_including_wal_and_stay_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            seed = workdir / "seed.sqlite"
            writer = sqlite3.connect(seed)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE t (v TEXT)")
            writer.execute("INSERT INTO t VALUES ('committed-in-wal')")
            writer.commit()
            try:
                frozen = run.freeze_cache_seed(str(seed), workdir / "work")
                first = run.cache_plan("warm", frozen, workdir / "work", "grading", 1, 1)
                second = run.cache_plan("warm", frozen, workdir / "work", "grading", 1, 1)
                with sqlite3.connect(first["path"]) as clone:
                    clone.execute("INSERT INTO t VALUES ('attempt-only')")
                with sqlite3.connect(second["path"]) as clone:
                    rows = [row[0] for row in clone.execute("SELECT v FROM t ORDER BY v")]
                self.assertEqual(rows, ["committed-in-wal"])
                self.assertEqual(first["initial_sha256"], second["initial_sha256"])
                self.assertNotEqual(first["path"], second["path"])
            finally:
                writer.close()
            with sqlite3.connect(seed) as reread:
                self.assertEqual([row[0] for row in reread.execute("SELECT v FROM t")], ["committed-in-wal"])

    def test_cold_plans_are_distinct_empty_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run.cache_plan("cold", None, Path(tmp), "grading", 1, 1)
            second = run.cache_plan("cold", None, Path(tmp), "grading", 1, 1)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(first["path"].stat().st_size, 0)


FAKE_OMP = r"""#!__PYTHON__
import json
import os
import sys

if "--version" in sys.argv:
    print("fake-omp 0.0.0")
    raise SystemExit(0)
prompt = sys.argv[-1]
head, rest = prompt.split("Cell 1 (language py):\n", 1)[1].split("\n\nCell 2 (language py):\n", 1)
cells = [head, rest.rstrip("\n")]
mode = os.environ.get("FAKE_OMP_MODE", "")
if mode == "load_only":
    cells = cells[:1]
if mode == "extra_cell":
    cells.append("1 + 1")
events = [{"type": "agent_start"}]
for index, code in enumerate(cells):
    events.append({"type": "tool_execution_start", "toolName": "eval", "toolCallId": f"c{index}",
                   "args": {"code": code}})
    events.append({"type": "tool_execution_end", "toolCallId": f"c{index}", "isError": False})
if mode != "no_agent_end":
    events.append({"type": "agent_end"})
for event in events:
    print(json.dumps(event), flush=True)
"""


class LaunchFreezeTests(unittest.TestCase):
    def test_freeze_copies_exact_bytes_and_keeps_the_results_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            results, cases = root / "cand.jsonl", root / "cases.json"
            results.write_text('{"schema": 3}\n')
            cases.write_text("{}")
            frozen = launch.freeze_inputs(scratch, results, cases)
            self.assertTrue(frozen["results"]["snapshot"].endswith(".jsonl"))
            self.assertEqual(Path(frozen["grader"]["snapshot"]).read_bytes(), launch.GRADE.read_bytes())
            for name in grade.FIXTURE_SOURCES:
                self.assertEqual(Path(frozen[f"fixture:{name}"]["snapshot"]).read_bytes(),
                                 (ROOT / "evals" / name).read_bytes())
            self.assertEqual(launch.input_drift(frozen), [])
            results.write_text('{"schema": 3, "edited": true}\n')
            self.assertEqual(Path(frozen["results"]["snapshot"]).read_text(), '{"schema": 3}\n')
            self.assertEqual(launch.input_drift(frozen), [str(results)])


class LaunchOutputIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Launch verification over the real grade() output of the frozen snapshot; only the judge is faked."""

    async def _launch_frozen(self, root):
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [item]}}, "public_identity": {},
                                  "private_keys_sha256": "p", "fixture_sha256": "f"}}
        results, cases = root / "captured.json", root / "cases.json"
        results.write_text(json.dumps({"schema": 4, "cost_ledger": {}, "runs": [{
            "case": "facets", "rep": 1, "final_attempt_id": "a1",
            "attempt_records": [{"attempt_id": "a1", "execution_status": "complete",
                                 "answer_status": "final_answer", "process_status": "clean_exit",
                                 "process_problems": [], "control": None,
                                 "final_answers": [{"text": "Bug is real."}]}]}]}))
        cases.write_text(json.dumps({"cases": [{"id": "facets", "dataset": "local", "key": "facets"}]}))
        scratch = root / "scratch"
        scratch.mkdir()
        frozen = launch.freeze_inputs(scratch, results, cases)
        plan = run.cache_plan("cold", None, scratch, launch.CELL_NAMESPACE, 1, 1)
        descriptor = launch.cache_descriptor(plan, "cold")
        spec = importlib.util.spec_from_file_location("jevify_frozen_grade", frozen["grader"]["snapshot"])
        frozen_grade = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(frozen_grade)

        async def judge(states, questions, timeout, concurrency):
            return {sid: dict(GOOD_ANSWERS) for sid in states}, {}, _batch_record(len(states))

        out = root / "facets.graded.json"
        with mock.patch.object(frozen_grade, "_fixture_keys", return_value=bundles), \
                mock.patch.object(frozen_grade, "_judge", mock.AsyncMock(side_effect=judge)), \
                mock.patch.object(frozen_grade, "_print_table"):
            await frozen_grade.grade(frozen["results"]["snapshot"], frozen["cases"]["snapshot"],
                                     out_path=out, launch=descriptor)
        return frozen, descriptor, out

    @staticmethod
    def _edit(path, change):
        document = json.loads(path.read_text())
        change(document)
        path.write_text(json.dumps(document))

    async def test_real_grade_output_verifies_against_the_frozen_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            self.assertEqual(json.loads(out.read_text())["input_sha256"], frozen["results"]["sha256"])
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertEqual(outcome["problems"], [])
            self.assertTrue(outcome["ok"])

    async def test_a_different_launch_descriptor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            outcome = launch.verify_outputs(out, frozen, {**descriptor, "requested": "warm"})
            self.assertIn("ledger does not record this launch descriptor", outcome["problems"])

    async def test_a_mapping_where_the_single_result_digest_belongs_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            digest, snapshot = frozen["results"]["sha256"], frozen["results"]["snapshot"]
            self._edit(out, lambda doc: doc.update(input_sha256={snapshot: digest}))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("graded output is not bound to the frozen results bytes", outcome["problems"])

    async def test_a_forged_grader_identity_in_the_output_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            self._edit(out, lambda doc: doc["identities"].update(source_sha256="0" * 64))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("graded output was not produced by the frozen grader bytes", outcome["problems"])
            self.assertIn("ledger identities differ from the graded output", outcome["problems"])

    async def test_the_ledger_must_bind_the_frozen_results_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            ledger = Path(json.loads(out.read_text())["grading"]["ledger_path"])
            snapshot = frozen["results"]["snapshot"]
            self._edit(ledger, lambda doc: doc.update(input_sha256={snapshot: "0" * 64}))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("graded output is not bound to the frozen results bytes", outcome["problems"])

    async def test_inputs_changed_during_grading_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            ledger = Path(json.loads(out.read_text())["grading"]["ledger_path"])
            snapshot = frozen["results"]["snapshot"]
            self._edit(ledger, lambda doc: doc.update(inputs_changed_during_grading=[snapshot]))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("grading inputs changed during grading", outcome["problems"])


class SessionLimitTests(unittest.TestCase):
    def test_default_session_limit_outlives_the_grading_deadline(self):
        self.assertEqual(launch.session_max_time(1800), 1800 + launch.SESSION_MARGIN_SECONDS)
        self.assertGreater(launch.session_max_time(1800), 1800)
        self.assertEqual(launch.session_max_time(1800, 3600), 3600)

    def test_explicit_limit_at_or_below_the_deadline_is_rejected(self):
        for limit in (900, 1800):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "must exceed"):
                launch.session_max_time(1800, limit)

    def test_launch_rejects_the_limit_before_creating_any_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "cand.json", root / "cases.json"
            results.write_text('{"schema": 3}')
            cases.write_text("{}")
            with self.assertRaisesRegex(ValueError, "must exceed"):
                launch.launch(results=results, cases=cases, out=root / "graded.json", omp="/nonexistent/omp",
                              workdir=root / "work", timeout=60, max_time=60)
            self.assertFalse((root / "work").exists())
            self.assertFalse((root / "graded.json.launch.json").exists())


class LaunchBehaviorTests(unittest.TestCase):
    def _run(self, root, mode):
        root.mkdir(parents=True, exist_ok=True)
        results, cases = root / "cand.json", root / "cases.json"
        results.write_text('{"schema": 3}')
        cases.write_text("{}")
        fake = root / "fake-omp"
        fake.write_text(FAKE_OMP.replace("__PYTHON__", sys.executable))
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"FAKE_OMP_MODE": mode}):
            return launch.launch(results=results, cases=cases, out=root / "graded.json", omp=str(fake),
                                 workdir=root / "work")

    def test_exact_cells_without_graded_output_are_not_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self._run(Path(tmp), "")
            self.assertEqual(record["status"], "not_graded")
            self.assertTrue(record["cells"]["sequence_exact"])
            self.assertTrue(record["cells"]["grade_ok"])
            self.assertTrue(record["session"]["complete"])
            self.assertEqual(record["problems"], ["graded output was not written"])
            self.assertEqual(record["session"]["max_time"], 1800 + launch.SESSION_MARGIN_SECONDS)
            self.assertFalse((Path(tmp) / "graded.json").exists())

    def test_missing_or_extra_eval_cells_are_problems(self):
        for mode in ("load_only", "extra_cell"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                record = self._run(Path(tmp), mode)
                self.assertEqual(record["status"], "not_graded")
                self.assertFalse(record["cells"]["sequence_exact"])
                self.assertTrue(any("not exactly the two approved cells" in p for p in record["problems"]))

    def test_a_session_without_agent_end_is_not_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self._run(Path(tmp), "no_agent_end")
            self.assertEqual(record["status"], "not_graded")
            self.assertFalse(record["session"]["complete"])
            self.assertIn("session did not reach agent_end", record["problems"])

    def test_scratch_is_removed_after_a_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root, "")
            self.assertEqual(list((root / "work").iterdir()), [])

    def test_scratch_is_removed_when_the_launch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "cand.json", root / "cases.json"
            results.write_text('{"schema": 3}')
            cases.write_text("{}")
            work = root / "work"
            work.mkdir()
            with mock.patch.object(launch.run, "source_identity", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    launch.launch(results=results, cases=cases, out=root / "graded.json",
                                  omp="/nonexistent/omp", workdir=work)
            self.assertEqual(list(work.iterdir()), [])
            self.assertFalse((root / "graded.json.launch.json").exists())


class PairedComparisonTests(unittest.TestCase):
    def _row(self, case, action_accuracy, unsupported=(), *, key_sha=None):
        return {"case": case, "final_attempt": True, "key_schema": 2, "factual_accuracy": 1.0,
                "action_accuracy": action_accuracy, "introduced_accuracy": 1.0, "factual_coverage": 1.0,
                "action_coverage": 1.0, "introduced_coverage": 1.0, "recall": 1.0, "precision": 1.0,
                "repair": {"unsupported_fix_now_claims": list(unsupported)},
                "key_identity": {"key_schema": 2, "key": case, "key_sha256": key_sha or f"sha-{case}",
                                 "public_identity": {}},
                "key_binding": {"status": "matched", "captured": {}}}

    def _graded(self, rows, *, model="m", skill=None, identities=None, predeclared=None, recorded=True):
        result = {"results": "x", "input_sha256": "h", "identities": _grader_ids() if identities is None else identities,
                  "runs": rows, "control_runs": [],
                  "run_manifest": None if predeclared is None else {"predeclared_thresholds": predeclared}}
        if recorded:
            contract = {"controlled": {"model_requested": model}, "treatment": {"skill": skill, "strategy": "auto"}}
            result.update({"comparison_contract": contract, "comparison_contract_sha256": run.digest_json(contract)})
        return result

    def test_case_means_average_repetitions_before_the_paired_difference(self):
        base = self._graded([self._row("c1", 0.4), self._row("c1", 0.6), self._row("c2", 0.5), self._row("c3", 0.5)])
        candidate = self._graded([self._row("c1", 0.6), self._row("c1", 0.8), self._row("c2", 0.6), self._row("c3", 0.8)])
        comparison = grade.compare_graded(base, candidate)
        action = comparison["comparisons"]["action_accuracy"]
        self.assertAlmostEqual(action["mean_delta"], 0.2, places=3)
        self.assertEqual(action["n"], 3)
        self.assertTrue(comparison["contract_check"]["compatible"])

    def test_different_controlled_model_is_not_comparable_and_is_post_hoc(self):
        base = self._graded([self._row("c1", 0.5)], model="m1")
        candidate = self._graded([self._row("c1", 0.7)], model="m2")
        comparison = grade.compare_graded(base, candidate)
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertIn("model_requested", comparison["contract_check"]["controlled_differences"])
        self.assertTrue(comparison["post_hoc"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_one_treatment_difference_with_identical_instrument_is_comparable(self):
        base = self._graded([self._row("c1", 0.5), self._row("c2", 0.5)])
        candidate = self._graded([self._row("c1", 0.7), self._row("c2", 0.6)], skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertEqual(comparison["contract_check"]["treatment_differences"], ["skill"])
        self.assertTrue(comparison["contract_check"]["one_variable"])
        self.assertTrue(comparison["contract_check"]["compatible"])
        # No predeclared thresholds: the rule withholds, but it is not blocked as not_comparable.
        self.assertEqual(comparison["recommendation"]["verdict"], "withheld")

    def test_one_treatment_difference_with_predeclared_rule_can_support_the_candidate(self):
        thresholds = {"schema": 1, "confidence": 0.95, "min_cases": 2,
                      "metrics": {"correctness": "action_accuracy", "coverage": "action_coverage",
                                  "unsafe": "unsupported_fix_now_claims"},
                      "rules": {"correctness_min_delta": 0.1, "coverage_max_regression": 0.05,
                                "unsafe_max_increase": 0}}
        predeclared = {"sha256": run.digest_json(thresholds), "thresholds": thresholds, "path": "t.json"}
        base = self._graded([self._row("c1", 0.4), self._row("c2", 0.5), self._row("c3", 0.5)], predeclared=predeclared)
        candidate = self._graded([self._row("c1", 0.7), self._row("c2", 0.8), self._row("c3", 0.8)], skill="abc",
                                 predeclared=predeclared)
        comparison = grade.compare_graded(base, candidate)
        self.assertTrue(comparison["predeclared_thresholds_matched"])
        self.assertEqual(comparison["recommendation"]["verdict"], "supports_candidate", comparison["recommendation"])

    def test_unrecorded_contract_returns_a_not_comparable_verdict_instead_of_raising(self):
        base = self._graded([self._row("c1", 0.5)], recorded=False)
        candidate = self._graded([self._row("c1", 0.7)], skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertFalse(comparison["contract_check"]["one_variable"])
        self.assertIn("comparison_contract", comparison["contract_check"]["unrecorded"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_differing_grader_identity_is_a_controlled_difference(self):
        base = self._graded([self._row("c1", 0.5)])
        changed = {**_grader_ids(), "extraction_rubric_sha256": {"1": "r1", "2": "other"}}
        candidate = self._graded([self._row("c1", 0.9)], identities=changed, skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertIn("grader:extraction_rubric_sha256", comparison["contract_check"]["controlled_differences"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_missing_grader_identity_is_unrecorded_and_blocks_a_claim(self):
        base = self._graded([self._row("c1", 0.5)])
        without = {k: v for k, v in _grader_ids().items() if k != "source_sha256"}
        candidate = self._graded([self._row("c1", 0.9)], identities=without, skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertIn("grader:source_sha256", comparison["contract_check"]["unrecorded"])
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_differing_key_identity_is_a_controlled_difference(self):
        base = self._graded([self._row("c1", 0.5)])
        candidate = self._graded([self._row("c1", 0.9, key_sha="other-key")], skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertIn("key:c1", comparison["contract_check"]["controlled_differences"])
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")


if __name__ == "__main__":
    unittest.main()
