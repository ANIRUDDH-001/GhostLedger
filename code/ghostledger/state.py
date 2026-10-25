"""Temporal state engine: the state of one resource as of a case's audit_at.

Only evidence a reviewer could have held at audit_at is used: the file must be captured at or
before audit_at and the record-level observation time must not be later than audit_at.
The latest eligible sighting wins. Within one file each physical location (replica table, store)
contributes its latest reading and any non-compliant one (still present / identifiable / active)
decides that file's reading, so a drifted failover replica is not hidden by a clean primary and a
stale row in a time series does not outlive a later reading (file_reading); a file that only claims
compliance gives no reading. Absence without a sighting needs a scope: a capture that enumerates the
resource's system and does not list it, taken after the last known operation on the record.
Activity rows (operation claims) never establish state.
"""
from collections import defaultdict
from dataclasses import dataclass

from .evidence import system_of

# non-compliant states rank above compliant ones when copies in one file disagree
SEVERITY = {"PRESENT": 3, "IDENTIFIABLE": 3, "ACTIVE": 3,
            "REDACTED": 1, "ANONYMIZED": 1, "DISABLED": 1, "BLOCKED": 1, "ABSENT": 0}
SCOPE_KINDS = {"snapshot_scope", "collection"}
RETAINED = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}   # records whose compliant state is PRESENT


def adverse(o, retained):
    """Whether reading `o` would keep an obligation open (the state a claim may still assert)."""
    return o.state != "PRESENT" if retained else SEVERITY.get(o.state, 0) >= 3


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
    scope: str = ""          # for an omission: the location of the enumerating capture


def eligible(o, audit_at):
    return o.file_captured_at <= audit_at and (o.observed_at or o.file_captured_at) <= audit_at


def scope_kind(s):
    if s.location in ("snapshot_scope", "replica_tables"):
        return s.location
    return "collection"          # typed SQL table dump or JSON snapshot collection


class StateEngine:
    def __init__(self, index, scope_kinds=SCOPE_KINDS, in_file="per_location"):
        self.index = index
        self.in_file = in_file               # per_location (Phase 7) | max_severity (Phase 6 and earlier)
        self.scope_filter = None             # (scope, audit_at, resource_id) -> may this capture prove absence?
        self.scopes_by_system = defaultdict(list)
        for s in index.scopes:
            if scope_kind(s) in scope_kinds:
                self.scopes_by_system[s.system_id].append(s)

    def resolve(self, resource_id, audit_at):
        obs = [o for o in self.index.by_resource.get(resource_id, []) if o.state and eligible(o, audit_at)]
        cats = [o.category for o in self.index.by_resource.get(resource_id, []) if o.category]
        if obs:
            by_file = defaultdict(list)
            for o in obs:
                by_file[o.file_id].append(o)
            # the file whose latest sighting of the resource is the most recent; ties -> later capture.
            # A file whose only readings are compliance claims gives no reading: the next file decides.
            for fid in sorted(by_file, key=lambda f: (max(o.observed_at for o in by_file[f]), by_file[f][0].file_captured_at, f),
                              reverse=True):
                group = by_file[fid]
                o = self.file_reading(group)
                if o is not None:
                    # Captures list a selection of records, not the whole system: an omission never
                    # overrides a direct sighting (measured: it hid a drifted replica and retained audit records).
                    return ResourceState(resource_id, o.system_id, o.state, self._category(group, cats), fid,
                                         max(x.observed_at for x in group), f"latest sighting in {o.location} ({o.kind})", obs)
        return None

    def file_reading(self, group):
        """The reading one file gives for a resource.

        per_location: each physical location (replica table, store, claim list) contributes its
        latest eligible reading, so a time series inside one file is not decided by a stale row;
        a compliant claim (deleted / anonymized, or PRESENT for a retained record) never
        establishes compliance, so that location falls back to its latest
        direct reading (or drops out). The most severe location reading decides, so a drifted
        replica still outranks a clean primary; same-timestamp disagreements resolve to the more
        severe reading. If every location drops out (compliance only claimed) the file gives no
        reading (None): a claim can keep a finding open but never close it."""
        sev = lambda x: (SEVERITY.get(x.state, 0), x.observed_at)
        if self.in_file != "per_location":
            return max(group, key=sev)
        by_loc = defaultdict(list)
        for x in group:
            by_loc[x.location].append(x)
        retained = any(x.category in RETAINED for x in group)
        picked = []
        for xs in by_loc.values():
            o = max(xs, key=lambda x: (x.observed_at, SEVERITY.get(x.state, 0)))
            if o.claim and not adverse(o, retained):
                direct = [x for x in xs if not x.claim]
                o = max(direct, key=lambda x: (x.observed_at, SEVERITY.get(x.state, 0))) if direct else None
            if o:
                picked.append(o)
        return max(picked, key=sev) if picked else None

    def scoped_absence(self, resource_id, audit_at, after=None):
        """For a resource with no eligible sighting: ABSENT if an eligible capture enumerates its
        system without listing it. `after` (the latest operation on the record known by audit_at,
        e.g. the deletion attempt) bounds the capture from below: an omission recorded before that
        operation says nothing about the record after it. Callers decide whether that is enough for
        the policy at hand."""
        cats = [o.category for o in self.index.by_resource.get(resource_id, []) if o.category]
        system = system_of(resource_id)
        s = self.absence_for_system(system, audit_at, resource_id, after) if system else None
        if s:
            return ResourceState(resource_id, system, "ABSENT", self._category([], cats), s.file_id, s.captured_at,
                                 f"no sighting; not listed in {s.location} capture of {system}", [], s.location)
        return None

    def absence_for_system(self, system, audit_at, resource_id=None, after=None):
        """Eligible scope that enumerates `system` without listing resource_id (captured at or after
        `after` when given). A capture that declares its coverage of the system (snapshot_scope) is
        preferred over a collection that may be a selection; the latest wins within each kind."""
        cands = [s for s in self.scopes_by_system.get(system, [])
                 if s.file_captured_at <= audit_at and s.captured_at <= audit_at and resource_id not in s.resources
                 and (after is None or s.captured_at >= after)
                 and (self.scope_filter is None or self.scope_filter(s, audit_at, resource_id))]
        return max(cands, key=lambda s: (s.location == "snapshot_scope", s.captured_at, s.file_id)) if cands else None

    @staticmethod
    def _category(group, cats):
        # the most specific category seen for the resource (attachment/marketing outrank generic)
        pool = [o.category for o in group if o.category] or cats
        for pref in ("SUPPORT_ATTACHMENT", "MARKETING_PROFILE"):
            if pref in pool:
                return pref
        return pool[0] if pool else None

