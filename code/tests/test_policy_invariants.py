"""Policy safety invariants of the default PolicyEngine and evidence normalizer.

Synthetic counterexamples (no dataset case IDs): each test states a policy condition that must not
be turned into a destructive treatment. Run: python -m pytest code/tests -q
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.evidence import normalize  # noqa: E402
from ghostledger.io import read_csv  # noqa: E402
from ghostledger.policy import PolicyEngine  # noqa: E402

AT = "2026-06-01T00:00:00Z"
DESTRUCTIVE = {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"}


def engine():
    return PolicyEngine(read_csv(os.path.join(REPO, "dataset", "deletion_rules.csv")))


class PolicyInvariants(unittest.TestCase):
    def test_missing_retention_term_does_not_authorize_deletion(self):
        d = engine().evaluate("audit_record", "SECURITY_AUDIT_LOG", "PRESENT", None, AT)
        self.assertEqual((d.treatment, d.compliant, d.remediation), ("RETAIN", True, None))
        self.assertEqual(d.verification, "VERIFY_RECORD_PRESENT")

    def test_expired_retention_ends_retention_without_ordering_deletion(self):
        d = engine().evaluate("audit_record", "SECURITY_AUDIT_LOG", "PRESENT", "2026-01-01T00:00:00Z", AT)
        self.assertEqual(d.treatment, "EXPIRED")
        self.assertIsNone(d.remediation)

    def test_missing_retained_record_has_no_remediation(self):
        for prefix, cat in (("audit_record", "SECURITY_AUDIT_LOG"), ("legal_hold", "LEGAL_HOLD_RECORD")):
            d = engine().evaluate(prefix, cat, "ABSENT", None, AT)
            self.assertFalse(d.compliant)
            self.assertIsNone(d.finding)
            self.assertIsNone(d.remediation)

    def test_backup_is_retained_and_restore_blocked_never_deleted(self):
        d = engine().evaluate("backup", "BACKUP_ARCHIVE", "ACTIVE")
        self.assertEqual((d.finding, d.remediation, d.verification),
                         ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"))

    def test_archive_existence_alone_is_not_a_restore_path(self):
        state, cat = normalize("backup", "log", "", {"archive_present": "true", "event": "restore_policy_read_started"})
        self.assertEqual((state, cat), (None, "BACKUP_ARCHIVE"))

    def test_data_flow_disable_obligation_is_kept(self):
        d = engine().evaluate("data_flow", None, "ACTIVE")
        self.assertEqual((d.finding, d.remediation, d.verification),
                         ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"))
        self.assertTrue(engine().evaluate("data_flow", None, "DISABLED").compliant)

    def test_tombstone_is_redacted_not_deleted(self):
        d = engine().evaluate("tombstone", "ACCOUNT_TOMBSTONE", "IDENTIFIABLE")
        self.assertEqual((d.remediation, d.verification_state), ("REDACT_RECORD", "REDACTED"))
        self.assertTrue(engine().evaluate("tombstone", "ACCOUNT_TOMBSTONE", "REDACTED").compliant)

    def test_retained_categories_never_get_destructive_remediation(self):
        for prefix, cat in (("audit_record", "SECURITY_AUDIT_LOG"), ("legal_hold", "LEGAL_HOLD_RECORD"),
                            ("backup", "BACKUP_ARCHIVE")):
            for state in ("PRESENT", "IDENTIFIABLE", "ACTIVE", "BLOCKED", "ABSENT"):
                d = engine().evaluate(prefix, cat, state, None, AT)
                self.assertNotIn(d.remediation, DESTRUCTIVE, (cat, state))


if __name__ == "__main__":
    unittest.main()
