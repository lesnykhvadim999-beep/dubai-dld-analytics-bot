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


def install_agent_bus(
    bot_name: str,
    subscribes_to: Optional[List[str]] = None,
    handler_endpoint: Optional[str] = None,
) -> None:
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


def beat(bot_name: str) -> None:
    """Optional periodic heartbeat from main loop."""
    try:
        heartbeat(bot_name)
    except Exception:
        pass
