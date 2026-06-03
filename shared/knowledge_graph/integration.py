"""Drop-in helpers for migrating hardcoded BUILDING_ALIASES/AREA_ALIASES to KG.

Goal: zero-risk migration. Bot code keeps its hardcoded dict as fallback,
but routes lookup through KG first.

Usage in `dubai-dld-analytics-bot-main/main.py`:

    from shared.knowledge_graph.integration import resolve_aliases

    # BEFORE:
    # candidates = BUILDING_ALIASES.get(name.lower(), [name])

    # AFTER:
    candidates = resolve_aliases(
        name, category="building_alias",
        legacy_map=BUILDING_ALIASES, default=[name],
    )

Behavior:
  1. Try KG.get_aliases (timeout 100ms, cached 5min).
  2. If KG empty, return legacy_map.get(name.lower(), default).
  3. Never raises; safe for hot path.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Optional

from .client import KG


def resolve_aliases(
    name: str,
    category: str,
    legacy_map: Optional[Mapping[str, Iterable[str]]] = None,
    default: Optional[List[str]] = None,
) -> List[str]:
    if not name:
        return list(default or [])
    key = name.lower().strip()
    legacy = list((legacy_map or {}).get(key) or [])
    if legacy:
        # Pass legacy as fallback so KG miss still returns curated data.
        return KG.get_aliases(name, category=category, fallback=legacy)
    return KG.get_aliases(name, category=category, fallback=default or [name])


def merge_aliases_into_kg(legacy_map: Mapping[str, Iterable[str]], category: str,
                          source_bot: str) -> int:
    """One-shot importer if a bot has its own dict not yet in seed. Auto-approved."""
    added = 0
    for k, vals in (legacy_map or {}).items():
        aliases = [v for v in (vals or []) if v]
        if not aliases:
            continue
        canonical = next((a for a in aliases if a[0].isupper()), aliases[0])
        kid = KG.add(
            category,
            {"key": k.lower().strip(), "aliases": aliases, "canonical": canonical},
            description=f"{category} '{k}' → '{canonical}' (imported from {source_bot})",
            source_bot=source_bot, auto_approve=True, notify=False,
        )
        if kid:
            added += 1
    return added
