"""
Layer 21 — Applier.

После approval применяет diff к боевому репо, делает git commit + push
(если есть remote). Railway сам перезапускает после push (стандартный workflow).
НИКОГДА не запускается без статуса 'approved'.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

from ._db import get_proposal, mark_applied

log = logging.getLogger("self_modify.applier")

REPO_ROOT = Path(os.getenv("PROJECTS_ROOT", "C:/Projects"))
COMMIT_AUTHOR = os.getenv("SELF_MODIFY_AUTHOR", "Agent L <agent-l@vadim-realty.local>")
DEFAULT_BRANCH = os.getenv("SELF_MODIFY_BRANCH", "agent-l-autopatch")

# Hard guard — никогда не пушим в main/master без human approval.
PROTECTED_BRANCHES = {"main", "master", "production", "prod", "release"}
if DEFAULT_BRANCH in PROTECTED_BRANCHES:
    raise RuntimeError(
        f"SELF_MODIFY_BRANCH={DEFAULT_BRANCH!r} is protected. "
        "Agent L MUST push to a separate branch (e.g. 'agent-l-autopatch')."
    )


def _run(cmd, cwd, timeout=120) -> Dict[str, Any]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout)
        return {"returncode": p.returncode,
                "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}
    except Exception as e:  # noqa: BLE001
        return {"returncode": -1, "stdout": "", "stderr": str(e)[:600]}


def apply_approved(proposal_id: int, *, push: bool = True) -> Dict[str, Any]:
    p = get_proposal(proposal_id)
    if not p:
        return {"ok": False, "error": "not_found"}
    if p["status"] != "approved":
        return {"ok": False, "error": f"status={p['status']} (need 'approved')"}

    repo_dir = REPO_ROOT / p["target_repo"]
    if not repo_dir.exists():
        return {"ok": False, "error": "repo_missing"}

    # Branch
    _run(["git", "checkout", "-B", DEFAULT_BRANCH], cwd=repo_dir)

    # Apply diff
    patch_file = repo_dir / "_proposal.patch"
    patch_file.write_text(p["proposed_diff"], encoding="utf-8")
    apply_res = _run(["git", "apply", "--whitespace=fix", str(patch_file)], cwd=repo_dir)
    try:
        patch_file.unlink()
    except Exception:
        pass
    if apply_res["returncode"] != 0:
        return {"ok": False, "stage": "apply", "details": apply_res}

    # Mandatory pytest sandbox gate — patch must not break tests.
    # Set SELF_MODIFY_SKIP_PYTEST=1 only in dev/test environments.
    if os.getenv("SELF_MODIFY_SKIP_PYTEST", "0") != "1":
        pytest_res = _run(
            ["python", "-m", "pytest", "-q", "--maxfail=1", "-x"],
            cwd=repo_dir, timeout=600,
        )
        if pytest_res["returncode"] != 0:
            # Revert applied changes — leave repo clean.
            _run(["git", "reset", "--hard", "HEAD"], cwd=repo_dir)
            return {"ok": False, "stage": "pytest_sandbox", "details": pytest_res}

    _run(["git", "add", "-A"], cwd=repo_dir)
    msg = f"agent-L: {p['title']} (proposal #{proposal_id})"
    commit_res = _run(
        ["git", "-c", f"user.name=Agent L",
         "-c", "user.email=agent-l@vadim-realty.local",
         "commit", "-m", msg],
        cwd=repo_dir,
    )
    if commit_res["returncode"] != 0:
        return {"ok": False, "stage": "commit", "details": commit_res}

    sha_res = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    sha = (sha_res["stdout"] or "").strip()

    push_res = None
    if push:
        # Refuse to push to protected branch even at runtime (belt-and-braces).
        if DEFAULT_BRANCH in PROTECTED_BRANCHES:
            return {"ok": False, "stage": "push", "details":
                    {"error": f"refusing push to protected branch {DEFAULT_BRANCH}"}}
        push_res = _run(["git", "push", "origin", DEFAULT_BRANCH], cwd=repo_dir, timeout=180)

    mark_applied(proposal_id, sha)
    return {"ok": True, "sha": sha, "push": push_res, "commit": commit_res}
