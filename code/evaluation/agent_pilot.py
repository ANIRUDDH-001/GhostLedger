"""Investigator proof-of-value pilot over the frozen questions in pilot_questions.json.

    python code/evaluation/agent_pilot.py --out .scratch/phase7/pilot/run1 [--offline] [--model gemini/gemini-3.1-flash-lite]

Writes traces.jsonl (one investigation per line), accepted_links.json (validated links, the only thing
that can reach the engine via Config(agent_links=...)) and summary.json. Sample questions are scored
against sample_output.csv here, at evaluation time; gold never enters a prompt or a tool.
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.agent.investigator import investigate  # noqa: E402
from ghostledger.agent.llm import Client  # noqa: E402
from ghostledger.agent.validators import known_id  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, Config  # noqa: E402
from ghostledger.safeio import atomic_write_text  # noqa: E402

FILES = {"sample": "sample_deletion_requests.csv", "eval": "deletion_requests.csv"}


def gold_resources():
    with open(os.path.join(REPO, "dataset", "sample_output.csv"), encoding="utf-8", newline="") as f:
        return {r["case_id"]: {s["resource_id"] for s in json.loads(r["steps_json"])} for r in csv.DictReader(f)}


def score(q, t, auditor, gold):
    props = t["proposals"]
    ids = [p.get("resource_id") for p in props] + [e for p in props for e in (p.get("evidence_ids") or [])] \
        + [p.get("activity_id") for p in props if p.get("activity_id")]
    fabricated = sorted({str(i) for i in ids if i and not known_id(auditor, str(i))})
    links = [p for p in props if p.get("type") == "link"]
    s = {"id": q["id"], "case_id": q["case_id"], "kind": q["kind"], "fallback": t["fallback"],
         "tool_calls": len(t.get("tool_calls", [])), "proposals": Counter(p.get("type") for p in props),
         "link_proposals": len(links), "accepted": len(t["accepted_links"]), "fabricated_ids": fabricated,
         "rejections": [v["reason"] for v in t["validations"] if v["type"] == "link" and not v["ok"]],
         "refused": any(p.get("type") == "refusal" for p in props) or (not links and t["fallback"] is None)}
    if q["targets"] is not None:
        seen, mentioned = set(t.get("seen_ids", [])), {p.get("resource_id") for p in props}
        s["targets_retrieved"] = [x for x in q["targets"] if x in seen]
        s["targets_mentioned"] = [x for x in q["targets"] if x in mentioned]
        g = gold.get(q["case_id"], set())
        s["accepted_correct"] = sum(1 for l in t["accepted_links"] if l[0] in g)
        s["accepted_wrong"] = sum(1 for l in t["accepted_links"] if l[0] not in g)
    return s


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--offline", action="store_true", help="cache replay only; no network")
    ap.add_argument("--model", default="gemini/gemini-3.1-flash-lite")
    ap.add_argument("--only", default="", help="comma-separated question ids")
    a = ap.parse_args(argv)
    with open(os.path.join(HERE, "pilot_questions.json"), encoding="utf-8") as f:
        qs = json.load(f)["questions"]
    if a.only:
        qs = [q for q in qs if q["id"] in a.only.split(",")]
    provider, model = a.model.split("/", 1)
    client = Client(provider, model, offline=a.offline)
    gold = gold_resources()
    auditors, traces, scores, accepted = {}, [], [], {}
    for q in qs:
        if q["file"] not in auditors:
            ds = load_dataset(os.path.join(REPO, "dataset"), FILES[q["file"]])
            aud = Auditor(ds, Config())
            auditors[q["file"]] = (aud, {r["case_id"]: r for r in ds.requests})
        aud, reqs = auditors[q["file"]]
        req = reqs[q["case_id"]]
        base = aud.audit(req)
        t = investigate(aud, req, base, client, q["question"])
        t["question_id"] = q["id"]
        traces.append(t)
        s = score(q, t, aud, gold)
        scores.append(s)
        for l in t["accepted_links"]:
            accepted.setdefault(f"{q['file']}:{q['case_id']}", []).append(l)
        print(json.dumps({k: v for k, v in s.items() if k not in ("rejections",)}, default=str))
    tot = Counter()
    for t in traces:
        for k, v in t["usage"].items():
            tot[k] += v
    summary = {"model": a.model, "questions": len(qs), "usage": dict(tot), "scores": scores,
               "accepted_links": accepted,
               "totals": {"fallbacks": sum(1 for s in scores if s["fallback"]),
                          "link_proposals": sum(s["link_proposals"] for s in scores),
                          "accepted": sum(s["accepted"] for s in scores),
                          "accepted_wrong": sum(s.get("accepted_wrong", 0) for s in scores),
                          "fabricated": sum(len(s["fabricated_ids"]) for s in scores),
                          "targets": sum(len(q["targets"] or []) for q in qs),
                          "targets_retrieved": sum(len(s.get("targets_retrieved", [])) for s in scores),
                          "targets_mentioned": sum(len(s.get("targets_mentioned", [])) for s in scores)}}
    os.makedirs(a.out, exist_ok=True)
    atomic_write_text(os.path.join(a.out, "traces.jsonl"),
                      "\n".join(json.dumps(t, ensure_ascii=False, sort_keys=True, default=str) for t in traces) + "\n")
    atomic_write_text(os.path.join(a.out, "accepted_links.json"), json.dumps(accepted, indent=1, sort_keys=True) + "\n")
    atomic_write_text(os.path.join(a.out, "summary.json"), json.dumps(summary, indent=1, default=str) + "\n")
    print(json.dumps(summary["totals"]), json.dumps(summary["usage"]))


if __name__ == "__main__":
    main()
