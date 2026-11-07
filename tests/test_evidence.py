"""Provider-free evidence contract acceptance and adversarial boundary regressions."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "skills" / "jevify" / "evidence.py"
spec = importlib.util.spec_from_file_location("jevify_evidence_tests", MODULE)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class EvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.comment = "Heading\r\nA 😀 quoted issue\r\nquoted issue again\n"
        (self.root / "current.py").write_bytes(b"def current():\r\n    return 1\r\n")
        (self.root / "contract.md").write_bytes(b"The current function must return one.\n")
        self.sources = [
            {"id": "comment", "role": "original_comment", "text": self.comment,
             "snapshot_identity": "review-123/comment-1", "origin": "https://example.invalid/review/123",
             "subject": "current", "version": "r1"},
            {"id": "code", "role": "current_code", "path": "current.py", "subject": "current", "version": "r1"},
            {"id": "contract", "role": "governing_contract", "path": "contract.md"},
        ]
        self.records = [{"id": "comment-1", "source_id": "comment", "required_fields": ["quote", "claim"],
                         "required_questions": ["valid", "severity"], "dependencies": ["code", "contract"],
                         "subject": "current", "version": "r1", "facts": {"has_code": True}}]
        self.fields = {"quote": {"source_roles": ["original_comment"]}, "claim": {}}

    def prepare(self, **kwargs):
        inputs = dict(records=self.records, fields=self.fields, root=self.root, sources=self.sources)
        inputs.update(kwargs)
        return evidence.prepare_evidence(**inputs)

    def candidate(self, value="😀 quoted issue", *, source_id="comment", start=None, end=None):
        if start is None and end is None:
            start = self.comment.index(value)
            end = start + len(value)
        return {"status": "candidate", "value": value, "source_id": source_id,
                "start": start, "end": end, "reason": None}

    def adopt(self, plan, observations=None, *, records=None, status="complete", **extra):
        if records is None:
            records = {"comment-1": {"observations": observations or {}}}
        result = {"schema": evidence.RESULT_SCHEMA, "plan_digest": plan["plan_digest"],
                  "status": status, "records": records, **extra}
        path = self.root / "candidate-result.json"
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return evidence.adopt_evidence(plan, path)

    def adapter_fixture(self, plan, *, completed=True, publish=True):
        """Synthetic local transport artifacts; no request or provider claim."""
        digest, run = plan["plan_digest"], "11111111-1111-4111-8111-111111111111"
        identity = {"plan_digest": digest, "run_id": run, "generation": 0}
        source = plan["sources"]["comment"]
        observation = self.candidate()
        missing = {"status": "missing", "value": None, "source_id": None, "start": None,
                   "end": None, "reason": "missing_field"}
        observations = {"quote": observation, "claim": missing}
        attempt = {"attempt_id": run + ":1", "record_id": "comment-1", "source_id": "comment",
                   "source_digest": source["sha256"], "fields": 2, "input_bytes": source["byte_length"],
                   "request_bytes": 100, "requested_model": "cloudflare/clairic", "completed": completed,
                   "disposition": "complete" if completed else "transport_failure", "elapsed_ms": 1,
                   "observed_model": None, "observed_provider": None, "usage": {}}
        provenance = {"schema": "jevify-extraction-receipt/1", "run_id": run, "generation": 0,
                      "disposition": attempt["disposition"], "requested_model": "cloudflare/clairic",
                      "requested_provider": "openrouter", "attempts": [attempt], "request_attempts": 1,
                      "completed_requests": int(completed), "original_records": 1, "required_fields": 2,
                      "input_bytes": source["byte_length"], "elapsed_ms": 1, "usage_complete": False}
        result = {"schema": evidence.RESULT_SCHEMA, "plan_digest": digest,
                  "status": "complete" if completed else "error", "provenance": provenance,
                  "records": {"comment-1": {"observations": observations}}}
        root = Path(plan["artifact_root"])
        (root / "attempts").mkdir(exist_ok=True)
        (root / "results").mkdir(exist_ok=True)
        result_path = root / "results" / (digest + ".json")
        raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
        result_path.write_bytes(raw)
        summary = {**identity, "disposition": attempt["disposition"], "result_path": str(result_path),
                   "receipt_path": None, "accounting": provenance, "transmission_uncertain": False,
                   "candidates": 1, "request_attempts": 1, "result_sha256": hashlib.sha256(raw).hexdigest(),
                   "records": 1, "fields": 2, "reused": False}
        files = {digest: {"schema": "jevify-extraction-attempt/1", **identity,
                          "disposition": "reserved", "requested_model": "cloudflare/clairic"},
                 digest + "-1": {"schema": "jevify-extraction-intent/1", **identity,
                                 **{key: attempt[key] for key in ("attempt_id", "record_id", "source_id", "source_digest",
                                                                  "fields", "input_bytes", "request_bytes", "requested_model")}},
                 digest + "-1-0": {"schema": "jevify-extraction-outcome/1", **identity,
                                   "attempt": attempt, "observations": observations}}
        if publish:
            files[digest + "-99"] = {"schema": "jevify-extraction-publication/1", **identity,
                                      "result_sha256": summary["result_sha256"], "summary": summary}
        for name, body in files.items():
            (root / "attempts" / (name + ".json")).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return result_path

    def test_adapter_result_requires_committed_publication_receipt(self):
        plan = self.prepare()
        result_path = self.adapter_fixture(plan, publish=False)
        with self.assertRaisesRegex(ValueError, "publication"):
            evidence.adopt_evidence(plan, result_path)
        self.assertFalse((Path(plan["artifact_root"]) / "adoptions").exists())

    def test_committed_adapter_result_binds_raw_hash_and_completed_outcomes(self):
        plan = self.prepare()
        result_path = self.adapter_fixture(plan)
        adopted = evidence.adopt_evidence(plan, result_path)
        self.assertEqual(adopted["records"]["comment-1"]["observations"]["quote"]["status"], "candidate")
        self.assertTrue(evidence.validate_evidence(adopted)["ok"])
        receipt = Path(plan["artifact_root"]) / "attempts" / (plan["plan_digest"] + "-99.json")
        receipt.write_text("{}", encoding="utf-8")
        self.assertFalse(evidence.validate_evidence(adopted)["integrity_ok"])

    def test_failed_request_cannot_supply_candidate_even_with_publication(self):
        plan = self.prepare()
        result_path = self.adapter_fixture(plan, completed=False)
        with self.assertRaisesRegex(ValueError, "completed"):
            evidence.adopt_evidence(plan, result_path)

    def test_adapter_publication_hash_is_raw_not_reencoded_json(self):
        plan = self.prepare()
        result_path = self.adapter_fixture(plan)
        result_path.write_bytes(result_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            evidence.adopt_evidence(plan, result_path)

    def test_raw_capture_and_repeat_preparation_have_identical_identity(self):
        plan = self.prepare()
        again = self.prepare()
        self.assertEqual(plan, again)
        snapshot = Path(plan["sources"]["comment"]["snapshot"])
        self.assertEqual(snapshot.read_bytes(), self.comment.encode("utf-8"))
        self.assertEqual(plan["sources"]["comment"]["byte_length"], len(self.comment.encode("utf-8")))
        self.assertTrue(evidence.validate_evidence(plan)["ok"])
        self.assertEqual(len(list((self.root / ".jevify-evidence" / "plans").iterdir())), 1)

    def test_astral_subline_occurrence_maps_to_raw_bytes_and_complete_lines(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate()})
        occurrence = adopted["records"]["comment-1"]["observations"]["quote"]["occurrence"]
        raw = self.comment.encode("utf-8")
        self.assertEqual(raw[occurrence["byte_start"]:occurrence["byte_end"]].decode(), "😀 quoted issue")
        self.assertEqual((occurrence["line_start"], occurrence["line_end"]), (2, 2))
        self.assertEqual(occurrence["line_quote"], "A 😀 quoted issue")
        self.assertEqual(raw[occurrence["line_byte_start"]:occurrence["line_byte_end"]], b"A \xf0\x9f\x98\x80 quoted issue\r\n")
        self.assertEqual(occurrence["byte_end"] - occurrence["end"], 3)

    def test_multiline_quote_is_complete_lf_joined_not_fragment(self):
        plan = self.prepare()
        value = "issue\r\nquoted"
        adopted = self.adopt(plan, {"quote": self.candidate(value)})
        occurrence = adopted["records"]["comment-1"]["observations"]["quote"]["occurrence"]
        self.assertEqual(occurrence["line_quote"], "A 😀 quoted issue\nquoted issue again")
        self.assertEqual((occurrence["line_start"], occurrence["line_end"]), (2, 3))
        self.assertNotEqual(occurrence["line_quote"], value)

    def test_repeated_unlocated_value_does_not_pick_first_match(self):
        plan = self.prepare()
        candidate = self.candidate("quoted issue")
        candidate.update(start=None, end=None)
        adopted = self.adopt(plan, {"quote": candidate})
        field = adopted["records"]["comment-1"]["observations"]["quote"]
        self.assertEqual(field["status"], "rejected")
        self.assertIn("nonunique", field["reason"])
        self.assertIsNone(field["occurrence"])

    def test_explicit_second_occurrence_keeps_its_attribution(self):
        plan = self.prepare()
        start = self.comment.rindex("quoted issue")
        adopted = self.adopt(plan, {"quote": self.candidate("quoted issue", start=start, end=start + 12)})
        occurrence = adopted["records"]["comment-1"]["observations"]["quote"]["occurrence"]
        self.assertEqual(occurrence["start"], start)
        self.assertEqual(occurrence["line_start"], 3)

    def test_unique_unlocated_occurrence_can_be_proven(self):
        plan = self.prepare()
        candidate = self.candidate()
        candidate.update(start=None, end=None)
        adopted = self.adopt(plan, {"quote": candidate})
        field = adopted["records"]["comment-1"]["observations"]["quote"]
        self.assertEqual(field["status"], "candidate")
        self.assertEqual(field["start"], self.comment.index("😀"))

    def test_null_value_remains_missing(self):
        plan = self.prepare()
        candidate = self.candidate()
        candidate.update(value=None)
        adopted = self.adopt(plan, {"quote": candidate})
        field = adopted["records"]["comment-1"]["observations"]["quote"]
        self.assertEqual(field["status"], "missing")
        self.assertIsNone(field["occurrence"])
        self.assertIsNone(field["start"])

    def test_invalid_bounds_boolean_and_utf16_offsets_are_not_repaired(self):
        plan = self.prepare()
        start = self.comment.index("😀")
        for first, last in ((-1, 2), (start, len(self.comment) + 1), (True, 4),
                            (start, start + len("😀 quoted issue") + 1), (start, None)):
            with self.subTest(start=first, end=last):
                candidate = self.candidate()
                candidate.update(start=first, end=last)
                adopted = self.adopt(plan, {"quote": candidate})
                field = adopted["records"]["comment-1"]["observations"]["quote"]
                self.assertEqual(field["status"], "rejected")
                self.assertIsNone(field["occurrence"])

    def test_wrong_source_role_subject_version_or_dependency_refused(self):
        extra = {"id": "other", "role": "original_comment", "text": self.comment,
                 "snapshot_identity": "another", "subject": "different", "version": "r1"}
        sources = self.sources + [extra]
        records = copy.deepcopy(self.records)
        records[0]["dependencies"].append("other")
        plan = self.prepare(sources=sources, records=records)
        for source_id in ("unknown", "other", "code"):
            with self.subTest(source_id=source_id):
                adopted = self.adopt(plan, {"quote": self.candidate(source_id=source_id)})
                self.assertEqual(adopted["records"]["comment-1"]["observations"]["quote"]["status"], "rejected")
        sources[-1] = dict(extra, subject="current", version="r2")
        version_plan = self.prepare(sources=sources, records=records)
        adopted = self.adopt(version_plan, {"quote": self.candidate(source_id="other")})
        self.assertIn("version", adopted["records"]["comment-1"]["observations"]["quote"]["reason"])
        dependency_plan = self.prepare(sources=sources)
        adopted = self.adopt(dependency_plan, {"quote": self.candidate(source_id="other")})
        self.assertIn("dependency", adopted["records"]["comment-1"]["observations"]["quote"]["reason"])

    def test_missing_records_errors_and_empty_sources_never_disappear(self):
        sources = self.sources + [{"id": "empty", "role": "original_comment", "text": "", "snapshot_identity": "empty-1"}]
        records = self.records + [{"id": "empty-1", "source_id": "empty", "required_fields": ["quote"],
                                   "required_questions": ["valid"]}]
        plan = self.prepare(sources=sources, records=records)
        adopted = self.adopt(plan, records={}, status="error")
        self.assertEqual(set(adopted["records"]), {"comment-1", "empty-1"})
        self.assertEqual(adopted["ledger"]["totals"]["records"], 2)
        self.assertEqual(adopted["ledger"]["totals"]["unresolved_fields"], 3)
        self.assertEqual(adopted["ledger"]["totals"]["blocked_questions"], 3)
        self.assertFalse(adopted["ledger"]["complete"])
        self.assertEqual(adopted["records"]["empty-1"]["observations"]["quote"]["status"], "error")

    def test_one_candidate_does_not_resolve_other_required_facet(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate()})
        self.assertEqual(adopted["ledger"]["records"]["comment-1"]["disposition"], "unresolved")
        self.assertEqual(adopted["records"]["comment-1"]["questions"], {"valid": "blocked", "severity": "blocked"})
        rendered = evidence.evidence_states(adopted, {"valid": object(), "severity": object()})
        self.assertEqual(rendered["report"]["unresolved_ids"], ["comment-1"])
        self.assertIn("required field not returned", rendered["states"]["comment-1"])

    def test_all_candidates_still_leave_every_question_pending(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate(), "claim": self.candidate()})
        self.assertEqual(adopted["records"]["comment-1"]["questions"], {"valid": "pending", "severity": "pending"})
        self.assertFalse(adopted["ledger"]["complete"])
        rendered = evidence.evidence_states(adopted, {"valid": object()})
        self.assertFalse(rendered["report"]["ok"])
        self.assertEqual(rendered["ledger"]["records"]["comment-1"]["questions"]["severity"], "blocked")
        self.assertEqual(rendered["ledger"]["totals"]["blocked_questions"], 1)

    def test_observations_never_become_independent_facts(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate()})
        item = adopted["records"]["comment-1"]
        self.assertEqual(item["facts"], {"has_code": True})
        self.assertEqual(item["dependencies"], ["code", "comment", "contract"])
        self.assertEqual(item["observations"]["quote"]["plan_digest"], plan["plan_digest"])
        self.records[0]["facts"]["has_code"] = False
        plan["records"]["comment-1"]["facts"]["has_code"] = False
        self.assertTrue(item["facts"]["has_code"])

    def test_changed_contract_stales_dependent_even_when_quote_unchanged(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate()})
        (self.root / "contract.md").write_text("Now return two.\n", encoding="utf-8")
        status = evidence.validate_evidence(adopted)
        self.assertFalse(status["ok"])
        self.assertEqual(status["changed"], ["contract"])
        self.assertEqual(status["stale_records"], ["comment-1"])
        with self.assertRaisesRegex(ValueError, "stale"):
            evidence.evidence_states(adopted, {"valid": object(), "severity": object()})
        self.assertEqual(Path(plan["sources"]["contract"]["snapshot"]).read_text(), "The current function must return one.\n")

    def test_revision_only_change_invalidates_unchanged_source(self):
        (self.root / "revision").write_bytes(b"revision-1")
        sources = copy.deepcopy(self.sources)
        sources[1].update(revision="revision-1", revision_path="revision")
        plan = self.prepare(sources=sources)
        (self.root / "revision").write_bytes(b"revision-2")
        status = evidence.validate_evidence(plan)
        self.assertTrue(status["integrity_ok"])
        self.assertFalse(status["ok"])
        self.assertEqual(status["changed"], ["code"])
        self.assertEqual(status["stale_records"], ["comment-1"])
        self.assertIn("revision changed", "; ".join(status["problems"]))
        with self.assertRaisesRegex(ValueError, "revision marker"):
            self.prepare(sources=sources)

    def test_integrity_failure_differs_from_refreshable_origin_drift(self):
        plan = self.prepare()
        (self.root / "contract.md").write_text("fresh policy", encoding="utf-8")
        self.assertTrue(evidence.validate_evidence(plan)["integrity_ok"])
        snapshot = Path(plan["sources"]["contract"]["snapshot"])
        snapshot.chmod(0o600)
        snapshot.write_text("corrupt captured policy", encoding="utf-8")
        self.assertFalse(evidence.validate_evidence(plan)["integrity_ok"])

    def test_oversize_state_preserves_id_and_explicitly_blocks_all_questions(self):
        plan = self.prepare(limits={"max_state_bytes": 16})
        rendered = evidence.evidence_states(plan, {"valid": object(), "severity": object()})
        self.assertEqual(rendered["states"], {"comment-1": ""})
        self.assertFalse(rendered["report"]["ok"])
        self.assertEqual(rendered["report"]["outcomes"]["comment-1"]["status"], "oversize")
        self.assertTrue(rendered["report"]["outcomes"]["comment-1"]["truncated"])
        self.assertEqual(rendered["ledger"]["totals"]["blocked_questions"], 2)
        self.assertEqual(Path(plan["sources"]["comment"]["snapshot"]).read_bytes(), self.comment.encode())

    def test_structured_records_with_no_extraction_fields_still_need_questions(self):
        records = [{"id": "structured", "source_id": "code", "required_fields": [],
                    "required_questions": ["valid"], "facts": {"count": 1}}]
        plan = self.prepare(records=records, fields={})
        rendered = evidence.evidence_states(plan, {"valid": object()})
        self.assertTrue(rendered["report"]["ok"])
        self.assertEqual(rendered["ledger"]["records"]["structured"]["questions"], {"valid": "pending"})
        self.assertFalse(rendered["ledger"]["complete"])
        self.assertIn("def current", rendered["states"]["structured"])

    def test_removed_dependency_is_reported_missing(self):
        plan = self.prepare()
        (self.root / "contract.md").unlink()
        status = evidence.validate_evidence(plan)
        self.assertEqual(status["missing"], ["contract"])
        self.assertEqual(status["stale_records"], ["comment-1"])

    def test_snapshot_refresh_preserves_historical_base_without_live_refresh(self):
        sources = self.sources + [{"id": "base", "role": "base_code", "path": "current.py", "refresh": "snapshot"}]
        records = [{"id": "base-review", "source_id": "base", "required_fields": [], "required_questions": ["valid"]}]
        plan = self.prepare(sources=[sources[-1]], records=records)
        (self.root / "current.py").write_text("different code", encoding="utf-8")
        self.assertTrue(evidence.validate_evidence(plan)["ok"])
        with self.assertRaisesRegex(ValueError, "read-only"):
            evidence.assert_deletion_eligible(plan, ["base"])

    def test_inventory_additions_removals_and_changes_invalidate_all_records(self):
        inputs = {"items": self.sources, "inventories": [{"id": "python", "directory": ".", "pattern": "**/*.py"}]}
        plan = self.prepare(sources=inputs)
        (self.root / "new.py").write_text("new", encoding="utf-8")
        status = evidence.validate_evidence(plan)
        self.assertEqual(status["inventory_changes"]["python"]["added"], ["new.py"])
        self.assertEqual(status["stale_records"], ["comment-1"])
        refreshed = self.prepare(sources=inputs)
        self.assertNotEqual(plan["plan_digest"], refreshed["plan_digest"])
        (self.root / "new.py").unlink()
        status = evidence.validate_evidence(refreshed)
        self.assertEqual(status["inventory_changes"]["python"]["removed"], ["new.py"])
        (self.root / "current.py").write_text("changed", encoding="utf-8")
        self.assertEqual(evidence.validate_evidence(plan)["inventory_changes"]["python"]["changed"], ["current.py"])

    def test_inventory_nonrecursive_pattern_does_not_include_nested_files(self):
        (self.root / "nested").mkdir()
        (self.root / "nested" / "hidden.py").write_text("nested", encoding="utf-8")
        inputs = {"items": self.sources, "inventories": [{"id": "top", "directory": ".", "pattern": "*.py"}]}
        plan = self.prepare(sources=inputs)
        self.assertEqual(set(plan["inventories"]["top"]["files"]), {"current.py"})
        (self.root / "nested" / "hidden.py").write_text("updated nested", encoding="utf-8")
        self.assertTrue(evidence.validate_evidence(plan)["ok"])

    def test_tampered_snapshot_or_plan_never_repairs_itself(self):
        plan = self.prepare()
        snapshot = Path(plan["sources"]["comment"]["snapshot"])
        snapshot.chmod(0o600)
        snapshot.write_bytes(b"altered snapshot")
        status = evidence.validate_evidence(plan)
        self.assertEqual(status["snapshot_changed"], ["comment"])
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(snapshot.read_bytes(), b"altered snapshot")
        altered = copy.deepcopy(plan)
        altered["records"]["comment-1"]["subject"] = "forged"
        self.assertFalse(evidence.validate_evidence(altered)["ok"])

    def test_tampered_adoption_and_result_are_rejected(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate()})
        tampered = copy.deepcopy(adopted)
        tampered["records"]["comment-1"]["observations"]["quote"]["value"] = "forged"
        self.assertFalse(evidence.validate_evidence(tampered)["ok"])
        result_file = self.root / ".jevify-evidence" / "results" / (adopted["result_digest"] + ".json")
        result_file.chmod(0o600)
        result_file.write_bytes(b"{}")
        self.assertFalse(evidence.validate_evidence(adopted)["ok"])

    def test_interrupted_atomic_publication_does_not_admit_partial_artifact(self):
        with mock.patch.object(evidence.os, "link", side_effect=OSError("injected publication interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.prepare()
        self.assertFalse(list(self.root.rglob(".pending-*")))
        self.assertFalse(list(self.root.rglob("*.utf8")))
        self.assertFalse((self.root / ".jevify-evidence" / "plans").exists())
        self.assertTrue(evidence.validate_evidence(self.prepare())["ok"])

    def test_snapshot_and_imported_comment_deletion_refused_even_with_candidate(self):
        plan = self.prepare()
        adopted = self.adopt(plan, {"quote": self.candidate(), "claim": self.candidate()})
        with self.assertRaisesRegex(ValueError, "read-only"):
            evidence.assert_deletion_eligible(adopted, ["comment"])
        with self.assertRaisesRegex(ValueError, "never deletable"):
            evidence.assert_deletion_eligible(adopted, paths=[plan["sources"]["comment"]["snapshot"]])
        with self.assertRaisesRegex(ValueError, "read-only"):
            evidence.assert_deletion_eligible(adopted, paths=["contract.md"])
        eligibility = evidence.assert_deletion_eligible(adopted, paths=["current.py"])
        self.assertEqual(eligibility, {"eligible": ["code"], "grants_authority": False})
        self.assertTrue((self.root / "current.py").exists())

    def test_path_alias_to_imported_role_cannot_bypass_deletion_guard(self):
        sources = self.sources + [{"id": "imported-file", "role": "original_comment", "path": "current.py"}]
        plan = self.prepare(sources=sources)
        with self.assertRaisesRegex(ValueError, "read-only"):
            evidence.assert_deletion_eligible(plan, paths=["current.py"])
        with self.assertRaisesRegex(ValueError, "read-only"):
            evidence.assert_deletion_eligible(plan, ["code"])

    def test_unknown_schema_and_foreign_plan_result_are_refused(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            evidence.validate_evidence({"schema": "jevify-evidence/99"})
        plan = self.prepare()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.adopt(plan, plan_digest="f" * 64)
        with self.assertRaisesRegex(ValueError, "unknown record"):
            self.adopt(plan, records={"extra": {"observations": {}}})
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self.adopt(plan, {"not-declared": self.candidate()})

    def test_duplicate_json_keys_and_oversize_results_fail_before_admission(self):
        plan = self.prepare()
        result_file = self.root / "invalid.json"
        result_file.write_text('{"schema":"first","schema":"second"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            evidence.adopt_evidence(plan, result_file)
        small = self.prepare(limits={"max_value_bytes": 4})
        with self.assertRaisesRegex(ValueError, "string bound"):
            self.adopt(small, {"quote": self.candidate()})

    def test_bounds_bad_utf8_and_unstable_json_numbers_are_refused(self):
        for limits in ({"max_records": True}, {"max_fields": 0}, {"unknown": 1}, {"max_sources": 1000}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.prepare(limits=limits)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.prepare(limits={"max_source_bytes": 2})
        with self.assertRaisesRegex(ValueError, "total byte"):
            self.prepare(limits={"max_total_source_bytes": 2})
        (self.root / "current.py").write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.prepare()
        records = copy.deepcopy(self.records)
        records[0]["facts"]["unstable"] = float("nan")
        records[0]["dependencies"] = []
        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            self.prepare(records=records, sources=[self.sources[0]])

    def test_no_path_traversal_or_symlink_capture_and_publication(self):
        for path in ("../outside", "/etc/passwd", "a/../../escape"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.prepare(sources=[dict(self.sources[1], path=path)])
        (self.root / "link.py").symlink_to(self.root / "current.py")
        with self.assertRaises(OSError):
            self.prepare(sources=[dict(self.sources[1], path="link.py")])
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.root / ".jevify-evidence").symlink_to(outside.name, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(list(Path(outside.name).iterdir()), [])

    def test_secret_classification_is_explicit_and_persistent(self):
        sources = copy.deepcopy(self.sources)
        sources[0]["sensitive"] = True
        sources[1]["classification"] = "public"
        plan = self.prepare(sources=sources)
        self.assertEqual(plan["sources"]["comment"]["classification"], "secret")
        self.assertFalse(plan["sources"]["comment"]["transport_admitted"])
        self.assertTrue(plan["sources"]["code"]["transport_admitted"])
        self.assertEqual(plan["sources"]["contract"]["classification"], "private")
        rendered = evidence.evidence_states(plan, {"valid": object(), "severity": object()})
        self.assertEqual(rendered["states"], {"comment-1": ""})
        self.assertEqual(rendered["report"]["outcomes"]["comment-1"]["status"], "secret")
        self.assertFalse(rendered["report"]["ok"])
        self.assertNotIn("quoted issue", json.dumps(rendered["states"]))
        self.assertEqual(rendered["ledger"]["totals"]["blocked_questions"], 2)

    def test_fresh_process_reopens_immutable_plan_and_detects_contract_only_drift(self):
        plan = self.prepare()
        saved = self.root / ".jevify-evidence" / "plans" / (plan["plan_digest"] + ".json")
        (self.root / "contract.md").write_text("Different governing policy", encoding="utf-8")
        program = (
            "import importlib.util,json,sys\n"
            "from pathlib import Path\n"
            "s=importlib.util.spec_from_file_location('evidence',sys.argv[1])\n"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            "print(json.dumps(m.validate_evidence(json.loads(Path(sys.argv[2]).read_text()))))\n"
        )
        child = subprocess.run([sys.executable, "-c", program, str(MODULE), str(saved)],
                               cwd="/", capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        status = json.loads(child.stdout)
        self.assertEqual(status["changed"], ["contract"])
        self.assertEqual(status["stale_records"], ["comment-1"])


if __name__ == "__main__":
    unittest.main()
