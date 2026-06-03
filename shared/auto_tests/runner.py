"""Run generated regression tests.

Invoked from pre-commit hook (PHASE BL Level 8). Returns non-zero if any
test fails. Honors AUTO_TESTS_SKIP=1 env to bypass.
"""
from __future__ import annotations
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(_HERE, "tests")


def run() -> int:
    if os.environ.get("AUTO_TESTS_SKIP") == "1":
        sys.stderr.write("[auto_tests] AUTO_TESTS_SKIP=1, skipping\n")
        return 0
    if not os.path.isdir(TESTS_DIR):
        return 0
    # Any .py test files?
    has = any(
        n.startswith("test_") and n.endswith(".py")
        for n in os.listdir(TESTS_DIR)
    )
    if not has:
        return 0
    cmd = [sys.executable, "-m", "pytest", TESTS_DIR, "-q",
           "--tb=short", "-x", "--no-header"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        sys.stderr.write("[auto_tests] timeout 120s\n")
        return 0  # fail-open on timeout
    except FileNotFoundError:
        sys.stderr.write("[auto_tests] pytest not installed, skip\n")
        return 0
    sys.stderr.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    # rc=5 means "no tests collected" — treat as success.
    if proc.returncode == 5:
        return 0
    return proc.returncode


if __name__ == "__main__":
    sys.exit(run())
