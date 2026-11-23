# Counterfactual mode: procedure, safety, recovery

Question answered: *if the change were not there, would the tests notice?* Tests that pass with
and without the change do not exercise it, however green they are.

```sh
python3 scripts/receipts.py counterfactual --cmd "pytest -q"           # keep changed test files
python3 scripts/receipts.py counterfactual --cmd "pytest -q" --all-changes  # stash tests too
```

Only **uncommitted** changes are considered (working tree + index + untracked, non-ignored files
versus HEAD). A commit-level counterfactual is not in v1.

## Refusals (exit 2, nothing touched)

- `git` missing, not a git repository, or no commits yet
- `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/`, `rebase-apply/` or `BISECT_LOG`
  present
- unmerged paths (`git ls-files -u`)
- files marked assume-unchanged or skip-worktree
- a submodule that is modified or not initialised
- `.git/index.lock` exists (another git process)

## Procedure

1. Classify changed paths: test paths (`tests/`, `test_*.py`, `*_test.go`, `*.test.ts`, ...) are
   *kept*; everything else is *stashed*.
2. Run the tests **with** the change.
3. Take a fingerprint: sha256 of every tracked and untracked non-ignored file (content + executable
   bit, symlink targets) plus `git ls-files -s` (the index).
4. Copy every changed file and every untracked file to `.git/receipts-backup/<timestamp>/files/`.
5. `git stash push --include-untracked -m "receipts-counterfactual <time>" -- <stashed paths>`;
   confirm the stash entry exists and those paths no longer differ from HEAD.
6. Run the tests on the **baseline**. Afterwards, undo anything the run itself did: new files are
   moved into the backup folder (`baseline-artifacts/`), kept/untracked files are copied back from
   the backup, tracked files are reset with `git checkout -- <path>`.
7. `finally`: `git stash apply --index <ref>` (falls back to `git stash apply`), take the
   fingerprint again and compare with step 3. Only if they are identical is the stash dropped and
   the backup removed (`--keep-backup` keeps it).

## If restoration cannot be verified (exit 4, `SAFETY ABORT`)

receipts stops, does **not** drop the stash, keeps the backup, and prints exact steps, e.g.:

```
restoration could not be verified (mismatch in calc.py). Nothing was deleted: your changes are in
`git stash list` as stash@{0} (1a2b3c4d5e6f) and copied to .git/receipts-backup/20261123-101500-4242.
To recover: inspect `git status`, then `git stash apply --index stash@{0}`.
```

Manual recovery:

```sh
git status                       # see what is in the working tree now
git stash list                   # find "receipts-counterfactual ..."
git stash show -p stash@{0}      # inspect it
git stash apply --index stash@{0}   # re-apply (resolve conflicts if any); drop it only when happy
ls .git/receipts-backup/         # byte-for-byte copies of every changed and untracked file
```

## Caveats

- The test command runs twice, with your environment. Commands that write outside the repository
  (databases, caches, network services) are not undone.
- Ignored files (`.gitignore`) are neither stashed nor fingerprinted; build outputs there may
  differ between runs.
- If the baseline fails for an unrelated reason (e.g. new tests import a module that only exists in
  the change), the verdict is `exercises_change`; check `baseline.output_tail` in the receipt.
- Per-test names are listed only when the runner prints them (`pytest -v`, `unittest -v`,
  `go test -v`, `cargo test`); otherwise only the overall verdict is given.
