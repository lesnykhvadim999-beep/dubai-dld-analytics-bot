"""Daily Auto-Audit — runs once per day (Windows Task Scheduler 06:00 Asia/Dubai).

Checks A-I from the spec:
  A. Parser accuracy regression (vs yesterday)
  B. Per-property-type accuracy (top-5 categories)
  C. DB disk usage (all 4 DBs)
  D. Cron tick freshness (per-job heartbeat → audit_history)
  E. listings_staging backlog
  F. Bot HTTP healthchecks (`/health` on every Railway service)
  G. LLM provider health (free tier first)
  H. bot_errors aggregate (top-5 types in 24h, soft-skip if table absent)
  I. Leads intake (24h vs 7-day avg)

Writes a single HTML digest to @vadim_admin_bot and persists every metric
into `audit_history` so we can graph regressions later.

Invocation:
  py -3 C:\\Projects\\shared\\auto_audit\\daily_audit.py
"""
from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from auto_audit._common import (  # noqa: E402
    DSNS, BOTS, LLM_PROVIDERS,
    admin_notify, ensure_schema, record_metric, last_metric, metric_at_offset,
    q, http_probe, llm_probe,
)

# Predictive forecasts (Level 7). Import-safe: predictor module degrades
# gracefully if Prophet or pandas/numpy are missing.
try:
    from auto_audit.predictor import forecast_metric  # noqa: E402
    _HAS_PREDICTOR = True
except Exception as _e:  # pragma: no cover
    _HAS_PREDICTOR = False
    _PREDICTOR_ERR = str(_e)

# ────────────────────────── Constants ──────────────────────────
DUBAI_TZ = timezone(timedelta(hours=4))
NOW_DUBAI = datetime.now(DUBAI_TZ)
DB_LIMIT_MB = int(os.environ.get("DB_VOLUME_LIMIT_MB", "5000"))
STAGING_BACKLOG_LIMIT = int(os.environ.get("STAGING_BACKLOG_LIMIT", "50000"))
CRON_STALE_HOURS = int(os.environ.get("CRON_STALE_HOURS", "48"))

# ────────────────────────── Result container ──────────────────────────
class Section:
    __slots__ = ("title", "lines", "alerts")
    def __init__(self, title: str):
        self.title = title
        self.lines: list[str] = []
        self.alerts: list[str] = []

# ────────────────────────── A. Parser accuracy ──────────────────────────
def section_parser_accuracy() -> Section:
    s = Section("PARSER QUALITY")
    rows = q(DSNS["resale"], """
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE area IS NOT NULL AND building IS NOT NULL
                            AND price > 0 AND bedrooms IS NOT NULL
                            AND (size_sqft IS NOT NULL OR size_sqm IS NOT NULL)
                            AND property_type IS NOT NULL) AS fully_parsed,
          COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS added_24h
        FROM public.listings
        WHERE is_active = true
    """)
    if not rows or rows[0].get("_error"):
        err = rows[0].get("_error") if rows else "no rows"
        s.lines.append(f"❌ query failed: {err}")
        return s
    r = rows[0]
    total = int(r.get("total") or 0)
    fully = int(r.get("fully_parsed") or 0)
    added = int(r.get("added_24h") or 0)
    pct = (fully / total * 100) if total else 0.0

    prev = last_metric("parser.accuracy_pct")
    prev_pct = float(prev["value"]) if prev and prev.get("value") is not None else pct
    delta = pct - prev_pct

    prev_total = last_metric("parser.active_total")
    prev_total_val = int(prev_total["value"]) if prev_total and prev_total.get("value") is not None else total
    total_delta = total - prev_total_val

    s.lines += [
        f"├ Active: {total} (Δ {total_delta:+d} vs yesterday)",
        f"├ Fully parsed: {pct:.1f}% (Δ {delta:+.1f}%)",
        f"├ Added 24h: {added}",
    ]

    # Weekly delta: compare against ~7 days ago
    weekly = metric_at_offset("parser.accuracy_pct", 7)
    weekly_pct = float(weekly["value"]) if weekly and weekly.get("value") is not None else None
    weekly_delta = (pct - weekly_pct) if weekly_pct is not None else None

    if delta < -2.0:
        s.alerts.append(
            f"Parser accuracy regression {prev_pct:.1f}% → {pct:.1f}% (Δ {delta:+.1f}% vs yesterday)"
        )
        s.lines.append("└ 🔴 REGRESSION > 2% (daily)")
    elif delta < -0.5:
        s.lines.append(f"├ ⚠️ slight drop {delta:+.1f}% (daily)")
    else:
        s.lines.append("├ ✅ No daily regression")

    if weekly_delta is not None:
        s.lines.append(
            f"├ Weekly: {weekly_pct:.1f}% → {pct:.1f}% (Δ {weekly_delta:+.1f}%)"
        )
        if weekly_delta < -2.0:
            s.alerts.append(
                f"Parser accuracy WEEKLY regression {weekly_pct:.1f}% → {pct:.1f}% "
                f"(Δ {weekly_delta:+.1f}% over 7 days)"
            )
            s.lines.append("└ 🔴 WEEKLY REGRESSION > 2% — investigate")
        elif weekly_delta < -1.0:
            s.lines.append(f"└ ⚠️ weekly slow drift {weekly_delta:+.1f}%")
        else:
            s.lines.append("└ ✅ Weekly stable")
    else:
        s.lines.append("└ (weekly baseline not yet available)")

    record_metric("parser.active_total", total, {"added_24h": added})
    record_metric("parser.accuracy_pct", round(pct, 2),
                   {"fully_parsed": fully, "total": total})
    return s


# ────────────────────────── B. Per-category accuracy ──────────────────────────
def section_category_accuracy() -> Section:
    s = Section("CATEGORIES")
    rows = q(DSNS["resale"], """
        SELECT
          property_type,
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE area IS NOT NULL AND building IS NOT NULL
                            AND price > 0 AND bedrooms IS NOT NULL
                            AND (size_sqft IS NOT NULL OR size_sqm IS NOT NULL)) AS fully
        FROM public.listings
        WHERE is_active=true AND property_type IS NOT NULL
        GROUP BY property_type
        ORDER BY total DESC
        LIMIT 5
    """)
    if not rows or rows[0].get("_error"):
        s.lines.append(f"❌ {rows[0].get('_error') if rows else 'no rows'}")
        return s
    for i, r in enumerate(rows):
        pt = (r.get("property_type") or "?")[:20]
        total = int(r.get("total") or 0)
        fully = int(r.get("fully") or 0)
        pct = (fully / total * 100) if total else 0.0
        prefix = "└" if i == len(rows) - 1 else "├"
        flag = ""
        if pt.lower() == "apartment" and pct < 95.0:
            flag = " ⚠️"
            s.alerts.append(f"apartment accuracy {pct:.1f}% < 95%")
        s.lines.append(f"{prefix} {pt}: {pct:.1f}% ({fully}/{total}){flag}")
        record_metric(f"parser.cat.{pt}", round(pct, 2),
                       {"total": total, "fully": fully})
    return s


# ────────────────────────── C. DB disk usage ──────────────────────────
def section_db_disk() -> Section:
    s = Section("DATABASE")
    items = list(DSNS.items())
    for i, (name, dsn) in enumerate(items):
        prefix = "└" if i == len(items) - 1 else "├"
        if not dsn:
            s.lines.append(f"{prefix} {name}: no DSN (skipped)")
            continue
        rows = q(dsn, "SELECT pg_database_size(current_database())/1024/1024 AS mb")
        if not rows or rows[0].get("_error"):
            s.lines.append(f"{prefix} {name}: connect failed")
            continue
        mb = int(rows[0]["mb"])
        pct = mb / DB_LIMIT_MB * 100
        tag = ""
        if pct >= 90:
            tag = " 🔴 EXTEND"
            s.alerts.append(f"DB {name} {mb} MB ({pct:.0f}%) — extend volume")
        elif pct >= 80:
            tag = " ⚠️"
            s.alerts.append(f"DB {name} {mb} MB ({pct:.0f}%) — getting full")
        s.lines.append(f"{prefix} {name}: {mb} MB / {DB_LIMIT_MB} ({pct:.0f}%){tag}")
        record_metric(f"db.size_mb.{name}", mb, {"limit_mb": DB_LIMIT_MB})
    return s


# ────────────────────────── D. Cron tick freshness ──────────────────────────
# heartbeat metrics names — services should call record_metric("cron.tick.<job>")
# on every run; here we only audit.
CRON_JOBS = [
    "telethon_parser",
    "dld_sync",
    "daily_reports",
    "intelligence_updater",
    "staging_processor",
    "reelly_enrichment",
]

def section_cron() -> Section:
    s = Section("CRON HEALTH")
    rows = q(DSNS["resale"], """
        SELECT metric, MAX(ts) AS last_ts
        FROM audit_history
        WHERE metric LIKE 'cron.tick.%%'
        GROUP BY metric
    """)
    last_by_job: dict[str, datetime] = {}
    if rows and not rows[0].get("_error"):
        for r in rows:
            job = (r["metric"] or "").replace("cron.tick.", "")
            last_by_job[job] = r["last_ts"]
    for i, job in enumerate(CRON_JOBS):
        prefix = "└" if i == len(CRON_JOBS) - 1 else "├"
        last = last_by_job.get(job)
        if not last:
            s.lines.append(f"{prefix} ❓ {job} (no tick ever)")
            continue
        age = NOW_DUBAI.astimezone(timezone.utc) - last.astimezone(timezone.utc)
        hours = age.total_seconds() / 3600
        if hours >= CRON_STALE_HOURS:
            s.lines.append(f"{prefix} 🔴 {job} (no tick {hours:.0f}h)")
            s.alerts.append(f"cron {job} silent {hours:.0f}h")
        elif hours >= 24:
            s.lines.append(f"{prefix} ⚠️ {job} ({hours:.1f}h ago)")
        else:
            mins = age.total_seconds() / 60
            label = f"{int(mins)}m" if mins < 60 else f"{hours:.1f}h"
            s.lines.append(f"{prefix} ✅ {job} ({label} ago)")
    return s


# ────────────────────────── E. Staging backlog ──────────────────────────
def section_staging() -> Section:
    s = Section("STAGING BACKLOG")
    rows = q(DSNS["resale"], """
        SELECT
          COUNT(*) FILTER (WHERE status NOT IN ('promoted','rejected','spam','duplicate')) AS pending,
          COUNT(*) AS total
        FROM listings_staging
    """)
    if not rows or rows[0].get("_error"):
        s.lines.append("└ table missing (skip)")
        return s
    pending = int(rows[0].get("pending") or 0)
    total = int(rows[0].get("total") or 0)
    s.lines.append(f"├ Pending: {pending}")
    s.lines.append(f"└ Total: {total}")
    if pending > STAGING_BACKLOG_LIMIT:
        s.alerts.append(f"staging backlog {pending} > {STAGING_BACKLOG_LIMIT}")
    record_metric("staging.pending", pending, {"total": total})
    return s


# ────────────────────────── F. Bot HTTP health ──────────────────────────
def section_bot_health() -> Section:
    s = Section("BOT HEALTH")
    for i, (name, url) in enumerate(BOTS):
        prefix = "└" if i == len(BOTS) - 1 else "├"
        status, latency, body = http_probe(url, timeout=10)
        if status == 200:
            s.lines.append(f"{prefix} ✅ {name} ({latency:.0f}ms)")
        elif status == 0:
            s.lines.append(f"{prefix} 🔴 {name}: CONN FAIL ({body[:40]})")
            s.alerts.append(f"{name} unreachable: {body[:80]}")
        else:
            s.lines.append(f"{prefix} ⚠️ {name}: HTTP {status}")
            if status >= 500:
                s.alerts.append(f"{name} HTTP {status}")
        record_metric(f"bot.health.{name}",
                       1 if status == 200 else 0,
                       {"status": status, "latency_ms": int(latency)})
    return s


# ────────────────────────── G. LLM provider health ──────────────────────────
def section_llm() -> Section:
    s = Section("LLM CHAIN")
    for i, (name, env_key, url) in enumerate(LLM_PROVIDERS):
        prefix = "└" if i == len(LLM_PROVIDERS) - 1 else "├"
        ok, latency, info = llm_probe(name, env_key, url, timeout=6)
        tag = "✅" if ok else "⚠️"
        s.lines.append(f"{prefix} {tag} {name}: {info} ({latency:.0f}ms)")
        record_metric(f"llm.health.{name}", 1 if ok else 0, {"info": info})
    return s


# ────────────────────────── H. bot_errors aggregate ──────────────────────────
def section_errors() -> Section:
    s = Section("ERRORS 24h")
    rows = q(DSNS["resale"], """
        SELECT error_class, COUNT(*) AS c
        FROM bot_errors
        WHERE occurred_at > NOW() - INTERVAL '24 hours'
        GROUP BY error_class
        ORDER BY c DESC
        LIMIT 5
    """)
    if not rows:
        s.lines.append("└ no errors in last 24h")
        return s
    if rows[0].get("_error"):
        s.lines.append("└ no bot_errors table (skip)")
        return s
    for i, r in enumerate(rows):
        prefix = "└" if i == len(rows) - 1 else "├"
        s.lines.append(f"{prefix} {(r.get('error_class') or '?')[:30]}: {r.get('c')}")
    return s


# ────────────────────────── I. Leads intake ──────────────────────────
def section_user_feedback() -> Section:
    """Phase BL: bot_interactions + bot_feedback last 24h, with empty-rate anomalies."""
    s = Section("USER FEEDBACK")
    try:
        import sys as _ufs, os as _ufo
        for _p in (r"C:\Projects\shared", "/app/shared", "../"):
            if _ufo.path.isdir(_p) and _p not in _ufs.path:
                _ufs.path.insert(0, _p)
        from behavior_tracking.analytics import (
            daily_health, feedback_summary, empty_rate_anomalies,
        )
    except Exception as e:
        s.lines.append(f"└ behavior_tracking unavailable: {e}")
        return s
    health = daily_health(24)
    fb_idx = {r["bot_name"]: r for r in feedback_summary(24)}
    if not health and not fb_idx:
        s.lines.append("└ no interactions logged in last 24h")
        return s
    total_24h = 0
    for h in health:
        total_24h += int(h.get("total") or 0)
        bot = h["bot_name"]
        f = fb_idx.get(bot)
        fb_part = ""
        if f and (f.get("total") or 0):
            fb_part = (f" | {int(f.get('pos') or 0)}👍 / {int(f.get('neg') or 0)}👎"
                       f" / sat {f.get('satisfaction_pct') or 0}%")
        p95 = int(h.get("p95_ms") or 0)
        s.lines.append(
            f"├ {bot}: {int(h.get('total') or 0)} req · "
            f"{int(h.get('users') or 0)} users · "
            f"ok {h.get('success_pct') or 0}% · "
            f"empty {h.get('empty_pct') or 0}% · "
            f"p95 {p95}ms{fb_part}"
        )
        try: record_metric(f"bot.success_pct.{bot}", float(h.get('success_pct') or 0))
        except Exception: pass
        try: record_metric(f"bot.p95_ms.{bot}", float(p95))
        except Exception: pass
        if p95 and p95 > 2000:
            s.alerts.append(f"{bot}: p95 latency {p95}ms > 2s")
    anomalies = empty_rate_anomalies()
    if anomalies:
        s.lines.append("└ ⚠️ EMPTY-RATE anomalies:")
        for a in anomalies:
            s.lines.append(
                f"   • {a['bot_name']}: {a['today_pct']}% "
                f"(avg {a['mean_pct']}% ±{a['sd_pct']}%)"
            )
            s.alerts.append(
                f"{a['bot_name']}: empty rate {a['today_pct']}% > "
                f"avg+2σ ({a['mean_pct']}% ± {a['sd_pct']}%)"
            )
    else:
        s.lines.append("└ ✓ no empty-rate anomalies")
    record_metric("bot.interactions_24h", total_24h, {"bots": len(health)})
    return s


def section_leads() -> Section:
    s = Section("LEADS")
    rows = q(DSNS["resale"], """
        SELECT
          COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS leads_24h,
          COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days')  AS leads_7d
        FROM leads
    """)
    if not rows or rows[0].get("_error"):
        s.lines.append("└ leads table missing (skip)")
        return s
    l24 = int(rows[0].get("leads_24h") or 0)
    l7 = int(rows[0].get("leads_7d") or 0)
    avg = l7 / 7
    s.lines.append(f"├ 24h: {l24}")
    s.lines.append(f"└ Avg 7d: {avg:.1f}/day")
    if l24 == 0 and avg >= 1:
        s.alerts.append(f"0 leads in 24h (7-day avg {avg:.1f}/day)")
    record_metric("leads.intake_24h", l24, {"avg_7d": round(avg, 2)})
    return s


# ────────────────────────── J. Predictive forecasts ──────────────────────────
FORECAST_METRICS = [
    "parser.accuracy_pct",
    "db.size_mb.archive",
    "db.size_mb.resale",
    "db.size_mb.live",
    "db.size_mb.intelligence",
    "staging.pending",
    "leads.intake_24h",
]


def section_forecasts() -> "Section":
    s = Section("7-DAY FORECASTS")
    if not _HAS_PREDICTOR:
        s.lines.append("└ predictor unavailable (missing prophet/pandas)")
        return s
    items = FORECAST_METRICS
    any_data = False
    for i, metric in enumerate(items):
        prefix = "└" if i == len(items) - 1 else "├"
        try:
            r = forecast_metric(metric, days_ahead=7, use_cache=True)
        except Exception as e:
            s.lines.append(f"{prefix} {metric}: error ({type(e).__name__})")
            continue
        status = r.get("status")
        if status == "insufficient_history":
            need = r.get("need_samples")
            have = r.get("have_samples")
            s.lines.append(
                f"{prefix} {metric}: warming up ({have}/{need} samples)"
            )
            continue
        if status != "ok":
            s.lines.append(f"{prefix} {metric}: {status}")
            continue
        any_data = True
        cur = r.get("current") or 0
        fs = r.get("forecast_summary") or {}
        last = fs.get("yhat_last")
        if last is None or cur == 0:
            delta_pct = 0.0
        else:
            delta_pct = ((last - cur) / cur * 100) if cur else 0.0
        arrow = "→"
        if delta_pct > 1.0:
            arrow = "↗"
        elif delta_pct < -1.0:
            arrow = "↘"
        # Compose alert markers
        alerts = r.get("alerts") or []
        flag = ""
        if alerts:
            sev = alerts[0].get("severity", "")
            flag = " 🔴" if sev == "CRITICAL" else " ⚠️"
            for a in alerts:
                s.alerts.append(
                    f"FORECAST {metric}: {a.get('type')} "
                    f"({a.get('severity')}) — see Predictive Alert"
                )
        s.lines.append(
            f"{prefix} {metric}: {cur:.1f} {arrow} {last:.1f} "
            f"(Δ {delta_pct:+.1f}%){flag}"
        )
    if not any_data:
        s.lines.append(
            "└ (no metrics have ≥14 days of history yet — predictor warming up)"
        )
    return s


# ────────────────────────── Build digest ──────────────────────────
SECTION_EMOJI = {
    "PARSER QUALITY":   "🧠",
    "CATEGORIES":       "📂",
    "DATABASE":         "💾",
    "CRON HEALTH":      "⚙️",
    "STAGING BACKLOG":  "📥",
    "BOT HEALTH":       "🌐",
    "LLM CHAIN":        "🤖",
    "ERRORS 24h":       "❗",
    "LEADS":            "🎯",
    "USER FEEDBACK":    "🗣",
    "7-DAY FORECASTS":  "🔮",
    "KNOWLEDGE GRAPH":  "🧠",
}


# ────────────────────────── L5. Knowledge Graph ──────────────────────────
def section_knowledge_graph() -> Section:
    s = Section("KNOWLEDGE GRAPH")
    try:
        from shared.knowledge_graph.client import KG  # type: ignore
        stats = KG.stats() or {}
    except Exception as exc:
        s.lines.append(f"├ KG unavailable: {exc}")
        return s
    total = stats.get("total", 0)
    appr = stats.get("approved", 0)
    pend = stats.get("pending", 0)
    rej = stats.get("rejected", 0)
    used = stats.get("used_24h", 0)
    vrate = stats.get("validation_rate", 0)
    top = stats.get("by_category", []) or []
    top_str = ", ".join(f"{r['category']} ({r['n']})" for r in top[:3]) or "—"
    s.lines.append(f"├ Total entries: {total} ({appr} approved, {pend} pending, {rej} rejected)")
    s.lines.append(f"├ Used last 24h: {used:,} times")
    s.lines.append(f"├ Top categories: {top_str}")
    s.lines.append(f"├ Validation rate: {vrate}%")
    if pend:
        s.lines.append(f"└ ⏳ {pend} pending Vadim approval")
        if pend >= 5:
            s.alerts.append(f"KG: {pend} pending entries — review backlog")
    else:
        s.lines.append("└ ✅ no pending entries")
    return s


def run() -> tuple[str, list[str]]:
    ensure_schema()
    sections = [
        section_parser_accuracy(),
        section_category_accuracy(),
        section_db_disk(),
        section_cron(),
        section_staging(),
        section_bot_health(),
        section_llm(),
        section_errors(),
        section_leads(),
        section_user_feedback(),
        section_forecasts(),
        section_knowledge_graph(),
    ]
    import html as _html
    out: list[str] = []
    out.append(f"📊 <b>Daily Auto-Audit — {NOW_DUBAI.strftime('%Y-%m-%d %H:%M Asia/Dubai')}</b>")
    out.append("")
    actions: list[str] = []
    for sec in sections:
        emo = SECTION_EMOJI.get(sec.title, "•")
        out.append(f"{emo} <b>{sec.title}</b>")
        for line in (sec.lines or ["└ (empty)"]):
            out.append(_html.escape(line))
        out.append("")
        actions.extend(sec.alerts)
    if actions:
        out.append("⚠️ <b>ACTIONS NEEDED</b>")
        for i, a in enumerate(actions, 1):
            out.append(f"{i}. {_html.escape(a)}")
    else:
        out.append("✅ <b>No actions needed</b>")
    msg = "\n".join(out)
    return msg, actions


def main() -> int:
    t0 = time.time()
    msg, actions = run()
    priority = "critical" if actions else "normal"
    ok = admin_notify(msg, priority=priority)
    dt = time.time() - t0
    record_metric("audit.daily.duration_s", round(dt, 2),
                   {"alerts": len(actions), "sent": ok})
    print(f"[daily_audit] done in {dt:.1f}s, alerts={len(actions)}, sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
