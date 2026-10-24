"""Output-contract validator (AGENTS.md section 6.3-6.6) and its tests.

contract_errors() checks a complete output.csv text against the requests and the evidence corpus:
header, one row per case with no duplicates, numeric case order, statuses, step fields, consecutive
step numbers, phase and canonical operation order, operation/state pairs, citations (findings and
COMPLETE verifications cite an eligible participant-visible id; future steps cite nothing),
NOT_APPLICABLE with [], and no fabricated resource or system ids. Each check is killed by a
targeted mutation below. Run: python -m pytest code/tests -q
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

from ghostledger.evidence import system_of  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, case_sort_key, serialize_output, write_output  # noqa: E402
from ghostledger.safeio import atomic_write_text  # noqa: E402
from ghostledger.steps import FIELDS, OP_ORDER, PHASES  # noqa: E402

OPS = {
    "FINDING": {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
                "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE"},
    "REMEDIATION": {"DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
                    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED"},
    "VERIFICATION": {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
                     "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
                     "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"},
}
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


def corpus(ds, index):
    """Known ids: evidence files with capture times, activity/audit rows with event times, resources."""
    files = {f: m["captured_at"] for f, m in ds.manifest.items()}
    rows = {a["activity_id"]: a["event_time"] for a in ds.activity}
    rows.update({a["audit_id"]: a.get("changed_at", "") for a in ds.audit_log})
    resources = set(index.by_resource) | {a["resource_id"] for a in ds.activity if a.get("resource_id")}
    return files, rows, resources


def contract_errors(text, requests, files, rows, resources):
    """[(case_id, code, detail)] for every contract violation in a complete output.csv text."""
    errs = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header != ["case_id", "deletion_status", "steps_json"]:
        errs.append(("*", "HEADER", str(header)))
    audit = {r["case_id"]: r["audit_at"] for r in requests}
    seen, order = set(), []
    for rec in reader:
        if len(rec) != 3:
            errs.append(("*", "ROW_SHAPE", str(rec)[:80]))
            continue
        cid, status, raw = rec
        order.append(cid)
        if cid in seen:
            errs.append((cid, "DUPLICATE", ""))
        seen.add(cid)
        if cid not in audit:
            errs.append((cid, "UNKNOWN_CASE", ""))
            continue
        if status not in STATUSES:
            errs.append((cid, "STATUS", status))
        try:
            steps = json.loads(raw)
        except ValueError:
            errs.append((cid, "JSON", raw[:60]))
            continue
        if not isinstance(steps, list):
            errs.append((cid, "JSON", "not an array"))
            continue
        if status == "NOT_APPLICABLE":
            if steps:
                errs.append((cid, "NA_STEPS", str(len(steps))))
            continue
        if not steps and status == "INCOMPLETE":
            errs.append((cid, "EMPTY_STEPS", status))
        last = (-1, -1)
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict) or set(s) != set(FIELDS):
                errs.append((cid, "FIELDS", str(sorted(s) if isinstance(s, dict) else s)))
                continue
            if s["step_no"] != i:
                errs.append((cid, "STEP_NO", f"{s['step_no']}!={i}"))
            ph, op = s["phase"], s["operation"]
            if ph not in OPS or op not in OPS[ph]:
                errs.append((cid, "OPERATION", f"{ph}/{op}"))
                continue
            if OPS[ph][op] != s["state"]:
                errs.append((cid, "STATE", f"{op}/{s['state']}"))
            pos = (PHASES.index(ph), OP_ORDER[ph].index(op))
            if pos < last:
                errs.append((cid, "ORDER", f"step {i} {ph}/{op}"))
            last = pos
            rid, ev = s["resource_id"], s["evidence_id"]
            if rid not in resources:
                errs.append((cid, "FABRICATED_RESOURCE", rid))
            elif s["system_id"] != system_of(rid):
                errs.append((cid, "SYSTEM", f"{rid}->{s['system_id']}"))
            future = ph == "REMEDIATION" or (ph == "VERIFICATION" and status == "INCOMPLETE")
            if future:
                if ev != "":
                    errs.append((cid, "FUTURE_CITED", f"step {i} {ev}"))
                continue
            if status == "COMPLETE" and ph != "VERIFICATION":
                errs.append((cid, "COMPLETE_PHASE", ph))
            if not ev:
                errs.append((cid, "UNCITED", f"step {i} {ph}"))
            elif ev in files:
                if files[ev] > audit[cid]:
                    errs.append((cid, "POST_AUDIT_CITATION", f"{ev} {files[ev]}"))
            elif ev in rows:
                if rows[ev] and rows[ev] > audit[cid]:
                    errs.append((cid, "POST_AUDIT_CITATION", f"{ev} {rows[ev]}"))
            else:
                errs.append((cid, "UNKNOWN_EVIDENCE", ev))
        if status == "INCOMPLETE" and not any(isinstance(s, dict) and s.get("phase") == "FINDING" for s in steps):
            errs.append((cid, "NO_FINDING", ""))
    missing = set(audit) - seen
    if missing:
        errs.append(("*", "MISSING_CASES", str(len(missing))))
    if order != sorted(order, key=case_sort_key):
        errs.append(("*", "CASE_ORDER", "not numeric case order"))
    return errs


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
