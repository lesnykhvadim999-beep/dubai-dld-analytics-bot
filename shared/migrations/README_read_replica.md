# Read Replica for Analytics DB — Investigation Result

**Date:** 2026-05-30
**Decision:** **Do NOT provision a read replica at this time.** Skip Improvement #11.

## Investigation

Goals: offload heavy analytics queries (intel DB used by `dxb_stats_client.py`
in resale-bot and `read_model.py` in analytics-bot) onto a replica so writes
on the primary aren't blocked by long-running reads.

### What Railway exposes

`railway --version` → 4.58.0
Available subcommands relevant to databases:

| Subcommand | Purpose | Replica support? |
|---|---|---|
| `railway add --database postgres` | Provision a fresh Postgres service | No replica flag |
| `railway connect` | psql shell into an existing DB | n/a |
| `railway service` | Generic service mgmt | No replica subtree |
| `railway database` | **DOES NOT EXIST** (`unrecognized subcommand`) | — |

There is no first-class "read replica" abstraction in Railway CLI. The
official Railway plugin Postgres image is a single-node instance with no
streaming replication wired up.

### Alternatives considered

1. **Provision a second blank Postgres + `pg_basebackup` + streaming
   replication manually.** Possible, but:
   - Requires custom Docker images on both ends (Railway's plugin image
     doesn't expose `recovery.conf` or `primary_conninfo`).
   - WAL streaming over Railway's egress (public proxy) is fragile and
     expensive — bandwidth-billed.
   - No automated failover; would need pgbouncer or a custom router.

2. **Logical replication to a second Railway Postgres via `pg_logical` /
   `pglogical`.** Same problems plus extension install requires custom image.

3. **External hosted replica (Neon, Supabase, Crunchy Bridge).** Crosses the
   `No paid services` policy in memory (`feedback_no_paid_services.md`). Free
   tiers exist but they all require the primary to be reachable from the
   replica, which on Railway means exposing the primary publicly and paying
   for proxy egress.

### Why it's not worth it right now

Looking at the actual workload via `dxb_stats_client.py` and `read_model.py`:

- Both already read from a **pre-aggregated** `area_stats` / `building_stats`
  / `market_overview` table maintained by `dxb-stats-builder`. The raw
  multi-million-row JOINs that originally motivated this concern are NOT in
  the hot path any more.
- 5-second `statement_timeout` is set on both clients; long reads are
  capped before they can block writers.
- 60-second negative cache + 5-minute TTL cache mean each unique key hits
  the DB at most ~12 times an hour.
- Telemetry on Sentinel shows no `lock_timeout` or `deadlock_detected`
  events on the analytics DB in the last 30 days.

**Conclusion:** the bottleneck the replica would solve doesn't currently
exist on this workload. Adding a replica would introduce operational
complexity, a new failure mode (replica lag → stale answers), and a new
fallback code path to maintain, all to optimise a DB that's already idle
most of the time.

### When to revisit

Provision a replica only if **any** of the following becomes true:

- p95 latency of `dxb_stats_client.get_area_stats` exceeds 1500 ms for >1h
- Sentinel reports `db_queries_total{status="timeout"}` rising above 0.5%
- A new analytics-heavy bot ships that scans raw DLD tables outside the
  read-model
- We move off Railway to a host with first-class replicas (Crunchy, Aurora)

In that case, the cleanest implementation is to (a) add an
`INTEL_DB_REPLICA_URL` env var, (b) extend `_conn()` in both
`dxb_stats_client.py` and `read_model.py` to round-robin between primary
and replica with a try/except fallback, and (c) verify with `pg_is_in_recovery()`
on connect that we landed on the replica.

This document exists so the next session sees the decision and the trigger.
