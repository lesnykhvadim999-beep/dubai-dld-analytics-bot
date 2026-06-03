"""Thin wrapper around shared/channel-poster llm_chain.

Locates a reachable llm_chain.py copy and exposes llm_call(prompt).
Fails open (returns None) if no provider is reachable.
"""
from __future__ import annotations
import os
import sys
import importlib.util
from typing import Optional

# Canonical locations (in order of preference).
_CANDIDATES = [
    "C:/Projects/channel-poster/llm_chain.py",
    "C:/Projects/audit-queue-processor/llm_chain.py",
    "C:/Projects/staging-processor/llm_chain.py",
    "C:/Projects/resale-bot/llm_chain.py",
    "C:/Projects/reelly-enrichment-service/llm_chain.py",
]

_llm_call = None


def _load() -> None:
    global _llm_call
    if _llm_call is not None:
        return
    for path in _CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("auto_docs_llm_chain", path)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "llm_call"):
                _llm_call = mod.llm_call
                return
        except Exception as e:
            sys.stderr.write(f"[auto_docs.llm_client] failed to load {path}: {e}\n")
            continue


def llm_call(prompt: str, max_tokens: int = 800, timeout: int = 20) -> Optional[str]:
    """Call shared LLM chain; return None if unavailable."""
    _load()
    if _llm_call is None:
        sys.stderr.write("[auto_docs.llm_client] no llm_chain.py reachable, skip\n")
        return None
    try:
        return _llm_call(prompt, max_tokens=max_tokens, timeout=timeout, use_cache=True)
    except Exception as e:
        sys.stderr.write(f"[auto_docs.llm_client] llm_call err: {e}\n")
        return None
