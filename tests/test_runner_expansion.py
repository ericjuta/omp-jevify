"""Runner behavior: envelope gating, retry accounting, outcome classes, controls, and predeclared inputs.

Exercises evals/run.py directly. attempt_one would start a live omp session, so run_one is driven
with it replaced by a mock whose return value carries the fields run_one and summarize read.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner_expansion_run", ROOT / "evals" / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


def _attempt(status, secs, *, known=0, unknown=0, complete=False):
    """The fields run_one reads from an attempt_one result. Stock coverage is incomplete unless stated."""
    return {"attempt_id": "count:1:0", "case": "count", "rep": 1, "attempt": 0, "expect": "trigger",
            "execution_status": status, "secs": secs,
            "method": {"cache": {"questions_known": known, "questions_unknown_rows": unknown, "rows": 0},
                       "judged": None},
            "question_coverage": {"complete": complete, "evidence_class": "test"},
            "accounting": {"entries": [], "conflicts": [], "snapshot_statuses": []}}


def _args(retries, envelope):
    return SimpleNamespace(retries=retries, envelope=envelope)


class RunOneTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(run, "merge_accounting", return_value={"monetary_status": "unknown"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_infra_attempt_is_retried_and_total_time_covers_every_attempt(self):
        envelope = run.Envelope(attempts=5)
        with mock.patch.object(run, "attempt_one",
                               side_effect=[_attempt("infra", 2.0), _attempt("complete", 3.5)]) as attempt_one:
            result = run.run_one({"id": "count"}, 1, None, None, _args(1, envelope), None)
        self.assertEqual(attempt_one.call_count, 2)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["execution_status"], "complete")
        self.assertEqual(result["attempt_secs"], [2.0, 3.5])
        self.assertEqual(result["total_secs"], 5.5)
        self.assertEqual(result["final_attempt_secs"], 3.5)
        self.assertIsNone(result["retry_blocked_by_envelope"])
        self.assertEqual(envelope.attempts_reserved, 2)

    def test_a_session_that_failed_after_work_is_not_retried(self):
        with mock.patch.object(run, "attempt_one", side_effect=[_attempt("failed", 1.0)]) as attempt_one:
            result = run.run_one({"id": "count"}, 1, None, None, _args(2, run.Envelope(attempts=5)), None)
        self.assertEqual(attempt_one.call_count, 1)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["execution_status"], "failed")

    def test_envelope_blocks_a_retry_and_reports_the_block(self):
        envelope = run.Envelope(attempts=1)
        with mock.patch.object(run, "attempt_one", side_effect=[_attempt("infra", 1.0)]) as attempt_one:
            result = run.run_one({"id": "count"}, 1, None, None, _args(1, envelope), None)
        self.assertEqual(attempt_one.call_count, 1)
        self.assertEqual(result["execution_status"], "infra")
        self.assertEqual(result["retry_blocked_by_envelope"], "attempts")
        self.assertEqual(envelope.record()["exhausted_by"], "attempts")

    def test_exhausted_envelope_schedules_nothing_and_names_the_limit(self):
        envelope = run.Envelope(attempts=1)
        envelope.reserve()
        with mock.patch.object(run, "attempt_one") as attempt_one:
            result = run.run_one({"id": "count"}, 2, None, None, _args(1, envelope), None)
        attempt_one.assert_not_called()
        self.assertEqual(result, {"not_scheduled": True, "case": "count", "rep": 2, "reason": "attempts"})

    def test_question_limit_counts_known_questions_as_an_observed_stop_guard(self):
        envelope = run.Envelope(questions=5)
        with mock.patch.object(run, "attempt_one",
                               side_effect=[_attempt("complete", 1.0, known=3, complete=True)]):
            run.run_one({"id": "count"}, 1, None, None, _args(0, envelope), None)
        self.assertEqual(envelope.record()["questions_observed"], 3)
        self.assertIsNone(envelope.exhausted_by)
        self.assertTrue(envelope.reserve())
        envelope.observe(2, 0, coverage_complete=True)
        self.assertFalse(envelope.reserve())
        self.assertEqual(envelope.exhausted_by, "questions")


class EnvelopeTests(unittest.TestCase):
    def test_attempt_limit_is_reserved_before_each_start(self):
        envelope = run.Envelope(attempts=2)
        self.assertEqual([envelope.reserve() for _ in range(3)], [True, True, False])
        self.assertEqual(envelope.exhausted_by, "attempts")
        record = envelope.record()
        self.assertEqual((record["attempts_reserved"], record["dollar_envelope"], record["hard_dollar_guarantee"]),
                         (2, None, False))

    def test_unbounded_envelope_never_refuses(self):
        envelope = run.Envelope()
        self.assertTrue(all(envelope.reserve() for _ in range(10)))
        self.assertIsNone(envelope.exhausted_by)


class PreflightTests(unittest.TestCase):
    def _args(self, **overrides):
        values = {"authorize_attempts": None, "authorize_questions": None, "authorize_concurrency": None,
                  "retries": 1, "max_time": 420, "max_calls": 40}
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_attempts_below_one_per_planned_run_are_refused_with_the_bound(self):
        plan = run.preflight_plan(self._args(authorize_attempts=2), planned_runs=3, concurrency=1)
        self.assertEqual(plan["attempt_upper_bound"], 6)
        self.assertEqual(len(plan["refusals"]), 1)
        self.assertIn("cannot cover one attempt per planned run (3)", plan["refusals"][0])

    def test_unauthorized_questions_are_reported_as_unbounded_not_zero(self):
        plan = run.preflight_plan(self._args(authorize_attempts=3), planned_runs=3, concurrency=1)
        self.assertEqual(plan["refusals"], [])
        self.assertTrue(plan["question_exposure"].startswith("unbounded"))
        self.assertEqual(plan["per_attempt"]["watchdog_seconds"], 450)
        self.assertFalse(plan["dollar_exposure"]["hard_dollar_guarantee"])

    def test_authorized_questions_are_labelled_an_observed_only_stop_guard_never_a_bound(self):
        plan = run.preflight_plan(self._args(authorize_attempts=3, authorize_questions=5), 3, 1)
        self.assertTrue(plan["question_exposure"].startswith("observed-only stop guard"))
        self.assertIn("actual question exposure is not bounded", plan["question_exposure"])
        self.assertNotIn("bounded on", plan["question_exposure"])


class OutcomeTests(unittest.TestCase):
    def test_a_captured_answer_survives_a_process_failure(self):
        process = run.process_outcome(deadline=False, max_calls=False, forced=False, returncode=1,
                                      malformed=False, agent_ended=True)
        self.assertEqual(process, ("nonzero_exit", ["nonzero_exit"]))
        self.assertEqual(run.answer_outcome([{"text": "3"}], True), "final_answer")
        self.assertEqual(run.execution_status(True, [{}], None, process[0]), "process_failed")

    def test_first_forced_cause_leads_and_every_problem_is_kept(self):
        first, problems = run.process_outcome(deadline=True, max_calls=False, forced=True, returncode=-9,
                                              malformed=False, agent_ended=False)
        self.assertEqual(first, "forced_deadline")
        self.assertEqual(problems, ["forced_deadline", "nonzero_exit", "missing_agent_end"])

    def test_limits_are_incomplete_not_failed_or_infra(self):
        self.assertEqual(run.execution_status(True, [{}], "deadline", "forced_deadline"), "incomplete")
        self.assertEqual(run.execution_status(False, [{}], "max_calls", "forced_max_calls"), "incomplete")

    def test_no_calls_and_no_answer_is_infra_while_calls_without_answer_fail(self):
        self.assertEqual(run.execution_status(False, [], None), "infra")
        self.assertEqual(run.execution_status(False, [{}], None), "failed")
        self.assertEqual(run.execution_status(True, [{}], None), "complete")
        self.assertEqual(run.answer_outcome([], True), "empty_stop")
        self.assertEqual(run.answer_outcome([], False), "no_answer")


class ControlTests(unittest.TestCase):
    def _repo(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, text in files.items():
            path = Path(tmp.name) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return Path(tmp.name)

    def test_integer_control_counts_call_sites_from_the_fixture_not_the_answer(self):
        repo = self._repo({"billing/a.py": "x = fetch_rate(1)\ny = fetch_rate(2)\n",
                           "billing/b.py": "def fetch_rate(x):\n    return x\nz = fetch_rate(3)\n"})
        expectation = run.control_expectation({"control": {"kind": "answer_integer"}}, repo)
        self.assertEqual((expectation["precondition"], expectation["expected"]), (True, 3))
        right = run.control_outcome(expectation, repo, "3\n", "final_answer")
        self.assertTrue(right["correct"])
        self.assertEqual(right["observed"], 3)
        self.assertFalse(run.control_outcome(expectation, repo, "2", "final_answer")["correct"])
        prose = run.control_outcome(expectation, repo, "three", "final_answer")
        self.assertFalse(prose["correct"])
        self.assertEqual(prose["reasons"], ["final answer is not a single integer"])
        silent = run.control_outcome(expectation, repo, "", "empty_stop")
        self.assertFalse(silent["correct"])
        self.assertEqual(silent["reasons"], ["no final answer"])

    def test_missing_billing_directory_is_a_failed_precondition_not_a_zero(self):
        repo = self._repo({"other.py": "x = 1\n"})
        expectation = run.control_expectation({"control": {"kind": "answer_integer"}}, repo)
        outcome = run.control_outcome(expectation, repo, "0", "final_answer")
        self.assertFalse(expectation["precondition"])
        self.assertIsNone(outcome["correct"])
        self.assertEqual(outcome["reasons"], ["control precondition absent from the launch fixture"])

    def test_rename_control_needs_new_name_with_original_value_and_no_stale_references(self):
        spec = {"control": {"kind": "repo_rename", "old": "RETRY_LIMIT", "new": "MAX_RETRIES", "file": "settings.py"}}
        repo = self._repo({"settings.py": "RETRY_LIMIT = 3\n", "service.py": "print('ok')\n"})
        expectation = run.control_expectation(spec, repo)
        self.assertEqual(expectation["expected"]["value"], "3")
        (repo / "settings.py").write_text("MAX_RETRIES = 3\n", encoding="utf-8")
        self.assertTrue(run.control_outcome(expectation, repo, "", "final_answer")["correct"])
        (repo / "service.py").write_text("from settings import RETRY_LIMIT\n", encoding="utf-8")
        stale = run.control_outcome(expectation, repo, "", "final_answer")
        self.assertFalse(stale["correct"])
        self.assertEqual(stale["observed"]["stale_references"], ["service.py"])
        (repo / "service.py").write_text("print('ok')\n", encoding="utf-8")
        (repo / "settings.py").write_text("MAX_RETRIES = 5\n", encoding="utf-8")
        changed = run.control_outcome(expectation, repo, "", "final_answer")
        self.assertFalse(changed["correct"])
        self.assertEqual(changed["reasons"], ["new identifier with the original value is absent"])

    def test_control_means_average_only_scored_controls_per_case(self):
        runs = [{"case": "count", "control": {"correct": True}},
                {"case": "count", "control": {"correct": False}},
                {"case": "count", "control": {"correct": None}},
                {"case": "rename", "control": None}]
        self.assertEqual(run.control_means(runs), {"count": 0.5})

    def test_control_kinds_are_validated_before_any_session_starts(self):
        base = {"id": "x", "expect": "trigger", "prompt": "do it"}
        with self.assertRaisesRegex(ValueError, "control kind"):
            run.validate_case({**base, "control": {"kind": "vibes"}})
        with self.assertRaisesRegex(ValueError, "repo_rename control needs old, new and file"):
            run.validate_case({**base, "control": {"kind": "repo_rename", "old": "A", "new": "B"}})
        run.validate_case({**base, "control": {"kind": "answer_integer"}})


class SummaryContractTests(unittest.TestCase):
    def _run(self, **overrides):
        base = {"case": "count", "rep": 1, "expect": "trigger", "keyword": False, "infra": False, "pass": True,
                "skill_read": True, "skill_before_data": True, "judged": True, "skill_source_ok": None,
                "eval_errors": 0, "secs": 1.0, "usage": {"cost": None}, "execution_status": "complete",
                "answer_status": "final_answer", "process_status": "clean_exit", "control": {"correct": True}}
        base.update(overrides)
        return base

    def test_cohorts_separate_planned_not_scheduled_and_infra_final_runs(self):
        runs = [self._run(),
                self._run(rep=2, infra=True, execution_status="infra", answer_status="no_answer",
                          process_status="nonzero_exit", control=None)]
        summary = run.summarize(runs, planned=5, not_scheduled=3)
        cohorts = summary["cohorts"]
        self.assertEqual((cohorts["planned_runs"], cohorts["scheduled_runs"], cohorts["not_scheduled_runs"],
                          cohorts["completed_runs"], cohorts["infra_final_runs"]), (5, 2, 3, 1, 1))
        self.assertEqual(summary["process_outcomes"], {"clean_exit": 1, "nonzero_exit": 1})
        self.assertEqual(summary["answer_outcomes"], {"final_answer": 1})
        self.assertEqual(summary["controls"], {"scored": 1, "correct": 1, "unscored": 0})

    def test_process_failures_and_incomplete_sessions_are_counted_apart_from_answers(self):
        runs = [self._run(execution_status="process_failed", process_status="nonzero_exit"),
                self._run(rep=2, execution_status="incomplete", answer_status="no_answer", control={"correct": None})]
        summary = run.summarize(runs)
        self.assertEqual(summary["process_failed"], 1)
        self.assertEqual(summary["failed_or_incomplete"], 1)
        self.assertEqual(summary["per_case"]["count"]["execution_incomplete"], 1)
        self.assertEqual(summary["controls"], {"scored": 1, "correct": 1, "unscored": 1})


class QuestionGuardRegressionTests(unittest.TestCase):
    def test_unknown_question_coverage_stops_scheduling_under_a_question_limit(self):
        envelope = run.Envelope(questions=5)
        self.assertTrue(envelope.reserve())
        envelope.observe(0, 1)
        self.assertFalse(envelope.reserve())
        self.assertEqual(envelope.exhausted_by, "questions_unverifiable")
        record = envelope.record()
        self.assertEqual((record["questions_observed"], record["questions_unknown_rows"]), (0, 1))
        self.assertEqual((record["question_exposure"], record["question_guard"]), ("unknown", "observed_only_stop"))

    def test_known_questions_below_the_limit_do_not_mask_unknown_coverage(self):
        envelope = run.Envelope(questions=50)
        envelope.reserve()
        envelope.observe(3, 2)
        self.assertFalse(envelope.reserve())
        self.assertEqual(envelope.exhausted_by, "questions_unverifiable")

    def test_unknown_coverage_without_a_question_limit_leaves_attempts_binding_and_exposure_labelled(self):
        envelope = run.Envelope(attempts=3)
        envelope.reserve()
        envelope.observe(0, 4)
        self.assertTrue(envelope.reserve())
        self.assertTrue(envelope.reserve())
        self.assertFalse(envelope.reserve())
        self.assertEqual(envelope.exhausted_by, "attempts")
        record = envelope.record()
        self.assertEqual((record["question_exposure"], record["question_guard"]), ("unknown", "none"))

    def test_attempt_limit_keeps_its_own_reason_when_questions_are_also_unverifiable(self):
        envelope = run.Envelope(attempts=1, questions=5)
        self.assertTrue(envelope.reserve())
        envelope.observe(0, 1)
        self.assertFalse(envelope.reserve())
        self.assertEqual(envelope.exhausted_by, "attempts")

    def test_known_subtotal_without_complete_coverage_is_never_labelled_exact(self):
        envelope = run.Envelope(questions=50)
        self.assertEqual(envelope.record()["question_exposure"], "not_observed")
        envelope.reserve()
        envelope.observe(3, 0)
        record = envelope.record()
        self.assertEqual((record["questions_observed"], record["question_exposure"]), (3, "unknown"))
        self.assertFalse(envelope.reserve())
        self.assertEqual(envelope.exhausted_by, "questions_unverifiable")

    def test_complete_host_coverage_is_the_only_path_to_an_observed_exposure(self):
        envelope = run.Envelope(questions=50)
        envelope.reserve()
        envelope.observe(3, 0, coverage_complete=True)
        self.assertEqual(envelope.record()["question_exposure"], "observed_complete")
        self.assertTrue(envelope.reserve())

    def test_unverifiable_question_coverage_blocks_retries_and_later_runs(self):
        envelope = run.Envelope(attempts=5, questions=5)
        args = SimpleNamespace(retries=1, envelope=envelope)
        with mock.patch.object(run, "merge_accounting", return_value={"monetary_status": "unknown"}), \
                mock.patch.object(run, "attempt_one",
                                  side_effect=[_attempt("infra", 1.0, unknown=1)]) as attempt_one:
            first = run.run_one({"id": "count"}, 1, None, None, args, None)
            second = run.run_one({"id": "count"}, 2, None, None, args, None)
        self.assertEqual(attempt_one.call_count, 1)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["retry_blocked_by_envelope"], "questions_unverifiable")
        self.assertEqual(second, {"not_scheduled": True, "case": "count", "rep": 2,
                                  "reason": "questions_unverifiable"})

    def test_zero_ledger_attempt_leaves_question_coverage_incomplete_and_stops_later_runs(self):
        for judged in (None, True):
            with self.subTest(judged=judged):
                self.assertFalse(run.question_coverage({"judged": judged, "cache": {"rows": 0}})["complete"])
        envelope = run.Envelope(questions=5)
        args = SimpleNamespace(retries=0, envelope=envelope)
        with mock.patch.object(run, "merge_accounting", return_value={"monetary_status": "unknown"}), \
                mock.patch.object(run, "attempt_one", side_effect=[_attempt("complete", 1.0)]) as attempt_one:
            first = run.run_one({"id": "count"}, 1, None, None, args, None)
            second = run.run_one({"id": "count"}, 2, None, None, args, None)
        self.assertEqual(first["execution_status"], "complete")
        self.assertEqual(attempt_one.call_count, 1)
        self.assertEqual(second, {"not_scheduled": True, "case": "count", "rep": 2,
                                  "reason": "questions_unverifiable"})
        self.assertEqual(envelope.record()["question_exposure"], "unknown")

    def test_zero_ledger_infra_retry_is_blocked_when_question_coverage_is_incomplete(self):
        envelope = run.Envelope(attempts=5, questions=5)
        args = SimpleNamespace(retries=1, envelope=envelope)
        with mock.patch.object(run, "merge_accounting", return_value={"monetary_status": "unknown"}), \
                mock.patch.object(run, "attempt_one", side_effect=[_attempt("infra", 1.0)]) as attempt_one:
            result = run.run_one({"id": "count"}, 1, None, None, args, None)
        self.assertEqual(attempt_one.call_count, 1)
        self.assertEqual(result["retry_blocked_by_envelope"], "questions_unverifiable")

    def test_process_failure_with_a_captured_answer_is_never_complete_and_is_not_retried(self):
        failed = {**_attempt("process_failed", 1.0), "answer_status": "final_answer",
                  "process_status": "nonzero_exit", "process_problems": ["nonzero_exit"]}
        with mock.patch.object(run, "merge_accounting", return_value={"monetary_status": "unknown"}), \
                mock.patch.object(run, "attempt_one", side_effect=[failed]) as attempt_one:
            result = run.run_one({"id": "count"}, 1, None, None, _args(2, run.Envelope(attempts=5)), None)
        self.assertEqual(attempt_one.call_count, 1)
        self.assertEqual(result["execution_status"], "process_failed")
        self.assertEqual(result["answer_status"], "final_answer")
        self.assertNotEqual(result["execution_status"], "complete")


def _contract(arm="skill", bundle="b" * 64, strategy="auto", model=None):
    """A real comparison_contract built from the same argument shape main() passes."""
    args = SimpleNamespace(model=model, thinking=None, cache_mode="cold", max_time=420, max_calls=40, retries=1,
                           reps=1, authorize_attempts=3, authorize_questions=None, arm=arm, strategy=strategy)
    source = {"kind": "worktree", "source_manifest_sha256": "s" * 64,
              "executable": {"sha256": "e" * 64, "version": "x", "path": "/bin/omp"}}
    skill = {"materialized_sha256": bundle}
    return run.comparison_contract(args, cases_sha="c" * 64, selected_ids=["count"], identities={"count": {}},
                                   source=source, skill=skill, runner={"sha256": "r" * 64, "python": "3"},
                                   cache_seed=None, thresholds_sha=None, concurrency=1)


class ContractTreatmentTests(unittest.TestCase):
    def test_skill_versus_none_is_exactly_one_treatment_difference(self):
        diff = run.contract_differences(_contract(arm="skill"), _contract(arm="none", bundle=None))
        self.assertEqual(diff["treatment_differences"], ["skill"])
        self.assertTrue(diff["one_variable"])
        self.assertTrue(diff["compatible"])

    def test_changed_skill_bundle_alone_is_one_treatment_difference(self):
        diff = run.contract_differences(_contract(bundle="a" * 64), _contract(bundle="b" * 64))
        self.assertEqual(diff["treatment_differences"], ["skill"])
        self.assertTrue(diff["one_variable"])

    def test_changing_arm_and_strategy_together_is_two_differences_and_not_one_variable(self):
        diff = run.contract_differences(_contract(strategy="auto"), _contract(arm="none", bundle=None,
                                                                              strategy="jev-flat"))
        self.assertEqual(diff["treatment_differences"], ["skill", "strategy"])
        self.assertFalse(diff["one_variable"])

    def test_a_controlled_field_change_is_never_compatible_even_with_one_treatment_change(self):
        diff = run.contract_differences(_contract(), _contract(arm="none", bundle=None, model="other"))
        self.assertFalse(diff["compatible"])
        self.assertIn("model_requested", diff["controlled_differences"])

    def test_identical_contracts_have_no_treatment_difference_and_are_not_one_variable(self):
        diff = run.contract_differences(_contract(), _contract())
        self.assertEqual(diff["treatment_differences"], [])
        self.assertFalse(diff["one_variable"])


class OrderedRubricIdentityTests(unittest.TestCase):
    RUBRIC = {"factual": {"type": "choice", "instructions": "Décide from the cited evidence.",
                          "criteria": {"supported": "Evidence establishes it.", "refuted": "Evidence contradicts it."}}}

    def test_ordered_helper_hash_matches_the_helper_identity_digest(self):
        self.assertEqual(run.helper_qhash(self.RUBRIC), "ca2521dd7996")

    def test_question_order_is_identity_and_the_legacy_sorted_digest_is_not_used(self):
        reordered = {"b": {"type": "choice"}, "a": {"type": "choice"}}
        self.assertNotEqual(run.helper_qhash(reordered), run.helper_qhash(dict(reversed(list(reordered.items())))))
        self.assertNotEqual(run.helper_qhash(self.RUBRIC), "84ec3b9e62d7")

    def test_criterion_order_is_identity(self):
        flipped = {"factual": {**self.RUBRIC["factual"],
                               "criteria": dict(reversed(list(self.RUBRIC["factual"]["criteria"].items())))}}
        self.assertNotEqual(run.helper_qhash(self.RUBRIC), run.helper_qhash(flipped))

    def test_public_plan_hashes_the_public_rubric_with_the_ordered_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "review").mkdir()
            (repo / "review" / "rubric.json").write_text(json.dumps(self.RUBRIC), encoding="utf-8")
            self.assertEqual(run.public_plan(repo)["rubric_hash"], "ca2521dd7996")


class PredeclaredInputTests(unittest.TestCase):
    def test_shipped_release_smoke_selection_validates_with_both_runner_controls(self):
        payload = json.loads((ROOT / "evals" / "cases-smoke.json").read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in payload["cases"]}
        self.assertEqual(list(cases), ["refund_retry_review", "count", "rename"])
        for case in payload["cases"]:
            run.validate_case(case)
        self.assertEqual(cases["count"]["control"], {"kind": "answer_integer"})
        self.assertEqual(cases["rename"]["control"]["kind"], "repo_rename")
        self.assertNotIn("control", cases["refund_retry_review"])

    def test_thresholds_digest_is_key_order_independent_and_bad_rules_are_refused(self):
        raw = (ROOT / "evals" / "thresholds-release-smoke.json").read_bytes()
        value, digest = run.load_thresholds(raw)
        reordered = json.dumps(dict(reversed(list(value.items()))), indent=3).encode()
        self.assertEqual(run.load_thresholds(reordered)[1], digest)
        self.assertEqual(value["min_cases"], 2)
        with self.assertRaisesRegex(ValueError, "min_cases"):
            run.load_thresholds(json.dumps(dict(value, min_cases=1)).encode())
        with self.assertRaisesRegex(ValueError, "confidence 0.95"):
            run.load_thresholds(json.dumps(dict(value, confidence=0.9)).encode())


if __name__ == "__main__":
    unittest.main()
