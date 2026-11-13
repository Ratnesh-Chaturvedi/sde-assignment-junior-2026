"""
Event ingestion engine: reads JSONL call events into an append-only ledger,
handles deduplication, unknown dispositions, and orphan reconciliation.
"""
import json
from pathlib import Path
from typing import Dict, Set
import sqlite3

from solution.normalizers import normalize_phone, parse_timestamp
from solution.security import hash_phone


def load_valid_dispositions(conn: sqlite3.Connection) -> Set[tuple[str, str]]:
    """Loads valid (workflow_id, disposition_code) pairs from dispositions table."""
    cur = conn.cursor()
    cur.execute("SELECT workflow_id, code FROM dispositions")
    return set((row["workflow_id"], row["code"]) for row in cur.fetchall())


def reconcile_orphans(conn: sqlite3.Connection):
    """
    Reconciles orphan events whose lead has arrived in a later batch/drop:
    1. Promotes orphan events whose (workflow_id, phone_hash) now exists in leads.
    2. Promotes orphan overrides whose call_id now exists in events.
    Iterates until no more orphans can be promoted.
    """
    while True:
        promoted_count = 0
        # 1. Promote direct orphans matching newly ingested leads
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT o.event_id, o.event_type, o.workflow_id, o.call_id, o.occurred_at,
                   o.phone_hash, o.payload_json, o.drop_name
            FROM orphan_events o
            JOIN leads l ON o.workflow_id = l.workflow_id AND o.phone_hash = l.phone_hash
            WHERE o.phone_hash IS NOT NULL AND o.phone_hash != ''
        """)
        orphans_to_promote = cur.fetchall()

        if orphans_to_promote:
            for o in orphans_to_promote:
                dt_ist = parse_timestamp(o["occurred_at"])
                conn.execute("""
                    INSERT OR IGNORE INTO events (
                        event_id, event_type, workflow_id, call_id, occurred_at,
                        occurred_date, phone_hash, payload_json, drop_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    o["event_id"], o["event_type"], o["workflow_id"], o["call_id"],
                    o["occurred_at"], dt_ist.strftime("%Y-%m-%d"), o["phone_hash"],
                    o["payload_json"], o["drop_name"]
                ))
                conn.execute("DELETE FROM orphan_events WHERE event_id = ?", (o["event_id"],))
                promoted_count += 1

        # 2. Promote orphan overrides whose call_id now exists in events
        cur.execute("""
            SELECT DISTINCT o.event_id, o.event_type, o.workflow_id, o.call_id, o.occurred_at,
                   o.payload_json, o.drop_name, e.phone_hash
            FROM orphan_events o
            JOIN events e ON o.call_id = e.call_id
            WHERE o.event_type = 'disposition.override'
        """)
        overrides_to_promote = cur.fetchall()

        if overrides_to_promote:
            for o in overrides_to_promote:
                dt_ist = parse_timestamp(o["occurred_at"])
                conn.execute("""
                    INSERT OR IGNORE INTO events (
                        event_id, event_type, workflow_id, call_id, occurred_at,
                        occurred_date, phone_hash, payload_json, drop_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    o["event_id"], o["event_type"], o["workflow_id"], o["call_id"],
                    o["occurred_at"], dt_ist.strftime("%Y-%m-%d"), o["phone_hash"],
                    o["payload_json"], o["drop_name"]
                ))
                conn.execute("DELETE FROM orphan_events WHERE event_id = ?", (o["event_id"],))
                promoted_count += 1

        if promoted_count == 0:
            break


def ingest_event_file(
    file_path: Path,
    drop_name: str,
    conn: sqlite3.Connection,
    stats: dict
):
    """
    Ingests one JSON Lines event file following exact spec order:
    1. Malformed -> skip
    2. Duplicate event_id -> skip
    3. Unknown disposition -> skip
    4. Link to lead; if unlinked -> orphan_events
    """
    valid_disps = load_valid_dispositions(conn)
    cur = conn.cursor()

    # Preload existing event_ids in memory for fast O(1) deduplication check
    cur.execute("SELECT event_id FROM events UNION SELECT event_id FROM orphan_events")
    seen_event_ids: Set[str] = set(row[0] for row in cur.fetchall())

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            # Spec: "events_total counts every non-empty line read, including malformed and duplicate lines."
            stats["events_total"] += 1

            # 1. Check malformed
            try:
                event = json.loads(stripped)
            except Exception:
                stats["malformed_events"] += 1
                continue

            if not isinstance(event, dict) or "event_id" not in event or not event["event_id"]:
                stats["malformed_events"] += 1
                continue

            event_id = str(event["event_id"]).strip()

            # 2. Check duplicate event_id
            if event_id in seen_event_ids:
                stats["duplicate_events_dropped"] += 1
                continue
            seen_event_ids.add(event_id)

            event_type = event.get("event_type", "")
            workflow_id = event.get("workflow_id", "")
            call_id = event.get("call_id", "")
            occurred_at = event.get("occurred_at", "")
            payload = event.get("payload", {})

            # 3. Check unknown disposition
            if event_type.startswith("disposition."):
                disp_code = payload.get("disposition_code")
                if not disp_code or (workflow_id, disp_code) not in valid_disps:
                    stats["unknown_disposition_events"] += 1
                    continue

            # Standardize timestamp to IST
            try:
                dt_ist = parse_timestamp(occurred_at)
                occurred_date = dt_ist.strftime("%Y-%m-%d")
            except Exception:
                stats["malformed_events"] += 1
                continue

            # 4. Link event to lead
            phone_hash = None

            if event_type == "disposition.override":
                # Override links via call_id
                cur.execute("SELECT phone_hash FROM events WHERE call_id = ? LIMIT 1", (call_id,))
                row = cur.fetchone()
                if row and row["phone_hash"]:
                    phone_hash = row["phone_hash"]
                else:
                    # Check orphan_events for this call_id
                    cur.execute("SELECT phone_hash FROM orphan_events WHERE call_id = ? AND phone_hash IS NOT NULL LIMIT 1", (call_id,))
                    orow = cur.fetchone()
                    if orow and orow["phone_hash"]:
                        phone_hash = orow["phone_hash"]
            else:
                raw_phone = event.get("phone", "")
                norm_phone = normalize_phone(raw_phone)
                if norm_phone:
                    candidate_h = hash_phone(norm_phone)
                    # Check if lead exists in leads table
                    cur.execute("SELECT 1 FROM leads WHERE workflow_id = ? AND phone_hash = ? LIMIT 1", (workflow_id, candidate_h))
                    if cur.fetchone():
                        phone_hash = candidate_h
                    else:
                        phone_hash = candidate_h  # will be stored as orphan with phone_hash for future reconciliation

            # Check if linked to an existing valid lead
            cur.execute("SELECT 1 FROM leads WHERE workflow_id = ? AND phone_hash = ? LIMIT 1", (workflow_id, phone_hash or ""))
            is_linked = cur.fetchone() is not None

            if is_linked:
                # Store in append-only events ledger
                conn.execute("""
                    INSERT INTO events (
                        event_id, event_type, workflow_id, call_id, occurred_at,
                        occurred_date, phone_hash, payload_json, drop_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id, event_type, workflow_id, call_id, occurred_at,
                    occurred_date, phone_hash, json.dumps(payload), drop_name
                ))
            else:
                # Store in orphan_events
                conn.execute("""
                    INSERT INTO orphan_events (
                        event_id, event_type, workflow_id, call_id, occurred_at,
                        phone_hash, payload_json, drop_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id, event_type, workflow_id, call_id, occurred_at,
                    phone_hash or "", json.dumps(payload), drop_name
                ))

