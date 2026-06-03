"""
Layer 21 — Sandbox.

Применяет proposed_diff в временный git worktree, прогоняет pytest + py_compile.
Никогда не трогает основной checkout.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from ._db import get_proposal, set_sandbox_result

log = logging.getLogger("self_modify.sandbox")

REPO_ROOT = Path(os.getenv("PROJECTS_ROOT", "C:/Projects"))


def _run(cmd, cwd, timeout=180) -> Dict[str, Any]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, shell=isinstance(cmd, str),
        )
        return {"returncode": p.returncode,
                "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"returncode": -2, "stdout": "", "stderr": str(e)[:1000]}


def _copy_repo(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns(
        "__pycache__", ".git", ".venv", "venv", "node_modules",
        "_cache", "_reports", "*.log",
    )
    shutil.copytree(str(src), str(dst), ignore=ignore, dirs_exist_ok=True)


def _apply_patch(patch_text: str, repo_dir: Path) -> Dict[str, Any]:
    patch_file = repo_dir / "_proposal.patch"
    patch_file.write_text(patch_text, encoding="utf-8")
    # Сначала пробуем `git apply --check`, потом полноценно.
    chk = _run(["git", "apply", "--check", str(patch_file)], cwd=repo_dir)
    if chk["returncode"] != 0:
        # fallback: GNU patch
        chk2 = _run(["patch", "-p1", "--dry-run", "-i", str(patch_file)], cwd=repo_dir)
        if chk2["returncode"] != 0:
            return {"applied": False, "stage": "check", "git": chk, "patch": chk2}
        applied = _run(["patch", "-p1", "-i", str(patch_file)], cwd=repo_dir)
    else:
        applied = _run(["git", "apply", str(patch_file)], cwd=repo_dir)
    return {"applied": applied["returncode"] == 0, "stage": "apply", "result": applied}


def run_sandbox(proposal_id: int) -> Dict[str, Any]:
    proposal = get_proposal(proposal_id)
    if not proposal:
        return {"ok": False, "error": "proposal_not_found"}

    repo = proposal["target_repo"]
    src = REPO_ROOT / repo
    if not src.exists():
        result = {"ok": False, "error": f"repo_not_found:{src}"}
        set_sandbox_result(proposal_id, False, result)
        return result

    started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"sm_{proposal_id}_") as tmp:
        sandbox_dir = Path(tmp) / repo
        _copy_repo(src, sandbox_dir)

        apply_res = _apply_patch(proposal["proposed_diff"], sandbox_dir)
        if not apply_res["applied"]:
            result = {"ok": False, "stage": "apply", "details": apply_res,
                      "duration_s": round(time.time() - started, 2)}
            set_sandbox_result(proposal_id, False, result)
            return result

        # py_compile всех затронутых файлов
        files = proposal.get("target_files") or []
        compile_res = _run(
            ["python", "-m", "py_compile", *[str(sandbox_dir / f) for f in files]],
            cwd=sandbox_dir, timeout=60,
        )
        if compile_res["returncode"] != 0:
            result = {"ok": False, "stage": "py_compile", "details": compile_res,
                      "duration_s": round(time.time() - started, 2)}
            set_sandbox_result(proposal_id, False, result)
            return result

        # pytest (best-effort) — если тестов нет, считаем soft-pass
        pytest_res = _run(
            ["python", "-m", "pytest", "-q", "--maxfail=3", "--disable-warnings"],
            cwd=sandbox_dir, timeout=300,
        )
        # returncode 5 = no tests collected — терпимо
        pytest_ok = pytest_res["returncode"] in (0, 5)

        result = {
            "ok": pytest_ok,
            "stage": "pytest",
            "apply": apply_res,
            "compile": compile_res,
            "pytest": pytest_res,
            "duration_s": round(time.time() - started, 2),
        }
        set_sandbox_result(proposal_id, pytest_ok, result)
        return result
