import os
"""Deploy agent M1 — apply PHASE BM migrations to Railway intelligence DB."""
import sys
import psycopg2

DSN = os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or ""

MIGRATIONS = [
    ("L9  user_memory",           r"C:\Projects\shared\user_memory\migrations.sql"),
    ("L10 proactive_agent",       r"C:\Projects\shared\proactive_agent\migrations.sql"),
    ("L11 causal_engine",         r"C:\Projects\shared\causal_engine\schema.sql"),
    ("L12 agent_bus",             r"C:\Projects\shared\agent_bus\migrations.sql"),
    ("L13 market_world_model",    r"C:\Projects\shared\market_world_model\schema.sql"),
    ("L15 meta_learner",          r"C:\Projects\shared\meta_learner\migrations.sql"),
    ("L16 external_knowledge",    r"C:\Projects\shared\external_knowledge\migrations.sql"),
    ("L17 causal_inference",      r"C:\Projects\shared\causal_inference\schema.sql"),
    ("L18-L22 tier4_5",           r"C:\Projects\shared\migrations\2026-06-03_phase_bm_l_tier4_5.sql"),
]

CHECK_PATTERNS = [
    "user_memory%", "proactive_%", "causal_%", "agent_events", "agent_registry",
    "cross_bot_%", "market_%", "meta_learning%", "meta_strategy%",
    "external_signals", "multimodal_inputs", "content_pipeline%",
    "virtual_tours", "code_proposals", "continuous_%",
]


def main():
    print(f"[M1] Connecting to Railway intelligence DB...")
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    results = []

    for label, path in MIGRATIONS:
        try:
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"[OK]   {label} ({path})")
            results.append((label, "success", None))
        except FileNotFoundError as e:
            conn.rollback()
            print(f"[SKIP] {label} -- file not found: {path}")
            results.append((label, "skip", "file not found"))
        except Exception as e:
            conn.rollback()
            err = str(e).split("\n")[0]
            print(f"[ERR]  {label} -- {err}")
            results.append((label, "error", err))

    # verification
    print("\n[M1] Verifying created tables...")
    placeholders = ",".join(["%s"] * len(CHECK_PATTERNS))
    q = f"""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public'
          AND table_name ILIKE ANY(ARRAY[{placeholders}])
        ORDER BY table_name
    """
    with conn.cursor() as cur:
        cur.execute(q, CHECK_PATTERNS)
        tables = [r[0] for r in cur.fetchall()]

    print(f"\n[M1] Found {len(tables)} matching tables:")
    for t in tables:
        print(f"     - {t}")

    print("\n[M1] === MIGRATION SUMMARY ===")
    for label, status, err in results:
        marker = {"success": "OK", "skip": "SKIP", "error": "ERR"}[status]
        line = f"  [{marker}] {label}"
        if err:
            line += f" :: {err}"
        print(line)

    conn.close()
    err_count = sum(1 for _, s, _ in results if s == "error")
    sys.exit(1 if err_count else 0)


if __name__ == "__main__":
    main()
