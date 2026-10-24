# Evaluation report (interim, Phase 2 baseline)

Measured on the 32 public sample cases with `python -W ignore code/evaluation/evaluate.py`. These are sample numbers, not hidden-test accuracy. Full details, ablations and per-resource evidence are in [`phase2_checkpoint.md`](phase2_checkpoint.md) and [`phase2_reachability.md`](phase2_reachability.md).

## Case level

- **Deletion-status accuracy: 1.000 (32/32).**
- **Exact case match: 0.375 (12/32).**
- The sample contains no NOT_APPLICABLE case, so intent classification is not measured against gold here.

## Step and field level

- **Steps:**
  - Positional exact step accuracy: 0.663.
  - Order-free step recall 0.865, precision 1.000.
- **Resources:** precision 1.000, recall 0.766 (85/111), 0 false positives.
- **Evidence:** 1.000 on aligned steps; 0.813 over all gold-cited steps.
- **Fields on aligned steps:** phase, system_id, resource_id, operation, state and evidence_id are all 1.000; step_no is 0.766.

## Sequence

Canonical phase/operation order holds in 100% of cases, and LCS order agreement with gold is 1.000.

## Participant-created evaluation cases

The cases are in `code/tests/test_synthetic.py` (fictional fixtures; all pass):

1. **Temporal:** a record goes present → deleted → recreated, and a SUCCESS claim is ignored. The answer follows the latest capture at or before audit_at.
2. **Retention:** a security audit record inside retention, an expired one, a legal hold, a user_id-only tombstone, and a non-deletion request.
3. **Ordering:** an account still answering 200 after a SUCCESS claim, a historical email-hash link, an active flow, an allowed restore, an identifying support message and a decoy. The test checks the exact 15-step plan.

Mechanism tests for sample cases 05, 07, 12, 17, 27, 31 and 32 are in `code/tests/test_mechanisms.py`: 7 pass, plus 1 known gap marked expected-failure.

## Failures found and improvements made

Fixes made:

- Cache-miss results leaked across log traces. Now only subject keys merge.
- `*_started` lines were read as PRESENT.
- Partial inventory scopes overrode direct sightings. This hid replica drift and marked audit records missing.
- Retained records with unknown state were dropped.
- Structurally linked accounts were dropped.
- PDF rows lacked per-row identifiers.

Known remaining failures:

- Primary-account attribution: 16 sample resources.
- Anonymized analytics with no identifiers: 7.
- Genuinely absent resources: 3.
- 11 PNG screenshots unparsed.
