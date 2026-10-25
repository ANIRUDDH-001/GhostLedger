"""Per-case trace dumps and run-to-run diffs: every change to output.csv has to name its mechanism.

    python code/evaluation/trace.py dump --out .scratch/phase7/baseline            # eval set, default config
    python code/evaluation/trace.py dump --requests sample_deletion_requests.csv --out .scratch/phase7/s_base
    python code/evaluation/trace.py diff .scratch/phase7/baseline .scratch/phase7/stage2

dump writes <out>/output.csv and <out>/traces.jsonl (one object per case: status, steps and the
auditor trace: intent, user ids, links with their sources, attribution, state per resource with its
evidence and basis, policy decision, and every dropped resource with its reason).
diff reports status flips and the added/removed steps of each changed case, with the trace fields
that explain them (attribution, link sources, state basis, drop reason).
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger import contract  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, Config, write_output  # noqa: E402
from ghostledger.safeio import atomic_write_text  # noqa: E402


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in (sorted(x) if isinstance(x, (set, frozenset)) else x)]
    return x if isinstance(x, (str, int, float, bool)) or x is None else str(x)


def dump(dataset, requests, out, cfg):
    ds = load_dataset(dataset, requests)
    auditor = Auditor(ds, cfg)
    results = auditor.run()
    os.makedirs(out, exist_ok=True)
    write_output(results, os.path.join(out, "output.csv"), gate=contract.gate(ds.requests, contract.corpus(ds, auditor.index)),
                 order=[r["case_id"] for r in ds.requests])
    lines = [json.dumps({"case_id": r.case_id, "status": r.status, "steps": r.steps, "trace": jsonable(r.trace)},
                        ensure_ascii=False, sort_keys=True) for r in results]
    atomic_write_text(os.path.join(out, "traces.jsonl"), "\n".join(lines) + "\n")
    atomic_write_text(os.path.join(out, "config.json"), json.dumps(jsonable(vars(cfg)), indent=1) + "\n")
    return results


def load(run):
    with open(os.path.join(run, "traces.jsonl"), encoding="utf-8") as f:
        return {d["case_id"]: d for d in map(json.loads, f) if d}


def step_key(s):
    return (s["phase"], s["resource_id"], s["operation"], s["evidence_id"])


def explain(rid, t):
    """Trace facts about one resource in one run."""
    bits = []
    if (t.get("attributed") or [None])[0] == rid:
        bits.append(f"attributed via {t['attributed'][1]}")
    if rid in t.get("links", {}):
        bits.append("links=" + ",".join(sorted(set(t["links"][rid]))))
    if rid in t.get("states", {}):
        state, _, ev, basis = t["states"][rid]
        bits.append(f"state={state}@{ev or '-'} ({basis})")
    if rid in t.get("dropped", {}):
        bits.append("dropped: " + t["dropped"][rid])
    return "; ".join(bits) or "not linked"


def diff(a_dir, b_dir, show=True):
    a, b = load(a_dir), load(b_dir)
    summary = Counter()
    changed = []
    for cid in sorted(set(a) | set(b), key=lambda c: (len(c), c)):
        x, y = a.get(cid), b.get(cid)
        if x is None or y is None:
            summary["case_only_in_" + ("b" if x is None else "a")] += 1
            continue
        xs, ys = {step_key(s) for s in x["steps"]}, {step_key(s) for s in y["steps"]}
        if x["status"] == y["status"] and x["steps"] == y["steps"]:
            continue
        added, removed = sorted(ys - xs), sorted(xs - ys)
        summary["cases_changed"] += 1
        summary["steps_added"] += len(added)
        summary["steps_removed"] += len(removed)
        if x["status"] != y["status"]:
            summary[f"status {x['status']}->{y['status']}"] += 1
        if not added and not removed:
            summary["reordered_or_renumbered"] += 1
        changed.append((cid, x, y, added, removed))
    if show:
        for cid, x, y, added, removed in changed:
            flip = f" {x['status']}->{y['status']}" if x["status"] != y["status"] else f" {y['status']}"
            print(f"{cid}{flip}")
            for tag, steps, t in (("+", added, y["trace"]), ("-", removed, x["trace"])):
                for ph, rid, op, ev in steps:
                    print(f"  {tag} {ph} {op} {rid} ev={ev or '-'} | {explain(rid, t)}")
        print(json.dumps(dict(summary), indent=1, sort_keys=True))
    return summary, changed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    d.add_argument("--requests", default="deletion_requests.csv")
    d.add_argument("--out", required=True)
    d.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                   help="override a Config field, e.g. --set worker_attribution=false")
    f = sub.add_parser("diff")
    f.add_argument("a")
    f.add_argument("b")
    a = p.parse_args(argv)
    if a.cmd == "diff":
        diff(a.a, a.b)
        return
    cfg = Config()
    for kv in a.set:
        k, _, v = kv.partition("=")
        if not hasattr(cfg, k):
            sys.exit(f"unknown Config field {k!r}")
        cur = getattr(cfg, k)
        setattr(cfg, k, v.lower() in ("1", "true", "yes") if isinstance(cur, bool) else v)
    results = dump(a.dataset, a.requests, a.out, cfg)
    print(f"dumped {len(results)} cases to {a.out} {dict(Counter(r.status for r in results))}")


if __name__ == "__main__":
    main()
