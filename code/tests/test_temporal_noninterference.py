"""Temporal noninterference probes (Phase 4 B): facts recorded after audit_at must not change a case.

AGENTS.md section 6.2: "Use only information available at or before audit_at when deciding the case.
Evidence created later must not change the answer for that case." Each test builds an invented
miniature dataset twice, without and with one perturbation dated after case_p's audit_at, and checks:
  strict         - Config(temporal="strict") gives byte-identical status and steps for case_p;
  retrospective  - the default reading, recorded as it behaves today (it lets each perturbation in).
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

from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, Config  # noqa: E402
from test_synthetic import REQ_COLS, DELETE_TEXT, user, write_csv  # noqa: E402

EMAIL = "ines.moravec@example.test"
UID = "user_9961"
PHONE_OLD, PHONE_NEW = "+420 601 900 610", "+420 601 900 699"
AUDIT_AT = "2026-09-15T00:00:00Z"
LATER = "2026-09-20T00:00:00Z"


def req(cid, email, requested_at, reported_at, audit_at=AUDIT_AT, status="COMPLETE"):
    return {"case_id": cid, "email": email, "requested_at": requested_at, "reported_status": status,
            "reported_at": reported_at, "audit_at": audit_at, "request_text": DELETE_TEXT}


CASE_P = req("case_p", EMAIL, "2026-09-14T20:00:00Z", "2026-09-14T23:55:00Z")


def worker(aid, t, rid, result="SUCCESS"):
    return {"activity_id": aid, "event_time": t, "event_type": "DELETE_RECORD", "source_system": "deletion_worker",
            "target_system": "account_service", "resource_id": rid, "reported_result": result,
            "details": f"Deletion worker processed {rid}."}


def records(cap, recs):
    return cap, "json", {"collection": "records", "captured_at": cap, "records": recs}


BASE_FILES = {"ev_n0": records("2026-09-10T00:00:00Z", [
    {"resource_id": "account_99610", "account_status": "DELETED", "user_id": UID}])}


def run(files=None, users=None, audit=(), activity=(), requests=(CASE_P,), case="case_p"):
    """-> {(temporal, worker_attribution): (status, steps)} for `case` (default case_p)."""
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "evidence", "records"))
        shutil.copy(os.path.join(REPO, "dataset", "deletion_rules.csv"), os.path.join(tmp, "deletion_rules.csv"))
        write_csv(os.path.join(tmp, "user_identities.csv"), ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
                  users or [user(UID, EMAIL, PHONE_OLD), user("user_9962", "jonas.berg@example.test", "+46 70 900 6200")])
        write_csv(os.path.join(tmp, "user_audit_log.csv"),
                  ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], list(audit))
        write_csv(os.path.join(tmp, "system_activity.csv"),
                  ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                   "reported_result", "details"], list(activity))
        # the worker-attribution request pool reads every *deletion_requests.csv in the dataset root
        write_csv(os.path.join(tmp, "fx_deletion_requests.csv"), REQ_COLS, list(requests))
        manifest = []
        for fid, (cap, ext, content) in (files or BASE_FILES).items():
            rel = f"evidence/records/{fid}.{ext}"
            if isinstance(content, bytes):
                with open(os.path.join(tmp, rel), "wb") as f:
                    f.write(content)
            else:
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                    f.write(content if isinstance(content, str) else json.dumps(content))
            manifest.append({"file_id": fid, "file_path": rel, "origin": "synthetic_fixture", "captured_at": cap})
        write_csv(os.path.join(tmp, "evidence_files.csv"), ["file_id", "file_path", "origin", "captured_at"], manifest)
        ds = load_dataset(tmp, "fx_deletion_requests.csv")
        out = {}
        for temporal in ("retrospective", "strict"):
            for wa in (False, True):
                r = next(x for x in Auditor(ds, Config(temporal=temporal, worker_attribution=wa)).run()
                         if x.case_id == case)
                out[(temporal, wa)] = (r.status, r.steps)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


class Noninterference(unittest.TestCase):
    def check(self, base, pert, retro_changes=((False,), (True,))):
        for wa in (False, True):
            self.assertEqual(base[("strict", wa)], pert[("strict", wa)], f"strict, attribution={wa}")
        changed = [wa for wa in (False, True) if base[("retrospective", wa)] != pert[("retrospective", wa)]]
        self.assertEqual(changed, [c[0] for c in retro_changes], "retrospective modes that let the perturbation in")

    def test_future_identity_history(self):
        # pre-audit account carries the phone number the user only adopts after audit_at
        files = dict(BASE_FILES, ev_n1=records("2026-09-10T00:00:00Z", [
            {"resource_id": "account_99611", "account_status": "ACTIVE", "phone": PHONE_NEW}]))
        base = run(files)
        pert = run(files, users=[user(UID, EMAIL, PHONE_NEW), user("user_9962", "jonas.berg@example.test", "+46 70 900 6200")],
                   audit=[{"audit_id": "audit_99611", "user_id": UID, "event_time": LATER, "field_name": "phone",
                           "old_value": PHONE_OLD, "new_value": PHONE_NEW}])
        self.assertEqual(base[("strict", False)][0], "COMPLETE")
        self.assertEqual(pert[("retrospective", False)][0], "INCOMPLETE")
        self.check(base, pert)

    def test_future_catalog_link(self):
        files = dict(BASE_FILES, ev_n2=records("2026-09-10T00:00:00Z", [
            {"resource_id": "support_99621", "message_text": "Customer asked about order 7720", "ticket_status": "OPEN"}]))
        base = run(files)
        pert = run(files, activity=[{"activity_id": "activity_99621", "event_time": LATER, "event_type": "LINK_RESOURCE_IDENTITY",
                                     "source_system": "data_catalog", "target_system": "support_platform",
                                     "resource_id": "support_99621", "reported_result": "SUCCESS",
                                     "details": f"Catalog linked support_99621 to {EMAIL}."}])
        self.check(base, pert)

    def test_future_structural_link(self):
        files = dict(BASE_FILES, ev_n3=records("2026-09-10T00:00:00Z", [
            {"resource_id": "account_99631", "account_status": "ACTIVE", "email": EMAIL},
            {"resource_id": "search_99632", "total_hits": 1}]))
        sql = ("CREATE TABLE customer_search_documents (resource_id TEXT, source_resource_id TEXT, updated_at TEXT);\n"
               f"INSERT INTO customer_search_documents VALUES ('search_99632', 'account_99631', '{LATER}');\n")
        base = run(files)
        pert = run(dict(files, ev_n3s=(LATER, "sql", sql)))
        self.check(base, pert)

    def test_future_snapshot(self):
        # the account is recreated after audit_at; the later capture is backdated in its own observed time
        base = run()
        pert = run(dict(BASE_FILES, ev_n4=(LATER, "json", {"collection": "records", "captured_at": LATER, "records": [
            {"resource_id": "account_99610", "account_status": "ACTIVE", "email": EMAIL,
             "updated_at": "2026-09-12T00:00:00Z"}]})))
        self.assertEqual(base[("strict", False)][0], "COMPLETE")
        self.check(base, pert, retro_changes=())

    def test_future_worker_row(self):
        acts = [worker("activity_99641", "2026-09-14T23:30:00Z", "account_99640")]
        files = dict(BASE_FILES, ev_n5=records("2026-09-14T23:50:00Z", [
            {"resource_id": "account_99640", "account_status": "DELETED"}]))
        base = run(files, activity=acts)
        # a second compatible row inside the band but logged after audit_at makes the match ambiguous
        pert = run(files, activity=acts + [worker("activity_99642", "2026-09-15T00:03:00Z", "account_99645")])
        self.assertIn("account_99640", {s["resource_id"] for s in base[("strict", True)][1]})
        self.check(base, pert, retro_changes=((True,),))

    def test_future_request(self):
        acts = [worker("activity_99651", "2026-09-14T23:30:00Z", "account_99650")]
        files = dict(BASE_FILES, ev_n6=records("2026-09-14T23:50:00Z", [
            {"resource_id": "account_99650", "account_status": "DELETED"}]))
        base = run(files, activity=acts)
        # another person's request, reported after case_p's audit_at, competes for the same row
        later_req = req("case_q", "jonas.berg@example.test", "2026-09-14T22:00:00Z", "2026-09-15T00:20:00Z",
                        audit_at="2026-09-16T00:00:00Z")
        pert = run(files, activity=acts, requests=(CASE_P, later_req))
        self.assertIn("account_99650", {s["resource_id"] for s in base[("strict", True)][1]})
        self.check(base, pert, retro_changes=((True,),))


if __name__ == "__main__":
    unittest.main()
