"""
PHASE BM Layer 22 — Continuous Reasoning Loop.

После каждого user-запроса бот в фоне "додумывает":
что ещё было бы полезно сказать через час/день? Сохраняет в continuous_thoughts,
отправляет follow-up с opt-out. Rate-limit: 1/user/48ч.
"""
from .background_thinker import fire_background_think
from .delayed_followup import dispatch_due_followups
from .rate_limiter import can_send_followup, mark_optout, is_opted_out

__all__ = [
    "fire_background_think", "dispatch_due_followups",
    "can_send_followup", "mark_optout", "is_opted_out",
]
