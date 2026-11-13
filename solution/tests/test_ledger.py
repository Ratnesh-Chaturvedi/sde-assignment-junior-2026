"""
Tests for append-only ledger, effective disposition overrides, and bucket logic.
"""
import sqlite3
import pytest
from solution.db import init_db
from solution.normalizers import parse_timestamp
from solution.security import hash_phone, mask_phone
from solution.reporting import generate_reports
import csv
from pathlib import Path


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


class TestEffectiveDispositionAndLedger:
    def test_override_precedence(self, test_db, tmp_path):
        conn = test_db
        wf = "motor_renewal"
        phone = "+919876543210"
        ph = hash_phone(phone)

        # 1. Insert a lead
        conn.execute("""
            INSERT INTO leads (workflow_id, phone_hash, masked_phone, batch_id, external_ref, name_hash, attributes_json, created_at, updated_at)
            VALUES (?, ?, ?, 'batch_001', 'MOT/2026/000001', 'name_hash', '{}', '2026-10-01 10:00:00', '2026-10-01 10:00:00')
        """, (wf, ph, mask_phone(phone)))

        # 2. Append call.initiated
        conn.execute("""
            INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, payload_json, drop_name)
            VALUES ('evt_1', 'call.initiated', ?, 'call_1', '2026-10-01 10:00:00', '2026-10-01', ?, '{}', 'drop_1')
        """, (wf, ph))

        # 3. Append call.connected
        conn.execute("""
            INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, payload_json, drop_name)
            VALUES ('evt_2', 'call.connected', ?, 'call_1', '2026-10-01 10:00:30', '2026-10-01', ?, '{}', 'drop_1')
        """, (wf, ph))

        # 4. Append disposition.set -> callback_requested (non-terminal)
        conn.execute("""
            INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, payload_json, drop_name)
            VALUES ('evt_3', 'disposition.set', ?, 'call_1', '2026-10-01 10:02:00', '2026-10-01', ?, '{"disposition_code": "callback_requested"}', 'drop_1')
        """, (wf, ph))

        # Check before override
        generate_reports(conn, tmp_path)
        with open(tmp_path / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert rows[0]["bucket"] == "in_progress_dialed"
            assert rows[0]["current_disposition"] == "callback_requested"
            assert rows[0]["ever_success"] == "false"

        # 5. Append disposition.override -> renewed (terminal and success)
        # Note: Original event evt_3 is NOT edited or deleted!
        conn.execute("""
            INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, payload_json, drop_name)
            VALUES ('evt_4', 'disposition.override', ?, 'call_1', '2026-10-01 14:00:00', '2026-10-01', ?, '{"disposition_code": "renewed"}', 'drop_1')
        """, (wf, ph))

        # Check after override
        generate_reports(conn, tmp_path)
        with open(tmp_path / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert rows[0]["bucket"] == "completed"
            assert rows[0]["current_disposition"] == "renewed"
            assert rows[0]["ever_success"] == "true"


class TestBucketLogic:
    def test_bucket_yet_to_reach_out(self, test_db, tmp_path):
        conn = test_db
        conn.execute("""
            INSERT INTO leads (workflow_id, phone_hash, masked_phone, batch_id, external_ref, name_hash, attributes_json, created_at, updated_at)
            VALUES ('motor_renewal', 'hash_1', '+91******1111', 'b1', 'REF1', 'nh', '{}', '2026-10-01', '2026-10-01')
        """)
        generate_reports(conn, tmp_path)
        with open(tmp_path / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            assert rows[0]["bucket"] == "yet_to_reach_out"

    def test_bucket_telephony_failed(self, test_db, tmp_path):
        conn = test_db
        wf = "motor_renewal"
        ph = "hash_2"
        conn.execute("""
            INSERT INTO leads (workflow_id, phone_hash, masked_phone, batch_id, external_ref, name_hash, attributes_json, created_at, updated_at)
            VALUES (?, ?, '+91******2222', 'b1', 'REF2', 'nh', '{}', '2026-10-01', '2026-10-01')
        """, (wf, ph))

        # 2 calls, both had call.telephony_failed
        for i in [1, 2]:
            conn.execute(f"""
                INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, payload_json, drop_name)
                VALUES ('e_init_{i}', 'call.initiated', '{wf}', 'call_{i}', '2026-10-01 10:0{i}:00', '2026-10-01', '{ph}', '{{}}', 'drop_1')
            """)
            conn.execute(f"""
                INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, payload_json, drop_name)
                VALUES ('e_fail_{i}', 'call.telephony_failed', '{wf}', 'call_{i}', '2026-10-01 10:0{i}:05', '2026-10-01', '{ph}', '{{}}', 'drop_1')
            """)

        generate_reports(conn, tmp_path)
        with open(tmp_path / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            assert rows[0]["bucket"] == "in_progress_telephony_failed"

