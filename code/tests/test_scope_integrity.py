"""Scope, replica and claim probes (Phase 4 C) on invented miniature datasets.

  current - what the default pipeline emits today (documents behaviour, not correctness);
  ledger  - the analysis-only reading with typed scopes (ledger.ScopeTyper) and per-copy replica checks.
Run: python -m pytest code/tests -q
"""
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
from test_synthetic import REQ_COLS, request, user, write_csv  # noqa: E402

EMAIL = "aiko.brandt@example.test"
UID = "user_9971"
LATER = "2026-09-20T00:00:00Z"


def build(files):
    """files: {file_id: (captured_at, ext, content, origin)} -> (auditor, case result, ledger entries)."""
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "evidence", "records"))
        shutil.copy(os.path.join(REPO, "dataset", "deletion_rules.csv"), os.path.join(tmp, "deletion_rules.csv"))
        write_csv(os.path.join(tmp, "user_identities.csv"), ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
                  [user(UID, EMAIL, "+49 151 900 7171")])
        write_csv(os.path.join(tmp, "user_audit_log.csv"),
                  ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
        write_csv(os.path.join(tmp, "system_activity.csv"),
                  ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                   "reported_result", "details"], [])
        req = request("case_c", EMAIL, "2026-09-15T00:00:00Z")
        write_csv(os.path.join(tmp, "requests.csv"), REQ_COLS, [req])
        manifest = []
        for fid, (cap, ext, content, origin) in files.items():
            rel = f"evidence/records/{fid}.{ext}"
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else json.dumps(content))
            manifest.append({"file_id": fid, "file_path": rel, "origin": origin, "captured_at": cap})
        write_csv(os.path.join(tmp, "evidence_files.csv"), ["file_id", "file_path", "origin", "captured_at"], manifest)
        aud = Auditor(load_dataset(tmp, "requests.csv"))
        res = aud.run()[0]
        led = {e.resource_id: e for e in ledger.obligations(aud, req, ledger.ScopeTyper(aud).status, replicas=True)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return aud, res, led


def snapshot(cap, coll, recs):
    return cap, "json", {"snapshots": [{"collection": coll, "captured_at": cap, "records": recs}]}, "fixture_inventory"


def later_identity(rid):
    # the subject's account is identified only by a capture after audit_at, so its state at audit_at
    # can come from omission alone
    return LATER, "json", {"collection": "records", "captured_at": LATER,
                           "records": [{"resource_id": rid, "account_status": "ACTIVE", "email": EMAIL}]}, "fixture"


def records(cap, recs, origin="fixture"):
    return cap, "json", {"collection": "records", "captured_at": cap, "records": recs}, origin


def ops(r):
    return [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in r.steps]


class ScopeIntegrity(unittest.TestCase):
    def test_filtered_scope_cannot_prove_absence(self):
        other = {"resource_id": "account_99712", "account_status": "ACTIVE", "user_id": "user_9979"}
        aud, r, led = build({
            "ev_c1a": records("2026-09-05T00:00:00Z", [other]),
            # the listing omits account_99712, which is live both before and after it
            "ev_c1s": snapshot("2026-09-10T00:00:00Z", "customer_profiles",
                               [{"resource_id": "account_99711", "account_status": "ACTIVE", "user_id": "user_9978"}]),
            "ev_c1b": records("2026-09-12T00:00:00Z", [other]),
            "ev_c1l": later_identity("account_99713")})
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "account_99713", "VERIFY_RECORD_ABSENT", "ev_c1s")])
        e = led["account_99713"]
        self.assertIn("scope:FILTERED", e.flags)
        self.assertEqual(e.status, ledger.UNRESOLVED)

    def test_undeclared_versus_declared_scope(self):
        aud, r, led = build({
            "ev_c2s": snapshot("2026-09-10T00:00:00Z", "customer_profiles",
                               [{"resource_id": "account_99721", "account_status": "ACTIVE", "user_id": "user_9978"}]),
            "ev_c2l": later_identity("account_99722")})
        self.assertEqual(r.status, "COMPLETE")
        self.assertIn("scope:UNKNOWN", led["account_99722"].flags)
        self.assertEqual(led["account_99722"].status, ledger.UNRESOLVED)
        sql = ("CREATE TABLE snapshot_scope (origin_system TEXT, captured_at TEXT);\n"
               "INSERT INTO snapshot_scope VALUES ('account_service', '2026-09-10T00:00:00Z');\n"
               "CREATE TABLE customer_profiles (resource_id TEXT, customer_reference TEXT, account_status TEXT, updated_at TEXT);\n"
               "INSERT INTO customer_profiles VALUES ('account_99721', 'user_9978', 'ACTIVE', '2026-09-10T00:00:00Z');\n")
        aud, r, led = build({"ev_c2q": ("2026-09-10T00:00:00Z", "sql", sql, "fixture_inventory"),
                             "ev_c2l": later_identity("account_99722")})
        self.assertEqual(ops(r), [("VERIFICATION", "account_99722", "VERIFY_RECORD_ABSENT", "ev_c2q")])
        self.assertIn("scope:FULL", led["account_99722"].flags)
        self.assertEqual(led["account_99722"].status, ledger.PROVEN_COMPLIANT)

    def test_unrelated_scope(self):
        aud, r, led = build({
            "ev_c3s": snapshot("2026-09-10T00:00:00Z", "customer_profiles", [{"resource_id": "legacy_99731", "flag": 1}]),
            "ev_c3l": later_identity("account_99732")})
        self.assertEqual(r.status, "COMPLETE")
        self.assertIn("scope:UNRELATED", led["account_99732"].flags)
        self.assertEqual(led["account_99732"].status, ledger.UNRESOLVED)

    def test_newer_primary_absent_hides_live_failover_copy(self):
        def sql(failover_status):
            return ("CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, email TEXT, account_status TEXT, updated_at TEXT);\n"
                    f"INSERT INTO customer_profiles_primary VALUES ('account_99741', '{UID}', '{EMAIL}', 'ACTIVE', '2026-09-05T00:00:00Z');\n"
                    "CREATE TABLE customer_profiles_failover_replica (resource_id TEXT, user_id TEXT, email TEXT, account_status TEXT, updated_at TEXT);\n"
                    f"INSERT INTO customer_profiles_failover_replica VALUES ('account_99741', '{UID}', '{EMAIL}', '{failover_status}', '2026-09-05T00:00:00Z');\n")
        newer = records("2026-09-10T00:00:00Z", [{"resource_id": "account_99741", "account_status": "DELETED", "user_id": UID}])
        aud, r, led = build({"ev_c4q": ("2026-09-05T00:00:00Z", "sql", sql("ACTIVE"), "fixture_db"), "ev_c4r": newer})
        # current: the newer role-less readback decides, the live failover copy is not reported
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "account_99741", "VERIFY_RECORD_ABSENT", "ev_c4r")])
        e = led["account_99741"]
        self.assertTrue(any(f.startswith("live_replica_copy:customer_profiles_failover_replica@ev_c4q") for f in e.flags))
        self.assertEqual(e.status, ledger.PROVEN_VIOLATION)
        self.assertEqual(ledger.case_decision(led.values()), "INCOMPLETE")
        # control: a failover copy already deleted in the older capture raises nothing
        aud, r, led = build({"ev_c4q": ("2026-09-05T00:00:00Z", "sql", sql("DELETED"), "fixture_db"), "ev_c4r": newer})
        self.assertFalse(any(f.startswith("live_replica_copy") for f in led["account_99741"].flags))
        self.assertEqual(led["account_99741"].status, ledger.PROVEN_COMPLIANT)

    def test_claim_classes(self):
        row = lambda rid, system: {"resource_id": rid, "system": system, "record_type": "USER_PROFILE" if system == "account_service"
                                   else "SECURITY_AUDIT_LOG", "result": "NOT_FOUND", "subject_reference": EMAIL,
                                   "recorded_at": "2026-09-10T00:00:00Z"}
        aud, r, led = build({"ev_c5": records("2026-09-10T00:00:00Z", [row("account_99751", "account_service")],
                                              origin="security_audit_service")})
        self.assertEqual(led["account_99751"].basis, "CLAIM")
        self.assertIn("claim:CROSS_SYSTEM_REPORT", led["account_99751"].flags)
        aud, r, led = build({"ev_c5": records("2026-09-10T00:00:00Z", [row("account_99752", "account_service")],
                                              origin="customer_account_service")})
        self.assertIn("claim:SELF_REPORT", led["account_99752"].flags)


if __name__ == "__main__":
    unittest.main()
