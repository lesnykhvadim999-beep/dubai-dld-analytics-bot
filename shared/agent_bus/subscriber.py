"""
agent_bus.subscriber — LISTEN on 'agent_events', dispatch by event_type.

Two modes:
  1. run_subscriber_loop(agent_name, dispatch_map) — long-lived (daemon).
  2. run_subscriber_once(agent_name, dispatch_map, batch=50) — cron-friendly:
     picks pending events for this agent's subscriptions, processes batch, exits.
"""
from __future__ import annotations

import json
import logging
import select
import time
from typing import Any, Callable, Dict, List, Optional

from ._db import connect, is_available
from .registry import heartbeat, list_agents

log = logging.getLogger("agent_bus.subscriber")

# dispatch_map: { event_type: callable(payload_dict, event_row) }
Handler = Callable[[Dict[str, Any], Dict[str, Any]], None]


def _subscriptions_for(agent_name: str) -> List[str]:
    for a in list_agents("active"):
        if a["agent_name"] == agent_name:
            return a["subscribes_to"]
    return []


def _claim_event(conn, event_id: int, agent_name: str) -> bool:
    """Atomically mark event as processed by this agent (idempotent)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_events
                   SET processed_by = processed_by || to_jsonb(%s::text),
                       status = CASE
                           WHEN status = 'pending' THEN 'processed'
                           ELSE status
                       END
                 WHERE id = %s
                   AND NOT (processed_by @> to_jsonb(%s::text))
                """,
                (agent_name, event_id, agent_name),
            )
            return cur.rowcount > 0
    except Exception as e:
        log.warning("claim_event failed id=%s: %s", event_id, e)
        return False


def _fetch_event(conn, event_id: int) -> Optional[Dict[str, Any]]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_bot, event_type, payload, created_at "
                "FROM agent_events WHERE id=%s",
                (event_id,),
            )
            r = cur.fetchone()
        if not r:
            return None
        return {
            "id": r[0],
            "source_bot": r[1],
            "event_type": r[2],
            "payload": r[3] or {},
            "created_at": r[4].isoformat() if r[4] else None,
        }
    except Exception:
        return None


def run_subscriber_once(
    agent_name: str,
    dispatch_map: Dict[str, Handler],
    batch: int = 50,
) -> int:
    """
    Cron-friendly: poll pending events matching subscriptions, dispatch, mark processed.
    Returns count of events processed.
    """
    if not is_available():
        return 0
    subs = list(dispatch_map.keys()) or _subscriptions_for(agent_name)
    if not subs:
        return 0
    heartbeat(agent_name)
    processed = 0
    try:
        conn = connect(autocommit=True)
        if conn is None:
            return 0
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_bot, event_type, payload, created_at
                  FROM agent_events
                 WHERE event_type = ANY(%s)
                   AND NOT (processed_by @> to_jsonb(%s::text))
                   AND created_at > NOW() - INTERVAL '24 hours'
                 ORDER BY id ASC
                 LIMIT %s
                """,
                (subs, agent_name, batch),
            )
            rows = cur.fetchall()

        for r in rows:
            event = {
                "id": r[0],
                "source_bot": r[1],
                "event_type": r[2],
                "payload": r[3] or {},
                "created_at": r[4].isoformat() if r[4] else None,
            }
            handler = dispatch_map.get(event["event_type"])
            if not handler:
                continue
            try:
                handler(event["payload"], event)
                if _claim_event(conn, event["id"], agent_name):
                    processed += 1
            except Exception as e:
                log.warning("handler error type=%s id=%s err=%s",
                            event["event_type"], event["id"], e)

        try:
            conn.close()
        except Exception:
            pass
    except Exception as e:
        log.warning("run_subscriber_once failed: %s", e)
    return processed


def run_subscriber_loop(
    agent_name: str,
    dispatch_map: Dict[str, Handler],
    poll_timeout: int = 30,
) -> None:
    """
    Long-lived daemon. LISTEN on 'agent_events', dispatch as notifications arrive.
    On idle timeout, also runs a once-pass to catch missed events.
    """
    if not is_available():
        log.warning("agent_bus subscriber: DB not available, sleeping")
        time.sleep(60)
        return

    backoff = 1.0
    while True:
        try:
            conn = connect(autocommit=True)
            if conn is None:
                time.sleep(10)
                continue
            with conn.cursor() as cur:
                cur.execute("LISTEN agent_events;")
            log.info("agent_bus: %s LISTENing on agent_events", agent_name)
            heartbeat(agent_name)
            backoff = 1.0

            # initial catchup
            run_subscriber_once(agent_name, dispatch_map, batch=100)

            while True:
                if select.select([conn], [], [], poll_timeout) == ([], [], []):
                    # idle: heartbeat + once-pass
                    heartbeat(agent_name)
                    run_subscriber_once(agent_name, dispatch_map, batch=50)
                    continue
                conn.poll()
                while conn.notifies:
                    n = conn.notifies.pop(0)
                    try:
                        data = json.loads(n.payload)
                    except Exception:
                        data = {}
                    etype = data.get("event_type")
                    if etype not in dispatch_map:
                        continue
                    event = _fetch_event(conn, data.get("id"))
                    if not event:
                        continue
                    try:
                        dispatch_map[etype](event["payload"], event)
                        _claim_event(conn, event["id"], agent_name)
                    except Exception as e:
                        log.warning("dispatch failed type=%s err=%s", etype, e)
        except Exception as e:
            log.warning("subscriber loop crash: %s — backoff %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
