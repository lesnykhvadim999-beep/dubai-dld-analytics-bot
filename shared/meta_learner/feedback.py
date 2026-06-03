"""Record outcome of a meta-learning decision."""
from __future__ import annotations

import logging
import os

from ._db import conn_cursor

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("META_LEARNER_ENABLED", "1").lower() not in ("0", "false", "off", "no")


def record_outcome(
    decision_id: int | None,
    score: float | None = None,
    label: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float = 0.0,
    llm_provider_used: str | None = None,
) -> bool:
    """Attach outcome to a previously logged decision. Best-effort."""
    if not _ENABLED or decision_id is None:
        return False

    if score is not None:
        score = max(0.0, min(1.0, float(score)))

    try:
        with conn_cursor() as (_, cur):
            cur.execute(
                """
                UPDATE meta_learning_decisions
                SET outcome_score      = COALESCE(%s, outcome_score),
                    outcome_label      = COALESCE(%s, outcome_label),
                    latency_ms         = COALESCE(%s, latency_ms),
                    cost_usd           = COALESCE(%s, cost_usd),
                    llm_provider_used  = COALESCE(%s, llm_provider_used),
                    feedback_recorded_at = NOW()
                WHERE id = %s
                """,
                (score, label, latency_ms, cost_usd, llm_provider_used, decision_id),
            )
        return True
    except Exception as e:
        log.debug("record_outcome failed for %s: %s", decision_id, e)
        return False


def record_user_reaction(decision_id: int | None, positive: bool) -> bool:
    """Shortcut: positive reaction = score 1.0, negative = 0.0."""
    score = 1.0 if positive else 0.0
    label = "success" if positive else "fail"
    return record_outcome(decision_id, score=score, label=label)
