"""Proof model (Phase 5 B1, analysis only): coverage, compliance and citation as separate questions.

The obligation ledger (ledger.py) answers "is the required treatment in place?". That answer is not
yet a proof. This module adds the questions the output contract also depends on, and keeps each
one separate so that no single verdict can stand in for another:

  coverage    how the resource's state at audit_at is known at all
                OBSERVED       an eligible, determinate, direct readback of the record
                ENUMERATED     omitted from an eligible capture whose scope type is admitted
                CLAIMED        only reported results (handover lines, record-list rows, lookup history)
                INDETERMINATE  the readback's own status says nothing (HTTP 500, unknown restore eligibility)
                UNCOVERED      no eligible sighting, a non-admitted scope, or a catalog-only link
  compliance  the ledger's status (PROVEN_COMPLIANT, PROVEN_VIOLATION, UNRESOLVED, NOT_REQUIRED)
  proven      a PROVEN_* or NOT_REQUIRED status resting on OBSERVED or ENUMERATED coverage. A CLAIMED or
              INDETERMINATE reading is not a proof, whichever way it points
  citation    for one output step: does the cited evidence_id prove the step's state for that
              resource at audit_at? This has two parts:
                identity   RESOURCE (the file reads the record), SCOPE (the file enumerates the record's
                           system and omits it), ROW (an activity/audit row), NONE
                authority  AUTHORITATIVE, or the first defect from: NO_CITATION, UNKNOWN_ID, INELIGIBLE,
                           NO_IDENTITY, CLAIM, INDETERMINATE, STATE_MISMATCH, OMISSION_UNADMITTED

A citation with the right identity can still fail on authority. For example, a capture that omits the
record "identifies" the record's absence, but cannot prove it was REDACTED. assess() checks one
case's output against the hard invariants (no invented resource ids, no false findings, no
unsupported verification citations). It also reports hidden violations and coverage gaps. The model
is review tooling. It is not an oracle: its scope typing and claim classes are heuristics, and it
cannot see facts the parsers did not extract (screenshots, for example).
"""
from collections import Counter
from dataclasses import dataclass, field

from . import ledger
from .evidence import system_of
from .state import SEVERITY, eligible

OBSERVED, ENUMERATED, CLAIMED, INDETERMINATE, UNCOVERED = (
    "OBSERVED", "ENUMERATED", "CLAIMED", "INDETERMINATE", "UNCOVERED")
AUTHORITATIVE = "AUTHORITATIVE"
DEFECT_ORDER = ("NO_CITATION", "UNKNOWN_ID", "INELIGIBLE", "NO_IDENTITY", "CLAIM", "INDETERMINATE",
                "STATE_MISMATCH", "OMISSION_UNADMITTED")
# step state -> record states that support it (a finding needs the record still exposed; verifying a
# retained record only needs it to exist)
SUPPORTS = {
    ("FINDING", "PRESENT"): {"PRESENT", "IDENTIFIABLE"},
    ("FINDING", "ACTIVE"): {"ACTIVE"},
    ("VERIFICATION", "PRESENT"): {"PRESENT", "IDENTIFIABLE", "REDACTED", "ANONYMIZED"},
}


@dataclass
class Citation:
    identity: str
    authority: str
    defects: list = field(default_factory=list)
    reading: str = ""            # the cited file's state for the resource (ABSENT for an omission)


@dataclass
class Proof:
    resource_id: str
    coverage: str
    compliance: str
    proven: bool
    entry: ledger.Obligation


def coverage(e):
    f = e.flags
    if e.basis in ("CATALOG_ONLY", "NONE") or any(x.startswith("omission_from_") for x in f):
        return UNCOVERED
    if any(x.startswith(("indeterminate_readback", "stateless_latest")) for x in f):
        return INDETERMINATE
    if e.basis == "CLAIM":
        return CLAIMED
    return ENUMERATED if e.basis.startswith("OMISSION:") else OBSERVED


def proofs(entries):
    out = []
    for e in entries:
        cov = coverage(e)
        proven = e.status != ledger.UNRESOLVED and cov in (OBSERVED, ENUMERATED)
        out.append(Proof(e.resource_id, cov, e.status, proven, e))
    return out


def proof_decision(ps):
    """ledger.case_decision, except that a reading (compliant or not) resting on claims or
    indeterminate readbacks is UNRESOLVED instead of proven."""
    violations = [p for p in ps if p.proven and p.compliance == ledger.PROVEN_VIOLATION]
    if any("unreportable_violation" not in p.entry.flags for p in violations):
        return "INCOMPLETE"
    if violations:
        return "VIOLATION_UNREPORTABLE"
    if any(not p.proven and p.entry.basis != "CATALOG_ONLY" for p in ps):
        return "UNRESOLVED"
    if not any(p.entry.basis != "CATALOG_ONLY" for p in ps):
        return "UNRESOLVED"      # nothing linked: an empty COMPLETE proves nothing about the subject
    return "COMPLETE"


def cite(auditor, rid, phase, state, evidence_id, audit_at, scope_status=None, admit=frozenset({ledger.FULL})):
    """Identity and authority of `evidence_id` as proof that `rid` is in `state` at audit_at."""
    defects = []
    if not evidence_id:
        return Citation("NONE", "NO_CITATION", ["NO_CITATION"])
    man = auditor.ds.manifest.get(evidence_id)
    if man is None:
        rows = {a["activity_id"] for a in auditor.ds.activity} | {a["audit_id"] for a in auditor.ds.audit_log}
        if evidence_id in rows:  # workflow and audit rows are claims (problem statement, evidence rules)
            return Citation("ROW", "CLAIM", ["CLAIM"])
        return Citation("NONE", "UNKNOWN_ID", ["UNKNOWN_ID"])
    in_file = [o for o in auditor.index.by_resource.get(rid, []) if o.file_id == evidence_id]
    scopes = [s for s in auditor.states.scopes_by_system.get(system_of(rid), []) if s.file_id == evidence_id
              and rid not in s.resources]
    identity = "RESOURCE" if in_file else "SCOPE" if scopes else "NONE"
    ok = SUPPORTS.get((phase, state), {state})
    if man["captured_at"] > audit_at:
        defects.append("INELIGIBLE")
    if identity == "NONE":
        defects.append("NO_IDENTITY")
        return Citation(identity, defects[0], defects)
    if identity == "RESOURCE":
        obs = [o for o in in_file if o.state and eligible(o, audit_at)]
        if not obs:
            defects.append("INELIGIBLE" if any(o.state for o in in_file) else "INDETERMINATE")
            return Citation(identity, _first(defects), sorted(set(defects), key=DEFECT_ORDER.index))
        reading = max(obs, key=lambda o: (SEVERITY.get(o.state, 0), o.observed_at))
        if all(o.claim for o in obs):
            defects.append("CLAIM")
        if any(ledger._indeterminate(o) for o in obs):
            defects.append("INDETERMINATE")
        if reading.state not in ok:
            defects.append("STATE_MISMATCH")
        r = reading.state
    else:
        elig = [s for s in scopes if s.captured_at <= audit_at]
        if not elig:
            defects.append("INELIGIBLE")
        if "ABSENT" not in ok:
            defects.append("STATE_MISMATCH")
        if scope_status and elig:
            s = max(elig, key=lambda s: s.captured_at)
            if scope_status(s, audit_at, rid)[0] not in admit:
                defects.append("OMISSION_UNADMITTED")
        r = "ABSENT"
    defects = sorted(set(defects), key=DEFECT_ORDER.index)
    return Citation(identity, _first(defects), defects, r)


def _first(defects):
    return defects[0] if defects else AUTHORITATIVE


@dataclass
class CaseProof:
    case_id: str
    output_status: str
    ledger_decision: str
    proof_decision: str
    coverage: Counter
    citations: list                   # (step_no, phase, resource_id, state, evidence_id, Citation)
    invented_resources: list
    false_findings: list
    unsupported_verifications: list
    hidden_violations: list           # proven violations the output does not report
    hidden_claimed: list              # violations read from claims only, not reported
    gaps: list                        # resources not proven compliant in a COMPLETE output
    proofs: list


def assess(auditor, req, result, scope_status=None, admit=frozenset({ledger.FULL}), replicas=True):
    """Check one case's output against the obligations the evidence supports."""
    entries = ledger.obligations(auditor, req, scope_status, replicas=replicas, admit=admit)
    ps = proofs(entries)
    by_rid = {p.resource_id: p for p in ps}
    audit_at = req["audit_at"]
    cits, invented, false_f, unsupported = [], [], [], []
    for s in result.steps:
        rid = s["resource_id"]
        if rid not in by_rid:
            invented.append(rid)
        if s["phase"] == "REMEDIATION" or (s["phase"] == "VERIFICATION" and result.status == "INCOMPLETE"):
            continue          # future work: no evidence is cited, by contract
        c = cite(auditor, rid, s["phase"], s["state"], s["evidence_id"], audit_at, scope_status, admit)
        cits.append((s["step_no"], s["phase"], rid, s["state"], s["evidence_id"], c))
        if s["phase"] == "FINDING" and (c.authority != AUTHORITATIVE or by_rid.get(rid) is None
                                        or by_rid[rid].compliance != ledger.PROVEN_VIOLATION):
            false_f.append(rid)
        if s["phase"] == "VERIFICATION" and c.authority != AUTHORITATIVE:
            unsupported.append(rid)
    findings = {s["resource_id"] for s in result.steps if s["phase"] == "FINDING"}
    hidden = [p.resource_id for p in ps if p.compliance == ledger.PROVEN_VIOLATION and p.resource_id not in findings
              and p.proven]
    hidden_claimed = [p.resource_id for p in ps if p.compliance == ledger.PROVEN_VIOLATION
                      and p.resource_id not in findings and not p.proven]
    gaps = [p.resource_id for p in ps if result.status == "COMPLETE" and not (
        p.proven and p.compliance in (ledger.PROVEN_COMPLIANT, ledger.NOT_REQUIRED))]
    return CaseProof(req["case_id"], result.status, ledger.case_decision(entries), proof_decision(ps),
                     Counter(p.coverage for p in ps), cits, invented, false_f, unsupported, hidden, hidden_claimed, gaps, ps)
