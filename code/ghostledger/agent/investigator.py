"""Bounded tool loop. One investigation = one question about one case: the model calls read-only tools
(tools.Toolbox) for at most MAX_STEPS turns, then returns proposals that validators.py checks.

Fallback: no credential, a provider failure, an unparseable reply or an exhausted budget ends the
investigation with no accepted proposals; the deterministic result stands unchanged.
"""
import json
import re

from . import prompts, validators
from .llm import ModelUnavailable
from .tools import Toolbox

MAX_STEPS = 6
MAX_RESULT_CHARS = 3500


def parse(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        j = json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        try:
            j = json.loads(m.group(0))
        except ValueError:
            return None
    return j if isinstance(j, dict) else None


def investigate(auditor, req, baseline, client, question, max_steps=MAX_STEPS):
    """Return a trace dict: tool calls, model turns, proposals, validation of each, accepted links."""
    tools = Toolbox(auditor, req, baseline)
    system = prompts.INVESTIGATOR_SYSTEM.format(
        tools="\n".join(f"  {sig}: {desc}" for sig, desc in Toolbox.TOOLS.values()), max_steps=max_steps)
    messages = [{"role": "user", "content": f"Case {req['case_id']}. Question: {question}"}]
    trace = {"case_id": req["case_id"], "question": question, "prompt_version": prompts.INVESTIGATOR_VERSION,
             "model": f"{client.provider}/{client.model}", "turns": [], "proposals": [], "validations": [],
             "accepted_links": [], "usage": {"calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0},
             "fallback": None}
    final, steps, bad = None, 0, 0
    while final is None:
        if steps >= max_steps:
            messages.append({"role": "user", "content": "Tool budget exhausted. Reply now with the final JSON object."})
        try:
            text, meta = client.complete(system, messages, schema={"type": "object"},
                                         note=f"{req['case_id']} turn {len(trace['turns'])}")
        except ModelUnavailable as e:
            trace["fallback"] = f"model unavailable: {e}"
            break
        u = trace["usage"]
        u["calls"] += 1
        u["cached"] += meta["cached"]
        u["input_tokens"] += meta["input_tokens"]
        u["output_tokens"] += meta["output_tokens"]
        j = parse(text)
        messages.append({"role": "assistant", "content": text})
        if j is None or j.get("action") not in ("tool", "final"):
            bad += 1
            trace["turns"].append({"action": "invalid", "raw": (text or "")[:300]})
            if bad > 1 or steps > max_steps:
                trace["fallback"] = "unparseable model reply"
                break
            messages.append({"role": "user", "content": "Reply with one valid JSON object as specified."})
            continue
        if j["action"] == "final" or steps >= max_steps:
            final = j if j["action"] == "final" else None
            if final is None:
                trace["fallback"] = "no final answer within the tool budget"
            break
        steps += 1
        result = tools.call(str(j.get("tool")), j.get("args"))
        body = json.dumps(result, ensure_ascii=False, sort_keys=True)
        trace["turns"].append({"action": "tool", "tool": j.get("tool"), "args": j.get("args"), "why": j.get("why"),
                               "result_chars": len(body)})
        messages.append({"role": "user", "content": f"TOOL_RESULT {j.get('tool')}: {body[:MAX_RESULT_CHARS]}"})
    if final is None:
        trace["tool_calls"], trace["seen_ids"] = tools.calls, sorted(tools.seen_ids)
        return trace
    trace["summary"] = final.get("summary")
    for p in final.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        trace["proposals"].append(p)
        kind = p.get("type")
        if kind == "link":
            ok, checks, reason, link = validators.validate_link(auditor, req, baseline, p, tools)
            if ok:
                trace["accepted_links"].append([link.resource_id, link.source, link.basis])
        elif kind == "contradiction":
            ok, checks, reason = validators.validate_contradiction(auditor, req, p, tools)
        else:
            ok, checks, reason = None, {}, "refusal recorded (advisory)"
        trace["validations"].append({"type": kind, "resource_id": p.get("resource_id"), "ok": ok,
                                     "checks": checks, "reason": reason})
    trace["tool_calls"] = tools.calls
    trace["seen_ids"] = sorted(tools.seen_ids)
    return trace
