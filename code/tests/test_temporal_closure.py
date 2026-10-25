"""Phase 6 closure: temporal metamorphic probes not covered by test_temporal_noninterference.py.

Each probe adds one fact dated after case_p's audit_at (or checks a fact the time contract must keep
eligible) and records strict and retrospective behaviour with the same harness as the Phase 4 probes:
  - a historical email adopted after audit_at;
  - a log captured after audit_at whose lines are dated before it (an earlier event, learned later);
  - a structurally valid screenshot captured after audit_at;
  - evidence captured before requested_at that is still the latest valid evidence at audit_at
    (organizer clarification: no request-time lower bound);
  - a repeated deletion request by the same subject with a later audit_at.
Invented fixtures only. Run: python -m pytest code/tests -q
"""
import os
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from test_synthetic import user  # noqa: E402
import test_temporal_noninterference as tn  # noqa: E402
from test_temporal_noninterference import BASE_FILES, CASE_P, EMAIL, LATER, PHONE_OLD, UID, records, req, run  # noqa: E402

OTHER = user("user_9962", "jonas.berg@example.test", "+46 70 900 6200")
NEW_EMAIL = "ines.moravec.new@example.test"


def png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b""))


class TemporalClosure(unittest.TestCase):
    check = tn.Noninterference.check

    def test_future_historical_email(self):
        # a pre-audit record carries an address the user adopts only after audit_at
        files = dict(BASE_FILES, ev_t1=records("2026-09-10T00:00:00Z", [
            {"resource_id": "search_99711", "total_hits": 1, "email": NEW_EMAIL}]))
        base = run(files)
        pert = run(files, users=[user(UID, NEW_EMAIL, PHONE_OLD), OTHER],
                   audit=[{"audit_id": "audit_99711", "user_id": UID, "event_time": LATER, "field_name": "email",
                           "old_value": EMAIL, "new_value": NEW_EMAIL}])
        self.assertEqual(base[("strict", False)][0], "COMPLETE")
        self.assertIn("search_99711", {s["resource_id"] for s in pert[("retrospective", False)][1]})
        self.check(base, pert)

    def test_late_captured_log_of_an_earlier_event(self):
        # the record is sighted before audit_at without any identifier; a log captured after audit_at
        # (lines dated before it) is the only thing that ties it to the subject and says it is present
        files = dict(BASE_FILES, ev_t2=records("2026-09-10T00:00:00Z", [
            {"resource_id": "cache_99721", "status": "cached"}]))
        log = (f'2026-09-12T08:00:00Z level=info event=cache_lookup resource_id=cache_99721 email={EMAIL} result=hit\n')
        base = run(files)
        pert = run(dict(files, ev_t2l=(LATER, "log", log)))
        self.assertEqual(base[("strict", False)][0], "COMPLETE")
        # retrospective links it (identity from the late file) and reports it from the pre-audit sighting
        self.assertIn("cache_99721", {s["resource_id"] for s in pert[("retrospective", False)][1]})
        self.check(base, pert)

    def test_late_screenshot_is_inert_in_the_default_run(self):
        base = run()
        pert = run(dict(BASE_FILES, ev_t3=(LATER, "png", png())))
        self.check(base, pert, retro_changes=())

    def test_pre_request_evidence_stays_eligible(self):
        # captured before requested_at, still the latest valid evidence at audit_at: it decides and is cited
        early = "2026-09-01T00:00:00Z"
        for status, expect in (("DELETED", "COMPLETE"), ("ACTIVE", "INCOMPLETE")):
            files = {"ev_t4": records(early, [{"resource_id": "account_99741", "account_status": status, "user_id": UID}])}
            out = run(files)
            for key, (st, steps) in out.items():
                with self.subTest(status=status, mode=key):
                    self.assertEqual(st, expect)
                    self.assertEqual({s["evidence_id"] for s in steps if s["evidence_id"]}, {"ev_t4"})

    def test_repeated_request_does_not_change_the_earlier_case(self):
        files = dict(BASE_FILES, ev_t5=records("2026-09-18T00:00:00Z", [
            {"resource_id": "account_99610", "account_status": "ACTIVE", "user_id": UID}]))
        again = req("case_r", EMAIL, "2026-09-17T00:00:00Z", "2026-09-17T06:00:00Z", audit_at="2026-09-19T00:00:00Z")
        base = run(files)
        pert = run(files, requests=(CASE_P, again))
        for key in base:
            with self.subTest(mode=key):
                self.assertEqual(base[key], pert[key])
        # the later request sees the later recreation; the earlier one does not
        later = run(files, requests=(CASE_P, again), case="case_r")
        self.assertEqual(later[("strict", False)][0], "INCOMPLETE")
        self.assertEqual(base[("strict", False)][0], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
