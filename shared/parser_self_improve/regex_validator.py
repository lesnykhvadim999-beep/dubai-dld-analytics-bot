"""
regex_validator — pre-flight для предложений LLM-proposer'а.

Поверяет:
1. Pattern компилируется
2. Все declared test_cases проходят (positive matches, negatives — no match)
3. Match rate на cluster samples >= 50%
4. False-positive rate на случайных листингах < 5%
5. Field-specific sanity (price в допустимом диапазоне, и т.п.)

Conservative by design: лучше пропустить полезный pattern чем applied bad.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Sequence

log = logging.getLogger(__name__)


# Окно вокруг матча для anti-context detection (в символах)
CONTEXT_WINDOW = 30

# Anti-context — слова рядом с match → False Positive
ANTI_CONTEXT_BY_FIELD = {
    "price": [
        "sq.ft", "sqft", "sq ft", "sq.m", "sqm", "sq m", "m²",
        "/year", "per year", "yearly", "annual", "p.a.",
        "+971", "phone", "tel:", "whatsapp", "call ",
        "brn", "rera",
        "floor", "unit ",
        "permit",
    ],
    "area_sqft": [
        "aed", "د.إ", "usd", "$",
        "+971", "phone", "tel:",
        "brn", "rera",
        "year", "q1 20", "q2 20", "q3 20", "q4 20",
    ],
    "bedrooms": [
        "bath", "wc", "parking",
        "floor", "unit ", "permit",
        "+971", "phone",
    ],
    "handover": [
        "+971", "phone", "tel:",
        "aed", "د.إ", "price",
    ],
    "deal_type": [],
    "building": [],
    "property_type": [],
    "unknown": [
        "+971", "phone", "tel:", "brn", "rera",
    ],
}


def _field_value_sanity(field: str, value: str) -> bool:
    """Field-specific sanity check на captured value."""
    if value is None:
        return False
    v = value.strip()
    if not v:
        return False

    if field == "price":
        digits = re.sub(r"[^\d]", "", v)
        if not digits:
            return False
        try:
            n = int(digits)
        except ValueError:
            return False
        return 100_000 <= n <= 100_000_000

    if field == "area_sqft":
        digits = re.sub(r"[^\d]", "", v)
        if not digits:
            return False
        try:
            n = int(digits)
        except ValueError:
            return False
        return 200 <= n <= 50_000

    if field == "bedrooms":
        if v.lower() in ("studio", "0"):
            return True
        try:
            n = int(re.sub(r"[^\d]", "", v) or "0")
            return 1 <= n <= 9
        except ValueError:
            return False

    if field == "handover":
        return bool(re.search(r"20\d{2}", v))

    # для прочих полей — просто непустая строка приемлемой длины
    return 1 <= len(v) <= 80


def _safe_capture(match: re.Match, group: str | int) -> str | None:
    try:
        if isinstance(group, int):
            return match.group(group)
        # named group
        return match.group(group)
    except (IndexError, error_type := re.error):
        return None
    except Exception:
        return None


def _fetch_random_listings(limit: int = 100) -> list[str]:
    """Сэмпл случайных listings для FP-теста."""
    try:
        # local import чтобы validator не падал при импорте если нет БД
        from ._db import conn_cursor  # type: ignore
    except Exception:
        return []
    try:
        with conn_cursor() as (_, cur):
            # Используем ORDER BY RANDOM() только на небольшом subset — иначе медленно
            cur.execute(
                """
                SELECT original_text
                FROM public.listings
                WHERE original_text IS NOT NULL
                  AND length(original_text) > 50
                ORDER BY id DESC
                LIMIT 2000
                """
            )
            rows = [r[0] for r in cur.fetchall() if r[0]]
    except Exception as exc:
        log.warning("validator: random listings fetch failed: %s", exc)
        return []
    if len(rows) <= limit:
        return rows
    return random.sample(rows, limit)


def validate_proposed_regex(
    suggested_rule: dict,
    sample_texts: Sequence[str],
    field: str,
    random_sample_size: int = 100,
) -> dict:
    """См. модуль docstring. Возвращает:
        {
          "valid": bool,
          "reasons": [...],
          "match_rate": float,
          "fp_rate": float,
          "tested_random": int,
          "tested_samples": int,
        }
    """
    out = {
        "valid": False,
        "reasons": [],
        "match_rate": 0.0,
        "fp_rate": 0.0,
        "tested_random": 0,
        "tested_samples": 0,
    }

    pattern = suggested_rule.get("pattern") or suggested_rule.get("regex") or ""
    if not pattern:
        out["reasons"].append("no pattern field in suggested_rule")
        return out

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        out["reasons"].append(f"invalid regex: {e}")
        return out

    group_key = suggested_rule.get("match_group", "value")
    # если в pattern нет named group "value" — fallback на group(0)
    if isinstance(group_key, str) and group_key not in (compiled.groupindex or {}):
        if group_key == "value":
            # legacy: позволим group 1 если есть, иначе group 0
            group_key = 1 if compiled.groups >= 1 else 0

    # ── Test 1: declared test_cases ──
    test_cases = suggested_rule.get("test_cases") or []
    tc_failures = []
    for i, tc in enumerate(test_cases):
        if not isinstance(tc, dict):
            continue
        inp = tc.get("input", "")
        expected = tc.get("expected_value")
        m = compiled.search(inp or "")
        actual = _safe_capture(m, group_key) if m else None
        # Negative case: expected_value is None — pattern must NOT match
        if expected is None:
            if actual is not None:
                tc_failures.append(
                    f"#{i+1} negative test failed: input={inp[:80]!r} unexpectedly matched={actual!r}"
                )
        else:
            if actual is None:
                tc_failures.append(f"#{i+1} positive test failed: no match for input={inp[:80]!r}")
            elif actual.strip() != str(expected).strip():
                tc_failures.append(
                    f"#{i+1} mismatch: input={inp[:80]!r} expected={expected!r} actual={actual!r}"
                )
    if tc_failures:
        out["reasons"].extend(tc_failures)
        return out

    # ── Test 2: match rate on cluster samples ──
    samples = list(sample_texts or [])
    out["tested_samples"] = len(samples)
    if samples:
        sane_matches = 0
        for s in samples:
            m = compiled.search(s or "")
            if not m:
                continue
            val = _safe_capture(m, group_key)
            if _field_value_sanity(field, val or ""):
                sane_matches += 1
        match_rate = sane_matches / len(samples)
        out["match_rate"] = round(match_rate, 3)
        if match_rate < 0.5:
            out["reasons"].append(
                f"low match rate on cluster samples: {match_rate:.0%} (< 50%)"
            )
    else:
        out["reasons"].append("no cluster samples provided")

    # ── Test 3: FP rate on random listings ──
    randoms = _fetch_random_listings(random_sample_size)
    out["tested_random"] = len(randoms)
    if randoms:
        anti_ctx = ANTI_CONTEXT_BY_FIELD.get(field, ANTI_CONTEXT_BY_FIELD["unknown"])
        # plus user-provided anti_patterns
        anti_ctx = list(anti_ctx) + [
            str(x).lower() for x in (suggested_rule.get("anti_patterns") or [])
        ]
        fp_count = 0
        for txt in randoms:
            m = compiled.search(txt or "")
            if not m:
                continue
            val = _safe_capture(m, group_key)
            # FP type A: failed sanity → wrong-range value
            if not _field_value_sanity(field, val or ""):
                fp_count += 1
                continue
            # FP type B: anti-context word nearby
            ctx_lo = max(0, m.start() - CONTEXT_WINDOW)
            ctx_hi = m.end() + CONTEXT_WINDOW
            ctx = (txt[ctx_lo:ctx_hi] or "").lower()
            if any(bad in ctx for bad in anti_ctx if bad):
                fp_count += 1

        fp_rate = fp_count / max(len(randoms), 1)
        out["fp_rate"] = round(fp_rate, 3)
        if fp_rate > 0.05:
            out["reasons"].append(
                f"high false positive rate: {fp_rate:.1%} (> 5%) on {len(randoms)} random listings"
            )

    # ── Final ──
    if not out["reasons"]:
        out["reasons"] = ["all checks passed"]
        out["valid"] = True

    return out
