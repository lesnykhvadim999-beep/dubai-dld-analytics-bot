#!/usr/bin/env python3
"""
Pre-commit secrets scanner.

Reads a newline-separated list of staged .py file paths from stdin,
scans each file for hardcoded credentials, prints findings,
and exits 1 if anything suspicious is found, 0 otherwise.

Whitelist (skipped):
  - filename starts with `_`        (e.g. _scratch.py)
  - filename starts with `test_`    (test fixtures often contain fake tokens)
  - filename ends with `_test.py`
  - any path under `attached_assets/` (Replit scratch dumps)
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
from typing import List, Tuple

# (name, compiled regex) — order = priority of message
PATTERNS: List[Tuple[str, re.Pattern]] = [
    # Telegram bot token literal: 8-12 digits ':' 35+ url-safe chars
    ("telegram_bot_token", re.compile(r"""['"]\d{8,12}:[a-zA-Z0-9_-]{35,}['"]""")),
    # OpenAI / Anthropic style keys
    ("openai_key", re.compile(r"""['"]sk-[a-zA-Z0-9]{40,}['"]""")),
    # Google API keys
    ("google_api_key", re.compile(r"""['"]AIza[a-zA-Z0-9_-]{30,}['"]""")),
    # PostgreSQL connection URL with embedded password
    ("postgres_url", re.compile(r"""postgresql://[^"'\s]+:[^"'\s]+@[^"'\s]+""")),
    # Generic *_TOKEN = "<>=30 chars>"
    ("token_literal", re.compile(r"""[a-zA-Z0-9_]+_TOKEN\s*=\s*['"][^'"]{30,}['"]""")),
    # Generic *_KEY = "<>=30 url-safe chars>"
    ("api_key_literal", re.compile(r"""[a-zA-Z0-9_]+_KEY\s*=\s*['"][a-zA-Z0-9_-]{30,}['"]""")),
    # Generic *_HASH = "<32+ hex chars>"  (Telegram api_hash etc.)
    ("hash_literal", re.compile(r"""[a-zA-Z0-9_]+_HASH\s*=\s*['"][a-f0-9]{32,}['"]""")),
]

WHITELIST_GLOBS = ("_*.py", "test_*.py", "*_test.py")


def is_whitelisted(path: str) -> bool:
    norm = path.replace("\\", "/")
    if "attached_assets/" in norm:
        return True
    base = os.path.basename(norm)
    for pat in WHITELIST_GLOBS:
        if fnmatch.fnmatch(base, pat):
            return True
    return False


def scan_file(path: str) -> List[Tuple[int, str, str]]:
    """Return list of (line_no, pattern_name, matched_text)."""
    findings: List[Tuple[int, str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                # Skip obvious comments quickly — but only pure comment lines.
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for name, rx in PATTERNS:
                    m = rx.search(line)
                    if m:
                        findings.append((line_no, name, m.group(0)))
                        break  # one finding per line is enough
    except FileNotFoundError:
        # File staged-then-deleted; nothing to scan.
        return []
    except OSError as exc:
        print(f"[precommit_secrets_check] WARN: cannot read {path}: {exc}",
              file=sys.stderr)
        return []
    return findings


def main() -> int:
    raw = sys.stdin.read()
    files = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    any_findings = False
    for path in files:
        if not path.endswith(".py"):
            continue
        if is_whitelisted(path):
            continue
        findings = scan_file(path)
        if not findings:
            continue
        any_findings = True
        for line_no, name, text in findings:
            # Truncate matched text for safety in logs.
            shown = text if len(text) <= 80 else text[:77] + "..."
            print(f"SECRET DETECTED  {path}:{line_no}  [{name}]  {shown}")

    if any_findings:
        print("", file=sys.stderr)
        print("Commit blocked: hardcoded credentials detected.", file=sys.stderr)
        print("Move secrets to environment variables (os.environ.get(...))",
              file=sys.stderr)
        print("or to a .env file that is git-ignored.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
