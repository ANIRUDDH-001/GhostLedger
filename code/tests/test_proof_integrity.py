"""Proof-integrity probes (Phase 4 A): six situations where evidence cannot support the output.

Each test pins two things on an invented miniature dataset (example.test addresses, resource ids
outside the shipped ranges):
  current  - what the default pipeline emits today; these assertions document a defect and are
             expected to change deliberately when the defect is fixed;
  ledger   - how the obligation ledger (ghostledger/ledger.py) classifies the same evidence.
Run: python -m pytest code/tests -q
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger import ledger  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor  # noqa: E402
from test_synthetic import REQ_COLS, request, sha, user, write_csv  # noqa: E402

EMAIL = "noor.haddad@example.test"
UID = "user_9951"


def build(files, audit_at="2026-09-15T00:00:00Z", activity=()):
    """files: {file_id: (captured_at, json_document)} -> (auditor, result, ledger entries) for one case."""
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "evidence", "records"))
        shutil.copy(os.path.join(REPO, "dataset", "deletion_rules.csv"), os.path.join(tmp, "deletion_rules.csv"))
        write_csv(os.path.join(tmp, "user_identities.csv"),
                  ["user_id", "name", "email", "phone", "email_hash", "updated_at"], [user(UID, EMAIL, "+971 50 900 5151")])
        write_csv(os.path.join(tmp, "user_audit_log.csv"),
                  ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
        write_csv(os.path.join(tmp, "system_activity.csv"),
                  ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                   "reported_result", "details"], list(activity))
        req = request("case_p", EMAIL, audit_at)
        write_csv(os.path.join(tmp, "requests.csv"), REQ_COLS, [req])
        manifest = []
        for fid, (cap, doc) in files.items():
            rel = f"evidence/records/{fid}.json"
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                json.dump(doc, f)
            manifest.append({"file_id": fid, "file_path": rel, "origin": "synthetic_fixture", "captured_at": cap})
        write_csv(os.path.join(tmp, "evidence_files.csv"), ["file_id", "file_path", "origin", "captured_at"], manifest)
        aud = Auditor(load_dataset(tmp, "requests.csv"))
        res = aud.run()[0]
        led = {e.resource_id: e for e in ledger.obligations(aud, req)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return aud, res, led


def records(cap, recs, coll="records"):
    return cap, {"collection": coll, "captured_at": cap, "records": recs}


def ops(r):
    return [(s["phase"], s["resource_id"], s["operation"], s["state"], s["evidence_id"]) for s in r.steps]


class ProofIntegrity(unittest.TestCase):
    def test_1_known_required_retained_record_absent(self):
        aud, r, led = build({
            "ev_p1": records("2026-09-10T00:00:00Z", [
                {"resource_id": "account_99511", "account_status": "DELETED", "user_id": UID},
                {"resource_id": "audit_record_99512", "record_type": "SECURITY_AUDIT_LOG", "result": "NOT_FOUND",
                 "user_id": UID, "retention_until": "2027-06-01T00:00:00Z"}])})
        # current (defect): the missing retained record is dropped and the case is COMPLETE
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "account_99511", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_p1")])
        self.assertIn("retained record missing", r.trace["dropped"]["audit_record_99512"])
        # ledger: a proven violation that no allowed operation can report
        self.assertEqual(led["audit_record_99512"].status, ledger.PROVEN_VIOLATION)
        self.assertIn("unreportable_violation", led["audit_record_99512"].flags)
        self.assertEqual(ledger.case_decision(led.values()), "VIOLATION_UNREPORTABLE")

    def test_2_linked_unknown_account(self):
        aud, r, led = build({
            "ev_p2a": records("2026-09-10T00:00:00Z", [{"resource_id": "search_99521", "total_hits": 0, "email": EMAIL}]),
            # the only sighting of the account is captured after audit_at
            "ev_p2b": records("2026-09-20T00:00:00Z", [{"resource_id": "account_99522", "http_status": 200, "rows": 1,
                                                        "email": EMAIL}])})
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "search_99521", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_p2a")])
        self.assertIn("not established", r.trace["dropped"]["account_99522"])
        self.assertEqual(led["account_99522"].status, ledger.UNRESOLVED)
        self.assertEqual(ledger.case_decision(led.values()), "UNRESOLVED")

    def test_3_multiple_live_account_copies(self):
        aud, r, led = build({
            "ev_p3a": records("2026-09-01T00:00:00Z", [{"resource_id": "account_99531", "account_status": "ACTIVE",
                                                        "email": EMAIL}]),
            "ev_p3b": records("2026-09-05T00:00:00Z", [{"resource_id": "account_99532", "account_status": "DELETED",
                                                        "user_id": UID}])})
        # current (defect): the live older account is suppressed as "superseded"
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "account_99532", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_p3b")])
        self.assertIn("superseded", r.trace["dropped"]["account_99531"])
        self.assertEqual(led["account_99531"].status, ledger.PROVEN_VIOLATION)
        self.assertTrue(any(f.startswith("suppressed_by_one_primary_account") for f in led["account_99531"].flags))
        self.assertEqual(ledger.case_decision(led.values()), "INCOMPLETE")

    def test_4_absent_record_verified_as_redacted_anonymized_disabled_blocked(self):
        def snap(coll, rid):
            return {"collection": coll, "captured_at": "2026-09-10T00:00:00Z", "records": [{"resource_id": rid}]}
        aud, r, led = build({
            # enumerating snapshots before audit_at that do not list the subject's records
            "ev_p4s": ("2026-09-10T00:00:00Z", {"snapshots": [
                snap("deleted_customer_references", "tombstone_99549"), snap("backup_catalog", "backup_99549"),
                snap("connector_configurations", "data_flow_99549"), snap("customer_analytics_reports", "analytics_99549")]}),
            # identifiers for the subject's records appear only in a later capture
            "ev_p4l": records("2026-09-20T00:00:00Z", [
                {"resource_id": "tombstone_99541", "user_id": UID, "email": EMAIL},
                {"resource_id": "backup_99542", "restore_allowed": True, "user_id": UID},
                {"resource_id": "data_flow_99543", "enabled": True, "customer_reference": UID},
                {"resource_id": "analytics_99544", "email": EMAIL}])})
        # fixed (Phase 7 final A1): an omission proves absence only, so no step asserts a treatment state
        # the cited file never shows; every obligation stays open and is accounted in the trace
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [])
        self.assertEqual(r.trace["unresolved"], {
            "analytics_99544": "OMISSION_DOES_NOT_ESTABLISH_ANONYMIZED",
            "backup_99542": "OMISSION_DOES_NOT_ESTABLISH_BLOCKED",
            "data_flow_99543": "OMISSION_DOES_NOT_ESTABLISH_DISABLED",
            "tombstone_99541": "OMISSION_DOES_NOT_ESTABLISH_REDACTED"})
        for rid in ("tombstone_99541", "backup_99542", "data_flow_99543", "analytics_99544"):
            self.assertTrue(any(f.startswith("verify_state_mismatch") for f in led[rid].flags), rid)
            self.assertTrue(led[rid].basis.startswith("OMISSION:"), rid)
            self.assertEqual(led[rid].status, ledger.UNRESOLVED, rid)
            self.assertIn("omission does not establish", led[rid].reason)

    def test_5_http_500_account_readback(self):
        aud, r, led = build({
            "ev_p5": records("2026-09-10T00:00:00Z", [
                {"resource_id": "account_99551", "http_status": 500, "rows": 0, "email": EMAIL},
                {"resource_id": "account_99552", "status_code": 500, "profile": None, "user_id": UID}])})
        # current (defect): a server error is read as "record absent"
        self.assertEqual(r.trace["states"].get("account_99552", r.trace["states"].get("account_99551"))[0], "ABSENT")
        self.assertEqual(r.status, "COMPLETE")
        for rid in ("account_99551", "account_99552"):
            self.assertEqual(led[rid].status, ledger.UNRESOLVED, rid)
            self.assertTrue(any(f.startswith("indeterminate_readback") for f in led[rid].flags), rid)

    def test_6_unknown_restore_eligibility(self):
        aud, r, led = build({
            "ev_p6": records("2026-09-10T00:00:00Z", [
                {"resource_id": "account_99561", "account_status": "DELETED", "user_id": UID},
                {"resource_id": "backup_99562", "eligible": "pending_review", "deletion_block": False, "user_id": UID},
                {"resource_id": "backup_99563", "user_id": UID}])})
        # current (defect): unknown eligibility is read as BLOCKED; a backup with no restore field is dropped
        self.assertEqual(r.trace["states"]["backup_99562"][0], "BLOCKED")
        self.assertNotIn("backup_99563", {s["resource_id"] for s in r.steps})
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(led["backup_99562"].status, ledger.UNRESOLVED)
        self.assertEqual(led["backup_99563"].status, ledger.UNRESOLVED)
        self.assertEqual(ledger.case_decision(led.values()), "UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
