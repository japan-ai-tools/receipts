#!/usr/bin/env sh
# 10-second demo: a throwaway repo, an agent report with one false claim, and a receipt.
# Usage: sh examples/demo.sh        (needs git and python3; touches nothing outside a temp dir)
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
R="$HERE/scripts/receipts.py"
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
cd "$D"
git init -q
git -c user.name=demo -c user.email=demo@example.invalid commit -q --allow-empty -m init
printf 'def add(a, b):\n    return a - b\n' > calc.py
mkdir tests
printf 'import unittest\n\n\nclass T(unittest.TestCase):\n    def test_true(self):\n        self.assertTrue(True)\n' > tests/test_smoke.py
git add -A
git -c user.name=demo -c user.email=demo@example.invalid commit -q -m base

# the "agent" fixes calc.py ...
printf 'def add(a, b):\n    return a + b\n' > calc.py
# ... and reports:
REPORT='Fixed add() in calc.py and added tests/test_calc.py. All 3 tests passed.'
echo "report: $REPORT"
echo
python3 "$R" check --no-write --run-tests "python3 -m unittest discover -s tests -v" -- "$REPORT" || true
echo
python3 "$R" counterfactual --no-write --cmd "python3 -m unittest discover -s tests -v" || true
