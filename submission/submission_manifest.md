# GhostLedger — submission manifest

Prepared 2026-10-25 (UTC). **Nothing was submitted to HackerRank, and nothing was committed, pushed or branched.**
The participant does the upload and the version-control publication.

## Files in this folder

| File | Bytes | sha256 | Upload slot |
|---|---:|---|---|
| `code.zip` | 136,119 | `4f310438015a910f857575943806a9245ab135af1fc04db77ea77f41cc74ce3e` | code.zip |
| `output.csv` | 600,914 | `5ac4a836dc71da2a0c7f2b866f2f8d54077f6d755b11c6f75d889d038fd082b7` | output.csv |
| `log.txt` | 217,639 | `bc63849d6a8363c356572b704354440fdbd8e34d61af9a4ea5da88f25dc8881b` | chat_transcript |
| `agent-trace.zip` | 51,585,012 | `8e9a7f82715fe7a3dd94f30b4a0d84c84dc82d74f91f638bafae5e2e0b0d11d4` | agent trace (if the portal asks for one) |
| `interview_handover.md` | — | — | not uploaded; notes for the AI interview |

The sizes and hashes for `log.txt` and `agent-trace.zip` were taken before this session closed. Both files change once the
final turn is logged (see the post-close steps below).

## Checks behind each file

- **`output.csv`**
  - Byte-identical to the repository-root `output.csv`.
  - 518 rows sorted by `case_id`: 162 COMPLETE, 346 INCOMPLETE, 10 NOT_APPLICABLE.
  - 0 model calls; deterministic across repeat runs.
  - Row order is lexicographic by `case_id` (case_1, case_10, case_100, …). This is the same order as `sample_output.csv`
    and the CLI's sort.
- **`code.zip`**
  - 51 allowlisted files with `README.md` at the root.
  - Excludes `dataset/`, `.env`, virtual environments, bytecode, `.git/`, `.scratch/`, caches and nested archives.
  - Secret scan clean.
  - In a clean-room unpack (`python -B -E code/main.py`, dataset outside the room), it reproduces `output.csv`
    byte for byte. Report: `.scratch/phase7/release/code_zip_report.txt`.
- **`log.txt`**
  - Append-only copy of the root log.
  - Every entry from this session has exactly one `tool=Claude Code` and one `model=claude-opus-5-5`.
  - The final-release brief is logged verbatim.
  - The earlier entries, including the git-stash incident notes, are preserved.
- **`agent-trace.zip`**
  - 12 files: this project's Claude Code session transcripts, plus the Codex rollouts whose working directory was this
    checkout, plus `TRACE_MANIFEST.md`.
  - Regex redaction found 0 hits, and the post-scan found no leaks.
  - No `.env`, credential stores, auth files or personal documents.
  - Not nested inside `code.zip`.

## Submission URL

https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

The URL has four `w`s exactly as `AGENTS.md` gives it. `wwww.hackerrank.com` resolves to the same HackerRank Akamai edge
as `www.hackerrank.com`. The page itself was not opened while signed in.

## Deadline

**Not verified.** It is not configured in the repository. Public information suggests about 12:30 UTC on 2026-10-25;
confirm it on the contest page before uploading.

## Rollback

The pre-promotion root output is at `.scratch/phase7/release/rollback_output_root_before_promote.csv`. To restore it, copy
it over `output.csv`.

## Post-close steps (manual)

1. **Refresh `log.txt`:** after the last agent turn, copy the root `log.txt` over `submission/log.txt`.
2. **Rebuild the trace:**
   ```bash
   python .scratch/phase7/release/build_trace_zip.py submission
   ```
   The current session's transcript keeps growing until the session closes.
3. **Check the trace size:** at about 51.6 MB, `agent-trace.zip` may exceed a portal upload limit. If it does, upload the
   transcript the portal requires (`log.txt`) and keep the trace ready to provide on request.
