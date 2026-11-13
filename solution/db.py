"""
Database schema, connection manager, and initialization for SQLite.
"""
import sqlite3
from pathlib import Path
from typing import Optional
import csv
from solution.config import DEFAULT_DB_PATH, DISPOSITIONS_PATH


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Returns an active SQLite connection with foreign keys enabled and row factory set.
    """
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")  # High performance & safe concurrency
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection):
    """
    Creates all necessary tables, indexes, and constraints if they do not exist.
    """
    with conn:
        # 1. Ingested drops tracker for idempotency
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingested_drops (
                drop_name TEXT PRIMARY KEY,
                ingested_at TEXT NOT NULL
            );
        """)

        # 2. Leads table (PII protected with blind index phone_hash)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                masked_phone TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                name_hash TEXT NOT NULL,
                attributes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workflow_id, phone_hash)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_ref ON leads(workflow_id, external_ref);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(workflow_id, batch_id);")

        # 3. Lead rejections (No PII)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                drop_name TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rejections_order ON rejections(workflow_id, batch_id, row_number);")

        # 4. Dispositions catalogue
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispositions (
                workflow_id TEXT NOT NULL,
                code TEXT NOT NULL,
                label TEXT NOT NULL,
                is_terminal INTEGER NOT NULL,
                is_success INTEGER NOT NULL,
                PRIMARY KEY (workflow_id, code)
            );
        """)

        # 5. Events table (Append-only interaction ledger)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_date TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                drop_name TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_phone ON events(workflow_id, phone_hash);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(workflow_id, occurred_date);")

        # 6. Orphan events table (Events that cannot be linked to a lead yet)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orphan_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                phone_hash TEXT,
                payload_json TEXT NOT NULL,
                drop_name TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orphans_phone ON orphan_events(workflow_id, phone_hash);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orphans_call ON orphan_events(call_id);")

        # 7. Ingestion metrics tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingestion_metrics (
                metric_key TEXT PRIMARY KEY,
                metric_value INTEGER NOT NULL DEFAULT 0
            );
        """)

    # Populate dispositions table if empty
    load_dispositions_catalogue(conn)


def load_dispositions_catalogue(conn: sqlite3.Connection):
    """
    Populates the dispositions table from dispositions.csv if not already loaded.
    """
    if not DISPOSITIONS_PATH.exists():
        return

    with open(DISPOSITIONS_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        with conn:
            for row in reader:
                conn.execute("""
                    INSERT OR REPLACE INTO dispositions (workflow_id, code, label, is_terminal, is_success)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    row["workflow_id"].strip(),
                    row["code"].strip(),
                    row["label"].strip(),
                    1 if row["is_terminal"].strip().lower() == "true" else 0,
                    1 if row["is_success"].strip().lower() == "true" else 0
                ))

