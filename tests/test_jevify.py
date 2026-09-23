"""Behavioral contracts for the jevify extraction, batching, and deletion helpers."""

import contextlib
import copy
import io
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
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            dry = jv.delete_spans(units, ["first", "overlap", "adjacent", "later"], root=root)
        self.assertEqual(dry, {"sample.py": 7})
        self.assertIn("(dry run)", output.getvalue())
        self.assertEqual(path.read_bytes(), original)

        with contextlib.redirect_stdout(io.StringIO()):
            applied = jv.delete_spans(units, ["first", "overlap", "adjacent", "later"],
                                      root=root, dry_run=False)
        self.assertEqual(applied, dry)
        self.assertEqual(path.read_bytes(), b"before\t\r\nmiddle\t\r\nafter \xc3\xa9\nfinal-no-eol")

    def test_delete_spans_unknown_ids_warn_without_mutation(self):
        root = _temporary_directory(self)
        path = root / "sample.py"
        original = b"keep\r\n"
        path.write_bytes(original)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = jv.delete_spans({}, ["unknown:9"], root=root, dry_run=False)
        self.assertEqual(result, {})
        self.assertIn("warning: 1 ids missing from units: ['unknown:9']", output.getvalue())
        self.assertEqual(path.read_bytes(), original)


class _FakeBatch:
    """Completed judge_batch handle with the documented drain and result interface."""

    def __init__(self, payload, items):
        self.id = "fake-batch"
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


def _item(answers, *, model="jev-primary", error=None):
    return SimpleNamespace(ok=error is None, answers=answers, error=error, model=model)


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

    async def test_grouped_run_routes_each_slot_and_discards_absent_slots(self):
        units = {
            "a.py:1": {"file": "a.py", "text": "first"},
            "a.py:2": {"file": "a.py", "text": "second"},
            "a.py:3": {"file": "a.py", "text": "third"},
            "b.py:1": {"file": "b.py", "text": "fourth"},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, slots = jv.group(units, by=lambda unit: unit["file"], k=2,
                                      render=lambda unit: unit["text"])
        self.assertEqual(slots, {
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
                items[identifier] = _item(answers)
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}):
            with contextlib.redirect_stdout(io.StringIO()):
                rows = await jv.run(grouped, questions, intent="grouped contract", slots=slots)
        self.assertEqual(len(calls), 1)  # an absent s1 is not a failed group to retry
        self.assertEqual(set(calls[0][1]), {"verdict@s0", "verdict@s1", "risk@s0", "risk@s1"})
        self.assertEqual((calls[0][2], calls[0][3], calls[0][4]),
                         (32, 1, "grouped contract"))
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

    async def test_grouped_run_retries_failed_group_and_flags_each_failure_row(self):
        units = {"x:1": {"file": "x", "text": "good"},
                 "y:1": {"file": "y", "text": "bad one"},
                 "y:2": {"file": "y", "text": "bad two"}}
        with contextlib.redirect_stdout(io.StringIO()):
            grouped, slots = jv.group(units, by=lambda unit: unit["file"], k=2,
                                      render=lambda unit: unit["text"])
        questions = {"verdict": jv.choice("Assess", KEEP="useful", PURGE="weak")}
        calls, batches = [], []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            calls.append((list(payload), intent))
            items = {}
            for identifier, state in payload.items():
                if identifier == "y#1":
                    items[identifier] = _item(None, error="synthetic failure")
                else:
                    items[identifier] = _item({"verdict@s0": _choice("KEEP")})
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}):
            with contextlib.redirect_stdout(io.StringIO()):
                rows = await jv.run(grouped, questions, intent="failed group", slots=slots)
                flags = jv.flag(rows, lambda row: row["verdict"] == "PURGE",
                                primary="jev-primary")
        self.assertEqual(calls, [(["x#1", "y#1"], "failed group"),
                                 (["y#1"], "failed group (retry)")])
        self.assertEqual([row["id"] for row in rows], ["x:1", "y:1", "y:2"])
        self.assertEqual(rows[0]["verdict"], "KEEP")
        for row in rows[1:]:
            self.assertEqual(row["group"], "y#1")
            self.assertEqual(row["error"], "synthetic failure")
            self.assertNotIn("verdict", row)
        self.assertEqual([row["id"] for row in flags], ["y:1", "y:2"])
        self.assertTrue(all(row["why"] == ["error"] for row in flags))
        self.assertTrue(all(batch.closed for batch in batches))

    async def test_fallback_item_model_is_flagged_despite_batch_model(self):
        states = {"ordinary": "short input", "large": "oversized input"}
        questions = {"safe": jv.yes("Is it safe?")}
        batches = []

        def judge_batch(payload, submitted_questions, *, concurrency, retries, intent):
            items = {identifier: _item({"safe": {"type": "bool", "bool": 0.1}},
                                       model="session-default" if identifier == "large" else "jev-primary")
                     for identifier in payload}
            batch = _FakeBatch(payload, items)
            batches.append(batch)
            return batch

        with mock.patch.dict(namespace, {"judge_batch": judge_batch}):
            with contextlib.redirect_stdout(io.StringIO()):
                rows = await jv.run(states, questions, intent="fallback contract")
                flags = jv.flag(rows, lambda row: False, primary="jev-primary")
        self.assertEqual({row["id"]: row["model"] for row in rows},
                         {"ordinary": "jev-primary", "large": "session-default"})
        self.assertEqual([row["id"] for row in flags], ["large"])
        self.assertEqual(flags[0]["why"], ["fallback:session-default"])
        self.assertTrue(all("error" not in row for row in rows))
        self.assertTrue(all(batch.closed for batch in batches))


class QuestionHashTests(unittest.TestCase):
    def test_qhash_ignores_key_order_but_detects_criterion_change(self):
        original = {
            "verdict": {"type": "choice", "instructions": "Assess",
                        "criteria": {"KEEP": "consumer contract", "PURGE": "echo"}},
            "risk": {"type": "bool", "instructions": "Risk?",
                     "criteria": {"true": "harm", "false": "none"}},
        }
        reordered = {
            "risk": {"criteria": {"false": "none", "true": "harm"},
                     "instructions": "Risk?", "type": "bool"},
            "verdict": {"criteria": {"PURGE": "echo", "KEEP": "consumer contract"},
                        "instructions": "Assess", "type": "choice"},
        }
        self.assertEqual(jv.qhash(original), jv.qhash(reordered))
        changed = copy.deepcopy(reordered)
        changed["verdict"]["criteria"]["PURGE"] = "unconsumed echo"
        self.assertNotEqual(jv.qhash(original), jv.qhash(changed))


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
