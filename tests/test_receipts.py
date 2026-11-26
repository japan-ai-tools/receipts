"""Tests for receipts (stdlib unittest). Run: python3 -m unittest discover -s tests -v"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import receipts  # noqa: E402
import stop_hook  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}
PY = sys.executable


def git(repo, *args, check=True):
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"] + list(args),
                          cwd=repo, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def write(repo, rel, content):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(content)


def read(repo, rel, mode="r"):
    with open(os.path.join(repo, rel), mode) as fh:
        return fh.read()


CALC = "def add(a, b):\n    return a + b\n"
CALC_BUG = "def add(a, b):\n    return a - b  # bug\n"
TEST_CALC = ("import unittest\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n")
TEST_TRIVIAL = ("import unittest\n\n\nclass TrivialTest(unittest.TestCase):\n"
                "    def test_true(self):\n        self.assertTrue(True)\n")
UNITTEST_CMD = "%s -m unittest discover -s tests -t . -v" % PY


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="receipts-test-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        write(self.repo, ".gitignore", "__pycache__/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, files, msg="base"):
        for rel, content in files.items():
            write(self.repo, rel, content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)

    def check(self, text, log=None, **kw):
        return receipts.run_check(text, self.repo, log_path=log, **kw)

    def statuses(self, receipt, kind=None):
        return [c["status"] for c in receipt["claims"] if kind is None or c["kind"] == kind]

    def write_log(self, entries):
        path = os.path.join(self.tmp, "log.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return path


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

class ExtractionTests(unittest.TestCase):
    def kinds(self, text):
        return [(c["kind"], c.get("path") or c.get("name") or c.get("command") or c.get("keyword")
                 or c.get("count") or c.get("n")) for c in receipts.extract_claims(text)]

    def test_en_tests_count(self):
        c = receipts.extract_claims("All 42 tests passed.")
        self.assertEqual(c[0]["kind"], "tests_passed")
        self.assertEqual(c[0]["count"], 42)

    def test_en_tests_no_count(self):
        c = receipts.extract_claims("Everything works and all tests pass.")
        self.assertEqual([x["kind"] for x in c], ["tests_passed"])
        self.assertIsNone(c[0]["count"])

    def test_en_suite_with_parenthetical_count(self):
        c = receipts.extract_claims("The vitest suite passes (24 tests).")
        self.assertEqual(c[0]["kind"], "tests_passed")
        self.assertEqual(c[0]["count"], 24)

    def test_en_failed_and_passed(self):
        c = receipts.extract_claims("Unit tests: 1 failed, 11 passed.")
        self.assertEqual(c[0]["count"], 11)
        self.assertEqual(c[0]["claimed_failed"], 1)

    def test_en_files(self):
        k = self.kinds("Created src/cache.py and updated README.md. Deleted old_api.py.")
        self.assertIn(("file_changed", "src/cache.py"), k)
        self.assertIn(("file_changed", "README.md"), k)
        self.assertIn(("file_changed", "old_api.py"), k)
        actions = {c["path"]: c["action"] for c in receipts.extract_claims(
            "Created src/cache.py and updated README.md. Deleted old_api.py.")}
        self.assertEqual(actions, {"src/cache.py": "create", "README.md": "modify", "old_api.py": "delete"})

    def test_en_symbol_and_command(self):
        k = self.kinds("Added a `parse_date()` function to utils.py. Ran `npm test` and it passed.")
        self.assertIn(("symbol_added", "parse_date"), k)
        self.assertIn(("command_succeeded", "npm test"), k)

    def test_en_class(self):
        c = [x for x in receipts.extract_claims("Added a `RateLimiter` class in limiter.py.")
             if x["kind"] == "symbol_added"]
        self.assertEqual(c[0]["symbol_kind"], "class")
        self.assertEqual(c[0]["file"], "limiter.py")

    def test_en_counts(self):
        k = self.kinds("Added 4 unit tests. Modified 2 files.")
        self.assertIn(("count", 4), k)
        self.assertIn(("count", 2), k)

    def test_en_hedged_and_negated_are_ignored(self):
        self.assertEqual(receipts.extract_claims("The tests should pass once CI runs."), [])
        self.assertEqual(receipts.extract_claims("I did not run the tests."), [])
        self.assertEqual(receipts.extract_claims("I couldn't run the tests, they should pass."), [])

    def test_en_false_friends(self):
        k = self.kinds("Bumped the Node.js note (see https://example.com/docs/setup.md).")
        self.assertFalse([x for x in k if x[0] == "file_changed"])

    def test_ja_tests(self):
        c = receipts.extract_claims("テスト 15 件すべてパスしました。")
        self.assertEqual((c[0]["kind"], c[0]["count"], c[0]["lang"]), ("tests_passed", 15, "ja"))
        c = receipts.extract_claims("全 8 件のテストがパスしました")
        self.assertEqual(c[0]["count"], 8)
        c = receipts.extract_claims("`pytest` を実行し、全てのテストが通りました。")
        self.assertEqual([(x["kind"], x.get("command")) for x in c], [("command_succeeded", "pytest")])

    def test_ja_files_and_symbols(self):
        k = self.kinds("`src/router.py` と `src/handlers.py` を作成しました。utils.py に関数 `normalize` を追加しました。")
        self.assertIn(("file_changed", "src/router.py"), k)
        self.assertIn(("file_changed", "src/handlers.py"), k)
        self.assertIn(("symbol_added", "normalize"), k)
        c = [x for x in receipts.extract_claims("repository.py にクラス `UserRepository` を追加しました。")
             if x["kind"] == "symbol_added"]
        self.assertEqual((c[0]["name"], c[0]["symbol_kind"]), ("UserRepository", "class"))

    def test_ja_build_and_counts(self):
        k = self.kinds("ビルドが成功しました。単体テストを3件追加しました。")
        self.assertIn(("command_succeeded", "build"), k)
        self.assertIn(("count", 3), k)

    def test_ja_negated(self):
        self.assertEqual([x["kind"] for x in receipts.extract_claims("テストは未実行です。")], [])
        self.assertEqual(receipts.extract_claims("テストは通るはずです。"), [])


# ---------------------------------------------------------------------------
# test output parsing
# ---------------------------------------------------------------------------

class ParseOutputTests(unittest.TestCase):
    def test_pytest(self):
        p = receipts.parse_test_output("tests/test_a.py ..F\n===== 1 failed, 2 passed in 0.12s =====")
        self.assertEqual((p["framework"], p["passed"], p["failed"]), ("pytest", 2, 1))

    def test_unittest(self):
        p = receipts.parse_test_output("Ran 6 tests in 0.003s\n\nFAILED (failures=1, errors=1)")
        self.assertEqual((p["framework"], p["passed"], p["failed"], p["errors"]), ("unittest", 4, 1, 1))
        p = receipts.parse_test_output("Ran 3 tests in 0.001s\n\nOK")
        self.assertEqual((p["passed"], p["failed"]), (3, 0))

    def test_jest(self):
        p = receipts.parse_test_output("Tests:       1 failed, 4 passed, 5 total")
        self.assertEqual((p["framework"], p["passed"], p["failed"], p["total"]), ("jest", 4, 1, 5))

    def test_vitest(self):
        p = receipts.parse_test_output("      Tests  2 failed | 22 passed (24)")
        self.assertEqual((p["framework"], p["passed"], p["failed"], p["total"]), ("vitest", 22, 2, 24))

    def test_go(self):
        p = receipts.parse_test_output("--- FAIL: TestX (0.00s)\nFAIL\nFAIL\texample.com/p\t0.01s\n")
        self.assertEqual(p["framework"], "go")
        self.assertGreaterEqual(p["failed"], 1)
        p = receipts.parse_test_output("ok  \texample.com/p\t0.01s\n")
        self.assertEqual((p["framework"], p["failed"]), ("go", 0))

    def test_cargo(self):
        out = ("test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured\n"
               "test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured\n")
        p = receipts.parse_test_output(out)
        self.assertEqual((p["framework"], p["passed"], p["failed"], p["skipped"]), ("cargo", 5, 1, 1))


# ---------------------------------------------------------------------------
# verifiers
# ---------------------------------------------------------------------------

class VerifierTests(RepoCase):
    def test_file_created_modified_missing_unchanged(self):
        self.commit({"app.py": "x = 1\n", "README.md": "# hi\n"})
        write(self.repo, "app.py", "x = 2\n")
        write(self.repo, "src/new.py", "y = 1\n")
        r = self.check("Created src/new.py and src/ghost.py. Updated app.py and README.md.")
        by = {c["path"]: c["status"] for c in r["claims"] if c["kind"] == "file_changed"}
        # README.md changed in the last commit: without a transcript the agent may have committed it
        self.assertEqual(by, {"src/new.py": "verified", "src/ghost.py": "contradicted",
                              "app.py": "verified", "README.md": "unverifiable"})
        # an explicit --base, or a complete transcript with no `git commit`, removes that doubt
        r = self.check("Updated README.md.", base="HEAD")
        self.assertEqual(self.statuses(r), ["contradicted"])
        log = self.write_log([{"command": "pytest", "exit_code": 0, "output": "== 1 passed in 0.1s =="}])
        self.assertEqual(self.statuses(self.check("Updated README.md.", log=log)), ["contradicted"])
        log = self.write_log([{"command": "git commit -am wip", "exit_code": 0, "output": ""}])
        self.assertEqual(self.statuses(self.check("Updated README.md.", log=log)), ["unverifiable"])

    def test_file_deleted(self):
        self.commit({"old.py": "x = 1\n", "keep.py": "y = 1\n"})
        os.remove(os.path.join(self.repo, "old.py"))
        r = self.check("Deleted old.py and removed keep.py.")
        by = {c["path"]: c["status"] for c in r["claims"]}
        self.assertEqual(by, {"old.py": "verified", "keep.py": "contradicted"})

    def test_symbol_python_ast(self):
        self.commit({"utils.py": "def strip(s):\n    return s.strip()\n", "text.py": "def slugify(s):\n    return s\n"})
        write(self.repo, "utils.py", read(self.repo, "utils.py") + "\n\ndef parse_date(s):\n    return s\n\n\n"
              "def RateLimiter():\n    return 1\n")
        r = self.check("Added `parse_date()` to utils.py. Added function `normalize` to utils.py. "
                       "Added a `RateLimiter` class in utils.py. Added function `slugify` to text.py.")
        by = {c["name"]: c for c in r["claims"] if c["kind"] == "symbol_added"}
        self.assertEqual(by["parse_date"]["status"], "verified")
        self.assertEqual(by["parse_date"]["evidence"][-1]["type"], "ast")
        self.assertEqual(by["normalize"]["status"], "contradicted")
        self.assertEqual(by["RateLimiter"]["status"], "contradicted")
        self.assertIn("not a class", by["RateLimiter"]["reason"])
        self.assertEqual(by["slugify"]["status"], "unverifiable")  # text.py changed in the last commit
        r = self.check("Added function `slugify` to text.py.", base="HEAD")
        self.assertEqual(self.statuses(r, "symbol_added"), ["contradicted"])  # existed before, unchanged

    def test_symbol_other_language_low_confidence(self):
        self.commit({"src/v.ts": "export const A = 1;\n"})
        write(self.repo, "src/v.ts", "export const A = 1;\nexport function validateEmail(s: string) {\n  return s;\n}\n")
        r = self.check("Added `validateEmail` helper in src/v.ts. Added `isPhone` helper in src/v.ts.")
        by = {c["name"]: c for c in r["claims"] if c["kind"] == "symbol_added"}
        self.assertEqual((by["validateEmail"]["status"], by["validateEmail"]["confidence"]), ("verified", "low"))
        self.assertEqual(by["isPhone"]["status"], "contradicted")

    def test_tests_with_log(self):
        self.commit({"a.py": "x = 1\n"})
        ok = self.write_log([{"command": "pytest -q", "exit_code": 0, "output": "===== 12 passed in 0.1s ====="}])
        self.assertEqual(self.statuses(self.check("All 12 tests passed.", log=ok)), ["verified"])
        self.assertEqual(self.statuses(self.check("All 15 tests passed.", log=ok)), ["contradicted"])
        bad = self.write_log([{"command": "pytest", "exit_code": 1, "output": "== 1 failed, 11 passed in 0.2s =="}])
        self.assertEqual(self.statuses(self.check("All tests pass.", log=bad)), ["contradicted"])
        self.assertEqual(self.statuses(self.check("1 failed, 11 passed (known flaky).", log=bad)), ["verified"])
        none = self.write_log([{"command": "git status", "exit_code": 0, "output": ""}])
        self.assertEqual(self.statuses(self.check("All tests pass.", log=none)), ["contradicted"])
        self.assertEqual(self.statuses(self.check("All tests pass.")), ["unverifiable"])

    def test_tests_with_run_tests(self):
        self.commit({"calc.py": CALC, "tests/__init__.py": "", "tests/test_calc.py": TEST_CALC})
        r = self.check("All tests pass.", run_tests_cmd=UNITTEST_CMD)
        self.assertEqual(self.statuses(r), ["verified"])
        write(self.repo, "calc.py", CALC_BUG)
        r = self.check("All tests pass.", run_tests_cmd=UNITTEST_CMD)
        self.assertEqual(self.statuses(r), ["contradicted"])

    def test_claude_code_transcript(self):
        self.commit({"a.py": "x = 1\n"})
        lines = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "Exit code 1\nTests:       1 failed, 4 passed, 5 total"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Done! `npm test` passes."}]}},
        ]
        path = os.path.join(self.tmp, "t.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
        log = receipts.parse_log(path)
        self.assertEqual(log["kind"], "transcript")
        self.assertEqual(log["runs"][0]["exit_code"], 1)
        self.assertEqual(log["last_assistant_message"], "Done! `npm test` passes.")
        r = self.check(log["last_assistant_message"], log=path)
        self.assertIn("contradicted", self.statuses(r, "command_succeeded"))

    def test_command_text_log(self):
        self.commit({"a.py": "x = 1\n"})
        path = os.path.join(self.tmp, "log.txt")
        with open(path, "w") as fh:
            fh.write("$ ruff check .\napp.py:1:8: F401 unused\nFound 1 error.\nexit code: 1\n")
        r = self.check("Lint is clean.", log=path)
        self.assertEqual(self.statuses(r), ["contradicted"])

    def test_counts(self):
        self.commit({"tests/test_m.py": "def test_a():\n    pass\n", "m.py": "x = 1\n"})
        write(self.repo, "tests/test_m.py", read(self.repo, "tests/test_m.py") +
              "\n\ndef test_b():\n    pass\n\n\ndef test_c():\n    pass\n")
        write(self.repo, "n.py", "y = 1\n")
        r = self.check("Added 2 tests. Created 1 new file.")
        self.assertEqual(self.statuses(r, "count"), ["verified", "verified"])
        r = self.check("Added 4 tests. Created 3 new files.")
        self.assertEqual(self.statuses(r, "count"), ["contradicted", "contradicted"])

    def test_clean_tree_uses_last_commit(self):
        self.commit({"a.py": "x = 1\n"})
        self.commit({"text.py": "def slugify(s):\n    return s\n"}, msg="work")
        r = self.check("Added function `slugify` to text.py.")
        self.assertEqual(set(self.statuses(r)), {"verified"})
        self.assertIn("HEAD~1", r["repo"]["base_note"])

    def test_not_a_git_repo(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        write(plain, "a.py", "def f():\n    pass\n")
        r = receipts.run_check("Created a.py and b.py.", plain)
        by = {c["path"]: c["status"] for c in r["claims"]}
        self.assertEqual(by, {"a.py": "unverifiable", "b.py": "contradicted"})
        self.assertFalse(r["repo"]["is_git"])
        self.assertIn("not a git repository", receipts.human_summary(r))

    def test_receipt_fields_and_hashes(self):
        self.commit({"a.py": "x = 1\n"})
        write(self.repo, "a.py", "x = 2\n")
        out = os.path.join(self.repo, "receipt.json")
        r = self.check("Updated a.py.", out_path=out)
        receipts.write_receipt(r, out)
        data = json.load(open(out))
        for key in ("tool", "version", "created_at", "repo_head", "diff_sha256", "claims", "tests",
                    "counterfactual", "summary"):
            self.assertIn(key, data)
        self.assertEqual(len(data["diff_sha256"]), 64)
        self.assertFalse([k for k in data if "sign" in k.lower()])
        self.assertIn("not signatures", data["integrity"]["note"])
        # receipt.json itself is excluded from the change
        r2 = self.check("Updated a.py.", out_path=out)
        self.assertEqual(r2["diff_sha256"], r["diff_sha256"])


# ---------------------------------------------------------------------------
# counterfactual safety
# ---------------------------------------------------------------------------

class CounterfactualTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.commit({"calc.py": CALC_BUG, "tests/__init__.py": "", "tests/test_trivial.py": TEST_TRIVIAL,
                     "data.bin": b"\x00\x01\x02\xff" * 10})

    def snapshot(self):
        return receipts.worktree_fingerprint(self.repo)["digest"]

    def test_tests_do_not_exercise_change(self):
        write(self.repo, "calc.py", CALC)
        before = self.snapshot()
        r, code = receipts.run_counterfactual(UNITTEST_CMD, self.repo)
        self.assertEqual(code, receipts.EXIT_CONTRADICTED)
        cf = r["counterfactual"]
        self.assertEqual(cf["verdict"], "does_not_exercise_change")
        self.assertTrue(any("test_true" in n for n in cf["tests_pass_both"]))
        self.assertTrue(cf["restored"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(read(self.repo, "calc.py"), CALC)
        self.assertEqual(git(self.repo, "stash", "list").stdout, b"")

    def test_tests_exercise_change_and_untracked_test_kept(self):
        write(self.repo, "calc.py", CALC)
        write(self.repo, "tests/test_calc.py", TEST_CALC)  # untracked new test file (kept during baseline)
        before = self.snapshot()
        r, code = receipts.run_counterfactual(UNITTEST_CMD, self.repo)
        self.assertEqual(code, receipts.EXIT_OK, r["counterfactual"])
        cf = r["counterfactual"]
        self.assertEqual(cf["verdict"], "exercises_change")
        self.assertTrue(any("test_add" in n for n in cf["tests_fail_without_change"]))
        self.assertTrue(any("test_true" in n for n in cf["tests_pass_both"]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(read(self.repo, "tests/test_calc.py"), TEST_CALC)

    def test_preserves_uncommitted_untracked_binary_and_index(self):
        write(self.repo, "calc.py", CALC)
        write(self.repo, "notes/untracked.txt", "my notes\n")
        write(self.repo, "data.bin", b"\xff\xfe\x00binary-change\x00" * 50)
        write(self.repo, "new.bin", bytes(range(256)) * 4)
        write(self.repo, "staged.py", "z = 3\n")
        git(self.repo, "add", "staged.py")
        os.chmod(os.path.join(self.repo, "calc.py"), 0o755)
        before = self.snapshot()
        index_before = git(self.repo, "diff", "--cached", "--name-only").stdout
        r, code = receipts.run_counterfactual(UNITTEST_CMD, self.repo)
        self.assertIn(code, (receipts.EXIT_OK, receipts.EXIT_CONTRADICTED), r["counterfactual"])
        self.assertTrue(r["counterfactual"]["restored"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(read(self.repo, "notes/untracked.txt"), "my notes\n")
        self.assertEqual(read(self.repo, "new.bin", "rb"), bytes(range(256)) * 4)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only").stdout, index_before)
        self.assertTrue(os.access(os.path.join(self.repo, "calc.py"), os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "receipts-backup")))

    def test_baseline_run_side_effects_are_undone(self):
        write(self.repo, "calc.py", CALC)
        write(self.repo, "tests/test_calc.py", TEST_CALC)
        before = self.snapshot()
        # a "test command" that vandalises the tree only on the baseline (buggy calc.py)
        vandal = os.path.join(self.tmp, "vandal.py")
        with open(vandal, "w") as fh:
            fh.write("src = open('calc.py').read()\n"
                     "if 'a - b' in src:\n"
                     "    open('tests/test_calc.py', 'a').write('# junk\\n')\n"
                     "    open('tests/test_trivial.py', 'a').write('# junk\\n')\n"
                     "    open('made_by_run.txt', 'w').write('x')\n")
        cmd = "%s; %s %s" % (UNITTEST_CMD, PY, vandal)
        r, code = receipts.run_counterfactual(cmd, self.repo)
        self.assertTrue(r["counterfactual"]["restored"], r["counterfactual"])
        self.assertIsNone(r["counterfactual"].get("error"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(read(self.repo, "tests/test_calc.py"), TEST_CALC)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "made_by_run.txt")))

    def test_refuses_without_git(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        write(plain, "a.py", "x = 1\n")
        r, code = receipts.run_counterfactual("true", plain)
        self.assertEqual(code, receipts.EXIT_BAD_INPUT)
        self.assertEqual(r["counterfactual"]["verdict"], "refused")
        self.assertIn("not a git repository", r["counterfactual"]["reason"])
        self.assertEqual(read(plain, "a.py"), "x = 1\n")

    def test_refuses_during_merge(self):
        git(self.repo, "checkout", "-q", "-b", "other")
        self.commit({"calc.py": "def add(a, b):\n    return 0\n"}, msg="other")
        git(self.repo, "checkout", "-q", "main")
        self.commit({"calc.py": "def add(a, b):\n    return 1\n"}, msg="main")
        git(self.repo, "merge", "other", check=False)
        self.assertTrue(os.path.exists(os.path.join(self.repo, ".git", "MERGE_HEAD")))
        before = self.snapshot()
        r, code = receipts.run_counterfactual("true", self.repo)
        self.assertEqual(code, receipts.EXIT_BAD_INPUT)
        self.assertIn("merge", r["counterfactual"]["reason"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(git(self.repo, "stash", "list").stdout, b"")

    def test_refuses_during_rebase(self):
        write(self.repo, "calc.py", CALC)
        os.makedirs(os.path.join(self.repo, ".git", "rebase-merge"))
        r, code = receipts.run_counterfactual("true", self.repo)
        self.assertEqual(code, receipts.EXIT_BAD_INPUT)
        self.assertIn("rebase", r["counterfactual"]["reason"])
        self.assertEqual(read(self.repo, "calc.py"), CALC)

    def test_no_changes(self):
        r, code = receipts.run_counterfactual("true", self.repo)
        self.assertEqual(code, receipts.EXIT_UNVERIFIABLE)
        self.assertEqual(r["counterfactual"]["verdict"], "no_changes")

    def test_restoration_mismatch_stops_with_recovery_steps(self):
        write(self.repo, "calc.py", CALC)
        write(self.repo, "extra.txt", "untracked user work\n")
        real = receipts.worktree_fingerprint
        calls = {"n": 0}

        def flaky(root):
            calls["n"] += 1
            fp = real(root)
            if calls["n"] >= 4:  # the final post-restore check
                fp = dict(fp, digest="0" * 64)
            return fp

        with mock.patch.object(receipts, "worktree_fingerprint", side_effect=flaky):
            r, code = receipts.run_counterfactual(UNITTEST_CMD, self.repo)
        self.assertEqual(code, receipts.EXIT_SAFETY)
        cf = r["counterfactual"]
        self.assertEqual(cf["verdict"], "safety_abort")
        self.assertIn("git stash list", cf["recovery"])
        self.assertIn("git stash apply", cf["recovery"])
        # nothing was lost: stash still present and a backup copy exists
        self.assertIn(b"receipts-counterfactual", git(self.repo, "stash", "list").stdout)
        self.assertTrue(os.path.isdir(cf["backup_dir"]))
        self.assertEqual(read(cf["backup_dir"], "files/extra.txt"), "untracked user work\n")

    def test_stash_lost_reports_backup(self):
        write(self.repo, "calc.py", CALC)

        def drop(root):
            git(root, "stash", "drop")

        r, code = receipts.run_counterfactual(UNITTEST_CMD, self.repo, _hook_after_stash=drop)
        self.assertEqual(code, receipts.EXIT_SAFETY)
        cf = r["counterfactual"]
        self.assertIn("backed up", cf["recovery"])
        self.assertEqual(read(cf["backup_dir"], "files/calc.py"), CALC)


# ---------------------------------------------------------------------------
# Stop hook
# ---------------------------------------------------------------------------

class HookTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp, "state")
        self.env = mock.patch.dict(os.environ, {"RECEIPTS_STATE_DIR": self.state}, clear=False)
        self.env.start()
        os.environ.pop("RECEIPTS_MAX_BLOCKS", None)
        self.commit({"app.py": "x = 1\n"})

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def payload(self, msg, active=False, session="s1"):
        return {"session_id": session, "transcript_path": "/nonexistent/t.jsonl", "cwd": self.repo,
                "hook_event_name": "Stop", "stop_hook_active": active, "last_assistant_message": msg}

    def run_hook(self, payload):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = stop_hook.main(io.StringIO(json.dumps(payload)), out)
        return code, out.getvalue(), err.getvalue()

    def test_blocks_on_contradiction(self):
        code, out, _ = self.run_hook(self.payload("Done. I created src/cache.py."))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("src/cache.py", data["reason"])

    def test_allows_when_verified_or_unverifiable(self):
        write(self.repo, "app.py", "x = 2\n")
        code, out, _ = self.run_hook(self.payload("Updated app.py. All tests pass."))
        self.assertEqual((code, out), (0, ""))

    def test_loop_guard_same_contradiction(self):
        msg = "Done. I created src/cache.py."
        self.assertIn("block", self.run_hook(self.payload(msg))[1])
        code, out, err = self.run_hook(self.payload(msg, active=True))
        self.assertEqual(out, "")
        self.assertIn("loop guard", err)

    def test_loop_guard_max_blocks(self):
        self.assertIn("block", self.run_hook(self.payload("I created a.py."))[1])
        self.assertIn("block", self.run_hook(self.payload("I created b.py.", active=True))[1])
        self.assertEqual(self.run_hook(self.payload("I created c.py.", active=True))[1], "")
        # a fresh (non-hook-driven) stop resets the counter
        self.assertIn("block", self.run_hook(self.payload("I created d.py."))[1])

    def test_report_only_mode(self):
        with mock.patch.dict(os.environ, {"RECEIPTS_MAX_BLOCKS": "0"}):
            self.assertEqual(self.run_hook(self.payload("I created a.py."))[1], "")

    def test_japanese_reason(self):
        data = json.loads(self.run_hook(self.payload("src/cache.py を作成しました。"))[1])
        self.assertIn("矛盾", data["reason"])

    def test_bad_input_never_blocks(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = stop_hook.main(io.StringIO("{not json"), out)
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_reads_transcript_when_message_missing(self):
        path = os.path.join(self.tmp, "t.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I created ghost.py."}]}}) + "\n")
        p = self.payload(None)
        p["transcript_path"] = path
        self.assertIn("ghost.py", json.loads(self.run_hook(p)[1])["reason"])

    def test_hook_as_subprocess(self):
        proc = subprocess.run([PY, os.path.join(ROOT, "hooks", "stop_hook.py")],
                              input=json.dumps(self.payload("I created ghost.py.")).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------

class ExitCodeTests(RepoCase):
    def cli(self, *args):
        proc = subprocess.run([PY, os.path.join(ROOT, "scripts", "receipts.py")] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.repo)
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def setUp(self):
        super().setUp()
        self.commit({"app.py": "x = 1\n"})
        write(self.repo, "app.py", "x = 2\n")

    def test_0_verified(self):
        self.assertEqual(self.cli("check", "--no-write", "--", "Updated app.py.")[0], 0)

    def test_1_contradicted(self):
        self.assertEqual(self.cli("check", "--no-write", "--", "Created ghost.py.")[0], 1)

    def test_2_bad_input(self):
        self.assertEqual(self.cli("check", "--no-write")[0], 2)
        self.assertEqual(self.cli("check", "--claims", "/nonexistent/x.txt")[0], 2)
        self.assertEqual(self.cli("bogus")[0], 2)
        self.assertEqual(self.cli("check", "--no-write", "--base", "nope", "--", "Updated app.py.")[0], 2)

    def test_3_unverifiable(self):
        self.assertEqual(self.cli("check", "--no-write", "--", "All tests pass.")[0], 3)
        self.assertEqual(self.cli("check", "--no-write", "--", "Hello there.")[0], 3)

    def test_json_and_lang(self):
        code, out, _ = self.cli("check", "--no-write", "--json", "--", "Updated app.py.")
        data = json.loads(out)
        self.assertEqual((data["exit_code"], data["receipt"]["summary"]["verified"]), (0, 1))
        code, out, _ = self.cli("check", "--no-write", "--lang", "ja", "--", "Created ghost.py.")
        self.assertIn("矛盾", out)

    def test_help(self):
        for sub in ("check", "run-tests", "counterfactual"):
            code, out, _ = self.cli(sub, "--help")
            self.assertEqual(code, 0)
            self.assertIn("--json", out)

    def test_run_tests_exit(self):
        self.assertEqual(self.cli("run-tests", "--no-write", "--cmd", "true")[0], 0)
        self.assertEqual(self.cli("run-tests", "--no-write", "--cmd", "false")[0], 1)
        code, out, _ = self.cli("run-tests", "--no-write", "--json", "--timeout", "1", "--cmd", "sleep 5")
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(out)["receipt"]["tests"]["timed_out"])

    def test_writes_receipt_by_default(self):
        self.cli("check", "--", "Updated app.py.")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "receipt.json")))


if __name__ == "__main__":
    unittest.main()


class GeneratedFilesTests(RepoCase):
    """A repo with no .gitignore: bytecode written by a test run must not change the receipt's diff fingerprint."""

    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.repo, ".gitignore"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "no ignore")

    def test_pyc_does_not_change_fingerprint_or_claims(self):
        write(self.repo, "calc.py", "def add(a, b):\n    return a + b\n")
        r1 = self.check("Created calc.py.")
        write(self.repo, "__pycache__/calc.cpython-313.pyc", "\x00bytecode")
        write(self.repo, "pkg/__pycache__/x.pyc", "\x00")
        write(self.repo, ".pytest_cache/v/cache/lastfailed", "{}")
        r2 = self.check("Created calc.py.")
        self.assertEqual(r1["diff_sha256"], r2["diff_sha256"])
        self.assertEqual(self.statuses(r2), ["verified"])
        write(self.repo, "notes.txt", "real new file")
        self.assertNotEqual(r1["diff_sha256"], self.check("Created calc.py.")["diff_sha256"])

    def test_generated_pattern(self):
        for p in ("__pycache__/a.pyc", "x/__pycache__/a.cpython-311.pyc", "a.pyo", ".pytest_cache/v/x", ".coverage"):
            self.assertTrue(receipts.GENERATED_RE.search(p), p)
        for p in ("pycache.py", "src/cache.py", "coverage.md", "tests/test_x.py"):
            self.assertFalse(receipts.GENERATED_RE.search(p), p)
