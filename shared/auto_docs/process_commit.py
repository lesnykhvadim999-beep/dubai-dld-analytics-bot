"""Entry point for git post-commit hook.

Receives commit hash + message + diff stats, classifies the commit, and
dispatches updates to bug KB, README, changelog, contracts.

Fail-open: never raises (would block git operations). All errors go to
stderr.
"""
from __future__ import annotations
import argparse
import os
import sys
import traceback


def _ensure_path() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)


_ensure_path()

# After path fix.
from auto_docs import classifier, bug_kb_updater, readme_updater  # noqa: E402
from auto_docs import changelog, contracts_updater  # noqa: E402


def process(commit_hash: str, message: str, diff: str, repo_dir: str) -> dict:
    result = {"classification": None, "bug_kb": None, "readme": None,
              "changelog": None, "contracts": None, "errors": []}
    try:
        cls = classifier.classify(message, diff)
        result["classification"] = cls
    except Exception as e:
        result["errors"].append(f"classify: {e}")
        cls = {"type": "chore", "scope": "unknown"}

    if cls["type"] == "bugfix":
        try:
            entry = bug_kb_updater.add_entry(commit_hash, message, diff)
            if entry:
                result["bug_kb"] = "appended"
                # Derive bug id from entry header for matching auto-test name.
                import re as _re
                m = _re.search(r"###\s*(B\d{3})", entry)
                bug_id = m.group(1) if m else "B000"
            else:
                bug_id = "B000"
        except Exception as e:
            result["errors"].append(f"bug_kb: {e}")
            bug_id = "B000"
        # Generate regression test (Level 8).
        try:
            from auto_tests import generator as _gen
            path = _gen.generate(commit_hash, message, diff, bug_id)
            if path:
                result["auto_test"] = path
        except Exception as e:
            result["errors"].append(f"auto_test: {e}")

    if cls["type"] == "feature":
        try:
            sec = readme_updater.add_section(repo_dir, commit_hash, message, diff)
            if sec:
                result["readme"] = "appended"
        except Exception as e:
            result["errors"].append(f"readme: {e}")

    if cls["type"] == "schema":
        try:
            blk = contracts_updater.update(commit_hash, diff)
            if blk:
                result["contracts"] = "appended"
        except Exception as e:
            result["errors"].append(f"contracts: {e}")

    try:
        path = changelog.append(repo_dir, commit_hash, message, cls)
        result["changelog"] = path
    except Exception as e:
        result["errors"].append(f"changelog: {e}")

    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hash", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--diff", default="")
    p.add_argument("--repo", default=os.getcwd())
    args = p.parse_args()

    try:
        res = process(args.hash, args.message, args.diff, args.repo)
        sys.stderr.write(f"[auto_docs] {res}\n")
    except Exception:
        sys.stderr.write("[auto_docs] crash:\n" + traceback.format_exc())
    # Always succeed — git hook must not block on docs failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
