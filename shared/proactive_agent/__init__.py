"""
shared.proactive_agent — Phase BM, Layer 10: proactive agentic behavior.

Public API:
    from shared.proactive_agent import (
        register_trigger,          # create a trigger for a user
        run_scheduler_tick,        # scheduler entrypoint (cron / loop)
        send_notification,         # low-level sender (rate-limited)
        is_opted_out,
        reduce_notifications,
        block_notifications,
    )
"""
from .triggers import register_trigger, list_triggers, disable_trigger
from .scheduler import run_scheduler_tick
from .notifier import send_notification, OPT_OUT_CALLBACK_PREFIX
from .opt_out import (
    is_opted_out, reduce_notifications, block_notifications,
    handle_opt_out_callback,
)

__all__ = [
    "register_trigger", "list_triggers", "disable_trigger",
    "run_scheduler_tick",
    "send_notification", "OPT_OUT_CALLBACK_PREFIX",
    "is_opted_out", "reduce_notifications", "block_notifications",
    "handle_opt_out_callback",
]
