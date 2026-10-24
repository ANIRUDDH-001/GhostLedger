"""Step generator: canonical ordering and serialization of steps_json."""
import json

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OP_ORDER = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"],
    # flows and restore paths are cut before dependent records are changed
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"],
}
FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def sort_key(step):
    ph = step["phase"]
    return (PHASES.index(ph), OP_ORDER[ph].index(step["operation"]), step["system_id"], step["resource_id"])


def finalize(steps):
    out = sorted(steps, key=sort_key)
    return [dict(zip(FIELDS, [i + 1] + [s[k] for k in FIELDS[1:]])) for i, s in enumerate(out)]


def to_json(steps):
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


def step(phase, system_id, resource_id, operation, state, evidence_id=""):
    return {"phase": phase, "system_id": system_id, "resource_id": resource_id,
            "operation": operation, "state": state, "evidence_id": evidence_id}
