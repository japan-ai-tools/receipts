---
name: receipts
description: >-
  Verify an AI coding agent's "done" claims (tests passed, N tests passed, file created/modified/deleted,
  command succeeded, function/class added, counts) deterministically against the git working tree, the
  files, Python ASTs, real test runs and the session transcript, and write receipt.json with evidence.
  Use before telling the user a coding task is finished, when reviewing another agent's completion
  report, or when the user asks to verify, prove or double-check what was done. Includes a
  counterfactual mode that checks whether the tests fail without the change. Do NOT use for judging
  code quality or whether logic is correct, for non-code tasks, for repositories you may not run
  commands in, or as a substitute for actually running the tests.
license: MIT
---

# receipts

Checks what an agent *says* it did against what the repository *shows*. Every claim gets one of
`verified`, `contradicted` or `unverifiable`, with evidence (git status/diff, file paths and line
numbers, test exit codes and summary lines). Python 3.9+ standard library only; no network.

The script lives next to this file: `scripts/receipts.py` (for a global Claude Code install:
`~/.claude/skills/receipts/scripts/receipts.py`). Below, `R` means that path.

## When to use

- You are about to write a final "done" message for a coding task.
- The user pastes or points at another agent's report and asks whether it is true.
- You added or changed tests and want to know whether they actually exercise the change.

## When not to use

- Questions about code quality, design, or whether a fix is *semantically* right: receipts only
  checks facts that can be observed (files, definitions, exit codes, counts).
- No repository, or the user has not allowed running commands. Never run `counterfactual` in a
  repository where you are not allowed to touch the working tree.

## Workflow before reporting "done"

1. Write your draft completion report to a file outside the repo, e.g. `/tmp/report.md`. State
   facts plainly ("Created src/cache.py", "All 12 tests passed"); say explicitly what you did
   **not** verify ("I did not run the integration tests") so it is not counted as a claim.
2. Run the check, letting receipts run the tests itself:

   ```sh
   python3 R check --claims /tmp/report.md --run-tests "pytest -q"
   ```

   If you have the session transcript (Claude Code JSONL), add `--log <transcript>` so receipts can
   see which commands really ran and their exit codes.
3. Read the result:
   - exit `0`: everything verified.
   - exit `1`: something is **contradicted**. Fix the work, or correct the report. Do not reword the
     report just to avoid detection; the goal is a true report.
   - exit `3`: nothing contradicted, but some claims could not be checked. Say so in the report
     ("not verified: ...") or provide evidence (`--run-tests`, `--log`).
   - exit `2`: bad input or refused; read the error.
4. If the change adds behaviour and tests, optionally run the counterfactual (see below).
5. Put the one-line summary in your final message, e.g.
   `receipts: 5 claims - 5 verified (receipt.json, diff sha256 3f2a...)`.

## Subcommands

| Command | What it does |
| --- | --- |
| `check --claims FILE` (or `-- "text"`, or `--claims -` for stdin) | extract claims (EN + JA) and verify them |
| `check ... --run-tests CMD` | also run the test command and use its result |
| `check ... --log FILE` | use a transcript / command log as evidence |
| `check ... --base REF` | compare with a specific commit instead of HEAD |
| `run-tests --cmd CMD` | run tests with a timeout; record exit code, parsed counts, output hash |
| `counterfactual --cmd CMD` | run tests with and without the change (see below) |

Common options: `--repo DIR`, `--json` (full receipt on stdout), `--lang ja`, `--out PATH`,
`--no-write`, `--timeout SECONDS`. Every subcommand has `--help`.

Base selection: with uncommitted changes the change is `HEAD..working tree`. With a clean tree the
last commit is treated as the change (`HEAD~1..HEAD`). Use `--base` for anything else.

## Counterfactual (do the tests exercise the change?)

```sh
python3 R counterfactual --cmd "pytest -q"
```

1. Runs the tests with the change.
2. Stashes the non-test changes (`git stash push --include-untracked`; changed test files are kept
   so new tests run against the old code; `--all-changes` stashes them too).
3. Runs the tests on the baseline.
4. Restores in a `finally` block and compares a sha256 fingerprint of every tracked and untracked
   file (and the index) with the fingerprint taken before. A copy of every changed and untracked
   file is kept in `.git/receipts-backup/` until restoration is verified.

Verdicts: `exercises_change` (exit 0), `does_not_exercise_change` (exit 1, warning: tests pass with
AND without the change; names listed when the runner prints them), `with_change_failing` /
`no_changes` (exit 3), `refused` (exit 2), `safety_abort` (exit 4).

It **refuses** (changes nothing) when: not a git repo, no commits, merge / rebase / cherry-pick /
revert / bisect in progress, unmerged paths, `index.lock` present, assume-unchanged or
skip-worktree files, or modified submodules.

If it ever prints `SAFETY ABORT`, stop and show the user the printed recovery steps verbatim
(`git status`, `git stash list`, `git stash apply --index stash@{N}`). Do not run other git
commands first. Nothing is deleted in that path.

## Claude Code Stop hook

`hooks/stop_hook.py` checks the final assistant message on every stop and blocks only when a claim
is contradicted (with a short reason), with a loop guard. Setup: `references/claude-code-hook.md`.

## Rules

- receipts is evidence, not judgement: `verified` means "the observable fact matches", not "the
  code is correct".
- Never edit receipt.json by hand. Its sha256 values are integrity hashes for later comparison;
  they are **not signatures** and prove nothing about who produced the file. Do not call it signed.
- Report `unverifiable` claims honestly instead of presenting them as verified.

## More detail

- `references/claims.md` - what is extracted (EN/JA phrasing) and how each claim is verified
- `references/receipt-format.md` - receipt.json fields, exit codes, how to re-check hashes
- `references/counterfactual.md` - the stash/restore procedure and recovery
- `references/claude-code-hook.md` - installing the Stop hook
