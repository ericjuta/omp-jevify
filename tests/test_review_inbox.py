import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "jevify" / "jevify.py"
namespace = {}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(SKILL_SOURCE.read_text(encoding="utf-8"), str(SKILL_SOURCE), "exec"), namespace)
jv = namespace["jv"]
JvRows = namespace["_JvRows"]

QUESTION_SOURCE = 'Q = {"severity": jv.choice("How severe is the defect?", low="minor", high="blocking")}'
_scope = {"jv": jv}
exec(QUESTION_SOURCE, _scope)
Q = _scope["Q"]
POLICY = jv.policy(Q, {"severity": {"top": 0.5}})
HUMAN = {"kind": "human", "id": "reviewer-1"}
CITE_F0 = {"file": "many.py", "start": 1, "end": 2}

# Child processes exec the same skill file and the same rubric source, so their rubric identity matches.
CHILD_PRELUDE = (
    "import contextlib, io, json, sys\n"
    "from pathlib import Path\n"
    "source = Path(sys.argv[1])\n"
    "namespace = {}\n"
    "with contextlib.redirect_stdout(io.StringIO()):\n"
    "    exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), namespace)\n"
    "jv = namespace['jv']\n"
    + QUESTION_SOURCE + "\n"
)
STATUS_BODY = "print(json.dumps(jv.review_status(sys.argv[2], Q=Q)))\n"
DECIDE_BODY = (
    "try:\n"
    "    result = jv.adjudicate(sys.argv[2], json.loads(sys.argv[3]), Q=Q, reviewer=json.loads(sys.argv[4]))\n"
    "    print(json.dumps({'ok': True, 'batch': result['batch']}))\n"
    "except jv.ReviewRefused as refusal:\n"
    "    print(json.dumps({'ok': False, 'reasons': [problem['reason'] for problem in refusal.problems]}))\n"
)


def _spawn(body, *args):
    return subprocess.Popen([sys.executable, "-c", CHILD_PRELUDE + body, str(SKILL_SOURCE), *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT))


def _spawn_all(calls):
    """Start every child before any is settled, so they run concurrently. calls: [(body, args)]."""
    started = []
    try:
        for body, args in calls:
            started.append(_spawn(body, *args))
    except BaseException:
        _settle(started)
        raise
    return started


def _settle(processes):
    """Communicate with each child exactly once and reap it, even when an earlier child times out or a later
    assertion fails. Returns (returncode, stdout, stderr) per child, in order."""
    settled = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=120)
            settled.append((process.returncode, out, err))
    finally:
        for process in processes[len(settled):]:
            if process.poll() is None:
                process.kill()
            process.communicate()
    return settled


def _finish_all(processes):
    """Settle every child before judging any, so one failing child cannot leak its siblings' pipes."""
    decoded = []
    for returncode, out, err in _settle(processes):
        if returncode != 0:
            raise AssertionError(err)
        decoded.append(json.loads(out.strip().splitlines()[-1]))
    return decoded


def _finish(process):
    return _finish_all([process])[0]


def _scratch(testcase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    return Path(temporary.name)


def _many_source(root):
    (root / "many.py").write_text("".join(f"def f{i}():\n    return {i}\n" for i in range(8)), encoding="utf-8")


def _many_units(count=8):
    return {f"u{i}": {"file": "many.py", "line": 2 * i + 1, "end_line": 2 * i + 2} for i in range(count)}


def _items(count=8):
    return {f"f{i}": {"unit": f"u{i}", "claim": f"f{i} returns {i}", "question": "severity"}
            for i in range(count)}


def _run_rows(manifest, *, count=8, receipt_qhash=None, receipt_digest=None, row_qhash=None, error_on=None):
    """A synthetic run: one row per unit, answering severity under the given hashes. Not a model result."""
    rows = []
    for i in range(count):
        row = {"id": f"u{i}", "qhash": row_qhash or jv.qhash(Q)}
        if error_on == f"u{i}":
            row["error"] = "provider failed"
        else:
            row.update({"model": "test-model", "batch_id": "b1", "severity": "low",
                        "severity.low": 0.8, "severity.high": 0.2, "severity.conf": 0.6})
        rows.append(row)
    receipt = {"qhash": receipt_qhash or jv.qhash(Q), "complete": True,
               "extraction": {"digest": receipt_digest or manifest["digest"]}}
    return JvRows(rows, receipt=receipt)


class ReviewInboxTests(unittest.TestCase):
    def setUp(self):
        self.root = _scratch(self)
        self.home = _scratch(self)
        self.path = self.home / "review"
        _many_source(self.root)
        self.units = _many_units()
        self.manifest = jv.extraction(self.units, {uid: uid for uid in self.units}, root=self.root)
        self.rows = _run_rows(self.manifest)
        jv.review_queue(self.path, self.manifest, _items(), Q=Q, policy=POLICY, root=self.root, rows=self.rows)

    def _bindings(self, limit=8):
        return jv.review_questions(self.path, Q=Q, limit=limit)["bindings"]

    def _decision(self, item_id, binding, verdict="confirmed"):
        evidence = [] if verdict == "unresolved" else [binding["citation"]]
        return {"item": item_id, "fingerprint": binding["fingerprint"], "view": binding["view"],
                "verdict": verdict, "evidence": evidence}

    def test_questions_are_bounded_unrecommended_and_show_the_frozen_excerpt(self):
        page = jv.review_questions(self.path, Q=Q, limit=3)
        self.assertEqual([question["id"] for question in page["questions"]], ["f0", "f1", "f2"])
        self.assertEqual(page["open"], 8)
        first = page["questions"][0]
        self.assertNotIn("recommended", first)
        self.assertEqual([option["label"] for option in first["options"]],
                         ["confirmed", "overturned", "unresolved"])
        self.assertIn("def f0():\n    return 0", first["question"])
        self.assertIn('(model "test-model", row qhash', first["question"])
        self.assertIn('): "low"', first["question"])
        self.assertIn("top_probability", first["question"])
        self.assertEqual(page["bindings"]["f0"]["citation"], CITE_F0)
        with self.assertRaises(ValueError):
            jv.review_questions(self.path, Q=Q, limit=0)

    def test_model_verdict_is_derived_from_the_bound_row_with_its_provenance(self):
        item = {entry["item"]: entry for entry in jv.review_items(self.path, Q=Q)}["f0"]
        self.assertEqual(item["model_verdict"], "low")
        self.assertEqual(item["model"], {"source": "run_row", "model": "test-model",
                                         "row_qhash": jv.qhash(Q), "batch_id": "b1"})
        self.assertEqual(item["uncertainty"]["host_confidence"], 0.6)

    def test_items_without_a_question_keep_model_fields_unknown(self):
        bare = self.home / "bare"
        jv.review_queue(bare, self.manifest, {"g0": {"unit": "u0", "claim": "bare claim"}},
                        Q=Q, policy=POLICY, root=self.root)
        question = jv.review_questions(bare, Q=Q)["questions"][0]["question"]
        self.assertIn("Model verdict: unknown (no run row bound to this item)", question)
        self.assertIn("Model uncertainty: unknown", question)
        item = jv.review_items(bare, Q=Q)[0]
        self.assertIsNone(item["model_verdict"])
        self.assertIsNone(item["model"])

    def test_run_rows_bind_only_to_their_question_hash_extraction_unit_and_success(self):
        cases = {
            "other-rubric": _run_rows(self.manifest, receipt_qhash="0" * 12),
            "other-extraction": _run_rows(self.manifest, receipt_digest="f" * 64),
            "row-other-rubric": _run_rows(self.manifest, row_qhash="0" * 12),
            "error-row": _run_rows(self.manifest, error_on="u0"),
            "missing-rows": _run_rows(self.manifest, count=0),
        }
        for label, rows in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                jv.review_queue(self.home / label, self.manifest, _items(), Q=Q, policy=POLICY,
                                root=self.root, rows=rows)

    def test_caller_declared_model_values_and_questions_without_rows_are_refused(self):
        with self.assertRaises(ValueError):
            jv.review_queue(self.home / "declared", self.manifest,
                            {"g0": {"unit": "u0", "claim": "c", "model_verdict": "low"}},
                            Q=Q, policy=POLICY, root=self.root, rows=self.rows)
        with self.assertRaises(ValueError):
            jv.review_queue(self.home / "unbound", self.manifest,
                            {"g0": {"unit": "u0", "claim": "c", "question": "severity"}},
                            Q=Q, policy=POLICY, root=self.root)

    def test_legacy_review_dicts_are_refused_explicitly(self):
        legacy = {"schema": "jevify-review/1", "items": {"f0": {"verdict": "pending"}}}
        with self.assertRaises(ValueError):
            jv.review_items(legacy, Q=Q)
        with self.assertRaises(ValueError):
            jv.review_status(legacy, Q=Q)
        with self.assertRaises(ValueError):
            jv.adjudicate(legacy, [{"item": "f0", "fingerprint": "x", "view": "y", "verdict": "confirmed",
                                    "evidence": []}], Q=Q, reviewer=HUMAN)

    def test_mixed_validity_batch_appends_nothing(self):
        bindings = self._bindings()
        batch = [self._decision("f0", bindings["f0"]), self._decision("f1", bindings["f1"], "maybe")]
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(self.path, batch, Q=Q, reviewer=HUMAN)
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["invalid_decision"])
        status = jv.review_status(self.path, Q=Q)
        self.assertEqual(status["confirmed"], [])
        self.assertEqual(len(status["pending"]), 8)
        self.assertFalse((self.path / "events.jsonl").exists())

    def test_a_stale_view_after_another_decision_is_superseded_not_drift(self):
        bindings = self._bindings()
        jv.adjudicate(self.path, [self._decision("f0", bindings["f0"])], Q=Q, reviewer=HUMAN)
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(self.path, [self._decision("f0", bindings["f0"], "overturned")], Q=Q, reviewer=HUMAN)
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["superseded"])
        status = jv.review_status(self.path, Q=Q)
        self.assertEqual(status["stale"], {})
        self.assertEqual(status["drift"], {"rubric": False, "policy": False, "source": []})
        self.assertEqual(len(jv.review_items(self.path, Q=Q)[0]["history"]), 1)

    def test_agent_cannot_replace_a_human_decision_but_a_human_can_revise_it(self):
        binding = self._bindings(limit=1)["f0"]
        jv.adjudicate(self.path, [self._decision("f0", binding)], Q=Q, reviewer=HUMAN)
        current = jv.review_items(self.path, Q=Q)[0]
        revision = {"item": "f0", "fingerprint": current["fingerprint"], "view": current["view"],
                    "verdict": "overturned", "evidence": [CITE_F0]}
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(self.path, [revision], Q=Q, reviewer={"kind": "agent", "id": "bot"})
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["protected_decision"])
        jv.adjudicate(self.path, [revision], Q=Q, reviewer=HUMAN)
        history = jv.review_items(self.path, Q=Q)[0]["history"]
        self.assertEqual([entry["verdict"] for entry in history], ["confirmed", "overturned"])

    def test_reopen_in_a_separate_process_keeps_history_and_reviewer_kind(self):
        binding = self._bindings(limit=1)["f0"]
        decided = _finish(_spawn(DECIDE_BODY, self.path, json.dumps([self._decision("f0", binding)]),
                                 json.dumps({"kind": "agent", "id": "audit-bot"})))
        self.assertEqual(decided, {"ok": True, "batch": 1})
        self.assertEqual(_finish(_spawn(STATUS_BODY, self.path))["confirmed"], ["f0"])
        history = jv.review_items(self.path, Q=Q)[0]["history"][0]
        self.assertEqual(history["reviewer"], {"kind": "agent", "id": "audit-bot"})
        self.assertEqual(history["identity"], "self_declared")
        self.assertIsInstance(history["at"], float)

    def test_concurrent_processes_on_distinct_items_each_land_once(self):
        bindings = self._bindings()
        processes = _spawn_all([(DECIDE_BODY, (self.path, json.dumps([self._decision(item, binding)]),
                                               json.dumps({"kind": "agent", "id": f"worker-{index}"})))
                                for index, (item, binding) in enumerate(sorted(bindings.items()))])
        results = _finish_all(processes)
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(sorted(result["batch"] for result in results), list(range(1, 9)))
        self.assertEqual(len(jv.review_status(self.path, Q=Q)["confirmed"]), 8)
        lines = (self.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 8)
        for line in lines:
            json.loads(line)

    def test_racing_processes_on_one_item_leave_exactly_one_decision(self):
        payload = json.dumps([self._decision("f0", self._bindings(limit=1)["f0"])])
        processes = _spawn_all([(DECIDE_BODY, (self.path, payload, json.dumps({"kind": "human", "id": f"rev-{index}"})))
                                for index in range(2)])
        results = _finish_all(processes)
        winners = [result for result in results if result["ok"]]
        losers = [result for result in results if not result["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(losers[0]["reasons"], ["superseded"])
        self.assertEqual(len(jv.review_items(self.path, Q=Q)[0]["history"]), 1)

    def test_torn_journal_tail_is_discarded_and_complete_decisions_survive(self):
        binding = self._bindings(limit=1)["f0"]
        jv.adjudicate(self.path, [self._decision("f0", binding)], Q=Q, reviewer=HUMAN)
        with open(self.path / "events.jsonl", "ab") as journal:
            journal.write(b'{"event":"decisions","batch":2,"reviewer"')
        status = jv.review_status(self.path, Q=Q)
        self.assertEqual(status["confirmed"], ["f0"])
        self.assertEqual(status["batches"], 1)
        self.assertTrue((self.path / "events.jsonl").read_bytes().endswith(b"\n"))

    def test_corrupt_complete_journal_record_fails_safe(self):
        with open(self.path / "events.jsonl", "ab") as journal:
            journal.write(b"not json\n")
        with self.assertRaises(ValueError):
            jv.review_status(self.path, Q=Q)

    def test_source_drift_stales_only_items_whose_excerpt_changed(self):
        (self.root / "other.py").write_text("x = 1\n", encoding="utf-8")
        units = {"o0": {"file": "other.py", "line": 1, "end_line": 1}, "u1": dict(self.units["u1"])}
        manifest = jv.extraction(units, {uid: uid for uid in units}, root=self.root)
        path = self.home / "drift"
        jv.review_queue(path, manifest,
                        {"p0": {"unit": "o0", "claim": "other is trivial"},
                         "p1": {"unit": "u1", "claim": "u1 returns 1"}},
                        Q=Q, policy=POLICY, root=self.root)
        before = jv.review_questions(path, Q=Q)["bindings"]
        (self.root / "other.py").write_text("x = 2\n", encoding="utf-8")
        page = jv.review_questions(path, Q=Q)
        self.assertEqual([question["id"] for question in page["questions"]], ["p1"])
        self.assertEqual(page["stale"], {"p0": "stale_source"})
        status = jv.review_status(path, Q=Q)
        self.assertEqual(status["drift"]["source"], ["other.py"])
        self.assertFalse(status["settled"])
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(path, [self._decision("p0", before["p0"])], Q=Q, reviewer=HUMAN)
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["stale_source"])
        jv.adjudicate(path, [self._decision("p1", before["p1"])], Q=Q, reviewer=HUMAN)
        self.assertEqual(jv.review_status(path, Q=Q)["confirmed"], ["p1"])

    def test_source_change_reports_every_prior_confirmation_as_stale_not_an_empty_queue(self):
        bindings = self._bindings()
        jv.adjudicate(self.path, [self._decision(item, binding) for item, binding in sorted(bindings.items())],
                      Q=Q, reviewer=HUMAN)
        self.assertEqual(jv.review_questions(self.path, Q=Q)["questions"], [])
        (self.root / "many.py").write_text("".join(f"def h{i}():\n    return {i}\n" for i in range(8)),
                                           encoding="utf-8")
        page = jv.review_questions(self.path, Q=Q)
        self.assertEqual(page["questions"], [])
        self.assertEqual(page["stale"], {f"f{i}": "stale_source" for i in range(8)})
        status = jv.review_status(self.path, Q=Q)
        self.assertEqual(status["approved_units"], [])
        self.assertFalse(status["settled"])

    def test_pending_items_are_asked_before_items_answered_unresolved(self):
        bindings = self._bindings()
        jv.adjudicate(self.path, [self._decision(item, bindings[item], "unresolved") for item in ("f0", "f1", "f2")],
                      Q=Q, reviewer=HUMAN)
        page = jv.review_questions(self.path, Q=Q, limit=3)
        self.assertEqual([question["id"] for question in page["questions"]], ["f3", "f4", "f5"])
        pending = jv.review_questions(self.path, Q=Q, limit=8)["bindings"]
        jv.adjudicate(self.path, [self._decision(item, pending[item]) for item in ("f3", "f4", "f5", "f6", "f7")],
                      Q=Q, reviewer=HUMAN)
        self.assertEqual([question["id"] for question in jv.review_questions(self.path, Q=Q, limit=5)["questions"]],
                         ["f0", "f1", "f2"])

    def test_confirmation_citing_another_file_goes_stale_and_is_redecided_with_history_kept(self):
        (self.root / "other.py").write_text("x = 1\n", encoding="utf-8")
        units = {"o0": {"file": "other.py", "line": 1, "end_line": 1}, "u0": dict(self.units["u0"])}
        manifest = jv.extraction(units, {uid: uid for uid in units}, root=self.root)
        path = self.home / "cited"
        jv.review_queue(path, manifest, {"c0": {"unit": "u0", "claim": "u0 returns 0"}},
                        Q=Q, policy=POLICY, root=self.root)
        binding = jv.review_questions(path, Q=Q)["bindings"]["c0"]
        cited = {"file": "other.py", "start": 1, "end": 1}
        jv.adjudicate(path, [{"item": "c0", "fingerprint": binding["fingerprint"], "view": binding["view"],
                              "verdict": "confirmed", "evidence": [cited]}], Q=Q, reviewer=HUMAN)
        self.assertEqual(jv.review_status(path, Q=Q)["approved_units"], ["u0"])
        (self.root / "other.py").write_text("x = 2\n", encoding="utf-8")
        page = jv.review_questions(path, Q=Q)
        self.assertEqual(page["stale"], {"c0": "stale_evidence"})
        self.assertEqual([question["id"] for question in page["questions"]], ["c0"])
        self.assertIn("no longer counts", page["questions"][0]["question"])
        status = jv.review_status(path, Q=Q)
        self.assertEqual(status["confirmed"], ["c0"])
        self.assertEqual(status["approved_units"], [])
        self.assertFalse(status["settled"])
        redecided = page["bindings"]["c0"]
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(path, [{"item": "c0", "fingerprint": redecided["fingerprint"], "view": redecided["view"],
                                  "verdict": "confirmed", "evidence": [cited]}], Q=Q, reviewer=HUMAN)
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["stale_source"])
        jv.adjudicate(path, [{"item": "c0", "fingerprint": redecided["fingerprint"], "view": redecided["view"],
                              "verdict": "confirmed", "evidence": [redecided["citation"]]}], Q=Q, reviewer=HUMAN)
        status = jv.review_status(path, Q=Q)
        self.assertEqual(status["approved_units"], ["u0"])
        self.assertEqual(status["stale"], {})
        self.assertEqual([entry["verdict"] for entry in jv.review_items(path, Q=Q)[0]["history"]],
                         ["confirmed", "confirmed"])

    def test_settled_review_with_no_confirmation_grants_no_deletion(self):
        bindings = self._bindings()
        jv.adjudicate(self.path, [self._decision(item, binding, "overturned")
                                  for item, binding in sorted(bindings.items())], Q=Q, reviewer=HUMAN)
        status = jv.review_status(self.path, Q=Q)
        self.assertTrue(status["settled"])
        self.assertFalse(status["grants_authority"])
        self.assertEqual(status["approved_units"], [])
        with self.assertRaises(ValueError) as caught, contextlib.redirect_stdout(io.StringIO()):
            jv.delete_spans(self.units, ["u0"], root=self.root, manifest=self.manifest, review=self.path, Q=Q)
        self.assertIn("lacks a confirmed review decision", str(caught.exception))

    def test_rubric_drift_refuses_questions_and_every_decision(self):
        reworded = {"severity": jv.choice("Reworded question?", low="minor", high="blocking")}
        binding = self._bindings(limit=1)["f0"]
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.review_questions(self.path, Q=reworded)
        self.assertEqual(caught.exception.problems[0]["reason"], "stale_rubric")
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(self.path, [self._decision("f0", binding)], Q=reworded, reviewer=HUMAN)
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["stale_rubric"])
        status = jv.review_status(self.path, Q=reworded)
        self.assertTrue(status["drift"]["rubric"])
        self.assertFalse(status["settled"])

    def test_policy_edited_on_disk_is_drift_and_refuses_decisions(self):
        binding = self._bindings(limit=1)["f0"]
        plan_path = self.path / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["policy"]["questions"]["severity"]["top"] = 0.99
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        self.assertTrue(jv.review_status(self.path, Q=Q)["drift"]["policy"])
        with self.assertRaises(jv.ReviewRefused) as caught:
            jv.adjudicate(self.path, [self._decision("f0", binding)], Q=Q, reviewer=HUMAN)
        self.assertEqual([problem["reason"] for problem in caught.exception.problems], ["stale_policy"])

    def test_plan_tamper_outside_the_policy_is_refused(self):
        plan_path = self.path / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["items"]["f0"]["claim"] = "a changed claim"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        with self.assertRaises(ValueError):
            jv.review_items(self.path, Q=Q)

    def test_reviewer_identity_is_validated_and_recorded_as_self_declared(self):
        decision = [self._decision("f0", self._bindings(limit=1)["f0"])]
        for reviewer in ({"kind": "robot", "id": "x"}, {"kind": "human", "id": "  "}, {"kind": "human"}, "human"):
            with self.assertRaises(ValueError):
                jv.adjudicate(self.path, decision, Q=Q, reviewer=reviewer)
        jv.adjudicate(self.path, decision, Q=Q, reviewer={"kind": "agent", "id": "model-7"})
        history = jv.review_items(self.path, Q=Q)[0]["history"]
        self.assertEqual(history[0]["reviewer"], {"kind": "agent", "id": "model-7"})
        self.assertEqual(history[0]["identity"], "self_declared")

    def test_identical_identity_reopens_and_a_different_identity_or_run_is_refused(self):
        status = jv.review_queue(self.path, self.manifest, _items(), Q=Q, policy=POLICY, root=self.root,
                                 rows=self.rows)
        self.assertEqual(status["items"], 8)
        changed = {**_items(), "f0": {**_items()["f0"], "claim": "different"}}
        with self.assertRaises(ValueError):
            jv.review_queue(self.path, self.manifest, changed, Q=Q, policy=POLICY, root=self.root, rows=self.rows)
        with self.assertRaises(ValueError):
            jv.review_queue(self.path, self.manifest, _items(), Q=Q, policy=POLICY, root=self.root)

    def test_excerpt_beyond_the_evidence_bound_is_refused(self):
        (self.root / "long.py").write_text("x = 1\n" * 401, encoding="utf-8")
        manifest = jv.extraction({"long0": {"file": "long.py", "line": 1, "end_line": 401}},
                                 {"long0": "long0"}, root=self.root)
        with self.assertRaises(ValueError):
            jv.review_queue(self.home / "long", manifest, {"l": {"unit": "long0", "claim": "c"}},
                            Q=Q, policy=POLICY, root=self.root)

    def test_delete_refuses_while_any_item_is_pending_or_unresolved(self):
        bindings = self._bindings()
        jv.adjudicate(self.path, [self._decision("f0", bindings["f0"])], Q=Q, reviewer=HUMAN)
        jv.adjudicate(self.path, [self._decision("f1", bindings["f1"], "unresolved")], Q=Q, reviewer=HUMAN)
        with self.assertRaises(ValueError) as caught, contextlib.redirect_stdout(io.StringIO()):
            jv.delete_spans(self.units, ["u0"], root=self.root, manifest=self.manifest, review=self.path, Q=Q)
        self.assertIn("pending or unresolved", str(caught.exception))

    def test_two_process_drift_smoke_refuses_a_decision_made_before_a_source_edit(self):
        binding = self._bindings(limit=1)["f0"]
        (self.root / "many.py").write_text("".join(f"def g{i}():\n    return {i}\n" for i in range(8)),
                                           encoding="utf-8")
        result = _finish(_spawn(DECIDE_BODY, self.path, json.dumps([self._decision("f0", binding)]),
                                json.dumps(HUMAN)))
        self.assertEqual(result, {"ok": False, "reasons": ["stale_source"]})
        self.assertEqual(jv.review_status(self.path, Q=Q)["confirmed"], [])


if __name__ == "__main__":
    unittest.main()
