"""Hardcoded free-tier limits for known LLM / media providers.

Keys per dict: rpm, tpm, rpd, tpd (None means "no published cap").

Source: documented free tiers as of 2026-06 — adjust as providers change.
"""
from __future__ import annotations

from typing import Dict, Optional

from ._db import upsert_quota_limit, apply_schema

# ── known free-tier limits ───────────────────────────────────────────────────
LIMITS: Dict[str, Dict[str, Optional[int]]] = {
    # LLM providers (the 7 mentioned in PHASE BO O5)
    "cerebras":    {"rpm": 60,  "tpm": None,    "rpd": None,  "tpd": 1_000_000},
    "groq":        {"rpm": 30,  "tpm": 14_400,  "rpd": None,  "tpd": 500_000},
    "gemini":      {"rpm": 60,  "tpm": None,    "rpd": None,  "tpd": 1_000_000},
    "sambanova":   {"rpm": 60,  "tpm": None,    "rpd": 1000,  "tpd": None},
    "openrouter":  {"rpm": None,"tpm": None,    "rpd": 200,   "tpd": None},
    "mistral":     {"rpm": 60,  "tpm": None,    "rpd": None,  "tpd": None},  # 1 RPS
    "anthropic":   {"rpm": None,"tpm": None,    "rpd": None,  "tpd": None},  # paid last-resort

    # Bonus extras commonly used in this ecosystem (not strictly required by spec)
    "pollinations":{"rpm": 240, "tpm": None,    "rpd": None,  "tpd": None},  # ~4 RPS
    "telethon":    {"rpm": 1800,"tpm": None,    "rpd": None,  "tpd": None},  # 30 msg/sec floodwait
}

# canonical aliases (callers may pass lower/upper case)
_ALIASES = {
    "gemini-flash": "gemini",
    "gemini_flash": "gemini",
    "google-gemini": "gemini",
    "claude": "anthropic",
}


def _canonical(provider: str) -> str:
    p = (provider or "").strip().lower()
    return _ALIASES.get(p, p)


def get_limit(provider: str) -> Dict[str, Optional[int]]:
    """Returns limit dict (rpm/tpm/rpd/tpd) for provider. Empty dict if unknown."""
    return LIMITS.get(_canonical(provider), {})


def seed_limits() -> Dict[str, bool]:
    """Apply schema + UPSERT all known limits. Returns {provider: ok_bool}."""
    apply_schema()
    out: Dict[str, bool] = {}
    for provider, lim in LIMITS.items():
        out[provider] = upsert_quota_limit(
            provider,
            rpm=lim.get("rpm"),
            tpm=lim.get("tpm"),
            rpd=lim.get("rpd"),
            tpd=lim.get("tpd"),
        )
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(seed_limits(), indent=2))
