"""Sample evaluation harness for GhostLedger.

    python code/evaluation/evaluate.py                    # metrics + ablations, writes .scratch/eval/*
    python code/evaluation/evaluate.py --cases case_07,case_12 --show

Parses predicted and gold steps_json as JSON (never string-compares rows) and reports:
case-status accuracy, resource precision/recall, exact step / case accuracy, field accuracy,
evidence accuracy, sequence correctness, false-positive attribution, identity reachability,
citation agreement and a cause for every gold resource the pipeline does not link.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.evidence import EvidenceIndex, prefix_of  # noqa: E402
from ghostledger.io import load_dataset, read_csv  # noqa: E402
from ghostledger.pipeline import Auditor, Config  # noqa: E402
from ghostledger.state import eligible  # noqa: E402
from ghostledger.steps import sort_key  # noqa: E402

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load_rows(path):
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in read_csv(path)}


def key(s):
    return (s["phase"], s["resource_id"], s["operation"])


def lcs(a, b):
    m = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            m[i + 1][j + 1] = m[i][j] + 1 if a[i] == b[j] else max(m[i][j + 1], m[i + 1][j])
    return m[-1][-1]


def score(pred, gold):
    c = Counter()
    per_case = {}
    for cid, (gs, gsteps) in sorted(gold.items()):
        ps, psteps = pred.get(cid, ("<missing>", []))
        c["cases"] += 1
        c["status_ok"] += ps == gs
        c["case_exact"] += ps == gs and psteps == gsteps
        gres = {s["resource_id"] for s in gsteps}
        pres = {s["resource_id"] for s in psteps}
        c["res_tp"] += len(gres & pres)
        c["res_fp"] += len(pres - gres)
        c["res_fn"] += len(gres - pres)
        c["gold_steps"] += len(gsteps)
        c["pred_steps"] += len(psteps)
        c["pos_exact"] += sum(1 for a, b in zip(psteps, gsteps) if a == b)
        gset = Counter(json.dumps({k: v for k, v in s.items() if k != "step_no"}, sort_keys=True) for s in gsteps)
        pset = Counter(json.dumps({k: v for k, v in s.items() if k != "step_no"}, sort_keys=True) for s in psteps)
        c["content_tp"] += sum((gset & pset).values())
        pk = {key(s): s for s in psteps}
        for g in gsteps:
            p = pk.get(key(g))
            if p:
                c["aligned"] += 1
                for f in FIELDS:
                    c["field_" + f] += p[f] == g[f]
                if g["evidence_id"]:
                    c["ev_gold_nonempty"] += 1
                    c["ev_ok_nonempty"] += p["evidence_id"] == g["evidence_id"]
            if g["evidence_id"]:
                c["ev_gold_total"] += 1
                c["ev_ok_total"] += bool(p) and p["evidence_id"] == g["evidence_id"]
        canon = psteps == sorted(psteps, key=sort_key) and [s["step_no"] for s in psteps] == list(range(1, len(psteps) + 1))
        c["seq_canonical"] += canon
        common_g = [key(s) for s in gsteps if key(s) in pk]
        common_p = [key(s) for s in psteps if key(s) in {key(g) for g in gsteps}]
        c["seq_order_ok"] += lcs(common_g, common_p) == len(common_g)
        per_case[cid] = dict(gold_status=gs, pred_status=ps, missing=sorted(gres - pres), extra=sorted(pres - gres),
                             exact=ps == gs and psteps == gsteps)
    m = {
        "case_status_accuracy": c["status_ok"] / c["cases"],
        "case_exact_accuracy": c["case_exact"] / c["cases"],
        "resource_precision": c["res_tp"] / max(1, c["res_tp"] + c["res_fp"]),
        "resource_recall": c["res_tp"] / max(1, c["res_tp"] + c["res_fn"]),
        "step_exact_positional": c["pos_exact"] / c["gold_steps"],
        "step_content_recall": c["content_tp"] / c["gold_steps"],
        "step_content_precision": c["content_tp"] / max(1, c["pred_steps"]),
        "evidence_accuracy_aligned": c["ev_ok_nonempty"] / max(1, c["ev_gold_nonempty"]),
        "evidence_accuracy_all_cited": c["ev_ok_total"] / max(1, c["ev_gold_total"]),
        "sequence_canonical": c["seq_canonical"] / c["cases"],
        "sequence_order_vs_gold": c["seq_order_ok"] / c["cases"],
        "false_positive_resources": c["res_fp"],
    }
    for f in FIELDS:
        m["field_" + f] = c["field_" + f] / max(1, c["aligned"])
    return m, c, per_case


def reachability(auditor, reqs, gold):
    """Gold resources reachable through supported identity links under each horizon, and
    whether the state engine's citation for reached cited resources equals the gold citation."""
    out = {}
    for horizon in ("audit", "any"):
        tot = hit = cite_tot = cite_ok = 0
        rows = []
        for r in reqs:
            subj = auditor.graph.subject(r["email"])
            hz = None if horizon == "any" else (lambda o, a=r["audit_at"]: eligible(o, a))
            links = auditor.graph.links(subj, hz)
            for s in gold[r["case_id"]][1]:
                pass
            gres = {}
            for s in gold[r["case_id"]][1]:
                gres.setdefault(s["resource_id"], s["evidence_id"] or gres.get(s["resource_id"], ""))
            for rid, gev in sorted(gres.items()):
                tot += 1
                reached = rid in links
                hit += reached
                st = auditor.states.resolve(rid, r["audit_at"]) if reached else None
                if gev:
                    cite_tot += 1
                    cite_ok += bool(st) and st.evidence_id == gev
                rows.append(dict(case_id=r["case_id"], resource_id=rid, reached=reached,
                                 via=sorted({l.source for l in links.get(rid, [])}), gold_evidence=gev,
                                 our_evidence=st.evidence_id if st else "", our_state=st.state if st else ""))
        out[horizon] = dict(reached=hit, total=tot, cited_ok=cite_ok, cited_total=cite_tot, rows=rows)
    # temporal engine in isolation: given the gold resource, does the latest eligible sighting
    # (or scoped absence when there is none) name the gold evidence file?
    ok = tot = 0
    rows = []
    for r in reqs:
        for s in gold[r["case_id"]][1]:
            if not s["evidence_id"]:
                continue
            tot += 1
            st = auditor.states.resolve(s["resource_id"], r["audit_at"]) or auditor.states.scoped_absence(s["resource_id"], r["audit_at"])
            ev = st.evidence_id if st else ""
            ok += ev == s["evidence_id"]
            if ev != s["evidence_id"]:
                rows.append(dict(case_id=r["case_id"], resource_id=s["resource_id"], gold=s["evidence_id"], ours=ev))
    out["oracle_resources"] = dict(reached=tot, total=tot, cited_ok=ok, cited_total=tot, rows=rows)
    return out


def diagnose_missing(auditor, ds, reqs, gold):
    """Cause for each gold resource with no supported link."""
    raw = {}
    for fid, m in ds.manifest.items():
        p = ds.path(m["file_path"])
        if p.endswith((".sql", ".json", ".log", ".txt")):
            with open(p, encoding="utf-8") as f:
                raw[fid] = f.read()
    acts = defaultdict(list)
    for a in ds.activity:
        acts[a["resource_id"]].append(a)
    all_reqs = read_csv(ds.path("sample_deletion_requests.csv")) + read_csv(ds.path("deletion_requests.csv"))
    rows = []
    for r in reqs:
        subj = auditor.graph.subject(r["email"])
        links = auditor.graph.links(subj)
        for rid in sorted({s["resource_id"] for s in gold[r["case_id"]][1]} - set(links)):
            obs = auditor.index.by_resource.get(rid, [])
            in_raw = sorted(f for f, t in raw.items() if re.search(r"\b%s\b" % re.escape(rid), t))
            parsed_files = sorted({o.file_id for o in obs})
            competing = 0
            win = []
            for a in acts.get(rid, []):
                if a["source_system"] == "deletion_worker":
                    rivals = [q["case_id"] for q in all_reqs if q["requested_at"] <= a["event_time"] <= q["reported_at"]]
                    win.append(a["activity_id"])
                    competing = max(competing, len(rivals))
            if not obs and not in_raw and not acts.get(rid):
                cause = "absent: resource appears in no participant-visible file or activity row"
            elif in_raw and set(in_raw) - set(parsed_files):
                cause = "parser gap: id present in raw text of " + ",".join(sorted(set(in_raw) - set(parsed_files)))
            elif obs and not any(o.identifiers for o in obs):
                cause = (f"ambiguous attribution: sighted in {','.join(parsed_files)} with no subject identifier; "
                         f"only link is workflow activity {','.join(win) or '-'} whose window overlaps {competing} requests")
            elif not obs:
                cause = (f"ambiguous attribution: no evidence sighting; workflow activity {','.join(win) or '-'} "
                         f"overlaps {competing} requests")
            else:
                cause = "unlinked: sighted with identifiers that do not match the subject"
            rows.append(dict(case_id=r["case_id"], resource_id=rid, cause=cause, sighted_in=parsed_files))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    p.add_argument("--out-dir", default=os.path.join(REPO, ".scratch", "eval"))
    p.add_argument("--cases", default="")
    p.add_argument("--show", action="store_true")
    a = p.parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)
    ds = load_dataset(a.dataset, "sample_deletion_requests.csv")
    gold = load_rows(os.path.join(a.dataset, "sample_output.csv"))
    index = EvidenceIndex(ds).build()
    variants = {
        "default": Config(),
        "horizon=audit": Config(identity_horizon="audit"),
        "all_accounts": Config(one_primary_account=False),
        "drop_stale_absent": Config(drop_stale_absent=True),
        "with_candidates(diagnostic)": Config(use_candidates=True),
        "worker_attribution(exp)": Config(worker_attribution=True),
    }
    report = {}
    for name, cfg in variants.items():
        aud = Auditor(ds, cfg, index)
        res = aud.run()
        pred = {r.case_id: (r.status, r.steps) for r in res}
        m, c, per_case = score(pred, gold)
        report[name] = m
        if name == "default":
            default_aud, default_res, default_cases = aud, res, per_case
    reqs = sorted(ds.requests, key=lambda r: r["case_id"])
    reach = reachability(default_aud, reqs, gold)
    missing = diagnose_missing(default_aud, ds, reqs, gold)
    fp = []
    for cid, d in default_cases.items():
        tr = next(r.trace for r in default_res if r.case_id == cid)
        for rid in d["extra"]:
            fp.append(dict(case_id=cid, resource_id=rid, via=tr["links"].get(rid), state=tr["states"].get(rid)))
    out = dict(metrics=report, reachability={k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in reach.items()},
               per_case=default_cases, missing=missing, false_positives=fp)
    with open(os.path.join(a.out_dir, "sample_eval.json"), "w", encoding="utf-8") as f:
        json.dump(dict(out, reach_rows=reach), f, indent=1, ensure_ascii=False)
    # console summary
    names = list(report)
    print("metric".ljust(30) + "".join(n[:16].rjust(17) for n in names))
    for k in report["default"]:
        print(k.ljust(30) + "".join((f"{report[n][k]:.3f}" if isinstance(report[n][k], float) else str(report[n][k])).rjust(17) for n in names))
    for h, v in reach.items():
        print(f"reachability[{h}] {v['reached']}/{v['total']}  citation agreement {v['cited_ok']}/{v['cited_total']}")
    print("cases not exact:", [c for c, d in default_cases.items() if not d["exact"]])
    print("missing-resource causes:", Counter(m["cause"].split(":")[0] for m in missing))
    if a.show or a.cases:
        sel = set(a.cases.split(",")) if a.cases else set(default_cases)
        for r in default_res:
            if r.case_id not in sel:
                continue
            d = default_cases[r.case_id]
            print(f"\n## {r.case_id} gold={d['gold_status']} pred={d['pred_status']} missing={d['missing']} extra={d['extra']}")
            gk = {key(s): s for s in gold[r.case_id][1]}
            pk = {key(s): s for s in r.steps}
            for k in sorted(set(gk) | set(pk), key=lambda k: sort_key(dict(phase=k[0], resource_id=k[1], operation=k[2], system_id=(gk.get(k) or pk.get(k))["system_id"]))):
                g, pp = gk.get(k), pk.get(k)
                mark = "==" if g and pp and g == {**pp, "step_no": g["step_no"]} else ("G " if not pp else ("P " if not g else "~~"))
                print(f"  {mark} {k[0][:3]} {k[2]:26} {k[1]:20} gold_ev={(g or {}).get('evidence_id', '-'):12} pred_ev={(pp or {}).get('evidence_id', '-')}")
            for rid, why in r.trace.get("dropped", {}).items():
                print(f"     dropped {rid}: {why}")
    return out


if __name__ == "__main__":
    main()
