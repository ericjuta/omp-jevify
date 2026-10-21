"""Receipt contracts: row-to-input binding, served-model comparability, honest splices, and export."""

import contextlib
import hashlib
import io
import itertools
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]

ANSWERS = {"safe": {"type": "bool", "bool": 0.1}}
Q = {"safe": jv.yes("Is it safe?")}
STATES = {"a": "first unit", "b": "second unit", "c": "third unit"}


class _FakeBatch:
    """Completed judge_batch handle; `status_override` replaces the host's status() report."""

    identifiers = itertools.count(1)

    def __init__(self, payload, items, status_override=None):
        self.id = f"receipt-batch-{next(self.identifiers)}"
        self.total = len(payload)
        self.items = items
        self.status_override = status_override
        for key, item in items.items():
            item.key = key

    async def drain_iter(self, timeout):
        for entry in list(self.items.items()):
            yield entry

    def status(self):
        if self.status_override is not None:
            return dict(self.status_override)
        return {"id": self.id, "done": len(self.items), "total": self.total,
                "failed": sum(not item.ok for item in self.items.values()),
                "cost": 0.125, "running": False, "elapsedS": 0}

    def results(self):
        return {key: item.answers for key, item in self.items.items() if item.ok}

    def failed(self):
        return {key: item.error for key, item in self.items.items() if not item.ok}

    def cancel(self):
        pass

    def close(self):
        pass


def _item(answers=ANSWERS, *, model="jev-1", error=None, selected_model="jev-1"):
    return SimpleNamespace(
        ok=error is None, answers=None if error else answers, error=error, model=model,
        selected_model=selected_model, primary_model="jev-1",
        fallback=selected_model not in (None, "jev-1"),
        response_model="jev-1", cache="miss", questions=1, cached_questions=0)


def _host(items, status_override=None):
    """judge_batch fake: `items` maps unit id to a zero-argument item factory (fresh item per call)."""
    def judge_batch(payload, submitted_questions, **kwargs):
        return _FakeBatch(payload, {key: items[key]() for key in payload}, status_override)
    return judge_batch


class ReceiptCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        previous_stats, previous_frozen = jv.stats, jv.frozen
        self.addCleanup(setattr, jv, "stats", previous_stats)
        self.addCleanup(setattr, jv, "frozen", previous_frozen)
        jv.stats = {"states": {}, "runs": []}
        jv.frozen = None
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    async def run_rows(self, states, items, *, questions=Q, intent="receipts", status_override=None, **kwargs):
        with mock.patch.dict(namespace, {"judge_batch": _host(items, status_override)}), \
                contextlib.redirect_stdout(io.StringIO()):
            return await jv.run(states, questions, intent=intent, **kwargs)

    @staticmethod
    def ok(model="jev-1"):
        return lambda: _item(model=model)


class RowBindingTests(ReceiptCase):
    def test_state_digest_is_canonical_json_sha256(self):
        state = {"b": ["é", 2], "a": {"z": 1, "y": None}}
        canonical = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        self.assertEqual(jv.state_digest(state), hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(jv.state_digest({"a": 1, "b": 2}), jv.state_digest({"b": 2, "a": 1}))
        self.assertNotEqual(jv.state_digest("x"), jv.state_digest("y"))

    def test_row_built_without_a_state_never_fabricates_a_digest(self):
        row = jv._flat_row("x", Q, _item(), "hash")
        self.assertIsNone(row["state_sha256"])
        self.assertIsNone(row["judged_at"])
        self.assertEqual(row["assembly"], "single")

    async def test_flat_rows_are_bound_to_their_submitted_state_and_single_assembly(self):
        rows = await self.run_rows(STATES, {key: self.ok() for key in STATES})
        self.assertEqual([row["id"] for row in rows], list(STATES))
        for row in rows:
            self.assertEqual(row["state_sha256"], jv.state_digest(STATES[row["id"]]))
            self.assertEqual(row["assembly"], "single")
            self.assertIsInstance(row["judged_at"], float)
        self.assertEqual(len({row["state_sha256"] for row in rows}), 3)
        receipt = rows.receipt
        self.assertEqual(receipt["assembly"], "single")
        self.assertEqual(receipt["billing"], "estimate_unverified")
        self.assertEqual(receipt["served_models"], {"jev-1": 3})
        self.assertTrue(receipt["comparable"])
        self.assertEqual(receipt["host_counts"]["host_done"], 3)
        self.assertEqual(receipt["host_counts"]["host_total"], 3)
        self.assertTrue(receipt["host_counts"]["reconciled"])

    async def test_grouped_rows_share_their_group_state_digest(self):
        units = {"a.py:1": {"file": "a.py", "text": "one"}, "a.py:2": {"file": "a.py", "text": "two"}}
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, manifest = jv.group(units, by=lambda unit: unit["file"],
                                         render=lambda unit: unit["text"], k=2)
        items = {gid: (lambda slots=slots: _item({f"safe@s{index}": ANSWERS["safe"]
                                                  for index in range(len(slots))}))
                 for gid, slots in manifest["slots"].items()}
        rows = await self.run_rows(grouped, items, grouping=manifest)
        self.assertEqual([row["id"] for row in rows], ["a.py:1", "a.py:2"])
        for row in rows:
            self.assertEqual(row["state_sha256"], jv.state_digest(grouped[row["group"]]))
        self.assertEqual(len({row["state_sha256"] for row in rows}), 1)

    async def test_resumed_bundle_rows_rebuild_the_same_binding_and_host_counts_stay_unobserved(self):
        bundle = self.tmp / "bundle"
        await self.run_rows(STATES, {key: self.ok() for key in STATES}, bundle_path=bundle)
        refuse = mock.Mock(side_effect=AssertionError("a completed bundle must not resubmit"))
        with mock.patch.dict(namespace, {"judge_batch": refuse}), contextlib.redirect_stdout(io.StringIO()):
            resumed = await jv.resume(bundle, intent="resume")
        for row in resumed:
            self.assertEqual(row["state_sha256"], jv.state_digest(STATES[row["id"]]))
            self.assertEqual(row["assembly"], "single")
        self.assertEqual(resumed.receipt["served_models"], {"jev-1": 3})
        counts = resumed.receipt["host_counts"]
        self.assertIsNone(counts["host_done"])
        self.assertFalse(counts["reconciled"])


class ServedModelTests(ReceiptCase):
    async def test_two_served_models_in_one_run_are_not_comparable(self):
        rows = await self.run_rows(STATES, {"a": self.ok("m1"), "b": self.ok("m2"), "c": self.ok("m1")})
        self.assertEqual(rows.receipt["served_models"], {"m1": 2, "m2": 1})
        self.assertFalse(rows.receipt["comparable"])

    async def test_an_answered_row_without_a_served_model_is_unknown_not_comparable(self):
        rows = await self.run_rows(STATES, {"a": self.ok(), "b": self.ok(None), "c": self.ok()})
        self.assertEqual(rows.receipt["served_models"], {"jev-1": 2})
        self.assertEqual(rows.receipt["served_unknown"], 1)
        self.assertFalse(rows.receipt["comparable"])

    async def test_host_counts_without_host_status_never_reconcile(self):
        rows = await self.run_rows(STATES, {key: self.ok() for key in STATES},
                                   status_override={"cost": 0.5, "running": False})
        counts = rows.receipt["host_counts"]
        self.assertIsNone(counts["host_done"])
        self.assertIsNone(counts["host_total"])
        self.assertFalse(counts["reconciled"])
        self.assertEqual(counts["answered"], 3)

    async def test_host_counts_that_disagree_with_answers_do_not_reconcile(self):
        # The host reports 3 done while only 2 answers were usable.
        rows = await self.run_rows(STATES, {"a": self.ok(), "b": lambda: _item(error="host failed"),
                                            "c": self.ok()})
        counts = rows.receipt["host_counts"]
        self.assertEqual(counts["answered"], 2)
        self.assertFalse(counts["reconciled"])

    async def test_comparable_needs_one_known_shared_served_model_and_one_question_hash(self):
        first = await self.run_rows(STATES, {key: self.ok("m1") for key in STATES})
        same = await self.run_rows(STATES, {key: self.ok("m1") for key in STATES})
        other = await self.run_rows(STATES, {key: self.ok("m2") for key in STATES})
        unknown = await self.run_rows(STATES, {key: self.ok(None) for key in STATES})
        reworded = await self.run_rows(STATES, {key: self.ok("m1") for key in STATES},
                                       questions={"safe": jv.yes("Is it harmless?")})
        self.assertEqual(jv.comparable(first, same), {"comparable": True, "reasons": []})
        different = jv.comparable(first, other)
        self.assertFalse(different["comparable"])
        self.assertTrue(any("served models differ" in reason for reason in different["reasons"]))
        missing = jv.comparable(first, unknown)
        self.assertFalse(missing["comparable"])
        self.assertTrue(any("unknown served model" in reason for reason in missing["reasons"]))
        changed = jv.comparable(first, reworded)
        self.assertFalse(changed["comparable"])
        self.assertTrue(any("question hashes differ" in reason for reason in changed["reasons"]))


class SpliceTests(ReceiptCase):
    async def runs(self, *, patch_fails=False):
        base = await self.run_rows(STATES, {"a": self.ok(), "b": lambda: _item(error="host failed"),
                                            "c": self.ok()}, intent="base sweep")
        patch_item = (lambda: _item(error="host failed again")) if patch_fails else self.ok()
        patch = await self.run_rows({"b": STATES["b"]}, {"b": patch_item}, intent="re-judge b")
        return base, patch

    async def test_splice_marks_rows_and_receipt_spliced_and_never_claims_a_single_sweep(self):
        base, patch = await self.runs()
        self.assertFalse(base.receipt["complete"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            spliced = jv.splice(base, patch)
        self.assertIn("spliced: 1 of 3 re-judged", output.getvalue())
        self.assertEqual([row["id"] for row in spliced], ["a", "b", "c"])
        self.assertEqual({row["id"]: row["assembly"] for row in spliced},
                         {"a": "single", "b": "spliced-patch", "c": "single"})
        self.assertTrue(all("error" not in row for row in spliced))
        receipt = spliced.receipt
        self.assertEqual(receipt["assembly"], "spliced")
        self.assertEqual(receipt["replaced_ids"], ["b"])
        self.assertEqual([(part["intent"], part["units"], part["answered"]) for part in receipt["parts"]],
                         [("base sweep", 3, 2), ("re-judge b", 1, 1)])
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["answered_ids"], ["a", "b", "c"])
        self.assertEqual(receipt["error_ids"], [])
        # The base inputs stay untouched.
        self.assertIn("error", base[1])
        self.assertEqual(base[1]["assembly"], "single")

    async def test_a_patch_that_still_fails_leaves_the_splice_incomplete(self):
        base, patch = await self.runs(patch_fails=True)
        with contextlib.redirect_stdout(io.StringIO()):
            spliced = jv.splice(base, patch)
        self.assertFalse(spliced.receipt["complete"])
        self.assertEqual(spliced.receipt["error_ids"], ["b"])
        self.assertTrue(all(row["coverage_complete"] is False for row in spliced))

    async def test_splice_refuses_different_question_hashes_and_foreign_ids(self):
        base, patch = await self.runs()
        reworded = await self.run_rows({"b": STATES["b"]}, {"b": self.ok()},
                                       questions={"safe": jv.yes("Is it harmless?")})
        with self.assertRaisesRegex(ValueError, "different question hashes"):
            jv.splice(base, reworded)
        stranger = await self.run_rows({"zzz": "new"}, {"zzz": self.ok()})
        with self.assertRaisesRegex(ValueError, "absent from the base"):
            jv.splice(base, stranger)
        with self.assertRaisesRegex(ValueError, "carry their run receipt"):
            jv.splice(list(base), patch)

    async def test_receipt_md_of_a_splice_says_spliced_and_never_a_full_sweep(self):
        base, patch = await self.runs()
        with contextlib.redirect_stdout(io.StringIO()):
            spliced = jv.splice(base, patch)
        text = jv.receipt_md(spliced)
        self.assertIn("spliced: 1 of 3 re-judged", text)
        self.assertIn("not a single full sweep", text)
        self.assertIn("re-judge b", text)
        self.assertIsNone(re.search(r"\b(\d+)/\1\b", text), text)
        self.assertIn("unverified", text)
        self.assertIn("not source-confirmed", text)
        self.assertIn("not observed", text)  # a splice has no host counts to reconcile

    async def test_spliced_rows_round_trip_through_save_load_report_and_flag(self):
        base, patch = await self.runs()
        with contextlib.redirect_stdout(io.StringIO()):
            spliced = jv.splice(base, patch)
            path = self.tmp / "spliced.jsonl"
            jv.save(spliced, path)
            restored = jv.load(path)
            flags = jv.flag(restored)
            report = jv.report(restored, Q)
        self.assertEqual(restored, spliced)
        self.assertEqual(restored.receipt, spliced.receipt)
        self.assertEqual(flags, [])
        self.assertIn("spliced", report)

    async def test_receipt_md_of_a_single_run_reports_counts_without_claiming_reconciliation_it_lacks(self):
        rows = await self.run_rows(STATES, {key: self.ok() for key in STATES},
                                   status_override={"cost": 0.5, "running": False})
        text = jv.receipt_md(rows)
        self.assertIn("answered: 3", text)
        self.assertIn("batch estimate, unverified", text)
        self.assertIn("Host counts: not observed", text)
        self.assertNotIn("spliced", text)
        self.assertIn("{'jev-1': 3}", text)


class ExportTests(ReceiptCase):
    async def rows(self):
        states = {"a": "first", "b|pipe": "second"}
        return states, await self.run_rows(states, {"a": lambda: _item(selected_model=None), "b|pipe": self.ok()})

    async def test_rule_flags_stay_separate_from_judge_flags_and_review_defaults_to_unreviewed(self):
        states, rows = await self.rows()
        with contextlib.redirect_stdout(io.StringIO()):
            text = jv.export(rows, Q=Q, rule_flags={"b|pipe": ["regex: a|b"]}, format="jsonl")
        records = {record["id"]: record for record in map(json.loads, text.splitlines())}
        self.assertEqual(records["a"]["judge_flag"], ["provenance_unknown"])
        self.assertEqual(records["a"]["rule_flags"], [])
        self.assertEqual(records["b|pipe"]["judge_flag"], [])
        self.assertEqual(records["b|pipe"]["rule_flags"], ["regex: a|b"])
        for identifier, record in records.items():
            self.assertEqual(record["review_status"], "unreviewed")
            self.assertEqual(record["assembly"], "single")
            self.assertEqual(record["state_sha256"], jv.state_digest(states[identifier])[:12])
            self.assertEqual(record["safe"], 0.1)

    async def test_markdown_escapes_pipes_and_keeps_every_row_on_one_line(self):
        _, rows = await self.rows()
        path = self.tmp / "export.md"
        with contextlib.redirect_stdout(io.StringIO()):
            text = jv.export(rows, path, Q=Q, rule_flags={"b|pipe": "regex: a|b\nsecond line"})
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)  # header, rule, two rows
        self.assertIn("b\\|pipe", lines[3])
        self.assertIn("regex: a\\|b second line", lines[3])
        separators = len(re.findall(r"(?<!\\)\|", lines[0]))
        for line in lines[1:]:
            self.assertEqual(len(re.findall(r"(?<!\\)\|", line)), separators)
        self.assertIn("rule_flags", lines[0])
        self.assertIn("judge_flag", lines[0])

    async def test_review_status_comes_from_the_durable_review_and_stale_wins(self):
        _, rows = await self.rows()
        entries = [{"unit": "a", "verdict": "confirmed", "stale": None},
                   {"unit": "b|pipe", "verdict": None, "stale": None},
                   {"unit": "b|pipe", "verdict": "confirmed", "stale": None}]
        with mock.patch.object(jv, "review_items", return_value=entries) as review, \
                contextlib.redirect_stdout(io.StringIO()):
            mixed = jv.export(rows, Q=Q, review="review-dir", format="jsonl")
            entries[0] = {"unit": "a", "verdict": "confirmed", "stale": "stale_evidence"}
            stale = jv.export(rows, Q=Q, review="review-dir", format="jsonl")
        review.assert_called_with("review-dir", Q=Q)
        self.assertEqual({r["id"]: r["review_status"] for r in map(json.loads, mixed.splitlines())},
                         {"a": "confirmed", "b|pipe": "mixed"})
        self.assertEqual({r["id"]: r["review_status"] for r in map(json.loads, stale.splitlines())},
                         {"a": "stale", "b|pipe": "mixed"})

    async def test_export_refuses_rule_flags_for_unknown_ids_and_bad_formats(self):
        _, rows = await self.rows()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "not rows"):
                jv.export(rows, Q=Q, rule_flags={"ghost": ["x"]})
            with self.assertRaisesRegex(ValueError, "format"):
                jv.export(rows, Q=Q, format="csv")
            with self.assertRaisesRegex(ValueError, "rule_flags"):
                jv.export(rows, Q=Q, rule_flags=["a"])

    async def test_new_row_fields_survive_save_and_load(self):
        rows = await self.run_rows(STATES, {key: self.ok() for key in STATES})
        path = self.tmp / "rows.jsonl"
        jv.save(rows, path)
        restored = jv.load(path)
        self.assertEqual(restored, rows)
        self.assertEqual(restored.receipt, rows.receipt)
        self.assertEqual([row["state_sha256"] for row in restored],
                         [jv.state_digest(STATES[key]) for key in STATES])


if __name__ == "__main__":
    unittest.main()
