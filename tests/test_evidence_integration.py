"""Consumer-visible evidence/inbox and immutable-source boundaries."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "skills" / "jevify" / "jevify.py"
namespace = {}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(HELPER.read_text(), str(HELPER), "exec"), namespace)
jv = namespace["jv"]


class EvidenceInboxTests(unittest.TestCase):
    def test_adopted_judgment_is_reviewable_only_for_exact_source_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "code.py"
            source.write_text("raise RetryError()\n")
            units = {"c1": {"file": "code.py", "line": 1, "end_line": 1}}
            states = {"c1": "raise RetryError()"}
            manifest = jv.extraction(units, states, root=root)
            questions = {"valid": jv.yes("Does the handler retain the retry failure?")}
            with contextlib.redirect_stdout(io.StringIO()):
                rows = jv.adopt({"c1": {"valid": {"type": "bool", "bool": 0.9}}},
                                states, questions, extraction=manifest)
            result = jv.review_queue(root / "review", manifest,
                                     {"c1": {"unit": "c1", "claim": "Retains errors", "question": "valid"}},
                                     Q=questions, policy=jv.policy(questions, {"valid": {"band": [0.2, 0.8]}}),
                                     rows=rows)
            self.assertEqual(result["pending"], ["c1"])
            self.assertEqual(jv.review_items(root / "review", Q=questions)[0]["model_verdict"], 0.9)
            source.write_text("return None\n")
            with self.assertRaises(ValueError):
                jv.adopt({"c1": {"valid": {"type": "bool", "bool": 0.9}}},
                         states, questions, extraction=manifest)

    def test_contract_only_change_invalidates_confirmed_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "comment.txt").write_text("Handler must retain the retry error.\n")
            contract = root / "contract.txt"
            contract.write_text("Retain retry errors.\n")
            evidence = jv.prepare_evidence(
                [{"id": "c1", "source_id": "comment", "required_fields": [],
                  "required_questions": [], "dependencies": ["contract"]}],
                {}, root=root,
                sources=[{"id": "comment", "role": "original_comment", "path": "comment.txt"},
                         {"id": "contract", "role": "governing_contract", "path": "contract.txt"}])
            units = {"c1": {"file": "comment.txt", "line": 1, "end_line": 1}}
            manifest = jv.extraction(units, {"c1": "Handler must retain the retry error."}, root=root)
            questions = {"valid": jv.yes("Is the claim supported?")}
            policy = jv.policy(questions, {"valid": {"band": [0.2, 0.8]}})
            inbox = root / "inbox"
            jv.review_queue(inbox, manifest, {"c1": {"unit": "c1", "claim": "Retain errors"}},
                            Q=questions, policy=policy, evidence=evidence)
            binding = jv.review_questions(inbox, Q=questions)["bindings"]["c1"]
            decision = {"item": "c1", "fingerprint": binding["fingerprint"],
                        "view": binding["view"], "verdict": "confirmed", "evidence": [binding["citation"]]}
            jv.adjudicate(inbox, [decision], Q=questions, reviewer={"kind": "human", "id": "operator"})
            self.assertEqual(jv.review_status(inbox, Q=questions)["approved_units"], ["c1"])
            contract.write_text("Retry errors may be discarded.\n")
            status = jv.review_status(inbox, Q=questions)
            self.assertEqual(status["stale"], {"c1": "stale_source"})
            self.assertEqual(status["approved_units"], [])
            self.assertFalse(status["grants_authority"])
            with self.assertRaises(jv.ReviewRefused):
                jv.adjudicate(inbox, [decision], Q=questions,
                              reviewer={"kind": "human", "id": "operator"})


class EvidenceAdmissionRegressionTests(unittest.TestCase):
    def test_unrelated_drift_never_unblocks_missing_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {"current.py": "retry()\n", "a.md": "Contract A.\n", "b.md": "Contract B.\n"}.items():
                (root / name).write_text(text)
            sources = [
                {"id": "comment", "role": "original_comment", "text": "Retry changed.\n", "snapshot_identity": "comment"},
                {"id": "code", "role": "current_code", "path": "current.py"},
                {"id": "a-contract", "role": "governing_contract", "path": "a.md"},
                {"id": "b-contract", "role": "governing_contract", "path": "b.md"},
            ]
            records = [{"id": uid, "source_id": "comment", "claim": "Retry changed.",
                        "required_questions": [question],
                        "dependencies": {"current_code": ["code"], "governing_contract": [contract]}}
                       for uid, question, contract in [("a", "introduced_by_diff", "a-contract"),
                                                       ("b", "triage", "b-contract")]]
            evidence = jv.prepare_review(records, root=root, sources=sources)
            questions = {"introduced_by_diff": jv.yes("Was this introduced?"), "triage": jv.yes("Is this actionable?")}
            with contextlib.redirect_stdout(io.StringIO()):
                workflow = jv.workflow_review(evidence, questions)
            inbox = root / "inbox"
            jv.review_queue(inbox, workflow["manifest"], workflow["items"], Q=questions,
                            policy=jv.policy(questions, {"triage": {"band": [0.2, 0.8]}}))
            blocked = next(item for item in jv.review_items(inbox, Q=questions) if item["unit"] == "a")
            self.assertEqual(blocked["stale"], "missing_evidence")
            (root / "b.md").write_text("Changed unrelated contract.\n")
            status = jv.review_status(inbox, Q=questions)
            self.assertEqual(status["stale"][blocked["item"]], "missing_evidence")
            citation = {key: blocked["excerpt"][key] for key in ("file", "start", "end")}
            with self.assertRaises(jv.ReviewRefused):
                jv.adjudicate(inbox, [{"item": blocked["item"], "fingerprint": blocked["fingerprint"],
                                      "view": blocked["view"], "verdict": "confirmed", "evidence": [citation]}],
                              Q=questions, reviewer={"kind": "human", "id": "operator"})
            self.assertNotIn("a", jv.review_status(inbox, Q=questions)["approved_units"])

    def test_identical_file_bytes_do_not_allow_wrong_locator_or_section(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "Section A promise.\nSection B promise.\n"
            (root / "docs.md").write_text(text)
            (root / "alias.md").write_text(text)
            evidence = jv.prepare_evidence(
                [{"id": "section-a", "source_id": "docs", "required_fields": [], "required_questions": [],
                  "facts": {"source_span": {"line": 1, "end_line": 1}}}], {}, root=root,
                sources=[{"id": "docs", "role": "original_comment", "path": "docs.md"}])
            questions = {"valid": jv.yes("Is section A supported?")}
            for filename, line, reason in [("docs.md", 2, "frozen original span"),
                                           ("alias.md", 1, "different source locator")]:
                with self.subTest(filename=filename):
                    units = {"section-a": {"file": filename, "line": line, "end_line": line}}
                    manifest = jv.extraction(units, {"section-a": "claim"}, root=root, evidence=evidence)
                    with self.assertRaisesRegex(ValueError, reason):
                        jv.review_queue(root / "inbox", manifest,
                                        {"item": {"unit": "section-a", "claim": "Section A promise"}},
                                        Q=questions, policy=jv.policy(questions, {"valid": {"band": [0.2, 0.8]}}))


class EvidenceDeletionTests(unittest.TestCase):
    def test_imported_comment_cannot_be_deleted_without_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "comment.txt"
            source.write_text("do not remove this comment\n")
            unit = {"file": "comment.txt", "line": 1, "end_line": 1,
                    "role": "original_comment"}
            with self.assertRaisesRegex(ValueError, "imported/read-only"):
                jv.delete_spans({"c1": unit}, ["c1"], root=root, dry_run=False)
            self.assertEqual(source.read_text(), "do not remove this comment\n")

    def test_snapshot_path_is_protected_even_if_role_marker_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = root / ".jevify-evidence" / "snapshots"
            snapshots.mkdir(parents=True)
            source = snapshots / "captured.utf8"
            source.write_text("immutable evidence\n")
            unit = {"file": str(source.relative_to(root)), "line": 1, "end_line": 1}
            with self.assertRaisesRegex(ValueError, "immutable evidence"):
                jv.delete_spans({"c1": unit}, ["c1"], root=root, dry_run=False)
            self.assertEqual(source.read_text(), "immutable evidence\n")
