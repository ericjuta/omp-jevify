import contextlib
import copy
import io
from pathlib import Path
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


def exact_sizer(text):
    """One token per UTF-8 byte: a measured size equals the byte bound, so boundaries are exact."""
    return len(text.encode("utf-8"))


def unmeasurable(text):
    return None


def forbidden(*args, **kwargs):
    raise AssertionError("planning must not call the judge or completion")


QUESTIONS = {
    "severity": jv.choice("How severe is the defect?", low="minor", high="blocking"),
    "relevance": jv.scale("How relevant is the unit?", "none", "some", "direct"),
}
GENEROUS = {"total_budget": 10**6, "single_budget": 10**6, "headroom": 0, "framing": 0,
            "sizer": exact_sizer}

SOURCE_TEXT = "def a():\n    return 1\n\ndef b():\n    return 2\n"


def _quiet(testcase):
    patcher = mock.patch("builtins.print")
    patcher.start()
    testcase.addCleanup(patcher.stop)


def _source_tree(testcase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    (root / "a.py").write_text(SOURCE_TEXT, encoding="utf-8")
    (root / "b.py").write_text("x = 1\n", encoding="utf-8")
    return root


def _units():
    return {
        "a.py:1": {"file": "a.py", "line": 1, "end_line": 2, "text": "def a():"},
        "a.py:4": {"file": "a.py", "line": 4, "end_line": 5, "text": ""},
        "b.py:1": {"file": "b.py", "line": 1, "end_line": 1, "text": "z" * 500},
    }


def _render(unit):
    return unit["text"]


class PlanTests(unittest.TestCase):
    def setUp(self):
        _quiet(self)

    def test_request_fits_at_its_byte_bound_and_one_byte_less_is_over(self):
        states = {"s": {"text": "x" * 500}}
        generous = jv.plan(states, QUESTIONS, **GENEROUS)
        bound = generous["requests"]["s"]["total"]["upper"]
        at_bound = jv.plan(states, QUESTIONS, **dict(GENEROUS, total_budget=bound, headroom=0))
        below = jv.plan(states, QUESTIONS, **dict(GENEROUS, total_budget=bound - 1))
        self.assertTrue(at_bound["admissible"])
        self.assertEqual(below["over_budget"], ["s"])
        self.assertFalse(below["admissible"])

    def test_single_question_budget_is_checked_per_question(self):
        states = {"s": {"text": "x" * 500}}
        generous = jv.plan(states, QUESTIONS, **GENEROUS)
        worst = generous["requests"]["s"]["single"]["upper"]
        at_bound = jv.plan(states, QUESTIONS, **dict(GENEROUS, single_budget=worst))
        below = jv.plan(states, QUESTIONS, **dict(GENEROUS, single_budget=worst - 1))
        self.assertTrue(at_bound["admissible"])
        self.assertEqual(below["requests"]["s"]["single"]["status"], "over")
        self.assertEqual(below["over_budget"], ["s"])

    def test_unmeasurable_request_is_reported_unmeasured_not_guessed(self):
        states = {"s": {"text": "x" * 500}}
        report = jv.plan(states, QUESTIONS, **dict(GENEROUS, total_budget=100, sizer=unmeasurable))
        self.assertEqual(report["requests"]["s"]["status"], "unmeasured")
        self.assertEqual(report["unmeasured"], ["s"])
        self.assertEqual(report["over_budget"], [])
        self.assertFalse(report["admissible"])

    def test_planning_makes_no_judge_or_completion_calls(self):
        states = {"s": {"text": "x" * 500}}
        with mock.patch.dict(namespace, {"judge_batch": forbidden, "completion": forbidden,
                                         "wait": forbidden}):
            report = jv.plan(states, QUESTIONS, **GENEROUS)
        self.assertEqual(report["inference_calls"], 0)
        self.assertTrue(report["admissible"])

    def test_grouped_requests_count_every_slot_expansion(self):
        state = {"group": "g", "units": {"s0": "a", "s1": "b", "s2": "c"}}
        flat = jv.plan({"g": state}, QUESTIONS, **GENEROUS)
        grouping = {"original_ids": ["u1", "u2", "u3"], "slots": {"g": ["u1", "u2", "u3"]},
                    "excluded": {}}
        grouped = jv.plan({"g": state}, QUESTIONS, grouping=grouping, **GENEROUS)
        flat_request, grouped_request = flat["requests"]["g"], grouped["requests"]["g"]
        base = flat_request["total"]["upper"] - flat_request["state_bytes"]
        expanded = grouped_request["total"]["upper"] - grouped_request["state_bytes"]
        self.assertEqual(grouped["questions"]["expanded"], 3 * len(QUESTIONS))
        self.assertGreaterEqual(expanded, 3 * base)
        # The flat request fits this budget; the expanded grouped request does not.
        budget = flat_request["total"]["upper"]
        self.assertTrue(jv.plan({"g": state}, QUESTIONS, **dict(GENEROUS, total_budget=budget))["admissible"])
        over = jv.plan({"g": state}, QUESTIONS, grouping=grouping,
                       **dict(GENEROUS, total_budget=budget))
        self.assertEqual(over["over_budget"], ["g"])

    def test_invalid_grouping_is_refused(self):
        grouping = {"original_ids": ["u1", "u2"], "slots": {"g": ["u1"]}, "excluded": {}}
        with self.assertRaises(ValueError):
            jv.plan({"g": {"group": "g", "units": {"s0": "a"}}}, QUESTIONS, grouping=grouping, **GENEROUS)


class PackTests(unittest.TestCase):
    def setUp(self):
        _quiet(self)

    def test_pack_keeps_every_original_and_never_truncates(self):
        units = {"u1": "a" * 30_000, "u2": "b" * 10, "u3": "c" * 10}
        states, grouping = jv.pack(units, None, QUESTIONS, k=3, **GENEROUS)
        placed = [uid for ids in grouping["slots"].values() for uid in ids]
        self.assertEqual(sorted(placed + list(grouping["excluded"])), sorted(units))
        self.assertEqual(grouping["original_ids"], list(units))
        packed = {uid: states[gid]["units"][f"s{index}"]
                  for gid, ids in grouping["slots"].items() for index, uid in enumerate(ids)}
        self.assertEqual(packed["u1"], units["u1"])

    def test_unit_over_budget_alone_is_excluded_with_reason(self):
        units = {"small": "a" * 10, "huge": "h" * 6_000}
        budgets = dict(GENEROUS, total_budget=4_000, single_budget=4_000)
        states, grouping = jv.pack(units, None, QUESTIONS, k=2, **budgets)
        self.assertIn("budget", grouping["excluded"]["huge"])
        self.assertEqual(sum(grouping["slots"].values(), []), ["small"])
        report = jv.plan(states, QUESTIONS, grouping=grouping, **budgets)
        self.assertTrue(report["admissible"])

    def test_tighter_budget_splits_groups_without_dropping_units(self):
        units = {f"u{i}": "x" * 400 + str(i) for i in range(4)}
        states, grouping = jv.pack(units, None, QUESTIONS, k=3, **GENEROUS)
        self.assertEqual(sorted(map(len, grouping["slots"].values())), [1, 3])
        full = max(grouping["slots"], key=lambda gid: len(grouping["slots"][gid]))
        need = jv.plan(states, QUESTIONS, grouping=grouping, **GENEROUS)["requests"][full]["total"]["upper"]

        _, at_budget = jv.pack(units, None, QUESTIONS, k=3, **dict(GENEROUS, total_budget=need))
        self.assertEqual(sorted(map(len, at_budget["slots"].values())), [1, 3])

        tighter_states, tighter = jv.pack(units, None, QUESTIONS, k=3,
                                          **dict(GENEROUS, total_budget=need - 1))
        self.assertLessEqual(max(map(len, tighter["slots"].values())), 2)
        self.assertEqual(sorted(sum(tighter["slots"].values(), [])), sorted(units))
        self.assertTrue(jv.plan(tighter_states, QUESTIONS, grouping=tighter,
                                **dict(GENEROUS, total_budget=need - 1))["admissible"])

    def test_unmeasured_unit_is_excluded_with_explicit_reason(self):
        units = {"u1": "a" * 50_000}
        _, grouping = jv.pack(units, None, QUESTIONS, k=2, **dict(GENEROUS, total_budget=1_000,
                                                                  single_budget=1_000, sizer=unmeasurable))
        self.assertIn("unmeasured", grouping["excluded"]["u1"])


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        _quiet(self)
        self.root = _source_tree(self)

    def _manifest(self, units, *, cap=100, hits=None):
        report = {}
        built = jv.states(units, _render, cap=cap, report=report)
        return jv.extraction(units, built, outcomes=report, root=self.root, hits=hits), built, report

    def test_manifest_records_per_original_drop_and_truncation_reasons(self):
        manifest, _, _ = self._manifest(_units())
        entries = manifest["units"]
        self.assertEqual(entries["a.py:4"]["status"], "dropped")
        self.assertEqual(entries["a.py:4"]["reason"], "empty rendered unit")
        self.assertEqual(entries["b.py:1"]["status"], "submitted")
        self.assertEqual(entries["b.py:1"]["truncated"], 1)
        self.assertIn("capped", entries["b.py:1"]["reason"])
        self.assertIsNotNone(manifest["source"]["files"]["a.py"]["sha256"])
        self.assertEqual(manifest["coverage"]["dropped"], 1)
        self.assertEqual(manifest["coverage"]["truncated"], 1)
        self.assertFalse(manifest["coverage"]["complete"])

    def test_complete_manifest_validates_against_its_states(self):
        units = {"a.py:1": _units()["a.py:1"], "b.py:1": dict(_units()["b.py:1"], text="short")}
        manifest, built, _ = self._manifest(units, cap=12_000)
        self.assertTrue(manifest["coverage"]["complete"])
        result = jv.validate_extraction(manifest, built, root=self.root)
        self.assertTrue(result["ok"], result["problems"])

    def test_changed_source_fails_validation_and_restoring_it_passes_again(self):
        units = {"a.py:1": _units()["a.py:1"]}
        manifest, built, _ = self._manifest(units, cap=12_000)
        (self.root / "a.py").write_text("changed\n", encoding="utf-8")
        result = jv.validate_extraction(manifest, built, root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["changed"], ["a.py"])
        (self.root / "a.py").write_text(SOURCE_TEXT, encoding="utf-8")
        self.assertTrue(jv.validate_extraction(manifest, built, root=self.root)["ok"])

    def test_changed_state_fails_validation(self):
        units = {"a.py:1": _units()["a.py:1"]}
        manifest, built, _ = self._manifest(units, cap=12_000)
        result = jv.validate_extraction(manifest, {**built, "a.py:1": "other"}, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("state changed" in problem for problem in result["problems"]))

    def test_tampered_manifest_fails_its_digest(self):
        manifest, built, _ = self._manifest(_units())
        tampered = copy.deepcopy(manifest)
        tampered["coverage"]["complete"] = True
        result = jv.validate_extraction(tampered, built, root=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("digest" in problem for problem in result["problems"]))

    def test_source_hits_outside_every_unit_are_reported_uncovered(self):
        units = {"b.py:1": _units()["b.py:1"]}
        manifest, _, _ = self._manifest(units, cap=12_000,
                                        hits=[{"file": "b.py", "line": 1}, {"file": "b.py", "line": 9}])
        self.assertEqual(manifest["hits"]["uncovered"], [{"file": "b.py", "line": 9}])
        self.assertFalse(manifest["coverage"]["complete"])

    def test_grouped_manifest_binds_grouping_and_refuses_a_flat_check(self):
        units = {"a.py:1": _units()["a.py:1"], "b.py:1": dict(_units()["b.py:1"], text="short")}
        states, grouping = jv.pack(units, _render, QUESTIONS, k=2, **GENEROUS)
        manifest = jv.extraction(units, states, grouping=grouping, root=self.root)
        self.assertTrue(jv.validate_extraction(manifest, states, grouping=grouping, root=self.root)["ok"])
        flat_check = jv.validate_extraction(manifest, states, root=self.root)
        self.assertFalse(flat_check["ok"])

    def test_group_report_maps_each_original_to_its_slot_and_truncation(self):
        report = {}
        jv.group(_units(), by=lambda unit: unit["file"], k=2, render=_render, cap=100, report=report)
        self.assertEqual(report["a.py:1"]["status"], "submitted")
        self.assertEqual(report["a.py:1"]["state"], "a.py#1")
        self.assertEqual(report["a.py:1"]["slot"], 0)
        self.assertEqual(report["a.py:4"]["status"], "excluded")
        self.assertEqual(report["a.py:4"]["reason"], "empty rendered unit")
        self.assertEqual(report["b.py:1"]["truncated"], 1)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        _quiet(self)
        self.root = _source_tree(self)
        self.units = {"a.py:1": _units()["a.py:1"], "b.py:1": dict(_units()["b.py:1"], text="short")}
        self.states = {"a.py:1": "def a():", "b.py:1": "short"}
        self.manifest = jv.extraction(self.units, self.states, root=self.root)
        self.policy = {"escalate": "confirmed-only"}
        self.review = jv.review_queue(
            self.manifest,
            {"f1": {"unit": "a.py:1", "claim": "a is a stub"},
             "f2": {"unit": "b.py:1", "claim": "b is dead code"}},
            policy=self.policy, root=self.root)

    def test_confirmation_requires_evidence_and_records_the_cited_quote(self):
        with self.assertRaises(ValueError):
            jv.adjudicate(self.review, "f1", "confirmed")
        jv.adjudicate(self.review, "f1", "confirmed",
                      evidence=[{"file": "a.py", "start": 1, "end": 2}], note="reads ok")
        entry = self.review["items"]["f1"]
        self.assertEqual(entry["verdict"], "confirmed")
        self.assertEqual(entry["evidence"][0]["quote"], "def a():\n    return 1")
        self.assertEqual(jv.review_status(self.review)["pending"], ["f2"])

    def test_evidence_from_a_changed_source_is_refused_and_nothing_is_recorded(self):
        (self.root / "a.py").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            jv.adjudicate(self.review, "f1", "confirmed",
                          evidence=[{"file": "a.py", "start": 1, "end": 1}])
        self.assertEqual(self.review["items"]["f1"]["verdict"], "pending")

    def test_unresolved_needs_no_evidence_and_unknown_verdicts_are_refused(self):
        jv.adjudicate(self.review, "f2", "unresolved")
        self.assertEqual(jv.review_status(self.review)["unresolved"], 1)
        with self.assertRaises(ValueError):
            jv.adjudicate(self.review, "f1", "maybe")

    def test_evidence_outside_the_snapshot_is_refused(self):
        with self.assertRaises(ValueError):
            jv.adjudicate(self.review, "f1", "overturned",
                          evidence=[{"file": "missing.py", "start": 1, "end": 1}])
        with self.assertRaises(ValueError):
            jv.adjudicate(self.review, "f1", "overturned",
                          evidence=[{"file": "a.py", "start": 1, "end": 50}])

    def test_policy_identity_follows_the_frozen_policy(self):
        same = jv.review_queue(self.manifest, {"f1": {"unit": "a.py:1", "claim": "x"}},
                               policy={"escalate": "confirmed-only"}, root=self.root)
        other = jv.review_queue(self.manifest, {"f1": {"unit": "a.py:1", "claim": "x"}},
                                policy={"escalate": "any"}, root=self.root)
        self.assertEqual(self.review["policy_digest"], same["policy_digest"])
        self.assertNotEqual(self.review["policy_digest"], other["policy_digest"])

    def test_queue_refuses_units_absent_from_the_manifest(self):
        with self.assertRaises(ValueError):
            jv.review_queue(self.manifest, {"f1": {"unit": "nope.py:1", "claim": "x"}},
                            policy=self.policy, root=self.root)


class DeletionGuardTests(unittest.TestCase):
    def setUp(self):
        _quiet(self)
        self.root = _source_tree(self)
        self.units = {"a.py:1": {"file": "a.py", "line": 1, "end_line": 2},
                      "a.py:4": {"file": "a.py", "line": 4, "end_line": 5}}
        self.states = {"a.py:1": "def a():", "a.py:4": "def b():"}
        self.manifest = jv.extraction(self.units, self.states, root=self.root)
        self.review = jv.review_queue(
            self.manifest,
            {"f1": {"unit": "a.py:1", "claim": "a is a stub"},
             "f2": {"unit": "a.py:4", "claim": "b is a stub"}},
            policy={"escalate": "confirmed-only"}, root=self.root)

    def _delete(self, ids, **kwargs):
        return jv.delete_spans(self.units, ids, root=self.root, dry_run=False,
                               manifest=self.manifest, **kwargs)

    def test_changed_source_refuses_deletion_before_any_write(self):
        (self.root / "a.py").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self._delete(["a.py:1"])
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "changed\n")

    def test_pending_review_refuses_deletion(self):
        jv.adjudicate(self.review, "f1", "confirmed", evidence=[{"file": "a.py", "start": 1, "end": 2}])
        with self.assertRaises(ValueError):
            self._delete(["a.py:1"], review=self.review)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), SOURCE_TEXT)

    def test_target_without_a_confirmed_decision_refuses_deletion(self):
        jv.adjudicate(self.review, "f1", "unresolved")
        jv.adjudicate(self.review, "f2", "confirmed", evidence=[{"file": "a.py", "start": 4, "end": 5}])
        with self.assertRaises(ValueError):
            self._delete(["a.py:1"], review=self.review)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), SOURCE_TEXT)

    def test_review_without_manifest_is_refused(self):
        with self.assertRaises(ValueError):
            jv.delete_spans(self.units, ["a.py:1"], root=self.root, dry_run=False, review=self.review)

    def test_unit_altered_after_extraction_is_refused(self):
        self.units["a.py:1"]["end_line"] = 3
        jv.adjudicate(self.review, "f1", "confirmed", evidence=[{"file": "a.py", "start": 1, "end": 2}])
        jv.adjudicate(self.review, "f2", "confirmed", evidence=[{"file": "a.py", "start": 4, "end": 5}])
        with self.assertRaises(ValueError):
            self._delete(["a.py:1"], review=self.review)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), SOURCE_TEXT)

    def test_fully_confirmed_review_deletes_the_reviewed_span(self):
        jv.adjudicate(self.review, "f1", "confirmed", evidence=[{"file": "a.py", "start": 1, "end": 2}])
        jv.adjudicate(self.review, "f2", "confirmed", evidence=[{"file": "a.py", "start": 4, "end": 5}])
        self._delete(["a.py:1"], review=self.review)
        remaining = (self.root / "a.py").read_text(encoding="utf-8")
        self.assertNotIn("def a():", remaining)
        self.assertIn("def b():", remaining)


if __name__ == "__main__":
    unittest.main()
