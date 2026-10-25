"""Deterministic validation of investigator proposals. A model's wording, confidence or quoted span is
never evidence: each check below is recomputed from the dataset.

A "link" proposal (resource R belongs to this requester and is missing from the audit) is accepted
only if every check passes:
  provenance   every cited id exists in the dataset and was returned by a tool in this investigation
  novelty      R is not already in the deterministic result (otherwise it is a confirmation, not a change)
  relevance    R is a physical copy type the policy covers (known record prefix)
  temporal     R's state at audit_at is established by evidence captured at or before audit_at
  authority    that state comes from an evidence file reading, not an activity row or a reported result
  ownership    an eligible sighting of R carries one of the requester's identifiers, and no eligible
               sighting names another user; timing alone (worker rows) is never ownership for a new link,
               and a grounded conflict (an identifier of someone else) is never overridden
  policy       deletion_rules.csv has a treatment for R's category
"""
from ..evidence import prefix_of
from ..identity import Link
from ..policy import PREFIX_CATEGORY
from ..state import eligible

CHECKS = ("provenance", "novelty", "relevance", "temporal", "authority", "ownership", "policy")


def known_id(auditor, x):
    a = auditor
    return x in a.ds.manifest or x in a.activity_ids() or x in a.index.by_resource or x in a.activity_by_resource()


def validate_link(auditor, req, baseline, prop, tools):
    rid = str(prop.get("resource_id", ""))
    cited = [str(x) for x in (prop.get("evidence_ids") or []) if x]
    at = req["audit_at"]
    res = {}
    ids = [rid] + cited
    res["provenance"] = all(known_id(auditor, x) and x in tools.seen_ids for x in ids) and bool(rid)
    res["novelty"] = rid not in baseline.trace.get("states", {})
    res["relevance"] = prefix_of(rid) in PREFIX_CATEGORY
    st = auditor.states.resolve(rid, at)
    obs = [o for o in auditor.index.by_resource.get(rid, []) if eligible(o, at)]
    res["temporal"] = st is not None and any(o.file_id in cited for o in obs)
    res["authority"] = st is not None and any(not o.claim and o.file_id == st.evidence_id for o in obs)
    owners = {tools.owner_class(o.identifiers) for o in obs}
    conflict = any(c == "both" or c.startswith("other") for c in owners)
    res["ownership"] = "requester" in owners and not conflict
    d = auditor.policy.evaluate(prefix_of(rid), st.category if st else None, st.state if st else "UNKNOWN", None, at)
    res["policy"] = d is not None
    ok = all(res[c] for c in CHECKS)
    reason = "accepted" if ok else "rejected: " + ",".join(c for c in CHECKS if not res[c])
    if conflict:
        reason += " (grounded ownership conflict: evidence names another user)"
    link = Link(rid, "supported", st.evidence_id, "investigator proposal validated by identifier sighting", at) if ok else None
    return ok, res, reason, link


def validate_contradiction(auditor, req, prop, tools):
    """Advisory only: a cited evidence reading that disagrees with the workflow's reported status."""
    rid = str(prop.get("resource_id", ""))
    cited = [str(x) for x in (prop.get("evidence_ids") or []) if x]
    at = req["audit_at"]
    st = auditor.states.resolve(rid, at)
    prov = bool(rid) and all(known_id(auditor, x) and x in tools.seen_ids for x in [rid] + cited)
    grounded = prov and st is not None and st.evidence_id in cited
    return grounded, {"provenance": prov, "state_cited": grounded}, ("grounded" if grounded else "ungrounded")
