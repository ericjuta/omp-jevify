"""Behavioral contracts for the eval fixture and answer keys (evals/fixture.py), answer scoring
(evals/grade.py) and run summaries (evals/run.py)."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"jevify_eval_{name}", ROOT / "evals" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = _load("fixture")
grade = _load("grade")
run = _load("run")
# Hermetic git: no user or system configuration and no inherited GIT_* variables.
CLEAN_GIT = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
CLEAN_GIT.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _snapshot(environment, scale=None):
    """What the tests read from a fixture built and read under the given environment (scale None:
    build()'s default): answer keys, Python sources as lines, log lines, review findings, and the
    trees of HEAD and HEAD~1."""
    with mock.patch.dict(os.environ, environment, clear=True), tempfile.TemporaryDirectory() as tmp:
        root = fixture.build(Path(tmp) / "repo") if scale is None else fixture.build(Path(tmp) / "repo", scale=scale)
        trees = subprocess.run(["git", "rev-parse", "HEAD^{tree}", "HEAD~1^{tree}"], cwd=root, check=True,
                               capture_output=True, text=True).stdout.split()
        return {
            "keys": fixture.answer_keys(root), "trees": trees,
            "sources": {str(path.relative_to(root)): path.read_text().splitlines() for path in root.rglob("*.py")},
            "log": (root / "logs" / "worker.log").read_text().splitlines(),
            "findings": [json.loads(line) for line in (root / "review" / "findings.jsonl").read_text().splitlines()],
        }


def _flagged(key):
    return {item["id"] for item in key["items"] if item["flag"] is True}


def _as_fixed(line):
    """A removed line rewritten as the commit message describes: truncation becomes round_cents in
    the same order of operations, and truncation wording becomes half-even wording."""
    line = re.sub(r"int\((\w+) \* rate \* 100\) / 100", r"round_cents(\1 * rate)", line)
    line = re.sub(r"int\((\w+) \* 100\) / 100 \* rate", r"round_cents(\1) * rate", line)
    for old, new in [*(("# " + old, "# " + new) for old, new in fixture.COMMENT_PAIRS), *fixture.DOC_PAIRS]:
        match = re.fullmatch(re.escape(old).replace(r"\{name\}", r"(\w+)"), line)
        if match:
            return new.replace("{name}", match[1]) if match.groups() else new
    return line


def _verdict_from_code(kind, module, stem, node):
    """What reading one line-item function's code (node, in the parsed file module) says of a review
    claim: a factual claim is valid exactly when true of that code, a nit never is, and the
    missing-test claim is debatable."""
    if kind in ("hints", "rename"):
        return False
    if kind == "test":
        return None

    def calls(tree, name):
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]

    def uses_rate(tree):
        return any(isinstance(n, ast.Name) and n.id == "rate" for n in ast.walk(tree))

    fetch = calls(node, "fetch_rate")[0]
    money = max((n for n in ast.walk(node) if isinstance(n, ast.Return)), key=lambda n: n.lineno).value
    rounded = calls(money, "round_cents")[0]
    return {
        "legacy": any(k.arg == "legacy" and getattr(k.value, "value", None) is True for k in fetch.keywords),
        "usd": getattr(fetch.args[0], "value", None) == "USD",
        "order": money is not rounded and uses_rate(money) and not uses_rate(rounded),
        "product": uses_rate(rounded.args[0]) and any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult)
                                                      for n in ast.walk(rounded.args[0])),
        "pii": any(isinstance(n, ast.Attribute) and n.attr == "email" for n in ast.walk(node)),
        "swallow": any(isinstance(h, ast.ExceptHandler) and getattr(h.type, "id", None) in (None, "Exception")
                       and any(getattr(s.value, "value", None) == 1.0 for s in h.body if isinstance(s, ast.Assign))
                       for h in ast.walk(node)),
        "truncates": bool(calls(node, "int")),
        "import": not any(isinstance(n, ast.ImportFrom) and any(a.name == "round_cents" for a in n.names)
                          for n in module.body),
        "naming": not re.fullmatch(rf"{stem}_line_\d+", node.name),
    }[kind]


class _EveryScale:
    """Contracts a fixture keeps at every scale; test classes set SCALE (None: build()'s default)."""

    SCALE = None

    @classmethod
    def setUpClass(cls):
        cls.snapshot = _snapshot(CLEAN_GIT, cls.SCALE)
        cls.keys, cls.sources = cls.snapshot["keys"], cls.snapshot["sources"]

    def test_ids_unique_and_every_item_judgeable(self):
        for name, key in self.keys.items():
            ids = [item["id"] for item in key["items"]]
            self.assertEqual(len(ids), len(set(ids)), name)
            # An empty state would make judge_batch reject the whole grading batch.
            self.assertTrue(key["flag_label"] and all(item["desc"] for item in key["items"]), name)
            self.assertTrue(all(item["flag"] in (True, False, None) for item in key["items"]), name)

    def test_keys_identical_across_builds_and_user_diff_config(self):
        self.assertEqual(_snapshot(CLEAN_GIT, self.SCALE)["keys"], self.keys)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "gitconfig"
            config.write_text("[diff]\n\tcontext = 12\n\tinterHunkContext = 40\n\talgorithm = histogram\n"
                              "\tnoprefix = true\n\trenames = copies\n[color]\n\tui = always\n")
            self.assertEqual(_snapshot(dict(CLEAN_GIT, GIT_CONFIG_GLOBAL=str(config)), self.SCALE)["keys"], self.keys)


class AnswerKeyTests(_EveryScale, unittest.TestCase):
    def test_default_build_is_byte_identical_to_the_fixture_of_recorded_results(self):
        self.assertEqual(self.snapshot["trees"], ["419371f788a2bd7f6314105fa0cfb663851a2e4a",
                                                  "b24d83f6616e9eb0c00abc455b6239882701bfff"])

    def test_seven_of_28_call_sites_flagged_and_ids_point_at_their_lines(self):
        key = self.keys["legacy_callsites"]
        self.assertEqual(len(key["items"]), 28)
        self.assertEqual(len(_flagged(key)), 7)
        for item in key["items"]:
            path, line = item["id"].rsplit(":", 1)
            text = self.sources[path][int(line) - 1]
            self.assertIn("fetch_rate(", text, item["id"])
            self.assertEqual("legacy=" in text, item["flag"], item["id"])

    def test_flagged_hunks_and_functions_are_exactly_the_unexplained_drift(self):
        hunk_files = sorted(item_id.split(":")[0] for item_id in _flagged(self.keys["unexplained_hunks"]))
        self.assertEqual(hunk_files, sorted([f"billing/{name}.py" for name in fixture.DRIFT] + ["settings.py"]))
        added = {f"billing/{name}.py:{lines[0].removeprefix('def ').split('(')[0]}"
                 for name, lines in fixture.DRIFT.items()}
        self.assertEqual(_flagged(self.keys["unexplained_functions"]), added)

    def test_every_case_names_an_existing_key_expectation_and_scale(self):
        for cases in ("cases.json", "cases-large.json", "holdout.json"):
            for case in json.loads((ROOT / "evals" / cases).read_text())["cases"]:
                label = f"{cases} {case['id']}"
                self.assertIn(case["expect"], ("trigger", "no_trigger", "either"), label)
                scale = case.get("scale", 1)
                self.assertTrue(isinstance(scale, int) and 1 <= scale <= fixture.MAX_SCALE, label)
                self.assertIn("key", case, label)
                self.assertTrue(case["key"] is None or case["key"] in self.keys, label)


class ScaledAnswerKeyTests(_EveryScale, unittest.TestCase):
    SCALE = 3

    def test_flagged_hunks_are_the_drift_injections_one_hunk_each_and_nothing_the_message_explains(self):
        items = self.keys["unexplained_hunks"]["items"]
        injected = [module["path"] for module in fixture.plan(self.SCALE) if module["drift"]]
        flagged = [item["id"].split(":")[0] for item in items if item["flag"] is True]
        self.assertEqual(sorted(flagged), sorted(injected + ["settings.py"]))
        # Independently of the construction: a hunk is explained exactly when rewriting its removed
        # lines as the message describes gives its added lines (money.py rewrites round_cents itself).
        for item in items:
            path = item["id"].split(":")[0]
            changes = item["desc"].split("which makes these changes: ", 1)[1].split(" | ")
            removed = sorted(_as_fixed(change[2:]) for change in changes if change.startswith("- "))
            added = sorted(change[2:] for change in changes if change.startswith("+ "))
            explained = path == "billing/money.py" or (path != "settings.py" and removed == added)
            self.assertEqual(item["flag"], not explained, item["id"])

    def test_every_finding_verdict_matches_the_cited_function_code(self):
        flags = {item["id"]: item["flag"] for item in self.keys["valid_findings"]["items"]}
        kind_of = {text: kind for kind, texts in fixture.SCALED_CLAIMS.items() for text in texts}
        verdicts = {}
        for finding in self.snapshot["findings"]:
            path = finding["file"]
            module = ast.parse("\n".join(self.sources[path]))
            node = next((n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == finding["function"]),
                        None)
            self.assertIsNotNone(node, finding["id"])
            self.assertTrue(node.lineno <= finding["line"] <= node.end_lineno, finding["id"])
            kind = kind_of[finding["claim"]]
            self.assertEqual(flags[finding["id"]], _verdict_from_code(kind, module, Path(path).stem, node), finding["id"])
            verdicts.setdefault(kind, set()).add(flags[finding["id"]])
        for kind in ("legacy", "order", "product", "usd"):
            self.assertEqual(verdicts[kind], {True, False}, kind)

    def test_log_kinds_cover_every_error_and_warn_line_exactly_once(self):
        items = self.keys["log_real_bugs"]["items"]
        patterns = {}
        for item in items:
            level, template = item["id"].split(" ", 1)
            patterns[item["id"]] = re.compile(rf"\S+ {level} " + re.sub(r"\\\{\w+\\\}", ".+?", re.escape(template)))
        lines = {item_id: [] for item_id in patterns}
        for n, line in enumerate(self.snapshot["log"], 1):
            if line.split(" ", 2)[1] in ("ERROR", "WARN"):
                hits = [item_id for item_id, pattern in patterns.items() if pattern.fullmatch(line)]
                self.assertEqual(len(hits), 1, f"line {n} ({line}) matches {hits}")
                lines[hits[0]].append(n)
        for item in items:  # each item's desc lists exactly the lines of its kind
            listed = re.search(r"\(\d+ lines: ([\d, ]+)\)", item["desc"])[1]
            self.assertEqual([int(n) for n in listed.split(", ")], lines[item["id"]], item["id"])


class RunSummaryTests(unittest.TestCase):
    def test_either_runs_are_their_own_count_and_stay_out_of_the_trigger_rates(self):
        def result(expect, skill_read):
            return {"case": expect, "rep": 1, "expect": expect, "keyword": False, "infra": False, "pass": True,
                    "skill_read": skill_read, "skill_before_data": skill_read, "judged": False,
                    "skill_source_ok": None, "eval_errors": 0, "secs": 1.0, "usage": {"cost": None}}
        summary = run.summarize([result("trigger", False), result("no_trigger", False),
                                 result("either", True), result("either", False)])
        self.assertEqual((summary["auto_trigger"], summary["false_trigger"], summary["either"]), ("0/1", "0/1", "1/2"))


class ScoreItemsTests(unittest.TestCase):
    def test_misses_count_whether_cleared_or_unmentioned_and_failed_judgments_count_nowhere(self):
        truth = {"a": True, "b": True, "c": True, "d": False, "e": False, "f": True}
        verdicts = {"a": "flagged", "b": "cleared", "c": "not_mentioned", "d": "flagged", "e": "cleared"}
        self.assertEqual(grade.score_items(truth, verdicts),
                         {"tp": 1, "fp": 1, "fn": 2, "recall": 1 / 3, "precision": 0.5, "false_flags": 1})

    def test_empty_truth_has_no_recall_and_an_answer_flagging_nothing_has_no_precision(self):
        self.assertEqual(grade.score_items({"a": False, "b": False}, {"a": "flagged", "b": "cleared"}),
                         {"tp": 0, "fp": 1, "fn": 0, "recall": None, "precision": 0.0, "false_flags": 1})
        self.assertEqual(grade.score_items({"a": True, "b": False}, {"a": "not_mentioned", "b": "cleared"}),
                         {"tp": 0, "fp": 0, "fn": 1, "recall": 0.0, "precision": None, "false_flags": 0})


if __name__ == "__main__":
    unittest.main()
