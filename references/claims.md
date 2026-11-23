# Claims: what receipts extracts and how it verifies them

receipts reads a completion report sentence by sentence (bullets, numbered lists and Japanese
`。` sentences are split). Extraction is rule-based (regular expressions), not an LLM. A sentence
that is hedged or negated is **not** treated as a claim:

- EN hedges: should, would, might, may, could, will, expect, hopefully, probably, please, once,
  unless, if you, need to, make sure, try
- EN negation: not, n't, never, unable, did not, haven't, cannot, no tests ...
- JA hedges: はず, 予定, かもしれ, でしょう, だろう, てください, たら, れば, 見込み, つもり ...
- JA negation: ていません, ませんでした, できません, 未実行, 未確認, 未実施, せず, なかった ...

So "I did not run the tests" / 「テストは未実行です」 produce no test claim. That is deliberate:
honest statements of what was *not* done should never be flagged.

## Claim kinds

| kind | example (EN) | example (JA) | verified against |
| --- | --- | --- | --- |
| `tests_passed` | "All 42 tests passed", "tests are green", "the vitest suite passes (24 tests)", "1 failed, 11 passed" | 「テスト 15 件すべてパスしました」「全 8 件のテストがパスしました」 | last test run in `--run-tests` / `--log`: exit code + parsed summary line + counts |
| `command_succeeded` | "Ran `npm test` and it passed", "`ruff check .` is clean", "the build succeeds", "mypy passes" | 「`go test ./...` が成功しました」「ビルドが成功しました」 | matching command in `--log` (or `--run-tests`) and its exit code |
| `file_changed` (create / modify / delete) | "Created src/cache.py", "updated README.md", "deleted old_api.py" | 「tests/test_auth.py を新規作成」「`config.py` を修正」「old.py を削除」 | file existence + `git diff --name-status` / untracked files vs. the base |
| `symbol_added` | "Added a `parse_date()` function to utils.py", "added class `RateLimiter`" | 「関数 `normalize` を追加」「クラス `UserRepository` を追加」 | Python: `ast` (definition exists, kind matches, new or changed vs. base). Other languages: word-boundary definition search (`function X`, `func X`, `fn X`, `const X =`, ...), **low confidence** |
| `count` | "Added 4 unit tests", "with 2 new tests", "Modified 2 files", "Created 3 new files" | 「単体テストを3件追加」「3つのファイルを変更」 | new test functions (Python AST; `it(`/`test(` for JS/TS, `func Test` for Go, `#[test]` for Rust), new/changed files from git |

## Statuses

- `verified` - the observable fact matches the claim. `confidence` is `high`, `medium` or `low`
  (low = text search instead of AST, or exit code inferred).
- `contradicted` - the evidence disagrees (file missing / unchanged, definition absent or of a
  different kind, test run failed, count differs, command exited non-zero, or a complete
  transcript shows no test command ran at all).
- `unverifiable` - there is no evidence either way (no test run or log, not a git repository,
  definition only mentioned in a non-Python file, file changed in the last commit and the agent
  may have committed it, ...).

## Test output parsers

Summary lines are parsed for: pytest, unittest, jest, vitest, mocha, go test, cargo test, and
per-test verbose lines (pytest `-v`, unittest `-v`, `go test -v`, cargo). A run counts as passed
only when the exit code is 0, no failures/errors were parsed, and the run did not report zero tests.

## Logs (`--log`)

Accepted formats:

1. Claude Code transcript JSONL (`tool_use` blocks with `input.command`, matching `tool_result`
   blocks; exit code taken from "Exit code N" text or `is_error`). Treated as a **complete**
   record of the session, so "no test command ran" is a contradiction.
2. JSONL with one object per command: `{"command": "...", "exit_code": 0, "output": "..."}`.
   Also treated as complete. Any agent or CI can produce this.
3. Plain text, where lines starting with `$ ` (or `> `, `❯ `, `% `, `PS ...>`) start a command and
   an `exit code: N` line gives the exit code. Treated as **incomplete** (absence proves nothing).

When a complete log is given and it contains no `git commit`, receipts knows the agent did not
commit, so "file unchanged in the working tree" is a contradiction even if the last commit touched
the file.

## Known gaps

- Claims phrased in ways the patterns do not cover are silently skipped (no claim, no flag).
- A claim about *what* a change does ("fixed the timezone bug") is not checked; only that the file
  changed.
- Directories are not file claims in v1.
- Japanese summaries use Japanese labels; the per-claim reason strings are English.
