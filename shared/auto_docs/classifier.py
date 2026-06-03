"""LLM-based commit classifier.

Classifies a commit into:
    type:  bugfix | feature | refactor | docs | test | chore | schema
    scope: parser | analytics | bot | shared | docs | infra | ...

Heuristic fallback (no LLM): parse conventional-commit prefix.
"""
from __future__ import annotations
import json
import re
from typing import Dict

from . import llm_client


_TYPE_PREFIX = {
    "fix": "bugfix",
    "feat": "feature",
    "refactor": "refactor",
    "docs": "docs",
    "test": "test",
    "chore": "chore",
    "perf": "refactor",
    "build": "chore",
    "ci": "chore",
    "style": "chore",
}


def heuristic_classify(message: str, diff: str) -> Dict[str, str]:
    """Parse conventional commit `type(scope): subject`."""
    m = re.match(r"^(\w+)(?:\(([^)]+)\))?:", (message or "").strip())
    ctype = "chore"
    scope = "unknown"
    if m:
        ctype = _TYPE_PREFIX.get(m.group(1).lower(), "chore")
        scope = (m.group(2) or "unknown").lower()
    # Schema detection from diff
    if diff and re.search(r"(CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)", diff, re.I):
        return {"type": "schema", "scope": scope}
    return {"type": ctype, "scope": scope}


_PROMPT = """Classify this git commit. Reply with ONE LINE of JSON only.

Schema: {"type": "bugfix|feature|refactor|docs|test|chore|schema", "scope": "<one word>"}

Commit message:
{message}

Diff stats (truncated):
{diff}

JSON:"""


def classify(message: str, diff: str) -> Dict[str, str]:
    """LLM-classify, fall back to heuristic."""
    fallback = heuristic_classify(message, diff)
    prompt = _PROMPT.replace("{message}", (message or "")[:1500])\
                    .replace("{diff}", (diff or "")[:2000])
    raw = llm_client.llm_call(prompt, max_tokens=80, timeout=15)
    if not raw:
        return fallback
    # Extract first {...} block
    m = re.search(r"\{[^}]+\}", raw)
    if not m:
        return fallback
    try:
        obj = json.loads(m.group(0))
        t = str(obj.get("type", "")).lower()
        s = str(obj.get("scope", "")).lower()
        valid = {"bugfix", "feature", "refactor", "docs", "test", "chore", "schema"}
        if t not in valid:
            return fallback
        return {"type": t, "scope": s or fallback["scope"]}
    except Exception:
        return fallback
