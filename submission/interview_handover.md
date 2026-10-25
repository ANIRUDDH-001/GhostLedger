# GhostLedger — interview handover (2 pages max)

## 30-second pitch
GhostLedger is a deterministic deletion auditor. For each request it:
1. decides whether deletion was actually requested;
2. rebuilds the person's current and historical identifiers;
3. reconstructs every linked resource's state **as of `audit_at`** from evidence, never from workflow claims;
4. applies `deletion_rules.csv`;
5. emits ordered FINDING → REMEDIATION → VERIFICATION steps.

The final run makes 0 model calls and is reproducible byte for byte. An LLM investigator and an LLM intent
cross-check were piloted on Gemini and Groq; they added no accuracy, so they were left out.

## Pipeline (what to draw on the whiteboard)
`io` → `intent` → `evidence` (JSON, logs, SQL via SQLite, PDF; PNG checked but not OCR'd) → `identity`/`attribution`
(audit-log history, hashes, resource links) → `state` (latest eligible direct sighting; omission → ABSENT only from
an enumerating capture taken after the last operation) → `policy` → `pipeline` → `steps` → `contract` gate → atomic write.
A separate `ledger`/`proof` layer classifies every obligation as PROVEN_COMPLIANT, PROVEN_VIOLATION or UNRESOLVED.
It drives qualification, not the output.

## Numbers to quote
- **Public sample (32 cases):**
  - status 32/32;
  - exact 21/32;
  - resource precision 1.000 and recall 0.883 (TP/FP/FN 98/0/13);
  - step content precision 1.000 and recall 0.933;
  - sequence order 1.000.
- **All 518 cases:** 162 COMPLETE / 346 INCOMPLETE / 10 NOT_APPLICABLE; 3,011 steps.
- **Independent R1–R7 checker:**
  - 371/374 COMPLETE verification steps pass, with 0 state-entailment failures (R4);
  - 139/162 COMPLETE cases are fully proven.
- **Tests:** 130 pass and 1 is an expected failure.

## Final-release changes (A1/A2), and why
- **A1, omission is not treatment.** A capture that does not list a record shows it is *missing*, not that it was
  *redacted* or *anonymized*, nor that a flow is *disabled* or a backup *blocked*. The rule lives in
  `PolicyEngine.omission_establishes`, which both the output and the ledger use. Effect: 11 unsupported
  VERIFY steps removed across cases 39, 41, 46, 50, 53, 55 and 390, including support_2667, analytics_7438,
  support_1034 and support_5720. A record that policy keeps but is missing is never called compliant.
- **A2, filtered listings.** A scope that omits a record eligible evidence shows was live is FILTERED and cannot
  prove absence (cases 162 and 362). UNKNOWN-scope omissions (cases 253, 506 and 512) are kept because the public
  sample uses that convention; they are the 3 remaining R6 findings.
- Neither change moved the public score or any status. Both only remove claims the evidence could not support.

## Honest limitations (say these before being asked)
- **No UNKNOWN status in the schema:** 23 COMPLETE cases contain an obligation the evidence cannot prove, usually
  a retained audit record with no direct sighting. They are left as COMPLETE without invented steps rather than
  forced to INCOMPLETE with fabricated findings.
- **Missed resources on the public sample:** 23 resources with ambiguous primary-account attribution, and 3
  resources absent from all parsed evidence. No PNG OCR.
- **case_86:** a search-index diagnostic log's cross-system lookup is accepted as a readback of account_service.
  This is an authority assumption I can defend but have not proven.

## Likely questions → short answers
- *Why not an LLM?* It was measured: 66 live calls produced no gain on the pilot cases, while determinism gives
  reproducibility and auditability.
- *How do you avoid future leakage?* Only evidence with `captured_at` and `observed_at` ≤ `audit_at` is used.
  Non-interference tests show that later files never change an answer.
- *Remediation order?* Disable flows and block restores first, then delete, redact or anonymize, downstream before
  source. Verification covers every remediated item.
- *Tombstones?* Keep the stable `user_id`. Name, email, phone and email hash must be gone (REDACT rule).
