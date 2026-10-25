# GhostLedger Phase 7: Final Plan to Submission

Written 2026-10-25 08:40 IST by Claude Code (claude-opus-5-5). Plan only: nothing is implemented, committed or overwritten.
Source state: HEAD `a8c339e` (main) plus the uncommitted Phase 6 closure work (`contract.py`, closure tests,
`conftest.py`). Root `output.csv` sha256 is `75509079…636c`, and its row order equals the input order (the input is
also in numeric order).

---

## Part A. Executive assessment

**Deadline.** The official event page says "Hackathon begins October 24 at 6:00 PM IST" and "Challenge starts • 24h".
It gives no explicit end time. The inferred deadline is **Sat 25 Oct 2026, 18:00 IST**. At 08:40 IST that leaves
**about 9 h 20 m**. The page also says the 30-minute AI voice interview "opens immediately after submission". This plan
therefore targets **submission by 17:00 IST** and reserves the last hour for submission, the interview and the
agent-trace upload. If the user's challenge email or dashboard shows a different end time, Part F rescales; the order of
work does not change.

**Where we stand.** We have a fast (2–5 s), deterministic and well-tested evidence pipeline: 95 tests, a contract
validator that catches 20/20 seeded mutations, and a temporal-integrity harness. On the 32 public cases it gets status
32/32, resource precision 1.000, recall 85/111 and exact 12/32. At runtime it makes **zero model calls**. The organizers
have said explicitly that an AI agent is expected. That is our largest *non-accuracy* gap.

**Strengths that are production-ready.** Evidence indexing for SQL, JSON, logs, PDF and text; the identity graph with
historical identifiers; scoped-absence proofs; the policy engine; canonical step ordering (sequence 1.000); atomic
output writes; the integrity guard against an interpreter without pypdf; and `contract.py`, which exists but is not
wired into the writer.

**Highest-impact defects, ranked by expected score effect.**

1. **Unlinked primary accounts.** 23 of the 26 missing public resources are "ambiguous attribution". The deletion-worker
   activity row names the account, but no identifier links that row to the requester. The existing worker-attribution
   mode fixes 13 of them with 0 false positives: exact goes 12 → 21/32. It is off by default.
2. **No genuine agent.** The AI-agent expectation is unmet: there are no tools, no model loop and no usage to report.
3. **Anonymized analytics records** (7 public resources). They carry no identifiers after anonymization. Some analytics
   records are identifier-linked *before* anonymization (for example, `analytics_5208` carries an email), which gives
   honest calibration pairs for a timing method. This is untested.
4. **Correctness defects with small measured footprints:**
   - stale in-file state selection (case_86, case_390);
   - claim-cited COMPLETE verifications (13 steps);
   - a missing required retained record that becomes `COMPLETE []` (synthetic only, 0 in the data);
   - the contract validator is not enforced at write time.

**Do not promote:**

- State engine S, as a whole. It flips case_86 and case_390 to COMPLETE and drops 33 obligations.
- Strict temporal mode. Public gold endorses retrospective links 8/8, and it costs 2 exact cases.
- Screenshot V5+O. It is sample-neutral, and its reference was built by us.
- Intent v2.1 regex growth. It changes 0 of 550 classifications.
- A global UNKNOWN → INCOMPLETE mapping.

**Competitive position (honest).**

- Strong: precision discipline, provenance, ordering and test depth.
- Weak: recall on unlinked resources, and no observable AI agency, which the interviewer and code judge will probe.

The plan attacks the two weaknesses first. Each attack is gated so it cannot damage precision.

---

## Part B. Reconstructed system architecture (current)

```
main.py ── load_dataset (io.py) ── EvidenceIndex.build (evidence.py: SQL/JSON/log/PDF/txt parsers, PNG unparsed)
   │                                   └─ integrity.check (fail closed on unparsed non-PNG)
   └─ Auditor.run (pipeline.py), per request in input order:
        intent.classify (regex, multilingual)               ── NOT_APPLICABLE → []
        IdentityGraph.subject/links (identity.py)           ── current + audit-log history, structural/catalog links
        [attribution.attribute, off by default]             ── forced-edge worker-row matching
        sighted() filter                                    ── drop catalog-only links
        StateEngine.resolve / scoped_absence (state.py)     ── latest eligible observation; FULL-scope omission = ABSENT
        one_primary_account pruning
        PolicyEngine.evaluate (policy.py)                   ── rule → treatment → finding/remediation/verification
        step assembly + finalize (steps.py)                 ── canonical order, numbering
   write_output → serialize_output (minimal checks) → atomic_write_text
Side modules: proof.py (citation checker, eval-only), ledger.py (Phase 4 obligation ledger, eval-only),
contract.py (full validator, tests only), evaluation/evaluate.py (sample metrics + ablations).
```

**Failure propagation.** UNKNOWN resources are dropped from COMPLETE rows. In INCOMPLETE rows they become an uncited
VERIFICATION. Unparsed non-PNG evidence aborts the run. Model failures do not exist yet, because there are no models.

**Production/scratch boundary.** Everything at runtime lives in `code/`. `.scratch/` (32 MB) holds experiments only;
nothing in the default path imports from it. The candidates that would need promotion are `state_s.py`,
`intent_v2.py`, the Gemini extraction scripts, the OCR cache and the Groq intent prompt.

---

## Part C. Findings reconciliation

| Finding | Source | Verified status | Counterexamples | Recommendation |
|---|---|---|---|---|
| Worker attribution +13 TP / 0 FP | Phase 3 E1; Phase 4 D blinded CV | Reproduced today: 0.656 exact | 2–8 FP only under *synthetic* perturbation (true row removed or displaced, decoy injected) | **PROMOTE**, with forced-edge abstention and a trace of every assignment |
| Attribution negative controls fail | Phase 4 §5.4 | Real | A forced edge is relative to the candidate graph | Add a corroboration filter: reject any assigned account with an identifier owner elsewhere (already done) or a sighting tied to another user. Report the residual risk honestly |
| Strict temporal is safer | Phase 4/6 | Truncation oracle clean; gold disagrees 8/8 | case_09, case_24 | **Keep retrospective as default** and document it as a contract ambiguity. Organizer ruling is still open |
| S fixes 21/21 scenarios | Phase 6 closure | Reproduced, but end-to-end optimistic | Defects A/B/C in brief §6.1 | **Do not promote S.** Port only one rule: *claims never set state; the latest eligible DIRECT observation per physical copy wins.* An authority change may never turn an existing finding into a silent drop |
| 13 claim-cited COMPLETE verifications | Phase 6 | Real by our proof model | **Aligned evidence accuracy is 1.000 on the sample**, so our citation choice matches gold wherever the resource matches | Downgrade from "defect" to "proof-model disagreement". Do not remove these steps without public evidence |
| UNRESOLVED → INCOMPLETE | brief §10 | Unsupported | Gold sample shows INCOMPLETE cases verify unproven accounts with an empty `evidence_id`, the same as B0. It shows nothing for COMPLETE cases | **Reject the global mapping.** Keep B0 semantics, expose every dropped obligation in the trace and report |
| Missing retained record → `COMPLETE []` | Phase 6 synthetic | Real mechanism, 0 data occurrences | — | Fix cheaply: INCOMPLETE + VERIFY_RECORD_PRESENT with empty evidence (the only schema-legal expression); no invented op |
| Intent v2.1 | Phase 6 | Dev/holdout gains; adversarial fails 6/12 | Negation, questions, quoted UI text | **Replace the regex arms race** with an LLM semantic judge that must agree with the regex. Disagreements go to a bounded re-investigation |
| Gemini 2.5 Flash screenshot IDs 30/30 | Phase 6 E | The reference is not independent | No end-to-end gain | Use only as an agent *tool* whose outputs must be corroborated by OCR or a structured source |
| Groq intent cross-check | Phase 6 E | 518/518 agree on eval; 5 sample errors | Preregistration timestamp later than the call ledger | Use as the intent judge model. **Correct the provenance record**: it was not independently preregistered |
| 3 Gemini keys round-robin | Phase 6 E log | Keys may share one project quota | — | **Use one key** from now on (`GEMINI_API_KEY_1`). Disclose the earlier round-robin in the usage report |
| Independent conservative reconstruction (508 INCOMPLETE) | brief §5 | Analytical, not gold | Contradicts the 32 public statuses (13 public COMPLETE) | Mine its mechanisms only; never a target |
| Organizer: keep input row order | Discord | Current output already equals input order (= numeric order) | AGENTS.md says "sort by case_id" | The validator asserts *input order*; both are satisfied today |

**Official requirements** (AGENTS.md §6, problem_statement, organizer answers 7.1–7.13) are binding. **Empirical
findings** are the rows above marked reproduced. **Hypotheses** are analytics timing, agent resolution of UNRESOLVED
obligations, and the value of the screenshot tool.

---

## Part D. Recommended target architecture

**Choice: Option A+.** A deterministic proof core, with a bounded tool-using investigator agent that is invoked on an
explicit open-question queue. It is a small custom loop: no LangGraph, no vector DB, no new heavy dependencies
(`google-genai` and `groq` are already installed).

Why not B or C:

- **Option B (agent drives everything).** It would replace a pipeline that is precision-1.000 on the sample with an
  untested model-led flow, nine hours before the deadline. It risks hallucinated resources and quota exhaustion (518
  cases × multi-turn > 1,000 RPD), and it is not reproducible.
- **Option C (specialist agents).** Same risks, plus coordination overhead, with no measured benefit. Its useful idea,
  role-specific prompts, is kept inside A+ as *question types*.

```
                 ┌─────────────── deterministic core (existing, hardened) ───────────────┐
requests ─► intent(regex) ─► identity graph ─► attribution(forced edges) ─► state(authority rule) ─► policy ─► ObligationLedger
                 │                                                                                     │
                 │              open-question queue  ◄─────────────────────────────────────────────────┘
                 │   Q1 intent: regex vs LLM disagreement / low-margin text
                 │   Q2 UNRESOLVED obligation: no eligible DIRECT observation
                 │   Q3 ambiguous attribution: >1 compatible worker row (case_23/28/29 pattern)
                 │   Q4 screenshot fact needed for a linked resource
                 ▼
        Investigator agent loop (agent/investigator.py)
          model: tool-calling LLM; budget ≤ 4 turns, ≤ 6 tool calls, timeout, per-run call cap
          tools (read-only, allow-listed, argument-validated, dataset-scoped):
            search_evidence(term)  read_evidence(file_id, locator)  resource_observations(resource_id)
            activity(resource_id|activity_id)  identity_history(email|user_id)  policy_for(category)
            view_screenshot(file_id, question)   [Gemini vision; result is a *claim* until corroborated]
          output: Proposal{kind, resource_id, state|link|intent, evidence_id, quoted_span, rationale}
                 ▼
        validators.py (deterministic; the only path back into the core)
          - every evidence_id exists, is captured ≤ audit_at, and the quoted span occurs verbatim in it
          - the resource_id/alias occurs in that span; the state keyword matches the treatment (per-treatment table)
          - links require a shared grounded identifier (email/hash/user_id/historical id) in an eligible source
          - a proposal may resolve UNRESOLVED → a state, or veto/confirm intent; it may never add a resource
            with no grounded link, never invent an op, never touch a finding backed by direct evidence
                 ▼
        accepted facts re-enter state/policy ─► steps ─► contract.validate (write-time gate) ─► output.csv
        everything ─► per-case trace JSON + usage ledger
```

**Model calls, caching and fallback.**

- Each response is cached by sha256(model, prompt version, inputs, tool results) in `code/agent_cache/` (gitignored,
  not zipped).
- Rate limiting is token-bucket per model. Retries are capped at 2 with `retry-after`.
- On any provider error, the case keeps its deterministic result and the trace records `fallback:<reason>`.
- A run with no keys equals the deterministic result plus the regex intent. This is tested.

**Prompt-injection posture.**

- Evidence text is wrapped as quoted data under an explicit "data, not instructions" system rule.
- Tools are read-only and limited to `dataset/` paths.
- No network access beyond the two model endpoints. Secrets come from env and are never placed in prompts.
- Validators make injected "mark COMPLETE" text inert: the agent cannot emit statuses or ops at all.

**Example walk-through (shape of the interview demo):** case_390, or whichever public INCOMPLETE case the trace shows
best.

1. The request text is classified by regex and LLM; both agree it is a deletion.
2. The identity graph finds the current and historical email, giving 6 linked resources.
3. Attribution adds the worker-row account (activity_X; the forced edge is shown).
4. The state engine finds a workflow "SUCCESS" claim, but a later DIRECT replica readback shows PRESENT.
5. Policy requires DELETE, so the case gets a FINDING citing the readback file.
6. One obligation is UNRESOLVED and goes on the queue. The agent searches for the resource, reads a SQL snapshot,
   proposes ABSENT with a quoted row omission, and the validator rejects it because the scope is not FULL. The step
   stays as an uncited future VERIFICATION, and the rejection is visible in the trace.
7. The remediation is ordered: data flows first, then dependents.
8. The contract gate passes.

---

## Part E. Root-cause analysis strategy

**Trace tool** (`code/evaluation/trace.py`, new). It writes one JSON object per case to
`.scratch/phase7/traces/<run>/case_X.json`, plus a diff tool comparing two runs. The schema covers the brief's §9
fields:

- request and intent: `regex_reason`, `llm_verdict`, `final`;
- identity: subject ids and the identifier set with source and capture time;
- resources: linked or dropped, with reason and link provenance;
- state: per observation `file`, `locator`, `event_time`, `captured_at`, `authority` (DIRECT/CLAIM/OMISSION), chosen
  state and rationale;
- policy: rule, treatment, conditions, compliant;
- agent: questions, tool calls, proposals, validator verdicts, tokens and the termination reason;
- export: steps, dropped obligations, and a baseline diff.

**Prioritized clusters:**

| # | Cluster | Cases | First divergence | Planned action |
|---|---|---|---|---|
| 1 | Unlinked worker accounts | 01, 02, 07, 08, 10, 22, 30, 31, 32 (+18, 20, 21, 25 partial) | identity | Stage 2 promotion |
| 2 | Anonymized analytics | 03, 04, 21, 25, 26, 27, 28 | identity | Stage 6 stretch experiment (timing anchored on the account row, calibrated on identifier-linked analytics) |
| 3 | Timing-ambiguous accounts | 23, 28, 29 | identity | Q3 agent: look for non-timing corroboration; abstain otherwise (organizer 7.1) |
| 4 | Gold resources absent from data | 18, 20 (`audit_record_6645/5569`), 04 (`account_004`, no worker row) | data | Accept the loss; document it |
| 5 | Stale state / claim authority | 86, 390, 41, 135, 137, 383, 392 | state | Stage 4 authority rule; before/after traces |
| 6 | Screenshot / catalog-only | 33, 35, 46, 50, 53, 61, 63, 170 | evidence | Q4 agent with corroboration; otherwise unchanged |
| 7 | Strict-temporal disagreement | 09, 24 | temporal | No change; documented |

Every change must have a before/after trace diff that names the mechanism. Any row change not explained by the
intended mechanism blocks promotion.

---

## Part F. Implementation roadmap (time-boxed; IST)

Each stage ends with: `python -m pytest code/tests -q` (Python 3.11) green, a sample eval, a full 518 candidate run
under `.scratch/phase7/`, a diff against the previous stage, and a commit on branch `phase7` (needs approval).

### Stage 0. Safety and harness (08:50–09:20, 30 m) [critical]

- Create branch `phase7` and commit the existing uncommitted closure work as the "Phase 6 closure" commit.
- Snapshot baseline outputs to `.scratch/phase7/baseline/`.
- Write `trace.py` (dump plus diff); reuse `Auditor` traces and `ledger.py`.
- **Accept when:** the baseline reproduces (hash `75509079…`, 12/32, 95 tests).
- **Rollback:** the branch.

### Stage 1. Write-time contract gate and obligation surfacing (09:20–09:50, 30 m) [critical]

- `write_output` calls `contract.validate`. Structural errors refuse the write; semantic issues are logged.
- Harden the known validator gaps: bool or float `step_no`, list-valued fields, real calendar dates via
  `datetime.strptime`, and input-order check.
- Missing retained record: INCOMPLETE + VERIFY_RECORD_PRESENT (empty evidence), with a test.
- Add tests for each gap.
- **Accept when:** 0 contract errors on 518 + 32; output unchanged except synthetic tests.

### Stage 2. Promote worker attribution (09:50–10:30, 40 m) [critical, highest accuracy lever]

- Default `worker_attribution=True`. Keep forced-edge abstention.
- Add a corroboration veto: drop an assigned account that is sighted in any eligible source with identifiers matching
  a *different* user.
- Every assignment goes in the trace with its candidate set.
- Re-run the Phase 4 negative controls and report them unchanged or improved.
- **Accept when:** sample exact ≥ 21/32, precision 1.000, 0 status flips on eval, and every eval change is exactly
  "+1 account step" (Phase 3 measured 155).
- **Rollback:** the flag.

### Stage 3. Agent core and intent role (10:30–12:45, 2 h 15 m) [critical for "AI agent"]

- New package `code/ghostledger/agent/`:
  - `llm.py`: Gemini and Groq clients, JSON-schema output, cache, rate limiter, usage ledger, fallback;
  - `tools.py`: read-only tools over `EvidenceIndex`;
  - `investigator.py`: the loop with budgets;
  - `validators.py`;
  - `prompts/*.md`, versioned.
- Run `models.list` once (free) to confirm which models are accessible today.
- **Intent role.** Groq `openai/gpt-oss-120b` judges all 550 texts, batched about 16 per call, about 35 calls, within
  8K TPM with pacing.
  - Final rule: authorize only if regex and LLM agree.
  - On disagreement, run a single focused Gemini re-read. Any remaining disagreement takes the LLM verdict when it cites
    a negation or question cue that appears verbatim in the text; otherwise it takes the regex verdict. Both are traced.
- Freeze an adversarial intent set *before* tuning: the brief's 12 probes plus 20 new ones written blind, in five
  languages. The set lives in `code/tests/fixtures/`.
- **Accept when:** adversarial false authorizations are below the regex's 6/12; sample intent 32/32; eval changes are
  listed and each one is justified by quoted text; the keyless run equals the deterministic run.
- **Stop/go:** if the LLM path changes any sample status, revert to advisory mode.

### Stage 4. Authority rule and investigator on UNRESOLVED obligations (12:45–14:30, 1 h 45 m) [critical-lite]

- In `state.py`, claims never set state, and the latest eligible DIRECT observation per physical copy wins.
  Same-time conflicts become UNRESOLVED, but **an existing direct-evidence finding is never silently dropped**.
- Port S's per-resource authority logic only. Reuse `state_scenarios.json` as tests and add the brief's §11 scenarios.
  Per-treatment compliance (DISABLED does not satisfy DELETE) goes in `policy.py`.
- **Q2 investigator:** for each UNRESOLVED obligation (about 127 on eval), at most 4 turns using Gemini Flash-Lite
  (`gemini-3.1-flash-lite`, 15 RPM / 500 RPD). Overflow goes to `gemini-3.5-flash-lite`; if it is still exhausted, the
  case stays deterministic. Validators gate every proposal.
- **Accept when:** the 21 + new scenarios pass; case_86 and case_390 stay INCOMPLETE with correct citations; sample
  exact and precision do not drop; each accepted agent proposal has a verbatim span; the eval wall-clock is ≤ 30 min.
- **Stop/go:** if accepted proposals are 0 or wrong on spot-check, keep the investigator advisory: it is traced, but its
  results do not change outputs. Report that honestly.

### Stage 5. Final run, reports and package (14:30–16:30, 2 h) [critical]

- Run a fresh-cache live run, which produces the real usage, then a cached replay to confirm byte-identical output.
- Overwrite root `output.csv` (needs approval).
- Write `evaluation_report.md` with:
  - the four dimensions;
  - sample metrics for default vs each stage;
  - at least 3 custom cases, drawn from the adversarial and scenario fixtures;
  - failures and improvements;
  - limitations: the 7 analytics resources, 3 absent-from-data resources, the strict-temporal ambiguity, and the
    unvalidated attribution risk.
- Write `usage_report.md` from the usage ledger of the final run only.
- Update README with setup, env vars, the run command, the no-key fallback and the architecture.
- Package `code.zip` per Part I. Run a clean-copy dry run: unzip into temp, add the dataset, run, and compare hashes.
- Append to `log.txt`. Collect the native Claude Code session JSONLs for `agent-trace.zip`, redacted.

### Stage 6. Stretch, only if Stage 5 is staged by 15:30

- **6a. Analytics attribution (45 m).** Anchor on the attributed or settled account row; take ANONYMIZE rows in a window
  calibrated on identifier-linked analytics records; use forced edges.
  - Gate: held-out settled pairs 100% precision with n ≥ 15, and the sample at +3 TP or better with 0 FP.
  - Otherwise reject and document.
- **6b. Q3/Q4 agent roles**, with the same validator gate.

### Interview prep (16:30–17:00)

Two real traces, the architecture diagram, a defense of every rejected candidate, and the limitations. Submission by
17:00. The user submits; the interview follows.

**Minimum viable:** Stages 0, 1, 2 and 3 (intent role only), then 5. **Full:** adds 4. **Stretch:** 6.

---

## Part G. Model and provider strategy (₹0)

| Role | Model | Quota (pasted 25 Oct) | Planned use | Fallback |
|---|---|---|---|---|
| Intent judge (batched) | Groq `openai/gpt-oss-120b` | 30 RPM, 1K RPD, 8K TPM, 200K TPD | ~35–70 calls | regex |
| Re-read on disagreement, Q2/Q3 investigator | Gemini `gemini-3.1-flash-lite` | 15 RPM, 250K TPM, 500 RPD | ≤ 450 calls | `gemini-3.5-flash-lite` (separate 500 RPD), then deterministic |
| Screenshot tool (Q4) | Gemini `gemini-2.5-flash` | 5 RPM, 20 RPD | ≤ 11 calls | OCR cache, then "unreadable" |

- One Gemini key only; no key rotation.
- Cache every response. Record the model id, prompt version and tokens per call.
- The usage report states the free-tier cost as $0, plus a list-price estimate.

---

## Part H. Evaluation strategy

| Dimension | Measurement |
|---|---|
| A. Output correctness | `evaluate.py` on 32 public cases (acknowledging repeated exposure); excluded cases 39/55 reported separately |
| B. Evidence soundness | `contract.citation_issues` and `proof.cite` on all 518; claims vs readbacks counted |
| C. Obligation completeness | Ledger: every linked resource is either emitted or dropped with a reason; counts per reason |
| D. Agent quality | Calls; accepted vs rejected proposals; hallucination rejections (IDs or spans not found); tokens; fallbacks; termination reasons; adversarial intent score |

- The frozen adversarial and scenario fixtures are written before any prompt tuning.
- Eval-set changes are judged by trace mechanism, never by row counts alone.
- We make no claims about hidden accuracy.

---

## Part I. Submission packaging

| Path | Contents |
|---|---|
| `code/` | `main.py`, `ghostledger/` (including `agent/` and `prompts/*.md`), `evaluation/` (`evaluate.py`, `trace.py`, `evaluation_report.md`, `usage_report.md`), `tests/` + fixtures, `requirements.txt` |
| `README.md` (root, zipped) | Setup, env vars (names only), run commands, architecture, fallback behaviour, limitations |
| root `output.csv` | Final run; 518 rows in input order |
| root `log.txt` | Append-only; uploaded as chat_transcript |
| `agent-trace.zip` | Native Claude Code session JSONLs for this repo, redacted; uploaded after the interview per `agent_trace_instructions.md` |
| `.scratch/` | Stays local, untouched. Nothing at runtime imports it |

**Excluded from the zip:** `dataset/`, `.env`, `agent_cache/`, `__pycache__`, `.pytest_cache`, `.scratch`, `.git`,
nested archives.

**Gate:** the zip listing is checked, a secret-pattern grep finds nothing, the size is under 50 MB, and a clean-copy run
reproduces the output hash.

---

## Part J. Risks and unresolved decisions (by severity)

1. **Deadline uncertainty.** If the window is shorter, cut Stage 4 and then Stage 3's re-read. Ask the organizers:
   "Is the submission deadline 25 Oct 18:00 IST, and must the AI interview finish before it?"
2. **Attribution false positives on hidden cases.** Not measurable without labels. Bounded by forced-edge abstention
   and the veto. Disclosed.
3. **Provider outage or quota.** The deterministic fallback is tested. The final run must not start later than 15:00.
4. **Post-audit identity links (contract ambiguity).** We follow gold behaviour and document it. An organizer answer
   would change 222 eval resources.
5. **COMPLETE rows dropping UNRESOLVED obligations.** These are kept, traced and disclosed. An organizer answer
   ("does empty or partial COMPLETE cover unresolved linked obligations?") would change about 15–33 rows.
6. **Excluded cases 39 and 55** stay in the output, because the contract says 518 rows.

Everything else is resolved by code and evidence in this plan.

---

## Part K. Checklist (for sequential sessions)

- [ ] S0 branch `phase7`; commit closure work; baseline snapshot; `trace.py` and diff
- [ ] S1 wire `contract.validate` into `write_output`; harden 5 validator gaps; missing-retained-record → INCOMPLETE +
      VERIFY_RECORD_PRESENT; tests
- [ ] S2 attribution default-on + identifier veto + trace; Phase 4 controls re-run; sample ≥ 21/32
- [ ] S3 `agent/` (llm, tools, investigator, validators, prompts); `models.list`; frozen intent fixtures; Groq intent
      judge + Gemini re-read; keyless test; usage ledger
- [ ] S4 state authority rule + per-treatment compliance; scenario tests; Q2 investigator gated
- [ ] S5 live final run → replay check → root `output.csv` (approval) → reports → README → `code.zip` dry run → log →
      agent-trace
- [ ] S6 (stretch) analytics attribution experiment; Q3/Q4 roles
- [ ] Interview notes; submit by 17:00 IST

---

## Part L. Final recommendation

1. **Architecture:** Option A+. A deterministic proof core plus a bounded, validator-gated investigator agent run on
   an open-question queue.
2. **First task:** Stages 0–2. Branch and commit, the trace tool, the write-time contract gate, then promote worker
   attribution (+9 public exact, 0 FP).
3. **Preserve:** attribution, `contract.py`, the retrospective identity default, the B0 citation choice and S's
   scenario tests.
   **Redesign:** S becomes a single authority rule; intent becomes regex + LLM agreement; the screenshots become an
   agent tool.
   **Reject:** S wholesale, strict temporal as the default, B2b, A-V4, global UNKNOWN → INCOMPLETE, regex-only intent
   growth.
4. **Fit:** Stages 0–5 fit in about 8 h with about 30 m of slack. Stage 6 is likely cut.
5. **Minimum:** S0 + S1 + S2 + S3 (intent role) + S5.
6. **Stretch:** S4 investigator and S6 analytics attribution.
7. **Approval needed for:**
   - creating branch `phase7` and committing to it (no push);
   - promoting attribution to the default;
   - live free-tier model calls (one Gemini key + Groq);
   - overwriting root `output.csv` at S5;
   - any push to GitHub;
   - the submission itself, which the user performs.
