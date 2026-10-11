"""Durable run bundles: checkpointed drains, honest resume, and explicit resubmission.

The fake host keeps batches in a registry that outlives an owner, as a host session does. A simulated
kernel loss (Crash) skips cancel and close, so the batch stays attachable, and nothing else is recorded.
"""

import contextlib
import io
import json
from pathlib import Path
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
Bundle = namespace["_JvBundle"]
JournalError = namespace["_JvJournalError"]

Q = {"safe": jv.yes("Is it safe?")}
STATES = {"a": "alpha", "b": "beta", "c": "gamma", "d": "delta"}


class Crash(BaseException):
    """A kernel loss: finally blocks run in this process, but nothing is recorded afterwards."""


def _item(questions, *, model="jev-primary"):
    return SimpleNamespace(
        ok=True, error=None, model=model, selected_model=model, primary_model=model, fallback=False,
        response_model=model, cache="miss", questions=len(questions), cached_questions=0,
        answers={qid: {"type": "bool", "bool": 0.9} for qid in questions})


class HostBatch:
    """Items complete at submission. drain_iter advances a cursor that every handle shares, so an item the
    cursor passed is never yielded again; results() reports every completed item regardless of the cursor."""

    def __init__(self, host, batch_id, payload, questions):
        self.host, self.id = host, batch_id
        self.total = len(payload)
        self.order = list(payload)
        self.items = {key: _item(questions) for key in payload}
        self.cursor = 0

    async def drain_iter(self, timeout):
        while self.cursor < len(self.order):
            key = self.order[self.cursor]
            self.cursor += 1
            yield key, self.items[key]

    def status(self):
        return {"id": self.id, "done": self.total, "total": self.total, "failed": 0, "cost": 0.25,
                "running": False, "elapsedS": 0}

    def results(self):
        return {key: item.answers for key, item in self.items.items()}

    def failed(self):
        return {}

    def cancel(self):
        if not self.host.killed:
            self.host.cancelled.append(self.id)

    def close(self):
        if not self.host.killed:
            self.host.batches.pop(self.id, None)


class FakeHost:
    """judge_batch stand-in with attach(). `killed` marks the owner dead: cancel and close no longer run."""

    def __init__(self):
        self.batches, self.submissions, self.attach_calls, self.cancelled = {}, [], [], []
        self.killed = False
        self.on_submit = None
        self._count = 0

    def __call__(self, payload, questions, *, concurrency, retries, intent):
        if self.on_submit is not None:
            self.on_submit()
        self._count += 1
        batch_id = f"batch-{self._count}"
        self.batches[batch_id] = HostBatch(self, batch_id, payload, questions)
        self.submissions.append({"id": batch_id, "keys": list(payload), "intent": intent})
        return self.batches[batch_id]

    def attach(self, batch_id):
        self.attach_calls.append(batch_id)
        if batch_id not in self.batches:
            raise LookupError(f"batch {batch_id} is not registered in this host session")
        return self.batches[batch_id]


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if Bundle.host()["boot_id"] is None:
            self.skipTest("bundle recovery needs a Linux boot ID to identify the host session")
        self.host = FakeHost()
        patcher = mock.patch.dict(namespace, {"judge_batch": self.host})
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bundle = self.root / "bundle"

    async def quiet(self, awaitable):
        with contextlib.redirect_stdout(io.StringIO()):
            return await awaitable

    @contextlib.contextmanager
    def crash_on(self, predicate):
        """Simulate losing the owner at the first journal record matching predicate."""
        original = Bundle.append

        def append(bundle, event):
            if predicate(event):
                self.host.killed = True
                raise Crash()
            return original(bundle, event)

        with mock.patch.object(Bundle, "append", append):
            yield

    def events(self):
        path = self.bundle / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    async def interrupted_run(self, predicate, *, states=STATES):
        with self.crash_on(predicate):
            with self.assertRaises(Crash):
                await self.quiet(jv.run(states, Q, intent="interrupted", bundle_path=self.bundle))


class StockAndDurabilityTests(RecoveryTests):
    async def test_stock_run_writes_no_bundle_and_does_not_plan_by_default(self):
        with mock.patch.object(jv, "plan", side_effect=AssertionError("preflight is opt-in")):
            rows = await self.quiet(jv.run(STATES, Q, intent="stock"))
        self.assertEqual(rows.receipt["answered_ids"], list(STATES))
        self.assertIsNone(rows.receipt["bundle"])
        self.assertIsNone(rows.receipt["preflight"])
        self.assertFalse(rows.receipt["cost_uncertain"])
        self.assertFalse(self.bundle.exists())
        self.assertEqual(len(self.host.submissions), 1)

    async def test_plan_and_attempt_are_durable_before_submission_and_batch_id_follows_at_once(self):
        seen = []

        def on_submit():
            text = (self.bundle / "events.jsonl").read_text() if (self.bundle / "events.jsonl").exists() else ""
            seen.append(((self.bundle / "plan.json").exists(), '"event":"attempt"' in text,
                         '"event":"batch"' in text))

        self.host.on_submit = on_submit
        rows = await self.quiet(jv.run(STATES, Q, intent="durable", bundle_path=self.bundle))
        self.assertEqual(seen[0], (True, True, False))
        batch_events = [event for event in self.events() if event["event"] == "batch"]
        self.assertEqual([event["batch_id"] for event in batch_events], ["batch-1"])
        self.assertEqual(rows.receipt["batch_ids"], ["batch-1"])
        self.assertEqual({row["batch_id"] for row in rows}, {"batch-1"})

    async def test_non_string_state_ids_are_refused_before_any_submission_or_bundle(self):
        # JSON would stringify an int key, so the plan digest and replay could never match it again.
        states = {1: "int key", "b": "beta"}
        with self.assertRaisesRegex(ValueError, "state IDs must be strings"):
            await self.quiet(jv.run(states, Q, intent="bad ids", bundle_path=self.bundle))
        with self.assertRaisesRegex(ValueError, "state IDs must be strings"):
            await self.quiet(jv.run(states, Q, intent="bad ids stock"))
        self.assertEqual(self.host.submissions, [])
        self.assertFalse(self.bundle.exists())

    async def test_refuses_to_overwrite_an_existing_plan(self):
        await self.quiet(jv.run(STATES, Q, intent="first", bundle_path=self.bundle))
        with self.assertRaisesRegex(ValueError, "already has a plan"):
            await self.quiet(jv.run(STATES, Q, intent="second", bundle_path=self.bundle))
        self.assertEqual(len(self.host.submissions), 1)

    async def test_resume_without_a_plan_refuses_instead_of_closing_empty(self):
        self.bundle.mkdir()
        with self.assertRaisesRegex(ValueError, "no plan"):
            await self.quiet(jv.resume(self.bundle, intent="resume"))


class InterruptionTests(RecoveryTests):
    async def test_crash_mid_drain_reattaches_and_recovers_the_unjournaled_item_without_provenance(self):
        # c leaves the host cursor and the owner dies before journaling it.
        await self.interrupted_run(lambda event: event["event"] == "item" and event["key"] == "c")
        rows = await self.quiet(jv.resume(self.bundle, intent="resumed"))
        self.assertEqual(self.host.attach_calls, ["batch-1"])
        self.assertEqual(len(self.host.submissions), 1)
        self.assertTrue(rows.receipt["complete"])
        self.assertEqual(rows.receipt["unresolved_ids"], [])
        self.assertTrue(rows.receipt["resumed"])
        by_id = {row["id"]: row for row in rows}
        self.assertFalse(by_id["a"]["provenance_unknown"])
        self.assertFalse(by_id["b"]["provenance_unknown"])
        self.assertTrue(by_id["c"]["provenance_unknown"])
        self.assertFalse(by_id["d"]["provenance_unknown"])
        self.assertFalse(rows.receipt["provenance_complete"])

    async def test_crash_before_any_attempt_record_is_incomplete_never_a_zero_row_closure(self):
        await self.interrupted_run(lambda event: event["event"] == "attempt")
        rows = await self.quiet(jv.resume(self.bundle, intent="resumed"))
        self.assertEqual(len(rows), len(STATES))
        self.assertFalse(rows.receipt["complete"])
        self.assertEqual(sorted(rows.receipt["unresolved_ids"]), sorted(STATES))
        self.assertEqual(self.host.submissions, [])
        self.assertEqual(rows.receipt["resubmit"]["skipped"],
                         "not requested; pass resubmit_missing=True to resubmit missing units")

    async def test_submit_without_recorded_batch_id_is_unknown_and_resubmits_only_when_asked(self):
        await self.interrupted_run(lambda event: event["event"] == "batch")
        rows = await self.quiet(jv.resume(self.bundle, intent="resumed"))
        self.assertEqual(self.host.attach_calls, [])
        self.assertFalse(rows.receipt["complete"])
        self.assertEqual(sorted(rows.receipt["unresolved_ids"]), sorted(STATES))
        self.assertTrue(rows.receipt["cost_uncertain"])
        self.assertTrue(any("submit_unknown" in reason for reason in rows.receipt["cost_uncertainty"]))
        self.assertEqual(len(self.host.submissions), 1)
        done = await self.quiet(jv.resume(self.bundle, intent="resumed", resubmit_missing=True))
        self.assertEqual(len(self.host.submissions), 2)
        self.assertTrue(done.receipt["complete"])
        self.assertEqual(done.receipt["resubmit"]["submitted_ids"], list(STATES))

    async def test_stale_batch_from_another_host_session_stays_incomplete_until_explicit_resubmit(self):
        other = {"hostname": "other-host", "boot_id": "other-boot", "pid": 1}
        with mock.patch.object(Bundle, "host", return_value=other):
            await self.interrupted_run(lambda event: event["event"] == "item" and event["key"] == "c")
        rows = await self.quiet(jv.resume(self.bundle, intent="resumed"))
        self.assertEqual(self.host.attach_calls, [])
        self.assertFalse(rows.receipt["complete"])
        self.assertEqual(sorted(rows.receipt["unresolved_ids"]), ["c", "d"])
        self.assertTrue(rows.receipt["cost_uncertain"])
        self.assertEqual({row["id"] for row in rows if "error" in row}, {"c", "d"})
        self.assertIn("cannot attach", next(row["error"] for row in rows if row["id"] == "c"))
        self.assertEqual(len(self.host.submissions), 1)
        self.assertEqual(rows.receipt["resubmit"]["submitted_ids"], [])

        resubmitted = await self.quiet(jv.resume(self.bundle, intent="resumed", resubmit_missing=True))
        self.assertEqual(self.host.submissions[-1]["keys"], ["c", "d"])
        self.assertTrue(resubmitted.receipt["complete"])
        self.assertEqual(resubmitted.receipt["resubmit"]["submitted_ids"], ["c", "d"])
        by_id = {row["id"]: row for row in resubmitted}
        self.assertFalse(by_id["a"]["provenance_unknown"])
        self.assertEqual(by_id["c"]["batch_id"], "batch-2")

    async def test_live_owner_blocks_resume_and_tampered_plan_is_refused(self):
        await self.quiet(jv.run(STATES, Q, intent="complete", bundle_path=self.bundle))
        owner = Bundle(self.bundle)
        owner.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "owned by a live run"):
                await self.quiet(jv.resume(self.bundle, intent="resume"))
        finally:
            owner.release()
        plan_path = self.bundle / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["intent"] = "edited after the fact"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            await self.quiet(jv.resume(self.bundle, intent="resume"))

    async def test_torn_trailing_record_is_dropped_but_damage_in_a_complete_record_is_refused(self):
        await self.quiet(jv.run(STATES, Q, intent="complete", bundle_path=self.bundle))
        journal = self.bundle / "events.jsonl"
        with journal.open("ab") as output:
            output.write(b'{"event":"item","att')
        rows = await self.quiet(jv.resume(self.bundle, intent="resume"))
        self.assertTrue(rows.receipt["complete"])
        self.assertTrue(journal.read_bytes().endswith(b"\n"))
        lines = journal.read_bytes().splitlines(keepends=True)
        lines.insert(1, b"not json at all\n")
        journal.write_bytes(b"".join(lines))
        with self.assertRaisesRegex(ValueError, "corrupt"):
            await self.quiet(jv.resume(self.bundle, intent="resume"))

    async def test_journal_write_failure_stops_before_the_item_is_accepted(self):
        original = Bundle.append

        def append(bundle, event):
            if event["event"] == "item" and event["key"] == "b":
                raise JournalError("disk full")
            return original(bundle, event)

        with mock.patch.object(Bundle, "append", append):
            with self.assertRaises(JournalError):
                await self.quiet(jv.run(STATES, Q, intent="disk full", bundle_path=self.bundle))
        self.assertIn("batch-1", self.host.cancelled)
        self.assertFalse((self.bundle / "rows.jsonl").exists())
        self.assertNotIn("b", [event.get("key") for event in self.events() if event["event"] == "item"])


class CompletedBundleTests(RecoveryTests):
    async def test_completed_bundle_reloads_without_resubmitting_and_saved_rows_load(self):
        rows = await self.quiet(jv.run(STATES, Q, intent="complete", bundle_path=self.bundle))
        saved = jv.load(self.bundle / "rows.jsonl")
        self.assertEqual([row["id"] for row in saved], [row["id"] for row in rows])
        self.assertEqual(saved.receipt["answered_ids"], rows.receipt["answered_ids"])
        again = await self.quiet(jv.resume(self.bundle, intent="reload"))
        self.assertEqual(again.receipt["answered_ids"], rows.receipt["answered_ids"])
        self.assertEqual(len(self.host.submissions), 1)
        self.assertEqual(self.host.attach_calls, [])

    async def test_grouped_bundle_keeps_slots_and_exclusions_across_resume(self):
        grouping = {"original_ids": ["u1", "u2", "u3", "u4"],
                    "slots": {"g1": ["u1", "u2"], "g2": ["u3"]},
                    "excluded": {"u4": "empty rendered unit"}}
        states = {"g1": {"group": "x", "units": {"s0": "t1", "s1": "t2"}},
                  "g2": {"group": "y", "units": {"s0": "t3"}}}
        rows = await self.quiet(jv.run(states, Q, intent="grouped", grouping=grouping,
                                       bundle_path=self.bundle))
        self.assertEqual([row["id"] for row in rows], ["u1", "u2", "u3"])
        self.assertEqual(rows.receipt["excluded_ids"], ["u4"])
        self.assertFalse(rows.receipt["complete"])
        again = await self.quiet(jv.resume(self.bundle, intent="reload"))
        self.assertEqual([row["id"] for row in again], ["u1", "u2", "u3"])
        self.assertEqual(again.receipt["exclusions"], {"u4": "empty rendered unit"})
        self.assertEqual(again.receipt["group_slots"], grouping["slots"])
        self.assertEqual(len(self.host.submissions), 1)


class PreflightAndExtractionTests(RecoveryTests):
    async def test_preflight_refuses_an_over_budget_request_before_submission(self):
        with mock.patch.object(jv, "jev_tokens", lambda text, **kwargs: 50_000):
            with self.assertRaisesRegex(ValueError, "preflight refuses"):
                await self.quiet(jv.run({"big": "x" * 100_000}, Q, intent="too big",
                                        preflight=True, bundle_path=self.bundle))
        self.assertEqual(self.host.submissions, [])
        self.assertFalse(self.bundle.exists())

    async def test_admitted_preflight_is_recorded_without_its_requests(self):
        rows = await self.quiet(jv.run(STATES, Q, intent="admitted", preflight=True,
                                       bundle_path=self.bundle))
        preflight = rows.receipt["preflight"]
        self.assertTrue(preflight["admissible"])
        self.assertNotIn("requests", preflight)
        self.assertEqual(preflight["inference_calls"], 0)
        plan = json.loads((self.bundle / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["preflight"]["admissible"], True)

    async def test_extraction_manifest_is_bound_to_states_before_submission(self):
        (self.root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        units = {"a.py:1": {"file": "a.py", "line": 1, "end_line": 2, "text": "def a():"}}
        states = {"a.py:1": "def a():"}
        manifest = jv.extraction(units, states, root=self.root)
        rows = await self.quiet(jv.run(states, Q, intent="extracted", extraction=manifest,
                                       bundle_path=self.bundle))
        self.assertEqual(rows.receipt["extraction"]["digest"], manifest["digest"])
        plan = json.loads((self.bundle / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["extraction"]["digest"], manifest["digest"])
        changed = {"a.py:1": "def a(): pass"}
        with self.assertRaisesRegex(ValueError, "extraction does not match"):
            await self.quiet(jv.run(changed, Q, intent="changed", extraction=manifest))
        self.assertEqual(len(self.host.submissions), 1)


if __name__ == "__main__":
    unittest.main()
