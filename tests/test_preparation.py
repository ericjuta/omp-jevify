"""Provider-free end-to-end preparation experiments. All gold/usage here is SOFTWARE fixture data.

No real adjudicator, measurement, paid authorization or unexposed holdout is asserted.
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("jevify_preparation_test", ROOT / "evals" / "preparation.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PreparationFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, scenario="improves", name="fixture"):
        manifest, outcomes = p.synthetic_fixture(self.root / name, scenario)
        return manifest, outcomes

    def mutate(self, manifest, outcomes, change):
        value = p.decode(manifest.read_bytes())
        change(value)
        manifest.write_bytes(p.canonical(value))
        if outcomes is not None:
            bundle = p.load_manifest(manifest)
            rows = p.decode(outcomes.read_bytes())
            for cid, arm, rep in p.schedule(bundle):
                request = p.request_for(bundle, cid, arm, rep, 0)
                row = rows[f"{cid}:{arm}:{rep}:0"]
                row["identity"] = request["identity"]
                if arm == "C":
                    row["observed"]["candidate"] = request["candidate"]
            outcomes.write_bytes(p.canonical(rows))
        return value

    def run_fixture(self, scenario="improves", name="fixture"):
        manifest, outcomes = self.fixture(scenario, name)
        report = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        return manifest, outcomes, report

    def test_four_frozen_outcome_scenarios_execute_actual_runner_and_report(self):
        for scenario, expected in (("improves", "adopt"), ("worsens", "keep_deterministic"),
                                   ("ties", "keep_deterministic"), ("missing", "inconclusive")):
            with self.subTest(scenario=scenario):
                manifest, _, result = self.run_fixture(scenario, scenario)
                self.assertEqual(result["primary_contrast"]["gate_verdict"], expected)
                self.assertEqual(result["recommendation"], "inconclusive")
                self.assertEqual(result["evidence_class"], "injected_software_evidence")
                self.assertEqual(result, p.report(manifest, manifest.parent / "run"))
                if scenario != "missing":
                    self.assertEqual({r["arm"] for r in result["outcomes"]}, set(p.ARMS))
                    self.assertEqual(len(result["outcomes"]), 24)
                    self.assertEqual(result["independent_cases"], 3)
                    self.assertEqual(result["primary_contrast"]["metrics"]["decision_accuracy"]["n"], 3)
                else:
                    self.assertFalse(result["gates"]["complete_outcomes"])
                    self.assertIsNone(result["cost"]["observed_total_usd"])
                    self.assertEqual(result["stop"]["reason"], "incomplete_evidence")

    def test_public_adversarial_corpus_covers_named_challenges_without_real_gold(self):
        manifest, _ = self.fixture()
        b = p.load_manifest(manifest)
        self.assertEqual(set().union(*(set(c["manifest"]["challenges"]) for c in b["cases"])), p.CHALLENGES)
        preflight = p.preflight(manifest)
        self.assertEqual(preflight["transmissions"], 0)
        self.assertEqual(preflight["recommendation"], "inconclusive")
        self.assertTrue(preflight["paid_admission"].startswith("withheld"))
        self.assertEqual(set(preflight["gold_questions"].values()), {2})

    def test_cli_demo_and_recomputed_report_are_provider_free(self):
        done = subprocess.run([sys.executable, str(ROOT / "evals" / "preparation.py"), "demo", "--out", str(self.root / "cli"),
                               "--scenario", "improves"], capture_output=True, text=True, check=True)
        result = json.loads(done.stdout)
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "adopt")
        self.assertEqual(result["recommendation"], "inconclusive")
        done = subprocess.run([sys.executable, str(ROOT / "evals" / "preparation.py"), "preflight", str(self.root / "cli" / "manifest.json")],
                              capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(done.stdout)["transmissions"], 0)

    def test_one_changed_B_C_retrieval_passage_refuses_before_execution(self):
        manifest, outcomes = self.fixture()
        m = p.decode(manifest.read_bytes())
        case = m["cases"][0]
        source = (manifest.parent / case["sources"]["comment"]["file"]["path"]).read_text()
        quote = source.splitlines()[0]
        changed = [{"source_id": "comment", "revision": "v2", "start": 0, "end": len(quote), "quote": quote}]
        path = manifest.parent / "changed-passages.json"
        path.write_bytes(p.canonical(changed))
        case["retrieval"]["C"] = p.file_ref(path, manifest.parent)
        manifest.write_bytes(p.canonical(m))
        with self.assertRaisesRegex(ValueError, "B/C retrieved"):
            p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertFalse((manifest.parent / "run").exists())

    def test_B_C_strategy_or_control_change_cannot_masquerade_as_extraction(self):
        for change in (lambda m: m["treatments"]["C"].update(strategy="new-method"),
                       lambda m: m["treatments"]["C"].update(controls_sha256="0" * 64)):
            manifest, _ = self.fixture(name=str(len(list(self.root.iterdir()))))
            self.mutate(manifest, None, change)
            with self.assertRaises(ValueError):
                p.preflight(manifest)

    def test_unbound_field_instruction_refused(self):
        manifest, _ = self.fixture()
        m = p.decode(manifest.read_bytes())
        control_path = manifest.parent / m["controls"]["path"]
        controls = p.decode(control_path.read_bytes())
        del controls["fields"]["evidence"]["instructions"]
        control_path.write_bytes(p.canonical(controls))
        m["controls"] = p.file_ref(control_path, manifest.parent)
        for arm in p.ARMS:
            m["treatments"][arm]["controls_sha256"] = m["controls"]["sha256"]
        manifest.write_bytes(p.canonical(m))
        with self.assertRaisesRegex(ValueError, "field"):
            p.preflight(manifest)

    def test_duplicate_aliases_do_not_increase_independent_denominator(self):
        manifest, outcomes = self.fixture("ties")
        self.mutate(manifest, outcomes, lambda m: m["cases"][1].update(unit_id=m["cases"][0]["unit_id"]))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["independent_cases"], 2)
        self.assertFalse(result["gates"]["no_duplicate_aliases"])
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")

    def test_key_alias_and_same_sources_collapse_even_different_unit_names(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        alias = copy.deepcopy(bundle["cases"][0])
        alias["manifest"]["id"] = "renamed-case"
        alias["manifest"]["unit_id"] = "renamed-unit"
        bundle["cases"].append(alias)
        self.assertEqual(len(p.independent_units(bundle)), 3)

    def test_independent_gold_rejects_self_grading_and_source_drift(self):
        for index, change in enumerate((lambda m: m["cases"][0]["adjudication"].update(adjudicator="synthetic-author"),
                                        lambda m: m["cases"][0]["adjudication"].update(kind="model"),
                                        lambda m: m["cases"][0]["adjudication"].update(source_inventory_sha256="0" * 64))):
            manifest, _ = self.fixture(name=str(index))
            self.mutate(manifest, None, change)
            with self.assertRaises(ValueError):
                p.preflight(manifest)

    def test_exposed_history_cannot_be_relabelled_fresh(self):
        manifest, outcomes = self.fixture()
        self.mutate(manifest, outcomes, lambda m: m["cases"][0]["exposure_history"].append(
            {"at": m["frozen_at"], "kind": "evaluated", "actor": "fixture-history"}))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertFalse(result["gates"]["heldout_unexposed_gold"])
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")

    def test_request_boundary_never_exposes_gold_or_adjudication(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        for cid, arm, rep in p.schedule(bundle):
            request = p.request_for(bundle, cid, arm, rep, 0)
            encoded = p.canonical(request)
            self.assertNotIn(b'"gold"', encoded)
            self.assertNotIn(b'"adjudication"', encoded)
            self.assertNotIn(b'"unsafe_values"', encoded)
            self.assertNotIn(b'synthetic-independent-fixture', encoded)
            self.assertNotIn(b'qualified_only', encoded)

    def test_wrong_id_subject_and_version_receive_no_credit_even_with_valid_quote(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        case = bundle["cases"][0]
        cid = case["manifest"]["id"]
        original = p.decode(outcomes.read_bytes())[f"{cid}:C:0:0"]
        for field, replacement in (("id", cid + "-neighbor"), ("subject", "neighbor"), ("revision", "v1")):
            outcome = copy.deepcopy(original)
            outcome["records"][0][field] = replacement
            score = p.score(case, outcome, 1, p.account(outcome["receipts"]), None)
            self.assertEqual(score["metrics"]["occurrence_accuracy"], 1)
            self.assertEqual(score["metrics"]["decision_accuracy"], 0)
            self.assertEqual(score["metrics"]["claim_recall"], 0)

    def test_exact_repeated_unicode_occurrences_and_null_denominators(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        case = bundle["cases"][0]
        cid = case["manifest"]["id"]
        outcome = p.decode(outcomes.read_bytes())[f"{cid}:C:0:0"]
        scored = p.score(case, outcome, 1, p.account(outcome["receipts"]), None)
        self.assertEqual(scored["denominators"]["null_questions"], 1)
        self.assertEqual(scored["metrics"]["abstention_accuracy"], 1)
        self.assertEqual(scored["metrics"]["unresolved_rate"], 0)
        source = case["sources"]["comment"]["text"]
        quote = "Identical repeated quote."
        second = source.rindex(quote)
        citation = {"source_id": "comment", "revision": "v2", "start": second, "end": second + len(quote), "quote": quote}
        self.assertTrue(p._citation(citation, case["sources"]))
        outcome["records"][0]["occurrences"] = [citation]
        scored = p.score(case, outcome, 1, p.account(outcome["receipts"]), None)
        self.assertEqual(scored["metrics"]["occurrence_accuracy"], 1)
        self.assertEqual(scored["metrics"]["decision_accuracy"], 0)

    def test_semantic_missing_claim_recall_is_not_processed_section_coverage(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        case = bundle["cases"][0]
        cid = case["manifest"]["id"]
        case["gold"]["claims"]["hidden-source-claim"] = copy.deepcopy(case["gold"]["claims"][cid])
        outcome = p.decode(outcomes.read_bytes())[f"{cid}:C:0:0"]
        scored = p.score(case, outcome, 1, p.account(outcome["receipts"]), None)
        self.assertEqual(scored["processed_section_coverage"], 1)
        self.assertEqual(scored["metrics"]["claim_recall"], .5)
        self.assertEqual(scored["denominators"]["questions"], 4)

    def test_missing_required_outcome_stops_admission_and_is_not_success(self):
        manifest, outcomes = self.fixture()
        rows = p.decode(outcomes.read_bytes())
        first = p.schedule(p.load_manifest(manifest))[0]
        row = rows[f"{first[0]}:{first[1]}:{first[2]}:0"]
        del row["records"][0]["answers"]["base_proves_introduced"]
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["cost"]["attempts"], 1)
        self.assertEqual(result["stop"]["reason"], "incomplete_evidence")
        self.assertFalse(result["gates"]["complete_outcomes"])

    def test_observed_model_drift_withholds_even_requested_config_matches(self):
        manifest, outcomes = self.fixture()
        rows = p.decode(outcomes.read_bytes())
        for row in rows.values():
            if row["identity"]["arm"] == "C":
                row["observed"]["model"] = "different-model"
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertFalse(result["gates"]["observed_B_C_identity"])
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")

    def test_disabled_candidate_cannot_evaluate_baseline_twice(self):
        manifest, outcomes = self.fixture()
        rows = p.decode(outcomes.read_bytes())
        for row in rows.values():
            if row["identity"]["arm"] == "C":
                row["observed"]["candidate"] = None
                row["observed"]["extraction_invoked"] = False
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")
        self.assertFalse(result["gates"]["complete_outcomes"])

    def test_unsafe_candidate_loss_does_not_automatically_recommend_baseline(self):
        manifest, outcomes = self.fixture()
        rows = p.decode(outcomes.read_bytes())
        for row in rows.values():
            if row["identity"]["arm"] == "C":
                row["records"][0]["answers"]["supported"] = "repair_now"
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["absolute_means"]["B"]["decision_accuracy"], .5)
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")

    def test_cost_stages_and_unknown_zero_are_independent(self):
        manifest, outcomes = self.fixture()
        row = next(r for r in p.decode(outcomes.read_bytes()).values() if r["identity"]["arm"] == "C")
        account = p.account(row["receipts"])
        self.assertEqual(account["total_cost_usd"], "0.04")
        self.assertEqual(account["stages"]["extraction"]["attempts"], 1)
        self.assertEqual(account["stages"]["extraction"]["provider_passes"], 1)
        self.assertEqual(account["stages"]["extraction"]["input_tokens"], 20)
        row["receipts"][1]["cost_usd"] = "0"
        self.assertIsNone(p.account(row["receipts"])["total_cost_usd"])
        row["receipts"][1]["cost_evidence"] = "corroborated_zero"
        self.assertEqual(p.account(row["receipts"])["total_cost_usd"], "0.03")
        row["receipts"].append(copy.deepcopy(row["receipts"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate receipt"):
            p.account(row["receipts"])

    def test_approved_attempt_allocation_caps_stop_without_silent_scope_change(self):
        manifest, outcomes = self.fixture()
        self.mutate(manifest, outcomes, lambda m: m["budget"].update(max_allocation_usd="1"))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["cost"]["attempts"], 1)
        self.assertEqual(result["stop"]["reason"], "authorized_admission_cap")
        self.assertEqual(result["stop"]["allocation_consumed_usd"], "1")
        self.assertEqual(result["recommendation"], "inconclusive")

    def test_interruption_is_durable_unknown_spend_and_never_retried(self):
        manifest, _ = self.fixture()
        calls = []
        def interrupted(request, directory):
            calls.append(request)
            raise KeyboardInterrupt()
        result = p.run_experiment(manifest, manifest.parent / "run", executor=interrupted)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["stop"]["reason"], "interrupted")
        self.assertIsNone(result["cost"]["observed_total_usd"])
        self.assertEqual(result["stop"]["allocation_consumed_usd"], "1")
        self.assertFalse(result["gates"]["complete_outcomes"])

    def test_all_bound_files_are_rechecked_before_next_attempt(self):
        manifest, outcomes = self.fixture()
        delegate = p.InjectedExecutor(outcomes)
        calls = []
        def mutating(request, directory):
            calls.append(request)
            source = next(iter(request["sources"].values()))["file"]["path"]
            (manifest.parent / source).write_text("changed after first provider-free attempt")
            return delegate(request, directory)
        with self.assertRaisesRegex(ValueError, "bound bytes changed"):
            p.run_experiment(manifest, manifest.parent / "run", executor=mutating)
        self.assertEqual(len(calls), 1)
        entries, _ = p.read_ledger(manifest.parent / "run" / "attempts.jsonl")
        self.assertEqual([x["event"] for x in entries], ["start", "reserve", "finish"])

    def test_output_tampering_and_completed_reruns_refused(self):
        manifest, outcomes, result = self.run_fixture()
        with self.assertRaises(FileExistsError):
            p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        with self.assertRaisesRegex(ValueError, "completed experiments"):
            p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes), resume=True)
        row = result["outcomes"][0]
        artifact = manifest.parent / "run" / row["attempt_id"] / "outcome.json"
        value = p.decode(artifact.read_bytes())
        value["records"][0]["answers"]["supported"] = "tampered"
        artifact.write_bytes(p.canonical(value))
        with self.assertRaisesRegex(ValueError, "outcome artifact changed"):
            p.report(manifest, manifest.parent / "run")

    def test_ledger_hash_chain_detects_editing(self):
        manifest, _, _ = self.run_fixture()
        ledger = manifest.parent / "run" / "attempts.jsonl"
        lines = ledger.read_bytes().splitlines()
        row = p.decode(lines[1])
        row["payload"]["reservation_usd"] = "0"
        lines[1] = p.canonical(row)
        ledger.write_bytes(b"\n".join(lines) + b"\n")
        with self.assertRaisesRegex(ValueError, "ledger chain changed"):
            p.report(manifest, manifest.parent / "run")

    def test_paid_admission_needs_real_gold_exact_manifest_and_best_effort_acceptance(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        approval = {"schema": p.APPROVAL_SCHEMA, "manifest_sha256": bundle["manifest_sha256"],
                    "max_attempts": 24, "max_allocation_usd": "24", "owner": "SOFTWARE FIXTURE OWNER",
                    "approved_at": p.now(), "accept_best_effort": True}
        with patch.object(p, "HostExecutor", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(ValueError, "independently adjudicated"):
                p.run_experiment(manifest, manifest.parent / "run", live=True, approval=approval)
        # Isolated admission simulation, never a real adjudication and never executed.
        for case in bundle["cases"]:
            case["manifest"]["kind"] = "realistic"
            case["manifest"]["adjudication"]["kind"] = "human"
        self.assertEqual(p.approve(bundle, approval), p.digest(approval))
        for change in ({"manifest_sha256": "0" * 64}, {"max_attempts": 23}, {"max_allocation_usd": "25"}, {"accept_best_effort": False}):
            with self.assertRaises(ValueError):
                p.approve(bundle, {**approval, **change})

    def test_host_argv_excludes_all_other_extensions_and_only_C_loads_candidate(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        executor = p.HostExecutor(bundle)
        cid = bundle["cases"][0]["manifest"]["id"]
        for arm in p.ARMS:
            request = p.request_for(bundle, cid, arm, 0, 0)
            argv = executor.argv(request, self.root, self.root / "session", self.root / "config", "public prompt")
            self.assertIn("--no-extensions", argv)
            self.assertNotIn("--extension", argv)
            extensions = [argv[index + 1] for index, argument in enumerate(argv) if argument == "--trusted-extension"]
            self.assertEqual(extensions, [str(self.root / "observer.ts"), *([bundle["candidate"]["path"]] if arm == "C" else [])])

    def test_frozen_implementation_and_legacy_strategy_gate_stay_separate(self):
        manifest, _ = self.fixture()
        self.mutate(manifest, None, lambda m: m["implementation"].update({"grade.py": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "implementation changed"):
            p.preflight(manifest)
        old = p._shared()
        self.assertEqual(old.PILOT_SCHEMA, "jevify-pilot/1")
        contract = {"controlled": {k: "same" for k in old.CONTRACT_CONTROLLED},
                    "treatment": {k: "same" for k in old.CONTRACT_TREATMENT}}
        self.assertFalse(old.contract_differences(contract, contract)["one_variable"])

    def test_human_labor_requires_frozen_protocol_and_observed_review(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        case = bundle["cases"][0]
        cid = case["manifest"]["id"]
        row = p.decode(outcomes.read_bytes())[f"{cid}:C:0:0"]
        row["human_review"] = {"protocol_sha256": "test-protocol", "reviewer": "fixture-reviewer", "preparation_minutes": 1,
                               "review_minutes": 2, "overturned": 1, "reviewed": 2}
        self.assertIsNone(p.score(case, row, 1, p.account(row["receipts"]), None)["metrics"]["human_minutes"])
        protocol = {"design": "counterbalanced", "protocol_sha256": "test-protocol", "reviewers": ["fixture-reviewer"]}
        scored = p.score(case, row, 1, p.account(row["receipts"]), protocol)
        self.assertEqual(scored["metrics"]["human_minutes"], 3)
        self.assertEqual(scored["metrics"]["reviewer_overturn_rate"], .5)


    def test_resume_uncertain_reservation_never_replays_it(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        executor = p.InjectedExecutor(outcomes)
        directory = manifest.parent / "run"
        directory.mkdir()
        (directory / "manifest.json").write_bytes(p.canonical(bundle["manifest"]))
        header = {"event": "start", "manifest_sha256": bundle["manifest_sha256"], "raw_sha256": bundle["raw_sha256"],
                  "started_at": p.now(), "approval_sha256": None, "execution": executor.identity,
                  "evidence_class": "injected_software_evidence"}
        ledger = directory / "attempts.jsonl"
        previous = p._journal(ledger, header, None)
        cid, arm, rep = p.schedule(bundle)[0]
        request = p.request_for(bundle, cid, arm, rep, 0)
        aid = p.sha(p.canonical(request["identity"]))
        (directory / aid).mkdir()
        (directory / aid / "request.json").write_bytes(p.canonical(request))
        p._journal(ledger, {"event": "reserve", "attempt_id": aid, "case": cid, "arm": arm, "rep": rep, "attempt": 0,
                            "request_sha256": p.digest(request), "reservation_usd": "1"}, previous)
        result = p.run_experiment(manifest, directory, executor=executor, resume=True)
        self.assertEqual(result["stop"]["reason"], "uncertain_prior_attempt")
        self.assertEqual(result["cost"]["attempts"], 1)
        self.assertEqual(result["cost"]["uncertain_attempts"], 1)
        self.assertEqual(result["outcomes"], [])

    def test_invalid_final_answer_preserves_independent_receipts_and_full_reservation(self):
        manifest, outcomes = self.fixture()
        injected = p.InjectedExecutor(outcomes)
        def invalid(request, directory):
            outcome = injected(request, directory)
            outcome["identity"] = {"wrong": "identity"}
            outcome["receipts"][0]["cost_usd"] = "2"
            return outcome
        result = p.run_experiment(manifest, manifest.parent / "run", executor=invalid)
        self.assertGreaterEqual(float(result["cost"]["observed_subtotal_usd"]), 2)
        self.assertGreaterEqual(float(result["stop"]["allocation_consumed_usd"]), 2)
        self.assertIsNone(result["cost"]["observed_total_usd"])
        self.assertEqual(result["cost"]["attempts"], 1)
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")

    def test_adapter_numeric_string_receipts_are_independent_of_host_accounting(self):
        result = {"schema": "jevify-extraction-accounting/1", "request_attempts": 2, "transmission_uncertain": True, "provenance": {"attempts": [
            {"attempt_id": "software-only", "disposition": "complete", "usage": {"cost_usd": "0.00125", "input_tokens": "42", "reported_passes": "2"}},
            {"attempt_id": "uncertain-only", "disposition": "interrupted", "usage": {"cost_usd": None, "input_tokens": None, "reported_passes": None}}
        ]}}
        receipts = p.HostExecutor.extraction_receipts(result)
        self.assertEqual(receipts[0]["cost_usd"], "0.00125")
        self.assertEqual(receipts[0]["cost_evidence"], "provider_reported")
        self.assertEqual(receipts[0]["input_tokens"], 42)
        self.assertEqual(receipts[0]["provider_passes"], 2)
        self.assertEqual(receipts[1]["status"], "failed")
        accounting = p.account(receipts)
        self.assertEqual(accounting["stages"]["extraction"]["reported_subtotal_usd"], "0.00125")
        self.assertFalse(accounting["cost_complete"])
        self.assertFalse(accounting["billing_complete"])
        self.assertEqual(accounting["accounting_basis"], "provider_reported_not_invoice")
        self.assertIsNone(accounting["total_cost_usd"])
        self.assertIsNone(p.provider_count("1.5"))

    def test_secondary_direct_review_is_separate_from_extraction_conclusion(self):
        manifest, outcomes = self.fixture()
        rows = p.decode(outcomes.read_bytes())
        for row in rows.values():
            row["records"][0]["answers"]["supported"] = "qualified_only" if row["identity"]["arm"] == "D" else "unqualified"
            if row["identity"]["arm"] == "D":
                row["observed"]["judge"] = {"mode": "direct_no_judge"}
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, manifest.parent / "run", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")
        self.assertEqual(result["secondary_contrasts"]["direct_verdict"], "direct-review preferred")
        self.assertEqual(result["recommendation"], "inconclusive")

    def test_missing_qualifications_do_not_preserve_decision_credit(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        case = bundle["cases"][0]
        row = p.decode(outcomes.read_bytes())[f'{case["manifest"]["id"]}:C:0:0']
        row["records"][0]["qualifications"] = []
        scored = p.score(case, row, 1, p.account(row["receipts"]), None)
        self.assertEqual(scored["metrics"]["occurrence_accuracy"], 1)
        self.assertEqual(scored["metrics"]["qualification_retention"], 0)
        self.assertEqual(scored["metrics"]["decision_accuracy"], 0)


    def test_authorization_claim_blocks_second_output_and_concurrent_resume(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        with patch.object(p, "AUTHORIZATION_ROOT", self.root / "controller-authorizations"):
            first, second = self.root / "first-ledger", self.root / "second-ledger"
            with p.authorization_claim(bundle, "fixture-approval", first, False):
                with self.assertRaisesRegex(ValueError, "active experiment"):
                    with p.authorization_claim(bundle, "fixture-approval", first, True):
                        self.fail("concurrent resume admitted")
            with self.assertRaisesRegex(ValueError, "already claimed"):
                with p.authorization_claim(bundle, "fixture-approval", second, False):
                    self.fail("same approval reset its budget")
            with self.assertRaisesRegex(ValueError, "another or missing"):
                with p.authorization_claim(bundle, "fixture-approval", second, True):
                    self.fail("resume moved the authorization")
            with p.authorization_claim(bundle, "fixture-approval", first, True):
                pass
            with p.authorization_claim(bundle, "new-fixture-approval", second, False):
                pass

    def test_uncertain_paid_launch_cannot_reset_budget_with_new_output(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        approval = {"schema": p.APPROVAL_SCHEMA, "manifest_sha256": bundle["manifest_sha256"],
                    "max_attempts": 24, "max_allocation_usd": "24", "owner": "SOFTWARE TEST ONLY",
                    "approved_at": p.now(), "accept_best_effort": True}
        calls = []
        def interrupted(request, directory):
            calls.append(request["identity"])
            raise KeyboardInterrupt()
        # This is an admission-boundary simulation: no real gold and no installed-host call.
        with patch.object(p, "AUTHORIZATION_ROOT", self.root / "controller-authorizations"), \
                patch.object(p, "live_evidence_ready", return_value=True), \
                patch.object(p, "HostExecutor", return_value=interrupted):
            result = p.run_experiment(manifest, self.root / "first-paid", live=True, approval=approval)
            self.assertEqual(result["stop"]["reason"], "interrupted")
            with self.assertRaisesRegex(ValueError, "already claimed"):
                p.run_experiment(manifest, self.root / "second-paid", live=True, approval=approval)
        self.assertEqual(len(calls), 1)
        self.assertFalse((self.root / "second-paid").exists())

    def test_semantic_gold_and_deduplicated_sources_collapse_reformatted_aliases(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        alias = copy.deepcopy(bundle["cases"][0])
        original = alias["manifest"]["id"]
        alias["manifest"].update(id="alias-case", unit_id="alias-unit")
        claim = alias["gold"]["claims"].pop(original)
        alias["gold"]["claims"]["renamed-record"] = claim
        alias["manifest"]["gold"]["sha256"] = p.sha(json.dumps(alias["gold"], indent=4).encode())
        for name in ("alias-source-1", "alias-source-2"):
            alias["manifest"]["sources"][name] = copy.deepcopy(alias["manifest"]["sources"]["comment"])
            alias["sources"][name] = copy.deepcopy(alias["sources"]["comment"])
        claim["occurrences"][0]["source_id"] = "alias-source-2"
        bundle["cases"].append(alias)
        self.assertEqual(len(p.independent_units(bundle)), 3)
        self.assertIn(sorted([original, "alias-case"]), p.independent_units(bundle))

    def test_identical_gold_stays_aliased_after_all_sources_are_padded(self):
        manifest, _ = self.fixture()
        value = p.decode(manifest.read_bytes())
        alias = copy.deepcopy(value["cases"][0])
        original = alias["id"]
        alias.update(id="padded-case", unit_id="padded-unit")
        for sid, source in alias["sources"].items():
            source_path = manifest.parent / source["file"]["path"]
            padded_path = manifest.parent / f"padded-{sid}.txt"
            padded_path.write_bytes(source_path.read_bytes() + b"\n")
            source["file"] = p.file_ref(padded_path, manifest.parent)
        alias["adjudication"]["source_inventory_sha256"] = p.digest(alias["sources"])
        value["cases"].append(alias)
        manifest.write_bytes(p.canonical(value))
        result = p.preflight(manifest)
        self.assertEqual(result["independent_cases"], 3)
        self.assertIn(sorted([original, "padded-case"]), result["aliases"])

    def test_unfinished_reservation_makes_all_stage_counts_incomplete(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        executor = p.InjectedExecutor(outcomes)
        directory = self.root / "partial-ledger"
        directory.mkdir()
        (directory / "manifest.json").write_bytes(p.canonical(bundle["manifest"]))
        header = {"event": "start", "manifest_sha256": bundle["manifest_sha256"], "raw_sha256": bundle["raw_sha256"],
                  "started_at": p.now(), "approval_sha256": None, "execution": executor.identity,
                  "evidence_class": "injected_software_evidence"}
        ledger = directory / "attempts.jsonl"
        previous = p._journal(ledger, header, None)
        for index, (cid, arm, rep) in enumerate(p.schedule(bundle)[:2]):
            request = p.request_for(bundle, cid, arm, rep, 0)
            aid = p.digest(request["identity"])
            attempt = directory / aid
            attempt.mkdir()
            (attempt / "request.json").write_bytes(p.canonical(request))
            previous = p._journal(ledger, {"event": "reserve", "attempt_id": aid, "case": cid, "arm": arm, "rep": rep,
                "attempt": 0, "request_sha256": p.digest(request), "reservation_usd": "1"}, previous)
            if index == 0:
                outcome = executor(request, attempt)
                (attempt / "outcome.json").write_bytes(p.canonical(outcome))
                validation = p.validate_outcome(outcome, request)
                previous = p._journal(ledger, {"event": "finish", "attempt_id": aid, "outcome_sha256": p.digest(outcome),
                    "latency_seconds": 1, "accounting": validation["accounting"], "complete": True,
                    "validation": validation, "error": None, "artifacts": p.artifact_hashes(attempt)}, previous)
        result = p.report(manifest, directory)
        self.assertEqual(result["cost"]["uncertain_attempts"], 1)
        self.assertGreater(result["cost"]["stages"]["completion"]["input_tokens"], 0)
        for stage in result["cost"]["stages"].values():
            self.assertFalse(stage["tokens_complete"])
            self.assertFalse(stage["passes_complete"])
            self.assertFalse(stage["cost_complete"])

    def test_direct_metrics_exclude_cases_where_direct_review_is_inapplicable(self):
        manifest, outcomes = self.fixture()
        def applicable(m):
            m["cases"][-1]["direct_suitable"] = False
            m["thresholds"]["min_cases"] = 2
        self.mutate(manifest, outcomes, applicable)
        rows = p.decode(outcomes.read_bytes())
        for row in rows.values():
            row["records"][0]["answers"]["supported"] = "qualified_only" if row["identity"]["arm"] == "D" else "unqualified"
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, self.root / "direct-subset", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["secondary_contrasts"]["B_to_D"]["decision_accuracy"]["n"], 2)
        self.assertEqual(result["absolute_means"]["D"]["decision_accuracy"], 1)
        self.assertEqual(result["secondary_contrasts"]["direct_verdict"], "direct-review preferred")

    def test_unsupported_judge_effort_is_refused_before_host_launch(self):
        for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                manifest, _ = self.fixture(name=effort)
                value = p.decode(manifest.read_bytes())
                control_path = manifest.parent / value["controls"]["path"]
                controls = p.decode(control_path.read_bytes())
                controls["judge"]["thinking"] = effort
                control_path.write_bytes(p.canonical(controls))
                value["controls"] = p.file_ref(control_path, manifest.parent)
                for treatment in value["treatments"].values():
                    treatment["controls_sha256"] = value["controls"]["sha256"]
                manifest.write_bytes(p.canonical(value))
                with patch.object(p.subprocess, "Popen") as launch:
                    with self.assertRaisesRegex(ValueError, "judgment disables reasoning"):
                        p.run_experiment(manifest, self.root / f"run-{effort}", live=True, approval={})
                    launch.assert_not_called()
                self.assertFalse((self.root / f"run-{effort}").exists())

    def test_matching_judge_is_admitted_but_ambient_judge_is_rejected(self):
        manifest, outcomes = self.fixture()
        bundle = p.load_manifest(manifest)
        rows = p.decode(outcomes.read_bytes())
        cid = bundle["cases"][0]["manifest"]["id"]
        request = p.request_for(bundle, cid, "B", 0, 0)
        row = copy.deepcopy(rows[f"{cid}:B:0:0"])
        self.assertTrue(p.validate_outcome(row, request)["provenance_complete"])
        row["observed"]["judge"]["models"] = ["ambient/wrong-judge"]
        self.assertFalse(p.validate_outcome(row, request)["provenance_complete"])
        judge = {**request["requested"]["judge"], "fallback_models": ["provider/fallback"]}
        request["requested"]["judge"] = request["controls"]["judge"] = judge
        row["observed"]["judge"] = {"configuration": copy.deepcopy(judge), "models": ["provider/fallback"]}
        self.assertTrue(p.validate_outcome(row, request)["provenance_complete"])
        row["observed"]["judge"]["configuration"]["thinking"] = "high"
        self.assertFalse(p.validate_outcome(row, request)["provenance_complete"])
        for row in rows.values():
            if row["identity"]["arm"] != "D":
                row["observed"]["judge"]["models"] = ["ambient/wrong-judge"]
        outcomes.write_bytes(p.canonical(rows))
        result = p.run_experiment(manifest, self.root / "wrong-judge", executor=p.InjectedExecutor(outcomes))
        self.assertEqual(result["primary_contrast"]["gate_verdict"], "inconclusive")
        self.assertFalse(result["gates"]["complete_outcomes"])
        self.assertEqual(result["stop"]["reason"], "incomplete_evidence")

    def test_record_original_dependencies_and_passages_survive_preparation(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        cid = bundle["cases"][0]["manifest"]["id"]
        request = p.request_for(bundle, cid, "C", 0, 0)
        other = copy.deepcopy(request["sources"]["comment"])
        other.update(subject="other-subject", version="v3", revision="v3")
        request["sources"]["other"] = other
        request["passages"].append({**request["passages"][0], "source_id": "other", "revision": "v3"})
        request["inventory"].append({"id": "other-record", "subject": "other-subject", "revision": "v3", "required_questions": ["supported"],
                                     "source_id": "other", "dependencies": ["other"], "passages": {arm: [1] for arm in p.ARMS}})
        records, fields, sources = p.HostExecutor.evidence_inputs(request)
        self.assertEqual([record["source_id"] for record in records], ["comment", "other"])
        self.assertEqual([record["dependencies"] for record in records], [["comment"], ["other"]])
        self.assertEqual(records[1]["facts"]["retrieved_passages"], [request["passages"][1]])
        self.assertEqual(sources[1]["subject"], "other-subject")
        self.assertEqual(sources[1]["version"], "v3")
        self.assertEqual(fields["evidence"]["description"], request["controls"]["fields"]["evidence"]["instructions"])

    def test_invalid_record_passage_binding_refuses_before_launch(self):
        manifest, _ = self.fixture()
        m = p.decode(manifest.read_bytes())
        ref = m["cases"][0]["inventory"]
        path = manifest.parent / ref["path"]
        records = p.decode(path.read_bytes())
        records[0]["passages"]["B"] = records[0]["passages"]["C"] = [99]
        path.write_bytes(p.canonical(records))
        m["cases"][0]["inventory"] = p.file_ref(path, manifest.parent)
        manifest.write_bytes(p.canonical(m))
        with self.assertRaisesRegex(ValueError, "outside its declared"):
            p.preflight(manifest)

    def controller_event(self, plan, published=True, raw_result=None):
        raw_result = raw_result or {"schema": "jevify-evidence-result/1", "plan_digest": plan["plan_digest"], "status": "error", "records": {}}
        result = {"schema": "jevify-extraction-accounting/1", "plan_digest": plan["plan_digest"],
                  "run_id": "00000000-0000-4000-8000-000000000000", "generation": 0, "disposition": "complete",
                  "records": len(raw_result["records"]), "fields": 1, "candidates": 0, "request_attempts": 1,
                  "reused": False, "transmission_uncertain": not published,
                  "result_sha256": p.digest(raw_result) if published else None,
                  "provenance": {"attempts": [{"disposition": "complete", "usage": {"cost_usd": "0.00125", "input_tokens": "42", "reported_passes": "2"}}]}}
        receipt = {"schema": "jevify-extraction-receipt/1", "tool_call_id": "fixture-call", "plan_digest": plan["plan_digest"],
                   "artifact_root": plan["artifact_root"], "result_sha256": p.digest(result), "result": result, "published": published}
        call = {"id": "fixture-call", "tool": "jevify_extract", "args": {"artifact_root": plan["artifact_root"], "plan_digest": plan["plan_digest"]}}
        event = {"type": "tool_execution_end", "toolName": "jevify_extract", "toolCallId": "fixture-call", "isError": not published,
                 "result": {"details": {"controllerReceipt": receipt}}}
        return call, event

    def test_forged_or_overwritten_result_cannot_supply_provider_accounting(self):
        plan = {"plan_digest": "a" * 64, "artifact_root": str(self.root / "model-artifacts")}
        call, event = self.controller_event(plan)
        result_path = self.root / "model-result.json"
        result_path.write_bytes(p.canonical({"provenance": {"attempts": [{"usage": {"cost_usd": "999999"}}]}}))
        with self.assertRaisesRegex(ValueError, "controller extraction receipt"):
            p.HostExecutor.capture_extraction({**event, "result": {}}, call, plan, self.root, 0)
        event["isError"] = True  # A published error result can still contain provider-reported usage.
        captured = p.HostExecutor.capture_extraction(event, call, plan, self.root, 0)
        result_path.write_text('{"overwritten":"anything"}')
        self.assertEqual(p.HostExecutor.extraction_receipts(captured["result"])[0]["cost_usd"], "0.00125")
        event["result"]["details"]["controllerReceipt"]["result"]["provenance"]["attempts"][0]["usage"]["cost_usd"] = "999999"
        with self.assertRaisesRegex(ValueError, "result binding"):
            p.HostExecutor.capture_extraction(event, call, plan, self.root, 1)
        self.assertFalse((self.root / "extraction-receipt-1.json").exists())

    def test_sanitized_accounting_hash_is_distinct_from_private_result_hash(self):
        plan = {"plan_digest": "b" * 64, "artifact_root": str(self.root / "private-model-artifacts")}
        raw_result = {"schema": "jevify-evidence-result/1", "plan_digest": plan["plan_digest"], "status": "complete",
                      "records": {"record": {"observations": {"field": {"value": "PRIVATE SOURCE VALUE MUST STAY OFF TOOL EVENT"}}}}}
        call, event = self.controller_event(plan, raw_result=raw_result)
        captured = p.HostExecutor.capture_extraction(event, call, plan, self.root, 0)
        self.assertNotIn(b"PRIVATE SOURCE VALUE", p.canonical(captured))
        self.assertNotEqual(captured["result_sha256"], captured["result"]["result_sha256"])
        result_path = Path(plan["artifact_root"]) / "results" / f'{plan["plan_digest"]}.json'
        result_path.parent.mkdir(parents=True)
        result_path.write_bytes(p.canonical(raw_result))
        p.HostExecutor.verify_extraction_publication(captured, plan)
        result_path.write_bytes(p.canonical(captured["result"]))
        with self.assertRaisesRegex(ValueError, "differs from controller"):
            p.HostExecutor.verify_extraction_publication(captured, plan)
        self.assertEqual(p.HostExecutor.extraction_receipts(captured["result"])[0]["cost_usd"], "0.00125")

    def test_provider_reported_completeness_never_claims_invoice_and_excludes_estimates(self):
        manifest, outcomes = self.fixture("ties")
        row = next(row for row in p.decode(outcomes.read_bytes()).values() if row["identity"]["arm"] == "C")
        receipts = row["receipts"]
        totals = p.account(receipts)
        self.assertTrue(totals["cost_complete"])
        self.assertFalse(totals["billing_complete"])
        self.assertEqual(totals["accounting_basis"], "provider_reported_not_invoice")
        extraction = next(receipt for receipt in receipts if receipt["stage"] == "extraction")
        extraction["cost_evidence"] = "catalog_estimate"
        totals = p.account(receipts)
        self.assertFalse(totals["cost_complete"])
        self.assertIsNone(totals["total_cost_usd"])
        self.assertEqual(totals["stages"]["extraction"]["reported_subtotal_usd"], "0")
        self.assertEqual(totals["catalog_estimate_subtotal_usd"], "0.01")
        extraction.update(cost_evidence="provider_reported", cost_usd="0")
        self.assertFalse(p.account(receipts)["cost_complete"])
        extraction.update(cost_evidence="provider_billed", cost_usd="0.01")
        with self.assertRaisesRegex(ValueError, "invalid usage provenance"):
            p.account(receipts)

    def test_initial_effective_thinking_and_fixed_effort_events_reach_outcome(self):
        for source in ("observer_initial", "fixed_effort_event"):
            with self.subTest(source=source):
                manifest, outcomes = self.fixture(name=source)
                bundle = p.load_manifest(manifest)
                bundle["manifest"]["candidate"] = {"root": str(ROOT), "entry": "src/index.ts", "files": {}}
                cid = bundle["cases"][0]["manifest"]["id"]
                request = p.request_for(bundle, cid, "D", 0, 0)
                expected = p.decode(outcomes.read_bytes())[f"{cid}:D:0:0"]
                directory = self.root / f"controller-{source}"
                directory.mkdir()
                observation = {"type": "preparation_observation", "schema": "jevify-host-observation/1", "session_id": "initial-session",
                               "thinking": "high", "usage": [], "final": False}
                events = [observation] if source == "observer_initial" else [{"type": "thinking_level_changed", "thinkingLevel": "high"}]
                answer = {key: expected[key] for key in ("records", "processed_passages")}
                events += [{"type": "message_end", "message": {"role": "assistant", "model": "fixture-model", "stopReason": "stop",
                            "content": [{"type": "text", "text": p.canonical(answer).decode()}], "usage": {"input": 11}}}]
                if source == "observer_initial":
                    events.append({**observation, "final": True})
                events.append({"type": "agent_end"})
                def launch(*args, **kwargs):
                    read_fd, write_fd = os.pipe()
                    os.write(write_fd, b"".join(p.canonical(row) + b"\n" for row in events))
                    os.close(write_fd)
                    return SimpleNamespace(stdout=os.fdopen(read_fd, "rb"), returncode=0, wait=lambda **kwargs: 0)
                shared = p._shared()
                with patch.object(p, "_shared", return_value=shared), patch.object(p.subprocess, "Popen", side_effect=launch), \
                        patch.object(shared, "kill_tree"), \
                        patch("threading.Timer", return_value=SimpleNamespace(start=lambda: None, cancel=lambda: None)):
                    result = p.HostExecutor(bundle)(request, directory)
                self.assertEqual(result["observed"]["thinking"], "high")
                self.assertTrue(p.validate_outcome(result, request)["provenance_complete"])

    def test_keyboard_interrupt_drains_late_receipt_and_ignores_forged_disk_usage(self):
        manifest, _ = self.fixture()
        bundle = p.load_manifest(manifest)
        bundle["manifest"]["candidate"] = {"root": str(ROOT), "entry": "src/index.ts",
            "files": {"skills/jevify/evidence.py": p.sha((ROOT / "skills/jevify/evidence.py").read_bytes())}}
        bundle["candidate"] = {"path": str(ROOT / "src/index.ts"), "sha256": "fixture", "bundle_sha256": "fixture"}
        request = p.request_for(bundle, bundle["cases"][0]["manifest"]["id"], "C", 0, 0)
        executor = p.HostExecutor(bundle)
        directory = self.root / "controller-attempt"
        directory.mkdir()
        shared = p._shared()
        workspaces = []
        test = self
        def launch(argv, *, cwd, **kwargs):
            workspaces.append(cwd)
            session = Path(argv[argv.index("--session-dir") + 1])
            forged = {"type": "model_usage", "id": "forged-judge", "timestamp": p.now(), "purpose": "judge_batch", "role": "judge",
                      "api": "fixture", "provider": "forged", "model": "judge", "stopReason": "stop", "estimated": False,
                      "usage": {"input": 999999, "cost": {"total": 999999}}}
            rows = [{"type": "session", "id": "fixture-session", "cwd": str(cwd)}, forged]
            (session / "fixture.jsonl").write_bytes(b"".join(p.canonical(row) + b"\n" for row in rows))
            plan = p.decode(next((cwd / ".jevify-evidence" / "plans").glob("*.json")).read_bytes())
            call, event = test.controller_event(plan, published=False)
            initial = {"type": "preparation_observation", "schema": "jevify-host-observation/1", "session_id": "fixture-session",
                       "thinking": "high", "usage": [], "final": False}
            usage = {key: value for key, value in forged.items() if key not in ("type", "timestamp")}
            usage.update(id="observed-judge", provider="fixture", usage={"input": 7, "cost": {"total": .25}})
            late_observation = {**initial, "usage": [usage]}
            events = [initial, {"type": "message_end", "message": {"role": "assistant", "model": "fixture-model", "stopReason": "toolUse",
                       "content": [], "usage": {"input": 11, "cost": {"total": .5}}}},
                      {"type": "tool_execution_start", "toolName": "jevify_extract", "toolCallId": call["id"], "args": call["args"]}]
            read_fd, write_fd = os.pipe()
            stream = os.fdopen(read_fd, "rb")
            completion = p.canonical(event) + b"\n"
            split = len(completion) // 2
            os.write(write_fd, b"".join(p.canonical(row) + b"\n" for row in events) + completion[:split])
            def interrupted_wait(**kwargs):
                raise KeyboardInterrupt()
            return SimpleNamespace(stdout=stream, returncode=None, wait=interrupted_wait, write_fd=write_fd,
                                   remaining=completion[split:] + p.canonical(late_observation) + b"\n")
        def terminate(proc):
            if proc.returncode is None:
                os.write(proc.write_fd, proc.remaining)
                os.close(proc.write_fd)
                proc.returncode = -2
        with patch.object(p, "_shared", return_value=shared), patch.object(p.subprocess, "Popen", side_effect=launch), \
                patch.object(shared, "kill_tree", side_effect=terminate), \
                patch("threading.Timer", return_value=SimpleNamespace(start=lambda: None, cancel=lambda: None)):
            with self.assertRaises(KeyboardInterrupt):
                executor(request, directory)
        receipts = p.decode((directory / "captured-receipts.json").read_bytes())
        totals = p.account(receipts)
        self.assertEqual(totals["stages"]["extraction"]["reported_subtotal_usd"], "0.00125")
        self.assertEqual(totals["stages"]["judgment"]["reported_subtotal_usd"], "0.25")
        self.assertEqual(totals["stages"]["completion"]["input_tokens"], 11)
        self.assertEqual(totals["stages"]["judgment"]["input_tokens"], 7)
        self.assertTrue(totals["stages"]["judgment"]["cost_unknown"])
        self.assertFalse(totals["cost_complete"])
        self.assertNotIn(b"999999", p.canonical(receipts))
        self.assertEqual(p.decode((directory / "host-observation-0.json").read_bytes())["thinking"], "high")
        self.assertTrue((directory / "extraction-receipt-0.json").exists())
        self.assertTrue(workspaces)
        self.assertFalse(workspaces[0].exists())


if __name__ == "__main__":
    unittest.main()
