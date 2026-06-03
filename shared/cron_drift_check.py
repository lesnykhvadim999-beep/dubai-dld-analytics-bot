"""
cron_drift_check.py — daily drift check для всей экосистемы.

Запускается по расписанию (cron / Railway schedule / cloud-watchdog),
читает все DSN из env, прогоняет validate_contracts() по полному списку
CONTRACTS, шлёт сводку в @vadim_admin_bot через admin_notify.

Запуск:
    python -m shared.cron_drift_check
    # или
    python cron_drift_check.py

Env (с фолбэками):
    LIVE_DATABASE_URL / DATABASE_URL
    ARCHIVE_DATABASE_URL
    RESALE_DATABASE_URL
    CURRENCY_DATABASE_URL
    ADMIN_TG_CHAT_ID, ADMIN_BOT_TOKEN — для admin_notify

Exit-коды:
    0 — успех (даже если есть drift; алерт уже отправлен)
    2 — runtime ошибка (cron должен залогировать)
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import traceback


def _build_dsns():
    return {
        "live":     os.getenv("LIVE_DATABASE_URL") or os.getenv("DATABASE_URL", ""),
        "archive":  os.getenv("ARCHIVE_DATABASE_URL", ""),
        "resale":   os.getenv("RESALE_DATABASE_URL") or os.getenv("DATABASE_URL", ""),
        "currency": os.getenv("CURRENCY_DATABASE_URL", ""),
    }


def _notify(text: str) -> None:
    """Best-effort: пробуем admin_notify если он есть, иначе stdout."""
    try:
        from admin_notify import admin_notify  # тот же что в боте
        admin_notify(text, priority="info")
        return
    except Exception:
        pass
    try:
        import urllib.request, urllib.parse, json
        token = os.getenv("ADMIN_BOT_TOKEN") or os.getenv("ALERT_BOT_TOKEN")
        chat = os.getenv("ADMIN_TG_CHAT_ID") or os.getenv("ALERT_CHAT_ID")
        if not token or not chat:
            print("[cron] no admin_notify available; printing only", flush=True)
            print(text, flush=True)
            return
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        urllib.request.urlopen(url, data=data, timeout=10).read()
    except Exception as e:
        print(f"[cron] telegram fallback failed: {e!r}", flush=True)
        print(text, flush=True)


def _render(rep) -> str:
    sev = rep.severity_summary()
    head_emoji = "🚨" if sev["CRITICAL"] else ("⚠️" if sev["HIGH"] else "✅")
    today = _dt.date.today().isoformat()
    lines = [f"{head_emoji} *DLD Schema Drift Check* — {today}",
             f"`{rep.summary()}`", ""]
    # Группируем violations по таблице.
    from collections import defaultdict
    by_table = defaultdict(list)
    for v in rep.violations:
        by_table[v.table].append(v)

    # Сначала список всех проверенных таблиц с ✅ / ⚠️
    from db_contracts import CONTRACTS
    for c in CONTRACTS:
        vios = by_table.get(c.table, [])
        if not vios:
            lines.append(f"✅ `{c.table}`: 0 violations")
        else:
            crit = sum(1 for v in vios if v.criticality == "CRITICAL")
            high = sum(1 for v in vios if v.criticality == "HIGH")
            marker = "🚨" if crit else "⚠️"
            lines.append(f"{marker} `{c.table}`: CRIT={crit} HIGH={high}")
            for v in vios[:5]:
                lines.append(f"   • {v.column}: {v.kind} ({v.actual or v.expected})")
            if len(vios) > 5:
                lines.append(f"   (+{len(vios)-5} more)")

    if rep.db_errors:
        lines.append("")
        lines.append("*DB errors:*")
        for e in rep.db_errors[:5]:
            lines.append(f"- {e}")
    return "\n".join(lines)


def main() -> int:
    try:
        # Импорты внутри main чтобы CLI выводил красивый traceback.
        from contract_validator import validate_contracts
        from db_contracts import CONTRACTS

        dsns = _build_dsns()
        present = [k for k, v in dsns.items() if v]
        print(f"[cron] DSNs available: {present}", flush=True)

        rep = validate_contracts(
            dsns=dsns,
            contracts=CONTRACTS,
            bot_name="cron-drift",
            sample_rows=5000,
        )
        msg = _render(rep)
        print(msg, flush=True)
        try:
            _notify(msg)
        except Exception as e:
            print(f"[cron] notify failed: {e!r}", flush=True)
        return 0
    except Exception:
        traceback.print_exc()
        try:
            _notify(f"🚨 cron_drift_check crashed:\n```\n{traceback.format_exc()[-1500:]}\n```")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
