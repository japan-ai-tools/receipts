#!/usr/bin/env python3
"""Run the receipts bench: build each case as a small temporary git repo, run a
checker on the report, and measure detection rate and false-positive rate.

  python3 bench/run.py                  # measure receipts itself
  python3 bench/run.py --tool 'CMD'     # measure any other checker (see bench/README.md)

A checker "flags" a case when it exits with code 1. Detection rate = flagged
lying cases / lying cases. False-positive rate = flagged honest cases / honest
cases. Results go to bench/results.json (or --out).
"""

import argparse
import base64
import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_TOOL = "{python} %s check --repo {repo} --claims {report} {log_opt} --no-write" % shlex.quote(
    os.path.join(ROOT, "scripts", "receipts.py"))


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="bench", GIT_AUTHOR_EMAIL="bench@example.invalid",
               GIT_COMMITTER_NAME="bench", GIT_COMMITTER_EMAIL="bench@example.invalid",
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"] + list(args),
                   cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def write_files(repo, files):
    for rel, content in files.items():
        path = os.path.join(repo, rel)
        if content is None:
            if os.path.exists(path):
                os.remove(path)
            continue
        os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
        if content.startswith("base64:"):
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(content[7:]))
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)


def build_case(case, workdir):
    """Create <workdir>/repo (git) plus report.txt and optionally log.jsonl."""
    repo = os.path.join(workdir, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q")
    write_files(repo, case["base_files"])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    write_files(repo, case["work_files"])
    if case.get("commit_work"):
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "work")
    report = os.path.join(workdir, "report.txt")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(case["report"])
    log = None
    if case.get("log") is not None:
        log = os.path.join(workdir, "log.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            for entry in case["log"]:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return repo, report, log


def run_tool(template, repo, report, log, timeout):
    cmd = template.format(python=shlex.quote(sys.executable), repo=shlex.quote(repo),
                          report=shlex.quote(report), log=shlex.quote(log or ""),
                          log_opt=("--log %s" % shlex.quote(log)) if log else "")
    try:
        proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, "timeout"


def rate(num, den):
    return round(100.0 * num / den, 1) if den else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", default=os.path.join(HERE, "cases.json"))
    ap.add_argument("--tool", default=DEFAULT_TOOL,
                    help="command template; placeholders {python} {repo} {report} {log} {log_opt}")
    ap.add_argument("--out", default=os.path.join(HERE, "results.json"))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--verbose", "-v", action="store_true", help="print the checker output per case")
    args = ap.parse_args(argv)

    with open(args.cases, encoding="utf-8") as fh:
        data = json.load(fh)
    cases = data["cases"]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]

    rows = []
    for c in cases:
        tmp = tempfile.mkdtemp(prefix="receipts-bench-")
        try:
            repo, report, log = build_case(c, tmp)
            code, output = run_tool(args.tool, repo, report, log, args.timeout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        flagged = code == 1
        correct = flagged if c["label"] == "lying" else not flagged
        rows.append({"id": c["id"], "label": c["label"], "lang": c["lang"], "exit_code": code,
                     "flagged": flagged, "correct": correct, "note": c.get("note", "")})
        mark = "ok " if correct else "MISS" if c["label"] == "lying" else "FP "
        print("%s %s %-6s %s exit=%s %s" % (mark, c["id"], c["label"], c["lang"], code, c.get("note", "")))
        if args.verbose:
            print("    " + output.replace("\n", "\n    "))

    def stats(sub):
        lying = [r for r in sub if r["label"] == "lying"]
        honest = [r for r in sub if r["label"] == "honest"]
        return {"lying": len(lying), "honest": len(honest),
                "detected": sum(r["flagged"] for r in lying),
                "false_positives": sum(r["flagged"] for r in honest),
                "detection_rate_pct": rate(sum(r["flagged"] for r in lying), len(lying)),
                "false_positive_rate_pct": rate(sum(r["flagged"] for r in honest), len(honest)),
                "lying_exit_codes": _hist(lying), "honest_exit_codes": _hist(honest)}

    result = {"format": "receipts-bench-results/1",
              "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
              "tool": args.tool if args.tool != DEFAULT_TOOL else "receipts (scripts/receipts.py check)",
              "cases_file": os.path.relpath(args.cases, ROOT) if args.cases.startswith(ROOT) else args.cases,
              "overall": stats(rows),
              "by_lang": {lang: stats([r for r in rows if r["lang"] == lang])
                          for lang in sorted(set(r["lang"] for r in rows))},
              "cases": rows}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    o = result["overall"]
    print("\ndetection: %s/%s (%s%%)  false positives: %s/%s (%s%%)" % (
        o["detected"], o["lying"], o["detection_rate_pct"], o["false_positives"], o["honest"],
        o["false_positive_rate_pct"]))
    print("results: %s" % args.out)
    return 0


def _hist(rows):
    h = {}
    for r in rows:
        k = str(r["exit_code"])
        h[k] = h.get(k, 0) + 1
    return dict(sorted(h.items()))


if __name__ == "__main__":
    sys.exit(main())
