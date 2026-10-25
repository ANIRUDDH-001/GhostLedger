"""Model clients for Gemini and Groq over their HTTP APIs (httpx only), with a response cache,
per-model rate limits, retries, timeouts and a usage ledger of the tokens the provider reports.

Credentials come from the process environment, or from the repository .env file when the variable
is not set. A key value is never printed, logged or cached: the ledger records the variable name and
an 8-hex sha256 fingerprint only. Exactly one Gemini key is used (the first one present); keys are
never rotated to get around a quota.

Cached responses are keyed by sha256 of (provider, model, request body). A replay with the cache in
place makes no network call and returns byte-identical text, so a model-assisted run is reproducible.
"""
import hashlib
import json
import os
import time
from collections import deque
from datetime import datetime, timezone

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CACHE_DIR = os.environ.get("GHOSTLEDGER_LLM_CACHE") or os.path.join(REPO, ".llm_cache")

GEMINI_VARS = ("GEMINI_API_KEY", "GEMINI_API_KEY_1", "GOOGLE_API_KEY")
GROQ_VARS = ("GROQ_API_KEY", "GROQ_APY_KEY")      # GROQ_APY_KEY: misspelled alias found in the project .env

# free-tier limits as published on the provider consoles on 2026-10-25 (requests/min, tokens/min, requests/day)
LIMITS = {
    ("gemini", "gemini-3.1-flash-lite"): (15, 250_000, 500),
    ("gemini", "gemini-3.5-flash-lite"): (15, 250_000, 500),
    ("groq", "openai/gpt-oss-120b"): (30, 8_000, 1_000),
    ("groq", "openai/gpt-oss-20b"): (30, 8_000, 1_000),
}
SAFETY = 0.8          # use at most 80% of a published limit


class ModelUnavailable(RuntimeError):
    """No credential, quota exhausted, or the provider failed after retries. Callers fall back."""


def _env_file():
    out, path = {}, os.path.join(REPO, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if v:
                        out[k.strip().removeprefix("export ").strip()] = v
    return out


def credential(provider):
    """(var_name, value) for the provider's first configured key, or None."""
    names = GEMINI_VARS if provider == "gemini" else GROQ_VARS
    env = _env_file()
    for k in names:
        v = os.environ.get(k) or env.get(k)
        if v:
            return k, v
    return None


def fingerprint(v):
    return hashlib.sha256(v.encode()).hexdigest()[:8]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    """One provider/model. complete() returns (text, meta); meta carries tokens, cache hit and latency."""

    def __init__(self, provider, model, ledger_path=None, offline=False, timeout=90, retries=2):
        self.provider, self.model = provider, model
        self.offline = offline                      # cache replay only; a miss raises ModelUnavailable
        self.timeout, self.retries = timeout, retries
        self.ledger_path = ledger_path or os.path.join(CACHE_DIR, "usage_ledger.jsonl")
        rpm, self.tpm, self.rpd = LIMITS.get((provider, model), (5, 8_000, 20))
        self.gap = 60.0 / (rpm * SAFETY)
        self.recent = deque()                       # (t, tokens) in the last minute
        self.last = 0.0
        self.cred = None if offline else credential(provider)

    # -- cache ---------------------------------------------------------------------------------
    def _key(self, body):
        blob = json.dumps([self.provider, self.model, body], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, key):
        return os.path.join(CACHE_DIR, self.provider, key[:2], key + ".json")

    # -- ledger --------------------------------------------------------------------------------
    def _ledger(self, status, latency, in_tok=0, out_tok=0, note="", cached=False):
        row = {"ts": _now(), "provider": self.provider, "model": self.model, "status": status, "cached": cached,
               "latency_s": round(latency, 3), "input_tokens": in_tok, "output_tokens": out_tok,
               "key_var": self.cred[0] if self.cred else None,
               "key_fp": fingerprint(self.cred[1]) if self.cred else None, "cost_usd_recorded": 0.0, "note": note}
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def calls_today(self):
        if not os.path.exists(self.ledger_path):
            return 0
        day = _now()[:10]
        with open(self.ledger_path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        return sum(1 for r in rows if r["ts"][:10] == day and r["provider"] == self.provider
                   and r["model"] == self.model and not r.get("cached") and r["status"] != "skipped")

    def _pace(self, est_tokens):
        if self.calls_today() >= self.rpd * SAFETY:
            raise ModelUnavailable(f"{self.model}: daily request budget reached")
        while True:
            now = time.monotonic()
            while self.recent and now - self.recent[0][0] > 60:
                self.recent.popleft()
            used = sum(t for _, t in self.recent)
            wait = max(self.last + self.gap - now, 0.0)
            if used + est_tokens > self.tpm * SAFETY and self.recent:
                wait = max(wait, 60 - (now - self.recent[0][0]) + 0.5)
            if wait <= 0:
                return
            time.sleep(wait)

    # -- providers -----------------------------------------------------------------------------
    def _request(self, system, messages, schema):
        if self.provider == "groq":
            body = {"model": self.model, "temperature": 0, "reasoning_effort": "low",
                    "messages": [{"role": "system", "content": system}] + messages}
            if schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "answer", "strict": False, "schema": schema}}
            return body
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                             for m in messages],
                "generationConfig": {"temperature": 0}}
        if schema:
            body["generationConfig"].update(responseMimeType="application/json")
        return body

    def _post(self, body):
        import httpx
        if self.provider == "groq":
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {self.cred[1]}"}, json=body, timeout=self.timeout)
        else:
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                           headers={"x-goog-api-key": self.cred[1]}, json=body, timeout=self.timeout)
        return r

    def _parse(self, j):
        if self.provider == "groq":
            u = j.get("usage", {})
            return j["choices"][0]["message"]["content"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        u = j.get("usageMetadata", {})
        parts = j["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)

    def complete(self, system, messages, schema=None, note=""):
        body = self._request(system, messages, schema)
        key = self._key(body)
        path = self._cache_path(key)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                hit = json.load(f)
            self._ledger("ok", 0.0, hit["input_tokens"], hit["output_tokens"], note, cached=True)
            return hit["text"], {"cached": True, "input_tokens": hit["input_tokens"],
                                 "output_tokens": hit["output_tokens"], "key": key}
        if self.offline or not self.cred:
            raise ModelUnavailable(f"{self.provider}: {'offline replay cache miss' if self.offline else 'no credential'}")
        est = len(json.dumps(body)) // 3 + 1500
        err = None
        for attempt in range(self.retries + 1):
            self._pace(est)
            t0 = time.monotonic()
            self.last = t0
            try:
                r = self._post(body)
            except Exception as e:                      # timeout / connection error
                err = f"{type(e).__name__}"
                self._ledger("error:" + err, time.monotonic() - t0, note=note)
                time.sleep(2 * (attempt + 1))
                continue
            dt = time.monotonic() - t0
            if r.status_code == 200:
                text, i, o = self._parse(r.json())
                self.recent.append((time.monotonic(), i + o))
                self._ledger("ok", dt, i, o, note)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"provider": self.provider, "model": self.model, "text": text, "input_tokens": i,
                               "output_tokens": o, "at": _now()}, f, ensure_ascii=False)
                return text, {"cached": False, "input_tokens": i, "output_tokens": o, "key": key, "latency_s": dt}
            err = f"http {r.status_code}"
            self._ledger(err, dt, note=note)
            if r.status_code == 429:
                time.sleep(min(60.0, float(r.headers.get("retry-after", 20) or 20)))
            elif r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
            else:
                break                                   # 4xx other than 429 will not succeed on retry
        raise ModelUnavailable(f"{self.provider}/{self.model}: {err}")


def list_models(provider):
    """Metadata-only availability check (no generation, no token charge): model ids the key can see."""
    import httpx
    cred = credential(provider)
    if not cred:
        return None
    if provider == "groq":
        r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {cred[1]}"}, timeout=30)
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))
    out, page = [], None
    while True:
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": cred[1]},
                      params={"pageSize": 1000, **({"pageToken": page} if page else {})}, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += [m["name"].removeprefix("models/") for m in j.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]
        page = j.get("nextPageToken")
        if not page:
            return sorted(out)
