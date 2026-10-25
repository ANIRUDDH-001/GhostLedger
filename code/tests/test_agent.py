"""Investigator guardrails: deterministic fallback, the bounded tool loop, and proposal validation.

No network: the model is a scripted stub. Run: python -m pytest code/tests -q
"""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.agent import investigator, validators  # noqa: E402
from ghostledger.agent.llm import Client, ModelUnavailable  # noqa: E402
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


class Stub:
    """Scripted model: returns the queued replies in order; raises when it runs out."""
    provider, model = "stub", "stub"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages, schema=None, note=""):
        if not self.replies:
            raise ModelUnavailable("stub exhausted")
        r = self.replies.pop(0)
        return (r if isinstance(r, str) else json.dumps(r)), {"cached": False, "input_tokens": 1, "output_tokens": 1}


def grounded_resource(aud, req, base):
    """A resource the engine linked through an identifier sighting of the requester."""
    tb = Toolbox(aud, req, base)
    for rid in sorted(base.trace["states"]):
        ev = tb.resource_evidence(rid)
        if any(s["owner"] == "requester" for s in ev["sightings"]) and aud.states.resolve(rid, req["audit_at"]):
            return rid, tb
    raise AssertionError("no identifier-grounded resource in case")


class AgentGuardrails(unittest.TestCase):
    def test_no_credential_falls_back_without_network(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        with tempfile.TemporaryDirectory() as d:
            c = Client("gemini", "gemini-3.1-flash-lite", ledger_path=os.path.join(d, "l.jsonl"), offline=True)
            t = investigator.investigate(aud, req, aud.audit(req), c, "anything missing?")
        self.assertTrue(t["fallback"].startswith("model unavailable"))
        self.assertEqual(t["accepted_links"], [])

    def test_loop_runs_tools_then_validates(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        t = investigator.investigate(aud, req, aud.audit(req), Stub([
            {"action": "tool", "tool": "worker_rows", "args": {}},
            {"action": "final", "summary": "", "proposals": [{"type": "refusal", "resource_id": "", "claim": "timing only"}]}]),
            "q")
        self.assertIsNone(t["fallback"])
        self.assertEqual([x["tool"] for x in t["tool_calls"]], ["worker_rows"])
        self.assertEqual(t["accepted_links"], [])

    def test_tool_budget_is_enforced(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        many = [{"action": "tool", "tool": "case_summary", "args": {}}] * 20
        t = investigator.investigate(aud, req, aud.audit(req), Stub(many), "q", max_steps=3)
        self.assertLessEqual(len(t["tool_calls"]), 3)
        self.assertIsNotNone(t["fallback"])

    def test_unparseable_reply_falls_back(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        t = investigator.investigate(aud, req, aud.audit(req), Stub(["not json", "still not json"]), "q")
        self.assertEqual(t["fallback"], "unparseable model reply")

    def test_fabricated_ids_are_rejected(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        base = aud.audit(req)
        tb = Toolbox(aud, req, base)
        ok, checks, reason, _ = validators.validate_link(aud, req, base, {"resource_id": "account_999999",
                                                                          "evidence_ids": ["evidence_9999"]}, tb)
        self.assertFalse(ok)
        self.assertFalse(checks["provenance"])

    def test_ids_not_returned_by_a_tool_are_rejected(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        base = aud.audit(req)
        rid, _ = grounded_resource(aud, req, base)
        b2 = copy.deepcopy(base)
        del b2.trace["states"][rid]
        fresh = Toolbox(aud, req, b2)              # nothing looked up: the model could only have guessed the ids
        st = aud.states.resolve(rid, req["audit_at"])
        ok, checks, _, _ = validators.validate_link(aud, req, b2, {"resource_id": rid, "evidence_ids": [st.evidence_id]}, fresh)
        self.assertFalse(checks["provenance"])
        self.assertFalse(ok)

    def test_identifier_grounded_new_link_is_accepted(self):
        aud, reqs = setup()
        req = reqs["case_03"]
        base = aud.audit(req)
        rid, _ = grounded_resource(aud, req, base)
        b2 = copy.deepcopy(base)
        del b2.trace["states"][rid]                # pretend the engine missed it
        tb = Toolbox(aud, req, b2)
        tb.resource_evidence(rid)
        st = aud.states.resolve(rid, req["audit_at"])
        ok, checks, reason, link = validators.validate_link(aud, req, b2, {"resource_id": rid, "evidence_ids": [st.evidence_id]}, tb)
        self.assertTrue(ok, reason)
        self.assertEqual(link.resource_id, rid)

    def test_timing_only_identifier_free_record_is_rejected(self):
        aud, reqs = setup()
        req = reqs["case_03"]                      # analytics_9406: anonymized, no identifier, linked only by a worker row
        base = aud.audit(req)
        tb = Toolbox(aud, req, base)
        tb.worker_rows()
        tb.resource_evidence("analytics_9406")
        ok, checks, reason, _ = validators.validate_link(aud, req, base, {"resource_id": "analytics_9406",
                                                                          "evidence_ids": ["evidence_94"]}, tb)
        self.assertFalse(ok)
        self.assertFalse(checks["ownership"])

    def test_no_agent_links_leaves_output_unchanged(self):
        aud, reqs = setup()
        req = reqs["case_21"]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "links.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"case_21": [["analytics_does_not_exist", "evidence_92", "x"]]}, f)
            a2 = Auditor(aud.ds, Config(agent_links=p), index=aud.index)
            self.assertEqual(a2.audit(req).steps, aud.audit(req).steps)   # unknown ids are dropped on load


if __name__ == "__main__":
    unittest.main()
