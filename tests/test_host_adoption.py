"""Behavioral contracts for jv.adopt and jv.scalar: built-in judge_batch() output bound to submitted states."""

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]

Q = {"severity": jv.choice("How severe is the defect?", low="minor", high="blocking"),
     "safe": jv.yes("Is it safe to ship?")}
ROUTE_A = "openrouter/typesafe/jev-1.13-20260917"
ROUTE_B = "openrouter/typesafe/jev-1.14-20261014"


def _choice(label, top=0.9):
    other = "high" if label == "low" else "low"
    return {"type": "choice", "choice": label, "confidence": top,
            "probabilities": {label: top, other: round(1 - top, 6)}}


def _answers(label="low", top=0.9, safe=0.2):
    return {"severity": _choice(label, top), "safe": {"type": "bool", "bool": safe}}


def _host_item(key, answers, *, model=ROUTE_A, error=None):
    """What OMP 18.12.5 drains: only key, ok, error, answers and model."""
    return SimpleNamespace(key=key, ok=error is None, error=error,
                           answers=None if error else answers, model=model)


def _full_item(key, answers, *, model=ROUTE_A):
    return SimpleNamespace(key=key, ok=True, error=None, answers=answers, model=model,
                           selected_model=model, primary_model=model, fallback=False,
                           response_model=model, cache="miss", questions=2, cached_questions=0)


class _Batch:
    """Host JudgmentBatch snapshot; draining is forbidden because adopt must stay synchronous."""

    def __init__(self, items, *, total=None, done=None, running=False, cost=0.125, status_id="jdgb-1",
                 intent="host sweep"):
        self.items = items
        self.snapshot = {"id": status_id, "intent": intent,
                         "total": len(items) if total is None else total,
                         "done": len(items) if done is None else done,
                         "failed": sum(not item.ok for item in items.values()),
                         "cost": cost, "running": running, "elapsedS": 1.5, "model": ROUTE_B}

    def results(self):
        return {key: item.answers for key, item in self.items.items() if item.ok}

    def failed(self):
        return {key: item.error for key, item in self.items.items() if not item.ok}

    def status(self):
        return dict(self.snapshot)

    async def drain(self, timeout):
        raise AssertionError("adopt must not drain the host batch")

    def drain_iter(self, timeout):
        raise AssertionError("adopt must not drain the host batch")


class AdoptTests(unittest.TestCase):
    def setUp(self):
        previous_stats, previous_frozen = jv.stats, jv.frozen
        self.addCleanup(setattr, jv, "stats", previous_stats)
        self.addCleanup(setattr, jv, "frozen", previous_frozen)
        jv.stats = {"states": {}, "runs": []}
        jv.frozen = None

    def _adopt(self, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return jv.adopt(*args, **kwargs)

    def test_items_become_run_style_rows_with_one_comparable_model(self):
        states = {"a": "first", "b": "second"}
        rows = self._adopt({"a": _host_item("a", _answers("low", 0.9, 0.2)),
                            "b": _host_item("b", _answers("high", 0.7, 0.8))}, states, Q, intent="adopted")
        self.assertEqual([row["id"] for row in rows], ["a", "b"])
        self.assertEqual({k: rows[0][k] for k in ("severity", "severity.low", "severity.high",
                                                   "severity.conf", "safe")},
                         {"severity": "low", "severity.low": 0.9, "severity.high": 0.1,
                          "severity.conf": 0.9, "safe": 0.2})
        self.assertEqual((rows[1]["severity"], rows[1]["safe"]), ("high", 0.8))
        self.assertTrue(all(row["qhash"] == jv.qhash(Q) and row["model"] == ROUTE_A for row in rows))
        self.assertTrue(all(row["state_sha256"] == jv.state_digest(states[row["id"]]) for row in rows))
        receipt = rows.receipt
        self.assertIs(receipt["comparable"], True)
        self.assertEqual(receipt["served_models"], {ROUTE_A: 2})
        self.assertEqual((receipt["strategy"], receipt["intent"], receipt["units"], receipt["ok"],
                          receipt["errors"], receipt["assembly"]), ("host-judge-batch", "adopted", 2, 2, 0, "single"))
        self.assertEqual((receipt["qhash"], receipt["question_hash"]), (jv.qhash(Q),) * 2)
        self.assertEqual((receipt["unit_ids"], receipt["answered_ids"], receipt["error_ids"]),
                         (["a", "b"], ["a", "b"], []))
        self.assertIs(receipt["complete"], True)
        self.assertIs(receipt["provenance_complete"], False)  # the host exposes no route or cache fields
        self.assertEqual(jv.stats["runs"][-1], receipt)

    def test_drained_items_with_their_batch_keep_served_model_and_batch_cost(self):
        items = {"a": _host_item("a", _answers()), "b": _host_item("b", _answers("high"))}
        batch = _Batch(items, cost=0.25, status_id="jdgb-9")
        rows = self._adopt(items, {"a": "x", "b": "y"}, Q, batch=batch)
        receipt = rows.receipt
        self.assertEqual(receipt["served_models"], {ROUTE_A: 2})
        self.assertEqual((receipt["cost"], receipt["cost_known"], receipt["batch_ids"]), (0.25, True, ["jdgb-9"]))
        self.assertEqual((receipt["host_counts"]["host_done"], receipt["host_counts"]["reconciled"]), (2, True))
        self.assertEqual(receipt["source"], "items+status")
        lagging = _Batch(items, done=1)
        self.assertIs(self._adopt(items, {"a": "x", "b": "y"}, Q, batch=lagging).receipt["complete"], False)
        with self.assertRaises(ValueError):
            self._adopt(batch, {"a": "x", "b": "y"}, Q, batch=batch)

    def test_two_served_models_are_not_comparable(self):
        rows = self._adopt({"a": _host_item("a", _answers(), model=ROUTE_A),
                            "b": _host_item("b", _answers(), model=ROUTE_B)}, {"a": "x", "b": "y"}, Q)
        self.assertEqual(rows.receipt["served_models"], {ROUTE_A: 1, ROUTE_B: 1})
        self.assertIs(rows.receipt["comparable"], False)

    def test_answers_only_results_have_unknown_served_model(self):
        rows = self._adopt({"a": _answers(), "b": _answers()}, {"a": "x", "b": "y"}, Q)
        self.assertEqual([row["model"] for row in rows], [None, None])
        self.assertEqual(rows.receipt["served_models"], {})
        self.assertEqual(rows.receipt["served_unknown"], 2)
        self.assertIs(rows.receipt["comparable"], False)
        self.assertIs(rows.receipt["cost_known"], False)  # no batch status was supplied

    def test_host_batch_is_read_from_snapshots_without_draining(self):
        batch = _Batch({"a": _host_item("a", _answers()), "b": _host_item("b", _answers())})
        rows = self._adopt(batch, {"a": "x", "b": "y"}, Q)
        receipt = rows.receipt
        self.assertEqual((receipt["cost"], receipt["cost_kind"], receipt["cost_known"]), (0.125, "batch_estimate", True))
        self.assertEqual((receipt["batch_ids"], receipt["intent"], receipt["billing"]),
                         (["jdgb-1"], "host sweep", "estimate_unverified"))
        self.assertEqual({row["batch_id"] for row in rows}, {"jdgb-1"})
        self.assertEqual(receipt["host_counts"], {"answered": 2, "planned": 2, "host_done": 2, "host_total": 2,
                                                  "reconciled": True})
        # results() carries answers only, so the batch-level model must never be attributed to a row.
        self.assertEqual([row["model"] for row in rows], [None, None])
        self.assertEqual(receipt["status_model"], ROUTE_B)
        self.assertIs(receipt["comparable"], False)
        self.assertIs(receipt["complete"], True)

    def test_state_without_a_result_is_an_error_row_and_the_run_is_incomplete(self):
        rows = self._adopt({"a": _host_item("a", _answers())}, {"a": "x", "b": "y"}, Q)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["b"]["error"], "no result delivered")
        self.assertNotIn("severity", by_id["b"])
        self.assertIs(rows.receipt["complete"], False)
        self.assertEqual((rows.receipt["answered_ids"], rows.receipt["error_ids"], rows.receipt["unresolved_ids"]),
                         (["a"], ["b"], ["b"]))
        with contextlib.redirect_stdout(io.StringIO()):
            flags = jv.flag(rows)
        self.assertEqual({row["id"]: row["why"] for row in flags},
                         {"a": ["coverage_incomplete", "provenance_unknown"], "b": ["error"]})

    def test_result_for_an_unsubmitted_key_is_refused(self):
        for source in ({"a": _host_item("a", _answers()), "ghost": _host_item("ghost", _answers())},
                       _Batch({"a": _host_item("a", _answers()), "ghost": _host_item("ghost", _answers())})):
            with self.subTest(type(source).__name__):
                with self.assertRaisesRegex(ValueError, "'ghost'"):
                    self._adopt(source, {"a": "x"}, Q)
        self.assertEqual(jv.stats["runs"], [])

    def test_host_item_delivered_under_another_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "keyed 'b'"):
            self._adopt({"a": _host_item("b", _answers())}, {"a": "x"}, Q)

    def test_non_string_state_ids_are_refused_like_run(self):
        with self.assertRaisesRegex(ValueError, "state IDs must be strings"):
            self._adopt({1: _answers()}, {1: "x"}, Q)

    def test_status_count_disagreement_marks_unreconciled_and_incomplete(self):
        items = {"a": _host_item("a", _answers()), "b": _host_item("b", _answers())}
        rows = self._adopt(_Batch(items, done=3, total=3), {"a": "x", "b": "y"}, Q)
        counts = rows.receipt["host_counts"]
        self.assertEqual((counts["answered"], counts["host_done"], counts["host_total"], counts["reconciled"]),
                         (2, 3, 3, False))
        self.assertIs(rows.receipt["complete"], False)
        self.assertTrue(rows.receipt["coverage_errors"])
        self.assertTrue(all(row["coverage_complete"] is False for row in rows))
        self.assertEqual([row["id"] for row in rows if "error" in row], [])

    def test_a_failed_item_does_not_count_as_answered_in_reconciliation(self):
        items = {"a": _host_item("a", _answers()), "b": _host_item("b", None, error="provider 500")}
        rows = self._adopt(_Batch(items), {"a": "x", "b": "y"}, Q)
        self.assertEqual({row["id"]: row.get("error") for row in rows}, {"a": None, "b": "provider 500"})
        self.assertIs(rows.receipt["host_counts"]["reconciled"], False)
        self.assertIs(rows.receipt["complete"], False)

    def test_a_still_running_batch_is_a_partial_snapshot(self):
        rows = self._adopt(_Batch({"a": _host_item("a", _answers())}, total=2, done=1, running=True),
                           {"a": "x", "b": "y"}, Q)
        self.assertIs(rows.receipt["complete"], False)
        self.assertIs(rows.receipt["cost_known"], False)
        self.assertIs(rows.receipt["cost_uncertain"], True)

    def test_answer_missing_a_question_is_an_error_row_not_a_partial_verdict(self):
        rows = self._adopt({"a": _host_item("a", {"severity": _choice("low")})}, {"a": "x"}, Q)
        self.assertIn("invalid judge answer", rows[0]["error"])
        self.assertNotIn("severity", rows[0])
        self.assertIs(rows.receipt["complete"], False)

    def test_adopted_rows_flow_through_flag_and_policy_rule_like_run_rows(self):
        policy = jv.policy(Q, {"severity": {"top": 0.8}})
        rule = jv.policy_rule(policy, Q)
        items = {"sure": _full_item("sure", _answers("low", 0.95)),
                 "unsure": _full_item("unsure", _answers("high", 0.55))}
        rows = self._adopt(items, {"sure": "x", "unsure": "y"}, Q)
        self.assertIs(rows.receipt["provenance_complete"], True)
        with contextlib.redirect_stdout(io.StringIO()):
            flags = jv.flag(rows, rule)
        self.assertEqual([(row["id"], row["why"]) for row in flags],
                         [("unsure", ["severity:low_top_probability"])])
        # The same rule must refuse rows judged under another rubric, exactly as for run() output.
        other = {"severity": jv.choice("Different wording", low="minor", high="blocking")}
        with self.assertRaisesRegex(ValueError, "different question hash"):
            jv.policy_rule(policy, other)

    def test_host_items_without_route_fields_are_flagged_provenance_unknown(self):
        rows = self._adopt({"a": _host_item("a", _answers())}, {"a": "x"}, Q)
        with contextlib.redirect_stdout(io.StringIO()):
            flags = jv.flag(rows)
        self.assertEqual([(row["id"], row["why"]) for row in flags], [("a", ["provenance_unknown"])])

    def test_grouped_adoption_expands_slots_and_loses_no_slot_on_a_missing_group(self):
        units = {"x:1": {"file": "x", "text": "alpha"}, "x:2": {"file": "x", "text": "beta"},
                 "y:1": {"file": "y", "text": "gamma"}}
        questions = {"verdict": jv.choice("Assess", KEEP="useful", PURGE="weak")}
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, manifest = jv.group(units, by=lambda unit: unit["file"], k=2,
                                         render=lambda unit: unit["text"])
        verdict = {"type": "choice", "choice": "KEEP", "confidence": 0.9,
                   "probabilities": {"KEEP": 0.9, "PURGE": 0.1}}
        first = next(iter(manifest["slots"]))
        answered = {first: _host_item(first, {f"verdict@s{i}": verdict
                                              for i in range(len(manifest["slots"][first]))})}
        rows = self._adopt(answered, grouped, questions, grouping=manifest)
        self.assertEqual([row["id"] for row in rows], [uid for ids in manifest["slots"].values() for uid in ids])
        self.assertEqual(rows.receipt["strategy"], "host-judge-batch-grouped")
        by_group = {}
        for row in rows:
            by_group.setdefault(row["group"], []).append(row)
        self.assertTrue(all("error" not in row and row["verdict"] == "KEEP" for row in by_group[first]))
        missing = [gid for gid in manifest["slots"] if gid != first]
        self.assertTrue(missing)
        for gid in missing:
            self.assertTrue(all(row["error"] == "no result delivered" for row in by_group[gid]))
        self.assertIs(rows.receipt["complete"], False)
        self.assertEqual(sorted(rows.receipt["unresolved_ids"]),
                         sorted(uid for gid in missing for uid in manifest["slots"][gid]))


class ScalarTests(unittest.TestCase):
    def test_answers_reduce_to_label_probability_and_score(self):
        self.assertEqual(jv.scalar(_choice("high")), "high")
        self.assertEqual(jv.scalar({"type": "bool", "bool": 0.25}), 0.25)
        self.assertEqual(jv.scalar({"type": "score", "score": 1.5, "confidence": 0.5}), 1.5)

    def test_host_item_and_answers_mapping_need_a_question_id(self):
        item = _host_item("a", _answers("high", 0.7, 0.8))
        self.assertEqual(jv.scalar(item, "severity"), "high")
        self.assertEqual(jv.scalar(item, "safe"), 0.8)
        self.assertEqual(jv.scalar(item.answers, "safe"), 0.8)
        for value in (item, item.answers):
            with self.assertRaisesRegex(ValueError, "requires qid"):
                jv.scalar(value)

    def test_rows_return_their_answer_column(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rows = jv.adopt({"a": _answers("high", 0.7, 0.8)}, {"a": "x"}, Q)
        self.assertEqual((jv.scalar(rows[0], "severity"), jv.scalar(rows[0], "safe")), ("high", 0.8))
        with self.assertRaisesRegex(ValueError, "no column 'missing'"):
            jv.scalar(rows[0], "missing")
        with self.assertRaisesRegex(ValueError, "requires qid"):
            jv.scalar(rows[0])
        error_row = {"id": "b", "qhash": "h", "error": "no result delivered"}
        with self.assertRaisesRegex(ValueError, "no result delivered"):
            jv.scalar(error_row, "severity")

    def test_unreducible_shapes_raise_value_error_not_type_error(self):
        failed = _host_item("a", None, error="provider 500")
        cases = [
            ({"choice": "low"}, None, "requires qid"),
            ({"type": "mystery"}, None, "unknown type"),
            ({"type": "bool"}, None, "lacks 'bool'"),
            ({"type": "bool", "bool": 1.5}, None, "invalid probability"),
            ({"type": "bool", "bool": None}, None, "malformed|invalid probability"),
            ({"type": "score", "score": "NaN"}, None, "not finite"),
            ({"type": "score", "score": None}, None, "malformed"),
            ({"type": "choice", "choice": ["low"]}, None, "not a label"),
            (failed, "severity", "provider 500"),
            (_host_item("a", _answers()), "absent", "no 'absent' answer"),
            ({"severity": "low"}, "severity", "not a host answer mapping"),
            ({"other": 1}, "severity", "no 'severity' answer"),
            ("low", None, "not str"),
            (3.5, None, "not float"),
        ]
        for value, qid, pattern in cases:
            with self.subTest(value=value, qid=qid):
                with self.assertRaisesRegex(ValueError, pattern):
                    jv.scalar(value, qid)


if __name__ == "__main__":
    unittest.main()
