import os
import time
import psycopg2
from datetime import datetime

LIVE_DATABASE_URL = os.getenv("LIVE_DATABASE_URL")
ARCHIVE_DATABASE_URL = os.getenv("ARCHIVE_DATABASE_URL")
INTELLIGENCE_DATABASE_URL = os.getenv("INTELLIGENCE_DATABASE_URL")

print("=" * 60)
print("Dubai DLD Intelligence Updater started")
print("Time:", datetime.utcnow())
print("=" * 60)

if not LIVE_DATABASE_URL:
    raise RuntimeError("LIVE_DATABASE_URL is not set")

if not ARCHIVE_DATABASE_URL:
    raise RuntimeError("ARCHIVE_DATABASE_URL is not set")

if not INTELLIGENCE_DATABASE_URL:
    raise RuntimeError("INTELLIGENCE_DATABASE_URL is not set")

print("Connecting to LIVE database...")
live_conn = psycopg2.connect(LIVE_DATABASE_URL)
print("LIVE database connected")

print("Connecting to ARCHIVE database...")
archive_conn = psycopg2.connect(ARCHIVE_DATABASE_URL)
print("ARCHIVE database connected")

print("Connecting to INTELLIGENCE database...")
intel_conn = psycopg2.connect(INTELLIGENCE_DATABASE_URL)
print("INTELLIGENCE database connected")

live_cursor = live_conn.cursor()
archive_cursor = archive_conn.cursor()
intel_cursor = intel_conn.cursor()

print("Starting intelligence loop...")

while True:
    try:
        print("-" * 60)
        print("New intelligence cycle:", datetime.utcnow())

        # TEST LIVE
        live_cursor.execute("""
            SELECT COUNT(*)
            FROM public.dld_transactions_full
        """)
        live_count = live_cursor.fetchone()[0]

        # TEST ARCHIVE
        archive_cursor.execute("""
            SELECT COUNT(*)
            FROM public.dld_sale_archive
        """)
        archive_count = archive_cursor.fetchone()[0]

        print(f"LIVE deals count: {live_count}")
        print(f"ARCHIVE deals count: {archive_count}")

        # CREATE TEST TABLE
        intel_cursor.execute("""
            CREATE TABLE IF NOT EXISTS intelligence_status (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP,
                live_count BIGINT,
                archive_count BIGINT
            )
        """)

        intel_cursor.execute("""
            INSERT INTO intelligence_status (
                created_at,
                live_count,
                archive_count
            )
            VALUES (%s, %s, %s)
        """, (
            datetime.utcnow(),
            live_count,
            archive_count
        ))

        intel_conn.commit()

        print("Intelligence status updated successfully")

    except Exception as e:
        print("ERROR:", str(e))

        try:
            intel_conn.rollback()
        except:
            pass

    print("Sleeping 300 seconds...")
    time.sleep(300)
