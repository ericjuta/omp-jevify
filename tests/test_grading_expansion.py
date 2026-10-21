"""Behavioral contracts for grading expansions: route provenance,
calibration effects, off-inventory boundaries, controls, immutable inputs, cache-isolated
launch evidence, and paired comparison (evals/grade.py, evals/grade_launch.py)."""

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
# Two-facet questions for the generic judge path (grade's schema1 path uses HISTORICAL_QUESTIONS).
QUESTIONS = {"factual": {"type": "choice", "instructions": "", "criteria": {"supported": "", "refuted": ""}},
             "action": {"type": "choice", "instructions": "", "criteria": {"fix_now": "", "defer": ""}},
             "introduced": {"type": "choice", "instructions": "", "criteria": {"yes": "", "no": ""}}}


def _bundle(keys):
    """Grader bundle for synthetic keys. Schema2 keys also need a reviewed source snapshot (no git here)."""
    head = "h" * 40
    return {"keys": keys, "public_identity": {}, "private_keys_sha256": "p", "fixture_sha256": "f",
            "snapshot": {"schema": "jevify-source/1", "head": head, "base": None, "trees": {head: {}}},
            "output_contract_sha256": None}


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
            _, _, record = await grade._judge(states, QUESTIONS, 10, 1)
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
            labels, errors, record = await grade._judge(states, QUESTIONS, 10, 1)
        self.assertEqual(labels["slot"]["factual"], "supported")
        self.assertEqual(record["provenance"]["slot"]["source"], "host_item_stream_unavailable")
        self.assertTrue(record["provenance"]["slot"]["provenance_unknown"])
        self.assertIsNone(record["provenance"]["slot"]["selected_model"])
        self.assertEqual(record["provenance_summary"]["known"], 0)



def _finding(iid, defect, *, factual="supported", actions=("fix_now",), introduced="yes"):
    """Tiny schema2 key item, independent of the generated datasets and private keys."""
    return {"id": iid, "desc": f"Synthetic claim {iid}", "factual": factual,
            "actions": list(actions), "introduced": introduced, "defect": defect,
            "group": "local", "evidence": ["local:1"], "rationale": "Synthetic regression key."}


# The prior extractor's erroneous output for the retained failures: fix_now, which smoke recorded as false repairs.
PRIOR_FAILURE = {"factual": "supported", "action": "fix_now", "introduced": "yes"}
OMITTED = {"factual": "omitted", "action": "omitted", "introduced": "omitted"}



class ScoreAccountingTests(unittest.TestCase):
    ITEMS = [_finding("C01", "D-A"), _finding("C02", "D-B"), _finding("C03", "D-C"), _finding("C04", "D-D")]

    def test_omitted_rejected_and_failed_facets_are_separate_with_fixed_denominators(self):
        verdicts = {"C01": dict(PRIOR_FAILURE), "C02": dict(OMITTED),
                    "C04": {"factual": "supported", "action": "fix_now", "introduced": "yes"}}
        errors = {"C03": {facet: "extraction failed" for facet in ("factual", "action", "introduced")}}
        score = grade.score_facets(self.ITEMS, verdicts, errors, unattributed=["C01"])
        for facet in ("factual", "action", "introduced"):
            with self.subTest(facet=facet):
                metrics = score["facets"][facet]
                self.assertEqual(metrics["planned"], 4)
                self.assertEqual((metrics["omitted"], metrics["unattributed"], metrics["extraction_errors"],
                                  metrics["unresolved"]), (1, 1, 1, 2))
        self.assertEqual(score["facets"]["factual"]["correct"], 1)
        self.assertEqual((score["unattributed_items"], score["unresolved_items"]), (1, 2))
        # The injected fix_now for the rejected C01 earns no credit; only the judged C04 is recommended.
        self.assertEqual(score["repair"]["recommended_defects"], ["D-D"])
        self.assertEqual((score["repair"]["tp"], score["repair"]["fp"], score["repair"]["fn"]), (1, 0, 3))

    def test_standalone_scoring_without_attribution_counts_every_gap_as_an_extraction_error(self):
        score = grade.score_facets(self.ITEMS, {"C04": {"factual": "supported", "action": "fix_now", "introduced": "yes"}})
        for facet in ("factual", "action", "introduced"):
            with self.subTest(facet=facet):
                self.assertEqual((score["facets"][facet]["extraction_errors"], score["facets"][facet]["unattributed"]), (3, 0))
        self.assertEqual(score["unattributed_items"], 0)



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
        bundles = {("local", 1): _bundle({"facets": {"schema": 2, "items": [item]}})}

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
        item = {"id": "bug", "desc": "Synthetic claim bug", "flag": True}
        bundles = {("local", 1): _bundle({"facets": {"schema": 1, "flag_label": "a bug", "items": [item]}})}
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
                return ({sid: {"verdict": "flagged"} for sid in states}, {},
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
            # The frozen structured-findings module is part of the grader's identity and launch snapshot.
            self.assertEqual(Path(frozen["grader:findings.py"]["snapshot"]).read_bytes(),
                             (ROOT / "evals" / "findings.py").read_bytes())
            self.assertEqual(launch.input_drift(frozen), [])
            results.write_text('{"schema": 3, "edited": true}\n')
            self.assertEqual(Path(frozen["results"]["snapshot"]).read_text(), '{"schema": 3}\n')
            self.assertEqual(launch.input_drift(frozen), [str(results)])


class LaunchOutputIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Launch verification over the real grade() output of the frozen snapshot; only the judge is faked."""

    async def _launch_frozen(self, root):
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): _bundle({"facets": {"schema": 2, "items": [item]}})}
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


if __name__ == "__main__":
    unittest.main()
