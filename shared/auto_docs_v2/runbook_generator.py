"""PHASE BO O4 — runbook_generator.

Generates C:/Projects/shared/docs/ADMIN_RUNBOOK.md — a single merged document
containing architecture overview, known issues, emergency procedures, cron
schedule, DB schema, and the free LLM chain.

Sources (best-effort; missing → graceful skip):
- BL/BM/BN/BO phase layers: directory scan of shared/.
- Known issues: optional `bug_kb` table.
- Cron jobs: shared/scripts/phase_bm_master_cron.py JOBS list.
- DB schema: information_schema.tables (public schema).
- LLM chain order: shared/auto_docs/llm_client.py + memory reference.

DSN env vars only. NEVER includes secrets / tokens / DSNs in output.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .schema import get_dsn, upsert_doc_inventory

OUTPUT_PATH = Path(r"C:\Projects\shared\docs\ADMIN_RUNBOOK.md")
SHARED_ROOT = Path(r"C:\Projects\shared")
CRON_FILE = Path(r"C:\Projects\shared\scripts\phase_bm_master_cron.py")

_SECRET_PATTERNS = re.compile(
    r"(postgres(?:ql)?://[^\s]+|"
    r"\b[A-Za-z0-9_-]{20,}\b\s*[:=]\s*['\"][^'\"]+['\"]|"
    r"token['\"]?\s*[:=]\s*['\"][^'\"]+|"
    r"password['\"]?\s*[:=]\s*['\"][^'\"]+)",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


# ── 1. Architecture overview ──────────────────────────────────────────────────
def _architecture_section() -> str:
    """Detect BL/BM/BN/BO layer directories."""
    phases = {
        "BL": ["fsst_core.py", "vadim_pdf.py", "subscription.py"],
        "BM": ["causal_engine", "market_world_model", "agent_bus",
               "meta_learner", "content_pipeline", "session_continuity",
               "external_knowledge", "self_modify", "continuous_reasoning"],
        "BN": ["immune_system", "autonomous_audit", "optimizer", "safety_nets"],
        "BO": ["disaster_recovery", "auto_docs_v2"],
    }
    lines = ["## 1. Architecture Overview", ""]
    for phase, items in phases.items():
        present = [i for i in items if (SHARED_ROOT / i).exists()]
        lines.append(f"### Phase {phase}")
        if present:
            for p in present:
                kind = "dir" if (SHARED_ROOT / p).is_dir() else "module"
                lines.append(f"- `shared/{p}` ({kind})")
        else:
            lines.append("- _no components detected_")
        lines.append("")
    return "\n".join(lines)


# ── 2. Common issues ──────────────────────────────────────────────────────────
def _common_issues_section() -> str:
    dsn = get_dsn()
    lines = ["## 2. Common Issues & Solutions", ""]
    rows: list[tuple[str, str, str]] = []
    if dsn:
        try:
            import psycopg2  # type: ignore
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT to_regclass('bug_kb') IS NOT NULL,
                               to_regclass('bug_knowledge_base') IS NOT NULL
                    """)
                    bk1, bk2 = cur.fetchone()
                    table = "bug_kb" if bk1 else ("bug_knowledge_base" if bk2 else None)
                    if table:
                        cur.execute(
                            f"SELECT id, symptom, fix FROM {table} "
                            f"ORDER BY id DESC LIMIT 15"
                        )
                        for r in cur.fetchall():
                            rows.append((str(r[0]), (r[1] or "")[:120], (r[2] or "")[:200]))
        except Exception as e:
            sys.stderr.write(f"[runbook] bug_kb fetch failed: {e}\n")
    if rows:
        lines.append("| ID | Symptom | Fix |")
        lines.append("|----|---------|-----|")
        for rid, sym, fix in rows:
            sym = sym.replace("|", "/").replace("\n", " ")
            fix = fix.replace("|", "/").replace("\n", " ")
            lines.append(f"| {rid} | {sym} | {fix} |")
    else:
        lines += [
            "_No bug_kb table found. Hand-curated highlights:_",
            "",
            "- **Bot silent / no replies**: check Railway `logs --tail 50`; "
            "most often DSN env var missing or LLM chain exhausted (see §6).",
            "- **PDF generation slow**: Cerebras likely degraded — chain falls "
            "back to Groq → Together → Anthropic. Verify with "
            "`select * from llm_provider_health order by checked_at desc limit 10;`",
            "- **Duplicate listings in staging**: instant-delete should run; "
            "if not, check `staging-processor` health.",
            "- **Cron jobs not firing**: inspect "
            "`shared/scripts/phase_bm_cron_state.json` and master_cron logs.",
        ]
    lines.append("")
    return "\n".join(lines)


# ── 3. Emergency procedures ───────────────────────────────────────────────────
def _emergency_section() -> str:
    return """## 3. Emergency Procedures

### 3.1 Rollback a deploy (Railway)
```
C:\\Temp\\railway.exe status
C:\\Temp\\railway.exe rollback        # interactive picker
# or: redeploy a specific commit
git revert <bad-sha> && git push
```

### 3.2 Restore from backup
- Daily DR backup runs at 01:00 UTC (`dr_backup_daily`).
- Restore script: `python -m shared.disaster_recovery.restore --latest`.
- Verify checksums first with `--db intelligence --bucket daily`.

### 3.3 Disable a feature flag
- Feature flags live in env vars (e.g. `ENABLE_AI_CONSULT=0`).
- `C:\\Temp\\railway.exe variable set ENABLE_AI_CONSULT=0`
- Bot picks it up on next restart (or auto-restart via `/health`).

### 3.4 Kill a runaway cron job
- `shared/scripts/phase_bm_cron_state.json` holds last_run per job.
- Edit the file or delete the entry to force-skip until the next slot.
- For long-running rogue: kill via Railway dashboard → service → restart.

### 3.5 Database lock storm
- `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='active' AND query_start < now() - interval '5 min';`
- Use sparingly — prefer `pg_cancel_backend` first.
"""


# ── 4. Cron schedule ──────────────────────────────────────────────────────────
def _cron_section() -> str:
    lines = ["## 4. Cron Schedule (UTC)", ""]
    if not CRON_FILE.exists():
        lines.append("_master_cron file not found_")
        return "\n".join(lines)
    src = CRON_FILE.read_text(encoding="utf-8", errors="ignore")
    # Extract JOBS entries: {"name": "...", "cron": "...", ...}
    pat = re.compile(r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"cron"\s*:\s*"([^"]+)"')
    jobs = pat.findall(src)
    if not jobs:
        lines.append("_no JOBS detected_")
        return "\n".join(lines)
    lines.append("| Job | Cron | Description |")
    lines.append("|-----|------|-------------|")
    for name, cron in jobs:
        # crude description: derive from name
        desc = name.replace("_", " ").title()
        lines.append(f"| `{name}` | `{cron}` | {desc} |")
    lines.append("")
    lines.append("Add new jobs in `shared/scripts/phase_bm_master_cron.py` "
                 "(JOBS list) — never schedule outside this registry.")
    return "\n".join(lines)


# ── 5. Database schema ────────────────────────────────────────────────────────
def _db_schema_section() -> str:
    lines = ["## 5. Database Schema (public)", ""]
    dsn = get_dsn()
    if not dsn:
        lines.append("_no DSN env var available (see deploy environment)_")
        return "\n".join(lines)
    try:
        import psycopg2  # type: ignore
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name,
                           (SELECT count(*) FROM information_schema.columns c
                            WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name)
                    FROM information_schema.tables t
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
                rows = cur.fetchall()
    except Exception as e:
        lines.append(f"_schema fetch failed: {type(e).__name__}_")
        return "\n".join(lines)
    if not rows:
        lines.append("_no tables found_")
        return "\n".join(lines)
    lines.append(f"_Total tables: {len(rows)}_")
    lines.append("")
    lines.append("| Table | Columns |")
    lines.append("|-------|---------|")
    for name, cols in rows:
        lines.append(f"| `{name}` | {cols} |")
    lines.append("")
    lines.append("Full per-column definitions: query `information_schema.columns` "
                 "or use `\\d+ <table>` in psql.")
    return "\n".join(lines)


# ── 6. LLM chain ──────────────────────────────────────────────────────────────
def _llm_chain_section() -> str:
    return """## 6. Free LLM Chain Order

All LLM calls go through `shared/llm_chain.py` (synced into each bot). Order:

1. **Cerebras** (`CEREBRAS_API_KEY`) — fastest, free tier, primary.
2. **Groq** (`GROQ_API_KEY`) — fast fallback.
3. **Together AI** (`TOGETHER_API_KEY`).
4. **Gemini** (`GEMINI_API_KEY`) — used for translations / long-context.
5. **OpenRouter** (`OPENROUTER_API_KEY`) — multi-model proxy.
6. **Anthropic** (`ANTHROPIC_API_KEY`) — last-resort paid fallback (cap'd).

Health tracked in `llm_provider_health` table. Provider that returns 5xx /
429 is cooled-down for 5 min then re-tried.

**Never** hard-code a provider — always call `llm_chain.llm_call(prompt)`.
"""


# ── orchestrator ──────────────────────────────────────────────────────────────
def generate() -> Path:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    sections = [
        f"# Vadim Realty Ecosystem — Admin Runbook",
        f"",
        f"_Auto-generated by `shared/auto_docs_v2/runbook_generator.py` on "
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}._",
        f"",
        f"Brand: **Vadim Realty** · RERA BRN **65011**.",
        f"",
        "## Table of Contents",
        "1. [Architecture Overview](#1-architecture-overview)",
        "2. [Common Issues & Solutions](#2-common-issues--solutions)",
        "3. [Emergency Procedures](#3-emergency-procedures)",
        "4. [Cron Schedule (UTC)](#4-cron-schedule-utc)",
        "5. [Database Schema (public)](#5-database-schema-public)",
        "6. [Free LLM Chain Order](#6-free-llm-chain-order)",
        "",
        _architecture_section(),
        _common_issues_section(),
        _emergency_section(),
        _cron_section(),
        _db_schema_section(),
        _llm_chain_section(),
        "",
        "---",
        "_End of runbook. Never paste DSNs / tokens / passwords here — this "
        "document is generated and re-generated weekly._",
    ]
    body = _scrub("\n".join(sections))
    OUTPUT_PATH.write_text(body, encoding="utf-8")
    try:
        upsert_doc_inventory(str(OUTPUT_PATH), "admin_runbook", len(body.encode("utf-8")))
    except Exception:
        pass
    return OUTPUT_PATH


if __name__ == "__main__":
    p = generate()
    print(f"[runbook_generator] wrote {p} ({p.stat().st_size} bytes)")
