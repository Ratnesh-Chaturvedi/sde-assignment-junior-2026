"""
Reporting engine: aggregates data from SQLite and generates the 5 required files:
1. batch_summary.csv
2. daily_overview.csv
3. lead_report.csv
4. rejections.csv
5. data_quality.json
"""
import csv
import json
from pathlib import Path
from typing import Dict, Any, List
import sqlite3

from solution.normalizers import parse_timestamp
from solution.ingestion.events import reconcile_orphans


def format_iso_ist(occurred_at_str: str) -> str:
    """Converts occurred_at to ISO-8601 in IST format: YYYY-MM-DDTHH:MM:SS+05:30."""
    if not occurred_at_str:
        return ""
    dt = parse_timestamp(occurred_at_str)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def generate_reports(conn: sqlite3.Connection, out_dir: Path):
    """Generates all 5 report files into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Reconcile any remaining orphans before generating reports
    reconcile_orphans(conn)

    # 1. Load disposition metadata: code -> (is_terminal, is_success)
    cur = conn.cursor()
    cur.execute("SELECT workflow_id, code, is_terminal, is_success FROM dispositions")
    disp_meta: Dict[tuple[str, str], tuple[bool, bool]] = {}
    for r in cur.fetchall():
        disp_meta[(r["workflow_id"], r["code"])] = (bool(r["is_terminal"]), bool(r["is_success"]))

    # 2. Extract call-level summary
    # A call is counted only if call.initiated event is present and linked.
    cur.execute("""
        SELECT call_id, workflow_id, phone_hash, occurred_at, occurred_date
        FROM events
        WHERE event_type = 'call.initiated'
    """)
    calls: Dict[str, dict] = {}
    for r in cur.fetchall():
        cid = r["call_id"]
        calls[cid] = {
            "call_id": cid,
            "workflow_id": r["workflow_id"],
            "phone_hash": r["phone_hash"],
            "initiated_at": r["occurred_at"],
            "date": r["occurred_date"],
            "is_connected": False,
            "has_telephony_failed": False,
            "disposition_set": None,       # (occurred_at, code)
            "disposition_override": None,  # (occurred_at, code)
            "effective_disposition": None,
        }

    # Fetch other events for these valid calls
    cur.execute("""
        SELECT call_id, event_type, occurred_at, payload_json
        FROM events
        WHERE call_id IN (SELECT call_id FROM events WHERE event_type = 'call.initiated')
    """)
    for r in cur.fetchall():
        cid = r["call_id"]
        if cid not in calls:
            continue
        etype = r["event_type"]
        when = r["occurred_at"]
        payload = json.loads(r["payload_json"])

        if etype == "call.connected":
            calls[cid]["is_connected"] = True
        elif etype == "call.telephony_failed":
            calls[cid]["has_telephony_failed"] = True
        elif etype == "disposition.set":
            code = payload.get("disposition_code")
            curr = calls[cid]["disposition_set"]
            if curr is None or parse_timestamp(when) > parse_timestamp(curr[0]):
                calls[cid]["disposition_set"] = (when, code)
        elif etype == "disposition.override":
            code = payload.get("disposition_code")
            curr = calls[cid]["disposition_override"]
            if curr is None or parse_timestamp(when) > parse_timestamp(curr[0]):
                calls[cid]["disposition_override"] = (when, code)

    # Compute effective disposition for each call
    for cid, cinfo in calls.items():
        if cinfo["disposition_override"]:
            cinfo["effective_disposition"] = cinfo["disposition_override"][1]
        elif cinfo["disposition_set"]:
            cinfo["effective_disposition"] = cinfo["disposition_set"][1]
        else:
            cinfo["effective_disposition"] = None

    # Group calls by lead: (workflow_id, phone_hash) -> list of calls
    calls_by_lead: Dict[tuple[str, str], List[dict]] = {}
    for c in calls.values():
        key = (c["workflow_id"], c["phone_hash"])
        calls_by_lead.setdefault(key, []).append(c)

    # 3. Load all leads from database
    cur.execute("""
        SELECT workflow_id, phone_hash, masked_phone, batch_id, external_ref
        FROM leads
    """)
    leads_rows = cur.fetchall()

    leads_data: List[dict] = []
    for l in leads_rows:
        wf = l["workflow_id"]
        ph = l["phone_hash"]
        lead_calls = calls_by_lead.get((wf, ph), [])
        total_calls = len(lead_calls)
        connected_calls = sum(1 for c in lead_calls if c["is_connected"])

        # Determine bucket:
        # 1. yet_to_reach_out: no calls
        # 2. completed: any call's effective disposition is terminal
        # 3. in_progress_telephony_failed: every call has call.telephony_failed
        # 4. in_progress_dialed: everything else
        if total_calls == 0:
            bucket = "yet_to_reach_out"
        elif any(
            c["effective_disposition"] and disp_meta.get((wf, c["effective_disposition"]), (False, False))[0]
            for c in lead_calls
        ):
            bucket = "completed"
        elif all(c["has_telephony_failed"] for c in lead_calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"

        # Sort lead calls by initiated_at
        sorted_calls = sorted(lead_calls, key=lambda c: parse_timestamp(c["initiated_at"]))
        last_call_at = format_iso_ist(sorted_calls[-1]["initiated_at"]) if sorted_calls else ""

        # current_disposition: effective disposition of most recent call (by initiated time) that has one
        calls_with_disp = [c for c in sorted_calls if c["effective_disposition"] is not None]
        current_disp = calls_with_disp[-1]["effective_disposition"] if calls_with_disp else ""

        # ever_success: whether any call's effective disposition has is_success=true
        ever_succ = any(
            c["effective_disposition"] and disp_meta.get((wf, c["effective_disposition"]), (False, False))[1]
            for c in lead_calls
        )

        leads_data.append({
            "workflow_id": wf,
            "external_ref": l["external_ref"],
            "batch_id": l["batch_id"],
            "phone_masked": l["masked_phone"],
            "bucket": bucket,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": last_call_at,
            "current_disposition": current_disp,
            "ever_success": "true" if ever_succ else "false",
        })

    # --- Write File 1: batch_summary.csv ---
    # sorted by workflow_id, batch_id
    batch_summary_map: Dict[tuple[str, str], dict] = {}
    for ld in leads_data:
        bkey = (ld["workflow_id"], ld["batch_id"])
        if bkey not in batch_summary_map:
            batch_summary_map[bkey] = {
                "workflow_id": ld["workflow_id"],
                "batch_id": ld["batch_id"],
                "total_leads": 0,
                "yet_to_reach_out": 0,
                "in_progress_telephony_failed": 0,
                "in_progress_dialed": 0,
                "completed": 0,
            }
        batch_summary_map[bkey]["total_leads"] += 1
        b_bucket = ld["bucket"]
        if b_bucket in batch_summary_map[bkey]:
            batch_summary_map[bkey][b_bucket] += 1

    sorted_batch_summary = sorted(batch_summary_map.values(), key=lambda r: (r["workflow_id"], r["batch_id"]))
    with open(out_dir / "batch_summary.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
            "in_progress_telephony_failed", "in_progress_dialed", "completed"
        ])
        writer.writeheader()
        for r in sorted_batch_summary:
            writer.writerow(r)

    # --- Write File 2: daily_overview.csv ---
    # sorted by workflow_id, date
    # one row per workflow per IST date that has at least one call
    daily_map: Dict[tuple[str, str], dict] = {}
    for c in calls.values():
        dkey = (c["workflow_id"], c["date"])
        if dkey not in daily_map:
            daily_map[dkey] = {
                "workflow_id": c["workflow_id"],
                "date": c["date"],
                "total_calls": 0,
                "connected_calls": 0,
                "success_leads_set": set(),
            }
        daily_map[dkey]["total_calls"] += 1
        if c["is_connected"]:
            daily_map[dkey]["connected_calls"] += 1

        eff = c["effective_disposition"]
        if eff and disp_meta.get((c["workflow_id"], eff), (False, False))[1]:
            daily_map[dkey]["success_leads_set"].add(c["phone_hash"])

    sorted_daily = sorted(daily_map.values(), key=lambda r: (r["workflow_id"], r["date"]))
    with open(out_dir / "daily_overview.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"
        ])
        writer.writeheader()
        for r in sorted_daily:
            tc = r["total_calls"]
            cc = r["connected_calls"]
            conn_rate = f"{cc / tc:.4f}" if tc > 0 else "0.0000"
            writer.writerow({
                "workflow_id": r["workflow_id"],
                "date": r["date"],
                "total_calls": tc,
                "connected_calls": cc,
                "connectivity_rate": conn_rate,
                "success_leads": len(r["success_leads_set"]),
            })

    # --- Write File 3: lead_report.csv ---
    # sorted by workflow_id, external_ref
    sorted_leads = sorted(leads_data, key=lambda r: (r["workflow_id"], r["external_ref"]))
    with open(out_dir / "lead_report.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
            "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"
        ])
        writer.writeheader()
        for r in sorted_leads:
            writer.writerow(r)

    # --- Write File 4: rejections.csv ---
    # sorted by workflow_id, batch_id, row_number
    cur.execute("""
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id, batch_id, row_number
    """)
    rejections = cur.fetchall()
    with open(out_dir / "rejections.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["workflow_id", "batch_id", "row_number", "reason"])
        writer.writeheader()
        for r in rejections:
            writer.writerow({
                "workflow_id": r["workflow_id"],
                "batch_id": r["batch_id"],
                "row_number": r["row_number"],
                "reason": r["reason"]
            })

    # --- Write File 5: data_quality.json ---
    cur.execute("SELECT metric_key, metric_value FROM ingestion_metrics")
    saved_metrics = {r["metric_key"]: r["metric_value"] for r in cur.fetchall()}

    # Count final orphan events
    cur.execute("SELECT COUNT(*) FROM orphan_events")
    final_orphan_count = cur.fetchone()[0]

    # Count rejections by reason
    cur.execute("""
        SELECT reason, COUNT(*) as cnt
        FROM rejections
        GROUP BY reason
        ORDER BY reason
    """)
    reasons_dict = {r["reason"]: r["cnt"] for r in cur.fetchall()}

    cur.execute("SELECT COUNT(*) FROM rejections")
    total_rejections = cur.fetchone()[0]

    dq = {
        "lead_rows_total": saved_metrics.get("lead_rows_total", 0),
        "lead_rows_rejected": total_rejections,
        "rejections_by_reason": reasons_dict,
        "duplicate_lead_rows_merged": saved_metrics.get("duplicate_lead_rows_merged", 0),
        "field_warnings": saved_metrics.get("field_warnings", 0),
        "events_total": saved_metrics.get("events_total", 0),
        "malformed_events": saved_metrics.get("malformed_events", 0),
        "duplicate_events_dropped": saved_metrics.get("duplicate_events_dropped", 0),
        "orphan_events": final_orphan_count,
        "unknown_disposition_events": saved_metrics.get("unknown_disposition_events", 0),
    }

    with open(out_dir / "data_quality.json", "w", encoding="utf-8") as f:
        json.dump(dq, f, indent=2)

