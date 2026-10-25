"""Agent-link trust boundary: a stored links file is untrusted input. Every (case, resource, evidence)
entry is revalidated against its own case when the engine consumes it (agent.validators.revalidate),
through the same grounding checks the investigator's proposals pass (check_grounding).

Run: python -m pytest code/tests -q
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.agent import validators  # noqa: E402
from ghostledger.agent.tools import Toolbox  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, Config  # noqa: E402

_c = {}


def setup():
    if not _c:
        ds = load_dataset(os.path.join(REPO, "dataset"), "sample_deletion_requests.csv")
        _c["aud"] = Auditor(ds, Config())
        _c["reqs"] = {r["case_id"]: r for r in ds.requests}
    return _c["aud"], _c["reqs"]


def grounded(aud, req, skip=()):
    """(resource, evidence) the engine itself linked through an identifier sighting of the requester."""
    base = aud.audit(req)
    tb = Toolbox(aud, req, base)
    for rid in sorted(r for r in base.trace["states"] if not r.startswith(skip)):
        st = aud.states.resolve(rid, req["audit_at"])
        if st and any(s["owner"] == "requester" and s["evidence_id"] == st.evidence_id and not s["is_claim"]
                      for s in tb.resource_evidence(rid)["sightings"]):
            return rid, st.evidence_id
    raise AssertionError("no identifier-grounded resource")


class LinkFile:
    """Writes a links file and returns an Auditor that consumes it (sharing the built index)."""

    def __init__(self, aud, links):
        self.aud, self.links = aud, links

    def __enter__(self):
        self.d = tempfile.TemporaryDirectory()
        p = os.path.join(self.d.name, "links.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.links, f)
        return Auditor(self.aud.ds, Config(agent_links=p), index=self.aud.index)

    def __exit__(self, *a):
        self.d.cleanup()


class AgentLinkTrustBoundary(unittest.TestCase):
    def assert_rejected(self, cid, rid, ev):
        aud, reqs = setup()
        req = reqs[cid]
        self.assertIsNone(validators.revalidate(aud, req, rid, ev))
        with LinkFile(aud, {cid: [[rid, ev, "accepted"]]}) as a2:
            got = a2.audit(req)
        base = aud.audit(req)
        self.assertEqual((got.status, got.steps), (base.status, base.steps))
        self.assertEqual(got.trace["agent_links"], [(rid, ev, "rejected at consumption")])

    def test_reported_injection_case_03_search_001(self):
        # the Stage 3 repro: an existing resource and file id flipped case_03 COMPLETE -> INCOMPLETE
        self.assert_rejected("case_03", "search_001", "evidence_57")

    def test_01_resource_of_another_user(self):
        self.assert_rejected("case_03", "support_9553", "evidence_01")

    def test_02_correct_resource_unrelated_evidence(self):
        aud, reqs = setup()
        rid, ev = grounded(aud, reqs["case_03"])
        unrelated = next(f for f in sorted(aud.ds.manifest)
                         if f != ev and all(o.file_id != f for o in aud.index.by_resource[rid]))
        self.assert_rejected("case_03", rid, unrelated)

    def test_03_fabricated_resource(self):
        self.assert_rejected("case_03", "account_999999", "evidence_01")

    def test_04_fabricated_evidence(self):
        aud, reqs = setup()
        rid, _ = grounded(aud, reqs["case_03"])
        self.assert_rejected("case_03", rid, "evidence_9999")

    def test_05_post_audit_evidence(self):
        # audit_record_4902 is read with case_09's identifiers only by a capture after its audit_at
        self.assert_rejected("case_09", "audit_record_4902", "evidence_06")

    def test_06_timing_only_resource(self):
        # anonymized analytics record with no identifier: linked to case_03 only by a worker row
        self.assert_rejected("case_03", "analytics_9406", "evidence_94")

    def test_07_conflicting_ownership_identifiers(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        rid, ev = grounded(aud, req)
        a2 = Auditor(aud.ds, Config(), index=aud.index)
        users = {k: set(v) for k, v in validators.identifier_users(aud).items()}
        ident = next(i for o in aud.index.by_resource[rid] if o.file_id == ev for i in sorted(o.identifiers)
                     if i in a2.graph.subject(req["email"]).identifiers)
        users[ident] = users.get(ident, set()) | {"user_conflicting_owner"}
        a2._identifier_users = users              # the same identifier now also belongs to someone else
        self.assertIsNotNone(validators.revalidate(aud, req, rid, ev))
        self.assertIsNone(validators.revalidate(a2, req, rid, ev))

    def test_08_same_resource_competing_requests(self):
        aud, reqs = setup()
        rid, ev = grounded(aud, reqs["case_03"])
        other = reqs["case_07"]
        self.assertFalse(aud.graph.subject(other["email"]).user_ids & aud.graph.subject(reqs["case_03"]["email"]).user_ids)
        self.assertIsNotNone(validators.revalidate(aud, reqs["case_03"], rid, ev))
        self.assert_rejected("case_07", rid, ev)

    def test_09_grounded_link_is_accepted(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        rid, ev = grounded(aud, req)
        link = validators.revalidate(aud, req, rid, ev)
        self.assertIsNotNone(link)
        self.assertEqual((link.resource_id, link.source), (rid, ev))
        with LinkFile(aud, {"case_03": [[rid, ev, "accepted"]]}) as a2:
            got = a2.audit(req)
        self.assertEqual(got.trace["agent_links"], [(rid, ev, "accepted")])
        self.assertEqual(got.steps, aud.audit(req).steps)   # already linked: confirmation, no change

    def test_10_validated_link_altered_before_consumption(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        rid, ev = grounded(aud, req)
        self.assertIsNotNone(validators.revalidate(aud, req, rid, ev))
        self.assert_rejected("case_03", "search_001", ev)          # resource swapped
        self.assert_rejected("case_03", rid, "evidence_57")         # evidence swapped
        self.assert_rejected("case_07", rid, ev)                    # moved to another case

    def test_11_novel_grounded_resource_enters_the_right_case_end_to_end(self):
        # A resource the deterministic linker misses (simulated blind spot) but that a non-claim,
        # eligible reading ties to the requester's identifiers: proposed, validated, stored, consumed.
        aud, reqs = setup()
        req = reqs["case_03"]
        rid, ev = grounded(aud, req, skip=("account",))
        full = aud.audit(req)

        def blind(a):
            orig = a.case_links

            def case_links(r):
                subj, links, cutoff = orig(r)
                links.pop(rid, None)
                return subj, links, cutoff
            a.case_links = case_links
            return a

        a1 = blind(Auditor(aud.ds, Config(), index=aud.index))
        base = a1.audit(req)
        self.assertNotIn(rid, base.trace["states"])
        self.assertNotIn(rid, {s["resource_id"] for s in base.steps})
        tb = Toolbox(a1, req, base)
        tb.resource_evidence(rid)                                   # the investigator's own tool call
        ok, checks, reason, link = validators.validate_link(a1, req, base, {"resource_id": rid, "evidence_ids": [ev]}, tb)
        self.assertTrue(ok, reason)
        self.assertTrue(checks["novelty"])
        with LinkFile(aud, {"case_03": [[rid, ev, reason]], "case_07": [[rid, ev, reason]]}) as a2:
            a2 = blind(a2)
            got = a2.audit(req)
            other = a2.audit(reqs["case_07"])
        self.assertEqual(got.trace["agent_links"], [(rid, ev, "accepted")])
        mine = lambda r: [s for s in r.steps if s["resource_id"] == rid]
        self.assertTrue(mine(got))
        self.assertEqual((got.status, mine(got)), (full.status, mine(full)))   # same steps as when linked natively
        # the same entry filed under another subject's case does not enter it
        self.assertEqual(other.trace["agent_links"], [(rid, ev, "rejected at consumption")])
        self.assertEqual(other.steps, aud.audit(reqs["case_07"]).steps)

    def test_malformed_links_file_entries_are_ignored(self):
        aud, reqs = setup()
        with LinkFile(aud, {"case_03": [["search_001"], "x", None], "case_04": "bad"}) as a2:
            self.assertEqual(a2.audit(reqs["case_03"]).steps, aud.audit(reqs["case_03"]).steps)


if __name__ == "__main__":
    unittest.main()
