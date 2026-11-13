"""
Command-line interface for running ingestion and reporting.
Usage:
    python -m solution ingest data/drop_1
    python -m solution ingest data/drop_2
    python -m solution report --out reports/
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime

from solution.config import DEFAULT_DB_PATH
from solution.db import get_connection, init_db
from solution.ingestion.leads import ingest_lead_file
from solution.ingestion.events import ingest_event_file, reconcile_orphans
from solution.reporting import generate_reports


def run_ingest(drop_path_str: str, db_path: Path = DEFAULT_DB_PATH):
    drop_path = Path(drop_path_str).resolve()
    if not drop_path.exists():
        print(f"Error: Drop path '{drop_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    drop_name = drop_path.name
    conn = get_connection(db_path)
    init_db(conn)

    # Check idempotency: has this drop already been ingested?
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM ingested_drops WHERE drop_name = ?", (drop_name,))
    if cur.fetchone():
        print(f"Notice: Drop '{drop_name}' was already ingested. Skipping to ensure strict idempotency.")
        return

    print(f"==> Starting ingestion for drop: {drop_name}")

    stats = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "rejections_by_reason": {},
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "unknown_disposition_events": 0,
    }

    # 1. Ingest Leads: process workflows and batches in sorted order
    leads_base = drop_path / "leads"
    if leads_base.exists():
        workflow_dirs = sorted([p for p in leads_base.iterdir() if p.is_dir()])
        for wf_dir in workflow_dirs:
            wf_id = wf_dir.name
            csv_files = sorted(wf_dir.glob("*.csv"))
            for csv_file in csv_files:
                batch_id = csv_file.stem
                print(f"  Ingesting leads [{wf_id}]: {csv_file.name} (batch: {batch_id})")
                ingest_lead_file(csv_file, wf_id, batch_id, drop_name, conn, stats)

    # 2. Ingest Events in filename order
    events_base = drop_path / "events"
    if events_base.exists():
        jsonl_files = sorted(events_base.glob("*.jsonl"))
        for jfile in jsonl_files:
            print(f"  Ingesting events: {jfile.name}")
            ingest_event_file(jfile, drop_name, conn, stats)

    # 3. Reconcile orphan events with leads
    print("  Reconciling orphan events...")
    reconcile_orphans(conn)

    # 4. Update cumulative ingestion metrics in DB
    with conn:
        for k, v in stats.items():
            if k == "rejections_by_reason":
                continue
            cur.execute("""
                INSERT INTO ingestion_metrics (metric_key, metric_value)
                VALUES (?, ?)
                ON CONFLICT(metric_key) DO UPDATE SET metric_value = metric_value + excluded.metric_value
            """, (k, v))

        conn.execute("""
            INSERT INTO ingested_drops (drop_name, ingested_at)
            VALUES (?, ?)
        """, (drop_name, datetime.now().isoformat()))

    print(f"==> Ingestion for '{drop_name}' completed successfully!")
    print(f"    Leads processed: {stats['lead_rows_total']} (Rejected: {stats['lead_rows_rejected']}, Merged: {stats['duplicate_lead_rows_merged']})")
    print(f"    Events processed: {stats['events_total']} (Malformed: {stats['malformed_events']}, Dropped duplicates: {stats['duplicate_events_dropped']})")


def run_report(out_dir_str: str, db_path: Path = DEFAULT_DB_PATH):
    out_dir = Path(out_dir_str).resolve()
    conn = get_connection(db_path)
    init_db(conn)

    print(f"==> Generating reports into: {out_dir}")
    generate_reports(conn, out_dir)
    print(f"==> Reports successfully generated in '{out_dir}':")
    for fname in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        fpath = out_dir / fname
        if fpath.exists():
            print(f"    - {fname} ({fpath.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Agentic Universe - Interaction Ledger & Reporting Pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: ingest
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop directory (e.g. data/drop_1)")
    ingest_parser.add_argument("drop_path", help="Path to drop folder")
    ingest_parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")

    # Subcommand: report
    report_parser = subparsers.add_parser("report", help="Generate all reports")
    report_parser.add_argument("--out", required=True, help="Output directory for reports")
    report_parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")

    args = parser.parse_args()

    if args.command == "ingest":
        run_ingest(args.drop_path, Path(args.db))
    elif args.command == "report":
        run_report(args.out, Path(args.db))


if __name__ == "__main__":
    main()

