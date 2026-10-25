# Evaluation report (final release, 2026-10-25)

Final submitted output: the deterministic pipeline (`python code/main.py`), 518 rows, 0 model calls.
`output.csv` sha256 `5ac4a836dc71da2a0c7f2b866f2f8d54077f6d755b11c6f75d889d038fd082b7`; two consecutive runs and a
clean-room run from `code.zip` are byte-identical.

Public numbers are measured on the 32 sample cases with
`python code/evaluation/evaluate.py` against `dataset/sample_output.csv`. They are not hidden-test accuracy.
The full 518-case set has no public labels; it is checked with the structural, semantic and proof checks
described under "Full-dataset qualification".

## Case level (public sample, 32 cases)

| Metric | Value |
| --- | --- |
| Deletion-status accuracy | **1.000 (32/32)** |
| Exact case match (status and every step) | **0.656 (21/32)** |
| NOT_APPLICABLE in sample | none (intent is covered by synthetic controls instead) |

Not exact: case_03, 04, 18, 20, 21, 23, 25, 26, 27, 28 and 29. Every miss is a missing resource, never a wrong
status or an invented step. Causes: 23 resources with ambiguous primary-account attribution, and 3 resources that
are genuinely absent from any parsed evidence.

## Step and field level (public sample)

| Metric | Value |
| --- | --- |
| Step exact (positional) | 0.829 |
| Step content recall / precision (order-free) | 0.933 / **1.000** |
| Resource recall / precision | 0.883 / **1.000** (0 false-positive resources) |
| Resources TP / FP / FN | 98 / 0 / 13 |
| Evidence id, aligned steps | 1.000 |
| Evidence id, over all gold-cited steps | 0.880 |
| Fields on aligned steps: phase, system_id, resource_id, operation, state, evidence_id | 1.000 each |
| Field step_no | 0.889 (shifted by the missing resources above) |

## Sequence (public sample)

- Canonical phase and operation order: 1.000 of cases.
- Order agreement with gold (LCS): 1.000.

## Full-dataset qualification (518 cases)

There are no labels, so every candidate output was run through the same independent checker
(`final_qual.py`, rules R1–R7). The checker tests each COMPLETE verification step:
- R1: the cited file is eligible (captured at or before `audit_at`).
- R2: the file actually reads, or enumerates, the resource.
- R3: the reading is direct, not a claim.
- R4: the reading entails the step's state; an omission entails ABSENT only.
- R5: the reading is the latest one.
- R6: an omission comes from a declared, uncontradicted scope.
- R7: the capture comes after the last operation on the record.

| Candidate | C / I / NA | Steps | Verif. steps passing | R4 fails | R6 fails | Proven COMPLETE |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Stage 2 | 160 / 348 / 10 | 3,040 | 347 / 391 | 18 | 27 | 126 |
| Recovery | 161 / 347 / 10 | 3,038 | 349 / 393 | 18 | 27 | 127 |
| q3 | 162 / 346 / 10 | 3,019 | 370 / 382 | 7 | 5 | 139 |
| q4 | 162 / 346 / 10 | 3,024 | 371 / 387 | 11 | 5 | 139 |
| **q5 (final)** | **162 / 346 / 10** | **3,011** | **371 / 374** | **0** | **3** | **139** |

On the public sample, q4 and q5 are identical: 21/32 exact, 32/32 status, TP/FP/FN 98/0/13. q5 versus q4
on the 518 cases: no status changes, 13 steps removed, no steps added. q5 was selected as the final output.

## Participant-created evaluation cases

All cases use fictional fixtures (`example.test` addresses, resource ids outside the dataset ranges), and all
pass: **130 passed, 1 expected failure** (`python -m pytest code/tests -q`).

1. **Temporal recreation** (`test_synthetic.py`): a record goes present → deleted → recreated, and a SUCCESS
   claim is ignored. The answer follows the latest capture at or before `audit_at`.
2. **Retention and tombstones** (`test_synthetic.py`): a security audit record inside retention, an expired one,
   a legal hold, a user_id-only tombstone, and a non-deletion request (NOT_APPLICABLE).
3. **Remediation ordering** (`test_synthetic.py`): a live account after a SUCCESS claim, a historical email-hash
   link, an active flow, an allowed restore, an identifying support message and a decoy. The test checks the
   exact 15-step plan.
4. **Preservation semantics** (`test_preservation_semantics.py`, six controls added in the final release):
   - Omission does not prove REDACTED.
   - Omission does not prove ANONYMIZED.
   - A directly read redacted record verifies REDACTED.
   - A directly read anonymized record verifies ANONYMIZED.
   - A missing record that policy preserves is never declared preserved and gets no invented step.
   - A DELETE absence proof from an uncontradicted inventory still holds.
5. **Proof and scope integrity** (`test_proof_integrity.py`, `test_proof_model.py`, `test_scope_integrity.py`):
   - retained records that are missing;
   - linked accounts in an unknown state;
   - several live account copies;
   - HTTP-500 readbacks;
   - unknown restore eligibility;
   - FILTERED and UNKNOWN inventories.
6. **Temporal non-interference, output contract, reliability**: evidence captured after `audit_at` never
   changes an answer; contract-gate closure; malformed PNG, PDF and SQL inputs.

Mechanism tests for sample cases 05, 07, 12, 17, 27, 31 and 32 are in `test_mechanisms.py`. Seven pass, and one
known gap is marked as an expected failure.

## Failures found and improvements made

Final release:

- **A1. Omission is not proof of treatment.** An enumerating capture that merely did not list a record was
  used to emit `VERIFY_RECORD_REDACTED` / `_ANONYMIZED` / `VERIFY_DATA_FLOW_DISABLED` / `VERIFY_RESTORE_BLOCKED`.
  An omission entails ABSENT only. The rule now lives in the shared policy layer
  (`PolicyEngine.omission_establishes`, `PRESERVING`), which both the output and the obligation ledger use:
  - The step is no longer emitted.
  - The obligation is recorded as unresolved.
  - A missing record that policy preserves is never treated as compliant.
  
  This removed 11 steps across cases 39, 41, 46, 50, 53, 55 and 390. Among them are support_2667,
  analytics_7438, support_1034 and support_5720.
- **A2. Filtered listings are not exhaustive.** A capture that omits a record that eligible evidence shows was
  live at the capture time (scope type FILTERED) no longer proves absence. This removed account steps in
  cases 162 and 362. UNKNOWN-scope omissions (cases 253, 506 and 512) are kept, because they match the public
  sample's convention; they are the 3 remaining R6 findings.

Earlier phases:
- **Evidence parsing:** cache-miss log traces no longer leak across subjects, `*_started` lines are no longer
  read as PRESENT, and PDF rows carry per-row identifiers.
- **State:** partial inventories no longer override direct sightings (this was hiding replica drift), and
  failover replicas are checked per location.
- **Policy:** retained records in an unknown state and structurally linked accounts are kept.
- **Output:** an output contract gate runs before an atomic write.
- **Identity:** worker-row account attribution.
- **Proof:** a proof and obligation ledger separates coverage, compliance and citation authority.

Known limitations (documented, not patched case by case):

- **Forced COMPLETE serialization.** The schema has no "unknown" status. 23 COMPLETE cases contain at least
  one obligation the evidence cannot prove, so their COMPLETE is not proven:
  - cases 33, 35, 39, 41, 46, 50, 52, 53, 55, 63, 103, 135, 137, 162, 253, 362, 383, 390, 392, 403, 462, 506
    and 512;
  - the usual causes are a retained audit record with no eligible sighting, or one whose compliance is only
    claimed, and the A1/A2 obligations above.

  No finding, citation or step is invented to force INCOMPLETE. Specific cases:
  - **case_35 and case_63** have no verification step at all. case_63's only linked resource is
    marketing_001, which is known from the data catalog only.
  - **case_390** keeps its account status as the evidence shows it; no account finding is restored.
- **case_86** cites `evidence_08`, a search-index diagnostic log with cross-system `record_lookup` results.
  It is accepted as a direct readback, but its authority over account_service is an assumption that has not
  been verified.
- **Primary-account attribution** is ambiguous for 23 sample resources. **11 PNG screenshots** are structurally
  validated but not OCR'd.
