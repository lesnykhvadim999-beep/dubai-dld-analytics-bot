"""Meta-Learning strategy selector — epsilon-greedy multi-armed bandit.

Strategy families (and their arms):
  - provider:    {cerebras, groq, gemini_flash2, sambanova, openrouter, mistral, anthropic}
  - prompt:      {A, B, C}                  # short, medium, long template
  - cache:       {reuse, fresh}             # use cached result or regenerate
  - verbosity:   {short, detailed}          # response style per user type

Task types (rough taxonomy):
  extraction | summarization | reasoning | classification | generation

Usage in bot:
    from shared.meta_learner import choose_strategy, record_outcome

    decision_id, provider = choose_strategy(
        family="provider", task_type="summarization",
        context={"input_len": 1500, "user_lang": "ru"},
        bot_name="hub-bot", user_id=uid,
    )
    # ... do work with provider, measure latency ...
    record_outcome(decision_id, score=0.9, label="success",
                   latency_ms=820, llm_provider_used=provider)

Best-effort: any DB failure returns the default arm and logs.
"""
from __future__ import annotations

import logging
import os
import random
from typing import Any

from ._db import conn_cursor, ensure_schema as _ensure_schema

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("META_LEARNER_ENABLED", "1").lower() not in ("0", "false", "off", "no")
_EPSILON = float(os.environ.get("META_LEARNER_EPSILON", "0.1"))

# Canonical arm registry per family. Keep ordered (first = safe default).
ARMS: dict[str, list[str]] = {
    "provider": [
        "cerebras", "groq", "gemini_flash2",
        "sambanova", "openrouter", "mistral", "anthropic",
    ],
    "prompt": ["A", "B", "C"],
    "cache": ["reuse", "fresh"],
    "verbosity": ["short", "detailed"],
}

VALID_TASK_TYPES = {
    "extraction", "summarization", "reasoning", "classification", "generation"
}

_SCHEMA_READY = False


def _ensure() -> bool:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return True
    try:
        _ensure_schema()
        _SCHEMA_READY = True
        return True
    except Exception as e:
        log.warning("meta_learner schema init failed: %s", e)
        return False


def _load_weights(family: str, task_type: str) -> dict[str, dict[str, float]]:
    """Return {arm: {avg_score, weight, trials}} from DB; empty if none."""
    out: dict[str, dict[str, float]] = {}
    try:
        with conn_cursor(dict_cursor=True) as (_, cur):
            cur.execute(
                """
                SELECT strategy, avg_score, weight, trials
                FROM meta_strategy_weights
                WHERE strategy_family = %s AND task_type = %s
                """,
                (family, task_type),
            )
            for row in cur.fetchall():
                out[row["strategy"]] = {
                    "avg_score": float(row["avg_score"] or 0.5),
                    "weight": float(row["weight"] or 1.0),
                    "trials": int(row["trials"] or 0),
                }
    except Exception as e:
        log.debug("load_weights failed (%s/%s): %s", family, task_type, e)
    return out


def _pick_arm(family: str, task_type: str) -> str:
    arms = ARMS.get(family, [])
    if not arms:
        raise ValueError(f"Unknown strategy family: {family}")

    # epsilon-greedy explore
    if random.random() < _EPSILON:
        return random.choice(arms)

    weights = _load_weights(family, task_type)
    if not weights:
        return arms[0]

    # exploit: pick arm with highest avg_score * weight (with mild cold-start prior 0.5)
    best_arm = arms[0]
    best_value = -1.0
    for arm in arms:
        w = weights.get(arm)
        if w is None:
            value = 0.5  # cold-start prior
        else:
            value = w["avg_score"] * max(w["weight"], 0.01)
        if value > best_value:
            best_value = value
            best_arm = arm
    return best_arm


def choose_strategy(
    family: str,
    task_type: str,
    context: dict[str, Any] | None = None,
    bot_name: str | None = None,
    user_id: int | None = None,
) -> tuple[int | None, str]:
    """Pick a strategy arm and log the decision.

    Returns (decision_id, arm). decision_id may be None on DB failure
    but the arm is always usable.
    """
    if task_type not in VALID_TASK_TYPES:
        log.debug("unknown task_type %s, allowing through", task_type)

    if not _ENABLED:
        return None, ARMS.get(family, ["default"])[0]

    _ensure()
    try:
        arm = _pick_arm(family, task_type)
    except Exception as e:
        log.warning("pick_arm failed: %s", e)
        return None, ARMS.get(family, ["default"])[0]

    decision_id: int | None = None
    try:
        import json
        ctx = json.dumps(context or {}, ensure_ascii=False, default=str)
        with conn_cursor() as (_, cur):
            cur.execute(
                """
                INSERT INTO meta_learning_decisions
                  (task_type, chosen_strategy, strategy_family,
                   context_features, bot_name, user_id)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                RETURNING id
                """,
                (task_type, arm, family, ctx, bot_name, user_id),
            )
            row = cur.fetchone()
            decision_id = int(row[0]) if row else None
    except Exception as e:
        log.debug("log decision failed: %s", e)

    return decision_id, arm
