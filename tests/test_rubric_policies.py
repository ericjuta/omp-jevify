"""Behavioral contracts for ordered rubric identity, score evidence, policies, calibration, and pilots."""

import contextlib
import copy
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
# The %load-oriented script announces itself on execution; keep test output quiet.
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]


class _StateIsolation:
    def setUp(self):
        previous_stats, previous_frozen = jv.stats, jv.frozen
        self.addCleanup(setattr, jv, "stats", previous_stats)
        self.addCleanup(setattr, jv, "frozen", previous_frozen)
        jv.stats = {"states": {}, "runs": []}
        jv.frozen = None


def _item(answers=None, *, error=None, cache="miss", questions=1, cached_questions=0):
    return SimpleNamespace(
        ok=error is None, answers=answers, error=error, model="jev-primary",
        selected_model="jev-primary", primary_model="jev-primary", fallback=False,
        response_model="jev-primary", cache=cache, questions=questions,
        cached_questions=cached_questions)


class _FakeBatch:
    """Completed judge_batch handle with the drain, status, cancel, and close interface run() uses."""

    identifiers = itertools.count(1)

    def __init__(self, payload, items):
        self.id = f"rubric-batch-{next(self.identifiers)}"
        self.total = len(payload)
        self.items = items
        for key, item in items.items():
            item.key = key
        self.closed = False
        self.cancelled = False

    async def drain(self, timeout):
        return list(self.items.items())

    async def drain_iter(self, timeout):
        for entry in await self.drain(timeout):
            yield entry

    def status(self):
        return {"id": self.id, "done": len(self.items), "total": self.total,
                "failed": sum(not item.ok for item in self.items.values()),
                "cost": 0.0, "running": False, "elapsedS": 0}

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def _fake_judge(answer_for):
    """answer_for(key, state, submitted_questions) returns the item for one payload key."""
    def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
        return _FakeBatch(payload, {key: answer_for(key, state, submitted_questions)
                                    for key, state in payload.items()})
    return judge_batch


class OrderedIdentityTests(_StateIsolation, unittest.TestCase):
    def _rubric(self):
        return {
            "verdict": jv.choice("Assess the symbol", KEEP="consumer contract", PURGE="unused"),
            "risk": jv.yes("Is there harm?", true="harm", false="none"),
        }

    def test_question_and_choice_order_are_part_of_the_identity(self):
        original = self._rubric()
        criteria_swapped = {
            "verdict": {"type": "choice", "instructions": "Assess the symbol",
                        "criteria": {"PURGE": "unused", "KEEP": "consumer contract"}},
            "risk": original["risk"],
        }
        questions_swapped = {"risk": original["risk"], "verdict": original["verdict"]}
        self.assertNotEqual(jv.qhash(original), jv.qhash(criteria_swapped))
        self.assertNotEqual(jv.qhash(original), jv.qhash(questions_swapped))

    def test_field_insertion_order_inside_a_question_is_not_identity(self):
        original = self._rubric()
        rebuilt = {"verdict": dict(reversed(list(original["verdict"].items()))),
                   "risk": original["risk"]}
        self.assertEqual(jv.qhash(original), jv.qhash(rebuilt))

    def test_order_insensitive_identity_groups_only_reorderings_of_one_rubric(self):
        original = self._rubric()
        criteria_swapped = {
            "verdict": {"type": "choice", "instructions": "Assess the symbol",
                        "criteria": {"PURGE": "unused", "KEEP": "consumer contract"}},
            "risk": original["risk"],
        }
        self.assertEqual(jv.qhash(original, ordered=False), jv.qhash(criteria_swapped, ordered=False))
        changed = copy.deepcopy(original)
        changed["verdict"]["criteria"]["PURGE"] = "changed rubric"
        self.assertNotEqual(jv.qhash(original, ordered=False), jv.qhash(changed, ordered=False))

    def test_freeze_writes_insertion_order_and_binds_the_identity(self):
        rubric = self._rubric()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "questions.json"
        digest = jv.freeze(rubric, path=str(path))
        self.assertEqual(digest, jv.qhash(rubric))
        self.assertEqual(jv.frozen, digest)
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index('"verdict"'), text.index('"risk"'))
        self.assertLess(text.index('"KEEP"'), text.index('"PURGE"'))
        reloaded = json.loads(text)
        self.assertEqual(list(reloaded), ["verdict", "risk"])
        self.assertEqual(jv.qhash(reloaded), digest)


class ScoreEvidenceTests(_StateIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.Q = {"severity": jv.scale("Severity", "low", "mid", "high")}

    def _score(self, **fields):
        answer = {"type": "score", "score": 1.0, "confidence": 0.6}
        answer.update(fields)
        return answer

    def test_supplied_distribution_and_legend_are_kept_in_level_order(self):
        row = {}
        jv._answer(row, "severity", self.Q["severity"], self._score(
            score=1.25, probabilities={"2": 0.5, "0": 0.25, "1": 0.25}, legend=["low", "mid", "high"]))
        self.assertEqual(row["severity"], 1.25)
        self.assertEqual(row["severity.conf"], 0.6)
        self.assertEqual(row["severity.dist"], [0.25, 0.25, 0.5])
        self.assertEqual(row["severity.legend"], ["low", "mid", "high"])

    def test_absent_distribution_and_legend_are_explicitly_unavailable(self):
        row = {}
        jv._answer(row, "severity", self.Q["severity"], self._score(score=1.0))
        self.assertIn("severity.dist", row)
        self.assertIsNone(row["severity.dist"])
        self.assertIn("severity.legend", row)
        self.assertIsNone(row["severity.legend"])

    def test_malformed_distributions_are_rejected_without_partial_columns(self):
        malformed = {
            "missing level": {"0": 0.5, "1": 0.5},
            "extra level": {"0": 0.25, "1": 0.25, "2": 0.25, "3": 0.25},
            "not normalized": {"0": 0.2, "1": 0.2, "2": 0.2},
            "negative": {"0": -0.5, "1": 1.0, "2": 0.5},
            "above one": {"0": 1.5, "1": -0.5, "2": 0.0},
            "non-finite": {"0": float("nan"), "1": 0.5, "2": 0.5},
            "duplicate level": {"0": 0.5, 0: 0.5, "1": 0.0, "2": 0.0},
            "wrong-length list": [0.5, 0.5],
        }
        for name, probabilities in malformed.items():
            with self.subTest(case=name):
                row = {}
                with self.assertRaises(ValueError):
                    jv._answer(row, "severity", self.Q["severity"],
                               self._score(score=1.0, probabilities=probabilities))
                self.assertEqual(row, {})

    def test_malformed_legends_are_rejected(self):
        for name, legend in {"wrong length": ["low", "high"], "non-text": [1, 2, 3]}.items():
            with self.subTest(case=name):
                row = {}
                with self.assertRaises(ValueError):
                    jv._answer(row, "severity", self.Q["severity"], self._score(score=1.0, legend=legend))
                self.assertEqual(row, {})

    def test_partial_decode_leaves_no_score_columns_behind(self):
        Q = {"severity": self.Q["severity"],
             "verdict": jv.choice("Assess", KEEP="kept", PURGE="removed")}
        item = _item({
            "severity": self._score(score=1.0, probabilities={"0": 0.0, "1": 1.0, "2": 0.0}),
            # The choice answer lacks confidence, so decoding fails after the verdict is partly written.
            "verdict": {"type": "choice", "choice": "KEEP",
                        "probabilities": {"KEEP": 1.0, "PURGE": 0.0}},
        })
        row = jv._flat_row("unit-1", Q, item, jv.qhash(Q))
        self.assertIn("error", row)
        for name in ("severity", "severity.conf", "severity.dist", "severity.legend",
                     "verdict", "verdict.KEEP", "verdict.PURGE", "verdict.conf"):
            self.assertNotIn(name, row)


class PrimitiveUncertaintyTests(_StateIsolation, unittest.TestCase):
    def test_choice_margin_and_host_confidence_are_reported_separately(self):
        Q = {"verdict": jv.choice("Assess", KEEP="kept", PURGE="removed")}
        row = {"verdict": "KEEP", "verdict.KEEP": 0.7, "verdict.PURGE": 0.3, "verdict.conf": 0.9}
        measures = jv.uncertainty(row, "verdict", Q["verdict"])
        self.assertAlmostEqual(measures["top_probability"], 0.7)
        self.assertAlmostEqual(measures["margin"], 0.4)
        self.assertEqual(measures["host_confidence"], 0.9)

    def test_bool_ambiguity_peaks_at_even_odds_and_reports_no_confidence(self):
        Q = {"risk": jv.yes("Risk?")}
        even = jv.uncertainty({"risk": 0.5}, "risk", Q["risk"])
        certain = jv.uncertainty({"risk": 0.95}, "risk", Q["risk"])
        self.assertEqual(even["ambiguity"], 1.0)
        self.assertAlmostEqual(certain["ambiguity"], 0.1)
        self.assertNotIn("host_confidence", even)

    def test_score_spread_separates_opposite_ends_from_concentrated_mass(self):
        Q = {"severity": jv.scale("Severity", "low", "mid", "high")}
        opposite = {"severity": 1.0, "severity.conf": 0.5,
                    "severity.dist": [0.5, 0.0, 0.5], "severity.legend": None}
        concentrated = {"severity": 1.0, "severity.conf": 0.5,
                        "severity.dist": [0.0, 1.0, 0.0], "severity.legend": None}
        self.assertAlmostEqual(jv.uncertainty(opposite, "severity", Q["severity"])["spread"], 1.0)
        self.assertAlmostEqual(jv.uncertainty(concentrated, "severity", Q["severity"])["spread"], 0.0)

    def test_missing_score_distribution_is_unavailable_not_zero_spread(self):
        Q = {"severity": jv.scale("Severity", "low", "mid", "high")}
        row = {"severity": 1.0, "severity.conf": 0.5, "severity.dist": None, "severity.legend": None}
        measures = jv.uncertainty(row, "severity", Q["severity"])
        self.assertIsNone(measures["spread"])
        self.assertFalse(measures["distribution_available"])


class ReviewPolicyTests(_StateIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.Q = {"verdict": jv.choice("Assess", KEEP="kept", PURGE="removed", UNKNOWN="unclear"),
                  "risk": jv.yes("Risk?"),
                  "severity": jv.scale("Severity", "low", "mid", "high")}
        self.spec = jv.policy(self.Q, {"verdict": {"top": 0.8, "margin": 0.2},
                                       "risk": {"band": [0.4, 0.6]},
                                       "severity": {"spread": 0.5}})

    def _row(self, verdict=(0.85, 0.1, 0.05), p_yes=0.9, dist=(0.0, 1.0, 0.0)):
        return {"id": "unit", "verdict": "KEEP", "verdict.KEEP": verdict[0],
                "verdict.PURGE": verdict[1], "verdict.UNKNOWN": verdict[2], "verdict.conf": 0.9,
                "risk": p_yes, "severity": 1.0, "severity.conf": 0.5,
                "severity.dist": None if dist is None else list(dist), "severity.legend": None}

    def test_rule_escalates_each_primitive_with_its_own_reason(self):
        rule = jv.policy_rule(self.spec, self.Q)
        self.assertIsNone(rule(self._row()))
        self.assertEqual(rule(self._row(verdict=(0.7, 0.2, 0.1))), "verdict:low_top_probability")
        self.assertEqual(rule(self._row(verdict=(0.45, 0.4, 0.15))),
                         "verdict:low_top_probability; verdict:low_margin")
        self.assertEqual(rule(self._row(p_yes=0.5)), "risk:ambiguous_bool")
        self.assertEqual(rule(self._row(dist=(0.5, 0.0, 0.5))), "severity:wide_score")
        self.assertEqual(rule(self._row(dist=None)), "severity:score_distribution_unavailable")
        self.assertIsNone(rule({"id": "failed", "error": "provider failed"}))

    def test_policy_is_bound_to_the_exact_ordered_question_identity(self):
        changed = copy.deepcopy(self.Q)
        changed["verdict"]["criteria"] = dict(reversed(list(changed["verdict"]["criteria"].items())))
        with self.assertRaisesRegex(ValueError, "different question hash"):
            jv.policy_rule(self.spec, changed)

    def test_policy_rejects_unknown_questions_wrong_fields_and_inverted_bands(self):
        with self.assertRaisesRegex(ValueError, "unknown question"):
            jv.policy(self.Q, {"nope": {"top": 0.5}})
        with self.assertRaisesRegex(ValueError, "accepts"):
            jv.policy(self.Q, {"risk": {"top": 0.5}})
        with self.assertRaisesRegex(ValueError, "lo <= hi"):
            jv.policy(self.Q, {"risk": {"band": [0.7, 0.3]}})


class ScoreHistogramTests(_StateIsolation, unittest.TestCase):
    def test_level_bins_place_each_whole_level_in_its_own_bin(self):
        rows = [{"id": str(index), "severity": value}
                for index, value in enumerate([0.0, 1.4, 2.0, 1.6])]
        rows.append({"id": "failed", "error": "provider failed"})
        with contextlib.redirect_stdout(io.StringIO()):
            result = jv.tally(rows, "severity", levels=3)
        self.assertEqual(result, {"level 0": 1, "level 1": 1, "level 2": 2, "missing/error": 1})

    def test_default_bins_refuse_weighted_scores_instead_of_funneling_them(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "explicit edges or levels"):
                jv.tally([{"id": "a", "severity": 2.0}], "severity")

    def test_explicit_edges_report_values_outside_them(self):
        rows = [{"id": "a", "severity": 2.5}, {"id": "b", "severity": 0.5}]
        with contextlib.redirect_stdout(io.StringIO()):
            result = jv.tally(rows, "severity", edges=(0, 1, 2))
        self.assertEqual(result["[0, 1)"], 1)
        self.assertEqual(result["outside bins"], 1)

    def test_levels_and_edges_cannot_both_define_bins(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "not both"):
                jv.tally([{"id": "a", "severity": 1.0}], "severity", levels=3, edges=(0, 1, 2))


class CalibrationEvidenceTests(_StateIsolation, unittest.IsolatedAsyncioTestCase):
    async def _judge(self, Q, states, answer_for):
        with mock.patch.dict(namespace, {"judge_batch": _fake_judge(answer_for)}), \
                contextlib.redirect_stdout(io.StringIO()):
            return await jv.run(states, Q, intent="calibration evidence")

    @staticmethod
    def _opinions(opinion_for_text):
        """Second-model completion keyed by the exact state text it receives."""
        seen = []

        def completion(prompt, *, model, system, schema):
            text = json.loads(prompt)
            seen.append(text)
            return {"verdict": opinion_for_text[text], "reason": "second opinion"}
        completion.seen = seen
        return completion

    async def test_bool_calibration_samples_by_label_probability_and_records_evidence(self):
        Q = {"risk": jv.yes("Is there harm?")}
        states = {"a": "clearly harmful", "b": "clearly harmless"}
        probability = {"a": 0.9, "b": 0.2}
        rows = await self._judge(Q, states, lambda key, state, questions: _item(
            {"risk": {"type": "bool", "bool": probability[key]}}))
        completion = self._opinions({"clearly harmful": "yes", "clearly harmless": "no"})
        slow = mock.Mock(side_effect=completion)
        wait = mock.Mock(side_effect=lambda handles, *, raise_errors=False: handles)
        with mock.patch.dict(namespace, {"completion": slow, "wait": wait}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = jv.calibrate(rows, states, Q, qid="risk", label="yes",
                                  edges=(0, 0.5, 1.01), per_bin=10)

        self.assertEqual([bucket["agreement"] for bucket in result["bins"]], [0.0, 1.0])
        evidence = result["evidence"]
        self.assertEqual(evidence["qhash"], jv.qhash(Q))
        self.assertEqual(evidence["criteria"]["strata"], [[0, 0.5], [0.5, 1.01]])
        json.dumps(evidence)
        self.assertIn("not correctness", evidence["claim"])
        self.assertEqual(evidence["sampled_ids"], ["a", "b"])
        self.assertEqual(evidence["overhead"]["slow_calls"], 2)
        self.assertEqual(evidence["population"], {"rows": 2, "supported": 2, "unsupported": 0})
        records = {record["id"]: record for record in evidence["sampled"]}
        self.assertEqual({key: record["final"]["status"] for key, record in records.items()},
                         {"a": "agree", "b": "disagree"})
        self.assertEqual({key: record["stratum"] for key, record in records.items()}, {"a": 1, "b": 0})
        self.assertEqual(records["a"]["initial"]["p"], 0.9)
        self.assertTrue(all(len(record["context"]["digest"]) == 12 for record in records.values()))

    async def test_score_calibration_bins_supplied_distributions_and_skips_unsupported_rows(self):
        Q = {"severity": jv.scale("Severity", "low", "high")}
        states = {"a": "dense finding", "b": "no distribution supplied"}

        def answer_for(key, state, questions):
            if key == "a":
                return _item({"severity": {"type": "score", "score": 0.9, "confidence": 0.8,
                                           "probabilities": {"0": 0.1, "1": 0.9}}})
            return _item({"severity": {"type": "score", "score": 0.5, "confidence": 0.4}})

        rows = await self._judge(Q, states, answer_for)
        completion = self._opinions({"dense finding": "1"})
        slow = mock.Mock(side_effect=completion)
        wait = mock.Mock(side_effect=lambda handles, *, raise_errors=False: handles)
        with mock.patch.dict(namespace, {"completion": slow, "wait": wait}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = jv.calibrate(rows, states, Q, qid="severity", label=1,
                                  edges=(0, 0.5, 1.01), per_bin=10)

        evidence = result["evidence"]
        self.assertEqual(evidence["population"], {"rows": 2, "supported": 1, "unsupported": 1})
        self.assertEqual(completion.seen, ["dense finding"])
        self.assertEqual(evidence["sampled_ids"], ["a"])
        self.assertNotIn("b", result["verdicts"])
        self.assertEqual(evidence["sampled"][0]["final"]["status"], "agree")
        self.assertEqual(result["bins"][1]["agreement"], 1.0)
        self.assertIsNone(result["bins"][0]["agreement"])

    async def test_second_model_failures_are_unresolved_not_agreement(self):
        Q = {"risk": jv.yes("Risk?")}
        states = {"a": "risky"}
        rows = await self._judge(Q, states, lambda key, state, questions: _item(
            {"risk": {"type": "bool", "bool": 0.9}}))
        wait = mock.Mock(side_effect=lambda handles, *, raise_errors=False:
                         [RuntimeError("provider down")] * len(handles))
        completion = mock.Mock(return_value={"verdict": "yes", "reason": "unused"})
        with mock.patch.dict(namespace, {"completion": completion, "wait": wait}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = jv.calibrate(rows, states, Q, qid="risk", label="yes",
                                  edges=(0, 0.5, 1.01), per_bin=10)
        evidence = result["evidence"]
        self.assertEqual(evidence["sampled"][0]["final"]["status"], "unresolved")
        self.assertEqual(evidence["overhead"]["unresolved"], 1)
        self.assertEqual(result["bins"][1]["errors"], 1)
        self.assertEqual(result["bins"][1]["calibrated"], 0)
        self.assertIsNone(result["bins"][1]["agreement"])

    async def test_rows_judged_under_another_question_hash_fail_before_any_slow_call(self):
        Q = {"risk": jv.yes("Is there harm?")}
        states = {"a": "risky"}
        rows = await self._judge(Q, states, lambda key, state, questions: _item(
            {"risk": {"type": "bool", "bool": 0.9}}))
        other = {"risk": jv.yes("Is there a different harm?")}
        completion = mock.Mock(side_effect=AssertionError("mismatched rows reached completion"))
        wait = mock.Mock(side_effect=AssertionError("mismatched rows reached wait"))
        with mock.patch.dict(namespace, {"completion": completion, "wait": wait}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "question hash"):
                jv.calibrate(rows, states, other, qid="risk", label="yes")
        completion.assert_not_called()
        wait.assert_not_called()


def _order_sensitive(key, state, submitted_questions):
    """Unit x takes whichever label the host lists first; unit y is always KEEP."""
    labels = list(submitted_questions["verdict"]["criteria"])
    choice = labels[0] if key == "x" else "KEEP"
    return _item({"verdict": {"type": "choice", "choice": choice,
                              "probabilities": {label: 1.0 if label == choice else 0.0 for label in labels},
                              "confidence": 1.0}})


class SensitivityPilotTests(_StateIsolation, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.Q = {"verdict": jv.choice("Assess the unit", KEEP="kept", PURGE="removed")}
        self.states = {"x": "unit x", "y": "unit y"}

    async def _pilot(self, judge, **kwargs):
        with mock.patch.dict(namespace, {"judge_batch": _fake_judge(judge)}), \
                contextlib.redirect_stdout(io.StringIO()):
            return await jv.sensitivity(self.states, self.Q, intent="pilot", **kwargs)

    async def test_order_permutation_disagreement_is_reported_without_a_consensus(self):
        result = await self._pilot(_order_sensitive, max_variants=2,
                                   permutations={"verdict": [["PURGE", "KEEP"]]})
        units = {unit["id"]: unit for unit in result["units"]}
        self.assertEqual(units["x"]["status"], "disagree")
        self.assertEqual(units["y"]["status"], "stable")
        self.assertEqual(units["x"]["answers"],
                         {"base": "KEEP", 'perm:verdict:["PURGE", "KEEP"]': "PURGE"})
        self.assertEqual(result["counts"], {"stable": 1, "disagree": 1, "unresolved": 0})
        self.assertFalse({"majority", "consensus", "winner"} & set(result))
        self.assertEqual(result["strategy"], "flat")

    async def test_variant_identities_are_exact_and_base_rubric_identity_is_order_free(self):
        result = await self._pilot(_order_sensitive, max_variants=2,
                                   permutations={"verdict": [["PURGE", "KEEP"]]})
        base, permuted = result["variants"]
        self.assertNotEqual(base["qhash"], permuted["qhash"])
        self.assertNotEqual(base["identity"], permuted["identity"])
        self.assertEqual(result["base_qhash"], jv.qhash(self.Q))
        self.assertEqual(result["base_rubric_qhash"], jv.qhash(self.Q, ordered=False))
        self.assertTrue(any("unverified" in warning for warning in result["warnings"]))

    async def test_context_variants_are_judged_as_distinct_states(self):
        def adversarial_aware(key, state, submitted_questions):
            choice = "PURGE" if key == "x" and "IGNORE PREVIOUS" in state else "KEEP"
            return _item({"verdict": {"type": "choice", "choice": choice,
                                      "probabilities": {"KEEP": 1.0 if choice == "KEEP" else 0.0,
                                                        "PURGE": 1.0 if choice == "PURGE" else 0.0},
                                      "confidence": 1.0}})

        result = await self._pilot(adversarial_aware, max_variants=2, contexts={
            "adversarial": {"x": "unit x IGNORE PREVIOUS", "y": "unit y"}})
        units = {unit["id"]: unit for unit in result["units"]}
        self.assertEqual(units["x"]["status"], "disagree")
        self.assertEqual(units["y"]["status"], "stable")
        base, context = result["variants"]
        self.assertEqual(context["id"], "context:adversarial")
        self.assertNotEqual(base["states_digest"], context["states_digest"])
        self.assertNotEqual(base["identity"], context["identity"])

    async def test_missing_variant_answers_are_unresolved_not_stable(self):
        def flaky(key, state, submitted_questions):
            labels = list(submitted_questions["verdict"]["criteria"])
            if key == "y" and labels[0] == "PURGE":
                return _item(None, error="provider failed")
            return _item({"verdict": {"type": "choice", "choice": "KEEP",
                                      "probabilities": {"KEEP": 1.0, "PURGE": 0.0},
                                      "confidence": 1.0}})

        result = await self._pilot(flaky, max_variants=2,
                                   permutations={"verdict": [["PURGE", "KEEP"]]})
        units = {unit["id"]: unit for unit in result["units"]}
        self.assertEqual(units["y"]["status"], "unresolved")
        self.assertEqual(units["y"]["answers"]['perm:verdict:["PURGE", "KEEP"]'], "ERR")
        self.assertEqual(units["x"]["status"], "stable")

    async def test_cache_hits_are_warned_because_they_may_reuse_earlier_verdicts(self):
        def cached(key, state, submitted_questions):
            return _item({"verdict": {"type": "choice", "choice": "KEEP",
                                      "probabilities": {"KEEP": 1.0, "PURGE": 0.0},
                                      "confidence": 1.0}}, cache="hit", cached_questions=1)

        result = await self._pilot(cached, max_variants=2, contexts={
            "same": {"x": "unit x", "y": "unit y"}})
        self.assertTrue(any("cache hits" in warning for warning in result["warnings"]))

    async def test_pilot_requires_explicit_bounded_variants_before_any_judge_call(self):
        judge = mock.Mock(side_effect=AssertionError("invalid pilot reached the judge"))
        with mock.patch.dict(namespace, {"judge_batch": judge}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "explicit permutations or contexts"):
                await jv.sensitivity(self.states, self.Q, intent="pilot", max_variants=4)
            with self.assertRaises(TypeError):
                await jv.sensitivity(self.states, self.Q, intent="pilot",
                                     permutations={"verdict": [["PURGE", "KEEP"]]})
            with self.assertRaisesRegex(ValueError, "max_variants must be"):
                await jv.sensitivity(self.states, self.Q, intent="pilot", max_variants=1,
                                     permutations={"verdict": [["PURGE", "KEEP"]]})
            with self.assertRaisesRegex(ValueError, "repeats the base order"):
                await jv.sensitivity(self.states, self.Q, intent="pilot", max_variants=2,
                                     permutations={"verdict": [["KEEP", "PURGE"]]})
            with self.assertRaisesRegex(ValueError, "reorder every label"):
                await jv.sensitivity(self.states, self.Q, intent="pilot", max_variants=2,
                                     permutations={"verdict": [["KEEP"]]})
            with self.assertRaisesRegex(ValueError, "exceed max_variants"):
                await jv.sensitivity(self.states, self.Q, intent="pilot", max_variants=2,
                                     permutations={"verdict": [["PURGE", "KEEP"]]},
                                     contexts={"more": {"x": "a", "y": "b"}})
            with self.assertRaisesRegex(ValueError, "exactly the base unit IDs"):
                await jv.sensitivity(self.states, self.Q, intent="pilot", max_variants=3,
                                     contexts={"bad": {"x": "a", "z": "b"}})
        judge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
