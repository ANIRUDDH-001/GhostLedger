"""Output-contract validator (AGENTS.md section 6.3-6.6) and its tests.

contract_errors() runs the structural layer of ghostledger/contract.py over a complete output.csv text
(see that module for the full rule list). Each check here is killed by a targeted mutation; the
Phase 6 closure checks (audit-row times, COMPLETE phase, full canonical order, plan correspondence,
semantic citations) are in test_contract_closure.py. Run: python -m pytest code/tests -q
"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, case_sort_key, serialize_output, write_output  # noqa: E402
from ghostledger import contract  # noqa: E402
from ghostledger.safeio import atomic_write_text  # noqa: E402

OPS, STATUSES = contract.OPS, contract.STATUSES


def corpus(ds, index):
    """(files, rows, resources) as before; rows are dated by user_audit_log.event_time (Phase 6 closure:
    the earlier 'changed_at' key does not exist, so every audit citation passed the cutoff check)."""
    c = contract.corpus(ds, index)
    return c.files, {k: v[0] for k, v in c.rows.items()}, c.resources


def contract_errors(text, requests, files, rows, resources):
    """[(case_id, code, detail)] errors (not warnings) from ghostledger.contract for a complete output.csv text."""
    rows = {k: (v, None, None) if isinstance(v, str) else v for k, v in rows.items()}
    return contract.errors(text, requests, contract.Corpus(files, rows, resources))


_cache = {}


def sample():
    if not _cache:
        ds = load_dataset(os.path.join(REPO, "dataset"), "sample_deletion_requests.csv")
        a = Auditor(ds)
        res = a.run()
        _cache.update(ds=ds, res=res, text=serialize_output(res), corpus=corpus(ds, a.index))
    return _cache


def mutate(text, fn):
    rows = list(csv.reader(io.StringIO(text)))
    fn(rows)
    buf = io.StringIO(newline="")
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def first(rows, status):
    return next(r for r in rows[1:] if r[1] == status and r[2] != "[]")


def edit_steps(row, fn):
    s = json.loads(row[2])
    fn(s)
    row[2] = json.dumps(s, separators=(",", ":"))


class OutputContract(unittest.TestCase):
    def codes(self, text):
        c = sample()
        return {e[1] for e in contract_errors(text, c["ds"].requests, *c["corpus"])}

    def test_pipeline_sample_output_is_valid(self):
        c = sample()
        self.assertEqual(contract_errors(c["text"], c["ds"].requests, *c["corpus"]), [])

    def test_public_sample_answers_are_valid(self):
        """The validator must accept the organizer's public sample answers (no false alarms), except that
        the sample answers name 3 resource ids that occur in no participant-visible file; the pipeline must
        never do that, so FABRICATED_RESOURCE stays an error for predictions and is only counted here."""
        with open(os.path.join(REPO, "dataset", "sample_output.csv"), encoding="utf-8", newline="") as f:
            gold = f.read()
        errs = [e for e in contract_errors(gold, sample()["ds"].requests, *sample()["corpus"])
                if e[1] not in ("POST_AUDIT_CITATION",)]   # gold cites some post-audit facts (Phase 5 note)
        unseen = [e for e in errs if e[1] == "FABRICATED_RESOURCE"]
        self.assertEqual([e for e in errs if e not in unseen], [])
        self.assertEqual(len(unseen), 3)

    def test_mutations_are_detected(self):
        t = sample()["text"]
        cases = {
            "DUPLICATE": lambda r: r.insert(2, list(r[1])),
            "MISSING_CASES": lambda r: r.pop(1),
            "CASE_ORDER": lambda r: r.__setitem__(slice(1, 3), [r[2], r[1]]),
            "STATUS": lambda r: r[1].__setitem__(1, "DONE"),
            "NA_STEPS": lambda r: (first(r, "INCOMPLETE").__setitem__(1, "NOT_APPLICABLE")),
            "STEP_NO": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s[0].__setitem__("step_no", 0)),
            "ORDER": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s.reverse()),
            "STATE": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s[0].__setitem__("state", "ACTIVE")
                                          if s[0]["state"] == "PRESENT" else s[0].__setitem__("state", "PRESENT")),
            "OPERATION": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s[0].__setitem__("operation", "PURGE")),
            "FIELDS": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s[0].pop("system_id")),
            "FUTURE_CITED": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s[-1].__setitem__("evidence_id", "evidence_01")),
            "UNCITED": lambda r: edit_steps(first(r, "COMPLETE"), lambda s: s[0].__setitem__("evidence_id", "")),
            "UNKNOWN_EVIDENCE": lambda r: edit_steps(first(r, "COMPLETE"), lambda s: s[0].__setitem__("evidence_id", "evidence_999")),
            "FABRICATED_RESOURCE": lambda r: edit_steps(first(r, "COMPLETE"), lambda s: s[0].__setitem__("resource_id", "account_0000000")),
            "SYSTEM": lambda r: edit_steps(first(r, "COMPLETE"), lambda s: s[0].__setitem__("system_id", "nowhere_service")),
            "NO_FINDING": lambda r: edit_steps(first(r, "INCOMPLETE"), lambda s: s.__setitem__(
                slice(None), [dict(x, step_no=i + 1) for i, x in enumerate(y for y in s if y["phase"] != "FINDING")])),
            "HEADER": lambda r: r[0].__setitem__(2, "steps"),
        }
        for code, fn in cases.items():
            with self.subTest(code):
                self.assertIn(code, self.codes(mutate(t, fn)))

    def test_post_audit_citation_detected(self):
        c = sample()
        files = c["corpus"][0]
        req = c["ds"].requests[0]
        late = next(f for f, at in sorted(files.items()) if at > req["audit_at"])
        row = f'{req["case_id"]},COMPLETE,"[{{""step_no"":1,""phase"":""VERIFICATION"",""system_id"":""account_service"",' \
              f'""resource_id"":""{next(r for r in c["corpus"][2] if r.startswith("account_"))}"",""operation"":""VERIFY_RECORD_ABSENT"",' \
              f'""state"":""ABSENT"",""evidence_id"":""{late}""}}]"'
        errs = contract_errors("case_id,deletion_status,steps_json\n" + row + "\n", [req], *c["corpus"])
        self.assertIn("POST_AUDIT_CITATION", {e[1] for e in errs})

    def test_serializer_refuses_invalid_rows_and_keeps_previous_file(self):
        from ghostledger.pipeline import CaseResult
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "output.csv")
            atomic_write_text(p, "previous\n")
            bad = [CaseResult("case_1", "NOT_APPLICABLE", [{"step_no": 1, "phase": "FINDING", "system_id": "s",
                                                            "resource_id": "r", "operation": "REPORT_REMAINING_RECORD",
                                                            "state": "PRESENT", "evidence_id": "e"}])]
            with self.assertRaises(ValueError):
                write_output(bad, p)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "previous\n")
            self.assertEqual(sorted(os.listdir(d)), ["output.csv"])   # no temp file left behind

    def test_case_order_is_numeric_not_lexicographic(self):
        ids = ["case_100", "case_33", "case_9"]
        self.assertEqual(sorted(ids, key=case_sort_key), ["case_9", "case_33", "case_100"])


if __name__ == "__main__":
    unittest.main()
