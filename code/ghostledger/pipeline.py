"""Per-case audit: intent -> subject -> linked resources -> state at audit_at -> policy -> steps."""
import csv
import io
import json
from dataclasses import dataclass, field

from . import attribution
from .evidence import EvidenceIndex, prefix_of, system_of
from .identity import IdentityGraph, Link
from .intent import classify
from .ledger import FILTERED, ScopeTyper
from .policy import FINDING_STATE, PREFIX_CATEGORY, REMEDIATION_STATE, PolicyEngine
from .safeio import atomic_write_text
from .state import RETAINED as RETAINED_CATEGORIES, ResourceState, StateEngine, eligible

from .agent import validators
from .steps import finalize, step, to_json

# system_activity event types that do not change the record they name
READ_ONLY_EVENTS = {"LINK_RESOURCE_IDENTITY", "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "CLASSIFY_REQUEST"}


@dataclass
class Config:
    identity_horizon: str = "any"      # any: identity facts from any capture; audit: only captures <= audit_at
    one_primary_account: bool = True   # keep only the most recently sighted linked account
    drop_stale_absent: bool = False    # drop resources already absent before requested_at
    use_candidates: bool = False       # workflow-correlation links (diagnostic only; never default)
    worker_attribution: bool = True    # H2: forced-edge worker-row primary-account attribution (Phase 7 default)
    temporal: str = "retrospective"    # experimental strict: identity history, catalog/structural links,
                                       # sightings and worker matching all cut off at audit_at
    agent_links: str = ""              # path to validated investigator links (agent/investigator.py); "" = none
    absence_after_operation: bool = True   # an omission proves absence only if captured after the record's
                                           # last known operation (deletion attempt) at or before audit_at
    reject_filtered_scopes: bool = True    # a capture shown to omit live records of its system (ledger.ScopeTyper
                                           # FILTERED) is a selection and never proves another record absent


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    trace: dict = field(default_factory=dict)


class Auditor:
    def __init__(self, dataset, config=None, index=None):
        self.ds = dataset
        self.cfg = config or Config()
        self.index = index or EvidenceIndex(dataset).build()
        self.graph = IdentityGraph(dataset, self.index)
        self.states = StateEngine(self.index)
        if self.cfg.reject_filtered_scopes:
            typer = ScopeTyper(self)
            self.states.scope_filter = lambda s, audit_at, rid: typer.status(s, audit_at, rid)[0] != FILTERED
        self.policy = PolicyEngine(dataset.rules)
        self._attributed = {}

    def agent_links(self):
        """case_id -> [(resource_id, evidence_id)] as stored by agent/investigator.py. Untrusted: audit()
        revalidates every entry against its own case (agent.validators.revalidate) before using it."""
        if not hasattr(self, "_agent_links"):
            self._agent_links = {}
            if self.cfg.agent_links:
                with open(self.cfg.agent_links, encoding="utf-8") as f:
                    raw = json.load(f)
                self._agent_links = {str(c): [(str(x[0]), str(x[1])) for x in v if isinstance(x, (list, tuple)) and len(x) >= 2]
                                     for c, v in raw.items() if isinstance(v, list)}
        return self._agent_links

    def attributed_pool(self):
        """Deletion-authorizing requests from every request file (the worker rows are shared)."""
        if not hasattr(self, "_pool"):
            self._pool = [r for r in attribution.request_pool(self.ds.root) if classify(r["request_text"])[0]]
        return self._pool

    def activity_by_resource(self):
        if not hasattr(self, "_act"):
            self._act = {}
            for a in self.ds.activity:
                self._act.setdefault(a["resource_id"], []).append(a)
        return self._act

    def activity_ids(self):
        if not hasattr(self, "_aids"):
            self._aids = {a["activity_id"] for a in self.ds.activity}
        return self._aids

    def last_operation(self, rid, audit_at):
        """Time of the latest activity row on `rid` at or before audit_at that can change the record
        (None: no known operation). Catalog links, checks and intake rows only read or describe it."""
        if not self.cfg.absence_after_operation:
            return None
        return max((a["event_time"] for a in self.activity_by_resource().get(rid, ())
                    if a["event_time"] <= audit_at and a["event_type"] not in READ_ONLY_EVENTS), default=None)

    def attributed(self, as_of=None):
        if as_of not in self._attributed:
            self._attributed[as_of] = attribution.attribute(self.ds, self.graph, attribution.request_pool(self.ds.root),
                                                            as_of)
        return self._attributed[as_of]

    def case_links(self, req):
        """(subject, supported links, cutoff) for one request under the configured temporal mode."""
        audit_at = req["audit_at"]
        if self.cfg.temporal == "strict":
            subj = self.graph.subject(req["email"], audit_at)
            return subj, self.graph.links(subj, lambda o: eligible(o, audit_at), audit_at), audit_at
        subj = self.graph.subject(req["email"])
        horizon = None if self.cfg.identity_horizon == "any" else (lambda o: eligible(o, audit_at))
        return subj, self.graph.links(subj, horizon), None

    def sighted(self, rid, cutoff):
        obs = self.index.by_resource.get(rid, [])
        if cutoff is None:
            return bool(obs) or bool(self.graph.structural.get(rid))
        return any(eligible(o, cutoff) for o in obs) or any(
            self.ds.manifest[f]["captured_at"] <= cutoff for _, f in self.graph.structural.get(rid, ()))

    def audit(self, req):
        cid, audit_at = req["case_id"], req["audit_at"]
        ok, why = classify(req["request_text"])
        trace = {"intent": why}
        if not ok:
            return CaseResult(cid, "NOT_APPLICABLE", [], trace)
        subj, links, cutoff = self.case_links(req)
        trace.update(user_ids=sorted(subj.user_ids), links={r: [l.source for l in v] for r, v in links.items()})
        if self.cfg.use_candidates:
            for rid, v in self.graph.candidates(req, links).items():
                links[rid] = v
        for rid, ev in self.agent_links().get(cid, ()):
            link = validators.revalidate(self, req, rid, ev)
            trace.setdefault("agent_links", []).append((rid, ev, "accepted" if link else "rejected at consumption"))
            if link:
                links.setdefault(rid, []).append(link)
        primary = None
        if self.cfg.worker_attribution and cid in self.attributed(cutoff):
            primary, aid = self.attributed(cutoff)[cid]
            links.setdefault(primary, []).append(Link(primary, "attributed", aid, "unique worker-row assignment"))
            trace["attributed"] = (primary, aid)
        resolved, dropped = {}, {}
        for rid in sorted(links):
            if not self.sighted(rid, cutoff) and rid != primary:
                dropped[rid] = "referenced by no evidence file (catalog/activity link only)"
                continue
            st = self.states.resolve(rid, audit_at)
            if st is None:
                retained = PREFIX_CATEGORY.get(prefix_of(rid)) in RETAINED_CATEGORIES
                # partial captures cannot prove a retained record is gone; for other records an
                # enumerating capture that omits it is evidence of absence
                st = None if retained else self.states.scoped_absence(rid, audit_at, self.last_operation(rid, audit_at))
                if st is None:
                    obs = self.index.by_resource.get(rid, [])
                    st = ResourceState(rid, system_of(rid), "UNKNOWN", self.states._category([], [o.category for o in obs]),
                                       "", "", "no eligible sighting", [])
            if self.cfg.drop_stale_absent and st.state == "ABSENT" and st.observed_at < req["requested_at"]:
                dropped[rid] = "already absent before the request"
                continue
            resolved[rid] = st
        if self.cfg.one_primary_account:
            accts = [r for r in resolved if prefix_of(r) == "account"]
            if len(accts) > 1:
                keep = primary if primary in accts else max(accts, key=lambda r: (resolved[r].observed_at, r))
                for r in accts:
                    if r != keep:
                        dropped[r] = f"superseded by more recently sighted account {keep}"
                        del resolved[r]
        decisions = {}
        for rid, st in resolved.items():
            ret = next((o.attrs.get("retention_until") for o in reversed(st.observations) if o.attrs.get("retention_until")), None)
            d = self.policy.evaluate(prefix_of(rid), st.category, st.state, ret, audit_at)
            if d is None:
                dropped[rid] = f"no rule for category {st.category}"
                continue
            if st.state == "ABSENT" and not st.observations and not self.policy.omission_establishes(d.treatment):
                # omitted by a capture: absence is all it shows, not the treatment state policy requires
                st = resolved[rid] = ResourceState(rid, st.system_id, "UNKNOWN", st.category, "", "",
                                                   f"{st.basis}; an omission does not establish {d.treatment}", [])
                d = self.policy.evaluate(prefix_of(rid), st.category, "UNKNOWN", ret, audit_at)
                d.note = "OMISSION_DOES_NOT_ESTABLISH_" + d.verification_state
            decisions[rid] = d
        incomplete = any(not d.compliant and d.finding and resolved[r].state != "UNKNOWN" for r, d in decisions.items())
        steps, unresolved = [], {}       # obligations whose required state the evidence does not establish
        for rid, d in decisions.items():
            st = resolved[rid]
            if st.state == "UNKNOWN":
                # state at audit_at not established: no finding; a post-remediation check is still
                # required in an incomplete case, but nothing can be cited for a complete one
                if incomplete:
                    steps.append(step("VERIFICATION", st.system_id, rid, d.verification, d.verification_state))
                else:
                    dropped[rid] = "state at audit_at not established; cannot be cited as verified"
                    unresolved[rid] = d.note if d.note.startswith("OMISSION_") else "UNKNOWN_STATE"
                continue
            if d.treatment == "EXPIRED" or (not d.compliant and not d.finding):
                dropped[rid] = d.note
                if d.treatment != "EXPIRED":
                    # a record policy keeps (retained, redacted or anonymized) is missing: no allowed
                    # operation reports it, and it is not compliant either
                    unresolved[rid] = "PRESERVED_RECORD_MISSING"
                continue
            if not d.compliant:
                steps.append(step("FINDING", st.system_id, rid, d.finding, FINDING_STATE[d.finding], st.evidence_id))
                steps.append(step("REMEDIATION", st.system_id, rid, d.remediation, REMEDIATION_STATE[d.remediation]))
            ev = "" if incomplete else st.evidence_id
            if ev and st.state == "ABSENT" and not st.observations:
                if st.scope != "snapshot_scope":
                    # omitted by a capture that does not declare full coverage of the system: kept (the
                    # public key cites such omissions), but not proof the record is gone everywhere
                    unresolved[rid] = "ABSENT_BY_UNDECLARED_SCOPE"
            steps.append(step("VERIFICATION", st.system_id, rid, d.verification, d.verification_state, ev))
        trace.update(states={r: (s.state, s.category, s.evidence_id, s.basis) for r, s in resolved.items()},
                     decisions={r: (d.rule_id, d.treatment, d.compliant) for r, d in decisions.items()},
                     dropped=dropped, unresolved=unresolved)
        return CaseResult(cid, "INCOMPLETE" if incomplete else "COMPLETE", finalize(steps), trace)

    def run(self, requests=None):
        reqs = sorted(requests or self.ds.requests, key=lambda r: r["case_id"])
        return [self.audit(r) for r in reqs]


def case_sort_key(cid):
    head, _, num = cid.rpartition("_")
    return (head, int(num)) if num.isdigit() else (cid, 0)


STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


def serialize_output(results, order=None):
    """Complete output.csv text, checked against the output contract before anything is written.
    Rows follow `order` (the input request order) when given, else numeric case order."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["case_id", "deletion_status", "steps_json"])
    seen = set()
    pos = {c: i for i, c in enumerate(order or ())}
    for r in sorted(results, key=lambda r: (pos.get(r.case_id, len(pos)), case_sort_key(r.case_id))):
        steps = to_json(r.steps)
        parsed = json.loads(steps)
        if r.case_id in seen or r.status not in STATUSES or not isinstance(parsed, list) \
                or [s["step_no"] for s in parsed] != list(range(1, len(parsed) + 1)) \
                or (r.status == "NOT_APPLICABLE" and parsed):
            raise ValueError(f"output row for {r.case_id!r} violates the output contract; nothing written")
        seen.add(r.case_id)
        w.writerow([r.case_id, r.status, steps])
    return buf.getvalue()


def write_output(results, path, gate=None, order=None):
    """Atomic write; `gate` (contract.gate) sees the complete text first and raises to refuse it."""
    text = serialize_output(results, order)
    if gate is not None:
        gate(text)
    atomic_write_text(path, text)
