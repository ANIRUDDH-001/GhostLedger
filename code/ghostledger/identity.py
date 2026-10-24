"""Identity graph: subject identifiers (current + historical) and resource links.

Two link tiers are kept apart:
  supported  - the resource's own record (or its trace's lookup line, or a document row that
               names it) carries one of the subject's identifiers; or an explicit structural link
               (source_resource_id, data-catalog LINK_RESOURCE_IDENTITY) to the subject or to a
               supported resource.
  candidate  - workflow correlation only: a deletion-worker activity row for the resource falls
               inside the request window. Timestamp proximity alone never makes a link supported;
               candidates are reported for diagnosis and are not used to build output.
"""
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .evidence import EMAIL_RE, RESOURCE_RE, identifiers_in, norm_phone


@dataclass
class Subject:
    request_email: str
    user_ids: set
    identifiers: set                       # normalized "kind:value" tokens
    history: list = field(default_factory=list)   # audit_log rows that contributed


@dataclass
class Link:
    resource_id: str
    tier: str                              # supported | candidate
    source: str                            # evidence/activity id that establishes it
    basis: str                             # human-readable reason
    at: str = None                         # capture time of the source


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class IdentityGraph:
    def __init__(self, dataset, index):
        self.ds = dataset
        self.index = index
        self.users = {r["user_id"]: r for r in dataset.identities}
        self.email_to_users = defaultdict(set)
        self.history = defaultdict(list)
        for r in dataset.identities:
            self.email_to_users[r["email"].strip().lower()].add(r["user_id"])
        for r in dataset.audit_log:
            self.history[r["user_id"]].append(r)
            if r["field_name"] == "email":
                for v in (r["old_value"], r["new_value"]):
                    if v:
                        self.email_to_users[v.strip().lower()].add(r["user_id"])
        # data-catalog links from the activity table (structural, not temporal)
        self.catalog = defaultdict(list)
        for a in dataset.activity:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                ids = identifiers_in(a["details"])
                self.catalog[a["resource_id"]].append((a["activity_id"], a["event_time"], ids))
        # explicit structural links carried on records (e.g. search doc -> source account)
        self.structural = defaultdict(set)
        for o in index.observations:
            for l in o.links:
                self.structural[o.resource_id].add((l, o.file_id))
                self.structural[l].add((o.resource_id, o.file_id))

    def subject(self, email, as_of=None):
        """as_of (experimental strict cutoff): only identity history known at as_of; the current
        identity row is rolled back through later audit_log changes. None = all history."""
        if as_of is not None:
            return self._subject_as_of(email, as_of)
        email = email.strip().lower()
        uids = set(self.email_to_users.get(email, set()))
        ids = {"email:" + email, "hash:" + sha(email)}
        hist = []
        for u in uids:
            r = self.users.get(u)
            ids.add("user:" + u)
            if r:
                ids.add("email:" + r["email"].strip().lower())
                ids.add("hash:" + sha(r["email"].strip().lower()))
                if r.get("email_hash"):
                    ids.add("hash:" + r["email_hash"].lower())
                if r.get("phone"):
                    ids.add("phone:" + norm_phone(r["phone"]))
            for h in self.history.get(u, []):
                hist.append(h)
                for v in (h["old_value"], h["new_value"]):
                    if not v:
                        continue
                    if h["field_name"] == "email":
                        ids.add("email:" + v.strip().lower())
                        ids.add("hash:" + sha(v.strip().lower()))
                    elif h["field_name"] == "email_hash":
                        ids.add("hash:" + v.strip().lower())
                    elif h["field_name"] == "phone":
                        ids.add("phone:" + norm_phone(v))
        return Subject(email, uids, ids, hist)

    def _subject_as_of(self, email, as_of):
        email = email.strip().lower()
        known = {}                          # user_id -> {field: values known at as_of}
        for u in self.email_to_users.get(email, set()):
            r, vals = self.users.get(u), {"email": set(), "email_hash": set(), "phone": set()}
            later = sorted((h for h in self.history.get(u, []) if h["event_time"] > as_of), key=lambda h: h["event_time"])
            for f in vals:
                first_later = next((h for h in later if h["field_name"] == f), None)
                cur = first_later["old_value"] if first_later else (r or {}).get(f)
                if cur:
                    vals[f].add(cur)
            hist = [h for h in self.history.get(u, []) if h["event_time"] <= as_of]
            for h in hist:
                for v in (h["old_value"], h["new_value"]):
                    if v and h["field_name"] in vals:
                        vals[h["field_name"]].add(v)
            if email in {v.strip().lower() for v in vals["email"]}:
                known[u] = (vals, hist)
        ids = {"email:" + email, "hash:" + sha(email)}
        hist_all = []
        for u, (vals, hist) in known.items():
            ids.add("user:" + u)
            for v in vals["email"]:
                ids.add("email:" + v.strip().lower())
                ids.add("hash:" + sha(v.strip().lower()))
            ids.update("hash:" + v.lower() for v in vals["email_hash"])
            ids.update("phone:" + norm_phone(v) for v in vals["phone"])
            hist_all.extend(hist)
        return Subject(email, set(known), ids, hist_all)

    def links(self, subject, eligible=None, as_of=None):
        """Return {resource_id: [Link]} for supported links. `eligible(obs)` restricts which
        observations may establish identity (temporal horizon); None = any observation.
        as_of (experimental strict cutoff) also drops catalog rows and structural links recorded
        after as_of."""
        out = defaultdict(list)
        for o in self.index.observations:
            if eligible is not None and not eligible(o):
                continue
            hit = o.identifiers & subject.identifiers
            if hit:
                out[o.resource_id].append(Link(o.resource_id, "supported", o.file_id,
                                               "identifier match: " + ",".join(sorted(hit))[:120],
                                               o.file_captured_at))
        for rid, rows in self.catalog.items():
            for aid, at, ids in rows:
                if as_of is not None and at > as_of:
                    continue
                if ids & subject.identifiers:
                    out[rid].append(Link(rid, "supported", aid, "data catalog link", at))
        # one hop over explicit structural links from supported resources
        for rid in list(out):
            for other, fid in self.structural.get(rid, ()):
                if as_of is not None and self.ds.manifest[fid]["captured_at"] > as_of:
                    continue
                if other not in out:
                    out[other].append(Link(other, "supported", fid, f"structural link to {rid}",
                                           self.ds.manifest[fid]["captured_at"]))
        return out

    def candidates(self, request, supported):
        """Workflow-correlation candidates: deletion-worker rows inside [requested_at, reported_at]
        for resources with no supported link. Diagnostic only."""
        out = defaultdict(list)
        lo, hi = request["requested_at"], request["reported_at"]
        for a in self.ds.activity:
            if a["source_system"] != "deletion_worker":
                continue
            if lo <= a["event_time"] <= hi and a["resource_id"] not in supported:
                out[a["resource_id"]].append(Link(a["resource_id"], "candidate", a["activity_id"],
                                                  f"{a['event_type']} in request window", a["event_time"]))
        return out
