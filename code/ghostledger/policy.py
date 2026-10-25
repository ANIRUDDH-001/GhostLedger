"""Policy evaluator: map a resource's category and state at audit_at to a required treatment.

Each of the 15 rules in deletion_rules.csv is a (data_category, condition, required_action).
evaluate() returns a Decision: the rule applied, whether the resource is compliant now, the
finding to report if not, the remediation and the verification that must hold afterwards.
"""
from dataclasses import dataclass

# record prefix -> default data category when the evidence does not name one
PREFIX_CATEGORY = {
    "account": "USER_PROFILE", "search": "SEARCH_INDEX_ENTRY", "cache": "CACHE_ENTRY",
    "analytics": "ANALYTICS_PROFILE", "support": "SUPPORT_MESSAGE", "export": "USER_EXPORT",
    "marketing": "MARKETING_PROFILE", "email_delivery": "EMAIL_DELIVERY_LOG",
    "audit_record": "SECURITY_AUDIT_LOG", "legal_hold": "LEGAL_HOLD_RECORD", "backup": "BACKUP_ARCHIVE",
    "tombstone": "ACCOUNT_TOMBSTONE", "data_flow": "DATA_FLOW", "object": "SUPPORT_ATTACHMENT",
}

# treatment -> (compliant states, finding op, remediation op, verification op, target state)
TREATMENT = {
    "DELETE":    ({"ABSENT"}, "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT", "ABSENT"),
    "REDACT":    ({"REDACTED"}, "REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED", "REDACTED"),
    "ANONYMIZE": ({"ANONYMIZED"}, "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    "RETAIN":    ({"PRESENT", "IDENTIFIABLE", "REDACTED", "ANONYMIZED"}, None, None, "VERIFY_RECORD_PRESENT", "PRESENT"),
    "DISABLE":   ({"DISABLED", "ABSENT"}, "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    "BLOCK":     ({"BLOCKED", "ABSENT"}, "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
}
# treatments that keep the record: policy requires it to remain, minus its identifiers / its link to the user
PRESERVING = {"REDACT", "ANONYMIZE"}
FINDING_STATE = {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
                 "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE"}
REMEDIATION_STATE = {"DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
                     "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED"}


@dataclass
class Decision:
    rule_id: str
    treatment: str
    compliant: bool
    finding: str = None
    remediation: str = None
    verification: str = None
    verification_state: str = None
    note: str = ""


class PolicyEngine:
    def __init__(self, rules):
        self.rules = {r["data_category"]: r for r in rules}

    def evaluate(self, prefix, category, state, retention_until=None, audit_at=None):
        cat = category or PREFIX_CATEGORY.get(prefix)
        rule = self.rules.get(cat)
        if cat == "DATA_FLOW":
            # Not a stored record: an enabled flow can copy or recreate the subject's data.
            return self._decide("DATA_FLOW", "DISABLE", state)
        if rule is None:
            return None
        action = rule["required_action"]
        if action == "RETAIN" and cat == "SECURITY_AUDIT_LOG" and retention_until and audit_at and retention_until < audit_at:
            return Decision(rule["rule_id"], "EXPIRED", True, note="retention period ended; no retained-record check")
        if action == "RETAIN" and cat == "BACKUP_ARCHIVE":
            # rule_14: keep the archive, but no restore path may bring the subject's data back
            return self._decide(rule["rule_id"], "BLOCK", state)
        if action == "RETAIN" and state == "ABSENT":
            return Decision(rule["rule_id"], "RETAIN", False, note="retained record missing; no allowed remediation")
        if action in PRESERVING and state == "ABSENT":
            # the record must remain (redacted / anonymized); a missing one is not a treated one
            return Decision(rule["rule_id"], action, False, note="record policy preserves is missing; no allowed remediation")
        return self._decide(rule["rule_id"], action if action in TREATMENT else "DELETE", state)

    @staticmethod
    def omission_establishes(treatment):
        """Whether omission from an enumerating capture can establish `treatment`'s required state.
        An omission shows only that no copy is listed, which entails ABSENT: it cannot show that a
        kept record had its identifiers removed (REDACTED) or its link cut (ANONYMIZED), nor read a
        flow as DISABLED or a backup as BLOCKED (and rule_14 keeps the archive)."""
        return treatment in TREATMENT and TREATMENT[treatment][4] == "ABSENT"

    @staticmethod
    def _decide(rule_id, treatment, state):
        ok_states, finding, remediation, verification, vstate = TREATMENT[treatment]
        compliant = state in ok_states
        return Decision(rule_id, treatment, compliant, None if compliant else finding,
                        None if compliant else remediation, verification, vstate)
