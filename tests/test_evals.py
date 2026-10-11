"""Behavioral contracts for the eval fixture and answer keys (evals/fixture.py), answer scoring
(evals/grade.py) and run summaries (evals/run.py)."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
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
    build()'s default): answer keys, Python sources as lines, and generated log lines."""
    with mock.patch.dict(os.environ, environment, clear=True), tempfile.TemporaryDirectory() as tmp:
        root = fixture.build(Path(tmp) / "repo") if scale is None else fixture.build(Path(tmp) / "repo", scale=scale)
        return {
            "keys": fixture.answer_keys(root),
            "sources": {str(path.relative_to(root)): path.read_text().splitlines() for path in root.rglob("*.py")},
            "log": (root / "logs" / "worker.log").read_text().splitlines(),
        }


def _flagged(key):
    return {item["id"] for item in key["items"] if item["flag"] is True}




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

    def test_call_site_ids_point_at_their_lines_and_flags_match_legacy_arguments(self):
        key = self.keys["legacy_callsites"]
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


class ScaledAnswerKeyTests(_EveryScale, unittest.TestCase):
    SCALE = 3

    def test_flagged_hunks_cover_drift_injections_only(self):
        items = self.keys["unexplained_hunks"]["items"]
        injected = [module["path"] for module in fixture.plan(self.SCALE) if module["drift"]]
        flagged = [item["id"].split(":")[0] for item in items if item["flag"] is True]
        self.assertEqual(sorted(flagged), sorted(injected + ["settings.py"]))

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


class CacheSnapshotTests(unittest.TestCase):
    def _query(self, path, sql):
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _write(self, path, sql):
        connection = sqlite3.connect(path)
        try:
            connection.executescript(sql)
            connection.commit()
        finally:
            connection.close()

    def _assert_fingerprint(self, descriptor, contents):
        self.assertEqual(descriptor["initial_sha256"], hashlib.sha256(contents).hexdigest())
        self.assertEqual(descriptor["initial_bytes"], len(contents))

    def test_warm_clones_capture_wal_and_isolate_attempts_from_live_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, workdir = root / "source.sqlite", root / "work"
            writer = sqlite3.connect(source)
            try:
                self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE entries (value TEXT)")
                writer.commit()
                self.assertEqual(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(), (0, 0, 0))
                schema_bytes = source.read_bytes()
                writer.execute("INSERT INTO entries VALUES ('wal-seed')")
                writer.commit()
                wal = Path(str(source) + "-wal")
                before = (source.read_bytes(), wal.read_bytes())
                self.assertEqual(before[0], schema_bytes)
                self.assertGreater(len(before[1]), 32)

                frozen = run.freeze_cache_seed(source, workdir)
                pristine = frozen["path"].read_bytes()
                self.assertTrue(frozen["path"].resolve().is_relative_to(workdir.resolve()))
                self.assertNotEqual(frozen["path"].resolve(), source.resolve())
                self.assertEqual(frozen["kind"], "frozen_sqlite_seed")
                self.assertEqual(frozen["isolation"], "frozen_seed")
                self.assertEqual(frozen["source"], {"path": str(source.resolve()),
                                                  "device": source.stat().st_dev,
                                                  "inode": source.stat().st_ino})
                self.assertEqual(frozen["snapshot"], {"method": "sqlite_backup",
                                                    "source_access": "read_only", "wal_included": True})
                self._assert_fingerprint(frozen, pristine)
                # Read locks may change SHM; only main/WAL bytes must remain untouched.
                self.assertEqual((source.read_bytes(), wal.read_bytes()), before)
                self.assertEqual(self._query(frozen["path"], "SELECT value FROM entries"), [("wal-seed",)])

                args = ("warm", frozen, workdir, "../../escaped/case", 1, 1)
                first = run.cache_plan(*args)
                self.assertEqual(first["path"].read_bytes(), pristine)
                self._assert_fingerprint(first, pristine)
                self.assertEqual(self._query(first["path"], "SELECT value FROM entries"), [("wal-seed",)])
                self._write(first["path"], "INSERT INTO entries VALUES ('first-only')")
                second = run.cache_plan(*args)
                self.assertNotEqual(first["path"], second["path"])
                self.assertEqual(second["path"].read_bytes(), pristine)
                self._assert_fingerprint(second, pristine)
                self.assertEqual(self._query(second["path"], "SELECT value FROM entries"), [("wal-seed",)])
                self._write(second["path"], "INSERT INTO entries VALUES ('second-only')")
                self.assertEqual(self._query(first["path"], "SELECT value FROM entries ORDER BY value"),
                                 [("first-only",), ("wal-seed",)])
                self.assertEqual(self._query(second["path"], "SELECT value FROM entries ORDER BY value"),
                                 [("second-only",), ("wal-seed",)])
                self.assertEqual(self._query(source, "SELECT value FROM entries"), [("wal-seed",)])
                self.assertEqual(self._query(frozen["path"], "SELECT value FROM entries"), [("wal-seed",)])
                self.assertEqual(frozen["path"].read_bytes(), pristine)
                self.assertEqual((source.read_bytes(), wal.read_bytes()), before)

                writer.execute("INSERT INTO entries VALUES ('later-source')")
                writer.commit()
                updated_source = (source.read_bytes(), wal.read_bytes())
                self.assertNotEqual(updated_source, before)
                third = run.cache_plan(*args)
                self.assertEqual(len({first["path"], second["path"], third["path"]}), 3)
                self.assertEqual(third["path"].read_bytes(), pristine)
                self._assert_fingerprint(third, pristine)
                self.assertEqual(self._query(third["path"], "SELECT value FROM entries"), [("wal-seed",)])
                self._write(third["path"], "INSERT INTO entries VALUES ('third-only')")
                self.assertEqual(self._query(third["path"], "SELECT value FROM entries ORDER BY value"),
                                 [("third-only",), ("wal-seed",)])
                self.assertEqual(self._query(source, "SELECT value FROM entries ORDER BY value"),
                                 [("later-source",), ("wal-seed",)])
                self.assertEqual(self._query(frozen["path"], "SELECT value FROM entries"), [("wal-seed",)])
                self.assertEqual(frozen["path"].read_bytes(), pristine)
                self.assertEqual((source.read_bytes(), wal.read_bytes()), updated_source)
                for plan in (first, second, third):
                    self.assertTrue(plan["path"].resolve().is_relative_to((workdir / "warm-cache").resolve()))
                    self.assertNotEqual(plan["path"].resolve(), frozen["path"].resolve())
                    self.assertEqual(plan["mode"], "warm")
                    self.assertEqual(plan["status"], "frozen_seed_clone")
                    self.assertIs(plan["seeded"], True)
                    self.assertEqual(plan["isolation"], "independent_per_attempt")
                    self.assertEqual(plan["seed_path"], str(frozen["path"].resolve()))
                    self.assertEqual(plan["source"], frozen["source"])
                    self.assertEqual(plan["snapshot"], frozen["snapshot"])
            finally:
                writer.close()

    def test_cold_repeated_arguments_start_distinct_empty_writable_contained_databases(self):
        for case_id in ("ordinary-case", "../../escaped/case"):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp) / "work"
                args = ("cold", None, workdir, case_id, 1, 1)
                first = run.cache_plan(*args)
                self.assertEqual(first["path"].read_bytes(), b"")
                self.assertEqual(self._query(first["path"], "SELECT name FROM sqlite_master"), [])
                self._write(first["path"], "CREATE TABLE entries (value TEXT); INSERT INTO entries VALUES ('first')")
                second = run.cache_plan(*args)
                self.assertNotEqual(first["path"], second["path"])
                self.assertEqual(second["path"].read_bytes(), b"")
                self.assertEqual(self._query(second["path"], "SELECT name FROM sqlite_master"), [])
                self._write(second["path"], "CREATE TABLE entries (value TEXT); INSERT INTO entries VALUES ('second')")
                self.assertEqual(self._query(first["path"], "SELECT value FROM entries"), [("first",)])
                self.assertEqual(self._query(second["path"], "SELECT value FROM entries"), [("second",)])
                for plan in (first, second):
                    self.assertTrue(plan["path"].resolve().is_relative_to((workdir / "cold-cache").resolve()))
                    self.assertEqual(plan["mode"], "cold")
                    self.assertEqual(plan["status"], "fresh_empty_namespace")
                    self.assertIs(plan["seeded"], False)
                    self.assertEqual(plan["isolation"], "independent_per_attempt")
                    self._assert_fingerprint(plan, b"")

    def test_warm_rejects_live_path_instead_of_frozen_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite"
            self._write(source, "CREATE TABLE entries (value TEXT); INSERT INTO entries VALUES ('seed')")
            before = source.read_bytes()
            for namespace in (source, str(source)):
                with self.subTest(namespace=namespace), self.assertRaisesRegex(ValueError, "descriptor from freeze_cache_seed"):
                    run.cache_plan("warm", namespace, Path(tmp) / "work", "case", 1, 1)
            self.assertEqual(source.read_bytes(), before)

    def test_freeze_rejects_missing_empty_and_corrupt_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, workdir = Path(tmp), Path(tmp) / "work"
            empty, corrupt, missing = root / "empty.sqlite", root / "corrupt.sqlite", root / "missing.sqlite"
            empty.write_bytes(b"")
            corrupt.write_bytes(b"not a SQLite database")
            for source in (None, ""):
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, "requires --cache-namespace"):
                    run.freeze_cache_seed(source, workdir)
            for source in (missing, empty):
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, "missing or empty"):
                    run.freeze_cache_seed(source, workdir)
            with self.assertRaisesRegex(RuntimeError, "could not freeze warm cache seed"):
                run.freeze_cache_seed(corrupt, workdir)
            self.assertFalse(missing.exists())
            self.assertEqual(empty.read_bytes(), b"")
            self.assertEqual(corrupt.read_bytes(), b"not a SQLite database")


class RunSummaryTests(unittest.TestCase):
    def test_unknown_judgments_are_reported_separately_from_known_trigger_observations(self):
        def result(case, judged, *, expect="trigger", keyword=False, infra=False):
            return {"case": case, "rep": 1, "expect": expect, "keyword": keyword, "infra": infra, "pass": True,
                    "skill_read": True, "skill_before_data": True, "judged": judged,
                    "skill_source_ok": None, "eval_errors": 0, "secs": 1.0, "usage": {"cost": None}}
        summary = run.summarize([
            result("auto", True), result("auto", False), result("auto", None),
            result("keyword", None, keyword=True), result("negative", None, expect="no_trigger"),
            result("either", None, expect="either"), result("infra", None, infra=True),
        ])
        self.assertEqual(summary["auto_judged"], "1/3")
        self.assertEqual(summary["auto_judged_unverified"], 1)
        self.assertEqual(summary["per_case"]["auto"]["n"], 3)
        self.assertEqual(summary["per_case"]["auto"]["judged"], 1)
        self.assertEqual(summary["per_case"]["auto"]["judged_unverified"], 1)
        for case in ("keyword", "negative", "either"):
            self.assertEqual(summary["per_case"][case]["judged"], 0)
            self.assertEqual(summary["per_case"][case]["judged_unverified"], 1)
        self.assertNotIn("infra", summary["per_case"])
        unknown_only = run.summarize([result("auto", None)])
        self.assertEqual(unknown_only["auto_judged"], "0/1")
        self.assertEqual(unknown_only["auto_judged_unverified"], 1)

    def test_either_runs_are_their_own_count_and_stay_out_of_the_trigger_rates(self):
        def result(expect, skill_read):
            return {"case": expect, "rep": 1, "expect": expect, "keyword": False, "infra": False, "pass": True,
                    "skill_read": skill_read, "skill_before_data": skill_read, "judged": False,
                    "skill_source_ok": None, "eval_errors": 0, "secs": 1.0, "usage": {"cost": None}}
        summary = run.summarize([result("trigger", False), result("no_trigger", False),
                                 result("either", True), result("either", False)])
        self.assertEqual((summary["auto_trigger"], summary["false_trigger"], summary["either"]), ("0/1", "0/1", "1/2"))


class ScoreItemsTests(unittest.TestCase):
    def test_cleared_unmentioned_and_failed_true_items_all_remain_misses(self):
        truth = {"a": True, "b": True, "c": True, "d": False, "e": False, "f": True}
        verdicts = {"a": "flagged", "b": "cleared", "c": "not_mentioned", "d": "flagged", "e": "cleared"}
        score = grade.score_items(truth, verdicts)
        self.assertEqual(score["tp"], 1)
        self.assertEqual(score["fp"], 1)
        self.assertEqual(score["fn"], 3)
        self.assertEqual(score["recall"], 0.25)
        self.assertEqual(score["precision"], 0.5)
        self.assertEqual(score["false_flags"], 1)
        self.assertEqual(score["planned_items"], 6)
        self.assertEqual(score["unresolved"], 1)
        self.assertEqual(score["omitted"], 1)
        self.assertEqual(score["accuracy"], 2 / 6)

    def test_extraction_errors_override_apparent_flags_without_dropping_truth(self):
        score = grade.score_items({"a": True, "b": False}, {"a": "flagged", "b": "cleared"},
                                  errors={"a": "extraction failed"})
        self.assertEqual(score["planned_items"], 2)
        self.assertEqual(score["unresolved"], 1)
        self.assertEqual(score["tp"], 0)
        self.assertEqual(score["fn"], 1)
        self.assertEqual(score["recall"], 0.0)
        self.assertEqual(score["accuracy"], 0.5)
        self.assertIsNone(score["precision"])

    def test_empty_truth_has_no_recall_and_an_answer_flagging_nothing_has_no_precision(self):
        false_only = grade.score_items({"a": False, "b": False}, {"a": "flagged", "b": "cleared"})
        self.assertEqual(false_only["tp"], 0)
        self.assertEqual(false_only["fp"], 1)
        self.assertEqual(false_only["fn"], 0)
        self.assertIsNone(false_only["recall"])
        self.assertEqual(false_only["precision"], 0.0)
        self.assertEqual(false_only["false_flags"], 1)
        unmentioned = grade.score_items({"a": True, "b": False}, {"a": "not_mentioned", "b": "cleared"})
        self.assertEqual(unmentioned["tp"], 0)
        self.assertEqual(unmentioned["fp"], 0)
        self.assertEqual(unmentioned["fn"], 1)
        self.assertEqual(unmentioned["recall"], 0.0)
        self.assertIsNone(unmentioned["precision"])
        self.assertEqual(unmentioned["false_flags"], 0)


# Tiny schema2 rows deliberately independent of the generated datasets and private keys.
def _finding(iid, defect, *, factual="supported", actions=("fix_now",), introduced="yes"):
    return {"id": iid, "desc": f"Synthetic claim {iid}", "factual": factual,
            "actions": list(actions), "introduced": introduced, "defect": defect,
            "group": "local", "evidence": ["local:1"], "rationale": "Synthetic regression key."}


def _assertion(*, factual="supported", action="fix_now", introduced="yes"):
    return {"factual": factual, "action": action, "introduced": introduced}


class FacetScoreTests(unittest.TestCase):
    def test_true_nonblocking_debt_can_be_correctly_deferred_or_left_alone(self):
        items = [_finding("debt", None, actions=("defer", "no_action"), introduced="no")]
        for action in ("defer", "no_action"):
            with self.subTest(action=action):
                score = grade.score_facets(items, {"debt": _assertion(action=action, introduced="no")})
                self.assertEqual(score["factual_accuracy"], 1.0)
                self.assertEqual(score["action_accuracy"], 1.0)
                self.assertEqual(score["introduced_accuracy"], 1.0)
                self.assertEqual(score["repair"]["planned_defects"], 0)
                self.assertEqual(score["repair"]["tp"], 0)
                self.assertEqual(score["repair"]["fp"], 0)
                self.assertIsNone(score["repair"]["recall"])
                self.assertIsNone(score["repair"]["precision"])

    def test_fix_now_on_true_nonblocking_debt_is_not_a_justified_repair(self):
        items = [_finding("debt", None, actions=("defer", "no_action"), introduced="no")]
        score = grade.score_facets(items, {"debt": _assertion(introduced="no")})
        self.assertEqual(score["factual_accuracy"], 1.0)
        self.assertEqual(score["action_accuracy"], 0.0)
        self.assertEqual(score["repair"]["tp"], 0)
        self.assertEqual(score["repair"]["fp"], 1)
        self.assertEqual(score["repair"]["precision"], 0.0)

    def test_fix_now_on_refuted_key_cannot_earn_credit_even_if_answer_claims_support(self):
        items = [_finding("false", None, factual="refuted", actions=("no_action",), introduced="no")]
        for factual in ("supported", "refuted"):
            with self.subTest(factual=factual):
                score = grade.score_facets(items, {"false": _assertion(factual=factual, introduced="no")})
                self.assertEqual(score["repair"]["tp"], 0)
                self.assertEqual(score["repair"]["fp"], 1)
                self.assertEqual(score["repair"]["precision"], 0.0)
                self.assertEqual(score["action_accuracy"], 0.0)

    def test_fix_now_requires_definite_supported_factual_assertion(self):
        items = [_finding("bug", "bug")]
        for factual in ("refuted", "omitted", "underdetermined", None):
            with self.subTest(factual=factual):
                score = grade.score_facets(items, {"bug": _assertion(factual=factual)})
                self.assertEqual(score["factual_accuracy"], 0.0)
                self.assertEqual(score["action_accuracy"], 1.0)
                self.assertEqual(score["repair"]["tp"], 0)
                self.assertEqual(score["repair"]["fp"], 1)
                self.assertEqual(score["repair"]["fn"], 1)
                self.assertEqual(score["repair"]["recall"], 0.0)
                self.assertEqual(score["repair"]["precision"], 0.0)

    def test_canonical_duplicate_claims_do_not_boost_recall_or_precision(self):
        items = [_finding("a", "first"), _finding("b", "second"),
                 _finding("false", "spurious", factual="refuted", actions=("no_action",))]
        verdicts = {"a": _assertion(), "b": _assertion(action="omitted"), "false": _assertion()}
        baseline = grade.score_facets(items, verdicts)
        repeated = grade.score_facets([*items, _finding("a-copy", "first"),
                                       _finding("false-copy", "spurious", factual="refuted", actions=("no_action",))],
                                      {**verdicts, "a-copy": _assertion(), "false-copy": _assertion()})
        for score in (baseline, repeated):
            self.assertEqual(score["repair"]["planned_defects"], 2)
            self.assertEqual(score["repair"]["predicted_defects"], 2)
            self.assertEqual(score["repair"]["tp"], 1)
            self.assertEqual(score["repair"]["fp"], 1)
            self.assertEqual(score["repair"]["fn"], 1)
            self.assertEqual(score["repair"]["recall"], 0.5)
            self.assertEqual(score["repair"]["precision"], 0.5)

    def test_introduction_is_measured_independently_of_truth_and_action(self):
        items = [_finding("old", "old", introduced="no"), _finding("new", "new")]
        score = grade.score_facets(items, {"old": _assertion(introduced="no"),
                                          "new": _assertion(introduced="no")})
        self.assertEqual(score["factual_accuracy"], 1.0)
        self.assertEqual(score["action_accuracy"], 1.0)
        self.assertEqual(score["introduced_accuracy"], 0.5)
        self.assertEqual(score["facets"]["introduced"]["correct"], 1)
        self.assertEqual(score["repair"]["tp"], 1)
        self.assertEqual(score["repair"]["fp"], 1)
        self.assertEqual(score["repair"]["fn"], 1)

    def test_omission_and_extraction_failure_keep_all_planned_denominators(self):
        items = [_finding("answered", "one"), _finding("omitted", "two"), _finding("failed", "three")]
        omitted = _assertion(factual="omitted", action="omitted", introduced="omitted")
        score = grade.score_facets(items, {"answered": _assertion(), "omitted": omitted,
                                          "failed": _assertion()}, errors={"failed": "extraction failed"})
        self.assertEqual(score["planned_items"], 3)
        self.assertEqual(score["unresolved_items"], 1)
        self.assertEqual(score["unresolved_facets"], 3)
        for facet in ("factual", "action", "introduced"):
            with self.subTest(facet=facet):
                metrics = score["facets"][facet]
                self.assertEqual(metrics["planned"], 3)
                self.assertEqual(metrics["correct"], 1)
                self.assertEqual(metrics["accuracy"], 1 / 3)
                self.assertEqual(metrics["coverage"], 1 / 3)
                self.assertEqual(metrics["omitted"], 1)
                self.assertEqual(metrics["extraction_errors"], 1)
                self.assertEqual(metrics["unresolved"], 1)
        self.assertEqual(score["repair"]["planned_defects"], 3)
        self.assertEqual(score["repair"]["tp"], 1)
        self.assertEqual(score["repair"]["fn"], 2)
        self.assertEqual(score["repair"]["recall"], 1 / 3)

    def test_correct_uncertainty_is_creditable_but_not_a_definite_repair(self):
        items = [_finding("uncertain", "gap", factual="underdetermined",
                          actions=("investigate",), introduced="unknown")]
        score = grade.score_facets(items, {"uncertain": _assertion(
            factual="underdetermined", action="investigate", introduced="unknown")})
        for facet in ("factual", "action", "introduced"):
            self.assertEqual(score["facets"][facet]["planned"], 1)
            self.assertEqual(score["facets"][facet]["accuracy"], 1.0)
            self.assertEqual(score["facets"][facet]["key_uncertain"], 1)
            self.assertEqual(score["facets"][facet]["asserted_uncertain"], 1)
        self.assertEqual(score["unresolved_items"], 0)
        self.assertEqual(score["evidence_unresolved_items"], ["uncertain"])
        self.assertEqual(score["repair"]["tp"], 0)
        self.assertIsNone(score["repair"]["recall"])

    def test_missing_extracted_item_remains_unresolved_not_removed_from_denominator(self):
        score = grade.score_facets([_finding("seen", "one"), _finding("missing", "two")],
                                  {"seen": _assertion()})
        self.assertEqual(score["planned_items"], 2)
        self.assertEqual(score["unresolved_items"], 1)
        for facet in ("factual", "action", "introduced"):
            self.assertEqual(score["facets"][facet]["planned"], 2)
            self.assertEqual(score["facets"][facet]["accuracy"], 0.5)
            self.assertEqual(score["facets"][facet]["unresolved"], 1)
        self.assertEqual(score["repair"]["fn"], 1)
        self.assertEqual(score["repair"]["recall"], 0.5)

    def test_facet_extraction_failure_does_not_erase_other_facet_credit(self):
        score = grade.score_facets([_finding("bug", "bug")], {"bug": _assertion()},
                                  errors={"bug": {"factual": "invalid extraction"}})
        self.assertEqual(score["factual_accuracy"], 0.0)
        self.assertEqual(score["action_accuracy"], 1.0)
        self.assertEqual(score["introduced_accuracy"], 1.0)
        self.assertEqual(score["facets"]["factual"]["unresolved"], 1)
        self.assertEqual(score["unresolved_facets"], 1)
        self.assertEqual(score["repair"]["tp"], 0)
        self.assertEqual(score["repair"]["fp"], 1)
        self.assertEqual(score["repair"]["fn"], 1)

    def test_conflicting_spurious_duplicate_invalidates_canonical_repair_in_any_order(self):
        valid = _finding("valid", "shared")
        spurious = _finding("spurious", "shared", factual="refuted", actions=("no_action",))
        verdicts = {"valid": _assertion(), "spurious": _assertion()}
        for items in ([valid, spurious], [spurious, valid]):
            with self.subTest(first=items[0]["id"]):
                score = grade.score_facets(items, verdicts)
                self.assertEqual(score["repair"]["planned_defects"], 1)
                self.assertEqual(score["repair"]["predicted_defects"], 1)
                self.assertEqual(score["repair"]["tp"], 0)
                self.assertEqual(score["repair"]["fp"], 1)
                self.assertEqual(score["repair"]["fn"], 1)
                self.assertEqual(score["repair"]["recall"], 0.0)
                self.assertEqual(score["repair"]["precision"], 0.0)

    def test_unselected_spurious_duplicate_cannot_hide_an_unsupported_repair(self):
        items = [_finding("spurious", "shared", factual="refuted", actions=("no_action",)),
                 _finding("spurious-copy", "shared", factual="refuted", actions=("no_action",))]
        selected = _assertion()
        unselected = _assertion(factual="refuted", action="no_action")
        for verdicts in ({"spurious": selected, "spurious-copy": unselected},
                         {"spurious": unselected, "spurious-copy": selected}):
            with self.subTest(selected=[iid for iid, value in verdicts.items() if value["action"] == "fix_now"]):
                score = grade.score_facets(items, verdicts)
                self.assertEqual(score["repair"]["tp"], 0)
                self.assertEqual(score["repair"]["fp"], 1)
                self.assertEqual(score["repair"]["predicted_defects"], 1)
                self.assertEqual(score["repair"]["precision"], 0.0)


class GradeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_key_switch_retains_identities_and_scores_current_schema2_key(self):
        original = {"schema": 2, "items": [_finding("shared", "original-defect")]}
        selected = {"schema": 2, "items": [
            _finding("shared", None, factual="refuted", actions=("defer",), introduced="no"),
            _finding("selected-bug", "selected-defect", introduced="no"),
            _finding("selected-debt", None, actions=("no_action",), introduced="no"),
        ]}
        captured_base = {"key": "original", "dataset": "local", "scale": 1,
                         "fixture_sha256": "a" * 64, "private_keys_sha256": "b" * 64,
                         "public_rubric_sha256": "c" * 64}
        bundles = {("local", 1): {"keys": {"original": original, "selected": selected},
                                  "fixture_sha256": "a" * 64, "private_keys_sha256": "b" * 64,
                                  "public_identity": {"review/rubric.json": "c" * 64}}}

        async def fake_judge(states, questions, timeout, concurrency):
            return {sid: ({"factual": "supported", "action": "omitted"}
                          if state["id"] == "selected-debt" else _assertion())
                    for sid, state in states.items()}, {}, {"id": "switch", "cost_usd": 0.25, "cache": {}}

        for label, captured in (("name-only", captured_base),
                                ("name-and-digest", {**captured_base, "key_sha256": grade._digest(original)})):
            with self.subTest(capture=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cases, source, ledger_path = root / "cases.json", root / "capture.json", root / "ledger.json"
                cases.write_text(json.dumps({"cases": [
                    {"id": "outcome", "dataset": "local", "scale": 1, "key": "selected"}]}))
                source.write_text(json.dumps({"schema": 2, "fixture_identities": {"outcome": captured},
                                              "runs": [{"case": "outcome", "answer": "Synthetic answer."}]}))
                with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                        mock.patch.object(grade, "_judge", side_effect=fake_judge), \
                        mock.patch.object(grade, "_print_table"):
                    result = await grade.grade(source, cases, ledger_path=ledger_path)
                report = json.loads(source.with_suffix(".graded.json").read_text())
                ledger = json.loads(ledger_path.read_text())
                for row in (result["runs"][0], report["runs"][0]):
                    self.assertEqual(row["key_binding"], {"captured": captured, "status": "changed"})
                    identity = row["key_identity"]
                    self.assertEqual(tuple(identity[field] for field in ("key", "key_sha256", "dataset", "scale")),
                                     ("selected", grade._digest(selected), "local", 1))
                    self.assertEqual((identity["fixture_sha256"], identity["private_keys_sha256"],
                                      identity["public_identity"]["review/rubric.json"]),
                                     (captured["fixture_sha256"], captured["private_keys_sha256"],
                                      captured["public_rubric_sha256"]))
                self.assertEqual(report["execution"]["metadata"]["fixture_identities"]["outcome"], captured)
                self.assertEqual(ledger["key_identities"]["outcome"], result["runs"][0]["key_identity"])
                self.assertEqual(ledger["frozen_keys"]["outcome"], selected)
                for facet, correct, accuracy in (("factual", 2, 2 / 3), ("action", 1, 1 / 3), ("introduced", 0, 0.0)):
                    score = result["runs"][0]["facets"][facet]
                    self.assertEqual((score["planned"], score["correct"], score["accuracy"]), (3, correct, accuracy))
                self.assertEqual(tuple(report["runs"][0][f"{facet}_accuracy"]
                                       for facet in ("factual", "action", "introduced")), (2 / 3, 1 / 3, 0.0))

    async def test_selected_key_binding_distinguishes_unrecorded_and_independent_identity_mismatches(self):
        key = {"schema": 2, "items": [_finding("only", "only-defect")]}
        recorded = {"dataset": "local", "scale": 1, "key": "selected", "key_sha256": grade._digest(key),
                    "fixture_sha256": "1" * 64, "private_keys_sha256": "2" * 64,
                    "public_rubric_sha256": "3" * 64}
        bundles = {("local", 1): {"keys": {"selected": key}, "fixture_sha256": "1" * 64,
                                  "private_keys_sha256": "2" * 64,
                                  "public_identity": {"review/rubric.json": "3" * 64}}}
        hashes_only = {field: value for field, value in recorded.items() if field not in ("key", "key_sha256")}
        scenarios = [("absent", None, "unrecorded"), ("hash-only", hashes_only, "unrecorded"),
                     ("matched", recorded, "matched"),
                     ("digest-only", {field: value for field, value in recorded.items() if field != "key"}, "matched")]
        scenarios += [(field, {**recorded, field: value}, "changed")
                      for field, value in (("dataset", "other"), ("scale", 2), ("key_sha256", "4" * 64))]

        async def fake_judge(states, questions, timeout, concurrency):
            return {sid: _assertion() for sid in states}, {}, {"id": "binding", "cost_usd": 0.25, "cache": {}}

        for label, captured, status in scenarios:
            with self.subTest(binding=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cases, source, ledger_path = root / "cases.json", root / "capture.json", root / "ledger.json"
                cases.write_text(json.dumps({"cases": [
                    {"id": "outcome", "dataset": "local", "scale": 1, "key": "selected"}]}))
                capture = {"schema": 2, "runs": [{"case": "outcome", "answer": "Synthetic answer."}]}
                if captured is not None:
                    capture["fixture_identities"] = {"outcome": captured}
                source.write_text(json.dumps(capture))
                with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                        mock.patch.object(grade, "_judge", side_effect=fake_judge), \
                        mock.patch.object(grade, "_print_table"):
                    result = await grade.grade(source, cases, ledger_path=ledger_path)
                report = json.loads(source.with_suffix(".graded.json").read_text())
                ledger = json.loads(ledger_path.read_text())
                for row in (result["runs"][0], report["runs"][0]):
                    self.assertEqual(row["key_binding"], {"captured": captured or {}, "status": status})
                    identity = row["key_identity"]
                    self.assertEqual(tuple(identity[field] for field in ("key", "key_sha256", "dataset", "scale")),
                                     ("selected", grade._digest(key), "local", 1))
                self.assertEqual(report["execution"]["metadata"].get("fixture_identities"),
                                 None if captured is None else {"outcome": captured})
                self.assertEqual(ledger["key_identities"]["outcome"], result["runs"][0]["key_identity"])
                self.assertEqual(ledger["frozen_keys"]["outcome"], key)

    async def test_current_schemas_retain_failed_and_empty_attempts_with_distinct_final_subset_and_execution_cost(self):
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [_finding("bug", "bug")]}},
                                  "public_identity": {}}}
        execution_ledger = run.merge_accounting([
            run.finalize_accounting([run.ledger_row({
                "id": f"{attempt}-cost", "type": "model_usage", "purpose": "judge_batch",
                "timestamp": "2026-10-09T00:00:00.000Z", "api": "openai-completions",
                "provider": "openrouter", "model": "jev", "stopReason": "stop",
                "usage": {"input": 10, "output": 4, "cacheRead": 0, "cacheWrite": 0,
                          "cost": {"total": cost}},
            }, "auxiliary", attempt)], [], ["complete"])
            for attempt, cost in (("failed", 2.0), ("empty", 3.0), ("final", 4.0))
        ])
        attempt_records = [
            {"attempt_id": "failed", "execution_status": "infra_failed",
             "final_answers": [{"text": "The bug is supported and introduced, but defer it."}]},
            {"attempt_id": "empty", "execution_status": "infra_failed", "final_answers": [],
             "answer": "COMPATIBILITY_TEXT_NOT_CAPTURED"},
            {"attempt_id": "final", "execution_status": "completed", "final_answers": [
                {"text": "The bug is supported and introduced; fix it now."},
                {"text": "Fix it now: the supported bug was introduced."},
            ]},
        ]
        for schema in (3, 4):
            with self.subTest(schema=schema):
                extracted_answers = []
                judge_calls = []

                async def fake_judge(states, questions, timeout, concurrency):
                    judge_calls.append(len(states))
                    labels = {}
                    for sid, state in states.items():
                        extracted_answers.append(state["answer"])
                        self.assertEqual(set(state), {"answer", "id", "item"})
                        labels[sid] = _assertion(action="defer" if "defer" in state["answer"] else "fix_now")
                    return labels, {}, {"id": "local-attempt-extraction", "cost_usd": 0.25,
                                        "cost_basis": "batch_status_estimate", "billing_complete": False, "cache": {}}

                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    cases, source, ledger_path = root / "cases.json", root / "captured.json", root / "grading.ledger.json"
                    cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "facets"}]}))
                    source.write_text(json.dumps({"schema": schema, "strategy": "jev-flat", "cost_ledger": execution_ledger,
                                                  "runs": [{"case": "outcome", "rep": 1, "final_attempt_id": "final",
                                                            "answer": "TOP_LEVEL_COMPATIBILITY_NOT_CAPTURED",
                                                            "attempt_records": attempt_records}]}))
                    with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                            mock.patch.object(grade, "_judge", side_effect=fake_judge), \
                            mock.patch.object(grade, "_print_table"):
                        result = await grade.grade(source, cases, ledger_path=ledger_path)
                    grading_ledger = json.loads(ledger_path.read_text())
                    report = json.loads(source.with_suffix(".graded.json").read_text())
                    self.assertEqual(grading_ledger["batch_cost_estimate_usd"], 0.25)
                    self.assertIsNone(grading_ledger["total_cost_usd"])
                    self.assertIsNone(grading_ledger["session_outer_cost_usd"])
                    self.assertFalse(grading_ledger["billing_complete"])
                    self.assertEqual(result["execution"]["source_schema"], schema)
                    for output in (result, report):
                        ledger = output["execution"]["cost_ledger"]
                        self.assertEqual(ledger, execution_ledger)
                        self.assertEqual(ledger["collection_status"], "complete")
                        self.assertEqual(ledger["monetary_status"], "incomplete")
                        self.assertEqual(ledger["totals"]["total"]["reported_subtotal"], 9.0)
                        self.assertEqual(ledger["totals"]["judgment"]["reported_cost"], 9.0)
                        for bucket in ("completion", "auxiliary", "total"):
                            self.assertIsNone(ledger["totals"][bucket]["reported_cost"])
                            self.assertTrue(ledger["totals"][bucket]["cost_unknown"])
                            self.assertTrue(ledger["totals"][bucket]["coverage_unknown"])
                    self.assertEqual({entry["id"] for entry in result["execution"]["cost_ledger"]["entries"]},
                                     {"failed-cost", "empty-cost", "final-cost"})
                    self.assertEqual([attempt["attempt_id"]
                                      for attempt in result["execution"]["runs"][0]["attempts"]],
                                     ["failed", "empty", "final"])
                    self.assertNotIn("cost_ledger", result["execution"]["metadata"])
                    self.assertNotIn("run_cost_ledgers", result["execution"])
                    self.assertNotIn("historical_outer_usage", result["execution"])
                    self.assertFalse(result["execution"]["historical_outer_only"])

                # One extraction batch, one state per retained non-empty answer: no duplicate grading.
                self.assertEqual(judge_calls, [3])
                self.assertEqual(sorted(extracted_answers), sorted(
                    answer["text"] for attempt in attempt_records for answer in attempt["final_answers"]))
                rows = result["runs"]
                self.assertEqual([(row["execution_ref"]["attempt_id"], row["execution_ref"]["answer_index"])
                                  for row in rows], [("failed", 0), ("empty", 0), ("final", 0), ("final", 1)])
                self.assertEqual([row["execution_status"] for row in rows],
                                 ["infra_failed", "infra_failed", "completed", "completed"])
                self.assertEqual([row["final_attempt"] for row in rows], [False, False, True, True])
                self.assertEqual(rows[0]["action_accuracy"], 0.0)
                self.assertTrue(rows[1]["empty"])
                for facet in ("factual", "action", "introduced"):
                    self.assertEqual(rows[1]["facets"][facet]["planned"], 1)
                    self.assertEqual(rows[1]["facets"][facet]["omitted"], 1)
                    self.assertEqual(rows[1]["facets"][facet]["accuracy"], 0.0)
                self.assertEqual(rows[1]["repair"]["fn"], 1)
                for row in rows:
                    self.assertNotIn("cost_ledger", row)
                    self.assertNotIn("usage", row)
                for answer in extracted_answers:
                    self.assertNotIn("COMPATIBILITY", answer)
                overall = result["summary"]["overall"]["by_key_schema"]["2"]
                final = result["summary"]["final_attempt"]["by_key_schema"]["2"]
                self.assertEqual(overall["n"], 4)
                self.assertEqual(overall["empty"], 1)
                self.assertEqual(overall["action_accuracy"], 0.5)
                self.assertEqual(overall["recall"], 0.5)
                self.assertEqual(final["n"], 2)
                self.assertEqual(final["action_accuracy"], 1.0)
                self.assertEqual(final["recall"], 1.0)
                self.assertEqual(result["summary"]["final_per_case"]["outcome"]["by_key_schema"]["2"]["n"], 2)

    async def test_grading_overhead_stays_unknown_with_missing_zero_or_no_batch_cost(self):
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [_finding("bug", "bug")]}},
                                  "public_identity": {}}}
        for answer, batch_cost in (("Review complete.", None), ("Review complete.", 0.0), ("", None)):
            with self.subTest(answer=answer, batch_cost=batch_cost), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cases, source, ledger_path = root / "cases.json", root / "captured.json", root / "grading.json"
                cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "facets"}]}))
                source.write_text(json.dumps({"schema": 4, "cost_ledger": run.finalize_accounting([], [], ["complete"]),
                                              "runs": [{"case": "outcome", "final_attempt_id": "final",
                                                        "attempt_records": [{"attempt_id": "final",
                                                                             "final_answers": [{"text": answer}]}]}]}))
                judge = mock.AsyncMock(return_value=({"0": _assertion()}, {},
                                                     {"id": "batch", "cost_usd": batch_cost, "cache": {}}))
                with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                        mock.patch.object(grade, "_judge", judge), mock.patch.object(grade, "_print_table"):
                    result = await grade.grade(source, cases, ledger_path=ledger_path)
                ledger = json.loads(ledger_path.read_text())
                self.assertEqual(judge.await_count, 1 if answer else 0)
                self.assertEqual(ledger["batch_cost_estimate_usd"], batch_cost if answer else 0)
                self.assertIsNone(ledger["session_outer_cost_usd"])
                self.assertIsNone(ledger["total_cost_usd"])
                self.assertFalse(ledger["billing_complete"])
                self.assertEqual(result["grading"]["ledger_id"], ledger["id"])
                self.assertIsNone(result["execution"]["cost_ledger"]["totals"]["total"]["reported_cost"])

    async def test_current_schemas_reject_omitted_or_null_attempt_records_before_grading(self):
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [_finding("bug", "bug")]}},
                                  "public_identity": {}}}
        judge = mock.AsyncMock()
        for schema in (3, 4):
            for label, records in (("omitted", {}), ("null", {"attempt_records": None})):
                with self.subTest(schema=schema, attempt_records=label), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    cases, source, ledger_path = root / "cases.json", root / "captured.json", root / "grading.ledger.json"
                    cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "facets"}]}))
                    # A top-level answer must not let current executions fall back to historical one-answer grading.
                    source.write_text(json.dumps({"schema": schema, "strategy": "jev-flat",
                                                  "cost_ledger": run.finalize_accounting([], [], ["complete"]),
                                                  "runs": [{"case": "outcome", "rep": 1,
                                                            "answer": "The bug is supported and introduced; fix it now.",
                                                            **records}]}))
                    with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                            mock.patch.object(grade, "_judge", judge), \
                            mock.patch.object(grade, "_print_table"), \
                            self.assertRaisesRegex(ValueError, "current execution omitted retained attempts"):
                        await grade.grade(source, cases, ledger_path=ledger_path)
                    self.assertFalse(ledger_path.exists())
                    self.assertFalse(source.with_suffix(".graded.json").exists())
        judge.assert_not_awaited()

    async def test_partial_batch_extraction_retains_valid_facets_and_reports_only_failed_facet(self):
        batch = mock.Mock()
        batch.id = "local-partial-batch"
        batch.drain = mock.AsyncMock()
        batch.status.return_value = {"done": 1, "running": False, "cost": 0.25,
                                     "model": "local-extractor", "cache": "miss"}
        batch.results.return_value = {"slot": {
            "factual": {"choice": "supported"},
            "action": {"choice": "invalid-action"},
            "introduced": {"choice": "no"},
        }}
        batch.failed.return_value = {}
        states = {"slot": {"id": "bug", "item": "Synthetic claim", "answer": "A pre-existing bug."}}
        with mock.patch.object(grade, "judge_batch", return_value=batch, create=True):
            labels, errors, record = await grade._judge(states, grade.FACET_QUESTIONS, 10, 1)
        self.assertEqual(labels["slot"]["factual"], "supported")
        self.assertEqual(labels["slot"]["introduced"], "no")
        self.assertNotIn("action", labels["slot"])
        self.assertIn("action", errors["slot"])
        self.assertNotIn("factual", errors["slot"])
        self.assertNotIn("introduced", errors["slot"])
        self.assertEqual(record["cost_usd"], 0.25)
        self.assertFalse(record["billing_complete"])

    async def test_grade_captures_json_jsonl_and_cases_before_fixture_mutation(self):
        original_key = {"schema": 2, "items": [
            _finding("debt", None, actions=("defer",), introduced="no"),
            _finding("bug", "original-bug"),
        ]}
        replacement_key = {"schema": 2, "items": [
            _finding("replacement", None, factual="refuted", actions=("no_action",), introduced="no"),
        ]}
        bundles = {("local", 1): {"keys": {"original": original_key, "replacement": replacement_key},
                                  "public_identity": {"review/rubric.json": "original-rubric"}}}
        assertions = {
            "Original JSONL first": {
                "debt": _assertion(action="defer", introduced="yes"),
                "bug": _assertion(action="defer", introduced="no"),
            },
            "Original JSONL second": {
                "debt": _assertion(factual="refuted", action="defer", introduced="no"),
                "bug": _assertion(introduced="no"),
            },
            "Original JSON answer": {
                "debt": _assertion(factual="refuted", action="fix_now", introduced="no"),
                "bug": _assertion(factual="refuted"),
            },
        }
        extracted = []

        async def fake_judge(states, questions, timeout, concurrency):
            extracted.extend(states.values())
            labels = {sid: assertions.get(state["answer"], {}).get(state["id"], _assertion())
                      for sid, state in states.items()}
            return labels, {}, {"id": "snapshot-extraction", "cost_usd": 0.25, "cache": {}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial, source, cases = root / "captured.partial.jsonl", root / "captured.json", root / "cases.json"
            original_rows = [{"case": "outcome", "rep": rep, "answer": answer}
                             for rep, answer in enumerate(list(assertions)[:2], 1)]
            partial.write_text("\n".join(json.dumps(row) for row in original_rows) + "\n")
            original_meta = {"schema": 2, "strategy": "original-strategy", "model": "original-model",
                             "fixture_identities": {"outcome": {"key": "original"}}}
            source.write_text(json.dumps({**original_meta, "runs": [
                {"case": "outcome", "rep": 3, "answer": "Original JSON answer"},
            ]}))
            cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "original"}]}))
            original_bytes = {path: path.read_bytes() for path in (partial, source, cases)}
            original_hashes = {path: hashlib.sha256(raw).hexdigest() for path, raw in original_bytes.items()}
            key_hash = hashlib.sha256(json.dumps(original_key, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

            def mutate_during_fixture_build(specs, sources=None):
                mutated_rows = [{**row, "answer": "Mutated existing JSONL answer"} for row in original_rows]
                mutated_rows.append({"case": "outcome", "rep": 4, "answer": "Appended JSONL answer"})
                partial.write_text("\n".join(json.dumps(row) for row in mutated_rows) + "\n")
                source.write_text(json.dumps({"schema": 2, "strategy": "mutated-strategy", "model": "mutated-model",
                                               "fixture_identities": {"outcome": {"key": "replacement"}},
                                               "runs": [{"case": "outcome", "rep": 3, "answer": "Mutated JSON answer"}]}))
                cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "replacement"}]}))
                return bundles

            ledger_path = root / "capture.ledger.json"
            with mock.patch.object(grade, "_fixture_keys", side_effect=mutate_during_fixture_build), \
                    mock.patch.object(grade, "_judge", side_effect=fake_judge), \
                    mock.patch.object(grade, "_print_table"):
                results = await grade.grade([partial, source], cases, ledger_path=ledger_path)
            ledger = json.loads(ledger_path.read_text())
            for path in (partial, source, cases):
                self.assertNotEqual(path.read_bytes(), original_bytes[path])
                self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hashes[path])
            self.assertEqual(ledger["input_sha256"], {str(path): original_hashes[path] for path in (partial, source)})
            self.assertEqual(ledger["identities"]["cases_sha256"], original_hashes[cases])
            self.assertEqual(ledger["frozen_keys"], {"outcome": original_key})
            self.assertEqual(ledger["key_identities"]["outcome"]["key"], "original")
            self.assertEqual(ledger["key_identities"]["outcome"]["key_sha256"], key_hash)
            self.assertEqual(ledger["key_identities"]["outcome"]["public_identity"], bundles[("local", 1)]["public_identity"])
            self.assertEqual({state["answer"] for state in extracted}, set(assertions))
            self.assertEqual({state["id"] for state in extracted}, {"debt", "bug"})
            expected_rows = [original_rows, [{"case": "outcome", "rep": 3, "answer": "Original JSON answer"}]]
            expected_correct = {
                "Original JSONL first": {"factual": 2, "action": 1, "introduced": 0},
                "Original JSONL second": {"factual": 1, "action": 2, "introduced": 1},
                "Original JSON answer": {"factual": 0, "action": 1, "introduced": 2},
            }
            for path, result, inputs in zip((partial, source), results, expected_rows):
                persisted = json.loads(path.with_suffix(".graded.json").read_text())
                for report in (result, persisted):
                    self.assertEqual(report["input_sha256"], original_hashes[path])
                    self.assertEqual(report["identities"]["cases_sha256"], original_hashes[cases])
                    self.assertEqual(report["grading"]["ledger_id"], ledger["id"])
                    self.assertEqual(len(report["runs"]), len(inputs))
                    self.assertEqual([row["rep"] for row in report["runs"]], [row["rep"] for row in inputs])
                    for row, captured in zip(report["runs"], inputs):
                        self.assertEqual(row["key"], "original")
                        self.assertEqual(row["key_identity"], ledger["key_identities"]["outcome"])
                        self.assertEqual(row["answer_sha256"], hashlib.sha256(
                            json.dumps(captured["answer"], sort_keys=True, separators=(",", ":")).encode()).hexdigest())
                        self.assertEqual(row["verdicts"], assertions[captured["answer"]])
                        self.assertEqual(row["planned_items"], 2)
                        for facet, correct in expected_correct[captured["answer"]].items():
                            self.assertEqual(row["facets"][facet]["planned"], 2)
                            self.assertEqual(row["facets"][facet]["correct"], correct)
                            self.assertEqual(row["facets"][facet]["accuracy"], correct / 2)
                            self.assertEqual(row[f"{facet}_accuracy"], correct / 2)
                    if path == source:
                        self.assertEqual(report["execution"]["metadata"], original_meta)
                        self.assertEqual(report["strategy"], "original-strategy")
                        self.assertEqual(report["model"], "original-model")
                        self.assertEqual(report["runs"][0]["key_binding"], {
                            "captured": {"key": "original"}, "status": "matched"})

    async def test_grade_uses_captured_cases_when_disk_changes_during_read(self):
        original_key = {"schema": 2, "items": [
            _finding("original", None, actions=("defer",), introduced="no"),
        ]}
        replacement_key = {"schema": 2, "items": [
            _finding("replacement", "replacement-defect"),
        ]}
        bundles = {("local", 1): {"keys": {"original": original_key, "replacement": replacement_key},
                                  "public_identity": {}}}

        async def fake_judge(states, questions, timeout, concurrency):
            return {sid: _assertion(action="defer", introduced="no") for sid in states}, {}, {
                "id": "cases-race-extraction", "cost_usd": 0.25, "cache": {}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, cases, ledger_path = root / "captured.json", root / "cases.json", root / "cases.ledger.json"
            source.write_text(json.dumps({"schema": 2, "runs": [
                {"case": "outcome", "answer": "The original claim is supported but pre-existing; defer it."},
            ]}))
            cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "original"}]}))
            original_bytes = cases.read_bytes()
            original_hash = hashlib.sha256(original_bytes).hexdigest()
            original_read_bytes = Path.read_bytes

            def replace_after_read(path):
                captured = original_read_bytes(path)
                if path == cases:
                    cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "key": "replacement"}]}))
                return captured

            with mock.patch.object(Path, "read_bytes", new=replace_after_read), \
                    mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", side_effect=fake_judge), \
                    mock.patch.object(grade, "_print_table"):
                result = await grade.grade(source, cases, ledger_path=ledger_path)
            self.assertNotEqual(cases.read_bytes(), original_bytes)
            self.assertEqual(json.loads(cases.read_text())["cases"][0]["key"], "replacement")
            ledger = json.loads(ledger_path.read_text())
            self.assertEqual(ledger["identities"]["cases_sha256"], original_hash)
            self.assertEqual(ledger["frozen_keys"], {"outcome": original_key})
            self.assertEqual(ledger["key_identities"]["outcome"]["key"], "original")
            self.assertEqual(ledger["key_identities"]["outcome"]["key_sha256"], hashlib.sha256(
                json.dumps(original_key, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            for report in (result, json.loads(source.with_suffix(".graded.json").read_text())):
                self.assertEqual(report["identities"]["cases_sha256"], original_hash)
                self.assertEqual(len(report["runs"]), 1)
                row = report["runs"][0]
                self.assertEqual(row["key"], "original")
                self.assertEqual(row["key_identity"], ledger["key_identities"]["outcome"])
                self.assertEqual(row["verdicts"], {"original": _assertion(action="defer", introduced="no")})
                for facet in ("factual", "action", "introduced"):
                    self.assertEqual(row["facets"][facet]["planned"], 1)
                    self.assertEqual(row["facets"][facet]["correct"], 1)
                    self.assertEqual(row["facets"][facet]["accuracy"], 1.0)

    def test_load_runs_one_argument_reads_current_json_and_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, partial = root / "standalone.json", root / "standalone.partial.jsonl"
            original_rows = [{"case": "first", "rep": 1, "answer": "First captured answer"},
                             {"case": "second", "rep": 2, "answer": "Second captured answer"}]
            original_meta = {"schema": 2, "strategy": "original", "model": "local", "custom": {"retained": True}}
            source.write_text(json.dumps({**original_meta, "runs": original_rows}))
            partial.write_text("\n \t\n" + json.dumps(original_rows[0]) + "\n\n" + json.dumps(original_rows[1]) + "\n \n")
            self.assertEqual(grade._load_runs(source), (original_meta, original_rows))
            self.assertEqual(grade._load_runs(partial), ({"schema": None, "partial": True}, original_rows))
            replacement_rows = [{"case": "replacement", "rep": 3, "answer": "New standalone answer"}]
            replacement_meta = {"schema": 2, "strategy": "replacement", "custom": {"retained": False}}
            source.write_text(json.dumps({**replacement_meta, "runs": replacement_rows}))
            partial.write_text("\n\t\n" + json.dumps(replacement_rows[0]) + "\n\n")
            self.assertEqual(grade._load_runs(source), (replacement_meta, replacement_rows))
            self.assertEqual(grade._load_runs(partial), ({"schema": None, "partial": True}, replacement_rows))

    async def test_strategy_outputs_share_blind_extraction_cost_and_keep_key_schemas_separate(self):
        complete_answer = "The debt is supported but defer it; the introduced bug is supported and needs fixing."
        partial_answer = "Fix the introduced bug."
        key = {"schema": 2, "items": [
            _finding("debt", None, actions=("defer", "no_action"), introduced="no"),
            _finding("bug", "PRIVATE_CANONICAL_DEFECT"),
        ]}
        for item in key["items"]:
            item["rationale"] = "PRIVATE_KEY_RATIONALE"
        historical_key = {"schema": 1, "flag_label": "a historical concern", "items": [
            {"id": "historical-true", "desc": "Synthetic historical concern", "flag": True},
            {"id": "historical-false", "desc": "Synthetic historical nonconcern", "flag": False},
        ]}
        bundles = {("local", 1): {"keys": {"facets": key, "historical": historical_key},
                                  "public_identity": {}}}
        extractions = []

        async def fake_judge(states, questions, timeout, concurrency):
            facet_batch = "factual" in questions
            extractions.append((facet_batch, states))
            labels, errors = {}, {}
            for sid, state in states.items():
                # An allow-list enforces the privacy boundary, not extensible report shape.
                allowed = {"answer", "id", "item"} if facet_batch else {"answer", "id", "item", "flag_label"}
                self.assertEqual(set(state), allowed)
                if not facet_batch:
                    labels[sid] = {"verdict": "flagged" if state["id"] == "historical-true" else "cleared"}
                elif state["answer"] == complete_answer:
                    labels[sid] = (_assertion(action="defer", introduced="no")
                                   if state["id"] == "debt" else _assertion())
                elif state["id"] == "debt":
                    labels[sid] = _assertion(factual="omitted", action="omitted", introduced="omitted")
                else:
                    labels[sid] = {"action": "fix_now", "introduced": "yes"}
                    errors[sid] = {"factual": "invalid extraction"}
            record = {"id": "facet-batch" if facet_batch else "historical-batch",
                      "kind": "judgment_batch", "items": len(states), "model": "local-extractor",
                      "cost_usd": 0.25 if facet_batch else 0.125,
                      "cost_basis": "batch_status_estimate", "billing_complete": False,
                      "cache": {"cache": "miss", "cachedQuestions": 0}}
            return labels, errors, record

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [
                {"id": "outcome", "dataset": "local", "scale": 1, "key": "facets"},
                {"id": "historical", "dataset": "local", "scale": 1, "key": "historical"},
            ]}))
            flat_path, grouped_path = root / "flat.json", root / "grouped.json"
            flat_path.write_text(json.dumps({"schema": 2, "arm": "skill", "strategy": "jev-flat", "runs": [
                {"case": "outcome", "rep": 1, "answer": complete_answer, "usage": {"cost": 2.0}},
                {"case": "historical", "rep": 1, "answer": "Flag historical-true; clear historical-false."},
            ]}))
            grouped_path.write_text(json.dumps({"schema": 2, "arm": "none", "strategy": "jev-grouped", "runs": [
                {"case": "outcome", "rep": 1, "answer": partial_answer, "usage": {"cost": 4.0},
                 "adherence": {"status": "violated"}, "infra": True},
            ]}))
            ledger_path = root / "shared.ledger.json"
            with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", side_effect=fake_judge), \
                    mock.patch.object(grade, "_print_table"):
                flat, grouped = await grade.grade([flat_path, grouped_path], cases, ledger_path=ledger_path)

            # Actual written reports must point to one shared overhead artifact, not bill each arm.
            ledger = json.loads(ledger_path.read_text())
            for source, result in ((flat_path, flat), (grouped_path, grouped)):
                persisted = json.loads(source.with_suffix(".graded.json").read_text())
                self.assertEqual(persisted["grading"]["ledger_id"], ledger["id"])
                self.assertEqual(persisted["grading"]["ledger_path"], str(ledger_path))
                for field in ("cost_usd", "batch_cost", "batch_cost_estimate_usd", "batches"):
                    self.assertNotIn(field, persisted["grading"])
                    self.assertNotIn(field, result)
            self.assertEqual({batch["id"] for batch in ledger["batches"]}, {"facet-batch", "historical-batch"})
            self.assertEqual(len(ledger["batches"]), 2)
            self.assertEqual(ledger["batch_cost_estimate_usd"], 0.375)
            self.assertIsNone(ledger["session_outer_cost_usd"])
            self.assertIsNone(ledger["total_cost_usd"])
            self.assertFalse(ledger["billing_complete"])
            self.assertEqual(flat["execution"]["historical_outer_usage"]["0"]["cost"], 2.0)
            self.assertEqual(grouped["execution"]["historical_outer_usage"]["0"]["cost"], 4.0)

        blind_inputs = json.dumps(extractions)
        for private in ("PRIVATE_KEY_RATIONALE", "PRIVATE_CANONICAL_DEFECT", "jev-flat", "jev-grouped"):
            self.assertNotIn(private, blind_inputs)
        facet_batches = [states for facet_batch, states in extractions if facet_batch]
        self.assertEqual(len(facet_batches), 1)
        self.assertEqual({state["answer"] for state in facet_batches[0].values()}, {complete_answer, partial_answer})

        flat_outcome = next(row for row in flat["runs"] if row["case"] == "outcome")
        for facet in ("factual", "action", "introduced"):
            self.assertEqual(flat_outcome["facets"][facet]["planned"], 2)
            self.assertEqual(flat_outcome["facets"][facet]["accuracy"], 1.0)
        self.assertEqual(flat_outcome["repair"]["tp"], 1)
        self.assertEqual(flat_outcome["repair"]["fp"], 0)
        grouped_outcome = grouped["runs"][0]
        self.assertEqual(grouped_outcome["planned_items"], 2)
        self.assertEqual(grouped_outcome["factual_accuracy"], 0.0)
        self.assertEqual(grouped_outcome["action_accuracy"], 0.5)
        self.assertEqual(grouped_outcome["introduced_accuracy"], 0.5)
        self.assertEqual(grouped_outcome["facets"]["factual"]["omitted"], 1)
        self.assertEqual(grouped_outcome["facets"]["factual"]["unresolved"], 1)
        self.assertEqual(grouped_outcome["judge_errors"], 1)
        self.assertEqual(grouped_outcome["repair"]["tp"], 0)
        self.assertEqual(grouped_outcome["repair"]["fp"], 1)
        self.assertEqual(grouped_outcome["repair"]["fn"], 1)

        separated = flat["summary"]["overall"]["by_key_schema"]
        self.assertEqual(set(separated), {"1", "2"})
        self.assertEqual(separated["1"]["n"], 1)
        self.assertEqual(separated["1"]["accuracy"], 1.0)
        self.assertNotIn("factual_accuracy", separated["1"])
        self.assertEqual(separated["2"]["n"], 1)
        self.assertEqual(separated["2"]["factual_accuracy"], 1.0)
        self.assertNotIn("accuracy", separated["2"])
        self.assertEqual(grouped["summary"]["overall"]["by_key_schema"]["2"]["factual_accuracy"], 0.0)
        self.assertEqual(set(flat["summary"]["per_case"]["outcome"]["by_key_schema"]), {"2"})
        self.assertEqual(set(flat["summary"]["per_case"]["historical"]["by_key_schema"]), {"1"})


if __name__ == "__main__":
    unittest.main()
