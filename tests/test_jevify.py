"""Behavioral contracts for the jevify extraction, batching, and deletion helpers."""

import contextlib
import copy
import io
import itertools
import json
import re
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
# The %load-oriented script announces itself on execution; keep test output quiet.
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]


# Recipe F from skills/jevify/SKILL.md: a Python test's decorators must be part
# of its deletable span, rather than a separate or duplicated test unit.
TEST_RULE = {"any": [
    {"kind": "decorated_definition", "inside": {"kind": "module"},
     "has": {"kind": "function_definition", "regex": r"^(async\s+)?def\s+test_"}},
    {"kind": "function_definition", "inside": {"kind": "module"},
     "regex": r"^(async\s+)?def\s+test_"},
]}


def _temporary_directory(testcase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    return Path(temporary.name)


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _repository(testcase):
    # Keep repository discovery, identity, hooks, filters, and signing independent
    # of whichever checkout or Git configuration launches the test process.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    patcher = mock.patch.dict(os.environ, environment, clear=True)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    root = _temporary_directory(testcase) / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "--local", "user.name", "Jevify Fixture")
    _git(root, "config", "--local", "user.email", "jevify@example.invalid")
    _git(root, "config", "--local", "core.autocrlf", "false")
    _git(root, "config", "--local", "core.filemode", "true")
    return root


def _commit(root):
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture baseline")


class GitUnitsTests(unittest.TestCase):
    def test_git_units_separated_hunks_and_added_file(self):
        root = _repository(self)
        original = [f"line {line:02d}\n" for line in range(1, 31)]
        (root / "existing.txt").write_text("".join(original), encoding="utf-8")
        _commit(root)

        changed = original.copy()
        changed[1] = "changed line 02\n"
        changed[25] = "changed line 26\n"
        (root / "existing.txt").write_text("".join(changed), encoding="utf-8")
        (root / "added.txt").write_text("brand new\n", encoding="utf-8")
        _git(root, "add", "-A")

        units = jv.git_units("STAGED", by="hunk", cwd=root, context=1)
        self.assertEqual(
            set(units), {"existing.txt:+1", "existing.txt:+25", "added.txt:+1"}
        )
        first, second = units["existing.txt:+1"], units["existing.txt:+25"]
        self.assertEqual((first["line"], first["old_line"], first["status"]), (1, 1, "modified"))
        self.assertEqual((second["line"], second["old_line"], second["status"]), (25, 25, "modified"))
        self.assertIn("+changed line 02", first["hunk"])
        self.assertNotIn("changed line 26", first["hunk"])
        self.assertIn("+changed line 26", second["hunk"])
        added = units["added.txt:+1"]
        self.assertEqual((added["file"], added["status"], added["line"]),
                         ("added.txt", "added", 1))
        self.assertIn("+brand new", added["hunk"])
        files = jv.git_units("STAGED", by="file", cwd=root)
        self.assertEqual({path: unit["status"] for path, unit in files.items()},
                         {"existing.txt": "modified", "added.txt": "added"})

    def test_git_units_file_statuses_and_rename_binary_mode_metadata(self):
        root = _repository(self)
        (root / "old.txt").write_text("same content after moving\n", encoding="utf-8")
        (root / "gone.txt").write_text("deleted line\n", encoding="utf-8")
        (root / "binary.bin").write_bytes(b"\x00previous bytes\n")
        (root / "mode.txt").write_text("mode only\n", encoding="utf-8")
        _commit(root)

        _git(root, "mv", "old.txt", "renamed.txt")
        _git(root, "rm", "gone.txt")
        (root / "binary.bin").write_bytes(b"\x00different bytes\n")
        mode = root / "mode.txt"
        mode.chmod(mode.stat().st_mode | stat.S_IXUSR)
        _git(root, "add", "-A")

        files = jv.git_units("STAGED", by="file", cwd=root)
        self.assertEqual({key: unit["status"] for key, unit in files.items()}, {
            "renamed.txt": "renamed", "gone.txt": "deleted",
            "binary.bin": "binary", "mode.txt": "modified",
        })
        self.assertIn("rename to renamed.txt", files["renamed.txt"]["diff"])
        hunks = jv.git_units("STAGED", by="hunk", cwd=root)
        self.assertEqual(set(hunks), {
            "renamed.txt:meta", "gone.txt:-1", "binary.bin:meta", "mode.txt:meta"
        })
        for name, status in (("renamed.txt", "renamed"), ("binary.bin", "binary"),
                             ("mode.txt", "modified")):
            unit = hunks[f"{name}:meta"]
            self.assertEqual((unit["file"], unit["status"], unit["header"],
                              unit["line"], unit["old_line"]),
                             (name, status, "", None, None))
        self.assertIn("Binary files ", hunks["binary.bin:meta"]["hunk"])
        self.assertIn("old mode ", hunks["mode.txt:meta"]["hunk"])
        self.assertIn("new mode ", hunks["mode.txt:meta"]["hunk"])
        self.assertEqual(hunks["gone.txt:-1"]["status"], "deleted")


class RgUnitsTests(unittest.TestCase):
    def test_rg_units_match_ids_text_context_and_empty_result(self):
        root = _temporary_directory(self)
        (root / "sample.txt").write_text(
            "before\nneedle alpha\nafter\nseparator\nNEEDLE beta\nend\n", encoding="utf-8"
        )
        (root / "excluded.md").write_text("needle excluded\n", encoding="utf-8")
        units = jv.rg_units("needle", cwd=root, fixed=True, case=False,
                            globs=["*.txt"], context=1)
        self.assertEqual(set(units), {"sample.txt:2", "sample.txt:5"})
        self.assertEqual(units["sample.txt:2"]["file"], "sample.txt")
        self.assertEqual(units["sample.txt:2"]["line"], 2)
        self.assertEqual(units["sample.txt:2"]["match"], "needle alpha")
        self.assertEqual(units["sample.txt:5"]["match"], "NEEDLE beta")
        self.assertIn(">   2| needle alpha", units["sample.txt:2"]["context"])
        self.assertIn("    3| after", units["sample.txt:2"]["context"])
        self.assertEqual(jv.rg_units("not in any file", cwd=root, fixed=True), {})


class DeleteSpansTests(unittest.TestCase):
    def test_delete_spans_dry_run_merges_overlap_and_adjacency_preserving_bytes(self):
        root = _temporary_directory(self)
        path = root / "sample.py"
        original = b"before\t\r\nA\r\nB\nC\r\nD\n \t\r\nmiddle\t\r\nZ\n\nafter \xc3\xa9\nfinal-no-eol"
        path.write_bytes(original)
        units = {
            "first": {"file": "sample.py", "line": 2, "end_line": 3},
            "overlap": {"file": "sample.py", "line": 3, "end_line": 4},
            "adjacent": {"file": "sample.py", "line": 5, "end_line": 5},
            "later": {"file": "sample.py", "line": 8, "end_line": 8},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            dry = jv.delete_spans(units, ["first", "overlap", "adjacent", "later"], root=root)
        self.assertEqual(dry, {"sample.py": 7})
        self.assertEqual(path.read_bytes(), original)

        with contextlib.redirect_stdout(io.StringIO()):
            applied = jv.delete_spans(units, ["first", "overlap", "adjacent", "later"],
                                      root=root, dry_run=False)
        self.assertEqual(applied, dry)
        self.assertEqual(path.read_bytes(), b"before\t\r\nmiddle\t\r\nafter \xc3\xa9\nfinal-no-eol")

    def test_delete_spans_unknown_ids_do_not_mutate_files(self):
        root = _temporary_directory(self)
        path = root / "sample.py"
        original = b"keep\r\n"
        path.write_bytes(original)
        with contextlib.redirect_stdout(io.StringIO()):
            result = jv.delete_spans({}, ["unknown:9"], root=root, dry_run=False)
        self.assertEqual(result, {})
        self.assertEqual(path.read_bytes(), original)


class _FakeBatch:
    """Completed judge_batch handle with the documented drain and result interface."""

    identifiers = itertools.count(1)

    def __init__(self, payload, items):
        self.id = f"fake-batch-{next(self.identifiers)}"
        self.total = len(payload)
        self.items = items
        for key, item in items.items():
            item.key = key
        self.closed = False
        self.cancelled = False
        self.drain_timeouts = []

    async def drain(self, timeout):
        self.drain_timeouts.append(timeout)
        return list(self.items.items())

    async def drain_iter(self, timeout):
        for entry in await self.drain(timeout):
            yield entry

    def status(self):
        return {"id": self.id, "done": len(self.items), "total": self.total,
                "failed": sum(not item.ok for item in self.items.values()),
                "cost": 0.125, "running": False, "elapsedS": 0,
                "model": "batch-level-model-is-not-an-item-model"}

    def results(self):
        return {key: item.answers for key, item in self.items.items() if item.ok}

    def failed(self):
        return {key: item.error for key, item in self.items.items() if not item.ok}

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def _item(answers, *, model="jev-primary", error=None,
          selected_model="jev-primary", primary_model="jev-primary", fallback=False,
          response_model="jev-primary", cache="miss", questions=1, cached_questions=0):
    return SimpleNamespace(
        ok=error is None, answers=answers, error=error, model=model,
        selected_model=selected_model, primary_model=primary_model, fallback=fallback,
        response_model=response_model, cache=cache, questions=questions,
        cached_questions=cached_questions,
    )


def _choice(label):
    return {"type": "choice", "choice": label, "confidence": 0.9,
            "probabilities": {"KEEP": 0.1 if label == "PURGE" else 0.9,
                              "PURGE": 0.9 if label == "PURGE" else 0.1}}


class RunAndFlagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        previous_stats, previous_frozen = jv.stats, jv.frozen
        self.addCleanup(setattr, jv, "stats", previous_stats)
        self.addCleanup(setattr, jv, "frozen", previous_frozen)
        jv.stats = {"states": {}, "runs": []}
        jv.frozen = None
        _FakeBatch.identifiers = itertools.count(1)

    def _saved_rows(self, rows):
        path = _temporary_directory(self) / "rows.jsonl"
        jv.save(rows, path)
        restored = jv.load(path)
        self.assertEqual(restored, rows)
        self.assertEqual(restored.receipt, rows.receipt)
        saved_lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(saved_lines[0]), {"jevify_receipt": rows.receipt})
        for row in restored:
            self.assertIs(row["coverage_complete"], restored.receipt["complete"])
            self.assertEqual(row["coverage_errors"], restored.receipt["coverage_errors"])
        return restored

    def _receipt(self, output):
        objects = [json.loads(line) for line in output.getvalue().splitlines()
                   if line.lstrip().startswith("{")]
        receipts = [value["jevify_receipt"] for value in objects if "jevify_receipt" in value]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(jv.stats["runs"][-1], receipts[0])
        return receipts[0]

    def _assert_receipt(self, output, expected):
        receipt = self._receipt(output)
        self.assertEqual({key: receipt[key] for key in expected}, expected)
        return receipt

    def _report_count(self, report, label):
        match = re.search(rf"{re.escape(label)}:\s*(\d+)", report, re.IGNORECASE)
        self.assertIsNotNone(match, f"Missing {label} counter in report")
        return int(match.group(1))

    async def test_mixed_cached_and_fresh_responses_do_not_create_false_fallbacks(self):
        alias = "openrouter/~typesafe/jev-latest"
        response = "typesafe/jev-1.13-20260917"
        states = {"cached": "previously judged", "fresh": "new input", "partial": "one answer cached"}
        questions = {"safe": jv.yes("Is it safe?"), "ready": jv.yes("Is it ready?")}
        answers = {"safe": {"type": "bool", "bool": 0.1},
                   "ready": {"type": "bool", "bool": 0.8}}
        batches = []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            items = {
                "cached": _item(answers, model=alias, selected_model=alias, primary_model=alias,
                                response_model=None, cache="hit", questions=2, cached_questions=2),
                "fresh": _item(answers, model=f"openrouter/{response}", selected_model=alias,
                               primary_model=alias, response_model=response, questions=2),
                "partial": _item(answers, model=f"openrouter/{response}", selected_model=alias,
                                 primary_model=alias, response_model=response, cache="partial",
                                 questions=2, cached_questions=1),
            }
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        output = io.StringIO()
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(output):
            rows = await jv.run(states, questions, intent="mixed cache provenance")
            restored = self._saved_rows(rows)
            self.assertEqual(jv.flag(restored), [])
            # A real policy flag still applies; cache provenance must not suppress rules.
            policy_flags = jv.flag(restored, lambda row: row["ready"] >= 0.5)
            report = jv.report(restored, questions)
        self.assertEqual([(row["id"], row["why"]) for row in policy_flags],
                         [(identifier, ["rule"]) for identifier in states])
        self.assertEqual(restored[0]["model"], alias)
        self.assertIsNone(restored[0]["response_model"])
        self.assertEqual(restored[1]["response_model"], response)
        self.assertEqual(self._report_count(report, "fallback rows"), 0)
        self.assertEqual(self._report_count(report, "provenance unknown rows"), 0)
        self.assertEqual(self._report_count(report, "errors"), 0)
        receipt = self._receipt(output)
        self.assertEqual(receipt["strategy"], "jev-flat")
        self.assertEqual(receipt["batch_ids"], [batches[0].id])
        self.assertEqual(receipt["unit_ids"], list(states))
        self.assertEqual(receipt["answered_ids"], list(states))
        self.assertEqual(receipt["cache_counts"], {"hit": 1, "partial": 1, "miss": 1, "unknown": 0})
        self.assertEqual(receipt["questions"], 6)
        self.assertEqual(receipt["cached_questions"], 3)
        self.assertEqual(receipt["question_counts_unknown"], 0)
        self.assertEqual(receipt["error_ids"], [])
        self.assertTrue(batches[0].closed and not batches[0].cancelled)

    async def test_all_fallback_rows_remain_flagged_after_save_load(self):
        states = {"first": "first input", "second": "second input"}
        questions = {"safe": jv.yes("Is it safe?")}
        selected = "openrouter/secondary"
        # Even identical display models cannot prove that the primary route succeeded.
        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            return _FakeBatch(payload, {
                identifier: _item({"safe": {"type": "bool", "bool": 0.1}},
                                  model="shared-display-model", selected_model=selected,
                                  primary_model="openrouter/primary", fallback=True,
                                  response_model="secondary-response")
                for identifier in payload
            })

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
            rows = await jv.run(states, questions, intent="all fallback")
            restored = self._saved_rows(rows)
            flags = jv.flag(restored, lambda row: False)
            report = jv.report(restored, questions)
        self.assertEqual([(row["id"], row["why"]) for row in flags],
                         [(identifier, [f"fallback:{selected}"]) for identifier in states])
        self.assertEqual(self._report_count(report, "fallback rows"), 2)
        self.assertEqual(self._report_count(report, "provenance unknown rows"), 0)
        self.assertEqual(self._report_count(report, "errors"), 0)

    async def test_missing_route_provenance_is_not_cleared_by_matching_display_models(self):
        states = {"known": "known route", "legacy": "old host item"}
        questions = {"safe": jv.yes("Is it safe?")}

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            answers = {"safe": {"type": "bool", "bool": 0.1}}
            return _FakeBatch(payload, {
                "known": _item(answers),
                "legacy": SimpleNamespace(ok=True, answers=answers, error=None, model="jev-primary"),
            })

        output = io.StringIO()
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(output):
            rows = await jv.run(states, questions, intent="legacy provenance")
            restored = self._saved_rows(rows)
            flags = jv.flag(restored, lambda row: False)
            report = jv.report(restored, questions)
        self.assertEqual([(row["id"], row["why"]) for row in flags],
                         [("legacy", ["provenance_unknown"])])
        self.assertEqual(restored[1]["safe"], 0.1)
        self.assertNotIn("error", restored[1])
        self.assertEqual(self._report_count(report, "provenance unknown rows"), 1)
        self.assertEqual(self._report_count(report, "fallback rows"), 0)
        self.assertEqual(self._report_count(report, "errors"), 0)
        receipt = self._receipt(output)
        self.assertEqual(receipt["answered_ids"], list(states))
        self.assertEqual(receipt["error_ids"], [])
        self.assertTrue(receipt["complete"])
        self.assertFalse(receipt["provenance_complete"])
        self.assertEqual(receipt["batches"][0]["items"][1], {
            "id": "legacy", "batch_id": receipt["batch_ids"][0],
            "selected_model": None, "primary_model": None, "response_model": None,
            "fallback": None, "cache": None, "questions": None, "cached_questions": None,
        })

    async def test_host_coverage_errors_cannot_claim_complete_success(self):
        states = {"wanted": "one intended unit"}
        questions = {"safe": jv.yes("Is it safe?")}
        scenarios = {
            "missing-id": "host batch ID missing",
            "unexpected-key": "unexpected host key: unplanned",
            "duplicate-key": "duplicate host key: wanted",
        }

        for scenario, expected_error in scenarios.items():
            with self.subTest(scenario=scenario):
                batches = []

                class CoverageBatch(_FakeBatch):
                    async def drain_iter(self, timeout):
                        async for entry in super().drain_iter(timeout):
                            yield entry
                        if scenario == "unexpected-key":
                            yield "unplanned", _item({"safe": {"type": "bool", "bool": 0.9}})
                        elif scenario == "duplicate-key":
                            # Do not let a second conflicting answer overwrite the first.
                            yield "wanted", _item({"safe": {"type": "bool", "bool": 0.9}})

                def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
                    batch = CoverageBatch(payload, {
                        "wanted": _item({"safe": {"type": "bool", "bool": 0.1}}),
                    })
                    if scenario == "missing-id":
                        batch.id = None
                    batches.append(batch)
                    return batch

                path = _temporary_directory(self) / "receipt.json"
                output = io.StringIO()
                with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(output):
                    rows = await jv.run(states, questions, intent="host coverage", receipt_path=path)
                    restored = self._saved_rows(rows)
                    flags = jv.flag(restored, lambda row: row["safe"] >= 0.5)
                    report = jv.report(restored, questions)
                receipt = self._assert_receipt(output, {
                    "unit_ids": ["wanted"], "answered_ids": ["wanted"], "error_ids": [],
                    "coverage_errors": [expected_error], "complete": False,
                    "batch_ids": [] if scenario == "missing-id" else [batches[0].id],
                })
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), receipt)
                self.assertEqual(restored[0]["safe"], 0.1)
                self.assertEqual([(row["id"], row["why"]) for row in flags],
                                 [("wanted", ["coverage_incomplete"])])
                self.assertEqual(jv.flag(rows, lambda row: False), flags)
                self.assertNotIn("error", restored[0])
                self.assertEqual(restored[0]["selected_model"], "jev-primary")
                self.assertEqual(restored[0]["primary_model"], "jev-primary")
                self.assertEqual(restored[0]["response_model"], "jev-primary")
                self.assertIs(restored[0]["fallback"], False)
                self.assertIs(restored[0]["provenance_unknown"], False)
                self.assertEqual(receipt["batches"][0]["items"], [{
                    "id": "wanted", "batch_id": batches[0].id,
                    "selected_model": "jev-primary", "primary_model": "jev-primary",
                    "response_model": "jev-primary", "fallback": False,
                    "cache": "miss", "questions": 1, "cached_questions": 0,
                }])
                self.assertTrue(receipt["provenance_complete"])
                self.assertEqual(self._report_count(report, "errors"), 0)
                self.assertEqual(len(batches), 1)
                self.assertTrue(batches[0].closed and not batches[0].cancelled)

    async def test_old_rows_keep_their_own_coverage_after_an_opposite_new_run(self):
        states = {"same-unit": "same state"}
        questions = {"safe": jv.yes("Is it safe?")}
        for old_complete in (False, True):
            with self.subTest(old_complete=old_complete):
                completes = iter((old_complete, not old_complete))

                class BindingBatch(_FakeBatch):
                    async def drain_iter(self, timeout):
                        async for entry in super().drain_iter(timeout):
                            yield entry
                        if not self.complete:
                            yield "unexpected", _item({"safe": {"type": "bool", "bool": 0.9}})

                def judge_batch(payload, submitted_questions, **kwargs):
                    batch = BindingBatch(payload, {
                        uid: _item({"safe": {"type": "bool", "bool": 0.1}}) for uid in payload
                    })
                    batch.complete = next(completes)
                    return batch

                with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
                    old = await jv.run(states, questions, intent="older run")
                    old_receipt = copy.deepcopy(old.receipt)
                    new = await jv.run(states, questions, intent="newer run")
                    restored = self._saved_rows(old)
                    self.assertIs(new.receipt["complete"], not old_complete)
                    self.assertIs(jv.stats["runs"][-1]["complete"], not old_complete)
                    for result in (old, restored):
                        self.assertEqual(result.receipt, old_receipt)
                        self.assertIs(result.receipt["complete"], old_complete)
                        flags = jv.flag(result, lambda row: False)
                        self.assertEqual([(row["id"], row["why"]) for row in flags],
                                         [] if old_complete else [("same-unit", ["coverage_incomplete"])])
                        report = jv.report(result, questions)
                        self.assertRegex(report, rf"(?i)coverage complete:\s*{old_complete}")
                        self.assertNotRegex(report, rf"(?i)coverage complete:\s*{not old_complete}")
                        self.assertIn(old_receipt["batch_ids"][0], report)
                        self.assertNotIn(new.receipt["batch_ids"][0], report)
                        self.assertEqual(result[0]["safe"], 0.1)
                        self.assertNotIn("error", result[0])

    async def test_only_bound_complete_empty_run_can_pass_flagging(self):
        questions = {"safe": jv.yes("Is it safe?")}
        host = mock.Mock(side_effect=AssertionError("empty run must not call host"))
        with mock.patch.dict(namespace, {"judge_batch": host}), contextlib.redirect_stdout(io.StringIO()):
            rows = await jv.run({}, questions, intent="empty complete run")
            restored = self._saved_rows(rows)
            self.assertEqual(rows, [])
            self.assertIs(rows.receipt["complete"], True)
            self.assertEqual(jv.flag(rows), [])
            self.assertEqual(jv.flag(restored), [])
            for unbound in ([], list(rows)):
                with self.assertRaises(ValueError):
                    jv.flag(unbound)
        host.assert_not_called()

    def test_loading_rows_without_a_valid_receipt_header_fails_closed(self):
        path = _temporary_directory(self) / "rows.jsonl"
        invalid = {
            "missing-header": '{"id":"one","safe":0.1}\n',
            "empty-file": "",
            "list-header": "[]\n",
            "scalar-receipt": '{"jevify_receipt":true}\n',
            "extra-header-key": '{"jevify_receipt":null,"other":true}\n',
            "broken-json": '{"jevify_receipt":\n',
        }
        for case, content in invalid.items():
            with self.subTest(case=case):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    jv.load(path)

    async def test_loading_truncated_saved_run_fails_closed(self):
        states = {"first": "first unit", "second": "second unit"}
        questions = {"safe": jv.yes("Is it safe?")}
        answers = {"safe": {"type": "bool", "bool": 0.1}}

        def judge_batch(payload, submitted_questions, **kwargs):
            return _FakeBatch(payload, {uid: _item(answers) for uid in payload})

        path = _temporary_directory(self) / "rows.jsonl"
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
            rows = await jv.run(states, questions, intent="saved truncation baseline")
            jv.save(rows, path)
            restored = jv.load(path)
            self.assertTrue(rows)
            self.assertEqual(len(rows), 2)
            self.assertIs(rows.receipt["complete"], True)
            self.assertEqual([row["id"] for row in rows], list(states))
            self.assertEqual([row["safe"] for row in rows], [0.1, 0.1])
            self.assertEqual(restored, rows)
            self.assertEqual(restored.receipt, rows.receipt)
            self.assertEqual(jv.flag(restored), [])
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(len(lines), 3)
        for case, content in (("header-only", lines[0]),
                              ("header-and-first-row", lines[0] + lines[1])):
            with self.subTest(case=case):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    jv.load(path)

    async def test_loading_mutated_saved_inventory_fails_closed(self):
        states = {"first": "first unit", "second": "second unit"}
        questions = {"safe": jv.yes("Is it safe?")}
        answers = {"safe": {"type": "bool", "bool": 0.1}}

        def judge_batch(payload, submitted_questions, **kwargs):
            return _FakeBatch(payload, {uid: _item(answers) for uid in payload})

        path = _temporary_directory(self) / "rows.jsonl"
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
            rows = await jv.run(states, questions, intent="saved inventory baseline")
            jv.save(rows, path)
            restored = jv.load(path)
            self.assertEqual(restored, rows)
            self.assertEqual(restored.receipt, rows.receipt)
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["id"] for row in rows], ["first", "second"])
            self.assertEqual([row["safe"] for row in rows], [0.1, 0.1])
            self.assertTrue(all("error" not in row for row in rows))
            self.assertEqual(rows.receipt["unit_ids"], ["first", "second"])
            self.assertEqual(rows.receipt["answered_ids"], ["first", "second"])
            self.assertEqual(rows.receipt["error_ids"], [])
            self.assertIs(rows.receipt["complete"], True)
            self.assertEqual(jv.flag(restored), [])
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        header = lines[0]
        original = [json.loads(line) for line in lines[1:]]
        self.assertEqual(len(original), 2)
        for case in ("duplicate-id", "appended-unknown-id", "replaced-unknown-id"):
            with self.subTest(case=case):
                mutated = copy.deepcopy(original)
                if case == "duplicate-id":
                    mutated[1]["id"] = mutated[0]["id"]
                elif case == "appended-unknown-id":
                    extra = copy.deepcopy(original[0])
                    extra["id"] = "unknown"
                    mutated.append(extra)
                else:
                    mutated[0]["id"] = "unknown"
                path.write_text(header + "".join(json.dumps(row) + "\n" for row in mutated),
                                encoding="utf-8")
                with self.assertRaises(ValueError):
                    jv.load(path)

    async def test_loading_mutated_saved_result_partition_fails_closed(self):
        states = {"success": "successful unit", "failed": "failed unit"}
        questions = {"safe": jv.yes("Is it safe?")}
        answers = {"safe": {"type": "bool", "bool": 0.1}}
        calls = []
        batches = []
        results = []

        def judge_batch(payload, submitted_questions, **kwargs):
            calls.append(list(payload))
            items = {uid: _item(answers) if uid == "success"
                     else _item(None, error="host failed") for uid in payload}
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            results.append(batch.results())
            return batch

        path = _temporary_directory(self) / "rows.jsonl"
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
            rows = await jv.run(states, questions, intent="saved result partition baseline")
            self.assertEqual(calls, [["success", "failed"], ["failed"]])
            self.assertEqual(len(batches), 2)
            for batch in batches:
                self.assertIs(batch.items["failed"].ok, False)
                self.assertEqual(batch.items["failed"].error, "host failed")
            self.assertIs(batches[0].items["success"].ok, True)
            self.assertEqual(batches[0].items["success"].answers, answers)
            self.assertEqual(batches[0].results(), {"success": answers})
            self.assertEqual(batches[1].results(), {})
            self.assertEqual(results, [{"success": answers}, {}])
            self.assertEqual(rows.receipt["unit_ids"], ["success", "failed"])
            self.assertEqual(rows.receipt["answered_ids"], ["success"])
            self.assertEqual(rows.receipt["error_ids"], ["failed"])
            self.assertIs(rows.receipt["complete"], False)
            self.assertEqual([row["id"] for row in rows], ["success", "failed"])
            self.assertEqual(rows[0]["safe"], 0.1)
            self.assertNotIn("error", rows[0])
            self.assertEqual(rows[1]["error"], "host failed")
            jv.save(rows, path)
            restored = jv.load(path)
            self.assertEqual(restored, rows)
            self.assertEqual(restored.receipt, rows.receipt)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        header = lines[0]
        original = [json.loads(line) for line in lines[1:]]
        for case in ("removed-failure-error", "added-success-error"):
            with self.subTest(case=case):
                mutated = copy.deepcopy(original)
                by_id = {row["id"]: row for row in mutated}
                if case == "removed-failure-error":
                    del by_id["failed"]["error"]
                else:
                    by_id["success"]["error"] = "host failed"
                path.write_text(header + "".join(json.dumps(row) + "\n" for row in mutated),
                                encoding="utf-8")
                with self.assertRaises(ValueError):
                    jv.load(path)

    async def test_reserved_answer_columns_are_rejected_before_host_calls(self):
        reserved_columns = (
            "id", "model", "qhash", "batch_id", "selected_model", "primary_model",
            "fallback", "response_model", "cache", "questions", "cached_questions",
            "provenance_unknown", "group", "slot", "error", "why",
            "coverage_complete", "coverage_errors",
        )
        units = {
            "a.py:1": {"file": "a.py", "text": "first line"},
            "a.py:2": {"file": "a.py", "text": "second line"},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            grouped_states, manifest = jv.group(
                units, by=lambda unit: unit["file"],
                render=lambda unit: unit["text"], k=2,
            )
        schemas = (
            ("bool", jv.yes("Is it safe?")),
            ("choice", jv.choice("Choose disposition.", KEEP="Keep this unit.",
                                 PURGE="Purge this unit.")),
        )
        for reserved in reserved_columns:
            for schema, question in schemas:
                for mode, states, grouping in (
                    ("flat", units, None), ("grouped", grouped_states, manifest),
                ):
                    with self.subTest(reserved=reserved, schema=schema, mode=mode):
                        host = mock.Mock(side_effect=AssertionError(
                            "judge_batch must not run for colliding columns"))
                        rubric = {reserved: question}
                        runs_before = list(jv.stats["runs"])
                        with mock.patch.dict(namespace, {"judge_batch": host}):
                            try:
                                with self.assertRaisesRegex(ValueError, "collides"):
                                    await jv.run(states, rubric,
                                                 intent="reserved answer column rejection",
                                                 grouping=grouping)
                            finally:
                                try:
                                    host.assert_not_called()
                                finally:
                                    self.assertEqual(jv.stats["runs"], runs_before)

    async def test_generated_answer_column_overlaps_are_rejected_before_host_calls(self):
        units = {
            "a.py:1": {"file": "a.py", "text": "first line"},
            "a.py:2": {"file": "a.py", "text": "second line"},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            grouped_states, manifest = jv.group(
                units, by=lambda unit: unit["file"],
                render=lambda unit: unit["text"], k=2,
            )
        choice = jv.choice("Choose disposition.", KEEP="Keep this unit.",
                           PURGE="Purge this unit.")
        score = jv.scale("Rate this unit.", "low", "high")
        boolean = jv.yes("Is it safe?")
        cases = (
            ("choice_conf", choice, "q.conf"),
            ("score_conf", score, "q.conf"),
            ("choice_label", choice, "q.KEEP"),
        )
        for case, question, other_qid in cases:
            for order, rubric in (
                ("question-first", dict((("q", question), (other_qid, boolean)))),
                ("bool-first", dict(((other_qid, boolean), ("q", question)))),
            ):
                for mode, states, grouping in (
                    ("flat", units, None), ("grouped", grouped_states, manifest),
                ):
                    with self.subTest(case=case, order=order, mode=mode):
                        host = mock.Mock(side_effect=AssertionError(
                            "judge_batch must not run for colliding columns"))
                        runs_before = list(jv.stats["runs"])
                        with mock.patch.dict(namespace, {"judge_batch": host}):
                            try:
                                with self.assertRaisesRegex(ValueError, "collides"):
                                    await jv.run(states, rubric,
                                                 intent="generated answer column rejection",
                                                 grouping=grouping)
                            finally:
                                try:
                                    host.assert_not_called()
                                finally:
                                    self.assertEqual(jv.stats["runs"], runs_before)

        self_question = jv.choice("Choose disposition.", conf="Confidence label.",
                                  KEEP="Keep this unit.")
        rubric = {"q": self_question}
        for mode, states, grouping in (
            ("flat", units, None), ("grouped", grouped_states, manifest),
        ):
            with self.subTest(case="self_overlap", mode=mode):
                host = mock.Mock(side_effect=AssertionError(
                    "judge_batch must not run for colliding columns"))
                runs_before = list(jv.stats["runs"])
                with mock.patch.dict(namespace, {"judge_batch": host}):
                    try:
                        with self.assertRaisesRegex(ValueError, "collides"):
                            await jv.run(states, rubric,
                                         intent="generated answer column rejection",
                                         grouping=grouping)
                    finally:
                        try:
                            host.assert_not_called()
                        finally:
                            self.assertEqual(jv.stats["runs"], runs_before)

    async def test_malformed_grouping_manifests_cannot_call_host(self):
        questions = {"safe": jv.yes("Is it safe?")}
        states = {"group": {"units": {"s0": "first", "s1": "second"}}}
        valid = {"original_ids": ["first", "second", "empty"],
                 "slots": {"group": ["first", "second"]},
                 "excluded": {"empty": "empty rendered unit"}}
        malformed = {
            "missing-original-list": {key: value for key, value in valid.items()
                                      if key != "original_ids"},
            "missing-original-id": {**valid, "original_ids": ["first", "empty"]},
            "duplicate-original-id": {**valid, "original_ids": ["first", "second", "empty", "first"]},
            "unaccounted-original-id": {**valid, "original_ids": [*valid["original_ids"], "lost"]},
            "overlapping-exclusion": {**valid, "excluded": {"empty": "empty", "first": "overlap"}},
            "unknown-exclusion": {**valid, "excluded": {"empty": "empty", "alien": "unknown"}},
            "missing-exclusion": {**valid, "excluded": {}},
            "blank-exclusion-reason": {**valid, "excluded": {"empty": "  "}},
            "invalid-exclusion-reason": {**valid, "excluded": {"empty": None}},
            "duplicate-slot-unit": {**valid, "slots": {"group": ["first", "first"]}},
            "missing-slot": {**valid, "slots": {"group": ["first"]},
                             "excluded": {"second": "excluded", "empty": "empty"}},
            "invented-slot": {**valid, "slots": {"group": ["first", "second", "empty"]},
                              "excluded": {}},
            "unknown-group": {**valid, "slots": {"other": ["first", "second"]}},
            "missing-group": {**valid, "slots": {}},
            "legacy-slot-map": valid["slots"],
        }
        host = mock.Mock(side_effect=AssertionError("invalid manifests must not reach the host"))
        for case, manifest in malformed.items():
            with self.subTest(case=case), mock.patch.dict(namespace, {"judge_batch": host}):
                with self.assertRaises(ValueError):
                    await jv.run(states, questions, intent="invalid group coverage", grouping=manifest)
                host.assert_not_called()
                self.assertEqual(jv.stats["runs"], [])

    async def test_grouping_exclusions_preserve_original_inventory_and_surviving_slots(self):
        units = {"empty:first": "", "keep:first": "first", "empty:middle": "  ",
                 "keep:second": "second", "empty:last": "", "keep:third": "third"}
        expected_excluded = ["empty:first", "empty:middle", "empty:last"]
        questions = {"verdict": jv.choice("Assess", KEEP="useful", PURGE="weak")}
        decisions = {"first": "KEEP", "second": "PURGE", "third": "KEEP"}
        calls = []

        def judge_batch(payload, submitted_questions, **kwargs):
            calls.append(copy.deepcopy(payload))
            return _FakeBatch(payload, {
                gid: _item({f"verdict@{slot}": _choice(decisions[text])
                            for slot, text in state["units"].items()},
                           questions=len(submitted_questions))
                for gid, state in payload.items()
            })

        output = io.StringIO()
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(output):
            grouped, manifest = jv.group(units, by=lambda text: "review", k=2)
            rows = await jv.run(grouped, questions, intent="excluded originals", grouping=manifest)
            restored = self._saved_rows(rows)
            flags = jv.flag(restored, lambda row: False)
            report = jv.report(restored, questions)
        self.assertEqual(manifest["original_ids"], list(units))
        self.assertEqual(list(manifest["excluded"]), expected_excluded)
        self.assertTrue(all(reason.strip() for reason in manifest["excluded"].values()))
        self.assertEqual(manifest["slots"], {"review#1": ["keep:first", "keep:second"],
                                             "review#2": ["keep:third"]})
        self.assertEqual([(row["id"], row["group"], row["slot"], row["verdict"]) for row in rows],
                         [("keep:first", "review#1", "s0", "KEEP"),
                          ("keep:second", "review#1", "s1", "PURGE"),
                          ("keep:third", "review#2", "s0", "KEEP")])
        receipt = self._assert_receipt(output, {
            "schema": 2, "original_ids": list(units), "excluded_ids": expected_excluded,
            "exclusions": manifest["excluded"], "complete": False,
            "unit_ids": ["keep:first", "keep:second", "keep:third"],
            "answered_ids": ["keep:first", "keep:second", "keep:third"], "error_ids": [],
        })
        self.assertEqual(rows.receipt, receipt)
        self.assertEqual(restored.receipt, receipt)
        self.assertEqual([(row["id"], row["why"]) for row in flags],
                         [(row["id"], ["coverage_incomplete"]) for row in rows])
        self.assertTrue(all("error" not in row for row in restored))
        self.assertEqual(self._report_count(report, "errors"), 0)
        for identifier in expected_excluded:
            self.assertIn(identifier, report)
            self.assertIn(manifest["excluded"][identifier], report)
        self.assertEqual(len(calls), 1)

    async def test_all_excluded_grouping_retains_empty_incomplete_receipt_without_host_calls(self):
        units = {"empty:first": "", "empty:second": "  "}
        questions = {"safe": jv.yes("Is it safe?")}
        host = mock.Mock(side_effect=AssertionError("excluded units must not reach the host"))
        output = io.StringIO()
        with mock.patch.dict(namespace, {"judge_batch": host}), contextlib.redirect_stdout(output):
            grouped, manifest = jv.group(units, by=lambda text: "empty")
            rows = await jv.run(grouped, questions, intent="all excluded", grouping=manifest)
            restored = self._saved_rows(rows)
            for result in (rows, restored):
                with self.assertRaisesRegex(ValueError, "(?i)incomplete|coverage|excluded"):
                    jv.flag(result, lambda row: False)
        host.assert_not_called()
        self.assertEqual(grouped, {})
        self.assertEqual(rows, [])
        receipt = self._assert_receipt(output, {
            "original_ids": list(units), "excluded_ids": list(units),
            "exclusions": manifest["excluded"], "complete": False,
            "unit_ids": [], "answered_ids": [], "error_ids": [], "batch_ids": [],
        })
        self.assertEqual(rows.receipt, receipt)
        self.assertEqual(restored.receipt, receipt)

    async def test_valid_mixed_rubric_preserves_answers_and_metadata_flat_and_grouped(self):
        units = {
            "review:1": {"file": "review", "text": "first"},
            "review:2": {"file": "review", "text": "second"},
        }
        questions = {
            "risk": jv.yes("Does it need attention?"),
            "verdict": jv.choice("Assess each unit", KEEP="useful", PURGE="weak"),
            "severity": jv.scale("How severe is the issue?", "low", "medium", "high"),
        }
        expected = {
            "review:1": {"risk": 0.17, "verdict": "KEEP", "verdict.KEEP": 0.76,
                         "verdict.PURGE": 0.24, "verdict.conf": 0.81,
                         "severity": 0.375, "severity.conf": 0.64},
            "review:2": {"risk": 0.83, "verdict": "PURGE", "verdict.KEEP": 0.13,
                         "verdict.PURGE": 0.87, "verdict.conf": 0.92,
                         "severity": 1.625, "severity.conf": 0.88},
        }
        by_text = {unit["text"]: expected[identifier] for identifier, unit in units.items()}

        for grouped in (False, True):
            with self.subTest(grouped=grouped):
                states, manifest = units, None
                if grouped:
                    with contextlib.redirect_stdout(io.StringIO()):
                        states, manifest = jv.group(units, by=lambda unit: unit["file"], k=2,
                                                 render=lambda unit: unit["text"])
                batches = []

                def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
                    items = {}
                    for identifier, state in payload.items():
                        answers = {}
                        occupied = state["units"] if grouped else {None: state["text"]}
                        for slot, text in occupied.items():
                            values = by_text[text]
                            suffix = f"@{slot}" if grouped else ""
                            verdict = _choice(values["verdict"])
                            verdict.update(confidence=values["verdict.conf"], probabilities={
                                "KEEP": values["verdict.KEEP"], "PURGE": values["verdict.PURGE"],
                            })
                            answers[f"risk{suffix}"] = {"type": "bool", "bool": values["risk"]}
                            answers[f"verdict{suffix}"] = verdict
                            answers[f"severity{suffix}"] = {
                                "type": "score", "score": values["severity"],
                                "confidence": values["severity.conf"],
                            }
                        items[identifier] = _item(answers, questions=len(submitted_questions))
                    batch = _FakeBatch(payload, items)
                    batches.append(batch)
                    return batch

                with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
                    rows = await jv.run(states, questions, intent="valid mixed rubric", grouping=manifest)
                    restored = self._saved_rows(rows)
                    flags = jv.flag(restored, lambda row: row["verdict"] == "PURGE"
                                    or row["severity"] >= 1)
                self.assertEqual([row["id"] for row in restored], list(units))
                for index, row in enumerate(restored):
                    self.assertEqual({key: row[key] for key in expected[row["id"]]},
                                     expected[row["id"]])
                    metadata = {
                        "qhash": jv.qhash(questions), "batch_id": batches[0].id,
                        "model": "jev-primary", "selected_model": "jev-primary",
                        "primary_model": "jev-primary", "response_model": "jev-primary",
                        "cache": "miss", "questions": 6 if grouped else 3,
                        "cached_questions": 0,
                    }
                    self.assertEqual({key: row[key] for key in metadata}, metadata)
                    self.assertIs(row["fallback"], False)
                    self.assertIs(row["provenance_unknown"], False)
                    self.assertNotIn("error", row)
                    if grouped:
                        self.assertEqual((row["group"], row["slot"]), ("review#1", f"s{index}"))
                    else:
                        self.assertNotIn("group", row)
                        self.assertNotIn("slot", row)
                self.assertEqual([(row["id"], row["why"]) for row in flags],
                                 [("review:2", ["rule"])])

    async def test_grouped_run_routes_each_slot_and_discards_absent_slots(self):
        units = {
            "a.py:1": {"file": "a.py", "text": "first"},
            "a.py:2": {"file": "a.py", "text": "second"},
            "a.py:3": {"file": "a.py", "text": "third"},
            "b.py:1": {"file": "b.py", "text": "fourth"},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, manifest = jv.group(units, by=lambda unit: unit["file"], k=2,
                                      render=lambda unit: unit["text"])
        self.assertEqual(manifest["slots"], {
            "a.py#1": ["a.py:1", "a.py:2"],
            "a.py#2": ["a.py:3"],
            "b.py#1": ["b.py:1"],
        })
        questions = {"verdict": jv.choice("Assess each unit", KEEP="useful", PURGE="weak"),
                     "risk": jv.yes("Does it need attention?")}
        verdicts = {"first": "KEEP", "second": "PURGE", "third": "KEEP", "fourth": "PURGE"}
        risks = {"first": 0.1, "second": 0.8, "third": 0.3, "fourth": 0.7}
        calls, batches = [], []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            calls.append((dict(payload), dict(submitted_questions), concurrency, retries, intent))
            items = {}
            for identifier, state in payload.items():
                answers = {}
                for slot, text in state["units"].items():
                    answers[f"verdict@{slot}"] = _choice(verdicts[text])
                    answers[f"risk@{slot}"] = {"type": "bool", "bool": risks[text]}
                # Single-unit groups deliberately omit all s1 answers, despite the
                # batch-wide question schema still including @s1.
                items[identifier] = _item(answers, questions=len(submitted_questions))
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        output = io.StringIO()
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(output):
            rows = await jv.run(grouped, questions, intent="grouped contract", grouping=manifest)
            restored = self._saved_rows(rows)
            flags = jv.flag(restored, lambda row: row["verdict"] == "PURGE")
            report = jv.report(restored, questions)
        self.assertEqual(len(calls), 1)  # an absent s1 is not a failed group to retry
        self.assertEqual(set(calls[0][1]), {"verdict@s0", "verdict@s1", "risk@s0", "risk@s1"})
        self.assertEqual([row["id"] for row in rows], list(units))
        for row in rows:
            source = units[row["id"]]["text"]
            self.assertEqual(row["verdict"], verdicts[source])
            self.assertEqual(row["risk"], risks[source])
            self.assertEqual(row["group"], row["id"].split(":")[0]
                             + ("#2" if row["id"] == "a.py:3" else "#1"))
            self.assertEqual(row["qhash"], jv.qhash(questions))
            self.assertNotIn("error", row)
        self.assertEqual(rows[1]["verdict.PURGE"], 0.9)
        self.assertTrue(all(batch.closed and not batch.cancelled for batch in batches))
        self.assertEqual([(row["id"], row["why"]) for row in flags],
                         [("a.py:2", ["rule"]), ("b.py:1", ["rule"])])
        self.assertEqual(self._report_count(report, "fallback rows"), 0)
        self.assertEqual(self._report_count(report, "errors"), 0)
        receipt = self._assert_receipt(output, {
            "schema": 2,
            "strategy": "jev-grouped",
            "batch_ids": ["fake-batch-1"],
            "unit_ids": list(units),
            "answered_ids": list(units),
            "error_ids": [],
            "question_hash": jv.qhash(questions),
            "group_sizes": [2, 1, 1],
            "group_slots": manifest["slots"],
            "coverage_errors": [],
            "complete": True,
        })
        # Three host groups each use the four-question schema; expansion to
        # four original rows must not duplicate cache/question accounting.
        self.assertEqual(receipt["cache_counts"], {"hit": 0, "partial": 0, "miss": 3, "unknown": 0})
        self.assertEqual(receipt["questions"], 12)
        self.assertEqual(receipt["cached_questions"], 0)
        self.assertEqual(receipt["question_counts_unknown"], 0)
        self.assertEqual(receipt["batches"][0]["planned_ids"], list(grouped))
        self.assertEqual(receipt["batches"][0]["answered_ids"], list(grouped))
        self.assertEqual(receipt["batches"][0]["items"], [{
            "id": gid, "batch_id": "fake-batch-1", "selected_model": "jev-primary",
            "primary_model": "jev-primary", "response_model": "jev-primary",
            "fallback": False, "cache": "miss", "questions": 4, "cached_questions": 0,
        } for gid in grouped])
        self.assertEqual([(row["id"], row["slot"], row["batch_id"]) for row in restored],
                         [("a.py:1", "s0", "fake-batch-1"),
                          ("a.py:2", "s1", "fake-batch-1"),
                          ("a.py:3", "s0", "fake-batch-1"),
                          ("b.py:1", "s0", "fake-batch-1")])

    async def test_grouped_run_retries_failed_group_and_flags_each_failure_row(self):
        units = {"x:1": {"file": "x", "text": "good"},
                 "y:1": {"file": "y", "text": "bad one"},
                 "y:2": {"file": "y", "text": "bad two"}}
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, manifest = jv.group(units, by=lambda unit: unit["file"], k=2,
                                      render=lambda unit: unit["text"])
        questions = {"verdict": jv.choice("Assess", KEEP="useful", PURGE="weak")}
        calls, batches = [], []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            calls.append((list(payload), intent))
            items = {}
            for identifier, state in payload.items():
                if identifier == "y#1":
                    items[identifier] = _item(None, error="synthetic failure",
                                              response_model=None, questions=len(submitted_questions))
                else:
                    items[identifier] = _item({"verdict@s0": _choice("KEEP")},
                                              questions=len(submitted_questions))
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        output = io.StringIO()
        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(output):
            rows = await jv.run(grouped, questions, intent="failed group", grouping=manifest)
            restored = self._saved_rows(rows)
            flags = jv.flag(restored, lambda row: row["verdict"] == "PURGE")
            report = jv.report(restored, questions)
        self.assertEqual(calls, [(["x#1", "y#1"], "failed group"),
                                 (["y#1"], "failed group (retry)")])
        self.assertEqual([row["id"] for row in rows], ["x:1", "y:1", "y:2"])
        self.assertEqual(rows[0]["verdict"], "KEEP")
        for row in rows[1:]:
            self.assertEqual(row["group"], "y#1")
            self.assertEqual(row["error"], "synthetic failure")
            self.assertNotIn("verdict", row)
        self.assertEqual([row["id"] for row in flags], ["x:1", "y:1", "y:2"])
        self.assertEqual(flags[0]["why"], ["coverage_incomplete"])
        self.assertTrue(all(row["why"] == ["error"] for row in flags[1:]))
        self.assertTrue(all(batch.closed for batch in batches))
        self.assertEqual(self._report_count(report, "errors"), 2)
        self.assertEqual(self._report_count(report, "fallback rows"), 0)
        receipt = self._assert_receipt(output, {
            "schema": 2,
            "strategy": "jev-grouped",
            "batch_ids": ["fake-batch-1", "fake-batch-2"],
            "unit_ids": ["x:1", "y:1", "y:2"],
            "answered_ids": ["x:1"],
            "error_ids": ["y:1", "y:2"],
            "question_hash": jv.qhash(questions),
            "group_sizes": [1, 2],
            "group_slots": {"x#1": ["x:1"], "y#1": ["y:1", "y:2"]},
            "coverage_errors": [],
            "complete": False,
        })
        self.assertEqual([(batch["planned_ids"], batch["answered_ids"], batch["error_ids"])
                          for batch in receipt["batches"]],
                         [(["x#1", "y#1"], ["x#1"], ["y#1"]),
                          (["y#1"], [], ["y#1"])])
        self.assertEqual([batch["items"] for batch in receipt["batches"]], [[{
            "id": gid, "batch_id": f"fake-batch-{attempt}",
            "selected_model": "jev-primary", "primary_model": "jev-primary",
            "response_model": "jev-primary" if gid == "x#1" else None,
            "fallback": False, "cache": "miss", "questions": 2, "cached_questions": 0,
        } for gid in ids] for attempt, ids in enumerate((["x#1", "y#1"], ["y#1"]), 1)])
        self.assertEqual([(row["id"], row["batch_id"]) for row in restored],
                         [("x:1", "fake-batch-1"), ("y:1", "fake-batch-2"),
                          ("y:2", "fake-batch-2")])

    async def test_actual_fallback_route_is_flagged_despite_batch_model(self):
        states = {"ordinary": "short input", "rerouted": "other input"}
        questions = {"safe": jv.yes("Is it safe?")}
        batches = []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            items = {
                identifier: _item(
                    {"safe": {"type": "bool", "bool": 0.1}},
                    model="jev-secondary" if identifier == "rerouted" else "jev-primary",
                    selected_model="jev-secondary" if identifier == "rerouted" else "jev-primary",
                    response_model="jev-secondary" if identifier == "rerouted" else "jev-primary",
                    fallback=identifier == "rerouted",
                )
                for identifier in payload
            }
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}):
            with contextlib.redirect_stdout(io.StringIO()):
                rows = await jv.run(states, questions, intent="non-primary contract")
                flags = jv.flag(rows, lambda row: False)
        self.assertEqual({row["id"]: row["model"] for row in rows},
                         {"ordinary": "jev-primary", "rerouted": "jev-secondary"})
        self.assertEqual([row["id"] for row in flags], ["rerouted"])
        self.assertEqual(flags[0]["why"], ["fallback:jev-secondary"])
        self.assertTrue(all("error" not in row for row in rows))
        self.assertTrue(all(batch.closed for batch in batches))

    async def test_oversize_item_is_not_retried_while_other_failures_are(self):
        states = {"small": "fine", "huge": "x" * 50, "flaky": "transient"}
        questions = {"safe": jv.yes("Is it safe?")}
        oversize = ('judgment: every judge candidate failed: typesafe/jev-latest API error (400): '
                    '{"detail":{"error_type":"max_tokens_exceeded"}}')
        calls = []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            calls.append((list(payload), intent))
            retry = intent.endswith("(retry)")
            items = {}
            for identifier in payload:
                if identifier == "huge":
                    items[identifier] = _item(None, model=None, response_model=None, error=oversize)
                elif identifier == "flaky" and not retry:
                    items[identifier] = _item(None, model=None, response_model=None,
                                              error="judge timed out")
                else:
                    items[identifier] = _item({"safe": {"type": "bool", "bool": 0.2}})
            return _FakeBatch(payload, items)

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}):
            with contextlib.redirect_stdout(io.StringIO()):
                rows = await jv.run(states, questions, intent="oversize contract")
                flags = jv.flag(rows)
        self.assertEqual(calls, [(["small", "huge", "flaky"], "oversize contract"),
                                 (["flaky"], "oversize contract (retry)")])
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["small"]["safe"], 0.2)
        self.assertEqual(by_id["flaky"]["safe"], 0.2)
        self.assertTrue(by_id["huge"]["error"].startswith("oversize: "))
        self.assertIn("max_tokens_exceeded", by_id["huge"]["error"])
        self.assertNotIn("safe", by_id["huge"])
        self.assertEqual([row["id"] for row in flags], list(states))
        self.assertEqual({row["id"]: row["why"] for row in flags if row["id"] != "huge"},
                         {"small": ["coverage_incomplete"], "flaky": ["coverage_incomplete"]})
        self.assertIn("error", next(row for row in flags if row["id"] == "huge")["why"])

    async def test_grouped_oversize_rows_inherit_oversize_error_without_retry(self):
        units = {"big:1": "a", "big:2": "b"}
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, manifest = jv.group(units, by=lambda text: "big", k=2)
        calls = []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            calls.append(intent)
            return _FakeBatch(payload, {
                identifier: _item(None, model=None, response_model=None,
                                  questions=len(submitted_questions),
                                  error="API error (400): max_tokens_exceeded")
                for identifier in payload
            })

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}):
            with contextlib.redirect_stdout(io.StringIO()):
                rows = await jv.run(grouped, {"safe": jv.yes("Is it safe?")},
                                    intent="grouped oversize", grouping=manifest)
        self.assertEqual(calls, ["grouped oversize"])
        self.assertEqual([row["id"] for row in rows], ["big:1", "big:2"])
        self.assertTrue(all(row["error"].startswith("oversize: ") and row["group"] == "big#1"
                            for row in rows))


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    PAIRING_ERROR = (
        r"(?i)(?=.*calibrat)(?:(?=.*missing)(?=.*state)|"
        r"(?=.*invalid)(?=.*(?:state|slot)))"
    )

    def setUp(self):
        previous_stats, previous_frozen = jv.stats, jv.frozen
        self.addCleanup(setattr, jv, "stats", previous_stats)
        self.addCleanup(setattr, jv, "frozen", previous_frozen)
        jv.stats = {"states": {}, "runs": []}
        jv.frozen = None

    async def _grouped_run(self, *, helper_id="helper"):
        units = {
            helper_id: {"symbol": "helper", "uses": [],
                       "text": "def helper(): return 1\n" + "# helper details\n" * 100},
            "consumer": {"symbol": "consumer", "uses": ["helper"],
                         "text": "def consumer(): return helper()\n" + "# consumer details\n" * 100},
        }
        shared = {"policy": "Permission: purge unconsumed symbols. "
                  + "Check sibling references before removal. " * 100}
        questions = {"verdict": jv.choice(
            "Use the shared policy and sibling references to assess the target symbol.",
            KEEP="Keep consumed symbols or symbols without removal permission.",
            PURGE="Removal is permitted and no sibling uses this symbol.",
        )}
        decisions = {"helper": "KEEP", "consumer": "PURGE"}

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            return _FakeBatch(payload, {
                gid: _item({f"verdict@{slot}": _choice(decisions[unit["symbol"]])
                            for slot, unit in state["units"].items()},
                           questions=len(submitted_questions))
                for gid, state in payload.items()
            })

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}), contextlib.redirect_stdout(io.StringIO()):
            states, grouping = jv.group(units, by=lambda unit: "review", k=2,
                                        shared=lambda group: shared, cap=100)
            rows = await jv.run(states, questions, intent="grouped calibration regression",
                                grouping=grouping)
        return units, shared, states, rows, questions

    async def test_grouped_calibration_preserves_bounded_shared_and_sibling_context(self):
        units, shared, states, rows, questions = await self._grouped_run()
        observed = {}

        def completion(prompt, *, model, system, schema):
            state = json.loads(prompt)
            match = re.search(r"\[['\"](s\d+)['\"]\]", system)
            if match is None:
                raise ValueError("No slot target in calibration instructions")
            slot = match.group(1)
            target = state["units"][slot]
            observed.setdefault(target["symbol"], []).append(state)
            permitted = "Permission: purge unconsumed symbols." in state["shared"]["policy"]
            consumed = any(target["symbol"] in sibling["uses"]
                           for key, sibling in state["units"].items() if key != slot)
            verdict = "PURGE" if permitted and not consumed else "KEEP"
            return {"verdict": verdict,
                    "reason": f"{target['symbol']}: permitted={permitted}, consumed={consumed}"}

        slow = mock.Mock(side_effect=completion)
        wait = mock.Mock(side_effect=lambda handles, *, raise_errors=False: handles)
        with mock.patch.dict(namespace, {"completion": slow, "wait": wait}), contextlib.redirect_stdout(io.StringIO()):
            result = jv.calibrate(rows, states, questions, qid="verdict", label="PURGE",
                                  edges=(0, 0.5, 1.01), per_bin=10)

        self.assertEqual({uid: verdict["slow"] for uid, verdict in result["verdicts"].items()},
                         {"helper": "KEEP", "consumer": "PURGE"})
        self.assertEqual(len(result["bins"]), 2)
        for bucket, expected_agreement in zip(result["bins"], (0.0, 1.0)):
            self.assertEqual(bucket["n"], 1)
            self.assertEqual(bucket["calibrated"], 1)
            self.assertEqual(bucket["errors"], 0)
            self.assertEqual(bucket["agreed"], int(expected_agreement))
            self.assertEqual(bucket["agreement"], expected_agreement)
        self.assertEqual(slow.call_count, 2)
        self.assertEqual(set(observed), {"helper", "consumer"})

        raw_state = {"group": "review", "shared": shared,
                     "units": {f"s{index}": unit for index, unit in enumerate(units.values())}}
        for row in rows:
            with self.subTest(target=row["id"]):
                bounded = states[row["group"]]
                self.assertEqual(observed[row["id"]], [bounded])
                self.assertNotEqual(observed[row["id"]][0], raw_state)
                self.assertNotEqual(bounded["shared"]["policy"], shared["policy"])
                self.assertGreater(len(shared["policy"]), 1000)
                self.assertLessEqual(len(bounded["shared"]["policy"]), 100)
                siblings = {unit["symbol"]: unit for unit in bounded["units"].values()}
                self.assertEqual(set(siblings), {"helper", "consumer"})
                for symbol, original in units.items():
                    self.assertEqual(siblings[symbol]["uses"], original["uses"])
                    self.assertNotEqual(siblings[symbol]["text"], original["text"])
                    self.assertGreater(len(original["text"]), 1000)
                    self.assertLessEqual(len(siblings[symbol]["text"]), 100)

    async def test_invalid_grouped_pairings_fail_before_sampling_or_slow_calls(self):
        units, shared, states, rows, questions = await self._grouped_run()
        collision_units, _, _, collision_rows, _ = await self._grouped_run(helper_id="review#1")
        first, second = rows
        flat = {"id": "valid-flat", "verdict.PURGE": 0.9}
        missing_slot = {key: value for key, value in second.items() if key != "slot"}
        missing_group = {key: value for key, value in second.items() if key != "group"}
        cases = [
            ("original-ungrouped-states", [flat, second],
             {**units, flat["id"]: {"symbol": "valid-flat"}}),
            ("original-ungrouped-group-id-collision", [flat, collision_rows[1]],
             {**collision_units, flat["id"]: {"symbol": "valid-flat"}}),
            ("missing-slot", [first, missing_slot], states),
            ("nonexistent-slot", [first, {**second, "slot": "s99"}], states),
            ("non-string-slot", [first, {**second, "slot": 1}], states),
            ("missing-group-metadata", [first, missing_group],
             {**states, second["id"]: states[second["group"]]}),
        ]
        bounded_units = states[second["group"]]["units"]
        malformed_states = [
            ("non-object-group", []),
            ("null-group", None),
            ("missing-units", {"shared": shared}),
            ("null-units", {"units": None}),
            ("list-units", {"units": list(bounded_units.values())}),
            ("string-units", {"units": "not a slot mapping"}),
            ("empty-units", {"units": {}}),
            ("noncontiguous-units", {"units": {second["slot"]: bounded_units[second["slot"]]}}),
        ]
        for name, malformed in malformed_states:
            cases.append((name, [first, {**second, "group": "malformed-group"}],
                          {**states, "malformed-group": malformed}))

        for name, paired_rows, paired_states in cases:
            for per_bin in (10, 0):
                with self.subTest(case=name, per_bin=per_bin):
                    completion = mock.Mock(side_effect=AssertionError("invalid pairing reached completion"))
                    wait = mock.Mock(side_effect=AssertionError("invalid pairing reached wait"))
                    with mock.patch.dict(namespace, {"completion": completion, "wait": wait}):
                        with self.assertRaisesRegex(ValueError, self.PAIRING_ERROR):
                            jv.calibrate(paired_rows, paired_states, questions,
                                         qid="verdict", label="PURGE", per_bin=per_bin)
                    completion.assert_not_called()
                    wait.assert_not_called()

    def test_missing_flat_state_fails_before_sampling_or_slow_calls(self):
        rows = [{"id": "present", "verdict.PURGE": 0.1},
                {"id": "missing", "verdict.PURGE": 0.9}]
        states = {"present": {"symbol": "present"}}
        questions = {"verdict": jv.choice("Assess the symbol", KEEP="consumed", PURGE="unused")}
        for per_bin in (10, 0):
            with self.subTest(per_bin=per_bin):
                completion = mock.Mock(side_effect=AssertionError("missing state reached completion"))
                wait = mock.Mock(side_effect=AssertionError("missing state reached wait"))
                with mock.patch.dict(namespace, {"completion": completion, "wait": wait}):
                    with self.assertRaisesRegex(ValueError, self.PAIRING_ERROR):
                        jv.calibrate(rows, states, questions, qid="verdict", label="PURGE",
                                     per_bin=per_bin)
                completion.assert_not_called()
                wait.assert_not_called()


class JevSizingTests(unittest.TestCase):
    def setUp(self):
        previous = jv.stats
        self.addCleanup(setattr, jv, "stats", previous)
        jv.stats = {"states": {}, "runs": []}

    def test_states_record_oversize_and_unmeasured_ids(self):
        units = {"tiny": "short", "huge": "h" * 40_000, "fits": "f" * 40_000,
                 "odd": "o" * 40_000, "wide": "\u4e00" * 11_000}
        tokens = {"h": 40_000, "f": 20_000, "o": None, "\u4e00": 33_000}
        measured = []

        def fake(text, *, timeout=20):
            measured.append(text[0])
            return tokens[text[0]]

        with mock.patch.object(jv, "jev_tokens", fake), contextlib.redirect_stdout(io.StringIO()):
            built = jv.states(units, cap=50_000)
        self.assertEqual(list(built), list(units))
        # 11k CJK chars are 33k UTF-8 bytes, so they are measured despite the low char count.
        self.assertEqual(sorted(measured), sorted(["h", "f", "o", "\u4e00"]))
        self.assertEqual(jv.stats["states"]["oversize"], ["huge", "wide"])
        self.assertEqual(jv.stats["states"]["unmeasured"], ["odd"])

    def _fake_omp(self, body):
        root = _temporary_directory(self)
        script = root / "omp"
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return str(root)

    def test_jev_tokens_picks_jev_encoding_and_returns_none_without_usable_omp(self):
        report = ('{"encodings":[{"encoding":"o200k","tokens":7},'
                  '{"encoding":"Jev","name":"jev","tokens":42}]}')
        # PATH holds only the fake, so the scripts use builtins and absolute paths.
        working = self._fake_omp(f"printf '%s\\n' '{report}'\n")
        slow = self._fake_omp("exec /bin/sleep 5\n")
        garbled = self._fake_omp("echo not json\n")
        empty = str(_temporary_directory(self))
        with mock.patch.dict(os.environ, {"PATH": working}):
            self.assertEqual(jv.jev_tokens("hello"), 42)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            self.assertIsNone(jv.jev_tokens("hello"))
        with mock.patch.dict(os.environ, {"PATH": slow}):
            self.assertIsNone(jv.jev_tokens("hello", timeout=0.3))
        with mock.patch.dict(os.environ, {"PATH": garbled}):
            self.assertIsNone(jv.jev_tokens("hello"))


class AstUnitsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("uv"), "uv is needed to bootstrap ast-grep-py")
    def test_ast_units_recipe_f_includes_decorators_and_skips_helpers(self):
        root = _temporary_directory(self)
        folder = root / "tests"
        folder.mkdir()
        (folder / "test_sample.py").write_text(
            '@pytest.mark.slow\n'
            '@pytest.mark.parametrize("value", [1])\n'
            'def test_decorated(value):\n'
            '    assert value == 1\n'
            '\n'
            'def test_plain():\n'
            '    assert 2 == 2\n'
            '\n'
            'def helper():\n'
            '    return 3\n', encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            units = jv.ast_units(lang="python", rule=TEST_RULE, cwd=root,
                                 globs=["tests/**/*.py"])
        self.assertEqual(set(units), {"tests/test_sample.py:1", "tests/test_sample.py:6"})
        decorated = units["tests/test_sample.py:1"]
        self.assertEqual((decorated["line"], decorated["end_line"]), (1, 4))
        self.assertTrue(decorated["text"].startswith("@pytest.mark.slow\n"))
        self.assertIn('@pytest.mark.parametrize("value", [1])', decorated["text"])
        self.assertTrue(decorated["text"].endswith("assert value == 1"))
        plain = units["tests/test_sample.py:6"]
        self.assertEqual((plain["line"], plain["end_line"]), (6, 7))
        self.assertTrue(plain["text"].startswith("def test_plain():"))


if __name__ == "__main__":
    unittest.main()
