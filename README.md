# receipts

**Check an AI coding agent's "done" claims against the repository, and keep a JSON receipt.**

An Agent Skill (SKILL.md) plus a single Python script (3.9+, standard library only, no network).
Works with any agent that can run a shell command; ships a Stop hook for Claude Code.

[日本語は下にあります](#日本語)

```text
report: Fixed add() in calc.py and added tests/test_calc.py. All 3 tests passed.

receipts 1.0.0: 3 claims - 1 verified, 2 contradicted, 0 unverifiable
  [CONTRADICTED] file_changed: tests/test_calc.py does not exist
  [CONTRADICTED] tests_passed: claimed 3 tests passed, the last run shows 1 (Ran 1 test in 0.000s / OK)
  [VERIFIED] file_changed: calc.py changed (+1 -1)

counterfactual: WARNING: tests pass with AND without the change - they do not exercise it
      - test_smoke.T.test_true
```

That is `sh examples/demo.sh` (abridged; about 10 seconds, runs in a temp directory).

## What it checks

| claim in the report | checked against |
| --- | --- |
| "All 42 tests passed", 「テスト 15 件すべてパス」 | a real test run (`--run-tests`) or the session log: exit code + parsed summary (pytest, unittest, jest, vitest, mocha, go test, cargo test) |
| "Ran `npm test`, it passed", "lint is clean", 「ビルドが成功」 | the command in the log and its exit code |
| "Created / updated / deleted `path`" | file existence and `git status` / `git diff` against the base |
| "Added function `parse_date`", 「クラス `UserRepository` を追加」 | Python AST (exists, right kind, new or changed); other languages: definition search, low confidence |
| "Added 4 tests", "Modified 2 files" | counted from the AST / git |

Each claim becomes `verified`, `contradicted` or `unverifiable`, with evidence. Hedged or negated
sentences ("should pass", "I did not run the tests", 「未実行」) are not claims.

**Counterfactual mode** answers a different question: *do the tests notice if the change is
removed?* It stashes the non-test changes, runs the tests on the baseline, restores everything,
and verifies the restoration with a sha256 fingerprint of the whole working tree. It refuses to run
during a merge/rebase or in any other unsafe state, and never deletes your changes.

## Install

**Claude Code** (global skill):

```sh
git clone https://github.com/japan-ai-tools/receipts ~/.claude/skills/receipts
```

Optional Stop hook (blocks a "done" message only when a claim is contradicted, with a loop guard):
see [references/claude-code-hook.md](references/claude-code-hook.md).

**Other agents (Codex CLI, Cursor, Gemini CLI, ...)**: if your agent loads SKILL.md skills, copy
or clone this folder into its skills directory (see that tool's documentation for the location).
Otherwise, clone it anywhere and either tell the agent to follow `SKILL.md`, or run the script
yourself:

```sh
python3 receipts/scripts/receipts.py check --claims report.md --run-tests "pytest -q"
python3 receipts/scripts/receipts.py check --claims report.md --log commands.jsonl
python3 receipts/scripts/receipts.py counterfactual --cmd "pytest -q"
```

`--log` accepts a Claude Code transcript, a JSONL file of `{"command", "exit_code", "output"}`
objects (easy to produce from any agent or CI), or a plain `$ command` text log.

## Output

`receipt.json` in the repository root (excluded from the diff it describes): tool, version,
created_at, repo_head, diff_sha256, claims with evidence, tests, counterfactual, summary. The
sha256 values are integrity hashes for later comparison. They are **not signatures** and do not
prove who produced a receipt. Format: [references/receipt-format.md](references/receipt-format.md).

Exit codes: `0` all verified, `1` something contradicted, `2` bad input / refused, `3` only
unverifiable claims remain (or none found), `4` counterfactual safety abort (recovery steps are
printed; your changes are kept in `git stash list` and `.git/receipts-backup/`).

Human summary in English by default, Japanese with `--lang ja`; `--json` prints the full receipt.

## Bench

`bench/` has 60 seeded cases (30 lying, 30 honest, English and Japanese), each a small git repo
plus a report, and a runner that measures detection rate and false-positive rate for receipts or
**any other checker** (see [bench/README.md](bench/README.md) for the format).

Current result: **86.7 % detection (26/30), 0.0 % false positives (0/30)**. The cases and the tool
come from the same author, so treat this as a sanity check, not an independent evaluation.

## Limits (please read)

- **Facts, not correctness.** "Fixed the timezone bug" is verified only as far as "the file
  changed". receipts does not judge whether code is right, and does not use an LLM.
- **Pattern-based extraction.** Claims phrased in ways it does not recognise are skipped (not
  flagged). English and Japanese only.
- **No evidence, no verdict.** Without `--run-tests` or a log, test and command claims are
  `unverifiable`. A plain text log is treated as incomplete.
- **Commits blur the picture.** If a claimed file changed only in the last commit, receipts says
  `unverifiable` unless a complete transcript shows the agent made no commit, or you pass `--base`.
- **Non-Python definitions** are found by text search (low confidence). No TypeScript AST.
- **Counterfactual** handles uncommitted changes only; it runs your test command twice, and cannot
  undo side effects outside the repository (databases, services). Ignored files are not touched.
- The Stop hook uses the commands Claude already ran; it does not run tests itself.
- Per-claim reasons are in English even with `--lang ja`.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 bench/run.py
```

## License

MIT, (c) 2026 NEXUS tools. See [LICENSE](LICENSE).

receipts is an independent project by NEXUS tools. It is not affiliated with, endorsed by, or
sponsored by the makers of any agent or tool mentioned here; product names are trademarks of their
respective owners.

---

## 日本語

**AI コーディングエージェントの「完了しました」を、リポジトリの実際の状態と照らし合わせて確認し、JSON のレシートとして残すツールです。**

Agent Skill (SKILL.md) と Python スクリプト 1 本でできています(Python 3.9 以上、標準ライブラリのみ、ネットワーク通信なし)。シェルコマンドを実行できるエージェントなら使えます。Claude Code 用の Stop フックも付いています。

### 10 秒デモ

```sh
sh examples/demo.sh
```

一時ディレクトリに小さなリポジトリを作り、「tests/test_calc.py を追加、テスト 3 件パス」という報告を検証します。結果は、ファイルが存在しないので「矛盾」、実際のテストは 1 件なので「矛盾」です。さらに反実仮想テストで「変更がなくてもテストが通る(テストが変更を検証していない)」と警告します。

### 確認できること

| 報告の中の主張 | 照合先 |
| --- | --- |
| 「テスト 15 件すべてパスしました」「全テストが通りました」 | 実際のテスト実行(`--run-tests`)またはログ。終了コードとサマリー行を見ます(pytest / unittest / jest / vitest / mocha / go test / cargo test) |
| 「`npm test` が成功」「ビルドが成功」「lint はクリーン」 | ログにあるコマンドとその終了コード |
| 「`path` を作成 / 修正 / 削除しました」 | ファイルの有無と `git status` / `git diff` |
| 「関数 `parse_date` を追加」「クラス `UserRepository` を追加」 | Python は AST(存在するか、種類が合っているか、新規または変更か)。他の言語はテキスト検索(信頼度: 低) |
| 「テストを 4 件追加」「2 ファイルを変更」 | AST と git から数えます |

各主張は `verified`(検証済み)、`contradicted`(矛盾)、`unverifiable`(検証不能)のどれかになり、根拠が付きます。「〜のはずです」「テストは未実行です」のような推量や否定の文は主張として扱いません。

**反実仮想モード(counterfactual)** では、テスト以外の変更を一時的に stash し、変更前の状態でテストを実行してから元に戻します。戻した後に作業ツリー全体の sha256 を取り、実行前と一致するか確かめます。マージ中・リベース中など危険な状態では実行を拒否します。変更を消すことはありません。

### インストール

**Claude Code**(全プロジェクト共通のスキル):

```sh
git clone https://github.com/japan-ai-tools/receipts ~/.claude/skills/receipts
```

Stop フック(矛盾があるときだけ完了報告を止めます。無限ループ防止付き)の設定は [references/claude-code-hook.md](references/claude-code-hook.md) を見てください。

**他のエージェント(Codex CLI、Cursor、Gemini CLI など)**: SKILL.md 形式のスキルを読み込めるなら、このフォルダをそのツールのスキル用ディレクトリに置いてください(場所は各ツールのドキュメントを確認してください)。読み込めない場合は、好きな場所に clone して、エージェントに `SKILL.md` の手順に従うよう指示するか、スクリプトを直接実行します。

```sh
python3 receipts/scripts/receipts.py check --claims report.md --run-tests "pytest -q" --lang ja
python3 receipts/scripts/receipts.py counterfactual --cmd "pytest -q" --lang ja
```

### 出力

リポジトリ直下に `receipt.json` を書き出します。中身は tool, version, created_at, repo_head, diff_sha256, 根拠付きの claims, tests, counterfactual, summary です。sha256 は後から変更の有無を確かめるための整合性ハッシュで、**署名ではありません**。誰が作ったかの証明にはなりません。

終了コード: `0` すべて検証済み、`1` 矛盾あり、`2` 入力エラーまたは実行拒否、`3` 検証不能のみ(または主張なし)、`4` 反実仮想モードの安全停止(復旧手順を表示します。変更は `git stash list` と `.git/receipts-backup/` に残っています)。

### ベンチマーク

`bench/` に 60 ケース(嘘 30、正直 30、英語と日本語)と、検出率・誤検出率を測るスクリプトがあります。形式は [bench/README.md](bench/README.md) にあり、receipts 以外のツールも同じ方法で測れます。

現在の結果: **検出率 86.7 %(26/30)、誤検出率 0.0 %(0/30)**。ケースとツールの作者が同じなので、独立した評価ではなく動作確認として見てください。

### 制限

- **確認するのは事実で、正しさではありません。** 「タイムゾーンのバグを直した」は「ファイルが変わった」ところまでしか確認しません。LLM による判定もしません。
- **パターンで抽出します。** 想定外の書き方の主張は飛ばします(矛盾扱いにはしません)。対応言語は英語と日本語です。
- **根拠がなければ判定しません。** `--run-tests` やログがない場合、テストやコマンドの主張は「検証不能」になります。
- **コミット済みの変更は判断が難しくなります。** 主張されたファイルが直前のコミットでだけ変わっている場合は「検証不能」になります。エージェントがコミットしていないことがトランスクリプトで分かるとき、または `--base` を指定したときは判定できます。
- Python 以外の定義はテキスト検索です。TypeScript の AST 解析はしません。
- 反実仮想モードはコミット前の変更だけが対象です。テストを 2 回実行します。リポジトリ外への副作用(DB など)は元に戻せません。
- `--lang ja` でも、各主張の理由文は英語です。

### ライセンス

MIT、(c) 2026 NEXUS tools。receipts は NEXUS tools による独立したプロジェクトです。ここに名前が出てくるエージェントやツールの提供元とは関係がなく、承認や支援も受けていません。製品名は各社の商標です。
