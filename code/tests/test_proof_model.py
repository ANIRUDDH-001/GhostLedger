"""Proof-model regressions (Phase 5 B2, B3) on invented miniature datasets.

B2: eight situations where an output can look valid without being proven. Each test pins what the
default pipeline emits today (`current`), and how ghostledger/proof.py separates coverage,
compliance and citation for the same evidence.
B3: a citation can name the right record (identity) and still fail to prove the step's state
(authority).
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

from ghostledger import ledger, proof  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor  # noqa: E402
from test_synthetic import REQ_COLS, request, user, write_csv  # noqa: E402

EMAIL = "lena.okafor@example.test"
UID = "user_9981"
AUDIT_AT = "2026-09-15T00:00:00Z"
CAP = "2026-09-10T00:00:00Z"
LATER = "2026-09-20T00:00:00Z"


def build(files, activity=()):
    """files: {file_id: (captured_at, ext, content, origin)} -> (auditor, request, case result)."""
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "evidence", "records"))
        shutil.copy(os.path.join(REPO, "dataset", "deletion_rules.csv"), os.path.join(tmp, "deletion_rules.csv"))
        write_csv(os.path.join(tmp, "user_identities.csv"), ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
                  [user(UID, EMAIL, "+44 7700 900981")])
        write_csv(os.path.join(tmp, "user_audit_log.csv"),
                  ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
        write_csv(os.path.join(tmp, "system_activity.csv"),
                  ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                   "reported_result", "details"], list(activity))
        req = request("case_b", EMAIL, AUDIT_AT)
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return aud, req, res


def records(cap, recs, origin="fixture"):
    return cap, "json", {"collection": "records", "captured_at": cap, "records": recs}, origin


def snapshot(cap, coll, recs):
    return cap, "json", {"snapshots": [{"collection": coll, "captured_at": cap, "records": recs}]}, "fixture_inventory"


def lookup_claim(rid, result, record_type="USER_PROFILE", system="account_service"):
    # a record-list row in which one service reports the result of a lookup in another
    return records(CAP, [{"resource_id": rid, "system": system, "record_type": record_type, "result": result,
                          "subject_reference": EMAIL, "recorded_at": CAP}], origin="security_audit_service")


def readback(rid, http_status=404, rows=0, cap=CAP):
    return records(cap, [{"resource_id": rid, "http_status": http_status, "rows": rows, "email": EMAIL}])


def assess(built, typed=False):
    aud, req, res = built
    return res, proof.assess(aud, req, res, ledger.ScopeTyper(aud).status if typed else None)


def ops(r):
    return [(s["phase"], s["resource_id"], s["state"], s["evidence_id"]) for s in r.steps]


def by_rid(cp):
    return {p.resource_id: p for p in cp.proofs}


def citation(cp, rid):
    return next(c[5] for c in cp.citations if c[2] == rid)


class ProofModelRegressions(unittest.TestCase):
    def test_1_required_retained_record_absent(self):
        # the only absence signal the parsers produce for a retained audit record is a lookup claim
        res, cp = assess(build({"ev_b1": records(CAP, [
            {"resource_id": "account_99811", "account_status": "DELETED", "user_id": UID},
            {"resource_id": "audit_record_99812", "system": "security_audit", "record_type": "SECURITY_AUDIT_LOG",
             "result": "NOT_FOUND", "user_id": UID}])}))
        self.assertEqual(res.status, "COMPLETE")                       # current: the missing record is dropped
        self.assertEqual(ops(res), [("VERIFICATION", "account_99811", "ABSENT", "ev_b1")])
        p = by_rid(cp)["audit_record_99812"]
        self.assertEqual((p.compliance, p.coverage, p.proven), (ledger.PROVEN_VIOLATION, proof.CLAIMED, False))
        self.assertIn("unreportable_violation", p.entry.flags)
        self.assertEqual(cp.ledger_decision, "VIOLATION_UNREPORTABLE")
        self.assertEqual(cp.proof_decision, "UNRESOLVED")              # a claim cannot prove the violation either
        self.assertEqual(cp.hidden_claimed, ["audit_record_99812"])
        self.assertIn("audit_record_99812", cp.gaps)

    def test_2_account_in_unknown_state(self):
        res, cp = assess(build({
            "ev_b2a": records(CAP, [{"resource_id": "search_99821", "total_hits": 0, "email": EMAIL}]),
            "ev_b2b": records(LATER, [{"resource_id": "account_99822", "account_status": "ACTIVE", "email": EMAIL}])}))
        self.assertEqual(res.status, "COMPLETE")
        p = by_rid(cp)["account_99822"]
        self.assertEqual((p.coverage, p.compliance, p.proven), (proof.UNCOVERED, ledger.UNRESOLVED, False))
        # every citation in the output is valid; the case is still not proven
        self.assertEqual([c[5].authority for c in cp.citations], [proof.AUTHORITATIVE])
        self.assertEqual(cp.unsupported_verifications, [])
        self.assertEqual(cp.gaps, ["account_99822"])
        self.assertEqual(cp.proof_decision, "UNRESOLVED")

    def test_2b_nothing_linked_is_not_proof(self):
        # evidence about someone else only: the pipeline emits COMPLETE with no steps
        res, cp = assess(build({"ev_b2c": records(CAP, [
            {"resource_id": "account_99823", "account_status": "ACTIVE", "email": "someone.else@example.test"}])}))
        self.assertEqual((res.status, res.steps), ("COMPLETE", []))
        self.assertEqual((cp.proofs, cp.ledger_decision), ([], "COMPLETE"))
        self.assertEqual(cp.proof_decision, "UNRESOLVED")

    def test_3_multiple_live_account_copies(self):
        res, cp = assess(build({
            "ev_b3a": records("2026-09-01T00:00:00Z", [{"resource_id": "account_99831", "account_status": "ACTIVE",
                                                        "email": EMAIL}]),
            "ev_b3b": records("2026-09-05T00:00:00Z", [{"resource_id": "account_99832", "account_status": "DELETED",
                                                        "user_id": UID}])}))
        self.assertEqual(res.status, "COMPLETE")                       # current: the live copy is "superseded"
        self.assertEqual(citation(cp, "account_99832").authority, proof.AUTHORITATIVE)
        self.assertEqual(cp.hidden_violations, ["account_99831"])
        self.assertEqual(cp.proof_decision, "INCOMPLETE")

    def test_4_catalog_only_resource(self):
        res, cp = assess(build(
            {"ev_b4": records(CAP, [{"resource_id": "search_99841", "total_hits": 0, "email": EMAIL}])},
            activity=[{"activity_id": "activity_99842", "event_time": "2026-09-01T00:00:00Z",
                       "event_type": "LINK_RESOURCE_IDENTITY", "source_system": "data_catalog",
                       "target_system": "marketing_platform", "resource_id": "marketing_99842",
                       "reported_result": "SUCCESS", "details": f"Catalog linked marketing_99842 to {EMAIL}."}]))
        self.assertEqual(res.status, "COMPLETE")
        self.assertNotIn("marketing_99842", {s["resource_id"] for s in res.steps})
        p = by_rid(cp)["marketing_99842"]
        self.assertEqual((p.entry.basis, p.coverage, p.compliance, p.proven),
                         ("CATALOG_ONLY", proof.UNCOVERED, ledger.UNRESOLVED, False))
        self.assertEqual(cp.gaps, ["marketing_99842"])
        self.assertEqual(cp.invented_resources, [])
        # reported as a coverage gap, not counted in the decision (pending a ruling, checkpoint section 5)
        self.assertEqual(cp.proof_decision, "COMPLETE")

    def test_5_absence_does_not_prove_redacted_anonymized_disabled_blocked(self):
        def snap(coll, rid):
            return {"collection": coll, "captured_at": CAP, "records": [{"resource_id": rid}]}
        built = build({
            "ev_b5s": (CAP, "json", {"snapshots": [
                snap("deleted_customer_references", "tombstone_99859"), snap("backup_catalog", "backup_99859"),
                snap("connector_configurations", "data_flow_99859"), snap("customer_analytics_reports", "analytics_99859")]},
                "fixture_inventory"),
            "ev_b5l": records(LATER, [
                {"resource_id": "tombstone_99851", "user_id": UID, "email": EMAIL},
                {"resource_id": "backup_99852", "restore_allowed": True, "user_id": UID},
                {"resource_id": "data_flow_99853", "enabled": True, "customer_reference": UID},
                {"resource_id": "analytics_99854", "email": EMAIL}])})
        res, cp = assess(built)
        self.assertEqual(res.status, "COMPLETE")
        rids = ["analytics_99854", "backup_99852", "data_flow_99853", "tombstone_99851"]
        for rid in rids:
            c = citation(cp, rid)
            self.assertEqual((c.identity, c.authority, c.reading), ("SCOPE", "STATE_MISMATCH", "ABSENT"), rid)
        self.assertEqual(sorted(cp.unsupported_verifications), rids)
        # compliance and citation are separate: absence satisfies DISABLE and ANONYMIZE, and that
        # compliance is proven (ENUMERATED), yet the cited capture cannot show DISABLED/ANONYMIZED
        for rid in ("data_flow_99853", "analytics_99854"):
            p = by_rid(cp)[rid]
            self.assertEqual((p.compliance, p.coverage, p.proven), (ledger.PROVEN_COMPLIANT, proof.ENUMERATED, True))
        # control: a direct readback of a redacted tombstone supports VERIFY_RECORD_REDACTED
        res, cp = assess(build({"ev_b5c": records(CAP, [{"resource_id": "tombstone_99855", "user_id": UID,
                                                         "name": None, "email": None, "phone": None}])}))
        self.assertEqual(ops(res), [("VERIFICATION", "tombstone_99855", "REDACTED", "ev_b5c")])
        self.assertEqual(citation(cp, "tombstone_99855").authority, proof.AUTHORITATIVE)

    def test_6_partial_inventory_omission_is_not_absence(self):
        other = {"resource_id": "account_99862", "account_status": "ACTIVE", "user_id": "user_9989"}
        later = records(LATER, [{"resource_id": "account_99863", "account_status": "ACTIVE", "email": EMAIL}])
        res, cp = assess(build({
            "ev_b6a": records("2026-09-05T00:00:00Z", [other]),
            "ev_b6s": snapshot(CAP, "customer_profiles",
                               [{"resource_id": "account_99861", "account_status": "ACTIVE", "user_id": "user_9988"}]),
            "ev_b6b": records("2026-09-12T00:00:00Z", [other]),
            "ev_b6l": later}), typed=True)
        self.assertEqual(ops(res), [("VERIFICATION", "account_99863", "ABSENT", "ev_b6s")])
        c = citation(cp, "account_99863")
        self.assertEqual((c.identity, c.authority), ("SCOPE", "OMISSION_UNADMITTED"))
        self.assertEqual(by_rid(cp)["account_99863"].coverage, proof.UNCOVERED)
        self.assertEqual(cp.unsupported_verifications, ["account_99863"])
        # control: a capture that declares its scope (snapshot_scope) and is not contradicted
        sql = ("CREATE TABLE snapshot_scope (origin_system TEXT, captured_at TEXT);\n"
               f"INSERT INTO snapshot_scope VALUES ('account_service', '{CAP}');\n"
               "CREATE TABLE customer_profiles (resource_id TEXT, customer_reference TEXT, account_status TEXT, updated_at TEXT);\n"
               f"INSERT INTO customer_profiles VALUES ('account_99861', 'user_9988', 'ACTIVE', '{CAP}');\n")
        res, cp = assess(build({"ev_b6q": (CAP, "sql", sql, "fixture_inventory"), "ev_b6l": later}), typed=True)
        self.assertEqual(citation(cp, "account_99863").authority, proof.AUTHORITATIVE)
        self.assertEqual(by_rid(cp)["account_99863"].coverage, proof.ENUMERATED)

    def test_7_newer_clean_primary_conceals_older_live_replica(self):
        def sql(failover_status):
            cols = "(resource_id TEXT, user_id TEXT, email TEXT, account_status TEXT, updated_at TEXT)"
            return (f"CREATE TABLE customer_profiles_primary {cols};\n"
                    f"INSERT INTO customer_profiles_primary VALUES ('account_99871', '{UID}', '{EMAIL}', 'ACTIVE', '2026-09-05T00:00:00Z');\n"
                    f"CREATE TABLE customer_profiles_failover_replica {cols};\n"
                    f"INSERT INTO customer_profiles_failover_replica VALUES ('account_99871', '{UID}', '{EMAIL}', "
                    f"'{failover_status}', '2026-09-05T00:00:00Z');\n")
        newer = records(CAP, [{"resource_id": "account_99871", "account_status": "DELETED", "user_id": UID}])
        res, cp = assess(build({"ev_b7q": ("2026-09-05T00:00:00Z", "sql", sql("ACTIVE"), "fixture_db"), "ev_b7r": newer}))
        self.assertEqual(ops(res), [("VERIFICATION", "account_99871", "ABSENT", "ev_b7r")])
        # the citation is authoritative for what it reads (the primary copy is gone) ...
        self.assertEqual(citation(cp, "account_99871").authority, proof.AUTHORITATIVE)
        # ... and the obligation is still violated by the live failover copy
        self.assertEqual(cp.hidden_violations, ["account_99871"])
        self.assertEqual(cp.proof_decision, "INCOMPLETE")
        res, cp = assess(build({"ev_b7q": ("2026-09-05T00:00:00Z", "sql", sql("DELETED"), "fixture_db"), "ev_b7r": newer}))
        self.assertEqual((cp.hidden_violations, cp.proof_decision), ([], "COMPLETE"))

    def test_8_deletion_claim_without_independent_state_proof(self):
        # Phase 7: a claim-only file gives no reading, so the claim is neither cited nor silently
        # dropped: no verification is emitted and the obligation is recorded as unresolved
        res, cp = assess(build({"ev_b8": lookup_claim("account_99881", "NOT_FOUND")}))
        self.assertEqual(ops(res), [])
        self.assertEqual(res.trace["unresolved"], {"account_99881": "UNKNOWN_STATE"})
        p = by_rid(cp)["account_99881"]
        self.assertEqual((p.compliance, p.coverage, p.proven), (ledger.UNRESOLVED, proof.CLAIMED, False))
        self.assertEqual(p.entry.basis, "CLAIM")
        self.assertEqual(cp.unsupported_verifications, [])
        self.assertEqual((cp.ledger_decision, cp.proof_decision), ("UNRESOLVED", "UNRESOLVED"))
        # control: a direct readback of the same record is a proof
        res, cp = assess(build({"ev_b8d": readback("account_99881")}))
        self.assertEqual(citation(cp, "account_99881").authority, proof.AUTHORITATIVE)
        self.assertEqual(cp.proof_decision, "COMPLETE")

    def test_9_omission_proves_absence_only_after_the_last_state_changing_operation(self):
        sql = ("CREATE TABLE snapshot_scope (origin_system TEXT, captured_at TEXT);\n"
               f"INSERT INTO snapshot_scope VALUES ('account_service', '{CAP}');\n"
               "CREATE TABLE customer_profiles (resource_id TEXT, customer_reference TEXT, account_status TEXT, updated_at TEXT);\n"
               f"INSERT INTO customer_profiles VALUES ('account_99891', 'user_9988', 'ACTIVE', '{CAP}');\n")
        files = {"ev_b9q": (CAP, "sql", sql, "fixture_inventory"),
                 "ev_b9l": records(LATER, [{"resource_id": "account_99892", "account_status": "ACTIVE", "email": EMAIL}])}

        def act(etype, t="2026-09-12T00:00:00Z"):
            return {"activity_id": "activity_99890", "event_time": t, "event_type": etype, "source_system": "x",
                    "target_system": "account_service", "resource_id": "account_99892", "reported_result": "SUCCESS",
                    "details": ""}
        proven = [("VERIFICATION", "account_99892", "ABSENT", "ev_b9q")]
        # a deletion attempt after the inventory: its omission says nothing about the record afterwards
        res = build(files, [act("DELETE_RECORD")])[2]
        self.assertEqual((ops(res), res.trace["unresolved"]), ([], {"account_99892": "UNKNOWN_STATE"}))
        # a catalog link after the inventory changes no record: the omission still proves absence
        self.assertEqual(ops(build(files, [act("LINK_RESOURCE_IDENTITY")])[2]), proven)
        # control: the deletion attempt preceded the inventory
        self.assertEqual(ops(build(files, [act("DELETE_RECORD", "2026-09-08T00:00:00Z")])[2]), proven)


class AuthorityVersusIdentity(unittest.TestCase):
    """The same, correctly named evidence id passes or fails depending on what it can prove."""

    @classmethod
    def setUpClass(cls):
        cls.aud, cls.req, cls.res = build({
            "ev_direct": readback("account_99901"),
            "ev_late": readback("account_99901", cap=LATER),
            "ev_500": readback("account_99902", http_status=500),
            "ev_claim": lookup_claim("account_99903", "NOT_FOUND"),
            "ev_other": records(CAP, [{"resource_id": "search_99909", "total_hits": 0, "email": EMAIL}]),
            # a capture made after audit_at whose inventory is backdated to before it
            "ev_backdated": (LATER, "json", {"snapshots": [{"collection": "customer_profiles", "captured_at": CAP,
                                                            "records": [{"resource_id": "account_99908"}]}]},
                             "fixture_inventory")},
            activity=[{"activity_id": "activity_99904", "event_time": CAP, "event_type": "DELETE_RECORD",
                       "source_system": "deletion_worker", "target_system": "account_service",
                       "resource_id": "account_99901", "reported_result": "SUCCESS", "details": "deleted"}])

    def cite(self, rid, phase, state, eid):
        c = proof.cite(self.aud, rid, phase, state, eid, AUDIT_AT)
        return c.identity, c.authority

    def test_right_record_right_state(self):
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", "ev_direct"), ("RESOURCE", "AUTHORITATIVE"))

    def test_right_record_wrong_state(self):
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "REDACTED", "ev_direct"), ("RESOURCE", "STATE_MISMATCH"))
        self.assertEqual(self.cite("account_99901", "FINDING", "PRESENT", "ev_direct"), ("RESOURCE", "STATE_MISMATCH"))

    def test_right_record_captured_after_audit_at(self):
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", "ev_late"), ("RESOURCE", "INELIGIBLE"))
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", "ev_backdated"), ("SCOPE", "INELIGIBLE"))

    def test_right_record_indeterminate_readback(self):
        self.assertEqual(self.cite("account_99902", "VERIFICATION", "ABSENT", "ev_500"), ("RESOURCE", "INDETERMINATE"))

    def test_right_record_claim_only(self):
        self.assertEqual(self.cite("account_99903", "VERIFICATION", "ABSENT", "ev_claim"), ("RESOURCE", "CLAIM"))
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", "activity_99904"), ("ROW", "CLAIM"))

    def test_wrong_or_missing_identity(self):
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", "ev_other"), ("NONE", "NO_IDENTITY"))
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", "evidence_404"), ("NONE", "UNKNOWN_ID"))
        self.assertEqual(self.cite("account_99901", "VERIFICATION", "ABSENT", ""), ("NONE", "NO_CITATION"))


if __name__ == "__main__":
    unittest.main()
