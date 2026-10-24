"""Experimental (Phase 3, H2): attribute deletion-worker account rows to requests by global assignment.

A deletion-worker DELETE_RECORD row on account_* is a candidate for a request when it falls inside
[max(requested_at, reported_at - LEAD), reported_at + SLACK] and its reported_result is compatible
with the request's reported_status. Rows whose resource is identifier-linked to another request are
excluded, and requests that already have exactly one identifier-linked candidate row are settled.
An edge is accepted only if it belongs to every maximum matching of the remaining bipartite graph
(Dulmage-Mendelsohn: matched edge not on an alternating cycle and not reachable from a free vertex),
so an ambiguous row is never assigned. Off by default; enabled with Config(worker_attribution=True).
"""
import glob
import os
from collections import defaultdict
from datetime import datetime, timedelta

from .intent import classify
from .io import read_csv

LEAD = timedelta(minutes=80)
SLACK = timedelta(minutes=10)
COMPAT = {"COMPLETE": {"SUCCESS"}, "NOT_STARTED": {"FAILED"}, "PARTIALLY_COMPLETE": {"PARTIAL", "SUCCESS"}}


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def request_pool(root):
    """Every participant-facing request file in the dataset; worker rows are shared across them."""
    seen = {}
    for path in sorted(glob.glob(os.path.join(root, "*deletion_requests.csv"))):
        for r in read_csv(path):
            seen.setdefault(r["case_id"], r)
    return list(seen.values())


def edges(dataset, graph, pool):
    reqs = [r for r in pool if classify(r["request_text"])[0]]
    rows = {a["activity_id"]: a for a in dataset.activity
            if a["source_system"] == "deletion_worker" and a["resource_id"].startswith("account_")
            and a["event_type"] == "DELETE_RECORD"}
    owners = defaultdict(set)
    for r in reqs:
        for rid in graph.links(graph.subject(r["email"])):
            owners[rid].add(r["case_id"])
    cand = defaultdict(set)
    for aid, a in rows.items():
        t = _t(a["event_time"])
        for r in reqs:
            lo = max(_t(r["requested_at"]), _t(r["reported_at"]) - LEAD)
            if lo <= t <= _t(r["reported_at"]) + SLACK and a["reported_result"] in COMPAT.get(r["reported_status"], ()):
                cand[r["case_id"]].add(aid)
    settled = {}
    for cid, es in cand.items():
        own = [aid for aid in es if cid in owners.get(rows[aid]["resource_id"], ())]
        if len(own) == 1:
            settled[cid] = own[0]
    taken = set(settled.values())
    open_ = {cid: {aid for aid in es if aid not in taken and not (owners.get(rows[aid]["resource_id"], set()) - {cid})}
             for cid, es in cand.items() if cid not in settled}
    return rows, open_


def _max_matching(E):
    match_r = {}

    def augment(c, seen):
        for a in sorted(E[c]):
            if a in seen:
                continue
            seen.add(a)
            if a not in match_r or augment(match_r[a], seen):
                match_r[a] = c
                return True
        return False

    for c in sorted(E):
        augment(c, set())
    return {c: a for a, c in match_r.items()}


def _sccs(nodes, adj):
    index, low, on, stack, comp, out, n = {}, {}, set(), [], {}, 0, [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(adj.get(root, ())))]
        index[root] = low[root] = n[0]; n[0] += 1; stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = n[0]; n[0] += 1; stack.append(w); on.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                while True:
                    w = stack.pop(); on.discard(w); comp[w] = out
                    if w == v:
                        break
                out += 1
    return comp


def forced_edges(E):
    """Edges contained in every maximum matching of the bipartite graph E: request -> {row}."""
    M = _max_matching(E)
    adj = defaultdict(set)              # unmatched edges request->row, matched edges row->request
    for c, rs in E.items():
        for a in rs:
            if M.get(c) == a:
                adj[("r", a)].add(("c", c))
            else:
                adj[("c", c)].add(("r", a))
    rev = defaultdict(set)
    for v, ws in adj.items():
        for w in ws:
            rev[w].add(v)
    matched_rows = set(M.values())
    free = [("c", c) for c in E if c not in M] + [("r", a) for rs in E.values() for a in rs if a not in matched_rows]

    def reach(starts, g):
        seen, todo = set(starts), list(starts)
        while todo:
            for w in g.get(todo.pop(), ()):
                if w not in seen:
                    seen.add(w); todo.append(w)
        return seen

    from_free_c = reach([v for v in free if v[0] == "c"], adj)   # even alternating paths from free requests
    to_free_r = reach([v for v in free if v[0] == "r"], rev)     # even alternating paths to free rows
    nodes = sorted(set(adj) | set(rev))
    comp = _sccs(nodes, adj)
    return {c: a for c, a in M.items()
            if comp[("c", c)] != comp[("r", a)] and ("r", a) not in from_free_c and ("c", c) not in to_free_r}


def attribute(dataset, graph, pool):
    """case_id -> (resource_id, activity_id) for unambiguously attributed primary-account rows."""
    rows, open_ = edges(dataset, graph, pool)
    return {c: (rows[a]["resource_id"], a) for c, a in forced_edges(open_).items()}
