"""Read-only investigative tools over one case. Every tool reads the participant-facing dataset through
the existing EvidenceIndex / IdentityGraph / StateEngine / PolicyEngine of a pipeline.Auditor and
returns plain JSON. Nothing here writes, and no tool reveals evidence captured after the case's
audit_at except as a count (so the model can say "later evidence exists" but cannot use it).

Ownership is reported as a class, not as raw identifiers: "requester" (an identifier of the
requester), "other:<n>" (identifiers naming n other users only), "both" or "none".
"""
from .. import attribution
from . import validators
from ..evidence import prefix_of, system_of
from ..policy import PREFIX_CATEGORY
from ..state import eligible

MAX_ROWS = 25


class Toolbox:
    def __init__(self, auditor, req, baseline):
        self.a, self.req, self.base = auditor, req, baseline
        self.subj = auditor.graph.subject(req["email"])
        self.calls = []                                  # (tool, args, n results) for the trace
        self._id_users = None
        self.seen_ids = set()                            # every evidence/activity/resource id a tool returned

    # -- helpers -------------------------------------------------------------------------------
    def id_users(self):
        if self._id_users is None:
            self._id_users = attribution.identifier_users(self.a.graph)
        return self._id_users

    def owner_class(self, identifiers):
        return validators.owner_class(self.a, self.subj, identifiers)

    def _note(self, tool, args, out):
        self.calls.append({"tool": tool, "args": args, "n": len(out) if isinstance(out, list) else 1})
        return out

    # -- tools ---------------------------------------------------------------------------------
    def case_summary(self):
        r, b = self.req, self.base
        out = {"case_id": r["case_id"], "requested_at": r["requested_at"], "reported_status": r["reported_status"],
               "reported_at": r["reported_at"], "audit_at": r["audit_at"], "request_text": r["request_text"],
               "requester_user_ids": sorted(self.subj.user_ids), "known_identifier_count": len(self.subj.identifiers),
               "deterministic_status": b.status,
               "deterministic_resources": {rid: {"state": s[0], "evidence_id": s[2], "basis": s[3]}
                                           for rid, s in b.trace.get("states", {}).items()},
               "attributed_by_timing": b.trace.get("attributed"),
               "dropped": b.trace.get("dropped", {})}
        self.seen_ids |= set(out["deterministic_resources"]) | {s["evidence_id"] for s in out["deterministic_resources"].values()}
        return self._note("case_summary", {}, out)

    def worker_rows(self):
        """Deletion-worker rows inside this request's processing window, with how many other requests'
        windows also contain each row (competition = timing alone cannot decide ownership)."""
        r = self.req
        lo = max(attribution._t(r["requested_at"]), attribution._t(r["reported_at"]) - attribution.LEAD)
        hi = attribution._t(r["reported_at"]) + attribution.SLACK
        pool = [p for p in self.a.attributed_pool() if p["case_id"] != r["case_id"]]
        out = []
        for a in self.a.ds.activity:
            if a["source_system"] != "deletion_worker" or not lo <= attribution._t(a["event_time"]) <= hi:
                continue
            t = attribution._t(a["event_time"])
            rivals = sum(1 for p in pool if max(attribution._t(p["requested_at"]), attribution._t(p["reported_at"])
                                                 - attribution.LEAD) <= t <= attribution._t(p["reported_at"]) + attribution.SLACK)
            out.append({"activity_id": a["activity_id"], "event_time": a["event_time"], "event_type": a["event_type"],
                        "resource_id": a["resource_id"], "reported_result": a["reported_result"],
                        "other_requests_in_window": rivals})
        out = out[:MAX_ROWS]
        self.seen_ids |= {x["activity_id"] for x in out} | {x["resource_id"] for x in out}
        return self._note("worker_rows", {}, out)

    def resource_evidence(self, resource_id):
        """Every sighting of a resource in evidence captured at or before audit_at."""
        at = self.req["audit_at"]
        obs = self.a.index.by_resource.get(resource_id, [])
        rows = [{"evidence_id": o.file_id, "captured_at": o.file_captured_at, "observed_at": o.observed_at,
                 "state": o.state, "location": o.location, "kind": o.kind, "owner": self.owner_class(o.identifiers),
                 "is_claim": o.claim}
                for o in obs if eligible(o, at)][:MAX_ROWS]
        later = sum(1 for o in obs if not eligible(o, at))
        self.seen_ids |= {x["evidence_id"] for x in rows} | ({resource_id} if obs else set())
        return self._note("resource_evidence", {"resource_id": resource_id},
                          {"resource_id": resource_id, "system_id": system_of(resource_id), "sightings": rows,
                           "sightings_after_audit_at": later,
                           "structural_links": sorted({l for l, _ in self.a.graph.structural.get(resource_id, ())})[:10]})

    def resource_activity(self, resource_id):
        """Activity rows for a resource up to audit_at. Activity results are claims, not proof of state."""
        rows = [{"activity_id": a["activity_id"], "event_time": a["event_time"], "event_type": a["event_type"],
                 "source_system": a["source_system"], "reported_result": a["reported_result"]}
                for a in self.a.activity_by_resource().get(resource_id, []) if a["event_time"] <= self.req["audit_at"]]
        rows = rows[:MAX_ROWS]
        self.seen_ids |= {x["activity_id"] for x in rows}
        return self._note("resource_activity", {"resource_id": resource_id}, rows)

    def search_evidence(self, text):
        """Resources whose evidence records (captured by audit_at) contain `text` (>= 4 chars, case-insensitive)."""
        text = (text or "").strip().lower()
        if len(text) < 4:
            return self._note("search_evidence", {"text": text}, {"error": "query must be at least 4 characters"})
        at, out, seen = self.req["audit_at"], [], set()
        for o in self.a.index.observations:
            if not eligible(o, at) or (o.resource_id, o.file_id) in seen:
                continue
            if text in str(o.attrs).lower() or text in o.resource_id:
                seen.add((o.resource_id, o.file_id))
                out.append({"resource_id": o.resource_id, "evidence_id": o.file_id, "state": o.state,
                            "owner": self.owner_class(o.identifiers)})
                if len(out) >= MAX_ROWS:
                    break
        self.seen_ids |= {x["resource_id"] for x in out} | {x["evidence_id"] for x in out}
        return self._note("search_evidence", {"text": text}, out)

    def policy_for(self, resource_id):
        st = self.a.states.resolve(resource_id, self.req["audit_at"])
        cat = st.category if st else PREFIX_CATEGORY.get(prefix_of(resource_id))
        d = self.a.policy.evaluate(prefix_of(resource_id), cat, st.state if st else "UNKNOWN", None, self.req["audit_at"])
        return self._note("policy_for", {"resource_id": resource_id},
                          {"resource_id": resource_id, "category": cat, "state_at_audit": st.state if st else None,
                           "rule_id": d.rule_id if d else None, "treatment": d.treatment if d else None,
                           "compliant": d.compliant if d else None})

    TOOLS = {
        "case_summary": ("case_summary()", "request, requester user ids, and the deterministic engine's current result"),
        "worker_rows": ("worker_rows()", "deletion-worker activity rows in this request's processing window"),
        "resource_evidence": ("resource_evidence(resource_id)", "evidence sightings of a resource up to audit_at with ownership class"),
        "resource_activity": ("resource_activity(resource_id)", "activity rows (claims) for a resource up to audit_at"),
        "search_evidence": ("search_evidence(text)", "find evidence records containing a text fragment"),
        "policy_for": ("policy_for(resource_id)", "category, deletion rule and required treatment for a resource"),
    }

    def call(self, name, args):
        if name not in self.TOOLS:
            return {"error": f"unknown tool {name!r}"}
        args = args if isinstance(args, dict) else {}
        try:
            if name in ("case_summary", "worker_rows"):
                return getattr(self, name)()
            if name == "search_evidence":
                return self.search_evidence(str(args.get("text", "")))
            return getattr(self, name)(str(args.get("resource_id", "")))
        except Exception as e:                           # a tool error is reported to the model, never raised
            return {"error": f"{type(e).__name__}: {e}"[:200]}
