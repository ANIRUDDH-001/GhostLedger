"""Output-contract validation (AGENTS.md 6.3-6.6, problem_statement.md "Rules"), in two layers.

structural  validate() checks a complete output.csv text against the requests and the corpus of
            participant-visible ids: header and row shape, one row per case in input order (the
            organizers' ruling; the supplied request files are also in numeric case_id order),
            statuses, exact step fields, consecutive step numbers, canonical order (phase, operation,
            system_id, resource_id), operation/state pairs, operation/resource kind, duplicate and
            conflicting steps, plan correspondence for INCOMPLETE rows, phase rules for COMPLETE rows,
            and citation eligibility. A timestamp that is missing or malformed fails closed: the
            citation is reported, never assumed eligible.
semantic    citation_issues() asks, for one case, whether each cited id can prove the step: the id
            names the resource (or is a capture that enumerates its system and omits it), the
            resource is linked to the case subject, the cited reading is eligible at audit_at, it is
            a direct reading rather than a reported claim, it is not superseded by a later eligible
            reading, and every physical copy read in the file supports the verified state (any copy
            supports a finding).

Plan correspondence (INCOMPLETE), grounded in the operation tables and the public sample answers:
every finding has exactly one remediation of a compatible operation on the same system/resource;
every remediation answers a finding and is followed by the verification of its target state.
Verification without remediation is allowed (retained or already-compliant records).
Structural validity says nothing about whether the evidence proves the steps; that is the semantic
layer, and neither layer can tell whether resources the pipeline did not link were omitted.
"""
import csv
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .evidence import prefix_of, system_of
from .steps import FIELDS, OP_ORDER, PHASES

OPS = {
    "FINDING": {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
                "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE"},
    "REMEDIATION": {"DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
                    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED"},
    "VERIFICATION": {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
                     "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
                     "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"},
}
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
FINDING_REMEDIATION = {"REPORT_REMAINING_RECORD": {"DELETE_RECORD"},
                       "REPORT_EXPOSED_PII": {"REDACT_RECORD", "ANONYMIZE_RECORD"},
                       "REPORT_ACTIVE_DATA_FLOW": {"DISABLE_DATA_FLOW"},
                       "REPORT_RESTORE_RISK": {"BLOCK_RESTORE"}}
REMEDIATION_VERIFICATION = {"DELETE_RECORD": "VERIFY_RECORD_ABSENT", "REDACT_RECORD": "VERIFY_RECORD_REDACTED",
                            "ANONYMIZE_RECORD": "VERIFY_RECORD_ANONYMIZED",
                            "DISABLE_DATA_FLOW": "VERIFY_DATA_FLOW_DISABLED", "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED"}
# operations that act on a process or a restore path only make sense for those resource kinds
OP_PREFIX = {"REPORT_ACTIVE_DATA_FLOW": "data_flow", "DISABLE_DATA_FLOW": "data_flow",
             "VERIFY_DATA_FLOW_DISABLED": "data_flow", "REPORT_RESTORE_RISK": "backup",
             "BLOCK_RESTORE": "backup", "VERIFY_RESTORE_BLOCKED": "backup"}
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
STR_FIELDS = ("phase", "operation", "state", "system_id", "resource_id", "evidence_id")
# reported, not failed: structurally permitted, but nothing in it proves the subject's state
WARNINGS = {"EMPTY_COMPLETE"}


def valid_ts(x):
    """An ISO-8601 UTC second that exists on the calendar (2026-02-30 matches the pattern but is not a time)."""
    if not isinstance(x, str) or not TS_RE.match(x):
        return False
    try:
        datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


class ContractError(ValueError):
    """Raised by the write-time gate; nothing is written."""


@dataclass
class Corpus:
    files: dict          # file_id -> captured_at
    rows: dict           # activity_id / audit_id -> (event_time, resource_id or None, user_id or None)
    resources: set


def corpus(ds, index):
    """Participant-visible ids. user_audit_log.csv rows are dated by event_time (its only time column)."""
    rows = {a["activity_id"]: (a.get("event_time", ""), a.get("resource_id") or None, None) for a in ds.activity}
    rows.update({a["audit_id"]: (a.get("event_time", ""), None, a.get("user_id") or None) for a in ds.audit_log})
    resources = set(index.by_resource) | {a["resource_id"] for a in ds.activity if a.get("resource_id")}
    return Corpus({f: m["captured_at"] for f, m in ds.manifest.items()}, rows, resources)


def sort_key(s):
    return (PHASES.index(s["phase"]), OP_ORDER[s["phase"]].index(s["operation"]), s["system_id"], s["resource_id"])


def _citation_time(ev, c):
    if ev in c.files:
        return c.files[ev]
    if ev in c.rows:
        return c.rows[ev][0]
    return None


def step_issues(cid, status, steps, audit_at, c):
    """Structural issues of one row's parsed steps: [(case_id, code, detail)]."""
    errs = []
    if status == "NOT_APPLICABLE":
        return [(cid, "NA_STEPS", str(len(steps)))] if steps else []
    if not steps:
        return [(cid, "EMPTY_STEPS" if status == "INCOMPLETE" else "EMPTY_COMPLETE", status)]
    good, last = [], None
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or set(s) != set(FIELDS):
            errs.append((cid, "FIELDS", str(sorted(s) if isinstance(s, dict) else s)))
            continue
        if type(s["step_no"]) is not int or s["step_no"] != i:   # True == 1 and 1.0 == 1 in Python
            errs.append((cid, "STEP_NO", f"{s['step_no']!r}!={i}"))
        if not all(isinstance(s[k], str) for k in STR_FIELDS):
            errs.append((cid, "FIELD_TYPE", f"step {i}"))
            continue
        ph, op = s["phase"], s["operation"]
        if ph not in OPS or op not in OPS[ph]:
            errs.append((cid, "OPERATION", f"{ph}/{op}"))
            continue
        if OPS[ph][op] != s["state"]:
            errs.append((cid, "STATE", f"{op}/{s['state']}"))
        good.append(s)
        pos = sort_key(s)
        if last is not None and pos < last:
            errs.append((cid, "ORDER", f"step {i} {ph}/{op}/{s['system_id']}/{s['resource_id']}"))
        last = pos
        rid, ev = s["resource_id"], s["evidence_id"]
        if rid not in c.resources:
            errs.append((cid, "FABRICATED_RESOURCE", rid))
        elif s["system_id"] != system_of(rid):
            errs.append((cid, "SYSTEM", f"{rid}->{s['system_id']}"))
        if op in OP_PREFIX and prefix_of(rid) != OP_PREFIX[op]:
            errs.append((cid, "OP_RESOURCE_KIND", f"{op} on {rid}"))
        if status == "COMPLETE" and ph != "VERIFICATION":
            errs.append((cid, "COMPLETE_PHASE", f"step {i} {ph}"))   # checked before any citation rule
            continue
        future = ph == "REMEDIATION" or (ph == "VERIFICATION" and status == "INCOMPLETE")
        if future:
            if ev != "":
                errs.append((cid, "FUTURE_CITED", f"step {i} {ev}"))
            continue
        if not ev:
            errs.append((cid, "UNCITED", f"step {i} {ph}"))
            continue
        at = _citation_time(ev, c)
        if at is None:
            errs.append((cid, "UNKNOWN_EVIDENCE", ev))
        elif not valid_ts(at) or not valid_ts(audit_at):
            errs.append((cid, "CITATION_TIME_UNKNOWN", f"{ev} {at!r}"))
        elif at > audit_at:
            errs.append((cid, "POST_AUDIT_CITATION", f"{ev} {at}"))
    seen = defaultdict(list)
    for s in good:
        seen[(s["phase"], s["system_id"], s["resource_id"])].append(s["operation"])
    for (ph, sysid, rid), ops in seen.items():
        if len(ops) != len(set(ops)):
            errs.append((cid, "DUPLICATE_STEP", f"{ph}/{rid}"))
        if len(set(ops)) > 1:
            errs.append((cid, "PLAN_CONFLICT", f"{ph}/{rid}: {sorted(set(ops))}"))
    if status == "INCOMPLETE":
        if not any(s["phase"] == "FINDING" for s in good):
            errs.append((cid, "NO_FINDING", ""))
        errs += _plan_issues(cid, seen)
    return errs


def _plan_issues(cid, seen):
    errs = []
    keys = {(sysid, rid) for _, sysid, rid in seen}
    for sysid, rid in sorted(keys):
        f, r, v = (set(seen.get((ph, sysid, rid), ())) for ph in PHASES)
        for op in f:
            if not r:
                errs.append((cid, "PLAN_NO_REMEDIATION", f"{op} {rid}"))
            elif not r & FINDING_REMEDIATION[op]:
                errs.append((cid, "PLAN_WRONG_REMEDIATION", f"{op} {rid} -> {sorted(r)}"))
        for op in r:
            if not f:
                errs.append((cid, "PLAN_UNSUPPORTED_REMEDIATION", f"{op} {rid}"))
            if REMEDIATION_VERIFICATION[op] not in v:
                errs.append((cid, "PLAN_NO_VERIFICATION", f"{op} {rid}"))
    return errs


def validate(text, requests, c):
    """[(case_id, code, detail)] for every issue in a complete output.csv text (errors and WARNINGS)."""
    errs = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header != ["case_id", "deletion_status", "steps_json"]:
        errs.append(("*", "HEADER", str(header)))
    audit = {r["case_id"]: r["audit_at"] for r in requests}
    seen, order = set(), []
    for rec in reader:
        if len(rec) != 3:
            errs.append(("*", "ROW_SHAPE", str(rec)[:80]))
            continue
        cid, status, raw = rec
        order.append(cid)
        if cid in seen:
            errs.append((cid, "DUPLICATE", ""))
        seen.add(cid)
        if cid not in audit:
            errs.append((cid, "UNKNOWN_CASE", ""))
            continue
        if status not in STATUSES:
            errs.append((cid, "STATUS", status))
            continue
        try:
            steps = json.loads(raw)
        except ValueError:
            errs.append((cid, "JSON", raw[:60]))
            continue
        if not isinstance(steps, list):
            errs.append((cid, "JSON", "not an array"))
            continue
        errs += step_issues(cid, status, steps, audit[cid], c)
    missing = set(audit) - seen
    if missing:
        errs.append(("*", "MISSING_CASES", str(len(missing))))
    expected = [r["case_id"] for r in requests if r["case_id"] in seen]
    if list(dict.fromkeys(c for c in order if c in audit)) != expected:
        errs.append(("*", "CASE_ORDER", "rows not in input request order"))
    return errs


def errors(text, requests, c):
    return [e for e in validate(text, requests, c) if e[1] not in WARNINGS]


def gate(requests, c):
    """Write-time check for pipeline.write_output: any structural error refuses the write."""
    def check(text):
        errs = errors(text, requests, c)
        if errs:
            shown = "; ".join(f"{cid} {code} {detail}" for cid, code, detail in errs[:10])
            raise ContractError(f"{len(errs)} output-contract error(s), nothing written: {shown}")
    return check


# ---------------------------------------------------------------------------------------------
# semantic citation layer

# step (phase, state) -> record states that support it
SUPPORTS = {("FINDING", "PRESENT"): {"PRESENT", "IDENTIFIABLE"}, ("FINDING", "ACTIVE"): {"ACTIVE"},
            ("VERIFICATION", "PRESENT"): {"PRESENT", "IDENTIFIABLE", "REDACTED", "ANONYMIZED"}}


def copy_role(o):
    loc = o.location.lower()
    if o.kind in ("sql_replica", "sql_table") and ("replica" in loc or "failover" in loc):
        return loc
    return "primary"


def file_reading(obs, audit_at):
    """Latest eligible stateful reading per physical copy within one file (ties: most severe)."""
    from .state import SEVERITY, eligible
    out = {}
    for o in obs:
        if not o.state or not eligible(o, audit_at):
            continue
        k = copy_role(o)
        if k not in out or (o.observed_at, SEVERITY.get(o.state, 0)) > (out[k].observed_at, SEVERITY.get(out[k].state, 0)):
            out[k] = o
    return out


def citation_issues(auditor, req, status, steps, scope_status=None, admit=frozenset({"FULL"}), links=None):
    """[(step_no, code, detail)] semantic defects of the cited steps of one case (findings, and the
    verifications of a COMPLETE case). Codes: NOT_LINKED, NO_IDENTITY, INELIGIBLE, CLAIM (verification),
    CLAIM_RESIDUE (finding; reported, gold also cites residue claims), INDETERMINATE, STATE_MISMATCH,
    STALE, OMISSION_UNADMITTED, NO_RESOURCE_STATE (an identity-history row cannot show a record state)."""
    from .ledger import _indeterminate
    from .state import eligible
    audit_at = req["audit_at"]
    if links is None:
        links = auditor.case_links(req)[1]
    acts = getattr(auditor, "_contract_rows", None)
    if acts is None:
        acts = auditor._contract_rows = ({a["activity_id"]: a for a in auditor.ds.activity},
                                         {a["audit_id"]: a for a in auditor.ds.audit_log})
    out = []
    for s in steps:
        ph = s["phase"]
        if ph == "REMEDIATION" or (ph == "VERIFICATION" and status != "COMPLETE"):
            continue
        n, rid, ev, state = s["step_no"], s["resource_id"], s["evidence_id"], s["state"]
        ok = SUPPORTS.get((ph, state), {state})
        if rid not in links:
            out.append((n, "NOT_LINKED", rid))
        if ev in acts[0]:
            a = acts[0][ev]
            if a["resource_id"] != rid:
                out.append((n, "NO_IDENTITY", f"{ev} is about {a['resource_id']}"))
            if a["event_time"] > audit_at:
                out.append((n, "INELIGIBLE", ev))
            out.append((n, "CLAIM" if ph == "VERIFICATION" else "CLAIM_RESIDUE", ev))
            continue
        if ev in acts[1]:
            out.append((n, "NO_RESOURCE_STATE", ev))
            continue
        man = auditor.ds.manifest.get(ev)
        if man is None:
            continue   # structural layer reports unknown ids
        if man["captured_at"] > audit_at:
            out.append((n, "INELIGIBLE", ev))
        in_file = [o for o in auditor.index.by_resource.get(rid, []) if o.file_id == ev]
        if not in_file:
            scopes = [x for x in auditor.states.scopes_by_system.get(system_of(rid), []) if x.file_id == ev
                      and rid not in x.resources and x.captured_at <= audit_at]
            if not scopes:
                out.append((n, "NO_IDENTITY", f"{ev} neither reads nor enumerates {rid}"))
            elif "ABSENT" not in ok:
                out.append((n, "STATE_MISMATCH", f"omission cannot show {state}"))
            elif scope_status and scope_status(max(scopes, key=lambda x: x.captured_at), audit_at, rid)[0] not in admit:
                out.append((n, "OMISSION_UNADMITTED", ev))
            continue
        reading = file_reading(in_file, audit_at)
        if not reading:
            out.append((n, "INELIGIBLE" if any(o.state for o in in_file) else "INDETERMINATE", ev))
            continue
        states = {o.state for o in reading.values()}
        if ph == "FINDING":
            if not states & ok:
                out.append((n, "STATE_MISMATCH", f"{ev} reads {sorted(states)}"))
        elif not states <= ok:
            out.append((n, "STATE_MISMATCH", f"{ev} reads {sorted(states)}"))
        if all(o.claim for o in reading.values()):
            out.append((n, "CLAIM" if ph == "VERIFICATION" else "CLAIM_RESIDUE", ev))
        if any(_indeterminate(o) for o in reading.values()):
            out.append((n, "INDETERMINATE", ev))
        t = max(o.observed_at for o in reading.values())
        later = [o for o in auditor.index.by_resource.get(rid, []) if o.file_id != ev and o.state and not o.claim
                 and eligible(o, audit_at) and o.observed_at > t]
        if ph == "VERIFICATION" and any(o.state not in ok for o in later):
            out.append((n, "STALE", f"later {sorted({(o.file_id, o.state) for o in later if o.state not in ok})[:2]}"))
        if ph == "FINDING":
            roles = {copy_role(o) for o in reading.values() if o.state in ok}
            newer = {}
            for o in later:
                k = copy_role(o)
                if k in roles and (k not in newer or o.observed_at > newer[k].observed_at):
                    newer[k] = o
            if roles and roles <= set(newer) and all(newer[k].state not in ok for k in roles):
                out.append((n, "STALE", f"every cited copy later reads compliant"))
    return out
