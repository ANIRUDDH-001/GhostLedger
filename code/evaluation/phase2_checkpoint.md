# Phase 2 checkpoint — GhostLedger deterministic baseline

Date: 2026-10-24. Scope: evidence index, identity graph, temporal state engine, policy evaluator,
step generator, sample evaluation harness. No LLM agent loop and no model integration (0 model calls).
Sample numbers below are measured on the 32 public sample cases. They are **not** an estimate of
hidden-test accuracy, and the synthetic tests are not either.

## Executed commands

```bash
python -W ignore code/main.py                      # 518 rows -> ./output.csv
python -W ignore code/main.py --requests sample_deletion_requests.csv --out .scratch/sample_pred.csv
python -W ignore code/evaluation/evaluate.py       # metrics + ablations -> .scratch/eval/sample_eval.json
python -W ignore code/evaluation/evaluate.py --show --cases case_01,case_07   # per-case step diffs
python code/evaluation/render_reachability.py      # per-resource table -> code/evaluation/phase2_reachability.md
python -m pytest code/tests -q -W ignore           # 10 passed, 1 xfailed
python .scratch/validate_output.py output.csv dataset/deletion_requests.csv   # schema check (scratch helper)
```

## Output

- `output.csv`: 518 rows, natural `case_id` order, 2,885 steps. Status counts: INCOMPLETE 348, COMPLETE 160, NOT_APPLICABLE 10.
- Schema check: every `steps_json` parses; all 7 keys are present; op/state pairs are allowed; `step_no` is consecutive; phases are ordered; COMPLETE rows are verification-only with evidence; INCOMPLETE rows cite evidence on findings only.
  **1 contract violation:** `case_63` is COMPLETE with `[]`. Its only link (`marketing_001`, through data-catalog row `activity_4008`) appears in no parsed file, most likely an unparsed screenshot.

## Measured sample metrics (default configuration)

| Metric | Value |
|---|---|
| Case-status accuracy | **1.000** (32/32) |
| Case exact match (status + full steps) | 0.375 (12/32) |
| Resource precision / recall | 1.000 / **0.766** (85/111) |
| False-positive resources | 0 |
| Step exact (positional) | 0.663 |
| Step content recall / precision (order-free) | 0.865 / 1.000 |
| Evidence accuracy, aligned steps / all gold-cited steps | 1.000 / 0.813 |
| Sequence: canonical order / order vs gold (LCS) | 1.000 / 1.000 |
| Field accuracy (aligned): phase, system, resource, op, state, evidence | 1.000 each |
| Field accuracy: step_no | 0.766 (shifted by missing steps) |

Caveat: the sample has **no NOT_APPLICABLE case**, so the 10 NOT_APPLICABLE predictions in the evaluation set (case_533–542) are not measured against any gold answer.

Ablations (same index, one switch each):

| Variant | Status acc | Recall | Precision | FP | Exact cases |
|---|---|---|---|---|---|
| default (identity from any capture) | 1.000 | 0.766 | 1.000 | 0 | 0.375 |
| identity only from captures ≤ audit_at | 1.000 | 0.694 | 1.000 | 0 | 0.312 |
| keep every linked account | 1.000 | 0.766 | 0.988 | 1 | 0.344 |
| drop resources already absent before request | 1.000 | 0.766 | 1.000 | 0 | 0.375 |
| + workflow-window candidate links (diagnostic) | 0.844 | 0.775 | **0.202** | **340** | 0.031 |

Timestamp-window attribution adds about 1 point of recall for 340 false positives. It stays off.

## Reproducing the Phase 1 figures

Per-resource evidence: [`phase2_reachability.md`](phase2_reachability.md), 111 rows giving gold evidence, reach under each horizon, our citation, the oracle citation and the cause when unreached.

- **83/111 reachability is not reproduced exactly.** Measured: **85/111** with identity facts from any capture (default) and **77/111** with only captures ≤ audit_at. The +2 over Phase 1 matches two Phase 2 fixes: `support_001` (PDF row parsing) and `account_006` (structural `source_resource_id` link).
  The 8 resources reachable only at the any horizon (case_09 ×2, 23, 24, 26, 27, 29, 30) are linked by identity facts that first appear in post-audit captures. Their state is still read only from captures ≤ audit_at. Whether this is allowed is an open contract question.
- **70/75 citation agreement is falsified** as an end-to-end figure. End to end it is **61/75** (any) and 59/75 (audit). All 14 misses are unreached resources; among reached gold-cited resources agreement is 61/61.
  As an oracle figure (the state engine handed the gold resource) it is **74/75**. The one disagreement is `case_04 account_004`: we cite scope capture `evidence_90`, gold cites `evidence_54`. Both are inventory captures that omit the account.

## Why 26 sample resources are not linked (Phase 1 said 28)

`support_001` was a parser gap and is fixed. There are no remaining parser gaps in text, SQL, JSON, log or PDF formats.

| Cause | Count | Resources |
|---|---|---|
| Genuinely absent: in no participant file or activity row | 3 | case_04 account_004, case_18 audit_record_6645, case_20 audit_record_5569 |
| Ambiguous attribution: primary account | 16 | account_001/002/007/008/010, 5521, 5037, 6440, 4990, 410, 3136, 5482, 7012, 4163, 4857, 8858 |
| Ambiguous attribution: anonymized analytics | 7 | analytics_9406, 901, 5335, 1895, 4566, 4574, 6077 |

- **Primary accounts:** the only link is a deletion-worker "submitted primary profile" activity row, whose time window overlaps 3–12 other requests. A few are sighted only as un-keyed 404 lookups in `evidence_92`.
- **Anonymized analytics:** these carry no subject identifier in any capture.
- **Uncounted:** the 11 PNG screenshots are unparsed (no OCR backend). Gold excludes their catalog-only resources in the sample, so they cost no sample recall.
- **No wrong steps on linked resources:** every one of the 20 non-exact sample cases is explained by these unlinked resources.

Evaluation-set signal of the same gap: every sample answer has an account step. Our output lacks one in 17/32 sample cases and 262/508 applicable evaluation cases.

## Mechanism tests (`code/tests/test_mechanisms.py`, no case-specific logic)

| Case | Mechanism | Result |
|---|---|---|
| 05 | failover replica still holds a profile the primary dropped | pass |
| 07 | PDF attachment carrying requester email/phone | pass (case not exact: account_007 unlinked) |
| 12 | link only through a superseded email hash; marketing contact → DELETE | pass |
| 17 | eligible restore path → restore risk; older 404 account not reported; catalog-only tombstone excluded | pass |
| 27 | redacted support, disabled flow, blocked backup, retained audit record in a COMPLETE case | pass |
| 27 | anonymized analytics_4574 attributable | **expected failure** (known gap) |
| 31 | tombstone keeping only user_id → VERIFY_RECORD_REDACTED | pass |
| 32 | tombstone still holding identifiers → REPORT_EXPOSED_PII | pass |

## Synthetic tests (`code/tests/test_synthetic.py`)

These are hand-built miniature datasets with fictional users. They are not derived from any case and are not hidden-test accuracy. All 3 pass.

1. **Temporal:** the same record is PRESENT (09-01), DELETED (09-10) and recreated (09-20), with a SUCCESS deletion claim in activity. Audits at 09-05, 09-12 and 09-25 give INCOMPLETE, COMPLETE and INCOMPLETE, each citing the latest eligible capture.
2. **Retention:** an audit record inside retention gets VERIFY_RECORD_PRESENT; an expired one is dropped; a legal hold gets VERIFY_RECORD_PRESENT; a user_id-only tombstone is REDACTED; a "send me a copy… do not delete" request is NOT_APPLICABLE.
3. **Ordering and claims:** an account answering 200 despite a SUCCESS claim, analytics linked by a historical email hash, an enabled flow, an allowed restore and an identifying support message, plus a decoy account for another user. The test checks the exact 15-step plan: DISABLE and BLOCK come before DELETE, REDACT and ANONYMIZE, and the decoy is not attributed.

## Defects and limitations

1. **Primary-account attribution (largest):** 16/111 sample resources, and probably about half of the evaluation cases' account steps. COMPLETE cases then miss their main verification; INCOMPLETE cases miss a post-remediation check.
2. **PNG screenshots unparsed (11 files):** these give empty or short outputs where a screenshot is the only evidence. `case_63` is COMPLETE with `[]`, and four more evaluation cases have catalog-only links (case_33, 46, 52, 61).
3. **Anonymized analytics** with no identifiers cannot be attributed without workflow correlation.
4. **Identity horizon:** the default uses identity facts from post-audit captures (+8 sample resources). This needs a contract decision.
5. **Scope-absence citation choice is ambiguous** when several inventory captures omit a resource (case_04).
6. **NOT_APPLICABLE is untested against gold:** there are no sample examples.
7. **Flaky test seen once:** a pytest run of test_07 reported COMPLETE for case_07, in about half the normal runtime. It was not reproduced in any of the 15 later runs. test_07 now asserts that `evidence_58` parsed, so a recurrence will name the cause. A transient PDF read error is the suspect (`build()` records it as `error:` in `file_status`).

## Resource usage

| Run | Wall time | Peak RSS |
|---|---|---|
| `code/main.py` (518 cases, index built once) | 1.1 s | 43 MiB |
| `code/evaluation/evaluate.py` (5 variants) | 1.6 s | 45 MiB |
| `pytest code/tests` (11 tests) | 4.1 s | 92 MiB |

Python 3.11.9 and pypdf 4.3.0 on Windows 11 (AMD64). Evidence is 3.1 MB across 94 files: 83 parsed into 3,160 observations and 271 scopes, 11 unparsed PNGs. **Model calls: 0. Tokens: 0. Cost: $0.**

## Next recommended experiment

**Corroborated primary-account attribution, measured before adoption.** Treat the "submitted primary profile" deletion-worker rows as a global assignment problem across all 550 requests, not per-request windows. Accept an assignment only if:

- (a) it is the unique consistent match after every other request's supported links are removed, and
- (b) it is corroborated by something other than time: an un-keyed 404 lookup in the same trace window, a structural link, or an account-id/user-id pattern backed by evidence.

Report sample precision and recall for accounts separately from analytics. Analytics stays off unless precision is 1.0, since window elimination previously mis-assigned 4 analytics records.

In parallel, as a bounded and cached experiment, run OCR or a single vision pass over the 11 PNGs (11 calls, cached to `.scratch/`). Measure whether case_63 and the 4 other catalog-only evaluation cases gain citable evidence. Either step is adopted only if sample precision stays at 1.000.
