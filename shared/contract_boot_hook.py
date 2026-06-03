"""
contract_boot_hook.py — тонкая обёртка над contract_validator для bot main().

Цель: в каждом боте одна строка интеграции, неблокирующая.

Использование (sync main):
    from contract_boot_hook import install_contract_check
    install_contract_check(
        bot_name="analytics",
        dsns={"live": LIVE_DATABASE_URL, "archive": ARCHIVE_DATABASE_URL},
        contracts_filter=["dld_*", "users"],
        admin_notify=admin_notify,
    )

Использование (asyncio main):
    asyncio.create_task(async_contract_check(
        bot_name="analytics",
        dsns={...},
        contracts_filter=["dld_*"],
        admin_notify=admin_notify,
    ))

Поведение:
  - всегда стартует в фоне (отдельный thread / asyncio.to_thread).
  - не падает на ошибках — только логирует.
  - STRICT_CONTRACTS=1 (env) ⇒ при has_critical зовёт sys.exit(1).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from typing import Callable, Dict, Optional, Sequence

try:
    from contract_validator import validate_contracts_on_boot, ValidationReport
except Exception:
    from .contract_validator import validate_contracts_on_boot, ValidationReport  # type: ignore


def _default_log(msg: str) -> None:
    print(f"[contract] {msg}", flush=True)


def install_contract_check(bot_name: str,
                           dsns: Dict[str, str],
                           contracts_filter: Optional[Sequence[str]] = None,
                           admin_notify: Optional[Callable[[str], None]] = None,
                           strict_env: str = "STRICT_CONTRACTS",
                           log: Callable[[str], None] = _default_log,
                           delay_seconds: float = 5.0) -> threading.Thread:
    """Запускает validate_contracts_on_boot в фоновом thread.

    delay_seconds: даём боту полностью подняться (polling, health-server),
    потом дёргаем БД.
    """
    strict = (os.getenv(strict_env, "0").strip() in ("1", "true", "yes", "on"))

    def _worker():
        try:
            time.sleep(delay_seconds)
            t0 = time.time()
            rep = validate_contracts_on_boot(
                bot_name=bot_name,
                dsns=dsns,
                contracts_filter=contracts_filter,
            )
            dur = int((time.time() - t0) * 1000)
            sev = rep.severity_summary()
            if not rep.has_any:
                log(f"OK ({rep.tables_checked} tables, {dur}ms)")
                return
            log(f"drift detected: {rep.summary()}")
            for v in rep.violations[:20]:
                log(f"  {v}")
            for e in rep.db_errors[:5]:
                log(f"  db_error: {e}")
            if rep.has_critical and admin_notify:
                try:
                    admin_notify(
                        f"🚨 {bot_name}: schema drift CRITICAL={sev['CRITICAL']} HIGH={sev['HIGH']}\n"
                        + rep.render_telegram()
                    )
                except Exception as e:
                    log(f"admin_notify failed: {e!r}")
            if rep.has_critical and strict:
                log("STRICT_CONTRACTS=1 + critical drift → sys.exit(1)")
                os._exit(1)
        except Exception as e:
            log(f"validator worker crashed: {e!r}")
            log(traceback.format_exc())

    th = threading.Thread(target=_worker, name=f"contract-check-{bot_name}", daemon=True)
    th.start()
    return th


async def async_contract_check(bot_name: str,
                               dsns: Dict[str, str],
                               contracts_filter: Optional[Sequence[str]] = None,
                               admin_notify: Optional[Callable[[str], None]] = None,
                               strict_env: str = "STRICT_CONTRACTS",
                               log: Callable[[str], None] = _default_log,
                               delay_seconds: float = 5.0):
    """Async-вариант. Использовать как asyncio.create_task(async_contract_check(...))."""
    import asyncio

    strict = (os.getenv(strict_env, "0").strip() in ("1", "true", "yes", "on"))
    try:
        await asyncio.sleep(delay_seconds)
        rep = await asyncio.to_thread(
            validate_contracts_on_boot,
            bot_name=bot_name,
            dsns=dsns,
            contracts_filter=contracts_filter,
        )
        sev = rep.severity_summary()
        if not rep.has_any:
            log(f"OK ({rep.tables_checked} tables, {rep.duration_ms}ms)")
            return rep
        log(f"drift detected: {rep.summary()}")
        for v in rep.violations[:20]:
            log(f"  {v}")
        for e in rep.db_errors[:5]:
            log(f"  db_error: {e}")
        if rep.has_critical and admin_notify:
            try:
                admin_notify(
                    f"🚨 {bot_name}: schema drift CRITICAL={sev['CRITICAL']} HIGH={sev['HIGH']}\n"
                    + rep.render_telegram()
                )
            except Exception as e:
                log(f"admin_notify failed: {e!r}")
        if rep.has_critical and strict:
            log("STRICT_CONTRACTS=1 + critical drift → exit(1)")
            os._exit(1)
        return rep
    except Exception as e:
        log(f"async validator crashed: {e!r}")
        log(traceback.format_exc())
        return None
