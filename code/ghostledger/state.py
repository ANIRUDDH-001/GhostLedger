"""Temporal state engine: the state of one resource as of a case's audit_at.

Only evidence a reviewer could have held at audit_at is used: the file must be captured at or
before audit_at and the record-level observation time must not be later than audit_at.
The latest eligible sighting wins. Within one file every replica copy is compared and any
non-compliant copy (still present / identifiable / active) decides that file's reading, so a
drifted failover replica is not hidden by a clean primary. Absence without a sighting needs a
scope: a capture that enumerates the resource's system completely and does not list it.
Activity rows (operation claims) never establish state.
"""
from collections import defaultdict
from dataclasses import dataclass

from .evidence import system_of

# non-compliant states rank above compliant ones when copies in one file disagree
SEVERITY = {"PRESENT": 3, "IDENTIFIABLE": 3, "ACTIVE": 3,
            "REDACTED": 1, "ANONYMIZED": 1, "DISABLED": 1, "BLOCKED": 1, "ABSENT": 0}
SCOPE_KINDS = {"snapshot_scope", "collection"}


@dataclass
class ResourceState:
    resource_id: str
    system_id: str
    state: str
    category: str
    evidence_id: str
    observed_at: str
    basis: str
    observations: list


def eligible(o, audit_at):
    return o.file_captured_at <= audit_at and (o.observed_at or o.file_captured_at) <= audit_at


def scope_kind(s):
    if s.location in ("snapshot_scope", "replica_tables"):
        return s.location
    return "collection"          # typed SQL table dump or JSON snapshot collection


class StateEngine:
    def __init__(self, index, scope_kinds=SCOPE_KINDS):
        self.index = index
        self.scopes_by_system = defaultdict(list)
        for s in index.scopes:
            if scope_kind(s) in scope_kinds:
                self.scopes_by_system[s.system_id].append(s)

    def resolve(self, resource_id, audit_at):
        obs = [o for o in self.index.by_resource.get(resource_id, []) if o.state and eligible(o, audit_at)]
        cats = [o.category for o in self.index.by_resource.get(resource_id, []) if o.category]
        best = None
        if obs:
            by_file = defaultdict(list)
            for o in obs:
                by_file[o.file_id].append(o)
            # the file whose latest sighting of the resource is the most recent; ties -> later capture
            fid = max(by_file, key=lambda f: (max(o.observed_at for o in by_file[f]), by_file[f][0].file_captured_at, f))
            group = by_file[fid]
            o = max(group, key=lambda x: (SEVERITY.get(x.state, 0), x.observed_at))
            best = ResourceState(resource_id, o.system_id, o.state, self._category(group, cats), fid,
                                 max(x.observed_at for x in group), f"latest sighting in {o.location} ({o.kind})", obs)
        if best:
            # Captures list a selection of records, not the whole system: an omission never
            # overrides a direct sighting (measured: it hid a drifted replica and retained audit records).
            return best
        return None

    def scoped_absence(self, resource_id, audit_at):
        """For a resource with no eligible sighting: ABSENT if an eligible capture enumerates its
        system without listing it. Callers decide whether that is enough for the policy at hand."""
        cats = [o.category for o in self.index.by_resource.get(resource_id, []) if o.category]
        system = system_of(resource_id)
        s = self.absence_for_system(system, audit_at, resource_id) if system else None
        if s:
            return ResourceState(resource_id, system, "ABSENT", self._category([], cats), s.file_id, s.captured_at,
                                 f"no sighting; not listed in {s.location} capture of {system}", [])
        return None

    def absence_for_system(self, system, audit_at, resource_id=None):
        """Latest eligible scope that enumerates `system` without listing resource_id."""
        cands = [s for s in self.scopes_by_system.get(system, [])
                 if s.file_captured_at <= audit_at and s.captured_at <= audit_at and resource_id not in s.resources]
        return max(cands, key=lambda s: (s.captured_at, s.file_id)) if cands else None

    @staticmethod
    def _category(group, cats):
        # the most specific category seen for the resource (attachment/marketing outrank generic)
        pool = [o.category for o in group if o.category] or cats
        for pref in ("SUPPORT_ATTACHMENT", "MARKETING_PROFILE"):
            if pref in pool:
                return pref
        return pool[0] if pool else None

