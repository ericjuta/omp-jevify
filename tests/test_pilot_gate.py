import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]

Q = {"verdict": jv.choice("Which verdict fits?", real="a real defect", other="something else",
                          unrelated="not about this")}
QB = {"safe": jv.yes("Is it safe?")}
QH = jv.qhash(Q)
QBH = jv.qhash(QB)


def crow(identifier, label="real", top=0.95, *, state=None, qhash=QH, provenance=False, **extra):
    """One flat choice row as run() emits it, with host provenance fields only when asked for."""
    rest = (1 - top) / 2
    row = {"id": identifier, "qhash": qhash, "coverage_complete": True, "verdict": label,
           "verdict.conf": top}
    for name in Q["verdict"]["criteria"]:
        row[f"verdict.{name}"] = top if name == label else rest
    if state is not None:
        row["state_sha256"] = jv.state_digest(state)
    if provenance:
        row.update(selected_model="m", primary_model="m", fallback=False, cache="miss", questions=1,
                   cached_questions=0)
    row.update(extra)
    return row


def brow(identifier, p, *, state=None, qhash=QBH):
    row = {"id": identifier, "qhash": qhash, "coverage_complete": True, "safe": p}
    if state is not None:
        row["state_sha256"] = jv.state_digest(state)
    return row


class QuietCase(unittest.TestCase):
    def call(self, function, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args, **kwargs)


class PilotCheckTests(QuietCase):
    def test_a_pilot_flagged_by_the_frozen_rule_goes_to_direct_reading(self):
        rule = jv.policy_rule(jv.policy(Q, {"verdict": {"top": 0.9}}), Q)
        rows = [crow(f"u{i}", top=0.8) for i in range(24)] + [crow("u24", top=0.99)]
        result = self.call(jv.pilot_check, rows, Q, rule=rule)
        self.assertEqual(result["verdict"], "use_direct")
        self.assertEqual((result["units"], result["flagged"]), (25, 24))
        self.assertAlmostEqual(result["flag_rate"], 0.96)
        self.assertEqual(result["band"], 0)  # 0.8 is outside the band, so only the flag rate objects
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("flag_rate 0.96", result["reasons"][0])
        self.assertIn("script", result["reasons"][0])

    def test_a_healthy_pilot_proceeds_and_provenance_alone_never_counts(self):
        rows = [crow(f"u{i}") for i in range(20)]
        # Every row lacks host provenance, so flag() itself would flag all of them.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual({why for row in jv.flag(rows) for why in row["why"]}, {"provenance_unknown"})
        result = self.call(jv.pilot_check, rows, Q)
        self.assertEqual(result["verdict"], "proceed")
        self.assertEqual((result["flagged"], result["flag_rate"], result["provenance_only"]), (0, 0.0, 20))
        self.assertEqual(result["reasons"], [])

    def test_errors_and_incomplete_coverage_do_count_as_flags(self):
        rows = [crow(f"u{i}") for i in range(10)]
        rows += [{"id": f"e{i}", "qhash": QH, "error": "boom"} for i in range(3)]
        rows += [crow(f"c{i}", coverage_complete=False) for i in range(3)]
        result = self.call(jv.pilot_check, rows, Q, max_flag_rate=0.3)
        self.assertEqual((result["errors"], result["flagged"], result["answered"]), (3, 6, 13))
        self.assertEqual(result["verdict"], "use_direct")

    def test_the_uncertain_band_for_choices_excludes_a_top_probability_of_exactly_point_seven(self):
        rows = [crow(f"lo{i}", top=0.4) for i in range(5)] + [crow(f"hi{i}", top=0.7) for i in range(5)]
        result = self.call(jv.pilot_check, rows, Q, max_band_rate=0.4)
        self.assertEqual(result["band"], 5)  # 0.4 in band, 0.7 out
        self.assertEqual(result["verdict"], "use_direct")
        self.assertIn("band_rate 0.50", result["reasons"][0])

    def test_a_bool_probability_of_exactly_point_seven_is_in_the_band(self):
        rows = [brow(f"u{i}", 0.7) for i in range(10)]
        result = self.call(jv.pilot_check, rows, QB)
        self.assertEqual((result["band"], result["verdict"]), (10, "use_direct"))

    def test_a_catchall_label_absorbing_the_pilot_triggers_use_direct(self):
        rows = [crow(f"u{i}", "other") for i in range(8)] + [crow(f"v{i}") for i in range(2)]
        result = self.call(jv.pilot_check, rows, Q, catchall={"verdict": ["other", "unrelated"]})
        self.assertEqual(result["catchall"], {"verdict": 0.8})
        self.assertEqual(result["verdict"], "use_direct")
        self.assertIn("catchall verdict", result["reasons"][0])
        healthy = self.call(jv.pilot_check, [crow(f"u{i}") for i in range(10)], Q,
                            catchall={"verdict": ["other"]})
        self.assertEqual((healthy["verdict"], healthy["catchall"]), ("proceed", {"verdict": 0.0}))

    def test_a_tiny_pilot_is_too_small_even_when_every_row_is_flagged(self):
        rows = [crow(f"u{i}", top=0.5) for i in range(4)]
        result = self.call(jv.pilot_check, rows, Q)
        self.assertEqual(result["verdict"], "too_small")
        self.assertEqual(self.call(jv.pilot_check, [], Q)["verdict"], "too_small")

    def test_rows_from_another_rubric_and_bad_catchall_are_refused(self):
        with self.assertRaises(ValueError):
            self.call(jv.pilot_check, [crow("u", qhash="deadbeef")] * 10, Q)
        with self.assertRaises(ValueError):
            self.call(jv.pilot_check, [crow("u")] * 10, Q, catchall={"verdict": ["nope"]})
        with self.assertRaises(ValueError):
            self.call(jv.pilot_check, [crow("u")] * 10, Q, catchall={"missing": ["other"]})


class RemainingTests(QuietCase):
    def test_matching_rows_are_reused_and_everything_else_is_judged_again(self):
        states = {"same": "text a", "edited": "text b v2", "erred": "text c", "new": "text d",
                  "grouped": "text e"}
        prior = [
            crow("same", state="text a"),
            crow("edited", state="text b"),
            {**crow("erred", state="text c"), "error": "boom"},
            crow("grouped", state="text e", group="g1", slot="s0"),
            crow("gone", state="text z"),
        ]
        todo, reused = self.call(jv.remaining, states, prior, Q)
        self.assertEqual([row["id"] for row in reused], ["same"])
        self.assertEqual(set(todo), {"edited", "erred", "new", "grouped"})
        self.assertEqual(todo["edited"], "text b v2")

    def test_a_changed_rubric_or_missing_digest_forces_a_rejudge(self):
        states = {"a": "x", "b": "y"}
        other = {"verdict": jv.choice("Which verdict fits?", real="a real defect", other="something else")}
        prior = [crow("a", state="x", qhash=jv.qhash(other)), crow("b")]
        todo, reused = self.call(jv.remaining, states, prior, Q)
        self.assertEqual((set(todo), reused), ({"a", "b"}, []))

    def test_reused_rows_are_copies(self):
        states = {"a": "x"}
        prior = [crow("a", state="x")]
        _, reused = self.call(jv.remaining, states, prior, Q)
        reused[0]["verdict"] = "other"
        self.assertEqual(prior[0]["verdict"], "real")


class GateTests(QuietCase):
    def test_a_flip_on_an_identical_state_digest_is_noise(self):
        base = [crow("u", "real", state="same")]
        now = [crow("u", "other", state="same")]
        result = self.call(jv.gate, base, now, Q, qid="verdict", rejudge_rows=[crow("u", "other", state="same")])
        self.assertEqual((result["noise"], result["regressions"]), (["u"], []))

    def test_a_changed_digest_confirmed_by_rejudge_is_a_regression(self):
        base = [crow("u", "real", state="v1")]
        now = [crow("u", "other", state="v2")]
        again = [crow("u", "other", state="v2")]
        result = self.call(jv.gate, base, now, Q, qid="verdict", rejudge_rows=again)
        self.assertEqual((result["regressions"], result["noise"], result["unconfirmed"]), (["u"], [], []))

    def test_a_changed_digest_without_rejudge_is_unconfirmed(self):
        base = [crow("u", "real", state="v1")]
        now = [crow("u", "other", state="v2")]
        result = self.call(jv.gate, base, now, Q, qid="verdict")
        self.assertEqual((result["unconfirmed"], result["regressions"]), (["u"], []))
        # A rejudge of some other state does not confirm it either.
        stale = [crow("u", "other", state="v1")]
        self.assertEqual(self.call(jv.gate, base, now, Q, qid="verdict", rejudge_rows=stale)["unconfirmed"], ["u"])

    def test_a_rejudge_that_returns_to_the_baseline_verdict_is_noise(self):
        base = [crow("u", "real", state="v1")]
        now = [crow("u", "other", state="v2")]
        again = [crow("u", "real", state="v2")]
        result = self.call(jv.gate, base, now, Q, qid="verdict", rejudge_rows=again)
        self.assertEqual((result["noise"], result["regressions"]), (["u"], []))

    def test_unchanged_verdicts_split_by_whether_the_source_changed(self):
        base = [crow("a", state="1"), crow("b", state="1")]
        now = [crow("a", state="1"), crow("b", state="2")]
        result = self.call(jv.gate, base, now, Q, qid="verdict")
        self.assertEqual((result["unchanged"], result["source_changed"]), (["a"], ["b"]))

    def test_missing_digests_errors_and_absent_units_are_unknown(self):
        base = [crow("nodigest", "real"), crow("erred", "real", state="1"), crow("lost", state="1")]
        now = [crow("nodigest", "other"), {"id": "erred", "qhash": QH, "error": "x"}, crow("added", state="1")]
        result = self.call(jv.gate, base, now, Q, qid="verdict")
        self.assertEqual(sorted(result["unknown"]), ["added", "erred", "lost", "nodigest"])
        self.assertEqual(result["regressions"], [])

    def test_bool_movement_is_judged_against_the_margin(self):
        base = [brow("a", 0.9, state="1"), brow("b", 0.9, state="1")]
        now = [brow("a", 0.8, state="1"), brow("b", 0.6, state="1")]
        result = self.call(jv.gate, base, now, QB, qid="safe", margin=0.15)
        self.assertEqual((result["unchanged"], result["noise"]), (["a"], ["b"]))

    def test_a_different_rubric_is_refused_not_compared(self):
        other = {"verdict": jv.choice("Which verdict fits?", real="a real defect", other="something else")}
        base = [crow("u", state="1", qhash=jv.qhash(other))]
        with self.assertRaises(ValueError):
            self.call(jv.gate, base, [crow("u", state="1")], Q, qid="verdict")
        with self.assertRaises(ValueError):
            self.call(jv.gate, [crow("u", state="1")], [crow("u", state="1")], Q, qid="verdict",
                      rejudge_rows=base)
        with self.assertRaises(ValueError):
            self.call(jv.gate, [crow("u")], [crow("u")], Q, qid="nope")


class PowerTests(unittest.TestCase):
    def test_worst_case_sample_sizes_round_up(self):
        self.assertEqual(jv.power(rate=0.5, margin=0.1, confidence=0.95), 97)
        self.assertEqual(jv.power(), 97)
        self.assertEqual(jv.power(rate=0.5, margin=0.1, confidence=0.9), 68)
        self.assertEqual(jv.power(rate=0.5, margin=0.1, confidence=0.99), 166)
        self.assertEqual(jv.power(rate=0.2, margin=0.05, confidence=0.95), 246)

    def test_unsupported_inputs_are_refused(self):
        for kwargs in ({"confidence": 0.8}, {"rate": 0.0}, {"rate": 1.0}, {"margin": 0.0}, {"margin": 1.0}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                jv.power(**kwargs)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "ledger.jsonl"

    def test_tuned_units_cannot_become_heldout_and_nothing_is_recorded_on_refusal(self):
        ledger = jv.ledger(self.path)
        ledger.record(["a", "b"], purpose="tune")
        with self.assertRaises(ValueError) as caught:
            ledger.record(["c", "b"], purpose="heldout")
        self.assertIn("'b'", str(caught.exception))
        self.assertEqual(len(ledger.entries()), 1)
        ledger.record(["c", "d"], purpose="heldout")  # disjoint ids are fine
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_the_ledger_is_durable_across_handles(self):
        jv.ledger(self.path).record(["a"], purpose="tune")
        with self.assertRaises(ValueError):
            jv.ledger(self.path).record(["a"], purpose="heldout")

    def test_agent_and_script_gold_is_reported_as_not_independent(self):
        ledger = jv.ledger(self.path)
        ledger.record(["h1", "h2"], purpose="gold", labeler="reviewer-1", labeler_kind="human")
        ledger.record(["x1"], purpose="gold", labeler="vendor", labeler_kind="external")
        ledger.record(["a1", "s1"], purpose="gold", labeler="omp", labeler_kind="agent")
        ledger.record(["s1"], purpose="gold", labeler="regex", labeler_kind="script")
        ledger.record(["h2"], purpose="gold", labeler="omp", labeler_kind="agent")
        result = ledger.independent_gold()
        self.assertEqual(result["independent"], ["h1", "h2", "x1"])
        self.assertEqual(result["not_independent"], ["a1", "s1"])
        self.assertEqual(result["labeler_kinds"]["h2"], ["agent", "human"])

    def test_invalid_records_are_refused_before_anything_is_written(self):
        ledger = jv.ledger(self.path)
        for kwargs in ({"purpose": "gold"}, {"purpose": "train"}, {"purpose": "tune", "labeler_kind": "robot"}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                ledger.record(["a"], **kwargs)
        for ids in ([], "abc", [1]):
            with self.assertRaises(ValueError):
                ledger.record(ids, purpose="tune")
        self.assertFalse(self.path.exists() and self.path.read_text())

    def test_a_corrupt_ledger_is_refused_and_a_torn_tail_is_cut(self):
        ledger = jv.ledger(self.path)
        ledger.record(["a"], purpose="tune")
        with open(self.path, "ab") as handle:
            handle.write(b'{"ids":["b"],"purp')
        self.assertEqual(len(ledger.entries()), 1)
        ledger.record(["z"], purpose="tune")
        self.assertEqual([json.loads(line)["ids"] for line in self.path.read_text().splitlines()],
                         [["a"], ["z"]])
        with open(self.path, "ab") as handle:
            handle.write(b"not json\n")
        with self.assertRaises(ValueError):
            ledger.entries()


if __name__ == "__main__":
    unittest.main()
