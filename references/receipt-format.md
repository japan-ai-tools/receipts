# receipt.json format (v1)

Written by every subcommand (default `<repo>/receipt.json`; `--out PATH` to change, `--no-write` to
skip). The receipt file itself is excluded from the diff it describes. A later `run-tests` or
`counterfactual` for the **same** `repo_head` and `diff_sha256` merges into the existing receipt,
so `check`, `run-tests` and `counterfactual` can be run one after another.

```jsonc
{
  "tool": "receipts",
  "version": "1.0.0",
  "created_at": "2026-11-23T02:15:50+00:00",      // UTC, ISO 8601
  "mode": "check",                                 // check | run-tests | counterfactual
  "repo": {"root": "...", "is_git": true, "head": "<sha>", "base": "<sha>",
           "base_note": "compared with HEAD", "dirty": true},
  "repo_head": "<sha of HEAD or null>",
  "diff_sha256": "<sha256>",                       // see below
  "changed_files": [{"path": "calc.py", "status": "M", "added": 5, "deleted": 1}],
  "report_sha256": "<sha256 of the report text>",  // check only
  "log": {"path": "...", "kind": "transcript", "commands_seen": 3, "sha256": "..."},  // with --log
  "claims": [
    {"id": "c1", "kind": "file_changed", "text": "Created tests/test_calc.py ...",
     "path": "tests/test_calc.py", "action": "create", "lang": "en",
     "status": "verified", "confidence": "high",
     "reason": "tests/test_calc.py is new (+10 -0)",
     "evidence": [{"type": "file", "path": "tests/test_calc.py", "exists": true},
                  {"type": "git", "command": "git diff --numstat 894777e525d5 -- tests/test_calc.py",
                   "status": "untracked", "diff_stat": "+10 -0"}]}
  ],
  "tests": {                                       // from --run-tests / run-tests / counterfactual
    "command": "pytest -q", "exit_code": 0, "timed_out": false, "duration_s": 0.41,
    "source": "run-tests", "passed": true,
    "parsed": {"framework": "pytest", "passed": 12, "failed": 0, "errors": 0, "skipped": 0,
               "total": 12, "summary_line": "12 passed in 0.31s"},
    "test_names": {"tests/test_a.py::test_x": "passed"},
    "output_sha256": "<sha256 of the full output>", "output_tail": "<last 4000 chars>"
  },
  "counterfactual": {                              // see counterfactual.md
    "command": "pytest -q", "verdict": "does_not_exercise_change", "reason": "...",
    "stashed_paths": ["calc.py"], "kept_paths": ["tests/test_calc.py"],
    "with_change": {"...": "test record"}, "baseline": {"...": "test record"},
    "tests_pass_both": ["..."], "tests_fail_without_change": ["..."],
    "restored": true, "backup_dir": null, "recovery": null
  },
  "summary": {"verified": 4, "contradicted": 2, "unverifiable": 0, "total": 6},
  "integrity": {"note": "sha256 values are integrity hashes for later comparison, not signatures"},
  "receipt_sha256": "<sha256>"
}
```

## Hashes (integrity, not signatures)

- `diff_sha256` = sha256 over `git diff --binary --no-renames <base> -- . ':(exclude)receipt.json'`
  followed, for each untracked file in sorted order, by `\0untracked\0<path>\0<sha256 of content>`.
  Re-run `check` later: a different `diff_sha256` means the change is not the one that was checked.
- `output_sha256` = sha256 of the full captured test output.
- `receipt_sha256` = sha256 of the receipt serialised with `receipt_sha256` set to `null`, using
  `json.dumps(receipt, ensure_ascii=False, indent=2)` (key order as written). It detects accidental
  edits. Anyone can recompute it, so it does **not** prove who made the receipt.

Re-check in Python:

```python
import hashlib, json
r = json.load(open("receipt.json", encoding="utf-8"))
claimed, r["receipt_sha256"] = r["receipt_sha256"], None
body = json.dumps(r, ensure_ascii=False, indent=2)
print(hashlib.sha256(body.encode()).hexdigest() == claimed)
```

## Exit codes

| code | check | run-tests | counterfactual |
| --- | --- | --- | --- |
| 0 | all claims verified | tests passed | tests fail without the change (they exercise it) |
| 1 | at least one claim contradicted | tests failed / timed out | tests pass with AND without the change |
| 2 | bad input | bad input | refused for safety (nothing changed) / bad input |
| 3 | only verified + unverifiable remain, or no claims found | - | inconclusive (no changes, or tests fail even with the change) |
| 4 | - | - | safety abort: restoration not verified; recovery steps printed, changes kept in `git stash list` and `.git/receipts-backup/` |

`--json` prints `{"exit_code": N, "receipt": {...}}` to stdout.
