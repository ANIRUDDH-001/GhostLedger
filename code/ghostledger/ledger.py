"""Obligation ledger (Phase 4, analysis only): what each linked resource proves for one case.

The ledger never changes output. It re-derives, for every resource the identity graph links to the
subject (including resources the pipeline later drops), the policy obligation and whether the
evidence available at audit_at proves it:

  PROVEN_COMPLIANT  the required treatment is established by an eligible observation
  PROVEN_VIOLATION  an eligible observation shows the required treatment is not in place
  UNRESOLVED        the evidence cannot establish the state the obligation needs
  NOT_REQUIRED      the rule's condition is shown not to apply (e.g. retention period ended)

Each entry carries the rule, its condition text, the basis of the state (direct readback, reported
claim, omission from an enumerating capture, none), the cited evidence and its time, and a reason
code. case_decision() turns entries into a case-level reading; the output enum has no UNKNOWN, so an
UNRESOLVED case is reported as such here and is not mapped to an output status.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from .evidence import prefix_of, system_of
from .policy import PREFIX_CATEGORY
from .state import SEVERITY, eligible

PROVEN_COMPLIANT, PROVEN_VIOLATION, UNRESOLVED, NOT_REQUIRED = (
    "PROVEN_COMPLIANT", "PROVEN_VIOLATION", "UNRESOLVED", "NOT_REQUIRED")
RETAIN_CATEGORIES = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}
# verification op target state for each treatment (policy.TREATMENT); ABSENT satisfies these
# treatments in policy.py, but then the cited evidence shows ABSENT, not the verified state
NON_ABSENT_TARGET = {"REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "DISABLE": "DISABLED", "BLOCK": "BLOCKED"}
AUTHORITATIVE_HTTP = {"200", "404"}
FULL, FILTERED, UNKNOWN, UNRELATED = "FULL", "FILTERED", "UNKNOWN", "UNRELATED"
DECLARED_SCOPES = {"snapshot_scope"}       # the capture names the systems it enumerates
GENERIC_NAME_TOKENS = {"service", "customer", "platform", "management", "data"}


@dataclass
class Obligation:
    resource_id: str
    system_id: str
    category: str
    rule_id: str
    condition: str
    treatment: str
    state: str
    basis: str                  # DIRECT | CLAIM | OMISSION:<scope location> | NONE | CATALOG_ONLY
    evidence_id: str
    evidence_at: str
    status: str
    reason: str
    link_sources: list = field(default_factory=list)
    flags: list = field(default_factory=list)


def _indeterminate(o):
    """A readback whose own status code says nothing about the record (e.g. HTTP 500)."""
    a = o.attrs
    for k in ("http_status", "status_code"):
        if k in a and str(a[k]) not in AUTHORITATIVE_HTTP:
            return f"{k}={a[k]}"
    if prefix_of(o.resource_id) == "backup":
        for k in ("eligible", "restore_allowed", "decision"):
            if k in a and str(a[k]).strip().lower() not in ("true", "false", "1", "0", "yes", "no", "allow",
                                                           "allowed", "deny", "denied"):
                return f"{k}={a[k]!r}"
    return None


class ScopeTyper:
    """Typed completeness of an enumerating scope (Phase 4 C), judged from evidence eligible at audit_at.

    UNRELATED  the scope lists records, none of the resource's type: its silence says nothing about it
    FILTERED   the scope omits a record of its system that eligible evidence shows existing at the
               scope's capture (sighted non-absent in the same file, or both at/before and at/after
               the capture); a capture that misses a live record cannot prove another is gone
    FULL       not contradicted, and the capture declares the systems it enumerates (snapshot_scope)
    UNKNOWN    not contradicted, but nothing in the capture says the listing is complete
    Limitation: a record deleted and recreated across the capture time also reads as a contradiction."""

    def __init__(self, auditor):
        self.by_system = defaultdict(list)
        for o in auditor.index.observations:
            if o.state and o.state != "ABSENT":
                self.by_system[o.system_id].append(o)
        self._cache = {}

    def status(self, scope, audit_at, rid=None):
        if rid is not None and scope.resources and prefix_of(rid) not in {prefix_of(r) for r in scope.resources}:
            return UNRELATED, f"lists no {prefix_of(rid)} records"
        key = (scope.file_id, scope.system_id, scope.location, scope.captured_at, audit_at)
        if key not in self._cache:
            same, before, after = set(), set(), set()
            for o in self.by_system[scope.system_id]:
                if o.resource_id in scope.resources or not eligible(o, audit_at):
                    continue
                t = o.observed_at or o.file_captured_at
                if o.file_id == scope.file_id:
                    same.add(o.resource_id)
                if t <= scope.captured_at:
                    before.add(o.resource_id)
                if t >= scope.captured_at:
                    after.add(o.resource_id)
            contra = sorted(same | (before & after))
            if contra:
                self._cache[key] = (FILTERED, f"omits {len(contra)} live record(s), e.g. {contra[0]}")
            elif scope.location in DECLARED_SCOPES:
                self._cache[key] = (FULL, "declared by snapshot_scope, not contradicted")
            else:
                self._cache[key] = (UNKNOWN, "completeness not declared")
        return self._cache[key]


def copy_role(location):
    loc = location.lower()
    return "failover" if "failover" in loc else (loc if "replica" in loc else "primary")


def claim_class(o, manifest):
    """How a claim-only observation was produced. LOOKUP_HISTORY: a dated per-key lookup row in a
    record history; WORKFLOW_REPORT: a narrative/handover line; SELF_REPORT / CROSS_SYSTEM_REPORT: a
    record-list row about the file's own service or about another one (name-token heuristic)."""
    if o.kind == "history":
        return "LOOKUP_HISTORY"
    if o.kind == "handover":
        return "WORKFLOW_REPORT"
    toks = lambda s: set(str(s).lower().replace("-", "_").split("_")) - GENERIC_NAME_TOKENS
    return "SELF_REPORT" if toks(manifest[o.file_id]["origin"]) & toks(o.attrs.get("system", o.system_id))         else "CROSS_SYSTEM_REPORT"


def case_links(auditor, req):
    _, linked, cutoff = auditor.case_links(req)
    links = {r: [l.source for l in v] for r, v in linked.items()}
    primary = None
    if auditor.cfg.worker_attribution and req["case_id"] in auditor.attributed(cutoff):
        primary, aid = auditor.attributed(cutoff)[req["case_id"]]
        links.setdefault(primary, []).append(aid)
    return links, primary, cutoff


def obligations(auditor, req, scope_status=None, replicas=False, admit=frozenset({FULL})):
    """scope_status(scope, audit_at, rid) -> (FULL | FILTERED | UNKNOWN | UNRELATED, reason), e.g.
    ScopeTyper(auditor).status; when given, omission from a non-FULL scope does not prove absence
    (Phase C); `admit` lists the scope types accepted as absence proof. None keeps the pipeline's
    reading of every scope as FULL. replicas=True checks each
    replica copy (failover / named replica tables) separately: the latest eligible sighting of that
    copy decides it, so a newer primary or role-less readback cannot hide a live failover copy."""
    audit_at = req["audit_at"]
    links, primary, cutoff = case_links(auditor, req)
    rules = auditor.policy.rules
    out = []
    for rid in sorted(links):
        prefix = prefix_of(rid)
        sightings = auditor.index.by_resource.get(rid, [])
        cat_hint = PREFIX_CATEGORY.get(prefix)
        if not auditor.sighted(rid, cutoff) and rid != primary:
            out.append(Obligation(rid, system_of(rid), cat_hint, (rules.get(cat_hint) or {}).get("rule_id", ""),
                                  (rules.get(cat_hint) or {}).get("condition", ""), "", "UNKNOWN", "CATALOG_ONLY",
                                  "", "", UNRESOLVED, "linked but sighted in no parsed evidence", links[rid]))
            continue
        st = auditor.states.resolve(rid, audit_at)
        basis, flags = "NONE", []
        if st is not None:
            deciding = [o for o in st.observations if o.file_id == st.evidence_id]
            basis = "CLAIM" if all(o.claim for o in deciding) else "DIRECT"
            if basis == "CLAIM":
                flags.append("claim:" + claim_class(deciding[0], auditor.ds.manifest))
            bad = [_indeterminate(o) for o in deciding if _indeterminate(o)]
            if bad:
                flags.append("indeterminate_readback:" + bad[0])
        else:
            retained = cat_hint in RETAIN_CATEGORIES
            if not retained:
                st = auditor.states.scoped_absence(rid, audit_at)
                if st is not None:
                    scope = next(s for s in auditor.states.scopes_by_system[system_of(rid)]
                                 if s.file_id == st.evidence_id and s.captured_at == st.observed_at)
                    basis = "OMISSION:" + scope.location
                    if scope_status:
                        typed = scope_status(scope, audit_at, rid)[0]
                        flags.append(f"scope:{typed}")
                        if typed not in admit:
                            flags.append(f"omission_from_{typed}_scope")
        # eligible sightings with no normalizable state (e.g. backup with unknown restore eligibility)
        stateless = [o for o in sightings if not o.state and eligible(o, audit_at) and o.kind != "log"]
        if stateless and (st is None or max(o.observed_at for o in stateless) >= st.observed_at):
            flags.append("stateless_latest_sighting:" + stateless[-1].file_id)
        if replicas and st is not None:
            for role, latest in _copies(sightings, audit_at).items():
                if role != "primary" and latest.file_id != st.evidence_id and SEVERITY.get(latest.state, 0) == 3:
                    flags.append(f"live_replica_copy:{latest.location}@{latest.file_id}:{latest.state}")
        if st is None:
            cat = cat_hint
            rule = rules.get(cat) or {}
            out.append(Obligation(rid, system_of(rid), cat, rule.get("rule_id", ""), rule.get("condition", ""), "",
                                  "UNKNOWN", basis, "", "", UNRESOLVED, "no eligible sighting and no enumerating scope",
                                  links[rid], flags))
            continue
        ret = next((o.attrs.get("retention_until") for o in reversed(st.observations) if o.attrs.get("retention_until")), None)
        d = auditor.policy.evaluate(prefix, st.category, st.state, ret, audit_at)
        cat = st.category or cat_hint
        rule = rules.get(cat) or {}
        e = Obligation(rid, st.system_id, cat, d.rule_id if d else "", rule.get("condition", ""),
                       d.treatment if d else "", st.state, basis, st.evidence_id, st.observed_at, "", "", links[rid], flags)
        if d is None:
            e.status, e.reason = UNRESOLVED, "no rule for category"
        elif d.treatment not in ("RETAIN", "EXPIRED") and any(f.startswith("live_replica_copy") for f in flags):
            e.status, e.reason = PROVEN_VIOLATION, "a replica copy's latest eligible sighting is still live"
        elif any(f.startswith(("indeterminate_readback", "stateless_latest", "omission_from_")) for f in flags):
            e.status, e.reason = UNRESOLVED, "deciding evidence cannot establish state: " + ",".join(flags)
        elif d.treatment == "EXPIRED":
            e.status, e.reason = NOT_REQUIRED, d.note
        elif d.treatment == "RETAIN" and st.state == "ABSENT":
            e.status, e.reason = PROVEN_VIOLATION, "retained record missing; no output operation can report it"
            e.flags.append("unreportable_violation")
        elif st.state == "ABSENT" and d.treatment in NON_ABSENT_TARGET:
            if cat in ("ACCOUNT_TOMBSTONE", "BACKUP_ARCHIVE"):
                # rule_15 preserves the tombstone, rule_14 retains the archive: absence is not the
                # required state, and the verification op would assert a state nobody observed
                e.status, e.reason = UNRESOLVED, f"{cat} absent where policy preserves it"
            else:
                e.status, e.reason = PROVEN_COMPLIANT, "compliant by absence"
            e.flags.append(f"verify_state_mismatch:{NON_ABSENT_TARGET[d.treatment]}!=ABSENT")
        elif d.compliant:
            e.status, e.reason = PROVEN_COMPLIANT, f"{st.state} satisfies {d.treatment}"
        else:
            e.status, e.reason = PROVEN_VIOLATION, f"{st.state} violates {d.treatment}"
        out.append(e)
    # the pipeline keeps one account; a second live copy is a violation the output would hide
    accts = [e for e in out if prefix_of(e.resource_id) == "account" and e.state != "UNKNOWN"]
    if len(accts) > 1:
        keep = primary if primary in {e.resource_id for e in accts} else \
            max(accts, key=lambda e: (e.evidence_at, e.resource_id)).resource_id
        for e in accts:
            if e.resource_id != keep:
                e.flags.append(f"suppressed_by_one_primary_account(kept {keep})")
    return out


def _copies(sightings, audit_at):
    """role -> latest eligible stateful sighting of that copy of the resource."""
    out = {}
    for o in sightings:
        if o.state and eligible(o, audit_at):
            r = copy_role(o.location)
            if r not in out or (o.observed_at, o.file_captured_at) > (out[r].observed_at, out[r].file_captured_at):
                out[r] = o
    return out


def case_decision(entries):
    """COMPLETE only if every obligation is PROVEN_COMPLIANT/NOT_REQUIRED (catalog-only links are
    reported separately, see `strict`)."""
    st = {e.status for e in entries}
    if any(e.status == PROVEN_VIOLATION and "unreportable_violation" not in e.flags for e in entries):
        return "INCOMPLETE"
    if PROVEN_VIOLATION in st:
        return "VIOLATION_UNREPORTABLE"
    if any(e.status == UNRESOLVED and e.basis != "CATALOG_ONLY" for e in entries):
        return "UNRESOLVED"
    return "COMPLETE"
