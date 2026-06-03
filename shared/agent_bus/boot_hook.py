"""
agent_bus.boot_hook — одностроковая интеграция publisher+registry в любой бот.

Использование в main.py любого бота:

    from shared.agent_bus.boot_hook import install_agent_bus
    install_agent_bus(
        bot_name="resale",
        subscribes_to=["user.lead_created", "market.shift_detected"],
    )

Поведение:
  - регистрирует бота в agent_registry
  - вызывает start_event_publishing(bot_name)
  - не блокирует boot; ошибки только в лог
"""
from __future__ import annotations

import logging
import threading
from typing import List, Optional

from .publisher import start_event_publishing
from .registry import register_agent, heartbeat

log = logging.getLogger("agent_bus.boot_hook")


def _install_immune(bot_name: str) -> None:
    """PHASE BN N2 — install universal exception catcher. Best-effort."""
    try:
        from shared.immune_system.boot_hook import install_immune_catcher
        install_immune_catcher(bot_name)
    except Exception as e:
        log.debug("immune_system install skipped for %s: %s", bot_name, e)


def install_agent_bus(
    bot_name: str,
    subscribes_to: Optional[List[str]] = None,
    handler_endpoint: Optional[str] = None,
) -> None:
    # PHASE BN N2 — install immune catcher synchronously (cheap; sets hooks)
    # so any exception raised during the rest of boot still gets captured.
    _install_immune(bot_name)

    def _run():
        try:
            start_event_publishing(bot_name)
            register_agent(
                agent_name=bot_name,
                subscribes_to=subscribes_to or [],
                handler_endpoint=handler_endpoint,
            )
            log.info("agent_bus installed for %s", bot_name)
        except Exception as e:
            log.warning("agent_bus install failed for %s: %s", bot_name, e)

    t = threading.Thread(target=_run, daemon=True, name=f"agent_bus_boot_{bot_name}")
    t.start()

    # Phase BN N3 — safety net heartbeat. Spawns a daemon thread that
    # writes to bot_heartbeats every 30 sec. NEVER auto-restarts.
    try:
        from shared.safety_nets import start_heartbeat_thread
        start_heartbeat_thread(bot_name)
    except Exception as e:
        log.warning("safety_nets heartbeat skipped for %s: %s", bot_name, e)

    # PHASE BO O5 — quota_tracker boot (idempotent schema apply + seed limits).
    # Runs once in a daemon thread to avoid blocking boot on slow DB connect.
    def _quota_boot():
        try:
            from shared.quota_tracker.boot import main as _qt_main
            _qt_main()
        except Exception as e:
            log.debug("quota_tracker boot skipped for %s: %s", bot_name, e)
    threading.Thread(target=_quota_boot, daemon=True,
                     name=f"quota_tracker_boot_{bot_name}").start()


def beat(bot_name: str) -> None:
    """Optional periodic heartbeat from main loop."""
    try:
        heartbeat(bot_name)
    except Exception:
        pass
