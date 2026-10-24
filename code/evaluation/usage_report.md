# Usage report

Run summarized: the full-dataset run on 2026-10-24 that produced `output.csv`.

```bash
python -W ignore code/main.py
```

This produced 518 rows (INCOMPLETE 348, COMPLETE 160, NOT_APPLICABLE 10) in 1.1 s wall time, with 43 MiB peak RSS.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Avg tokens / request | Est. cost | Est. cost / request |
|---|---|---|---|---|---|---|---|---|
| none | none (deterministic pipeline) | 0 | 0 | 0 | 0 | 0 | $0.00 | $0.00 |
| **Overall** | | **0** | **0** | **0** | **0** | **0** | **$0.00** | **$0.00** |

The current pipeline makes no model calls: intent classification, evidence parsing, identity resolution, state reconstruction, policy evaluation and step generation are all deterministic code. Update this report if a model is introduced.
