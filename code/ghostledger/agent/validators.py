"""Deterministic validation of investigator proposals. A model's wording, confidence or quoted span is
never evidence: each check below is recomputed from the dataset.

A "link" proposal (resource R belongs to this requester and is missing from the audit) is accepted
only if every check passes:
  provenance   every cited id exists in the dataset and was returned by a tool in this investigation
  novelty      R is not already in the deterministic result (otherwise it is a confirmation, not a change)
  relevance    R is a physical copy type the policy covers (known record prefix)
  temporal     R's state at audit_at is established by evidence captured at or before audit_at
  authority    that state comes from an evidence file reading, not an activity row or a reported result
  ownership    a cited eligible non-claim reading of R carries one of the requester's identifiers, and no eligible
               sighting names another user; timing alone (worker rows) is never ownership for a new link,
               and a grounded conflict (an identifier of someone else) is never overridden
  policy       deletion_rules.csv has a treatment for R's category

The same checks (minus provenance/novelty, which belong to one investigation) run again when the engine
consumes a stored link (revalidate), so an edited or injected links file cannot change an output.
"""
from .. import attribution
from ..evidence import prefix_of
from ..identity import Link
from ..policy import PREFIX_CATEGORY
from ..state import eligible

CHECKS = ("provenance", "novelty", "relevance", "temporal", "authority", "ownership", "policy")


def known_id(auditor, x):
    a = auditor
    return x in a.ds.manifest or x in a.activity_ids() or x in a.index.by_resource or x in a.activity_by_resource()


def identifier_users(auditor):
    if not hasattr(auditor, "_identifier_users"):
        auditor._identifier_users = attribution.identifier_users(auditor.graph)
    return auditor._identifier_users


def owner_class(auditor, subj, identifiers):
    """requester / both / other:N / none for the identifiers one sighting carries."""
    if not identifiers:
        return "none"
    mine = bool(identifiers & subj.identifiers)
    users = set().union(*(identifier_users(auditor).get(i, ()) for i in identifiers))
    others = users - subj.user_ids
    if mine and others:
        return "both"
    if mine:
        return "requester"
    return f"other:{len(others)}" if others else "none"


def check_grounding(auditor, req, rid, cited):
    """The dataset-recomputed checks shared by proposal (validate_link) and consumption (Auditor.agent_links).

    Returns (checks, state, conflict). The cited evidence must itself be an eligible, non-claim reading
    of rid that carries one of the requester's identifiers, and no eligible sighting of rid may name
    another user."""
    at = req["audit_at"]
    subj = auditor.graph.subject(req["email"])
    res = {"relevance": prefix_of(rid) in PREFIX_CATEGORY}
    st = auditor.states.resolve(rid, at) if rid else None
    obs = [o for o in auditor.index.by_resource.get(rid, []) if eligible(o, at)]
    cited_obs = [o for o in obs if o.file_id in cited]
    res["temporal"] = st is not None and bool(cited_obs)
    res["authority"] = st is not None and any(not o.claim and o.file_id == st.evidence_id for o in obs)
    owners = {owner_class(auditor, subj, o.identifiers) for o in obs}
    conflict = any(c == "both" or c.startswith("other") for c in owners)
    res["ownership"] = not conflict and any(not o.claim and owner_class(auditor, subj, o.identifiers) == "requester"
                                            for o in cited_obs)
    d = auditor.policy.evaluate(prefix_of(rid), st.category if st else None, st.state if st else "UNKNOWN", None, at)
    res["policy"] = d is not None
    return res, st, conflict


def validate_link(auditor, req, baseline, prop, tools):
    rid = str(prop.get("resource_id", ""))
    cited = [str(x) for x in (prop.get("evidence_ids") or []) if x]
    at = req["audit_at"]
    res = {"provenance": bool(rid) and all(known_id(auditor, x) and x in tools.seen_ids for x in [rid] + cited),
           "novelty": rid not in baseline.trace.get("states", {})}
    g, st, conflict = check_grounding(auditor, req, rid, cited)
    res.update(g)
    ok = all(res[c] for c in CHECKS)
    reason = "accepted" if ok else "rejected: " + ",".join(c for c in CHECKS if not res[c])
    if conflict:
        reason += " (grounded ownership conflict: evidence names another user)"
    link = Link(rid, "supported", st.evidence_id, "investigator proposal validated by identifier sighting", at) if ok else None
    return ok, res, reason, link


def revalidate(auditor, req, rid, ev):
    """Consumption-time gate: a stored link is re-checked against this case before it can change output.
    Nothing from the links file is trusted but the (case, resource, evidence) triple; the emitted link
    cites the evidence the state engine resolves, never the file's basis text."""
    if not (known_id(auditor, rid) and known_id(auditor, ev)):
        return None
    g, st, _ = check_grounding(auditor, req, rid, [ev])
    if not all(g.values()):
        return None
    return Link(rid, "supported", st.evidence_id, "investigator link revalidated at consumption", req["audit_at"])


def validate_contradiction(auditor, req, prop, tools):
    """Advisory only: a cited evidence reading that disagrees with the workflow's reported status."""
    rid = str(prop.get("resource_id", ""))
    cited = [str(x) for x in (prop.get("evidence_ids") or []) if x]
    at = req["audit_at"]
    st = auditor.states.resolve(rid, at)
    prov = bool(rid) and all(known_id(auditor, x) and x in tools.seen_ids for x in [rid] + cited)
    grounded = prov and st is not None and st.evidence_id in cited
    return grounded, {"provenance": prov, "state_cited": grounded}, ("grounded" if grounded else "ungrounded")
