#!/usr/bin/env python3
"""receipts - verify an AI coding agent's "done" claims and write a JSON receipt.

Standard library only. No network access. Never destructive: the counterfactual
mode stashes changes, runs tests on the baseline, and always restores the working
tree, verifying the restoration with a content fingerprint.

Subcommands:
  check           extract claims from a completion report and verify them
  run-tests       run a test command and record exit code + parsed counts
  counterfactual  run tests with and without the change to see if tests exercise it

Exit codes (stable):
  0  all claims verified (counterfactual: tests exercise the change)
  1  at least one claim contradicted (counterfactual: tests do not exercise it;
     run-tests: tests failed or timed out)
  2  bad input, or refused for safety before anything was changed
  3  nothing contradicted, but some claims are unverifiable (or no claims found;
     counterfactual: inconclusive)
  4  safety abort: restoration could not be verified (counterfactual only);
     your changes are kept in `git stash list` and a backup folder
"""

import argparse
import ast
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata

TOOL = "receipts"
VERSION = "1.0.0"

EXIT_OK = 0
EXIT_CONTRADICTED = 1
EXIT_BAD_INPUT = 2
EXIT_UNVERIFIABLE = 3
EXIT_SAFETY = 4

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_OUT = "receipt.json"
MAX_SCAN_FILES = 5000
MAX_FILE_BYTES = 2_000_000
OUTPUT_TAIL = 4000


class BadInput(Exception):
    """Raised for invalid user input (exit code 2)."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8", "replace"))


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def read_text(path, limit=MAX_FILE_BYTES):
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def has_japanese(text):
    return bool(re.search(r"[぀-ヿ一-鿿]", text or ""))


# --------------------------------------------------------------------------
# git access
# --------------------------------------------------------------------------

def _git_env():
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat",
                "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def git_available():
    return shutil.which("git") is not None


def git_run(root, args, input_bytes=None, timeout=120):
    """Run git; returns (returncode, stdout bytes, stderr text)."""
    cmd = ["git", "-c", "core.quotepath=false", "-c", "color.ui=false"] + list(args)
    try:
        proc = subprocess.run(cmd, cwd=root, input=input_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_git_env(), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, b"", str(exc)
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")


def git_out(root, args):
    code, out, _ = git_run(root, args)
    return out.decode("utf-8", "replace") if code == 0 else None


def find_git_root(path):
    if not git_available():
        return None
    out = git_out(path, ["rev-parse", "--show-toplevel"])
    return os.path.realpath(out.strip()) if out else None


def is_test_path(path):
    p = path.replace("\\", "/")
    return bool(re.search(
        r"(^|/)(tests?|__tests__|spec|specs)/"
        r"|(^|/)test_[^/]*\.py$|_test\.(py|go)$|(^|/)conftest\.py$"
        r"|\.(test|spec)\.[cm]?[jt]sx?$|Tests?\.(java|kt|cs)$|_spec\.rb$", p))


class Repo:
    """A view of the repository and of the change under review."""

    def __init__(self, path, base=None, exclude=()):
        self.path = os.path.realpath(path)
        if not os.path.isdir(self.path):
            raise BadInput("repository path does not exist: %s" % path)
        self.git = git_available()
        self.root = find_git_root(self.path)
        self.is_git = self.root is not None
        if not self.is_git:
            self.root = self.path
        self.exclude = set()
        for ex in exclude:
            if ex:
                rel = os.path.relpath(os.path.realpath(ex), self.root)
                if not rel.startswith(".."):
                    self.exclude.add(rel.replace(os.sep, "/"))
        self.head = None
        self.base = None
        self.base_note = None
        self.changes = {}     # path -> 'A' | 'M' | 'D' | 'T' | '?' (untracked)
        self.numstat = {}     # path -> (added, deleted) or (None, None) for binary
        self.base_explicit = bool(base)
        # set by run_check from a complete transcript: True if the agent ran `git commit`,
        # False if it provably did not, None if unknown
        self.agent_committed = None
        if self.is_git:
            self._load(base)

    # -- loading -------------------------------------------------------------
    def _verify_commit(self, ref):
        out = git_out(self.root, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
        return out.strip() if out else None

    def _load(self, base):
        self.head = self._verify_commit("HEAD")
        if base:
            resolved = self._verify_commit(base)
            if not resolved:
                raise BadInput("--base %r is not a commit in this repository" % base)
            self.base = resolved
            self.base_note = "compared with --base %s" % base
            self._load_changes()
            return
        self.base = self.head or EMPTY_TREE
        self.base_note = "compared with HEAD" if self.head else "repository has no commits yet"
        self._load_changes()
        if not self.changes and self.head:
            parent = self._verify_commit("HEAD~1")
            if parent:
                self.base = parent
                self.base_note = ("working tree is clean; compared with HEAD~1 "
                                  "(the last commit is treated as the change)")
                self._load_changes()

    def _pathspec_excludes(self):
        return [":(exclude,literal)%s" % p for p in sorted(self.exclude)]

    def _load_changes(self):
        self.changes, self.numstat = {}, {}
        spec = ["--", "."] + self._pathspec_excludes()
        code, out, _ = git_run(self.root, ["diff", "--name-status", "-z", "--no-renames",
                                           self.base] + spec)
        if code == 0:
            parts = out.decode("utf-8", "replace").split("\0")
            i = 0
            while i + 1 < len(parts):
                status, p = parts[i], parts[i + 1]
                if status:
                    self.changes[p] = status[0]
                i += 2
        code, out, _ = git_run(self.root, ["diff", "--numstat", "-z", "--no-renames",
                                           self.base] + spec)
        if code == 0:
            for rec in out.decode("utf-8", "replace").split("\0"):
                bits = rec.split("\t")
                if len(bits) == 3:
                    a, d, p = bits
                    self.numstat[p] = (None, None) if a == "-" else (int(a), int(d))
        for p in self.untracked():
            if p in self.exclude:
                continue
            self.changes[p] = "?"
            text = read_text(os.path.join(self.root, p))
            self.numstat[p] = (None, None) if text is None else (len(text.splitlines()), 0)

    def untracked(self):
        code, out, _ = git_run(self.root, ["ls-files", "--others", "--exclude-standard", "-z"])
        if code != 0:
            return []
        return [p for p in out.decode("utf-8", "replace").split("\0") if p]

    def tracked(self):
        code, out, _ = git_run(self.root, ["ls-files", "-z"])
        if code != 0:
            return []
        return [p for p in out.decode("utf-8", "replace").split("\0") if p]

    # -- queries -------------------------------------------------------------
    def all_files(self):
        if self.is_git:
            files = list(dict.fromkeys(self.tracked() + list(self.changes)))
            return [f for f in files if f not in self.exclude][:MAX_SCAN_FILES * 4]
        result = []
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                "target", ".tox", ".mypy_cache", ".pytest_cache"}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                result.append(rel.replace(os.sep, "/"))
                if len(result) >= MAX_SCAN_FILES * 4:
                    return result
        return result

    def exists(self, rel):
        return os.path.lexists(os.path.join(self.root, rel))

    def base_content(self, rel):
        if not self.is_git or self.base == EMPTY_TREE:
            return None
        code, out, _ = git_run(self.root, ["show", "%s:%s" % (self.base, rel)])
        return out.decode("utf-8", "replace") if code == 0 else None

    def in_base(self, rel):
        if not self.is_git or self.base == EMPTY_TREE:
            return False
        code, out, _ = git_run(self.root, ["ls-tree", "--name-only", self.base, "--", rel])
        return code == 0 and bool(out.strip())

    def changed_in_head_commit(self, rel):
        """True if the HEAD commit itself touched rel and the agent may have made that commit.

        Used to downgrade "unchanged" to "unverifiable" when the agent might have committed part of
        its work. Not used when --base was given or a complete transcript shows no `git commit`."""
        if not self.is_git or not self.head or self.base != self.head:
            return False
        if self.base_explicit or self.agent_committed is False:
            return False
        out = git_out(self.root, ["diff-tree", "--no-commit-id", "--name-only", "-r",
                                  "--root", "HEAD", "--", rel])
        return bool(out and out.strip())

    def diff_sha256(self):
        if not self.is_git:
            return None
        spec = ["--", "."] + self._pathspec_excludes()
        code, out, _ = git_run(self.root, ["diff", "--binary", "--no-renames", self.base] + spec)
        h = hashlib.sha256(out if code == 0 else b"")
        for p in sorted(k for k, v in self.changes.items() if v == "?"):
            try:
                with open(os.path.join(self.root, p), "rb") as fh:
                    content = fh.read()
            except OSError:
                content = b""
            h.update(b"\0untracked\0" + p.encode() + b"\0" + sha256_bytes(content).encode())
        return h.hexdigest()

    def resolve(self, claimed):
        """Map a path as written in a report to a repo-relative path (or None)."""
        p = claimed.strip().strip("`'\"").rstrip(".,:;)")
        if not p:
            return None
        if os.path.isabs(p):
            rel = os.path.relpath(os.path.realpath(p), self.root)
            return None if rel.startswith("..") else rel.replace(os.sep, "/")
        p = re.sub(r"^\./", "", p)
        for base in (self.root, self.path):
            cand = os.path.relpath(os.path.join(base, p), self.root).replace(os.sep, "/")
            if not cand.startswith("..") and (self.exists(cand) or cand in self.changes):
                return cand
        if self.in_base(p):
            return p
        # basename / suffix match, preferring files in the change
        tail = "/" + p
        changed = [f for f in self.changes if ("/" + f).endswith(tail)]
        if len(changed) == 1:
            return changed[0]
        if not changed:
            others = [f for f in self.all_files() if ("/" + f).endswith(tail)]
            if len(others) == 1:
                return others[0]
        return p  # unresolved: keep as written (relative to root)

    def info(self):
        return {"root": self.root, "is_git": self.is_git, "head": self.head,
                "base": self.base, "base_note": self.base_note,
                "dirty": bool(self.changes) if self.is_git else None}


# --------------------------------------------------------------------------
# test output parsing
# --------------------------------------------------------------------------

TEST_CMD_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:pytest|py\.test|unittest|nosetests|tox|nox|jest|vitest|mocha|"
    r"ava|karma|rspec|phpunit|ctest|go\s+test|cargo\s+(?:test|nextest)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|bun\s+test|deno\s+test|make\s+(?:test|check)|"
    r"gradlew?\s+test|mvn\s+test|dotnet\s+test|swift\s+test|mix\s+test|run_tests|runtests)"
    r"(?:\b|$)")


def is_test_command(cmd):
    return bool(cmd and TEST_CMD_RE.search(cmd))


def _num(m, key):
    v = m.get(key)
    return int(v) if v is not None else None


def parse_test_output(text):
    """Parse common test runner summaries. Returns a dict (values None if unknown)."""
    res = {"framework": None, "passed": None, "failed": None, "errors": None,
           "skipped": None, "total": None, "summary_line": None, "tests": {}}
    if not text:
        return res
    lines = text.splitlines()

    def setres(fw, line, **kw):
        res["framework"] = fw
        res["summary_line"] = line.strip()[:300]
        res.update(kw)

    # per-test outcomes (verbose modes)
    tests = {}
    for line in lines:
        m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", line)
        if not m:
            m2 = re.match(r"^(PASSED|FAILED|ERROR)\s+(\S+::\S+)", line)
            if m2:
                m = None
                tests[m2.group(2)] = m2.group(1).lower()
        if m:
            tests[m.group(1)] = m.group(2).lower()
            continue
        m = re.match(r"^(test\w*) \(([\w.]+)\)(?:.*)? \.\.\. (ok|FAIL|ERROR|skipped|expected failure)",
                     line)
        if m:
            name = m.group(2) if m.group(2).endswith("." + m.group(1)) else m.group(2) + "." + m.group(1)
            st = {"ok": "passed", "FAIL": "failed", "ERROR": "error"}.get(m.group(3), "skipped")
            tests[name] = st
            continue
        m = re.match(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", line)
        if m:
            tests[m.group(2)] = {"PASS": "passed", "FAIL": "failed"}.get(m.group(1), "skipped")
            continue
        m = re.match(r"^test (\S+) \.\.\. (ok|FAILED|ignored)", line)
        if m:
            tests[m.group(1)] = {"ok": "passed", "FAILED": "failed"}.get(m.group(2), "skipped")
    res["tests"] = tests

    # cargo (sum all "test result" lines)
    cargo = [re.search(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", l)
             for l in lines]
    cargo = [(m, l) for m, l in zip(cargo, lines) if m]
    if cargo:
        p = sum(int(m.group(1)) for m, _ in cargo)
        f = sum(int(m.group(2)) for m, _ in cargo)
        s = sum(int(m.group(3)) for m, _ in cargo)
        setres("cargo", cargo[-1][1], passed=p, failed=f, errors=0, skipped=s, total=p + f + s)
        return res

    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        s = line.strip()
        # jest: "Tests:       1 failed, 4 passed, 5 total"
        m = re.match(r"^Tests:\s+(.*\d+ total)", s)
        if m:
            d = dict((k, int(v)) for v, k in re.findall(r"(\d+) (passed|failed|skipped|todo|total)", m.group(1)))
            setres("jest", s, passed=d.get("passed", 0), failed=d.get("failed", 0), errors=0,
                   skipped=d.get("skipped", 0) + d.get("todo", 0), total=d.get("total"))
            return res
        # vitest: "Tests  1 failed | 3 passed (4)"
        m = re.match(r"^Tests\s+(.*)\((\d+)\)\s*$", s)
        if m and re.search(r"\d+ (passed|failed)", m.group(1)):
            d = dict((k, int(v)) for v, k in re.findall(r"(\d+) (passed|failed|skipped|todo)", m.group(1)))
            setres("vitest", s, passed=d.get("passed", 0), failed=d.get("failed", 0), errors=0,
                   skipped=d.get("skipped", 0) + d.get("todo", 0), total=int(m.group(2)))
            return res
        # pytest: "=== 1 failed, 2 passed in 0.05s ===" / "no tests ran in 0.01s"
        core = s.strip("= ").strip()
        if re.search(r"\bin [\d.]+s\b", core) and (
                re.search(r"\b\d+ (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", core)
                or core.startswith("no tests ran")):
            d = {}
            for v, k in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)", core):
                k = "errors" if k.startswith("error") else k
                d[k] = d.get(k, 0) + int(v)
            p, f, e = d.get("passed", 0) + d.get("xpassed", 0), d.get("failed", 0), d.get("errors", 0)
            sk = d.get("skipped", 0) + d.get("xfailed", 0)
            setres("pytest", s, passed=p, failed=f, errors=e, skipped=sk, total=p + f + e + sk)
            return res
        # unittest: "Ran 5 tests in 0.001s" followed by OK / FAILED (...)
        m = re.match(r"^Ran (\d+) tests? in [\d.]+s", s)
        if m:
            total = int(m.group(1))
            verdict = ""
            for later in lines[i + 1:i + 6]:
                if later.strip().startswith(("OK", "FAILED", "NO TESTS RAN")):
                    verdict = later.strip()
                    break
            d = dict((k, int(v)) for k, v in re.findall(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)", verdict))
            f, e, sk = d.get("failures", 0), d.get("errors", 0), d.get("skipped", 0) + d.get("expected failures", 0)
            if verdict.startswith("FAILED") and not (f or e):
                f = 1
            setres("unittest", s + ((" / " + verdict) if verdict else ""), passed=max(total - f - e - sk, 0),
                   failed=f, errors=e, skipped=sk, total=total)
            return res
        # mocha: "  5 passing (12ms)" + optional "  1 failing"
        m = re.match(r"^(\d+) passing\b", s)
        if m:
            failing = 0
            for later in lines[i + 1:i + 4]:
                mm = re.match(r"^\s*(\d+) failing", later)
                if mm:
                    failing = int(mm.group(1))
            p = int(m.group(1))
            setres("mocha", s, passed=p, failed=failing, errors=0, skipped=0, total=p + failing)
            return res

    # go test: package lines
    go_ok = [l for l in lines if re.match(r"^ok\s+\S+", l)]
    go_fail = [l for l in lines if re.match(r"^(FAIL|--- FAIL)\s*\S*", l)]
    if go_ok or go_fail:
        p = sum(1 for v in tests.values() if v == "passed") if tests else None
        f = sum(1 for v in tests.values() if v == "failed") if tests else (1 if go_fail else 0)
        setres("go", (go_fail or go_ok)[-1], passed=p, failed=f, errors=0, skipped=None,
               total=(p + f) if p is not None else None)
        return res
    if tests:
        p = sum(1 for v in tests.values() if v == "passed")
        f = sum(1 for v in tests.values() if v in ("failed", "error"))
        setres("verbose", "%d passed, %d failed (per-test lines)" % (p, f), passed=p, failed=f,
               errors=0, skipped=sum(1 for v in tests.values() if v == "skipped"), total=len(tests))
    return res


def run_command(cmd, cwd, timeout, extra_env=None):
    """Run a shell command (the user's own test command). Never raises on failure."""
    env = dict(os.environ)
    env.update(extra_env or {})
    start = time.time()
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    except OSError as exc:
        return {"command": cmd, "exit_code": 127, "timed_out": False, "duration_s": 0.0,
                "output": "could not start command: %s" % exc}
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
    text = (out or b"").decode("utf-8", "replace")
    return {"command": cmd, "exit_code": proc.returncode if not timed_out else None,
            "timed_out": timed_out, "duration_s": round(time.time() - start, 3), "output": text}


def test_record(run, source):
    parsed = parse_test_output(run["output"])
    rec = {"command": run["command"], "exit_code": run["exit_code"], "timed_out": run["timed_out"],
           "duration_s": run.get("duration_s"), "source": source,
           "parsed": {k: v for k, v in parsed.items() if k != "tests"},
           "test_names": parsed["tests"],
           "output_sha256": sha256_text(run["output"]),
           "output_tail": run["output"][-OUTPUT_TAIL:]}
    rec["passed"] = test_run_passed(rec)
    return rec


def test_run_passed(rec):
    if rec.get("timed_out") or rec.get("exit_code") not in (0, None):
        return False
    p = rec.get("parsed") or {}
    if (p.get("failed") or 0) + (p.get("errors") or 0) > 0:
        return False
    if p.get("total") == 0 or (p.get("summary_line") or "").startswith("no tests ran"):
        return False
    if rec.get("exit_code") is None:
        return None  # unknown exit code and no failures seen
    return True


# --------------------------------------------------------------------------
# logs / transcripts
# --------------------------------------------------------------------------

def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
                elif "content" in c:
                    parts.append(_content_text(c["content"]))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


EXIT_IN_TEXT = re.compile(r"(?im)^(?:error:\s*)?exit(?:ed)?(?: with)?(?: code| status)[:= ]?\s*(-?\d+)\b"
                          r"|\b(?:exit code|exit status|return code|exited with code)[:= ]\s*(-?\d+)\b")


def _exit_from_text(text):
    m = EXIT_IN_TEXT.search(text or "")
    if m:
        return int(m.group(1) or m.group(2))
    return None


def parse_log(path):
    """Parse a transcript (JSONL) or plain text log into command executions."""
    text = read_text(path, limit=50_000_000)
    if text is None:
        raise BadInput("cannot read log file (missing or binary): %s" % path)
    runs = []
    json_lines = 0
    uses = {}
    last_assistant = None
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        json_lines += 1
        # documented simple format: {"command": ..., "exit_code": ..., "output": ...}
        if "command" in obj and ("exit_code" in obj or "exitCode" in obj):
            ec = obj.get("exit_code", obj.get("exitCode"))
            runs.append({"command": str(obj["command"]), "exit_code": ec if isinstance(ec, int) else None,
                         "output": str(obj.get("output", "")), "source": "%s:%d" % (path, lineno),
                         "exit_code_source": "log"})
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        content = msg.get("content")
        role = msg.get("role") or obj.get("type")
        if role == "assistant":
            t = _content_text([c for c in content if isinstance(c, dict) and c.get("type") == "text"]) \
                if isinstance(content, list) else _content_text(content)
            if t.strip():
                last_assistant = t
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inp = block.get("input") or {}
                cmd = inp.get("command") or inp.get("cmd")
                if isinstance(cmd, list):
                    cmd = " ".join(str(c) for c in cmd)
                if isinstance(cmd, str):
                    uses[block.get("id")] = (cmd, lineno)
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                cmd, _ = uses.pop(block.get("tool_use_id"))
                out = _content_text(block.get("content"))
                tur = obj.get("toolUseResult")
                if isinstance(tur, dict) and (tur.get("stdout") or tur.get("stderr")):
                    out = "%s\n%s" % (tur.get("stdout", ""), tur.get("stderr", ""))
                ec = _exit_from_text(_content_text(block.get("content")))
                src = "text"
                if ec is None:
                    ec, src = (1, "is_error") if block.get("is_error") else (0, "inferred")
                runs.append({"command": cmd, "exit_code": ec, "output": out,
                             "source": "%s:%d" % (path, lineno), "exit_code_source": src})
    if json_lines and json_lines >= max(1, len([l for l in lines if l.strip()]) // 2):
        return {"kind": "transcript", "complete": True, "runs": runs, "path": path,
                "last_assistant_message": last_assistant}
    # plain text log: "$ cmd" lines start a new command
    runs = []
    cur = None
    for lineno, line in enumerate(lines, 1):
        m = re.match(r"^\s*(?:\$|>|❯|%|PS [^>]*>)\s+(\S.*)$", line)
        if m:
            if cur:
                runs.append(cur)
            cur = {"command": m.group(1).strip(), "output": "", "source": "%s:%d" % (path, lineno)}
            continue
        if cur is None:
            cur = {"command": None, "output": "", "source": "%s:%d" % (path, lineno)}
        cur["output"] += line + "\n"
    if cur:
        runs.append(cur)
    for r in runs:
        ec = _exit_from_text(r["output"])
        r["exit_code"] = ec
        r["exit_code_source"] = "text" if ec is not None else "unknown"
    return {"kind": "text", "complete": False, "runs": runs, "path": path,
            "last_assistant_message": None}


# --------------------------------------------------------------------------
# claim extraction
# --------------------------------------------------------------------------

EXTS = ("py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|kts|rb|php|c|h|cc|cpp|hpp|cs|swift|scala|"
        "sh|bash|zsh|ps1|sql|md|rst|txt|json|jsonc|yaml|yml|toml|ini|cfg|conf|env|html|htm|css|"
        "scss|less|vue|svelte|lock|xml|csv|ipynb|gradle|proto|graphql|tf|mk|dart|lua|r|ex|exs|"
        "erl|hs|ml|zig|nim|jl|pl|bat|cmd")
PATH_RE = re.compile(
    r"(?<![\w/.@-])((?:\.{1,2}/)?(?:[\w@+\-]+/)*[\w@+\-]*[\w+\-]\.(?:%s)"
    r"|(?:\.{1,2}/)?(?:[\w@+\-]+/)+[\w@+\-.]*[\w+\-]"
    r"|(?:[\w@+\-]+/)*(?:Dockerfile|Makefile|Procfile|Gemfile|Rakefile))(?![\w/-])" % EXTS)
KNOWN_CMDS = set("""npm npx pnpm yarn bun deno node python python3 py pip pip3 pytest tox nox make cmake
cargo go gradle gradlew mvn dotnet ruff black isort flake8 pylint mypy pyright eslint prettier tsc jest
vitest mocha docker docker-compose bash sh zsh uv uvx poetry pdm hatch rake bundle rspec php composer
swift xcodebuild terraform helm kubectl git just task nx turbo ctest ninja meson rustc gcc clang javac
java mix elixir flutter dart lua perl ruby""".split())
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "an": 1,
             "single": 1}
NUM = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"

HEDGE_EN = re.compile(r"\b(should|would|might|may|could|will|expect(?:ed)?|hopefully|probably|"
                      r"please|once|unless|if you|need to|needs to|make sure|try)\b", re.I)
NEG_EN = re.compile(r"\b(not|n't|never|no longer|unable|without running|did not|didn't|haven't|"
                    r"hasn't|wasn't|weren't|cannot|can't|couldn't)\b|\bno (?:tests?|changes?)\b", re.I)
HEDGE_JA = re.compile(r"(はず|予定|かもしれ|でしょう|だろう|てください|下さい|必要があり|お願い|"
                      r"たら|れば|予想|見込み|つもり)")
NEG_JA = re.compile(r"(ていません|ていない|ませんでした|できません|できなかった|未実行|未確認|未実施|"
                    r"しません|しない|せず|ません|ない[。、]?$|なかった|触っていません|変更なし|"
                    r"変更していません|対象外)")

PASS_WORDS_EN = r"(?:pass(?:ed|es|ing)?|green|succeed(?:ed|s|ing)?|OK)"
PASS_WORDS_JA = r"(?:パス|通過|成功|通り|通っ|通る|グリーン|OK|オールグリーン)"

TESTS_EN = [
    re.compile(r"\b(?:all\s+(?:of\s+the\s+)?)?(\d[\d,]*)\s*/\s*(\d[\d,]*)\s+(?:\w+\s+){0,2}"
               r"(?:tests?|specs?|test cases)\s+(?:\w+\s+){0,2}" + PASS_WORDS_EN + r"\b", re.I),
    re.compile(r"\b(?:all\s+(?:of\s+the\s+)?)?(\d[\d,]*)\s+(?:(?:unit|integration|e2e|end-to-end|"
               r"new|existing|regression|remaining|python|go|rust|js)\s+)*(?:tests?|specs?|test cases|checks)"
               r"\s+(?:\([^)]*\)\s+)?(?:(?:are|were|now|all|still|have|has|that|which|successfully)\s+)*"
               + PASS_WORDS_EN + r"\b", re.I),
    re.compile(r"\b(\d[\d,]*)\s+passed\b", re.I),
    re.compile(r"\b(?:tests?|test suite|suite)\s+(?:all\s+)?" + PASS_WORDS_EN +
               r"\s*\(\s*(\d[\d,]*)\s+(?:tests?|specs?)\s*\)", re.I),
    re.compile(r"\b(?:all\s+(?:of\s+)?(?:the\s+)?)?(?:tests?|test suite|test cases|specs|unit tests|"
               r"integration tests|(?:pytest|jest|vitest|mocha|go test|cargo test)(?:\s+suite)?|CI|checks)\s+"
               r"(?:(?:are|were|is|now|still|all|have|has|successfully|run|ran|and)\s+)*"
               r"(?:all\s+)?" + PASS_WORDS_EN + r"\b", re.I),
    re.compile(r"\ball\s+(?:tests\s+)?green\b", re.I),
]
TESTS_JA = [
    re.compile(r"(\d+)\s*(?:件|個|本|ケース)?\s*(?:の|全て|すべて)?\s*(?:テスト|test)(?:ケース)?\s*"
               r"(?:が|は|も)?\s*(?:すべて|全て|全部|全件|問題なく|無事)?\s*(?:に)?\s*" + PASS_WORDS_JA, re.I),
    re.compile(r"(?:テスト|test)(?:ケース)?\s*(?:\(|（)?\s*(?:全)?\s*(\d+)\s*(?:件|個|本)\s*(?:\)|）)?\s*"
               r"(?:が|は|も)?\s*(?:すべて|全て|全部|全件|問題なく)?\s*" + PASS_WORDS_JA, re.I),
    re.compile(r"全\s*(\d+)\s*(?:件|個|本)の?\s*(?:テスト|test)(?:ケース)?\s*(?:が|は)?\s*(?:すべて|全て)?\s*" + PASS_WORDS_JA, re.I),
    re.compile(r"(?:テスト|test|pytest|jest|vitest|unittest)(?:ケース|スイート)?\s*(?:が|は|も)?\s*"
               r"(?:すべて|全て|全部|全件|問題なく|無事|正常に)?\s*(?:に)?\s*" + PASS_WORDS_JA, re.I),
    re.compile(r"(?:すべて|全て|全部)の?\s*(?:テスト|test)(?:ケース)?\s*(?:が|は)?\s*" + PASS_WORDS_JA, re.I),
]
FAILED_COUNT = re.compile(r"\b(\d+)\s+(?:tests?\s+)?(?:failed|failures?|failing|errors?)\b|"
                          r"(\d+)\s*件?の?\s*(?:失敗|エラー)|(?:失敗|エラー)\s*(\d+)\s*件", re.I)

SYMBOL_KIND = {"function": "function", "functions": "function", "func": "function", "method": "function",
               "methods": "function", "helper": "function", "helpers": "function", "decorator": "function",
               "fixture": "function", "class": "class", "classes": "class", "関数": "function",
               "メソッド": "function", "ヘルパー": "function", "クラス": "class"}
IDENT = r"[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*"
SYMBOL_EN = [
    re.compile(r"\b(?:added|add|adding|created|implemented|implement|introduced|defined|wrote|"
               r"exposed|new)\s+(?:(?:a|an|the|new|two|three|helper|public|private|static|async|"
               r"utility|small|simple|dedicated|separate)\s+)*(functions?|methods?|class(?:es)?|"
               r"func|helpers?|decorator|fixture)\s+(?:called\s+|named\s+)?((?:`?" + IDENT +
               r"`?(?:\(\))?(?:\s*(?:,|and|&)\s*)?)+)", re.I),
    re.compile(r"\b(?:added|add|adding|created|implemented|implement|introduced|defined|wrote|"
               r"exposed)\s+(?:(?:a|an|the|new)\s+)*`(" + IDENT + r")(\(\))?`\s*(functions?|methods?|"
               r"class(?:es)?|helper)?", re.I),
]
SYMBOL_JA = [
    re.compile(r"`?(" + IDENT + r")`?\s*(\(\))?\s*(?:という|の)?\s*(関数|メソッド|クラス|ヘルパー)\s*"
               r"(?:を|も)?[^。\n]{0,24}?(追加|実装|作成|定義|新設|用意|新規)", re.I),
    re.compile(r"(関数|メソッド|クラス|ヘルパー)\s*`?(" + IDENT + r")`?\s*(\(\))?\s*(?:を|も)"
               r"[^。\n]{0,24}?(追加|実装|作成|定義|新設|用意)", re.I),
    re.compile(r"`(" + IDENT + r")(\(\))?`\s*を[^。\n]{0,24}?(追加|実装|作成|定義|新設)", re.I),
]
COUNT_EN = [
    re.compile(r"\b(?:added|wrote|created|introduced|implemented)\s+" + NUM +
               r"\s+(?:new\s+)?(?:(?:unit|integration|regression|e2e)\s+)?(tests?|test cases|functions?|"
               r"methods?|class(?:es)?|files?)\b", re.I),
    re.compile(r"\b(?:modified|changed|updated|edited|touched)\s+" + NUM + r"\s+(files?)\b", re.I),
    re.compile(r"\bwith\s+" + NUM + r"\s+new\s+(?:(?:unit|integration|regression|e2e)\s+)?(tests?|test cases)\b",
               re.I),
    re.compile(r"\b" + NUM + r"\s+(files?)\s+(?:were\s+|have\s+been\s+)?(changed|modified|updated|"
               r"touched|edited|created|added)\b", re.I),
]
COUNT_JA = [
    re.compile(r"(\d+)\s*(?:つ|個|件|本|ファイル)?\s*の?\s*(ファイル|テスト(?:ケース)?|関数|メソッド|クラス)\s*"
               r"(?:を|が)?[^。\n\d]{0,8}?(追加|作成|変更|修正|更新|編集|実装)"),
    re.compile(r"(ファイル|テスト(?:ケース)?|関数|メソッド|クラス)\s*を\s*(\d+)\s*(?:つ|個|件|本)\s*"
               r"(?:を)?[^。\n\d]{0,6}?(追加|作成|変更|修正|更新|編集|実装)"),
]
GENERIC_CMD_EN = re.compile(r"\b(?:the\s+)?(build|lint(?:er|ing)?|type[- ]?check(?:ing|s|er)?|mypy|"
                            r"eslint|ruff|flake8|tsc|compil(?:e|ation)|formatter|format check)\s+"
                            r"(?:(?:is|now|still|also|all|step)\s+)*(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|"
                            r"clean|green|works|OK|is clean)\b", re.I)
GENERIC_CMD_JA = re.compile(r"(ビルド|build|lint|リント|型チェック|型検査|コンパイル|mypy|eslint|ruff|tsc)"
                            r"\s*(?:が|は|も)?\s*(?:すべて|問題なく|エラーなく|無事|正常に)?\s*"
                            r"(成功|通り|通っ|通過|パス|OK|クリーン)", re.I)
SUCCESS_EN = re.compile(r"\b(succeed(?:ed|s)?|successful(?:ly)?|pass(?:es|ed|ing)?|works|worked|"
                        r"completed?|clean|green|no errors|without errors|exit(?:ed)?(?: with)?"
                        r"(?: code| status)? 0)\b", re.I)
SUCCESS_JA = re.compile(r"(成功|通りました|通った|通過|パス|問題なく|エラーなく|正常(?:に)?(?:終了|完了)|"
                        r"クリーン|完走|OKでした|エラー0)")
GENERIC_KEYWORDS = {
    "build": r"\b(build|compile|tsc|make|gradle|mvn|cargo\s+build|go\s+build|webpack|vite)\b",
    "lint": r"\b(lint|ruff|flake8|eslint|pylint|clippy|golangci|rubocop|stylelint|biome)\b",
    "typecheck": r"\b(mypy|tsc|pyright|typecheck|type-check|check-types|vue-tsc)\b",
    "format": r"\b(prettier|black|fmt|format|gofmt|isort)\b",
}

CREATE_EN = r"(?:created|create|creating|added|add|adding|wrote|written|introduced|generated|scaffolded|new file)"
MODIFY_EN = (r"(?:modified|modify|updated|update|updating|edited|edit|changed|change|fixed|fix|refactored|"
             r"refactor|patched|rewrote|rewritten|tweaked|adjusted|extended|touched|renamed|cleaned up|"
             r"moved|reworked|improved|replaced)")
DELETE_EN = r"(?:deleted|delete|removed|remove|dropped)"
CREATE_JA = r"(?:作成|新規|追加|新しく|書き起こし|生成|新設|用意)"
MODIFY_JA = r"(?:修正|変更|更新|編集|書き換え|改修|リファクタ|直し|改善|調整|置き換え|手を入れ)"
DELETE_JA = r"(?:削除|消し|除去)"


def _norm_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("**", "").replace("__", " ").replace("’", "'")
    return text


def split_segments(text):
    segs = []
    for line in _norm_text(text).splitlines():
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)]|#+|>)\s*", "", line).strip()
        if not line:
            continue
        for piece in re.split(r"(?<=[。！？])|(?<=[.!?;])\s+(?=[A-Z`\"'(\[]|[぀-ヿ一-鿿])", line):
            piece = piece.strip()
            if piece:
                segs.append(piece)
    return segs


def _code_spans(seg):
    return [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"`([^`]+)`", seg)]


def _looks_like_command(span):
    s = span.strip()
    if not s or "\n" in s:
        return False
    first = s.split()[0]
    first_base = first.split("/")[-1]
    if first_base in KNOWN_CMDS or first.startswith("./") and " " in s:
        return True
    return False


def _hedged(seg, ja):
    if ja:
        return bool(HEDGE_JA.search(seg))
    return bool(HEDGE_EN.search(seg))


def _negated(seg, ja):
    if ja and has_japanese(seg):
        return bool(NEG_JA.search(seg))
    return bool(NEG_EN.search(seg))


def _to_int(word):
    if word is None:
        return None
    w = word.lower().replace(",", "")
    if w.isdigit():
        return int(w)
    return NUM_WORDS.get(w)


def _paths_in(seg):
    """Find file paths in a segment, skipping URLs and paths inside command spans."""
    blocked = []
    for s, e, span in _code_spans(seg):
        if _looks_like_command(span):
            blocked.append((s, e))
    found = []
    for m in PATH_RE.finditer(seg):
        start, end = m.span(1)
        p = m.group(1).rstrip(".")
        if any(s <= start < e for s, e in blocked):
            continue
        around = seg[max(0, start - 8):end + 3]
        if "://" in around or p.startswith("//") or re.match(r"^[\d.]+$", p):
            continue
        in_tick = start > 0 and seg[start - 1] == "`"
        if not in_tick and "/" not in p and re.match(r"^[A-Z][\w]*\.js$", p):
            continue  # "Node.js", "Vue.js", ...
        if not in_tick and "/" in p and "." not in p.split("/")[-1] and p.count("/") == 1 \
                and not p.endswith("/"):
            # "and/or", "input/output": only accept slash-words when they look like dirs
            if not re.search(r"(src|lib|test|tests|app|pkg|cmd|docs|scripts|internal|bin)", p):
                continue
        found.append((start, end, p))
    return found


def _file_action(seg, start, end, ja):
    before = seg[max(0, start - 70):start]
    after = seg[end:end + 45]
    if ja and has_japanese(after[:20] + before[-20:]):
        after_s = after.lstrip("` ")
        if re.match(r"^(?:に|へ|内|の中|の\S{0,10}?(?:に|を))", after_s) and \
                re.search(CREATE_JA + "|" + MODIFY_JA + "|" + DELETE_JA, after_s):
            return "modify"
        m = re.search(r"(" + CREATE_JA + "|" + MODIFY_JA + "|" + DELETE_JA + ")", after_s)
        if m and m.start() <= 25:
            v = m.group(1)
            if re.match(DELETE_JA, v):
                return "delete"
            if re.match(CREATE_JA, v):
                return "create"
            return "modify"
        return None
    b = before.lower()
    if re.search(r"\b(?:to|in|into|inside|within|from|under|on)\s+(?:the\s+)?(?:file\s+)?[`'\"]?$", b):
        if re.search(r"\b(?:%s|%s|%s)\b" % (CREATE_EN, MODIFY_EN, DELETE_EN), b):
            return "modify"
        return None
    verbs = list(re.finditer(r"\b(%s|%s|%s)\b" % (CREATE_EN, MODIFY_EN, DELETE_EN), b))
    if verbs:
        v = verbs[-1].group(1)
        gap = b[verbs[-1].end():]
        if len(gap) <= 45 and not re.search(r"[.;:]\s|,\s*(?:but|so)\b", gap) and \
                not re.search(r"\b(?:for|with|because|so|which|that|after|before|using|via|by)\b", gap):
            if re.match(DELETE_EN, v):
                return "delete"
            if re.match(CREATE_EN, v):
                return "create"
            return "modify"
    m = re.match(r"^[`'\"]?\s*(?:is|was|were|has been|have been|got|now)\s+(?:also\s+|now\s+)?(%s|%s|%s)\b"
                 % (CREATE_EN, MODIFY_EN, DELETE_EN), after, re.I)
    if m:
        v = m.group(1).lower()
        if re.match(DELETE_EN, v):
            return "delete"
        if re.match(CREATE_EN, v):
            return "create"
        return "modify"
    return None


def extract_claims(text):
    """Return a list of claim dicts extracted from a completion report."""
    claims = []
    seen = set()

    def add(kind, seg, **params):
        ignore = ("lang", "symbol_kind") if kind == "symbol_added" else ("lang",)
        key = (kind,) + tuple(sorted((k, str(v)) for k, v in params.items() if k not in ignore))
        if key in seen:
            return
        seen.add(key)
        c = {"id": "c%d" % (len(claims) + 1), "kind": kind, "text": seg[:300]}
        c.update(params)
        claims.append(c)

    for seg in split_segments(text):
        ja = has_japanese(seg)
        lang = "ja" if ja else "en"
        hedged = _hedged(seg, ja)
        negated = _negated(seg, ja)
        test_cmd_claim = False

        # --- commands (explicit, in backticks) -----------------------------
        success = SUCCESS_JA.search(seg) if ja else SUCCESS_EN.search(seg)
        if success and not hedged and not negated:
            for s, e, span in _code_spans(seg):
                if _looks_like_command(span):
                    add("command_succeeded", seg, command=span.strip(), lang=lang)
                    test_cmd_claim = test_cmd_claim or is_test_command(span)
            if not _code_spans(seg) or not any(_looks_like_command(sp) for _, _, sp in _code_spans(seg)):
                gm = (GENERIC_CMD_JA if ja else GENERIC_CMD_EN).search(seg)
                if gm:
                    word = gm.group(1).lower()
                    kw = ("build" if re.match(r"build|ビルド|compil|コンパイル|tsc", word) else
                          "lint" if re.match(r"lint|リント|eslint|ruff|flake8", word) else
                          "typecheck" if re.match(r"type|型|mypy", word) else "format")
                    add("command_succeeded", seg, command=None, keyword=kw, lang=lang)

        # --- tests passed --------------------------------------------------
        if not hedged and not negated:
            count = None
            matched = False
            for rx in (TESTS_JA + TESTS_EN) if ja else TESTS_EN:
                m = rx.search(seg)
                if m:
                    matched = True
                    nums = [g for g in m.groups() if g and re.match(r"^\d", g)]
                    if nums:
                        count = _to_int(nums[0])
                        if len(nums) > 1 and rx is TESTS_EN[0]:
                            count = _to_int(nums[0])
                    break
            if matched and not (test_cmd_claim and count is None):
                fm = FAILED_COUNT.search(seg)
                claimed_failed = None
                if fm:
                    claimed_failed = int(next(g for g in fm.groups() if g))
                params = {"count": count, "lang": lang}
                if claimed_failed is not None:
                    params["claimed_failed"] = claimed_failed
                add("tests_passed", seg, **params)

        # --- symbols ---------------------------------------------------------
        if not negated or ja:
            paths = _paths_in(seg)
            target_file = None
            if paths:
                pref = [p for s, e, p in paths
                        if re.search(r"(?:to|in|into|inside|within)\s+[`'\"]?$", seg[max(0, s - 12):s], re.I)
                        or re.match(r"^`?\s*(?:に|へ|内|の中)", seg[e:e + 4])]
                target_file = (pref or [paths[-1][2]])[0]
            for rx in (SYMBOL_JA if ja else []) + SYMBOL_EN:
                for m in rx.finditer(seg):
                    if ja and _negated(seg[m.start():m.end() + 12], True):
                        continue
                    groups = m.groups()
                    if rx is SYMBOL_EN[0]:
                        kind = SYMBOL_KIND.get(groups[0].lower(), "symbol")
                        names = re.findall(r"`?(" + IDENT + r")`?", groups[1])
                        names = [n for n in names if n.lower() not in ("and", "called", "named", "to", "in", "for",
                                                                       "that", "which", "the", "a", "an", "with")]
                        names = names[:6]
                    elif rx is SYMBOL_EN[1]:
                        kind = SYMBOL_KIND.get((groups[2] or "").lower(), "function" if groups[1] else "symbol")
                        names = [groups[0]]
                    elif rx is SYMBOL_JA[0]:
                        kind, names = SYMBOL_KIND.get(groups[2], "symbol"), [groups[0]]
                    elif rx is SYMBOL_JA[1]:
                        kind, names = SYMBOL_KIND.get(groups[0], "symbol"), [groups[1]]
                    else:
                        kind, names = ("function" if groups[1] else "symbol"), [groups[0]]
                    for name in names:
                        if re.search(r"\.(%s)$" % EXTS, name) or name.lower() in NUM_WORDS:
                            continue
                        if kind == "symbol" and not ("`" in seg):
                            continue
                        add("symbol_added", seg, name=name, symbol_kind=kind, file=target_file, lang=lang)

        # --- files -----------------------------------------------------------
        if not negated:
            for s, e, p in _paths_in(seg):
                action = _file_action(seg, s, e, ja)
                if action:
                    if p.endswith("/") or ("." not in p.split("/")[-1] and not
                                           re.search(r"(Dockerfile|Makefile|Procfile|Gemfile|Rakefile)$", p)):
                        continue  # directories are not file claims in v1
                    add("file_changed", seg, path=p, action=action, lang=lang)

        # --- counts ----------------------------------------------------------
        if not hedged and not negated:
            for rx in (COUNT_JA if ja else COUNT_EN):
                for m in rx.finditer(seg):
                    g = m.groups()
                    if ja:
                        if rx is COUNT_JA[0]:
                            n, noun, verb = _to_int(g[0]), g[1], g[2]
                        else:
                            noun, n, verb = g[0], _to_int(g[1]), g[2]
                        what = ("files" if noun == "ファイル" else "tests" if noun.startswith("テスト")
                                else "classes" if noun == "クラス" else "functions")
                        mode = "created" if verb in ("追加", "作成", "実装") else "changed"
                    else:
                        if rx is COUNT_EN[3]:
                            n, noun, verb = _to_int(g[0]), g[1], g[2]
                        elif rx is COUNT_EN[1]:
                            n, noun, verb = _to_int(g[0]), g[1], "changed"
                        else:
                            n, noun, verb = _to_int(g[0]), g[1], "added"
                        noun = noun.lower()
                        what = ("files" if noun.startswith("file") else "tests" if noun.startswith("test")
                                else "classes" if noun.startswith("class") else "functions")
                        mode = "created" if verb.lower() in ("added", "created") else "changed"
                    if n is None:
                        continue
                    add("count", seg, what=what, n=n, mode=mode, lang=lang)
    return claims


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _result(claim, status, reason, evidence, confidence="high"):
    claim = dict(claim)
    claim.update({"status": status, "reason": reason, "evidence": evidence, "confidence": confidence})
    return claim


def py_definitions(source):
    """Return [(qualname, name, kind, lineno, source_hash)] for a Python source string."""
    tree = ast.parse(source)
    out = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                qual = prefix + child.name
                seg = ast.get_source_segment(source, child) or ""
                out.append((qual, child.name, kind, child.lineno, sha256_text(seg)))
                visit(child, qual + ".")
            else:
                visit(child, prefix)
    visit(tree, "")
    return out


def _match_defs(defs, name):
    parts = name.split(".")
    leaf = parts[-1]
    hits = [d for d in defs if d[1] == leaf]
    if len(parts) > 1:
        q = [d for d in hits if d[0].endswith(name) or d[0].split(".")[-2:] == parts[-2:]]
        hits = q or hits
    return hits


TEXT_DEF_RE = (r"(?:\b(?:function\*?|def|class|fn|func|interface|type|struct|enum|trait|impl|"
               r"module|record|object|sub|proc)\s+(?:\([^)]*\)\s*)?{n}\b"
               r"|\b(?:const|let|var|export\s+const|export\s+default)\s+{n}\s*[=:]"
               r"|^\s*(?:(?:public|private|protected|static|async|override|export|default|final|"
               r"virtual|abstract|internal|pub(?:\([^)]*\))?)\s+)*(?:[\w<>\[\],.?]+\s+)?{n}\s*\([^;]*\)\s*"
               r"(?:[:\w<>\[\],.?\s]*)\{{"
               r"|\b{n}\s*[:=]\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|\w+\s*=>))")


def verify_symbol(claim, repo):
    name = claim["name"]
    leaf = name.split(".")[-1]
    want_kind = claim.get("symbol_kind")
    evidence = []
    files = []
    if claim.get("file"):
        rel = repo.resolve(claim["file"])
        files = [rel] if rel else []
    if not files or not repo.exists(files[0]):
        changed = [p for p in repo.changes if repo.exists(p)]
        files = sorted(changed, key=lambda p: (not p.endswith(".py"), p))
        claimed_file_missing = bool(claim.get("file"))
    else:
        claimed_file_missing = False
    searched = set()

    def check_file(rel, in_change):
        path = os.path.join(repo.root, rel)
        text = read_text(path)
        if text is None:
            return None
        searched.add(rel)
        if rel.endswith((".py", ".pyi")):
            try:
                defs = _match_defs(py_definitions(text), name)
            except (SyntaxError, ValueError) as exc:
                evidence.append({"type": "ast", "path": rel, "note": "syntax error: %s" % exc})
                defs = None
            if defs is not None:
                if not defs:
                    return None
                d = defs[0]
                return {"path": rel, "line": d[3], "kind": d[2], "hash": d[4], "method": "ast",
                        "snippet": text.splitlines()[d[3] - 1].strip()[:200], "qualname": d[0]}
        rx = re.compile(TEXT_DEF_RE.format(n=re.escape(leaf)), re.M)
        m = rx.search(text)
        if m:
            line = text.count("\n", 0, m.start()) + 1
            snippet = text.splitlines()[line - 1].strip()[:200] if text.splitlines() else ""
            return {"path": rel, "line": line, "kind": "class" if "class " in snippet else "function",
                    "hash": None, "method": "text", "snippet": snippet}
        return None

    hit = None
    for rel in files:
        hit = check_file(rel, rel in repo.changes)
        if hit:
            break
    if not hit:
        # search the rest of the repository
        count = 0
        for rel in repo.all_files():
            if rel in searched or not re.search(r"\.(%s)$" % EXTS, rel):
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                break
            hit = check_file(rel, False)
            if hit:
                break
    if not hit:
        mentioned = []
        for rel in list(searched)[:200]:
            t = read_text(os.path.join(repo.root, rel)) or ""
            if re.search(r"\b%s\b" % re.escape(leaf), t):
                mentioned.append(rel)
        if mentioned and not all(r.endswith(".py") for r in mentioned):
            return _result(claim, "unverifiable", "'%s' is mentioned in %s but no definition was recognised "
                           "(non-Python text search)" % (name, ", ".join(mentioned[:3])),
                           evidence + [{"type": "text_search", "paths": mentioned[:5]}], "low")
        where = claim.get("file") or "the repository"
        if claimed_file_missing:
            where = "%s (file not found) or anywhere else in the repository" % claim["file"]
        return _result(claim, "contradicted", "no definition of '%s' found in %s" % (name, where),
                       evidence + [{"type": "search", "files_searched": len(searched)}],
                       "high" if all(r.endswith(".py") for r in searched) else "medium")
    ev = {"type": hit["method"], "path": hit["path"], "line": hit["line"], "snippet": hit["snippet"]}
    evidence.append(ev)
    conf = "high" if hit["method"] == "ast" else "low"
    if want_kind in ("class", "function") and hit["method"] == "ast" and hit["kind"] != want_kind:
        return _result(claim, "contradicted", "'%s' exists at %s:%d but is a %s, not a %s"
                       % (name, hit["path"], hit["line"], hit["kind"], want_kind), evidence, conf)
    if not repo.is_git:
        return _result(claim, "verified", "definition found at %s:%d (not a git repo: cannot tell if it is new)"
                       % (hit["path"], hit["line"]), evidence, "low")
    if hit["path"] not in repo.changes:
        if repo.changed_in_head_commit(hit["path"]):
            return _result(claim, "unverifiable", "found at %s:%d; the file changed in the last commit, not in "
                           "the working tree (pass --base to compare against an older commit)"
                           % (hit["path"], hit["line"]), evidence, "medium")
        return _result(claim, "contradicted", "'%s' exists at %s:%d but that file is unchanged since %s, so "
                       "it was not added by this change" % (name, hit["path"], hit["line"], repo.base[:12]),
                       evidence, conf)
    base_src = repo.base_content(hit["path"])
    if base_src is not None and hit["method"] == "ast":
        try:
            base_defs = _match_defs(py_definitions(base_src), name)
        except (SyntaxError, ValueError):
            base_defs = []
        same = [d for d in base_defs if d[4] == hit["hash"]]
        if same:
            return _result(claim, "contradicted", "'%s' already existed at %s:%d in the base commit with an "
                           "identical body" % (name, hit["path"], same[0][3]), evidence, "high")
        if base_defs:
            return _result(claim, "verified", "'%s' at %s:%d existed before and was modified by this change"
                           % (name, hit["path"], hit["line"]), evidence, "medium")
    return _result(claim, "verified", "new definition at %s:%d" % (hit["path"], hit["line"]), evidence, conf)


def verify_file(claim, repo):
    rel = repo.resolve(claim["path"])
    action = claim["action"]
    exists = repo.exists(rel)
    ev = [{"type": "file", "path": rel, "exists": exists}]
    if repo.is_git:
        st = repo.changes.get(rel)
        if st:
            a, d = repo.numstat.get(rel, (None, None))
            ev.append({"type": "git", "command": "git diff --numstat %s -- %s" % (repo.base[:12], rel),
                       "status": {"?": "untracked", "A": "added", "M": "modified", "D": "deleted",
                                  "T": "type changed"}.get(st, st),
                       "diff_stat": "binary" if a is None else "+%d -%d" % (a, d)})
    else:
        st = None
    if action == "delete":
        if exists:
            return _result(claim, "contradicted", "%s still exists" % rel, ev)
        if repo.is_git and not (st == "D" or repo.in_base(rel)):
            return _result(claim, "unverifiable", "%s does not exist, and it was not in the base commit either"
                           % rel, ev, "medium")
        return _result(claim, "verified", "%s is deleted" % rel, ev)
    if not exists:
        return _result(claim, "contradicted", "%s does not exist" % rel, ev)
    if not repo.is_git:
        return _result(claim, "unverifiable", "%s exists, but this is not a git repository, so whether it "
                       "changed cannot be checked" % rel, ev, "low")
    if st:
        if action == "create" and st not in ("?", "A") and repo.in_base(rel):
            return _result(claim, "verified", "%s was modified (it already existed; claimed as created)"
                           % rel, ev, "medium")
        return _result(claim, "verified", "%s %s (%s)" % (rel, "is new" if st in ("?", "A") else "changed",
                                                         ev[-1].get("diff_stat")), ev)
    if repo.changed_in_head_commit(rel):
        return _result(claim, "unverifiable", "%s is unchanged in the working tree but changed in the last "
                       "commit; pass --base to compare against an older commit" % rel, ev, "medium")
    return _result(claim, "contradicted", "%s exists but is unchanged since %s" % (rel, (repo.base or "")[:12]),
                   ev + [{"type": "git", "command": "git diff --numstat %s -- %s" % ((repo.base or "")[:12], rel),
                          "result": "no changes"}])


def _count_new_tests(repo, rel):
    cur = read_text(os.path.join(repo.root, rel))
    if cur is None:
        return None
    base = repo.base_content(rel) or ""
    if rel.endswith(".py"):
        try:
            names_now = [d[0] for d in py_definitions(cur) if d[1].startswith("test") and d[2] == "function"]
            names_then = [d[0] for d in py_definitions(base) if d[1].startswith("test") and d[2] == "function"] \
                if base else []
        except (SyntaxError, ValueError):
            return None
        return len(set(names_now) - set(names_then))
    if re.search(r"\.[cm]?[jt]sx?$", rel):
        rx = r"\b(?:it|test)(?:\.only)?\s*\(\s*['\"`]"
    elif rel.endswith(".go"):
        rx = r"^func Test\w+\("
    elif rel.endswith(".rs"):
        rx = r"#\[(?:tokio::)?test\]"
    else:
        return None
    return max(len(re.findall(rx, cur, re.M)) - len(re.findall(rx, base, re.M)), 0)


def _count_new_defs(repo, rel, kind):
    cur = read_text(os.path.join(repo.root, rel))
    if cur is None:
        return None
    base = repo.base_content(rel) or ""
    if not rel.endswith(".py"):
        return None
    try:
        now = {d[0] for d in py_definitions(cur) if d[2] == kind and not d[1].startswith("test")}
        then = {d[0] for d in py_definitions(base) if d[2] == kind} if base else set()
    except (SyntaxError, ValueError):
        return None
    return len(now - then)


def verify_count(claim, repo):
    n, what, mode = claim["n"], claim["what"], claim["mode"]
    if not repo.is_git:
        return _result(claim, "unverifiable", "not a git repository; counts of changes cannot be checked", [], "low")
    if what == "files":
        all_changed = sorted(repo.changes)
        new = sorted(p for p, s in repo.changes.items() if s in ("?", "A"))
        ev = [{"type": "git", "command": "git diff --name-status %s + untracked" % repo.base[:12],
               "changed_files": all_changed[:50], "new_files": new[:50]}]
        options = [len(new)] if mode == "created" else [len(all_changed),
                                                       len([p for p, s in repo.changes.items() if s == "M"])]
        if mode == "created" and n == len(all_changed):
            options.append(len(all_changed))
        if n in options:
            return _result(claim, "verified", "%d files %s" % (n, mode), ev)
        return _result(claim, "contradicted", "claimed %d files %s, observed %d (%d changed in total)"
                       % (n, mode, options[0], len(all_changed)), ev)
    total, unknown, per = 0, [], {}
    for rel in sorted(repo.changes):
        if repo.changes[rel] == "D" or not repo.exists(rel):
            continue
        if what == "tests":
            if not (is_test_path(rel) or re.search(r"\.(go|rs)$", rel)):
                continue
            c = _count_new_tests(repo, rel)
        else:
            if not re.search(r"\.(%s)$" % EXTS, rel) or rel.endswith((".md", ".txt", ".json", ".yml", ".yaml")):
                continue
            c = _count_new_defs(repo, rel, "class" if what == "classes" else "function")
        if c is None:
            unknown.append(rel)
        elif c:
            per[rel] = c
            total += c
    ev = [{"type": "ast", "new_%s" % what: per, "unparsed_files": unknown[:20]}]
    if total == n:
        return _result(claim, "verified", "%d new %s found in the change" % (total, what), ev,
                       "high" if not unknown else "medium")
    if unknown and total < n:
        return _result(claim, "unverifiable", "found %d new %s; could not count in %s"
                       % (total, what, ", ".join(unknown[:3])), ev, "low")
    return _result(claim, "contradicted", "claimed %d new %s, found %d" % (n, what, total), ev,
                   "high" if not unknown else "medium")


def _run_evidence(r):
    return {"type": "test_run" if is_test_command(r.get("command")) else "command",
            "source": r.get("source"), "command": r.get("command"), "exit_code": r.get("exit_code"),
            "exit_code_source": r.get("exit_code_source", "observed"),
            "summary_line": (r.get("parsed") or {}).get("summary_line")}


def _as_test_run(r):
    if "parsed" in r:
        return r
    rec = dict(r)
    parsed = parse_test_output(r.get("output", ""))
    rec["parsed"] = {k: v for k, v in parsed.items() if k != "tests"}
    rec["passed"] = test_run_passed(rec)
    return rec


def verify_tests(claim, fresh, log):
    runs = []
    if log:
        for r in log["runs"]:
            rr = _as_test_run(r)
            if is_test_command(r.get("command")) or (rr["parsed"].get("framework") and r.get("command") is None):
                runs.append(rr)
    runs += fresh
    if not runs:
        if log and log.get("complete"):
            return _result(claim, "contradicted", "no test command was executed in the transcript (%d commands "
                           "ran)" % len(log["runs"]), [{"type": "transcript", "source": log["path"],
                                                       "commands": [r["command"] for r in log["runs"]][-10:]}])
        hint = "pass --log <transcript> or --run-tests <cmd>"
        if log:
            hint = "the log shows no recognised test run"
        return _result(claim, "unverifiable", "no test run observed (%s)" % hint, [], "low")
    last = runs[-1]
    ev = [_run_evidence(last)]
    p = last["parsed"]
    passed = last.get("passed")
    n = claim.get("count")
    claimed_failed = claim.get("claimed_failed")
    observed_fail = (p.get("failed") or 0) + (p.get("errors") or 0)
    if passed is False:
        if claimed_failed is not None and claimed_failed == observed_fail and \
                (n is None or n == p.get("passed")):
            return _result(claim, "verified", "the last test run reports %s, as stated" % p.get("summary_line"), ev)
        why = "timed out" if last.get("timed_out") else (
            "%s" % p.get("summary_line") if p.get("summary_line") else "exit code %s" % last.get("exit_code"))
        return _result(claim, "contradicted", "the last test run did not pass: %s (exit %s)"
                       % (why, last.get("exit_code")), ev)
    if n is not None:
        if p.get("passed") is None:
            return _result(claim, "unverifiable", "tests passed, but the count (%d) could not be parsed from "
                           "the output" % n, ev, "medium")
        if n != p.get("passed") and n != p.get("total"):
            return _result(claim, "contradicted", "claimed %d tests passed, the last run shows %d (%s)"
                           % (n, p.get("passed"), p.get("summary_line")), ev)
    if passed is None:
        return _result(claim, "unverifiable", "a test run was found but its exit code is unknown", ev, "low")
    conf = "high" if last.get("exit_code_source") in ("observed", "log", "text", None) else "medium"
    return _result(claim, "verified", "last test run passed: %s" % (p.get("summary_line") or "exit 0"), ev, conf)


def _norm_cmd(c):
    return re.sub(r"\s+", " ", (c or "").strip())


def verify_command(claim, fresh, log):
    runs = (log["runs"] if log else []) + fresh
    if claim.get("command"):
        want = _norm_cmd(claim["command"])
        matches = [r for r in runs if r.get("command") and
                   (want in _norm_cmd(r["command"]) or _norm_cmd(r["command"]) in want and
                    len(_norm_cmd(r["command"])) > 3)]
        label = "`%s`" % claim["command"]
    else:
        rx = re.compile(GENERIC_KEYWORDS.get(claim.get("keyword"), r"$^"), re.I)
        matches = [r for r in runs if r.get("command") and rx.search(r["command"])
                   and not is_test_command(r["command"]) or (r.get("command") and rx.search(r["command"])
                                                              and claim.get("keyword") == "build")]
        label = "a %s command" % claim.get("keyword")
    if not runs:
        return _result(claim, "unverifiable", "no command log available (pass --log <transcript>)", [], "low")
    if not matches:
        if log and log.get("complete"):
            return _result(claim, "contradicted", "%s never ran in the transcript" % label,
                           [{"type": "transcript", "source": log["path"],
                             "commands": [r["command"] for r in log["runs"]][-10:]}], "medium")
        return _result(claim, "unverifiable", "%s does not appear in the log" % label, [], "low")
    last = matches[-1]
    ev = [_run_evidence(_as_test_run(last))]
    ec = last.get("exit_code")
    if ec is None:
        return _result(claim, "unverifiable", "%s ran but its exit code is unknown" % label, ev, "low")
    if ec != 0:
        return _result(claim, "contradicted", "%s exited with code %d (%s)" % (label, ec, last.get("source")), ev)
    conf = "medium" if last.get("exit_code_source") == "inferred" else "high"
    return _result(claim, "verified", "%s exited 0 (%s)" % (label, last.get("source")), ev, conf)


def verify_claims(claims, repo, fresh_runs, log):
    out = []
    for c in claims:
        k = c["kind"]
        if k == "tests_passed":
            out.append(verify_tests(c, fresh_runs, log))
        elif k == "command_succeeded":
            out.append(verify_command(c, fresh_runs, log))
        elif k == "file_changed":
            out.append(verify_file(c, repo))
        elif k == "symbol_added":
            out.append(verify_symbol(c, repo))
        elif k == "count":
            out.append(verify_count(c, repo))
    return out


# --------------------------------------------------------------------------
# receipt
# --------------------------------------------------------------------------

def summarize(claims):
    s = {"verified": 0, "contradicted": 0, "unverifiable": 0}
    for c in claims:
        s[c["status"]] += 1
    s["total"] = len(claims)
    return s


def base_receipt(repo):
    return {"tool": TOOL, "version": VERSION, "created_at": now_iso(),
            "repo": repo.info(), "repo_head": repo.head, "diff_sha256": repo.diff_sha256(),
            "changed_files": [{"path": p, "status": s, "added": repo.numstat.get(p, (None, None))[0],
                               "deleted": repo.numstat.get(p, (None, None))[1]}
                              for p, s in sorted(repo.changes.items())],
            "claims": [], "tests": None, "counterfactual": None,
            "summary": {"verified": 0, "contradicted": 0, "unverifiable": 0, "total": 0},
            "integrity": {"note": "sha256 values are integrity hashes for later comparison, not signatures",
                          "diff_sha256": "sha256 of `git diff --binary <base>` plus untracked file hashes"}}


def load_previous(out_path, receipt):
    """Merge tests/counterfactual from an earlier receipt for the same diff."""
    if not out_path or not os.path.exists(out_path):
        return None
    try:
        with open(out_path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        return None
    if prev.get("tool") != TOOL or prev.get("diff_sha256") != receipt.get("diff_sha256") \
            or prev.get("repo_head") != receipt.get("repo_head"):
        return None
    return prev


def write_receipt(receipt, out_path):
    if not out_path:
        return
    receipt["receipt_sha256"] = None
    body = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=False)
    receipt["receipt_sha256"] = sha256_text(body)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt, ensure_ascii=False, indent=2))
        fh.write("\n")
    os.replace(tmp, out_path)


MSG = {
    "en": {"verified": "VERIFIED", "contradicted": "CONTRADICTED", "unverifiable": "UNVERIFIABLE",
           "head": "receipts %s: %d claims - %d verified, %d contradicted, %d unverifiable",
           "none": "receipts %s: no checkable claims found in the report",
           "notgit": "note: %s is not a git repository; file and change checks are limited",
           "tests": "tests: `%s` exit %s - %s", "receipt": "receipt: %s",
           "cf": "counterfactual: %s"},
    "ja": {"verified": "検証済み", "contradicted": "矛盾", "unverifiable": "検証不能",
           "head": "receipts %s: 主張 %d 件 - 検証済み %d / 矛盾 %d / 検証不能 %d",
           "none": "receipts %s: 検証できる主張が報告から見つかりませんでした",
           "notgit": "注意: %s は git リポジトリではないため、ファイル変更の確認は限定的です",
           "tests": "テスト: `%s` 終了コード %s - %s", "receipt": "レシート: %s",
           "cf": "反実仮想テスト: %s"},
}
CF_MSG = {
    "en": {"exercises_change": "tests fail without the change, so they exercise it",
           "does_not_exercise_change": "WARNING: tests pass with AND without the change - they do not exercise it",
           "with_change_failing": "tests fail even with the change; comparison is inconclusive",
           "no_changes": "no implementation changes to remove", "refused": "refused",
           "safety_abort": "SAFETY ABORT"},
    "ja": {"exercises_change": "変更を外すとテストが失敗するため、テストは変更を検証しています",
           "does_not_exercise_change": "警告: 変更の有無に関わらずテストが通ります。テストは変更を検証していません",
           "with_change_failing": "変更ありでもテストが失敗するため、比較できません",
           "no_changes": "外す対象の実装変更がありません", "refused": "実行を拒否しました",
           "safety_abort": "安全のため中断"},
}


def human_summary(receipt, lang="en", out_path=None):
    m = MSG.get(lang, MSG["en"])
    lines = []
    s = receipt["summary"]
    if receipt.get("claims") is not None and (receipt.get("mode") == "check"):
        if s["total"]:
            lines.append(m["head"] % (VERSION, s["total"], s["verified"], s["contradicted"], s["unverifiable"]))
        else:
            lines.append(m["none"] % VERSION)
    if not receipt["repo"]["is_git"]:
        lines.append(m["notgit"] % receipt["repo"]["root"])
    order = {"contradicted": 0, "unverifiable": 1, "verified": 2}
    for c in sorted(receipt.get("claims") or [], key=lambda c: order[c["status"]]):
        lines.append("  [%s] %s: %s" % (m[c["status"]], c["kind"], c["reason"]))
        lines.append("      \"%s\"" % c["text"][:120])
    t = receipt.get("tests")
    if t:
        lines.append(m["tests"] % (t["command"], t["exit_code"], t["parsed"].get("summary_line") or
                                   ("timed out" if t["timed_out"] else "no summary line parsed")))
    cf = receipt.get("counterfactual")
    if cf:
        cm = CF_MSG.get(lang, CF_MSG["en"])
        text = cm.get(cf["verdict"], cf["verdict"])
        if cf.get("reason"):
            text += " (%s)" % cf["reason"]
        lines.append(m["cf"] % text)
        for name in (cf.get("tests_pass_both") or [])[:10]:
            lines.append("      - %s" % name)
        if cf.get("recovery"):
            lines.append("      " + cf["recovery"])
    if out_path:
        lines.append(m["receipt"] % out_path)
    return "\n".join(lines)


def check_exit_code(summary):
    if summary["contradicted"]:
        return EXIT_CONTRADICTED
    if summary["unverifiable"] or summary["total"] == 0:
        return EXIT_UNVERIFIABLE
    return EXIT_OK


def run_check(text, repo_path=".", base=None, log_path=None, run_tests_cmd=None, timeout=600,
              out_path=None):
    """Library entry point used by the CLI and the Stop hook. Returns the receipt dict."""
    repo = Repo(repo_path, base=base, exclude=[out_path] if out_path else [])
    receipt = base_receipt(repo)
    receipt["mode"] = "check"
    prev = load_previous(out_path, receipt)
    fresh = []
    if prev:
        receipt["tests"] = prev.get("tests")
        receipt["counterfactual"] = prev.get("counterfactual")
        if prev.get("tests"):
            fresh.append(prev["tests"])
    if run_tests_cmd:
        receipt["tests"] = test_record(run_command(run_tests_cmd, repo.root, timeout), "run-tests")
        fresh = [receipt["tests"]]
    log = parse_log(log_path) if log_path else None
    if log and log.get("complete"):
        repo.agent_committed = any(re.search(r"\bgit\b[^|;&]*\bcommit\b", r.get("command") or "")
                                   for r in log["runs"])
    if log:
        receipt["log"] = {"path": log_path, "kind": log["kind"], "commands_seen": len(log["runs"]),
                          "sha256": sha256_bytes(open(log_path, "rb").read())}
    claims = extract_claims(text)
    receipt["report_sha256"] = sha256_text(text)
    receipt["claims"] = verify_claims(claims, repo, fresh, log)
    receipt["summary"] = summarize(receipt["claims"])
    return receipt


# --------------------------------------------------------------------------
# counterfactual
# --------------------------------------------------------------------------

def git_dir(root):
    out = git_out(root, ["rev-parse", "--absolute-git-dir"])
    return out.strip() if out else None


def unsafe_state(root):
    """Return a reason string if stashing would be unsafe, else None."""
    gd = git_dir(root)
    if not gd:
        return "cannot locate the .git directory"
    for name, what in (("MERGE_HEAD", "a merge is in progress"), ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
                       ("REVERT_HEAD", "a revert is in progress"), ("rebase-merge", "a rebase is in progress"),
                       ("rebase-apply", "a rebase or am is in progress"), ("BISECT_LOG", "a bisect is in progress")):
        if os.path.exists(os.path.join(gd, name)):
            return what
    code, out, _ = git_run(root, ["ls-files", "-u"])
    if code != 0 or out.strip():
        return "there are unmerged paths"
    code, out, _ = git_run(root, ["ls-files", "-v"])
    if code == 0:
        for line in out.decode("utf-8", "replace").splitlines():
            if line[:1].islower() or line[:2] == "S ":
                return "some files are marked assume-unchanged or skip-worktree (%s)" % line[2:]
    code, out, _ = git_run(root, ["submodule", "status"])
    if code == 0 and out.strip():
        for line in out.decode("utf-8", "replace").splitlines():
            if line[:1] in "+-U":
                return "a submodule is modified or not initialised (%s)" % line.strip()
    if os.path.exists(os.path.join(gd, "index.lock")):
        return "another git process is running (index.lock exists)"
    return None


def worktree_fingerprint(root):
    """Hash of every tracked and untracked (non-ignored) path, plus the index."""
    code, out, _ = git_run(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if code != 0:
        raise RuntimeError("git ls-files failed")
    paths = sorted(set(p for p in out.decode("utf-8", "replace").split("\0") if p))
    files = {}
    for p in paths:
        full = os.path.join(root, p)
        try:
            st = os.lstat(full)
        except OSError:
            files[p] = "missing"
            continue
        if os.path.islink(full):
            files[p] = "link:" + os.readlink(full)
        elif os.path.isdir(full):
            files[p] = "dir"  # e.g. a nested repository
        else:
            h = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            files[p] = "%s:%o" % (h.hexdigest(), st.st_mode & 0o111)
    code, idx, _ = git_run(root, ["ls-files", "-s", "-z"])
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode() + b"\0" + idx).hexdigest()
    return {"digest": digest, "files": files}


def _fp_diff(a, b):
    keys = set(a["files"]) | set(b["files"])
    return sorted(k for k in keys if a["files"].get(k) != b["files"].get(k))


def _stash_entries(root):
    out = git_out(root, ["stash", "list", "--format=%H %gd %gs"]) or ""
    return [l.split(" ", 2) for l in out.splitlines() if l.strip()]


def _backup(root, gd, paths):
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + str(os.getpid())
    dest = os.path.join(gd, "receipts-backup", stamp)
    os.makedirs(dest, exist_ok=True)
    for p in paths:
        src = os.path.join(root, p)
        if not os.path.lexists(src):
            continue
        target = os.path.join(dest, "files", p)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.islink(src):
            os.symlink(os.readlink(src), target)
        elif os.path.isfile(src):
            shutil.copy2(src, target)
    return dest


def run_counterfactual(cmd, repo_path=".", timeout=600, keep_tests=True, keep_backup=False,
                       out_path=None, _hook_after_stash=None):
    """Run tests with and without the change. Always restores the working tree.

    Returns (receipt, exit_code)."""
    repo = Repo(repo_path, exclude=[out_path] if out_path else [])
    receipt = base_receipt(repo)
    receipt["mode"] = "counterfactual"
    receipt["claims"] = None
    prev = load_previous(out_path, receipt)
    if prev and prev.get("claims"):
        receipt["claims"] = prev["claims"]
        receipt["summary"] = prev.get("summary", receipt["summary"])
    cf = {"command": cmd, "keep_tests": keep_tests, "verdict": None}
    receipt["counterfactual"] = cf

    def refuse(reason):
        cf.update({"verdict": "refused", "reason": reason})
        return receipt, EXIT_BAD_INPUT

    if not repo.git:
        return refuse("git is not installed")
    if not repo.is_git:
        return refuse("%s is not a git repository" % repo.path)
    if not repo.head:
        return refuse("the repository has no commits; there is no baseline to compare with")
    if repo.base != repo.head:
        # working tree clean: Repo fell back to HEAD~1. Counterfactual only works on uncommitted changes.
        cf.update({"verdict": "no_changes", "reason": "no uncommitted changes (commit-level counterfactual is "
                   "not supported in v1)"})
        return receipt, EXIT_UNVERIFIABLE
    reason = unsafe_state(repo.root)
    if reason:
        return refuse(reason)
    root = repo.root
    changed = sorted(p for p in repo.changes)
    if out_path:
        rel_out = os.path.relpath(os.path.realpath(out_path), root).replace(os.sep, "/")
        changed = [p for p in changed if p != rel_out]
    impl = [p for p in changed if not (keep_tests and is_test_path(p))]
    kept = [p for p in changed if p not in impl]
    cf.update({"stashed_paths": impl, "kept_paths": kept})
    if not impl:
        cf.update({"verdict": "no_changes", "reason": "only test files changed" if kept else "no changes"})
        return receipt, EXIT_UNVERIFIABLE

    # 1) with the change
    with_run = test_record(run_command(cmd, root, timeout, {"PYTHONDONTWRITEBYTECODE": "1"}), "with-change")
    cf["with_change"] = with_run
    receipt["tests"] = with_run

    # 2) baseline (stash implementation changes, keep tests), restore in finally
    gd = git_dir(root)
    fp_before = worktree_fingerprint(root)
    # back up every changed file plus every untracked file present now (the with-change run may
    # have created some), so anything the baseline run touches can be put back byte for byte
    extra_untracked = [p for p in Repo(root).untracked() if p not in changed]
    backup_dir = _backup(root, gd, sorted(set(changed) | set(extra_untracked)))
    cf["backup_dir"] = backup_dir
    stash_sha = None
    baseline = None
    restored_ok = False
    recovery = None
    stashes_before = len(_stash_entries(root))
    msg = "receipts-counterfactual %s" % now_iso()
    try:
        spec = [":(literal)%s" % p for p in impl]
        code, out, err = git_run(root, ["stash", "push", "--include-untracked", "-m", msg, "--"] + spec)
        entries = _stash_entries(root)
        if code != 0 or len(entries) != stashes_before + 1 or msg not in entries[0][2]:
            if len(entries) == stashes_before + 1 and msg in entries[0][2]:
                stash_sha = entries[0][0]
            raise RuntimeError("git stash push failed: %s" % err.strip()[:300])
        stash_sha = entries[0][0]
        cf["stash_sha"] = stash_sha
        still = [p for p in impl if p in Repo(root, base="HEAD").changes]
        if still:
            raise RuntimeError("stash did not remove changes for: %s" % ", ".join(still[:5]))
        fp_stashed = worktree_fingerprint(root)
        if _hook_after_stash:
            _hook_after_stash(root)
        baseline = test_record(run_command(cmd, root, timeout, {"PYTHONDONTWRITEBYTECODE": "1"}), "baseline")
        cf["baseline"] = baseline
        # clean up anything the baseline run itself created/modified, so the stash applies cleanly
        fp_after_run = worktree_fingerprint(root)
        for p in _fp_diff(fp_stashed, fp_after_run):
            full = os.path.join(root, p)
            if p not in fp_stashed["files"]:  # new file made by the test run: move it aside
                target = os.path.join(backup_dir, "baseline-artifacts", p)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(full, target)
            elif p in kept or p in extra_untracked:  # restore our byte-for-byte copy
                src = os.path.join(backup_dir, "files", p)
                if os.path.lexists(full) and not os.path.islink(full) and os.path.isdir(full):
                    raise RuntimeError("baseline run replaced %s with a directory" % p)
                if os.path.lexists(full):
                    os.remove(full)
                if os.path.islink(src):
                    os.symlink(os.readlink(src), full)
                else:
                    shutil.copy2(src, full)
            else:
                git_run(root, ["checkout", "--", ":(literal)%s" % p])
        if worktree_fingerprint(root)["digest"] != fp_stashed["digest"]:
            raise RuntimeError("the baseline test run changed files that could not be reset")
    except Exception as exc:  # noqa: BLE001 - we must always reach the restore step
        cf["error"] = str(exc)
    finally:
        if stash_sha:
            entries = _stash_entries(root)
            ref = next((e[1] for e in entries if e[0] == stash_sha), None)
            if ref is None:
                recovery = "stash %s is missing from `git stash list`; files are backed up in %s" % (
                    stash_sha, backup_dir)
            else:
                code, _, err = git_run(root, ["stash", "apply", "--index", ref])
                if code != 0:
                    code, _, err = git_run(root, ["stash", "apply", ref])
                fp_after = worktree_fingerprint(root)
                if code == 0 and fp_after["digest"] == fp_before["digest"]:
                    git_run(root, ["stash", "drop", ref])
                    restored_ok = True
                else:
                    diff = _fp_diff(fp_before, fp_after)
                    recovery = ("restoration could not be verified (%s). Nothing was deleted: your changes are "
                                "in `git stash list` as %s (%s) and copied to %s. To recover: inspect "
                                "`git status`, then `git stash apply --index %s`."
                                % ("git stash apply failed: " + err.strip()[:200] if code else
                                   "mismatch in " + ", ".join(diff[:5]), ref, stash_sha[:12], backup_dir, ref))
        else:
            fp_after = worktree_fingerprint(root)
            if fp_after["digest"] == fp_before["digest"]:
                restored_ok = True
            else:
                recovery = ("the working tree differs from before but no stash was recorded; files are "
                            "copied to %s. Check `git stash list`." % backup_dir)
    cf["restored"] = restored_ok
    if not restored_ok:
        cf.update({"verdict": "safety_abort", "reason": cf.get("error") or "restoration mismatch",
                   "recovery": recovery})
        return receipt, EXIT_SAFETY
    if not keep_backup:
        shutil.rmtree(backup_dir, ignore_errors=True)
        cf["backup_dir"] = None
        parent = os.path.dirname(backup_dir)
        try:
            os.rmdir(parent)
        except OSError:
            pass
    if cf.get("error"):
        cf.update({"verdict": "refused", "reason": cf["error"]})
        return receipt, EXIT_BAD_INPUT

    # 3) compare
    if with_run["passed"] is not True:
        cf.update({"verdict": "with_change_failing", "reason": with_run["parsed"].get("summary_line") or
                   "exit %s" % with_run["exit_code"]})
        return receipt, EXIT_UNVERIFIABLE
    wn, bn = with_run["test_names"], baseline["test_names"]
    both = sorted(n for n, s in wn.items() if s == "passed" and bn.get(n) == "passed")
    only = sorted(n for n, s in wn.items() if s == "passed" and bn.get(n) in ("failed", "error"))
    cf["tests_pass_both"] = both
    cf["tests_fail_without_change"] = only
    if baseline["passed"] is True:
        cf.update({"verdict": "does_not_exercise_change",
                   "reason": "baseline: %s" % (baseline["parsed"].get("summary_line") or "exit 0")})
        return receipt, EXIT_CONTRADICTED
    cf.update({"verdict": "exercises_change",
               "reason": "baseline: %s" % (baseline["parsed"].get("summary_line") or "exit %s" % baseline["exit_code"])})
    if both:
        cf["note"] = "%d tests pass with and without the change (they do not exercise it)" % len(both)
    return receipt, EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(receipt, args, code, out_path):
    if getattr(args, "json", False):
        print(json.dumps({"exit_code": code, "receipt": receipt}, ensure_ascii=False, indent=2))
    else:
        print(human_summary(receipt, args.lang, out_path))


def _out_path(args, repo_path):
    if getattr(args, "no_write", False):
        return None
    if args.out:
        return os.path.abspath(args.out)
    return os.path.join(os.path.realpath(repo_path), DEFAULT_OUT)


def cmd_check(args):
    if args.claims and args.text:
        raise BadInput("give either --claims FILE or text after --, not both")
    if args.claims == "-":
        text = sys.stdin.read()
    elif args.claims:
        t = read_text(args.claims, limit=10_000_000)
        if t is None:
            raise BadInput("cannot read claims file: %s" % args.claims)
        text = t
    elif args.text:
        text = " ".join(args.text)
    else:
        raise BadInput("no report given: use --claims FILE, --claims - (stdin) or -- \"text\"")
    if args.log and not os.path.exists(args.log):
        raise BadInput("log file not found: %s" % args.log)
    out = _out_path(args, args.repo)
    receipt = run_check(text, args.repo, base=args.base, log_path=args.log, run_tests_cmd=args.run_tests,
                        timeout=args.timeout, out_path=out)
    code = check_exit_code(receipt["summary"])
    write_receipt(receipt, out)
    _emit(receipt, args, code, out)
    return code


def cmd_run_tests(args):
    out = _out_path(args, args.repo)
    repo = Repo(args.repo, exclude=[out] if out else [])
    receipt = base_receipt(repo)
    receipt["mode"] = "run-tests"
    receipt["claims"] = None
    prev = load_previous(out, receipt)
    if prev:
        receipt["claims"] = prev.get("claims")
        receipt["counterfactual"] = prev.get("counterfactual")
        receipt["summary"] = prev.get("summary", receipt["summary"])
    receipt["tests"] = test_record(run_command(args.cmd, repo.root, args.timeout), "run-tests")
    code = EXIT_OK if receipt["tests"]["passed"] is True else EXIT_CONTRADICTED
    write_receipt(receipt, out)
    _emit(receipt, args, code, out)
    return code


def cmd_counterfactual(args):
    out = _out_path(args, args.repo)
    receipt, code = run_counterfactual(args.cmd, args.repo, timeout=args.timeout,
                                       keep_tests=not args.all_changes, keep_backup=args.keep_backup,
                                       out_path=out)
    if code != EXIT_SAFETY:
        try:
            write_receipt(receipt, out)
        except OSError:
            pass
    else:
        write_receipt(receipt, None)
    _emit(receipt, args, code, out)
    if code == EXIT_SAFETY:
        sys.stderr.write("receipts: SAFETY ABORT. %s\n" % receipt["counterfactual"].get("recovery"))
    return code


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=".", help="repository directory (default: current directory)")
    common.add_argument("--json", action="store_true", help="print the full receipt as JSON on stdout")
    common.add_argument("--lang", choices=["en", "ja"], default="en", help="language of the human summary")
    common.add_argument("--out", help="receipt path (default: <repo>/receipt.json; it is excluded from the diff)")
    common.add_argument("--no-write", action="store_true", help="do not write a receipt file")
    common.add_argument("--timeout", type=int, default=600, help="seconds before a test command is killed")

    p = argparse.ArgumentParser(prog="receipts.py", description=__doc__.split("\n\n")[0],
                                epilog="Exit codes: 0 verified, 1 contradicted, 2 bad input/refused, "
                                       "3 unverifiable only, 4 safety abort.",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("check", parents=[common], help="extract and verify claims from a completion report",
                       description="Extract claims (tests passed, files changed, commands succeeded, "
                                   "symbols added, counts) from a report and verify them against the repo.")
    c.add_argument("--claims", help="file with the agent's report ('-' for stdin)")
    c.add_argument("--log", help="transcript (Claude Code JSONL), JSONL of {command,exit_code,output}, or text log")
    c.add_argument("--base", help="commit to compare against (default: HEAD, or HEAD~1 if the tree is clean)")
    c.add_argument("--run-tests", metavar="CMD", help="also run this test command and use its result as evidence")
    c.add_argument("text", nargs="*", help="report text (after --)")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run-tests", parents=[common], help="run a test command and record the result",
                       description="Run a test command with a timeout; record exit code, parsed counts and "
                                   "an output hash in the receipt. Exit 0 if tests passed, 1 otherwise.")
    r.add_argument("--cmd", required=True, help="test command, e.g. \"pytest -q\"")
    r.set_defaults(func=cmd_run_tests)

    f = sub.add_parser("counterfactual", parents=[common],
                       help="check whether the tests fail without the change",
                       description="Run tests with the change, stash the non-test changes (keeping test files), "
                                   "run tests on the baseline, then restore and verify the working tree. "
                                   "Exit 0 if tests fail without the change, 1 if they pass anyway.")
    f.add_argument("--cmd", required=True, help="test command, e.g. \"pytest -q\"")
    f.add_argument("--all-changes", action="store_true", help="also stash changed test files")
    f.add_argument("--keep-backup", action="store_true", help="keep the file backup in .git/receipts-backup")
    f.set_defaults(func=cmd_counterfactual)
    return p


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_BAD_INPUT
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_BAD_INPUT
    try:
        return args.func(args)
    except BadInput as exc:
        if getattr(args, "json", False):
            print(json.dumps({"exit_code": EXIT_BAD_INPUT, "error": str(exc)}))
        sys.stderr.write("receipts: error: %s\n" % exc)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
