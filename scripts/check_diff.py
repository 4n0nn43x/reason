#!/usr/bin/env python3
"""Détecteur de falsification de tests dans un diff (stdin). Sort 1 si suspect.
Signale : fichiers de tests/evals/harnais modifiés, marqueurs skip/xfail/only ajoutés, assertions supprimées,
workflows CI ou CODEOWNERS touchés. Usage : git diff origin/main...HEAD | python scripts/check_diff.py
"""
import re
import sys

PROTECTED = re.compile(r"^(tests?/|evals?/|\.github/|\.claude/|CODEOWNERS$|scripts/check_diff\.py$)|(^|/)(test_[^/]*|[^/]*(_test|\.test|\.spec))\.\w+$")
SKIP = re.compile(r"\+.*(pytest\.mark\.skip|\.skip\(|xfail|@unittest\.skip|\.only\(|it\.skip|describe\.skip|--no-verify|# *noqa: *S|SKIP_TESTS)", re.I)
ASSERT_REMOVED = re.compile(r"^-\s*(assert\b|self\.assert|expect\()")


def main() -> int:
    findings, current = [], None
    for line in sys.stdin:
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            current = m.group(2)
            if PROTECTED.search(current):
                findings.append(f"{current}: fichier protégé modifié")
            continue
        if current is None or line.startswith(("+++", "---")):
            continue
        if SKIP.search(line):
            findings.append(f"{current}: marqueur de contournement ajouté : {line.strip()[:80]}")
        if ASSERT_REMOVED.match(line):
            findings.append(f"{current}: assertion supprimée : {line.strip()[:80]}")
    for f in findings:
        print("SUSPECT", f)
    print("OK" if not findings else f"{len(findings)} alerte(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
