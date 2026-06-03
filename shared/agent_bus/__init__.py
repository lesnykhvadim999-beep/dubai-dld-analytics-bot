"""
shared.agent_bus — PHASE BM Layer 12.

Centralized event bus using Postgres LISTEN/NOTIFY (no Redis).

Public API:
    publish_event(event_type, payload, source_bot=None)
    start_event_publishing(bot_name)         # call from each bot on startup
    register_agent(agent_name, subscribes_to=[...], handler=None)
    run_subscriber_loop(agent_name, dispatch_map)
"""
from .publisher import publish_event, start_event_publishing
from .registry import register_agent, heartbeat, list_agents
from .subscriber import run_subscriber_loop, run_subscriber_once

__all__ = [
    "publish_event",
    "start_event_publishing",
    "register_agent",
    "heartbeat",
    "list_agents",
    "run_subscriber_loop",
    "run_subscriber_once",
]
