"""Seed initial knowledge from existing hardcoded alias maps.

Run once after migrations.sql is applied:
    python -m shared.knowledge_graph.seed

Idempotent: ON CONFLICT DO NOTHING via unique (category, payload->>'key').
Auto-approves (these aliases were curated by Vadim already).
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

# ──────────────── ANALYTICS BUILDING_ALIASES (main.py:1103) ────────────────
ANALYTICS_BUILDING_ALIASES = {
    "grande signature": ["grande"],
    "grande signature residences": ["grande"],
    "address opera": ["address", "opera"],
    "the address opera": ["address", "opera"],
    "address residences dubai opera": ["address", "opera"],
    "corner": ["corner"],
    "binghatti corner": ["binghatti", "corner"],
    "jvc": ["jvc"],
}

# ──────────────── ANALYTICS AREA_ALIASES (main.py:734) ────────────────
ANALYTICS_AREA_ALIASES = {
    "jvc": ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jumeirah village circle": ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "downtown": ["Burj Khalifa"],
    "downtown dubai": ["Burj Khalifa"],
    "dubai downtown": ["Burj Khalifa"],
    "dubai marina": ["Marsa Dubai"],
    "marina": ["Marsa Dubai"],
    "marsa dubai": ["Marsa Dubai"],
    "business bay": ["Business Bay"],
    "palm": ["Palm Jumeirah"],
    "palm jumeirah": ["Palm Jumeirah"],
    "jlt": ["Jumeirah Lakes Towers"],
    "jumeirah lakes towers": ["Jumeirah Lakes Towers"],
    "creek": ["Dubai Creek Harbour", "Creek"],
    "dubai creek": ["Dubai Creek Harbour", "Creek"],
    "sobha": ["Sobha Hartland"],
    "sobha hartland": ["Sobha Hartland"],
}

# ──────────────── ANALYTICS _AREA_ALIASES_V66 (main.py:7523) ────────────────
ANALYTICS_AREA_ALIASES_V66 = {
    "jvc": ["JVC", "Jumeirah Village Circle", "Jumeirah Village", "Jumeirah Village Circle (JVC)",
            "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jumeirah village circle": ["JVC", "Jumeirah Village Circle", "Jumeirah Village",
                                "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jlt": ["JLT", "Jumeirah Lakes Towers", "Jumeirah Lake Towers"],
    "downtown": ["Downtown", "Downtown Dubai", "Burj Khalifa"],
    "downtown dubai": ["Downtown", "Downtown Dubai", "Burj Khalifa"],
    "dubai marina": ["Dubai Marina", "Marina", "Marsa Dubai"],
    "marina": ["Dubai Marina", "Marina", "Marsa Dubai"],
    "business bay": ["Business Bay"],
    "palm": ["Palm Jumeirah"],
    "palm jumeirah": ["Palm Jumeirah"],
}

# ──────────────── ANALYTICS _V106_BUILDING_ALIASES (main.py:11963) ────────────────
ANALYTICS_V106_BUILDING_ALIASES = {
    "address opera": ["address opera", "the address opera", "address dubai opera",
                       "the address dubai opera", "address residences dubai opera",
                       "the address residences dubai opera",
                       "The Address Residences Dubai Opera T1", "The Address Residences Dubai Opera T2",
                       "THE ADDRESS DUBAI OPERA", "Opera Grand", "address residences",
                       "residences dubai opera"],
    "the address opera": ["address opera", "the address opera", "address residences dubai opera",
                          "the address residences dubai opera", "address dubai opera",
                          "The Address Residences Dubai Opera T1", "The Address Residences Dubai Opera T2",
                          "THE ADDRESS DUBAI OPERA"],
    "address residences dubai opera": ["address residences dubai opera",
                                       "the address residences dubai opera",
                                       "The Address Residences Dubai Opera T1",
                                       "The Address Residences Dubai Opera T2",
                                       "THE ADDRESS DUBAI OPERA", "address opera"],
    "the address residences dubai opera": ["address residences dubai opera",
                                           "the address residences dubai opera",
                                           "The Address Residences Dubai Opera T1",
                                           "The Address Residences Dubai Opera T2",
                                           "THE ADDRESS DUBAI OPERA", "address opera"],
    "opera grand": ["opera grand", "Opera Grand", "il primo"],
    "opera grande": ["opera grande", "il primo", "the address residences dubai opera", "opera grand"],
    "grande burj khalifa": ["grande signature", "grande burj khalifa", "the grande", "grande residence"],
    "grande": ["grande signature", "the grande", "grande burj khalifa", "grande residence"],
    "burj khalifa": ["burj khalifa", "burj views", "the residences", "armani residence"],
    "binghatti corner": ["binghatti corner"],
    "corner": ["binghatti corner", "marina corner"],
    "marina gate": ["marina gate"],
    "burj vista": ["burj vista"],
    "address downtown": ["address downtown", "the address downtown"],
    "address residences": ["address residences"],
    "sobha hartland": ["sobha hartland"],
}

# ──────────────── Bug knowledge — short summaries ────────────────
KNOWN_BUGS = [
    ("B-jvc-area-empty", "JVC search returned empty when AREA_ALIASES not applied. Fix: map JVC -> Al Barsha South Fourth/Fifth + Al Hebiah First.",
     {"symptom": "JVC empty results", "fix": "apply AREA_ALIASES with DLD official names"}),
    ("B-grande-marketing-suffix", "DLD registry stores 'grande' without 'signature residences' marketing suffix. Fix: strip marketing suffixes for building lookup.",
     {"symptom": "Grande Signature returns no deals", "fix": "use short DLD name 'grande'"}),
    ("B-address-opera-multi-tower", "The Address Dubai Opera has T1/T2 + Opera Grand separate entries. Fix: query all variants.",
     {"symptom": "Address Opera deals split across towers", "fix": "include T1, T2, Opera Grand in aliases"}),
]


def seed_all(dry_run: bool = False) -> dict:
    """Seed all initial knowledge. Returns counters."""
    # late import so test of structure works without DB
    from .client import KG
    counts = {"building_alias": 0, "area_alias": 0, "edge_case": 0, "skipped": 0}

    def _push_building(key: str, aliases: list, canonical: str | None = None,
                        source: str = "analytics-bot-seed"):
        if dry_run:
            counts["building_alias"] += 1
            return
        # canonical: pick the first alias that's TitleCase-like, else key
        canonical = canonical or next((a for a in aliases if a[0].isupper()), key.title())
        desc = f"Building alias for '{key}': maps to canonical '{canonical}' (aliases: {', '.join(aliases[:5])})"
        payload = {"key": key, "aliases": aliases, "canonical": canonical}
        kid = KG.add("building_alias", payload, desc, source,
                     auto_approve=True, notify=False)
        if kid:
            counts["building_alias"] += 1
        else:
            counts["skipped"] += 1

    def _push_area(key: str, aliases: list, source: str = "analytics-bot-seed"):
        if dry_run:
            counts["area_alias"] += 1
            return
        canonical = next((a for a in aliases if a[0].isupper()), aliases[0] if aliases else key.title())
        desc = f"Area alias for '{key}': maps to DLD area(s) '{', '.join(aliases[:5])}'"
        payload = {"key": key, "aliases": aliases, "canonical": canonical}
        kid = KG.add("area_alias", payload, desc, source,
                     auto_approve=True, notify=False)
        if kid:
            counts["area_alias"] += 1
        else:
            counts["skipped"] += 1

    # Merge building maps (V106 wins over short list)
    merged_buildings: dict[str, list] = {}
    for src in (ANALYTICS_BUILDING_ALIASES, ANALYTICS_V106_BUILDING_ALIASES):
        for k, v in src.items():
            merged_buildings.setdefault(k.lower(), [])
            for alias in v:
                if alias not in merged_buildings[k.lower()]:
                    merged_buildings[k.lower()].append(alias)

    for key, aliases in merged_buildings.items():
        _push_building(key, aliases)

    # Merge area maps
    merged_areas: dict[str, list] = {}
    for src in (ANALYTICS_AREA_ALIASES, ANALYTICS_AREA_ALIASES_V66):
        for k, v in src.items():
            merged_areas.setdefault(k.lower(), [])
            for alias in v:
                if alias not in merged_areas[k.lower()]:
                    merged_areas[k.lower()].append(alias)

    for key, aliases in merged_areas.items():
        _push_area(key, aliases)

    # Edge cases / bug knowledge
    for key, desc, payload in KNOWN_BUGS:
        if dry_run:
            counts["edge_case"] += 1
            continue
        payload2 = {"key": key, **payload}
        kid = KG.add("edge_case", payload2, desc, "bug_knowledge_base",
                     auto_approve=True, notify=False)
        if kid:
            counts["edge_case"] += 1
        else:
            counts["skipped"] += 1

    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry = "--dry-run" in sys.argv
    if "--ensure-schema" in sys.argv:
        from ._db import ensure_schema
        ensure_schema()
        print("schema ensured")
    result = seed_all(dry_run=dry)
    print("SEED RESULT:", result)
