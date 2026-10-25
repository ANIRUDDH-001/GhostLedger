"""Compare deletion-intent rules on labelled controls and on every participant request text.

    python code/evaluation/intent_compare.py --out .scratch/phase7/intent [--offline]

Rules: v1 (ghostledger.intent, the production regex), v2.1 (Phase 6 clause-scoped candidate, read from
.scratch when present), LLM (Groq openai/gpt-oss-120b, prompt int2) and combinations of them.
Controls: code/tests/fixtures/intent_controls_p7.json (frozen before any run), plus the Phase 6 sets
when present (those were used to tune v2.1 and are reported separately). AMBIG items are never errors.
FALSE_AUTH = a non-deletion text authorized; FALSE_REJECT = a deletion text rejected.
--offline replays cached model answers only (no network); a cache miss leaves that item unlabelled.
"""
import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger.agent import prompts  # noqa: E402
from ghostledger.agent.llm import Client, ModelUnavailable  # noqa: E402
from ghostledger.agent.investigator import parse  # noqa: E402
from ghostledger.intent import classify as v1  # noqa: E402
from ghostledger.safeio import atomic_write_text  # noqa: E402

MODEL = ("groq", "openai/gpt-oss-120b")
BATCH = 24


def load_v21():
    p = os.path.join(REPO, ".scratch", "phase6", "closure", "intent_v2.py")
    if not os.path.exists(p):
        return None
    spec = importlib.util.spec_from_file_location("intent_v21", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.classify


def controls():
    out = []
    with open(os.path.join(REPO, "code", "tests", "fixtures", "intent_controls_p7.json"), encoding="utf-8") as f:
        for iid, kind, lang, label, text in json.load(f)["items"]:
            out.append(("p7", iid, kind, lang, label, text))
    p6 = os.path.join(REPO, ".scratch", "phase6", "closure", "intent_controls.json")
    if os.path.exists(p6):
        with open(p6, encoding="utf-8") as f:
            d = json.load(f)
        for split in ("dev", "holdout", "holdout2"):
            for i, (lang, label, text) in enumerate(d.get(split, [])):
                out.append(("p6_" + split, f"{split}_{i:02d}", "", lang, label, text))
    return out


def requests():
    out = []
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        with open(os.path.join(REPO, "dataset", name), encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                out.append(("eval" if name.startswith("deletion") else "sample", r["case_id"], r["request_text"]))
    return out


def llm_labels(texts, offline):
    """{text: (label, cue)} for distinct texts, batched; cached by the client."""
    client = Client(*MODEL, offline=offline)
    uniq = sorted(set(texts))
    out, usage = {}, Counter()
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        ids = {f"m{j:02d}": t for j, t in enumerate(chunk)}
        payload = "\n".join(json.dumps({"id": k, "message": t}, ensure_ascii=False) for k, t in ids.items())
        try:
            text, meta = client.complete(prompts.INTENT_SYSTEM, [{"role": "user", "content": payload}],
                                         schema=prompts.INTENT_SCHEMA, note=f"intent {prompts.INTENT_VERSION} batch {i // BATCH}")
        except ModelUnavailable as e:
            usage["unavailable"] += 1
            print("model unavailable:", e)
            continue
        usage["calls"] += 1
        usage["cached"] += meta["cached"]
        usage["input_tokens"] += meta["input_tokens"]
        usage["output_tokens"] += meta["output_tokens"]
        for it in (parse(text) or {}).get("items", []):
            if it.get("id") in ids:
                out[ids[it["id"]]] = (it.get("label"), it.get("cue", ""))
    return out, dict(usage)


def rules(v21):
    def llm(t, L):
        return L.get(t, (None,))[0]
    R = {
        "v1": lambda t, L: v1(t)[0],
        "LLM": lambda t, L: None if llm(t, L) is None else llm(t, L) == "DELETE",
        "AND(v1,LLM)": lambda t, L: v1(t)[0] and llm(t, L) == "DELETE",
        "OR(v1,LLM)": lambda t, L: v1(t)[0] or llm(t, L) == "DELETE",
        # LLM decides; a v1 explicit keep cue still vetoes; LLM AMBIGUOUS falls back to v1
        "LLM+v1keep": lambda t, L: (v1(t)[0] if llm(t, L) in (None, "AMBIGUOUS")
                                    else llm(t, L) == "DELETE" and not v1(t)[1].startswith("keep")),
    }
    if v21:
        R["v2.1"] = lambda t, L: v21(t)[0]
        R["AND(v2.1,LLM)"] = lambda t, L: v21(t)[0] and llm(t, L) == "DELETE"
        # agreement decides; on disagreement the regex with clause scoping decides (no NA merely from disagreement)
        R["v2.1|LLM-agree"] = lambda t, L: v21(t)[0] if (llm(t, L) in (None, "AMBIGUOUS") or (llm(t, L) == "DELETE") != v21(t)[0]) else llm(t, L) == "DELETE"
        R["OR(v2.1,LLM)"] = lambda t, L: v21(t)[0] or llm(t, L) == "DELETE"
    return R


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    v21 = load_v21()
    C, Q = controls(), requests()
    L, usage = llm_labels([c[5] for c in C] + [q[2] for q in Q], a.offline)
    R = rules(v21)
    score, fails = {}, []
    for rname, f in R.items():
        for c in C:
            src, iid, kind, lang, label, text = c
            pred = f(text, L)
            key = (rname, src)
            s = score.setdefault(key, Counter())
            if label == "AMBIG" or label not in ("DELETE", "NA"):
                s["ambig_auth" if pred else "ambig_reject"] += 1
                continue
            if pred is None:
                s["unlabelled"] += 1
            elif label == "DELETE" and not pred:
                s["FALSE_REJECT"] += 1
                fails.append({"rule": rname, "set": src, "id": iid, "kind": kind, "lang": lang, "gold": label,
                              "text": text, "llm": L.get(text)})
            elif label == "NA" and pred:
                s["FALSE_AUTH"] += 1
                fails.append({"rule": rname, "set": src, "id": iid, "kind": kind, "lang": lang, "gold": label,
                              "text": text, "llm": L.get(text)})
            else:
                s["ok"] += 1
    # behaviour on participant texts: how many authorizations each rule changes relative to v1
    req = {}
    for rname, f in R.items():
        flips = [(s, c) for s, c, t in Q if bool(f(t, L)) != v1(t)[0]]
        req[rname] = {"authorized": sum(1 for s, c, t in Q if s == "eval" and f(t, L)),
                      "flips_vs_v1": [f"{s}:{c}" for s, c in flips]}
    disagree = [{"set": s, "case_id": c, "text": t, "v1": v1(t), "v2.1": v21(t) if v21 else None, "llm": L.get(t)}
                for s, c, t in Q if L.get(t) and (L[t][0] == "DELETE") != v1(t)[0]]
    rep = {"model": "/".join(MODEL), "prompt": prompts.INTENT_VERSION, "usage": usage,
           "scores": {f"{r}|{s}": dict(v) for (r, s), v in sorted(score.items())},
           "requests": req, "llm_vs_v1_disagreements_on_requests": disagree, "control_failures": fails}
    os.makedirs(a.out, exist_ok=True)
    atomic_write_text(os.path.join(a.out, "intent_compare.json"), json.dumps(rep, indent=1, ensure_ascii=False, default=str) + "\n")
    sets = sorted({s for _, s in score})
    print("rule".ljust(16) + "".join(f"{s:>26}" for s in sets) + "   eval_auth flips")
    for rname in R:
        cells = []
        for s in sets:
            v = score.get((rname, s), {})
            cells.append(f"FA {v.get('FALSE_AUTH', 0):>2} FR {v.get('FALSE_REJECT', 0):>2} amb+{v.get('ambig_auth', 0)}".rjust(26))
        print(rname.ljust(16) + "".join(cells) + f"   {req[rname]['authorized']:>4} {len(req[rname]['flips_vs_v1']):>4}")
    print("usage", usage, "| request disagreements LLM vs v1:", len(disagree))


if __name__ == "__main__":
    main()
