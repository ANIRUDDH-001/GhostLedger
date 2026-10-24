"""Per-case audit: intent -> subject -> linked resources -> state at audit_at -> policy -> steps."""
import csv
from dataclasses import dataclass, field

from . import attribution
from .evidence import EvidenceIndex, prefix_of, system_of
from .identity import IdentityGraph, Link
from .intent import classify
from .policy import FINDING_STATE, PREFIX_CATEGORY, REMEDIATION_STATE, PolicyEngine
from .state import ResourceState, StateEngine, eligible

RETAINED_CATEGORIES = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}
from .steps import finalize, step, to_json


@dataclass
class Config:
    identity_horizon: str = "any"      # any: identity facts from any capture; audit: only captures <= audit_at
    one_primary_account: bool = True   # keep only the most recently sighted linked account
    drop_stale_absent: bool = False    # drop resources already absent before requested_at
    use_candidates: bool = False       # workflow-correlation links (diagnostic only; never default)
    worker_attribution: bool = False   # experimental H2: unambiguous worker-row primary-account attribution
    temporal: str = "retrospective"    # experimental strict: identity history, catalog/structural links,
                                       # sightings and worker matching all cut off at audit_at


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
        self.policy = PolicyEngine(dataset.rules)
        self._attributed = {}

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
                st = None if retained else self.states.scoped_absence(rid, audit_at)
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
            decisions[rid] = d
        incomplete = any(not d.compliant and d.finding and resolved[r].state != "UNKNOWN" for r, d in decisions.items())
        steps = []
        for rid, d in decisions.items():
            st = resolved[rid]
            if st.state == "UNKNOWN":
                # state at audit_at not established: no finding; a post-remediation check is still
                # required in an incomplete case, but nothing can be cited for a complete one
                if incomplete:
                    steps.append(step("VERIFICATION", st.system_id, rid, d.verification, d.verification_state))
                else:
                    dropped[rid] = "state at audit_at not established; cannot be cited as verified"
                continue
            if d.treatment == "EXPIRED" or (not d.compliant and not d.finding):
                dropped[rid] = d.note
                continue
            if not d.compliant:
                steps.append(step("FINDING", st.system_id, rid, d.finding, FINDING_STATE[d.finding], st.evidence_id))
                steps.append(step("REMEDIATION", st.system_id, rid, d.remediation, REMEDIATION_STATE[d.remediation]))
            ev = "" if incomplete else st.evidence_id
            steps.append(step("VERIFICATION", st.system_id, rid, d.verification, d.verification_state, ev))
        trace.update(states={r: (s.state, s.category, s.evidence_id, s.basis) for r, s in resolved.items()},
                     decisions={r: (d.rule_id, d.treatment, d.compliant) for r, d in decisions.items()},
                     dropped=dropped)
        return CaseResult(cid, "INCOMPLETE" if incomplete else "COMPLETE", finalize(steps), trace)

    def run(self, requests=None):
        reqs = sorted(requests or self.ds.requests, key=lambda r: r["case_id"])
        return [self.audit(r) for r in reqs]


def case_sort_key(cid):
    head, _, num = cid.rpartition("_")
    return (head, int(num)) if num.isdigit() else (cid, 0)


def write_output(results, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in sorted(results, key=lambda r: case_sort_key(r.case_id)):
            w.writerow([r.case_id, r.status, to_json(r.steps)])
