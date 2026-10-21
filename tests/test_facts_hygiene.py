import contextlib
import io
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
# The %load-oriented script announces itself on execution; keep test output quiet.
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = function(*args, **kwargs)
    return result, output.getvalue()


def exact_sizer(text):
    return len(text.encode("utf-8"))


class FactsTests(unittest.TestCase):
    def test_facts_attach_to_wrapped_and_dict_units_without_clobbering_fields(self):
        units = {"a": "plain text", "b": {"name": "beta", "size": 3}}
        (states, settled, report), _ = quiet(jv.facts, units, {"n": lambda unit: len(str(unit))})
        self.assertEqual(states["a"], {"unit": "plain text", "facts": {"n": 10}})
        self.assertEqual(states["b"]["name"], "beta")
        self.assertEqual(states["b"]["facts"], {"n": len(str(units["b"]))})
        self.assertEqual(settled, [])
        self.assertEqual(report["counts"]["judged"], 2)
        self.assertEqual(units["b"], {"name": "beta", "size": 3})  # caller's unit untouched

    def test_an_existing_facts_key_is_refused_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, "facts"):
            quiet(jv.facts, {"a": {"facts": "mine"}}, {"n": lambda unit: 1})

    def test_rule_decided_units_leave_the_states_as_rows(self):
        units = {"a": "", "b": "text", "c": "more"}
        (states, settled, report), _ = quiet(
            jv.facts, units, {"empty": lambda unit: unit == ""},
            settle=lambda facts: {"safe": False} if facts["empty"] else None)
        self.assertEqual(set(states), {"b", "c"})
        self.assertEqual(settled, [{"id": "a", "settled_by": "rule", "safe": False, "qhash": None}])
        self.assertEqual(report["counts"]["settled"], 1)
        self.assertEqual(report["counts"]["judged"], 2)

    def test_a_raising_check_is_recorded_and_the_unit_is_neither_dropped_nor_settled(self):
        def boom(unit):
            if unit == 2:
                raise RuntimeError("cannot read")
            return unit

        (states, settled, report), _ = quiet(
            jv.facts, {"a": 1, "b": 2}, {"v": boom}, settle=lambda facts: {"safe": True})
        self.assertEqual([row["id"] for row in settled], ["a"])
        self.assertIn("b", states)  # never silently dropped, and the rule never decided it
        self.assertEqual(states["b"]["facts"], {})
        self.assertIn("RuntimeError: cannot read", report["check_errors"]["b"]["v"])

    def test_a_non_json_check_value_is_a_check_error(self):
        (states, _, report), _ = quiet(jv.facts, {"a": 1}, {"bad": lambda unit: {1, 2}})
        self.assertIn("a", states)
        self.assertIn("bad", report["check_errors"]["a"])

    def test_a_raising_or_malformed_settle_rule_leaves_the_unit_to_be_judged(self):
        def rule(facts):
            raise KeyError("missing")

        (states, settled, report), _ = quiet(jv.facts, {"a": 1}, {"n": lambda unit: unit}, settle=rule)
        self.assertEqual((list(states), settled), (["a"], []))
        self.assertIn("a", report["settle_errors"])
        (states, settled, report), _ = quiet(
            jv.facts, {"a": 1}, {"n": lambda unit: unit}, settle=lambda facts: {"id": "x"})
        self.assertEqual((list(states), settled), (["a"], []))
        self.assertIn("a", report["settle_errors"])

    def test_required_but_missing_names_are_reported_and_warned(self):
        units = {"p1": {"upstream": "diff one", "fork_diff": ""},
                 "p2": {"upstream": "diff two", "fork_diff": "fork diff"},
                 "p3": {"upstream": "diff three"}}
        (states, _, report), out = quiet(
            jv.facts, units, {}, requires={"fork_applies": ["fork_diff"]})
        self.assertEqual(report["missing"], {"fork_applies": {"p1": ["fork_diff"], "p3": ["fork_diff"]}})
        self.assertEqual(report["counts"]["missing"], {"fork_applies": 2})
        self.assertIn("warning", out)
        self.assertEqual(set(states), {"p1", "p2", "p3"})  # reported, never dropped

    def test_a_fact_satisfies_a_requirement_and_settled_units_are_not_asked(self):
        units = {"a": "x", "b": "y"}
        (_, settled, report), _ = quiet(
            jv.facts, units, {"has": lambda unit: unit if unit == "x" else None},
            settle=lambda facts: {"q": 1} if facts["has"] is None else None,
            requires={"q": ["has"]})
        self.assertEqual([row["id"] for row in settled], ["b"])
        self.assertEqual(report["missing"], {})


class RefuseElidedTests(unittest.TestCase):
    MARKERS = {
        "line elision": "a\n[… 12 ln elided]\nb",
        "re-read hint": "x\nBody elided; re-read needed ranges.\n",
        "more count": "row\n… 14 more rows\n",
        "truncated notice": "[Some lines truncated to 500 chars]\n",
        "artifact pointer": "Output cut. Use artifact://42 for the full text",
    }

    def test_each_listed_marker_is_refused_and_named(self):
        for name, text in self.MARKERS.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "elided"):
                jv.refuse_elided(text, source="read")

    def test_the_error_names_the_marker_and_line(self):
        with self.assertRaisesRegex(ValueError, r"input is elided.*ln elided.*line 2"):
            jv.refuse_elided("a\n[… 3 ln elided]\nb")

    def test_normal_text_and_the_end_of_file_footer_pass_through(self):
        text = "plain\n| a | b |\nthree more items are listed\n[end of file: 120 bytes]\n"
        self.assertEqual(jv.refuse_elided(text), text)

    def test_non_text_is_refused(self):
        with self.assertRaises(ValueError):
            jv.refuse_elided(None)


TABLE = """Findings:

| id | claim | note |
|----|:-----:|-----:|
| a1 | uses `x \\| y` | ok |
| a2 | plain | |
"""


class UnitsFromTableTests(unittest.TestCase):
    def test_an_escaped_pipe_stays_one_cell_and_is_unescaped(self):
        units = jv.units_from_table(TABLE, key="id")
        self.assertEqual(units["a1"], {"id": "a1", "claim": "uses `x | y`", "note": "ok"})
        self.assertEqual(units["a2"]["note"], "")
        self.assertEqual(list(units), ["a1", "a2"])

    def test_without_a_key_ids_are_one_based_row_numbers(self):
        units = jv.units_from_table(TABLE)
        self.assertEqual(list(units), ["1", "2"])
        self.assertEqual(units["2"]["claim"], "plain")

    def test_an_unescaped_pipe_makes_a_ragged_row_that_is_refused_with_its_number(self):
        text = "| id | claim |\n|---|---|\n| a | fine |\n| b | x | y |\n| c | short |\n| d |\n"
        with self.assertRaisesRegex(ValueError, r"rows \[2, 4\]"):
            jv.units_from_table(text)

    def test_elided_input_is_refused_before_parsing(self):
        text = "| id | claim |\n|---|---|\n| a | fine |\n[… 9 ln elided]\n"
        with self.assertRaisesRegex(ValueError, "elided"):
            jv.units_from_table(text)

    def test_duplicate_and_empty_keys_are_refused(self):
        head = "| id | claim |\n|---|---|\n"
        with self.assertRaisesRegex(ValueError, "duplicate table key 'a'"):
            jv.units_from_table(head + "| a | one |\n| a | two |\n", key="id")
        with self.assertRaisesRegex(ValueError, r"empty 'id' value in table rows \[2\]"):
            jv.units_from_table(head + "| a | one |\n|  | two |\n", key="id")

    def test_no_data_rows_or_no_table_is_an_error_about_elision(self):
        for text in ("| id | claim |\n|---|---|\n", "no table here", ""):
            with self.subTest(text), self.assertRaisesRegex(ValueError, "no table rows; was the read elided"):
                jv.units_from_table(text)

    def test_an_unknown_key_column_and_a_second_table_are_refused(self):
        with self.assertRaisesRegex(ValueError, "key column"):
            jv.units_from_table(TABLE, key="missing")
        with self.assertRaisesRegex(ValueError, "more than one table"):
            jv.units_from_table(TABLE + "\ntext\n| id |\n|---|\n| z |\n")


class FlagTruncationTests(unittest.TestCase):
    @staticmethod
    def row(identifier, **extra):
        return {"id": identifier, "coverage_complete": True, "selected_model": "m", "primary_model": "m",
                "fallback": False, "cache": "miss", "questions": 1, "cached_questions": 0, **extra}

    def test_a_truncated_state_is_flagged_with_its_reason(self):
        rows = [self.row("a"), self.row("b"), self.row("c"), self.row("d")]
        report = {"a": {"truncated": 2, "oversize": False, "unmeasured": False},
                  "b": {"truncated": 0, "oversize": False, "unmeasured": False},
                  "c": {"truncated": 0, "oversize": True, "unmeasured": False},
                  "d": {"truncated": 0, "oversize": False, "unmeasured": True}}
        flagged, _ = quiet(jv.flag, rows, report=report)
        self.assertEqual({row["id"]: row["why"] for row in flagged},
                         {"a": ["truncated"], "c": ["oversize"], "d": ["unmeasured"]})

    def test_grouped_rows_inherit_their_group_state_report(self):
        rows = [self.row("u1", group="g#1", slot="s0"), self.row("u2", group="g#2", slot="s0")]
        flagged, _ = quiet(jv.flag, rows, report={"g#1": {"truncated": 1, "oversize": False,
                                                          "unmeasured": False}})
        self.assertEqual([row["id"] for row in flagged], ["u1"])
        self.assertIn("truncated", flagged[0]["why"])

    def test_without_a_report_behaviour_is_unchanged(self):
        flagged, _ = quiet(jv.flag, [self.row("a")])
        self.assertEqual(flagged, [])

    def test_truncation_reasons_add_to_other_reasons(self):
        flagged, _ = quiet(jv.flag, [self.row("a", error="judge failed")],
                           report={"a": {"truncated": 1}})
        self.assertEqual(flagged[0]["why"], ["error", "truncated"])


class RouteLimitTests(unittest.TestCase):
    @staticmethod
    def labels(count):
        return {f"l{i}": f"label {i}" for i in range(count)}

    def test_choice_allows_255_labels_and_refuses_256(self):
        self.assertEqual(len(jv.choice("which?", **self.labels(255))["criteria"]), 255)
        with self.assertRaisesRegex(ValueError, "at most 255 labels.*jev-1.13"):
            jv.choice("which?", **self.labels(256))
        with self.assertRaisesRegex(ValueError, "255"):
            jv.choice("which?", self.labels(200), **{f"x{i}": "d" for i in range(56)})

    def test_plan_reports_label_counts_and_refuses_a_hand_built_oversize_choice(self):
        Q = {"ok": jv.choice("which?", a="a", b="b"),
             "big": {"type": "choice", "instructions": "which?", "criteria": self.labels(256)}}
        report, _ = quiet(jv.plan, {"u": "text"}, Q, sizer=exact_sizer)
        self.assertEqual(report["labels"], {"ok": 2, "big": 256})
        self.assertEqual(report["over_labels"], ["big"])
        self.assertFalse(report["admissible"])
        Q["big"]["criteria"] = self.labels(255)
        report, _ = quiet(jv.plan, {"u": "text"}, Q, sizer=exact_sizer)
        self.assertEqual(report["over_labels"], [])
        self.assertTrue(report["admissible"])


class PackSuggestionTests(unittest.TestCase):
    Q = {"q": jv.yes("Is it a defect?")}

    def plan(self, states, **options):
        report, _ = quiet(jv.plan, states, self.Q, sizer=exact_sizer, **options)
        return report

    def test_many_tiny_flat_states_get_a_suggestion_that_demands_a_pilot(self):
        report = self.plan({f"u{i}": "tiny" for i in range(25)})
        suggestion = report["pack_suggestion"]
        self.assertIn("separate instrument", suggestion)
        self.assertIn("own pilot", suggestion)
        self.assertEqual(report["strategy"], "flat")

    def test_few_states_get_none(self):
        self.assertIsNone(self.plan({f"u{i}": "tiny" for i in range(19)})["pack_suggestion"])

    def test_states_that_fill_a_tenth_of_the_budget_get_none(self):
        states = {f"u{i}": "x" * 4000 for i in range(25)}
        self.assertIsNone(self.plan(states)["pack_suggestion"])

    def test_the_suggestion_follows_the_median_request_not_the_mean(self):
        states = {f"u{i}": "tiny" for i in range(21)}
        states["big"] = "x" * 30_000
        self.assertIsNotNone(self.plan(states)["pack_suggestion"])

    def test_existing_plan_keys_and_grouped_plans_are_unchanged(self):
        report = self.plan({"u": "tiny"})
        for key in ("schema", "strategy", "qhash", "questions", "budgets", "requests", "over_budget",
                    "unmeasured", "excluded", "admissible", "inference_calls"):
            self.assertIn(key, report)
        self.assertIsNone(report["pack_suggestion"])
        self.assertTrue(report["admissible"])


if __name__ == "__main__":
    unittest.main()
