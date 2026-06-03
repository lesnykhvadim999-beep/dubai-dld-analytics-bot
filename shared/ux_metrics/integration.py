"""
shared.ux_metrics.integration — tiny helpers for bot wiring.

Designed for resale-bot and dubai-dld-analytics-bot-main first (per PHASE BO O3
scope). Other bots can adopt the same helpers later — they're stateless.

Helpers:
    track_menu_shown(bot, user_id, menu_name, variant=None)
    track_button_click(bot, user_id, label, variant=None, extra=None)
    track_action_completed(bot, user_id, action, variant=None, extra=None)
    track_drop_off(bot, user_id, step, variant=None)
    track_error(bot, user_id, error_kind, variant=None)
    handle_no_analytics_command(bot, user_id, language='en') -> str
        — returns a localized confirmation; sets opt-out flag.

Variant resolver:
    resolve_variant(feature_name='ui_simplification',
                     enabled_variant='simple_menu_v2',
                     disabled_variant='legacy_menu') -> str
        Reads shared.safety_nets.feature_flags to decide which variant the
        user is currently in. Fail-open → enabled_variant.

All functions are non-blocking and never raise.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .tracker import track_event, set_opt_out, log_friction

log = logging.getLogger("ux_metrics.integration")


def resolve_variant(
    feature_name: str = "ui_simplification",
    enabled_variant: str = "simple_menu_v2",
    disabled_variant: str = "legacy_menu",
) -> str:
    """Map a feature flag → variant label used in ux_events.variant."""
    try:
        from shared.safety_nets.feature_flags import is_feature_enabled  # type: ignore
        return enabled_variant if is_feature_enabled(feature_name) else disabled_variant
    except Exception:
        return enabled_variant  # fail-open


# ── tiny one-liners ────────────────────────────────────────────────────────
def track_menu_shown(bot: str, user_id: Optional[int],
                     menu_name: str, variant: Optional[str] = None) -> None:
    track_event(bot, user_id, "menu_shown",
                {"menu": menu_name}, variant=variant)


def track_button_click(bot: str, user_id: Optional[int],
                       label: str, variant: Optional[str] = None,
                       extra: Optional[Dict[str, Any]] = None) -> None:
    props: Dict[str, Any] = {"label": label}
    if extra:
        props.update(extra)
    track_event(bot, user_id, "button_clicked", props, variant=variant)


def track_action_completed(bot: str, user_id: Optional[int],
                           action: str, variant: Optional[str] = None,
                           extra: Optional[Dict[str, Any]] = None) -> None:
    props: Dict[str, Any] = {"action": action}
    if extra:
        props.update(extra)
    track_event(bot, user_id, "action_completed", props, variant=variant)


def track_drop_off(bot: str, user_id: Optional[int],
                   step: str, variant: Optional[str] = None) -> None:
    track_event(bot, user_id, "drop_off",
                {"step": step}, variant=variant)
    log_friction(bot, user_id, "drop_off", {"step": step, "variant": variant})


def track_error(bot: str, user_id: Optional[int],
                error_kind: str, variant: Optional[str] = None) -> None:
    track_event(bot, user_id, "error_seen",
                {"kind": error_kind}, variant=variant)


# ── /no_analytics command ──────────────────────────────────────────────────
_OPT_OUT_REPLY = {
    "en": "Analytics disabled. Your interactions are no longer tracked.\n\n"
          "Vadim Realty | RERA BRN 65011",
    "ru": "Аналитика отключена. Ваши действия больше не отслеживаются.\n\n"
          "Vadim Realty | RERA BRN 65011",
}


def handle_no_analytics_command(bot: str, user_id: int,
                                 language: str = "en") -> str:
    """Sets opt-out flag for (user_id, bot) and returns a confirmation string."""
    try:
        set_opt_out(int(user_id), bot, opted=True)
    except Exception as e:
        log.warning("opt-out failed: %s", e)
    return _OPT_OUT_REPLY.get((language or "en").lower()[:2], _OPT_OUT_REPLY["en"])


# ── callback middleware (very thin) ────────────────────────────────────────
def callback_middleware(bot: str):
    """Returns a function `record(user_id, data)` that logs a button_clicked
    event with variant resolved from ui_simplification flag.

    Usage in resale_bot.py inside handle_cb:
        from shared.ux_metrics.integration import callback_middleware
        _ux_record = callback_middleware("resale")
        ...
        _ux_record(uid, data)
    """
    def record(user_id: Optional[int], data: Optional[str]) -> None:
        if not data:
            return
        try:
            label = data.split("|", 1)[0][:64]
            variant = resolve_variant()
            track_button_click(bot, user_id, label, variant=variant)
        except Exception:
            pass
    return record
