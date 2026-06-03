"""
agent_bus.registry — UPSERT into agent_registry, heartbeat, query.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ._db import connect, is_available

log = logging.getLogger("agent_bus.registry")


def register_agent(
    agent_name: str,
    subscribes_to: Optional[List[str]] = None,
    handler_endpoint: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """UPSERT agent. Idempotent — safe to call on every bot startup."""
    if not is_available():
        return False
    subs = subscribes_to or []
    meta = meta or {}
    try:
        conn = connect()
        if conn is None:
            return False
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_registry (agent_name, subscribes_to, handler_endpoint, status, meta)
                VALUES (%s, %s, %s, 'active', %s::jsonb)
                ON CONFLICT (agent_name) DO UPDATE SET
                    subscribes_to    = EXCLUDED.subscribes_to,
                    handler_endpoint = EXCLUDED.handler_endpoint,
                    status           = 'active',
                    last_heartbeat   = NOW(),
                    meta             = EXCLUDED.meta
                """,
                (agent_name, subs, handler_endpoint, json.dumps(meta, default=str)),
            )
        conn.close()
        log.info("agent registered: %s subs=%s", agent_name, subs)
        return True
    except Exception as e:
        log.warning("register_agent failed: %s", e)
        return False


def heartbeat(agent_name: str) -> bool:
    if not is_available():
        return False
    try:
        conn = connect()
        if conn is None:
            return False
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_registry SET last_heartbeat = NOW(), status='active' "
                "WHERE agent_name=%s",
                (agent_name,),
            )
        conn.close()
        return True
    except Exception:
        return False


def list_agents(status: str = "active") -> List[Dict[str, Any]]:
    if not is_available():
        return []
    try:
        conn = connect()
        if conn is None:
            return []
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name, subscribes_to, handler_endpoint, status, "
                "last_heartbeat, meta FROM agent_registry WHERE status=%s",
                (status,),
            )
            rows = cur.fetchall()
        conn.close()
        return [
            {
                "agent_name": r[0],
                "subscribes_to": r[1] or [],
                "handler_endpoint": r[2],
                "status": r[3],
                "last_heartbeat": r[4].isoformat() if r[4] else None,
                "meta": r[5] or {},
            }
            for r in rows
        ]
    except Exception:
        return []
