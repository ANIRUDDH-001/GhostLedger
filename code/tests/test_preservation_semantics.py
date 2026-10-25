"""REDACT / ANONYMIZE preserve the record (Phase 7 final A1) on invented miniature datasets.

The contract: REDACT keeps the record minus the identifying fields policy names; ANONYMIZE keeps it
after irreversibly unlinking it. A record that a capture merely does not list has been shown to be
missing from that listing, not to have been treated, so an omission never verifies those states, and
a record policy keeps is never declared preserved when it is missing. DELETE absence proofs stay.
The same rule (policy.omission_establishes / policy.PRESERVING) governs the output and the ledger.
Run: python -m pytest code/tests -q
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from ghostledger import ledger  # noqa: E402
from test_proof_integrity import EMAIL, UID, build, ops, records  # noqa: E402

CAP = "2026-09-10T00:00:00Z"
LATER = "2026-09-20T00:00:00Z"      # after audit_at: links the ids to the subject, proves no state


def inventory(coll, other):
    """An eligible enumerating capture of `coll` that lists only another record."""
    return CAP, {"snapshots": [{"collection": coll, "captured_at": CAP, "records": [{"resource_id": other}]}]}


class PreservationSemantics(unittest.TestCase):
    def test_1_omission_does_not_prove_redacted(self):
        aud, r, led = build({
            "ev_r1s": inventory("support_records", "support_99601"),
            "ev_r1l": records(LATER, [{"resource_id": "support_99602", "requester_email": EMAIL,
                                       "message_text": "please call me"}])})
        self.assertNotIn("VERIFY_RECORD_REDACTED", [s["operation"] for s in r.steps])
        self.assertEqual(r.trace["unresolved"]["support_99602"], "OMISSION_DOES_NOT_ESTABLISH_REDACTED")
        self.assertEqual(led["support_99602"].status, ledger.UNRESOLVED)

    def test_2_omission_does_not_prove_anonymized(self):
        aud, r, led = build({
            "ev_r2s": inventory("customer_analytics_reports", "analytics_99611"),
            "ev_r2l": records(LATER, [{"resource_id": "analytics_99612", "email": EMAIL}])})
        self.assertNotIn("VERIFY_RECORD_ANONYMIZED", [s["operation"] for s in r.steps])
        self.assertEqual(r.trace["unresolved"]["analytics_99612"], "OMISSION_DOES_NOT_ESTABLISH_ANONYMIZED")
        self.assertEqual(led["analytics_99612"].status, ledger.UNRESOLVED)

    def test_3_verified_redacted_record_satisfies_redact(self):
        aud, r, led = build({"ev_r3": records(CAP, [
            {"resource_id": "support_99621", "user_id": UID, "message_text": "[REDACTED]"}])})
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "support_99621", "VERIFY_RECORD_REDACTED", "REDACTED", "ev_r3")])
        self.assertEqual(led["support_99621"].status, ledger.PROVEN_COMPLIANT)

    def test_4_verified_anonymized_record_satisfies_anonymize(self):
        aud, r, led = build({"ev_r4": records(CAP, [
            {"resource_id": "analytics_99631", "user_id": UID, "direct_identifiers": 0}])})
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "analytics_99631", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", "ev_r4")])
        self.assertEqual(led["analytics_99631"].status, ledger.PROVEN_COMPLIANT)

    def test_5_missing_preserved_record_is_not_declared_preserved(self):
        # Policy level: no parsed source currently yields a direct ABSENT reading of a record policy
        # keeps (a NOT_FOUND lookup is a claim and cannot close an obligation), so the rule is pinned
        # on the engine the pipeline and the ledger share.
        aud, r, led = build({})
        for prefix, cat, treatment in (("support", "SUPPORT_MESSAGE", "REDACT"),
                                       ("analytics", "ANALYTICS_PROFILE", "ANONYMIZE")):
            d = aud.policy.evaluate(prefix, cat, "ABSENT")
            self.assertEqual(d.treatment, treatment)
            self.assertFalse(d.compliant)                # missing is not preserved
            self.assertIsNone(d.verification)            # no VERIFY_RECORD_REDACTED / _ANONYMIZED
            self.assertIsNone(d.finding)                 # and no invented finding or remediation
            self.assertIsNone(d.remediation)
            self.assertFalse(aud.policy.omission_establishes(treatment))
        self.assertTrue(aud.policy.omission_establishes("DELETE"))

    def test_6_delete_absence_proof_remains_valid(self):
        aud, r, led = build({
            "ev_r6s": inventory("customer_search_documents", "search_99651"),
            "ev_r6l": records(LATER, [{"resource_id": "search_99652", "email": EMAIL, "total_hits": 1}])})
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(ops(r), [("VERIFICATION", "search_99652", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_r6s")])
        self.assertEqual(led["search_99652"].status, ledger.PROVEN_COMPLIANT)


if __name__ == "__main__":
    unittest.main()
