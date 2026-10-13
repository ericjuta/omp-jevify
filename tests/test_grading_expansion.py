"""Behavioral contracts for grading expansions: route provenance, extractor validation,
calibration effects, off-inventory boundaries, controls, immutable inputs, cache-isolated
launch evidence, and paired comparison (evals/grade.py, evals/grade_launch.py)."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"jevify_grading_{name}", ROOT / "evals" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grade = _load("grade")
run = _load("run")
launch = _load("grade_launch")

GOOD_ANSWERS = {"factual": {"choice": "supported"}, "action": {"choice": "fix_now"}, "introduced": {"choice": "yes"}}
SYNTHETIC = {"kind": "source_adjudicated_synthetic", "human_adjudicated": False, "basis": "unit test"}
EXTERNAL_HUMAN = {"kind": "external_human", "human_adjudicated": True, "adjudicator": "reviewer-1",
                  "adjudicated_at": "2026-10-11", "basis": "blind review"}


def _gold(factual="supported", action="fix_now", introduced="yes"):
    return {"factual": factual, "action": action, "introduced": introduced}


def _record(rid, category, gold, *, answer="answer text", provenance=SYNTHETIC):
    claim = "C" + rid.upper()
    return {"id": rid, "claim": claim, "category": category, "item": f"review claim {claim} (unit): item {rid}",
            "answer": answer, "gold": gold, "gold_basis": "unit test", "provenance": provenance}


def _grader_ids():
    return {"source_sha256": "g", "cases_sha256": "c", "extraction_rubric_sha256": {"1": "r1", "2": "r2"},
            "fixture_sources": {"fixture.py": "f1", "refund_retry_fixture.py": "f2"},
            "scoring_revision": grade.SCORING_REVISION, "historical_revision": grade.HISTORICAL_REVISION}


def _batch_record(count):
    return {"id": "b", "kind": "judgment_batch", "items": count, "model": "m", "cost_usd": None,
            "cost_basis": "batch_status_estimate", "billing_complete": False, "cleanup_errors": [], "cache": {}}


class _Item:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _StreamBatch:
    """A completed judge_batch handle that streams item objects, as the host helper does."""
    id = "stream-batch"

    def __init__(self, items, answers):
        self._items, self._answers = items, answers

    async def drain_iter(self, seconds):
        for identifier, item in self._items.items():
            yield identifier, item

    async def drain(self, seconds):
        raise AssertionError("a streaming handle must not fall back to drain")

    def status(self):
        return {"done": len(self._answers), "running": False, "cost": 0.5, "model": "judge-x", "cache": "miss"}

    def results(self):
        return self._answers

    def failed(self):
        return {}

    def cancel(self):
        pass

    def close(self):
        pass


def _known_route(**overrides):
    fields = {"selected_model": "m-1", "primary_model": "m-1", "response_model": "m-1", "fallback": False,
              "cache": "hit", "questions": 3, "cached_questions": 3}
    fields.update(overrides)
    return fields


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_item_routes_are_kept_and_inconsistent_fields_stay_unknown(self):
        states = {"a": {"id": "a"}, "b": {"id": "b"}, "c": {"id": "c"}}
        answers = {sid: dict(GOOD_ANSWERS) for sid in states}
        items = {
            "a": _Item(**_known_route()),
            "b": _Item(**_known_route(fallback=True)),  # fallback claimed although selected == primary
            "c": _Item(**_known_route(cache=None)),      # cache not reported
        }
        batch = _StreamBatch(items, answers)
        with mock.patch.object(grade, "judge_batch", return_value=batch, create=True):
            _, _, record = await grade._judge(states, grade.FACET_QUESTIONS, 10, 1)
        provenance = record["provenance"]
        self.assertFalse(provenance["a"]["provenance_unknown"])
        self.assertEqual(provenance["a"]["selected_model"], "m-1")
        self.assertEqual(provenance["a"]["source"], "host_item")
        self.assertTrue(provenance["b"]["provenance_unknown"])
        self.assertTrue(provenance["c"]["provenance_unknown"])
        self.assertIsNone(provenance["c"]["cache"])
        self.assertEqual(record["provenance_summary"], {"items": 3, "known": 1, "unknown": 2, "item_stream": True})

    async def test_handle_without_item_stream_reports_unknown_and_never_infers_from_status(self):
        batch = mock.Mock()
        batch.id = "plain-batch"
        batch.drain = mock.AsyncMock()
        batch.status.return_value = {"done": 1, "running": False, "cost": 0.1, "model": "judge-x", "cache": "hit"}
        batch.results.return_value = {"slot": dict(GOOD_ANSWERS)}
        batch.failed.return_value = {}
        states = {"slot": {"id": "x", "item": "Synthetic claim", "answer": "text"}}
        with mock.patch.object(grade, "judge_batch", return_value=batch, create=True):
            labels, errors, record = await grade._judge(states, grade.FACET_QUESTIONS, 10, 1)
        self.assertEqual(labels["slot"]["factual"], "supported")
        self.assertEqual(record["provenance"]["slot"]["source"], "host_item_stream_unavailable")
        self.assertTrue(record["provenance"]["slot"]["provenance_unknown"])
        self.assertIsNone(record["provenance"]["slot"]["selected_model"])
        self.assertEqual(record["provenance_summary"]["known"], 0)


class ValidateExtractorTests(unittest.TestCase):
    def _corpus(self, records, provenance=SYNTHETIC):
        return {"schema": 1, "provenance": provenance, "records": records}

    def test_confusion_denominators_and_high_impact_false_repair_are_exact(self):
        records = [
            _record("r1", "exact_id", _gold()),
            _record("r2", "exact_id", _gold("supported", "no_action", "no")),
            _record("r3", "omission", _gold()),
        ]
        extractions = {
            "r1": _gold(),
            "r2": {"factual": "supported", "action": "fix_now", "introduced": "no"},
            # r3 has no extraction: every facet is an unresolved error.
        }
        report = grade.validate_extractor(self._corpus(records), extractions)
        self.assertEqual(report["facets"]["factual"]["correct"], 2)
        self.assertEqual(report["facets"]["action"]["correct"], 1)
        self.assertAlmostEqual(report["facets"]["action"]["accuracy"], 0.333, places=3)
        self.assertEqual(report["facets"]["action"]["unresolved"], 1)
        self.assertEqual(report["facets"]["action"]["confusion"]["no_action"]["fix_now"], 1)
        self.assertEqual(report["facets"]["action"]["confusion"]["fix_now"]["unresolved"], 1)
        self.assertEqual(report["false_repairs"], [{"id": "r2", "gold_action": "no_action", "high_impact": True}])
        self.assertEqual(report["high_impact_false_repairs"], ["r2"])
        self.assertEqual(report["missed_repairs"], [{"id": "r3", "predicted_action": "unresolved"}])
        self.assertEqual(report["unresolved_records"], ["r3"])
        self.assertEqual(report["categories"]["exact_id"]["records"], 2)
        self.assertEqual(report["categories"]["exact_id"]["action"]["correct"], 1)
        self.assertFalse(report["quality_claim"])

    def test_synthetic_labels_never_count_as_human_adjudication(self):
        records = [_record("r1", "exact_id", _gold())]
        report = grade.validate_extractor(self._corpus(records), {"r1": _gold()})
        self.assertEqual(report["provenance"]["human_adjudicated_records"], 0)
        self.assertEqual(report["provenance"]["records_by_kind"], {"source_adjudicated_synthetic": 1})

    def test_external_human_needs_a_named_adjudicator_and_is_counted_only_when_declared(self):
        unnamed = {**EXTERNAL_HUMAN, "adjudicator": ""}
        with self.assertRaisesRegex(ValueError, "adjudicator"):
            grade.validate_extractor(self._corpus([_record("r1", "exact_id", _gold(), provenance=unnamed)], unnamed),
                                     {"r1": _gold()})
        mislabelled = {**SYNTHETIC, "human_adjudicated": True}
        with self.assertRaisesRegex(ValueError, "human_adjudicated"):
            grade.validate_extractor(self._corpus([_record("r1", "exact_id", _gold(), provenance=mislabelled)]),
                                     {"r1": _gold()})
        report = grade.validate_extractor(
            self._corpus([_record("r1", "exact_id", _gold(), provenance=EXTERNAL_HUMAN)], EXTERNAL_HUMAN),
            {"r1": _gold()})
        self.assertEqual(report["provenance"]["human_adjudicated_records"], 1)

    def test_host_validation_sends_the_claim_id_and_keys_each_record_uniquely(self):
        corpus_path = ROOT / "evals" / "extractor-corpus.json"
        corpus = json.loads(corpus_path.read_text())
        records = {record["id"]: record for record in corpus["records"]}
        gold = {rid: record["gold"] for rid, record in records.items()}
        sent = {}

        async def judge(states, questions, timeout, concurrency):
            sent.update(states)
            return {sid: dict(gold[sid]) for sid in states}, {}, _batch_record(len(states))

        with mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)):
            report = asyncio.run(grade.validate_extractor_host(corpus_path))
        # Absent claim ids are omitted and never judged; only bound records reach the host.
        self.assertEqual(set(sent), set(records) - ABSENT_RECORDS)
        for rid, state in sent.items():
            self.assertEqual(state["id"], records[rid]["claim"], rid)
            self.assertEqual(state["item"], records[rid]["item"], rid)
        self.assertEqual(sent["r01"]["id"], sent["r07"]["id"])  # one claim, two records, distinct keys
        self.assertEqual(report["facets"]["action"]["accuracy"], 1.0)

    def test_host_validation_scores_judged_labels_and_keeps_extraction_provenance(self):
        corpus_path = ROOT / "evals" / "extractor-corpus.json"
        corpus = json.loads(corpus_path.read_text())
        labels = {record["id"]: record["gold"] for record in corpus["records"]}
        provenance = {rid: {"provenance_unknown": True, "source": "host_item_stream_unavailable"} for rid in labels}
        batch = {"id": "b", "kind": "judgment_batch", "items": len(labels), "provenance": provenance,
                 "cost_usd": None, "cost_basis": "batch_status_estimate", "billing_complete": False,
                 "cleanup_errors": [], "cache": {}, "model": "judge-x"}
        with mock.patch.object(grade, "_judge", mock.AsyncMock(return_value=(labels, {}, batch))):
            report = asyncio.run(grade.validate_extractor_host(corpus_path))
        self.assertEqual(report["facets"]["action"]["accuracy"], 1.0)
        self.assertEqual(report["false_repairs"], [])
        self.assertEqual(report["extraction"]["provenance"]["r01"]["source"], "host_item_stream_unavailable")
        self.assertNotIn("provenance", report["extraction"]["batch"])


def _finding(iid, defect, *, factual="supported", actions=("fix_now",), introduced="yes"):
    """Tiny schema2 key item, independent of the generated datasets and private keys."""
    return {"id": iid, "desc": f"Synthetic claim {iid}", "factual": factual,
            "actions": list(actions), "introduced": introduced, "defect": defect,
            "group": "local", "evidence": ["local:1"], "rationale": "Synthetic regression key."}


# Corpus records whose answers never name their claim id exactly (r02, r03, r04, r06 retained failures; r10, r12 silent).
ABSENT_RECORDS = {"r02", "r03", "r04", "r06", "r10", "r12"}
RETAINED_FAILURES = ("r02", "r03", "r04", "r06")
# The prior extractor's erroneous output for the retained failures: fix_now, which smoke recorded as false repairs.
PRIOR_FAILURE = {"factual": "supported", "action": "fix_now", "introduced": "yes"}
OMITTED = {"factual": "omitted", "action": "omitted", "introduced": "omitted"}


class EvidenceBindingTests(unittest.TestCase):
    INVENTORY = frozenset(f"C{number:02d}" for number in range(1, 25))

    def test_inventory_matches_the_refund_key_and_family_is_detected(self):
        self.assertEqual(grade._refund_claim_inventory(), set(self.INVENTORY))
        self.assertEqual(grade._id_family(self.INVENTORY), "C")
        self.assertIsNone(grade._id_family(frozenset({"debt", "bug"})))

    def test_retained_failure_patterns_are_absent_or_nonexact(self):
        cases = {
            ("C01 is supported and must be fixed now; HEAD introduced it.", "C02"),  # r02: sibling named
            ("C99 is a fix-now defect and HEAD introduced it.", "C07"),  # r03: off-inventory named
            ("C1 is supported and fix_now; HEAD introduced it.", "C01"),  # r04: nonexact near-miss
            ("C01, C02 and C04 are one defect. All three are supported, must be fixed now.", "C06"),  # r06
            ("c01 is supported.", "C01"),  # case-sensitive exact id
            ("C010 is supported.", "C01"),  # token boundary
        }
        for answer, claim in cases:
            with self.subTest(answer=answer, claim=claim):
                status, reason = grade._evidence_binding(answer, claim, self.INVENTORY)
                self.assertEqual(status, "absent")
                self.assertIn(repr(claim), reason)

    def test_valid_exact_and_explicit_group_attribution_is_judged_on_its_own_sentences(self):
        whole = "C01 is supported. It should be fixed now, before release. HEAD introduced it: the stored-id branch sends a second create under a new key."
        group = "C01, C02 and C04 are one defect. All three are supported, must be fixed now, and HEAD introduced them."
        contradiction = "C01 is supported and should be fixed now; HEAD introduced it. Reconsidering, C01 is refuted and needs no action."
        self.assertEqual(grade._evidence_binding(whole, "C01", self.INVENTORY), ("judged", whole))
        self.assertEqual(grade._evidence_binding(group, "C02", self.INVENTORY), ("judged", group))
        self.assertEqual(grade._evidence_binding(contradiction, "C01", self.INVENTORY), ("judged", contradiction))
        self.assertEqual(grade._evidence_binding("C01 is supported. C02 is a separate topic.", "C02", self.INVENTORY),
                         ("judged", "C02 is a separate topic."))

    def test_cross_claim_off_inventory_and_repeated_ids_are_unattributed(self):
        cases = (
            ("C01 is supported; C02 is refuted.", "C01"),  # two claims, no explicit group
            ("C01 is supported and must be fixed now; C99 is a separate defect.", "C01"),  # off-inventory in span
            ("C01 and C02 are supported, C02 is refuted.", "C02"),  # repeated id outside the list
            ("C01 is supported.", "C99"),  # claim outside the inventory
        )
        for answer, claim in cases:
            with self.subTest(answer=answer, claim=claim):
                status, reason = grade._evidence_binding(answer, claim, self.INVENTORY)
                self.assertEqual(status, "unattributed")
                self.assertTrue(reason)

    def test_non_family_inventories_bind_by_exact_token_only(self):
        inventory = frozenset({"debt", "bug"})
        self.assertEqual(grade._evidence_binding("Fix the introduced bug.", "bug", inventory),
                         ("judged", "Fix the introduced bug."))
        self.assertEqual(grade._evidence_binding("Fix the introduced bug.", "debt", inventory)[0], "absent")

    def test_disjunction_is_never_an_explicit_group(self):
        answer = "C06 or C07 is a second settlement that must be fixed now; HEAD introduced it."
        for claim in ("C06", "C07"):
            with self.subTest(claim=claim):
                status, reason = grade._evidence_binding(answer, claim, self.INVENTORY)
                self.assertEqual(status, "unattributed")
                self.assertIn("outside an explicit group", reason)

    def test_repeated_or_off_inventory_member_breaks_a_group(self):
        for answer in ("C01, C02 and C01 are one defect; fix now.", "C01, C02 and C99 are one defect; fix now."):
            with self.subTest(answer=answer):
                self.assertEqual(grade._evidence_binding(answer, "C01", self.INVENTORY)[0], "unattributed")

    def test_explicit_groups_in_serial_bullet_bold_table_and_ampersand_forms_are_judged(self):
        forms = (
            ("C01, C02, and C04 are one defect. All three are supported, must be fixed now.", "C04",
             "C01, C02, and C04 are one defect. All three are supported, must be fixed now."),
            ("- C01, C02 and C04: supported, fix now, introduced by HEAD.", "C02",
             "- C01, C02 and C04: supported, fix now, introduced by HEAD."),
            ("**C01, C02 and C04** are one defect. All three are supported, must be fixed now.", "C01",
             "**C01, C02 and C04** are one defect. All three are supported, must be fixed now."),
            ("| C01, C02, C04 | supported | fix_now | yes |", "C04", "| C01, C02, C04 | supported | fix_now | yes |"),
            ("C01 & C02 & C04 are one defect; all three must be fixed now.", "C02",
             "C01 & C02 & C04 are one defect; all three must be fixed now."),
        )
        for answer, claim, span in forms:
            with self.subTest(answer=answer, claim=claim):
                self.assertEqual(grade._evidence_binding(answer, claim, self.INVENTORY), ("judged", span))

    def test_continuation_stops_at_paragraph_heading_and_list_boundaries(self):
        answer = ("## C01\nSupported; fix now; HEAD introduced it.\n\n"
                  "## Lost-ack claims\nAll of these are refuted and need no action.\n- C06\n- C07")
        self.assertEqual(grade._blocks(answer), ["## C01 Supported; fix now; HEAD introduced it.",
                                                 "## Lost-ack claims All of these are refuted and need no action.",
                                                 "- C06", "- C07"])
        self.assertEqual(grade._evidence_binding(answer, "C01", self.INVENTORY),
                         ("judged", "## C01 Supported; fix now; HEAD introduced it."))
        # '- C06' is a bare label with no assertion of its own: unattributed, never a label-only judged span.
        self.assertEqual(grade._evidence_binding(answer, "C06", self.INVENTORY)[0], "unattributed")

    def test_clean_sentence_survives_a_back_reference_and_a_shared_summary_is_not_borrowed(self):
        back_reference = "C01 is supported and must be fixed now; HEAD introduced it. C04 is the same defect as C01."
        self.assertEqual(grade._evidence_binding(back_reference, "C01", self.INVENTORY),
                         ("judged", "C01 is supported and must be fixed now; HEAD introduced it."))
        self.assertEqual(grade._evidence_binding(back_reference, "C04", self.INVENTORY)[0], "unattributed")
        summary = "C01 is supported. C02 is refuted. Overall, the defect must be fixed now."
        self.assertEqual(grade._evidence_binding(summary, "C02", self.INVENTORY), ("judged", "C02 is refuted."))
        self.assertEqual(grade._evidence_binding(summary, "C01", self.INVENTORY), ("judged", "C01 is supported."))

    def test_heading_and_label_sections_keep_their_owned_verdict(self):
        per_claim = "## C01\n\nSupported; fix now; HEAD introduced it.\n\n## C06\n\nRefuted; no action."
        cases = (
            (per_claim, "C01", "## C01 Supported; fix now; HEAD introduced it."),
            (per_claim, "C06", "## C06 Refuted; no action."),
            ("### C01\n- Factual: supported\n- Action: fix now\n- Introduced: yes", "C01",
             "### C01 - Factual: supported - Action: fix now - Introduced: yes"),
            ("**C01**:\n- supported\n- fix now", "C01", "**C01**: - supported - fix now"),
            ("## C01, C02 and C04\n\nOne defect: supported, fix now, introduced by HEAD.", "C02",
             "## C01, C02 and C04 One defect: supported, fix now, introduced by HEAD."),
        )
        for answer, claim, span in cases:
            with self.subTest(answer=answer, claim=claim):
                self.assertEqual(grade._evidence_binding(answer, claim, self.INVENTORY), ("judged", span))

    def test_owned_section_stops_at_the_next_heading_and_claim_bearing_block(self):
        answer = "## C01\n\nSupported; fix now.\n\n## Lost-ack claims\n\nAll of these are refuted and need no action.\n- C06\n- C07"
        self.assertEqual(grade._evidence_binding(answer, "C01", self.INVENTORY), ("judged", "## C01 Supported; fix now."))
        self.assertEqual(grade._evidence_binding("**C01**:\n- supported\n- C06 is refuted.", "C01", self.INVENTORY),
                         ("judged", "**C01**: - supported"))

    def test_a_claim_named_only_as_a_bare_label_is_unattributed_not_an_omission(self):
        for answer in ("## C01\n\n## C06\n\nRefuted; no action.", "**C01**:", "- C01\n- C06 is refuted."):
            with self.subTest(answer=answer):
                status, reason = grade._evidence_binding(answer, "C01", self.INVENTORY)
                self.assertEqual(status, "unattributed")
                self.assertIn("label", reason)


class AttributionBindingTests(unittest.IsolatedAsyncioTestCase):
    """Exact claim-id binding through the real admission path; only the judge is faked."""

    def setUp(self):
        self.corpus_path = ROOT / "evals" / "extractor-corpus.json"
        self.records = {record["id"]: record for record in json.loads(self.corpus_path.read_text())["records"]}

    @staticmethod
    async def _grade(answer, items, judge):
        bundles = {("local", 1): {"keys": {"family": {"schema": 2, "items": items}}, "public_identity": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases, source = root / "cases.json", root / "captured.json"
            cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "scale": 1, "key": "family"}]}))
            source.write_text(json.dumps({"schema": 2, "runs": [{"case": "outcome", "rep": 1, "answer": answer}]}))
            with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", side_effect=judge), \
                    mock.patch.object(grade, "_print_table"):
                result = await grade.grade(source, cases, ledger_path=root / "ledger.json")
        return result["runs"][0]

    async def test_retained_misattributed_repairs_are_never_judged_or_credited(self):
        judged = []

        async def judge(states, questions, timeout, concurrency):
            judged.extend(states)
            # Erroneous extractor: fix_now for the retained failures if it were ever shown them.
            labels = {sid: dict(PRIOR_FAILURE) if sid in RETAINED_FAILURES else dict(self.records[sid]["gold"])
                      for sid in states}
            return labels, {}, _batch_record(len(states))

        with mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)):
            report = await grade.validate_extractor_host(self.corpus_path)
        self.assertFalse(set(judged) & set(RETAINED_FAILURES))
        self.assertEqual(report["false_repairs"], [])
        self.assertEqual(report["high_impact_false_repairs"], [])
        statuses = {row["id"]: row["status"] for row in report["attribution"]}
        for rid in RETAINED_FAILURES:
            self.assertEqual(statuses[rid], "absent", rid)
        self.assertEqual(statuses["r05"], "judged")  # explicit leading group naming the target stays admissible
        self.assertEqual(report["unresolved_records"], [])
        self.assertEqual(report["facets"]["action"]["accuracy"], 1.0)

    async def test_absent_claim_is_omitted_without_a_judge_call_and_earns_no_repair_credit(self):
        judged = []

        async def judge(states, questions, timeout, concurrency):
            judged.extend(state["id"] for state in states.values())
            return {sid: dict(PRIOR_FAILURE) for sid in states}, {}, _batch_record(len(states))

        row = await self._grade("C01 is supported and must be fixed now; HEAD introduced it.",
                                [_finding("C01", "D-A"), _finding("C02", "D-B")], judge)
        self.assertEqual(judged, ["C01"])
        self.assertEqual(row["attribution"]["C02"]["status"], "absent")
        self.assertEqual(row["verdicts"]["C02"], OMITTED)
        self.assertEqual(row["repair"]["recommended_defects"], ["D-A"])
        self.assertEqual(row["repair"]["unrecommended_defects"], ["D-B"])
        self.assertEqual((row["repair"]["tp"], row["repair"]["fp"], row["repair"]["fn"]), (1, 0, 1))

    async def test_target_named_only_in_an_unrelated_sentence_cannot_borrow_another_claims_fix(self):
        seen = {}

        async def judge(states, questions, timeout, concurrency):
            seen.update({state["id"]: state["answer"] for state in states.values()})
            # Erroneous extractor: fix_now whenever the text it is shown contains the C01 repair sentence.
            labels = {sid: dict(PRIOR_FAILURE) if "fixed now" in state["answer"] else dict(OMITTED)
                      for sid, state in states.items()}
            return labels, {}, _batch_record(len(states))

        row = await self._grade("C01 is supported and must be fixed now; HEAD introduced it. "
                                "C02 is a separate topic and needs no decision.",
                                [_finding("C01", "D-A"), _finding("C02", "D-B")], judge)
        self.assertEqual(seen["C02"], "C02 is a separate topic and needs no decision.")
        self.assertEqual(row["verdicts"]["C02"]["action"], "omitted")
        self.assertEqual(row["repair"]["recommended_defects"], ["D-A"])
        self.assertEqual(row["repair"]["tp"], 1)
        self.assertEqual(row["repair"]["fp"], 0)

    async def test_sentence_sharing_two_claims_is_unattributed_with_a_reason_not_affirmative_action(self):
        judged = []

        async def judge(states, questions, timeout, concurrency):
            judged.extend(states)
            return {sid: dict(PRIOR_FAILURE) for sid in states}, {}, _batch_record(len(states))

        row = await self._grade("C01 is supported and must be fixed now; C02 is refuted.",
                                [_finding("C01", "D-A"), _finding("C02", None, factual="refuted",
                                                                  actions=("no_action",), introduced="no")],
                                judge)
        self.assertEqual(judged, [])
        self.assertEqual(row["attribution"]["C01"]["status"], "unattributed")
        self.assertEqual(row["errors"], {})  # deterministic attribution is not a judge failure
        self.assertEqual(row["judge_errors"], 0)
        self.assertTrue(row["attribution"]["C01"]["reason"].startswith("sentence naming"))
        self.assertEqual(row["attribution"]["C02"]["status"], "unattributed")
        self.assertEqual(row["verdicts"]["C01"], {})
        self.assertEqual((row["repair"]["tp"], row["repair"]["fn"]), (0, 1))

    async def test_explicit_leading_group_naming_the_target_is_judged_and_credited(self):
        answer = "C01, C02 and C04 are one defect. All three are supported, must be fixed now, and HEAD introduced them."
        seen = {}

        async def judge(states, questions, timeout, concurrency):
            seen.update({state["id"]: state["answer"] for state in states.values()})
            return {sid: dict(PRIOR_FAILURE) for sid in states}, {}, _batch_record(len(states))

        row = await self._grade(answer, [_finding("C01", "D-A"), _finding("C02", "D-A"), _finding("C04", "D-A")], judge)
        self.assertEqual(sorted(seen), ["C01", "C02", "C04"])
        self.assertEqual(seen["C02"], answer)
        self.assertEqual(row["attribution"]["C02"]["status"], "judged")
        self.assertEqual(row["errors"], {})
        self.assertEqual(row["repair"]["recommended_defects"], ["D-A"])
        self.assertEqual((row["repair"]["tp"], row["repair"]["fp"]), (1, 0))

    async def test_valid_exact_attribution_with_two_claims_in_separate_sentences_is_unchanged(self):
        answer = "C01 is supported and must be fixed now; HEAD introduced it. C02 is refuted and needs no action."

        async def judge(states, questions, timeout, concurrency):
            labels = {sid: dict(PRIOR_FAILURE) if state["id"] == "C01"
                      else {"factual": "refuted", "action": "no_action", "introduced": "no"}
                      for sid, state in states.items()}
            return labels, {}, _batch_record(len(states))

        row = await self._grade(answer, [_finding("C01", "D-A"),
                                         _finding("C02", None, factual="refuted", actions=("no_action",), introduced="no")],
                                judge)
        self.assertEqual(row["errors"], {})
        self.assertEqual(row["verdicts"]["C02"], {"factual": "refuted", "action": "no_action", "introduced": "no"})
        self.assertEqual((row["repair"]["tp"], row["repair"]["fp"], row["repair"]["fn"]), (1, 0, 0))


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads((ROOT / "evals" / "extractor-corpus.json").read_text())

    def test_a_claim_absent_from_its_item_is_rejected(self):
        bad = json.loads(json.dumps(self.corpus))
        bad["records"][0]["claim"] = "C99"
        with self.assertRaisesRegex(ValueError, "claim"):
            grade._validate_corpus(bad)


class CalibrationTests(unittest.TestCase):
    def _records(self):
        return [
            {"id": "a", "stratum": "s1", "source_truth": "no_action", "initial": "fix_now", "final": "no_action",
             "reference": "fix_now", "overhead_usd": 0.1, "manual_resolution": False},
            {"id": "b", "stratum": "s2", "source_truth": "fix_now", "initial": "no_action", "final": "no_action",
             "reference": "no_action", "overhead_usd": 0.1, "manual_resolution": True},
            {"id": "c", "stratum": "s1", "source_truth": "defer", "initial": "defer", "final": "defer",
             "reference": "fix_now", "overhead_usd": 0.1, "manual_resolution": False},
        ]

    def test_source_truth_effect_is_reported_apart_from_reference_agreement(self):
        report = grade.calibration_effect(self._records())
        effect = report["effect_on_source_truth"]
        self.assertEqual(effect["unsupported_immediate_repairs"], {"before": 1, "after": 0, "delta": -1})
        self.assertEqual(effect["missed_defects"], {"before": 1, "after": 1, "delta": 0})
        self.assertEqual(effect["changed_decisions"], 1)
        self.assertEqual(effect["manual_resolutions"], 1)
        agreement = report["agreement_with_reference"]
        self.assertEqual((agreement["before"], agreement["after"], agreement["referenced"]), (2, 1, 3))
        self.assertTrue(agreement["agreement_not_correctness"])
        self.assertLess(agreement["after"], agreement["before"])  # agreement fell while truth improved
        self.assertEqual(report["overhead_usd"], {"total": 0.3, "records_with_unknown_overhead": 0})
        self.assertEqual(report["sample"]["strata"]["s1"]["records"], 2)

    def test_records_without_source_truth_are_unscored_but_keep_unknown_overhead(self):
        records = self._records()[:1] + [{"id": "z", "stratum": "s1", "source_truth": None, "initial": "fix_now",
                                          "final": "fix_now", "reference": None, "overhead_usd": None,
                                          "manual_resolution": False}]
        report = grade.calibration_effect(records)
        self.assertEqual(report["sample"]["scored"], 1)
        self.assertEqual(report["sample"]["unscored"], 1)
        self.assertIsNone(report["overhead_usd"]["total"])
        self.assertEqual(report["overhead_usd"]["records_with_unknown_overhead"], 1)

    def test_invalid_decisions_are_rejected(self):
        records = self._records()
        records[0]["final"] = "fix-now"
        with self.assertRaisesRegex(ValueError, "final"):
            grade.calibration_effect(records)


class OffInventoryTests(unittest.TestCase):
    def test_claims_outside_the_inventory_are_unknown_without_an_audit(self):
        boundary = grade.off_inventory_boundary(None, "any-digest")
        self.assertEqual(boundary["status"], "unknown")
        self.assertFalse(boundary["audited"])
        self.assertIsNone(boundary["false_claims"])

    def test_audited_answers_count_verdicts_and_unaudited_answers_stay_unknown(self):
        audit = grade._load_off_inventory(json.dumps({
            "schema": 1, "provenance": SYNTHETIC,
            "answers": {"sha": {"claims": [
                {"text": "t1", "verdict": "false"}, {"text": "t2", "verdict": "true"}, {"text": "t3", "verdict": "unknown"}]}},
        }).encode())
        audited = grade.off_inventory_boundary(audit, "sha")
        self.assertEqual((audited["status"], audited["claims"], audited["false_claims"], audited["unknown_claims"]),
                         ("audited", 3, 1, 1))
        self.assertEqual(grade.off_inventory_boundary(audit, "other")["status"], "unaudited")

    def test_invalid_verdicts_are_rejected(self):
        bad = json.dumps({"schema": 1, "provenance": SYNTHETIC,
                          "answers": {"sha": {"claims": [{"text": "t", "verdict": "maybe"}]}}}).encode()
        with self.assertRaisesRegex(ValueError, "invalid claim"):
            grade._load_off_inventory(bad)


class ControlSummaryTests(unittest.TestCase):
    def test_unscored_controls_stay_in_the_denominator_of_control_accuracy(self):
        rows = [{"case": "count", "final_attempt": True, "control_correct": value} for value in (True, None, False)]
        summary = grade.summarize_controls(rows)
        self.assertEqual(summary["overall"], {"n": 3, "scored": 2, "correct": 1, "control_accuracy": 0.333})
        self.assertEqual(summary["final_attempt"]["n"], 3)
        self.assertEqual(summary["per_case"]["count"]["correct"], 1)


class ControlRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_cases_that_declare_a_control_emit_control_rows(self):
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [item]}}, "public_identity": {},
                                  "private_keys_sha256": "p", "fixture_sha256": "f"}}

        def attempt(attempt_id, text, control=None):
            return {"attempt_id": attempt_id, "execution_status": "complete", "answer_status": "final_answer",
                    "process_status": "clean_exit", "process_problems": [], "control": control,
                    "final_answers": [{"text": text}]}

        runs = [
            {"case": "facets", "rep": 1, "final_attempt_id": "a1", "attempt_records": [attempt("a1", "Bug is real.")]},
            {"case": "summary", "rep": 1, "final_attempt_id": "s1", "attempt_records": [attempt("s1", "Three bullets.")]},
            {"case": "count", "rep": 1, "final_attempt_id": "c1",
             "attempt_records": [attempt("c1", "3", {"kind": "answer_integer", "correct": True,
                                                     "evidence_class": "runner_graded"})]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "captured.json", root / "cases.json"
            results.write_text(json.dumps({"schema": 4, "cost_ledger": {}, "runs": runs}))
            cases.write_text(json.dumps({"cases": [
                {"id": "facets", "dataset": "local", "key": "facets"},
                {"id": "summary", "dataset": "local", "key": None},
                {"id": "count", "dataset": "local", "key": None, "control": {"kind": "answer_integer"}}]}))

            async def judge(states, questions, timeout, concurrency):
                return {sid: dict(GOOD_ANSWERS) for sid in states}, {}, _batch_record(len(states))

            with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)), \
                    mock.patch.object(grade, "_print_table"):
                result = await grade.grade(results, cases, out_path=root / "out.json", ledger_path=root / "ledger.json")
        self.assertEqual([row["case"] for row in result["control_runs"]], ["count"])
        self.assertEqual(result["controls"]["overall"], {"n": 1, "scored": 1, "correct": 1, "control_accuracy": 1.0})
        self.assertEqual(set(result["controls"]["per_case"]), {"count"})


class ImmutableInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_during_grading_does_not_change_the_bytes_graded(self):
        original = "Original answer about the bug."
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [item]}}, "public_identity": {},
                                  "private_keys_sha256": "p", "fixture_sha256": "f"}}
        sent = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "captured.json", root / "cases.json"
            ledger, out = root / "grading.ledger.json", root / "captured.graded.json"
            results.write_text(json.dumps({"schema": 4, "cost_ledger": {}, "runs": [{
                "case": "facets", "rep": 1, "final_attempt_id": "a1",
                "attempt_records": [{"attempt_id": "a1", "execution_status": "complete", "answer_status": "final_answer",
                                     "process_status": "clean_exit", "process_problems": [], "control": None,
                                     "final_answers": [{"text": original}]}]}]}))
            cases.write_text(json.dumps({"cases": [{"id": "facets", "dataset": "local", "key": "facets"}]}))

            async def judge(states, questions, timeout, concurrency):
                sent.update({sid: state["answer"] for sid, state in states.items()})
                results.write_text("MUTATED after the snapshot was taken")  # intervening edit
                return ({sid: dict(GOOD_ANSWERS) for sid in states}, {},
                        {"id": "b", "kind": "judgment_batch", "items": len(states), "model": "m",
                         "cost_usd": None, "cost_basis": "batch_status_estimate", "billing_complete": False,
                         "cleanup_errors": [], "cache": {}})

            with mock.patch.object(grade, "_fixture_keys", return_value=bundles), \
                    mock.patch.object(grade, "_judge", mock.AsyncMock(side_effect=judge)), \
                    mock.patch.object(grade, "_print_table"):
                result = await grade.grade(results, cases, out_path=out, ledger_path=ledger)
            self.assertEqual(list(sent.values()), [original])
            self.assertEqual(result["runs"][0]["answer_sha256"], grade._digest(original))
            self.assertEqual(json.loads(ledger.read_text())["inputs_changed_during_grading"], [str(results)])


class LaunchEvidenceTests(unittest.TestCase):
    def test_verify_cells_requires_load_before_an_error_free_grading_call(self):
        load, call = "%load x", "await grade('r', 'c', 'o')"
        in_order_ok = [{"code": load, "is_error": False}, {"code": call, "is_error": False}]
        self.assertTrue(launch.verify_cells(in_order_ok, load, call)["grade_ok"])
        grade_first = [{"code": call, "is_error": False}, {"code": load, "is_error": False}]
        self.assertFalse(launch.verify_cells(grade_first, load, call)["grade_seen"])
        errored = [{"code": load, "is_error": False}, {"code": call, "is_error": True}]
        result = launch.verify_cells(errored, load, call)
        self.assertTrue(result["grade_seen"])
        self.assertFalse(result["grade_ok"])

    def test_cache_descriptor_never_names_the_private_clone_path(self):
        plan = {"status": "fresh_empty_namespace", "isolation": "independent_per_attempt", "path": Path("/tmp/x.sqlite"),
                "initial_sha256": "e", "initial_bytes": 0}
        descriptor = launch.cache_descriptor(plan, "cold")
        self.assertNotIn("path", descriptor)
        self.assertEqual(descriptor["env"], "OMP_JUDGMENT_CACHE_DB")
        self.assertFalse(descriptor["host_attested"])

    def test_warm_clones_start_from_the_frozen_backup_including_wal_and_stay_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            seed = workdir / "seed.sqlite"
            writer = sqlite3.connect(seed)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE t (v TEXT)")
            writer.execute("INSERT INTO t VALUES ('committed-in-wal')")
            writer.commit()
            try:
                frozen = run.freeze_cache_seed(str(seed), workdir / "work")
                first = run.cache_plan("warm", frozen, workdir / "work", "grading", 1, 1)
                second = run.cache_plan("warm", frozen, workdir / "work", "grading", 1, 1)
                with sqlite3.connect(first["path"]) as clone:
                    clone.execute("INSERT INTO t VALUES ('attempt-only')")
                with sqlite3.connect(second["path"]) as clone:
                    rows = [row[0] for row in clone.execute("SELECT v FROM t ORDER BY v")]
                self.assertEqual(rows, ["committed-in-wal"])
                self.assertEqual(first["initial_sha256"], second["initial_sha256"])
                self.assertNotEqual(first["path"], second["path"])
            finally:
                writer.close()
            with sqlite3.connect(seed) as reread:
                self.assertEqual([row[0] for row in reread.execute("SELECT v FROM t")], ["committed-in-wal"])

    def test_cold_plans_are_distinct_empty_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run.cache_plan("cold", None, Path(tmp), "grading", 1, 1)
            second = run.cache_plan("cold", None, Path(tmp), "grading", 1, 1)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(first["path"].stat().st_size, 0)


FAKE_OMP = r"""#!__PYTHON__
import json
import os
import sys

if "--version" in sys.argv:
    print("fake-omp 0.0.0")
    raise SystemExit(0)
prompt = sys.argv[-1]
head, rest = prompt.split("Cell 1 (language py):\n", 1)[1].split("\n\nCell 2 (language py):\n", 1)
cells = [head, rest.rstrip("\n")]
mode = os.environ.get("FAKE_OMP_MODE", "")
if mode == "load_only":
    cells = cells[:1]
if mode == "extra_cell":
    cells.append("1 + 1")
events = [{"type": "agent_start"}]
for index, code in enumerate(cells):
    events.append({"type": "tool_execution_start", "toolName": "eval", "toolCallId": f"c{index}",
                   "args": {"code": code}})
    events.append({"type": "tool_execution_end", "toolCallId": f"c{index}", "isError": False})
if mode != "no_agent_end":
    events.append({"type": "agent_end"})
for event in events:
    print(json.dumps(event), flush=True)
"""


class LaunchFreezeTests(unittest.TestCase):
    def test_freeze_copies_exact_bytes_and_keeps_the_results_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            results, cases = root / "cand.jsonl", root / "cases.json"
            results.write_text('{"schema": 3}\n')
            cases.write_text("{}")
            frozen = launch.freeze_inputs(scratch, results, cases)
            self.assertTrue(frozen["results"]["snapshot"].endswith(".jsonl"))
            self.assertEqual(Path(frozen["grader"]["snapshot"]).read_bytes(), launch.GRADE.read_bytes())
            for name in grade.FIXTURE_SOURCES:
                self.assertEqual(Path(frozen[f"fixture:{name}"]["snapshot"]).read_bytes(),
                                 (ROOT / "evals" / name).read_bytes())
            self.assertEqual(launch.input_drift(frozen), [])
            results.write_text('{"schema": 3, "edited": true}\n')
            self.assertEqual(Path(frozen["results"]["snapshot"]).read_text(), '{"schema": 3}\n')
            self.assertEqual(launch.input_drift(frozen), [str(results)])


class LaunchOutputIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Launch verification over the real grade() output of the frozen snapshot; only the judge is faked."""

    async def _launch_frozen(self, root):
        item = {"id": "bug", "desc": "Synthetic claim bug", "factual": "supported", "introduced": "yes",
                "actions": ["fix_now"], "defect": "D1", "group": "g", "evidence": [], "rationale": ""}
        bundles = {("local", 1): {"keys": {"facets": {"schema": 2, "items": [item]}}, "public_identity": {},
                                  "private_keys_sha256": "p", "fixture_sha256": "f"}}
        results, cases = root / "captured.json", root / "cases.json"
        results.write_text(json.dumps({"schema": 4, "cost_ledger": {}, "runs": [{
            "case": "facets", "rep": 1, "final_attempt_id": "a1",
            "attempt_records": [{"attempt_id": "a1", "execution_status": "complete",
                                 "answer_status": "final_answer", "process_status": "clean_exit",
                                 "process_problems": [], "control": None,
                                 "final_answers": [{"text": "Bug is real."}]}]}]}))
        cases.write_text(json.dumps({"cases": [{"id": "facets", "dataset": "local", "key": "facets"}]}))
        scratch = root / "scratch"
        scratch.mkdir()
        frozen = launch.freeze_inputs(scratch, results, cases)
        plan = run.cache_plan("cold", None, scratch, launch.CELL_NAMESPACE, 1, 1)
        descriptor = launch.cache_descriptor(plan, "cold")
        spec = importlib.util.spec_from_file_location("jevify_frozen_grade", frozen["grader"]["snapshot"])
        frozen_grade = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(frozen_grade)

        async def judge(states, questions, timeout, concurrency):
            return {sid: dict(GOOD_ANSWERS) for sid in states}, {}, _batch_record(len(states))

        out = root / "facets.graded.json"
        with mock.patch.object(frozen_grade, "_fixture_keys", return_value=bundles), \
                mock.patch.object(frozen_grade, "_judge", mock.AsyncMock(side_effect=judge)), \
                mock.patch.object(frozen_grade, "_print_table"):
            await frozen_grade.grade(frozen["results"]["snapshot"], frozen["cases"]["snapshot"],
                                     out_path=out, launch=descriptor)
        return frozen, descriptor, out

    @staticmethod
    def _edit(path, change):
        document = json.loads(path.read_text())
        change(document)
        path.write_text(json.dumps(document))

    async def test_real_grade_output_verifies_against_the_frozen_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            self.assertEqual(json.loads(out.read_text())["input_sha256"], frozen["results"]["sha256"])
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertEqual(outcome["problems"], [])
            self.assertTrue(outcome["ok"])

    async def test_a_different_launch_descriptor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            outcome = launch.verify_outputs(out, frozen, {**descriptor, "requested": "warm"})
            self.assertIn("ledger does not record this launch descriptor", outcome["problems"])

    async def test_a_mapping_where_the_single_result_digest_belongs_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            digest, snapshot = frozen["results"]["sha256"], frozen["results"]["snapshot"]
            self._edit(out, lambda doc: doc.update(input_sha256={snapshot: digest}))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("graded output is not bound to the frozen results bytes", outcome["problems"])

    async def test_a_forged_grader_identity_in_the_output_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            self._edit(out, lambda doc: doc["identities"].update(source_sha256="0" * 64))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("graded output was not produced by the frozen grader bytes", outcome["problems"])
            self.assertIn("ledger identities differ from the graded output", outcome["problems"])

    async def test_the_ledger_must_bind_the_frozen_results_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            ledger = Path(json.loads(out.read_text())["grading"]["ledger_path"])
            snapshot = frozen["results"]["snapshot"]
            self._edit(ledger, lambda doc: doc.update(input_sha256={snapshot: "0" * 64}))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("graded output is not bound to the frozen results bytes", outcome["problems"])

    async def test_inputs_changed_during_grading_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            frozen, descriptor, out = await self._launch_frozen(Path(tmp))
            ledger = Path(json.loads(out.read_text())["grading"]["ledger_path"])
            snapshot = frozen["results"]["snapshot"]
            self._edit(ledger, lambda doc: doc.update(inputs_changed_during_grading=[snapshot]))
            outcome = launch.verify_outputs(out, frozen, descriptor)
            self.assertIn("grading inputs changed during grading", outcome["problems"])


class SessionLimitTests(unittest.TestCase):
    def test_default_session_limit_outlives_the_grading_deadline(self):
        self.assertEqual(launch.session_max_time(1800), 1800 + launch.SESSION_MARGIN_SECONDS)
        self.assertGreater(launch.session_max_time(1800), 1800)
        self.assertEqual(launch.session_max_time(1800, 3600), 3600)

    def test_explicit_limit_at_or_below_the_deadline_is_rejected(self):
        for limit in (900, 1800):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "must exceed"):
                launch.session_max_time(1800, limit)

    def test_launch_rejects_the_limit_before_creating_any_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "cand.json", root / "cases.json"
            results.write_text('{"schema": 3}')
            cases.write_text("{}")
            with self.assertRaisesRegex(ValueError, "must exceed"):
                launch.launch(results=results, cases=cases, out=root / "graded.json", omp="/nonexistent/omp",
                              workdir=root / "work", timeout=60, max_time=60)
            self.assertFalse((root / "work").exists())
            self.assertFalse((root / "graded.json.launch.json").exists())


class LaunchBehaviorTests(unittest.TestCase):
    def _run(self, root, mode):
        root.mkdir(parents=True, exist_ok=True)
        results, cases = root / "cand.json", root / "cases.json"
        results.write_text('{"schema": 3}')
        cases.write_text("{}")
        fake = root / "fake-omp"
        fake.write_text(FAKE_OMP.replace("__PYTHON__", sys.executable))
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"FAKE_OMP_MODE": mode}):
            return launch.launch(results=results, cases=cases, out=root / "graded.json", omp=str(fake),
                                 workdir=root / "work")

    def test_exact_cells_without_graded_output_are_not_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self._run(Path(tmp), "")
            self.assertEqual(record["status"], "not_graded")
            self.assertTrue(record["cells"]["sequence_exact"])
            self.assertTrue(record["cells"]["grade_ok"])
            self.assertTrue(record["session"]["complete"])
            self.assertEqual(record["problems"], ["graded output was not written"])
            self.assertEqual(record["session"]["max_time"], 1800 + launch.SESSION_MARGIN_SECONDS)
            self.assertFalse((Path(tmp) / "graded.json").exists())

    def test_missing_or_extra_eval_cells_are_problems(self):
        for mode in ("load_only", "extra_cell"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                record = self._run(Path(tmp), mode)
                self.assertEqual(record["status"], "not_graded")
                self.assertFalse(record["cells"]["sequence_exact"])
                self.assertTrue(any("not exactly the two approved cells" in p for p in record["problems"]))

    def test_a_session_without_agent_end_is_not_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self._run(Path(tmp), "no_agent_end")
            self.assertEqual(record["status"], "not_graded")
            self.assertFalse(record["session"]["complete"])
            self.assertIn("session did not reach agent_end", record["problems"])

    def test_scratch_is_removed_after_a_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root, "")
            self.assertEqual(list((root / "work").iterdir()), [])

    def test_scratch_is_removed_when_the_launch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results, cases = root / "cand.json", root / "cases.json"
            results.write_text('{"schema": 3}')
            cases.write_text("{}")
            work = root / "work"
            work.mkdir()
            with mock.patch.object(launch.run, "source_identity", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    launch.launch(results=results, cases=cases, out=root / "graded.json",
                                  omp="/nonexistent/omp", workdir=work)
            self.assertEqual(list(work.iterdir()), [])
            self.assertFalse((root / "graded.json.launch.json").exists())


class PairedComparisonTests(unittest.TestCase):
    def _row(self, case, action_accuracy, unsupported=(), *, key_sha=None):
        return {"case": case, "final_attempt": True, "key_schema": 2, "factual_accuracy": 1.0,
                "action_accuracy": action_accuracy, "introduced_accuracy": 1.0, "factual_coverage": 1.0,
                "action_coverage": 1.0, "introduced_coverage": 1.0, "recall": 1.0, "precision": 1.0,
                "repair": {"unsupported_fix_now_claims": list(unsupported)},
                "key_identity": {"key_schema": 2, "key": case, "key_sha256": key_sha or f"sha-{case}",
                                 "public_identity": {}},
                "key_binding": {"status": "matched", "captured": {}}}

    def _graded(self, rows, *, model="m", skill=None, identities=None, predeclared=None, recorded=True):
        result = {"results": "x", "input_sha256": "h", "identities": _grader_ids() if identities is None else identities,
                  "runs": rows, "control_runs": [],
                  "run_manifest": None if predeclared is None else {"predeclared_thresholds": predeclared}}
        if recorded:
            contract = {"controlled": {"model_requested": model}, "treatment": {"skill": skill, "strategy": "auto"}}
            result.update({"comparison_contract": contract, "comparison_contract_sha256": run.digest_json(contract)})
        return result

    def test_case_means_average_repetitions_before_the_paired_difference(self):
        base = self._graded([self._row("c1", 0.4), self._row("c1", 0.6), self._row("c2", 0.5), self._row("c3", 0.5)])
        candidate = self._graded([self._row("c1", 0.6), self._row("c1", 0.8), self._row("c2", 0.6), self._row("c3", 0.8)])
        comparison = grade.compare_graded(base, candidate)
        action = comparison["comparisons"]["action_accuracy"]
        self.assertAlmostEqual(action["mean_delta"], 0.2, places=3)
        self.assertEqual(action["n"], 3)
        self.assertTrue(comparison["contract_check"]["compatible"])

    def test_different_controlled_model_is_not_comparable_and_is_post_hoc(self):
        base = self._graded([self._row("c1", 0.5)], model="m1")
        candidate = self._graded([self._row("c1", 0.7)], model="m2")
        comparison = grade.compare_graded(base, candidate)
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertIn("model_requested", comparison["contract_check"]["controlled_differences"])
        self.assertTrue(comparison["post_hoc"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_one_treatment_difference_with_identical_instrument_is_comparable(self):
        base = self._graded([self._row("c1", 0.5), self._row("c2", 0.5)])
        candidate = self._graded([self._row("c1", 0.7), self._row("c2", 0.6)], skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertEqual(comparison["contract_check"]["treatment_differences"], ["skill"])
        self.assertTrue(comparison["contract_check"]["one_variable"])
        self.assertTrue(comparison["contract_check"]["compatible"])
        # No predeclared thresholds: the rule withholds, but it is not blocked as not_comparable.
        self.assertEqual(comparison["recommendation"]["verdict"], "withheld")

    def test_one_treatment_difference_with_predeclared_rule_can_support_the_candidate(self):
        thresholds = {"schema": 1, "confidence": 0.95, "min_cases": 2,
                      "metrics": {"correctness": "action_accuracy", "coverage": "action_coverage",
                                  "unsafe": "unsupported_fix_now_claims"},
                      "rules": {"correctness_min_delta": 0.1, "coverage_max_regression": 0.05,
                                "unsafe_max_increase": 0}}
        predeclared = {"sha256": run.digest_json(thresholds), "thresholds": thresholds, "path": "t.json"}
        base = self._graded([self._row("c1", 0.4), self._row("c2", 0.5), self._row("c3", 0.5)], predeclared=predeclared)
        candidate = self._graded([self._row("c1", 0.7), self._row("c2", 0.8), self._row("c3", 0.8)], skill="abc",
                                 predeclared=predeclared)
        comparison = grade.compare_graded(base, candidate)
        self.assertTrue(comparison["predeclared_thresholds_matched"])
        self.assertEqual(comparison["recommendation"]["verdict"], "supports_candidate", comparison["recommendation"])

    def test_unrecorded_contract_returns_a_not_comparable_verdict_instead_of_raising(self):
        base = self._graded([self._row("c1", 0.5)], recorded=False)
        candidate = self._graded([self._row("c1", 0.7)], skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertFalse(comparison["contract_check"]["one_variable"])
        self.assertIn("comparison_contract", comparison["contract_check"]["unrecorded"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_differing_grader_identity_is_a_controlled_difference(self):
        base = self._graded([self._row("c1", 0.5)])
        changed = {**_grader_ids(), "extraction_rubric_sha256": {"1": "r1", "2": "other"}}
        candidate = self._graded([self._row("c1", 0.9)], identities=changed, skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertIn("grader:extraction_rubric_sha256", comparison["contract_check"]["controlled_differences"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_missing_grader_identity_is_unrecorded_and_blocks_a_claim(self):
        base = self._graded([self._row("c1", 0.5)])
        without = {k: v for k, v in _grader_ids().items() if k != "source_sha256"}
        candidate = self._graded([self._row("c1", 0.9)], identities=without, skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertIn("grader:source_sha256", comparison["contract_check"]["unrecorded"])
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")

    def test_differing_key_identity_is_a_controlled_difference(self):
        base = self._graded([self._row("c1", 0.5)])
        candidate = self._graded([self._row("c1", 0.9, key_sha="other-key")], skill="abc")
        comparison = grade.compare_graded(base, candidate)
        self.assertIn("key:c1", comparison["contract_check"]["controlled_differences"])
        self.assertFalse(comparison["contract_check"]["compatible"])
        self.assertEqual(comparison["recommendation"]["verdict"], "not_comparable")


if __name__ == "__main__":
    unittest.main()
