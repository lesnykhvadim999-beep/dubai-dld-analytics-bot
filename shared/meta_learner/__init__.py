"""shared.meta_learner — Phase BM Layer 15.

Public API:
    choose_strategy(family, task_type, context, bot_name, user_id) -> (decision_id, arm)
    record_outcome(decision_id, score, label, latency_ms, cost_usd, llm_provider_used)
    record_user_reaction(decision_id, positive: bool)
    render_meta_stats(days)   # admin /meta_stats
"""
from .selector import choose_strategy, ARMS, VALID_TASK_TYPES
from .feedback import record_outcome, record_user_reaction
from .introspect import render_meta_stats, get_meta_stats

__all__ = [
    "choose_strategy", "record_outcome", "record_user_reaction",
    "render_meta_stats", "get_meta_stats",
    "ARMS", "VALID_TASK_TYPES",
]
