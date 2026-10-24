"""Synthetic policy/temporal tests on hand-built miniature datasets.

These fixtures are invented here (fictional users, example.test addresses, resource ids outside
the shipped ranges). They check general behaviour of the pipeline end to end; they are not
derived from any sample or evaluation case and say nothing about hidden-test accuracy.
Run: python -m pytest code/tests -q
"""
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor  # noqa: E402

REQ_COLS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
DELETE_TEXT = "Please delete my account and all personal data you hold about me."


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def write_csv(path, cols, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def build(tmp, users, audit, activity, requests, evidence):
    """evidence: {file_id: (captured_at, collection, [records])} -> record-list JSON files."""
    os.makedirs(os.path.join(tmp, "evidence", "records"))
    shutil.copy(os.path.join(REPO, "dataset", "deletion_rules.csv"), os.path.join(tmp, "deletion_rules.csv"))
    write_csv(os.path.join(tmp, "user_identities.csv"),
              ["user_id", "name", "email", "phone", "email_hash", "updated_at"], users)
    write_csv(os.path.join(tmp, "user_audit_log.csv"),
              ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], audit)
    write_csv(os.path.join(tmp, "system_activity.csv"),
              ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
               "reported_result", "details"], activity)
    write_csv(os.path.join(tmp, "requests.csv"), REQ_COLS, requests)
    manifest = []
    for fid, (cap, coll, recs) in evidence.items():
        rel = f"evidence/records/{fid}.json"
        with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
            json.dump({"collection": coll, "captured_at": cap, "records": recs}, f)
        manifest.append({"file_id": fid, "file_path": rel, "origin": "synthetic_fixture", "captured_at": cap})
    write_csv(os.path.join(tmp, "evidence_files.csv"), ["file_id", "file_path", "origin", "captured_at"], manifest)
    results = Auditor(load_dataset(tmp, "requests.csv")).run()
    return {r.case_id: r for r in results}


def user(uid, email, phone):
    return {"user_id": uid, "name": "Synthetic Person", "email": email, "phone": phone,
            "email_hash": sha(email), "updated_at": "2026-01-01T00:00:00Z"}


def request(cid, email, audit_at, text=DELETE_TEXT):
    return {"case_id": cid, "email": email, "requested_at": "2026-08-01T00:00:00Z", "reported_status": "COMPLETE",
            "reported_at": "2026-08-02T00:00:00Z", "audit_at": audit_at, "request_text": text}


def ops(r):
    return [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in r.steps]


class SyntheticTemporal(unittest.TestCase):
    """The answer depends only on evidence captured at or before audit_at; a SUCCESS claim is not proof."""

    def test_state_as_of_audit_at_and_recreation(self):
        email = "lena.ostrova@example.test"
        tmp = tempfile.mkdtemp()
        try:
            res = build(
                tmp,
                users=[user("user_9901", email, "+44 7700 900111")],
                audit=[],
                activity=[{"activity_id": "activity_99001", "event_time": "2026-08-01T12:00:00Z",
                           "event_type": "DELETE_RECORD", "source_system": "deletion_worker",
                           "target_system": "account_service", "resource_id": "account_99001",
                           "reported_result": "SUCCESS", "details": "Deletion worker removed account_99001."}],
                requests=[request("case_s1a", email, "2026-09-05T00:00:00Z"),   # only the PRESENT capture is eligible
                          request("case_s1b", email, "2026-09-12T00:00:00Z"),   # deletion capture now eligible
                          request("case_s1c", email, "2026-09-25T00:00:00Z")],  # record recreated afterwards
                evidence={
                    "evidence_s1a": ("2026-09-01T00:00:00Z", "customer_profiles",
                                     [{"resource_id": "account_99001", "account_status": "ACTIVE", "email": email}]),
                    "evidence_s1b": ("2026-09-10T00:00:00Z", "customer_profiles",
                                     [{"resource_id": "account_99001", "account_status": "DELETED", "email": None}]),
                    "evidence_s1c": ("2026-09-20T00:00:00Z", "customer_profiles",
                                     [{"resource_id": "account_99001", "account_status": "ACTIVE", "email": email}]),
                })
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(res["case_s1a"].status, "INCOMPLETE")
        self.assertEqual(ops(res["case_s1a"]), [
            ("FINDING", "account_99001", "REPORT_REMAINING_RECORD", "evidence_s1a"),
            ("REMEDIATION", "account_99001", "DELETE_RECORD", ""),
            ("VERIFICATION", "account_99001", "VERIFY_RECORD_ABSENT", "")])
        self.assertEqual(res["case_s1b"].status, "COMPLETE")
        self.assertEqual(ops(res["case_s1b"]), [("VERIFICATION", "account_99001", "VERIFY_RECORD_ABSENT", "evidence_s1b")])
        self.assertEqual(res["case_s1c"].status, "INCOMPLETE")
        self.assertEqual(res["case_s1c"].steps[0]["evidence_id"], "evidence_s1c")


class SyntheticRetention(unittest.TestCase):
    """Retained categories are verified present, never deleted; expired retention drops the check;
    a tombstone that keeps only the stable user_id is compliant; non-deletion intent is not audited."""

    def test_retention_legal_hold_tombstone_and_intent(self):
        email = "tomas.vidal@example.test"
        tmp = tempfile.mkdtemp()
        try:
            res = build(
                tmp,
                users=[user("user_9902", email, "+34 600 900 222")],
                audit=[],
                activity=[],
                requests=[request("case_s2a", email, "2026-09-15T00:00:00Z"),
                          request("case_s2b", email, "2026-09-15T00:00:00Z",
                                  "Could you send me a copy of my data? Please do not delete anything.")],
                evidence={"evidence_s2a": ("2026-09-10T00:00:00Z", "retained_records", [
                    {"resource_id": "account_99021", "account_status": "DELETED", "user_id": "user_9902"},
                    {"resource_id": "audit_record_99022", "user_id": "user_9902", "retention_until": "2027-03-01T00:00:00Z"},
                    {"resource_id": "audit_record_99023", "user_id": "user_9902", "retention_until": "2026-02-01T00:00:00Z"},
                    {"resource_id": "legal_hold_99024", "user_id": "user_9902", "hold_status": "ACTIVE"},
                    {"resource_id": "tombstone_99025", "user_id": "user_9902", "name": None, "email": None,
                     "phone": None, "email_hash": None},
                ])})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(res["case_s2a"].status, "COMPLETE")
        self.assertEqual(ops(res["case_s2a"]), [
            ("VERIFICATION", "account_99021", "VERIFY_RECORD_ABSENT", "evidence_s2a"),
            ("VERIFICATION", "legal_hold_99024", "VERIFY_RECORD_PRESENT", "evidence_s2a"),
            ("VERIFICATION", "audit_record_99022", "VERIFY_RECORD_PRESENT", "evidence_s2a"),
            ("VERIFICATION", "tombstone_99025", "VERIFY_RECORD_REDACTED", "evidence_s2a")])
        self.assertNotIn("audit_record_99023", {s["resource_id"] for s in res["case_s2a"].steps})
        self.assertEqual((res["case_s2b"].status, res["case_s2b"].steps), ("NOT_APPLICABLE", []))


class SyntheticOrdering(unittest.TestCase):
    """Historical identifiers link records; every treatment appears once in canonical order, with
    flows disabled and restores blocked before records change; unrelated records are not attributed."""

    def test_claims_history_and_remediation_order(self):
        old, new = "maya.korhonen@example.test", "maya.k@example.test"
        tmp = tempfile.mkdtemp()
        try:
            res = build(
                tmp,
                users=[user("user_9903", new, "+358 40 900 3333"), user("user_9904", "other.person@example.test", "+1 555 900 4444")],
                audit=[{"audit_id": "audit_99031", "user_id": "user_9903", "event_time": "2025-03-01T00:00:00Z",
                        "field_name": "email", "old_value": old, "new_value": new},
                       {"audit_id": "audit_99032", "user_id": "user_9903", "event_time": "2025-03-01T00:00:00Z",
                        "field_name": "email_hash", "old_value": sha(old), "new_value": sha(new)}],
                activity=[{"activity_id": "activity_99031", "event_time": "2026-08-01T12:00:00Z",
                           "event_type": "DELETE_RECORD", "source_system": "deletion_worker",
                           "target_system": "account_service", "resource_id": "account_99031",
                           "reported_result": "SUCCESS", "details": "Deletion worker removed account_99031."}],
                requests=[request("case_s3", new, "2026-09-15T00:00:00Z")],
                evidence={"evidence_s3": ("2026-09-10T00:00:00Z", "mixed_inventory", [
                    {"resource_id": "account_99031", "http_status": 200, "rows": 1, "email": new},
                    {"resource_id": "analytics_99032", "email_hash": sha(old), "segment": "frequent_buyer"},
                    {"resource_id": "data_flow_99033", "enabled": True, "customer_reference": "user_9903"},
                    {"resource_id": "backup_99034", "restore_allowed": True, "user_id": "user_9903"},
                    {"resource_id": "support_99035", "message_text": f"Please reply to {old} about order 4411"},
                    {"resource_id": "account_99036", "http_status": 200, "rows": 1, "email": "other.person@example.test"},
                ])})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        r = res["case_s3"]
        self.assertEqual(r.status, "INCOMPLETE")
        ev = "evidence_s3"
        self.assertEqual(ops(r), [
            ("FINDING", "account_99031", "REPORT_REMAINING_RECORD", ev),
            ("FINDING", "analytics_99032", "REPORT_EXPOSED_PII", ev),
            ("FINDING", "support_99035", "REPORT_EXPOSED_PII", ev),
            ("FINDING", "data_flow_99033", "REPORT_ACTIVE_DATA_FLOW", ev),
            ("FINDING", "backup_99034", "REPORT_RESTORE_RISK", ev),
            ("REMEDIATION", "data_flow_99033", "DISABLE_DATA_FLOW", ""),
            ("REMEDIATION", "backup_99034", "BLOCK_RESTORE", ""),
            ("REMEDIATION", "account_99031", "DELETE_RECORD", ""),
            ("REMEDIATION", "support_99035", "REDACT_RECORD", ""),
            ("REMEDIATION", "analytics_99032", "ANONYMIZE_RECORD", ""),
            ("VERIFICATION", "account_99031", "VERIFY_RECORD_ABSENT", ""),
            ("VERIFICATION", "support_99035", "VERIFY_RECORD_REDACTED", ""),
            ("VERIFICATION", "analytics_99032", "VERIFY_RECORD_ANONYMIZED", ""),
            ("VERIFICATION", "data_flow_99033", "VERIFY_DATA_FLOW_DISABLED", ""),
            ("VERIFICATION", "backup_99034", "VERIFY_RESTORE_BLOCKED", "")])
        self.assertEqual([s["step_no"] for s in r.steps], list(range(1, 16)))


if __name__ == "__main__":
    unittest.main()
