"""Pilot reporting: strict manifests, fail-closed comparisons and gated method recommendations.

Every fixture is explicitly synthetic test data written into temporary directories. These tests check
deterministic consumer-visible behaviour only; they make no quality claim and read no held-out prompt or key.
"""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"jevify_pilot_{name}", ROOT / "evals" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load("run")
grade = _load("grade")

CASES = ("c1", "c2")


def _synthetic(label):
    """A labelled synthetic digest: a hash of a test string, never a measured answer key or fixture."""
    return hashlib.sha256(f"synthetic:{label}".encode()).hexdigest()


# Each case is a hypothetical independent item with its own synthetic answer key and fixture digest.
KEYS = {case: _synthetic(f"answer-key-{case}") for case in CASES}
FIXTURES = {case: _synthetic(f"fixture-{case}") for case in CASES}
RUBRIC = "b" * 64
RUN_STARTED = "2026-10-13T12:00:00Z"
THRESHOLDS = {"schema": 1, "confidence": 0.95, "min_cases": 2,
              "metrics": {"correctness": "action_accuracy", "coverage": "action_coverage",
                          "unsafe": "unsupported_fix_now_claims"},
              "rules": {"correctness_min_delta": 0.1, "coverage_max_regression": 0.05, "unsafe_max_increase": 0}}
THRESHOLDS_SHA = run.load_thresholds(json.dumps(THRESHOLDS).encode())[1]
GRADER_IDS = {"source_sha256": "s", "cases_sha256": "c", "extraction_rubric_sha256": {"2": "r"},
              "fixture_sources": {"fixture.py": "f"}, "grader_sources": {"findings.py": "g"},
              "scoring_revision": "structured-findings-fixed-denominators-v4", "historical_revision": "h"}


def _args(strategy, arm="skill"):
    return SimpleNamespace(model=None, thinking=None, cache_mode="cold", max_time=420, max_calls=40, retries=1,
                           reps=3, authorize_attempts=None, authorize_questions=None, arm=arm, strategy=strategy)


def _contract(strategy, pilot_sha, arm="skill", bundle="b" * 64):
    source = {"kind": "worktree", "source_manifest_sha256": "s" * 64,
              "executable": {"sha256": "e" * 64, "version": "x", "path": "/bin/omp"}}
    return run.comparison_contract(_args(strategy, arm), cases_sha="c" * 64, selected_ids=list(CASES),
                                   identities={case: {} for case in CASES}, source=source,
                                   skill={"materialized_sha256": bundle if arm == "skill" else None},
                                   runner={"sha256": "r" * 64, "python": "3"}, cache_seed=None,
                                   thresholds_sha=THRESHOLDS_SHA, concurrency=1, pilot_sha256=pilot_sha)


def _row(case, *, action=0.5, coverage=1.0, unsupported=(), fp=0, fn=0, planned=1, predicted=1, unresolved=0,
         key=None, fixture=None):
    """One synthetic graded row; key and fixture default to the case's own synthetic identity."""
    return {"case": case, "final_attempt": True, "key_schema": 2, "action_accuracy": action,
            "action_coverage": coverage,
            "factual_accuracy": 1.0, "factual_coverage": 1.0, "introduced_accuracy": 1.0,
            "introduced_coverage": 1.0, "recall": 1.0, "precision": 1.0,
            "repair": {"unsupported_fix_now_claims": list(unsupported), "fp": fp, "fn": fn,
                       "planned_defects": planned, "predicted_defects": predicted},
            "unresolved_items": unresolved, "evidence_unresolved_items": [],
            "key_identity": {"key_schema": 2, "key_sha256": key or KEYS[case],
                             "fixture_sha256": fixture or FIXTURES[case],
                             "public_identity": {"review/rubric.json": RUBRIC}},
            "key_binding": {"status": "matched", "captured": {}}}


def _graded(strategy, rows, *, pilot=None, execution=None):
    """A graded result with the controlled contract shape compare_graded reads."""
    contract = _contract(strategy, None if pilot is None else pilot["sha256"])
    return {"results": f"{strategy}.json", "input_sha256": "h", "arm": "skill", "strategy": strategy,
            "identities": GRADER_IDS, "runs": rows, "control_runs": [],
            "run_manifest": {"pilot": pilot,
                             "predeclared_thresholds": {"sha256": THRESHOLDS_SHA, "thresholds": THRESHOLDS}},
            "comparison_contract": contract, "comparison_contract_sha256": run.digest_json(contract),
            "execution": {"runs": [], "cost_ledger": None,
                          "metadata": {"models_seen": ["synthetic-model"], "thinking_seen": ["off"]},
                          **(execution or {})}}


def _supported_rows():
    return ([_row("c1", action=0.2), _row("c2", action=0.2)], [_row("c1", action=0.8), _row("c2", action=0.8)])


class PilotFixture(unittest.TestCase):
    """A synthetic manifest directory with gold evidence files. No real key, prompt or holdout is involved."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "gold").mkdir()
        self.evidence = {}
        for case in CASES:
            data = f"synthetic adjudication record for {case}\n".encode()
            (self.dir / "gold" / f"{case}.json").write_bytes(data)
            self.evidence[case] = hashlib.sha256(data).hexdigest()
        self.identities = {case: {"key_sha256": KEYS[case], "public_rubric_sha256": RUBRIC,
                                  "fixture_sha256": FIXTURES[case]} for case in CASES}

    def _gold(self, case, **changes):
        gold = {"establishment": "independent_adjudication", "key_author": "fixture-author-1",
                "key_sha256": KEYS[case], "adjudicator": {"kind": "human", "id": "reviewer-7"},
                "adjudicated_at": "2026-10-02T00:00:00Z",
                "evidence": {"path": f"gold/{case}.json", "sha256": self.evidence[case]}}
        gold.update(changes)
        return gold

    def _doc(self, **changes):
        doc = {"schema": "jevify-pilot/1", "pilot_id": "synthetic-pilot", "phase": "heldout",
               "rubric_frozen_at": "2026-10-01T00:00:00Z", "frozen_at": "2026-10-03T00:00:00Z",
               "thresholds_sha256": THRESHOLDS_SHA, "comparison": {"base": "auto", "candidate": "direct-script"},
               "cases": {case: {"split": "heldout", "exposure": "unexposed", "key_sha256": KEYS[case],
                                "public_rubric_sha256": RUBRIC, "gold": self._gold(case)} for case in CASES}}
        doc.update(changes)
        return doc

    def _raw(self, raw, **changes):
        options = {"path": self.dir / "pilot.json", "selected_ids": list(CASES), "identities": self.identities,
                   "thresholds_sha": THRESHOLDS_SHA, "cases_path": self.dir / "cases.json",
                   "run_started": RUN_STARTED}
        options.update(changes)
        return run.load_pilot(raw, **options)

    def _load(self, doc, **changes):
        return self._raw(json.dumps(doc).encode(), **changes)

    def _compare(self, record, base_rows, candidate_rows, *, base="auto", candidate="direct-script"):
        return grade.compare_graded(_graded(base, base_rows, pilot=record),
                                    _graded(candidate, candidate_rows, pilot=record))


class PilotManifestTests(PilotFixture):
    def test_valid_heldout_manifest_binds_keys_rubric_and_retains_exact_bytes(self):
        record, retained = self._load(self._doc())
        self.assertEqual(record["independence"], {"independent_cases": 2, "cases": 2, "all_independent": True})
        self.assertEqual(record["attestation"], "operator_attested_unauthenticated")
        self.assertEqual(record["cases"]["c1"]["gold"]["status"], "attested_independent_adjudication")
        self.assertEqual(record["cases"]["c1"]["key_sha256"], KEYS["c1"])
        self.assertEqual(len(retained), 3)  # manifest bytes plus one evidence file per case

    def test_canonical_digest_ignores_key_order_but_not_content(self):
        first, _ = self._load(self._doc())
        second, _ = self._load(dict(reversed(list(self._doc().items()))))
        self.assertEqual(first["sha256"], second["sha256"])
        changed, _ = self._load(self._doc(pilot_id="other-pilot"))
        self.assertNotEqual(first["sha256"], changed["sha256"])

    def test_undeclared_or_missing_fields_fail_closed(self):
        doc = self._doc()
        with self.assertRaisesRegex(ValueError, r"unknown \['extra'\]"):
            self._load({**doc, "extra": 1})
        with self.assertRaisesRegex(ValueError, r"missing \['frozen_at'\]"):
            self._load({k: v for k, v in doc.items() if k != "frozen_at"})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            self._raw(b"[]")
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self._raw(b" " * (run.PILOT_MAX_BYTES + 1))

    def test_a_development_case_cannot_pass_as_heldout(self):
        doc = self._doc()
        doc["cases"]["c1"]["split"] = "development"
        with self.assertRaisesRegex(ValueError, "splits cannot mix"):
            self._load(doc)

    def test_tuned_or_exposed_cases_are_refused_as_held_out(self):
        for exposure in ("tuned", "exposed"):
            with self.subTest(exposure=exposure):
                doc = self._doc()
                doc["cases"]["c2"]["exposure"] = exposure
                with self.assertRaisesRegex(ValueError, "only unexposed"):
                    self._load(doc)

    def test_the_sealed_holdout_path_is_refused_for_held_out_evidence(self):
        # Only the path is compared; the holdout file is never opened.
        with self.assertRaisesRegex(ValueError, "sealed holdout"):
            self._load(self._doc(), cases_path=run.HERE / "holdout.json")

    def test_key_and_rubric_digests_must_equal_the_launch_fixture(self):
        doc = self._doc()
        doc["cases"]["c1"]["key_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "key digest differs"):
            self._load(doc)
        doc = self._doc()
        doc["cases"]["c1"]["public_rubric_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "rubric digest differs"):
            self._load(doc)

    def test_case_set_must_equal_the_selected_cases(self):
        with self.assertRaisesRegex(ValueError, r"unknown \['c2'\]"):
            self._load(self._doc(), selected_ids=["c1"])

    def test_gold_must_bind_the_key_and_name_a_separate_adjudicator(self):
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["key_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "different answer key"):
            self._load(doc)
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["adjudicator"] = {"kind": "human", "id": "Fixture-Author-1"}
        with self.assertRaisesRegex(ValueError, "independent of the key author"):
            self._load(doc)
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["adjudicator"] = {"kind": "agent", "id": "jevify-repository"}
        with self.assertRaisesRegex(ValueError, "independent of the key author"):
            self._load(doc)
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["adjudicated_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "after the pilot was frozen"):
            self._load(doc)

    def test_gold_evidence_bytes_must_match_and_stay_inside_the_manifest_directory(self):
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["evidence"]["sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "do not match"):
            self._load(doc)
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["evidence"]["path"] = "../escape.json"
        with self.assertRaisesRegex(ValueError, "relative to the manifest"):
            self._load(doc)
        doc = self._doc()
        doc["cases"]["c1"]["gold"]["evidence"]["path"] = "gold/absent.json"
        with self.assertRaisesRegex(ValueError, "existing file"):
            self._load(doc)

    def test_evidence_identical_to_a_fixture_is_not_separate_adjudication(self):
        identities = {case: {**self.identities[case], "fixture_sha256": self.evidence["c1"]} for case in CASES}
        with self.assertRaisesRegex(ValueError, "not separate adjudication"):
            self._load(self._doc(), identities=identities)

    def test_manifest_must_be_frozen_after_the_rubric_and_before_the_run(self):
        with self.assertRaisesRegex(ValueError, "before this run starts"):
            self._load(self._doc(frozen_at="2026-10-13T12:00:00Z"))
        with self.assertRaisesRegex(ValueError, "precede rubric_frozen_at"):
            self._load(self._doc(frozen_at="2026-09-30T00:00:00Z"))

    def test_heldout_needs_the_identical_predeclared_rule(self):
        with self.assertRaisesRegex(ValueError, "predeclared --thresholds"):
            self._load(self._doc(thresholds_sha256=None), thresholds_sha=None)
        with self.assertRaisesRegex(ValueError, "must equal the digest"):
            self._load(self._doc(thresholds_sha256="9" * 64))

    def test_absent_gold_is_explicitly_non_independent_and_never_filled_in(self):
        cases = {case: {**entry, "split": "development", "gold": None}
                 for case, entry in self._doc()["cases"].items()}
        record, _ = self._load(self._doc(phase="development", thresholds_sha256=None, cases=cases),
                               thresholds_sha=None)
        self.assertEqual(record["cases"]["c1"]["gold"], {"status": "absent_non_independent", "attestation": None})
        self.assertFalse(record["independence"]["all_independent"])


class PilotComparisonTests(PilotFixture):
    def test_a_supported_heldout_pilot_maps_to_its_method_outcome(self):
        record, _ = self._load(self._doc())
        comparison = self._compare(record, *_supported_rows())
        self.assertEqual(comparison["statistical_result"]["verdict"], "supports_candidate",
                         comparison["statistical_result"])
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "use_direct_script", decision)
        self.assertTrue(all(decision["gates"].values()), decision["gates"])
        self.assertEqual(decision["attestation"], "operator_attested_unauthenticated")

    def test_a_development_pilot_never_recommends_a_method(self):
        cases = {case: {**entry, "split": "development"} for case, entry in self._doc()["cases"].items()}
        record, _ = self._load(self._doc(phase="development", thresholds_sha256=None, cases=cases),
                               thresholds_sha=None)
        decision = self._compare(record, *_supported_rows())["method_recommendation"]
        self.assertEqual(decision["verdict"], "withheld")
        self.assertIn("the pilot phase is not heldout", decision["reasons"])

    def test_gold_that_is_absent_withholds_even_when_the_statistics_support(self):
        cases = {case: {**entry, "gold": None} for case, entry in self._doc()["cases"].items()}
        record, _ = self._load(self._doc(cases=cases))
        comparison = self._compare(record, *_supported_rows())
        self.assertEqual(comparison["statistical_result"]["verdict"], "supports_candidate")
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "withheld")
        self.assertIn("gold is not independently attested for every case", decision["reasons"])

    def test_results_that_ran_another_strategy_than_declared_are_not_comparable(self):
        record, _ = self._load(self._doc())
        base_rows, _ = _supported_rows()
        comparison = self._compare(record, base_rows, _supported_rows()[1], candidate="jev-flat")
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "not_comparable")
        self.assertIn("run strategies differ from the manifest comparison", decision["reasons"])

    def test_results_under_different_pilot_manifests_are_not_comparable(self):
        record, _ = self._load(self._doc())
        other, _ = self._load(self._doc(pilot_id="other-pilot"))
        base_rows, candidate_rows = _supported_rows()
        comparison = grade.compare_graded(_graded("auto", base_rows, pilot=record),
                                          _graded("direct-script", candidate_rows, pilot=other))
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "not_comparable")
        self.assertIn("results were not run under one pilot manifest", decision["reasons"])

    def test_a_result_without_the_pilot_manifest_is_withheld_and_still_reports_observed_outcomes(self):
        base_rows, candidate_rows = _supported_rows()
        comparison = grade.compare_graded(_graded("auto", base_rows), _graded("direct-script", candidate_rows))
        self.assertEqual(comparison["method_recommendation"]["verdict"], "withheld")
        self.assertIn("candidate", comparison["observed_outcomes"])

    def test_observed_model_or_thinking_differences_are_not_comparable(self):
        record, _ = self._load(self._doc())
        base_rows, candidate_rows = _supported_rows()
        candidate = _graded("direct-script", candidate_rows, pilot=record)
        candidate["execution"]["metadata"]["models_seen"] = ["other-model"]
        comparison = grade.compare_graded(_graded("auto", base_rows, pilot=record), candidate)
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "not_comparable")
        self.assertIn("observed model or thinking level differs, or is unrecorded on a side", decision["reasons"])

    def test_a_historical_contract_without_the_pilot_field_fails_closed(self):
        record, _ = self._load(self._doc())
        base_rows, candidate_rows = _supported_rows()
        base = _graded("auto", base_rows, pilot=record)
        del base["comparison_contract"]["controlled"]["pilot"]
        comparison = grade.compare_graded(base, _graded("direct-script", candidate_rows, pilot=record))
        self.assertIn("pilot", comparison["contract_check"]["controlled_differences"])
        self.assertEqual(comparison["method_recommendation"]["verdict"], "not_comparable")

    def test_a_candidate_that_lowers_correctness_contradicts_the_method(self):
        record, _ = self._load(self._doc())
        comparison = self._compare(record, *reversed(_supported_rows()))
        self.assertEqual(comparison["statistical_result"]["verdict"], "contradicts_candidate")
        self.assertEqual(comparison["method_recommendation"]["verdict"], "contradicts_candidate")

    def test_a_noisy_gain_is_inconclusive_not_a_recommendation(self):
        record, _ = self._load(self._doc())
        comparison = self._compare(record, [_row("c1", action=0.2), _row("c2", action=0.2)],
                                   [_row("c1", action=0.8), _row("c2", action=0.2)])
        self.assertEqual(comparison["statistical_result"]["verdict"], "inconclusive")
        self.assertEqual(comparison["method_recommendation"]["verdict"], "inconclusive")

    def test_self_authored_support_is_never_exposed_as_a_recommendation(self):
        cases = {case: {**entry, "gold": None} for case, entry in self._doc()["cases"].items()}
        record, _ = self._load(self._doc(cases=cases))
        comparison = self._compare(record, *_supported_rows())
        self.assertNotIn("recommendation", comparison)
        self.assertEqual(comparison["statistical_result"]["verdict"], "supports_candidate")
        self.assertEqual(comparison["method_recommendation"]["verdict"], "withheld")
        self.assertIsNone(comparison["method_recommendation"]["strategy"])

    def test_a_contradicted_jev_candidate_recommends_the_direct_baseline(self):
        record, _ = self._load(self._doc(comparison={"base": "direct-script", "candidate": "jev-flat"}))
        comparison = self._compare(record, [_row("c1", action=0.8), _row("c2", action=0.8)],
                                   [_row("c1", action=0.2), _row("c2", action=0.2)],
                                   base="direct-script", candidate="jev-flat")
        self.assertEqual(comparison["statistical_result"]["verdict"], "contradicts_candidate")
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "use_direct_script", decision)
        self.assertEqual(decision["strategy"], "direct-script")
        self.assertEqual((decision["candidate"], decision["baseline"]), ("jev-flat", "direct-script"))
        self.assertEqual(comparison["reversed_statistical_result"]["verdict"], "supports_candidate")
        self.assertTrue(all(decision["gates"].values()), decision["gates"])

    def test_a_contradicted_candidate_over_an_auto_baseline_names_no_method(self):
        record, _ = self._load(self._doc())
        comparison = self._compare(record, [_row("c1", action=0.8), _row("c2", action=0.8)],
                                   [_row("c1", action=0.2), _row("c2", action=0.2)])
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "contradicts_candidate")
        self.assertIsNone(decision["strategy"])

    def test_an_unsafe_candidate_over_a_much_less_correct_baseline_names_no_method(self):
        # Forward: correctness rises, but unsafe claims rise too, so the candidate is contradicted.
        # Reversed: the much less correct baseline cannot be recommended, so no method is named.
        record, _ = self._load(self._doc(comparison={"base": "direct-script", "candidate": "jev-flat"}))
        comparison = self._compare(record,
                                   [_row("c1", action=0.2), _row("c2", action=0.2)],
                                   [_row("c1", action=0.8, unsupported=["C01"]),
                                    _row("c2", action=0.8, unsupported=["C02"])],
                                   base="direct-script", candidate="jev-flat")
        self.assertEqual(comparison["statistical_result"]["verdict"], "contradicts_candidate")
        self.assertEqual(comparison["reversed_statistical_result"]["verdict"], "contradicts_candidate")
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "contradicts_candidate", decision)
        self.assertIsNone(decision["strategy"])

    def test_a_baseline_is_not_recommended_when_the_reversed_rule_does_not_clear(self):
        # Forward contradicts on coverage; reversed coverage favours the baseline but correctness does not clear.
        record, _ = self._load(self._doc(comparison={"base": "direct-script", "candidate": "jev-flat"}))
        comparison = self._compare(record, [_row("c1"), _row("c2")],
                                   [_row("c1", coverage=0.5), _row("c2", coverage=0.5)],
                                   base="direct-script", candidate="jev-flat")
        self.assertEqual(comparison["statistical_result"]["verdict"], "contradicts_candidate")
        self.assertEqual(comparison["reversed_statistical_result"]["verdict"], "inconclusive")
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "contradicts_candidate", decision)
        self.assertIsNone(decision["strategy"])

    def test_two_case_ids_over_one_answer_key_are_one_piece_of_evidence(self):
        alias = _synthetic("answer-key-shared-by-two-case-ids")
        doc = self._doc()
        for case in CASES:
            doc["cases"][case]["key_sha256"] = alias
            doc["cases"][case]["gold"]["key_sha256"] = alias
        identities = {case: {**self.identities[case], "key_sha256": alias} for case in CASES}
        record, _ = self._load(doc, identities=identities)
        comparison = self._compare(record, [_row(case, action=0.2, key=alias) for case in CASES],
                                   [_row(case, action=0.8, key=alias) for case in CASES])
        decision = comparison["method_recommendation"]
        self.assertEqual(decision["verdict"], "withheld", decision)
        self.assertFalse(decision["gates"]["distinct_case_evidence"])
        self.assertIn(grade._PILOT_EVIDENCE["distinct_case_evidence"], decision["reasons"])
        self.assertIsNone(decision["strategy"])

    def test_a_missing_fixture_binding_withholds_and_is_never_inferred(self):
        record, _ = self._load(self._doc())
        base_rows, candidate_rows = _supported_rows()
        del candidate_rows[0]["key_identity"]["fixture_sha256"]
        comparison = self._compare(record, base_rows, candidate_rows)
        decision = comparison["method_recommendation"]
        self.assertNotIn(decision["verdict"], run.METHOD_OUTCOMES.values())
        self.assertFalse(decision["gates"]["case_bindings_recorded"])
        self.assertIsNone(decision["strategy"])

    def test_unsupported_fix_now_claims_contradict_even_when_accuracy_rises(self):
        record, _ = self._load(self._doc())
        candidate = [_row("c1", action=0.8, unsupported=["C01"]), _row("c2", action=0.8, unsupported=["C02"])]
        comparison = self._compare(record, _supported_rows()[0], candidate)
        self.assertEqual(comparison["method_recommendation"]["verdict"], "contradicts_candidate")

    def test_unobserved_quantities_stay_null_and_costs_are_never_imputed(self):
        base = _graded("auto", [_row("c1", action=0.2, planned=2, predicted=0, unresolved=1),
                                _row("c2", action=0.2, planned=2, predicted=0)])
        candidate = _graded("direct-script", [_row("c1", action=0.8, fp=1, fn=1, planned=2, predicted=2),
                                              _row("c2", action=0.8, planned=2, predicted=2)])
        comparison = grade.compare_graded(base, candidate)
        side = comparison["observed_outcomes"]["candidate"]
        self.assertEqual(side["false_repair_recommendations"], 1)
        self.assertEqual(side["misses"], 1)
        self.assertEqual(side["miss_rate"], 0.25)
        self.assertIsNone(side["review_burden"]["manual_review_minutes"])
        self.assertIsNone(side["latency_secs"]["mean_total"])
        self.assertEqual(side["observed_cost"], {"observed_subtotal_usd": None, "ledger_entries": 0,
                                                 "entries_with_observed_cost": None, "coverage": None,
                                                 "monetary_status": None, "billing_complete": False,
                                                 "imputed": False})
        self.assertEqual(comparison["observed_outcomes"]["base"]["review_burden"]["unresolved_items"], 1)

    def test_observed_cost_reports_its_coverage_without_claiming_billing_completeness(self):
        ledger = {"monetary_status": "incomplete", "totals": {"total": {"reported_subtotal": 0.5}},
                  "entries": [{"cost_unknown": False, "reported_cost": 0.5},
                              {"cost_unknown": True, "reported_cost": None}]}
        execution = {"runs": [{"case": "c1", "rep": 1, "total_secs": 3.0},
                              {"case": "c2", "rep": 1, "total_secs": 5.0}], "cost_ledger": ledger}
        base = _graded("auto", _supported_rows()[0], execution=execution)
        comparison = grade.compare_graded(base, _graded("direct-script", _supported_rows()[1]))
        side = comparison["observed_outcomes"]["base"]
        self.assertEqual(side["observed_cost"]["observed_subtotal_usd"], 0.5)
        self.assertEqual(side["observed_cost"]["coverage"], 0.5)
        self.assertFalse(side["observed_cost"]["billing_complete"])
        self.assertEqual(side["latency_secs"], {"runs": 2, "mean_total": 4.0})


if __name__ == "__main__":
    unittest.main()
