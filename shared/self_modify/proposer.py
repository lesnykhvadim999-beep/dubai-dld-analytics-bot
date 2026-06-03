"""
Layer 21 — Patch proposer.

Берёт bug из bug_kb (Level 6 PHASE BL) или alert из auto_audit,
просит Cerebras сгенерировать unified diff и rationale.
Сохраняет в code_proposals (status='pending').
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from ._db import insert_proposal

log = logging.getLogger("self_modify.proposer")

CEREBRAS_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

REPO_ROOT = Path(os.getenv("PROJECTS_ROOT", "C:/Projects"))

SYSTEM = (
    "You are a senior Python engineer for Vadim Realty Telegram bot ecosystem. "
    "Generate MINIMAL unified diffs (patch -p1 compatible) that fix the specific bug. "
    "Never invent files. Never include secrets. Always preserve psycopg2 style. "
    "Output format STRICTLY:\n"
    "<RATIONALE>...short why and what...</RATIONALE>\n"
    "<DIFF>\n"
    "--- a/path/to/file.py\n"
    "+++ b/path/to/file.py\n"
    "@@ ... @@\n"
    " context\n"
    "-old\n"
    "+new\n"
    "</DIFF>\n"
    "If no safe fix is possible — output <RATIONALE>UNSAFE: reason</RATIONALE><DIFF></DIFF>."
)


def _read_snippets(files: List[str], max_lines_per_file: int = 200) -> str:
    """Соберём текущие куски кода для контекста."""
    out: list[str] = []
    for rel in files[:5]:
        path = REPO_ROOT / rel
        if not path.exists() or not path.is_file():
            out.append(f"### {rel}\n[file not found]")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
            slice_ = "\n".join(text[:max_lines_per_file])
            out.append(f"### {rel}\n```python\n{slice_}\n```")
        except Exception as e:  # noqa: BLE001
            out.append(f"### {rel}\n[read error: {e}]")
    return "\n\n".join(out)


def _ask_llm(prompt: str) -> Optional[str]:
    if not CEREBRAS_KEY:
        log.warning("CEREBRAS_API_KEY missing")
        return None
    try:
        r = requests.post(
            CEREBRAS_URL,
            headers={"Authorization": f"Bearer {CEREBRAS_KEY}"},
            json={
                "model": CEREBRAS_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.15,
                "max_tokens": 1800,
            },
            timeout=35,
        )
        if r.status_code != 200:
            log.warning("cerebras http=%s body=%s", r.status_code, r.text[:200])
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        log.exception("LLM call failed: %s", e)
        return None


def _split_response(text: str) -> Dict[str, str]:
    rationale = ""
    diff = ""
    m = re.search(r"<RATIONALE>(.*?)</RATIONALE>", text, re.DOTALL)
    if m:
        rationale = m.group(1).strip()
    m = re.search(r"<DIFF>(.*?)</DIFF>", text, re.DOTALL)
    if m:
        diff = m.group(1).strip()
    return {"rationale": rationale, "diff": diff}


def propose_fix(
    *,
    bug_id: Optional[str] = None,
    alert_id: Optional[int] = None,
    title: str,
    bug_description: str,
    target_repo: str,
    target_files: List[str],
    extra_context: str = "",
) -> Optional[int]:
    """
    Возвращает proposal_id или None. Никогда не применяет изменения сама.
    """
    snippets = _read_snippets(target_files)
    user_prompt = (
        f"BUG TITLE: {title}\n"
        f"BUG DESCRIPTION:\n{bug_description}\n\n"
        f"TARGET REPO: {target_repo}\n"
        f"TARGET FILES: {target_files}\n\n"
        f"CURRENT CODE:\n{snippets}\n\n"
        f"EXTRA:\n{extra_context}\n\n"
        "Сгенерируй минимальный безопасный патч."
    )
    raw = _ask_llm(user_prompt)
    if not raw:
        return None
    parts = _split_response(raw)
    if not parts["diff"] or parts["rationale"].startswith("UNSAFE"):
        log.info("proposer: no safe diff (%s)", parts["rationale"][:120])
        return None

    pid = insert_proposal(
        target_repo=target_repo,
        target_files=target_files,
        title=title,
        rationale=parts["rationale"],
        proposed_diff=parts["diff"],
        bug_id=bug_id,
        alert_id=alert_id,
    )
    log.info("proposer: proposal_id=%s", pid)
    return pid
