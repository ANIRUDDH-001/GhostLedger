"""Phase 6 closure: output-contract defects reproduced and fixed in ghostledger/contract.py.

Structural tests use an invented corpus (no dataset ids); each mutation targets one failure
mechanism and has a valid positive control. Semantic tests build miniature evidence sets with the
proof-model fixture builder and check that a correctly named id still fails when it cannot prove
the step (wrong resource, subject, copy, state, time, scope, or a claim instead of a readback).
Run: python -m pytest code/tests -q
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from ghostledger import contract, ledger  # noqa: E402
from ghostledger.evidence import system_of  # noqa: E402
from test_proof_model import CAP, EMAIL, LATER, UID, build, lookup_claim, readback, records, snapshot  # noqa: E402

AUDIT_AT = "2026-09-15T00:00:00Z"
C = contract.Corpus(
    files={"ev_a": "2026-09-01T00:00:00Z", "ev_late": "2026-10-01T00:00:00Z"},
    rows={"audit_in": ("2026-09-01T00:00:00Z", None, "user_1"), "audit_late": ("2026-10-01T00:00:00Z", None, "user_1"),
          "audit_blank": ("", None, "user_1"), "audit_bad": ("2026-09-01 10:00", None, "user_1")},
    resources={"account_1", "account_2", "search_1", "data_flow_1", "backup_1"})
REQ = [{"case_id": "case_1", "audit_at": AUDIT_AT}]


def st(phase, rid, op, ev="", state=None):
    return {"phase": phase, "system_id": system_of(rid), "resource_id": rid, "operation": op,
            "state": state or contract.OPS[phase][op], "evidence_id": ev}


def numbered(steps, sort=True):
    steps = sorted(steps, key=contract.sort_key) if sort else steps
    return [dict(s, step_no=i + 1) for i, s in enumerate(steps)]


def codes(status, steps, sort=True):
    return {e[1] for e in contract.step_issues("case_1", status, numbered(steps, sort), AUDIT_AT, C)}


PLAN = [st("FINDING", "account_1", "REPORT_REMAINING_RECORD", "ev_a"),
        st("FINDING", "data_flow_1", "REPORT_ACTIVE_DATA_FLOW", "ev_a"),
        st("REMEDIATION", "data_flow_1", "DISABLE_DATA_FLOW"), st("REMEDIATION", "account_1", "DELETE_RECORD"),
        st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT"),
        st("VERIFICATION", "data_flow_1", "VERIFY_DATA_FLOW_DISABLED"),
        st("VERIFICATION", "account_2", "VERIFY_RECORD_PRESENT")]   # verification without remediation is allowed


def without(steps, phase, rid):
    return [s for s in steps if not (s["phase"] == phase and s["resource_id"] == rid)]


def replace(steps, phase, rid, **kw):
    return [dict(s, **kw) if s["phase"] == phase and s["resource_id"] == rid else s for s in steps]


class StructuralClosure(unittest.TestCase):
    def test_positive_controls(self):
        self.assertEqual(codes("INCOMPLETE", PLAN), set())
        self.assertEqual(codes("COMPLETE", [st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", "ev_a")]), set())
        self.assertEqual(codes("NOT_APPLICABLE", []), set())

    def test_audit_row_times_use_event_time_and_fail_closed(self):
        def v(ev):
            return codes("COMPLETE", [st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", ev)])
        self.assertEqual(v("audit_in"), set())
        self.assertEqual(v("audit_late"), {"POST_AUDIT_CITATION"})
        self.assertEqual(v("audit_blank"), {"CITATION_TIME_UNKNOWN"})
        self.assertEqual(v("audit_bad"), {"CITATION_TIME_UNKNOWN"})
        self.assertEqual(v("audit_unknown"), {"UNKNOWN_EVIDENCE"})
        self.assertEqual(v("ev_late"), {"POST_AUDIT_CITATION"})

    def test_audit_corpus_reads_event_time(self):
        class DS:
            manifest, activity = {}, []
            audit_log = [{"audit_id": "audit_x", "user_id": "user_1", "event_time": "2026-01-02T03:04:05Z",
                          "field_name": "email", "old_value": "", "new_value": ""}]

        class Index:
            by_resource = {}
        self.assertEqual(contract.corpus(DS, Index).rows["audit_x"][0], "2026-01-02T03:04:05Z")

    def test_complete_rejects_remediation_and_findings(self):
        ok = st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", "ev_a")
        self.assertIn("COMPLETE_PHASE", codes("COMPLETE", [st("REMEDIATION", "account_1", "DELETE_RECORD"), ok]))
        self.assertIn("COMPLETE_PHASE", codes("COMPLETE", [st("FINDING", "account_1", "REPORT_REMAINING_RECORD", "ev_a"), ok]))

    def test_full_canonical_order(self):
        a = st("FINDING", "account_1", "REPORT_REMAINING_RECORD", "ev_a")
        b = st("FINDING", "account_2", "REPORT_REMAINING_RECORD", "ev_a")
        s = st("FINDING", "search_1", "REPORT_REMAINING_RECORD", "ev_a")
        rem = [st("REMEDIATION", r, "DELETE_RECORD") for r in ("account_1", "account_2", "search_1")]
        ver = [st("VERIFICATION", r, "VERIFY_RECORD_ABSENT") for r in ("account_1", "account_2", "search_1")]
        self.assertEqual(codes("INCOMPLETE", [a, b, s] + rem + ver, sort=False), set())
        self.assertIn("ORDER", codes("INCOMPLETE", [b, a, s] + rem + ver, sort=False))     # resource_id
        self.assertIn("ORDER", codes("INCOMPLETE", [s, a, b] + rem + ver, sort=False))     # system_id
        dep = [PLAN[0], PLAN[1], PLAN[3], PLAN[2]] + PLAN[4:]                              # delete before disable
        self.assertIn("ORDER", codes("INCOMPLETE", dep, sort=False))

    def test_plan_completeness_mutations(self):
        cases = {
            "PLAN_NO_REMEDIATION": without(PLAN, "REMEDIATION", "account_1"),
            "PLAN_NO_VERIFICATION": without(PLAN, "VERIFICATION", "account_1"),
            "PLAN_WRONG_REMEDIATION": replace(replace(PLAN, "REMEDIATION", "account_1", operation="REDACT_RECORD",
                                                      state="REDACTED"), "VERIFICATION", "account_1",
                                              operation="VERIFY_RECORD_REDACTED", state="REDACTED"),
            "STATE": replace(PLAN, "REMEDIATION", "account_1", state="REDACTED"),
            "PLAN_UNSUPPORTED_REMEDIATION": PLAN + [st("REMEDIATION", "search_1", "DELETE_RECORD"),
                                                    st("VERIFICATION", "search_1", "VERIFY_RECORD_ABSENT")],
            "DUPLICATE_STEP": PLAN + [st("REMEDIATION", "account_1", "DELETE_RECORD")],
            "OP_RESOURCE_KIND": replace(PLAN, "FINDING", "data_flow_1", resource_id="backup_1",
                                        system_id=system_of("backup_1")),
            "PLAN_CONFLICT": PLAN + [st("REMEDIATION", "account_1", "REDACT_RECORD")],
            "NO_FINDING": [s for s in PLAN if s["phase"] != "FINDING"],
        }
        flowless = without(without(PLAN, "REMEDIATION", "data_flow_1"), "VERIFICATION", "data_flow_1")
        cases["PLAN_NO_REMEDIATION (flow shutdown)"] = flowless
        moved = replace(PLAN, "REMEDIATION", "account_1", resource_id="account_2")         # wrong resource
        for code, steps in cases.items():
            with self.subTest(code):
                self.assertIn(code.split()[0], codes("INCOMPLETE", steps))
        got = codes("INCOMPLETE", moved)
        self.assertTrue({"PLAN_NO_REMEDIATION", "PLAN_UNSUPPORTED_REMEDIATION"} <= got, got)

    def test_empty_complete_is_a_warning_not_an_error(self):
        self.assertEqual(codes("COMPLETE", []), {"EMPTY_COMPLETE"})
        self.assertIn("EMPTY_COMPLETE", contract.WARNINGS)
        self.assertEqual(codes("INCOMPLETE", []), {"EMPTY_STEPS"})


class Phase7ValidatorGaps(unittest.TestCase):
    """Gaps left open by the Phase 6 closure: JSON types that compare equal to valid values, values
    the checks could not even hash, impossible calendar dates, and row order against the input file."""
    OK = st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", "ev_a")

    def test_step_no_must_be_an_integer(self):
        for bad in (True, 1.0, "1"):
            with self.subTest(bad):
                steps = [dict(self.OK, step_no=bad)]
                got = {e[1] for e in contract.step_issues("case_1", "COMPLETE", steps, AUDIT_AT, C)}
                self.assertIn("STEP_NO", got)

    def test_list_valued_fields_are_reported_not_raised(self):
        for k in ("phase", "operation", "state", "system_id", "resource_id", "evidence_id"):
            with self.subTest(k):
                steps = [dict(self.OK, step_no=1, **{k: [self.OK[k]]})]
                got = {e[1] for e in contract.step_issues("case_1", "COMPLETE", steps, AUDIT_AT, C)}
                self.assertIn("FIELD_TYPE", got)

    def test_impossible_calendar_dates_fail_closed(self):
        c = contract.Corpus(dict(C.files, ev_feb30="2026-02-30T00:00:00Z"), C.rows, C.resources)
        steps = [dict(st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", "ev_feb30"), step_no=1)]
        got = {e[1] for e in contract.step_issues("case_1", "COMPLETE", steps, AUDIT_AT, c)}
        self.assertEqual(got, {"CITATION_TIME_UNKNOWN"})

    def test_rows_follow_input_order(self):
        reqs = [{"case_id": "case_2", "audit_at": AUDIT_AT}, {"case_id": "case_1", "audit_at": AUDIT_AT}]
        header = "case_id,deletion_status,steps_json\n"
        good = header + "case_2,NOT_APPLICABLE,[]\ncase_1,NOT_APPLICABLE,[]\n"
        bad = header + "case_1,NOT_APPLICABLE,[]\ncase_2,NOT_APPLICABLE,[]\n"
        self.assertEqual(contract.errors(good, reqs, C), [])
        self.assertIn("CASE_ORDER", {e[1] for e in contract.errors(bad, reqs, C)})

    def test_writer_gate_refuses_contract_errors_and_keeps_previous_file(self):
        import tempfile
        from ghostledger.pipeline import CaseResult, write_output
        reqs = [{"case_id": "case_1", "audit_at": AUDIT_AT}]
        gate = contract.gate(reqs, C)
        cited_late = [dict(st("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", "ev_late"), step_no=1)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "output.csv")
            with open(p, "w", encoding="utf-8") as f:
                f.write("previous\n")
            with self.assertRaises(contract.ContractError) as e:
                write_output([CaseResult("case_1", "COMPLETE", cited_late)], p, gate=gate)
            self.assertIn("POST_AUDIT_CITATION", str(e.exception))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "previous\n")
            write_output([CaseResult("case_1", "COMPLETE", [dict(self.OK, step_no=1)])], p, gate=gate)
            with open(p, encoding="utf-8") as f:
                self.assertIn("ev_a", f.read())

    def test_writer_uses_input_order_when_given(self):
        from ghostledger.pipeline import CaseResult, serialize_output
        rs = [CaseResult("case_1", "NOT_APPLICABLE", []), CaseResult("case_2", "NOT_APPLICABLE", [])]
        text = serialize_output(rs, order=["case_2", "case_1"])
        self.assertEqual([l.split(",")[0] for l in text.splitlines()[1:]], ["case_2", "case_1"])


class SemanticClosure(unittest.TestCase):
    """Each negative has the same shape as a positive control; only what the cited id proves differs."""

    @classmethod
    def setUpClass(cls):
        def sql(primary, failover):
            cols = "(resource_id TEXT, user_id TEXT, email TEXT, account_status TEXT, updated_at TEXT)"
            return (f"CREATE TABLE customer_profiles_primary {cols};\n"
                    f"INSERT INTO customer_profiles_primary VALUES ('account_70030', '{UID}', '{EMAIL}', '{primary}', '{CAP}');\n"
                    f"CREATE TABLE customer_profiles_failover_replica {cols};\n"
                    f"INSERT INTO customer_profiles_failover_replica VALUES ('account_70030', '{UID}', '{EMAIL}', "
                    f"'{failover}', '{CAP}');\n")
        inv = "2026-09-12T00:00:00Z"     # after every other live sighting, so the inventory is not contradicted
        full_scope = ("CREATE TABLE snapshot_scope (origin_system TEXT, captured_at TEXT);\n"
                      f"INSERT INTO snapshot_scope VALUES ('account_service', '{inv}');\n"
                      "CREATE TABLE customer_profiles (resource_id TEXT, customer_reference TEXT, account_status TEXT, updated_at TEXT);\n"
                      f"INSERT INTO customer_profiles VALUES ('account_70099', 'user_70099', 'ACTIVE', '{inv}');\n")
        early = "2026-09-01T00:00:00Z"
        cls.aud, cls.req, _ = build({
            "ev_absent": readback("account_70001"),
            "ev_present": readback("account_70002", http_status=200, rows=1),
            "ev_other": records(CAP, [{"resource_id": "account_70003", "http_status": 200, "rows": 1,
                                       "email": "someone.else@example.test"}]),
            "ev_drift": (CAP, "sql", sql("DELETED", "ACTIVE"), "fixture_db"),
            "ev_clean": (CAP, "sql", sql("DELETED", "DELETED"), "fixture_db"),
            "ev_old404": readback("account_70040", cap=early),
            "ev_new200": readback("account_70040", http_status=200, rows=1),
            "ev_seen": readback("account_70050", http_status=200, rows=1, cap="2026-08-01T00:00:00Z"),
            "ev_unknown_scope": snapshot("2026-09-11T00:00:00Z", "customer_profiles", [{"resource_id": "account_70098", "account_status": "ACTIVE"}]),
            "ev_full_scope": (inv, "sql", full_scope, "fixture_inventory"),
            "ev_claim": lookup_claim("account_70060", "NOT_FOUND"),
            "ev_after": readback("account_70070", cap=LATER),
            "ev_seen70": readback("account_70070", http_status=200, rows=1),
        }, activity=[{"activity_id": "activity_9001", "event_time": CAP, "event_type": "DELETE_RECORD",
                      "source_system": "deletion_worker", "target_system": "account_service",
                      "resource_id": "account_70099", "reported_result": "SUCCESS", "details": ""}])
        cls.aud.ds.audit_log.append({"audit_id": "audit_9001", "user_id": UID, "event_time": CAP,
                                     "field_name": "email", "old_value": "", "new_value": EMAIL})
        cls.typer = ledger.ScopeTyper(cls.aud)

    def check(self, status, step, typed=False):
        s = dict(step, step_no=1)
        return {c for _, c, _ in contract.citation_issues(self.aud, self.req, status, [s],
                                                          self.typer.status if typed else None)}

    def v(self, rid, ev, op="VERIFY_RECORD_ABSENT", typed=False):
        return self.check("COMPLETE", st("VERIFICATION", rid, op, ev), typed)

    def f(self, rid, ev):
        return self.check("INCOMPLETE", st("FINDING", rid, "REPORT_REMAINING_RECORD", ev))

    def test_positive_controls(self):
        self.assertEqual(self.v("account_70001", "ev_absent"), set())
        self.assertEqual(self.f("account_70002", "ev_present"), set())
        self.assertEqual(self.v("account_70030", "ev_clean"), set())
        self.assertEqual(self.f("account_70030", "ev_drift"), set())          # any live copy supports a finding
        self.assertEqual(self.v("account_70050", "ev_full_scope", typed=True), set())
        self.assertEqual(self.f("account_70040", "ev_new200"), set())

    def test_wrong_resource_and_unrelated_content(self):
        self.assertIn("NO_IDENTITY", self.v("account_70001", "ev_present"))
        self.assertIn("NO_IDENTITY", self.v("account_70001", "activity_9001"))

    def test_wrong_subject(self):
        self.assertIn("NOT_LINKED", self.f("account_70003", "ev_other"))

    def test_wrong_physical_copy(self):
        self.assertIn("STATE_MISMATCH", self.v("account_70030", "ev_drift"))

    def test_wrong_state(self):
        self.assertIn("STATE_MISMATCH", self.f("account_70001", "ev_absent"))
        self.assertIn("STATE_MISMATCH", self.v("account_70001", "ev_absent", op="VERIFY_RECORD_REDACTED"))

    def test_wrong_observation_time(self):
        self.assertIn("STALE", self.v("account_70040", "ev_old404"))
        self.assertIn("INELIGIBLE", self.v("account_70070", "ev_after"))

    def test_wrong_source_scope(self):
        self.assertIn("OMISSION_UNADMITTED", self.v("account_70050", "ev_unknown_scope", typed=True))

    def test_claim_instead_of_direct_proof(self):
        self.assertIn("CLAIM", self.v("account_70060", "ev_claim"))
        self.assertIn("CLAIM", self.v("account_70099", "activity_9001"))

    def test_identity_history_row_proves_no_record_state(self):
        self.assertIn("NO_RESOURCE_STATE", self.v("account_70001", "audit_9001"))


if __name__ == "__main__":
    unittest.main()
