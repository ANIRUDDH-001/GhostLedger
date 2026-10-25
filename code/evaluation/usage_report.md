# Usage report

## Final full-dataset run (produced the submitted `output.csv`)

Run on 2026-10-25 with:

```bash
python code/main.py
```

- **Output:** 518 rows (COMPLETE 162, INCOMPLETE 346, NOT_APPLICABLE 10) in about 3 s.
- **Evidence:** 3,160 observations.
- **Model calls:** the CLI prints `model_calls=0`.
- **sha256:** `5ac4a836dc71da2a0c7f2b866f2f8d54077f6d755b11c6f75d889d038fd082b7`.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Avg tokens / request | Est. cost | Est. cost / request |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| none | none (deterministic pipeline) | 0 | 0 | 0 | 0 | 0 | $0.00 | $0.00 |
| **Overall** | | **0** | **0** | **0** | **0** | **0** | **$0.00** | **$0.00** |

The final run uses no model. Intent classification, evidence parsing, identity resolution, state
reconstruction, policy evaluation and step generation are all deterministic code.

## Earlier experiments (not part of the final run)

During development, two optional LLM components were piloted and then not adopted:

- an investigator agent (`code/ghostledger/agent/`, `code/evaluation/agent_pilot.py`);
- an intent cross-check (`code/evaluation/intent_compare.py`).

Their accuracy gain over the deterministic pipeline was **0**: neither changed a status or improved a step on the
measured cases. They are therefore disabled, and they contribute nothing to `output.csv`.

The figures below come from the local usage ledger on 2026-10-25, 03:00–04:06 UTC. Every call succeeded.

| Provider | Model | Purpose | Live calls | Cache replays | Input tokens (live) | Output tokens (live) | Total (live) | Billed cost recorded |
|---|---|---|---:|---:|---:|---:|---:|---:|
| Google | gemini-3.1-flash-lite | investigator pilot, 12 cases × 3–5 turns | 41 | 82 | 45,914 | 2,603 | 48,517 | $0.00 |
| Groq | openai/gpt-oss-120b | intent cross-check, 25 batches | 25 | 0 | 33,815 | 21,247 | 55,062 | $0.00 |
| **Overall** | | | **66** | **82** | **79,729** | **23,850** | **103,579** | **$0.00** |

Notes on the experiment figures:
- **Cache replays** are offline re-runs answered from the local response cache. They made no network call; the
  ledger records the replayed token counts (91,828 in / 5,206 out), which are not added to the totals.
- **Cost:** both providers were used on free-tier keys, and no billed cost was recorded. A list-price estimate
  is not given, because provider pricing was not verified at release time.
- **Averages:** spread across the 518 requests, the experiments come to about 200 live tokens per request. They
  did not run on the 518-case output, though, so the per-request figure for the submitted run is 0.
- **Keys:** read from environment variables only. No key, fingerprint or credential appears in this report or
  in `code.zip`.
