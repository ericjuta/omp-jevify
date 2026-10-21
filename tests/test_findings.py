"""Structured findings (evals/findings.py): exact ids, source revisions and bounded exact quotations.

Validation is deterministic and makes no model call. A passing check proves well-formed, exact, unambiguous
attribution; it does not prove that a correctly named passage supports its labels. Those tests are therefore
about structure and binding only.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"jevify_findings_{name}", ROOT / "evals" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


findings = _load("findings")
grade = _load("grade")
fixture = _load("fixture")
refund_fixture = _load("refund_retry_fixture")

HEAD, BASE = "1" * 40, "2" * 40
INVENTORY = frozenset({"C01", "C02", "C03", "C04"})
HEAD_SOURCE = ("def retry(refund):\n    key = refund.key\n    if refund.persisted:\n"
               "        return create(refund, key + ':retry')\n    return settled(refund)\n")
BASE_SOURCE = "def retry(refund):\n    return settled(refund)\n"


def _tree(text):
    return {"refunds/worker.py": {"sha256": "", "text": text}}


SNAPSHOT = {"schema": "jevify-source/1", "head": HEAD, "base": BASE,
            "trees": {HEAD: _tree(HEAD_SOURCE), BASE: _tree(BASE_SOURCE)}}


def _cite(line, end=None, *, revision=HEAD):
    source = HEAD_SOURCE if revision == HEAD else BASE_SOURCE
    end = line if end is None else end
    return {"revision": revision, "file": "refunds/worker.py", "start": line, "end": end,
            "quote": "\n".join(source.splitlines()[line - 1:end])}


def _finding(ids, *, line=3, factual="supported", action="fix_now", introduced="yes", evidence=None):
    return {"claim_ids": list(ids), "factual": factual, "action": action, "introduced": introduced,
            "evidence": [_cite(line)] if evidence is None else evidence}


def _document(*items, revision=HEAD):
    return json.dumps({"schema": findings.SCHEMA, "source_revision": revision, "findings": list(items)})


class ValidationTests(unittest.TestCase):
    def _validate(self, answer):
        return findings.validate_findings(answer, SNAPSHOT, INVENTORY)

    @staticmethod
    def _statuses(record):
        return {cid: claim["status"] for cid, claim in record["claims"].items()}

    def test_former_prose_misattributions_are_rejected_as_a_whole_and_stay_unresolved(self):
        prose = (
            "C01 is supported and must be fixed now; HEAD introduced it.",
            "C99 is a fix-now defect and HEAD introduced it.",
            "C1 is supported and fix_now; HEAD introduced it.",
            "C01, C02 and C04 are one defect. All three are supported, must be fixed now.",
        )
        for answer in prose:
            with self.subTest(answer=answer):
                record = self._validate(answer)
                self.assertEqual(record["status"], "rejected")
                self.assertEqual(set(self._statuses(record).values()), {"unresolved"})

    def test_exact_claim_is_asserted_with_its_labels_and_others_are_omitted(self):
        record = self._validate(_document(_finding(["C01"])))
        self.assertEqual(record["status"], "accepted")
        self.assertEqual(record["claims"]["C01"], {"status": "asserted", "finding": 0, "factual": "supported",
                                                   "action": "fix_now", "introduced": "yes", "reason": None})
        self.assertEqual(self._statuses(record)["C02"], "omitted")
        self.assertEqual(record["unknown_ids"], [])

    def test_explicit_group_credits_each_listed_id_with_the_same_labels(self):
        record = self._validate(_document(_finding(["C01", "C02", "C04"], action="defer", introduced="no")))
        for cid in ("C01", "C02", "C04"):
            with self.subTest(cid=cid):
                claim = record["claims"][cid]
                self.assertEqual((claim["status"], claim["action"], claim["introduced"]), ("asserted", "defer", "no"))
        self.assertEqual(self._statuses(record)["C03"], "omitted")

    def test_json_formatting_cannot_change_attribution(self):
        body = _document(_finding(["C01"]), _finding(["C02"], line=5, action="no_action", introduced="no"))
        variants = (body, f"```json\n{body}\n```", f"```\n{body}\n```", f"  \n{body}\n\n",
                    json.dumps(json.loads(body), indent=2))
        records = [self._validate(variant) for variant in variants]
        for variant, record in zip(variants, records):
            with self.subTest(variant=variant[:12]):
                self.assertEqual(record["status"], "accepted")
                self.assertEqual(record["claims"], records[0]["claims"])

    def test_prose_around_the_object_or_a_second_object_is_not_one_document(self):
        body = _document(_finding(["C01"]))
        for answer in (f"Here it is:\n{body}", f"{body}\n{body}", f"```json\n{body}\n```\nThanks."):
            with self.subTest(answer=answer[:20]):
                self.assertEqual(self._validate(answer)["status"], "rejected")

    def test_document_level_rejections_leave_every_inventory_claim_unresolved(self):
        good = json.loads(_document(_finding(["C01"])))
        cases = {
            "not json": "C01 is supported.",
            "trailing prose": _document(_finding(["C01"])) + "\nDone.",
            "duplicate key": '{"schema": "jevify-findings/1", "schema": "jevify-findings/1", '
                             f'"source_revision": "{HEAD}", "findings": []}}',
            "non-finite number": '{"schema": "jevify-findings/1", "source_revision": NaN, "findings": []}',
            "wrong schema": json.dumps({**good, "schema": "jevify-findings/0"}),
            "stale source (BASE tree)": json.dumps({**good, "source_revision": BASE}),
            "unknown top-level key": json.dumps({**good, "note": "x"}),
            "findings not a list": json.dumps({**good, "findings": {}}),
        }
        for label, answer in cases.items():
            with self.subTest(case=label):
                record = self._validate(answer)
                self.assertEqual(record["status"], "rejected")
                self.assertEqual(set(self._statuses(record).values()), {"unresolved"})

    def test_finding_level_rejections_leave_their_named_claims_unresolved(self):
        base_citation = _cite(3)
        cases = {
            "wrong revision": _finding(["C01"], evidence=[{**base_citation, "revision": "3" * 40}]),
            "unknown id": _finding(["C99"]),
            "unknown id beside a known id": _finding(["C01", "C99"]),
            "repeated id": _finding(["C01", "C01"]),
            "bad factual label": _finding(["C01"], factual="maybe"),
            "bad action label": _finding(["C01"], action="ship_it"),
            "extra finding key": {**_finding(["C01"]), "reason": "x"},
            "no evidence": _finding(["C01"], evidence=[]),
            "extra evidence key": _finding(["C01"], evidence=[{**base_citation, "note": "x"}]),
            "missing file": _finding(["C01"], evidence=[{**base_citation, "file": "refunds/missing.py"}]),
            "span past end of file": _finding(["C01"], evidence=[{**base_citation, "start": 3, "end": 9}]),
            "start below one": _finding(["C01"], evidence=[{**base_citation, "start": 0, "end": 1}]),
            "start after end": _finding(["C01"], evidence=[{**base_citation, "start": 4, "end": 3}]),
            "boolean line number": _finding(["C01"], evidence=[{**base_citation, "start": True, "end": True}]),
            "quote is not exact text": _finding(["C01"], evidence=[{**base_citation, "quote": "if refund.persisted: return"}]),
            "quote lies outside the cited lines": _finding(
                ["C01"], evidence=[{**base_citation, "quote": "return settled(refund)"}]),
            "shortened quote from a multi-line span": _finding(
                ["C01"], evidence=[{"revision": HEAD, "file": "refunds/worker.py", "start": 3, "end": 4,
                                    "quote": "    if refund.persisted:"}]),
            "subsequence of the cited lines": _finding(
                ["C01"], evidence=[{"revision": HEAD, "file": "refunds/worker.py", "start": 3, "end": 4,
                                    "quote": "        return create(refund, key + ':retry')"}]),
            "fragment of a word inside the span": _finding(
                ["C01"], evidence=[{**base_citation, "quote": "persisted"}]),
            "trailing newline added to a span": _finding(
                ["C01"], evidence=[{**base_citation, "quote": "    if refund.persisted:\n"}]),
            "empty quote": _finding(["C01"], evidence=[{**base_citation, "quote": "  "}]),
        }
        for label, item in cases.items():
            with self.subTest(case=label):
                record = self._validate(_document(item))
                self.assertEqual(record["status"], "accepted")
                self.assertEqual(record["findings"][0]["status"], "rejected", record["findings"][0]["reason"])
                if "C99" not in item["claim_ids"]:
                    self.assertEqual(record["claims"]["C01"]["status"], "unresolved")
                self.assertEqual(record["claims"]["C02"]["status"], "omitted")

    def test_full_span_quote_is_the_only_accepted_form(self):
        full = {"revision": HEAD, "file": "refunds/worker.py", "start": 3, "end": 4,
                "quote": "    if refund.persisted:\n        return create(refund, key + ':retry')"}
        record = self._validate(_document(_finding(["C01"], evidence=[full])))
        self.assertEqual(record["claims"]["C01"]["status"], "asserted")

    def test_base_revision_citations_are_accepted_only_for_base_text(self):
        record = self._validate(_document(_finding(["C02"], evidence=[_cite(1, revision=BASE)])))
        self.assertEqual(record["claims"]["C02"]["status"], "asserted")

    def test_unknown_ids_are_reported_and_never_credited(self):
        record = self._validate(_document(_finding(["C99"])))
        self.assertEqual(record["unknown_ids"], ["C99"])
        self.assertEqual(record["findings"][0]["status"], "rejected")
        self.assertEqual(set(self._statuses(record).values()), {"omitted"})

    def test_duplicate_records_for_one_claim_are_unresolved(self):
        record = self._validate(_document(_finding(["C01"]), _finding(["C01"], action="defer")))
        self.assertEqual(record["claims"]["C01"]["status"], "unresolved")
        self.assertIn("duplicate or conflicting", record["claims"]["C01"]["reason"])

    def test_conflicting_group_leaves_the_contested_member_unresolved_and_others_asserted(self):
        record = self._validate(_document(_finding(["C01", "C02"]), _finding(["C02"], action="defer", introduced="no")))
        self.assertEqual(record["claims"]["C01"]["status"], "asserted")
        self.assertEqual(record["claims"]["C02"]["status"], "unresolved")

    def test_group_with_an_off_inventory_member_is_rejected_for_every_listed_id(self):
        record = self._validate(_document(_finding(["C01", "C02", "C99"])))
        self.assertEqual(record["unknown_ids"], ["C99"])
        self.assertEqual((record["claims"]["C01"]["status"], record["claims"]["C02"]["status"]), ("unresolved", "unresolved"))

    def test_rendered_text_comes_only_from_the_validated_record(self):
        record = self._validate(_document(_finding(["C01"]), _finding(["C02"], line=99)))
        rendered = findings.render_findings(record)
        self.assertEqual(rendered.splitlines()[0],
                         "C01: factual=supported action=fix_now introduced=yes (finding 0)")
        self.assertTrue(rendered.splitlines()[1].startswith("C02: unresolved ("))
        self.assertNotIn("refunds/worker.py", rendered)


class GitContractTests(unittest.TestCase):
    def test_billing_repository_has_no_public_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = fixture.build(Path(tmp) / "repo")
            self.assertEqual(findings.output_contract(repo), "")
            self.assertIsNone(findings.contract_digest(findings.output_contract(repo)))

    def test_refund_contract_binds_head_and_base_trees_and_lists_every_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = refund_fixture.build(Path(tmp) / "repo")
            snapshot = findings.source_snapshot(repo)
            contract = findings.contract_from_snapshot(snapshot)
            self.assertIn(snapshot["head"], contract)
            self.assertIn(snapshot["base"], contract)
            self.assertNotEqual(snapshot["head"], snapshot["base"])
            for cid, *_rest in refund_fixture.CLAIMS:
                self.assertIn(cid, contract)
            self.assertEqual(findings.output_contract(repo), contract)

    def test_snapshot_is_content_identity_json_serializable_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = findings.source_snapshot(refund_fixture.build(Path(tmp) / "first"))
            second = findings.source_snapshot(refund_fixture.build(Path(tmp) / "second"))
        self.assertEqual(first["head"], second["head"])
        self.assertEqual(first["base"], second["base"])
        self.assertEqual(json.loads(json.dumps(first)), json.loads(json.dumps(second)))
        with self.assertRaises(TypeError):
            first["head"] = "x"
        with self.assertRaises(TypeError):
            first["trees"][first["head"]]["review/claims.json"] = {}


class SchemaTwoGradingTests(unittest.IsolatedAsyncioTestCase):
    """grade() on structured findings: validation replaces attribution and no model call is made."""

    KEY = {"schema": 2, "items": [
        {"id": "C01", "desc": "Claim C01", "factual": "supported", "actions": ["fix_now"], "introduced": "yes",
         "defect": "D-A", "group": "g", "evidence": [], "rationale": ""},
        {"id": "C02", "desc": "Claim C02", "factual": "supported", "actions": ["no_action"], "introduced": "no",
         "defect": None, "group": "g", "evidence": [], "rationale": ""},
    ]}

    @classmethod
    def _bundles(cls):
        return {("local", 1): {"keys": {"facets": cls.KEY}, "public_identity": {}, "private_keys_sha256": "p",
                               "fixture_sha256": "f", "snapshot": SNAPSHOT, "output_contract_sha256": None}}

    async def _grade(self, answer):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, cases, ledger = root / "captured.json", root / "cases.json", root / "ledger.json"
            cases.write_text(json.dumps({"cases": [{"id": "outcome", "dataset": "local", "scale": 1, "key": "facets"}]}))
            source.write_text(json.dumps({"schema": 4, "runs": [{"case": "outcome", "rep": 1, "final_attempt_id": "a1",
                                                                  "attempt_records": [{"attempt_id": "a1",
                                                                                       "final_answers": [{"text": answer}]}]}]}))
            judge = mock.AsyncMock(side_effect=AssertionError("structured findings must not reach the judge"))
            with mock.patch.object(grade, "_fixture_keys", return_value=self._bundles()), \
                    mock.patch.object(grade, "_judge", judge), mock.patch.object(grade, "_print_table"):
                result = await grade.grade(source, cases, ledger_path=ledger)
            judge.assert_not_awaited()
        return result["runs"][0]

    async def test_asserted_claims_are_scored_and_omitted_claims_earn_nothing(self):
        row = await self._grade(_document(_finding(["C01"])))
        self.assertEqual(row["verdicts"]["C01"], {"factual": "supported", "action": "fix_now", "introduced": "yes"})
        self.assertEqual(row["attribution"]["C01"]["status"], "asserted")
        self.assertEqual(row["verdicts"]["C02"], {"factual": "omitted", "action": "omitted", "introduced": "omitted"})
        for facet in ("factual", "action"):
            self.assertEqual((row["facets"][facet]["planned"], row["facets"][facet]["correct"]), (2, 1), facet)
            self.assertEqual(row["facets"][facet]["omitted"], 1, facet)
        self.assertEqual((row["repair"]["tp"], row["repair"]["fp"], row["repair"]["fn"]), (1, 0, 0))
        self.assertEqual(row["unattributed_items"], 0)
        self.assertEqual(row["judge_errors"], 0)

    async def test_wrong_revision_makes_every_claim_unresolved_and_not_credited(self):
        row = await self._grade(_document(_finding(["C01"]), revision=BASE))
        self.assertEqual({record["status"] for record in row["attribution"].values()}, {"unattributed"})
        self.assertEqual(row["unattributed_items"], 2)
        self.assertEqual((row["repair"]["tp"], row["repair"]["fn"]), (0, 1))

    async def test_prose_answer_is_unresolved_not_a_judge_failure(self):
        row = await self._grade("C01 is supported and must be fixed now.")
        self.assertEqual(row["unattributed_items"], 2)
        self.assertEqual(row["judge_errors"], 0)
        self.assertEqual(row["repair"]["recommended_defects"], [])

    async def test_empty_answer_omits_every_claim(self):
        row = await self._grade("")
        self.assertEqual(row["attribution"], {})
        self.assertEqual(row["unattributed_items"], 0)
        self.assertEqual(row["repair"]["fn"], 1)


if __name__ == "__main__":
    unittest.main()
